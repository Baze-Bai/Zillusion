"use client";

import { Download, Loader2, RefreshCw } from "lucide-react";
import { memo, useEffect, useRef, useState } from "react";

import { useT } from "@/i18n";
import { artifactDownloadUrl } from "@/lib/api";
import { useConversationActions } from "@/state/ConversationContext";
import type { RunState } from "@/types/transcript";

function fmtElapsed(s?: number): string {
  if (!s || s < 0) return "0s";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Ticking elapsed clock. `run_progress` events are deduped server-side on
 * (state, lines, records) — a quiet crawl publishes nothing for minutes, so the
 * displayed elapsed must advance locally. Anchored on the latest server
 * elapsed_s: against the payload's server timestamp (`atS`, replay-accurate)
 * when present, otherwise against its arrival time. Every tick recomputes from
 * Date.now(), so background-tab interval throttling can't accumulate drift; on
 * a terminal state the display freezes to the server's final value.
 */
function useTickingElapsed(
  progress: RunState["progress"],
  running: boolean,
  runId?: string,
): number | undefined {
  const [, setTick] = useState(0);
  // Arrival-time anchor — fallback for events without a server timestamp.
  const anchorRef = useRef<{ baseS: number; atMs: number } | null>(null);

  // A new run starts from a clean anchor.
  useEffect(() => {
    anchorRef.current = null;
  }, [runId]);

  useEffect(() => {
    if (typeof progress?.elapsedS === "number") {
      anchorRef.current = { baseS: progress.elapsedS, atMs: Date.now() };
    }
  }, [progress?.elapsedS]);

  useEffect(() => {
    if (!running) return;
    // No progress yet (the first snapshot lags ~3s behind run_started): tick
    // from 0 immediately; the first real value re-anchors.
    if (!anchorRef.current) anchorRef.current = { baseS: 0, atMs: Date.now() };
    const id = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [running]);

  if (!running) return progress?.elapsedS;
  if (typeof progress?.elapsedS === "number" && typeof progress?.atS === "number") {
    return progress.elapsedS + Math.max(0, Date.now() / 1000 - progress.atS);
  }
  const a = anchorRef.current;
  return a ? a.baseS + (Date.now() - a.atMs) / 1000 : progress?.elapsedS;
}

const OUTCOME_CLS: Record<string, string> = {
  COMPLETE: "text-emerald-600 dark:text-emerald-400",
  PARTIAL: "text-amber-600 dark:text-amber-400",
  FAILED: "text-red-600 dark:text-red-400",
  ABORTED: "text-red-600 dark:text-red-400",
};

/**
 * The Run (production crawl) phase view: a live progress ticker for the
 * VALIDATED workflow.py executing at full scope. Rendered below the ScraperPanel
 * (which already shows the source card on top) on the same scraper child page —
 * so the run "界面" is: source card up top, live workflow.py progress below.
 * Driven by `state.harness.run`; populated once the user clicks "Run".
 */
// memo: the panel re-rendered on EVERY conversation state change (each SSE
// event recreates the context value) while its own 1s elapsed tick already
// drives the updates it needs. With memo it re-renders only when the `run`
// object itself changes (progress/status updates) or its own tick fires.
export const RunPanel = memo(function RunPanel({ run }: { run: RunState }) {
  const { t } = useT();
  // Actions-only: with the merged context, this subscription re-rendered the
  // panel on every SSE event and pierced the React.memo above.
  const { startRun } = useConversationActions();
  const [restarting, setRestarting] = useState(false);
  const [rerunError, setRerunError] = useState<string | null>(null);
  const p = run.progress;
  const running = run.status === "running";
  const records = p?.recordCount ?? run.recordCount;
  const elapsedS = useTickingElapsed(p, running, run.runId);

  // Re-run: a terminal run can be repeated (the PASSed workflow is unchanged —
  // data refresh). Same startRun() as the launcher; the new run_started event
  // resets this panel to a fresh running state, and each run keeps its own
  // runs/<run_id>/ on disk.
  const onRerun = async () => {
    setRestarting(true);
    setRerunError(null);
    try {
      await startRun();
    } catch (e) {
      setRerunError(e instanceof Error ? e.message : String(e));
    } finally {
      setRestarting(false);
    }
  };

  return (
    <div className="space-y-2 rounded-lg border border-border p-3">
      <div className="flex items-center gap-2 text-sm">
        <span
          className={`h-2 w-2 shrink-0 rounded-full ${
            running
              ? "bg-blue-500 dark:bg-blue-400 animate-pulse"
              : run.status === "error"
                ? "bg-red-500 dark:bg-red-400"
                : "bg-emerald-500 dark:bg-emerald-400"
          }`}
        />
        <span className="font-medium">{t("run.title")}</span>
        <span className="text-xs text-muted-foreground">
          · {running ? t("run.running") : run.status === "error" ? t("status.error") : t("run.done")}
        </span>
        {run.outcome ? (
          <span className={`text-xs font-medium ${OUTCOME_CLS[run.outcome] ?? "text-muted-foreground"}`}>
            · {t(`run.outcome.${run.outcome}`)}
          </span>
        ) : null}
        {!running ? (
          <button
            onClick={onRerun}
            disabled={restarting}
            className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs transition-colors hover:bg-accent disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${restarting ? "animate-spin" : ""}`} />
            {restarting ? t("run.starting") : t("run.rerun")}
          </button>
        ) : null}
      </div>
      {rerunError ? <div className="text-[11px] text-red-600 dark:text-red-400">{rerunError}</div> : null}

      {/* Live metrics — records crawled + elapsed + crawl state, from
          runs/<run_id>/_run_progress.json relayed every ~3s. */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {typeof records === "number" ? (
          <span>
            <span className="font-medium text-foreground/80">{records}</span> {t("run.records")}
          </span>
        ) : null}
        {elapsedS != null ? (
          <span>
            {t("run.elapsed")} {fmtElapsed(elapsedS)}
          </span>
        ) : null}
        {typeof p?.linesEmitted === "number" ? (
          <span>
            {p.linesEmitted} {t("run.lines")}
          </span>
        ) : null}
        {p?.state ? <span>· {p.state}</span> : null}
      </div>

      {/* Live agent narration while crawling (mirrors the explore/validate line). */}
      {running && run.activity ? (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-blue-600 dark:text-blue-400" />
          <span className="font-medium text-foreground/80">
            {run.activity.phase === "executing" ? t("executing") : t("thinking")}…
          </span>
          {run.activity.text ? (
            <span className="min-w-0 truncate opacity-70">· {run.activity.text}</span>
          ) : null}
        </div>
      ) : null}

      {/* Terminal one-line reason / machinery error. */}
      {!running && run.reason ? (
        <div className="text-xs text-muted-foreground">{run.reason}</div>
      ) : null}
      {run.status === "error" && run.error ? (
        <div className="break-all text-xs text-red-600 dark:text-red-400">{run.error}</div>
      ) : null}

      {/* Downloads: the kept dataset (registered at run end; also on the error
          path when the crawl produced data) + the field-doc snapshot
          (task_plan.md as of run start — available while still crawling). */}
      {run.outputArtifact || run.fieldDocArtifact ? (
        <div className="flex flex-wrap items-center gap-2">
          {run.outputArtifact ? (
            <a
              href={artifactDownloadUrl(run.outputArtifact.sessionId, run.outputArtifact.artifactId)}
              download={run.outputArtifact.filename}
              className="inline-flex w-fit items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium transition-colors hover:bg-accent"
            >
              <Download className="h-3.5 w-3.5" />
              {t("run.download")}
              <span className="font-normal text-muted-foreground">
                · {run.outputArtifact.filename}
                {run.outputBytes != null ? ` · ${fmtBytes(run.outputBytes)}` : ""}
              </span>
            </a>
          ) : null}
          {run.fieldDocArtifact ? (
            <a
              href={artifactDownloadUrl(run.fieldDocArtifact.sessionId, run.fieldDocArtifact.artifactId)}
              download={run.fieldDocArtifact.filename}
              className="inline-flex w-fit items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium transition-colors hover:bg-accent"
            >
              <Download className="h-3.5 w-3.5" />
              {t("run.downloadFields")}
              <span className="font-normal text-muted-foreground">· {run.fieldDocArtifact.filename}</span>
            </a>
          ) : null}
        </div>
      ) : null}
    </div>
  );
});
