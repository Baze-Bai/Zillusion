/**
 * The transcript is `events.reduce(reducer, initialState)`. Live and review use
 * the SAME reducer — review just front-loads historical events before the live
 * tail. Every server-event case is idempotent and order-tolerant: events with
 * `seq <= lastSeq` are dropped, so replay↔live overlap and reconnection are
 * safe. Blocks are addressable by stable id so updates reconcile in place.
 *
 * Display model (2026 redesign):
 *  - Discovery narration (agent_text), pipeline stage markers, and the agent's
 *    final JSON dump are NO LONGER persisted as blocks. They feed a single
 *    EPHEMERAL `activity` indicator that is cleared on done/error. Only the
 *    user query, the steerable Task plan, and — once the run is finalized — the
 *    grouped source cards (built from the final report) persist on the page.
 */

import type {
  ArtifactRef,
  HarnessState,
  SourceBlock,
  TaskDescriptionBlock,
  TranscriptBlock,
} from "@/types/transcript";
import type { DataSource, StructuredRequirement } from "@/types/data-source";

/** Ephemeral "working" status shown while the run is live (never persisted). */
export interface ActivityState {
  phase: "thinking" | "executing";
  /** Stage key when `isStage` is set, otherwise raw agent narration. */
  text: string;
  isStage?: boolean;
}

export interface ConversationState {
  sessionId: string | null;
  order: string[];
  blocks: Record<string, TranscriptBlock>;
  status: "idle" | "connecting" | "live" | "done" | "error";
  lastSeq: number;
  costUsd: number;
  candidatesFound: number;
  title: string | null;
  activeArtifact: ArtifactRef | null;
  error: string | null;
  /** Ephemeral working indicator (discovery phase); null when idle/terminal. */
  activity: ActivityState | null;
  /** Parsed requirement from the final report (drives the requirement summary). */
  requirement: StructuredRequirement | null;
  /** Top-level harness (scraper) state — populated only on a scraper CHILD
   * session (which has no source cards of its own); null on discovery. */
  harness: HarnessState | null;
  /** Which phase PAGE of a scraper child is showing: the explore/validate
   * (harness) page or the Run page. Pager-set by the user; auto-flips to "run"
   * on run_started and back to "harness" when the explore loop (re)starts.
   * Only meaningful once `harness.run` exists (before that there is one page). */
  activeView: "harness" | "run";
}

export function initialConversationState(sessionId: string | null): ConversationState {
  return {
    sessionId,
    order: [],
    blocks: {},
    status: sessionId ? "connecting" : "idle",
    lastSeq: 0,
    costUsd: 0,
    candidatesFound: 0,
    title: null,
    activeArtifact: null,
    error: null,
    activity: null,
    requirement: null,
    harness: null,
    activeView: "harness",
  };
}

export type Action =
  | { type: "reset"; sessionId: string | null }
  | { type: "stream_status"; status: ConversationState["status"] }
  | { type: "server_event"; eventType: string; data: unknown; seq: number | null }
  | { type: "ui/open_artifact"; artifact: ArtifactRef }
  | { type: "ui/close_artifact" }
  | { type: "ui/steer_pending"; blockId: string; message: string }
  | { type: "ui/steer_result"; blockId: string; ok: boolean }
  | { type: "ui/harness_steer_pending"; siteId: string; message: string }
  | { type: "ui/harness_steer_result"; siteId: string; ok: boolean }
  | { type: "ui/harness_takeover_dismiss" }
  | { type: "ui/set_view"; view: "harness" | "run" };

const TASK_DESC_ID = "task_desc";

function putBlock(state: ConversationState, block: TranscriptBlock): ConversationState {
  const existed = state.blocks[block.id] !== undefined;
  return {
    ...state,
    blocks: { ...state.blocks, [block.id]: block },
    order: existed ? state.order : [...state.order, block.id],
  };
}

