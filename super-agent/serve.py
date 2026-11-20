#!/usr/bin/env python3
"""super-agent 看板服务（只读为主 + 人工干预写回）。

    python3 super-agent/serve.py --port 8788

只绑定 127.0.0.1。所有写操作都走 sa.py 的同一套文件协议，
所以页面上的每一次干预，在 CLI / agent 那边都能看到同一份状态。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import core  # noqa: E402
import sa  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
MAX_BODY = 2 * 1024 * 1024

# `/api/file` 允许读取的一级子目录（相对 ROOT）。
# 只放状态目录：配置文件（lark_config.json / policy.json）里有 app_id、open_id 等标识符，
# 看板是无鉴权的本地服务，不应该把它们暴露出去。
FILE_READABLE_DIRS = ("tasks", "outbox", "router")


def read_text(path: str | None, limit: int = 60000) -> str:
    if not path:
        return ""
    p = pathlib.Path(path)
    if not p.is_file():
        return ""
    try:
        t = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return t[:limit]


def read_jsonl(path: pathlib.Path, limit: int = 200) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def pid_alive(pid_file: pathlib.Path) -> dict:
    info = {"pid_file": str(pid_file), "running": False, "pid": None}
    if not pid_file.exists():
        return info
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        info.update({"running": True, "pid": pid})
    except (OSError, ValueError):
        pass
    return info




def board_pid_file(port: int) -> pathlib.Path:
    """pid 文件按端口分 —— 不同端口的看板不该抢同一个 pid 文件。

    命名与 board.sh 保持一致：super-agent-board-<port>.pid。
    """
    return pathlib.Path(f"/tmp/super-agent-board-{port}.pid")


def collect_daemons(port: int = 8788) -> dict:
    return {
        "lark_bridge": pid_alive(pathlib.Path("/tmp/super-agent-lark-bridge.pid")),
        "router": pid_alive(pathlib.Path("/tmp/super-agent-router.pid")),
        "worker_runner": pid_alive(pathlib.Path("/tmp/super-agent-worker-runner.pid")),
        "board": pid_alive(board_pid_file(port)),
    }


try:
    import router as router_mod
except Exception:      # router 模块不可用时不影响看板
    router_mod = None


def collect_router() -> dict:
    rdir = ROOT / "router"
    session = {}
    sp = rdir / "session.json"
    if sp.exists():
        try:
            session = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            session = {}
    decisions = read_jsonl(rdir / "outbox.jsonl", 50)
    inbox = read_jsonl(rdir / "inbox.jsonl", 500)
    # 已处理数取 processed.json（真正被 router 处理过的 triggerId），
    # 不能用决策条数 —— 决策和事件不是一对一，之前的算法会算出虚高的「待处理」
    processed = 0
    pfile = rdir / "processed.json"
    if pfile.exists():
        try:
            pdata = json.loads(pfile.read_text(encoding="utf-8"))
            processed = len(pdata) if isinstance(pdata, (dict, list)) else 0
        except (json.JSONDecodeError, OSError):
            processed = 0
    # 模型状态（router/model.json）
    model_state = {}
    mfile = rdir / "model.json"
    if mfile.exists():
        try:
            model_state = json.loads(mfile.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            model_state = {}
    digest_lines = 0
    dfile = rdir / "digest.md"
    if dfile.exists():
        digest_lines = len([l for l in dfile.read_text(encoding="utf-8").splitlines() if l.strip()])
    return {
        "session": session,
        "digest_lines": digest_lines,
        "decisions": decisions[-30:],
        "decision_count": processed,
        "event_count": len(inbox),
        "pending_events": max(0, len(inbox) - processed),
        "model": {
            "primary": model_state.get("primary") or "",
            "boot_primary": model_state.get("boot_primary") or "",
            "active": model_state.get("active") or (session.get("model") or ""),
            "degraded": bool(model_state.get("degraded")),
            "degraded_to": model_state.get("degraded_to") or "",
            "degraded_at": model_state.get("degraded_at") or "",
            "restore_after_seconds": model_state.get("restore_after_seconds", 300),
            "fallback_models": list(getattr(router_mod, "FALLBACK_MODELS", ())),
            "log": _reaction_log(),
        },
        "chain": sa.load_chain_config(),
    }


def _reaction_log() -> list:
    """最近的表情记录（收到/派发/完成）。"""
    try:
        import lark_bridge as lb
        return lb.reaction_log(12)
    except Exception:
        return []


def collect_messages() -> list[dict]:
    return read_jsonl(ROOT / "inbox" / "messages.jsonl", 300)


def collect_chains() -> list[dict]:
    """消息链路（一条消息 → 表情 → router → 任务 → 指派 → 回话）。

    看板的其它面板是**按文件分栏**的（收件箱 / 决策流 / Decision / Outbox），
    这里以 **message_id 为主键**把同一条消息的各个环节串成一条时间线。
    """
    try:
        import chain_view
        return chain_view.build_chains(limit=18)
    except Exception as e:      # 链路视图挂了不该拖垮整个看板
        return [{"error": str(e)}]


def collect_bridge() -> dict:
    """飞书桥接的运行态（模式 / 最近拉取 / 最近收到消息）。"""
    st = {}
    p = ROOT / "lark_state.json"
    if p.exists():
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            st = {}
    return {
        "mode": st.get("mode"),
        "last_poll": st.get("last_poll"),
        "last_message_at": st.get("last_message_at"),
        "last_event_at": st.get("last_event_at"),
        "started_at": st.get("started_at"),
        "seen_count": len(st.get("seen") or {}),
    }


def _decision_entry(task: str, doc: dict) -> dict:
    res = doc.get("result") or {}
    sub = doc.get("submitted") or {}
    return {
        "task": task,
        "ts": (res.get("ts") or ""),
        "triggerId": sub.get("triggerId"),
        "items": [{"taskAction": i.get("taskAction"), "taskId": i.get("taskId"),
                   "agentId": i.get("agentId"), "goal": (i.get("goal") or "")[:120],
                   "sessionFrom": i.get("sessionFrom"),
                   "confidence": i.get("confidence"), "reason": i.get("reason")}
                  for i in (sub.get("items") or [])],
        "reviews": sub.get("reviews") or [],
        "outbox": sub.get("outbox") or [],
        "applied": res.get("applied") or [],
        "rejected": res.get("rejected") or [],
    }


def collect_decisions() -> list[dict]:
    """各任务的 Decision（结构化决策）+ 落地结果。

    包含两类：
      · 任务级决策 —— tasks/Txxx/decision.json
      · 无任务轮次（用户消息轮）的决策 —— router/decisions/<triggerId>.json
    """
    out = []
    for d in sorted((ROOT / "tasks").glob("T*")):
        dp = d / "decision.json"
        if not dp.exists():
            continue
        try:
            doc = json.loads(dp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        out.append(_decision_entry(d.name, doc))
    rdir = ROOT / "router" / "decisions"
    if rdir.exists():
        for dp in sorted(rdir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:30]:
            try:
                doc = json.loads(dp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            out.append(_decision_entry("(用户消息轮)", doc))
    return out


def collect_registry() -> dict:
    import core
    return core.load_registry()


# 当前看板端口（main 里按 --port 设置）—— pid 文件按端口分
PORT = 8788


def collect() -> dict:
    tasks = []
    if sa.TASKS_DIR.exists():
        for d in sorted(sa.TASKS_DIR.glob("T*")):
            sp = d / sa.STATE_FILE
            if not sp.exists():
                continue
            st = json.loads(sp.read_text(encoding="utf-8"))
            asgs = []
            for a in st.get("assignments", []):
                notes = sa.notes_path(st["task_id"], a["id"], create=False)
                asgs.append(
                    {
                        **a,
                        "instruction_text": read_text(a.get("instruction")),
                        "response_text": read_text(a.get("response")),
                        "notes_text": read_text(str(notes)) if notes.exists() else "",
                        "events": [e for e in sa.read_events(st["task_id"]) if e.get("assignment") == a["id"]],
                    }
                )
            tasks.append(
                {
                    "task_id": st["task_id"],
                    "title": st["title"],
                    "goal": st["goal"],
                    "status": st["status"],
                    "source": st.get("source", "manual"),
                    "source_ref": st.get("source_ref", {}),
                    "authorized_actors": st.get("authorized_actors", []),
                    "created_at": st.get("created_at"),
                    "updated_at": st.get("updated_at"),
                    "acceptance": st.get("acceptance_lines", []),
                    "context_files": st.get("context_files", []),
                    "task_notes": st.get("task_notes", ""),
                    "task_md": read_text(str(d / "task.md")),
                    "assignments": asgs,
                    "events": sa.read_events(st["task_id"]),
                }
            )
    return {
        "tasks": tasks,
        "workers": list(sa.WORKERS),
        "generated_at": sa.now(),
        "messages": collect_messages(),
        "router": collect_router(),
        "daemons": collect_daemons(PORT),
        "bridge": collect_bridge(),
        "decisions": collect_decisions(),
        "outbox": core.list_outbox(),
        "registry": collect_registry(),
        "chains": collect_chains(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SuperAgentBoard/1.0"

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                return
            if path == "/api/state":
                self._json(collect())
                return
            if path == "/api/chain-raw":
                # 消息链路某个阶段的原文（按需取，不随 /api/state 轮询）
                q = parse_qs(urlparse(self.path).query)
                mid = (q.get("message_id") or [""])[0]
                try:
                    idx = int((q.get("index") or ["0"])[0])
                except ValueError:
                    idx = 0
                try:
                    import chain_view
                    self._json(chain_view.stage_raw(mid, idx) or {"error": "没有这一段"})
                except Exception as e:
                    self._json({"error": str(e)}, 500)
                return
            if path == "/api/invariants":
                # 不变式校验（只读）：给看板一个"状态是否自相矛盾"的答案
                try:
                    import invariants as inv
                except Exception as e:
                    self._json({"error": f"invariants 不可用：{e}"}, 500)
                    return
                rep = inv.run()
                errs = [v for v in rep.violations if v["level"] == "ERROR"]
                warns = [v for v in rep.violations if v["level"] == "WARN"]
                self._json({"ok": not errs, "errors": len(errs), "warnings": len(warns),
                            "rules": inv.RULES, "violations": rep.violations[-40:]})
                return
            if path == "/api/policy":
                # 策略摘要（谁能发起命令）
                try:
                    import policy
                except Exception as e:
                    self._json({"error": f"policy 不可用：{e}"}, 500)
                    return
                self._json({"ok": True, "policy": policy.policy_summary()})
                return
            if path == "/api/health":
                self._json({"ok": True, "root": str(ROOT)})
                return
            if path.startswith("/api/file"):
                # 只允许读 super-agent 目录内的**状态类**文件：
                #   - 路径必须在 ROOT 内（用 is_relative_to，前缀字符串比较会被 ../ 绕过）
                #   - 且必须落在状态目录白名单里（tasks/outbox/router），
                #     否则 lark_config.json / policy.json 里的标识符会被无鉴权读走
                q = urlparse(self.path).query
                target = ""
                for part in q.split("&"):
                    if part.startswith("path="):
                        from urllib.parse import unquote

                        target = unquote(part[5:])
                rp = pathlib.Path(target).resolve()
                root = ROOT.resolve()
                if not rp.is_relative_to(root):
                    self._json({"error": "path outside super-agent root"}, 403)
                    return
                try:
                    rel = rp.relative_to(root).parts
                except ValueError:
                    rel = ()
                if not rel or rel[0] not in FILE_READABLE_DIRS:
                    self._json({"error": f"read not allowed under {rel[0] if rel else '?'}"}, 403)
                    return
                if not rp.is_file():
                    self._json({"error": "not found"}, 404)
                    return
                self._send(200, rp.read_bytes(), "text/plain; charset=utf-8")
                return
            self._json({"error": "not found"}, 404)
        except Exception:
            # 细节只进服务端日志，不回给客户端（traceback 里含绝对路径与内部结构）
            traceback.print_exc()
            self._json({"error": "internal error"}, 500)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json({"error": "bad content-length"}, 400)
            return
        if n < 0:
            self._json({"error": "bad content-length"}, 400)
            return
        if n > MAX_BODY:
            self._json({"error": "body too large"}, 413)
            return
        raw = self.rfile.read(n) if n else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, 400)
            return
        try:
            if path == "/api/intervene":
                task_id = payload.get("task")
                assignment_id = payload.get("assignment")
                action = payload.get("action")
                if not task_id or not action:
                    self._json({"error": "task 与 action 必填"}, 400)
                    return
                if not sa.valid_task_id(task_id):
                    self._json({"error": "非法 task id"}, 400)
                    return
                if action in ("comment", "approve", "reject", "cancel", "redirect") and not assignment_id:
                    self._json({"error": "该操作需要 assignment"}, 400)
                    return
                res = sa.intervene(
                    task_id,
                    assignment_id,
                    action,
                    text=payload.get("text", "") or "",
                    to_worker=payload.get("to", "") or "",
                )
                self._json({"ok": True, "result": res})
                return
            if path == "/api/lark-reply":
                task_id = payload.get("task")
                text = payload.get("text") or ""
                if not task_id or not text.strip():
                    self._json({"error": "task 与 text 必填"}, 400)
                    return
                if not sa.valid_task_id(task_id):
                    self._json({"error": "非法 task id"}, 400)
                    return
                st = sa.load_state(task_id)
                mid = (st.get("source_ref") or {}).get("message_id")
                if not mid:
                    self._json({"error": "该任务没有飞书来源，无法回执"}, 400)
                    return
                import lark_bridge
                lark_bridge.send_reply(mid, text)
                sa.log_event(task_id, "feishu_replied", "human", "结果已回传飞书", message_id=mid)
                self._json({"ok": True, "message_id": mid})
                return
            if path == "/api/assign":
                task_id = payload.get("task")
                worker = payload.get("worker") or "general-purpose"
                instruction = payload.get("instruction") or ""
                brief = payload.get("brief") or ""
                if not task_id or not instruction.strip():
                    self._json({"error": "task 与 instruction 必填"}, 400)
                    return
                if not sa.valid_task_id(task_id):
                    self._json({"error": "非法 task id"}, 400)
                    return
                if worker not in sa.WORKERS:
                    self._json({"error": f"worker 必须是 {sa.WORKERS} 之一"}, 400)
                    return
                tmp = pathlib.Path("/tmp") / f"sa-assign-{threading.get_ident()}.md"
                tmp.write_text(instruction, encoding="utf-8")
                try:
                    sa.cmd_assign(
                        argparse.Namespace(task=task_id, worker=worker, instruction_file=str(tmp), brief=brief)
                    )
                finally:
                    tmp.unlink(missing_ok=True)
                self._json({"ok": True})
                return
            if path == "/api/router/model":
                # 切 router 的模型 / 思考等级：写 model.json，运行中的 router 下一轮生效
                model = (payload.get("model") or "").strip()
                restore = bool(payload.get("restore"))
                thinking = (payload.get("thinking") or "").strip().lower()
                if router_mod is None:
                    self._json({"error": "router 模块不可用"}, 500)
                    return
                st = router_mod.load_model_state()
                if thinking:
                    if thinking not in router_mod.THINKING_LEVELS:
                        self._json({"error": "思考等级要写成 off|minimal|low|medium|high|xhigh|max"}, 400)
                        return
                    st["thinking"] = thinking
                if restore:
                    target = st.get("boot_primary") or "minimax-cn/MiniMax-M3.1-Flash"
                elif model:
                    if "/" not in model:
                        self._json({"error": "模型要写成 provider/model"}, 400)
                        return
                    target = model
                elif not thinking:
                    self._json({"error": "model / thinking / restore 至少给一个"}, 400)
                    return
                else:
                    target = st.get("primary") or "minimax-cn/MiniMax-M3.1-Flash"
                st["primary"] = target
                st["degraded"] = False
                st["degraded_to"] = ""
                st["degraded_at"] = ""
                router_mod.save_model_state(st)
                self._json({"ok": True, "primary": target,
                            "thinking": st.get("thinking") or router_mod.DEFAULT_THINKING,
                            "hint": "运行中的 router 下一轮生效"})
                return
            if path == "/api/policy/grant":
                task_id = payload.get("task") or ""
                person = payload.get("person") or ""
                if not task_id or not person:
                    self._json({"error": "task 与 person 必填"}, 400)
                    return
                if not sa.valid_task_id(task_id):
                    self._json({"error": "非法 task id"}, 400)
                    return
                import policy
                try:
                    state = sa.load_state(task_id)
                except SystemExit:
                    self._json({"error": f"任务 {task_id} 不存在"}, 404)
                    return
                oid = policy.resolve_person(person)
                if not oid:
                    self._json({"error": f"人员目录里找不到「{person}」"}, 400)
                    return
                ref = state.get("source_ref") or {}
                g = policy.grant_actor(task_id, oid, name=policy.person_name(oid),
                                       reason=payload.get("reason") or "",
                                       chat_id=ref.get("chat_id") or "",
                                       question=payload.get("question") or "")
                sa.log_event(task_id, "actor_granted", "human",
                             f"临时授权 {g.get('name')} 在本任务会话内发言", actor_open_id=oid)
                self._json({"ok": True, "granted": g})
                return
            if path == "/api/policy/revoke":
                task_id = payload.get("task") or ""
                person = payload.get("person") or ""
                if not task_id or not person:
                    self._json({"error": "task 与 person 必填"}, 400)
                    return
                if not sa.valid_task_id(task_id):
                    self._json({"error": "非法 task id"}, 400)
                    return
                import policy
                oid = policy.resolve_person(person)
                if not oid:
                    self._json({"error": f"人员目录里找不到「{person}」"}, 400)
                    return
                ok = policy.revoke_actor(task_id, oid)
                if ok:
                    sa.log_event(task_id, "actor_revoked", "human",
                                 f"撤销 {person} 的临时授权", actor_open_id=oid)
                self._json({"ok": ok})
                return
            if path == "/api/router/chain":
                # 链式派工：允许在「指派结束」轮里再派工，但连续次数有上限
                chain = sa.load_chain_config()
                enabled = payload.get("enabled")
                if enabled is None:
                    enabled = chain["enabled"]
                raw = payload.get("max_consecutive_dispatches",
                                  chain["max_consecutive_dispatches"])
                try:
                    n = int(raw)
                except (TypeError, ValueError):
                    self._json({"error": "max_consecutive_dispatches 要整数"}, 400)
                    return
                if n < 0 or n > 10:
                    self._json({"error": "上限要在 0–10 之间"}, 400)
                    return
                cfg = sa.save_chain_config(bool(enabled), n)
                self._json({"ok": True, "chain": cfg})
                return
            if path == "/api/router/rotate":
                # 轮转 router session：归档当前会话，重启 router 开一个干净的
                if router_mod is None:
                    self._json({"error": "router 模块不可用"}, 500)
                    return
                out = subprocess.run(
                    [sys.executable, str(pathlib.Path(__file__).resolve().parent / "router.py"), "rotate"],
                    capture_output=True, text=True, timeout=120)
                ok = out.returncode == 0
                self._json({"ok": ok, "output": (out.stdout or out.stderr or "").strip()[-600:]},
                           200 if ok else 500)
                return
            if path == "/api/new-task":
                # id 一律由服务端分配：外部传入的 id 可能是 `../` 之类的路径片段
                args = argparse.Namespace(
                    id=None,
                    title=payload.get("title") or "未命名任务",
                    goal=payload.get("goal") or "",
                    context=payload.get("context") or None,
                    context_file=None,
                    acceptance=payload.get("acceptance") or None,
                )
                sa.cmd_new_task(args)
                self._json({"ok": True})
                return
            self._json({"error": "not found"}, 404)
        except SystemExit as e:
            self._json({"error": str(e) or "操作失败"}, 400)
        except Exception:
            traceback.print_exc()
            self._json({"error": "internal error"}, 500)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址；非 127.0.0.1 会把无鉴权的看板暴露到网络上")
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"⚠️  警告：监听 {a.host}，看板无鉴权，任何人都能派单/干预任务。",
              file=sys.stderr)
    # 自己登记 pid —— 否则看板在「运行状态」里显示「已停止」（它自己明明在服务）
    global PORT
    PORT = a.port
    pid_file = board_pid_file(a.port)
    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"super-agent 看板: http://{a.host}:{a.port}  (Ctrl+C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        srv.server_close()
        try:
            if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
