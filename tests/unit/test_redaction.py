"""Tests for the Redactor, which removes secrets from trace payloads, tool outputs and error text.

Fake keys are assembled at runtime ("sk-" + ...) so this file never contains a string that
a secret scanner would mistake for a real credential.
"""

from __future__ import annotations

import copy

import pytest

from recap.core.models import ToolCall
from recap.telemetry.redact import REDACTED, Redactor
from recap.telemetry.tracer import MemorySink, TraceCategory, Tracer

ANTHROPIC_KEY = "sk-ant-" + "api03-" + "a1B2c3D4e5" * 3
OPENAI_KEY = "sk-" + "Z9y8X7w6" * 3
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
GITHUB_TOKEN = "ghp_" + "x1" * 15


def test_api_keys_are_redacted_inside_nested_payloads() -> None:
    redact = Redactor(environ={})
    payload = {
        "request": {
            "note": f"called with {ANTHROPIC_KEY}",
            "headers": ["Authorization: Bearer abc.def-123456"],
        },
        "attempts": [({"aws": AWS_KEY}, 2)],
        "text": f"tokens {GITHUB_TOKEN} and {OPENAI_KEY}",
    }

    result = redact(payload)

    for secret in (ANTHROPIC_KEY, OPENAI_KEY, AWS_KEY, GITHUB_TOKEN, "abc.def-123456"):
        assert secret not in str(result)
    assert result["request"]["note"] == f"called with {REDACTED}"
    assert result["request"]["headers"] == [f"Authorization: {REDACTED}"]
    assert result["attempts"] == [({"aws": REDACTED}, 2)]  # tuples stay tuples
    assert result["text"] == f"tokens {REDACTED} and {REDACTED}"


def test_values_under_sensitive_key_names_are_hidden() -> None:
    payload = {
        "api_key": "plain",
        "Authorization": "Basic dXNlcjpwYXNz",
        "nested": {"client_secret": "s", "db_password": "hunter2", "credentials": {"user": "bob"}},
        "service": "checkout",
    }

    assert Redactor(environ={})(payload) == {
        "api_key": REDACTED,
        "Authorization": REDACTED,
        "nested": {"client_secret": REDACTED, "db_password": REDACTED, "credentials": REDACTED},
        "service": "checkout",
    }


def test_token_counts_stay_visible() -> None:
    # Context-pack and budget events carry counts under "token" keys; those are not secrets.
    payload = {
        "tokens_before": 4210,
        "tokens_after": 2960,
        "budget": {"estimated_tokens": 120, "max_estimated_tokens": 120_000},
        "actual_input_tokens": None,
        "estimate": True,
        "access_token": "abc123",
    }

    assert Redactor(environ={})(payload) == {**payload, "access_token": REDACTED}


def test_secrets_from_environment_variables_are_redacted_everywhere() -> None:
    environ = {
        "PAYMENTS_API_KEY": "pay-live-7f3a9c2e",
        "GITHUB_TOKEN": "short",  # too short to be treated as a secret value
        "HOME": "/home/not-a-secret",  # the variable name does not look secret
    }
    redact = Redactor(environ=environ)

    payload = {
        "log": "connecting with pay-live-7f3a9c2e to payments",
        "home": "/home/not-a-secret",
        "word": "short",
    }

    assert redact(payload) == {**payload, "log": f"connecting with {REDACTED} to payments"}


def test_os_environ_is_the_default_source_of_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECAP_TEST_SECRET", "env-secret-value-123")

    assert Redactor()("x env-secret-value-123 y") == f"x {REDACTED} y"


def test_extra_secrets_are_redacted() -> None:
    redact = Redactor(extra_secrets=["my-extra-secret", ""], environ={})

    assert redact("uses my-extra-secret") == f"uses {REDACTED}"
    assert redact("plain text") == "plain text"  # the empty extra secret is ignored


def test_input_is_never_mutated() -> None:
    payload = {"api_key": "k", "items": [{"note": ANTHROPIC_KEY}], "pair": (1, OPENAI_KEY)}
    before = copy.deepcopy(payload)

    result = Redactor(environ={})(payload)

    assert payload == before
    assert result is not payload
    assert result["items"] is not payload["items"]


def test_ordinary_values_pass_through_unchanged() -> None:
    payload = {
        "service": "checkout",
        "message": "error rate 32% after deploy v2.4.1; see runbook-bad-deployment#remediation-1",
        "latency_ms": 1600.5,
        "ok": True,
        "missing": None,
        "ids": ("ev-3f9a2c-001", "ev-3f9a2c-002"),
        "source_ref": "mcp:get_service_logs(service=checkout)",
    }

    assert Redactor(environ={})(payload) == payload


def test_pydantic_models_in_payloads_are_redacted_as_data() -> None:
    call = ToolCall(tool_name="get_service_logs", args={"api_key": "k"}, reason=OPENAI_KEY)

    assert Redactor(environ={})({"call": call}) == {
        "call": {"tool_name": "get_service_logs", "args": {"api_key": REDACTED}, "reason": REDACTED}
    }


def test_tracer_redacts_payloads_before_any_sink_sees_them() -> None:
    sink = MemorySink()
    tracer = Tracer("run-a", sinks=[sink], redact=Redactor(environ={}))

    tracer.emit(TraceCategory.TOOL_CALL, {"api_key": "k", "note": f"key {ANTHROPIC_KEY}"})

    assert sink.events[0].payload == {"api_key": REDACTED, "note": f"key {REDACTED}"}


def test_free_text_key_value_secrets_keep_the_key_but_lose_the_value() -> None:
    line = "db login failed: user=shop password=hunter2hunter2 token: abc123def456, retrying"
    out = Redactor(environ={})(line)
    assert "hunter2hunter2" not in out and "abc123def456" not in out
    assert "password=[REDACTED]" in out and "user=shop" in out and "retrying" in out
