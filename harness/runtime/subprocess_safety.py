"""A scrubbed environment for spawning UNTRUSTED workflow.py.

The explore agent WRITES workflow.py; the validator (run_workflow_isolated) and
the production runner (run_exec) then EXECUTE it. That code is untrusted — it
must not inherit the harness's own secrets (ANTHROPIC_API_KEY and every other
provider key, plus DB/Redis URLs the parent process may carry). The "isolation
by tool surface" story (the validator/run agents themselves have no Bash/Write)
is defeated if they shell out to attacker-controlled code with the full env.

The per-site API credential a workflow legitimately needs is staged as
``credentials.json`` in its cwd (the workflow walks up to find it) — it is NEVER
read from this env, so dropping secret-shaped vars breaks no legitimate workflow.

This is a best-effort DENYLIST (preserves operational env — PATH, SYSTEMROOT,
TEMP, proxy, PYTHON* — so crawling / playwright still work). It removes the
named provider keys and anything matching a secret-shaped name.
"""

from __future__ import annotations

import os

# Suffixes/prefixes/exact names that denote a secret. Compared case-insensitively.
_SECRET_SUFFIXES = (
    "_API_KEY",
    "_APIKEY",
    "_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_PASSWD",
    "_DSN",
    "_CREDENTIAL",
    "_CREDENTIALS",
)
_SECRET_PREFIXES = (
    "ANTHROPIC_",
    "OPENAI_",
    "DEEPSEEK_",
    "DASHSCOPE_",
    "ZAI_",
    "MINIMAX_",
    "SEARCH_",
    "AWS_",
    "AZURE_",
    "GCP_",
    "GOOGLE_",
)
_SECRET_EXACT = {"DB_URL", "DATABASE_URL", "REDIS_URL"}


def is_secret_key(key: str) -> bool:
    k = key.upper()
    if k in _SECRET_EXACT:
        return True
    if any(k.endswith(s) for s in _SECRET_SUFFIXES):
        return True
    return any(k.startswith(p) for p in _SECRET_PREFIXES)


def scrubbed_env(base: dict | None = None, extra: dict | None = None) -> dict:
    """A copy of ``base`` (default ``os.environ``) with secret-shaped vars
    dropped. ``extra`` (caller's explicit, non-secret additions like CRAWL_MODE)
    is applied AFTER scrubbing so it always wins."""
    src = os.environ if base is None else base
    out = {k: v for k, v in src.items() if not is_secret_key(k)}
    if extra:
        out.update(extra)
    return out


__all__ = ["scrubbed_env", "is_secret_key"]
