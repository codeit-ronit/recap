"""HTTP transport for LLM providers: live requests, recordings, and offline replay.

Where it sits:

    OllamaProvider / FreeHostedProvider
        |  request("POST", "/api/chat", body)
        v
    ModelTransport ----> HttpExchange (status, the headers RECAP keeps, JSON body)
        |-- LiveTransport       real HTTP with httpx; exactly one request, no hidden retries
        |-- RecordingTransport  a live transport that also appends each exchange to a JSONL file
        '-- ReplayTransport     answers from a recording, with no network at all

Providers never call httpx themselves. They hand a JSON body to a transport and get the
answer back. That one seam gives three things:

  * tests drive the real provider code against a local protocol fixture (an
    httpx.MockTransport that answers like Ollama or Groq would);
  * every live exchange can be recorded. The API key is added only on the wire (a header
    built at send time), so it never reaches a recording;
  * CI replays a recorded real-model run offline. ReplayTransport finds each answer by a
    fingerprint of the request and fails loudly (ReplayMismatch) when the request was never
    recorded: a stale recording is an error, never a silent substitute.

Fingerprints skip what changes from run to run but means nothing: random run, incident,
proposal and episode IDs, and the 6-character run prefix inside evidence IDs
("ev-3f9a2c-007" becomes "ev-*-007"). When a recorded answer cites evidence, the replay
swaps the recorded run's prefix for the current run's, so citations point at this run.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from recap.core.errors import ConfigError, ProviderError, ReplayMismatch
from recap.core.ids import canonical_json, sha256_hex, utcnow
from recap.core.models import Contract

RECORDING_SCHEMA = "recap-recording-v1"
MAX_ERROR_TEXT = 300  # characters of a non-JSON error body kept for messages

# Response headers worth keeping: the rate-limit picture (the free-plan tripwire and replays
# need it). Nothing else is stored or recorded: no cookies, no auth echoes, and no request
# IDs, which could link a published recording to the account that made it.
KEPT_HEADERS = (
    "retry-after",
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
)

# Values that differ between two runs of the same incident without changing its meaning.
_EVIDENCE_PREFIX = re.compile(r"\bev-([0-9a-f]{6})-")
_RANDOM_IDS = re.compile(r"\b(run|inc|prop|apr|call|ep)-[0-9a-f]{10}\b")


class HttpExchange(Contract):
    """One HTTP answer, reduced to what providers use."""

    status: int
    headers: dict[str, str] = Field(default_factory=dict)  # lower-cased; only KEPT_HEADERS
    body: Any = None  # the parsed JSON body, or None when it was not JSON
    text: str = ""  # start of a body that was not JSON (for error messages)
    duration_ms: float = 0.0
    replayed: bool = False


class ModelTransport(Protocol):
    """Sends one request to a model server and returns its answer."""

    name: str  # "live", "recording" or "replay"

    async def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float
    ) -> HttpExchange: ...


# --- live HTTP ----------------------------------------------------------------------------


class LiveTransport:
    """Real HTTP with httpx: one call is exactly one request (no retries, no redirects).

    `headers` is called at send time, so a credential read from the environment is put on
    the wire and nowhere else. `mock` replaces the network with an httpx.MockTransport in
    tests (the local protocol fixture); everything else runs exactly as in production.

    Proxy settings from the environment or the operating system are ignored
    (`trust_env=False`): a proxy would receive every prompt, could answer as "the model",
    and would quietly turn a loopback-only local model into a remote one.
    """

    name = "live"

    def __init__(
        self,
        base_url: str,
        *,
        headers: Callable[[], dict[str, str]] = dict,
        mock: httpx.MockTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.host = urlsplit(self.base_url).netloc or self.base_url
        self._headers = headers
        self._mock = mock

    async def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float
    ) -> HttpExchange:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                transport=self._mock, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.request(
                    method,
                    self.base_url + path,
                    json=body,
                    headers=self._headers(),
                    timeout=timeout,
                )
        except httpx.TimeoutException:
            # `from None`: the httpx error adds nothing for the user and must never be able
            # to carry request details (headers) into a trace.
            raise ProviderError(f"{self.host} did not answer within {timeout:g}s") from None
        except httpx.HTTPError as exc:
            reason = type(exc).__name__
            raise ProviderError(f"could not reach {self.host} ({reason})") from None
        return _exchange(response, (time.perf_counter() - started) * 1000)


def _exchange(response: httpx.Response, duration_ms: float) -> HttpExchange:
    headers = {name: response.headers[name] for name in KEPT_HEADERS if name in response.headers}
    try:
        body = response.json()
        text = ""
    except ValueError:  # not JSON (an HTML error page, a proxy message, ...)
        body, text = None, response.text[:MAX_ERROR_TEXT]
    return HttpExchange(
        status=response.status_code,
        headers=headers,
        body=body,
        text=text,
        duration_ms=round(duration_ms, 1),
    )


# --- recording ----------------------------------------------------------------------------


# Trims a response body before it is recorded: (path, body) -> what to keep.
Trim = Callable[[str, Any], Any]


class Recorder:
    """Appends model exchanges to one JSONL file: a header line, then one line per exchange.

    Recordings may be committed (as CI replay fixtures), so two things happen first:
      * `trim` keeps only what the provider reads from a response. Ollama's /api/show, for
        example, also returns the model's license, template and a local file path;
      * `redact` runs over every line: known secret values and key formats, and the
        user's home folder becomes "~". The key is never in the data to begin with; this
        is a second line of defence.
    """

    def __init__(
        self,
        path: Path,
        header: dict[str, Any],
        redact: Callable[[str], str] = str,
        trim: Trim | None = None,
    ) -> None:
        self.path = path
        self.header = {"type": "header", "schema": RECORDING_SCHEMA, **header}
        self._redact = redact
        self._trim = trim
        self._seq = 0

    def write(
        self, method: str, path: str, body: dict[str, Any] | None, exchange: HttpExchange
    ) -> None:
        if self._seq == 0:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.header["created_at"] = utcnow().isoformat()
            self._append(self.header)
        self._seq += 1
        text = canonical_json(body or {})
        if self._trim is not None:
            exchange = exchange.model_copy(update={"body": self._trim(path, exchange.body)})
        self._append(
            {
                "type": "exchange",
                "seq": self._seq,
                "method": method,
                "path": path,
                "fingerprint": fingerprint(method, path, body),
                "evidence_prefix": _evidence_prefix(text),
                "request": body,
                "response": exchange.model_dump(mode="json"),
            }
        )

    def _append(self, record: dict[str, Any]) -> None:
        # Redact the DATA (every string in it), then serialize: redacting the serialized line
        # could cut through JSON escapes (a log line with token="..." inside a JSON string)
        # and leave a line that no longer parses.
        line = json.dumps(_scrub(record, self._redact), sort_keys=True, default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class RecordingTransport:
    """A live transport that also records every exchange (for debugging and replay)."""

    name = "recording"

    def __init__(self, inner: ModelTransport, recorder: Recorder) -> None:
        self.inner = inner
        self.recorder = recorder

    async def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float
    ) -> HttpExchange:
        exchange = await self.inner.request(method, path, body, timeout=timeout)
        self.recorder.write(method, path, body, exchange)
        return exchange


# --- replay -------------------------------------------------------------------------------


class Recording(Contract):
    """A recording file loaded into memory: its header plus every recorded exchange."""

    path: str
    header: dict[str, Any]
    exchanges: list[dict[str, Any]]

    @classmethod
    def load(cls, path: Path) -> Recording:
        if not path.is_file():
            raise ConfigError(f"recording not found: {path}")
        try:
            text = path.read_text(encoding="utf-8")
            lines = [json.loads(line) for line in text.splitlines() if line]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"{path.name} is not a valid recording ({exc})") from None
        if not lines or lines[0].get("type") != "header":
            raise ConfigError(f"{path} is not a RECAP recording (no header line)")
        header = lines[0]
        if header.get("schema") != RECORDING_SCHEMA:
            raise ConfigError(f"{path}: unsupported recording schema {header.get('schema')!r}")
        exchanges = [line for line in lines[1:] if line.get("type") == "exchange"]
        return cls(path=str(path), header=header, exchanges=exchanges)


class ReplayTransport:
    """Answers each request from a recording. No network, and never a guessed answer.

    Requests are matched by fingerprint, not by order. Agents that ran side by side may
    still ask something never recorded: their prompts include what every agent has already
    observed, so a different finishing order makes different prompts (ADR 0018). Each
    recorded answer is used once; asking the same thing again uses the next recording of it,
    and asking something never recorded raises ReplayMismatch.
    """

    name = "replay"

    def __init__(self, recording: Recording) -> None:
        self.recording = recording
        self._answers: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        for exchange in recording.exchanges:
            self._answers[exchange["fingerprint"]].append(exchange)

    async def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float
    ) -> HttpExchange:
        key = fingerprint(method, path, body)
        queue = self._answers.get(key)
        if not queue:
            raise ReplayMismatch(
                f"the recording {Path(self.recording.path).name} has no answer for this "
                f"{method} {path} request (fingerprint {key[:12]}). Prompts, tools or tool "
                "results changed since it was recorded: record the run again."
            )
        recorded = queue.popleft()
        response = dict(recorded["response"])
        current = _evidence_prefix(canonical_json(body or {}))
        old = recorded.get("evidence_prefix")
        if old and current and old != current:
            # The recorded answer cites the recorded run's evidence IDs: point them at ours.
            text = json.dumps(response["body"]).replace(f"ev-{old}-", f"ev-{current}-")
            response["body"] = json.loads(text)
        response["replayed"] = True
        return HttpExchange.model_validate(response)


# --- fingerprints -------------------------------------------------------------------------


def _scrub(value: Any, redact: Callable[[str], str]) -> Any:
    """A copy of a JSON-like value with `redact` applied to every string in it."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _scrub(item, redact) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, redact) for item in value]
    return value


def normalize(text: str) -> str:
    """Text with run-specific random values replaced by placeholders (see the module doc)."""
    text = _EVIDENCE_PREFIX.sub("ev-*-", text)
    return _RANDOM_IDS.sub(lambda match: f"{match.group(1)}-*", text)


def fingerprint(method: str, path: str, body: Mapping[str, Any] | None) -> str:
    """Stable identity of a request: the same question in another run gives the same value."""
    return sha256_hex(f"{method} {path}\n" + normalize(canonical_json(body or {})))


def _evidence_prefix(text: str) -> str | None:
    """The run prefix of the evidence IDs in `text` ("3f9a2c"): the one that appears most.

    The system prompt shows one example ID; this run's own IDs appear in the context, the
    schema and the history, so they outnumber it whenever there are any (a tie goes to the
    prefix that appears last, which is the context, not the system prompt).
    """
    counts = Counter(_EVIDENCE_PREFIX.findall(text))
    if not counts:
        return None
    return max(counts, key=lambda prefix: (counts[prefix], text.rfind(f"ev-{prefix}-")))
