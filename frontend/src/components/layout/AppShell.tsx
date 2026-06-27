"use client";

import { usePathname } from "next/navigation";

import { ArtifactPanel } from "@/components/artifact/ArtifactPanel";
import { Sidebar } from "@/components/sidebar/Sidebar";
import { LocaleProvider } from "@/i18n";
import { ConversationProvider } from "@/state/ConversationContext";
import { SessionListProvider } from "@/state/SessionListContext";

/** Derive the active session id from the URL (/c/{id}); null on the home route. */
export function sessionIdFromPath(pathname: string | null): string | null {
  if (!pathname) return null;
  const m = pathname.match(/^\/c\/([^/]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

/**
 * 3-pane shell: sidebar | conversation | artifact panel. The providers live
 * here (not in the pages) so all three panes share the same conversation +
 * session state. The ConversationProvider is keyed by sessionId, so navigating
 * between tasks remounts it cleanly (fresh reducer + stream).
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const sessionId = sessionIdFromPath(pathname);

  return (
    <LocaleProvider>
      <SessionListProvider>
        <ConversationProvider key={sessionId ?? "__new__"} sessionId={sessionId}>
          <div className="flex h-screen overflow-hidden">
            <Sidebar />
            <main className="flex min-w-0 flex-1 flex-col">{children}</main>
            <ArtifactPanel />
          </div>
        </ConversationProvider>
      </SessionListProvider>
    </LocaleProvider>
  );
}
