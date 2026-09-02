# Zillusion —— 数据源发现 + 爬虫构建 Agent

[English](README.md) | **中文**

用一句大白话说出你的数据需求，得到**一份经过筛选的数据源清单、一个验证过能跑的爬虫，
或者一份现成的数据集**——全程由 LLM agent 驱动。

[![License](https://img.shields.io/badge/license-Elastic%20License%202.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)
[![Stars](https://img.shields.io/github/stars/Baze-Bai/Zillusion?style=social)](https://github.com/Baze-Bai/Zillusion/stargazers)

![Zillusion 演示](docs/demo.gif)

*输入一句话——「按国家统计的全球 CO2 排放」——它按 5 个维度给 13 个真实数据源打分排序，
为你选中的那个构建爬虫、验证它，然后跑出一份可下载的数据集。真实录屏；地址栏是
`127.0.0.1`，因为它就跑在本机。*

## 凭什么不是又一个爬虫工具

难的从来不是写爬虫，而是**找到对的源**；而写好的爬虫，会在网站下次改版时挂掉。
这里有两样东西专门对付这两件事——即使你最后用别的工具，这两点也值得借鉴：

- **验证器无法给自己的活儿盖章。** 爬虫由一个**独立、只读**的 agent 会话评判，
  它**物理上无法修改**自己正在评判的 workflow。它最锋利的一项检查是**重新读取真实页面**，
  确认每个字段的含义与选择器声称的一致——要抓的正是「选择器悄悄绑到了点赞数而不是评论数」
  这类错误。结论由评分卡按硬性门槛算出，不是模型自己论证出来的。
- **反爬知识会累积，而不是每次重新发现。** 真实的、带日期的笔记——CDN 签名 URL、
  fresh-context 指纹、软登录墙、水合陷阱——跨运行持久化，某个模式重复出现后会晋升为
  跨站技能。多数工具每一次运行都要从零重新学一遍同样的封锁。

你问*「哪里能拿到关于 X 的数据？」*，agent 跑完这条流水线：

```
discover → explore → validate → run →（data）
```

1. **Discover 发现** —— 从数据源注册表 + 网页搜索中找出并排序候选源
   （API、可下载文件、页面内嵌表格）。
2. **Explore 探索** —— 对你选中的源，探测站点并写出一个能跑的爬虫（`workflow.py`）。
3. **Validate 验证** —— 运行该爬虫，并将其输出与真实页面比对。
4. **Run 运行** —— 全量执行已验证的爬虫 → 一份可下载的数据集。
5. **Data 数据产品**（可选，harness CLI）—— 清洗数据并构建数据产品（报告、图表、数据集）。

> ⚠️ **安全模型：** 出厂配置为**单租户、仅本机使用**（不含内置用户账号体系）。
> 在把它暴露到网络之前，请先读 [SECURITY.md](SECURITY.md)。

## 特性

- **端到端**：一个问题 → 排序后的数据源 → 爬虫 → 数据集。
- **三类数据源**：API、可下载文件、页面内嵌 / HTML 表格数据。
- **实时干预**：运行途中用对话引导或纠正 agent。
- **人工接管**：登录 / 验证码墙把控制权交给内嵌浏览器。
- **凭据闸门**：需要 key 的 API 会向你索取（**绝不自行注册**；key 不离开后端）。
- **边抓边出**：长时间爬取的结果流式落盘，数据集可下载。
- **自带模型**：DeepSeek、GLM（智谱）、Claude、OpenAI…… 经 LiteLLM 路由；
  agentic 节点通过 Anthropic 兼容端点使用 Claude Agent SDK。
- **默认不需要搜索 key**：自托管 SearXNG 元搜索 + 免费数据源注册表；
  想用商业搜索（Brave / Tavily / Exa）另填 key 即可。

## 负责任地使用 —— 它替你做什么，以及不做什么

这是一个研究与自用工具。它以你的名义、从你的机器、用你的 IP 去驱动真实浏览器访问真实站点。
**你要为你指向的目标负责。** 下面这份清单力求精确而非让人安心，因为**一条读者打开源码
就能证伪的合规声明，比没有声明更糟**。

**`robots.txt` 在每次导航前都会被抓取并判定——但你仍应自己去看一眼。**
`CRAWLER_ROBOTS_MODE` 有三档：

| 模式 | 行为 |
| --- | --- |
| `warn`（默认） | 抓取 robots.txt、做出判定、记录违规并在结果上附 `robots_warning`——然后照常导航 |
| `enforce` | 改为拒绝该次导航 |
| `off` | 跳过检查（限速仍然生效） |

**在你相信某个判定之前，请先读这段。** 解析器是 Python 的 `urllib.robotparser`，
它有两个限制，是我们**实测出来的、不是假设的**——两者都会让它**漏掉**规则，而绝不会凭空造出规则：

- `User-agent:` 与其 `Disallow:` 之间的空行会终结该条记录，其后的规则被丢弃。
  GitHub 的 robots.txt 恰好就是这么写的，于是 stdlib 认为它整个 `User-agent: *`
  段是空的。我们实测过：`https://github.com/search/advanced` 返回*允许*。
- 路径中的 `*` 与 `$` 在这里是**字面字符，不是通配符**。

所以 `allowed` 的含义是**「本解析器能读到的规则没有禁止它」**，而不是「站点许可」。
把它当作一张有洞的安全网，而不是通行证。若需要真正的规范符合性，
可通过 `RobotsPolicy(fetch=...)` 换入一个完整实现的解析器。

**Per-host 限速是真实的，且在所有模式下生效**（`off` 也不例外）：
`CRAWLER_MIN_HOST_INTERVAL_S`（默认 1 秒）会为同一 host 的连续请求拉开间隔，
robots.txt 里的 `Crawl-delay` 最多遵守到 10 秒。**它管不到的**是全量抓取阶段生成的
`workflow.py`——那是独立进程，只有当 agent 把限速写进了代码，它才会限速。
API manifest 上的 `rate_limit` 字段是 agent **记录**的描述性文字，没有任何东西执行它。

**发现侧的查询是有限速的**，因为那些是对着有公开配额的 API 说话：
注册表适配器各自带有上限（按源不同，1–10 req/s），扇出共享一个并发信号量。
那是限速真实存在的唯一位置，而它不是爬取目标站点的那一部分。

**它绝不以你的名义注册任何账号。** 当某个源需要 API key 时，运行会停下来请你自己把它写进
`inputs/<site>/credentials.json`；agent 没有任何注册账号的路径，而且被禁止回读该文件。

**登录墙与验证码交由人工处理，而不是被破解。** 越过认证墙的唯一受支持路径是
`browser_request_user_login`，它打开一个浏览器让**你**登录；没有验证码破解器，
agent 自己写登录脚本被明令禁止。这是刻意划定的边界，不是疏漏——但请注意后果：
你一旦登录，那个会话就是**你的**，站点条款中关于自动化访问的规定随之适用到你的账号上。

### 你的义务，任何设置都无法免除

上面这些闸门只是辅助。它们**不会使一次爬取变得合法**，也**不会把责任转移给本项目**。
运行它意味着发出请求的人是你，因此请你：

1. **遵守 `robots.txt`** —— 包括本解析器读不到的那些部分（见上面两个限制）。
   事关重大时，请自己打开那个文件读一遍。
2. **阅读并尊重站点的服务条款（ToS）。** 很多站点在 robots.txt 里允许爬取，
   却在条款里加以限制；这是两份不同的文件，**有法律效力的是条款**。
3. **遵守适用于你的法律** —— 计算机滥用、著作权、数据库权，以及数据保护法
   （GDPR、CCPA、中国《个人信息保护法》及其同类）都涵盖网页抓取；
   什么算合法因法域而异，也取决于你采集什么。**涉及个人信息时门槛显著更高。**
4. **温和地爬。** 默认值是刻意放慢的。调高是你的决定，后果也由你承担：
   被你压垮的站点，是别人正在使用的真实服务。
5. **被拒即停。** 一次封禁、一个 `429`、一封停止函——把每一个都当作答复本身，
   而不是需要绕过的障碍。

许可证授予你的是软件，**不是**针对任何特定目标使用它的许可。
**它免除一切担保与责任；你爬取什么，后果由你承担。**

## 三种使用方式

| 形态 | 适合谁 | 你要运行什么 | 分量 |
|------|--------|--------------|------|
| **① 自托管 Web 应用** | 多数人 —— 在浏览器里走完 发现→构建→验证→运行 | `docker compose up`，访问 `localhost:3000` | 重（约 2.5GB 镜像；内含 harness 与 Chromium） |
| **② Harness CLI** | 开发者 / 脚本 / CI —— 一次一个站点，无发现阶段 | `python -m runtime.cli <cmd> <site>` | 中 |
| **③ Claude Code 技能** | 已经在用 Claude Code 的人 | 把 `skills/find-and-scrape-data` 放进 `.claude/skills/` | 轻（几乎零基础设施） |

三者共用同一套 harness 内核。**你始终自带 LLM API key**
（DeepSeek / GLM / Claude / OpenAI / …）——项目不捆绑任何 key。

**一次真实运行要花多少钱**，在这套栈上用 DeepSeek 实测得出，免得你去猜：
一次针对「带作者与标签的名言」的发现耗时 **8 分钟、返回 6 个源**；
为其中一个构建爬虫——explore、探测真实页面、写出 `workflow.py`、验证——
最终走到 `deterministic` 判定。对一个更宽的问题（「按国家统计的全球 CO2 排放」），
单是发现阶段就跑了 **11 分钟、$1.71**，返回 18 个源。
整个过程（两次发现 + 一次构建）合计 **$4.54**。
成本随你所用模型与站点难度变化，请把这些当作**数量级参考，不是报价**。

---

## ① 自托管 Web 应用（快速上手）

**前置条件：** Docker + Docker Compose，以及至少一个 LLM 供应商的 API key。

> **这套 compose 栈以开发模式运行**，是有意为之——前端走 `next dev`，
> 后端用 uvicorn `--reload`，因此改动源码会直接反映到运行中的栈上。
> 对它出厂设定的本机安装来说这正是你要的；但它**不是生产部署**，
> 结合 [SECURITY.md](SECURITY.md) 里的单租户安全模型，
> 不应就这样暴露到网络上。

```bash
git clone <your-repo-url> zillusion && cd zillusion

# 1. Compose 层配置
cp .env.example .env
#    设置 SEARXNG_SECRET   (openssl rand -hex 32)

# 2. 应用 / LLM 配置
cp backend/.env.example backend/.env
```

**在 `backend/.env` 里选择你的 LLM 供应商。** 模板默认使用 Claude：

- **Claude** —— 只需设置 `ANTHROPIC_API_KEY`，完成。
- **DeepSeek / GLM / 其他** —— 设置 key **并且**改掉模型字段，
  否则 agent 会去调用你的 key 无权访问的 Claude 模型：

  ```bash
  # DeepSeek
  DEEPSEEK_API_KEY=sk-...
  LLM_PRIMARY_REASONING=deepseek/deepseek-v4-pro
  LLM_PRIMARY_STRONG=deepseek/deepseek-v4-pro
  LLM_PRIMARY_FAST=deepseek/deepseek-v4-flash
  # GLM:  ZAI_API_KEY=...  配合  LLM_PRIMARY_*=zai/glm-4.7 , zai/glm-4.5-flash
  ```

起栈、验证、使用：

```bash
docker compose pull && docker compose up   # postgres + redis + searxng + backend + frontend
curl http://localhost:8000/api/v1/health   # -> {"status":"ok", ...}
# 打开 http://localhost:3000
docker compose down                        # 停止（加 -v 同时删除数据库卷）
```

`pull` 会从 GHCR 拉取预构建好的 backend 与 frontend 镜像
（`ghcr.io/baze-bai/zillusion-backend`、`ghcr.io/baze-bai/zillusion-frontend`），
每次推送到 `main` 都会为 **linux/amd64 与 linux/arm64** 各构建一份——
所以 Apple Silicon 的 Mac 是原生运行，而不是模拟 x86。`latest` 跟随 `main`；
`git pull` 之后记得再 `pull` 一次镜像。若想改用你本地检出的代码来构建这两个镜像
（backend 那个约 2.5GB，要等一阵子），用 `docker compose up --build`。
单独的 `docker compose up` 两者都不做：本地已有镜像就直接用，没有才会构建。

可选组件：

```bash
docker compose --profile embeddings up     # + qdrant（语义去重）
```

**页面抓取（Firecrawl）不随项目捆绑。** 可以让后端指向 Firecrawl Cloud
（`SEARCH_FIRECRAWL_USE_SELF_HOSTED=false` + `SEARCH_FIRECRAWL_API_KEY`），
或者自托管并设置 `SEARCH_FIRECRAWL_SELF_HOSTED_URL`。
两者都没有时，页面抓取会退化到 jina / httpx。

**在浏览器里：** 输入一个数据问题 → 从排序报告中挑选数据源 → **构建爬虫** →
**运行** → 下载数据集。全程可随时在对话中干预 agent；
登录 / 验证码墙会把控制权交给内嵌浏览器。

→ 数据源如何被搜索与抓取：
[docs/discovery-architecture.md](docs/discovery-architecture.md)。

---

## ② Harness CLI（无头 / 脚本化）

不经 Web UI，直接驱动单个站点。**一次性准备**（它自己的 venv）：

```bash
cd harness
python -m venv .venv && . .venv/bin/activate    # Windows: .\.venv\Scripts\Activate.ps1
pip install -c constraints.txt -e .             # -c 锁定已解析的依赖集合
playwright install chromium                     # 用于 JS 渲染的站点
# 在环境变量里导出你的 LLM key 与模型（与 backend/.env 同名）
```

然后：

```bash
python -m runtime.cli explore      <site_id>   # 探索一个站点
python -m runtime.cli explore-loop <site_id>   # explore -> validate 循环
python -m runtime.cli validate <site_id>
python -m runtime.cli run      <site_id>   # 全量生产抓取
python -m runtime.cli crawl    <site_id>   # agentic 抓取（动态站点）
python -m runtime.cli data     <site_id>   # 构建数据产品
```

把站点的 `goal.md` + `seed.json` 放在 `harness/inputs/<site_id>/` 下
（参考 `harness/inputs/example/`）。产物落在 `harness/workspaces/<site_id>/`。
更多：[harness/README.md](harness/README.md)。

> 注意：`--json` / `--quiet` / `--model` / `--max-turns` / `--vision` /
> `--permission-mode` 是**全局参数，必须写在子命令之前**，
> 例如 `python -m runtime.cli --json explore example`。

---

## ③ Claude Code 技能（零基础设施）

如果你已经在用 Claude Code，把技能拷进去然后直接提要求即可：

```bash
cp -r skills/find-and-scrape-data <your-project>/.claude/skills/
pip install -r skills/find-and-scrape-data/scripts/requirements.txt   # 可选辅助脚本
```

然后在 Claude Code 里说：*「帮我找关于 `<主题>` 的数据源」* 或
*「抓取 `<url>` 并给我做一个爬虫」*。它不需要后端、不需要 Firecrawl / SearXNG、
也不需要另外的 key——用你自己的 Claude 即可。完整指南：
[skills/find-and-scrape-data/README.md](skills/find-and-scrape-data/README.md)。

---

## 架构

```
                 前端 (Next.js)
                        │  SSE / REST
                        ▼
   后端 (FastAPI) ──────┬── 发现流水线 (LangGraph + agentic 超级节点)
                        ├── 判定 / 排序
                        ├── 数据源适配器  +  搜索 (SearXNG, …)
                        └── harness 编排器 ──► harness (Claude Agent SDK)
                                               explore→validate→run→crawl→data

   基础设施: postgres (会话/事件) · redis (缓存/限额) · searxng (搜索)
             · qdrant (可选, 去重) · firecrawl (可选, 自备)
```

后端的发现流水线负责找出并排序数据源；**发现→harness 桥接**
（`scripts/discovery_to_harness.py`）把选中的源暂存进 harness，
由 harness 构建、验证并运行爬虫。完整设计：
[docs/DATASOURCE_DISCOVERY_AGENT_DOC.md](docs/DATASOURCE_DISCOVERY_AGENT_DOC.md)、
[docs/UNIFIED_DATA_SOURCE_DISCOVERY_AGENT.md](docs/UNIFIED_DATA_SOURCE_DISCOVERY_AGENT.md)（均为中文）。

## 仓库结构

```
backend/                  FastAPI 服务
  src/agents/             发现流水线：LangGraph 节点 + agentic 超级节点
  src/judging/            多维数据源打分 + 否决
  src/adapters/           数据源注册表（学术/数据集/政府/代码/地理）
  src/classifiers/        URL / 类型 / 门户分类
  src/tools/              搜索（searxng/brave/tavily/exa）+ 抓取 + 验证
  src/services/           编排、事件存储、run 注册表、CDP 桥接……
  src/api/                路由 + 中间件 + SSE
  src/db/ , src/models/   持久化 + schema
  tests/                  单元测试
frontend/                 Next.js 对话式界面（src/{app,components,hooks,state}）
harness/                  Claude Agent SDK 运行时
  runtime/                explore / validate / run / crawl / data agent + CLI
  mcp_server/             浏览器 / 工作区 / 视觉工具
  .claude/ domain_skills/ agent 配置 + 跨站技能
skills/                   find-and-scrape-data —— Claude Code 技能（形态 ③）
scripts/                  发现 → harness 桥接
searxng/                  自托管 SearXNG 配置
docs/                     架构文档
docker-compose.yml        自托管栈
.env.example, backend/.env.example   配置模板（复制为 .env）
```

## 开发

```bash
# backend 测试套件（每行都从仓库根目录运行）
(cd backend && pip install -c constraints.txt -e ".[dev]" && pytest)
# harness 测试套件
(cd harness  && pip install -c constraints.txt -e .        && pytest)
```

## 文档

- [docs/discovery-architecture.md](docs/discovery-architecture.md) —— 数据源如何被搜索与抓取
- [docs/DATASOURCE_DISCOVERY_AGENT_DOC.md](docs/DATASOURCE_DISCOVERY_AGENT_DOC.md) —— 完整技术参考（中文）
- [docs/UNIFIED_DATA_SOURCE_DISCOVERY_AGENT.md](docs/UNIFIED_DATA_SOURCE_DISCOVERY_AGENT.md) —— 架构设计（中文）
- [SECURITY.md](SECURITY.md) —— 部署与安全模型
- [harness/README.md](harness/README.md) · [skills/find-and-scrape-data/README.md](skills/find-and-scrape-data/README.md)

## 参与贡献

欢迎提 issue 和 PR —— 见 [CONTRIBUTING.md](CONTRIBUTING.md)。
你能提供的最有价值的东西是**一个把它难住的站点**：哪个站、你想要什么数据、
以及在哪个阶段卡住了。

## 许可证

**[Elastic License 2.0](LICENSE)** —— 源码可见（source-available），
**不是 OSI 定义的开源**。这个区别是实质性的，值得你花两段话读完，
而不是从一个徽章去反推。

**你可以做的**，无需申请、无需付费：阅读、修改、运行、自托管——
**包括在公司内部用于商业工作**。把它部署在你自己的基础设施上供你的团队使用，
完全在许可范围内。fork 它、在它之上构建、分发修改后的副本，都可以。

**你唯一不能做的**是这里真正要紧的那一条限制，引自许可证原文：

> You may not provide the software to third parties as a hosted or managed
> service, where the service provides users with access to any substantial set
> of the features or functionality of the software.
>
> （你不得将本软件作为托管或受管服务提供给第三方，
> 使该服务的用户可以访问本软件的任何实质性功能集合。）

说白了：**自己跑随便，别拿去转卖成服务。** 这条线是刻意划在这里的——
同一条流水线的托管版是养活这个项目的东西，因此竞争性的托管服务被排除在外，
而其余一切用途（含商业用途）保持开放。

许可证还要求：你分发任何副本时须一并传递这些条款，并在修改过的副本中注明已修改。

**第三方服务保留各自的许可证。** Firecrawl（AGPL-3.0）与 SearXNG 是作为
**独立 HTTP 服务**运行的，本项目通过网络调用它们、从不链接其代码；
两者都不随本仓库分发。
