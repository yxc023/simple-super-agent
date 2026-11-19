# 代码 review 与整理记录

对 `super-agent/` 做了一次全量 review（3 个并行子 agent 分模块读代码），
下面是**已修**与**留待后续**的问题清单。修复后 `python3 super-agent/selftest.py` **249/249 全部通过**。

## 一、结构与配置

| 项 | 处理 |
| --- | --- |
| 根目录散落文件（早读规格、两个辅助脚本、飞书下载的 pdf/jpg/png） | 规格进 `docs/`，脚本进 `scripts/`，二进制垃圾直接删除 |
| `super-agent/super-agent/channels/…` | 路径拼接重复产生的垃圾目录，删除 |
| `__pycache__`、`.task-counter`、`lark_state.lock` | 运行期产物，加入 `.gitignore` |
| 三个配置文件 | 真实文件（`lark_config.json` / `policy.json` / `registry.json`）不入库，只提交 `*.example.json` 模板；写清初始化步骤与「改完要重启什么」 |
| `SKILL.md` 与 `router-system.md` 两份手抄副本 | 改为**单一真源**：`router-system.md` + `skill-frontmatter.md` → `sync_skill.py` 生成 `SKILL.md`（两份已经漂移过一次：SKILL.md 缺触发闸门与 Decision 的 `requester` 字段） |

## 二、已修的缺陷

### 安全 / 越权

| 位置 | 问题 | 修法 |
| --- | --- | --- |
| `sa.py` `task_dir` | task_id 来自飞书消息与 HTTP 请求体，全无校验，`TASKS_DIR / task_id` 可被 `../` 带出状态根（**路径穿越写**） | 统一加 `TASK_ID_RE = ^T\d{3,}$` 校验，不合法直接退出；`notes_path` 同样收口 |
| `serve.py` `/api/file` | 用字符串前缀比较判目录归属，`/…/super-agent-evil` 可绕过；且能读到含 open_id 的 `lark_config.json` / `policy.json` | 改用 `is_relative_to` + 一级目录白名单（tasks/channels/outbox/router） |
| `serve.py` `/api/new-task` | 请求体里的 `id` 原样传给建任务 | id 一律由服务端分配 |
| `serve.py` 错误处理 | 把 `traceback.format_exc()` 直接回给客户端 | 细节只进服务端日志，客户端只看到 `internal error` |
| `web/index.html` | `esc()` 不转义单引号 → 人员姓名里一个 `'` 就能闭合 `onclick` 属性注入脚本 | `esc` 补 `'` → `&#39;`；`openModal` 的标题补转义 |
| `router_gate.ts` | bash 白名单是**正则匹配前缀**：`python3 …/sa.py && rm -rf ~` 也能命中，护栏形同虚设 | 改为 shell 词法解析：必须恰好是 `python3 <repo>/super-agent/sa.py <白名单子命令>`，出现 `&& ; \\| \\` $() 换行 一律拒绝 |
| `seed_demo.py --clear` | 递归删除生产状态根下的全部任务与 channel，无确认 | 必须同时加 `--yes` |
| `policy.py` `find_grant_for_sender` | 调用方没带 `chat_id` 时，会话约束整段被跳过 → 临时授权可在任意会话生效 | 查不到会话就拒绝，不当作「不约束」 |
| `policy.py` `private_chat.owner_only=false` | 这个开关完全不生效（私聊仍一律拒绝） | 开关为假时私聊显式放行 |

### 逻辑 bug

