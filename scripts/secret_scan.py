"""Scan the repository, and every blob in its git history, for secrets, without printing any.

    python scripts/secret_scan.py            # tracked files + every blob any commit ever had
    python scripts/secret_scan.py --tree     # tracked files only (fast)

Two checks:

  key shapes    the key and token formats the recorder redacts (recap/telemetry/redact.py:
                Anthropic, OpenAI, OpenRouter, Groq, Google, AWS, GitHub, Slack, bearer
                tokens, private-key blocks, JSON Web Tokens, kubeconfig key material) in
                every tracked text file and every text blob in the history (all branches,
                tags and, locally, the reflog);
  owner's keys  every secret value in the local key files (~/.config/recap/*.env: names
                containing KEY, SECRET, TOKEN or PASSWORD) must appear nowhere, binary blobs
                included. The values are read locally and never printed. In CI there are no
                key files, so only the shapes are checked.

A finding names the place and the kind, and identifies the value only by the first 12
characters of its SHA-256 (which reveal nothing about a key). Test files hold a few
deliberately fake keys, to test redaction; each is allowed here by the SHA-256 of its exact
value (KNOWN_FAKES), never by its path, so a real key added next to them is still found.
Exit code 1 when anything is found.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from recap.telemetry.redact import SECRET_ENV_NAME, SECRET_PATTERNS

ROOT = Path(__file__).resolve().parents[1]
KEY_FILES = Path.home() / ".config" / "recap"
MIN_SECRET_LENGTH = 16  # a key file's flags (=1, a date) are not secrets
MAX_TEXT_BYTES = 5_000_000  # larger blobs are only checked for the owner's exact values

# Kinds, in the order of recap/telemetry/redact.py SECRET_PATTERNS.
KINDS = (
    "private key",
    "Anthropic key",
    "OpenRouter key",
    "OpenAI project key",
    "OpenAI-style key",
    "Groq key",
    "Google key",
    "AWS access key",
    "GitHub fine-grained token",
    "GitHub token",
    "Slack token",
    "bearer token",
    "JSON Web Token",
    "kubeconfig key material",
)
assert len(KINDS) == len(SECRET_PATTERNS), "a redaction pattern has no name here"

# SHA-256 of the fake keys the tests use on purpose (tests/unit/test_redaction.py and
# friends). Reviewed by hand: each is an obviously fake string, never a real credential.
KNOWN_FAKES: frozenset[str] = frozenset(
    {
        # apps/dashboard/tests/auth.test.ts: two made-up "Bearer ..." header values
        "9680414819ed7483e72ee9be174054d6836d4e622e81f2795573baed72a04ff3",
        "4e10d030cca1ac142cb28738a564f3ef829e5277c19743e6a1e4853474433770",
        # tests/e2e/test_api_security.py: a wrong bearer token the API must refuse
        "8a6ad3560de2c4938508369708e902c037206f1cca12503b7bbfe02ab2b103b6",
        # tests/unit/test_redaction.py: a bearer header the redactor must replace
        "6ba6623e89df4308ed93166982e6f1c59a422b32b10477d2b78892ea2da2343b",
        # older versions of tests/integration/test_controller_paths.py and
        # tests/unit/test_prompts.py: "sk-ant-api03-" followed by the alphabet, and by
        # the words "this is a test secret"
        "c123349fa674f64900bcc98f093f656a5430b87b7586f8da37371a90ecfb3c48",
        "bdf7c4a66ae3245ae34346c3e858cd5df8b3dafad6b89d1ad5c715f00ebc02be",
    }
)


@dataclass(frozen=True)
class Finding:
    where: str  # a path, plus the blob id for history
    kind: str
    digest: str  # first 12 hex characters of the value's SHA-256

    def line(self) -> str:
        return f"{self.where}: {self.kind} [sha256:{self.digest}]"


def digest(value: str | bytes) -> str:
    data = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def shape_findings(text: str, where: str) -> Iterator[Finding]:
    """Key-shaped strings in one text, minus the known fakes."""
    seen: set[str] = set()
    for kind, pattern in zip(KINDS, SECRET_PATTERNS, strict=True):
        for match in pattern.finditer(text):
            full = digest(match.group(0))
            if full in KNOWN_FAKES or full in seen:
                continue
            seen.add(full)
            yield Finding(where, kind, full[:12])


def owner_values(folder: Path = KEY_FILES) -> list[bytes]:
    """The secret values in the local key files (NAME=value lines). Never printed."""
    values: list[bytes] = []
    for path in sorted(folder.glob("*.env")) if folder.is_dir() else []:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip().removeprefix("export ").strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            value = value.strip().strip("'\"")
            if SECRET_ENV_NAME.search(name) and len(value) >= MIN_SECRET_LENGTH:
                values.append(value.encode())
    return values


def scan_blob(data: bytes, where: str, secrets: list[bytes]) -> list[Finding]:
    found = [Finding(where, "owner's key value", digest(s)[:12]) for s in secrets if s in data]
    if len(data) <= MAX_TEXT_BYTES and b"\0" not in data[:8000]:
        found.extend(shape_findings(data.decode("utf-8", errors="replace"), where))
    return found


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def tracked_files() -> Iterator[tuple[str, bytes]]:
    for name in git("ls-files", "-z").decode().split("\0"):
        path = ROOT / name
        if name and path.is_file():
            yield name, path.read_bytes()


def history_blobs() -> Iterator[tuple[str, bytes]]:
    """Every blob reachable from any ref (and, locally, the reflog), with a path it had."""
    listing = git("rev-list", "--all", "--reflog", "--objects").decode(errors="replace")
    paths: dict[str, str] = {}
    for row in listing.splitlines():
        oid, _, path = row.partition(" ")
        if path and oid not in paths:
            paths[oid] = path
    checked = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        cwd=ROOT,
        input="\n".join(paths).encode(),
        check=True,
        capture_output=True,
    ).stdout.decode()
    blobs = [row.split()[0] for row in checked.splitlines() if row.endswith(" blob")]
    with subprocess.Popen(
        ["git", "cat-file", "--batch"], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE
    ) as proc:
        assert proc.stdin is not None and proc.stdout is not None
        for oid in blobs:
            proc.stdin.write(oid.encode() + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            size = int(header[2])
            data = proc.stdout.read(size)
            proc.stdout.read(1)  # the newline after each object
            yield f"{paths[oid]} (blob {oid[:10]})", data
        proc.stdin.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tree", action="store_true", help="tracked files only, no history")
    args = parser.parse_args(argv)
    secrets = owner_values()
    findings: list[Finding] = []

    files = 0
    for name, data in tracked_files():
        files += 1
        findings.extend(scan_blob(data, name, secrets))
    print(f"tracked files: {files} scanned")

    if not args.tree:
        blobs = 0
        for where, data in history_blobs():
            blobs += 1
            findings.extend(scan_blob(data, where, secrets))
        commits = len(git("rev-list", "--all").splitlines())
        print(f"history: {blobs} blobs from {commits} commits scanned")

    if secrets:
        print(f"owner's keys: {len(secrets)} value(s) from {KEY_FILES.name}/*.env checked")
    else:
        print("owner's keys: no local key files (key shapes only)")
    unique = sorted({f.line() for f in findings})
    for line in unique:
        print(f"FOUND {line}")
    print(f"{len(unique)} finding(s)" if unique else "no secrets found")
    if unique:
        print("find a blob's commits with: git log --all --find-object=<blob id>")
    return 1 if unique else 0


if __name__ == "__main__":
    sys.exit(main())
