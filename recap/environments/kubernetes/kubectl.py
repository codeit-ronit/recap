"""Typed Kubernetes operations: the only way the Kind adapter talks to a cluster.

    adapter method --(one KubeOp from a closed list, typed arguments)--> argv built here
                   --> kubectl, started without a shell --> strictly parsed JSON

No model ever writes a kubectl command (docs/m5-integration-plan.md, section 3.6). The
adapter calls one of the methods below; each one validates its arguments again (names must
be DNS-1123 names, revisions positive integers, label selectors are built from validated
key/value pairs, never taken as a string) and builds the argument list itself. A value such
as "-l a,b", "--all-namespaces" or "x; rm -rf ~" can never reach kubectl as an argument: it
is not a Kubernetes name, so it is refused before any process starts.

Every call carries --kubeconfig, --context, --namespace and --request-timeout (and -o json
wherever kubectl prints an object; `kubectl logs` prints text and has no -o), so kubectl never
falls back to ~/.kube/config or another cluster. The child gets a minimal environment of its
own (KUBECONFIG, a private HOME for kubectl's cache, a fixed PATH, the C locale), its output
is size-limited and parsed strictly (an unexpected shape is an error, never a guess), and
each call is written to the run's command audit with the credential paths replaced by
placeholders. Each client is bound to ONE identity and refuses operations that identity's
Role does not grant: RECAP's own copy of the RBAC matrix, checked before the API server
checks the real one.

Commands that never appear here: exec, port-forward, proxy, apply, create, edit, delete,
scale, run, cp, debug, drain, cordon. `set_image` (a typed patch of one container image) is
used only by the trusted reset, as the scenario identity.

`FakeKubeClient` (recap/environments/kubernetes/fake.py) runs the same validation and the
same parsing over an in-process fake cluster, for offline tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from recap.environments.kubernetes.options import DNS_LABEL, REPOSITORY, Identity


class KubeError(RuntimeError):
    """A Kubernetes operation failed. The message is safe to show: no paths, no credentials."""


class KubeOutputError(KubeError):
    """kubectl answered with output of an unexpected shape or size: refused, never guessed."""


class KubeOp(StrEnum):
    """The closed list of operations. There is no "run this command" operation."""

    LIST_DEPLOYMENTS = "list_deployments"
    GET_DEPLOYMENT = "get_deployment"
    LIST_REPLICASETS = "list_replicasets"
    LIST_PODS = "list_pods"
    LIST_EVENTS = "list_events"
    POD_LOGS = "pod_logs"
    VERSION = "version"
    ROLLOUT_UNDO = "rollout_undo"  # execute_authorized: rollback, as recap-writer
    ROLLOUT_RESTART = "rollout_restart"  # execute_authorized: restart, as recap-writer
    SET_IMAGE = "set_image"  # reset only: a scenario's fault step, as recap-scenario


WRITE_OPS = frozenset({KubeOp.ROLLOUT_UNDO, KubeOp.ROLLOUT_RESTART, KubeOp.SET_IMAGE})
_READS = frozenset(
    {
        KubeOp.LIST_DEPLOYMENTS,
        KubeOp.GET_DEPLOYMENT,
        KubeOp.LIST_REPLICASETS,
        KubeOp.LIST_PODS,
        KubeOp.LIST_EVENTS,
        KubeOp.POD_LOGS,
        KubeOp.VERSION,
    }
)
# What each identity's Role in recap-demo grants (docs/m5-integration-plan.md, section 3.4).
# `version` reads the API server's /version, which every authenticated identity may read.
IDENTITY_OPS: dict[Identity, frozenset[KubeOp]] = {
    "reader": _READS,
    "writer": frozenset(
        {
            KubeOp.GET_DEPLOYMENT,
            KubeOp.LIST_REPLICASETS,
            KubeOp.ROLLOUT_UNDO,
            KubeOp.ROLLOUT_RESTART,
            KubeOp.VERSION,
        }
    ),
    "oracle": frozenset(
        {KubeOp.LIST_DEPLOYMENTS, KubeOp.GET_DEPLOYMENT, KubeOp.LIST_PODS, KubeOp.VERSION}
    ),
    # reset only: a scenario's image step, and a restart that gives the baseline fresh pods
    # when an earlier run left them unready (both are a `patch` of the two Deployments).
    "scenario": frozenset(
        {
            KubeOp.GET_DEPLOYMENT,
            KubeOp.LIST_REPLICASETS,
            KubeOp.LIST_PODS,
            KubeOp.SET_IMAGE,
            KubeOp.ROLLOUT_RESTART,
            KubeOp.VERSION,
        }
    ),
}

# --- argument checks ------------------------------------------------------------------------

POD_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
LABEL_KEY = re.compile(
    r"^([a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*/)?"
    r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$"
)
LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")
VERSION_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
MAX_REVISION = 1_000_000


def check_name(value: object, what: str = "name") -> str:
    """A DNS-1123 label (a Deployment or container name), else KubeError."""
    if not isinstance(value, str) or not DNS_LABEL.fullmatch(value):
        raise KubeError(f"refused: {what} {_shown(value)} is not a Kubernetes name")
    return value


def check_pod(value: object) -> str:
    """A pod name (a DNS-1123 subdomain of at most 253 characters), else KubeError."""
    if not isinstance(value, str) or len(value) > 253 or not POD_NAME.fullmatch(value):
        raise KubeError(f"refused: pod {_shown(value)} is not a Kubernetes name")
    return value


def selector_text(labels: Mapping[str, str]) -> str:
    """'k1=v1,k2=v2' from validated labels (sorted). A selector is never accepted as text."""
    if not isinstance(labels, Mapping) or not labels:
        raise KubeError("refused: a label selector needs at least one label")
    parts = []
    for key, value in sorted(labels.items()):
        if not isinstance(key, str) or len(key) > 253 or not LABEL_KEY.fullmatch(key):
            raise KubeError(f"refused: label key {_shown(key)} is not a Kubernetes label key")
        if not isinstance(value, str) or not LABEL_VALUE.fullmatch(value):
            raise KubeError(f"refused: label value {_shown(value)} is not a label value")
        parts.append(f"{key}={value}")
    return ",".join(parts)


def _bounded_int(value: object, low: int, high: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise KubeError(f"refused: {what} must be an integer from {low} to {high}")
    return value


def _rfc3339(value: object) -> str:
    if not isinstance(value, datetime):
        raise KubeError("refused: a log window must start at a datetime")
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shown(value: object) -> str:
    """A refused value, shortened and quoted, for an error message."""
    return repr(str(value)[:40])


@dataclass(frozen=True)
class Command:
    """One validated operation: what runs, with which arguments (the argv after the
    global flags), and its typed parameters (for the fake cluster)."""

    op: KubeOp
    tail: tuple[str, ...]
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def writes(self) -> bool:
        return self.op in WRITE_OPS

    @property
    def dry_run(self) -> bool:
        return bool(self.params.get("dry_run"))


def build_command(
    op: KubeOp, *, repository: str = "recap-demo", services: Collection[str] = (), **params: Any
) -> Command:
    """Validate the parameters of `op` and build its argument list (no global flags yet).

    `repository` and `services` bound what set_image may deploy: only
    <repository>/<service>:vX.Y.Z of an allowed service, like the cluster's admission policy.
    """
    if op == KubeOp.LIST_DEPLOYMENTS:
        return Command(op, ("get", "deployments", "-o", "json"))
    if op == KubeOp.GET_DEPLOYMENT:
        name = check_name(params.get("name"), "deployment")
        return Command(op, ("get", "deployment", name, "-o", "json"), {"name": name})
    if op == KubeOp.LIST_REPLICASETS:
        selector = selector_text(params.get("selector") or {})
        return Command(
            op, ("get", "replicasets", "-l", selector, "-o", "json"), {"selector": selector}
        )
    if op == KubeOp.LIST_PODS:
        if params.get("selector") is None:
            return Command(op, ("get", "pods", "-o", "json"), {"selector": None})
        selector = selector_text(params["selector"])
        return Command(op, ("get", "pods", "-l", selector, "-o", "json"), {"selector": selector})
    if op == KubeOp.LIST_EVENTS:
        return Command(op, ("get", "events", "-o", "json"))
    if op == KubeOp.POD_LOGS:
        return _logs_command(params)
    if op == KubeOp.VERSION:
        return Command(op, ("version", "-o", "json"))
    if op == KubeOp.ROLLOUT_UNDO:
        name = check_name(params.get("name"), "deployment")
        revision = _bounded_int(params.get("revision"), 1, MAX_REVISION, "the revision")
        dry_run = bool(params.get("dry_run"))
        undo = ("rollout", "undo", f"deployment/{name}", f"--to-revision={revision}", "-o", "json")
        values = {"name": name, "revision": revision, "dry_run": dry_run}
        return Command(op, (*undo, *_dry(dry_run)), values)
    if op == KubeOp.ROLLOUT_RESTART:
        name = check_name(params.get("name"), "deployment")
        dry_run = bool(params.get("dry_run"))
        restart = ("rollout", "restart", f"deployment/{name}", "-o", "json")
        return Command(op, (*restart, *_dry(dry_run)), {"name": name, "dry_run": dry_run})
    if op == KubeOp.SET_IMAGE:
        return _set_image_command(params, repository, services)
    raise KubeError(f"refused: unknown operation {_shown(op)}")  # pragma: no cover


def _dry(dry_run: bool) -> tuple[str, ...]:
    """A server-side dry run passes RBAC and admission without saving anything."""
    return ("--dry-run=server",) if dry_run else ()


def _logs_command(params: Mapping[str, Any]) -> Command:
    pod = check_pod(params.get("pod"))
    container = check_name(params.get("container"), "container")
    since = _rfc3339(params.get("since_time"))
    tail = _bounded_int(params.get("tail"), 1, 1000, "the line count")
    limit = _bounded_int(params.get("limit_bytes"), 1024, 4_000_000, "the byte limit")
    previous = bool(params.get("previous"))
    argv = (
        "logs",
        f"pod/{pod}",
        "-c",
        container,
        "--prefix",
        "--timestamps",
        f"--since-time={since}",
        f"--tail={tail}",
        f"--limit-bytes={limit}",
        *(("--previous",) if previous else ()),
    )
    values = {
        "pod": pod,
        "container": container,
        "since_time": since,
        "tail": tail,
        "limit_bytes": limit,
        "previous": previous,
    }
    return Command(KubeOp.POD_LOGS, argv, values)


def _set_image_command(
    params: Mapping[str, Any], repository: str, services: Collection[str]
) -> Command:
    """A strategic-merge patch of one container's image plus the change-cause annotation."""
    name = check_name(params.get("name"), "deployment")
    container = check_name(params.get("container"), "container")
    version = params.get("version")
    if not isinstance(version, str) or not VERSION_TAG.fullmatch(version):
        raise KubeError(f"refused: version {_shown(version)} is not vX.Y.Z")
    if name not in services or not REPOSITORY.fullmatch(repository):
        raise KubeError(f"refused: {name} is not an allowed demo Deployment")
    image = f"{repository}/{name}:{version}"
    patch = {
        "metadata": {"annotations": {"kubernetes.io/change-cause": f"release {version}"}},
        "spec": {"template": {"spec": {"containers": [{"name": container, "image": image}]}}},
    }
    body = json.dumps(patch, sort_keys=True, separators=(",", ":"))
    dry_run = bool(params.get("dry_run"))
    tail = ("patch", "deployment", name, "--type=strategic", "-p", body, "-o", "json")
    values = {
        "name": name,
        "container": container,
        "image": image,
        "change_cause": f"release {version}",
        "dry_run": dry_run,
    }
    return Command(KubeOp.SET_IMAGE, (*tail, *_dry(dry_run)), values)


