"""Leakage guards for the W4 evaluation (M6 W4), and what they do NOT catch.

* RECAP's own code imports nothing from `evals` and names no dataset file, and names no
  scenario id in a string constant (docstrings and comments aside). A rule keyed on a
  scenario's features, or an id built from pieces, would pass: that needs review;
* the runbooks (fixtures/knowledge/*.md) and the prompts carry no scenario id, no bad
  release and no rollback target of any scenario's fix. Advice worded around a scenario
  without those strings would pass;
* the W4 split keeps train, test and held-out apart, labels agree, and no SREGym problem
  is part of it (SREGym's held-out problems are W5's, never tuned on here);
* scored runs read the TRAIN partition only and write nothing (also checked end to end in
  tests/integration/test_eval_runner.py).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

from evals.datasets import load_labels, scenario_files
from evals.runner import EVALS_DIR, EvalConfig, _scored_config, load_split
from recap.core.config import REPO_ROOT
from recap.core.models import MemoryPartition
from recap.memory.store import MemoryAccess

RECAP = REPO_ROOT / "recap"
ANSWER_FILES = ("evals/datasets", "labels-v1", "retrieval-v1", "query-vectors", "expected_files")


def _code_strings(path: Path) -> list[str]:
    """Every string constant in a module except docstrings (comments are not in the AST)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_recap_never_reads_an_evaluation_dataset_or_answer_file() -> None:
    for path in RECAP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(name in text for name in ANSWER_FILES), path.relative_to(REPO_ROOT)
        assert "from evals" not in text and "import evals" not in text, path.relative_to(REPO_ROOT)


def test_no_scenario_has_a_rule_of_its_own_in_recap_code() -> None:
    ids = list(scenario_files())
    pattern = re.compile(r"(?<![\w-])(" + "|".join(map(re.escape, ids)) + r")(?![\w-])")
    for path in RECAP.rglob("*.py"):
        found = [s for s in _code_strings(path) if pattern.search(s)]
        assert not found, f"{path.relative_to(REPO_ROOT)} names a scenario: {found[:3]}"


def test_runbooks_and_prompts_carry_no_scenario_answer() -> None:
    scenarios = scenario_files()
    bad_releases = {
        str(f["version"])
        for data in scenarios.values()
        for f in data.get("faults", [])
        if f.get("type") == "bad_release"
    }
    # Each rollback's target (the version a scenario's fix restores) is an answer too.
    bad_releases |= {
        label.fix.split("to_version=")[1].rstrip(")")
        for label in load_labels().scenarios.values()
        if "to_version=" in label.fix
    }
    texts = {
        p: p.read_text(encoding="utf-8")
        for p in (REPO_ROOT / "fixtures" / "knowledge").glob("*.md")
    }
    texts[RECAP / "providers" / "prompts.py"] = (RECAP / "providers" / "prompts.py").read_text(
        "utf-8"
    )
    for path, text in texts.items():
        assert not [sid for sid in scenarios if sid in text], path.name
        assert not [
            v for v in bad_releases if re.search(rf"(?<![\w.]){re.escape(v)}(?![\w.])", text)
        ], path.name


def test_the_w4_split_keeps_its_sets_apart_and_names_no_sregym_problem() -> None:
    split = load_split(EVALS_DIR / "splits" / "w4.yaml")
    scenarios = scenario_files()
    assert not set(split.train) & (set(split.test) | set(split.held_out))
    assert not set(split.test) & set(split.held_out)
    assert {scenarios[s]["partition"] for s in split.train} == {"train"}
    assert {scenarios[s]["partition"] for s in split.test + split.held_out + split.redteam} == {
        "test"
    }
    sregym = {
        yaml.safe_load(p.read_text(encoding="utf-8")).get("id")
        for p in (REPO_ROOT / "fixtures" / "sregym-scenarios").glob("*.yaml")
    }
    named = set(split.train + split.test + split.held_out + split.redteam)
    assert not named & sregym and not any("sregym" in s for s in named)
    # V1's six test scenarios are W4's development set unchanged: results stay comparable.
    assert split.test == load_split().test


def test_no_configuration_can_score_with_more_memory_than_train_or_write_any() -> None:
    for name in ("full_adaptive", "hybrid", "tight_tools", "small_context", "parallel_only"):
        path = next(EVALS_DIR.rglob(f"{name.replace('_', '-')}.yaml"))
        config = _scored_config(EvalConfig.model_validate(yaml.safe_load(path.read_text())))
        assert config.memory_read_partitions == [MemoryPartition.TRAIN]
        assert config.memory_write is False
    access = MemoryAccess.eval_test()
    assert access.readable_partitions == [MemoryPartition.TRAIN] and not access.write_enabled
