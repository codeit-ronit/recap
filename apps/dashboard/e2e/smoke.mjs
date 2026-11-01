#!/usr/bin/env node
/**
 * Browser smoke test of RECAP Mission Control, against a RUNNING RECAP API that serves the
 * built dashboard (`npm run build`, then start uvicorn). Nothing to install: it drives an
 * installed Google Chrome (headless, a throwaway profile) through the Chrome DevTools Protocol
 * with Node's own fetch and WebSocket (Node 22+).
 *
 *   node e2e/smoke.mjs --base http://127.0.0.1:8000 --token <operator token>
 *
 * What it checks, as a person would see it:
 *   1. Pages: the nine routes at 1440 x 900 and at a 390 x 844 phone, the run-scoped ones for a
 *      replay of the golden recording, a scripted run, a BLOCKED run and a BUDGET_EXHAUSTED run
 *      (whichever the data folder has). Each page must load (its heading, no error panel, nothing
 *      still loading), print nothing to the console (no error, no warning, no exception), never
 *      scroll sideways (also with every <details> opened), show the SIMULATED ENVIRONMENT strip
 *      and the selected run's data source, and never put the operator token or injected markup
 *      in the DOM.
 *   2. The live demo (needs the token): sign in through the top bar, open the Incident Lab,
 *      replay the golden recording at the watchable pace, watch the stage stepper and the event
 *      feed advance, approve the exact action in the modal, and see RESOLVED with the oracle's
 *      verdict; the API must agree (RESOLVED, oracle verified, approved by the operator). Then
 *      the run's Trace Explorer, Evidence, Knowledge, Memory and Safety pages are checked too.
 *
 * The demo starts one real replay run on that server: point it at a demo data folder
 * (scripts/seed_demo.py), not at the data you care about. A replay calls no model.
 *
 * Options:
 *   --base <url>          the dashboard's address (default http://127.0.0.1:8000)
 *   --token <t>           the operator token (or RECAP_OPERATOR_TOKEN, or --token-file <path>);
 *                         without one the demo is skipped. It is never printed.
 *   --chrome <path>       Chrome or Chromium (default: CHROME_PATH, then the usual install paths)
 *   --screenshots <dir>   also save the README screenshots there (1440 x 900 and two 390 px)
 *   --skip-pages          only the demo        --skip-flow   only the pages
 *   --pace <p>            watchable (default) or instant
 *
 * Exit code: 0 every check passed, 1 a check failed, 2 it could not run.
 */

import { spawn } from "node:child_process";
import { accessSync, constants, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join, resolve } from "node:path";
import { parseArgs } from "node:util";

// The route labels (src/hooks.ts PAGE_LABELS) and the shell's product name.
const PRODUCT = "RECAP Mission Control";
const LABELS = {
  overview: "Overview",
  live: "Incident Lab",
  timeline: "Trace Explorer",
  evidence: "Evidence & Decisions",
  knowledge: "Knowledge",
  memory: "Experience Memory",
  safety: "Safety & Approvals",
  evals: "Evaluations",
  architecture: "Architecture & Learning",
};
const GLOBAL_PAGES = ["overview", "evals", "architecture"];
const RUN_PAGES = ["live", "timeline", "evidence", "knowledge", "memory", "safety"];
const DESKTOP = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };
const SOURCE_LABELS = {
  scripted: "SCRIPTED BASELINE",
  replay: "REPLAY",
  local_llm: "LOCAL LLM",
  free_hosted_llm: "FREE HOSTED LLM",
  paid_llm: "PAID LLM",
};

// ------------------------------------------------------------------------------------------
// Arguments
// ------------------------------------------------------------------------------------------