function mergeSource(a: Partial<DataSource>, b: Partial<DataSource>): Partial<DataSource> {
  const out: Record<string, unknown> = { ...a };
  for (const [k, v] of Object.entries(b)) {
    if (v === undefined || v === null) continue;
    // Prefer the longer description (source_committed has the full text;
    // partial_sources truncates to 200 chars).
    if (k === "description" && typeof a.description === "string" && a.description.length > String(v).length) {
      continue;
    }
    out[k] = v;
  }
  return out as Partial<DataSource>;
}

function upsertSource(state: ConversationState, src: Partial<DataSource>, seq: number): ConversationState {
  if (!src || !src.id) return state;
  const id = `source-${src.id}`;
  const existing = state.blocks[id] as SourceBlock | undefined;
  const block: SourceBlock = {
    kind: "source_box",
    id,
    seq,
    source: existing ? mergeSource(existing.source, src) : src,
  };
  return putBlock(state, block);
}

/** The agent's final assistant message is a big JSON dump (the backend parser's
 * source of truth). We never surface it verbatim — it collapses to a
 * "Finalizing" tick in the ephemeral activity indicator (points 1 & 2). */
function looksLikeFinalJson(text: string): boolean {
  const t = text.trimStart();
  return t.startsWith("{") || t.startsWith("[") || t.startsWith("```");
}

