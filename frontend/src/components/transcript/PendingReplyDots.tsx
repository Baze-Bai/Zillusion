"use client";

/**
 * Three softly bouncing dots in an agent-side bubble — the conversation's
 * typing indicator. Shown after the user's latest message until the agent's
 * reply bubble lands (or the run reaches a terminal state). Purely
 * presentational; the showing/hiding logic lives in Transcript.
 */
export function PendingReplyDots() {
  return (
    <div className="flex justify-start" aria-hidden>
      <div className="flex items-center gap-1.5 rounded-2xl bg-muted px-4 py-3">
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:-0.3s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:-0.15s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground/60" />
      </div>
    </div>
  );
}
