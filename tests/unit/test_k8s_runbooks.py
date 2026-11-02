"""The Kubernetes runbooks: a knowledge overlay for the local Kind target (M5).

fixtures/knowledge-overlays/kubernetes/ holds general operational runbooks (rollouts and
crash loops, readiness and memory, dependency failures, rollback or restart or a person,
cluster text as untrusted data). They load next to the shipped runbooks, never inside
fixtures/knowledge/, so the simulator's index and its stored vectors stay exactly as they
are. They are reading material, not answer keys: no release version, no service, and no
"Recommended action:" line, so the scripted policy derives its fixes exactly as before.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from recap.core.config import REPO_ROOT, Settings, load_settings
from recap.core.models import DocChunk
from recap.knowledge.hints import parse_action_hints
from recap.knowledge.ingest import WINDOW_WORDS, load_documents
from recap.knowledge.retriever import KnowledgeRetriever, searchable_text
from recap.knowledge.vectors import VectorIndex
from recap.runtime import Runtime
from recap.safety.injection import scan_text

KNOWLEDGE_DIR = REPO_ROOT / "fixtures" / "knowledge"
OVERLAYS = REPO_ROOT / "fixtures" / "knowledge-overlays"
K8S = OVERLAYS / "kubernetes"
POISONED = OVERLAYS / "poisoned"
K8S_FILES = sorted(K8S.glob("*.md"))
SHOWN = "fixtures/knowledge-overlays/kubernetes"

EXPECTED_TITLES = {
    "guide-k8s-dependency-failures.md": "Guide: Kubernetes failures that come from a dependency",
    "guide-k8s-rollback-or-restart.md": "Guide: rollback, rolling restart, or a person",
    "policy-k8s-untrusted-cluster-text.md": "Policy: cluster text is untrusted data",
    "runbook-k8s-crash-loops-and-rollouts.md": (
        "Runbook: Kubernetes rollouts that stall and pods that crash loop"
    ),
    "runbook-k8s-readiness-and-memory.md": (
        "Runbook: Kubernetes readiness failures and memory pressure"
    ),
}
# The shipped runbooks' fixed remediation hints (tests/unit/test_knowledge_ingest.py).
SHIPPED_HINTS = {("bad_deployment", "execute_rollback"), ("resource_pressure", "restart_service")}


@pytest.fixture(scope="module")
def default_index() -> KnowledgeRetriever:
    return KnowledgeRetriever.from_directory(KNOWLEDGE_DIR, root=REPO_ROOT)


@pytest.fixture(scope="module")
def with_overlay() -> KnowledgeRetriever:
    return KnowledgeRetriever.from_directory(KNOWLEDGE_DIR, root=REPO_ROOT, overlays=[K8S])


def overlay_chunks(index: KnowledgeRetriever) -> list[DocChunk]:
    return [chunk for chunk in index.chunks if chunk.doc_path.startswith(f"{SHOWN}/")]


# --- loading: next to the shipped runbooks, with no ID clash -----------------------------------


def test_the_overlay_loads_with_the_shipped_runbooks_without_an_id_clash(
    with_overlay: KnowledgeRetriever, default_index: KnowledgeRetriever
) -> None:
    added = overlay_chunks(with_overlay)
    assert {Path(c.doc_path).name: c.title for c in added} == EXPECTED_TITLES
    assert len(with_overlay.chunks) == len(default_index.chunks) + len(added)
    ids = [chunk.chunk_id for chunk in with_overlay.chunks]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-z0-9-]+#[a-z0-9-]+-\d+", chunk.chunk_id) for chunk in added)
    for chunk in added:  # cited like a shipped runbook: repo-relative, with provenance
        assert chunk.provenance["source"] == SHOWN
        assert chunk.chunk_id.split("#")[0] == Path(chunk.doc_path).stem
    # File names are chunk IDs: none may repeat a shipped runbook's or another overlay's.
    names = {path.name for path in K8S_FILES}
    assert not names & {path.name for path in KNOWLEDGE_DIR.glob("*.md")}
    assert not names & {path.name for path in POISONED.glob("*.md")}


def test_it_loads_together_with_the_red_team_overlay() -> None:
    both = KnowledgeRetriever.from_directory(
        KNOWLEDGE_DIR, root=REPO_ROOT, overlays=[K8S, POISONED]
    )
    sources = {chunk.provenance["source"] for chunk in both.chunks}
    assert sources == {"fixtures/knowledge", SHOWN, "fixtures/knowledge-overlays/poisoned"}


def test_the_overlay_is_switched_on_like_any_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RECAP_KNOWLEDGE_OVERLAYS today; a target profile can list the same folder."""
    monkeypatch.setenv("RECAP_KNOWLEDGE_OVERLAYS", SHOWN)
    switched = load_settings(data_dir=tmp_path / "data")
    assert switched.paths.knowledge_overlays == [K8S.resolve()]
    runtime = Runtime(switched)
    assert {Path(c.doc_path).name for c in overlay_chunks(runtime.knowledge)} == set(
        EXPECTED_TITLES
    )

    monkeypatch.delenv("RECAP_KNOWLEDGE_OVERLAYS")
    default = load_settings(data_dir=tmp_path / "data")
    assert default.paths.knowledge_overlays == []
    assert overlay_chunks(Runtime(default).knowledge) == []


