"""RECAP HTTP API (FastAPI).

Start it with:  uvicorn apps.api.main:app --reload

Every endpoint reads real data produced by the runtime (runs, trace events, evidence,
approvals, memory, knowledge, evaluation reports). Incidents run against the environment
chosen in configs/environment.yaml (the SIMULATED local environment in V1); /health
labels which kind it is.

The read endpoints return typed, versioned contracts (apps/api/dto.py, schema
"recap-api-v1", ADR 0027), built by apps/api/views.py from the persisted run records,
stored traces and the oracle's verdicts; GET /openapi.json documents every field.

No endpoint executes an action directly. Clients can start simulated runs (optionally with
the simulation auto-approver, a provider the owner allows per run, or a replay of a
committed recording) and evaluations, and approve or reject one exact proposal; the
controller executes approved actions through the safety gateway.

Security (audit finding 2.4, see apps/api/auth.py): every state-changing endpoint needs the
operator's bearer token, and the approver recorded is the authenticated operator, never a
client-supplied name. Only requests for this machine's loopback host names are served, and
unsafe requests from a browser page on another origin are refused. No response carries a
token, a key, a signed capability or a local file path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from apps.api.auth import (
    UNSAFE_METHODS,
    Operator,
    OperatorAuth,
    allowed_hosts_from_environment,
    origin_allowed,
)
from apps.api.dto import (
    API_SCHEMA,
    SCHEMA_HEADER,
    TOTAL_HEADER,
    BatchView,
    EvalsView,
    EvidenceView,
    ProviderView,
    ReplaysView,
    RetrievalView,
    RunDetail,
    RunStarted,
    RunSummary,
    SystemView,
    TimelineFilters,
    TimelineKind,
    TimelineView,
    UsageView,
)
from apps.api.service import RunManager, RunReader, RunRefused, check_run_request
from apps.api.views import RunFilters, environment_label
from recap import __version__
from recap.core.errors import ApprovalError, ConfigError
from recap.core.models import (
    Approval,
    IncidentState,
    MemoryPartition,
    ProviderOrigin,
    RunConfig,
)
from recap.knowledge.hints import parse_action_hints
from recap.providers.catalog import provider_status
from recap.runtime import Runtime
from recap.safety.injection import injection_flags

DASHBOARD_DIST = Path(__file__).resolve().parents[1] / "dashboard" / "dist"


class RunRequest(BaseModel):
    scenario_id: str
    config: RunConfig = Field(default_factory=RunConfig)


class ApprovalRequestBody(BaseModel):
    """A decision on one exact proposal. Who decided comes from the credential, not the body."""

    model_config = ConfigDict(extra="forbid")  # e.g. an "actor" field is refused, not ignored

    decision: Literal["approved", "rejected"]
    proposal_digest: str  # must match the exact proposal the human reviewed


class EvalRequest(BaseModel):
    configs: list[str] | None = None


def require_operator(request: Request) -> Operator:
    """Dependency of every state-changing endpoint: the caller must be the operator (else 401)."""
    auth: OperatorAuth = request.app.state.auth
    return auth.check(request)


# `who: OperatorRequired` in an endpoint's parameters makes it operator-only.
OperatorRequired = Annotated[Operator, Depends(require_operator)]


def create_app(
    runtime: Runtime | None = None,
    *,
    auth: OperatorAuth | None = None,
    allowed_hosts: list[str] | None = None,
) -> FastAPI:
    """Build the API. Tests pass their own Runtime (temporary data directory).

    `auth` defaults to the operator token from the environment or the data directory (see
    apps/api/auth.py); `allowed_hosts` defaults to RECAP_ALLOWED_HOSTS or the loopback names.
    """
    hosts = allowed_hosts or allowed_hosts_from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.manager = RunManager(runtime)
        app.state.reader = RunReader(app.state.manager)
        app.state.auth = auth or OperatorAuth.from_environment(
            app.state.manager.runtime.settings.paths.data_dir
        )
        yield

    app = FastAPI(
        title="RECAP API",
        version=__version__,
        description=f"RECAP backend. Read contracts: schema {API_SCHEMA} (apps/api/dto.py).",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[SCHEMA_HEADER, TOTAL_HEADER],
    )

    @app.middleware("http")
    async def refuse_foreign_origins(request: Request, call_next: Any) -> Response:
        """A browser page on another host may not change anything here (anti CSRF)."""
        if request.method in UNSAFE_METHODS and not origin_allowed(
            request.headers.get("origin"), hosts
        ):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def name_the_schema(request: Request, call_next: Any) -> Response:
        """Every response names the API schema it follows (JSON arrays cannot carry a field)."""
        response: Response = await call_next(request)
        response.headers[SCHEMA_HEADER] = API_SCHEMA
        return response

    # Outermost: a request addressed to any other host name (DNS rebinding) gets 400.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    def manager(request: Request) -> RunManager:
        return request.app.state.manager

    def reader(request: Request) -> RunReader:
        return request.app.state.reader

    def clean(request: Request, value: Any) -> Any:
        """A response body without any local folder (see views.LocalPaths)."""
        return reader(request).paths.scrub(value)

    def found(body: Any | None, what: str) -> Any:
        if body is None:
            raise HTTPException(404, f"unknown {what}")
        return body

    # --- session ----------------------------------------------------------------------------

    @app.get("/auth/session")
    def session(who: OperatorRequired) -> dict[str, str]:
        """Who the token belongs to (the dashboard's sign-in check)."""
        return {"operator": who.name, "actor": who.actor}

    # --- system ---------------------------------------------------------------------------

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        rt = manager(request).runtime
        return {
            "status": "ok",
            "version": __version__,
            "environment": rt.settings.environment.kind,
            "provider": rt.settings.providers.default,
            "label": environment_label(rt.environment_info()),
        }

    @app.get("/system", response_model=SystemView)
    def system(request: Request) -> Any:
        """Which implementation is plugged into each interface, its real status (a stub is
        "planned", never "active"), feature flags and versions."""
        return reader(request).system()

    @app.get("/providers", response_model=list[ProviderView])
    async def providers(request: Request) -> Any:
        """Every configured provider, whether it could run now and whether a client may pick
        it for one run (no hosted calls, no inference; a key is reported only as present)."""
        rt = manager(request).runtime
        return reader(request).providers(await provider_status(rt.settings, rt.ledger))

    @app.get("/scenarios")
    def scenarios(request: Request) -> list[dict[str, Any]]:
        scenarios = manager(request).runtime.list_scenarios()
        return clean(request, [s.model_dump(mode="json") for s in scenarios])

    @app.get("/replays", response_model=ReplaysView)
    def replays(request: Request) -> Any:
        """The committed recordings of real model runs, and whether each can be replayed."""
        return reader(request).replays()

    # --- incident runs ----------------------------------------------------------------------

    @app.post("/incidents/run", status_code=202, response_model=RunStarted)
    async def run_incident(body: RunRequest, request: Request, who: OperatorRequired) -> Any:
        # async: starting a background task needs the server's event loop (sync endpoints
        # run in a worker thread that has none).
        mgr = manager(request)
        try:
            config = check_run_request(
                mgr.runtime, body.scenario_id, body.config, manifest=reader(request).manifest
            )
        except RunRefused as refused:
            raise HTTPException(refused.status, refused.detail) from None
        run_id = mgr.start_run(body.scenario_id, config)
        return reader(request).run_started(run_id, body.scenario_id, config)

    @app.get(
        "/runs",
        response_model=list[RunSummary],
        responses={200: {"headers": {TOTAL_HEADER: {"description": "runs matching the filters"}}}},
    )
    def list_runs(
        request: Request,
        response: Response,
        limit: int = Query(50, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        provider_class: ProviderOrigin | None = None,
        provider: str | None = Query(None, description="provider id or model name"),
        scenario: str | None = None,
        state: IncidentState | None = None,
        environment: str | None = Query(None, description="environment kind"),
        label: str | None = None,
    ) -> Any:
        """Persisted runs, newest first. The total of matching runs is in X-Total-Count."""
        filters = RunFilters(
            provider_class=provider_class,
            provider=provider,
            scenario=scenario,
            state=state.value if state else None,
            environment=environment,
            label=label,
        )
        page, total = reader(request).runs(filters, offset, limit)
        response.headers[TOTAL_HEADER] = str(total)
        return page

    @app.get("/runs/{run_id}", response_model=RunDetail)
    def get_run(run_id: str, request: Request) -> Any:
        """The run's summary, plus its stored record and evidence (the dashboard reads both)."""
        return found(reader(request).run_detail(run_id), f"run '{run_id}'")

    @app.get("/runs/{run_id}/timeline", response_model=TimelineView)
    def run_timeline(
        run_id: str,
        request: Request,
        after_seq: int = Query(0, ge=0),
        limit: int = Query(500, ge=1, le=2000),
        agent: str | None = None,
        round: int | None = Query(None, ge=1),
        kind: TimelineKind | None = None,
        tool: str | None = None,
    ) -> Any:
        """One page of the run's trace, its agent lanes and rounds (resume with after_seq)."""
        filters = TimelineFilters(agent=agent, round=round, kind=kind, tool=tool)
        view = reader(request).timeline(run_id, after_seq=after_seq, limit=limit, filters=filters)
        return found(view, f"run '{run_id}'")

    @app.get("/runs/{run_id}/evidence", response_model=EvidenceView)
    def run_evidence(run_id: str, request: Request) -> Any:
        """The evidence registry with trust classes, every claim and every verdict on it."""
        return found(reader(request).evidence(run_id), f"run '{run_id}'")

    @app.get("/runs/{run_id}/retrieval", response_model=RetrievalView)
    def run_retrieval(run_id: str, request: Request) -> Any:
        """Runbook searches, citations, memory hits and reuse verdicts, as recorded."""
        return found(reader(request).retrieval(run_id), f"run '{run_id}'")

    @app.get("/runs/{run_id}/usage", response_model=UsageView)
    def run_usage(run_id: str, request: Request) -> Any:
        """Model requests, reported tokens, RECAP's estimates (labeled) and time, per model."""
        return found(reader(request).usage(run_id), f"run '{run_id}'")

    @app.post("/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str, request: Request, who: OperatorRequired) -> dict[str, str]:
        """Stop a run this server is running. A pending approval request expires with it."""
        mgr = manager(request)
        if mgr.status(run_id) == "unknown":
            raise HTTPException(404, f"unknown run '{run_id}'")
        if not mgr.cancel(run_id):
            raise HTTPException(409, f"run '{run_id}' is not running in this server")
        return {"run_id": run_id, "status": "cancelling"}

    @app.get("/runs/{run_id}/events")
    def run_events(run_id: str, request: Request, after_seq: int = 0) -> list[dict[str, Any]]:
        events = manager(request).runtime.events.list_events(run_id, after_seq)
        return clean(request, [e.model_dump(mode="json") for e in events])

    @app.get("/runs/{run_id}/stream")
    async def run_stream(
        run_id: str,
        request: Request,
        after_seq: int = Query(0, ge=0),
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        """Server-sent events: stored events, then live ones if this server is running the run.

        Every event's `id` is its seq. A reconnecting client sends the last one it received
        as Last-Event-ID (EventSource does so by itself), or passes after_seq, and the
        stream resumes right after it, so no event is sent twice.
        """
        resume = max(after_seq, _event_id(last_event_id))
        return StreamingResponse(
            manager(request).stream(run_id, resume),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- approvals --------------------------------------------------------------------------

    @app.get("/approvals/pending")
    def pending_approvals(request: Request) -> list[dict[str, Any]]:
        pending = manager(request).runtime.approvals.pending()
        return clean(request, [r.model_dump(mode="json") for r in pending])

    @app.get("/approvals/events")
    def recent_approval_events(request: Request, limit: int = 50) -> list[dict[str, Any]]:
        """The newest entries of the append-only decision log, across all proposals."""
        events = manager(request).runtime.approvals.recent_events(max(1, min(limit, 500)))
        return clean(request, [e.model_dump(mode="json") for e in events])

    @app.get("/approvals/{proposal_id}/events")
    def approval_events(proposal_id: str, request: Request) -> list[dict[str, Any]]:
        """The full decision log of one proposal: requested, decided, used or refused."""
        events = manager(request).runtime.approvals.events(proposal_id)
        return clean(request, [e.model_dump(mode="json") for e in events])

    @app.post("/approvals/{proposal_id}")
    def decide_approval(
        proposal_id: str,
        body: ApprovalRequestBody,
        request: Request,
        who: OperatorRequired,
    ) -> dict[str, Any]:
        """Approve or reject ONE proposal. The digest must match exactly; approvals expire.

        A rejection also revokes an approval that was not used yet: "no" wins until the
        gateway uses the approval, right before it sends the write. The approver recorded is
        the authenticated operator.
        """
        approvals = manager(request).runtime.approvals
        if approvals.get_request(proposal_id) is None:
            raise HTTPException(404, f"no approval request for proposal '{proposal_id}'")
        try:
            approval: Approval = approvals.decide(
                proposal_id,
                decision=body.decision,
                proposal_digest=body.proposal_digest,
                actor=who.actor,
            )
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc
        return approval.model_dump(mode="json")

    # --- memory and knowledge -----------------------------------------------------------------

    @app.get("/memory")
    def memory(request: Request, partition: MemoryPartition | None = None) -> list[dict[str, Any]]:
        episodes = manager(request).runtime.memory.list([partition] if partition else None)
        return clean(request, [e.model_dump(mode="json") for e in episodes])

    @app.get("/knowledge/search")
    def knowledge_search(
        request: Request, q: str = Query(..., min_length=2), k: int = 4
    ) -> list[dict[str, Any]]:
        results = manager(request).runtime.knowledge.search(q, k=k)
        found = [
            {
                **r.model_dump(mode="json"),
                "action_hints": [h.model_dump() for h in parse_action_hints(r.chunk.text)],
                "flags": injection_flags(r.chunk.text),
            }
            for r in results
        ]
        return clean(request, found)

    @app.get("/knowledge/docs")
    def knowledge_docs(request: Request) -> list[dict[str, Any]]:
        chunks = manager(request).runtime.knowledge.chunks
        return clean(
            request,
            [
                {
                    "chunk_id": c.chunk_id,
                    "doc_path": c.doc_path,
                    "title": c.title,
                    "heading": c.heading,
                }
                for c in chunks
            ],
        )

    # --- evaluations --------------------------------------------------------------------------

    @app.post("/evals/run", status_code=202)
    async def run_evals(
        body: EvalRequest, request: Request, who: OperatorRequired
    ) -> dict[str, Any]:
        try:
            return manager(request).start_evaluation(body.configs)
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from exc

    # Declared before /evals/{batch_id}, so "results" is never read as a batch id.
    @app.get("/evals/results")
    def eval_results(request: Request) -> dict[str, Any]:
        return clean(request, manager(request).evaluation_results())

    @app.get("/evals", response_model=EvalsView)
    def evals(request: Request) -> Any:
        """Evaluation batches (published and local), one mode each, with their counts."""
        return reader(request).evals()

    @app.get("/evals/{batch_id}", response_model=BatchView)
    def eval_batch(batch_id: str, request: Request) -> Any:
        """One batch with every record; a batch is found by its id, never by a path."""
        return found(reader(request).batch(batch_id), f"evaluation batch '{batch_id}'")

    # The built dashboard (npm run build) is served at / when present.
    if DASHBOARD_DIST.exists():
        app.mount("/", StaticFiles(directory=DASHBOARD_DIST, html=True), name="dashboard")
    return app


def _event_id(value: str | None) -> int:
    """The seq in a Last-Event-ID header (0 when absent or not a sequence number)."""
    if value is None or not value.strip().isdigit():
        return 0
    return int(value.strip())


app = create_app()
