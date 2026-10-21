"""FreeHostedProvider: real inference on a hosted FREE plan through an OpenAI-compatible API.

Configured for Groq's Free plan (configs/providers.yaml). Where it sits: one of the two
HttpLLMProvider subclasses (http_llm.py); the shared loop renders the step protocol and
this class sends it as a chat completion:

    POST {base_url}/chat/completions
      {model, messages, temperature, max_completion_tokens, seed, reasoning_effort,
       response_format: {type: json_schema, json_schema: {name, strict: true, schema}}  (json)
       | tools: [...], tool_choice: "required"                                          (tools)}

Every request first passes the FreeTierGuard (quota.py): the opt-in, the exact model
allowlist, the key from the environment, the owner's dated free-plan confirmation, no
earlier stop, RECAP's own caps. Every response then goes through its tripwire (the free
plan's daily-limit header must match).

Never sent: service_tier (Groq's default on-demand tier applies), a `models` fallback list,
Groq's built-in tools or remote MCP (they would run tools outside RECAP's gateway),
stream, n, logprobs.

    200                      the answer; the response's model must be the requested model
    429                      one retry if retry-after <= max_retry_after_seconds and the caps
                             allow it; otherwise QuotaExhausted (the run is deferred)
    401 / 403                refused (bad key, model not permitted): stop, no retry
    402, blocked_api_access  free status no longer verified: the provider is stopped
    400 json_validate_failed the answer broke the schema: an unusable answer (retried once
                             with feedback, like any other)
    5xx                      one retry after 2 seconds, then ProviderError
"""

from __future__ import annotations

import asyncio
from typing import Any

from recap.context.tokens import estimate_tokens
from recap.core.config import ProviderSettings
from recap.core.errors import ConfigError, ProviderError, QuotaExhausted
from recap.core.ids import canonical_json
from recap.providers import protocol
from recap.providers.http_llm import HttpLLMProvider, ModelReply, Purpose
from recap.providers.protocol import Message
from recap.providers.quota import FreeTierGuard, MinuteCapReached, rate_limit_snapshot
from recap.providers.transport import HttpExchange, ModelTransport

SERVER_ERROR_RETRY_SECONDS = 2.0
PROBE_TIMEOUT_SECONDS = 15.0
# Longest wait for RECAP's own per-minute cap, per request. Two parallel agents can each
# need most of a minute's tokens, so one may wait for the other's minute to pass and then
# for its own (measured on Groq Free, 2026-10-21). The run's wall-clock budget bounds it all.
MAX_PACING_SECONDS = 150.0


