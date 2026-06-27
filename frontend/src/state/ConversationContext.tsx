"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";

import { useConversationStream } from "@/hooks/useConversationStream";
import { startHarness as startHarnessApi, startRun as startRunApi, steerDiscovery, steerHarness as steerHarnessApi, steerRun as steerRunApi } from "@/lib/api";
import {
  conversationReducer,
  initialConversationState,
  type ConversationState,
} from "@/state/conversationReducer";
import { useSessionList } from "@/state/SessionListContext";
import type { ArtifactRef } from "@/types/transcript";

interface ConversationActions {
  /** Free-form message to the agent from the bottom composer (chat). Routes to
   * the right steering channel and re-runs a terminal run. */
  sendMessage: (message: string) => Promise<void>;
  startHarness: (siteIds?: string[]) => Promise<string[]>;
  /** Start the Run (production crawl) phase for the current scraper child. */
  startRun: () => Promise<void>;
  openArtifact: (artifact: ArtifactRef) => void;
  closeArtifact: () => void;
  /** Drop a stale takeover canvas (the takeover WS said none is active). */
  dismissTakeover: () => void;
  /** Flip the phase pager between the harness page and the Run page. */
  setView: (view: "harness" | "run") => void;
}

interface ConversationContextValue extends ConversationActions {
  state: ConversationState;
}

// State and actions live in SEPARATE contexts: every SSE event produces a new
// `state`, and a single merged context made every consumer re-render per
// event — including action-only components (FileChip, RunPanel, the takeover
// canvas), where the context subscription pierced React.memo. The actions
// object is identity-stable for the provider's lifetime, so components that
// subscribe via useConversationActions() re-render only on their own props.
const StateCtx = createContext<ConversationState | null>(null);
const ActionsCtx = createContext<ConversationActions | null>(null);

