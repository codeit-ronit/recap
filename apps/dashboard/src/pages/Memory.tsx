import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import { Chip, HypothesisChip, Mono } from "../components/Badge";
import { Card, KeyValues, Meter, Notice, PageHeader } from "../components/Card";
import { DataSourceBadge, describeTarget, summarySource } from "../components/DataSourceBadge";
import { Icon } from "../components/Icon";
import { JsonView } from "../components/JsonView";
import { Pager, paginate } from "../components/Pager";
import { RunScope } from "../components/RunPicker";
import { EmptyView, PolledView } from "../components/StateViews";
import { StatusToken } from "../components/StatusToken";
import { DataTable } from "../components/Table";
import { UntrustedText } from "../components/Untrusted";
import type * as dto from "../dto";
import { fmtDateTime, fmtInt, fmtScore, isTerminal } from "../format";
import { hrefFor, usePolling, useRunSelection } from "../hooks";
import type { PageProps, Polled } from "../hooks";
import type { ExperienceEpisode, RunSummary } from "../types";
import {
  HIT_ORDER_NOTES,
  REUSE_RULE,
  REUSE_VIEWS,
  accessRules,
  actionWords,
  claimWords,
  decisionRows,
  diagnosisWords,
  episodeIndex,
  episodeOrigin,
  episodeOutcome,
  episodePartitionBadge,
  episodeSummary,
  episodeTarget,
  episodeTargetNote,
  episodesByTarget,
  groupEpisodes,
  hitOrder,
  isPlanted,
  legacyDecider,
  memoryIntro,
  partitionBadge,
  plantedNote,
  runHits,
  runMemoryFacts,
  sourceRun,
} from "../view/memory";
import type { ClaimRow, EpisodeSummary, FieldRow, PartitionGroup, RunHit, RunMemoryFacts, TrajectoryRow } from "../view/memory";
import "../styles/memory-m3.css";

/**
 * Experience Memory: what RECAP remembers (GET /memory, grouped by partition), the rules that
 * keep kinds of runs and kinds of target apart (GET /system features.memory_access), and for one
 * run what it read, what it found and RECAP's reuse verdict on each match (GET
 * /runs/{id}/retrieval).
 *
 * An episode is the record of one past run, written by RECAP's own extraction: it is shown as
 * that run's result, never as a general claim about a model. It names the target its run acted
 * on (the simulated shop or a live cluster), and that comes from the episode's own provenance,
 * never from this server's target: GET /memory lists every episode in the data folder. Alert
 * text and tool output inside an episode are untrusted data.
 */

export const NOT_RECORDED = "Not recorded";

/** How many episodes a partition shows per page. */
export const EPISODES_PER_PAGE = 10;

/** The run list the page checks source runs against (useRunSelection asks for 100). */
const RUN_LIST_LIMIT = 100;

export interface KnownRuns {
  ids: ReadonlySet<string>;
  /** False when the list may be cut off (it holds the limit): a missing run may still exist. */
  complete: boolean;
}

export function knownRuns(runs: readonly Pick<RunSummary, "run_id">[] | undefined): KnownRuns | null {
  if (!runs) return null;
  return { ids: new Set(runs.map((r) => r.run_id)), complete: runs.length < RUN_LIST_LIMIT };
}

function NotRecorded({ children = NOT_RECORDED }: { children?: ReactNode }) {
  return <span className="not-recorded">{children}</span>;
}

// --- small pieces ----------------------------------------------------------------------------

/** A red-team plant is a false memory planted on purpose: marked loudly wherever it appears. */
export function PlantChip() {
  return (
    <StatusToken
      tone="bad"
      icon="alert"
      size="sm"
      label="RED-TEAM PLANT"
      title="Planted on purpose by a red-team setup: not experience from any run RECAP recorded."
    />
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
        only what the plant claims; a run that finds it must reject it on its own evidence.
        <span className="mem-plant-note">
          <span className="muted small">provenance note (fixture text): </span>
          <UntrustedText text={note} author="the red-team fixture" quote={false} />
        </span>
      </Notice>
    </div>
  );
}

