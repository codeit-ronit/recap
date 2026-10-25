import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import { Chip, HypothesisChip, Mono, OriginChip, OutcomeChip, SimulatedBadge, YesNoChip } from "../components/Badge";
import { Card, KeyValues, Meter, Notice, PageHeader, Segmented } from "../components/Card";
import { EmptyState, ErrorState, Loading } from "../components/EmptyState";
import { Icon } from "../components/Icon";
import { JsonView } from "../components/JsonView";
import { RunScope } from "../components/RunPicker";
import { DataTable } from "../components/Table";
import { actionResultTone, fmtArgs, fmtDateTime, fmtScore, isTerminal } from "../format";
import { usePolling, useRunSelection } from "../hooks";
import type { PageProps } from "../hooks";
import type { ActionRecord, ExperienceEpisode, MemoryHit, MemoryPartition, RunRecord } from "../types";
import {
  HIT_ORDER_NOTES,
  RESTORE_NOTES,
  claimWords,
  decisionRows,
  episodeOrigin,
  episodePartitionBadge,
  episodeSummary,
  hitConditions,
  hitOrder,
  hitSignals,
  isPlanted,
  memoryTrace,
  partitionBadge,
  plantedNote,
} from "../view/memory";
import type { ClaimRow, EpisodeSummary, FieldRow, MemoryTrace, TrajectoryRow } from "../view/memory";
import "../styles/memory-m3.css";

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

/** A partition in plain words (live = real-model runs), for the partitions a run reads. */
function PartitionChip({ partition }: { partition: string | null | undefined }) {
  const badge = partitionBadge(partition);
  return (
    <Chip tone={badge.tone} title={badge.title}>
      {badge.label}
    </Chip>
  );
}

/** One episode's partition, worded by who made its decisions (a demo episode may be a model's, before M3). */
function EpisodePartitionChip({ episode }: { episode: ExperienceEpisode }) {
  const badge = episodePartitionBadge(episode);
  return (
    <Chip tone={badge.tone} title={badge.title}>
      {badge.label}
    </Chip>
  );
}

/** A red-team plant is a false memory planted on purpose: marked loudly wherever it appears. */
function PlantChip() {
  return (
    <Chip tone="bad" title="Planted on purpose by a red-team setup: not experience from any run RECAP recorded.">
      <Icon name="alert" size={12} /> RED-TEAM PLANT
    </Chip>
  );
}

/** A plant's outcome, verification and fix are only its own claims: label them as such. */
function Claimed({ planted, children }: { planted: boolean; children: ReactNode }) {
  if (!planted) return <>{children}</>;
  return (
    <span className="cell-stack">
      {children}
      <span className="muted small">claimed by the plant</span>
    </span>
  );
}

function PlantNotice({ note }: { note: string }) {
  return (
    <div className="mem-plant">
      <Notice tone="bad" icon="alert" title="RED-TEAM PLANT: a false memory, planted on purpose">
        No run that RECAP recorded produced or verified this episode. Its outcome, oracle verification, diagnosis and fix are
        what the plant claims; a run that finds it must reject it on live evidence.
        <pre className="raw excerpt mem-plant-note">{`provenance.planted: ${note}`}</pre>
      </Notice>
    </div>
  );
}

function FieldValue({ row }: { row: FieldRow }) {
  if (row.state === "value") return <Mono>{row.value}</Mono>;
  return <span className="muted">{row.state === "none" ? "—" : "not recorded"}</span>;
}

/** Who made the past run's decisions, and with what (consolidator-v2 provenance). */
function DecisionProvenance({ episode }: { episode: ExperienceEpisode }) {
  const origin = episodeOrigin(episode);
  const rows = decisionRows(episode).map((row): [ReactNode, ReactNode] => {
    if (row.key === "origin" && row.state === "value") {
      return [
        row.label,
        <span className="chip-row">
          <OriginChip provider={{ origin }} />
          {origin ? null : <Mono>{row.value}</Mono>}
        </span>,
      ];
    }
    // The scripted policy has no model: say so, so its episodes never read as model reasoning.
    if (row.key === "model" && row.state === "none" && origin === "scripted") {
      return [row.label, <span className="muted">none: scripted policy, not an LLM</span>];
    }
    return [row.label, <FieldValue row={row} />];
  });
  return <KeyValues className="kv-wide" rows={rows} />;
}