let args;
try {
  ({ values: args } = parseArgs({
    options: {
      base: { type: "string", default: "http://127.0.0.1:8000" },
      token: { type: "string" },
      "token-file": { type: "string" },
      chrome: { type: "string" },
      screenshots: { type: "string" },
      "skip-flow": { type: "boolean", default: false },
      "skip-pages": { type: "boolean", default: false },
      pace: { type: "string", default: "watchable" },
      help: { type: "boolean", short: "h", default: false },
    },
  }));
} catch (err) {
  console.error(`${err.message}\nRun with --help for the options.`);
  process.exit(2);
}
if (args.help) {
  const source = readFileSync(new URL(import.meta.url), "utf8");
  console.log(source.slice(source.indexOf("/**") + 3, source.indexOf("*/")).replace(/^ \* ?/gm, ""));
  process.exit(0);
}
if (typeof WebSocket !== "function" || typeof fetch !== "function") {
  console.error("This smoke test needs Node 22 or newer (built-in fetch and WebSocket).");
  process.exit(2);
}
if (!["watchable", "instant"].includes(args.pace)) {
  console.error("--pace is watchable or instant");
  process.exit(2);
}

const BASE = args.base.replace(/\/+$/, "");
let TOKEN = args.token ?? process.env.RECAP_OPERATOR_TOKEN ?? null;
if (!TOKEN && args["token-file"]) TOKEN = readFileSync(args["token-file"], "utf8").trim();
TOKEN = TOKEN ? TOKEN.trim() : null;
const SHOTS = args.screenshots ? resolve(args.screenshots) : null;

/** Never let the token reach the output, whatever prints it. */
function redact(text) {
  const s = String(text);
  return TOKEN ? s.split(TOKEN).join("<operator token>") : s;
}

// ------------------------------------------------------------------------------------------
// Results
// ------------------------------------------------------------------------------------------

const results = [];
function record(ok, name, detail = "") {
  results.push({ ok, name, detail: redact(detail) });
  const mark = ok === true ? "PASS" : ok === "skip" ? "SKIP" : "FAIL";
  console.log(`${mark}  ${name}${detail ? `  -- ${redact(detail)}` : ""}`);
}
const note = (text) => console.log(`      ${redact(text)}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------------------------------------
// The API (read-only calls from Node, to choose runs and to cross-check the demo)
// ------------------------------------------------------------------------------------------

let API = BASE;
async function apiGet(path, token) {
  const res = await fetch(`${API}${path}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!res.ok) throw new Error(`GET ${path}: HTTP ${res.status}`);
  return res.json();
}

async function findApi() {
  for (const candidate of [BASE, `${BASE}/api`]) {
    try {
      const res = await fetch(`${candidate}/health`);
      if (res.ok && (res.headers.get("content-type") ?? "").includes("json")) {
        API = candidate;
        return await res.json();
      }
    } catch {
      // try the next one
    }
  }
  return null;
}

// ------------------------------------------------------------------------------------------
// Chrome and the DevTools Protocol
// ------------------------------------------------------------------------------------------

function findChrome() {
  const candidates = [
    args.chrome,
    process.env.CHROME_PATH,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  ];
  for (const name of ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]) {
    for (const dir of (process.env.PATH ?? "").split(delimiter)) if (dir) candidates.push(join(dir, name));
  }
  for (const path of candidates) {
    if (!path) continue;
    try {
      accessSync(path, constants.X_OK);
      if (statSync(path).isFile()) return path;
    } catch {
      // not this one
    }
  }
  return null;
}

class Browser {
  constructor() {
    this.id = 0;
    this.pending = new Map();
    this.console = [];
    this.handlers = [];
  }

  async launch(chromePath) {
    this.profile = mkdtempSync(join(tmpdir(), "recap-smoke-"));
    this.process = spawn(
      chromePath,
      [
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-component-update",
        "--hide-scrollbars",
        "--mute-audio",
        "--use-mock-keychain",
        "--password-store=basic",
        `--window-size=${DESKTOP.width},${DESKTOP.height}`,
        `--user-data-dir=${this.profile}`,
        "--remote-debugging-port=0",
        "about:blank",
      ],
      { stdio: "ignore" },
    );
    // Chrome writes the port it picked into the profile once DevTools listens.
    const portFile = join(this.profile, "DevToolsActivePort");
    let port = null;
    for (let i = 0; i < 100 && !port; i++) {
      await sleep(150);
      if (existsSync(portFile)) port = Number(readFileSync(portFile, "utf8").split("\n")[0]) || null;
    }
    if (!port) throw new Error("Chrome did not open its DevTools port");
    let target = null;
    for (let i = 0; i < 50 && !target; i++) {
      try {
        const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
        target = list.find((t) => t.type === "page");
      } catch {
        // not ready yet
      }
      if (!target) await sleep(150);
    }
    if (!target) throw new Error("Chrome opened no page");
    this.ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((ok, fail) => {
      this.ws.addEventListener("open", ok, { once: true });
      this.ws.addEventListener("error", () => fail(new Error("could not connect to Chrome")), { once: true });
    });
    this.ws.addEventListener("message", (message) => this.onMessage(JSON.parse(message.data)));
    await this.send("Runtime.enable");
    await this.send("Log.enable");
    await this.send("Page.enable");
  }