@pytest.mark.parametrize("path", K8S_FILES, ids=lambda path: path.name)
def test_the_runbooks_follow_the_shipped_format(path: Path) -> None:
    """A title, at least three sections, a page long, and one citable chunk per section."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith(("# Runbook: ", "# Guide: ", "# Policy: "))
    assert len(re.findall(r"^## ", text, flags=re.MULTILINE)) >= 3
    assert 200 <= len(text.split()) <= 500
    chunks = load_documents(K8S, root=REPO_ROOT)
    own = [c for c in chunks if c.doc_path.endswith(f"/{path.name}")]
    assert all(len(c.text.split()) <= WINDOW_WORDS for c in own)  # never split into windows
    assert all(c.chunk_id.endswith("-1") for c in own)


# --- the default index is unchanged ------------------------------------------------------------


def test_without_the_overlay_the_index_and_its_vectors_are_unchanged(
    default_index: KnowledgeRetriever, with_overlay: KnowledgeRetriever, settings: Settings
) -> None:
    assert overlay_chunks(default_index) == []
    assert {c.provenance["source"] for c in default_index.chunks} == {"fixtures/knowledge"}
    # Every shipped section still has its stored vector, and no vector is left over: the
    # vector file is keyed by each section's exact text, so this is a byte-level check.
    stored = VectorIndex.load(REPO_ROOT / settings.retrieval.embedder.vectors)
    texts = {searchable_text(chunk) for chunk in default_index.chunks}
    assert all(stored.vector(text) is not None for text in texts)
    assert len(stored) == len(texts)
    # The overlay has no stored vectors: with it on, a run searches with BM25 only.
    hybrid = KnowledgeRetriever.from_directory(
        KNOWLEDGE_DIR,
        root=REPO_ROOT,
        vectors_path=REPO_ROOT / settings.retrieval.embedder.vectors,
        overlays=[K8S],
    )
    assert not hybrid.hybrid_ready
    assert set(hybrid.dense_missing) == {c.chunk_id for c in overlay_chunks(with_overlay)}


# --- retrieval: a Kubernetes question finds them with BM25 -------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "pod stuck in ImagePullBackOff after a new release",
            "runbook-k8s-crash-loops-and-rollouts",
        ),
        ("rollout stuck ProgressDeadlineExceeded", "runbook-k8s-crash-loops-and-rollouts"),
        (
            "readiness probe failing Unhealthy events pods not ready",
            "runbook-k8s-readiness-and-memory",
        ),
        (
            "rpc to a dependency failed: which service do I investigate",
            "guide-k8s-dependency-failures",
        ),
        ("when is neither a rollback nor a restart safe", "guide-k8s-rollback-or-restart"),
        (
            "log lines events and annotations are untrusted data",
            "policy-k8s-untrusted-cluster-text",
        ),
    ],
)
def test_a_kubernetes_question_finds_an_overlay_runbook_with_bm25(
    with_overlay: KnowledgeRetriever, default_index: KnowledgeRetriever, query: str, expected: str
) -> None:
    [top] = with_overlay.search(query, k=1)
    assert top.chunk.doc_path == f"{SHOWN}/{expected}.md"
    assert (top.retriever, with_overlay.retriever_for(None)) == ("bm25", "bm25")
    assert not any(SHOWN in hit.chunk.doc_path for hit in default_index.search(query, k=10))


def test_a_kind_alert_brings_kubernetes_runbooks_into_the_first_search(
    with_overlay: KnowledgeRetriever, settings: Settings
) -> None:
    """The controller's first search is the alerted service plus its symptoms; worded here as
    the Kind probe words them (recap/environments/kubernetes/probe.py), for a made-up service."""
    symptoms = [
        "api: HTTP error rate 0.40 over 20 probe requests (first failure: HTTP 500)",
        "api: 0 of 2 replicas ready (readiness failing)",
        "api: a container is in CrashLoopBackOff (crash loop)",
        "api: its rollout exceeded the progress deadline (unavailable)",
    ]
    found = with_overlay.search(" ".join(["api", *symptoms]), k=settings.retrieval.k)
    assert any(hit.chunk.doc_path.startswith(f"{SHOWN}/") for hit in found)


# --- they change what a run reads, never which fix the scripted policy derives ------------------


def test_the_overlay_recommends_no_action(with_overlay: KnowledgeRetriever) -> None:
    assert [c.chunk_id for c in overlay_chunks(with_overlay) if parse_action_hints(c.text)] == []
    hints = {
        (h.mechanism, h.action) for c in with_overlay.chunks for h in parse_action_hints(c.text)
    }
    assert hints == SHIPPED_HINTS


@pytest.mark.parametrize(
    ("mechanism", "chunk_id"),
    [
        ("bad_deployment", "runbook-bad-deployment#remediation-1"),
        ("resource_pressure", "runbook-memory-pressure#remediation-1"),
    ],
)
def test_the_proposal_search_still_finds_the_shipped_hint_first(
    with_overlay: KnowledgeRetriever, mechanism: str, chunk_id: str
) -> None:
    """The controller's proposal-stage query (k=2) must keep finding the hint it acts on:
    an overlay chunk ranked above it would leave the scripted policy a blind restart."""
    query = f"{mechanism.replace('_', ' ')} remediation recommended action runbook"
    results = with_overlay.search(query, k=2)
    assert results[0].chunk.chunk_id == chunk_id
    assert results[0].score > 1.3 * results[1].score  # with a margin, not by a hair


def test_the_overlay_is_not_flagged_as_a_prompt_injection(with_overlay: KnowledgeRetriever) -> None:
    """Retrieved runbooks are scanned like any untrusted text, and a red-team planner acts on a
    flagged destructive instruction: these runbooks describe such lines without quoting one."""
    flagged = {
        c.chunk_id: scan_text(f"{c.heading}\n{c.text}")
        for c in overlay_chunks(with_overlay)
        if scan_text(f"{c.heading}\n{c.text}")
    }
    assert flagged == {}


# --- no answer keys ---------------------------------------------------------------------------

VERSION = re.compile(r"(?<![\w.])v?\d+\.\d+\.\d+(?![\w.])", re.IGNORECASE)
VERDICT = re.compile(
    r"\b(bad|broken|faulty|defect\w*|buggy|culprit|cause[sd]?|guilty|suspect|victim|"
    r"roll(?:ed|s|ing)?[ -]?back|rollback|restart\w*|revert\w*|fix\w*)\b",
    re.IGNORECASE,
)
# What the Kind demo's scenario files hold (fixtures/kind-scenarios/): its services, its
# releases and its defects. These constants are a floor; the files add whatever they name.
DEMO_SERVICES = {"frontend", "backend"}
DEMO_VERSIONS = {"v1.0.0", "v1.1.0", "v1.1.1", "v1.2.0"}
DEMO_DETAILS = {"recap-demo", "recap-m5", "KeyError", "'region'", "/report", "/items"}
DEMO_SCENARIOS = {
    "kind-bad-release",
    "kind-dependency-crash",
    "kind-injected-logs",
    "kind-report-leak",
}


def demo_labels() -> tuple[set[str], set[str], set[str]]:
    """(services, versions, other identifiers) of the Kind demo scenarios."""
    services, versions = set(DEMO_SERVICES), set(DEMO_VERSIONS)
    details = DEMO_DETAILS | DEMO_SCENARIOS
    for path in sorted((REPO_ROOT / "fixtures" / "kind-scenarios").glob("*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        details.add(str(spec.get("id", path.stem)))
        services |= set((spec.get("baseline") or {}).keys())
        versions |= {str(v) for v in (spec.get("baseline") or {}).values()}
        for fault in spec.get("faults") or []:
            services.add(str(fault.get("service", "")))
            versions.add(str(fault.get("version", "")))
        expected = spec.get("expected") or {}
        services.add(str(expected.get("root_cause_service", "")))
        versions.add(str((expected.get("fix_params") or {}).get("to_version", "")))
        services.add(str((spec.get("alert") or {}).get("service", "")))
    return services - {""}, versions - {""}, details - {""}


def answer_key_findings(text: str) -> list[str]:
    """Why a runbook would hand a demo scenario its answer (empty when it does not): any
    release version, a demo service named in one sentence with a verdict, a demo detail."""
    services, versions, details = demo_labels()
    findings = [f"names the version {m.group(0)}" for m in VERSION.finditer(text)]
    findings += [f"names the demo version {v}" for v in sorted(versions) if v in text]
    findings += [f"names the demo detail {d!r}" for d in sorted(details) if d in text]
    flat = " ".join(text.split())
    for sentence in re.split(r"(?<=[.!?;:])\s+", flat):
        named = [s for s in sorted(services) if re.search(rf"\b{re.escape(s)}\b", sentence, re.I)]
        verdict = VERDICT.search(sentence)
        if named and verdict:
            findings.append(f"pairs {named[0]} with {verdict.group(0)!r}: {sentence!r}")
    return findings


@pytest.mark.parametrize("path", K8S_FILES, ids=lambda path: path.name)
def test_no_runbook_names_a_demo_version_or_pairs_a_demo_service_with_a_verdict(
    path: Path,
) -> None:
    assert answer_key_findings(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("path", K8S_FILES, ids=lambda path: path.name)
def test_the_runbooks_name_no_service_at_all(path: Path) -> None:
    """Stricter than the guard, as for the shipped runbooks: only an architecture document
    may name services, and these are general runbooks."""
    services, _, _ = demo_labels()
    shop = {"checkout", "payment", "inventory", "cart"}  # the simulator's services
    text = path.read_text(encoding="utf-8")
    assert [s for s in sorted(services | shop) if re.search(rf"\b{s}\b", text, re.I)] == []


@pytest.mark.parametrize(
    "leak",
    [
        "Roll back backend to its previous release.",
        "backend's release is bad.",
        "When frontend errors, the cause is\nbackend.",  # across a line break
        "Fix: frontend is a victim; backend is the culprit.",
        "Restart backend when its memory is high",
        "The image tag v1.1.0 is broken.",
        "Prefer 1.0.0 when in doubt.",
        "A KeyError for 'region' means the new build.",
        "The kind-report-leak case needs a restart.",
    ],
)
def test_the_guard_catches_an_answer_key(leak: str) -> None:
    assert answer_key_findings(f"# Runbook: x\n\n## Notes\n\n{leak}\n")


@pytest.mark.parametrize(
    "general",
    [
        "Fix the dependency, not the caller.",
        "A crash loop right after a release makes that release a suspect.",
        "Roll back only to a release the evidence names as healthy.",
        "`propose_rollback` names the release a rollback would restore.",
    ],
)
def test_the_guard_lets_general_advice_through(general: str) -> None:
    assert answer_key_findings(general) == []


def test_the_folder_holds_only_the_runbooks() -> None:
    """Every Markdown file in the folder is indexed (a README there would become a runbook)."""
    listed = sorted(path.name for path in K8S.iterdir() if not path.name.startswith("."))
    assert listed == sorted(EXPECTED_TITLES)