| 位置 | 问题 | 修法 |
| --- | --- | --- |
| `worker_runner.py` `run_one_guarded` | 函数体调用**自己** → 必然 `RecursionError`；而 `loop()` 用的是 `run_one`，兜底逻辑从来没被调用过（worker 崩了指派永久卡在 running） | 改调 `run_one`，`loop()` 接上 guarded 版本 |
| `worker_runner.py` `pending_assignments` | 重试用尽的指派被跳过但仍留在 `assigned` → 无声悬挂 | 显式置 `failed` + 记事件 + 通知 router |
| `core.py` `count_active` | 只数 `assigned/running`，漏了 `rework` → 实际并发可超过 `max_parallel` | 与 `sa.ACTIVE` 对齐 |
| `core.py` `list_outbox` | 按文件名排序，而文件名是随机 uuid → 外发顺序是随机的 | 按 `created_at` 升序（FIFO） |
| `core.py` `render_instruction` | 先编号再过滤空要求 → 渲染出「1. 2. 4.」跳号 | 先过滤再编号 |
| `chain_view.py` `_iso` | 数字一律按毫秒处理，10 位秒级 epoch 渲染成 1970 年；无时区 ISO 串硬编码 `+08:00` | 按量级分档（秒/毫秒/微秒/纳秒）；统一走本机时区 |
| `chain_view.py` decisions | 解析结果未做类型检查，内容是数组时 `dec.get` → 整个看板 500 | 加 `isinstance` 判断 |
| `router.py` `prompt()` | 只捕获 `RuntimeError`，主模型超时（`TimeoutError`）不会降级 | 超时/OSError 一并纳入降级判定 |
| `router.py` `mark_degraded` | 把降级模型写成 `primary` → 原始主模型永久丢失，`--restore` 也回不去 | 不再改写 `primary`，降级目标单独存 |
| `router.py` session 目录 | 写死本机路径 `/Users/…/yxc-open-agent` → 换机器/换目录两个命令直接失效 | 由 `REPO` 推导目录名 |
| `invariants.py` `_event_status` | 漏掉 `rejected` / `redirected` / `assignment_failed` 三类事件 → 重放出的权威状态停在旧值，且持续误报 ERROR | 补齐事件→状态映射；`responded` 优先读结构化 `status` 字段 |
| `cleanup_state.py` | `--apply` 会把人工打回/改派的**合法状态**回滚；并自动写 `review="approved"` 凭空伪造人工放行 | 依赖上面的事件映射修正；不再自动写 `review` |
| `cleanup_state.py` | 用 `"/super-agent/"` 子串过滤基础设施路径 —— 仓库本身就叫 super-agent，会把自己的交付物也丢掉 | 改为按路径成分判定 |
| `policy.py` `load_policy` | 已知 key 且值不是 dict 时直接覆盖默认结构 → `owner` 写成字符串会让 `owner_id()` 崩溃 | 类型不对就保留默认结构 |
| `policy.py` `resolve_person` | 子串匹配下单字姓名会误命中；`ou_` 直传判断排在 people 循环之后 | `ou_` 前置 + 精确匹配优先 + 子串匹配收紧 |
| `lark_bridge.py` 状态读写 | 直写覆盖 → 断电留下半截 JSON，之后每轮解析都抛异常，状态文件永久不可用 | 临时文件 + `os.replace` 原子写；坏 JSON 备份后退回默认结构 |
| `lark_bridge.py` `state["seen"]` | 状态文件缺 `seen` 键时直接 KeyError | 统一 `state.setdefault("seen", {})` |
| `sa.py` `save_state` | 直写覆盖，sa / worker_runner / router / 看板四个进程并发写 → 读者可能读到半截 JSON | 临时文件 + `os.replace` |
| `serve.py` `Content-Length` | 解析在 try 之外，缺失/非数字 → 裸 500 traceback | 收进 try，非法值返回 400 |

### 死代码与一致性

- 删除：`core.enabled_worker_ids`、`core.REPORT_STATUSES`、`policy.POLICY_FILE`（import 时求值，导致之后设 `SA_POLICY_FILE` 不生效）、`worker_runner.RUN_DIR` / `ACTIVE_STATUSES`、`trace.py` 未用的 import、`router.py` 里 `sync_skill.main.__wrapped__ if False else None` 之类的调试残留。
- `router_gate.ts` 的「护栏限制 bash」自检断言同步更新，并新增一条「拒绝 shell 拼接」的断言。
- `board.sh`：pid 与日志文件名统一按端口派生，补 `stop` / `status` 子命令、端口数字校验、启动等待放宽到 15 秒并在失败时打印日志尾部。

## 三、留待后续（本次未做，列出以免遗忘）

