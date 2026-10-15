"""The central safety policy: decides ALLOW / APPROVAL_REQUIRED / DENY for every tool call.

Where it sits: the tool gateway asks this engine about every read and every mitigation
before anything reaches the MCP server. It is trusted code, and its decisions depend only
on trusted inputs: the registry's risk tier (from configs/policies.yaml), validated
arguments, who is calling, the environment, and the stored approval. They NEVER depend
on model text, a proposal's self-declared risk, tool descriptions or server annotations,
so a prompt-injected planner cannot talk its way past it.

Rules run in the order listed and the first DENY wins. `rule_ids` lists every rule
evaluated, in order, so the last ID is always the rule that produced the verdict.

    R1  tool-name-format        plain lowercase identifier only (blocks look-alike names)
    R2  known-tool              must be in the trusted registry
    R3  safety-scope            safety may be switched off only in a simulated environment
    R4  args-valid              strict argument validation
    R5  namespace-allowlist     environment and namespace argument must be allowlisted
    R6  service-allowlist       (checked by R4's validation; recorded for the audit trail)
    R7  worker-read-only        only the controller may use non-read tools
    R8  read-allow              plain read calls are allowed automatically (not mitigations)
    R9  destructive-deny        destructive tools are always denied
    R10 high-risk-deny          high-risk tools are denied in V1
    R14 remediation-catalog     a mitigation must use a tool from the trusted remediation catalog
    R11 reversible-approval     reversible writes need a proposal and an exact approval
    R12 approval-binding        the approval must match this exact, unexpired, unused proposal

R14 was added after the gateway's own R13, so its number is out of order. It runs after
R9/R10, which keeps the risk tier as the reason a destructive or high-risk proposal is denied.
R11 and R12 also deny a proposal whose params set the target argument: the target fills
that argument, so params could only show an approver a decoy service.
"""

from __future__ import annotations

import re

from recap.core.config import PolicySettings, ToolPolicy
from recap.core.models import (
    AgentRole,
    Approval,
    MitigationProposal,
    PolicyRequest,
    SafetyDecision,
    ToolRisk,
    Verdict,
)
from recap.tools.registry import ToolRegistry

R0_SAFETY_DISABLED = "R0-safety-disabled-simulation"
R1_TOOL_NAME_FORMAT = "R1-tool-name-format"
R2_KNOWN_TOOL = "R2-known-tool"
R3_SAFETY_SCOPE = "R3-safety-scope"
R4_ARGS_VALID = "R4-args-valid"
R5_NAMESPACE_ALLOWLIST = "R5-namespace-allowlist"
R6_SERVICE_ALLOWLIST = "R6-service-allowlist"
R7_WORKER_READ_ONLY = "R7-worker-read-only"
R8_READ_ALLOW = "R8-read-allow"
R9_DESTRUCTIVE_DENY = "R9-destructive-deny"
R10_HIGH_RISK_DENY = "R10-high-risk-deny"
R11_REVERSIBLE_APPROVAL = "R11-reversible-approval"
R12_APPROVAL_BINDING = "R12-approval-binding"
R14_REMEDIATION_CATALOG = "R14-remediation-catalog"

# Explicit ASCII classes: uppercase, whitespace, ';', and Cyrillic look-alikes all fail.
_TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_]{2,63}")

Judgement = tuple[Verdict, list[str]]  # (verdict, human-readable reasons)


