"""Application configuration via pydantic-settings.

All values driven by environment variables. Every hardcoded parameter
in the codebase is externalized here with a sensible default.
"""

from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load backend/.env into os.environ BEFORE Settings() is instantiated below, so
# both pydantic-settings AND libraries that read os.environ directly (e.g.
# litellm's provider keys) see the configured values. Without this, a bare
# `uvicorn`/`pytest`/IDE launch instantiates Settings() before the env is loaded
# and silently falls back to defaults (e.g. litellm routes to OpenAI).
# In container/prod the env is already injected (docker-compose env_file) and
# override=False ensures those real values win. No-op if the file is absent.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


# ──────────────────────────────────────────────
# 1. Application
# ──────────────────────────────────────────────
class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")
    debug: bool = False
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:3000"]
    # Optional shared secret. Empty (default) = no API auth (single-user /
    # localhost). Set APP_API_KEY to require an ``X-API-Key`` header on all API
    # requests, and ``?api_key=`` on the takeover WebSocket. See SECURITY.md.
    api_key: str = ""


# ──────────────────────────────────────────────
# 2. Database
# ──────────────────────────────────────────────
class DatabaseConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DB_")
    # Single-user local default: SQLite (async via aiosqlite). Persists the
    # conversational sessions/events/artifacts. Override DB_URL for Postgres.
    # Path is relative to the process CWD — matches where agent-workspace/ and
    # run-logs/ already land (launch the backend from the same dir as before).
    url: str = "sqlite+aiosqlite:///./agent-workspace/discovery.db"
    pool_size: int = 20
    pool_overflow: int = 10


# ──────────────────────────────────────────────
# 3. Redis
# ──────────────────────────────────────────────
class RedisConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REDIS_")
    url: str = "redis://localhost:6379/0"


