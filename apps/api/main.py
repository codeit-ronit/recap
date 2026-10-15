"""RECAP HTTP API (FastAPI).

Start it with:  uvicorn apps.api.main:app --reload

Every endpoint reads real data produced by the runtime (runs, trace events, evidence,
approvals, memory, knowledge, evaluation reports). Incidents run against the environment
chosen in configs/environment.yaml (the SIMULATED local environment in V1); /health
labels which kind it is.

No endpoint executes an action directly. Clients can start simulated runs (optionally with
the simulation auto-approver) and evaluations, and approve or reject one exact proposal;
the controller executes approved actions through the safety gateway.

Security (audit finding 2.4, see apps/api/auth.py): every state-changing endpoint needs the
operator's bearer token, and the approver recorded is the authenticated operator, never a
client-supplied name. Only requests for this machine's loopback host names are served, and
unsafe requests from a browser page on another origin are refused.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
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
from apps.api.service import RunManager
from recap import __version__
from recap.core.errors import ApprovalError, ConfigError
from recap.core.models import Approval, EnvironmentInfo, MemoryPartition, RunConfig, RunRecord
from recap.knowledge.hints import parse_action_hints
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
        app.state.auth = auth or OperatorAuth.from_environment(
            app.state.manager.runtime.settings.paths.data_dir
        )
        yield

    app = FastAPI(
        title="RECAP API",
        version=__version__,
        description="RECAP backend.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def refuse_foreign_origins(request: Request, call_next: Any) -> Response:
        """A browser page on another host may not change anything here (anti CSRF)."""
        if request.method in UNSAFE_METHODS and not origin_allowed(
            request.headers.get("origin"), hosts
        ):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    # Outermost: a request addressed to any other host name (DNS rebinding) gets 400.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    def manager(request: Request) -> RunManager:
        return request.app.state.manager

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
            "label": _environment_label(rt.environment_info()),
        }

    @app.get("/system")
    def system(request: Request) -> dict[str, Any]:
        """Which implementation is plugged into each interface (for the Architecture page)."""
        return manager(request).runtime.system_summary()

    @app.get("/scenarios")
    def scenarios(request: Request) -> list[dict[str, Any]]:
        return [s.model_dump(mode="json") for s in manager(request).runtime.list_scenarios()]

    # --- incident runs ----------------------------------------------------------------------

    @app.post("/incidents/run", status_code=202)
    async def run_incident(
        body: RunRequest, request: Request, who: OperatorRequired
    ) -> dict[str, str]:
        # async: starting a background task needs the server's event loop (sync endpoints
        # run in a worker thread that has none).
        mgr = manager(request)
        known = {s.id for s in mgr.runtime.list_scenarios()}
        if body.scenario_id not in known:
            raise HTTPException(404, f"unknown scenario '{body.scenario_id}'")
        if not body.config.safety_enabled:
            raise HTTPException(400, "safety cannot be disabled from the API; use the eval runner")
        default_provider = mgr.runtime.settings.providers.default
        if body.config.provider not in (None, default_provider):
            # A client must not be able to switch on a paid LLM provider; that is server config.
            raise HTTPException(400, "the provider is chosen in server configuration, not per run")
        return {"run_id": mgr.start_run(body.scenario_id, body.config), "status": "started"}

    @app.get("/runs")
    def list_runs(request: Request, limit: int = 50) -> list[dict[str, Any]]:
        return [_run_summary(r) for r in manager(request).runtime.runs.list(limit)]

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> dict[str, Any]:
        mgr = manager(request)
        record = mgr.runtime.runs.get(run_id)
        status = mgr.status(run_id)
        if record is None:
            if status == "unknown":
                raise HTTPException(404, f"unknown run '{run_id}'")
            return {
                "status": status,
                "record": None,
                "evidence": [],
                "error": mgr.errors.get(run_id),
            }
        evidence = mgr.runtime.runs.evidence(run_id)
        return {
            "status": status,
            "record": record.model_dump(mode="json"),
            "evidence": [e.model_dump(mode="json") for e in evidence],
            "error": mgr.errors.get(run_id),
        }

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
        return [e.model_dump(mode="json") for e in events]

    @app.get("/runs/{run_id}/stream")
    async def run_stream(run_id: str, request: Request, after_seq: int = 0) -> StreamingResponse:
        """Server-sent events: stored events, then live ones if this server is running the run."""
        return StreamingResponse(
            manager(request).stream(run_id, after_seq),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- approvals --------------------------------------------------------------------------

    @app.get("/approvals/pending")
    def pending_approvals(request: Request) -> list[dict[str, Any]]:
        return [r.model_dump(mode="json") for r in manager(request).runtime.approvals.pending()]

    @app.get("/approvals/events")
    def recent_approval_events(request: Request, limit: int = 50) -> list[dict[str, Any]]:
        """The newest entries of the append-only decision log, across all proposals."""
        events = manager(request).runtime.approvals.recent_events(max(1, min(limit, 500)))
        return [e.model_dump(mode="json") for e in events]

    @app.get("/approvals/{proposal_id}/events")
    def approval_events(proposal_id: str, request: Request) -> list[dict[str, Any]]:
        """The full decision log of one proposal: requested, decided, used or refused."""
        events = manager(request).runtime.approvals.events(proposal_id)
        return [e.model_dump(mode="json") for e in events]

    @app.post("/approvals/{proposal_id}")
    def decide_approval(
        proposal_id: str,
        body: ApprovalRequestBody,
        request: Request,
        who: OperatorRequired,
    ) -> dict[str, Any]:
        """Approve or reject ONE proposal. The digest must match exactly; approvals expire.

        A rejection also revokes an approval that was not used yet: "no" wins until the
        action has run. The approver recorded is the authenticated operator.
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
        return [e.model_dump(mode="json") for e in episodes]

    @app.get("/knowledge/search")
    def knowledge_search(
        request: Request, q: str = Query(..., min_length=2), k: int = 4
    ) -> list[dict[str, Any]]:
        results = manager(request).runtime.knowledge.search(q, k=k)
        return [
            {
                **r.model_dump(mode="json"),
                "action_hints": [h.model_dump() for h in parse_action_hints(r.chunk.text)],
                "flags": injection_flags(r.chunk.text),
            }
            for r in results
        ]

    @app.get("/knowledge/docs")
    def knowledge_docs(request: Request) -> list[dict[str, Any]]:
        return [
            {"chunk_id": c.chunk_id, "doc_path": c.doc_path, "title": c.title, "heading": c.heading}
            for c in manager(request).runtime.knowledge.chunks
        ]

    # --- evaluations --------------------------------------------------------------------------

    @app.post("/evals/run", status_code=202)
    async def run_evals(
        body: EvalRequest, request: Request, who: OperatorRequired
    ) -> dict[str, Any]:
        try:
            return manager(request).start_evaluation(body.configs)
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/evals/results")
    def eval_results(request: Request) -> dict[str, Any]:
        return manager(request).evaluation_results()

    # The built dashboard (npm run build) is served at / when present.
    if DASHBOARD_DIST.exists():
        app.mount("/", StaticFiles(directory=DASHBOARD_DIST, html=True), name="dashboard")
    return app


def _environment_label(info: EnvironmentInfo) -> str:
    """The dashboard banner: it says SIMULATED only when the environment really is."""
    if info.is_simulated:
        return "SIMULATED LOCAL ENVIRONMENT · V1"
    return f"LIVE ENVIRONMENT ({info.kind}) · V1"


def _run_summary(record: RunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "scenario_id": record.scenario_id,
        "state": record.state.value,
        "label": record.config.label,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "modes": [s.mode.value for s in record.strategies],
        "recovered": bool(record.recovery and record.recovery.verified),
        "outcome_note": record.outcome_note,
        "provider": record.provider,
        "tool_calls": record.usage.tool_calls,
        "pending_approval": record.pending_approval is not None,
    }


app = create_app()
