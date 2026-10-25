"""Episodic experience memory: a SQLite store of past runs, kept apart from runbook RAG.

Where it sits: before investigating, the controller asks this store "have we seen an
incident like this before?". After a run ends, the Consolidator turns it into an
episode and the controller adds it here. Runbooks (generic knowledge) live in the RAG
index instead; this store only holds what RECAP itself experienced.

Three rules keep memory honest:
  1. Similar is not safe. `search` returns hits marked "not yet checked". Later,
     `assess_reuse` compares each hit with the live verification report: only a verified
     success whose mechanism AND service match today's evidence (its diagnosis and the
     fix that worked were both on the service diagnosed now), and whose fix still fits it
     (a rollback must restore a version today's evidence names), is safe to reuse.
     Measured on the scenarios: alerts with different causes can read exactly alike, so
     no similarity, lexical or semantic, is ever evidence of the cause.
  2. No contamination. A `MemoryAccess` says which partitions a run may read and where
     it may write. Evaluation test runs read TRAIN only and never write, held-out test
     scenarios never become episodes (see consolidation.py), and `add` refuses any
     TEST-partition episode. Scripted runs use DEMO, real-model runs LIVE, by default; a
     replay writes nothing, and can read memory as it was when its recording began
     (`MemoryAccess.as_of`). Opening a file from before M3, when every run wrote DEMO,
     moves the episodes a real model made to LIVE and records each move (`partition_moves`).
  3. Success needs proof. An episode can be stored as SUCCESS only if the independent
     recovery oracle verified it. Failed runs are kept as cautionary cases.

Search uses word overlap (`similarity`, which routing and the scripted policy use) and,
when the run embeds its incident, a semantic signal that can only ADD candidates: after
the k most similar episodes by their words come up to k more whose text means nearly the
same (cosine >= SEMANTIC_RECALL), closest first, however few words they share. Their
decision-relevant `similarity` stays the lexical one.

Memory is historical data, not instructions: agents must treat hits as untrusted text.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import struct
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from recap.context.evidence import is_plain_value
from recap.core.db import connect
from recap.core.ids import utcnow
from recap.core.models import (
    Contract,
    EpisodeOutcome,
    ExperienceEpisode,
    MemoryHit,
    MemoryPartition,
    ProviderOrigin,
    RunConfig,
    VerificationReport,
)
from recap.knowledge.embeddings import cosine
from recap.memory.similarity import MemoryQuery, episode_similarity

ALL_PARTITIONS = tuple(MemoryPartition)
UNCHECKED_REASON = "similar symptoms; not yet checked against live evidence"
# A semantic match this close adds an episode even when few words match (a paraphrased
# alert). Not tuned on the scenarios: there, every pair of alerts already shares words.
SEMANTIC_RECALL = 0.8
MODEL_ORIGINS = ("local_llm", "free_hosted_llm", "paid_llm")

# The metadata columns make the file easy to inspect and filter with plain SQL;
# `json` holds the complete episode. `created_at` is fixed-width UTC text, so
# ordering by it as text is ordering by time.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    episode_id     TEXT PRIMARY KEY,
    origin_run_id  TEXT NOT NULL,
    scenario_id    TEXT NOT NULL,
    failure_family TEXT NOT NULL,
    partition      TEXT NOT NULL,
    service        TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    verified       INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    json           TEXT NOT NULL
)
"""
# Every episode the store moved to another partition when it opened, with its original
# partition, why and when (see `_move_pre_m3_model_episodes`).
_MOVES_SCHEMA = """
CREATE TABLE IF NOT EXISTS partition_moves (
    episode_id     TEXT NOT NULL,
    from_partition TEXT NOT NULL,
    to_partition   TEXT NOT NULL,
    reason         TEXT NOT NULL,
    moved_at       TEXT NOT NULL
)
"""
# Before M3 every run wrote DEMO. This consolidator wrote those episodes; since M3 a run's
# partition follows its origin, so a newer episode is in DEMO because its run chose DEMO.
_PRE_M3_CONSOLIDATOR = "consolidator-v1"


