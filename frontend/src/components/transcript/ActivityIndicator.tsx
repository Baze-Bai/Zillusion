"use client";

import { Loader2 } from "lucide-react";

import { useT } from "@/i18n";
import { useConversation } from "@/state/ConversationContext";

function oneLine(s: string, max = 160): string {
  const c = s.replace(/\s+/g, " ").trim();
  return c.length > max ? `${c.slice(0, max - 1)}…` : c;
}

/**
 * The single ephemeral "working" indicator (Claude-Code style). Reflects the
 * latest discovery activity — Thinking (agent narration) or Executing (a
 * pipeline stage / source commit) — and disappears the moment the run reaches a
 * terminal state (the reducer nulls `activity` on done/error). Rendered OUTSIDE
 * `state.order`, so it never persists in the transcript.
 */
export function ActivityIndicator() {
  const { state } = useConversation();
  const { t } = useT();
  const a = state.activity;
  if (!a) return null;

  const label = a.phase === "executing" ? t("executing") : t("thinking");
  const detail = a.isStage ? t(`stage.${a.text}`) : oneLine(a.text);

  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-blue-600 dark:text-blue-400" />
      <span className="font-medium text-foreground/80">{label}…</span>
      {detail ? <span className="min-w-0 truncate opacity-70">· {detail}</span> : null}
    </div>
  );
}
