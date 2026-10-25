"""Optional Claude adapter: an LLMProvider backed by the official Anthropic Python SDK.

Where it sits: the harness asks a provider for decisions through the LLMProvider interface
(recap/core/interfaces.py). The default provider is a scripted policy, NOT an LLM. This
adapter is the opt-in alternative: it renders each request with the versioned prompts in
prompts.py, asks Claude for JSON in the closed output schemas defined there (structured
outputs), and maps the reply to the same validated decision types the scripted policy
returns. The harness cannot tell the two apart.

It runs only when you choose it (RECAP_PROVIDER=anthropic) and it uses YOUR API key and
billing. Tests never call the API: they inject a fake client.

Claude's reply is untrusted, so it is checked here before RECAP uses it:
  * tool names must be in the request's tool list (other calls are dropped with a note)
  * tool arguments arrive as a JSON string (args_json) and must parse to a JSON object
  * cited evidence IDs must exist in the request (made-up IDs are dropped)
  * a remediation must be one of the allowed remediation tools
Even after these checks, the safety gateway alone decides whether anything may run.

Usage carries the token counts the API reports. A reply that breaks the output schema is
rejected inside the SDK before those counts are returned, yet the call was billed: its usage
stays an estimate and the decision says "provider usage not reported for this call".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Generic, TypeVar

from pydantic import ValidationError

from recap.context.tokens import estimate_tokens
from recap.core.config import ProviderSettings
from recap.core.errors import ConfigError, RecapError
from recap.core.models import (
    Contract,
    Hypothesis,
    InvestigationDecision,
    InvestigationRequest,
    MitigationDraft,
    ProposalDecision,
    ProposalRequest,
    ProviderInfo,
    ToolCall,
    Usage,
)
from recap.providers.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    HypothesisOutput,
    InvestigationOutput,
    ProposalOutput,
    ToolCallOutput,
    citable_ids,
    render_investigation_prompt,
    render_proposal_prompt,
)

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_MAX_TOKENS = 16_000  # enough room to answer while staying a normal (non-streaming) call
DEFAULT_EFFORT = "high"
# Server-side refusal fallback: if a safety classifier declines a request, the API re-runs
# it on Anthropic's recommended fallback model. The "default" form needs this beta header.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DESCRIPTION = "Claude via the official Anthropic SDK (opt-in; uses your API key and billing)"
USAGE_NOT_REPORTED = "provider usage not reported for this call"

OutputT = TypeVar("OutputT", bound=Contract)


@dataclass(frozen=True)
class _Reply(Generic[OutputT]):
    """Result of one API call: the parsed output (None if unusable), notes and usage."""

    output: OutputT | None
    notes: list[str]
    usage: Usage


def load_sdk() -> ModuleType:
    """Import the optional Anthropic SDK, or explain how to install it."""
    try:
        import anthropic
    except ImportError as exc:
        raise ConfigError("install the llm extra: pip install -e '.[llm]'") from exc
    return anthropic


# --- the provider -----------------------------------------------------------------------


class AnthropicProvider:
    """LLMProvider that asks Claude for each decision (opt-in; uses your API key and billing)."""

    def __init__(self, settings: ProviderSettings, client: object | None = None) -> None:
        self.settings = settings
        self.model = settings.model or DEFAULT_MODEL
        self.max_tokens = settings.max_tokens or DEFAULT_MAX_TOKENS
        self.effort = settings.effort or DEFAULT_EFFORT
        # anthropic.AsyncAnthropic, or a test fake with the same beta.messages.parse method.
        self._client: Any = client
        self.info = ProviderInfo(
            name=f"anthropic:{self.model}",
            kind="llm",
            origin="paid_llm",  # billed per call: the owner keeps this adapter disabled
            model=self.model,
            description=DESCRIPTION,
            mode="json",
            prompt_version=PROMPT_VERSION,
        )

    async def next_action(self, request: InvestigationRequest) -> InvestigationDecision:
        """Ask Claude for the next read-only tool calls, or for final hypotheses."""
        reply = await self._ask(render_investigation_prompt(request), InvestigationOutput)
        notes = list(reply.notes)
        if reply.output is None:
            # No usable answer: the agent stops here and the verifier judges what it has.
            return InvestigationDecision(
                action="finish", summary=_with_notes("", notes), usage=reply.usage
            )
        return _investigation_decision(reply.output, request, reply.usage, notes)

    async def propose_mitigation(self, request: ProposalRequest) -> ProposalDecision:
        """Ask Claude for one remediation of the verified root cause."""
        reply = await self._ask(render_proposal_prompt(request), ProposalOutput)
        notes = list(reply.notes)
        draft = None if reply.output is None else _mitigation_draft(reply.output, request, notes)
        return ProposalDecision(draft=draft, notes=notes, usage=reply.usage)

    # --- one API call -------------------------------------------------------------------

    async def _ask(self, prompt: str, schema: type[OutputT]) -> _Reply[OutputT]:
        """Send one structured-output request; return the parsed reply or notes saying why not."""
        estimated = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(prompt)
        client = self._get_client()
        try:
            response = await client.beta.messages.parse(**self._request(prompt, schema))
        except ValidationError:
            # parse() validates the JSON itself and raises a bare pydantic error, so a reply
            # cut off mid-JSON fails here before stop_reason or usage can be read. The call
            # was still billed: keep only the local estimate and say the real usage is unknown.
            notes = [
                "Claude's reply did not match the required JSON schema (it may be cut off)",
                USAGE_NOT_REPORTED,
            ]
            unknown = Usage(
                steps=1, estimated_tokens=estimated, is_estimate=True, unreported_calls=1
            )
            return _Reply(None, notes, unknown)
        except Exception as exc:
            message = _sdk_error_message(exc)
            if message is None:
                raise
            raise RecapError(message) from exc  # the controller turns this into FAILED

        usage = self._usage(response, estimated)
        notes: list[str] = []
        if response.model != self.model:
            notes.append(f"answered by {response.model} (server-side refusal fallback)")
        # Check stop_reason BEFORE reading the output: a refusal or a cut-off reply is
        # not a decision, even if some JSON came back.
        if response.stop_reason == "refusal":
            return _Reply(None, [*notes, _refusal_note(response)], usage)
        if response.stop_reason == "max_tokens":
            return _Reply(None, [*notes, f"Claude hit max_tokens={self.max_tokens}"], usage)
        if response.parsed_output is None:
            note = f"Claude returned no JSON answer (stop_reason={response.stop_reason})"
            return _Reply(None, [*notes, note], usage)
        return _Reply(response.parsed_output, notes, usage)

    def _request(self, prompt: str, schema: type[Contract]) -> dict[str, Any]:
        """Keyword arguments for client.beta.messages.parse.

        There is deliberately no `thinking` and no `temperature`: on claude-opus-5-5 thinking
        is always on (disabling it returns HTTP 400) and sampling parameters are rejected.
        `effort` is the knob that controls how deeply Claude thinks.
        """
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
            "output_format": schema,  # the SDK sends its JSON schema and validates the reply
            "output_config": {"effort": self.effort},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }

    def _get_client(self) -> Any:
        """The SDK client, created on first use (so building the provider needs no key)."""
        if self._client is None:
            self._client = load_sdk().AsyncAnthropic()  # reads ANTHROPIC_API_KEY itself
        return self._client

    def _usage(self, response: Any, estimated: int) -> Usage:
        """Real token counts from the API, plus a cost estimate from the configured prices."""
        tokens_in = response.usage.input_tokens
        tokens_out = response.usage.output_tokens
        return Usage(
            steps=1,
            estimated_tokens=estimated,
            actual_input_tokens=tokens_in,
            actual_output_tokens=tokens_out,
            external_cost_usd=_cost_usd(tokens_in, tokens_out, self.settings.price_per_mtok),
            is_estimate=False,
        )


# --- mapping Claude's reply to RECAP decisions ------------------------------------------


def _investigation_decision(
    output: InvestigationOutput, request: InvestigationRequest, usage: Usage, notes: list[str]
) -> InvestigationDecision:
    """Turn Claude's reply into a validated decision; every correction becomes a note."""
    calls = _valid_tool_calls(output.tool_calls, {tool.name for tool in request.tools}, notes)
    # IDs Claude can legitimately know: the packed evidence plus its own earlier results.
    known = set(request.evidence) | {r.evidence_id for r in request.history if r.evidence_id}
    hypotheses = [_hypothesis(raw, known, notes) for raw in output.hypotheses]
    action = output.action
    if action == "call_tools" and not calls:
        # Another step with nothing to run would only spend a paid API call.
        action = "finish"
        notes.append("no valid tool call was left, so the agent finishes")
    return InvestigationDecision(
        action=action,
        tool_calls=calls if action == "call_tools" else [],
        hypotheses=hypotheses,
        summary=_with_notes(output.summary, notes),
        next_steps=output.next_steps,
        usage=usage,
    )


