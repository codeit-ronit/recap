"""Cluster pinning: before RECAP reads or changes anything, prove it is THE demo cluster.

Checked on every reset and before each adapter's first kubectl call, failing closed
(docs/m5-integration-plan.md, 3.9). For each identity's kubeconfig:

  * the file is private (no group or other access), like the folder the setup writes;
  * it has the pinned context (kind-recap-m5), and that context's API server is https on a
    loopback address: a kubeconfig that points at any other cluster, such as a work
    cluster, stops the run before a single request;
  * TLS verification is on, and the cluster's certificate authority matches the SHA-256
    fingerprint recorded when the cluster was created. Every Kind cluster gets a new CA,
    and kubectl's TLS check then proves the API server belongs to exactly that cluster;
  * the credential is a ServiceAccount token: no exec plugin or auth provider (which would
    run a program) and no client certificate (the administrator's kind of credential).

The kubeconfig is read here only to check those facts; its token is never copied, logged or
returned. Error messages name the identity, never the file's path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes

from recap.environments.kubernetes.options import is_loopback_host

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class PreflightError(RuntimeError):
    """The configured cluster is not provably the pinned demo cluster: nothing may run."""


def read_fingerprint(path: Path | None) -> str:
    """The recorded CA fingerprint: 64 hex digits (colons, case and "sha256:" are ignored)."""
    if path is None or not path.is_file():
        raise PreflightError("the recorded CA fingerprint (ca.sha256) is missing")
    words = path.read_text(encoding="utf-8").split()
    raw = words[0] if words else ""
    value = raw.lower().removeprefix("sha256:").replace(":", "")
    if not _HEX64.fullmatch(value):
        raise PreflightError("the recorded CA fingerprint is not a SHA-256 hex digest")
    return value


def ca_fingerprints(pem: bytes) -> set[str]:
    """SHA-256 of the CA certificate: of its DER bytes (the usual X.509 fingerprint, what
    `openssl x509 -fingerprint -sha256` prints) and of the PEM text as written; either may
    have been recorded at setup."""
    found = {hashlib.sha256(pem).hexdigest()}
    try:
        certificate = x509.load_pem_x509_certificate(pem)
    except ValueError:
        return found
    found.add(certificate.fingerprint(hashes.SHA256()).hex())
    return found


def check_kubeconfig(identity: str, path: Path | None, context: str, fingerprint: str) -> None:
    """Refuse (PreflightError) unless this kubeconfig provably reaches the pinned cluster."""
    who = f"the {identity} kubeconfig"
    if path is None:
        raise PreflightError(f"{who} is not configured")
    if not path.is_file():
        raise PreflightError(f"{who} is missing")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise PreflightError(f"{who} is readable by other users (it must be mode 600)")
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise PreflightError(f"{who} cannot be read as YAML") from None
    if not isinstance(config, dict):
        raise PreflightError(f"{who} is not a kubeconfig")
    entry = _named(config, "contexts", context, "context")
    if entry is None:
        raise PreflightError(f"{who} has no context {context!r}")
    cluster = _named(config, "clusters", str(entry.get("cluster") or ""), "cluster")
    user = _named(config, "users", str(entry.get("user") or ""), "user")
    if cluster is None or user is None:
        raise PreflightError(f"{who}: context {context!r} names a missing cluster or user")
    _check_server(who, cluster)
    _check_ca(who, cluster, path.parent, fingerprint)
    _check_credential(who, user)


def _named(config: dict[str, Any], section: str, name: str, key: str) -> dict[str, Any] | None:
    """The `key` mapping of the entry called `name` in a kubeconfig section."""
    entries = config.get(section)
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("name") == name and name:
            value = entry.get(key)
            return value if isinstance(value, dict) else None
    return None


def _check_server(who: str, cluster: dict[str, Any]) -> None:
    server = urlsplit(str(cluster.get("server") or ""))
    if server.scheme != "https" or not is_loopback_host(server.hostname):
        raise PreflightError(f"{who} points at an API server that is not https on loopback")
    if cluster.get("insecure-skip-tls-verify") is True:
        raise PreflightError(f"{who} turns TLS verification off")
    if cluster.get("proxy-url"):
        raise PreflightError(f"{who} sends its requests through a proxy")


def _check_ca(who: str, cluster: dict[str, Any], folder: Path, fingerprint: str) -> None:
    pem: bytes | None = None
    inline = cluster.get("certificate-authority-data")
    if isinstance(inline, str) and inline:
        try:
            pem = base64.b64decode(inline, validate=True)
        except (binascii.Error, ValueError):
            raise PreflightError(f"{who}: the CA data is not base64") from None
    elif isinstance(cluster.get("certificate-authority"), str):
        ca_file = Path(cluster["certificate-authority"]).expanduser()
        ca_file = ca_file if ca_file.is_absolute() else folder / ca_file
        if not ca_file.is_file():
            raise PreflightError(f"{who}: the CA file is missing")
        pem = ca_file.read_bytes()
    if not pem:
        raise PreflightError(f"{who} names no certificate authority")
    if fingerprint not in ca_fingerprints(pem):
        raise PreflightError(
            f"{who}: the cluster's CA does not match the recorded fingerprint "
            "(another cluster, or a recreated one: record its fingerprint again)"
        )


def _check_credential(who: str, user: dict[str, Any]) -> None:
    for runs_a_program in ("exec", "auth-provider"):
        if user.get(runs_a_program):
            raise PreflightError(f"{who} uses an {runs_a_program} credential plugin")
    if user.get("client-certificate-data") or user.get("client-certificate"):
        raise PreflightError(f"{who} uses a client certificate, not a ServiceAccount token")
    if not (user.get("token") or user.get("tokenFile")):
        raise PreflightError(f"{who} holds no ServiceAccount token")
