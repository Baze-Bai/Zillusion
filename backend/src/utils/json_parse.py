"""Robust JSON extraction from LLM responses.

LLMs don't reliably return raw JSON — they wrap it in markdown code fences,
prepend explanations, mix reasoning and output, etc. This module tries
multiple strategies to find valid JSON in the response.

Typical LLM outputs we handle:
  - '{"key": "value"}'                          ← raw JSON
  - '```json\n{"key": "value"}\n```'            ← markdown fence
  - 'Here is the answer:\n{"key": "value"}'     ← preamble
  - 'Let me think... [{"a":1}, {"b":2}]'        ← reasoning before output
  - Text with JSON embedded mid-paragraph

The previous `.strip("```json").strip("```")` had a bug: strip() removes
character sets, not substrings — stripping "```json" also removed any
j/s/o/n at the edges.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Matches markdown code fences (with or without "json" label)
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL)

# GLM/Z.AI sometimes emit a pseudo-tool-call XML wrapper instead of a
# proper JSON tool call. The closing </arg_value> is sometimes missing
# when the completion was truncated, so accept end-of-string as a
# terminator too.
_PSEUDO_TOOL_CALL_KV_RE = re.compile(
    r"<arg_key>\s*(.*?)\s*</arg_key>\s*<arg_value>\s*(.*?)\s*(?:</arg_value>|$)",
    re.DOTALL,
)


def extract_pseudo_tool_call(text: str) -> dict[str, str] | None:
    """Parse GLM/Z.AI pseudo-tool-call XML into a flat dict.

    Some GLM responses look like::

        <tool_call>SchemaName
        <arg_key>score</arg_key>
        <arg_value>3</arg_value>
        <arg_key>rationale</arg_key>
        <arg_value>...</arg_value>
        </tool_call>

    Instructor sees the repeated arg_key/arg_value as "multiple tool calls"
    and refuses to merge. We extract what we can; Pydantic's
    model_validate coerces "3" → int(3), "true" → bool(True), etc.
    Returns None if no arg_key/arg_value pairs are found.

    Empty captured values are dropped — they typically indicate the
    completion was truncated mid-`<arg_value>` (max_tokens hit), where
    the regex's `$` fallback matches the open tag with no payload.
    Forwarding `key=""` would then break Pydantic on typed fields
    (e.g. ``fields_present: list[str]`` rejects ``""``), wasting the
    recovery path. Letting the schema default fill in — which is what
    happens when the key is omitted — keeps the rest of the parsed
    response usable.
    """
    if not text or "<arg_key>" not in text:
        return None
    out: dict[str, str] = {}
    for m in _PSEUDO_TOOL_CALL_KV_RE.finditer(text):
        key = m.group(1).strip()
        value = m.group(2).strip()
        if key and value:
            out[key] = value
    return out or None


def extract_json(text: str, default: Any = None) -> Any:
    """Extract JSON (object or array) from an LLM response string.

    Tries in order:
      1. Direct json.loads on stripped text
      2. Content inside ```json ... ``` fence
      3. Content inside any ``` ... ``` fence
      4. First balanced {...} object found
      5. First balanced [...] array found

    Returns `default` if no valid JSON found.
    """
    if not text:
        return default

    stripped = text.strip()

    # Strategy 1: direct parse
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Strategy 2/3: extract from code fence
    m = _FENCE_RE.search(stripped)
    if m:
        fenced = m.group(1).strip()
        try:
            return json.loads(fenced)
        except json.JSONDecodeError:
            # Fall through — maybe fence is truncated
            stripped = fenced

    # Strategy 4/5: find first balanced brackets
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        result = _find_balanced(stripped, open_ch, close_ch)
        if result is not None:
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                continue

    logger.debug("JSON extraction failed from: %s", text[:200])
    return default


def _find_balanced(text: str, open_ch: str, close_ch: str) -> str | None:
    """Find the first balanced {...} or [...] block in text.

    Handles nested brackets but does NOT handle strings containing
    unescaped brackets. Good enough for LLM JSON output in practice.
    """
    start = text.find(open_ch)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]

        if escape:
            escape = False
            continue

        if ch == "\\":
            escape = True
            continue

        if ch == '"' and not escape:
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None
