"""Cluster pinning before any read or write (recap/environments/kubernetes/preflight.py).

A kubeconfig that points anywhere but the pinned demo cluster stops RECAP before a single
request: the pinned context, an https API server on loopback, TLS verification on, the CA
fingerprint recorded at creation, a ServiceAccount token (no exec plugin, no client
certificate) and a private file. The credentials here are generated into tmp_path by
`write_credentials` (a fresh self-signed CA, a dummy token): nothing is committed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from recap.environments.kubernetes.options import IDENTITIES, KUBECONFIG_FILES
from recap.environments.kubernetes.preflight import (
    PreflightError,
    ca_fingerprints,
    check_kubeconfig,
    read_fingerprint,
)

CONTEXT = "kind-recap-m5"


def ca_certificate() -> bytes:
    """A throwaway self-signed CA certificate (PEM), like the one Kind makes per cluster."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kubernetes")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(Encoding.PEM)


def kubeconfig(identity: str, pem: bytes, **changes: Any) -> dict[str, Any]:
    """A kubeconfig like mint-tokens.sh writes: one cluster, one context, one token."""
    cluster = {
        "server": changes.pop("server", "https://127.0.0.1:6443"),
        "certificate-authority-data": base64.b64encode(pem).decode(),
        **changes.pop("cluster", {}),
    }
    user = changes.pop("user", {"token": f"offline-test-{identity}-not-a-real-token"})
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "kind-recap-m5", "cluster": cluster}],
        "contexts": [
            {
                "name": changes.pop("context", CONTEXT),
                "context": {"cluster": "kind-recap-m5", "user": f"recap-{identity}"},
            }
        ],
        "users": [{"name": f"recap-{identity}", "user": user}],
        "current-context": CONTEXT,
    }


def write_credentials(folder: Path, **changes: Any) -> Path:
    """A credentials folder for the four identities plus ca.sha256 (mode 700 / 600)."""
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    pem = ca_certificate()
    for identity in IDENTITIES:
        path = folder / KUBECONFIG_FILES[identity]
        path.write_text(yaml.safe_dump(kubeconfig(identity, pem, **dict(changes))), "utf-8")
        path.chmod(0o600)
    der = x509.load_pem_x509_certificate(pem).fingerprint(hashes.SHA256()).hex()
    (folder / "ca.sha256").write_text(der + "\n", encoding="utf-8")
    return folder


@pytest.fixture
def credentials(tmp_path: Path) -> Path:
    return write_credentials(tmp_path / "kind-creds")


def check(folder: Path, identity: str = "reader") -> None:
    fingerprint = read_fingerprint(folder / "ca.sha256")
    path = folder / f"recap-{identity}.kubeconfig"
    check_kubeconfig(identity, path, CONTEXT, fingerprint)


def test_the_pinned_cluster_passes_for_every_identity(credentials: Path) -> None:
    for identity in IDENTITIES:
        check(credentials, identity)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"server": "https://10.0.0.5:6443"}, "not https on loopback"),
        ({"server": "https://work-cluster.example.com"}, "not https on loopback"),
        ({"server": "http://127.0.0.1:6443"}, "not https on loopback"),
        ({"cluster": {"insecure-skip-tls-verify": True}}, "TLS verification off"),
        ({"cluster": {"proxy-url": "http://127.0.0.1:3128"}}, "through a proxy"),
        ({"context": "kind-other"}, "has no context"),
        ({"user": {"exec": {"command": "aws"}}}, "exec credential plugin"),
        ({"user": {"auth-provider": {"name": "oidc"}}}, "auth-provider credential plugin"),
        ({"user": {"client-certificate-data": "Zm9v", "client-key-data": "YmFy"}}, "client cert"),
        ({"user": {}}, "no ServiceAccount token"),
    ],
    ids=[
        "remote-ip",
        "remote-name",
        "plain-http",
        "insecure",
        "proxy",
        "context",
        "exec",
        "auth-provider",
        "client-cert",
        "no-token",
    ],
)
def test_anything_but_the_pinned_cluster_is_refused(
    tmp_path: Path, changes: dict[str, Any], reason: str
) -> None:
    folder = write_credentials(tmp_path / "creds", **changes)
    with pytest.raises(PreflightError, match=reason):
        check(folder)


