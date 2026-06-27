"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";

import { Markdown } from "@/components/common/Markdown";
import { useT } from "@/i18n";
import type { DataSource } from "@/types/data-source";

const TYPE_BADGES: Record<string, { label: string; color: string }> = {
  api: { label: "API", color: "bg-blue-100 text-blue-800 dark:bg-blue-500/15 dark:text-blue-300" },
  file: { label: "File", color: "bg-green-100 text-green-800 dark:bg-green-500/15 dark:text-green-300" },
  embedded: { label: "Embedded", color: "bg-purple-100 text-purple-800 dark:bg-purple-500/15 dark:text-purple-300" },
};

interface SourceCardProps {
  source: Partial<DataSource>;
  rank?: number;
}

export function SourceCard({ source, rank }: SourceCardProps) {
  const badge = TYPE_BADGES[source.source_type || "api"];
  const scores = source.scores;
  const [guideOpen, setGuideOpen] = useState(false);
  const { t } = useT();

  return (
    <div className="border border-border rounded-lg p-4 hover:border-primary/30 transition-colors bg-card">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            {rank !== undefined && (
              <span className="text-xs font-bold text-muted-foreground">#{rank}</span>
            )}
            <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${badge?.color}`}>
              {t(`badge.${source.source_type || "api"}`)}
            </span>
            {source.access_level === "open" && (
              <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300">
                {t("access.open")}
              </span>
            )}
          </div>
          <h4 className="font-semibold text-sm truncate">{source.name}</h4>
          <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
            {source.description}
          </p>
          <a
            href={source.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-primary hover:underline mt-1 block truncate"
          >
            {source.url}
          </a>

          {source.tags && source.tags.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-2">
              {source.tags.slice(0, 5).map((tag) => (
                <span
                  key={tag}
                  className="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground"
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Score indicator */}
        {scores && (
          <div className="flex-shrink-0 text-center">
            <div
              className={`w-12 h-12 rounded-full flex items-center justify-center text-sm font-bold
                ${scores.overall >= 8 ? "bg-green-100 text-green-800 dark:bg-green-500/15 dark:text-green-300" : ""}
                ${scores.overall >= 6 && scores.overall < 8 ? "bg-yellow-100 text-yellow-800 dark:bg-yellow-500/15 dark:text-yellow-300" : ""}
                ${scores.overall < 6 ? "bg-red-100 text-red-800 dark:bg-red-500/15 dark:text-red-300" : ""}`}
            >
              {scores.overall.toFixed(1)}
            </div>
            <span className="text-[10px] text-muted-foreground">{t("card.score")}</span>
          </div>
        )}
      </div>

      {/* Score breakdown */}
      {scores && (
        <div className="mt-3 pt-3 border-t border-border">
          <div className="grid grid-cols-5 gap-2 text-center">
            {[
              { label: t("dim.relevance"), value: scores.relevance, weight: "40%" },
              { label: t("dim.authority"), value: scores.authority, weight: "20%" },
              { label: t("dim.freshness"), value: scores.freshness, weight: "15%" },
              { label: t("dim.access"), value: scores.accessibility, weight: "15%" },
              { label: t("dim.license"), value: scores.license_fit, weight: "10%" },
            ].map(({ label, value, weight }) => (
              <div key={label}>
                <div className="text-xs font-medium">{value.toFixed(1)}</div>
                <div className="w-full bg-muted rounded-full h-1 mt-0.5">
                  <div
                    className="bg-primary rounded-full h-1"
                    style={{ width: `${value * 10}%` }}
                  />
                </div>
                <div className="text-[9px] text-muted-foreground mt-0.5">
                  {label} ({weight})
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Usage guide (point 6) — folded into the card, collapsed by default */}
      {source.usage_guide ? (
        <div className="mt-3 border-t border-border pt-3">
          <button
            onClick={() => setGuideOpen((o) => !o)}
            className="inline-flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground"
          >
            {guideOpen ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
            {t("card.usageGuide")}
          </button>
          {guideOpen ? (
            <div className="mt-2">
              <Markdown>{source.usage_guide}</Markdown>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
