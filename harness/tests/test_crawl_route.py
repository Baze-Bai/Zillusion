"""Crawl-route selection: CrawlBriefFile schema (mandatory anchor + hardness label).

The brief is the Explore→Agentic-Crawl handoff; its completeness anchor is the
load-bearing, MANDATORY field (the agent judges 'done' against it). These pin the
schema's guarantees so a malformed brief is rejected at write time, not discovered
mid-crawl.
"""

from __future__ import annotations

import pytest

from mcp_server.schemas import CrawlBriefFile, CompletenessAnchor


def _anchor(**kw) -> CompletenessAnchor:
    base = dict(
        hardness="hard",
        full_set="100 posts",
        enumeration="_page/_limit",
        termination="empty array",
        estimated_total=100,
    )
    base.update(kw)
    return CompletenessAnchor(**base)


def _brief(**kw) -> CrawlBriefFile:
    base = dict(
        site_id="x",
        goal_summary="blog posts",
        why_agentic="dynamic templates",
        completeness_anchor=_anchor(),
    )
    base.update(kw)
    return CrawlBriefFile(**base)


def test_completeness_anchor_mandatory():
    with pytest.raises(Exception):
        CrawlBriefFile(site_id="x", goal_summary="g", why_agentic="w")  # no anchor


def test_hardness_must_be_hard_or_soft():
    with pytest.raises(Exception):
        _anchor(hardness="medium")


def test_anchor_needs_full_set_enumeration_termination():
    for empty in ("full_set", "enumeration", "termination"):
        with pytest.raises(Exception):
            _anchor(**{empty: ""})


def test_goal_and_why_agentic_required():
    with pytest.raises(Exception):
        _brief(goal_summary="")
    with pytest.raises(Exception):
        _brief(why_agentic="")


def test_extra_keys_forbidden():
    with pytest.raises(Exception):
        _brief(bogus=1)


def test_render_hard_anchor_markdown():
    md = _brief(
        field_schema={"id": "pk"},
        identifier_field="id",
        hazards="none",
        recommended_strategy="batch via httpx",
    ).to_markdown()
    assert "HARD" in md and "hardness: hard" in md and "estimated_total: 100" in md
    assert "identifier_field" in md and "## Why agentic" in md and "batch via httpx" in md


def test_render_soft_anchor_omits_estimated_total():
    md = _brief(
        completeness_anchor=CompletenessAnchor(
            hardness="soft",
            full_set="unknown",
            enumeration="infinite scroll",
            termination="3 empty batches",
        )
    ).to_markdown()
    assert "SOFT" in md and "estimated_total" not in md
