# Worker 名册

**真源是 [`registry.json`](./registry.json)** —— 能力、只读/可写、并发上限、启停状态、
以及给 worker 的角色提示（`role_prompt`）都在那里。本文件只给人读。

派工前由 Task Center（`sa.py`）校验 registry：不在册 / 已停用 / 并发超限 → 直接拒绝派工。

| agent_id | 角色 | 只读 | 并发 | 状态 | 适合 |
| --- | --- | --- | --- | --- | --- |
| `explore` | 调查 | 是 | 2 | 启用 | 摸清现状、定位实现、梳理调用链、列清单 |
| `plan` | 方案 | 是 | 1 | 启用 | 怎么改、设计实现、根因分析、对比方案 |
| `general-purpose` | 实现 | 否 | 2 | 启用 | 实现功能、修 bug、跑数据、跑测试、写文档 |
| `research` | 调研 | 是 | 1 | **停用** | 查行业资料、核实外部事实 |
| `daily-news` | 早读 | 否 | 1 | 启用 | 每日早读报告（**仅 owner 私聊触发**，见 registry 的 `trigger_gate`） |
| `team-support` | 团队常规支持 | 否 | 2 | 启用 | 团队内常规支持：BPM 审批处理、GitLab MR 合入/评论（群内可触发；T1 自动执行 / T2 待人工决定 / T3 只列清单） |

`research` 停用原因：pi CLI 未配置联网检索工具，派出去等于没有角色约束的默认 worker。
配好工具后在 registry 里把 `enabled` 改回 true 即可。

`team-support` 是**分组自主**的 worker：常规动作（常规审批、年假/考勤类审批、CI 通过无冲突的 MR 合入）
直接执行；高危动作（资金流水、生产 DDL、删数据、CI 红/冲突 MR）只查清事实、交人工决定；
只列清单的请求全程只读。规格在 [`workers/team-support/ROLE.md`](./workers/team-support/ROLE.md)。

`daily-news` 带 `trigger_gate`（只允许 owner 本人私聊触发）：闸门由 **Task Center 校验**
（`core.validate_decision` 读 registry 的 `trigger_gate`，核对 Decision 的 `items[].requester`），
不靠提示词自觉。worker 规格在 [`workers/daily-news/ROLE.md`](./workers/daily-news/ROLE.md)。

要加新 worker：在 `registry.json` 加一条，然后重启 router 与 worker runner。
**不要**在代码里另存一份名单（之前 `workers.md` 与 `worker_runner.py` 的 `ROLE_HINT`
两份真源已经漂移过一次）。
