"use client";

import { Markdown } from "@/components/common/Markdown";
import { useT } from "@/i18n";
import type { SendState } from "@/types/transcript";

import { SteerableBox } from "./SteerableBox";

export function HarnessTaskPlanBox({
  plan,
}: {
  plan: { text: string; version: number; pendingMessage?: string; sendState: SendState };
}) {
  const { t } = useT();
  // Display only — the user steers via the bottom conversation composer now.
  return (
    <SteerableBox
      title={t("taskPlan.title")}
      body={
        plan.text ? (
          <Markdown>{plan.text}</Markdown>
        ) : (
          <span className="text-muted-foreground">{t("scraperPlan.empty")}</span>
        )
      }
    />
  );
}
