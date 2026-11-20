#!/usr/bin/env python3
"""Router —— 常驻的主 agent session。

一个 pi RPC 进程（固定 session id），所有飞书指令都进这一个 session，
所以它对上下文是有记忆的。每当协议层产生一个事件（收到指令 / worker 回执 /
人工干预），router 就在这个 session 里被唤醒一次，由它判断下一步：

    · 要不要回飞书给用户（lark_bridge.py reply）
    · 要不要再派一个 worker（sa.py assign）
    · 还是先跟人确认

    python3 super-agent/router.py start|stop|status|run|tail
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import datetime as _dt

ROOT = pathlib.Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))
import sa  # noqa: E402

# router 的目录来源只有一处：sa（它自己又按 SA_ROOT / SA_ROUTER_DIR 派生）。
# 两边各自写 ROOT / "router" 时，沙箱演练会把事件送进生产 router，或者反过来
# 读不到自己沙箱里的事件。
ROUTER_DIR = sa.ROUTER_DIR
INBOX = sa.ROUTER_INBOX
OUTBOX = ROUTER_DIR / "outbox.jsonl"
SESSION_FILE = ROUTER_DIR / "session.json"
# 桥接的永久账本：正常路径下账本一定先于 router 事件写入（反查可信来源的依据）
LEDGER_FILE = sa.LEDGER_FILE
# 被拦下的不可核验事件（只在 router/ 下 append，不建任务、不唤醒模型）
UNVERIFIED_FILE = ROUTER_DIR / "unverified.jsonl"

# 模型回退链：主渠道额度耗尽时依次尝试
FALLBACK_MODELS = (
    "bella/Deepseek-V4.1-Flash",
    "minimax-cn/MiniMax-M3.1-Flash",
    "bella/MiniMax-M3.1-Flash",
)
# 运行时模型状态（文件驱动 —— 改它，运行中的 router 下一轮就会跟着切）
MODEL_FILE = ROUTER_DIR / "model.json"
DEFAULT_MODEL = "minimax-cn/MiniMax-M3.1-Flash"
# router 的思考等级（thinking level）——
# router 只做「语义判断 + 写 Decision」，不是长链推理任务；
# 高思考档实测会在第一次工具调用前空转 23 秒（用户全程看不到东西）。
# low 是平衡点：明显提速，判断质量基本不掉。
DEFAULT_THINKING = "low"
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
# 降级后多久尝试自动切回主模型（秒）
RESTORE_AFTER = int(os.environ.get("SA_ROUTER_RESTORE_AFTER", "300"))


def thinking_level() -> str:
    """当前生效的思考等级（model.json 的 thinking 字段）。"""
    lv = str(load_model_state().get("thinking") or DEFAULT_THINKING).strip().lower()
    return lv if lv in THINKING_LEVELS else DEFAULT_THINKING


def load_model_state() -> dict:
    """读运行时模型状态。"""
    st = {"primary": DEFAULT_MODEL, "boot_primary": DEFAULT_MODEL, "active": "",
          "degraded": False, "degraded_to": "", "degraded_at": "",
          "thinking": DEFAULT_THINKING,
          "restore_after_seconds": RESTORE_AFTER}
    if MODEL_FILE.exists():
        try:
            st.update(json.loads(MODEL_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    env = os.environ.get("SA_ROUTER_MODEL")
    if env and not MODEL_FILE.exists():
        st["primary"] = st["boot_primary"] = env
    return st


def save_model_state(st: dict) -> None:
    MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODEL_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# session id = 前缀 + 创建时间（如 super-agent-router-20261120-165446）。
#
# 为什么带时间戳：pi 复现会话靠 id 反查（SessionManager.findById），
# 顶层一旦有**两份同 id** 的文件，pi 会挑其中任意一份 resume ——
# 轮转后如果还沿用固定 id，老会话被捞回来，router 就又带着上一代的上下文跑。
# 所以「开一个新会话」必须换一个**唯一** id；
# 「重启续接同一会话」则靠持久化的 router/session_id.json 记着当前 id。
SESSION_ID_PREFIX = "super-agent-router"
# 当前 session 的持久化状态（id / 创建时间 / 显示名）—— 跨进程的唯一权威
SESSION_ID_FILE = ROUTER_DIR / "session_id.json"
# 操作契约放在 system prompt 里 —— 事件消息只留「哪个任务、哪个 trigger」，不重复携带规格
SYSTEM_PROMPT_FILE = ROOT / "skill" / "router-system.md"
# worker 名册：每个新 session 启动时从 registry.json 重新生成并注入 system prompt
WORKER_BRIEF_FILE = ROUTER_DIR / "worker_brief.md"
REGISTRY_FILE = ROOT / "registry.json"
# 工具护栏：让 router 在机制上无法自己调查/执行
GATE_FILE = ROOT / "router_gate.ts"
PID_FILE = pathlib.Path("/tmp/super-agent-router.pid")
LOG_FILE = pathlib.Path("/tmp/super-agent-router.out")


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now()}] {msg}\n"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line)
    print(msg, flush=True)


# ────────────────────── session 身份（id / 创建时间 / 归档） ──────────────────────


def session_display_name(created_at: str) -> str:
    """会话显示名：Super Agent Router <创建时间>。

    `pi --resume` 的选择器渲染的是 name，所以历次会话在列表里一眼能分清是哪一次开的。
    """
    try:
        ts = _dt.datetime.fromisoformat(created_at).astimezone()
        return f"Super Agent Router {ts.strftime('%Y-%m-%d %H:%M:%S')}"
    except (ValueError, TypeError):
        return f"Super Agent Router {created_at}"


def router_session_dir() -> pathlib.Path:
    """pi 为本仓库 cwd 建的会话目录（pi 用 cwd 反写目录名，不能写死本机路径）。"""
    return pathlib.Path(os.path.expanduser(
        "~/.pi/agent/sessions")) / ("--" + str(REPO).strip("/").replace("/", "-") + "--")


def router_session_files() -> list[pathlib.Path]:
    """本仓库 cwd 下**顶层**的 router session 文件（archive/ 下的不算，按新到旧排）。

    只认顶层：归档是历史，不是「当前会话」—— 看板和轮转都不该把它们当现行。
    """
    d = router_session_dir()
    if not d.exists():
        return []
    out = [p for p in d.iterdir()
           if p.is_file() and p.suffix == ".jsonl" and SESSION_ID_PREFIX in p.name]
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def session_header(path: pathlib.Path) -> dict:
    """读 session 文件头（id / 创建时间都在头一行）。"""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            d = json.loads(f.readline())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def session_header_id(path: pathlib.Path) -> str:
    """读 session 文件头里的 id。"""
    return str(session_header(path).get("id") or "")


def session_header_time(path: pathlib.Path) -> str:
    """session 的创建时间（文件头里的 UTC 时间戳 → 本地时间）。"""
    ts = str(session_header(path).get("timestamp") or "")
    if not ts:
        return ""
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return ""


def session_file_for(sid: str) -> pathlib.Path | None:
    """某个 id 对应的**顶层** session 文件（没有 = 还没开过）。"""
    for p in router_session_files():
        if p.name.endswith(f"_{sid}.jsonl"):
            return p
    return None


def make_session_id(created_at: str | None = None) -> str:
    """生成一个新的、唯一的 session id：super-agent-router-<创建时间>。

    时间戳是本地时间（和显示名里的时间一致），秒级精度。
    同一秒内连续创建两次会撞 id（pi 按 id 反查，撞了会复现错的那个）→ 顺延。
    """
    ts = created_at or now()
    try:
        stamp = _dt.datetime.fromisoformat(ts).astimezone().strftime("%Y%m%d-%H%M%S")
    except (ValueError, TypeError):
        stamp = _dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    used = set()
    for p in router_session_files():
        used.add(session_header_id(p))
        used.add(p.stem.split("_", 1)[-1])
    arch = router_session_dir() / "archive"
    if arch.exists():
        for p in arch.glob("*.jsonl"):
            used.add(session_header_id(p))
    # 当前状态里的 id 也算「已用」—— 同一秒内连着轮转两次不会撞回同一个 id
    used.add(str(load_session_state().get("session_id") or ""))
    sid = f"{SESSION_ID_PREFIX}-{stamp}"
    n = 0
    while sid in used:
        n += 1
        if n > 120:
            sid = f"{SESSION_ID_PREFIX}-{stamp}-{int(time.time())}"
            break
        sid = f"{SESSION_ID_PREFIX}-{stamp}-{n:02d}"
    return sid


def load_session_state() -> dict:
    st = {}
    if SESSION_ID_FILE.exists():
        try:
            st = json.loads(SESSION_ID_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            st = {}
    return st if isinstance(st, dict) else {}


def save_session_state(st: dict) -> None:
    """落盘 session 状态（原子替换：崩在半路不会读到半个文件）。"""
    SESSION_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_ID_FILE.with_name(SESSION_ID_FILE.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(SESSION_ID_FILE)


def current_session_state() -> dict:
    """当前 router session 的状态（id / 创建时间 / 显示名）。

    状态文件是**唯一权威**：
      · router 重启 → 读同一个 id → pi 复现同一会话（上下文不丢）
      · 轮转 → 换一个全新 id → pi 开新会话（上下文清零），老会话留在 archive/

    状态文件不存在时（首次上线 / 被误删）：认领顶层已有的最新 router 会话，
    别把正在跑的上下文弄丢。
    """
    st = load_session_state()
    sid = str(st.get("session_id") or "").strip()
    if sid:
        st.setdefault("created_at", now())
        st.setdefault("name", session_display_name(st["created_at"]))
        return st
    cands = router_session_files()
    if cands:
        p = cands[0]
        created = (session_header_time(p)
                   or _dt.datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds"))
        sid = session_header_id(p) or make_session_id(created)
        st = {"session_id": sid, "created_at": created,
              "name": session_display_name(created), "adopted_from": p.name}
        save_session_state(st)
        return st
    created = now()
    st = {"session_id": make_session_id(created), "created_at": created,
          "name": session_display_name(created)}
    save_session_state(st)
    return st


def wait_pid_gone(pid: int, timeout: float = 15.0) -> bool:
    """等进程真的退出（stop 只是发信号）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def render_worker_brief() -> str:
    """从 registry.json 生成 worker 名册。

    名册只讲**谁是谁、能干什么、有什么闸门** ——
    具体事项怎么做是 worker 自己的角色规格（role_prompt / ROLE.md）的事，
    router 不写、不猜、不复述。
    """
    try:
        data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    workers = data.get("workers") or []
    lines = [
        "# Worker 名册（本 session 启动时从 registry.json 生成）",
        "",
        "`agentId` 只能填下表里的 agent_id。名册与 registry 不一致时以 registry 为准。",
        "",
        "**你只带目标，不带做法。** 怎么做、拆成几步、交付成什么样、怎么验收 —— "
        "全是 worker 自己的判断；worker 有自己的角色规格，你不用替它设计步骤。",
        "",
    ]
    if not workers:
        lines += ["（registry.json 读不到，先别派工。）", ""]
        return "\n".join(lines)
    lines += [
        "| agent_id | 角色 | 只读 | 职责范围 | 触发闸门 |",
        "| --- | --- | --- | --- | --- |",
    ]
    disabled: list[str] = []
    for w in workers:
        aid = str(w.get("agent_id") or "")
        if not aid:
            continue
        if not w.get("enabled"):
            disabled.append(f"- `{aid}`（{w.get('label') or ''}）已停用："
                            f"{w.get('disabled_reason') or '未说明'} —— **不要派它**")
            continue
        gate = w.get("trigger_gate") or {}
        if gate:
            gate_s = "有（Task Center 强制校验，闸门不符直接拒这条 item）"
            if gate.get("require_p2p"):
                gate_s += "；只对指定说话人的私聊开放"
        else:
            gate_s = "无"
        scope = "、".join(str(a) for a in (w.get("accepts") or [])) or "（未声明）"
        lines.append(f"| `{aid}` | {w.get('label') or ''} | "
                     f"{'是' if w.get('readonly') else '否'} | {scope} | {gate_s} |")
    lines += [
        "",
        "## 通用约定",
        "",
        "- 只读 worker（调查 / 方案类）由框架强制：不能改、删、建任何业务文件。",
        "- 有触发闸门的 worker：闸门不符时 Task Center 直接拒这条 item；"
        "你不用自己拦，但**不要**把这类请求硬派给别的 worker 做 —— "
        "闸门不符时用 outbox 回一句说明就行。",
        "- 名册里没有的 agent_id 一律不能填（registry 校验会拒）。",
    ]
    if disabled:
        lines += ["", "## 已停用", ""] + disabled
    return "\n".join(lines) + "\n"