# ──────────────────────────────────────────────
# 4. LLM
# ──────────────────────────────────────────────
class LLMConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_")

    @model_validator(mode="before")
    @classmethod
    def _blank_optional_numeric_to_none(cls, data):
        # A .env may leave optional per-profile overrides blank, e.g.
        # ``LLM_FINALIZE_TEMPERATURE=``. pydantic can't parse '' as float/int,
        # so treat a blank numeric override as "unset" → fall back to default.
        # (The .env.example documents "Empty = use tier default".)
        if isinstance(data, dict):
            for k, v in list(data.items()):
                if v == "" and (str(k).endswith("_temperature") or str(k).endswith("_max_tokens")):
                    data[k] = None
        return data

    # ── Model selection (fallback chain: primary → fallback → china) ──
    # 三档模型 (当前 .env 默认: strong=glm-5.1 实测限 5, fast=glm-4.5-air 实测限 5):
    #   reasoning: 最强推理模型, 单次调用 (Intent Parser / Critic / Router)
    #   strong:    高质量强模型, 并行调用 (Stage-B 精评 max_candidates_stage_b=10 路并发)
    #   fast:      高吞吐快模型, 大量并发 (Stage-A 8 批 / TypeClassifier / Tier2 ~45 路 / 路由)
    # 跨进程协调: 服务端 per-API-key 限额由 Redis distributed semaphore (services/llm.py
    # _RedisModelSemaphore) 管理, 多个 backend 进程加起来不会超过本表中的硬限额.
    # 单请求峰值与限额对比 (实测值, 见 services/llm.py _MODEL_CONCURRENCY 注释):
    #   strong  限 5   vs  stage-B 10 路 → 单管线 1 批等待 (5 立即跑+5 排队)
    #   fast    限 5   vs  tier-2 ~45 路 → 队列深度 ~40 (在该 key 上 fast 池真就这么紧)
    # 觉得 stage-B 太慢可考虑 LLM_JUDGE_STAGE_B_PRIMARY=zai/autoglm-phone-multilingual
    # (实测 11 路并发, 但是 phone 专用模型, 不一定适合通用评分场景).
    primary_reasoning: str = ""           # e.g. "zai/glm-5.1" (实测限 5, 单次调用为主)
    primary_strong: str = "claude-sonnet-4-20250514"
    primary_fast: str = "claude-haiku-4-5-20251001"
    fallback_strong: str = "gpt-4o"
    fallback_fast: str = "gpt-4o-mini"

    # China-region models (third-level fallback or primary for CN users)
    china_strong: str = ""                # e.g. "dashscope/qwen-max", "zai/glm-4.7", "minimax/MiniMax-M2.5", "deepseek/deepseek-v4-pro"
    china_fast: str = ""                  # e.g. "dashscope/qwen-turbo", "zai/glm-4.5-flash", "minimax/MiniMax-M2.1", "deepseek/deepseek-v4-flash"

    # Embedding model
    embedding_model: str = "text-embedding-3-small"

    # ── Provider API keys ──
    # (litellm reads these from env automatically, listed here for documentation)
    # ANTHROPIC_API_KEY     — Claude
    # OPENAI_API_KEY        — GPT
    # DASHSCOPE_API_KEY     — Qwen (Alibaba DashScope)
    # ZAI_API_KEY           — GLM (Zhipu Z.AI)
    # MINIMAX_API_KEY       — MiniMax
    # DEEPSEEK_API_KEY      — DeepSeek

    # ── Qwen (DashScope) specific ──
    dashscope_api_base: str = ""          # 留空=默认 | 国际站: https://dashscope-intl.aliyuncs.com/compatible-mode/v1

    # ── GLM (Z.AI / Zhipu) specific ──
    # 国际站: https://api.z.ai/api/paas/v4/
    # 国内站: https://open.bigmodel.cn/api/paas/v4/
    zai_api_base: str = ""                # 留空=litellm 默认
    # 推理模式: 默认关闭 (速度优先)。开启可提升智能度但速度慢 3-5 倍
    # 仅影响推理模型 (glm-4.6+, glm-5.x)
    zai_thinking_enabled: bool = False

    # ── MiniMax specific ──
    minimax_api_base: str = "https://api.minimax.io/v1"

    # ── DeepSeek specific ──
    # OpenAI-compatible: https://api.deepseek.com (默认, 推荐)
    # Anthropic-compatible: https://api.deepseek.com/anthropic
    # litellm 通过 deepseek/ 前缀路由到 OpenAI 兼容端点, 因此这里默认填
    # OpenAI 那个. 用 model="deepseek/deepseek-v4-pro" 即可.
    deepseek_api_base: str = ""           # 留空=litellm 默认 (https://api.deepseek.com)
    # 推理模式: 默认关闭 (速度优先). DeepSeek V4 服务端默认是开启的,
    # 不显式 disabled 每次调用都会走慢 3-5x 的 thinking 路径, 还会被某些
    # 客户端校验当成 400. 仅影响 deepseek-v4-pro / deepseek-v4-flash.
    deepseek_thinking_enabled: bool = False

    # ── Token limits (per-stage) ──
    max_tokens_parse: int = 4096          # Stage 1 Intent Parser
    max_tokens_route: int = 256           # Stage 2 Router LLM supplement
    max_tokens_classify: int = 1024       # Stage 3.5 TypeClassifier LLM
    max_tokens_portal_detect: int = 512   # Portal Detector LLM
    max_tokens_stage_a: int = 512         # Stage-A batch judging
    max_tokens_stage_b: int = 1536        # Stage-B per-source scoring (rationale + score; rubric asks for 3-point rationale, 1024 still truncated mid-sentence on verbose sources)
    max_tokens_authority: int = 384       # Authority LLM fallback (must fit `{"score": x, "rationale": "..."}`; 128 truncated mid-JSON → extract_json fails → silent fallback to base score with "no rationale")
    max_tokens_critic: int = 1024         # Stage 7 Reflect
    max_tokens_guide: int = 1024          # Stage 8 per-source usage guide (3-5 sentences with spec details fits in ~300-400 tok; 1024 leaves headroom)
    max_tokens_embedded_tier2: int = 1024
    max_tokens_embedded_tier3: int = 2048

    # Generation params
    temperature: float = 0.1

    # Per-LLM-call HTTP timeout in seconds. Without this, a hung TLS
    # handshake or unresponsive provider blocks the SSE pipeline forever
    # (no exception, no error event, the client just sees an empty stream
    # until its own client-side timeout fires). 60s is comfortably above
    # typical reasoning-tier latency while still letting a stuck call
    # surface as a real `litellm.Timeout` that the SSE layer can report.
    request_timeout: float = 60.0

    # ── 跨进程并发协调 (Redis distributed semaphore) ──
    # True 时所有 backend 进程通过 Redis ZSET 共享同一份 per-model 并发计数,
    # 加起来不会超过 _MODEL_CONCURRENCY 的限额——避免多个管线 backend 共用同一个
    # Z.AI API key 时本地 semaphore 各算各的、加起来超出服务端 per-key 上限触发 429.
    # False 时退回原来的进程内 asyncio.Semaphore (适合单进程开发调试).
    # Redis 不可达时自动降级到进程内 semaphore + 一次 warning 日志.
    distributed_semaphore_enabled: bool = True

    # 单个 token 在 Redis ZSET 里的最长持有时间. 超时后下一次 acquire 把它清掉,
    # 避免进程崩溃留下"幽灵槽位"永不释放. 默认 120s = LLM request_timeout (60s)
    # 留 2x 安全余量, 调用真在跑时不会被误清.
    distributed_semaphore_hold_timeout_seconds: float = 120.0

    # ══════════════════════════════════════════════════════════════
    # Per-site LLM profile overrides
    # ══════════════════════════════════════════════════════════════
    # Every LLM call site in the project has a *profile name*. By default,
    # a profile uses its category's tier chain (primary_fast/strong →
    # fallback → china). These fields let you override the chain PER
    # CALL SITE — e.g., use Opus for Intent Parser but Haiku for everything
    # else, or route Embedded Tier-2 specifically to GLM while keeping
    # other fast-tier sites on Haiku.
    #
    # For each profile, three env vars are available (all optional):
    #   LLM_<PROFILE>_PRIMARY    — e.g., claude-opus-4-20250514
    #   LLM_<PROFILE>_FALLBACK   — e.g., openai/gpt-4o
    #   LLM_<PROFILE>_CHINA      — e.g., dashscope/qwen-max
    # Empty string means "use the default tier chain for this profile".
    #
    # Two more optional knobs per profile:
    #   LLM_<PROFILE>_TEMPERATURE — float (default: inherit global temperature)
    #   LLM_<PROFILE>_MAX_TOKENS  — int (default: inherit site's existing budget)
    #
    # Profile name → default tier is defined in
    # `src.services.llm._DEFAULT_PROFILE_TIER`. Profiles without env
    # overrides will use that tier's chain transparently.

    # ── Strong-tier profiles (Sonnet-class by default) ──
    intent_parser_primary: str = ""
    intent_parser_fallback: str = ""
    intent_parser_china: str = ""
    intent_parser_temperature: float | None = None
    intent_parser_max_tokens: int | None = None

    judge_stage_b_primary: str = ""
    judge_stage_b_fallback: str = ""
    judge_stage_b_china: str = ""
    judge_stage_b_temperature: float | None = None
    judge_stage_b_max_tokens: int | None = None

    reflect_primary: str = ""
    reflect_fallback: str = ""
    reflect_china: str = ""
    reflect_temperature: float | None = None
    reflect_max_tokens: int | None = None

    # ── Fast-tier profiles (Haiku-class by default) ──
    source_router_primary: str = ""
    source_router_fallback: str = ""
    source_router_china: str = ""
    source_router_temperature: float | None = None
    source_router_max_tokens: int | None = None

    type_classifier_primary: str = ""
    type_classifier_fallback: str = ""
    type_classifier_china: str = ""
    type_classifier_temperature: float | None = None
    type_classifier_max_tokens: int | None = None

    portal_detector_primary: str = ""
    portal_detector_fallback: str = ""
    portal_detector_china: str = ""
    portal_detector_temperature: float | None = None
    portal_detector_max_tokens: int | None = None

    judge_stage_a_primary: str = ""
    judge_stage_a_fallback: str = ""
    judge_stage_a_china: str = ""
    judge_stage_a_temperature: float | None = None
    judge_stage_a_max_tokens: int | None = None

    judge_authority_primary: str = ""
    judge_authority_fallback: str = ""
    judge_authority_china: str = ""
    judge_authority_temperature: float | None = None
    judge_authority_max_tokens: int | None = None

    finalize_primary: str = ""
    finalize_fallback: str = ""
    finalize_china: str = ""
    finalize_temperature: float | None = None
    finalize_max_tokens: int | None = None

    embedded_tier2_primary: str = ""
    embedded_tier2_fallback: str = ""
    embedded_tier2_china: str = ""
    embedded_tier2_temperature: float | None = None
    embedded_tier2_max_tokens: int | None = None

    embedded_tier3_tree_primary: str = ""
    embedded_tier3_tree_fallback: str = ""
    embedded_tier3_tree_china: str = ""
    embedded_tier3_tree_temperature: float | None = None
    embedded_tier3_tree_max_tokens: int | None = None

    embedded_cluster_review_primary: str = ""
    embedded_cluster_review_fallback: str = ""
    embedded_cluster_review_china: str = ""
    embedded_cluster_review_temperature: float | None = None
    embedded_cluster_review_max_tokens: int | None = None

    context_compressor_primary: str = ""
    context_compressor_fallback: str = ""
    context_compressor_china: str = ""
    context_compressor_temperature: float | None = None
    context_compressor_max_tokens: int | None = None


