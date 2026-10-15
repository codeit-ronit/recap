"""Parses remediation hints written in runbooks.

Runbook remediation sections contain two plain lines:

    Mechanism: bad_deployment
    Recommended action: execute_rollback

The scripted (non-LLM) policy reads these hints from *retrieved* chunks to choose a
remediation. Hints are untrusted text: a hint naming a tool outside the trusted
remediation catalog is ignored by the policy and would be denied by the gateway anyway.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

_MECHANISM_RE = re.compile(r"^\s*mechanism:\s*([a-z_]+)\s*$", re.IGNORECASE | re.MULTILINE)
_ACTION_RE = re.compile(r"^\s*recommended action:\s*([a-z_]+)\s*$", re.IGNORECASE | re.MULTILINE)


class ActionHint(BaseModel):
    mechanism: str
    action: str


def parse_action_hints(text: str) -> list[ActionHint]:
    """Pair every 'Recommended action:' line with the closest preceding 'Mechanism:' line."""
    mechanisms = [(m.start(), m.group(1).lower()) for m in _MECHANISM_RE.finditer(text)]
    hints: list[ActionHint] = []
    for match in _ACTION_RE.finditer(text):
        preceding = [name for pos, name in mechanisms if pos < match.start()]
        if preceding:
            hints.append(ActionHint(mechanism=preceding[-1], action=match.group(1).lower()))
    return hints