export function ConversationProvider({
  sessionId,
  children,
}: {
  sessionId: string | null;
  children: React.ReactNode;
}) {
  const [state, dispatch] = useReducer(conversationReducer, sessionId, initialConversationState);

  // Stable getter for the reconnect cursor (reads the latest lastSeq).
  const seqRef = useRef(0);
  seqRef.current = state.lastSeq;
  const getLastSeq = useCallback(() => seqRef.current, []);

  // Latest run status, readable inside callbacks without re-creating them.
  const statusRef = useRef(state.status);
  statusRef.current = state.status;
  // Whether the run looks finished from the event timeline. The stream hook
  // uses this to tell "review of a finished run" (stop on clean close) from a
  // zombie "running" session whose stream closed without a terminal event
  // (e.g. a backend restart killed the run mid-flight) — those keep a slow
  // reconnect loop so a steer-triggered restart shows up live.
  const isTerminal = useCallback(
    () => statusRef.current === "done" || statusRef.current === "error",
    [],
  );
  // Latest harness target (a scraper child session), read inside the stable
  // sendMessage callback to route the message to the right channel.
  const harnessRef = useRef(state.harness);
  harnessRef.current = state.harness;
  // Active phase page (harness vs run) — the composer routes by it.
  const viewRef = useRef(state.activeView);
  viewRef.current = state.activeView;

  // Bumping the epoch re-opens the stream (replay is deduped by seq). Used to
  // re-attach after starting the harness phase, which begins after the
  // discovery stream has already closed.
  const [epoch, setEpoch] = useState(0);
  useConversationStream(sessionId, dispatch, getLastSeq, epoch, isTerminal);

  // Keep the sidebar in sync when this conversation's title resolves or the
  // run reaches a terminal state.
  const { refresh } = useSessionList();
  const lastTitle = useRef<string | null>(null);
  useEffect(() => {
    if (state.title && state.title !== lastTitle.current) {
      lastTitle.current = state.title;
      refresh();
    }
  }, [state.title, refresh]);
  useEffect(() => {
    if (state.status === "done" || state.status === "error") refresh();
  }, [state.status, refresh]);

  // The conversation composer: a free-form message to the agent. Routes to the
  // right channel — a scraper child session steers its harness site; a discovery
  // session sends a "note". Both echo back as a right-aligned user bubble
  // (user_steer / harness_user_steer) — there is NO optimistic local append, so
  // the bubbles only render if the stream is alive. The connection may have
  // closed cleanly (terminal run, or a zombie "running" session — see
  // isTerminal above), and a steer on an idle run RE-RUNS it server-side, so we
  // ALWAYS re-open the stream after a send: replay resumes from lastSeq and is
  // deduped by seq, making the reconnect harmless on a healthy live stream.
  const sendMessage = useCallback(
    async (message: string) => {
      if (!sessionId) return;
      const wasTerminal = statusRef.current === "done" || statusRef.current === "error";
      const harness = harnessRef.current;
      if (harness?.run && viewRef.current === "run") {
        // On the RUN page → talk to the Run agent: delivered live while it
        // crawls; on an idle/terminal run the backend AUTO-RERUNS the validated
        // workflow carrying this message into the new session.
        await steerRunApi(harness.siteId, message);
      } else if (harness) {
        // On the harness page → explore/validate steering (live delivery or
        // auto-restart of the explore loop, as before).
        await steerHarnessApi(harness.siteId, sessionId, message);
      } else {
        await steerDiscovery(sessionId, message, "note");
      }
      if (wasTerminal) refresh();
      setEpoch((e) => e + 1);
    },
    [sessionId, refresh],
  );

  const startHarness = useCallback(
    async (siteIds?: string[]): Promise<string[]> => {
      if (!sessionId) return [];
      const res = await startHarnessApi(
        sessionId,
        siteIds && siteIds.length ? { site_ids: siteIds } : {},
      );
      // Each requested source becomes its OWN child session — surface them in
      // the sidebar immediately (the caller navigates to the child page).
      refresh();
      setEpoch((e) => e + 1);
      return res.child_query_ids ?? [];
    },
    [sessionId, refresh],
  );

  // Start the Run (production crawl) phase on the current scraper child. The run
  // is a new RunRegistry run on the SAME session id (seq continues), so we just
  // re-open the stream (replay is deduped by seq) to pick up its run_* events —
  // the same re-attach trick startHarness uses after the prior phase's stream
  // has already closed.
  const startRun = useCallback(async (): Promise<void> => {
    const harness = harnessRef.current;
    if (!harness) return;
    // A crawl on an INCONCLUSIVE loop only ever starts through the explicit
    // "run anyway (at your own risk)" entry points (launcher + re-run button),
    // so the override is derived here once to pass the backend's PASS gate.
    await startRunApi(
      harness.siteId,
      harness.finalVerdict === "INCONCLUSIVE" ? { force: true } : {},
    );
    refresh();
    setEpoch((e) => e + 1);
  }, [refresh]);

  const openArtifact = useCallback((artifact: ArtifactRef) => {
    dispatch({ type: "ui/open_artifact", artifact });
  }, []);
  const closeArtifact = useCallback(() => dispatch({ type: "ui/close_artifact" }), []);
  const dismissTakeover = useCallback(
    () => dispatch({ type: "ui/harness_takeover_dismiss" }),
    [],
  );
  const setView = useCallback(
    (view: "harness" | "run") => dispatch({ type: "ui/set_view", view }),
    [],
  );

  const actions = useMemo(
    () => ({ sendMessage, startHarness, startRun, openArtifact, closeArtifact, dismissTakeover, setView }),
    [sendMessage, startHarness, startRun, openArtifact, closeArtifact, dismissTakeover, setView],
  );

  return (
    <ActionsCtx.Provider value={actions}>
      <StateCtx.Provider value={state}>{children}</StateCtx.Provider>
    </ActionsCtx.Provider>
  );
}

/** Live conversation state — re-renders the consumer on every reduced event. */
export function useConversationState(): ConversationState {
  const v = useContext(StateCtx);
  if (!v) throw new Error("useConversationState must be used within ConversationProvider");
  return v;
}

/** Stable action callbacks ONLY — never re-renders the consumer on state
 * changes. Use this in components that don't read `state` (chips, buttons,
 * the takeover canvas) so React.memo on them actually holds. */
export function useConversationActions(): ConversationActions {
  const v = useContext(ActionsCtx);
  if (!v) throw new Error("useConversationActions must be used within ConversationProvider");
  return v;
}

/** Combined view (state + actions) — the original API, for components that
 * need both. Subscribes to state, so it re-renders per event. */
export function useConversation(): ConversationContextValue {
  const state = useConversationState();
  const actions = useConversationActions();
  return useMemo(() => ({ state, ...actions }), [state, actions]);
}
