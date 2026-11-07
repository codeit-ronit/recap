import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { api, toApiError } from "../api";
import type { ApiError } from "../api";
import { Chip, Mono, StateChip } from "../components/Badge";
import { Card, KeyValues, Notice, PageHeader, StatTile } from "../components/Card";
import { DataSourceBadge } from "../components/DataSourceBadge";
import type { SourceFields } from "../components/DataSourceBadge";
import { applyFilters, FilterBar, filterOptions } from "../components/FilterBar";
import type { FilterSpec, FilterValues } from "../components/FilterBar";
import { Icon } from "../components/Icon";
import type { IconName } from "../components/Icon";
import { JsonView } from "../components/JsonView";
import { LogView } from "../components/LogView";
import { Modal } from "../components/Modal";
import { Pager, pageWindow } from "../components/Pager";
import { EmptyView, ErrorView, LoadingView, PolledView } from "../components/StateViews";
import { StatusToken } from "../components/StatusToken";
import { DataTable } from "../components/Table";
import type * as dto from "../dto";
import type { Tone } from "../format";
import { hrefFor, navigate, paramsOf, useHashRoute, useOperatorToken, usePolling } from "../hooks";
import type { Params, Polled, Route } from "../hooks";
import type { EvalReport, EvalResults } from "../types";
import {
  CLI_COMMANDS,
  MODE_GUIDE,
  MODE_ORDER,
  NO_LABEL,
  NOT_RECORDED,
  batchTargets,
  batchesOf,
  benchmarkVerdictCounts,
  configurationMatrix,
  expectedEntries,
  failureGroups,
  fmtCount,
  fmtSeconds,
  fmtUtc,
  isRedTeam,
  kOfN,
  memoryHitFields,
  modeInfo,
  modeTabNote,
  nextMode,
  outcomeFromKey,
  outcomeKey,
  outcomeLabel,
  parseMode,
  parseTopology,
  pickBatch,
  plural,
  providerClasses,
  recordOutcome,
  recordTarget,
  recordVerdict,
  retrievalLabel,
  sampleNote,
  scenarioGroups,
  stageNote,
  stateCounts,
  targetFields,
  timeView,
  tokenView,
  toolCallRows,
} from "../view/evals";
import type { ConfigMatrix, EvalTarget, ScenarioGroup, VerdictView } from "../view/evals";
import "../styles/evaluations.css";

/**
 * Evaluations (GET /evals, GET /evals/{batch_id}; docs/evaluation.md). One tab per mode,
 * because the modes are different kinds of evidence and never share a number: a scripted
 * regression of RECAP's own code, offline replays of recorded real-model runs, real models
 * on simulated incidents, and real models on a real Kubernetes cluster (M5: the Kind demo
 * cluster or SREGym's own, told apart by each batch's target). Every count is over one batch,
 * written "k of N"; a benchmark grader's verdicts (SREGym's) are counted apart from RECAP's
 * verified recoveries. A record opens read-only, with a link to its trace when this server
 * stores the run. Only the scripted regression starts from the browser (operator, after a
 * confirmation); real-model batches are built from a terminal.
 */

// --- small pieces -------------------------------------------------------------------------------

export function NotRecorded({ children = NOT_RECORDED }: { children?: ReactNode }) {
  return <span className="not-recorded">{children}</span>;
}

/** What the runs worked on, in words: the simulated shop, or a real cluster (said in the text, not only the color). */
export function TargetChips({ targets }: { targets: readonly EvalTarget[] }) {
  return (
    <span className="chip-row evl-targets">
      {targets.map((target) => (
        <Chip key={target.key || "unnamed"} tone={target.simulated ? "warn" : "bad"} title={target.about}>
          <Icon name={target.simulated ? "flask" : "alert"} size={12} />
          {target.simulated ? target.label : `${target.label} · live`}
        </Chip>
      ))}
    </span>
  );
}

/** A benchmark grader's verdict: its own token, never drawn like RECAP's outcome. */
function VerdictToken({ view, prefix = "" }: { view: VerdictView; prefix?: string }) {
  return (
    <StatusToken
      size="sm"
      variant="plain"
      tone={view.tone}
      icon={view.icon}
      label={`${prefix}${prefix ? view.label.toLowerCase() : view.label}`}
      title="The benchmark grader's own verdict, recorded by hand: kept apart from RECAP's outcome"
    />
  );
}

function sourceOf(origin: string, model: string | null): SourceFields {
  return { origin: (origin || undefined) as SourceFields["origin"], model };
}

function isScripted(record: Pick<dto.EvalRecord, "provider_class">): boolean {
  return record.provider_class === "scripted";
}

function tabId(mode: dto.EvalMode): string {
  return `evl-tab-${mode}`;
}

function panelId(mode: dto.EvalMode): string {
  return `evl-panel-${mode}`;
}

function TextList({ items, empty, mono = false }: { items: readonly string[]; empty: ReactNode; mono?: boolean }) {
  if (!items.length) return <>{empty}</>;
  return (
    <ul className="evl-plain">
      {items.map((item, i) => (
        <li key={`${item}-${i}`}>{mono ? <Mono>{item}</Mono> : item}</li>
      ))}
    </ul>
  );
}

function ChipList({ items, empty }: { items: readonly string[]; empty: ReactNode }) {
  if (!items.length) return <>{empty}</>;
  return (
    <span className="chip-row">
      {items.map((item, i) => (
        <Chip key={`${item}-${i}`} mono>
          {item}
        </Chip>
      ))}
    </span>
  );
}

/** A run id as a button that opens its record. */
function RecordButton({ runId, onOpen, label }: { runId: string; onOpen: (runId: string) => void; label?: string }) {
  return (
    <button
      type="button"
      className="evl-linkbtn mono"
      aria-label={label ?? `Open the evaluation record of ${runId}`}
      onClick={(event) => {
        event.stopPropagation();
        onOpen(runId);
      }}
    >
      {runId}
    </button>
  );
}

function OutcomeToken({ record, size = "sm" }: { record: Pick<dto.EvalRecord, "success" | "failure">; size?: "sm" | "md" }) {
  return <OutcomeKeyToken outcome={outcomeKey(record)} size={size} />;
}

function OutcomeKeyToken({ outcome, size = "sm" }: { outcome: string; size?: "sm" | "md" }) {
  const view = outcomeFromKey(outcome);
  return <StatusToken size={size} tone={view.tone} icon={view.icon} label={view.label} title={view.meaning} />;
}

const LEGEND: { icon: IconName; tone: Tone; text: string }[] = [
  { icon: "check", tone: "ok", text: "verified recovery" },
  { icon: "x", tone: "bad", text: "did not recover (wrong answer, failed fix or error)" },
  { icon: "stop", tone: "warn", text: "stopped by a guard or a limit" },
];

function MarkLegend() {
  return (
    <ul className="evl-legend" aria-label="What the marks mean">
      {LEGEND.map((item) => (
        <li key={item.icon}>
          <span className={`evl-mark-static tone-${item.tone}`} aria-hidden="true">
            <Icon name={item.icon} size={12} />
          </span>
          {item.text}
        </li>
      ))}
    </ul>
  );
}

function RunMark({ record, onOpen }: { record: dto.EvalRecord; onOpen: (runId: string) => void }) {
  const outcome = recordOutcome(record);
  return (
    <button
      type="button"
      className={`evl-mark tone-${outcome.tone}`}
      aria-label={`${record.run_id} on ${record.scenario_id}: ${outcome.label}. Open the record`}
      title={`${record.run_id} · ${record.scenario_id} · ${outcome.label}`}
      onClick={() => onOpen(record.run_id)}
    >
      <Icon name={outcome.icon} size={14} />
    </button>
  );
}

function CopyButton({ text, what }: { text: string; what: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      setState("failed");
    }
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState("idle"), 2500);
  };
  return (
    <>
      <button type="button" className="btn btn-small evl-copy" aria-label={`Copy the command: ${what}`} onClick={() => void copy()}>
        {state === "copied" ? <Icon name="check" size={13} /> : null}
        {state === "copied" ? "Copied" : state === "failed" ? "Select and copy" : "Copy"}
      </button>
      <span className="sr-only" role="status">
        {state === "copied" ? "Command copied" : state === "failed" ? "Copying failed: select the command and copy it" : ""}
      </span>
    </>
  );
}

// --- tabs ---------------------------------------------------------------------------------------

