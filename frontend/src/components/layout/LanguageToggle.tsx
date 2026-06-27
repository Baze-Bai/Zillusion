"use client";

import { Languages } from "lucide-react";

import { useT } from "@/i18n";

/** EN ⇄ 中文 switch (point 9). Shows the language you'll switch TO. */
export function LanguageToggle({ className }: { className?: string }) {
  const { locale, toggle } = useT();
  return (
    <button
      onClick={toggle}
      title="中文 / English"
      aria-label="Toggle language"
      className={`inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs transition-colors hover:bg-accent ${className ?? ""}`}
    >
      <Languages className="h-3.5 w-3.5" />
      {locale === "en" ? "中文" : "EN"}
    </button>
  );
}
