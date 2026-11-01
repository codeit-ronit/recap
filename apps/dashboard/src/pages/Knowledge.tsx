import { useEffect, useId, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "../api";
import { Chip, FlagChip, Mono, YesNoChip } from "../components/Badge";
import { Card, KeyValues, Notice, PageHeader } from "../components/Card";
import { DataSourceBadge, summarySource } from "../components/DataSourceBadge";
import type { SummarySourceFields } from "../components/DataSourceBadge";
import { Icon } from "../components/Icon";
import { LogView } from "../components/LogView";
import { RunScope } from "../components/RunPicker";
import { EmptyView, PolledView } from "../components/StateViews";
import { StateToken, StatusToken } from "../components/StatusToken";
import { DataTable } from "../components/Table";
import { UntrustedText } from "../components/Untrusted";
import type { MemoryRetrievalView, RetrievalView, SearchResultView, SearchView } from "../dto";
import { fmtDateTime, fmtInt, fmtMs, fmtTime, hybridReadiness, isTerminal, outcomeTone, searchModeLabel } from "../format";
import { PAGE_LABELS, hrefFor, navigate, paramsOf, usePolling, useRunSelection } from "../hooks";
import type { PageProps, Route } from "../hooks";
import type { KnowledgeDocRef, KnowledgeResult, RunSummary, SystemSummary } from "../types";
import {
  NOT_RECORDED,
  embeddingServerText,
  embeddingTokensText,
  fmtMeasure,
  fmtRank,
  isAgentQuery,
  isOverlayPath,
  memoryDecisions,
  memorySignalText,
  modeNote,
  queryAuthorText,
  recapQueryNote,
  runEmbedderText,
  searchCounts,
  shortDigest,
  signalParts,
  signalSummary,
  stageHelp,
  stageTitle,
} from "../view/retrieval";
import type { MemoryDecision } from "../view/retrieval";
import "../styles/evidence-m3.css";
import "../styles/evidence-knowledge.css";

/**
 * Knowledge / agentic RAG.
 *
 *   This run (run scope, ?run=): GET /runs/{id}/retrieval, how the selected run searched the
 *   runbooks (each query, who wrote it, BM25 or hybrid, ranks, scores and signals, what used each
 *   result), its embedding model, agent-initiated and refused searches, the runbook sections in
 *   context, and its memory searches with RECAP's reuse verdicts.
 *
 *   The knowledge base: the runbook index the retriever searches (GET /knowledge/docs), how a
 *   run searches it by default (GET /system), and the same search for a person to try
 *   (GET /knowledge/search).
 *
 * A field the server does not report reads NOT_RECORDED. Runbook text, a query an agent wrote
 * and the alert's words are untrusted data: shown as escaped text and never followed.
 */

export { NOT_RECORDED };

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


// =================================================================================================
// This run: GET /runs/{id}/retrieval
// =================================================================================================

/** Poll while the run is live; retry slowly while nothing loaded (the API may be restarting). */
export function retrievalInterval(view: RetrievalView | undefined): number | null {
  if (!view) return 10_000;
  if (view.status === "running") return 2500;
  return view.status === "external" && !isTerminal(view.state) ? 5000 : null;
}

const SERVICE_NAME = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

/** Text a provider (or an alert) wrote: an escaped quote with a tag naming who wrote it. */
function QuotedText({ text, tag, author }: { text: string; tag: string; author: string }) {
  return (
    <span className="ed-quote">
      <UntrustedText text={text} author={author} />
      <span className="untrusted-tag" title={`Written by ${author}: untrusted data, shown as text and never followed`}>
        {tag}
      </span>
    </span>
  );
}

function Line({ label, children, title }: { label: string; children: ReactNode; title?: string }) {
  return (
    <div className="ed-line">
      <span className="ed-label" title={title}>
        {label}
      </span>
      <span className="ed-line-value">{children}</span>
    </div>
  );
}

function NotRecordedText({ children = NOT_RECORDED }: { children?: ReactNode }) {
  return <span className="not-recorded">{children}</span>;
}

/** A service name from a past episode or a search: a checked value, else quoted data. */
function Service({ value }: { value: string | null }) {
  if (!value) return <NotRecordedText />;
  return SERVICE_NAME.test(value) ? <Mono>{value}</Mono> : <UntrustedText text={value} author="a recorded run" />;
}

/** Who wrote a search's query, and how to read it. */
function SearchQuery({ search }: { search: SearchView }) {
  if (!search.query) return <NotRecordedText />;
  if (isAgentQuery(search)) {
    const who = queryAuthorText(search.query_author);
    return <QuotedText text={search.query} tag={`untrusted · written by ${who}`} author={`${who}${search.agent ? ` (${search.agent})` : ""}`} />;
  }
  // RECAP joins its own query from the alert's words (initial) or the diagnosed mechanism (proposal).
  return search.stage === "initial" ? (
    <QuotedText text={search.query} tag="RECAP's query · the alert's words" author="RECAP, from the alert's words" />
  ) : (
    <span className="ed-quote">
      <span className="kn-query">{search.query}</span>
      <span className="trust-tag trust-trusted ed-recap-tag" title={recapQueryNote(search.stage)}>
        RECAP's query
      </span>
    </span>
  );
}

function UsedBy({ result }: { result: Pick<SearchResultView, "cited_by_agents" | "cited_by_proposal"> }) {
  if (!result.cited_by_agents.length && !result.cited_by_proposal) return <span className="muted small">not used</span>;
  return (
    <span className="chip-row">
      {result.cited_by_agents.map((agent, i) => (
        <Chip key={`${agent}-${i}`} mono title="This section was in the agent's context pack">
          {agent}
        </Chip>
      ))}
      {result.cited_by_proposal ? (
        <Chip tone="accent" title="The proposed fix cites this section">
          cited by the fix
        </Chip>
      ) : null}
    </span>
  );
}

function OverlayChip() {
  return (
    <Chip tone="warn" title="From an extra knowledge folder indexed for a red-team run: it may be deliberately wrong">
      <Icon name="alert" size={12} /> red-team overlay
    </Chip>
  );
}

function SignalCell({ signals }: { signals: Readonly<Record<string, number>> }) {
  const parts = signalParts(signals);
  if (!parts.length) return <NotRecordedText />;
  return (
    <span className="cell-stack small">
      {parts.map((part, i) => (
        <span key={i} className="nowrap">
          {part}
        </span>
      ))}
    </span>
  );
}

/** A table of ranked runbook sections (a search's results, or the sections in context at the end). */
export function ResultTable({
  results,
  overlays,
  showRetriever = false,
  empty,
}: {
  results: readonly SearchResultView[];
  overlays: readonly string[];
  showRetriever?: boolean;
  empty: string;
}) {
  return (
    <DataTable
      className="compact kn-results-table"
      rows={[...results]}
      rowKey={(r, i) => `${r.chunk_id}-${i}`}
      rowClassName={(r) => (isOverlayPath(r.doc_path, overlays) ? "row-warn" : undefined)}
      empty={empty}
      columns={[
        {
          key: "rank",
          header: <span title="The rank in the search that retrieved the section">Rank</span>,
          numeric: true,
          render: (r) => (r.rank === null ? <NotRecordedText /> : fmtRank(r.rank)),
        },
        {
          key: "section",
          header: "Runbook section",
          render: (r) => (
            <span className="cell-stack">
              <span className="kn-heading">{r.heading || <NotRecordedText>heading not recorded</NotRecordedText>}</span>
              <span className="chip-row">
                <Mono>{r.chunk_id || NOT_RECORDED}</Mono>
                {isOverlayPath(r.doc_path, overlays) ? <OverlayChip /> : null}
              </span>
              {r.doc_path ? (
                <span className="muted small kn-path">
                  <Mono>{r.doc_path}</Mono>
                </span>
              ) : null}
            </span>
          ),
        },
        { key: "score", header: "Score", numeric: true, render: (r) => (r.score === null ? <NotRecordedText /> : fmtMeasure(r.score)) },
        ...(showRetriever
          ? [{ key: "retriever", header: "Ranked by", render: (r: SearchResultView) => (r.retriever ? <Mono>{r.retriever}</Mono> : <NotRecordedText />) }]
          : []),
        { key: "signals", header: <span title="The ranking signals the search recorded (BM25 rank and score; dense rank and cosine; RRF)">Signals</span>, render: (r) => <SignalCell signals={r.signals} /> },
        { key: "used", header: <span title="Agents whose context held the section, and whether the proposed fix cites it">Used by</span>, render: (r) => <UsedBy result={r} /> },
      ]}
    />
  );
}

function EmbeddingLine({ search }: { search: SearchView }) {
  const e = search.embedding;
  if (!e) return null;
  const parts = [
    e.model ? `model ${e.model}` : "model not recorded",
    e.inputs !== null ? `${fmtInt(e.inputs)} input${e.inputs === 1 ? "" : "s"}` : null,
    e.input_tokens !== null ? `${fmtInt(e.input_tokens)} tokens (as the server reported them)` : "tokens not reported",
    e.duration_ms !== null ? fmtMs(e.duration_ms) : null,
    e.replayed ? "answered from a recording" : null,
  ].filter(Boolean);
  return (
    <Line label="Embedding" title="The embedding request behind this hybrid search, as the embedder recorded it">
      <span className="muted">{parts.join(" · ")}</span>
    </Line>
  );
}

function SearchBlock({ search, index, overlays }: { search: SearchView; index: number; overlays: readonly string[] }) {
  return (
    <article className="kn-search" aria-label={`Search ${index + 1}: ${stageTitle(search.stage)}`}>
      <header className="kn-search-head">
        <span className="kn-search-num" aria-hidden="true">
          {index + 1}
        </span>
        <span className="kn-search-title">{stageTitle(search.stage)}</span>
        {search.agent ? (
          <Chip mono tone="accent">
            {search.agent}
          </Chip>
        ) : null}
        {search.mode ? (
          <Chip mono title={modeNote(search.mode)}>
            {search.mode}
          </Chip>
        ) : (
          <NotRecordedText>mode not recorded</NotRecordedText>
        )}
        {search.retriever ? (
          <span className="muted small">
            ranked by <Mono>{search.retriever}</Mono>
          </span>
        ) : null}
        <span className="muted small kn-search-time">{fmtTime(search.ts)}</span>
      </header>
      <p className="ed-help">{stageHelp(search.stage)}</p>
      <Line label="Query">
        <SearchQuery search={search} />
      </Line>
      {search.reason ? (
        <Line label="Why" title="RECAP's reason for this search">
          <span className="kn-reason">{search.reason}</span>
        </Line>
      ) : null}
      <EmbeddingLine search={search} />
      <ResultTable results={search.results} overlays={overlays} empty="No runbook section matched this query." />
    </article>
  );
}

/** How the run searched: mode, embedder, embedding server and requests, agent searches. */
export function RunRetrievalSummary({ view }: { view: RetrievalView }) {
  const embedder = runEmbedderText(view);
  const server = embeddingServerText(view.embedding_server);
  const tokens = embeddingTokensText(view);
  const bm25 = view.mode === "bm25";
  const counts = searchCounts(view.searches);
  const rows: [ReactNode, ReactNode][] = [
    [
      "Runbook search",
      view.use_rag ? (
        <StatusToken tone="ok" size="sm" label="On" />
      ) : (
        <span className="cell-stack">
          <StatusToken tone="neutral" size="sm" label="Off" />
          <span className="muted small">the run's configuration switched retrieval off</span>
        </span>
      ),
    ],
    [
      "Search mode",
      view.mode ? (
        <span className="cell-stack">
          <span>{searchModeLabel(view.mode, true)}</span>
          <span className="muted small">{modeNote(view.mode)}</span>
        </span>
      ) : (
        <NotRecordedText />
      ),
    ],
    ["Query embedder", view.embedder ? <Mono>{view.embedder}</Mono> : embedder ? <span className="muted">{embedder}</span> : <NotRecordedText />],
    [
      "Embedding server",
      server ? (
        <span className="muted" title={typeof view.embedding_server?.weights === "string" ? view.embedding_server.weights : undefined}>
          {server}
        </span>
      ) : bm25 ? (
        <span className="muted">not used (BM25 only)</span>
      ) : (
        <NotRecordedText />
      ),
    ],
    [
      "Vector index",
      bm25 ? (
        <span className="muted">not used (BM25 only)</span>
      ) : (
        <span className="cell-stack">
          <NotRecordedText />
          <span className="muted small">a run records its embedder, not which stored vectors it searched; the knowledge base below shows the index loaded now</span>
        </span>
      ),
    ],
    [
      "Embedding requests",
      view.embedding_requests === null ? (
        <NotRecordedText />
      ) : (
        <span>
          {fmtInt(view.embedding_requests)}
          {view.embedding_requests && view.replayed_embedding_requests !== null ? (
            <span className="muted small"> · {fmtInt(view.replayed_embedding_requests)} answered from a recording</span>
          ) : null}
        </span>
      ),
    ],
    ["Embedding input tokens", tokens ? <span>{tokens}</span> : <NotRecordedText />],
    [
      "Searches",
      <span className="cell-stack">
        <span>
          {fmtInt(counts.searches)} search{counts.searches === 1 ? "" : "es"}
          {counts.byStage.length ? <span className="muted small"> · {counts.byStage.map((s) => `${fmtInt(s.count)} ${stageTitle(s.stage).toLowerCase()}`).join(", ")}</span> : null}
        </span>
        <span className="muted small">{fmtInt(counts.results)} results returned</span>
      </span>,
    ],
    [
      "Agent searches",
      view.agent_searches === null ? (
        <NotRecordedText />
      ) : (
        <span className="cell-stack">
          <span>
            {fmtInt(view.agent_searches)} answered
            {view.agent_search_refusals.length ? <span className="muted"> · {fmtInt(view.agent_search_refusals.length)} refused</span> : null}
          </span>
          <span className="muted small">search_runbooks calls an agent made with its own query</span>
        </span>
      ),
    ],
  ];
  return <KeyValues className="kv-wide kn-summary" rows={rows} />;
}

function MemoryVerdict({ decision }: { decision: MemoryDecision }) {
  if (decision.safeToReuse === null) {
    return <StatusToken tone="neutral" size="sm" icon="minus" label="Not assessed" title="RECAP did not judge this episode against live evidence" />;
  }
  return decision.safeToReuse ? (
    <StatusToken tone="ok" size="sm" label="Safe to reuse: YES" />
  ) : (
    <StatusToken tone="bad" size="sm" label="Safe to reuse: NO" />
  );
}

/** Memory searches and RECAP's reuse verdict for every past episode the run found. */
export function MemoryRetrieval({ memory, runId }: { memory: MemoryRetrievalView; runId: string }) {
  const decisions = memoryDecisions(memory);
  const rows: [ReactNode, ReactNode][] = [
    ["Memory", memory.enabled ? <StatusToken tone="ok" size="sm" label="On" /> : <StatusToken tone="neutral" size="sm" label="Off" />],
    [
      "Reads",
      memory.read_partitions.length ? (
        <span className="chip-row">
          {memory.read_partitions.map((p, i) => (
            <Chip key={`${p}-${i}`} mono>
              {p}
            </Chip>
          ))}
        </span>
      ) : (
        <span className="muted">no partition</span>
      ),
    ],
    [
      "Writes",
      memory.write_enabled && memory.write_partition ? <Mono>{memory.write_partition}</Mono> : <span className="muted">nothing (writing is off)</span>,
    ],
    [
      "Searches",
      memory.searches.length ? (
        <span className="cell-stack">
          {memory.searches.map((s) => (
            <span key={s.seq}>
              {fmtInt(s.hits.length)} match{s.hits.length === 1 ? "" : "es"} ·{" "}
              {s.semantic ? `semantic (${s.semantic}) and lexical` : "lexical"}
              {s.created_before ? <span className="muted small"> · memory as of {fmtDateTime(s.created_before)} (the recording's start)</span> : null}
            </span>
          ))}
        </span>
      ) : (
        <span className="muted">none</span>
      ),
    ],
    [
      "Episode written",
      memory.written ? (
        <span className="chip-row">
          <Mono>{memory.written.episode_id}</Mono>
          {memory.written.partition ? <Chip mono>{memory.written.partition}</Chip> : null}
          {memory.written.outcome ? <Chip tone={outcomeTone(memory.written.outcome)}>{memory.written.outcome.replace(/_/g, " ")}</Chip> : null}
          <YesNoChip value={memory.written.verified} yes="oracle-verified" no="not verified" />
        </span>
      ) : memory.skipped.length ? (
        <span className="cell-stack">
          <span className="muted">none, because:</span>
          {memory.skipped.map((reason, i) => (
            <span key={i}>{reason}</span>
          ))}
        </span>
      ) : (
        <span className="muted">none recorded</span>
      ),
    ],
  ];
  return (
    <div className="kn-memory">
      <KeyValues className="kv-wide" rows={rows} />
      {decisions.length ? (
        <DataTable
          className="compact kn-memory-table"
          rows={decisions}
          rowKey={(d) => d.episodeId}
          columns={[
            {
              key: "episode",
              header: "Past episode",
              render: (d) => (
                <span className="cell-stack">
                  <Mono nowrap>{d.episodeId}</Mono>
                  {d.partition ? <span className="muted small">partition {d.partition}</span> : null}
                </span>
              ),
            },
            {
              key: "similarity",
              header: "Similarity",
              numeric: true,
              render: (d) => (
                <span className="cell-stack">
                  <span>{d.similarity === null ? <NotRecordedText /> : fmtMeasure(d.similarity)}</span>
                  {memorySignalText(d.signals) ? <span className="muted small">{memorySignalText(d.signals)}</span> : null}
                </span>
              ),
            },
            {
              key: "past",
              header: "What it remembers",
              render: (d) => (
                <span className="cell-stack">
                  <span className="chip-row">
                    {d.mechanism ? <Mono>{d.mechanism}</Mono> : <NotRecordedText />}
                    <span className="muted">on</span>
                    <Service value={d.service} />
                  </span>
                  <span className="chip-row">
                    {d.pastOutcome ? <Chip tone={outcomeTone(d.pastOutcome)}>{d.pastOutcome.replace(/_/g, " ")}</Chip> : null}
                    <YesNoChip value={d.verified} yes="oracle-verified" no="not verified" />
                  </span>
                </span>
              ),
            },
            {
              key: "verdict",
              header: "RECAP's verdict",
              render: (d) => (
                <span className="cell-stack">
                  <MemoryVerdict decision={d} />
                  {d.reason ? <span className="kn-reason">{d.reason}</span> : null}
                  {d.round !== null ? <span className="muted small">assessed in round {d.round}</span> : null}
                </span>
              ),
            },
          ]}
        />
      ) : (
        <p className="ed-none">{memory.enabled ? "The memory search matched no past episode." : "Memory was off for this run, so it searched no past episode."}</p>
      )}
      <p className="ed-help">
        Similar is not the same as safe to reuse: RECAP reuses a past fix only when live evidence confirms the same mechanism.{" "}
        <a href={hrefFor("memory", { run: runId })}>Open {PAGE_LABELS.memory} for this run</a>
      </p>
    </div>
  );
}

/** One run's retrieval (exported for the component tests). */
export function RunRetrievalBody({ view, runId, summary }: { view: RetrievalView; runId: string; summary?: RunSummary }) {
  const source = summary ? summarySource(summary as unknown as SummarySourceFields) : null;
  const overlays = view.knowledge_overlays;
  const agentSearches = view.searches.filter((s) => s.stage === "agent");
  return (
    <>
      <div className="kn-runbar">
        {source ? <DataSourceBadge provider={source.provider} environment={source.environment} label="Where this run's decisions came from" /> : null}
        <StateToken state={view.state} />
        {view.status === "running" ? <StatusToken tone="accent" icon="clock" label="Running · refreshing" /> : null}
      </div>
      {overlays.length ? (
        <Notice tone="warn" icon="alert" title={`This run indexed extra knowledge folders (red-team): ${overlays.join(", ")}`}>
          Their runbooks may be deliberately wrong, and every section from them is marked on this page. A runbook never proves a
          claim: RECAP's evidence rules read only live tool results.
        </Notice>
      ) : null}
      <div className="kn-grid">
        <Card title="How this run searched" subtitle="The search mode and embedding model this run recorded (they may differ from today's defaults below).">
          <RunRetrievalSummary view={view} />
        </Card>
        <Card
          title="Experience memory"
          subtitle="Past episodes the run's memory search found, and RECAP's verdict on reusing each one's fix."
        >
          <MemoryRetrieval memory={view.memory} runId={runId} />
        </Card>
      </div>
      {/* What the run's records lack: after the summary it qualifies, so the run's facts lead. */}
      {view.not_recorded.length ? (
        <Notice tone="info" icon="info" title="Not recorded for this run">
          <ul className="plain-list ed-plain">
            {view.not_recorded.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </Notice>
      ) : null}
      <Card
        title={`Runbook searches (${fmtInt(view.searches.length)})`}
        subtitle="Every search in order: who wrote the query, how the results ranked, and which agents or which fix used each section."
      >
        {view.searches.length ? (
          <div className="kn-searches">
            {view.searches.map((search, index) => (
              <SearchBlock key={search.seq} search={search} index={index} overlays={overlays} />
            ))}
          </div>
        ) : (
          <EmptyView
            title="No runbook search recorded"
            icon="search"
            reason={
              !view.use_rag
                ? "Retrieval was off for this run."
                : view.status === "running"
                  ? "RECAP searches the runbooks first, when the run starts; the search appears here once it is traced."
                  : view.trace_available
                    ? "The run's trace has no runbook search."
                    : "This run has no trace, so its searches were not recorded."
            }
          />
        )}
        {view.agent_searches === 0 && !agentSearches.length ? (
          <p className="ed-help">No agent called search_runbooks with its own query in this run.</p>
        ) : null}
        {view.skipped.length ? (
          <div className="kn-aside">
            <h3 className="mini-title">Skipped searches</h3>
            <ul className="ed-list">
              {view.skipped.map((skip) => (
                <li key={skip.seq}>
                  <span className="kn-aside-key">{stageTitle(skip.stage)}</span> {skip.reason || NOT_RECORDED}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {view.agent_search_refusals.length ? (
          <div className="kn-aside">
            <h3 className="mini-title">Refused agent searches</h3>
            <ul className="ed-list">
              {view.agent_search_refusals.map((refusal) => (
                <li key={refusal.seq}>
                  {refusal.agent ? <Mono>{refusal.agent}</Mono> : <NotRecordedText>agent not recorded</NotRecordedText>}{" "}
                  <span className="muted">· error as recorded:</span> {refusal.error || NOT_RECORDED}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </Card>
      <Card
        title={`Runbook sections in context (${fmtInt(view.context_docs.length)})`}
        subtitle="The sections in the agents' context packs when the run ended, with the ranking that put them there and what used them."
        flush
      >
        <ResultTable
          results={view.context_docs}
          overlays={overlays}
          showRetriever
          empty={view.use_rag ? "No runbook section was in the agents' context." : "Retrieval was off for this run."}
        />
      </Card>
    </>
  );
}

function RunRetrieval({ runId, summary }: { runId: string; summary: RunSummary | undefined }) {
  const polled = usePolling(() => api.retrievalView(runId), retrievalInterval, [runId]);
  return (
    <PolledView polled={polled} loadingLabel="Loading this run's searches…" errorTitle="Could not load the run's retrieval (GET /runs/{id}/retrieval)">
      {(view) => <RunRetrievalBody view={view} runId={runId} summary={summary} />}
    </PolledView>
  );
}

// =================================================================================================
// The page
// =================================================================================================

export function KnowledgePage({ route, system }: PageProps) {
  const docs = usePolling(() => api.knowledgeDocs(), null, []);
  const selection = useRunSelection(route);
  return (
    <>
      <PageHeader
        title="Knowledge"
        description="How a run searched the runbooks and its experience memory, then the knowledge base itself: the runbooks RECAP retrieves from and the same search for you to try. Runbook text and agents' queries are untrusted data, shown and never followed."
      />

      <section className="kn-section" aria-labelledby="kn-run-title">
        <div className="section-head">
          <h2 className="section-title" id="kn-run-title">
            This run's retrieval
          </h2>
          <p className="section-lead">Every runbook search the selected run made and what it found in memory, as its trace and record have them.</p>
        </div>
        <RunScope selection={selection} page="knowledge">
          {(runId, summary) => <RunRetrieval key={runId} runId={runId} summary={summary} />}
        </RunScope>
      </section>

      <section className="kn-section" aria-labelledby="kn-base-title">
        <div className="section-head">
          <h2 className="section-title" id="kn-base-title">
            Knowledge base
          </h2>
          <p className="section-lead">The runbooks every run searches, how runs search them by default, and a search box that uses the same index.</p>
        </div>
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
      </section>
    </>
  );
}
