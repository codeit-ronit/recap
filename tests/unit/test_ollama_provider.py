"""Tests for OllamaProvider against a local protocol fixture (tests/llm_fakes.py: FakeOllama).

The provider's real HTTP code runs; only the network is replaced by httpx.MockTransport. No
model is loaded and nothing leaves the machine. Live measurements with a real qwen3:8b are
in docs/status.md (they are not part of the automated suite).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from recap.core.config import Settings
from recap.core.errors import ConfigError, ProviderError
from recap.providers import protocol
from recap.providers.factory import create_provider
from recap.providers.http_llm import HttpLLMProvider
from recap.providers.quota import UsageLedger
from recap.providers.transport import Recording
from tests.llm_fakes import (
    FakeOllama,
    call,
    evidence,
    hypothesis,
    investigation_request,
    proposal,
    proposal_request,
    step,
    tool_answer,
)

DEPLOYS = evidence("ev-abc123-001", "mcp:get_recent_deployments(service=checkout)", "v2.4.1")


def with_ollama(settings: Settings, **changes: Any) -> Settings:
    """Settings whose 'ollama' entry has `changes` applied."""
    providers = dict(settings.providers.providers)
    providers["ollama"] = providers["ollama"].model_copy(update=changes)
    config = settings.providers.model_copy(update={"providers": providers})
    return settings.model_copy(update={"providers": config})


def ollama(settings: Settings, fake: FakeOllama, **changes: Any) -> HttpLLMProvider:
    provider = create_provider("ollama", with_ollama(settings, **changes), mock_http=fake.mock())
    assert isinstance(provider, HttpLLMProvider)
    return provider


# --- a decision, end to end over the protocol ----------------------------------------------


async def test_a_tools_mode_step_sends_the_real_chat_request(settings: Settings) -> None:
    fake = FakeOllama([tool_answer(("get_service_health", {"service": "checkout"}))])
    provider = ollama(settings, fake)

    decision = await provider.next_action(investigation_request(settings))

    [body] = fake.chats
    assert body["model"] == "qwen3:8b" and body["stream"] is False and body["think"] is False
    assert body["options"] == {"temperature": 0.0, "seed": 7, "num_ctx": 16384, "num_predict": 1024}
    assert "format" not in body
    assert [t["function"]["name"] for t in body["tools"]][-1] == protocol.FINISH_TOOL
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert decision.action == "call_tools"
    assert decision.tool_calls[0].args == {"service": "checkout"}


async def test_the_decision_carries_exact_usage_and_a_model_call_record(settings: Settings) -> None:
    fake = FakeOllama([tool_answer(("list_services", {}))], prompt_tokens=1407, output_tokens=57)
    provider = ollama(settings, fake)

    decision = await provider.next_action(investigation_request(settings))

    usage = decision.usage
    assert (usage.actual_input_tokens, usage.actual_output_tokens) == (1407, 57)
    assert usage.is_estimate is False and usage.model_requests == 1 and usage.unreported_calls == 0
    [record] = decision.model_calls
    assert (record.provider, record.model, record.model_reported) == (
        "ollama",
        "qwen3:8b",
        "qwen3:8b",
    )
    assert (record.origin, record.mode, record.attempt, record.ok) == (
        "local_llm",
        "tools",
        1,
        True,
    )
    assert record.response_kind == "tool_request" and record.replayed is False
    assert len(record.prompt_sha256) == 64 and record.prompt_chars > 1000
    assert "list_services" in record.output_excerpt


async def test_json_mode_sends_the_requests_own_schema(settings: Settings) -> None:
    fake = FakeOllama([step("tool_request", tool_calls=[call("list_services")])])
    provider = ollama(settings, fake, mode="json")
    request = investigation_request(settings)

    decision = await provider.next_action(request)

    assert fake.chats[0]["format"] == protocol.step_schema(request)
    assert "tools" not in fake.chats[0]
    assert decision.tool_calls[0].tool_name == "list_services"


async def test_a_proposal_always_uses_a_json_schema(settings: Settings) -> None:
    fake = FakeOllama([proposal(["ev-abc123-001"])])
    provider = ollama(settings, fake)  # tools mode for investigation steps
    request = proposal_request(settings, [DEPLOYS])

    decision = await provider.propose_mitigation(request)

    assert fake.chats[0]["format"] == protocol.proposal_schema(request)
    assert decision.draft is not None and decision.draft.params == {"to_version": "v2.4.0"}
    assert decision.model_calls[0].purpose == "propose"


async def test_the_server_is_checked_once_before_the_first_request(settings: Settings) -> None:
    fake = FakeOllama([tool_answer(("list_services", {}))] * 2)
    provider = ollama(settings, fake)

    await provider.next_action(investigation_request(settings))
    await provider.next_action(investigation_request(settings))

    paths = [r.url.path for r in fake.requests]
    assert paths == ["/api/version", "/api/show", "/api/chat", "/api/chat"]
    assert provider.capabilities["context_length"] == 40960


# --- refusals before any inference ---------------------------------------------------------


async def test_a_model_that_is_not_installed_is_never_downloaded(settings: Settings) -> None:
    fake = FakeOllama(installed=False)
    provider = ollama(settings, fake)

    with pytest.raises(ProviderError, match="never downloads models"):
        await provider.next_action(investigation_request(settings))

    paths = {r.url.path for r in fake.requests}
    assert "/api/chat" not in paths and "/api/pull" not in paths


async def test_tools_mode_needs_a_model_that_can_call_tools(settings: Settings) -> None:
    fake = FakeOllama(capabilities=["completion"])
    with pytest.raises(ProviderError, match="cannot call tools"):
        await ollama(settings, fake).next_action(investigation_request(settings))
    assert fake.chats == []


async def test_thinking_needs_a_thinking_model(settings: Settings) -> None:
    fake = FakeOllama(capabilities=["completion", "tools"])
    with pytest.raises(ProviderError, match="no thinking mode"):
        await ollama(settings, fake, think=True).next_action(investigation_request(settings))


async def test_the_context_window_must_fit_the_model(settings: Settings) -> None:
    fake = FakeOllama(context_length=8192)
    with pytest.raises(ProviderError, match="larger than"):
        await ollama(settings, fake).next_action(investigation_request(settings))


async def test_a_prompt_that_may_not_fit_is_refused_before_it_is_sent(settings: Settings) -> None:
    """Ollama would silently cut the START of the prompt, where RECAP's safety rules are."""
    fake = FakeOllama(context_length=40960)
    provider = ollama(settings, fake, context_tokens=2048)
    with pytest.raises(ProviderError, match="may not fit"):
        await provider.next_action(investigation_request(settings))
    assert fake.chats == []


