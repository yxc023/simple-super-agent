# super-agent — 指令调度层

一个**只接指令、不亲自干活**的调度 agent。

它接收人的指令，把「指令 + 上下文」拆成可独立执行的指派（assignment），
交给其他 worker agent 去执行，然后只负责收回执、汇总、转述。

> 边界：super agent **不**做业务执行、不替 worker 下结论、不改业务代码。
> 它只做四件事：建任务 → 派指派 → 收回执 → 汇总。

## 目录布局

```
super-agent/
├── README.md              ← 本文件：协议说明
├── sa.py                  ← 协议工具（建任务 / 派指派 / 收回执 / 查看）
├── templates/
│   ├── INSTRUCTION.md     ← 指派单模板（派给 worker 的东西）
│   └── CONTEXT.md         ← 上下文包模板
├── registry.json          ← Worker Registry（唯一真源：能力/只读/并发/启停）
├── core.py                ← Task Center 规则层（Registry 校验、Decision 校验、指令单渲染、outbox、完成策略）
├── outbox/                ← 外发消息队列（pending → dispatched → succeeded|failed → dead_letter）
├── serve.py               ← 看板服务（只绑 127.0.0.1）
├── seed_demo.py           ← 灌演示数据（--clear 清空）
├── board.sh               ← 一键启动看板 + 开浏览器
├── lark_bridge.py         ← 飞书 → Task 桥接（pull / event / reply / status）
├── lark_bridge.sh         ← 桥接守护进程 start/stop/status/logs
├── lark_config.json       ← 飞书桥接配置（会话、白名单、触发规则）
├── lark_state.json        ← 桥接状态（已读消息去重）
├── web/index.html         ← 看板页面（单文件，无外部依赖）
├── tasks/
│   └── T001/
│       ├── task.md        ← 任务定义（目标、上下文、验收标准）
│       ├── state.json     ← 机器可读状态（唯一事实源）
│       ├── context/       ← 上下文文件副本（worker 只读这里）
│       └── notes/         ← super agent 的过程笔记
└── channels/
    └── T001/
        ├── instructions/  ← 指令渠道：每次指派一份 A01.md
        ├── responses/     ← 回执渠道：worker 写回 A01.md
        └── notes/         ← 人工干预记录：A01.md / TASK.md
```

任务目录下还有 `events.jsonl`（事件流，看板时间线的数据源）。

## 核心概念

| 概念 | 含义 | 载体 |
| --- | --- | --- |
| **Task（任务）** | 一次完整的指令受理，含目标、上下文、验收标准 | `tasks/T001/task.md` + `state.json` |
| **Assignment（指派）** | 一次「把活派给某个 worker」的动作 | `channels/T001/instructions/A01.md` |
| **Channel（渠道）** | 指令下行 + 回执上行的两条文件通道 | `instructions/` 与 `responses/` |
| **Context（上下文）** | worker 执行所需的背景材料 | `tasks/T001/context/` |

一次 Task 可以有多个 Assignment；同一个 Task 下的多个 Assignment 可以并行跑。

## 状态机

Task 状态：

```
open ──assign──> dispatched ──任一指派在跑──> running ──全部终态──> completed
```

Assignment 状态：

```
assigned ──worker 回执──> done ──人工放行──> approved
   │                       │
   │                       └──人工打回──> rework ──重新派发──> assigned
   ├──人工改派──> assigned（换 worker）
   └──人工取消──> cancelled
worker 失败 ──> failed ；worker 卡住 ──> blocked
```

`approved / failed / cancelled` 是终态。任一 Assignment 处于 `assigned` 时，Task 处于 `dispatched` 或 `running`；
出现 `blocked` / `rework` / 未销账的人工批注时，Task 处于 `needs_human`。

## 协议（命令）

工具入口：`python3 super-agent/sa.py <cmd>`（相对本仓库根目录执行）。

### 1. 建任务

```bash
python3 super-agent/sa.py new-task \
  --title "整理 Q3 商户平台需求清单" \
  --goal "从需求仓拉出 Q3 全部需求，按状态/负责人归类，输出 md 表格" \
  --context "需求仓地址：https://…（补充背景）" \
  --context-file /path/to/背景材料.md \
  --acceptance "表格含 编号/标题/状态/负责人/来源链接 四列，按状态分组"
```

产出 `tasks/T001/`（task.md、state.json、context/ 副本）与 `channels/T001/` 空目录。

### 2. 派指派（写指令单 → assign）

先按 `templates/INSTRUCTION.md` 写一份指令单（可以是临时文件），
再让工具把它落到指令渠道并登记状态：

```bash
python3 super-agent/sa.py assign \
  --task T001 \
  --worker explorer \
  --instruction-file /tmp/A01.md \
  --brief "先摸清需求仓数据结构"
```

工具会：
- 复制指令单到 `channels/T001/instructions/A01.md`
- 建回执骨架 `channels/T001/responses/A01.md`
- 在 `state.json` 里登记 `A01`，状态置 `assigned`

### 3. 收回执

worker 干完后（自己写回执文件，或由 super agent 代写）：

```bash
python3 super-agent/sa.py respond \
  --assignment A01 \
  --status done \
  --summary "产出 /tmp/需求清单.md，共 42 条" \
  --artifact /tmp/需求清单.md
```

### 4. 查看

```bash
python3 super-agent/sa.py list          # 所有 task + assignment 状态
python3 super-agent/sa.py show --task T001
```

## Worker 回执约定

worker 必须把回执写进 `channels/T001/responses/A01.md`，至少包含：

- `status`：`done` / `failed` / `blocked`
- **结论**：一句话说清做了什么、结果是什么（不是过程复述）
- **产出**：可访问的文件绝对路径清单（这是 super agent 汇总时唯一能引用的东西）
- 如果 blocked：写明「卡在什么、需要谁给什么」

**同义约束（产出与回执的对外粒度）**：回执与产出文件里不要出现同事姓名/工号/uid、
他人或群的消息内容、内部接口路径与内部域名、内部工单号/计划号/审批 id、内部仓库名与库表名、
SQL 片段。需要细节就写进产出文件（细节本身是交付物），由用户自己打开；必要时用占位符
（如 `<同事A>`、`<内部工单>`）代替。回执里**结论 + 产出路径**即可，不要复述明细。

只回一句话、不落文件的回执视为**未完成**——super agent 无法据此汇总。

## Worker 侧怎么接

worker（Pi subagent）只需要知道两件事：

1. **指派单路径** —— 里面是目标、上下文（绝对路径）、要求、边界、回执格式。
2. **回执路径** —— 把结果写在这里。

典型 prompt：

```
你现在是 general-purpose worker。

请先完整读取指派单：<abs>/channels/T001/instructions/A01.md
严格按指派单里的「具体要求」「边界」执行，只做这一件事。

完成后把回执写到：<abs>/channels/T001/responses/A01.md
回执格式见指派单末尾「回执要求」（status / 结论 / 产出绝对路径）。
```

worker 不需要知道 `sa.py` 的存在：它只写回执文件，登记由 super agent 侧做。
（`sa.py respond` 接受 `done / failed / blocked`，并把 `completed / complete / success`
归一到 `done`、`error` 归一到 `failed`；worker 写什么词都能被正确登记。）

## 看板（可视化 + 人工干预）

```bash
cd <project-root>
python3 super-agent/serve.py --port 8788      # 只绑 127.0.0.1
# 打开 http://127.0.0.1:8788
```

### 演示数据