def _valid_tool_calls(
    raw_calls: list[ToolCallOutput], allowed: set[str], notes: list[str]
) -> list[ToolCall]:
    """Keep calls to listed tools whose args_json is a JSON object; note each dropped call."""
    calls: list[ToolCall] = []
    for raw in raw_calls:
        if raw.tool_name not in allowed:
            notes.append(f"dropped a call to unlisted tool {raw.tool_name[:60]!r}")
            continue
        args = _json_object(raw.args_json)
        if args is None:
            notes.append(f"dropped a {raw.tool_name} call: args_json is not a JSON object")
            continue
        calls.append(ToolCall(tool_name=raw.tool_name, args=args, reason=raw.reason))
    return calls


def _hypothesis(raw: HypothesisOutput, known_ids: set[str], notes: list[str]) -> Hypothesis:
    """A Hypothesis that cites only evidence IDs which really exist."""
    details = _json_object(raw.details_json)
    if details is None:
        notes.append("ignored hypothesis details_json that is not a JSON object")
    return Hypothesis(
        claim=raw.claim,
        mechanism=raw.mechanism,
        target_service=raw.target_service or None,
        supporting_evidence_ids=_known_ids(raw.supporting_evidence_ids, known_ids, notes),
        details=details or {},
    )


def _mitigation_draft(
    raw: ProposalOutput, request: ProposalRequest, notes: list[str]
) -> MitigationDraft | None:
    """A draft for one allowed remediation tool, or None (with a note) if unusable."""
    if not raw.propose:
        notes.append(f"Claude proposed no action: {raw.rationale}")
        return None
    tool = next((t for t in request.allowed_actions if t.name == raw.action), None)
    if tool is None:
        notes.append(f"rejected an action outside the remediation catalog: {raw.action[:60]!r}")
        return None
    if any(a.action == raw.action and a.target == raw.target for a in request.failed_actions):
        notes.append(f"rejected {raw.action} on {raw.target}: it already failed in this run")
        return None
    params = _json_object(raw.params_json)
    if params is None:
        notes.append("rejected the draft: params_json is not a JSON object")
        return None
    # The same rule as the step protocol: a tampered episode ID is never accepted.
    citable = set(citable_ids(request))
    return MitigationDraft(
        action=tool.name,
        target=raw.target,
        params=params,
        rationale=raw.rationale,
        evidence_ids=_known_ids(raw.evidence_ids, set(request.evidence), notes),
        expected_health_change=raw.expected_health_change,
        rollback_hint=raw.rollback_hint,
        risk_level=tool.risk,  # the trusted tier from RECAP's registry, not Claude's opinion
        hypothesis_claim=request.hypothesis.claim,
        source="llm",
        citations=_known_ids(raw.citations, citable, notes),
    )


