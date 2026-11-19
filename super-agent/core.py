#!/usr/bin/env python3
"""Task Center 规则层（纯规则，不依赖 sa.py）。

包含：
  · Worker Registry 读取与派工校验
  · Decision / DecisionItem 的 Schema 校验（一期简化口径）
  · 指令单渲染（ContextBuilder 化：信封由代码统一生成，router 只填目标与要求）
  · outbox 外发消息队列
  · Task 完成策略评估

所有函数都只吃普通 dict / 路径，方便单测。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent
REGISTRY_FILE = ROOT / "registry.json"


def _state_dir(env_name: str, default_rel: str) -> pathlib.Path:
    """可写状态目录：环境变量覆盖 > SA_ROOT/<default_rel> > 代码目录。

    沙箱演练只要把 SA_ROOT 指向临时目录，outbox / tasks 就不会再写进生产
    （曾经的隔离假设：只重定向桥接自己的文件就够 —— 结果外发记录仍落在生产）。
    """
    raw = (os.environ.get(env_name) or "").strip()
    if raw:
        return pathlib.Path(raw).expanduser().resolve()
    base = (os.environ.get("SA_ROOT") or "").strip()
    root = pathlib.Path(base).expanduser().resolve() if base else ROOT
    return root / default_rel


OUTBOX_DIR = _state_dir("SA_OUTBOX_DIR", "outbox")
TASKS_DIR = _state_dir("SA_TASKS_DIR", "tasks")

# 指派的活跃状态口径（与 sa.ACTIVE 保持一致）
ACTIVE_STATUSES = ("assigned", "running", "rework")

DECISION_SCHEMA_VERSION = 2
TASK_ACTIONS = ("create", "continue")
VERDICTS = ("approve", "reject")
OUTBOX_PURPOSES = ("ack", "reply", "question", "approval", "collaborate", "notify", "result")
OUTBOX_TARGETS = ("thread", "actor", "subscribers")


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------- Registry


def load_registry() -> dict:
    if not REGISTRY_FILE.exists():
        return {"version": 1, "workers": []}
    try:
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": 1, "workers": []}


def worker_specs() -> dict:
    reg = load_registry()
    return {w["agent_id"]: w for w in reg.get("workers", []) if w.get("agent_id")}


def worker_spec(agent_id: str) -> dict | None:
    return worker_specs().get(agent_id)


def validate_agent(agent_id: str, readonly_needed: bool = False) -> tuple[bool, str]:
    """派工前校验 worker：是否在册、是否启用、能力是否匹配。"""
    spec = worker_spec(agent_id)
    if not spec:
        return False, f"worker '{agent_id}' 不在 registry 名册内"
    if not spec.get("enabled", True):
        return False, (f"worker '{agent_id}' 已停用："
                       f"{spec.get('disabled_reason', '未说明原因')}")
    if readonly_needed and not spec.get("readonly", False):
        return False, f"worker '{agent_id}' 不是只读角色，不能用于只读指派"
    return True, ""


def max_parallel_for(agent_id: str) -> int:
    spec = worker_spec(agent_id) or {}
    reg = load_registry()
    return int(spec.get("max_parallel", reg.get("defaults", {}).get("max_parallel", 2)))


def count_active(agent_id: str, states: list[dict]) -> int:
    """该 agent 当前占用的并发数。

    口径与 sa.ACTIVE 一致（assigned / running / rework）——
    漏算 rework 会让实际并发超过 registry 里的 max_parallel。
    """
    n = 0
    for st in states:
        for a in st.get("assignments", []):
            if a.get("worker") == agent_id and a.get("status") in ACTIVE_STATUSES:
                n += 1
    return n


# --------------------------------------------------------------- Decision


def _err(code: str, msg: str) -> dict:
    return {"reason_code": code, "message": msg}


def validate_decision(doc: dict, *, allowed_triggers: list[str] | None = None,
                      known_task_ids: list[str] | None = None,
                      known_assignments: list[str] | None = None,
                      session_sources: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """校验 Decision。返回 (合法条目, 错误列表)。

    Schema v2 口径：
      - 一个独立用户目标 = 一个 DecisionItem
      - taskAction: create（新任务，taskId 必须为空）/ continue（沿用已有任务，taskId 必填）
      - agentId 必须来自 registry
      - sessionFrom 可选：沿用某个已有指派的 worker session（同一 agent 的上下文延续）
      - 置信度只作评测信号，不单独决定是否派发

    session_sources 形如 [{"task": "T012", "assignment": "A01",
                            "agent": "explore", "session": "w-T012-A01-xxxx"}]
    """
    errs: list[dict] = []
    good: list[dict] = []

    if not isinstance(doc, dict):
        return [], [_err("schema_invalid", "Decision 必须是一个 JSON 对象")]
    if doc.get("schemaVersion") != DECISION_SCHEMA_VERSION:
        errs.append(_err("schema_version", f"schemaVersion 必须是 {DECISION_SCHEMA_VERSION}"))
    trig = doc.get("triggerId")
    if not trig:
        errs.append(_err("missing_trigger", "缺少 triggerId"))
    elif allowed_triggers is not None and trig not in allowed_triggers:
        errs.append(_err("unknown_trigger", f"triggerId '{trig}' 不在本批事件里"))
        trig = None

    items = doc.get("items")
    if items is None:
        items = []
    if not isinstance(items, list):
        return [], errs + [_err("schema_invalid", "items 必须是数组")]

    for idx, it in enumerate(items):
        e: list[dict] = []
        if not isinstance(it, dict):
            errs.append(_err("item_invalid", f"items[{idx}] 不是对象"))
            continue
        if not it.get("triggerId"):
            e.append(_err("missing_trigger", f"items[{idx}] 缺少 triggerId"))
        elif allowed_triggers is not None and it["triggerId"] not in allowed_triggers:
            e.append(_err("unknown_trigger", f"items[{idx}].triggerId '{it['triggerId']}' 不在本批事件里"))
        action = it.get("taskAction")
        if action not in TASK_ACTIONS:
            e.append(_err("bad_task_action",
                           f"items[{idx}].taskAction 必须是 {TASK_ACTIONS} 之一，得到 '{action}'"))
        task_id = (it.get("taskId") or "").strip()
        if action == "create" and task_id:
            e.append(_err("create_with_task_id",
                           f"items[{idx}] taskAction=create 表示新任务，不能带 taskId"
                           f"（沿用已有任务请用 continue + taskId）"))
        if action == "continue" and not task_id:
            e.append(_err("continue_without_task_id", f"items[{idx}] taskAction=continue 必须带 taskId"))
        if action == "continue" and known_task_ids is not None and task_id not in known_task_ids:
            e.append(_err("unknown_task", f"items[{idx}].taskId '{task_id}' 不存在"))
        agent = (it.get("agentId") or "").strip()
        ok, why = validate_agent(agent)
        if not ok:
            e.append(_err("bad_agent", f"items[{idx}]: {why}"))
        else:
            # ── trigger_gate：registry 声明的触发闸门，由 Task Center 强制（不靠提示词）──
            # worker 声明了 trigger_gate 时，Decision 必须显式带上 requester 归属信息，
            # 且满足闸门（如「只有 owner 本人私聊才触发」）。不满足 → 整条 item 被拒。
            gate = (worker_spec(agent) or {}).get("trigger_gate") or {}
            if gate:
                req = it.get("requester") or {}
                if not isinstance(req, dict):
                    req = {}
                allowed_senders = [str(s) for s in (gate.get("senders") or [])]
                sender_id = str(req.get("openId") or "").strip()
                chat_type = str(req.get("chatType") or "").strip().lower()
                if not sender_id and not chat_type:
                    e.append(_err("gate_missing_requester",
                                  f"items[{idx}]: worker '{agent}' 有触发闸门，"
                                  f"Decision 必须带 requester.openId / requester.chatType"))
                else:
                    if allowed_senders and sender_id not in allowed_senders:
                        e.append(_err("gate_sender_denied",
                                      f"items[{idx}]: worker '{agent}' 只允许特定请求人触发"
                                      f"（{'、'.join(allowed_senders)}），"
                                      f"本次 requester.openId='{sender_id}'"))
                    if gate.get("require_p2p") and chat_type != "p2p":
                        e.append(_err("gate_channel_denied",
                                      f"items[{idx}]: worker '{agent}' 只允许私聊（p2p）触发，"
                                      f"本次 requester.chatType='{chat_type or '未知'}'"))
        if not (it.get("goal") or "").strip():
            e.append(_err("missing_goal", f"items[{idx}] 缺少 goal"))

        # ── sessionFrom：沿用某个已有指派的 worker session ──
        sfrom = (it.get("sessionFrom") or "").strip()
        if sfrom:
            if action != "continue":
                e.append(_err("session_without_continue",
                               f"items[{idx}] sessionFrom 只能配合 taskAction=continue 使用"))
            src = next((x for x in (session_sources or [])
                        if x.get("task") == task_id and x.get("assignment") == sfrom), None)
            if src is None:
                e.append(_err("unknown_session_source",
                               f"items[{idx}] sessionFrom '{sfrom}' 在任务 {task_id} 里不存在"))
            elif src.get("agent") and agent and src["agent"] != agent:
                e.append(_err("session_agent_mismatch",
                               f"items[{idx}] sessionFrom '{sfrom}' 属于 agent "
                               f"'{src['agent']}'，与 agentId '{agent}' 不一致"))
            elif not (src.get("session") or "").strip():
                e.append(_err("session_not_ready",
                               f"items[{idx}] sessionFrom '{sfrom}' 还没有建立 worker session"
                               f"（该指派尚未运行过）"))
        conf = it.get("confidence")
        if conf is not None and (not isinstance(conf, (int, float)) or not 0 <= float(conf) <= 1):
            e.append(_err("bad_confidence", f"items[{idx}].confidence 必须在 0..1"))
        if e:
            errs.extend(e)
            continue
        good.append(it)

    for idx, rv in enumerate(doc.get("reviews") or []):
        if not isinstance(rv, dict):
            errs.append(_err("review_invalid", f"reviews[{idx}] 不是对象"))
            continue
        if rv.get("verdict") not in VERDICTS:
            errs.append(_err("bad_verdict", f"reviews[{idx}].verdict 必须是 {VERDICTS} 之一"))
        elif known_assignments is not None and (rv.get("assignment") or "") not in known_assignments:
            errs.append(_err("unknown_assignment",
                              f"reviews[{idx}].assignment '{rv.get('assignment')}' 不存在"))

    for idx, ob in enumerate(doc.get("outbox") or []):
        if not isinstance(ob, dict):
            errs.append(_err("outbox_invalid", f"outbox[{idx}] 不是对象"))
            continue
        if ob.get("purpose") not in OUTBOX_PURPOSES:
            errs.append(_err("bad_purpose",
                             f"outbox[{idx}].purpose 必须是 {OUTBOX_PURPOSES} 之一"))
        if ob.get("target") not in OUTBOX_TARGETS:
            errs.append(_err("bad_target",
                             f"outbox[{idx}].target 必须是 {OUTBOX_TARGETS} 之一"))
        if not (ob.get("text") or "").strip():
            errs.append(_err("empty_text", f"outbox[{idx}] 缺少 text"))

    return good, errs


# --------------------------------------------------------------- 指令单渲染


def render_instruction(*, task_id: str, assignment_id: str, agent_id: str,
                       goal: str, requirements: list[str], acceptance: list[str],
                       context_files: list[str], background: str = "",
                       trigger_id: str = "", readonly: bool = False,
                       response_path: str = "") -> str:
    """统一渲染指派单信封。router 只提供 goal / requirements / acceptance。"""
    spec = worker_spec(agent_id) or {}
    lines = [
        f"# 指派单 {assignment_id}",
        "",
        f"- task: `{task_id}`",
        f"- assignment: `{assignment_id}`",
        f"- agent: `{agent_id}`（{spec.get('label', '')}）",
        f"- 派发时间: {now()}",
    ]
    if trigger_id:
        lines.append(f"- trigger: `{trigger_id}`")
    lines += [
        "",
        "## 目标",
        "",
        goal.strip(),
        "",
    ]
    if background:
        lines += ["## 背景", "", background.strip(), ""]
    if context_files:
        lines += ["## 输入材料（绝对路径，只读）", ""]
        lines += [f"- `{p}`" for p in context_files]
        lines += [""]
    if requirements:
        reqs = [str(r).strip() for r in requirements if str(r).strip()]
        if reqs:
            lines += ["## 具体要求", ""]
            lines += [f"{i}. {r}" for i, r in enumerate(reqs, start=1)]
            lines += [""]
    if acceptance:
        lines += ["## 验收标准", "", "- [ ] " + "\n- [ ] ".join(str(a).strip() for a in acceptance if str(a).strip()), ""]
    lines += [
        "## 边界（不要做这些）",
        "",
        "- 不要修改本指派单与 channels/ 目录之外的文件，除非上面明确授权",
    ]
    if readonly:
        lines.append("- 你是只读角色：**绝对不要**修改、创建、删除任何业务文件")
    lines += [
        "- 不要替 router 做汇总或对外发消息",
        "- 缺信息时不要猜：回执写 `status: waiting_user_input` 并提出具体问题",
        "- 产出与回执中不要出现：同事姓名/工号/uid、他人或群的消息内容、内部接口与域名、"
        "内部工单号/计划号、内部仓库名与库表名、SQL 片段；必要时用占位符（如 `<同事A>`、`<内部工单>`）代替。"
        "需要细节就把细节写进产出文件，由用户自己打开",
        "",
        "## 回执要求",
        "",
        f"完成后把回执写到：`{response_path}`",
        "",
        "格式：",
        "",
        "```markdown",
        "# 回执 " + assignment_id,
        "",
        "- assignment: `" + assignment_id + "`",
        "- agent: `" + agent_id + "`",
        "- status: `done`   # done | failed | blocked | waiting_user_input",
        "- updated_at: <ISO8601>   # 这一行由系统盖章，worker 不用填",
        "",
        "## 结论",
        "",
        "（一句话：做了什么 + 结果是什么）",
        "",
        "## 产出",
        "",
        "- `/abs/path/产物.md`",
        "```",
        "",
        "## 完成策略",
        "",
        "本任务按「目标 + 完成证据」判定完成：产出文件必须落盘并给出绝对路径。",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------- outbox


def outbox_path(msg_id: str) -> pathlib.Path:
    return OUTBOX_DIR / f"{msg_id}.json"


# --------------------------------------------------------------- 会话标识解析


def task_source_ref(task_id: str) -> dict:
    """任务状态里的来源引用（飞书 chat_id / message_id 等）。

    任务状态里的 source_ref 有两种历史形态：
      · dict（当前形态）
      · ["chat_id=xxx", "message_id=yyy"]（列表形态）
    两种都要认，否则老任务的来源标识取不到。
    """
    if not task_id:
        return {}
    p = TASKS_DIR / task_id / "state.json"
    if not p.exists():
        return {}
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    ref = st.get("source_ref") or {}
    out: dict = {}
    if isinstance(ref, dict):
        out = {k: str(v) for k, v in ref.items() if v}
    elif isinstance(ref, (list, tuple)):
        for kv in ref:
            if isinstance(kv, str) and "=" in kv:
                k, v = kv.split("=", 1)
                if k.strip() and v.strip():
                    out[k.strip()] = v.strip()
    return out


def resolve_session(task_id: str = "", chat_id: str = "", message_id: str = "",
                    thread_id: str = "", root_id: str = "") -> dict:
    """解析一条外发消息的投递会话。

    调用方给了会话就直接用；没给就**从任务的 source_ref 继承** ——
    Decision 生成的 ack 若丢了会话标识，投递器会判成「无处可投」并静默丢弃，
    用户永远收不到「已受理：任务号 TXXX」。

    返回 {"chat_id", "reply_to", "sender", "thread_id", "root_id", "session_source"}。
    """
    ref = task_source_ref(task_id)
    got_chat = chat_id or ref.get("chat_id", "")
    got_reply = message_id or ref.get("message_id", "")
    if chat_id or message_id:
        source = "explicit"
    elif got_chat or got_reply:
        source = "task.source_ref"
    else:
        source = ""
    return {
        "chat_id": got_chat,
        "reply_to": got_reply,
        "sender": ref.get("sender", ""),
        "thread_id": thread_id or ref.get("thread_id", ""),
        "root_id": root_id or ref.get("root_id", ""),
        "session_source": source,
    }


def add_outbox(*, target: str, purpose: str, text: str, task: str = "",
               assignment: str = "", channel: str = "", chat_id: str = "",
               message_id: str = "", sender: str = "", actor: str = "",
               batch: str = "", dedupe_window_seconds: int = 900) -> dict:
    """写入一条待发消息。

    同一条消息被重复提交（例如 Decision 重投）时不产生第二条 —— 按
    (task, purpose, assignment, text) 在时间窗内去重，避免给渠道刷屏。
    """
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    key = (task, purpose, assignment, text.strip())
    if dedupe_window_seconds > 0:
        cutoff = _dt.datetime.now().astimezone() - _dt.timedelta(seconds=dedupe_window_seconds)
        for rec in list_outbox():
            if rec.get("status") not in ("pending", "dispatched", "succeeded"):
                continue
            if (rec.get("task"), rec.get("purpose"), rec.get("assignment", ""),
                    (rec.get("text") or "").strip()) != key:
                continue
            try:
                created = _dt.datetime.fromisoformat(rec.get("created_at", ""))
            except ValueError:
                continue
            if created >= cutoff:
                return rec
    import uuid
    mid = f"ob_{uuid.uuid4().hex[:10]}"
    # 会话标识：调用方没给就从任务来源继承（Decision 生成的 ack 最容易缺这一段）
    sess = resolve_session(task, chat_id, message_id)
    rec = {
        "id": mid,
        "target": target,
        "purpose": purpose,
        "text": text,
        "task": task,
        "assignment": assignment,
        # 批次 id：同一批的结果消息在投递时合并成一条（见 bridge 的 deliver_outbox）
        "batch": batch or "",
        "channel": channel or "feishu",
        "chat_id": sess["chat_id"],
        "reply_to": sess["reply_to"],
        "sender": sender or sess["sender"],
        "thread_id": sess.get("thread_id", ""),
        "root_id": sess.get("root_id", ""),
        "actor": actor,
        "status": "pending",
        "attempts": 0,
        "created_at": now(),
        "next_run_at": now(),
        "session_source": sess["session_source"],
    }
    outbox_path(mid).write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return rec


def list_outbox(status: str = "") -> list[dict]:
    """列出 outbox 记录，按创建时间升序（FIFO）。

    文件名是随机 uuid，按文件名排序等于随机顺序 —— 外发顺序必须是 FIFO。
    """
    if not OUTBOX_DIR.exists():
        return []
    out = []
    for p in sorted(OUTBOX_DIR.glob("ob_*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if status and rec.get("status") != status:
            continue
        out.append(rec)
    out.sort(key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")))
    return out


def update_outbox(rec: dict, **fields) -> dict:
    rec.update(fields)
    rec["updated_at"] = now()
    p = outbox_path(rec["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return rec


def pending_outbox() -> list[dict]:
    return [r for r in list_outbox("pending")]


# --------------------------------------------------------------- 完成策略


def evaluate_completion(state: dict) -> tuple[str, str]:
    """Task 完成由「目标 + 完成证据」决定，不由 Assignment 是否结束决定。

    返回 (status, reason)。
    """
    policy = state.get("completion_policy") or {}
    asgs = state.get("assignments", [])
    if not asgs:
        return "open", "尚未派发任何指派"
    need_approval = policy.get("require_approval", True)
    evidence_required = policy.get("evidence_required", True)

    # 已取消的指派不参与完成判定 —— 取消是有意为之的「不做了」，
    # 若把它算作未达完成条件，任务会永久卡在 needs_human。
    active = [a for a in asgs if a.get("status") != "cancelled"]
    if not active:
        return "needs_human", "全部指派均已取消，无有效交付"

    for a in active:
        if a.get("status") in ("assigned", "running"):
            return "dispatched", f"{a['id']} 仍在执行"
        if a.get("status") in ("blocked", "waiting_user_input"):
            return "needs_human", f"{a['id']} 等待人工输入"
        if a.get("status") in ("rework", "failed"):
            return "needs_human", f"{a['id']} 未通过（{a.get('status')}）"
        if need_approval and a.get("status") == "done":
            return "needs_human", f"{a['id']} 已回执，等待确认"
        if need_approval and a.get("status") not in ("approved",):
            return "needs_human", f"{a['id']} 状态 {a.get('status')} 未达完成条件"

    if evidence_required:
        for a in active:
            if a.get("status") == "approved" and not (a.get("artifacts") or []):
                return "needs_human", f"{a['id']} 已放行但缺少产出证据（artifact）"
    return "completed", "全部指派已完成且证据齐备"
