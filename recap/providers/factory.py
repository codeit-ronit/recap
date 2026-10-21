"""Provider factory: builds the decision provider a run asked for, by name.

Where it sits: the composition root (recap/runtime.py) calls create_provider(...) once per
run with the provider name from RunConfig or configs/providers.yaml (RECAP_PROVIDER can
override it). What gets built depends on the entry's kind and origin:

    kind scripted                 DeterministicProvider (the default; NOT an LLM, $0)
    origin local_llm   (ollama)   OllamaProvider     over a live, recorded HTTP transport
    origin free_hosted_llm        FreeHostedProvider guarded by FreeTierGuard (quota.py)
    kind replay                   the recorded provider again, answering from a recording
    origin paid_llm               the optional Claude adapter: refused unless the owner sets
                                  RECAP_ALLOW_PAID_LLM=1 (this project's budget is $0)

Each provider module is imported only when it is chosen, so the default run never loads
LLM code, and red-team code loads only for red-team runs.

Two rules keep this honest:
  * There is no silent fallback. A provider that cannot run raises ConfigError here (or
    ProviderError on its first request); RECAP never quietly uses another provider, so a
    run never claims to be something it is not.
  * Every live LLM exchange is recorded under data/recordings/ (when `record` is on), so a
    run can be inspected or replayed later without calling the model again.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from recap import __version__
from recap.core.config import ProviderSettings, Settings
from recap.core.errors import ConfigError
from recap.core.ids import new_id, utcnow
from recap.core.interfaces import LLMProvider
from recap.providers.quota import UsageLedger
from recap.providers.transport import (
    LiveTransport,
    ModelTransport,
    Recorder,
    Recording,
    RecordingTransport,
    ReplayTransport,
    Trim,
)

KNOWN_PLANNERS = ("default", "prompt_injected")
API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
PAID_OPT_IN_ENV = "RECAP_ALLOW_PAID_LLM"
REPLAY_ENV = "RECAP_REPLAY_RECORDING"  # overrides the replay provider's recording path


def create_provider(
    name: str,
    settings: Settings,
    *,
    planner: str = "default",
    client: object | None = None,
    mock_http: httpx.MockTransport | None = None,
    ledger: UsageLedger | None = None,
) -> LLMProvider:
    """Build the provider called `name`.

    Tests only: `client` injects an SDK client, and `mock_http` stands in for the network
    (a local protocol fixture), so the real HTTP code still runs. `ledger` is the shared
    usage ledger (the runtime passes its own).
    """
    if planner not in KNOWN_PLANNERS:
        raise ConfigError(f"unknown planner '{planner}' (known: {', '.join(KNOWN_PLANNERS)})")
    spec = settings.providers.providers.get(name)
    if spec is None:
        known = ", ".join(settings.providers.providers)
        raise ConfigError(f"unknown provider '{name}' (configured: {known})")
    if spec.kind == "scripted":
        provider = _deterministic(settings)
    elif spec.kind == "replay":
        provider = _replay(spec, settings)
    elif spec.origin == "paid_llm":
        provider = _anthropic(spec, client)
    elif spec.api == "ollama":
        provider = _ollama(name, spec, settings, mock_http, ledger)
    elif spec.api == "openai_compatible":
        provider = _free_hosted(name, spec, settings, mock_http, ledger)
    else:
        raise ConfigError(f"provider '{name}' has no supported api (ollama, openai_compatible)")
    if planner == "prompt_injected":
        provider = _red_team(provider)
    return provider


def describe_providers(settings: Settings) -> list[dict[str, Any]]:
    """What each configured provider is (for the API and dashboard). Builds and calls nothing."""
    return [
        {
            "name": name,
            "kind": spec.kind,
            "origin": spec.origin,
            "model": spec.model,
            "mode": spec.mode if spec.kind == "llm" else None,
            "description": spec.description,
            "requires_api_key": spec.origin in ("free_hosted_llm", "paid_llm"),
        }
        for name, spec in settings.providers.providers.items()
    ]


# --- builders ---------------------------------------------------------------------------


def _deterministic(settings: Settings) -> LLMProvider:
    from recap.providers.deterministic import DeterministicProvider

    # The memory shortcut must use the same "familiar" threshold as routing.
    return DeterministicProvider(familiar_similarity=settings.budget.routing.familiar_similarity)


def _ollama(
    name: str,
    spec: ProviderSettings,
    settings: Settings,
    mock_http: httpx.MockTransport | None,
    ledger: UsageLedger | None,
) -> LLMProvider:
    from recap.providers.ollama_provider import OllamaProvider, check_local_settings

    check_local_settings(spec)  # loopback only, no cloud models: refuse before anything else
    from recap.providers.ollama_provider import trim_for_recording

    live = LiveTransport(spec.base_url or "", mock=mock_http)
    wired, recording = _maybe_record(name, spec, settings, live, trim_for_recording)
    return OllamaProvider(name, spec, wired, ledger=ledger, recording=recording)


def _free_hosted(
    name: str,
    spec: ProviderSettings,
    settings: Settings,
    mock_http: httpx.MockTransport | None,
    ledger: UsageLedger | None,
) -> LLMProvider:
    from recap.providers.openai_compatible import FreeHostedProvider
    from recap.providers.quota import FreeTierGuard

    assert spec.free_tier is not None  # ProviderSettings guarantees it for this origin
    guard = FreeTierGuard(name, spec.free_tier, ledger or UsageLedger(settings.paths.recap_db))
    guard.check_allowed(spec.model or "")  # opt-in, allowlist, key, attestation, not stopped

    def auth() -> dict[str, str]:
        # Read at send time and put only on the wire: never stored, traced or recorded.
        return {"Authorization": f"Bearer {guard.api_key()}"}

    from recap.providers.openai_compatible import trim_for_recording

    live = LiveTransport(spec.base_url or "", headers=auth, mock=mock_http)
    wired, recording = _maybe_record(
        name, spec, settings, live, trim_for_recording(spec.model or "")
    )
    return FreeHostedProvider(name, spec, wired, guard=guard, recording=recording)


def _replay(spec: ProviderSettings, settings: Settings) -> LLMProvider:
    """The provider that made a recording, answering from it: same code path, no network."""
    raw = os.environ.get(REPLAY_ENV, "").strip() or spec.recording
    if not raw:
        raise ConfigError(
            f"the replay provider needs a recording (configs/providers.yaml or {REPLAY_ENV})"
        )
    path = Path(raw) if Path(raw).is_absolute() else settings.paths.root / raw
    recording = Recording.load(path)
    header = recording.header
    original = ProviderSettings.model_validate(header["settings"])
    provenance = {
        key: header.get(key)
        for key in ("recording_id", "original_provider", "model", "created_at", "prompt_version")
    }
    provenance["recording_schema"] = header.get("schema")
    transport = ReplayTransport(recording)
    name = str(header.get("original_provider", "recorded"))
    if original.api == "ollama":
        from recap.providers.ollama_provider import OllamaProvider

        return OllamaProvider(name, original, transport, replay_of=provenance, recording=raw)
    if original.api == "openai_compatible":
        from recap.providers.openai_compatible import FreeHostedProvider

        return FreeHostedProvider(
            name, original, transport, guard=None, replay_of=provenance, recording=raw
        )
    raise ConfigError(f"{path.name}: cannot replay a recording of api {original.api!r}")


def _maybe_record(
    name: str,
    spec: ProviderSettings,
    settings: Settings,
    live: ModelTransport,
    trim: Trim | None = None,
) -> tuple[ModelTransport, str | None]:
    """Wrap `live` so every exchange is appended to data/recordings/<id>.jsonl."""
    if not spec.record:
        return live, None
    from recap.providers.protocol import PROMPT_VERSION
    from recap.telemetry.redact import Redactor

    recording_id = f"{utcnow():%Y%m%d-%H%M%S}-{name}-{new_id('rec').split('-')[-1][:6]}"
    path = settings.paths.data_dir / "recordings" / f"{recording_id}.jsonl"
    header = {
        "recording_id": recording_id,
        "original_provider": name,
        "model": spec.model,
        "prompt_version": PROMPT_VERSION,
        "recap_version": __version__,
        # The provider's settings, so a replay sends byte-identical requests. No secrets:
        # free_tier holds the NAMES of environment variables, never their values.
        "settings": spec.model_dump(mode="json"),
    }
    redactor = Redactor()
    home = str(Path.home())

    def scrub(text: str) -> str:  # secrets, then the user's home folder (recordings may be shared)
        return redactor.redact_text(text).replace(home, "~")

    recorder = Recorder(path, header, redact=scrub, trim=trim)
    root = settings.paths.root
    shown = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    return RecordingTransport(live, recorder), shown


def _anthropic(spec: ProviderSettings, client: object | None) -> LLMProvider:
    from recap.providers.anthropic_provider import AnthropicProvider, load_sdk

    if client is None:
        # A real, billed API call is about to become possible: every opt-in check must pass.
        _require_paid_opt_in()
        load_sdk()
        _require_api_key()
    return AnthropicProvider(spec, client=client)


def _red_team(base: LLMProvider) -> LLMProvider:
    # RED-TEAM ONLY: a planner that obeys injected instructions, to prove the gateway holds.
    from recap.safety.redteam import PromptInjectedProvider

    return PromptInjectedProvider(base)


def _require_paid_opt_in() -> None:
    """This project's budget is $0: a paid API needs the owner's explicit, separate opt-in."""
    if os.environ.get(PAID_OPT_IN_ENV, "").strip() != "1":
        raise ConfigError(
            "the 'anthropic' provider calls a PAID API and this project's budget is $0. It "
            f"runs only if the owner sets {PAID_OPT_IN_ENV}=1 for a separately funded API "
            "account. Use 'ollama' (local) or 'groq' (free plan) instead."
        )


def _require_api_key() -> None:
    """Refuse to build the LLM provider unless the user set their own API credentials."""
    if not any(os.environ.get(var, "").strip() for var in API_KEY_ENV_VARS):
        raise ConfigError(
            "the 'anthropic' provider is opt-in: it calls the paid Claude API with your own "
            "API key and billing. Set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN), or use "
            "the default 'deterministic' provider (scripted, not an LLM, $0)."
        )
