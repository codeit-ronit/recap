"""Routing signals: the observable facts the router uses to pick single vs parallel.

Where it sits:

    incident + memory hits + budget --> [compute_signals] --> RoutingSignals --> RulesRouter

Every signal is something a person can check in the dashboard: how many services the alert
names, which kinds of symptoms it lists, whether memory holds a verified fix for a very
similar incident (or similar incidents that disagree), how many fixes already failed, and
how much budget is left. Nothing here is learned or estimated by a model.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from recap.core.config import RoutingSettings
from recap.core.models import EpisodeOutcome, Incident, MemoryHit, RoutingSignals
from recap.harness.budget import BudgetTracker
from recap.memory.store import memory_conflict

# Words that put a symptom line into a category. Plain substrings, except that "down" and
# "oom" must start a word, so "downstream" or "room" do not count as availability/resource.
SYMPTOM_PATTERNS: dict[str, re.Pattern[str]] = {
    "errors": re.compile(r"5xx|error|failure", re.IGNORECASE),
    "latency": re.compile(r"latency|p95|slow", re.IGNORECASE),
    "availability": re.compile(r"readiness|unavailable|\bdown\b|crash", re.IGNORECASE),
    "resource": re.compile(r"memory|cpu|\boom|disk", re.IGNORECASE),
}


def symptom_categories(symptoms: Sequence[str]) -> list[str]:
    """Sorted, unique kinds of symptoms: "availability", "errors", "latency", "resource"."""
    return sorted(
        name
        for name, pattern in SYMPTOM_PATTERNS.items()
        if any(pattern.search(symptom) for symptom in symptoms)
    )


def compute_signals(
    *,
    incident: Incident,
    memory_hits: Sequence[MemoryHit],
    budget: BudgetTracker,
    routing: RoutingSettings,
    prior_failed_attempts: int = 0,
    escalation: bool = False,
) -> RoutingSignals:
    """Collect the routing signals for one investigation round."""
    similar = [hit for hit in memory_hits if hit.similarity >= routing.familiar_similarity]
    # A very similar past incident that ended FAILED counts like a fix that already failed.
    failed_before = sum(1 for hit in similar if hit.outcome == EpisodeOutcome.FAILED)
    return RoutingSignals(
        services_affected=len({incident.service, *incident.affected_services}),
        symptom_categories=symptom_categories(incident.symptoms),
        memory_hits=len(memory_hits),
        top_memory_similarity=max((hit.similarity for hit in memory_hits), default=0.0),
        familiar_success=any(_verified_fix_here(hit, incident) for hit in similar),
        memory_conflict=memory_conflict(memory_hits, routing.conflict_similarity),
        prior_failed_attempts=prior_failed_attempts + failed_before,
        escalation=escalation,
        remaining_tool_calls=budget.remaining_tool_calls(),
        remaining_steps=budget.remaining_steps(),
        max_parallel_agents=budget.budget.max_parallel_agents,
    )


def _verified_fix_here(hit: MemoryHit, incident: Incident) -> bool:
    """A past incident on the same service whose recovery the independent oracle verified."""
    return (
        hit.outcome == EpisodeOutcome.SUCCESS
        and hit.episode.verified
        and hit.episode.service == incident.service
    )
