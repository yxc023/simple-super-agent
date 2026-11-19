#!/usr/bin/env python3
"""按一条消息把整条链路捞出来 —— message_id / triggerId / task id 都行。

    python3 super-agent/trace.py om_x100b60d2d2d6f8a8c0b2d45ddb35ab8
    python3 super-agent/trace.py T764

链路涉及的文件（都在 SA_ROOT 下）：
    logs/lark-bridge.log      桥接过程（收到 / 投 router / 表情 / outbox 投递）
    logs/reactions.jsonl      表情时间线（append-only，带耗时）
    inbox/messages.jsonl      消息元信息
    inbox/handled.jsonl       永久账本（去重凭据）
    router/inbox.jsonl        投给 router 的事件
    router/outbox.jsonl       router 的判断与回话
    router/decisions/<id>.json 单次决策原文
    tasks/T*/events.jsonl     任务时间线（actor 归因）
    tasks/T*/channels/...     派工指令与 worker 回执
    outbox/ob_*.json          外发记录
    ~/.pi/agent/sessions/     worker / router 的原始会话（模型思考过程）
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import sa


def _root() -> pathlib.Path:
    return sa.SA_ROOT


def _lines(path: pathlib.Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                yield ln


def _jsonl(path: pathlib.Path):
    for ln in _lines(path):
        try:
            yield json.loads(ln)
        except json.JSONDecodeError:
            continue


def _ts_of(obj) -> str:
    if isinstance(obj, dict):
        for k in ("ts", "created_at", "create_time", "dispatched_at"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
    return ""


def _rel(p: pathlib.Path) -> str:
    try:
        return str(p.relative_to(_root()))
    except Exception:
        return str(p)


def collect(key: str) -> dict:
    root = _root()
    out: dict = {"key": key, "bridge": [], "reactions": [], "inbox": [],
                 "ledger": [], "router_in": [], "router_out": [], "decisions": [],
                 "tasks": {}, "outbox": [], "sessions": []}

    for ln in _lines(root / "logs" / "lark-bridge.log"):
        if key in ln:
            out["bridge"].append(ln)
    for rec in _jsonl(root / "logs" / "reactions.jsonl"):
        if key in json.dumps(rec, ensure_ascii=False):
            out["reactions"].append(rec)
    for rec in _jsonl(root / "inbox" / "messages.jsonl"):
        if key in json.dumps(rec, ensure_ascii=False):
            out["inbox"].append(rec)
    for rec in _jsonl(root / "inbox" / "handled.jsonl"):
        if key in json.dumps(rec, ensure_ascii=False):
            out["ledger"].append(rec)
    for rec in _jsonl(root / "router" / "inbox.jsonl"):
        if key in json.dumps(rec, ensure_ascii=False):
            out["router_in"].append(rec)
    for rec in _jsonl(root / "router" / "outbox.jsonl"):
        if key in json.dumps(rec, ensure_ascii=False):
            out["router_out"].append(rec)
    for p in (root / "router" / "decisions").glob("*.json"):
        if key in p.read_text(encoding="utf-8", errors="replace"):
            out["decisions"].append(p)

    tdir = root / "tasks"
    if tdir.exists():
        for t in sorted(tdir.iterdir()):
            if not t.is_dir():
                continue
            if key != t.name and not any(
                    key in p.read_text(encoding="utf-8", errors="replace")
                    for p in list(t.glob("*.json")) + list(t.glob("*.md"))
                    + list((t / "context").glob("*")) if p.is_file()):
                continue
            out["tasks"][t.name] = {
                "events": list(_jsonl(t / "events.jsonl")),
                "instructions": [p.name for p in (t / "channels" / "instructions").glob("*")] if (t / "channels" / "instructions").exists() else [],
                "responses": [p.name for p in (t / "channels" / "responses").glob("*")] if (t / "channels" / "responses").exists() else [],
            }
    for p in (root / "outbox").glob("ob_*.json"):
        if key in p.read_text(encoding="utf-8", errors="replace"):
            out["outbox"].append(p)

    # worker / router 的原始会话（pi session jsonl）
    sess_dir = pathlib.Path.home() / ".pi" / "agent" / "sessions"
    if sess_dir.exists():
        for p in sess_dir.rglob("*.jsonl"):
            name = p.name
            if key in name or any(key in f"w-{t}-{a}" for t in out["tasks"] for a in out["tasks"][t]["instructions"] if f"w-{t}-{a}" in name):
                out["sessions"].append(p)
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    key = sys.argv[1]
    d = collect(key)
    root = _root()

    print(f"══ 链路追踪：{key} ══\n")
    print(f"· 桥接日志（{len(d['bridge'])} 行）")
    for ln in d["bridge"]:
        print("   " + ln)
    if d["reactions"]:
        print(f"\n· 表情时间线（{len(d['reactions'])} 条）")
        for r in d["reactions"]:
            print(f"   {r.get('ts')} {r.get('emoji')} {r.get('elapsed_ms')}ms")
    if d["inbox"]:
        print(f"\n· 消息元信息（{len(d['inbox'])} 条）")
        for r in d["inbox"]:
            print(f"   {r.get('sent_at')} chat={r.get('chat_id')} sender={r.get('sender_name') or r.get('sender_id')} action={r.get('action')}")
    if d["ledger"]:
        print(f"\n· 永久账本（{len(d['ledger'])} 条）")
        for r in d["ledger"]:
            print(f"   {r.get('ts')} {r.get('action')} {str(r.get('content'))[:80]}")
    if d["router_in"]:
        print(f"\n· 投给 router 的事件（{len(d['router_in'])} 条）")
        for r in d["router_in"]:
            sr = r.get("source_ref") or {}
            print(f"   {r.get('ts')} id={r.get('id')} kind={r.get('kind')} chat_type={sr.get('chat_type')} text={str(r.get('text'))[:60]}")
    if d["router_out"]:
        print(f"\n· router 判断与回话（{len(d['router_out'])} 条）")
        for r in d["router_out"]:
            ev = r.get("event") or {}
            print(f"   {r.get('ts')} trigger={ev.get('id')} kind={ev.get('kind')}")
            body = r.get("router") or ""
            for line in str(body).splitlines()[:6]:
                print(f"      {line}")
    for t, info in d["tasks"].items():
        print(f"\n· 任务 {t} 时间线（{len(info['events'])} 事件）")
        for e in info["events"]:
            print(f"   {e.get('ts')} [{e.get('actor')}] {e.get('kind')} {str(e.get('message'))[:90]}")
        if info["instructions"]:
            print(f"   指令: {info['instructions']}  回执: {info['responses']}")
    if d["outbox"]:
        print(f"\n· 外发记录（{len(d['outbox'])} 条）")
        for p in d["outbox"]:
            try:
                o = json.loads(p.read_text(encoding="utf-8"))
                print(f"   {p.name} kind={o.get('kind')} status={o.get('status')} reply_to={o.get('reply_to')}")
            except Exception:
                print(f"   {p.name}")
    if d["decisions"]:
        print(f"\n· router 决策原文：{[_rel(p) for p in d['decisions']]}")
    if d["sessions"]:
        print(f"\n· 原始会话：")
        for p in d["sessions"]:
            print(f"   {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