看板第一次打开是空的。用 `seed_demo.py` 灌一套完整 case（含派发、回执、人工批注、打回、阻塞、
放行、未派发任务共 4 个）：

```bash
python3 super-agent/seed_demo.py            # 灌入
python3 super-agent/seed_demo.py --clear    # 清空全部任务
```

演示数据走的是与真实运行**完全相同的协议**（同样的 `state.json` / `channels/` 文件），
所以页面上的每一个按钮在演示数据上都能真实生效。

### 页面能力

看板读的就是上面那套文件，没有第二份状态。页面能力：

| 区域 | 内容 |
| --- | --- |
| 顶部 KPI | 任务总数 / 执行中指派 / 待人工确认 / 阻塞待重做 / 待 agent 回应 / 每个 worker 在跑数 |
| **运行状态** | 四个守护进程存活状态 + pid、**Router Session**（模型 / 上下文占用 / 消息数）、事件数 vs 决策数、待处理事件数、最近模型错误 |
| **飞书消息收件箱** | 每条收到的消息：时间 / 发送人 / 内容 / 处理结果（→ T00X 或 ignored / baseline） |
| **Router 决策流** | 每个事件唤起 router 后的判断原文（可据此核对信息传递是否符合预期） |
| 左侧任务栏 | 状态筛选（全部/待派发/已派发/进行中/待人工/已完成）、搜索、指派缩略 |
| 任务详情 | 目标、验收标准、上下文文件（可点开）、创建/更新时间、人工留言 |
| 指派与派发 | 每个指派一张卡：状态、worker、指令单全文、回执全文、人工干预记录，**并排对照** |
| 时间线 | 本任务事件流 + 全局事件流（最近 40 条，跨任务） |
| 人工干预 | 批注 / 放行 / 打回重做 / 改派 / 取消 / 手动派单 / 新建任务 / 任务留言 |

干预动作与 CLI 完全等价（都写同一批文件）：

| 页面动作 | CLI 等价命令 | 效果 |
| --- | --- | --- |
| ✍ 批注 | `sa.py comment --assignment A01 --text "…"` | 追加要求到指令单尾部 + 记 `notes/A01.md` + 置 `pending_ack`（agent 必须回应） |
| ✓ 放行 | `sa.py approve --assignment A01` | 状态 `done → approved` |
| ↺ 打回重做 | `sa.py reject --assignment A01 --reason "…"` | 状态 `done → rework`，意见追加到指令单，worker 重做时能看到 |
| ⇄ 改派 | `sa.py redirect --assignment A01 --to explore` | 换 worker，状态回到 `assigned` |
| ✕ 取消 | `sa.py cancel --assignment A01 --reason "…"` | 状态 `cancelled` |
| 任务留言 | `sa.py note --task T001 --text "…"` | 任务级留言，agent 下一轮看到 |

**agent 侧的闭环**：看板上的「待 agent 回应」由 `pending_ack` 驱动。agent 处理完人工意见后执行
`sa.py ack --assignment A01` 销账，看板上的角标和任务状态才回到正常。

> 看板服务只绑定 `127.0.0.1`，不对外暴露；所有写操作限制在 `super-agent/` 目录内。

## 飞书对接（消息 → 工作指令）

飞书机器人 **「飞书助手」**（app `cli_<app_id>`）在群
**「飞书助手群」**（`oc_<chat_id>`）里 @机器人发消息，
即视为一条工作指令。

```
飞书群 @机器人
   │
   ▼  lark_bridge 拉取（去重 / 白名单 / 触发规则）
Task（source=feishu，带 message_id）  ──►  主 agent 从 sa.py list 认领
   │
   ▼  lark_bridge.py reply
飞书群里回结果
```

### 为什么是拉取而不是事件长连接

本机该 app 的**事件总线已被 HiFox 运行时占用**，实测：

```
another event bus is already connected to this app (1 remote event connection(s) detected via API);
only one bus should run globally to avoid duplicate event delivery
```

所以默认走 `lark-cli im +chat-messages-list` 轮询（user 身份读会话消息），不抢总线。
`lark_bridge.py event` 也可用：它会先尝试事件模式，遇到占用**自动降级为 pull**。

> ⚠️ **私聊（1:1）必须显式写进 `lark_config.json` 的 `chats`**：
> 轮询模式只拉「配置里的 chats + 动态发现的群」，而 `chat_discovery.types=["group"]`
> （`im +chat-list --as bot` 也拿不到 p2p，p2p 只能用 user 身份查）。
> 所以私聊会话 `oc_<chat_id>` 写成
> `{"chat_id": "...", "name": "飞书助手（私聊）", "mode": "p2p"}`。
> 漏了这一条的症状：**事件长连接断掉降级成 pull 之后，私聊消息全部石沉大海**
> （群消息照常，私聊没人理）—— 早读这种「只在本人私聊触发」的 worker 也就彻底哑火。
> 事件模式则不受影响（长连接本来就能收到 p2p 事件）。

### 启动

```bash
./super-agent/lark_bridge.sh start     # 常驻轮询（默认 20s 一次）
./super-agent/lark_bridge.sh status    # 状态 + 已受理任务
./super-agent/lark_bridge.sh logs      # 日志
./super-agent/lark_bridge.sh stop
```

首次运行会**建立基线**：把历史消息标记为已读但不建任务，避免把旧消息当新指令。

### 触发规则（`lark_config.json`）

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `chats` | 助手群 + 助手私聊 | 监听哪些会话（`mode` = `group` / `p2p`；p2p 必须显式列，轮询模式发现不到） |
| `senders` | owner | 白名单 open_id |
| `group_requires_mention` | `true` | 群里必须 @机器人才算指令 |
| `prefix_always_triggers` | `!` `派单` `任务` | 这些前缀即使不 @ 也触发 |
| `ack.on_receive` | `true` | 受理后自动回群「已受理为任务 T00X」 |
| `poll.interval_seconds` | `20` | 轮询间隔 |

### 消息不会被重放（两层去重）

| 层 | 机制 | 作用 |
| --- | --- | --- |
| 工作状态 | `lark_state.json` 的 `seen` | 日常去重 |
| **永久账本** | `inbox/handled.jsonl`（append-only） | 即使 `seen` 被清掉，历史消息也**永不**被当成新指令 |

另外 `sa.py new-task` 按 `message_id` 幂等 + 目标文本去重，`sa.py` 任务 id **永不复用**
（`tasks/.counter`）—— 避免旧回执串到新任务。

> 清账本（会让历史消息重新受理一次）：`python3 super-agent/lark_bridge.py reset-ledger`

### 全链路日志（一条消息从进来到回话）

按 `message_id`（`om_xxx`）、triggerId（12 位 hex）或 task id 查：

```bash
python3 super-agent/trace.py om_x100b60d2d2d6f8a8c0b2d45ddb35ab8
python3 super-agent/trace.py T764
```

会把下面这些文件按时间轴拼出来：

| 文件 | 内容 |
| --- | --- |
| `logs/lark-bridge.log` | 桥接过程：收到 / 投 router / 表情（带 ms 耗时）/ outbox 投递 / 拉取失败 |
| `logs/reactions.jsonl` | 表情时间线（append-only，带 `elapsed_ms`） |
| `inbox/messages.jsonl`、`inbox/handled.jsonl` | 消息元信息 + 永久账本（去重凭据） |
| `router/inbox.jsonl`、`router/outbox.jsonl`、`router/decisions/<id>.json` | 投给 router 的事件、router 的判断与回话、决策原文 |
| `tasks/T*/events.jsonl` | 任务时间线（带 actor 归因：router / worker-runner / worker / outbox） |
| `channels/T*/instructions|responses/*` | 派工指令与 worker 回执 |
| `outbox/ob_*.json` | 外发记录（凭据 message_id） |
| `~/.pi/agent/sessions/**` | worker / router 的**原始会话**（模型思考与工具调用全文） |

