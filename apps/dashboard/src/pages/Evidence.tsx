import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, toApiError } from "../api";
import type { ApiError } from "../api";
import { Chip, FlagChip, HypothesisChip, Mono } from "../components/Badge";
import { Card, KeyValues, Meter, Notice, PageHeader } from "../components/Card";
import { EmptyState, ErrorState, Loading } from "../components/EmptyState";
import { Icon } from "../components/Icon";
import { JsonView } from "../components/JsonView";
import { RunScope } from "../components/RunPicker";
import { DataTable } from "../components/Table";
import { arr, fmtDateTime, fmtInt, fmtScore, isTerminal, str } from "../format";
import { usePolling, useRunSelection } from "../hooks";
import type { PageProps } from "../hooks";
import type { DocRef, Evidence, HypothesisCheck, KnowledgeResult, RetrievalPayload, RetrievedChunk, RunRecord, TraceEvent } from "../types";

// --- evidence registry -------------------------------------------------------------------

function EvidenceTable({ evidence, focusId }: { evidence: Evidence[]; focusId: string | null }) {
  return (
    <DataTable
      rows={evidence}
      rowKey={(e) => e.id}
      initiallyOpen={focusId ? [focusId] : undefined}
      rowClassName={(e) => (e.flags.includes("prompt_injection_suspected") ? "row-warn" : e.id === focusId ? "row-focus" : undefined)}
      empty="No evidence recorded for this run yet."
      expand={(e) => (
        <div className="detail-grid">
          <div>
            <h3 className="mini-title">Content (what a model would read · {e.trust_level} data)</h3>
            <pre className="raw">{e.content}</pre>
          </div>
          <div>
            <h3 className="mini-title">Structured data (what the rules read)</h3>
            <JsonView value={e.data} maxHeight={300} />
            <div className="muted small spaced">
              recorded {fmtDateTime(e.timestamp)} · incident <Mono>{e.incident_id}</Mono>
            </div>
          </div>
        </div>
      )}
      columns={[
        { key: "id", header: "Evidence", nowrap: true, render: (e) => <Mono>{e.id}</Mono> },
        {
          key: "tool",
          header: "Tool / source",
          render: (e) => (
            <span className="cell-stack">
              {e.tool_name ? <Mono>{e.tool_name}</Mono> : null}
              {/* Tool results are the common case; other sources (doc, memory, alert, oracle) get a chip. */}
              {e.source_type !== "tool" || !e.tool_name ? <Chip>{e.source_type}</Chip> : null}
            </span>
          ),
        },
        {
          key: "ref",
          header: "Source ref",
          render: (e) => (
            <code className="mono truncate" title={e.source_ref}>
              {e.source_ref}
            </code>
          ),
        },
        { key: "agent", header: "Agent", render: (e) => (e.agent_role ? <Mono>{e.agent_role}</Mono> : <span className="muted">—</span>) },
        {
          key: "trust",
          header: "Trust",
          render: (e) =>
            e.trust_level === "trusted" ? (
              <Chip tone="accent">
                <Icon name="lock" size={12} /> trusted
              </Chip>
            ) : (
              <Chip>untrusted</Chip>
            ),
        },
        {
          key: "flags",
          header: "Flags",
          render: (e) =>
            e.flags.length ? (
              <span className="chip-row">
                {e.flags.map((f) => (
                  <FlagChip key={f} flag={f} />
                ))}
              </span>
            ) : (
              <span className="muted">—</span>
            ),
        },
        { key: "tokens", header: "Est. tokens", numeric: true, render: (e) => fmtInt(e.token_estimate) },
      ]}
    />
  );
}

// --- hypothesis verification -------------------------------------------------------------

function CheckRow({ check }: { check: HypothesisCheck }) {
  const h = check.hypothesis;
  return (
    <div className="check">
      <div className="check-head">
        <HypothesisChip status={h.status} />
        <span className="check-claim">{h.claim}</span>
      </div>
      <div className="chip-row">
        <Mono>{h.mechanism}</Mono>
        <span className="muted">on</span>
        <Mono>{h.target_service ?? "?"}</Mono>
        <Chip tone={h.origin === "memory" ? "info" : "neutral"}>origin: {h.origin}</Chip>
        {h.proposed_by ? <Chip mono>{h.proposed_by}</Chip> : null}
      </div>
      <ul className="rule-list">
        {check.passed.map((r, i) => (
          <li key={`p${i}`} className="text-ok">
            <Icon name="check" size={13} /> {r}
          </li>
        ))}
        {check.failed.map((r, i) => (
          <li key={`f${i}`} className="text-bad">
            <Icon name="x" size={13} /> {r}
          </li>
        ))}
      </ul>
      <div className="chip-row">
        {check.rule_ids.map((id) => (
          <Chip key={id} mono>
            {id}
          </Chip>
        ))}
        {h.supporting_evidence_ids.length ? (
          <span className="muted small">supports: {h.supporting_evidence_ids.join(", ")}</span>
        ) : null}
        {h.contradicting_evidence_ids.length ? (
          <span className="text-bad small">contradicts: {h.contradicting_evidence_ids.join(", ")}</span>
        ) : null}
      </div>
    </div>
  );
}

