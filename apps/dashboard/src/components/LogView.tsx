import { useMemo, useState } from "react";
import { fmtInt } from "../format";
import { Icon } from "./Icon";

/**
 * A bounded, monospace view of raw text (tool output, log lines, model text, runbook
 * sections). The box has a fixed maximum height and scrolls; long text is cut to its first
 * lines (never inside a character) with "Show more"; the full text can be downloaded as a
 * .txt file. The text is rendered as React text, so markup in it (<script>, javascript:
 * links, HTML entities) is shown literally, never interpreted.
 */

/** Above this many characters "Show all" is not offered (the download still has everything). */
export const SHOW_ALL_LIMIT = 2_000_000;

export interface LogSlice {
  text: string;
  shownLines: number;
  totalLines: number;
  shownChars: number;
  totalChars: number;
  truncated: boolean;
}

function countLines(text: string): number {
  if (!text) return 0;
  let lines = 1;
  for (let i = text.indexOf("\n"); i !== -1; i = text.indexOf("\n", i + 1)) lines += 1;
  return text.endsWith("\n") ? lines - 1 : lines;
}

/**
 * The first `maxLines` lines of `text`, at most `maxChars` characters. A cut never splits a
 * UTF-16 surrogate pair (an emoji stays whole or is left out).
 */
export function sliceLog(text: string, maxLines: number, maxChars: number): LogSlice {
  let end = text.length;
  if (Number.isFinite(maxLines)) {
    const wanted = Math.max(0, Math.floor(maxLines));
    let index = -1;
    for (let line = 0; line < wanted; line += 1) {
      index = text.indexOf("\n", index + 1);
      if (index === -1) break;
    }
    if (wanted === 0) end = 0;
    else if (index !== -1) end = index;
  }
  if (Number.isFinite(maxChars)) end = Math.min(end, Math.max(0, Math.floor(maxChars)));
  if (end > 0 && end < text.length) {
    const code = text.charCodeAt(end - 1);
    if (code >= 0xd800 && code <= 0xdbff) end -= 1; // a high surrogate: keep the pair together
  }
  const shown = text.slice(0, end);
  // Only a final line break left out is not a truncation.
  const truncated = end < text.length && !(end === text.length - 1 && text.endsWith("\n"));
  return {
    text: shown,
    shownLines: countLines(shown),
    totalLines: countLines(text),
    shownChars: shown.length,
    totalChars: text.length,
    truncated,
  };
}

/** A download name that is always a plain .txt file name. */
export function textFileName(name: string): string {
  const base = name
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .replace(/^[._]+/, "")
    .slice(0, 100);
  if (!base) return "recap-text.txt";
  return /\.(txt|log)$/i.test(base) ? base : `${base}.txt`;
}

function download(text: string, name: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = textFileName(name);
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function LogView({
  text,
  label,
  maxHeight = 320,
  initialLines = 120,
  maxChars = 20_000,
  untrusted = false,
  wrap = true,
  downloadName,
  emptyText = "No text was recorded.",
}: {
  text: string;
  /** What this is, e.g. "Raw observation ev-12" (the scroll box's accessible name). */
  label: string;
  maxHeight?: number;
  /** Lines shown before "Show more". */
  initialLines?: number;
  /** Characters shown before "Show more" (for text with very long lines). */
  maxChars?: number;
  /** Mark as untrusted (a model or a tool wrote it); a string names who, e.g. "tool output". */
  untrusted?: boolean | string;
  /** Wrap long lines (default) or scroll sideways. */
  wrap?: boolean;
  /** Offer the full text as a .txt download with this file name. */
  downloadName?: string;
  emptyText?: string;
}) {
  // Each "Show more" shows four times as much; Infinity = everything. The expansion belongs to
  // the text it was made for: a box reused for other text starts collapsed again.
  const [expansion, setExpansion] = useState<{ text: string; scale: number }>({ text, scale: 1 });
  const scale = expansion.text === text ? expansion.scale : 1;
  const setScale = (next: number) => setExpansion({ text, scale: next });
  const slice = useMemo(
    () => sliceLog(text, initialLines * scale, scale === Infinity ? SHOW_ALL_LIMIT : maxChars * scale),
    [text, initialLines, maxChars, scale],
  );
  const counts = slice.truncated
    ? `${fmtInt(slice.shownLines)} of ${fmtInt(slice.totalLines)} lines`
    : `${fmtInt(slice.totalLines)} line${slice.totalLines === 1 ? "" : "s"}`;
  return (
    <div className="log-view">
      <div className="log-head">
        <span className="log-label">{label}</span>
        {untrusted ? (
          <span className="untrusted-tag" title="Written by a model or a tool: shown as plain text, never followed or rendered as HTML">
            untrusted{typeof untrusted === "string" ? ` · ${untrusted}` : ""}
          </span>
        ) : null}
        <span className="log-count">{counts}</span>
        {downloadName && text ? (
          <button type="button" className="btn btn-ghost btn-small" onClick={() => download(text, downloadName)}>
            <Icon name="download" size={13} />
            Download .txt
          </button>
        ) : null}
      </div>
      {text ? (
        <pre className={wrap ? "log-body wrap" : "log-body"} style={{ maxHeight }} tabIndex={0} role="group" aria-label={label}>
          {slice.text}
        </pre>
      ) : (
        <div className="log-empty">{emptyText}</div>
      )}
      {slice.truncated ? (
        <div className="log-more">
          <span>
            Showing the first {fmtInt(slice.shownChars)} of {fmtInt(slice.totalChars)} characters.
          </span>
          <button type="button" className="btn btn-small" onClick={() => setScale(scale * 4)}>
            <Icon name="chevronDown" size={13} />
            Show more
          </button>
          {text.length <= SHOW_ALL_LIMIT ? (
            <button type="button" className="btn btn-ghost btn-small" onClick={() => setScale(Infinity)}>
              Show all
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