# ──────────────────────────────────────────────
# 5. Search Engine API Keys
# ──────────────────────────────────────────────
class SearchConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SEARCH_")

    # API keys
    exa_api_key: str = ""
    brave_api_key: str = ""
    tavily_api_key: str = ""
    searxng_url: str = "http://localhost:8888"
    jina_api_key: str = ""

    # Firecrawl: 优先自部署，fallback 到云端 API
    firecrawl_self_hosted_url: str = "http://firecrawl:3002"  # Docker 内部地址
    firecrawl_api_key: str = ""       # 仅云端需要；自部署留空
    firecrawl_use_self_hosted: bool = True  # True=自部署, False=云端API

    # Per-engine defaults
    default_max_results: int = 10
    default_timeout: int = 30             # seconds

    # Exa-specific
    exa_use_autoprompt: bool = True
    exa_search_type: str = "neural"       # neural / keyword

    # Tavily-specific
    tavily_search_depth: str = "advanced"  # basic / advanced
    tavily_include_raw_content: bool = False
    # 0-1 normalized semantic relevance. Tavily-tier sample distribution:
    # top-5 = 0.93/0.52/0.48/0.46/0.33; <0.3 is the long-tail noise.
    # 客户端按这个阈值过滤, 不设上下限——多少条由 Tavily score 自适应决定.
    tavily_score_threshold: float = 0.3

    # SearXNG-specific
    searxng_categories: str = "general,science,files"
    # 拉多少页. 实测 page 4 起 max_score 跌到 1.0 (单引擎位置 1, 无共识),
    # page 1-3 是 "明星 + 长尾权威源" 的甜点区, page 4+ 大量 arxiv 噪声.
    searxng_pages: int = 3
    # RRF 量纲 (Σ 1/position_in_engine). 实测分布:
    #   ≥1.0 通过 ~12%  (多引擎共识 / 单引擎排第 1)
    #   ≥0.5 通过 ~22%  (单引擎排第 2 / ≥2 引擎前几)
    #   ≥0.3 通过 ~30%  (单引擎排第 3 / 长尾起点) ← 当前默认
    #   ≥0.1 通过 ~91%  (噪声主导, 大量单引擎位置 10+ 的 arxiv/pubmed)
    # 0.3 偏宽松, 给"被埋没的权威源"更多机会进入 pipeline, 由下游 LLM URL
    # filter + Stage-A 二次过滤. 严格场景可调到 0.5 (省下游成本).
    searxng_score_threshold: float = 0.3