function Verification({ record }: { record: RunRecord }) {
  const report = record.verification;
  if (!report) return <span className="muted">The evidence verifier has not run yet.</span>;
  return (
    <>
      <div className="verify-head">
        <HypothesisChip status={report.status} />
        <span>{report.summary || "—"}</span>
      </div>
      {report.best ? (
        <Notice tone="ok" icon="check" title="Diagnosis (best supported, actionable hypothesis)">
          {report.best.claim} · <Mono>{`${report.best.mechanism}@${report.best.target_service ?? "?"}`}</Mono>
        </Notice>
      ) : null}
      {report.unknown_evidence_ids.length ? (
        <Notice tone="bad" icon="x" title="Cited evidence that does not exist in this run (rejected)">
          {report.unknown_evidence_ids.join(", ")}
        </Notice>
      ) : null}
      {report.checks.length ? (
        report.checks.map((c, i) => <CheckRow key={i} check={c} />)
      ) : (
        <span className="muted">No hypotheses were checked.</span>
      )}
    </>
  );
}

// --- retrieval (citations) ------------------------------------------------------------------

function RetrievedDocs({ docs }: { docs: RetrievedChunk[] }) {
  const top = Math.max(0.0001, ...docs.map((d) => d.score));
  return (
    <DataTable
      rows={docs}
      rowKey={(d) => `${d.chunk.chunk_id}-${d.rank}`}
      empty="No runbook chunks were retrieved (RAG off, or retrieval skipped)."
      expand={(d) => (
        <div className="detail-grid">
          <div>
            <h3 className="mini-title">
              {d.chunk.title} › {d.chunk.heading} (untrusted text)
            </h3>
            <pre className="raw">{d.chunk.text}</pre>
          </div>
          <div>
            <KeyValues
              rows={[
                ["Checksum", <Mono>{d.chunk.checksum}</Mono>],
                ["Est. tokens", fmtInt(d.chunk.token_estimate)],
                ["Provenance", <JsonView value={d.chunk.provenance} maxHeight={160} />],
              ]}
            />
          </div>
        </div>
      )}
      columns={[
        { key: "rank", header: "Rank", numeric: true, render: (d) => d.rank },
        { key: "score", header: "Score", render: (d) => <Meter value={d.score} max={top} label={fmtScore(d.score)} width={70} /> },
        { key: "chunk", header: "Chunk", render: (d) => <Mono>{d.chunk.chunk_id}</Mono> },
        { key: "path", header: "Document", render: (d) => <Mono>{d.chunk.doc_path}</Mono> },
        { key: "heading", header: "Heading", render: (d) => d.chunk.heading },
        {
          key: "retriever",
          header: "Retriever",
          render: (d) => (
            <span className="cell-stack">
              <Mono>{d.retriever}</Mono>
              {d.reranker ? <span className="muted small">reranker {d.reranker}</span> : null}
            </span>
          ),
        },
      ]}
    />
  );
}