看板 `python3 super-agent/serve.py --port 8788` 里也有同一条事件时间线（可视化版）。

> 表情计时：`react()` 会把 list / create / delete 每一步的耗时写进桥接日志与
> `logs/reactions.jsonl`。表情替换的顺序是**先加新的、再删旧的** —— 反过来会在两次
> API 调用之间出现「消息上没有任何表情」的空档，用户看着像卡住。

### 消息链路视图（看板主视图：以 message_id 为主键）

看板顶部新增「**消息链路**」面板：**不再按文件分栏**（收件箱 / Router 决策流 / Decision /
Outbox 各自一摊），而是把**一条消息**的完整链路串成一条时间线：

```
📥 飞书收到消息 → 😀 表情（⌨️/💭/✅，带耗时）→ 🧭 投给 router → 📋 router 判断（叙述）
   → 📋 Decision（结构化）→ 🧠 router session 命中 → 🗂 任务 → 👷 指派 → ⚙️ worker 回执/放行
   → 📤 回话（outbox 凭据）
```

- 左侧列表 = 最近的消息；点一条 → 右侧展开它的链路；每段有「原文」按钮（按需拉取）。
- 来自**共享任务**（不一定由这条消息触发）的阶段带「任务侧」标记，避免误读。
- 原来那四个分文件面板收进一个可折叠的 `<details>`（默认收起），需要时再展开。
- 后端：`super-agent/chain_view.py`（拼接 + 索引 + 3s TTL 缓存）；原文接口
  `GET /api/chain-raw?message_id=om_xxx&index=N`（不进轮询 payload）。
- 命令行等价物：`python3 super-agent/chain_view.py om_xxx`（打印某条消息的链路）。

**难点与处理方式**（真做下来遇到的）：

| 难点 | 怎么处理 |
| --- | --- |
| 没有单一主键：session 用 triggerId、task 用 Txxx、outbox 用 ob_xxx、worker 用 session 文件名 | 自建连接表 `message_id → triggerId → task → assignment → session 文件` |
| 一条消息 ≠ 一个任务（同一任务会被后续消息继续推进） | 阶段带 `scope`：message / task；界面上标「任务侧」 |
| router session 里**没有** triggerId 字段 | 全文搜 triggerId 命中行号 + 行时间戳（router 每轮都会带上） |
| 原文很大（state.json / decision / outbox 全文） | 列表只给摘要，原文按需走 `/api/chain-raw`（/api/state 只多 93KB） |
| 时间戳三种格式（毫秒 epoch / ISO / `YYYY-MM-DD HH:MM`） | 统一归一化成 ISO 再排序，否则跨来源会错序 |
| 目录持续变大（tasks 72 个、outbox 228 个文件） | 只索引 + TTL 缓存；jsonl **增量读**（记住偏移量、只解析新增行），单文件 json 按 (mtime,size) 缓存 |
| 历史消息缺环节（表情台账是今天才加的） | 缺段就不显示，不伪造 |

**实测（当前数据规模：72 任务 / 228 outbox / 488 个 pi session）**：

| | 耗时 |
| --- | --- |
| 冷启动（重建索引） | 65 ms |
| 看板每 3s 一次的热刷新 | **~20 ms**（且与历史总量无关，只与新增量成正比） |
| 合成 10 万行 / 20MB 账本：整读 | 204 ms（随历史线性增长） |
| 合成 10 万行 / 20MB 账本：增量读 | 无新增 0.0 ms / 新增 1 行 0.1 ms |

> append-only 账本 + 增量读 + 内存索引 = 代价与**新增量**成正比、与历史总量无关。
> 顺序文件做关联查询真正贵的地方不是「join」，而是「每次重新扫全量」。
> 仍会线性增长的两处：`tasks/` `outbox/` 目录的 stat 遍历（当前 13ms）、
> pi session 目录的 rglob（已 30s 缓存，但随 pi 历史增长）。

> 更彻底的做法（未做）：在桥接 / router / worker **落盘时**就写一条链账本
> （`chains/chain-<message_id>.jsonl`，每阶段 append 一行），看板直接读它，
> 不用每次现拼。代价是要改 4 个写入点；收益是历史链路也能回溯、看板更快。

### 命令

```bash
python3 super-agent/lark_bridge.py pull --once          # 拉一次
python3 super-agent/lark_bridge.py pull --follow        # 常驻
python3 super-agent/lark_bridge.py pull --once --dry-run # 只看会命中哪些，不落库
python3 super-agent/lark_bridge.py event                # 事件模式（被占用则自动降级）
python3 super-agent/lark_bridge.py reply --task T001 --text "结果…"
python3 super-agent/lark_bridge.py status
```

### 任务里会存什么

`source=feishu`，`source_ref` 带 `chat_id / message_id / sender / sent_at`，
`context/01-*.md` 存飞书原文与元信息。回结果就靠 `message_id`：

```bash
python3 super-agent/lark_bridge.py reply --task T001 --text "结论 + 产出路径"
```

看板上也能点「↩ 回结果到飞书」。

### 与 HiFox 会话的重复处理

同一条飞书消息，**桥接**和 **HiFox 会话**两边都可能接到。所以 agent 侧规则是：
收到疑似飞书的指令时，先 `sa.py find --match "<开头 20 字>"` 查有没有同源 Task，
**有就接着那个 Task 做，不要再建一个**。

## 用户消息：先判断，再动手

飞书用户说的**不一定是新目标**。消息不再直接建任务，而是作为 `user.message` 事件交给
Router 判断：

```
飞书消息 → user.message 事件 → Router 判断 → Decision
                                          ├─ create   → 新建任务并派工
                                          └─ continue → 沿用已有任务
                                                        + agentId  派给谁
                                                        + sessionFrom 沿用哪个指派的 session
```

Router 用 `sa.py tasks-list --limit 15` 列候选任务（每项含 title / status / 指派 / session），
据此判断「全新目标」还是「延续之前的任务」。

**沿用时的 session 语义**：每个指派带一个 `worker_session_id`。Decision 里填
`sessionFrom: "A01"` 就会**继承 A01 的 worker session** —— worker 用
`pi --print --session-id <sid>` 复现同一条会话，带着上一轮上下文继续做。
不填则开一个干净的新会话。

实测：连发两条消息（「写 /tmp/e2e-new.md」→「接着刚才那个，再加一行」），
Router 判定延续 → 同一任务 T058 下新增 A02，A02 继承 A01 的 session id，
产出文件由 1 行变 2 行，worker session 文件里确实是同一会话的 2 个用户轮。

### Decision Schema v2

| 字段 | 语义 |
| --- | --- |
| `taskAction` | `create` = 新任务（`taskId` 必须为空）；`continue` = 沿用已有任务（`taskId` 必填） |
| `sessionFrom` | 沿用哪个指派的 worker session（必须同 agent） |
| `title` | 新建任务时的标题（不填取 goal 首行） |

`submit-decision` 的 `--task` 现在**可省略**：用户消息轮没有既有任务，
由 Decision 自己决定新建还是沿用（省略时 Task Center 从 `router/round.json` 取本轮上下文）。

