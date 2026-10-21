"""The decision loop shared by every provider that talks to a language model over HTTP.

Where it sits:

    AgentHarness / IncidentController
        | next_action(request) / propose_mitigation(request)        (the LLMProvider interface)
        v
    HttpLLMProvider  render (protocol.py) -> _send (subclass) -> parse (protocol.py)
        |-- OllamaProvider      local model, /api/chat              (ollama_provider.py)
        '-- FreeHostedProvider  Groq Free plan, /chat/completions   (openai_compatible.py)

One decision is at most `max_attempts` answers from the model:

    1. render the messages and the request's own JSON schema (or tool definitions)
    2. _send: the subclass turns that into its server's HTTP request(s) and returns a
       provider-neutral ModelReply (content, tool calls, exact token counts, ...)
    3. parse the reply with the step protocol
    4. usable?  -> return the decision
       not?     -> ask again once, with the problem as feedback (a corrective retry)
    5. still unusable -> an empty "finish" (or no proposal). Never a scripted fallback.

Every HTTP request is accounted for. The subclass logs each exchange it makes, including
the ones it retries (a short 429, a 5xx) and the one that made it give up, and each becomes
a ModelCall (an `llm.call` trace event) with its exact token counts in the Usage. When a
decision fails after requests went out (a refusal, a quota stop, the free-plan tripwire),
the requests already sent are attached to the exception (`attach_model_calls`), and the
harness and controller record them before the error goes on: the request most likely to
have cost something is never the one missing from the record.

HTTP-level trouble is the subclass's job: it raises ProviderError (the run fails with that
message), FreeStatusLost (the provider is stopped) or QuotaExhausted (the run is deferred).

The run's model-token budget (RunBudget.max_model_tokens) is enforced here when the
controller binds it (`bind_token_budget`): each attempt reserves its estimated prompt plus
`max_output_tokens` BEFORE it is sent, and settles with the tokens the server reported
afterwards (the estimate for an answer that reported none). Running out raises
BudgetExhausted before anything is sent. A replay settles with the recorded counts, so it
stops where the original run would have.
"""

from __future__ import annotations

import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol

from recap.context.tokens import estimate_tokens
from recap.core.config import ProviderSettings
from recap.core.errors import attach_model_calls
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
# What the model is told when the server itself rejected its answer (a schema failure).
SCHEMA_REJECTED = (
    "the server rejected your answer because it did not match the required JSON schema; "
    "answer again, following the schema exactly."
)

Purpose = Literal["investigate", "propose"]


class TokenBudget(Protocol):
    """What the provider needs from the run's BudgetTracker (recap/harness/budget.py)."""

    def reserve_model_tokens(self, tokens: int) -> int: ...

    def settle_model_tokens(self, reserved: int, actual: int) -> None: ...


