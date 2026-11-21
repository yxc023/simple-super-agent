#!/usr/bin/env python3
"""自检：把 Task Center 的关键路径跑一遍并断言。

    python3 super-agent/selftest.py

覆盖：
  1. Registry 派工校验（在册 / 停用 / 只读匹配）
  2. Decision Schema 校验（负例）
  3. Decision 逐条独立生效（单条被拒不回滚其它）
  4. approve 必须有产出证据（no_evidence）
  5. 同一 triggerId 重复提交幂等
  6. Decision 只能落在本轮事件的任务上
  7. waiting_user_input → needs_human
  8. 完成策略：目标 + 完成证据 → completed
  9. outbox 去重
  10. 指令单模板渲染
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import core  # noqa: E402
import sa  # noqa: E402
import worker_runner  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  —— {detail}" if detail and not cond else ""))


def main() -> None:
    # 自检用宽松策略：新准入规则不该把测试里的假事件全挡掉
    _pol = pathlib.Path(tempfile.mkdtemp()) / "policy.json"
    _pol.write_text(json.dumps({"owner": {"open_id": ""}, "groups": {"allow": []},
                                "private_chat": {"owner_only": False}}), encoding="utf-8")
    os.environ["SA_POLICY_FILE"] = str(_pol)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sa-selftest-"))
    orig_tasks, orig_channels = sa.TASKS_DIR, sa.CHANNELS_DIR
    orig_legacy_channels = sa.LEGACY_CHANNELS_DIR
    orig_core_outbox = core.OUTBOX_DIR
    orig_core_tasks = core.TASKS_DIR
    orig_router_dir, orig_router_inbox = sa.ROUTER_DIR, sa.ROUTER_INBOX
    sa.TASKS_DIR = tmp / "tasks"
    sa.CHANNELS_DIR = tmp / "channels"
    sa.LEGACY_CHANNELS_DIR = sa.CHANNELS_DIR
    core.OUTBOX_DIR = tmp / "outbox"
    # 关键：自检事件不能写进真实 router 收件箱，否则会把常驻 router session 唤醒，
    # 让它处理一个只存在于临时沙箱里的 task（曾导致 triggerId 指向不存在的任务）。
    # 注意 ROUTER_INBOX 是独立的模块常量，必须单独重定向。
    sa.ROUTER_DIR = tmp / "router"
    sa.ROUTER_INBOX = sa.ROUTER_DIR / "inbox.jsonl"
    for d in (sa.TASKS_DIR, sa.CHANNELS_DIR, sa.ROUTER_DIR):
        d.mkdir(parents=True, exist_ok=True)

    try:
        print("\n[1] Registry 派工校验")
        ok, why = core.validate_agent("general-purpose")
        check("在册且启用的 worker 通过", ok, why)
        ok, why = core.validate_agent("research")
        check("停用的 worker 被拒", not ok and "停用" in why, why)
        ok, why = core.validate_agent("不存在的")
        check("不在册的 worker 被拒", not ok and "名册" in why, why)

        print("\n[2] Decision Schema 校验（负例）")
        bad = {"schemaVersion": 1, "triggerId": "t1", "items": [
            {"triggerId": "t1", "taskAction": "create", "agentId": "不存在", "goal": "g"}]}
        good, errs = core.validate_decision(bad, allowed_triggers=["t1"],
                                            known_task_ids=[], known_assignments=[])
        check("非法 agentId 被拒", any(e["reason_code"] == "bad_agent" for e in errs))
        bad2 = {"schemaVersion": 1, "triggerId": "t2", "items": [
            {"triggerId": "t2", "taskAction": "continue", "agentId": "explore", "goal": "g"}]}
        good2, errs2 = core.validate_decision(bad2, allowed_triggers=["t2"],
                                               known_task_ids=[], known_assignments=[])
        check("continue 缺 taskId 被拒",
              any(e["reason_code"] == "continue_without_task_id" for e in errs2))
        bad3 = {"schemaVersion": 1, "triggerId": "t3", "items": [
            {"triggerId": "t3", "taskAction": "create", "agentId": "explore", "goal": "g"}]}
        good3, errs3 = core.validate_decision(bad3, allowed_triggers=["别的"],
                                               known_task_ids=[], known_assignments=[])
        check("不在本批的 triggerId 被拒",
              any(e["reason_code"] == "unknown_trigger" for e in errs3))

        print("\n[3] 建任务 + Decision 落地")
        import argparse as _ap
        tid = sa.cmd_new_task(_ap.Namespace(
            id=None, title="自检任务", goal="自检用",
            context=None, context_file=None, acceptance="能通过",
            source="manual", source_ref=None, by="human", allow_new_task=False))
        doc = {
            "schemaVersion": 2, "triggerId": "trig-1",
            "items": [
                {"triggerId": "trig-1", "taskAction": "continue", "taskId": tid,
                 "agentId": "general-purpose", "goal": "写 /tmp/x.md",
                 "confidence": 0.9, "reason": "实现", "rationale": "简单"},
                {"triggerId": "trig-1", "taskAction": "continue", "taskId": tid,
                 "agentId": "不存在", "goal": "这条应该被单独拒绝",
                 "confidence": 0.1, "reason": "坏", "rationale": "坏"},
            ],
            "reviews": [],
            "outbox": [{"target": "thread", "purpose": "ack", "text": "已受理"}],
        }
        res = sa.apply_decision(tid, doc, actor="router")
        check("合法条目已落地", any(a.get("kind") == "dispatch" for a in res["applied"]))
        check("非法条目被单独拒绝", any(r["reason_code"] == "bad_agent" for r in res["rejected"]))
        st = sa.load_state(tid)
        check("指派已建且渲染了指令单",
              len(st["assignments"]) == 1 and
              pathlib.Path(st["assignments"][0]["instruction"]).exists())
        instr = pathlib.Path(st["assignments"][0]["instruction"]).read_text(encoding="utf-8")
        check("指令单由模板渲染（含回执要求）",
              "## 回执要求" in instr)
        check("指令单没有「具体要求 / 验收标准」小节（做法交给 worker）",
              "## 具体要求" not in instr and "## 验收标准" not in instr, instr[:200])
        check("指令单带用户目标", "写 /tmp/x.md" in instr)

        print("\n[4] approve 必须有产出证据")
        res2 = sa.apply_decision(tid, {
            "schemaVersion": 2, "triggerId": "trig-2",
            "items": [], "reviews": [{"assignment": st["assignments"][0]["id"],
                                      "verdict": "approve", "note": "看起来没问题"}],
            "outbox": []}, actor="router")
        check("无证据的 approve 被拒（no_evidence）",
              any(r["reason_code"] == "no_evidence" for r in res2["rejected"]))

        print("\n[5] 有证据的 approve → completed")
        aid = st["assignments"][0]["id"]
        st = sa.load_state(tid)
        for a in st["assignments"]:
            a["artifacts"] = ["/tmp/x.md"]
        sa.save_state(tid, st)
        res3 = sa.apply_decision(tid, {
            "schemaVersion": 2, "triggerId": "trig-3",
            "items": [], "reviews": [{"assignment": aid, "verdict": "approve", "note": "核验过"}],
            "outbox": [{"target": "thread", "purpose": "result", "text": "做完了"}]}, actor="router")
        st = sa.load_state(tid)
        check("approve 已生效", st["assignments"][0]["status"] == "approved",
              str(st["assignments"][0]["status"]))
        check("任务状态 = completed", st["status"] == "completed", st["status"])

        print("\n[6] 同一 triggerId 重复提交幂等")
        before = sa.load_state(tid)["assignments"][0]["status"]
        sa.apply_decision(tid, {
            "schemaVersion": 2, "triggerId": "trig-3",
            "items": [], "reviews": [{"assignment": aid, "verdict": "reject", "note": "重做"}],
            "outbox": []}, actor="router")
        after = sa.load_state(tid)["assignments"][0]["status"]
        check("重复 triggerId 不会把 approved 改回 rework", before == after == "approved",
              f"{before} → {after}")

        print("\n[7] waiting_user_input → needs_human")
        tid2 = sa.cmd_new_task(_ap.Namespace(
            id=None, title="提问任务", goal="worker 要问人", context=None, context_file=None,
            acceptance="", source="manual", source_ref=None, by="human", allow_new_task=False))
        (tmp / "instr.md").write_text("# 指派单\n\n## 任务目标\n自检\n", encoding="utf-8")
        sa.cmd_assign(_ap.Namespace(task=tid2, worker="explore",
                                    instruction_file=str(tmp / "instr.md"), brief="b"))
        st2 = sa.load_state(tid2)
        aid2 = st2["assignments"][0]["id"]
        sa.cmd_respond(_ap.Namespace(assignment=f"{tid2}/{aid2}", status="waiting_user_input",
                                     summary="需要知道用哪个环境", artifact=None))
        st2 = sa.load_state(tid2)
        check("指派状态 = waiting_user_input", st2["assignments"][0]["status"] == "waiting_user_input")
        check("任务状态 = needs_human", st2["status"] == "needs_human", st2["status"])

        print("\n[8] outbox 去重")
        m1 = core.add_outbox(target="thread", purpose="reply", text="同一条", task=tid)
        m2 = core.add_outbox(target="thread", purpose="reply", text="同一条", task=tid)
        check("相同内容不产生第二条", m1["id"] == m2["id"], f"{m1['id']} vs {m2['id']}")
        m3 = core.add_outbox(target="thread", purpose="reply", text="另一条", task=tid)
        check("不同内容产生新条目", m3["id"] != m1["id"])

        print("\n[9] 完成策略：缺证据不算完成")
        st3 = sa.load_state(tid2)
        for a in st3["assignments"]:
            a["status"] = "approved"
        sa.save_state(tid2, st3)
        status, reason = core.evaluate_completion(sa.load_state(tid2))
        check("已放行但缺 artifact → needs_human", status == "needs_human", f"{status} / {reason}")

        print("\n[9b] 完成策略：已取消的指派不阻塞完成")
        st4 = sa.load_state(tid2)
        for a in st4["assignments"]:
            a["status"] = "approved"
            a["artifacts"] = ["/tmp/x"]
        st4["assignments"].append({
            "id": "A99", "worker": "general-purpose", "status": "cancelled",
            "review": "cancelled", "artifacts": [], "instruction": "", "response": "",
            "pending_ack": False, "dispatched_at": "", "completed_at": "",
        })
        status, reason = core.evaluate_completion(st4)
        check("含 cancelled 指派时仍可 completed", status == "completed", f"{status} / {reason}")
        st5 = json.loads(json.dumps(st4))
        for a in st5["assignments"]:
            a["status"] = "cancelled"
        status, reason = core.evaluate_completion(st5)
        check("全部取消 → needs_human（无有效交付）", status == "needs_human", f"{status} / {reason}")

        print("\n[10] Schema v2：create=新任务 / continue=沿用")
        ok, errs = core.validate_decision(
            {"schemaVersion": 2, "triggerId": "t", "items": [
                {"triggerId": "t", "taskAction": "create", "taskId": "T001",
                 "agentId": "explore", "goal": "g"}]},
            allowed_triggers=["t"], known_task_ids=["T001"], known_assignments=[])
        check("create 带 taskId 被拒",
              any(e["reason_code"] == "create_with_task_id" for e in errs))
        ok, errs = core.validate_decision(
            {"schemaVersion": 2, "triggerId": "t", "items": [
                {"triggerId": "t", "taskAction": "create",
                 "agentId": "explore", "goal": "g"}]},
            allowed_triggers=["t"], known_task_ids=["T001"], known_assignments=[])
        check("create 不带 taskId 通过", ok and not errs, str(errs))

        print("\n[11] create → 真的新建任务并派工")
        def _ev_kinds():
            if not sa.ROUTER_INBOX.exists():
                return []
            out = []
            for l in sa.ROUTER_INBOX.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(l)["kind"])
                except Exception:
                    pass
            return out
        kinds_before = _ev_kinds()
        res4 = sa.apply_decision("", {
            "schemaVersion": 2, "triggerId": "trig-new",
            "items": [{"triggerId": "trig-new", "taskAction": "create",
                       "title": "自检新建任务", "agentId": "general-purpose",
                       "goal": "写 /tmp/created.md", "requirements": [],
                       "acceptance": ["文件存在"], "confidence": 0.9,
                       "reason": "实现", "rationale": "新目标"}],
            "reviews": [], "outbox": []}, actor="router")
        created = [a for a in res4["applied"] if a.get("kind") == "task.create"]
        dispatched = [a for a in res4["applied"] if a.get("kind") == "dispatch"]
        check("create 生成了新任务", len(created) == 1, str(res4))
        check("create 同时派了工", len(dispatched) == 1)
        if created and dispatched:
            nt = created[0]["task"]
            stn = sa.load_state(nt)
            check("新任务挂在正确的任务下",
                  stn["title"] == "自检新建任务", stn.get("title", ""))
            check("新指派有 worker session id",
                  bool(stn["assignments"][0].get("worker_session_id")))
            check("新任务不会通知 router（避免自唤醒）",
                  _ev_kinds().count("task.created") == kinds_before.count("task.created"),
                  f"{kinds_before.count('task.created')} → {_ev_kinds().count('task.created')}")

        print("\n[12] continue + sessionFrom → 沿用任务并继承 session")
        if dispatched:
            src_task = dispatched[0]["task"]
            src_aid = dispatched[0]["assignment"]
            src_sid = sa.load_state(src_task)["assignments"][0]["worker_session_id"]
            res5 = sa.apply_decision("", {
                "schemaVersion": 2, "triggerId": "trig-cont",
                "items": [{"triggerId": "trig-cont", "taskAction": "continue",
                           "taskId": src_task, "agentId": "general-purpose",
                           "sessionFrom": src_aid, "goal": "继续做一点",
                           "requirements": [], "acceptance": [], "confidence": 0.8,
                           "reason": "实现", "rationale": "延续"}],
                "reviews": [], "outbox": []}, actor="router")
            d2 = [a for a in res5["applied"] if a.get("kind") == "dispatch"]
            check("continue 沿用了原任务", len(d2) == 1 and d2[0]["task"] == src_task, str(res5))
            check("标记为继承 session", bool(d2 and d2[0].get("inherited_session")))
            if d2:
                st2b = sa.load_state(src_task)
                new_a = [a for a in st2b["assignments"] if a["id"] == d2[0]["assignment"]][0]
                check("新指派复用了同一个 worker session id",
                      new_a["worker_session_id"] == src_sid,
                      f"{new_a['worker_session_id']} vs {src_sid}")

        print("\n[13] sessionFrom 校验")
        ok, errs = core.validate_decision(
            {"schemaVersion": 2, "triggerId": "t", "items": [
                {"triggerId": "t", "taskAction": "continue", "taskId": "T001",
                 "agentId": "explore", "sessionFrom": "A99", "goal": "g"}]},
            allowed_triggers=["t"], known_task_ids=["T001"], known_assignments=[],
            session_sources=[])
        check("sessionFrom 指向不存在的指派被拒",
              any(e["reason_code"] == "unknown_session_source" for e in errs))
        ok, errs = core.validate_decision(
            {"schemaVersion": 2, "triggerId": "t", "items": [
                {"triggerId": "t", "taskAction": "continue", "taskId": "T001",
                 "agentId": "plan", "sessionFrom": "A01", "goal": "g"}]},
            allowed_triggers=["t"], known_task_ids=["T001"], known_assignments=[],
            session_sources=[{"task": "T001", "assignment": "A01",
                              "agent": "explore", "session": "w-1"}])
        check("sessionFrom 的 agent 与 agentId 不一致被拒",
              any(e["reason_code"] == "session_agent_mismatch" for e in errs))

        print("\n[14] router 的工具护栏与 session 轮转")
        router_src = (pathlib.Path(__file__).resolve().parent / "router.py").read_text(encoding="utf-8")
        check("router 只给最小工具集（read,write,bash）", '"--tools", "read,write,bash"' in router_src)
        check("router 挂载了工具护栏 extension", '"--extension"' in router_src and "router_gate.ts" in router_src)
        gate_src = (pathlib.Path(__file__).resolve().parent / "router_gate.ts").read_text(encoding="utf-8")
        check("护栏限制 bash 只能跑 sa.py",
              "isTaskCenterCommand" in gate_src and "SA_COMMANDS" in gate_src
              and "sa.py" in gate_src and "block: true" in gate_src)
        check("护栏拒绝 shell 拼接（&& ; | $()）",
              "SHELL_METACHARS" in gate_src)
        check("护栏限制 write 只能写 /tmp",
              "under(p, \"/tmp\")" in gate_src)
        check("router 保持单一 session（不自动轮转）",
              "sess.rotate()" not in router_src.split("def cmd_rotate")[0])
        check("提供手动轮转命令（session 太大时按需触发）",
              "def cmd_rotate" in router_src)
        check("router 用摘要文件承接跨轮次上下文",
              "digest.md" in router_src)

        print("\n[15] 飞书回复走话题串")
        bridge_src = (pathlib.Path(__file__).resolve().parent / "lark_bridge.py").read_text(encoding="utf-8")
        check("回复带 --reply-in-thread", "--reply-in-thread" in bridge_src)
        cfg = json.loads((pathlib.Path(__file__).resolve().parent / "lark_config.json").read_text(encoding="utf-8"))
        check("配置项 reply_in_thread 默认开启", cfg.get("reply_in_thread") is True)

        print("\n[16] 话题消息轮询")
        bridge_src2 = (pathlib.Path(__file__).resolve().parent / "lark_bridge.py").read_text(encoding="utf-8")
        check("桥接轮询话题串消息", "+threads-messages-list" in bridge_src2)
        check("话题里不强制 @ 机器人（bot_engaged）", "bot_engaged" in bridge_src2)
        check("话题登记在 seen 检查之前（否则永远轮询不到）",
              bridge_src2.index("threads.setdefault") < bridge_src2.rindex('if mid in state.get("seen", {}):'))

        print("\n[17] 接收消息走官方事件订阅")
        bs = (pathlib.Path(__file__).resolve().parent / "lark_bridge.py").read_text(encoding="utf-8")
        check("守护进程消费 im.message.receive_v1", "im.message.receive_v1" in bs)
        check("有 follow 模式（长连接为主、轮询兜底）", "def cmd_follow" in bs)
        check("长连接失败会降级轮询", "降级为轮询模式" in bs)
        check("用结构化 mentions 判定 @", "mentions" in bs and "def bot_mentioned" in bs)
        check("话题消息靠 root_id/thread_id 识别", "root_id" in bs and "thread_id" in bs)
        check("抢不到锁会等待而不是丢事件", "wait_seconds=2.5" in bs)

        print("\n[18] 任务 id 不复用")
        first = sa.next_task_id()
        second = sa.next_task_id()
        check("连续分配的 id 不同", first != second, f"{first} / {second}")

        print("\n[19] router 读 inbox：同 inode 就地改写不丢事件")
        import router as router_mod
        inbox = tmp / "router_inbox.jsonl"

        def _read(cursor, processed):
            """模拟 run_loop 的读法：拉新行 + 已处理的过滤掉。"""
            out = []
            if not cursor.refresh():
                return out
            for raw in cursor.lines():
                s = raw.decode("utf-8", errors="replace").strip()
                if not s:
                    continue
                try:
                    ev = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if ev.get("id") and ev["id"] in processed:
                    continue
                out.append(ev.get("id"))
            return out

        # 场景 A：同 inode 截断重写（真实丢事件的那一种）
        inbox.write_bytes(b'{"id":"e1"}\n{"id":"e2"}\n')
        cur = router_mod.InboxCursor(inbox)
        seen_a = _read(cur, set())
        check("首次读取拿到全部事件", seen_a == ["e1", "e2"], str(seen_a))
        processed = {"e1", "e2"}
        with inbox.open("wb") as f:          # 同一 inode 就地截断重写
            f.write(b'{"id":"e3"}\n')
        got = _read(cur, processed)
        check("同 inode 就地截断重写后新事件不被跳过", got == ["e3"], str(got))

        # 场景 B：同 inode 等长改写（内容指纹变了）
        inbox.write_bytes(b'{"id":"x1"}\n')
        cur2 = router_mod.InboxCursor(inbox)
        seen_b = _read(cur2, set())
        check("等长文件首次读取正常", seen_b == ["x1"], str(seen_b))
        with inbox.open("wb") as f:
            f.write(b'{"id":"x9"}\n')      # 长度相同、内容不同
        got2 = _read(cur2, {"x1"})
        check("同 inode 等长改写后能感知并重读", got2 == ["x9"], str(got2))

        # 场景 C：同 inode 追加（不应把旧事件重复吐出来）
        inbox.write_bytes(b'{"id":"y1"}\n')
        cur3 = router_mod.InboxCursor(inbox)
        _read(cur3, set())
        with inbox.open("ab") as f:
            f.write(b'{"id":"y2"}\n')
        got3 = _read(cur3, {"y1"})
        check("同 inode 追加只给新增事件（不重复处理）", got3 == ["y2"], str(got3))

        print("\n[20] outbox：会话继承 + 无会话记录不静默丢弃")
        core.TASKS_DIR = tmp / "tasks"
        tid3 = sa.cmd_new_task(_ap.Namespace(
            id=None, title="带来源的任务", goal="测会话继承",
            context=None, context_file=None, acceptance="",
            source="manual", source_ref=["chat_id=oc_test", "message_id=om_test"],
            by="human", allow_new_task=False))
        sess = core.resolve_session(tid3)
        check("能从任务 source_ref 解析出会话", sess["chat_id"] == "oc_test" and
              sess["reply_to"] == "om_test", str(sess))
        rec = core.add_outbox(target="thread", purpose="ack",
                               text="已受理：任务号 " + tid3, task=tid3)
        check("Decision 生成的 ack 继承了会话标识",
              rec["chat_id"] == "oc_test" and rec["reply_to"] == "om_test",
              f"{rec['chat_id']} / {rec['reply_to']}")
        check("继承来源被标注", rec.get("session_source") == "task.source_ref",
              str(rec.get("session_source")))

        # 无会话、无任务的记录 → 投递器不得静默丢弃
        import lark_bridge
        core.OUTBOX_DIR = tmp / "outbox_deliver"
        lonely = core.add_outbox(target="thread", purpose="ack", text="无处可投的记录",
                                 task="", dedupe_window_seconds=0)
        lark_bridge.deliver_outbox()
        rec2 = core.list_outbox().pop()
        check("无会话记录不进 failed（不再静默丢弃）", rec2["status"] != "failed",
              rec2["status"])
        check("无会话记录进 dead_letter（看板可见）", rec2["status"] == "dead_letter",
              rec2["status"])
        check("无会话记录标注了原因", bool(rec2.get("note")), str(rec2.get("note")))
        check("无会话记录标了需人工处理", bool(rec2.get("needs_manual")),
              str(rec2.get("needs_manual")))
        # 人工重投路径
        sa.cmd_outbox_retry(_ap.Namespace(id=rec2["id"]))
        rec3 = [r for r in core.list_outbox() if r["id"] == rec2["id"]][0]
        check("dead_letter 可人工重投回 pending", rec3["status"] == "pending", rec3["status"])
        core.OUTBOX_DIR = tmp / "outbox"

        print("\n[21] 事件模式：兜底轮询 + 状态字段 + pid 文件")
        bs2 = (pathlib.Path(__file__).resolve().parent / "lark_bridge.py").read_text(encoding="utf-8")
        check("事件模式有真兜底轮询（不再是只写日志）",
              "def fallback_poll" in bs2 and "fallback_seconds" in bs2)
        check("没有遗留未使用的 poll_tick", "poll_tick =" not in bs2)
        check("事件模式记录 last_message_at", "last_message_at" in bs2)
        check("事件模式写实际 mode=event", 'st["mode"] = "event"' in bs2)
        check("桥接 pid 记的是真实 python 进程", "os.getpid()" in bs2)
        check("两条接收路径共用永久账本去重", "ledger-dedup" in bs2)
        check("router 用内容指纹感知同 inode 改写",
              "class InboxCursor" in (pathlib.Path(__file__).resolve().parent
                                      / "router.py").read_text(encoding="utf-8"))
        core_src = (pathlib.Path(__file__).resolve().parent / "core.py").read_text(encoding="utf-8")
        check("outbox 有会话解析与继承能力",
              "def resolve_session" in core_src and "def task_source_ref" in core_src)
        cfg2 = json.loads((pathlib.Path(__file__).resolve().parent
                           / "lark_config.json").read_text(encoding="utf-8"))
        check("配置里有兜底轮询参数", "fallback_seconds" in (cfg2.get("poll") or {}))

        print("\n[22] 事件路径：话题内免 @ 校验 + 话题 id 全链路可见")
        import lark_bridge as lb
        cfg3 = json.loads((pathlib.Path(__file__).resolve().parent
                           / "lark_config.json").read_text(encoding="utf-8"))
        cfg3.setdefault("trigger", {})["thread_requires_mention"] = True
        lb.MESSAGES_FILE = tmp / "messages.jsonl"
        lb.LEDGER_FILE = tmp / "handled.jsonl"
        lb.LOG_FILE = tmp / "lark-bridge.log"
        st3 = {"seen": {}}

        def _router_events():
            if not sa.ROUTER_INBOX.exists():
                return []
            out = []
            for line in sa.ROUTER_INBOX.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.append(json.loads(line))
            return out

        # 场景 1：话题里的普通发言（未 @ 机器人）→ 不受理、不唤醒 router
        ev_plain = {"message_id": "om_topic_plain", "chat_id": "oc_t", "sender_id": "ou_u",
                    "sender_type": "user", "content": "大家随便聊聊",
                    "thread_id": "omt_topic", "root_id": "om_root",
                    "create_time": "2026-11-18 11:00"}
        act1 = lb.process_event(cfg3, st3, ev_plain)
        check("未 @ 的话题消息不被受理", act1 == "ignored", act1)
        check("未 @ 的话题消息不产生 router 事件",
              len([e for e in _router_events() if e.get("kind") == "user.message"]) == 0,
              str([e.get("kind") for e in _router_events()]))
        recs0 = [json.loads(l) for l in
                 lb.MESSAGES_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("未 @ 的话题消息按 ignored 记账",
              any(r.get("message_id") == "om_topic_plain" and r.get("action") == "ignored"
                  for r in recs0), str(recs0[-1:]))

        # 场景 2：话题里 @ 了机器人 → 正常受理
        ev_at = dict(ev_plain)
        ev_at["message_id"] = "om_topic_at"
        ev_at["content"] = "@" + cfg3["bot"]["name"] + " 帮我看下这个"
        ev_at["mentions"] = [{"id": cfg3["bot"]["open_id"], "name": cfg3["bot"]["name"]}]
        act2 = lb.process_event(cfg3, st3, ev_at)
        check("@ 了机器人的话题消息正常受理", act2 == "delivered", act2)
        ums = [e for e in _router_events() if e.get("kind") == "user.message"]
        check("受理后只产生一条 router 事件", len(ums) == 1, str(len(ums)))
        src = (ums[0].get("source_ref") or {}) if ums else {}
        check("router 事件的 source_ref 里有话题 id",
              src.get("thread_id") == "omt_topic" and src.get("root_id") == "om_root",
              str(src))

        # 场景 3：收件箱记录里也能看到话题 id（看板可归位）
        recs = [json.loads(l) for l in
                lb.MESSAGES_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        at_rec = [r for r in recs if r.get("message_id") == "om_topic_at"]
        check("收件箱记录里有话题 id",
              bool(at_rec) and at_rec[0].get("thread_id") == "omt_topic", str(at_rec[:1]))

        # 场景 4：新话题已登记进轮询侧话题表（降级轮询时能收到）
        th = st3.get("threads") or {}
        check("新话题已登记进轮询话题表", "omt_topic" in th, str(list(th.keys())))
        check("话题表条目带会话信息",
              bool(th.get("omt_topic", {}).get("chat", {}).get("chat_id") == "oc_t"),
              str(th.get("omt_topic")))

        # 场景 5：幂等 —— 同一条消息再投一次不会产生第二条 router 事件
        act3 = lb.process_event(cfg3, st3, ev_at)
        check("同一条话题消息重投被去重", act3 == "dup", act3)
        check("重投后仍只有一条 router 事件",
              len([e for e in _router_events() if e.get("kind") == "user.message"]) == 1)

        # 场景 6：降级轮询按 root/reply 关系动态登记话题
        bs3 = (pathlib.Path(__file__).resolve().parent / "lark_bridge.py").read_text(encoding="utf-8")
        check("轮询侧按 root/reply 关系反查话题", "thread_index" in bs3 and "root_id" in bs3)
        check("话题表在事件路径与轮询路径间共用", 'state.setdefault("threads", {})' in bs3)
        tindex = {"om_root_1": "omt_topic"}
        check("带 thread_id 的消息直接解析出话题",
              lb.discover_thread("omt_x", "", tindex) == "omt_x")
        check("只带 root_id 的追加消息能反查回话题",
              lb.discover_thread("", "om_root_1", tindex) == "omt_topic")
        check("查不到的话题返回空串",
              lb.discover_thread("", "om_unknown", tindex) == "")

        # 场景 7：话题消息建出的任务，source_ref 里也带话题 id
        rf = sa.ROUTER_DIR / "round.json"
        rf.parent.mkdir(parents=True, exist_ok=True)
        rf.write_text(json.dumps({
            "task": "", "trigger_id": "trig-topic", "kind": "user.message",
            "source_ref": {"chat_id": "oc_t", "message_id": "om_topic_at",
                           "thread_id": "omt_topic", "root_id": "om_root"},
            "text": "帮我看下这个", "ts": "2026-11-18T11:00:00+08:00"},
            ensure_ascii=False), encoding="utf-8")
        res_topic = sa.apply_decision("", {
            "schemaVersion": 2, "triggerId": "trig-topic",
            "items": [{"triggerId": "trig-topic", "taskAction": "create",
                       "title": "话题任务", "agentId": "explore", "goal": "验证话题 id",
                       "requirements": [], "acceptance": [], "confidence": 0.9,
                       "reason": "调查", "rationale": "自检"}],
            "reviews": [], "outbox": []}, actor="router")
        made = [a for a in res_topic["applied"] if a.get("kind") == "task.create"]
        st_ref = (sa.load_state(made[0]["task"]).get("source_ref") or {}) if made else {}
        check("按话题消息新建的任务 source_ref 带 thread_id",
              st_ref.get("thread_id") == "omt_topic" and st_ref.get("root_id") == "om_root",
              str(st_ref))
        lb.MESSAGES_FILE = pathlib.Path(__file__).resolve().parent / "inbox" / "messages.jsonl"
        lb.LEDGER_FILE = pathlib.Path(__file__).resolve().parent / "inbox" / "handled.jsonl"
        lb.LOG_FILE = pathlib.Path(__file__).resolve().parent / "logs" / "lark-bridge.log"

        # ────────────────────────────────────────────────────────────
        print("\n[23] 沙箱硬隔离：SA_ROOT 指到临时目录时不碰生产状态")
        # 这一节用**子进程 + 环境变量**验，而不是 monkeypatch ——
        # 事故原因正是「只有 monkeypatch 的人知道要隔离，import 一次就写了生产」。
        import hashlib
        import os as _os
        import subprocess
        boot = pathlib.Path(__file__).resolve().parent
        prod_inbox = boot / "router" / "inbox.jsonl"

        def _fingerprint(p):
            if not p.exists():
                return (0, "")
            data = p.read_bytes()
            return (len(data), hashlib.sha256(data).hexdigest())

        prod_before = _fingerprint(prod_inbox)
        sandbox = tmp / "sandbox_root"
        probe = tmp / "sandbox_probe"
        probe_code = (
            "import json, sa, core\n"
            "print(json.dumps({'router_inbox': str(sa.ROUTER_INBOX),\n"
            "                  'outbox': str(core.OUTBOX_DIR),\n"
            "                  'tasks': str(sa.TASKS_DIR),\n"
            "                  'ledger': str(sa.LEDGER_FILE),\n"
            "                  'sandbox': sa.SANDBOX_MODE}))\n"
            "sa.notify('user.message', None, text='sandbox-probe-只应在沙箱里',\n"
            "          source='feishu',\n"
            "          source_ref={'chat_id': 'oc_' + '0' * 32, 'message_id': 'om_sandbox_probe'})\n"
        )
        env = {k: v for k, v in _os.environ.items()
               if not k.startswith("SA_") and k not in ("SA_ROOT",)}
        env["SA_ROOT"] = str(sandbox)
        proc = subprocess.run([sys.executable, "-c", probe_code], cwd=str(boot),
                              env=env, capture_output=True, text=True)
        check("沙箱子进程正常退出", proc.returncode == 0, (proc.stderr or "")[-300:])
        try:
            paths = json.loads((proc.stdout or "{}").strip().splitlines()[-1])
        except Exception:
            paths = {}
        # macOS 的 /var 会解析成 /private/var：两边都 resolve 后再比前缀
        sandbox_real = str(sandbox.resolve())

        def _under(p: str, base: str) -> bool:
            try:
                return str(pathlib.Path(p).resolve()).startswith(str(pathlib.Path(base).resolve()))
            except OSError:
                return False

        check("SA_ROOT 生效：router 收件箱落在沙箱内",
              _under(paths.get("router_inbox", ""), sandbox), str(paths.get("router_inbox")))
        check("SA_ROOT 生效：outbox 目录落在沙箱内",
              _under(paths.get("outbox", ""), sandbox), str(paths.get("outbox")))
        check("SA_ROOT 生效：tasks 目录落在沙箱内",
              _under(paths.get("tasks", ""), sandbox), str(paths.get("tasks")))
        check("SA_ROOT 生效：永久账本落在沙箱内",
              _under(paths.get("ledger", ""), sandbox), str(paths.get("ledger")))
        check("沙箱模式被显式标注", bool(paths.get("sandbox")) is True, str(paths.get("sandbox")))
        check("notify 打印了实际目标路径并标注沙箱模式",
              sandbox_real in (proc.stderr or "")
              and "沙箱模式" in (proc.stderr or ""), (proc.stderr or "")[:300])
        check("事件真的写进了沙箱收件箱",
              "sandbox-probe" in (sandbox / "router" / "inbox.jsonl").read_text(encoding="utf-8")
              if (sandbox / "router" / "inbox.jsonl").exists() else False)
        prod_after = _fingerprint(prod_inbox)
        check("生产 router/inbox.jsonl 字节数与内容完全不变",
              prod_before == prod_after, f"{prod_before[0]} → {prod_after[0]}")

        # ────────────────────────────────────────────────────────────
        print("\n[24] 来源闸门：不可核验的 user.message 不建任务")
        import router as rt
        rt.LEDGER_FILE = tmp / "gate_ledger.jsonl"
        rt.LEDGER_FILE.write_text(json.dumps(
            {"message_id": "om_gate_ok", "task": "T1", "action": "delivered"},
            ensure_ascii=False) + "\n", encoding="utf-8")
        rt.UNVERIFIED_FILE = tmp / "gate_unverified.jsonl"
        rt.DIGEST_FILE = tmp / "gate_digest.md"
        good_ref = {"chat_id": "oc_" + "a" * 32, "message_id": "om_gate_ok",
                    "chat_name": "真实群", "chat_name_source": "feishu"}
        bad_ref = {"chat_id": "oc_x", "message_id": "om_evt_1",
                   "chat_name": "群X", "chat_name_source": "unknown"}

        def _ev(kind, source, ref):
            return {"id": "trig-" + (ref.get("message_id") or "x")[-6:],
                    "kind": kind, "source": source, "text": "测试指令",
                    "source_ref": dict(ref)}

        ok1, why1 = rt.verify_user_event(_ev("user.message", "feishu", good_ref))
        check("形态合法 + 账本命中 → 放行", ok1, why1)
        ok2, why2 = rt.verify_user_event(_ev("user.message", "feishu", bad_ref))
        check("chat_id 形态非法 → 拦下", not ok2 and "chat_id" in why2, why2)
        bad_mid = dict(good_ref)
        bad_mid["message_id"] = "evt_1"
        ok3, why3 = rt.verify_user_event(_ev("user.message", "feishu", bad_mid))
        check("message_id 不以 om_ 开头 → 拦下", not ok3 and "om_" in why3, why3)
        orphan = dict(good_ref)
        orphan["message_id"] = "om_not_in_ledger"
        ok4, why4 = rt.verify_user_event(_ev("user.message", "feishu", orphan))
        check("形态合法但账本里查不到 → 拦下", not ok4 and "账本" in why4, why4)
        ok5, _ = rt.verify_user_event(_ev("user.message", "manual", bad_ref))
        check("非飞书来源的人工事件不受闸门影响", ok5)
        ok6, _ = rt.verify_user_event({"id": "t", "kind": "task.created", "source": "router"})
        check("其它事件类型不受闸门影响", ok6)
        rt.reject_event(_ev("user.message", "feishu", bad_ref), why2)
        unv = [json.loads(l) for l in
               rt.UNVERIFIED_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("被拦下的事件记进 unverified.jsonl",
              any(u.get("status") == "unverified" and u.get("need_manual") for u in unv),
              str(unv[:1]))
        check("unverified 记录带失败原因",
              bool(unv) and bool(unv[0].get("reason")), str(unv[:1]))
        check("摘要里留了「来源不可核验」一行",
              "来源不可核验" in rt.DIGEST_FILE.read_text(encoding="utf-8"))
        check("事件循环真的接了这道闸（不是只写了函数）",
              "verify_user_event(ev, ledger_wait_seconds=" in (boot / "router.py").read_text(encoding="utf-8"))
        check("群名带可信度：feishu 才展示群名",
              "真实群" in rt.display_chat_name(good_ref))
        check("群名带可信度：未标注来源不冒充群名",
              "群X" not in rt.display_chat_name(
                  {"chat_id": "oc_x", "chat_name": "群X", "chat_name_source": "unknown"}))
        check("桥接在 source_ref 里写 chat_name_source",
              "chat_name_source" in (boot / "lark_bridge.py").read_text(encoding="utf-8"))

        # ────────────────────────────────────────────────────────────
        print("\n[25] outbox：succeeded 必须有真实渠道返回与凭据")
        core.OUTBOX_DIR = tmp / "outbox_cred"
        rec_c = core.add_outbox(target="thread", purpose="result", text="凭据测试",
                                task="", chat_id="oc_" + "b" * 32,
                                message_id="om_deliver_me", dedupe_window_seconds=0)
        orig_lb_log = lb.LOG_FILE
        lb.LOG_FILE = tmp / "bridge_cred.log"
        orig_send_reply = lb.send_reply
        lb.send_reply = lambda mid, text, in_thread=None, **kw: None  # 桩：只 append，不返回
        lb.deliver_outbox()
        rec_c = [r for r in core.list_outbox() if r["id"] == rec_c["id"]][0]
        check("桩返回 None → 不写 succeeded", rec_c["status"] != "succeeded", rec_c["status"])
        check("桩返回 None → 记了失败原因", bool(rec_c.get("last_error")),
              str(rec_c.get("last_error"))[:120])
        lb.send_reply = lambda mid, text, in_thread=None, **kw: {
            "ok": True, "data": {"message_id": "om_new_receipt"}}
        core.update_outbox(rec_c, status="pending", attempts=0, last_error="")
        lb.deliver_outbox()
        rec_c = [r for r in core.list_outbox() if r["id"] == rec_c["id"]][0]
        check("真实渠道返回 ok → 写 succeeded", rec_c["status"] == "succeeded", rec_c["status"])
        check("succeeded 记了投递凭据（渠道返回的 message_id）",
              (rec_c.get("delivery") or {}).get("message_id") == "om_new_receipt",
              str(rec_c.get("delivery")))
        check("投递凭据标为可核验",
              (rec_c.get("delivery") or {}).get("verified") is True)
        check("桥接日志里打了凭据",
              "om_new_receipt" in lb.LOG_FILE.read_text(encoding="utf-8"))
        lb.send_reply = orig_send_reply
        lb.LOG_FILE = orig_lb_log

        # 受理回执（ack）改成「只发表情、不发文字」
        rec_ack = core.add_outbox(target="thread", purpose="ack", text="已受理：任务号 T999",
                                  task="", chat_id="oc_" + "c" * 32,
                                  message_id="om_ack_me", dedupe_window_seconds=0)
        sent_calls = []
        lb.send_reply = lambda mid, text, in_thread=None, **kw: sent_calls.append(text) or {
            "ok": True, "data": {"message_id": "om_should_not_happen"}}
        lb.deliver_outbox()
        lb.send_reply = orig_send_reply
        rec_ack = [r for r in core.list_outbox() if r["id"] == rec_ack["id"]][0]
        check("受理回执 ack → 状态 suppressed（不发文字）",
              rec_ack["status"] == "suppressed", rec_ack["status"])
        check("受理回执 ack → 渠道没被调用", not sent_calls, str(sent_calls))

        # 对账：造一条「succeeded 但无凭据」的历史记录（不改原记录，只在临时目录里验）
        legacy = core.add_outbox(target="thread", purpose="result", text="历史无凭据",
                                 task="", chat_id="oc_x", message_id="om_legacy",
                                 dedupe_window_seconds=0)
        core.update_outbox(legacy, status="succeeded", sent_at="2026-11-18T11:29:08+08:00")
        report_path = tmp / "outbox-verify.json"
        sa.cmd_outbox_verify(_ap.Namespace(log=str(tmp / "bridge_cred.log"),
                                            out=str(report_path), json=False))
        rep = json.loads(report_path.read_text(encoding="utf-8"))
        flagged = {r["id"]: r for r in rep["unverified"]}
        check("对账：无凭据的 succeeded 被标 unverified",
              legacy["id"] in flagged, str(list(flagged)))
        check("对账：无凭据条目原因是「无投递凭据」",
              any("无投递凭据" in x for x in flagged.get(legacy["id"], {}).get("reasons", [])),
              str(flagged.get(legacy["id"])))
        check("对账：有凭据的 succeeded 不被误标",
              rec_c["id"] not in flagged, str(flagged.get(rec_c["id"])))
        check("对账不改写历史记录（原记录状态仍是 succeeded）",
              [r for r in core.list_outbox() if r["id"] == legacy["id"]][0]["status"]
              == "succeeded")
        core.OUTBOX_DIR = tmp / "outbox"

        # ────────────────────────────────────────────────
        print("\n[26] 可观测性：不受理消息留痕 + status 的 pid 指向守护进程")
        import os as _os2
        import subprocess as _sp
        boot2 = pathlib.Path(__file__).resolve().parent
        orig_lb_log = lb.LOG_FILE
        orig_lb_msgs = lb.MESSAGES_FILE
        orig_lb_ledger = lb.LEDGER_FILE
        lb.LOG_FILE = tmp / "ignored_bridge.log"
        lb.MESSAGES_FILE = tmp / "ignored_messages.jsonl"
        lb.LEDGER_FILE = tmp / "ignored_ledger.jsonl"
        cfg4 = json.loads((boot2 / "lark_config.json").read_text(encoding="utf-8"))
        cfg4["allow_any_sender"] = False
        cfg4["senders"] = [{"name": "白名单内", "open_id": "ou_allowed"}]
        real_chat = "oc_" + "c" * 32
        base_ev = {"message_id": "om_ignored_aaaa1111", "chat_id": real_chat,
                   "sender_id": "ou_allowed", "sender_type": "user",
                   "content": "没 @ 机器人的群聊", "chat_type": "group"}
        st4 = {"seen": {}}
        check("未 @ 的群消息仍不受理（行为不变）",
              lb.process_event(cfg4, st4, base_ev) == "ignored")
        ev_stranger = dict(base_ev, message_id="om_ignored_bbbb2222", sender_id="ou_stranger")
        check("白名单外的发送者仍不受理（行为不变）",
              lb.process_event(cfg4, st4, ev_stranger) == "not_allowed")
        ev_empty = dict(base_ev, message_id="om_ignored_cccc3333", content="")
        check("空正文（形态异常）仍不受理（行为不变）",
              lb.process_event(cfg4, st4, ev_empty) == "empty")
        ev_bad_chat = dict(base_ev, message_id="om_ignored_dddd4444", chat_id="oc_short")
        lb.process_event(cfg4, st4, ev_bad_chat)
        log_txt = lb.LOG_FILE.read_text(encoding="utf-8")
        check("不受理的消息在日志里有留痕（不再全静默）",
              log_txt.count("不受理：") == 4, str(log_txt.count("不受理：")))
        check("留痕含 message_id 前 8 位", "om_ignor…" in log_txt)
        check("留痕含判定原因（未 @ / 白名单 / 形态异常）",
              "未 @ 机器人" in log_txt and "白名单" in log_txt and "形态异常" in log_txt)
        check("留痕含会话 id 形态", "oc_+32位hex" in log_txt and "oc_+5位非规范" in log_txt)
        recs_ig = [json.loads(x) for x in
                   lb.MESSAGES_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
        check("日志与 ignored 记账一致（同一条消息记 ignored）",
              any(r.get("message_id") == "om_ignored_aaaa1111" and r.get("action") == "ignored"
                  for r in recs_ig), str(recs_ig[:1]))
        # 同一条消息再走一遍（轮询每轮都会重看）：不重复刷日志
        lb.process_event(cfg4, st4, dict(base_ev, message_id="om_ignored_eeee5555"))
        lb.process_event(cfg4, st4, ev_bad_chat)
        check("同一进程内同一条消息不重复刷日志",
              lb.LOG_FILE.read_text(encoding="utf-8").count("不受理：") == 5,
              str(lb.LOG_FILE.read_text(encoding="utf-8").count("不受理：")))

        # status 的 pid：必须来自 pid 文件，不是查询进程自己的 pid
        pid_root = tmp / "pidprobe"
        pid_root.mkdir(parents=True, exist_ok=True)
        pid_file = pid_root / ".lark-bridge.pid"
        env_pid = {k: v for k, v in _os2.environ.items() if not k.startswith("SA_")}
        env_pid["SA_ROOT"] = str(pid_root)

        def _status():
            r = _sp.run([sys.executable, str(boot2 / "lark_bridge.py"), "status"],
                        cwd=str(boot2), env=env_pid, capture_output=True, text=True)
            if r.returncode != 0:
                return {}, r.stderr
            try:
                return json.loads(r.stdout or "{}"), ""
            except json.JSONDecodeError:
                return {}, (r.stdout or "")[-200:]

        pid_file.write_text(str(_os2.getpid()), encoding="utf-8")   # 活着，但不是桥接
        sj, err = _status()
        check("status 可执行", bool(sj), err[-300:])
        check("status 的 pid 取自 pid 文件（= 守护进程 pid）",
              sj.get("pid") == _os2.getpid(), str(sj.get("pid")))
        check("status 的 pid 不是查询进程自己的 pid",
              sj.get("pid") != sj.get("status_pid"),
              f"pid={sj.get('pid')} status_pid={sj.get('status_pid')}")
        check("status 标了 pid 来源与 pid 文件路径",
              sj.get("pid_source") == "pid_file" and str(sj.get("pid_file", "")).endswith(".lark-bridge.pid"))
        check("存活校验：活着的 pid → daemon_alive=true", sj.get("daemon_alive") is True)
        check("pid 复用防护：命令行不是桥接 → pid_matches_bridge=false",
              sj.get("pid_matches_bridge") is False, str(sj.get("pid_matches_bridge")))
        check("pid 复用时不判为运行中", sj.get("running") is False)
        pid_file.write_text("4194305", encoding="utf-8")             # 几乎不可能存在的 pid
        sj2, _ = _status()
        check("pid 文件指向不存在的进程 → daemon_alive=false 且 running=false",
              sj2.get("daemon_alive") is False and sj2.get("running") is False,
              str({k: sj2.get(k) for k in ("daemon_alive", "running")}))
        pid_file.unlink()
        sj3, _ = _status()
        check("pid 文件缺失 → pid=null，不谎报存活",
              sj3.get("pid") is None and sj3.get("running") is False, str(sj3.get("pid")))
        check("lark_bridge.sh status 仍可用（pid 文件路径未变）",
              "/tmp/super-agent-lark-bridge.pid" in (boot2 / "lark_bridge.sh").read_text(encoding="utf-8"))
        lb.LOG_FILE = orig_lb_log
        lb.MESSAGES_FILE = orig_lb_msgs
        lb.LEDGER_FILE = orig_lb_ledger

        # ────────────────────────────────────────────────
        print("\n[26b] 指派结束的轮次：链式派工受连续上限约束")
        tidc = sa.cmd_new_task(_ap.Namespace(
            id=None, title="链式派工护栏", goal="验证完成轮不能再派工",
            context=None, context_file=None, acceptance="", source="manual",
            source_ref=None, by="human", allow_new_task=False))
        sa.cmd_assign(_ap.Namespace(task=tidc, worker="explore",
                                    instruction_file=str(tmp / "instr.md"), brief="b"))
        stc = sa.load_state(tidc)
        aidc = stc["assignments"][0]["id"]
        # 模拟「本轮是 assignment.completed」的 Decision，里面夹一条派工
        doc_dispatch = {
            "schemaVersion": 2, "triggerId": "trig-done",
            "_round_kind": "assignment.completed",
            "items": [{"triggerId": "trig-done", "taskAction": "continue",
                       "taskId": tidc, "agentId": "plan", "goal": "接着派给另一个 worker",
                       "requirements": [], "acceptance": [], "confidence": 0.9,
                       "reason": "调查", "rationale": "链式"}],
            "reviews": [], "outbox": [],
        }
        # 链式派工开着（默认上限 2）→ 完成轮里的派工**允许**，但连续次数有上限
        sa.save_chain_config(True, 2)
        resc = sa.apply_decision(tidc, doc_dispatch, actor="router")
        check("完成轮里的派工被允许（链式派工开启）",
              not any(r["reason_code"] == "dispatch_in_assignment_round"
                      for r in resc["rejected"]), str(resc["rejected"]))
        check("确实派出了新指派",
              len(sa.load_state(tidc)["assignments"]) == 2,
              str(len(sa.load_state(tidc)["assignments"])))
        check("链式计数记上了", sa.load_state(tidc).get("chain_dispatch_count") == 1,
              str(sa.load_state(tidc).get("chain_dispatch_count")))
        # 连续第 2 次 → 仍然允许
        doc_dispatch2 = dict(doc_dispatch)
        doc_dispatch2["triggerId"] = "trig-done-b"
        doc_dispatch2["items"] = [dict(doc_dispatch["items"][0], triggerId="trig-done-b")]
        resc_b = sa.apply_decision(tidc, doc_dispatch2, actor="router")
        check("连续第 2 次链式派工被允许",
              len(sa.load_state(tidc)["assignments"]) == 3,
              str(len(sa.load_state(tidc)["assignments"])))
        # 连续第 3 次 → 超过上限，拒绝
        doc_dispatch3 = dict(doc_dispatch)
        doc_dispatch3["triggerId"] = "trig-done-c"
        doc_dispatch3["items"] = [dict(doc_dispatch["items"][0], triggerId="trig-done-c")]
        resc_c = sa.apply_decision(tidc, doc_dispatch3, actor="router")
        check("连续第 3 次链式派工被拒绝（上限 2）",
              any(r["reason_code"] == "chain_dispatch_limit" for r in resc_c["rejected"]),
              str(resc_c["rejected"]))
        check("被拒后没有多派指派",
              len(sa.load_state(tidc)["assignments"]) == 3,
              str(len(sa.load_state(tidc)["assignments"])))
        # 用户新消息 → 计数清零 → 又能派
        sa.apply_decision(tidc, {
            "schemaVersion": 2, "triggerId": "trig-user-reset",
            "_round_kind": "user.message",
            "items": [{"triggerId": "trig-user-reset", "taskAction": "continue",
                       "taskId": tidc, "agentId": "plan", "goal": "新需求",
                       "requirements": [], "acceptance": [], "confidence": 0.9,
                       "reason": "新需求", "rationale": "新"}],
            "reviews": [], "outbox": []}, actor="router")
        check("用户新消息把链式计数清零",
              sa.load_state(tidc).get("chain_dispatch_count") == 0,
              str(sa.load_state(tidc).get("chain_dispatch_count")))
        # 关掉链式派工 → 回到「一律不允许」
        sa.save_chain_config(False, 2)
        resc_off = sa.apply_decision(tidc, doc_dispatch, actor="router")
        check("关掉链式派工后完成轮里的派工被拒绝",
              any(r["reason_code"] == "dispatch_in_assignment_round"
                  for r in resc_off["rejected"]), str(resc_off["rejected"]))
        sa.save_chain_config(True, 2)
        # 但复核仍然允许
        resc2 = sa.apply_decision(tidc, {
            "schemaVersion": 2, "triggerId": "trig-done2",
            "_round_kind": "assignment.completed",
            "items": [],
            "reviews": [{"assignment": aidc, "verdict": "reject", "note": "重做"}],
            "outbox": [{"target": "thread", "purpose": "reply", "text": "打回了"}]},
            actor="router")
        check("完成轮里 review 与 outbox 仍然生效",
              any(a.get("kind") == "review.reject" for a in resc2["applied"])
              and any(a.get("kind") == "outbox" for a in resc2["applied"]),
              str(resc2["applied"]))
        # 用户消息轮仍可自由派工
        resc3 = sa.apply_decision(tidc, {
            "schemaVersion": 2, "triggerId": "trig-user",
            "_round_kind": "user.message",
            "items": [{"triggerId": "trig-user", "taskAction": "continue",
                       "taskId": tidc, "agentId": "plan", "goal": "用户新指令引发的派工",
                       "requirements": [], "acceptance": [], "confidence": 0.9,
                       "reason": "方案", "rationale": "新消息"}],
            "reviews": [], "outbox": []}, actor="router")
        check("用户消息轮仍可派工",
              any(a.get("kind") == "dispatch" for a in resc3["applied"]),
              str(resc3["applied"]))

        print("\n[26c] 模型切换：运行中生效 + 降级可切回")
        import router as rt2
        mf = tmp / "model.json"
        rt2.MODEL_FILE = mf
        rt2.DEFAULT_MODEL = "test/default-model"
        st_m = rt2.load_model_state()
        check("默认模型有兜底", bool(st_m.get("primary")), str(st_m))
        st_m["primary"] = "p/model-a"
        st_m["boot_primary"] = "p/boot"
        st_m["degraded"] = True
        st_m["degraded_to"] = "p/model-b"
        st_m["degraded_at"] = "2000-01-01T00:00:00+08:00"
        rt2.save_model_state(st_m)
        check("模型状态可持久化", rt2.load_model_state()["primary"] == "p/model-a")
        check("降级状态被记录", rt2.load_model_state()["degraded"] is True)
        src = (pathlib.Path(__file__).resolve().parent / "router.py").read_text(encoding="utf-8")
        check("有 model 子命令（切模型/切回/查状态）", "def cmd_model" in src)
        check("运行中按 model.json 切模型（不用重启）", "def sync_model" in src)
        check("降级有自动切回（cooldown）", "restore_after_seconds" in src)

        print("\n[26d] 事件 prompt 不传上下文")
        import router as rt3
        ev_user = {"kind": "user.message", "id": "trig-x", "task": "",
                   "text": "帮我写个文件", "source_ref": {"chat_name": "群X"}}
        pr = rt3.build_prompt(ev_user)
        check("user.message prompt 只带事件+triggerId+原话",
              pr.strip().splitlines()[0].startswith("[事件]")
              and "triggerId: trig-x" in pr
              and "帮我写个文件" in pr
              and "原话" in pr, pr)
        check("prompt 里没有最近轮次摘要", "最近几轮" not in pr and "digest" not in pr.lower())
        check("prompt 里没有重复的规格说明",
              "判断：" not in pr and "tasks-list" not in pr)
        ev_task = {"kind": "assignment.completed", "id": "trig-y",
                   "task": "T001", "assignment": "A01"}
        pr2 = rt3.build_prompt(ev_task)
        check("任务事件 prompt 只带定位信息",
              "[事件] assignment.completed" in pr2 and "任务: T001 / A01" in pr2
              and "目录:" in pr2, pr2)
        src3 = (pathlib.Path(__file__).resolve().parent / "router.py").read_text(encoding="utf-8")
        check("build_prompt 不再读 digest",
              "read_digest" not in src3[src3.index("def build_prompt"):src3.index("# ------------------------------------------------------------------ pi RPC")])

        print("\n[26e] 看板：模型切换与 session 轮转")
        web = (pathlib.Path(__file__).resolve().parent / "web" / "index.html").read_text(encoding="utf-8")
        check("看板有模型下拉与切换按钮", "modelSel" in web and "setRouterModel" in web)
        check("看板有切回默认按钮", "切回默认" in web and "restore:true" in web)
        check("看板有 session 轮转按钮", "轮转 Session" in web and "rotateRouterSession" in web)
        srv = (pathlib.Path(__file__).resolve().parent / "serve.py").read_text(encoding="utf-8")
        check("有 /api/router/model 接口", "/api/router/model" in srv)
        check("有 /api/router/rotate 接口", "/api/router/rotate" in srv)
        check("router 面板展示降级状态", "degraded_to" in web)

        print("\n[26f] router session 命名带开启时间")
        src6 = (pathlib.Path(__file__).resolve().parent / "router.py").read_text(encoding="utf-8")
        check("session 名带启动时间", "Super Agent Router {" in src6)
        check("有历史会话名回填命令", "def cmd_rename_sessions" in src6)
        check("回填命令按文件名时间戳推本地时间", "rename-sessions" in src6)

        print("\n[26f2] router session id 唯一化（super-agent-router-<创建时间>）")
        import re as _re
        import router as _router
        # 状态文件指到沙箱，别碰生产 router/session_id.json
        _router.ROUTER_DIR = sa.ROUTER_DIR
        _router.SESSION_ID_FILE = sa.ROUTER_DIR / "session_id.json"
        check("session id 前缀常量存在", 'SESSION_ID_PREFIX = "super-agent-router"' in src6)
        check("id 带创建时间戳", "def make_session_id" in src6
              and 'SESSION_ID_PREFIX}-{stamp}' in src6)
        check("有持久化的 session 状态文件", 'SESSION_ID_FILE = ROUTER_DIR / "session_id.json"' in src6)
        check("状态先落盘再归档（崩了也不会指向已归档的 id）",
              src6.index("save_session_state({\n        \"session_id\": new_sid")
              < src6.index("shutil.move(str(f), str(arch / f.name))"))
        check("轮转会换新 id", "make_session_id(created)" in src6)
        check("新 id 格式正确", bool(_re.fullmatch(
            r"super-agent-router-\d{8}-\d{6}(-\d{2})?",
            _router.make_session_id("2026-11-20T17:12:00+08:00"))),
            _router.make_session_id("2026-11-20T17:12:00+08:00"))
        # 同秒内连着造两个 id 不能撞（pi 按 id 反查，撞了会复现错的那个会话）
        _sid_a = _router.make_session_id("2026-11-20T17:12:00+08:00")
        _router.save_session_state({"session_id": _sid_a, "created_at": "2026-11-20T17:12:00+08:00"})
        _sid_b = _router.make_session_id("2026-11-20T17:12:00+08:00")
        check("同秒内连造两个 id 不撞", _sid_a != _sid_b, f"{_sid_a} vs {_sid_b}")
        _router.save_session_state({"session_id": "super-agent-router-test",
                                    "created_at": "2026-11-20T17:12:00+08:00"})
        _st = _router.current_session_state()
        check("状态文件读回当前 id", _st.get("session_id") == "super-agent-router-test")
        check("状态文件带创建时间与显示名",
              bool(_st.get("created_at")) and str(_st.get("name")).startswith("Super Agent Router "))
        cv = (pathlib.Path(__file__).resolve().parent / "chain_view.py").read_text(encoding="utf-8")
        check("看板找 router 会话时排除归档目录", '"archive" not in p.parts' in cv)
        check("看板不再用旧的后缀匹配认会话", '"_super-agent-router.jsonl"' not in cv)

        print("\n[26f3] Decision 只带目标：做法归 worker")
        rs = (pathlib.Path(__file__).resolve().parent / "skill" / "router-system.md").read_text(encoding="utf-8")
        check("契约里不再出现 requirements 字段", "requirements" not in rs)
        check("契约里不再出现 acceptance 字段", "acceptance" not in rs)
        check("契约说清派工描述只有 goal", "只有 `goal`" in rs or "只有 goal" in rs)
        check("契约不再教具体业务怎么做",
              all(w not in rs for w in ("早读", "早报", "T1 ", "T2 ", "T3 ", "ROLE.md")))
        check("契约保留 worker 职责范围的选择规则", "职责范围" in rs)
        check("契约指明名册在系统提示里", "名册" in rs and "registry.json" in rs)
        _wb = _router.render_worker_brief()
        _reg = json.loads((pathlib.Path(__file__).resolve().parent / "registry.json").read_text(encoding="utf-8"))
        _enabled = [w["agent_id"] for w in _reg.get("workers") or [] if w.get("enabled")]
        check("名册列出全部在册 worker",
              all(f"`{a}`" in _wb for a in _enabled), _wb[:200])
        check("名册写明只带目标不带做法", "不带做法" in _wb)
        check("名册带触发闸门信息", "触发闸门" in _wb)
        check("名册标注已停用 worker", "已停用" in _wb)
        check("名册只讲职责范围，不讲做法",
              not any(w in _wb for w in ("ROLE.md", "先读", "自动同意", "Markdown",
                                        "窗口", "T1", "T2", "T3")))
        src7b = (pathlib.Path(__file__).resolve().parent / "router.py").read_text(encoding="utf-8")
        check("router 启动时把名册注入 system prompt",
              "write_worker_brief()" in src7b
              and src7b.count('"--append-system-prompt"') >= 2)

        print("\n[26g] 看板可读性优化")
        web = (pathlib.Path(__file__).resolve().parent / "web" / "index.html").read_text(encoding="utf-8")
        check("任务按最近活动倒序", "updated_at||''" in web and "localeCompare" in web)
        check("有「待我处理」快捷筛选", 'data-f="mine"' in web and "function needsMe" in web)
        check("时间统一格式化（兼容毫秒时间戳）", "function fmtTime" in web and "/^\\d{13}$/" in web)
        check("长文折叠可展开", "function longText" in web and "data-lt=" in web)
        check("侧栏 sticky，滚动时任务列表常驻", "position:sticky" in web)
        check("自动刷新不再重置筛选", "S.filter='all'" not in web)
        srv2 = (pathlib.Path(__file__).resolve().parent / "serve.py").read_text(encoding="utf-8")
        check("看板自登记 pid（不再显示「已停止」）", "super-agent-board-" in srv2)
        check("待处理数按 processed.json 算（不再是假数）",
              "processed.json" in srv2 and "pfile" in srv2)
        check("长列表有上限，不再全量渲染", "MSG_CAP" in web and "OB_CAP" in web and "DEC_CAP" in web)
        check("Outbox 失败项置顶并标数量", "obBad" in web and "条待处理" in web)
        check("收件箱整行铺满", 'style="grid-column:1/-1"' in web)
        check("KPI 固定 3 列成整齐网格", "repeat(3,1fr)" in web)

        print("\n[26g] 飞书表情：接收/分配/完成")
        src7 = (pathlib.Path(__file__).resolve().parent / "lark_bridge.py").read_text(encoding="utf-8")
        check("表情映射是 敲键盘/思考中(气泡三点)/完成",
              '"received": "Typing"' in src7
              and '"dispatched": "Status_PrivateMessage"' in src7
              and '"completed": "DONE"' in src7)
        check("表情是替换不是叠加（会删掉旧表情）",
              "def delete_reaction" in src7 and "delete_reaction(message_id" in src7)
        check("完成是终态不降级", '"DONE"' in src7 and "不再降级" in src7)
        check("有表情 API 调用", "reactions" in src7 and "reaction_type" in src7)
        check("收到任务在消息投递时就挂表情", "react_async(msg_id, rc[\"received\"])" in src7)
        check("有按指派状态补表情的扫描", "def sync_reactions" in src7)
        check("受理回执 ack 被拦成 suppressed", "suppress_ack_text" in src7 and "suppressed" in src7)
        check("ack 文字不再发", "ack.on_receive" in src7 or 'ack.get("on_receive"' in src7)
        cfg7 = json.loads((pathlib.Path(__file__).resolve().parent / "lark_config.json").read_text(encoding="utf-8"))
        check("配置里表情三态正确",
              cfg7.get("reactions", {}).get("received") == "Typing"
              and cfg7.get("reactions", {}).get("dispatched") == "Status_PrivateMessage"
              and cfg7.get("reactions", {}).get("completed") == "DONE",
              json.dumps(cfg7.get("reactions"), ensure_ascii=False))
        check("配置里收到不回文字", cfg7.get("ack", {}).get("on_receive") is False)
        src7b = (pathlib.Path(__file__).resolve().parent / "sa.py").read_text(encoding="utf-8")
        check("指派记录触发它的那条用户消息", "origin_ref" in src7b)

        print("\n[26g-2] 没有派单 → 表情直接标完成（不一直挂着敲键盘）")
        import lark_bridge as lb2
        import datetime as _dt2
        lb_state_file, lb_log_file = lb2.STATE_FILE, lb2.LOG_FILE
        lb_lock_file = lb2.LOCK_FILE
        lb2.STATE_FILE = tmp / "nodispatch_state.json"
        # 锁文件也要指到沙箱：否则会和生产桥接的锁抢（事件线程正在处理消息时锁被占着，
        # 非阻塞拿不到锁 → 表情同步直接跳过 → 用例随机失败）
        lb2.LOCK_FILE = tmp / "nodispatch.lock"
        lb2.LOG_FILE = tmp / "nodispatch_bridge.log"
        _calls = []
        _orig_react = lb2.react
        lb2.react = lambda mid, emoji: (_calls.append((mid, emoji)), True)[1]
        try:
            def _reply(mid, trig, offset=-120):
                ev = {"id": trig, "kind": "user.message",
                      "source_ref": {"message_id": mid, "chat_id": "oc_" + "a" * 32,
                                     "chat_type": "p2p"}}
                ts = _dt2.datetime.now().astimezone() + _dt2.timedelta(seconds=offset)
                p = sa.ROUTER_DIR / "outbox.jsonl"
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": ts.isoformat(timespec="seconds"),
                                        "event": ev, "router": "收到"}, ensure_ascii=False) + "\n")

            def _decision(trig, items):
                p = sa.ROUTER_DIR / "decisions" / f"{trig}.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"submitted": {"triggerId": trig, "items": items}},
                                        ensure_ascii=False), encoding="utf-8")

            _reply("om_nd_1", "trig_nd_1")
            lb2.sync_no_dispatch_reactions()
            check("没有决策文件（router 决定不派工）→ 标完成",
                  _calls == [("om_nd_1", "DONE")], str(_calls))
            _calls.clear()

            _reply("om_nd_2", "trig_nd_2")
            _decision("trig_nd_2", [{"taskAction": "create", "agentId": "general-purpose"}])
            lb2.sync_no_dispatch_reactions()
            check("决策里有派工条目 → 不标完成", _calls == [], str(_calls))
            _calls.clear()

            _reply("om_nd_3", "trig_nd_3")
            _decision("trig_nd_3", [])
            lb2.sync_no_dispatch_reactions()
            check("决策派工为空 → 标完成", _calls == [("om_nd_3", "DONE")], str(_calls))
            _calls.clear()

            lb2.sync_no_dispatch_reactions()
            check("同一消息不重复标（幂等）", _calls == [], str(_calls))

            _reply("om_nd_4", "trig_nd_4", offset=-5)
            lb2.sync_no_dispatch_reactions()
            check("宽限期内不标（等 Decision 落地）", _calls == [], str(_calls))
            _calls.clear()

            _reply("om_nd_5", "trig_nd_5")
            _td = sa.TASKS_DIR / "T901"
            (_td / "context").mkdir(parents=True, exist_ok=True)
            (_td / "state.json").write_text(json.dumps({
                "task_id": "T901", "title": "t", "goal": "g", "status": "open",
                "source_ref": {"message_id": "om_nd_5"},
                "assignments": [{"id": "A01", "worker": "general-purpose", "status": "assigned"}],
            }, ensure_ascii=False), encoding="utf-8")
            lb2.sync_no_dispatch_reactions()
            check("已经有指派落到这条消息 → 不标完成", _calls == [], str(_calls))
        finally:
            lb2.react = _orig_react
            lb2.STATE_FILE, lb2.LOG_FILE = lb_state_file, lb_log_file
            lb2.LOCK_FILE = lb_lock_file

        print("\n[26g-3] 批次：同批结果合并成一条 + 表情去重 + 完成语义")
        import lark_bridge as lb3
        # ── 批次：同一条用户消息派出的多条指派 = 同一批 ──
        _bt = sa.TASKS_DIR / "T910"
        (_bt / "context").mkdir(parents=True, exist_ok=True)
        (_bt / "state.json").write_text(json.dumps({
            "task_id": "T910", "title": "批次任务", "goal": "g", "status": "dispatched",
            "created_at": "2026-11-19T15:00:00+08:00", "updated_at": "2026-11-19T15:00:00+08:00",
            "source_ref": {"message_id": "om_batch_1"},
            "assignments": [
                {"id": "A01", "worker": "general-purpose", "status": "approved",
                 "batch_id": "om_batch_1", "artifacts": ["/tmp/a.md"], "review": "approved"},
                {"id": "A02", "worker": "general-purpose", "status": "running",
                 "batch_id": "om_batch_1", "artifacts": []},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        bst = sa.batch_status("om_batch_1")
        check("批次能查到指派", bst.get("total") == 2, str(bst))
        check("还有在跑的 → 批次未完", bst.get("complete") is False and bst.get("in_flight") == 1,
              str(bst))
        _bt2 = sa.TASKS_DIR / "T911"
        (_bt2 / "context").mkdir(parents=True, exist_ok=True)
        (_bt2 / "state.json").write_text(json.dumps({
            "task_id": "T911", "title": "兄弟任务", "goal": "g", "status": "dispatched",
            "created_at": "2026-11-19T15:00:00+08:00", "updated_at": "2026-11-19T15:00:00+08:00",
            "source_ref": {"message_id": "om_batch_1"},
            "assignments": [{"id": "A01", "worker": "general-purpose", "status": "approved",
                             "batch_id": "om_batch_1", "artifacts": ["/tmp/b.md"],
                             "review": "approved"}],
        }, ensure_ascii=False), encoding="utf-8")
        bst2 = sa.batch_status("om_batch_1")
        check("跨任务的同批指派都算一批", bst2.get("total") == 3, str(bst2))

        # ── 真实路径：Decision → 指派带 batch_id → 结果消息挂 batch ──
        _orig_dir0 = core.OUTBOX_DIR
        core.OUTBOX_DIR = tmp / "outbox_dec"
        try:
            _round = sa.ROUTER_DIR / "round.json"
            _round.parent.mkdir(parents=True, exist_ok=True)
            _round.write_text(json.dumps({
                "kind": "user.message", "task": "",
                "source_ref": {"message_id": "om_batch_2", "chat_id": "oc_" + "c" * 32},
            }, ensure_ascii=False), encoding="utf-8")
            _doc_dispatch = {
                "schemaVersion": 2, "triggerId": "trig-batch-dispatch",
                "_round_kind": "user.message",
                "items": [{"triggerId": "trig-batch-dispatch", "taskAction": "create",
                           "taskId": "", "agentId": "general-purpose",
                           "goal": "批次验证", "requirements": [], "acceptance": ["ok"],
                           "confidence": 0.9, "reason": "实现", "rationale": "测批次"}],
                "reviews": [], "outbox": [],
            }
            _res_d = sa.apply_decision("", _doc_dispatch, actor="router")
            _new_task = ""
            for _ap2 in _res_d.get("applied", []):
                if _ap2.get("kind") == "task.create":
                    _new_task = _ap2["task"]
            check("Decision 新建了任务", bool(_new_task), str(_res_d.get("applied")))
            _ast = sa.load_state(_new_task)
            check("指派记下了批次 id（= 触发它的那条用户消息）",
                  _ast["assignments"][0].get("batch_id") == "om_batch_2",
                  str(_ast["assignments"][0].get("batch_id")))
            # 复核 Decision：放行 + 发结果 → 结果消息应挂上批次
            _ast["assignments"][0]["artifacts"] = ["/tmp/batch.md"]
            sa.save_state(_new_task, _ast)
            _round.write_text(json.dumps({
                "kind": "assignment.completed", "task": _new_task,
                "source_ref": {"message_id": "om_batch_2", "chat_id": "oc_" + "c" * 32},
            }, ensure_ascii=False), encoding="utf-8")
            _doc_review = {
                "schemaVersion": 2, "triggerId": "trig-batch-review",
                "_round_kind": "assignment.completed",
                "items": [],
                "reviews": [{"assignment": _ast["assignments"][0]["id"],
                             "verdict": "approve", "note": "核对通过"}],
                "outbox": [{"target": "thread", "purpose": "result", "text": "批次任务已完成"}],
            }
            sa.apply_decision(_new_task, _doc_review, actor="router")
            _ob_recs = [r for r in core.list_outbox() if r.get("purpose") == "result"]
            check("结果消息挂上了批次 id",
                  bool(_ob_recs) and _ob_recs[0].get("batch") == "om_batch_2",
                  str([(r.get("purpose"), r.get("batch")) for r in _ob_recs]))
        finally:
            core.OUTBOX_DIR = _orig_dir0

        # ── 同批结果消息：等齐 → 合并成一条 ──
        core.OUTBOX_DIR = tmp / "outbox_batch"
        _sent = []
        _orig_send = lb3.send_reply
        lb3.send_reply = lambda mid, text, **kw: (_sent.append((mid, text)),
                                                  {"ok": True, "message_id": "om_x"})[1]
        try:
            r1 = core.add_outbox(target="thread", purpose="result", text="T876 已完成",
                                 task="T910", assignment="A01", batch="om_batch_1",
                                 chat_id="oc_" + "c" * 32, message_id="om_reply_1",
                                 dedupe_window_seconds=0)
            lb3.deliver_outbox()
            check("同批还有指派在跑 → 结果暂存不投递", _sent == [], str(_sent))
            # 把兄弟指派也置为已闭环 → 批次齐了（直接改文件，避开 task.md 渲染）
            _st = json.loads((_bt / "state.json").read_text(encoding="utf-8"))
            for _a in _st["assignments"]:
                _a["status"] = "approved"
            (_bt / "state.json").write_text(json.dumps(_st, ensure_ascii=False), encoding="utf-8")
            r2 = core.add_outbox(target="thread", purpose="result", text="T875 全部完成",
                                 task="T911", assignment="A01", batch="om_batch_1",
                                 chat_id="oc_" + "c" * 32, message_id="om_reply_1",
                                 dedupe_window_seconds=0)
            lb3.deliver_outbox()
            check("批次齐了 → 合并成一条发出", len(_sent) == 1, str(_sent))
            check("合并后的正文含两条结果",
                  len(_sent) == 1 and "T876 已完成" in _sent[0][1] and "T875 全部完成" in _sent[0][1],
                  str(_sent))
            _rc = core.list_outbox()
            check("两条记录都标成功（共享凭据）",
                  all(x["status"] == "succeeded" for x in _rc), str([x["status"] for x in _rc]))
        finally:
            lb3.send_reply = _orig_send
            core.OUTBOX_DIR = _orig_dir0


        # ── 表情：一条消息多个指派只挂一个 ──
        _calls2 = []
        _orig_react2 = lb3.react
        lb3.react = lambda mid, emoji: (_calls2.append((mid, emoji)), True)[1]
        _st2 = lb3.load_state()
        _st2["reaction_seen"] = {}
        lb3.save_state(_st2)
        try:
            lb3._reaction_targets({})
            check("一条消息多个指派 → 只出一个表情目标", len(_calls2) == 0, str(_calls2))
            tgt = lb3._reaction_targets({})
            check("同批同消息只留一个目标", len({t[0] for t in tgt}) == len(tgt), str(tgt))
        finally:
            lb3.react = _orig_react2

        # ── 完成语义：done（worker 交付）不算完成，approved（复核放行）才算 ──
        check("done 不再算完成（避免「已完成」后又等 30 秒）",
              lb3._assignment_stage({"status": "done"}) == "",
              str(lb3._assignment_stage({"status": "done"})))
        check("approved 才算完成",
              lb3._assignment_stage({"status": "approved"}) == "completed",
              str(lb3._assignment_stage({"status": "approved"})))
        check("running 算派发中",
              lb3._assignment_stage({"status": "running"}) == "dispatched")

        # ── 状态文件原子写：临时文件名唯一 ──
        _src3 = (pathlib.Path(__file__).resolve().parent / "lark_bridge.py").read_text(encoding="utf-8")
        check("状态临时文件名带 pid/随机后缀（并发写不再撞名）",
              "tmp = STATE_FILE.with_name(f\"{STATE_FILE.name}.tmp.{os.getpid()}" in _src3)
        check("状态锁可重入（同线程再拿不会自锁）",
              "_LOCK_OWNER == me" in _src3)

        # ── 投递与表情分线程 + 1 秒 tick ──
        check("投递与表情是两个独立线程",
              "def deliver_loop" in _src3 and "def reaction_loop" in _src3
              and "threading.Thread(target=reaction_loop" in _src3)
        check("投递循环 1 秒一 tick", "time.sleep(1)" in _src3)

        # ── 回执 updated_at 由系统盖章 ──
        _src4 = (pathlib.Path(__file__).resolve().parent / "worker_runner.py").read_text(encoding="utf-8")
        check("回执 updated_at 被系统改写（不信 worker 自填）",
              "updated_at\\s*[:：]" in _src4 and "resp_path.write_text(stamped" in _src4)

        # ── router 契约：先派单后叙述 + 同批合并 + 后续指派带标识 ──
        _rs = (pathlib.Path(__file__).resolve().parent / "skill" / "router-system.md").read_text(encoding="utf-8")
        check("契约里有「先派单，后叙述」", "### 先派单，后叙述" in _rs)
        check("契约里有同批合并回报规则", "### 同批指派：结果合并成一条回报" in _rs)
        check("契约要求后续指派带具体标识", "### 后续指派要带具体标识" in _rs)
        check("SKILL.md 由契约生成（单一真源）",
              "### 先派单，后叙述" in (pathlib.Path(__file__).resolve().parent / "skill" / "SKILL.md").read_text(encoding="utf-8"))

        print("\n[26g-4] @ 提及：飞书的 @ 是消息内容里的实体，不是正文里的「@名字」")
        import lark_bridge as lb4
        _OID = "ou_b18ba1fe9a09cabbe57e9af34535cec7"
        _rm = lb4.render_mentions(f"你好 @[{_OID}]")
        check("行内占位符 → 真正的提及实体",
              _rm.startswith("你好 <at user_id=") and _rm.endswith("</at>"),
              _rm)
        _rm2 = lb4.render_mentions(f"@[{_OID}|小德老师]")
        check("占位符可带显示名",
              "小德老师</at>" in _rm2 and f'user_id="{_OID}"' in _rm2, _rm2)
        check("卡片写法 <at id=…> 被修正成文字写法",
              lb4.render_mentions(f'<at id="{_OID}"></at>') ==
              f'<at user_id="{_OID}"></at>',
              lb4.render_mentions(f'<at id="{_OID}"></at>'))
        check("结构化 mentions 放正文开头",
              lb4.render_mentions("这条需要你拍板。", [{"open_id": _OID}]).startswith(
                  f'<at user_id="{_OID}">'),
              lb4.render_mentions("这条需要你拍板。", [{"open_id": _OID}]))
        check("结构化 mentions 可放结尾",
              lb4.render_mentions("这条需要你拍板。",
                                  [{"open_id": _OID, "at": "end"}]).rstrip().endswith("</at>"))
        check("正文里已提过的人不重复提",
              lb4.render_mentions(f"已提 @[{_OID}]", [{"open_id": _OID}]).count("user_id=") == 1,
              lb4.render_mentions(f"已提 @[{_OID}]", [{"open_id": _OID}]))
        check("纯文本 @名字 不被当成提及（不乱 @ 人）",
              "@小德" in lb4.render_mentions("@小德 看一下") and
              "<at" not in lb4.render_mentions("@小德 看一下"))
        # outbox 记录能带 mentions（Decision → outbox）
        _orig_dir4 = core.OUTBOX_DIR
        core.OUTBOX_DIR = tmp / "outbox_mentions"
        try:
            _rec_m = core.add_outbox(target="thread", purpose="question", text="需要你拍板",
                                     task="", chat_id="oc_" + "c" * 32,
                                     message_id="om_m1", mentions=[{"open_id": _OID}],
                                     dedupe_window_seconds=0)
            check("outbox 记录带上了 mentions",
                  _rec_m.get("mentions") and _rec_m["mentions"][0]["open_id"] == _OID,
                  str(_rec_m.get("mentions")))
        finally:
            core.OUTBOX_DIR = _orig_dir4
        # 契约：说清用 mentions 参数，不是文本 @
        _rs4 = (pathlib.Path(__file__).resolve().parent / "skill" / "router-system.md").read_text(encoding="utf-8")
        check("契约里有 @ 提及的说明", "### @ 某人：用 `mentions` 参数" in _rs4)
        check("契约说明了文字消息的提及写法", '<at user_id="ou_xxx">' in _rs4)
        check("契约警告了卡片写法在文字消息里不生效", "卡片写法" in _rs4 or "只**在卡片里生效" in _rs4)
        check("SKILL.md 同步了 @ 提及说明",
              "### @ 某人：用 `mentions` 参数" in
              (pathlib.Path(__file__).resolve().parent / "skill" / "SKILL.md").read_text(encoding="utf-8"))

        print("\n[26g-5] router 思考等级：可配、可运行中改")
        import router as rt5
        check("有思考等级常量与默认值", hasattr(rt5, "THINKING_LEVELS")
              and rt5.DEFAULT_THINKING == "low", str(getattr(rt5, "DEFAULT_THINKING", "")))
        _src5 = (pathlib.Path(__file__).resolve().parent / "router.py").read_text(encoding="utf-8")
        check("启动 pi 时带 --thinking", '"--thinking", self.thinking' in _src5)
        check("启动后再打一次等级（resume 会带回老等级）",
              "self.apply_thinking()" in _src5 and "set_thinking_level" in _src5)
        check("换模型 / 轮转 session 后重打等级",
              _src5.count("self.apply_thinking()") >= 3, str(_src5.count("self.apply_thinking()")))
        check("model 子命令能改思考等级",
              "--thinking" in _src5 and "思考等级已设为" in _src5)
        _mst5 = rt5.load_model_state()
        check("model.json 记着当前等级", bool(_mst5.get("thinking")), str(_mst5.get("thinking")))
        check("等级取值合法", (mst5 := _mst5.get("thinking")) in rt5.THINKING_LEVELS, str(mst5))
        _srv5 = (pathlib.Path(__file__).resolve().parent / "serve.py").read_text(encoding="utf-8")
        check("看板接口也能改等级", '"thinking"' in _srv5 and "THINKING_LEVELS" in _srv5)

        print("\n[26g-6] 表情先于投 router（收到就贴，不等 router 结果）")
        _src6 = (pathlib.Path(__file__).resolve().parent / "lark_bridge.py").read_text(encoding="utf-8")
        _du = _src6[_src6.index("def deliver_user_message"):_src6.index("# ------------------------------------------------------------------ 拉取")]
        _i_react = _du.index('react_async(msg_id, rc["received"])')
        _i_notify = _du.index('sa.notify("user.message"')
        check("贴表情在投 router 之前", _i_react < _i_notify, f"react@{_i_react} notify@{_i_notify}")
        check("贴表情是异步的（不把投 router 拖后 ~1 秒）",
              "def react_async" in _src6 and "threading.Thread(target=_run" in _src6)
        check("贴表情全程加锁（替换语义，并发会互相覆盖）",
              "_REACTION_LOCK = threading.Lock()" in _src6 and "with _REACTION_LOCK:" in _src6)
        import lark_bridge as lb6
        check("有异步贴表情入口", callable(getattr(lb6, "react_async", None)))

        print("\n[26i] 企微智能机器人接入（长连接 + 按渠道分流）")
        _root_i = pathlib.Path(__file__).resolve().parent
        _wb_i = (_root_i / "wecom_bridge.py")
        check("有企微桥接脚本", _wb_i.exists())
        _src_i = _wb_i.read_text(encoding="utf-8") if _wb_i.exists() else ""
        check("走长连接端点（不需要公网地址）",
              "wss://openws.work.weixin.qq.com" in _src_i and "aibot_subscribe" in _src_i)
        check("回复透传回调的 req_id",
              "aibot_respond_msg" in _src_i and "aibot_send_msg" in _src_i)
        check("有心跳与重连", "PING_INTERVAL" in _src_i and "RECONNECT_BACKOFF" in _src_i)
        check("企微投递器只取 channel=wecom 的",
              '(r.get("channel") or "") == "wecom"' in _src_i)
        _lb_i = (_root_i / "lark_bridge.py").read_text(encoding="utf-8")
        check("飞书投递器只取 channel=feishu 的",
              '(r.get("channel") or "feishu") == "feishu"' in _lb_i)
        check("飞书投递器遇到别的渠道是「跳过」不是「作废」",
              'return 0' in _lb_i.split('if rec.get("channel") != "feishu":')[1][:400]
              and 'status="cancelled"' not in _lb_i.split('if rec.get("channel") != "feishu":')[1][:400])
        check("企微配置不入库（只提交 example）",
              (_root_i / "wecom_config.example.json").exists()
              and "wecom_config.json" in
              (_root_i.parent / ".gitignore").read_text(encoding="utf-8"))
        _ex_i = json.loads((_root_i / "wecom_config.example.json").read_text(encoding="utf-8"))
        _real_i = json.loads((_root_i / "wecom_config.json").read_text(encoding="utf-8"))
        check("example 里没有真实凭据",
              "aib0HXOC9RXpwoBryQATEZnGgqgV9QH9qBD" not in json.dumps(_ex_i, ensure_ascii=False))
        check("真实凭据只在本机配置里",
              _real_i["bot"]["bot_id"] != _ex_i["bot"]["bot_id"])
        # 渠道继承：任务来源是企微 → outbox 落 wecom（**在沙箱里跑**，不碰生产任务）
        import os as _os_i
        import subprocess as _sp_i
        _probe_i = (
            "import json, sa, core\n"
            "t = sa.create_task_internal(title='渠道继承探针', goal='g', acceptance='a',\n"
            "    source='wecom', source_ref=['platform=wecom', 'chat_id=wr_x',\n"
            "                              'message_id=wm_x', 'reply_to=req_x'])\n"
            "tid = t if isinstance(t, str) else t['id']\n"
            "rec = core.add_outbox(target='thread', purpose='reply', text='t', task=tid)\n"
            "print(json.dumps({'channel': rec['channel'], 'reply_to': rec['reply_to'],\n"
            "                  'chat_id': rec['chat_id']}))\n"
        )
        _env_i = {k: v for k, v in _os_i.environ.items()
                  if not k.startswith("SA_") and k not in ("SA_ROOT",)}
        _env_i["SA_ROOT"] = str(tmp / "wecom_sandbox")
        _proc_i = _sp_i.run([sys.executable, "-c", _probe_i], cwd=str(_root_i),
                             env=_env_i, capture_output=True, text=True)
        _out_i = {}
        try:
            _out_i = json.loads((_proc_i.stdout or "{}").strip().splitlines()[-1])
        except Exception:
            _out_i = {}
        check("outbox 渠道从任务来源继承（企微→wecom）",
              _out_i.get("channel") == "wecom", str(_out_i))
        check("outbox 回复目标继承 req_id（企微回复要透传它）",
              _out_i.get("reply_to") == "req_x", str(_out_i))
        check("outbox 会话从任务来源继承", _out_i.get("chat_id") == "wr_x", str(_out_i))
        import router as _rt_i
        _ev_w = {"kind": "user.message", "source": "wecom",
                 "source_ref": {"platform": "wecom", "chat_id": "wr_x",
                                "message_id": "wm_not_in_ledger"}}
        _ok_w, _why_w = _rt_i.verify_user_event(_ev_w)
        check("企微事件也要过账本核验（查不到 → 拒）", not _ok_w, _why_w)
        # ── 企微「表情」：协议里没有 reaction，只能发表情短消息 ──
        check("企微表情三档（收到/加油/搞定）",
              all(x in _src_i for x in ('"received": "👌"', '"dispatched": "💪"',
                                        '"completed": "✅"')), "")
        check("企微表情是发消息（markdown），不是 reaction 类型",
              '"msgtype": "markdown"' in _src_i and "aibot_respond_msg" in _src_i)
        check("受理回执改用表情（不发文字）",
              "suppress_ack_text" in _src_i and 'rec.get("purpose") == "ack"' in _src_i)
        check("表情阶段：只有 approved 才算完成（done 不提前发「搞定」）",
              'if st == "approved":' in _src_i and 'if st in ("assigned", "running"):' in _src_i)
        check("表情循环等长连接就绪再发（否则状态被提前标成已发）",
              "conn.subscribed" in _src_i and "reaction_seen" in _src_i)

        print("\n[26h] 事件带说话人/渠道/话题 + 链式派工上限 + worker prompt 内联")
        import router as rt4
        ev_u = {"kind": "user.message", "id": "trig-s", "text": "看下审批",
                "source_ref": {"chat_id": "oc_" + "a" * 32, "chat_name": "商户平台研发",
                               "chat_name_source": "feishu", "sender": "ou_" + "b" * 32,
                               "sender_name": "同事A", "thread_id": "omt_xx"}}
        pr = rt4.build_prompt(ev_u)
        check("用户消息带说话人", "说话人: 同事A" in pr, pr[:200])
        check("用户消息带渠道", "渠道: 商户平台研发" in pr, pr[:200])
        check("用户消息带话题串", "话题串: 话题 omt_xx" in pr, pr[:200])
        check("用户消息带原话", "看下审批" in pr)
        ev_nt = {"kind": "user.message", "id": "t2", "text": "x", "source_ref": {}}
        prn = rt4.build_prompt(ev_nt)
        check("没有来源信息时也不报错",
              "[事件] user.message" in prn and "x" in prn, prn[:160])
        check("没有说话人时不硬造名字", "说话人: None" not in prn)

        src8 = (pathlib.Path(__file__).resolve().parent / "sa.py").read_text(encoding="utf-8")
        check("有链式派工配置读写", "def load_chain_config" in src8 and "def save_chain_config" in src8)
        check("链式超限有独立理由码", "chain_dispatch_limit" in src8)
        check("用户消息清零链式计数", "chain_dispatch_count\"] = 0" in src8)
        chain_cfg = json.loads((pathlib.Path(__file__).resolve().parent / "router" / "chain.json").read_text(
            encoding="utf-8")) if (pathlib.Path(__file__).resolve().parent / "router" / "chain.json").exists() else {}
        check("链式配置有上限字段", "max_consecutive_dispatches" in chain_cfg, json.dumps(chain_cfg))
        src9 = (pathlib.Path(__file__).resolve().parent / "worker_runner.py").read_text(encoding="utf-8")
        check("worker prompt 内联指派单内容", "def instruction_text" in src9
              and "以下是派给你的指派单全文" in src9)

        print("\n[26i] 话题 ID 的产生与回写")
        src10 = (pathlib.Path(__file__).resolve().parent / "lark_bridge.py").read_text(encoding="utf-8")
        check("有查话题 id 的工具", "def resolve_thread_id" in src10)
        check("用 messages-mget 回查 thread_id", "+messages-mget" in src10)
        check("回话后把话题 id 绑回任务", "def _bind_thread_to_task" in src10
              and "ref[\"thread_id\"] = thread_id" in src10)
        check("投递凭据里记了 thread_id", '"thread_id": thread_id' in src10)

        print("\n[26j] 不变式校验器（只读）")
        import importlib
        import invariants as inv
        importlib.reload(inv)
        # 在一个干净的沙箱状态上跑，必须零 ERROR —— 证明校验器不误报
        inv.TASKS_DIR = tmp / "inv_tasks"
        inv.OUTBOX_DIR = tmp / "inv_outbox"
        inv.ROUTER_DIR = tmp / "inv_router"
        (inv.TASKS_DIR / "T900").mkdir(parents=True, exist_ok=True)
        clean = {"task_id": "T900", "title": "干净任务", "status": "completed",
                 "source": "manual", "source_ref": {"message_id": "om_clean", "chat_id": "oc_x"},
                 "assignments": [{"id": "A01", "worker": "explore", "status": "approved",
                                  "response": str(tmp / "inv_resp.md"), "artifacts": [],
                                  "worker_session_id": "s1", "dispatched_at": "2026-01-01T00:00:00+08:00",
                                  "completed_at": "2026-01-01T00:01:00+08:00"}],
                 "applied_triggers": [], "applied_reviews": [], "chain_dispatch_count": 0}
        (tmp / "inv_resp.md").write_text("# 回执 A01\n", encoding="utf-8")
        (inv.TASKS_DIR / "T900" / "state.json").write_text(
            json.dumps(clean, ensure_ascii=False), encoding="utf-8")
        rep0 = inv.run()
        err0 = [v for v in rep0.violations if v["level"] == "ERROR"]
        check("校验器在干净状态上零误报", not err0,
              "; ".join(f"{v['rule']}@{v['where']}" for v in err0[:4]))
        check("校验器规则表覆盖完整", len(inv.RULES) >= 12, str(len(inv.RULES)))
        check("校验器在有数据时确实跑了多条规则", len(rep0.checked) >= 6,
              str(sorted(rep0.checked)))
        # 造一个坏状态：Task completed 但指派还在 running
        bad = json.loads(json.dumps(clean))
        bad["assignments"][0]["status"] = "running"
        (inv.TASKS_DIR / "T900" / "state.json").write_text(
            json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        rep1 = inv.run()
        errs1 = [v for v in rep1.violations if v["level"] == "ERROR"]
        check("校验器能抓到「completed 但指派还在 running」",
              any(v["rule"] == "task_completed_needs_evidence" for v in errs1),
              str([v["rule"] for v in errs1[:3]]))
        check("校验器只读（状态文件没被改）",
              json.loads((inv.TASKS_DIR / "T900" / "state.json").read_text(encoding="utf-8"))
              ["assignments"][0]["status"] == "running")

        print("\n[26k] 准入策略：私聊/群白名单/任务级临时授权")
        import policy as pol
        importlib.reload(pol)
        pol._tasks_dir = lambda: (tmp / "pol_tasks")
        (tmp / "pol_tasks" / "T910").mkdir(parents=True, exist_ok=True)
        (tmp / "pol_tasks" / "T910" / "state.json").write_text(json.dumps({
            "task_id": "T910", "title": "授权测试", "status": "running",
            "source_ref": {"chat_id": "oc_grp_a", "message_id": "om_g1"},
            "assignments": [], "authorized_actors": []}, ensure_ascii=False), encoding="utf-8")
        pcfg = {"owner": {"open_id": "ou_owner"}, "private_chat": {"owner_only": True},
                "groups": {"allow": ["oc_grp_a"]}, "people": {"甲": "ou_jia"},
                "grant_ttl_hours": 48}
        V = pol.check_command
        check("owner 私聊放行",
              V(chat_type="p2p", chat_id="oc_p2p", sender_id="ou_owner", cfg=pcfg)["allow"])
        check("非 owner 私聊被拒",
              not V(chat_type="p2p", chat_id="oc_p2p", sender_id="ou_jia", cfg=pcfg)["allow"])
        check("白名单群里其他人放行",
              V(chat_type="group", chat_id="oc_grp_a", sender_id="ou_jia", cfg=pcfg)["allow"])
        check("非白名单群被拒",
              not V(chat_type="group", chat_id="oc_grp_b", sender_id="ou_jia", cfg=pcfg)["allow"])
        pol.grant_actor("T910", "ou_jia", name="甲", chat_id="oc_grp_a", question="能过吗？", ttl_hours=48)
        gv = V(chat_type="group", chat_id="oc_grp_a", sender_id="ou_jia", cfg=pcfg)
        check("临时授权后可在任务会话发言", gv["allow"] and gv.get("grant_task") == "T910",
              str(gv.get("grant_task")))
        check("授权带上了在问什么",
              (gv.get("grant") or {}).get("question") == "能过吗？", str(gv.get("grant")))
        check("授权不跨会话（别的群不行）",
              not V(chat_type="group", chat_id="oc_grp_b", sender_id="ou_jia", cfg=pcfg)["allow"])
        pol.revoke_actor("T910", "ou_jia")
        check("撤销后回到群规则",
              not V(chat_type="group", chat_id="oc_grp_b", sender_id="ou_jia", cfg=pcfg)["allow"])
        pol.grant_actor("T910", "ou_jia", name="甲", chat_id="oc_grp_a", ttl_hours=0)
        check("过期授权立即失效",
              not V(chat_type="group", chat_id="oc_grp_b", sender_id="ou_jia", cfg=pcfg)["allow"])
        # router prompt 里要带授权上下文
        import router as rt5
        ev_g = {"kind": "user.message", "id": "tg", "text": "能过",
                "grant": {"grant_task": "T910", "granted_to": "甲", "question": "能过吗？"}}
        pg = rt5.build_prompt(ev_g)
        check("router prompt 带临时授权上下文", "临时授权: 任务 T910" in pg, pg[:200])
        check("授权消息指示 router 沿用该任务", "沿用该任务" in pg)
        src11 = (pathlib.Path(__file__).resolve().parent / "lark_bridge.py").read_text(encoding="utf-8")
        check("桥接接入了准入判定", "policy.check_command" in src11)
        check("准入拒绝时不投 router", "policy_denied" in src11)

        print("\n[26l] 历史脏数据清理（迟到回执覆盖）")
        import cleanup_state as cs
        importlib.reload(cs)
        cs.inv.TASKS_DIR = tmp / "clean_tasks"
        (cs.inv.TASKS_DIR / "T920").mkdir(parents=True, exist_ok=True)
        # 造一个「done 被迟到 failed 覆盖」的任务
        (cs.inv.TASKS_DIR / "T920" / "state.json").write_text(json.dumps({
            "task_id": "T920", "title": "脏数据", "goal": "验证清理", "status": "completed",
            "source": "manual", "acceptance_lines": [], "context_files": [], "task_notes": "",
            "created_at": "2026-01-01T10:00:00+08:00", "updated_at": "2026-01-01T10:00:00+08:00",
            "source_ref": {"message_id": "om_dirty"},
            "assignments": [{"id": "A01", "worker": "explore", "status": "failed",
                             "instruction": str(tmp / "clean_instr.md"),
                             "response": str(tmp / "clean_resp.md"), "artifacts": [],
                             "worker_session_id": "s1"}],
            "applied_triggers": [], "applied_reviews": [], "chain_dispatch_count": 0},
            ensure_ascii=False), encoding="utf-8")
        (tmp / "clean_resp.md").write_text("# 回执 A01\n\n## 产出\n\n- 无\n", encoding="utf-8")
        (tmp / "clean_instr.md").write_text("# 指派单 A01\n", encoding="utf-8")
        (cs.inv.TASKS_DIR / "T920" / "events.jsonl").write_text("\n".join([
            json.dumps({"ts": "2026-01-01T10:00:00+08:00", "kind": "assigned",
                        "assignment": "A01", "message": "A01 派给 explore"}),
            json.dumps({"ts": "2026-01-01T10:00:01+08:00", "kind": "assignment_started",
                        "assignment": "A01", "message": "A01 开始执行"}),
            json.dumps({"ts": "2026-01-01T10:00:10+08:00", "kind": "responded",
                        "assignment": "A01", "message": "A01 回执：done"}),
            json.dumps({"ts": "2026-01-01T10:00:20+08:00", "kind": "responded",
                        "assignment": "A01", "message": "A01 回执：failed"}),
        ]) + "\n", encoding="utf-8")
        plan0 = cs.plan()
        dirty = [c for c in plan0 if c["task"] == "T920"]
        check("清理计划发现脏状态（failed 应回滚为 done）",
              dirty and dirty[0]["fix_status"] and dirty[0]["authoritative"] == "done",
              str(dirty[:1]))
        check("清理前不变式能抓到状态不一致",
              any(v["rule"] == "assignment_state_consistent"
                  for v in inv.run().violations if v["level"] == "ERROR"))
        import sa as _sa
        _sa.TASKS_DIR = cs.inv.TASKS_DIR
        n = cs.apply_changes(dirty)
        st920 = json.loads((cs.inv.TASKS_DIR / "T920" / "state.json").read_text(encoding="utf-8"))
        check("清理把状态回滚到权威值",
              st920["assignments"][0]["status"] == "done",
              st920["assignments"][0]["status"])
        check("清理追加了 state_corrected 事件（审计链不断）",
              any(e.get("kind") == "state_corrected"
                  for e in cs.inv.read_jsonl(cs.inv.TASKS_DIR / "T920" / "events.jsonl")))
        check("清理后不再有状态不一致",
              not any(v["rule"] == "assignment_state_consistent"
                      for v in inv.run().violations if v["level"] == "ERROR"))
        check("历史违规降级为 WARN 而不是 ERROR",
              all(v["level"] == "WARN" for v in inv.run().violations
                  if v["rule"] == "assignment_transitions_legal"))

        print("\n[27] 指派单的两副面孔：新 session 完整 / 延续 session 精简")
        # 交互模式（system prompt）存在且说清了两个输出通道
        ipath = boot2 / "prompts" / "INTERACTION.md"
        itxt = ipath.read_text(encoding="utf-8") if ipath.is_file() else ""
        check("交互模式文件存在（框架补，router 不生成）", bool(itxt))
        check("交互模式说清只有两个输出通道",
              "产出文件" in itxt and "回执文件" in itxt)
        check("交互模式说清 status 语义",
              all(k in itxt for k in ("done", "failed", "blocked", "waiting_user_input")))

        kw = dict(task_id="T900", assignment_id="A01", agent_id="explore",
                  goal="统计任务数",
                  context_files=[], background="背景说明", trigger_id="tr1",
                  readonly=True, response_path="/tmp/x/response.md")
        full_i = core.render_instruction(**kw)
        cont_i = core.render_instruction(**kw, mode="continue")
        check("新 session 指派单含完整契约（边界/回执格式/完成策略）",
              all(s in full_i for s in ("## 边界（不要做这些）", "## 回执要求",
                                        "## 完成策略", "## 背景")), full_i[:120])
        check("新 session 指派单含完整回执格式骨架",
              "```markdown" in full_i and "## 产出" in full_i)
        check("延续 session 指派单标明是延续轮", "延续指派" in cont_i)
        check("延续 session 指派单裁掉背景/边界/完成策略",
              all(s not in cont_i for s in ("## 背景", "## 边界（不要做这些）", "## 完成策略")),
              cont_i[:200])
        check("延续 session 指派单保留目标",
              "## 目标" in cont_i)
        check("指派单不再有「具体要求 / 验收标准」小节（做法归 worker）",
              all(s not in full_i for s in ("## 具体要求", "## 验收标准"))
              and all(s not in cont_i for s in ("## 具体要求", "## 验收标准")))
        dup_i = core.render_instruction(**{**kw, "background": kw["goal"]})
        check("背景与目标相同时不重复渲染「## 背景」", "## 背景" not in dup_i)
        check("延续 session 指派单仍写清回执路径与 status 枚举",
              "/tmp/x/response.md" in cont_i and "waiting_user_input" in cont_i)
        check("延续版比完整版短", len(cont_i) < len(full_i),
              f"{len(cont_i)} vs {len(full_i)}")

        # session 存在性判定：看事实（会话文件），不看意图
        orig_sdir = sa.SESSION_DIR
        fake_sdir = tmp / "sessions_probe"
        (fake_sdir / "cwd-slug").mkdir(parents=True, exist_ok=True)
        (fake_sdir / "cwd-slug" / "2026-01-01T00-00-00_w-T900-A01-explore-abcd12.jsonl").write_text(
            "{}\n", encoding="utf-8")
        sa.SESSION_DIR = fake_sdir
        check("会话文件存在 → 判定为已有 session（可精简）",
              sa.session_exists("w-T900-A01-explore-abcd12") is True)
        check("会话文件不存在 → 判定为新 session（不能精简）",
              sa.session_exists("w-T900-A02-explore-ffff99") is False)
        check("非法 session id 不误判（防路径穿越）",
              sa.session_exists("../../etc/passwd") is False and sa.session_exists("") is False)
        sa.SESSION_DIR = orig_sdir

        # 延续轮 prompt：短、不内联指派单全文
        asg_cont = {"id": "A02", "worker": "explore",
                    "instruction": str(tmp / "T900" / "assignments" / "A02" / "instruction.md"),
                    "response": str(tmp / "T900" / "assignments" / "A02" / "response.md"),
                    "worker_session_id": "w-T900-A01-explore-abcd12"}
        (tmp / "T900" / "assignments" / "A02").mkdir(parents=True, exist_ok=True)
        (tmp / "T900" / "assignments" / "A02" / "instruction.md").write_text(
            cont_i, encoding="utf-8")
        sa.SESSION_DIR = fake_sdir
        p_cont, is_cont = worker_runner.build_prompt("T900", asg_cont)
        sa.SESSION_DIR = orig_sdir
        check("延续轮 prompt 不内联指派单全文", "```markdown" not in p_cont, p_cont[:160])
        check("延续轮 prompt 仍给回执路径与摘要要求",
              "response.md" in p_cont and "SUMMARY:" in p_cont)
        check("延续轮 prompt 标注为延续", "继续上一个 session" in p_cont)

        print("\n[28] 重复执行防护 + 分层产出（摘要/代投回话）")
        # ── 摘要解析 ──
        receipt = """# 回执 A01

