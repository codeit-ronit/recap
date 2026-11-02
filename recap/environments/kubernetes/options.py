"""The Kind adapter's options: what a target profile's environment.yaml may set.

Every option is checked here, before anything runs, and nothing is read from disk: the
runtime builds an adapter for every GET /system and every run start, so the constructor
must stay cheap and side-effect free. Names must be Kubernetes names, every URL must point
at the loopback interface, and kubectl must be an absolute path (a launchd PATH may not
contain Homebrew's folder, and a relative name could pick up another binary).

No option may hold a credential. The options travel to the MCP server subprocess as JSON on
its command line (visible in `ps`), so they carry only paths to the private credentials
folder, never a token; and no path or URL is ever shown in describe(), a trace or a record.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from recap.core.config import REPO_ROOT
from recap.core.errors import ConfigError
from recap.core.models import Contract

Identity = Literal["reader", "writer", "oracle", "scenario"]
# The four ServiceAccounts in recap-system (docs/m5-integration-plan.md, section 3.4). The
# cluster administrator's credential is deliberately not one of them: RECAP never uses it.
IDENTITIES: tuple[Identity, ...] = ("reader", "writer", "oracle", "scenario")
KUBECONFIG_FILES: dict[Identity, str] = {
    identity: f"recap-{identity}.kubeconfig" for identity in IDENTITIES
}
CA_FINGERPRINT_FILE = "ca.sha256"
WRITE_SWITCH_FILE = "kind-writes-disabled"  # in the state folder: present = every write refused

# A DNS-1123 label: Deployment, container, namespace and service names.
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
CONTEXT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,99}$")
REPOSITORY = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
URL_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,63}$")
RATE_WINDOW = re.compile(r"^[1-9][0-9]{0,2}[sm]$")  # a PromQL range such as "1m" or "90s"


def is_loopback_host(host: str | None) -> bool:
    """True for 127.0.0.0/8, ::1 and "localhost": addresses no other machine can reach."""
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _loopback_url(value: str, what: str) -> str:
    """A plain http(s) URL on the loopback interface, without credentials, query or fragment."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not is_loopback_host(parts.hostname):
        raise ValueError(f"{what} must be an http(s) URL on the loopback interface")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(f"{what} may not carry credentials, a query or a fragment")
    if parts.path not in ("", "/"):
        raise ValueError(f"{what} must be a base URL without a path")
    return value.rstrip("/")


