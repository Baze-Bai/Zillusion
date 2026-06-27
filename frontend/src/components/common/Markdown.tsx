"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Markdown renderer for agent narration, guides, and task descriptions.
 * Styled via arbitrary-variant utilities so it inherits the dark token theme
 * without the Tailwind typography plugin. Raw HTML is NOT rendered (no
 * rehype-raw) — safe by default.
 */
export function Markdown({ children }: { children: string }) {
  return (
    <div
      className="max-w-none text-sm leading-relaxed
        [&_a]:text-primary [&_a]:underline
        [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[0.85em]
        [&_h1]:mb-1 [&_h1]:mt-2 [&_h1]:text-base [&_h1]:font-semibold
        [&_h2]:mb-1 [&_h2]:mt-2 [&_h2]:text-sm [&_h2]:font-semibold
        [&_h3]:mb-1 [&_h3]:mt-2 [&_h3]:text-sm [&_h3]:font-semibold
        [&_li]:my-0.5
        [&_ol]:my-1 [&_ol]:list-decimal [&_ol]:pl-5
        [&_p]:my-1
        [&_pre]:my-1 [&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:bg-muted [&_pre]:p-2
        [&_ul]:my-1 [&_ul]:list-disc [&_ul]:pl-5"
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
