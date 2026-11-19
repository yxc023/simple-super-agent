#!/usr/bin/env python3
"""企业微信智能机器人 → super-agent 桥接。

把企微消息转成 super-agent 协议里的 Task，让主 agent 把它们当工作指令；
再把决策产出的回话发回企微。

    python3 super-agent/wecom_bridge.py follow     # 常驻长连接（守护进程主循环）
    python3 super-agent/wecom_bridge.py probe       # 只测能不能连上并订阅成功
    python3 super-agent/wecom_bridge.py status      # 看连接/状态

接入方式：**长连接（WebSocket）**，企业微信「智能机器人」API 模式。
  · 端点：wss://openws.work.weixin.qq.com
  · 订阅：aibot_subscribe（body.bot_id + body.secret）
  · 收消息：aibot_msg_callback（body.text.content / body.from.userid / body.chatid）
  · 收事件：aibot_event_callback（enter_chat / template_card_event / disconnected_event）
  · 回复：aibot_respond_msg（**透传回调的 req_id**）→ 失败或没有 req_id 时退回
          aibot_send_msg（body.chatid + body.chat_type）
  · 心跳：ping（建议 30s 一次，断了要重连）

为什么不用「接收消息回调地址」模式：那种模式要求一个**公网可访问的 HTTPS 地址**，
本机没有。长连接模式不需要公网 IP、不需要加解密，只要 BotID + Secret。

**一个机器人同时只能有一条长连接**（新连接会把旧连接踢掉）——
所以回复必须走**同一条**长连接发出去，不能另开连接（另开会把守护进程踢下线）。

与飞书侧同构：消息 → 记账 → 投 router（sa.notify）→ 决策产出 outbox →
投递器按渠道取走并发回。**outbox 的 channel 字段决定谁投**：
飞书投递器只取 channel=feishu 的，企微投递器只取 channel=wecom 的。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import re
import sys
import threading
import time
import uuid
import datetime as _dt

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import sa  # noqa: E402
import core  # noqa: E402

CONFIG_FILE = ROOT / "wecom_config.json"
WSS_ENDPOINT = "wss://openws.work.weixin.qq.com"

# ── 状态文件全部从 sa.SA_ROOT 派生（沙箱演练只需设 SA_ROOT）──
def _state_path(env_name: str, default_rel: str) -> pathlib.Path:
    p = sa.env_path(env_name)
    return p if p is not None else sa.SA_ROOT / default_rel


STATE_FILE = _state_path("SA_WECOM_STATE_FILE", "wecom_state.json")
LOG_FILE = sa.SA_ROOT / "logs" / "wecom-bridge.log"
MESSAGES_FILE = sa.INBOX_DIR / "wecom-messages.jsonl"
LEDGER_FILE = sa.LEDGER_FILE
PID_FILE = (pathlib.Path("/tmp/super-agent-wecom-bridge.pid")
            if not sa.SANDBOX_MODE else sa.SA_ROOT / ".wecom-bridge.pid")

PING_INTERVAL = 30          # 心跳间隔（企微建议 30s）
RECONNECT_BACKOFF = (2, 5, 10, 20, 30)
MAX_TEXT_BYTES = 18000      # 企微 markdown 正文上限 20480 字节，留余量
BATCH_HOLD_MAX_SECONDS = 1800  # 同批结果最多压多久

_LOCK = threading.RLock()


# ------------------------------------------------------------------ 配置/状态


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"缺少配置文件：{CONFIG_FILE}", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{_dt.datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}\n"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line)
    print(msg, flush=True)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen": {}, "chats": {}}
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"seen": {}, "chats": {}}
    st.setdefault("seen", {})
    st.setdefault("chats", {})
    return st


def save_state(st: dict) -> None:
    """状态文件原子写（临时文件名带 pid+uuid，避免并发写互相覆盖）。"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(f".json.tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def ledger_ids() -> set[str]:
    """永久账本：已处理过的 message_id（append-only，防重放）。"""
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


