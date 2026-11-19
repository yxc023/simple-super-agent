# 配置与本地化

框架本体不含任何个人数据。**真实配置永远不进版本库**，仓库里只有去标识化的模板。

## 三个配置文件

| 模板 | 运行时文件 | 作用 | 缺失后果 |
| --- | --- | --- | --- |
| `super-agent/lark_config.example.json` | `super-agent/lark_config.json` | 飞书机器人接入：app_id、bot、允许的会话、触发规则、表情、轮询参数 | 桥接不可用 |
| `super-agent/policy.example.json` | `super-agent/policy.json` | 准入策略：owner、允许发起命令的群、人员目录、临时授权有效期 | 无策略时按旧行为放行（见下） |
| `super-agent/registry.example.json` | `super-agent/registry.json` | **worker 名册唯一真源**：agent_id、能力、只读与否、并发上限、`trigger_gate` | 任何派工都会被拒（不在册） |

初始化：

```bash
cp super-agent/lark_config.example.json super-agent/lark_config.json
cp super-agent/policy.example.json      super-agent/policy.json
cp super-agent/registry.example.json    super-agent/registry.json
```

需要替换的占位符：

- `cli_<app_id>`：飞书应用 app id
- `ou_<open_id>`：你的 open id（owner / bot / 授权人）
- `oc_<chat_id>`：允许发起命令的群 id

### policy.json 的字段

```jsonc
{
  "owner":       { "open_id": "ou_...", "name": "你的名字" },
  "private_chat": { "owner_only": true },      // 私聊是否只允许 owner
  "groups":      { "allow": ["oc_..."] },       // 允许发起命令的群
  "people":      { "你的名字": "ou_..." },       // 人员目录（临时授权用）
  "grant_ttl_hours": 48                          // 临时授权有效期
}
```

判定顺序（`policy.check_command`）：

1. 没配策略 → 全部放行（避免配错就全线瘫痪）；
2. owner 本人 → 任何会话放行；
3. 任务级临时授权 → 放行，且**必须落在该任务自己的会话里**；
4. 私聊 → `private_chat.owner_only` 为真时只允许 owner；
5. 群聊 → 必须在 `groups.allow` 里；
6. 其余一律拒绝。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `SA_ROOT` | **状态根**。不设 = 生产（代码目录）；指到临时目录即为沙箱演练，`tasks/` `channels/` `outbox/` 都不会写进生产 |
| `SA_TASKS_DIR` / `SA_CHANNELS_DIR` / `SA_INBOX_DIR` / `SA_OUTBOX_DIR` | 单个状态目录覆盖（优先级高于 `SA_ROOT`） |
| `SA_POLICY_FILE` | 策略文件路径覆盖（自检与演练用） |
| `SA_ACTOR` | 调用者身份（如 `router`）。router 进程不允许自行新建任务 |
| `BLOG_REPO` / `BLOG_NEWS_ROOT` | `scripts/` 下两个博客辅助脚本的默认路径 |

## 改动配置后

| 改了什么 | 要做什么 |
| --- | --- |
| `registry.json` | 重启 router（`python3 super-agent/router.py rotate`）与 worker runner；长驻进程在 import 时快照了名册 |
| `skill/router-system.md` | `python3 super-agent/sync_skill.py` 重新生成 skill，再重启 router |
| `policy.json` | 立即生效（每次判定都读盘） |
| `lark_config.json` | 重启桥接：`./super-agent/lark_bridge.sh stop && ./super-agent/lark_bridge.sh start` |

改完跑一遍自检：

```bash
python3 super-agent/selftest.py
python3 super-agent/sync_skill.py --check    # 生成物与同步目标是否一致
```

## 哪些东西不入库

| 路径 | 原因 |
| --- | --- |
| `super-agent/tasks/` `channels/` `outbox/` `inbox/` `router/` | 运行期账本：真实任务、指派、消息正文、回执 |
| `super-agent/lark_state.json` | 飞书消息去重状态，含消息 id 与内容片段 |
| `super-agent/lark_config.json` `policy.json` `registry.json` | app_id / open_id / 群 id / 人员目录 |
| `logs/` | 个人工作日志，含会话 id、群 id、同事姓名 |

需要留痕：任务与回执都在 `super-agent/tasks/<任务号>/` 下，可以直接归档；
外发消息的审计链在 `super-agent/router/events.jsonl` 与 `outbox/` 里。
