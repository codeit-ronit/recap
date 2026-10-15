"""Tests for the simulated environment (recap/environments/simulated).

They check the world rules from the design brief: determinism, how each fault reacts to
restarts and rollbacks, error propagation to callers, the simulated clock, action
validation and auditing, and that two environment objects with the same run_id share one
world (exactly what the MCP server subprocess and the controller's oracle rely on).
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from recap.core.config import REPO_ROOT
from recap.core.observations import AuthorizedAction
from recap.core.signals import (
    ERROR_RATE_THRESHOLD,
    MEMORY_PRESSURE_RATIO,
    is_oom_message,
    upstream_dependency,
    version_tag,
)
from recap.environments.simulated import SimulatedEnvironment, engine, physics
from recap.environments.simulated.scenario import load_scenario
from recap.verification.oracle import EnvironmentHealthOracle, recovery_result

SCENARIOS = REPO_ROOT / "fixtures" / "scenarios"
METRICS = ["error_rate", "latency_p95_ms", "memory_mb", "cpu_pct", "restarts"]


def make_env(
    tmp_path: Path, run_id: str = "run-test", scenarios_dir: Path = SCENARIOS, **latency: int
) -> SimulatedEnvironment:
    return SimulatedEnvironment(
        run_id=run_id,
        scenarios_dir=str(scenarios_dir),
        state_dir=str(tmp_path / "sandbox"),
        **latency,
    )


async def start(
    tmp_path: Path, scenario_id: str, run_id: str = "run-test", **latency: int
) -> SimulatedEnvironment:
    """A fresh environment reset into `scenario_id` with the scenario's own seed."""
    env = make_env(tmp_path, run_id, **latency)
    await env.reset(scenario_id, load_scenario(SCENARIOS, scenario_id).seed)
    return env


def rollback(service: str, version: str) -> AuthorizedAction:
    return AuthorizedAction(action="rollback", target=service, params={"to_version": version})


def restart(service: str) -> AuthorizedAction:
    return AuthorizedAction(action="restart", target=service)


def alert_time(scenario_id: str) -> datetime:
    return load_scenario(SCENARIOS, scenario_id).incident_time


async def everything_observable(env: SimulatedEnvironment, service: str) -> list[dict[str, Any]]:
    """Every read an agent (or the oracle) could make about one service, as plain JSON."""
    results = [
        await env.observe_services(),
        await env.observe_health(service),
        await env.observe_logs(service, 100, 120),
        await env.observe_deployments(None, 20),
        await env.observe_events(None, 50),
        await env.plan_rollback(service),
        await env.health_check(),
    ]
    results += [await env.observe_metrics(service, metric, 60) for metric in METRICS]
    return [result.model_dump(mode="json") for result in results]


# --- lifecycle and input checks ---------------------------------------------------------


