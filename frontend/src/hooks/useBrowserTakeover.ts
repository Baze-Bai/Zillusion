"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { takeoverWsUrl } from "@/lib/api";

export type TakeoverConn = "idle" | "connecting" | "connected" | "disconnected" | "error";

/**
 * Drives the human-takeover WebSocket: receives base64 JPEG screencast frames
 * (damage-driven — a static page emits none, so the canvas keeps showing the
 * last painted frame) and paints them onto the returned canvas, and exposes
 * `sendAction` to forward the user's mouse/keyboard back to the real (headed)
 * login browser via the backend CDP bridge. Click→page coordinate mapping is
 * computed by the component at event time. Connects only while `active`.
 */
export function useBrowserTakeover({
  siteId,
  active,
  onStale,
}: {
  siteId: string;
  active: boolean;
  /** Called when the backend reports there is NO live takeover behind this
   * canvas (replay of a run that died mid-takeover) — the owner should drop
   * the stale window instead of letting it sit disconnected forever. */
  onStale?: () => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [conn, setConn] = useState<TakeoverConn>("idle");
  const onStaleRef = useRef(onStale);
  onStaleRef.current = onStale;

  const sendAction = useCallback((action: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(action));
  }, []);

  useEffect(() => {
    if (!active || !siteId) return;
    let closed = false;
    const img = new Image();
    const ws = new WebSocket(takeoverWsUrl(siteId));
    wsRef.current = ws;
    setConn("connecting");

    ws.onopen = () => setConn("connected");
    ws.onerror = () => setConn("error");
    ws.onclose = () => {
      if (!closed) setConn("disconnected");
    };
    ws.onmessage = (ev: MessageEvent) => {
      let msg: { type?: string; data?: string; code?: string };
      try {
        msg = JSON.parse(ev.data as string);
      } catch {
        return;
      }
      if (msg.type === "error" && msg.code === "no_active_takeover") {
        onStaleRef.current?.();
        return;
      }
      if (msg.type === "screenshot" && msg.data) {
        img.onload = () => {
          const c = canvasRef.current;
          if (!c) return;
          if (c.width !== img.width || c.height !== img.height) {
            c.width = img.width;
            c.height = img.height;
          }
          c.getContext("2d")?.drawImage(img, 0, 0);
        };
        img.src = `data:image/jpeg;base64,${msg.data}`;
      }
      // 'ready' / 'done' / 'error' are reflected via SSE takeover state; the
      // socket closes itself after 'done'.
    };

    return () => {
      closed = true;
      try {
        ws.close();
      } catch {
        /* already closing */
      }
      wsRef.current = null;
      setConn("idle");
    };
  }, [siteId, active]);

  return { canvasRef, conn, sendAction };
}
