import type { ReactNode } from "react";
import { fmtDateTime, stateLabel } from "../format";
import { hrefFor } from "../hooks";
import type { PageId, RunSelection } from "../hooks";
import type { RunSummary } from "../types";
import { Chip, StateChip } from "./Badge";
import { ErrorState, Loading, NoRuns } from "./EmptyState";
import { Icon } from "./Icon";

function optionLabel(run: RunSummary): string {
  const label = run.label ? ` [${run.label}]` : "";
  return `${run.scenario_id}${label} · ${stateLabel(run.state)} · ${fmtDateTime(run.created_at)} · ${run.run_id}`;
}

export function RunPicker({
  runs,
  value,
  onChange,
}: {
  runs: RunSummary[];
  value: string | null;
  onChange: (runId: string) => void;
}) {
  const known = runs.some((r) => r.run_id === value);
  return (
    <label className="run-picker">
      <span className="field-label">Run</span>
      <select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
        {/* A deep-linked run older than the list is still selectable. */}
        {value && !known ? <option value={value}>{value}</option> : null}
        {runs.map((r) => (
          <option key={r.run_id} value={r.run_id}>
            {optionLabel(r)}
          </option>
        ))}
      </select>
    </label>
  );
}

const RUN_PAGES: { page: PageId; label: string }[] = [
  { page: "timeline", label: "Timeline" },
  { page: "evidence", label: "Evidence" },
  { page: "memory", label: "Memory" },
  { page: "safety", label: "Safety" },
];

/** Links from one run-scoped page to the others, keeping the same run selected. */
export function RunLinks({ runId, current }: { runId: string; current?: PageId }) {
  return (
    <nav className="run-links" aria-label="This run on other pages">
      {RUN_PAGES.filter((p) => p.page !== current).map((p) => (
        <a key={p.page} href={hrefFor(p.page, { run: runId })}>
          {p.label}
          <Icon name="chevronRight" size={12} />
        </a>
      ))}
    </nav>
  );
}

/**
 * The run selector bar shown at the top of every run-scoped page. Renders the loading,
 * error and "no runs yet" states itself; `children` render only when a run is selected.
 */
export function RunScope({
  selection,
  page,
  children,
}: {
  selection: RunSelection;
  page: PageId;
  children: (runId: string, summary: RunSummary | undefined) => ReactNode;
}) {
  const { runs, runId, select } = selection;
  if (runs.loading && !runs.data) return <Loading label="Loading runs…" />;
  if (runs.error && !runs.data) return <ErrorState error={runs.error} />;
  const list = runs.data ?? [];
  if (!runId) return <NoRuns />;
  const summary = list.find((r) => r.run_id === runId);
  return (
    <>
      <div className="run-scope">
        <RunPicker runs={list} value={runId} onChange={select} />
        <div className="run-scope-meta">
          {summary ? <StateChip state={summary.state} /> : null}
          {summary?.modes.map((m, i) => (
            <Chip key={`${m}-${i}`} tone="accent" mono>
              {m}
            </Chip>
          ))}
          {summary?.pending_approval ? <Chip tone="warn">awaiting approval</Chip> : null}
          <RunLinks runId={runId} current={page} />
        </div>
      </div>
      {children(runId, summary)}
    </>
  );
}
