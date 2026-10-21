"""Tests for FreeHostedProvider (Groq Free plan) against a local protocol fixture.

FakeGroq (tests/llm_fakes.py) answers like Groq's OpenAI-compatible API through
httpx.MockTransport: the provider's real HTTP code runs, nothing leaves the machine and no
key is real. These tests pin the $0 rules: nothing is sent unless every check passes, a
response that shows a paid plan stops the provider, a 429 is retried at most once, and the
key never appears anywhere RECAP writes.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from recap.context.tokens import estimate_tokens
from recap.core.config import ProviderSettings, Settings
from recap.core.errors import ConfigError, ProviderError, QuotaExhausted
from recap.core.ids import canonical_json, utcnow
from recap.core.models import Usage
from recap.providers import openai_compatible, protocol
from recap.providers.factory import create_provider
from recap.providers.http_llm import HttpLLMProvider
from recap.providers.quota import OPT_IN_ENV, UsageLedger
from recap.telemetry.redact import Redactor
from recap.telemetry.usage import cost_label
from tests.llm_fakes import (
    FAKE_COMPLETION_ID,
    FAKE_GROQ_KEY,
    FAKE_REQUEST_ID,
    FakeGroq,
    call,
    error,
    evidence,
    investigation_request,
    proposal,
    proposal_request,
    step,
    tool_answer,
)

ATTESTATION_ENV = "RECAP_GROQ_FREE_PLAN_CONFIRMED_ON"
ALLOWED_FIELDS = {
    "model",
    "messages",
    "temperature",
    "max_completion_tokens",
    "seed",
    "reasoning_effort",
    "response_format",
    "tools",
    "tool_choice",
}
LIST_SERVICES = step("tool_request", tool_calls=[call("list_services")])
GROQ_SCOPE = "api.groq.com"  # the ledger counts and stops by host, not by provider name


@pytest.fixture
def groq_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything the owner does before a live free call (with a fake key)."""
    monkeypatch.setenv(OPT_IN_ENV, "1")
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv(ATTESTATION_ENV, date.today().isoformat())  # the owner's local date


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record retry waits instead of sleeping through them."""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(openai_compatible.asyncio, "sleep", fake_sleep)
    return waits


def with_groq(settings: Settings, **changes: Any) -> Settings:
    providers = dict(settings.providers.providers)
    providers["groq"] = providers["groq"].model_copy(update=changes)
    config = settings.providers.model_copy(update={"providers": providers})
    return settings.model_copy(update={"providers": config})


def groq(settings: Settings, fake: FakeGroq, **changes: Any) -> HttpLLMProvider:
    provider = create_provider("groq", with_groq(settings, **changes), mock_http=fake.mock())
    assert isinstance(provider, HttpLLMProvider)
    return provider


def chat_requests(fake: FakeGroq) -> list[Any]:
    return [r for r in fake.requests if r.url.path.endswith("/chat/completions")]


# --- nothing is sent unless every check passes ---------------------------------------------


@pytest.mark.parametrize(
    ("unset", "message"),
    [
        (OPT_IN_ENV, "RECAP_ALLOW_LIVE_FREE_INFERENCE=1"),
        ("GROQ_API_KEY", "set GROQ_API_KEY"),
        (ATTESTATION_ENV, "must confirm the free plan"),
    ],
)
def test_without_every_owner_step_nothing_is_built_or_sent(
    settings: Settings, groq_env: None, monkeypatch: pytest.MonkeyPatch, unset: str, message: str
) -> None:
    monkeypatch.delenv(unset)
    fake = FakeGroq([LIST_SERVICES])
    with pytest.raises(ConfigError, match=message):
        create_provider("groq", settings, mock_http=fake.mock())
    assert fake.requests == []


@pytest.mark.parametrize(
    ("checked", "message"),
    [(8, "8 days old"), (-1, "in the future")],
)
def test_a_stale_or_future_free_plan_confirmation_is_refused(
    settings: Settings, groq_env: None, monkeypatch: pytest.MonkeyPatch, checked: int, message: str
) -> None:
    day = (date.today() - timedelta(days=checked)).isoformat()
    monkeypatch.setenv(ATTESTATION_ENV, day)
    with pytest.raises(ConfigError, match=message):
        create_provider("groq", settings, mock_http=FakeGroq().mock())


def test_a_model_outside_the_free_allowlist_cannot_be_configured(settings: Settings) -> None:
    entry = settings.providers.providers["groq"].model_dump()
    with pytest.raises(ValidationError, match="not in free_tier.allowed_models"):
        ProviderSettings.model_validate({**entry, "model": "llama-3.3-70b-versatile"})


async def test_the_model_must_be_listed_before_any_inference(
    settings: Settings, groq_env: None
) -> None:
    fake = FakeGroq([LIST_SERVICES], model="openai/gpt-oss-20b")  # the server lacks our model
    provider = groq(settings, fake)
    with pytest.raises(ProviderError, match="not offered"):
        await provider.next_action(investigation_request(settings))
    assert chat_requests(fake) == []


# --- what is sent ---------------------------------------------------------------------------


async def test_the_request_has_only_allowlisted_fields_and_strict_json(
    settings: Settings, groq_env: None
) -> None:
    fake = FakeGroq([LIST_SERVICES])
    request = investigation_request(settings)
    decision = await groq(settings, fake).next_action(request)

    [body] = fake.chats
    assert set(body) <= ALLOWED_FIELDS  # never service_tier, models, stream, n, logprobs, ...
    assert body["model"] == "openai/gpt-oss-120b" and body["reasoning_effort"] == "low"
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == protocol.for_strict_outputs(
        protocol.step_schema(request)
    )
    assert "maxItems" not in json.dumps(response_format)
    assert decision.tool_calls[0].tool_name == "list_services"


async def test_a_recording_keeps_the_rate_limits_but_no_request_id(
    settings: Settings, groq_env: None
) -> None:
    """A provider's request ID could link a published recording to the owner's account."""
    fake = FakeGroq([LIST_SERVICES])
    provider = groq(settings, fake)
    await provider.next_action(investigation_request(settings))
    assert provider.info.recording is not None
    text = (settings.paths.root / provider.info.recording).read_text(encoding="utf-8")
    assert "x-ratelimit-limit-requests" in text  # the tripwire's evidence is kept
    for linkable in (FAKE_REQUEST_ID, FAKE_COMPLETION_ID, "x-request-id", "system_fingerprint"):
        assert linkable not in text, linkable
    assert '"prompt_tokens": 900' in text  # what the provider reads is still there


async def test_tools_mode_requires_a_tool_call(settings: Settings, groq_env: None) -> None:
    fake = FakeGroq([tool_answer(("list_services", {}))])
    decision = await groq(settings, fake, mode="tools").next_action(investigation_request(settings))
    [body] = fake.chats
    assert body["tool_choice"] == "required" and "response_format" not in body
    assert decision.tool_calls[0].tool_name == "list_services"


async def test_the_key_goes_on_the_wire_and_nowhere_else(
    settings: Settings, groq_env: None
) -> None:
    fake = FakeGroq([LIST_SERVICES])
    provider = groq(settings, fake)
    decision = await provider.next_action(investigation_request(settings))

    [chat] = chat_requests(fake)
    assert chat.headers["authorization"] == f"Bearer {FAKE_GROQ_KEY}"
    recorded = (settings.paths.root / provider.info.recording).read_text()
    assert FAKE_GROQ_KEY not in recorded and "authorization" not in recorded.lower()
    assert FAKE_GROQ_KEY not in decision.model_dump_json()
    assert FAKE_GROQ_KEY not in provider.info.model_dump_json()


async def test_exact_usage_and_the_limit_headers_are_kept(
    settings: Settings, groq_env: None
) -> None:
    fake = FakeGroq([LIST_SERVICES])
    decision = await groq(settings, fake).next_action(investigation_request(settings))

    usage = decision.usage
    assert (usage.actual_input_tokens, usage.actual_output_tokens) == (900, 120)
    assert usage.model_requests == 1 and usage.is_estimate is False
    record = decision.model_calls[0]
    assert record.origin == "free_hosted_llm"
    assert record.rate_limit["rpd_limit"] == "1000"
    assert "$0.00 - free plan" in cost_label(usage, "free_hosted_llm")


async def test_a_proposal_is_strict_json_too(settings: Settings, groq_env: None) -> None:
    deploys = evidence("ev-abc123-001", "mcp:get_recent_deployments(service=checkout)")
    fake = FakeGroq([proposal(["ev-abc123-001"])])
    decision = await groq(settings, fake).propose_mitigation(proposal_request(settings, [deploys]))
    assert fake.chats[0]["response_format"]["json_schema"]["strict"] is True
    assert decision.draft is not None and decision.draft.target == "checkout"


# --- the tripwire: a response that does not look free stops the provider -------------------


@pytest.mark.parametrize(
    ("fake", "reason"),
    [
        (FakeGroq([LIST_SERVICES], daily_limit="500000"), "is '500000'"),
        (FakeGroq([LIST_SERVICES], headers=False), "without x-ratelimit-limit-requests"),
        (FakeGroq([error(402, "payment_required")]), "HTTP 402"),
        (FakeGroq([error(400, "blocked_api_access")]), "blocked_api_access"),
    ],
    ids=["paid-plan-limit", "no-header", "402", "blocked"],
)
async def test_a_response_that_does_not_look_free_stops_the_provider(
    settings: Settings, groq_env: None, fake: FakeGroq, reason: str
) -> None:
    provider = groq(settings, fake)
    with pytest.raises(ProviderError, match="free status is no longer verified"):
        await provider.next_action(investigation_request(settings))
    assert len(chat_requests(fake)) == 1  # nothing after the response that tripped it
    lost = provider.free_status_lost  # type: ignore[attr-defined]
    assert reason in (lost or "")
    assert "cost unknown" in cost_label(Usage(), "free_hosted_llm", free_status_lost=lost)

    # The stop is remembered: a later run is refused before it can send anything...
    later = FakeGroq([LIST_SERVICES])
    with pytest.raises(ConfigError, match="was stopped"):
        create_provider("groq", settings, mock_http=later.mock())
    assert later.requests == []
    # ...until the owner checks the billing page and clears it.
    assert UsageLedger(settings.paths.usage_ledger).clear_stop(GROQ_SCOPE) is True
    create_provider("groq", settings, mock_http=later.mock())


async def test_an_answer_from_another_model_is_refused(settings: Settings, groq_env: None) -> None:
    fake = FakeGroq([LIST_SERVICES], answered_by="openai/gpt-oss-20b")
    with pytest.raises(ProviderError, match="not openai/gpt-oss-120b"):
        await groq(settings, fake).next_action(investigation_request(settings))


# --- 429 and other errors: bounded, never a switch to another provider ----------------------


async def test_a_short_429_is_retried_once(
    settings: Settings, groq_env: None, no_wait: list[float]
) -> None:
    fake = FakeGroq([error(429, "rate_limit_exceeded", **{"retry-after": "2"}), LIST_SERVICES])
    decision = await groq(settings, fake).next_action(investigation_request(settings))
    assert no_wait == [2.0]
    assert len(chat_requests(fake)) == 2 and decision.action == "call_tools"


@pytest.mark.parametrize(
    "answers",
    [
        [error(429, "rate_limit_exceeded", **{"retry-after": "120"})],  # too long to wait
        [error(429, "rate_limit_exceeded")],  # no retry-after at all
        [error(429, "rate_limit_exceeded", **{"retry-after": "1"})] * 2,  # still limited
    ],
    ids=["long-wait", "no-retry-after", "twice"],
)
async def test_a_quota_429_defers_the_run(
    settings: Settings, groq_env: None, no_wait: list[float], answers: list[Any]
) -> None:
    fake = FakeGroq(answers)
    with pytest.raises(QuotaExhausted, match="429"):
        await groq(settings, fake).next_action(investigation_request(settings))
    assert len(chat_requests(fake)) == len(answers)


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refusal_is_final(
    settings: Settings, groq_env: None, no_wait: list[float], status: int
) -> None:
    fake = FakeGroq([error(status, "invalid_api_key"), LIST_SERVICES])
    with pytest.raises(ProviderError, match=f"HTTP {status}"):
        await groq(settings, fake).next_action(investigation_request(settings))
    assert len(chat_requests(fake)) == 1 and no_wait == []


async def test_a_server_error_is_retried_once(
    settings: Settings, groq_env: None, no_wait: list[float]
) -> None:
    fake = FakeGroq([error(503, "service_unavailable"), LIST_SERVICES])
    decision = await groq(settings, fake).next_action(investigation_request(settings))
    assert decision.action == "call_tools" and len(chat_requests(fake)) == 2


async def test_a_schema_failure_is_an_unusable_answer_not_a_crash(
    settings: Settings, groq_env: None
) -> None:
    fake = FakeGroq([error(400, "json_validate_failed"), LIST_SERVICES])
    decision = await groq(settings, fake).next_action(investigation_request(settings))
    assert [c.ok for c in decision.model_calls] == [False, True]
    assert decision.action == "call_tools"


# --- RECAP's own caps stop it before the provider has to -------------------------------------


async def test_the_per_run_cap_is_enforced_before_sending(
    settings: Settings, groq_env: None
) -> None:
    caps = settings.providers.providers["groq"].free_tier.caps.model_copy(  # type: ignore[union-attr]
        update={"requests_per_run": 1}
    )
    free_tier = settings.providers.providers["groq"].free_tier.model_copy(update={"caps": caps})  # type: ignore[union-attr]
    fake = FakeGroq([LIST_SERVICES, LIST_SERVICES])
    provider = groq(settings, fake, free_tier=free_tier)

    await provider.next_action(investigation_request(settings))
    with pytest.raises(QuotaExhausted, match="1 requests per run"):
        await provider.next_action(investigation_request(settings))
    assert len(chat_requests(fake)) == 1


async def test_a_full_minute_cap_is_waited_out_not_exceeded(
    settings: Settings, groq_env: None, no_wait: list[float]
) -> None:
    """Another RECAP process used this minute's requests: wait for the window, never exceed it."""
    ledger = UsageLedger(settings.paths.usage_ledger)
    for _ in range(25):
        ledger.reserve(GROQ_SCOPE, "openai/gpt-oss-120b", 10)
    fake = FakeGroq([LIST_SERVICES])
    provider = groq(settings, fake)

    # Sleeping is faked, so real time barely moves and the window never frees up: RECAP
    # waits (up to a minute at a time) until its pacing limit, then defers the run instead
    # of sending anyway. Each wait is "until the oldest request leaves the minute window".
    with pytest.raises(QuotaExhausted, match="per-minute cap is full"):
        await provider.next_action(investigation_request(settings))
    assert chat_requests(fake) == []
    assert no_wait and all(0 < wait <= 60 for wait in no_wait)
    assert sum(no_wait) <= openai_compatible.MAX_PACING_SECONDS


