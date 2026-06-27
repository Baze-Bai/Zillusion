"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

export type Locale = "en" | "zh";

type Entry = { en: string; zh: string };

/** UI-chrome dictionary (point 9). LLM-generated content (descriptions, guides,
 * the requirement title, example queries) is intentionally NOT translated. */
const DICT: Record<string, Entry> = {
  // Activity indicator
  "thinking": { en: "Thinking", zh: "思考中" },
  "executing": { en: "Executing", zh: "执行中" },
  // Pipeline stages
  "stage.parse_intent": { en: "Parsing intent", zh: "解析意图" },
  "stage.agentic_discovery": { en: "Discovering sources", zh: "发现数据源" },
  "stage.judge": { en: "Scoring & ranking", zh: "打分与排名" },
  "stage.reflect": { en: "Reflecting", zh: "反思" },
  "stage.skill_writeback": { en: "Saving skills", zh: "保存技能" },
  "stage.diagnostics_writeback": { en: "Diagnostics", zh: "诊断" },
  "stage.finalize": { en: "Finalizing", zh: "收尾" },
  // Group headers
  "group.api": { en: "APIs", zh: "API 源" },
  "group.file": { en: "Downloadable files", zh: "可下载文件" },
  "group.embedded": { en: "Embedded data", zh: "网页内嵌数据" },
  // Conversation section (user + agent messages, below the task plan + data)
  "conversation.title": { en: "Conversation", zh: "对话" },
  // Requirement summary
  "req.domain": { en: "Domain", zh: "领域" },
  "req.formats": { en: "Formats", zh: "期望格式" },
  "req.geography": { en: "Geography", zh: "地理范围" },
  "req.timeRange": { en: "Time range", zh: "时间范围" },
  "req.updateFreq": { en: "Update frequency", zh: "更新频率" },
  "req.license": { en: "License", zh: "许可" },
  "req.budget": { en: "Budget", zh: "预算" },
  "req.subQuestions": { en: "Sub-questions", zh: "子问题" },
  // Source card
  "card.usageGuide": { en: "Usage guide", zh: "使用指南" },
  "card.score": { en: "score", zh: "评分" },
  "badge.api": { en: "API", zh: "API" },
  "badge.file": { en: "File", zh: "文件" },
  "badge.embedded": { en: "Embedded", zh: "网页内嵌" },
  "access.open": { en: "Open", zh: "开放" },
  "dim.relevance": { en: "Relevance", zh: "相关性" },
  "dim.authority": { en: "Authority", zh: "权威性" },
  "dim.freshness": { en: "Freshness", zh: "时效性" },
  "dim.access": { en: "Access", zh: "可获取性" },
  "dim.license": { en: "License", zh: "许可" },
  // Scraper sub-session
  "scraper.build": { en: "Build scraper", zh: "构建抓取器" },
  "scraper.view": { en: "View scraper →", zh: "查看抓取器 →" },
  "scraper.title": { en: "Scraper", zh: "抓取器" },
  "scraper.done": { en: "done", zh: "完成" },
  "scraper.building": { en: "building", zh: "构建中" },
  "scraper.awaitingKey": { en: "awaiting API key", zh: "等待 API Key" },
  // Credentials gate (keyed api source waiting for the user's key)
  "credgate.title": { en: "This API needs your key", zh: "该 API 需要你的 Key" },
  "credgate.why": {
    en: "The agent never signs up for keys itself. Get one from the provider, paste it below, and the build starts automatically.",
    zh: "Agent 不会代替你注册 Key。请到提供方获取后粘贴到下方,构建会自动开始。",
  },
  "credgate.signup": { en: "Get a key (opens provider signup)", zh: "去获取 Key(打开提供方注册页)" },
  "credgate.keyLabel": { en: "API key", zh: "API Key" },
  "credgate.submit": { en: "Save key & build", zh: "保存并开始构建" },
  "credgate.launching": { en: "Launching…", zh: "启动中…" },
  "credgate.storage": {
    en: "Stored locally in the harness inputs (gitignored); used by the explorer, the validator and the production run.",
    zh: "仅保存在本机 harness inputs 目录(已被 gitignore);探索、验证与正式爬取共用。",
  },
  // Signup fallback ladder (no precise signup_url — point at the provider site)
  "credgate.fallback.lead": {
    en: "No dedicated signup page was found. This API needs a key — find the Developer / API / Sign up entry on the provider's site to register.",
    zh: "没找到专属注册页。这个 API 需要 key——请到提供方网站找 Developer / API / Sign up 入口注册。",
  },
  "credgate.fallback.auth": { en: "Auth", zh: "鉴权方式" },
  "credgate.fallback.visit": { en: "Open the provider's site", zh: "打开提供方网站" },
  "credgate.fallback.docs": { en: "API documentation", zh: "API 文档" },
  "scraper.iter": { en: "iter", zh: "轮次" },
  "scraper.iterUnit": { en: "iters", zh: "轮" },
  "scraper.buildN": { en: "build", zh: "构建" },
  "scraper.exploring": { en: "exploring", zh: "探索中" },
  "scraper.validating": { en: "validating", zh: "验证中" },
  "scraper.sampleData": { en: "Sample data", zh: "样本数据" },
  "scraper.waitRun": { en: "Finish the current scraper first", zh: "等当前抓取器完成" },
  // Run (production crawl) phase — the third stage after explore→validate PASS
  "run.start": { en: "Run full crawl", zh: "运行全量爬取" },
  "run.starting": { en: "Starting…", zh: "启动中…" },
  "run.hint": {
    en: "Crawl all the data with the validated workflow",
    zh: "用已验证的工作流抓取全部数据",
  },
  "run.title": { en: "Run", zh: "运行" },
  "run.running": { en: "crawling", zh: "爬取中" },
  "run.done": { en: "done", zh: "完成" },
  "run.records": { en: "records", zh: "条" },
  "run.elapsed": { en: "elapsed", zh: "用时" },
  "run.lines": { en: "log lines", zh: "日志行" },
  "run.outcome.COMPLETE": { en: "Complete", zh: "完成" },
  "run.outcome.PARTIAL": { en: "Partial", zh: "部分完成" },
  "run.outcome.FAILED": { en: "Failed", zh: "失败" },
  "run.outcome.ABORTED": { en: "Aborted", zh: "已中止" },
  "run.download": { en: "Download data", zh: "下载数据" },
  "run.downloadFields": { en: "Download field doc", zh: "下载字段说明" },
  "run.rerun": { en: "Run again", zh: "重新爬取" },
  // Run gate — a terminal loop that did NOT pass, shown in the button's place
  "run.gate.inconclusive": {
    en: "Validation was inconclusive (INCONCLUSIVE) — full-crawl quality is not guaranteed.",
    zh: "验证未有定论(INCONCLUSIVE)— 全量爬取的数据质量没有保证。",
  },
  "run.gate.fail": {
    en: "Validation failed (FAIL) — the Run phase stays locked.",
    zh: "验证未通过(FAIL)— 运行阶段保持锁定。",
  },
  "run.gate.noVerdict": {
    en: "The build ended without a validation verdict — the Run phase stays locked.",
    zh: "构建结束但未产生验证裁决 — 运行阶段保持锁定。",
  },
  "run.gate.steerHint": {
    en: "Send a message below to re-run the build loop.",
    zh: "在下方发送引导可重跑构建循环。",
  },
  "run.force": { en: "Run anyway (at your own risk)", zh: "仍要运行(自担风险)" },
  // Human takeover (embedded browser: login / captcha / challenge)
  "takeover.title.login": { en: "Login required", zh: "需要登录" },
  "takeover.title.captcha": { en: "Captcha required", zh: "需要验证码" },
  "takeover.title.challenge": { en: "Verification required", zh: "需要人机验证" },
  "takeover.title.default": { en: "Manual action needed", zh: "需要人工操作" },
  "takeover.conn.idle": { en: "Preparing…", zh: "准备中…" },
  "takeover.conn.connecting": { en: "Connecting…", zh: "连接中…" },
  "takeover.conn.connected": { en: "Connected", zh: "已连接" },
  "takeover.conn.disconnected": { en: "Disconnected", zh: "已断开" },
  "takeover.conn.error": { en: "Connection failed", zh: "连接失败" },
  "takeover.enlarge": { en: "Enlarge", zh: "放大" },
  "takeover.shrink": { en: "Shrink", zh: "还原" },
  "takeover.done": { en: "Done", zh: "完成接管" },
  // Steerable box
  "steer.guide": { en: "Guide", zh: "引导" },
  "steer.sending": { en: "Sending…", zh: "发送中…" },
  "steer.sent": { en: "Sent · agent will incorporate", zh: "已发送 · Agent 将采纳" },
  "steer.failed": { en: "Failed to send", zh: "发送失败" },
  "steer.send": { en: "Send", zh: "发送" },
  // Task plan (discovery + scraper)
  "taskPlan.title": { en: "Task plan", zh: "任务计划" },
  "taskPlan.empty": { en: "(the agent hasn’t written this yet)", zh: "(Agent 还没写)" },
  "taskPlan.hint": {
    en: "Refine the goal or add a constraint; the agent adjusts on its next step.",
    zh: "调整目标或添加约束;Agent 会在下一步采纳。",
  },
  "taskPlan.finished": { en: "Run finished — steering is closed.", zh: "运行结束 — 引导已关闭。" },
  "taskPlan.rerunHint": {
    en: "Submit feedback — the agent re-runs discovery with it.",
    zh: "提交反馈,Agent 会带着它重跑发现。",
  },
  "taskPlan.rerunFooter": {
    en: "Run finished — your feedback will trigger a re-run.",
    zh: "运行已结束 — 提交反馈将触发带反馈的重跑。",
  },
  "scraperPlan.empty": { en: "(the crawler is forming its plan…)", zh: "(爬虫正在形成计划…)" },
  "scraperPlan.hint": {
    en: "Guide the crawler (fields to capture, pagination, anti-bot). Queued — applies next iteration.",
    zh: "引导爬虫(要抓的字段、翻页、反爬)。排队中 — 下一轮生效。",
  },
  // Conversation header
  "status.connecting": { en: "Connecting", zh: "连接中" },
  "status.live": { en: "Running", zh: "运行中" },
  "status.done": { en: "Done", zh: "完成" },
  "status.error": { en: "Error", zh: "错误" },
  "status.idle": { en: "Idle", zh: "空闲" },
  "header.candidates": { en: "candidates", zh: "候选" },
  "header.selected": { en: "selected", zh: "入选" },
  "header.buildAll": { en: "Build scrapers", zh: "构建抓取器" },
  "header.starting": { en: "Starting…", zh: "启动中…" },
  "header.newTask": { en: "New task", zh: "新任务" },
  // Transcript / common
  "transcript.connecting": { en: "Connecting…", zh: "连接中…" },
  "transcript.loadFailed": { en: "Failed to load this run.", zh: "加载此运行失败。" },
  "common.loading": { en: "Loading…", zh: "加载中…" },
  // Sidebar
  "sidebar.brand": { en: "Discovery", zh: "数据发现" },
  "sidebar.newTask": { en: "New task", zh: "新任务" },
  "sidebar.noTasks": { en: "No tasks yet", zh: "暂无任务" },
  // Sidebar item menu
  "menu.rename": { en: "Rename", zh: "重命名" },
  "menu.delete": { en: "Delete", zh: "删除" },
  "menu.deleteConfirm": {
    en: "Delete this and its data (run history + any scraper sub-sessions)? This can't be undone.",
    zh: "删除此项及其数据(运行记录 + 抓取器子会话)?不可撤销。",
  },
  // Home
  "home.title": { en: "Find the data you need", zh: "找到你需要的数据" },
  "home.desc": {
    en: "Describe your data need in natural language. The agent searches APIs, datasets, and web sources — and you can steer it as it works.",
    zh: "用自然语言描述你的数据需求。Agent 会搜索 API、数据集和网页源 — 你可以在过程中引导它。",
  },
  // Artifact panel
  "panel.resize": { en: "Drag to resize", zh: "拖动调整宽度" },
  // Theme toggle (label names the mode you'll switch TO)
  "theme.light": { en: "Light mode", zh: "白昼模式" },
  "theme.dark": { en: "Dark mode", zh: "黑夜模式" },
};