/** A partition a run read, in plain words (live = real-model runs). */
function PartitionChip({ partition }: { partition: string }) {
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

/** Whose decisions an episode records: the run-origin badge (with the model), never a guess. */
export function EpisodeSource({ episode }: { episode: ExperienceEpisode }) {
  if (isPlanted(episode)) {
    return <StatusToken tone="bad" icon="alert" size="sm" variant="outline" label="Planted" title="A red-team plant: no run made these decisions." />;
  }
  const origin = episodeOrigin(episode);
  if (origin) {
    const model = typeof episode.provenance.model === "string" ? episode.provenance.model : null;
    return <DataSourceBadge provider={{ origin, model }} size="sm" label="Decisions by" />;
  }
  // Before M3 an episode recorded only its provider name: say what is known, no badge.
  const legacy = legacyDecider(episode);
  return (
    <span className="cell-stack">
      <span className="small">{legacy.words}</span>
      {legacy.provider ? <Mono>{legacy.provider}</Mono> : null}
    </span>
  );
}

/**
 * The target an episode's run acted on, looking like a run's target in its data-source badge,
 * from the episode's own provenance (episodeTarget): never this server's target.
 */
export function EpisodeTargetToken({ episode }: { episode: ExperienceEpisode }) {
  const target = episodeTarget(episode);
  const note = episodeTargetNote(target);
  if (!target.recorded) return <StatusToken tone="neutral" icon="minus" size="sm" label="TARGET NOT RECORDED" title={note} />;
  const look = describeTarget({ is_simulated: target.simulated, kind: target.kind });
  return <StatusToken tone={look.tone} icon={look.icon} size="sm" label={look.label} title={note} />;
}

/** The run that wrote an episode: a link to its Trace Explorer when this server has it. */
export function SourceRunLink({ episode, known }: { episode: ExperienceEpisode; known: KnownRuns | null }) {
  const source = sourceRun(episode, known);
  if (source.state === "planted") return <span className="muted small">none: a plant has no recorded run</span>;
  if (!source.runId) return <NotRecorded />;
  if (source.state === "present") {
    return (
      <a className="mem-run-link" href={hrefFor("timeline", { run: source.runId })} title="Open this run in the Trace Explorer">
        <Mono nowrap>{source.runId}</Mono>
        <Icon name="chevronRight" size={12} />
      </a>
    );
  }
  return (
    <span className="cell-stack">
      <Mono nowrap>{source.runId}</Mono>
      <span className="muted small">
        {source.state === "absent" ? "not on this server" : known ? `not among the ${RUN_LIST_LIMIT} most recent runs` : "checking this server…"}
      </span>
    </span>
  );
}

/**
 * The result of the one run an episode records. A plant's result is only its claim: shown
 * neutral and labelled as claimed, never as a green success.
 */
function OutcomeToken({ outcome, verified, planted }: { outcome: string | null | undefined; verified: boolean | null | undefined; planted: boolean }) {
  const words = episodeOutcome({ outcome, verified });
  if (planted) {
    return <StatusToken tone="neutral" icon="alert" size="sm" label={`claimed: ${words.label}`} title="What the red-team plant claims; no run produced it" />;
  }
  const icon = words.tone === "ok" ? "check" : words.tone === "bad" ? "x" : words.tone === "warn" ? "alert" : "minus";
  return <StatusToken tone={words.tone} icon={icon} size="sm" label={words.label} title="The result of the one run this episode records, not a general claim" />;
}

// --- leakage protections ---------------------------------------------------------------------

const PROTECTIONS: readonly string[] = [
  "Scripted and real-model experience never mix: each kind of run reads and writes its own partitions.",
  "Each kind of target keeps its own experience: a run finds only episodes from runs on its own kind of target (the simulated shop, the Kind demo cluster or SREGym's cluster), whatever partition they are in and however alike they read.",
  "Evaluation runs read the train partition only, and scored test runs write nothing, so a test never sees its own answer.",
  "A replay reads memory as it was when its recording began and writes nothing: it is not new experience.",
  "Similar is not safe: a remembered fix is reused only when the new run's own evidence passes every reuse condition.",
];

function PartitionTiles({ groups, loaded }: { groups: PartitionGroup[]; loaded: boolean }) {
  return (
    <ul className="mem-partitions" aria-label="Memory partitions">
      {groups.map((group) => (
        <li key={group.partition} className={`mem-partition tone-${group.tone}`}>
          <div className="mem-partition-head">
            <span className="mem-partition-name">{group.partition}</span>
            <span className="mem-partition-count">
              {loaded ? `${fmtInt(group.episodes.length)} ${group.episodes.length === 1 ? "episode" : "episodes"}` : "…"}
            </span>
          </div>
          <div className="mem-partition-words">{group.words}</div>
          <p className="mem-partition-line">{group.line}</p>
          {group.plants ? (
            <span className="mem-partition-plant">
              <Icon name="alert" size={12} /> {group.plants} red-team {group.plants === 1 ? "plant" : "plants"}
            </span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

/** Who reads and writes which partition: GET /system's rules, then the evaluation's fixed ones. */
export function AccessTable({ system }: { system: dto.SystemView | undefined }) {
  const rules = accessRules(system?.features?.memory_access);
  const fromServer = rules.some((rule) => rule.source === "server");
  return (
    <div className="table-wrap">
      <table className="table compact mem-access">
        <caption className="sr-only">Which partitions each kind of run reads and writes</caption>
        <thead>
          <tr>
            <th scope="col">Kind of run</th>
            <th scope="col">Reads</th>
            <th scope="col">Writes</th>
          </tr>
        </thead>
        <tbody>
          {fromServer ? null : (
            <tr>
              <td colSpan={3}>
                <NotRecorded>Not reported by this server (GET /system has no memory access rules)</NotRecorded>
              </td>
            </tr>
          )}
          {rules.map((rule) => (
            <tr key={rule.key}>
              <th scope="row" className="mem-access-who">
                {rule.who}
                {rule.source === "code" ? <span className="muted small"> · fixed in code</span> : null}
              </th>
              <td>
                <span className="chip-row">
                  {rule.read.length ? rule.read.map((p) => <Chip key={p} mono>{p}</Chip>) : <span className="muted">nothing</span>}
                </span>
                {rule.note ? <div className="muted small">{rule.note}</div> : null}
              </td>
              <td>{rule.write ? <Chip mono>{rule.write}</Chip> : <span className="mem-never">never writes</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function MemoryGuards({
  system,
  episodes,
}: {
  system: Pick<Polled<dto.SystemView>, "data" | "error" | "loading" | "refresh">;
  episodes: ExperienceEpisode[] | undefined;
}) {
  const groups = groupEpisodes(episodes ?? [], system.data?.features?.memory_partitions);
  return (
    <Card
      title="How memory is kept apart"
      subtitle="Partitions stop experience leaking between kinds of runs. The read and write rules come from GET /system; the evaluation's are fixed in code (ADR 0023)."
    >
      <div className="mem-guards">
        <PartitionTiles groups={groups} loaded={episodes !== undefined} />
        <div className="mem-guards-side">
          <h3 className="mini-title">Who reads and writes</h3>
          <PolledView polled={system} loadingLabel="Loading the access rules…" errorTitle="Could not load GET /system">
            {(data) => <AccessTable system={data} />}
          </PolledView>
          <ul className="mem-protections">
            {PROTECTIONS.map((line) => (
              <li key={line}>
                <Icon name="shield" size={14} />
                <span>{line}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </Card>
  );
}

// --- one episode, expanded ----------------------------------------------------------------------

function FieldValue({ row }: { row: FieldRow }) {
  if (row.state === "value") return <Mono>{row.value}</Mono>;
  return row.state === "none" ? <span className="muted">none</span> : <NotRecorded />;
}

/** Who made the past run's decisions, and with what (consolidator-v2 provenance). */
function DecisionProvenance({ episode }: { episode: ExperienceEpisode }) {
  const origin = episodeOrigin(episode);
  const rows = decisionRows(episode).map((row): [ReactNode, ReactNode] => {
    if (row.key === "origin" && row.state === "value" && origin) {
      return [row.label, <DataSourceBadge provider={{ origin }} size="sm" label="Decisions by" />];
    }
    // The scripted policy has no model: say so, so its episodes never read as model reasoning.
    if (row.key === "model" && row.state === "none" && origin === "scripted") {
      return [row.label, <span className="muted">none: the scripted policy is not an LLM</span>];
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
            <th scope="col" className="num">
              #
            </th>
            <th scope="col">Agent</th>
            <th scope="col">Call</th>
            <th scope="col">Evidence</th>
          </tr>
        </thead>
        <tbody>
          {steps.map((step, i) => (
            <tr key={`${step.evidenceId}-${i}`}>
              <td className="num">{i + 1}</td>
              <td>{step.agent ? <Mono>{step.agent}</Mono> : <span className="muted">none</span>}</td>
              <td>
                <span className="chip-row">
                  <Mono>{step.call || "none"}</Mono>
                  {step.kind === "doc" ? (
                    <Chip title="DOC evidence: a runbook search receipt, which the verifier never counts">runbook search</Chip>
                  ) : step.kind !== "tool" ? (
                    <Chip>{step.kind}</Chip>
                  ) : null}
                </span>
              </td>
              <td>
                <Mono nowrap>{step.evidenceId || "none"}</Mono>
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
            <span>{claimWords(claim)}</span>
          </div>
          {claim.detail && !claim.detailChecked ? (
            // Not shaped like a version or a service: the claim's own text, shown as data.
            <div className="chip-row small">
              <span className="muted">judged as</span>
              <UntrustedText text={claim.detail} author="the claim's agent" />
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
              <span className="muted">not recorded</span>
            )}
          </div>
          <div className="chip-row small">
            <span className="muted">rules</span>
            {claim.ruleIds.length ? claim.ruleIds.map((id, j) => <Mono key={`${id}-${j}`}>{id}</Mono>) : <span className="muted">none</span>}
          </div>
          {claim.failed.length ? (
            <ul className="rule-list">
              {claim.failed.map((line, j) => (
                <li key={j} className="text-bad">
                  <Icon name="x" size={13} /> {line}
                  {/* RECAP's sentence, repeating the release the claim named in free text. */}
                  {claim.failedQuotesClaim[j] ? (
                    <span
                      className="untrusted-tag mem-rule-tag"
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

/** consolidator-v2: what the past run observed and how its claims were judged. */
function EpisodeSummaryView({ episode }: { episode: ExperienceEpisode }) {
  const summary = episodeSummary(episode);
  const content = summary.trajectory.length + summary.claims.length + summary.basis.length > 0;
  if (summary.source === "planted" && !content) {
    return (
      <div className="mem-summary muted small">
        No run summary (trajectory, checked claims, diagnosis basis): a planted episode has no recorded run behind it.
      </div>
    );
  }
  if (summary.source === "not_recorded") {
    return (
      <div className="mem-summary muted small">
        Run summary (trajectory, checked claims, diagnosis basis): not recorded
        {summary.createdBy ? (
          <>
            {" "}
            (episode written by <Mono>{summary.createdBy}</Mono>, before consolidator-v2).
          </>
        ) : (
          "."
        )}
      </div>
    );
  }
  // Empty lists mean "none" only when RECAP extracted them; a plant's are just missing.
  const extracted = summary.source === "extracted";
  const none = (what: string) => (extracted ? `none: ${what}` : "none recorded");
  return (
    <div className="mem-summary">
      <div className="mem-title-row">
        <h4 className="mini-title">Run summary</h4>
        {extracted ? <MethodLabel method={summary.method} /> : <Chip tone="bad">written by the plant, not extracted by RECAP</Chip>}
      </div>
      <div className="mem-summary-grid">
        <div>
          <h4 className="mini-title">Trajectory: what the run observed</h4>
          <Trajectory steps={summary.trajectory} empty={none("the run recorded no tool results or runbook searches")} />
        </div>
        <div>
          <h4 className="mini-title">Diagnosis basis</h4>
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
          <h4 className="mini-title">Checked claims</h4>
          <CheckedClaims claims={summary.claims} empty={none("no claim was checked")} />
        </div>
      </div>
    </div>
  );
}

/** The general reuse rule, then this episode's own fix preconditions. */
export function ReuseConditions({ episode }: { episode: ExperienceEpisode }) {
  const summary = episodeSummary(episode);
  return (
    <div className="mem-conditions-box">
      <h4 className="mini-title">Conditions for safe reuse</h4>
      <ul className="mem-rule">
        {REUSE_RULE.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
      <div className="mem-preconditions">
        <span className="muted small">This fix applies only when</span>
        {summary.preconditions.length ? (
          <span className="chip-row">
            {summary.preconditions.map(([key, value]) => (
              <Chip key={key} mono>
                {key}={value || "none"}
              </Chip>
            ))}
          </span>
        ) : summary.source === "extracted" ? (
          <span className="muted small">none: the run had no verified fix to reuse</span>
        ) : (
          <NotRecorded>{summary.source === "planted" ? "none recorded (a plant)" : NOT_RECORDED}</NotRecorded>
        )}
      </div>
    </div>
  );
}

export function EpisodeDetail({ episode, known }: { episode: ExperienceEpisode; known: KnownRuns | null }) {
  const planted = plantedNote(episode);
  const diagnosis = diagnosisWords(episode);
  const fix = actionWords(episode.successful_action);
  return (
    <div className="stack">
      {planted !== null ? <PlantNotice note={planted} /> : null}
      <div className="detail-grid">
        <div className="mem-detail-main">
          <KeyValues
            className="kv-wide"
            rows={[
              ["Diagnosis", diagnosis ? <Claimed planted={planted !== null}>{diagnosis}</Claimed> : <span className="muted">no verified diagnosis</span>],
              ["Fix that ran", fix ? <Claimed planted={planted !== null}><Mono>{fix}</Mono></Claimed> : <span className="muted">no fix applied</span>],
              ["Result", <OutcomeToken outcome={episode.outcome} verified={episode.verified} planted={planted !== null} />],
              ["Judged by", episode.verification_source ? <Mono>{episode.verification_source}</Mono> : <span className="muted">no oracle verdict</span>],
            ]}
          />
          <ReuseConditions episode={episode} />
          <div>
            <div className="mem-title-row">
              <h4 className="mini-title">Symptoms</h4>
              <span className="untrusted-tag">untrusted · the alert's words</span>
            </div>
            {episode.symptoms.length ? (
              <ul className="plain-list">
                {episode.symptoms.map((s, i) => (
                  <li key={i}>
                    <UntrustedText text={s} author="the alert" quote={false} />
                  </li>
                ))}
              </ul>
            ) : (
              <span className="muted">none recorded</span>
            )}
          </div>
          <div>
            <div className="mem-title-row">
              <h4 className="mini-title">Evidence behind the diagnosis</h4>
              <span className="untrusted-tag">untrusted · tool output</span>
            </div>
            {episode.verified_evidence.length ? (
              <ul className="plain-list">
                {episode.verified_evidence.map((ev) => (
                  <li key={ev.evidence_id}>
                    <Mono>{ev.evidence_id}</Mono> <UntrustedText text={ev.summary} author="an MCP tool, in the past run" quote={false} />
                  </li>
                ))}
              </ul>
            ) : (
              <span className="muted">{planted !== null ? "none: the plant cites no evidence" : "none (no supported diagnosis)"}</span>
            )}
          </div>
          {episode.unsuccessful_actions.length ? (
            <div>
              <h4 className="mini-title">Unsuccessful actions (kept as cautionary history)</h4>
              <ul className="plain-list">
                {episode.unsuccessful_actions.map((a, i) => (
                  <li key={i}>
                    <Mono>{actionWords(a) ?? "unknown action"}</Mono> <span className="muted small">{a.result}</span>
                    {a.note ? <span className="muted small"> · {a.note}</span> : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
        <div className="mem-side">
          <h4 className="mini-title">Who made the decisions</h4>
          <DecisionProvenance episode={episode} />
          <h4 className="mini-title">Where its run acted</h4>
          <span className="cell-stack">
            <EpisodeTargetToken episode={episode} />
            <span className="muted small">{episodeTargetNote(episodeTarget(episode))}</span>
          </span>
          <h4 className="mini-title">Source run</h4>
          <SourceRunLink episode={episode} known={known} />
          <details className="mem-raw">
            <summary>Raw provenance (as stored)</summary>
            <JsonView value={episode.provenance} maxHeight={220} />
          </details>
        </div>
      </div>
      <EpisodeSummaryView episode={episode} />
    </div>
  );
}

// --- episodes grouped by partition ------------------------------------------------------------------

function EpisodeTable({ episodes, known }: { episodes: ExperienceEpisode[]; known: KnownRuns | null }) {
  return (
    <DataTable
      className="mem-episodes"
      rows={episodes}
      rowKey={(e) => e.episode_id}
      expand={(e) => <EpisodeDetail episode={e} known={known} />}
      rowClassName={(e) => (isPlanted(e) ? "row-bad" : undefined)}
      columns={[
        {
          key: "id",
          header: "Episode",
          render: (e) => (
            <span className="cell-stack">
              <Mono nowrap>{e.episode_id}</Mono>
              <span className="muted small nowrap">{fmtDateTime(e.created_at)}</span>
              {isPlanted(e) ? <PlantChip /> : null}
            </span>
          ),
        },
        {
          key: "incident",
          header: "Incident and target",
          render: (e) => (
            <span className="cell-stack">
              <Mono>{e.scenario_id}</Mono>
              <span className="muted small">alert on {e.service || "an unnamed service"}</span>
              <EpisodeTargetToken episode={e} />
            </span>
          ),
        },
        {
          key: "found",
          header: "What that run found and did",
          render: (e) => {
            const diagnosis = diagnosisWords(e);
            const fix = actionWords(e.successful_action);
            return (
              <Claimed planted={isPlanted(e)}>
                <span className="cell-stack">
                  <span>{diagnosis ?? <span className="muted">no verified diagnosis</span>}</span>
                  {fix ? <Mono>{fix}</Mono> : <span className="muted small">no fix applied</span>}
                  {e.unsuccessful_actions.length ? (
                    <span className="text-warn small">
                      {e.unsuccessful_actions.length} unsuccessful {e.unsuccessful_actions.length === 1 ? "action" : "actions"}
                    </span>
                  ) : null}
                </span>
              </Claimed>
            );
          },
        },
        {
          key: "result",
          header: "That run's result",
          render: (e) => <OutcomeToken outcome={e.outcome} verified={e.verified} planted={isPlanted(e)} />,
        },
        { key: "source", header: "Decisions by", render: (e) => <EpisodeSource episode={e} /> },
        { key: "run", header: "Source run", render: (e) => <SourceRunLink episode={e} known={known} /> },
      ]}
    />
  );
}

function PartitionSection({ group, known }: { group: PartitionGroup; known: KnownRuns | null }) {
  const [offset, setOffset] = useState(0);
  const titleId = `mem-group-${group.partition.replace(/[^a-z0-9-]/gi, "-")}`;
  const page = paginate(group.episodes, offset, EPISODES_PER_PAGE);
  return (
    <section className="mem-group" aria-labelledby={titleId}>
      <div className="mem-group-head">
        <h3 className="mem-group-title" id={titleId}>
          <span className="mono">{group.partition}</span>
          <span className="mem-group-words"> · {group.words}</span>
        </h3>
        <span className="mem-group-count">
          {fmtInt(group.episodes.length)} {group.episodes.length === 1 ? "episode" : "episodes"}
          {group.plants ? ` · ${group.plants} red-team ${group.plants === 1 ? "plant" : "plants"}` : ""}
        </span>
      </div>
      <p className="mem-group-line">{group.line}</p>
      {group.episodes.length ? (
        <>
          <EpisodeTable episodes={page} known={known} />
          {group.episodes.length > EPISODES_PER_PAGE ? (
            <Pager offset={offset} limit={EPISODES_PER_PAGE} total={group.episodes.length} onChange={setOffset} noun="episodes" label={`${group.partition} episodes pages`} />
          ) : null}
        </>
      ) : (
        <div className="mem-group-empty">{group.empty}</div>
      )}
    </section>
  );
}

export function EpisodeGroups({
  episodes,
  partitions,
  known,
}: {
  episodes: ExperienceEpisode[];
  partitions?: readonly string[] | null;
  known: KnownRuns | null;
}) {
  const groups = groupEpisodes(episodes, partitions);
  const plants = episodes.filter(isPlanted).length;
  const targets = episodesByTarget(episodes);
  return (
    <div className="mem-groups">
      {targets.length ? (
        <p className="mem-targets">
          Filed by target: {targets.map((t) => `${fmtInt(t.count)} under ${t.words}`).join(", ")}. A run finds only the episodes filed
          under its own kind of target.
        </p>
      ) : null}
      {plants ? (
        <div className="mem-plant">
          <Notice tone="bad" icon="alert" title={`${plants} red-team ${plants === 1 ? "plant" : "plants"} in memory`}>
            A planted episode is a false memory put here on purpose, to show that RECAP rejects it on live evidence. It is not
            experience: its diagnosis, outcome and verification are only what the plant claims.
          </Notice>
        </div>
      ) : null}
      {groups.map((group) => (
        <PartitionSection key={group.partition} group={group} known={known} />
      ))}
    </div>
  );
}

function EpisodesCard({
  episodes,
  partitions,
  known,
}: {
  episodes: Polled<ExperienceEpisode[]>;
  partitions?: readonly string[] | null;
  known: KnownRuns | null;
}) {
  return (
    <Card
      title={`Episodes in memory${episodes.data ? ` (${fmtInt(episodes.data.length)})` : ""}`}
      subtitle="GET /memory, grouped by partition. Each episode is RECAP's own record of what one past run did, and names the target that run acted on; no model wrote it. Expand a row for its evidence and reuse conditions."
    >
      <PolledView
        polled={episodes}
        loadingLabel="Loading episodes…"
        errorTitle="Could not load GET /memory"
        emptyTitle="No episodes yet"
        emptyIcon="memory"
        empty={(list) =>
          list.length
            ? null
            : "Every finished run that may write adds one episode. Replays and held-out test scenarios never write, so they add none."
        }
      >
        {(list) => <EpisodeGroups episodes={list} partitions={partitions} known={known} />}
      </PolledView>
    </Card>
  );
}

// --- one run's memory -------------------------------------------------------------------------------

export interface RunMemoryData {
  detail: dto.RunDetail;
  retrieval: dto.RetrievalView;
}

async function loadRunMemory(runId: string): Promise<RunMemoryData> {
  const [detail, retrieval] = await Promise.all([api.runView(runId), api.retrievalView(runId)]);
  return { detail, retrieval };
}

/** The run's data source from its summary (with the record's red-team flag), or from its record. */
function RunSource({ detail }: { detail: dto.RunDetail }) {
  const record = detail.record;
  if (detail.summary) {
    const source = summarySource(detail.summary);
    return (
      <DataSourceBadge
        provider={{ ...source.provider, red_team: record?.provider_info?.red_team === true }}
        environment={source.environment}
        label="This run's data source"
      />
    );
  }
  return <DataSourceBadge provider={record?.provider_info ?? null} environment={record?.environment ?? null} label="This run's data source" />;
}

const WRITE_WORDS: Record<RunMemoryFacts["write"]["state"], string> = {
  written: "Wrote one episode",
  skipped: "Wrote nothing",
  disabled: "Wrote nothing: this run does not write memory",
  pending: "Not yet: an episode is written when the run ends",
  not_recorded: NOT_RECORDED,
};

export function MemoryFacts({ facts, record }: { facts: RunMemoryFacts; record: dto.RunDetail["record"] }) {
  const order = record ? hitOrder(record.memory_hits) : null;
  const rows: [ReactNode, ReactNode][] = [
    [
      "Memory",
      facts.enabled ? (
        <StatusToken tone="info" icon="memory" size="sm" label="On: read before the investigation" />
      ) : (
        <StatusToken tone="neutral" icon="minus" size="sm" label="Off: this run read no memory" />
      ),
    ],
    [
      "Partitions read",
      facts.read.length ? (
        <span className="chip-row">
          {facts.read.map((p) => (
            <PartitionChip key={p} partition={p} />
          ))}
        </span>
      ) : (
        <NotRecorded />
      ),
    ],
  ];
  if (facts.asOf) {
    rows.push([
      "Memory as of",
      <span className="cell-stack">
        <span>{fmtDateTime(facts.asOf)}</span>
        <span className="muted small">a replay reads only the episodes created before its recording began</span>
      </span>,
    ]);
  }
  if (facts.enabled) {
    rows.push([
      "Found by",
      facts.semantic ? (
        <span>
          lexical word overlap, plus semantic recall by <Mono>{facts.semantic}</Mono>{" "}
          <span className="muted small">(semantic recall can add a candidate, never decide reuse)</span>
        </span>
      ) : (
        <span>
          lexical word overlap <span className="muted small">(no semantic search recorded)</span>
        </span>
      ),
    ]);
    rows.push([
      "Live checks",
      facts.checks ? (
        <span>
          {fmtInt(facts.checks)} {facts.checks === 1 ? "check" : "checks"} against this run's evidence
          {order ? <span className="muted small"> · {HIT_ORDER_NOTES[order]}</span> : null}
        </span>
      ) : (
        <span className="muted">none recorded</span>
      ),
    ]);
  }
  rows.push([
    "Wrote",
    facts.write.state === "written" ? (
      <span>
        episode <Mono>{facts.write.episodeId}</Mono>
        {facts.write.partition ? (
          <>
            {" "}
            to <PartitionChip partition={facts.write.partition} />
          </>
        ) : null}
      </span>
    ) : (
      <span className="cell-stack">
        <span>{WRITE_WORDS[facts.write.state]}</span>
        {facts.write.reasons.map((reason) => (
          <span key={reason} className="muted small">
            RECAP: {reason}
          </span>
        ))}
      </span>
    ),
  ]);
  return <KeyValues className="mem-facts" rows={rows} />;
}

function SignalLine({ hit }: { hit: RunHit }) {
  return (
    <div className="mem-signals">
      <span className="mem-signal">
        <span className="muted small">lexical</span>
        {hit.lexical !== null ? <Meter value={hit.lexical} max={1} label={fmtScore(hit.lexical)} width={64} /> : <NotRecorded />}
      </span>
      <span className="mem-signal">
        <span className="muted small">semantic</span>
        {hit.semantic !== null ? (
          <Meter value={hit.semantic} max={1} label={fmtScore(hit.semantic)} width={64} />
        ) : hit.signalsRecorded ? (
          <span
            className="muted small"
            title="No semantic signal: this run did not embed its incident, or the episode was not embedded by the same model."
          >
            none
          </span>
        ) : (
          <NotRecorded />
        )}
      </span>
      {hit.otherSignals.map(([key, value]) => (
        <span key={key} className="mem-signal">
          <span className="muted small">{key}</span> <Mono>{value}</Mono>
        </span>
      ))}
    </div>
  );
}

function ConditionList({ held, against }: { held: string[]; against: string[] }) {
  return (
    <ul className="rule-list mem-cond-list">
      {held.map((line, i) => (
        <li key={`h${i}`} className="text-ok">
          <Icon name="check" size={13} />
          <span>
            <span className="sr-only">Held: </span>
            {line}
          </span>
        </li>
      ))}
      {against.map((line, i) => (
        <li key={`a${i}`} className="text-bad">
          <Icon name="x" size={13} />
          <span>
            <span className="sr-only">Against reuse: </span>
            {line}
          </span>
        </li>
      ))}
    </ul>
  );
}

function roundLabel(round: number | null): string {
  return round === null ? "outside a round" : `round ${round}`;
}

/** "bad deployment on payment · alert on checkout": the episode's diagnosis when it is known. */
function hitDiagnosis(hit: RunHit): string {
  const diagnosis = hit.episode ? diagnosisWords(hit.episode) : hit.mechanism ? hit.mechanism.replace(/_/g, " ") : null;
  const alert = hit.service ? `alert on ${hit.service}` : null;
  if (!diagnosis) return alert ? `no verified diagnosis · ${alert}` : "no verified diagnosis";
  return alert ? `${diagnosis} · ${alert}` : diagnosis;
}

/** One memory hit: how it was found, the past run it records, and RECAP's reuse verdict. */
export function HitCard({ hit, known }: { hit: RunHit; known: KnownRuns | null }) {
  const view = REUSE_VIEWS[hit.reuse];
  const latest = hit.checks.length ? hit.checks[hit.checks.length - 1] : null;
  const earlier = hit.checks.slice(0, -1);
  const partition = hit.episode ? (
    <EpisodePartitionChip episode={hit.episode} />
  ) : hit.partition ? (
    <PartitionChip partition={hit.partition} />
  ) : null;
  return (
    <article className={`mem-hit tone-${view.tone}${hit.planted ? " mem-hit-planted" : ""}`} aria-label={`Memory hit ${hit.episodeId}: ${view.label}`}>
      <header className="mem-hit-head">
        <StatusToken tone={view.tone} icon={view.icon} label={view.label} title={view.meaning} />
        <h3 className="mem-hit-title">
          <Mono>{hit.episodeId}</Mono>
        </h3>
        {partition}
        {hit.planted ? <PlantChip /> : null}
      </header>
      <div className="mem-hit-body">
        <div className="mem-hit-col">
          <h4 className="mini-title">Found by</h4>
          <SignalLine hit={hit} />
          <h4 className="mini-title">The past run it records</h4>
          <div className="cell-stack">
            <Claimed planted={hit.planted}>
              <span>{hitDiagnosis(hit)}</span>
            </Claimed>
            {hit.episode && actionWords(hit.episode.successful_action) ? <Mono>{actionWords(hit.episode.successful_action)}</Mono> : null}
            {hit.outcome ? <OutcomeToken outcome={hit.outcome} verified={hit.verified} planted={hit.planted} /> : null}
            {hit.episode ? <EpisodeSource episode={hit.episode} /> : <span className="muted small">episode not in memory now</span>}
            {hit.episode ? <SourceRunLink episode={hit.episode} known={known} /> : null}
          </div>
        </div>
        <div className="mem-hit-col">
          <h4 className="mini-title">
            RECAP's reuse check
            {latest ? <span className="mem-hit-round"> · {roundLabel(latest.round)}{hit.checks.length > 1 ? `, latest of ${hit.checks.length}` : ""}</span> : null}
          </h4>
          {hit.reuse === "not_checked" ? (
            <p className="muted mem-hit-note">
              No live check covered this hit: RECAP checks hits after each verification of the run's evidence, and none followed this
              search. It was never treated as safe to reuse.
            </p>
          ) : hit.held.length + hit.against.length ? (
            <ConditionList held={hit.held} against={hit.against} />
          ) : (
            <p className="reason">{hit.reason ?? NOT_RECORDED}</p>
          )}
          {hit.mechanismMatch !== null ? (
            <div className="small">
              Mechanism matches this run's evidence:{" "}
              <StatusToken tone={hit.mechanismMatch ? "ok" : "bad"} size="sm" variant="plain" label={hit.mechanismMatch ? "yes" : "no"} />
            </div>
          ) : null}
          {latest && latest.restorePoints !== null ? (
            <div className="small">
              Restore points today's evidence names:{" "}
              {latest.restorePoints.length ? latest.restorePoints.map((v) => <Chip key={v} mono>{v}</Chip>) : <span className="muted">none</span>}
            </div>
          ) : null}
          {earlier.length ? (
            <details className="mem-history">
              <summary>Earlier checks ({earlier.length})</summary>
              <ol className="mem-history-list">
                {earlier.map((check) => (
                  <li key={check.seq}>
                    <span className="chip-row">
                      <span className="muted small">{roundLabel(check.round)}</span>
                      <StatusToken
                        tone={check.safe ? "ok" : "bad"}
                        icon={check.safe ? "check" : "x"}
                        size="sm"
                        label={check.safe ? "safe to reuse" : "rejected"}
                      />
                    </span>
                    {check.held.length + check.against.length ? <ConditionList held={check.held} against={check.against} /> : <span className="reason">{check.reason ?? NOT_RECORDED}</span>}
                  </li>
                ))}
              </ol>
            </details>
          ) : null}
        </div>
      </div>
      {hit.episode ? (
        <details className="mem-hit-episode">
          <summary>The remembered episode</summary>
          <EpisodeDetail episode={hit.episode} known={known} />
        </details>
      ) : null}
    </article>
  );
}

export function RunHitsList({ hits, facts, known }: { hits: RunHit[]; facts: RunMemoryFacts; known: KnownRuns | null }) {
  if (!facts.enabled) {
    return <EmptyView title="Memory was off for this run" reason="It searched no memory, so there are no hits to judge (use_memory = false)." icon="memory" />;
  }
  if (!hits.length) {
    const read = facts.read.length ? facts.read.join(" + ") : "its partitions";
    return (
      <EmptyView
        title="No similar episode found"
        reason={
          facts.searches
            ? `The search of ${read}${facts.asOf ? ", as of the recording's start," : ""} returned nothing similar enough.`
            : "No memory search is recorded for this run yet."
        }
        icon="memory"
      />
    );
  }
  return (
    <div className="mem-hits">
      {hits.map((hit) => (
        <HitCard key={hit.episodeId} hit={hit} known={known} />
      ))}
    </div>
  );
}

export function RunMemoryView({
  data,
  episodes,
  known,
}: {
  data: RunMemoryData;
  episodes: ExperienceEpisode[] | undefined;
  known: KnownRuns | null;
}) {
  const { detail, retrieval } = data;
  const record = detail.record;
  const terminal = detail.summary ? detail.summary.terminal : record ? isTerminal(record.state) : false;
  const facts = runMemoryFacts(retrieval.memory, terminal);
  const index = episodeIndex(record?.memory_hits.map((h) => h.episode), episodes);
  const hits = runHits(retrieval.memory, index);
  const counts = { safe: 0, rejected: 0, not_checked: 0 };
  for (const hit of hits) counts[hit.reuse] += 1;
  return (
    <div className="stack">
      <div className="mem-run-source">
        <RunSource detail={detail} />
      </div>
      {!record ? (
        <EmptyView title="No checkpoint yet" reason="The run has just started; its memory search appears within a few seconds." icon="clock" />
      ) : (
        <>
          <Card title="What this run read and wrote" subtitle="GET /runs/{id}/retrieval: memory is read before the investigation starts; a replay reads it as its recording found it.">
            <MemoryFacts facts={facts} record={record} />
            {retrieval.trace_available ? null : (
              <Notice tone="warn" icon="alert" title="Trace not available">
                This run's stored events are missing, so its searches and live checks are not recorded; the hits below come from its
                record.
              </Notice>
            )}
          </Card>
          <Card
            title={`Memory hits and reuse verdicts${hits.length ? ` (${hits.length})` : ""}`}
            subtitle="Found by word overlap (plus semantic recall when the run embedded its incident), then checked against this run's own evidence after each verification."
            actions={
              hits.length ? (
                <span className="chip-row" role="group" aria-label="Reuse verdict counts">
                  <StatusToken tone={counts.safe ? "ok" : "neutral"} icon="check" size="sm" label={`${counts.safe} safe to reuse`} />
                  <StatusToken tone={counts.rejected ? "bad" : "neutral"} icon="x" size="sm" label={`${counts.rejected} rejected`} />
                  {counts.not_checked ? <StatusToken tone="neutral" size="sm" label={`${counts.not_checked} not checked`} /> : null}
                </span>
              ) : null
            }
          >
            {hits.length ? (
              <Notice tone="warn" icon="alert" title="Similar is not safe to reuse">
                A past episode can match today's alert word for word and still be rejected: only this run's own evidence decides. The
                reasons below are RECAP's reuse check, in its own words.
              </Notice>
            ) : null}
            <RunHitsList hits={hits} facts={facts} known={known} />
          </Card>
        </>
      )}
    </div>
  );
}

function RunMemory({
  runId,
  running,
  episodes,
  known,
}: {
  runId: string;
  running: boolean;
  episodes: ExperienceEpisode[] | undefined;
  known: KnownRuns | null;
}) {
  const data = usePolling(() => loadRunMemory(runId), (d) => (running || d?.detail.status === "running" ? 3000 : null), [runId]);
  const { refresh } = data;
  // When the run ends, load its final memory verdicts once more.
  const wasRunning = useRef(running);
  useEffect(() => {
    if (wasRunning.current && !running) refresh();
    wasRunning.current = running;
  }, [running, refresh]);
  return (
    <PolledView polled={data} loadingLabel="Loading this run's memory…" errorTitle="Could not load this run's memory">
      {(d) => <RunMemoryView data={d} episodes={episodes} known={known} />}
    </PolledView>
  );
}

// --- the page ------------------------------------------------------------------------------------------

export function MemoryPage({ route, system: shell }: PageProps) {
  const selection = useRunSelection(route);
  const system = usePolling(() => api.systemView(), 30000, []);
  const episodes = usePolling(() => api.memory(), 10000, []);
  const known = useMemo(() => knownRuns(selection.runs.data), [selection.runs.data]);
  return (
    <>
      <PageHeader title="Experience Memory" description={memoryIntro(shell.data?.environment ?? system.data?.environment)} />
      <MemoryGuards system={system} episodes={episodes.data} />
      <EpisodesCard episodes={episodes} partitions={system.data?.features?.memory_partitions} known={known} />
      <div className="section-head">
        <h2 className="section-title">Memory in one run</h2>
        <p className="section-lead">What a run searched, what it found, and why each match was reused or rejected.</p>
      </div>
      <RunScope selection={selection} page="memory">
        {(runId, summary) => (
          <RunMemory runId={runId} running={!!summary && !isTerminal(summary.state)} episodes={episodes.data} known={known} />
        )}
      </RunScope>
    </>
  );
}