class KindOptions(Contract):
    """Options of the `kind` adapter. Unknown keys are refused, so a typo fails at start."""

    # A time limit of NaN or infinity would mean "wait forever": refused like a typo.
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    # Injected by load_settings for every adapter (recap/core/config.py).
    scenarios_dir: str
    state_dir: str
    # The simulator's latency knobs, also injected: a real cluster has real latency, so the
    # Kind adapter accepts and ignores them.
    tool_latency_ms: int = 0
    write_latency_ms: int = 0

    # How the adapter reaches the cluster: the kubectl binary, or an in-process fake cluster
    # that serves synthetic Kubernetes JSON (offline tests only; describe() says so).
    client: Literal["kubectl", "fake"] = "kubectl"
    fake_data_dir: str | None = None  # the fake's seed data (repo-relative or absolute)
    kubectl: str | None = None  # absolute path of the kubectl binary (client "kubectl")

    # Where the four identities' kubeconfigs and the recorded CA fingerprint live: the
    # private folder (outside git), or explicit paths that override it.
    credentials_dir: str | None = None
    kubeconfigs: dict[Identity, str] = Field(default_factory=dict)
    ca_fingerprint_file: str | None = None

    # The pinned cluster and the only namespace and services RECAP may see or change.
    cluster: str = "recap-m5"
    context: str = "kind-recap-m5"
    namespace: str = "recap-demo"
    services: list[str] = Field(default_factory=lambda: ["frontend", "backend"])
    image_repository: str = "recap-demo"  # images are <repository>/<service>:vX.Y.Z

    # Loopback endpoints: each service's NodePort, the path its probe asks, and Prometheus.
    service_urls: dict[str, str] = Field(
        default_factory=lambda: {
            "frontend": "http://127.0.0.1:30080",
            "backend": "http://127.0.0.1:30081",
        }
    )
    probe_paths: dict[str, str] = Field(
        default_factory=lambda: {"frontend": "/", "backend": "/items"}
    )
    prometheus_url: str = "http://127.0.0.1:30090"
    metrics_rate_window: str = "1m"  # the rate() range of the error-rate and latency queries
    metrics_max_points: int = Field(60, ge=2, le=240)  # the step grows to stay under this

    # Time bounds. Every trusted call bounds itself: nothing wraps health_check or reset.
    request_timeout_seconds: float = Field(4.0, gt=0, le=30)  # kubectl --request-timeout
    write_deadline_seconds: float = Field(8.0, gt=0, le=9.5)  # dry run + write + read-back
    stabilization_timeout_seconds: float = Field(180.0, ge=0)
    stabilization_poll_seconds: float = Field(5.0, gt=0)
    probe_requests: int = Field(10, ge=1, le=50)
    probe_timeout_seconds: float = Field(1.0, gt=0, le=10)
    probe_round_gap_seconds: float = Field(2.0, ge=0, le=30)
    baseline_timeout_seconds: float = Field(240.0, gt=0)
    calm_timeout_seconds: float = Field(420.0, gt=0)
    baseline_margin_seconds: float = Field(120.0, ge=0, le=3600)
    alert_poll_seconds: float = Field(3.0, gt=0)
    lease_seconds: float = Field(3600.0, gt=0)
    max_clock_skew_seconds: float = Field(5.0, gt=0)
    write_switch_file: str | None = None

    @field_validator("cluster", "namespace")
    @classmethod
    def _dns_label(cls, value: str) -> str:
        if not DNS_LABEL.fullmatch(value):
            raise ValueError("must be a DNS-1123 label")
        return value

    @field_validator("context")
    @classmethod
    def _context(cls, value: str) -> str:
        if not CONTEXT_NAME.fullmatch(value):
            raise ValueError("must be a plain kubeconfig context name")
        return value

    @field_validator("image_repository")
    @classmethod
    def _repository(cls, value: str) -> str:
        if not REPOSITORY.fullmatch(value):
            raise ValueError("must be an image repository path such as 'recap-demo'")
        return value

    @field_validator("metrics_rate_window")
    @classmethod
    def _rate_window(cls, value: str) -> str:
        if not RATE_WINDOW.fullmatch(value):
            raise ValueError("must be a PromQL range such as '1m' or '90s'")
        return value

    @field_validator("prometheus_url")
    @classmethod
    def _prometheus(cls, value: str) -> str:
        return _loopback_url(value, "prometheus_url")

    @field_validator("service_urls")
    @classmethod
    def _service_urls(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            service: _loopback_url(url, f"the URL of {service}") for service, url in value.items()
        }

    @field_validator("kubectl")
    @classmethod
    def _kubectl(cls, value: str | None) -> str | None:
        if value is not None and not Path(value).expanduser().is_absolute():
            raise ValueError("must be an absolute path (a launchd PATH may not find kubectl)")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> KindOptions:
        if not self.services or len(set(self.services)) != len(self.services):
            raise ValueError("services must list each allowed Deployment once")
        for service in self.services:
            if not DNS_LABEL.fullmatch(service):
                raise ValueError("every service must be a DNS-1123 label")
        if set(self.service_urls) != set(self.services):
            raise ValueError("service_urls must name exactly the allowed services")
        for service, path in self.probe_paths.items():
            if service not in self.services or not URL_PATH.fullmatch(path):
                raise ValueError("probe_paths must map allowed services to plain URL paths")
        if self.client == "kubectl" and self.kubectl is None:
            raise ValueError("client 'kubectl' needs the absolute path of the kubectl binary")
        if self.client == "fake" and self.fake_data_dir is None:
            raise ValueError("client 'fake' needs fake_data_dir")
        return self

    # --- derived locations (pure path arithmetic: nothing is read here) --------------------

    def kubeconfig_path(self, identity: Identity) -> Path | None:
        """The kubeconfig of one identity, or None when none is configured."""
        explicit = self.kubeconfigs.get(identity)
        if explicit:
            return Path(explicit).expanduser()
        if self.credentials_dir:
            return Path(self.credentials_dir).expanduser() / KUBECONFIG_FILES[identity]
        return None

    def fingerprint_path(self) -> Path | None:
        """The recorded SHA-256 fingerprint of the cluster's certificate authority."""
        if self.ca_fingerprint_file:
            return Path(self.ca_fingerprint_file).expanduser()
        if self.credentials_dir:
            return Path(self.credentials_dir).expanduser() / CA_FINGERPRINT_FILE
        return None

    def kubectl_path(self) -> Path | None:
        return Path(self.kubectl).expanduser() if self.kubectl else None

    def fake_data_path(self) -> Path:
        """The fake cluster's seed folder; a relative path is read from the repository."""
        path = Path(self.fake_data_dir or "").expanduser()
        return path if path.is_absolute() else REPO_ROOT / path

    def write_switch_path(self) -> Path:
        if self.write_switch_file:
            return Path(self.write_switch_file).expanduser()
        return Path(self.state_dir) / WRITE_SWITCH_FILE

    def service_url(self, service: str, path: str) -> str:
        return f"{self.service_urls[service]}{path}"

    def probe_url(self, service: str) -> str:
        return self.service_url(service, self.probe_paths.get(service, "/"))


def parse_options(options: dict[str, object]) -> KindOptions:
    """Validate the adapter options; a ConfigError names the bad keys, never their values."""
    kubeconfigs = options.get("kubeconfigs")
    if isinstance(kubeconfigs, dict) and "admin" in kubeconfigs:
        raise ConfigError("the Kind adapter never uses the cluster administrator's credential")
    try:
        return KindOptions.model_validate(options)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'options'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigError(f"invalid Kind adapter options: {problems}") from None
