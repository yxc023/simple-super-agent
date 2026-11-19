#!/usr/bin/env python3
"""飞书 → super-agent 桥接。

把飞书消息转成 super-agent 协议里的 Task，让主 agent 把它们当工作指令。

    python3 super-agent/lark_bridge.py pull --once     # 拉一次
    python3 super-agent/lark_bridge.py pull --follow   # 常驻轮询
    python3 super-agent/lark_bridge.py event           # 事件长连接（被占用时自动降级到 pull）
    python3 super-agent/lark_bridge.py reply --task T001 --text "结果…"
    python3 super-agent/lark_bridge.py status

为什么默认是 pull：
本机飞书助手 app（cli_<app_id>）的事件总线已被 HiFox 运行时占用，
`lark-cli event consume` 会报 "another event bus is already connected"。
所以默认走 `im +chat-messages-list` 轮询（user 身份读会话消息），不抢总线。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import pathlib
import queue
import re
import shutil
import subprocess
import threading
import sys
import time
import datetime as _dt

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import sa  # noqa: E402
import policy  # noqa: E402

CONFIG_FILE = ROOT / "lark_config.json"
# 机器人自己的 open_id（用于精确判断 mentions 里 @ 的是不是自己）
BOT_OPEN_ID = ""

# ── 状态文件全部从 sa.SA_ROOT 派生（沙箱演练只需设 SA_ROOT）──
# 以前这里各自 ROOT / "..." 写死，验证脚本只能逐个 monkeypatch，漏一个就写生产：
# 漏的恰恰是 sa.notify 的出口，于是一次「只读演练」凭空造出了真实任务。
def _state_path(env_name: str, default_rel: str) -> pathlib.Path:
    p = sa.env_path(env_name)
    return p if p is not None else sa.SA_ROOT / default_rel


STATE_FILE = _state_path("SA_STATE_FILE", "lark_state.json")
LOG_FILE = sa.SA_ROOT / "logs" / "lark-bridge.log"
# 守护进程 pid 文件 —— 记的是**真正干活的 python 进程**，不是包装 shell
# 沙箱里记到自己的状态根下：避免演练进程的 pid 覆盖生产 pid 文件
PID_FILE = (pathlib.Path("/tmp/super-agent-lark-bridge.pid")
            if not sa.SANDBOX_MODE else sa.SA_ROOT / ".lark-bridge.pid")


# ------------------------------------------------------------------ 配置/状态


def load_config() -> dict:
    global BOT_OPEN_ID
    cfg = _load_config_raw()
    BOT_OPEN_ID = (cfg.get("bot", {}) or {}).get("open_id", "")
    return cfg


def _load_config_raw() -> dict:
    if not CONFIG_FILE.exists():
        print(f"缺少配置文件：{CONFIG_FILE}", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


LOCK_FILE = _state_path("SA_LOCK_FILE", "lark_state.lock")


@contextlib.contextmanager
def state_lock(wait_seconds: float = 0.0):
    """保证同一时刻只有一个进程在处理消息（daemon + 手动 pull 不能并行）。

    wait_seconds > 0 时会等待锁，而不是立刻放弃 —— 事件订阅模式下，
    官方要求 3 秒内处理完事件，抢不到锁就丢弃会造成消息丢失。
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    # "a+" 而不是 "w"：flock 只关心 inode，"w" 会在拿到锁之前就把文件截断
    with LOCK_FILE.open("a+") as f:
        deadline = time.time() + max(0.0, wait_seconds)
        got = False
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    break
                time.sleep(0.2)
        if not got:
            print("另一个 lark_bridge 正在处理消息，本次跳过", file=sys.stderr)
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def default_state() -> dict:
    """状态文件的初始结构（缺键时用它补齐）。"""
    return {"seen": {}, "last_poll": None, "mode": None, "created_tasks": 0}


