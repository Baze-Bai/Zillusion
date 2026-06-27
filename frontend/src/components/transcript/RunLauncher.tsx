"use client";

import { Play } from "lucide-react";
import { useState } from "react";

import { useT } from "@/i18n";
import { useConversation } from "@/state/ConversationContext";

/**
 * "Run" launcher, shown ABOVE the conversation divider once a scraper child's
 * explore→validate loop has finished and the Run phase hasn't started yet.
 * Hidden while the loop is still running or was restarted, because the reducer
 * clears `finalVerdict` + `run` on every `harness_site_started`.
 *
 * What renders depends on the loop's final verdict:
 *   - PASS          → the primary "Run full crawl" button.
 *   - INCONCLUSIVE  → the gate note (verdict + exit reason) and a secondary
 *                     "run anyway (at your own risk)" override button.
 *   - FAIL / none   → the gate note only; the Run phase stays locked.
 * The note exists so a non-PASS ending explains itself instead of silently
 * rendering nothing where the button would be.
 */
export function RunLauncher() {
  const { state, startRun } = useConversation();
  const { t } = useT();
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const h = state.harness;
  if (!h || h.status !== "done" || h.run) return null;
  const verdict = h.finalVerdict;

  const onClick = async () => {
    setStarting(true);
    setError(null);
    try {
      await startRun();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  };

  if (verdict === "PASS") {
    return (
      <div className="flex flex-col items-center gap-1 pt-2">
        <button
          onClick={onClick}
          disabled={starting}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-accent/40 px-4 py-2 text-sm font-medium transition-colors hover:bg-accent disabled:opacity-50"
        >
          <Play className="h-4 w-4" />
          {starting ? t("run.starting") : t("run.start")}
        </button>
        <span className="text-[11px] text-muted-foreground">{t("run.hint")}</span>
        {error ? <span className="text-[11px] text-red-600 dark:text-red-400">{error}</span> : null}
      </div>
    );
  }

  const noteKey =
    verdict === "INCONCLUSIVE"
      ? "run.gate.inconclusive"
      : verdict === "FAIL"
        ? "run.gate.fail"
        : "run.gate.noVerdict";
  const tone =
    verdict === "FAIL" ? "text-red-600 dark:text-red-400" : "text-amber-700 dark:text-amber-400";

  return (
    <div className="flex flex-col items-center gap-1 pt-2 text-center">
      <span className={`text-xs ${tone}`}>
        {t(noteKey)}
        {h.exitReason ? <span className="opacity-75"> · {h.exitReason}</span> : null}
      </span>
      <span className="text-[11px] text-muted-foreground">{t("run.gate.steerHint")}</span>
      {verdict === "INCONCLUSIVE" ? (
        <button
          onClick={onClick}
          disabled={starting}
          className="mt-1 inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
        >
          <Play className="h-3.5 w-3.5" />
          {starting ? t("run.starting") : t("run.force")}
        </button>
      ) : null}
      {error ? <span className="text-[11px] text-red-600 dark:text-red-400">{error}</span> : null}
    </div>
  );
}
