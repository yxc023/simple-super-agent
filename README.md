# simple-super-agent

一个**基于文件存储**做**任务管理与分发**的 super agent。

- 架构与协议的完整说明： [`super-agent/README.md`](./super-agent/README.md)
- 配置与本地化： [`docs/configuration.md`](./docs/configuration.md)
- 本次代码整理与 review 记录： [`docs/review-and-cleanup.md`](./docs/review-and-cleanup.md)

## 它是什么

一个常驻的 **router session**（主 agent）负责接收指令、判断意图、拆成指派，
把指派交给**只执行不判断**的 worker agent；worker 的回执再由 router 核对后放行或打回。

**任务管理与分发全程基于文件存储**：没有数据库、没有常驻服务端的内存状态，
任务、指派、回执、事件、外发消息全是文件，**文件就是状态机**。

```
用户消息 ──► router（判断） ──► Task Center（校验/落地） ──► worker（执行）
                ▲                                            │
                └────────── 回执 + 人工干预 ◄────────────────┘
```

一次任务的全部状态都落在一个目录里，人能直接看、能直接改、能进 git：

```
tasks/T875/
├── task.md        # 任务定义（目标、上下文、验收标准）
├── state.json     # 机器可读状态（唯一事实源）
├── events.jsonl   # 事件流（谁在什么时候做了什么）
├── context/       # 上下文文件副本（worker 只读这里）
├── notes/         # 任务级人工批注（TASK.md）
└── assignments/
    └── A01/
        ├── instruction.md   # 指派单（派给 worker 的东西）
        ├── response.md      # 回执（worker 写回）
        └── notes.md         # 该指派的人工批注
```

因此：

| 角色 | 约束手段 |
| --- | --- |
| router | pi 扩展 `router_gate.ts` 直接掐掉能力：bash 只能调 Task Center，read 只能读任务目录，write 只能写 /tmp；**查代码、跑脚本、调外部系统一律拒绝** |
| worker | `registry.json` 是名册唯一真源：是否在册、是否启用、是否只读、并发上限，派工前由 Task Center 校验；只读 worker 由机制强制，不靠 prompt 自觉 |
| 触发闸门 | 某些 worker 带 `trigger_gate`（例如只允许 owner 私聊触发），由 Task Center 校验 Decision 里的 `requester`，闸门不符直接拒派 |
| 状态 | 全部落文件（`tasks/` `outbox/`），CLI、看板、agent 三方读写同一套文件 |

### 为什么用文件存

- **可审计**：每一次派工、放行、投递都有 `events.jsonl` 流水，出了问题能按时间线倒查。
- **可手工干预**：看板上能改的东西，都能在文件里直接改（`state.json` / `notes/`）——
  没有隐藏的内存状态要同步。
- **零基础设施**：不装数据库、不跑迁移；`git clone` 完就能跑。
- **三方同源**：router、worker、看板、CLI 读写的是同一批文件，不存在「两份状态对不上」。

## 目录结构

```
.
├── super-agent/                 # 框架本体（Python / TypeScript / Shell）
│   ├── sa.py                    # Task Center：任务的唯一写入口（CLI）
│   ├── core.py                  # 协议与校验：Decision 校验、outbox、完成策略
│   ├── policy.py                # 准入策略：谁能发起命令、临时授权
│   ├── invariants.py            # 不变式巡检（只读）
│   ├── cleanup_state.py         # 历史脏数据修正（事件重放）
│   ├── router.py                # router 常驻进程（pi session 驱动）
│   ├── router_gate.ts           # router 工具护栏（pi 扩展）
│   ├── lark_bridge.py           # 飞书 ↔ super-agent 桥接
│   ├── worker_runner.py         # worker 执行器
│   ├── serve.py + web/          # 本地看板（127.0.0.1）
│   ├── chain_view.py / trace.py # 消息链路视图 / 全链路日志
│   ├── seed_demo.py             # 演示数据
│   ├── selftest.py              # 自检（249 项）
│   ├── skill/                   # router 契约（唯一真源 router-system.md）
│   ├── templates/               # 指派单 / 上下文模板
│   ├── workers/                 # 各 worker 的角色说明（ROLE.md）
│   └── *.example.json           # 配置模板（真实配置不入库）
├── docs/                        # 业务侧文档与规格
├── scripts/                     # 辅助脚本（博客仓库相关）
└── .pi/skills/super-agent/      # Pi skill 入口（由 sync_skill.py 生成）
```

## 快速开始

```bash
# 1) 准备配置（真实配置在 .gitignore 里，只入库模板）
cp super-agent/lark_config.example.json super-agent/lark_config.json
cp super-agent/policy.example.json      super-agent/policy.json
cp super-agent/registry.example.json    super-agent/registry.json

# 2) 自检（249 项断言，覆盖协议、状态机、护栏、链路）
python3 super-agent/selftest.py

# 3) 起本地看板（只绑 127.0.0.1）
./super-agent/board.sh            # http://127.0.0.1:8788
./super-agent/board.sh stop

# 4) 命令行
python3 super-agent/sa.py list
python3 super-agent/sa.py show --task T001
python3 super-agent/sa.py tasks-list --limit 15
```

飞书对接（可选，需要本机已登录 `lark-cli`）：

```bash
./super-agent/lark_bridge.sh start     # 常驻轮询/长连接
./super-agent/lark_bridge.sh status
python3 super-agent/lark_bridge.py reply --task T001 --text "结果…"
```

## 安全边界

- **运行期状态不入库**：`tasks/` `outbox/` `inbox/` `router/` 与飞书状态文件
  都在 `.gitignore` 里 —— 它们记录真实任务与消息正文（含人名、open_id、群 id）。
- **配置不入库**：`lark_config.json` / `policy.json` / `registry.json` 只提交
  `*.example.json` 模板。
- **看板无鉴权**：只应绑 `127.0.0.1`；`/api/file` 只允许读状态目录，不允许读配置文件。
- **router 无执行能力**：护栏按 shell 词法解析命令，出现 `&& ; | $()` 一律拒绝。

## 依赖

- Python 3.11+（标准库即可，无第三方依赖）
- `pi` CLI（可选：跑 router / worker 时需要）
- `lark-cli`（可选：飞书桥接时需要）
- Node.js（可选：router 护栏是 TypeScript 扩展，由 pi 加载）

## License

未指定 license。内部/私有使用请自便；对外分发前请自行补充 LICENSE。