class MemoryAccess(Contract):
    """Which memory partitions a run may read, and whether (and where) it may write."""

    readable_partitions: list[MemoryPartition]
    write_enabled: bool = True
    write_partition: MemoryPartition = MemoryPartition.DEMO
    # Memory as it was at this moment: episodes created at or after it are not read. A
    # replay reads as of the moment its recording began, so it finds what the original run
    # could have found, never the episode that run wrote when it ended. None: as it is now.
    created_before: datetime | None = None

    @classmethod
    def default(cls) -> MemoryAccess:
        """Demo and CLI runs: read TRAIN + DEMO, write new episodes to DEMO."""
        return cls(
            readable_partitions=[MemoryPartition.TRAIN, MemoryPartition.DEMO],
            write_partition=MemoryPartition.DEMO,
        )

    @classmethod
    def eval_train(cls) -> MemoryAccess:
        """Evaluation training runs: read and write TRAIN only."""
        return cls(
            readable_partitions=[MemoryPartition.TRAIN],
            write_partition=MemoryPartition.TRAIN,
        )

    @classmethod
    def eval_test(cls) -> MemoryAccess:
        """Evaluation test runs: read TRAIN only and never write any memory."""
        # Labeled TEST as well, so even a forced write would be refused by `add`.
        return cls(
            readable_partitions=[MemoryPartition.TRAIN],
            write_enabled=False,
            write_partition=MemoryPartition.TEST,
        )

    @classmethod
    def for_origin(cls, origin: ProviderOrigin) -> MemoryAccess:
        """The default for a run whose decisions come from `origin`.

        scripted: TRAIN + DEMO, writes DEMO. A real model: TRAIN + LIVE, writes LIVE (its
        episodes never mix with the scripted policy's). A replay reproduces a recorded run:
        it reads like the model it replays and never writes (it is not new experience). To
        read what the original run read, it must also read `as_of` the recording's start.
        """
        if origin == "scripted":
            return cls.default()
        live = cls(
            readable_partitions=[MemoryPartition.TRAIN, MemoryPartition.LIVE],
            write_partition=MemoryPartition.LIVE,
        )
        if origin == "replay":
            return live.model_copy(update={"write_enabled": False})
        return live

    @classmethod
    def from_run_config(cls, config: RunConfig) -> MemoryAccess:
        """Translate a run's memory switches into an access rule (unset: the DEMO default)."""
        default = cls.default()
        return cls(
            readable_partitions=list(config.memory_read_partitions or default.readable_partitions),
            write_enabled=config.memory_write,
            write_partition=config.memory_write_partition or default.write_partition,
        )

    def as_of(self, moment: datetime) -> MemoryAccess:
        """The same access, reading memory as it was at `moment` (a naive time is UTC)."""
        return self.model_copy(update={"created_before": moment})


