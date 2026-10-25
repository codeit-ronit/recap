/**
 * FilterBar (chips or selects for agent, round, tool) with its data helpers, and Pager
 * (offset/limit pagination) with its window arithmetic.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { FilterBar, activeFilterCount, applyFilters, filterOptions } from "../src/components/FilterBar";
import type { FilterSpec } from "../src/components/FilterBar";
import { Pager, pageWindow, paginate } from "../src/components/Pager";
import { render, tags, textOf, withAttr } from "./support/render";

interface Event {
  agent: string | null;
  round: number;
  tools: string[];
}

const EVENTS: Event[] = [
  { agent: "telemetry_investigator", round: 1, tools: ["get_logs"] },
  { agent: "infrastructure_investigator", round: 1, tools: ["get_deployments", "get_metrics"] },
  { agent: "telemetry_investigator", round: 2, tools: ["get_metrics"] },
  { agent: null, round: 10, tools: [] },
];

const readers = {
  agent: (e: Event) => e.agent,
  round: (e: Event) => e.round,
  tool: (e: Event) => e.tools,
};

// --- data helpers -------------------------------------------------------------------------------

test("options come from the data, with counts; empty values are left out", () => {
  assert.deepEqual(filterOptions(EVENTS, readers.agent), [
    { value: "infrastructure_investigator", label: "infrastructure_investigator", count: 1 },
    { value: "telemetry_investigator", label: "telemetry_investigator", count: 2 },
  ]);
});

test("rounds sort as numbers, not text; items with several values count once per value", () => {
  assert.deepEqual(
    filterOptions(EVENTS, readers.round).map((o) => o.value),
    ["1", "2", "10"],
  );
  assert.deepEqual(
    filterOptions(EVENTS, readers.tool).map((o) => [o.value, o.count]),
    [
      ["get_deployments", 1],
      ["get_logs", 1],
      ["get_metrics", 2],
    ],
  );
  assert.equal(filterOptions(EVENTS, readers.round, (v) => `round ${v}`)[0].label, "round 1");
});

test("applyFilters keeps items matching every active filter; no filter keeps everything", () => {
  assert.equal(applyFilters(EVENTS, {}, readers).length, 4);
  assert.equal(applyFilters(EVENTS, { agent: null, round: "" }, readers).length, 4);
  assert.deepEqual(applyFilters(EVENTS, { agent: "telemetry_investigator" }, readers), [EVENTS[0], EVENTS[2]]);
  assert.deepEqual(applyFilters(EVENTS, { agent: "telemetry_investigator", round: "2" }, readers), [EVENTS[2]]);
  assert.deepEqual(applyFilters(EVENTS, { tool: "get_metrics" }, readers), [EVENTS[1], EVENTS[2]]);
  assert.deepEqual(applyFilters(EVENTS, { unknown_key: "x" }, readers).length, 4);
  assert.equal(activeFilterCount({ agent: "a", round: null, tool: "" }), 1);
});

// --- FilterBar markup ---------------------------------------------------------------------------------

const FILTERS: FilterSpec[] = [
  { key: "agent", label: "Agent", options: filterOptions(EVENTS, readers.agent) },
  { key: "round", label: "Round", options: filterOptions(EVENTS, readers.round) },
  {
    key: "tool",
    label: "Tool",
    options: ["a", "b", "c", "d", "e", "f", "g"].map((value) => ({ value })),
  },
];

test("few options render as toggle chips with aria-pressed; many as a labelled select", () => {
  const html = render(<FilterBar filters={FILTERS} values={{ agent: "telemetry_investigator" }} onChange={() => undefined} />);
  assert.equal(withAttr(html, "role", "group")[0].attrs["aria-label"], "Filters");
  const agentChips = tags(html, "button").filter((b) => b.attrs.class === "filter-chip");
  // Agent: All + 2 · Round: All + 3
  assert.equal(agentChips.length, 7);
  const pressed = withAttr(html, "aria-pressed", "true");
  assert.equal(pressed.length, 2, "the chosen agent, and All for round");
  assert.match(textOf(html), /Agent All infrastructure_investigator 1 telemetry_investigator 2/);
  const select = tags(html, "select");
  assert.equal(select.length, 1);
  assert.equal(tags(html, "option").length, 8, "All + 7 tools");
  assert.match(textOf(html), /Tool All a b c d e f g/);
});

test("each chip group is labelled by its visible label", () => {
  const html = render(<FilterBar filters={FILTERS.slice(0, 1)} values={{}} onChange={() => undefined} />);
  const group = withAttr(html, "aria-labelledby")[0];
  const label = withAttr(html, "id", group.attrs["aria-labelledby"]);
  assert.equal(label.length, 1);
});

test("Clear filters appears only while a filter is active, with the count; the summary is a status", () => {
  const off = render(<FilterBar filters={FILTERS} values={{}} onChange={() => undefined} onClear={() => undefined} />);
  assert.doesNotMatch(textOf(off), /Clear filters/);
  const on = render(
    <FilterBar filters={FILTERS} values={{ agent: "x", round: "1" }} onChange={() => undefined} onClear={() => undefined} summary="Showing 1 of 4 events" />,
  );
  assert.match(textOf(on), /Clear filters \(2\)/);
  assert.equal(withAttr(on, "role", "status").length, 1);
  assert.match(textOf(on), /Showing 1 of 4 events/);
});

test("a deep-linked value absent from the data stays visible and chosen", () => {
  const html = render(<FilterBar filters={FILTERS.slice(0, 1)} values={{ agent: "ghost_agent" }} onChange={() => undefined} />);
  assert.match(textOf(html), /ghost_agent \(not in this list\)/);
  assert.equal(withAttr(html, "aria-pressed", "true").length, 1);
});

test("hostile option text is escaped", () => {
  const html = render(
    <FilterBar filters={[{ key: "tool", label: "Tool", options: [{ value: "<script>alert(1)</script>" }] }]} values={{}} onChange={() => undefined} />,
  );
  assert.doesNotMatch(html, /<script/);
});

// --- Pager ---------------------------------------------------------------------------------------------

test("pageWindow: first, middle and last pages of a known total", () => {
  const first = pageWindow(0, 50, 1234);
  assert.deepEqual([first.from, first.to, first.page, first.pages, first.hasPrev, first.hasNext], [1, 50, 1, 25, false, true]);
  const middle = pageWindow(50, 50, 1234);
  assert.deepEqual([middle.from, middle.to, middle.page, middle.prevOffset, middle.nextOffset], [51, 100, 2, 0, 100]);
  const last = pageWindow(1200, 50, 1234);
  assert.deepEqual([last.from, last.to, last.page, last.hasNext, last.lastOffset], [1201, 1234, 25, false, 1200]);
});

test("pageWindow clamps a bad offset or limit, and handles an empty list", () => {
  assert.equal(pageWindow(5000, 50, 1234).offset, 1200);
  assert.equal(pageWindow(-10, 50, 1234).offset, 0);
  assert.equal(pageWindow(0, 0, 10).limit, 1);
  const empty = pageWindow(0, 50, 0);
  assert.deepEqual([empty.from, empty.to, empty.pages, empty.hasNext, empty.hasPrev], [0, 0, 1, false, false]);
});

test("pageWindow without a total pages forward while the server says there is more", () => {
  const w = pageWindow(100, 50, null, { shown: 50, hasMore: true });
  assert.deepEqual([w.from, w.to, w.pages, w.hasNext, w.lastOffset], [101, 150, null, true, null]);
  assert.equal(pageWindow(100, 50, null, { shown: 12, hasMore: false }).to, 112);
});

test("paginate slices one page of a list in memory", () => {
  const items = Array.from({ length: 7 }, (_, i) => i);
  assert.deepEqual(paginate(items, 3, 3), [3, 4, 5]);
  assert.deepEqual(paginate(items, 6, 3), [6]);
  assert.deepEqual(paginate(items, 99, 3), [6]);
});

test("the pager is a labelled navigation with a status summary and disabled edges", () => {
  const html = render(<Pager offset={50} limit={50} total={1234} noun="events" onChange={() => undefined} pageSizes={[50, 100]} onPageSizeChange={() => undefined} />);
  assert.equal(tags(html, "nav")[0].attrs["aria-label"], "Pagination");
  assert.match(textOf(html), /51–100 of 1,234 events/);
  assert.match(textOf(html), /Page 2 of 25/);
  const buttons = tags(html, "button");
  assert.deepEqual(
    buttons.map((b) => b.attrs["aria-label"]),
    ["First page", "Previous page", "Next page", "Last page"],
  );
  assert.ok(buttons.every((b) => !("disabled" in b.attrs)));
  assert.equal(tags(html, "select").length, 1);

  const first = render(<Pager offset={0} limit={50} total={30} onChange={() => undefined} />);
  const disabled = tags(first, "button").filter((b) => "disabled" in b.attrs);
  assert.equal(disabled.length, 4, "one page: nothing to move to");
  assert.match(textOf(first), /1–30 of 30 items/);
  assert.match(textOf(render(<Pager offset={0} limit={50} total={0} noun="events" onChange={() => undefined} />)), /No events/);
});

test("without a total the pager has no Last button", () => {
  const html = render(<Pager offset={0} limit={50} shown={50} hasMore onChange={() => undefined} />);
  assert.equal(tags(html, "button").length, 3);
  assert.match(textOf(html), /1–50 items/);
});
