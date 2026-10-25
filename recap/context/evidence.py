"""Evidence registry: the append-only record of everything observed in one run.

Every tool result becomes an immutable `Evidence` with a run-unique ID like
"ev-3f9a2c-007" (runbook chunks and memories enter context packs under their own IDs).
Agents and the verifier only pass these IDs around, so no one can quietly edit what was
observed, and a claim that cites an ID from another run is detectably invalid.

In a parallel round (several specialists at once) each agent's evidence is numbered in its
own namespace, e.g. "ev-3f9a2c-r2t01" for the telemetry investigator's first result in
round 2. IDs then never depend on which agent's result happened to arrive first, and when
the round closes its items are put in a canonical order (agent, then the agent's own
order), in this registry and in the run store. Both make a recorded parallel run replay the
same way whatever the timing.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from typing import Any

from recap.context.tokens import estimate_tokens
from recap.core.ids import utcnow
from recap.core.models import AgentRole, Evidence, SourceType, TrustLevel

# One letter per agent in parallel-round evidence IDs ("r2t01": round 2, telemetry, first).
_ROLE_CODES = {
    AgentRole.TELEMETRY: "t",
    AgentRole.INFRASTRUCTURE: "i",
    AgentRole.INVESTIGATOR: "v",
}


# A plain value: a service name, an enum value, a version, a number. Prompts show source
# refs as trusted text ("already observed"), so any other value is never repeated in one:
# it becomes "#" and the first 8 hex characters of its SHA-256, which still compares exactly.
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9._:+-]{1,64}")

# Argument rules (configs/policies.yaml) the registry checks against a closed set or a fixed
# shape: an allowlisted service or namespace, an enum, a version like v1.2.3, a bounded
# number. Any other argument (a "string", such as search_runbooks' query) is free text a
# model wrote. Its value is hashed in every ref, whatever it looks like: a one-word query
# such as "SYSTEM:approved" has the shape of a plain value, but no rule ever checked it.
_CHECKED_TYPES = frozenset({"service", "namespace", "enum", "version", "int"})


def is_plain_value(value: Any) -> bool:
    """True for a value safe to show as trusted text (see _PLAIN_VALUE)."""
    return isinstance(value, int | float | str) and bool(_PLAIN_VALUE.fullmatch(str(value)))


def free_text_args(rules: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    """The arguments a model may fill with any text, from a tool's registry rules
    (ToolSpecView.args): every argument whose rule is not one of the checked types."""
    return frozenset(arg for arg, rule in rules.items() if rule.get("type") not in _CHECKED_TYPES)


def tool_source_ref(
    tool_name: str, args: dict[str, Any], scheme: str = "mcp", free_text: Collection[str] = ()
) -> str:
    """Canonical reference for a tool call, e.g. 'mcp:get_service_logs(service=checkout)'.

    The gateway stamps it on tool evidence and providers use it to avoid repeating a
    call that was already made in this run. Arguments are sorted so order never matters.
    `scheme` is "local" for a tool RECAP answers itself (search_runbooks). The arguments
    named in `free_text` (free_text_args of the tool's rules) are always hashed. Every
    place that builds a ref to compare with the gateway's must pass the same names, or a
    repeated call would no longer be recognized.
    """
    inner = ",".join(
        f"{key}={_ref_value(args[key], key in free_text)}"
        for key in sorted(args)
        if args[key] is not None
    )
    return f"{scheme}:{tool_name}({inner})"


def _ref_value(value: Any, free_text: bool) -> str:
    if is_plain_value(value) and not free_text:
        return str(value)
    return "#" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]


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
        # e.g. persist to SQLite. Called for each new item at once (a crash keeps it), and
        # again for a parallel round's items, in canonical order, when the round closes.
        self._on_add = on_add
        # The open parallel round, if any: its number, where it starts, per-agent counters.
        self._round: int | None = None
        self._round_start = 0
        self._counters: dict[AgentRole, int] = {}

    def open_round(self, round_number: int, roles: Iterable[AgentRole]) -> int:
        """Start a parallel round: from now on each listed agent's evidence gets IDs in its
        own namespace. Returns the index where the round's evidence starts."""
        self._round = round_number
        self._round_start = len(self._items)
        self._counters = {role: 0 for role in roles}
        return self._round_start

    def close_round(self, order: Iterable[AgentRole]) -> None:
        """End the parallel round and put its evidence in a canonical order: by the agents'
        order in `order`, then each agent's own order (never by arrival time)."""
        if self._round is None:
            return
        rank = {role: index for index, role in enumerate(order)}
        items = list(self._items.values())
        head, tail = items[: self._round_start], items[self._round_start :]
        ordered = [
            evidence
            for _, evidence in sorted(
                enumerate(tail),
                key=lambda pair: (rank.get(pair[1].agent_role, len(rank)), pair[0]),  # type: ignore[arg-type]
            )
        ]
        self._items = {e.id: e for e in [*head, *ordered]}
        self._round, self._round_start, self._counters = None, 0, {}
        if self._on_add:
            # The round's items were stored as they arrived. Writing them again in canonical
            # order leaves a store that keeps an item's latest write last (the run store's
            # INSERT OR REPLACE, listed by rowid) in this order too, so what the API and the
            # exports serve never depends on which agent finished first.
            for evidence in ordered:
                self._on_add(evidence)

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
            id=self._next_id(agent_role),
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

    def _next_id(self, agent_role: AgentRole | None) -> str:
        """Sequential outside parallel rounds ("ev-3f9a2c-007"); per agent inside one
        ("ev-3f9a2c-r2t01")."""
        if self._round is not None and agent_role in self._counters:
            self._counters[agent_role] += 1
            code = _ROLE_CODES.get(agent_role, "x")
            return f"{self._prefix}-r{self._round}{code}{self._counters[agent_role]:02d}"
        return f"{self._prefix}-{len(self._items) + 1:03d}"

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
