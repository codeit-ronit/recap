"""scripts/secret_scan.py: finds key shapes and the owner's own key values, never prints them.

Every key-shaped string here is built at run time, so this file itself stays clean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import secret_scan
from scripts.secret_scan import digest, owner_values, scan_blob, shape_findings

GROQ_SHAPED = "gsk_" + "Q" * 30
GITHUB_SHAPED = "gh" + "p_" + "a1" * 15


def test_a_key_shape_is_found_and_named_without_its_value() -> None:
    text = f'config = {{"key": "{GROQ_SHAPED}"}}\ntoken: {GITHUB_SHAPED}\n'
    found = list(shape_findings(text, "notes.txt"))
    assert {f.kind for f in found} == {"Groq key", "GitHub token"}
    for finding in found:
        assert GROQ_SHAPED not in finding.line() and GITHUB_SHAPED not in finding.line()
        assert finding.line().startswith("notes.txt: ")


def test_a_reviewed_fake_is_allowed_by_its_exact_value_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secret_scan, "KNOWN_FAKES", frozenset({digest(GROQ_SHAPED)}))
    assert list(shape_findings(GROQ_SHAPED, "tests/x.py")) == []
    other = GROQ_SHAPED[:-1] + "R"  # one character different: a different key
    assert len(list(shape_findings(other, "tests/x.py"))) == 1


def test_owner_values_come_from_secret_named_lines_only(tmp_path: Path) -> None:
    secret = "v" * 40
    (tmp_path / "groq.env").write_text(
        "# a comment\n"
        f"export GROQ_API_KEY='{secret}'\n"
        "RECAP_ALLOW_LIVE_FREE_INFERENCE=1\n"  # a flag, not a secret
        "RECAP_GROQ_FREE_PLAN_CONFIRMED_ON=2026-10-21\n"  # a date, not a secret
        "SHORT_TOKEN=abc\n",  # too short to be a key
        encoding="utf-8",
    )
    (tmp_path / "notes.txt").write_text(f"OTHER_KEY={'w' * 40}\n", encoding="utf-8")
    assert owner_values(tmp_path) == [secret.encode()]  # only *.env files are read
    assert owner_values(tmp_path / "missing") == []


def test_an_owner_value_is_found_anywhere_even_in_binary_data() -> None:
    secret = b"x" * 24 + b"-real-looking-value"
    blob = b"\x89PNG\r\n\x1a\n\0\0" + secret + b"\0trailer"
    found = scan_blob(blob, "docs/screenshots/a.png", [secret])
    assert [f.kind for f in found] == ["owner's key value"]
    assert secret.decode() not in found[0].line()


def test_the_tracked_files_hold_no_secret(capsys: pytest.CaptureFixture[str]) -> None:
    # The full history scan runs in CI (scripts/secret_scan.py, with the whole history).
    assert secret_scan.main(["--tree"]) == 0, capsys.readouterr().out
