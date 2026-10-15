"""Cost labels: one honest line about what a run cost.

The default provider is a scripted policy (not an LLM). Its token counts are local
estimates (about 4 characters per token) and nothing is billed, so its label says
"$0.00 external API charged". Only that provider gets this deterministic wording.

An LLM provider has two labels. When every call's usage carries provider-reported token
counts (`usage.is_estimate` is False and `usage.unreported_calls` is 0), the label shows a
dollar figure computed from the prices in configs/providers.yaml: an *estimate*, never an
invoice. Otherwise the label shows no dollar figure and says the cost was not fully reported.
The controller puts `cost_label(...)` in every run summary.
"""

from __future__ import annotations

from typing import Literal

from recap.core.models import Usage

DETERMINISTIC_COST_LABEL = (
    "$0.00 external API charged - token counts are estimates (deterministic mode)"
)
UNREPORTED_COST_LABEL = (
    "LLM provider: cost not fully reported (some calls had no usage data); "
    "tokens shown are estimates"
)


def cost_label(usage: Usage, provider_kind: Literal["scripted", "llm"]) -> str:
    """One honest line about cost for the dashboard, the CLI and eval reports."""
    if provider_kind == "scripted":
        return DETERMINISTIC_COST_LABEL
    if usage.is_estimate or usage.unreported_calls:
        # A dollar figure computed from guessed token counts would read like a real bill.
        return UNREPORTED_COST_LABEL
    return f"~${usage.external_cost_usd:.4f} estimated from provider-reported token counts"
