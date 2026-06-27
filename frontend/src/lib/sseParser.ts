/**
 * Spec-compliant SSE byte parser (extracted from the since-removed useSSE.ts;
 * this is the hardened version that fixes three real bugs: CRLF line endings
 * from sse-starlette, event/data fields split across TCP chunks, and the
 * trailing event with no terminating blank line). Extended to also capture
 * the `id:` field (our monotonic per-event `seq`), which the conversation
 * reducer uses to dedup replay↔live overlap and to resume via Last-Event-ID.
 *
 * Pure transport: feeds (eventType, parsedData, id) to `onEvent`. No React.
 */

export type SSEHandler = (eventType: string, data: unknown, id: string | null) => void;

export async function parseSSEStream(
  response: Response,
  onEvent: SSEHandler,
  signal?: AbortSignal,
): Promise<void> {
  if (!response.body) throw new Error("No response body");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // Event-scoped state MUST live outside the read loop — TCP chunks can split
  // anywhere, and resetting per-chunk silently drops events whose `event:` and
  // `data:` lines land in different chunks.
  let eventType = "";
  let dataLines: string[] = [];
  let lastEventId: string | null = null;

  const dispatchPending = () => {
    if (dataLines.length === 0) return;
    const type = eventType || "message";
    const raw = dataLines.join("\n");
    let parsed: unknown = raw;
    try {
      parsed = JSON.parse(raw);
    } catch (err) {
      console.warn("[SSE] JSON.parse failed", { type, raw, err });
    }
    onEvent(type, parsed, lastEventId);
    eventType = "";
    dataLines = [];
    // lastEventId persists across events per the SSE spec (used for resume).
  };

  const stripFieldPrefix = (line: string, field: string): string | null => {
    if (!line.startsWith(field)) return null;
    const rest = line.slice(field.length);
    if (rest.startsWith(": ")) return rest.slice(2);
    if (rest.startsWith(":")) return rest.slice(1);
    return null;
  };

  const onAbort = () => {
    reader.cancel().catch(() => {});
  };
  if (signal) {
    if (signal.aborted) {
      await reader.cancel().catch(() => {});
      return;
    }
    signal.addEventListener("abort", onAbort, { once: true });
  }

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        dispatchPending(); // flush a trailing event with no blank-line terminator
        break;
      }

      buffer += decoder.decode(value, { stream: true });
      // SSE allows \n, \r\n, or \r terminators; sse-starlette emits \r\n.
      buffer = buffer.replace(/\r\n?/g, "\n");
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (line === "") {
          dispatchPending();
          continue;
        }
        if (line.startsWith(":")) {
          continue; // comment / keep-alive (e.g. ": ping")
        }
        const eventField = stripFieldPrefix(line, "event");
        if (eventField !== null) {
          eventType = eventField.trim();
          continue;
        }
        const dataField = stripFieldPrefix(line, "data");
        if (dataField !== null) {
          dataLines.push(dataField);
          continue;
        }
        const idField = stripFieldPrefix(line, "id");
        if (idField !== null) {
          lastEventId = idField.trim();
          continue;
        }
        // retry: / unknown fields — ignore.
      }
    }
  } finally {
    if (signal) signal.removeEventListener("abort", onAbort);
  }
}
