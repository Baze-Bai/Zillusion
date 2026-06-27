"use client";

/**
 * Inline display card for a "steerable" artifact — the discovery task
 * description and the harness task_plan. Steering now happens through the bottom
 * conversation composer (web-Claude style), so this box is read-only: a title,
 * the rendered body, and an optional footer note. (The previous inline "Guide"
 * edit affordance was removed in favor of the composer.)
 */
export function SteerableBox({
  title,
  body,
  footer,
}: {
  title: React.ReactNode;
  body: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-border bg-card/60 p-3">
      <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      <div className="text-sm">{body}</div>
      {footer ? <div className="mt-2 text-[11px] text-muted-foreground">{footer}</div> : null}
    </div>
  );
}
