import type { ReactNode } from "react";
import { MAIN_FLOW, TERMINAL_STATES, stateLabel, stateTone } from "../format";
import type { Tone } from "../format";
import { fmtOffset } from "../view/lab";
import { Icon } from "./Icon";

type StepStatus = "done" | "current" | "stopped here" | "skipped" | "not reached";

/**
 * The incident lifecycle as a stepper. `states` is the sequence of states the run has
 * entered (from state.transition events or the checkpoint history), in order.
 * FAILED / BLOCKED / BUDGET_EXHAUSTED / QUOTA_EXHAUSTED are terminal side exits shown after the step
 * where the run stopped.
 *
 * `times`: when the run first entered each state (ms after its first event), shown under the
 * step. `caption`: what the current stage means, announced politely when it changes. Each
 * step's status is also written out for screen readers: it never depends on color alone.
 * `exit`: plain words for how the run ended when the stored terminal state alone would mislead
 * (M6: a run closed after its worker stopped is stored as FAILED but reads "Interrupted").
 */
export function StateStepper({
  states,
  running,
  times,
  caption,
  label = "Run stages",
  exit,
}: {
  states: string[];
  running: boolean;
  times?: Readonly<Record<string, number>>;
  caption?: ReactNode;
  label?: string;
  exit?: { label: string; tone: Tone } | null;
}) {
  const current = states.length ? states[states.length - 1] : null;
  const visited = new Set(states);
  const failed = current && TERMINAL_STATES.has(current) && current !== "RESOLVED" ? current : null;
  const flow: readonly string[] = MAIN_FLOW;
  // The last happy-path state reached before a terminal side exit.
  const stoppedAt = failed ? [...states].reverse().find((s) => flow.includes(s)) ?? null : null;
  const rounds = states.filter((s) => s === "INVESTIGATING").length;

  return (
    <div className="stepper">
      <ol aria-label={label}>
        {MAIN_FLOW.map((state, index) => {
          const isCurrent = state === current;
          const isStop = state === stoppedAt;
          const done = visited.has(state) && !isCurrent;
          let cls = "step";
          let status: StepStatus = "not reached";
          if (isCurrent) {
            cls += state === "RESOLVED" ? " ok" : state === "AWAITING_APPROVAL" ? " current warn" : " current";
            status = state === "RESOLVED" ? "done" : "current";
          } else if (isStop) {
            cls += ` stop tone-${stateTone(failed)}`;
            status = "stopped here";
          } else if (done) {
            cls += " done";
            status = "done";
          } else if (failed || current === "RESOLVED") {
            cls += " skipped";
            status = "skipped";
          }
          if (isCurrent && running) cls += " running";
          const at = times && state in times ? fmtOffset(times[state]) : "";
          return (
            <li key={state} className={cls} aria-current={isCurrent ? "step" : undefined}>
              <span className="step-node" aria-hidden="true">
                {done || (isCurrent && state === "RESOLVED") ? (
                  <Icon name="check" size={13} />
                ) : isStop ? (
                  <Icon name="x" size={13} />
                ) : (
                  index + 1
                )}
              </span>
              <span className="step-label">
                {stateLabel(state)}
                {state === "INVESTIGATING" && rounds > 1 ? <span className="step-count"> ×{rounds}</span> : null}
                <span className="sr-only">
                  {" "}
                  ({status}
                  {state === "INVESTIGATING" && rounds > 1 ? `, ${rounds} rounds` : ""})
                </span>
              </span>
              {at && (done || isCurrent || isStop) ? <span className="step-time">{at}</span> : null}
            </li>
          );
        })}
      </ol>
      {failed && exit ? (
        <div className={`stepper-exit tone-${exit.tone}`}>
          <Icon name="alert" size={14} />
          <span>
            <strong>{exit.label}</strong>
            {stoppedAt ? <> while {stateLabel(stoppedAt).toLowerCase()}</> : null}
            <span className="sr-only"> (stored state: {stateLabel(failed)})</span>
          </span>
        </div>
      ) : failed ? (
        <div className={`stepper-exit tone-${stateTone(failed)}`}>
          <Icon name={failed === "BUDGET_EXHAUSTED" || failed === "QUOTA_EXHAUSTED" ? "stop" : "x"} size={14} />
          <span>
            Ended in <strong>{stateLabel(failed)}</strong>
            {stoppedAt ? <> while {stateLabel(stoppedAt).toLowerCase()}</> : null}
          </span>
        </div>
      ) : null}
      {caption ? (
        <p className="stepper-now" role="status" aria-live="polite">
          {caption}
        </p>
      ) : null}
    </div>
  );
}
