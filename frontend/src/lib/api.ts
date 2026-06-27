/**
 * Backend API client.
 *
 * Normal JSON requests (sessions, artifacts) can go through the Next proxy,
 * but the SSE stream MUST bypass it (the rewrite buffers chunked responses),
 * so it hits the backend directly via NEXT_PUBLIC_API_URL. The POST /discover
 * also goes direct so we can read the X-Query-Id response header (exposed via
 * CORS) without consuming the stream body — we re-attach through GET /stream.
 */

import type { ArtifactRef, SessionSummary } from "@/types/transcript";

const DIRECT = process.env.NEXT_PUBLIC_API_URL || "";

export interface SessionArtifact {
  id: number;
  site_id: string | null;
  kind: string;
  path: string;
  meta: Record<string, unknown> | null;
  created_at: string | null;
}

export interface SessionDetail extends SessionSummary {
  description: string | null;
  error: string | null;
  run_config: Record<string, unknown> | null;
  request_id: string | null;
  artifacts: SessionArtifact[];
}

export async function fetchSessions(): Promise<SessionSummary[]> {
  const r = await fetch(`/api/v1/sessions`, { cache: "no-store" });
  if (!r.ok) throw new Error(`GET /sessions failed: ${r.status}`);
  return r.json();
}

export async function fetchSession(queryId: string): Promise<SessionDetail> {
  const r = await fetch(`/api/v1/sessions/${encodeURIComponent(queryId)}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`GET /sessions/${queryId} failed: ${r.status}`);
  return r.json();
}

/** Rename a session (its sidebar title). */
export async function renameSession(queryId: string, title: string): Promise<void> {
  const r = await fetch(`/api/v1/sessions/${encodeURIComponent(queryId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!r.ok) throw new Error(`rename failed: ${r.status}`);
}

/** Delete a session (+ its scraper child sessions, events, artifacts). */
export async function deleteSession(queryId: string): Promise<void> {
  const r = await fetch(`/api/v1/sessions/${encodeURIComponent(queryId)}`, { method: "DELETE" });
  if (!r.ok) throw new Error(`delete failed: ${r.status}`);
}

/**
 * Start a discovery run. Returns the query_id (read from the X-Query-Id
 * response header) without consuming the SSE body — the caller navigates to
 * /c/{id} and attaches via streamUrl(). The detached run continues regardless.
 */
export async function createDiscovery(query: string): Promise<string> {
  const r = await fetch(`${DIRECT}/api/v1/discover`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!r.ok) {
    let detail = "";
    try {
      detail = await r.text();
    } catch {
      /* ignore */
    }
    throw new Error(`POST /discover failed: ${r.status} ${detail.slice(0, 200)}`);
  }
  const qid = r.headers.get("X-Query-Id");
  // We don't read the stream here — close it; the conversation page re-attaches.
  r.body?.cancel().catch(() => {});
  if (!qid) throw new Error("POST /discover did not return X-Query-Id");
  return qid;
}

/** URL for the live+replay SSE stream (bypasses the Next proxy). */
export function streamUrl(queryId: string, afterSeq = 0): string {
  const u = `${DIRECT}/api/v1/stream/${encodeURIComponent(queryId)}`;
  return afterSeq > 0 ? `${u}?after_seq=${afterSeq}` : u;
}

/** Phase 2: steer a live discovery run. */
export async function steerDiscovery(
  queryId: string,
  content: string,
  kind: "note" | "task_description_edit" = "note",
): Promise<void> {
  const r = await fetch(`/api/v1/discover/${encodeURIComponent(queryId)}/steer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, content }),
  });
  if (!r.ok) throw new Error(`steer failed: ${r.status}`);
}

/** Artifact content for the right-hand viewer (Phase 3). */
export function artifactUrl(queryId: string, artifactId: number): string {
  return `/api/v1/sessions/${encodeURIComponent(queryId)}/artifacts/${artifactId}`;
}

export interface ArtifactContent {
  id: number;
  kind: string;
  site_id: string | null;
  filename: string;
  content_type: "json" | "markdown" | "csv" | "text";
  content: string;
  meta: Record<string, unknown> | null;
}

export async function fetchArtifact(
  queryId: string,
  artifactId: number,
  signal?: AbortSignal,
): Promise<ArtifactContent> {
  const r = await fetch(artifactUrl(queryId, artifactId), { cache: "no-store", signal });
  if (!r.ok) throw new Error(`GET artifact failed: ${r.status}`);
  return r.json();
}

/** Raw attachment download for an artifact (a full crawl's output.json can be
 * multi-MB — served as a file, not via the JSON viewer endpoint). Direct when
 * NEXT_PUBLIC_API_URL is set, so big files don't funnel through the Next dev
 * proxy; the backend's Content-Disposition forces the download either way. */
export function artifactDownloadUrl(queryId: string, artifactId: number): string {
  return `${DIRECT}/api/v1/sessions/${encodeURIComponent(queryId)}/artifacts/${artifactId}/download`;
}

/** Phase 3: start the harness (scraper-builder) on a session's sources. */
export async function startHarness(
  queryId: string,
  opts: { site_ids?: string[]; max_iters?: number; max_cost_usd?: number; model?: string } = {},
): Promise<{ accepted: boolean; child_query_ids: string[] }> {
  const r = await fetch(`/api/v1/harness/${encodeURIComponent(queryId)}/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts),
  });
  if (!r.ok) {
    const t = await r.text().catch(() => "");
    throw new Error(`harness start failed: ${r.status} ${t.slice(0, 160)}`);
  }
  return r.json();
}

