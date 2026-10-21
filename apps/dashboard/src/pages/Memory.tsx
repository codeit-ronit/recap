import { useState } from "react";
import { api } from "../api";
import { Chip, Mono, OutcomeChip, SimulatedBadge, YesNoChip } from "../components/Badge";
import { Card, Meter, Notice, PageHeader, Segmented } from "../components/Card";
import { EmptyState, ErrorState, Loading } from "../components/EmptyState";
import { JsonView } from "../components/JsonView";
import { RunScope } from "../components/RunPicker";
import { DataTable } from "../components/Table";
import { actionResultTone, fmtArgs, fmtDateTime, fmtScore, isTerminal } from "../format";
import { usePolling, useRunSelection } from "../hooks";
import type { PageProps } from "../hooks";
import type { ActionRecord, ExperienceEpisode, MemoryHit, MemoryPartition } from "../types";

function ActionText({ action }: { action: ActionRecord }) {
  const params = fmtArgs(action.params);
  return (
    <span className="cell-stack">
      <Mono>{`${action.action} on ${action.target}${params ? ` (${params})` : ""}`}</Mono>
      <span className="chip-row">
        <Chip tone={actionResultTone(action.result)}>{action.result}</Chip>
        {action.note ? <span className="muted small">{action.note}</span> : null}
      </span>
    </span>
  );
}

