import { useEffect, useId, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "../api";
import { Chip, FlagChip, Mono } from "../components/Badge";
import { Card, KeyValues, Notice, PageHeader } from "../components/Card";
import { Icon } from "../components/Icon";
import { LogView } from "../components/LogView";
import { EmptyView, PolledView } from "../components/StateViews";
import { StatusToken } from "../components/StatusToken";
import { fmtInt, hybridReadiness, searchModeLabel } from "../format";
import { PAGE_LABELS, hrefFor, navigate, paramsOf, usePolling } from "../hooks";
import type { PageProps, Route } from "../hooks";
import type { KnowledgeDocRef, KnowledgeResult, SystemSummary } from "../types";
import { fmtMeasure, fmtRank, isOverlayPath, modeNote, shortDigest, signalSummary } from "../view/retrieval";

/**
 * Knowledge / agentic RAG: the runbook index the retriever searches (GET /knowledge/docs),
 * how a run searches it by default (GET /system), and the same search for a person to try
 * (GET /knowledge/search). A field the server does not report reads NOT_RECORDED.
 *
 * Runbook text is untrusted data (the "Context & experience" layer): it is shown as text in a
 * bounded box, with the injection flags the server found, and never followed.
 */

export const NOT_RECORDED = "Not recorded";

/** How many results a search asks for (GET /knowledge/search `k`). */
export const RESULT_COUNTS = [3, 5, 10] as const;
const DEFAULT_COUNT = 5;

export function resultCount(raw: string | null | undefined): number {
  const n = Number(raw);
  return (RESULT_COUNTS as readonly number[]).includes(n) ? n : DEFAULT_COUNT;
}

export interface RunbookSection {
  chunkId: string;
  heading: string;
}

export interface RunbookDocument {
  path: string;
  title: string;
  sections: RunbookSection[];
}

/** GET /knowledge/docs (one row per indexed section) grouped by document, in the server's order. */
export function groupRunbooks(docs: readonly KnowledgeDocRef[]): RunbookDocument[] {
  const byPath = new Map<string, RunbookDocument>();
  for (const doc of docs) {
    let entry = byPath.get(doc.doc_path);
    if (!entry) {
      entry = { path: doc.doc_path, title: doc.title, sections: [] };
      byPath.set(doc.doc_path, entry);
    }
    entry.sections.push({ chunkId: doc.chunk_id, heading: doc.heading });
  }
  return [...byPath.values()];
}

function NotRecorded() {
  return <span className="not-recorded">{NOT_RECORDED}</span>;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function scalar(value: unknown): string | null {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/**
 * GET /system `features` (added by the typed M4 API: `vector_index`, `knowledge_overlays`, ...),
 * or null from a server that does not report it; its fields then read Not recorded.
 */
export function systemFeatures(system: SystemSummary): Record<string, unknown> | null {
  return asRecord((system as unknown as Record<string, unknown>).features);
}

/** The stored runbook vectors' identity (features.vector_index), as one line under the embedder. */
export function vectorIndexLine(index: Record<string, unknown>): string {
  const weights = scalar(index.weights);
  return [
    scalar(index.dimensions) ? `${scalar(index.dimensions)} dimensions` : null,
    scalar(index.chunks) ? `${scalar(index.chunks)} sections embedded` : null,
    scalar(index.model) ? `model ${scalar(index.model)}` : null,
    weights ? `weights ${shortDigest(weights)}` : null,
    scalar(index.ollama_version) ? `Ollama ${scalar(index.ollama_version)}` : null,
    scalar(index.created_at) ? `built ${scalar(index.created_at)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

function VectorIndex({ features }: { features: Record<string, unknown> | null }) {
  if (!features || !("vector_index" in features)) return <NotRecorded />;
  const index = asRecord(features.vector_index);
  if (!index) return <span className="muted">No stored vectors reported by the server</span>;
  const line = vectorIndexLine(index);
  return (
    <span className="cell-stack">
      {scalar(index.embedder) ? <Mono>{scalar(index.embedder)}</Mono> : <NotRecorded />}
      {line ? <span className="muted small">{line}</span> : null}
    </span>
  );
}

function Overlays({ features }: { features: Record<string, unknown> | null }) {
  const overlays = features?.knowledge_overlays;
  if (!Array.isArray(overlays)) return <NotRecorded />;
  const folders = overlays.filter((folder): folder is string => typeof folder === "string" && folder.trim() !== "");
  if (!folders.length) return <span>None</span>;
  return (
    <span className="chip-row">
      {folders.map((folder) => (
        <Chip key={folder} tone="warn" mono title="An extra knowledge folder indexed for red-team tests: its runbooks may be deliberately wrong">
          {folder}
        </Chip>
      ))}
    </span>
  );
}

/** GET /system: the default search mode, whether hybrid can run, and the index's identity. */
export function RetrievalConfig({ system }: { system: SystemSummary }) {
  const hybrid = hybridReadiness(system);
  const features = systemFeatures(system);
  const rows: [ReactNode, ReactNode][] = [
    [
      "Default search mode",
      system.retrieval_mode ? (
        <span className="cell-stack">
          <span>{searchModeLabel(system.retrieval_mode, true)}</span>
          <span className="muted small">{modeNote(system.retrieval_mode)}</span>
        </span>
      ) : (
        <NotRecorded />
      ),
    ],
    [
      "Default retriever",
      system.retriever ? (
        <span className="cell-stack">
          <Mono>{system.retriever}</Mono>
          <span className="muted small">what a run that does not choose a mode searches with</span>
        </span>
      ) : (
        <NotRecorded />
      ),
    ],
    [
      "Hybrid search",
      hybrid === "ready" ? (
        <span className="cell-stack">
          <StatusToken tone="ok" label="Ready" />
          <span className="muted small">stored vectors cover every runbook section</span>
        </span>
      ) : hybrid === "unavailable" ? (
        <span className="cell-stack">
          <StatusToken tone="warn" label="Unavailable" />
          <span className="muted small">stored vectors are missing or incomplete</span>
        </span>
      ) : (
        <NotRecorded />
      ),
    ],
    ["Embedding model (stored vectors)", system.embedder ? <Mono>{system.embedder}</Mono> : <NotRecorded />],
    ["Vector index", <VectorIndex features={features} />],
    ["Knowledge overlays", <Overlays features={features} />],
    [
      "Indexed sections",
      typeof system.knowledge_chunks === "number" ? fmtInt(system.knowledge_chunks) : <NotRecorded />,
    ],
    [
      "Fixtures fingerprint",
      system.fixture_hash ? (
        <span className="cell-stack">
          <Mono title={system.fixture_hash}>{shortDigest(system.fixture_hash, 16)}</Mono>
          <span className="muted small">scenarios and runbooks, recorded on every run</span>
        </span>
      ) : (
        <NotRecorded />
      ),
    ],
  ];
  return <KeyValues className="kv-wide kb-config" rows={rows} />;
}

/** The documents and sections the retriever indexed (citations name sections by chunk id). */
export function RunbookIndex({ docs }: { docs: readonly KnowledgeDocRef[] }) {
  const documents = groupRunbooks(docs);
  return (
    <div className="kb-index">
      <p className="muted small kb-index-count">
        {fmtInt(documents.length)} document{documents.length === 1 ? "" : "s"} · {fmtInt(docs.length)} section
        {docs.length === 1 ? "" : "s"} indexed by the server.
      </p>
      <ul className="kb-docs">
        {documents.map((doc) => (
          <li key={doc.path} className="kb-doc">
            <details>
              <summary>
                <Icon name="chevronRight" size={14} />
                <span className="kb-doc-title">{doc.title || doc.path}</span>
                <Mono>{doc.path}</Mono>
                <span className="muted small">
                  {fmtInt(doc.sections.length)} section{doc.sections.length === 1 ? "" : "s"}
                </span>
                {isOverlayPath(doc.path) ? (
                  <Chip tone="warn" title="From an extra knowledge folder indexed for red-team tests: it may be deliberately wrong">
                    red-team overlay
                  </Chip>
                ) : null}
              </summary>
              <ol className="kb-sections">
                {doc.sections.map((section) => (
                  <li key={section.chunkId}>
                    <span className="kb-section-heading">{section.heading || "(no heading)"}</span>
                    <Mono>{section.chunkId}</Mono>
                  </li>
                ))}
              </ol>
            </details>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Search results: rank, score, the retriever that ranked each one, flags and the section text. */
export function SearchResults({ results }: { results: readonly KnowledgeResult[] }) {
  const retrievers = [...new Set(results.map((r) => r.retriever).filter(Boolean))];
  return (
    <div className="kb-results-wrap">
      <p className="muted small">
        {fmtInt(results.length)} section{results.length === 1 ? "" : "s"} · ranked by{" "}
        {retrievers.length ? <Mono>{retrievers.join(", ")}</Mono> : NOT_RECORDED.toLowerCase()}
      </p>
      <ol className="kb-results">
        {results.map((result) => {
          const overlay = isOverlayPath(result.chunk.doc_path);
          const flags = result.flags ?? [];
          const hints = result.action_hints ?? [];
          return (
            <li key={`${result.chunk.chunk_id}-${result.rank}`} className={overlay ? "kb-result overlay" : "kb-result"}>
              <div className="kb-result-head">
                <span className="kb-rank" title="Rank in this search">
                  #{fmtRank(result.rank)}
                </span>
                <span className="kb-cite">
                  {result.chunk.title} › {result.chunk.heading}
                </span>
                <span className="chip-row">
                  <Chip mono title="The score this result was ranked by">
                    score {fmtMeasure(result.score)}
                  </Chip>
                  <Chip mono title="The retriever that ranked this result (reported by the server)">
                    {result.retriever || NOT_RECORDED}
                  </Chip>
                  {result.reranker ? (
                    <Chip mono title="The reranker the server applied">
                      reranker {result.reranker}
                    </Chip>
                  ) : null}
                  {overlay ? (
                    <Chip tone="warn" title="From an extra knowledge folder indexed for red-team tests: it may be deliberately wrong">
                      red-team overlay
                    </Chip>
                  ) : null}
                </span>
              </div>
              <div className="kb-result-meta">
                <Mono>{result.chunk.doc_path}</Mono>
                <Mono>{result.chunk.chunk_id}</Mono>
                <span className="muted">signals: {signalSummary(result.signals)}</span>
              </div>
              {flags.length ? (
                <div className="chip-row" aria-label="Flags the server raised on this text">
                  {flags.map((flag) => (
                    <FlagChip key={flag} flag={flag} />
                  ))}
                </div>
              ) : null}
              {hints.length ? (
                <div className="chip-row kb-hints">
                  <span className="muted small">Action hints parsed from the text:</span>
                  {hints.map((hint, i) => (
                    <Chip key={i} tone="accent" mono title="Parsed by RECAP from the runbook text: a hint for the proposer, never an instruction">
                      {hint.mechanism} → {hint.action}
                    </Chip>
                  ))}
                </div>
              ) : null}
              <LogView
                text={result.chunk.text}
                label={`Section text ${result.chunk.chunk_id}`}
                untrusted="runbook text"
                maxHeight={240}
                initialLines={14}
                maxChars={4000}
              />
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/** The search box: its query and size live in the route (#/knowledge?q=...&k=5), so a search is a deep link. */
function KnowledgeSearch({ route }: { route: Route }) {
  const query = (route.params.get("q") ?? "").trim();
  const k = resultCount(route.params.get("k"));
  const [draft, setDraft] = useState(query);
  const [size, setSize] = useState(k);
  const inputId = useId();
  useEffect(() => setDraft(query), [query]);
  useEffect(() => setSize(k), [k]);
  // The API needs at least 2 characters; a search runs once per query (no polling).
  const search = usePolling(query.length >= 2 ? () => api.knowledgeSearch(query, k) : null, null, [query, k]);

  const go = (q: string, size: number) => navigate("knowledge", { ...paramsOf(route), q, k: String(size) }, true);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const next = draft.trim();
    if (next.length < 2) return;
    if (next === query && size === k) search.refresh();
    else go(next, size);
  };
  const resize = (next: number) => {
    setSize(next);
    if (query) go(query, next);
  };

  return (
    <div className="kb-search">
      <form className="search-row" role="search" aria-label="Runbooks" onSubmit={submit}>
        <Icon name="search" size={16} />
        <label htmlFor={inputId} className="sr-only">
          Search the runbooks
        </label>
        <input
          id={inputId}
          className="input"
          type="search"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="e.g. roll back a bad release, memory pressure, upstream errors"
          autoComplete="off"
        />
        <select aria-label="Number of results" value={size} onChange={(event) => resize(Number(event.target.value))}>
          {RESULT_COUNTS.map((n) => (
            <option key={n} value={n}>
              top {n}
            </option>
          ))}
        </select>
        <button type="submit" className="btn btn-primary" disabled={draft.trim().length < 2}>
          Search
        </button>
      </form>
      <PolledView
        polled={search}
        loadingLabel="Searching…"
        errorTitle="The search failed"
        emptyTitle="No results"
        emptyIcon="search"
        empty={(results) =>
          results.length ? null : (
            <>
              No indexed section matched <q>{query}</q>.
            </>
          )
        }
        idle={
          <EmptyView
            title="Search the runbooks"
            icon="search"
            reason="Type at least 2 characters. Results come from the server's index with their rank, score and the text agents would see."
          />
        }
      >
        {(results) => <SearchResults results={results} />}
      </PolledView>
    </div>
  );
}

export function KnowledgePage({ route, system }: PageProps) {
  const docs = usePolling(() => api.knowledgeDocs(), null, []);
  return (
    <>
      <PageHeader
        title="Knowledge"
        description="The runbooks RECAP retrieves from, how runs search them, and the same search for you to try. Every value comes from the API; runbook text is untrusted data, shown and never followed."
      />

      <div className="grid-2">
        <Card title="Retrieval configuration" subtitle="GET /system: the default for runs that do not choose a search mode">
          <PolledView polled={system} loadingLabel="Loading the retrieval configuration…" errorTitle="Could not load GET /system">
            {(summary) => <RetrievalConfig system={summary} />}
          </PolledView>
        </Card>
        <Card title="Search the runbooks" subtitle="GET /knowledge/search: each result names the retriever that ranked it">
          <KnowledgeSearch route={route} />
        </Card>
      </div>

      <Card title="Runbook index" subtitle="GET /knowledge/docs: the documents and sections the retriever indexed">
        <PolledView
          polled={docs}
          loadingLabel="Loading the runbook index…"
          errorTitle="Could not load GET /knowledge/docs"
          emptyTitle="No runbooks indexed"
          emptyIcon="book"
          empty={(list) => (list.length ? null : "The server reports no indexed runbook sections.")}
        >
          {(list) => <RunbookIndex docs={list} />}
        </PolledView>
      </Card>

      <Notice tone="info" icon="search" title="Agent-initiated searches">
        The searches made during a run (RECAP's and the agents' own queries, their results and citations) are on{" "}
        <a href={hrefFor("evidence")}>{PAGE_LABELS.evidence}</a> for that run.
      </Notice>
      {/*
        Seam for run-level retrieval: a run's searches (RECAP's and the agents' own search_runbooks
        calls) belong here. Select the run with useRunSelection(route) inside a RunScope, add
        "knowledge" to RUN_SCOPED_PAGES (src/hooks.ts) so the selected run follows the user here,
        read GET /runs/{id}/retrieval, and reuse SearchResults / NOT_RECORDED. An agent's query is
        its model's text: show it with UntrustedText.
      */}
    </>
  );
}
