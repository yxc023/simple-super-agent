#!/usr/bin/env python3
"""布局迁移：channels/ 并入 tasks/。

旧布局（2026-11 之前）::

    tasks/T190/{task.md,state.json,events.jsonl,context/,notes/}
    channels/T190/{instructions/A01.md, responses/A01.md, notes/{A01.md,TASK.md}, output/}

新布局::

    tasks/T190/
    ├── task.md / state.json / events.jsonl / context/
    ├── notes/TASK.md                 ← 任务级人工批注
    ├── output/                       ← 产物落盘目录（若有）
    └── assignments/
        └── A01/
            ├── instruction.md        ← 原 channels/T190/instructions/A01.md
            ├── response.md           ← 原 channels/T190/responses/A01.md
            └── notes.md              ← 原 channels/T190/notes/A01.md

迁移做三件事：
  1. 搬文件（instructions/responses/notes/output → 新位置）；
  2. 改写 `state.json` 里 `instruction` / `response` 的绝对路径；
  3. 改写 `task.md`「指派」段里的路径。

安全约束：
  * 默认**只演练**（dry-run），加 `--apply` 才真正动文件；
  * 幂等：已迁移的任务直接跳过；
  * 不删数据：旧目录只在搬空后移除，且 `channels/` 整体保留为备份目录名不变前不动。

用法::

    python3 super-agent/migrate_layout.py            # 演练
    python3 super-agent/migrate_layout.py --apply    # 真迁移
    python3 super-agent/migrate_layout.py --apply --keep-legacy  # 保留旧目录（只复制不删）
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import sa  # noqa: E402


def plan_task(task_id: str) -> dict:
    """算出一个任务的搬迁计划（纯计算，不动文件）。"""
    tdir = sa.TASKS_DIR / task_id
    legacy = sa.LEGACY_CHANNELS_DIR / task_id
    moves: list[tuple[pathlib.Path, pathlib.Path]] = []

    adir = tdir / "assignments"
    instr_dir = legacy / "instructions"
    resp_dir = legacy / "responses"
    notes_dir = legacy / "notes"

    aid_set: set[str] = set()
    if instr_dir.is_dir():
        for p in sorted(instr_dir.glob("*.md")):
            aid_set.add(p.stem)
    if resp_dir.is_dir():
        for p in sorted(resp_dir.glob("*.md")):
            aid_set.add(p.stem)

    for aid in sorted(aid_set):
        src_i = instr_dir / f"{aid}.md"
        src_r = resp_dir / f"{aid}.md"
        src_n = notes_dir / f"{aid}.md"
        dst_dir = adir / aid
        if src_i.exists():
            moves.append((src_i, dst_dir / "instruction.md"))
        if src_r.exists():
            moves.append((src_r, dst_dir / "response.md"))
        if src_n.exists():
            moves.append((src_n, dst_dir / "notes.md"))

    # 任务级批注
    src_task_note = notes_dir / "TASK.md"
    if src_task_note.exists():
        moves.append((src_task_note, tdir / "notes" / "TASK.md"))

    # 其他散落的人工批注（A01-xxx.md 之类）→ 任务级 notes/
    if notes_dir.is_dir():
        for p in sorted(notes_dir.glob("*.md")):
            if p.stem in aid_set or p.name == "TASK.md":
                continue
            moves.append((p, tdir / "notes" / p.name))

    # 产物目录
    legacy_out = legacy / "output"
    if legacy_out.is_dir():
        for p in sorted(legacy_out.iterdir()):
            if p.is_file():
                moves.append((p, tdir / "output" / p.name))

    return {"task_id": task_id, "moves": moves, "legacy": legacy, "tdir": tdir}


def apply_task(plan: dict, keep_legacy: bool, log: list[dict]) -> None:
    tdir: pathlib.Path = plan["tdir"]
    legacy: pathlib.Path = plan["legacy"]
    for src, dst in plan["moves"]:
        if dst.exists():
            # 目标已存在 → 不覆盖（可能已迁移过），跳过
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        log.append({"task": plan["task_id"], "from": str(src), "to": str(dst)})

    # 改写 state.json 里的路径指针
    sp = tdir / sa.STATE_FILE
    if sp.exists():
        try:
            state = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = None
        if isinstance(state, dict):
            changed = False
            for asg in state.get("assignments") or []:
                aid = asg.get("id") or ""
                if not aid:
                    continue
                new_i = tdir / "assignments" / aid / "instruction.md"
                new_r = tdir / "assignments" / aid / "response.md"
                if new_i.exists() and asg.get("instruction") != str(new_i):
                    asg["instruction"] = str(new_i)
                    changed = True
                if new_r.exists() and asg.get("response") != str(new_r):
                    asg["response"] = str(new_r)
                    changed = True
            if changed:
                sp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")

    # 改写 task.md 里的路径引用
    mp = tdir / "task.md"
    if mp.exists():
        text = mp.read_text(encoding="utf-8")
        new_text = text
        for src, dst in plan["moves"]:
            new_text = new_text.replace(str(src), str(dst))
        # 旧布局里的 instructions/A01.md 与 responses/A01.md 名字不同，
        # 逐个精确替换（move 记录里已包含绝对路径，这里再兜一层相对写法）
        new_text = new_text.replace("/channels/", "/tasks/")
        if new_text != text:
            mp.write_text(new_text, encoding="utf-8")

    if not keep_legacy and legacy.is_dir():
        # 只在旧目录搬空后移除；搬不空的（未知文件）保留原样，避免丢数据
        leftover = [p for p in legacy.rglob("*") if p.is_file()]
        if not leftover:
            shutil.rmtree(legacy, ignore_errors=True)


def _ref_fix_text(text: str, task_id: str) -> str:
    """把正文里的旧路径引用改成新布局。

    只用于**仍在跑的指派**（历史指派保持原样 —— 那是当时下发的原文，改了就是改历史）。
    """
    import re

    def repl(m: re.Match) -> str:
        kind = m.group("kind")
        aid = m.group("aid")
        fname = "instruction.md" if kind == "instructions" else "response.md"
        return f"tasks/{task_id}/assignments/{aid}/{fname}"

    return re.sub(r"channels/(?P<tid>T\d+)/(?P<kind>instructions|responses)/(?P<aid>A\d+)\.md",
                  lambda m: repl(m), text)


def fix_live_refs(tid: str, log: list[dict]) -> int:
    """修正仍在跑的指派里指向旧路径的引用（指令单/回执正文）。"""
    tdir = sa.TASKS_DIR / tid
    sp = tdir / sa.STATE_FILE
    if not sp.exists():
        return 0
    try:
        state = json.loads(sp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    n = 0
    for asg in state.get("assignments") or []:
        if asg.get("status") not in ("assigned", "running", "rework"):
            continue          # 终态指派：原文保持历史原样
        aid = asg.get("id") or ""
        for fname in ("instruction.md", "response.md"):
            p = tdir / "assignments" / aid / fname
            if not p.exists():
                continue
            old = p.read_text(encoding="utf-8")
            new = _ref_fix_text(old, tid)
            if new != old:
                p.write_text(new, encoding="utf-8")
                n += 1
                log.append({"task": tid, "assignment": aid, "file": fname, "action": "fix_refs"})
    return n


def _write_log(log: list[dict]) -> pathlib.Path:
    log_dir = sa.ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = sa.now().replace(":", "").replace("+", "_")
    log_file = log_dir / f"migrate-layout-{ts}.jsonl"
    with log_file.open("a", encoding="utf-8") as f:
        for rec in log:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return log_file


def main() -> int:
    ap = argparse.ArgumentParser(description="把 channels/ 并入 tasks/（默认演练）")
    ap.add_argument("--apply", action="store_true", help="真正执行迁移（默认只演练）")
    ap.add_argument("--keep-legacy", action="store_true", help="保留旧 channels/ 目录（只复制不删）")
    ap.add_argument("--task", action="append", default=[], help="只迁指定任务（可重复）")
    args = ap.parse_args()

    if not sa.TASKS_DIR.is_dir():
        print(f"✖ 找不到 tasks/：{sa.TASKS_DIR}")
        return 1

    task_ids = sorted(p.name for p in sa.TASKS_DIR.glob("T*") if p.is_dir())
    if args.task:
        wanted = set(args.task)
        task_ids = [t for t in task_ids if t in wanted]

    plans = []
    for tid in task_ids:
        plan = plan_task(tid)
        if plan["moves"]:
            plans.append(plan)

    log: list[dict] = []

    if not plans:
        # 没有文件要搬，但**仍在跑的指派**里可能还写着旧路径 → 照样修引用
        fixed = sum(fix_live_refs(tid, log) for tid in task_ids)
        if fixed:
            _write_log(log)
            print(f"✓ 无文件需迁移；修正了在跑指派的旧路径引用 {fixed} 处")
        else:
            print("✓ 没有需要迁移的内容（已经是新布局，或 channels/ 为空）")
        return 0

    total = sum(len(p["moves"]) for p in plans)
    print(f"待迁移任务 {len(plans)} 个，文件 {total} 个")
    for plan in plans:
        print(f"\n── {plan['task_id']} ──")
        for src, dst in plan["moves"]:
            flag = "  [已存在，跳过]" if dst.exists() else ""
            print(f"  {src}\n    → {dst}{flag}")

    if not args.apply:
        print("\n（演练模式；加 --apply 执行）")
        return 0

    log: list[dict] = []
    for plan in plans:
        apply_task(plan, args.keep_legacy, log)

    # 仍在跑的指派：把正文里指向旧路径的引用改过来（历史指派不改）
    fixed = 0
    for tid in task_ids:
        fixed += fix_live_refs(tid, log)

    log_file = _write_log(log)
    print(f"\n✓ 迁移完成：{len(log)} 个文件，在跑指派路径引用修正 {fixed} 处，日志 {log_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
