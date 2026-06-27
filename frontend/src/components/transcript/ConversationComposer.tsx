"use client";

import { useState } from "react";

import { Composer } from "@/components/transcript/Composer";
import { useConversation } from "@/state/ConversationContext";

/**
 * Web-Claude-style chat input pinned at the bottom of a conversation. Sends a
 * free-form message to the agent through the shared steering channel (a scraper
 * child session → its harness site; a discovery session → a "note"). Replaces
 * the old inline "Guide" affordance that used to live on the task-plan box.
 *
 * While the run is LIVE the message steers the agent on its next turn; once the
 * run is terminal, sending re-runs it with the message as feedback (the backend
 * decides — see ConversationContext.sendMessage).
 */
export function ConversationComposer() {
  const { state, sendMessage } = useConversation();
  const [error, setError] = useState<string | null>(null);

  const ready =
    state.status === "live" || state.status === "done" || state.status === "error";

  const onSubmit = async (value: string) => {
    setError(null);
    try {
      await sendMessage(value);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="shrink-0 border-t border-border px-4 py-3">
      <div className="mx-auto max-w-3xl">
        {error ? <div className="mb-1 text-xs text-red-600 dark:text-red-400">{error}</div> : null}
        <Composer onSubmit={onSubmit} disabled={!ready} minChars={1} />
      </div>
    </div>
  );
}