## Router（常驻主 agent session）

### 契约在 system prompt 里，事件消息只留定位信息

操作契约写在 [`skill/router-system.md`](./skill/router-system.md)，启动 pi 时用
`--append-system-prompt` 注入 system prompt。所以每次唤醒 router 收到的消息只有：

```
[事件] task.created
triggerId: 0610e27dc5a9
任务: T040
目录: /abs/path/super-agent/tasks/T040
```

用户消息则是：

```
[事件] user.message
triggerId: 3810e9f3baaa
同意 今天同事A提的都同意
```

**不传上下文**：规格要求（角色边界、Decision 格式、registry、事件→动作表、硬规则）在
system prompt 里，历史与用户主动 @ 的那些消息在 session 里。事件消息实测 **55–89 字**
（改之前是 1400–1800 字，因为每条都重发一遍「最近几轮」摘要和判定指引）。

`router/digest.md` 仍然逐轮落盘，但**只作记录**（看板/排障用），不再注入 prompt。
手动 `rotate` 之后 session 会丢一段上下文，router 用 `sa.py tasks-list` 自行重建现状。

### 派发事件不再唤醒 router

`assignment.dispatched` 是 router 自己的动作 —— 投递它只会让 router 被自己的回声唤醒，
产生「本轮无动作」的空转。所以它**只记任务时间线，不进 router inbox**。

router 会被唤醒的事件：`task.created` / `assignment.completed` / `assignment.failed` /
`human.intervention` / `task.note`。

```
super-agent/router.py start|stop|status|run|tail
```

- **一个常驻 pi 进程 = 一个常驻 session**（`--session-id super-agent-router`），
  所有飞书指令、worker 回执、人工干预都进这**同一个 session**，所以 router 对上下文有记忆。
- 事件通过 `router/inbox.jsonl` 投递，router tail 它；处理结果写入 `router/outbox.jsonl`。
- **prompt 只带事件类型 + id + 文件路径，不重复携带上下文** —— 上下文在 session 里。

### 模型回退

主渠道额度耗尽（429）时自动换下一个模型：

```
minimax-cn/MiniMax-M3.1-Flash → bella/Deepseek-V4.1-Flash → bella/MiniMax-M3.1-Flash
```

worker 侧有同样的回退链（`SA_WORKER_MODEL` 可指定首选）。

### 反馈回路（router 决策流为什么会一直涨）

router 做完决定后要记账（`approve` / `ack`）。如果这些记账动作也去唤醒 router，
就会变成 `回执 → approve → 唤醒 → ack → 唤醒 → …` 的无限自问自答，
看板上的「Router 决策流」就会一直冒新数据。

现在的规则：

| 动作 | actor | 是否唤醒 router |
| --- | --- | --- |
| 看板上的批注 / 打回 / 取消 | `human` | ✅ 唤醒 |
| 任务留言 | `human` | ✅ 唤醒 |
| router 自己的 `approve` / `ack` | `router` | ❌ 不唤醒 |
| worker 自身的动作 | `worker` | ❌ 不唤醒 |

actor 的判定是**机制性的**，不靠模型自觉：
- `sa.py` 的 `--actor` 默认留空 → 按进程身份 `SA_ACTOR` 推断（router 进程 = router，人在终端/看板跑 = human）；
- `ack` 永远不投递（它是闭环的最后一步）；
- 非人工动作只写任务事件流，不进 router inbox。

### 事件去重

每个事件带唯一 `id`，router 把处理过的 id 记在 `router/processed.json`，
同一个事件不会被处理两次 —— 所以**清空 `router/inbox.jsonl` 也不会引发重放**。

### 护栏（不依赖模型自觉）

| 护栏 | 效果 |
| --- | --- |
| `SA_ACTOR=router` 时 `sa.py new-task` 直接拒绝 | 常驻 session 里的旧指令不会被重建成新任务 |
| `sa.py assign` 同任务同摘要已在跑 → 拒绝 | 不会重复派同一个活 |
| worker 非 0 退出 / 429 → 登记 `failed`（不是 `done`） | 失败不会伪装成完成 |
| 任务 id 永不复用 | 旧回执不会串到新任务 |

### 命令

```bash
python3 super-agent/router.py start|stop|status|tail -n 20
```

## Task Center：Router 只产出 Decision，不写状态

这是从《商户助手 Super Agent 调度框架技术实现方案》借来的核心结构。

```
Router（常驻 session，只做语义判断）
   │  submit-decision --task T001 --file dec.json
   ▼
Task Center（sa.py = 唯一写入口）
   ├─ 校验：Schema / triggerId / taskId 存在 / agentId 在册且启用
   ├─ items   → 建 Assignment（指令单由 core.render_instruction 统一渲染）
   ├─ reviews → 确认(approve，需产出证据) / 打回(reject，意见追加到指派单)
   └─ outbox  → 写外发消息队列（投递器负责发回渠道）
```

### Decision 结构

```json
{
  "schemaVersion": 1,
  "triggerId": "<本轮 triggerId>",
  "items": [
    {"triggerId": "…", "taskAction": "create|continue", "taskId": "",
     "agentId": "general-purpose", "goal": "…", "requirements": ["…"],
     "acceptance": ["…"], "confidence": 0.9, "reason": "…", "rationale": "…"}
  ],
  "reviews": [{"assignment": "A01", "verdict": "approve|reject", "note": "…"}],
  "outbox": [{"target": "thread|actor", "purpose": "ack|reply|question|result|notify", "text": "…"}]
}
```

规则：

- **逐条独立生效**：单条被拒不回滚其它条目（`rejected` 里带 `reason_code`）。
- **整体 Schema 非法 → 整批不执行**。
- `approve` **必须有产出证据**（artifact 绝对路径），否则 `no_evidence` 拒绝。
- **同一 triggerId 重复提交幂等**：reviews 不重复生效。
- **Decision 只能落在 router 本轮被唤醒的那个任务上** —— `router/round.json` 记录本轮
  task/triggerId，跨任务提交会被拒（防常驻 session 的旧上下文串味）。

```bash
python3 super-agent/sa.py submit-decision --task T001 --file /tmp/dec.json --triggers <id>
```

### Worker Registry

`registry.json` 是 worker 名单的**唯一真源**（不再有 `workers.md` 与代码里的第二份）：

```json
{"agent_id": "explore", "enabled": true, "readonly": true,
 "capabilities": ["read","search"], "max_parallel": 2, "role_prompt": "…"}
```

派工前 `sa.py` 校验：在册？启用？只读角色是否匹配？并发未超？任一不满足直接拒绝。

#### 触发闸门（`trigger_gate`）

某个 worker 声明了 `trigger_gate` 时，「谁能派它」由**机制**管，不靠提示词自觉：

```json
{"agent_id": "daily-news", "trigger_gate": {
   "senders": ["ou_<open_id>"], "require_p2p": true}}
```

- Decision 的 item 必须带 `requester: {openId, chatType, name}`（router 从本轮事件的
  `source_ref` 取：`sender` / `chat_type` / `sender_name`）。
- `core.validate_decision` 核对：发送者在不在 `senders` 里、渠道是不是要求的形态。
  不满足 → 该 item 被拒（`gate_sender_denied` / `gate_channel_denied` /
  `gate_missing_requester`），其余条目照常生效。
