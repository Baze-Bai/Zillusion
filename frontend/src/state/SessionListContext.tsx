"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { createDiscovery, fetchSessions } from "@/lib/api";
import type { SessionSummary } from "@/types/transcript";

interface SessionListValue {
  sessions: SessionSummary[];
  loading: boolean;
  refresh: () => Promise<void>;
  createSession: (query: string) => Promise<string>;
}

const Ctx = createContext<SessionListValue | null>(null);

// Sidebar statuses that can still change server-side. Everything else
// (completed/error/interrupted/cancelled) is terminal.
const ACTIVE_STATUSES = new Set(["pending", "running"]);
const ACTIVE_POLL_MS = 5000;

export function SessionListProvider({ children }: { children: React.ReactNode }) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      setSessions(await fetchSessions());
    } catch {
      /* keep last good list */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // The list is a REST snapshot; the SSE stream only feeds the conversation
  // that's currently open. Status flips of sessions you're NOT viewing —
  // detached scraper children finishing, wave-created children appearing,
  // backend-restart reconciliation writing "interrupted" (no event exists for
  // it at all) — would otherwise sit stale until a full page reload. While
  // anything is in flight, re-fetch on a slow tick; once every session is
  // terminal the interval stops. Hidden tabs get throttled by the browser,
  // which is fine — the visibility listener below catches up on return.
  const hasActive = sessions.some((s) => ACTIVE_STATUSES.has(s.status));
  useEffect(() => {
    if (!hasActive) return;
    const id = setInterval(refresh, ACTIVE_POLL_MS);
    return () => clearInterval(id);
  }, [hasActive, refresh]);

  // Re-sync after the user was away (other tab / other window), covering
  // changes that happened while polling was off or throttled.
  useEffect(() => {
    const onFocus = () => refresh();
    const onVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh]);

  const createSession = useCallback(
    async (query: string) => {
      const id = await createDiscovery(query);
      // Sidebar shows the new task immediately (placeholder title); the live
      // session_titled event will refresh it to the real title shortly.
      refresh();
      return id;
    },
    [refresh],
  );

  const value = useMemo(
    () => ({ sessions, loading, refresh, createSession }),
    [sessions, loading, refresh, createSession],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSessionList(): SessionListValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSessionList must be used within SessionListProvider");
  return v;
}
