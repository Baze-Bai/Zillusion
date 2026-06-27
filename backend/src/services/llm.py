"""Multi-provider LLM service with automatic fallback, cost tracking, and structured output.

Uses litellm as the unified interface across ALL providers:
  - Claude (Anthropic)       — primary
  - GPT (OpenAI)             — fallback
  - Qwen (Alibaba DashScope) — china / fallback
  - GLM (Zhipu Z.AI)         — china / fallback
  - MiniMax                   — china / fallback

Fallback chain: primary → fallback → china (if configured).
Uses instructor for Pydantic structured output extraction.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
from typing import Any, TypeVar

import instructor
import litellm
import redis.asyncio as redis
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings
from src.services.run_logger import log_event

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Suppress litellm debug noise
litellm.suppress_debug_info = True


# ═══════════════════════════════════════════════════════════════
# DeepSeek V4 pricing registration
# ═══════════════════════════════════════════════════════════════
# litellm ships with built-in pricing tables for many models but does
# NOT yet include DeepSeek V4 (deepseek-v4-pro / deepseek-v4-flash).
# Without explicit registration, `litellm.completion_cost(response)`
# returns 0.0 for these models, which silently zeroes out the cost
# accumulated by parse_intent / judge / reflect / authority stages.
# Result: diagnostics.cost_usd under-reports actual spend by ~4-8%.
#
# The numbers below are the CURRENT pricing on api.deepseek.com as of
# 2026-05-15 (per the official pricing table). The v4-pro entries are
# the DISCOUNTED rates from the 75%-off promo currently in effect.
# When the promo ends, the v4-pro values should be updated to the list
# prices: $0.0145 cache hit, $1.74 cache miss, $3.48 output per 1M.
#
# We register under BOTH the prefixed (deepseek/deepseek-v4-pro, used
# by litellm.completion calls) and unprefixed (deepseek-v4-pro, used
# by Claude Agent SDK + ResultMessage) forms — litellm's cost lookup
# checks the response.model field which can be either depending on
# call path.
# ═══════════════════════════════════════════════════════════════


_DEEPSEEK_V4_PRO_PRICING: dict[str, Any] = {
    # Per-token = per-1M / 1_000_000
    "input_cost_per_token": 0.435 / 1_000_000,            # cache miss
    "input_cost_per_token_cache_hit": 0.003625 / 1_000_000,
    "cache_read_input_token_cost": 0.003625 / 1_000_000,
    "cache_creation_input_token_cost": 0.0,                # DeepSeek doesn't charge for cache write
    "output_cost_per_token": 0.87 / 1_000_000,
    "litellm_provider": "deepseek",
    "max_tokens": 8192,
    "max_input_tokens": 131072,
    "max_output_tokens": 8192,
    "mode": "chat",
    "supports_prompt_caching": True,
    "supports_function_calling": True,
    "supports_tool_choice": True,
    "supports_response_schema": True,
    "supports_system_messages": True,
}

_DEEPSEEK_V4_FLASH_PRICING: dict[str, Any] = {
    "input_cost_per_token": 0.14 / 1_000_000,
    "input_cost_per_token_cache_hit": 0.0028 / 1_000_000,
    "cache_read_input_token_cost": 0.0028 / 1_000_000,
    "cache_creation_input_token_cost": 0.0,
    "output_cost_per_token": 0.28 / 1_000_000,
    "litellm_provider": "deepseek",
    "max_tokens": 8192,
    "max_input_tokens": 131072,
    "max_output_tokens": 8192,
    "mode": "chat",
    "supports_prompt_caching": True,
    "supports_function_calling": True,
    "supports_tool_choice": True,
    "supports_response_schema": True,
    "supports_system_messages": True,
}

try:
    litellm.register_model({
        # Prefixed forms (used by litellm.completion / litellm.acompletion)
        "deepseek/deepseek-v4-pro": _DEEPSEEK_V4_PRO_PRICING,
        "deepseek/deepseek-v4-flash": _DEEPSEEK_V4_FLASH_PRICING,
        # Unprefixed forms (used by Claude Agent SDK runtime + reported
        # back in ResultMessage.model). litellm.completion_cost() looks
        # up response.model verbatim, so both forms need to exist.
        "deepseek-v4-pro": _DEEPSEEK_V4_PRO_PRICING,
        "deepseek-v4-flash": _DEEPSEEK_V4_FLASH_PRICING,
    })
    logger.info(
        "Registered DeepSeek V4 pricing with litellm: "
        "v4-pro $%.4f/M in / $%.4f/M out (75%% promo), "
        "v4-flash $%.4f/M in / $%.4f/M out",
        _DEEPSEEK_V4_PRO_PRICING["input_cost_per_token"] * 1_000_000,
        _DEEPSEEK_V4_PRO_PRICING["output_cost_per_token"] * 1_000_000,
        _DEEPSEEK_V4_FLASH_PRICING["input_cost_per_token"] * 1_000_000,
        _DEEPSEEK_V4_FLASH_PRICING["output_cost_per_token"] * 1_000_000,
    )
except Exception as e:
    logger.warning(
        "Failed to register DeepSeek V4 pricing with litellm: %s — "
        "completion_cost() will return $0 for these models, "
        "diagnostics.cost_usd will under-report",
        e,
    )

# ═══════════════════════════════════════════════════════════════
# 按模型并发限制 (per-model semaphore)
# ═══════════════════════════════════════════════════════════════
# Z.AI GLM 系列各模型的并发限制 (实测 + 官方文档)
# 注意: docs.z.ai 公布的 "Concurrency limit" 与服务端实际行为有显著偏差,
# 已用本项目 ZAI_API_KEY 在 https://api.z.ai/api/anthropic 实测过
# (见 rate_limit_test/), 取多轮 burst=30 的最小成功数作为安全上限.
#
# 2026-05-15 复测: coding-plan 端点 (https://api.z.ai/api/coding/paas/v4) 上
# glm-5.1 与 anthropic 端点同账户共享同一组并发配额, 稳态 5 并发, 偶有 burst
# tolerance 透到 10 (见 rate_limit_test/glm51_coding_burst*.py). 因此本表数值
# 同时适用于 anthropic 端点和 coding 端点, 无需为新 base 单独维护.
# ═══════════════════════════════════════════════════════════════
_MODEL_CONCURRENCY: dict[str, int] = {
    # ── Z.AI GLM ──
    # 服务端 per-key 总并发上限. 多 backend 进程共用同一 key 时, 通过
    # Redis distributed semaphore (见 _RedisModelSemaphore) 跨进程协调,
    # 加起来不超过这里的数字, 避免 1302 "Rate limit reached for requests".
    # 实测端点: https://api.z.ai/api/anthropic/v1/messages, 触发 429 时
    # 返回 {"error":{"code":"1302",...}}. 服务端不返回 X-RateLimit-* /
    # Retry-After 等头, 只能通过 burst 测试反推, 因此每次改值都建议复测.
    #
    # 标 "实测" = 用本项目 key 实际验证 (取 min 多轮观测做保守值).
    # 标 "doc"  = 官方文档值, 因账户余额/订阅无法实测 (1113/1311).
    # Language Models
    "glm-5.1": 5,                       # 实测 5-11 (floor 5 in n<=15, 偶尔 race 漂到 11 in n>=20), 文档声称 10
    "glm-5": 5,                         # 实测 5-8 (服务端转 glm-5.1), 文档声称 2
    "glm-5-turbo": 6,                   # 实测 6-10, 文档声称 1
    "glm-5v-turbo": 1,                  # doc, 当前账户订阅未含 (1311)
    "glm-4.7": 5,                       # 实测 5-9, 文档声称 2
    "glm-4.7-flash": 1,                 # doc, 端点不健康 (30s timeout)
    "glm-4.7-flashx": 3,                # doc, 当前账户余额不足 (1113)
    "glm-4.6": 5,                       # 实测 5-10, 文档声称 3
    "glm-4.6v": 6,                      # 实测 6-13, 文档声称 10
    "glm-4.6v-flash": 1,                # doc, 当前账户余额不足或服务过载 (1113/1305)
    "glm-4.6v-flashx": 3,               # doc, 当前账户余额不足 (1113)
    "glm-4.5": 5,                       # 实测 5-8, 文档声称 10
    "glm-4.5v": 5,                      # 实测 5-8, 文档声称 10
    "glm-4.5-flash": 2,                 # 实测 2-3, 文档声称 2 (相符)
    "glm-4.5-air": 5,                   # 实测 5-11, 文档声称 5 (下界相符)
    "glm-4.5-airx": 5,                  # doc, 当前账户余额不足 (1113)
    "glm-4-plus": 20,                   # doc, 当前账户余额不足 (1113), 项目 .env 已切走
    "glm-4-32b-0414-128k": 15,          # doc, 当前账户余额不足 (1113), 项目 .env 已切走
    "autoglm-phone-multilingual": 11,   # 实测 11-16, 文档声称 5
    "glm-ocr": 2,                       # doc, 当前账户余额不足 (1113)
    # ── DeepSeek (https://api.deepseek.com, OpenAI-compatible) ──
    # 实测 (2026-05-07, rate_limit_test/rate_limit_deepseek_high.py):
    #   burst=480 仍 100% 200, 服务端 per-key 上限远高于 480.
    # 设 200 = 留 2.4x 余量给峰值场景 (单请求 ~50 fast + ~30 strong, 4 路
    # /discover 同时 → ~320 fast 峰值, 200 足够). 想要更激进可调到 400.
    # 注意: fallback 链是 GLM (限 5), DeepSeek 故障时会被压到 5, 别被本表
    # 200 误导以为系统永远能跑 200 路 LLM.
    "deepseek-v4-pro": 200,             # 实测 burst=480 全过, 真实上限 >>480
    "deepseek-v4-flash": 200,           # 实测 burst=480 全过, 真实上限 >>480
    # Image / Video / Audio (项目目前不调用, 不在 /v1/messages 端点, 未实测)
    "glm-image": 1,                     # doc, 当前账户余额不足 (1113)
    "cogview-4-250304": 5,
    "glm-asr-2512": 5,
    "viduq1-text": 5,
    "viduq1-image": 5,
    "viduq1-start-end": 5,
    "vidu2-image": 5,
    "vidu2-start-end": 5,
    "vidu2-reference": 5,
    "cogvideox-3": 1,
    # ── Claude (OAI-compatible endpoints) ──
    # Anthropic native has ~50 rpm default, very generous
    "claude-sonnet-4-20250514": 20,
    "claude-haiku-4-5-20251001": 50,
    # ── OpenAI ──
    "gpt-4o": 20,
    "gpt-4o-mini": 100,
    # ── DashScope / Qwen (default estimates) ──
    "qwen-max": 5,
    "qwen-plus": 10,
    "qwen-turbo": 15,
    # Default for unlisted models (conservative)
    "__default__": 3,
}

# 每个模型独立的进程内 semaphore (Redis 不可达时的降级路径).
# 多进程部署 (例: pipeline A2 + B2 各跑自己的 backend) 共用同一个 Z.AI API key 时,
# 两边的进程内 semaphore 互不知情, 加起来会超过服务端的 per-key 限额 → 触发 429.
# 因此默认改用下面的 Redis distributed semaphore, 只在 Redis 不可达时回退到这里.
_model_semaphores: dict[str, asyncio.Semaphore] = {}


def _normalize_model_name(model: str) -> str:
    """Strip provider prefix: 'zai/glm-4.6' → 'glm-4.6'."""
    if "/" in model:
        return model.split("/", 1)[1].lower()
    return model.lower()


# ── Adaptive concurrency (AIMD) on 429 ──
# DeepSeek (and increasingly other providers) dynamically tightens per-key
# concurrency under load and returns 429 the moment we exceed it. The static
# `_MODEL_CONCURRENCY` table is just our *upper* cap; we additionally do
# AIMD-style flow control: shrink the per-model cap on 429 (multiplicative
# decrease, halve), grow it back after a streak of successful calls
# (additive increase, +1 every ADAPTIVE_RAMP_UP_THRESHOLD successes), capped
# at the static value. Both `_get_local_semaphore` and `_RedisModelSemaphore`
# read the live cap via `_get_concurrency_limit`, so changes take effect on
# the next semaphore acquire. In-flight calls run under the cap they already
# acquired, which is fine — the goal is to throttle *future* arrivals.
#
# This is process-local state. Multi-process deployments will each tune their
# own copy, which is acceptable: each process still respects the global
# Redis-side count, and a tightening signal usually arrives nearly
# simultaneously across processes (they share the same upstream).
_adaptive_limits: dict[str, int] = {}
_consecutive_ok: dict[str, int] = {}

# Per-model retry budget on 429 BEFORE falling through to the next model in
# the chain. Each retry releases the semaphore slot during backoff so other
# callers can proceed. With base=2s and exponential growth the worst-case
# wait per model is ~2 + 4 = ~6s before fallback (with jitter ±50%).
RATE_LIMIT_MAX_RETRIES = 2
RATE_LIMIT_BACKOFF_BASE = 2.0
ADAPTIVE_FLOOR = 1
ADAPTIVE_RAMP_UP_THRESHOLD = 20


def _get_concurrency_limit(model: str) -> int:
    """Look up the LIVE concurrency limit (adaptive cap if shrunk, else static)."""
    name = _normalize_model_name(model)
    static = _MODEL_CONCURRENCY.get(name, _MODEL_CONCURRENCY["__default__"])
    return _adaptive_limits.get(name, static)


def _is_rate_limit_error(e: BaseException) -> bool:
    """Detect 429-class errors across litellm provider variants.

    litellm normalises most provider 429s to `litellm.RateLimitError`, but
    some upstreams stuff the rate-limit message into `BadRequestError` or
    plain `Exception` with the status in the message body — so we also
    string-match. False positives here just trigger backoff+retry which is
    benign; false negatives skip rate-limit handling and fall through to
    the next model immediately, which is the existing behaviour.
    """
    if isinstance(e, litellm.RateLimitError):
        return True
    msg = str(e).lower()
    return (
        "ratelimiterror" in msg
        or "rate limit" in msg
        or "rate_limit" in msg
        or " 429" in msg
        or "'429'" in msg
        or '"429"' in msg
    )


def _on_rate_limit(model: str) -> None:
    """Multiplicative decrease: halve the model's adaptive cap (floor=1)."""
    name = _normalize_model_name(model)
    static = _MODEL_CONCURRENCY.get(name, _MODEL_CONCURRENCY["__default__"])
    cur = _adaptive_limits.get(name, static)
    new = max(ADAPTIVE_FLOOR, cur // 2)
    if new != cur:
        _adaptive_limits[name] = new
        _consecutive_ok[name] = 0
        logger.warning(
            "adaptive_limit.shrink: %s %d → %d (429 hit; static cap=%d)",
            name, cur, new, static,
        )


def _on_call_success(model: str) -> None:
    """Additive increase: after N consecutive successes, grow cap by 1.

    Cleared back to the static cap when ramped fully — we want the dict to
    stay empty in the steady state so the hot path is a plain dict miss.
    """
    name = _normalize_model_name(model)
    static = _MODEL_CONCURRENCY.get(name, _MODEL_CONCURRENCY["__default__"])
    cur = _adaptive_limits.get(name)
    if cur is None or cur >= static:
        return  # not under shrinkage; nothing to ramp
    streak = _consecutive_ok.get(name, 0) + 1
    if streak < ADAPTIVE_RAMP_UP_THRESHOLD:
        _consecutive_ok[name] = streak
        return
    new = min(static, cur + 1)
    _consecutive_ok[name] = 0
    if new >= static:
        _adaptive_limits.pop(name, None)
        _consecutive_ok.pop(name, None)
    else:
        _adaptive_limits[name] = new
    logger.info(
        "adaptive_limit.grow: %s %d → %d (after %d consecutive ok; static cap=%d)",
        name, cur, new, ADAPTIVE_RAMP_UP_THRESHOLD, static,
    )


def _rate_limit_backoff_seconds(attempt: int) -> float:
    """Jittered exponential backoff: base * 2^attempt with ±50% jitter.

    attempt=0 → ~1-3s, attempt=1 → ~2-6s, attempt=2 → ~4-12s. Jitter avoids
    the thundering-herd problem where ~45 parallel Tier-2 calls all hit 429
    at the same instant and would otherwise retry in lockstep.
    """
    base = RATE_LIMIT_BACKOFF_BASE * (2 ** attempt)
    return random.uniform(0.5 * base, 1.5 * base)


def _get_local_semaphore(model: str) -> asyncio.Semaphore:
    """Process-local fallback semaphore (used when Redis is unavailable)."""
    name = _normalize_model_name(model)
    if name not in _model_semaphores:
        limit = _get_concurrency_limit(model)
        _model_semaphores[name] = asyncio.Semaphore(limit)
        logger.debug("Created local semaphore for %s with limit=%d", name, limit)
    return _model_semaphores[name]


# ═══════════════════════════════════════════════════════════════
# Redis distributed semaphore
# ═══════════════════════════════════════════════════════════════
# 跨进程协调 LLM 并发——多个 backend 进程共用一份 Z.AI API key 时,
# 进程内 asyncio.Semaphore 各算各的, 加起来很容易超过服务端 per-key 限额.
# 把闸门搬到 Redis, 所有进程在同一个 ZSET 上竞争, 加起来不超过 limit.
#
# 算法 (Salvatore Sanfilippo "Redis in Action" 6.3 / 公平有序集合信号量):
#   key = "dsa:llm_sem:<model_name>"
#   acquire (Lua 原子):
#     1. ZREMRANGEBYSCORE key -inf (now - hold_timeout)   # 清掉崩溃进程留下的 token
#     2. count = ZCARD key
#     3. count < limit ? ZADD key now <my_token>, return 1 : return 0
#   release: ZREM key <my_token>
#
# 进程崩溃 → token 被 hold_timeout 清掉, 不会永久占槽.
# Redis 不可达 → 自动降级到进程内 asyncio.Semaphore, 加 warning 日志.
# ═══════════════════════════════════════════════════════════════

_REDIS_SEM_KEY_PREFIX = "dsa:llm_sem:"

# Lua 脚本 ARGV: [limit, now_ms, hold_timeout_ms, token]
# KEYS[1]: ZSET 的 key
# 返回: 1 = 获得槽位, 0 = 满
_LUA_ACQUIRE = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local now_ms = tonumber(ARGV[2])
local hold_timeout_ms = tonumber(ARGV[3])
local token = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms - hold_timeout_ms)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now_ms, token)
    redis.call('PEXPIRE', key, hold_timeout_ms * 2)
    return 1
