"""Stage 1: Intent Parser — NL query → StructuredRequirement.

The make-or-break stage. A vague query dies here; a well-parsed one
drives high recall downstream. Model: Sonnet (quality over speed).
"""

from __future__ import annotations

import logging
import time

from src.config import settings
from src.models.requirement import StructuredRequirement
from src.models.state import AgentState
from src.services.llm import llm_service

logger = logging.getLogger(__name__)

INTENT_PARSER_SYSTEM = """You are a data requirements analyst. Given a user's natural language data need,
parse it into a structured requirement WITHOUT imposing any opinion about
WHERE or HOW the data should come from. Be NEUTRAL about source types.

RULES:
1. ALWAYS generate English keywords (search_keywords_en) even if the query
   is in Chinese — many downstream tools are English-only.
2. Generate 2-5 independently searchable sub-questions that decompose the
   query along its natural axes.
3. Synthesize a target_schema reflecting the fields the user likely cares
   about. Example: user says "AI company funding" → produce
   {"properties": {"company_name": {}, "round": {}, "amount_usd": {},
                   "date": {}, "lead_investor": {}}}.

4. `known_authoritative_sources`: list ANY candidate sources that come to
   mind — WITHOUT pre-filtering by "trustworthiness" or "authority". This
   list may include:
     - Government / academic / standards-body APIs and datasets
     - Open-data registries (Kaggle, HuggingFace, CKAN portals, OpenAlex)
     - Commercial listing sites (Ctrip, Amazon, Booking, JD)
     - Consumer-facing search/category pages
     - Forums, knowledge bases, encyclopedias
     - Specific named URLs the user mentioned
   The pipeline will validate liveness and relevance downstream — your job
   here is BREADTH, not pre-judgement.

5. `target_registries`: ONLY include if the user explicitly asked for known
   registries OR the query is clearly about a registry-covered domain
   (e.g. "academic papers" → openalex). Do NOT reflexively suggest
   "kaggle, huggingface, openalex" — they may be irrelevant to commercial
   / consumer / regional queries.

6. `desired_formats` / `data_type_hints`: ONLY include what the user
   explicitly stated or strongly implied. Do NOT default to
   ['csv', 'json', 'api'] just because the query mentions "data". If the
   user says "info about X" without format preference, leave these empty
   or near-empty so downstream discovery stays neutral.

7. Be specific with domain/sub_domains tags. "finance" beats "general".
8. Set appropriate constraints: geographic_scope, temporal_range,
   update_frequency.
9. `title`: a concise 3-8 word label naming the data need, in the SAME
   language as the user's query (e.g. "US historical weather data sources",
   "美国历史天气数据源"). It is shown as the task's name in the UI sidebar, so
   make it specific and human-readable — not a full sentence, not "data".
"""

INTENT_PARSER_USER = """User data need:
{query}

Analyze this requirement and produce a comprehensive StructuredRequirement.
Think about:
- What domain does this fall into?
- What does the user LITERALLY ask for? (a data file? an API? a list of
  items from a website? a specific URL? scraped HTML? a comparison?)
- What are the implicit constraints (geographic, temporal, license)?
- What English keywords would find related material?
- What sources OF ANY KIND might have this content — APIs, datasets,
  commercial sites, consumer listing pages, official portals, search-
  result pages, forums, encyclopedias? Surface them all without prejudging.
- What schema would the ideal output have?

Be NEUTRAL about source types. If the user did not specify, do not invent
a preference for "authoritative" or "structured" sources.
"""


async def parse_intent_node(state: AgentState) -> dict:
    """Parse natural language query into structured requirement."""
    start = time.monotonic()
    query = state["query"]

    logger.info("Stage 1: Parsing intent for query: %s", query[:100])

    requirement, usage = await llm_service.complete_structured(
        messages=[
            {"role": "system", "content": INTENT_PARSER_SYSTEM},
            {"role": "user", "content": INTENT_PARSER_USER.format(query=query)},
        ],
        response_model=StructuredRequirement,
        profile="intent_parser",
        model_tier="reasoning",  # fallback tier if profile has no override
        max_tokens=settings.llm.max_tokens_parse,
    )

    # Ensure original query is preserved
    requirement.original_query = query

    # Debug: full requirement structure
    logger.debug(
        "Stage 1 requirement detail:\n"
        "  domain=%s, sub_domains=%s\n"
        "  keywords_en=%s\n"
        "  keywords_zh=%s\n"
        "  sub_questions=%s\n"
        "  data_type_hints=%s, desired_formats=%s\n"
        "  known_sources=%s\n"
        "  activated_registries=%s\n"
        "  target_schema=%s\n"
        "  constraints: geo=%s, temporal=%s, license=%s, update_freq=%s",
        requirement.domain, getattr(requirement, 'sub_domains', []),
        requirement.search_keywords_en,
        getattr(requirement, 'search_keywords_zh', []),
        requirement.sub_questions,
        getattr(requirement, 'data_type_hints', []),
        getattr(requirement, 'desired_formats', []),
        getattr(requirement, 'known_authoritative_sources', []),
        getattr(requirement, 'activated_registries', []),
        str(getattr(requirement, 'target_schema', None))[:200],
        getattr(requirement, 'geographic_scope', None),
        getattr(requirement, 'temporal_range', None),
        getattr(requirement, 'license_constraint', None),
        getattr(requirement, 'update_frequency', None),
    )
    logger.debug("Stage 1 LLM cost: $%.4f", usage.cost_usd)

    duration = time.monotonic() - start
    logger.info(
        "Stage 1 complete: domain=%s, %d en-keywords, %d sub-questions, %.1fs",
        requirement.domain,
        len(requirement.search_keywords_en),
        len(requirement.sub_questions),
        duration,
    )

    return {
        "requirement": requirement,
        "cost_accumulated": state.get("cost_accumulated", 0) + usage.cost_usd,
        "stage_timings": {**state.get("stage_timings", {}), "parse_intent": duration},
    }
