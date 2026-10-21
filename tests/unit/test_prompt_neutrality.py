"""No scenario's answer may hide in RECAP's fixed text.

Everything a model can be shown that does not come from the incident or its evidence is the
same for every incident: system prompts, instructions, each agent role's step prompt, the
proposal prompt, schemas, tool definitions, corrective feedback, and the runbooks retrieval
can return. If any of it contained a scenario's version, exception, class name or log
detail, a model could copy the answer instead of reading the evidence, and a correct
diagnosis would prove nothing. The review of the first human-approved M2 run found exactly
that: prompts-v4.1 used "v2.4.1" and "v2.4.0" as examples, which are bad-deployment's bad
release and its rollback target.

The guard checks both prompt versions (v4 and the paid adapter's v3). A self-test plants a
fact in each kind of source and expects the guard to catch it.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from recap.core.config import Settings
from recap.core.models import AgentRole, FailureMechanism
from recap.providers import prompts, protocol
from tests.llm_fakes import investigation_request, proposal_request

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "fixtures" / "scenarios"
KNOWLEDGE = ROOT / "fixtures" / "knowledge"
PROMPT_MODULES = [
    ROOT / "recap" / "providers" / name for name in ("protocol.py", "http_llm.py", "prompts.py")
]

VERSION = re.compile(r"(?<![\w.])v\d+\.\d+\.\d+(?![\w.])")
CAMEL_CASE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")  # PricingClient
DOTTED = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.[a-z_][A-Za-z0-9_]*\b")  # discount.legacy_mode
QUOTED = re.compile(r"['\"]([^'\"\s][^'\"]{3,})['\"]")  # 'fast-checkout'
# Platform and runtime terms that runbooks teach as SIGNS of a mechanism ("a CrashLoopBackOff
# event", "OutOfMemoryError in the logs"): they say what kind of failure it is, never which
# scenario it is or what to fix. Any other class name from a scenario counts as a fact.
GENERIC_TERMS = frozenset(
    {"OutOfMemoryError", "NullPointerException", "CrashLoopBackOff", "BackOff", "OOMKilled"}
)


def strings_in(value: Any) -> list[str]:
    """Every string anywhere inside a YAML value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in strings_in(item)]
    if isinstance(value, list):
        return [text for item in value for text in strings_in(item)]
    return []


def catalog(settings: Settings) -> set[str]:
    """Names from RECAP's trusted catalog: the schemas and prompts list them on purpose."""
    return (
        {m.value for m in FailureMechanism}
        | set(settings.policies.allowed_services)
        | set(settings.policies.allowed_namespaces)
        | set(settings.policies.tools)
    )