end
return 0
"""

# 模块级单例: 一个 backend 进程一份 Redis 客户端连接池.
# Lazy-init 在第一次 acquire 时建, 失败缓存 None 让所有调用走 local 降级.
_redis_client: redis.Redis | None = None
_redis_init_lock: asyncio.Lock | None = None
_redis_unavailable: bool = False  # True 表示已经探测过 Redis 不可达, 不再重试
_lua_acquire_script: Any = None   # redis.commands.core.Script 实例


def _get_init_lock() -> asyncio.Lock:
    """Lazy-create init lock (must be on the running event loop)."""
    global _redis_init_lock
    if _redis_init_lock is None:
        _redis_init_lock = asyncio.Lock()
    return _redis_init_lock


async def _ensure_redis() -> redis.Redis | None:
    """Return the connected Redis client, or None if Redis is unavailable.

    Probes the connection on first call. If the probe fails we mark Redis
    as permanently unavailable for this process — falling back to local
    semaphores, with a one-time warning. Restart the process to retry.
    """
    global _redis_client, _redis_unavailable, _lua_acquire_script
    if _redis_unavailable:
        return None
    if _redis_client is not None:
        return _redis_client
    async with _get_init_lock():
        if _redis_unavailable:
            return None
        if _redis_client is not None:
            return _redis_client
        try:
            client = redis.from_url(settings.redis.url, decode_responses=True)
            await client.ping()
            _lua_acquire_script = client.register_script(_LUA_ACQUIRE)
            _redis_client = client
            logger.info(
                "redis_semaphore.connected url=%s",
                settings.redis.url,
            )
            return client
        except Exception as e:
            _redis_unavailable = True
            logger.warning(
                "redis_semaphore.unavailable falling_back_to_local "
                "url=%s err_type=%s err=%s",
                settings.redis.url, type(e).__name__, e,
            )
            return None


class _RedisModelSemaphore:
    """Distributed counting semaphore for a single model, backed by Redis ZSET.

    Per-call (NOT singleton): each `async with` gets a fresh instance because
    the acquired token is held as instance state. Construction is cheap.
    """

    def __init__(self, model_name: str, limit: int, hold_timeout_ms: int) -> None:
        self._key = _REDIS_SEM_KEY_PREFIX + model_name
        self._model_name = model_name
        self._limit = limit
        self._hold_timeout_ms = hold_timeout_ms
        self._token: str | None = None
        self._client: redis.Redis | None = None

    async def __aenter__(self) -> "_RedisModelSemaphore":
        client = await _ensure_redis()
        if client is None:
            # Redis 不可达, 降级: 在 __aenter__ 里直接 await 本地 semaphore.
            # 用 enter_async_context 的方式把生命周期挂在自己身上.
            self._local = _get_local_semaphore(self._model_name)
            await self._local.acquire()
            self._fallback_acquired = True
            return self
        self._client = client
        token = uuid.uuid4().hex
        attempt = 0
        while True:
            try:
                now_ms = int(time.time() * 1000)
                ok = await _lua_acquire_script(
                    keys=[self._key],
                    args=[self._limit, now_ms, self._hold_timeout_ms, token],
                    client=client,
                )
            except Exception as e:
                # Redis 闪断: 走本地降级, 不阻塞调用.
                logger.warning(
                    "redis_semaphore.acquire_error model=%s err_type=%s err=%s "
                    "falling_back_to_local",
                    self._model_name, type(e).__name__, e,
                )
                self._client = None
                self._local = _get_local_semaphore(self._model_name)
                await self._local.acquire()
                self._fallback_acquired = True
                return self
            if int(ok) == 1:
                self._token = token
                return self
            # 满: 退避后重试. 上限 0.5s 是为了让 LLM 调用完成后能较快接班——
            # LLM 单次调用 1-5s, 用 50-500ms 退避只增加 ~5% 队列等待开销.
            attempt += 1
            wait = min(0.05 * (2 ** min(attempt, 5)), 0.5)
            await asyncio.sleep(wait)

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if getattr(self, "_fallback_acquired", False):
            try:
                self._local.release()
            finally:
                self._fallback_acquired = False
            return
        if self._token and self._client is not None:
            try:
                await self._client.zrem(self._key, self._token)
            except Exception as e:
                # ZREM 失败不要抛——token 在 hold_timeout 后会被下一个 acquire 清掉.
                logger.warning(
                    "redis_semaphore.release_error model=%s token=%s err=%s",
                    self._model_name, self._token[:8], e,
                )
            finally:
                self._token = None


def _get_model_semaphore(model: str) -> _RedisModelSemaphore | asyncio.Semaphore:
    """Return an async-context-manager that gates concurrent calls for `model`.

    Default path: a fresh `_RedisModelSemaphore` (cross-process coordination).
    If `LLM_DISTRIBUTED_SEMAPHORE_ENABLED=false`, returns the legacy
    process-local `asyncio.Semaphore` singleton.
    """
    name = _normalize_model_name(model)
    if not settings.llm.distributed_semaphore_enabled:
        return _get_local_semaphore(model)
    limit = _get_concurrency_limit(model)
    timeout_ms = int(settings.llm.distributed_semaphore_hold_timeout_seconds * 1000)
    return _RedisModelSemaphore(name, limit, timeout_ms)


class LLMUsage(BaseModel):
    """Token usage and cost for a single LLM call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    latency_ms: float = 0.0
    cached_tokens: int = 0          # prompt_tokens served from cache (when provider reports it)
    reasoning_tokens: int = 0       # thinking-mode reasoning tokens (when reported)


