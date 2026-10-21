"""Tests for the provider catalog behind GET /providers: honest status, no secrets, no calls."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from recap.core.config import Settings
from recap.providers.catalog import provider_status
from recap.providers.factory import PAID_OPT_IN_ENV, REPLAY_ENV
from recap.providers.quota import UsageLedger
from recap.providers.transport import RECORDING_SCHEMA
from recap.runtime import Runtime
from tests.helpers import OPERATOR_HEADERS
from tests.llm_fakes import FAKE_GROQ_KEY, FakeOllama


def owner_ready() -> dict[str, str]:
    """The owner's steps, dated now (the owner's local date, as the guard counts it)."""
    return {
        "GROQ_API_KEY": FAKE_GROQ_KEY,
        "RECAP_GROQ_FREE_PLAN_CONFIRMED_ON": date.today().isoformat(),
    }


async def statuses(
    settings: Settings, environ: dict[str, str], fake: FakeOllama | None = None
) -> dict[str, dict]:
    ledger = UsageLedger(settings.paths.usage_ledger)
    mock = (fake or FakeOllama()).mock()
    entries = await provider_status(settings, ledger, environ=environ, mock_http=mock)
    return {entry["name"]: entry for entry in entries}


async def test_every_configured_provider_is_listed_with_its_origin(settings: Settings) -> None:
    found = await statuses(settings, {})
    assert {name: entry["origin"] for name, entry in found.items()} == {
        "deterministic": "scripted",
        "ollama": "local_llm",
        "groq": "free_hosted_llm",
        "replay": "replay",
        "anthropic": "paid_llm",
    }
    assert found["deterministic"]["status"] == "ready" and found["deterministic"]["default"]
    assert found["anthropic"]["status"] == "disabled"


async def test_a_local_model_must_already_be_installed(settings: Settings) -> None:
    ready = await statuses(settings, {}, FakeOllama())
    assert ready["ollama"]["status"] == "ready"
    assert "qwen3:8b" in ready["ollama"]["installed"]

    missing = await statuses(settings, {}, FakeOllama(installed=False))
    assert missing["ollama"]["status"] == "needs_setup"
    assert "never downloads" in missing["ollama"]["detail"]


async def test_no_local_server_is_reported_not_guessed(settings: Settings) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    ledger = UsageLedger(settings.paths.usage_ledger)
    entries = await provider_status(
        settings, ledger, environ={}, mock_http=httpx.MockTransport(refuse)
    )
    ollama = next(e for e in entries if e["name"] == "ollama")
    assert ollama["status"] == "unavailable" and "ollama serve" in ollama["detail"]


async def test_the_hosted_provider_shows_each_missing_owner_step(settings: Settings) -> None:
    nothing = (await statuses(settings, {}))["groq"]
    assert (nothing["status"], nothing["key_present"]) == ("needs_setup", False)

    key_only = (await statuses(settings, {"GROQ_API_KEY": FAKE_GROQ_KEY}))["groq"]
    assert key_only["status"] == "needs_setup" and "re-check the free plan" in key_only["detail"]

    ready = (await statuses(settings, owner_ready()))["groq"]
    assert ready["status"] == "ready" and ready["confirmation_age_days"] == 0
    assert "RECAP_ALLOW_LIVE_FREE_INFERENCE=1" in ready["detail"]  # still a per-run opt-in


async def test_a_stopped_provider_says_why(settings: Settings) -> None:
    UsageLedger(settings.paths.usage_ledger).stop(
        "api.groq.com", "x-ratelimit-limit-requests is '500000'"
    )
    groq = (await statuses(settings, owner_ready()))["groq"]
    assert groq["status"] == "stopped" and "500000" in groq["detail"]


async def test_the_key_value_is_never_reported(settings: Settings) -> None:
    found = await statuses(settings, owner_ready())
    assert found["groq"]["key_present"] is True
    assert FAKE_GROQ_KEY not in json.dumps(found)


async def test_the_paid_adapter_is_off_unless_the_owner_opts_in(settings: Settings) -> None:
    assert (await statuses(settings, {}))["anthropic"]["status"] == "disabled"
    assert (await statuses(settings, {PAID_OPT_IN_ENV: "1"}))["anthropic"]["status"] == "ready"


async def test_a_replay_names_what_it_replays(settings: Settings, tmp_path: Path) -> None:
    recording = tmp_path / "rec.jsonl"
    header = {
        "type": "header",
        "schema": RECORDING_SCHEMA,
        "original_provider": "ollama",
        "model": "qwen3:8b",
        "created_at": "2026-10-21T10:00:00+00:00",
        "prompt_version": "prompts-v4",
    }
    recording.write_text(json.dumps(header) + "\n")
    found = await statuses(settings, {REPLAY_ENV: str(recording)})
    assert found["replay"]["status"] == "ready"
    assert found["replay"]["replay_of"]["model"] == "qwen3:8b"

    missing = await statuses(settings, {REPLAY_ENV: str(tmp_path / "none.jsonl")})
    assert missing["replay"]["status"] == "unavailable"


def test_the_api_serves_the_catalog_without_a_token(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only and secret-free, like the other GET endpoints on localhost. The local Ollama
    probe is answered by a fake, so this test touches no real server."""
    import apps.api.main as api_main

    real = api_main.provider_status

    async def with_fake_ollama(settings_: Settings, ledger: UsageLedger, **kwargs: object) -> list:
        return await real(settings_, ledger, mock_http=FakeOllama().mock())

    monkeypatch.setattr(api_main, "provider_status", with_fake_ollama)
    with TestClient(create_app(Runtime(settings))) as client:
        response = client.get("/providers")
    assert response.status_code == 200
    names = [entry["name"] for entry in response.json()]
    assert names[0] == "deterministic" and "groq" in names
    with TestClient(create_app(Runtime(settings)), headers=OPERATOR_HEADERS) as client:
        assert client.get("/providers").status_code == 200
