import { api } from "../api";
import { Mono } from "../components/Badge";
import { Card, Notice, PageHeader } from "../components/Card";
import { Icon } from "../components/Icon";
import { PolledView } from "../components/StateViews";
import { StatusToken } from "../components/StatusToken";
import type * as dto from "../dto";
import { hrefFor, usePolling } from "../hooks";
import type { PageProps, Polled } from "../hooks";
import "../styles/published.css";
import { categoryInfo } from "../view/evals";
import { HISTORICAL_LABEL, dayOf, graderView, realClusterRows, rowCounts, targetName } from "../view/published";
import type { RealClusterRow } from "../view/published";
import { repoUrl } from "../view/system";

/**
 * Published Runs: M5's real AI-driven incident recoveries on Kubernetes (Kind and SREGym),
 * published with the repository and readable on a fresh checkout, offline (GET /published).
 * The server shows a run only after checking its files against evals/live/MANIFEST.json
 * (evals/published.py); each one opens in Trace Explorer, Evidence & Decisions and Safety &
 * Approvals, marked historical and read-only. Runs whose trace was not published (the
 * failures among them) are listed from their evaluation records, and an entry that failed its
 * checks is named with the reason. Nothing here can start, approve or change anything.
 */

type Loaded<T> = Pick<Polled<T>, "data" | "error" | "loading" | "refresh">;

const RUN_PAGES = [
  ["timeline", "Trace Explorer", "Every model answer, tool call and policy decision, in order"],
  ["evidence", "Evidence & Decisions", "The claims, the verifier's verdicts and the proposed fix"],
  ["safety", "Safety & Approvals", "The policy's verdicts, the approval and the write"],
] as const;

function OutcomeTokens({ run }: { run: dto.RunSummary }) {
  const provenance = run.published;
  const grader = provenance ? graderView(provenance.target, provenance.benchmark_verdict, provenance.benchmark) : null;
  return (
    <div className="chip-row">
      <StatusToken tone={run.recovered ? "ok" : "bad"} icon={run.recovered ? "check" : "x"} label={run.state} size="sm" />
      <StatusToken
        tone={run.oracle?.verified ? "ok" : "neutral"}
        icon={run.oracle?.verified ? "check" : "minus"}
        label={run.oracle?.verified ? "RECAP's oracle: recovery verified" : "RECAP's oracle: not verified"}
        title={run.oracle ? `${run.oracle.oracle}, checked on its own reads` : "No fix reached the oracle"}
        size="sm"
        variant="outline"
      />
      {grader ? <StatusToken tone={grader.tone} icon={grader.tone === "ok" ? "check" : "minus"} label={grader.label} title={grader.detail} size="sm" variant="outline" /> : null}
    </div>
  );
}

