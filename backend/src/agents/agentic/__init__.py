"""Agentic super-node — Claude Agent SDK-driven replacement for
route_sources / discover / coarse_filter / portal_detect / expand_portals /
content_classify / merge_urls / process_by_type / normalize_dedupe.

See backend/SPIKE.md for Phase 0 findings that justify this architecture.
The hybrid LangGraph keeps parse_intent / judge / reflect / finalize
unchanged; only the discovery+classification middle is replaced by an
agent loop.
"""
