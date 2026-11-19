---
name: super-agent
description: >-
  Super Agent 调度层（router）：一个**基于文件存储**做**任务管理与分发**的 super agent。
  它接收指令、把「指令 + 上下文」拆成可独立执行的指派交给 worker agent，自己不执行任务本体。
  任务、指派、回执、事件、外发消息全是文件 —— 文件就是状态机，没有数据库。
  它的产出是**结构化 Decision**（由 Task Center 校验后落地），而不是直接改状态。
  收到 worker 回执后，由 router 判断是回消息给用户、还是再派一个 worker。
  用户说「派给 worker」「拆解任务给 agent」「super agent」「router」「只做调度不执行」时使用。
  协议与目录约定见 super-agent/README.md。
---
