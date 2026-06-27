"use client";

import { useEffect, useRef } from "react";

import { RequirementSummary } from "@/components/results/RequirementSummary";
import { ActivityIndicator } from "@/components/transcript/ActivityIndicator";
import { BrowserTakeoverWindow } from "@/components/transcript/BrowserTakeoverWindow";
import { CredentialsGate } from "@/components/transcript/CredentialsGate";
import { PendingReplyDots } from "@/components/transcript/PendingReplyDots";
import { PhasePager } from "@/components/transcript/PhasePager";
import { RunLauncher } from "@/components/transcript/RunLauncher";
import { RunPanel } from "@/components/transcript/RunPanel";
import { ScraperPanel } from "@/components/transcript/ScraperPanel";
import { SourceBox } from "@/components/transcript/SourceBox";
import { TranscriptBlockView } from "@/components/transcript/TranscriptBlock";
import { useT } from "@/i18n";
import { useConversation } from "@/state/ConversationContext";
import type { SourceBlock, TranscriptBlock } from "@/types/transcript";

/** Fixed group order (point 5: requirement + 3 grouped source types). */
const TYPE_ORDER = ["api", "file", "embedded"] as const;

export function Transcript() {
  const { state } = useConversation();
  const { t } = useT();
  const scrollerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  // Autoscroll to bottom on new content, unless the user scrolled up.
  useEffect(() => {
    if (pinnedRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [state.order.length, state.lastSeq, state.activity]);

  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const blocks = state.order.map((id) => state.blocks[id]).filter(Boolean) as TranscriptBlock[];
  // The conversation (user messages on the right, agent messages on the left)
  // is pulled out of the timeline and rendered as its OWN section BELOW the Task
  // plan + data display, web-Claude style. Chronological by event seq so user
  // and agent turns interleave naturally.
  const chat = blocks
    .filter((b) => b.kind === "user_message" || b.kind === "agent_message")
    .sort((a, b) => a.seq - b.seq);
  // Timeline = the non-conversation, non-source blocks that frame the run
  // (Task plan box, file chips, errors) — rendered in event order, above.
  const timeline = blocks.filter(
    (b) => b.kind !== "source_box" && b.kind !== "user_message" && b.kind !== "agent_message",
  );
  // Source cards are only built at `done`; group by type (fixed order) and sort
  // by overall score within each group.
  const sources = blocks.filter((b): b is SourceBlock => b.kind === "source_box");
  const groups: Record<string, SourceBlock[]> = { api: [], file: [], embedded: [] };
  for (const b of sources) {
    const type = (b.source.source_type as string) || "embedded";
    (groups[type] ??= []).push(b);
  }
  for (const k of Object.keys(groups)) {
    groups[k].sort(
      (a, b) => (b.source.scores?.overall ?? -Infinity) - (a.source.scores?.overall ?? -Infinity),
    );
  }

  const empty = state.order.length === 0;

  // Typing indicator: the user spoke last and the run is still alive — the
  // agent owes a reply (a steer on an idle run auto-restarts it server-side),
  // so keep a pulsing bubble until the agent_message lands or the run goes
  // terminal. Derived from the timeline, so it survives a refresh mid-wait.
  const lastChat = chat[chat.length - 1];
  const awaitingReply =
    lastChat?.kind === "user_message" && state.status !== "done" && state.status !== "error";

  return (
    <div ref={scrollerRef} onScroll={onScroll} className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-4 px-4 py-6">
        {empty && state.status === "connecting" ? (
          <div className="text-sm text-muted-foreground">{t("transcript.connecting")}</div>
        ) : null}
        {empty && state.status === "error" ? (
          <div className="text-sm text-red-600 dark:text-red-300">{state.error || t("transcript.loadFailed")}</div>
        ) : null}

        {/* Phase pager (top of the page): flips a scraper child between its
            harness page and its Run page. Hidden until a Run exists. */}
        <PhasePager />

        {/* [content-visibility:auto] = browser-level virtualization: offscreen
            blocks skip layout+paint entirely (works with variable heights, no
            JS windowing lib, autoscroll unaffected). contain-intrinsic-size
            gives offscreen blocks an estimated height so the scrollbar stays
            stable. */}
        {timeline.map((block) => (
          <div key={block.id} className="[content-visibility:auto] [contain-intrinsic-size:auto_8rem]">
            <TranscriptBlockView block={block} />
          </div>
        ))}

        {/* Scraper child session, rendered by the ACTIVE phase page:
            - harness page → the full explore/validate presentation;
            - Run page → source card only (collapsed) + the run progress panel.
            The pager above flips between them once a Run exists. */}
        {state.harness ? (
          <ScraperPanel
            harness={state.harness}
            collapsed={!!state.harness.run && state.activeView === "run"}
          />
        ) : null}
        {/* Key gate: a keyed api site staged without its credential — the user
            obtains the key (signup guidance shown) and submits it here; the
            loop then launches and harness_site_started clears the card. */}
        {state.harness?.status === "awaiting_credentials" && state.harness.awaitingCredentials ? (
          <CredentialsGate
            siteId={state.harness.siteId}
            awaiting={state.harness.awaitingCredentials}
          />
        ) : null}
        {state.harness?.run && state.activeView === "run" ? (
          <RunPanel run={state.harness.run} />
        ) : null}

        {state.requirement ? <RequirementSummary requirement={state.requirement} /> : null}

        {TYPE_ORDER.map((type) =>
          groups[type]?.length ? (
            <section key={type} className="space-y-2">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {t(`group.${type}`)} <span className="opacity-60">· {groups[type].length}</span>
              </h3>
              {groups[type].map((block) => (
                <SourceBox key={block.id} block={block} />
              ))}
            </section>
          ) : null,
        )}

        {/* Run launcher: a button ABOVE the conversation divider, shown only
            once explore→validate PASSes (hidden while running / after a restart
            / once the run has started). */}
        <RunLauncher />

        {/* ── Conversation: user (right) + agent (left), below the Task plan +
            data display and separated by a labeled divider (web-Claude style). */}
        {chat.length > 0 ? (
          <section className="space-y-3 pt-2">
            <div className="flex items-center gap-3" aria-hidden>
              <div className="h-px flex-1 bg-border" />
              <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {t("conversation.title")}
              </span>
              <div className="h-px flex-1 bg-border" />
            </div>
            {chat.map((block) => (
              <div key={block.id} className="[content-visibility:auto] [contain-intrinsic-size:auto_6rem]">
                <TranscriptBlockView block={block} />
              </div>
            ))}
            {awaitingReply ? <PendingReplyDots /> : null}
          </section>
        ) : null}

        {/* Human takeover canvas — pinned at the BOTTOM of the conversation so
            it lands where the user's attention is (and gets auto-scrolled into
            view) when the harness hands over the browser for login / captcha /
            challenge. */}
        {state.harness?.takeover?.active ? (
          <BrowserTakeoverWindow
            siteId={state.harness.siteId}
            takeover={state.harness.takeover}
          />
        ) : null}

        <ActivityIndicator />
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
