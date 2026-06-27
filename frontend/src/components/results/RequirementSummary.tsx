"use client";

import { Markdown } from "@/components/common/Markdown";
import { useT } from "@/i18n";
import type { StructuredRequirement } from "@/types/data-source";

function joinList(v?: string[] | null): string | null {
  if (!v || v.length === 0) return null;
  const items = v.filter(Boolean);
  return items.length ? items.join(", ") : null;
}

/**
 * The parsed requirement, rendered as Markdown at the top of the results once
 * the run is finalized (point 5). Only non-empty fields are shown.
 */
export function RequirementSummary({ requirement: r }: { requirement: StructuredRequirement }) {
  const { t } = useT();
  const domain = [r.domain, ...(r.sub_domains ?? [])].filter(Boolean).join(" / ") || null;
  const rows: Array<[string, string | null]> = [
    [t("req.domain"), domain],
    [t("req.formats"), joinList(r.desired_formats)],
    [t("req.geography"), joinList(r.geographic_scope)],
    [t("req.timeRange"), r.temporal_range ?? null],
    [t("req.updateFreq"), r.update_frequency ?? null],
    [t("req.license"), r.license_constraint && r.license_constraint !== "any" ? r.license_constraint : null],
    [t("req.budget"), r.budget_constraint && r.budget_constraint !== "any" ? r.budget_constraint : null],
  ];
  const lines = rows.filter(([, v]) => v).map(([k, v]) => `- **${k}:** ${v}`);
  const subq = (r.sub_questions ?? []).filter(Boolean);

  const md =
    `### ${r.title || r.original_query}\n\n` +
    (lines.length ? `${lines.join("\n")}\n` : "") +
    (subq.length ? `\n**${t("req.subQuestions")}**\n\n${subq.map((q) => `- ${q}`).join("\n")}\n` : "");

  return (
    <div className="rounded-lg border border-border bg-card/60 p-4">
      <Markdown>{md}</Markdown>
    </div>
  );
}
