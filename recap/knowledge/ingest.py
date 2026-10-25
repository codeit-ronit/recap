"""Markdown ingestion: turns runbook files into small, citable chunks.

This is the "indexing" half of RECAP's static RAG. Each `## ` section of a Markdown file
becomes one `DocChunk` with everything a citation needs: a stable chunk ID, the file
path, the document title, the section heading, a checksum of the source file and its
provenance. `KnowledgeRetriever` (retriever.py) then indexes these chunks for search.

Why sections? A runbook section ("Symptoms", "Remediation", ...) is a natural unit of
meaning, and citing "runbook-bad-deployment#remediation-1" tells a reader exactly where
a hint came from. Unusually long sections are split into overlapping word windows so no
single chunk floods a bounded context pack.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path

from recap.context.tokens import estimate_tokens
from recap.core.errors import ConfigError
from recap.core.models import DocChunk

WINDOW_WORDS = 180  # sections longer than this are split into windows
OVERLAP_WORDS = 30  # words shared by consecutive windows, so no sentence is lost at a cut
OVERVIEW_HEADING = "Overview"  # name for the text between the title and the first section

PROVENANCE_KIND = "synthetic runbook authored for RECAP V1"
PROVENANCE_LICENSE = "MIT (this repository)"

_WORD_RE = re.compile(r"\S+")


def load_documents(knowledge_dir: Path, *, root: Path | None = None) -> list[DocChunk]:
    """Read every Markdown file in `knowledge_dir` (sorted by name) into cited chunks.

    With `root` (normally the repo root), paths are stored relative to it, e.g.
    "fixtures/knowledge/runbook-bad-deployment.md". A folder outside `root` (a knowledge
    overlay elsewhere on disk) is shown by its own name, e.g. "redteam-kb/runbook-x.md".
    The output is deterministic.
    """
    knowledge_dir = Path(knowledge_dir)
    if not knowledge_dir.is_dir():
        raise ConfigError(f"knowledge directory not found: {knowledge_dir}")
    shown = _shown_folder(knowledge_dir, root)
    files = sorted((p for p in knowledge_dir.glob("*.md") if p.is_file()), key=lambda p: p.name)
    return [chunk for path in files for chunk in _chunk_file(path, shown)]


def _chunk_file(path: Path, shown: Path) -> list[DocChunk]:
    """All chunks of one Markdown file; `shown` is how its folder appears in citations."""
    raw = path.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()[:12]  # detects edits to the source file
    title, sections = _split_sections(raw.decode("utf-8"), fallback_title=path.stem)
    doc_path = (shown / path.name).as_posix()
    source = shown.as_posix()
    provenance = {"source": source, "kind": PROVENANCE_KIND, "license": PROVENANCE_LICENSE}

    chunks: list[DocChunk] = []
    seen: Counter[str] = Counter()  # numbers repeated slugs: windows and duplicate headings
    for heading, body in sections:
        slug = _slug(heading)
        for window in _split_into_windows(body):
            seen[slug] += 1
            chunks.append(
                DocChunk(
                    chunk_id=f"{path.stem}#{slug}-{seen[slug]}",
                    doc_path=doc_path,
                    title=title,
                    heading=heading,
                    text=window,
                    checksum=checksum,
                    provenance=dict(provenance),
                    token_estimate=estimate_tokens(window),
                )
            )
    return chunks


def _split_sections(markdown: str, fallback_title: str) -> tuple[str, list[tuple[str, str]]]:
    """Split Markdown into its title and (heading, body) pairs at `## ` headings.

    The first `# ` line is the title. Text before the first `## ` heading becomes an
    "Overview" section. Sections with an empty body are dropped (nothing to cite).
    """
    title = fallback_title
    title_found = False
    in_code_block = False
    heading = OVERVIEW_HEADING
    lines: list[str] = []
    sections: list[tuple[str, list[str]]] = []

    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            # Inside a code block "# ..." is a shell comment, not a heading.
            in_code_block = not in_code_block
        elif not in_code_block and line.startswith("## "):
            sections.append((heading, lines))
            heading, lines = line[3:].strip(), []
            continue
        elif not in_code_block and not title_found and line.startswith("# "):
            title, title_found = line[2:].strip(), True
            continue
        lines.append(line)
    sections.append((heading, lines))

    bodies = [(name, "\n".join(body).strip()) for name, body in sections]
    return title, [(name, body) for name, body in bodies if body]


def _split_into_windows(text: str) -> list[str]:
    """Split text longer than WINDOW_WORDS into overlapping windows of words.

    Each window is a slice of the original text, so line breaks survive. That matters:
    hint lines such as "Recommended action: ..." are parsed line by line later on.
    """
    words = list(_WORD_RE.finditer(text))
    if len(words) <= WINDOW_WORDS:
        return [text]
    step = WINDOW_WORDS - OVERLAP_WORDS
    windows: list[str] = []
    start = 0
    while True:
        end = min(start + WINDOW_WORDS, len(words))
        windows.append(text[words[start].start() : words[end - 1].end()])
        if end == len(words):
            return windows
        start += step


def _slug(heading: str) -> str:
    """URL-style section name: 'Rollback hint' -> 'rollback-hint'."""
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-") or "section"


def _shown_folder(folder: Path, root: Path | None) -> Path:
    """How a knowledge folder appears in citations: relative to `root` when it is inside it,
    as given when there is no root, and by its own name when it is elsewhere on disk.

    Only the folder's own name, as the run record shows an overlay: the local folders above
    it can name a person or a project, and citations end up in shared records and exports.
    """
    if root is None:
        return folder
    resolved, base = folder.resolve(), Path(root).resolve()
    return resolved.relative_to(base) if resolved.is_relative_to(base) else Path(resolved.name)
