"use client";

import { Hammer } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { SourceCard } from "@/components/results/SourceCard";
import { useT } from "@/i18n";
import { useConversation } from "@/state/ConversationContext";
import { useSessionList } from "@/state/SessionListContext";
import type { SourceBlock } from "@/types/transcript";

/**
 * Per-source card with a "Build scraper" button. Clicking it spins up a
 * dedicated scraper CHILD session (nested under this discovery session) and
 * navigates to it. Once a scraper already exists for this source (a child
 * session with matching source_id), the Build button is replaced by a link to
 * that scraper — we don't re-offer building a source that's already built.
 */
export function SourceBox({ block }: { block: SourceBlock }) {
  const { state, startHarness } = useConversation();
  const { sessions } = useSessionList();
  const { t } = useT();
  const router = useRouter();
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const sourceId = block.source.id;
  // A scraper child session already built from this source (under this run).
  const built = sourceId
    ? sessions.find((s) => s.parent_query_id === state.sessionId && s.source_id === sourceId)
    : undefined;

  const onBuild = async () => {
    if (!sourceId) return;
    setStarting(true);
    setErr(null);
    try {
      const ids = await startHarness([sourceId]);
      if (ids && ids[0]) router.push(`/c/${ids[0]}`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="space-y-2">
      <SourceCard source={block.source} />
      {sourceId ? (
        <div className="ml-3 flex items-center gap-2">
          {built ? (
            <Link
              href={`/c/${built.query_id}`}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent"
            >
              <Hammer className="h-3.5 w-3.5" />
              {t("scraper.view")}
            </Link>
          ) : (
            <button
              onClick={onBuild}
              disabled={starting}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs transition-colors hover:bg-accent disabled:opacity-50"
            >
              <Hammer className="h-3.5 w-3.5" />
              {starting ? t("header.starting") : t("scraper.build")}
            </button>
          )}
          {err ? <span className="text-xs text-red-600 dark:text-red-400">{err}</span> : null}
        </div>
      ) : null}
    </div>
  );
}
