"""Markdown ingestion: provenance, stable chunk IDs, windows, and the knowledge fixtures."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from recap.context.tokens import estimate_tokens
from recap.core.config import REPO_ROOT
from recap.core.errors import ConfigError
from recap.core.models import DocChunk
from recap.knowledge.hints import ActionHint, parse_action_hints
from recap.knowledge.ingest import load_documents

KNOWLEDGE_DIR = REPO_ROOT / "fixtures" / "knowledge"
DOC_FILES = sorted(KNOWLEDGE_DIR.glob("*.md"))

EXPECTED_TITLES = {
    "guide-cascading-failures.md": "Guide: cascading failures across services",
    "guide-deployment-pipeline.md": "Guide: deployments and revisions",
    "guide-reading-signals.md": "Guide: reading health signals",
    "policy-change-management.md": "Policy: automated change management",
    "runbook-bad-deployment.md": "Runbook: bad deployment",
    "runbook-memory-pressure.md": "Runbook: memory pressure and crash loops",
    "shop-architecture.md": "ShopSim architecture",
}

# Strings that belong to specific scenarios. Docs must stay generic, never answer keys.
SCENARIO_SPECIFIC = ["PricingClient", "v2.4.1", "LedgerClient", "CouponService"]
SERVICE_NAMES = re.compile(r"\b(frontend|checkout|payment|inventory|cart)\b", re.IGNORECASE)


@pytest.fixture(scope="module")
def chunks() -> list[DocChunk]:
    return load_documents(KNOWLEDGE_DIR, root=REPO_ROOT)


def _write_doc(folder: Path, name: str, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    return folder


# --- the shipped knowledge fixtures --------------------------------------------------------


def test_every_doc_is_loaded_in_name_order_with_its_title(chunks: list[DocChunk]) -> None:
    titles = {Path(chunk.doc_path).name: chunk.title for chunk in chunks}
    assert titles == EXPECTED_TITLES

    names = [Path(chunk.doc_path).name for chunk in chunks]
    assert names == sorted(names)


def test_chunks_carry_provenance_checksum_and_repo_relative_path(chunks: list[DocChunk]) -> None:
    for chunk in chunks:
        source = REPO_ROOT / chunk.doc_path
        assert chunk.doc_path == f"fixtures/knowledge/{source.name}"
        assert chunk.checksum == hashlib.sha256(source.read_bytes()).hexdigest()[:12]
        assert chunk.provenance == {
            "source": "fixtures/knowledge",
            "kind": "synthetic runbook authored for RECAP V1",
            "license": "MIT (this repository)",
        }
        assert chunk.token_estimate == estimate_tokens(chunk.text) > 0


def test_chunk_ids_are_unique_readable_and_stable(chunks: list[DocChunk]) -> None:
    ids = [chunk.chunk_id for chunk in chunks]

    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-z0-9-]+#[a-z0-9-]+-\d+", chunk_id) for chunk_id in ids)
    assert {"runbook-bad-deployment#remediation-1", "shop-architecture#overview-1"} <= set(ids)
    # Same files in, same chunks out: IDs can be cited across runs.
    assert load_documents(KNOWLEDGE_DIR, root=REPO_ROOT) == chunks


def test_remediation_sections_carry_the_hint_lines(chunks: list[DocChunk]) -> None:
    by_id = {chunk.chunk_id: chunk for chunk in chunks}

    rollback = by_id["runbook-bad-deployment#remediation-1"]
    restart = by_id["runbook-memory-pressure#remediation-1"]
    cascading = by_id["guide-cascading-failures#remediation-1"]

    assert parse_action_hints(rollback.text) == [
        ActionHint(mechanism="bad_deployment", action="execute_rollback")
    ]
    assert parse_action_hints(restart.text) == [
        ActionHint(mechanism="resource_pressure", action="restart_service")
    ]
    # A dependency failure is a symptom: the guide names the mechanism but no action.
    assert "Mechanism: dependency_failure" in cascading.text
    assert parse_action_hints(cascading.text) == []


def test_only_the_two_runbooks_recommend_an_action(chunks: list[DocChunk]) -> None:
    hints = {(hint.mechanism, hint.action) for c in chunks for hint in parse_action_hints(c.text)}
    assert hints == {
        ("bad_deployment", "execute_rollback"),
        ("resource_pressure", "restart_service"),
    }


@pytest.mark.parametrize("path", DOC_FILES, ids=lambda path: path.name)
def test_docs_follow_the_knowledge_conventions(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert text.startswith("# ")
    assert len(re.findall(r"^## ", text, flags=re.MULTILINE)) >= 3
    assert 200 <= len(text.split()) <= 500  # concise: a doc is a page, not a manual


@pytest.mark.parametrize("path", DOC_FILES, ids=lambda path: path.name)
def test_docs_are_generic_knowledge_not_answer_keys(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    for needle in SCENARIO_SPECIFIC:
        assert needle not in text
    assert not re.search(r"\bv\d+\.\d+\.\d+\b", text), "docs must not name concrete versions"
    if path.name != "shop-architecture.md":
        assert not SERVICE_NAMES.search(text), "only the architecture doc names services"


# --- parsing rules (small documents written to a temp folder) -----------------------------


def test_intro_text_becomes_an_overview_chunk(tmp_path: Path) -> None:
    folder = _write_doc(tmp_path, "guide.md", "# My guide\n\nIntro words.\n\n## Steps\n\nDo it.\n")

    chunks = load_documents(folder, root=tmp_path)

    assert [(c.chunk_id, c.heading, c.text) for c in chunks] == [
        ("guide#overview-1", "Overview", "Intro words."),
        ("guide#steps-1", "Steps", "Do it."),
    ]
    assert {c.title for c in chunks} == {"My guide"}
    assert chunks[0].doc_path == "guide.md"


def test_missing_title_falls_back_to_the_file_name_and_empty_sections_are_dropped(
    tmp_path: Path,
) -> None:
    folder = _write_doc(tmp_path, "notes.md", "## Empty\n\n## Filled\n\nSome text.\n")

    chunks = load_documents(folder)

    assert [(c.chunk_id, c.title) for c in chunks] == [("notes#filled-1", "notes")]
    # Without a root, the path is kept exactly as it was given.
    assert chunks[0].doc_path == (folder / "notes.md").as_posix()


def test_long_sections_become_overlapping_windows(tmp_path: Path) -> None:
    words = [f"w{i}" for i in range(400)]
    folder = _write_doc(tmp_path, "long.md", "# Long\n\n## Body\n\n" + " ".join(words) + "\n")

    chunks = load_documents(folder)
    windows = [chunk.text.split() for chunk in chunks]

    assert [chunk.chunk_id for chunk in chunks] == ["long#body-1", "long#body-2", "long#body-3"]
    assert all(len(window) <= 180 for window in windows)
    assert windows[0][-30:] == windows[1][:30]  # consecutive windows share 30 words
    assert windows[1][-30:] == windows[2][:30]
    assert windows[0][0] == "w0" and windows[-1][-1] == "w399"  # nothing is lost


def test_windows_keep_line_breaks_so_hint_lines_still_parse(tmp_path: Path) -> None:
    filler = " ".join(f"w{i}" for i in range(170))
    body = f"{filler}\nMechanism: bad_deployment\nRecommended action: execute_rollback\n{filler}"
    folder = _write_doc(tmp_path, "runbook.md", f"# Runbook\n\n## Remediation\n\n{body}\n")

    chunks = load_documents(folder)

    assert len(chunks) > 1
    hint = ActionHint(mechanism="bad_deployment", action="execute_rollback")
    assert any(parse_action_hints(chunk.text) == [hint] for chunk in chunks)


def test_repeated_headings_get_distinct_ids(tmp_path: Path) -> None:
    folder = _write_doc(tmp_path, "t.md", "# T\n\n## Notes\n\none\n\n## Notes\n\ntwo\n")

    assert [c.chunk_id for c in load_documents(folder)] == ["t#notes-1", "t#notes-2"]


def test_headings_inside_code_blocks_are_ignored(tmp_path: Path) -> None:
    text = "## Commands\n\n```bash\n# list pods\n## not a heading\n```\n"
    folder = _write_doc(tmp_path, "cmds.md", text)

    chunks = load_documents(folder)

    assert [(c.chunk_id, c.title) for c in chunks] == [("cmds#commands-1", "cmds")]
    assert "# list pods\n## not a heading" in chunks[0].text


def test_missing_directory_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="knowledge directory not found"):
        load_documents(tmp_path / "missing")
