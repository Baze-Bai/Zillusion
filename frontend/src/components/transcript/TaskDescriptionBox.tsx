"use client";

import { Markdown } from "@/components/common/Markdown";
import { useT } from "@/i18n";
import type { TaskDescriptionBlock } from "@/types/transcript";

import { SteerableBox } from "./SteerableBox";

export function TaskDescriptionBox({ block }: { block: TaskDescriptionBlock }) {
  const { t } = useT();
  // Display only — the user steers via the bottom conversation composer now.
  return (
    <SteerableBox
      title={t("taskPlan.title")}
      body={
        block.text ? (
          <Markdown>{block.text}</Markdown>
        ) : (
          <span className="text-muted-foreground">{t("taskPlan.empty")}</span>
        )
      }
    />
  );
}