# ──────────────────────────────────────────────
# 6. Cache
# ──────────────────────────────────────────────
class CacheConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CACHE_")
    l1_search_ttl: int = 3600             # 1 hour
    l2_page_ttl: int = 21600              # 6 hours
    l3_judging_ttl: int = 86400           # 24 hours


# ──────────────────────────────────────────────
# 7. Budget & Pipeline Limits
# ──────────────────────────────────────────────
class BudgetConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BUDGET_")

    # Iteration control
    max_iterations: int = 3

    # Candidate limits
    max_candidates_stage_a: int = 80
    # Stage-B 精评候选数. 配合 max_concurrent_discoveries=6 一起调:
    # K × (N+2) strong + K × (N+61) fast ≤ DeepSeek 200 是"零排队"的边界.
    # 当前 K=6, N=60 → strong peak 372/200 (排队 172 ≈ 1 批 ~5s), fast peak
    # 726/200 (排队 526 ≈ 3 批 ~15s). 这是"中庸"档——质量(把 stage-A 排序
    # 30-60 名的候选也接住)与吞吐 (6 路并发用户) 兼顾. 实际各阶段错峰不会
    # 全部叠加到峰值, 真实排队比这小. 硬上限 = max_candidates_stage_a = 80.
    max_candidates_stage_b: int = 60
    # ``stage_a_batch_size`` is no longer used by the stage_a binary filter
    # or scoring (both moved to per-source parallel calls). Kept here for
    # backward-compat with anything that may still read it (run_config
    # snapshot, external tooling). Safe to remove when no callers remain.
    stage_a_batch_size: int = 10
    # Stage-A binary keep/drop is per-source (one LLM call per candidate)
    # rather than batched. Fast tier; cheap per-call but parallel makes
    # wall time ≈ slowest single call instead of batch_count × per_batch.
    stage_a_binary_max_concurrent: int = 100
    # Stage-A relevance scoring is per-source (one LLM call per candidate)
    # rather than batched. Calls fan out via asyncio.gather; this cap bounds
    # the number of in-flight scoring calls at any moment. Sits on top of
    # the per-model semaphore (deepseek-v4-pro: 200), so the effective
    # ceiling is min(this, model semaphore). 100 leaves headroom for other
    # callers (classify_urls helpers, ranker) that share the pool.
    stage_a_scoring_max_concurrent: int = 100
    relevance_threshold_stage_a: float = 5.0

    # Portal limits
    max_portals_to_map: int = 5
    max_scrape_per_portal: int = 15
    max_total_scrape: int = 40

    # Discovery limits
    max_keywords_en: int = 3              # Top N English keyword groups to search
    max_keywords_zh: int = 2              # Top N Chinese keyword groups to search
    max_sub_questions: int = 3            # Top N sub-questions to search

    # API endpoint validation
    query_min_length: int = 5
    query_max_length: int = 2000
    max_iterations_upper_bound: int = 5   # User can set max_iterations up to this

    # 单进程同时执行的 /discover 请求上限. 与 max_candidates_stage_b 联调:
    # 当前 K=6, N=60 时 strong peak K*(N+2)=372/200 (排队 ~5s), fast peak
    # K*(N+61)=726/200 (排队 ~15s). "中庸"档——拿质量 (N=60 接住 stage-A
    # 排到 60 名的候选) 但保留 6 路用户并发. 第 7 个 /discover 在 HTTP 层
    # 排队 (asyncio Semaphore 挡在 graph 入口) 等前面释放.
    # 想极致质量降到 K=4 (峰值零排队); 想高 QPS 升到 K=12 + N=30 配置.
    # 注意 fallback 是 GLM (限 5) — DeepSeek 故障时 fast 池队列会暴涨, 这是
    # 已知的"主线挂时降级冲击"代价, 与 K 关系不大.
    max_concurrent_discoveries: int = 6


