"""Who may change things through the API: the local operator, proven by a bearer token.

Where it sits (audit finding 2.4): V1's API approved actions and started runs for anyone
who could reach it, and took the approver's "identity" from a free-text body field. Now:

  * Every state-changing endpoint (start a run, decide an approval, cancel a run, start an
    evaluation) needs `Authorization: Bearer <operator token>`, compared in constant time.
    Reads stay open on the trusted hosts, because the dashboard's live event stream
    (EventSource) cannot send headers. Run data is simulated and holds no secrets.
  * The approver recorded for a decision is `operator:<name>` from server configuration,
    never something a client typed.
  * Host header check (anti DNS rebinding): only requests addressed to this machine's
    loopback names are served; anything else gets 400.
  * Origin check (anti CSRF) for unsafe methods: a browser request from a page on another
    host gets 403. (The token is not a cookie, so a foreign page cannot send it anyway;
    this is defense in depth.)

Configuration, all through environment variables (nothing secret is ever committed):

    RECAP_OPERATOR_TOKEN   the token; if unset, one is generated on first start, saved to
                           <data dir>/operator-token (readable by your user only) and printed
                           once, like a Jupyter token
    RECAP_OPERATOR_NAME    the operator's display name (default "local-operator")
    RECAP_ALLOWED_HOSTS    comma-separated host names (default: localhost,127.0.0.1,[::1])

This is a single-operator scheme for a local prototype. A shared deployment needs real
user accounts (SSO/OIDC), per-user roles and TLS; see docs/threat-model.md.
"""

from __future__ import annotations

import hmac
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

TOKEN_FILE = "operator-token"
DEFAULT_OPERATOR = "local-operator"
DEFAULT_HOSTS = ("localhost", "127.0.0.1", "[::1]")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class Operator:
    """The authenticated caller of a state-changing request."""

    name: str

    @property
    def actor(self) -> str:
        """How decisions by this operator are recorded in the approval log."""
        return f"operator:{self.name}"


class OperatorAuth:
    """Checks the operator token of a request (one token, one operator, local use)."""

    def __init__(self, token: str, name: str = DEFAULT_OPERATOR) -> None:
        if len(token) < 16:
            raise ValueError("the operator token must be at least 16 characters")
        self._token = token.encode("utf-8")
        self.operator = Operator(name)

    @classmethod
    def from_environment(cls, data_dir: Path) -> OperatorAuth:
        """The token from RECAP_OPERATOR_TOKEN, else the saved (or a new) one in `data_dir`."""
        name = os.environ.get("RECAP_OPERATOR_NAME", DEFAULT_OPERATOR).strip() or DEFAULT_OPERATOR
        token = os.environ.get("RECAP_OPERATOR_TOKEN", "").strip()
        return cls(token or _saved_or_new_token(data_dir), name)

    def check(self, request: Request) -> Operator:
        """The operator, if the request carries the right bearer token; else 401."""
        scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
        given = credentials.strip().encode("utf-8")
        # compare_digest takes the same time whatever the input, so timing leaks nothing.
        if scheme.lower() != "bearer" or not hmac.compare_digest(given, self._token):
            raise HTTPException(
                status_code=401,
                detail="sign in with the operator token printed by the API server",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return self.operator


def allowed_hosts_from_environment() -> list[str]:
    """Host names this API answers to (RECAP_ALLOWED_HOSTS, else the loopback names)."""
    configured = os.environ.get("RECAP_ALLOWED_HOSTS", "")
    hosts = [host.strip() for host in configured.split(",") if host.strip()]
    return hosts or list(DEFAULT_HOSTS)


def origin_allowed(origin: str | None, allowed_hosts: list[str]) -> bool:
    """True for no Origin header (not a browser) or an Origin on one of the allowed hosts."""
    if origin is None:
        return True
    parts = urlsplit(origin)
    host = parts.hostname or ""
    if ":" in host:  # urlsplit drops the brackets of an IPv6 literal
        host = f"[{host}]"
    return parts.scheme in {"http", "https"} and host in allowed_hosts


def _saved_or_new_token(data_dir: Path) -> str:
    """Read <data_dir>/operator-token, or create it (mode 0600) and print it once."""
    path = data_dir / TOKEN_FILE
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    # Created readable by this user only, before the secret is written into it.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    print(
        f"RECAP operator token (needed to start runs and approve actions): {token}\n"
        f"  saved in {path}; paste it into the dashboard's sign-in box.",
        file=sys.stderr,
    )
    return token