# --- strict output parsing ------------------------------------------------------------------


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def parse_object(text: str, op: KubeOp, kinds: Collection[str]) -> dict[str, Any]:
    """One JSON object whose `kind` is one of `kinds` (or, for version, no kind at all)."""
    try:
        data = json.loads(text, parse_constant=_refuse_constant)
    except ValueError:
        raise KubeOutputError(f"kubectl {op.value} returned output that is not JSON") from None
    if not isinstance(data, dict):
        raise KubeOutputError(f"kubectl {op.value} returned JSON that is not an object")
    if kinds and data.get("kind") not in kinds:
        raise KubeOutputError(
            f"kubectl {op.value} returned kind {_shown(data.get('kind'))}, expected "
            + " or ".join(sorted(kinds))
        )
    return data


def parse_list(text: str, op: KubeOp, item_kind: str) -> list[dict[str, Any]]:
    """The items of a list (kubectl prints a generic 'List'; the API a '<Kind>List')."""
    data = parse_object(text, op, {"List", f"{item_kind}List"})
    items = data.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise KubeOutputError(f"kubectl {op.value} returned a list without valid items")
    for item in items:
        if item.get("kind") not in (None, item_kind):
            raise KubeOutputError(f"kubectl {op.value} returned an item that is not a {item_kind}")
    return items


