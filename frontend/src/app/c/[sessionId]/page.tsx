"use client";

import { Hammer } from "lucide-react";
import { useState } from "react";

import { LanguageToggle } from "@/components/layout/LanguageToggle";
import { ThemeToggle } from "@/components/layout/ThemeToggle";
import { Transcript } from "@/components/layout/Transcript";
import { ConversationComposer } from "@/components/transcript/ConversationComposer";
import { useT } from "@/i18n";
import { useConversation } from "@/state/ConversationContext";

const STATUS_CLS: Record<string, string> = {
  connecting: "bg-blue-500/15 text-blue-700 dark:text-blue-300",
  live: "bg-blue-500/15 text-blue-700 dark:text-blue-300",
  done: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  error: "bg-red-500/15 text-red-700 dark:text-red-300",
  idle: "bg-muted text-muted-foreground",
};

export default function ConversationPage() {
  const { state, startHarness } = useConversation();
  const { t } = useT();
  const statusCls = STATUS_CLS[state.status] ?? STATUS_CLS.idle;

  const [harnessStarting, setHarnessStarting] = useState(false);
  const [harnessError, setHarnessError] = useState<string | null>(null);

  const sourceIds = state.order.filter((id) => state.blocks[id]?.kind === "source_box");
  // The global button builds every source that doesn't already have a scraper.
  const hasUnbuilt = sourceIds.some((id) => !(state.blocks[id] as any).harness);
  const canRunHarness = state.status === "done" && hasUnbuilt;

  const onRunHarness = async () => {
    setHarnessStarting(true);
    setHarnessError(null);
    try {
      await startHarness();
    } catch (e) {
      setHarnessError(e instanceof Error ? e.message : String(e));
    } finally {
      setHarnessStarting(false);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-4">
        <span className="truncate text-sm font-medium">{state.title || t("header.newTask")}</span>
        <span className={`rounded-full px-2 py-0.5 text-[11px] ${statusCls}`}>
          {t(`status.${state.status}`)}
        </span>
        {state.candidatesFound > 0 ? (
          <span className="text-xs text-muted-foreground">
            · {state.candidatesFound}{" "}
            {t(state.status === "done" ? "header.selected" : "header.candidates")}
          </span>
        ) : null}
        {state.costUsd > 0 ? (
          <span className="text-xs text-muted-foreground">· ${state.costUsd.toFixed(4)}</span>
        ) : null}

        <div className="ml-auto flex items-center gap-2">
          {harnessError ? <span className="text-xs text-red-600 dark:text-red-400">{harnessError}</span> : null}
          {canRunHarness ? (
            <button
              onClick={onRunHarness}
              disabled={harnessStarting}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs transition-colors hover:bg-accent disabled:opacity-50"
            >
              <Hammer className="h-3.5 w-3.5" />
              {harnessStarting ? t("header.starting") : t("header.buildAll")}
            </button>
          ) : null}
          <ThemeToggle />
          <LanguageToggle />
        </div>
      </header>

      <Transcript />
      <ConversationComposer />
    </div>
  );
}
