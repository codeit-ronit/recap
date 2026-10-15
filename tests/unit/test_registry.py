"""Tests for the trusted tool registry: strict argument rules and mitigation argument mapping."""

from __future__ import annotations

from typing import Any

import pytest

from recap.core.config import ArgRule, Settings, ToolPolicy
from recap.core.models import DiscoveredTool, MitigationProposal, ToolRisk
from recap.tools.registry import MAX_STRING_CHARS, ToolRegistry


@pytest.fixture
def registry(settings: Settings) -> ToolRegistry:
    return ToolRegistry(settings.policies)


def make_proposal(action: str, target: str, params: dict[str, Any] | None = None):
    return MitigationProposal(
        proposal_id="prop-1",
        incident_id="inc-1",
        action=action,
        target=target,
        params=params or {},
        rationale="test",
    )


def logs_args(**extra: Any) -> dict[str, Any]:
    return {"service": "checkout", **extra}


# --- catalog -----------------------------------------------------------------------------


def test_names_and_risk_tiers_come_from_trusted_config(registry: ToolRegistry) -> None:
    assert registry.is_known("get_service_logs")
    assert not registry.is_known("read_only_cleanup")
    assert registry.risk_of("get_service_logs") == ToolRisk.READ
    assert registry.risk_of("execute_rollback") == ToolRisk.REVERSIBLE_WRITE
    assert registry.risk_of("delete_namespace") == ToolRisk.DESTRUCTIVE
    assert registry.risk_of("read_only_cleanup") is None
    assert registry.get("read_only_cleanup") is None


def test_views_describe_tools_from_config_not_from_the_server(
    registry: ToolRegistry, settings: Settings
) -> None:
    view = registry.view("get_service_logs")
    assert view.description == settings.policies.tools["get_service_logs"].description
    assert view.risk == ToolRisk.READ
    assert view.args["limit"] == {"type": "int", "required": False, "min": 1, "max": 100}
    # Providers are told the allowlist up front.
    assert view.args["service"]["values"] == sorted(settings.policies.allowed_services)
    assert [v.name for v in registry.remediation_views()] == ["execute_rollback", "restart_service"]
    with pytest.raises(KeyError):
        registry.view("read_only_cleanup")


# --- argument validation -----------------------------------------------------------------


def test_valid_args_pass_and_none_means_not_given(registry: ToolRegistry) -> None:
    clean, errors = registry.validate_args(
        "get_service_logs", logs_args(limit=20, since_minutes=None)
    )
    assert errors == []
    assert clean == {"service": "checkout", "limit": 20}


def test_unknown_tool_and_unknown_argument_are_rejected(registry: ToolRegistry) -> None:
    assert registry.validate_args("read_only_cleanup", {}) == (
        {},
        ["unknown tool 'read_only_cleanup'"],
    )
    # approval_id is never a valid tool argument: the gateway adds it itself on writes.
    clean, errors = registry.validate_args("get_service_health", logs_args(approval_id="apr-1"))
    assert errors == ["unknown argument 'approval_id'"]
    assert clean == {"service": "checkout"}


def test_missing_required_argument(registry: ToolRegistry) -> None:
    assert registry.validate_args("get_service_health", {}) == (
        {},
        ["missing required argument 'service'"],
    )
    assert registry.validate_args("get_service_health", {"service": None})[1] == [
        "missing required argument 'service'"
    ]


@pytest.mark.parametrize("limit", [True, False, "5", 5.0, 0, 101, -1])
def test_int_args_must_be_real_ints_within_bounds(registry: ToolRegistry, limit: Any) -> None:
    clean, errors = registry.validate_args("get_service_logs", logs_args(limit=limit))
    assert len(errors) == 1 and errors[0].startswith("limit")
    assert "limit" not in clean


@pytest.mark.parametrize("limit", [1, 100])
def test_int_bounds_are_inclusive(registry: ToolRegistry, limit: int) -> None:
    assert registry.validate_args("get_service_logs", logs_args(limit=limit))[1] == []


def test_enum_args(registry: ToolRegistry) -> None:
    ok = {"service": "checkout", "metric": "memory_mb"}
    assert registry.validate_args("get_service_metrics", ok)[1] == []
    for bad in ["disk_io", "MEMORY_MB", 3]:
        errors = registry.validate_args("get_service_metrics", {**ok, "metric": bad})[1]
        assert len(errors) == 1 and errors[0].startswith("metric")


@pytest.mark.parametrize("version", ["v2.4.0", "v10.0.12"])
def test_version_format_accepts_semver_tags(registry: ToolRegistry, version: str) -> None:
    args = {"service": "checkout", "to_version": version}
    assert registry.validate_args("execute_rollback", args)[1] == []


@pytest.mark.parametrize(
    "version",
    ["2.4.0", "v2.4", "v2.4.0-rc1", "V2.4.0", "v2.4.0\n", " v2.4.0", "v١.٢.٣", 240, None],
)
def test_version_format_rejects_everything_else(registry: ToolRegistry, version: Any) -> None:
    args = {"service": "checkout", "to_version": version}
    assert len(registry.validate_args("execute_rollback", args)[1]) == 1