async def test_an_answer_whose_prompt_filled_the_window_is_rejected(settings: Settings) -> None:
    fake = FakeOllama([{**tool_answer(("list_services", {})), "prompt_tokens": 16384}])
    with pytest.raises(ProviderError, match="filled the whole context window"):
        await ollama(settings, fake).next_action(investigation_request(settings))


@pytest.mark.parametrize("url", ["http://192.168.1.20:11434", "https://ollama.example.com"])
def test_only_a_loopback_server_is_accepted(settings: Settings, url: str) -> None:
    with pytest.raises(ConfigError, match="local-only"):
        create_provider("ollama", with_ollama(settings, base_url=url))


def test_ollama_cloud_models_are_refused(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="cloud model"):
        create_provider("ollama", with_ollama(settings, model="gpt-oss:120b-cloud"))


# --- answers that cannot be used -----------------------------------------------------------


async def test_an_unusable_answer_gets_one_corrective_retry(settings: Settings) -> None:
    fake = FakeOllama(
        [
            {"content": "I would look at the logs."},  # text instead of a tool call
            tool_answer(("get_service_logs", {"service": "checkout"})),
        ]
    )
    decision = await ollama(settings, fake).next_action(investigation_request(settings))

    assert len(fake.chats) == 2
    correction = fake.chats[1]["messages"][-1]
    assert (
        correction["role"] == "user" and "could not use your last answer" in correction["content"]
    )
    assert decision.action == "call_tools"
    assert [c.ok for c in decision.model_calls] == [False, True]
    assert [c.attempt for c in decision.model_calls] == [1, 2]
    assert decision.usage.model_requests == 2