| 优先级 | 项 |
| --- | --- |
| 高 | `lark_bridge.py` 事件长连接启动时的阻塞 `readline`（45s 超时形同虚设，可能永久卡在启动阶段）；stderr 无 drain 线程 → 管道写满会阻塞子进程；降级时只 terminate 父进程，`lark-cli event consume` 可能存活并继续占着事件总线 |
| 高 | `lark_bridge.py` 轮询路径**没有**走 `policy.check_command`（准入闸门只在事件路径生效），`allow_any_sender=true` 时等于对全群开放指令入口 |
| 中 | `router_gate.ts` 的 read 白名单放开了 `router/`、`inbox/` 整个目录（含原始事件与他人消息），应收窄到具体子目录 |
| 中 | `serve.py` 无 CSRF/Origin 校验；`--host` 非 127.0.0.1 时虽有告警但仍可对外暴露 |
| 中 | `sa.py` 派工时用 `len(assignments)+1` 生成 assignment id，无锁 → 并发派工会撞号 |
| 中 | `worker_runner.py` 领取任务时不看 registry 的 per-agent `max_parallel`，绕过 `cmd_assign` 的校验 |
| 低 | `router.py` 的 inbox 游标每 2 秒全量读并对前缀算 sha256；processed 集合每条事件全量重写 → 随运行时间线性变慢 |
| 低 | `lark_bridge.py` 每次表情调用全量读 `logs/reactions.jsonl`；该文件只追加不裁剪 |
| 低 | `sa.py` / `serve.py` 里几处 `print(json.dumps(...))` 导致 stdout 出现两个 JSON 文档 |

## 四、验证

```bash
python3 super-agent/selftest.py        # 249 / 249 通过
python3 super-agent/sync_skill.py --check
bash -n super-agent/board.sh super-agent/lark_bridge.sh
```

---

# 第二轮：一条真实消息链路的耗时 review 与整改（2026-11-19）

## 一、被 review 的那条链路

`2026-11-19T15:24:29 · om_x100b60de0c189cb4de03b30a8b9be4a · 年假以后一律可以同意`
—— 用户一句话，router 派了两件事并行，**211 秒**后才有最终结果，
中间触发了 **57 次模型调用**、约 390 万缓存输入 token、成本 **$0.082**，
用户收到 **3 条群消息**。

实测耗时分布：

| 段 | 耗时 | 问题 |
| --- | --- | --- |
| worker 执行（44 次模型调用） | 149s | 调用轮次过多（本次**不优化 worker 自身执行**） |
| router 决策轮 | 32s | 第一次工具调用前**空转 23 秒** |
| 两次复核串行 | 38s | 拆成两轮 → 两条群消息 |
| 投递 + 表情滞后 | 16s | 投递与表情挤在同一个 5 秒循环里 |

## 二、本次落地的改动

| 项 | 改动 | 位置 |
| --- | --- | --- |
| P0-3 | BPM skill 新增坑 31「复核只查一次」：一轮验收就收工，**别点进已办详情**（会被重定向到 SSO），跳登录页立刻放弃（v0.7.0 → v0.7.1） | `~/.pi/agent/skills/ke/ke-bpm-approver/SKILL.md`（不在本仓） |
| P0-4 | 契约新增「先派单，后叙述」：收到事件第一个动作就是写并提交 Decision，提交后再叙述 | `skill/router-system.md`（真源）→ `SKILL.md`（生成） |
| P0-5 | 回执的 `updated_at` 由**系统盖章**（worker 自填的时间戳实测出现过「比现在还晚 14 分钟」的编造值） | `worker_runner.py` + `core.py` 模板 |
| P1-6 | **批次**：同一条用户消息派出的多条指派 = 同一批（`assignment.batch_id`）；同批 `purpose=result` 消息**等齐后合并成一条**发出；router 侧按事件里的「本批视图」决定放行/回话；`sa.py batch <id>` 可查 | `sa.py` / `core.py` / `lark_bridge.py` / `router.py` / 契约 |
| P1-7 | 表情**按 message_id 去重**（一条消息只调一次 API）；投递与表情**拆成两个线程** | `lark_bridge.py` |
| P1-8 | outbox 投递改成 **1 秒一 tick**（outbox 目录变了就投），不再被 5 秒轮询和表情调用拖住 | `lark_bridge.py` |
| P1-9 | `save_state` 临时文件名带 pid+随机后缀（**并发写不再撞名** → 已实际发生过一次 `os.replace` 报错）；状态锁**同线程可重入**；`sync_*` 的读-改-写纳入锁 | `lark_bridge.py` |
| P1-10 | 「完成」表情改挂在**复核放行**（approved）而不是 worker 交付（done）—— 避免「已完成」之后用户还得再等 38 秒 | `lark_bridge.py` |
| P2-13 | 契约要求后续指派**带上具体标识**（单据链接/编号），别让 worker 再翻一遍列表 | `skill/router-system.md` |