function applyServerEvent(
  state: ConversationState,
  eventType: string,
  data: any,
  seq: number,
): ConversationState {
  switch (eventType) {
    case "query_accepted": {
      // A feedback-restart re-emits query_accepted with the SYSTEM-COMPOSED
      // enriched prompt (original query + "[Follow-up feedback…]" + the user's
      // note). Rendering that would (a) rewrite + re-seq the original query
      // bubble to the bottom and (b) duplicate the note already shown as its
      // own steer bubble — so skip the bubble, keep the status flip.
      if (data?.rerun) return { ...state, status: "live" };
      const text = data?.query ?? "";
      const s = putBlock(state, { kind: "user_message", id: "user-query", seq, text });
      return { ...s, status: "live" };
    }

    case "session_titled":
      return { ...state, title: data?.title ?? state.title };

    case "user_steer":
      // The user's mid-run guidance, rendered as a right-aligned bubble.
      return putBlock(state, {
        kind: "user_message",
        id: `steer-${seq}`,
        seq,
        text: data?.content ?? "",
      });

    // A deliberate message the agent sent the user via its send_user_message
    // tool. Discovery emits `agent_message`; the harness emits
    // `harness_agent_message` (file reverse-channel). Both PERSIST as a
    // left-aligned chat bubble (unlike ephemeral agent_text narration).
    case "agent_message":
    case "harness_agent_message": {
      const text = data?.text ?? data?.content ?? data?.message ?? "";
      if (!String(text).trim()) return state;
      return putBlock(state, {
        kind: "agent_message",
        id: `amsg-${seq}`,
        seq,
        markdown: String(text),
        createdAt: typeof data?.created_at === "string" ? data.created_at : null,
      });
    }

    case "task_description_committed": {
      const existing = state.blocks[TASK_DESC_ID] as TaskDescriptionBlock | undefined;
      const block: TaskDescriptionBlock = {
        kind: "task_description_box",
        id: TASK_DESC_ID,
        seq,
        text: data?.content ?? existing?.text ?? "",
        version: (existing?.version ?? 0) + 1,
        status: "live",
        // An authoritative commit supersedes any optimistic edit.
        pendingMessage: undefined,
        sendState: "idle",
      };
      return putBlock(state, block);
    }

    // ── Discovery narration → ephemeral activity (never persisted) ──
    case "agent_text": {
      const text = data?.text_full || data?.text || "";
      if (!text) return state;
      if (looksLikeFinalJson(text)) {
        return { ...state, activity: { phase: "executing", text: "finalize", isStage: true } };
      }
      return { ...state, activity: { phase: "thinking", text } };
    }

    case "stage_start": {
      const stage = data?.stage ?? "";
      return { ...state, activity: { phase: "executing", text: stage, isStage: true } };
    }

    case "stage_complete":
      return {
        ...state,
        costUsd: typeof data?.cost_usd === "number" ? data.cost_usd : state.costUsd,
      };

    case "progress":
      return {
        ...state,
        candidatesFound:
          typeof data?.candidates_found === "number" ? data.candidates_found : state.candidatesFound,
      };

    // Live source commits are NOT rendered as cards anymore — the cards are
    // built at `done` from the final grouped report (so vetoed/dropped sources
    // never appear, and there is no re-sort flicker). We only surface liveness.
    case "source_committed": {
      const name = (data?.source ?? {})?.name;
      return name ? { ...state, activity: { phase: "executing", text: String(name) } } : state;
    }

    case "partial_sources":
      return state;

    case "parse_intent_result":
      return state;

    case "done": {
      const report = data?.report;
      // Replace the source set: a re-run (done-after-done, e.g. the feedback
      // re-run) refines results, so clear prior source cards and rebuild from the
      // latest report rather than accumulating stale ones. The timeline (user
      // query, steers/feedback) is kept.
      const keptOrder = state.order.filter((id) => state.blocks[id]?.kind !== "source_box");
      const keptBlocks: Record<string, TranscriptBlock> = {};
      for (const id of keptOrder) keptBlocks[id] = state.blocks[id];
      let next: ConversationState = { ...state, order: keptOrder, blocks: keptBlocks };
      // Build the persisted source cards from the final report's three grouped
      // lists (type-grouped + rank-sorted within each, full fields incl.
      // usage_guide). Append order api→file→embedded; the renderer regroups.
      const grouped: Partial<DataSource>[] = [
        ...(report?.api_sources ?? []),
        ...(report?.file_sources ?? []),
        ...(report?.embedded_sources ?? []),
      ];
      for (const s of grouped) next = upsertSource(next, s, seq);
      return {
        ...next,
        status: "done",
        // Overwrite the live "candidates" count (judge-INPUT, e.g. 65) with the
        // final SELECTED count (judge-OUTPUT = the grouped report = sidebar
        // n_sources, e.g. 51) so the done header agrees with the sidebar. The
        // header label switches "candidates"→"selected" on done (see page.tsx).
        candidatesFound: grouped.length,
        activity: null,
        requirement: report?.requirement ?? next.requirement,
      };
    }

    case "error":
      return {
        ...putBlock(state, { kind: "error", id: `error-${seq}`, seq, message: data?.message ?? "Unknown error" }),
        status: "error",
        activity: null,
        error: data?.message ?? "Unknown error",
      };

    // ── The final report file is intentionally NOT surfaced to the user (point 10) ──
    case "artifact_ready": {
      if (data?.kind === "final_report") return state;
      const artifact: ArtifactRef = {
        sessionId: state.sessionId ?? "",
        artifactId: data?.artifact_id,
        filename: data?.filename ?? "file",
        contentType: data?.content_type ?? "json",
        kind: data?.kind,
      };
      return putBlock(state, {
        kind: "file_chip",
        id: `artifact-${data?.artifact_id}`,
        seq,
        artifact,
        label: data?.filename,
      });
    }

    // ── harness (scraper) events → TOP-LEVEL state.harness (a scraper child
    //    session has no source cards of its own) ──
    case "harness_site_started": {
      const prev = state.harness;
      return {
        ...state,
        // The explore loop (re)started — the Run page is gone (run cleared
        // below), so the pager view returns to the harness page.
        activeView: "harness",
        harness: {
          siteId: data?.site_id,
          status: "running",
          iterations: prev?.iterations ?? [],
          // Count loop (re)starts: iteration numbers reset per start, so rows
          // are stamped with their build to stay distinguishable.
          builds: (prev?.builds ?? 0) + 1,
          taskPlan: prev?.taskPlan,
          sampleArtifact: prev?.sampleArtifact,
          sourceName: data?.name ?? prev?.sourceName,
          sourceUrl: data?.url ?? prev?.sourceUrl,
          source: data?.source ?? prev?.source,
          // A (re)start of the explore→validate loop clears any prior PASS
          // verdict + Run-phase state, so the Run button hides until this loop
          // reaches PASS again (requirement: hidden while running / on restart).
          finalVerdict: undefined,
          exitReason: undefined,
          run: undefined,
          // The loop is launching — the key gate (if any) has been satisfied.
          awaitingCredentials: undefined,
        },
      };
    }

    // Key-gated api site: the backend staged it but won't explore until the
    // user supplies the API key (POST /harness/{site}/credentials). Initializes
    // state.harness like harness_site_started does (this may be the child
    // session's FIRST event), with the signup guidance for the key-entry card.
    case "harness_site_awaiting_credentials": {
      const prev = state.harness;
      return {
        ...state,
        activeView: "harness",
        harness: {
          siteId: data?.site_id,
          status: "awaiting_credentials",
          iterations: prev?.iterations ?? [],
          builds: prev?.builds ?? 0,
          taskPlan: prev?.taskPlan,
          sampleArtifact: prev?.sampleArtifact,
          sourceName: data?.name ?? prev?.sourceName,
          sourceUrl: data?.url ?? prev?.sourceUrl,
          source: data?.source ?? prev?.source,
          finalVerdict: prev?.finalVerdict,
          exitReason: prev?.exitReason,
          run: undefined,
          awaitingCredentials: {
            authType: data?.auth_type,
            signupUrl: data?.signup_url,
            signupInstructions: data?.signup_instructions,
            signupSteps: Array.isArray(data?.signup_steps) ? data.signup_steps : [],
            writeTo: data?.write_to,
            provider: data?.provider,
            sourceUrl: data?.source_url,
            docsUrl: data?.docs_url,
          },
        },
      };
    }

    // The key arrived and the loop is relaunching (harness_site_started follows
    // on the new run) — drop the gate card right away.
    case "harness_credentials_received": {
      if (!state.harness) return state;
      return { ...state, harness: { ...state.harness, awaitingCredentials: undefined } };
    }

    case "harness_iter_done": {
      if (!state.harness) return state;
      const iterations = [
        ...state.harness.iterations,
        {
          n: data?.iter_n,
          note: data?.verdict ? `verdict: ${data.verdict}` : "",
          build: state.harness.builds ?? 1,
        },
      ];
      return { ...state, harness: { ...state.harness, iterations } };
    }

    // Per-turn agent activity (explore/validate) → ephemeral live indicator,
    // mirroring discovery's `activity`. Cleared on harness_site_done.
    case "harness_activity": {
      if (!state.harness) return state;
      const phase = data?.phase === "executing" ? "executing" : "thinking";
      const stage = data?.stage === "validate" ? "validate" : "explore";
      const text = typeof data?.text === "string" ? data.text : "";
      return { ...state, harness: { ...state.harness, activity: { phase, stage, text } } };
    }

    case "harness_task_plan": {
      if (!state.harness) return state;
      const prev = state.harness.taskPlan;
      return {
        ...state,
        harness: {
          ...state.harness,
          taskPlan: {
            text: data?.text ?? "",
            version: (prev?.version ?? 0) + 1,
            pendingMessage: undefined,
            sendState: "idle",
          },
        },
      };
    }

    case "harness_sample_ready": {
      if (!state.harness) return state;
      const sampleArtifact: ArtifactRef = {
        sessionId: state.sessionId ?? "",
        artifactId: data?.artifact_id,
        filename: data?.filename ?? "output_sample.json",
        contentType: data?.content_type ?? "json",
        kind: "harness_output_sample",
      };
      return { ...state, harness: { ...state.harness, sampleArtifact } };
    }

    case "harness_site_done": {
      if (!state.harness) return state;
      // A finished site has no pending takeover and no live activity. Capture the
      // loop's final verdict + exit reason — PASS shows the primary Run button;
      // anything else shows the gate note (with an override on INCONCLUSIVE).
      return {
        ...state,
        harness: {
          ...state.harness,
          status: "done",
          finalVerdict: data?.final_verdict ?? state.harness.finalVerdict,
          exitReason: data?.exit_reason ?? state.harness.exitReason,
          takeover: undefined,
          activity: undefined,
        },
      };
    }

    case "harness_takeover_request": {
      if (!state.harness) return state;
      return {
        ...state,
        harness: {
          ...state.harness,
          takeover: {
            active: true,
            reason: data?.reason ?? "",
            message: data?.message ?? "",
            pageUrl: data?.page_url,
            wallType: data?.wall_type,
          },
        },
      };
    }

    case "harness_takeover_done": {
      if (!state.harness) return state;
      return { ...state, harness: { ...state.harness, takeover: undefined } };
    }

    case "harness_user_steer":
      return putBlock(state, {
        kind: "user_message",
        id: `hsteer-${seq}`,
        seq,
        text: data?.content ?? "",
      });

    // phase markers (status, not rendered as blocks)
    case "harness_started":
      return { ...state, status: "live" };
    case "harness_done":
    case "harness_no_sites":
      return { ...state, status: "done", activity: null };
    case "harness_iter_start":
      return state;

    // ── Run (production crawl) phase → state.harness.run ──────────────────
    // Started when the user clicks "Run" on a PASSed scraper child. The run
    // executes in the SAME child session (a new RunRegistry run whose seq
    // continues the timeline), so these events arrive on the same stream as
    // explore/validate, and we hang the live run state off the existing harness.
    case "run_started": {
      if (!state.harness) return state;
      return {
        ...state,
        status: "live",
        // A run just started — flip the pager to the Run page (the user either
        // clicked Run/重新爬取, or a run-page steer auto-reran it).
        activeView: "run",
        harness: {
          ...state.harness,
          run: { status: "running", runId: data?.run_id, crawlMode: data?.crawl_mode },
        },
      };
    }

    case "run_activity": {
      if (!state.harness?.run) return state;
      const phase = data?.phase === "executing" ? "executing" : "thinking";
      const text = typeof data?.text === "string" ? data.text : "";
      return {
        ...state,
        harness: { ...state.harness, run: { ...state.harness.run, activity: { phase, text } } },
      };
    }

    case "run_progress": {
      if (!state.harness?.run) return state;
      return {
        ...state,
        harness: {
          ...state.harness,
          run: {
            ...state.harness.run,
            progress: {
              state: data?.state ?? "running",
              recordCount: data?.record_count ?? null,
              linesEmitted: data?.lines_emitted,
              elapsedS: data?.elapsed_s,
              atS: data?.at ?? null,
              lastOutputAgeS: data?.last_output_age_s ?? null,
            },
          },
        },
      };
    }

    case "run_done": {
      if (!state.harness?.run) return state;
      // run_done carries the gate-computed outcome (incl. FAILED/ABORTED) — that
      // is a normal run result, not a system error, so the conversation returns
      // to "done" and the RunPanel renders the outcome. A finished run also has
      // no pending takeover (mirrors harness_site_done).
      return {
        ...state,
        status: "done",
        harness: {
          ...state.harness,
          takeover: undefined,
          run: {
            ...state.harness.run,
            status: "done",
            outcome: data?.outcome,
            recordCount: data?.record_count ?? state.harness.run.recordCount,
            reason: data?.reason,
            activity: undefined,
          },
        },
      };
    }

    case "run_error": {
      // A run-machinery error (not the discovery/explore error path): keep the
      // conversation "done" (explore succeeded) and surface it inside the run.
      if (!state.harness?.run) return state;
      return {
        ...state,
        status: "done",
        harness: {
          ...state.harness,
          takeover: undefined,
          run: { ...state.harness.run, status: "error", error: data?.message, activity: undefined },
        },
      };
    }

    case "run_output_ready": {
      if (!state.harness?.run) return state;
      const outputArtifact: ArtifactRef = {
        sessionId: state.sessionId ?? "",
        artifactId: data?.artifact_id,
        filename: data?.filename ?? "output.json",
        contentType: data?.content_type ?? "json",
        kind: "harness_run_output",
      };
      return {
        ...state,
        harness: {
          ...state.harness,
          run: {
            ...state.harness.run,
            outputArtifact,
            outputBytes: typeof data?.bytes === "number" ? data.bytes : undefined,
            recordCount: data?.record_count ?? state.harness.run.recordCount,
          },
        },
      };
    }

    case "run_field_doc_ready": {
      if (!state.harness?.run) return state;
      const fieldDocArtifact: ArtifactRef = {
        sessionId: state.sessionId ?? "",
        artifactId: data?.artifact_id,
        filename: data?.filename ?? "task_plan.md",
        contentType: data?.content_type ?? "markdown",
        kind: "harness_run_field_doc",
      };
      return {
        ...state,
        harness: { ...state.harness, run: { ...state.harness.run, fieldDocArtifact } },
      };
    }

    case "run_user_steer":
      // The user's mid-run message to the Run agent, echoed as a right bubble.
      return putBlock(state, {
        kind: "user_message",
        id: `rsteer-${seq}`,
        seq,
        text: data?.content ?? "",
      });

    default:
      return state;
  }
}

