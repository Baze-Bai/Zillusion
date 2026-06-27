# 统一数据源发现 Agent 框架：完整架构设计

> **定位**：用户输入自然语言需求 → Agent 从全网发现三类数据源（公开 API、可下载文件、网页内嵌数据）→ 评估、排序、结构化输出。
>
> 版本：v2.0 | 2026-04

---

## 目录

1. [设计哲学](#1-设计哲学)
2. [三类数据源的统一抽象](#2-三类数据源的统一抽象)
3. [整体架构：八阶段管道](#3-整体架构八阶段管道)
4. [各阶段详细设计](#4-各阶段详细设计)
   - 4.1 Intent Parser
   - 4.2 Source Router（含详细路由表 + LLM 补充层）
   - 4.3 Parallel Discovery（含 Portal Detector、五路 /map 触发、Adapter 架构）
   - 4.4 TypeClassifier（搜索→处理的类型判定桥梁）
   - 4.5 Type-Specific Processing（含 Tier-3 列表页展开 + 数据页面树）
   - 4.6 Normalize & Dedupe
   - 4.7 Two-Stage Judging
   - 4.8 Reflect & Gap Check
   - 4.9 Finalize & Output
5. [Judge 系统：五维评分](#5-judge-系统五维评分)
6. [技术选型](#6-技术选型)
7. [垂类目录 Adapter 体系](#7-垂类目录-adapter-体系)
8. [Firecrawl 使用策略](#8-firecrawl-使用策略)
9. [成本控制与缓存](#9-成本控制与缓存)
10. [安全与合规](#10-安全与合规)
11. [可观测性与评估](#11-可观测性与评估)
12. [落地路线图](#12-落地路线图)
13. [反模式清单](#13-反模式清单)
14. [附录](#附录)

---

## 1. 设计哲学

### 1.1 这是数据源发现 Agent，不是研究 Agent

| | 研究 Agent (Perplexity 类) | 数据源发现 Agent (本方案) |
|-|---------------------------|-------------------------|
| 输入 | 一个问题 | 一个数据需求 |
| 输出 | 综合答案 | 结构化数据源清单 |
| 核心能力 | 内容综合与推理 | 源的定位、评估、分类与去重 |
| 用户后续动作 | 读答案就结束 | 调 API、下载文件、编写抽取代码 |

### 1.2 七条铁律

| # | 原则 | 理由 |
|---|------|------|
| 1 | **结构化一切** | 需求、候选、评分、输出全走强类型 schema，不给 LLM 自由发挥空间 |
| 2 | **规则优先，LLM 兜底** | 能用确定性代码算的绝不交给 LLM；但该用 LLM 的地方果断用 |
| 3 | **分层检索，多源组合** | 单一搜索 API 覆盖度上限约 30-40%，必须并行多路 |
| 4 | **LLM 不做事实提供者** | LLM 负责调度、判断、格式化；真实数据必须来自真实 API 和真实网页 |
| 5 | **分阶段、强契约** | 拒绝"一个大 ReAct 循环包办一切"——拆成独立 stage，每个 stage 有明确输入输出 |
| 6 | **假设一切会失败** | API 会改、会限流；网站会反爬、会下线；LLM 会幻觉。架构内建重试、降级、熔断 |
| 7 | **可观测、可复现、可调试** | 所有 LLM 调用和决策都记录 trace，所有评分附带理由 |

### 1.3 三类数据源的核心差异

| 维度 | 公开 API | 可下载文件 | 网页内嵌数据 |
|------|---------|-----------|------------|
| **发现方式** | API 目录 + OpenAPI 注册表 + Web 搜索 | Web 搜索 + 数据门户 + 文件后缀过滤 | Web 搜索 + 页面结构分析 |
| **验证方式** | Endpoint 可达性 + Schema 校验 | HEAD 请求 + Content-Type + 文件大小 | Tier-0 确定性检测 + LLM 分类 |
| **质量信号** | 文档完整度、Rate Limit、认证方式 | 格式、大小、更新时间、许可证 | 数据密度、Schema 覆盖率、记录数 |
| **用户后续动作** | 注册获取 Key → 编写调用代码 | 直接下载 → 解析使用 | 编写抽取代码 / 用工具抽取 |
| **成本** | 低（检索 + 验证） | 低（HEAD 请求） | 高（需要抓取页面 + LLM 判断） |

---

## 2. 三类数据源的统一抽象

### 2.1 统一输出 Schema：DataSource

无论来源类型如何，最终都归一化成同一个 `DataSource` 对象：

```python
class DataSourceType(str, Enum):
    API = "api"                    # 公开 API
    DOWNLOADABLE_FILE = "file"     # 可下载的 CSV/JSON/Excel/Parquet 等
    EMBEDDED_DATA = "embedded"     # 网页内嵌的结构化/半结构化数据

class AccessLevel(str, Enum):
    OPEN = "open"                  # 无需注册，直接可用
    FREE_REGISTRATION = "free_reg" # 免费注册即可
    API_KEY_FREE = "api_key_free"  # 需要 API Key 但免费
    API_KEY_PAID = "api_key_paid"  # 需要付费 API Key
    OAUTH = "oauth"                # 需要 OAuth 授权
    PAYWALL = "paywall"            # 付费墙
    UNKNOWN = "unknown"

class DataSource(BaseModel):
    """统一数据源对象——贯穿整个 Pipeline 的核心数据模型"""

    # === 基础标识 ===
    id: str                          # 全局唯一 ID（UUID 或 hash）
    name: str                        # 数据源名称
    provider: str                    # 提供方（如 "OpenWeatherMap", "World Bank"）
    url: str                         # 主 URL
    source_type: DataSourceType      # 三大类之一

    # === 内容描述 ===
    description: str                 # 内容描述
    domain: str                      # 领域标签（finance/health/geo/...）
    tags: list[str]                  # 细粒度标签
    data_format: list[str]           # 数据格式 ["json", "csv", "html_table", ...]
    sample_excerpt: str | None       # 样本摘录（前 N 行或 Schema 片段）
    estimated_record_count: int | None

    # === 时空覆盖 ===
    geographic_coverage: list[str]   # 地理覆盖
    temporal_coverage: str | None    # 时间覆盖描述
    update_frequency: str | None     # 更新频率

    # === 访问与法律 ===
    access_level: AccessLevel
    license: str | None              # SPDX 标识或自由文本
    pricing: str | None              # 定价说明
    rate_limit: str | None           # API 限流说明

    # === 类型专属字段 ===
    api_spec: APISpec | None
    file_spec: FileSpec | None
    embedded_spec: EmbeddedSpec | None

    # === 质量评分（Judge 阶段填充） ===
    scores: SourceScores | None

    # === 溯源 ===
    discovery_method: str            # web_search / registry / llm_prior / portal_profiler / ...
    discovered_at: datetime
    raw_source_category: str         # academic / gov / dataset / ...
    metadata: dict                   # 原始元数据保留


class APISpec(BaseModel):
    """API 类数据源的专属信息"""
    endpoint: str                    # Base URL 或具体 endpoint
    method: str                      # HTTP Method
    auth_type: str                   # api_key / oauth / hmac / none
    auth_location: str | None        # header / query / body
    auth_param_name: str | None
    signup_url: str | None           # 注册链接
    signup_instructions: str | None  # 注册引导
    openapi_spec_url: str | None     # OpenAPI 规范地址
    has_sdk: bool                    # 是否有官方 SDK
    example_code: str | None         # 示例代码片段
    documentation_url: str | None


class FileSpec(BaseModel):
    """可下载文件类数据源的专属信息"""
    download_url: str                # 直接下载链接
    file_format: str                 # csv / json / xlsx / parquet / xml / ...
    file_size: int | None            # 文件大小（字节）
    content_type: str | None         # HTTP Content-Type
    encoding: str | None             # 文件编码
    compression: str | None          # gzip / zip / none
    column_headers: list[str] | None # 表头（浅层检查获取）
    row_sample: list[dict] | None    # 前几行样本


class EmbeddedSpec(BaseModel):
    """网页内嵌数据的专属信息"""
    extraction_method: str           # json_ld / microdata / html_table / llm_extract
    data_shape: str                  # table / list / prose / mixed
    schema_type: str | None          # Schema.org 类型（如 Product, Event）
    fields_present: list[str]        # 可抽取的字段列表
    coverage: float                  # 对用户 schema 的覆盖率 0-1
    extraction_difficulty: str       # trivial / easy / medium / hard
    extraction_code_hint: str | None # 抽取代码提示

    # === 列表页专属（is_list_page=True 时填充） ===
    is_list_page: bool               # 是否为列表页（可展开到详情页）
    page_tree: DataPageTree | None   # 列表页→详情页的树结构


class DataPageTree(BaseModel):
    """从列表页到详情页的完整数据页面树"""
    root: DataPageNode                          # 根节点（列表页本身）
    total_detail_pages: int                     # 详情页总数
    sampled_detail_pages: int                   # 实际采样的详情页数
    field_progression: dict[str, list[str]]     # 各层级可获取的字段
    tree_summary: str                           # LLM 生成的树结构自然语言描述


class DataPageNode(BaseModel):
    """数据页面树中的单个节点"""
    url: str
    page_type: str              # list / detail / category / pagination / hub
    title: str
    depth: int                  # 树深度，根节点=0
    fields_available: list[str] # 该层级可获取的数据字段
    record_count: int | None    # 该页包含的记录数（列表页）
    children: list[DataPageNode]
    is_sampled: bool            # 是否实际抓取过（vs 仅从 URL 推断）


class SourceScores(BaseModel):
    """五维评分"""
    relevance: float         # 0-10，LLM 评分
    authority: float         # 0-10，混合评分
    freshness: float         # 0-10，确定性计算
    accessibility: float     # 0-10，确定性计算
    license_fit: float       # 0-10，规则匹配
    overall: float           # 加权聚合
    relevance_rationale: str
    authority_rationale: str
```

---

## 3. 整体架构：八阶段管道

```
用户自然语言需求
      |
      v
+====================================================================+
|  [Stage 1] Intent Parser                                            |
|  输入: 自然语言                                                       |
|  输出: StructuredRequirement (schema + 双语关键词 + 子问题)            |
|  模型: Claude Sonnet 4.6                                             |
+====================================================================+
      |
      v
+====================================================================+
|  [Stage 2] Source Router                                             |
|  输入: StructuredRequirement                                         |
|  输出: ActivatedWorkerSet (启用哪些 Worker + 优先级)                   |
|  方法: 三层路由表（domain/type/format/keyword） + LLM 补充            |
+====================================================================+
      |
      v
+====================================================================+
|  [Stage 3] Parallel Discovery (Fan-out)                              |
|  ┌──────────────┬─────────────┬──────────────┬───────────────┐      |
|  | Web Worker    | Registry    | Portal       | LLM 先验       |      |
|  | (Exa+Brave+  | Workers     | Profiler     | + 语义邻居     |      |
|  |  Tavily+     | (各垂类     | (/map深挖     | 扩展           |      |
|  |  SearXNG)    |  Adapter)   |  数据门户)   |               |      |
|  └──────────────┴─────────────┴──────────────┴───────────────┘      |
|  输出: list[RawCandidate]                                            |
+====================================================================+
      |
      v
+====================================================================+
|  [Stage 3.5] TypeClassifier (类型判定桥梁)                            |
|  对 Web Worker 返回的类型未知 URL 做三层判定:                          |
|  Layer 1: URL 模式匹配 (免费, <1ms)                                  |
|  Layer 2: HEAD 请求探测 (便宜, ~100ms)                               |
|  Layer 3: Haiku LLM 批量判定 (不确定的才调)                           |
|  一个 URL 可同时判定为多种类型                                        |
|  Registry + Portal Profiler 的产出类型已确定，直接通过                  |
+====================================================================+
      |
      v
+====================================================================+
|  [Stage 4] Type-Specific Processing                                  |
|  ┌──────────────────┬──────────────────┬─────────────────────┐      |
|  | API Processor     | File Processor   | Embedded Processor  |      |
|  | - OpenAPI 加载    | - HEAD 验证      | - Tier-0 确定性检测 |      |
|  | - Endpoint 可达   | - Content-Type   | - Tier-1 启发式     |      |
|  | - Auth/SDK 检测   | - 浅层检查50KB   | - Tier-2 LLM 分类   |      |
|  |                   |                  | - Tier-3 列表页展开 |      |
|  |                   |                  |   (/map+数据页面树) |      |
|  └──────────────────┴──────────────────┴─────────────────────┘      |
+====================================================================+
      |
      v
+====================================================================+
|  [Stage 5] Normalize & Dedupe                                        |
|  - 各类目独立 Adapter 归一化到 DataSource schema                      |
|  - URL canonicalize 去精确重复                                        |
|  - Embedding 聚类去语义重复 (cosine > 0.92)                          |
+====================================================================+
      |
      v
+====================================================================+
|  [Stage 6] Two-Stage Judging                                         |
|  Stage-A: Haiku 批量快筛 (10个/批, relevance < 5 淘汰)               |
|  Stage-B: Sonnet 精评 top-30 (五维评分 + 硬否决)                     |
+====================================================================+
      |
      v
+====================================================================+
|  [Stage 7] Reflect & Gap Check                                       |
|  Critic Agent 审视覆盖度                                              |
|  - 充分 → Stage 8                                                    |
|  - 不足 → 生成反馈，回到 Stage 2 (最多 3 轮)                          |
+====================================================================+
      |
      v
+====================================================================+
|  [Stage 8] Finalize & Output                                         |
|  - 按 overall score 排序 + 三类分组                                   |
|  - 结构化 JSON + 自然语言摘要 + 使用指南                              |
+====================================================================+
```

---

## 4. 各阶段详细设计

### 4.1 Stage 1: Intent Parser（需求解析）

**整个系统的成败命门。** 模糊需求在此变成结构化检索计划。

```python
class StructuredRequirement(BaseModel):
    """Intent Parser 的输出——驱动后续所有环节"""

    # === 核心需求 ===
    original_query: str
    domain: str                          # finance / health / geo / science / tech / ...
    sub_domains: list[str]               # 细分领域
    data_type_hints: list[str]           # api / tabular / timeseries / text / geo / ...
    desired_formats: list[str]           # csv, json, api, any

    # === 约束条件 ===
    geographic_scope: list[str]          # 国家/地区列表
    temporal_range: str | None           # "2020-2024" 或 "latest"
    update_frequency: str | None         # realtime / daily / monthly / any
    license_constraint: str              # commercial / open_only / any
    budget_constraint: str               # free_only / freemium / any

    # === 检索驱动 ===
    sub_questions: list[str]             # 2-5 个可独立检索的子问题
    search_keywords_zh: list[str]        # 中文关键词 5-10 个
    search_keywords_en: list[str]        # 英文关键词 5-10 个（必须生成！）
    known_authoritative_sources: list[str]  # LLM 先验知识推荐的权威源
    target_registries: list[str]         # 建议查询的注册表

    # === 用户 Schema（用于内嵌数据判断） ===
    target_schema: dict | None           # 用户期望的数据字段 JSON Schema
```

**关键设计点**：

1. **强制双语关键词**：中文查询直接丢给 OpenAlex、Semantic Scholar 几乎查不到。Parser 必须生成对等英文关键词。
2. **子问题分解**：复合需求拆成 2-5 个可独立检索的子问题，每个子问题独立投入 Worker。
3. **Target Schema 生成**：用户说"AI 公司融资情况"→ 自动合成 `{company_name, round, amount_usd, date, lead_investor}` 这样的 schema。此 schema 贯穿内嵌数据检测和文件内容校验。
4. **先验知识直采**：让 LLM 直接列出该领域的权威数据源（World Bank、FRED、data.gov 等），标记为 `discovery_method: "llm_prior"`，后续验证存活性。

**模型**：Claude Sonnet 4.6。此步决定召回上限，质量优先于速度。

---

### 4.2 Stage 2: Source Router（源路由）

**规则优先 + LLM 补充的混合策略**。规则层处理 85% 的确定 case（< 1ms, 零成本），LLM 补充层处理 15% 的边缘 case（~500ms, ~$0.002）。

#### 4.2.1 规则路由层

```python
# ── Layer 1: domain → Worker 集合 ──
DOMAIN_TO_WORKERS = {
    "science|research|medicine|biology": ["academic", "datasets", "web"],
    "finance|economics|market":          ["gov", "finance_api", "datasets", "web"],
    "government|statistics|census":      ["gov", "datasets", "web"],
    "ai|ml|nlp|cv":                      ["datasets", "academic", "github", "web"],
    "geography|climate|environment":     ["geo", "gov", "datasets", "web"],
    "news|events|social":                ["news", "web"],
}

# ── Layer 2: data_type → 额外启用 ──
DATA_TYPE_TO_WORKERS = {
    "api":              ["api_directory", "github"],
    "timeseries|tabular": ["gov", "datasets", "finance_api"],
    "code|sdk":         ["github"],
    "knowledge_graph":  ["kb"],
}

# ── Layer 3: format → 额外启用 ──
FORMAT_TO_WORKERS = {
    "csv|xlsx|parquet": ["datasets", "gov"],
    "json":            ["api_directory", "datasets"],
    "geojson|shapefile": ["geo"],
}

# ── Layer 4: keyword 级快速信号 ──
KEYWORD_SIGNALS = {
    "paper|论文|research|study":     "academic",
    "dataset|数据集":                 "datasets",
    "stock|股票|ticker|fund":        "finance_api",
    "census|人口普查|统计局":         "gov",
    "satellite|卫星|地图|coordinate": "geo",
    "github|仓库|repo|开源":          "github",
}


def rule_based_routing(req: StructuredRequirement) -> set[str]:
    workers = {"web"}  # Web Worker 永远启用

    # Layer 1-4 取并集
    for pattern, wlist in DOMAIN_TO_WORKERS.items():
        if req.domain in pattern.split("|"):
            workers.update(wlist)

    for dtype in req.data_type_hints:
        for pattern, wlist in DATA_TYPE_TO_WORKERS.items():
            if dtype in pattern.split("|"):
                workers.update(wlist)

    for fmt in req.desired_formats:
        for pattern, wlist in FORMAT_TO_WORKERS.items():
            if fmt in pattern.split("|"):
                workers.update(wlist)

    all_keywords = " ".join(req.search_keywords_zh + req.search_keywords_en)
    for pattern, worker in KEYWORD_SIGNALS.items():
        if any(kw in all_keywords for kw in pattern.split("|")):
            workers.add(worker)

    return workers
```

#### 4.2.2 LLM 补充层

规则层完成后，让 Planner LLM 做一次快速审查（~$0.002，~500ms）：

```python
ROUTER_LLM_PROMPT = """
用户需求: {requirement}
规则层已选中的 Worker: {selected_workers}
所有可用 Worker: {all_workers}

请检查:
1. 是否有遗漏——用户需求是否涉及某个未选中的领域？
2. 是否有多余——是否有明显不相关的 Worker 被选中？

只在有明确理由时修改。输出 JSON:
{"add": [...], "remove": [...], "reason": "..."}
"""
```

**为什么需要 LLM 补充**——示例：

```
用户查询: "NBA 球员历年三分命中率数据"
规则层: domain="sports" → 规则表无 sports 专项 → 只选中 {web, datasets}
LLM 层: 识别出 NBA 统计 API → add: ["api_directory"]
        reason: "NBA 数据常见于专用 API (nba_api, balldontlie)"
```

---

### 4.3 Stage 3: Parallel Discovery（并行多路发现）

**决定整个系统的召回率。** 四条并行路径 + Portal Profiler 深挖。

#### 4.3.1 Web Worker（通用搜索）

并行四路搜索引擎：

| 引擎 | 特点 | 定位 |
|------|------|------|
| **Exa** | 神经/语义搜索，支持 `find_similar` | 不可替代——处理"找类似 X 的数据源"这种关键词无法表达的查询 |
| **Brave Search API** | 独立索引，抗 SEO 污染 | 主力通用搜索 |
| **Tavily** | Agent 原生，返回结构化摘要 | 快速 RAG 场景 |
| **SearXNG** | 开源自托管，聚合 70+ 引擎，零边际成本 | 扩充覆盖度 15-25%，反思阶段免费重试 |

**绝对不要**用 Perplexity Sonar 当搜索——它返回合成答案，绕过了 Agent 自己的判断。

**重要**：Web Worker 返回的 URL **类型未知**，搜索时无法区分 API/文件/内嵌数据。需经过 Stage 3.5 TypeClassifier 做类型判定后再进入 Stage 4 分流处理。

#### 4.3.2 Registry Workers（垂类目录 API）

**这是护城河。** 通用搜索覆盖度上限约 30-40%，垂类目录把下限和上限同时抬高。

```
┌──────────────────┬──────────────────────────────────────────────┐
│ Academic Worker  │ OpenAlex (免费无 key, 首选)                    │
│                  │ Semantic Scholar (2.25亿+论文, 引用图谱)       │
│                  │ arXiv API / Crossref / PubMed                 │
├──────────────────┼──────────────────────────────────────────────┤
│ Datasets Worker  │ HuggingFace Datasets API (结构化最好)          │
│                  │ Kaggle API / Zenodo / Figshare / DataCite     │
├──────────────────┼──────────────────────────────────────────────┤
│ Gov Worker       │ CKAN API (data.gov / data.europa.eu 等)       │
│                  │ World Bank / IMF / FRED / Eurostat / UN Data  │
├──────────────────┼──────────────────────────────────────────────┤
│ Finance Worker   │ yfinance / Polygon / Alpha Vantage / FRED     │
│                  │ SEC EDGAR                                     │
├──────────────────┼──────────────────────────────────────────────┤
│ GitHub Worker    │ GitHub Search API / APIs.guru / RapidAPI Hub  │
├──────────────────┼──────────────────────────────────────────────┤
│ Geo Worker       │ OpenStreetMap Overpass / Copernicus            │
│                  │ NASA EarthData / ArcGIS Hub                   │
├──────────────────┼──────────────────────────────────────────────┤
│ KB Worker        │ Wikidata SPARQL / DBpedia                     │
├──────────────────┼──────────────────────────────────────────────┤
│ News Worker      │ GDELT 2.0 / Common Crawl News                │
└──────────────────┴──────────────────────────────────────────────┘
```

**Registry Worker 的结果类型已确定**——HuggingFace 返回的一定是数据集，APIs.guru 返回的一定是 API。不需要经过 TypeClassifier，直接进入对应的 Stage 4 Processor。

每个 Registry 对应一个提前接入的 Adapter（详见第 7 节）。

#### 4.3.3 Portal Profiler（门户深度探测 + Portal Detector）

##### Portal Detector：三层 Cascading 门户识别

搜索返回一个 URL 后，首先判断它是否为"数据门户"。门户 = 主要功能是提供大量可检索、可下载、可通过 API 访问的数据集的网站。

```python
class PortalDetector:
    """三层 cascading 判断一个 URL 是否值得做 /map 深挖"""

    # ── Layer 1: 已知门户白名单（确定性，零成本，<1ms） ──
    KNOWN_PORTALS = {
        "data.gov", "data.gov.uk", "data.europa.eu", "data.gov.au",
        "data.worldbank.org", "data.imf.org", "data.un.org", "data.oecd.org",
        "kaggle.com", "huggingface.co", "zenodo.org", "figshare.com",
        "fred.stlouisfed.org", "earthdata.nasa.gov", "copernicus.eu",
        "datasetsearch.research.google.com", "dataverse.harvard.edu",
        "registry.opendata.aws", "datahub.io",
        # ... 持续扩充（200+ 条）
    }

    # ── Layer 2: URL 模式启发式（确定性，零成本，<1ms） ──
    PORTAL_URL_PATTERNS = [
        r'^https?://data\.',           # data.xxx.org
        r'^https?://datos\.',          # datos.xxx.gov
        r'^https?://opendata\.',       # opendata.xxx
        r'/portal/?$', r'/datacatalog/?$', r'/opendata/?$',
    ]
    PORTAL_SIGNAL_WORDS = [
        "open data portal", "data catalog", "数据门户",
        "data repository", "dataset collection", "browse datasets",
    ]

    # ── Layer 3: LLM 兜底（只处理 Layer 1&2 未命中的 URL） ──
    async def detect_portals(self, urls_with_snippets):
        results, uncertain = [], []

        for url, snippet in urls_with_snippets:
            domain = extract_domain(url)

            if domain in self.KNOWN_PORTALS:
                results.append(PortalDecision(url=url, is_portal=True,
                    confidence=1.0, source="whitelist"))
                continue

            if any(re.search(p, url) for p in self.PORTAL_URL_PATTERNS):
                results.append(PortalDecision(url=url, is_portal=True,
                    confidence=0.85, source="url_pattern"))
                continue

            if snippet and any(w in snippet.lower() for w in self.PORTAL_SIGNAL_WORDS):
                results.append(PortalDecision(url=url, is_portal=True,
                    confidence=0.75, source="snippet_signal"))
                continue

            uncertain.append((url, snippet))

        # Layer 3: 不确定的批量交给 Haiku（一次调用，~$0.002）
        if uncertain:
            llm_results = await self.batch_llm_portal_detection(uncertain)
            results.extend(llm_results)

        return results
```

**为什么 Layer 3 用 LLM**：白名单无法穷举全球数据门户（如 ourworldindata.org、tradingeconomics.com、knoema.com 等域名无特征）。漏掉一个门户 = 损失一整棵数据树。LLM 兜底成本仅 ~$0.002，但能多捕获 10-15% 的门户。

##### /map 的五个触发来源

/map 不只在搜索发现门户时才触发：

```
┌──────────────────────────────────────────────────────────────┐
│                    /map 的五个触发来源                         │
├──────────────────────────────────────────────────────────────┤
│ ① 搜索结果中发现门户 URL (~60%)                               │
│   Web Worker 返回 data.worldbank.org → 识别为门户 → /map      │
│                                                              │
│ ② Intent Parser 的 LLM 先验推荐 (~25%)                       │
│   known_authoritative_sources 中的门户 URL                    │
│   → 直接触发 /map，和搜索并行执行，不等搜索返回                 │
│                                                              │
│ ③ 门户中发现的其他门户（链式发现）(~10%)                       │
│   /scrape data.worldbank.org/about → 发现 data.imf.org       │
│   → 触发新一轮 /map                                          │
│                                                              │
│ ④ Reflect 阶段的定向补充 (~5%)                                │
│   Critic: "缺少政府官方数据，建议探索 data.stats.gov.cn"      │
│   → 触发 /map                                                │
│                                                              │
│ ⑤ 用户显式指定（优先级最高，但最少见）                         │
│   用户: "帮我看看 data.gov 上有没有相关数据" → 直接 /map       │
└──────────────────────────────────────────────────────────────┘
```

**来源②和搜索完全并行**——不等搜索返回就开始探测先验推荐的门户，省 2-5 秒。

##### Portal Profiler 执行流程

```
识别为门户 URL
       |
       v
Firecrawl /map  →  2-8秒返回几千条 URL (1 credit)
       |
       v
路径规则过滤 (/data, /dataset, /catalog, /download, /api)
  几千条 → 几十条
       |
       v
LLM 仅基于 URL 路径选出 top 15-20 数据页 (~$0.001)
       |
       v
Firecrawl /scrape 定向抓取
       |
       v
从页面中提取数据源线索:
  - download_link → FileSpec (类型已确定)
  - api_reference → APISpec (类型已确定)
  - embedded_table → EmbeddedSpec (类型已确定)
  - related_portal → 新的 /map 种子 (来源③)
```

**成本对比**：全站 crawl ~1000 credits vs Portal Profiler ~16 credits，降低 60 倍。

##### Portal Profiler 预算上限

```python
class PortalProfilerBudget:
    max_portals_to_map: int = 5        # 单次查询最多探测 5 个门户
    max_scrape_per_portal: int = 15    # 每个门户最多 scrape 15 页
    max_total_scrape: int = 40         # 总共最多 scrape 40 页
    # 优先级: ⑤用户指定 > ②先验推荐 > ①搜索发现 > ③链式 > ④反思
```

#### 4.3.4 语义邻居扩展

前三路拿到初步结果后，对置信度最高的几个源用 Exa 的 `find_similar` 扩展，发现语义相似但不在搜索结果中的站点。

---

### 4.4 Stage 3.5: TypeClassifier（类型判定桥梁）

**核心问题**：Web Worker 搜索返回的 URL 类型未知。同一条搜索返回的 10 个结果可能三种类型都有，甚至同一个 URL 同时是多种类型。

**Registry 和 Portal Profiler 的产出不经过此步**——它们的类型在发现时已确定。

#### 三层 Cascading 判定

```python
async def classify_source_types(
    urls_with_snippets: list[tuple[str, str]]
) -> list[SourceTypeClassification]:
    results, uncertain = [], []

    for url, snippet in urls_with_snippets:
        detected = []

        # ══ Layer 1: URL 模式匹配（免费, <1ms）══
        FILE_PATTERNS = [
            r'\.(csv|json|xlsx|parquet|xml|tsv|zip|gz)(\?|$)',
            r'/download/', r'/export/', r'/bulk/', r'/files/',
        ]
        API_PATTERNS = [
            r'/api/', r'/v[1-9]/', r'/rest/', r'/graphql',
            r'/swagger', r'/openapi', r'api\.([\w]+)\.(com|org|io)',
        ]
        DOMAIN_TYPE_MAP = {
            "kaggle.com": [DataSourceType.DOWNLOADABLE_FILE],
            "huggingface.co": [DataSourceType.DOWNLOADABLE_FILE],
            "rapidapi.com": [DataSourceType.API],
            "amazon.com": [DataSourceType.EMBEDDED_DATA],
            "wikipedia.org": [DataSourceType.EMBEDDED_DATA],
        }

        if any(re.search(p, url.lower()) for p in FILE_PATTERNS):
            detected.append(DataSourceType.DOWNLOADABLE_FILE)
        if any(re.search(p, url.lower()) for p in API_PATTERNS):
            detected.append(DataSourceType.API)
        domain = extract_domain(url)
        if domain in DOMAIN_TYPE_MAP:
            detected.extend(DOMAIN_TYPE_MAP[domain])

        if detected:
            results.append(SourceTypeClassification(
                url=url, detected_types=list(set(detected)),
                confidence=0.8, source="url_pattern"))
            continue

        # ══ Layer 2: HEAD 请求探测（便宜, ~100ms）══
        head = await http_head(url, timeout=5)
        if head:
            ct = head.headers.get("content-type", "")
            if any(m in ct for m in ["text/csv", "application/json",
                    "application/xml", "application/zip",
                    "application/vnd.openxmlformats", "application/parquet"]):
                detected.append(DataSourceType.DOWNLOADABLE_FILE)
            if "attachment" in head.headers.get("content-disposition", ""):
                detected.append(DataSourceType.DOWNLOADABLE_FILE)
            if "text/html" in ct and not detected:
                detected.append(DataSourceType.EMBEDDED_DATA)

        if detected:
            results.append(SourceTypeClassification(
                url=url, detected_types=list(set(detected)),
                confidence=0.7, source="head_probe"))
            continue

        # ══ Layer 3: 攒起来给 Haiku 批量判定 ══
        uncertain.append((url, snippet))

    if uncertain:
        llm_results = await batch_type_classification_llm(uncertain)
        results.extend(llm_results)

    return results
```

#### 关键：一个 URL 可同时是多种类型

```
data.worldbank.org/indicator/NY.GDP.MKTP.CD
├── API         → api.worldbank.org 提供 REST API
├── 可下载文件   → 页面上有 "Download CSV" 按钮
└── 内嵌数据     → 页面直接展示数据表格

→ 同时进入三个 Processor，生成三个独立 DataSource 对象
→ Judge 阶段各自评分，用户看到三种获取方式的对比
```

#### 搜索管道 vs Portal/Registry 管道的汇合点

```
Web Worker 返回 30 条 URL (类型未知)
         |
         v
    TypeClassifier → 判定每条的类型
         |
         v ─────────── 汇合 ←─── Registry 产出 (类型已知)
                        |     ←─── Portal Profiler 提取物 (类型已知)
                        v
                   Stage 4 分流处理
```

---

### 4.5 Stage 4: Type-Specific Processing（类型特化处理）

三类数据源走不同处理管道，但输出统一归一化。

#### 4.5.1 API Processor

```python
async def process_api_candidate(candidate: RawCandidate) -> ProcessedCandidate:
    health = await check_endpoint_health(candidate.url)
    if not health.reachable:
        return mark_dead(candidate)

    spec = await try_load_openapi_spec(candidate)  # APIs.guru / 文档页 / 常见路径
    auth = detect_auth_type(spec, candidate)
    rate_limit = extract_rate_limit(spec, candidate)
    sdk = check_sdk_availability(candidate.provider)
    example = generate_example_code(spec, auth) if spec else None

    return ProcessedCandidate(
        type=DataSourceType.API,
        api_spec=APISpec(
            endpoint=candidate.url, auth_type=auth.type,
            auth_location=auth.location, signup_url=auth.signup_url,
            openapi_spec_url=spec.url if spec else None,
            has_sdk=sdk is not None, example_code=example, ...
        )
    )
```

#### 4.5.2 File Processor

```python
async def process_file_candidate(candidate: RawCandidate) -> ProcessedCandidate:
    head = await http_head(candidate.download_url, timeout=10)
    if head.status >= 400:
        return mark_dead(candidate)
    if is_false_positive(candidate.download_url, head.content_type):
        return mark_dead(candidate)

    file_size = head.content_length
    sample = None
    if candidate.priority == "high" and file_size and file_size < 500_000_000:
        sample = await shallow_inspect(candidate.download_url, range_bytes=50_000)

    return ProcessedCandidate(
        type=DataSourceType.DOWNLOADABLE_FILE,
        file_spec=FileSpec(
            download_url=candidate.download_url,
            file_format=detect_format(candidate.download_url, head.content_type),
            file_size=file_size, content_type=head.content_type,
            column_headers=sample.headers if sample else None,
            row_sample=sample.rows[:5] if sample else None, ...
        )
    )
```

**浅层检查**解决"文件名像但内容不对"——用户要 GDP 数据，文件名含 GDP 但内容是平减指数系数。只下载前 50KB 成本极低但价值极大。

#### 4.5.3 Embedded Data Processor（五层 Cascading）

```
网页 URL
    |
    v
[Page Fetch] Jina Reader (免费) / Firecrawl (fallback)
    |
    v
[Tier-0] 确定性检测 (免费, 毫秒级)
    |  extruct: JSON-LD / Microdata / RDFa / OpenGraph
    |  pandas.read_html: <table> 检测
    |  XHR/API endpoint 发现
    |  命中 → 标记 extraction_method + confidence=1.0
    |
    v
[Tier-1] 启发式打分 (免费)
    |  正文长度 / 链接密度 / 关键词 TF-IDF / 重复 DOM 结构
    |  低分 → 丢弃
    |
    v
[Tier-2] 小模型 LLM 分类器 (Haiku)
    |  输入: target_schema + 截断 Markdown (4k tokens)
    |  输出: {relevant, coverage, fields_present, data_shape,
    |         estimated_record_count, extraction_difficulty,
    |         is_list_page, detail_link_pattern}
    |  coverage < 0.6 或 relevant=false → 丢弃
    |
    v
[Tier-3] 列表页展开 (仅 is_list_page=true 触发)
    |  /map + URL 模式聚类 + 采样详情页 + 构建数据页面树
    |  详见 4.5.4 节
    |
    v
[标记为 EmbeddedSpec] (不执行实际抽取)
```

**关键设计决策**：

1. **Tier-0 能免费处理掉 20-40% 的页面**，JSON-LD 在商品/文章/事件页大概率存在。
2. **不执行实际数据抽取**——Agent 的职责是发现和评估数据源，不是执行抽取。
3. **Jina Reader 优先 + Firecrawl fallback**：默认用免费的 Jina Reader，省 50%+ credit。
4. **列表页必须展开到详情页**，构建完整数据页面树——这是 Embedded 类数据源的"Portal Profiler"等价物。

#### 4.5.4 Tier-3：列表页检测与数据页面树构建

**核心洞察**：很多内嵌数据分布在"列表→详情"结构中。列表页只有摘要，详情页才有完整数据。不展开就严重低估数据源价值。

##### 什么算"列表页"

**包含多个同类数据实体，每个实体可链接到更详细页面**的任何形态：

| 形态 | 例子 |
|------|------|
| 搜索结果页 | 携程酒店搜索、Amazon 商品搜索 |
| 卡片网格 | Airbnb 房源、大众点评餐厅 |
| 表格列表 | Wikipedia 国家列表、SEC EDGAR |
| 目录/索引 | GitHub Trending、HuggingFace Models |
| 排行榜 | QS 大学排名、Fortune 500 |

##### 列表页判定

在 Tier-2 LLM 分类时一并完成，增加输出字段：

```python
class Tier2Output(BaseModel):
    # ... 原有字段 ...
    is_list_page: bool              # 是否为列表页
    list_item_pattern: str | None   # 列表项的模式描述
    detail_link_pattern: str | None # 详情页链接的 URL 模式
    list_page_rationale: str | None # 判定理由
```

##### 列表页展开流程（五步）

```
Step 1: /map 获取同站 URL 骨架 (1 credit, 2-8s)
    |
    v
Step 2: URL 模式聚类
    |  extract_path_skeleton: /hotel/436215.html → /hotel/{id}.html
    |  按骨架分组，识别 detail / pagination / category 三类模式
    |
    v
Step 3: 采样 2-3 个代表性详情页 /scrape
    |  确认详情页实际字段结构
    |
    v
Step 4: LLM 构建 DataPageTree
    |  输入: 列表页内容 + URL 模式分组 + 采样详情页
    |  输出: 完整树结构 JSON
    |
    v
Step 5: 字段递进分析
    |  列表页字段 vs 详情页字段 vs 用户 target_schema
    |  → 建议: list_sufficient / detail_required / detail_recommended / insufficient
```

##### LLM 输出示例（携程酒店）

```json
{
    "root": {
        "url": "hotels.ctrip.com/hotels/list?city=1",
        "page_type": "list",
        "title": "北京酒店搜索结果",
        "depth": 0,
        "fields_available": ["hotel_name", "price", "rating", "star_level",
                             "location_district"],
        "record_count": 30,
        "is_sampled": true,
        "children": [
            {
                "url": "hotels.ctrip.com/hotel/436215.html",
                "page_type": "detail", "depth": 1,
                "fields_available": ["hotel_name", "price", "rating",
                    "address", "phone", "amenities", "room_types",
                    "review_count", "latitude", "longitude"],
                "is_sampled": true, "children": []
            },
            {
                "url": "hotels.ctrip.com/hotel/{id}.html",
                "page_type": "detail", "depth": 1,
                "title": "约 4200 个酒店详情页",
                "is_sampled": false, "children": []
            }
        ]
    },
    "total_detail_pages": 4200,
    "sampled_detail_pages": 3,
    "field_progression": {
        "list_page": ["hotel_name", "price", "rating", "star_level", "district"],
        "detail_page": ["hotel_name", "price", "rating", "address", "phone",
                        "amenities", "room_types", "review_count", "lat", "lng"]
    },
    "tree_summary": "两层结构: 列表页(140页×30条≈4200家)展示摘要, 详情页多出约8个字段"
}
```

##### 字段递进分析

```python
def analyze_field_progression(list_fields, detail_fields, target_schema):
    list_set, detail_set = set(list_fields), set(detail_fields)
    target_set = set(target_schema.get("properties", {}).keys()) if target_schema else set()

    list_cov = len(list_set & target_set) / len(target_set) if target_set else 0
    detail_cov = len(detail_set & target_set) / len(target_set) if target_set else 0

    if list_cov >= 0.9:   return "list_sufficient"
    if detail_cov >= 0.9: return "detail_required"
    if detail_cov >= 0.6: return "detail_recommended"
    return "insufficient"
```

##### Portal Profiler vs Tier-3 列表页展开

| | Portal Profiler | Tier-3 列表页展开 |
|-|----------------|------------------|
| **触发对象** | data.gov 等开放数据门户 | ctrip.com 等商业/内容网站 |
| **触发条件** | PortalDetector 识别为门户 | Tier-2 LLM 判定 is_list_page=true |
| **目标** | 找出门户里的数据集/API/下载链接 | 展开列表→详情的完整数据结构 |
| **/scrape 策略** | 10-20 页（内容差异大） | 2-3 页采样（结构相同） |
| **输出** | 多个独立 DataSource 对象 | 一棵 DataPageTree 作为 EmbeddedSpec 附属 |
| **所在阶段** | Stage 3 (Discovery) | Stage 4 (Processing) |

---

### 4.6 Stage 5: Normalize & Dedupe（归一化与去重）

#### 归一化

每个来源类目有独立 Adapter，不用通用解析器（详见第 7 节 Adapter 体系）：

```python
ADAPTERS = {
    "openalex": OpenAlexAdapter,
    "huggingface": HuggingFaceAdapter,
    "kaggle": KaggleAdapter,
    "ckan": CKANAdapter,        # 复用于 data.gov / data.europa.eu
    "github": GitHubAdapter,
    "web_generic": WebGenericAdapter,
}
```

#### 去重（两层）

**Layer 1: URL 规范化** — 去掉 trailing slash / fragment / 追踪参数 / HTTP→HTTPS 统一。

**Layer 2: Embedding 语义聚类** — 对 `name + description` 做 embedding（voyage-3 / bge-m3），余弦相似度 > 0.92 视为同一资源的不同落地页，保留 provider 权威性最高的版本。通常减少 30-50% 候选。

---

### 4.7 Stage 6: Two-Stage Judging

详见第 5 节。

### 4.8 Stage 7: Reflect & Gap Check

```python
class CriticOutput(BaseModel):
    is_sufficient: bool
    coverage_analysis: str
    gaps: list[str]
    quality_issues: list[str]
    next_round_feedback: str | None   # 给下一轮 Planner 的具体反馈
    new_keywords: list[str] | None
    new_registries: list[str] | None
    new_portal_urls: list[str] | None # 建议探测的新门户 → 触发 /map 来源④
```

**原则**：80% 覆盖且质量可接受就返回。只在存在**明确、可解决**的缺口时启动下一轮。**硬性上限 3 轮**。

### 4.9 Stage 8: Finalize & Output

```python
class FinalReport(BaseModel):
    query: str
    requirement: StructuredRequirement

    # === 三类数据源分组输出 ===
    api_sources: list[DataSource]
    file_sources: list[DataSource]
    embedded_sources: list[DataSource]

    # === 汇总 ===
    all_sources_ranked: list[DataSource]    # 跨类型统一排序
    total_found: int
    coverage_summary: str

    # === 使用指南 ===
    api_quickstart_guide: str | None
    file_download_guide: str | None
    embedded_extraction_guide: str | None

    # === 元信息 ===
    iterations: int
    total_candidates_screened: int
    processing_time_seconds: float
    estimated_cost_usd: float
```

---

## 5. Judge 系统（五维评分）

**核心原则：不让 LLM 凭空打分。** 五个维度中只有 Relevance 必须由 LLM 判断，其余全部或部分由确定性代码完成。

### 5.1 五维评分体系

| 维度 | 打分方式 | 权重 | 说明 |
|------|---------|------|------|
| **Relevance** | LLM + 严格 Rubric | 0.40 | 数据内容与用户需求的匹配度 |
| **Authority** | 域名先验表 + 元数据信号 + LLM 兜底 | 0.20 | 数据源的权威性和可信度 |
| **Freshness** | 确定性代码（指数衰减） | 0.15 | 数据时效性 |
| **Accessibility** | 确定性代码（查表） | 0.15 | 获取数据的难度 |
| **License Fit** | 硬规则（SPDX 匹配） | 0.10 | 许可证是否满足用户约束 |

### 5.2 各维度评分细则

#### Relevance（LLM，用严格 Rubric 约束）

```
10: 完全匹配 — 恰好提供用户需要的域、类型、地理和时间范围
8-9: 强匹配 — 主要维度符合,次要约束有小差距
6-7: 部分匹配 — 核心主题一致,至少一个关键约束不符
4-5: 弱匹配 — 主题相关但需大量处理
2-3: 边缘相关 — 只是话题沾边
0-1: 不相关
```

强制 LLM 在 rationale 中列出：匹配的维度、不匹配的维度、"为什么比泛泛 Google 结果好"。

#### Authority（三层混合）

- **Layer 1**: 域名先验表（人工维护 500+ 条，确定性）
- **Layer 2**: 元数据信号微调（citations/downloads/stars → -2 to +2 调整）
- **Layer 3**: LLM 兜底（只对先验表未覆盖的源，只给元数据不给 URL）

#### Freshness（确定性计算）

基于 `last_updated` 距今天数 + 按领域设置不同半衰期（新闻 7 天 / 金融 30 天 / 科学 365 天）→ 指数衰减映射到 0-10。

#### Accessibility（查表）

OPEN=10 / FREE_REG=8 / API_KEY_FREE=7 / OAUTH=6 / API_KEY_PAID=4 / PAYWALL=2 / UNKNOWN=3。

#### License Fit（硬规则，绝不交给 LLM）

LLM 在"CC-BY-NC 能否商用"这类问题上会犯错且错得很自信。用 SPDX 规则表硬匹配。

### 5.3 聚合与硬否决

```python
def aggregate_score(scores: SourceScores) -> float:
    if scores.license_fit == 0: return -1   # 许可证不满足 → 淘汰
    if scores.relevance < 4:    return -1   # 相关性太低 → 淘汰
    return (scores.relevance * 0.40 + scores.authority * 0.20 +
            scores.freshness * 0.15 + scores.accessibility * 0.15 +
            scores.license_fit * 0.10)
```

### 5.4 两阶段 Judging（成本优化）

| | Stage-A 快筛 | Stage-B 精评 |
|-|-------------|-------------|
| 模型 | Haiku 4.5 | Sonnet 4.6 |
| 输入 | title + 一句描述 | 完整描述 + 样本 |
| 每批 | 10 个打包 | 每个独立调用 |
| 维度 | 只打 relevance | 完整五维 |
| 淘汰 | relevance < 5 | 硬否决规则 |
| 保留 | top 30 | 最终排序 |

成本从 80 次 Sonnet → 8 次 Haiku + 30 次 Sonnet = 降本 3-4 倍。

---

## 6. 技术选型

### 6.1 核心技术栈

| 层 | 选型 | 理由 |
|----|------|------|
| **Agent 编排** | LangGraph | 生产级事实标准；checkpoint、time-travel debug、流式、human-in-the-loop |
| **LLM（强）** | Claude Sonnet 4.6 | Planner、Critic、Stage-B Judging |
| **LLM（轻）** | Claude Haiku 4.5 | 批量快筛、Tier-2 分类、TypeClassifier、Portal Detector |
| **Embedding** | voyage-3 / bge-m3 | 去重聚类 |
| **通用搜索** | Exa + Brave + Tavily + SearXNG | 多源组合 |
| **页面抓取** | Jina Reader (免费优先) + Firecrawl (fallback) | Markdown 清洗、反爬、JS 渲染 |
| **确定性抽取** | extruct + pandas | JSON-LD/Microdata/HTML Table 零成本抽取 |
| **API 知识源** | APIs.guru + RapidAPI + public-apis | API 发现基石 |
| **向量库** | Qdrant / pgvector | 去重聚类（不是 RAG） |
| **缓存** | Redis | 多级缓存 |
| **速率限制** | aiolimiter + tenacity | 独立限流 + 指数退避 |
| **可观测性** | LangSmith / Langfuse | LLM 追踪 + 成本核算 |
| **任务队列** | Arq (Redis 底) | 长任务异步化 |
| **API 层** | FastAPI + SSE | 流式返回 |

### 6.2 MCP 工具协议层

把所有数据源工具包装成 MCP tool：

```
exa_search / brave_search / tavily_search / searxng_search
firecrawl_scrape / firecrawl_map / firecrawl_extract / jina_read
openalex_search / semantic_scholar_search / arxiv_search
huggingface_datasets_search / kaggle_search / zenodo_search
ckan_search / worldbank_search / fred_search / eurostat_search
github_search / apisguru_search / wikidata_sparql
```

好处：换框架、换 LLM 都不用重写工具层。

### 6.3 数据存储

| 存储 | 用途 |
|------|------|
| PostgreSQL | 用户查询历史、DataSource 持久化、域名先验表、门户白名单、审计日志 |
| Redis | 三级缓存 + 任务队列 + 速率限制计数器 |
| Qdrant/pgvector | DataSource embedding 用于去重聚类 |

---

## 7. 垂类目录 Adapter 体系

### 7.1 为什么必须提前接入

每个垂类 API 的协议完全不同（URL 结构、认证方式、分页机制、返回字段）。**不可能让 LLM 在运行时临时猜出怎么调用**。必须提前写好每个 API 的 Adapter。

**注意**："提前接入" = 写好调用代码，**不是**提前爬数据。运行时实时查询，永远拿最新数据。

### 7.2 Adapter 基类

```python
class BaseRegistryAdapter(ABC):
    name: str                    # "openalex", "huggingface_datasets", ...
    domains: list[str]           # 适用领域
    worker_tags: list[str]       # 所属 Worker 标签
    rate_limit: RateLimit
    requires_auth: bool

    @abstractmethod
    async def search(self, keywords: list[str], filters: dict) -> list[RawCandidate]: ...

    @abstractmethod
    def normalize(self, raw: dict) -> DataSource: ...

    async def health_check(self) -> bool: ...
```

### 7.3 插件注册机制

```python
class AdapterRegistry:
    _adapters: dict[str, BaseRegistryAdapter] = {}

    @classmethod
    def register(cls, adapter: BaseRegistryAdapter):
        cls._adapters[adapter.name] = adapter

    @classmethod
    def get_by_worker(cls, worker_name: str) -> list[BaseRegistryAdapter]:
        return [a for a in cls._adapters.values() if worker_name in a.worker_tags]

# CKAN 是复用典范——同协议，不同 base_url
AdapterRegistry.register(CKANAdapter(base_url="https://catalog.data.gov"))
AdapterRegistry.register(CKANAdapter(base_url="https://data.europa.eu/api"))
AdapterRegistry.register(CKANAdapter(base_url="https://data.gov.uk"))
```

### 7.4 分批接入优先级

| 优先级 | 时间 | Adapter | 免费？ | 复杂度 |
|--------|------|---------|--------|--------|
| **P1 MVP** | Week 1-2 | Exa / Brave / Tavily (搜索) | 有额度 | 低 |
| **P2 核心** | Week 2-3 | OpenAlex / Semantic Scholar / HuggingFace / Kaggle / CKAN | 全免费 | 低-中 |
| **P3 扩展** | Week 3-4 | World Bank / FRED / Eurostat / GitHub / APIs.guru / Zenodo | 免费 | 低 |
| **P4 专业** | Week 5-6 | GDELT / Wikidata / arXiv / PubMed / NASA EarthData | 免费 | 中-高 |
| **P5 长尾** | 按需 | Polygon / Alpha Vantage / Crossref / Figshare / DataCite | 多免费 | 低 |

**单个 Adapter 约 100-300 行代码。** 简单的（CKAN/OpenAlex）半天；复杂的（Wikidata SPARQL）1-2 天。

### 7.5 每个 Adapter 的职责

1. URL 构建（该 API 规范的请求格式）
2. 认证注入（API Key / Bearer / 无认证）
3. 查询翻译（统一 keywords+filters → 该 API 的查询语法）
4. 分页处理（cursor / offset / page）
5. 响应解析（JSON path 各不相同）
6. 错误处理 + 速率限制
7. 归一化到统一 DataSource
8. **元数据保留**（citations / downloads / stars → Judge 打分信号）
9. 健康检查

---

## 8. Firecrawl 使用策略

### 8.1 定位

**不是搜索引擎**，是"URL → LLM 可消化文本"的基础设施。处于搜索层之后、LLM 处理层之前。

### 8.2 四个端点的精确用法

| 端点 | 功能 | 消耗 | 在本方案中的用途 |
|------|------|------|----------------|
| `/scrape` | 单页精读 → Markdown | 1 credit | Judge 证据层 + 详情页采样 + 门户定向抓取 |
| `/map` | 整站 URL 骨架 | 1 credit | Portal Profiler + Tier-3 列表页展开 |
| `/extract` | 按 Schema 抽结构化字段 | ~1 credit | 数据集详情页元数据抽取 |
| `/crawl` | 全站递归爬取 | N credits | **不用**（太贵太慢） |

### 8.3 Jina Reader 优先 + Firecrawl Fallback

```python
async def fetch_page_content(url: str) -> str:
    try:  # Level 1: Jina Reader（免费）
        resp = await http_get(f"https://r.jina.ai/{url}", timeout=15)
        if resp.status == 200 and len(resp.text) > 200:
            return resp.text
    except: pass
    # Level 2: Firecrawl（付费，处理 JS 渲染 / Cloudflare 等）
    return await firecrawl.scrape(url)
```

大约 60-70% 的页面 Jina Reader 就够了，省 60%+ Firecrawl credit。

### 8.4 什么时候不用 Firecrawl

- 目标源有官方 API → **永远优先 API**
- 超轻量抓取 → Jina Reader
- 需要登录/多步骤交互 → 自写 Playwright
- 每月 50 万页以上 → 自建 Playwright + 代理池更划算

---

## 9. 成本控制与缓存

### 9.1 成本估算（单次查询）

| 环节 | 调用次数 | 单次成本 | 小计 |
|------|---------|---------|------|
| Intent Parser (Sonnet) | 1 | ~$0.01 | $0.01 |
| Source Router LLM 补充 (Haiku) | 1 | ~$0.002 | $0.002 |
| Web Search APIs | 4-8 | ~$0.002 | $0.01 |
| Registry APIs | 3-5 | 免费 | $0 |
| Portal Detector LLM (Haiku) | 1 | ~$0.002 | $0.002 |
| Portal Profiler /map | 1-3 | ~$0.004 | $0.01 |
| Portal Profiler /scrape | 10-15 | ~$0.004 | $0.05 |
| Jina Reader | 10-20 | 免费 | $0 |
| Firecrawl /scrape (fallback) | 3-5 | ~$0.004 | $0.02 |
| TypeClassifier LLM (Haiku) | 1 | ~$0.002 | $0.002 |
| Tier-2 LLM 分类 (Haiku) | 5-10 | ~$0.001 | $0.005 |
| Tier-3 列表页 /map+/scrape | 1+3 | ~$0.004 | $0.02 |
| Tier-3 树构建 (Haiku) | 1-3 | ~$0.003 | $0.006 |
| Stage-A Judging (Haiku, 批量) | 8 批 | ~$0.002 | $0.016 |
| Stage-B Judging (Sonnet) | 30 | ~$0.005 | $0.15 |
| Critic (Sonnet) | 1-3 | ~$0.01 | $0.03 |
| Embedding (去重) | 1 批 | ~$0.001 | $0.001 |
| **总计** | | | **~$0.08-0.30** |

### 9.2 七大成本优化手段

1. **两阶段 Judging** — 小模型批筛 + 强模型精评，降本 3-4x
2. **Jina Reader 优先** — 省 60%+ Firecrawl credit
3. **多级 Redis 缓存** — 重复查询近零成本
4. **批量 LLM 调用** — 单批 5-10 个，Batch API 再降 50%
5. **Tier-0 确定性检测** — 免费处理 20-40% 内嵌数据判断
6. **Tier-3 采样策略** — 列表页展开只采样 2-3 个详情页，不抓全部
7. **硬上限** — 最多 3 轮反思、最多 80 候选进 Stage-B、最多 5 个门户 /map

### 9.3 三级缓存策略

```
L1: 搜索 API 响应         TTL = 1 小时    Redis
L2: 页面抓取内容           TTL = 6-24 小时  Redis
L3: Judging 结果(需求+源)  TTL = 24 小时    Redis
```

---

## 10. 安全与合规

### 10.1 许可证处理
- Normalizer 遇到未知许可证填 `unknown`，Judge 扣分
- 用户要求 `commercial` 时，非商用许可证**硬否决**（淘汰，不是扣分）
- **绝不让 LLM 判断许可证兼容性** — 用 SPDX 规则表

### 10.2 PII 过滤
用户查询经过轻量检测器，包含身份证号/手机号/邮箱等 PII 直接拒绝。

### 10.3 反爬合规
- 遵守 `robots.txt` / Per-domain 速率限制（默认 2 req/s）
- 优先走官方 API，爬虫只是兜底
- 商业受保护源（Bloomberg、Wind 等）主动跳过

### 10.4 API Key 安全
- Key 绝不出现在 LLM prompt/completion 或前端/日志
- 静态存储 AES-256-GCM 加密，主密钥放 KMS
- 代码中用占位符 `${PROVIDER_KEY}`，运行时后端注入

---

## 11. 可观测性与评估

### 11.1 Observability Stack

```
┌──────────────────┬─────────────────────────────────────┐
│ LLM Trace        │ LangSmith / Langfuse                 │
│                  │ 每次调用: prompt, response, cost      │
├──────────────────┼─────────────────────────────────────┤
│ Judge Trace      │ 每个 DataSource 五维评分:             │
│                  │ 数值 + 来源(rule/prior/llm) + 理由   │
├──────────────────┼─────────────────────────────────────┤
│ Decision Trace   │ PortalDetector / TypeClassifier /    │
│                  │ Tier-2 分类 / 列表页判定的决策记录    │
├──────────────────┼─────────────────────────────────────┤
│ Business Metrics │ 检索命中率 / 类目分布 / 去重压缩比 /  │
│                  │ 端到端成功率 / P95 延迟 / 单次成本    │
├──────────────────┼─────────────────────────────────────┤
│ Alerts           │ 失败率异常 / 成本 > $0.50 /          │
│                  │ API 健康检查失败 / SearXNG 被反爬     │
└──────────────────┴─────────────────────────────────────┘
```

### 11.2 金标集评估

从第一天就建 30-50 条金标集。每条：用户查询 + 期望 top 5 数据源 + 覆盖维度要求。

指标：**Recall@10** / **NDCG@10** / 平均成本 / 平均延迟。每次改 prompt 后回归跑一遍，指标不降才合并。

### 11.3 用户反馈回路

每个源卡片放"有用/无用"按钮。每周拉低分 case 分析根因：
- relevance 打分错了 → prompt 问题
- authority 先验表缺域名 → 扩表
- 归一化 adapter 丢了信息 → adapter bug
- 门户未被识别 → 扩充白名单或调 LLM 门户检测

---

## 12. 落地路线图

```
Week 1: MVP (最小闭环)
├── LangGraph 骨架 + Claude Sonnet/Haiku
├── Intent Parser → Web Worker (Exa + Tavily) → 简单评分 → Finalize
├── TypeClassifier 基础版（Layer 1 URL 模式匹配）
├── 只输出 API + 文件两类数据源
└── 里程碑: "帮我找全球天气数据" → 返回 5-10 个数据源

Week 2: v0.2 (垂类目录 + 路由)
├── Source Router (四层规则表 + LLM 补充)
├── Academic Worker (OpenAlex + Semantic Scholar Adapter)
├── Datasets Worker (HuggingFace + Kaggle Adapter)
├── Brave Search + SearXNG
├── TypeClassifier 完整版（Layer 1-3）
└── URL 去重 + 基础归一化

Week 3: v0.3 (门户深挖 + 内嵌数据)
├── Portal Detector (三层 cascading，含 LLM 兜底)
├── Portal Profiler (/map + 定向 /scrape)
├── Embedded Processor (Tier-0 extruct + Tier-1 启发式 + Tier-2 Haiku)
├── Jina Reader 集成
└── 三类数据源完整覆盖

Week 4: v0.4 (Judge 系统 + 列表页)
├── Two-Stage Judging 完整实现
├── 五维评分: 确定性维度 + LLM Relevance
├── 域名先验表 (初始 200+ 条)
├── 硬否决规则 + Embedding 语义去重
├── Tier-3 列表页检测 + 数据页面树构建
└── Gov Worker + Finance Worker Adapter

Week 5: v0.5 (Agent 循环 + 生产化)
├── Reflect & Gap Check (最多 3 轮，含 /map 来源④)
├── /map 五路触发全部打通
├── LangSmith/Langfuse 接入
├── Redis 三级缓存 + 速率限制
└── Portal Profiler 预算控制

Week 6: v0.6 (质量保障)
├── 构造 30 条金标集
├── 首轮评估 + Prompt 调优
├── 门户白名单扩充至 200+
├── 成本分析 + 优化
└── P4 专业领域 Adapter (GDELT/Wikidata/arXiv)

Week 7-8: v1.0 (生产就绪)
├── SSE 流式返回 + 进度通知
├── Human-in-the-loop (LangGraph interrupt)
├── 前端卡片渲染（含 DataPageTree 可视化）
├── 用户反馈收集 (有用/无用)
├── 审计日志 + 安全加固
└── 部署 + SLA/SLO
```

---

## 13. 反模式清单

| # | 反模式 | 对策 |
|---|--------|------|
| 1 | 单一搜索 API | 必须多源组合，单源覆盖上限 30-40% |
| 2 | 一个大 ReAct 循环包办一切 | 拆成八个独立 Stage + TypeClassifier 桥梁 |
| 3 | 让 LLM 凭空打权威性分 | 域名先验表 + 元数据信号 + LLM 兜底 |
| 4 | 让 LLM 判断许可证兼容性 | SPDX 硬规则，合规事故赔不起 |
| 5 | 忽略双语关键词 | Intent Parser 强制生成英文关键词 |
| 6 | 全部候选都用强模型精评 | 两阶段 Judging，小模型批筛 + 强模型精评 |
| 7 | 把 Perplexity Sonar 当搜索用 | 它返回合成答案，没有原始源列表 |
| 8 | 没有 Tier-0 确定性检测 | 白白浪费 20-40% 免费处理机会 |
| 9 | 不做浅层文件内容检查 | "文件名像但内容不对"是常见假阳性 |
| 10 | 预先爬数据到向量库 | 存储和维护成本爆炸且永远滞后，应实时查目录 API |
| 11 | 没有金标集就调 prompt | 纯凭感觉，越调越差 |
| 12 | 忽略 Portal Profiler | 只看搜索返回的首页，错过 80% 相关页面 |
| 13 | 门户识别纯用白名单不加 LLM | 无法穷举全球门户，漏检代价极高（丢失一整棵数据树） |
| 14 | 把商业数据网站当门户 /map | ctrip.com 不是 data.gov，应走 Embedded Processor |
| 15 | 内嵌数据只看列表页不展开详情页 | 列表页覆盖率可能只有 50%，详情页才有完整数据 |
| 16 | 搜索结果不做类型判定直接处理 | 同一搜索结果可能是 API / 文件 / 内嵌数据 |
| 17 | 不遵守 robots.txt | 法律风险 + 被封 IP |
| 18 | 不做缓存 | 重复查询反复烧钱 |

---

## 附录

### 附录 A：LangGraph 状态机伪代码

```python
from langgraph.graph import StateGraph, END

class AgentState(TypedDict):
    query: str
    requirement: StructuredRequirement | None
    activated_workers: list[str]
    raw_candidates: list[RawCandidate]
    type_classified: list[TypeClassifiedCandidate]  # TypeClassifier 输出
    processed_candidates: list[ProcessedCandidate]
    normalized_sources: list[DataSource]
    deduplicated_sources: list[DataSource]
    scored_sources: list[DataSource]
    critic_feedback: CriticOutput | None
    iteration: int
    max_iterations: int  # = 3
    portal_budget: PortalProfilerBudget

graph = StateGraph(AgentState)

graph.add_node("parse_intent", parse_intent_node)
graph.add_node("route_sources", route_sources_node)
graph.add_node("discover", discover_node)              # fan-out: Web + Registry + Portal + LLM Prior
graph.add_node("classify_types", type_classifier_node)  # TypeClassifier 桥梁
graph.add_node("process_by_type", type_process_node)    # API/File/Embedded 三路分流
graph.add_node("normalize_dedupe", normalize_dedupe_node)
graph.add_node("judge", two_stage_judge_node)
graph.add_node("reflect", reflect_node)
graph.add_node("finalize", finalize_node)

graph.add_edge("parse_intent", "route_sources")
graph.add_edge("route_sources", "discover")
graph.add_edge("discover", "classify_types")
graph.add_edge("classify_types", "process_by_type")
graph.add_edge("process_by_type", "normalize_dedupe")
graph.add_edge("normalize_dedupe", "judge")
graph.add_edge("judge", "reflect")

graph.add_conditional_edges(
    "reflect",
    lambda s: "route_sources" if (
        not s["critic_feedback"].is_sufficient
        and s["iteration"] < s["max_iterations"]
    ) else "finalize"
)
graph.add_edge("finalize", END)

graph.set_entry_point("parse_intent")
app = graph.compile(checkpointer=MemorySaver())
```

### 附录 B：流式返回设计

端到端延迟预期 30s-5min，**流式返回是 UX 的生命线**。

```python
@app.get("/api/discover")
async def discover(query: str):
    async def event_stream():
        async for event in agent.astream_events({"query": query}):
            if event["event"] == "on_chain_start":
                yield sse_event("stage", event["name"])
            elif event["event"] == "on_tool_end":
                yield sse_event("progress", {
                    "stage": current_stage,
                    "candidates_found": len(state.raw_candidates),
                    "iteration": state.iteration,
                })
            elif event["event"] == "partial_result":
                yield sse_event("sources", event["data"])
        yield sse_event("done", final_report)
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

### 附录 C：方案核心总结

**用 LangGraph 做编排，Claude Sonnet 4.6 做规划和 Judging，Haiku 做批量 Worker。**

**搜索层**：通用 Web 搜索（Exa + Brave + Tavily + SearXNG）和垂类目录 API（通过提前接入的 Adapter 实时调用 OpenAlex、HuggingFace、CKAN、APIs.guru 等）并行执行。

**深挖层**：Portal Detector 三层 cascading（白名单 + URL 模式 + LLM 兜底）识别数据门户，通过五个来源触发 Firecrawl /map 做门户深度探测。Jina Reader 优先 + Firecrawl /scrape 做页面获取。

**类型判定层**：TypeClassifier 三层 cascading（URL 模式 + HEAD 探测 + LLM）对搜索结果做类型判定。一个 URL 可同时判定为多种类型。Registry 和 Portal Profiler 的产出类型已确定，直接通过。

**处理层**：三类数据源（API / 可下载文件 / 网页内嵌数据）走各自 Type-Specific Processor。Embedded Processor 含五层 Cascading（Tier-0 确定性检测 → Tier-1 启发式 → Tier-2 LLM 分类 → Tier-3 列表页展开构建数据页面树）。

**评分层**：归一化成统一 DataSource Schema 后，两阶段评分（确定性算 Freshness/License/Accessibility + 域名先验算 Authority + LLM 算 Relevance）做五维判断，配合硬否决规则。

**循环层**：反思回路最多 3 轮，可触发新一轮搜索和 /map 探测。

**配套**：金标集评估、LangSmith 可观测性、Redis 多级缓存、速率限制、用户反馈回路。

**单次查询成本 $0.08-0.30。6-8 周可落地生产。**

**核心心法：结构化一切，规则优先 LLM 兜底（但该用 LLM 时果断用），分层检索多源组合，三类数据源统一抽象但分流处理，所有决策可观测可解释。**
