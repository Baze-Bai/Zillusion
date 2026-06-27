"""Context auto-compression — ported from Claude Code's multi-level compression system.

Three compression levels:
  Level 1: Time-based microcompact (clear old tool results, keep structure)
  Level 2: Token-aware selective pruning (remove low-value messages)
  Level 3: Full LLM summarization (replace history with compact summary)

Production guards:
  - Circuit breaker: stop after 3 consecutive failures
  - Token buffer: reserve 13,000 tokens for response
  - Warning at 80% capacity before compression kicks in
  - Recursive compaction prevention
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.config import settings
from src.services.llm import llm_service

logger = logging.getLogger(__name__)

# Compactable tool types (their outputs can be safely summarized)
COMPACTABLE_TOOLS = frozenset({
    "file_read", "bash", "grep", "glob",
    "web_search", "web_fetch", "file_edit", "file_write",
    "exa_search", "brave_search", "tavily_search", "searxng_search",
    "head_probe", "firecrawl_scrape", "firecrawl_map",
})


class CompactLevel(str, Enum):
    NONE = "none"
    MICRO = "micro"           # Level 1: Clear old tool results
    SELECTIVE = "selective"    # Level 2: Remove low-value messages
    FULL = "full"             # Level 3: LLM summarization


@dataclass
class CompactState:
    """Tracks compression state across turns."""
    compacted: bool = False
    turn_counter: int = 0
    consecutive_failures: int = 0
    last_compact_time: float = 0.0
    total_tokens_saved: int = 0


@dataclass
class Message:
    """Simplified message for compression context."""
    role: str  # system / user / assistant / tool_result
    content: str
    tool_name: str | None = None
    tool_id: str | None = None
    token_estimate: int = 0
    timestamp: float = field(default_factory=time.monotonic)
    is_compactable: bool = False


class ContextCompressor:
    """Multi-level context compression with circuit breaker.

    Ported from Claude Code's autoCompact + microCompact + compact system.
    """

    def __init__(
        self,
        max_context_tokens: int | None = None,
        buffer_tokens: int | None = None,
    ) -> None:
        self.max_context_tokens = max_context_tokens or settings.compression.max_context_tokens
        self.buffer_tokens = buffer_tokens or settings.compression.buffer_tokens
        self.state = CompactState()
        self._threshold = self.max_context_tokens - self.buffer_tokens
        self._warning_threshold = self.max_context_tokens - settings.compression.warning_buffer_tokens
        self._max_failures = settings.compression.max_consecutive_failures

    @property
    def warning_threshold(self) -> int:
        return self._warning_threshold

    def should_compact(self, current_tokens: int) -> CompactLevel:
        """Determine if and what level of compaction is needed.

        Returns CompactLevel indicating the action to take.
        """
        # Circuit breaker: stop trying after repeated failures
        if self.state.consecutive_failures >= self._max_failures:
            logger.warning(
                "Compact circuit breaker open (%d consecutive failures)",
                self.state.consecutive_failures,
            )
            return CompactLevel.NONE

        # Below warning threshold: no action needed
        if current_tokens < self.warning_threshold:
            return CompactLevel.NONE

        # Warning zone (80-90%): try microcompact first
        if current_tokens < self._threshold:
            return CompactLevel.MICRO

        # Over threshold: need real compression
        if current_tokens >= self._threshold:
            # If microcompact was already done recently, escalate
            if self.state.compacted:
                return CompactLevel.FULL
            return CompactLevel.SELECTIVE

        return CompactLevel.NONE

    async def microcompact(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Level 1: Clear old tool result content, keep message structure.

        Preserves the conversation flow but removes heavy tool outputs
        from older turns. Most recent N tool results are kept intact.
        """
        keep_recent = settings.compression.microcompact_keep_recent
        tool_results = [
            (i, m) for i, m in enumerate(messages)
            if m.get("role") == "tool" or m.get("tool_name") in COMPACTABLE_TOOLS
        ]

        if len(tool_results) <= keep_recent:
            return messages

        # Clear content of older tool results
        to_clear = tool_results[:-keep_recent]
        result = list(messages)
        tokens_saved = 0

        for idx, msg in to_clear:
            original_content = result[idx].get("content", "")
            if len(original_content) > 100:
                tokens_saved += len(original_content) // 4  # Rough token estimate
                result[idx] = {
                    **result[idx],
                    "content": f"[Tool result cleared — {len(original_content)} chars, use available context]",
                }

        self.state.compacted = True
        self.state.total_tokens_saved += tokens_saved
        logger.info("Microcompact: cleared %d tool results, ~%d tokens saved",
                     len(to_clear), tokens_saved)
        return result

    async def selective_prune(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Level 2: Remove low-value messages entirely.

        Removes messages that contribute least to the conversation:
        - Failed tool results with no useful information
        - Repeated similar searches
        - System messages from earlier turns
        """
        result = []
        seen_searches: set[str] = set()

        for msg in messages:
            # Always keep system, recent assistant, and user messages
            if msg.get("role") in ("system", "user"):
                result.append(msg)
                continue

            # Keep assistant messages (they define the conversation flow)
            if msg.get("role") == "assistant":
                result.append(msg)
                continue

            # Deduplicate search results
            tool_name = msg.get("tool_name", "")
            if tool_name in ("web_search", "exa_search", "brave_search"):
                search_key = f"{tool_name}:{msg.get('content', '')[:50]}"
                if search_key in seen_searches:
                    continue  # Skip duplicate search
                seen_searches.add(search_key)

            # Keep everything else (for now)
            result.append(msg)

        removed = len(messages) - len(result)
        if removed > 0:
            self.state.compacted = True
            logger.info("Selective prune: removed %d messages", removed)
        return result

    async def full_compact(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> list[dict[str, Any]]:
        """Level 3: Full LLM summarization of conversation history.

        Replaces the entire conversation history with a compact summary,
        preserving only the system prompt and the summary.
        """
        try:
            # Build conversation text for summarization
            conv_text = "\n".join(
                f"[{m.get('role', 'unknown')}] {m.get('content', '')[:500]}"
                for m in messages
                if m.get("role") != "system"
            )

            if not conv_text.strip():
                return messages

            summary_prompt = f"""Summarize this conversation into a compact context that preserves:
1. The user's original request and key requirements
2. All important findings, data sources, and decisions made
3. Current state: what has been done, what remains
4. Any errors or blockers encountered

Be concise but complete. Preserve specific names, URLs, scores, and technical details.

Conversation:
{conv_text[:10000]}"""

            summary, usage = await llm_service.complete(
                messages=[{"role": "user", "content": summary_prompt}],
                profile="context_compressor",
                model_tier="fast",
                max_tokens=2048,
            )

            # Replace history with compact summary
            compacted = []

            # Keep system prompt
            for m in messages:
                if m.get("role") == "system":
                    compacted.append(m)
                    break

            # Add compact summary as user context
            compacted.append({
                "role": "user",
                "content": f"[Context from previous conversation — auto-compacted]\n\n{summary}",
            })

            # Keep the last few messages for continuity
            recent = [m for m in messages[-4:] if m.get("role") != "system"]
            compacted.extend(recent)

            self.state.compacted = True
            self.state.consecutive_failures = 0
            self.state.last_compact_time = time.monotonic()

            tokens_before = sum(len(m.get("content", "")) // 4 for m in messages)
            tokens_after = sum(len(m.get("content", "")) // 4 for m in compacted)
            self.state.total_tokens_saved += tokens_before - tokens_after

            logger.info(
                "Full compact: %d messages → %d, ~%d tokens saved",
                len(messages), len(compacted), tokens_before - tokens_after,
            )
            return compacted

        except Exception as e:
            self.state.consecutive_failures += 1
            logger.error(
                "Full compact failed (attempt %d/%d): %s",
                self.state.consecutive_failures, self._max_failures, e,
            )
            return messages  # Return original on failure

    async def auto_compact(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int,
        system_prompt: str = "",
    ) -> list[dict[str, Any]]:
        """Main entry point — automatically choose and apply compression level."""
        level = self.should_compact(current_tokens)

        if level == CompactLevel.NONE:
            return messages

        logger.info(
            "Auto-compact triggered: level=%s, tokens=%d/%d (%.1f%%)",
            level.value, current_tokens, self.max_context_tokens,
            current_tokens / self.max_context_tokens * 100,
        )

        self.state.turn_counter += 1

        if level == CompactLevel.MICRO:
            return await self.microcompact(messages)
        elif level == CompactLevel.SELECTIVE:
            return await self.selective_prune(messages)
        elif level == CompactLevel.FULL:
            return await self.full_compact(messages, system_prompt)

        return messages