@dataclass(frozen=True)
class ModelReply:
    """One HTTP exchange with the model server, in a provider-neutral shape.

    `status` 200 is an answer; anything else is an exchange that produced no usable answer
    (a 429, a 5xx, a refusal, a schema failure) and is still recorded.
    """

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
    error_code: str = ""  # e.g. "rate_limit_exceeded", "json_validate_failed"
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

    api: ClassVar[str]  # "ollama" or "openai_compatible"
    # Where decisions come from, fixed by the CODE (never by configuration), because the
    # run badge and the cost line depend on it. A replay transport turns it into "replay".
    origin: ClassVar[ProviderOrigin]

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
        # RunBudget overrides for runs with this provider. For a replay these come from the
        # recording, so the replayed run gets the same limits (and the same "step N of M").
        self.budget_overrides: dict[str, Any] = dict(settings.budget)
        replaying = transport.name == "replay"
        self.info = ProviderInfo(
            name=f"{'replay:' if replaying else ''}{name}:{self.model}",
            kind="replay" if replaying else "llm",
            origin="replay" if replaying else type(self).origin,
            model=self.model,
            description=settings.description,
            mode=self.mode,
            prompt_version=protocol.PROMPT_VERSION,
            replay_of=replay_of,
            recording=recording,
        )
        # The run's model-token budget, bound by the controller (None: not enforced here).
        self.token_budget: TokenBudget | None = None

    def bind_token_budget(self, budget: TokenBudget) -> None:
        """Enforce the run's max_model_tokens on every request this provider sends."""
        self.token_budget = budget

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
            decision = InvestigationDecision(
                action="finish", summary=summary, kind="no_usable_answer"
            )
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
            log: list[ModelReply] = []  # every HTTP exchange of this attempt, in order
            prompt = estimate_tokens(canonical_json(sent)) + estimate_tokens(
                canonical_json(schema or tools or {})
            )
            reserved = self._reserve_run_tokens(prompt + self.settings.max_output_tokens)
            try:
                reply = await self._send(sent, schema=schema, tools=tools, purpose=purpose, log=log)
            except Exception as exc:
                self._settle_run_tokens(reserved, log, prompt)
                # Requests already sent are recorded before the error goes on (they may have
                # cost tokens, and they show what happened).
                for exchange in log:
                    calls.append(self._model_call(purpose, attempt, sent, schema, exchange, None))
                    usage = usage.plus(_reply_usage(exchange))
                attach_model_calls(exc, calls, usage)
                raise
            self._settle_run_tokens(reserved, log, prompt)
            for retried in log[:-1]:  # exchanges the subclass retried (a short 429, a 5xx)
                calls.append(self._model_call(purpose, attempt, sent, schema, retried, None))
                usage = usage.plus(_reply_usage(retried))
            if reply.status == 200:
                parsed = parse(reply)
            else:  # the server rejected the answer (a schema failure): unusable, ask again
                parsed = Parsed(None, "rejected", problem=SCHEMA_REJECTED)
            calls.append(self._model_call(purpose, attempt, sent, schema, reply, parsed))
            usage = usage.plus(_reply_usage(reply))
            if parsed.decision is not None:
                decision = _with_notes(parsed.decision, reply.notes, parsed.notes)
                return Outcome(decision, usage, calls, "")
            problem = parsed.problem
        return Outcome(None, usage, calls, problem)

    # Named for the RUN's budget: FreeHostedProvider has its own `_reserve` (the free plan).
    def _reserve_run_tokens(self, tokens: int) -> int:
        """Reserve one request's worst case against the run's budget (0 when unbound)."""
        return self.token_budget.reserve_model_tokens(tokens) if self.token_budget else 0

    def _settle_run_tokens(self, reserved: int, log: Sequence[ModelReply], prompt: int) -> None:
        if self.token_budget is not None:
            self.token_budget.settle_model_tokens(reserved, _spent(log, prompt))

    @abstractmethod
    async def _send(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        purpose: Purpose,
        log: list[ModelReply],
    ) -> ModelReply:
        """Send the request to the model server and return the final exchange.

        Append EVERY HTTP exchange to `log` as soon as it is made, before retrying or
        raising: the base class records each one. Return the last one (status 200, or a
        server-rejected answer); raise ProviderError / FreeStatusLost / QuotaExhausted.
        """

    # --- trace records -------------------------------------------------------------------

    def _model_call(
        self,
        purpose: Purpose,
        attempt: int,
        sent: list[Message],
        schema: dict[str, Any] | None,
        reply: ModelReply,
        parsed: Parsed | None,
    ) -> ModelCall:
        """The trace record of one HTTP exchange. `parsed` is None for an exchange that
        produced no answer to parse (a retried 429 or 5xx, or one that ended the decision)."""
        answer = reply.content
        if reply.tool_calls:  # tools mode: show the calls the model made
            answer = (answer + "\n" if answer else "") + canonical_json(reply.tool_calls)
        notes = list(reply.notes)
        if parsed is not None:
            notes += parsed.notes
            if parsed.problem:
                notes.append(f"unusable: {parsed.problem}")
        if reply.status != 200 and reply.error_code:
            notes.append(f"HTTP {reply.status} {reply.error_code}")
        return ModelCall(
            provider=self.name,
            model=self.model,
            model_reported=reply.model_reported,
            origin=self.info.origin,
            purpose=purpose,
            mode="json" if schema is not None else "tools",
            attempt=attempt,
            status=reply.status,
            ok=parsed is not None and parsed.decision is not None,
            response_kind=parsed.kind if parsed is not None else "http_error",
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            duration_ms=reply.duration_ms,
            finish_reason=reply.finish_reason,
            prompt_sha256=sha256_hex(canonical_json(sent)),
            prompt_chars=sum(len(str(message.get("content", ""))) for message in sent),
            output_excerpt=_printable(answer)[:EXCERPT_CHARS],
            thinking_excerpt=_printable(reply.thinking)[:EXCERPT_CHARS],
            rate_limit=reply.rate_limit,
            replayed=reply.replayed,
            notes=notes,
        )


def _spent(log: Sequence[ModelReply], prompt: int) -> int:
    """What an attempt's exchanges cost in model tokens, for the run's budget.

    The server's own counts when it reported them (a replay reports the recorded ones);
    the prompt estimate for an answer that reported none; nothing for an exchange that
    generated nothing (a 429, a 5xx).
    """
    total = 0
    for reply in log:
        if reply.input_tokens is not None or reply.output_tokens is not None:
            total += (reply.input_tokens or 0) + (reply.output_tokens or 0)
        elif reply.status in (200, 400):
            total += prompt
    return total


def _reply_usage(reply: ModelReply) -> Usage:
    """What one HTTP exchange cost. A replayed exchange consumed nothing now.

    A request counts as "unreported" only when the server may have generated tokens (an
    answer or a schema failure) but reported none; a 429 or a 5xx generated nothing.
    """
    if reply.replayed:
        return Usage()
    reported = reply.input_tokens is not None and reply.output_tokens is not None
    may_have_generated = reply.status == 200 or reply.status == 400
    return Usage(
        model_requests=1,
        actual_input_tokens=reply.input_tokens,
        actual_output_tokens=reply.output_tokens,
        is_estimate=not reported,
        unreported_calls=1 if may_have_generated and not reported else 0,
    )


def _printable(text: str) -> str:
    """Model text without control characters (terminal escape codes), newlines kept."""
    return "".join(
        char for char in text if char in "\n\t" or not unicodedata.category(char).startswith("C")
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