# ──────────────────────────────────────────────
# 8. Rate Limiting (per provider, req/s)
# ──────────────────────────────────────────────
class RateLimitConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RATE_")
    exa: float = 5.0
    brave: float = 10.0
    tavily: float = 5.0
    searxng: float = 2.0
    # firecrawl: lifted from 3 → 5 req/s. Self-hosted firecrawl runs
    # BROWSER_POOL_SIZE=5 (docker-compose) — at rate=3 we under-utilised
    # the pool while the 162-candidate fan-out still backed up because
    # the bottleneck was actually queue-depth, not request-rate. Pairing
    # this with the firecrawl_client semaphore (cap=8) means: rate
    # limiter smooths the burst to 5/s, semaphore prevents the pool
    # from over-queuing. Each browser slot completes ~10 s/page on
    # typical sites, so 5/s × 10 s = 50 jobs in flight worst-case;
    # semaphore cap=8 is the real bound. Together: zero head-of-line
    # blocking, no 19-second rate_limit_waits.
    firecrawl: float = 5.0
    jina: float = 10.0
    openalex: float = 10.0
    semantic_scholar: float = 1.0   # API key 限额: 1 req/s cumulative across all endpoints
    huggingface: float = 5.0
    kaggle: float = 2.0
    ckan: float = 5.0
    worldbank: float = 5.0
    fred: float = 5.0
    github: float = 10.0
    apis_guru: float = 10.0
    # Financial data APIs (free tiers are very restrictive)
    polygon: float = 0.08          # 5 req/min free tier → 0.08 req/s
    alpha_vantage: float = 0.08    # 5 req/min free tier → 0.08 req/s
    # Earth observation
    nasa_earthdata: float = 5.0
    copernicus: float = 5.0
    # Biomedical (free: 3 req/s, with key: 10 req/s)
    ncbi: float = 3.0
    default_scrape: float = 2.0


# ──────────────────────────────────────────────
# 9. Scoring Weights & Thresholds
# ──────────────────────────────────────────────
class ScoringConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCORING_")

    # Dimension weights (sum to 1.0 when ALL dimensions enabled)
    weight_relevance: float = 0.40
    weight_authority: float = 0.20
    weight_freshness: float = 0.15
    weight_accessibility: float = 0.15
    weight_license_fit: float = 0.10

    # ── Dimension on/off switches (2026-05-22) ────────────────────────
    enable_freshness_dim: bool = False

    # ── v3 architecture flags (2026-05-22) ────────────────────────────
    # The original v2 architecture: Stage-A binary filter → Stage-B 5-dim
    # weighted-sum with relevance as one dim. v3 splits relevance into
    # Stage-A (precise scoring) and turns the 5 multipliers into standalone
    # dimensions evaluated by an LLM ranker in Stage-B.
    #
    # All v3 flags default True. Flip individual switches off to revert
    # specific pieces without rolling back the whole refactor.
    enable_stage_a_scoring: bool = True       # Stage-A computes relevance 0-10
    enable_stage_a_tree_judge: bool = True    # portal_trees go through Stage-A
    enable_stage_b_llm_ranker: bool = True    # Final ranking by LLM not weighted-sum
    # When True, the LLM ranker can ALSO drop sources (not just rank).
    # Drops are bounded by stage_b_llm_filter_max_drop_ratio and
    # stage_b_llm_filter_min_keep below — safety nets against the LLM
    # going haywire and dropping the entire candidate set.
    enable_stage_b_llm_filtering: bool = True
    # The LLM cannot drop more than this fraction of the input.
    # 0.6 = up to 60% may be filtered; the rest are kept regardless of
    # LLM's drop decisions (oldest-rank-first or lowest-rank-first).
    stage_b_llm_filter_max_drop_ratio: float = 0.6
    # Floor on how many survivors remain even if LLM drops aggressively.
    # When input < this, no LLM-drop happens (pool already too small).
    stage_b_llm_filter_min_keep: int = 3
    # NOTE (2026-05-22): An ``enable_llm_confirm_license_veto`` flag was
    # added then removed. License compatibility is a legal-compliance task
    # that should never be subject to LLM hallucination. Rule-based veto
    # is the only correct decision path. If the rule misclassifies, the
    # fix is to extend the alias table in ``deterministic.py``, not to
    # ask an LLM. Do not re-add an LLM layer here.

    # Hard veto thresholds
    veto_license_zero: bool = True        # license_fit==0 → eliminate
    veto_relevance_threshold: float = 4.0 # relevance < this → eliminate

    # Accessibility scores (by access level)
    access_score_open: float = 10.0
    access_score_free_reg: float = 8.0
    access_score_api_key_free: float = 7.0
    access_score_oauth: float = 6.0
    access_score_api_key_paid: float = 4.0
    access_score_paywall: float = 2.0
    access_score_unknown: float = 3.0

    # Freshness half-life (days, by domain)
    halflife_news: int = 7
    halflife_finance: int = 30
    halflife_economics: int = 30
    halflife_market: int = 30
    halflife_tech: int = 90
    halflife_health: int = 180
    halflife_science: int = 365
    halflife_research: int = 365
    halflife_government: int = 365
    halflife_geo: int = 730
    halflife_default: int = 180

    # Freshness: unknown date fallback
    freshness_unknown_score: float = 5.0

    # Retired/historical-only sources: hard cap on overall so they never
    # outrank a current alternative even when LLM-judged relevance is high.
    # Cap (not veto) because a retired dataset can still be the right answer
    # for a strictly historical query.
    retired_overall_cap: float = 4.0

    # Semantic deduplication
    dedup_cosine_threshold: float = 0.92

    # Authority metadata adjustments
    authority_citation_high: int = 10000      # >N → +1.5
    authority_citation_medium: int = 1000     # >N → +1.0
    authority_citation_low: int = 100         # >N → +0.5
    authority_download_high: int = 100000     # >N → +1.0
    authority_download_medium: int = 10000    # >N → +0.5
    authority_star_high: int = 10000          # >N → +1.0
    authority_star_medium: int = 1000         # >N → +0.5
    authority_archived_penalty: float = -1.5
    authority_issues_penalty: float = -0.5
    authority_adjustment_min: float = -2.0
    authority_adjustment_max: float = 2.0
    authority_gov_edu_bonus: float = 2.0
    authority_org_bonus: float = 0.5
    # ── Layer 3 (in-line LLM authority fallback) — 2026-05-22 ─────────
    # Measured behavior on hotel-bd: 24 LLM calls → 12 successful responses
    # → 11/12 returned score=7.0 (anchor-bias on the prompt's example value).
    # The in-line LLM is contributing almost no signal beyond "bump unknown
    # commercial domains from 5.0 → 7.0". Default-off; the bump is now done
    # deterministically via the raised ``authority_unknown_base`` below.
    # Set True to restore the old behavior.
    authority_online_llm_enabled: bool = False
    authority_llm_fallback_min: float = 3.5   # (kept for backward compat;
    authority_llm_fallback_max: float = 6.5   #  only used when above flag=True)
    # Bumped from 5.0 → 6.0 on 2026-05-22 to absorb the offline LLM's
    # observed contribution (commercial unknown domains were getting 7.0
    # via Layer 3). For an honest baseline + raised .gov/.org bonuses the
    # final scores stay close to pre-change while costing 0 LLM calls.
    authority_unknown_base: float = 6.0       # Base score for unknown domains

    # ── Relevance optimizations (2026-05-22) ──────────────────────────
    # Extract deterministic checks from the LLM relevance prompt into
    # explicit multipliers so an LLM that overlooks a hard constraint
    # can't accidentally let a wrong source rank high. Each is independent
    # and can be flipped off if a regression appears.
    enable_geographic_fit: bool = True   # source vs requirement.geographic_scope
    enable_schema_coverage_fit: bool = True   # fields_present ∩ target_schema
    enable_wrapper_url_blacklist: bool = True   # drop R/Python wrapper docs pre-Stage-A

    # Multiplier strengths (similar pattern to format_fit/temporal_fit).
    # 1.0 = no penalty; 0.5 = halve relevance; 0.0 = veto via aggregator.
    geographic_fit_mismatch_multiplier: float = 0.5
    geographic_fit_partial_multiplier: float = 0.8
    schema_coverage_zero_multiplier: float = 0.4   # 0 overlap with target schema
    schema_coverage_low_multiplier: float = 0.7    # < 50% overlap (1 of 3 fields, etc.)
    schema_coverage_low_threshold: float = 0.5