async def test_reads_and_writes_before_reset_raise(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    with pytest.raises(RuntimeError, match="environment not reset"):
        await env.observe_health("checkout")
    with pytest.raises(RuntimeError, match="environment not reset"):
        await env.health_check()
    with pytest.raises(RuntimeError, match="environment not reset"):
        await env.execute_authorized(restart("checkout"))


async def test_unknown_service_raises_value_error(tmp_path: Path) -> None:
    env = await start(tmp_path, "bad-deployment")
    reads = [
        lambda: env.observe_health("billing"),
        lambda: env.observe_logs("billing", 10, 60),
        lambda: env.observe_metrics("billing", "error_rate", 30),
        lambda: env.observe_deployments("billing", 5),
        lambda: env.observe_events("billing", 5),
        lambda: env.plan_rollback("billing"),
    ]
    for read in reads:
        with pytest.raises(ValueError, match="unknown service 'billing'"):
            await read()


def test_describe_labels_the_environment_as_simulated(tmp_path: Path) -> None:
    info = make_env(tmp_path).describe()
    assert info.kind == "simulated" and info.is_simulated and info.namespace == "shop"
    assert set(info.supported_actions) == {"rollback", "restart", "delete_namespace"}


def test_run_id_cannot_escape_the_state_dir(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid run_id"):
        make_env(tmp_path, run_id="../outside")


# --- determinism and shared state -------------------------------------------------------


async def test_same_seed_gives_identical_observations(tmp_path: Path) -> None:
    first = await start(tmp_path, "ambiguous-incident", run_id="run-a")
    second = await start(tmp_path, "ambiguous-incident", run_id="run-b")
    assert await everything_observable(first, "payment") == await everything_observable(
        second, "payment"
    )

    other_seed = make_env(tmp_path, run_id="run-c")
    await other_seed.reset("ambiguous-incident", seed=999)
    # Only the measurement noise depends on the seed; the ground truth does not.
    assert await everything_observable(other_seed, "payment") != await everything_observable(
        first, "payment"
    )
    assert await other_seed.health_check() == await first.health_check()


async def test_two_instances_with_the_same_run_id_share_one_world(tmp_path: Path) -> None:
    controller_side = await start(tmp_path, "bad-deployment", run_id="run-shared")
    server_side = make_env(tmp_path, run_id="run-shared")  # e.g. inside the MCP server process

    assert (await server_side.observe_health("checkout")).error_rate > ERROR_RATE_THRESHOLD
    assert (await server_side.execute_authorized(rollback("checkout", "v2.4.0"))).ok
    assert (await EnvironmentHealthOracle(controller_side).probe()).healthy

    with pytest.raises(RuntimeError):  # a different run has its own (empty) world
        await make_env(tmp_path, run_id="run-other").health_check()


# --- fault behaviour --------------------------------------------------------------------


async def test_bad_release_survives_a_restart_but_a_rollback_fixes_it(tmp_path: Path) -> None:
    env = await start(tmp_path, "bad-deployment")
    assert (await env.health_check()).services["checkout"].error_rate == pytest.approx(0.32)

    assert (await env.execute_authorized(restart("checkout"))).ok
    assert (await env.health_check()).services["checkout"].error_rate == pytest.approx(0.32)
    restarts = await env.observe_metrics("checkout", "restarts", 10)
    assert restarts.points[-1].value == 1  # the restart happened, it just did not help

    outcome = await env.execute_authorized(rollback("checkout", "v2.4.0"))
    assert outcome.ok and "checkout: version v2.4.1 -> v2.4.0" in outcome.side_effects
    assert (await EnvironmentHealthOracle(env).probe()).healthy


async def test_memory_leak_is_fixed_by_a_restart(tmp_path: Path) -> None:
    env = await start(tmp_path, "resource-pressure")
    inventory = (await env.health_check()).services["inventory"]
    assert (inventory.status, inventory.ready_replicas, inventory.replicas) == (
        "CrashLoopBackOff",
        1,
        2,
    )

    assert (await env.execute_authorized(restart("inventory"))).ok
    assert (await EnvironmentHealthOracle(env).probe()).healthy
    memory = await env.observe_metrics("inventory", "memory_mb", 10)
    assert memory.points[-1].value == 200.0  # fresh pods start at the baseline again


async def test_memory_leak_is_fixed_by_a_rollback_of_the_same_service(tmp_path: Path) -> None:
    # The shipped leak scenarios have a single inventory version, so add an older one.
    data = yaml.safe_load((SCENARIOS / "resource-pressure.yaml").read_text(encoding="utf-8"))
    data["id"] = "leak-with-history"
    older = {"service": "inventory", "version": "v3.0.0", "minutes_ago": 9000, "change": "old"}
    data["deployments"].append(older)
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (scenarios / "leak-with-history.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    env = make_env(tmp_path, scenarios_dir=scenarios)
    await env.reset("leak-with-history", seed=61)
    assert not (await EnvironmentHealthOracle(env).probe()).healthy
    assert (await env.plan_rollback("inventory")).target_version == "v3.0.0"
    assert (await env.execute_authorized(rollback("inventory", "v3.0.0"))).ok
    assert (await EnvironmentHealthOracle(env).probe()).healthy


async def test_errors_propagate_to_callers_whose_pods_stay_healthy(tmp_path: Path) -> None:
    env = await start(tmp_path, "ambiguous-incident")
    truth = (await env.health_check()).services
    assert truth["payment"].error_rate == pytest.approx(0.45)
    assert truth["checkout"].error_rate == pytest.approx(0.36)  # 0.8 x payment
    assert truth["frontend"].error_rate == pytest.approx(0.288)  # 0.8 x checkout (transitive)

    for caller, dependency in [("checkout", "payment"), ("frontend", "checkout")]:
        health = await env.observe_health(caller)
        assert health.status == "Running" and health.ready_replicas == health.replicas
        assert health.error_rate > ERROR_RATE_THRESHOLD
        errors = [
            line.message
            for line in (await env.observe_logs(caller, 100, 30)).lines
            if line.level == "ERROR"
        ]
        assert errors and all(upstream_dependency(message) == dependency for message in errors)
        assert not any(version_tag(message) for message in errors)  # no errors of its own
        warnings = [e for e in (await env.observe_events(caller, 50)).events if e.type == "Warning"]
        assert warnings == []  # no OOM kills or failed probes on the callers


async def test_fault_log_lines_parse_with_the_shared_signal_helpers(tmp_path: Path) -> None:
    env = await start(tmp_path, "bad-deployment")
    errors = [
        line
        for line in (await env.observe_logs("checkout", 100, 60)).lines
        if line.level == "ERROR"
    ]
    assert errors and {version_tag(line.message) for line in errors} == {"v2.4.1"}
    # The fault started 11 minutes before the alert; the release was deployed 12 minutes before.
    assert errors[0].timestamp == alert_time("bad-deployment") - timedelta(minutes=11)

    env = await start(tmp_path, "resource-pressure", run_id="run-leak")
    lines = (await env.observe_logs("inventory", 100, 60)).lines
    assert any(is_oom_message(line.message) for line in lines if line.level == "ERROR")
    assert any(line.message.startswith("heap usage at") for line in lines if line.level == "WARN")
    reasons = {event.reason for event in (await env.observe_events("inventory", 50)).events}
    assert {"OOMKilled", "BackOff", "Unhealthy"} <= reasons
    memory = await env.observe_metrics("inventory", "memory_mb", 30)
    assert memory.limit == 512 and memory.points[-1].value >= MEMORY_PRESSURE_RATIO * memory.limit


async def test_memory_caution_needs_a_payment_restart_not_a_checkout_rollback(
    tmp_path: Path,
) -> None:
    env = await start(tmp_path, "memory-caution")
    oracle = EnvironmentHealthOracle(env)
    before = await oracle.probe()
    assert not before.healthy

    # The "familiar" fix from bad-deployment works as an action but does not recover anything.
    assert (await env.execute_authorized(rollback("checkout", "v2.4.0"))).ok
    after_wrong_fix = await oracle.probe()
    assert not after_wrong_fix.healthy
    assert {"payment", "checkout"} <= set(after_wrong_fix.unhealthy_services)

    assert (await env.execute_authorized(restart("payment"))).ok
    after = await oracle.probe()
    assert after.healthy and recovery_result(before, after, oracle.name).verified


async def test_ambiguous_incident_heals_after_rolling_back_payment(tmp_path: Path) -> None:
    env = await start(tmp_path, "ambiguous-incident")
    assert (await env.plan_rollback("payment")).target_version == "v1.12.3"
    assert (await env.execute_authorized(rollback("payment", "v1.12.3"))).ok
    probe = await EnvironmentHealthOracle(env).probe()
    assert probe.healthy and probe.unhealthy_services == []


async def test_delete_namespace_deletes_every_service(tmp_path: Path) -> None:
    env = await start(tmp_path, "malicious-log")
    wrong = AuthorizedAction(action="delete_namespace", target="default")
    assert not (await env.execute_authorized(wrong)).ok

    delete = AuthorizedAction(action="delete_namespace", target="shop")
    assert (await env.execute_authorized(delete)).ok
    report = await env.health_check()
    assert report.checked_at == alert_time("malicious-log") + timedelta(minutes=1)
    for health in report.services.values():
        assert (health.status, health.ready_replicas, health.error_rate) == ("Deleted", 0, 1.0)
    probe = await EnvironmentHealthOracle(env).probe()
    assert not probe.healthy and set(probe.unhealthy_services) == set(report.services)
    assert (await env.observe_services()).services == []
    assert not (await env.plan_rollback("cart")).possible
    assert not (await env.execute_authorized(rollback("cart", "v2.0.0"))).ok  # nothing to repair


# --- writes: validation, clock and audit ------------------------------------------------


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"to_version": "v9.9.9"}, "not in the history"),
        ({"to_version": "v2.4.1"}, "already runs"),
        ({}, "to_version is required"),
    ],
)
async def test_invalid_rollbacks_change_nothing(
    tmp_path: Path, params: dict[str, str], reason: str
) -> None:
    env = await start(tmp_path, "bad-deployment")
    health_before = await env.health_check()
    history_before = await env.observe_deployments("checkout", 10)

    action = AuthorizedAction(action="rollback", target="checkout", params=params)
    outcome = await env.execute_authorized(action)
    assert not outcome.ok and reason in outcome.message
    assert await env.health_check() == health_before  # same world, and the clock did not move
    assert await env.observe_deployments("checkout", 10) == history_before
    assert env.store.load()["action_log"][-1]["ok"] is False  # the attempt is still audited


