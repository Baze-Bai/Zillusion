"use client";

import { MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { useT } from "@/i18n";
import { deleteSession, renameSession } from "@/lib/api";
import { useSessionList } from "@/state/SessionListContext";
import type { SessionSummary } from "@/types/transcript";

const STATUS_DOT: Record<string, string> = {
  running: "bg-blue-500 dark:bg-blue-400 animate-pulse",
  completed: "bg-emerald-500 dark:bg-emerald-400",
  error: "bg-red-500 dark:bg-red-400",
  interrupted: "bg-amber-500 dark:bg-amber-400",
  cancelled: "bg-muted-foreground",
  pending: "bg-muted-foreground",
};

export function SessionListItem({
  session,
  active,
  indented = false,
}: {
  session: SessionSummary;
  active: boolean;
  indented?: boolean;
}) {
  const { t } = useT();
  const router = useRouter();
  const { refresh } = useSessionList();
  const dot = STATUS_DOT[session.status] ?? "bg-muted-foreground";
  const subtitle =
    session.n_sources != null ? `${session.n_sources} sources` : session.status;
  const original = session.title || session.query_text;

  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(original);
  const [busy, setBusy] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const savingRef = useRef(false);

  // Close the menu on an outside click.
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [menuOpen]);

  const startRename = () => {
    setName(original);
    setRenaming(true);
    setMenuOpen(false);
  };

  const commitRename = async () => {
    if (savingRef.current) return;
    const next = name.trim();
    setRenaming(false);
    if (!next || next === original) return;
    savingRef.current = true;
    setBusy(true);
    try {
      await renameSession(session.query_id, next);
      await refresh();
    } catch {
      /* keep the old title on failure */
    } finally {
      savingRef.current = false;
      setBusy(false);
    }
  };

  const onDelete = async () => {
    setMenuOpen(false);
    if (!window.confirm(t("menu.deleteConfirm"))) return;
    setBusy(true);
    try {
      await deleteSession(session.query_id);
      await refresh();
      if (active) router.push("/"); // the open conversation just vanished
    } catch {
      /* ignore */
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      ref={rootRef}
      className={`group relative ${indented ? "ml-3 border-l border-border pl-1" : ""}`}
    >
      {renaming ? (
        <div className="px-2 py-1.5">
          <input
            autoFocus
            value={name}
            disabled={busy}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitRename();
              else if (e.key === "Escape") {
                setName(original);
                setRenaming(false);
              }
            }}
            onBlur={commitRename}
            className="w-full rounded border border-border bg-background px-2 py-1 text-sm outline-none focus:border-primary"
          />
        </div>
      ) : (
        <Link
          href={`/c/${session.query_id}`}
          className={`block rounded-md py-2 pl-2 pr-7 transition-colors hover:bg-accent/60 ${
            active ? "bg-accent" : ""
          }`}
        >
          <div className="flex items-center gap-2">
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
            <span className="flex-1 truncate text-sm">{original}</span>
          </div>
          <div className="mt-0.5 pl-3.5 text-[10px] capitalize text-muted-foreground">
            {subtitle}
          </div>
        </Link>
      )}

      {!renaming ? (
        <button
          type="button"
          aria-label="session menu"
          data-open={menuOpen}
          disabled={busy}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            setMenuOpen((o) => !o);
          }}
          className="absolute right-1 top-1.5 rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:bg-accent group-hover:opacity-100 data-[open=true]:opacity-100"
        >
          <MoreHorizontal className="h-4 w-4" />
        </button>
      ) : null}

      {menuOpen ? (
        <div className="absolute right-1 top-9 z-20 w-32 overflow-hidden rounded-md border border-border bg-card shadow-lg">
          <button
            type="button"
            onClick={startRename}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-accent"
          >
            <Pencil className="h-3.5 w-3.5" /> {t("menu.rename")}
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-red-600 dark:text-red-400 hover:bg-accent"
          >
            <Trash2 className="h-3.5 w-3.5" /> {t("menu.delete")}
          </button>
        </div>
      ) : null}
    </div>
  );
}
