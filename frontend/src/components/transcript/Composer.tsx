"use client";

import { ArrowUp } from "lucide-react";
import { useState } from "react";

export function Composer({
  onSubmit,
  disabled,
  autoFocus,
  minChars = 5,
}: {
  onSubmit: (value: string) => void;
  disabled?: boolean;
  autoFocus?: boolean;
  minChars?: number;
}) {
  const [value, setValue] = useState("");
  const canSend = value.trim().length >= minChars && !disabled;

  const submit = () => {
    if (!canSend) return;
    onSubmit(value.trim());
    setValue("");
  };

  return (
    <div className="flex items-end gap-2 rounded-xl border border-border bg-card p-2 shadow-sm">
      <textarea
        autoFocus={autoFocus}
        rows={1}
        value={value}
        disabled={disabled}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
        className="max-h-40 flex-1 resize-none bg-transparent px-2 py-2 text-sm outline-none"
      />
      <button
        onClick={submit}
        disabled={!canSend}
        aria-label="Send"
        className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-primary text-primary-foreground transition-opacity disabled:opacity-40"
      >
        <ArrowUp className="h-4 w-4" />
      </button>
    </div>
  );
}