function RetrievalDecisions({ events }: { events: TraceEvent[] }) {
  const decisions = events.filter((e) => e.category === "retrieval.docs" || e.category === "retrieval.skip");
  if (!decisions.length) return null;
  return (
    <ul className="decision-list">
      {decisions.map((e) => {
        const p = e.payload as RetrievalPayload;
        const results = arr<DocRef>(p.results);
        const skipped = e.category === "retrieval.skip";
        return (
          <li key={e.seq}>
            <Chip tone={skipped ? "neutral" : "info"}>
              {str(p.stage, "?")} · {skipped ? "skipped" : `${results.length} retrieved`}
            </Chip>
            <span>{str(p.reason)}</span>
            {p.query ? (
              <span className="muted">
                query <Mono>{p.query}</Mono>
              </span>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

// --- knowledge search -----------------------------------------------------------------------

function KnowledgeSearch() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<KnowledgeResult[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);

  async function search(e: FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (q.length < 2) return;
    setBusy(true);
    setError(null);
    try {
      setResults(await api.knowledgeSearch(q, 5));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Knowledge search" subtitle="GET /knowledge/search — BM25 over the runbooks, with citations, parsed action hints and injection flags">
      <form className="search-row" onSubmit={(e) => void search(e)}>
        <Icon name="search" size={16} />
        <input
          className="input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. rollback after a bad release, OOMKilled restart, upstream dependency errors"
          aria-label="Search the knowledge base"
        />
        <button type="submit" className="btn btn-primary" disabled={busy || query.trim().length < 2}>
          {busy ? "Searching…" : "Search"}
        </button>
      </form>
      {error ? <ErrorState error={error} /> : null}
      {results && results.length === 0 ? <EmptyState title="No matching chunks" icon="search" /> : null}
      {results && results.length > 0 ? (
        <DataTable
          rows={results}
          rowKey={(r) => `${r.chunk.chunk_id}-${r.rank}`}
          expand={(r) => <pre className="raw">{r.chunk.text}</pre>}
          columns={[
            { key: "rank", header: "Rank", numeric: true, render: (r) => r.rank },
            { key: "score", header: "Score", numeric: true, render: (r) => fmtScore(r.score) },
            {
              key: "heading",
              header: "Citation",
              render: (r) => (
                <span className="cell-stack">
                  <span>
                    {r.chunk.title} › {r.chunk.heading}
                  </span>
                  <span className="muted small">
                    <Mono>{r.chunk.doc_path}</Mono> · <Mono>{r.chunk.chunk_id}</Mono>
                  </span>
                </span>
              ),
            },
            {
              key: "hints",
              header: "Action hints",
              render: (r) =>
                r.action_hints.length ? (
                  <span className="chip-row">
                    {r.action_hints.map((h, i) => (
                      <Chip key={i} tone="accent" mono>
                        {h.mechanism} → {h.action}
                      </Chip>
                    ))}
                  </span>
                ) : (
                  <span className="muted">—</span>
                ),
            },
            {
              key: "flags",
              header: "Flags",
              render: (r) =>
                r.flags.length ? (
                  <span className="chip-row">
                    {r.flags.map((f) => (
                      <FlagChip key={f} flag={f} />
                    ))}
                  </span>
                ) : (
                  <span className="muted">—</span>
                ),
            },
          ]}
        />
      ) : null}
    </Card>
  );
}

// --- page -------------------------------------------------------------------------------------

function EvidenceView({ runId, running, focusId }: { runId: string; running: boolean; focusId: string | null }) {
  const detail = usePolling(() => api.run(runId), (d) => (d?.status === "running" || (running && !d?.record) ? 2000 : null), [runId]);
  const events = usePolling(() => api.events(runId), (list) => (list?.some((e) => e.category === "run.summary") || !running ? null : 2500), [runId]);
  const { refresh: refreshEvents } = events;
  useEffect(() => {
    if (running) refreshEvents();
  }, [running, refreshEvents]);

  if (detail.error && !detail.data) return <ErrorState error={detail.error} />;
  if (!detail.data) return <Loading label="Loading run checkpoint and evidence…" />;
  const record = detail.data.record;
  if (!record) {
    return (
      <EmptyState title="No checkpoint yet" icon="clock">
        {detail.data.error ?? "The run has not saved its first checkpoint."}
      </EmptyState>
    );
  }
  const evidence = detail.data.evidence;
  const flagged = evidence.filter((e) => e.flags.includes("prompt_injection_suspected")).length;

  return (
    <>
      <Card
        title={`Evidence registry (${evidence.length})`}
        subtitle="Immutable observations with run-unique ids. Agents and the verifier pass these ids around, never mutable objects. Click a row for the raw content."
        actions={flagged ? <Chip tone="warn"><Icon name="alert" size={12} /> {flagged} injection-flagged</Chip> : undefined}
        flush
      >
        <EvidenceTable evidence={evidence} focusId={focusId} />
      </Card>

      <Card title="Hypothesis verification" subtitle="record.verification — every claim must cite evidence ids recorded in this run">
        <Verification record={record} />
      </Card>

      <Card title="Citations: retrieved runbook chunks" subtitle="record.retrieved_docs — source-labeled RAG results the agents could see" flush>
        <div className="card-pad">
          <RetrievalDecisions events={events.data ?? []} />
        </div>
        <RetrievedDocs docs={record.retrieved_docs} />
      </Card>
    </>
  );
}

export function EvidencePage({ route }: PageProps) {
  const selection = useRunSelection(route);
  return (
    <>
      <PageHeader
        title="Evidence & RAG"
        description="What the agents observed (tool outputs are untrusted data), how each hypothesis was checked against that evidence, and which runbooks were cited."
      />
      <RunScope selection={selection} page="evidence">
        {(runId, summary) => (
          <EvidenceView runId={runId} running={!!summary && !isTerminal(summary.state)} focusId={route.params.get("ev")} />
        )}
      </RunScope>
      <KnowledgeSearch />
    </>
  );
}