  onMessage(msg) {
    if (msg.id && this.pending.has(msg.id)) {
      const { ok, fail } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.error) fail(new Error(`${msg.error.message}`));
      else ok(msg.result);
      return;
    }
    const p = msg.params ?? {};
    if (msg.method === "Runtime.consoleAPICalled" && ["error", "warning", "warn", "assert"].includes(p.type)) {
      this.console.push(`console.${p.type}: ${(p.args ?? []).map((a) => a.value ?? a.description ?? "").join(" ")}`);
    } else if (msg.method === "Runtime.exceptionThrown") {
      const d = p.exceptionDetails ?? {};
      this.console.push(`exception: ${d.exception?.description ?? d.text ?? "unknown"}`);
    } else if (msg.method === "Log.entryAdded" && ["error", "warning"].includes(p.entry?.level)) {
      this.console.push(`${p.entry.source} ${p.entry.level}: ${p.entry.text}${p.entry.url ? ` (${p.entry.url})` : ""}`);
    }
  }

  send(method, params = {}) {
    return new Promise((ok, fail) => {
      const id = ++this.id;
      this.pending.set(id, { ok, fail });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  /** Evaluate `fn(arg)` in the page and return its (JSON) value. */
  async run(fn, arg) {
    const expression = `(${fn.toString()})(${JSON.stringify(arg ?? null)})`;
    const res = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (res.exceptionDetails) throw new Error(res.exceptionDetails.exception?.description ?? res.exceptionDetails.text);
    return res.result.value;
  }

  async viewport({ width, height }) {
    await this.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 600 });
  }

  async goto(url) {
    await this.send("Page.navigate", { url });
  }

  /** Poll `fn(arg)` in the page until it returns a truthy value; the last value either way. */
  async waitFor(fn, arg, timeoutMs, everyMs = 250) {
    const t0 = Date.now();
    let value = null;
    while (Date.now() - t0 < timeoutMs) {
      try {
        value = await this.run(fn, arg);
      } catch {
        value = null; // the page is between documents
      }
      if (value) return { ok: true, value, ms: Date.now() - t0 };
      await sleep(everyMs);
    }
    return { ok: false, value, ms: Date.now() - t0 };
  }

  async screenshot(file) {
    const res = await this.send("Page.captureScreenshot", { format: "png" });
    writeFileSync(file, Buffer.from(res.data, "base64"));
    return statSync(file).size;
  }

  async close() {
    try {
      this.ws?.close();
    } catch {
      // already closed
    }
    try {
      this.process?.kill();
    } catch {
      // already gone
    }
    await sleep(500);
    if (this.profile) rmSync(this.profile, { recursive: true, force: true });
  }
}

// ------------------------------------------------------------------------------------------
// In-page probes (serialized into the page: no closures over this file)
// ------------------------------------------------------------------------------------------

/** The page has settled: its heading, nothing loading in <main>, the same text twice in a row. */
function settledProbe(expected) {
  const main = document.querySelector("main");
  const h1 = main?.querySelector("h1")?.textContent?.trim() ?? "";
  if (h1 !== expected.label) return false;
  if (expected.run && !location.hash.includes(`run=${expected.run}`)) return false;
  if (main.querySelector(".loading")) return false;
  const size = main.innerText.length;
  const previous = window.__smokeSize;
  window.__smokeSize = size;
  return previous === size && size > 0;
}