## 三、验证

```bash
python3 super-agent/selftest.py        # 279 / 279 通过（新增 [26g-3] 21 条断言）
```

- 新增断言覆盖：批次识别（跨任务同批）、真实 Decision 路径写入 `batch_id`、
  同批结果「暂存 → 等齐 → 合并成一条」、表情按消息去重、`done`/`approved` 的阶段语义、
  状态临时文件名唯一、锁可重入、投递/表情分线程、回执盖章、契约三条新规则。
- 线上：桥接与 router 已重启加载新代码（事件长连接正常）。

## 四、本次明确**不做**的

- **worker 自身执行过程**的优化（大改少调、脚本文件替代内联 JS、合并验证轮次）——
  按决定本次不动，只做 P0-3 这条例外。
- P2 其余项（router 上下文瘦身、兜底轮询降频、session 继承的并发保护）。

## 五、遗留风险

- **批次暂存的兜底是 30 分钟**：如果同批某个指派永远不闭环（worker 卡死、router 挂了），
  结果消息会压 30 分钟才发。可接受，但要记得这个上限存在。
- **router 侧的「等齐」靠模型自觉**：代码只保证「结果消息会合并」，
  模型若仍提前发一条中间结果，用户会收到两条（一条合并后的 + 一条提前的）。
  契约里已写明「还有在跑的只放行不回话」，但这是**提示词约束**，不是硬闸门。

---

# 第三轮：@ 提及要发成「消息内容里的提及实体」（2026-11-19）

## 一、问题

飞书的 @ **不是正文里的一个「@名字」** —— 它是**消息内容里的提及实体**：

| 消息类型 | 提及写法 |
| --- | --- |
| 文字消息（桥接用的） | `<at user_id="ou_xxx">名字</at>` |
| 富文本 post | `{"tag":"at","user_id":"ou_xxx"}` |
| 卡片 | `<at id=ou_xxx></at>`（**只**在卡片里生效） |

原状（两个都错）：

1. router 在正文里写「@杨晓辰(小德)」→ **只是普通文字**，对方收不到通知、不高亮、点不开；
2. `ke-bpm-approver` skill 让 worker 产出 `<at id=ou_xxx></at>` → 那是**卡片语法**，
   放进文字消息里**不生效**（`lark-cli --dry-run` 实测：原样进 content）。

所以凡是「需要人拍板」的回报（`purpose=question`），实际**根本没 @ 到人**。

## 二、改动

| 位置 | 改动 |
| --- | --- |
| `lark_bridge.render_mentions()` | 新增：把三种写法转成真正的提及实体 —— ① outbox 记录的 `mentions` 结构化字段；② 行内占位符 `@[ou_xxx]` / `@[ou_xxx\|名字]`；③ 已写对的 `<at user_id=…>` 原样保留；并把卡片写法 `<at id=…>` **修正**成文字写法 |
| `lark_bridge.send_reply/send_text` | 发送前统一过 `render_mentions()`；批次合并时各条记录的 mentions 也会合并 |
| `core.add_outbox` / `sa.apply_decision` | outbox 记录新增 `mentions` 字段，Decision 的 outbox 条目可直接给 `mentions` |
| `skill/router-system.md`（真源） | 新增「@ 某人：用 `mentions` 参数」一节：说清三种消息类型的写法差异、结构化参数与行内占位符、**别在正文里只写名字** |
| `ke-bpm-approver` SKILL.md（仓外） | v0.7.1 → **v0.7.2**：回报规范的 @ 写法从卡片语法 `<at id=…>` 改成文字语法 `<at user_id="ou_xxx">名字</at>`，并注明「不要用卡片写法」 |

**刻意不做**：把正文里的「@名字」自动升级成提及 —— 报告里经常提到第三方姓名，
自动升级会误 @ 到不该被通知的人。纯文本 `@名字` 保持原样。

## 三、验证

- `selftest.py` **291 / 291**（新增 `[26g-4]` 12 条断言：占位符转换、卡片写法修正、
  结构化 mentions 的 start/end、正文已提过不重复、纯文本 @名字 不被升级、
  outbox 带 mentions、契约三条）。
