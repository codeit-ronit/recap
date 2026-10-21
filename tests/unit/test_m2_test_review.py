"""Tests added after the M2 test-quality review: each closes a gap where a broken guarantee
would not have failed any test (or would have failed for the wrong reason).

Offline, against the local protocol fixtures (tests/llm_fakes.py).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from recap.context.tokens import estimate_tokens  # noqa: F401  (kept for readers of the fit test)
from recap.core.config import Settings
from recap.core.errors import ConfigError, ProviderError, QuotaExhausted
from recap.core.ids import canonical_json, utcnow
from recap.core.models import AgentRole
from recap.providers import openai_compatible, protocol
from recap.providers.factory import create_provider
from recap.providers.http_llm import HttpLLMProvider
from recap.providers.ollama_provider import SAFE_CHARS_PER_TOKEN, check_local_settings
from recap.providers.openai_compatible import FreeHostedProvider
from recap.providers.quota import OPT_IN_ENV, UsageLedger
from recap.providers.transport import LiveTransport, Recording
from tests.llm_fakes import (
    FAKE_GROQ_KEY,
    FakeGroq,
    FakeOllama,
    call,
    error,
    evidence,
    investigation_request,
    proposal_request,
    step,
    tool_answer,
)

GROQ_SCOPE = "api.groq.com"
LIST_SERVICES = step("tool_request", tool_calls=[call("list_services")])
DEPLOYS = evidence("ev-abc123-001", "mcp:get_recent_deployments(service=checkout)", "v2.4.1")
LOGS = evidence("ev-abc123-002", "mcp:get_service_logs(service=checkout)", "ERROR NPE")
# A key in a variable whose NAME the redactor does not treat as secret, with a value no key
# pattern matches: only the first line of defence (never writing it) can keep it out.
PLAIN_KEY_ENV = "GROQ_CREDENTIAL"
PLAIN_KEY = "plainCredentialValue0123456789"


@pytest.fixture
def groq_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OPT_IN_ENV, "1")
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("RECAP_GROQ_FREE_PLAN_CONFIRMED_ON", date.today().isoformat())


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(openai_compatible.asyncio, "sleep", fake_sleep)
    return waits


def groq(settings: Settings, fake: FakeGroq) -> HttpLLMProvider:
    provider = create_provider("groq", settings, mock_http=fake.mock())
    assert isinstance(provider, HttpLLMProvider)
    return provider


def chat_requests(fake: FakeGroq) -> list[httpx.Request]:
    return [r for r in fake.requests if r.url.path.endswith("/chat/completions")]


def with_plain_key(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings whose Groq key lives in PLAIN_KEY_ENV (a name the redactor ignores)."""
    monkeypatch.setenv(OPT_IN_ENV, "1")
    monkeypatch.setenv(PLAIN_KEY_ENV, PLAIN_KEY)
    monkeypatch.setenv("RECAP_GROQ_FREE_PLAN_CONFIRMED_ON", date.today().isoformat())
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    entry = settings.providers.providers["groq"]
    assert entry.free_tier is not None
    free = entry.free_tier.model_copy(update={"api_key_env": PLAIN_KEY_ENV})
    providers = dict(
        settings.providers.providers, groq=entry.model_copy(update={"free_tier": free})
    )
    return settings.model_copy(
        update={"providers": settings.providers.model_copy(update={"providers": providers})}
    )


# --- 1. loopback and cloud checks with tricky inputs -----------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1.nip.io:11434",
        "http://localhost.evil.example:11434",
        "http://127.0.0.1@evil.example:11434",
        "http://evil.example/?h=127.0.0.1",
        "http://evil.example/127.0.0.1",
        "http://10.0.0.1:11434",
    ],
)
def test_lookalike_hosts_are_not_loopback(settings: Settings, url: str) -> None:
    spec = settings.providers.providers["ollama"].model_copy(update={"base_url": url})
    with pytest.raises(ConfigError, match="local-only"):
        check_local_settings(spec)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434"]
)
def test_real_loopback_forms_are_accepted(settings: Settings, url: str) -> None:
    check_local_settings(
        settings.providers.providers["ollama"].model_copy(update={"base_url": url})
    )


