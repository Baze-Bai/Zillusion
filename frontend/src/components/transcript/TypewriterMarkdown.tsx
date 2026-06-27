"use client";

import { useEffect, useState } from "react";

import { Markdown } from "@/components/common/Markdown";

// How fresh an agent message must be (event creation → mount) to get the
// typewriter treatment. Replayed history is older than this and renders whole.
const FRESH_WINDOW_MS = 15_000;
// Reveal cadence: ~3 chars / 30ms ≈ 100 chars/s — fast enough not to lag the
// conversation, slow enough to read as streaming.
const CHARS_PER_TICK = 3;
const TICK_MS = 30;

/**
 * Agent-bubble body: a FRESH live message "streams" in with a typewriter
 * reveal; replayed history renders instantly. Presentation only — the text
 * arrives whole (send_user_message is an atomic tool call), so this fakes the
 * stream client-side. Slicing mid-markdown briefly renders unbalanced markup,
 * the same artifact real token-streaming chat UIs show.
 */
export function TypewriterMarkdown({
  markdown,
  createdAt,
}: {
  markdown: string;
  createdAt?: string | null;
}) {
  // Decide ONCE at mount whether this bubble animates — re-renders (e.g. the
  // seq-dedup replace after a stream reconnect) must not restart the reveal.
  const [animate] = useState(() => {
    if (!createdAt) return false;
    const t = Date.parse(createdAt);
    return Number.isFinite(t) && Date.now() - t < FRESH_WINDOW_MS;
  });
  const [shown, setShown] = useState(() => (animate ? 0 : markdown.length));
  const done = shown >= markdown.length;

  useEffect(() => {
    if (!animate || done) return;
    const id = setInterval(
      () => setShown((n) => Math.min(n + CHARS_PER_TICK, markdown.length)),
      TICK_MS,
    );
    return () => clearInterval(id);
  }, [animate, done, markdown.length]);

  return <Markdown>{done ? markdown : markdown.slice(0, shown)}</Markdown>;
}
