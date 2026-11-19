#!/usr/bin/env python3
"""灌入一批演示数据，让看板有东西可看。

    python3 super-agent/seed_demo.py            # 灌演示数据
    python3 super-agent/seed_demo.py --clear    # 清空所有任务

演示数据走的是与真实运行完全相同的协议（同样的 state.json / channels 文件），
所以在看板上的每一个按钮都能真实生效。清空用 --clear。
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import sa  # noqa: E402


def clear() -> int:
    """清空 tasks/ 与 channels/ 下的所有目录。

    ⚠️ 破坏性操作：这里删的是**生产状态根**下的真实任务与指派，
    所以必须显式加 --yes 才会执行。
    """
    n = 0
    for base in (sa.TASKS_DIR, sa.CHANNELS_DIR):
        if not base.exists():
            continue
        for d in base.iterdir():
            if d.is_dir():
                shutil.rmtree(d)
                n += 1
    for base in (sa.TASKS_DIR, sa.CHANNELS_DIR):
        base.mkdir(parents=True, exist_ok=True)
        (base / ".gitkeep").touch()
    return n


def write_task(**kw) -> str:
    """直接落一个任务（绕过 CLI 参数拼接，便于写长文本）。"""
    task_id = kw["task_id"]
    d = sa.task_dir(task_id)
    (d / "context").mkdir(parents=True, exist_ok=True)
    (d / "notes").mkdir(parents=True, exist_ok=True)
    for sub in ("instructions", "responses", "notes"):
        (sa.CHANNELS_DIR / task_id / sub).mkdir(parents=True, exist_ok=True)

    ctx = []
    for i, (name, body) in enumerate(kw.get("context", []), start=1):
        p = d / "context" / f"{i:02d}-{name}"
        p.write_text(body, encoding="utf-8")
        ctx.append(str(p))

    state = {
        "task_id": task_id,
        "title": kw["title"],
        "goal": kw["goal"],
        "status": "open",
        "created_at": kw["created_at"],
        "updated_at": kw["updated_at"],
        "context_files": ctx,
        "acceptance_lines": kw.get("acceptance", []),
        "task_notes": kw.get("task_notes", ""),
        "assignments": [],
    }
    sa.save_state(task_id, state)
    sa.log_event(task_id, "task_created", "human", f"任务已建立：{kw['title']}")
    return task_id


def write_asg(task_id: str, aid: str, worker: str, brief: str, instruction: str, **kw) -> None:
    state = sa.load_state(task_id)
    instr = sa.CHANNELS_DIR / task_id / "instructions" / f"{aid}.md"
    instr.write_text(instruction, encoding="utf-8")
    resp = sa.CHANNELS_DIR / task_id / "responses" / f"{aid}.md"
    resp.write_text(
        f"# 回执 {aid}\n\n- assignment: `{aid}`\n- worker: `{worker}`\n"
        f"- status: `pending`\n- updated_at: {kw.get('at', sa.now())}\n\n## 结论\n\n（待 worker 填写）\n",
        encoding="utf-8",
    )
    state["assignments"].append(
        {
            "id": aid,
            "worker": worker,
            "status": "assigned",
            "instruction": str(instr),
            "response": str(resp),
            "brief": brief,
            "dispatched_at": kw.get("at", sa.now()),
            "completed_at": None,
            "pending_ack": False,
            "review": None,
        }
    )
    state["status"] = sa.recompute_task_status(state)
    sa.save_state(task_id, state)
    sa.log_event(task_id, "assigned", "super-agent", f"{aid} 派给 {worker}", assignment=aid, worker=worker)


def seed() -> None:
    clear()

    # ---------------------------------------------------------- T001
    write_task(
        task_id="T001",
        title="整理 2026 Q3 商户平台需求清单",
        goal="从需求仓导出 Q3 全部需求，按状态与负责人归类，输出 Markdown 表格，附总数校验",
        created_at="2026-11-17T09:12:00+08:00",
        updated_at="2026-11-17T15:40:00+08:00",
        context=[
            ("需求仓口径.md", "# 口径\n\n- 时间窗：创建时间落在 2026-07-01 ~ 2026-09-30\n- 排除：已作废、已归档\n- 导出接口：/api/req/export?start=&end=&page=\n"),
            ("字段说明.md", "# 字段\n\n| 字段 | 含义 |\n| --- | --- |\n| reqNo | 需求编号 |\n| title | 标题 |\n| status | 状态 |\n| owner | 负责人 |\n| createdAt | 创建时间 |\n"),
        ],
        acceptance=[
            "表格含 需求编号/标题/状态/负责人/创建时间 五列，按状态分组",
            "表格总数与接口返回 total 一致",
            "重复需求已按 reqNo 去重",
        ],
    )
    write_asg(
        "T001", "A01", "explore", "先摸清需求仓数据结构",
        """# 指派单 A01

