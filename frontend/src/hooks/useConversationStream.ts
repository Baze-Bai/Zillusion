"use client";

import { useEffect } from "react";
import type { Dispatch } from "react";

import { streamUrl } from "@/lib/api";
import { parseSSEStream } from "@/lib/sseParser";
import type { Action } from "@/state/conversationReducer";

// Reconnect cadence for a session whose stream closed cleanly while its status
// is still non-terminal (a "zombie" — e.g. a backend restart orphaned the run
// without a terminal event, or the run may be auto-restarted by a steer). Each
// poll is a cheap replay-from-lastSeq that ends instantly when nothing is new.
const ZOMBIE_RECONNECT_MS = 5000;

/**
 * Open GET /stream/{sessionId} (replay-then-live), feed the parser into the
 * reducer, and reconnect with the seq cursor on transient failures. The reducer
 * dedups by seq, so a reconnect that re-replays is harmless. StrictMode's
 * double-mount is handled by aborting the prior reader in cleanup.
 *
 * `isTerminal` decides what a CLEAN server close means: finished run → stop
 * (review mode); non-terminal → keep a slow reconnect loop so the session
 * picks events back up live if the run returns. Defaults to "always stop"
 * (the legacy behavior) for callers that don't wire a status getter.
 */
export function useConversationStream(
  sessionId: string | null,
  dispatch: Dispatch<Action>,
  getLastSeq: () => number,
  epoch: number = 0,
  isTerminal: () => boolean = () => true,
): void {
  useEffect(() => {
    if (!sessionId) return;

    const controller = new AbortController();
    let cancelled = false;
    let attempt = 0;

    async function run() {
      while (!cancelled) {
        const afterSeq = getLastSeq();
        dispatch({ type: "stream_status", status: afterSeq > 0 ? "live" : "connecting" });
        try {
          const res = await fetch(streamUrl(sessionId!, afterSeq), {
            headers: { Accept: "text/event-stream" },
            signal: controller.signal,
            cache: "no-store",
          });
          if (!res.ok) throw new Error(`stream HTTP ${res.status}`);
          attempt = 0; // connected — refresh the transient-error budget
          await parseSSEStream(
            res,
            (eventType, data, id) => {
              dispatch({
                type: "server_event",
                eventType,
                data,
                seq: id != null ? Number(id) : null,
              });
            },
            controller.signal,
          );
          // Clean end: the server closed the stream. If the run reached a
          // terminal state (or this was a replay-only review of one) we are
          // done for good. Otherwise this session may come back to life — a
          // steer auto-restarts idle runs, and a backend restart can orphan a
          // "running" timeline with no terminal event — so keep re-attaching.
          if (isTerminal()) return;
          await new Promise((r) => setTimeout(r, ZOMBIE_RECONNECT_MS));
          // loop → reconnect from getLastSeq()
        } catch (err) {
          if (cancelled || controller.signal.aborted) return;
          attempt += 1;
          if (attempt > 5) {
            dispatch({ type: "stream_status", status: "error" });
            return;
          }
          await new Promise((r) => setTimeout(r, Math.min(1000 * attempt, 5000)));
          // loop → reconnect from getLastSeq()
        }
      }
    }

    run();
    return () => {
      cancelled = true;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, dispatch, getLastSeq, epoch, isTerminal]);
}
