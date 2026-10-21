"""code_version(): which code a run used, as recorded in every run's versions.

Checked against real, temporary git repositories (skipped where git is not installed).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from recap.runtime import code_version

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True,
    )  # fmt: skip


@pytest.fixture(autouse=True)
def fresh_cache() -> Iterator[None]:
    code_version.cache_clear()  # it is computed once per folder and process
    yield
    code_version.cache_clear()


def committed_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "tracked.txt").write_text("one\n")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-q", "-m", "first")
    return path


def test_a_clean_checkout_is_its_commit(tmp_path: Path) -> None:
    assert re.fullmatch(r"[0-9a-f]{12}", code_version(committed_repo(tmp_path / "r")))


def test_a_changed_tracked_file_marks_it_dirty(tmp_path: Path) -> None:
    repo = committed_repo(tmp_path / "r")
    (repo / "tracked.txt").write_text("two\n")
    assert re.fullmatch(r"[0-9a-f]{12}\+dirty", code_version(repo))


def test_untracked_files_do_not_make_it_dirty(tmp_path: Path) -> None:
    repo = committed_repo(tmp_path / "r")
    (repo / "new.txt").write_text("not in git\n")
    assert re.fullmatch(r"[0-9a-f]{12}", code_version(repo))


def test_outside_a_checkout_it_is_unknown(tmp_path: Path) -> None:
    assert code_version(tmp_path) == "unknown"


def test_a_copy_inside_another_repository_is_unknown(tmp_path: Path) -> None:
    outer = committed_repo(tmp_path / "outer")
    vendored = outer / "vendored-recap"
    vendored.mkdir()
    assert code_version(vendored) == "unknown"  # not the outer repository's commit