# ──────────────────────────────────────────────
# 10. Classification Confidence Levels
# ──────────────────────────────────────────────
class ClassificationConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLASSIFY_")

    # TypeClassifier confidence levels
    type_url_pattern_confidence: float = 0.8
    type_head_probe_confidence: float = 0.7
    type_llm_confidence: float = 0.6
    type_fallback_confidence: float = 0.3
    type_head_timeout: float = 5.0        # HEAD request timeout (seconds)

    # PortalDetector confidence levels
    portal_whitelist_confidence: float = 1.0
    portal_url_pattern_confidence: float = 0.85
    portal_snippet_confidence: float = 0.75

    # Pre-determined source confidence
    pre_determined_confidence: float = 1.0


# ──────────────────────────────────────────────
# 10.5 Embedded Processor
# ──────────────────────────────────────────────
class EmbeddedConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EMBEDDED_")

    # Tier-0 confidence thresholds
    tier0_json_ld_confidence: float = 1.0
    tier0_microdata_confidence: float = 0.95
    tier0_rdfa_confidence: float = 0.90
    tier0_opengraph_confidence: float = 0.85
    tier0_html_table_confidence: float = 0.90
    tier0_xhr_endpoint_confidence: float = 0.85
    tier0_table_min_rows: int = 3
    tier0_table_min_cols: int = 2

    # Tier-1 heuristic scoring
    tier1_discard_threshold: float = 0.25
    tier1_max_markdown_chars: int = 50000

    # Tier-2 LLM classification
    tier2_coverage_threshold: float = 0.6
    tier2_max_input_tokens: int = 4000
    tier2_max_output_tokens: int = 1024

    # Tier-3 list page expansion
    tier3_max_map_per_list: int = 1
    tier3_max_output_tokens: int = 2048
    tier3_url_cluster_min_count: int = 3

    # Tier-3 per-cluster-type sampling budgets.
    # Each cluster type gets its own /scrape budget. Sum is the max scrapes
    # per list page (currently 3+2+2+1 = 8, capped by tier3_max_total_scrapes).
    tier3_sample_detail_pages: int = 3          # primary /product/{id}
    tier3_sample_sub_detail_pages: int = 2      # /product/{id}/reviews
    tier3_sample_category_pages: int = 2        # /category/electronics
    tier3_sample_faceted_pages: int = 1         # /products?category=X
    tier3_max_total_scrapes: int = 8            # hard ceiling per list page

    # Tier-3 Step 2.5: LLM review of cluster classifications.
    # When enabled, Haiku reviews the rule-based classification and can
    # reclassify any cluster. EVERY populated cluster type gets reviewed
    # with its own independent scrape budget for content evidence.
    tier3_enable_llm_review: bool = True
    tier3_review_peek_per_type: int = 3            # per-cluster-type scrape budget
    tier3_review_max_total_peeks: int = 20         # global ceiling across all types
    tier3_review_peek_markdown_chars: int = 800    # peek snippet length per URL
    tier3_review_max_output_tokens: int = 1536     # larger output to cover more decisions
    tier3_review_examples_per_skeleton: int = 2    # example URLs shown per cluster
    tier3_review_top_skeletons_per_type: int = 5   # how many skeletons per category to show


