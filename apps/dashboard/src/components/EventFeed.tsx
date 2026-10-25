import { useEffect, useMemo, useRef, useState } from "react";
import { describeEvent, fmtMs, modelNotesByAgent, tsMs } from "../format";
import type { FeedContext } from "../format";
import type { TraceEvent } from "../types";
import "../styles/live-m3.css";
import { isOverlayPath } from "../view/retrieval";
import { Chip } from "./Badge";
import { Icon } from "./Icon";
import { JsonView } from "./JsonView";

const QUOTE_TITLE = "Written by the agent's decision provider: untrusted text, shown as data and never followed";
const MIXED_TITLE =
  "Written by the agent's decision provider, and it may also hold a note RECAP added that the feed cannot tell apart (a run before M3, or notes its trace does not confirm): untrusted text, shown as data and never followed";

/**
 * The live trace as one readable line per event, oldest first. While the user is
 * scrolled to the bottom the feed follows new events; scrolling up pauses that.
 * Click a line to see the raw event (the exact JSON the backend recorded). Text an agent's
 * provider wrote (a search query, a summary, a claim) is quoted: untrusted data, never markup;
 * the notes RECAP adds to a summary are told apart from it with the agents' model-request notes.
 * `overlays`: the run's extra knowledge folders, when known (red-team sections are marked).
 */
export function EventFeed({
  events,
  live = false,
  height = 480,
  overlays = [],
}: {
  events: TraceEvent[];
  live?: boolean;
  height?: number;
  overlays?: string[];
}) {
  const [showAll, setShowAll] = useState(false);
  const [openSeq, setOpenSeq] = useState<number | null>(null);
  const [follow, setFollow] = useState(true);
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
  const visible = showAll ? lines : lines.filter((l) => !l.line.internal);
  const hidden = lines.length - visible.length;

  useEffect(() => {
    const el = scroller.current;
    if (el && follow) el.scrollTop = el.scrollHeight;
  }, [visible.length, follow]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 48);
  };

  return (
    <div className="feed">
      <div className="feed-toolbar">
        <div className="segmented small" role="radiogroup" aria-label="Which events to show">
          <button type="button" role="radio" aria-checked={!showAll} className={!showAll ? "active" : ""} onClick={() => setShowAll(false)}>
            Key events
          </button>
          <button type="button" role="radio" aria-checked={showAll} className={showAll ? "active" : ""} onClick={() => setShowAll(true)}>
            All events
          </button>
        </div>
        <span className="muted">
          {lines.length} events{hidden > 0 ? ` · ${hidden} routine internals hidden (context packs, agent steps, read ALLOWs)` : ""}
        </span>
        {live ? (
          <span className="live-pill">
            <span className="live-dot" aria-hidden="true" /> live
          </span>
        ) : null}
      </div>
      <div className="feed-scroll" ref={scroller} onScroll={onScroll} style={{ maxHeight: height }} aria-live="polite">
        {visible.length === 0 ? <div className="feed-empty">Waiting for the first trace event…</div> : null}
        {visible.map(({ event, line }) => {
          const offset = tsMs(event.ts) - t0;
          const open = openSeq === event.seq;
          return (
            <div key={event.seq} className={`feed-item tone-${line.tone}${open ? " open" : ""}`}>
              <button
                type="button"
                className="feed-line"
                aria-expanded={open}
                onClick={() => setOpenSeq(open ? null : event.seq)}
              >
                <span className="feed-time">+{(Math.max(0, offset) / 1000).toFixed(2)}s</span>
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
        <button
          type="button"
          className="btn btn-small feed-jump"
          onClick={() => {
            setFollow(true);
            const el = scroller.current;
            if (el) el.scrollTop = el.scrollHeight;
          }}
        >
          <Icon name="chevronDown" size={14} /> Jump to latest
        </button>
      ) : null}
    </div>
  );
}
