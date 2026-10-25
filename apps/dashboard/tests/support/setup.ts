/**
 * Injected first into every component-test bundle by scripts/test.mjs (esbuild `inject`).
 *
 * Any console.error or console.warn while a test file runs fails the test it happened in.
 * React's development build reports problems through console.error (a list without keys,
 * an unknown DOM attribute, invalid nesting, a bad prop type...), so a render with a React
 * warning fails too. The message is still printed, so the failure is easy to read.
 */

import { after, afterEach } from "node:test";
import { format } from "node:util";

const recorded: string[] = [];

for (const level of ["error", "warn"] as const) {
  const original = console[level].bind(console);
  console[level] = (...args: unknown[]) => {
    recorded.push(`console.${level}: ${format(...args)}`);
    original(...args);
  };
}

/** The console messages logged since the last call (and forgets them). */
export function takeConsoleMessages(): string[] {
  return recorded.splice(0, recorded.length);
}

function failOnMessages(when: string): void {
  const messages = takeConsoleMessages();
  if (messages.length) {
    throw new Error(`${messages.length} console message(s) ${when}; a component test must log nothing:\n${messages.join("\n")}`);
  }
}

afterEach(() => failOnMessages("during this test"));
after(() => failOnMessages("after the last test"));
