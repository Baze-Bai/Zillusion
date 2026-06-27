"use client";

import { FileJson, FileText, Table } from "lucide-react";

import { useConversationActions } from "@/state/ConversationContext";
import type { ArtifactRef } from "@/types/transcript";

export function FileChip({ artifact, label }: { artifact: ArtifactRef; label?: string }) {
  // Actions-only subscription: a chip never reads state, and the old merged
  // context re-rendered every chip on every SSE event (piercing the
  // transcript block memo).
  const { openArtifact } = useConversationActions();
  const ct = artifact.contentType;
  const Icon = ct === "json" ? FileJson : ct === "csv" ? Table : FileText;

  return (
    <button
      onClick={() => openArtifact(artifact)}
      className="inline-flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-sm transition-colors hover:border-primary/40"
    >
      <Icon className="h-4 w-4 text-primary" />
      <span className="truncate">{label || artifact.filename}</span>
    </button>
  );
}