# --- what may be shown of a failure ---------------------------------------------------------

_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_HOST_PORT = re.compile(r"\[?\b[0-9a-f:.]{2,}\]?:\d{2,5}\b", re.IGNORECASE)
_PATH = re.compile(r"(?:~|\B/|\b[A-Z]:\\)[^\s'\",;:]*")
_TOKEN = re.compile(r"(?i)(?:bearer\s+\S+|eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,})")
MAX_ERROR_CHARS = 300


def sanitize(text: str, hidden: Collection[str] = ()) -> str:
    """kubectl's error text without paths, URLs, addresses or tokens, on one short line.

    kubectl's stderr can name the kubeconfig file, the API server's URL or a token; the text
    reaches agents (as a tool error) and the run record, so all of that is removed first.
    """
    for secret in sorted((h for h in hidden if h), key=len, reverse=True):
        text = text.replace(secret, "<hidden>")
    text = _TOKEN.sub("<token>", text)
    text = _URL.sub("<url>", text)
    text = _HOST_PORT.sub("<address>", text)
    text = _PATH.sub("<path>", text)
    return " ".join(text.split())[:MAX_ERROR_CHARS]


# --- the client interface -------------------------------------------------------------------


@dataclass(frozen=True)
class CommandRecord:
    """One executed command, for the run's command audit (trusted local data, never shown
    to a model). `argv` holds placeholders instead of credential paths."""

    identity: str
    op: str
    argv: list[str]
    exit_code: int
    duration_ms: int
    output_bytes: int
    error: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


