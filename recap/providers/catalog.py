"""Provider catalog: what each configured provider is, and whether it could run right now.

For the API (GET /providers) and the dashboard's provider panel. It reads the configuration,
the environment (only WHETHER a key variable is set, never its value), the usage ledger
and, for a local Ollama server, the list of installed models over loopback. It never calls
a hosted API and never runs inference, so opening the dashboard costs nothing.

Each entry has a `status` (one word for a badge) and a `detail` (what to do about it):

    ready             can run now (a hosted provider still needs the per-run opt-in)
    needs_setup       a key, an owner confirmation or a local model is missing
    stopped           the free-plan tripwire fired; the owner must check billing and clear it
    disabled          the paid adapter: off for this $0 project
    unavailable       the local server is not running, or a recording is missing
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from recap.core.config import ProviderSettings, Settings
from recap.core.ids import utcnow
from recap.providers.factory import PAID_OPT_IN_ENV, REPLAY_ENV
from recap.providers.quota import OPT_IN_ENV, UsageLedger
from recap.providers.transport import Recording

PROBE_TIMEOUT_SECONDS = 1.0


async def provider_status(
    settings: Settings,
    ledger: UsageLedger,
    *,
    environ: Mapping[str, str] | None = None,
    mock_http: httpx.MockTransport | None = None,
) -> list[dict[str, Any]]:
    """One entry per configured provider (see the module doc). `mock_http` is for tests."""
    environ = os.environ if environ is None else environ
    entries = []
    for name, spec in settings.providers.providers.items():
        entry: dict[str, Any] = {
            "name": name,
            "kind": spec.kind,
            "origin": spec.origin,
            "model": spec.model,
            "mode": spec.mode if spec.kind == "llm" else None,
            "description": spec.description,
            "default": name == settings.providers.default,
        }
        if spec.origin == "scripted":
            entry |= {"status": "ready", "detail": "scripted policy, not an LLM; $0"}
        elif spec.origin == "local_llm":
            entry |= await _local(spec, mock_http)
        elif spec.origin == "free_hosted_llm":
            entry |= _hosted(name, spec, ledger, environ)
        elif spec.origin == "replay":
            entry |= _replay(spec, settings.paths.root, environ)
        else:  # paid_llm
            enabled = environ.get(PAID_OPT_IN_ENV, "").strip() == "1"
            entry |= {
                "status": "ready" if enabled else "disabled",
                "detail": "paid API: off for this $0 project"
                if not enabled
                else f"paid API enabled by {PAID_OPT_IN_ENV}=1",
            }
        if spec.kind == "llm":
            entry["usage"] = _usage(name, ledger)
        entries.append(entry)
    return entries


async def _local(spec: ProviderSettings, mock_http: httpx.MockTransport | None) -> dict[str, Any]:
    """Ask the local Ollama server which models are installed (loopback, metadata only)."""
    base = (spec.base_url or "").rstrip("/")
    info: dict[str, Any] = {
        "base_url": base,
        "think": spec.think,
        "context_tokens": spec.context_tokens,
    }
    try:
        async with httpx.AsyncClient(transport=mock_http) as client:
            response = await client.get(f"{base}/api/tags", timeout=PROBE_TIMEOUT_SECONDS)
        installed = [m.get("name") for m in response.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return info | {
            "status": "unavailable",
            "detail": f"no Ollama server at {base}; start it with: OLLAMA_NO_CLOUD=1 ollama serve",
            "installed": [],
        }
    if spec.model not in installed:
        return info | {
            "status": "needs_setup",
            "detail": f"{spec.model} is not installed (RECAP never downloads models)",
            "installed": installed,
        }
    return info | {"status": "ready", "detail": "local model installed; $0", "installed": installed}


def _hosted(
    name: str, spec: ProviderSettings, ledger: UsageLedger, environ: Mapping[str, str]
) -> dict[str, Any]:
    free = spec.free_tier
    assert free is not None  # ProviderSettings guarantees it for this origin
    key_present = bool(environ.get(free.api_key_env, "").strip())
    confirmed = environ.get(free.attestation_env, "").strip()
    age = _age_days(confirmed)
    fresh = age is not None and 0 <= age <= free.attestation_max_age_days
    stop = ledger.stopped(name)
    info = {
        "key_env": free.api_key_env,
        "key_present": key_present,  # whether the variable is set; the value is never read here
        "opt_in_env": OPT_IN_ENV,
        "opted_in": environ.get(OPT_IN_ENV, "").strip() == "1",
        "attestation_env": free.attestation_env,
        "confirmed_on": confirmed or None,
        "confirmation_age_days": age,
        "confirmation_fresh": fresh,
        "allowed_models": free.allowed_models,
        "free_daily_request_limit": free.free_daily_request_limit,
        "limits_checked_on": free.limits_checked_on,
        "caps": free.caps.model_dump(),
        "last_rate_limit": ledger.last_rate_limit(name),
        "stopped": stop,
    }
    if stop is not None:
        status, detail = "stopped", f"stopped: {stop['reason']}"
    elif not key_present:
        status, detail = "needs_setup", f"set {free.api_key_env} in the environment"
    elif not fresh:
        status, detail = (
            "needs_setup",
            (f"the owner must re-check the free plan and set {free.attestation_env}=YYYY-MM-DD"),
        )
    else:
        status = "ready"
        detail = f"free plan confirmed {age} day(s) ago; live calls also need {OPT_IN_ENV}=1"
    return info | {"status": status, "detail": detail}


def _replay(spec: ProviderSettings, root: Path, environ: Mapping[str, str]) -> dict[str, Any]:
    raw = environ.get(REPLAY_ENV, "").strip() or spec.recording or ""
    path = Path(raw) if Path(raw).is_absolute() else root / raw
    try:
        header = Recording.load(path).header
    except Exception as exc:  # a missing or broken recording: say so, never guess
        return {"recording": raw, "status": "unavailable", "detail": str(exc)[:200]}
    provenance = {
        key: header.get(key)
        for key in ("original_provider", "model", "created_at", "prompt_version")
    }
    return {
        "recording": raw,
        "replay_of": provenance,
        "status": "ready",
        "detail": f"replays a recorded {header.get('model')} run; no model is called",
    }


def _usage(name: str, ledger: UsageLedger) -> dict[str, int]:
    now = utcnow()
    minute = ledger.window(name, now - timedelta(minutes=1))
    day = ledger.window(name, now - timedelta(days=1))
    return {
        "requests_last_minute": minute[0],
        "requests_last_24h": day[0],
        "tokens_last_24h": day[1],
    }


def _age_days(raw: str) -> int | None:
    try:
        return (utcnow().date() - date.fromisoformat(raw)).days
    except ValueError:
        return None