def write_worker_brief() -> pathlib.Path:
    """生成 worker 名册文件（每个新 session 启动时重生成）。"""
    try:
        WORKER_BRIEF_FILE.parent.mkdir(parents=True, exist_ok=True)
        WORKER_BRIEF_FILE.write_text(render_worker_brief(), encoding="utf-8")
    except OSError as e:
        log(f"worker 名册生成失败：{e}")
    return WORKER_BRIEF_FILE


# ------------------------------------------------------------------ prompt


def build_prompt(ev: dict) -> str:
    """事件消息 —— 只给「哪件事」，不给上下文。

    router 是**单一长驻 session**：规格要求在 system prompt 里，历史在 session 里，
    用户自己主动 @ 机器人的那些消息也在 session 里。所以这里不重复传上下文 ——
    只给定位信息：事件类型 + triggerId + 任务/路径 + 用户这句话本身。
    """
    kind = ev.get("kind") or ""
    task = ev.get("task") or ""
    asg = ev.get("assignment") or ""
    lines = [
        f"[事件] {kind}",
        f"triggerId: {ev.get('id', '')}",
    ]
    if kind == "user.message":
        # 谁说的、在哪说的、是不是话题里 —— 这三项决定派给谁、怎么回。
        # 不是上下文，是这条消息自己的属性；缺了 router 只能瞎猜。
        ref = ev.get("source_ref") or {}
        who = (ref.get("sender_name") or "").strip()
        sid = (ref.get("sender") or "").strip()
        if who and sid:
            lines.append(f"说话人: {who}（{sid}）")
        elif who:
            lines.append(f"说话人: {who}")
        elif sid:
            lines.append(f"说话人: {sid}")
        chat = display_chat_name(ref)
        cid = (ref.get("chat_id") or "").strip()
        platform = str(ref.get("platform") or "").strip()
        if platform == "wecom":
            # 企微没有群名（回调里只给 chatid），但渠道本身要说清楚：
            # 单聊还是群聊，模型才知道该怎么回话。
            chat = f"企业微信（{ref.get('chat_type') or '未知形态'}）"
        lines.append(f"渠道: {chat}" + (f"（{cid}）" if cid and cid not in chat else ""))
        tid = (ref.get("thread_id") or "").strip()
        rid = (ref.get("root_id") or "").strip()
        if tid or rid:
            parts = []
            if tid:
                parts.append(f"话题 {tid}")
            if rid and rid != tid:
                parts.append(f"根消息 {rid}")
            lines.append("话题串: " + " · ".join(parts))
        else:
            lines.append("话题串: 无（主频道）")
        # 临时授权命中：这个人是被某个任务拉进来回答问题的
        # → 明确告诉 router 该延续哪个任务，否则它会当成新目标
        g = ev.get("grant") or {}
        if g.get("grant_task"):
            who = g.get("granted_to") or "被授权人"
            q = g.get("question") or ""
            lines.append(f"临时授权: 任务 {g['grant_task']} 正在等「{who}」回答")
            if q:
                lines.append(f"待回答的问题: {q[:200]}")
            lines.append("→ 这条消息应当沿用该任务（taskAction=continue），不是新任务")
        # 用户原话是 payload（不是上下文）—— 没有它 router 就不知道要做什么
        lines.append("")
        lines.append("用户说：")
        lines.append((ev.get("text") or "").strip())
        lines.append("")
        lines.append("→ 派工时把这段**原话**原样带给 worker（框架会自动放进指派单的「## 用户原话」）；"
                     "需求分析、怎么做、怎么验收由 worker 自己判断，**不要替用户拆解**。")
        return "\n".join(lines)
    if task:
        lines.append(f"任务: {task}" + (f" / {asg}" if asg else ""))
        lines.append(f"目录: {sa.task_dir(task)}")
    if kind in ("assignment.completed", "assignment.failed") and task and asg:
        # ── 分层产出：先给摘要，router 默认**不用**读产出全文 ──
        status = ev.get("status") or ""
        summ = (ev.get("summary") or "").strip()
        fields = ev.get("summary_fields") or {}
        arts = ev.get("artifacts") or []
        if ev.get("auto_approved"):
            lines.append(f"状态: {status}（**框架已自动放行**：产出 {len(arts)} 个文件均存在、回执结构完整）")
            lines.append("→ 不用为此去读 response.md / task.md / 产出物；"
                         "要知道细节看下面的摘要即可。")
        else:
            lines.append(f"状态: {status}")
            if ev.get("review_note"):
                lines.append(f"⚠ 框架复核未通过：{ev.get('review_note')} → 需要你决定（补证据 / 打回 / 问人）")
        if summ:
            lines.append(f"摘要: {summ}")
            if fields.get("问题") and fields.get("问题") not in ("无", "-", "none"):
                lines.append(f"⚠ 问题: {fields['问题']}")
            if arts:
                lines.append("产出: " + "、".join(str(a) for a in arts))
            if ev.get("auto_replied"):
                lines.append("worker 已自己写好回话，框架**已直接投给用户** —— "
                             "不必再重复回话，除非要补充说明。")
            lines.append("（默认只看摘要；除非要核对数字，否则不要去读产出文件。）")
        # 本批兄弟指派：同一句用户消息派出去的其它活。
        # router 用它判断「还有几条在跑」——
        #   还有在跑的 → 这次只复核放行，回报等齐了统一发（系统会合并成一条）；
        #   全部结束 → 一次性复核并回报。
        try:
            st = sa.load_state(task)
            me = next((x for x in st.get("assignments") or [] if x["id"] == asg), None)
            bid = (me or {}).get("batch_id") or ""
            if bid:
                b = sa.batch_status(bid)
                if b.get("total", 0) > 1:
                    lines.append(f"本批（{bid}）共 {b['total']} 条指派，还在飞 {b['in_flight']} 条")
                    for r in b.get("assignments") or []:
                        extra = ""
                        if r.get("artifacts"):
                            extra = " · 产出 " + ", ".join(str(x) for x in r["artifacts"][:2])
                        lines.append(f"  · {r['task']}/{r['assignment']} · {r['status']}{extra}")
                    lines.append("→ 还有在跑的：本轮只放行不回话，等本批全部结束再统一回报")
        except (Exception, SystemExit) as e:
            log(f"查本批兄弟指派失败：{str(e)[:120]}")
    return "\n".join(lines)