export function conversationReducer(state: ConversationState, action: Action): ConversationState {
  switch (action.type) {
    case "reset":
      return initialConversationState(action.sessionId);

    case "stream_status":
      // Don't let a late "connecting"/"live" clobber a terminal status.
      if ((state.status === "done" || state.status === "error") && action.status !== "error") {
        return state;
      }
      return { ...state, status: action.status };

    case "server_event": {
      const seq = action.seq != null ? Number(action.seq) : state.lastSeq + 1;
      if (seq <= state.lastSeq) return state; // dedup replay↔live overlap
      const next = applyServerEvent(state, action.eventType, action.data, seq);
      return { ...next, lastSeq: Math.max(next.lastSeq, seq) };
    }

    case "ui/set_view":
      return { ...state, activeView: action.view };

    case "ui/open_artifact":
      return { ...state, activeArtifact: action.artifact };

    case "ui/close_artifact":
      return { ...state, activeArtifact: null };

    // The takeover WS reported there is no live takeover behind the canvas
    // (replay of a run that died mid-takeover) — drop the stale window.
    case "ui/harness_takeover_dismiss": {
      if (!state.harness?.takeover) return state;
      return { ...state, harness: { ...state.harness, takeover: undefined } };
    }

    case "ui/steer_pending": {
      const b = state.blocks[action.blockId];
      if (!b || b.kind !== "task_description_box") return state;
      return putBlock(state, { ...b, pendingMessage: action.message, sendState: "sending" });
    }

    case "ui/steer_result": {
      const b = state.blocks[action.blockId];
      if (!b || b.kind !== "task_description_box") return state;
      return putBlock(state, { ...b, sendState: action.ok ? "sent" : "error" });
    }

    case "ui/harness_steer_pending": {
      if (!state.harness?.taskPlan) return state;
      return {
        ...state,
        harness: {
          ...state.harness,
          taskPlan: { ...state.harness.taskPlan, pendingMessage: action.message, sendState: "sending" },
        },
      };
    }

    case "ui/harness_steer_result": {
      if (!state.harness?.taskPlan) return state;
      return {
        ...state,
        harness: {
          ...state.harness,
          taskPlan: { ...state.harness.taskPlan, sendState: action.ok ? "sent" : "error" },
        },
      };
    }

    default:
      return state;
  }
}