def _provider_of(model: str) -> str:
    """Return the provider prefix (e.g. 'deepseek', 'zai', 'anthropic'); '' if none."""
    if not model:
        return ""
    if "/" in model:
        return model.split("/", 1)[0].lower()
    name = model.lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith("gpt") or name.startswith("o1") or name.startswith("o3"):
        return "openai"
    if name.startswith("glm"):
        return "zai"
    if name.startswith("qwen"):
        return "dashscope"
    if name.startswith("deepseek"):
        return "deepseek"
    return ""


# ═══════════════════════════════════════════════════════════════
# Per-site LLM profile registry
# ═══════════════════════════════════════════════════════════════
# Each call site has a named profile. Profile resolution order:
#   1. Per-profile env-var overrides (primary/fallback/china)
#   2. Profile's default tier chain (this map)
#
# Profile names are stable identifiers used by:
#   - llm_service.complete(profile="<name>", ...)
#   - llm_service.complete_structured(profile="<name>", ...)
#   - config fields like settings.llm.<name>_primary etc.
_DEFAULT_PROFILE_TIER: dict[str, str] = {
    # Strong-tier profiles (Sonnet-class by default)
    "intent_parser":           "strong",
    "judge_stage_b":           "strong",
    "judge_stage_a_scoring":   "strong",   # v3: Stage-A relevance scoring
    "reflect":                 "strong",
    "classify_urls":           "strong",   # hybrid URL classifier helper

    # Fast-tier profiles (Haiku-class by default)
    "source_router":           "fast",
    "type_classifier":         "fast",
    "portal_detector":         "fast",
    "judge_stage_a":           "fast",    # v3: Stage-A binary filter
    "judge_authority":         "fast",
    "finalize":                "fast",
    "embedded_tier2":          "fast",
    "embedded_tier3_tree":     "fast",
    "embedded_cluster_review": "fast",
    "context_compressor":      "fast",
}