@pytest.mark.parametrize("service", ["billing", "Checkout", "checkout ", "", 42, ["checkout"]])
def test_service_allowlist(registry: ToolRegistry, service: Any) -> None:
    errors = registry.validate_args("get_service_health", {"service": service})[1]
    assert len(errors) == 1 and "allowlisted service" in errors[0]


def test_namespace_allowlist(registry: ToolRegistry) -> None:
    assert registry.validate_args("delete_namespace", {"namespace": "shop"})[1] == []
    errors = registry.validate_args("delete_namespace", {"namespace": "kube-system"})[1]
    assert errors == ["namespace: 'kube-system' is not an allowlisted namespace"]


def test_string_args_are_length_capped(settings: Settings) -> None:
    tools = {"annotate": ToolPolicy(risk=ToolRisk.READ, args={"note": ArgRule(type="string")})}
    registry = ToolRegistry(settings.policies.model_copy(update={"tools": tools}))
    assert registry.validate_args("annotate", {"note": "x" * MAX_STRING_CHARS})[1] == []
    assert len(registry.validate_args("annotate", {"note": "x" * (MAX_STRING_CHARS + 1)})[1]) == 1
    assert len(registry.validate_args("annotate", {"note": 7})[1]) == 1


def test_error_messages_do_not_echo_huge_inputs(registry: ToolRegistry) -> None:
    errors = registry.validate_args("get_service_health", {"service": "x" * 10_000})[1]
    assert len(errors[0]) < 120


# --- mitigation arguments ----------------------------------------------------------------


def test_rollback_target_fills_the_service_argument(registry: ToolRegistry) -> None:
    proposal = make_proposal("execute_rollback", "checkout", {"to_version": "v2.4.0"})
    assert registry.mitigation_args(proposal) == {"service": "checkout", "to_version": "v2.4.0"}


def test_restart_target_always_wins_over_params(registry: ToolRegistry) -> None:
    proposal = make_proposal("restart_service", "checkout", {"service": "payment"})
    assert registry.mitigation_args(proposal) == {"service": "checkout"}


def test_delete_namespace_target_fills_the_namespace_argument(registry: ToolRegistry) -> None:
    proposal = make_proposal("delete_namespace", "shop", {"namespace": "kube-system"})
    assert registry.mitigation_args(proposal) == {"namespace": "shop"}


def test_unexpected_params_are_kept_so_validation_rejects_them(registry: ToolRegistry) -> None:
    args = registry.mitigation_args(make_proposal("restart_service", "checkout", {"force": True}))
    assert args == {"service": "checkout", "force": True}
    assert registry.validate_args("restart_service", args)[1] == ["unknown argument 'force'"]


def test_unknown_action_keeps_params_unchanged(registry: ToolRegistry) -> None:
    proposal = make_proposal("scale_to_zero", "checkout", {"replicas": 0})
    assert registry.mitigation_args(proposal) == {"replicas": 0}


def test_target_arg_is_the_first_service_or_namespace_argument(registry: ToolRegistry) -> None:
    assert registry.target_arg("execute_rollback") == "service"
    assert registry.target_arg("delete_namespace") == "namespace"
    assert registry.target_arg("list_services") is None  # nothing for a target to fill
    assert registry.target_arg("scale_to_zero") is None  # unknown tool


# --- comparing a server's advertised tools -----------------------------------------------


def test_compare_reports_unknown_missing_and_lying_tools(registry: ToolRegistry) -> None:
    tools = [
        DiscoveredTool(name="get_service_logs", annotations={"readOnlyHint": True}),
        DiscoveredTool(
            name="delete_namespace",
            annotations={"read_only_hint": True, "destructive_hint": False},
        ),
        DiscoveredTool(name="read_only_cleanup", annotations={"readOnlyHint": True}),
    ]
    report = registry.compare("test-transport", tools)
    assert report.transport == "test-transport"
    assert report.advertised == ["get_service_logs", "delete_namespace", "read_only_cleanup"]
    assert report.usable == ["get_service_logs", "delete_namespace"]
    assert report.unknown_tools == ["read_only_cleanup"]
    assert "execute_rollback" in report.missing_tools
    assert "get_service_logs" not in report.missing_tools
    assert len(report.annotation_mismatches) == 2
    assert all(m.startswith("delete_namespace:") for m in report.annotation_mismatches)


def test_compare_accepts_honest_or_ambiguous_hints(registry: ToolRegistry) -> None:
    tools = [
        DiscoveredTool(name="get_service_logs", annotations={"readOnlyHint": True}),
        # "destructive" is ambiguous for reversible writes under MCP, so it is not flagged.
        DiscoveredTool(name="restart_service", annotations={"destructiveHint": True}),
        DiscoveredTool(name="execute_rollback", annotations={"readOnlyHint": None}),
    ]
    assert registry.compare("t", tools).annotation_mismatches == []


def test_compare_flags_a_read_tool_the_server_calls_destructive(registry: ToolRegistry) -> None:
    tools = [DiscoveredTool(name="get_service_logs", annotations={"destructiveHint": True})]
    assert registry.compare("t", tools).annotation_mismatches == [
        "get_service_logs: server says destructiveHint=True, trusted: read"
    ]
