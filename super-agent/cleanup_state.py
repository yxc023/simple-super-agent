#!/usr/bin/env python3
"""历史脏数据清理 —— 把被**迟到回执覆盖**的指派状态回滚到权威值。

背景：`sa.py cmd_respond` 曾经无条件 `asg["status"] = status`，
一个迟到的回执（worker 崩了之后又写了一份回执、或两个 worker 并发跑同一指派）
会把已经终结的指派改回去 —— `done→failed`、`cancelled→done`、`approved→cancelled`。
根因已修（终态指派收到迟到回执只记 `report_stale`，不改状态），
但**历史任务里的脏状态还在** —— 不清掉，不变式校验永远是红的，
看板上那个红点会让人麻木。

原则：
  · **只改当前状态**（state.json），**不改事件历史** —— 事件流是 append-only 的
    审计链，改它等于毁掉证据链。修正时**追加**一条 `state_corrected` 事件，
    写清改了什么、依据是什么。
  · 权威状态由**合法迁移表重放**得出（`invariants.replay_assignments`），
    非法的那一步不改变状态 —— 重放逻辑与校验器共用一份，不会漂。
  · **默认 dry-run**，加 `--apply` 才真正写。

用法：
    python3 super-agent/cleanup_state.py            # 只看会改什么
    python3 super-agent/cleanup_state.py --apply    # 真改
    python3 super-agent/cleanup_state.py --task T001
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import invariants as inv  # noqa: E402


def artifacts_from_response(path: str) -> list[str]:
    """从回执文件的「## 产出」段里抓**真正的交付物**绝对路径。

    与 worker_runner 同源，但多一道过滤 —— 回执里常出现这种句子：
        「无（未落盘任何交付文件）；本回执仅为 `.../responses/A03.md`」
    这里的回执路径**不是交付物**，把它登记成 artifact 等于凭空造证据。
    所以排除：回执自身、指派单、以及 channels/ 下的基础设施路径。
    """
    out: list[str] = []
    p = pathlib.Path(path)
    try:
        body = p.read_text(encoding="utf-8")
    except OSError:
        return out
    m = re.search(r"##\s*产出\s*\n(.*?)(?:\n##|\Z)", body, re.S)
    if not m:
        return out
    for line in m.group(1).splitlines():
        for cand in re.findall(r"`([^`\n]+)`", line):
            cand = cand.strip()
            if not cand.startswith("/") or cand in out:
                continue
            if cand == str(p):
                continue                                  # 回执自己
            if _is_infra_path(cand):
                continue                                  # 基础设施路径，不是交付物
            if not pathlib.Path(cand).exists():
                continue                                  # 产物已被清理 → 不当证据
            out.append(cand)
    return out


def _is_infra_path(cand: str) -> bool:
    """判断候选产物是不是「框架自身的基础设施路径」（不是交付物）。

    不能靠 `/super-agent/` 这种目录名子串判断 —— 仓库本身就可能叫 super-agent，
    结果会把自己的交付物也当成基础设施丢掉。改成按**路径成分**判定。
    """
    parts = pathlib.Path(cand).parts
    infra = {"channels", "outbox", "inbox", "tasks", "router", "z-archive", "logs"}
    return bool(infra.intersection(parts))


def plan(task_filter: str | None = None) -> list[dict]:
    """算出每个需要修正的指派（不写任何文件）。"""
    changes: list[dict] = []
    for tid in sorted(p.name for p in inv.TASKS_DIR.glob("T*")):
        if task_filter and tid != task_filter:
            continue
        state_path = inv.TASKS_DIR / tid / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        replay = inv.replay_assignments(tid)
        for a in state.get("assignments") or []:
            aid = a.get("id")
            rec = replay.get(aid) or {}
            auth = rec.get("status")
            cur = a.get("status")
            dup_starts = len(rec.get("starts") or [])
            item = {"task": tid, "assignment": aid, "current": cur, "authoritative": auth,
                    "fix_status": bool(auth and cur != auth),
                    "completed_at": rec.get("ts") or "",
                    "duplicate_starts": dup_starts,
                    "violations": rec.get("violations") or [],
                    "artifacts_now": a.get("artifacts") or [],
                    "response": a.get("response") or ""}
            # 产物被迟到回执清空 → 从回执文件里恢复
            if item["fix_status"] or not item["artifacts_now"]:
                restored = artifacts_from_response(item["response"])
                if restored and restored != item["artifacts_now"]:
                    item["artifacts_restore"] = restored
            changes.append(item)
    return changes


def apply_changes(changes: list[dict]) -> int:
    import sa
    fixed = 0
    by_task: dict[str, list[dict]] = {}
    for c in changes:
        if c["fix_status"] or c.get("artifacts_restore"):
            by_task.setdefault(c["task"], []).append(c)
    for tid, items in by_task.items():
        state = sa.load_state(tid)
        for c in items:
            for a in state.get("assignments") or []:
                if a.get("id") != c["assignment"]:
                    continue
                before = a.get("status")
                if c["fix_status"]:
                    a["status"] = c["authoritative"]
                    a["completed_at"] = c["completed_at"] or a.get("completed_at")
                    # 不要凭空写 review="approved"：人工放行是**人的动作**，
                    # 事件重放只能证明「回执已完成」，不能替代人工复核。
                    # 只在原本就有 review 值时保留它，不新造。
                    if c["authoritative"] == "cancelled":
                        a["review"] = "cancelled"
                if c.get("artifacts_restore"):
                    a["artifacts"] = c["artifacts_restore"]
                detail = (f"状态 {before} → {a.get('status')}"
                          + (f"；产物恢复 {len(c['artifacts_restore'])} 个"
                             if c.get("artifacts_restore") else ""))
                sa.log_event(tid, "state_corrected", "cleanup",
                              f"{c['assignment']} 历史脏数据修正：{detail}"
                              f"（依据：事件重放，迟到回执不得覆盖终态）",
                              assignment=c["assignment"], from_status=before,
                              to_status=a.get("status"))
                fixed += 1
        state["status"] = sa.recompute_task_status(state)
        sa.save_state(tid, state)
    return fixed


def main() -> int:
    ap = argparse.ArgumentParser(description="清理被迟到回执覆盖的历史脏状态")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只报告）")
    ap.add_argument("--task", help="只处理某个任务")
    a = ap.parse_args()

    changes = plan(a.task)
    need = [c for c in changes if c["fix_status"] or c.get("artifacts_restore")]
    dup = [c for c in changes if c["duplicate_starts"] > 1]

    print("=" * 68)
    print("历史脏数据清理" + ("（DRY-RUN，不写文件）" if not a.apply else "（--apply）"))
    print("=" * 68)
    print(f"  扫描指派 {len(changes)} 个 · 需要修正 {len(need)} 个 · 重复启动 {len(dup)} 个")
    if not need and not dup:
        print("✅ 没有需要清理的脏数据")
        return 0

    if need:
        print("\n需要修正的状态：")
        for c in need:
            mark = "状态" if c["fix_status"] else "产物"
            extra = ""
            if c.get("artifacts_restore"):
                extra = f"  产物 {len(c['artifacts_now'])} → {len(c['artifacts_restore'])} 个"
            print(f"  · {c['task']}/{c['assignment']}: {c['current']} → {c['authoritative']}"
                  f"   [{mark}]{extra}")
            for v in c["violations"][:3]:
                print(f"      依据: 非法迁移 {v.get('from')} → {v.get('to')}"
                      f"（{v.get('event')} @ {str(v.get('ts'))[:19]}）")
    if dup:
        print("\n重复启动（历史事实，无法撤销，只标注）：")
        for c in dup:
            print(f"  · {c['task']}/{c['assignment']}: 被启动 {c['duplicate_starts']} 次"
                  f"（当前状态 {c['current']}，无需改状态）")

    if not a.apply:
        print("\n加 --apply 执行修正。")
        return 0
    n = apply_changes(changes)
    print(f"\n✅ 已修正 {n} 个指派（每个都追加了一条 state_corrected 事件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