def _resolve_profile_chain(profile: str) -> list[str]:
    """Resolve a profile name → ordered list of models (primary, fallback, china).

    Order of precedence:
      1. If any of {profile}_primary/fallback/china env overrides are set in
         settings.llm, use those (filtered to non-empty).
      2. Otherwise fall back to the profile's default tier chain.

    Unknown profile names fall back to the "fast" tier.
    """
    cfg = settings.llm

    # Try per-profile overrides (fields are named f"{profile}_primary" etc.)
    primary = getattr(cfg, f"{profile}_primary", "") or ""
    fallback = getattr(cfg, f"{profile}_fallback", "") or ""
    china = getattr(cfg, f"{profile}_china", "") or ""

    override_chain = [m for m in (primary, fallback, china) if m]
    if override_chain:
        return override_chain

    # No overrides — fall back to tier
    tier = _DEFAULT_PROFILE_TIER.get(profile, "fast")
    return _resolve_tier_chain(tier)


def _resolve_tier_chain(tier: str) -> list[str]:
    """Resolve a tier name → ordered list of configured models.

    Shared logic with LLMService._get_models; exposed at module level for
    use by _resolve_profile_chain.
    """
    cfg = settings.llm
    if tier == "reasoning":
        candidates = [
            cfg.primary_reasoning,
            cfg.primary_strong,
            cfg.fallback_strong,
            cfg.china_strong,
        ]
    elif tier == "strong":
        candidates = [cfg.primary_strong, cfg.fallback_strong, cfg.china_strong]
    else:  # "fast" or unknown
        candidates = [cfg.primary_fast, cfg.fallback_fast, cfg.china_fast]
    return [m for m in candidates if m]


