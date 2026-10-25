/**
 * Loading, empty (with a reason) and error (with the message and a retry) states, PolledView,
 * and the older EmptyState / ErrorState / Loading / NoRuns / ErrorBoundary that now share them.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError } from "../src/api";
import { EmptyState, ErrorBoundary, ErrorState, Loading, NoRuns } from "../src/components/EmptyState";
import { EmptyView, ErrorView, LoadingView, PolledView } from "../src/components/StateViews";
import { polled, render, tags, textOf, withAttr } from "./support/render";

const http500 = new ApiError("database is locked", 500, "/runs", { detail: "database is locked" }, false);
const offline = new ApiError("Cannot reach the RECAP API.", 0, "/runs", null, true);

test("loading: a polite status with its label (block or inline)", () => {
  const html = render(<LoadingView label="Loading runs…" />);
  assert.equal(withAttr(html, "role", "status").length, 1);
  assert.equal(textOf(html), "Loading runs…");
  assert.equal(tags(html)[0].name, "div");
  assert.equal(tags(render(<LoadingView inline />))[0].name, "span");
});

test("empty: a title and the reason there is nothing", () => {
  const html = render(<EmptyView title="No runs yet" reason="The backend has recorded no runs." icon="live" />);
  assert.equal(textOf(html), "No runs yet The backend has recorded no runs.");
});

test("error: the server's message, the HTTP status, an alert role, and Retry only when it can retry", () => {
  let retried = 0;
  const html = render(<ErrorView error={http500} onRetry={() => (retried += 1)} />);
  assert.equal(withAttr(html, "role", "alert").length, 1);
  assert.match(textOf(html), /Request failed · HTTP 500 database is locked Retry/);
  const button = tags(html, "button");
  assert.equal(button.length, 1);
  assert.equal(button[0].attrs.type, "button");
  assert.equal(retried, 0, "rendering never retries by itself");
  assert.equal(tags(render(<ErrorView error={http500} />), "button").length, 0);
  assert.match(textOf(render(<ErrorView error="plain message" title="Could not load" />)), /^Could not load plain message$/);
});

test("an unreachable API makes a view wait (status, not alert): the shell shows the one banner", () => {
  const html = render(<ErrorView error={offline} onRetry={() => undefined} />);
  assert.equal(withAttr(html, "role", "alert").length, 0);
  assert.equal(withAttr(html, "role", "status").length, 1);
  assert.match(textOf(html), /Waiting for the RECAP API/);
  assert.match(textOf(html), /Retry/);
});

test("PolledView: loading, then an error with Retry, empty with its reason, or the data", () => {
  const view = (state: ReturnType<typeof polled<string[]>>) =>
    textOf(
      render(
        <PolledView polled={state} empty={(d) => (d.length ? null : "No episodes were written yet.")} emptyTitle="No episodes" loadingLabel="Loading episodes…">
          {(d) => <ul>{d.map((x) => <li key={x}>{x}</li>)}</ul>}
        </PolledView>,
      ),
    );
  assert.equal(view(polled<string[]>(undefined)), "Loading episodes…");
  assert.match(view(polled<string[]>(undefined, http500)), /database is locked Retry/);
  assert.equal(view(polled<string[]>([])), "No episodes No episodes were written yet.");
  assert.equal(view(polled<string[]>(["ep-1", "ep-2"])), "ep-1 ep-2");
});

test("PolledView keeps data on screen when a refresh fails, with a notice and Retry", () => {
  const html = render(
    <PolledView polled={polled<string[]>(["ep-1"], http500)}>{(d) => <p>{d.join(",")}</p>}</PolledView>,
  );
  assert.match(textOf(html), /Showing the last data received\. Refreshing failed: database is locked Retry ep-1/);
});

test("PolledView with nothing requested shows its idle content, not a spinner", () => {
  const idle = { data: undefined, error: null, loading: false, refresh: () => undefined };
  assert.equal(textOf(render(<PolledView polled={idle} idle={<p>Pick a run</p>}>{() => null}</PolledView>)), "Pick a run");
});

test("the older state components render the shared views (pages keep working unchanged)", () => {
  assert.equal(textOf(render(<EmptyState title="Nothing">because</EmptyState>)), "Nothing because");
  assert.match(textOf(render(<ErrorState error={http500} title="Could not load the run" />)), /Could not load the run · HTTP 500/);
  assert.match(textOf(render(<ErrorState error={http500} onRetry={() => undefined} />)), /Retry$/);
  assert.equal(textOf(render(<Loading />)), "Loading…");
  const noRuns = render(<NoRuns />);
  assert.match(textOf(noRuns), /Start one in the Incident Lab/);
  assert.equal(tags(noRuns, "a")[0].attrs.href, "#/live");
});

test("the error boundary renders its children when nothing throws", () => {
  assert.equal(textOf(render(<ErrorBoundary resetKey="overview"><p>fine</p></ErrorBoundary>)), "fine");
});

test("error text is escaped", () => {
  const html = render(<ErrorView error={new ApiError(`<img src=x onerror=alert(1)>`, 400, "/x", null, false)} />);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});
