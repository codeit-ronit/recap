"""A victim lead (M4.5) reaches the next round's real prompts.

A fake model (no reasoning) reads one service's logs in round 1 and claims resource pressure
on that service from the log text alone, so the rules cannot support the claim. The test
reads the round-2 prompts the model server received through the whole pipeline (HTTP,
protocol, harness, MCP gateway, simulator, verifier). When most of that service's ERROR
lines say its calls to a dependency fail, the dependency must be named in the verifier's
trusted section and in "Services to examine first"; when its errors are its own, no such
lead may appear. The services used here are not the ones of the M3 failures analyzed.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

from recap.core.config import Settings
from recap.core.models import RunConfig
from recap.providers.factory import create_provider
from recap.runtime import Runtime
from tests.llm_fakes import FakeOllama, OperatorApproves, hypothesis, tool_answer

pytestmark = pytest.mark.e2e

FINISH = "submit_findings"
QUESTIONS = "## Open questions from RECAP's verifier"
FOCUS = "## Services to examine first"


def need_more() -> dict[str, Any]:
    finish = {"kind": "need_more_evidence", "summary": "", "hypotheses": [], "next_checks": []}
    return tool_answer((FINISH, finish))


class TextOnlyClaimer:
    """Round 1: telemetry reads `target`'s logs, then claims resource pressure on it from that
    text alone; infrastructure needs more evidence. Round 2: both stop."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.round_two: list[str] = []

    def __call__(self, body: dict[str, Any]) -> Any:
        text = body["messages"][1]["content"]
        if FOCUS in text:  # only a later round names services to examine first
            self.round_two.append(text)
            return need_more()
        role = re.search(r"Role: (\S+)", text).group(1)  # type: ignore[union-attr]
        step = int(re.search(r"This is step (\d+)", text).group(1))  # type: ignore[union-attr]
        if role != "telemetry_investigator":
            return need_more()
        if step == 1:
            return tool_answer(("get_service_logs", {"service": self.target, "limit": 100}))
        ids = re.findall(r'id="(ev-[0-9a-f]{6}-[0-9a-z]+)"', text)
        claim = hypothesis(
            self.target, ids, mechanism="resource_pressure", version=None, claim="log text"
        )
        finish = {"kind": "hypothesis", "summary": "", "next_checks": [], "hypotheses": [claim]}
        return tool_answer((FINISH, finish))


def body_of(text: str, header: str) -> str:
    """The lines of one prompt section, without its header line ("" if it is absent)."""
    if header not in text:
        return ""
    rest = text.split(header, 1)[1].split("\n", 1)[1]
    return rest.split("\n## ", 1)[0]


async def round_two_prompts(settings: Settings, scenario: str, target: str) -> list[str]:
    runtime = Runtime(settings)
    model = TextOnlyClaimer(target)
    mock = httpx.MockTransport(FakeOllama(responder=model).handle)
    runtime.make_provider = lambda config: create_provider(  # type: ignore[method-assign]
        "ollama", runtime.settings, mock_http=mock, ledger=runtime.ledger
    )
    config = RunConfig(provider="ollama", use_memory=False, strategy="parallel")
    await runtime.run_incident(
        scenario, config, approval_channel=OperatorApproves(runtime.approvals)
    )
    return model.round_two


async def test_a_text_only_claim_on_a_victim_sends_round_two_to_its_dependency(
    settings: Settings,
) -> None:
    # RPC-style logs: most of cart's ERROR lines say its calls to inventory fail.
    prompts = await round_two_prompts(settings, "m3-inventory-leak-behind-frontend", "cart")
    assert len(prompts) == 2  # both specialists of round 2 were asked
    for text in prompts:
        assert QUESTIONS in text, "round 2 got no pointer from the verifier"
        assert "cart's calls to inventory fail" in body_of(text, QUESTIONS)
        assert body_of(text, FOCUS).strip() == "cart, inventory"


async def test_no_victim_lead_when_the_claimed_service_fails_on_its_own(
    settings: Settings,
) -> None:
    # cart's own leak: its errors are its own, so nothing points past it.
    prompts = await round_two_prompts(settings, "test-cart-memory-leak", "cart")
    assert len(prompts) == 2
    for text in prompts:
        assert "calls to" not in body_of(text, QUESTIONS)
        assert body_of(text, FOCUS).strip() == "cart"
