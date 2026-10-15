"""Provider factory: builds the decision provider a run asked for, by name.

Where it sits: the composition root (recap/runtime.py) calls create_provider(...) once per
run with the provider name from RunConfig or configs/providers.yaml (RECAP_PROVIDER can
override it). Each provider module is imported only when that provider is chosen, so the
default run never loads LLM code and red-team code loads only for red-team runs.

Two rules keep this honest:
  * The default is "deterministic", a scripted policy that is NOT an LLM.
  * There is no silent fallback. If the Claude adapter is requested but cannot run (SDK not
    installed, no API key), this raises ConfigError instead of quietly using another
    provider, so a run never claims to be something it is not.
"""

from __future__ import annotations

import os
from typing import Any

from recap.core.config import Settings
from recap.core.errors import ConfigError
from recap.core.interfaces import LLMProvider

KNOWN_PROVIDERS = ("deterministic", "anthropic")
KNOWN_PLANNERS = ("default", "prompt_injected")
API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def create_provider(
    name: str, settings: Settings, *, planner: str = "default", client: object | None = None
) -> LLMProvider:
    """Build the provider called `name`; `client` injects an SDK client (tests only)."""
    if planner not in KNOWN_PLANNERS:
        raise ConfigError(f"unknown planner '{planner}' (known: {', '.join(KNOWN_PLANNERS)})")
    if name == "deterministic":
        provider = _deterministic(settings)
    elif name == "anthropic":
        provider = _anthropic(settings, client)
    else:
        raise ConfigError(f"unknown provider '{name}' (known: {', '.join(KNOWN_PROVIDERS)})")
    if planner == "prompt_injected":
        provider = _red_team(provider)
    return provider


def describe_providers(settings: Settings) -> list[dict[str, Any]]:
    """What each configured provider is (for the API and dashboard). Builds and calls nothing."""
    return [
        {
            "name": name,
            "kind": spec.kind,
            "model": spec.model,
            "description": spec.description,
            "requires_api_key": spec.kind == "llm",
        }
        for name, spec in settings.providers.providers.items()
    ]


# --- builders ---------------------------------------------------------------------------


def _deterministic(settings: Settings) -> LLMProvider:
    from recap.providers.deterministic import DeterministicProvider

    # The memory shortcut must use the same "familiar" threshold as routing.
    return DeterministicProvider(familiar_similarity=settings.budget.routing.familiar_similarity)


def _anthropic(settings: Settings, client: object | None) -> LLMProvider:
    from recap.providers.anthropic_provider import AnthropicProvider, load_sdk

    spec = settings.providers.providers.get("anthropic")
    if spec is None:
        raise ConfigError("the 'anthropic' provider is not configured in configs/providers.yaml")
    if client is None:
        # A real API call is about to become possible: both opt-in checks must pass.
        load_sdk()
        _require_api_key()
    return AnthropicProvider(spec, client=client)


def _red_team(base: LLMProvider) -> LLMProvider:
    # RED-TEAM ONLY: a planner that obeys injected instructions, to prove the gateway holds.
    from recap.safety.redteam import PromptInjectedProvider

    return PromptInjectedProvider(base)


def _require_api_key() -> None:
    """Refuse to build the LLM provider unless the user set their own API credentials."""
    if not any(os.environ.get(var, "").strip() for var in API_KEY_ENV_VARS):
        raise ConfigError(
            "the 'anthropic' provider is opt-in: it calls the paid Claude API with your own "
            "API key and billing. Set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN), or use "
            "the default 'deterministic' provider (scripted, not an LLM, $0)."
        )
