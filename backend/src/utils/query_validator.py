"""Query content validation — reject obviously malformed inputs early.

Without this, the LangGraph pipeline burns 130+ seconds of LLM time on
garbage inputs (markdown code fences, whitespace, etc.) before producing
a useless answer. A Pydantic field validator catches these before they
reach parse_intent and returns 422.
"""

from __future__ import annotations

import json
import re

# Pure markdown delimiters / code fences. Matches strings consisting only
# of triple-backticks (optionally followed by a language tag), triple-tildes,
# horizontal-rule markers (---/***/___), or HTML comment markers.
_MARKDOWN_FENCE_ONLY_RE = re.compile(
    r"^\s*("
    r"`{3,}[a-zA-Z0-9_+\-]*"     # ```json, ```python, ```
    r"|~{3,}[a-zA-Z0-9_+\-]*"    # ~~~json
    r"|[-*_]{3,}"                # ---, ***, ___ (HR markers)
    r"|<!--.*?-->"               # HTML comment
    r")\s*$",
    re.DOTALL,
)

# Leading bullet/list marker — `- foo`, `* foo`. Real users phrase a
# question as a sentence; bullet-prefixed input means a planning note
# or changelog entry leaked into the request body.
_BULLET_FRAGMENT_RE = re.compile(r"^\s*[-*]\s+\S")

# Pipeline-iteration / dev-process vocabulary. None of these phrases
# appear in a genuine data-discovery question — they show up only in
# planning prose, regression-test descriptions, or changelog fragments.
# Kept narrow on purpose: "iteration 5" (full word) is allowed because
# it can describe ML training data; only the abbreviated "iter N" /
# "iters N" / "per rules" keywords are rejected. "stress-test" is
# intentionally NOT included — it is a legitimate noun ("financial
# stress test data") and the iter / per-rules patterns already catch
# every observed failing input.
_PIPELINE_META_RE = re.compile(
    r"\biter\s*\d+\b"                # "iter 10", "iter10"
    r"|\biters\s+\d+"                # "iters 2,3,4"
    r"|\bper\s+rules?\b",            # "per rules", "per rule"
    re.IGNORECASE,
)

# Regression-test / dev-process prose. These multi-word collocations are
# unambiguously software-engineering vocabulary ("hit the same code path",
# "verify the fix generalized") and don't appear in genuine data-discovery
# questions. Distinct from _PIPELINE_META_RE because they catch flowing
# English rather than structured tokens like "iter 10". Kept narrow on
# purpose, mirroring _PIPELINE_META_RE: only collocations that survive a
# "would a real user ever type this verbatim?" sanity check — single words
# like "fix", "regression", or "test" alone are NOT matched because they
# can legitimately describe data ("regression analysis dataset", "fix
# rates", "stress test data").
_REGRESSION_PROSE_RE = re.compile(
    r"\bsame\s+code(-?path|\s+path)\b"          # "(the) same code path", "same codepath"
    r"|\bverify\s+(the\s+|that\s+)?fix\b"       # "verify the fix", "verify that fix"
    r"|\bfix\s+generali[sz]ed\b",               # "fix generalized" / "fix generalised"
    re.IGNORECASE,
)

# Trailing inline dev annotation: " # ..." at the end of the query.
# Whitespace required on both sides of the hash so URL fragments
# (page#section) and hashtags (#climate) are NOT matched. Only used
# for the recoverable-strip path — the comment itself is then checked
# for pipeline-iteration metadata before we agree to drop it.
_TRAILING_DEV_ANNOTATION_RE = re.compile(r"\s+#\s+[^\n]*$")

# Leading shape of a JSON test envelope: `{"query": "...`. Catches the
# case where the closing brace was eaten by some earlier preprocessing
# (e.g. the dev-annotation strip greedily consumed `..., "max_iter": 1}`),
# leaving a fragment that no longer parses as JSON but is still obviously
# a wrapped envelope rather than an actual question.
_JSON_ENVELOPE_HEAD_RE = re.compile(
    r'^\s*\{\s*"\s*query\s*"\s*:\s*"', re.IGNORECASE
)

