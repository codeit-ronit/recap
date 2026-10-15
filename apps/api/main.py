"""RECAP HTTP API (FastAPI).

Start it with:  uvicorn apps.api.main:app --reload

Every endpoint reads real data produced by the runtime (runs, trace events, evidence,
approvals, memory, knowledge, evaluation reports). Incidents run against the environment
chosen in configs/environment.yaml (the SIMULATED local environment in V1); /health
labels which kind it is.

No endpoint executes an action directly. Clients can start simulated runs (optionally with
the simulation auto-approver) and evaluations, and approve or reject one exact proposal;
the controller executes approved actions through the safety gateway.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
    decision: Literal["approved", "rejected"]
    proposal_digest: str  # must match the exact proposal the human reviewed
    actor: str = "dashboard-user"


class EvalRequest(BaseModel):
    configs: list[str] | None = None


def create_app(runtime: Runtime | None = None) -> FastAPI:
    """Build the API. Tests pass their own Runtime (temporary data directory)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.manager = RunManager(runtime)
        yield

    app = FastAPI(
        title="RECAP API",
        version=__version__,
        description="RECAP V1 backend.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def manager(request: Request) -> RunManager:
        return request.app.state.manager

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
    async def run_incident(body: RunRequest, request: Request) -> dict[str, str]:
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

    @app.post("/approvals/{proposal_id}")
    def decide_approval(
        proposal_id: str, body: ApprovalRequestBody, request: Request
    ) -> dict[str, Any]:
        """Approve or reject ONE proposal. The digest must match exactly; approvals expire."""
        approvals = manager(request).runtime.approvals
        if approvals.get_request(proposal_id) is None:
            raise HTTPException(404, f"no approval request for proposal '{proposal_id}'")
        try:
            approval: Approval = approvals.decide(
                proposal_id,
                decision=body.decision,
                proposal_digest=body.proposal_digest,
                actor=body.actor,
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
    async def run_evals(body: EvalRequest, request: Request) -> dict[str, Any]:
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
