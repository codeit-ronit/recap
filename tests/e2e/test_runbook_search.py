"""Agents search the runbooks themselves; a hybrid-retrieval run is recorded and replays offline.

A fake Ollama server answers both the chat model (fixed answers, no reasoning) and the
embedding model, through the real provider, embedder, gateway, MCP server and simulator.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from recap.core.config import REPO_ROOT, Settings
from recap.core.models import IncidentState, RunConfig, RunRecord, SourceType
from recap.knowledge.retriever import searchable_text
from recap.knowledge.vectors import VectorIndex
from recap.providers.factory import REPLAY_ENV, create_embedder, create_provider
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from tests.llm_fakes import FakeOllama, OperatorApproves, ScriptedModel

pytestmark = pytest.mark.e2e

QUERY = "errors began right after a new release went out"


class Searcher(ScriptedModel):
    """ScriptedModel that first searches the runbooks, then investigates as usual."""

    def __init__(self, searches: int = 1) -> None:
        super().__init__()
        # Different queries: an identical search would be dropped as a repeat.
        queries = [QUERY, "pods restarting with out of memory kills"][:searches]
        steps = [[("search_runbooks", {"query": query, "k": 2})] for query in queries]
        self.plan = [*steps, *self.plan]


def stored_vector(chunk_id: str, settings: Settings) -> list[float]:
    """The stored (real) vector of one runbook chunk: a query 'meaning' exactly that chunk."""
    runtime = Runtime(settings)
    chunk = runtime.knowledge.chunk(chunk_id)
    assert chunk is not None
    index = VectorIndex.load(REPO_ROOT / settings.retrieval.embedder.vectors)
    vector = index.vector(searchable_text(chunk))
    assert vector is not None
    return vector


async def live_run(
    settings: Settings, config: RunConfig, fake: FakeOllama
) -> tuple[Runtime, RunRecord]:
    runtime = Runtime(settings)
    mock = httpx.MockTransport(fake.handle)
    provider = create_provider("ollama", runtime.settings, mock_http=mock, ledger=runtime.ledger)
    runtime.make_provider = lambda c: provider  # type: ignore[method-assign]
    runtime.make_embedder = (  # type: ignore[method-assign]
        lambda p: create_embedder(runtime.settings, p, mock_http=mock)
    )
    record = await runtime.run_incident(
        "bad-deployment", config, approval_channel=OperatorApproves(runtime.approvals)
    )
    return runtime, record


async def test_an_agent_searches_the_runbooks_and_reads_what_it_found(settings: Settings) -> None:
    fake = FakeOllama(responder=Searcher())
    runtime, record = await live_run(settings, RunConfig(provider="ollama", use_memory=False), fake)
    assert record.state == IncidentState.RESOLVED, record.error
    events = runtime.events.list_events(record.run_id)

    # The call went through the gateway's read path like any tool, answered by RECAP itself.
    calls = [e.payload for e in events if e.category == C.TOOL_CALL]
    search = next(c for c in calls if c["tool"] == "search_runbooks")
    assert search["caller"] == "investigator" and search["args"]["query"] == QUERY
    result = next(
        e.payload
        for e in events
        if e.category == C.TOOL_RESULT and e.payload["invocation_id"] == search["invocation_id"]
    )
    receipt = next(e for e in runtime.runs.evidence(record.run_id) if e.id == result["evidence_id"])
    assert receipt.source_type == SourceType.DOC  # never a live observation for the verifier
    assert receipt.source_ref.startswith("local:search_runbooks(k=2,query=#")  # no free text
    found = [r["chunk_id"] for r in receipt.data["results"]]
    assert len(found) == 2

    # It did not use the environment's tool-call budget; it used its own.
    mcp_calls = [c for c in calls if c["tool"] != "search_runbooks"]
    assert record.usage.tool_calls == len(mcp_calls) + 1  # the finding counts every call
    assert record.retrieval is not None and record.retrieval.agent_searches == 1
    retrievals = [e for e in events if e.category == C.RETRIEVAL and e.payload["stage"] == "agent"]
    assert [e.agent for e in retrievals] == ["investigator"]

    # The next prompt carries the sections it found, and the finding cites them.
    step_two = fake.chats[1]["messages"][1]["content"]
    assert all(f'id="{chunk_id}"' in step_two for chunk_id in found)
    (finding,) = record.findings
    assert set(found) <= set(finding.doc_refs)
    # The receipt supports nothing: the verifier's basis holds tool evidence only.
    assert record.verification is not None
    basis = {i for c in record.verification.checks for i in c.basis_evidence_ids}
    assert receipt.id not in basis


async def test_the_search_budget_refuses_more_searches_without_stopping_the_run(
    settings: Settings,
) -> None:
    retrieval = settings.retrieval.model_copy(
        update={
            "agent_search": settings.retrieval.agent_search.model_copy(update={"max_per_run": 1})
        }
    )
    limited = settings.model_copy(update={"retrieval": retrieval})
    fake = FakeOllama(responder=Searcher(searches=2))
    runtime, record = await live_run(limited, RunConfig(provider="ollama", use_memory=False), fake)
    assert record.state == IncidentState.RESOLVED, record.error
    results = [
        e.payload
        for e in runtime.events.list_events(record.run_id)
        if e.category == C.TOOL_RESULT and not e.payload["ok"]
    ]
    assert [r["error"] for r in results] == ["the runbook search budget is used up (1 per run)"]


async def test_a_hybrid_run_is_recorded_with_its_embeddings_and_replays_offline(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = "runbook-bad-deployment#diagnosis-1"
    meaning = stored_vector(target, settings)
    fake = FakeOllama(responder=Searcher(), embed_vector=lambda text: meaning)
    hybrid = RunConfig(provider="ollama", use_memory=False, retrieval="hybrid")
    runtime, live = await live_run(settings, hybrid, fake)
    assert live.state == IncidentState.RESOLVED, live.error

    info = live.retrieval
    assert info is not None and (info.mode, info.embedder) == ("hybrid", "ollama:nomic-embed-text")
    assert info.embedding_requests == len(fake.embeds) >= 2  # the alert query and the search
    assert info.replayed_requests == 0 and info.embedding_input_tokens
    assert info.server is not None and info.server["weights"] == "sha256:" + "e3be" * 16
    assert target in [d.chunk.chunk_id for d in live.retrieved_docs]  # the dense ranking at work
    assert all(d.retriever == "hybrid-bm25-embeddings" for d in live.retrieved_docs)

    # The recording holds the embedding exchanges, trimmed like every Ollama answer.
    assert live.provider_info is not None and live.provider_info.recording
    recording = settings.paths.root / live.provider_info.recording
    lines = [json.loads(line) for line in recording.read_text().splitlines()]
    embeds = [x for x in lines if x.get("path") == "/api/embed"]
    assert len(embeds) == info.embedding_requests
    assert all(
        set(x["response"]["body"]) <= {"model", "embeddings", "prompt_eval_count"} for x in embeds
    )
    assert "LICENSE" not in recording.read_text() and str(Path.home()) not in recording.read_text()

    # Replayed: the same retrieval and the same decisions, with no model and no network.
    monkeypatch.setenv(REPLAY_ENV, str(recording))
    replay_runtime = Runtime(settings)
    replay = await replay_runtime.run_incident(
        "bad-deployment",
        hybrid.model_copy(update={"provider": "replay"}),
        approval_channel=OperatorApproves(replay_runtime.approvals),
    )
    assert replay.state == IncidentState.RESOLVED, replay.error
    assert replay.retrieval is not None
    assert replay.retrieval.replayed_requests == replay.retrieval.embedding_requests == len(embeds)
    assert [d.chunk.chunk_id for d in replay.retrieved_docs] == [
        d.chunk.chunk_id for d in live.retrieved_docs
    ]


async def test_hybrid_retrieval_without_complete_vectors_fails_loudly(settings: Settings) -> None:
    embedder = settings.retrieval.embedder.model_copy(update={"vectors": "missing/vectors.json"})
    retrieval = settings.retrieval.model_copy(update={"embedder": embedder})
    broken = settings.model_copy(update={"retrieval": retrieval})
    fake = FakeOllama(responder=ScriptedModel())
    _, record = await live_run(
        broken, RunConfig(provider="ollama", use_memory=False, retrieval="hybrid"), fake
    )
    assert record.state == IncidentState.FAILED
    assert record.error is not None and "scripts/build_vector_index.py" in record.error
    assert fake.chats == [] and fake.embeds == []  # it stopped before asking anything


async def test_the_model_token_budget_stops_the_run_before_an_over_budget_request(
    settings: Settings,
) -> None:
    """Each answer reports 3,000 prompt tokens; the run may spend 5,000 in all."""
    from recap.core.models import RunBudget

    fake = FakeOllama(responder=ScriptedModel(), prompt_tokens=3_000)
    tight = RunConfig(provider="ollama", use_memory=False, budget=RunBudget(max_model_tokens=5_000))
    runtime, record = await live_run(settings, tight, fake)
    assert record.state == IncidentState.BUDGET_EXHAUSTED
    assert "max_model_tokens" in record.outcome_note
    assert len(fake.chats) == 1  # the request that would overshoot was never sent
    [summary] = [
        e for e in runtime.events.list_events(record.run_id) if e.category == C.RUN_SUMMARY
    ]
    assert summary.payload["budget"]["model_tokens"] == 3_080  # the server's own counts
