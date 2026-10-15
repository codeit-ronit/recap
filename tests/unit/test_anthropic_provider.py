"""Tests for the optional Claude adapter.

No network and no API key: a fake client stands in for anthropic.AsyncAnthropic. It
records the keyword arguments of every beta.messages.parse call and returns a canned
response with stop_reason, parsed_output and usage, like the real SDK does.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from recap.context.tokens import estimate_tokens
from recap.core.config import ProviderSettings, Settings
from recap.core.errors import RecapError
from recap.core.models import (
    ActionRecord,
    AgentRole,
    ContextItem,
    ContextPack,
    Evidence,
    FailureMechanism,
    Hypothesis,
    HypothesisStatus,
    Incident,
    InvestigationDecision,
    InvestigationRequest,
    MitigationDraft,
    ProposalDecision,
    ProposalRequest,
    RunBudget,
    SourceType,
    ToolCall,
    ToolCallRecord,
    ToolRisk,
    ToolSpecView,
    VerificationReport,
)
from recap.providers.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from recap.providers.prompts import (
    SYSTEM_PROMPT,
    HypothesisOutput,
    InvestigationOutput,
    ProposalOutput,
    ToolCallOutput,
    render_investigation_prompt,
)

T0 = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)


# --- fake SDK client --------------------------------------------------------------------


class FakeMessages:
    """Stands in for client.beta.messages: records calls, returns a response or raises."""

    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.messages = FakeMessages(response, error)
        self.beta = SimpleNamespace(messages=self.messages)


def fake_response(
    parsed: object,
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
    category: str | None = None,
) -> SimpleNamespace:
    """The fields of a ParsedBetaMessage that the adapter reads."""
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category=category) if stop_reason == "refusal" else None,
        model=model,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
    )


# --- builders ---------------------------------------------------------------------------


def _evidence(evidence_id: str) -> Evidence:
    return Evidence(
        id=evidence_id,
        incident_id="inc-1",
        source_type=SourceType.TOOL,
        source_ref="mcp:get_service_logs(service=checkout)",
        timestamp=T0,
        content="ERROR NullPointerException in PricingClient (version=v2.4.1)",
        tool_name="get_service_logs",
    )


def _tool(name: str, risk: ToolRisk = ToolRisk.READ) -> ToolSpecView:
    return ToolSpecView(name=name, description=name, risk=risk, args={})


def _incident() -> Incident:
    return Incident(
        incident_id="inc-1",
        scenario_id="bad-deployment",
        service="checkout",
        symptoms=["checkout: HTTP 5xx ratio above 30%"],
        created_at=T0,
        environment="simulated",
        budget=RunBudget(),
    )


def _context() -> ContextPack:
    items = [
        ContextItem(
            item_id=item_id,
            kind=kind,
            source_ref=item_id,
            text=f"{kind} text",
            token_estimate=3,
            score=1.0,
        )
        for item_id, kind in [("ev-1", "evidence"), ("runbook#1", "doc"), ("ep-1", "memory")]
    ]
    return ContextPack(items=items, budget_tokens=3000, tokens_before=9, tokens_after=9)


@pytest.fixture
def provider_settings(settings: Settings) -> ProviderSettings:
    """The real configs/providers.yaml entry for the Claude adapter."""
    return settings.providers.providers["anthropic"]


@pytest.fixture
def investigation() -> InvestigationRequest:
    return InvestigationRequest(
        role=AgentRole.INVESTIGATOR,
        goal="Investigate the incident.",
        incident=_incident(),
        context=_context(),
        evidence={"ev-1": _evidence("ev-1")},
        tools=[_tool("get_service_logs"), _tool("get_service_health")],
        # ev-0 came from an earlier call; it is real but was not packed this step.
        history=[ToolCallRecord(tool_name="list_services", ok=True, evidence_id="ev-0")],
        step=1,
        max_steps=8,
    )


@pytest.fixture
def proposal() -> ProposalRequest:
    best = Hypothesis(
        claim="checkout v2.4.1 is a bad release",
        mechanism=FailureMechanism.BAD_DEPLOYMENT,
        target_service="checkout",
        supporting_evidence_ids=["ev-1"],
        status=HypothesisStatus.SUPPORTED,
    )
    return ProposalRequest(
        incident=_incident(),
        hypothesis=best,
        report=VerificationReport(status="supported", best=best),
        context=_context(),
        evidence={"ev-1": _evidence("ev-1")},
        allowed_actions=[
            _tool("execute_rollback", ToolRisk.REVERSIBLE_WRITE),
            _tool("restart_service", ToolRisk.REVERSIBLE_WRITE),
        ],
        failed_actions=[
            ActionRecord(action="restart_service", target="checkout", result="not_recovered")
        ],
    )


def _finish(**overrides: object) -> InvestigationOutput:
    fields: dict = {
        "action": "finish",
        "tool_calls": [],
        "hypotheses": [],
        "summary": "Errors started right after the v2.4.1 release.",
        "next_steps": [],
    }
    return InvestigationOutput(**{**fields, **overrides})


def _call(tool_name: str, args_json: str) -> ToolCallOutput:
    return ToolCallOutput(tool_name=tool_name, args_json=args_json, reason="check it")


def _rollback(**overrides: object) -> ProposalOutput:
    fields: dict = {
        "propose": True,
        "action": "execute_rollback",
        "target": "checkout",
        "params_json": '{"to_version": "v2.4.0"}',
        "rationale": "The errors began with v2.4.1; v2.4.0 was healthy.",
        "evidence_ids": ["ev-1"],
        "citations": ["runbook#1"],
        "expected_health_change": "checkout error rate back under 5%",
        "rollback_hint": "redeploy v2.4.1",
    }
    return ProposalOutput(**{**fields, **overrides})


async def _decide(
    settings: ProviderSettings, request: InvestigationRequest, response: object
) -> tuple[InvestigationDecision, list[dict]]:
    """One investigation decision against a fake client; also returns the recorded calls."""
    client = FakeClient(response)
    decision = await AnthropicProvider(settings, client=client).next_action(request)
    return decision, client.messages.calls


async def _propose(
    settings: ProviderSettings, request: ProposalRequest, response: object
) -> tuple[ProposalDecision, list[dict]]:
    """One proposal against a fake client; also returns the recorded calls."""
    client = FakeClient(response)
    decision = await AnthropicProvider(settings, client=client).propose_mitigation(request)
    return decision, client.messages.calls


# --- the request sent to Claude ---------------------------------------------------------


async def test_request_follows_the_claude_api_contract(
    provider_settings: ProviderSettings, investigation: InvestigationRequest
) -> None:
    _, calls = await _decide(provider_settings, investigation, fake_response(_finish()))
    [kwargs] = calls
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["max_tokens"] == 16000
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["betas"] == [FALLBACK_BETA] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"
    assert kwargs["output_format"] is InvestigationOutput
    assert kwargs["system"] == SYSTEM_PROMPT
    assert kwargs["messages"] == [
        {"role": "user", "content": render_investigation_prompt(investigation)}
    ]
    # Thinking is always on for this model (disabling it is a 400); no sampling params.
    assert "thinking" not in kwargs
    assert "temperature" not in kwargs


def test_info_says_it_is_an_llm(provider_settings: ProviderSettings) -> None:
    info = AnthropicProvider(provider_settings, client=FakeClient()).info
    assert info.kind == "llm"
    assert info.name == "anthropic:claude-opus-5-5"
    assert info.model == "claude-opus-5-5"
    assert "your API key and billing" in info.description


def test_defaults_apply_when_the_config_is_minimal() -> None:
    provider = AnthropicProvider(ProviderSettings(kind="llm"), client=FakeClient())
    assert (provider.model, provider.max_tokens, provider.effort) == (
        "claude-opus-5-5",
        16000,
        "high",
    )


async def test_sdk_client_is_created_only_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
    provider_settings: ProviderSettings,
    investigation: InvestigationRequest,
) -> None:
    fake = FakeClient(fake_response(_finish()))
    created: list[FakeClient] = []

    def async_anthropic() -> FakeClient:
        created.append(fake)
        return fake

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(AsyncAnthropic=async_anthropic))
    provider = AnthropicProvider(provider_settings)
    assert created == []  # building the provider needs no client and no key
    await provider.next_action(investigation)
    await provider.next_action(investigation)
    assert created == [fake]  # one client, reused


# --- investigation replies --------------------------------------------------------------


async def test_tool_calls_parse_args_json_and_drop_invalid_calls(
    provider_settings: ProviderSettings, investigation: InvestigationRequest
) -> None:
    output = _finish(
        action="call_tools",
        tool_calls=[
            _call("get_service_logs", '{"service": "checkout", "limit": 20}'),
            _call("delete_namespace", '{"namespace": "shop"}'),  # not in this role's tools
            _call("get_service_health", "service=checkout"),  # not JSON
            _call("get_service_health", '["checkout"]'),  # JSON, but not an object
        ],
    )
    decision, _ = await _decide(provider_settings, investigation, fake_response(output))
    assert decision.action == "call_tools"
    assert decision.tool_calls == [
        ToolCall(
            tool_name="get_service_logs",
            args={"service": "checkout", "limit": 20},
            reason="check it",
        )
    ]
    assert "unlisted tool 'delete_namespace'" in decision.summary
    assert decision.summary.count("args_json is not a JSON object") == 2


async def test_no_valid_tool_call_left_means_finish(
    provider_settings: ProviderSettings, investigation: InvestigationRequest
) -> None:
    output = _finish(action="call_tools", tool_calls=[_call("delete_namespace", "{}")])
    decision, _ = await _decide(provider_settings, investigation, fake_response(output))
    assert decision.action == "finish"
    assert decision.tool_calls == []


async def test_made_up_evidence_ids_are_dropped(
    provider_settings: ProviderSettings, investigation: InvestigationRequest
) -> None:
    hypothesis = HypothesisOutput(
        claim="checkout v2.4.1 is a bad release",
        mechanism=FailureMechanism.BAD_DEPLOYMENT,
        target_service="checkout",
        supporting_evidence_ids=["ev-1", "ev-made-up-999", "ev-1", "ev-0"],
        details_json='{"version": "v2.4.1"}',
    )
    decision, _ = await _decide(
        provider_settings, investigation, fake_response(_finish(hypotheses=[hypothesis]))
    )
    [result] = decision.hypotheses
    # Packed evidence and the agent's own earlier results are real; the rest is dropped.
    assert result.supporting_evidence_ids == ["ev-1", "ev-0"]
    assert result.details == {"version": "v2.4.1"}
    assert result.mechanism == FailureMechanism.BAD_DEPLOYMENT
    assert result.target_service == "checkout"
    assert "ev-made-up-999" in decision.summary


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [("refusal", "refusal, category: cyber"), ("max_tokens", "max_tokens=16000")],
)
async def test_stop_reason_is_checked_before_the_output(
    stop_reason: str,
    expected: str,
    provider_settings: ProviderSettings,
    investigation: InvestigationRequest,
) -> None:
    # Even though JSON with a tool call came back, it must not be used.
    output = _finish(action="call_tools", tool_calls=[_call("get_service_logs", "{}")])
    response = fake_response(output, stop_reason=stop_reason, category="cyber")
    decision, _ = await _decide(provider_settings, investigation, response)
    assert decision.action == "finish"
    assert decision.tool_calls == [] and decision.hypotheses == []
    assert expected in decision.summary
    assert decision.usage.actual_input_tokens == 1000  # the tokens were still spent


async def test_reply_that_breaks_the_schema_finishes_with_a_note(
    provider_settings: ProviderSettings, investigation: InvestigationRequest
) -> None:
    with pytest.raises(ValidationError) as cut_off:
        InvestigationOutput.model_validate_json('{"action": "call_tools", "tool_ca')
    provider = AnthropicProvider(provider_settings, client=FakeClient(error=cut_off.value))
    decision = await provider.next_action(investigation)
    assert decision.action == "finish"
    assert "did not match the required JSON schema" in decision.summary
    # The call was billed, but the SDK raised before returning its token counts.
    assert "provider usage not reported for this call" in decision.summary
    assert decision.usage.is_estimate is True
    assert decision.usage.actual_input_tokens is None


async def test_proposal_that_breaks_the_schema_says_its_usage_is_unknown(
    provider_settings: ProviderSettings, proposal: ProposalRequest
) -> None:
    # Regression: this paid call was reported with $0 estimated usage, like the scripted policy.
    with pytest.raises(ValidationError) as cut_off:
        ProposalOutput.model_validate_json('{"propose": true, "act')
    provider = AnthropicProvider(provider_settings, client=FakeClient(error=cut_off.value))
    decision = await provider.propose_mitigation(proposal)
    assert decision.draft is None
    assert decision.notes == [
        "Claude's reply did not match the required JSON schema (it may be cut off)",
        "provider usage not reported for this call",
    ]
    usage = decision.usage
    assert (usage.steps, usage.is_estimate, usage.actual_output_tokens) == (1, True, None)
    assert usage.estimated_tokens > 0  # the prompt RECAP sent, estimated locally


async def test_answer_from_a_fallback_model_is_noted(
    provider_settings: ProviderSettings, investigation: InvestigationRequest
) -> None:
    response = fake_response(_finish(), model="claude-opus-5")
    decision, _ = await _decide(provider_settings, investigation, response)
    assert "answered by claude-opus-5" in decision.summary


# --- usage and cost ---------------------------------------------------------------------


async def test_usage_has_real_tokens_and_cost_from_the_price_table(
    provider_settings: ProviderSettings, investigation: InvestigationRequest
) -> None:
    decision, _ = await _decide(provider_settings, investigation, fake_response(_finish()))
    usage = decision.usage
    assert (usage.actual_input_tokens, usage.actual_output_tokens) == (1000, 200)
    # configs/providers.yaml: $4 input and $20 output per million tokens.
    assert usage.external_cost_usd == pytest.approx(1000 * 4 / 1e6 + 200 * 20 / 1e6)
    assert usage.is_estimate is False
    assert usage.steps == 1
    expected = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(
        render_investigation_prompt(investigation)
    )
    assert usage.estimated_tokens == expected


async def test_cost_is_zero_without_a_price_table(investigation: InvestigationRequest) -> None:
    settings = ProviderSettings(kind="llm", model="claude-opus-5-5")
    decision, _ = await _decide(settings, investigation, fake_response(_finish()))
    assert decision.usage.external_cost_usd == 0.0
    assert decision.usage.actual_output_tokens == 200


# --- proposal replies -------------------------------------------------------------------


async def test_proposal_becomes_an_llm_mitigation_draft(
    provider_settings: ProviderSettings, proposal: ProposalRequest
) -> None:
    output = _rollback(evidence_ids=["ev-1", "ev-bogus"], citations=["runbook#1", "doc-bogus"])
    decision, calls = await _propose(provider_settings, proposal, fake_response(output))
    assert calls[0]["output_format"] is ProposalOutput
    assert decision.draft == MitigationDraft(
        action="execute_rollback",
        target="checkout",
        params={"to_version": "v2.4.0"},
        rationale="The errors began with v2.4.1; v2.4.0 was healthy.",
        evidence_ids=["ev-1"],
        expected_health_change="checkout error rate back under 5%",
        rollback_hint="redeploy v2.4.1",
        risk_level=ToolRisk.REVERSIBLE_WRITE,  # from the trusted tool list
        hypothesis_claim="checkout v2.4.1 is a bad release",
        source="llm",
        citations=["runbook#1"],
    )
    assert decision.notes == [
        "dropped unknown IDs ['ev-bogus']",
        "dropped unknown IDs ['doc-bogus']",
    ]
    assert decision.usage.is_estimate is False


@pytest.mark.parametrize(
    ("overrides", "expected_note"),
    [
        ({"propose": False, "rationale": "evidence is unclear"}, "proposed no action"),
        ({"action": "delete_namespace", "target": "shop"}, "outside the remediation catalog"),
        ({"action": "restart_service"}, "already failed in this run"),
        ({"params_json": "to_version=v2.4.0"}, "params_json is not a JSON object"),
    ],
)
async def test_unusable_proposals_give_no_draft(
    overrides: dict,
    expected_note: str,
    provider_settings: ProviderSettings,
    proposal: ProposalRequest,
) -> None:
    decision, _ = await _propose(provider_settings, proposal, fake_response(_rollback(**overrides)))
    assert decision.draft is None
    assert any(expected_note in note for note in decision.notes)


async def test_refused_proposal_gives_no_draft(
    provider_settings: ProviderSettings, proposal: ProposalRequest
) -> None:
    response = fake_response(_rollback(), stop_reason="refusal")
    decision, _ = await _propose(provider_settings, proposal, response)
    assert decision.draft is None
    assert decision.notes == [
        "Claude declined the request (refusal, category: unspecified); nothing was decided"
    ]


# --- errors -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("rate_limit", "HTTP 429"), ("server", "HTTP 500"), ("network", "could not reach")],
)
async def test_sdk_errors_become_recap_errors(
    kind: str,
    expected: str,
    provider_settings: ProviderSettings,
    investigation: InvestigationRequest,
) -> None:
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    errors = {
        "rate_limit": anthropic.RateLimitError(
            "rate limited", response=httpx2.Response(429, request=request), body=None
        ),
        "server": anthropic.InternalServerError(
            "overloaded", response=httpx2.Response(500, request=request), body=None
        ),
        "network": anthropic.APIConnectionError(request=request),
    }
    provider = AnthropicProvider(provider_settings, client=FakeClient(error=errors[kind]))
    with pytest.raises(RecapError, match=expected) as raised:
        await provider.next_action(investigation)
    assert raised.value.__cause__ is errors[kind]  # the original SDK error is kept


async def test_other_errors_are_not_disguised(
    provider_settings: ProviderSettings, investigation: InvestigationRequest
) -> None:
    provider = AnthropicProvider(provider_settings, client=FakeClient(error=KeyError("bug")))
    with pytest.raises(KeyError):
        await provider.next_action(investigation)