interface LocaleCtxValue {
  locale: Locale;
  setLocale: (l: Locale) => void;
  toggle: () => void;
  t: (key: string) => string;
}

const Ctx = createContext<LocaleCtxValue | null>(null);
const STORAGE_KEY = "locale";

export function LocaleProvider({ children }: { children: React.ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>("en");

  // Resolve the preferred locale on mount (localStorage → browser → en). Done in
  // an effect so the SSR/first-client render is deterministic ("en"), avoiding a
  // hydration mismatch.
  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "en" || saved === "zh") {
      setLocaleState(saved);
    } else if (typeof navigator !== "undefined" && navigator.language?.toLowerCase().startsWith("zh")) {
      setLocaleState("zh");
    }
  }, []);

  const setLocale = useCallback((l: Locale) => {
    setLocaleState(l);
    try {
      localStorage.setItem(STORAGE_KEY, l);
    } catch {
      /* ignore */
    }
  }, []);

  const toggle = useCallback(() => setLocale(locale === "en" ? "zh" : "en"), [locale, setLocale]);

  const t = useCallback(
    (key: string) => {
      const entry = DICT[key];
      if (!entry) return key;
      return entry[locale] ?? entry.en ?? key;
    },
    [locale],
  );

  const value = useMemo(() => ({ locale, setLocale, toggle, t }), [locale, setLocale, toggle, t]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useT(): LocaleCtxValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useT must be used within LocaleProvider");
  return v;
}
