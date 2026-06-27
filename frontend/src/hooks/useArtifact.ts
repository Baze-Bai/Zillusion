"use client";

import { useEffect, useState } from "react";

import { fetchArtifact, type ArtifactContent } from "@/lib/api";
import type { ArtifactRef } from "@/types/transcript";

/** Fetch an artifact's content for the right-hand viewer (lazily, on open). */
export function useArtifact(ref: ArtifactRef | null) {
  const [data, setData] = useState<ArtifactContent | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!ref) {
      setData(null);
      setError(null);
      return;
    }
    // AbortController (not just a cancelled flag): closing the panel while a
    // multi-MB artifact is in flight should drop the network request itself,
    // not merely ignore its result.
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);
    fetchArtifact(ref.sessionId, ref.artifactId, controller.signal)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (cancelled || (e instanceof DOMException && e.name === "AbortError")) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [ref?.sessionId, ref?.artifactId]);

  return { data, loading, error };
}