def record_message(mid: str, chat: dict, msg: dict, action: str,
                   task: str = "", body: str = "") -> None:
    """把「收到的一条消息」记到收件箱文件，看板上能看到消息是否被正确受理。"""
    MESSAGES_FILE.parent.mkdir(parents=True, exist_ok=True)
    sender = msg.get("sender", {}) or {}
    rec = {
        "ts": sa.now(),
        "platform": "wecom",
        "message_id": mid,
        "chat_id": chat.get("chat_id", ""),
        "chat_name": chat.get("name", ""),
        "chat_type": chat.get("mode", ""),
        "sender_id": sender.get("id", ""),
        "sender_name": sender.get("name", ""),
        "sent_at": msg.get("create_time", ""),
        "action": action,
        "task": task,
        "content": (body or msg.get("content", ""))[:400],
    }
    with MESSAGES_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def log_ignored(mid: str, chat_id: str, reason: str, text: str = "") -> None:
    mid_s = (mid or "").strip()
    shown = (mid_s[:8] + "…") if mid_s else "<?>"
    tail = f" 摘要={text[:30]!r}" if text else ""
    log(f"不受理：消息 {shown} 会话={(chat_id or '')[:16]} 原因={reason}{tail}")


# ------------------------------------------------------------------ 正文处理


def strip_bot_mention(text: str, bot_name: str) -> tuple[str, bool]:
    """剥掉正文开头的 @机器人。

    返回 (剥完的正文, 是不是在叫机器人)。

    企微回调里**没有**「被 @ 了没有」的结构化字段，只能看正文开头的 @：
      · 配了机器人名字 → 精确匹配（名字可带或不带 @ 前缀）；
      · 没配名字 → 兜底剥掉开头任意一个 @token（群里的 @ 一定指向某个机器人）。
    """
    raw = (text or "").strip()
    if not raw:
        return "", False
    names = []
    if bot_name:
        n = bot_name.strip()
        names += [n, n.lstrip("@").strip()]
    for n in names:
        if n and raw.startswith(n):
            return raw[len(n):].strip(), True
    m = re.match(r"^@\S+\s*", raw)
    if m:
        return raw[m.end():].strip(), True
    return raw, False


# ------------------------------------------------------------------ 长连接