- 用途：**每日早读**（`daily-news`）只在owner 本人私聊里触发；群里、别人代问一律不派工。
  规格见 [`workers/daily-news/ROLE.md`](./workers/daily-news/ROLE.md)。

### 完成策略（B5）

Task 是否完成由**目标 + 完成证据**决定，不由指派是否结束决定：

```json
"completion_policy": {"require_approval": true, "evidence_required": true}
```

- 指派仍 `assigned/running` → `dispatched`
- 指派 `blocked` / `waiting_user_input` / `rework` / `failed` → `needs_human`
- 指派 `done` 未确认 → `needs_human`（等放行）
- 指派 `approved` 但缺 artifact → `needs_human`
- 全部满足 → `completed`

### worker 反问用户（waiting_user_input）

worker 缺信息时回 `status: waiting_user_input` + 具体问题 → 任务转 `needs_human`，
router 用 `outbox: purpose=question` 把问题问出去，用户回答后由新事件唤醒继续。

## Worker session 复用

每个指派带 `worker_session_id`（`w-<任务>-<指派>-<agent>-<随机>`）。worker_runner 用
`pi --print --session-id <sid>` 跑，因此：

- 同一指派重做 → 复现同一会话；
- `sessionFrom` 继承 → 新指派复现源指派的会话。

worker 进程崩溃会让指派卡在 `running`（pending 只看 `assigned`）—— runner 启动时
`reclaim_stale()` 把僵死指派收回重派，`run_one_guarded` 保证异常被登记成 `failed`。

## Router 的两条护栏

### ① 工具护栏：让它**没有**调查能力

`super-agent/router_gate.ts`（pi extension，router 启动时 `--extension` 加载）拦截工具调用：

| 工具 | 放行 | 拦截 |
| --- | --- | --- |
| `bash` | 只有 `python3 .../super-agent/sa.py <子命令>` | 其余一切命令 |
| `read` | 任务/上下文/回执目录、`/tmp` 产出物 | 框架源码与业务代码 |
| `write` | `/tmp/` 下的 Decision 草稿 | 其它路径 |
| 其它（grep/find/ls…） | 无 | 全拦 |

router 启动参数同时收紧到 `--tools read,write,bash`。

**为什么**：靠提示词说「你是调度者」不够 —— 长上下文会把它冲淡。改用**机制**：
收到「查一下我有没有待审批」这种请求时，router **根本没有能力自己查**，
唯一的正确动作就是派 worker。

实测：router 想跑 `ls /tmp` 被拦后，自动建任务 T088 派给 worker 去做。

### ② 单一长驻 session + 手动轮转

router 用**一个统一的长驻 session**（`super-agent-router`），所有指令、回执、人工干预
都进这一个会话 —— 上下文连续，不需要重新交代背景。

代价是 session 会越滚越大。处理方式：

- **不自动轮转**（自动轮转会切断上下文，用户明确要求统一会话）。
- **手动轮转**：`python3 super-agent/router.py rotate` —— 归档当前 session 文件、
  重启 router 开一个干净的；跨轮次连续性由 `super-agent/router/digest.md` 承接
  （每轮追加一行摘要，同时作为「最近状态」锚点随事件一起给 router）。
- 每轮事件消息里带最近 8 行摘要，帮助在长上下文里定位「刚才那个」指什么。

「别去处理历史遗留任务」靠**工具护栏 + 契约**约束，不靠切断上下文。

> 注意：`--append-system-prompt` 与 `--extension` 都是**启动时**生效的 ——
> 改完契约或护栏必须**重启 router 进程**，否则老进程还在用旧的。

## 切换 Router 的模型

模型状态放在 `super-agent/router/model.json`（文件驱动），
**运行中的 router 下一轮就会跟着切，不用重启、不丢 session**。

```bash
python3 super-agent/router.py model --status              # 看当前/主模型/降级状态
python3 super-agent/router.py model <provider/model>      # 切到指定模型
python3 super-agent/router.py model --restore             # 切回启动默认模型
```

三个模型来源，优先级从高到低：

| 来源 | 怎么来的 | 生效时机 |
| --- | --- | --- |
| `model.json` 的 `primary` | 人工执行 `router.py model ...` | **下一轮立刻** |
| `SA_ROUTER_MODEL` 环境变量 | 启动时的默认（写进 `boot_primary`） | 进程启动 |
| `FALLBACK_MODELS` | 429/额度耗尽时**自动降级** | 当轮 |

### 降级后怎么切回来

降级链：`minimax-cn/MiniMax-M3.1-Flash → bella/Deepseek-V4.1-Flash → bella/MiniMax-M3.1-Flash`
（`FALLBACK_MODELS`）。

降级时会把状态写进 `model.json`：`degraded: true`、`degraded_to`、`degraded_at`、
`degraded_reason`。`router.py status` 和看板都能看到「现在跑在降级模型上」。

切回有两条路：

1. **自动切回**（默认）：降级后过 `restore_after_seconds`（默认 300 秒，可用
   `SA_ROUTER_RESTORE_AFTER` 调），下一轮自动切回 `primary`。切回后如果主模型又 429，
   会再次降级 —— 不会死循环。
2. **手动切回**：`router.py model --restore`（回 `boot_primary`）或
   `router.py model <provider/model>`（指定任意模型）。下一轮生效。

想关掉自动切回，把 `model.json` 的 `restore_after_seconds` 设成很大即可。

## 看板的可读性设计

看板是排查「现在到底发生了什么」的地方，所以有几条硬规则：

| 规则 | 做法 |
|---|---|
| **最新在上** | 任务列表按 `updated_at` 倒序，不用滚到列表底部找刚才的任务 |
| **要处理的浮出来** | 「⚠ 待我处理」筛选（待回应/打回/待人工）；Outbox 里失败、死信、待人工重投的**置顶**，标题标「N 条待处理」 |
| **不假装还有事** | 「待处理事件数」按 `router/processed.json` 算真实值，不拿决策条数凑 |
| **时间一个格式** | `fmtTime()` 兼容 ISO / 毫秒时间戳 / 秒时间戳 / `YYYY-MM-DD HH:mm` |
| **长文折叠** | router 输出、outbox 正文、任务时间线里的决策正文超阈值折叠成「展开 +N」 |
| **只渲染近的** | 收件箱 60 条、决策流 20 条、Decision 20 份、Outbox 40 条，标题标「显示数/总数」 |
| **筛选不被冲掉** | 自动刷新不重置当前筛选（之前每轮都重置回「全部」） |
| **整页常驻** | 左侧任务列表 `sticky`，右侧滚多长都不丢上下文 |

KPI 区固定 3 列 6 张卡（任务总数 / 执行中指派 / 待人工确认 / 阻塞待重做 / 待 agent 回应 / Worker 在跑负载），
运行状态区固定 6 列 2 行 —— 不留参差的空格子。

## 准入策略：谁能发起命令（Step 1）

`super-agent/policy.py` + `super-agent/policy.json`。**判定入口只有一个**
（`policy.check_command`），桥接的「事件长连接路径」与「轮询兜底路径」都调它。

| 情形 | 谁可以发起命令 | 理由码 |
|---|---|---|
| 私聊 | **只有 owner 本人** | `deny_private_chat` |
| owner 指定过的群 | 群内**任何人** | `allow_group` |
| 其它群 | 都不行（owner 本人除外） | `deny_group_not_allowed` |
| 某任务里**临时授权**的人 | 只在**该任务的会话里** | `allow_task_grant` |

