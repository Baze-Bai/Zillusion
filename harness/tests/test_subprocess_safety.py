"""Unit tests for runtime.subprocess_safety — untrusted-workflow env scrub (C2)."""

from __future__ import annotations

from runtime.subprocess_safety import is_secret_key, scrubbed_env


def test_is_secret_key_named_providers():
    for k in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",  # prefix match — dropped too (workflow doesn't need it)
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "DASHSCOPE_API_KEY",
        "ZAI_API_KEY",
        "MINIMAX_API_KEY",
        "SEARCH_BRAVE_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "GOOGLE_API_KEY",
    ):
        assert is_secret_key(k), k


def test_is_secret_key_shapes():
    for k in ("MY_TOKEN", "X_SECRET", "DB_PASSWORD", "FOO_APIKEY", "SOME_KEY", "DB_URL", "REDIS_URL", "DATABASE_URL"):
        assert is_secret_key(k), k


def test_is_secret_key_keeps_operational():
    for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "PYTHONPATH", "HTTP_PROXY", "LANG", "CRAWL_MODE", "HEADLESS"):
        assert not is_secret_key(k), k


def test_scrubbed_env_drops_secrets_keeps_rest():
    base = {
        "ANTHROPIC_API_KEY": "sk-secret",
        "DEEPSEEK_API_KEY": "ds",
        "DB_URL": "postgres://x",
        "REDIS_URL": "redis://x",
        "MY_TOKEN": "t",
        "PATH": "/usr/bin",
        "SYSTEMROOT": "C:/Windows",
    }
    out = scrubbed_env(base=base)
    assert "PATH" in out and "SYSTEMROOT" in out
    for leaked in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "DB_URL", "REDIS_URL", "MY_TOKEN"):
        assert leaked not in out, leaked


def test_scrubbed_env_extra_wins_and_is_kept():
    base = {"ANTHROPIC_API_KEY": "x", "PATH": "/usr/bin"}
    out = scrubbed_env(base=base, extra={"CRAWL_MODE": "full", "HEADLESS": "1", "WORKFLOW_CDP_PORT": "9222"})
    assert out["CRAWL_MODE"] == "full" and out["HEADLESS"] == "1" and out["WORKFLOW_CDP_PORT"] == "9222"
    assert "ANTHROPIC_API_KEY" not in out
    assert out["PATH"] == "/usr/bin"


def test_scrubbed_env_extra_is_not_secret_filtered():
    # `extra` is the caller's explicit, trusted addition — applied AFTER the
    # scrub, so even a secret-shaped knob name would survive (none here).
    out = scrubbed_env(base={"PATH": "/x"}, extra={"CRAWL_MODE": "sample"})
    assert out["CRAWL_MODE"] == "sample"
