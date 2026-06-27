"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";

import { useT } from "@/i18n";
import { useConversation } from "@/state/ConversationContext";

/**
 * Docs-style prev/next pager between a scraper child's two phase PAGES:
 * the harness page (explore/validate — source card, task plan, iterations,
 * sample) and the Run page (production crawl progress + downloads). Rendered
 * only once a Run exists; before that there is just the harness page. The
 * composer routes the user's prompt to the agent of the ACTIVE page.
 */
export function PhasePager() {
  const { state, setView } = useConversation();
  const { t } = useT();
  if (!state.harness?.run) return null;
  const view = state.activeView;

  return (
    <div className="flex items-center justify-between border-b border-border pb-3">
      {view === "run" ? (
        <button
          onClick={() => setView("harness")}
          className="inline-flex items-center gap-1.5 text-sm font-semibold text-foreground/90 transition-colors hover:text-foreground"
        >
          <ChevronLeft className="h-4 w-4 text-muted-foreground" />
          {t("scraper.title")}
        </button>
      ) : (
        <span aria-hidden />
      )}
      {view === "harness" ? (
        <button
          onClick={() => setView("run")}
          className="inline-flex items-center gap-1.5 text-sm font-semibold text-foreground/90 transition-colors hover:text-foreground"
        >
          {t("run.title")}
          <ChevronRight className="h-4 w-4 text-muted-foreground" />
        </button>
      ) : (
        <span aria-hidden />
      )}
    </div>
  );
}
