"use client";

import { Loader2 } from "lucide-react";

import { SourceCard } from "@/components/results/SourceCard";
import { useT } from "@/i18n";
import type { HarnessState } from "@/types/transcript";

import { HarnessTaskPlanBox } from "./HarnessTaskPlanBox";
import { ScraperSample } from "./ScraperSample";

function oneLine(s: string, max = 160): string {
  const c = s.replace(/\s+/g, " ").trim();
  return c.length > max ? `${c.slice(0, max - 1)}…` : c;
}

/**
 * Full-page view of a scraper (harness) CHILD session: the discovered source
 * card (migrated from the discovery page) on top, then run status, per-iteration
 * verdicts, the steerable Task plan, and the inline sample table. Driven by the
 * top-level `state.harness` (a child session has no source cards of its own).
 *
 * `collapsed` (set once the Run phase starts): only the source card survives —
 * the explore/validate presentation (status line, iterations, task plan,
 * sample table) gives way to the RunPanel's output below.
 */
export function ScraperPanel({
  harness: h,
  collapsed = false,
}: {
  harness: HarnessState;
  collapsed?: boolean;
}) {
  const { t } = useT();
  if (collapsed) {
    return (
      <div className="space-y-3">
        {h.source ? <SourceCard source={h.source} /> : null}
        {!h.source && h.sourceUrl ? (
          <a
            href={h.sourceUrl}
            target="_blank"
            rel="noreferrer"
            className="block break-all text-xs text-primary/80 hover:underline"
          >
            {h.sourceUrl}
          </a>
        ) : null}
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {/* The discovered source being scraped — the same card as on the
          discovery page (read-only here; no Build button). */}
      {h.source ? <SourceCard source={h.source} /> : null}

      <div className="flex items-center gap-2 text-sm">
        <span
          className={`h-2 w-2 shrink-0 rounded-full ${
            h.status === "done"
              ? "bg-emerald-500 dark:bg-emerald-400"
              : h.status === "awaiting_credentials"
                ? "bg-amber-500 dark:bg-amber-400"
                : "bg-blue-500 dark:bg-blue-400 animate-pulse"
          }`}
        />
        <span className="font-medium">{t("scraper.title")}</span>
        <span className="text-xs text-muted-foreground">
          ·{" "}
          {h.status === "done"
            ? t("scraper.done")
            : h.status === "awaiting_credentials"
              ? t("scraper.awaitingKey")
              : t("scraper.building")}
        </span>
        {h.iterations.length > 0 ? (
          <span className="text-xs text-muted-foreground">
            · {h.iterations.length} {t("scraper.iterUnit")}
          </span>
        ) : null}
      </div>

      {/* Live "working" indicator — the agent's latest turn during explore /
          validate (mirrors the discovery activity line). Cleared on done. */}
      {h.status === "running" && h.activity ? (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-blue-600 dark:text-blue-400" />
          <span className="font-medium text-foreground/80">
            {h.activity.phase === "executing" ? t("executing") : t("thinking")}…
          </span>
          <span className="shrink-0 opacity-70">
            · {h.activity.stage === "validate" ? t("scraper.validating") : t("scraper.exploring")}
          </span>
          {h.activity.text ? (
            <span className="min-w-0 truncate opacity-70">· {oneLine(h.activity.text)}</span>
          ) : null}
        </div>
      ) : null}

      {/* Fall back to a bare URL link only when the full source card is absent
          (e.g. an older child session from before this field existed). */}
      {!h.source && h.sourceUrl ? (
        <a
          href={h.sourceUrl}
          target="_blank"
          rel="noreferrer"
          className="block break-all text-xs text-primary/80 hover:underline"
        >
          {h.sourceUrl}
        </a>
      ) : null}

      {h.iterations.length > 0 ? (
        <div className="space-y-0.5 rounded-md border border-border px-3 py-2 text-[11px] text-muted-foreground">
          {/* Prefix the build only when the loop has restarted — iteration
              numbers reset per build, so "iter 1" alone would repeat.
              Computed once for the list, not per row (was O(n²)). */}
          {(() => {
            const multiBuild = h.iterations.some((x) => (x.build ?? 1) > 1);
            return h.iterations.map((it, i) => (
              <div key={i}>
                {multiBuild ? `${t("scraper.buildN")} #${it.build ?? 1} · ` : ""}
                {t("scraper.iter")} {it.n}
                {it.note ? ` — ${it.note}` : ""}
              </div>
            ));
          })()}
        </div>
      ) : null}

      {h.taskPlan ? <HarnessTaskPlanBox plan={h.taskPlan} /> : null}
      {h.sampleArtifact ? <ScraperSample artifact={h.sampleArtifact} /> : null}
    </div>
  );
}
