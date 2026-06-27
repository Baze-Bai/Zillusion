"use client";

import type { ArtifactContent } from "@/lib/api";

import { JsonView } from "./JsonView";
import { MarkdownView } from "./MarkdownView";
import { TableView } from "./TableView";

function splitCsvLine(line: string): string[] {
  const out: string[] = [];
  let cur = "";
  let q = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (q) {
      if (ch === '"') {
        if (line[i + 1] === '"') {
          cur += '"';
          i++;
        } else {
          q = false;
        }
      } else {
        cur += ch;
      }
    } else if (ch === '"') {
      q = true;
    } else if (ch === ",") {
      out.push(cur);
      cur = "";
    } else {
      cur += ch;
    }
  }
  out.push(cur);
  return out;
}

function parseCsv(text: string): Record<string, unknown>[] {
  const lines = text.trim().split(/\r?\n/);
  if (lines.length < 1) return [];
  const headers = splitCsvLine(lines[0]);
  return lines.slice(1).map((l) => {
    const cells = splitCsvLine(l);
    const o: Record<string, unknown> = {};
    headers.forEach((h, i) => {
      o[h] = cells[i] ?? "";
    });
    return o;
  });
}

function isScalar(v: unknown): boolean {
  // null counts as a scalar cell; objects/arrays/functions do not.
  return v === null || (typeof v !== "object" && typeof v !== "function");
}

/** A "flat" row = a plain object whose values are all scalars (2D data). A
 * nested object/array value means the data is 3D+ → not table-able. */
function isFlatRow(o: unknown): o is Record<string, unknown> {
  return (
    !!o &&
    typeof o === "object" &&
    !Array.isArray(o) &&
    Object.values(o as Record<string, unknown>).every(isScalar)
  );
}

function isFlatRowArray(v: unknown): v is Record<string, unknown>[] {
  return Array.isArray(v) && v.length > 0 && v.every(isFlatRow);
}

/** Find a 2D table to render: the value itself if it's a flat-row array, else
 * the first flat-row array nested directly under a wrapper object (e.g.
 * {site_id, record_count, records:[...]}). Returns null for 3D/4D-nested or
 * non-tabular data → caller falls back to JSON. */
function findTableData(
  parsed: unknown,
): { rows: Record<string, unknown>[]; meta?: [string, unknown][] } | null {
  if (isFlatRowArray(parsed)) return { rows: parsed };
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const obj = parsed as Record<string, unknown>;
    const entry = Object.entries(obj).find(([, v]) => isFlatRowArray(v));
    if (entry) {
      const meta = Object.entries(obj).filter(([, v]) => isScalar(v));
      return { rows: entry[1] as Record<string, unknown>[], meta: meta.length ? meta : undefined };
    }
  }
  return null;
}

export function ArtifactRenderer({ artifact }: { artifact: ArtifactContent }) {
  if (artifact.content_type === "markdown") return <MarkdownView content={artifact.content} />;
  if (artifact.content_type === "csv") return <TableView rows={parseCsv(artifact.content)} />;
  if (artifact.content_type === "json") {
    try {
      const parsed = JSON.parse(artifact.content);
      const table = findTableData(parsed);
      if (table) {
        // 2D → table. Show any wrapper scalar fields (site_id, record_count, …)
        // as a small caption above it.
        return (
          <div className="space-y-2">
            {table.meta ? (
              <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-muted-foreground">
                {table.meta.map(([k, v]) => (
                  <span key={k}>
                    <span className="font-medium">{k}</span>: {v === null ? "null" : String(v)}
                  </span>
                ))}
              </div>
            ) : null}
            <TableView rows={table.rows} />
          </div>
        );
      }
    } catch {
      /* fall through to JsonView */
    }
    // 3D/4D-nested or non-tabular → raw JSON.
    return <JsonView content={artifact.content} />;
  }
  return (
    <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed">
      {artifact.content}
    </pre>
  );
}