# ------------------------------------------------------------------ pi RPC


class PiSession:
    """一个常驻的 pi RPC 进程 = 一个常驻 session。"""

    def __init__(self, cwd: pathlib.Path, session_id: str = ""):
        self.cwd = cwd
        self.session_id = session_id or current_session_state().get("session_id") or SESSION_ID_PREFIX
        self.session_created_at = ""
        self.proc: subprocess.Popen | None = None
        self.pending: dict[str, dict] = {}
        self.events: list[dict] = []
        self.last_assistant = ""
        self.last_error = ""
        self.model = load_model_state().get("primary") or os.environ.get("SA_ROUTER_MODEL", DEFAULT_MODEL)
        self.thinking = thinking_level()
        self.started_at = ""
        self.session_name = "Super Agent Router"
        self._id = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        mst = load_model_state()
        self.model = mst.get("primary") or os.environ.get("SA_ROUTER_MODEL", DEFAULT_MODEL)
        self.thinking = str(mst.get("thinking") or DEFAULT_THINKING).strip().lower()
        if self.thinking not in THINKING_LEVELS:
            self.thinking = DEFAULT_THINKING
        # 会话身份来自**持久化状态**：id 带创建时间（super-agent-router-<时间戳>），
        # 重启后读同一个 id → pi 复现同一会话 → 上下文不丢；
        # 轮转时换新 id → pi 开新会话。
        st = current_session_state()
        self.session_id = st.get("session_id") or self.session_id
        self.started_at = now()
        self.session_created_at = st.get("created_at") or self.started_at
        self.session_name = st.get("name") or session_display_name(self.session_created_at)
        # worker 名册：每个新 session 启动时从 registry.json 重新生成
        brief = write_worker_brief()
        cmd = [
            "pi", "--mode", "rpc",
            "--session-id", self.session_id,
            "--model", self.model,
            # 思考等级：router 只做语义判断，不需要长链推理；
            # 高档位实测会在第一次工具调用前空转 23 秒。
            "--thinking", self.thinking,
            "--append-system-prompt", str(SYSTEM_PROMPT_FILE),
            # worker 名册：每个新 session 启动时从 registry.json 重新生成
            "--append-system-prompt", str(brief),
            # 工具最小集：只读任务上下文 + 写 Decision 草稿 + 调 Task Center。
            # grep/find/ls 等探索工具一律不给 —— router 不做调查。
            "--tools", "read,write,bash",
            # 护栏：bash 只许 sa.py，read 只许任务目录，write 只许 /tmp
            "--extension", str(GATE_FILE),
        ]
        # display name 只在**新建**会话时写一次；复现旧会话时别再 append 一条同名 session_info
        if not session_file_for(self.session_id):
            cmd += ["--name", self.session_name]
        log(f"system prompt 注入：契约 {SYSTEM_PROMPT_FILE.name} + worker 名册 {brief.name}")
        env = dict(os.environ)
        env.setdefault("PI_NO_UPDATE_CHECK", "1")
        env["SA_ACTOR"] = "router"   # 让 sa.py 知道这是常驻 router，不许建任务
        self.proc = subprocess.Popen(
            cmd, cwd=str(self.cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
        )
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        # 等 session 就绪
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                st = self.request("get_state", timeout=30)
                log(f"pi session 就绪：{(st or {}).get('sessionId')} model={(st or {}).get('model',{}).get('id')}"
                    f" thinking={self.thinking}")
                break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("pi session 启动超时")
        # 立刻刷一次快照 —— 否则刚启动/刚轮转时，看板显示的还是上一份旧状态
        try:
            self.snapshot()
        except Exception:
            pass
        # 启动后**再打一次**思考等级：pi 恢复 session 时会把 session 里记录的老等级
        # 带回来（“Resuming the session restores them”），只靠启动参数不保险。
        self.apply_thinking()

    def rotate(self) -> bool:
        """轮转 session —— 清空上下文，重新开始。

        常驻 session 会越滚越大，模型被历史（旧任务、旧决策、旧回执）带偏：
        表现为「这轮的用户指令被忽略，router 反而去处理历史遗留」。
        所以每轮处理完就换一个干净 session，跨轮次的连续性靠 router/digest.md 承接。
        """
        try:
            self.request("new_session", timeout=60)
            self.request("set_model", timeout=60,
                         provider=self.model.split("/")[0], modelId=self.model.split("/")[-1])
            # 轮转会沿用 session 里记录的思考等级 → 重新按配置打一遍
            self.apply_thinking()
            log(f"session 已轮转（上下文清零，thinking={self.thinking}）")
            return True
        except Exception as e:
            log(f"session 轮转失败：{e}")
            return False

    def apply_thinking(self) -> None:
        """把思考等级打到当前 session 上（运行中生效）。

        为什么要单独打一遍：pi 的 session 会**记住**上次的思考等级，
        恢复 session 时会把老等级带回来 —— 不重打就等于改了配置也没用。
        """
        try:
            self.request("set_thinking_level", timeout=30, level=self.thinking)
            log(f"router 思考等级 → {self.thinking}")
        except Exception as e:
            log(f"设置思考等级失败（{self.thinking}）：{str(e)[:120]}")

    def _drain_stderr(self) -> None:
        try:
            for line in self.proc.stderr:
                if line.strip():
                    log("pi stderr: " + line.strip()[:300])
        except Exception:
            pass

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = rec.get("type")
            if t == "response":
                rid = rec.get("id")
                with self._lock:
                    self.pending[rid] = rec
            elif t == "message_end":
                msg = rec.get("message") or {}
                if msg.get("role") == "assistant":
                    if msg.get("errorMessage"):
                        self.last_error = msg["errorMessage"]
                    c = msg.get("content")
                    text = c if isinstance(c, str) else "".join(
                        b.get("text", "") for b in (c or []) if isinstance(b, dict))
                    if text.strip():
                        self.last_assistant = text
            elif t in ("agent_end", "agent_settled"):
                self.events.append(rec)
            elif t == "error":
                self.last_error = str(rec.get("message") or rec.get("error") or rec)

    def _send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(self, cmd: str, timeout: int = 120, **extra) -> dict:
        with self._lock:
            self._id += 1
            rid = f"r{self._id}"
            self.pending[rid] = {}
        payload = {"id": rid, "type": cmd, **extra}
        self._send(payload)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                rec = self.pending.get(rid)
            if rec and rec.get("type") == "response":
                with self._lock:
                    self.pending.pop(rid, None)
                return rec.get("data") if rec.get("success") else {"error": rec.get("error")}
            time.sleep(0.1)
        with self._lock:
            self.pending.pop(rid, None)
        raise TimeoutError(f"pi RPC {cmd} 超时")

    def snapshot(self) -> dict:
        """抓一份 session 状态，写到 router/session.json 供看板展示。"""
        info = {"session_id": self.session_id, "cwd": str(self.cwd), "ts": now(),
                "session_name": self.session_name, "started_at": self.started_at,
                "session_created_at": self.session_created_at}
        try:
            st = self.request("get_state", timeout=20) or {}
            info["model"] = (st.get("model") or {}).get("id")
            info["provider"] = (st.get("model") or {}).get("provider")
            info["session_file"] = st.get("sessionFile")
            info["session_id_actual"] = st.get("sessionId")
            info["message_count"] = st.get("messageCount")
            info["context"] = st.get("contextUsage")
            info["running"] = st.get("run", {}).get("running")
            info["awaiting_input"] = st.get("awaiting_input")
        except Exception as e:
            info["error"] = str(e)
        try:
            stats = self.request("get_session_stats", timeout=20) or {}
            info["stats"] = stats
        except Exception:
            pass
        info["pid"] = self.proc.pid if self.proc else None
        info["last_error"] = self.last_error or None
        mst = load_model_state()
        info["model_configured"] = self.model
        info["model_primary"] = mst.get("primary")
        info["thinking_level"] = self.thinking
        info["model_degraded"] = bool(mst.get("degraded"))
        info["model_degraded_to"] = mst.get("degraded_to") or ""
        info["fallback_models"] = list(FALLBACK_MODELS)
        try:
            SESSION_FILE.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
        return info

    def prompt(self, message: str, timeout: int = 900) -> str:
        """发一条 prompt，等 agent_settled，返回最后的 assistant 文本。

        单渠道额度耗尽（429）时自动换下一个模型重试，避免整个调度瘫痪。

        模型切换的三个来源：
          1. router/model.json 的 primary（人工指定，运行中即可生效）
          2. FALLBACK_MODELS（429 自动降级）
          3. 降级后过一段时间自动切回 primary（restore_after_seconds）
        """
        self.sync_model()
        primary = self.model
        models = [primary] + [m for m in FALLBACK_MODELS if m != primary]
        last_err = ""
        for i, model in enumerate(models):
            try:
                if i > 0:
                    log(f"模型 {primary} 失败，换 {model} 重试：{last_err[:120]}")
                    self.set_model(model)
                    self.mark_degraded(primary, model, last_err)
                out = self._prompt_once(message, timeout)
                if i > 0:
                    # 降级模型成功跑完 —— 标记为「降级中」，等 cooldown 后自动切回
                    self.mark_degraded(primary, model, last_err)
                else:
                    self.clear_degraded()
                return out
            except (RuntimeError, TimeoutError, OSError) as e:
                last_err = str(e)
                if "429" not in last_err and "rate_limit" not in last_err.lower():
                    # 只有额度类错误才静默换模型；其它错误直接抛给调用方
                    raise
        raise RuntimeError(f"所有模型都失败，最后一个错误：{last_err[:300]}")

    # ── 模型状态维护 ──

    def sync_model(self) -> None:
        """按 model.json 调整当前模型（运行中生效，无需重启）。

        - primary 与当前模型不同 → 立即切过去
        - 处于降级状态且已过 cooldown → 尝试切回 primary
        """
        st = load_model_state()
        primary = st.get("primary") or DEFAULT_MODEL
        degraded = bool(st.get("degraded"))
        # 思考等级改了也要跟着生效（不必重启）
        want_thinking = str(st.get("thinking") or DEFAULT_THINKING).strip().lower()
        if want_thinking not in THINKING_LEVELS:
            want_thinking = DEFAULT_THINKING
        if want_thinking != self.thinking:
            self.thinking = want_thinking
            self.apply_thinking()
        cooled = True
        if degraded:
            try:
                at = _dt.datetime.fromisoformat(st.get("degraded_at") or "")
                wait = int(st.get("restore_after_seconds") or RESTORE_AFTER)
                cooled = (_dt.datetime.now(at.tzinfo) - at).total_seconds() >= wait
            except (ValueError, TypeError):
                cooled = True
        if primary == self.model:
            return
        if degraded and not cooled:
            return      # 还在 cooldown 内，先用降级模型
        if degraded and cooled:
            log(f"降级 cooldown 已过（{st.get('degraded_to')} → {primary}），尝试切回主模型")
        log(f"切换模型 → {primary}")
        self.set_model(primary)
        st = load_model_state()
        st["active"] = primary
        st["degraded"] = False
        st["degraded_to"] = ""
        st["degraded_at"] = ""
        save_model_state(st)

    def mark_degraded(self, primary: str, fallback: str, err: str = "") -> None:
        """记录降级状态。

        注意：primary 只由人工 / model 子命令决定，这里**不改写**它 ——
        之前把降级模型写成 primary，原主模型就再也回不去了。
        """
        st = load_model_state()
        st["active"] = fallback
        st["degraded"] = True
        st["degraded_to"] = fallback
        st["degraded_at"] = now()
        st["degraded_reason"] = (err or "")[:200]
        save_model_state(st)
        log(f"已降级到 {fallback}（主模型 {primary}）：{err[:120]}")

    def clear_degraded(self) -> None:
        st = load_model_state()
        if st.get("degraded"):
            st["degraded"] = False
            st["degraded_to"] = ""
            st["degraded_at"] = ""
            st["active"] = self.model
            save_model_state(st)
            log("主模型恢复正常")

    def set_model(self, model: str) -> None:
        try:
            provider, _, mid = model.partition("/")
            self.request("set_model", timeout=30, provider=provider, modelId=mid)
            self.model = model
            # 换模型后思考等级可能被重置 → 重打一遍
            self.apply_thinking()
            log(f"router 已切换到模型 {model}")
        except Exception as e:
            log(f"切换模型失败 {model}: {e}")

    def _prompt_once(self, message: str, timeout: int = 900) -> str:
        self.last_assistant = ""
        self.last_error = ""
        self.events.clear()
        extra = {"message": message}
        if self.events:  # 忙 → 排队
            extra["streamingBehavior"] = "followUp"
        resp = self.request("prompt", timeout=60, **extra)
        if isinstance(resp, dict) and resp.get("error"):
            raise RuntimeError(f"prompt 被拒: {resp['error']}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(e.get("type") == "agent_settled" for e in self.events):
                time.sleep(0.3)
                if not self.last_assistant.strip() and self.last_error:
                    raise RuntimeError(f"模型调用失败: {self.last_error[:300]}")
                return self.last_assistant
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError("pi 进程退出")
            time.sleep(0.2)
        log(f"prompt 超时 {timeout}s，主动 abort 本轮")
        try:
            self.request("abort", timeout=15)
        except Exception:
            pass
        return self.last_assistant

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.terminate()


# ------------------------------------------------------------------ 事件循环


ROUND_FILE = ROUTER_DIR / "round.json"
# 跨轮次摘要：session 每轮轮转后，过去的上下文就靠这个文件承接。
# router 每轮会读它（它在 router/ 目录下，读权限放行）。
DIGEST_FILE = ROUTER_DIR / "digest.md"
DIGEST_MAX_LINES = 40


def read_digest(max_lines: int = DIGEST_MAX_LINES) -> str:
    """读跨轮次摘要（最近若干条）。"""
    if not DIGEST_FILE.exists():
        return ""
    lines = [l for l in DIGEST_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    return "\n".join(lines[-max_lines:])


def append_digest(line: str) -> None:
    """追加一条摘要。"""
    DIGEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DIGEST_FILE.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")
    # 只保留最近若干条，避免无限增长
    lines = DIGEST_FILE.read_text(encoding="utf-8").splitlines()
    if len(lines) > DIGEST_MAX_LINES * 2:
        DIGEST_FILE.write_text("\n".join(lines[-DIGEST_MAX_LINES:]) + "\n", encoding="utf-8")


# ------------------------------------------------------- 来源闸门（user.message）
#
# 背景：router 以前对 source_ref **零校验** —— 唯一受理条件就是「这行 JSON 出现
# 在 router/inbox.jsonl 里」。于是一个沙箱验证脚本造出来的假事件（会话 id 只是
# 占位串、永久账本里根本没有它）被当成用户指令，直接建了任务、回了 ack。
#
# 现在两道闸：
#   ① 形态校验：chat_id 必须 oc_+32 位十六进制，message_id 必须以 om_ 开头；
#   ② 账本反查：message_id 必须能在桥接的永久账本（inbox/handled.jsonl）里查到
#      —— 正常路径下账本先写、事件后写；查不到就是「非桥接产出」。
# 两项任一不通过 → 不建任务、不唤醒模型，记 unverified + 摘要留一行给人工复核。

CHAT_ID_RE = re.compile(r"^oc_[0-9a-f]{32}$")
MESSAGE_ID_PREFIX = "om_"

_ledger_cache: dict = {"mtime": None, "ids": set()}


def ledger_ids() -> set[str]:
    """永久账本里的 message_id 集合（按 mtime 缓存，账本是 append-only 的）。"""
    if not LEDGER_FILE.exists():
        return set()
    try:
        mtime = LEDGER_FILE.stat().st_mtime_ns
    except OSError:
        return set()
    if _ledger_cache["mtime"] == mtime:
        return _ledger_cache["ids"]
    ids: set[str] = set()
    try:
        with LEDGER_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    mid = json.loads(line).get("message_id")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if mid:
                    ids.add(mid)
    except OSError:
        return set()
    _ledger_cache["mtime"] = mtime
    _ledger_cache["ids"] = ids
    return ids


def verify_user_event(ev: dict, ledger_wait_seconds: float = 0.0) -> tuple[bool, str]:
    """判断一条事件是不是「可核验的飞书用户消息」。返回 (通过?, 失败原因)。

    只管 kind=user.message 且 source=feishu 的事件；其余事件（task.created /
    assignment.completed / 人工注入的非飞书事件）不受影响 —— 它们由协议层产生，
    本来就不带飞书会话标识。

    ledger_wait_seconds > 0 时允许短暂重试：桥接现在是**先投递事件、后补账本**
    （两行之间是毫秒级窗口），router 轮询恰好落在窗口里就会看到「账本里还没有」。
    默认 0（纯粹判定）；事件循环里给 1.2 秒容忍。
    """
    if (ev.get("kind") or "") != "user.message":
        return True, ""
    source = (ev.get("source") or "")
    if source not in ("feishu", "wecom"):
        return True, ""
    ref = ev.get("source_ref") or {}
    if not isinstance(ref, dict):
        return False, "source_ref 不是对象"
    chat_id = str(ref.get("chat_id") or "")
    msg_id = str(ref.get("message_id") or "")
    if source == "feishu":
        if not CHAT_ID_RE.match(chat_id):
            return False, "chat_id 形态非法（期望 oc_+32 位十六进制）"
        if not msg_id.startswith(MESSAGE_ID_PREFIX):
            return False, "message_id 不以 om_ 开头"
    else:
        # 企微：会话 id 形态不固定（群 chatid / 单聊 userid），
        # 但 message_id 必须在账本里 —— 同样是「桥接产出」才可信。
        if not msg_id:
            return False, "message_id 为空"
    if msg_id in ledger_ids():
        return True, ""
    deadline = time.time() + max(0.0, ledger_wait_seconds)
    while time.time() < deadline:
        time.sleep(0.2)        # 等桥接补账本（mtime 一变缓存就失效，会重读）
        if msg_id in ledger_ids():
            return True, ""
    return False, "永久账本里查不到这个 message_id（非桥接产出）"


def display_chat_name(ref: dict) -> str:
    """给提示词/看板用：群名只在其可信时才展示。

    chat_name_source 是新增字段（feishu / fallback / unknown）；老事件没这个字段时
    按「名字等于 chat_id」判为 fallback，否则按 unknown 处理 —— 不拿未标注的名字
    冒充真实群名。
    """
    name = str(ref.get("chat_name") or "").strip()
    cid = str(ref.get("chat_id") or "").strip()
    src = str(ref.get("chat_name_source") or "").strip()
    if not src:
        if not name:
            src = "unknown"
        elif cid and name == cid:
            src = "fallback"
        else:
            src = "unknown"
    if src == "feishu" and name:
        return f"{name}（群名可信）"
    if src == "fallback":
        return f"未识别（回退为会话 id {cid or '空'}）"
    return "未识别"


def reject_event(ev: dict, reason: str) -> None:
    """把一条不可核验的事件记下来：不建任务、不唤醒模型，只留痕给人工复核。"""
    ref = ev.get("source_ref") if isinstance(ev.get("source_ref"), dict) else {}
    rec = {
        "ts": now(),
        "event_id": ev.get("id", ""),
        "kind": ev.get("kind", ""),
        "source": ev.get("source", ""),
        "reason": reason,
        "status": "unverified",
        "need_manual": True,
        "chat_id": ref.get("chat_id", ""),
        "chat_name": ref.get("chat_name", ""),
        "chat_name_source": ref.get("chat_name_source", "") or "unknown",
        "message_id": ref.get("message_id", ""),
        "sender": ref.get("sender", ""),
        "text": (ev.get("text") or "")[:200],
    }
    try:
        UNVERIFIED_FILE.parent.mkdir(parents=True, exist_ok=True)
        with UNVERIFIED_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"记录 unverified 事件失败: {e}")
    log(f"事件 {ev.get('kind')} id={ev.get('id')} 来源不可核验（{reason}）→ 不建任务")
    try:
        append_digest(f"- [{now()[5:16]}] user.message {(ev.get('id') or '')[:8]}："
                      f"收到一条来源不可核验的事件（{reason}）→ 未建任务，待人工确认")
    except Exception:
        pass


def write_round(ev: dict) -> None:
    """记录本轮 router 被唤醒的事件（task + triggerId）。

    sa.py submit-decision 会校验 Decision 只能落在这一轮的 task 上 ——
    常驻 session 里堆着历次指令，模型偶尔会去处理旧指令，必须在代码层挡住。
    """
    try:
        ROUND_FILE.parent.mkdir(parents=True, exist_ok=True)
        ROUND_FILE.write_text(json.dumps({
            "task": ev.get("task"), "trigger_id": ev.get("id"),
            "kind": ev.get("kind"), "assignment": ev.get("assignment"),
            "source_ref": ev.get("source_ref") or {},
            "text": (ev.get("text") or "")[:2000],
            "ts": now(),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def digest_line(ev: dict, out: str) -> str:
    """把一轮压成摘要里的一行。"""
    kind = ev.get("kind") or "?"
    trig = ev.get("id") or ""
    task = ev.get("task") or "新任务"
    what = ""
    if ev.get("text"):
        what = ev["text"][:60]
    elif ev.get("summary"):
        what = ev["summary"][:60]
    elif ev.get("title"):
        what = ev["title"][:60]
    first = (out or "").strip().splitlines()
    head = next((l.strip() for l in first if l.strip()), "")[:90]
    return f"- [{now()[5:16]}] {kind} {trig[:8]} {task}：{what} → {head}"


def handle_event(sess: PiSession, ev: dict) -> None:
    write_round(ev)
    prompt = build_prompt(ev)
    log(f"事件 {ev.get('kind')} task={ev.get('task')} asg={ev.get('assignment')} → 唤醒 router")
    try:
        out = sess.prompt(prompt)
    except Exception as e:
        log(f"router 处理失败: {e}")
        OUTBOX.parent.mkdir(parents=True, exist_ok=True)
        with OUTBOX.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now(), "event": ev, "router": "",
                                "error": str(e)[:500]}, ensure_ascii=False) + "\n")
        try:
            sess.snapshot()
        except Exception:
            pass
        return
    out = (out or "").strip()
    log(f"router 回复：{out[:200]}")
    try:
        sess.snapshot()
    except Exception:
        pass
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    with OUTBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "event": ev, "router": out}, ensure_ascii=False) + "\n")
    task = ev.get("task")
    if task and (sa.task_dir(task) / sa.STATE_FILE).exists():
        sa.log_event(task, "router_decision", "router", out[:400] or "(无输出)")

    # ── 跨轮次摘要（落盘记录，供看板/排障与手动轮转时承接）──
    # 注意：session 是**单一长驻**的，不自动轮转（用户要求统一会话）。
    # 「别去处理旧任务」靠工具护栏 + 契约约束，不靠切断上下文。
    try:
        append_digest(digest_line(ev, out))
    except Exception:
        pass


