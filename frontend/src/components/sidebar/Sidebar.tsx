"use client";

import { Plus } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";

import { useT } from "@/i18n";
import { useSessionList } from "@/state/SessionListContext";
import { SessionListItem } from "./SessionListItem";

export function Sidebar() {
  const { sessions, loading } = useSessionList();
  const { t } = useT();
  const pathname = usePathname();
  const router = useRouter();
  const activeMatch = pathname?.match(/^\/c\/([^/]+)/);
  const activeId = activeMatch ? decodeURIComponent(activeMatch[1]) : null;

  // Nest scraper child sessions under their discovery parent. Roots = sessions
  // with no parent, plus orphans whose parent isn't in the loaded list.
  const knownIds = new Set(sessions.map((s) => s.query_id));
  const childrenOf = (pid: string) => sessions.filter((s) => s.parent_query_id === pid);
  const roots = sessions.filter(
    (s) => !s.parent_query_id || !knownIds.has(s.parent_query_id),
  );

  return (
    <aside className="flex w-[260px] shrink-0 flex-col border-r border-border bg-card/40">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <img src="/logo-mark.svg" alt="" aria-hidden="true" className="h-6 w-6" />
        <span className="text-sm font-semibold">{t("sidebar.brand")}</span>
      </div>

      <div className="p-3">
        <button
          onClick={() => router.push("/")}
          className="flex w-full items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm transition-colors hover:bg-accent"
        >
          <Plus className="h-4 w-4" />
          {t("sidebar.newTask")}
        </button>
      </div>

      <nav className="flex-1 space-y-1 overflow-y-auto px-2 pb-4">
        {loading && sessions.length === 0 ? (
          <div className="px-2 py-1 text-xs text-muted-foreground">{t("common.loading")}</div>
        ) : null}
        {roots.map((s) => (
          <div key={s.query_id}>
            <SessionListItem session={s} active={activeId === s.query_id} />
            {childrenOf(s.query_id).map((c) => (
              <SessionListItem
                key={c.query_id}
                session={c}
                active={activeId === c.query_id}
                indented
              />
            ))}
          </div>
        ))}
        {!loading && sessions.length === 0 ? (
          <div className="px-2 py-1 text-xs text-muted-foreground">{t("sidebar.noTasks")}</div>
        ) : null}
      </nav>
    </aside>
  );
}