- **线上实测**：用桥接的真实发送路径发了一条带 `mentions` 的私聊消息，读回来是
  ```json
  "mentions": [{"id": "ou_b18ba1fe9a09cabbe57e9af34535cec7",
                "key": "@_user_1", "name": "杨晓辰(小德)"}]
  ```
  → 飞书认成了真提及（`key=@_user_1` 是飞书内部的提及占位键），对方会收到 @ 通知。

---

# 第四轮：降低 router 的思考等级（2026-11-19）

## 一、为什么

第一轮诊断出：router 决策轮 **32 秒**里，**23 秒是第一次工具调用之前的空转思考**
（消息 → 事件注入 → 第一次调工具之间没有任何输出）。这段时间用户什么都看不到。

根因是思考档位：pi 的会话记录里 `thinkingLevel = medium`。

router 的职责是「语义判断 + 写 Decision」，不是长链推理任务 —— medium 太奢侈了。

## 二、改动

| 位置 | 改动 |
| --- | --- |
| `router/model.json` | 新增 `thinking` 字段，默认 **`low`** |
| `router.py` `PiSession.start()` | 启动 pi 时带 `--thinking <level>`；**启动后再用 RPC `set_thinking_level` 打一遍**（pi 恢复 session 时会把 session 里记录的老等级带回来，只靠启动参数不保险） |
| `router.py` `set_model()` / `rotate()` | 换模型、轮转 session 后重打等级 |
| `router.py` `sync_model()` | 配置里改了等级 → 运行中下一轮生效，不用重启 |
| `router.py model --thinking <level>` | CLI 改等级（off\|minimal\|low\|medium\|high\|xhigh\|max），非法值直接报错 |
| `router.py status` / `snapshot()` | 状态里带上 `thinking` / `thinking_level` |
| `serve.py` `POST /api/router/model` | 也接受 `{"thinking": "low"}`（看板 UI 暂未接） |

## 三、验证

- `selftest.py` **299 / 299**（新增 `[26g-5]` 8 条断言）。
- RPC 单独验证过：`set_thinking_level` 对 `low` / `medium` 都返回 `success: true`。
- 重启后日志：`pi session 就绪：… model=MiniMax-M3.1-Flash thinking=low` +
  `router 思考等级 → low`；`router.py status` 显示 `thinking: low`。

## 四、注意

- 只改了 **router**；worker 的思考等级没动（worker 干的是真活，需要推理）。
- 效果要等**下一条真实消息**才能量到：看 pi 会话记录里的 `thinkingLevel`
  从 `medium` 变成 `low`，以及决策轮里「第一次工具调用前的空转」是否明显缩短。

---

# 第五轮：收到消息就贴「敲键盘」，不等 router 结果（2026-11-19）

## 一、改动

`lark_bridge.deliver_user_message()` 原来的顺序是：

```
投 router（sa.notify）→ 贴 ⌨️ 表情（同步，~1 秒）
```

改成：

```
贴 ⌨️ 表情（**异步**发起）→ 投 router（sa.notify）
```

- **异步**是关键：表情 API 一次要 ~1 秒，如果同步贴在投 router 前面，
  反而把 router 的启动拖后一秒（那一秒是用户的真实等待）。
  现在两个并行：贴表情的请求先发出去，router 同步启动。
- **加锁**：表情是「替换」语义（贴新的 → 删旧的），
  两次贴表情并发会互相覆盖（旧的可能还没删、新的又加上）。
  所有贴表情动作共用 `_REACTION_LOCK` 串行执行。
- 落点在**准入检查之后**：只对真正受理的消息贴，没 @ 机器人、被策略拒绝、
  正文过短的消息不会贴。

## 二、验证

- `selftest.py` **303 / 303**（新增 `[26g-6]` 4 条断言：
  贴表情在投 router 之前、异步入口存在、全程加锁）。

---

# 第六轮：企微智能机器人接入（2026-11-19）

## 一、接入方式

企业微信「智能机器人」API 模式，两种接法：

| | 回调（Webhook） | **长连接（WebSocket）** |
| --- | --- | --- |
| 公网地址 | **要**（HTTPS 回调地址） | 不要 |
| 加解密 | 要（Token / EncodingAESKey） | 不要 |
| 凭据 | Token + EncodingAESKey | **BotID + Secret** |