def test_a_cloud_tag_without_a_dash_is_refused(settings: Settings) -> None:
    spec = settings.providers.providers["ollama"].model_copy(update={"model": "glm-4.6:cloud"})
    with pytest.raises(ConfigError, match="cloud model"):
        check_local_settings(spec)


# --- 2-3. the schemas carry the allowlists (asserted directly, not against the builder) -------


def test_strict_outputs_keep_every_constraint_but_the_list_caps() -> None:
    schema = {
        "type": "object",
        "required": ["a", "b"],
        "additionalProperties": False,
        "properties": {
            "a": {"type": "string", "enum": ["x", "y"]},
            "b": {"anyOf": [{"type": "string", "enum": ["z"]}, {"type": "null"}]},
            "c": {"type": "array", "maxItems": 2, "items": {"type": "string", "enum": ["q"]}},
        },
    }
    stripped = protocol.for_strict_outputs(schema)
    assert "maxItems" not in json.dumps(stripped)
    del schema["properties"]["c"]["maxItems"]
    assert stripped == schema  # everything else, enums included, is untouched


async def test_the_hosted_request_carries_the_allowlists(
    settings: Settings, groq_env: None
) -> None:
    fake = FakeGroq([LIST_SERVICES])
    await groq(settings, fake).next_action(investigation_request(settings, seen=[DEPLOYS, LOGS]))
    sent = fake.chats[0]["response_format"]["json_schema"]["schema"]["properties"]
    tool_names = sent["tool_calls"]["items"]["properties"]["tool_name"]["enum"]
    assert set(tool_names) == set(settings.agents[AgentRole.INVESTIGATOR].tools)
    hypothesis = sent["hypotheses"]["items"]["properties"]
    assert hypothesis["supporting_evidence_ids"]["items"]["enum"] == [
        "ev-abc123-001",
        "ev-abc123-002",
    ]
    assert hypothesis["target_service"]["anyOf"][0]["enum"] == sorted(
        settings.policies.allowed_services
    )


def test_the_proposal_schema_offers_only_the_catalog_and_real_ids(settings: Settings) -> None:
    request = proposal_request(
        settings, [DEPLOYS], docs=[("runbook-bad-deployment#remediation-1", "x")]
    )
    properties = protocol.proposal_schema(request)["properties"]
    assert properties["action"]["enum"] == settings.policies.remediation_tools
    assert properties["target_service"]["enum"] == sorted(settings.policies.allowed_services)
    assert properties["evidence_ids"]["items"]["enum"] == ["ev-abc123-001"]
    assert properties["citations"]["items"]["enum"] == ["runbook-bad-deployment#remediation-1"]
    assert "delete_namespace" not in json.dumps(properties)


def test_tool_definitions_are_closed_and_offer_no_write_tool(settings: Settings) -> None:
    tools = protocol.tool_definitions(investigation_request(settings))
    names = {tool["function"]["name"] for tool in tools}
    assert names == set(settings.agents[AgentRole.INVESTIGATOR].tools) | {protocol.FINISH_TOOL}
    for tool in tools:
        parameters = tool["function"]["parameters"]
        assert parameters["additionalProperties"] is False
        for prop in parameters["properties"].values():
            if prop.get("type") == "array":
                assert prop["items"].get("additionalProperties", False) is False
    finish = next(t for t in tools if t["function"]["name"] == protocol.FINISH_TOOL)
    params = finish["function"]["parameters"]
    assert set(params["required"]) == set(params["properties"])


# --- 5. nothing is sent before the checks, also AFTER the provider was built -----------------