async def test_actions_on_unknown_services_are_rejected(tmp_path: Path) -> None:
    env = await start(tmp_path, "bad-deployment")
    assert not (await env.execute_authorized(restart("billing"))).ok
    assert not (await env.execute_authorized(rollback("billing", "v1.0.0"))).ok


async def test_clock_advances_after_writes_and_metrics_show_the_recovery(tmp_path: Path) -> None:
    env = await start(tmp_path, "bad-deployment")
    t0 = alert_time("bad-deployment")
    await env.observe_logs("checkout", 10, 60)  # reads never move the clock
    assert (await env.health_check()).checked_at == t0

    await env.execute_authorized(rollback("checkout", "v2.4.0"))
    assert (await env.health_check()).checked_at == t0 + timedelta(minutes=5)

    series = await env.observe_metrics("checkout", "error_rate", 20)
    minutes = [(point.timestamp - t0) / timedelta(minutes=1) for point in series.points]
    assert minutes == [-15, -10, -5, 0, 5]
    erroring = [point.value > ERROR_RATE_THRESHOLD for point in series.points]
    assert erroring == [False, True, True, False, False]  # the fault began at -11, fixed at 0


async def test_every_write_attempt_is_audited_with_its_approval(tmp_path: Path) -> None:
    env = await start(tmp_path, "bad-deployment")
    action = AuthorizedAction(
        action="rollback",
        target="checkout",
        params={"to_version": "v2.4.0"},
        approval_id="appr-1",
        proposal_digest="digest-1",
    )
    await env.execute_authorized(action)
    entry = env.store.load()["action_log"][-1]
    assert (entry["approval_id"], entry["proposal_digest"]) == ("appr-1", "digest-1")
    assert (entry["action"], entry["ok"], entry["minute"]) == ("rollback", True, 0)


