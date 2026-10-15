"""Evidence registry: the append-only record of everything observed in one run.

Every tool result becomes an immutable `Evidence` with a run-unique ID like
"ev-3f9a2c-007" (runbook chunks and memories enter context packs under their own IDs).
Agents and the verifier only pass these IDs around, so no one can quietly edit what was
observed, and a claim that cites an ID from another run is detectably invalid.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from recap.context.tokens import estimate_tokens
from recap.core.ids import utcnow
from recap.core.models import AgentRole, Evidence, SourceType, TrustLevel


def tool_source_ref(tool_name: str, args: dict[str, Any]) -> str:
    """Canonical reference for a tool call, e.g. 'mcp:get_service_logs(service=checkout)'.

    The gateway stamps it on tool evidence and providers use it to avoid repeating a
    call that was already made in this run. Arguments are sorted so order never matters.
    """
    inner = ",".join(f"{key}={args[key]}" for key in sorted(args) if args[key] is not None)
    return f"mcp:{tool_name}({inner})"


class EvidenceRegistry:
    """Stores the original observations of one run. Context packs are views over it."""

    def __init__(
        self,
        run_id: str,
        incident_id: str,
        on_add: Callable[[Evidence], None] | None = None,
    ) -> None:
        self.run_id = run_id
        self.incident_id = incident_id
        self._prefix = f"ev-{run_id.split('-')[-1][:6]}"
        self._items: dict[str, Evidence] = {}
        self._on_add = on_add  # e.g. persist to SQLite

    def add(
        self,
        *,
        source_type: SourceType,
        source_ref: str,
        content: str,
        data: dict[str, Any] | None = None,
        tool_name: str | None = None,
        trust_level: TrustLevel = TrustLevel.UNTRUSTED,
        flags: Iterable[str] = (),
        agent_role: AgentRole | None = None,
    ) -> Evidence:
        """Record a new observation and return it with its assigned ID."""
        evidence = Evidence(
            id=f"{self._prefix}-{len(self._items) + 1:03d}",
            incident_id=self.incident_id,
            source_type=source_type,
            source_ref=source_ref,
            timestamp=utcnow(),
            content=content,
            data=data or {},
            tool_name=tool_name,
            trust_level=trust_level,
            token_estimate=estimate_tokens(content),
            flags=tuple(flags),
            agent_role=agent_role,
        )
        self._items[evidence.id] = evidence
        if self._on_add:
            self._on_add(evidence)
        return evidence

    def get(self, evidence_id: str) -> Evidence | None:
        return self._items.get(evidence_id)

    def exists(self, evidence_id: str) -> bool:
        """True only for evidence recorded in THIS run for THIS incident."""
        item = self._items.get(evidence_id)
        return item is not None and item.incident_id == self.incident_id

    def by_ids(self, ids: Iterable[str]) -> list[Evidence]:
        return [self._items[i] for i in ids if i in self._items]

    def all(self) -> list[Evidence]:
        return list(self._items.values())

    def ids(self) -> list[str]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)