## 任务目标
确认需求仓导出接口的路径、参数、分页方式，并列出返回字段含义。

## 背景 / 上下文
- 需求仓：https://example.internal/requirements
- 导出接口初步判断在 /api/req/export

## 具体要求
1. 确认接口完整路径与全部查询参数（时间范围、分页、过滤）。
2. 确认单页最大条数，是否需要翻页。
3. 列出返回字段及含义，标出哪些字段在表格里要用。

## 边界
- 只读，不要触发任何写接口。
- 不要修改 channels/ 与 tasks/ 之外的业务文件。

## 回执要求
写到 /abs/channels/T001/responses/A01.md，包含 status / 结论 / 产出绝对路径。
""",
        at="2026-11-17T09:13:00+08:00",
    )
    write_asg(
        "T001", "A02", "general-purpose", "按口径导出并出表",
        """# 指派单 A02

## 任务目标
按口径导出 Q3 需求并产出 Markdown 表格。

## 输入材料
- /abs/tasks/T001/context/01-需求仓口径.md
- /abs/tasks/T001/context/02-字段说明.md

## 具体要求
1. 调用导出接口，按时间窗 2026-07-01 ~ 2026-09-30 拉全量。
2. 按 reqNo 去重，排除已作废/已归档。
3. 输出 Markdown 表格，按状态分组，附总数校验行。

## 验收标准
- [ ] 表格五列齐全且按状态分组
- [ ] 总数与接口 total 一致，校验行写明两个数字

## 边界
- 不要改需求仓里的任何数据。
- 不要替 super agent 做汇总。

## 回执要求
写到 /abs/channels/T001/responses/A02.md，包含 status / 结论 / 产出绝对路径。
""",
        at="2026-11-17T14:05:00+08:00",
    )
    sa.intervene("T001", "A01", "comment", "分页上限是 50 条/页，导出时必须按页合并再按 reqNo 去重，别只取第一页。")
    sa.intervene("T001", "A02", "reject", "表格里缺了「需求类型」这一列，口径里写了要按类型分组，去重后总数比接口 total 少 3 条，说明漏了一页。")

    # ---------------------------------------------------------- T002
    write_task(
        task_id="T002",
        title="商户入驻流程卡在「资质核验」排查",
        goal="定位商户入驻流程卡在资质核验环节的根因，给出修复方案",
        created_at="2026-11-17T11:30:00+08:00",
        updated_at="2026-11-17T16:05:00+08:00",
        context=[
            ("现象.md", "# 现象\n\n- 3 个商户卡在资质核验，最长 4 小时无进展\n- 核验任务无失败记录，一直 running\n- 核验服务日志无对应请求\n"),
        ],
        acceptance=[
            "给出根因（哪一层、哪个条件分支）",
            "给出修复方案与影响面",
            "指明是否需要数据补偿",
        ],
    )
    write_asg(
        "T002", "A01", "explore", "定位资质核验的调用链",
        """# 指派单 A01

## 任务目标
梳理商户入驻资质核验的调用链，定位在哪一层可能吞掉请求。

## 具体要求
1. 找到资质核验的触发点（流程节点 / 消息消费）。
2. 追到实际发起核验请求的那一层，看异常是否被 catch 后吞掉。
3. 列出所有"可能不抛异常但也不推进"的分支。

## 边界
- 只读，不要修改代码。

## 回执要求
写到 /abs/channels/T002/responses/A01.md。
""",
        at="2026-11-17T11:32:00+08:00",
    )
    write_asg(
        "T002", "A02", "plan", "给出修复方案",
        """# 指派单 A02

## 任务目标
基于 A01 的结论给出修复方案。

## 边界
- 只出方案，不改代码。

## 回执要求
写到 /abs/channels/T002/responses/A02.md。
""",
        at="2026-11-17T13:20:00+08:00",
    )

    # ---------------------------------------------------------- T003
    write_task(
        task_id="T003",
        title="2026-11-17 每日早读报告发布",
        goal="采集今日 AI 领域要闻，产出富 HTML 报告并发布到博客",
        created_at="2026-11-17T07:00:00+08:00",
        updated_at="2026-11-17T08:20:00+08:00",
        context=[("早读规范.md", "# 早读规范\n\n- 触发：仅owner 本人 1:1 私聊\n- 流程：采集 → 飞书选题 → 深挖 → 富 HTML → 发布 → 回执\n- 线上路径：https://blog.example.com/news/2026/11/17/\n")],
        acceptance=["线上路径可访问", "报告含 5 条以上要闻且每条带来源链接"],
    )
    write_asg(
        "T003", "A01", "general-purpose", "采集 + 产出 + 发布",
        """# 指派单 A01

## 任务目标
完成今日早读报告并发布。

## 具体要求
1. 采集 AI 领域要闻，给出候选选题。
2. 选定后深挖，产出富 HTML。
3. 发布到 https://blog.example.com/news/2026/11/17/ 并回执线上链接。