- assignment: `A01`
- status: `done`

## 摘要

产出：/tmp/xxx.md
已回话：否
问题：无
补充一句：只读统计完成。

## 结论

做完了。

## 产出

- `/tmp/xxx.md`

## 回话

统计完成：任务 84 个、指派 113 条。
"""
        ps = sa.parse_summary(receipt)
        check("摘要段解析出产出", ps["produced"] == "/tmp/xxx.md", str(ps))
        check("摘要段解析出「已回话：否」", ps["replied"] is False, str(ps))
        check("摘要段解析出问题字段", ps["problem"] == "无", str(ps))
        check("摘要有自由文本", "只读统计完成" in ps["text"], ps["text"])
        check("回话段能取到", sa.receipt_section(receipt, "回话").startswith("统计完成"))
        check("没有摘要段时返回空（不报错）", sa.parse_summary("# 回执\n\n## 结论\n\nx")["text"] == "")
        long_sum = "## 摘要\n\n" + ("很长的摘要内容" * 200)
        check("超长摘要被截断",
              len(sa.parse_summary(long_sum)["text"]) <= sa.SUMMARY_MAX_CHARS + 12,
              str(len(sa.parse_summary(long_sum)["text"])))
        # ── 重复执行防护 ──
        check("进程存活判定：假 pid 不算活", worker_runner.pid_alive(999999) is False)
        check("worker 进程判定：假 pid 不算 worker", worker_runner.pid_is_worker(999999) is False)
        check("回执是否已出结果：pending 不算",
              worker_runner.receipt_has_result("- status: `pending`") is False)
        check("回执是否已出结果：done 算",
              worker_runner.receipt_has_result("- status: `done`") is True)

        # ── runner 独占锁：两个 runner 不能同时跑 ──
        import fcntl as _fcntl
        lock_path = tmp / "runner.lock"
        lfd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        _fcntl.flock(lfd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        lfd2 = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        locked = False
        try:
            _fcntl.flock(lfd2, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            locked = True
        except OSError:
            locked = False
        os.close(lfd2)
        os.close(lfd)
        check("第二个 runner 拿不到锁（防重复派工）", locked is False)

        print("\n[28b] router 不拆解需求 + 框架自动复核（router 不读产出）")
        # ── 用户原话进指派单 ──
        iu = core.render_instruction(
            task_id="T901", assignment_id="A01", agent_id="explore",
            goal="用户的原话",
            context_files=[], background="", trigger_id="tr", readonly=True,
            response_path="/tmp/x/response.md",
            user_text="帮我把 2024 年的报销单都核一遍", user_text_note="")
        check("指派单含「## 用户原话」小节", "## 用户原话" in iu)
        check("用户原话原文进指派单", "帮我把 2024 年的报销单都核一遍" in iu)
        check("原话小节写明「不要改写」", "不要改写" in iu)

        # ── Decision 里残留 requirements/acceptance → 不进指派单（做法归 worker）──
        tid4 = sa.cmd_new_task(_ap.Namespace(
            id=None, title="残留要求任务", goal="唯一目标-残留要求-4c2d", context=None,
            context_file=None, acceptance="", source="manual", source_ref=None,
            by="human", allow_new_task=False))
        sa.apply_decision(tid4, {
            "schemaVersion": 2, "triggerId": "trig-leftover",
            "items": [{"triggerId": "trig-leftover", "taskAction": "continue", "taskId": tid4,
                       "agentId": "general-purpose", "goal": "把事情办完",
                       "requirements": ["必须先看 README 再改代码"],
                       "acceptance": ["产出里要有三段"], "confidence": 0.8,
                       "reason": "实现", "rationale": "r"}],
            "reviews": [], "outbox": []}, actor="router")
        st4 = sa.load_state(tid4)
        instr4 = pathlib.Path(st4["assignments"][0]["instruction"]).read_text(encoding="utf-8")
        check("Decision 里的 requirements 不进指派单",
              "必须先看 README 再改代码" not in instr4, instr4[:200])
        check("Decision 里的 acceptance 不进指派单",
              "产出里要有三段" not in instr4, instr4[:200])
        check("指派单只有目标，没有做法小节",
              "把事情办完" in instr4 and "## 具体要求" not in instr4
              and "## 验收标准" not in instr4)

        # ── 框架机械复核：done + 产出存在 + 回执完整 → 自动放行 ──
        tid3 = sa.cmd_new_task(_ap.Namespace(
            id=None, title="自动复核任务", goal="唯一目标-自动复核-9b7c", context=None, context_file=None,
            acceptance="", source="manual", source_ref=None, by="human", allow_new_task=False))
        (tmp / "instr3.md").write_text("# 指派单\n\n干活\n", encoding="utf-8")
        sa.cmd_assign(_ap.Namespace(task=tid3, worker="explore",
                                    instruction_file=str(tmp / "instr3.md"), brief="b"))
        aid3 = sa.load_state(tid3)["assignments"][0]["id"]
        art3 = tmp / "art3.md"
        art3.write_text("产出内容\n", encoding="utf-8")
        sa.cmd_respond(_ap.Namespace(assignment=f"{tid3}/{aid3}", status="done",
                                     summary="做完了", artifact=[str(art3)]))
        a3 = sa.load_state(tid3)["assignments"][0]
        check("框架自动放行（status=approved）", a3["status"] == "approved", a3["status"])
        check("放行来源标为 framework", a3.get("review_by") == "framework", str(a3.get("review_by")))
        check("任务直接完成（router 不参与）", sa.load_state(tid3)["status"] == "completed",
              sa.load_state(tid3)["status"])
        evs3 = [e for e in sa.read_events(tid3) if e.get("kind") == "assignment_auto_approved"]
        check("有「框架自动放行」事件", len(evs3) == 1, str(evs3[:1]))

        # ── 证据不足 → 不自动放行，交给 router ──
        tid4 = sa.cmd_new_task(_ap.Namespace(
            id=None, title="证据不足任务", goal="唯一目标-证据不足-4f2a", context=None, context_file=None,
            acceptance="", source="manual", source_ref=None, by="human", allow_new_task=False))
        (tmp / "instr4.md").write_text("# 指派单\n\n干活\n", encoding="utf-8")
        sa.cmd_assign(_ap.Namespace(task=tid4, worker="explore",
                                    instruction_file=str(tmp / "instr4.md"), brief="b"))
        aid4 = sa.load_state(tid4)["assignments"][0]["id"]
        sa.cmd_respond(_ap.Namespace(assignment=f"{tid4}/{aid4}", status="done",
                                     summary="说完成了", artifact=[str(tmp / "不存在.md")]))
        a4 = sa.load_state(tid4)["assignments"][0]
        check("产出不存在 → 不自动放行", a4["status"] == "done", a4["status"])
        check("未放行 → 任务挂 needs_human", sa.load_state(tid4)["status"] == "needs_human",
              sa.load_state(tid4)["status"])
        evs4 = [e for e in sa.read_events(tid4) if e.get("kind") == "assignment_review_pending"]
        check("有「机械复核未通过」事件", len(evs4) == 1, str(evs4[:1]))

        print("\n[29] 滞留进程巡检：连生产状态的验证脚本可被发现/清理")
        import time as _time
        import stale_sandbox as ss
        probe_dir = tmp / "stale_probe"
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe = probe_dir / "sandbox-harness-probe.py"
        probe.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
        env_prod = {k: v for k, v in _os2.environ.items() if not k.startswith("SA_")}
        env_sand = dict(env_prod, SA_ROOT=str(probe_dir / "sandbox_root"))
        p_prod = _sp.Popen([sys.executable, str(probe)], env=env_prod)
        p_sand = _sp.Popen([sys.executable, str(probe)], env=env_sand)
        try:
            _time.sleep(0.8)
            check("脚本画像能认出验证脚本",
                  ss.script_hit("python3 /tmp/x/event-channel-harness.py --a") == "event-channel-harness.py"
                  and ss.script_hit("python3 /tmp/x/sandbox-harness-probe.py")
                  == "sandbox-harness-probe.py")
            check("普通业务脚本不会被误报（不在画像里）",
                  not ss.script_hit("python3 /x/serve.py --port 8080"))
            res27 = ss.scan(min_age=0, prod_root=boot2)
            hit_pids = {r["pid"] for r in res27["hits"]}
            iso_pids = {r["pid"] for r in res27["isolated"]}
            check("连生产状态的滞留脚本被列为需处置", p_prod.pid in hit_pids, str(hit_pids))
            check("SA_ROOT 隔离的脚本只列为已隔离，不算需处置",
                  p_sand.pid in iso_pids and p_sand.pid not in hit_pids,
                  f"iso={iso_pids} hit={hit_pids}")
            check("存活阈值生效：阈值很高时不再命中",
                  p_prod.pid not in {r["pid"] for r in
                                     ss.scan(min_age=10 ** 6, prod_root=boot2)["hits"]})
            fake = [{"pid": 11, "ppid": 1, "age": 99999, "etime": "1-03:00:00",
                     "cmd": "python3 lark_bridge.py follow"},
                    {"pid": 12, "ppid": 1, "age": 99999, "etime": "1-03:00:00",
                     "cmd": "python3 router.py run"},
                    {"pid": 13, "ppid": 1, "age": 99999, "etime": "1-03:00:00",
                     "cmd": "python3 worker_runner.py run"}]
            resx = ss.scan(min_age=0, procs=fake, prod_root=boot2)
            check("真守护进程命令行永不命中（不会被误清）",
                  not resx["hits"] and not resx["isolated"] and len(resx["protected"]) == 3,
                  str(resx)[:200])
            # CLI 真的接了这套逻辑（默认只报告）
            r27 = _sp.run([sys.executable, str(boot2 / "stale_sandbox.py"),
                           "--json", "--min-age", "0"],
                          cwd=str(boot2), env=env_prod, capture_output=True, text=True)
            try:
                cj = json.loads(r27.stdout or "{}")
            except json.JSONDecodeError:
                cj = {}
            check("CLI 默认只报告：退出码 0 且给出 JSON 清单",
                  r27.returncode == 0 and bool(cj.get("hits")), (r27.stderr or "")[-200:])
            check("CLI 报告里包含连生产状态的那个进程",
                  p_prod.pid in {r["pid"] for r in cj.get("hits", [])}, str(cj.get("hits"))[:200])
            check("默认报告模式没有杀掉任何进程", p_prod.poll() is None)
            # 可选终止模式：只终止命中项，且真的退出
            acts = ss.terminate([r for r in res27["hits"] if r["pid"] == p_prod.pid],
                                grace=1.0, prod_root=boot2)
            check("终止模式对命中进程生效",
                  bool(acts) and acts[0]["result"] in ("terminated", "killed"), str(acts))
            try:
                p_prod.wait(timeout=5)
            except _sp.TimeoutExpired:
                pass
            check("被终止的进程确实退出了", p_prod.poll() is not None)
            acts2 = ss.terminate([{"pid": _os2.getpid(), "cmd": "python3 selftest.py",
                                   "state_root": "production"}], grace=0.1, prod_root=boot2)
            check("终止模式有二次防护：不碰自身/守护进程",
                  bool(acts2) and acts2[0]["result"] == "skipped-protected", str(acts2))
        finally:
            for p in (p_prod, p_sand):
                try:
                    p.kill()
                except OSError:
                    pass
    finally:
        sa.TASKS_DIR, sa.CHANNELS_DIR = orig_tasks, orig_channels
        sa.LEGACY_CHANNELS_DIR = orig_legacy_channels
        core.OUTBOX_DIR = orig_core_outbox
        core.TASKS_DIR = orig_core_tasks
        sa.ROUTER_DIR, sa.ROUTER_INBOX = orig_router_dir, orig_router_inbox
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'='*46}\n通过 {len(PASS)} / {len(PASS)+len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  ✗", f)
        raise SystemExit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
