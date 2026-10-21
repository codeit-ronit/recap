"""Cost labels: one honest line about what a run cost.

The label depends on where the run's decisions came from (ProviderInfo.origin):

    scripted         "$0.00 external API charged" - the scripted policy calls no model, and
                     its token counts are local estimates (about 4 characters per token)
    local_llm        "$0.00" - the model ran on this machine; the token counts are the
                     model server's own (exact)
    free_hosted_llm  "$0.00" - only because RECAP refuses every request unless the owner
                     confirmed the free plan, and checks each response's rate-limit header
                     against the free plan (recap/providers/quota.py). If that check failed,
                     the cost is unknown and the label says so.
    replay           "$0.00" - recorded answers were replayed; no model was called
    paid_llm         a dollar ESTIMATE from the prices in configs/providers.yaml when every
                     call reported its tokens, otherwise "not fully reported" (never a guess)

The controller puts `cost_label(...)` in every run summary.
"""

from __future__ import annotations

from recap.core.models import Usage

DETERMINISTIC_COST_LABEL = (
    "$0.00 external API charged - token counts are estimates (deterministic mode)"
)
REPLAY_COST_LABEL = "$0.00 - replayed recording: no model was called in this run"
UNREPORTED_COST_LABEL = (
    "LLM provider: cost not fully reported (some calls had no usage data); "
    "tokens shown are estimates"
)


def cost_label(usage: Usage, origin: str, *, free_status_lost: str | None = None) -> str:
    """One honest line about cost for the dashboard, the CLI and eval reports.

    `origin` is the provider's ProviderInfo.origin. `free_status_lost` is the reason a free
    provider's tripwire fired during the run (None when every response looked free).
    """
    if origin == "scripted":
        return DETERMINISTIC_COST_LABEL
    if origin == "replay":
        return REPLAY_COST_LABEL
    if origin == "local_llm":
        return f"$0.00 - local model on this machine, no API; {_tokens(usage)}"
    if origin == "free_hosted_llm":
        if free_status_lost:
            return (
                f"cost unknown: {free_status_lost}. RECAP stopped the provider; check the "
                "account's billing page"
            )
        return (
            "$0.00 - free plan (owner-confirmed; every response's limit header matched the "
            f"free plan); {_tokens(usage)}"
        )
    if usage.is_estimate or usage.unreported_calls:
        # A dollar figure computed from guessed token counts would read like a real bill.
        return UNREPORTED_COST_LABEL
    return f"~${usage.external_cost_usd:.4f} estimated from provider-reported token counts"


def _tokens(usage: Usage) -> str:
    """'1,234 in / 567 out tokens (reported by the server)', or why there are none."""
    if usage.actual_input_tokens is None and usage.actual_output_tokens is None:
        return "no token counts were reported"
    tokens_in = usage.actual_input_tokens or 0
    tokens_out = usage.actual_output_tokens or 0
    missing = f", {usage.unreported_calls} request(s) unreported" if usage.unreported_calls else ""
    return f"{tokens_in:,} in / {tokens_out:,} out tokens reported by the server{missing}"