class WeComConn:
    """企业微信智能机器人长连接客户端（同步 WebSocket）。

    收发解耦：接收线程独占 socket 的读；发送线程通过 `request()` 写帧并等应答
    （应答按 headers.req_id 配对，由接收线程分发）。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        bot = cfg.get("bot", {}) or {}
        self.bot_id = bot.get("bot_id", "")
        self.secret = bot.get("secret", "")
        self.endpoint = (cfg.get("wss", {}) or {}).get("endpoint", WSS_ENDPOINT)
        self.ws = None
        self.subscribed = False
        self.connected_at = ""
        self.last_error = ""
        self._pending: dict[str, dict] = {}
        self._events: dict[str, threading.Event] = {}
        self._send_lock = threading.Lock()
        self._stop = threading.Event()

    # ── 帧收发 ──
    def _write(self, payload: dict) -> None:
        with self._send_lock:
            self.ws.send(json.dumps(payload, ensure_ascii=False))

    def request(self, cmd: str, body: dict | None = None, timeout: float = 15) -> dict:
        """发一条命令并等它的应答（按 req_id 配对）。"""
        req_id = uuid.uuid4().hex
        payload = {"cmd": cmd, "headers": {"req_id": req_id}}
        if body is not None:
            payload["body"] = body
        ev = threading.Event()
        with self._send_lock:
            self._events[req_id] = ev
            self._pending[req_id] = {}
            try:
                self.ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception:
                self._events.pop(req_id, None)
                self._pending.pop(req_id, None)
                raise
        if not ev.wait(timeout):
            self._events.pop(req_id, None)
            self._pending.pop(req_id, None)
            raise TimeoutError(f"{cmd} 等待应答超时")
        result = self._pending.pop(req_id, {})
        self._events.pop(req_id, None)
        return result or {}

    def subscribe(self) -> None:
        resp = self.request("aibot_subscribe",
                            {"bot_id": self.bot_id, "secret": self.secret})
        if resp.get("errcode", -1) != 0:
            raise RuntimeError(f"订阅失败：errcode={resp.get('errcode')} "
                               f"errmsg={resp.get('errmsg')}")
        self.subscribed = True

    def ping(self) -> None:
        self.request("ping", None, timeout=10)

    # ── 帧处理（接收线程）──
    def dispatch(self, msg: dict) -> None:
        cmd = msg.get("cmd") or ""
        req_id = (msg.get("headers") or {}).get("req_id", "")
        # 先看是不是某条命令的应答 → 唤醒等待它的那条命令
        with self._send_lock:
            ev = self._events.get(req_id)
        if ev is not None:
            self._pending[req_id] = msg
            ev.set()
            return
        if cmd in ("aibot_msg_callback", "aibot_event_callback"):
            # **不能阻塞接收线程**：处理过程可能要发帧并等应答，
            # 而应答只能由接收线程分发 —— 在接收线程里等 = 死等。
            threading.Thread(target=self._handle_frame, args=(msg,),
                             daemon=True).start()
        elif cmd in ("aibot_subscribe", "ping", "pong"):
            pass
        else:
            log(f"收到未识别帧：cmd={cmd}")

    def _handle_frame(self, msg: dict) -> None:
        cmd = msg.get("cmd") or ""
        req_id = (msg.get("headers") or {}).get("req_id", "")
        try:
            if cmd == "aibot_msg_callback":
                handle_message(msg.get("body") or {}, req_id=req_id)
            elif cmd == "aibot_event_callback":
                handle_event(msg.get("body") or {}, req_id=req_id)
        except Exception as e:
            log(f"处理企微帧出错：{type(e).__name__} {str(e)[:160]}")

    # ── 主循环 ──
    def start_reader(self, ws) -> threading.Thread:
        """启动读线程（**必须先有读线程才能等应答** —— 应答由读线程分发）。"""
        self._stop = threading.Event()

        def _reader() -> None:
            while not self._stop.is_set():
                try:
                    raw = ws.recv(timeout=1)
                except TimeoutError:
                    raw = None
                except Exception as e:
                    if not self._stop.is_set():
                        self.last_error = str(e)[:200]
                        log(f"企微长连接读取失败：{type(e).__name__} {str(e)[:120]}")
                    self._stop.set()
                    return
                if raw is not None:
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        log(f"收到非 JSON 帧，已忽略（{str(raw)[:60]}）")
                        msg = None
                    if isinstance(msg, dict):
                        self.dispatch(msg)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        # 心跳单独一个线程：发 ping 并等应答（应答由读线程分发，不能在读线程里等）
        threading.Thread(target=self._heartbeat, daemon=True).start()
        return t

    def _heartbeat(self) -> None:
        while not self._stop.is_set():
            time.sleep(PING_INTERVAL)
            if self._stop.is_set():
                return
            try:
                self.ping()
            except Exception as e:
                log(f"心跳失败：{str(e)[:120]}")
                self._stop.set()
                return

    def run_forever(self) -> None:
        from websockets.sync.client import connect
        attempt = 0
        while True:
            try:
                # ping_interval=None：**关掉库自带的 WebSocket 协议级心跳**。
                # 企微的长连接网关不实现 pong —— 库发一次协议 ping，
                # 网关就会把后续帧解析错，报 1002「incorrect masking」把连接掐掉。
                # 改用**应用层心跳**：每 30s 发一条 {"cmd":"ping"} 命令帧，
                # 网关会正常回 {"errcode":0}（实测通）。
                with connect(self.endpoint, open_timeout=15,
                             max_size=4 * 1024 * 1024, ping_interval=None) as ws:
                    self.ws = ws
                    self.start_reader(ws)
                    self.subscribe()
                    self.connected_at = sa.now()
                    attempt = 0
                    log(f"企微长连接已就绪（订阅成功）：bot_id={self.bot_id[:8]}…")
                    while not self._stop.is_set():
                        time.sleep(0.5)
                    raise ConnectionError("连接已断开")
            except Exception as e:
                self.subscribed = False
                with contextlib.suppress(Exception):
                    self._stop.set()
                self.last_error = str(e)[:200]
                wait = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                attempt += 1
                log(f"企微长连接断开：{type(e).__name__} {str(e)[:160]} → {wait}s 后重连")
                time.sleep(wait)


# 守护进程里的那条长连接（全局共享：回复必须走它，不能另开连接）
ACTIVE_CONN: WeComConn | None = None


# ------------------------------------------------------------------ 收消息


def handle_event(body: dict, req_id: str = "") -> None:
    ev = (body.get("event") or {})
    etype = ev.get("eventtype") or ""
    if etype == "enter_chat":
        cfg = load_config()
        welcome = (cfg.get("welcome") or "").strip()
        if welcome:
            try:
                send_welcome(req_id, welcome)
                log("已回复欢迎语")
            except Exception as e:
                log(f"欢迎语发送失败：{str(e)[:120]}")
    elif etype == "disconnected_event":
        log("收到连接断开事件：说明有新的连接抢占了本机器人的长连接")
    elif etype:
        log(f"收到事件：{etype}")


def handle_message(body: dict, req_id: str = "") -> None:
    """一条企微用户消息 → 记账 → 投 router。"""
    cfg = load_config()
    trig = cfg.get("trigger", {}) or {}
    bot_name = (cfg.get("bot", {}) or {}).get("name", "")

    mid = body.get("msgid") or ""
    if not mid:
        log_ignored("", "", "回调缺 msgid")
        return
    chat_type = body.get("chattype") or ""
    sender_id = ((body.get("from") or {}).get("userid")) or ""
    chat_id = body.get("chatid") or (sender_id if chat_type == "single" else "")
    if body.get("msgtype") == "text":
        raw_text = (body.get("text") or {}).get("content") or ""
    elif body.get("msgtype"):
        raw_text = f"[{body.get('msgtype')} 消息]"
    else:
        raw_text = ""

    with _LOCK:
        state = load_state()
        seen = state.setdefault("seen", {})
        if mid in seen:
            return
        if mid in ledger_ids():
            seen[mid] = {"ts": sa.now(), "action": "ledger-dedup"}
            save_state(state)
            return

        # ── 白名单：只允许配置里的人说话 ──
        allowed = {s.get("userid") for s in (cfg.get("senders") or []) if s.get("userid")}
        allow_any = bool(cfg.get("allow_any_sender", False))
        if not allow_any and not allowed:
            # 白名单还没配：把 userid 打出来，等人工确认后写进配置。
            # **不标记 seen** —— 配好白名单后同一条消息还能被受理（不因为一次拒绝就作废）。
            log_ignored(mid, chat_id,
                        f"白名单为空，暂不受理（发件人 userid={sender_id}）——"
                        f"确认后写进 wecom_config.json 的 senders", raw_text)
            return
        if not allow_any and sender_id not in allowed:
            seen[mid] = {"ts": sa.now(), "action": "not_allowed"}
            save_state(state)
            log_ignored(mid, chat_id,
                        f"发送者不在白名单（userid={sender_id[:12]}…）", raw_text)
            return

        # ── 群里必须 @ 机器人才算在跟它说话 ──
        text, mentioned = strip_bot_mention(raw_text, bot_name)
        if chat_type == "group" and trig.get("group_requires_mention", True) \
                and not mentioned:
            seen[mid] = {"ts": sa.now(), "action": "ignored"}
            ledger_append(mid, "", raw_text, "ignored")
            record_message(mid, {"chat_id": chat_id, "name": "", "mode": chat_type},
                           {"sender": {"id": sender_id}, "content": raw_text}, "ignored",
                           body=raw_text)
            save_state(state)
            log_ignored(mid, chat_id, "群消息未 @ 机器人", raw_text)
            return
        if len(text) < int(trig.get("min_length", 2)):
            seen[mid] = {"ts": sa.now(), "action": "too_short"}
            save_state(state)
            log_ignored(mid, chat_id, f"正文过短（<{trig.get('min_length', 2)} 字）", text)
            return

        chat = {"chat_id": chat_id, "name": "", "mode": chat_type}
        msg = {"message_id": mid, "content": text, "sender": {"id": sender_id},
               "create_time": str(body.get("create_time") or "")}
        # ── 记账 → 投 router（账本先写，router 收到事件后会回查账本）──
        record_message(mid, chat, msg, "delivered", body=text)
        ledger_append(mid, "", text, "delivered")
        source_ref = {
            "platform": "wecom",
            "chat_id": chat_id,
            "chat_type": chat_type,
            "chat_name": "",
            "chat_name_source": "unknown",
            "message_id": mid,
            # 回复目标 = 回调帧的 req_id（企微回复必须透传它）
            "reply_to": req_id,
            "sender": sender_id,
            "sender_name": sender_id,
            "sent_at": str(body.get("create_time") or ""),
            "link": "",
        }
        seen[mid] = {"ts": sa.now(), "action": "delivered", "req_id": req_id}
        chats = state.setdefault("chats", {})
        chats[chat_id] = {"last_req_id": req_id, "last_msg_id": mid, "ts": sa.now(),
                          "chat_type": chat_type}
        save_state(state)
        try:
            ev = sa.notify("user.message", None, text=text,
                           source_ref=source_ref, source="wecom")
            log(f"企微消息已投 router（trigger {ev.get('id')}）：{text[:50]}")
        except Exception as e:
            log(f"投 router 失败：{e}")


# ------------------------------------------------------------------ 发消息


def _text_body(text: str) -> dict:
    """文本消息体。

    企微的主动推送只支持 markdown / 模板卡片；回复（respond_msg）两种都行。
    这里统一用 markdown —— 两种模式都支持，且我们的正文本来就是纯文本。
    """
    t = (text or "").strip()
    if len(t.encode("utf-8")) > MAX_TEXT_BYTES:
        t = (t.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", "ignore")
             + "\n…（内容过长已截断）")
    return {"msgtype": "markdown", "markdown": {"content": t}}


def _send_on(conn: WeComConn, cmd: str, body: dict, req_id: str = "",
             timeout: float = 15) -> dict:
    """在指定连接上发一帧并等应答。"""
    headers_req = req_id or uuid.uuid4().hex
    payload = {"cmd": cmd, "headers": {"req_id": headers_req}, "body": body}
    with conn._send_lock:
        ev = threading.Event()
        conn._events[headers_req] = ev
        conn._pending[headers_req] = {}
        try:
            conn.ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception:
            conn._events.pop(headers_req, None)
            conn._pending.pop(headers_req, None)
            raise
    if not ev.wait(timeout):
        conn._events.pop(headers_req, None)
        conn._pending.pop(headers_req, None)
        raise TimeoutError(f"{cmd} 等待应答超时")
    result = conn._pending.pop(headers_req, {})
    conn._events.pop(headers_req, None)
    return result or {}


def send_welcome(req_id: str, text: str) -> dict:
    conn = ACTIVE_CONN
    if conn is None or conn.ws is None:
        raise RuntimeError("长连接不在场，无法回复欢迎语")
    return _send_on(conn, "aibot_respond_welcome_msg",
                    {"msgtype": "text", "text": {"content": text}}, req_id=req_id)


def send_message(req_id: str, chat_id: str, chat_type: str, text: str) -> dict:
    """发一条消息。

    优先用 req_id 回复（保证落在用户问的那个会话里）；
    没有 req_id 时退回主动推送（chatid + chat_type）。

    **必须走守护进程那条长连接** —— 另开连接会把守护进程踢下线。
    """
    conn = ACTIVE_CONN
    if conn is None or conn.ws is None or not conn.subscribed:
        raise RuntimeError("企微长连接未就绪，无法发消息")
    body = _text_body(text)
    if req_id:
        resp = _send_on(conn, "aibot_respond_msg", body, req_id=req_id)
        if resp.get("errcode", 0) == 0:
            return {"ok": True, "via": "respond", "request_id": req_id, "raw": resp}
        log(f"respond_msg 失败（{resp.get('errcode')} {resp.get('errmsg')}）→ 改主动推送")
    if not chat_id:
        return {"ok": False, "error": "没有 req_id 也没有 chat_id，无处可发"}
    send_body = dict(body)
    send_body["chatid"] = chat_id
    send_body["chat_type"] = 2 if chat_type == "group" else (1 if chat_type == "single" else 0)
    resp = _send_on(conn, "aibot_send_msg", send_body)
    return {"ok": resp.get("errcode", 0) == 0, "via": "send", "raw": resp}


# ------------------------------------------------------------------ 投递


def _record_age_seconds(rec: dict) -> float:
    try:
        created = _dt.datetime.fromisoformat(str(rec.get("created_at") or ""))
    except (ValueError, TypeError):
        return 0.0
    return (_dt.datetime.now().astimezone() - created).total_seconds()


def deliver_outbox() -> int:
    """把 channel=wecom 的待发消息发回企微。

    状态机与飞书侧一致：pending → succeeded | failed（重试耗尽 → dead_letter）。
    """
    sent = 0
    pending = [r for r in core.pending_outbox()
               if (r.get("channel") or "") == "wecom"]
    groups: dict[str, list[dict]] = {}
    singles: list[dict] = []
    for rec in pending:
        b = (rec.get("batch") or "").strip()
        if b and (rec.get("purpose") or "") == "result":
            groups.setdefault(b, []).append(rec)
        else:
            singles.append(rec)
    for rec in singles:
        sent += _deliver_one(rec)
    for b, recs in groups.items():
        sent += _deliver_batch(b, recs)
    return sent


def _deliver_batch(batch_id: str, recs: list[dict]) -> int:
    """同一批的结果合并成一条发出去（与飞书侧同一套口径）。"""
    st = sa.batch_status(batch_id)
    expired = any(_record_age_seconds(r) > BATCH_HOLD_MAX_SECONDS for r in recs)
    if st.get("total", 0) and not st.get("complete") and not expired:
        log(f"[企微] 批次 {batch_id[:12]} 还有 {st.get('in_flight')} 条在跑 → 结果暂存")
        return 0
    recs = sorted(recs, key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")))
    text = "\n\n".join((r.get("text") or "").strip() for r in recs if (r.get("text") or "").strip())
    if not text:
        for r in recs:
            core.update_outbox(r, status="cancelled", note="批次合并时正文为空")
        return 0
    return _deliver_one(recs[0], text=text, siblings=recs[1:])


def _deliver_one(rec: dict, text: str = "", siblings: list | None = None) -> int:
    try:
        if (rec.get("channel") or "") != "wecom":
            return 0
        req_id = rec.get("reply_to") or ""
        chat_id = rec.get("chat_id") or ""
        if not req_id and not chat_id:
            sess = core.resolve_session(rec.get("task", ""))
            req_id = sess.get("reply_to") or ""
            chat_id = sess.get("chat_id") or ""
            if req_id or chat_id:
                core.update_outbox(rec, chat_id=chat_id, reply_to=req_id,
                                   note="已从任务来源继承会话标识")
        if not req_id and not chat_id:
            core.update_outbox(rec, status="dead_letter",
                               attempts=rec.get("attempts", 0) + 1,
                               needs_manual=True,
                               note="没有可投递的会话（缺 chat_id/reply_to）"
                                    "——已进 dead_letter，需补齐会话后人工重投")
            log(f"[企微] outbox {rec['id']} 无可投递会话 → dead_letter")
            return 0
        # 会话类型：单聊 1、群聊 2（决定 chatid 怎么解析）
        src = core.task_source_ref(rec.get("task", "")) if rec.get("task") else {}
        chat_type = src.get("chat_type", "") or ""
        body = text or (rec.get("text") or "")
        resp = send_message(req_id, chat_id, chat_type, body)
        if not isinstance(resp, dict) or not resp.get("ok"):
            raise RuntimeError(f"企微返回未成功：{str(resp)[:160]}")
        delivery = {"channel": "wecom", "via": resp.get("via", ""),
                    "request_id": resp.get("request_id", ""),
                    "at": sa.now(), "verified": True}
        group = [rec] + list(siblings or [])
        for r in group:
            fields = {"status": "succeeded", "attempts": r.get("attempts", 0) + 1,
                      "sent_at": sa.now(), "delivery": delivery}
            if len(group) > 1:
                fields["note"] = "与同批其它结果合并成一条发出"
            core.update_outbox(r, **fields)
        log(f"[企微] outbox 已投递 {rec['id']}（{rec['purpose']}）"
            + (f"（同批 {len(group)} 条合并）" if len(group) > 1 else ""))
        if rec.get("task"):
            sa.log_event(rec["task"], "outbox_sent", "outbox",
                         f"{rec['purpose']} 已发回企微", outbox_id=rec["id"],
                         receipt=resp.get("request_id", ""))
        return 1
    except Exception as e:
        attempts = rec.get("attempts", 0) + 1
        status = "dead_letter" if attempts >= 3 else "pending"
        core.update_outbox(rec, status=status, attempts=attempts,
                           last_error=str(e)[:200], next_run_at=sa.now(),
                           note=("重试耗尽，需人工处理" if status == "dead_letter"
                                 else "投递失败，将重试"))
        log(f"[企微] outbox 投递失败 {rec['id']}：{e}（{status}）")
    return 0


def deliver_loop() -> None:
    """outbox 投递循环：1 秒一 tick，目录变了就立刻投。"""
    last_sig = None
    ticks = 0
    while True:
        try:
            sig = _outbox_signature()
            ticks += 1
            if sig != last_sig or ticks % 15 == 0:
                last_sig = sig
                deliver_outbox()
        except Exception as e:
            log(f"[企微] outbox 投递异常：{e}")
        time.sleep(1)


def _outbox_signature() -> tuple:
    try:
        recs = core.list_outbox("pending")
    except Exception:
        return ()
    return tuple(sorted((r.get("id", ""), r.get("created_at", ""))
                        for r in recs if (r.get("channel") or "") == "wecom"))


# ------------------------------------------------------------------ 入口


def cmd_probe(a: argparse.Namespace) -> None:
    """只测能不能连上并订阅成功（不收消息、不发消息）。

    **注意**：开一条新连接会把守护进程的长连接踢下线（企微只允许一条），
    所以守护进程在跑时不要用这个命令。
    """
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            print(json.dumps({"ok": False, "error": f"守护进程还在跑（pid {pid}）——"
                                                  f"probe 会把它的长连接踢下线，先停再测"},
                             ensure_ascii=False))
            raise SystemExit(1)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass
    cfg = load_config()
    conn = WeComConn(cfg)
    from websockets.sync.client import connect
    try:
        with connect(conn.endpoint, open_timeout=15, ping_interval=None) as ws:
            conn.ws = ws
            conn.start_reader(ws)
            conn.subscribe()
            print(json.dumps({"ok": True, "bot_id": conn.bot_id[:8] + "…",
                              "endpoint": conn.endpoint, "subscribed": True},
                             ensure_ascii=False))
            conn._stop.set()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False))
        raise SystemExit(1)


def cmd_follow(a: argparse.Namespace) -> None:
    """守护进程主循环：长连接 + 投递线程。"""
    global ACTIVE_CONN
    cfg = load_config()
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    conn = WeComConn(cfg)
    ACTIVE_CONN = conn
    threading.Thread(target=deliver_loop, daemon=True).start()
    log(f"企微桥接启动：bot_id={conn.bot_id[:8]}…（长连接模式）")
    try:
        conn.run_forever()
    except KeyboardInterrupt:
        log("企微桥接收到中断，退出")
    finally:
        with contextlib.suppress(Exception):
            PID_FILE.unlink()


def cmd_status(a: argparse.Namespace) -> None:
    st = load_state()
    out = {
        "config": str(CONFIG_FILE),
        "config_exists": CONFIG_FILE.exists(),
        "state_file": str(STATE_FILE),
        "seen_count": len(st.get("seen", {})),
        "chats": list((st.get("chats") or {}).keys()),
        "pending_wecom_outbox": len([r for r in core.pending_outbox()
                                      if (r.get("channel") or "") == "wecom"]),
        "daemon_pid": (PID_FILE.read_text(encoding="utf-8").strip()
                       if PID_FILE.exists() else ""),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description="企业微信智能机器人 ↔ super-agent 桥接")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("follow", help="常驻长连接（守护进程）")
    sub.add_parser("probe", help="测连接与订阅")
    sub.add_parser("status", help="看状态")
    a = ap.parse_args()
    if a.cmd == "follow":
        cmd_follow(a)
    elif a.cmd == "probe":
        cmd_probe(a)
    elif a.cmd == "status":
        cmd_status(a)


if __name__ == "__main__":
    main()