# ──────────────────────────────────────────────
# 11. Context Compression
# ──────────────────────────────────────────────
class CompressionConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COMPRESS_")
    max_context_tokens: int = 200000
    buffer_tokens: int = 13000            # Reserved for LLM response
    warning_buffer_tokens: int = 20000    # Trigger warning at 80% capacity
    max_consecutive_failures: int = 3     # Circuit breaker threshold
    microcompact_keep_recent: int = 5     # Recent tool results to keep


# ──────────────────────────────────────────────
# 12. Adapter Defaults & Credentials
# ──────────────────────────────────────────────
class AdapterConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADAPTER_")

    # Global defaults
    default_timeout: int = 30             # seconds
    default_results_per_page: int = 15
    default_rate_limit: float = 5.0       # req/s
    default_burst: int = 10
    health_check_timeout: int = 10        # seconds

    # ── OpenAlex (free, no key needed, but email improves rate limit) ──
    openalex_email: str = ""              # Polite pool: higher rate limit

    # ── Semantic Scholar (free, key optional for higher limits) ──
    semantic_scholar_api_key: str = ""    # Optional: 100 req/s vs 1 req/s

    # ── HuggingFace (free, token optional for private datasets) ──
    huggingface_token: str = ""           # Optional: access gated datasets

    # ── Kaggle (requires username + key) ──
    kaggle_username: str = ""
    kaggle_key: str = ""

    # ── CKAN (free, no key) ──
    # Configurable portal URLs
    ckan_portals: str = "https://catalog.data.gov,https://data.europa.eu/api/hub/search"

    # ── FRED (free, requires key) ──
    fred_api_key: str = ""                # https://fredaccount.stlouisfed.org/apikeys

    # ── GitHub (free, token optional but 60→5000 req/hr) ──
    github_token: str = ""                # https://github.com/settings/tokens

    # ── APIs.guru (free, no key) ──
    # No credentials needed

    # ── Zenodo (free, no key for search) ──
    # No credentials needed

    # ── arXiv (free, no key) ──
    # No credentials needed

    # ── Wikidata SPARQL (free, no key) ──
    # No credentials needed

    # ── NASA EarthData (free, requires key) ──
    nasa_earthdata_api_key: str = ""      # https://urs.earthdata.nasa.gov/

    # ── Polygon.io (free tier, requires key) ──
    polygon_api_key: str = ""             # https://polygon.io/

    # ── Alpha Vantage (free tier, requires key) ──
    alpha_vantage_api_key: str = ""       # https://www.alphavantage.co/support/#api-key

    # ── Copernicus (free, requires OAuth client credentials) ──
    # 获取方式: Dashboards → OAuth clients → Create
    # 用 client_id + client_secret 换取 access_token (auto-refresh)
    copernicus_client_id: str = ""
    copernicus_client_secret: str = ""
    copernicus_token_url: str = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"

    # ── PubMed/NCBI (free, email recommended) ──
    ncbi_email: str = ""                  # Polite usage: include email
    ncbi_api_key: str = ""               # Optional: 10 req/s vs 3 req/s


# ──────────────────────────────────────────────
# 13. Coordinator (Multi-Agent)
# ──────────────────────────────────────────────
class CoordinatorConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COORD_")
    max_concurrent_research: int = 3
    max_concurrent_implementation: int = 1


# ──────────────────────────────────────────────
# 14. Validation
# ──────────────────────────────────────────────
class ValidationConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VALIDATE_")
    head_probe_timeout: float = 10.0      # URL liveness HEAD timeout (seconds)
    head_probe_verify_ssl: bool = False


# ──────────────────────────────────────────────
# 14.5 Judging-stage flags
# ──────────────────────────────────────────────
class JudgingConfig(BaseSettings):
    """Stage-A / Stage-B specific switches added 2026-05-22 when Stage-A
    moved from numeric scoring to binary filter + probe."""
    model_config = SettingsConfigDict(env_prefix="JUDGING_")

    # Stage-A URL liveness probe — runs after binary LLM decision, drops
    # 404/410/timeout candidates so Stage-B doesn't pay Sonnet cost on
    # dead URLs. Default ON. Probe uses HEAD with streamed-GET fallback.
    enable_stage_a_probe: bool = True
    # Per-URL timeout. Lower than the general head_probe_timeout because
    # we're probing many candidates in parallel and want fast failure
    # rather than waiting for slow ones — Stage-B has its own retry path.
    stage_a_probe_timeout: float = 6.0


# ──────────────────────────────────────────────
# 15. Auth
# ──────────────────────────────────────────────
class AuthConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTH_")
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440        # 24 hours
    api_key_prefix: str = "dsa_"


