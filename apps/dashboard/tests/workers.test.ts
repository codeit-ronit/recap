/**
 * Tests for what the dashboard says about runs executed by workers (M6 W3, the Redis execution
 * backend): a queued run, an interrupted one, and how each ended (src/view/lab.ts queueNotice,
 * src/view/system.ts outcome looks, src/format.ts stillGoing). Run with `npm test`.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import type { JobView } from "../src/dto.ts";
import { stillGoing } from "../src/format.ts";
import type { Interruption } from "../src/types.ts";
import { outcomeView, queueNotice } from "../src/view/lab.ts";
import { glanceCounts, outcomeLook } from "../src/view/system.ts";

const JOB: JobView = {
  job_id: "job-1",
  status: "queued",
  run_id: "run-2",
  runs: ["run-1", "run-2"],
  scenario_id: "bad-deployment",
  attempt: 1,
  max_attempts: 3,
  position: 2,
  worker: null,
  worker_alive: null,
  submitted_at: "2026-11-08T10:00:00Z",
  claimed_at: null,
  finished_at: null,
  category: "worker_lost_before_write",
  detail: "the run stopped in INVESTIGATING; no write was sent",
  cancel_requested: false,
};

function interruption(over: Partial<Interruption> = {}): Interruption {
  return {
    at: "2026-11-08T10:01:00Z",
    phase: "before_write",
    write: "none",
    needs_reconciliation: false,
    detail: "the run stopped in INVESTIGATING; no write was sent",
    retried_as: "run-2",
    cause: "worker_lost",
    ...over,
  };
}

test("a run that may still add events: running, queued or interrupted", () => {
  for (const status of ["running", "queued", "interrupted"]) assert.equal(stillGoing(status), true, status);
  for (const status of ["finished", "external", "cancelled", "published", "error", "unknown", null]) {
    assert.equal(stillGoing(status), false, String(status));
  }
});

test("a queued run says where it waits and which attempt comes next", () => {
  const notice = queueNotice("queued", JOB, null, false);
  assert.ok(notice);
  assert.equal(notice.title, "Queued for a worker");
  assert.equal(notice.body, "Position 2 in the queue, attempt 2 of 3. Its events appear here as soon as a worker starts it.");
  assert.equal(queueNotice("queued", JOB, null, true), null); // its events flow: nothing to add
  assert.equal(queueNotice("queued", { ...JOB, attempt: 0, position: null }, null, false)?.body.startsWith("In the queue. "), true);
});

test("an interrupted run that ran again links its next run; one that may have written does not", () => {
  const again = queueNotice("finished", null, interruption(), true);
  assert.ok(again);
  assert.equal(again.tone, "warn");
  assert.equal(again.next, "run-2");
  assert.match(again.body, /no fix reached the target/);
  assert.match(again.body, /asks for its own approval/);

  const person = queueNotice("finished", null, interruption({ phase: "after_write", write: "applied", needs_reconciliation: true, retried_as: null }), true);
  assert.ok(person);
  assert.equal(person.tone, "bad");
  assert.equal(person.title, "Needs reconciliation: a person must check the target");
  assert.equal(person.next, null);
  assert.match(person.body, /after a fix was sent/);
  assert.match(person.body, /not retried/);

  const stopped = queueNotice("finished", null, interruption({ cause: "worker_stopped", phase: "awaiting_approval" }), true);
  assert.match(stopped?.body ?? "", /was stopped while it waited for a person's approval/);
});

test("a worker that stopped and a run cancelled before it started are said plainly", () => {
  assert.equal(queueNotice("interrupted", JOB, null, true)?.title, "Its worker stopped");
  assert.equal(queueNotice("cancelled", JOB, null, false)?.body, "It was taken off the queue: nothing ran.");
  assert.equal(queueNotice("finished", null, null, true), null);
  assert.equal(queueNotice("running", null, null, true), null);
});

test("the two new outcomes have words, tones and an at-a-glance group", () => {
  assert.equal(outcomeLook("interrupted").label, "Interrupted");
  assert.equal(outcomeLook("needs_reconciliation").tone, "bad");
  assert.equal(outcomeView("needs_reconciliation").label, "Needs reconciliation");
  assert.match(outcomeView("interrupted").sentence, /before any fix could reach the target/);
  const counts = glanceCounts([
    { outcome: "interrupted", terminal: true },
    { outcome: "needs_reconciliation", terminal: true },
  ]);
  assert.equal(counts.other, 1);
  assert.equal(counts.failed, 1);
});
