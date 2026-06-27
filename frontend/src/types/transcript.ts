/**
 * Transcript model — the conversation is a reduction over an ordered event
 * log. Each block has a stable `id` (for in-place reconciliation + React key)
 * and the `seq` of the event that last touched it. The SAME reducer drives
 * live streaming and review/replay.
 */

import type { DataSource } from "./data-source";

export type SendState = "idle" | "sending" | "sent" | "error";

export type ArtifactContentType = "json" | "markdown" | "csv" | "text";

export interface ArtifactRef {
  sessionId: string;
  artifactId: number;
  filename: string;
  contentType: ArtifactContentType;
  kind?: string;
}

interface BlockBase {
  id: string; // stable; used for in-place reconciliation & React key
  seq: number; // event-log ordinal of the last event that touched this block
}

export interface UserMessageBlock extends BlockBase {
  kind: "user_message";
  text: string;
}

export interface AgentTextBlock extends BlockBase {
  kind: "agent_text";
  markdown: string;
}

/** A deliberate message the agent SENT to the user via its send_user_message
 * tool (discovery `agent_message` / harness `harness_agent_message`). Unlike the
 * ephemeral `agent_text` narration, these are PERSISTED and rendered as
 * left-aligned chat bubbles in the conversation section (web-Claude style). */
export interface AgentMessageBlock extends BlockBase {
  kind: "agent_message";
  markdown: string;
  /** Event creation time (ISO, from the payload). Lets the renderer give a
   * typewriter reveal to messages that just arrived while replayed history
   * renders instantly. Absent on events whose producer doesn't stamp it. */
  createdAt?: string | null;
}

export interface StageMarkerBlock extends BlockBase {
  kind: "stage_marker";
  stage: string;
  status: "active" | "complete";
  durationMs?: number;
}

export interface TaskDescriptionBlock extends BlockBase {
  kind: "task_description_box";
  text: string;
  version: number; // bumped on each task_description_committed
  status: "live" | "sealed";
  pendingMessage?: string; // optimistic guidance awaiting the next commit
  sendState: SendState;
}

/** A pending human-takeover: the harness hit a login / captcha / challenge wall
 * and is streaming its headed login browser into this sub-session for the user
 * to operate. `message` is agent-authored markdown (why + how to take over). */
export interface TakeoverState {
  active: boolean;
  reason?: string;
  message?: string;
  pageUrl?: string;
  wallType?: string; // login | login_modal | captcha | challenge
}

/** The Run (production crawl) phase: the VALIDATED workflow.py executing at full
 * scope to produce + keep the real dataset. Populated only after the user clicks
 * "Run" on a scraper child whose explore→validate loop ended in PASS. Lives under
 * `HarnessState.run` so it shares the same child page + source card. */
export interface RunState {
  status: "running" | "done" | "error";
  runId?: string;
  crawlMode?: string;
  /** Gate-computed outcome (COMPLETE | PARTIAL | FAILED | ABORTED) at run_done. */
  outcome?: string;
  /** Final record count + one-line reason, parsed from the outcome line. */
  recordCount?: number;
  reason?: string;
  /** Live crawl progress, relayed from runs/<run_id>/_run_progress.json (~3s). */
  progress?: {
    state: string; // starting | running | done | killed | error
    recordCount?: number | null;
    linesEmitted?: number;
    elapsedS?: number;
    /** Server wall-clock (unix s) when this snapshot was published — lets the
     * client tick elapsed accurately even across a replay gap. */
    atS?: number | null;
    lastOutputAgeS?: number | null;
  };
  /** Live agent narration (mirrors the explore/validate activity line). */
  activity?: { phase: "thinking" | "executing"; text: string };
  error?: string;
  /** The kept dataset (runs/<run_id>/output.json), downloadable once the
   * backend registers it at run end. */
  outputArtifact?: ArtifactRef;
  outputBytes?: number;
  /** Field documentation snapshot (task_plan.md as it stood at run START —
   * the source doc behind the 目标数据与字段 table), downloadable alongside
   * the dataset. Registered when the run starts. */
  fieldDocArtifact?: ArtifactRef;
}