function pageFacts() {
  const main = document.querySelector("main");
  const vw = document.documentElement.clientWidth;
  const attrs = [];
  for (const el of main ? main.querySelectorAll("*") : []) {
    for (const a of el.attributes) if (/^on/i.test(a.name)) attrs.push(`${el.tagName.toLowerCase()}[${a.name}]`);
  }
  return {
    h1: main?.querySelector("h1")?.textContent?.trim() ?? null,
    title: document.title,
    navCurrent: document.querySelector('nav[aria-label="Primary"] [aria-current="page"]')?.textContent?.trim() ?? null,
    env: document.querySelector(".env-banner")?.textContent ?? "",
    topbar: document.querySelector(".topbar")?.textContent ?? "",
    textLength: main?.innerText.length ?? 0,
    errors: [...(main?.querySelectorAll('.error-state[role="alert"]') ?? [])].map((e) => e.innerText.replace(/\s+/g, " ").slice(0, 160)),
    loading: main?.querySelectorAll(".loading").length ?? 0,
    sideways: document.documentElement.scrollWidth > vw + 1 ? `${document.documentElement.scrollWidth} px wide in a ${vw} px viewport` : null,
    markup: [...(main?.querySelectorAll("script, iframe, object, embed") ?? [])].map((e) => e.tagName.toLowerCase()).concat(attrs),
  };
}

/** Open every <details> in <main>, measure the page width, close them again. */
function expandedWidth() {
  const closed = [...document.querySelectorAll("main details:not([open])")];
  closed.forEach((d) => (d.open = true));
  const vw = document.documentElement.clientWidth;
  const width = document.documentElement.scrollWidth;
  closed.forEach((d) => (d.open = false));
  return { opened: closed.length, sideways: width > vw + 1 ? `${width} px wide in a ${vw} px viewport with ${closed.length} sections opened` : null };
}

function domHtml() {
  return document.documentElement.outerHTML;
}

/** Click the first element matching `selector` whose text contains `text` (if given). */
function clickProbe({ selector, text }) {
  const el = [...document.querySelectorAll(selector)].find((e) => !text || (e.textContent ?? "").includes(text));
  if (!el) return "missing";
  if (el.disabled) return "disabled";
  el.scrollIntoView({ block: "center" });
  el.click();
  return "clicked";
}

/** Type into an input the way React notices (the native setter, then an input event). */
function typeProbe({ selector, value }) {
  const el = document.querySelector(selector);
  if (!el) return "missing";
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  return "typed";
}

function labState() {
  const stage = document.querySelector(".stepper-now strong")?.textContent?.replace(/\.$/, "").trim() ?? null;
  const count = /([\d,]+) events?\b/.exec(document.querySelector(".feed-toolbar")?.textContent ?? "");
  const dialog = document.querySelector('[role="dialog"]');
  return {
    hash: location.hash,
    stage,
    events: count ? Number(count[1].replace(/,/g, "")) : 0,
    dialog: dialog ? dialog.innerText : null,
    outcome: document.querySelector(".lab-outcome")?.innerText ?? null,
    facts: document.querySelector(".lab-outcome-grid")?.innerText ?? null,
  };
}

/** Scroll so that the element (a selector, or the first heading with that text) is at the top. */
function scrollProbe({ selector, heading, offset }) {
  let el = selector ? document.querySelector(selector) : null;
  if (!el && heading) el = [...document.querySelectorAll("main h2, main h3")].find((h) => h.textContent.trim().startsWith(heading)) ?? null;
  if (heading && el && !selector) el = el.closest("section.card, .card, section") ?? el;
  if (!el) {
    window.scrollTo(0, 0);
    return false;
  }
  window.scrollTo(0, Math.max(0, el.getBoundingClientRect().top + window.scrollY - offset));
  return true;
}

// ------------------------------------------------------------------------------------------
// Checks
// ------------------------------------------------------------------------------------------

let browser;
let healthInfo;

