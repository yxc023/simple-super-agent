#!/usr/bin/env python3
"""super-agent 文件协议工具（sa.py）。

只做四件事：建任务、派指派、收回执、人工干预。所有状态都落在文件里，
任何一方（super agent / worker / 人）都能只用文本工具读懂。

子命令：
  sa.py new-task   --title T --goal G [--context-file F]... [--context TEXT]...
  sa.py assign     --task T --worker W --instruction-file F [--brief B]
  sa.py respond    --assignment A --status done|failed|blocked --summary S
  sa.py list
  sa.py show       --task T
  sa.py events     --task T
  sa.py comment    --assignment A --text "..."        （人工批注，agent 下轮必须回应）
  sa.py approve    --assignment A --note "..."        （人工放行）
  sa.py reject     --assignment A --reason "..."      （人工打回，等重派）
  sa.py cancel     --assignment A --reason "..."      （人工取消）
  sa.py redirect   --assignment A --to <worker>       （人工改派）
  sa.py ack        --assignment A                    （agent 回应人工批注）
  sa.py note       --task T --text "..."             （任务级人工留言）
  sa.py outbox-verify [--out F]                     （对账：succeeded 无凭据/无投递行 → unverified，只出清单）
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import re
import uuid
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import core  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent

# ------------------------------------------------------------------ 状态根 / 沙箱
#
# 所有**可写状态**（任务、channels、router 收件箱、outbox）都从 SA_ROOT 派生，
# 而不是写死成模块级常量 —— 验证脚本以前只隔离了桥接自己的文件，没隔离
# notify() 的出口，于是「沙箱里造的事件」直接落进生产 router/inbox.jsonl，
# router 见到就当真指令建了任务（真实事故）。
#
#   SA_ROOT        状态根目录（默认 = 代码目录，即生产）
#   SA_TASKS_DIR   覆盖 tasks/
#   SA_CHANNELS_DIR 覆盖 channels/
#   SA_ROUTER_DIR  覆盖 router/
#   SA_INBOX_DIR   覆盖 inbox/（消息流水与永久账本）
#   SA_OUTBOX_DIR  覆盖 outbox/
#
# 只要把 SA_ROOT 指到临时目录，一次演练/自检就**物理上碰不到**生产状态。

def env_path(name: str) -> pathlib.Path | None:
    """读一个路径型环境变量（空串视为未设置）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    return pathlib.Path(raw).expanduser().resolve()


def state_path(env_name: str, default_rel: str) -> pathlib.Path:
    """状态路径 = 环境变量覆盖 > SA_ROOT/<default_rel>。"""
    override = env_path(env_name)
    if override is not None:
        return override
    return SA_ROOT / default_rel


SA_ROOT = env_path("SA_ROOT") or ROOT
# 沙箱模式：状态根被挪到了代码目录之外 ⇒ 本次跑的是「演练」，不是生产。
SANDBOX_MODE = SA_ROOT != ROOT

TASKS_DIR = state_path("SA_TASKS_DIR", "tasks")
CHANNELS_DIR = state_path("SA_CHANNELS_DIR", "channels")
INBOX_DIR = state_path("SA_INBOX_DIR", "inbox")
# 桥接的永久账本（正常路径下账本先于 router 事件写入，router 用它做反查）
LEDGER_FILE = INBOX_DIR / "handled.jsonl"
STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"

ACTIVE = ("assigned", "running", "rework")
TERMINAL = ("done", "approved", "failed", "blocked", "cancelled", "waiting_user_input")

# worker 名册以 registry.json 为唯一真源
WORKERS = tuple(core.worker_specs().keys())


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


# task id 会从飞书消息、HTTP 请求体等外部输入进来，必须先校验格式，
# 否则 `TASKS_DIR / task_id` 会被 `../` 之类的片段带出状态根（路径穿越写）。
TASK_ID_RE = re.compile(r"^T\d{3,}$")


def valid_task_id(task_id: str) -> bool:
    """task id 是否是合法格式（T + 至少 3 位数字）。"""
    return bool(task_id) and bool(TASK_ID_RE.match(task_id))


def task_dir(task_id: str) -> pathlib.Path:
    """任务目录。

    task_id 来自飞书消息、HTTP 请求体等外部输入，这里统一做格式校验：
    不合法就直接退出，避免 `../` 之类的片段把文件写到状态根之外。
    空串表示「还没分配 id」，允许。
    """
    if task_id and not valid_task_id(task_id):
        die(f"invalid task id: {task_id!r}（应为 T + 至少 3 位数字）")
    return TASKS_DIR / task_id


