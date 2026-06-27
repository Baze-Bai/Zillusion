import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/layout/AppShell";

export const metadata: Metadata = {
  title: "DataSource Discovery Agent",
  description: "Discover APIs, datasets, and embedded data sources — steerable, conversational.",
  icons: {
    icon: "/logo-mark.svg",
    shortcut: "/logo-mark.svg",
    apple: "/logo-mark.svg",
  },
};

// Inline + parser-blocking (first thing in <body>) so the stored preference is
// applied before first paint — no dark→light flash. Anything other than a
// stored "light" keeps the dark default.
const THEME_BOOT = `try{if(localStorage.getItem("theme")==="light")document.documentElement.classList.remove("dark")}catch(e){}`;

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Dark by default to match the Claude-Code-web feel. The `.dark` tokens are
  // defined in globals.css; ThemeToggle + THEME_BOOT swap the class, hence
  // suppressHydrationWarning on <html>.
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <body className="bg-background text-foreground antialiased">
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT }} />
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
