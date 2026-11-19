# 每日早读报告（Daily News Brief）— 工作说明

> 本目录是「我的对外助手」（HiFox Agent，runtime=pi，agentId `<agent-id>`）的工作目录。
> 完整可执行 prompt 见同目录 [`daily-news-brief.prompt.md`](./daily-news-brief.prompt.md)（以该文件为准，本文件是给 agent 看的操作说明）。

## 1. 触发条件（严格）

**只有 owner 本人在 1:1 私聊里提出，才执行本流程。**

- 身份判定：当前会话消息里的发送者前缀 `[owner]`，或 `hifox chat message list <session-id> --output json` 里最后一条消息的发送者 = owner / `your-name` / open_id `ou_<open_id>`。
- 场景判定：必须是**私聊**（`hifox chat session view <session-id>` 的 `accessMode` 为 private，且会话里只有该发送者）。
- **群聊、其他同事的私聊、机器人消息、任务评论 → 一律不触发**，只在普通答复里说明「该流程只对 owner 本人开放」，不要采集、不要发清单、不要部署。
- 触发词（owner 本人私聊里出现即视为请求，措辞不拘）：`早读`、`早报`、`每日新闻`、`今日新闻`、`新闻早读`、`生成早读`、`daily news`。
- 静默规则：如果 owner 没有明确要「今天的」，而是随口说「早读」，默认按**今天**的窗口执行；他说「昨天的」就回溯一天。

## 2. 做什么（一句话）

扫描「前一天 08:00 → 今天 08:00」窗口内的 AI/技术/国际要闻 → 先发清单让 owner 选题 → 按选题深挖 → 生成图文并茂的富 HTML 报告 → 发布到 `blog.example.com/news/YYYY/MM/DD/` → 飞书回执给链接。

## 3. 关键参数

| 项 | 值 |
| --- | --- |
| 扫描窗口 | 运行日的前一天 08:00 → 运行日 08:00（Asia/Shanghai）。**以 `date "+%Y-%m-%d %H:%M %Z"` 为准**，不要用上下文里的日期 |
| 博客仓库 | `<blog-repo>`（Astro 5 → 阿里云 OSS） |
| 交付形态 | **默认 Markdown 正文直接返回**；发布分支才用 `public/news/YYYY/MM/DD/index.html` |
| 索引页 | `public/news/index.html`（新的一期插在最前） |
| 工作产物 | `<work-dir>/YYYYMMDD-daily-news-brief/`（`candidates.md` / `candidates.json` / `notes/<id>.md`） |
| 飞书收发 | `lark-cli im +messages-send --as bot --user-id ou_<open_id>`（**bot 身份**，否则对方回复收不到） |
| 跟踪仓库 | `agentclientprotocol/agent-client-protocol`、`earendil-works/pi`、`anomalyco/opencode`、`vectorize-io/hindsight`、`agegr/pi-web` |

## 4. 执行要点

1. **先清单、后深挖**：清单 8–15 条，每条含编号 / 分类 / 一句话标题 / ≤60 字摘要 / 量化热度信号 / 一手来源链接，发飞书等选题。
2. **等选题**：`lark-cli event consume im.message.receive_v1 --as bot --timeout 30m`；超时或失败就用 `ask_user_question` 在当前会话里问，绝不无限等待、不反复催。
3. **深挖**：每个话题 ≥2 个一手来源，结构为「是什么 → 为什么现在重要 → 对我的影响 → 风险与不确定点」。
4. **报告（默认文字版）**：一份完整 Markdown 正文 —— 结论先行 → 细节 → 关键数据（Markdown 表格）→ 对我的影响 → 风险 → 来源链接；每话题 ≥2 个一手来源；**不生成插画、不生成 HTML**。正文写进回执的「## 报告正文」段，并**自己分段发到飞书**（bot 身份，≤4500 字/条，带 i/N 标记）；router 只发确认与摘要，不转述正文。同时落 `report.md`。
   发布分支（仅明确要求时）：单文件 HTML、零 CDN、内联 SVG 图表、响应式 375px、不生成插画，部署后 `curl -sI` 验证。
5. **部署**：

   ```bash
   cd <blog-repo>
   npm run build
   npm run deploy          # 脚本已默认 --keep-prefix reports/，不会误删线上报告
   ```

   部署后 `curl -sI` 验证首页与 2–3 张图片 200。
6. **回执**：飞书发一条带 URL 的完成通知（话题数 / 图片数 / 来源数）。

## 5. 已知坑（2026-11-15 实战验证）

- **`npm run deploy` 曾经会删掉线上 7 篇报告**（源码不在仓库里）。现已修好：孤儿页面已回填为 `src/content/reports/2026-11-03…09.md`，且 `deploy-oss.mjs` 支持 `--keep-prefix`（默认保护 `reports/`）。改脚本时别把这个保护去掉。
- **窗口按运行日算**，不是按任务创建日；否则报告内容会比发布日早好几天。
- **star 增量是抓取日口径**（Trending 的「当日 +N」），不等于窗口内增量，正文必须写清口径。
- **BBC / Guardian / NYT / Reuters RSS、lmarena.ai 常抓不到**；DeepSeek / MiniMax / Qwen 博客是 JS 渲染 → 用 HN Algolia + 官方英文博客 + GitHub API 交叉验证，抓不到就写「未证实」。
- **付费墙**（如 The Economist）：只引标题 + 公开讨论，并注明「原文付费墙」。
- **不能编造**模型名、分数、营收、日期、star；所有数字要能指回原文。
- **不生成插画、不默认生成 HTML**：不要调用 `mmx` / 任何图片生成；默认交付 Markdown 正文，不碰博客仓库、不跑 build/deploy。
- 子 agent 可能没有 `web_search`/`fetch_content`，只能 `curl` + `jq` + `gh api`；端点清单见 prompt 的 §0.2。
- 本机 2026-11-08 起处于休眠状态，无法保证定时唤醒；本流程是**按需触发**，不是定时任务。

## 6. 日志

按「工作日志」格式规范写入 `logs/assistant-YYYY-MM-DD.md`（分节固定为 日期与主题 / 今日进展 / 结论与产出 / 待办与风险，条目用 `- 标签：内容`）：

- 日期与主题：日期写当天，主题写「每日早读报告」，会话写本次 chat session 与请求方。
- 今日进展：`- 任务：每日早读报告` 下一条 `- 进展：收到「早读」→ 采集 N 条 → 选题回复「…」→ 发布 <URL>`。
- 结论与产出：`- 结论：已发布 <URL>`、`- 产出：<work-dir>/YYYYMMDD-daily-news-brief/`。
- 待办与风险：无则写 `- 无`。
