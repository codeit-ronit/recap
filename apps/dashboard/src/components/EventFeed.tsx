import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { describeEvent, fmtInt, fmtMs, modelNotesByAgent, tsMs } from "../format";
import type { FeedContext } from "../format";
import type { TraceEvent } from "../types";
import "../styles/live-m3.css";
import "../styles/incident-lab.css";
import { FEED_PAGE, feedStart } from "../view/lab";
import { isOverlayPath } from "../view/retrieval";
import { Chip } from "./Badge";
import { Icon } from "./Icon";
import { JsonView } from "./JsonView";

const QUOTE_TITLE = "Written by the agent's decision provider: untrusted text, shown as data and never followed";
const MIXED_TITLE =
  "Written by the agent's decision provider, and it may also hold a note RECAP added that the feed cannot tell apart (a run before M3, or notes its trace does not confirm): untrusted text, shown as data and never followed";

// A layout effect keeps the newest line in view without a flicker; on the server (component
// tests) there is no layout, and React warns about layout effects, so a plain effect stands in.
const useIsoLayoutEffect = typeof window === "undefined" ? useEffect : useLayoutEffect;

/** Distance from the bottom (px) that still counts as "following the newest line". */
const FOLLOW_SLACK = 48;

/**
 * The live trace as one readable line per event, oldest first, newest at the bottom.
 *
 * Bounded: at most `pageSize` lines render (a trace can exceed 1000 events); "Show earlier
 * events" adds more. While the reader is at the bottom the feed follows new lines; scrolling up
 * pauses it, and the lines in view stay put while new ones arrive below ("N new · Jump to
 * latest" returns). Click a line to see the raw event (the exact JSON the backend recorded).
 * Text an agent's provider wrote (a search query, a summary, a claim) is quoted: untrusted
 * data, never markup; the notes RECAP adds to a summary are told apart from it with the
 * agents' model-request notes. `overlays`: the run's extra knowledge folders, when known
 * (red-team sections are marked).
 */