PROCESSED_FILE = ROUTER_DIR / "processed.json"
# 一次唤醒最多连着处理几条事件（session 统一，连着处理省等待；太多会撑上下文）
BATCH_MAX = int(os.environ.get("SA_ROUTER_BATCH", "4"))


class InboxCursor:
    """跟踪 router/inbox.jsonl 的读取位置，**能感知同 inode 的就地改写**。

    之前只用「inode + 字节 offset」定位：文件被原地删改（inode 不变）时，
    router 仍拿着旧 offset，恢复读取时整段跳过了新写入的内容 ——
    已经投递的事件既没被处理、也没留在文件里（已实际丢过一批事件）。

    现在三重校验：
      · inode 变了 → 文件被替换 → 从头读
      · size < offset → 文件被截断 → 从头读
      · 已读前缀的内容指纹变了 → 同 inode 就地改写 → 从头读

    从头读不会重复处理：事件 id 去重靠 processed.json。
    """

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.offset = 0
        self.ino: tuple | None = None
        self._hasher = hashlib.sha256()
        self._sig = None
        self._data = b""
        self._pos = 0

    def _reset(self, ino: tuple) -> None:
        self.offset = 0
        self.ino = ino
        self._hasher = hashlib.sha256()

    def refresh(self) -> bool:
        """检查文件是否变化；检测到同 inode 就地改写时把读取位置重置到 0。

        返回 True 表示可能有新内容可读。
        """
        if not self.path.exists():
            return False
        try:
            data = self.path.read_bytes()
            st = self.path.stat()
        except OSError:
            return False
        ino = (st.st_ino, st.st_dev)
        sig = (ino[0], ino[1], len(data), st.st_mtime_ns)
        if sig == self._sig:
            return False            # 没变化 → 不用重算指纹
        self._sig = sig
        if self.ino != ino:
            log("inbox 文件被替换，重新打开")
            self._reset(ino)
        elif len(data) < self.offset:
            log("inbox 文件被截断，读取位置重置（靠 processed 去重）")
            self._reset(ino)
        elif hashlib.sha256(data[:self.offset]).digest() != self._hasher.copy().digest():
            log("inbox 同 inode 就地改写，读取位置重置到 0（靠 processed 去重，不重复处理）")
            self._reset(ino)
        self._data = data
        self._pos = self.offset
        return True

    def lines(self):
        """逐行吐出新增内容（每次 readline 都推进 offset 与内容指纹）。"""
        data = self._data
        pos = self._pos
        while pos < len(data):
            nl = data.find(b"\n", pos)
            end = len(data) if nl < 0 else nl + 1
            raw = data[pos:end]
            pos = end
            self.offset = pos
            self._hasher.update(raw)
            yield raw


