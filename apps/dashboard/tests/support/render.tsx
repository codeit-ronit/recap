/**
 * Helpers for component tests: render on the server (react-dom/server, no DOM needed) and read
 * the markup. Effects do not run in a server render, so these tests check what a component
 * shows for given props: labels, roles, aria attributes, escaping, empty/loading/error states.
 */

import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { takeConsoleMessages } from "./setup";

/** The element's HTML. Throws if React (or anything else) logged to the console meanwhile. */
export function render(element: ReactElement): string {
  const html = renderToStaticMarkup(element);
  const messages = takeConsoleMessages();
  if (messages.length) throw new Error(`The render logged to the console:\n${messages.join("\n")}`);
  return html;
}

const ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'", "#x27": "'", nbsp: " " };

/** The visible text of some markup: tags dropped (as spaces), entities decoded, spaces collapsed. */
export function textOf(html: string): string {
  return html
    .replace(/<[^>]+>/g, " ")
    .replace(/&(amp|lt|gt|quot|#39|#x27|nbsp);/g, (_, name: string) => ENTITIES[name])
    .replace(/\s+/g, " ")
    .trim();
}

export interface Tag {
  name: string;
  attrs: Record<string, string>;
}

/** Every opening tag named `name` (or every tag), in document order, with its attributes. */
export function tags(html: string, name?: string): Tag[] {
  const found: Tag[] = [];
  const tagRe = /<([a-zA-Z][\w-]*)((?:\s+[^\s=>/]+(?:="[^"]*")?)*)\s*\/?>/g;
  for (const match of html.matchAll(tagRe)) {
    if (name && match[1] !== name) continue;
    const attrs: Record<string, string> = {};
    for (const attr of match[2].matchAll(/([^\s=>/]+)(?:="([^"]*)")?/g)) attrs[attr[1]] = attr[2] ?? "";
    found.push({ name: match[1], attrs });
  }
  return found;
}

/** Tags carrying attribute `attr` (with value `value` when given). */
export function withAttr(html: string, attr: string, value?: string): Tag[] {
  return tags(html).filter((tag) => attr in tag.attrs && (value === undefined || tag.attrs[attr] === value));
}

/** How many times `needle` occurs in `haystack`. */
export function count(haystack: string, needle: string): number {
  return needle ? haystack.split(needle).length - 1 : 0;
}

/** A usePolling result for props: loaded data, an error, or still loading. */
export function polled<T>(data: T | undefined, error: Error | null = null, loading = data === undefined && !error) {
  return { data, error: error as never, loading, refresh: () => undefined };
}