function MethodLabel({ method }: { method: EpisodeSummary["method"] }) {
  if (!method) return <span className="muted small">summary method not recorded</span>;
  if (!method.known) {
    return (
      <span className="chip-row">
        <span className="muted small">summary method</span>
        <Mono>{method.text}</Mono>
      </span>
    );
  }
  return (
    <Chip tone="accent" title="summary_method: no model wrote or reworded this summary">
      {method.text}
    </Chip>
  );
}

function Trajectory({ steps, empty }: { steps: TrajectoryRow[]; empty: string }) {
  if (!steps.length) return <span className="muted">{empty}</span>;
  return (
    <div className="table-wrap">
      <table className="table compact mem-trajectory">
        <thead>
          <tr>
            <th style={{ textAlign: "right" }}>#</th>
            <th>Agent</th>
            <th>Call</th>
            <th>Evidence</th>
          </tr>
        </thead>
        <tbody>
          {steps.map((step, i) => (
            <tr key={`${step.evidenceId}-${i}`}>
              <td className="num">{i + 1}</td>
              <td>{step.agent ? <Mono>{step.agent}</Mono> : <span className="muted">—</span>}</td>
              <td>
                <span className="chip-row">
                  <Mono>{step.call || "—"}</Mono>
                  {step.kind === "doc" ? (
                    <Chip title="DOC evidence: a runbook search receipt, which the verifier never counts">runbook search</Chip>
                  ) : step.kind !== "tool" ? (
                    <Chip>{step.kind}</Chip>
                  ) : null}
                </span>
              </td>
              <td>
                <Mono nowrap>{step.evidenceId || "—"}</Mono>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CheckedClaims({ claims, empty }: { claims: ClaimRow[]; empty: string }) {
  if (!claims.length) return <span className="muted">{empty}</span>;
  return (
    <div className="mem-claims">
      {claims.map((claim, i) => (
        <div className="check" key={i}>
          <div className="chip-row">
            <HypothesisChip status={claim.status || null} />
            <span title={`${claim.mechanism || "?"}@${claim.target ?? "?"}`}>{claimWords(claim)}</span>
          </div>
          {claim.detail && !claim.detailChecked ? (
            // Not shaped like a version or a service: the claim's own text, shown as data.
            <div className="chip-row small">
              <span className="muted">judged as</span>
              <q className="mem-data mem-quote">{claim.detail}</q>
              <span className="trust-tag trust-untrusted" title="The release the claim named, as its agent wrote it: untrusted data">
                untrusted · the claim's text
              </span>
            </div>
          ) : null}
          <div className="chip-row small">
            <span className="muted">proposed by</span>
            {claim.proposers.length ? (
              claim.proposers.map((role, j) => (
                <Chip key={`${role}-${j}`} mono>
                  {role}
                </Chip>
              ))
            ) : (
              <span className="muted">—</span>
            )}
          </div>
          <div className="chip-row small">
            <span className="muted">rules</span>
            {claim.ruleIds.length ? claim.ruleIds.map((id, j) => <Mono key={`${id}-${j}`}>{id}</Mono>) : <span className="muted">—</span>}
          </div>
          {claim.failed.length ? (
            <ul className="rule-list">
              {claim.failed.map((line, j) => (
                <li key={j} className="text-bad">
                  <Icon name="x" size={13} /> {line}
                  {/* RECAP's sentence, repeating the release the claim named in free text. */}
                  {claim.failedQuotesClaim[j] ? (
                    <span
                      className="trust-tag trust-untrusted mem-rule-tag"
                      title="RECAP's rule line, but it repeats the release the claim named, which its agent wrote: that part is untrusted data."
                    >
                      quotes the claim's text
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ))}
    </div>
  );
}

/** consolidator-v2: what the past run observed, how its claims were judged, and what its fix needs. */
function EpisodeSummaryView({ episode }: { episode: ExperienceEpisode }) {
  const summary = episodeSummary(episode);
  const content = summary.trajectory.length + summary.claims.length + summary.basis.length + summary.preconditions.length > 0;
  if (summary.source === "planted" && !content) {
    return (
      <div className="mem-summary muted small">
        No run summary (trajectory, checked claims, diagnosis basis, fix preconditions): a planted episode has no recorded run
        behind it.
      </div>
    );
  }
  if (summary.source === "not_recorded") {
    return (
      <div className="mem-summary muted small">
        <span>
          Run summary (trajectory, checked claims, diagnosis basis, fix preconditions): not recorded
          {summary.createdBy ? (
            <>
              {" "}
              (episode written by <Mono>{summary.createdBy}</Mono>).
            </>
          ) : (
            "."
          )}
        </span>
      </div>
    );
  }
  // Empty lists mean "none" only when RECAP extracted them; a plant's are just missing.
  const extracted = summary.source === "extracted";
  const none = (what: string) => (extracted ? `none: ${what}` : "none recorded");
  return (
    <div className="mem-summary">
      <div className="mem-title-row">
        <h3 className="mini-title">Run summary</h3>
        {extracted ? <MethodLabel method={summary.method} /> : <Chip tone="bad">written by the plant, not extracted by RECAP</Chip>}
        {extracted ? <OriginChip provider={{ origin: episodeOrigin(episode) }} /> : null}
      </div>
      <div className="mem-summary-grid">
        <div>
          <h3 className="mini-title">Trajectory: what the run observed</h3>
          <Trajectory steps={summary.trajectory} empty={none("the run recorded no tool results or runbook searches")} />
        </div>
        <div>
          <h3 className="mini-title">Diagnosis basis</h3>
          {summary.basis.length ? (
            <ul className="rule-list">
              {summary.basis.map((line, i) => (
                <li key={i} className="text-ok">
                  <Icon name="check" size={13} /> {line}
                </li>
              ))}
            </ul>
          ) : (
            <span className="muted">{none("the run reached no supported diagnosis")}</span>
          )}
          <h3 className="mini-title">Checked claims</h3>
          <CheckedClaims claims={summary.claims} empty={none("no claim was checked")} />
          <h3 className="mini-title">Fix preconditions</h3>
          {summary.preconditions.length ? (
            <KeyValues
              className="kv-wide"
              rows={summary.preconditions.map(([key, value]): [ReactNode, ReactNode] => [
                <Mono>{key}</Mono>,
                value ? <Mono>{value}</Mono> : <span className="muted">—</span>,
              ])}
            />
          ) : (
            <span className="muted">{none("the run had no verified fix")}</span>
          )}
        </div>
      </div>
    </div>
  );
}

function EpisodeDetail({ episode }: { episode: ExperienceEpisode }) {
  const planted = plantedNote(episode);
  return (
    <div className="stack">
      {planted !== null ? <PlantNotice note={planted} /> : null}
      <div className="detail-grid">
        <div>
          <div className="mem-title-row">
            <h3 className="mini-title">Symptoms (from the alert)</h3>
            <span className="trust-tag trust-untrusted">untrusted alert text</span>
          </div>
          <ul className="plain-list">
            {episode.symptoms.map((s, i) => (
              <li key={i} className="mem-data">
                {s}
              </li>
            ))}
          </ul>
          <div className="mem-title-row">
            <h3 className="mini-title">Verified evidence behind the diagnosis</h3>
            <span className="trust-tag trust-untrusted">untrusted tool output</span>
          </div>
          {episode.verified_evidence.length ? (
            <ul className="plain-list">
              {episode.verified_evidence.map((ev) => (
                <li key={ev.evidence_id}>
                  <Mono>{ev.evidence_id}</Mono> <span className="mem-data">{ev.summary}</span>
                </li>
              ))}
            </ul>
          ) : (
            <span className="muted">{planted !== null ? "none: the plant cites no evidence" : "none (no supported diagnosis)"}</span>
          )}
          {episode.unsuccessful_actions.length ? (
            <>
              <h3 className="mini-title mem-after">Unsuccessful actions (kept as cautionary history)</h3>
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
        <div className="mem-side">
          <h3 className="mini-title">Who made the decisions</h3>
          <DecisionProvenance episode={episode} />
          <h3 className="mini-title">Provenance</h3>
          <JsonView value={episode.provenance} maxHeight={260} />
          <div className="muted small spaced">
            verification source <Mono>{episode.verification_source ?? "—"}</Mono> · origin run <Mono>{episode.origin_run_id}</Mono>
          </div>
        </div>
      </div>
      <EpisodeSummaryView episode={episode} />
    </div>
  );
}

function EpisodesCard() {
  const [partition, setPartition] = useState<"all" | MemoryPartition>("all");
  const episodes = usePolling(() => api.memory(partition === "all" ? undefined : partition), 10000, [partition]);
  const list = episodes.data ?? [];
  const plants = list.filter(isPlanted).length;
  return (
    <Card
      title={`Episodes in memory${episodes.data ? ` (${list.length})` : ""}`}
      subtitle="GET /memory — one consolidated episode per finished run. outcome=success requires an oracle-verified recovery. Partitions: demo = scripted runs (and real-model runs before M3), live = real-model runs since M3, train = evaluation training."
      actions={
        <Segmented
          label="Partition"
          value={partition}
          onChange={setPartition}
          options={[
            { value: "all", label: "All" },
            { value: "demo", label: "Demo" },
            { value: "live", label: "Live" },
            { value: "train", label: "Train" },
          ]}
        />
      }
      flush
    >
      {plants ? (
        <div className="card-pad mem-plant">
          <Notice tone="bad" icon="alert" title={`${plants} red-team ${plants === 1 ? "plant" : "plants"} in this list`}>
            A planted episode is a false memory put here on purpose, to test that RECAP rejects it on live evidence. It is not
            experience: its diagnosis, outcome and verification are only what the plant claims.
          </Notice>
        </div>
      ) : null}
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
          rowClassName={(e) => (isPlanted(e) ? "row-bad" : undefined)}
          columns={[
            {
              key: "id",
              header: "Episode",
              render: (e) => (
                <span className="cell-stack">
                  <Mono nowrap>{e.episode_id}</Mono>
                  <span className="muted small nowrap">{fmtDateTime(e.created_at)}</span>
                  {isPlanted(e) ? (
                    <span>
                      <PlantChip />
                    </span>
                  ) : null}
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
                  <Claimed planted={isPlanted(e)}>
                    <Mono>{`${e.failure_mechanism}@${e.mechanism_target ?? "?"}`}</Mono>
                  </Claimed>
                ) : (
                  <span className="muted">no verified diagnosis</span>
                ),
            },
            {
              key: "outcome",
              header: "Outcome",
              render: (e) => (
                <Claimed planted={isPlanted(e)}>
                  <OutcomeChip outcome={e.outcome} />
                </Claimed>
              ),
            },
            {
              key: "verified",
              header: "Oracle-verified",
              render: (e) => (
                <Claimed planted={isPlanted(e)}>
                  <YesNoChip value={e.verified} />
                </Claimed>
              ),
            },
            { key: "partition", header: "Partition", render: (e) => <EpisodePartitionChip episode={e} /> },
            {
              key: "success",
              header: "Successful action",
              render: (e) =>
                e.successful_action ? (
                  <Claimed planted={isPlanted(e)}>
                    <ActionText action={e.successful_action} />
                  </Claimed>
                ) : (
                  <span className="muted">—</span>
                ),
            },
            {
              key: "unsuccessful",
              header: "Unsuccessful",
              numeric: true,
              render: (e) => (e.unsuccessful_actions.length ? <span className="text-warn">{e.unsuccessful_actions.length}</span> : "0"),
            },
            { key: "sim", header: "Provenance", render: (e) => <ProvenanceChips episode={e} /> },
          ]}
        />
      )}
    </Card>
  );
}

/** Simulated or not, and where the decisions came from (origin since M3, the provider name before). */
function ProvenanceChips({ episode }: { episode: ExperienceEpisode }) {
  if (isPlanted(episode)) {
    return (
      <Chip tone="bad" outline>
        planted
      </Chip>
    );
  }
  const origin = episodeOrigin(episode);
  const provider = typeof episode.provenance.provider === "string" ? episode.provenance.provider : null;
  return (
    <span className="cell-stack">
      <span className="chip-row">
        {episode.provenance.simulated === true ? <SimulatedBadge /> : <Chip>{String(episode.provenance.simulated ?? "unknown")}</Chip>}
        {origin ? <OriginChip provider={{ origin }} /> : null}
      </span>
      {!origin && provider ? <span className="muted small mono">{provider}</span> : null}
    </span>
  );
}

/** The semantic signal (cosine), when the run embedded its incident with the episode's embedder. */
function SemanticValue({ hit }: { hit: MemoryHit }) {
  const signals = hitSignals(hit);
  if (signals.semantic !== null) {
    return (
      <span className="muted small" title="Cosine of the two incidents' embeddings: it can add a candidate, never decide reuse.">
        semantic <span className="num">{fmtScore(signals.semantic)}</span>
      </span>
    );
  }
  if (!signals.recorded) return <span className="muted small">semantic: not recorded</span>;
  return (
    <span className="muted small" title="No semantic signal: this run did not embed its incident, or the episode was not embedded by the same model.">
      semantic —
    </span>
  );
}

/** RECAP's reuse check: the conditions that held and the ones against reuse (its own text). */
function Conditions({ hit }: { hit: MemoryHit }) {
  const conditions = hitConditions(hit);
  // Unchecked hits, and runs checked before M3, have only the one-line reason.
  if (!conditions.recorded) return <span className="reason">{hit.reason || "—"}</span>;
  return (
    <div className="mem-conditions">
      {conditions.held.length ? (
        <>
          <span className="mem-cond-label">Held</span>
          <ul className="rule-list">
            {conditions.held.map((line, i) => (
              <li key={i} className="text-ok">
                <Icon name="check" size={13} /> {line}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {conditions.against.length ? (
        <>
          <span className="mem-cond-label">Against reuse</span>
          <ul className="rule-list">
            {conditions.against.map((line, i) => (
              <li key={i} className="text-bad">
                <Icon name="x" size={13} /> {line}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {/* The check reads a plant's stored claims like any episode's; only live evidence decides. */}
      {isPlanted(hit.episode) ? <span className="muted small">a plant's past outcome and verification are only its claims</span> : null}
    </div>
  );
}

function HitDetail({ hit }: { hit: MemoryHit }) {
  const signals = hitSignals(hit);
  const found = [
    `lexical similarity ${fmtScore(signals.lexical)}`,
    signals.semantic !== null ? `semantic ${fmtScore(signals.semantic)}` : null,
    ...signals.other.map(([key, value]) => `${key} ${value}`),
  ].filter(Boolean);
  return (
    <div className="stack">
      <div>
        <h3 className="mini-title">RECAP's reuse verdict</h3>
        <div className="reason">{hit.reason || "—"}</div>
        <div className="muted small spaced">found by {found.join(" · ")}</div>
      </div>
      <EpisodeDetail episode={hit.episode} />
    </div>
  );
}

function HitsTable({ hits }: { hits: MemoryHit[] }) {
  return (
    <DataTable
      className="wrap-head"
      rows={hits}
      rowKey={(h) => h.episode.episode_id}
      empty="This run retrieved no similar episodes (memory off, empty, or nothing similar)."
      expand={(h) => <HitDetail hit={h} />}
      // A planted false memory is marked loudly; a look-alike whose mechanism does NOT match
      // live evidence is the dangerous case.
      rowClassName={(h) => (isPlanted(h.episode) ? "row-bad" : !h.safe_to_reuse && h.mechanism_match === false ? "row-warn" : undefined)}
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
              <span className="chip-row">
                <EpisodePartitionChip episode={h.episode} />
                {isPlanted(h.episode) ? <PlantChip /> : null}
              </span>
            </span>
          ),
        },
        {
          key: "similarity",
          header: "Similarity (lexical, semantic)",
          render: (h) => {
            const lexical = hitSignals(h).lexical;
            return (
              <span className="cell-stack">
                <Meter value={lexical ?? 0} max={1} label={fmtScore(lexical)} width={72} />
                <SemanticValue hit={h} />
              </span>
            );
          },
        },
        {
          key: "outcome",
          header: "Past outcome and fix",
          render: (h) => (
            <Claimed planted={isPlanted(h.episode)}>
              <span className="chip-row">
                <OutcomeChip outcome={h.outcome} />
                <YesNoChip value={h.episode.verified} yes="fix verified" no="fix not verified" />
              </span>
            </Claimed>
          ),
        },
        { key: "match", header: "Mechanism matches live evidence", render: (h) => <YesNoChip value={h.mechanism_match} /> },
        {
          key: "safe",
          header: "Safe to reuse",
          render: (h) => <YesNoChip value={h.safe_to_reuse} yes="YES" no="NO" />,
        },
        { key: "conditions", header: "Conditions (held / against reuse)", render: (h) => <Conditions hit={h} /> },
      ]}
    />
  );
}

function SearchSignals({ trace }: { trace: MemoryTrace }) {
  if (!trace.searched) return <span className="muted">no memory.search event</span>;
  if (trace.embedder === undefined) {
    return (
      <span>
        lexical word overlap <span className="muted">(semantic recall not recorded: the run predates M3)</span>
      </span>
    );
  }
  if (trace.embedder === null) {
    return (
      <span>
        lexical word overlap only <span className="muted">(this run did not embed its incident)</span>
      </span>
    );
  }
  return (
    <span>
      lexical word overlap (the similarity routing uses), plus semantic recall by <Mono>{trace.embedder}</Mono>{" "}
      <span className="muted">(it can only add candidates)</span>
    </span>
  );
}

function RestorePoints({ trace }: { trace: MemoryTrace }) {
  const latest = trace.checks > 1 ? ` (latest of ${trace.checks} live checks)` : "";
  if (trace.restore !== "named") return <span className="muted">{`${RESTORE_NOTES[trace.restore]}${latest}`}</span>;
  return (
    <span className="cell-stack">
      <span className="chip-row">
        {trace.restorePoints.map((version, i) => (
          <Chip key={`${version}-${i}`} mono>
            {version}
          </Chip>
        ))}
      </span>
      <span className="muted small">
        {`the versions today's evidence names as a rollback target: a remembered rollback is reusable only if it restores one${latest}`}
      </span>
    </span>
  );
}

/** How this run searched memory and what its latest live check found (memory.search, memory.assessment). */
function MemoryCheck({ record, trace }: { record: RunRecord; trace: MemoryTrace }) {
  const order = hitOrder(record.memory_hits);
  const readable = trace.readable.length ? trace.readable : (record.config.memory_read_partitions ?? []);
  const rows: [ReactNode, ReactNode][] = [
    [
      "Partitions read",
      readable.length ? (
        <span className="chip-row">
          {readable.map((p) => (
            <PartitionChip key={p} partition={p} />
          ))}
        </span>
      ) : (
        <span className="muted">not recorded</span>
      ),
    ],
    ["Found by", <SearchSignals trace={trace} />],
    ["Restore points", <RestorePoints trace={trace} />],
  ];
  // A replay reads memory as it was when its recording began (memory.search created_before).
  if (trace.asOf) {
    rows.splice(1, 0, [
      "Memory as of",
      <span>
        memory as of {fmtDateTime(trace.asOf)}{" "}
        <span className="muted small">(a replay reads only the episodes created before its recording began)</span>
      </span>,
    ]);
  }
  if (order) rows.push(["Order", HIT_ORDER_NOTES[order]]);
  return <KeyValues className="mem-check" rows={rows} />;
}

function RunHits({ runId, running }: { runId: string; running: boolean }) {
  const detail = usePolling(() => api.run(runId), (d) => (d?.status === "running" || running ? 2500 : null), [runId]);
  const events = usePolling(
    () => api.events(runId),
    (list) => (list?.some((e) => e.category === "run.summary") || !running ? null : 2500),
    [runId],
  );
  const { refresh: refreshEvents } = events;
  useEffect(() => {
    if (running) refreshEvents();
  }, [running, refreshEvents]);
  const trace = useMemo(() => (events.data ? memoryTrace(events.data) : null), [events.data]);

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
      ) : trace ? (
        <MemoryCheck record={record} trace={trace} />
      ) : events.error ? (
        <ErrorState error={events.error} title="Could not load this run's trace (memory search and checks)" />
      ) : (
        <Loading label="Loading the run's memory checks…" />
      )}
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
        subtitle="record.memory_hits — found by lexical similarity (plus semantic recall when the run embeds its incident), then re-checked against the run's verified live evidence"
      >
        <Notice tone="warn" icon="alert" title="Similar ≠ safe to reuse">
          A past episode is safe to reuse only if that run succeeded, the independent oracle verified its recovery, live evidence
          supports the same mechanism on the same service, and a remembered rollback restores a version today's evidence names
          (not today's bad release). A look-alike incident with a different root cause is shown as NO, so its old fix is never
          repeated blindly. Runs recorded before M3 were checked on outcome, verification and mechanism only.
        </Notice>
        <RunScope selection={selection} page="memory">
          {(runId, summary) => <RunHits runId={runId} running={!!summary && !isTerminal(summary.state)} />}
        </RunScope>
      </Card>
    </>
  );
}
