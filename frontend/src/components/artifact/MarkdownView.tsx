"use client";

import { Markdown } from "@/components/common/Markdown";

export function MarkdownView({ content }: { content: string }) {
  return <Markdown>{content}</Markdown>;
}