def load_state(task_id: str) -> dict:
    p = task_dir(task_id) / STATE_FILE
    if not p.exists():
        die(f"task not found: {task_id}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"task state is corrupted: {task_id} ({e})")


def list_task_ids() -> list[str]:
    """列出所有任务 id（按 id 排序）。桥接按指派状态补表情时用。"""
    out = []
    for d in TASKS_DIR.glob("T*"):
        if (d / STATE_FILE).exists():
            out.append(d.name)
    return sorted(out)


def log_event(task_id: str, kind: str, actor: str, message: str, **extra) -> None:
    d = task_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    ev = {"ts": now(), "kind": kind, "actor": actor, "message": message}
    ev.update(extra)
    with (d / EVENTS_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def read_events(task_id: str) -> list:
    p = task_dir(task_id) / EVENTS_FILE
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def notes_path(task_id: str, name: str) -> pathlib.Path:
    if not valid_task_id(task_id):
        die(f"invalid task id: {task_id!r}（应为 T + 至少 3 位数字）")
    p = CHANNELS_DIR / task_id / "notes"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{name}.md"


def append_note(task_id: str, name: str, block: str) -> pathlib.Path:
    p = notes_path(task_id, name)
    header = "" if p.exists() else f"# 人工干预记录 {name}\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(header + block.rstrip() + "\n\n")
    return p


def render_task_md(state: dict) -> str:
    asg = state.get("assignments", [])
    lines = [
        f"# {state['title']}",
        "",
        f"- task_id: `{state['task_id']}`",
        f"- created_at: {state['created_at']}",
        f"- updated_at: {state['updated_at']}",
        f"- status: `{state['status']}`",
        "",
        "## 来源",
        "",
        f"- source: `{state.get('source', 'manual')}`",
        *[
            f"- {k}: `{v}`"
            for k, v in (state.get("source_ref") or {}).items()
        ],
        "",
        "## 目标",
        "",
        state["goal"],
        "",
        "## 上下文文件",
        "",
    ]
    cf = state.get("context_files", [])
    lines += [f"- `{p}`" for p in cf] if cf else ["- （无）"]
    lines += ["", "## 验收标准", ""]
    lines += state.get("acceptance_lines") or ["- （未定义）"]
    lines += ["", "## 指派", ""]
    if asg:
        for x in asg:
            flag = " ⏳待agent回应" if x.get("pending_ack") else ""
            lines.append(f"- `{x['id']}` → worker `{x['worker']}`（{x['status']}{flag}）")
            lines.append(f"  - 指令：`{x['instruction']}`")
            lines.append(f"  - 回执：`{x['response']}`")
    else:
        lines.append("- （尚未派发）")
    lines += ["", "## 人工干预", ""]
    nt = state.get("task_notes")
    lines.append(nt.strip() if nt else "- （无）")
    lines += [""]
    return "\n".join(lines)


def save_state(task_id: str, state: dict) -> None:
    """写任务状态。

    state.json 用「临时文件 + os.replace」原子替换：sa / worker_runner / router /
    看板是四个独立进程，直写的话读者可能读到半截 JSON。
    """
    state["updated_at"] = now()
    d = task_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    sp = d / STATE_FILE
    tmp = d / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, sp)
    (d / "task.md").write_text(render_task_md(state), encoding="utf-8")


def recompute_task_status(state: dict) -> str:
    """Task 状态由「完成策略」评估得出（目标 + 完成证据），不是由指派是否结束推导。"""
    status, _reason = core.evaluate_completion(state)
    if status in ("completed", "needs_human", "dispatched", "open"):
        return status
    return status or "running"


ROUTER_DIR = state_path("SA_ROUTER_DIR", "router")
ROUTER_INBOX = ROUTER_DIR / "inbox.jsonl"
# outbox 目录的真源在 core（core.py 里同样按 SA_ROOT / SA_OUTBOX_DIR 派生）
OUTBOX_DIR = core.OUTBOX_DIR


def notify(kind: str, task_id: str | None = None, **payload) -> None:
    """把事件投递到 router 收件箱。

    router 是一个常驻的 pi session（同一 session 处理所有飞书指令），
    它 tail 这个 JSONL 文件，收到事件就醒过来做决策。

    出口路径一律打印到 stderr（不污染 stdout 的 JSON 约定），沙箱模式下显式标注 ——
    「不知道自己在写哪个文件」正是上次凭空造出任务的原因。
    """
    ROUTER_DIR.mkdir(parents=True, exist_ok=True)
    ev = {"id": uuid.uuid4().hex[:12], "ts": now(), "kind": kind, "task": task_id}
    ev.update({k: v for k, v in payload.items() if v is not None})
    with ROUTER_INBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    tag = "沙箱模式（写的是临时状态根，不碰生产）" if SANDBOX_MODE else "生产模式"
    print(f"[sa.notify] {tag} → {ROUTER_INBOX}", file=sys.stderr)
    return ev


# 计数器放在 tasks/ 之外 —— 清空 tasks/ 时不能连带清掉，否则任务 id 会被复用
# （跟着 SA_ROOT 走：沙箱里分配的任务 id 不会吃掉生产的计数器）
COUNTER_FILE = SA_ROOT / ".task-counter"


def next_task_id() -> str:
    """分配任务 id，**永不复用**（历史任务被清掉后也不会再分配同一个 id）。

    复用 id 会让旧回执/旧指派串到新任务上，所以这里用独立的计数器文件。
    """
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    used = {p.name for p in TASKS_DIR.iterdir() if p.is_dir()}
    used |= {p.name for p in (ROOT / "z-archive").iterdir() if p.is_dir()} if (ROOT / "z-archive").exists() else set()
    n = 0
    if COUNTER_FILE.exists():
        try:
            n = int(COUNTER_FILE.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            n = 0
    # 计数器落后于实际目录时，向后跳过
    idx = 1
    while f"T{idx:03d}" in used:
        idx += 1
    n = max(n, idx - 1)
    n += 1
    COUNTER_FILE.write_text(str(n), encoding="utf-8")
    return f"T{n:03d}"


def find_assignment(assignment_id: str, task_hint: str | None = None):
    """按 id 找指派。

    assignment id 是**任务内序号**（A01 在每个任务里都有一份），所以：
      - 传 "T009/A01" 复合 id → 精确定位（推荐）
      - 传 "A01" + task_hint     → 在指定任务里找
      - 只传 "A01" 且全局唯一     → 允许
      - 只传 "A01" 且不唯一       → 报错并列出候选
    """
    task_id = None
    aid = assignment_id
    if "/" in assignment_id:
        task_id, aid = assignment_id.split("/", 1)
    hits = []
    for d in sorted(TASKS_DIR.glob("T*")):
        if task_id and d.name != task_id:
            continue
        if task_hint and d.name != task_hint:
            continue
        p = d / STATE_FILE
        if not p.exists():
            continue
        st = json.loads(p.read_text(encoding="utf-8"))
        for asg in st.get("assignments", []):
            if asg["id"] == aid:
                hits.append((st, asg))
    if not hits:
        die(f"assignment not found: {assignment_id}")
    if len(hits) > 1:
        die(
            f"ambiguous assignment id {aid}（存在于 {[h[0]['task_id'] for h in hits]}）。"
            f"请用复合 id，如 {hits[0][0]['task_id']}/{aid}"
        )
    return hits[0]


# ---------------------------------------------------------------- commands


def find_task_by_message(message_id: str) -> str | None:
    """按飞书 message_id 找已有任务（去重用）。"""
    if not message_id:
        return None
    for d in sorted(TASKS_DIR.glob("T*")):
        sp = d / STATE_FILE
        if not sp.exists():
            continue
        st = json.loads(sp.read_text(encoding="utf-8"))
        if (st.get("source_ref") or {}).get("message_id") == message_id:
            return st["task_id"]
    return None


def normalize_goal(t: str) -> str:
    return "".join(ch for ch in (t or "").lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")[:120]


def cmd_new_task(a: argparse.Namespace) -> None:
    by = getattr(a, "by", "human") or "human"

    # 护栏 0：常驻 router 进程（SA_ACTOR=router）无权新建任务。
    # 常驻 session 里堆着历次指令，模型容易把旧指令重建成新任务；
    # 任务应该只由桥接进程（飞书）或人（看板）创建。
    if os.environ.get("SA_ACTOR") == "router" and not getattr(a, "allow_new_task", False):
        die("常驻 router 进程不允许新建任务：任务由 lark_bridge（飞书）或人（看板）创建。"
            "你只负责拆解和派发已有任务。确需新建请由人操作。")

    # 护栏 1：飞书来源的任务只允许 lark_bridge 创建
    # （router 在常驻 session 里容易把旧指令重建成新任务，这里从机制上堵死）
    if (getattr(a, "source", "manual") == "feishu") and by != "bridge":
        die("飞书来源的任务只能由 lark_bridge 创建（请加 --by bridge）；"
            "router 不要重复建任务 —— 被 task.created 唤醒时任务已经存在。")

    return create_task_internal(
        title=a.title, goal=a.goal, acceptance=a.acceptance, source=a.source or "manual",
        source_ref=a.source_ref, context=a.context, context_file=a.context_file,
        by=by, task_id=a.id, notify_created=True)


def create_task_internal(*, title: str, goal: str, acceptance: str = "", source: str = "manual",
                         source_ref: list | None = None, context: list | None = None,
                         context_file: list | None = None, by: str = "human",
                         task_id: str | None = None, notify_created: bool = True) -> str:
    """建任务的内部实现。

    CLI 入口 cmd_new_task 有护栏（router 不能直接建任务）；
    这里供 Task Center 在落地 Decision 时调用 —— 那是「执行决策」，不是 router 自己动手。
    """
    src_ref = {}
    for kv in source_ref or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            src_ref[k.strip()] = v.strip()
    # 按 message_id 去重：**只对桥接进程建的任务生效**。
    # Router 建的���务不能这么去重 —— 一条用户消息可能拆出多个不同目标的任务，
    # 按消息号去重会把不同目标硬塞进同一个任务（目标与标题对不上）。
    mid = src_ref.get("message_id") if by == "bridge" else None
    if mid:
        for d in sorted(TASKS_DIR.glob("T*")):
            sp = d / STATE_FILE
            if not sp.exists():
                continue
            st0 = json.loads(sp.read_text(encoding="utf-8"))
            if (st0.get("source_ref") or {}).get("message_id") == mid:
                print(json.dumps({"task_id": st0["task_id"], "deduped": True,
                                  "reason": "message_id 已存在"}, ensure_ascii=False))
                return st0["task_id"]

    # 目标文本去重 —— 防止把旧指令镜像成新任务
    ng = normalize_goal(goal)
    if ng:
        for d in sorted(TASKS_DIR.glob("T*")):
            sp = d / STATE_FILE
            if not sp.exists():
                continue
            st0 = json.loads(sp.read_text(encoding="utf-8"))
            if normalize_goal(st0.get("goal", "")) == ng:
                print(json.dumps({"task_id": st0["task_id"], "deduped": True,
                                  "reason": "目标文本与已有任务重复"}, ensure_ascii=False))
                return st0["task_id"]

    task_id = task_id or next_task_id()
    if not valid_task_id(task_id):
        die(f"invalid task id: {task_id!r}（应为 T + 至少 3 位数字）")
    d = task_dir(task_id)
    (d / "context").mkdir(parents=True, exist_ok=True)
    (d / "notes").mkdir(parents=True, exist_ok=True)
    ch = CHANNELS_DIR / task_id / "instructions"
    ch.mkdir(parents=True, exist_ok=True)
    (CHANNELS_DIR / task_id / "responses").mkdir(parents=True, exist_ok=True)
    (CHANNELS_DIR / task_id / "notes").mkdir(parents=True, exist_ok=True)

    ctx_files = []
    for i, f in enumerate(context_file or [], start=1):
        src = pathlib.Path(f).expanduser().resolve()
        if not src.is_file():
            die(f"context file not found: {f}")
        dst = d / "context" / f"{i:02d}-{src.name}"
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        ctx_files.append(str(dst))

    ctx_text = "\n\n".join(context or [])
    if ctx_text:
        (d / "context" / "brief.md").write_text(ctx_text + "\n", encoding="utf-8")
        ctx_files.append(str(d / "context" / "brief.md"))

    acceptance = (acceptance or "").strip()
    state = {
        "task_id": task_id,
        "title": title,
        "goal": goal,
        "status": "open",
        "source": source or "manual",
        "source_ref": src_ref,
        "created_at": now(),
        "updated_at": now(),
        "context_files": ctx_files,
        "acceptance_lines": [acceptance] if acceptance else [],
        "completion_policy": {"require_approval": True, "evidence_required": True},
        "task_notes": "",
        "assignments": [],
    }
    save_state(task_id, state)
    log_event(task_id, "task_created", source or "human", f"任务已建立：{title}")
    if notify_created:
        notify("task.created", task_id,
               source=state["source"],
               title=state["title"],
               goal=state["goal"],
               source_ref=state.get("source_ref") or {},
               text=state["goal"])
    print(json.dumps({"task_id": task_id, "task_md": str(d / "task.md"), "state": str(d / STATE_FILE)}, ensure_ascii=False))
    return task_id


def cmd_assign(a: argparse.Namespace) -> None:
    state = load_state(a.task)
    task_id = state["task_id"]
    n = len(state["assignments"]) + 1
    aid = f"A{n:02d}"

    # ── Registry 校验：worker 在册 / 启用 / 并发未超 ──
    ok, why = core.validate_agent(a.worker)
    if not ok:
        die(f"派工被 registry 拒绝：{why}")
    others = []
    for d in sorted(TASKS_DIR.glob("T*")):
        sp = d / STATE_FILE
        if sp.exists():
            others.append(json.loads(sp.read_text(encoding="utf-8")))
    limit = core.max_parallel_for(a.worker)
    if core.count_active(a.worker, others) >= limit:
        die(f"派工被 registry 拒绝：worker '{a.worker}' 并发已达上限 {limit}")

    # 护栏：同一任务已有「在跑/待执行」的指派，且摘要相同 → 不重复派
    brief = (a.brief or "").strip()
    for x in state["assignments"]:
        if x["status"] in ("assigned", "running") and brief and (x.get("brief") or "").strip() == brief:
            die(f"任务 {task_id} 已有在跑的同类指派 {x['id']}（{x['status']}），不重复派：{brief}")

    src = pathlib.Path(a.instruction_file).expanduser().resolve()
    if not src.is_file():
        die(f"instruction file not found: {a.instruction_file}")
    instr_dst = CHANNELS_DIR / task_id / "instructions" / f"{aid}.md"
    instr_dst.parent.mkdir(parents=True, exist_ok=True)
    body = src.read_text(encoding="utf-8")
    if a.brief:
        body = f"<!-- brief: {a.brief} -->\n\n" + body
    instr_dst.write_text(body, encoding="utf-8")

    resp_dst = CHANNELS_DIR / task_id / "responses" / f"{aid}.md"
    resp_dst.write_text(
        f"# 回执 {aid}\n\n- assignment: `{aid}`\n- worker: `{a.worker}`\n"
        f"- status: `pending`\n- updated_at: {now()}\n\n## 结论\n\n（待 worker 填写）\n",
        encoding="utf-8",
    )

    state["assignments"].append(
        {
            "id": aid,
            "worker": a.worker,
            "status": "assigned",
            "instruction": str(instr_dst),
            "response": str(resp_dst),
            "brief": a.brief or "",
            "artifacts": [],
            "dispatched_at": now(),
            "completed_at": None,
            "pending_ack": False,
            "review": None,
        }
    )
    state["status"] = recompute_task_status(state)
    save_state(task_id, state)
    log_event(task_id, "assigned", "super-agent", f"{aid} 派给 {a.worker}", assignment=aid, worker=a.worker)
    # 派发事件不投递给 router（避免 router 被自己的动作唤醒）
    print(json.dumps({"assignment": aid, "task": task_id, "instruction": str(instr_dst), "response": str(resp_dst)}, ensure_ascii=False))


def cmd_respond(a: argparse.Namespace) -> None:
    status = {
        "done": "done",
        "completed": "done",
        "complete": "done",
        "success": "done",
        "failed": "failed",
        "error": "failed",
        "blocked": "blocked",
        "waiting_user_input": "waiting_user_input",
        "need_input": "waiting_user_input",
        "ask": "waiting_user_input",
    }[a.status]
    state, asg = find_assignment(a.assignment, getattr(a, "task", None))
    task_id = state["task_id"]

    resp_path = pathlib.Path(asg["response"])
    footer = [
        "",
        "---",
        "",
        f"- [sa.py 登记] status: `{status}`",
        f"- [sa.py 登记] updated_at: {now()}",
    ]
    if a.artifact:
        footer += [f"- [sa.py 登记] 产出：`{p}`" for p in a.artifact]
    footer.append("")

    if resp_path.exists():
        existing = resp_path.read_text(encoding="utf-8").rstrip()
        resp_path.write_text(existing + "\n" + "\n".join(footer), encoding="utf-8")
    else:
        body = [
            f"# 回执 {asg['id']}",
            "",
            f"- assignment: `{asg['id']}`",
            f"- worker: `{asg['worker']}`",
            f"- task: `{task_id}`",
            f"- status: `{status}`",
            f"- updated_at: {now()}",
            "",
            "## 结论",
            "",
            a.summary,
            "",
            "## 产出",
            "",
        ]
        body += [f"- `{p}`" for p in (a.artifact or [])] or ["- （无文件产出）"]
        body.append("")
        resp_path.write_text("\n".join(body) + "\n" + "\n".join(footer), encoding="utf-8")

    # 迟到/重复回执**不得覆盖**已终结的状态（方案 §9.4：迟到 Report 标 stale）
    # 之前是无条件 asg["status"] = status —— 一个迟到的 failed 会把已 done 的
    # 指派打回去，一个迟到的 done 会把已 cancelled / approved 的指派复活。
    prior = asg.get("status")
    stale = prior in ("done", "approved", "cancelled", "failed")
    if stale:
        log_event(task_id, "report_stale", "worker",
                  f"{asg['id']} 迟到回执（{status}）——指派已是 {prior}，不改状态",
                  assignment=asg["id"], status=status, prior_status=prior)
        print(json.dumps({"task": task_id, "assignment": asg["id"], "status": prior,
                          "stale_report": True, "reported": status,
                          "task_status": state.get("status")}, ensure_ascii=False))
        return
    asg["status"] = status
    asg["completed_at"] = now()
    asg["artifacts"] = a.artifact or []
    if status == "done":
        asg["review"] = "pending"
    if status == "waiting_user_input":
        asg["review"] = "pending"
        asg["question"] = a.summary
        # 「在等 A 回答」→ 自动把 A 加进本任务的临时授权名单，
        # 这样 A 的回复会被受理（准入判定放行 + router 知道该延续本任务）。
        ask_name = (getattr(a, "ask", None) or "").strip()
        if ask_name:
            try:
                import policy
                oid = policy.resolve_person(ask_name)
                if oid:
                    ref = state.get("source_ref") or {}
                    g = policy.grant_actor(task_id, oid, name=ask_name,
                                           reason=f"任务在等其回答：{a.summary[:120]}",
                                           chat_id=ref.get("chat_id") or "",
                                           question=a.summary[:200])
                    log_event(task_id, "actor_granted", "worker-runner",
                              f"指派在等「{g.get('name')}」回答，已临时授权其在本任务会话内发言",
                              assignment=asg["id"], actor_open_id=oid)
                    state = load_state(task_id)      # grant_actor 改过文件，重新读
                    asg = [x for x in state["assignments"] if x["id"] == asg["id"]][0]
                else:
                    log_event(task_id, "actor_grant_failed", "worker-runner",
                              f"人员目录里找不到「{ask_name}」，未授权", assignment=asg["id"])
            except Exception as e:
                log_event(task_id, "actor_grant_failed", "worker-runner",
                          f"临时授权失败：{str(e)[:120]}", assignment=asg["id"])
    state["status"] = recompute_task_status(state)
    save_state(task_id, state)
    log_event(task_id, "responded", "worker", f"{asg['id']} 回执：{status}", assignment=asg["id"], status=status)
    notify("assignment.completed", task_id, assignment=asg["id"], status=status, worker=asg["worker"],
           summary=a.summary, artifacts=a.artifact or [], response=str(resp_path))
    print(json.dumps({"task": task_id, "assignment": asg["id"], "status": status, "task_status": state["status"]}, ensure_ascii=False))


def decision_path(task_id: str) -> pathlib.Path:
    return task_dir(task_id) / "decision.json"


def _source_ref_kvlist(ref: dict) -> list[str]:
    """把 source_ref dict 转成 new-task 的 k=v 列表形式。"""
    return [f"{k}={v}" for k, v in (ref or {}).items() if v]


def _ref_dict(ref) -> dict:
    """把 source_ref（dict 或 ["k=v"] 列表）统一成 dict。"""
    if isinstance(ref, dict):
        return {k: str(v) for k, v in ref.items() if v}
    out: dict = {}
    for kv in ref or []:
        if isinstance(kv, str) and "=" in kv:
            k, v = kv.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def round_source_ref() -> dict:
    """本轮事件的来源引用（用户消息轮：飞书 chat/message id）—— 用来把回复发回原会话。"""
    rf = ROUTER_DIR / "round.json"
    if rf.exists():
        try:
            rnd = json.loads(rf.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return rnd.get("source_ref") or {}
    return {}


# 每轮开始由 router 刷新（apply_decision 读它给 outbox 定位投递目标）
ROUND_SOURCE_REF: dict = {}


# 指派结束类事件：这些轮次默认只做复核与回话
ASSIGNMENT_END_KINDS = ("assignment.completed", "assignment.failed")

# 链式派工配置：允许在「指派结束」轮里接着派下一个 worker，
# 但**连续**发起次数有上限 —— 没有上限就会变成
# router → worker → router → worker 的无限派生，任务永远做不完。
CHAIN_CONFIG_FILE = ROUTER_DIR / "chain.json"
DEFAULT_CHAIN_CONFIG = {"enabled": True, "max_consecutive_dispatches": 2}


def load_chain_config() -> dict:
    """读链式派工配置（坏文件退回默认值，不让配置问题挡住派工）。"""
    cfg = dict(DEFAULT_CHAIN_CONFIG)
    try:
        if CHAIN_CONFIG_FILE.exists():
            raw = json.loads(CHAIN_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg["enabled"] = bool(raw.get("enabled", cfg["enabled"]))
                n = raw.get("max_consecutive_dispatches", cfg["max_consecutive_dispatches"])
                try:
                    n = int(n)
                except (TypeError, ValueError):
                    n = cfg["max_consecutive_dispatches"]
                cfg["max_consecutive_dispatches"] = max(0, min(10, n))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_chain_config(enabled: bool, max_consecutive: int) -> dict:
    """写链式派工配置（看板管理页面用）。"""
    cfg = {"enabled": bool(enabled),
           "max_consecutive_dispatches": max(0, min(10, int(max_consecutive)))}
    CHAIN_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHAIN_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
    return cfg


def _empty_state() -> dict:
    """空任务状态 —— 用户消息轮（user.message）没有既有任务上下文时用。"""
    return {"task_id": "", "title": "", "goal": "", "status": "open", "source": "router",
            "source_ref": {}, "assignments": [], "acceptance_lines": [],
            "context_files": [], "applied_triggers": [], "applied_reviews": [],
            "chain_dispatch_count": 0}


def new_worker_session_id(task_id: str, aid: str, agent: str) -> str:
    """生成 worker session id —— worker_runner 用它 `pi --print --session-id` 复现/延续会话。"""
    return f"w-{task_id}-{aid}-{agent}-{uuid.uuid4().hex[:6]}"


def collect_session_sources() -> list[dict]:
    """收集所有可复用的 worker session 候选（供 Decision 的 sessionFrom 校验）。"""
    out = []
    for d in sorted(TASKS_DIR.glob("T*")):
        sp = d / STATE_FILE
        if not sp.exists():
            continue
        try:
            st = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for a in st.get("assignments") or []:
            out.append({"task": st.get("task_id") or d.name,
                        "assignment": a.get("id"), "agent": a.get("worker"),
                        "session": a.get("worker_session_id") or "",
                        "status": a.get("status")})
    return out


def apply_decision(task_id: str, doc: dict, actor: str = "router") -> dict:
    """Task Center 执行引擎：校验 Decision 并落地。

    - 一条 Decision 可含多个 item，**逐条独立校验与生效**；
      单条被拒不回滚其它有效条目。
    - 整体 Schema 非法 → 整批不执行。
    - Router 只提交 Decision，状态由这里写。
    """
    # task_id 可以为空 —— 用户消息轮（user.message）没有既有任务上下文，
    # 此时 Decision 自己决定新建任务还是沿用某个已有任务。
    global ROUND_SOURCE_REF
    ROUND_SOURCE_REF = round_source_ref()
    state = load_state(task_id) if task_id else _empty_state()
    # ── 幂等：同一 triggerId 重复提交时，reviews 不重复生效 ──
    trig = doc.get("triggerId")
    applied_triggers = state.get("applied_triggers", [])
    # 幂等粒度 = (triggerId, assignment)：同一 trigger 下对同一指派只生效一次。
    # 不能按 trigger 整批跳过 reviews：worker 回执可能在同轮 trigger 之后才到达，
    # 若整批被跳过，指派会永远停在 done / needs_human，无法放行。
    applied_review_keys = set(state.get("applied_reviews") or [])
    allowed_triggers = [t for t in (doc.get("_allowed_triggers") or [])] if isinstance(doc, dict) else []
    known_task_ids = sorted(p.name for p in TASKS_DIR.glob("T*") if (p / STATE_FILE).exists())
    good, errs = core.validate_decision(
        doc,
        allowed_triggers=allowed_triggers or None,
        known_task_ids=known_task_ids,
        known_assignments=[x["id"] for x in state["assignments"]],
        session_sources=collect_session_sources(),
    )
    result = {"task": task_id, "actor": actor, "applied": [], "rejected": [], "ts": now()}
    created_tasks: list[str] = []          # 本轮 Decision 新建的任务（outbox 自动挂靠用）

    def _reject(code: str, msg: str):
        """记录一条被拒理由（单项被拒不影响其它条目）。"""
        result["rejected"].append({"reason_code": code, "message": msg})

    # 整批 Schema 非法 → 不执行
    fatal = [e for e in errs if e["reason_code"] in
             ("schema_invalid", "schema_version", "missing_trigger")]
    if fatal:
        result["rejected"] = list(errs)
        result["fatal"] = True
        decision_path(task_id).write_text(
            json.dumps({"submitted": doc, "result": result}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        log_event(task_id, "decision_rejected", actor,
                  f"Decision 整体校验失败：{fatal[0]['message']}", reason_code=fatal[0]["reason_code"])
        return result

    # 条目级校验失败 → 逐条记录（不影响其它条目生效）
    for e in errs:
        if e not in fatal:
            _reject(e["reason_code"], e["message"])

    # ── 指派结束的轮次：可以再派工，但**连续**次数有上限 ──
    # 早先这里是一律禁止：怕形成 router → worker → router → worker 的无限链式派工，
    # 一次误判就把任务拖成永远做不完（连续派生新指派，任务永远到不了 completed）。
    # 现在允许接着派（router 复核完 worker 结果后发现还有活要干，是合理场景），
    # 但连续次数受配置限制（默认 2 次），超出就拒。
    # 计数口径：自**上一次用户消息**以来，在指派结束轮里连续发起的派工次数；
    # 用户新消息 = 新需求 → 计数清零。
    round_kind = (doc.get("_round_kind") or "").strip()
    chain_cfg = load_chain_config()
    if round_kind in ASSIGNMENT_END_KINDS and good:
        max_chain = int(chain_cfg["max_consecutive_dispatches"])
        used = int(state.get("chain_dispatch_count") or 0)
        if not chain_cfg["enabled"]:
            for it in good:
                _reject("dispatch_in_assignment_round",
                        f"本轮是 {round_kind}，只允许复核结论与回话，不允许再派工"
                        f"（{it.get('agentId')} / {it.get('goal','')[:40]}）。"
                        f"需要继续做事请等用户新消息或人工干预触发新一轮。")
            good = []
        elif used >= max_chain:
            for it in good:
                _reject("chain_dispatch_limit",
                        f"连续派工已 {used} 次（上限 {max_chain}），本轮不再派工"
                        f"（{it.get('agentId')} / {it.get('goal','')[:40]}）。"
                        f"要继续请等用户新消息，或到看板上调高链式派工上限。")
            good = []

    # ── reviews：确认 / 打回 worker 结果 ──
    # 注意：末尾会重新 load_state 再保存（因为 items 可能往同一任务追加指派），
    # 这里把 reviews 的结论记下来，末尾重新套用，避免被覆盖回旧值。
    review_updates = {}
    for rv in (doc.get("reviews") or []):
        aid = (rv.get("assignment") or "").strip()
        verdict = rv.get("verdict")
        review_key = f"{trig}:{aid}" if trig else ""
        if review_key and review_key in applied_review_keys:
            # 同一 trigger 对同一指派的复核已生效过，幂等跳过（不重复改状态）
            result["applied"].append({"kind": "review.idempotent",
                                      "assignment": aid, "verdict": verdict})
            continue
        asg = next((x for x in state["assignments"] if x["id"] == aid), None)
        if not asg:
            _reject("unknown_assignment", f"指派 {aid} 不存在")
            continue
        if verdict == "approve":
            if not (asg.get("artifacts") or []):
                _reject("no_evidence", f"{aid} 没有产出证据（artifact），不能放行")
                continue
            asg["status"] = "approved"
            asg["review"] = "approved"
            if review_key:
                applied_review_keys.add(review_key)
            asg["completed_at"] = now()
            review_updates[aid] = {"status": "approved", "review": "approved",
                                   "completed_at": asg["completed_at"]}
            result["applied"].append({"kind": "review.approve", "assignment": aid})
            log_event(task_id, "assignment_approved", actor,
                      f"{aid} 确认完成：{rv.get('note','')}", assignment=aid)
        elif verdict == "reject":
            asg["status"] = "rework"
            asg["review"] = "rejected"
            if review_key:
                applied_review_keys.add(review_key)
            asg["pending_ack"] = False
            review_updates[aid] = {"status": "rework", "review": "rejected",
                                   "pending_ack": False}
            note = rv.get("note") or ""
            instr = pathlib.Path(asg["instruction"])
            with instr.open("a", encoding="utf-8") as f:
                f.write(f"\n\n---\n\n## 复核意见（{now()}）\n\n{note}\n\n> 请按此意见重做，并重新写回执。\n")
            result["applied"].append({"kind": "review.reject", "assignment": aid, "note": note})
            log_event(task_id, "assignment_rework", actor,
                      f"{aid} 被要求重做：{note}", assignment=aid)

    # ── items：派工 ──
    #   create   → 新建任务并派工（taskId 必须为空）
    #   continue → 沿用已有任务（taskId 必填）
    #   sessionFrom → 继承某个已有指派的 worker session（同一 agent 的上下文延续）
    for it in good:
        action = it.get("taskAction")
        agent = (it.get("agentId") or "").strip()
        target_task = task_id
        if action == "create":
            # 新任务：标题优先用 item.title，否则取 goal 首行
            title = (it.get("title") or "").strip() or (it.get("goal") or "").strip().splitlines()[0][:40]
            try:
                target_task = create_task_internal(
                    title=title or "新任务",
                    goal=(it.get("goal") or "").strip(),
                    acceptance=(it.get("acceptance") or [""])[0] if it.get("acceptance") else "",
                    source="router",
                    source_ref=_source_ref_kvlist(ROUND_SOURCE_REF),
                    context=[],
                    context_file=[],
                    by=actor,
                    notify_created=False,   # 不再发 task.created 事件（router 自己在处理）
                )
            except Exception as e:
                _reject("task_create_failed", f"新建任务失败：{e}")
                continue
            result["applied"].append({"kind": "task.create", "task": target_task})
            created_tasks.append(target_task)
        else:
            target_task = (it.get("taskId") or "").strip()
            if not (task_dir(target_task) / STATE_FILE).exists():
                _reject("unknown_task", f"taskId '{target_task}' 不存在")
                continue
        tstate = load_state(target_task)
        n = len(tstate["assignments"]) + 1
        aid = f"A{n:02d}"
        spec = core.worker_spec(agent) or {}
        resp = CHANNELS_DIR / target_task / "responses" / f"{aid}.md"
        instr = CHANNELS_DIR / target_task / "instructions" / f"{aid}.md"
        instr.parent.mkdir(parents=True, exist_ok=True)
        resp.parent.mkdir(parents=True, exist_ok=True)
        body = core.render_instruction(
            task_id=target_task, assignment_id=aid, agent_id=agent,
            goal=it.get("goal", ""),
            requirements=list(it.get("requirements") or []),
            acceptance=list(it.get("acceptance") or tstate.get("acceptance_lines") or []),
            context_files=list(tstate.get("context_files") or []),
            background=tstate.get("goal", ""),
            trigger_id=it.get("triggerId", ""),
            readonly=bool(spec.get("readonly")),
            response_path=str(resp),
        )
        instr.write_text(body, encoding="utf-8")
        resp.write_text(
            f"# 回执 {aid}\n\n- assignment: `{aid}`\n- agent: `{agent}`\n"
            f"- status: `pending`\n- updated_at: {now()}\n\n## 结论\n\n（待 worker 填写）\n",
            encoding="utf-8")
        # worker session：sessionFrom 继承，否则新建一个
        sfrom = (it.get("sessionFrom") or "").strip()
        inherited = ""
        if sfrom:
            src = next((x for x in tstate["assignments"] if x["id"] == sfrom), None)
            if src and (src.get("worker_session_id") or "").strip():
                inherited = src["worker_session_id"]
            else:
                _reject("session_not_ready", f"{target_task}/{sfrom} 还没有 worker session")
                continue
        session_id = inherited or new_worker_session_id(target_task, aid, agent)
        tstate["assignments"].append({
            "id": aid, "worker": agent, "status": "assigned",
            "instruction": str(instr), "response": str(resp),
            "brief": (it.get("reason") or "")[:120],
            "artifacts": [], "dispatched_at": now(), "completed_at": None,
            "pending_ack": False, "review": None,
            "worker_session_id": session_id,
            "inherits_session_from": sfrom or None,
            # 触发这次工作的那条用户消息 —— 桥接把表情（收到/派发/完成）
            # 挂在它上面，形成一条完整的进度轨迹
            "origin_ref": _ref_dict(ROUND_SOURCE_REF),
        })
        tstate["status"] = recompute_task_status(tstate)
        # 链式派工计数：
        #   · 用户新消息轮 → 清零（新需求，链断了）
        #   · 指派结束轮里派工 → +1（连续派工，上限由配置管）
        if round_kind == "user.message":
            tstate["chain_dispatch_count"] = 0
        elif round_kind in ASSIGNMENT_END_KINDS:
            tstate["chain_dispatch_count"] = int(tstate.get("chain_dispatch_count") or 0) + 1
        save_state(target_task, tstate)
        result["applied"].append({"kind": "dispatch", "task": target_task,
                                   "assignment": aid, "agent": agent,
                                   "session": session_id,
                                   "chain": round_kind in ASSIGNMENT_END_KINDS,
                                   "chain_count": tstate.get("chain_dispatch_count", 0),
                                   "inherited_session": bool(inherited)})
        log_event(target_task, "assigned", actor,
                  f"{aid} 派给 {agent}（{it.get('reason','')}）", assignment=aid, worker=agent)
        # 派发是 router 自己的动作，不需要再唤醒它（只记任务时间线）

    # ── outbox：对外说话 ──
    for ob in doc.get("outbox") or []:
        # 投递目标：优先本轮事件来源（用户最新那条消息），其次任务自身的来源。
        # 这样「延续任务」的结果会回给用户最新的消息，而不是任务最初那条。
        ob_task = (ob.get("task") or task_id or (created_tasks[0] if created_tasks else ""))
        ref = _ref_dict(ROUND_SOURCE_REF)
        if not (ref.get("chat_id") or ref.get("message_id")):
            # 本轮事件没有会话标识（worker 回执轮、人工干预轮）→ 回落到任务自身的来源
            for cand in [state.get("source_ref")] + \
                        [load_state(t).get("source_ref") for t in created_tasks if t]:
                d = _ref_dict(cand)
                if d.get("chat_id") or d.get("message_id"):
                    ref = d
                    break
        sess = core.resolve_session(ob_task, ref.get("chat_id", ""), ref.get("message_id", ""),
                                    ref.get("thread_id", ""), ref.get("root_id", ""))
        rec = core.add_outbox(
            target=ob.get("target", "thread"), purpose=ob.get("purpose", "reply"),
            text=ob.get("text", ""), task=ob_task,
            assignment=ob.get("assignment", ""),
            channel="feishu", chat_id=sess["chat_id"],
            message_id=sess["reply_to"], sender=sess["sender"],
            actor=actor)
        result["applied"].append({"kind": "outbox", "id": rec["id"],
                                   "purpose": rec["purpose"],
                                   "session_source": rec.get("session_source", "")})

    if task_id:
        state = load_state(task_id)
        for aid, upd in review_updates.items():
            for x in state["assignments"]:
                if x["id"] == aid:
                    x.update(upd)
        state["status"] = recompute_task_status(state)
        if applied_review_keys:
            prev = list(state.get("applied_reviews") or [])
            for k in applied_review_keys:
                if k not in prev:
                    prev.append(k)
            state["applied_reviews"] = prev[-100:]
        if trig and trig not in state.get("applied_triggers", []):
            state.setdefault("applied_triggers", []).append(trig)
            state["applied_triggers"] = state["applied_triggers"][-50:]
        # 用户新消息 → 链式派工计数清零（新需求，链断了）
        if round_kind == "user.message":
            state["chain_dispatch_count"] = 0
        save_state(task_id, state)
    # Decision 留档：任务轮次存到任务下；无任务轮次（用户消息轮）存到 router 目录
    dp = decision_path(task_id) if task_id else (ROUTER_DIR / "decisions" / f"{trig or 'unknown'}.json")
    dp.parent.mkdir(parents=True, exist_ok=True)
    dp.write_text(
        json.dumps({"submitted": doc, "result": result}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return result


def cmd_tasks_list(a: argparse.Namespace) -> None:
    """列出任务（router 判断"沿用之前的哪个任务"用）。

    默认按更新时间倒序，只回摘要：id / 标题 / 状态 / 更新时间 / 指派一览（含 worker session）。
    """
    limit = getattr(a, "limit", 15) or 15
    rows = []
    for d in TASKS_DIR.glob("T*"):
        sp = d / STATE_FILE
        if not sp.exists():
            continue
        try:
            st = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        rows.append(st)
    rows.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    out = []
    for st in rows[:limit]:
        asgs = [{
            "id": a.get("id"), "agent": a.get("worker"), "status": a.get("status"),
            "session": a.get("worker_session_id") or "",
            "goal": (a.get("brief") or "")[:60],
        } for a in (st.get("assignments") or [])]
        out.append({
            "task": st.get("task_id"), "title": st.get("title"),
            "status": st.get("status"), "goal": (st.get("goal") or "")[:120],
            "updated_at": st.get("updated_at"), "source": st.get("source"),
            "assignments": asgs,
        })
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_submit_decision(a: argparse.Namespace) -> None:
    """Router 提交 Decision（只提交，不直接写状态）。"""
    src = pathlib.Path(a.file).expanduser().resolve()
    if not src.is_file():
        die(f"decision file not found: {a.file}")
    try:
        doc = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"Decision 不是合法 JSON: {e}")
    # ── 轮次上下文：优先用 --task；没给就用 round.json 里的本轮任务（用户消息轮为空）──
    round_file = ROUTER_DIR / "round.json"
    rnd = {}
    if round_file.exists():
        try:
            rnd = json.loads(round_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            rnd = {}
    task_id = (a.task or "").strip() or (rnd.get("task") or "")
    round_kind = rnd.get("kind") or ""

    # 允许的 trigger 集合来自本轮事件
    triggers = []
    if a.triggers:
        triggers = [t for t in a.triggers.split(",") if t]
    else:
        for line in (ROUTER_INBOX.read_text(encoding="utf-8").splitlines()[-50:] if ROUTER_INBOX.exists() else []):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("id") and (ev.get("task") == task_id or not task_id):
                triggers.append(ev["id"])
    # ── 轮次约束：Decision 只能落在 router 本轮被唤醒的那件事上 ──
    rt = rnd.get("trigger_id")
    if rt and doc.get("triggerId") and doc["triggerId"] != rt:
        die(f"Decision 的 triggerId '{doc['triggerId']}' 不是本轮 router 被唤醒的事件"
            f"（{rt}）。只能提交当前轮次的决策。")
    if rt and rt not in triggers:
        triggers = list(triggers) + [rt]
    # 任务轮次：只能处理本轮任务；用户消息轮：可以新建任务，也可以沿用任意已有任务
    if round_kind and round_kind != "user.message":
        rtask = rnd.get("task")
        if rtask and task_id and task_id != rtask:
            die(f"只能为当前轮次的任务 {rtask} 提交 Decision（你提交的是 {task_id}）。")
        if rtask and not task_id:
            die(f"当前轮次的任务是 {rtask}，请用 --task {rtask} 提交。")

    doc["_allowed_triggers"] = triggers
    doc["_round_kind"] = round_kind
    result = apply_decision(task_id, doc, actor=a.actor or os.environ.get("SA_ACTOR") or "router")
    result.pop("_allowed_triggers", None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("fatal"):
        raise SystemExit(2)


def cmd_outbox_list(a: argparse.Namespace) -> None:
    print(json.dumps(core.list_outbox(a.status or ""), ensure_ascii=False, indent=2))


def cmd_outbox_add(a: argparse.Namespace) -> None:
    # 会话标识来源：本轮事件来源（用户最新那条消息）→ 任务自身的来源。
    # 两处都没有时，core.add_outbox 会再从 task.source_ref 兜一次。
    ref = _ref_dict(round_source_ref())
    if not (ref.get("chat_id") or ref.get("message_id")) and a.task:
        ref = core.task_source_ref(a.task)
    sess = core.resolve_session(a.task or "", ref.get("chat_id", ""), ref.get("message_id", ""),
                                ref.get("thread_id", ""), ref.get("root_id", ""))
    rec = core.add_outbox(target=a.target, purpose=a.purpose, text=a.text,
                           task=a.task or "", assignment=a.assignment or "",
                           channel="feishu", chat_id=sess["chat_id"],
                           message_id=sess["reply_to"],
                           sender=sess["sender"],
                           actor=a.actor or os.environ.get("SA_ACTOR") or "human")
    print(json.dumps(rec, ensure_ascii=False, indent=2))


def cmd_outbox_retry(a: argparse.Namespace) -> None:
    """把一条 dead_letter / failed 的外发记录重新放回待发队列。

    「无处可投」的记录不会自己恢复（缺的就是会话标识），只能人工补齐后重投：
    补了会话标识就补投，没补就再进 dead_letter —— 但**不会静默消失**。
    """
    rec = next((r for r in core.list_outbox() if r["id"] == a.id), None)
    if not rec:
        print(json.dumps({"ok": False, "error": f"outbox 记录不存在: {a.id}"},
                         ensure_ascii=False))
        raise SystemExit(1)
    sess = core.resolve_session(rec.get("task", ""), rec.get("chat_id", ""),
                                rec.get("reply_to", ""))
    rec2 = core.update_outbox(rec, status="pending", attempts=0,
                              next_run_at=core.now(), needs_manual=False,
                              last_error="",
                              note="人工重投" + ("" if (sess["chat_id"] or sess["reply_to"])
                                                 else "（仍缺会话标识）"),
                              chat_id=sess["chat_id"], reply_to=sess["reply_to"])
    print(json.dumps(rec2, ensure_ascii=False, indent=2))


def cmd_outbox_verify(a: argparse.Namespace) -> None:
    """对账：把「succeeded 但拿不出投递凭据 / 桥接日志里没有投递行」标为 unverified。

    outbox 的 succeeded 曾长期不可信：只要 send_* 不抛异常就写成功，而「投递」
    可能是被桩替掉的、甚至根本没发生。这个命令不改历史记录（不迁移、不改写），
    只产出一份对账清单供人工复核：

      依据 1  记录自带的投递凭据 delivery.message_id / delivery.request_id
              （新口径下由 deliver_outbox 在真实渠道返回成功后写入）
      依据 2  桥接日志里同 id 的「outbox 已投递 ob_…」行
      依据 3  目标会话 id 是否符合飞书会话形态（占位串直接可疑）

    用法：sa.py outbox-verify [--log PATH] [--out PATH] [--json]
    """
    import re
    recs = core.list_outbox()
    log_path = pathlib.Path(a.log) if a.log else (SA_ROOT / "logs" / "lark-bridge.log")
    delivered_ids: set[str] = set()
    if log_path.exists():
        try:
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "outbox 已投递 ob_" in line:
                    for m in re.finditer(r"ob_[0-9a-f]{6,}", line):
                        delivered_ids.add(m.group(0))
        except OSError:
            pass
    chat_re = re.compile(r"^oc_[0-9a-f]{32}$")
    rows = []
    counts = {"succeeded": 0, "verified": 0, "unverified": 0, "high": 0, "legacy_only": 0}
    for r in recs:
        if r.get("status") != "succeeded":
            continue
        counts["succeeded"] += 1
        d = r.get("delivery") or {}
        reasons = []
        if not (d.get("message_id") or d.get("request_id")):
            # 区分两种无凭据：落地前的旧口径记录（连 delivery 字段都没有）/ 渠道没给 id
            reasons.append("无投递凭据（旧口径记录，早于投递凭据落地）" if "delivery" not in r
                           else "无投递凭据（渠道未返回 id）")
        if delivered_ids and r["id"] not in delivered_ids:
            reasons.append("桥接日志无对应投递行")
        chat = str(r.get("chat_id") or "")
        if chat and not chat_re.match(chat):
            reasons.append("目标会话 id 形态非法")
        if reasons:
            counts["unverified"] += 1
            # 严重度：只有「缺凭据」是旧口径的普遍现象（字段落地前的记录都存在）；
            # 同时还有桥接日志无行 / 会话 id 形态非法，才是真该人工查的。
            severity = "high" if len(reasons) > 1 else "legacy_only"
            counts[severity] += 1
            rows.append({"id": r["id"], "task": r.get("task", ""),
                         "purpose": r.get("purpose", ""),
                         "sent_at": r.get("sent_at", ""),
                         "status": "unverified", "severity": severity,
                         "reasons": reasons,
                         "has_credential": bool(d.get("message_id") or d.get("request_id"))})
        else:
            counts["verified"] += 1
    report = {"ts": now(), "log": str(log_path),
              "log_exists": log_path.exists(),
              "counts": counts, "unverified": rows}
    if a.out:
        outp = pathlib.Path(a.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["report_file"] = str(outp)
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"succeeded {counts['succeeded']} 条：可核验 {counts['verified']} / "
              f"unverified {counts['unverified']}"
              f"（其中疑点较重 {counts['high']}、仅缺凭据的旧记录 {counts['legacy_only']}）")
        for row in sorted(rows, key=lambda x: (x["severity"] != "high", x["id"])):
            mark = "✗" if row["severity"] == "high" else "·"
            print(f"  {mark} {row['id']}（{row['purpose'] or '-'} / {row['task'] or '-'}）："
                  f"{'；'.join(row['reasons'])}")
        if not log_path.exists():
            print(f"  （桥接日志缺失：{log_path}，本次只按凭据/形态判定）")
        if a.out:
            print(f"对账清单已写入 {a.out}")


def cmd_list(a: argparse.Namespace) -> None:
    rows = []
    for d in sorted(TASKS_DIR.glob("T*")):
        p = d / STATE_FILE
        if not p.exists():
            continue
        st = json.loads(p.read_text(encoding="utf-8"))
        rows.append(
            {
                "task": st["task_id"],
                "title": st["title"],
                "status": st["status"],
                "assignments": [
                    {"id": x["id"], "worker": x["worker"], "status": x["status"], "pending_ack": x.get("pending_ack", False)}
                    for x in st["assignments"]
                ],
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def cmd_show(a: argparse.Namespace) -> None:
    st = load_state(a.task)
    print(json.dumps(st, ensure_ascii=False, indent=2))
    tm = task_dir(a.task) / "task.md"
    if tm.exists():
        print("\n--- task.md ---\n")
        print(tm.read_text(encoding="utf-8"))


def cmd_events(a: argparse.Namespace) -> None:
    print(json.dumps(read_events(a.task), ensure_ascii=False, indent=2))


def cmd_find(a: argparse.Namespace) -> None:
    """按内容片段找任务（用于避免同一条飞书消息被重复建任务）。"""
    needle = a.match.strip().lower()
    hits = []
    for d in sorted(TASKS_DIR.glob("T*")):
        sp = d / STATE_FILE
        if not sp.exists():
            continue
        st = json.loads(sp.read_text(encoding="utf-8"))
        hay = " ".join([
            st.get("title", ""), st.get("goal", ""),
            " ".join(st.get("acceptance_lines") or []),
            " ".join((st.get("source_ref") or {}).values()),
        ]).lower()
        if not needle or needle in hay:
            hits.append({
                "task": st["task_id"], "title": st["title"], "status": st["status"],
                "source": st.get("source", "manual"),
                "message_id": (st.get("source_ref") or {}).get("message_id"),
                "updated_at": st.get("updated_at"),
            })
    if a.source:
        hits = [h for h in hits if h["source"] == a.source]
    if a.status:
        hits = [h for h in hits if h["status"] == a.status]
    print(json.dumps(hits, ensure_ascii=False, indent=2))


# ------------------------------------------------------- human intervention


def intervene(task_id: str, assignment_id: str | None, action: str, text: str = "",
              to_worker: str = "", actor: str = "") -> dict:
    # actor 缺省时看进程身份：常驻 router / worker 进程内的自主动作不能算「人工干预」，
    # 否则 approve→ack→notify→唤醒 自己，形成反馈回路。
    actor = actor or os.environ.get("SA_ACTOR") or "human"
    state = load_state(task_id)
    asg = None
    if assignment_id:
        for x in state["assignments"]:
            if x["id"] == assignment_id:
                asg = x
                break
        if asg is None:
            die(f"assignment not found in {task_id}: {assignment_id}")

    ts = now()
    who = actor or "human"

    if action == "comment":
        if asg is None:
            die("comment 需要 --assignment")
        asg["pending_ack"] = True
        block = f"## [{ts}] 人工批注 · {assignment_id}\n\n{text}\n"
        append_note(task_id, assignment_id, block)
        log_event(task_id, "comment", who, f"{assignment_id} 人工批注", assignment=assignment_id, text=text)
        # 批注也要让正在跑的 worker 看得到
        instr = pathlib.Path(asg["instruction"])
        with instr.open("a", encoding="utf-8") as f:
            f.write(f"\n\n---\n\n## 人工追加要求（{ts}）\n\n{text}\n")
    elif action == "approve":
        asg["status"] = "approved"
        asg["review"] = "approved"
        asg["completed_at"] = ts
        asg["pending_ack"] = False
        append_note(task_id, assignment_id, f"## [{ts}] 人工放行\n\n{text or '通过'}\n")
        log_event(task_id, "approved", who, f"{assignment_id} 人工放行", assignment=assignment_id, text=text)
    elif action == "reject":
        asg["status"] = "rework"
        asg["review"] = "rejected"
        asg["pending_ack"] = True
        append_note(task_id, assignment_id, f"## [{ts}] 人工打回\n\n{text}\n")
        instr = pathlib.Path(asg["instruction"])
        with instr.open("a", encoding="utf-8") as f:
            f.write(f"\n\n---\n\n## 人工打回意见（{ts}）\n\n{text}\n\n> 请按此意见重做，并重新写回执。\n")
        log_event(task_id, "rejected", who, f"{assignment_id} 人工打回", assignment=assignment_id, text=text)
    elif action == "cancel":
        asg["status"] = "cancelled"
        asg["review"] = "cancelled"
        asg["completed_at"] = ts
        asg["pending_ack"] = False
        append_note(task_id, assignment_id, f"## [{ts}] 人工取消\n\n{text}\n")
        log_event(task_id, "cancelled", who, f"{assignment_id} 人工取消", assignment=assignment_id, text=text)
    elif action == "redirect":
        if asg is None:
            die("redirect 需要 --assignment")
        if to_worker not in WORKERS:
            die(f"--to 必须是 {WORKERS} 之一")
        old = asg["worker"]
        asg["worker"] = to_worker
        asg["status"] = "assigned"
        asg["review"] = None
        asg["pending_ack"] = False
        asg["dispatched_at"] = ts
        asg["completed_at"] = None
        append_note(task_id, assignment_id, f"## [{ts}] 人工改派：{old} → {to_worker}\n\n{text}\n")
        instr = pathlib.Path(asg["instruction"])
        with instr.open("a", encoding="utf-8") as f:
            f.write(f"\n\n---\n\n## 人工改派（{ts}）：{old} → {to_worker}\n\n{text}\n")
        log_event(task_id, "redirected", who, f"{assignment_id} 改派 {old} → {to_worker}", assignment=assignment_id, to=to_worker, text=text)
    elif action == "note":
        state["task_notes"] = (state.get("task_notes", "") + f"\n\n[{ts}] {text}").strip()
        append_note(task_id, "TASK", f"## [{ts}] 任务留言\n\n{text}\n")
        log_event(task_id, "note", who, "任务级留言", text=text)
    elif action == "ack":
        asg["pending_ack"] = False
        log_event(task_id, "acked", "super-agent", f"{assignment_id} 已回应人工批注", assignment=assignment_id)
    else:
        die(f"unknown action: {action}")

    state["status"] = recompute_task_status(state)
    save_state(task_id, state)
    is_human = (who == "human")
    if action == "ack":
        # 销账是闭环的最后一步：只记账本，不再唤醒任何人
        kind = None
    elif action == "note":
        kind = "task.note"
    elif is_human:
        kind = "human.intervention"
    else:
        # router / runner 的自主动作：只记录事件，**不唤醒 router**（否则自问自答）
        kind = None
    if kind:
        notify(kind, task_id,
               assignment=assignment_id, action=action, text=text, actor=who,
               to_worker=to_worker or None,
               notes=str(notes_path(task_id, assignment_id)) if assignment_id else None)
    return {"task": task_id, "assignment": assignment_id, "action": action, "task_status": state["status"]}


def cmd_comment(a: argparse.Namespace) -> None:
    print(json.dumps(intervene(a.task, a.assignment, "comment", a.text, actor=a.actor), ensure_ascii=False))


def cmd_approve(a: argparse.Namespace) -> None:
    print(json.dumps(intervene(a.task, a.assignment, "approve", a.note or "", actor=a.actor), ensure_ascii=False))


def cmd_reject(a: argparse.Namespace) -> None:
    print(json.dumps(intervene(a.task, a.assignment, "reject", a.reason, actor=a.actor), ensure_ascii=False))


def cmd_cancel(a: argparse.Namespace) -> None:
    print(json.dumps(intervene(a.task, a.assignment, "cancel", a.reason, actor=a.actor), ensure_ascii=False))


def cmd_redirect(a: argparse.Namespace) -> None:
    print(json.dumps(intervene(a.task, a.assignment, "redirect", a.text or "", a.to, actor=a.actor), ensure_ascii=False))


def cmd_ack(a: argparse.Namespace) -> None:
    state, asg = find_assignment(a.assignment, getattr(a, "task", None))
    print(json.dumps(intervene(state["task_id"], asg["id"], "ack"), ensure_ascii=False))


def cmd_note(a: argparse.Namespace) -> None:
    print(json.dumps(intervene(a.task, None, "note", a.text, actor=a.actor), ensure_ascii=False))


def cmd_grant(a: argparse.Namespace) -> None:
    """给任务临时加一个人（可在这个任务的会话里发消息）。

    典型场景：bot 问 A 的事 → A 回答 bot。
    """
    import policy
    state = load_state(a.task)
    name = (a.person or "").strip()
    oid = policy.resolve_person(name)
    if not oid:
        die(f"人员目录里找不到「{name}」（policy.json 的 people 段），"
            f"也可以直接给 open_id")
    ref = state.get("source_ref") or {}
    g = policy.grant_actor(a.task, oid, name=name, reason=a.reason or "",
                            chat_id=ref.get("chat_id") or "", question=a.question or "",
                            ttl_hours=a.ttl_hours)
    log_event(a.task, "actor_granted", a.actor,
              f"临时授权 {g.get('name')}（{oid}）在本任务会话内发言：{a.reason or ''}",
              actor_open_id=oid)
    print(json.dumps({"task": a.task, "granted": g}, ensure_ascii=False))


def cmd_revoke(a: argparse.Namespace) -> None:
    """撤销任务里的临时授权。"""
    import policy
    import policy as _p
    name = (a.person or "").strip()
    oid = _p.resolve_person(name)
    if not oid:
        die(f"人员目录里找不到「{name}」")
    ok = _p.revoke_actor(a.task, oid)
    if ok:
        log_event(a.task, "actor_revoked", a.actor, f"撤销临时授权 {name}（{oid}）",
                  actor_open_id=oid)
    print(json.dumps({"task": a.task, "revoked": ok, "open_id": oid}, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(prog="sa.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new-task")
    p.add_argument("--id")
    p.add_argument("--title", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--context", action="append")
    p.add_argument("--context-file", action="append")
    p.add_argument("--acceptance")
    p.add_argument("--source", default="manual", help="manual | feishu | cli …")
    p.add_argument("--source-ref", action="append", help="k=v，可重复；如 --source-ref message_id=om_xxx")
    p.add_argument("--by", default="human", help="创建者：human | bridge | router")
    p.add_argument("--allow-new-task", action="store_true",
                   help="仅在 SA_ACTOR=router 时豁免护栏（一般不需要）")
    p.set_defaults(func=cmd_new_task)

    p = sub.add_parser("assign")
    p.add_argument("--task", required=True)
    p.add_argument("--worker", required=True)
    p.add_argument("--instruction-file", required=True)
    p.add_argument("--brief")
    p.set_defaults(func=cmd_assign)

    p = sub.add_parser("respond")
    p.add_argument("--ask", default="", help="在等谁回答（自动临时授权其在本任务内发言）")
    p.add_argument("--assignment", required=True, help="A01 或 T001/A01（推荐后者）")
    p.add_argument("--task", help="任务 id；配合 A01 使用时消歧")
    p.add_argument(
        "--status",
        required=True,
        choices=["done", "failed", "blocked", "waiting_user_input", "need_input", "ask",
                 "completed", "complete", "success", "error"],
        help="done|failed|blocked（completed/complete/success 归一到 done，error 归一到 failed）",
    )
    p.add_argument("--summary", required=True)
    p.add_argument("--artifact", action="append")
    p.set_defaults(func=cmd_respond)

    p = sub.add_parser("submit-decision")
    p.add_argument("--task", default="",
                   help="目标任务 id；省略则用 router 本轮事件的任务"
                        "（用户消息轮为空 —— 此时 Decision 自己决定新建还是沿用）")
    p.add_argument("--file", required=True, help="Decision JSON 文件路径")
    p.add_argument("--triggers", help="逗号分隔的允许 triggerId")
    p.add_argument("--actor", default="")
    p.set_defaults(func=cmd_submit_decision)

    p = sub.add_parser("tasks-list")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_tasks_list)

    p = sub.add_parser("outbox-list")
    p.add_argument("--status", default="")
    p.set_defaults(func=cmd_outbox_list)

    p = sub.add_parser("outbox-add")
    p.add_argument("--target", default="thread")
    p.add_argument("--purpose", default="reply")
    p.add_argument("--text", required=True)
    p.add_argument("--task", default="")
    p.add_argument("--assignment", default="")
    p.add_argument("--actor", default="")
    p.set_defaults(func=cmd_outbox_add)

    p = sub.add_parser("outbox-retry", help="把 dead_letter/failed 的外发记录放回待发队列")
    p.add_argument("--id", required=True)
    p.set_defaults(func=cmd_outbox_retry)

    p = sub.add_parser("outbox-verify",
                       help="对账：把 succeeded 但无凭据/无投递记录的标为 unverified（不改历史记录）")
    p.add_argument("--log", default="", help="桥接日志路径（默认 logs/lark-bridge.log）")
    p.add_argument("--out", default="", help="把对账清单写到这个文件")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_outbox_verify)

    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show")
    p.add_argument("--task", required=True)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("events")
    p.add_argument("--task", required=True)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("find")
    p.add_argument("--match", default="", help="内容片段（大小写不敏感）")
    p.add_argument("--source", help="manual | feishu")
    p.add_argument("--status", help="open | dispatched | running | needs_human | completed")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("comment")
    p.add_argument("--task", required=True)
    p.add_argument("--assignment", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--actor", default="", help="human | router | worker-runner；缺省按进程身份（SA_ACTOR）推断")
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser("approve")
    p.add_argument("--task", required=True)
    p.add_argument("--assignment", required=True)
    p.add_argument("--note")
    p.add_argument("--actor", default="", help="human | router | worker-runner；缺省按进程身份（SA_ACTOR）推断")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject")
    p.add_argument("--task", required=True)
    p.add_argument("--assignment", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--actor", default="", help="human | router | worker-runner；缺省按进程身份（SA_ACTOR）推断")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("cancel")
    p.add_argument("--task", required=True)
    p.add_argument("--assignment", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--actor", default="", help="human | router | worker-runner；缺省按进程身份（SA_ACTOR）推断")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("redirect")
    p.add_argument("--task", required=True)
    p.add_argument("--assignment", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--text")
    p.add_argument("--actor", default="", help="human | router | worker-runner；缺省按进程身份（SA_ACTOR）推断")
    p.set_defaults(func=cmd_redirect)

    p = sub.add_parser("ack")
    p.add_argument("--assignment", required=True, help="A01 或 T001/A01（推荐后者）")
    p.add_argument("--task", help="任务 id；配合 A01 使用时消歧")
    p.set_defaults(func=cmd_ack)

    p = sub.add_parser("note")
    p.add_argument("--task", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--actor", default="", help="human | router | worker-runner；缺省按进程身份（SA_ACTOR）推断")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("grant", help="给任务临时加一个人（可在本任务会话里发消息）")
    p.add_argument("--task", required=True)
    p.add_argument("--person", required=True, help="名字（policy.json 人员目录）或 open_id")
    p.add_argument("--reason", default="")
    p.add_argument("--question", default="", help="在等这个人回答什么")
    p.add_argument("--ttl-hours", type=int, default=None)
    p.add_argument("--actor", default="human")
    p.set_defaults(func=cmd_grant)

    p = sub.add_parser("revoke", help="撤销任务里的临时授权")
    p.add_argument("--task", required=True)
    p.add_argument("--person", required=True)
    p.add_argument("--actor", default="human")
    p.set_defaults(func=cmd_revoke)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
