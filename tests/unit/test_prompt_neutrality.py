"""No scenario's answer may hide in RECAP's fixed prompt text.

The system prompts, instructions, JSON schemas and tool definitions are the same for every
incident. If one of them contained a scenario's version, exception or log line, a model
could copy the answer instead of reading the evidence, and a correct diagnosis would prove
nothing. The review of the first human-approved M2 run found exactly that: prompts-v4.1
used "v2.4.1" and "v2.4.0" as examples, which are bad-deployment's bad release and its
rollback target.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from recap.core.config import Settings
from recap.core.models import FailureMechanism
from recap.providers import prompts, protocol
from tests.llm_fakes import investigation_request, proposal_request

SCENARIOS = Path(__file__).resolve().parents[2] / "fixtures" / "scenarios"
VERSION = re.compile(r"\bv\d+\.\d+\.\d+\b")


def strings_in(value: Any) -> list[str]:
    """Every string anywhere inside a YAML value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in strings_in(item)]
    if isinstance(value, list):
        return [text for item in value for text in strings_in(item)]
    return []


def scenario_facts(settings: Settings) -> set[str]:
    """What only a scenario knows: every version it mentions, and the longer free text of its
    faults, deployments, planted log lines and expected answer. Names from RECAP's trusted
    catalog (services, tools, mechanisms) are left out: the schemas list them on purpose."""
    catalog = (
        {mechanism.value for mechanism in FailureMechanism}
        | set(settings.policies.allowed_services)
        | set(settings.policies.tools)
    )
    facts: set[str] = set()
    for path in sorted(SCENARIOS.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        facts |= set(VERSION.findall(text))
        data = yaml.safe_load(text)
        for key in ("faults", "deployments", "log_lines", "expected"):
            facts |= {s for s in strings_in(data.get(key)) if len(s) >= 12 and s not in catalog}
    return facts


def fixed_texts(settings: Settings) -> dict[str, str]:
    """Everything a model is shown that does not come from the incident or its evidence."""
    request = investigation_request(settings)  # no evidence: the fixed text and the incident
    return {
        "v4 system prompt": protocol.SYSTEM_PROMPT,
        "v4 instructions": "\n".join(
            protocol.JSON_STEP_INSTRUCTIONS
            + protocol.TOOLS_STEP_INSTRUCTIONS
            + protocol.PROPOSAL_INSTRUCTIONS
        ),
        "v4 step prompt (json)": protocol.render_investigation(request, "json"),
        "v4 step prompt (tools)": protocol.render_investigation(request, "tools"),
        "v4 step schema": json.dumps(protocol.step_schema(request)),
        "v4 tool definitions": json.dumps(protocol.tool_definitions(request)),
        "v4 proposal schema": json.dumps(protocol.proposal_schema(proposal_request(settings, []))),
        "v3 system prompt": prompts.SYSTEM_PROMPT,
        "v3 instructions": "\n".join(
            prompts.INVESTIGATION_INSTRUCTIONS + prompts.PROPOSAL_INSTRUCTIONS
        ),
        "v3 step prompt": prompts.render_investigation_prompt(request),
        "v3 step schema": json.dumps(prompts.InvestigationOutput.model_json_schema()),
        "v3 proposal schema": json.dumps(prompts.ProposalOutput.model_json_schema()),
    }


def test_the_scenarios_hold_the_facts_this_guard_looks_for(settings: Settings) -> None:
    facts = scenario_facts(settings)
    assert {"v2.4.1", "v2.4.0"} <= facts  # bad-deployment's bad release and rollback target
    assert any("NullPointerException" in fact for fact in facts)  # a fault's exception text
    assert "execute_rollback" not in facts  # catalog names are not facts


def test_no_scenario_fact_appears_in_any_fixed_prompt_text(settings: Settings) -> None:
    facts = scenario_facts(settings)
    leaks = sorted(
        (name, fact)
        for name, text in fixed_texts(settings).items()
        for fact in facts
        if fact in text
    )
    assert leaks == []