def test_another_clusters_ca_is_refused(credentials: Path, tmp_path: Path) -> None:
    other = write_credentials(tmp_path / "other")  # a recreated cluster: a new CA
    (credentials / "ca.sha256").write_text((other / "ca.sha256").read_text())
    with pytest.raises(PreflightError, match="does not match the recorded fingerprint"):
        check(credentials)


def test_a_readable_kubeconfig_or_a_missing_one_is_refused(credentials: Path) -> None:
    path = credentials / KUBECONFIG_FILES["reader"]
    path.chmod(0o644)
    with pytest.raises(PreflightError, match="readable by other users"):
        check(credentials)
    path.unlink()
    with pytest.raises(PreflightError, match="the reader kubeconfig is missing"):
        check(credentials)
    with pytest.raises(PreflightError, match="not configured"):
        check_kubeconfig("reader", None, CONTEXT, "0" * 64)


def test_errors_name_the_identity_never_the_path(credentials: Path) -> None:
    (credentials / KUBECONFIG_FILES["writer"]).chmod(0o640)
    with pytest.raises(PreflightError) as refused:
        check(credentials, "writer")
    assert str(credentials) not in str(refused.value) and "writer" in str(refused.value)


@pytest.mark.parametrize(
    "written",
    ["{hex}", "{hex}\n", "{upper}", "{colons}", "sha256:{hex}", "{hex}  ca.crt"],
)
def test_the_recorded_fingerprint_is_read_in_its_usual_spellings(
    tmp_path: Path, written: str
) -> None:
    digest = hashlib.sha256(b"x").hexdigest()
    colons = ":".join(digest[i : i + 2] for i in range(0, 64, 2)).upper()
    path = tmp_path / "ca.sha256"
    path.write_text(written.format(hex=digest, upper=digest.upper(), colons=colons))
    assert read_fingerprint(path) == digest


@pytest.mark.parametrize("written", ["", "abc", "z" * 64, "sha1:" + "a" * 40])
def test_a_malformed_or_missing_fingerprint_stops_everything(tmp_path: Path, written: str) -> None:
    path = tmp_path / "ca.sha256"
    path.write_text(written)
    with pytest.raises(PreflightError):
        read_fingerprint(path)
    with pytest.raises(PreflightError, match="missing"):
        read_fingerprint(tmp_path / "nothing")


def test_both_fingerprint_conventions_of_one_certificate_are_accepted() -> None:
    pem = ca_certificate()
    der = x509.load_pem_x509_certificate(pem).fingerprint(hashes.SHA256()).hex()
    assert ca_fingerprints(pem) == {der, hashlib.sha256(pem).hexdigest()}


def _jwt(claims: dict[str, object]) -> str:
    """A token shaped like a ServiceAccount JWT, built at run time (no key shape in this file)."""

    def part(value: dict[str, object]) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join([part({"alg": "RS256"}), part(claims), "c2lnbmF0dXJl"])


def test_an_expired_token_is_refused_with_the_command_that_renews_it() -> None:
    from recap.environments.kubernetes.preflight import _check_expiry

    now = time.time()
    with pytest.raises(PreflightError, match=r"expired \d+ min ago: mint new ones"):
        _check_expiry("recap-reader", _jwt({"exp": int(now - 3600)}))
    with pytest.raises(PreflightError, match="expired"):  # about to expire counts too
        _check_expiry("recap-reader", _jwt({"exp": int(now + 10)}))
    _check_expiry("recap-reader", _jwt({"exp": int(now + 3600)}))  # valid: no error
    _check_expiry("recap-reader", "an-opaque-token")  # not a JWT: left to the API server