async def test_the_daily_cap_defers_without_waiting(
    settings: Settings, groq_env: None, no_wait: list[float]
) -> None:
    ledger = UsageLedger(settings.paths.usage_ledger)
    earlier = utcnow() - timedelta(hours=2)
    for _ in range(800):
        ledger.reserve(GROQ_SCOPE, "openai/gpt-oss-120b", 10, now=earlier)
    fake = FakeGroq([LIST_SERVICES])
    with pytest.raises(QuotaExhausted, match="requests in the last 24 hours"):
        await groq(settings, fake).next_action(investigation_request(settings))
    assert chat_requests(fake) == [] and no_wait == []


def test_the_redactor_hides_groq_keys() -> None:
    text = f"request failed with key {FAKE_GROQ_KEY} in it"
    assert FAKE_GROQ_KEY not in Redactor(environ={}).redact_text(text)


async def test_the_caps_count_a_typical_answer_not_the_largest(
    settings: Settings, groq_env: None
) -> None:
    """Reserving max_output_tokens per request let only one request through a minute; RECAP
    reserves the prompt plus reserved_output_tokens, and the real count replaces it."""
    fake = FakeGroq([error(400, "json_validate_failed"), LIST_SERVICES])
    await groq(settings, fake).next_action(investigation_request(settings))

    # The schema failure keeps its reservation (it may have generated tokens); the second
    # request's reservation was replaced by its reported 900 + 120 tokens.
    _, tokens = UsageLedger(settings.paths.usage_ledger).window(
        GROQ_SCOPE, utcnow() - timedelta(minutes=1)
    )
    reserved = tokens - 1020
    free_tier = settings.providers.providers["groq"].free_tier
    assert free_tier is not None
    expected = estimate_tokens(canonical_json(fake.chats[0])) + free_tier.reserved_output_tokens
    assert reserved == expected
    assert free_tier.reserved_output_tokens < settings.providers.providers["groq"].max_output_tokens
