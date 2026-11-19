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