class FreeHostedProvider(HttpLLMProvider):
    """A hosted free-plan model behind the LLMProvider interface (fails closed)."""

    api = "openai_compatible"

    def __init__(
        self,
        name: str,
        settings: ProviderSettings,
        transport: ModelTransport,
        *,
        guard: FreeTierGuard | None,
        **kwargs: Any,
    ) -> None:
        if guard is None and transport.name != "replay":
            raise ConfigError(
                f"{name}: a hosted provider needs its free-tier guard; only a replay, which "
                "sends nothing, may run without one"
            )
        super().__init__(name, settings, transport, **kwargs)
        self.guard = guard
        self._model_checked = False

    @property
    def free_status_lost(self) -> str | None:
        """Why the free-plan tripwire fired during this run (None if it never did)."""
        return self.guard.tripped if self.guard is not None else None

    async def check_model(self) -> None:
        """Before the first request: the model is listed and active (metadata, not inference)."""
        if self._model_checked:
            return
        if self.guard is not None:
            self.guard.check_allowed(self.model)  # no request of any kind before the checks
        listing = await self.transport.request("GET", "/models", timeout=PROBE_TIMEOUT_SECONDS)
        if listing.status != 200:
            raise ProviderError(f"{self.name}: the model list answered HTTP {listing.status}")
        models = (listing.body or {}).get("data") or []
        entry = next((m for m in models if isinstance(m, dict) and m.get("id") == self.model), None)
        if entry is None or entry.get("active") is False:
            raise ProviderError(f"{self.name}: {self.model} is not offered (or not active) now")
        self._model_checked = True

    async def _send(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        purpose: Purpose,
    ) -> ModelReply:
        await self.check_model()
        body = self._body(messages, schema, tools)
        # Reserved against RECAP's caps before sending: the prompt plus a typical answer
        # (FreeTierSettings.reserved_output_tokens); the real count replaces it afterwards.
        answer = self.settings.max_output_tokens
        if self.guard is not None:
            answer = min(answer, self.guard.settings.reserved_output_tokens)
        reserved = estimate_tokens(canonical_json(body)) + answer
        for attempt in (1, 2):
            request_id = await self._reserve(reserved)
            exchange = await self.transport.request(
                "POST", "/chat/completions", body, timeout=self.settings.request_timeout_seconds
            )
            data = exchange.body if isinstance(exchange.body, dict) else {}
            code = _error_code(data)
            usage = data.get("usage") or {}
            if self.guard is not None:
                snapshot = self.guard.after_response(  # the tripwire raises if not free
                    request_id,
                    self.model,
                    status=exchange.status,
                    headers=exchange.headers,
                    error_code=code,
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                )
            else:
                snapshot = rate_limit_snapshot(exchange.headers)
            if exchange.status == 200:
                return self._reply(data, exchange, snapshot)
            if exchange.status == 429:
                wait = _retry_after(exchange.headers)
                limit = self.guard.settings.max_retry_after_seconds if self.guard else 0.0
                if attempt == 1 and wait is not None and wait <= limit:
                    await asyncio.sleep(wait)
                    continue
                after = f"retry-after {wait:g}s" if wait is not None else "no retry-after"
                raise QuotaExhausted(self.name, f"HTTP 429 rate limit ({after}); run deferred")
            if exchange.status in (401, 403):
                raise ProviderError(
                    f"{self.name} refused the request (HTTP {exchange.status} {code}): check the "
                    "key and the model permissions. RECAP does not retry or switch provider."
                )
            if exchange.status == 400 and code == "json_validate_failed":
                note = "the server rejected an answer that broke the schema"
                return ModelReply(status=400, duration_ms=exchange.duration_ms, notes=[note])
            if exchange.status >= 500 and attempt == 1:
                await asyncio.sleep(SERVER_ERROR_RETRY_SECONDS)
                continue
            raise ProviderError(
                f"{self.name} returned HTTP {exchange.status} {code}: {_error_message(data)}"
            )
        raise ProviderError(f"{self.name}: no answer after a retry")  # pragma: no cover

    async def _reserve(self, tokens: int) -> int:
        """Run the guard's checks and reserve the request, pacing for a full minute cap.

        A full per-minute cap frees up within a minute, so RECAP waits instead of giving up
        (at most MAX_PACING_SECONDS per request). Daily and per-run caps defer the run.
        """
        if self.guard is None:  # a replay sends nothing, so there is nothing to reserve
            return 0
        waited = 0.0
        while True:
            try:
                return self.guard.preflight(self.model, tokens)
            except MinuteCapReached as full:
                if waited + full.retry_in > MAX_PACING_SECONDS:
                    raise
                waited += full.retry_in
                await asyncio.sleep(full.retry_in)

    def _body(
        self,
        messages: list[Message],
        schema: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """The chat-completion request. Only these fields, ever (see the module doc)."""
        settings = self.settings
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": settings.temperature,
            "max_completion_tokens": settings.max_output_tokens,
        }
        if settings.seed is not None:
            body["seed"] = settings.seed
        if settings.reasoning_effort:
            body["reasoning_effort"] = settings.reasoning_effort
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "recap_answer",
                    "strict": True,
                    "schema": protocol.for_strict_outputs(schema),
                },
            }
        if tools:
            body["tools"] = protocol.for_strict_outputs(tools)
            body["tool_choice"] = "required"  # a read-only tool, or submit_findings to finish
        return body

    def _reply(
        self, data: dict[str, Any], exchange: HttpExchange, snapshot: dict[str, Any]
    ) -> ModelReply:
        reported = data.get("model")
        if reported != self.model:
            raise ProviderError(f"{self.name} answered with {reported!r}, not {self.model}")
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        notes: list[str] = []
        if choice.get("finish_reason") == "length":
            limit = self.settings.max_output_tokens
            notes.append(f"the answer was cut off at max_output_tokens={limit}")
        calls = [
            {
                "name": str((call.get("function") or {}).get("name", "")),
                "arguments": (call.get("function") or {}).get("arguments") or "{}",
            }
            for call in message.get("tool_calls") or []
        ]
        return ModelReply(
            status=200,
            content=str(message.get("content") or ""),
            tool_calls=calls,
            thinking=str(message.get("reasoning") or ""),  # gpt-oss reasoning, when included
            finish_reason=choice.get("finish_reason"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            model_reported=reported,
            duration_ms=exchange.duration_ms,
            rate_limit=snapshot,
            replayed=exchange.replayed,
            notes=notes,
        )


def trim_for_recording(model: str) -> Any:
    """A recording keeps only this provider's model from the /models list."""

    def trim(path: str, body: Any) -> Any:
        if not path.endswith("/models") or not isinstance(body, dict):
            return body
        return {
            "data": [
                m for m in body.get("data") or [] if isinstance(m, dict) and m.get("id") == model
            ]
        }

    return trim


def _retry_after(headers: dict[str, str]) -> float | None:
    """Seconds from a retry-after header (Groq sends whole or decimal seconds)."""
    try:
        return max(0.0, float(headers.get("retry-after", "")))
    except ValueError:
        return None


def _error_code(data: dict[str, Any]) -> str:
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or "")[:60]
    return ""


def _error_message(data: dict[str, Any]) -> str:
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "")[:200]
    return "no details"