```jsonc
// policy.json
{
  "owner": {"open_id": "ou_<open_id>", "name": "owner"},
  "private_chat": {"owner_only": true},
  "groups": {"allow": ["oc_<chat_id>", "oc_<chat_id>"]},   // 其他人可发起命令的群
  "people": {"同事A(做大做强)": "ou_<open_id>"},       // 人员目录（加临时授权用）
  "grant_ttl_hours": 48
}
```

### 临时授权（任务级加人）

授权是**任务的属性**，存在任务 state 的 `authorized_actors` 里，跟着任务走、随任务归档。

**自动路径**（最贴合「bot 问 A，A 回 bot」）：
worker 在回执里写 `- ask: 张三` + `status: waiting_user_input`
→ 系统按人员目录解析名字 → 自动把张三加进本任务授权名单
→ 张三在本任务的会话里回复会被受理，router 收到的事件里带
「临时授权: 任务 Txxx 正在等「张三」回答」→ **沿用该任务，不另起新任务**。

**手动路径**：
```bash
python3 super-agent/sa.py grant  --task T012 --person 张三 --question "单号 360580 能过吗？"
python3 super-agent/sa.py revoke --task T012 --person 张三
```
看板任务详情里也有「＋ 加人 / 撤销」。

**约束**：
- 授权**不跨会话** —— 被授权的人在别的群里说话不算数；
- 授权**有期限**（默认 48 小时，`grant_ttl_hours`），过期即失效；
- 授权只影响「能不能发起命令」，不扩大 worker 的工具权限。

### 其它

- `SA_POLICY_FILE` 可覆盖策略文件路径（自检/演练用）。
- 未配置策略（owner 与 groups 都为空）时**按旧行为放行** —— 配错不会全线瘫痪。
- 判定留痕：拒绝会写桥接日志（`准入拒绝（理由码）：原因`）与 `seen` 记录。

## 不变式校验器（只读）

`super-agent/invariants.py` —— **实现之外的第三方视角**：不复用 sa.py/core.py 的写路径，
自己解析 `state.json` / `events.jsonl` / `outbox/*.json` / `router/inbox.jsonl`，
断言一组「系统该始终成立的东西」。

```bash
python3 super-agent/invariants.py            # 人读报告
python3 super-agent/invariants.py --json     # 机读（看板 / CI）
python3 super-agent/invariants.py --task T012
python3 super-agent/invariants.py --strict   # WARN 也算失败
```

| 规则 | 断言 |
|---|---|
| `task_completed_needs_evidence` | Task completed ⇒ 指派全终态且有产物/回执 |
| `terminal_assignment_has_response` | 指派终结 ⇒ 回执文件存在 |
| `assignment_transitions_legal` | 状态迁移只走合法边；迟到回执不得覆盖终态 |
| `assignment_started_once` | 同一指派不被重复启动执行 |
| `session_not_concurrent` | 同一 worker session 不并发 |
| `chain_dispatch_limit` | 链式派工计数 ≤ 配置上限 |
| `task_unique_per_source_message` | 一条外部消息最多建一个 Task |
| `assignment_ids_unique` | 任务内指派 id 唯一 |
| `outbox_succeeded_has_receipt` | outbox succeeded ⇒ 有投递凭据（旧口径记录只算 WARN） |
| `outbox_not_stuck` | outbox 不长期 pending |
| `artifact_paths_exist` | 登记的产物路径存在 |
| `event_timeline_monotonic` | 事件时间戳不倒流 |
| `every_event_accounted` | 每条事件都有决策留档或被显式处理 |
| `task_has_source` | Task 有可追溯来源 |

看板上「状态不变量」格子实时显示结果（红=有矛盾，绿=自洽）。

**它第一次跑就抓出了 8 处真实矛盾**：6 处「迟到回执覆盖已终结状态」
（`done→failed`、`approved→done`、`cancelled→done` …）+ 2 处「同一指派被启动两次」。
迟到回执覆盖的根因是 `sa.py` 里无条件 `asg["status"] = status` —— 已修：
终态指派收到迟到回执只记 `report_stale` 事件，**不改状态**（方案 §9.4）。

## 飞书表情：接收 / 分配 / 完成

状态播报**只发表情、不发文字**，表情挂在**用户那条消息**上，形成一条进度轨迹：

| 阶段 | 表情 | Feishu emoji_type | 什么时候挂 |
|---|---|---|---|
| 接收任务 | ⌨️ | `Typing` | 消息投给 router 的瞬间 |
| 分配任务 | 💭 | `Status_PrivateMessage` | 指派派出去（assignment → assigned/running） |
| 完成任务 | ✅ | `DONE` | **复核放行**（assignment → approved） |
| **没有派单** | ✅ | `DONE` | router 处理完这条消息但**没有派任何 worker**（自己回一句话就结束了） |

> 分配用的是**蓝色气泡三个点**那个（`Status_PrivateMessage`）——
> Feishu 里 `THINKING` 是「思考」的脸，不是「思考中」。

**「完成」= 复核放行，不是 worker 交付**。worker 交付（`status=done`）只是「交上来了」，
router 还要复核、还要把结果发回给用户 —— 早挂 ✅ 会让用户以为结束了，结果还得再等几十秒。
所以 `done` 不再算完成，只有 `approved` 才算。

**一条消息只挂一个表情**：同一句话可能派出多个指派，按指派逐个调 API 会重复调用
（一次 1~4 秒）。桥接按 message_id 去重，一轮只挂一次。

**表情是替换，不是叠加**：一条消息上只留当前状态那一个表情
（接收 → 分配 → 完成 依次替换别人）。做法是先 `reactions list` 找出**我们自己**
加的那条，删掉，再加新的。完成是终态，不会被降级回分配/接收。

- **收到的文字回执**（"已收到，正在安排处理"）关掉了 —— `ack.on_receive: false`。
- **受理回执**（`purpose=ack` 的 "已受理：任务号 Txxx"）也被拦成 `suppressed`，
  不发出去；任务号在**最终结果**里给出（`T575 已完成：…`）。
- 表情挂在 `assignment.origin_ref.message_id`（触发这次工作的那条用户消息），
  缺了才回落到任务来源 —— 同一份工作的三个表情落在同一条消息上。

配置（`lark_config.json`）：

```json
"reactions": {
  "enabled": true,
  "received": "Typing",
  "dispatched": "Status_PrivateMessage",
  "completed": "DONE",
  "suppress_ack_text": true
}
```

去重靠桥接状态里的 `reaction_seen`（指派 → 上次看到的阶段）；
首次看到某个指派时只有「10 分钟内刚发生」才补表情，不给历史活倒灌。

### 没有派单 → 直接标完成

router 看完一条消息后可能决定「不用派 worker」（自己回一句话、回答状态查询、
重复触发去重……）。这类消息如果只挂着 ⌨️，看起来像机器人卡住了 —— 实际上这一轮
早就结束了。所以桥接会把它直接换成 ✅。

判定（`sync_no_dispatch_reactions()`，三个条件都满足才标）：

1. router 已经处理过这条消息 —— `router/outbox.jsonl` 里有它这一轮的回复记录；
2. 过了宽限期（默认 45s，给 Decision 落地留时间），且在回溯窗口内（默认 6 小时）；
3. 这条消息**确实没有派工**：既没有 Decision 派工条目，也没有任何指派落在它身上
   （`assignment.origin_ref.message_id` / 任务 `source_ref.message_id`）。

台账在 `lark_state.json` 的 `no_dispatch_done`，天然幂等。

