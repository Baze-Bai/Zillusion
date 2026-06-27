"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { LanguageToggle } from "@/components/layout/LanguageToggle";
import { ThemeToggle } from "@/components/layout/ThemeToggle";
import { Composer } from "@/components/transcript/Composer";
import { useT } from "@/i18n";
import { useSessionList } from "@/state/SessionListContext";

const EXAMPLES = [
  "US historical weather data with hourly resolution",
  "Chinese A-share market historical prices (2020–2024)",
  "Open datasets for NLP sentiment analysis",
  "World Bank economic indicators for Southeast Asia",
];

export default function HomePage() {
  const { createSession } = useSessionList();
  const { t } = useT();
  const router = useRouter();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const start = async (query: string) => {
    setSubmitting(true);
    setError(null);
    try {
      const id = await createSession(query);
      router.push(`/c/${id}`);
    } catch (e) {
      setSubmitting(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="relative flex flex-1 flex-col items-center justify-center px-6">
      <div className="absolute right-4 top-4 flex items-center gap-2">
        <ThemeToggle />
        <LanguageToggle />
      </div>
      <div className="mb-8 w-full max-w-2xl text-center">
        <h2 className="mb-3 text-3xl font-semibold tracking-tight">{t("home.title")}</h2>
        <p className="text-muted-foreground">{t("home.desc")}</p>
      </div>

      <div className="w-full max-w-2xl">
        <Composer onSubmit={start} disabled={submitting} autoFocus />
        {error ? <div className="mt-2 text-sm text-red-600 dark:text-red-400">{error}</div> : null}

        <div className="mt-6 grid gap-2 sm:grid-cols-2">
          {EXAMPLES.map((q) => (
            <button
              key={q}
              onClick={() => start(q)}
              disabled={submitting}
              className="rounded-lg border border-border px-3 py-2 text-left text-sm text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground disabled:opacity-50"
            >
              {q}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
