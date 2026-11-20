#!/usr/bin/env python3
"""消息链路视图 —— 把一条飞书消息的完整链路拼成一条时间线。

看板上的「Router 决策流 / Decision / Outbox / 收件箱」是**按文件分栏**的：
同一条消息的各个环节散在四五个面板里，要靠人肉对 message_id / triggerId / task id。
这里反过来：**以消息为主键**，把

    飞书收到 → 表情（Typing/💭/✅）→ 投 router → router 决策 → 建任务 → 派工
    → worker 执行/回执 → 放行 → 回话（outbox 凭据）

串成一条按时间排序的链路，每一段都能展开看原文。

主键链：message_id → triggerId → task id → assignment → worker session
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
import time

import sa

ROOT = sa.SA_ROOT

EMOJI = {"Typing": "⌨️", "Status_PrivateMessage": "💭", "DONE": "✅"}

_CACHE: dict = {"at": 0.0, "data": []}
_TTL = 3.0          # 看板自动刷新 5s 一次，3s TTL 足够新鲜又不至于每次全量重算
_TAIL_CACHE: dict = {}      # jsonl 增量读缓存：path → {offset, rows, pending, mtime}
_JSON_CACHE: dict = {}      # 单文件 json 缓存：path → (mtime, size, obj)
_SESSION_CACHE: dict = {"at": 0.0, "data": {}}


# ────────────────────────────── 基础工具 ──────────────────────────────

def _jsonl_tail(path: pathlib.Path, n: int = 500) -> list[dict]:
    """读 jsonl 的最后 n 条 —— **增量读**：只读上次之后新增的字节。

    这些账本都是 append-only 的，所以「记住偏移量、只解析新增行」是安全的：
    代价与**新增量**成正比，与文件总大小无关。
    防御：文件被截断（size < offset）或被原地重写（size 不变但 mtime 变了）→ 缓存作废重读。
    """
    p = str(path)
    try:
        st = path.stat()
    except OSError:
        return []
    ent = _TAIL_CACHE.get(p)
    if ent is None or st.st_size < ent["offset"] or (
            st.st_size == ent["offset"] and st.st_mtime != ent["mtime"]):
        ent = {"offset": 0, "rows": [], "pending": b"", "mtime": st.st_mtime}
        _TAIL_CACHE[p] = ent
    if st.st_size > ent["offset"]:
        try:
            with path.open("rb") as f:
                f.seek(ent["offset"])
                chunk = f.read()
        except OSError:
            return ent["rows"][-n:]
        ent["offset"] += len(chunk)
        ent["mtime"] = st.st_mtime
        buf = ent["pending"] + chunk
        parts = buf.split(b"\n")
        ent["pending"] = parts.pop()          # 最后一段可能还没写完
        for ln in parts:
            if not ln.strip():
                continue
            try:
                ent["rows"].append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        if len(ent["rows"]) > n * 4:          # 防止无限增长
            ent["rows"] = ent["rows"][-n * 4:]
    return ent["rows"][-n:]


def _read(path: pathlib.Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _assignment_file(tid: str, aid: str, kind: str) -> tuple[pathlib.Path, str]:
    """指派文件路径（新布局优先，旧布局兜底）。

    kind: "instruction" | "response"
    新：tasks/<tid>/assignments/<aid>/<kind>.md
    旧：channels/<tid>/{instructions,responses}/<aid>.md
    """
    new = ROOT / "tasks" / tid / "assignments" / aid / f"{kind}.md"
    legacy = ROOT / "channels" / tid / ("instructions" if kind == "instruction" else "responses") / f"{aid}.md"
    if new.exists() or not legacy.exists():
        return new, f"tasks/{tid}/assignments/{aid}/{kind}.md"
    return legacy, f"channels/{tid}/{'instructions' if kind == 'instruction' else 'responses'}/{aid}.md"


def _iso(v) -> str:
    """epoch / ISO 字符串 → 统一成 '2026-11-19T11:15:14+08:00'。

    epoch 按**量级**分档：秒 / 毫秒 / 微秒 / 纳秒。
    之前一律当毫秒处理，10 位秒级 epoch 会渲染成 1970 年。
    """
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        val = float(v)
        if val > 1e17:      # 纳秒
            val /= 1e6
        elif val > 1e14:    # 微秒
            val /= 1000
        elif val < 1e11:    # 秒
            val *= 1000
        # 现在 val 是毫秒
        return _dt.datetime.fromtimestamp(val / 1000).astimezone().isoformat(timespec="seconds")
    s = str(v).strip()
    # '2026-11-19 11:06' → '2026-11-19T11:06:00+08:00'（保证不同来源能按字符串排序）
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(:\d{2})?$", s)
    if m:
        naive = _dt.datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}{m.group(3) or ':00'}")
        return naive.astimezone().isoformat(timespec="seconds")
    return s


def _hm(ts: str) -> str:
    """ISO → '11:15:14'（看板里只显示时分秒）。"""
    return (ts or "")[11:19]


# ────────────────────────────── 索引 ──────────────────────────────

def _cached_json(path: pathlib.Path):
    """读单个 json 文件，按 (mtime, size) 缓存 —— 没变的文件不重复解析。"""
    p = str(path)
    try:
        st = path.stat()
    except OSError:
        return None
    hit = _JSON_CACHE.get(p)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    _JSON_CACHE[p] = (st.st_mtime, st.st_size, obj)
    return obj


def _task_index() -> dict:
    """message_id → task id，以及 task id → state。"""
    by_msg: dict = {}
    tasks: dict = {}
    tdir = ROOT / "tasks"
    if not tdir.exists():
        return {"by_msg": by_msg, "tasks": tasks}
    for d in sorted(tdir.iterdir()):
        if not d.is_dir():
            continue
        st = _cached_json(d / sa.STATE_FILE)
        if not st:
            continue
        tid = st.get("task_id") or d.name
        tasks[tid] = st
        refs = [st.get("source_ref") or {}]
        for a in st.get("assignments") or []:
            refs.append(a.get("origin_ref") or {})
        for ref in refs:
            mid = ref.get("message_id")
            if mid:
                by_msg.setdefault(mid, []).append(tid)
    return {"by_msg": by_msg, "tasks": tasks}


def _outbox_index() -> dict:
    """reply_to → outbox 记录；task → outbox 记录。"""
    by_reply: dict = {}
    by_task: dict = {}
    odir = ROOT / "outbox"
    if not odir.exists():
        return {"by_reply": by_reply, "by_task": by_task}
    for p in sorted(odir.glob("ob_*.json")):
        rec = _cached_json(p)
        if not rec:
            continue
        if rec.get("reply_to"):
            by_reply.setdefault(rec["reply_to"], []).append(rec)
        if rec.get("task"):
            by_task.setdefault(rec["task"], []).append(rec)
    return {"by_reply": by_reply, "by_task": by_task}


def _sender_names() -> dict:
    """open_id → 名字（从 lark_config.json 的白名单里拿）。

    lark_config.json 在**代码目录**下（不是状态根），
    所以 SA_ROOT 指向沙箱时也要读得到。
    """
    try:
        cfg = json.loads((pathlib.Path(__file__).resolve().parent / "lark_config.json")
                         .read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {s.get("open_id"): s.get("name") for s in cfg.get("senders") or [] if s.get("open_id")}


def _router_session_file() -> pathlib.Path | None:
    """当前 router session 文件 —— 只认**活着**的那个。

    归档（archive/）里的是历史会话，不能当现行：
    早先这里用 rglob + 「后写覆盖」匹配，归档文件排在后面就会把当前会话顶掉，
    看板上的「router session 命中」会指向一个早就归档的旧会话。
    """
    # 1) 运行中的 router 自己把 session_file 写进 snapshot
    try:
        sj = json.loads((sa.ROUTER_DIR / "session.json").read_text(encoding="utf-8"))
        p = pathlib.Path(str(sj.get("session_file") or ""))
        if p.name and p.exists():
            return p
    except (OSError, ValueError):
        pass
    # 2) 兜底：状态文件里的 id → 顶层文件
    sid = ""
    try:
        st = json.loads((sa.ROUTER_DIR / "session_id.json").read_text(encoding="utf-8"))
        sid = str(st.get("session_id") or "")
    except (OSError, ValueError):
        pass
    base = pathlib.Path.home() / ".pi" / "agent" / "sessions"
    if sid and base.exists():
        cands = [p for p in base.rglob(f"*_{sid}.jsonl") if "archive" not in p.parts]
        if cands:
            return max(cands, key=lambda p: p.stat().st_mtime)
    # 3) 再兜底：非归档里最新那个 router 会话
    if base.exists():
        cands = [p for p in base.rglob("*super-agent-router*.jsonl") if "archive" not in p.parts]
        if cands:
            return max(cands, key=lambda p: p.stat().st_mtime)
    return None


def _session_files() -> dict:
    """worker_session_id / 会话文件名 → pi session 文件路径（30s 缓存：目录遍历最贵）。"""
    now = time.time()
    if now - _SESSION_CACHE["at"] < 30 and _SESSION_CACHE["data"]:
        return _SESSION_CACHE["data"]
    out: dict = {}
    base = pathlib.Path.home() / ".pi" / "agent" / "sessions"
    if base.exists():
        # 非归档优先、其次新的优先；同一 token 只认第一个（最新的非归档文件）
        files = sorted(base.rglob("*.jsonl"),
                       key=lambda p: ("archive" in p.parts, -p.stat().st_mtime))
        for p in files:
            name = p.name
            for token in re.findall(r"(w-T\d+-A\d+-[A-Za-z0-9._-]+)", name):
                out.setdefault(token, p)
        rf = _router_session_file()
        if rf:
            out["__router__"] = rf
    _SESSION_CACHE["at"] = now
    _SESSION_CACHE["data"] = out
    return out


def _session_hit(path: pathlib.Path, needle: str) -> dict:
    """在 pi session 里找某个 trigger 的落点（行号 + 时间戳）。"""
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    hits = []
    for i, ln in enumerate(lines):
        if needle and needle in ln:
            hits.append(i + 1)
    if not hits:
        return {}
    ts = ""
    for i in hits[:1]:
        try:
            d = json.loads(lines[i - 1])
            ts = d.get("timestamp") or d.get("ts") or ""
        except (json.JSONDecodeError, IndexError):
            pass
    return {"lines": hits[:5], "count": len(hits), "ts": ts, "file": str(path)}


# ────────────────────────────── 主构建 ──────────────────────────────

def _stage(kind: str, ts: str, title: str, lines: list[str],
           raw: str = "", raw_label: str = "", scope: str = "message") -> dict:
    return {"kind": kind, "ts": ts, "title": title, "lines": [x for x in lines if x],
            "raw": raw[:12000], "raw_label": raw_label, "scope": scope}


_KIND_RANK = {"received": 0, "reaction": 1, "router": 2, "decision": 3, "session": 4,
              "task": 5, "assign": 6, "worker": 7, "outbox": 8}


def _build_one(msg: dict, idx: dict, include_raw: bool = False) -> dict:
    mid = msg.get("message_id") or ""
    stages: list[dict] = []
    ts_recv = _iso(msg.get("sent_at") or msg.get("ts"))

    stages.append(_stage(
        "received", ts_recv, "飞书收到消息",
        [f"会话 {msg.get('chat_name') or msg.get('chat_id')}",
         f"发送人 {idx['sender_names'].get(msg.get('sender_id') or '') or msg.get('sender_name') or msg.get('sender_id')}",
         f"处理动作 {msg.get('action')}" + (f" → 任务 {msg.get('task')}" if msg.get("task") else ""),
         f"正文 {(msg.get('content') or '')[:200]}"],
        raw=json.dumps(msg, ensure_ascii=False, indent=2), raw_label="inbox/messages.jsonl"))

    # 表情
    for r in idx["reactions"]:
        if r.get("message_id") != mid:
            continue
        em = r.get("emoji") or ""
        stages.append(_stage(
            "reaction", _iso(r.get("ts")),
            f"表情 {EMOJI.get(em, '')} {em}",
            [f"自身耗时 {r.get('elapsed_ms')} ms"],
            raw=json.dumps(r, ensure_ascii=False), raw_label="logs/reactions.jsonl"))

    # 投给 router
    ev = None
    for e in idx["router_in"]:
        if ((e.get("source_ref") or {}).get("message_id") == mid):
            ev = e
            break
    trigger = (ev or {}).get("id") or ""
    if ev:
        sr = ev.get("source_ref") or {}
        stages.append(_stage(
            "router", _iso(ev.get("ts")), "投给 router",
            [f"triggerId {trigger}", f"事件类型 {ev.get('kind')}",
             f"会话形态 {sr.get('chat_type') or '-'}",
             f"原文 {(ev.get('text') or '')[:200]}"],
            raw=json.dumps(ev, ensure_ascii=False, indent=2), raw_label="router/inbox.jsonl"))

    # router 决策（叙述 + 结构化 Decision）
    narrative = ""
    for o in idx["router_out"]:
        if ((o.get("event") or {}).get("id") == trigger):
            narrative = o.get("router") or ""
            break
    if narrative:
        stages.append(_stage(
            "decision", _iso(ev.get("ts")) if ev else "", "router 判断（叙述）",
            [ln for ln in str(narrative).splitlines()[:8]],
            raw=narrative, raw_label="router/outbox.jsonl"))
    dec = {}
    if trigger:
        dp = ROOT / "router" / "decisions" / f"{trigger}.json"
        if dp.exists():
            try:
                dec = json.loads(dp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                dec = {}
            if not isinstance(dec, dict):
                # 文件内容是数组/字符串时，dec.get 会 AttributeError，整个面板 500
                dec = {}
    sub = (dec.get("submitted") or {})
    res = (dec.get("result") or {})
    if sub:
        items = []
        for it in sub.get("items") or []:
            items.append(f"{it.get('taskAction') or '-'} {it.get('taskId') or '新任务'} → "
                         f"{it.get('agentId') or '-'}（置信度 {it.get('confidence', '-')}）"
                         f"{(it.get('goal') or '')[:120]}")
        rej = [f"✗ {r.get('reason_code', '')}：{r.get('message', '')}" for r in (res.get("rejected") or [])]
        stages.append(_stage(
            "decision", _iso(res.get("ts")), "Decision（结构化）",
            items + rej, raw=json.dumps(dec, ensure_ascii=False, indent=2),
            raw_label=f"router/decisions/{trigger}.json"))

    # router session 落点
    if trigger and idx["sessions"].get("__router__"):
        hit = _session_hit(idx["sessions"]["__router__"], trigger)
        if hit:
            stages.append(_stage(
                "session", hit.get("ts") or "", "router session 命中",
                [f"session 文件 {pathlib.Path(hit['file']).name}",
                 f"命中行 {hit.get('lines')}（共 {hit.get('count')} 处）"],
                raw=json.dumps(hit, ensure_ascii=False), raw_label="pi session"))

    # 任务 + 指派 + worker
    tids = []
    for it in sub.get("items") or []:
        if it.get("taskId"):
            tids.append(it["taskId"])
    tids += idx["task_by_msg"].get(mid, [])
    seen_task = set()
    for tid in tids:
        if tid in seen_task:
            continue
        seen_task.add(tid)
        st = idx["tasks"].get(tid)
        if not st:
            continue
        stages.append(_stage(
            "task", _iso(st.get("created_at")), f"任务 {tid}",
            [f"标题 {st.get('title')}", f"状态 {st.get('status')}",
             f"目标 {(st.get('goal') or '')[:200]}"],
            raw=json.dumps(st, ensure_ascii=False, indent=2),
            raw_label=f"tasks/{tid}/state.json", scope="task"))
        for e in _jsonl_tail(ROOT / "tasks" / tid / "events.jsonl", 200):
            if e.get("kind") in ("task_created", "assigned"):
                continue
            stages.append(_stage(
                "worker", _iso(e.get("ts")), f"{e.get('kind')} · {e.get('actor')}",
                [str(e.get("message") or "")[:400]],
                raw=json.dumps(e, ensure_ascii=False),
                raw_label=f"tasks/{tid}/events.jsonl", scope="task"))
        for a in st.get("assignments") or []:
            aid = a.get("id")
            sid = a.get("worker_session_id") or ""
            sess = idx["sessions"].get(sid)
            lines = [f"worker {a.get('worker')} · 状态 {a.get('status')}",
                     f"摘要 {(a.get('brief') or '')[:120]}"]
            if sess:
                lines.append(f"worker session {sess.name}")
            instr_path, instr_label = _assignment_file(tid, aid, "instruction") if aid else (None, "")
            resp_path, resp_label = _assignment_file(tid, aid, "response") if aid else (None, "")
            stages.append(_stage(
                "assign", _iso(a.get("dispatched_at")), f"指派 {aid} → {a.get('worker')}",
                lines,
                raw=(_read(instr_path) if instr_path else ""),
                raw_label=instr_label, scope="task"))
            if aid:
                resp = _read(resp_path, 3000)
                if resp:
                    stages.append(_stage(
                        "worker", _iso(a.get("completed_at")), f"回执 {aid}",
                        [ln for ln in resp.splitlines()[:8] if ln.strip()],
                        raw=resp, raw_label=resp_label, scope="task"))

    # 外发回话
    obs = list(idx["outbox_by_reply"].get(mid, []))
    for tid in seen_task:
        obs += [o for o in idx["outbox_by_task"].get(tid, []) if o not in obs]
    for o in obs:
        stages.append(_stage(
            "outbox", _iso(o.get("sent_at") or o.get("created_at")),
            f"回话 · {o.get('purpose') or o.get('target') or 'outbox'}",
            [f"{o.get('id')} · 状态 {o.get('status')}"
             + (f" · 凭据 {((o.get('delivery') or {}).get('message_id'))}" if (o.get("delivery") or {}).get("message_id") else ""),
             (o.get("text") or "")[:240]],
            raw=json.dumps(o, ensure_ascii=False, indent=2), raw_label="outbox",
            scope="message" if o.get("reply_to") == mid else "task"))

    stages.sort(key=lambda s: (s["ts"] or "", _KIND_RANK.get(s["kind"], 9)))
    if not include_raw:
        for s in stages:
            s["raw"] = ""      # 原文按需取（/api/chain-raw），避免每次轮询都传几百 KB
    return {
        "message_id": mid,
        "chat_id": msg.get("chat_id") or "",
        "chat_name": msg.get("chat_name") or msg.get("chat_id") or "",
        "sender": msg.get("sender_name") or msg.get("sender_id") or "",
        "sent_at": ts_recv,
        "content": msg.get("content") or "",
        "action": msg.get("action") or "",
        "task": msg.get("task") or "",
        "trigger": trigger,
        "tasks": sorted(seen_task),
        "stage_count": len(stages),
        "stages": stages,
    }


def _indexes() -> dict:
    tidx = _task_index()
    oidx = _outbox_index()
    return {
        "reactions": _jsonl_tail(ROOT / "logs" / "reactions.jsonl", 500),
        "router_in": _jsonl_tail(ROOT / "router" / "inbox.jsonl", 600),
        "router_out": _jsonl_tail(ROOT / "router" / "outbox.jsonl", 400),
        "task_by_msg": tidx["by_msg"],
        "tasks": tidx["tasks"],
        "outbox_by_reply": oidx["by_reply"],
        "outbox_by_task": oidx["by_task"],
        "sessions": _session_files(),
        "sender_names": _sender_names(),
    }


def build_chains(limit: int = 18, force: bool = False, include_raw: bool = False) -> list[dict]:
    """最近 limit 条被受理的飞书消息 → 每条一条完整链路。

    include_raw=False（看板默认）：不把大段原文（state.json / decision / outbox）
    塞进每次轮询的 /api/state 里；原文按需走 /api/chain-raw 取。
    """
    now = time.time()
    if not force and now - _CACHE["at"] < _TTL and _CACHE["data"]:
        return _CACHE["data"]

    msgs = [m for m in _jsonl_tail(ROOT / "inbox" / "messages.jsonl", 400)
            if m.get("message_id") and m.get("action") in ("delivered", "task")]
    msgs = msgs[-limit:][::-1]
    idx = _indexes()          # 索引**只建一次**（早前版本放在列表推导里 → 每条消息重建一次，实测 60ms → 640ms）
    data = [_build_one(m, idx, include_raw) for m in msgs]
    _CACHE["at"] = now
    _CACHE["data"] = data
    return data


def stage_raw(message_id: str, index: int) -> dict:
    """按需取某一阶段的原文（看板点「原文」时调）。"""
    msgs = [m for m in _jsonl_tail(ROOT / "inbox" / "messages.jsonl", 400)
            if m.get("message_id") == message_id]
    if not msgs:
        return {}
    chain = _build_one(msgs[-1], _indexes(), True)
    stages = chain.get("stages") or []
    if index < 0 or index >= len(stages):
        return {}
    s = stages[index]
    return {"title": s.get("title"), "ts": s.get("ts"), "label": s.get("raw_label"),
            "raw": s.get("raw") or ""}


if __name__ == "__main__":
    import sys
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    for ch in build_chains(force=True):
        if key and key not in json.dumps(ch, ensure_ascii=False):
            continue
        print(f"══ {ch['sent_at']} {ch['sender']} · {ch['chat_name']}")
        print(f"   {ch['content'][:120]}")
        for s in ch["stages"]:
            print(f"   {_hm(s['ts'])} [{s['kind']}] {s['title']}")
            for ln in s["lines"][:4]:
                print(f"        {ln}")