> 抢跑保护：如果先标了 ✅、之后又真的派了工（Decision 晚到），
> 下一轮扫描会**先撤掉那个 ✅** 再挂 💭 —— 否则「完成是终态」的规则会把派发表情挡住。

## @ 某人：用 mentions 参数，不是「@名字」文本

飞书里的 @ **不是正文里的一个「@名字」** —— 它是消息内容里的**提及实体**：

| 消息类型 | 提及写法 |
| --- | --- |
| 文字消息（桥接用的） | `<at user_id="ou_xxx">名字</at>` |
| 富文本 post | `{"tag":"at","user_id":"ou_xxx"}` |
| 卡片 | `<at id=ou_xxx></at>`（**只**在卡片里生效） |

正文里写「@杨晓辰(小德)」只是**普通文字**：对方**收不到通知**、不高亮、点不开。
卡片写法 `<at id="ou_xxx"></at>` 放进文字消息里同样**不生效**（原样当文本显示）。

桥接（`render_mentions()`）接受三种写法，都会变成真正的提及：

| 写法 | 从哪来 | 说明 |
| --- | --- | --- |
| `mentions: [{"open_id": "ou_xxx", "name": "…", "at": "start\|end"}]` | Decision 的 outbox 条目 | **结构化参数**（推荐）；`at` 省略时放正文开头 |
| `@[ou_xxx]` / `@[ou_xxx\|显示名]` | 正文里的行内占位符 | 要把提及放在正文中间时用 |
| `<at user_id="ou_xxx">名字</at>` | 正文里已经写对 | 原样保留 |

顺带把常见的写错形式 `<at id="ou_xxx"></at>`（卡片语法）**修正**成文字写法。

- 纯文本 `@名字` **不会**被自动升级成提及（避免把报告里提到的人名都 @ 一遍）。
- `name` 可以省略 —— 飞书会用对方自己的显示名渲染，省略就不在正文里出现姓名。

> 线上实测：发出的消息读回来带 `mentions: [{id: "ou_xxx", key: "@_user_1", name: "…"}]`
> → 对方收到真的 @ 通知。

## 批次：同一句话派的几件事，回报合并成一条

同一句用户消息派出**多条指派**（并行做几件事）时，它们属于**同一批**
（批次 id = 触发它的那条用户消息 id，记在 `assignment.batch_id`）。

以前一条指派一条群消息（用户要连着看三条），现在：

- `purpose=result` 的消息挂上批次 id；
- 桥接投递时**等齐**：本批还有指派在跑/待复核 → 这条结果**先暂存不发**；
- 本批全部闭环 → 几条结果**合并成一条**发出去（共享同一条投递凭据）。

兜底：暂存超过 30 分钟（`BATCH_HOLD_MAX_SECONDS`）还没等齐，照发（不能无限期压着）。

router 侧配合（契约见 `skill/router-system.md`）：

- 复核轮的事件里会带**本批视图**（共几条、还在飞几条、各自状态与产出）；
- 还有兄弟在跑 → 本轮**只放行不回话**；
- 本批全部结束 → 一次性复核并**合并成一条**回报。

查批次：`sa.py batch <batch_id>`（返回 total / in_flight / complete / assignments）。

> 注意：受理回执、提问、派单回话**不进批次** —— 它们要立刻发。

## 投递与表情为什么分成两个线程

桥接里投递 outbox 和同步表情以前挤在同一个 5 秒循环里，而一次表情 API 调用要 1~4 秒
—— 表情一慢，**用户的回复就被拖着**（实测拖过 5~9 秒）。现在：

| 线程 | 节奏 | 干什么 |
|---|---|---|
| `deliver_loop` | 1 秒一 tick，outbox 目录变了就投 | 投递 outbox（含批次合并） |
| `reaction_loop` | 3 秒一轮 | 补表情（派发 / 完成 / 无派单） |

状态文件的读-改-写走 `state_lock()`（**同线程可重入**），但**表情 API 调用不持锁** ——
持着锁调 4 秒的 API 会把消息受理也堵住。

## 话题 ID 什么时候有

| 情形 | 事件里有 thread_id？ | 话题什么时候出现 |
|---|---|---|
| 用户在**话题里**发消息 | ✅ 有（`omt_xxx`）+ `root_id`（话题根消息） | 用户自己开的话题，消息进来就带着 |
| 用户在**主频道**发消息 | ❌ 没有 | **机器人第一次以「话题回复」回话时**由飞书创建 —— 回话之后才有 `omt_xxx` |

主频道这条路径有个坑：`+messages-reply --reply-in-thread` 的返回里**不给** thread_id
（只有新消息的 message_id）。要拿到话题 id 得用 `+messages-mget` 回查那条回复自己
（`resolve_thread_id()`）。

所以桥接在回话成功后会把话题 id **回写到任务的 `source_ref.thread_id`**
（`_bind_thread_to_task()`）—— 主频道发起的任务也就能顺话题找回了。
同时把新话题登记进轮询话题表，长连接降级成轮询后也收得到话题里的追加消息。

话题里的消息进 router 时长这样：

```
[事件] user.message
triggerId: a2f7837a3528
说话人: 同事A（ou_<open_id>）
渠道: 商户平台研发（群名可信）（oc_<chat_id>）
话题串: 话题 omt_1998064ac20f9a5d · 根消息 om_x100b60eca6f3cca8df1a3b9f453fff4
```

## 看板上的模型切换与 Session 轮转

看板顶部的「运行状态」面板里有两块新控件：

- **Router 模型** —— 下拉选模型 + 「切换」+「切回默认」。降级时显示「降级中 → xxx」。
  写 `router/model.json`，运行中的 router **下一轮生效**（不用重启、不丢 session）。
- **Router Session** —— 「轮转 Session」按钮：归档当前会话、重开一个干净的。
  会话文件进 `~/.pi/agent/sessions/.../archive/`，上下文丢一段，
  router 用 `sa.py tasks-list` 自行重建现状。

对应 API：

```bash
curl -X POST localhost:8788/api/router/model -d '{"model":"bella/Deepseek-V4.1-Flash"}'
curl -X POST localhost:8788/api/router/model -d '{"restore":true}'
curl -X POST localhost:8788/api/router/rotate -d '{}'
```

## Router Session 命名

会话 display name 带**开启时间**：`Super Agent Router 2026-11-18 15:35:02`。

`pi --resume` 的会话选择器渲染的是 `session.name`，所以历次 router session 在列表里
一眼能分清是哪一次开的（早期创建的都叫同一个名字，可用
`router.py rename-sessions` 按文件名时间戳回填）。

```bash
python3 super-agent/router.py rename-sessions   # 回填历史会话名
```

## 常用命令

```bash
python3 super-agent/router.py rotate      # 手动轮转 router session（统一会话太大时用）
python3 super-agent/router.py status      # 看 session 规模与待处理事件
python3 super-agent/sa.py tasks-list      # 列任务（router 判断「沿用哪个」用）
python3 super-agent/selftest.py           # 41 项回归断言
```

## 接收消息：官方事件订阅（长连接）

**不用轮询**。官方推荐方式是**事件订阅 → 使用长连接接收事件**：

