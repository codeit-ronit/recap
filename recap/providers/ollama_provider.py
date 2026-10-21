"""OllamaProvider: real inference with an open-weight model on this machine ($0, offline).

Where it sits: one of the two HttpLLMProvider subclasses (http_llm.py). The shared loop
renders the step protocol (protocol.py); this class turns it into Ollama's chat request
and reads the answer back:

    POST /api/chat  {model, messages, stream: false, think, keep_alive,
                     options: {temperature, seed, num_ctx, num_predict},
                     format: <the request's JSON schema>   (json mode and every proposal)
                     tools:  [tool definitions]            (tools mode)}
    <-  {model, message: {content, thinking, tool_calls}, done_reason,
         prompt_eval_count, eval_count, load_duration, ...}

Rules that keep it local, honest and safe:
  * loopback only: base_url must be 127.0.0.1, localhost or ::1;
  * no cloud models: Ollama can forward "...-cloud" models to its own servers; RECAP
    refuses them (and the server should run with OLLAMA_NO_CLOUD=1);
  * no downloads: the model must already be installed. RECAP never calls /api/pull;
  * capability check before the first request, with no inference: /api/show must list
    "tools" for tools mode and "thinking" when think is on, and num_ctx must fit the
    model's context window;
  * a prompt that may not fit num_ctx is refused before it is sent: Ollama would quietly
    drop the START of the prompt, which is where RECAP's safety rules are. An answer whose
    prompt filled the window is rejected for the same reason;
  * token counts are Ollama's own (prompt_eval_count, eval_count): exact, not estimated.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from recap.core.config import ProviderSettings
from recap.core.errors import ConfigError, ProviderError
from recap.core.ids import canonical_json
from recap.providers.http_llm import HttpLLMProvider, ModelReply, Purpose
from recap.providers.protocol import Message
from recap.providers.quota import UsageLedger, ledger_scope
from recap.providers.transport import HttpExchange, ModelTransport

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
DEFAULT_CONTEXT_TOKENS = 8192
KEEP_ALIVE = "10m"  # keep the model in memory between the steps of a run
# Conservative characters per token for the "does the prompt fit?" check. RECAP's prompts
# are full of JSON and IDs, which tokenize more densely than prose (about 3 chars/token).
SAFE_CHARS_PER_TOKEN = 3
PROBE_TIMEOUT_SECONDS = 10.0


def check_local_settings(settings: ProviderSettings) -> None:
    """Refuse a non-loopback server or a cloud-named model before anything is sent.

    The transport also ignores proxy settings (HTTP_PROXY, a system proxy), which could
    otherwise carry a "local" request, and its prompt, off the machine.
    """
    host = urlsplit(settings.base_url or "").hostname or ""
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"the Ollama provider is local-only: base_url must be on {', '.join(LOOPBACK_HOSTS)}, "
            f"not {host or settings.base_url!r}"
        )
    model = (settings.model or "").lower()
    if model.split(":")[-1].endswith("cloud") or model.endswith("-cloud"):
        raise ConfigError(
            f"{settings.model!r} is an Ollama cloud model (it runs on Ollama's servers): "
            "RECAP refuses it"
        )


class OllamaProvider(HttpLLMProvider):
    """A local Ollama model behind the LLMProvider interface."""

    api = "ollama"
    origin = "local_llm"

    def __init__(
        self,
        name: str,
        settings: ProviderSettings,
        transport: ModelTransport,
        *,
        ledger: UsageLedger | None = None,
        **kwargs: Any,
    ) -> None:
        check_local_settings(settings)
        super().__init__(name, settings, transport, **kwargs)
        self.context_tokens = settings.context_tokens or DEFAULT_CONTEXT_TOKENS
        self.ledger = ledger  # counts local requests too, for the dashboard (no caps apply)
        # What the server and model can do; filled by the first readiness check.
        self.capabilities: dict[str, Any] | None = None

    async def ensure_ready(self) -> dict[str, Any]:
        """Check the server, the model and its capabilities once (metadata only, no inference)."""
        if self.capabilities is not None:
            return self.capabilities
        version = await self.transport.request("GET", "/api/version", timeout=PROBE_TIMEOUT_SECONDS)
        if version.status != 200:
            raise ProviderError(
                f"Ollama answered HTTP {version.status} at {self.settings.base_url}; start it "
                "with: OLLAMA_NO_CLOUD=1 ollama serve"
            )
        show = await self.transport.request(
            "POST", "/api/show", {"model": self.model}, timeout=PROBE_TIMEOUT_SECONDS
        )
        if show.status == 404:
            raise ProviderError(
                f"{self.model} is not installed in Ollama. RECAP never downloads models; once "
                f"you have decided to, install it yourself: ollama pull {self.model}"
            )
        body = show.body if isinstance(show.body, dict) else {}
        if show.status != 200:
            raise ProviderError(f"Ollama /api/show answered HTTP {show.status}")
        if body.get("remote_host") or body.get("remote_model"):
            # A local name can point at a cloud model (e.g. a copy of a "-cloud" model).
            raise ProviderError(
                f"{self.model} is served from {body.get('remote_host') or 'a remote host'}, "
                "not this machine: RECAP refuses cloud models"
            )
        capabilities = [str(c) for c in body.get("capabilities") or []]
        if self.mode == "tools" and "tools" not in capabilities:
            raise ProviderError(
                f"{self.model} cannot call tools (capabilities: {capabilities}); use mode: json"
            )
        if self.settings.think and "thinking" not in capabilities:
            raise ProviderError(f"{self.model} has no thinking mode; set think: false")
        window = _context_length(body)
        if window is not None and self.context_tokens > window:
            raise ProviderError(
                f"context_tokens={self.context_tokens} is larger than {self.model}'s context "
                f"window ({window})"
            )
        details = body.get("details") or {}
        self.capabilities = {
            "ollama_version": (version.body or {}).get("version"),
            "capabilities": capabilities,
            "context_length": window,
            "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level"),
            # The exact weights file, so a run names the model it used beyond its tag.
            "weights": _weights_digest(body),
        }
        # The run record shows these facts (ProviderInfo.server) once they are known.
        self.info = self.info.model_copy(update={"server": dict(self.capabilities)})
        return self.capabilities

    async def _send(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        purpose: Purpose,
        log: list[ModelReply],
    ) -> ModelReply:
        await self.ensure_ready()
        self._check_fits(messages, schema, tools)
        settings = self.settings
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": settings.think,
            "keep_alive": KEEP_ALIVE,
            "options": {
                "temperature": settings.temperature,
                "seed": settings.seed,
                "num_ctx": self.context_tokens,
                "num_predict": settings.max_output_tokens,
            },
        }
        if schema is not None:
            body["format"] = schema  # Ollama constrains the answer to this schema as it writes
        if tools:
            body["tools"] = tools
        exchange = await self.transport.request(
            "POST", "/api/chat", body, timeout=settings.request_timeout_seconds
        )
        data = exchange.body if isinstance(exchange.body, dict) else {}
        self._count(exchange.status, data, exchange.replayed)
        reply = self._reply(exchange, data)
        log.append(reply)  # recorded whatever happens next
        if exchange.status != 200:
            raise ProviderError(f"Ollama returned HTTP {exchange.status}: {reply.notes[0]}")
        if reply.model_reported != self.model:
            raise ProviderError(
                f"Ollama answered with {reply.model_reported!r}, not the requested {self.model}"
            )
        if reply.input_tokens is not None and reply.input_tokens >= self.context_tokens:
            raise ProviderError(
                f"the prompt filled the whole context window ({reply.input_tokens} tokens), so "
                "its start may have been cut; raise context_tokens"
            )
        return reply

    def _reply(self, exchange: HttpExchange, data: dict[str, Any]) -> ModelReply:
        """Any exchange as a ModelReply (an error too: it is recorded like an answer)."""
        common: dict[str, Any] = {
            "status": exchange.status,
            "input_tokens": _int(data.get("prompt_eval_count")),
            "output_tokens": _int(data.get("eval_count")),
            "model_reported": data.get("model"),
            "duration_ms": exchange.duration_ms,
            "replayed": exchange.replayed,
        }
        if exchange.status != 200:
            error = data.get("error") or exchange.text or "no details"
            return ModelReply(notes=[str(error)[:200]], **common)
        notes: list[str] = []
        if data.get("done_reason") == "length":
            limit = self.settings.max_output_tokens
            notes.append(f"the answer was cut off at max_output_tokens={limit}")
        load_seconds = (_int(data.get("load_duration")) or 0) / 1e9
        if load_seconds >= 1:
            notes.append(f"Ollama first loaded the model into memory ({load_seconds:.1f}s)")
        message = data.get("message") or {}
        return ModelReply(
            content=str(message.get("content") or ""),
            tool_calls=[_tool_call(call) for call in message.get("tool_calls") or []],
            thinking=str(message.get("thinking") or ""),
            finish_reason=data.get("done_reason"),
            notes=notes,
            **common,
        )

    def _count(self, status: int, data: dict[str, Any], replayed: bool) -> None:
        """Add a live request to the usage ledger (a replayed answer cost nothing now)."""
        if self.ledger is None or replayed:
            return
        request_id = self.ledger.reserve(ledger_scope(self.settings.base_url or ""), self.model, 0)
        self.ledger.finish(
            request_id,
            status=status,
            input_tokens=_int(data.get("prompt_eval_count")),
            output_tokens=_int(data.get("eval_count")),
        )

    def _check_fits(
        self,
        messages: list[Message],
        schema: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
    ) -> None:
        """Refuse a prompt that might not fit num_ctx (Ollama would cut its start silently)."""
        chars = len(canonical_json(messages)) + len(canonical_json(schema or tools or {}))
        needed = chars // SAFE_CHARS_PER_TOKEN + self.settings.max_output_tokens
        if needed > self.context_tokens:
            raise ProviderError(
                f"the prompt (about {chars // SAFE_CHARS_PER_TOKEN} tokens) plus the answer "
                f"({self.settings.max_output_tokens}) may not fit context_tokens="
                f"{self.context_tokens}; raise context_tokens or lower the pack size"
            )


def trim_for_recording(path: str, body: Any) -> Any:
    """What a recording keeps of an Ollama response: only the fields this provider reads.

    /api/show also returns the model's license, chat template, parameters and modelfile
    (which names a local file path); none of it is needed to replay a run.
    """
    if path != "/api/show" or not isinstance(body, dict):
        return body
    details = body.get("details") or {}
    kept: dict[str, Any] = {
        "capabilities": body.get("capabilities"),
        "details": {
            key: details.get(key) for key in ("family", "parameter_size", "quantization_level")
        },
        "model_info": {
            key: value
            for key, value in (body.get("model_info") or {}).items()
            if key.endswith(".context_length")
        },
    }
    weights = _weights_digest(body)
    if weights is not None:  # the weights file's name only, never its local path
        kept["modelfile"] = "FROM " + weights.replace(":", "-")
    return kept


def _weights_digest(show: dict[str, Any]) -> str | None:
    """The model's weights file digest, from the FROM line of /api/show's modelfile.

    Only a line that starts with FROM counts: the modelfile also holds comments, and a
    model's tag may itself contain a digest-like name.
    """
    match = re.search(
        r"(?m)^FROM\s+\S*?sha256[-:]([0-9a-f]{64})\s*$", str(show.get("modelfile") or "")
    )
    return f"sha256:{match.group(1)}" if match else None


def _tool_call(raw: dict[str, Any]) -> dict[str, Any]:
    """Ollama's {"function": {"name", "arguments"}} as {"name", "arguments"}."""
    function = raw.get("function") or {}
    return {"name": str(function.get("name", "")), "arguments": function.get("arguments") or {}}


def _context_length(show: dict[str, Any]) -> int | None:
    """The model's context window from /api/show (model_info "<architecture>.context_length")."""
    for key, value in (show.get("model_info") or {}).items():
        if key.endswith(".context_length") and isinstance(value, int):
            return value
    return None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