class ExperienceStore:
    """SQLite-backed episodic memory with partition-aware, deterministic search."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.execute(_MOVES_SCHEMA)
            _move_pre_m3_model_episodes(conn)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection that commits on success and always closes."""
        # One connection per operation keeps the store safe to share between the
        # controller and API worker threads; memory is touched only a few times per run.
        conn = connect(self.db_path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # --- writing ---------------------------------------------------------------------

    def add(self, episode: ExperienceEpisode, access: MemoryAccess) -> bool:
        """Store one episode. Returns False (and stores nothing) if this run may not write."""
        if not access.write_enabled:
            return False
        _check_storable(episode, access)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO episodes (episode_id, origin_run_id, scenario_id, failure_family,"
                " partition, service, outcome, verified, created_at, json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    episode.episode_id,
                    episode.origin_run_id,
                    episode.scenario_id,
                    episode.failure_family,
                    episode.partition.value,
                    episode.service,
                    episode.outcome.value,
                    int(episode.verified),
                    _sortable_time(episode.created_at),
                    episode.model_dump_json(),
                ),
            )
        return True

    # --- reading ---------------------------------------------------------------------

    def get(self, episode_id: str) -> ExperienceEpisode | None:
        """One episode by ID, or None if it does not exist."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT json FROM episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
        return ExperienceEpisode.model_validate_json(row["json"]) if row else None

    def list(
        self, partitions: list[MemoryPartition] | None = None, limit: int = 200
    ) -> list[ExperienceEpisode]:
        """Episodes newest first, optionally only from some partitions (API and dashboard)."""
        return self._select(ALL_PARTITIONS if partitions is None else partitions, limit=limit)

    def count(self) -> int:
        """Total number of stored episodes."""
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]

    def search(
        self,
        query: MemoryQuery,
        access: MemoryAccess,
        *,
        k: int = 3,
        min_similarity: float = 0.2,
        query_vector: Sequence[float] | None = None,
        embedding_model: str | None = None,
    ) -> list[MemoryHit]:
        """Up to `k` readable episodes at least `min_similarity` alike, most similar first.

        With `query_vector` (the incident embedded by `embedding_model`), up to `k` more
        follow: episodes embedded by the same model whose cosine is at least
        SEMANTIC_RECALL and that the list does not hold yet, closest first. Every hit starts
        as "similar but unchecked" (safe_to_reuse=False). Only `assess_reuse` can mark it
        safe, after comparing it with live evidence.
        """
        candidates = self._select(access.readable_partitions, created_before=access.created_before)
        hits: list[MemoryHit] = []
        close: dict[str, float] = {}  # episode ID -> cosine, for each semantic match to recall
        for episode in candidates:
            similarity = episode_similarity(query, episode)
            signals = {"lexical": similarity}
            semantic = _semantic(query_vector, embedding_model, episode)
            if semantic is not None:
                signals["semantic"] = round(semantic, 3)
                if semantic >= SEMANTIC_RECALL:
                    close[episode.episode_id] = semantic
            if similarity >= min_similarity or episode.episode_id in close:
                hits.append(
                    MemoryHit(
                        episode=episode,
                        similarity=similarity,
                        mechanism_match=None,
                        outcome=episode.outcome,
                        safe_to_reuse=False,
                        reason=UNCHECKED_REASON,
                        signals=signals,
                    )
                )
        # Candidates arrive newest first (then by ID), and Python's sort is stable,
        # so equally similar hits keep that order: the result is fully deterministic.
        lexical = sorted(
            (hit for hit in hits if hit.similarity >= min_similarity),
            key=lambda hit: hit.similarity,
            reverse=True,
        )[:k]
        # Semantic recall only adds, after the lexical hits and with a cut of its own: ranked
        # with them, a paraphrase (few shared words) would sort last, and k episodes alerted
        # on the same service (0.3 for the service alone) would always fill the list first.
        unlisted = close.keys() - {hit.episode.episode_id for hit in lexical}
        recalled = sorted(
            (hit for hit in hits if hit.episode.episode_id in unlisted),
            key=lambda hit: close[hit.episode.episode_id],
            reverse=True,
        )
        return lexical + recalled[:k]

    def _select(
        self,
        partitions: Sequence[MemoryPartition],
        *,
        limit: int = -1,
        created_before: datetime | None = None,
    ) -> list[ExperienceEpisode]:
        """Episodes in `partitions` (created before `created_before`), newest first, then by ID."""
        # Only "?" placeholders are formatted into the SQL; values are always bound.
        sql = f"SELECT json FROM episodes WHERE partition IN ({_placeholders(partitions)})"
        params: list[object] = [partition.value for partition in partitions]
        if created_before is not None:
            # Both sides are fixed-width UTC text, so comparing the text compares the times.
            sql += " AND created_at < ?"
            params.append(_sortable_time(created_before))
        sql += " ORDER BY created_at DESC, episode_id LIMIT ?"
        params.append(limit)  # SQLite reads LIMIT -1 as "no limit"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ExperienceEpisode.model_validate_json(row["json"]) for row in rows]


def assess_reuse(
    hit: MemoryHit, report: VerificationReport, restore_points: Sequence[str] | None = None
) -> MemoryHit:
    """Decide whether a *similar* past episode is also *safe to reuse* today.

    Safe needs every condition: the past run succeeded, the oracle verified its recovery,
    live evidence supports the same mechanism on the same service (the past diagnosis and
    the fix that worked were both on it), and the remembered fix still fits today's
    evidence. For a rollback that means its version is one of today's `restore_points`
    (the versions the deployment evidence or a dry run names) and not the release
    diagnosed as bad. A high similarity never outweighs a failed condition. Returns a new
    hit; the one passed in is not changed.
    """
    supporting, contradicting = _conditions(hit, report, restore_points)
    live = report.best.mechanism if report.best is not None else None
    mechanism_match = live is not None and live == hit.episode.failure_mechanism
    safe = not contradicting
    if safe:
        reason = "safe to reuse: " + " and ".join(supporting)
    else:
        reason = (
            " and ".join(["similar symptoms", *supporting]) + ", but " + "; ".join(contradicting)
        )
    return hit.model_copy(
        update={
            "mechanism_match": mechanism_match,
            "safe_to_reuse": safe,
            "reason": reason,
            "supporting": supporting,
            "contradicting": contradicting,
        }
    )


def rank_hits(hits: Sequence[MemoryHit]) -> list[MemoryHit]:
    """Assessed hits, best first: safe to reuse, then a matching mechanism, then similarity.

    Re-ranking by what live evidence confirmed, not by look-alike symptoms (stable sort:
    equal hits keep their search order).
    """
    return sorted(
        hits,
        key=lambda hit: (not hit.safe_to_reuse, hit.mechanism_match is not True, -hit.similarity),
    )


def memory_conflict(hits: Sequence[MemoryHit], threshold: float) -> bool:
    """True if similar hits (similarity >= threshold) disagree on mechanism or outcome."""
    close = [hit for hit in hits if hit.similarity >= threshold]
    # An episode without a verified diagnosis has no opinion on the mechanism, and a
    # single hit can never disagree with itself, so a conflict needs two opinions.
    mechanisms = {hit.episode.failure_mechanism for hit in close} - {None}
    outcomes = {hit.outcome for hit in close}
    return len(mechanisms) > 1 or len(outcomes) > 1


# --- helpers ---------------------------------------------------------------------------


def _check_storable(episode: ExperienceEpisode, access: MemoryAccess) -> None:
    """Raise ValueError if storing this episode would contaminate or mislabel memory."""
    if episode.partition == MemoryPartition.TEST:
        raise ValueError("test episodes are never stored: test runs must not create memory")
    if episode.partition != access.write_partition:
        raise ValueError(
            f"episode partition '{episode.partition}' does not match this run's write "
            f"partition '{access.write_partition}'"
        )
    if episode.outcome == EpisodeOutcome.SUCCESS and not episode.verified:
        raise ValueError("an episode is a success only after independent recovery verification")


def _move_pre_m3_model_episodes(conn: sqlite3.Connection) -> None:
    """Move the episodes a real model wrote to DEMO before M3 to LIVE, recording each move.

    Before M3 every run wrote DEMO, so a data folder from then has model episodes where
    scripted runs read them and model runs never do. Only an episode whose provenance says
    a model made it moves (`_pre_m3_model`), and only its partition changes, in the column
    and in the stored episode. Safe at every start: a moved episode is no longer in DEMO,
    and an unreadable row is left as it is.
    """
    demo, live = MemoryPartition.DEMO.value, MemoryPartition.LIVE.value
    rows = conn.execute("SELECT episode_id, json FROM episodes WHERE partition = ?", (demo,))
    for row in rows.fetchall():
        try:
            stored = json.loads(row["json"])
        except ValueError:
            continue
        provider = _pre_m3_model(stored.get("provenance") if isinstance(stored, dict) else None)
        if provider is None:
            continue
        stored["partition"] = live
        moved = conn.execute(
            "UPDATE episodes SET partition = ?, json = ? WHERE episode_id = ? AND partition = ?",
            (
                live,
                json.dumps(stored, ensure_ascii=False, separators=(",", ":")),
                row["episode_id"],
                demo,
            ),
        )
        if moved.rowcount == 1:  # another process opening the file may have moved it first
            conn.execute(
                "INSERT INTO partition_moves (episode_id, from_partition, to_partition, reason,"
                " moved_at) VALUES (?, ?, ?, ?, ?)",
                (
                    row["episode_id"],
                    demo,
                    live,
                    f"made by {provider} before M3, when every run wrote {demo}",
                    _sortable_time(utcnow()),
                ),
            )


def _pre_m3_model(provenance: object) -> str | None:
    """The provider of a pre-M3 episode whose decisions came from a real model, else None.

    Every model provider is named "<provider>:<model>" ("anthropic:...", "ollama:qwen3:8b";
    a replay of a recorded model "replay:ollama:qwen3:8b"), and a red-team planner wrapping
    one only appends "+prompt-injected(red-team)". The scripted policy's name,
    "deterministic-scripted-policy", has no model part, with or without that suffix.
    """
    if not isinstance(provenance, dict) or provenance.get("created_by") != _PRE_M3_CONSOLIDATOR:
        return None
    provider = provenance.get("provider")
    return provider if isinstance(provider, str) and ":" in provider else None


def _conditions(
    hit: MemoryHit, report: VerificationReport, restore_points: Sequence[str] | None
) -> tuple[list[str], list[str]]:
    """(conditions that hold, conditions that argue against reuse), in plain words."""
    episode, best = hit.episode, report.best
    supporting: list[str] = []
    contradicting: list[str] = []
    past = episode.failure_mechanism
    live = best.mechanism if best is not None else None
    if live is None:
        contradicting.append("live evidence has not confirmed a mechanism yet")
    elif past is None:
        contradicting.append("the past episode has no verified diagnosis to compare")
    elif live == past:
        supporting.append(f"live evidence also points to {_words(live)}")
        contradicting += _other_service(episode, best.target_service if best is not None else None)
    else:
        contradicting.append(f"live evidence points to {_words(live)}, not {_words(past)}")

    if hit.outcome != EpisodeOutcome.SUCCESS:
        contradicting.append(
            f"the past attempt ended as {_words(hit.outcome)} (a caution, not a recipe)"
        )
    elif episode.verified:
        supporting.append("the past fix was independently verified")
    else:
        contradicting.append("the past success was never independently verified")

    contradicting += _stale_fix(hit, report, restore_points)
    return supporting, contradicting


def _other_service(episode: ExperienceEpisode, target: str | None) -> list[str]:
    """Why a remembered episode is not about the service diagnosed now (empty if it is).

    The same service means both the past diagnosis and the fix that worked: a fix that acted
    on another service is a recipe for that one. An episode (or a live diagnosis) that names
    no service cannot show it, so it does not pass by saying nothing.
    """
    remembered = episode.mechanism_target
    if not remembered:
        return ["the past episode names no service to compare"]
    if not target:
        return ["the live diagnosis names no service to compare"]
    if remembered != target:
        return [f"the live diagnosis is on {_shown(target)}, not on {_shown(remembered)}"]
    fix = episode.successful_action
    if fix is not None and fix.target != target:
        return [f"the remembered fix acts on {_shown(fix.target)}, not on {_shown(target)}"]
    return []


def _stale_fix(
    hit: MemoryHit, report: VerificationReport, restore_points: Sequence[str] | None
) -> list[str]:
    """A remembered rollback whose version no longer fits today's evidence."""
    fix = hit.episode.successful_action
    if fix is None:
        return []
    version = str(fix.params.get("to_version") or "")
    if not version or not is_plain_value(version):
        # A rollback that names no plain release cannot be checked against today's restore
        # points (a tampered episode can hide its version under another key): never "safe".
        if fix.action == "execute_rollback":
            return ["the remembered rollback names no release it restored"]
        return []
    best = report.best
    bad = str(best.details.get("version") or "") if best is not None else ""
    if bad and version == bad:
        return [
            f"the remembered rollback restores {_shown(version)}, the release diagnosed as bad now"
        ]
    if restore_points is not None and version not in restore_points:
        named = ", ".join(_shown(point) for point in restore_points) or "no version"
        restored = _shown(version)
        return [f"the remembered rollback restores {restored}, but today's evidence names {named}"]
    return []