CommandAudit = Callable[[CommandRecord], None]


class KubeClient(ABC):
    """One identity's typed operations. Subclasses decide how a validated Command runs."""

    def __init__(
        self,
        *,
        identity: Identity,
        context: str,
        namespace: str,
        request_timeout: float,
        repository: str = "recap-demo",
        services: Collection[str] = (),
        audit: CommandAudit | None = None,
    ) -> None:
        self.identity: Identity = identity
        self.context = context
        self.namespace = check_name(namespace, "namespace")
        self.request_timeout = request_timeout
        self.repository = repository
        self.services = tuple(services)
        self.audit = audit

    # --- the operations -----------------------------------------------------------------

    async def list_deployments(self) -> list[dict[str, Any]]:
        text = await self._run(KubeOp.LIST_DEPLOYMENTS)
        return parse_list(text, KubeOp.LIST_DEPLOYMENTS, "Deployment")

    async def get_deployment(self, name: str) -> dict[str, Any]:
        text = await self._run(KubeOp.GET_DEPLOYMENT, name=name)
        return parse_object(text, KubeOp.GET_DEPLOYMENT, {"Deployment"})

    async def list_replicasets(self, selector: Mapping[str, str]) -> list[dict[str, Any]]:
        text = await self._run(KubeOp.LIST_REPLICASETS, selector=selector)
        return parse_list(text, KubeOp.LIST_REPLICASETS, "ReplicaSet")

    async def list_pods(self, selector: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        text = await self._run(KubeOp.LIST_PODS, selector=selector)
        return parse_list(text, KubeOp.LIST_PODS, "Pod")

    async def list_events(self) -> list[dict[str, Any]]:
        text = await self._run(KubeOp.LIST_EVENTS)
        return parse_list(text, KubeOp.LIST_EVENTS, "Event")

    async def pod_logs(
        self,
        pod: str,
        container: str,
        *,
        since_time: datetime,
        tail: int,
        limit_bytes: int,
        previous: bool = False,
    ) -> str:
        """Log text of one container (`--prefix --timestamps`), oldest line first."""
        return await self._run(
            KubeOp.POD_LOGS,
            pod=pod,
            container=container,
            since_time=since_time,
            tail=tail,
            limit_bytes=limit_bytes,
            previous=previous,
        )

    async def version(self) -> dict[str, Any]:
        text = await self._run(KubeOp.VERSION)
        return parse_object(text, KubeOp.VERSION, ())

    async def rollout_undo(self, name: str, revision: int, *, dry_run: bool) -> dict[str, Any]:
        text = await self._run(KubeOp.ROLLOUT_UNDO, name=name, revision=revision, dry_run=dry_run)
        return parse_object(text, KubeOp.ROLLOUT_UNDO, {"Deployment"})

    async def rollout_restart(self, name: str, *, dry_run: bool) -> dict[str, Any]:
        text = await self._run(KubeOp.ROLLOUT_RESTART, name=name, dry_run=dry_run)
        return parse_object(text, KubeOp.ROLLOUT_RESTART, {"Deployment"})

    async def set_image(
        self, name: str, container: str, version: str, *, dry_run: bool
    ) -> dict[str, Any]:
        text = await self._run(
            KubeOp.SET_IMAGE, name=name, container=container, version=version, dry_run=dry_run
        )
        return parse_object(text, KubeOp.SET_IMAGE, {"Deployment"})

    # --- shared plumbing ----------------------------------------------------------------

    def command(self, op: KubeOp, **params: Any) -> Command:
        """Validate one call: the identity may run `op`, and every argument is well formed."""
        if op not in IDENTITY_OPS[self.identity]:
            raise KubeError(f"refused: the {self.identity} identity may not run {op.value}")
        return build_command(op, repository=self.repository, services=self.services, **params)

    def shown_argv(self, command: Command) -> list[str]:
        """The full argument list as audited: the credential path is a placeholder."""
        return self._argv(command, "kubectl", f"<recap-{self.identity} kubeconfig>")

    def _argv(self, command: Command, kubectl: str, kubeconfig: str) -> list[str]:
        return [
            kubectl,
            "--kubeconfig",
            kubeconfig,
            "--context",
            self.context,
            "--namespace",
            self.namespace,
            "--request-timeout",
            duration(self.request_timeout),
            *command.tail,
        ]

    async def _run(self, op: KubeOp, **params: Any) -> str:
        command = self.command(op, **params)
        started = time.monotonic()
        code, stdout, error = -1, "", ""
        try:
            code, stdout, error = await self.execute(command)
        except KubeError as exc:
            error = str(exc)
            raise
        finally:
            self._audit(command, code, stdout, error, started)
        if code != 0:
            raise KubeError(f"kubectl {op.value} failed (exit {code}): {error or 'no message'}")
        return stdout

    def _audit(self, command: Command, code: int, stdout: str, error: str, started: float) -> None:
        if self.audit is None:
            return
        self.audit(
            CommandRecord(
                identity=self.identity,
                op=command.op.value,
                argv=self.shown_argv(command),
                exit_code=code,
                duration_ms=round((time.monotonic() - started) * 1000),
                output_bytes=len(stdout.encode("utf-8")),
                error=error[:MAX_ERROR_CHARS],
            )
        )

    @abstractmethod
    async def execute(self, command: Command) -> tuple[int, str, str]:
        """Run one validated command: (exit code, stdout, sanitized stderr)."""


def duration(seconds: float) -> str:
    """A kubectl duration: '4s', or '500ms' for a fraction of a second."""
    millis = max(1, round(seconds * 1000))
    return f"{millis // 1000}s" if millis % 1000 == 0 else f"{millis}ms"


def grace(request_timeout: float) -> float:
    """How long after kubectl's own --request-timeout the process is killed: kubectl should
    end the call itself first; this only bounds a hung process start or exit."""
    return min(2.0, max(0.5, request_timeout / 2))


class KubectlClient(KubeClient):
    """Runs operations with the kubectl binary: argument lists, never a shell."""

    MAX_OUTPUT_BYTES = 4_000_000
    MAX_STDERR_BYTES = 64_000

    def __init__(self, *, kubectl: Path, kubeconfig: Path, home: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.kubectl = kubectl
        self.kubeconfig = kubeconfig
        self.home = home

    def child_env(self) -> dict[str, str]:
        """A minimal environment: no inherited variable (no API key, no other KUBECONFIG)."""
        return {
            "KUBECONFIG": str(self.kubeconfig),
            "HOME": str(self.home),  # kubectl's discovery cache, never the user's ~/.kube
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
        }

    async def execute(self, command: Command) -> tuple[int, str, str]:
        argv = self._argv(command, str(self.kubectl), str(self.kubeconfig))
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.child_env(),
                # Its own session: a Ctrl-C in the terminal must not cut a write in half.
                start_new_session=True,
            )
        except OSError as exc:
            raise KubeError(f"kubectl could not be started: {type(exc).__name__}") from None
        limit = self.request_timeout + grace(self.request_timeout)
        readers = [
            asyncio.ensure_future(_read_bounded(process.stdout, self.MAX_OUTPUT_BYTES)),
            asyncio.ensure_future(_read_bounded(process.stderr, self.MAX_STDERR_BYTES)),
        ]
        try:
            async with asyncio.timeout(limit):
                stdout, stderr = await asyncio.gather(*readers)
                code = await process.wait()
        except TimeoutError:
            await _stop(process, readers)
            raise KubeError(
                f"kubectl {command.op.value} did not finish within {limit:g} s"
            ) from None
        except _TooLarge:
            await _stop(process, readers)
            raise KubeOutputError(f"kubectl {command.op.value} printed too much output") from None
        except BaseException:  # cancelled: never leave a kubectl process behind
            await asyncio.shield(_stop(process, readers))
            raise
        hidden = (str(self.kubeconfig), str(self.kubectl), str(self.home))
        text = stderr.decode("utf-8", errors="replace")
        return code, stdout.decode("utf-8", errors="replace"), sanitize(text, hidden)


STOP_SECONDS = 2.0  # at most this long to reap a killed kubectl


class _TooLarge(Exception):
    pass


async def _read_bounded(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while chunk := await stream.read(65536):
        total += len(chunk)
        if total > limit:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


async def _stop(
    process: asyncio.subprocess.Process, readers: Sequence[asyncio.Future[bytes]]
) -> None:
    """Kill kubectl (its whole session: it was started as a session leader) and reap it.
    Its pipes are drained: Python waits for both to report end-of-file before wait()
    returns, and a pipe nobody reads never does. Bounded, whatever is left behind."""
    for reader in readers:
        reader.cancel()
    await asyncio.gather(*readers, return_exceptions=True)
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
    try:
        async with asyncio.timeout(STOP_SECONDS):
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    while await stream.read(65536):
                        pass
            await process.wait()
    except (TimeoutError, OSError):
        pass  # the process is killed; nothing more can be read from it
