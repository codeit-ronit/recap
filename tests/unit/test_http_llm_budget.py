"""The run's model-token budget at the provider (recap/providers/http_llm.py, review of M3).

Every way a request can end settles its reservation, including a cancellation (an agent's
time share or the run's deadline running out while the request is out), and a refused
reservation never hides a request the server had already answered.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import pytest

from recap.core.config import Settings
from recap.core.errors import BudgetExhausted, model_calls_of
from recap.core.models import RunBudget
from recap.harness.budget import BudgetTracker
from recap.providers.factory import create_provider
from recap.providers.http_llm import HttpLLMProvider
from recap.providers.quota import OPT_IN_ENV
from tests.llm_fakes import FAKE_GROQ_KEY, FakeGroq, FakeOllama, error, investigation_request

LIMIT = 50_000  # far above one request's reservation (a prompt of a few thousand tokens)


class Reservations(BudgetTracker):
    """The run's budget tracker, remembering what each request reserved."""

    def __init__(self, budget: RunBudget) -> None:
        super().__init__(budget)
        self.reserved: list[int] = []

    def reserve_model_tokens(self, tokens: int) -> int:
        self.reserved.append(tokens)
        return super().reserve_model_tokens(tokens)


class Slow:
    """A model server that takes `seconds` to answer a chat request (cancellable, like a
    slow model)."""

    def __init__(self, fake: FakeOllama | FakeGroq, seconds: float) -> None:
        self.fake, self.seconds = fake, seconds

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/api/chat", "/chat/completions")):
            await asyncio.sleep(self.seconds)
        return self.fake.handle(request)


@pytest.fixture
def groq_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(OPT_IN_ENV, "1")
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("RECAP_GROQ_FREE_PLAN_CONFIRMED_ON", date.today().isoformat())
    yield


def provider(settings: Settings, name: str, handler: Any, budget: BudgetTracker) -> HttpLLMProvider:
    built = create_provider(name, settings, mock_http=httpx.MockTransport(handler))
    assert isinstance(built, HttpLLMProvider)
    built.bind_token_budget(budget)
    return built


async def test_a_request_cut_off_in_flight_gives_back_its_reservation(settings: Settings) -> None:
    """The time share cancels a slow agent mid-request and the run goes on. Nothing is in
    flight any more, so the reservation must go; the server may already have read the
    prompt, so the prompt's estimate counts as spent."""
    budget = Reservations(RunBudget(max_model_tokens=LIMIT))
    slow = provider(settings, "ollama", Slow(FakeOllama(), 5), budget)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await slow.next_action(investigation_request(settings))

    [reserved] = budget.reserved
    assert budget.model_reserved == 0  # before the fix: the whole reservation stayed behind
    assert budget.model_tokens == reserved - slow.settings.max_output_tokens  # the prompt


async def test_a_request_cut_off_while_waiting_to_retry_gives_back_its_reservation(
    settings: Settings, groq_env: None
) -> None:
    """The free plan's provider waits before it retries a 5xx; a cut during that wait is
    settled too (the 5xx itself generated nothing)."""
    fake = FakeGroq([error(503, "service_unavailable")])
    budget = Reservations(RunBudget(max_model_tokens=LIMIT))
    waiting = provider(settings, "groq", fake.handle, budget)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):  # the retry waits 2 s
            await waiting.next_action(investigation_request(settings))

    assert len(fake.chats) == 1
    assert budget.model_reserved == 0


async def test_a_refused_retry_keeps_the_answered_request_on_record(settings: Settings) -> None:
    """The first answer is unusable (prose, no tool call), so RECAP asks once more; that
    retry no longer fits the run's model tokens. The server answered the first request and
    reported its tokens: it must travel with the stop, so the trace and usage show it."""
    limit = 12_000  # room for the first request's reservation (a few thousand tokens)
    answered = limit - 1024  # what the server reports for it: too little room left for more
    fake = FakeOllama([{"content": "It is probably the release.", "prompt_tokens": answered}])
    budget = Reservations(RunBudget(max_model_tokens=limit))
    retried = provider(settings, "ollama", fake.handle, budget)

    with pytest.raises(BudgetExhausted) as refused:
        await retried.next_action(investigation_request(settings))

    assert refused.value.limit == "max_model_tokens" and len(fake.chats) == 1
    calls, spent = model_calls_of(refused.value)
    assert [(c.status, c.input_tokens) for c in calls] == [(200, answered)]
    assert spent is not None and spent.model_requests == 1
    assert spent.actual_input_tokens == answered
    assert budget.model_tokens == answered + fake.output_tokens and budget.model_reserved == 0
