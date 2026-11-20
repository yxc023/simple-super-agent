#!/usr/bin/env python3
"""Worker runner —— 执行指派。

router 决定「再派一个 worker」后，只是往协议里写了一份指派单；
真正干活的是这个 runner：它盯着所有 status=assigned 的指派，
为每个指派起一个独立的一次性 pi 进程（headless），
worker 写回执 → runner 登记回执 → 通知 router。

    python3 super-agent/worker_runner.py start|stop|status|run
    python3 super-agent/worker_runner.py run --parallel 3
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import datetime as _dt

ROOT = pathlib.Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))
import sa  # noqa: E402

LOG_FILE = pathlib.Path("/tmp/super-agent-worker-runner.out")
PID_FILE = pathlib.Path("/tmp/super-agent-worker-runner.pid")

def role_prompt(agent_id: str) -> str:
    """角色提示以 registry.json 为唯一真源（不再在本文件里维护第二份）。"""
    import core
    spec = core.worker_spec(agent_id) or {}
    return spec.get("role_prompt") or f"你是 {agent_id} worker。"


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{now()}] {msg}\n")
    print(msg, flush=True)


MAX_ATTEMPTS = int(os.environ.get("SA_WORKER_MAX_ATTEMPTS", "2"))
# worker 单次执行超时（秒）—— 超时没回执就当僵死
WORKER_TIMEOUT = int(os.environ.get("SA_WORKER_TIMEOUT", "1800"))

# 当前进程正在执行的指派（避免接管逻辑把自己的活儿又收一遍）
_INFLIGHT: set[tuple[str, str]] = set()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def pid_is_worker(pid: int) -> bool:
    """这个 pid 是不是我们的 worker 进程（防 pid 复用误判）。

    判据用 pi 的**专有参数**（`--print` / `--session-id`）——
    只看「名字里有 pi」会把别的进程误当 worker（worker 是 python 起的话就没有 pi 字样）；
    只看「活着」又会把被复用的 pid 当成还在跑。
    """
    try:
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return False
    return bool(out) and ("--print" in out or "--session-id" in out)


def mutate_assignment(task_id: str, aid: str, fn) -> dict | None:
    """改一条指派的登记字段（读-改-写）。"""
    try:
        st = sa.load_state(task_id)
    except SystemExit:
        return None
    for a in st.get("assignments", []):
        if a.get("id") == aid:
            fn(a)
            sa.save_state(task_id, st)
            return a
    return None


def reconcile_running() -> int:
    """runner 启动时对账 `running` 指派 —— **绝不重复执行**。

    三种情况：
      · worker 进程还活着 → **不收回、不重派**：等它跑完（复用），由接管逻辑登记回执
      · worker 进程已死   → 真僵死 → 收回重派
      · 没有 pid 信息（旧数据）→ 超过超时才算僵死，否则当作还在跑（等）
    """
    n = 0
    if not sa.TASKS_DIR.exists():
        return 0
    for d in sorted(sa.TASKS_DIR.glob("T*")):
        sp = d / sa.STATE_FILE
        if not sp.exists():
            continue
        try:
            st = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        changed = False
        for a in st.get("assignments", []):
            if a.get("status") != "running":
                continue
            pid = int(a.get("worker_pid") or 0)
            started = str(a.get("worker_started_at") or "")
            if pid and pid_alive(pid) and pid_is_worker(pid):
                # 前一次还在跑 → 复用，等它结束
                if not a.get("reused"):
                    a["reused"] = True
                    a["reused_at"] = now()
                    a["reused_by_pid"] = os.getpid()
                    changed = True
                    sa.log_event(st["task_id"], "assignment_reused", "worker-runner",
                                 f"{a['id']} 的上一次执行还在跑（pid {pid}）→ 不重跑，等它结束再接管登记",
                                 assignment=a["id"], worker_pid=pid)
                continue
            if pid:
                # 进程没了 → 真僵死 → 收回
                a["status"] = "assigned"
                a.pop("runner_started_at", None)
                a.pop("worker_pid", None)
                a.pop("worker_started_at", None)
                a["reclaimed_reason"] = "worker 进程已退出但没登记回执"
                changed = True
                n += 1
                sa.log_event(st["task_id"], "assignment_reclaimed", "worker-runner",
                             f"{a['id']} 的 worker 进程已消失，收回重派", assignment=a["id"])
                continue
            # 没有 pid 信息（旧数据）→ 按超时判断
            age = 0.0
            try:
                age = (time.time() - time.mktime(time.strptime(started[:19], "%Y-%m-%dT%H:%M:%S"))) if started else 0.0
            except Exception:
                age = 0.0
            if age > WORKER_TIMEOUT:
                a["status"] = "assigned"
                a.pop("runner_started_at", None)
                a["reclaimed_reason"] = f"无 worker pid 且已超过 {WORKER_TIMEOUT}s 未完成"
                changed = True
                n += 1
                sa.log_event(st["task_id"], "assignment_reclaimed", "worker-runner",
                             f"{a['id']} 无 worker pid 且超时，收回重派", assignment=a["id"])
        if changed:
            st["status"] = sa.recompute_task_status(st)
            sa.save_state(st["task_id"], st)
    return n


def receipt_has_result(body: str) -> bool:
    """回执里 worker 是否已经写好了结果（status 不是 pending）。"""
    m = re.search(r"^\s*[-*]\s*status\s*[:：]\s*`?([a-z_]+)`?", body or "", re.M)
    return bool(m) and m.group(1) not in ("pending", "")


def takeover_finished(skip: set | None = None) -> int:
    """接管登记：前一次执行（或旧 runner）留下的活儿，**不重跑**，等它跑完就登记回执。

    · 回执已经写好 → 直接登记（复用回执）
    · worker 进程已死且回执还是空的 → 收回重派（真僵死）
    """
    n = 0
    skip = skip or set()
    if not sa.TASKS_DIR.exists():
        return 0
    for d in sorted(sa.TASKS_DIR.glob("T*")):
        sp = d / sa.STATE_FILE
        if not sp.exists():
            continue
        try:
            st = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        changed = False
        for a in st.get("assignments", []):
            if a.get("status") != "running":
                continue
            key = (st["task_id"], a["id"])
            if key in skip:
                continue
            resp = sa.response_path(st["task_id"], a["id"])
            body = resp.read_text(encoding="utf-8") if resp.exists() else ""
            if receipt_has_result(body):
                log(f"{st['task_id']}/{a['id']} → 接管登记（前一次执行已写好回执，不重跑）")
                try:
                    register_receipt(st["task_id"], dict(a))
                except SystemExit as e:
                    log(f"{st['task_id']}/{a['id']} 接管登记失败: {e}")
                n += 1
                continue
            pid = int(a.get("worker_pid") or 0)
            if pid and not pid_alive(pid):
                # 进程没了且没回执 → 僵死 → 收回重派
                a["status"] = "assigned"
                a.pop("runner_started_at", None)
                a.pop("worker_pid", None)
                a["reclaimed_reason"] = "worker 进程已退出但没写回执"
                changed = True
                sa.log_event(st["task_id"], "assignment_reclaimed", "worker-runner",
                             f"{a['id']} 的 worker 进程已退出且没写回执，收回重派",
                             assignment=a["id"])
        if changed:
            st["status"] = sa.recompute_task_status(st)
            sa.save_state(st["task_id"], st)
    return n


def pending_assignments() -> list[tuple[str, dict]]:
    """待执行的指派（status=assigned 且没被别的线程领走）。

    超过 MAX_ATTEMPTS 的指派在这里被**显式置为 failed** 并通知 router ——
    否则它会永远停在 assigned，既不执行也不失败，变成无声悬挂。
    """
    out = []
    if not sa.TASKS_DIR.exists():
        return out
    for d in sorted(sa.TASKS_DIR.glob("T*")):
        sp = d / sa.STATE_FILE
        if not sp.exists():
            continue
        try:
            st = json.loads(sp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        changed = False
        for a in st.get("assignments", []):
            if a.get("status") != "assigned" or a.get("runner_started_at"):
                continue
            try:
                attempts = int(a.get("attempts", 0))
            except (TypeError, ValueError):
                attempts = 0
            if attempts >= MAX_ATTEMPTS:
                a["status"] = "failed"
                a.pop("runner_started_at", None)
                a["runner_error"] = f"超过最大重试次数 {MAX_ATTEMPTS}"
                changed = True
                sa.log_event(st["task_id"], "assignment_failed", "worker-runner",
                             f"{a['id']} 超过最大重试次数 {MAX_ATTEMPTS}，标记失败",
                             assignment=a["id"])
                sa.notify("assignment.failed", st["task_id"], assignment=a["id"],
                          status="failed", summary=f"超过最大重试次数 {MAX_ATTEMPTS}")
                continue
            out.append((st["task_id"], a))
        if changed:
            st["status"] = sa.recompute_task_status(st)
            sa.save_state(st["task_id"], st)
    return out


def claim(task_id: str, aid: str) -> dict | None:
    st = sa.load_state(task_id)
    for a in st["assignments"]:
        if a["id"] == aid:
            a["status"] = "running"
            a["runner_started_at"] = now()
            a["attempts"] = int(a.get("attempts", 0)) + 1
            st["status"] = sa.recompute_task_status(st)
            sa.save_state(task_id, st)
            sa.log_event(task_id, "assignment_started", "worker-runner",
                         f"{aid} 开始执行", assignment=aid, worker=a.get("worker"))
            return a
    return None


def instruction_text(a: dict, limit: int = 12000) -> str:
    """把指派单内容读出来（worker 少读一次文件）。

    读不到也不致命 —— 返回空串，prompt 里会告诉 worker 去读文件。
    """
    path = pathlib.Path(a.get("instruction") or "")
    try:
        if not path.is_file():
            return ""
        txt = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(txt) > limit:
        txt = txt[:limit] + "\n\n（指派单过长，已截断；完整内容见文件）"
    return txt


def build_prompt(task_id: str, a: dict) -> tuple[str, bool]:
    """生成送进 worker 的 prompt，并告诉调用方「这是不是延续轮」。

    新 session（会话文件不存在）：内联指派单全文 —— worker 没听过这套协议。
    延续 session（会话文件已存在）：只给本轮增量（目标摘要 + 指派单路径 + 回执路径），
    交互模式/边界/回执格式上一轮都在历史里，重复一遍只是浪费上下文。
    """
    resp = pathlib.Path(a["response"])
    instr_path = a.get("instruction") or ""
    sid = (a.get("worker_session_id") or "").strip()
    fresh = not sa.session_exists(sid)
    body = instruction_text(a)

    if fresh:
        role = role_prompt(a.get("worker", ""))
        if body:
            head = f"""以下是派给你的指派单全文（直接照做，不用再去读文件）：