# ──────────────────────────────────────────────
# 15.5 Skill Library (browser-harness-style self-improving classifier)
# ──────────────────────────────────────────────
class SkillsConfig(BaseSettings):
    """Skill library: regex index → described-URL entries (one classify.yaml
    per eTLD+1). NO confidence / counters — a skill is trusted by default and
    fixed/deleted on error via the closed loop (in-run self-correction +
    harness feedback). See ``backend/src/services/skill_library.py``.
    """

    model_config = SettingsConfigDict(env_prefix="SKILLS_")

    # Master switch — when False, skip skill lookup / injection / proposal.
    enabled: bool = True

    # Where the per-domain classify.yaml files live. Relative paths resolve
    # against the backend working directory.
    workspace_dir: str = "agent-workspace/domain-skills"


# ──────────────────────────────────────────────
# 15.5b Memory library (cross-run prose notes)
# ──────────────────────────────────────────────
class MemoryConfig(BaseSettings):
    """Memory library: free-text cross-run notes (narrative / strategy / why)
    — the prose counterpart to the structured skill library. One markdown file
    per topic under workspace_dir; the agent grows it in-run via memory_append
    and the full set is injected into the system prompt at session start. See
    ``backend/src/services/memory_library.py``.
    """

    model_config = SettingsConfigDict(env_prefix="MEMORY_")

    # Master switch — when False, skip memory injection / append.
    enabled: bool = True

    # Where the per-topic <topic>.md notes live. Relative paths resolve
    # against the backend working directory.
    workspace_dir: str = "agent-workspace/memory"


# ──────────────────────────────────────────────
# 15.6 Diagnostics (per-query summary for external ReviewAgent)
# ──────────────────────────────────────────────
class DiagnosticsConfig(BaseSettings):
    """Per-query diagnostics writeback for the external ReviewAgent loop.

    Each `/discover` run drops one structured JSON file at
    ``<workspace_dir>/<YYYY-MM-DD>/<query_id>.json`` summarizing tool-call
    histograms, unresolved critic gaps, domain coverage, and per-tool
    signals (zero_results / duplicate_fetch / adapter_fallback / ...).

    The file is consumed offline by a separate ReviewAgent that proposes
    code-level changes (new adapters, new tools, prompt edits) as git PRs.
    Nothing in the runtime pipeline reads these files back.
    """

    model_config = SettingsConfigDict(env_prefix="DIAGNOSTICS_")

    enabled: bool = True
    workspace_dir: str = "agent-workspace/diagnostics"

    # Per-call signal thresholds — kept here so the ReviewAgent can be told
    # "these are the thresholds that generated the signals in the corpus".
    slow_call_ms: int = 30000        # tool call duration > this → `slow_call`
    slow_probe_ms: int = 5000        # probe_url response time > this → `slow_probe`


# ──────────────────────────────────────────────
# 15.7 Tool change proposals (agent → ReviewAgent)
# ──────────────────────────────────────────────
class ToolProposalsConfig(BaseSettings):
    """Agent-driven proposals for tool changes consumed by the offline
    ReviewAgent.

    Each /discover run MAY write zero or more proposals to
    ``<workspace_dir>/<YYYY-MM-DD>/<query_id>.jsonl``. Optional from
    the agent's perspective — no quota the agent must hit, no penalty
    for never calling these tools. They exist so the agent can voice
    "I wished I had X" / "tool Y was misleading" / "tool Z was useless
    in this run" without breaking flow.

    The ReviewAgent reads these alongside diagnostics/ when generating
    code-level PRs.
    """

    model_config = SettingsConfigDict(env_prefix="TOOL_PROPOSALS_")

    # Master switch. Set False to disable the three propose_* MCP tools
    # entirely (they become no-ops returning {disabled: True}).
    enabled: bool = True

    # Where the JSONL files live. Symmetric with diagnostics/.
    workspace_dir: str = "agent-workspace/tool-proposals"

    # Hard cap per query to keep a runaway LLM from filling disk with
    # repeated identical proposals. 0 = unlimited. When the cap is hit
    # the call returns {accepted: False, reason: "per_query_cap_reached"}
    # so the agent learns to back off without raising an error.
    max_per_query: int = 20


# ──────────────────────────────────────────────
# 16. Observability
# ──────────────────────────────────────────────
class ObservabilityConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OBS_")
    langsmith_api_key: str = ""
    langsmith_project: str = "datasource-discovery"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    enabled: bool = True


# ──────────────────────────────────────────────
# Root Container
# ──────────────────────────────────────────────
class Settings(BaseSettings):
    """Root settings — all sub-configs instantiated once at startup."""

    app: AppConfig = Field(default_factory=AppConfig)
    db: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    classification: ClassificationConfig = Field(default_factory=ClassificationConfig)
    embedded: EmbeddedConfig = Field(default_factory=EmbeddedConfig)
    compression: CompressionConfig = Field(default_factory=CompressionConfig)
    adapter: AdapterConfig = Field(default_factory=AdapterConfig)
    coordinator: CoordinatorConfig = Field(default_factory=CoordinatorConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    diagnostics: DiagnosticsConfig = Field(default_factory=DiagnosticsConfig)
    tool_proposals: ToolProposalsConfig = Field(default_factory=ToolProposalsConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    judging: JudgingConfig = Field(default_factory=JudgingConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)


settings = Settings()
