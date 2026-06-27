"use client";

import { ExternalLink, KeyRound } from "lucide-react";
import { useState } from "react";

import { useT } from "@/i18n";
import { submitHarnessCredentials } from "@/lib/api";
import type { AwaitingCredentialsState } from "@/types/transcript";

/**
 * Key-entry card for a key-gated api site: the backend staged the source but
 * won't explore until the user supplies the API key (the agent never signs up
 * for keys itself). Shows the discovered signup guidance, takes the key, and
 * POSTs it — the backend writes inputs/<site>/credentials.json and relaunches
 * the loop, whose harness_site_started event clears this card via the reducer.
 */
export function CredentialsGate({
  siteId,
  awaiting,
}: {
  siteId: string;
  awaiting: AwaitingCredentialsState;
}) {
  const { t } = useT();
  const [apiKey, setApiKey] = useState("");
  const [phase, setPhase] = useState<"idle" | "submitting" | "launching">("idle");
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async () => {
    const key = apiKey.trim();
    if (!key) return;
    setPhase("submitting");
    setError(null);
    try {
      const res = await submitHarnessCredentials(siteId, key);
      // Launched: the relaunched loop's events clear this card shortly. Not
      // launched (e.g. no resolvable child session): keep the note visible.
      setPhase("launching");
      if (!res.launched && res.note) setError(res.note);
    } catch (e) {
      setPhase("idle");
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const busy = phase !== "idle";

  return (
    <div className="space-y-3 rounded-xl border border-amber-300/60 bg-amber-50/50 p-4 dark:border-amber-400/30 dark:bg-amber-500/5">
      <div className="flex items-center gap-2 text-sm font-medium">
        <KeyRound className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
        {t("credgate.title")}
      </div>
      <p className="text-xs text-muted-foreground">{t("credgate.why")}</p>

      {/* Signup guidance ladder — precision degrades gracefully, never blank.
          T1: a precise signup_url (link + steps). T2/T3: no precise page, so
          point the user at the provider's site (+ auth type / docs when known)
          and let them find the Developer / API / Sign up entry. */}
      {awaiting.signupUrl ? (
        <>
          <a
            href={awaiting.signupUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            {t("credgate.signup")}
          </a>
          {awaiting.signupInstructions ? (
            <p className="text-xs text-muted-foreground">{awaiting.signupInstructions}</p>
          ) : null}
          {awaiting.signupSteps?.length ? (
            <ol className="list-decimal space-y-0.5 pl-5 text-xs text-muted-foreground">
              {awaiting.signupSteps.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ol>
          ) : null}
        </>
      ) : (
        <div className="space-y-1 text-xs text-muted-foreground">
          <p>{t("credgate.fallback.lead")}</p>
          {awaiting.authType && awaiting.authType !== "unknown" ? (
            <p>
              {t("credgate.fallback.auth")}: <span className="font-mono">{awaiting.authType}</span>
            </p>
          ) : null}
          {awaiting.sourceUrl ? (
            <a
              href={awaiting.sourceUrl}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-primary hover:underline"
            >
              <ExternalLink className="h-3.5 w-3.5" />
              {t("credgate.fallback.visit")}
              {awaiting.provider ? ` — ${awaiting.provider}` : ""}
            </a>
          ) : null}
          {awaiting.docsUrl ? (
            <a
              href={awaiting.docsUrl}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-primary hover:underline"
            >
              <ExternalLink className="h-3.5 w-3.5" />
              {t("credgate.fallback.docs")}
            </a>
          ) : null}
        </div>
      )}

      <div className="space-y-1.5">
        <label htmlFor={`credgate-key-${siteId}`} className="block text-xs font-medium">
          {t("credgate.keyLabel")}
        </label>
        <div className="flex gap-2">
          <input
            id={`credgate-key-${siteId}`}
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !busy) void onSubmit();
            }}
            disabled={busy}
            className="min-w-0 flex-1 rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
          />
          <button
            onClick={() => void onSubmit()}
            disabled={busy || !apiKey.trim()}
            className="shrink-0 rounded-lg border border-border bg-accent/40 px-4 py-2 text-sm font-medium transition-colors hover:bg-accent disabled:opacity-50"
          >
            {phase === "launching" ? t("credgate.launching") : t("credgate.submit")}
          </button>
        </div>
        <p className="text-[11px] text-muted-foreground">{t("credgate.storage")}</p>
        {error ? (
          <p className="text-[11px] text-red-600 dark:text-red-400">{error}</p>
        ) : null}
      </div>
    </div>
  );
}
