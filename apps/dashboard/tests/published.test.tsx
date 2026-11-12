/**
 * Component tests for the published real-cluster runs (M6 W2), rendered on the server: the
 * Published Runs page, the historical banner the shell puts on run pages, the run picker's
 * published group, the Evaluations record drawer, the step-by-step view and the Evidence page of
 * a published run. The data is the API's own JSON from a fresh data folder:
 * tests/fixtures/published-view.json (GET /published) and published-sregym-evidence.json
 * (GET /runs/run-9d7f47104d/evidence).
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError } from "../src/api";
import { HistoricalBanner } from "../src/components/Historical";
import { RunPicker } from "../src/components/RunPicker";
import { AppShell } from "../src/components/Shell";
import type { AppShellProps } from "../src/components/Shell";
import type * as dto from "../src/dto";
import { hrefFor } from "../src/hooks";
import type { PageId } from "../src/hooks";
import { EvidenceBody } from "../src/pages/Evidence";
import { TraceAvailabilityView } from "../src/pages/Evals";
import { DemoRunView } from "../src/pages/GuidedDemo";
import type { DemoRunViewProps } from "../src/pages/GuidedDemo";
import { PublishedRunsView } from "../src/pages/Published";
import type { HealthResponse, RunDetail, SystemSummary, TraceEvent } from "../src/types";
import approvedTrace from "./fixtures/golden-replay-approved.json";
import evidenceFixture from "./fixtures/published-sregym-evidence.json";
import publishedFixture from "./fixtures/published-view.json";
import { count, polled, render, tags, textOf } from "./support/render";

const VIEW = publishedFixture as unknown as dto.PublishedView;
const EVIDENCE = evidenceFixture as unknown as dto.EvidenceView;
const SREGYM = "run-9d7f47104d";
const SUMMARY = VIEW.runs.find((run) => run.run_id === SREGYM) as dto.RunSummary;
const HOSTILE = `<script>alert(1)</script><img src=x onerror="alert(2)">`;
const noop = () => undefined;

const page = (view: dto.PublishedView = VIEW) => render(<PublishedRunsView published={polled(view)} />);
const hrefs = (html: string) => tags(html, "a").map((a) => a.attrs.href ?? "");

// --- the Published Runs page -------------------------------------------------------------------------

test("the page says what a published run is, then the four runs you can open", () => {
  const html = page();
  const text = textOf(html);
  assert.match(text, /Published Runs Real AI-driven incident runs on Kubernetes, recorded in M5 and M6/);
  assert.match(text, /Recorded real Kubernetes execution — historical, read-only/);
  assert.match(text, /Nothing runs now: no model is called, no cluster is touched, and its approvals are records of a past decision, never permissions\./);
  assert.equal(count(html, 'class="pub-run"'), 4);
  assert.equal(count(text, "RECAP's oracle: recovery verified"), 4);
  assert.match(text, /Kind: backend's bad release found and rolled back \(M5's deliverable\)/);
  assert.match(text, /SREGym: geo's misconfigured release rolled back; SREGym's grader passed it/);
});

test("a run in which no approval was decided says so, never that a fact is missing", () => {
  const [first] = VIEW.runs;
  const run = { ...first, published: { ...first.published!, approvals: [] } };
  const text = textOf(page({ ...VIEW, runs: [run] }));
  assert.match(text, /Approval None \(no approval was decided in this run\)/);
  assert.doesNotMatch(text, /Approval Not recorded/);
});

test("each run opens in Trace Explorer, Evidence, Safety and step by step, with its checked files", () => {
  const html = page();
  for (const run of VIEW.runs) {
    for (const target of ["timeline", "evidence", "safety", "demo"] as PageId[]) {
      assert.ok(hrefs(html).includes(hrefFor(target, { run: run.run_id })), `${target} ${run.run_id}`);
    }
    assert.ok(hrefs(html).includes(`https://github.com/codeit-ronit/recap/tree/main/${run.published?.folder}`));
  }
  assert.equal(count(textOf(html), "Files checked against their pinned SHA-256"), 4);
});

test("SREGym's grader stays apart from RECAP's oracle, and the run is not called a benchmark result", () => {
  const html = page();
  const sregym = textOf(html.slice(html.indexOf(`id="pub-${SREGYM}"`)));
  assert.match(sregym, /SREGym's grader: pass/);
  assert.match(sregym, /Not a held-out benchmark result: it was the fifth run of this SREGym problem/);
  assert.match(sregym, /approved by console-user \(interactive terminal; typed the target service's name; answered after 121\.9 s/);
  // The Kind runs had no benchmark grader.
  const kind = textOf(html.slice(html.indexOf('id="pub-run-047aac6eb0"'), html.indexOf('id="pub-run-f5b9e5bf45"')));
  assert.doesNotMatch(kind, /grader/);
});

test("every real-cluster run is listed, failures included, as counts and never a rate", () => {
  const text = textOf(page());
  assert.match(text, /Kind cluster · 7 runs, 3 verified recoveries, 3 with a published trace \(counts, not a rate\)/);
  assert.match(text, /SREGym cluster · 5 runs, 1 verified recoveries, 1 with a published trace \(counts, not a rate\)/);
  assert.match(text, /run-9fea287988 FAILED Insufficient evidence\. bad_deployment on backend was claimed, but the rules found it insufficient not published/);
  assert.match(text, /run-e08c17d46e BLOCKED Safety blocked\. proposal is not supported by recorded evidence fail not published/);
  assert.equal(count(text, "not published"), 8);
  assert.doesNotMatch(text, /%/);
});

test("what the published files do not hold is one click away", () => {
  const html = page();
  assert.match(html, /<details class="pub-limits">/);
  assert.ok(!/<details class="pub-limits"[^>]*\sopen/.test(html));
  assert.match(textOf(html), /The evidence registry's contents \(the tool outputs the agents read\) were not exported/);
});

test("a refused artifact is named with its reason, and a broken manifest shows no run at all", () => {
  const refused = { ...VIEW, runs: VIEW.runs.slice(0, 3), rejected: [{ folder: "2026-11-07-x", run_id: SREGYM, reason: "run.json does not match the SHA-256 and size the manifest pins (altered or replaced)" }] };
  const text = textOf(page(refused));
  assert.match(text, /1 published run was refused, so it is not shown 2026-11-07-x : run\.json does not match the SHA-256/);
  assert.equal(count(page(refused), 'class="pub-run"'), 3);

  const broken = { ...VIEW, runs: [], records_without_trace: [], manifest_error: "MANIFEST.json: schema 'v2' is not 'recap-published-runs-v1'" };
  assert.match(textOf(page(broken)), /The list of published runs could not be read, so no run is shown MANIFEST\.json: schema 'v2'/);
});

test("loading and failure say so; text from the files is escaped", () => {
  assert.match(textOf(render(<PublishedRunsView published={polled<dto.PublishedView>(undefined)} />)), /Checking the published runs/);
  const failed = render(<PublishedRunsView published={polled<dto.PublishedView>(undefined, new ApiError("boom", 500, "/published", null, false))} />);
  assert.match(textOf(failed), /Could not load the published runs \(GET \/published\)/);

  const hostile = { ...VIEW, runs: [{ ...SUMMARY, published: { ...SUMMARY.published!, title: HOSTILE, notes: [HOSTILE] } }] };
  const html = page(hostile);
  assert.ok(!html.includes("<script>") && !html.includes("<img"));
});

// --- a published run on the other pages ------------------------------------------------------------

test("the historical banner says what the run is and links every published run", () => {
  const html = render(<HistoricalBanner summary={SUMMARY} />);
  assert.match(textOf(html), /^Recorded real Kubernetes execution — historical, read-only The record of a real run on the SREGym cluster, on 2026-11-07/);
  assert.match(textOf(html), /files in evals\/live\/2026-11-07-qwen3-8b-sregym-human-approved-prompts-v4\.7 , SHA-256 verified/);
  assert.ok(hrefs(html).includes("#/published"));
});

function shellWith(page: PageId): string {
  const detail = { status: "published", record: null, evidence: [], error: null, summary: SUMMARY } as unknown as RunDetail;
  const props: AppShellProps = {
    page,
    navHref: (p: PageId) => hrefFor(p),
    health: polled<HealthResponse>({ status: "ok", version: "0.6.0", environment: "simulated", provider: "deterministic", label: "SIMULATED LOCAL ENVIRONMENT · V1" } as HealthResponse),
    system: polled<SystemSummary>(undefined),
    waitingApprovals: 0,
    approvalsHref: hrefFor("safety"),
    selectedRun: { runId: SREGYM, detail: polled(detail) },
    account: null,
    children: <p>page body</p>,
  };
  return render(<AppShell {...props} />);
}

test("on a run page the shell marks a published run, above the page and in the top bar", () => {
  for (const page of ["timeline", "evidence", "safety", "live"] as PageId[]) {
    const text = textOf(shellWith(page));
    assert.match(text, /PUBLISHED · READ-ONLY/, page);
    assert.equal(count(text, "Recorded real Kubernetes execution — historical, read-only"), 1, page);
  }
  // The step-by-step view shows the banner itself, in place of a replay's label.
  assert.equal(count(textOf(shellWith("demo")), "Recorded real Kubernetes execution — historical, read-only"), 0);
});

test("the run picker lists published runs in a group of their own", () => {
  const html = render(<RunPicker runs={[]} value={SREGYM} onChange={noop} published={VIEW.runs} />);
  assert.match(html, /<optgroup label="Published real-cluster runs \(historical, read-only\)">/);
  assert.equal(count(html, "(published)</option>"), 4);
  assert.equal(count(html, `<option value="${SREGYM}"`), 1, "the selected published run is not listed twice");
});

test("an evaluation record of a published run links its trace, and says it is historical", () => {
  const html = render(<TraceAvailabilityView runId={SREGYM} polled={polled<dto.RunSummary | null>(SUMMARY)} />);
  assert.match(textOf(html), /Published with the repository: its full trace is one click away, historical and read-only\./);
  assert.ok(hrefs(html).includes(hrefFor("timeline", { run: SREGYM })));
});

test("the step-by-step view of a published run: its banner, and no restart of the replay demo", () => {
  const events = approvedTrace as unknown as TraceEvent[];
  const props: DemoRunViewProps = {
    runId: SREGYM,
    events,
    finished: true,
    live: false,
    transport: "recorded",
    streamError: null,
    interview: false,
    signedIn: true,
    provider: { origin: "local_llm", model: "qwen3:8b" },
    environment: { kind: "sregym", is_simulated: false },
    summary: SUMMARY,
    record: null,
    detailLoading: false,
    waiting: false,
    request: null,
    reviewable: false,
    onReview: noop,
    pinned: null,
    onPin: noop,
    technical: false,
    onTechnical: noop,
    onInterview: noop,
    onRestart: noop,
    restarting: false,
    restartError: null,
  };
  const html = render(<DemoRunView {...props} />);
  const text = textOf(html);
  assert.match(text, /Recorded real Kubernetes execution — historical, read-only/);
  assert.doesNotMatch(text, /Recorded real-model execution — replayed locally|This run is not a replay|Restart/);
  assert.ok(hrefs(html).includes("#/published"));
});

test("Evidence & Decisions never calls an unexported registry's ids missing", () => {
  const html = render(<EvidenceBody view={EVIDENCE} runId={SREGYM} summary={SUMMARY as never} />);
  const text = textOf(html);
  assert.doesNotMatch(text, /not in this run/);
  assert.match(text, /Observations Not published/);
  assert.match(text, /The registry's contents were not published/);
  assert.match(text, /not published with this run; each evidence ID and the tool that produced it are in the trace/);
});