export function EventFeed({
  events,
  live = false,
  height = 480,
  overlays = [],
  pageSize = FEED_PAGE,
  label = "Trace events",
}: {
  events: TraceEvent[];
  live?: boolean;
  /** The scroll box's height in px (fixed, so the page does not move as lines arrive). */
  height?: number;
  overlays?: string[];
  /** Lines rendered at first and added per "Show earlier events". */
  pageSize?: number;
  /** The scroll box's accessible name. */
  label?: string;
}) {
  const [showAll, setShowAll] = useState(false);
  const [openSeq, setOpenSeq] = useState<number | null>(null);
  const [follow, setFollow] = useState(true);
  const [limit, setLimit] = useState(pageSize);
  // Paused (scrolled up): the first rendered line and how many lines there were at that moment.
  const [frozen, setFrozen] = useState<{ start: number; seen: number } | null>(null);
  const scroller = useRef<HTMLDivElement>(null);

  const t0 = events.length ? tsMs(events[0].ts) : 0;
  const folders = overlays.join("\n");
  const lines = useMemo(() => {
    const ctx: FeedContext = {
      notes: modelNotesByAgent(events),
      isOverlay: (path) => isOverlayPath(path, folders ? folders.split("\n") : []),
    };
    return events.map((event) => ({ event, line: describeEvent(event, ctx) }));
  }, [events, folders]);
  const visible = useMemo(() => (showAll ? lines : lines.filter((l) => !l.line.internal)), [lines, showAll]);
  const hidden = lines.length - visible.length;
  const start = feedStart(visible.length, limit, follow ? null : frozen?.start ?? null);
  const shown = visible.slice(start);
  const fresh = !follow && frozen ? Math.max(0, visible.length - frozen.seen) : 0;
  const newest = visible.length ? visible[visible.length - 1].event.seq : null;

  // Following: keep the newest line in view as lines arrive.
  useIsoLayoutEffect(() => {
    const el = scroller.current;
    if (el && follow) el.scrollTop = el.scrollHeight;
  }, [newest, follow, showAll, limit]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < FOLLOW_SLACK;
    if (atBottom && !follow) {
      setFollow(true);
      setFrozen(null);
    } else if (!atBottom && follow) {
      setFollow(false);
      setFrozen({ start, seen: visible.length });
    }
  };

  const jumpToLatest = () => {
    setFollow(true);
    setFrozen(null);
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  };

  const showEarlier = () => {
    setLimit((n) => n + pageSize);
    setFrozen((f) => (f ? { ...f, start: Math.max(0, f.start - pageSize) } : f));
  };

  const switchFilter = (all: boolean) => {
    setShowAll(all);
    setLimit(pageSize);
    setFollow(true);
    setFrozen(null);
  };

  return (
    <div className="feed">
      <div className="feed-toolbar">
        <div className="segmented small" role="radiogroup" aria-label="Which events to show">
          <button type="button" role="radio" aria-checked={!showAll} className={!showAll ? "active" : ""} onClick={() => switchFilter(false)}>
            Key events
          </button>
          <button type="button" role="radio" aria-checked={showAll} className={showAll ? "active" : ""} onClick={() => switchFilter(true)}>
            All events
          </button>
        </div>
        <span className="muted">
          {fmtInt(lines.length)} event{lines.length === 1 ? "" : "s"}
          {hidden > 0 ? ` · ${fmtInt(hidden)} routine internals hidden (context packs, agent steps, read ALLOWs)` : ""}
        </span>
        {live ? (
          <span className="live-pill">
            <span className="live-dot" aria-hidden="true" /> {follow ? "live" : "paused"}
          </span>
        ) : null}
      </div>
      <div
        className="feed-scroll lab-feed-scroll"
        ref={scroller}
        onScroll={onScroll}
        style={{ height: visible.length ? height : undefined, maxHeight: height }}
        role="log"
        aria-live="off"
        aria-label={label}
        tabIndex={0}
      >
        {start > 0 ? (
          <div className="feed-earlier">
            <button type="button" className="btn btn-ghost btn-small" onClick={showEarlier}>
              <Icon name="chevronDown" size={13} />
              Show {fmtInt(Math.min(pageSize, start))} earlier events
            </button>
            <span className="muted small">{fmtInt(start)} earlier not shown</span>
          </div>
        ) : null}
        {visible.length === 0 ? (
          <div className="feed-empty">{events.length ? "No key events yet: choose All events to see the routine ones." : "Waiting for the first trace event…"}</div>
        ) : null}
        {shown.map(({ event, line }) => {
          const offset = tsMs(event.ts) - t0;
          const open = openSeq === event.seq;
          return (
            <div key={event.seq} className={`feed-item tone-${line.tone}${open ? " open" : ""}`}>
              <button type="button" className="feed-line" aria-expanded={open} onClick={() => setOpenSeq(open ? null : event.seq)}>
                <span className="feed-time">+{(Math.max(0, Number.isFinite(offset) ? offset : 0) / 1000).toFixed(2)}s</span>
                <span className="feed-icon">
                  <Icon name={line.icon} size={14} />
                </span>
                <span className="feed-tag">{line.tag}</span>
                <span className="feed-text">
                  {line.parts.map((part, i) =>
                    typeof part === "string" ? (
                      <span key={i}>{part}</span>
                    ) : "quote" in part ? (
                      <q key={i} className="feed-quote" title={part.mixed ? MIXED_TITLE : QUOTE_TITLE}>
                        {part.quote}
                      </q>
                    ) : (
                      <code key={i} className="mono">
                        {part.code}
                      </code>
                    ),
                  )}
                  {line.chips?.map((chip, i) => (
                    <Chip key={`c${i}`} tone={chip.tone} mono>
                      {chip.label}
                    </Chip>
                  ))}
                </span>
              </button>
              {open ? (
                <div className="feed-detail">
                  <div className="feed-meta">
                    <span>
                      seq <code className="mono">{event.seq}</code>
                    </span>
                    <span>
                      category <code className="mono">{event.category}</code>
                    </span>
                    <span>
                      stage <code className="mono">{event.stage}</code>
                    </span>
                    {event.agent ? (
                      <span>
                        agent <code className="mono">{event.agent}</code>
                      </span>
                    ) : null}
                    {event.duration_ms !== null ? <span>duration {fmtMs(event.duration_ms)}</span> : null}
                    <span>{event.ts}</span>
                  </div>
                  <JsonView value={event.payload} maxHeight={280} />
                  {event.usage ? <JsonView value={event.usage} maxHeight={140} /> : null}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
      {!follow && visible.length > 0 ? (
        <button type="button" className="btn btn-small feed-jump" onClick={jumpToLatest}>
          <Icon name="chevronDown" size={14} />
          {fresh > 0 ? `${fmtInt(fresh)} new · Jump to latest` : "Jump to latest"}
        </button>
      ) : null}
    </div>
  );
}