/** One published run: what it was, its provenance, and the ways into its detailed views. */
export function PublishedRunCard({ run }: { run: dto.RunSummary }) {
  const provenance = run.published;
  if (!provenance) return null;
  return (
    <article className="pub-run" aria-labelledby={`pub-${run.run_id}`}>
      <div className="pub-run-head">
        <StatusToken tone="bad" icon="alert" label={targetName(provenance.target)} detail="real cluster" size="sm" />
        <span className="muted small">{dayOf(run.started_at)}</span>
      </div>
      <h3 className="pub-run-title" id={`pub-${run.run_id}`}>
        {provenance.title}
      </h3>
      <OutcomeTokens run={run} />
      <dl className="pub-facts">
        <div>
          <dt>Model</dt>
          <dd>
            {run.model ?? "Not recorded"}
            {run.provider ? <span className="muted"> · {run.provider}</span> : null}
          </dd>
        </div>
        <div>
          <dt>Approval</dt>
          <dd>{provenance.approvals.length ? provenance.approvals.join("; ") : "Not recorded"}</dd>
        </div>
        <div>
          <dt>Run</dt>
          <dd>
            <Mono>{run.run_id}</Mono> · scenario <Mono>{run.scenario_id}</Mono>
          </dd>
        </div>
      </dl>
      {provenance.notes.length ? (
        <ul className="pub-notes">
          {provenance.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}
      <nav className="pub-links" aria-label={`Open run ${run.run_id}`}>
        {RUN_PAGES.map(([page, label, hint]) => (
          <a key={page} className={page === "timeline" ? "btn btn-small btn-primary" : "btn btn-small"} href={hrefFor(page, { run: run.run_id })} title={hint}>
            {label}
          </a>
        ))}
        <a className="btn btn-small btn-ghost" href={hrefFor("demo", { run: run.run_id })} title="The same trace as ten plain-language steps">
          <Icon name="play" size={13} /> Step by step
        </a>
      </nav>
      <p className="pub-files small muted">
        <Icon name="check" size={13} /> Files checked against their pinned SHA-256:{" "}
        <a href={repoUrl(`${provenance.folder}/`)} target="_blank" rel="noopener noreferrer">
          <Mono>{provenance.folder}</Mono>
          <span className="sr-only"> (opens on GitHub)</span>
        </a>
      </p>
    </article>
  );
}

function StateCell({ row }: { row: RealClusterRow }) {
  if (row.verified) return <StatusToken tone="ok" icon="check" label={row.finalState} size="sm" />;
  return <StatusToken tone={row.finalState === "BLOCKED" ? "warn" : "bad"} icon={row.finalState === "BLOCKED" ? "stop" : "x"} label={row.finalState} size="sm" />;
}

/** What happened, in the evaluation's words: its failure category first (explained on hover), then the detail. */
function DetailCell({ row }: { row: RealClusterRow }) {
  const info = row.failure ? categoryInfo(row.failure) : null;
  return (
    <>
      {info ? (
        <strong className="pub-category" title={info.meaning}>
          {info.label}.{" "}
        </strong>
      ) : null}
      {row.detail || "Not recorded"}
      {row.approvals.length ? <span className="muted small pub-approvals">{row.approvals.join("; ")}</span> : null}
    </>
  );
}

/** Every real-cluster run of one target, failures included, oldest first. */
export function RealClusterTable({ target, rows }: { target: string; rows: RealClusterRow[] }) {
  const counts = rowCounts(rows);
  return (
    <section className="pub-table-section" aria-labelledby={`pub-target-${target}`}>
      <h3 className="pub-table-title" id={`pub-target-${target}`}>
        {targetName(target)}
        <span className="muted small">
          {" "}
          · {counts.runs} runs, {counts.verified} verified recoveries, {counts.traces} with a published trace (counts, not a rate)
        </span>
      </h3>
      <div className="table-wrap">
        <table className="table pub-table">
          <thead>
            <tr>
              <th scope="col">Started</th>
              <th scope="col">Run</th>
              <th scope="col">How it ended</th>
              <th scope="col">What happened</th>
              {target === "sregym" ? <th scope="col">SREGym's grader</th> : null}
              <th scope="col">Trace</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.runId}>
                <td className="nowrap">{dayOf(row.startedAt)}</td>
                <td>
                  <Mono nowrap>{row.runId}</Mono>
                </td>
                <td className="nowrap">
                  <StateCell row={row} />
                </td>
                <td className="pub-detail">
                  <DetailCell row={row} />
                </td>
                {target === "sregym" ? (
                  <td className="nowrap">
                    {row.grader && row.grader.tone !== "neutral" ? (
                      <StatusToken tone={row.grader.tone} label={row.grader.label.replace("SREGym's grader: ", "")} title={row.grader.detail} size="sm" variant="outline" />
                    ) : (
                      <span className="muted small">not graded</span>
                    )}
                  </td>
                ) : null}
                <td>
                  {row.traceOpens ? (
                    <a href={hrefFor("timeline", { run: row.runId })}>
                      Open <span className="sr-only">run {row.runId} in Trace Explorer</span>
                    </a>
                  ) : (
                    <span className="muted small nowrap">not published</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** The whole page for one GET /published answer (the page polls it once; tests pass it). */
export function PublishedRunsView({ published }: { published: Loaded<dto.PublishedView> }) {
  return (
    <div className="pub">
      <PageHeader
        title="Published Runs"
        description="Real AI-driven incident recoveries on Kubernetes, recorded in M5 and published with this repository. Open any of them here, offline: no cluster, no model and no cloud account are needed."
      />
      <div className="historical-banner" role="note" aria-label="What a published run is">
        <Icon name="package" size={20} />
        <div className="historical-body">
          <div className="historical-label">{HISTORICAL_LABEL}</div>
          <p className="historical-text">
            Each run here really happened: a self-hosted model investigated a real cluster through RECAP's read-only tools, a person approved the exact fix,
            and RECAP's independent oracle checked the recovery. What you see is that run's record. Nothing runs now: no model is called, no cluster is
            touched, and its approvals are records of a past decision, never permissions.
          </p>
        </div>
      </div>
      <PolledView
        polled={published}
        loadingLabel="Checking the published runs…"
        errorTitle="Could not load the published runs (GET /published)"
        emptyTitle="Nothing is published"
        empty={(view) => (view.runs.length || view.records_without_trace.length || view.manifest_error || view.rejected.length ? null : "evals/live/MANIFEST.json lists no run.")}
      >
        {(view) => (
          <>
            {view.manifest_error ? (
              <Notice tone="bad" icon="x" title="The list of published runs could not be read, so no run is shown">
                {view.manifest_error}
              </Notice>
            ) : null}
            {view.rejected.length ? (
              <Notice tone="bad" icon="shield" title={`${view.rejected.length} published ${view.rejected.length === 1 ? "run was" : "runs were"} refused, so ${view.rejected.length === 1 ? "it is" : "they are"} not shown`}>
                <ul className="pub-rejected">
                  {view.rejected.map((r) => (
                    <li key={`${r.folder}-${r.run_id}`}>
                      <Mono>{r.folder ?? r.run_id ?? "?"}</Mono>: {r.reason}
                    </li>
                  ))}
                </ul>
              </Notice>
            ) : null}
            {view.runs.length ? (
              <Card title="Runs you can open" subtitle="Their full traces were published; each was checked against the repository's pinned files before it is shown">
                <div className="pub-grid">
                  {view.runs.map((run) => (
                    <PublishedRunCard key={run.run_id} run={run} />
                  ))}
                </div>
              </Card>
            ) : null}
            <Card
              title="Every real-cluster run, failures included"
              subtitle="From the published real-Kubernetes evaluation batches: the runs above, and those known only from their evaluation records"
            >
              {realClusterRows(view).map((group) => (
                <RealClusterTable key={group.target} target={group.target} rows={group.rows} />
              ))}
            </Card>
            <details className="pub-limits">
              <summary>
                <Icon name="chevronRight" size={15} />
                What these published files do not hold
              </summary>
              <ul>
                {view.limitations.map((text) => (
                  <li key={text}>{text}</li>
                ))}
              </ul>
            </details>
          </>
        )}
      </PolledView>
    </div>
  );
}

export function PublishedPage(_props: PageProps) {
  const published = usePolling(() => api.published(), null, []);
  return <PublishedRunsView published={published} />;
}
