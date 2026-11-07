/**
 * Status display: StatusToken (icon + text, never color alone), the lifecycle chips,
 * DataSourceBadge (where decisions came from, and the target) and EnvironmentBanner. Every
 * label must come from a server field; nothing is inferred from a name.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { HypothesisChip, OutcomeChip, StateChip } from "../src/components/Badge";
import { DataSourceBadge, describeSource, describeTarget, summarySource } from "../src/components/DataSourceBadge";
import { EnvironmentBanner, SIMULATED_ENVIRONMENT, environmentStatus } from "../src/components/EnvironmentBanner";
import { StateToken, StatusToken, TONE_ICONS, stateIcon } from "../src/components/StatusToken";
import type { Tone } from "../src/format";
import { count, render, tags, textOf, withAttr } from "./support/render";

const HOSTILE = `<script>alert("x")</script><img src=x onerror=alert(1)>`;

// --- StatusToken --------------------------------------------------------------------------------

test("a status token always has text and an icon; the tone only repeats them", () => {
  const html = render(<StatusToken tone="ok" label="Ready" detail="v1.2" title="from GET /providers" />);
  assert.equal(textOf(html), "Ready v1.2");
  assert.equal(tags(html, "svg").length, 1);
  assert.equal(tags(html, "svg")[0].attrs["aria-hidden"], "true");
  assert.match(html, /class="status-token tone-ok status-soft"/);
  assert.match(html, /title="from GET \/providers"/);
});

test("each tone has its own icon shape, so status never depends on color", () => {
  const tones: Tone[] = ["ok", "warn", "bad", "info", "accent", "neutral"];
  const icons = tones.map((tone) => TONE_ICONS[tone]);
  assert.equal(new Set(icons).size, tones.length);
  const shapes = tones.map((tone) => {
    const html = render(<StatusToken tone={tone} label="x" />);
    return html.slice(html.indexOf("<svg"), html.indexOf("</svg>"));
  });
  assert.equal(new Set(shapes).size, tones.length);
});

test("plain, outline and small variants; an empty detail renders nothing extra", () => {
  assert.match(render(<StatusToken label="API online" variant="plain" size="sm" />), /status-plain status-sm/);
  assert.match(render(<StatusToken label="x" variant="outline" />), /status-outline/);
  assert.equal(count(render(<StatusToken label="x" detail="" />), "status-detail"), 0);
});

test("lifecycle states: done, stopped, waiting for a person and in progress look different", () => {
  assert.equal(stateIcon("RESOLVED"), "check");
  assert.equal(stateIcon("FAILED"), "x");
  assert.equal(stateIcon("BUDGET_EXHAUSTED"), "stop");
  assert.equal(stateIcon("AWAITING_APPROVAL"), "key");
  assert.equal(stateIcon("INVESTIGATING"), "clock");
  assert.equal(stateIcon(null), "minus");
  const html = render(<StateToken state="AWAITING_APPROVAL" />);
  assert.equal(textOf(html), "Awaiting approval");
  assert.match(html, /tone-warn/);
  assert.match(html, /title="AWAITING_APPROVAL"/);
});

test("the older chips now carry an icon instead of a colored dot", () => {
  for (const html of [render(<StateChip state="RESOLVED" />), render(<OutcomeChip outcome="failed" />), render(<HypothesisChip status="supported" />)]) {
    assert.equal(tags(html, "svg").length, 1);
    assert.equal(count(html, "chip-dot"), 0);
  }
  assert.equal(textOf(render(<StateChip state="BLOCKED" />)), "Blocked");
});

// --- DataSourceBadge ----------------------------------------------------------------------------

test("each provider class reads as itself, from ProviderInfo.origin", () => {
  assert.equal(describeSource({ origin: "scripted", kind: "scripted" }).label, "SCRIPTED BASELINE");
  assert.equal(describeSource({ origin: "local_llm", model: "qwen3:8b" }).label, "LOCAL LLM");
  assert.equal(describeSource({ origin: "local_llm", model: "qwen3:8b" }).detail, "qwen3:8b");
  assert.equal(describeSource({ origin: "free_hosted_llm", model: "llama-3.1-8b" }).label, "FREE HOSTED LLM");
  assert.equal(describeSource({ origin: "paid_llm" }).label, "PAID LLM");
  assert.equal(describeSource({ origin: "replay" }).label, "REPLAY · RECORDED REAL-LLM RUN");
  assert.equal(describeSource(null).label, "SOURCE NOT RECORDED");
});

test("nothing is inferred from a name: a provider called like a model, without an origin, is not recorded", () => {
  const view = describeSource({ name: "ollama:qwen3:8b", kind: "llm", model: "qwen3:8b" });
  assert.equal(view.kind, "unknown");
  assert.equal(view.label, "SOURCE NOT RECORDED");
  // Runs from before origins existed: only the kind the server recorded counts.
  assert.equal(describeSource({ kind: "scripted" }).kind, "scripted");
  assert.equal(describeSource({ kind: "replay" }).kind, "replay");
});

test("a replay names the recorded model and the recording from replay_of, and says no model is called", () => {
  const view = describeSource({
    origin: "replay",
    model: "qwen3:8b",
    replay_of: { original_provider: "ollama", model: "qwen3:8b", created_at: "2026-10-25T10:00:00Z", recording_id: "rec-abc" },
  });
  assert.equal(view.detail, "qwen3:8b");
  assert.match(view.title, /recorded with ollama, model qwen3:8b, on 2026-10-25T10:00:00Z, recording rec-abc/);
  assert.match(view.title, /no model is called/);
  assert.equal(view.tone, "warn");
});

test("the target comes from is_simulated: simulated, live, or not recorded", () => {
  assert.equal(describeTarget({ kind: "simulated", is_simulated: true }).label, "SIMULATED TARGET");
  assert.equal(describeTarget({ kind: "kind", is_simulated: false }).label, "LIVE TARGET · kind");
  assert.equal(describeTarget({ kind: "sregym", is_simulated: false }).label, "LIVE TARGET · sregym");
  assert.equal(describeTarget({ kind: "simulated" }).label, "TARGET NOT RECORDED");
  assert.equal(describeTarget(null).label, "TARGET NOT RECORDED");
  // Only is_simulated decides: a kind named like the simulator, but not simulated, is live.
  assert.equal(describeTarget({ kind: "simulated", is_simulated: false }).label, "LIVE TARGET · simulated");
  assert.equal(describeTarget({ kind: "kind", is_simulated: false }).tone, "bad");
});

test("a run of the self-hosted model on a real cluster reads LOCAL LLM / LIVE TARGET, from its summary", () => {
  for (const kind of ["kind", "sregym"]) {
    const run = summarySource({ provider_class: "local_llm", provider_kind: "llm", model: "qwen3:8b", environment_kind: kind, environment_simulated: false });
    assert.equal(textOf(render(<DataSourceBadge {...run} />)), `LOCAL LLM qwen3:8b / LIVE TARGET · ${kind}`);
    // A model on the owner's VM is still a self-hosted model: the badge says where it may run.
    assert.match(describeSource(run.provider).title, /on this machine or the owner's own VM/);
  }
});

test("the badge groups decision source and target under one accessible name", () => {
  const html = render(
    <DataSourceBadge
      provider={{ origin: "replay", model: "qwen3:8b", replay_of: { model: "qwen3:8b" } }}
      environment={{ kind: "simulated", is_simulated: true, name: "RECAP simulated shop" }}
    />,
  );
  assert.equal(withAttr(html, "role", "group").length, 1);
  assert.equal(withAttr(html, "aria-label", "Data source").length, 1);
  assert.equal(textOf(html), "REPLAY · RECORDED REAL-LLM RUN qwen3:8b / SIMULATED TARGET");
  assert.equal(tags(html, "svg").length, 2);
});

test("environment left out: the source alone; environment null: the target reads not recorded", () => {
  assert.equal(textOf(render(<DataSourceBadge provider={{ origin: "local_llm", model: "qwen3:8b" }} />)), "LOCAL LLM qwen3:8b");
  assert.equal(
    textOf(render(<DataSourceBadge provider={{ origin: "scripted" }} environment={null} />)),
    "SCRIPTED BASELINE / TARGET NOT RECORDED",
  );
});

test("a typed run summary (GET /runs) maps onto the badge without guessing", () => {
  const replayed = summarySource({
    provider_class: "replay",
    provider_kind: "replay",
    model: "qwen3:8b",
    environment_kind: "simulated",
    environment_simulated: true,
    replay: { model: "qwen3:8b", original_provider: "ollama", recorded_at: "2026-10-25T09:00:00Z", recording_id: "rec-1" },
  });
  assert.equal(textOf(render(<DataSourceBadge {...replayed} />)), "REPLAY · RECORDED REAL-LLM RUN qwen3:8b / SIMULATED TARGET");
  assert.match(describeSource(replayed.provider).title, /recorded with ollama, model qwen3:8b, on 2026-10-25T09:00:00Z, recording rec-1/);

  const scripted = summarySource({ provider_class: "scripted", provider_kind: "scripted", model: null, environment_simulated: null });
  assert.equal(textOf(render(<DataSourceBadge {...scripted} />)), "SCRIPTED BASELINE / TARGET NOT RECORDED");
  assert.equal(describeSource(summarySource({}).provider).label, "SOURCE NOT RECORDED");
});

test("a red-team planner is flagged next to the source", () => {
  const html = render(<DataSourceBadge provider={{ origin: "scripted", red_team: true }} />);
  assert.match(textOf(html), /SCRIPTED BASELINE RED-TEAM PLANNER/);
});

test("hostile text in server fields is escaped, never markup", () => {
  const html = render(
    <DataSourceBadge provider={{ origin: "local_llm", model: HOSTILE }} environment={{ is_simulated: true, name: HOSTILE }} />,
  );
  assert.doesNotMatch(html, /<script|<img/);
  assert.match(html, /&lt;script&gt;alert\(&quot;x&quot;\)&lt;\/script&gt;/);
});

// --- EnvironmentBanner ----------------------------------------------------------------------------

test("environmentStatus: simulated and live from GET /system, the /health label until then, else unknown", () => {
  const simulated = environmentStatus({ kind: "simulated", is_simulated: true, name: "RECAP simulated shop", namespace: "shop" });
  assert.equal(simulated.state, "simulated");
  assert.equal(simulated.label, SIMULATED_ENVIRONMENT);
  assert.equal(simulated.detail, "RECAP simulated shop · kind simulated · namespace shop");
  assert.equal(simulated.tone, "warn");

  const live = environmentStatus({ kind: "sregym", is_simulated: false });
  assert.equal(live.state, "live");
  assert.equal(live.label, "LIVE ENVIRONMENT · sregym");
  assert.equal(live.tone, "bad");

  // The M5 targets as GET /system names them: name, kind and namespace in the detail line.
  const kind = environmentStatus({ kind: "kind", is_simulated: false, name: "Kind cluster recap-m5", namespace: "recap-demo" });
  assert.equal(kind.label, "LIVE ENVIRONMENT · kind");
  assert.equal(kind.detail, "Kind cluster recap-m5 · kind kind · namespace recap-demo");
  const sregym = environmentStatus({ kind: "sregym", is_simulated: false, name: "SREGym on Kind cluster kind", namespace: "hotel-reservation" });
  assert.equal(sregym.detail, "SREGym on Kind cluster kind · kind sregym · namespace hotel-reservation");
  assert.match(sregym.title, /approved mitigations change a real system/);

  const reported = environmentStatus(undefined, { environment: "simulated", label: "SIMULATED LOCAL ENVIRONMENT · V1" });
  assert.equal(reported.state, "reported");
  assert.equal(reported.label, "SIMULATED LOCAL ENVIRONMENT · V1");
  assert.equal(reported.tone, "neutral");

  const unknown = environmentStatus(null, null);
  assert.equal(unknown.state, "unknown");
  assert.doesNotMatch(unknown.label, /SIMULATED|LIVE/);
});

test("the banner is a labelled region whose status line is announced, with its right-hand slot", () => {
  const html = render(
    <EnvironmentBanner environment={{ kind: "simulated", is_simulated: true, description: "No real cluster is involved." }}>
      <span>API online</span>
    </EnvironmentBanner>,
  );
  assert.equal(tags(html, "section")[0].attrs["aria-label"], "Environment");
  assert.equal(withAttr(html, "role", "status").length, 1);
  assert.match(textOf(html), /^SIMULATED ENVIRONMENT kind simulated No real cluster is involved\. API online$/);
});

test("the banner escapes a hostile description", () => {
  const html = render(<EnvironmentBanner environment={{ is_simulated: true, description: HOSTILE, name: HOSTILE }} />);
  assert.doesNotMatch(html, /<script|<img/);
  assert.match(textOf(html), /<script>alert\("x"\)<\/script>/);
});
