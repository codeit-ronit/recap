/**
 * The run stepper for a run closed after its worker stopped (M6 W3): it reads "Interrupted",
 * not "Ended in Failed", and keeps the stored state for screen readers and inspection.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { StateStepper } from "../src/components/StateStepper";
import { render, textOf } from "./support/render";

const STOPPED = ["CREATED", "RETRIEVING", "INVESTIGATING", "PROPOSING", "AWAITING_APPROVAL", "FAILED"];

test("an interrupted run's stepper says so, with the stored state kept", () => {
  const text = textOf(render(<StateStepper states={STOPPED} running={false} exit={{ label: "Interrupted", tone: "warn" }} />));
  assert.match(text, /Interrupted while awaiting approval \(stored state: Failed\)/);
  assert.doesNotMatch(text, /Ended in Failed/);
});

test("any other failed run still reads as the stored state says", () => {
  const text = textOf(render(<StateStepper states={STOPPED} running={false} />));
  assert.match(text, /Ended in Failed while awaiting approval/);
  assert.doesNotMatch(text, /Interrupted/);
});
