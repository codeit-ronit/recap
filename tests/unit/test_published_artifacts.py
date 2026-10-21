"""Files meant to be published (recorded model runs, exported traces) hold no secret.

An independent check on top of the recorder's and the exporter's own cleaning: CI runs it
over every committed recording and export. A failure names the file and the kind of
finding, never the value (the assertions compare booleans on purpose, so pytest cannot
print the matched text into a public CI log).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLISHED = (ROOT / "fixtures" / "recordings", ROOT / "evals" / "live")

SECRET_SHAPES = {
    "Groq key": re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),
    "OpenAI or Anthropic key": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}"),
}
# Local paths, and a response header that could link a recording to a provider account.
MARKERS = ("/Users/", "/home/", "/var/folders/", "/private/tmp/", "x-request-id")


def published_files() -> list[Path]:
    return sorted(p for d in PUBLISHED if d.exists() for p in d.rglob("*") if p.is_file())


def test_there_are_published_files_to_check() -> None:
    assert any(p.suffix == ".jsonl" for p in published_files())  # the replay fixture at least


@pytest.mark.parametrize("path", published_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_a_published_file_holds_no_secret_and_no_local_path(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for kind, shape in SECRET_SHAPES.items():
        found = shape.search(text) is not None
        assert not found, f"{path.name} contains a {kind}"
    for marker in MARKERS:
        found = marker in text
        assert not found, f"{path.name} contains {marker!r}"
