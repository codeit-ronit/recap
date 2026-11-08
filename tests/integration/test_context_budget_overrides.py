"""A run's own context budget (M6 W4): RunConfig.pack_tokens and max_item_tokens replace the
configured values for that run only, the effective values are recorded with its versions,
and what a pack left out or shortened is traced."""

from __future__ import annotations

from evals.categorize import record_from_run
from recap.core.config import Settings
from recap.core.models import RunConfig
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C


async def test_a_run_without_overrides_records_the_configured_context_budget(
    settings: Settings,
) -> None:
    runtime = Runtime(settings)
    record = await runtime.run_incident("bad-deployment", RunConfig(approval_mode="auto"))
    configured = settings.budget.context
    assert record.versions["context"] == {
        "pack_tokens": configured.pack_tokens,
        "max_item_tokens": configured.max_item_tokens,
    }
    packs = [
        e.payload for e in runtime.events.list_events(record.run_id) if e.category == C.CONTEXT
    ]
    assert packs and all("compacted" in p and "dropped" in p for p in packs)


async def test_a_smaller_context_budget_is_used_recorded_and_its_cuts_traced(
    settings: Settings,
) -> None:
    runtime = Runtime(settings)
    config = RunConfig(approval_mode="auto", pack_tokens=200, max_item_tokens=32)
    record = await runtime.run_incident("bad-deployment", config)
    assert record.versions["context"] == {"pack_tokens": 200, "max_item_tokens": 32}
    events = runtime.events.list_events(record.run_id)
    packs = [e.payload for e in events if e.category == C.CONTEXT]
    assert any(p["dropped"] for p in packs)  # 200 estimated tokens cannot hold everything
    assert any(p["compacted"] for p in packs)  # items over 32 tokens were shortened
    evaluated = record_from_run(record, events, mode="scripted_regression")
    assert evaluated.context_limits == {"pack_tokens": 200, "max_item_tokens": 32}
    assert evaluated.context_dropped and evaluated.context_compacted
    assert evaluated.context_packs == len(packs)
    # The next run, without overrides, uses the configured values again (nothing was shared).
    again = await runtime.run_incident("bad-deployment", RunConfig(approval_mode="auto"))
    assert again.versions["context"]["pack_tokens"] == settings.budget.context.pack_tokens


async def test_a_run_traced_before_packs_recorded_their_cuts_reads_not_recorded(
    settings: Settings,
) -> None:
    """Older traces' context packs name what they dropped but not what they shortened: the
    record says "not recorded" (None), never a measured zero."""
    runtime = Runtime(settings)
    record = await runtime.run_incident("bad-deployment", RunConfig(approval_mode="auto"))
    events = runtime.events.list_events(record.run_id)
    older = [
        e.model_copy(update={"payload": {k: v for k, v in e.payload.items() if k != "compacted"}})
        if e.category == C.CONTEXT
        else e
        for e in events
    ]
    assert record_from_run(record, events, mode="scripted_regression").context_compacted == 0
    evaluated = record_from_run(record, older, mode="scripted_regression")
    assert evaluated.context_compacted is None and evaluated.context_packs
