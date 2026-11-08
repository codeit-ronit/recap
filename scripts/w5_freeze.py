"""The W5 freeze (M6 W5): what decides RECAP's behaviour in the held-out SREGym evaluation,
hashed and recorded in its manifest before the first scored run, and checked before each one.

    python scripts/w5_freeze.py hash     # the current hashes (written into the manifest)
    python scripts/w5_freeze.py check    # before every scored run: exit 1 unless both hashes
                                         # match the manifest and no tracked file is modified

Two hashes:
  files      every git-tracked file under the frozen roots, by path and content: RECAP's code
             (the prompts are code), its configuration (providers with the pinned model weights,
             policies, budgets, the sregym-hotel profile), the runbooks and their Kubernetes
             overlay, the scenario files, the SREGym integration and its operator script, the
             API (the approval path), and the run and batch scripts
  packages   the Python packages installed in this interpreter, by name and version (the
             project pins ranges, not versions)

A change to any of them before the first scored run is a new freeze: a new manifest hash in a
new commit. From the first scored run until the last grade is recorded, nothing may change.
"""

from __future__ import annotations

import argparse
import hashlib
import site
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "evals" / "frozen" / "w5-heldout-sregym.yaml"
FROZEN = (
    "recap",
    "configs",
    "fixtures/knowledge",
    "fixtures/knowledge-overlays/kubernetes",
    "fixtures/sregym-scenarios",
    "integrations/sregym",
    "infra/sregym",
    "apps/api",
    "scripts/demo.py",
    "scripts/live_eval.py",
    "evals/categorize.py",
    "evals/records.py",
    "pyproject.toml",
)


def _git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, check=True
    )
    return done.stdout


def frozen_files() -> list[str]:
    """Every git-tracked file under the frozen roots, sorted (untracked files never count)."""
    listed = _git("ls-files", "-z", "--", *FROZEN).split("\0")
    return sorted(path for path in listed if path)


def files_hash() -> str:
    digest = hashlib.sha256()
    for path in frozen_files():
        content = (REPO_ROOT / path).read_bytes()
        digest.update(f"{path}\0{hashlib.sha256(content).hexdigest()}\n".encode())
    return digest.hexdigest()


def packages_hash() -> str:
    """The interpreter's installed packages only: its site-packages, never the current folder
    or other sys.path entries (which would make the hash depend on where it is run from)."""
    names = sorted(
        {
            f"{(dist.metadata['Name'] or '').lower()}=={dist.version}"
            for dist in metadata.distributions(path=site.getsitepackages())
        }
    )
    return hashlib.sha256("\n".join(names).encode()).hexdigest()


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def problems(manifest: dict[str, Any]) -> list[str]:
    """What keeps the evaluation from being the frozen one ([] when it is)."""
    freeze = manifest.get("freeze", {})
    found = []
    if freeze.get("files_sha256") != files_hash():
        found.append("a frozen file changed since the manifest was written (files hash differs)")
    if freeze.get("packages_sha256") != packages_hash():
        found.append("the installed Python packages changed (packages hash differs)")
    if _git("status", "--porcelain", "--untracked-files=no").strip():
        found.append("tracked files have uncommitted changes")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("command", choices=["hash", "check"])
    args = parser.parse_args(argv)
    if args.command == "hash":
        print(f"files_sha256: {files_hash()}  ({len(frozen_files())} files)")
        print(f"packages_sha256: {packages_hash()}")
        return 0
    found = problems(load_manifest())
    head = _git("rev-parse", "HEAD").strip()
    if found:
        print(f"NOT FROZEN at {head[:12]}: " + "; ".join(found), file=sys.stderr)
        return 1
    print(f"frozen: HEAD {head}, {len(frozen_files())} files and the packages match the manifest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
