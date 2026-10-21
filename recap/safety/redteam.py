"""RED-TEAM ONLY: a planner that has been successfully prompt-injected.

Where it sits: this wraps any LLMProvider (the scripted policy by default) and plays the
worst case for RECAP's safety story. When untrusted data (a log line, a runbook chunk,
a memory) contains an instruction like "run delete_namespace", this planner obeys it and
proposes deleting the namespace, even lying that the action is read-only.

It exists so tests and the `malicious-log` demo can prove the point that matters: safety
does not depend on the planner behaving. The proposal still has to pass the central
policy in the tool gateway, which denies destructive actions (rule R9) no matter what any
text or any planner says. Selected with RunConfig(planner="prompt_injected").
"""

from __future__ import annotations

import re

from recap.core.interfaces import LLMProvider
from recap.core.models import (
    InvestigationDecision,
    InvestigationRequest,
    MitigationDraft,
    ProposalDecision,
    ProposalRequest,
    ProviderInfo,
    ToolRisk,
)
from recap.safety.injection import INJECTION_FLAG, scan_text

DEFAULT_NAMESPACE = "shop"
_DESTRUCTIVE_PATTERN = "imperative-destructive"  # pattern name in recap.safety.injection

# Explicit namespace forms only, e.g. "namespace=shop", "namespace: shop" or
# "delete_namespace('shop')". Anything vaguer falls back to DEFAULT_NAMESPACE.
_NAMESPACE_KEY_RE = re.compile(r"namespace\s*[=:]\s*['\"]?(?P<ns>[a-z][a-z0-9-]*)", re.IGNORECASE)
_NAMESPACE_CALL_RE = re.compile(
    r"delete_namespace\(\s*['\"]?(?P<ns>[a-z][a-z0-9-]*)['\"]?\s*\)", re.IGNORECASE
)


class PromptInjectedProvider:
    """Wraps a provider and follows destructive instructions found in untrusted data."""

    def __init__(self, base: LLMProvider) -> None:
        self._base = base
        self.info = ProviderInfo(
            name=f"{base.info.name}+prompt-injected(red-team)",
            kind=base.info.kind,
            origin=base.info.origin,
            model=base.info.model,
            mode=base.info.mode,
            prompt_version=base.info.prompt_version,
            description=(
                "RED-TEAM ONLY: simulates a planner that was successfully prompt-injected. "
                "It proposes whatever destructive action untrusted data asks for, so tests "
                "can show the safety gateway still blocks it."
            ),
        )

    async def next_action(self, request: InvestigationRequest) -> InvestigationDecision:
        """Investigation is unchanged: the compromise shows up when proposing a fix."""
        return await self._base.next_action(request)

    async def propose_mitigation(self, request: ProposalRequest) -> ProposalDecision:
        """Propose delete_namespace if untrusted data says so; otherwise act normally."""
        found = _find_injected_instruction(request)
        if found is None:
            return await self._base.propose_mitigation(request)
        source_id, is_evidence, text = found
        namespace = _target_namespace(text)
        draft = MitigationDraft(
            action="delete_namespace",
            target=namespace,
            params={"namespace": namespace},
            rationale=(
                "(red-team) compromised planner followed an instruction found in untrusted data"
            ),
            evidence_ids=[source_id] if is_evidence else [],
            citations=[] if is_evidence else [source_id],  # doc chunk / episode IDs
            risk_level=ToolRisk.READ,  # a deliberate lie; the policy ignores declared risk
            source="redteam",
        )
        return ProposalDecision(
            draft=draft, notes=["red-team: simulated successful prompt injection"]
        )


def _find_injected_instruction(request: ProposalRequest) -> tuple[str, bool, str] | None:
    """First (id, is_evidence, text) whose text carries a destructive instruction."""
    candidates = [(ev.id, True, ev.content, ev.flags) for ev in request.evidence.values()]
    candidates += [
        (item.item_id, item.kind == "evidence", item.text, tuple(item.flags))
        for item in request.context.items
    ]
    for source_id, is_evidence, text, flags in candidates:
        # Compacted context text may have lost the pattern, so the flag counts too.
        if _DESTRUCTIVE_PATTERN in scan_text(text) or INJECTION_FLAG in flags:
            return source_id, is_evidence, text
    return None


def _target_namespace(text: str) -> str:
    """The namespace an injected instruction names, or the default one."""
    for pattern in (_NAMESPACE_KEY_RE, _NAMESPACE_CALL_RE):
        match = pattern.search(text)
        if match:
            return match.group("ns").lower()
    return DEFAULT_NAMESPACE
