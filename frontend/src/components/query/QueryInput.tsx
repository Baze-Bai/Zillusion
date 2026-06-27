"use client";

import { useState, type FormEvent } from "react";

interface QueryInputProps {
  onSubmit: (query: string) => void;
  isLoading?: boolean;
}

export function QueryInput({ onSubmit, isLoading }: QueryInputProps) {
  const [query, setQuery] = useState("");

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (query.trim().length >= 5) {
      onSubmit(query.trim());
    }
  };

  return (
    <form onSubmit={handleSubmit} className="relative">
      <textarea
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        rows={3}
        className="w-full px-4 py-3 rounded-xl border border-border bg-card
                   text-foreground placeholder:text-muted-foreground
                   focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary
                   resize-none text-base"
        disabled={isLoading}
      />
      <div className="flex items-center justify-between mt-3">
        <span className="text-xs text-muted-foreground">
          {query.length < 5 ? `${5 - query.length} more characters needed` : "Ready to search"}
        </span>
        <button
          type="submit"
          disabled={isLoading || query.trim().length < 5}
          className="px-6 py-2 rounded-lg bg-primary text-primary-foreground font-medium
                     hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed
                     transition-opacity text-sm"
        >
          {isLoading ? "Discovering..." : "Discover Sources"}
        </button>
      </div>
    </form>
  );
}
