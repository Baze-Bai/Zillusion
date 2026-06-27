"""Base-model MODE (text vs multimodal) and the browser_screenshot vision gate.

Covers the three layers:
  * `runtime.modality.resolve_vision` — registry / explicit override / default.
  * `explore()` (extraction path) and the agentic-crawl allow-list — the
    AGENT-side tool gate (the real control: the tool is in or out of context).
  * The MCP server's `browser_screenshot` backstop — defense-in-depth that
    refuses on a text-model run even if the tool stays in context.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from runtime import modality

_ROOT = Path(__file__).resolve().parents[1]  # harness root (has .mcp.json)
_SHOT = "mcp__browser-harness__browser_screenshot"  # multimodal mode's vision tool
_PROBE = "mcp__browser-harness__browser_vision_probe"  # text mode's vision tool (submodel eyes)


# ── resolve_vision: registry ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    "model,want",
    [
        (None, False),  # no model, no base-url signal -> text default
        ("deepseek-v4", False),
        ("deepseek/deepseek-v4-pro", False),
        ("glm-4.5-air", False),
        ("qwen2.5-coder:32b", False),
        ("local-llama", False),  # 'llama3.1' family -> text
        ("claude-sonnet-4-6", True),
        ("anthropic/claude-haiku-4-5", True),
        ("gemini-2.5-pro", True),
        ("gpt-5", True),
        ("gpt-4.1", True),
        ("glm-4.5v", True),
        ("qwen2.5-vl-7b", True),
        # strong vision suffix overrides a text-family prefix (review finding #3)
        ("deepseek-vl", True),
        ("deepseek-vl2", True),
        ("deepseek/deepseek-vl2", True),
        ("qwen2.5-coder-vl", True),
        ("llama-3.2-vision", True),
    ],
)
def test_registry_modality(monkeypatch, model, want):
    monkeypatch.delenv("CRAWLER_EXPLORER_VISION", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert modality.resolve_vision(model) is want


def test_env_override_beats_registry(monkeypatch):
    monkeypatch.setenv("CRAWLER_EXPLORER_VISION", "off")
    assert modality.resolve_vision("claude-sonnet-4-6") is False
    monkeypatch.setenv("CRAWLER_EXPLORER_VISION", "on")
    assert modality.resolve_vision("deepseek-v4") is True


def test_deepseek_base_url_sniff_when_no_model(monkeypatch):
    monkeypatch.delenv("CRAWLER_EXPLORER_VISION", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:4000/deepseek")
    assert modality.resolve_vision(None) is False


def test_vision_env_is_concrete(monkeypatch):
    for k in ("CRAWLER_EXPLORER_VISION", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
              "CRAWLER_EXPLORER_VISION_MODEL"):
        monkeypatch.delenv(k, raising=False)
    # Multimodal: just the concrete flag, no submodel creds.
    assert modality.vision_env("claude-sonnet-4-6") == {"CRAWLER_EXPLORER_VISION": "1"}
    # Text: flag "0"; with no creds in env, nothing extra is forwarded.
    assert modality.vision_env("deepseek-v4") == {"CRAWLER_EXPLORER_VISION": "0"}
    assert modality.qualified() == [_SHOT]


def test_vision_env_forwards_submodel_creds_for_text_mode(monkeypatch):
    monkeypatch.delenv("CRAWLER_EXPLORER_VISION", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CRAWLER_EXPLORER_VISION_MODEL", "gemini-2.5-pro")
    # Text run: the submodel needs a backend, so creds ride along.
    env = modality.vision_env("deepseek-v4")
    assert env["CRAWLER_EXPLORER_VISION"] == "0"
    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert env["CRAWLER_EXPLORER_VISION_MODEL"] == "gemini-2.5-pro"
    # Multimodal run: it sees directly, no submodel creds forwarded.
    assert "ANTHROPIC_API_KEY" not in modality.vision_env("claude-sonnet-4-6")


def test_mode_tools_are_mutually_exclusive(monkeypatch):
    monkeypatch.delenv("CRAWLER_EXPLORER_VISION", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    # text base model: keeps browser_vision_probe, drops browser_screenshot
    assert modality.enabled_vision_tools("deepseek-v4") == ("browser_vision_probe",)
    assert modality.gated_off_tools("deepseek-v4") == [_SHOT]
    # multimodal base model: keeps browser_screenshot, drops browser_vision_probe
    assert modality.enabled_vision_tools("claude-sonnet-4-6") == ("browser_screenshot",)
    assert modality.gated_off_tools("claude-sonnet-4-6") == [_PROBE]


# ── agent-side gate: explore() (extraction path) ─────────────────────────────
def _capture_explore_kwargs(monkeypatch, model):
    """Drive explore() with run_prompt stubbed out; return the kwargs it passed
    (disallowed_tools + extra_env carry the vision gate)."""
    from runtime import run as run_mod

    captured: dict = {}

    async def _fake_run_prompt(prompt, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        if False:  # make this an async generator that yields nothing
            yield {}

    monkeypatch.setattr(run_mod, "input_workflow_type", lambda *a, **k: "extraction")
    monkeypatch.setattr(run_mod, "run_prompt", _fake_run_prompt)

    async def _drive():
        async for _ in run_mod.explore("somesite", model=model):
            pass

    asyncio.run(_drive())
    return captured


def test_explore_text_model_keeps_probe_drops_screenshot(monkeypatch):
    monkeypatch.delenv("CRAWLER_EXPLORER_VISION", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    kw = _capture_explore_kwargs(monkeypatch, model="deepseek-v4")
    disallowed = kw.get("disallowed_tools") or []
    assert _SHOT in disallowed and _PROBE not in disallowed
    assert kw["extra_env"]["CRAWLER_EXPLORER_VISION"] == "0"


def test_explore_vision_model_keeps_screenshot_drops_probe(monkeypatch):
    monkeypatch.delenv("CRAWLER_EXPLORER_VISION", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    kw = _capture_explore_kwargs(monkeypatch, model="claude-sonnet-4-6")
    disallowed = kw.get("disallowed_tools") or []
    assert _PROBE in disallowed and _SHOT not in disallowed
    assert kw["extra_env"]["CRAWLER_EXPLORER_VISION"] == "1"


# ── agent-side gate: raw `prompt` CLI path (review finding #1) ───────────────
def _capture_prompt_cli_kwargs(monkeypatch, model):
    from runtime import cli

    captured: dict = {}

    async def _fake_run_prompt(prompt, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        if False:
            yield {}

    monkeypatch.setattr(cli, "run_prompt", _fake_run_prompt)
    args = cli.build_parser().parse_args(["--model", model, "prompt", "hello"])
    asyncio.run(cli._run(args))
    return captured


def test_prompt_cli_text_model_keeps_probe_drops_screenshot(monkeypatch):
    monkeypatch.delenv("CRAWLER_EXPLORER_VISION", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    kw = _capture_prompt_cli_kwargs(monkeypatch, "deepseek-v4")
    disallowed = kw.get("disallowed_tools") or []
    assert _SHOT in disallowed and _PROBE not in disallowed
    assert kw["extra_env"]["CRAWLER_EXPLORER_VISION"] == "0"


def test_prompt_cli_vision_model_keeps_screenshot_drops_probe(monkeypatch):
    monkeypatch.delenv("CRAWLER_EXPLORER_VISION", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    kw = _capture_prompt_cli_kwargs(monkeypatch, "claude-sonnet-4-6")
    disallowed = kw.get("disallowed_tools") or []
    assert _PROBE in disallowed and _SHOT not in disallowed
    assert kw["extra_env"]["CRAWLER_EXPLORER_VISION"] == "1"


# ── agent-side gate: agentic crawl allow-list ────────────────────────────────
def test_crawl_allowlist_picks_the_modes_vision_tool(monkeypatch):
    monkeypatch.delenv("CRAWLER_EXPLORER_VISION", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    from runtime import crawl_agent

    text = crawl_agent._allowed_tools("deepseek-v4")
    assert _PROBE in text and _SHOT not in text
    vis = crawl_agent._allowed_tools("claude-sonnet-4-6")
    assert _SHOT in vis and _PROBE not in vis


# ── env forwarding: build_options threads it to the MCP server cfg ───────────
def test_build_options_forwards_vision_env_to_browser_server():
    from runtime.options import build_options

    br = build_options(_ROOT, extra_env={"CRAWLER_EXPLORER_VISION": "0"})
    cfg = br.options.mcp_servers["browser-harness"]
    env = cfg["env"] if isinstance(cfg, dict) else cfg.env
    assert env["CRAWLER_EXPLORER_VISION"] == "0"


# ── server backstop: browser_screenshot refuses / serves per VISION_ENABLED ──
def test_screenshot_backstop_refuses_when_disabled(monkeypatch):
    from mcp_server import server as s

    monkeypatch.setattr(s, "VISION_ENABLED", False)
    out = asyncio.run(s.browser_screenshot())
    assert isinstance(out, dict) and out.get("vision_disabled") is True


def test_screenshot_returns_image_when_enabled(monkeypatch):
    from mcp_server import server as s

    monkeypatch.setattr(s, "VISION_ENABLED", True)

    async def _fake_shot(**kwargs):  # noqa: ANN001
        return (b"\x89PNG\r\n\x1a\nFAKE", "png")

    monkeypatch.setattr(s._browser, "screenshot_image", _fake_shot)
    out = asyncio.run(s.browser_screenshot(full_page=True))
    assert isinstance(out, s.Image)


def _fake_browser(monkeypatch, page):
    """A Browser instance with no real Chromium, wired to a fake page."""
    from mcp_server.browser import Browser

    br = Browser.__new__(Browser)

    async def _ensure():
        return None

    monkeypatch.setattr(br, "ensure_started", _ensure)
    monkeypatch.setattr(br, "_require_page", lambda: page)
    return br


def test_screenshot_image_falls_back_to_jpeg_over_cap(monkeypatch):
    """A PNG over the byte cap steps down to JPEG so the tool result stays under
    the SDK stream buffer (the browser_content failure mode for images)."""
    from mcp_server.browser import _SCREENSHOT_BYTE_CAP

    big_png = b"\x89PNG" + b"\x00" * (_SCREENSHOT_BYTE_CAP + 1)
    small_jpg = b"\xff\xd8\xff" + b"\x00" * 100

    class _FakePage:
        async def screenshot(self, **kw):
            return small_jpg if kw.get("type") == "jpeg" else big_png

    br = _fake_browser(monkeypatch, _FakePage())
    data, fmt = asyncio.run(br.screenshot_image(full_page=True))
    assert fmt == "jpeg" and len(data) <= _SCREENSHOT_BYTE_CAP


def test_screenshot_image_full_page_falls_back_to_viewport(monkeypatch):
    """When even min-quality full_page JPEG overflows, fall back to the viewport
    (full_page=False) JPEG rather than raising (review finding #6)."""
    from mcp_server.browser import _SCREENSHOT_BYTE_CAP

    over = b"\x00" * (_SCREENSHOT_BYTE_CAP + 1)
    small = b"\xff\xd8\xff" + b"\x00" * 50

    class _FakePage:
        async def screenshot(self, **kw):
            # full_page over cap at any quality; viewport (full_page=False) fits.
            return over if kw.get("full_page") else small

    br = _fake_browser(monkeypatch, _FakePage())
    data, fmt = asyncio.run(br.screenshot_image(full_page=True))
    assert fmt == "jpeg" and len(data) <= _SCREENSHOT_BYTE_CAP


def test_screenshot_image_raises_when_nothing_fits(monkeypatch):
    """Everything over cap → RAISE (small error result) rather than return a
    multi-MB payload that overflows the SDK buffer (review finding #6)."""
    import pytest as _pytest
    from mcp_server.browser import _SCREENSHOT_BYTE_CAP

    over = b"\x00" * (_SCREENSHOT_BYTE_CAP + 1)

    class _FakePage:
        async def screenshot(self, **kw):
            return over

    br = _fake_browser(monkeypatch, _FakePage())
    with _pytest.raises(RuntimeError, match="too large"):
        asyncio.run(br.screenshot_image(full_page=True))


def test_backstop_vocabulary_matches_gate(monkeypatch):
    """The server backstop and the agent-side gate must call the SAME raw values
    'disabled' (review finding #2)."""
    from mcp_server import server as s

    for v in ["0", "false", "off", "no", "text", "OFF", " Off "]:
        assert v.strip().lower() in s._VISION_OFF_VALUES
        monkeypatch.setenv("CRAWLER_EXPLORER_VISION", v)
        assert modality.resolve_vision("claude-sonnet-4-6") is False  # gate agrees
    for v in ["1", "true", "on", "vision"]:
        assert v.strip().lower() not in s._VISION_OFF_VALUES


# ── browser_vision_probe: the TEXT mode's submodel-as-eyes tool ──────────────────────
def test_browser_vision_probe_refuses_when_multimodal(monkeypatch):
    """A multimodal base model sees directly, so browser_vision_probe declines and points
    at browser_screenshot."""
    from mcp_server import server as s

    monkeypatch.setattr(s, "VISION_ENABLED", True)
    out = asyncio.run(s.browser_vision_probe(question="what is here?"))
    assert isinstance(out, dict) and out["ok"] is False and "multimodal" in out["reason"]


def test_browser_vision_probe_calls_submodel_when_text(monkeypatch):
    """A text base model: browser_vision_probe screenshots and routes it to the submodel,
    returning the text answer."""
    from mcp_server import server as s

    monkeypatch.setattr(s, "VISION_ENABLED", False)

    async def _fake_shot(**kw):  # noqa: ANN001
        return (b"PNGBYTES", "png")

    seen: dict = {}

    async def _fake_describe(image, image_format, question, **kw):  # noqa: ANN001
        seen.update(image=image, fmt=image_format, q=question)
        return {"ok": True, "answer": "a bar chart with 3 bars", "submodel": "sub"}

    monkeypatch.setattr(s._browser, "screenshot_image", _fake_shot)
    monkeypatch.setattr(s.vision, "describe", _fake_describe)
    out = asyncio.run(s.browser_vision_probe(question="how many bars?", full_page=True))
    assert out["ok"] is True and out["answer"]
    assert seen["q"] == "how many bars?" and seen["fmt"] == "png" and seen["image"] == b"PNGBYTES"


def test_vision_describe_degrades_without_backend(monkeypatch):
    """No API key for the submodel → graceful text reason, never raises."""
    from mcp_server import vision as vis_mod

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = asyncio.run(vis_mod.describe(b"x", "png", "what?"))
    assert out["ok"] is False and "ANTHROPIC_API_KEY" in out["reason"]


def test_vision_submodel_default_and_override(monkeypatch):
    from mcp_server import vision as vis_mod

    monkeypatch.delenv("CRAWLER_EXPLORER_VISION_MODEL", raising=False)
    assert vis_mod.submodel() == vis_mod.DEFAULT_SUBMODEL
    monkeypatch.setenv("CRAWLER_EXPLORER_VISION_MODEL", "gemini-2.5-pro")
    assert vis_mod.submodel() == "gemini-2.5-pro"
