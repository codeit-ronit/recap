"""The frozen W5 manifest (evals/frozen/w5-heldout-sregym.yaml) says what will run, and the
repository agrees with it: the held-out problems and their blind scenario files, the
selection and its bookkeeping, the model, prompts, policy, budgets and configuration, and,
while the evaluation is frozen, the hash of every frozen file. Nothing RECAP reads names a
held-out problem."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from recap.core.config import REPO_ROOT, load_settings
from recap.environments.kubernetes.scenario import list_kind_scenarios
from recap.providers.protocol import PROMPT_VERSION
from scripts.w5_freeze import FROZEN, MANIFEST, files_hash, load_manifest

SCENARIOS = REPO_ROOT / "fixtures" / "sregym-scenarios"


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return load_manifest(MANIFEST)


def test_the_held_out_problems_are_the_blind_scenario_files_in_order(
    manifest: dict[str, Any],
) -> None:
    specs = {s.id: s for s in list_kind_scenarios(SCENARIOS)}
    problems = manifest["problems"]
    assert 3 <= len(problems) <= 5
    # Blind until the evaluation ends; then only the problems it attempted carry an answer (from
    # SREGym's own definitions), and those it never attempted stay blind.
    answered = {run["scenario"] for run in manifest.get("results", {}).get("runs", [])}
    if manifest["status"] != "completed":
        assert not answered
    for entry in problems:
        spec = specs[entry["scenario"]]
        assert spec.external is not None and spec.external.problem == entry["problem"]
        assert spec.partition.value == "test"
        if entry["scenario"] in answered:
            assert spec.family != "unlabelled" and spec.expected["root_cause_service"]
        else:
            assert (spec.family, spec.expected) == ("unlabelled", {})
        assert spec.external.commit == manifest["sregym"]["commit"]
    assert len({p["problem"] for p in problems}) == len(problems)


def test_the_selection_applies_its_rule_to_every_hotel_name(manifest: dict[str, Any]) -> None:
    selection = manifest["selection"]
    candidates = selection["candidates"]
    names = [c["problem"] for c in candidates]
    assert len(names) == len(set(names)) == selection["inventory"]["naming_the_hotel_app"] == 30
    assert all("hotel" in name for name in names)
    selected = [c["problem"] for c in candidates if c["decision"] == "selected"]
    # Every problem meeting the criteria, in the registry's own order, and nothing else.
    assert selected == [p["problem"] for p in manifest["problems"]]
    assert all(c["decision"] in ("selected", "excluded") and c["why"] for c in candidates)
    for used in selection["used_before"]:
        assert next(c for c in candidates if c["problem"] == used)["decision"] == "excluded"
    # The pilot is a development problem, in its own data folder, never a held-out one.
    pilot = manifest["pilot"]
    assert pilot["problem"] in selection["used_before"] and pilot["problem"] not in selected
    assert pilot["data_folder"] != manifest["memory"]["data_folder"]


def test_the_frozen_settings_are_the_repositorys(manifest: dict[str, Any]) -> None:
    settings = load_settings(target="sregym-hotel")
    vm = settings.providers.providers[manifest["model"]["provider"]]
    assert (vm.model, vm.weights) == (manifest["model"]["model"], manifest["model"]["weights"])
    assert (vm.temperature, vm.seed, vm.think, vm.mode) == (0.0, 7, False, "tools")
    assert vm.context_tokens == manifest["model"]["context_tokens"]
    assert manifest["prompts"] == PROMPT_VERSION
    assert settings.policies.version == manifest["safety"]["policy"]
    assert settings.budget.approvals.ttl_seconds == manifest["safety"]["approval_ttl_seconds"]
    budget = settings.budget.default_budget
    for key in ("max_steps", "max_tool_calls", "max_elapsed_seconds", "max_estimated_tokens"):
        assert getattr(budget, key) == manifest["budgets"][key]
    assert settings.budget.context.pack_tokens == manifest["budgets"]["pack_tokens"]
    assert (settings.retrieval.mode, settings.retrieval.k) == ("bm25", manifest["rag"]["k"])
    if manifest["status"] in ("frozen", "running"):
        assert settings.config_hash == manifest["config_hash"]


def test_while_frozen_no_frozen_file_has_changed(manifest: dict[str, Any]) -> None:
    """A change under the frozen roots during the evaluation is a new freeze, never silent.
    (After the evaluation, status `completed` keeps the hashes as its record.)"""
    assert list(manifest["freeze"]["roots"]) == list(FROZEN)
    if manifest["status"] not in ("frozen", "running"):
        pytest.skip("the evaluation is over: the hashes are its record")
    assert manifest["freeze"]["files_sha256"] == files_hash()


def test_nothing_recap_reads_names_a_held_out_problem(manifest: dict[str, Any]) -> None:
    problems = [p["problem"] for p in manifest["problems"]]
    pattern = re.compile("|".join(map(re.escape, problems)))
    roots = [
        "recap",
        "configs",
        "fixtures/knowledge",
        "fixtures/knowledge-overlays",
        "integrations",
        "apps/api",
        "infra",
    ]
    for root in roots:
        for path in (REPO_ROOT / root).rglob("*"):
            if path.is_file() and path.suffix in (".py", ".md", ".yaml", ".yml", ".json", ".sh"):
                assert not pattern.search(path.read_text(encoding="utf-8", errors="ignore")), path
    # Only the blind scenario files (evaluation-only fields) name them.
    named = {p.name for p in SCENARIOS.glob("*.yaml") if pattern.search(p.read_text())}
    assert named == {f"{entry['scenario']}.yaml" for entry in manifest["problems"]}
    assert Path(MANIFEST).is_relative_to(REPO_ROOT / "evals")
