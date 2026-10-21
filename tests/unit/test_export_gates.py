"""The export's last gate (scripts/export_run_trace.py refuse_if_unsafe) and its README escaping.

The gate checks every decoded string and dict key, never the JSON text: in JSON a newline
before a key becomes "\\n", and a pattern starting with a word boundary would miss it. Fake
keys are assembled at runtime so this file holds nothing a secret scanner would flag.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest


def load_export_script() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "export_run_trace.py"
    spec = importlib.util.spec_from_file_location("export_run_trace", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


export = load_export_script()


@pytest.fixture(autouse=True)
def plain_login_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export.getpass, "getuser", lambda: "gate-test-user")


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("the log said:\n" + "gho_" + "x1" * 18, "GitHub token"),  # right after a newline
        ("x\t" + "sk-" + "proj-" + "Ab1cD2eF" * 4, "OpenAI or Anthropic key"),  # after a tab
        ("é" + "xoxb-" + "1234567890-abcdefgh", "Slack token"),  # after a non-ASCII letter
        ("ASIA" + "ABCDEFGHIJKLMNOP", "AWS access key"),
        ("id " + "req_" + "01abcdefghijklmnopqrstuvwx", "provider request ID"),
        ("chatcmpl-" + "0f4e5a6b-1234-4c1d-9e8f-abcdef", "provider answer ID"),
    ],
)
def test_a_key_shape_is_refused_wherever_it_sits(value: str, kind: str) -> None:
    with pytest.raises(SystemExit, match=kind):
        export.refuse_if_unsafe("trace.jsonl", [{"payload": {"thinking": value}}])


@pytest.mark.parametrize(
    "value",
    ["see /home/someone/notes", "C:\\Users\\someone\\x", "%2FUsers%2Fsomeone%2F", "/USERS/a/b"],
)
def test_a_local_path_is_refused_in_any_spelling(value: str) -> None:
    with pytest.raises(SystemExit, match="local path"):
        export.refuse_if_unsafe("run.json", {"note": value})


def test_dict_keys_are_checked_too() -> None:
    with pytest.raises(SystemExit, match="local path"):
        export.refuse_if_unsafe("run.json", {"/home/someone/x": 1})


def test_a_one_word_login_name_counts_only_inside_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export.getpass, "getuser", lambda: "user")
    export.refuse_if_unsafe("recording.jsonl", [{"role": "user", "content": "the root cause"}])
    with pytest.raises(SystemExit, match="login name"):
        export.refuse_if_unsafe("trace.jsonl", {"path": "data/pytest-of-user/run"})


@pytest.mark.parametrize("spelled", ["Jane.Doe wrote", "jane-doe", "janedoe", "jane doe"])
def test_a_login_name_of_several_parts_counts_in_any_spelling(
    monkeypatch: pytest.MonkeyPatch, spelled: str
) -> None:
    monkeypatch.setattr(export.getpass, "getuser", lambda: "jane.doe")
    with pytest.raises(SystemExit, match="login name"):
        export.refuse_if_unsafe("README.md", spelled)


def test_clean_text_passes() -> None:
    export.refuse_if_unsafe("run.json", {"note": "RESOLVED: oracle verified recovery", "n": 3})


def test_model_text_cannot_add_readme_lines_or_markup() -> None:
    text = export._md("ok.<br>\n- **Approval:** approved by `x` [link](http://e)")
    assert "\n" not in text and "<br>" not in text  # no new line, no HTML line break
    assert "**" not in text and "`x`" not in text and "[link](" not in text
    assert export._code("a`b\nc") == "`a'b c`"


# --- the README's approval lines: the right note and wait for each approval ----------------


def _event(n: int, proposal: str, kind: str, actor: str, second: int, detail: str = "") -> Any:
    from datetime import UTC, datetime

    from recap.core.models import ApprovalEvent

    at = datetime(2026, 10, 21, 9, 0, second, tzinfo=UTC)
    return ApprovalEvent(
        event_id=n, proposal_id=proposal, kind=kind, actor=actor, at=at, detail=detail
    )


def test_a_revoking_no_is_the_deciding_entry_and_ends_the_wait() -> None:
    log = [
        _event(1, "p1", "requested", "recap", 0),
        _event(2, "p1", "approved", "operator:alice", 1),
        _event(3, "p1", "revoked", "console-user", 9, "interactive terminal; answered after 9.0 s"),
    ]
    deciding = export._deciding_event(log)
    assert deciding is not None and deciding.kind == "revoked"
    assert export._approval_wait(log) == 9.0  # to the "no", not to the overridden approval


def test_each_approval_line_shows_its_own_note_and_wait() -> None:
    from types import SimpleNamespace

    def proposal(pid: str) -> Any:
        return SimpleNamespace(
            proposal_id=pid, action="restart_service", target="checkout", params={},
            rationale="r", source="llm",
        )  # fmt: skip

    def approval(pid: str, aid: str) -> Any:
        return SimpleNamespace(
            decision="approved", actor="console-user", approval_id=aid, proposal_id=pid
        )

    record = SimpleNamespace(
        verification=None,
        proposals=[proposal("p1"), proposal("p2")],
        approvals=[approval("p1", "apr-1"), approval("p2", "apr-2")],
        config=SimpleNamespace(approval_mode="manual"),
        actions=[],
        recovery=None,
    )
    log = [
        _event(1, "p1", "requested", "recap", 0),
        _event(
            2, "p1", "approved", "console-user", 1, "interactive terminal; answered after 1.0 s"
        ),
        _event(3, "p2", "requested", "recap", 10),
        _event(
            4, "p2", "approved", "console-user", 50, "interactive terminal; answered after 40.0 s"
        ),
    ]
    lines = [line for line in export._outcome(record, [], log) if "**Approval:**" in line]
    assert "answered after 1.0 s" in lines[0] and "1.0 s after the request" in lines[0]
    assert "answered after 40.0 s" in lines[1] and "40.0 s after the request" in lines[1]