async def test_no_usable_answer_means_no_decision_never_a_scripted_one(settings: Settings) -> None:
    # A valid third answer waits: a third attempt (beyond max_attempts) would take it.
    fake = FakeOllama(
        [{"content": "hmm"}, {"content": "still prose"}, tool_answer(("list_services", {}))]
    )
    decision = await ollama(settings, fake).next_action(investigation_request(settings))
    assert len(fake.chats) == 2

    assert decision.action == "finish" and decision.hypotheses == [] and decision.tool_calls == []
    assert decision.summary.startswith("no usable answer from ollama:qwen3:8b")
    assert all(not c.ok for c in decision.model_calls)


async def test_no_usable_proposal_means_no_draft(settings: Settings) -> None:
    bad = proposal(["ev-made-up-1"])  # cites evidence that does not exist
    fake = FakeOllama([bad, bad])
    decision = await ollama(settings, fake).propose_mitigation(
        proposal_request(settings, [DEPLOYS])
    )
    assert decision.draft is None
    assert "cite at least one evidence ID" in decision.notes[0]


async def test_a_cut_off_answer_is_noted(settings: Settings) -> None:
    final = hypothesis("checkout", ["ev-abc123-001"])
    fake = FakeOllama([{**step("hypothesis", hypotheses=[final]), "done_reason": "length"}])
    provider = ollama(settings, fake, mode="json")
    decision = await provider.next_action(investigation_request(settings, seen=[DEPLOYS]))
    assert "cut off at max_output_tokens" in decision.model_calls[0].notes[0]


# --- server trouble ------------------------------------------------------------------------


async def test_an_answer_from_another_model_is_refused(settings: Settings) -> None:
    fake = FakeOllama([tool_answer(("list_services", {}))], answered_by="llama3.2:3b")
    with pytest.raises(ProviderError, match="not the requested qwen3:8b"):
        await ollama(settings, fake).next_action(investigation_request(settings))


async def test_a_server_error_fails_the_decision(settings: Settings) -> None:
    fake = FakeOllama([httpx.Response(500, json={"error": "out of memory"})])
    with pytest.raises(ProviderError, match="HTTP 500: out of memory"):
        await ollama(settings, fake).next_action(investigation_request(settings))


async def test_an_unreachable_server_fails_with_a_clear_message(settings: Settings) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = create_provider("ollama", settings, mock_http=httpx.MockTransport(refuse))
    with pytest.raises(ProviderError, match="could not reach 127.0.0.1:11434"):
        await provider.next_action(investigation_request(settings))


# --- what is kept: recording and usage ledger ------------------------------------------------


async def test_every_exchange_is_recorded_for_replay(settings: Settings) -> None:
    fake = FakeOllama([tool_answer(("list_services", {}))])
    provider = ollama(settings, fake)
    await provider.next_action(investigation_request(settings))

    assert provider.info.recording is not None
    recording = Recording.load(settings.paths.root / provider.info.recording)
    assert recording.header["original_provider"] == "ollama"
    assert recording.header["model"] == "qwen3:8b"
    assert recording.header["prompt_version"] == protocol.PROMPT_VERSION
    paths = [e["path"] for e in recording.exchanges]
    assert paths == ["/api/version", "/api/show", "/api/chat"]
    chat = recording.exchanges[-1]
    assert chat["request"]["model"] == "qwen3:8b" and chat["response"]["status"] == 200


async def test_local_requests_are_counted_in_the_usage_ledger(settings: Settings) -> None:
    ledger = UsageLedger(settings.paths.usage_ledger)
    fake = FakeOllama([tool_answer(("list_services", {}))], prompt_tokens=100, output_tokens=20)
    provider = create_provider("ollama", settings, mock_http=fake.mock(), ledger=ledger)
    await provider.next_action(investigation_request(settings))

    from datetime import timedelta

    from recap.core.ids import utcnow

    assert ledger.window("127.0.0.1:11434", utcnow() - timedelta(minutes=1)) == (1, 120)


def test_the_provider_says_what_it_is(settings: Settings) -> None:
    provider = ollama(settings, FakeOllama())
    info = provider.info
    assert (info.name, info.kind, info.origin) == ("ollama:qwen3:8b", "llm", "local_llm")
    assert (info.mode, info.prompt_version) == ("tools", protocol.PROMPT_VERSION)
    assert json.loads(info.model_dump_json())["replay_of"] is None