{body}

（指派单原文也存放在 {instr_path}，需要核对时可以去读。）"""
        else:
            head = f"""请先完整读取指派单：{instr_path}"""
        prompt = f"""{role}

{head}
严格按指派单里的「具体要求」「边界」执行，只做这一件事。

完成后把回执写到：{resp}
回执格式见指派单末尾「回执要求」，至少包含 status（done/failed/blocked）、结论（一句话说清做了什么、结果是什么）、产出文件绝对路径。

最后在标准输出打印一行摘要：SUMMARY: <一句话结论>"""
        return prompt, False

    # ── 延续轮：会话里已经有上一轮的全部上下文，这里只说本轮增量 ──
    goal_line = _instruction_goal(instr_path) or "（见指派单）"
    prompt = f"""继续上一个 session 的工作（同一 worker session 的后续一轮）。

本轮要做：{goal_line[:200]}
完整指派单（精简版，只写本轮增量）：{instr_path}

交互模式、边界与回执格式与上一轮一致，不用重复交代。
完成后把回执写到：{resp}
最后在标准输出打印一行摘要：SUMMARY: <一句话结论>"""
    return prompt, True


def _instruction_goal(instr_path: str) -> str:
    """从指派单里抽「## 目标」的第一行（延续轮 prompt 用）。"""
    try:
        txt = pathlib.Path(instr_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = txt.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == "## 目标":
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    return nxt.strip()
            return ""
    return ""


def run_one(task_id: str, a: dict) -> None:
    aid = a["id"]
    prompt, continued = build_prompt(task_id, a)
    # worker session：同一个 session id 复用 pi 会话 ——
    # 续做同一指派（重做）或继承别的指派的 session 时，worker 能带着上一轮的上下文。
    sid = (a.get("worker_session_id") or "").strip()
    inherits = (a.get("inherits_session_from") or "").strip()
    log(f"{task_id}/{aid} → 启动 worker 进程 ({a.get('worker')})"
        + (f"，session={sid}" if sid else "")
        + (f"，继承 {inherits} 的 session" if inherits else "")
        + ("，延续轮（短 prompt）" if continued else "，新 session（内联指派单）"))
    models = [os.environ.get("SA_WORKER_MODEL", "minimax-cn/MiniMax-M3.1-Flash"),
              "bella/Deepseek-V4.1-Flash", "bella/MiniMax-M3.1-Flash"]
    models = list(dict.fromkeys(models))
    # 交互模式（system prompt）—— **框架补**，不用 router 生成：
    # 你是谁、只有哪两个输出通道、status 语义、纪律。
    # pi 的 system prompt 不写进会话历史（实测 session 里 system 消息为空），
    # 所以**每次运行都注入**：延续轮若不补，worker 就只剩下历史里的间接痕迹。
    interaction_prompt = ROOT / "prompts" / "INTERACTION.md"
    out, err, rc = "", "", 0
    for i, model in enumerate(models):
        try:
            cmd = ["pi", "--print", "--model", model]
            if sid:
                cmd += ["--session-id", sid]
            if interaction_prompt.is_file():
                cmd += ["--append-system-prompt", str(interaction_prompt)]
            cmd.append(prompt)
            p = subprocess.Popen(
                cmd, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env={**os.environ, "SA_ACTOR": "worker"},
            )
            # 记 pid —— runner 重启后靠它判断「前一次是不是还在跑」，避免重复执行
            mutate_assignment(task_id, aid,
                              lambda x: x.update({"worker_pid": p.pid,
                                                   "worker_started_at": now(),
                                                   "runner_pid": os.getpid()}))
            try:
                so, se = p.communicate(timeout=WORKER_TIMEOUT)
            except subprocess.TimeoutExpired:
                p.kill()
                so, se = p.communicate()
                out, err, rc = "", "worker 超时", 124
            else:
                out, err, rc = (so or "").strip(), (se or "").strip(), p.returncode
        except Exception as e:
            out, err, rc = "", f"worker 启动失败: {e}", 125
        # 429 / 配额类错误 → 换下一个模型
        blob = out + "\n" + err
        if rc == 0 and "429" not in blob and "rate_limit" not in blob.lower():
            break
        if i < len(models) - 1:
            log(f"{task_id}/{aid} 模型 {model} 失败（rc={rc}），换 {models[i+1]}：{blob[-160:]}")
            out, err = "", ""

    register_receipt(task_id, a, out=out, err=err, rc=rc)


def register_receipt(task_id: str, a: dict, *, out: str = "", err: str = "", rc: int = 0) -> None:
    """登记回执（把 worker 的结果写进 state.json + 通知 router）。

    两条路径共用：
      · 正常跑完 → run_one 调它
      · runner 重启后接管前一次执行 → takeover_finished 调它（**不重跑**）
    """
    aid = a["id"]
    resp_path = pathlib.Path(a["response"])
    body = resp_path.read_text(encoding="utf-8") if resp_path.exists() else ""

    summary = ""
    for line in (out or "").splitlines():
        if line.startswith("SUMMARY:"):
            summary = line[len("SUMMARY:"):].strip()
    if not summary:
        summary = (out or "").splitlines()[-1][:200] if out else ((err or "")[-300:] if err else "")

    # 失败判定：退出码非 0，或输出里有配额/错误标记
    blob = (out or "") + "\n" + (err or "")
    failed = rc != 0 or ("429" in blob) or ("rate_limit" in blob.lower()) or not (out or "").strip()
    ask = False
    # 接管路径没有进程输出可参考 → 以**回执里 worker 写的 status** 为准
    m_st = re.search(r"^\s*[-*]\s*status\s*[:：]\s*`?([a-z_]+)`?", body, re.M)
    receipt_status = m_st.group(1) if (m_st and m_st.group(1) != "pending") else ""
    if receipt_status:
        failed = receipt_status == "failed"
        ask = receipt_status == "waiting_user_input"
        if not summary:
            m_c = re.search(r"##\s*结论\s*\n+(.+?)(?:\n##|\Z)", body, re.S)
            summary = (m_c.group(1).strip()[:500] if m_c else "")

    # worker 主动提问（ask question）→ waiting_user_input，而不是 failed
    if not failed and not receipt_status:
        if re.search(r"status:\s*`?waiting_user_input", body):
            failed = False
            ask = True
    if failed and "429" in blob:
        summary = f"worker 调用失败（模型额度/配额问题）：{blob[-260:]}"
    elif failed and rc != 0:
        summary = f"worker 进程退出码 {rc}：{(err or out)[-260:]}"

    body = resp_path.read_text(encoding="utf-8") if resp_path.exists() else ""
    # updated_at 由系统**盖章** —— worker 自己填的时间戳不可信
    # （实测出现过“比现在还晚 14 分钟”的编造时间，router 读到的就是它）。
    stamped = re.sub(r"^([ \t]*(?:[-*]\s*)?updated_at\s*[:：]).*$",
                     lambda mo: f"{mo.group(1)} {now()}", body, flags=re.M)
    if stamped != body:
        resp_path.write_text(stamped, encoding="utf-8")
        body = stamped
    if ask:
        # 提问类回执：把「结论」段当成问题提给用户
        m = re.search(r"##\s*结论\s*\n+(.+?)(?:\n##|\Z)", body, re.S)
        if m:
            summary = m.group(1).strip()[:500] or summary
    if "status" not in body:
        resp_path.write_text(
            f"# 回执 {aid}\n\n- assignment: `{aid}`\n- worker: `{a.get('worker')}`\n"
            f"- task: `{task_id}`\n- status: `done`\n- updated_at: {now()}\n"
            + (f"- session: `{sid}`\n" if sid else "")
            + f"\n## 结论\n\n{summary}\n\n## 产出\n\n- （见指派单要求）\n", encoding="utf-8")
        body = resp_path.read_text(encoding="utf-8")   # 重读，否则下面的产出抓取拿不到内容

    # 「在等谁回答」：worker 在回执里写 `- ask: 张三` → 系统自动把张三
    # 临时授权到本任务（他在本任务会话里回复 bot 时会被受理）。
    ask_person = ""
    if ask:
        m_ask = re.search(r"^\s*[-*]\s*ask\s*[:：]\s*(.+?)\s*$", body, re.M)
        if m_ask:
            ask_person = m_ask.group(1).strip().strip("`\"'").split("（")[0].strip()
            # 去掉可能的补充说明（"张三（审批人）" → "张三"）
            if "（" in ask_person:
                ask_person = ask_person.split("（")[0].strip()

    # 产出证据：从回执「## 产出」段里抓绝对路径，作为 assignment.artifacts 登记
    artifacts = []
    m_out = re.search(r"##\s*产出\s*\n(.*?)(?:\n##|\Z)", body, re.S)
    if m_out:
        for line in m_out.group(1).splitlines():
            for p in re.findall(r"`([^`\n]+)`", line):
                p = p.strip()
                if p.startswith("/") and p not in artifacts:
                    artifacts.append(p)

    st_status = "failed" if failed else ("waiting_user_input" if ask else "done")
    try:
        sa.cmd_respond(argparse.Namespace(
            assignment=f"{task_id}/{aid}", status=st_status, summary=summary,
            artifact=artifacts or None, ask=ask_person))
    except SystemExit as e:
        log(f"{task_id}/{aid} 登记失败: {e}")
        # 登记失败：置 failed 并通知 router，绝不回置成 assigned（否则会无限重跑）
        st = sa.load_state(task_id)
        for x in st["assignments"]:
            if x["id"] == aid:
                x["status"] = "failed"
                x.pop("runner_started_at", None)
                x["runner_error"] = str(e)
        st["status"] = sa.recompute_task_status(st)
        sa.save_state(task_id, st)
        sa.log_event(task_id, "assignment_failed", "worker-runner",
                     f"{aid} 回执登记失败：{e}", assignment=aid)
        sa.notify("assignment.failed", task_id, assignment=aid, status="failed",
                  summary=f"回执登记失败：{e}")
    else:
        st = sa.load_state(task_id)
        for x in st["assignments"]:
            if x["id"] == aid:
                x.pop("runner_started_at", None)
        sa.save_state(task_id, st)
        log(f"{task_id}/{aid} 完成：{summary[:120]}")


def run_one_guarded(task_id: str, a: dict) -> None:
    """run_one 的安全包装 —— 崩了要登记 failed，不能让指派卡在 running。"""
    aid = a["id"]
    try:
        run_one(task_id, a)
    except Exception as e:
        log(f"{task_id}/{aid} worker 异常：{e}")
        try:
            sa.cmd_respond(argparse.Namespace(
                assignment=f"{task_id}/{aid}", status="failed",
                summary=f"worker 进程异常：{e}", artifact=None, ask=""))
        except Exception:
            pass
    finally:
        _INFLIGHT.discard((task_id, aid))


def loop(max_parallel: int = 2) -> None:
    running: dict[str, threading.Thread] = {}
    while True:
        for key, th in list(running.items()):
            if not th.is_alive():
                running.pop(key, None)
        # 接管登记：前一次执行（或旧 runner）留下的活儿 —— **不重跑**，等它跑完就登记
        try:
            takeover_finished(set(_INFLIGHT))
        except Exception as e:
            log(f"接管登记异常: {e}")
        if len(running) < max_parallel:
            for task_id, a in pending_assignments():
                key = f"{task_id}/{a['id']}"
                if key in running:
                    continue
                claimed = claim(task_id, a["id"])
                if not claimed:
                    continue
                # 用 guarded 版本：worker 崩了要登记 failed，不能让指派卡在 running
                _INFLIGHT.add((task_id, a["id"]))
                th = threading.Thread(target=run_one_guarded, args=(task_id, claimed), daemon=True)
                th.start()
                running[key] = th
                break
        time.sleep(3)


def cmd_start(a: argparse.Namespace) -> None:
    if PID_FILE.exists():
        try:
            os.kill(int(PID_FILE.read_text().strip()), 0)
            print(f"已在运行 (pid {PID_FILE.read_text().strip()})")
            return
        except (OSError, ValueError):
            PID_FILE.unlink(missing_ok=True)
    # 清掉任何残留实例 —— 两个 runner 会重复派工
    subprocess.run(["pkill", "-f", "worker_runner.py run"], check=False)
    time.sleep(1.0)
    time.sleep(0.5)
    with open(LOG_FILE, "ab") as out:
        p = subprocess.Popen([sys.executable, str(ROOT / "worker_runner.py"), "run"],
                             cwd=str(REPO), stdout=out, stderr=out, start_new_session=True)
    # 新 runner 可能因为旧进程还没释放锁而退出 —— 重试几次
    for _ in range(10):
        time.sleep(0.5)
        if p.poll() is None:
            break
        with open(LOG_FILE, "ab") as out:
            p = subprocess.Popen([sys.executable, str(ROOT / "worker_runner.py"), "run"],
                                 cwd=str(REPO), stdout=out, stderr=out, start_new_session=True)
    PID_FILE.write_text(str(p.pid))
    time.sleep(1.5)
    log(f"worker runner 已启动 pid={p.pid}")


def cmd_stop(a: argparse.Namespace) -> None:
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, 15)
            log(f"已停止 pid={pid}")
        except OSError:
            pass
        PID_FILE.unlink(missing_ok=True)
    else:
        print("未在运行")


