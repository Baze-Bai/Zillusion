"use client";

import { ArtifactRenderer } from "@/components/artifact/ArtifactRenderer";
import { useArtifact } from "@/hooks/useArtifact";
import { useT } from "@/i18n";
import type { ArtifactRef } from "@/types/transcript";

/**
 * The scraper's sample output, rendered INLINE as a table (point 8). Reuses
 * ArtifactRenderer, which turns a JSON array / CSV into a TableView. Lazily
 * fetched when the (open) scraper sub-session mounts it.
 */
export function ScraperSample({ artifact }: { artifact: ArtifactRef }) {
  const { data, loading, error } = useArtifact(artifact);
  const { t } = useT();
  return (
    <div>
      <div className="mb-1 text-[11px] text-muted-foreground">{t("scraper.sampleData")}</div>
      {loading ? <div className="text-xs text-muted-foreground">{t("common.loading")}</div> : null}
      {error ? <div className="text-xs text-red-600 dark:text-red-400">{error}</div> : null}
      {data ? (
        <div className="overflow-auto rounded-md border border-border p-2">
          <ArtifactRenderer artifact={data} />
        </div>
      ) : null}
    </div>
  );
}