async def test_an_opt_in_withdrawn_after_build_stops_every_request(
    settings: Settings, groq_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGroq([LIST_SERVICES])
    provider = groq(settings, fake)
    monkeypatch.delenv(OPT_IN_ENV)
    with pytest.raises(ConfigError, match="RECAP_ALLOW_LIVE_FREE_INFERENCE"):
        await provider.next_action(investigation_request(settings))
    assert fake.requests == []  # not even the model-list request


async def test_a_stop_recorded_after_build_stops_every_request(
    settings: Settings, groq_env: None
) -> None:
    fake = FakeGroq([LIST_SERVICES])
    provider = groq(settings, fake)
    UsageLedger(settings.paths.usage_ledger).stop(GROQ_SCOPE, "another process saw a paid plan")
    with pytest.raises(ConfigError, match="was stopped"):
        await provider.next_action(investigation_request(settings))
    assert fake.requests == []


def test_a_live_hosted_transport_without_a_guard_is_refused(settings: Settings) -> None:
    spec = settings.providers.providers["groq"]
    with pytest.raises(ConfigError, match="needs its free-tier guard"):
        FreeHostedProvider("groq", spec, LiveTransport(spec.base_url or ""), guard=None)


async def test_an_inactive_model_is_never_called(settings: Settings, groq_env: None) -> None:
    fake = FakeGroq([LIST_SERVICES], active=False)
    with pytest.raises(ProviderError, match="not offered"):
        await groq(settings, fake).next_action(investigation_request(settings))
    assert chat_requests(fake) == []


# --- 6. a retried request is checked and counted again --------------------------------------


async def test_a_retry_after_a_429_is_reserved_and_counted_again(
    settings: Settings, groq_env: None, no_wait: list[float]
) -> None:
    fake = FakeGroq([error(429, "rate_limit_exceeded", **{"retry-after": "1"}), LIST_SERVICES])
    provider = groq(settings, fake)
    await provider.next_action(investigation_request(settings))
    assert provider.guard is not None and provider.guard.requests_this_run == 2  # type: ignore[attr-defined]
    assert (
        UsageLedger(settings.paths.usage_ledger).window(
            GROQ_SCOPE, utcnow() - timedelta(minutes=1)
        )[0]
        == 2
    )


@pytest.mark.parametrize(
    ("first", "label"),
    [
        (error(429, "rate_limit_exceeded", **{"retry-after": "120"}), "long retry-after"),
        (error(429, "rate_limit_exceeded"), "no retry-after"),
    ],
)
async def test_a_429_that_must_not_be_retried_is_not_even_with_an_answer_waiting(
    settings: Settings, groq_env: None, no_wait: list[float], first: Any, label: str
) -> None:
    """A trap answer waits behind the 429: a wrong retry would succeed instead of deferring."""
    fake = FakeGroq([first, LIST_SERVICES])
    with pytest.raises(QuotaExhausted, match="429"):
        await groq(settings, fake).next_action(investigation_request(settings))
    assert len(chat_requests(fake)) == 1 and no_wait == []


async def test_a_server_error_waits_before_its_single_retry(
    settings: Settings, groq_env: None, no_wait: list[float]
) -> None:
    fake = FakeGroq([error(503, "service_unavailable"), LIST_SERVICES])
    await groq(settings, fake).next_action(investigation_request(settings))
    assert no_wait == [openai_compatible.SERVER_ERROR_RETRY_SECONDS]
    assert len(chat_requests(fake)) == 2


# --- 7. the key never leaves the wire, checked with a key the redactor cannot see -------------


async def test_an_unredactable_key_reaches_no_file_recap_writes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = with_plain_key(settings, monkeypatch)
    fake = FakeGroq([LIST_SERVICES])
    provider = groq(plain, fake)
    decision = await provider.next_action(investigation_request(plain))

    assert chat_requests(fake)[0].headers["authorization"] == f"Bearer {PLAIN_KEY}"
    assert provider.info.recording is not None
    recorded = (plain.paths.root / provider.info.recording).read_text()
    assert PLAIN_KEY not in recorded
    assert "authorization" not in recorded.lower() and "bearer" not in recorded.lower()
    assert PLAIN_KEY not in decision.model_dump_json()
    assert PLAIN_KEY not in plain.paths.usage_ledger.read_bytes().decode("utf-8", "ignore")


# --- 8. recordings keep only what a provider reads --------------------------------------------


async def test_a_recording_drops_the_model_files_and_the_home_folder(settings: Settings) -> None:
    fake = FakeOllama([tool_answer(("list_services", {}))])  # its /api/show is realistic
    provider = create_provider("ollama", settings, mock_http=fake.mock())
    await provider.next_action(investigation_request(settings))
    assert provider.info.recording is not None
    text = (settings.paths.root / provider.info.recording).read_text()
    for unwanted in ("LICENSE TEXT", "{{ .System }}", str(Path.home()), FAKE_GROQ_KEY, "modelfile"):
        assert unwanted not in text, unwanted
    show = next(
        e
        for e in Recording.load(settings.paths.root / provider.info.recording).exchanges
        if e["path"] == "/api/show"
    )
    assert set(show["response"]["body"]) == {"capabilities", "details", "model_info"}


# --- 9. parser gaps ---------------------------------------------------------------------------


def test_the_same_call_twice_in_one_answer_runs_once(settings: Settings) -> None:
    calls = [call("get_service_health", service="checkout")] * 2
    parsed = protocol.parse_json_step(
        step("tool_request", tool_calls=calls)["content"], investigation_request(settings)
    )
    assert len(parsed.decision.tool_calls) == 1  # type: ignore[union-attr]
    assert any("repeat" in note for note in parsed.notes)


def proposal_with(**fields: Any) -> str:
    base = {
        "rationale": "roll back",
        "kind": "final_proposal",
        "action": "execute_rollback",
        "target_service": "checkout",
        "to_version": "v2.4.0",
        "evidence_ids": ["ev-abc123-001"],
        "citations": [],
        "expected_health_change": "",
        "rollback_hint": "",
    }
    return json.dumps({**base, **fields})


def test_a_proposal_keeps_only_the_actions_own_arguments(settings: Settings) -> None:
    text = proposal_with(service="payment", namespace="kube-system", replicas=0)
    parsed = protocol.parse_proposal(text, proposal_request(settings, [DEPLOYS]))
    draft = parsed.decision.draft  # type: ignore[union-attr]
    assert draft.params == {"to_version": "v2.4.0"} and draft.target == "checkout"


def test_a_runbook_id_is_not_evidence(settings: Settings) -> None:
    request = proposal_request(
        settings, [DEPLOYS], docs=[("runbook-bad-deployment#remediation-1", "x")]
    )
    text = proposal_with(evidence_ids=["runbook-bad-deployment#remediation-1"])
    parsed = protocol.parse_proposal(text, request)
    assert parsed.decision is None and "cite at least one evidence ID" in parsed.problem


def test_a_proposal_without_a_target_is_refused(settings: Settings) -> None:
    parsed = protocol.parse_proposal(
        proposal_with(target_service=None), proposal_request(settings, [DEPLOYS])
    )
    assert parsed.decision is None and "target_service is required" in parsed.problem


# --- 10. the prompt-fit boundary --------------------------------------------------------------


async def test_a_prompt_that_fits_without_room_for_the_answer_is_refused(
    settings: Settings,
) -> None:
    request = investigation_request(settings)
    messages = protocol.investigation_messages(request, "tools")
    tools = protocol.tool_definitions(request)
    prompt = (len(canonical_json(messages)) + len(canonical_json(tools))) // SAFE_CHARS_PER_TOKEN
    answer = settings.providers.providers["ollama"].max_output_tokens

    def with_context(tokens: int) -> HttpLLMProvider:
        providers = dict(settings.providers.providers)
        providers["ollama"] = providers["ollama"].model_copy(update={"context_tokens": tokens})
        changed = settings.model_copy(
            update={"providers": settings.providers.model_copy(update={"providers": providers})}
        )
        return create_provider("ollama", changed, mock_http=fake.mock())  # type: ignore[return-value]

    fake = FakeOllama([tool_answer(("list_services", {}))])
    with pytest.raises(ProviderError, match="may not fit"):
        await with_context(prompt + answer // 2).next_action(request)  # the prompt alone fits
    assert [r for r in fake.requests if r.url.path == "/api/chat"] == []
    decision = await with_context(prompt + answer + 8).next_action(request)  # just enough
    assert decision.tool_calls[0].tool_name == "list_services"
