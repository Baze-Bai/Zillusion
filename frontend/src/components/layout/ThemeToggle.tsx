"use client";

import { Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

import { useT } from "@/i18n";

const STORAGE_KEY = "theme";

/** Dark ⇄ light switch. Shows the mode you'll switch TO (same convention as
 * LanguageToggle). The boot script in layout.tsx applies the stored theme
 * before hydration; the real value is read off <html> on mount (SSR and the
 * first client render assume dark, so they always match). */
export function ThemeToggle({ className }: { className?: string }) {
  const { t } = useT();
  const [dark, setDark] = useState(true);

  useEffect(() => {
    setDark(document.documentElement.classList.contains("dark"));
  }, []);

  const toggle = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    try {
      localStorage.setItem(STORAGE_KEY, next ? "dark" : "light");
    } catch {
      /* ignore */
    }
  };

  const label = t(dark ? "theme.light" : "theme.dark");
  return (
    <button
      onClick={toggle}
      title={label}
      aria-label={label}
      className={`inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs transition-colors hover:bg-accent ${className ?? ""}`}
    >
      {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
    </button>
  );
}