class PolicyEngine:
    """RECAP's trusted SafetyPolicy implementation (simulator scope in V1)."""

    name = "recap-policy-engine"

    def __init__(self, policies: PolicySettings, registry: ToolRegistry) -> None:
        self.version = policies.version
        self._namespaces = frozenset(policies.allowed_namespaces)
        self._remediation_tools = frozenset(policies.remediation_tools)
        self._registry = registry

    def evaluate(self, request: PolicyRequest) -> SafetyDecision:
        """Judge one tool call or mitigation request."""
        rules: list[str] = []
        verdict, reasons = self._decide(request, rules)
        return SafetyDecision(
            verdict=verdict,
            reasons=reasons,
            rule_ids=rules,
            tool_name=request.tool_name,
            risk=self._registry.risk_of(request.tool_name),
            caller=request.caller,
            policy_version=self.version,
            decided_at=request.now,  # a pure function of the request
        )

    # --- rule chain ----------------------------------------------------------------------

    def _decide(self, request: PolicyRequest, rules: list[str]) -> Judgement:
        """R1-R3: is this a trusted tool, and is the safety switch allowed here?"""
        rules.append(R1_TOOL_NAME_FORMAT)
        if not _TOOL_NAME_RE.fullmatch(request.tool_name):
            return Verdict.DENY, [
                f"tool name {request.tool_name[:64]!r} is not a plain lowercase identifier"
            ]
        rules.append(R2_KNOWN_TOOL)
        policy = self._registry.get(request.tool_name)
        if policy is None:
            return Verdict.DENY, [
                "unknown tool; tools advertised by the MCP server are not trusted"
            ]
        rules.append(R3_SAFETY_SCOPE)
        if not request.safety_enabled:
            return self._safety_disabled(request, rules)
        return self._judge(request, policy, rules)

    def _safety_disabled(self, request: PolicyRequest, rules: list[str]) -> Judgement:
        """Simulation-only ablation: allow, but record what the verdict WOULD have been."""
        if not request.environment.is_simulated:
            return Verdict.DENY, ["safety can only be disabled in a simulated environment"]
        shadow_rules: list[str] = []
        would_be, reasons = self._decide(
            request.model_copy(update={"safety_enabled": True}), shadow_rules
        )
        rules.extend(rule for rule in shadow_rules if rule not in rules)
        rules.append(R0_SAFETY_DISABLED)
        return Verdict.ALLOW, [
            "safety checks disabled (simulation-only ablation); with safety on this would "
            f"have been {would_be.value}: {'; '.join(reasons)}"
        ]

    def _judge(self, request: PolicyRequest, policy: ToolPolicy, rules: list[str]) -> Judgement:
        """R4-R12 and R14: argument, scope, caller, risk-tier, catalog and approval checks."""
        rules.append(R4_ARGS_VALID)
        clean, errors = self._registry.validate_args(request.tool_name, request.args)
        if errors:
            return Verdict.DENY, errors

        rules.append(R5_NAMESPACE_ALLOWLIST)
        namespaces = [request.environment.namespace] + [
            value for arg, value in clean.items() if policy.args[arg].type == "namespace"
        ]
        outside = [ns for ns in namespaces if ns not in self._namespaces]
        if outside:
            return Verdict.DENY, [f"namespace {ns!r} is not in the allowlist" for ns in outside]

        if any(policy.args[arg].type == "service" for arg in clean):
            rules.append(R6_SERVICE_ALLOWLIST)  # already enforced by validate_args (R4)

        rules.append(R7_WORKER_READ_ONLY)
        if request.caller != AgentRole.CONTROLLER and policy.risk != ToolRisk.READ:
            return Verdict.DENY, [
                f"worker agents may only use read-only tools ({request.caller.value} asked "
                f"for a {policy.risk.value} tool)"
            ]

        rules.append(R8_READ_ALLOW)
        # Only a plain read is automatic. A read tool proposed as a mitigation would
        # otherwise skip the approval gate, so it goes on to R14.
        if policy.risk == ToolRisk.READ and request.proposal is None:
            return Verdict.ALLOW, ["read-only tool: allowed automatically"]

        rules.append(R9_DESTRUCTIVE_DENY)
        if policy.risk == ToolRisk.DESTRUCTIVE:
            return Verdict.DENY, [
                "destructive actions are never automated, regardless of model or document text"
            ]

        rules.append(R10_HIGH_RISK_DENY)
        if policy.risk == ToolRisk.HIGH_RISK:
            return Verdict.DENY, ["high-risk actions are denied in V1"]

        if request.proposal is not None:
            rules.append(R14_REMEDIATION_CATALOG)
            if request.tool_name not in self._remediation_tools:
                return Verdict.DENY, [
                    "mitigations must use a tool from the trusted remediation catalog"
                ]

        return self._judge_reversible(request, rules)

    def _judge_reversible(self, request: PolicyRequest, rules: list[str]) -> Judgement:
        """R11-R12: a reversible write runs only with an approval for this exact proposal."""
        rules.append(R11_REVERSIBLE_APPROVAL)
        proposal, approval = request.proposal, request.approval
        if proposal is None:
            return Verdict.DENY, ["mitigations must come from a proposal"]
        if approval is None:
            mismatches = self._proposal_mismatches(request, proposal)
            if mismatches:
                return Verdict.DENY, mismatches
            return Verdict.APPROVAL_REQUIRED, [
                "reversible write: needs a human approval bound to this exact proposal"
            ]

        rules.append(R12_APPROVAL_BINDING)
        problems = self._proposal_mismatches(request, proposal) + _approval_problems(
            request, proposal, approval
        )
        if problems:
            return Verdict.DENY, problems
        return Verdict.ALLOW, [
            f"approval {approval.approval_id} by {approval.actor} matches this exact proposal "
            "and is unused and unexpired"
        ]

    def _proposal_mismatches(
        self, request: PolicyRequest, proposal: MitigationProposal
    ) -> list[str]:
        """Ways the call differs from its proposal, or the proposal names its target twice."""
        problems = []
        if proposal.incident_id != request.incident_id:
            problems.append("proposal belongs to a different incident")
        if proposal.action != request.tool_name:
            problems.append("tool does not match the proposal's action")
        elif request.args != self._registry.mitigation_args(proposal):
            problems.append("arguments differ from the proposal")
        # The target always fills this argument, so a value in params is at best redundant
        # and at worst a decoy shown to the approver. Fail closed instead of guessing.
        target_arg = self._registry.target_arg(proposal.action)
        if target_arg is not None and target_arg in proposal.params:
            problems.append(
                "proposal params may not set the target argument; use the proposal target"
            )
        return problems


def _approval_problems(
    request: PolicyRequest, proposal: MitigationProposal, approval: Approval
) -> list[str]:
    """Every reason this approval does not authorize this exact proposal right now."""
    problems = []
    if approval.decision != "approved":
        problems.append(f"approval was rejected by {approval.actor}")
    if approval.incident_id != request.incident_id:
        problems.append("approval belongs to a different incident")
    if approval.proposal_id != proposal.proposal_id:
        problems.append("approval is for a different proposal")
    if approval.proposal_digest != proposal.digest():
        problems.append("approval digest does not match the proposal (it changed after approval)")
    if request.now >= approval.expires_at:
        problems.append(f"approval expired at {approval.expires_at.isoformat()}")
    if approval.consumed_at is not None:
        problems.append(
            f"approval was already used at {approval.consumed_at.isoformat()} (single use)"
        )
    return problems
