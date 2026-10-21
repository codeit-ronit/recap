"""Local protocol fixtures for the LLM providers: fake Ollama and Groq servers.

They speak the real HTTP protocols through httpx.MockTransport, so the provider code under
test (request building, status handling, parsing, recording) runs exactly as in production,
with no network and no model. A fixture never reasons about an incident: each test
scripts the answers it needs, and the fixture records every request it received, so tests
can check exactly what RECAP sent to a model.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from recap.core.config import Settings
from recap.core.models import (
    AgentRole,
    ContextItem,
    ContextPack,
    Evidence,
    FailureMechanism,
    Hypothesis,
    Incident,
    InvestigationRequest,
    ProposalRequest,
    RunBudget,
    SourceType,
    ToolCallRecord,
    VerificationReport,
)
from recap.tools.registry import ToolRegistry

T0 = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)
OLLAMA_MODEL = "qwen3:8b"
GROQ_MODEL = "openai/gpt-oss-120b"
FAKE_GROQ_KEY = "gsk_" + "TESTKEYnotREAL" * 3  # shaped like a Groq key, never valid
FAKE_REQUEST_ID = "req_01FAKEACCOUNTLINK0000000000"  # shaped like a Groq request ID
FAKE_COMPLETION_ID = "chatcmpl-0f4e5a6b-fake-4c1d-9e8f-account0link"  # an answer's own ID
FAKE_OLLAMA_VERSION = "0.0.0-fake"  # never a real Ollama version: fake runs stay recognizable
FAKE_WEIGHTS = "sha256-" + "fa4e" * 16  # a weights file name as /api/show's modelfile gives it

# An answer is a dict for the next 200 reply, or a ready httpx.Response (errors, 429s...).
Answer = dict[str, Any] | httpx.Response
Responder = Callable[[dict[str, Any]], Answer]


# --- answers a fake model can give ---------------------------------------------------------


def json_answer(value: dict[str, Any]) -> dict[str, Any]:
    """A json-mode answer: the content is one JSON object."""
    return {"content": json.dumps(value)}


def tool_answer(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """A tools-mode answer: native tool calls and no text."""
    return {"tool_calls": [{"name": name, "arguments": args} for name, args in calls]}


def step(kind: str, **fields: Any) -> dict[str, Any]:
    """A complete json-mode step answer with every field the schema requires."""
    answer = {"summary": "", "kind": kind, "tool_calls": [], "hypotheses": [], "next_checks": []}
    return json_answer({**answer, **fields})


def call(tool: str, **args: Any) -> dict[str, Any]:
    """One entry of a json-mode tool_calls list."""
    return {"tool_name": tool, "reason": "test", **args}


def hypothesis(
    target: str,
    evidence_ids: Iterable[str],
    *,
    mechanism: str = "bad_deployment",
    version: str | None = "v2.4.1",
    dependency: str | None = None,
    claim: str = "checkout v2.4.1 broke checkout",
    contradicting: Iterable[str] = (),
    confidence_note: str = "",
) -> dict[str, Any]:
    return {
        "claim": claim,
        "mechanism": mechanism,
        "target_service": target,
        "version": version,
        "dependency": dependency,
        "supporting_evidence_ids": list(evidence_ids),
        "contradicting_evidence_ids": list(contradicting),
        "confidence_note": confidence_note,
    }


def proposal(
    evidence_ids: Iterable[str],
    *,
    kind: str = "final_proposal",
    action: str = "execute_rollback",
    target: str = "checkout",
    to_version: str | None = "v2.4.0",
    citations: Iterable[str] = (),
) -> dict[str, Any]:
    return json_answer(
        {
            "rationale": "roll back the bad release",
            "kind": kind,
            "action": action,
            "target_service": target,
            "to_version": to_version,
            "evidence_ids": list(evidence_ids),
            "citations": list(citations),
            "expected_health_change": "errors drop",
            "rollback_hint": "redeploy v2.4.1",
        }
    )


# --- fake servers ---------------------------------------------------------------------------


class FakeOllama:
    """An Ollama server: /api/version, /api/show and /api/chat answered from a script.

    `answers` is consumed in order (one per /api/chat request); a callable `responder` is
    asked when the script is empty. Every /api/chat body is kept in `chats`.
    """

    def __init__(
        self,
        answers: Iterable[Answer] = (),
        *,
        responder: Responder | None = None,
        model: str = OLLAMA_MODEL,
        capabilities: Iterable[str] = ("completion", "tools", "thinking"),
        installed: bool = True,
        context_length: int = 40960,
        prompt_tokens: int = 1200,
        output_tokens: int = 80,
        answered_by: str | None = None,
    ) -> None:
        self.answers: deque[Answer] = deque(answers)
        self.responder = responder
        self.model = model
        self.capabilities = list(capabilities)
        self.installed = installed
        self.context_length = context_length
        self.prompt_tokens = prompt_tokens
        self.output_tokens = output_tokens
        self.answered_by = answered_by or model
        self.requests: list[httpx.Request] = []
        self.chats: list[dict[str, Any]] = []

    def mock(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": FAKE_OLLAMA_VERSION})
        if path == "/api/tags":
            models = [{"name": self.model}] if self.installed else []
            return httpx.Response(200, json={"models": [*models, {"name": "llama3.2:3b"}]})
        if path == "/api/show":
            if not self.installed:
                return httpx.Response(404, json={"error": f"model '{self.model}' not found"})
            return httpx.Response(
                200,
                json={
                    "capabilities": self.capabilities,
                    "details": {"parameter_size": "8.2B", "quantization_level": "Q4_K_M"},
                    "model_info": {"qwen3.context_length": self.context_length},
                    # What a real server also returns, and a recording must NOT keep:
                    "license": "LICENSE TEXT that RECAP never needs",
                    "template": "{{ .System }} {{ .Prompt }}",
                    "modelfile": f"FROM {Path.home()}/.ollama/models/blobs/{FAKE_WEIGHTS}",
                    "parameters": f"stop <|im_end|>\nnote {FAKE_GROQ_KEY}",
                },
            )
        if path == "/api/chat":
            body = json.loads(request.content)
            self.chats.append(body)
            answer = self._next(body)
            if isinstance(answer, httpx.Response):
                return answer
            message = {"role": "assistant", "content": answer.get("content", "")}
            if answer.get("tool_calls"):
                message["tool_calls"] = [
                    {"function": {"name": c["name"], "arguments": c["arguments"]}}
                    for c in answer["tool_calls"]
                ]
            if answer.get("thinking"):
                message["thinking"] = answer["thinking"]
            return httpx.Response(
                200,
                json={
                    "model": self.answered_by,
                    "message": message,
                    "done": True,
                    "done_reason": answer.get("done_reason", "stop"),
                    "prompt_eval_count": answer.get("prompt_tokens", self.prompt_tokens),
                    "eval_count": self.output_tokens,
                    "load_duration": 0,
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    def _next(self, body: dict[str, Any]) -> Answer:
        if self.answers:
            return self.answers.popleft()
        if self.responder is not None:
            return self.responder(body)
        raise AssertionError("the fake model has no answer left for this request")


class FakeGroq:
    """Groq's OpenAI-compatible API: /models and /chat/completions answered from a script.

    Successful answers carry the free plan's rate-limit headers unless `daily_limit` says
    otherwise (e.g. a paid plan's "500000"); `headers=False` sends none at all.
    """

    def __init__(
        self,
        answers: Iterable[Answer] = (),
        *,
        responder: Responder | None = None,
        model: str = GROQ_MODEL,
        active: bool = True,
        daily_limit: str = "1000",
        headers: bool = True,
        answered_by: str | None = None,
    ) -> None:
        self.answers: deque[Answer] = deque(answers)
        self.responder = responder  # asked when `answers` is empty (body in OpenAI form)
        self.model = model
        self.active = active
        self.daily_limit = daily_limit
        self.send_headers = headers
        self.answered_by = answered_by or model
        self.requests: list[httpx.Request] = []
        self.chats: list[dict[str, Any]] = []

    def mock(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [{"id": self.model, "active": self.active, "context_window": 131072}]
                },
            )
        if path.endswith("/chat/completions"):
            body = json.loads(request.content)
            self.chats.append(body)
            if self.answers:
                answer = self.answers.popleft()
            elif self.responder is not None:
                answer = self.responder(body)
            else:
                raise AssertionError("the fake model has no answer left for this request")
            if isinstance(answer, httpx.Response):
                return answer
            message: dict[str, Any] = {"role": "assistant", "content": answer.get("content")}
            if answer.get("tool_calls"):
                message["tool_calls"] = [
                    {
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                    }
                    for index, c in enumerate(answer["tool_calls"])
                ]
            headers = self.limit_headers() if self.send_headers else {}
            return httpx.Response(
                200,
                headers=headers,
                json={
                    # Like a real Groq answer: its own ID, Groq's request ID and a fingerprint.
                    "id": FAKE_COMPLETION_ID,
                    "x_groq": {"id": FAKE_REQUEST_ID},
                    "system_fingerprint": "fp_fakefakefake",
                    "model": self.answered_by,
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": answer.get("finish", "stop"),
                        }
                    ],
                    "usage": {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020},
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found", "code": "not_found"}})

    def limit_headers(self) -> dict[str, str]:
        return {
            "x-ratelimit-limit-requests": self.daily_limit,
            "x-ratelimit-remaining-requests": "999",
            "x-ratelimit-reset-requests": "1m26s",
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": "7000",
            "x-ratelimit-reset-tokens": "7.5s",
            "x-request-id": FAKE_REQUEST_ID,  # real servers send one; recordings must not keep it
        }


def error(status: int, code: str = "", message: str = "", **headers: str) -> httpx.Response:
    """An OpenAI-style error response."""
    body = {"error": {"message": message or f"error {status}", "type": code, "code": code}}
    return httpx.Response(status, headers=headers, json=body)


# --- requests a provider receives ---------------------------------------------------------


def evidence(evidence_id: str, ref: str, text: str = "ERROR NullPointerException") -> Evidence:
    tool = ref.split(":", 1)[1].split("(", 1)[0]
    return Evidence(
        id=evidence_id,
        incident_id="inc-1",
        source_type=SourceType.TOOL,
        source_ref=ref,
        timestamp=T0,
        content=text,
        tool_name=tool,
    )


def incident() -> Incident:
    return Incident(
        incident_id="inc-1",
        scenario_id="bad-deployment",  # eval bookkeeping: providers only see agent_view()
        service="checkout",
        affected_services=["checkout"],
        symptoms=["checkout: HTTP 5xx ratio above 30%"],
        created_at=T0,
        environment="simulated",
        budget=RunBudget(),
    )


def pack(items: Iterable[Evidence] = (), docs: Iterable[tuple[str, str]] = ()) -> ContextPack:
    entries = [
        ContextItem(
            item_id=e.id,
            kind="evidence",
            source_ref=e.source_ref,
            text=e.content,
            token_estimate=10,
            score=1.0,
        )
        for e in items
    ]
    entries += [
        ContextItem(
            item_id=doc_id, kind="doc", source_ref=doc_id, text=text, token_estimate=10, score=1.0
        )
        for doc_id, text in docs
    ]
    return ContextPack(items=entries, budget_tokens=3000, tokens_before=10, tokens_after=10)


def investigation_request(
    settings: Settings,
    *,
    role: AgentRole = AgentRole.INVESTIGATOR,
    seen: Iterable[Evidence] = (),
    history: Iterable[ToolCallRecord] = (),
    step_number: int = 1,
    max_steps: int = 6,
) -> InvestigationRequest:
    """A request with the real, trusted tool views for `role` (from configs/policies.yaml)."""
    tools = ToolRegistry(settings.policies).views(settings.agents[role].tools)
    seen = list(seen)
    return InvestigationRequest(
        role=role,
        goal=settings.agents[role].goal,
        incident=incident().agent_view(),
        context=pack(seen),
        evidence={e.id: e for e in seen},
        tools=tools,
        history=list(history),
        observed_refs=[e.source_ref for e in seen],
        step=step_number,
        max_steps=max_steps,
        responsibilities=settings.agents[role].responsibilities,  # the role's contract,
        mechanisms=settings.agents[role].mechanisms,  # as the harness passes it
    )


def proposal_request(
    settings: Settings,
    seen: Iterable[Evidence],
    *,
    docs: Iterable[tuple[str, str]] = (),
    failed: Iterable[Any] = (),
) -> ProposalRequest:
    seen = list(seen)
    best = Hypothesis(
        claim="checkout v2.4.1 broke checkout",
        mechanism=FailureMechanism.BAD_DEPLOYMENT,
        target_service="checkout",
        supporting_evidence_ids=[e.id for e in seen],
        details={"version": "v2.4.1"},
    )
    return ProposalRequest(
        incident=incident().agent_view(),
        hypothesis=best,
        report=VerificationReport(status="supported", best=best),
        context=pack(seen, docs),
        evidence={e.id: e for e in seen},
        allowed_actions=ToolRegistry(settings.policies).remediation_views(),
        failed_actions=list(failed),
    )


# --- a scripted stand-in for a model (for end-to-end runs) --------------------------------


class ScriptedModel:
    """Answers each request from a fixed plan. It does NOT reason: it lets end-to-end tests
    drive the real pipeline (HTTP, protocol, harness, MCP gateway, verifier, approval,
    executor, oracle) with predictable model answers, offline.

    Investigation steps follow `plan` (tools mode or json mode, whichever the request
    uses), keeping only calls to tools the agent was offered; when the plan runs out it
    finishes with a bad_deployment hypothesis citing every evidence ID the request lists.
    Proposals take the next action from `fixes`, citing every listed evidence ID.
    """

    def __init__(
        self,
        service: str = "checkout",
        bad_version: str = "v2.4.1",
        good_version: str = "v2.4.0",
        fixes: Iterable[str] = ("execute_rollback",),
    ) -> None:
        self.service = service
        self.bad_version = bad_version
        self.good_version = good_version
        self.fixes = deque(fixes)
        self.plan = [
            [
                ("get_recent_deployments", {"service": service}),
                ("get_service_logs", {"service": service}),
            ],
            [
                ("get_service_metrics", {"service": service, "metric": "error_rate"}),
                ("propose_rollback", {"service": service}),
            ],
        ]

    def __call__(self, body: dict[str, Any]) -> Answer:
        schema = body.get("format")
        if schema is not None and "action" in schema.get("properties", {}):
            return self._proposal(schema)
        text = body["messages"][1]["content"]
        step_number = int(text.rsplit("This is step ", 1)[1].split(" ", 1)[0])
        offered = self._offered(body)
        planned = self.plan[step_number - 1] if step_number <= len(self.plan) else []
        calls = [(name, args) for name, args in planned if name in offered]
        ids = self._visible_ids(body)
        if body.get("tools"):
            if calls:
                return tool_answer(*calls)
            finish = {
                "kind": "hypothesis",
                "summary": "bad release",
                "next_checks": [],
                "hypotheses": [self._hypothesis(ids)],
            }
            return tool_answer(("submit_findings", finish))
        if calls:
            return step("tool_request", tool_calls=[call(name, **args) for name, args in calls])
        return step("hypothesis", hypotheses=[self._hypothesis(ids)])

    def _proposal(self, schema: dict[str, Any]) -> Answer:
        action = self.fixes.popleft() if self.fixes else "execute_rollback"
        ids = schema["properties"]["evidence_ids"]["items"].get("enum", [])
        to_version = self.good_version if action == "execute_rollback" else None
        return proposal(ids[:6], action=action, target=self.service, to_version=to_version)

    def _hypothesis(self, ids: list[str]) -> dict[str, Any]:
        return hypothesis(
            self.service,
            ids[:8],
            version=self.bad_version,
            claim=f"{self.service} {self.bad_version} broke {self.service}",
        )

    @staticmethod
    def _offered(body: dict[str, Any]) -> set[str]:
        if body.get("tools"):
            return {tool["function"]["name"] for tool in body["tools"]}
        items = body["format"]["properties"]["tool_calls"]["items"]
        return set(items["properties"]["tool_name"].get("enum", []))

    @staticmethod
    def _visible_ids(body: dict[str, Any]) -> list[str]:
        """The evidence IDs in the prompt's context items (what a model can cite)."""
        text = body["messages"][1]["content"]
        return list(dict.fromkeys(re.findall(r'id="(ev-[0-9a-f]{6}-[0-9a-z]+)"', text)))


def as_ollama_body(body: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI-style request in the shape ScriptedModel reads (messages + format/tools)."""
    converted: dict[str, Any] = {"messages": body["messages"]}
    if "response_format" in body:
        converted["format"] = body["response_format"]["json_schema"]["schema"]
    if "tools" in body:
        converted["tools"] = body["tools"]
    return converted


class OperatorApproves:
    """Approval channel standing in for a human who approves each exact request.

    It decides through the ApprovalStore, which applies the same checks as for a person
    approving from the dashboard (exact digest, single use, "no" wins until used).
    """

    name = "test operator"

    def __init__(self, store: Any) -> None:
        self.store = store
        self.seen: list[Any] = []

    async def notify(self, request: Any) -> None:
        self.seen.append(request)
        self.store.decide(
            request.proposal_id,
            decision="approved",
            proposal_digest=request.proposal_digest,
            actor="test-operator",
        )