def _resolve_profile_temperature(profile: str) -> float | None:
    """Per-profile temperature override, or None to inherit global default."""
    return getattr(settings.llm, f"{profile}_temperature", None)


def _resolve_profile_max_tokens(profile: str) -> int | None:
    """Per-profile max_tokens override, or None to inherit caller's value."""
    return getattr(settings.llm, f"{profile}_max_tokens", None)


def _try_recover_structured(exc: Exception, response_model: type[T]) -> T | None:
    """Salvage a structured response from a failed instructor call.

    Instructor (TOOLS mode) raises on multiple tool_calls or on tool_call
    parse errors even when the LLM's `message.content` already contains a
    valid JSON object that matches `response_model`. We extract that JSON
    and validate it. Returns None if no usable content can be recovered;
    callers should treat that as a real failure and fall back.
    """
    completion = getattr(exc, "last_completion", None)
    if completion is None:
        return None
    try:
        choices = getattr(completion, "choices", None) or []
        if not choices:
            return None
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None) if message is not None else None
        if not content:
            return None
        from src.utils.json_parse import extract_json, extract_pseudo_tool_call
        parsed = extract_json(content)
        if isinstance(parsed, dict):
            try:
                return response_model.model_validate(parsed)
            except Exception:
                pass
        # GLM pseudo-tool-call XML fallback. Without this, every "multiple
        # tool calls" failure burns a full provider-fallback round-trip
        # (~5–10s per call); with it, most are recovered in-place.
        pseudo = extract_pseudo_tool_call(content)
        if isinstance(pseudo, dict):
            try:
                return response_model.model_validate(pseudo)
            except Exception:
                return None
        return None
    except Exception:
        return None


