"""The tool gateway: the single door every tool call goes through. RECAP's security boundary.

Where it sits: agents and the controller never talk to the MCP server directly. Before a
call reaches the `ToolTransport` (the real MCP client in V1), this gateway asks the central
safety policy and charges the budget. It then enforces a timeout, and the output is
redacted (secrets removed), bounded, flagged for prompt-injection text and stored as
immutable evidence. Every step is written to the audit trace.

Two deliberately separate paths:

  * read path  (`call_read`)          any agent, read-only tools only, allowed automatically.
  * write path (`execute_mitigation`) controller only, needs an approval bound to the exact
    proposal. The approval is consumed before the call, so it can never be replayed. Then
    the gateway signs a single-use capability for exactly that action (recap/safety/
    capabilities.py); the environment refuses any write without one, so the gateway's
    decision is enforced at the write boundary itself, not only by well-behaved callers.

Workers only ever get a `ReadOnlyTools` handle, which cannot express a write. What the
server advertises (names, descriptions, annotations) is compared with the trusted registry
and reported, but it never decides anything.

A few read tools are answered by RECAP itself (`source: local` in configs/policies.yaml,
e.g. search_runbooks over the runbook index). They take the same read path (policy,
argument checks, timeout, redaction, bounds, injection flags, audit), but a handler the
controller registers answers them instead of the MCP server, they do not use the
environment's tool-call budget (each handler keeps its own), and their results are
recorded as DOC evidence, which the verifier never counts as proof.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from recap.context.evidence import EvidenceRegistry, tool_source_ref
from recap.core.config import PolicyLimits
from recap.core.errors import ApprovalError, BudgetExhausted, ConfigError, ReplayMismatch
from recap.core.ids import new_id
from recap.core.interfaces import SafetyPolicy, ToolTransport
from recap.core.models import (
    AgentRole,
    Approval,
    EnvironmentInfo,
    Evidence,
    Incident,
    MitigationProposal,
    PolicyRequest,
    RawToolResult,
    SafetyDecision,
    SourceType,
    ToolRequest,
    ToolResult,
    ToolRisk,
    ToolSpecView,
    TrustLevel,
    Verdict,
)
from recap.harness.budget import BudgetTracker
from recap.safety.approvals import ApprovalStore
from recap.safety.capabilities import CapabilityIssuer
from recap.safety.injection import INJECTION_FLAG, PATTERNS, injection_flags, scan_text
from recap.telemetry.tracer import TraceCategory, Tracer
from recap.tools.actions import WRITE_TOOLS, to_environment_action
from recap.tools.registry import DiscoveryReport, ToolRegistry

RULE_AGENT_ALLOWLIST = "R-agent-tool-allowlist"
RULE_CONFLICTING_REMEDIATION = "R13-conflicting-remediation"
TRUNCATED_FLAG = "truncated"
TRUNCATION_MARK = "...[truncated]"
MAX_ERROR_CHARS = 500
# Chronological lists (oldest first) keep their LAST items, i.e. the most recent ones.
# Every other list (deployments, events, services: newest first) keeps its FIRST items.
_CHRONOLOGICAL_LISTS = frozenset({"lines", "points"})

# Answers one local read tool: (validated arguments, the calling agent) -> its raw result.
LocalTool = Callable[[dict[str, Any], AgentRole], Awaitable[RawToolResult]]


class RemediationLocks:
    """Targets currently being changed, so two fixes never touch one target at once."""

    def __init__(self) -> None:
        self._held: set[str] = set()
        self._guard = threading.Lock()  # runs may execute in different threads

    def acquire(self, key: str) -> bool:
        """Take the lock for `key`; False if another remediation already holds it."""
        with self._guard:
            if key in self._held:
                return False
            self._held.add(key)
            return True

    def release(self, key: str) -> None:
        """Give the lock for `key` back."""
        with self._guard:
            self._held.discard(key)


SHARED_LOCKS = RemediationLocks()  # one per process, shared by every run


class ReadOnlyTools:
    """The only tool handle a worker agent gets: allowlisted read-only tools, nothing else.

    Python cannot truly hide the gateway inside this object, so the gateway does not rely
    on it alone: its read path refuses non-read tools and every write needs an approval.
    """

    def __init__(self, gateway: ToolGateway, caller: AgentRole, allowed_tools: list[str]) -> None:
        not_read = [n for n in allowed_tools if gateway.registry.risk_of(n) != ToolRisk.READ]
        if not_read:
            raise ConfigError(f"{caller.value} may only hold read-only tools, got {not_read}")
        self._gateway = gateway
        self._allowed = list(allowed_tools)
        self.caller = caller

    def specs(self) -> list[ToolSpecView]:
        """Trusted descriptions of the tools this agent may call."""
        return self._gateway.registry.views(self._allowed)

    async def call(self, tool_name: str, args: dict[str, Any]) -> ToolResult:
        """Call one allowlisted read tool through the gateway."""
        if tool_name not in self._allowed:
            reason = f"{tool_name[:64]!r} is not in {self.caller.value}'s tool allowlist"
            # Same-module helper, so the denial still lands in the gateway's audit trail.
            return self._gateway._refuse(tool_name, self.caller, [RULE_AGENT_ALLOWLIST], reason)
        return await self._gateway.call_read(tool_name, args, self.caller)


class ToolGateway:
    """Policy, budget, timeout, redaction, output bounds, evidence and audit for every call."""

    def __init__(
        self,
        *,
        transport: ToolTransport,
        registry: ToolRegistry,
        policy: SafetyPolicy,
        approvals: ApprovalStore,
        budget: BudgetTracker,
        evidence: EvidenceRegistry,
        tracer: Tracer,
        incident: Incident,
        environment: EnvironmentInfo,
        limits: PolicyLimits,
        target_scope: str,
        safety_enabled: bool = True,
        locks: RemediationLocks | None = None,
        redact: Callable[[Any], Any] | None = None,
        capabilities: CapabilityIssuer | None = None,
        local_tools: Mapping[str, LocalTool] | None = None,
        local_timeout_seconds: float | None = None,
    ) -> None:
        self.registry = registry
        self.results: list[ToolResult] = []  # every call in this run, denied ones included
        self._transport = transport
        self._policy = policy
        self._approvals = approvals
        self._budget = budget
        self._evidence = evidence
        self._tracer = tracer
        self._incident = incident
        self._environment = environment
        self._limits = limits
        self._target_scope = target_scope  # what a lock covers, e.g. one sandbox or cluster
        self._safety_enabled = safety_enabled
        self._locks = locks if locks is not None else SHARED_LOCKS
        # Tool output can carry secrets (e.g. a key in a log line), and evidence is stored,
        # served and sent to providers. The runtime passes its Redactor; tests may omit it.
        self._redact = redact or (lambda value: value)
        # Signs one capability per allowed write. Without it a write carries none and the
        # environment refuses it (fail closed), so a gateway built without one cannot write.
        self._capabilities = capabilities
        self.last_write: ToolResult | None = None  # the outcome of the latest write sent
        # Handlers for the tools RECAP answers itself, and how long one may take (a local
        # model call, e.g. embedding a query, can take longer than an MCP read).
        self._local = dict(local_tools or {})
        self._local_timeout = local_timeout_seconds or limits.tool_timeout_seconds

    async def discover(self) -> DiscoveryReport:
        """Ask the server what it offers and compare that with the trusted registry."""
        tools = await self._transport.list_tools()
        report = self.registry.compare(self._transport.transport_name, tools)
        self._tracer.emit(TraceCategory.TOOL_DISCOVERY, report.model_dump())
        return report

    def read_only_view(self, caller: AgentRole, allowed_tools: list[str]) -> ReadOnlyTools:
        """The tool handle for one worker agent."""
        return ReadOnlyTools(self, caller, allowed_tools)

    # --- read path -----------------------------------------------------------------------

    async def call_read(
        self, tool_name: str, args: dict[str, Any], caller: AgentRole
    ) -> ToolResult:
        """Read path: policy -> budget -> transport -> redacted, bounded, flagged evidence."""
        invocation_id = new_id("call")
        decision = self._judge(self._policy_request(tool_name, args, caller))
        # The read path never writes, even if the policy allows it (safety-off ablation).
        if decision.verdict != Verdict.ALLOW or self.registry.risk_of(tool_name) != ToolRisk.READ:
            return self._denied(invocation_id, decision)

        clean_args, _ = self.registry.validate_args(tool_name, args)
        if not self.registry.is_local(tool_name):  # a local tool's handler has its own budget
            self._budget.reserve_tool_call()  # raises BudgetExhausted before anything is sent
        request = self._tool_request(invocation_id, tool_name, clean_args, caller)
        # Key names are a contract with the dashboard: it reads invocation_id, tool, caller
        # and args, and pairs tool.call with tool.result by invocation_id. Never rename them.
        call = {
            "invocation_id": request.invocation_id,
            "tool": request.tool_name,
            "caller": request.caller.value,
        }
        details = request.model_dump(mode="json", include={"args", "incident_id", "requested_at"})
        self._tracer.emit(TraceCategory.TOOL_CALL, {**call, **details}, agent=caller.value)

        result, flags = await self._run(request, decision)
        outcome = result.model_dump(include={"ok", "error", "evidence_id", "truncated"})
        self._tracer.emit(
            TraceCategory.TOOL_RESULT,
            {**call, **outcome, "duration_ms": result.duration_ms, "flags": flags},
            agent=caller.value,
            duration_ms=result.duration_ms,
        )
        self.results.append(result)
        return result

    # --- write path (controller only) ----------------------------------------------------

    def evaluate_mitigation(self, proposal: MitigationProposal) -> SafetyDecision:
        """The policy check before any approval is requested."""
        args = self.registry.mitigation_args(proposal)
        request = self._policy_request(proposal.action, args, AgentRole.CONTROLLER, proposal)
        return self._judge(request)

    async def execute_mitigation(
        self, proposal: MitigationProposal, approval: Approval | None
    ) -> ToolResult:
        """Write path: exact approval -> target lock -> budget -> consume -> one call."""
        invocation_id = new_id("call")
        self.last_write = None  # set again once this write's outcome is known
        tool, args = proposal.action, self.registry.mitigation_args(proposal)
        # Re-read the approval from the store: the caller's copy may be stale or forged.
        fresh = self._approvals.get_approval(approval.approval_id) if approval else None
        policy_request = self._policy_request(tool, args, AgentRole.CONTROLLER, proposal, fresh)
        decision = self._judge(policy_request)
        if decision.verdict != Verdict.ALLOW:
            return self._denied(invocation_id, decision)

        lock_key = f"{self._target_scope}/{self._environment.namespace}/{proposal.target}"
        if not self._locks.acquire(lock_key):
            reason = f"another remediation of {lock_key} is already in progress"
            rules = [*decision.rule_ids, RULE_CONFLICTING_REMEDIATION]
            return self._refuse(tool, AgentRole.CONTROLLER, rules, reason, invocation_id)
        try:
            self._budget.reserve_tool_call()
            if self._safety_enabled and fresh is not None:
                try:
                    # Consume BEFORE the call, so the approval can never be replayed,
                    # even if the call below fails or times out.
                    self._approvals.consume(fresh.approval_id)
                except ApprovalError as exc:  # e.g. another process used it first
                    rules = decision.rule_ids
                    return self._refuse(tool, AgentRole.CONTROLLER, rules, str(exc), invocation_id)
            approval_id = fresh.approval_id if fresh else ""
            # approval_id and proposal_digest name the approved proposal in the environment's
            # audit log; the capability is the proof the environment checks before acting.
            # It is passed only to the transport: never traced, stored or put in evidence.
            extra = {
                "approval_id": approval_id,
                "proposal_digest": proposal.digest(),
                "capability": self._capability(tool, args, approval_id, proposal.digest()),
            }
            request = self._tool_request(invocation_id, tool, args, AgentRole.CONTROLLER)
            write = {
                "invocation_id": invocation_id,
                "tool": tool,
                "target": proposal.target,
                "params": proposal.params,
                "approval_id": approval_id or None,
                "proposal_digest": proposal.digest(),
            }
            # Write-ahead: the trace shows the write was sent even if this process is
            # cancelled or dies before the outcome comes back.
            self._tracer.emit(
                TraceCategory.MITIGATION_STARTED, write, agent=AgentRole.CONTROLLER.value
            )
            result, cancelled = await self._send_write(request, decision, extra)
        finally:
            self._locks.release(lock_key)

        result = _with_action_outcome(result)
        self.last_write = result
        # The outcome is traced here, not as tool.call/tool.result: the dashboard draws and
        # counts each write once, from this event.
        self._tracer.emit(
            TraceCategory.MITIGATION,
            {
                **write,
                "ok": result.ok,
                "error": result.error,
                "side_effects": result.side_effects,
                "evidence_id": result.evidence_id,
                "safety_enabled": self._safety_enabled,
                "run_cancelled": cancelled is not None,
            },
            agent=AgentRole.CONTROLLER.value,
            duration_ms=result.duration_ms,
        )
        self.results.append(result)
        if cancelled is not None:
            raise cancelled  # the run is being stopped; the write's outcome is recorded above
        return result

    async def _send_write(
        self, request: ToolRequest, decision: SafetyDecision, extra: dict[str, Any]
    ) -> tuple[ToolResult, asyncio.CancelledError | None]:
        """Send one write and learn its outcome, even if the run is cancelled meanwhile.

        Cancelling this coroutine cannot call back a write that already reached the target,
        so the outcome is awaited anyway (the tool timeout bounds the wait) and returned with
        the cancellation, which the caller re-raises after recording what happened.
        """
        send = asyncio.ensure_future(self._run(request, decision, extra))
        try:
            result, _ = await asyncio.shield(send)
            return result, None
        except asyncio.CancelledError as cancelled:
            result, _ = await send
            return result, cancelled

    def _capability(
        self, tool: str, args: dict[str, Any], approval_id: str, proposal_digest: str
    ) -> str:
        """A signed, single-use capability for exactly the environment action of this call."""
        if self._capabilities is None or tool not in WRITE_TOOLS:
            return ""  # nothing to sign: the environment (or the server) refuses the write
        action, target, params = to_environment_action(tool, args)
        return self._capabilities.issue(
            action=action,
            target=target,
            params=params,
            approval_id=approval_id,
            proposal_digest=proposal_digest,
            # False only in the simulation-only safety-off ablation (no approval exists); a
            # real environment adapter refuses such a capability.
            safety_enabled=self._safety_enabled,
        )

    # --- shared steps --------------------------------------------------------------------

    def _policy_request(
        self,
        tool_name: str,
        args: dict[str, Any],
        caller: AgentRole,
        proposal: MitigationProposal | None = None,
        approval: Approval | None = None,
    ) -> PolicyRequest:
        """What the policy judges: the call as asked for, before anything is checked."""
        return PolicyRequest(
            tool_name=tool_name,
            args=args,
            caller=caller,
            incident_id=self._incident.incident_id,
            environment=self._environment,
            proposal=proposal,
            approval=approval,
            safety_enabled=self._safety_enabled,
        )

    def _tool_request(
        self, invocation_id: str, tool_name: str, args: dict[str, Any], caller: AgentRole
    ) -> ToolRequest:
        """What is sent: a call that passed every check, with its validated arguments."""
        return ToolRequest(
            invocation_id=invocation_id,
            tool_name=tool_name,
            args=args,
            caller=caller,
            incident_id=self._incident.incident_id,
        )

    def _judge(self, request: PolicyRequest) -> SafetyDecision:
        """Ask the policy, and write its decision to the audit trail."""
        decision = self._policy.evaluate(request)
        self._trace_decision(decision)
        return decision

    def _trace_decision(self, decision: SafetyDecision) -> None:
        payload = decision.model_dump(mode="json", include={"verdict", "rule_ids", "reasons"})
        caller = decision.caller.value if decision.caller else None
        risk = decision.risk.value if decision.risk else None
        self._tracer.emit(
            TraceCategory.POLICY,
            {"tool": decision.tool_name, **payload, "caller": caller, "risk": risk},
            agent=caller,
        )

    def _refuse(
        self,
        tool_name: str,
        caller: AgentRole,
        rule_ids: list[str],
        reason: str,
        invocation_id: str | None = None,
    ) -> ToolResult:
        """A denial decided by the gateway itself (agent allowlist, lock conflict, replay)."""
        decision = SafetyDecision(
            verdict=Verdict.DENY,
            reasons=[reason],
            rule_ids=rule_ids,
            tool_name=tool_name,
            risk=self.registry.risk_of(tool_name),
            caller=caller,
            policy_version=self._policy.version,
        )
        self._trace_decision(decision)
        return self._denied(invocation_id or new_id("call"), decision)

    def _denied(self, invocation_id: str, decision: SafetyDecision) -> ToolResult:
        """A refused call: nothing was sent to the transport and nothing was charged."""
        error = "denied: " + "; ".join(decision.reasons)
        identity = {"invocation_id": invocation_id, "tool_name": decision.tool_name}
        result = ToolResult(**identity, ok=False, error=error, decision=decision)
        self.results.append(result)
        return result

    async def _run(
        self,
        request: ToolRequest,
        decision: SafetyDecision,
        transport_extra: dict[str, Any] | None = None,
    ) -> tuple[ToolResult, list[str]]:
        """Send one call; a successful output is redacted, bounded, flagged and kept as evidence."""
        started = time.perf_counter()
        if self.registry.is_local(request.tool_name):
            raw = await self._send_local(request)
        else:
            raw = await self._send(request.tool_name, {**request.args, **(transport_extra or {})})
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        identity = {
            "invocation_id": request.invocation_id,
            "tool_name": request.tool_name,
            "decision": decision,
        }
        if not raw.ok:  # failed calls are traced but never become evidence
            # Error text is tool output too: providers and the run record show it.
            error = self._redact(raw.error or raw.text or "tool call failed")[:MAX_ERROR_CHARS]
            return ToolResult(**identity, ok=False, error=error, duration_ms=duration_ms), []
        output, evidence, truncated = self._record(request, raw)
        local = self.registry.is_local(request.tool_name)
        result = ToolResult(
            **identity,
            ok=True,
            output=output,
            duration_ms=duration_ms,
            evidence_id=evidence.id,
            truncated=truncated,
            docs=raw.docs if local else [],  # documents only ever come from RECAP's own index
        )
        return result, list(evidence.flags)

    async def _send(self, tool_name: str, args: dict[str, Any]) -> RawToolResult:
        """One transport call with a hard timeout. Failures become error results, not crashes."""
        timeout = self._limits.tool_timeout_seconds
        try:
            call = self._transport.call_tool(tool_name, args, timeout)
            return await asyncio.wait_for(call, timeout)
        except TimeoutError:
            return RawToolResult(ok=False, error=f"tool call timed out after {timeout:g}s")
        except Exception as exc:  # a broken transport or server must not crash the run
            return RawToolResult(ok=False, error=f"transport error: {type(exc).__name__}: {exc}")

    async def _send_local(self, request: ToolRequest) -> RawToolResult:
        """Answer a local read tool with its handler, under the local timeout."""
        handler = self._local.get(request.tool_name)
        if handler is None:
            error = f"{request.tool_name} is not available in this run"
            return RawToolResult(ok=False, error=error)
        timeout = self._local_timeout
        try:
            return await asyncio.wait_for(handler(dict(request.args), request.caller), timeout)
        except TimeoutError:
            error = f"{request.tool_name} timed out after {timeout:g}s"
            return RawToolResult(ok=False, error=error)
        except (BudgetExhausted, ReplayMismatch):
            # The run's own limits, or a replay asked for something it never recorded: these
            # stop the whole run (as they do for a model call), not just this one call.
            raise
        except Exception as exc:  # anything else fails this call only, like a broken server
            error = f"{request.tool_name} failed: {type(exc).__name__}: {exc}"
            return RawToolResult(ok=False, error=error)

    def _record(
        self, request: ToolRequest, raw: RawToolResult
    ) -> tuple[dict[str, Any], Evidence, bool]:
        """Redact a successful output, bound it, flag injection text, and store it as evidence."""
        structured = raw.structured if raw.structured is not None else {"text": raw.text}
        # Redact before bounding: a secret cut in half by truncation would no longer match.
        data, lists_cut = _bound(self._redact(structured), self._limits)
        content, text_cut = _render(data, self._limits.max_output_chars)
        truncated = lists_cut or text_cut
        flags = injection_flags(content) + ([TRUNCATED_FLAG] if truncated else [])
        local = self.registry.is_local(request.tool_name)
        evidence = self._evidence.add(
            # A local tool reads RECAP's own reference data (runbooks): DOC evidence, which
            # the verifier never counts as a live observation.
            source_type=SourceType.DOC if local else SourceType.TOOL,
            source_ref=tool_source_ref(
                request.tool_name, request.args, scheme="local" if local else "mcp"
            ),
            content=content,
            data=data,
            tool_name=request.tool_name,
            trust_level=TrustLevel.UNTRUSTED,  # tool output is data, never instructions
            flags=flags,
            agent_role=request.caller,
        )
        if INJECTION_FLAG in flags:
            patterns = scan_text(content)
            found = {"evidence_id": evidence.id, "tool": request.tool_name, "patterns": patterns}
            excerpt = _excerpt(content, patterns)
            self._tracer.emit(
                TraceCategory.INJECTION, {**found, "excerpt": excerpt}, agent=request.caller.value
            )
        return data, evidence, truncated


# --- output bounding ----------------------------------------------------------------------


def _bound(value: Any, limits: PolicyLimits, key: str | None = None) -> tuple[Any, bool]:
    """Cut long lists and long strings anywhere in a structure. Returns (bounded, was_cut)."""
    if isinstance(value, dict):
        pairs = {k: _bound(v, limits, k) for k, v in value.items()}
        return {k: v for k, (v, _) in pairs.items()}, any(cut for _, cut in pairs.values())
    if isinstance(value, list):
        keep = limits.max_list_items
        cut = len(value) > keep
        if cut:
            value = value[len(value) - keep :] if key in _CHRONOLOGICAL_LISTS else value[:keep]
        items = [_bound(item, limits) for item in value]
        return [v for v, _ in items], cut or any(c for _, c in items)
    if isinstance(value, str) and len(value) > limits.max_output_chars:
        return _head_and_tail(value, limits.max_output_chars), True
    return value, False


def _render(data: dict[str, Any], max_chars: int) -> tuple[str, bool]:
    """Compact JSON text of the data (the raw record), hard-capped at max_chars.

    Providers read a structured summary of `data` instead (recap/context/summaries.py);
    this text is the evidence's raw record for the registry, the dashboard and audits.
    """
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= max_chars:
        return text, False
    return _head_and_tail(text, max_chars), True


def _head_and_tail(text: str, max_chars: int) -> str:
    """Keep the start and the end of a long text, with TRUNCATION_MARK where the middle was.

    Tool output is often chronological (oldest first), so keeping only the beginning would
    throw away the newest lines, the ones that explain an incident (audit finding 2.1).
    Two thirds of the room go to the beginning, one third to the end.
    """
    head = max_chars * 2 // 3
    tail = max_chars - head
    return text[:head] + TRUNCATION_MARK + text[len(text) - tail :]


def _excerpt(content: str, patterns: list[str], width: int = 80) -> str:
    """The text around the first suspicious match, so a human can see what was injected."""
    match = PATTERNS[patterns[0]].search(content) if patterns else None
    if match is None:
        return content[: 2 * width]
    return content[max(0, match.start() - width) : match.end() + width]


def _with_action_outcome(result: ToolResult) -> ToolResult:
    """Apply the environment's ActionOutcome: it can refuse an action in a successful call."""
    outcome = result.output or {}
    effects = outcome.get("side_effects")
    update: dict[str, Any] = {
        "side_effects": [str(e) for e in effects] if isinstance(effects, list) else []
    }
    if result.ok and outcome.get("ok") is False:  # e.g. rollback to an unknown version
        message = str(outcome.get("message") or "action refused by the environment")
        update |= {"ok": False, "error": message[:MAX_ERROR_CHARS]}
    return result.model_copy(update=update)