def _shown(value: str) -> str:
    """A value safe to repeat in RECAP's reuse verdict, which prompts show as trusted text.

    Episodes come from a file on disk: a tampered one could hold instruction text in a
    "service name". Only plain identifiers (service names, versions) are repeated.
    """
    return value if is_plain_value(value) else "(withheld)"


def _semantic(
    query_vector: Sequence[float] | None, model: str | None, episode: ExperienceEpisode
) -> float | None:
    """Cosine of the query and the episode's stored embedding, when the same model made both."""
    if query_vector is None or not model or episode.embedding_model != model:
        return None
    vector = decode_vector(episode.embedding)
    if vector is None or len(vector) != len(query_vector):
        return None
    return cosine(query_vector, vector)


def encode_vector(vector: Sequence[float]) -> str:
    """A vector as base64 float32 (how an episode stores its embedding)."""
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def decode_vector(blob: str | None) -> list[float] | None:
    """The stored embedding back as floats; None if absent or unreadable."""
    if not blob:
        return None
    try:
        raw = base64.b64decode(blob, validate=True)
        return list(struct.unpack(f"<{len(raw) // 4}f", raw))
    except (ValueError, struct.error):
        return None


def _words(label: str) -> str:
    """Show an enum value as plain words, e.g. 'resource_pressure' -> 'resource pressure'."""
    return label.replace("_", " ")


def _placeholders(values: Sequence[object]) -> str:
    """'?, ?, ?' with one placeholder per value."""
    return ", ".join("?" for _ in values)


def _sortable_time(moment: datetime) -> str:
    """Fixed-width UTC text, e.g. '2026-03-14T10:00:00.000000+00:00'."""
    if moment.tzinfo is None:  # RECAP's convention: a naive timestamp is already UTC
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="microseconds")