class LLMService:
    """Unified LLM interface with multi-provider fallback and structured output."""

    def __init__(self) -> None:
        self._total_cost: float = 0.0
        self._call_count: int = 0
        self._usage_log: list[LLMUsage] = []

        # Initialize instructor-patched litellm client
        self._client = instructor.from_litellm(litellm.acompletion)

        # Configure China-region provider API bases (if set)
        self._setup_china_providers()

    def _setup_china_providers(self) -> None:
        """Configure DashScope / ZAI / MiniMax / DeepSeek API bases for litellm."""
        # DashScope (Qwen) — custom API base
        if settings.llm.dashscope_api_base:
            os.environ.setdefault("DASHSCOPE_API_BASE", settings.llm.dashscope_api_base)

        # ZAI (GLM/Zhipu) — custom API base (international / China BigModel)
        if settings.llm.zai_api_base:
            os.environ.setdefault("ZAI_API_BASE", settings.llm.zai_api_base)

        # MiniMax — custom API base
        if settings.llm.minimax_api_base:
            os.environ.setdefault("MINIMAX_API_BASE", settings.llm.minimax_api_base)

        # DeepSeek — custom API base (OpenAI-compatible)
        # litellm 的 deepseek/ 前缀会读 DEEPSEEK_API_KEY + DEEPSEEK_API_BASE.
        if settings.llm.deepseek_api_base:
            os.environ.setdefault("DEEPSEEK_API_BASE", settings.llm.deepseek_api_base)

    def _extra_params_for_model(self, model: str) -> dict[str, Any]:
        """Return provider-specific extra params to merge into the request.

        - GLM (zai/glm-*): inject `thinking: disabled/enabled` via extra_body.
          Required because glm-4.6+ and glm-5.x default to reasoning mode,
          which is 3-5x slower than direct answer mode.
        - DeepSeek (deepseek/deepseek-v4-*): both -pro and -flash keep
          thinking OFF for two reasons. First, enabling thinking flips the
          model into reasoner mode, which DOES NOT support `tool_choice` —
          and the strong tier's primary user is judge_stage_b which calls
          `complete_structured` (function-calling under the hood, requires
          tool_choice). Empirically: -pro + thinking + structured output
          → 100% BadRequestError "deepseek-reasoner does not support this
          tool_choice". Second, the fast tier runs ~45 parallel Tier-2
          calls; thinking would blow up end-to-end latency by 3-5x.
          Other deepseek models fall back to the global flag.
          litellm doesn't validate extra_body, so the param passes through.
        """
        extra: dict[str, Any] = {}

        if model.startswith("zai/"):
            # glm-4.6+ and glm-5.x support reasoning; control via thinking param
            thinking_type = "enabled" if settings.llm.zai_thinking_enabled else "disabled"
            # Pass via extra_body so litellm doesn't reject the unknown param
            extra["extra_body"] = {"thinking": {"type": thinking_type}}
        elif model.startswith("deepseek/"):
            if "-pro" in model or "-flash" in model:
                # Forced disabled — see docstring: tool_choice incompatibility
                # for -pro (structured output), latency budget for -flash.
                thinking_type = "disabled"
            else:
                thinking_type = "enabled" if settings.llm.deepseek_thinking_enabled else "disabled"
            extra["extra_body"] = {"thinking": {"type": thinking_type}}

        return extra

    @property
    def total_cost(self) -> float:
        return self._total_cost

    @property
    def call_count(self) -> int:
        return self._call_count

    def reset_tracking(self) -> None:
        self._total_cost = 0.0
        self._call_count = 0
        self._usage_log = []

    def _get_models(self, tier: str) -> list[str]:
        """Build model fallback chain for a tier (legacy tier-based API).

        Profiles are the preferred way to select models per-call-site; this
        method remains for backward compatibility with existing callers.

        Tiers:
          reasoning: 最强推理, 单次调用 (Intent Parser / Critic / Router)
                     → fallback to strong chain if not configured
          strong:    高并发强模型, 并行调用 (Stage-B 10 路并发)
          fast:      批量快模型 (Stage-A / TypeClassifier)
        """
        models = _resolve_tier_chain(tier)

        if not models:
            raise RuntimeError(
                f"No LLM models configured for tier '{tier}'. "
                "Set at least one of: LLM_PRIMARY_STRONG/FAST, LLM_FALLBACK_STRONG/FAST, LLM_CHINA_STRONG/FAST"
            )
        return models

    def _get_models_for_profile(self, profile: str) -> list[str]:
        """Resolve a profile name to its model fallback chain.

        Per-profile env overrides (e.g., LLM_INTENT_PARSER_PRIMARY) take
        precedence over the profile's default tier.

        Unknown profile → falls back to "fast" tier (safe default).
        """
        models = _resolve_profile_chain(profile)
        if not models:
            # No profile overrides AND no models configured for the default tier
            tier = _DEFAULT_PROFILE_TIER.get(profile, "fast")
            raise RuntimeError(
                f"No LLM models available for profile '{profile}' (default tier '{tier}'). "
                f"Set either LLM_{profile.upper()}_PRIMARY/FALLBACK/CHINA "
                f"or the tier chain LLM_PRIMARY_{tier.upper()} etc."
            )
        return models

    def _select_chain(self, profile: str | None, model_tier: str) -> list[str]:
        """Select the model fallback chain based on (profile, model_tier).

        If `profile` is given, it takes precedence. Otherwise fall back to
        tier-based resolution for backward compatibility.
        """
        if profile:
            return self._get_models_for_profile(profile)
        return self._get_models(model_tier)

    @staticmethod
    def _messages_for_model(
        messages: list[dict[str, Any]], model: str
    ) -> list[dict[str, Any]]:
        """Per-provider message shaping — currently: Anthropic prompt caching.

        For Claude models, the (stable, repeated) system message is converted
        to block form with ``cache_control: ephemeral`` so bursts of calls
        sharing one system prompt (classify_urls fans out dozens) pay ~0.1×
        input price on the cached prefix instead of full price every call.
        Anthropic silently skips prefixes below its minimum cacheable size
        (~2-4K tokens depending on model), so small prompts are a no-op.

        DeepSeek/GLM prefix-cache automatically server-side (no param), and
        block-form system content is not universally accepted — hence the
        provider gate instead of marking blindly for every model."""
        if "claude" not in model and "anthropic" not in model:
            return messages
        out: list[dict[str, Any]] = []
        marked = False
        for m in messages:
            content = m.get("content")
            if (
                not marked
                and m.get("role") == "system"
                and isinstance(content, str)
                and content
            ):
                out.append({
                    "role": "system",
                    "content": [{
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }],
                })
                marked = True
            else:
                out.append(m)
        return out

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def complete(
        self,
        messages: list[dict[str, str]],
        model_tier: str = "strong",
        max_tokens: int | None = 2048,
        temperature: float | None = None,
        profile: str | None = None,
        **kwargs: Any,
    ) -> tuple[str, LLMUsage]:
        """Raw completion — returns text response + usage.

        Args:
            messages: Chat messages in OpenAI format [{role, content}].
            model_tier: "strong" / "fast" / "reasoning". Used when `profile`
                is not supplied. Kept for backward compatibility.
            max_tokens: Max output tokens. Overridden by profile config if set.
            temperature: Override default temperature. Overridden by profile
                config if set.
            profile: Named call-site profile (e.g., "intent_parser",
                "embedded_tier2"). When given, takes precedence over
                `model_tier` and picks up per-profile env overrides for
                primary/fallback/china/temperature/max_tokens.
        """
        models = self._select_chain(profile, model_tier)
        logger.debug(
            "LLM complete [profile=%s, tier=%s]: models=%s, max_tokens=%s",
            profile or "-", model_tier, models, max_tokens,
        )
        # Profile-level temperature override (if set)
        if profile and temperature is None:
            prof_temp = _resolve_profile_temperature(profile)
            if prof_temp is not None:
                temperature = prof_temp
        # Profile-level max_tokens override (if set)
        if profile:
            prof_mt = _resolve_profile_max_tokens(profile)
            if prof_mt is not None:
                max_tokens = prof_mt
        temp = temperature if temperature is not None else settings.llm.temperature

        # max_tokens=None means "no artificial cap — provider uses its own
        # hard ceiling (deepseek-v4-pro: 8192, glm-4.6: 4096, etc.)".
        # Drop the kwarg from the litellm call so each provider applies its
        # native default rather than forcing a number our side chose.
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        # Apply a default per-call HTTP timeout so a hung provider raises
        # `litellm.Timeout` instead of blocking the SSE stream forever.
        # Caller can still override by passing `timeout=` explicitly.
        kwargs.setdefault("timeout", settings.llm.request_timeout)

        # Structured run-log: capture the call before model-loop starts.
        # `purpose` carries the call-site identity (profile name preferred —
        # "intent_parser" / "judge_stage_a" / etc. — falling back to tier).
        _purpose = profile or f"tier:{model_tier}"
        _prompt_chars_total = sum(len(m.get("content", "") or "") for m in messages)
        # Full message list — no truncation, per "全部保存" requirement.
        # Stored as the list of {role, content} dicts so multi-turn
        # context (system + user + assistant history) stays intact.
        _messages_full = [
            {"role": m.get("role", ""), "content": m.get("content", "") or ""}
            for m in messages
        ]
        log_event("llm_call", {
            "purpose": _purpose,
            "model_chain": models,
            "n_messages": len(messages),
            "prompt_chars_total": _prompt_chars_total,
            "messages": _messages_full,
            "structured": False,
            "max_tokens": max_tokens,
        })

        last_error: Exception | None = None
        for model in models:
            # Per-model retry budget on 429: hold the slot only for the call,
            # release during backoff so siblings can proceed (otherwise the
            # backoff would extend the queue head-of-line for everyone).
            for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
                try:
                    start = time.monotonic()
                    extra = self._extra_params_for_model(model)
                    last_msg = messages[-1]["content"] if messages else ""
                    logger.debug("LLM [%s] prompt (%d msgs, last %d chars): %s",
                                 model, len(messages), len(last_msg), last_msg[:500])
                    async with _get_model_semaphore(model):
                        response = await litellm.acompletion(
                            model=model,
                            messages=self._messages_for_model(messages, model),
                            temperature=temp,
                            **extra,
                            **kwargs,
                        )
                    latency = (time.monotonic() - start) * 1000

                    content = response.choices[0].message.content or ""
                    usage = self._extract_usage(response, model, latency)
                    self._track(usage)
                    _on_call_success(model)

                    logger.debug(
                        "LLM [%s] response: %d chars, %d prompt_tok, %d compl_tok, $%.4f, %.0fms | %s",
                        model, len(content), usage.prompt_tokens, usage.completion_tokens,
                        usage.cost_usd, latency, content[:500],
                    )

                    # Full response — no truncation, per "全部保存".
                    log_event("llm_response", {
                        "purpose": _purpose,
                        "model": model,
                        "provider": _provider_of(model),
                        "response_chars": len(content),
                        "response": content,
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "cached_tokens": usage.cached_tokens,
                        "reasoning_tokens": usage.reasoning_tokens,
                        "cache_hit": usage.cached_tokens > 0,
                        "cost_usd": round(usage.cost_usd, 6),
                        "latency_ms": round(latency, 1),
                    })

                    return content, usage

                except Exception as e:
                    last_error = e
                    is_rl = _is_rate_limit_error(e)
                    if is_rl:
                        _on_rate_limit(model)
                        if attempt < RATE_LIMIT_MAX_RETRIES:
                            wait = _rate_limit_backoff_seconds(attempt)
                            logger.warning(
                                "rate_limit on %s (attempt %d/%d), backing off %.1fs: %s",
                                model, attempt + 1, RATE_LIMIT_MAX_RETRIES + 1, wait, e,
                            )
                            log_event("llm_fallback", {
                                "purpose": _purpose,
                                "model": model,
                                "provider": _provider_of(model),
                                "kind": "rate_limit_backoff",
                                "attempt": attempt + 1,
                                "max_attempts": RATE_LIMIT_MAX_RETRIES + 1,
                                "wait_seconds": round(wait, 2),
                                "error_type": type(e).__name__,
                                "error": str(e)[:300],
                            })
                            await asyncio.sleep(wait)
                            continue  # retry same model
                        logger.warning(
                            "rate_limit on %s exhausted %d retries, falling back",
                            model, RATE_LIMIT_MAX_RETRIES + 1,
                        )
                    else:
                        logger.warning("LLM call failed on %s: %s, trying fallback", model, e)
                    log_event("llm_fallback", {
                        "purpose": _purpose,
                        "model": model,
                        "provider": _provider_of(model),
                        "kind": "rate_limit_exhausted" if is_rl else "error_fallthrough",
                        "error_type": type(e).__name__,
                        "error": str(e)[:300],
                    })
                    break  # exit retry loop, move to next model in chain

        # All models exhausted — log the failure so the run-log can show
        # WHY a node returned empty/errored output even though stdlib log
        # would already have the per-model warnings.
        log_event("llm_error", {
            "purpose": _purpose,
            "structured": False,
            "models_tried": models,
            "providers_tried": [_provider_of(m) for m in models],
            "error_type": type(last_error).__name__ if last_error else "unknown",
            "error_module": type(last_error).__module__ if last_error else "",
            "error": str(last_error) if last_error else "all_failed",
            "error_repr": repr(last_error)[:600] if last_error else "",
        })
        raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")

    async def complete_structured(
        self,
        messages: list[dict[str, str]],
        response_model: type[T],
        model_tier: str = "strong",
        max_tokens: int | None = 2048,
        temperature: float | None = None,
        max_retries: int = 2,
        profile: str | None = None,
        **kwargs: Any,
    ) -> tuple[T, LLMUsage]:
        """Structured output — returns parsed Pydantic model + usage.

        Uses instructor to extract structured data from LLM response.

        Args:
            profile: Optional named call-site profile. Same semantics as
                LLMService.complete — takes precedence over model_tier
                and picks up per-profile env overrides.
        """
        models = self._select_chain(profile, model_tier)
        logger.debug(
            "LLM structured [profile=%s, tier=%s]: models=%s, response_model=%s",
            profile or "-", model_tier, models, response_model.__name__,
        )
        # Profile-level overrides
        if profile and temperature is None:
            prof_temp = _resolve_profile_temperature(profile)
            if prof_temp is not None:
                temperature = prof_temp
        if profile:
            prof_mt = _resolve_profile_max_tokens(profile)
            if prof_mt is not None:
                max_tokens = prof_mt
        temp = temperature if temperature is not None else settings.llm.temperature

        # max_tokens=None semantics — see complete() for rationale.
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        # See `complete()` — same rationale: a hung structured call would
        # otherwise stall the SSE pipeline silently. `complete_structured`
        # has no @retry decorator, so the timeout is the *only* line of
        # defence against an unresponsive provider for these call sites.
        kwargs.setdefault("timeout", settings.llm.request_timeout)

        # Structured run-log entry — mirrors `complete()` but flags
        # structured=True and records the response_model name (since the
        # eventual "response" will be a Pydantic instance, not text).
        _purpose = profile or f"tier:{model_tier}"
        _prompt_chars_total = sum(len(m.get("content", "") or "") for m in messages)
        _messages_full = [
            {"role": m.get("role", ""), "content": m.get("content", "") or ""}
            for m in messages
        ]
        log_event("llm_call", {
            "purpose": _purpose,
            "model_chain": models,
            "n_messages": len(messages),
            "prompt_chars_total": _prompt_chars_total,
            "messages": _messages_full,
            "structured": True,
            "response_model": response_model.__name__,
            "max_tokens": max_tokens,
        })

        last_error: Exception | None = None
        for model in models:
          for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
            try:
                start = time.monotonic()
                extra = self._extra_params_for_model(model)
                last_msg = messages[-1]["content"] if messages else ""
                logger.debug("LLM [%s] structured prompt (%d msgs, last %d chars): %s",
                             model, len(messages), len(last_msg), last_msg[:500])
                # 按模型并发限制（避免 Z.AI/DashScope 429）
                async with _get_model_semaphore(model):
                    # instructor v1.x AsyncInstructor: create_with_completion
                    # returns (parsed_model, raw_completion). We need the raw
                    # completion to extract token usage / cost; the previous
                    # `.create()` path always reported cost_usd=0, which made
                    # cost_accumulated permanently 0 for every node that uses
                    # structured output (parse_intent / judge / classify_types
                    # / process_by_type / finalize / reflect — i.e. nearly all
                    # of them).
                    # max_tokens flows via kwargs (or omitted when None).
                    result, completion = await self._client.chat.completions.create_with_completion(
                        model=model,
                        messages=self._messages_for_model(messages, model),
                        response_model=response_model,
                        temperature=temp,
                        max_retries=max_retries,
                        **extra,
                        **kwargs,
                    )
                latency = (time.monotonic() - start) * 1000

                # Extract real usage/cost from the raw litellm ModelResponse;
                # if anything goes wrong, fall back to a zero-cost record so
                # callers don't crash.
                try:
                    usage = self._extract_usage(completion, model, latency)
                except Exception:
                    usage = LLMUsage(model=model, latency_ms=latency)
                self._track(usage)

                logger.debug("LLM [%s] structured response: %s, %.0fms | %s",
                             model, response_model.__name__, latency,
                             str(result)[:500])

                # Full structured response — no truncation, per "全部保存".
                log_event("llm_response", {
                    "purpose": _purpose,
                    "model": model,
                    "provider": _provider_of(model),
                    "response_model": response_model.__name__,
                    "response": str(result),
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "cached_tokens": usage.cached_tokens,
                    "reasoning_tokens": usage.reasoning_tokens,
                    "cache_hit": usage.cached_tokens > 0,
                    "cost_usd": round(usage.cost_usd, 6),
                    "latency_ms": round(latency, 1),
                })

                _on_call_success(model)
                return result, usage

            except Exception as e:
                last_error = e
                # GLM-class providers occasionally emit multiple tool_calls
                # in one response. Instructor refuses to merge them and
                # raises before we ever see a parsed model — but the LLM's
                # text content typically already contains a single valid
                # JSON object that satisfies response_model. Try to recover
                # it before discarding this provider's work, otherwise the
                # caller falls back to a fresh free-form call (extra
                # latency/cost) and frequently gets a *shorter* answer.
                recovered = _try_recover_structured(e, response_model)
                if recovered is not None:
                    latency = (time.monotonic() - start) * 1000
                    usage = LLMUsage(model=model, latency_ms=latency)
                    self._track(usage)
                    logger.info(
                        "Recovered structured output on %s after instructor failure: %s",
                        model, type(e).__name__,
                    )
                    # Log as a recovery path — agent caller still gets a
                    # valid result so this isn't an error from their POV;
                    # mark it explicitly via `recovered_from`.
                    log_event("llm_response", {
                        "purpose": _purpose,
                        "model": model,
                        "provider": _provider_of(model),
                        "response_model": response_model.__name__,
                        "response": str(recovered),
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "cached_tokens": 0,
                        "reasoning_tokens": 0,
                        "cache_hit": False,
                        "cost_usd": 0.0,
                        "latency_ms": round(latency, 1),
                        "recovered_from": type(e).__name__,
                    })
                    _on_call_success(model)
                    return recovered, usage
                is_rl = _is_rate_limit_error(e)
                if is_rl:
                    _on_rate_limit(model)
                    if attempt < RATE_LIMIT_MAX_RETRIES:
                        wait = _rate_limit_backoff_seconds(attempt)
                        logger.warning(
                            "rate_limit on %s structured (attempt %d/%d), backing off %.1fs: %s",
                            model, attempt + 1, RATE_LIMIT_MAX_RETRIES + 1, wait, e,
                        )
                        log_event("llm_fallback", {
                            "purpose": _purpose,
                            "model": model,
                            "provider": _provider_of(model),
                            "structured": True,
                            "kind": "rate_limit_backoff",
                            "attempt": attempt + 1,
                            "max_attempts": RATE_LIMIT_MAX_RETRIES + 1,
                            "wait_seconds": round(wait, 2),
                            "error_type": type(e).__name__,
                            "error": str(e)[:300],
                        })
                        await asyncio.sleep(wait)
                        continue  # retry same model
                    logger.warning(
                        "rate_limit on %s structured exhausted %d retries, falling back",
                        model, RATE_LIMIT_MAX_RETRIES + 1,
                    )
                else:
                    logger.warning(
                        "Structured LLM call failed on %s: %s, trying fallback", model, e
                    )
                log_event("llm_fallback", {
                    "purpose": _purpose,
                    "model": model,
                    "provider": _provider_of(model),
                    "structured": True,
                    "kind": "rate_limit_exhausted" if is_rl else "error_fallthrough",
                    "error_type": type(e).__name__,
                    "error": str(e)[:300],
                })
                break  # exit inner retry loop, move to next model in chain

        log_event("llm_error", {
            "purpose": _purpose,
            "structured": True,
            "models_tried": models,
            "providers_tried": [_provider_of(m) for m in models],
            "response_model": response_model.__name__,
            "error_type": type(last_error).__name__ if last_error else "unknown",
            "error_module": type(last_error).__module__ if last_error else "",
            "error": str(last_error) if last_error else "all_failed",
            "error_repr": repr(last_error)[:600] if last_error else "",
        })
        raise RuntimeError(f"All LLM providers failed for structured output. Last: {last_error}")

    async def batch_complete(
        self,
        items: list[Any],
        prompt_template: str,
        model_tier: str = "fast",
        batch_size: int = 10,
        max_tokens: int = 1024,
    ) -> list[tuple[str, LLMUsage]]:
        """Batch multiple items into a single LLM call per batch.

        Args:
            items: List of items to process.
            prompt_template: Template with {items} placeholder.
            batch_size: Items per LLM call.
        """
        results: list[tuple[str, LLMUsage]] = []

        for i in range(0, len(items), batch_size):
            batch = items[i : i + batch_size]
            batch_text = "\n".join(
                f"[{j+1}] {item}" for j, item in enumerate(batch)
            )
            prompt = prompt_template.format(items=batch_text, count=len(batch))

            result, usage = await self.complete(
                messages=[{"role": "user", "content": prompt}],
                model_tier=model_tier,
                max_tokens=max_tokens,
            )
            results.append((result, usage))

        return results

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a list of texts."""
        response = await litellm.aembedding(
            model=settings.llm.embedding_model,
            input=texts,
            timeout=settings.llm.request_timeout,
        )
        return [item["embedding"] for item in response.data]

    def _extract_usage(self, response: Any, model: str, latency_ms: float) -> LLMUsage:
        """Extract usage info from litellm response."""
        usage = response.usage if hasattr(response, "usage") else None
        cost = 0.0
        try:
            cost = litellm.completion_cost(completion_response=response)
        except Exception:
            pass

        cached = 0
        reasoning = 0
        if usage is not None:
            prompt_details = getattr(usage, "prompt_tokens_details", None)
            if prompt_details is not None:
                cached = int(getattr(prompt_details, "cached_tokens", 0) or 0)
            else:
                cached = int(getattr(usage, "cached_tokens", 0) or 0)
            completion_details = getattr(usage, "completion_tokens_details", None)
            if completion_details is not None:
                reasoning = int(getattr(completion_details, "reasoning_tokens", 0) or 0)

        return LLMUsage(
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            total_tokens=getattr(usage, "total_tokens", 0) if usage else 0,
            cost_usd=cost,
            model=model,
            latency_ms=latency_ms,
            cached_tokens=cached,
            reasoning_tokens=reasoning,
        )

    def _track(self, usage: LLMUsage) -> None:
        self._total_cost += usage.cost_usd
        self._call_count += 1
        self._usage_log.append(usage)
        # Funnel cost into the per-request contextvar so callers that
        # discard the usage tuple (stage_a/stage_b/authority/type_classifier
        # /finalize) still contribute to the request's running total.
        from src.utils.request_context import add_request_cost
        add_request_cost(usage.cost_usd)


# Singleton instance
llm_service = LLMService()