/**
 * Run phase: start the production crawl (the VALIDATED workflow.py at full
 * scope) for a PASSed scraper child. The run continues the SAME child session
 * timeline, so the caller just re-opens the stream to pick up its run_* events.
 */
export async function startRun(
  siteId: string,
  opts: {
    crawl_mode?: string;
    wall_clock_cap_s?: number;
    stall_timeout_s?: number;
    model?: string;
    /** User-owned override: run despite an INCONCLUSIVE loop verdict. */
    force?: boolean;
  } = {},
): Promise<{ accepted: boolean; site_id: string }> {
  const r = await fetch(`/api/v1/harness/${encodeURIComponent(siteId)}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts),
  });
  if (!r.ok) {
    const t = await r.text().catch(() => "");
    throw new Error(`run start failed: ${r.status} ${t.slice(0, 160)}`);
  }
  return r.json();
}

/**
 * WebSocket URL for the human-takeover embedded browser (login / captcha /
 * challenge). Derived from NEXT_PUBLIC_API_URL (http→ws) so it hits the backend
 * directly, bypassing the Next proxy — same rationale as the SSE stream.
 */
export function takeoverWsUrl(siteId: string): string {
  const base = DIRECT || (typeof window !== "undefined" ? window.location.origin : "");
  const ws = base.replace(/^http/, "ws");
  return `${ws}/api/v1/harness/${encodeURIComponent(siteId)}/takeover`;
}

/** Key-gated api site: submit the user-obtained API key. The backend writes
 * inputs/<site>/credentials.json and relaunches the explore→validate loop;
 * its events continue this child session's stream. The key is sent once and
 * never echoed back. */
export async function submitHarnessCredentials(
  siteId: string,
  apiKey: string,
  extra: Record<string, string> = {},
): Promise<{ accepted: boolean; launched: boolean; note?: string }> {
  const r = await fetch(`/api/v1/harness/${encodeURIComponent(siteId)}/credentials`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey, extra }),
  });
  if (!r.ok) {
    const t = await r.text().catch(() => "");
    throw new Error(`credentials submit failed: ${r.status} ${t.slice(0, 160)}`);
  }
  return r.json();
}

/** Phase 3: steer a live harness site (file-inbox; applies next iteration). */
export async function steerHarness(siteId: string, queryId: string, content: string): Promise<void> {
  const r = await fetch(`/api/v1/harness/${encodeURIComponent(siteId)}/steer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query_id: queryId, content }),
  });
  if (!r.ok) throw new Error(`harness steer failed: ${r.status}`);
}

/** Run phase: steer a LIVE production-crawl run — a mid-run message to the Run
 * agent. Appended to user_run_steering.md, which the run agent tails (~1s). */
export async function steerRun(siteId: string, content: string): Promise<void> {
  const r = await fetch(`/api/v1/harness/${encodeURIComponent(siteId)}/run/steer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (!r.ok) throw new Error(`run steer failed: ${r.status}`);
}

export type { ArtifactRef };
