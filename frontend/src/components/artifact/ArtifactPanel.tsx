"use client";

import { X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { useArtifact } from "@/hooks/useArtifact";
import { useT } from "@/i18n";
import { useConversation } from "@/state/ConversationContext";

import { ArtifactRenderer } from "./ArtifactRenderer";

const MIN_W = 320;
const MAX_W = 900;
const DEFAULT_W = 460;
const STORAGE_KEY = "artifactPanelWidth";

/** Right-hand artifact viewer — opens when a file chip is clicked. The left
 * edge is a drag handle to resize the panel; the width persists (point 4). */
export function ArtifactPanel() {
  const { state, closeArtifact } = useConversation();
  const { t } = useT();
  const ref = state.activeArtifact;
  const { data, loading, error } = useArtifact(ref);

  const [width, setWidth] = useState<number>(DEFAULT_W);
  const widthRef = useRef(width);
  widthRef.current = width;

  // Restore the persisted width on mount (client-only).
  useEffect(() => {
    const saved = Number(localStorage.getItem(STORAGE_KEY));
    if (saved && saved >= MIN_W && saved <= MAX_W) setWidth(saved);
  }, []);

  const startDrag = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = widthRef.current;
    // Panel sits on the right, so dragging the handle left (smaller clientX)
    // widens it.
    const onMove = (ev: PointerEvent) => {
      setWidth(Math.min(MAX_W, Math.max(MIN_W, startW + (startX - ev.clientX))));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      document.body.style.userSelect = "";
      try {
        localStorage.setItem(STORAGE_KEY, String(widthRef.current));
      } catch {
        /* ignore */
      }
    };
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }, []);

  if (!ref) return null;

  return (
    <aside
      style={{ width }}
      className="relative flex shrink-0 flex-col border-l border-border bg-card/30"
    >
      {/* Drag handle — left edge */}
      <div
        onPointerDown={startDrag}
        title={t("panel.resize")}
        className="absolute -left-1 top-0 z-10 h-full w-2 cursor-col-resize transition-colors hover:bg-primary/40"
      />
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-border px-3">
        <span className="truncate text-sm font-medium">{ref.filename}</span>
        <button
          onClick={closeArtifact}
          aria-label="Close"
          className="text-muted-foreground hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="flex-1 overflow-auto p-3">
        {loading ? <div className="text-sm text-muted-foreground">{t("common.loading")}</div> : null}
        {error ? <div className="text-sm text-red-600 dark:text-red-400">{error}</div> : null}
        {data ? <ArtifactRenderer artifact={data} /> : null}
      </div>
    </aside>
  );
}