def load_state() -> dict:
    """读状态。

    文件损坏（断电/被 kill 写成半截）时不能让整个桥接永久瘫痪：
    备份坏文件 + 回退到默认结构，让它能重新开始。
    """
    if not STATE_FILE.exists():
        return default_state()
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        try:
            STATE_FILE.with_suffix(".json.bak").write_text(
                STATE_FILE.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        except OSError:
            pass
        print(f"状态文件损坏，已备份并重建：{e}", file=sys.stderr)
        return default_state()
    if not isinstance(st, dict):
        return default_state()
    base = default_state()
    base.update(st)
    base["seen"] = st.get("seen") if isinstance(st.get("seen"), dict) else {}
    return base


def save_state(st: dict) -> None:
    """原子写状态：临时文件 + os.replace，避免读者读到半截 JSON。"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def write_pid_file() -> None:
    """把自己（真正干活的 python 进程）的 pid 写进 pid 文件。

    包装 shell 记的往往是中间那层 shell 的 pid —— 存活判定会指错对象。
    """
    try:
        PID_FILE.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    except OSError:
        pass


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{_dt.datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}\n"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line)
    print(msg, flush=True)


# ── 收到但不受理：一律留一行痕 ──
# 以前「桥接收到了、但这条消息不受理」是全静默的：日志里没有任何记录，账本/收件箱
# 只在部分分支里写。于是「机器人明明在群里、却像没看见」时无从判断是没收到、还是
# 收到了没受理（一次误发的畸形消息就是这么查不出原因的）。
# 现在每个不受理出口都打一行：message_id 前 8 位 + 会话 id 形态 + 判定原因。
CHAT_ID_RE = re.compile(r"^oc_[0-9a-f]{32}$")
# 进程内的「已记过日志」集合（只为防重复刷屏，不持久化）
_LOGGED_IGNORED: set[str] = set()


def chat_id_shape(chat_id: str) -> str:
    """会话 id 的形态（**只用于日志判据，不做拦截**）。"""
    cid = (chat_id or "").strip()
    if not cid:
        return "空"
    if CHAT_ID_RE.match(cid):
        return "oc_+32位hex"
    if cid.startswith("oc_"):
        return f"oc_+{len(cid) - 3}位非规范"
    return f"非oc_前缀（长度{len(cid)}）"


def log_ignored(mid: str, chat_id: str, reason: str, text: str = "") -> None:
    """一条「收到了但不受理」的日志（message_id 只露前 8 位）。

    与 ignored 记账保持一致：走 ignored 记账的分支，原因与记账里的 action 对得上；
    白名单/形态类分支本来就不记账（不把陌生人的消息钉进永久账本），只留日志。

    同一进程内同一条消息 + 同一原因只记一次：轮询每轮都会重看同一条未受理的消息，
    不去重会把日志刷满。去重表只活在本进程内存里（不写任何状态文件）——
    进程重启后再记一遍，而这正是「此刻真的收到过它」的证据。
    """
    mid_s = (mid or "").strip()
    key = f"{mid_s}|{reason}"
    if key in _LOGGED_IGNORED:
        return
    if len(_LOGGED_IGNORED) > 5000:
        _LOGGED_IGNORED.clear()
    _LOGGED_IGNORED.add(key)
    shown = (mid_s[:8] + "…") if mid_s else "<?>"
    tail = f" 摘要={text[:30]!r}" if text else ""
    log(f"不受理：消息 {shown}（message_id 前8位）会话形态={chat_id_shape(chat_id)} "
        f"原因={reason}{tail}")


def pid_alive(pid: int) -> bool:
    """进程是否存活（EPERM 也算活着：进程在，只是不归我们管）。"""
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def pid_cmdline(pid: int) -> str:
    """读某个 pid 的命令行（读不到返回空串，不抛异常）。"""
    try:
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return (out.stdout or "").strip()


def daemon_pid_info() -> dict:
    """`status` 的 pid 字段：pid 文件里的**守护进程** pid，不是查询进程自己。

    以前直接写 `os.getpid()`：每次 `lark_bridge.py status` 报的都是那个临时查询
    进程的 pid —— 守护进程早就退了也看不出来，巡检/看板据此判断存活必然误判。

    这里的取值链是：pid 文件 → 存活校验 → （能读到命令行时）确认它真的是桥接进程，
    防止 pid 复用把「别的进程」当成桥接。任一环节不成立就如实标不可信。
    """
    info = {"pid": None, "pid_file": str(PID_FILE), "pid_source": "pid_file",
            "daemon_alive": False, "daemon_cmd": "", "pid_matches_bridge": None,
            "running": False, "status_pid": os.getpid()}
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return info
    info["pid"] = pid
    info["daemon_alive"] = pid_alive(pid)
    if info["daemon_alive"]:
        cmd = pid_cmdline(pid)
        info["daemon_cmd"] = cmd[:200]
        if cmd:
            info["pid_matches_bridge"] = "lark_bridge" in cmd
    info["running"] = bool(info["daemon_alive"] and info["pid_matches_bridge"] is not False)
    return info


# ------------------------------------------------------------------ lark-cli


def lark(args: list[str], timeout: int = 60) -> dict:
    """跑一条 lark-cli 命令，返回解析后的 JSON。"""
    cmd = ["lark-cli", *args, "--json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"lark-cli 超时: {' '.join(cmd)}")
    out = (p.stdout or "").strip()
    if not out:
        raise RuntimeError(f"lark-cli 无输出 (exit {p.returncode}): {(p.stderr or '')[:400]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"lark-cli 输出非 JSON: {out[:300]}")
    if not data.get("ok", False):
        err = data.get("error", {})
        raise RuntimeError(f"lark-cli 失败: {err.get('type')}/{err.get('subtype')}: {err.get('message')}")
    return data


def fetch_messages(chat_id: str, page_size: int = 50, max_pages: int = 5,
                   stop_at_seen: str | None = None) -> list[dict]:
    """拉会话消息。

    注意排序方向：群里消息会超过单页上限，必须**从新往旧**拉（order=desc），
    否则新消息永远排在第一页之外。翻页直到遇到已读消息或页数用尽。
    """
    out: list[dict] = []
    token = ""
    for _ in range(max_pages):
        args = ["im", "+chat-messages-list", "--as", "user", "--chat-id", chat_id,
                "--page-size", str(page_size), "--order", "desc"]
        if token:
            args += ["--page-token", token]
        data = lark(args)
        d = data.get("data", {}) or {}
        msgs = d.get("messages", []) or []
        if not msgs:
            break
        hit_seen = False
        for m in msgs:
            mid = m.get("message_id")
            if stop_at_seen and mid == stop_at_seen:
                hit_seen = True
                break
            out.append(m)
        if hit_seen or not d.get("has_more"):
            break
        token = d.get("page_token") or ""
        if not token:
            break
    out.reverse()   # 按时间正序返回
    return out


def discover_chats(cfg: dict) -> list[dict]:
    """动态发现机器人所在的群。

    配置里写死的 chats 只是「重点群」；团队成员在别的群里 @ 机器人时，
    那些群也要能被收到 —— 否则「别人 @ 了但没反应」。
    """
    dc = cfg.get("chat_discovery", {}) or {}
    if not dc.get("enabled"):
        return []
    types = dc.get("types") or ["group"]
    try:
        data = lark(["im", "+chat-list", "--as", "bot", "--types", ",".join(types)])
    except Exception as e:
        log(f"发现群列表失败: {e}")
        return []
    chats = (data.get("data", {}) or {}).get("chats", []) or []
    out = []
    exclude = [str(x) for x in (dc.get("exclude") or [])]
    contains = [str(x) for x in (dc.get("name_contains") or [])]
    for c in chats:
        cid, name = c.get("chat_id", ""), c.get("name", "")
        if not cid:
            continue
        if cid in exclude or name in exclude:
            continue
        if contains and not any(k in name for k in contains):
            continue
        out.append({"chat_id": cid, "name": name, "mode": "group", "discovered": True})
    return out


def fetch_thread_messages(thread_id: str, page_size: int = 50) -> list[dict]:
    """拉一个话题串里的消息（正序）。

    话题里的回复**不在**主频道消息列表里 —— 只轮询主频道会完全看不到用户在
    话题里追加的指令。所以要单独轮询已知话题串。
    """
    data = lark(["im", "+threads-messages-list", "--as", "user", "--thread", thread_id,
                 "--page-size", str(page_size), "--order", "asc"])
    return list((data.get("data", {}) or {}).get("messages", []) or [])


def norm_ts(v) -> str:
    """归一化时间戳，让飞书时间与本地时间可比。

    飞书消息的 create_time 形如 "2026-11-18 10:53"（空格分隔、无秒无时区），
    本地时间形如 "2026-11-18T10:53:00"。直接比字符串会因为 " " < "T" 而全部误判。
    """
    t = str(v or "").strip()
    if not t:
        return ""
    t = t.replace("/", "-")
    if len(t) == 16 and t[10] == " ":
        t = t[:10] + "T" + t[11:] + ":00"
    if len(t) == 19 and t.endswith("+08:00"):
        t = t[:19]
    return t


def reply_in_thread() -> bool:
    """是否走飞书话题串（配置项 reply_in_thread，默认 True）。"""
    try:
        return bool(load_config().get("reply_in_thread", True))
    except SystemExit:
        return True


def send_reply(message_id: str, text: str, in_thread: bool | None = None) -> dict:
    """回复一条消息，返回渠道响应（用于记录投递凭据）。

    默认走**飞书话题**（--reply-in-thread）：回复出现在原消息的话题串里，
    而不是混在群聊主频道。群消息是多人共享的，话题串能让「这条消息 → 一串回复」
    归到一起，不刷屏。
    """
    if in_thread is None:
        in_thread = reply_in_thread()
    cmd = ["im", "+messages-reply", "--as", "bot", "--message-id", message_id, "--text", text]
    if in_thread:
        cmd.append("--reply-in-thread")
    return lark(cmd)


def send_text(chat_id: str, text: str) -> dict:
    return lark(["im", "+messages-send", "--as", "bot", "--chat-id", chat_id, "--text", text])


# ------------------------------------------------------------------ 表情

# 三段状态用不同表情挂在**用户那条消息**上，一眼能看出走到哪一步了：
#   ⌨️ Typing                收到任务（敲键盘 = 正在处理）
#   💭 Status_PrivateMessage 分配任务（思考中 = 蓝色气泡三个点）
#   ✅ DONE                  完成任务
#
# 表情是**替换**不是叠加：一条消息上只留当前状态那一个表情
# （接收 → 分配 → 完成 依次替换），否则越叠越多看不出走到哪一步了。
# 完成是终态，不会被降级回分配/接收。
#
# 收到 / 分配**只发表情、不发文字**（受理回执那条消息会被 suppress）；
# 完成的「结果」文字仍然发 —— 那是干活的产出，不是状态播报。
DEFAULT_REACTIONS = {"received": "Typing", "dispatched": "Status_PrivateMessage",
                     "completed": "DONE"}


def reaction_config() -> dict:
    """表情配置：lark_config.json 的 reactions 段（可整体关掉）。"""
    try:
        cfg = load_config().get("reactions") or {}
    except SystemExit:
        cfg = {}
    out = dict(DEFAULT_REACTIONS)
    for k in DEFAULT_REACTIONS:
        if cfg.get(k):
            out[k] = cfg[k]
    out["enabled"] = bool(cfg.get("enabled", True))
    # 受理回执（ack）改成「只发表情、不发文字」
    out["suppress_ack_text"] = bool(cfg.get("suppress_ack_text", True))
    return out


def list_reactions(message_id: str) -> list[dict]:
    """列出消息上的表情记录（含 reaction_id 与操作者）。"""
    try:
        d = lark(["im", "reactions", "list", "--as", "bot",
                  "--params", json.dumps({"message_id": message_id, "page_size": 50})])
        return d.get("data", {}).get("items") or []
    except Exception as e:
        log(f"读表情列表失败 {message_id}：{str(e)[:140]}")
        return []


def delete_reaction(message_id: str, reaction_id: str) -> bool:
    """撤掉一个表情回复（reaction_id 来自 list）。"""
    if not reaction_id:
        return False
    try:
        lark(["im", "reactions", "delete", "--as", "bot",
              "--params", json.dumps({"message_id": message_id, "reaction_id": reaction_id})])
        return True
    except Exception as e:
        log(f"撤表情失败 {reaction_id}：{str(e)[:140]}")
        return False


def _own_reactions(items: list[dict]) -> list[dict]:
    """筛出**我们自己**加的表情（bot 身份，operator_type=app）。"""
    try:
        app_id = (load_config().get("bot") or {}).get("app_id") or ""
    except SystemExit:
        app_id = ""
    out = []
    for r in items or []:
        op = r.get("operator") or {}
        if op.get("operator_type") != "app":
            continue
        if app_id and op.get("operator_id") and op.get("operator_id") != app_id:
            continue
        out.append(r)
    return out


def react(message_id: str, emoji_type: str) -> bool:
    """给一条消息**替换**成指定表情回复。

    不是叠加：一条消息上只保留当前状态那一个表情。做法是
      1. 读这条消息上的表情，挑出我们自己加的；
      2. 同类型已在 → 只需把别的删掉；不同类型 → **先加新的、再删旧的**；
      3. 已完成（DONE）不降级 —— 完成是终态。

    表情是锦上添花，不是链路成败的判据 —— 所以**失败只记日志不抛**，
    不能因为一个表情挂不上就把整条消息投递搞挂。
    """
    if not message_id or not emoji_type:
        return False
    t0 = time.time()
    # 我们自己的表情台账（logs/reactions.jsonl）：如果这条消息上**我们从没加过**表情，
    # 那就没有旧表情需要删 —— 直接 create，省掉一次 list（一次 lark-cli ≈ 1.3s，
    # 而用户等的正是第一个「敲键盘」）。
    try:
        mine_log = [json.loads(ln) for ln in
                    (LOG_FILE.parent / "reactions.jsonl").read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
    except Exception:
        mine_log = []
    touched = any(r.get("message_id") == message_id for r in mine_log)
    try:
        if touched:
            mine = _own_reactions(list_reactions(message_id))
        else:
            mine = []
        cur = [r for r in mine if (r.get("reaction_type") or {}).get("emoji_type") == emoji_type]
        others = [r for r in mine if (r.get("reaction_type") or {}).get("emoji_type") != emoji_type]
        # 完成是终态：已经被标完成的消息，不再退回分配/接收
        if others and emoji_type != "DONE" and any(
                (r.get("reaction_type") or {}).get("emoji_type") == "DONE" for r in others):
            log(f"{message_id} 已标记完成，不再降级为 {emoji_type}")
            return False
        if cur:
            # 目标表情已经在身上，只需把别的旧表情清掉
            for r in others:
                delete_reaction(message_id, r.get("reaction_id") or "")
            log(f"表情已是 {emoji_type} → {message_id}（{int((time.time()-t0)*1000)}ms）")
            return True
        stale = [r.get("reaction_id") or "" for r in others]
    except Exception as e:
        log(f"替换表情前置处理失败 {message_id}：{str(e)[:140]}")
        stale = []
    try:
        lark(["im", "reactions", "create", "--as", "bot",
              "--params", json.dumps({"message_id": message_id}),
              "--data", json.dumps({"reaction_type": {"emoji_type": emoji_type}})])
        # **先加新的，再删旧的** —— 反过来（先删后加）会在两次 API 调用之间
        # 出现一个「消息上什么表情都没有」的空档，用户看着像卡住。
        # 顺序反过来最多是短暂新旧并存，肉眼不可见。
        for rid in stale:
            delete_reaction(message_id, rid)
        log(f"已加表情 {emoji_type} → {message_id}（{int((time.time()-t0)*1000)}ms）")
        # 表情时间线写**独立的 append-only 文件**，不写进 lark_state.json：
        # state 会被轮询/事件两条路径反复 load-save，写进去会被后写覆盖丢掉。
        try:
            rlog = LOG_FILE.parent / "reactions.jsonl"
            rlog.parent.mkdir(parents=True, exist_ok=True)
            with rlog.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": sa.now(), "emoji": emoji_type,
                                    "message_id": message_id,
                                    "elapsed_ms": int((time.time() - t0) * 1000)},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass
        return True
    except Exception as e:
        log(f"加表情失败 {emoji_type} → {message_id}：{str(e)[:160]}")
        return False


def resolve_thread_id(message_id: str) -> str:
    """查一条消息所在的话题 id（omt_xxx）。

    **话题什么时候才有 id？**
      · 用户在话题里发消息 —— 事件里直接带 thread_id + root_id，无需查。
      · 用户在主频道发消息 —— 事件里**没有** thread_id；话题是在机器人第一次
        以「话题回复」方式回话时由飞书创建的，那一刻才出现 omt_xxx。
        而 `+messages-reply --reply-in-thread` 的返回里**不给** thread_id，
        要用 `+messages-mget` 回查那条回复自己。
    """
    if not message_id:
        return ""
    try:
        d = lark(["im", "+messages-mget", "--as", "bot", "--message-ids", message_id])
        items = d.get("data", {}).get("messages") or d.get("data", {}).get("items") or []
        if items and isinstance(items[0], dict):
            return str(items[0].get("thread_id") or "").strip()
    except Exception as e:
        log(f"查话题 id 失败 {message_id}：{str(e)[:140]}")
    return ""


def delivery_receipt(resp: object) -> dict:
    """从渠道响应里抽「投递凭据」（新消息 id / 请求 id）。

    取不到就是取不到 —— 不要拿本地时间自欺欺人：凭据是事后对账的唯一依据。
    """
    KEYS_MID = ("message_id", "messageId", "msg_id")
    KEYS_REQ = ("request_id", "requestId", "log_id", "logId", "trace_id")

    def find(obj, keys, depth: int = 0) -> str:
        if depth > 6:
            return ""
        if isinstance(obj, dict):
            for k in keys:
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            for v in obj.values():
                got = find(v, keys, depth + 1)
                if got:
                    return got
        elif isinstance(obj, (list, tuple)):
            for v in obj[:5]:
                got = find(v, keys, depth + 1)
                if got:
                    return got
        return ""

    mid = find(resp, KEYS_MID)
    rid = find(resp, KEYS_REQ)
    return {"message_id": mid, "request_id": rid,
            "verified": bool(mid or rid)}


# ------------------------------------------------------------------ 过滤


def strip_mentions(text: str, names: list[str]) -> tuple[str, bool]:
    """去掉 @机器人 的痕迹，返回 (清洗后的文本, 是否@了机器人)。"""
    mentioned = False
    for n in names:
        for pat in (f"@{n}", f"@{n} "):
            if pat in text:
                mentioned = True
                text = text.replace(pat, "")
    text = re.sub(r"\s+", " ", text).strip()
    return text, mentioned


def is_trigger(text: str, chat_mode: str, trig: dict, bot_names: list[str],
               allow_prefix: bool = True) -> tuple[bool, str]:
    """判断这条消息是否构成一条工作指令。返回 (是否触发, 清洗后的正文)。

    allow_prefix=False 时（「派单」「任务」等前缀词）不触发 —— 用于动态发现的其他群：
    陌生人随口一句「任务」不该把机器人叫起来。
    """
    prefixes = trig.get("prefix_always_triggers") or []
    if allow_prefix:
        for p in prefixes:
            if text.startswith(p):
                return True, text[len(p):].strip()
    if chat_mode == "p2p":
        return True, text
    if trig.get("group_requires_mention", True):
        clean, mentioned = strip_mentions(text, bot_names)
        if mentioned:
            return True, clean
        return False, text
    return True, text


# ------------------------------------------------------------------ 建任务


def bot_mentioned(ev: dict, bot_names: list[str]) -> bool:
    """事件里是否 @ 了机器人。

    事件带结构化的 mentions 字段 —— 比字符串匹配可靠得多。
    """
    for m in (ev.get("mentions") or []):
        mid = m.get("id") or m.get("open_id") or m.get("key") or ""
        name = m.get("name") or ""
        if mid and mid == BOT_OPEN_ID:
            return True
        if name and name in bot_names:
            return True
    txt = str(ev.get("content") or "")
    return any(n in txt for n in bot_names)


def process_event(cfg: dict, state: dict, ev: dict) -> str:
    """处理一条消息事件 → 返回动作名（delivered / ignored / ...）。

    事件模式（官方长连接）与轮询模式共用这段逻辑。
    """
    trig = cfg.get("trigger", {})
    bot_names = [cfg.get("bot", {}).get("name", "飞书助手")] + (trig.get("mention_names") or [])
    allowed = {s["open_id"] for s in cfg.get("senders", [])}
    allow_any = bool(cfg.get("allow_any_sender", False))

    mid = ev.get("message_id") or ev.get("id") or ""
    if not mid:
        log_ignored("", ev.get("chat_id") or "", "事件缺 message_id（形态异常）")
        return "dup"
    if mid in state.get("seen", {}):
        return "dup"
    if mid in ledger_ids():
        # 永久账本里已有这条消息（上一轮路径处理过 / 补账补写过）→ 绝不重复受理。
        # 两条接收路径（事件长连接 / 轮询兜底）共用这个账本去重。
        state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "ledger-dedup"}
        return "dup"
    chat_id = ev.get("chat_id") or ""
    sender_id = ev.get("sender_id") or ""
    sender_type = ev.get("sender_type") or "user"

    # ── 话题识别与话题表登记（两条路径共用同一张表）──
    tid = ev.get("thread_id") or ev.get("root_id") or ""
    in_thread = bool(tid)
    threads = state.setdefault("threads", {})
    chat = {"chat_id": chat_id, "name": ev.get("chat_name") or chat_id,
            "mode": "p2p" if ev.get("chat_type") == "p2p" else "group"}
    if in_thread:
        tinfo = threads.get(tid)
        if tinfo is None:
            # 新话题：登记进轮询侧话题表 —— 否则长连接一旦降级为轮询，
            # 这个话题的消息一条都收不到（静默丢失）。
            tinfo = {"chat": chat, "bot_engaged": False,
                     "discovered_at": sa.now(), "discovered_by": "event"}
            threads[tid] = tinfo
            log(f"新话题已登记到轮询话题表：{tid}（长连接降级为轮询后也能收到）")
        if sender_type != "user":
            # 机器人自己（或其它应用）在这个话题里说过话 → 该话题已被接手
            tinfo["bot_engaged"] = True

    if trig.get("ignore_non_user_sender", True) and sender_type != "user":
        log_ignored(mid, chat_id, f"非用户发送者（sender_type={sender_type}）")
        return "not_user"
    if allowed and not allow_any and sender_id not in allowed:
        state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "not_allowed"}
        log_ignored(mid, chat_id, "发送者不在白名单")
        return "not_allowed"

    # ── 准入判定：谁能发起命令 ──
    # 规则（policy.json）：私聊只允许 owner；群聊只允许 owner 指定过的群；
    # 某个任务里临时授权的人可以在该任务的会话里发言。
    chat_type = ev.get("chat_type") or ""
    verdict = policy.check_command(
        chat_type=chat_type, chat_id=chat_id, sender_id=sender_id,
        sender_name=ev.get("sender_name") or "", thread_id=tid)
    if not verdict["allow"]:
        state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "policy_denied",
                              "reason": verdict["reason_code"]}
        log_ignored(mid, chat_id, f"准入拒绝（{verdict['reason_code']}）：{verdict['detail']}",
                    text="")
        return "policy_denied"
    grant_task = verdict.get("grant_task") or ""

    text = (ev.get("content") or "").strip()
    if not text:
        log_ignored(mid, chat_id, "正文为空（形态异常）")
        return "empty"
    msg = {"message_id": mid, "content": text, "sender": {"id": sender_id},
           "create_time": ev.get("create_time", ""),
           "thread_id": ev.get("thread_id") or "", "root_id": ev.get("root_id") or ""}
    if in_thread:
        body = strip_mentions(text, bot_names)[0] or text
        mentioned = bot_mentioned(ev, bot_names)
        # 话题内是否免 @：
        #   默认（trigger.thread_requires_mention=true）——必须明确指向机器人
        #     （@ 了机器人，或用了「派单/任务/!」这类指令前缀）才受理；
        #   配成 false 时才按轮询路径的语义：机器人已接手过的话题免 @。
        # 事件长连接路径原来只判「落在话题里」就免 @，任意话题里的普通发言
        # 都会被当成指令受理并唤醒 router。
        strict_thread = trig.get("thread_requires_mention", True)
        if strict_thread:
            need_mention = True
        else:
            need_mention = (trig.get("group_requires_mention", True)
                            and not tinfo.get("bot_engaged"))
        prefix_hit = ""
        for p in (trig.get("prefix_always_triggers") or []):
            if text.startswith(p):
                prefix_hit = p
                break
        if need_mention and not (mentioned or prefix_hit):
            state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "ignored"}
            ledger_append(mid, "", text, "ignored")
            record_message(mid, chat, msg, "ignored", body=text)
            log_ignored(mid, chat_id, "话题消息未明确指向机器人（无事由 @ / 无指令前缀）", text)
            return "ignored"
        if prefix_hit and not mentioned:
            body = text[len(prefix_hit):].strip() or body
        if mentioned:
            # @ 了机器人 → 该话题从此算「机器人已接手」
            tinfo["bot_engaged"] = True
    else:
        chat_mode = "p2p" if ev.get("chat_type") == "p2p" else "group"
        mentioned = bot_mentioned(ev, bot_names)
        if chat_mode == "group" and trig.get("group_requires_mention", True) and not mentioned:
            state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "ignored"}
            ledger_append(mid, "", text, "ignored")
            record_message(mid, chat, msg, "ignored", body=text)
            log_ignored(mid, chat_id, "群消息未 @ 机器人（不是给我的）", text)
            return "ignored"
        body = strip_mentions(text, bot_names)[0] or text
    if len(body) < trig.get("min_length", 2):
        log_ignored(mid, chat_id, f"正文过短（<{trig.get('min_length', 2)} 字）", body)
        return "too_short"

    # 先记账、再投 router —— 账本是「这条消息确实来自桥接」的凭据，
    # router 收到事件后会立刻回查账本。先投后记会漏：
    # router 空闲时会毫秒级处理，账本还没写就被判成「非桥接产出」丢弃。
    record_message(mid, chat, msg, "delivered", body=body)
    ledger_append(mid, "", body, "delivered")
    try:
        e = deliver_user_message(cfg, chat, msg, body,
                                 grant={"grant_task": grant_task,
                                        "grant": (verdict.get("grant") or {})})
    except Exception as ex:
        log(f"投递 router 失败: {ex}")
        return "error"
    state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "delivered", "trigger": e.get("id")}
    log(f"消息已投 router（trigger {e.get('id')}）{'[话题]' if in_thread else ''}：{body[:50]}")
    return "delivered"


def chat_name_source(chat: dict) -> str:
    """群名这个**展示字段**的可信度：feishu / fallback / unknown。

    以前 source_ref 里只有 chat_name，没任何可信度标记 —— 外部塞进来一个
    「群X」和一个合法形态的会话 id 混在一起，看板/提示词里看起来就跟真实群名
    一模一样（真实事件反而是「群名=chat_id」的 fallback）。

    口径：
      · feishu   —— 名字来自飞书群信息（不是 chat_id 本身）
      · fallback —— 拿不到名字，用 chat_id 顶上
      · unknown  —— 连 chat_id 都没有（来源本身就不完整）
    """
    name = (chat.get("name") or "").strip()
    cid = (chat.get("chat_id") or "").strip()
    if not name:
        return "unknown" if not cid else "fallback"
    if cid and name == cid:
        return "fallback"
    return "feishu"


def deliver_user_message(cfg: dict, chat: dict, msg: dict, body: str,
                         grant: dict | None = None) -> dict:
    """把一条飞书消息交给 router 判断（**不直接建任务**）。

    用户的话可能是新目标，也可能是针对之前的任务。判断权在 router：
    它拿到 user.message 事件后，提交 Decision 决定新建任务还是沿用某个已有任务，
    以及沿用哪个 agent 的哪个 session。

    返回投递出去的事件。
    """
    sender = msg.get("sender", {}) or {}
    msg_id = msg.get("message_id", "")
    source_ref = {
        "chat_id": chat.get("chat_id", ""),
        "chat_name": chat.get("name", ""),
        # 会话形态（p2p / group）—— router 要据此判断「是不是私聊」，
        # 早读这类只在本人私聊触发的 worker 需要它来填 Decision.requester.chatType。
        "chat_type": chat.get("mode", "") or "",
        # 群名的可信度：router / 看板据此决定显示真实群名还是「未识别」
        "chat_name_source": chat_name_source(chat),
        "message_id": msg_id,
        "sender": sender.get("id", ""),
        "sender_name": sender.get("name", ""),
        "sent_at": msg.get("create_time", ""),
        "link": msg.get("message_app_link", ""),
        # 话题标识：链路各层都要能归位到具体话题串（收件箱 / router 事件 / 任务）
        "thread_id": msg.get("thread_id", "") or chat.get("thread_id", ""),
        "root_id": msg.get("root_id", "") or chat.get("root_id", ""),
    }
    # 临时授权命中 → 把「哪个任务在等他回答、问了什么」带进事件，
    # router 才能知道该延续哪个任务（而不是把它当成新任务）。
    grant_ctx = {}
    if grant:
        gt = grant.get("grant_task") or ""
        g = grant.get("grant") or {}
        grant_ctx = {"grant_task": gt, "granted_to": g.get("name") or "",
                     "question": g.get("question") or "", "reason": g.get("reason") or ""}
        if gt:
            try:
                policy.mark_grant_used(gt, g.get("open_id") or "")
            except Exception:
                pass
    ev = sa.notify("user.message", None,
                   text=body.strip(),
                   source_ref=source_ref,
                   grant=grant_ctx or None,
                   source="feishu")
    log(f"飞书消息已投递 router（待判断新建/沿用）：{body[:60]}")
    # 收到任务 → 在用户那条消息上挂 👌，不用等 router 想清楚
    rc = reaction_config()
    if rc.get("enabled"):
        react(msg_id, rc["received"])
    return ev


# ------------------------------------------------------------------ 拉取


MESSAGES_FILE = sa.INBOX_DIR / "messages.jsonl"
LEDGER_FILE = sa.LEDGER_FILE


def ledger_ids() -> set[str]:
    """永久账本：已处理过的 message_id。

    lark_state.json 里的 seen 是工作状态，可能被清掉；这个账本是 append-only 的，
    用来保证「同一条消息永远不会被当成新指令重放」。
    """
    if not LEDGER_FILE.exists():
        return set()
    out = set()
    for line in LEDGER_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.add(json.loads(line)["message_id"])
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def ledger_append(mid: str, task: str, body: str = "", action: str = "task") -> None:
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"message_id": mid, "task": task, "action": action,
                            "ts": sa.now(), "content": body[:200]}, ensure_ascii=False) + "\n")


def record_message(mid: str, chat: dict, msg: dict, action: str, task: str = "", body: str = "") -> None:
    """把「收到的一条消息」记到收件箱文件，看板上能看到消息是否被正确受理。"""
    MESSAGES_FILE.parent.mkdir(parents=True, exist_ok=True)
    sender = msg.get("sender", {}) or {}
    rec = {
        "ts": sa.now(),
        "message_id": mid,
        "chat_id": chat.get("chat_id", ""),
        "chat_name": chat.get("name", ""),
        "sender_id": sender.get("id", ""),
        "sender_name": sender.get("name", ""),
        "sent_at": msg.get("create_time", ""),
        "action": action,          # task | ignored | too_short | baseline | other_chat
        "task": task,
        "content": (body or msg.get("content", ""))[:400],
        "link": msg.get("message_app_link", ""),
        # 话题标识：看板上能看出这条消息属于哪个话题串
        "thread_id": msg.get("thread_id", "") or chat.get("thread_id", ""),
        "root_id": msg.get("root_id", "") or chat.get("root_id", ""),
    }
    with MESSAGES_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def backfill_ledger(state: dict) -> int:
    """用 seen 里已受理的消息补齐永久账本（防止历史消息被重放）。"""
    have = ledger_ids()
    n = 0
    for mid, meta in (state.get("seen") or {}).items():
        if mid in have:
            continue
        action = (meta or {}).get("action") or "seen"
        ledger_append(mid, (meta or {}).get("task", ""), "", action)
        n += 1
    return n


def discover_thread(tid: str, root_id: str, thread_index: dict) -> str:
    """按消息自带的 thread_id / root 关系解析它属于哪个话题串。

    话题里的追加消息在主列表里只给 root_id（父消息 id），不给 thread_id ——
    靠 thread_index（消息 id → 话题 id）反查。查不到返回空串。

    这条规则让降级轮询也能**兜底发现**未登记的话题：只要父消息在话题里被见过，
    追加消息就能被归回话题；话题本身则由事件路径直接登记进话题表。
    """
    if tid:
        return tid
    return (thread_index or {}).get(root_id or "", "")


def _assignment_stage(a: dict) -> str:
    """指派现在处在哪个阶段：dispatched（已派出去）/ completed（已完成）/ 空。"""
    st = a.get("status") or ""
    if st in ("assigned", "running"):
        return "dispatched"
    if st in ("done", "approved"):
        return "completed"
    return ""


def _stage_ts(a: dict, stage: str) -> str:
    return (a.get("dispatched_at") if stage == "dispatched" else a.get("completed_at")) or ""


def sync_reactions() -> int:
    """按指派状态补表情：派发 🎆 / 完成 ✅。

    表情挂在**触发这次工作的那条用户消息**上（assignment.origin_ref，
    缺了才回落到任务来源）—— 同一份工作的 👌/🎆/✅ 落在同一条消息上，
    形成一条完整的进度轨迹。

    去重靠 `reaction_seen`（指派 → 上次看到的阶段）：
      · 首次看到某个指派：只在它**刚发生**（时间戳在 10 分钟内）才补表情，
        否则认为是桥接重启前的旧活，不倒灌历史表情。
      · 阶段变了：补对应表情。
    """
    rc = reaction_config()
    if not rc.get("enabled"):
        return 0
    st = load_state()
    seen = st.get("reaction_seen")
    if not isinstance(seen, dict):
        seen = {}
    added = 0
    try:
        task_ids = [t for t in sa.list_task_ids()]
    except Exception:
        task_ids = []
    for tid in task_ids:
        try:
            tstate = sa.load_state(tid)
        except Exception:
            continue
        src = tstate.get("source_ref") or {}
        for a in tstate.get("assignments") or []:
            stage = _assignment_stage(a)
            if not stage:
                continue
            key = f"{tid}:{a.get('id')}"
            prev = seen.get(key)
            if prev == stage:
                continue
            first_seen = prev is None
            seen[key] = stage
            if first_seen:
                # 第一次看到它：只有「刚派发/刚完成」才补表情，否则是历史活
                ts = _stage_ts(a, stage)
                if not _is_recent(ts):
                    continue
            origin = a.get("origin_ref") or {}
            mid = origin.get("message_id") or src.get("message_id") or ""
            if not mid:
                continue
            if react(mid, rc[stage]):
                added += 1
    st["reaction_seen"] = seen
    save_state(st)
    return added


def reaction_log(limit: int = 20) -> list[dict]:
    """最近的表情记录（看板展示）。"""
    st = load_state()
    out = st.get("reaction_log")
    return out[-limit:] if isinstance(out, list) else []


def _is_recent(ts: str, window_seconds: int = 600) -> bool:
    """时间戳是否在最近 window_seconds 内（ISO 字符串）。"""
    if not ts:
        return False
    try:
        t = _dt.datetime.fromisoformat(ts)
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)
    return (_dt.datetime.now().astimezone() - t).total_seconds() <= window_seconds


def _bind_thread_to_task(rec: dict, thread_id: str) -> None:
    """把刚创建的话题 id 记到任务上（任务来源就是被回的那条消息时）。

    为什么要记：主频道消息进来时**没有**话题 id，话题是机器人回话时才被飞书创建。
    记下来之后，任务 → 话题的关联就落在 state 里，看板和排障都能顺着话题找任务。
    """
    tid = rec.get("task") or ""
    if not tid or not thread_id:
        return
    try:
        tstate = sa.load_state(tid)
    except Exception:
        return
    ref = tstate.get("source_ref")
    if not isinstance(ref, dict):
        return
    target = rec.get("reply_to") or ""
    if target and ref.get("message_id") and ref.get("message_id") != target:
        return
    if ref.get("thread_id") == thread_id:
        return
    ref["thread_id"] = thread_id
    tstate["source_ref"] = ref
    try:
        sa.save_state(tid, tstate)
        log(f"话题 {thread_id} 已绑定到任务 {tid}")
    except Exception as e:
        log(f"话题绑定任务失败 {tid}：{str(e)[:140]}")


def deliver_outbox() -> int:
    """outbox 投递器：把待发消息发回渠道。

    状态机：pending → succeeded | failed（重试耗尽 → dead_letter）

    `succeeded` 的口径很硬：**只有真实渠道客户端返回成功才写**，并同时记下
    投递凭据（渠道返回的新消息 id / 请求 id）。

    为什么收紧：以前只要 send_* 不抛异常就写 succeeded —— 一个把 send_reply
    换成「只往内存列表 append」的验证脚本，就能让生产 outbox 出现「已投递」，
    而用户侧什么都没收到（对账时 8 条 succeeded 在桥接日志里找不到投递行）。
    现在：桩返回 None / 非 ok 响应 → 不算成功（会重试，耗尽进 dead_letter）。

    两类「不投递」的处理**不再静默**：
      · 缺会话标识（chat_id / reply_to 都空）→ 先从任务 source_ref 继承；
        确实无处可投 → 进 dead_letter 并标 needs_manual，看板上可见、可人工重投。
      · 渠道报错 → 重试（回 pending），尝试次数耗尽才进 dead_letter。
    """
    import core
    sent = 0
    for rec in core.pending_outbox():
        try:
            if rec.get("channel") != "feishu":
                core.update_outbox(rec, status="cancelled", note="暂不支持的渠道")
                continue
            mid = rec.get("reply_to") or ""
            chat = rec.get("chat_id") or ""
            if not mid and not chat:
                # 记录本身没有会话标识 → 从任务来源继承（Decision 生成的 ack 最容易缺）
                sess = core.resolve_session(rec.get("task", ""))
                mid = sess.get("reply_to") or ""
                chat = sess.get("chat_id") or ""
                if mid or chat:
                    core.update_outbox(rec, chat_id=chat, reply_to=mid,
                                       session_source=sess.get("session_source", "task.source_ref"),
                                       note="已从任务来源继承会话标识")
                    log(f"outbox {rec['id']} 已从任务来源继承会话标识")
            if not mid and not chat:
                # 确实无处可投 → 不静默丢弃：进 dead_letter（看板可见，可人工重投）
                core.update_outbox(rec, status="dead_letter",
                                   attempts=rec.get("attempts", 0) + 1,
                                   needs_manual=True,
                                   note="没有可投递的会话（缺 chat_id/reply_to）"
                                        "——已进 dead_letter，需补齐会话后人工重投")
                log(f"outbox {rec['id']} 无可投递会话 → dead_letter（需人工重投）")
                continue
            # 受理回执改用表情表示 —— 这条文字不再发出去
            if rec.get("purpose") == "ack" and reaction_config().get("suppress_ack_text"):
                core.update_outbox(rec, status="suppressed", attempts=rec.get("attempts", 0) + 1,
                                   sent_at=sa.now(),
                                   note="受理回执改用表情表示（收到/分配只发表情，不发文字）")
                log(f"outbox {rec['id']} 是受理回执 → 改用表情，不发文字")
                sent += 1
                continue
            via = "reply" if mid else "send"
            resp = send_reply(mid, rec["text"]) if mid else send_text(chat, rec["text"])
            # ── 真渠道校验：桩 / 被替换的客户端一律不算投递成功 ──
            if not isinstance(resp, dict):
                raise RuntimeError(
                    f"渠道客户端没有返回可核验的响应（{type(resp).__name__}）——不写 succeeded")
            if not resp.get("ok", False):
                raise RuntimeError(f"渠道返回未成功：{str(resp)[:160]}")
            receipt = delivery_receipt(resp)
            # 回话在话题里 → 话题 id 回写到任务（主频道消息的任务也能带上话题了）
            thread_id = ""
            if via == "reply" and receipt["message_id"]:
                thread_id = resolve_thread_id(receipt["message_id"])
            core.update_outbox(rec, status="succeeded", attempts=rec.get("attempts", 0) + 1,
                               sent_at=sa.now(),
                               delivery={"channel": "feishu", "via": via,
                                         "message_id": receipt["message_id"],
                                         "request_id": receipt["request_id"],
                                         "thread_id": thread_id,
                                         "at": sa.now(),
                                         "verified": receipt["verified"]})
            if thread_id:
                _bind_thread_to_task(rec, thread_id)
            sent += 1
            cred = receipt["message_id"] or receipt["request_id"] or "无凭据"
            log(f"outbox 已投递 {rec['id']}（{rec['purpose']}）凭据={cred}")
            if rec.get("task"):
                sa.log_event(rec["task"], "outbox_sent", "outbox",
                             f"{rec['purpose']} 已发回渠道", outbox_id=rec["id"],
                             receipt=receipt["message_id"] or receipt["request_id"])
        except Exception as e:
            attempts = rec.get("attempts", 0) + 1
            status = "dead_letter" if attempts >= 3 else "pending"
            core.update_outbox(rec, status=status, attempts=attempts,
                               last_error=str(e)[:200],
                               next_run_at=sa.now(),
                               note=("重试耗尽，需人工处理" if status == "dead_letter"
                                     else "投递失败，将重试"))
            log(f"outbox 投递失败 {rec['id']}：{e}（{status}）")
    return sent


def poll_once(cfg: dict, state: dict, dry_run: bool = False, mode: str = "pull") -> list[dict]:
    trig = cfg.get("trigger", {})
    baseline = bool(cfg.get("bootstrap", {}).get("baseline_existing_on_first_run", True))
    first_run = not state.get("bootstrapped")
    bot_names = [cfg.get("bot", {}).get("name", "飞书助手")] + (trig.get("mention_names") or [])
    allowed = {s["open_id"] for s in cfg.get("senders", [])}
    allow_any_sender = bool(cfg.get("allow_any_sender", False))
    size = cfg.get("poll", {}).get("page_size", 50)
    accepted = []
    n = backfill_ledger(state)
    if n:
        log(f"永久账本补齐 {n} 条")
    handled = ledger_ids()

    # ── 启动时间闸门 ──
    # 只接受**桥接进程启动之后**收到的消息。早于启动时间的（无论来自重点群还是
    # 新发现的群）一律视为历史，标记已读不处理 —— 否则动态发现一个群就会把它
    # 几个月前的消息全灌给 router。
    # grace_seconds 给重启留个口子：桥接重启间隔内的消息不至于丢。
    started_at = state.get("started_at") or ""
    grace = int(cfg.get("startup_grace_seconds", 600))
    cutoff = ""
    if started_at:
        try:
            st_dt = _dt.datetime.fromisoformat(started_at).replace(tzinfo=None)
            cutoff = (st_dt - _dt.timedelta(seconds=grace)).isoformat(timespec="seconds")
        except ValueError:
            cutoff = ""

    # 要轮询的群 = 配置里的重点群 + 动态发现的群
    # 已基线化的群（动态发现的群首次见到时做基线）
    baselined = set(state.get("baselined_chats") or [])
    chat_list = list(cfg.get("chats", []))
    if cfg.get("chat_discovery", {}).get("enabled"):
        known = {c["chat_id"] for c in chat_list}
        for c in discover_chats(cfg):
            if c["chat_id"] not in known:
                chat_list.append(c)
    log(f"本轮轮询 {len(chat_list)} 个群（重点 {len(cfg.get('chats', []))} + 发现 "
        f"{len(chat_list) - len(cfg.get('chats', []))}）")

    # ── 话题串：用户在话题里的追加指令 ──
    # 话题消息不在主频道列表里，必须单独轮询。话题里不用再 @ 机器人 ——
    # 机器人已经在话题里参与了。
    threads = state.setdefault("threads", {})
    # 消息 id → 话题 id 的映射：用来按 reply/root 关系反查一条消息属于哪个话题串
    # （话题里的追加消息在主列表里只带 root_id，不带 thread_id）。
    thread_index = state.setdefault("thread_index", {})
    # 每轮最多轮询几个话题（轮转抽样）—— 话题一多，全量拉会拖垮轮询周期
    max_threads = int(cfg.get("poll", {}).get("max_threads_per_round", 4))
    tids = list(threads.keys())
    if tids and max_threads > 0:
        start = int(state.get("thread_cursor", 0)) % len(tids)
        picked = [tids[(start + i) % len(tids)] for i in range(min(max_threads, len(tids)))]
        state["thread_cursor"] = (start + len(picked)) % max(1, len(tids))
    else:
        picked = tids
    for tid in picked:
        tinfo = threads.get(tid)
        if not tinfo:
            continue
        try:
            tmsgs = fetch_thread_messages(tid, size)
        except Exception as e:
            log(f"拉取话题 {tid} 失败: {e}")
            continue
        for m in tmsgs:
            mid = m.get("message_id")
            if not mid:
                log_ignored("", (tinfo.get("chat") or {}).get("chat_id") or tid,
                            "话题消息缺 message_id（形态异常）")
                continue
            # 记住「这条消息属于哪个话题串」—— 降级轮询时用来按 root/reply 关系
            # 把消息归回话题（否则新话题只能靠事件路径发现）。
            thread_index[mid] = tid
            m.setdefault("thread_id", tid)
            sender = m.get("sender", {}) or {}
            chat_of_thread = tinfo.get("chat") or {}
            # 看到机器人自己在这个话题里说过话 → 话题已被机器人接手，
            # 之后用户在这个话题里追加的消息不用再 @ 机器人。
            if sender.get("sender_type") != "user":
                tinfo["bot_engaged"] = True
                continue
            if mid in state.get("seen", {}) or mid in handled:
                state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "thread-dedup"}
                continue
            if cutoff and norm_ts(m.get("create_time")) < cutoff:
                state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "stale"}
                continue
            if allowed and not allow_any_sender and sender.get("id") not in allowed:
                log_ignored(mid, chat_of_thread.get("chat_id") or tid,
                            "发送者不在白名单（话题消息）")
                continue
            text = (m.get("content") or "").strip()
            if not text:
                log_ignored(mid, chat_of_thread.get("chat_id") or tid,
                            "正文为空（形态异常）")
                continue
            # 话题里默认就是指令（机器人已在此话题中）；若配置要求 @ 则仍需 @。
            need_mention = trig.get("group_requires_mention", True) and not tinfo.get("bot_engaged")
            body = text
            if need_mention:
                ok2, body = is_trigger(text, "group", trig, bot_names,
                                       allow_prefix=not chat_of_thread.get("discovered"))
                if not ok2:
                    state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "ignored"}
                    ledger_append(mid, "", text, "ignored")
                    record_message(mid, chat_of_thread, m, "ignored", body=text)
                    log_ignored(mid, chat_of_thread.get("chat_id") or tid,
                                "话题消息未明确指向机器人（无事由 @ / 无指令前缀）", text)
                    continue
            else:
                body = strip_mentions(text, bot_names)[0] or text
            if len(body) < trig.get("min_length", 2):
                log_ignored(mid, chat_of_thread.get("chat_id") or tid,
                            f"正文过短（<{trig.get('min_length', 2)} 字）", body)
                continue
            if dry_run:
                accepted.append({"thread": tid, "message_id": mid, "body": body})
                record_message(mid, tinfo.get("chat") or {}, m, "dry-run", body=body)
                continue
            # 先记账再投（账本是 router 的来源凭据，先投后记会被判成非桥接产出）
            record_message(mid, tinfo.get("chat") or {}, m, "delivered", body=body)
            ledger_append(mid, "", body, "delivered")
            try:
                ev = deliver_user_message(cfg, tinfo.get("chat") or {}, m, body)
            except Exception as e:
                log(f"投递 router 失败 (话题 {tid}): {e}")
                continue
            state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "delivered", "trigger": ev.get("id")}
            accepted.append({"thread": tid, "message_id": mid, "trigger": ev.get("id"), "body": body})
            log(f"话题消息已投 router（trigger {ev.get('id')}）：{body[:60]}")


    for chat in chat_list:
        try:
            msgs = fetch_messages(chat["chat_id"], size)
        except Exception as e:
            log(f"拉取会话 {chat.get('name')} 失败: {e}")
            continue
        # 新发现的群：**先基线化** —— 它已有的历史消息一律标记已读。
        # 否则发现的那一刻起，这个群的全部历史（可能几个月前）都会被当成新指令
        # 灌给 router，一次几百条。
        if chat.get("discovered") and chat["chat_id"] not in baselined:
            for m in msgs:
                mid = m.get("message_id")
                if mid:
                    state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "baseline"}
                    ledger_append(mid, "", "", "baseline")
            baselined.add(chat["chat_id"])
            log(f"新群「{chat.get('name')}」已基线化：{len(msgs)} 条历史消息标记已读，"
                f"之后只处理新消息")
            continue
        for m in msgs:
            mid = m.get("message_id")
            if not mid:
                log_ignored("", chat.get("chat_id") or "", "会话消息缺 message_id（形态异常）")
                continue
            # 记住这个话题串 —— 之后要单独轮询它。
            # 必须在 seen 检查**之前**登记：根消息早已处理过（seen），
            # 若在这里 continue，话题就永远不会被轮询。
            tid = m.get("thread_id") or ""
            if not tid:
                # 话题里的追加消息：主列表里只给 root_id（父消息 id），
                # 用已建立的映射反查它属于哪个话题串（按 reply/root 关系动态登记）
                tid = discover_thread(tid, m.get("root_id") or "", thread_index)
            if tid and tid != mid:
                threads.setdefault(tid, {"chat": chat, "bot_engaged": False})
                if m.get("root_id"):
                    thread_index[m["root_id"]] = tid
            m2 = dict(m)
            m2["thread_id"] = m.get("thread_id") or tid
            m2["root_id"] = m.get("root_id") or ""
            if mid in state.get("seen", {}):
                continue
            # 时间闸门：启动前的老消息 → 标记已读，不处理
            if cutoff and norm_ts(m.get("create_time")) < cutoff:
                state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "stale"}
                continue
            if mid in handled:
                # 账本里已处理过 —— 永久跳过，绝不重放
                state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "ledger-dedup"}
                continue
            sender = m.get("sender", {}) or {}
            if trig.get("ignore_non_user_sender", True) and sender.get("sender_type") != "user":
                log_ignored(mid, chat.get("chat_id") or "",
                            f"非用户发送者（sender_type={sender.get('sender_type')}）")
                continue
            if allowed and not allow_any_sender and sender.get("id") not in allowed:
                log_ignored(mid, chat.get("chat_id") or "", "发送者不在白名单")
                continue
            text = (m.get("content") or "").strip()
            if not text:
                log_ignored(mid, chat.get("chat_id") or "", "正文为空（形态异常）")
                continue
            ok, body = is_trigger(text, chat.get("mode", "group"), trig, bot_names,
                                  allow_prefix=not chat.get("discovered"))
            if first_run and baseline:
                # 首次运行只建立基线：历史消息标记已读，不当成新指令
                state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "baseline"}
                ledger_append(mid, "", body, "baseline")
                record_message(mid, chat, m2, "baseline", body=body)
                continue
            if not ok:
                state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "ignored"}
                ledger_append(mid, "", text, "ignored")
                record_message(mid, chat, m2, "ignored", body=text)
                log_ignored(mid, chat.get("chat_id") or "",
                            "群消息未构成指令（未 @ 机器人 / 无指令前缀）", text)
                continue
            if len(body) < trig.get("min_length", 2):
                state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "too_short"}
                ledger_append(mid, "", body, "too_short")
                record_message(mid, chat, m2, "too_short", body=body)
                log_ignored(mid, chat.get("chat_id") or "",
                            f"正文过短（<{trig.get('min_length', 2)} 字）", body)
                continue
            if dry_run:
                accepted.append({"chat": chat.get("name"), "message_id": mid, "body": body})
                record_message(mid, chat, m2, "dry-run", body=body)
                continue
            # 先记账再投（账本是 router 的来源凭据，先投后记会被判成非桥接产出）
            record_message(mid, chat, m2, "delivered", body=body)
            ledger_append(mid, "", body, "delivered")
            try:
                ev = deliver_user_message(cfg, chat, m2, body)
            except Exception as e:
                log(f"投递 router 失败 (msg {mid}): {e}")
                continue
            state.setdefault("seen", {})[mid] = {"ts": sa.now(), "action": "delivered", "trigger": ev.get("id")}
            state["created_tasks"] = state.get("created_tasks", 0) + 1
            accepted.append({"chat": chat.get("name"), "message_id": mid,
                             "trigger": ev.get("id"), "body": body})
            log(f"飞书消息已投 router（trigger {ev.get('id')}）：{body[:60]}")

            ack = cfg.get("ack", {})
            if ack.get("on_receive", True):
                # 受理回执只说「收到」—— 任务号此刻还不存在：
                # 任务由 router 判断后才产生（可能沿用旧任务）。真实任务号由 router 的
                # outbox 回复给出。
                try:
                    send_reply(mid, ack.get("text", "已收到，正在安排处理").format(
                        title=body[:60]))
                except Exception as e:
                    log(f"回执发送失败 (msg {mid}): {e}")
    state["baselined_chats"] = sorted(baselined)
    if first_run and baseline:
        state["bootstrapped"] = True
        log(f"基线建立完成：已把 {len(state['seen'])} 条历史消息标记为已读（不建任务）")
    state["last_poll"] = sa.now()
    state["mode"] = mode
    return accepted


def cmd_follow(a: argparse.Namespace) -> None:
    """守护进程主循环：**官方事件订阅（长连接）为主，轮询为兜底**。

    官方推荐方式：
      lark-cli event consume im.message.receive_v1 --as bot
      底层是 feishu-websocket 长连接（open.feishu.cn「使用长连接接收事件」）：
      企业自建应用推荐，无需公网 IP/域名、无需内网穿透，SDK 内置鉴权与加密。

    轮询是**真兜底**：长连接连不上（总线冲突/网络）时降级为主；
    长连接连上了但**长时间收不到事件**（静默半开、不产生 EOF）时，
    也会周期性跑一轮轮询把可能漏掉的消息补回来 —— 之前只写了日志文案，
    并没有真的执行（poll_tick 声明后未使用）。
    """
    cfg = load_config()
    state = load_state()
    mode = "event"
    write_pid_file()

    # ── 启动长连接消费者 ──
    cmd = ["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    assert proc.stdin and proc.stdout
    proc.stdin.write(" ")          # 防 stdin EOF 触发优雅退出
    proc.stdin.flush()

    # 等待 ready 标记（官方约定：父进程应阻塞读 stderr 直到 ready）
    ready = False
    t0 = time.time()
    while time.time() - t0 < 45:
        line = proc.stderr.readline()
        if not line:
            break
        if "[event] ready" in line:
            ready = True
            log("事件长连接就绪（feishu-websocket，官方推荐方式）")
            break
        if "another event bus" in line or "already connected" in line:
            log(f"事件总线被占用：{line.strip()[:200]}")
            break
    if not ready:
        log("长连接未就绪 → 降级为轮询模式")
        try:
            proc.terminate()
        except Exception:
            pass
        mode = "poll"
        cmd_pull(argparse.Namespace(follow=True, dry_run=False))
        return

    # 后台线程：持续投递 outbox（回复/回执）
    def deliver_loop():
        while True:
            try:
                deliver_outbox()
            except Exception as e:
                log(f"outbox 投递异常: {e}")
            try:
                sync_reactions()
            except Exception as e:
                log(f"表情同步异常: {e}")
            time.sleep(5)

    threading.Thread(target=deliver_loop, daemon=True).start()

    # ── 进入事件模式：把运行态写准 ──
    # 之前事件模式不写 mode/last_poll/started_at，看板上「最近拉取时间」
    # 一直显示旧实例的旧值。这里写实际模式 + 本次实例的启动时间。
    with state_lock(wait_seconds=5) as got:
        if got:
            st = load_state()
            st["mode"] = "event"
            st["started_at"] = sa.now()
            st["last_event_mode_at"] = sa.now()
            st["bootstrapped"] = True      # 事件模式不做历史基线扫描
            save_state(st)

    poll_cfg = cfg.get("poll", {}) or {}
    fallback_enabled = bool(poll_cfg.get("fallback_enabled", True))
    fallback_seconds = int(poll_cfg.get("fallback_seconds", 120))
    log(f"进入事件模式（官方长连接）：消息实时到达；"
        f"连续 {fallback_seconds}s 没有事件则跑一轮兜底轮询"
        f"（{'开' if fallback_enabled else '关'}）")

    # 事件流读取线程：把行塞进队列，主循环才能在「没事件」时及时醒过来做兜底轮询。
    q: "queue.Queue" = queue.Queue()

    def reader():
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        q.put(None)     # EOF 哨兵

    threading.Thread(target=reader, daemon=True).start()

    def fallback_poll() -> None:
        """长连接长时间没消息 → 拉一轮，把可能漏掉的消息补回来。

        去重靠两条：state['seen'] 与永久账本 handled.jsonl ——
        同一条消息不会被两条路径重复投递。
        """
        with state_lock(wait_seconds=0) as got:
            if not got:
                log("兜底轮询：拿不到状态锁，本轮跳过")
                return
            st = load_state()
            try:
                accepted = poll_once(cfg, st, mode="event")
            except Exception as e:
                log(f"兜底轮询异常: {e}")
                return
            save_state(st)
        if accepted:
            log(f"兜底轮询补到 {len(accepted)} 条消息")

    last_event_at = time.time()
    while True:
        try:
            line = q.get(timeout=1.0)
        except queue.Empty:
            line = ""
        if line is None:
            # 2) 事件流结束 → 降级
            log("事件流结束 → 降级为轮询模式")
            try:
                proc.terminate()
            except Exception:
                pass
            cmd_pull(argparse.Namespace(follow=True, dry_run=False))
            return
        if line:
            last_event_at = time.time()
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                # 等锁最多 2.5s：官方要求长连接 3 秒内处理完，抢不到锁就丢
                # 会造成消息丢失，宁可等一下
                with state_lock(wait_seconds=2.5) as got:
                    if not got:
                        log("拿不到状态锁，事件延迟处理")
                        continue
                    st = load_state()
                    act = process_event(cfg, st, ev)
                    st["mode"] = "event"
                    st["last_message_at"] = sa.now()
                    st["last_event_at"] = sa.now()
                    save_state(st)
            except Exception as e:
                log(f"事件处理异常: {e}")
            continue
        # 3) 队列空：判断是否该跑兜底轮询
        if fallback_enabled and (time.time() - last_event_at) >= fallback_seconds:
            last_event_at = time.time()
            log("长连接长时间无事件 → 执行一轮兜底轮询")
            try:
                fallback_poll()
            except Exception as e:
                log(f"兜底轮询失败: {e}")


def cmd_pull(a: argparse.Namespace) -> None:
    cfg = load_config()
    state = load_state()
    if a.follow:
        write_pid_file()
        # 记录启动时间 —— 启动之前的老消息一律不处理（只接受启动后收到的）
        grace = int(cfg.get("startup_grace_seconds", 600))
        with state_lock() as got:
            if got:
                st = load_state()
                st["started_at"] = sa.now()
                st["baselined_chats"] = []
                save_state(st)
        log(f"轮询启动，间隔 {cfg.get('poll', {}).get('interval_seconds', 20)}s，Ctrl+C 退出")
        log(f"启动时间闸门：只处理 {sa.now()} 之后（±{grace}s 宽限）收到的消息，"
            f"更早的一律标记已读")
        try:
            while True:
                try:
                    with state_lock() as got:
                        if not got:
                            time.sleep(cfg.get("poll", {}).get("interval_seconds", 20))
                            continue
                        state = load_state()      # 每轮重读，避免和别的进程互相覆盖
                        poll_once(cfg, state)
                        save_state(state)
                        deliver_outbox()          # 顺带投递待发消息
                        sync_reactions()          # 顺带补表情（派发/完成）
                except Exception as e:
                    log(f"轮询异常: {e}")
                time.sleep(cfg.get("poll", {}).get("interval_seconds", 20))
        except KeyboardInterrupt:
            log("轮询停止")
            save_state(state)
        return
    with state_lock() as got:
        if not got:
            print(json.dumps([], ensure_ascii=False))
            return
        state = load_state()
        res = poll_once(cfg, state, dry_run=a.dry_run)
        save_state(state)
    print(json.dumps(res, ensure_ascii=False, indent=2))


# ------------------------------------------------------------------ 事件模式


def cmd_reply(a: argparse.Namespace) -> None:
    state = sa.load_state(a.task)
    ref = state.get("source_ref", {}) or {}
    mid = ref.get("message_id")
    if not mid:
        die_msg = f"任务 {a.task} 没有飞书来源（source={state.get('source')}），无法回执"
        log(die_msg)
        raise SystemExit(1)
    send_reply(mid, a.text)
    sa.log_event(a.task, "feishu_replied", "super-agent", "结果已回传飞书", message_id=mid)
    log(f"已回执飞书（{mid}）→ {a.task}")
    print(json.dumps({"ok": True, "task": a.task, "message_id": mid}, ensure_ascii=False))


def cmd_reset_ledger(a: argparse.Namespace) -> None:
    if LEDGER_FILE.exists():
        LEDGER_FILE.unlink()
    print("永久账本已清空（历史消息会被重新受理一次）")


def cmd_status(a: argparse.Namespace) -> None:
    cfg = load_config()
    st = load_state()
    seen = st.get("seen", {})
    # pid 字段 = pid 文件里的守护进程 pid（不是本次查询进程自己的 pid）
    pidi = daemon_pid_info()
    tasks = []
    for p in sorted(sa.TASKS_DIR.glob("T*")):
        sp = p / sa.STATE_FILE
        if not sp.exists():
            continue
        d = json.loads(sp.read_text(encoding="utf-8"))
        if d.get("source") == "feishu":
            tasks.append({"task": d["task_id"], "status": d["status"],
                          "message_id": (d.get("source_ref") or {}).get("message_id"),
                          "title": d["title"]})
    print(json.dumps({
        "enabled": cfg.get("enabled"),
        "mode": st.get("mode") or cfg.get("mode"),
        "runtime_mode": st.get("mode"),
        "pid": pidi["pid"],
        "pid_source": pidi["pid_source"],
        "pid_file": pidi["pid_file"],
        "daemon_alive": pidi["daemon_alive"],
        "pid_matches_bridge": pidi["pid_matches_bridge"],
        "running": pidi["running"],
        "daemon_cmd": pidi["daemon_cmd"],
        "status_pid": pidi["status_pid"],
        "bot": cfg.get("bot"),
        "chats": [{"name": c.get("name"), "chat_id": c.get("chat_id"), "mode": c.get("mode")}
                  for c in cfg.get("chats", [])],
        "senders": [s.get("name") for s in cfg.get("senders", [])],
        "seen_count": len(seen),
        "sandbox_mode": sa.SANDBOX_MODE,
        "state_root": str(sa.SA_ROOT),
        "ledger_count": len(ledger_ids()),
        "created_tasks": st.get("created_tasks", 0),
        "started_at": st.get("started_at"),
        "last_poll": st.get("last_poll"),
        "last_message_at": st.get("last_message_at"),
        "last_event_at": st.get("last_event_at"),
        "feishu_tasks": tasks,
    }, ensure_ascii=False, indent=2))


def sandbox_banner() -> None:
    """沙箱自检：状态根一旦被挪出代码目录，就在桥接日志 + stderr 里显式标出来。

    「以为在演练、实际在写生产」是上一次凭空造出任务的直接原因。
    不走 stdout 是故意的：status / pull 等命令的 stdout 是给脚本解析的 JSON。
    """
    if not sa.SANDBOX_MODE:
        return
    lines = [
        f"沙箱模式：状态根 = {sa.SA_ROOT}（不写生产状态）",
        f"  router 收件箱 = {sa.ROUTER_INBOX}",
        f"  outbox 目录   = {sa.OUTBOX_DIR}",
        f"  永久账本     = {LEDGER_FILE}",
    ]
    stamp = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(f"[{stamp}] {line}\n")
    except OSError:
        pass
    for line in lines:
        print(line, file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(prog="lark_bridge.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pull")
    p.add_argument("--once", action="store_true")
    p.add_argument("--follow", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("follow", help="守护进程：官方事件长连接为主，轮询兜底")
    p.set_defaults(func=cmd_follow)


    p = sub.add_parser("reply")
    p.add_argument("--task", required=True)
    p.add_argument("--text", required=True)
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("reset-ledger")
    p.set_defaults(func=cmd_reset_ledger)

    a = ap.parse_args()
    sandbox_banner()
    a.func(a)


if __name__ == "__main__":
    main()
