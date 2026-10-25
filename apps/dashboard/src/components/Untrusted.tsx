import type { ReactNode } from "react";

/**
 * Untrusted text is anything a model or a tool wrote: claims, summaries, search queries, log
 * lines, runbook text from an overlay, observation content. It is ALWAYS rendered as React
 * text (escaped; the dashboard never injects raw HTML, which tests/logview.test.tsx checks),
 * marked as untrusted, and never used as a link target unless safeHref accepts it.
 */

export function UntrustedText({
  text,
  author,
  quote = true,
  className,
}: {
  text: string;
  /** Who wrote it, e.g. "the telemetry agent's model" (shown on hover). */
  author?: string;
  /** Wrap in quotation marks (<q>); false for a plain span. */
  quote?: boolean;
  className?: string;
}) {
  const title = `Untrusted text${author ? ` written by ${author}` : ""}: shown as data, never followed or rendered as HTML`;
  const classes = className ? `untrusted ${className}` : "untrusted";
  return quote ? (
    <q className={classes} title={title}>
      {text}
    </q>
  ) : (
    <span className={classes} title={title}>
      {text}
    </span>
  );
}

const SAFE_PROTOCOLS: ReadonlySet<string> = new Set(["http:", "https:"]);

/**
 * `href` if a browser would open it as an http(s) URL or an in-app link (relative path,
 * #hash), else null. Parsed like a browser parses an href, so tricks such as
 * " javascript:", "java\tscript:", "JAVASCRIPT:" or "data:" are refused.
 */
export function safeHref(href: unknown): string | null {
  if (typeof href !== "string") return null;
  const trimmed = href.trim();
  if (!trimmed) return null;
  let url: URL;
  try {
    url = new URL(trimmed, "https://recap.invalid/");
  } catch {
    return null;
  }
  return SAFE_PROTOCOLS.has(url.protocol) ? trimmed : null;
}

/** A link only when `href` is safe (see safeHref); otherwise its text alone. */
export function SafeLink({ href, children }: { href: unknown; children: ReactNode }) {
  const safe = safeHref(href);
  if (!safe) {
    return (
      <span className="unsafe-link" title="Not a link: only http(s) and in-app addresses are opened">
        {children}
      </span>
    );
  }
  const external = /^(https?:)?\/\//i.test(safe);
  return external ? (
    <a href={safe} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ) : (
    <a href={safe}>{children}</a>
  );
}