## 回执要求
写到 /abs/channels/T003/responses/A01.md，必须包含线上 URL。
""",
        at="2026-11-17T07:02:00+08:00",
    )

    # ---------------------------------------------------------- T004
    write_task(
        task_id="T004",
        title="沉淀「商户需求仓」查询指引",
        goal="把需求仓的查询口径、常用筛选、字段含义写成一份团队可复用的指引",
        created_at="2026-11-17T16:10:00+08:00",
        updated_at="2026-11-17T16:10:00+08:00",
        task_notes="先别急着派，等 Q3 清单跑完再沉淀，避免把临时口径固化成规范。",
        context=[],
        acceptance=["包含口径、常用筛选、字段表", "至少 3 个真实查询示例"],
    )

    # 回填各指派状态（走协议层，事件会一起落盘）
    sa.intervene("T001", "A01", "approve", "字段说明可用，采纳。")
    sa.intervene("T001", "A02", "comment", "重做时请把漏掉的那一页也补上，并在校验行写明两个总数。")
    sa.intervene("T002", "A01", "comment", "再确认一下消息消费侧有没有重试队列吞掉消息。")
    sa.intervene("T003", "A01", "approve", "已发布，链接有效。")

    # 模拟 worker 回执（直接改回执文件 + 登记）
    def fake_respond(task_id: str, aid: str, status: str, summary: str, artifacts: list[str]):
        state = sa.load_state(task_id)
        asg = next(x for x in state["assignments"] if x["id"] == aid)
        resp = pathlib.Path(asg["response"])
        body = [
            f"# 回执 {aid}", "",
            f"- assignment: `{aid}`",
            f"- worker: `{asg['worker']}`",
            f"- task: `{task_id}`",
            f"- status: `{status}`",
            f"- updated_at: {sa.now()}", "",
            "## 结论", "", summary, "", "## 产出", "",
        ]
        body += [f"- `{p}`" for p in artifacts] or ["- （无文件产出）"]
        resp.write_text("\n".join(body) + "\n", encoding="utf-8")
        asg["status"] = status
        asg["completed_at"] = sa.now()
        if status == "done":
            asg["review"] = "pending"
        state["status"] = sa.recompute_task_status(state)
        sa.save_state(task_id, state)
        sa.log_event(task_id, "responded", "worker", f"{aid} 回执：{status}", assignment=aid, status=status)

    fake_respond("T001", "A01", "done",
                 "导出接口为 /api/req/export?start=&end=&page=，单页上限 50 条，需翻页；返回 reqNo/title/status/owner/createdAt/type 六个字段。",
                 ["/abs/output/需求仓接口说明.md"])
    fake_respond("T001", "A02", "rework",
                 "第一版表格缺「需求类型」列，且去重后比接口 total 少 3 条——分页只取了前两页。按人工意见重做。",
                 ["/abs/output/Q3需求清单-v1.md"])
    fake_respond("T002", "A01", "done",
                 "资质核验由流程节点触发，走 MQ 消费；消费侧 catch 住异常后只打 warn 日志，不重试也不置失败，导致任务永远 running。",
                 ["/abs/output/资质核验调用链.md"])
    fake_respond("T002", "A02", "blocked",
                 "修复方案依赖 A01 的具体分支结论，但 A01 只覆盖了 MQ 消费侧；同步调用那条路径还没查，缺了它无法判断是否两条路径都要改。需要确认要不要一起排查。",
                 [])
    fake_respond("T003", "A01", "done",
                 "采集 9 条候选要闻，选中 4 条深挖，产出富 HTML 并发布。",
                 ["https://blog.example.com/news/2026/11/17/"])

    # T001 A01 已放行 → approved；T003 A01 已放行 → approved
    for t, a in (("T001", "A01"), ("T003", "A01")):
        state = sa.load_state(t)
        for x in state["assignments"]:
            if x["id"] == a:
                x["status"] = "approved"
                x["review"] = "approved"
        state["status"] = sa.recompute_task_status(state)
        sa.save_state(t, state)
        sa.log_event(t, "approved", "human", f"{a} 人工放行", assignment=a)

    print("演示数据已灌入：T001 / T002 / T003 / T004")
    print("看板：http://127.0.0.1:8788")
    print("清空：python3 super-agent/seed_demo.py --clear")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clear", action="store_true",
                    help="清空 tasks/ 与 channels/ 下的所有任务（破坏性，需配合 --yes）")
    ap.add_argument("--yes", action="store_true", help="确认执行破坏性操作")
    a = ap.parse_args()
    if a.clear:
        if not a.yes:
            print("✖ --clear 会删除 tasks/ 与 channels/ 下的全部任务。"
                  "确认请加 --yes（建议先备份状态根）。", file=sys.stderr)
            raise SystemExit(2)
        n = clear()
        print(f"已清空 {n} 个任务")
    else:
        seed()


if __name__ == "__main__":
    main()