# --- small helpers ----------------------------------------------------------------------


def _known_ids(cited: list[str], known: set[str], notes: list[str]) -> list[str]:
    """Cited IDs that exist, once each and in order; made-up IDs are dropped with a note."""
    unknown = [item_id[:60] for item_id in cited if item_id not in known]
    if unknown:
        notes.append(f"dropped unknown IDs {unknown[:5]}")
    return list(dict.fromkeys(item_id for item_id in cited if item_id in known))


def _json_object(text: str) -> dict[str, Any] | None:
    """Parse a JSON object string from Claude; None if it is not valid JSON or not an object."""
    try:
        value = json.loads(text or "{}")
    except (json.JSONDecodeError, RecursionError):  # deeply nested input hits the stack limit
        return None
    return value if isinstance(value, dict) else None


def _with_notes(summary: str, notes: list[str]) -> str:
    """Append the adapter's notes so every correction shows up in the trace."""
    if not notes:
        return summary
    joined = "; ".join(notes)
    return f"{summary} [adapter notes: {joined}]" if summary else joined


def _refusal_note(response: Any) -> str:
    """Say that Claude declined, with the policy category when the API gives one."""
    details = response.stop_details
    category = details.category if details is not None and details.category else "unspecified"
    return f"Claude declined the request (refusal, category: {category}); nothing was decided"


def _cost_usd(tokens_in: int, tokens_out: int, prices: dict[str, float] | None) -> float:
    """USD cost from a per-million-token price table; 0.0 when no prices are configured."""
    if not prices:
        return 0.0
    usd = tokens_in * prices.get("input", 0.0) + tokens_out * prices.get("output", 0.0)
    return round(usd / 1_000_000, 6)


def _sdk_error_message(exc: Exception) -> str | None:
    """A clear message for an Anthropic SDK error, or None if `exc` is not one.

    Most specific first (RateLimitError is a kind of APIStatusError). The SDK already
    retried 429, 5xx and connection errors with backoff, so RECAP does not retry again.
    """
    try:
        import anthropic
    except ImportError:
        return None  # without the SDK installed, `exc` cannot be an SDK error
    if isinstance(exc, anthropic.RateLimitError):
        return "Claude API rate limit reached (HTTP 429), even after the SDK's retries"
    if isinstance(exc, anthropic.APIStatusError):
        return f"Claude API returned HTTP {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "could not reach the Claude API (network error or timeout)"
    return None
