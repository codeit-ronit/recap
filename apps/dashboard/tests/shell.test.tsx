/**
 * The Mission Control shell (src/components/Shell.tsx) and its routes (src/hooks.ts):
 * navigation order and labels, keyboard and landmark structure, the environment strip and the
 * selected run's data source, all from server-shaped props.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError } from "../src/api";
import { AppShell, NAV, PRODUCT_NAME } from "../src/components/Shell";
import type { AppShellProps } from "../src/components/Shell";
import { PAGES, PAGE_LABELS, RUN_SCOPED_PAGES, hrefFor, parseHash } from "../src/hooks";
import type { PageId } from "../src/hooks";
import type { EnvironmentInfo, HealthResponse, ProviderInfo, RunDetail, RunRecord, SystemSummary } from "../src/types";
import { count, polled, render, tags, textOf, withAttr } from "./support/render";

const SIMULATED: EnvironmentInfo = {
  kind: "simulated",
  name: "RECAP simulated shop",
  namespace: "shop",
  is_simulated: true,
  supported_actions: ["rollback_deployment"],
  description: "Deterministic local simulation of a five-service shop. No real cluster is involved.",
  stabilization_timeout_seconds: 0,
  stabilization_poll_seconds: 2,
};

const HEALTH: HealthResponse = {
  status: "ok",
  version: "0.4.0",
  environment: "simulated",
  provider: "deterministic",
  label: "SIMULATED LOCAL ENVIRONMENT · V1",
};

const SCRIPTED: ProviderInfo = {
  name: "deterministic-scripted-policy",
  kind: "scripted",
  origin: "scripted",
  model: null,
  description: "scripted",
};

function system(environment: EnvironmentInfo = SIMULATED, provider: ProviderInfo = SCRIPTED): SystemSummary {
  return {
    environment,
    environment_kind: environment.kind,
    tool_transport: "MCP over stdio",
    provider,
    retriever: "bm25",
    retrieval_mode: "bm25",
    hybrid_ready: false,
    embedder: null,
    knowledge_chunks: 41,
    router: "adaptive-router",
    safety_policy: "policy (v3)",
    verifier: "evidence-verifier",
    oracle: "simulated-health-oracle",
    memory_episodes: 12,
    trace_sinks: ["sqlite"],
    config_hash: "c0ffee",
    fixture_hash: "beef",
  };
}

function shell(overrides: Partial<AppShellProps> = {}): string {
  const props: AppShellProps = {
    page: "overview",
    navHref: (page: PageId) => hrefFor(page),
    health: polled<HealthResponse>(HEALTH),
    system: polled<SystemSummary>(system()),
    waitingApprovals: 0,
    approvalsHref: hrefFor("safety"),
    selectedRun: null,
    account: null,
    children: <p>page body</p>,
    ...overrides,
  };
  return render(<AppShell {...props} />);
}

// --- routes ------------------------------------------------------------------------------------

test("nine routes in the agreed order, each with its Mission Control label", () => {
  assert.deepEqual(
    [...PAGES],
    ["overview", "live", "timeline", "evidence", "knowledge", "memory", "safety", "evals", "architecture"],
  );
  assert.deepEqual(
    PAGES.map((p) => PAGE_LABELS[p]),
    [
      "Overview",
      "Incident Lab",
      "Trace Explorer",
      "Evidence & Decisions",
      "Knowledge",
      "Experience Memory",
      "Safety & Approvals",
      "Evaluations",
      "Architecture & Learning",
    ],
  );
  assert.deepEqual(
    NAV.map((n) => n.page),
    [...PAGES],
  );
});

test("hash routes keep working: deep links, the run parameter, the new knowledge route, unknown paths", () => {
  const route = parseHash("#/timeline?run=run-42&agent=telemetry_investigator");
  assert.equal(route.page, "timeline");
  assert.equal(route.params.get("run"), "run-42");
  assert.equal(parseHash("#/knowledge?q=rollback").page, "knowledge");
  assert.equal(parseHash("#/knowledge/").page, "knowledge");
  assert.equal(parseHash("#/no-such-page").page, "overview");
  assert.equal(parseHash("").page, "overview");
  assert.equal(hrefFor("evidence", { run: "run-42", ev: null }), "#/evidence?run=run-42");
  assert.deepEqual([...RUN_SCOPED_PAGES], ["timeline", "evidence", "knowledge", "memory", "safety"]);
});

// --- navigation and landmarks ------------------------------------------------------------------

test("the navigation lists the nine pages in order, and only the current one has aria-current", () => {
  const html = shell({ page: "knowledge" });
  const links = tags(html, "a").filter((a) => (a.attrs.class ?? "").split(" ").includes("nav-item"));
  assert.equal(links.length, 9);
  assert.deepEqual(
    links.map((a) => a.attrs.href),
    PAGES.map((p) => `#/${p}`),
  );
  const current = withAttr(html, "aria-current", "page");
  assert.equal(current.length, 1);
  assert.equal(current[0].attrs.href, "#/knowledge");
  for (const label of Object.values(PAGE_LABELS)) assert.ok(textOf(html).includes(label), label);
});

test("landmarks and keyboard: a skip link first, labelled navigation, a header, a focusable main", () => {
  const html = shell();
  const first = tags(html)[1]; // after the .app wrapper
  assert.equal(first.name, "a");
  assert.equal(first.attrs.class, "skip-link");
  assert.equal(first.attrs.href, "#main");
  assert.match(textOf(html), /Skip to main content/);

  const nav = tags(html, "nav");
  assert.equal(nav.length, 1);
  assert.equal(nav[0].attrs["aria-label"], "Primary");
  assert.equal(tags(html, "header").length, 1);
  const main = tags(html, "main");
  assert.equal(main.length, 1);
  assert.equal(main[0].attrs.id, "main");
  assert.equal(main[0].attrs.tabindex, "-1");
  assert.ok(html.indexOf("<main") > html.indexOf("<nav"), "main comes after the navigation");
  assert.match(html, /<p>page body<\/p>/);
});

test("the menu button controls the navigation list and starts collapsed", () => {
  const html = shell();
  const toggle = tags(html, "button").find((b) => (b.attrs.class ?? "").includes("nav-toggle"));
  assert.ok(toggle);
  assert.equal(toggle.attrs["aria-expanded"], "false");
  assert.equal(toggle.attrs["aria-controls"], "primary-nav-list");
  assert.equal(withAttr(html, "id", "primary-nav-list").length, 1);
});

test("the product name is RECAP Mission Control, and the brand links to the overview", () => {
  assert.equal(PRODUCT_NAME, "RECAP Mission Control");
  const html = shell();
  const brand = tags(html, "a").find((a) => a.attrs.class === "brand");
  assert.equal(brand?.attrs.href, "#/overview");
  assert.match(textOf(html), /RECAP Mission Control/);
});

// --- environment strip and data source -----------------------------------------------------------

test("a simulated environment reported by GET /system shows SIMULATED ENVIRONMENT with the server's words", () => {
  const html = shell();
  const region = tags(html, "section").find((s) => s.attrs["aria-label"] === "Environment");
  assert.ok(region, "the strip is a labelled region");
  assert.match(region.attrs.class, /tone-warn/);
  const text = textOf(html);
  assert.match(text, /SIMULATED ENVIRONMENT/);
  assert.match(text, /RECAP simulated shop · kind simulated · namespace shop/);
  assert.match(text, /No real cluster is involved/);
  assert.doesNotMatch(text, /production|LIVE ENVIRONMENT/i);
});

test("before the API answers the strip claims nothing: neither simulated nor live", () => {
  const html = shell({ health: polled<HealthResponse>(undefined), system: polled<SystemSummary>(undefined) });
  const text = textOf(html);
  assert.match(text, /ENVIRONMENT NOT CONFIRMED/);
  assert.doesNotMatch(text, /SIMULATED|LIVE/);
  assert.match(text, /Checking the API/);
});

test("with only GET /health answered, the strip shows the server's own label, unconfirmed", () => {
  const html = shell({ system: polled<SystemSummary>(undefined) });
  assert.match(textOf(html), /SIMULATED LOCAL ENVIRONMENT · V1/);
  assert.match(textOf(html), /confirming with GET \/system/);
  assert.match(html, /env-banner env-reported tone-neutral/);
});

test("a live environment reported by the server is labelled live, in red", () => {
  const live = { ...SIMULATED, kind: "kind", name: "demo cluster", is_simulated: false };
  const html = shell({ system: polled<SystemSummary>(system(live)) });
  assert.match(textOf(html), /LIVE ENVIRONMENT · kind/);
  assert.match(html, /env-banner env-live tone-bad/);
  assert.doesNotMatch(textOf(html), /SIMULATED ENVIRONMENT/);
});

test("API status: online with the server version, offline with the start command banner", () => {
  assert.match(textOf(shell()), /API online v0\.4\.0/);
  const offline = new ApiError("Cannot reach the RECAP API.", 0, "/health", null, true);
  const html = shell({ health: polled<HealthResponse>(undefined, offline) });
  assert.match(textOf(html), /API offline/);
  assert.match(textOf(html), /The RECAP API is not reachable/);
  assert.match(textOf(html), /uvicorn apps\.api\.main:app/);
  assert.equal(withAttr(html, "role", "alert").length, 1);
});

test("no run selected: the top bar shows the server's default provider, scripted, never as an LLM", () => {
  const text = textOf(shell());
  assert.match(text, /Default provider/);
  assert.match(text, /SCRIPTED BASELINE/);
  assert.doesNotMatch(text, /LOCAL LLM|REPLAY/);
});

test("a selected replayed run shows its id and RECORDED REAL-LLM RUN / SIMULATED TARGET from its record", () => {
  const provider: ProviderInfo = {
    name: "replay:ollama:qwen3:8b",
    kind: "replay",
    origin: "replay",
    model: "qwen3:8b",
    description: "",
    replay_of: { original_provider: "ollama", model: "qwen3:8b", recording_id: "rec-1" },
  };
  const record = { provider_info: provider, environment: SIMULATED } as unknown as RunRecord;
  const detail: RunDetail = { status: "finished", record, evidence: [], error: null };
  const html = shell({ page: "timeline", selectedRun: { runId: "run-7", detail: polled<RunDetail>(detail) } });
  const text = textOf(html);
  assert.match(text, /Selected run run-7/);
  assert.match(text, /REPLAY · RECORDED REAL-LLM RUN qwen3:8b \/ SIMULATED TARGET/);
  assert.equal(withAttr(html, "aria-label", "Selected run data source").length, 1);
});

test("a selected run still loading, or without a record, says so instead of guessing", () => {
  const loading = textOf(shell({ page: "evidence", selectedRun: { runId: "run-8", detail: polled<RunDetail>(undefined) } }));
  assert.match(loading, /loading data source/);
  const starting: RunDetail = { status: "running", record: null, evidence: [], error: null };
  const text = textOf(shell({ page: "evidence", selectedRun: { runId: "run-8", detail: polled<RunDetail>(starting) } }));
  assert.match(text, /starting: no record stored yet/);
  assert.doesNotMatch(text, /SCRIPTED|LLM|REPLAY/);
});

test("approvals waiting: a badge with screen-reader text on Safety, and a link in the top bar", () => {
  const html = shell({ waitingApprovals: 2 });
  assert.match(textOf(html), /2 approvals waiting/);
  assert.equal(count(html, "nav-badge"), 1);
  assert.match(html, /<span class="sr-only">, 2 approvals waiting<\/span>/);
});