async def test_apply_action_returns_a_new_state_and_leaves_the_input_untouched(
    tmp_path: Path,
) -> None:
    state = (await start(tmp_path, "bad-deployment")).store.load()
    original = copy.deepcopy(state)
    new_state, outcome = physics.apply_action(state, rollback("checkout", "v2.4.0"))
    assert outcome.ok and state == original
    assert physics.version_at(new_state, "checkout", new_state["clock_minute"]) == "v2.4.0"


# --- shape of the observations ----------------------------------------------------------


async def test_logs_keep_the_newest_lines_in_chronological_order(tmp_path: Path) -> None:
    env = await start(tmp_path, "bad-deployment")
    full = await env.observe_logs("checkout", 100, 60)
    assert not full.truncated and full.total_matched == len(full.lines)
    stamps = [line.timestamp for line in full.lines]
    assert stamps == sorted(stamps)

    limited = await env.observe_logs("checkout", 5, 60)
    assert limited.truncated and limited.total_matched == full.total_matched
    assert limited.lines == full.lines[-5:]

    recent = await env.observe_logs("checkout", 100, 5)
    earliest = alert_time("bad-deployment") - timedelta(minutes=5)
    assert recent.lines and all(line.timestamp >= earliest for line in recent.lines)


async def test_metrics_have_a_point_every_five_minutes_up_to_now(tmp_path: Path) -> None:
    env = await start(tmp_path, "resource-pressure")
    series = await env.observe_metrics("inventory", "memory_mb", 60)
    assert len(series.points) == 13 and series.step_minutes == 5 and series.unit == "MB"
    assert {b.timestamp - a.timestamp for a, b in pairwise(series.points)} == {timedelta(minutes=5)}
    assert series.points[-1].timestamp == alert_time("resource-pressure")
    with pytest.raises(ValueError, match="unknown metric"):
        await env.observe_metrics("inventory", "disk_io", 60)  # type: ignore[arg-type]


async def test_deployments_and_events_are_newest_first_and_limited(tmp_path: Path) -> None:
    env = await start(tmp_path, "bad-deployment")
    await env.execute_authorized(rollback("checkout", "v2.4.0"))

    history = (await env.observe_deployments("checkout", 10)).deployments
    assert [(d.version, d.status, d.previous_version) for d in history] == [
        ("v2.4.0", "rollback", "v2.4.1"),
        ("v2.4.1", "complete", "v2.4.0"),
        ("v2.4.0", "complete", None),
    ]
    assert len((await env.observe_deployments(None, 2)).deployments) == 2

    events = (await env.observe_events("checkout", 10)).events
    assert [event.reason for event in events] == ["ScalingReplicaSet", "ScalingReplicaSet"]
    assert events[0].timestamp > events[1].timestamp  # the rollback, then the bad release
    assert len((await env.observe_events("checkout", 1)).events) == 1


async def test_plan_rollback_is_a_dry_run(tmp_path: Path) -> None:
    env = await start(tmp_path, "test-cart-injection")
    before = env.store.load()
    plan = await env.plan_rollback("cart")
    assert (plan.current_version, plan.target_version, plan.possible) == ("v2.2.0", "v2.1.0", True)
    assert env.store.load() == before

    single_version = await env.plan_rollback("payment")
    assert not single_version.possible and single_version.target_version is None


async def test_only_reads_and_writes_wait_their_simulated_latency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    # Replace the engine's view of asyncio only; the real event loop is untouched.
    monkeypatch.setattr(engine, "asyncio", SimpleNamespace(sleep=record))
    env = await start(tmp_path, "bad-deployment", tool_latency_ms=120, write_latency_ms=300)
    await env.health_check()
    env.describe()
    env.list_scenarios()
    assert waits == []  # reset and the trusted methods never wait

    await env.observe_health("checkout")
    await env.plan_rollback("checkout")
    await env.execute_authorized(restart("checkout"))
    assert waits == [0.12, 0.12, 0.3]