/** Load one page and check it; returns true when every check passed. */
async function checkPage(page, { run = null, source = null, viewport, name }) {
  const label = LABELS[page];
  const url = `${BASE}/#/${page}${run ? `?run=${encodeURIComponent(run)}` : ""}`;
  browser.console.length = 0;
  await browser.run(() => {
    window.__smokeSize = -1;
  }).catch(() => undefined);
  await browser.goto(url);
  const settled = await browser.waitFor(settledProbe, { label, run }, 25_000, 300);
  await sleep(300); // late console output belongs to this page
  const facts = await browser.run(pageFacts);
  const problems = [];
  if (!settled.ok) problems.push(facts.h1 === label ? `still loading after 25 s (${facts.loading} spinner(s))` : `heading is ${JSON.stringify(facts.h1)}`);
  if (facts.title !== `${label} · ${PRODUCT}`) problems.push(`tab title ${JSON.stringify(facts.title)}`);
  if (facts.navCurrent !== label) problems.push(`navigation marks ${JSON.stringify(facts.navCurrent)} as current`);
  if (healthInfo?.environment === "simulated" && !facts.env.includes("SIMULATED ENVIRONMENT")) problems.push("no SIMULATED ENVIRONMENT strip");
  if (facts.errors.length) problems.push(`error panel: ${facts.errors.join(" | ")}`);
  if (facts.textLength < 300) problems.push(`almost blank (${facts.textLength} characters)`);
  if (facts.sideways) problems.push(`scrolls sideways: ${facts.sideways}`);
  if (facts.markup.length) problems.push(`markup in <main>: ${facts.markup.slice(0, 5).join(", ")}`);
  if (source) {
    for (const want of source) if (!facts.topbar.includes(want)) problems.push(`top bar does not show ${want}`);
  }
  const expanded = await browser.run(expandedWidth);
  if (expanded.sideways) problems.push(`scrolls sideways when expanded: ${expanded.sideways}`);
  if (TOKEN) {
    const html = await browser.run(domHtml);
    if (html.includes(TOKEN)) problems.push("the operator token is in the DOM");
  }
  if (browser.console.length) problems.push(`console: ${browser.console.slice(0, 4).join(" | ")}`);
  record(problems.length === 0, name, problems.join("; "));
  return problems.length === 0;
}

function sourceLabels(summary) {
  const labels = [];
  const source = SOURCE_LABELS[summary?.provider_class ?? ""];
  if (source) labels.push(source);
  if (summary?.environment_simulated === true) labels.push("SIMULATED TARGET");
  return labels;
}

/** Save a screenshot: `scroll` is where to scroll first (null: the top; undefined: leave it). */
async function shoot(fileName, scroll) {
  if (!SHOTS) return;
  if (scroll) {
    const found = await browser.run(scrollProbe, { offset: 104, ...scroll });
    if (!found) note(`${fileName}: ${scroll.selector ?? scroll.heading} not found, shot from the top`);
  } else if (scroll === null) {
    await browser.run(() => window.scrollTo(0, 0));
  }
  await sleep(500);
  const bytes = await browser.screenshot(join(SHOTS, fileName));
  note(`screenshot ${fileName} (${Math.round(bytes / 1024)} KB)`);
}

async function checkPages(runs) {
  for (const viewport of [DESKTOP, PHONE]) {
    await browser.viewport(viewport);
    const size = `${viewport.width}x${viewport.height}`;
    for (const page of GLOBAL_PAGES) await checkPage(page, { viewport, name: `${LABELS[page]} @ ${size}` });
    for (const [kind, summary] of runs) {
      for (const page of RUN_PAGES) {
        await checkPage(page, {
          run: summary.run_id,
          source: sourceLabels(summary),
          viewport,
          name: `${LABELS[page]} · ${kind} ${summary.run_id} (${summary.state}) @ ${size}`,
        });
      }
    }
  }
}