def scenario_facts(settings: Settings) -> set[str]:
    """What only a scenario knows: every version it mentions; the free text of its faults,
    deployments, planted log lines and expected answer; and the distinctive pieces of that
    text (class names, dotted identifiers, quoted names), so a partial copy is caught too."""
    names = catalog(settings)
    facts: set[str] = set()
    for path in sorted(SCENARIOS.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        facts |= set(VERSION.findall(text))
        data = yaml.safe_load(text)
        for key in ("faults", "deployments", "log_lines", "expected"):
            for s in strings_in(data.get(key)):
                if len(s) >= 12 and s not in names:
                    facts.add(s)
                facts |= {w for w in CAMEL_CASE.findall(s) if w not in GENERIC_TERMS}
                facts |= {w for w in DOTTED.findall(s) if len(w) >= 8}
                facts |= {q for q in QUOTED.findall(s) if q not in names}
    return facts


def answer_services() -> set[str]:
    """Services that are some scenario's answer (the service to fix, or a failing dependency)."""
    found: set[str] = set()
    for path in SCENARIOS.glob("*.yaml"):
        expected = yaml.safe_load(path.read_text(encoding="utf-8")).get("expected") or {}
        found |= {str(expected[k]) for k in ("root_cause_service", "dependency") if expected.get(k)}
    return found


def descriptions(schema: Any) -> list[str]:
    """The free-text parts of a JSON schema (enum lists name the catalog on purpose)."""
    if isinstance(schema, dict):
        own = [str(v) for k, v in schema.items() if k == "description"]
        return own + [d for k, v in schema.items() if k != "enum" for d in descriptions(v)]
    if isinstance(schema, list):
        return [d for item in schema for d in descriptions(item)]
    return []


def module_literals() -> str:
    """Every string literal in the prompt modules: instructions, feedback sent back to the
    model ("none of the tool calls can run ..."), and messages of any kind."""
    found = []
    for path in PROMPT_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found += [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
    return "\n".join(found)


def neutral_proposal_request(settings: Settings) -> Any:
    """A proposal request whose own data holds no scenario fact (only fixed text remains)."""
    base = proposal_request(settings, [])
    hypothesis = base.hypothesis.model_copy(update={"claim": "the diagnosed cause", "details": {}})
    report = base.report.model_copy(update={"best": hypothesis})
    return base.model_copy(update={"hypothesis": hypothesis, "report": report})


def fixed_texts(
    settings: Settings, knowledge: Path = KNOWLEDGE
) -> tuple[dict[str, str], dict[str, str]]:
    """(texts, prose): everything shown to a model that is not incident data, and the part of
    it that is prose (where a quoted service name can only be an example)."""
    texts: dict[str, str] = {}
    prose: dict[str, str] = {}
    for role in settings.agents:
        for step in (1, 6):  # the first step, and the last one (it adds its own lines)
            request = investigation_request(settings, role=role, step_number=step, max_steps=6)
            name = f"{role.value} step {step}"
            texts[f"v4 {name} (json)"] = protocol.render_investigation(request, "json")
            texts[f"v4 {name} (tools)"] = protocol.render_investigation(request, "tools")
            texts[f"v4 {name} schema"] = json.dumps(protocol.step_schema(request))
            texts[f"v4 {name} tools"] = json.dumps(protocol.tool_definitions(request))
            texts[f"v3 {name}"] = prompts.render_investigation_prompt(request)
            prose[f"v4 {name} schema"] = "\n".join(descriptions(protocol.step_schema(request)))
            prose[f"v4 {name} tools"] = "\n".join(descriptions(protocol.tool_definitions(request)))
    proposal = neutral_proposal_request(settings)
    texts["v4 proposal prompt"] = protocol.render_proposal(proposal)
    texts["v4 proposal schema"] = json.dumps(protocol.proposal_schema(proposal))
    texts["v3 proposal prompt"] = prompts.render_proposal_prompt(proposal)
    prose["v4 proposal schema"] = "\n".join(descriptions(protocol.proposal_schema(proposal)))
    v3_schemas = (("v3 step", prompts.InvestigationOutput), ("v3 proposal", prompts.ProposalOutput))
    for name, model in v3_schemas:
        schema = model.model_json_schema()
        texts[f"{name} schema"] = json.dumps(schema)
        prose[f"{name} schema"] = "\n".join(descriptions(schema))
    v4_instructions = [
        *protocol.JSON_STEP_INSTRUCTIONS,
        *protocol.TOOLS_STEP_INSTRUCTIONS,
        *protocol.PROPOSAL_INSTRUCTIONS,
        protocol.LAST_STEP_JSON,
        protocol.LAST_STEP_TOOLS,
    ]
    for name, text in (
        ("v4 system prompt", protocol.SYSTEM_PROMPT),
        ("v4 instructions", "\n".join(v4_instructions)),
        ("v3 system prompt", prompts.SYSTEM_PROMPT),
        (
            "v3 instructions",
            "\n".join(prompts.INVESTIGATION_INSTRUCTIONS + prompts.PROPOSAL_INSTRUCTIONS),
        ),
        ("prompt module literals", module_literals()),
    ):
        texts[name] = prose[name] = text
    for doc in sorted(knowledge.rglob("*.md")):
        texts[f"runbook {doc.name}"] = prose[f"runbook {doc.name}"] = doc.read_text("utf-8")
    return texts, prose


def leaks(settings: Settings, knowledge: Path = KNOWLEDGE) -> list[tuple[str, str]]:
    """(source, fact) for every scenario fact found in fixed text."""
    texts, prose = fixed_texts(settings, knowledge)
    facts = scenario_facts(settings)
    found = [(name, fact) for name, text in texts.items() for fact in facts if fact in text]
    for service in answer_services():
        quoted = re.compile(rf"['\"]{re.escape(service)}['\"]")  # "payment" as an example
        found += [(name, f'"{service}"') for name, text in prose.items() if quoted.search(text)]
    return sorted(set(found))


def test_the_guard_knows_the_facts_it_looks_for(settings: Settings) -> None:
    facts = scenario_facts(settings)
    assert {"v2.4.1", "v2.4.0"} <= facts  # bad-deployment's bad release and rollback target
    assert "PricingClient" in facts  # a piece of a fault's exception text
    assert "execute_rollback" not in facts and "OutOfMemoryError" not in facts
    assert "payment" in answer_services()
    assert "none of the tool calls can run" in module_literals()  # feedback text is scanned


def test_no_scenario_fact_appears_in_any_fixed_text(settings: Settings) -> None:
    assert leaks(settings) == []


def _with_tool_description(settings: Settings, tool: str, extra: str) -> Settings:
    tools = dict(settings.policies.tools)
    tools[tool] = tools[tool].model_copy(update={"description": tools[tool].description + extra})
    policies = settings.policies.model_copy(update={"tools": tools})
    return settings.model_copy(update={"policies": policies})


def _with_goal(settings: Settings, role: AgentRole, extra: str) -> Settings:
    agents = dict(settings.agents)
    agents[role] = agents[role].model_copy(update={"goal": agents[role].goal + extra})
    return settings.model_copy(update={"agents": agents})


@pytest.mark.parametrize(
    ("plant", "source"),
    [
        ("tool description", "v4 proposal prompt"),  # shown in every proposal prompt
        ("agent goal", "v4 telemetry_investigator step 1 (json)"),  # a specialist's own goal
        ("last-step line", "v4 investigator step 6 (json)"),  # only on the last step
        ("partial fact", "v4 system prompt"),  # a piece of a fault's text
        ("quoted service", "v4 instructions"),  # a service that is some scenario's answer
        ("runbook", "runbook planted.md"),  # a document retrieval can return
    ],
)
def test_a_fact_planted_in_any_kind_of_source_is_caught_there(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, plant: str, source: str
) -> None:
    knowledge = KNOWLEDGE
    if plant == "tool description":
        settings = _with_tool_description(settings, "execute_rollback", " Such as v2.4.0.")
    elif plant == "agent goal":
        settings = _with_goal(settings, AgentRole.TELEMETRY, " (e.g. v2.4.1)")
    elif plant == "last-step line":
        monkeypatch.setattr(protocol, "LAST_STEP_JSON", protocol.LAST_STEP_JSON + " Often v2.4.1.")
    elif plant == "partial fact":
        system = protocol.SYSTEM_PROMPT + "\ne.g. a fault in PricingClient"
        monkeypatch.setattr(protocol, "SYSTEM_PROMPT", system)
    elif plant == "quoted service":
        planted = [*protocol.PROPOSAL_INSTRUCTIONS, 'e.g. {"dependency": "payment"}']
        monkeypatch.setattr(protocol, "PROPOSAL_INSTRUCTIONS", planted)
    elif plant == "runbook":
        knowledge = tmp_path / "knowledge"
        knowledge.mkdir()
        (knowledge / "planted.md").write_text("Roll back to v2.4.0 when this happens.\n")
    assert source in {name for name, _ in leaks(settings, knowledge)}