/** A key-gated api site waiting for the user's API key: the signup guidance
 * surfaced by `harness_site_awaiting_credentials`, rendered as the
 * CredentialsGate card. Cleared the moment the loop (re)starts. */
export interface AwaitingCredentialsState {
  authType?: string;
  signupUrl?: string;
  signupInstructions?: string;
  signupSteps?: string[];
  writeTo?: string;
  /** Provider name + homepage + docs — the fallback ladder (tier 2/3) when
   * there's no precise signupUrl: point the user at the provider's site. */
  provider?: string;
  sourceUrl?: string;
  docsUrl?: string;
}

/** A harness (scraper) run's live state. In the child-session model this is a
 * TOP-LEVEL field of the conversation (one scraper session = one HarnessState),
 * not nested under a source card. */
export interface HarnessState {
  siteId: string;
  status: "running" | "done" | "awaiting_credentials";
  taskPlan?: { text: string; version: number; pendingMessage?: string; sendState: SendState };
  /** Per-iteration history. `build` says WHICH (re)start of the explore loop
   * the iteration belongs to — iteration numbers reset to 1 on every restart
   * (a steer on an idle scraper auto-restarts it), so without it two
   * "iter 1" rows from different builds look like duplicates. */
  iterations: { n: number; note: string; build: number }[];
  /** How many times the explore→validate loop has (re)started — incremented on
   * every harness_site_started. Stamps new iteration rows with their build. */
  builds?: number;
  sampleArtifact?: ArtifactRef;
  sourceName?: string;
  sourceUrl?: string;
  /** Full discovered source — renders the source card on the child page. */
  source?: Partial<DataSource>;
  /** Active human-takeover (login/captcha/challenge), or undefined. */
  takeover?: TakeoverState;
  /** Live "working" indicator (mirrors discovery's ephemeral activity): the
   * agent's latest turn during explore/validate. Undefined when idle/done. */
  activity?: { phase: "thinking" | "executing"; stage: "explore" | "validate"; text: string };
  /** Final verdict of the explore→validate loop, captured at harness_site_done.
   * Gates the "Run" button (primary on PASS; INCONCLUSIVE gets a "run anyway"
   * override; FAIL/no-verdict stay locked, with the reason shown in the
   * button's place). Reset to undefined on a (re)start so a re-running loop
   * hides the button until it finishes again. */
  finalVerdict?: string;
  /** Why the loop exited (harness_site_done.exit_reason, e.g. "stuck (last 2
   * iters' next_strategy too similar)") — shown with the non-PASS gate note. */
  exitReason?: string;
  /** The Run (production crawl) phase, once the user starts it. Undefined until
   * "Run" is clicked; reset on a (re)start of the explore→validate loop. */
  run?: RunState;
  /** Key-gated api site: signup guidance for the key-entry card, until the
   * user submits credentials and the loop launches. */
  awaitingCredentials?: AwaitingCredentialsState;
}

export interface SourceBlock extends BlockBase {
  kind: "source_box";
  source: Partial<DataSource>;
}

export interface FileChipBlock extends BlockBase {
  kind: "file_chip";
  artifact: ArtifactRef;
  label?: string;
}

export interface ErrorBlock extends BlockBase {
  kind: "error";
  message: string;
}

export type TranscriptBlock =
  | UserMessageBlock
  | AgentTextBlock
  | AgentMessageBlock
  | StageMarkerBlock
  | TaskDescriptionBlock
  | SourceBlock
  | FileChipBlock
  | ErrorBlock;

/** Sidebar list item (from GET /api/v1/sessions). */
export interface SessionSummary {
  query_id: string;
  /** Parent discovery session when this is a harness/scraper sub-session. */
  parent_query_id?: string | null;
  /** For a scraper child session: the discovery source id it was built from. */
  source_id?: string | null;
  title: string;
  query_text: string;
  status: string;
  created_at: string | null;
  finished_at: string | null;
  n_sources: number | null;
  total_cost_usd: number | null;
}
