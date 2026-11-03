"""Pure translation from Kubernetes JSON to RECAP's observation models.

No I/O here: every function takes the parsed objects kubectl printed and returns models
from recap/core/observations.py (or the facts the adapter builds them from). The rules
that need care on a real cluster (the M5 audit's gotchas) live here, each next to its why:

* versions are image tags: in the vX.Y.Z form on the demo cluster (any other tag reads as
  "unknown"), or, on a target whose images carry other release tags (SREGym's hotel
  reservation: date and commit tags, each pinned by a digest), the tag itself ("tag" format);
* "BackOff" keeps its meaning in RECAP's conventions (recap/core/signals.py counts it as
  memory evidence, because in the simulator it always follows an out-of-memory kill).
  Kubernetes uses that one reason for every back-off, so a pod that cannot pull its image
  becomes "ImagePullBackOff", only a back-off after an OOMKilled termination stays
  "BackOff", and every other back-off, whether its container last ended with an error at
  start-up or its pod is gone and nothing is left to tell, becomes "CrashLoopBackOff";
* "OOMKilled" is not an Event reason: it lives in a container's last termination state. The
  adapter reports it as a ClusterEvent whose message says it was read from pod status, so
  nothing is presented as an event the API never stored;
* log levels are read from the line format "<time>Z <LEVEL> <message>" (the demo app's, and
  zerolog's console format, whose levels are three letters), with WARNING/WRN -> WARN,
  FATAL/CRITICAL/PANIC/FTL/PNC -> ERROR and TRC/DBG/INF mapped alike; a line in another
  format keeps its text at level INFO, with the kubelet's timestamp;
* terminal colour codes are removed from log text (zerolog's console writer adds them);
* every timestamp is UTC.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from recap.core.observations import ClusterEvent, LogLine, ServiceInfo, ServiceStatus
from recap.environments.kubernetes.kubectl import (
    LABEL_KEY,
    LABEL_VALUE,
    VERSION_TAG,
    KubeOutputError,
)
from recap.environments.kubernetes.options import DNS_LABEL

DEPENDS_ON = "recap.dev/depends-on"  # set by the demo manifests: Kubernetes knows no dependencies
UNKNOWN_VERSION = "unknown"
MAX_MESSAGE_CHARS = 2000

# How a target names its releases: "semver" (vX.Y.Z tags, the demo cluster) or "tag" (any
# image tag, also when a digest pins it). A release is only ever a key into a Deployment's own
# revision history: an approved rollback restores the image that revision ran, never an image
# composed from the key.
ReleaseFormat = Literal["semver", "tag"]
# A container image tag (the reference grammar of the OCI distribution spec).
RELEASE_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")

# --- small accessors ------------------------------------------------------------------------


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def name_of(obj: Mapping[str, Any]) -> str:
    return str(_dict(obj.get("metadata")).get("name") or "")


def labels_of(obj: Mapping[str, Any]) -> dict[str, str]:
    return {str(k): str(v) for k, v in _dict(_dict(obj.get("metadata")).get("labels")).items()}


def annotations_of(obj: Mapping[str, Any]) -> dict[str, str]:
    raw = _dict(_dict(obj.get("metadata")).get("annotations"))
    return {str(k): str(v) for k, v in raw.items()}


def parse_time(value: Any) -> datetime | None:
    """An RFC 3339 time from the API or the kubelet (nanoseconds cut to microseconds), in UTC."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    match = re.fullmatch(r"(.*T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2})?", text)
    if match is None:
        return None
    base, fraction, zone = match.groups()
    fraction = (fraction or "")[:7]  # "." plus at most 6 digits: Python's resolution
    try:
        moment = datetime.fromisoformat(base + fraction + (zone or "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(UTC)


def created_at(obj: Mapping[str, Any]) -> datetime | None:
    return parse_time(_dict(obj.get("metadata")).get("creationTimestamp"))


# --- Deployments ----------------------------------------------------------------------------


def template_of(obj: Mapping[str, Any]) -> dict[str, Any]:
    """The pod template of a Deployment or a ReplicaSet."""
    return _dict(_dict(obj.get("spec")).get("template"))


def container_of(
    template: Mapping[str, Any], service: str, repository: str
) -> dict[str, Any] | None:
    """The service's own container: the one running <repository>/<service>:..., else the
    one named like the service, else the only one."""
    containers = [c for c in _list(_dict(template.get("spec")).get("containers")) if _dict(c)]
    prefix = f"{repository}/{service}:"
    for container in containers:
        if str(container.get("image", "")).startswith(prefix):
            return dict(container)
    for container in containers:
        if container.get("name") == service:
            return dict(container)
    return dict(containers[0]) if len(containers) == 1 else None


def image_of(obj: Mapping[str, Any], service: str, repository: str) -> str | None:
    container = container_of(template_of(obj), service, repository)
    image = container.get("image") if container else None
    return image if isinstance(image, str) and image else None


def version_of(image: str | None, release_format: ReleaseFormat = "semver") -> str | None:
    """'recap-demo/backend:v1.1.0' -> 'v1.1.0'; None for a digest or a non-vX.Y.Z tag.

    In the "tag" format any tag is the release, also when a digest pins the image:
    'ghcr.io/sregym/hotel-reservation:20260912.1@sha256:...' -> '20260912.1'; an image
    without a tag (a digest alone, or 'registry:5000/app') has none.
    """
    if not image:
        return None
    if release_format == "tag":
        name = image.split("@", 1)[0]
        _, colon, tag = name.rpartition(":")
        return tag if colon and "/" not in tag and RELEASE_TAG.fullmatch(tag) else None
    if "@" in image:
        return None
    _, _, tag = image.rpartition(":")
    return tag if VERSION_TAG.fullmatch(tag) and "/" not in tag else None


def valid_release(value: object, release_format: ReleaseFormat = "semver") -> bool:
    """Whether `value` has the shape of a release of this target (an approved rollback's)."""
    pattern = RELEASE_TAG if release_format == "tag" else VERSION_TAG
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def desired_replicas(deployment: Mapping[str, Any]) -> int:
    """spec.replicas (Kubernetes' default is 1 when it is left out)."""
    spec = _dict(deployment.get("spec"))
    return _int(spec.get("replicas"), 1) if "replicas" in spec else 1


def ready_replicas(deployment: Mapping[str, Any]) -> int:
    return _int(_dict(deployment.get("status")).get("readyReplicas"))


def selector_of(deployment: Mapping[str, Any]) -> dict[str, str]:
    """The Deployment's label selector, as validated labels (matchExpressions are refused:
    the adapter never passes a selector it did not build itself)."""
    selector = _dict(_dict(deployment.get("spec")).get("selector"))
    labels = _dict(selector.get("matchLabels"))
    if not labels or selector.get("matchExpressions"):
        raise KubeOutputError(f"deployment {name_of(deployment)} has no plain label selector")
    checked = {str(k): str(v) for k, v in labels.items()}
    for key, value in checked.items():
        if not LABEL_KEY.fullmatch(key) or not LABEL_VALUE.fullmatch(value):
            raise KubeOutputError(f"deployment {name_of(deployment)} has an invalid selector")
    return checked


def matches(labels: Mapping[str, str], selector: Mapping[str, str]) -> bool:
    return all(labels.get(key) == value for key, value in selector.items())


def dependencies(deployment: Mapping[str, Any], allowed: Iterable[str]) -> list[str]:
    """The services this one calls, from the manifests' recap.dev/depends-on annotation."""
    allowed_set = set(allowed)
    raw = annotations_of(deployment).get(DEPENDS_ON, "")
    names = [part.strip() for part in raw.split(",") if part.strip()]
    found = [n for n in names if DNS_LABEL.fullmatch(n) and n in allowed_set]
    return list(dict.fromkeys(found))


def service_info(
    deployment: Mapping[str, Any],
    namespace: str,
    allowed: Iterable[str],
    repository: str,
    release_format: ReleaseFormat = "semver",
    documented: Mapping[str, Collection[str]] | None = None,
) -> ServiceInfo:
    """One Deployment as a ServiceInfo. Its dependencies come from its own annotation, else
    from the target profile's documented call graph (`documented`): Kubernetes knows none."""
    service = name_of(deployment)
    allowed = list(allowed)
    found = dependencies(deployment, allowed)
    if not found and documented:
        found = [d for d in documented.get(service, ()) if d in allowed and d != service]
    image = image_of(deployment, service, repository)
    return ServiceInfo(
        name=service,
        namespace=namespace,
        version=version_of(image, release_format) or UNKNOWN_VERSION,
        replicas=desired_replicas(deployment),
        dependencies=found,
    )


# --- resource limits ------------------------------------------------------------------------

_QUANTITY = re.compile(
    r"^([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|Ei|m|k|M|G|T|P|E)?$"
)
_FACTORS = {
    None: 1.0,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
    "Ki": 2.0**10,
    "Mi": 2.0**20,
    "Gi": 2.0**30,
    "Ti": 2.0**40,
    "Pi": 2.0**50,
    "Ei": 2.0**60,
}


def parse_quantity(value: Any) -> float | None:
    """A Kubernetes quantity ('128Mi', '250m', '1.5') as a plain number; None if unreadable."""
    match = _QUANTITY.fullmatch(str(value).strip()) if value is not None else None
    if match is None:
        return None
    return float(match.group(1)) * _FACTORS[match.group(2)]


def limit_of(obj: Mapping[str, Any], service: str, repository: str, resource: str) -> float | None:
    """The service container's limit for `resource` ('memory' in bytes, 'cpu' in cores)."""
    container = container_of(template_of(obj), service, repository) or {}
    limits = _dict(_dict(container.get("resources")).get("limits"))
    return parse_quantity(limits.get(resource))


# --- pods -----------------------------------------------------------------------------------


def container_statuses(pod: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [_dict(s) for s in _list(_dict(pod.get("status")).get("containerStatuses"))]


def _waiting_reasons(pods: Iterable[Mapping[str, Any]]) -> list[str]:
    return [
        str(_dict(_dict(status.get("state")).get("waiting")).get("reason") or "")
        for pod in pods
        for status in container_statuses(pod)
    ]


IMAGE_PULL_REASONS = frozenset({"ImagePullBackOff", "ErrImagePull", "InvalidImageName"})


def crash_looping(pods: Iterable[Mapping[str, Any]]) -> bool:
    return "CrashLoopBackOff" in _waiting_reasons(pods)


def image_pull_failing(pods: Iterable[Mapping[str, Any]]) -> bool:
    return any(reason in IMAGE_PULL_REASONS for reason in _waiting_reasons(pods))


def pod_phase(pod: Mapping[str, Any]) -> str:
    return str(_dict(pod.get("status")).get("phase") or "")


def pod_ready(pod: Mapping[str, Any]) -> bool:
    conditions = _list(_dict(pod.get("status")).get("conditions"))
    return any(
        _dict(c).get("type") == "Ready" and _dict(c).get("status") == "True" for c in conditions
    )


def restart_count(pods: Iterable[Mapping[str, Any]]) -> int:
    return sum(
        _int(status.get("restartCount")) for pod in pods for status in container_statuses(pod)
    )


def agent_status(
    deployment: Mapping[str, Any] | None, pods: list[dict[str, Any]], stuck: bool
) -> ServiceStatus:
    """What a dashboard would show: CrashLoopBackOff, Running if pods run, otherwise Unknown
    (pending pods, image-pull failures, a stuck rollout), Deleted if the Deployment is gone."""
    if deployment is None:
        return "Deleted"
    if crash_looping(pods):
        return "CrashLoopBackOff"
    if stuck or image_pull_failing(pods):
        return "Unknown"
    if any(pod_phase(pod) == "Running" for pod in pods):
        return "Running"
    return "Unknown"


def ground_status(
    deployment: Mapping[str, Any] | None, pods: list[dict[str, Any]], rollout_complete: bool
) -> ServiceStatus:
    """The oracle's status: Running ONLY when the rollout is complete (surge pods, old pods
    still terminating or new pods not yet ready never look healthy)."""
    if deployment is None:
        return "Deleted"
    if crash_looping(pods):
        return "CrashLoopBackOff"
    return "Running" if rollout_complete else "Unknown"


# --- events ---------------------------------------------------------------------------------

_PULL_MESSAGE = re.compile(r"(?i)\b(pull|pulling|image)\b")


def event_time(event: Mapping[str, Any]) -> datetime | None:
    """When an event last happened: lastTimestamp, the series' last time, eventTime, ..."""
    series = _dict(event.get("series"))
    for value in (
        event.get("lastTimestamp"),
        series.get("lastObservedTime"),
        event.get("eventTime"),
        event.get("firstTimestamp"),
        _dict(event.get("metadata")).get("creationTimestamp"),
    ):
        moment = parse_time(value)
        if moment is not None:
            return moment
    return None


def event_first_time(event: Mapping[str, Any]) -> datetime | None:
    """When an event first happened (an aggregated back-off's start): firstTimestamp, the
    events.k8s.io field for it, or eventTime."""
    for value in (
        event.get("firstTimestamp"),
        event.get("deprecatedFirstTimestamp"),
        event.get("eventTime"),
    ):
        moment = parse_time(value)
        if moment is not None:
            return moment
    return None


def last_terminations(pods: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Pod name -> why its containers last terminated ("OOMKilled", "Error", ...)."""
    found: dict[str, set[str]] = {}
    for pod in pods:
        reasons = found.setdefault(name_of(pod), set())
        for status in container_statuses(pod):
            for state_key in ("lastState", "state"):
                terminated = _dict(_dict(status.get(state_key)).get("terminated"))
                if terminated.get("reason"):
                    reasons.add(str(terminated["reason"]))
    return found


def event_reason(event: Mapping[str, Any], terminated: Collection[str] = ()) -> str:
    """The event's reason in RECAP's conventions (see the module docstring for "BackOff")."""
    reason = str(event.get("reason") or "Unknown")
    if reason != "BackOff":
        return reason
    if _PULL_MESSAGE.search(str(event.get("message") or "")) and "restart" not in str(
        event.get("message") or ""
    ):
        return "ImagePullBackOff"
    if "OOMKilled" in terminated:
        return reason  # RECAP's convention: a back-off after an out-of-memory kill
    # Any other back-off restarts a crashing container: a crash loop, whose cause is not known
    # to be memory (its pod may be gone, with nothing left to tell). It is never reported as
    # memory evidence that nothing shows (M5: SREGym's start-up back-offs were).
    return "CrashLoopBackOff"


def cluster_event(
    event: Mapping[str, Any], service: str, terminated: Collection[str] = ()
) -> ClusterEvent | None:
    moment = event_time(event)
    involved = _dict(event.get("involvedObject") or event.get("regarding"))
    kind, name = str(involved.get("kind") or ""), str(involved.get("name") or "")
    if moment is None or not kind or not name:
        return None
    series = _dict(event.get("series"))
    count = _int(event.get("count")) or _int(series.get("count")) or 1
    kind_type = "Warning" if event.get("type") == "Warning" else "Normal"
    first = event_first_time(event)
    return ClusterEvent(
        timestamp=moment,
        type=kind_type,
        reason=event_reason(event, terminated),
        object=f"{kind.lower()}/{name}",
        service=service,
        message=str(event.get("message") or event.get("note") or "")[:MAX_MESSAGE_CHARS],
        count=max(1, count),
        first_seen=first if first is not None and first <= moment else None,
    )


def oom_kills(pods: Iterable[Mapping[str, Any]], service: str) -> list[ClusterEvent]:
    """OOMKilled terminations read from container status, presented as what they are."""
    found: list[ClusterEvent] = []
    for pod in pods:
        for status in container_statuses(pod):
            for state_key in ("lastState", "state"):
                terminated = _dict(_dict(status.get(state_key)).get("terminated"))
                if terminated.get("reason") != "OOMKilled":
                    continue
                moment = parse_time(terminated.get("finishedAt")) or parse_time(
                    terminated.get("startedAt")
                )
                if moment is None:
                    continue
                container = str(status.get("name") or "?")
                code = terminated.get("exitCode", 137)
                found.append(
                    ClusterEvent(
                        timestamp=moment,
                        type="Warning",
                        reason="OOMKilled",
                        object=f"pod/{name_of(pod)}",
                        service=service,
                        message=(
                            f"container {container} was OOMKilled (exit code {code}); read from "
                            "the pod's container status, not from a Kubernetes event"
                        ),
                    )
                )
    return found


# --- logs -----------------------------------------------------------------------------------

_PREFIX = re.compile(r"^\[(?:pod/)?(?P<pod>[^/\]\s]+)/(?P<container>[^\]\s]+)\] ")
# Terminal control sequences (CSI, e.g. colours): zerolog's console writer colours by default.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_STAMP = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})) ")
_APP_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) (?P<level>[A-Za-z]+) (?P<message>.*)$"
)
LogLevel = Literal["DEBUG", "INFO", "WARN", "ERROR"]
LEVELS: dict[str, LogLevel] = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "NOTICE": "INFO",
    "WARN": "WARN",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "ERR": "ERROR",
    "FATAL": "ERROR",
    "CRITICAL": "ERROR",
    "CRIT": "ERROR",
    "PANIC": "ERROR",
    # zerolog's console writer (Go services such as DeathStarBench's) writes three letters.
    "TRC": "DEBUG",
    "DBG": "DEBUG",
    "INF": "INFO",
    "WRN": "WARN",
    "FTL": "ERROR",
    "PNC": "ERROR",
}


def log_lines(service: str, text: str) -> list[LogLine]:
    """Lines of `kubectl logs --prefix --timestamps`, oldest first within the text."""
    lines: list[LogLine] = []
    for raw in text.splitlines():
        line = _ANSI.sub("", _PREFIX.sub("", raw.rstrip("\r"), count=1))
        stamp = _STAMP.match(line)
        kubelet_time = parse_time(stamp.group("ts")) if stamp else None
        body = line[stamp.end() :] if stamp else line
        if not body.strip():
            continue
        app = _APP_LINE.match(body)
        level_word = app.group("level").upper() if app else ""
        level: LogLevel = "INFO"
        if app and level_word in LEVELS:
            moment = parse_time(app.group("ts")) or kubelet_time
            level, message = LEVELS[level_word], app.group("message")
        else:
            moment, message = kubelet_time, body
        if moment is None:
            continue  # a line without any time cannot be placed in the incident
        lines.append(
            LogLine(
                timestamp=moment,
                level=level,
                service=service,
                message=message[:MAX_MESSAGE_CHARS],
            )
        )
    return lines
