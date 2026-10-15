import { useMemo } from "react";

interface Token {
  text: string;
  cls?: string;
}

// Strings (optionally followed by ':' = an object key), literals and numbers.
const TOKEN_RE = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;

function tokenize(text: string): Token[] {
  const tokens: Token[] = [];
  let last = 0;
  for (const match of text.matchAll(TOKEN_RE)) {
    const index = match.index ?? 0;
    if (index > last) tokens.push({ text: text.slice(last, index) });
    if (match[1] !== undefined) {
      tokens.push({ text: match[1], cls: match[2] ? "json-key" : "json-str" });
      if (match[2]) tokens.push({ text: match[2] });
    } else if (match[3] !== undefined) {
      tokens.push({ text: match[0], cls: "json-lit" });
    } else {
      tokens.push({ text: match[0], cls: "json-num" });
    }
    last = index + match[0].length;
  }
  if (last < text.length) tokens.push({ text: text.slice(last) });
  return tokens;
}

function stringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

/** Pretty-printed, lightly highlighted JSON. Text is rendered by React (escaped). */
export function JsonView({ value, maxHeight = 320 }: { value: unknown; maxHeight?: number }) {
  const tokens = useMemo(() => tokenize(stringify(value)), [value]);
  return (
    <pre className="json" style={{ maxHeight }}>
      {tokens.map((t, i) =>
        t.cls ? (
          <span key={i} className={t.cls}>
            {t.text}
          </span>
        ) : (
          t.text
        ),
      )}
    </pre>
  );
}
