# 指派单 A01

> 你是 worker。super agent 只派活、不干活；这条指派单就是你的全部输入。

## 任务目标

（一句话，动词开头，说清交付物是什么）

## 背景 / 上下文

（为什么做这件事、给谁做、在什么约束下。够 worker 判断边界即可）

## 输入材料（绝对路径，只读）

- `/abs/path/to/context/01-xxx.md`
- `/abs/path/to/context/brief.md`

## 具体要求

1. …
2. …
3. …

## 验收标准

- [ ] …
- [ ] …

## 边界（不要做这些）

- 不要修改 `channels/` 以外的任何文件
- 不要替 super agent 做汇总汇报，只做你的那一段
- 遇到歧义不要猜：写 `status: blocked`，说明卡在什么、需要什么

## 回执要求

完成后把回执写到：

```
/abs/path/to/channels/T001/responses/A01.md
```

格式：

```markdown
# 回执 A01

- assignment: `A01`
- worker: `<你的名字>`
- status: `done`   # done | failed | blocked | waiting_user_input
- updated_at: <ISO8601>

## 结论

（一句话：做了什么 + 结果是什么）

## 产出

- `/abs/path/to/产物.md`
```

**缺信息要问人时**（`status: waiting_user_input`）额外写一行 `- ask: <被问的人>`：

```markdown
- status: `waiting_user_input`
- ask: 张三
```

`- ask` 里的名字会**自动把这个人临时加进本任务的授权名单** ——
他在本任务的会话里回复 bot 时会被受理（别人不行）。
名字要写清楚（带括号的全名最好），系统按人员目录（`policy.json` 的 `people`）解析。
