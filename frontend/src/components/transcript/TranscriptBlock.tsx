"use client";

import { memo } from "react";

import { AlertTriangle } from "lucide-react";

import { Markdown } from "@/components/common/Markdown";
import type { TranscriptBlock } from "@/types/transcript";

import { FileChip } from "./FileChip";
import { SourceBox } from "./SourceBox";
import { TaskDescriptionBox } from "./TaskDescriptionBox";
import { TypewriterMarkdown } from "./TypewriterMarkdown";

// memo: the transcript renders every block on every SSE event (each event
// recreates the context value). Block objects keep a stable identity in the
// reducer unless that specific block changed, so reference equality is the
// right re-render gate — a thousand-event session re-renders only the block
// that moved instead of the whole list.
export const TranscriptBlockView = memo(function TranscriptBlockView({
  block,
}: {
  block: TranscriptBlock;
}) {
  switch (block.kind) {
    case "user_message":
      return (
        <div className="flex justify-end">
          <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl bg-primary px-4 py-2 text-sm text-primary-foreground">
            {block.text}
          </div>
        </div>
      );

    case "agent_message":
      return (
        <div className="flex justify-start">
          <div className="max-w-[80%] rounded-2xl bg-muted px-4 py-2 text-sm text-foreground">
            <TypewriterMarkdown markdown={block.markdown} createdAt={block.createdAt} />
          </div>
        </div>
      );

    case "task_description_box":
      return <TaskDescriptionBox block={block} />;

    case "source_box":
      return <SourceBox block={block} />;

    case "file_chip":
      return <FileChip artifact={block.artifact} label={block.label} />;

    case "error":
      return (
        <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-700 dark:text-red-300">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{block.message}</span>
        </div>
      );

    // agent_text & stage_marker are no longer persisted — they feed the
    // ephemeral ActivityIndicator instead (see conversationReducer).
    default:
      return null;
  }
});
