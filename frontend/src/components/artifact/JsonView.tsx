"use client";

export function JsonView({ content }: { content: string }) {
  let pretty = content;
  try {
    pretty = JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    /* show raw if not valid JSON */
  }
  return (
    <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed">{pretty}</pre>
  );
}