def load_processed() -> set[str]:
    if not PROCESSED_FILE.exists():
        return set()
    try:
        return set(json.loads(PROCESSED_FILE.read_text(encoding="utf-8")) or [])
    except json.JSONDecodeError:
        return set()


def save_processed(ids: set[str]) -> None:
    try:
        PROCESSED_FILE.write_text(json.dumps(sorted(ids), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def run_loop(sess: PiSession, once: bool = False) -> None:
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    cursor = InboxCursor(INBOX)
    processed = load_processed()
    log(f"已处理事件 {len(processed)} 条（用于去重）")
    if sa.SANDBOX_MODE:
        log(f"沙箱模式：状态根 = {sa.SA_ROOT}（收件箱 {INBOX}，账本 {LEDGER_FILE}）")
    while True:
        batch = 0
        if cursor.refresh():
            for raw in cursor.lines():
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                eid = ev.get("id")
                if eid and eid in processed:
                    continue          # 同一事件不重复处理（清过日志 / 重新读也不会重放）
                ok, why = verify_user_event(ev, ledger_wait_seconds=1.2)
                if not ok:
                    # 来源不可核验 → 不建任何任务，只留痕（也要记已处理，不反复拦）
                    reject_event(ev, why)
                    if eid:
                        processed.add(eid)
                        save_processed(processed)
                    continue
                handle_event(sess, ev)
                if eid:
                    processed.add(eid)
                    save_processed(processed)
                if once:
                    return
                # 同一次唤醒里继续处理后面的事件 —— session 是统一的，
                # 连着处理多条省掉每条之间的一次唤醒等待（用户连发几条时体感差很多）。
                # 但别一次灌太多：单轮上下文压力会线性上升。
                batch += 1
                if batch >= BATCH_MAX:
                    break
        if once:
            return
        time.sleep(2)


def cmd_start(a: argparse.Namespace) -> None:
    try:
        subprocess.run([sys.executable, str(ROOT / "sync_skill.py")], check=False)
    except Exception as e:
        log(f"skill 同步失败（不阻塞）: {e}")
    if PID_FILE.exists():
        try:
            os.kill(int(PID_FILE.read_text().strip()), 0)
            print(f"已在运行 (pid {PID_FILE.read_text().strip()})")
            return
        except (OSError, ValueError):
            PID_FILE.unlink(missing_ok=True)
    subprocess.run(["pkill", "-f", "router.py run"], check=False)
    time.sleep(0.5)
    cmd = [sys.executable, str(ROOT / "router.py"), "run"]
    with open(LOG_FILE, "ab") as out:
        p = subprocess.Popen(cmd, cwd=str(REPO), stdout=out, stderr=out, start_new_session=True)
    PID_FILE.write_text(str(p.pid))
    time.sleep(3)
    log(f"router 已启动 pid={p.pid}，日志 {LOG_FILE}")


def cmd_rotate(a: argparse.Namespace) -> None:
    """手动轮转 session：归档当前 session，换一个全新 id，重启 router 开一个干净的。

    session 默认是**单一长驻**的（用户要求统一会话）。当它太大、响应变慢时，
    手动跑一次这个命令即可 —— 跨轮次连续性由 router/digest.md 承接。

    轮转做三件事：
      1. 先落**新的 session 状态**（换新 id）—— 万一崩在半路，状态也不会指向已归档的 id
      2. 停 router（等它真的退出），把顶层所有 router session 文件挪进 archive/
      3. 重启 router → pi 按新 id 开一个干净的会话

    注意：轮转会丢掉未处理的旧上下文（摘要还在），轮转前先确认没有在跑的轮次。
    """
    st = current_session_state()
    old_sid = st.get("session_id") or ""
    created = now()
    new_sid = make_session_id(created)
    # 先换状态再归档：状态永远指向「还没开、即将开」的那个 id
    save_session_state({
        "session_id": new_sid,
        "created_at": created,
        "name": session_display_name(created),
        "rotated_at": created,
        "previous_session_id": old_sid,
    })
    # 停 router（等它退出，否则它会继续往即将被归档的文件里写）
    cmd_stop(argparse.Namespace())
    # 清掉去重账本里的旧事件？不去 —— 事件去重要跨 session 保持，
    # 否则旧事件会被重放。轮转只影响模型上下文，不影响事件账本。
    sess_dir = router_session_dir()
    arch = sess_dir / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    moved = []
    for f in sess_dir.glob(f"*{SESSION_ID_PREFIX}*.jsonl"):
        shutil.move(str(f), str(arch / f.name))
        moved.append(f.name)
    # 起新 router → 它读新状态 → pi 按新 id 开新会话
    cmd_start(argparse.Namespace())
    print(f"session 已轮转：{old_sid} → {new_sid}，归档 {len(moved)} 个文件")
    for m in moved:
        print(f"  → archive/{m}")
    print(f"连续性由 {DIGEST_FILE} 承接（{len(read_digest(100).splitlines())} 行摘要）")


def cmd_model(a: argparse.Namespace) -> None:
    """切换 router 的模型 / 思考等级（运行中的 router 下一轮就会生效，不用重启）。

        router.py model <provider/model>   # 切到指定模型
        router.py model --restore          # 切回启动默认模型（降级后用这个）
        router.py model --status           # 看当前/主/降级状态
        router.py model --degraded-to <m>  # 直接指定降级目标（备用）
        router.py model --thinking low     # 改思考等级（off|minimal|low|medium|high|xhigh|max）
    """
    st = load_model_state()
    if a.status:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return
    if a.thinking:
        lv = str(a.thinking).strip().lower()
        if lv not in THINKING_LEVELS:
            print(f"思考等级要写成 {'|'.join(THINKING_LEVELS)}（收到 {a.thinking}）",
                  file=sys.stderr)
            raise SystemExit(2)
        st["thinking"] = lv
        save_model_state(st)
        print(f"思考等级已设为 {lv}（运行中的 router 下一轮生效）")
        return
    if a.restore:
        target = st.get("boot_primary") or os.environ.get("SA_ROUTER_MODEL") or DEFAULT_MODEL
        st["primary"] = target
        st["degraded"] = False
        st["degraded_to"] = ""
        st["degraded_at"] = ""
        save_model_state(st)
        print(f"已恢复主模型 {target}（运行中的 router 下一轮切回）")
        return
    target = a.model or a.degraded_to
    if not target:
        print("用法：router.py model <provider/model> | --restore | --status", file=sys.stderr)
        raise SystemExit(2)
    if "/" not in target:
        print(f"模型要写成 provider/model，例如 minimax-cn/MiniMax-M3.1-Flash（收到 {target}）",
              file=sys.stderr)
        raise SystemExit(2)
    st["primary"] = target
    st["degraded"] = False
    st["degraded_to"] = ""
    st["degraded_at"] = ""
    save_model_state(st)
    print(f"主模型已设为 {target}（运行中的 router 下一轮生效）")
    print(f"  降级链：{' → '.join([target] + [m for m in FALLBACK_MODELS if m != target])}")


def cmd_rename_sessions(a: argparse.Namespace) -> None:
    """给历史 router session 补上带开启时间的名字。

    会话选择器（pi --resume）显示的是 display name，早期创建的都叫
    「Super Agent Router」，在列表里分不清哪次是哪次。文件名前缀带 UTC 时间戳，
    据此回填成「Super Agent Router <本地时间>」。
    """
    sess_dir = router_session_dir()
    files = list(sess_dir.glob(f"*{SESSION_ID_PREFIX}*.jsonl"))
    arch = sess_dir / "archive"
    if arch.exists():
        files += list(arch.glob(f"*{SESSION_ID_PREFIX}*.jsonl"))
    renamed = 0
    for f in files:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        out, changed = [], False
        for line in lines:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                out.append(line)
                continue
            if d.get("type") == "session_info":
                nm = d.get("name") or ""
                if nm == "Super Agent Router" or not nm:
                    # 从文件名的 UTC 时间戳推出本地时间
                    stem = f.name.split("_")[0]
                    try:
                        ts = _dt.datetime.strptime(stem[:19], "%Y-%m-%dT%H-%M-%S").replace(
                            tzinfo=_dt.timezone.utc).astimezone().isoformat(timespec="seconds")
                        d["name"] = f"Super Agent Router {ts[:19].replace('T', ' ')}"
                        changed = True
                    except ValueError:
                        pass
                out.append(json.dumps(d, ensure_ascii=False))
            else:
                out.append(line)
        if changed:
            f.write_text("\n".join(out) + "\n", encoding="utf-8")
            renamed += 1
    print(f"已回填 {renamed} 个会话名（共扫描 {len(files)} 个）")


def cmd_stop(a: argparse.Namespace) -> None:
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, 15)
            log(f"已停止 router pid={pid}")
        except OSError as e:
            log(f"停止失败: {e}")
        PID_FILE.unlink(missing_ok=True)
        # 等它真的退出：归档 session 文件时它还在写的话，会写进已归档的文件
        if not wait_pid_gone(pid):
            log(f"router pid={pid} 15s 内没退出（可能在跑长任务）")
    else:
        print("未在运行")


def cmd_status(a: argparse.Namespace) -> None:
    running = False
    pid = None
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        try:
            os.kill(int(pid), 0)
            running = True
        except OSError:
            running = False
    inbox = 0
    if INBOX.exists():
        inbox = sum(1 for l in INBOX.read_text(encoding="utf-8").splitlines() if l.strip())
    outbox = 0
    if OUTBOX.exists():
        outbox = sum(1 for l in OUTBOX.read_text(encoding="utf-8").splitlines() if l.strip())
    # session 规模：判断「要不要手动轮转」用
    sess_msgs, sess_file = 0, ""
    if SESSION_FILE.exists():
        try:
            sj = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            sess_msgs = sj.get("message_count") or 0
            sess_file = sj.get("session_file") or ""
        except (json.JSONDecodeError, OSError):
            pass
    mst = load_model_state()
    sst = current_session_state()
    print(json.dumps({
        "running": running, "pid": pid, "session_id": sst.get("session_id"),
        "session_created_at": sst.get("created_at"),
        "session_name": sst.get("name"),
        "model": {"primary": mst.get("primary"), "active": mst.get("active"),
                  "degraded": mst.get("degraded"), "degraded_to": mst.get("degraded_to"),
                  "degraded_at": mst.get("degraded_at"),
                  "thinking": mst.get("thinking") or DEFAULT_THINKING,
                  "fallback_models": list(FALLBACK_MODELS)},
        "repo": str(REPO), "inbox_events": inbox, "router_decisions": outbox,
        "session_messages": sess_msgs,
        "digest_lines": len(read_digest(999).splitlines()),
        "session_file": sess_file,
        "log": str(LOG_FILE),
        "sandbox_mode": sa.SANDBOX_MODE,
        "state_root": str(sa.SA_ROOT),
        "inbox": str(INBOX),
        "ledger": str(LEDGER_FILE),
        "unverified": (sum(1 for l in UNVERIFIED_FILE.read_text(encoding="utf-8").splitlines()
                           if l.strip()) if UNVERIFIED_FILE.exists() else 0),
    }, ensure_ascii=False, indent=2))


def cmd_run(a: argparse.Namespace) -> None:
    try:
        subprocess.run([sys.executable, str(ROOT / "sync_skill.py")], check=False)
    except Exception:
        pass
    sess = PiSession(REPO)
    sess.start()
    log("router session 就绪，开始监听事件")
    try:
        run_loop(sess, once=a.once)
    except KeyboardInterrupt:
        log("router 退出")
    finally:
        sess.stop()


def cmd_tail(a: argparse.Namespace) -> None:
    if not OUTBOX.exists():
        print("(还没有 router 决策)")
        return
    lines = OUTBOX.read_text(encoding="utf-8").splitlines()
    for line in lines[-a.n:]:
        d = json.loads(line)
        ev = d.get("event", {})
        print(f"--- {d.get('ts')} [{ev.get('kind')}] {ev.get('task')}/{ev.get('assignment','')}")
        print((d.get("router") or "")[:800])
        print()


def main() -> None:
    ap = argparse.ArgumentParser(prog="router.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start"); p.set_defaults(func=cmd_start)
    p = sub.add_parser("stop"); p.set_defaults(func=cmd_stop)
    p = sub.add_parser("status"); p.set_defaults(func=cmd_status)
    p = sub.add_parser("run"); p.add_argument("--once", action="store_true"); p.set_defaults(func=cmd_run)
    p = sub.add_parser("tail"); p.add_argument("-n", type=int, default=10); p.set_defaults(func=cmd_tail)
    p = sub.add_parser("rename-sessions", help="给历史 router session 补上带开启时间的名字")
    p.set_defaults(func=cmd_rename_sessions)

    p = sub.add_parser("model", help="切换 router 模型：model <provider/model> | --restore | --status")
    p.add_argument("model", nargs="?", default="", help="目标模型，形如 provider/model")
    p.add_argument("--restore", action="store_true", help="切回启动默认模型")
    p.add_argument("--status", action="store_true", help="只看状态")
    p.add_argument("--degraded-to", default="", help="把降级目标改成指定模型")
    p.add_argument("--thinking", default="",
                   help="思考等级：off|minimal|low|medium|high|xhigh|max")
    p.set_defaults(func=cmd_model)

    p = sub.add_parser("rotate", help="手动轮转 session（归档当前会话，重开干净的；连续性靠 digest.md）")
    p.set_defaults(func=cmd_rotate)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
