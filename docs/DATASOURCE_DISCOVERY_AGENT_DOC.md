# DataSource Discovery Agent — 完整技术文档

> **版本**: v0.1.0 | **日期**: 2026-04-12
>
> 用户输入自然语言数据需求 → Agent 从全网发现三类数据源（公开 API、可下载文件、网页内嵌数据）→ 多维评估、排序、结构化输出。

---

## 目录

1. [项目概述](#1-项目概述)
2. [架构全景](#2-架构全景)
3. [技术栈](#3-技术栈)
4. [项目目录结构](#4-项目目录结构)
5. [八阶段管道详解](#5-八阶段管道详解)
6. [核心模型系统](#6-核心模型系统)
7. [搜索工具层](#7-搜索工具层)
8. [分类器系统](#8-分类器系统)
9. [评审系统 (Judge)](#9-评审系统-judge)
10. [适配器插件系统](#10-适配器插件系统)
11. [从 Claude Code 移植的架构模式](#11-从-claude-code-移植的架构模式)
12. [服务层](#12-服务层)
13. [API 接口设计](#13-api-接口设计)
14. [前端架构](#14-前端架构)
15. [部署与运维](#15-部署与运维)
16. [配置参考](#16-配置参考)
17. [开发指南](#17-开发指南)
18. [设计哲学与反模式](#18-设计哲学与反模式)

---

## 1. 项目概述

### 1.1 这是什么

DataSource Discovery Agent 是一个**生产级数据源发现框架**，核心能力是：

- 用户说 "帮我找中国 A 股历史数据"
- Agent 自动搜索 4 个搜索引擎 + 20 余个垂类数据注册表
- 识别结果类型（API / 可下载文件 / 网页内嵌数据）
- 深挖数据门户（Portal Profiler）和列表页（DataPageTree）
- 五维评分（相关性、权威性、时效性、可获取性、许可证）
- 返回结构化报告 + 使用指南

### 1.2 不是什么

| | 研究 Agent (Perplexity 类) | 本方案 |
|-|---------------------------|-------|
| 输入 | 一个问题 | 一个**数据需求** |
| 输出 | 综合答案 | **结构化数据源清单** |
| 用户后续动作 | 读答案 | 调 API / 下载文件 / 编写抽取代码 |

### 1.3 核心指标

| 指标 | 目标 |
|------|------|
| 单次查询成本 | $0.08 - $0.30 |
| 端到端延迟 | 30s - 5min |
| 最大迭代轮次 | 3 |
| 候选源上限 (Stage-B) | 30 |
| 数据源类型覆盖 | API + 文件 + 网页内嵌 |

---

## 2. 架构全景

### 2.1 系统架构图

```
┌─────────────┐    SSE     ┌──────────────────────────────────────────┐
│  Next.js    │◄──────────►│  FastAPI Backend                         │
│  Frontend   │            │                                          │
│  (port 3000)│            │  ┌─────────────────────────────────────┐ │
└─────────────┘            │  │  LangGraph 8-Stage Pipeline          │ │
                           │  │                                     │ │
                           │  │  parse_intent → route_sources →     │ │
                           │  │  discover → classify_types →        │ │
                           │  │  process_by_type → normalize →      │ │
                           │  │  judge → reflect ─┐                 │ │
                           │  │       ↑            │                │ │
                           │  │       └──(loop)────┘                │ │
                           │  │            └→ finalize → END        │ │
                           │  └─────────────────────────────────────┘ │
                           │                                          │
                           │  ┌────────┐ ┌────────┐ ┌──────────────┐ │
                           │  │ Redis  │ │Postgres│ │  Qdrant      │ │
                           │  │ Cache  │ │  持久化 │ │ 向量去重     │ │
                           │  └────────┘ └────────┘ └──────────────┘ │
                           └──────────────────────────────────────────┘
```

### 2.2 数据流

```
用户自然语言需求
      │
      ▼
[Stage 1] Intent Parser ── Sonnet → StructuredRequirement
      │
      ▼
[Stage 2] Source Router ── 四层规则表 + Haiku 补充 → 激活 Worker 集合
      │
      ▼
[Stage 3] Parallel Discovery ── Web(4引擎) + Registry(适配器) + LLM Prior
      │
      ▼
[Stage 3.5] TypeClassifier ── URL 模式 + HEAD 探测 + Haiku 批量 → 类型判定
      │
      ▼
[Stage 4] Type-Specific Processing ── API/File/Embedded 三路分流
      │
      ▼
[Stage 5] Normalize & Dedupe ── URL 规范化 + 语义聚类(cosine>0.92)
      │
      ▼
[Stage 6] Two-Stage Judging ── Haiku 快筛 → Sonnet 五维精评
      │
      ▼
[Stage 7] Reflect & Gap Check ── Critic 审视覆盖度 → 回到 Stage 2 (最多3轮)
      │
      ▼
[Stage 8] Finalize & Output ── 分组排序 + 使用指南 → FinalReport
```

---

## 3. 技术栈

| 层 | 选型 | 版本 | 用途 |
|----|------|------|------|
| **Agent 编排** | LangGraph | ≥0.2.60 | StateGraph + 条件循环 + checkpoint |
| **后端框架** | FastAPI | ≥0.115 | async + SSE 流式 + Pydantic v2 |
| **前端框架** | Next.js | 14+ | App Router + Tailwind + shadcn/ui |
| **LLM (强)** | Claude Sonnet 4.6 | via litellm | Planner / Critic / Stage-B 评审 |
| **LLM (快)** | Claude Haiku 4.5 | via litellm | 批量快筛 / 分类器 / TypeClassifier |
| **LLM 回退** | GPT-4o / GPT-4o-mini | via litellm | 自动 failover |
| **Embedding** | text-embedding-3-small | via litellm | 语义去重 |
| **主数据库** | PostgreSQL | 16 | 查询历史 / 数据源持久化 / 审计日志 |
| **缓存 / 队列** | Redis | 7 | 三级缓存 + 速率限制 |
| **向量库** | Qdrant | 1.12 | 去重聚类 |
| **可观测性** | LangSmith / Langfuse | — | LLM 追踪 + 成本核算 |

---

## 4. 项目目录结构

```
E:\claude-code\
├── backend/                              # Python 后端 (84 文件)
│   ├── pyproject.toml                    # 依赖 + 工具配置
│   ├── Dockerfile
│   ├── .env.example
│   ├── src/
│   │   ├── main.py                       # FastAPI 应用工厂 + lifespan
│   │   ├── config.py                     # pydantic-settings 全量配置
│   │   │
│   │   ├── models/                       # Pydantic 领域模型 (9 个)
│   │   │   ├── state.py                  # AgentState — LangGraph 核心状态
│   │   │   ├── data_source.py            # DataSource 统一输出 Schema
│   │   │   ├── specs.py                  # APISpec / FileSpec / EmbeddedSpec
│   │   │   ├── page_tree.py              # DataPageTree / DataPageNode
│   │   │   ├── requirement.py            # StructuredRequirement
│   │   │   ├── scores.py                 # SourceScores 五维评分
│   │   │   ├── candidates.py             # Raw / TypeClassified / Processed
│   │   │   ├── report.py                 # FinalReport / CriticOutput
│   │   │   └── routing.py               # ActivatedWorkerSet
│   │   │
│   │   ├── agents/                       # LangGraph 管道
│   │   │   ├── graph.py                  # ★ 核心：StateGraph 组装 + 编译
│   │   │   └── nodes/                    # 9 个管道节点
│   │   │       ├── parse_intent.py       # Stage 1: NL → 结构化需求
│   │   │       ├── route_sources.py      # Stage 2: 四层规则 + LLM 路由
│   │   │       ├── discover.py           # Stage 3: 并行多路发现
│   │   │       ├── classify_types.py     # Stage 3.5: 类型判定桥梁
│   │   │       ├── process_by_type.py    # Stage 4: 三路类型处理
│   │   │       ├── normalize_dedupe.py   # Stage 5: 归一化 + 去重
│   │   │       ├── judge.py              # Stage 6: 两阶段评审
│   │   │       ├── reflect.py            # Stage 7: 反思 + 缺口检查
│   │   │       └── finalize.py           # Stage 8: 报告组装
│   │   │
│   │   ├── tools/                        # 外部工具封装
│   │   │   ├── search/                   # 搜索引擎 (Exa/Brave/Tavily/SearXNG)
│   │   │   ├── scraping/                 # 页面抓取 (Jina/Firecrawl)
│   │   │   ├── extraction/               # 确定性抽取 (extruct/pandas)
│   │   │   └── validation/               # 验证工具 (HEAD探测/URL规范化)
│   │   │
│   │   ├── adapters/                     # 注册表适配器插件
│   │   │   ├── base.py                   # BaseRegistryAdapter ABC
│   │   │   ├── registry.py              # AdapterRegistry 单例
│   │   │   ├── academic/                 # OpenAlex / Semantic Scholar
│   │   │   ├── datasets/                 # HuggingFace / Kaggle
│   │   │   └── government/               # CKAN (复用于 data.gov 等)
│   │   │
│   │   ├── classifiers/                  # 多层级联分类器
│   │   │   ├── type_classifier.py        # 三层：URL模式 + HEAD + LLM
│   │   │   ├── portal_detector.py        # 三层：白名单 + URL模式 + LLM
│   │   │   └── url_patterns.py           # 共享正则 + 已知门户白名单
│   │   │
│   │   ├── judging/                      # 两阶段评审系统
│   │   │   ├── stage_a.py                # Haiku 批量快筛
│   │   │   ├── stage_b.py                # Sonnet 五维精评
│   │   │   ├── deterministic.py          # 确定性维度 (时效/可获取/许可证)
│   │   │   ├── authority.py              # 权威性: 先验表 + 元数据 + LLM
│   │   │   ├── domain_prior_table.py     # 500+ 域名权威评分表
│   │   │   └── rubric.py                 # 相关性评分 Rubric 模板
│   │   │
│   │   ├── services/                     # ★ 核心服务 (含 Claude Code 移植)
│   │   │   ├── llm.py                    # 多供应商 LLM (litellm 封装)
│   │   │   ├── cache.py                  # 三级 Redis 缓存
│   │   │   ├── rate_limiter.py           # 按供应商速率限制
│   │   │   ├── cost_tracker.py           # 按查询成本追踪
│   │   │   ├── embeddings.py             # 向量嵌入 (语义去重)
│   │   │   ├── observability.py          # LangSmith/Langfuse 追踪
│   │   │   ├── tool_executor.py          # ★ StreamingToolExecutor (移植)
│   │   │   ├── context_compressor.py     # ★ 上下文自动压缩 (移植)
│   │   │   ├── permissions.py            # ★ 工具权限系统 (移植)
│   │   │   ├── coordinator.py            # ★ 多Agent协调器 (移植)
│   │   │   ├── plugin_registry.py        # ★ 插件注册系统 (移植)
│   │   │   ├── mcp_client.py             # ★ MCP 协议客户端 (移植)
│   │   │   └── prefetch.py               # ★ 并行预取优化 (移植)
│   │   │
│   │   ├── api/                          # FastAPI 路由
│   │   │   ├── routes/discover.py        # POST /api/v1/discover (SSE)
│   │   │   ├── sse.py                    # SSE 事件格式化
│   │   │   └── middleware/               # 认证 / CORS / 限流
│   │   │
│   │   └── db/                           # 数据库层
│   │       ├── engine.py                 # SQLAlchemy async
│   │       ├── models.py                 # ORM 模型
│   │       └── repositories/             # CRUD 操作
│   │
│   └── tests/                            # 测试
│       ├── unit/                         # 分类器 / 评分 / 适配器
│       ├── integration/                  # 管道流 / SSE 端点
│       └── eval/                         # 金标集评估
│
├── frontend/                             # Next.js 前端 (10 文件)
│   ├── src/
│   │   ├── app/
│   │   │   ├── page.tsx                  # 首页 (查询输入 + 示例)
│   │   │   ├── layout.tsx                # 根布局
│   │   │   └── discover/[queryId]/       # 结果页 (SSE 流式)
│   │   ├── components/
│   │   │   ├── query/QueryInput.tsx      # 查询输入组件
│   │   │   ├── progress/PipelineProgress.tsx  # 八阶段进度条
│   │   │   └── results/SourceCard.tsx    # 数据源卡片 (五维评分)
│   │   ├── hooks/useSSE.ts              # SSE 连接 + 状态管理
│   │   └── types/data-source.ts         # TypeScript 类型镜像
│   │
│   ├── package.json / tsconfig.json / tailwind.config.ts
│   └── Dockerfile
│
└── docker-compose.yml                    # PostgreSQL + Redis + Qdrant + Backend + Frontend
```

---

## 5. 八阶段管道详解

### 5.1 Stage 1: Intent Parser (`parse_intent.py`)

**输入**: 自然语言查询 (string)
**输出**: `StructuredRequirement`
**模型**: Claude Sonnet 4.6 (质量优先)

解析逻辑：
- 识别领域 (domain) 和子领域 (sub_domains)
- **强制生成双语关键词** — 中文查询丢给国际注册表几乎查不到
- 分解为 2-5 个可独立检索的子问题
- 合成目标 Schema（如 "AI 公司融资" → `{company_name, round, amount_usd, date}`）
- 列出 LLM 先验知识推荐的权威源

```python
class StructuredRequirement(BaseModel):
    original_query: str
    domain: str                          # finance / health / geo / ...
    sub_domains: list[str]
    data_type_hints: list[str]           # api / tabular / timeseries / ...
    desired_formats: list[str]           # csv, json, api, any
    geographic_scope: list[str]
    temporal_range: str | None
    license_constraint: str              # commercial / open_only / any
    budget_constraint: str               # free_only / freemium / any
    sub_questions: list[str]             # 2-5 个独立子问题
    search_keywords_zh: list[str]        # 中文关键词 5-10 个
    search_keywords_en: list[str]        # 英文关键词 5-10 个 (必须!)
    known_authoritative_sources: list[str]
    target_schema: dict | None           # 用户期望的字段 Schema
```

### 5.2 Stage 2: Source Router (`route_sources.py`)

**四层规则表** (确定性, <1ms, 零成本):

| 层 | 输入 | 示例 |
|----|------|------|
| Layer 1: domain → Workers | `finance` → `{gov, finance_api, datasets, web}` | — |
| Layer 2: data_type → Workers | `api` → `{api_directory, github}` | — |
| Layer 3: format → Workers | `csv` → `{datasets, gov}` | — |
| Layer 4: keyword → Worker | `"股票"` → `finance_api` | — |

**LLM 补充层** (Haiku, ~$0.002, ~500ms): 审查规则层结果，增补遗漏、移除多余。

### 5.3 Stage 3: Parallel Discovery (`discover.py`)

三路并行搜索：

| 路径 | 内容 | 并行度 |
|------|------|--------|
| **Web Worker** | Exa + Brave + Tavily + SearXNG 四引擎 | 全并行 |
| **Registry Workers** | 按激活的 Worker 标签调用对应适配器 | 全并行 |
| **LLM Prior** | Intent Parser 推荐的权威源直接作为候选 | 同时执行 |

所有路径结果合并，按 URL 粗去重。

### 5.4 Stage 3.5: TypeClassifier (`classify_types.py`)

**关键问题**: Web 搜索返回的 URL 类型未知。一个 URL 可同时是多种类型。

三层级联判定：

| 层 | 方法 | 成本 | 延迟 |
|----|------|------|------|
| Layer 1 | URL 正则匹配 (`.csv` / `/api/` / 域名映射) | 免费 | <1ms |
| Layer 2 | HTTP HEAD 探测 (Content-Type / attachment) | ≈0 | ~100ms |
| Layer 3 | Haiku LLM 批量分类 (仅不确定的 URL) | ~$0.002 | ~500ms |

Registry 和 Portal Profiler 的结果**类型已确定**，跳过此步。

### 5.5 Stage 4: Type-Specific Processing (`process_by_type.py`)

三类数据源走不同处理管道：

| 类型 | 处理内容 |
|------|---------|
| **API** | Endpoint 健康检查 / OpenAPI 规范加载 / 认证方式检测 |
| **File** | HEAD 验证 / Content-Type 检查 / 格式推断 / 浅层内容检查 |
| **Embedded** | 五层级联: Tier-0 确定性 → Tier-1 启发式 → Tier-2 LLM → Tier-3 列表页展开 |

### 5.6 Stage 5: Normalize & Dedupe (`normalize_dedupe.py`)

1. **归一化**: 所有 `ProcessedCandidate` → 统一 `DataSource` Schema
2. **URL 去重**: 规范化后精确匹配 (去 tracking 参数 / www / HTTP→HTTPS)
3. **语义去重**: Embedding 聚类，cosine > 0.92 视为重复，保留权威性最高的版本

### 5.7 Stage 6: Two-Stage Judging (`judge.py`)

| | Stage-A 快筛 | Stage-B 精评 |
|-|-------------|-------------|
| **模型** | Haiku 4.5 | Sonnet 4.6 |
| **每批** | 10 个打包 | 每个独立调用 |
| **维度** | 只打 relevance | 完整五维 |
| **淘汰** | relevance < 5 | 硬否决规则 |
| **保留** | top 30 | 最终排序 |

### 5.8 Stage 7: Reflect & Gap Check (`reflect.py`)

Critic Agent 审视覆盖度：
- **充分** (≥80%) → Stage 8
- **不足** → 生成反馈，回到 Stage 2 (最多 3 轮)
- 到达最大轮次 → 强制充分

### 5.9 Stage 8: Finalize (`finalize.py`)

- 按 overall score 排序
- 分三类分组 (API / File / Embedded)
- LLM 生成各类使用指南 (Haiku)
- 组装 `FinalReport`

---

## 6. 核心模型系统

### 6.1 DataSource — 统一输出 Schema

```python
class DataSource(BaseModel):
    # 身份
    id: str                          # SHA256 hash
    name: str
    provider: str                    # e.g. "OpenWeatherMap"
    url: str
    source_type: DataSourceType      # api / file / embedded

    # 内容
    description: str
    domain: str
    tags: list[str]
    data_format: list[str]           # ["json", "csv"]

    # 覆盖
    geographic_coverage: list[str]
    temporal_coverage: str | None
    update_frequency: str | None

    # 访问
    access_level: AccessLevel        # open / free_reg / api_key_free / ...
    license: str | None

    # 类型专属
    api_spec: APISpec | None
    file_spec: FileSpec | None
    embedded_spec: EmbeddedSpec | None

    # 评分
    scores: SourceScores | None

    # 溯源
    discovery_method: str            # web_search / registry / llm_prior / ...
    discovered_at: datetime
```

### 6.2 SourceScores — 五维评分

```python
SCORE_WEIGHTS = {
    "relevance":     0.40,   # LLM 评分 (严格 Rubric)
    "authority":     0.20,   # 混合: 先验表 + 元数据 + LLM
    "freshness":     0.15,   # 确定性: 指数衰减
    "accessibility": 0.15,   # 确定性: 查表
    "license_fit":   0.10,   # 规则匹配 (SPDX)
}

# 硬否决:
# license_fit == 0 → 淘汰 (许可证不满足)
# relevance < 4   → 淘汰 (相关性太低)
```

### 6.3 AgentState — LangGraph 核心状态

```python
class AgentState(TypedDict, total=False):
    query: str
    requirement: StructuredRequirement
    activated_workers: list[str]
    raw_candidates: Annotated[list[RawCandidate], operator.add]  # 支持并行合并
    type_classified: list[TypeClassifiedCandidate]
    processed_candidates: list[ProcessedCandidate]
    normalized_sources: list[DataSource]
    deduplicated_sources: list[DataSource]
    scored_sources: list[DataSource]
    critic_feedback: CriticOutput
    final_report: FinalReport
    iteration: int                   # 当前轮次
    max_iterations: int              # 硬上限 3
    cost_accumulated: float          # 累计成本 USD
    stage_timings: dict[str, float]  # 各阶段耗时
    errors: Annotated[list[dict], operator.add]
```

---

## 7. 搜索工具层

### 7.1 四引擎并行搜索

| 引擎 | 特点 | 定位 |
|------|------|------|
| **Exa** | 神经/语义搜索 + `find_similar` | 不可替代的语义搜索 |
| **Brave** | 独立索引，抗 SEO 污染 | 主力通用搜索 |
| **Tavily** | Agent 原生，返回结构化摘要 | 快速 RAG 场景 |
| **SearXNG** | 自托管，聚合 70+ 引擎，零边际成本 | 扩充覆盖度 |

所有引擎实现 `BaseSearchTool` 接口：

```python
class BaseSearchTool(ABC):
    name: str
    max_results: int = 10

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    async def search(self, query: str) -> list[RawCandidate]: ...
```

### 7.2 速率限制

每个供应商独立限流 (via `aiolimiter`)：

```python
"exa": 5 req/s    "brave": 10 req/s    "tavily": 5 req/s
"searxng": 2 req/s    "firecrawl": 3 req/s    "jina": 10 req/s
```

---

## 8. 分类器系统

### 8.1 TypeClassifier — 三层级联

```
URL + Snippet
    │
    ▼ Layer 1: 正则匹配 (.csv → file, /api/ → api, kaggle.com → file)
    │           命中 → 返回, confidence=0.8
    │
    ▼ Layer 2: HEAD 请求 (Content-Type 检测, attachment 检测)
    │           命中 → 返回, confidence=0.7
    │
    ▼ Layer 3: Haiku 批量分类 (仅不确定的 URL)
              返回 JSON: [{index, types, confidence}]
```

**关键设计**: 一个 URL 可同时判定为多种类型 → 生成多个 DataSource → 各自评分。

### 8.2 Portal Detector — 三层级联

```
URL + Snippet
    │
    ▼ Layer 1: 已知门户白名单 (200+ 域名, data.gov / kaggle.com / ...)
    │           命中 → is_portal=True, confidence=1.0
    │
    ▼ Layer 2: URL 模式 (^https?://data\. / /opendata/ / ...)
    │         + Snippet 关键词 ("open data portal" / "数据门户" / ...)
    │
    ▼ Layer 3: Haiku LLM 批量 (不确定的 URL)
```

---

## 9. 评审系统 (Judge)

### 9.1 五维评分细则

| 维度 | 方法 | 权重 | 永远不用 LLM 做的事 |
|------|------|------|-------------------|
| **Relevance** | LLM + 严格 Rubric | 40% | — |
| **Authority** | 先验表 + 元数据 + LLM 兜底 | 20% | — |
| **Freshness** | 确定性指数衰减 | 15% | 日期计算 |
| **Accessibility** | 确定性查表 | 15% | 访问级别映射 |
| **License Fit** | SPDX 硬规则 | 10% | **许可证兼容性判断** |

### 9.2 确定性评分公式

**Freshness**: `score = 10 × 0.5^(days / half_life)`

半衰期按领域: 新闻 7 天 / 金融 30 天 / 科学 365 天 / 地理 730 天

**Accessibility**: 直接查表

```
OPEN=10  FREE_REG=8  API_KEY_FREE=7  OAUTH=6  API_KEY_PAID=4  PAYWALL=2  UNKNOWN=3
```

**License Fit**: SPDX 硬规则, 返回 0 = 硬否决

### 9.3 域名权威先验表

500+ 条目，示例:

```python
"data.gov": 9.5          # 美国政府开放数据
"data.worldbank.org": 9.5 # 世界银行
"arxiv.org": 9.0          # 学术预印本
"huggingface.co": 8.5     # AI 数据集平台
"kaggle.com": 8.0          # 竞赛数据集
"github.com": 7.5          # 代码/API
"wikipedia.org": 7.5       # 百科
"reddit.com": 4.5          # 社交媒体
```

---

## 10. 适配器插件系统

### 10.1 BaseRegistryAdapter — 抽象基类

```python
class BaseRegistryAdapter(ABC):
    name: str                        # "openalex"
    display_name: str                # "OpenAlex"
    domains: list[str]               # ["science", "research"]
    worker_tags: list[str]           # ["academic"]
    rate_limit: RateLimitConfig
    requires_auth: bool
    base_url: str

    @abstractmethod
    async def search(self, keywords, filters) -> list[RawCandidate]: ...

    @abstractmethod
    def normalize(self, raw: dict) -> DataSource: ...

    async def health_check(self) -> HealthStatus: ...
    def get_metadata_signals(self, raw) -> dict: ...
```

### 10.2 已实现适配器

| 适配器 | 注册表 | Worker 标签 | 认证 |
|--------|-------|------------|------|
| OpenAlex | 2 亿+ 学术作品 | academic | 免费 |
| Semantic Scholar | 2.25 亿+ 论文 | academic | 免费 |
| HuggingFace | AI 数据集 | datasets | 免费 |
| Kaggle | 竞赛数据集 | datasets | 需要 Key |
| CKAN (data.gov) | 美国政府数据 | gov, datasets | 免费 |
| CKAN (data.europa.eu) | 欧盟数据 | gov, datasets | 免费 |

### 10.3 CKAN 复用模式

CKAN 是**同协议、不同 base_url** 的典范：

```python
AdapterRegistry.register(CKANAdapter(base_url="https://catalog.data.gov", portal_name="data.gov"))
AdapterRegistry.register(CKANAdapter(base_url="https://data.europa.eu/api/hub/search", portal_name="data.europa.eu"))
```

---

## 11. 从 Claude Code 移植的架构模式

本框架从 Claude Code (Anthropic 的 CLI 工具) 的 TypeScript 架构中移植了 **7 个核心生产级模式**：

### 11.1 StreamingToolExecutor (`services/tool_executor.py`)

**来源**: Claude Code `src/services/tools/StreamingToolExecutor.ts`

工具并发控制状态机：

```
Tool States: queued → executing → completed → yielded
```

**并发规则**:
- 只读工具 (29 种) 可并行执行
- 写操作工具 (5 种) 必须独占执行
- Bash 错误**级联中止**兄弟工具
- 进度消息立即流式输出，结果按原始顺序缓冲

```python
# 并发安全工具 (可并行)
CONCURRENT_SAFE_TOOLS = {"file_read", "grep", "glob", "web_search",
    "exa_search", "brave_search", "tavily_search", "openalex_search", ...}

# 非并发工具 (必须串行)
NON_CONCURRENT_TOOLS = {"bash", "file_write", "file_edit", "firecrawl_scrape", ...}
```

### 11.2 Context Compressor (`services/context_compressor.py`)

**来源**: Claude Code `src/services/compact/autoCompact.ts` + `microCompact.ts`

三级压缩策略：

| 级别 | 触发条件 | 策略 | 成本 |
|------|---------|------|------|
| Level 1: Micro | 80% 容量 | 清除旧工具结果，保留结构 | 免费 |
| Level 2: Selective | 90% 容量 | 删除失败/重复搜索 | 免费 |
| Level 3: Full | 超限 | LLM 全量摘要替换历史 | ~$0.01 |

**断路器**: 连续 3 次失败后停止尝试。

```python
AUTOCOMPACT_BUFFER_TOKENS = 13_000       # 预留给响应的 Token
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000  # 80% 容量警告
MAX_CONSECUTIVE_FAILURES = 3              # 断路器阈值
```

### 11.3 Permission System (`services/permissions.py`)

**来源**: Claude Code `src/hooks/toolPermission/PermissionContext.ts`

三阶段决策流：

```
Phase 1: 规则匹配 (deny_rules → allow_rules)     ← 确定性, <1ms
    │
Phase 2: 钩子执行 (自定义权限逻辑)                ← 可扩展
    │
Phase 3: 模式回退 (bypass/plan/auto/default)       ← 最终决策
```

四种权限模式：

| 模式 | 行为 |
|------|------|
| `bypass` | 全部允许 (开发环境) |
| `plan` | 只读自动允许，写操作需审批 |
| `auto` | 安全操作自动允许 (via classifier) |
| `default` | 全部需用户确认 |

**ResolveOnce** 防竞态守卫：当多个权限检查并发运行时，原子性保证只有一个获胜。

### 11.4 Agent Coordinator (`services/coordinator.py`)

**来源**: Claude Code `src/coordinator/coordinatorMode.ts`

```python
class AgentCoordinator:
    # Coordinator 编排, Worker 执行
    # 研究任务并行 (信号量=3), 实现任务串行 (信号量=1)
    # 结构化任务通知: TaskNotification(task_id, status, summary, result)
```

**核心原则** (来自 Claude Code):
> "Never delegate understanding" — Coordinator 必须读取 Worker 发现，**自己合成**为具体实现规格。永远不说 "基于你的发现，修复它"。

### 11.5 Plugin Registry (`services/plugin_registry.py`)

**来源**: Claude Code `src/plugins/` + `src/skills/`

```python
class PluginRegistry:
    def discover_from_directory(self, path):  # 目录发现 + SKILL.md frontmatter
    def search(self, query):                  # 关键词搜索 (ToolSearch 模式)
    def get_deferred(self):                   # 延迟加载的插件 (按需)
    def get_always_loaded(self):              # 始终可见的插件
    def get_trigger_matches(self, text):       # 正则触发匹配
```

Frontmatter 格式:
```yaml
---
name: my-plugin
description: Does something useful
tags: [search, data]
should_defer: true
search_hint: "financial data analysis"
---
```

### 11.6 MCP Client (`services/mcp_client.py`)

**来源**: Claude Code `src/services/mcp/client.ts`

- 支持三种传输: stdio / SSE / HTTP
- 工具命名规范: `mcp__{server_name}__{tool_name}`
- 所有内部工具可包装为 MCP 兼容格式
- 连接管理 + 自动工具发现

### 11.7 Parallel Prefetch (`services/prefetch.py`)

**来源**: Claude Code `main.tsx` 的启动预取模式

```python
async def run_startup_prefetches():
    """启动时 4 个操作并行执行, 总时间 ≈ 最慢的单个操作"""
    await asyncio.gather(
        _prefetch_adapter_health(),      # 适配器健康检查
        _prefetch_cache_warmup(),        # Redis 连接预热
        _prefetch_domain_priors(),       # 权威表加载
        _prefetch_portal_whitelist(),    # 门户白名单加载
    )
```

---

## 12. 服务层

### 12.1 LLM Service (`services/llm.py`)

```python
class LLMService:
    async def complete(messages, model_tier="strong"/"fast") -> (str, LLMUsage)
    async def complete_structured(messages, response_model, ...) -> (T, LLMUsage)
    async def batch_complete(items, prompt_template, batch_size=10) -> list
    async def embed(texts) -> list[list[float]]
```

自动 failover: Claude → OpenAI → Gemini
成本追踪: 每次调用累计 `total_cost`

### 12.2 Cache Service (`services/cache.py`)

三级 Redis 缓存：

| 级别 | 对象 | TTL |
|------|------|-----|
| L1 | 搜索 API 响应 | 1 小时 |
| L2 | 页面抓取内容 | 6-24 小时 |
| L3 | 评审结果 (需求+源) | 24 小时 |

### 12.3 Cost Tracker (`services/cost_tracker.py`)

```python
class CostTracker:
    def start_stage(name)    # 开始计时
    def end_stage(name)      # 结束计时
    def add_llm_cost(stage, cost)   # 累计 LLM 成本
    def add_api_call(stage, cost)   # 累计 API 成本
    def summary() -> dict           # 按阶段汇总
```

---

## 13. API 接口设计

### 13.1 端点列表

| 方法 | 路径 | 功能 | 认证 |
|------|------|------|------|
| POST | `/api/v1/discover` | 提交查询 → SSE 流 | JWT / API Key |
| GET | `/api/v1/health` | 健康检查 | 无 |

### 13.2 请求模型

```python
class DiscoverRequest(BaseModel):
    query: str                        # 5-2000 字符
    license_constraint: str = "any"   # commercial / open_only / any
    budget_constraint: str = "any"    # free_only / freemium / any
    geographic_scope: list[str] | None = None
    temporal_range: str | None = None
    desired_formats: list[str] | None = None
    max_iterations: int = 3           # 1-5
```

### 13.3 SSE 事件格式

```
event: query_accepted
data: {"query_id": "abc123", "query": "..."}

event: stage_start
data: {"stage": "discover"}

event: stage_complete
data: {"stage": "discover", "duration_ms": 3200, "cost_usd": 0.012}

event: progress
data: {"stage": "discover", "candidates_found": 47}

event: partial_sources
data: [{"id": "...", "name": "World Bank API", "source_type": "api", "scores": {...}}]

event: done
data: {"report": {...}, "query_id": "abc123"}

event: error
data: {"message": "Pipeline failed", "query_id": "abc123"}
```

---

## 14. 前端架构

### 14.1 页面结构

| 路由 | 组件 | 功能 |
|------|------|------|
| `/` | `page.tsx` | 查询输入 + 6 个示例查询 + 统计数据 |
| `/discover/[queryId]` | `page.tsx` | SSE 流式结果展示 |

### 14.2 核心组件

**QueryInput**: 文本域 + 字符数校验 (≥5) + 提交按钮
**PipelineProgress**: 9 阶段水平进度条，实时更新状态/耗时
**SourceCard**: 数据源卡片
  - 类型徽章 (API=蓝 / File=绿 / Embedded=紫)
  - 五维评分条 (雷达图式)
  - 评分颜色: ≥8 绿色 / 6-8 黄色 / <6 红色

### 14.3 useSSE Hook

管理 SSE 连接的完整状态机：

```typescript
status: "idle" → "connecting" → "streaming" → "done" | "error"
```

逐步解析 SSE 事件，增量渲染结果。

---

## 15. 部署与运维

### 15.1 本地开发

```bash
# 1. 复制配置
cp backend/.env.example backend/.env
# 编辑 .env 填入 API keys (ANTHROPIC_API_KEY, SEARCH_EXA_API_KEY, ...)

# 2. 启动全部服务
docker-compose up -d

# 访问:
# Frontend: http://localhost:3000
# Backend:  http://localhost:8000
# Qdrant:   http://localhost:6333
```

### 15.2 Docker 服务

| 服务 | 镜像 | 端口 | 健康检查 |
|------|------|------|---------|
| postgres | postgres:16-alpine | 5432 | pg_isready |
| redis | redis:7-alpine | 6379 | redis-cli ping |
| qdrant | qdrant/qdrant:v1.12.1 | 6333, 6334 | — |
| backend | 自建 (Python 3.12) | 8000 | 依赖 postgres+redis |
| frontend | 自建 (Node 20) | 3000 | 依赖 backend |

### 15.3 环境变量

```bash
# LLM
ANTHROPIC_API_KEY=sk-ant-xxx
OPENAI_API_KEY=sk-xxx

# 搜索
SEARCH_EXA_API_KEY=xxx
SEARCH_BRAVE_API_KEY=xxx
SEARCH_TAVILY_API_KEY=xxx
SEARCH_FIRECRAWL_API_KEY=xxx

# 存储
DB_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/discovery_agent
REDIS_URL=redis://localhost:6379/0

# 可观测性
OBS_LANGSMITH_API_KEY=xxx
OBS_LANGSMITH_PROJECT=datasource-discovery
```

---

## 16. 配置参考

### 16.1 完整配置组

| 配置组 | 前缀 | 关键字段 |
|--------|------|---------|
| `AppConfig` | `APP_` | debug, log_level, cors_origins |
| `DatabaseConfig` | `DB_` | url, pool_size=20 |
| `RedisConfig` | `REDIS_` | url |
| `LLMConfig` | `LLM_` | primary_strong/fast, fallback_strong/fast, temperature=0.1 |
| `SearchConfig` | `SEARCH_` | exa/brave/tavily/searxng/firecrawl/jina API keys |
| `CacheConfig` | `CACHE_` | L1=3600s, L2=21600s, L3=86400s |
| `BudgetConfig` | `BUDGET_` | max_iterations=3, max_candidates_stage_b=30, max_portals=5 |
| `AuthConfig` | `AUTH_` | jwt_secret, jwt_algorithm=HS256, expire=1440min |
| `ObservabilityConfig` | `OBS_` | langsmith/langfuse keys, enabled=true |

### 16.2 预算硬上限

```python
max_iterations = 3                  # 反思循环最多 3 轮
max_candidates_stage_a = 80         # Stage-A 最多评估 80 个
max_candidates_stage_b = 30         # Stage-B 最多精评 30 个
max_portals_to_map = 5              # 最多探测 5 个门户
max_scrape_per_portal = 15          # 每门户最多抓 15 页
max_total_scrape = 40               # 总共最多抓 40 页
relevance_threshold_stage_a = 5.0   # Stage-A 淘汰阈值
```

---

## 17. 开发指南

### 17.1 添加新适配器

```python
# backend/src/adapters/my_domain/my_adapter.py

class MyAdapter(BaseRegistryAdapter):
    name = "my_adapter"
    display_name = "My Data Source"
    domains = ["finance"]
    worker_tags = ["finance_api"]
    base_url = "https://api.example.com"

    async def search(self, keywords, filters):
        # 1. 构建请求 URL
        # 2. 注入认证
        # 3. 发送请求 (带速率限制)
        # 4. 解析响应
        # 5. 返回 list[RawCandidate]
        ...

    def normalize(self, raw):
        # 转换为统一 DataSource Schema
        ...

# 末尾自动注册
AdapterRegistry.register(MyAdapter())
```

### 17.2 添加新搜索引擎

实现 `BaseSearchTool` 接口，添加到 `tools/search/` 目录。

### 17.3 添加用户插件

创建 `~/.datasource-agent/plugins/my-plugin/SKILL.md`:

```yaml
---
name: my-custom-skill
description: Custom data processing skill
tags: [custom, processing]
search_hint: "custom data transformation"
---

Skill instructions here...
```

---

## 18. 设计哲学与反模式

### 18.1 七条铁律

| # | 原则 | 实现 |
|---|------|------|
| 1 | **结构化一切** | 全链路 Pydantic Schema, 不给 LLM 自由发挥空间 |
| 2 | **规则优先, LLM 兜底** | TypeClassifier/PortalDetector 三层级联 |
| 3 | **分层检索, 多源组合** | 4 搜索引擎 + 20+ 注册表并行 |
| 4 | **LLM 不做事实提供者** | LLM 只做调度/判断/格式化; 数据来自 API 和网页 |
| 5 | **分阶段, 强契约** | 8 阶段管道, 每阶段输入输出明确 |
| 6 | **假设一切会失败** | 重试 + 降级 + 熔断 + 断路器 |
| 7 | **可观测, 可复现** | LangSmith 追踪 + 评分附带理由 |

### 18.2 反模式清单

| # | 反模式 | 本方案对策 |
|---|--------|---------|
| 1 | 单一搜索 API | 四引擎 + 注册表并行 |
| 2 | 一个大 ReAct 循环 | 八阶段独立 Stage |
| 3 | LLM 凭空打权威分 | 域名先验表 + 元数据 + LLM 兜底 |
| 4 | LLM 判断许可证 | SPDX 硬规则 |
| 5 | 忽略双语关键词 | Intent Parser 强制生成英文 |
| 6 | 全部候选用强模型精评 | 两阶段: Haiku 快筛 + Sonnet 精评 |
| 7 | 没有确定性检测 | Tier-0 免费处理 20-40% |
| 8 | 没有金标集就调 prompt | 构建 30+ 条金标集回归测试 |
| 9 | 不做缓存 | 三级 Redis 缓存 |
| 10 | 不做并发控制 | StreamingToolExecutor (来自 Claude Code) |
| 11 | 没有上下文压缩 | 三级自动压缩 + 断路器 (来自 Claude Code) |
| 12 | 没有权限系统 | 三阶段权限决策流 (来自 Claude Code) |

---

## 项目统计

| 维度 | 数据 |
|------|------|
| Backend Python 文件 | 84 |
| Frontend TypeScript 文件 | 10 |
| 配置/Docker 文件 | 7 |
| 总文件数 | 103 |
| Pydantic 模型 | 9 |
| 管道节点 | 9 |
| 搜索引擎 | 4 |
| 注册表适配器 | 6 (可扩展) |
| 从 Claude Code 移植的模式 | 7 |
| Docker 服务 | 5 |