# Tokens that signal a real data-discovery query body (file format, API
# vocabulary, frequency, range words). Used by `_is_header_fragment` to
# spare colon-terminated phrases that actually mention the data they want.
_HEADER_FRAGMENT_DATA_SIGNAL_RE = re.compile(
    r"\b(api|apis|csv|tsv|json|xml|parquet|dataset|datasets|"
    r"sql|database|endpoint|endpoints|stats|statistics|"
    r"monthly|annual|yearly|weekly|daily|hourly|quarterly|"
    r"timeseries|metrics|records|registry|catalog)\b",
    re.IGNORECASE,
)

# Letters / digits / CJK ideographs / Hiragana / Katakana / Hangul.
# Anything in this set counts as "real query content".
_MEANINGFUL_CHAR_RE = re.compile(
    r"[A-Za-z0-9"
    r"一-鿿"             # CJK Unified Ideographs
    r"぀-ゟ"             # Hiragana
    r"゠-ヿ"             # Katakana
    r"가-힯"             # Hangul Syllables
    r"]"
)

_MIN_MEANINGFUL_CHARS = 3

# Shown in 422 error messages so users see what a real query looks like
# instead of just being told what they sent was wrong.
_EXAMPLE_QUERY = (
    'e.g. "monthly US unemployment rate by state, 2010-2024" '
    'or "FRED API for treasury yields"'
)


def _strip_trailing_dev_annotation(text: str) -> str:
    """Recoverable preprocessing: drop a trailing ` # ...iter N...` comment.

    Test harnesses often append a markdown-style annotation to a query
    (e.g. ``... 2018-2024 # regression of iter 7 (similar)``). The body
    before the space-hash-space delimiter is a legitimate data-discovery
    question; only the comment carries the dev-process metadata. Stripping
    it lets the real query proceed instead of failing validation.

    Conservative: only fires when the comment itself matches pipeline-
    iteration vocabulary; otherwise the trailing text may be meaningful.
    """
    m = _TRAILING_DEV_ANNOTATION_RE.search(text)
    if not m or not _PIPELINE_META_RE.search(m.group(0)):
        return text
    return text[: m.start()].rstrip()