本机没有公网地址 → 选长连接。端点 `wss://openws.work.weixin.qq.com`，
凭据校验 `aibot_subscribe`（`body.bot_id` + `body.secret`），
实测订阅返回 `errcode:0`。

## 二、协议要点

| 动作 | 命令 | 关键字段 |
| --- | --- | --- |
| 订阅 | `aibot_subscribe` | `body.bot_id` / `body.secret` |
| 收消息 | `aibot_msg_callback` | `body.msgid` / `chatid` / `chattype` / `from.userid` / `text.content` |
| 收事件 | `aibot_event_callback` | `enter_chat` / `template_card_event` / `feedback_event` / `disconnected_event` |
| 回复 | `aibot_respond_msg` | **必须透传回调帧的 `headers.req_id`**（24h 内有效） |
| 主动推送 | `aibot_send_msg` | `body.chatid` + `body.chat_type`（1 单聊 / 2 群聊） |
| 心跳 | `ping` | 应用层命令帧，不是 WebSocket 协议级 ping |

**一个机器人同时只能有一条长连接**（新连接把旧的踢掉）→ 回复必须走守护进程那条连接。

## 三、两个真踩到的坑

### 坑 1：库自带的协议级心跳会把连接掐掉

`websockets` 默认 `ping_interval=20` 会发 WebSocket 协议级 ping。企微网关**不实现 pong**：
库发一次协议 ping，网关之后解析帧就错了，回一个 close 1002
`incorrect masking`，连接被掐。

```
企微长连接已就绪（订阅成功）
企微长连接读取失败：ConnectionClosedError sent 1002 (protocol error) incorrect masking
```

实测对比（同一个端点）：

| 组合 | 结果 |
| --- | --- |
| 默认 keepalive（协议级 ping） | 20s 后开始异常，40s 报 keepalive ping timeout |
| `ping_interval=None` + 应用层 `{"cmd":"ping"}` | 稳定运行，`errcode:0` 应答正常 |

→ `connect(..., ping_interval=None)`，心跳改成应用层 `ping` 命令帧。

### 坑 2：接收线程里等应答 = 死等

应答帧只能由接收线程分发。如果在接收线程里发一条命令并等它的应答，
应答永远等不到自己 → 10s 超时 → 连接被判死。

→ 消息处理与心跳都放到**独立线程**；接收线程只负责「读 + 分发」。

## 四、改动清单

| 文件 | 改动 |
| --- | --- |
| `super-agent/wecom_bridge.py` | **新增**：长连接客户端 + 收消息 + 记账 + 投 router + outbox 投递 |
| `super-agent/wecom_bridge.sh` | **新增**：守护进程脚本（独立进程组；启动前清残留实例） |
| `super-agent/wecom_config.example.json` | **新增**：去标识化配置模板 |
| `super-agent/wecom_config.json` | 真实配置（**不入库**） |
| `core.py` | `resolve_session` 支持 `reply_to`（企微的回复目标是 req_id）；`add_outbox` 的 channel 从任务来源继承 `platform` |
| `sa.py` | `apply_decision` / `outbox add` 的 channel 跟着来源走，不再写死 `feishu` |
| `lark_bridge.py` | 投递器只取 `channel=feishu`；遇到别的渠道是**跳过**（原来直接标 cancelled，会把企微回话静默吃掉） |
| `router.py` | `verify_user_event` 支持 `source=wecom`（也要过账本核验） |
| `selftest.py` | 新增 `[26i]` 14 条断言 → **317 / 317** |

## 五、验证

- 沙箱演练（`SA_ROOT` 指临时目录）：白名单为空 → 不受理并打出 userid；
  配好白名单 → 受理 → 记账 → 投 router（`source_ref` 带 `platform=wecom` / `reply_to=req_id`）；
  重复消息去重；群里不 @ 不受理；@ 前缀剥离正确。
- 渠道分流：任务来源 `platform=wecom` → outbox 落 `channel=wecom`，
  飞书投递器取 0 条 / 企微投递器取 2 条；飞书投递器处理企微记录后状态仍是 `pending`（没作废）。
- 线上：长连接订阅成功；关掉库心跳 + 应用层心跳后，连接稳定运行 90s+ 无掉线。
