import { MAIN_FLOW, TERMINAL_STATES, stateLabel, stateTone } from "../format";
import { Icon } from "./Icon";

/**
 * The incident lifecycle as a stepper. `states` is the sequence of states the run has
 * entered (from state.transition events or the checkpoint history), in order.
 * FAILED / BLOCKED / BUDGET_EXHAUSTED / QUOTA_EXHAUSTED are terminal side exits shown after the step
 * where the run stopped.
 */
export function StateStepper({ states, running }: { states: string[]; running: boolean }) {
  const current = states.length ? states[states.length - 1] : null;
  const visited = new Set(states);
  const failed = current && TERMINAL_STATES.has(current) && current !== "RESOLVED" ? current : null;
  const flow: readonly string[] = MAIN_FLOW;
  // The last happy-path state reached before a terminal side exit.
  const stoppedAt = failed ? [...states].reverse().find((s) => flow.includes(s)) ?? null : null;
  const rounds = states.filter((s) => s === "INVESTIGATING").length;

  return (
    <div className="stepper">
      <ol>
        {MAIN_FLOW.map((state, index) => {
          const isCurrent = state === current;
          const isStop = state === stoppedAt;
          const done = visited.has(state) && !isCurrent;
          let cls = "step";
          if (isCurrent) cls += state === "RESOLVED" ? " ok" : state === "AWAITING_APPROVAL" ? " current warn" : " current";
          else if (isStop) cls += ` stop tone-${stateTone(failed)}`;
          else if (done) cls += " done";
          else if (failed || current === "RESOLVED") cls += " skipped";
          if (isCurrent && running) cls += " running";
          return (
            <li key={state} className={cls} aria-current={isCurrent ? "step" : undefined}>
              <span className="step-node">
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
              </span>
            </li>
          );
        })}
      </ol>
      {failed ? (
        <div className={`stepper-exit tone-${stateTone(failed)}`}>
          <Icon name={failed === "BUDGET_EXHAUSTED" || failed === "QUOTA_EXHAUSTED" ? "stop" : "x"} size={14} />
          Ended in <strong>{stateLabel(failed)}</strong>
          {stoppedAt ? <> while {stateLabel(stoppedAt).toLowerCase()}</> : null}
        </div>
      ) : null}
    </div>
  );
}
