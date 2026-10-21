"""Files meant to be published (recorded model runs, exported traces) hold no secret.

An independent check on top of the recorder's and the exporter's own cleaning: CI runs it
over every committed recording and export. JSON files are parsed and every string and key
is checked decoded (in JSON text a key after a newline sits behind "\\n" and a word-boundary
pattern would miss it). A failure names the file and the kind of finding, never the value:
the assertions compare booleans on purpose, so pytest cannot print the matched text into a
public CI log.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECORDINGS = ROOT / "fixtures" / "recordings"
EXPORTS = ROOT / "evals" / "live"

_START = r"(?<![A-Za-z0-9])"
SECRET_SHAPES = {
    "Groq key": re.compile(_START + r"gsk_[A-Za-z0-9]{20,}"),
    "OpenAI or Anthropic key": re.compile(
        _START + r"sk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_-]{20,}"
    ),
    "AWS access key": re.compile(_START + r"(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])"),
    "GitHub token": re.compile(
        _START + r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
    ),
    "Slack token": re.compile(_START + r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer token": re.compile(r"(?i)" + _START + r"bearer\s+[A-Za-z0-9._~+/-]{16,}"),
    "provider request ID": re.compile(r"(?i)" + _START + r"req_[0-9a-z]{20,}"),
    "provider answer ID": re.compile(_START + r"chatcmpl-[0-9A-Za-z-]{20,}"),
}
LOCAL_PATHS = ("/users/", "/home/", "/var/folders/", "/private/tmp/", "\\users\\")
LINKING_KEYS = frozenset({"x-request-id", "x_groq", "system_fingerprint", "request_id"})


def published_files() -> list[Path]:
    return sorted(p for d in (RECORDINGS, EXPORTS) for p in d.rglob("*") if p.is_file())


def strings(value: Any) -> Iterator[str]:
    """Every string in a JSON value, dict keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def decoded(path: Path) -> tuple[list[str], set[str]]:
    """(the file's strings, its dict keys); JSON is parsed, anything else is one text."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif path.suffix == ".json":
        values = [json.loads(text)]
    else:
        return [text], set()
    keys = {k.lower() for v in values for k in _keys(v)}
    return [s for v in values for s in strings(v)], keys


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def test_the_published_folders_exist_and_are_not_empty() -> None:
    assert any(p.suffix == ".jsonl" for p in RECORDINGS.glob("*.jsonl"))  # the replay fixture
    exports = [d for d in EXPORTS.iterdir() if d.is_dir()]
    assert exports, "evals/live holds no exported run"
    for folder in exports:  # each export is complete; a model run also has its recording
        names = {p.name for p in folder.iterdir()}
        assert {"trace.jsonl", "run.json", "README.md"} <= names, folder.name
        run = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        if (run.get("provider_info") or {}).get("kind") == "llm":
            assert "recording.jsonl" in names, folder.name


@pytest.mark.parametrize("path", published_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_a_published_file_holds_no_secret_and_no_local_path(path: Path) -> None:
    texts, keys = decoded(path)
    for text in texts:
        for form in {text, unquote(text)}:
            for kind, shape in SECRET_SHAPES.items():
                found = shape.search(form) is not None
                assert not found, f"{path.name} contains a {kind}"
            lowered = form.lower()
            for marker in LOCAL_PATHS:
                found = marker in lowered
                assert not found, f"{path.name} contains a local path ({marker})"
    linking = sorted(keys & LINKING_KEYS)
    assert not linking, f"{path.name} keeps account-linking keys: {linking}"
