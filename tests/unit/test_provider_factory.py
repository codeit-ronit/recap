"""Tests for the provider factory: honest default, opt-in LLM, no silent fallback.

No network: the Claude adapter is either refused (missing SDK or key) or given a fake
client. The SDK itself is replaced in sys.modules where a test needs it to be present
or missing, so these tests behave the same with or without the llm extra installed.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace

import pytest

from recap.core.config import ProvidersConfig, ProviderSettings, Settings
from recap.core.errors import ConfigError
from recap.core.models import (
    AgentRole,
    ContextPack,
    Incident,
    InvestigationRequest,
    RunBudget,
)
from recap.providers.anthropic_provider import AnthropicProvider
from recap.providers.deterministic import DeterministicProvider
from recap.providers.factory import create_provider, describe_providers
from recap.providers.prompts import InvestigationOutput

KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class FakeClient:
    """Minimal stand-in for anthropic.AsyncAnthropic that always answers 'finish'."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    async def _parse(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        output = InvestigationOutput(
            action="finish", tool_calls=[], hypotheses=[], summary="done", next_steps=[]
        )
        return SimpleNamespace(
            parsed_output=output,
            stop_reason="end_turn",
            stop_details=None,
            model="claude-opus-5-5",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


@pytest.fixture
def no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)


def _request() -> InvestigationRequest:
    incident = Incident(
        incident_id="inc-1",
        scenario_id="bad-deployment",
        service="checkout",
        symptoms=["checkout: 5xx above 30%"],
        created_at=datetime(2026, 3, 14, 10, 0, tzinfo=UTC),
        environment="simulated",
        budget=RunBudget(),
    )
    return InvestigationRequest(
        role=AgentRole.INVESTIGATOR,
        goal="Investigate.",
        incident=incident,
        context=ContextPack(budget_tokens=3000, tokens_before=0, tokens_after=0),
        step=1,
        max_steps=4,
    )


# --- the honest default -----------------------------------------------------------------


def test_deterministic_scripted_policy_is_the_default(settings: Settings) -> None:
    deterministic = pytest.importorskip("recap.providers.deterministic")
    assert settings.providers.default == "deterministic"
    provider = create_provider(settings.providers.default, settings)
    assert isinstance(provider, deterministic.DeterministicProvider)
    assert provider.info.kind == "scripted"  # not an LLM


def test_deterministic_provider_gets_the_familiar_threshold_from_the_config(
    settings: Settings,
) -> None:
    # Regression: configs/budget.yaml changed routing but not the memory shortcut.
    routing = settings.budget.routing.model_copy(update={"familiar_similarity": 0.85})
    budget = settings.budget.model_copy(update={"routing": routing})

    provider = create_provider("deterministic", settings.model_copy(update={"budget": budget}))

    assert isinstance(provider, DeterministicProvider)
    assert provider.familiar_similarity == 0.85


def test_unknown_provider_is_refused(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="unknown provider 'gpt-9'"):
        create_provider("gpt-9", settings)


def test_unknown_planner_is_refused(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="unknown planner 'evil'"):
        create_provider("anthropic", settings, planner="evil", client=FakeClient())


# --- the opt-in Claude adapter ----------------------------------------------------------


def test_anthropic_without_an_api_key_is_refused(
    settings: Settings, no_api_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pretend the SDK is installed, so the API-key check is what refuses.
    monkeypatch.setitem(sys.modules, "anthropic", ModuleType("anthropic"))
    with pytest.raises(ConfigError, match="opt-in") as refused:
        create_provider("anthropic", settings)
    assert "ANTHROPIC_API_KEY" in str(refused.value)


def test_anthropic_without_the_sdk_is_refused(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setitem(sys.modules, "anthropic", None)  # makes `import anthropic` fail
    with pytest.raises(ConfigError, match=r"pip install -e '\.\[llm\]'"):
        create_provider("anthropic", settings)


def test_anthropic_with_sdk_and_key_builds_without_calling_the_api(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setitem(sys.modules, "anthropic", ModuleType("anthropic"))  # no AsyncAnthropic
    provider = create_provider("anthropic", settings)
    # The SDK client is created lazily on the first decision, so nothing ran yet.
    assert isinstance(provider, AnthropicProvider)
    assert provider.info.kind == "llm"


async def test_anthropic_with_an_injected_client_works_without_key_or_sdk(
    settings: Settings, no_api_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    client = FakeClient()
    provider = create_provider("anthropic", settings, client=client)
    assert provider.info.name == "anthropic:claude-opus-5-5"
    decision = await provider.next_action(_request())
    assert decision.action == "finish"
    assert len(client.calls) == 1


def test_anthropic_needs_its_config_entry(settings: Settings) -> None:
    only_scripted = ProvidersConfig(providers={"deterministic": ProviderSettings(kind="scripted")})
    trimmed = settings.model_copy(update={"providers": only_scripted})
    with pytest.raises(ConfigError, match="not configured"):
        create_provider("anthropic", trimmed, client=FakeClient())


# --- red-team planner -------------------------------------------------------------------


def test_prompt_injected_planner_wraps_the_chosen_provider(settings: Settings) -> None:
    redteam = pytest.importorskip("recap.safety.redteam")
    provider = create_provider(
        "anthropic", settings, planner="prompt_injected", client=FakeClient()
    )
    assert isinstance(provider, redteam.PromptInjectedProvider)
    assert provider.info.name.startswith("anthropic:claude-opus-5-5")
    assert "prompt-injected" in provider.info.name


# --- describing providers ---------------------------------------------------------------


def test_describe_providers_needs_no_key_and_builds_nothing(
    settings: Settings, no_api_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    described = {entry["name"]: entry for entry in describe_providers(settings)}
    assert set(described) == {"deterministic", "anthropic"}
    for entry in described.values():
        assert set(entry) == {"name", "kind", "model", "description", "requires_api_key"}
    assert described["deterministic"]["kind"] == "scripted"
    assert described["deterministic"]["requires_api_key"] is False
    assert described["anthropic"]["kind"] == "llm"
    assert described["anthropic"]["model"] == "claude-opus-5-5"
    assert described["anthropic"]["requires_api_key"] is True
