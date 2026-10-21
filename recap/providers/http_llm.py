"""The decision loop shared by every provider that talks to a language model over HTTP.

Where it sits:

    AgentHarness / IncidentController
        | next_action(request) / propose_mitigation(request)        (the LLMProvider interface)
        v
    HttpLLMProvider  render (protocol.py) -> _send (subclass) -> parse (protocol.py)
        |-- OllamaProvider      local model, /api/chat              (ollama_provider.py)
        '-- FreeHostedProvider  Groq Free plan, /chat/completions   (openai_compatible.py)

One decision is at most `max_attempts` model requests:

    1. render the messages and the request's own JSON schema (or tool definitions)
    2. _send: the subclass turns that into its server's HTTP request and returns a
       provider-neutral ModelReply (content, tool calls, exact token counts, ...)
    3. parse the reply with the step protocol
    4. usable?  -> return the decision
       not?     -> ask again once, with the problem as feedback (a corrective retry)
    5. still unusable -> an empty "finish" (or no proposal). Never a scripted fallback.

Every request becomes a ModelCall in the decision (the harness turns those into `llm.call`
trace events), and its exact token counts go into the decision's Usage.

HTTP-level trouble (server down, refused, quota) is the subclass's job: it raises
ProviderError (the run fails with that message) or QuotaExhausted (the run is deferred).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from recap.context.tokens import estimate_tokens
from recap.core.config import ProviderSettings
from recap.core.ids import canonical_json, sha256_hex
from recap.core.models import (
    InvestigationDecision,
    InvestigationRequest,
    ModelCall,
    ProposalDecision,
    ProposalRequest,
    ProviderInfo,
    ProviderOrigin,
    Usage,
)
from recap.providers import protocol
from recap.providers.protocol import Message, Parsed
from recap.providers.transport import ModelTransport

EXCERPT_CHARS = 1500  # how much of a model's answer (or reasoning) a trace event keeps

Purpose = Literal["investigate", "propose"]


@dataclass(frozen=True)
class ModelReply:
    """One model answer in a provider-neutral shape (what a subclass's _send returns)."""

    status: int
    content: str = ""  # the text answer (JSON in json mode)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # [{"name", "arguments"}]
    thinking: str = ""  # the model's reasoning, when the server returns it separately
    finish_reason: str | None = None  # "stop", "length" (cut off), "tool_calls", ...
    input_tokens: int | None = None  # as reported by the server
    output_tokens: int | None = None
    model_reported: str | None = None
    duration_ms: float = 0.0
    rate_limit: dict[str, Any] = field(default_factory=dict)
    replayed: bool = False
    notes: list[str] = field(default_factory=list)  # e.g. "the answer was cut off"


@dataclass(frozen=True)
class Outcome:
    """A finished decision attempt: the decision (or None), its usage and its model calls."""

    decision: InvestigationDecision | ProposalDecision | None
    usage: Usage
    calls: list[ModelCall]
    problem: str  # why the last answer was unusable ("" if usable)


class HttpLLMProvider(ABC):
    """LLMProvider for a real model behind HTTP. Subclasses implement `_send` only."""

    api: str  # "ollama" or "openai_compatible"

    def __init__(
        self,
        name: str,
        settings: ProviderSettings,
        transport: ModelTransport,
        *,
        replay_of: dict[str, Any] | None = None,
        recording: str | None = None,
    ) -> None:
        if not settings.model:
            raise ValueError(f"provider {name!r} has no model configured")
        self.name = name
        self.settings = settings
        self.model = settings.model
        self.mode = settings.mode
        self.transport = transport
        replaying = transport.name == "replay"
        origin: ProviderOrigin = "replay" if replaying else settings.origin
        self.info = ProviderInfo(
            name=f"{'replay:' if replaying else ''}{name}:{self.model}",
            kind="replay" if replaying else "llm",
            origin=origin,
            model=self.model,
            description=settings.description,
            mode=self.mode,
            prompt_version=protocol.PROMPT_VERSION,
            replay_of=replay_of,
            recording=recording,
        )

    # --- the LLMProvider interface -------------------------------------------------------

    async def next_action(self, request: InvestigationRequest) -> InvestigationDecision:
        """Ask the model for the next read-only tool calls, or for its findings."""
        messages = protocol.investigation_messages(request, self.mode)
        if self.mode == "json":
            schema, tools = protocol.step_schema(request), None
        else:
            schema, tools = None, protocol.tool_definitions(request)

        def parse(reply: ModelReply) -> Parsed:
            if self.mode == "json":
                return protocol.parse_json_step(reply.content, request)
            return protocol.parse_tool_step(reply.tool_calls, reply.content, request)

        outcome = await self._decide("investigate", messages, schema, tools, parse)
        decision = outcome.decision
        if not isinstance(decision, InvestigationDecision):
            # No usable answer: the agent stops here, and the verifier judges what it has.
            summary = f"no usable answer from {self.info.name}: {outcome.problem}"
            decision = InvestigationDecision(action="finish", summary=summary)
        return decision.model_copy(update={"usage": outcome.usage, "model_calls": outcome.calls})

    async def propose_mitigation(self, request: ProposalRequest) -> ProposalDecision:
        """Ask the model for one remediation of the verified root cause (always JSON mode)."""
        messages = protocol.proposal_messages(request)
        schema = protocol.proposal_schema(request)
        outcome = await self._decide(
            "propose",
            messages,
            schema,
            None,
            lambda reply: protocol.parse_proposal(reply.content, request),
        )
        decision = outcome.decision
        if not isinstance(decision, ProposalDecision):
            note = f"no usable proposal from {self.info.name}: {outcome.problem}"
            decision = ProposalDecision(draft=None, notes=[note])
        return decision.model_copy(update={"usage": outcome.usage, "model_calls": outcome.calls})

    # --- one decision ------------------------------------------------------------------

    async def _decide(
        self,
        purpose: Purpose,
        messages: list[Message],
        schema: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        parse: Callable[[ModelReply], Parsed],
    ) -> Outcome:
        """Send, parse and (when the answer is unusable) ask again, at most max_attempts times."""
        estimated = estimate_tokens(canonical_json(messages)) + estimate_tokens(
            canonical_json(schema or tools or {})
        )
        usage = Usage(steps=1, estimated_tokens=estimated)
        calls: list[ModelCall] = []
        problem = ""
        for attempt in range(1, self.settings.max_attempts + 1):
            sent = messages if not problem else [*messages, protocol.correction(problem)]
            reply = await self._send(sent, schema=schema, tools=tools, purpose=purpose)
            parsed = parse(reply)
            calls.append(self._model_call(purpose, attempt, sent, schema, reply, parsed))
            usage = usage.plus(_reply_usage(reply))
            if parsed.decision is not None:
                decision = _with_notes(parsed.decision, reply.notes, parsed.notes)
                return Outcome(decision, usage, calls, "")
            problem = parsed.problem
        return Outcome(None, usage, calls, problem)

    @abstractmethod
    async def _send(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        purpose: Purpose,
    ) -> ModelReply:
        """Send one request to the model server. Raise ProviderError / QuotaExhausted."""

    # --- trace records -------------------------------------------------------------------

    def _model_call(
        self,
        purpose: Purpose,
        attempt: int,
        sent: list[Message],
        schema: dict[str, Any] | None,
        reply: ModelReply,
        parsed: Parsed,
    ) -> ModelCall:
        answer = reply.content
        if reply.tool_calls:  # tools mode: show the calls the model made
            answer = (answer + "\n" if answer else "") + canonical_json(reply.tool_calls)
        notes = [*reply.notes, *parsed.notes]
        if parsed.problem:
            notes.append(f"unusable: {parsed.problem}")
        return ModelCall(
            provider=self.name,
            model=self.model,
            model_reported=reply.model_reported,
            origin=self.info.origin,
            purpose=purpose,
            mode="json" if schema is not None else "tools",
            attempt=attempt,
            status=reply.status,
            ok=parsed.decision is not None,
            response_kind=parsed.kind,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            duration_ms=reply.duration_ms,
            finish_reason=reply.finish_reason,
            prompt_sha256=sha256_hex(canonical_json(sent)),
            prompt_chars=sum(len(str(message.get("content", ""))) for message in sent),
            output_excerpt=answer[:EXCERPT_CHARS],
            thinking_excerpt=reply.thinking[:EXCERPT_CHARS],
            rate_limit=reply.rate_limit,
            replayed=reply.replayed,
            notes=notes,
        )


def _reply_usage(reply: ModelReply) -> Usage:
    """What one request cost in tokens. A replayed answer consumed nothing now."""
    if reply.replayed:
        return Usage()
    reported = reply.input_tokens is not None and reply.output_tokens is not None
    return Usage(
        model_requests=1,
        actual_input_tokens=reply.input_tokens,
        actual_output_tokens=reply.output_tokens,
        is_estimate=not reported,
        unreported_calls=0 if reported else 1,
    )


def _with_notes(
    decision: InvestigationDecision | ProposalDecision,
    reply_notes: list[str],
    parse_notes: list[str],
) -> InvestigationDecision | ProposalDecision:
    """Make RECAP's corrections visible next to the decision itself.

    A proposal already carries its parse notes in `notes`; an investigation decision has no
    notes field, so they are appended to its summary (which the trace and dashboard show).
    """
    if isinstance(decision, ProposalDecision):
        if not reply_notes:
            return decision
        return decision.model_copy(update={"notes": [*reply_notes, *decision.notes]})
    notes = [*reply_notes, *parse_notes]
    if not notes:
        return decision
    joined = "; ".join(dict.fromkeys(notes))
    summary = f"{decision.summary} [RECAP corrections: {joined}]".strip()
    return decision.model_copy(update={"summary": summary})