> 「如果你已经集成飞书 SDK，且正在开发的是企业自建应用，推荐你使用更加安全高效的长连接订阅方式」
> —— [飞书开放平台 · 事件概述](https://open.feishu.cn/document/server-docs/event-subscription-guide/overview)

长连接的优势（官方原文）：无需公网 IP/域名、无需内网穿透、SDK 内置鉴权与加密
（不用自己解密验签）、接入成本从一周降到 5 分钟。

本项目用 `lark-cli event consume im.message.receive_v1 --as bot` 消费，
底层就是 **feishu-websocket 长连接**：

```bash
python3 super-agent/lark_bridge.py follow    # 守护进程：长连接为主，轮询兜底
```

### 事件模式带来三件事

| | 事件订阅 | 轮询 |
| --- | --- | --- |
| 延迟 | **实时推送**（WebSocket） | 20s 轮询周期 |
| 话题内回复 | ✅ 事件带 `root_id`/`thread_id` —— 天然识别 | 要单独轮询每个话题串 |
| @ 判定 | ✅ 结构化 `mentions` 字段 | 字符串匹配 |
| 历史消息 | ❌ 天然不会推历史 | ⚠️ 会回溯，必须额外做基线 |

事件字段：`message_id` `chat_id` `chat_type` `sender_id` `sender_type` `content`
`create_time` `root_id` `thread_id` `mentions`。

触发规则：
- 群里必须 **@ 机器人**（用 `mentions` 精确判定）；话题内的追加消息**不用 @**（机器人已在话题中）；
- `allow_any_sender` 为真时，团队成员在任意群 @ 都能触发；
- 动态发现的其他群，**前缀词**（「派单」「任务」）不触发 —— 陌生人随口一句不该把机器人叫起来。

### 降级与保底

- 长连接连不上（总线被占/网络故障）→ **自动降级为轮询**，并打日志；
- 状态锁抢不到时**等待最多 2.5 秒**而不是丢弃 —— 官方要求长连接 3 秒内处理完事件，
  抢不到锁就丢会造成消息丢失；
- `startup_grace_seconds`（默认 600）：只接受桥接启动前后宽限期内收到的消息，
  更早的一律标记已读 —— 避免动态发现一个群就把它几个月前的消息全灌给 router。

## 话题里的消息也要能被收到

飞书的**话题串消息不在主频道消息列表里** —— 只轮询 `+chat-messages-list` 完全看不到
用户在话题里追加的指令（表现为「我在话题里回复了，router 没反应」）。

所以桥接进程**同时轮询已知话题串**：

1. 主频道消息里带 `thread_id` 的 → 登记到 `lark_state.json` 的 `threads`；
2. 每轮对每个已知话题串调 `+threads-messages-list` 拉新消息；
3. 话题里**不用再 @ 机器人**（机器人已经在话题里参与过）——
   判定依据是该话题里出现过机器人的消息（`bot_engaged`）。

去重沿用同一套 `seen` + `handled.jsonl` 账本，主题与主频道的消息不会重复投递。

### 一次唤醒处理多条

用户连着发几条时，router **一次唤醒连着处理最多 4 条**（`SA_ROUTER_BATCH` 可调）。
session 已经是统一的，连着处理只省掉每条之间的一次唤醒等待。超过 4 条则分批，
避免单轮上下文压力过大。

## 飞书回复走话题串

所有对用户的回复都用 `--reply-in-thread`（飞书话题串），**不混在群聊主频道**：

```
群里：
  ┌ 话题 omt_xxx ─────────────────────────────┐
  │ 用户：@飞书助手 写 /tmp/x.md       │
  │   └ app：✅ 已收到，正在安排处理            │
  │   └ app：已受理，任务号 T154：…            │
  │   └ app：T154 已完成并核对：…              │
  └────────────────────────────────────────────┘
```

群消息是多人共享的，话题串让「这条消息 → 一串回复」归到一起，不刷屏。

- 开关：`lark_config.json` 的 `reply_in_thread`（默认 `true`）。
- 没有来源消息的任务（如人工在看板建的任务）没有可回复的根消息，会退回发到主频道。

## 飞书上的消息形态

一条用户消息会产生 2–3 条回复，各自职责不同：

| 发出方 | 内容 | 何时 |
| --- | --- | --- |
| 桥接进程 | 「✅ 已收到，正在安排处理」 | 收到消息即刻（**不含任务号** —— 此刻任务还不存在） |
| Router | 「已受理，任务号 T081：……」 | 提交 Decision 后（**带真实任务号**） |
| Router | 「已完成（T081）：……」 | worker 回执核对通过后 |

**任务号的时序**：`submit-decision` 先执行后返回 —— 任务号由 Task Center 在执行时分配，
所以 Router 是「先提交 Decision → 从 `result.applied` 里读到 `{"kind": "task.create",
"task": "T081"}` → 再发一条带任务号的 outbox」。

**对外粒度（正例 / 反例）**：outbox 是直接落到用户（及可能的群成员）眼前的文本，
只写「数量 + 结论 + 是否已操作 + 产出文件路径」。

- ✅ 正例：`T081 已完成：3 条群消息里 1 条需要你处理，未做任何操作。明细见 /tmp/msgs-2026-11-18.md`
- ❌ 反例：`T081 已完成：群里 3 条消息，提交人是同事B(<uid>)，群 id oc_<chat_id>，
  工单 341212 / 计划 352956，接口 https://bpm.example.com/api/open/processTask/pageQuery?uid=20230359，
  仓库 <repo-name>，表 <table_name>`

贴出前自检：「这段话里有别人名字/内部 id 吗？」—— 有就改写成数量与结论，
把明细留在产出文件里，让用户自己打开。

## 框架自修也派 worker

router 不能自己改代码（护栏只允许写 `/tmp`）。发现框架 bug 时派 `general-purpose` worker 去修，
指派单里写清：哪个文件、什么现象、期望行为、改完跑 `selftest.py` 验证、需要重启哪个守护进程。

worker 有完整工具，改完能自己验证 —— 比 router 自己改更可靠。

## 调度框架自修

router 被授权修复**框架自身**的 bug（`sa.py` / `core.py` / `worker_runner.py` /
`router.py` / `lark_bridge.py`）—— 这在实践中很有用（已经因此修掉过
「回执登记不上」「reviews 被覆盖」「worker 崩溃」三个真 bug）。

边界：只改框架不改业务；改完要交代；改完重启对应守护进程；改完跑一次自检。

```bash
python3 super-agent/selftest.py     # 20 项断言，回归用
```

## 硬规则

1. **super agent 不执行任务本体**。需要读代码、查库、跑脚本的活，一律派给 worker。
2. **上下文走文件，不走口述**。指派时把需要的材料放进 `tasks/T001/context/` 并在指令单里给绝对路径。
3. **一次指派 = 一个可独立完成的单元**。如果一件事必须拆成多步，拆成多个 Assignment，或在指令单里写清内部步骤。
4. **回执必须可验证**：有产出就给绝对路径，无产出就写明为什么。
5. **不并行超过 3 个指派**。并发太多会让回执难以归因。
6. **人工干预优先于自我拆解**。看板上的批注 / 打回是高优先级指令。
7. **人工待办必须销账**（`sa.py ack`），否则任务永远停在 `needs_human`。

## 验证结论

| 时间 | 任务 | 结论 |
| --- | --- | --- |
| 2026-11-17 21:09 | T006 | worker 已新建 `/tmp/loop2.md`，文件内容恰好一行 `LOOP2 OK`（非 `LOOP CHECK`）；router 已用 `cat -n` / `od -c` 核验通过（`cat -n` 输出 `1\tLOOP2 OK`，`od -c` 输出 `L O O P 2   O K \n`），并已回飞书 |
