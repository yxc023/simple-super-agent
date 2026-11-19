#!/usr/bin/env python3
"""不变式校验器 —— **只读**，不改任何状态。

定位：实现之外的**第三方视角**。
它**不复用** sa.py / core.py 的写路径（那套代码本身可能就是 bug 来源），
而是自己解析 `state.json` / `events.jsonl` / `outbox/*.json` / `router/inbox.jsonl`，
按一套写死的规则断言「系统该始终成立的东西」。

为什么要有它：
  · 自检（selftest.py）证明的是「我写的代码按我以为的方式跑」；
  · 不变式证明的是「**状态本身**没有自相矛盾」—— 这是别人能独立复核的东西。
  · 写它的时候顺手就会发现：现在的实现有哪些状态是脏的。

用法：
    python3 super-agent/invariants.py             # 人读报告
    python3 super-agent/invariants.py --json      # 机读（给看板 / CI）
    python3 super-agent/invariants.py --task T012 # 只看某个任务
    python3 super-agent/invariants.py --strict    # WARN 也算失败

退出码：0 = 没有 ERROR；1 = 有 ERROR（或 --strict 且有 WARN）；2 = 参数/环境错误

级别：
    ERROR —— 不变式被破坏，状态自相矛盾，必须查
    WARN  —— 不一定错，但可疑/陈旧（例如 pending 太久、产物已被清理）
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent

ERROR = "ERROR"
WARN = "WARN"

# 终态指派（不再会变化）
TERMINAL_ASSIGN = ("done", "approved", "failed", "cancelled")
# 活跃指派
ACTIVE_ASSIGN = ("assigned", "running", "rework", "waiting_user_input", "blocked")

# 合法的指派状态迁移边。来源：sa.py / worker_runner.py 实际写入的状态变更，
# 以及 events.jsonl 里能重建出来的迁移序列。
LEGAL_TRANSITIONS = {
    None: {"assigned"},
    "assigned": {"running", "done", "failed", "blocked", "waiting_user_input",
                 "cancelled", "approved", "rework"},
    # running → assigned 是合法的：worker 进程消失后 reclaim_stale 收回重派
    "running": {"done", "failed", "blocked", "waiting_user_input", "cancelled",
                "approved", "rework", "assigned"},
    "rework": {"assigned", "running", "done", "failed", "cancelled", "blocked"},
    "waiting_user_input": {"running", "assigned", "done", "failed", "cancelled",
                            "blocked", "approved"},
    "blocked": {"assigned", "running", "cancelled", "failed"},
    "done": {"approved", "rework"},
    "approved": set(),
    "failed": set(),
    "cancelled": set(),
}


# ------------------------------------------------------------------ 路径解析

def state_dir(env_name: str, default_rel: str) -> pathlib.Path:
    """和 core.py 同一套解析规则（env > SA_ROOT/<rel> > 代码目录）。"""
    raw = (os.environ.get(env_name) or "").strip()
    if raw:
        return pathlib.Path(raw).expanduser().resolve()
    base = (os.environ.get("SA_ROOT") or "").strip()
    root = pathlib.Path(base).expanduser().resolve() if base else ROOT
    return root / default_rel


TASKS_DIR = state_dir("SA_TASKS_DIR", "tasks")
OUTBOX_DIR = state_dir("SA_OUTBOX_DIR", "outbox")
ROUTER_DIR = state_dir("SA_ROUTER_DIR", "router")


# ------------------------------------------------------------------ 小工具

def read_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_jsonl(path: pathlib.Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                out.append(d)
    except OSError:
        pass
    return out


def parse_ts(v) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def now() -> datetime:
    return datetime.now().astimezone()


def age_minutes(ts) -> float | None:
    t = parse_ts(ts)
    if t is None:
        return None
    return (now() - t).total_seconds() / 60.0


class Report:
    """收集违反项。"""

    def __init__(self) -> None:
        self.violations: list[dict] = []
        self.checked: dict[str, int] = {}

    def add(self, rule: str, level: str, where: str, detail: str, evidence=None) -> None:
        self.violations.append({
            "rule": rule, "level": level, "where": where,
            "detail": detail, "evidence": evidence,
        })

    def error(self, rule, where, detail, evidence=None) -> None:
        self.add(rule, ERROR, where, detail, evidence)

    def warn(self, rule, where, detail, evidence=None) -> None:
        self.add(rule, WARN, where, detail, evidence)

    def count(self, rule: str) -> None:
        self.checked[rule] = self.checked.get(rule, 0) + 1


# ------------------------------------------------------------------ 事件重放

def _event_status(ev: dict) -> str | None:
    """从事件里还原它对指派状态的影响（还原不出来就返回 None）。

    注意：事件种类必须覆盖 sa.py / worker_runner.py 实际写出的全部状态变更，
    漏一种就会让重放出的权威状态停在旧值（并让 invariants 持续误报）。
    """
    kind = ev.get("kind")
    if kind == "assigned":
        return "assigned"
    if kind == "assignment_started":
        return "running"
    if kind == "responded":
        # 优先用结构化字段，回退到 message 尾缀解析
        status = str(ev.get("status") or "").strip()
        if status in ("done", "failed", "blocked", "waiting_user_input"):
            return status
        msg = str(ev.get("message") or "")
        tail = msg.split("：")[-1].strip() if "：" in msg else ""
        if tail in ("done", "failed", "blocked", "waiting_user_input"):
            return tail
        return None
    if kind in ("assignment_approved", "approved"):
        return "approved"
    if kind in ("assignment_rework", "rejected"):
        # 人工打回 → 回到 rework（可重新派发）
        return "rework"
    if kind == "assignment_reclaimed":
        return "assigned"
    if kind == "redirected":
        # 改派 → 重新变成待执行
        return "assigned"
    if kind == "assignment_failed":
        return "failed"
    if kind == "cancelled":
        return "cancelled"
    return None


def replay_assignments(task_id: str) -> dict[str, dict]:
    """按**合法迁移表**重放事件，得到每个指派的权威状态。

    非法的那一步（迟到回执覆盖终态、重复启动……）**不改变状态** ——
    权威状态就是「忽略非法迁移后」的状态。这正是清理脚本要回滚到的值。

    返回 {aid: {"status", "ts", "violations": [...], "starts": [...]}}
    """
    out: dict[str, dict] = {}
    for ev in read_jsonl(TASKS_DIR / task_id / "events.jsonl"):
        aid = ev.get("assignment")
        if not aid:
            continue
        rec = out.setdefault(aid, {"status": None, "ts": "", "violations": [], "starts": []})
        if ev.get("kind") == "assignment_started":
            rec["starts"].append(ev.get("ts"))
        nxt = _event_status(ev)
        if nxt is None:
            continue
        prev = rec["status"]
        allowed = LEGAL_TRANSITIONS.get(prev, set())
        if nxt == prev:
            rec["violations"].append({"type": "duplicate", "from": prev, "to": nxt,
                                      "ts": ev.get("ts"), "event": ev.get("kind")})
            continue
        if nxt not in allowed:
            rec["violations"].append({"type": "illegal", "from": prev, "to": nxt,
                                      "ts": ev.get("ts"), "event": ev.get("kind")})
            continue        # 非法迁移 → 不改变权威状态
        rec["status"] = nxt
        rec["ts"] = str(ev.get("ts") or "")
    return out


# ------------------------------------------------------------------ 数据装载

def load_tasks() -> dict[str, dict]:
    out = {}
    if not TASKS_DIR.is_dir():
        return out
    for d in sorted(TASKS_DIR.glob("T*")):
        st = read_json(d / "state.json")
        if isinstance(st, dict):
            out[d.name] = st
    return out


def load_outbox() -> list[dict]:
    out = []
    if not OUTBOX_DIR.is_dir():
        return out
    for f in sorted(OUTBOX_DIR.glob("ob_*.json")):
        d = read_json(f)
        if isinstance(d, dict):
            d["_file"] = f.name
            out.append(d)
    return out


def load_chain_config() -> dict:
    cfg = read_json(ROUTER_DIR / "chain.json")
    if not isinstance(cfg, dict):
        return {"enabled": True, "max_consecutive_dispatches": 2}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "max_consecutive_dispatches": int(cfg.get("max_consecutive_dispatches", 2) or 0),
    }


# ------------------------------------------------------------------ 各条不变式

def check_task_completed_needs_evidence(tasks: dict, rep: Report) -> None:
    """Task 标记 completed ⇒ 所有指派都到终态，且每个指派有产物或回执。"""
    for tid, st in tasks.items():
        rep.count("task_completed_needs_evidence")
        if st.get("status") != "completed":
            continue
        for a in st.get("assignments") or []:
            aid = a.get("id")
            if a.get("status") not in TERMINAL_ASSIGN:
                rep.error("task_completed_needs_evidence", f"{tid}/{aid}",
                          f"Task 已 completed，但指派还在 {a.get('status')}（非终态）",
                          {"task_status": st.get("status"), "assignment_status": a.get("status")})
            has_artifact = bool(a.get("artifacts"))
            resp = a.get("response") or ""
            has_response = bool(resp) and pathlib.Path(resp).is_file()
            if not (has_artifact or has_response):
                rep.error("task_completed_needs_evidence", f"{tid}/{aid}",
                          "Task 已 completed，但指派既无产物也无回执文件（缺完成证据）",
                          {"artifacts": a.get("artifacts"), "response": resp})


def check_terminal_assignment_has_response(tasks: dict, rep: Report) -> None:
    """指派到了终态（done/approved）⇒ 回执文件必须在。"""
    for tid, st in tasks.items():
        for a in st.get("assignments") or []:
            rep.count("terminal_assignment_has_response")
            if a.get("status") not in ("done", "approved"):
                continue
            resp = a.get("response") or ""
            if not resp:
                rep.error("terminal_assignment_has_response", f"{tid}/{a.get('id')}",
                          "指派已终结但没有 response 路径", {"status": a.get("status")})
            elif not pathlib.Path(resp).is_file():
                rep.error("terminal_assignment_has_response", f"{tid}/{a.get('id')}",
                          "指派已终结但回执文件不存在", {"response": resp})


def corrected_assignments(task_id: str) -> set[str]:
    """已被 `state_corrected` 事件修正过的指派。

    事件流是 append-only 的审计链 —— 历史上的违规**改不掉**（也不该改）。
    所以「历史上发生过、后来已回滚」与「现在还自相矛盾」必须分开：
      · 已修正 → WARN（历史事实，有据可查）
      · 没修正 → ERROR（状态现在就是错的）
    """
    out = set()
    for ev in read_jsonl(TASKS_DIR / task_id / "events.jsonl"):
        if ev.get("kind") == "state_corrected" and ev.get("assignment"):
            out.add(str(ev.get("assignment")))
    return out


def check_assignment_transitions(tasks: dict, rep: Report) -> None:
    """由 events.jsonl 重建指派状态序列，每一步都必须是合法边。"""
    for tid in tasks:
        fixed = corrected_assignments(tid)
        for aid, rec in replay_assignments(tid).items():
            for v in rec["violations"]:
                if v["type"] == "duplicate":
                    rep.warn("assignment_transitions_legal", f"{tid}/{aid}",
                             f"状态 {v['to']} 被重复写入（重复事件）", v)
                else:
                    # 历史事实 —— 事件流是 append-only 的，改不掉也不该改。
                    # 「现在状态对不对」由 assignment_state_consistent 判（ERROR）。
                    tail = "（已回滚修正）" if aid in fixed else ""
                    rep.warn("assignment_transitions_legal", f"{tid}/{aid}",
                             f"历史违规迁移 {v['from']} → {v['to']}{tail}"
                             + ("（迟到回执覆盖了已终结的状态）"
                                if v["from"] in TERMINAL_ASSIGN else ""),
                             v)


def check_assignment_state_consistent(tasks: dict, rep: Report) -> None:
    """**当前状态**必须与事件重放出的权威状态一致。

    这是「现在有没有自相矛盾」的活不变式 —— 与历史违规无关。
    """
    for tid in tasks:
        replay = replay_assignments(tid)
        for a in tasks[tid].get("assignments") or []:
            aid = a.get("id")
            rec = replay.get(aid) or {}
            auth = rec.get("status")
            cur = a.get("status")
            rep.count("assignment_state_consistent")
            if not auth or not cur:
                continue
            if cur != auth:
                rep.error("assignment_state_consistent", f"{tid}/{aid}",
                          f"当前状态 {cur} 与事件重放出的权威状态 {auth} 不一致"
                          f"（迟到的回执覆盖了状态？）",
                          {"current": cur, "authoritative": auth,
                           "violations": rec.get("violations", [])[:3]})


# 重建状态序列时用的跨任务缓存（每个任务单独跑，先清空）
_last_status: dict[tuple[str, str], str | None] = {}


def check_assignment_started_once(tasks: dict, rep: Report) -> None:
    """同一指派只能被启动执行一次。

    重复启动 = 两个 worker 进程跑同一个指派 → 回执互相覆盖、
    产物互相踩。方案 §9.4 要求迟到报告标 stale，而不是覆盖。
    """
    for tid in tasks:
        starts: dict[str, list[str]] = {}
        for ev in read_jsonl(TASKS_DIR / tid / "events.jsonl"):
            if ev.get("kind") != "assignment_started":
                continue
            aid = ev.get("assignment")
            if aid:
                starts.setdefault(aid, []).append(str(ev.get("ts")))
        for aid, tss in starts.items():
            rep.count("assignment_started_once")
            if len(tss) > 1:
                rep.warn("assignment_started_once", f"{tid}/{aid}",
                         f"同一指派历史上被启动了 {len(tss)} 次（并发跑同一指派，历史事实）",
                         {"starts": tss})


def check_session_not_concurrent(tasks: dict, rep: Report) -> None:
    """同一个 worker_session_id 不能同时有两个 running 指派。"""
    by_session: dict[str, list[str]] = {}
    for tid, st in tasks.items():
        for a in st.get("assignments") or []:
            sid = a.get("worker_session_id") or ""
            if not sid:
                continue
            if a.get("status") == "running":
                by_session.setdefault(sid, []).append(f"{tid}/{a.get('id')}(running)")
    for sid, owners in by_session.items():
        rep.count("session_not_concurrent")
        running = owners
        if len(running) > 1:
            rep.error("session_not_concurrent", sid,
                      f"同一 session 有 {len(running)} 个 running 指派（并发跑同一会话）",
                      {"assignments": owners})


def check_chain_dispatch_limit(tasks: dict, rep: Report) -> None:
    """链式派工计数不得超过配置上限。"""
    cfg = load_chain_config()
    limit = int(cfg.get("max_consecutive_dispatches", 2))
    for tid, st in tasks.items():
        rep.count("chain_dispatch_limit")
        n = int(st.get("chain_dispatch_count") or 0)
        if n > limit:
            rep.error("chain_dispatch_limit", tid,
                      f"链式派工计数 {n} 超过上限 {limit}",
                      {"chain_dispatch_count": n, "limit": limit, "enabled": cfg.get("enabled")})


def check_task_unique_per_source_message(tasks: dict, rep: Report) -> None:
    """同一个外部消息（source_ref.message_id）最多产生一个 Task。"""
    seen: dict[str, list[str]] = {}
    for tid, st in tasks.items():
        ref = st.get("source_ref") or {}
        mid = ref.get("message_id")
        if mid:
            seen.setdefault(str(mid), []).append(tid)
    for mid, tids in seen.items():
        rep.count("task_unique_per_source_message")
        if len(tids) > 1:
            rep.error("task_unique_per_source_message", mid,
                      f"同一条外部消息建了 {len(tids)} 个 Task", {"tasks": tids})


def check_assignment_ids_unique(tasks: dict, rep: Report) -> None:
    """任务内指派 id 不重复。"""
    for tid, st in tasks.items():
        rep.count("assignment_ids_unique")
        ids = [a.get("id") for a in st.get("assignments") or []]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            rep.error("assignment_ids_unique", tid, f"指派 id 重复：{dup}", {"ids": ids})


def check_outbox_succeeded_has_receipt(outbox: list[dict], rep: Report) -> None:
    """outbox 标记 succeeded ⇒ 必须有渠道凭据。

    分两级：
      · 记录里**根本没有** delivery 键 → 旧口径记录（投递凭据机制落地之前写的），
        无法追溯，只能算 WARN（历史债，不该和现在的新问题混在一起）；
      · 有 delivery 键但缺 message_id/request_id → 机制在跑却没拿到凭据 → ERROR。
    """
    for rec in outbox:
        rep.count("outbox_succeeded_has_receipt")
        if rec.get("status") != "succeeded":
            continue
        delivery = rec.get("delivery")
        if delivery is None:
            rep.warn("outbox_succeeded_has_receipt", rec.get("id", "?"),
                     "旧口径记录：标 succeeded 但没有 delivery 段（投递凭据机制落地之前）",
                     {"purpose": rec.get("purpose"), "task": rec.get("task"),
                      "sent_at": rec.get("sent_at")})
            continue
        mid = delivery.get("message_id") or ""
        rid = delivery.get("request_id") or ""
        if not (mid or rid):
            rep.error("outbox_succeeded_has_receipt", rec.get("id", "?"),
                      "有 delivery 段却没有任何凭据（message_id / request_id）",
                      {"purpose": rec.get("purpose"), "task": rec.get("task"),
                       "delivery": delivery})


def check_outbox_not_stuck(outbox: list[dict], rep: Report, stuck_minutes: int = 30) -> None:
    """pending 超过阈值的 outbox —— 不一定错，但该被人看一眼。"""
    for rec in outbox:
        rep.count("outbox_not_stuck")
        if rec.get("status") != "pending":
            continue
        age = age_minutes(rec.get("created_at") or rec.get("updated_at"))
        if age is not None and age > stuck_minutes:
            rep.warn("outbox_not_stuck", rec.get("id", "?"),
                     f"outbox pending 已 {int(age)} 分钟未投递",
                     {"purpose": rec.get("purpose"), "task": rec.get("task"),
                      "last_error": rec.get("last_error")})


def check_artifact_paths_exist(tasks: dict, rep: Report) -> None:
    """指派登记的产物路径应当真实存在（/tmp 产物被清理算 WARN 不算 ERROR）。"""
    for tid, st in tasks.items():
        for a in st.get("assignments") or []:
            for art in a.get("artifacts") or []:
                rep.count("artifact_paths_exist")
                p = pathlib.Path(str(art))
                if not p.is_file():
                    rep.warn("artifact_paths_exist", f"{tid}/{a.get('id')}",
                             f"登记的产物不存在：{art}", {"artifact": art})


def check_event_timeline_monotonic(tasks: dict, rep: Report) -> None:
    """任务事件流的时间戳不能倒流。"""
    for tid in tasks:
        last = None
        for ev in read_jsonl(TASKS_DIR / tid / "events.jsonl"):
            rep.count("event_timeline_monotonic")
            t = parse_ts(ev.get("ts"))
            if t is None:
                continue
            if last is not None and t < last:
                rep.error("event_timeline_monotonic", tid,
                          "事件时间戳倒流",
                          {"ts": ev.get("ts"), "prev": last.isoformat(),
                           "kind": ev.get("kind")})
            last = t


def check_every_event_accounted(rep: Report) -> None:
    """router inbox 里的事件应当都有决策留档或已被显式忽略。"""
    inbox = read_jsonl(ROUTER_DIR / "inbox.jsonl")
    if not inbox:
        return
    decisions_dir = ROUTER_DIR / "decisions"
    decided = set()
    if decisions_dir.is_dir():
        for f in decisions_dir.glob("*.json"):
            decided.add(f.stem)
    processed = read_json(ROUTER_DIR / "processed.json")
    processed_ids = set()
    if isinstance(processed, dict):
        processed_ids = set(processed.keys())
    elif isinstance(processed, list):
        processed_ids = {str(x) for x in processed}
    for ev in inbox:
        rep.count("every_event_accounted")
        eid = str(ev.get("id") or "")
        if not eid:
            continue
        if eid in decided or eid in processed_ids:
            continue
        # 最近一条事件可能正在处理中，不算漏
        if inbox and inbox[-1].get("id") == eid:
            continue
        rep.warn("every_event_accounted", eid,
                 "事件既无决策留档也未标记已处理（可能卡住或被漏掉）",
                 {"kind": ev.get("kind"), "ts": ev.get("ts")})


def check_grants_sane(tasks: dict, rep: Report) -> None:
    """临时授权要自洽：带会话的授权必须与任务来源会话一致（不能跨会话发有效）。"""
    for tid, st in tasks.items():
        grants = st.get("authorized_actors") or []
        if not grants:
            continue
        task_chat = str((st.get("source_ref") or {}).get("chat_id") or "")
        seen: set[str] = set()
        for g in grants:
            rep.count("grants_sane")
            oid = str(g.get("open_id") or "")
            if not oid:
                rep.error("grants_sane", tid, "临时授权缺少 open_id", {"grant": g})
                continue
            if oid in seen:
                rep.error("grants_sane", tid, f"同一人的授权重复出现：{oid}", {"grant": g})
            seen.add(oid)
            gchat = str(g.get("chat_id") or "")
            if gchat and task_chat and gchat != task_chat:
                rep.error("grants_sane", tid,
                          f"授权会话 {gchat} 与任务来源会话 {task_chat} 不一致"
                          f"（授权会失效或越界）", {"grant": g})
            exp = g.get("expires_at")
            if exp:
                t = parse_ts(exp)
                if t is not None and t < now() - timedelta(hours=1):
                    rep.warn("grants_sane", tid,
                             f"授权已过期仍留在名单里（{g.get('name') or oid}）", {"grant": g})


def check_task_has_source(tasks: dict, rep: Report) -> None:
    """Task 应该能说清是谁、从哪来触发的（source_ref 至少要有 message_id 或 chat_id）。"""
    for tid, st in tasks.items():
        rep.count("task_has_source")
        ref = st.get("source_ref") or {}
        if not (ref.get("message_id") or ref.get("chat_id")):
            rep.warn("task_has_source", tid, "Task 没有可追溯的来源引用",
                     {"source": st.get("source")})


# ------------------------------------------------------------------ 主流程

def run(task_filter: str | None = None) -> Report:
    global _last_status
    _last_status = {}
    rep = Report()
    tasks = load_tasks()
    if task_filter:
        tasks = {k: v for k, v in tasks.items() if k == task_filter}
    outbox = load_outbox()

    check_task_completed_needs_evidence(tasks, rep)
    check_terminal_assignment_has_response(tasks, rep)
    check_assignment_transitions(tasks, rep)
    check_assignment_state_consistent(tasks, rep)
    check_assignment_started_once(tasks, rep)
    check_session_not_concurrent(tasks, rep)
    check_chain_dispatch_limit(tasks, rep)
    check_task_unique_per_source_message(tasks, rep)
    check_assignment_ids_unique(tasks, rep)
    check_outbox_succeeded_has_receipt(outbox, rep)
    check_outbox_not_stuck(outbox, rep)
    check_artifact_paths_exist(tasks, rep)
    check_event_timeline_monotonic(tasks, rep)
    check_every_event_accounted(rep)
    check_task_has_source(tasks, rep)
    check_grants_sane(tasks, rep)
    return rep


RULES = {
    "task_completed_needs_evidence": "Task completed ⇒ 指派全部终态且有证据（产物或回执）",
    "terminal_assignment_has_response": "指派终结（done/approved）⇒ 回执文件存在",
    "assignment_transitions_legal": "指派状态迁移只走合法边（迟到回执不得覆盖终态）",
    "assignment_state_consistent": "当前指派状态与事件重放出的权威状态一致",
    "assignment_started_once": "同一指派不被重复启动执行",
    "session_not_concurrent": "同一 worker session 不并发",
    "chain_dispatch_limit": "链式派工计数 ≤ 配置上限",
    "task_unique_per_source_message": "一条外部消息最多建一个 Task",
    "assignment_ids_unique": "任务内指派 id 唯一",
    "outbox_succeeded_has_receipt": "outbox succeeded ⇒ 有投递凭据",
    "outbox_not_stuck": "outbox 不长期 pending",
    "artifact_paths_exist": "登记的产物路径存在",
    "event_timeline_monotonic": "事件时间戳不倒流",
    "every_event_accounted": "每条事件都有决策留档或被显式处理",
    "task_has_source": "Task 有可追溯来源",
    "grants_sane": "临时授权自洽（会话一致、无重复、无过期残留）",
}


def print_report(rep: Report, tasks_count: int, outbox_count: int) -> None:
    errs = [v for v in rep.violations if v["level"] == ERROR]
    warns = [v for v in rep.violations if v["level"] == WARN]
    print("=" * 68)
    print("不变式校验（只读）")
    print(f"  任务 {tasks_count} 个 · outbox {outbox_count} 条 · "
          f"检查项 {sum(rep.checked.values())} 次")
    print("=" * 68)
    if not rep.violations:
        print("✅ 全部通过 —— 没有发现违反不变式的状态")
        return
    for lvl, group in ((ERROR, errs), (WARN, warns)):
        if not group:
            continue
        icon = "❌" if lvl == ERROR else "⚠️ "
        print(f"\n{icon} {lvl} × {len(group)}")
        by_rule: dict[str, list[dict]] = {}
        for v in group:
            by_rule.setdefault(v["rule"], []).append(v)
        for rule, items in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
            print(f"\n  [{rule}] {RULES.get(rule, '')}  —— {len(items)} 处")
            for v in items[:6]:
                print(f"    · {v['where']}: {v['detail']}")
                if v.get("evidence"):
                    ev = json.dumps(v["evidence"], ensure_ascii=False)
                    print(f"      证据: {ev[:220]}")
            if len(items) > 6:
                print(f"    … 另有 {len(items) - 6} 处")


def main() -> int:
    ap = argparse.ArgumentParser(description="Super Agent 不变式校验器（只读）")
    ap.add_argument("--json", action="store_true", help="输出机读 JSON")
    ap.add_argument("--task", help="只校验某个任务（如 T012）")
    ap.add_argument("--strict", action="store_true", help="WARN 也算失败")
    a = ap.parse_args()

    rep = run(a.task)
    tasks = load_tasks()
    outbox = load_outbox()
    errs = [v for v in rep.violations if v["level"] == ERROR]
    warns = [v for v in rep.violations if v["level"] == WARN]

    if a.json:
        print(json.dumps({
            "tasks": len(tasks), "outbox": len(outbox),
            "rules": RULES,
            "checks": rep.checked,
            "errors": len(errs), "warnings": len(warns),
            "violations": rep.violations,
        }, ensure_ascii=False, indent=2))
    else:
        print_report(rep, len(tasks), len(outbox))

    if errs:
        return 1
    if a.strict and warns:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
