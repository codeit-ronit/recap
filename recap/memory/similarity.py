"""How alike is the current incident to a past episode? (deterministic, lexical)

Episodic memory needs a way to find "have we seen something like this before?".
V1 answers with plain word overlap, so the score is cheap, explainable and identical
on every machine:

    similarity = 0.7 * jaccard(symptom words) + 0.3 * service score

The service score is 1.0 when the past episode was alerted on the same service,
0.5 when its service is among the services affected now, and 0 otherwise.

A high score only means the incidents *look* alike. Whether the past fix is safe to
reuse is decided later, against live evidence (see `assess_reuse` in store.py).
Embedding similarity could replace this function without changing the store.
"""

from __future__ import annotations

import re

from pydantic import Field

from recap.core.models import Contract, ExperienceEpisode, Incident

SYMPTOM_WEIGHT = 0.7
SERVICE_WEIGHT = 0.3

_WORD_RE = re.compile(r"[a-z0-9]+")

# Common filler words. Threshold words ("above", "below", ...) are included because
# almost every alert uses them, so they say nothing about *what* is failing.
STOPWORDS = frozenset(
    "the and for with from into that this these those are was were has have had been "
    "not but its than then also above below over under".split()
)


def tokenize(text: str) -> set[str]:
    """Lowercase words of 3+ letters/digits, without stopwords or pure numbers."""
    return {
        word
        for word in _WORD_RE.findall(text.lower())
        if len(word) >= 3 and not word.isdigit() and word not in STOPWORDS
    }


def jaccard(a: set[str], b: set[str]) -> float:
    """Shared words divided by all words (0.0 when both sets are empty)."""
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


class MemoryQuery(Contract):
    """The parts of the current incident that memory compares against past episodes."""

    service: str  # primary alerted service
    affected_services: list[str] = Field(default_factory=list)
    symptoms: list[str]

    @classmethod
    def from_incident(cls, incident: Incident) -> MemoryQuery:
        """Build a query from the incident the controller is working on."""
        return cls(
            service=incident.service,
            affected_services=list(incident.affected_services),
            symptoms=list(incident.symptoms),
        )


def _service_score(query: MemoryQuery, episode_service: str) -> float:
    """1.0 for the same alerted service, 0.5 if it is affected now, else 0."""
    if episode_service == query.service:
        return 1.0
    if episode_service in query.affected_services:
        return 0.5
    return 0.0


def episode_similarity(query: MemoryQuery, episode: ExperienceEpisode) -> float:
    """Weighted symptom + service similarity in [0, 1], rounded to 3 decimals."""
    # All symptom lines are pooled into one word set, so line order never matters.
    symptoms = jaccard(tokenize(" ".join(query.symptoms)), tokenize(" ".join(episode.symptoms)))
    service = _service_score(query, episode.service)
    return round(SYMPTOM_WEIGHT * symptoms + SERVICE_WEIGHT * service, 3)