def _is_test_envelope(stripped: str) -> bool:
    """Detect a query that is itself a JSON test-harness envelope.

    A real user never types `{"query": "...", "expect_status": 422}` as
    a question — that shape only appears when a test harness accidentally
    double-wraps its payload. We catch it before parse_intent fabricates
    a domain and burns 200+ seconds chasing it.

    Two-stage detection:
    * leading shape `{"query": "...` — fires even when the closing brace
      was stripped by some earlier preprocessing step (e.g. an inline
      dev annotation greedily ate everything to end-of-line);
    * full JSON parse — fires for the strictly-valid envelope case.
    """
    if _JSON_ENVELOPE_HEAD_RE.match(stripped):
        return True
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return False
    try:
        parsed = json.loads(stripped)
    except (ValueError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    keys = {str(k).lower() for k in parsed.keys()}
    return "query" in keys


def _is_header_fragment(stripped: str) -> bool:
    """Detect a query that is just a short heading-style phrase ending in ':'.

    Real users phrase a discovery request as a sentence or noun phrase;
    a fragment like ``Looking at the history:`` reads as the title of a
    section the user was about to write rather than an actual question.
    Conservative: only fires when the body is short (<= 6 words) AND
    contains neither a four-digit year reference nor any of the obvious
    data-domain vocabulary that would suggest a real query (API, CSV,
    dataset, monthly, etc.).
    """
    if not stripped.endswith(":"):
        return False
    body = stripped.rstrip(":").strip()
    if not body:
        return True
    if len(body.split()) > 6:
        return False
    if _HEADER_FRAGMENT_DATA_SIGNAL_RE.search(body):
        return False
    if re.search(r"\b\d{4}\b", body):
        return False
    return True


def validate_query_content(query: str) -> str:
    """Reject markdown-fence-only / whitespace-only / no-content queries.

    Returns the original query if valid; raises ValueError otherwise.
    Pydantic converts ValueError into a 422 response, sparing the pipeline
    a ~3-minute LLM-burn on obvious garbage input.
    """
    if not query or not query.strip():
        raise ValueError(
            f"query must not be empty or whitespace-only ({_EXAMPLE_QUERY})"
        )

    stripped = query.strip()

    # JSON-envelope detection runs BEFORE the dev-annotation strip so the
    # closing brace is still in place for `json.loads`. Without this, a
    # double-wrapped payload like `{"query": "... # iter 9 ...", "max_iter": 1}`
    # gets its trailing `..."}` eaten by the strip and slips past the parse.
    if _is_test_envelope(stripped):
        raise ValueError(
            "query appears to be a JSON test envelope (object with a 'query' "
            f"key) rather than an actual question — unwrap the inner query "
            f"and resend ({_EXAMPLE_QUERY})"
        )

    # Recover legitimate queries that ended up with a stray ` # iter N ...`
    # annotation (typical test-harness artifact). If we strip it, downstream
    # nodes see the cleaned query body. If nothing matched, this is a no-op.
    cleaned = _strip_trailing_dev_annotation(stripped)
    if cleaned != stripped:
        stripped = cleaned
        query = cleaned

    if _MARKDOWN_FENCE_ONLY_RE.match(stripped):
        raise ValueError(
            f"query is only a markdown delimiter ({stripped!r}) — "
            f"please send the actual question, not the fence around it ({_EXAMPLE_QUERY})"
        )

    # Re-check post-strip in case the strip itself revealed an envelope
    # shape that wasn't visible before (e.g. `{"q": "..."} # iter N` had
    # the comment outside the braces, and stripping it now exposes the
    # bare envelope). Cheap and orthogonal to the pre-strip pass above.
    if _is_test_envelope(stripped):
        raise ValueError(
            "query appears to be a JSON test envelope (object with a 'query' "
            f"key) rather than an actual question — unwrap the inner query "
            f"and resend ({_EXAMPLE_QUERY})"
        )

    if _is_header_fragment(stripped):
        raise ValueError(
            f"query looks like a heading fragment ({stripped!r}) — a short "
            "colon-terminated phrase reads as a section title rather than a "
            f"data-discovery question ({_EXAMPLE_QUERY})"
        )

    if _BULLET_FRAGMENT_RE.match(stripped):
        raise ValueError(
            "query starts with a bullet/list marker ('- ' or '* ') — looks "
            "like a planning note or changelog entry rather than an actual "
            f"question; please rephrase as a sentence ({_EXAMPLE_QUERY})"
        )

    pipeline_match = _PIPELINE_META_RE.search(stripped)
    if pipeline_match:
        raise ValueError(
            f"query contains pipeline-iteration metadata (matched: "
            f"{pipeline_match.group(0)!r}; e.g. 'iter 10', 'iters 2,3,4', "
            "'per rules') which suggests it's planning prose rather than an "
            f"actual data-discovery question ({_EXAMPLE_QUERY})"
        )

    regression_match = _REGRESSION_PROSE_RE.search(stripped)
    if regression_match:
        raise ValueError(
            f"query contains regression-test / dev-process prose (matched: "
            f"{regression_match.group(0)!r}; e.g. 'same code path', 'verify "
            "the fix', 'fix generalized') which suggests it's an internal QA "
            f"note rather than an actual data-discovery question ({_EXAMPLE_QUERY})"
        )

    meaningful = len(_MEANINGFUL_CHAR_RE.findall(stripped))
    if meaningful < _MIN_MEANINGFUL_CHARS:
        raise ValueError(
            f"query has too little meaningful content "
            f"({meaningful} alphanumeric/CJK chars, need >= {_MIN_MEANINGFUL_CHARS}; {_EXAMPLE_QUERY})"
        )

    return query