/** The live demo: sign in, replay the golden recording, approve in the modal, see RESOLVED. */
async function demoFlow(golden) {
  await browser.viewport(DESKTOP);
  browser.console.length = 0;
  await browser.goto(`${BASE}/#/live`);
  const lab = await browser.waitFor(settledProbe, { label: LABELS.live }, 25_000, 300);
  if (!lab.ok) {
    record(false, "Incident Lab opens", "the page did not load");
    return null;
  }

  // Sign in through the top bar, as a presenter would.
  const signedInAlready = await browser.run(() => !!document.querySelector(".operator.signed-in"));
  if (!signedInAlready) {
    await browser.run(clickProbe, { selector: ".operator button", text: "Sign in" });
    await browser.waitFor(() => !!document.querySelector("#operator-token"), null, 5_000);
    await browser.run(typeProbe, { selector: "#operator-token", value: TOKEN });
    await browser.run(clickProbe, { selector: ".operator-form button[type=submit]" });
  }
  const signedIn = await browser.waitFor(() => document.querySelector(".operator.signed-in")?.textContent ?? null, null, 10_000);
  const session = await apiGet("/auth/session", TOKEN).catch(() => null);
  record(signedIn.ok, "Sign in with the operator token", signedIn.ok ? `signed in as ${session?.operator ?? "the operator"}` : "the top bar never showed a signed-in operator (wrong token?)");
  if (!signedIn.ok) return null;

  // The pace, then the golden recording's Replay button.
  const paceText = args.pace === "instant" ? "Instant" : "Watchable";
  await browser.run(clickProbe, { selector: '[role="radiogroup"][aria-label="Replay pace"] [role="radio"]', text: paceText });
  const paceOn = await browser.run(
    (text) => [...document.querySelectorAll('[role="radiogroup"][aria-label="Replay pace"] [role="radio"]')].some((b) => b.textContent.includes(text) && b.getAttribute("aria-checked") === "true"),
    paceText,
  );
  record(paceOn, `Replay pace set to ${paceText}`);
  const before = await browser.run(() => location.hash);
  const clicked = await browser.run(
    (title) => {
      const card = [...document.querySelectorAll("article.lab-replay")].find((a) => a.querySelector("h3")?.textContent?.trim() === title);
      const button = card ? [...card.querySelectorAll("button")].find((b) => b.textContent.includes("Replay this incident")) : null;
      if (!button) return "missing";
      if (button.disabled) return "disabled";
      button.click();
      return "clicked";
    },
    golden.title,
  );
  record(clicked === "clicked", `Click Replay on "${golden.title}"`, clicked === "clicked" ? "" : `the button is ${clicked}`);
  if (clicked !== "clicked") return null;
  const started = await browser.waitFor((prev) => (location.hash !== prev && /run=run-/.test(location.hash) ? location.hash : null), before, 15_000);
  const runId = started.ok ? /run=(run-[\w-]+)/.exec(started.value)[1] : null;
  record(!!runId, "The replay starts and the lab follows it", runId ? runId : "no run id in the address after 15 s");
  if (!runId) return null;

  // Watch it: the stage and the event count, until the approval dialog opens.
  const stages = [];
  const counts = [];
  let state = null;
  const t0 = Date.now();
  while (Date.now() - t0 < 180_000) {
    state = await browser.run(labState);
    if (state.stage && stages[stages.length - 1] !== state.stage) stages.push(state.stage);
    if (counts[counts.length - 1] !== state.events) counts.push(state.events);
    if (state.dialog && state.dialog.includes("Approve this exact action")) break;
    if (state.outcome) break;
    await sleep(400);
  }
  const waited = ((Date.now() - t0) / 1000).toFixed(1);
  record(counts.length >= 3, "The event feed advances while the replay plays", `event counts seen: ${counts.slice(0, 8).join(" → ")}${counts.length > 8 ? " → …" : ""}`);
  const dialog = state?.dialog ?? "";
  const dialogOk = /Approve this exact action\?/.test(dialog) && /digest/i.test(dialog) && /expires/i.test(dialog);
  record(dialogOk, "The approval modal shows the exact action, its digest and its expiry", dialogOk ? `after ${waited} s` : `no approval dialog after ${waited} s (stage ${state?.stage ?? "unknown"})`);
  if (!dialogOk) return runId;
  await sleep(1200); // the dialog's evidence and policy checks load
  await shoot("incident-lab-approval.png");

  await browser.run(clickProbe, { selector: '[role="dialog"] button', text: "Approve this exact action" });
  const approved = await browser.waitFor(() => (document.querySelector('[role="dialog"]')?.innerText ?? "").includes("Approved by"), null, 15_000);
  record(approved.ok, "Approve: the server records the approval", approved.ok ? "" : "the dialog never confirmed the approval");
  await browser.run(clickProbe, { selector: '[role="dialog"] button', text: "Close and watch the run" });

  const done = await browser.waitFor(
    () => {
      const outcome = document.querySelector(".lab-outcome")?.innerText ?? "";
      const facts = document.querySelector(".lab-outcome-grid")?.innerText ?? "";
      return outcome && facts ? { outcome, facts } : null;
    },
    null,
    90_000,
  );
  const last = await browser.run(labState);
  if (last.stage && stages[stages.length - 1] !== last.stage) stages.push(last.stage);
  const resolved = done.ok && done.value.outcome.includes("Resolved") && done.value.facts.includes("Recovery verified");
  record(resolved, "The lab shows RESOLVED with the oracle's verdict", done.ok ? done.value.outcome.split("\n")[0] : "no outcome after 90 s");
  const needed = ["Investigating", "Awaiting approval", "Resolved"];
  const missing = needed.filter((s) => !stages.includes(s));
  record(missing.length === 0, "The stage stepper advanced through the run", `${stages.join(" → ")}${missing.length ? ` (never showed ${missing.join(", ")})` : ""}`);

  // The API agrees with the screen.
  try {
    const detail = await apiGet(`/runs/${encodeURIComponent(runId)}`);
    const summary = detail.summary ?? {};
    const approvals = detail.record?.approvals ?? [];
    const approval = approvals.find((a) => a.decision === "approved");
    const ok =
      summary.state === "RESOLVED" &&
      summary.oracle?.verified === true &&
      summary.provider_class === "replay" &&
      !!approval &&
      String(approval.actor).startsWith("operator:");
    record(ok, "The API agrees: RESOLVED, oracle verified, a replay, approved by the operator", `${summary.state}, oracle ${summary.oracle?.verified ? "verified" : "not verified"}, ${summary.provider_class}, approved by ${approval?.actor ?? "nobody"}`);
  } catch (err) {
    record(false, "The API agrees with the lab", err.message);
  }
  if (browser.console.length) record(false, "No console output during the demo", browser.console.slice(0, 4).join(" | "));
  else record(true, "No console output during the demo");

  await shoot("incident-lab-resolved.png", { selector: "#run-panel" });
  return runId;
}