def cmd_status(a: argparse.Namespace) -> None:
    running = False
    pid = None
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        try:
            os.kill(int(pid), 0); running = True
        except OSError:
            running = False
    print(json.dumps({
        "running": running, "pid": pid,
        "pending": [f"{t}/{a['id']}" for t, a in pending_assignments()],
        "log": str(LOG_FILE),
    }, ensure_ascii=False, indent=2))


def cmd_run(a: argparse.Namespace) -> None:
    # **独占锁**：同一时刻只允许一个 runner。
    # 两个 runner 同时跑会把同一条指派执行两遍（重复副作用）。
    try:
        import fcntl
        lock_fd = os.open(str(PID_FILE), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, str(os.getpid()).encode())
        globals()["_LOCK_FD"] = lock_fd          # 保持打开 → 锁不释放
    except (ImportError, OSError) as e:
        print(f"✖ 已有 worker runner 在跑（pid 文件被锁）：{e}", file=sys.stderr)
        raise SystemExit(0)
    try:
        subprocess.run([sys.executable, str(ROOT / "sync_skill.py")], check=False)
    except Exception:
        pass
    log("worker runner 启动")
    n = reconcile_running()
    if n:
        log(f"回收僵死指派 {n} 条（worker 进程已消失且没写回执）")
    try:
        loop(a.parallel)
    except KeyboardInterrupt:
        log("退出")


def main() -> None:
    ap = argparse.ArgumentParser(prog="worker_runner.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start"); p.set_defaults(func=cmd_start)
    p = sub.add_parser("stop"); p.set_defaults(func=cmd_stop)
    p = sub.add_parser("status"); p.set_defaults(func=cmd_status)
    p = sub.add_parser("run"); p.add_argument("--parallel", type=int, default=2); p.set_defaults(func=cmd_run)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