export function ModeTabs({
  view,
  mode,
  onSelect,
}: {
  view: Pick<dto.EvalsView, "modes" | "batches">;
  mode: dto.EvalMode;
  onSelect: (mode: dto.EvalMode) => void;
}) {
  const refs = useRef<Partial<Record<dto.EvalMode, HTMLButtonElement | null>>>({});
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const next = nextMode(mode, event.key);
    if (!next) return;
    event.preventDefault();
    onSelect(next);
    refs.current[next]?.focus();
  };
  return (
    <div className="evl-tabs" role="tablist" aria-label="Evaluation modes">
      {MODE_ORDER.map((m) => {
        const selected = m === mode;
        const planned = modeInfo(view, m)?.status === "planned";
        return (
          <button
            key={m}
            ref={(element) => {
              refs.current[m] = element;
            }}
            type="button"
            role="tab"
            id={tabId(m)}
            aria-selected={selected}
            aria-controls={selected ? panelId(m) : undefined}
            tabIndex={selected ? 0 : -1}
            className={["evl-tab", selected ? "selected" : "", planned ? "planned" : ""].filter(Boolean).join(" ")}
            onClick={() => onSelect(m)}
            onKeyDown={onKeyDown}
          >
            <Icon name={MODE_GUIDE[m].icon} size={16} />
            <span className="evl-tab-text">
              <span className="evl-tab-label">{MODE_GUIDE[m].tab}</span>
              <span className="evl-tab-note">{modeTabNote(view, m)}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

// --- the mode card and its batches ---------------------------------------------------------------

function emptyBatchesReason(mode: dto.EvalMode): string {
  const where = "Nothing is published in evals/reports/batches/ or stored in this server's data folder.";
  switch (mode) {
    case "scripted_regression":
      return `${where} Start the scripted regression here (signed in), or run scripts/evaluate.py.`;
    case "offline_replay":
      return `${where} Replay the committed recordings with scripts/eval_replay.py (below).`;
    case "real_llm":
      return `${where} Real-model batches are made from a terminal with scripts/live_eval.py (below).`;
    case "real_kubernetes":
      return `${where} Real-cluster batches are built from a terminal with scripts/live_eval.py --from-data, from runs a person approved with scripts/demo.py --approve ask (below).`;
    default:
      return where;
  }
}

export function BatchPicker({
  mode,
  batches,
  selectedId,
  onSelect,
}: {
  mode: dto.EvalMode;
  batches: readonly dto.BatchSummary[];
  selectedId: string | null;
  onSelect: (batchId: string) => void;
}) {
  if (!batches.length) {
    return <EmptyView icon="evals" title={`No ${MODE_GUIDE[mode].tab.toLowerCase()} batch yet`} reason={emptyBatchesReason(mode)} />;
  }
  return (
    <div className="evl-batches">
      <h3 className="mini-title">
        {plural(batches.length, "batch", "batches")} · newest first · pick one to inspect
      </h3>
      <ul className="evl-batch-list">
        {batches.map((batch) => {
          const selected = batch.batch_id === selectedId;
          const about = [...batch.models, ...batch.prompt_versions];
          return (
            <li key={batch.batch_id}>
              <button
                type="button"
                className={selected ? "evl-batch selected" : "evl-batch"}
                aria-pressed={selected}
                onClick={() => onSelect(batch.batch_id)}
              >
                <span className="evl-batch-top">
                  <span className="evl-batch-title">{batch.title}</span>
                  {selected ? (
                    <span className="evl-batch-shown">
                      <Icon name="check" size={12} />
                      Shown
                    </span>
                  ) : null}
                </span>
                <TargetChips targets={batchTargets(batch)} />
                <span className="evl-batch-meta">
                  {fmtUtc(batch.created_at) ?? NOT_RECORDED} · {batch.source === "published" ? "published in git" : "local to this machine"}
                </span>
                <span className="evl-batch-count">
                  <strong>{kOfN(batch.outcomes.successes, batch.n)}</strong> verified recoveries
                </span>
                <span className="evl-batch-meta mono">
                  {about.length ? about.join(" · ") : mode === "scripted_regression" ? "scripted policy · no model" : "model not recorded"}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function ModeCard({
  mode,
  info,
  actions,
  children,
}: {
  mode: dto.EvalMode;
  info: dto.EvalModeInfo | null;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  const guide = MODE_GUIDE[mode];
  const status =
    info === null ? (
      <NotRecorded />
    ) : info.status === "implemented" ? (
      <StatusToken size="sm" tone="ok" label="Implemented" />
    ) : (
      <StatusToken size="sm" tone="neutral" icon="clock" label="Planned" />
    );
  return (
    <Card title={guide.tab} subtitle={info?.description ?? "The server did not describe this mode."} actions={actions}>
      <dl className="evl-guide">
        <div>
          <dt>Decisions come from</dt>
          <dd>{guide.decisions}</dd>
        </div>
        <div>
          <dt>It shows</dt>
          <dd>{guide.shows}</dd>
        </div>
        <div>
          <dt>It does not show</dt>
          <dd>{guide.doesNotShow}</dd>
        </div>
        <div>
          <dt>Made by · status</dt>
          <dd className="evl-guide-made">
            {guide.madeBy ? <Mono>{guide.madeBy}</Mono> : <span>Nothing yet</span>}
            {status}
          </dd>
        </div>
      </dl>
      {children}
    </Card>
  );
}

// --- one batch ------------------------------------------------------------------------------------

export function BatchHeader({ batch, records }: { batch: dto.BatchSummary; records: readonly dto.EvalRecord[] }) {
  const sources = providerClasses(records);
  const scripted = batch.mode === "scripted_regression";
  const none = scripted ? <span className="muted">None: the scripted policy is not an LLM</span> : <NotRecorded />;
  const targets = batchTargets(batch);
  // One target: the badge names it. Several (none so far): the badge shows the decisions only, the row lists them.
  const environment = targets.length === 1 ? targetFields(targets[0]) : undefined;
  return (
    <Card
      title={batch.title}
      subtitle={
        <span className="evl-batch-id">
          Batch <Mono>{batch.batch_id}</Mono>
        </span>
      }
      actions={
        <StatusToken
          size="sm"
          tone="neutral"
          icon={batch.source === "published" ? "save" : "package"}
          label={batch.source === "published" ? "Published in git" : "Local data folder"}
          title={
            batch.source === "published"
              ? "Kept in git under evals/reports/batches/"
              : "Kept only in this machine's data folder (or evals/reports/runs/): not in git"
          }
        />
      }
    >
      <div className="evl-sources" role="group" aria-label="Where this batch's decisions came from">
        {sources.length ? (
          sources.map((source) => (
            <DataSourceBadge key={source.origin || "none"} provider={sourceOf(source.origin, source.model)} environment={environment} size="sm" />
          ))
        ) : (
          <DataSourceBadge provider={null} environment={environment} size="sm" />
        )}
      </div>
      <KeyValues
        className="kv-wide evl-facts"
        rows={[
          ["Mode", <Mono>{batch.mode}</Mono>],
          ["Target", <TargetChips targets={targets} />],
          ...(batch.benchmark
            ? [
                [
                  "Benchmark grader",
                  <span className="cell-stack">
                    <span>{batch.benchmark}</span>
                    <span className="muted small">Its verdicts are counted apart (below) and never merged into RECAP's verified recoveries.</span>
                  </span>,
                ] as [ReactNode, ReactNode],
              ]
            : []),
          ["Created", fmtUtc(batch.created_at) ?? <NotRecorded />],
          [
            "Plan",
            batch.plan ? (
              <Mono>{batch.plan}</Mono>
            ) : (
              <span className="muted">No plan file: the methods below say how the runs were started</span>
            ),
          ],
          ["Providers", <ChipList items={batch.providers} empty={<NotRecorded />} />],
          ["Models", <ChipList items={batch.models} empty={none} />],
          ["Prompt versions", <ChipList items={batch.prompt_versions} empty={none} />],
          ["Runs (N)", <strong>{fmtCount(batch.n)}</strong>],
        ]}
      />
    </Card>
  );
}

function StatesTile({ outcomes }: { outcomes: dto.BatchOutcomes }) {
  const states = stateCounts(outcomes);
  return (
    <div className="stat-tile evl-states">
      <div className="stat-label">Final states</div>
      {states.length ? (
        <ul className="evl-state-list">
          {states.map(([state, k]) => (
            <li key={state}>
              <StateChip state={state} />
              <span>{kOfN(k, outcomes.n)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <NotRecorded />
      )}
    </div>
  );
}

/** A benchmark grader's verdicts over the batch: its own tile, beside (never inside) RECAP's count. */
export function BenchmarkTile({ benchmark, counts, n }: { benchmark: string; counts: readonly [VerdictView, number][]; n: number }) {
  return (
    <div className="stat-tile evl-verdicts" role="group" aria-label="Benchmark verdicts, kept apart from RECAP's outcome">
      <div className="stat-label">Benchmark verdicts</div>
      <ul className="evl-state-list">
        {counts.map(([view, k]) => (
          <li key={view.key}>
            <VerdictToken view={view} />
            <span>{kOfN(k, n)}</span>
          </li>
        ))}
      </ul>
      <div className="stat-note">
        From the grader this batch names: {benchmark}. Each verdict was copied by hand from its output, and none is merged into RECAP's verified
        recoveries.
      </div>
    </div>
  );
}

export function ConfigMatrixView({ matrix, onOpen }: { matrix: ConfigMatrix; onOpen: (runId: string) => void }) {
  return (
    <div className="evl-matrix-block">
      <h3 className="mini-title">Configurations × scenarios · one mark per run</h3>
      <p className="evl-help">
        Each row is one configuration of the benchmark, each column one scenario. Compare a row with the others only knowing that the policy is
        scripted: the differences show how it uses runbooks, memory and routing.
      </p>
      <div className="table-wrap">
        <table className="table dense evl-matrix">
          <caption className="sr-only">Outcome of every run, by configuration and scenario</caption>
          <thead>
            <tr>
              <th scope="col">Configuration</th>
              <th scope="col">Verified recoveries</th>
              {matrix.scenarios.map((scenario) => (
                <th key={scenario} scope="col" className="evl-matrix-scenario">
                  <Mono>{scenario}</Mono>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {matrix.rows.map((row) => (
              <tr key={row.label}>
                <th scope="row" className="evl-matrix-config">
                  <span className="cell-stack">
                    <Mono>{row.label}</Mono>
                    {row.redteam ? (
                      <Chip tone="bad" title="A deliberately compromised planner: its safe outcome is a refused fix (safety blocked), not a recovery">
                        red-team
                      </Chip>
                    ) : null}
                  </span>
                </th>
                <td className="num nowrap">{kOfN(row.successes, row.n)}</td>
                {matrix.scenarios.map((scenario) => {
                  const runs = row.cells[scenario] ?? [];
                  return (
                    <td key={scenario} className="evl-matrix-cell">
                      {runs.length ? (
                        runs.map((record) => <RunMark key={record.run_id} record={record} onOpen={onOpen} />)
                      ) : (
                        <span className="muted" title="This configuration did not run this scenario">
                          <span aria-hidden="true">·</span>
                          <span className="sr-only">no run</span>
                        </span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <MarkLegend />
    </div>
  );
}

export function ScenarioTable({ groups }: { groups: readonly ScenarioGroup[] }) {
  return (
    <div className="evl-scenarios">
      <h3 className="mini-title">By scenario</h3>
      <DataTable
        rows={[...groups]}
        rowKey={(g) => g.scenario}
        className="dense evl-scenario-table"
        empty="This batch holds no runs."
        columns={[
          {
            key: "scenario",
            header: "Scenario",
            render: (g) => (
              <span className="cell-stack">
                <Mono>{g.scenario}</Mono>
                <span className="muted small">{g.family || NOT_RECORDED}</span>
              </span>
            ),
          },
          { key: "n", header: "Runs", numeric: true, render: (g) => fmtCount(g.n) },
          { key: "ok", header: "Verified recoveries", nowrap: true, render: (g) => kOfN(g.successes, g.n) },
          {
            key: "outcomes",
            header: "Outcomes",
            render: (g) => (
              <span className="chip-row">
                {g.outcomes.map(([key, k]) => (
                  <span key={key} className="evl-outcome-count">
                    <OutcomeKeyToken outcome={key} />
                    <span className="muted small">×{fmtCount(k)}</span>
                  </span>
                ))}
              </span>
            ),
          },
        ]}
      />
    </div>
  );
}

export function OutcomesCard({
  batch,
  records,
  onOpen,
}: {
  batch: dto.BatchSummary;
  records: readonly dto.EvalRecord[];
  onOpen: (runId: string) => void;
}) {
  const { outcomes } = batch;
  const n = outcomes.n;
  const matrix = configurationMatrix(records);
  const note = sampleNote(n, matrix?.rows.length ?? 0);
  const missed = n - outcomes.successes;
  const verdicts = benchmarkVerdictCounts(batch); // empty unless the batch names a grader
  const graded = verdicts.length ? batch.benchmark : null;
  return (
    <Card
      title="Outcomes"
      subtitle={`Counts over this batch only, as runs out of N. A success needs RESOLVED and the independent oracle's verified recovery.${
        graded ? " A benchmark grader's verdicts are counted apart." : ""
      }`}
    >
      <Notice tone="warn" icon="alert" title={note.label}>
        {note.text}
      </Notice>
      <div className={graded ? "stat-grid evl-tiles evl-tiles-4" : "stat-grid evl-tiles"}>
        <StatTile
          label="Verified recoveries"
          value={kOfN(outcomes.successes, n)}
          tone={outcomes.successes ? "ok" : "neutral"}
          note={
            matrix
              ? "all configurations together: compare the rows below instead"
              : graded
                ? "resolved and confirmed by RECAP's own oracle"
                : "resolved and confirmed by the oracle"
          }
        />
        <StatTile label="Did not recover" value={kOfN(missed, n)} tone={missed ? "bad" : "neutral"} note="each with one failure category (below)" />
        <StatesTile outcomes={outcomes} />
        {graded ? <BenchmarkTile benchmark={graded} counts={verdicts} n={n} /> : null}
      </div>
      {matrix ? (
        <ConfigMatrixView matrix={matrix} onOpen={onOpen} />
      ) : records.length ? (
        <>
          <div className="evl-strip-block">
            <h3 className="mini-title">Each run, in batch order · select one to open its record</h3>
            <ol className="evl-strip">
              {records.map((record, i) => (
                <li key={`${record.run_id}-${i}`}>
                  <RunMark record={record} onOpen={onOpen} />
                </li>
              ))}
            </ol>
            <MarkLegend />
          </div>
          <ScenarioTable groups={scenarioGroups(records)} />
        </>
      ) : null}
    </Card>
  );
}

export function FailuresCard({
  batch,
  records,
  onOpen,
}: {
  batch: dto.BatchSummary;
  records: readonly dto.EvalRecord[];
  onOpen: (runId: string) => void;
}) {
  const groups = failureGroups(batch.outcomes, records);
  const allRecovered = batch.outcomes.n > 0 && batch.outcomes.successes === batch.outcomes.n;
  return (
    <Card
      title="Why runs did not recover"
      subtitle="Exactly one category per run, judged after the run against the scenario's predeclared answer (which the model never sees). The detail is RECAP's own wording."
    >
      {groups.length === 0 ? (
        allRecovered ? (
          <div className="evl-allgood">
            <StatusToken tone="ok" label={`Every run ended in a verified recovery (${kOfN(batch.outcomes.successes, batch.outcomes.n)})`} />
          </div>
        ) : (
          <EmptyView title="No failure categories" reason="The server reported no failure category for this batch." />
        )
      ) : (
        <ul className="evl-failures">
          {groups.map((group) => (
            <li key={group.category} className="evl-failure">
              <div className="evl-failure-head">
                <StatusToken tone={group.info.tone} icon={group.info.icon} label={group.info.label} />
                <span className="evl-failure-count">{kOfN(group.count, batch.outcomes.n)} runs</span>
                <Mono>{group.category}</Mono>
              </div>
              <p className="evl-failure-meaning">{group.info.meaning}</p>
              {group.records.length ? (
                <ul className="evl-failure-runs">
                  {group.records.map((record) => (
                    <li key={record.run_id}>
                      <RecordButton runId={record.run_id} onOpen={onOpen} />
                      <Mono>{record.scenario_id}</Mono>
                      {record.label && record.label !== record.scenario_id ? <span className="muted small">{record.label}</span> : null}
                      <span className="evl-failure-detail">{record.failure_detail || NOT_RECORDED}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted small">No record of this batch carries this category.</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

const PAGE_SIZES = [10, 20, 50] as const;

export function RecordsCard({
  records,
  onOpen,
  initialLimit = 20,
  benchmark = null,
}: {
  records: readonly dto.EvalRecord[];
  onOpen: (runId: string) => void;
  initialLimit?: number;
  /** The grader the batch names: each run's verdict is then shown, apart from RECAP's. */
  benchmark?: string | null;
}) {
  const [values, setValues] = useState<FilterValues>({});
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(initialLimit);
  const configured = configurationMatrix(records) !== null;
  const filters: FilterSpec[] = [
    ...(configured ? [{ key: "config", label: "Configuration", options: filterOptions(records, (r) => r.label || NO_LABEL) }] : []),
    { key: "scenario", label: "Scenario", options: filterOptions(records, (r) => r.scenario_id) },
    { key: "outcome", label: "Outcome", options: filterOptions(records, outcomeKey, outcomeLabel) },
  ];
  const shown = applyFilters(records, values, {
    config: (r) => r.label || NO_LABEL,
    scenario: (r) => r.scenario_id,
    outcome: outcomeKey,
  });
  const win = pageWindow(offset, limit, shown.length);
  const page = shown.slice(win.offset, win.offset + win.limit);
  const change = (key: string, value: string | null) => {
    setValues((prev) => ({ ...prev, [key]: value }));
    setOffset(0);
  };
  return (
    <Card
      title="Runs in this batch"
      subtitle="One evaluation record per run, built from the run's own record and trace. Select a run id (or a row) to see every field."
      flush
    >
      <div className="card-pad evl-records-head">
        <FilterBar
          filters={filters}
          values={values}
          onChange={change}
          onClear={() => {
            setValues({});
            setOffset(0);
          }}
          summary={`Showing ${fmtCount(shown.length)} of ${plural(records.length, "run")}`}
          label="Filter the runs"
        />
        <p className="evl-help">
          Tokens: what the model server reported (in · out) next to RECAP's own estimate of the context it built, which is never a model's count. Model time
          sums each request's duration, so requests of parallel agents can add up to more than the run's own time.
        </p>
      </div>
      <DataTable
        rows={page}
        rowKey={(r, i) => `${r.run_id}-${win.offset + i}`}
        className="dense wrap-head evl-records"
        onRowClick={(r) => onOpen(r.run_id)}
        empty={records.length ? "No run matches these filters." : "This batch holds no runs."}
        columns={[
          {
            key: "run",
            header: "Run",
            render: (r) => (
              <span className="cell-stack">
                <RecordButton runId={r.run_id} onOpen={onOpen} />
                <span className="muted small">{r.label || NO_LABEL}</span>
                {isRedTeam(r) ? (
                  <Chip tone="bad" title="A red-team scenario (its predeclared answer says so)">
                    red-team
                  </Chip>
                ) : null}
              </span>
            ),
          },
          {
            key: "scenario",
            header: "Scenario",
            render: (r) => (
              <span className="cell-stack">
                <Mono>{r.scenario_id}</Mono>
                <span className="muted small">
                  {r.incident_family || NOT_RECORDED} · {r.split || NOT_RECORDED}
                </span>
              </span>
            ),
          },
          {
            key: "topology",
            header: "Topology per round",
            render: (r) =>
              r.topology.length ? (
                <ol className="evl-rounds">
                  {r.topology.map((entry, i) => {
                    const round = parseTopology(entry);
                    return (
                      <li key={i}>
                        <span className="evl-round-no">R{i + 1}</span>
                        <span className="evl-round-mode">{round.mode}</span>
                        {round.agents.length ? <span className="muted small">{round.agents.join(" + ")}</span> : null}
                      </li>
                    );
                  })}
                </ol>
              ) : (
                <NotRecorded />
              ),
          },
          {
            key: "context",
            header: "Retrieval · memory",
            render: (r) => (
              <span className="cell-stack">
                <span>{retrievalLabel(r.retrieval_mode)}</span>
                <span className="muted small">memory {r.memory ? "on" : "off"}</span>
              </span>
            ),
          },
          {
            key: "result",
            header: (
              <span className="cell-stack">
                <span>Result</span>
                <span className="evl-th-note">state · category · oracle{benchmark ? " · benchmark (apart)" : ""}</span>
              </span>
            ),
            render: (r) => {
              const verdict = recordVerdict(r, benchmark);
              return (
                <span className="cell-stack evl-result">
                  {r.final_state ? <StateChip state={r.final_state} /> : <NotRecorded />}
                  <OutcomeToken record={r} />
                  <StatusToken
                    size="sm"
                    variant="plain"
                    tone={r.oracle_verified ? "ok" : "neutral"}
                    icon={r.oracle_verified ? "check" : "minus"}
                    label={r.oracle_verified ? "oracle verified" : "oracle: not verified"}
                  />
                  {verdict ? <VerdictToken view={verdict} prefix="benchmark: " /> : null}
                </span>
              );
            },
          },
          { key: "requests", header: "Model requests", numeric: true, render: (r) => fmtCount(r.model_requests) },
          {
            key: "tokens",
            header: (
              <span className="cell-stack">
                <span>Tokens</span>
                <span className="evl-th-note">reported · RECAP est.</span>
              </span>
            ),
            render: (r) => {
              const tokens = tokenView(r);
              return (
                <span className="cell-stack">
                  {tokens.reported ? <span className="nowrap">{tokens.reported}</span> : <NotRecorded>{tokens.missing}</NotRecorded>}
                  <span className="muted small nowrap">est. {tokens.estimate} (RECAP)</span>
                </span>
              );
            },
          },
          { key: "tools", header: "Tool calls", numeric: true, render: (r) => fmtCount(r.tool_calls) },
          {
            key: "time",
            header: (
              <span className="cell-stack">
                <span>Time</span>
                <span className="evl-th-note">run · model · waits</span>
              </span>
            ),
            render: (r) => {
              const time = timeView(r);
              return (
                <span className="cell-stack">
                  <span className="nowrap">
                    {time.run}
                    {time.about ? <span className="muted small"> {time.about}</span> : null}
                  </span>
                  {time.model ? <span className="muted small">{time.model}</span> : null}
                  {time.wait ? <span className="muted small">{time.wait}</span> : null}
                  {time.approval ? <span className="muted small">{time.approval}</span> : null}
                </span>
              );
            },
          },
        ]}
      />
      {records.length > PAGE_SIZES[0] ? (
        <div className="card-pad evl-pager">
          <Pager
            offset={win.offset}
            limit={win.limit}
            total={shown.length}
            onChange={setOffset}
            pageSizes={PAGE_SIZES}
            onPageSizeChange={(size) => {
              setLimit(size);
              setOffset(0);
            }}
            noun="runs"
            label="Pages of runs"
          />
        </div>
      ) : null}
    </Card>
  );
}

export function MethodsCards({ batch }: { batch: Pick<dto.BatchSummary, "methods" | "limitations"> }) {
  return (
    <div className="grid-2 evl-methods">
      <Card title="Methods" subtitle="How these runs were made and judged, as the batch records it">
        {batch.methods.length ? (
          <ul className="caveats">
            {batch.methods.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
        ) : (
          <EmptyView title="No methods recorded" reason="The batch file lists no methods." />
        )}
      </Card>
      <Card title="Limitations" subtitle="What these numbers do not show, as the batch records it">
        {batch.limitations.length ? (
          <ul className="caveats">
            {batch.limitations.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
        ) : (
          <EmptyView title="No limitations recorded" reason="The batch file lists no limitations." />
        )}
      </Card>
    </div>
  );
}

// --- the record drawer ---------------------------------------------------------------------------

/**
 * Whether this server stores the run: its summary, or null when it does not. Asked through
 * GET /runs?scenario= (the record's scenario, paged), which answers 200 either way, so a run
 * kept only in another data folder never shows up as a failed request.
 */
export async function storedRun(runId: string, scenarioId: string): Promise<dto.RunSummary | null> {
  const limit = 1000;
  for (let offset = 0; ; offset += limit) {
    const page = await api.runsPage({ scenario: scenarioId, limit, offset });
    const found = page.items.find((run) => run.run_id === runId);
    if (found) return found;
    if (page.items.length < limit || (page.total !== null && offset + limit >= page.total)) return null;
  }
}

/** Whether this server stores the run, with links to its trace when it does. */
export function TraceAvailabilityView({
  runId,
  polled,
}: {
  runId: string;
  polled: Pick<Polled<dto.RunSummary | null>, "data" | "error" | "loading" | "refresh">;
}) {
  if (polled.data) {
    return (
      <div className="evl-trace ok" role="status">
        <Icon name="check" size={16} />
        <span className="evl-trace-text">This server stores this run: its full trace is one click away.</span>
        <span className="evl-trace-links">
          <a className="btn btn-small btn-primary" href={hrefFor("timeline", { run: runId })}>
            <Icon name="timeline" size={13} />
            Open in Trace Explorer
          </a>
          <a className="btn btn-small" href={hrefFor("evidence", { run: runId })}>
            Evidence & Decisions
          </a>
        </span>
      </div>
    );
  }
  if (polled.data === null) {
    return (
      <div className="evl-trace" role="status">
        <Icon name="info" size={16} />
        <span className="evl-trace-text">
          This server stores no run with this id, so there is no trace to open here. The record below is everything this server holds for it.
        </span>
      </div>
    );
  }
  if (polled.error) {
    return <ErrorView error={polled.error} title="Could not check whether this server stores the run" onRetry={polled.refresh} />;
  }
  return <LoadingView inline label="Checking whether this server stores this run…" />;
}

function TraceAvailability({ runId, scenarioId }: { runId: string; scenarioId: string }) {
  const polled = usePolling(() => storedRun(runId, scenarioId), null, [runId, scenarioId]);
  return <TraceAvailabilityView runId={runId} polled={polled} />;
}

function Section({ title, icon, children, className }: { title: ReactNode; icon?: IconName; children: ReactNode; className?: string }) {
  return (
    <section className={className ? `evl-section ${className}` : "evl-section"}>
      <h3 className="evl-section-title">
        {icon ? <Icon name={icon} size={14} /> : null}
        {title}
      </h3>
      {children}
    </section>
  );
}

function orNone(value: string | null | undefined, none: ReactNode = <NotRecorded />): ReactNode {
  return value ? <Mono>{value}</Mono> : none;
}

export function RecordDetails({ record, benchmark = null }: { record: dto.EvalRecord; benchmark?: string | null }) {
  const scripted = isScripted(record);
  const noModel = scripted ? <span className="muted">None: the scripted policy is not an LLM</span> : <NotRecorded />;
  const asked = record.model_requests > 0;
  const seconds = (value: number | null, none: ReactNode) => (value === null ? none : fmtSeconds(value));
  const modelNone = asked ? <NotRecorded /> : <span className="muted">No model request was sent</span>;
  const tokens = tokenView(record);
  const expected = expectedEntries(record.expected);
  const byAgent = toolCallRows(record.tool_calls_by_agent);
  const verdict = recordVerdict(record, benchmark);
  return (
    <div className="evl-drawer-grid">
      <Section title="Run" icon="live">
        <KeyValues
          className="kv-wide"
          rows={[
            ["Run id", <Mono>{record.run_id}</Mono>],
            ["Incident", orNone(record.incident_id)],
            ["Scenario", <Mono>{record.scenario_id}</Mono>],
            ["Family · split", `${record.incident_family || NOT_RECORDED} · ${record.split || NOT_RECORDED}`],
            ["Label", record.label ? <Mono>{record.label}</Mono> : <NotRecorded />],
            ["Mode", <Mono>{record.mode}</Mono>],
            ["Target", <TargetChips targets={[recordTarget(record)]} />],
            ["Started", fmtUtc(record.started_at) ?? <NotRecorded />],
            ["Ended", fmtUtc(record.ended_at) ?? <NotRecorded />],
            ["Recording", orNone(record.recording, scripted ? <span className="muted">None: a scripted run has no model answers to record</span> : <NotRecorded />)],
          ]}
        />
      </Section>

      <Section title="Expected answer · hidden from the model" icon="lock" className="evl-hidden-answer">
        <p className="evl-help">The scenario's predeclared answer. RECAP never shows it to a model: it is read only after the run, to judge it.</p>
        {expected.length ? (
          <KeyValues className="kv-wide" rows={expected.map(([key, value]): [ReactNode, ReactNode] => [<Mono>{key}</Mono>, value])} />
        ) : (
          <NotRecorded>No predeclared answer</NotRecorded>
        )}
      </Section>

      <Section title="Who decided" icon="cpu">
        <KeyValues
          className="kv-wide"
          rows={[
            ["Provider", orNone(record.provider)],
            ["Provider class", orNone(record.provider_class)],
            ["Model", orNone(record.model, noModel)],
            ["Model weights", orNone(record.model_weights, scripted ? noModel : <NotRecorded />)],
            ["Prompt version", orNone(record.prompt_version, scripted ? <span className="muted">None: the scripted policy uses no prompts</span> : <NotRecorded />)],
            ["Config hash", orNone(record.config_hash)],
            ["Code version", orNone(record.code_version)],
          ]}
        />
      </Section>

      <Section title="Routing, runbooks and memory" icon="route">
        <KeyValues
          className="kv-wide"
          rows={[
            ["Strategy asked", orNone(record.strategy)],
            ["Rounds", fmtCount(record.rounds)],
            ["Topology per round", <TextList items={record.topology} empty={<NotRecorded />} mono />],
            ["Runbook search", retrievalLabel(record.retrieval_mode)],
            ["Agent searches", fmtCount(record.agent_searches)],
            ["Sections retrieved", <ChipList items={record.retrieved_docs} empty={<span className="muted">None</span>} />],
            ["Memory", record.memory ? "on" : "off"],
            [
              "Memory hits",
              record.memory_hits.length ? (
                <ul className="evl-hits">
                  {record.memory_hits.map((hit, i) => (
                    <li key={i}>
                      <KeyValues
                        className="kv-wide evl-hit"
                        rows={memoryHitFields(hit).map(([key, value]): [ReactNode, ReactNode] => [<Mono>{key}</Mono>, value])}
                      />
                    </li>
                  ))}
                </ul>
              ) : (
                <span className="muted">None</span>
              ),
            ],
          ]}
        />
      </Section>

      <Section title="Diagnosis" icon="evidence">
        <KeyValues
          className="kv-wide"
          rows={[
            ["Supported claims", <ChipList items={record.supported_hypotheses} empty={<span className="muted">None supported</span>} />],
            ["Final diagnosis", orNone(record.diagnosis, <span className="muted">No supported diagnosis</span>)],
            [
              "Matches the expected answer",
              record.diagnosis_correct === null ? (
                <span className="muted">Not judged: the scenario declares no answer</span>
              ) : (
                <StatusToken size="sm" variant="plain" tone={record.diagnosis_correct ? "ok" : "bad"} label={record.diagnosis_correct ? "yes" : "no"} />
              ),
            ],
          ]}
        />
      </Section>

      <Section title="Fix, guardrails and outcome" icon="shield">
        <KeyValues
          className="kv-wide"
          rows={[
            [
              "Proposed fixes",
              <div className="evl-stack">
                <TextList items={record.proposed_actions} empty={<span className="muted">None proposed</span>} mono />
                {record.proposed_actions.length ? <span className="muted small">The bracket names who drafted it ([llm]: the model).</span> : null}
              </div>,
            ],
            ["Guardrails", record.gateway_outcome ? <span>{record.gateway_outcome}</span> : <span className="muted">No fix reached the guardrails</span>],
            ["Approvals", <TextList items={record.approvals} empty={<span className="muted">None recorded</span>} />],
            ["Final state", record.final_state ? <StateChip state={record.final_state} /> : <NotRecorded />],
            [
              "Oracle verified",
              <StatusToken
                size="sm"
                variant="plain"
                tone={record.oracle_verified ? "ok" : "neutral"}
                icon={record.oracle_verified ? "check" : "minus"}
                label={record.oracle_verified ? "yes" : "no"}
              />,
            ],
            ["Outcome", <OutcomeToken record={record} />],
            ["Failure category", record.failure ? <Mono>{record.failure}</Mono> : <span className="muted">None</span>],
            ["Failure detail", record.failure_detail || <NotRecorded />],
            ...(verdict
              ? [
                  [
                    "Benchmark verdict",
                    <span className="cell-stack">
                      <VerdictToken view={verdict} />
                      <span className="muted small">
                        Grader: {benchmark || "not named by this batch"}. The grader's own verdict, copied by hand from its output: kept apart from
                        RECAP's outcome above and never merged into it.
                      </span>
                    </span>,
                  ] as [ReactNode, ReactNode],
                ]
              : []),
          ]}
        />
      </Section>

      <Section title="Usage and time" icon="clock" className="evl-wide">
        <KeyValues
          className="kv-wide"
          rows={[
            ["Model requests", fmtCount(record.model_requests)],
            ["Tokens reported", tokens.reported ?? <NotRecorded>{tokens.missing}</NotRecorded>],
            [
              "Tokens estimated",
              <span>
                {tokens.estimate} <span className="muted small">RECAP's own estimate of the context it built (about 4 characters per token), not a model's count</span>
              </span>,
            ],
            [
              "Tool calls",
              <span className="cell-stack">
                <span>{fmtCount(record.tool_calls)}</span>
                {byAgent.length ? (
                  <span className="chip-row">
                    {byAgent.map(([agent, calls]) => (
                      <Chip key={agent} mono title={agent === "controller" ? "Writes RECAP sent to the target" : "Reads by this agent"}>
                        {agent} {calls}
                      </Chip>
                    ))}
                  </span>
                ) : null}
              </span>,
            ],
            [
              "Run time",
              <span className="cell-stack">
                <span>{fmtSeconds(record.seconds)}</span>
                <span className="muted small">
                  From the run's creation to its end{record.success ? ": its time to a verified recovery" : ""}, approval wait included.
                </span>
              </span>,
            ],
            [
              "Model request time (summed)",
              <span className="cell-stack">
                <span>{seconds(record.model_seconds, modelNone)}</span>
                {record.model_seconds !== null ? (
                  <span className="muted small">Each request's duration as RECAP measured it, added up: parallel requests can exceed the run's time.</span>
                ) : null}
              </span>,
            ],
            ["Model server time", seconds(record.model_server_seconds, modelNone)],
            [
              "Wait inside the model server",
              <span className="cell-stack">
                <span>{seconds(record.model_wait_seconds, modelNone)}</span>
                {record.model_wait_seconds !== null ? (
                  <span className="muted small">
                    Time a request queued inside the server instead of computing (behind another agent's request on a server that answers one at a time, or loading).
                  </span>
                ) : null}
              </span>,
            ],
          ]}
        />
        {record.stages.length ? (
          <table className="table dense evl-stages">
            <caption className="evl-help">Time in each lifecycle stage, from the run's state transitions</caption>
            <thead>
              <tr>
                <th scope="col">Stage</th>
                <th scope="col" className="num">
                  Seconds
                </th>
                <th scope="col">Note</th>
              </tr>
            </thead>
            <tbody>
              {record.stages.map((stage, i) => (
                <tr key={`${stage.stage}-${i}`}>
                  <td>
                    <Mono>{stage.stage}</Mono>
                  </td>
                  <td className="num">{fmtSeconds(stage.seconds)}</td>
                  <td className="muted small">{stageNote(stage.stage) ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted small">Stage times: {NOT_RECORDED.toLowerCase()}.</p>
        )}
      </Section>
    </div>
  );
}

export function RecordDrawer({
  record,
  onClose,
  trace,
  benchmark = null,
}: {
  record: dto.EvalRecord | null;
  onClose: () => void;
  /** Replaces the GET /runs/{id} check (tests). */
  trace?: ReactNode;
  /** The grader the record's batch names (its verdict is shown apart from RECAP's outcome). */
  benchmark?: string | null;
}) {
  // A long record: focus starts at its top (not on the last control), so the dialog opens at its head.
  const top = useRef<HTMLDivElement>(null);
  if (!record) return null;
  return (
    <Modal
      open
      onClose={onClose}
      initialFocusRef={top}
      size="large"
      title={
        <span className="evl-drawer-title">
          Evaluation record <Mono>{record.run_id}</Mono>
        </span>
      }
      description="Read-only: every field of this run's evaluation record, as the server sent it."
      closeLabel="Close the record"
    >
      <div className="evl-drawer-badges" ref={top} tabIndex={-1}>
        <DataSourceBadge provider={sourceOf(record.provider_class, record.model)} environment={targetFields(recordTarget(record))} size="sm" />
        {record.final_state ? <StateChip state={record.final_state} /> : null}
        <OutcomeToken record={record} />
      </div>
      {trace ?? <TraceAvailability runId={record.run_id} scenarioId={record.scenario_id} />}
      <RecordDetails record={record} benchmark={benchmark} />
      <details className="evl-raw">
        <summary>The whole record as JSON</summary>
        <JsonView value={record} maxHeight={360} />
      </details>
    </Modal>
  );
}

export function BatchDetail({
  view,
  recordId,
  onOpenRecord,
  onCloseRecord,
}: {
  view: dto.BatchView;
  recordId: string | null;
  onOpenRecord: (runId: string) => void;
  onCloseRecord: () => void;
}) {
  const { batch, records } = view;
  const record = recordId ? (records.find((r) => r.run_id === recordId) ?? null) : null;
  return (
    <>
      <BatchHeader batch={batch} records={records} />
      {recordId && !record ? (
        <Notice tone="warn" icon="alert" title={`Run ${recordId} is not in this batch`}>
          The link names a run this batch does not hold. Pick a run below.
        </Notice>
      ) : null}
      <OutcomesCard batch={batch} records={records} onOpen={onOpenRecord} />
      <FailuresCard batch={batch} records={records} onOpen={onOpenRecord} />
      <RecordsCard key={batch.batch_id} records={records} onOpen={onOpenRecord} benchmark={batch.benchmark} />
      <MethodsCards batch={batch} />
      <RecordDrawer record={record} onClose={onCloseRecord} benchmark={batch.benchmark} />
    </>
  );
}

function BatchSection({ batchId, recordId, onParams }: { batchId: string; recordId: string | null; onParams: (params: Params) => void }) {
  const polled = usePolling(() => api.evalBatch(batchId), null, [batchId]);
  return (
    <PolledView polled={polled} loadingLabel="Loading the batch's records…" errorTitle={`Could not load batch ${batchId}`}>
      {(view) => (
        <BatchDetail
          view={view}
          recordId={recordId}
          onOpenRecord={(runId) => onParams({ record: runId })}
          onCloseRecord={() => onParams({ record: null })}
        />
      )}
    </PolledView>
  );
}

// --- starting the scripted regression ----------------------------------------------------------

export function LaunchConfirm({
  open,
  starting,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  starting: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  // Focus starts on Cancel: the safe choice is the default one.
  const cancel = useRef<HTMLButtonElement>(null);
  return (
    <Modal
      open={open}
      onClose={onCancel}
      initialFocusRef={cancel}
      title="Start the scripted regression?"
      description="It runs RECAP's own benchmark again, in the background on the API server. This is exactly what will run."
      footer={
        <>
          <button ref={cancel} type="button" className="btn" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" disabled={starting} onClick={onConfirm}>
            <Icon name="play" size={14} />
            {starting ? "Starting…" : "Start scripted regression"}
          </button>
        </>
      }
    >
      <ul className="evl-confirm">
        <li>
          <Icon name="code" size={16} />
          <span>
            <strong>Decisions:</strong> RECAP's scripted policy (fixed playbooks), not an LLM. No model is called and no free-plan quota is used:
            it costs $0.
          </span>
        </li>
        <li>
          <Icon name="flask" size={16} />
          <span>
            <strong>Target:</strong> the simulated shop only, in a fresh evaluation-only data folder. Your runs, your memory and the published
            batches stay as they are.
          </span>
        </li>
        <li>
          <Icon name="route" size={16} />
          <span>
            <strong>What runs:</strong> the benchmark split's train scenarios first (they build an evaluation-only memory), then every evaluation
            configuration on its test or red-team scenarios, each through the real MCP tool server, gateway, verifier and oracle. The red-team
            suite runs a deliberately compromised planner with the safety gateway on and off (off is allowed only in the simulator).
          </span>
        </li>
        <li>
          <Icon name="key" size={16} />
          <span>
            <strong>Approvals:</strong> the simulation auto-approver approves the fixes inside this evaluation, so it does not test human review.
          </span>
        </li>
        <li>
          <Icon name="save" size={16} />
          <span>
            <strong>Output:</strong> a report and a <Mono>scripted_regression</Mono> batch in <Mono>evals/reports/runs/&lt;report id&gt;/</Mono>{" "}
            (git-ignored), listed on this tab as a local batch. Nothing is published to git.
          </span>
        </li>
      </ul>
      <p className="evl-help">
        One evaluation runs at a time. It stops with an error, before any scenario runs, if the server's default provider is not the scripted policy.
      </p>
    </Modal>
  );
}

export function LaunchButton({
  signedIn,
  running,
  starting,
  onClick,
}: {
  signedIn: boolean;
  running: boolean;
  starting: boolean;
  onClick: () => void;
}) {
  const hint = useId();
  return (
    <span className="evl-launch">
      <button
        type="button"
        className="btn btn-primary"
        disabled={!signedIn || running || starting}
        aria-describedby={signedIn ? undefined : hint}
        onClick={onClick}
      >
        <Icon name="play" size={14} />
        {running ? "Scripted regression running…" : starting ? "Starting…" : "Run scripted regression…"}
      </button>
      {signedIn ? null : (
        <span id={hint} className="evl-launch-hint">
          Operator only: sign in (top right) to start it.
        </span>
      )}
    </span>
  );
}

export function EvaluationStatus({
  results,
  startedHere,
  reportBefore = null,
  note,
  startError,
}: {
  results: Pick<Polled<EvalResults>, "data" | "error">;
  startedHere: boolean;
  /** The report the server showed when this page started the evaluation (it is not the new one). */
  reportBefore?: string | null;
  note: string | null;
  startError: ApiError | null;
}) {
  const data = results.data;
  const finished =
    startedHere && data && !data.running && !data.error && data.report && data.report.report_id !== reportBefore ? data.report : null;
  return (
    <>
      {startError ? (
        <ErrorView error={startError} title="Could not start the scripted regression" />
      ) : note && !finished ? (
        <Notice tone="info" icon="info" title={note} />
      ) : null}
      {data?.running ? (
        <Card
          title="Scripted regression running"
          subtitle="The runner's latest lines: the train phase, then every configuration on each scenario. Its batch appears in the list above when it finishes."
          actions={<LoadingView inline label="running" />}
        >
          <LogView text={data.progress.join("\n")} label="Runner output (latest lines)" maxHeight={180} emptyText="Starting…" />
        </Card>
      ) : null}
      {data && !data.running && data.error ? (
        <Notice tone="bad" icon="x" title="The last scripted regression this server ran failed">
          {data.error}
        </Notice>
      ) : null}
      {finished ? (
        <Notice tone="ok" icon="check" title={`Finished: report ${finished.report_id}`}>
          Its batch is listed above as a local batch (it may take a moment to appear).
        </Notice>
      ) : null}
    </>
  );
}

// --- the benchmark report's extras (GET /evals/results) -------------------------------------------

export function BenchmarkReportCard({ report, batchId }: { report: EvalReport; batchId: string | null }) {
  const redteam = (report.summaries ?? []).filter((s) => s.suite === "redteam");
  const train = report.train ?? [];
  const sameRuns = batchId !== null && report.report_id === batchId;
  return (
    <Card
      title="From the benchmark report: what the batch does not hold"
      subtitle={
        <>
          GET /evals/results · report <Mono>{report.report_id}</Mono>
          {sameRuns ? " · the same runs as the batch shown above" : ""}
        </>
      }
      flush
    >
      <div className="card-pad">
        <h3 className="mini-title">Red-team suite: unsafe actions attempted vs executed</h3>
        <p className="evl-help">
          A deliberately compromised planner asks for a destructive action. “Attempted” counts what it asked for; “executed” what actually ran in the
          simulator. Blocking it is the safe outcome.
        </p>
      </div>
      <DataTable
        rows={redteam}
        rowKey={(s) => s.name}
        className="dense wrap-head"
        empty="This report has no red-team configuration."
        columns={[
          {
            key: "name",
            header: "Configuration",
            render: (s) => (
              <span className="cell-stack">
                <Mono>{s.name}</Mono>
                <span className="muted small">{s.description}</span>
              </span>
            ),
          },
          { key: "n", header: "Runs", numeric: true, render: (s) => fmtCount(s.n) },
          { key: "attempts", header: "Unsafe actions attempted", numeric: true, render: (s) => fmtCount(s.unsafe_attempts) },
          { key: "blocked", header: "Blocked by RECAP", numeric: true, render: (s) => fmtCount(s.unsafe_blocked) },
          {
            key: "executed",
            header: "Executed in the simulator",
            numeric: true,
            render: (s) =>
              s.unsafe_executed > 0 ? (
                <StatusToken size="sm" tone="bad" label={`${fmtCount(s.unsafe_executed)} executed`} />
              ) : (
                <StatusToken size="sm" variant="plain" tone="ok" label="0 executed" />
              ),
          },
          {
            key: "refused",
            header: "Refused by the simulator",
            numeric: true,
            render: (s) => (s.unsafe_refused === undefined ? <NotRecorded /> : fmtCount(s.unsafe_refused)),
          },
        ]}
      />
      <div className="card-pad evl-report-more">
        <details>
          <summary>Train phase: {plural(train.length, "run")} that built the evaluation memory (not scored)</summary>
          {train.length ? (
            <ul className="evl-train">
              {train.map((c, i) => (
                <li key={`${c.scenario_id}-${i}`}>
                  <Mono>{c.scenario_id}</Mono>
                  <StateChip state={c.final_state} />
                  <span className="muted small mono">{c.run_id}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted small">This report lists no train runs.</p>
          )}
        </details>
        <details>
          <summary>Report metadata (reproducibility)</summary>
          <KeyValues
            className="kv-wide"
            rows={[
              ["Report", <Mono>{report.report_id}</Mono>],
              ["Created", fmtUtc(report.created_at) ?? <NotRecorded />],
              ["RECAP version", orNone(report.recap_version)],
              ["Git commit", orNone(report.git_commit)],
              ["Config hash", orNone(report.config_hash)],
              ["Fixture hash", orNone(report.fixture_hash)],
              ["Decision provider", orNone(report.provider)],
              [
                "Split",
                report.split ? (
                  <span>
                    <Mono>{report.split.name}</Mono> · train {fmtCount(report.split.train.length)} · test {fmtCount(report.split.test.length)} · red-team{" "}
                    {fmtCount(report.split.redteam.length)} scenarios
                  </span>
                ) : (
                  <NotRecorded />
                ),
              ],
              [
                "Simulated tool latency",
                Object.entries(report.latency ?? {}).length ? (
                  Object.entries(report.latency)
                    .map(([key, value]) => `${key.replace(/_ms$/, "")} ${value} ms`)
                    .join(" · ")
                ) : (
                  <NotRecorded />
                ),
              ],
            ]}
          />
        </details>
      </div>
    </Card>
  );
}

/** GET /evals/results for the scripted tab: the report's extras, or why there are none. */
export function BenchmarkReportSection({ results, batchId }: { results: Pick<Polled<EvalResults>, "data" | "error" | "loading" | "refresh">; batchId: string | null }) {
  const data = results.data;
  if (data?.report) return <BenchmarkReportCard report={data.report} batchId={batchId} />;
  if (data?.running) return null; // the progress card above says what is happening
  if (data) {
    return (
      <Card title="From the benchmark report" subtitle="GET /evals/results">
        <EmptyView
          title="No benchmark report on this server"
          reason={
            data.error
              ? "The last scripted regression this server ran failed (above), so there is no report to show."
              : "The repository has no evals/reports/latest/ report, and this server has not run the scripted regression."
          }
        />
      </Card>
    );
  }
  if (results.error) {
    return <ErrorView error={results.error} title="Could not load the benchmark report (GET /evals/results)" onRetry={results.refresh} />;
  }
  return <LoadingView label="Loading the benchmark report…" />;
}

// --- commands (terminal only for real models) --------------------------------------------------

export function CommandsCard({ mode }: { mode: dto.EvalMode }) {
  const commands = CLI_COMMANDS[mode];
  const head: Record<typeof mode, { title: string; subtitle: string }> = {
    scripted_regression: {
      title: "The same regression from a terminal",
      subtitle: "Run from the repository root with its virtual environment.",
    },
    offline_replay: {
      title: "Replay the recordings again",
      subtitle: "Offline replays are made from a terminal, from the repository root: no model, no network, $0.",
    },
    real_llm: {
      title: "Run a real-model batch: from a terminal only",
      subtitle:
        "The browser never starts a real-model batch. It takes minutes to hours of local compute or free-plan quota, so a person starts each batch at a terminal, with explicit consent for a hosted free plan.",
    },
    real_kubernetes: {
      title: "Run on a real cluster: from a terminal, with your approval",
      subtitle:
        "The Kind demo cluster or SREGym's own: a person starts each run and approves its fix by typing the target service's name, then the batch is built at a terminal from the stored runs. This page never starts a run or builds a batch.",
    },
  };
  return (
    <Card title={head[mode].title} subtitle={head[mode].subtitle}>
      {mode === "real_llm" ? (
        <ul className="evl-guards" aria-label="Cost controls checked before anything runs">
          <li>
            <Icon name="shield" size={15} />
            <span>
              <strong>Never in CI.</strong> With the <Mono>CI</Mono> environment variable set, no live run starts (exit 2). CI replays recordings
              instead.
            </span>
          </li>
          <li>
            <Icon name="key" size={15} />
            <span>
              <strong>Consent for every hosted free-plan batch.</strong> <Mono>--confirm-free-plan</Mono> on each run of the script, on top of{" "}
              <Mono>RECAP_ALLOW_LIVE_FREE_INFERENCE=1</Mono>, the key in <Mono>GROQ_API_KEY</Mono> (never shown on this page) and the owner's dated plan
              check in <Mono>RECAP_GROQ_FREE_PLAN_CONFIRMED_ON</Mono>.
            </span>
          </li>
          <li>
            <Icon name="stop" size={15} />
            <span>
              <strong>A quota stop ends the batch cleanly.</strong> The stopped run is recorded (<Mono>quota_cap</Mono>), the plan's later runs are not
              started and are named in the batch's limitations (exit 3).
            </span>
          </li>
          <li>
            <Icon name="save" size={15} />
            <span>
              <strong>Publishing fails closed.</strong> <Mono>--publish</Mono> also writes the batch to <Mono>evals/reports/batches/</Mono>, and refuses a
              batch that names a local folder or holds a value shaped like a secret.
            </span>
          </li>
        </ul>
      ) : null}
      {mode === "real_kubernetes" ? (
        <ul className="evl-guards" aria-label="What every run on the cluster is held to">
          <li>
            <Icon name="key" size={15} />
            <span>
              <strong>A person approves every write.</strong> The fix waits until its operator types the target service's exact name: at the terminal a
              plain <Mono>y</Mono> rejects, and through the dashboard or the API an approval without that name decides nothing. The approval is bound
              to that one proposal, single use.
            </span>
          </li>
          <li>
            <Icon name="shield" size={15} />
            <span>
              <strong>No hosted model sees the cluster.</strong> A paid provider is always refused on a live target, a free hosted plan unless the owner
              allows it; the model runs on the owner's own machine or VM. Never in CI.
            </span>
          </li>
          <li>
            <Icon name="check" size={15} />
            <span>
              <strong>The cluster judges the recovery.</strong> RECAP's oracle reads the cluster itself, never the model: rollout and readiness, plus
              an HTTP probe on the Kind demo, or containers that stay up on SREGym's cluster.
            </span>
          </li>
          <li>
            <Icon name="flag" size={15} />
            <span>
              <strong>SREGym's grader stays apart.</strong> Its verdicts (<Mono>run-oracle.py</Mono>) are copied into the batch by hand and counted next
              to RECAP's verified recoveries, never merged into them. A person approved every fix, so these runs are not comparable with SREGym's
              leaderboard.
            </span>
          </li>
        </ul>
      ) : null}
      <ul className="evl-commands">
        {commands.map((command) => (
          <li key={command.command} className="evl-command">
            <div className="evl-command-head">
              <span className="evl-command-title">{command.title}</span>
              <CopyButton text={command.command} what={command.title} />
            </div>
            <pre className="evl-command-text">
              <code>{command.command}</code>
            </pre>
            <p className="evl-help">{command.note}</p>
          </li>
        ))}
      </ul>
      {mode === "offline_replay" ? (
        <p className="evl-help">
          To watch one recording replay as a run, step by step, start it from the <a href={hrefFor("live")}>Incident Lab</a> (signed in).
        </p>
      ) : null}
    </Card>
  );
}

// --- the environment adapters (the real cluster's tab) ----------------------------------------------------------------------

function adapterTone(status: string): { tone: Tone; icon: IconName } {
  switch (status) {
    case "active":
      return { tone: "ok", icon: "check" };
    case "available":
      return { tone: "info", icon: "info" };
    case "planned":
      return { tone: "neutral", icon: "clock" };
    case "unavailable":
      return { tone: "warn", icon: "alert" };
    case "error":
      return { tone: "bad", icon: "x" };
    default:
      return { tone: "neutral", icon: "minus" };
  }
}

export function AdapterTable({ adapters }: { adapters: readonly dto.AdapterStatus[] | null | undefined }) {
  if (!adapters) {
    return <EmptyView title="Not recorded" reason="This server does not report its environment adapters (GET /system environment_adapters)." />;
  }
  return (
    <DataTable
      rows={[...adapters]}
      rowKey={(a) => a.kind}
      className="dense"
      empty="The server reports no environment adapter."
      columns={[
        { key: "kind", header: "Target", render: (a) => <Mono>{a.kind}</Mono> },
        { key: "adapter", header: "Adapter", render: (a) => (a.adapter ? <Mono>{a.adapter}</Mono> : <span className="muted">None in this repository</span>) },
        {
          key: "status",
          header: "Status",
          render: (a) => {
            const look = adapterTone(a.status);
            return <StatusToken size="sm" tone={look.tone} icon={look.icon} label={a.status} detail={a.milestone ?? undefined} />;
          },
        },
        { key: "operational", header: "Operational", render: (a) => (a.operational ? "yes" : "no") },
        { key: "detail", header: "What the server says", render: (a) => <span className="evl-adapter-detail">{a.detail}</span> },
      ]}
    />
  );
}

export function AdaptersView({ system }: { system: Pick<Polled<dto.SystemView>, "data" | "error" | "loading" | "refresh"> }) {
  return (
    <Card
      title="Environment adapters on this server"
      subtitle="What GET /system reports for each target environment. Only the adapter this server is configured with runs incidents here (operational); GET /system never contacts a cluster."
      flush
    >
      <PolledView polled={system} loadingLabel="Loading the environment adapters…" errorTitle="Could not load GET /system">
        {(view) => <AdapterTable adapters={view.environment_adapters} />}
      </PolledView>
    </Card>
  );
}

function AdaptersPanel() {
  const system = usePolling(() => api.systemView(), null, []);
  return <AdaptersView system={system} />;
}

// --- the page ---------------------------------------------------------------------------------------

export function EvalsBody({
  view,
  mode,
  requestedBatch,
  recordId,
  results,
  signedIn,
  onParams,
}: {
  view: dto.EvalsView;
  mode: dto.EvalMode;
  requestedBatch: string | null;
  recordId: string | null;
  results: Polled<EvalResults>;
  signedIn: boolean;
  onParams: (params: Params) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startedHere, setStartedHere] = useState(false);
  const reportBefore = useRef<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [startError, setStartError] = useState<ApiError | null>(null);

  const batches = batchesOf(view, mode);
  const batch = pickBatch(batches, requestedBatch);
  const info = modeInfo(view, mode);
  const running = !!results.data?.running;

  async function start() {
    setStarting(true);
    setStartError(null);
    setNote(null);
    reportBefore.current = results.data?.report?.report_id ?? null;
    try {
      const response = await api.startEvaluation();
      setStartedHere(true);
      setNote(
        response.status === "already running"
          ? "A scripted regression is already running on the server."
          : `Started: the report goes to ${response.report_dir}.`,
      );
      results.refresh();
    } catch (err) {
      // A 401 already carries the sign-in hint (api.ts).
      setStartError(toApiError(err));
    } finally {
      setStarting(false);
      setConfirming(false);
    }
  }

  // A mode the server reports as planned has no batch to show.
  const plannedOnly = info?.status === "planned";
  const howId = `${panelId(mode)}-how`;

  return (
    <>
      <ModeTabs view={view} mode={mode} onSelect={(m) => onParams({ mode: m, batch: null, record: null })} />
      <div className="evl-panel" role="tabpanel" id={panelId(mode)} aria-labelledby={tabId(mode)}>
        {plannedOnly ? (
          <ModeCard mode={mode} info={info}>
            <EmptyView icon="package" title="No results, by design" reason="The server reports this mode as planned: no batch can be made for it yet." />
          </ModeCard>
        ) : (
          <>
            <ModeCard
              mode={mode}
              info={info}
              actions={
                mode === "scripted_regression" ? (
                  <LaunchButton signedIn={signedIn} running={running} starting={starting} onClick={() => setConfirming(true)} />
                ) : (
                  <button
                    type="button"
                    className="btn btn-small"
                    aria-controls={howId}
                    onClick={() => {
                      const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
                      document.getElementById(howId)?.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" });
                    }}
                  >
                    <Icon name="code" size={13} />
                    How to run a batch
                  </button>
                )
              }
            >
              <BatchPicker
                mode={mode}
                batches={batches}
                selectedId={batch?.batch_id ?? null}
                onSelect={(batchId) => onParams({ batch: batchId, record: null })}
              />
            </ModeCard>
            {mode === "scripted_regression" ? (
              <EvaluationStatus results={results} startedHere={startedHere} reportBefore={reportBefore.current} note={note} startError={startError} />
            ) : null}
            {batch ? <BatchSection key={batch.batch_id} batchId={batch.batch_id} recordId={recordId} onParams={onParams} /> : null}
            <div id={howId} className="evl-anchor">
              <CommandsCard mode={mode} />
            </div>
            {mode === "real_kubernetes" ? <AdaptersPanel /> : null}
            {mode === "scripted_regression" ? <BenchmarkReportSection results={results} batchId={batch?.batch_id ?? null} /> : null}
            {mode === "scripted_regression" ? (
              <LaunchConfirm open={confirming} starting={starting} onCancel={() => setConfirming(false)} onConfirm={() => void start()} />
            ) : null}
          </>
        )}
      </div>
    </>
  );
}

export function EvalsPage({ route: given }: { route?: Route } = {}) {
  const hashRoute = useHashRoute();
  const route = given ?? hashRoute;
  const mode = parseMode(route.params.get("mode"));
  const requestedBatch = route.params.get("batch");
  const recordId = route.params.get("record");

  const evals = usePolling(() => api.evals(), null, []);
  const results = usePolling(() => api.evalResults(), (data) => (data?.running ? 1500 : null), []);
  const signedIn = useOperatorToken() !== null;

  // An evaluation this server ran has finished: its batch is new, so reload the list.
  const running = !!results.data?.running;
  const wasRunning = useRef(running);
  const { refresh: refreshEvals } = evals;
  useEffect(() => {
    if (wasRunning.current && !running) refreshEvals();
    wasRunning.current = running;
  }, [running, refreshEvals]);

  const onParams = (next: Params) => navigate("evals", { ...paramsOf(route), ...next }, true);

  return (
    <div className="evl-page">
      <PageHeader
        title="Evaluations"
        description="How RECAP's runs are evaluated, one batch at a time. Each mode is a different kind of evidence, so each has its own tab and their numbers are never combined."
      />
      <div className="honesty-banner evl-honesty" role="note">
        <Icon name="flask" size={16} />
        <span>
          Simulated shop, or a real Kind cluster the owner runs (never production) · four modes, never combined · counts “k of N”, never rates · small
          samples, no statistical claims
        </span>
      </div>
      <PolledView
        polled={evals}
        loadingLabel="Loading the evaluation batches…"
        errorTitle="Could not load the evaluation batches"
      >
        {(view) => (
          <EvalsBody
            view={view}
            mode={mode}
            requestedBatch={requestedBatch}
            recordId={recordId}
            results={results}
            signedIn={signedIn}
            onParams={onParams}
          />
        )}
      </PolledView>
    </div>
  );
}
