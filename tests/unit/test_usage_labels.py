"""Tests for honest cost labels: the deterministic wording belongs to the scripted policy only."""

from __future__ import annotations

import pytest

from recap.core.models import Usage
from recap.telemetry.usage import DETERMINISTIC_COST_LABEL, cost_label


def test_scripted_provider_is_zero_cost_with_estimated_tokens() -> None:
    usage = Usage(steps=12, tool_calls=9, estimated_tokens=8400)

    assert cost_label(usage, "scripted") == (
        "$0.00 external API charged - token counts are estimates (deterministic mode)"
    )
    assert cost_label(usage, "scripted") == DETERMINISTIC_COST_LABEL


def test_llm_cost_is_an_estimate_from_provider_reported_tokens() -> None:
    usage = Usage(
        steps=1,
        actual_input_tokens=1000,
        actual_output_tokens=100,
        external_cost_usd=0.01234,
        is_estimate=False,
    )

    assert cost_label(usage, "llm") == "~$0.0123 estimated from provider-reported token counts"


@pytest.mark.parametrize("cost", [0.0, 0.5])
def test_llm_run_with_estimated_usage_says_the_cost_was_not_reported(cost: float) -> None:
    # Regression: an LLM run whose calls reported no usage data was labelled
    # "$0.00 external API charged ... (deterministic mode)", as if no LLM had been called.
    usage = Usage(steps=3, estimated_tokens=40_000, external_cost_usd=cost, is_estimate=True)

    assert cost_label(usage, "llm") == (
        "LLM provider: cost not fully reported (some calls had no usage data); "
        "tokens shown are estimates"
    )


def test_one_unreported_llm_call_makes_the_whole_run_not_fully_reported() -> None:
    reported = Usage(
        actual_input_tokens=10, actual_output_tokens=5, external_cost_usd=0.01, is_estimate=False
    )
    unreported = Usage(estimated_tokens=40, is_estimate=True, unreported_calls=1)
    total = reported.plus(unreported)
    assert total.unreported_calls == 1
    assert "not fully reported" in cost_label(total, "llm")