function EpisodeDetail({ episode }: { episode: ExperienceEpisode }) {
  return (
    <div className="detail-grid">
      <div>
        <h3 className="mini-title">Symptoms (from the alert)</h3>
        <ul className="plain-list">
          {episode.symptoms.map((s, i) => (
            <li key={i}>{s}</li>
          ))}
        </ul>
        <h3 className="mini-title">Verified evidence behind the diagnosis</h3>
        {episode.verified_evidence.length ? (
          <ul className="plain-list">
            {episode.verified_evidence.map((ev) => (
              <li key={ev.evidence_id}>
                <Mono>{ev.evidence_id}</Mono> {ev.summary}
              </li>
            ))}
          </ul>
        ) : (
          <span className="muted">none (no supported diagnosis)</span>
        )}
        {episode.unsuccessful_actions.length ? (
          <>
            <h3 className="mini-title">Unsuccessful actions (kept as cautionary history)</h3>
            <ul className="plain-list">
              {episode.unsuccessful_actions.map((a, i) => (
                <li key={i}>
                  <ActionText action={a} />
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </div>
      <div>
        <h3 className="mini-title">Provenance</h3>
        <JsonView value={episode.provenance} maxHeight={260} />
        <div className="muted small spaced">
          verification source <Mono>{episode.verification_source ?? "—"}</Mono> · origin run <Mono>{episode.origin_run_id}</Mono>
        </div>
      </div>
    </div>
  );
}

function EpisodesCard() {
  const [partition, setPartition] = useState<"all" | MemoryPartition>("all");
  const episodes = usePolling(() => api.memory(partition === "all" ? undefined : partition), 10000, [partition]);
  const list = episodes.data ?? [];
  return (
    <Card
      title={`Episodes in memory${episodes.data ? ` (${list.length})` : ""}`}
      subtitle="GET /memory — one consolidated episode per finished run. outcome=success requires an oracle-verified recovery."
      actions={
        <Segmented
          label="Partition"
          value={partition}
          onChange={setPartition}
          options={[
            { value: "all", label: "All" },
            { value: "demo", label: "Demo" },
            { value: "live", label: "Live model" },
            { value: "train", label: "Train" },
          ]}
        />
      }
      flush
    >
      {episodes.error && !episodes.data ? (
        <div className="card-pad">
          <ErrorState error={episodes.error} />
        </div>
      ) : !episodes.data ? (
        <Loading />
      ) : (
        <DataTable
          rows={list}
          rowKey={(e) => e.episode_id}
          empty="No episodes yet. Every finished run writes one (when memory writes are enabled)."
          expand={(e) => <EpisodeDetail episode={e} />}
          columns={[
            {
              key: "id",
              header: "Episode",
              render: (e) => (
                <span className="cell-stack">
                  <Mono nowrap>{e.episode_id}</Mono>
                  <span className="muted small nowrap">{fmtDateTime(e.created_at)}</span>
                </span>
              ),
            },
            {
              key: "scenario",
              header: "Scenario",
              render: (e) => (
                <span className="cell-stack">
                  <Mono nowrap>{e.scenario_id}</Mono>
                  <span className="muted small">alert on {e.service}</span>
                </span>
              ),
            },
            {
              key: "mechanism",
              header: "Mechanism",
              render: (e) =>
                e.failure_mechanism ? (
                  <Mono>{`${e.failure_mechanism}@${e.mechanism_target ?? "?"}`}</Mono>
                ) : (
                  <span className="muted">no verified diagnosis</span>
                ),
            },
            { key: "outcome", header: "Outcome", render: (e) => <OutcomeChip outcome={e.outcome} /> },
            { key: "verified", header: "Oracle-verified", render: (e) => <YesNoChip value={e.verified} /> },
            { key: "partition", header: "Partition", render: (e) => <Chip>{e.partition}</Chip> },
            {
              key: "success",
              header: "Successful action",
              render: (e) => (e.successful_action ? <ActionText action={e.successful_action} /> : <span className="muted">—</span>),
            },
            {
              key: "unsuccessful",
              header: "Unsuccessful",
              numeric: true,
              render: (e) => (e.unsuccessful_actions.length ? <span className="text-warn">{e.unsuccessful_actions.length}</span> : "0"),
            },
            {
              key: "sim",
              header: "Provenance",
              render: (e) => (e.provenance.simulated === true ? <SimulatedBadge /> : <Chip>{String(e.provenance.simulated ?? "unknown")}</Chip>),
            },
          ]}
        />
      )}
    </Card>
  );
}

function HitsTable({ hits }: { hits: MemoryHit[] }) {
  return (
    <DataTable
      rows={hits}
      rowKey={(h) => h.episode.episode_id}
      empty="This run retrieved no similar episodes (memory off, empty, or nothing similar)."
      expand={(h) => <EpisodeDetail episode={h.episode} />}
      // A look-alike whose mechanism does NOT match live evidence is the dangerous case.
      rowClassName={(h) => (!h.safe_to_reuse && h.mechanism_match === false ? "row-warn" : undefined)}
      columns={[
        {
          key: "episode",
          header: "Past episode",
          render: (h) => (
            <span className="cell-stack">
              <Mono nowrap>{h.episode.episode_id}</Mono>
              <span className="muted small">
                {h.episode.scenario_id} · {h.episode.failure_mechanism ?? "no diagnosis"}
              </span>
            </span>
          ),
        },
        { key: "sim", header: "Similarity", render: (h) => <Meter value={h.similarity} max={1} label={fmtScore(h.similarity)} width={110} /> },
        { key: "outcome", header: "Past outcome", render: (h) => <OutcomeChip outcome={h.outcome} /> },
        { key: "verified", header: "Past fix verified", render: (h) => <YesNoChip value={h.episode.verified} /> },
        { key: "match", header: "Mechanism matches live evidence", render: (h) => <YesNoChip value={h.mechanism_match} /> },
        {
          key: "safe",
          header: "Safe to reuse",
          render: (h) => <YesNoChip value={h.safe_to_reuse} yes="YES" no="NO" />,
        },
        { key: "reason", header: "Why", render: (h) => <span className="reason">{h.reason || "—"}</span> },
      ]}
    />
  );
}

function RunHits({ runId, running }: { runId: string; running: boolean }) {
  const detail = usePolling(() => api.run(runId), (d) => (d?.status === "running" || running ? 2500 : null), [runId]);
  if (detail.error && !detail.data) return <ErrorState error={detail.error} />;
  if (!detail.data) return <Loading />;
  const record = detail.data.record;
  if (!record) return <EmptyState title="No checkpoint yet" icon="clock" />;
  return (
    <>
      {!record.config.use_memory ? (
        <Notice tone="neutral" icon="info" title="Memory was disabled for this run">
          The run did not read experience memory (use_memory = false).
        </Notice>
      ) : null}
      <HitsTable hits={record.memory_hits} />
    </>
  );
}

export function MemoryPage({ route }: PageProps) {
  const selection = useRunSelection(route);
  return (
    <>
      <PageHeader
        title="Experience Memory"
        description="Episodic memory of past simulated runs, kept separate from runbook RAG. Memory is historical data, never instructions."
      />
      <EpisodesCard />
      <Card
        title="Memory hits for a run"
        subtitle="record.memory_hits — retrieved by lexical similarity, then re-checked against the run's verified live evidence"
      >
        <Notice tone="warn" icon="alert" title="Similar ≠ safe to reuse">
          A past episode is safe to reuse only if that run succeeded, the independent oracle verified its recovery, and its
          diagnosed mechanism matches what live evidence supports now. A look-alike incident with a different root cause is
          shown as NO, so its old fix is never repeated blindly.
        </Notice>
        <RunScope selection={selection} page="memory">
          {(runId, summary) => <RunHits runId={runId} running={!!summary && !isTerminal(summary.state)} />}
        </RunScope>
      </Card>
    </>
  );
}