// ------------------------------------------------------------------------------------------
// Main
// ------------------------------------------------------------------------------------------

async function main() {
  healthInfo = await findApi();
  if (!healthInfo) {
    console.error(`No RECAP API answers at ${BASE}/health (or ${BASE}/api/health). Start it first.`);
    return 2;
  }
  console.log(`RECAP ${healthInfo.version ?? "?"} at ${API} · environment ${healthInfo.environment ?? "not reported"} · ${healthInfo.label ?? ""}`);
  const chrome = findChrome();
  if (!chrome) {
    console.error("No Chrome or Chromium found: pass --chrome <path> or set CHROME_PATH.");
    return 2;
  }
  if (SHOTS) mkdirSync(SHOTS, { recursive: true });

  const [runList, replays] = await Promise.all([apiGet("/runs?limit=100"), apiGet("/replays")]);
  const fixtures = replays.fixtures ?? [];
  const golden = fixtures.find((f) => f.launchable && (f.tags ?? []).includes("golden")) ?? fixtures.find((f) => f.launchable) ?? null;
  const pick = (test) => runList.find(test) ?? null;
  const runs = [
    ["golden replay", pick((r) => golden && r.replay?.fixture_id === golden.id)],
    ["scripted", pick((r) => r.provider_class === "scripted" && r.state === "RESOLVED")],
    ["blocked", pick((r) => r.state === "BLOCKED")],
    ["budget-exhausted", pick((r) => r.state === "BUDGET_EXHAUSTED")],
  ];
  for (const [kind, summary] of runs) if (!summary) note(`no ${kind} run in this data folder: its pages are not checked`);
  const found = runs.filter(([, summary]) => summary);

  browser = new Browser();
  try {
    await browser.launch(chrome);
  } catch (err) {
    console.error(`Could not start Chrome (${chrome}): ${err.message}`);
    await browser.close();
    return 2;
  }

  try {
    if (!args["skip-pages"]) await checkPages(found);

    let demoRun = null;
    if (args["skip-flow"]) {
      record("skip", "Live demo", "--skip-flow");
    } else if (!TOKEN) {
      record("skip", "Live demo", "no operator token (--token, RECAP_OPERATOR_TOKEN or --token-file)");
    } else if (!golden) {
      record(false, "Live demo", "GET /replays lists no recording this server can replay");
    } else {
      demoRun = await demoFlow(golden);
    }

    // The demo run's pages (or, without one, the seeded golden replay's) for the screenshots.
    const showcase = demoRun ?? runs[0][1]?.run_id ?? null;
    if (demoRun) {
      await browser.viewport(DESKTOP);
      const summary = (await apiGet(`/runs/${encodeURIComponent(demoRun)}`)).summary;
      for (const page of ["timeline", "evidence", "knowledge", "memory", "safety"]) {
        await checkPage(page, { run: demoRun, source: sourceLabels(summary), viewport: DESKTOP, name: `${LABELS[page]} · the demo run ${demoRun}` });
      }
      const operator = (await apiGet("/auth/session", TOKEN).catch(() => null))?.operator;
      if (operator) {
        await checkPage("safety", { run: demoRun, source: sourceLabels(summary), viewport: DESKTOP, name: "Safety & Approvals lists the operator's approval" });
        const text = await browser.run(() => document.querySelector("main").innerText);
        record(text.includes(operator), `The approval log names the operator (${operator})`);
      }
    }

    if (SHOTS) {
      const blocked = runs[2][1]?.run_id ?? null;
      const views = [
        ["overview.png", "overview", null, null],
        ["trace-explorer.png", "timeline", showcase, { heading: "Run timeline" }],
        ["evidence-decision.png", "evidence", showcase, { heading: "The decision" }],
        ["evidence.png", "evidence", blocked ?? showcase, { heading: "Evidence registry" }],
        ["knowledge.png", "knowledge", showcase, null],
        ["memory.png", "memory", showcase, { heading: "Episodes in memory" }],
        ["safety.png", "safety", blocked ?? showcase, { heading: "This run, layer by layer" }],
        ["evals.png", "evals", null, null],
        ["architecture.png", "architecture", null, null],
      ];
      await browser.viewport(DESKTOP);
      for (const [file, page, run, scroll] of views) {
        await browser.goto(`${BASE}/#/${page}${run ? `?run=${run}` : ""}`);
        await browser.waitFor(settledProbe, { label: LABELS[page], run }, 25_000, 300);
        await shoot(file, scroll ?? null);
      }
      await browser.viewport(PHONE);
      for (const [file, page, run] of [
        ["phone-overview.png", "overview", null],
        ["phone-incident-lab.png", "live", showcase],
      ]) {
        await browser.goto(`${BASE}/#/${page}${run ? `?run=${run}` : ""}`);
        await browser.waitFor(settledProbe, { label: LABELS[page], run }, 25_000, 300);
        await shoot(file, page === "live" ? { selector: "#run-panel" } : null);
      }
    }
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => r.ok === false);
  const passed = results.filter((r) => r.ok === true);
  const skipped = results.filter((r) => r.ok === "skip");
  console.log(`\n${passed.length} passed · ${failed.length} failed · ${skipped.length} skipped`);
  for (const r of failed) console.log(`  FAIL ${r.name}: ${r.detail}`);
  return failed.length ? 1 : 0;
}

main().then(
  (code) => process.exit(code),
  async (err) => {
    console.error(redact(err?.stack ?? err));
    await browser?.close();
    process.exit(2);
  },
);
