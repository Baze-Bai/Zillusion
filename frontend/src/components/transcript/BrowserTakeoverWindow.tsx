"use client";

import { useCallback, useState } from "react";

import { Markdown } from "@/components/common/Markdown";
import { useBrowserTakeover } from "@/hooks/useBrowserTakeover";
import { useT } from "@/i18n";
import { useConversationActions } from "@/state/ConversationContext";
import type { TakeoverState } from "@/types/transcript";

const WALL_TITLE_KEY: Record<string, string> = {
  login: "takeover.title.login",
  login_modal: "takeover.title.login",
  captcha: "takeover.title.captcha",
  challenge: "takeover.title.challenge",
};

/**
 * Embedded interactive browser for human takeover. Streams the harness's headed
 * login browser into the data-source sub-session: the user clicks "放大" to
 * enlarge, operates on the live page (login / captcha / challenge), then clicks
 * "完成接管" — which tells the harness to save auth and continue the crawl.
 */
export function BrowserTakeoverWindow({
  siteId,
  takeover,
}: {
  siteId: string;
  takeover: TakeoverState;
}) {
  const { t } = useT();
  // Actions-only: the takeover canvas streams JPEG frames — re-rendering it
  // on every SSE event (the old merged-context behavior) competed with frame
  // decoding for main-thread time.
  const { dismissTakeover } = useConversationActions();
  const { canvasRef, conn, sendAction } = useBrowserTakeover({
    siteId,
    active: takeover.active,
    onStale: dismissTakeover,
  });
  const [enlarged, setEnlarged] = useState(false);

  // Map canvas-space to page-space at EVENT time from the canvas's current
  // bitmap vs displayed size — a cached scale goes stale when the layout
  // resizes (enlarge) while the static page emits no new frames.
  const toPage = useCallback(
    (clientX: number, clientY: number) => {
      const c = canvasRef.current;
      if (!c) return { x: 0, y: 0 };
      const r = c.getBoundingClientRect();
      return {
        x: Math.round((clientX - r.left) * (r.width ? c.width / r.width : 1)),
        y: Math.round((clientY - r.top) * (r.height ? c.height / r.height : 1)),
      };
    },
    [canvasRef],
  );

  const onClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const { x, y } = toPage(e.clientX, e.clientY);
    sendAction({ action: "click", x, y, button: e.button === 2 ? "right" : "left" });
  };
  const onDoubleClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const { x, y } = toPage(e.clientX, e.clientY);
    sendAction({ action: "dblclick", x, y });
  };
  const onMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (e.buttons === 0) return; // only drag, not idle hover
    const { x, y } = toPage(e.clientX, e.clientY);
    sendAction({ action: "mousemove", x, y });
  };
  const onWheel = (e: React.WheelEvent<HTMLCanvasElement>) => {
    const { x, y } = toPage(e.clientX, e.clientY);
    sendAction({ action: "scroll", x, y, deltaX: e.deltaX, deltaY: e.deltaY });
  };
  const onKeyDown = (e: React.KeyboardEvent<HTMLCanvasElement>) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      sendAction({ action: "type", text: e.key });
      return;
    }
    let key = e.key;
    if (e.ctrlKey && key !== "Control") key = `Control+${key}`;
    if (e.altKey && key !== "Alt") key = `Alt+${key}`;
    if (e.shiftKey && key !== "Shift") key = `Shift+${key}`;
    if (e.metaKey && key !== "Meta") key = `Meta+${key}`;
    sendAction({ action: "press", key });
  };

  const title = t(WALL_TITLE_KEY[takeover.wallType ?? ""] ?? "takeover.title.default");

  const panel = (
    <div className="overflow-hidden rounded-md border-2 border-primary/60 bg-black">
      <div className="flex flex-wrap items-center justify-between gap-2 bg-muted px-3 py-2">
        <div className="flex items-center gap-2 text-xs">
          <span
            className={`h-2 w-2 shrink-0 rounded-full ${
              conn === "connected"
                ? "bg-emerald-500 dark:bg-emerald-400"
                : "bg-amber-500 dark:bg-amber-400 animate-pulse"
            }`}
          />
          <span className="font-medium">{title}</span>
          <span className="text-muted-foreground">· {t(`takeover.conn.${conn}`)}</span>
          {takeover.pageUrl ? (
            <span className="max-w-[18rem] truncate text-muted-foreground">{takeover.pageUrl}</span>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setEnlarged((v) => !v)}
            className="rounded border border-border px-2 py-1 text-xs hover:bg-background"
          >
            {t(enlarged ? "takeover.shrink" : "takeover.enlarge")}
          </button>
          <button
            type="button"
            onClick={() => sendAction({ action: "done" })}
            className="rounded bg-primary px-3 py-1 text-xs font-medium text-primary-foreground hover:opacity-90"
          >
            {t("takeover.done")}
          </button>
        </div>
      </div>

      {takeover.message ? (
        <div className="border-b border-border bg-background px-3 py-2">
          <Markdown>{takeover.message}</Markdown>
        </div>
      ) : takeover.reason ? (
        <div className="border-b border-border bg-background px-3 py-2 text-sm">{takeover.reason}</div>
      ) : null}

      <div className="bg-neutral-900">
        <canvas
          ref={canvasRef}
          tabIndex={0}
          onClick={onClick}
          onDoubleClick={onDoubleClick}
          onMouseMove={onMouseMove}
          onWheel={onWheel}
          onKeyDown={onKeyDown}
          onContextMenu={(e) => e.preventDefault()}
          className="block w-full cursor-default outline-none"
        />
      </div>
    </div>
  );

  // Both modes render the SAME tree shape (wrappers always present, classes
  // toggle; `contents` = layout-neutral). A structural switch would remount the
  // <canvas> and wipe the last screencast frame — and on a static page (e.g. a
  // captcha) CDP emits no new frame to repaint it, leaving the window black.
  return (
    <div
      className={
        enlarged
          ? "fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
          : "contents"
      }
    >
      <div className={enlarged ? "max-h-[92vh] w-full max-w-5xl overflow-auto" : "contents"}>
        {panel}
      </div>
    </div>
  );
}
