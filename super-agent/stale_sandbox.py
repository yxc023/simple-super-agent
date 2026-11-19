#!/usr/bin/env python3
"""滞留沙箱进程巡检（可观测性补件）。

背景（真实事故）：一个验证脚本（`event-channel-harness.py` 一类）启动后没退出，
它只用桩替换了发送函数、却没有把 outbox 目录也挪走 —— 于是它每一轮都去扫
**生产** outbox，把记录用桩「投递」成 `succeeded`。用户侧什么都没收到，账面上
却是「已投递」，一个月内反复产生对不上的账。

状态根隔离（`SA_ROOT`）只能保护**此后启动**的进程：已经在跑的老进程不受任何事后
保护。这类进程必须靠巡检发现、人工处置。本工具就是那条巡检：

    python3 super-agent/stale_sandbox.py                    # 只报告（默认，不改动任何进程）
    python3 super-agent/stale_sandbox.py --min-age 60       # 放宽存活阈值
    python3 super-agent/stale_sandbox.py --json             # 机器可读
    python3 super-agent/stale_sandbox.py --kill             # 终止命中的「连生产状态」进程
    python3 super-agent/stale_sandbox.py --kill --include-sandbox   # 连已隔离的一起清

判定链（三条同时成立才算「命中」）：

  1. 命令行里某个参数的 basename 命中脚本画像（默认：
     `event-channel-harness.py`、`*harness*.py`、`verify-*.py`、`verify_*.py`、
     `sandbox-*.py`、`*-sandbox*.py`、`stub-*.py`、`*-stub*.py`）；
  2. 存活时长 ≥ `--min-age`（默认 300s）—— 验证脚本本来就该几秒内退出；
  3. 状态根是**生产**：没有设 `SA_ROOT`（缺省就是生产代码目录），或 `SA_ROOT`
     指向代码目录本身。读不到环境时按 `unknown` 处理，**与生产态同样报警**
     （宁可多报一条，也不要漏掉一个正在吞 ack 的进程）。

永不命中、也永不终止：本进程及其祖先链，以及真守护进程
（`lark_bridge.py follow`、`router.py run`、`worker_runner.py run`、`lark_bridge.sh`）。

退出码：0 = 未发现需处置项；1 = 发现需处置项且给了 `--strict`；
        0 = 发现需处置项（不给 `--strict` 时只报告，不算失败）；
        2 = 巡检本身失败（拿不到进程列表等）。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
# 生产状态根：默认就是代码目录（与 sa.SA_ROOT 的缺省口径一致）。
# 单独给 --prod-root 是为了「在沙箱里也能巡检生产」这种运维场景。
DEFAULT_PROD_ROOT = pathlib.Path(os.environ.get("SA_PROD_ROOT") or ROOT).resolve()

# 脚本画像：验证 / 演练 / 桩替身一类「跑完就该退出」的脚本
DEFAULT_SCRIPT_PATTERNS = [
    "event-channel-harness.py",
    "*harness*.py",
    "*harness*",
    "verify-*.py",
    "verify_*.py",
    "sandbox-*.py",
    "*-sandbox*.py",
    "stub-*.py",
    "*-stub*.py",
]

# 真守护进程：任何情况下都不算滞留、绝不被终止
PROTECTED_CMDLINE = [
    "lark_bridge.py follow",
    "lark_bridge.py pull --follow",
    "router.py run",
    "worker_runner.py run",
    "lark_bridge.sh",
    "stale_sandbox.py",
    "selftest.py",
]

_UNKNOWN = object()          # 读不到 SA_ROOT（既不是「未设置」也不是「设了」）
DEFAULT_MIN_AGE = 300        # 5 分钟 —— 验证脚本活过这个时长基本就是忘了退


# ------------------------------------------------------------------ 进程枚举


def parse_etime(raw: str) -> int:
    """把 `ps` 的 etime 解析成秒：`ss` / `mm:ss` / `hh:mm:ss` / `dd-hh:mm:ss`。"""
    s = (raw or "").strip()
    if not s:
        return 0
    days = 0
    if "-" in s:
        d, _, s = s.partition("-")
        try:
            days = int(d)
        except ValueError:
            days = 0
    parts = []
    for x in s.split(":"):
        try:
            parts.append(int(x))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts[-3], parts[-2], parts[-1]
    return days * 86400 + h * 3600 + m * 60 + sec


def list_processes() -> list[dict]:
    """`ps -Ao pid=,ppid=,etime=,command=` → [{pid, ppid, age, cmd}]。

    只用 POSIX `ps` 的通用字段（macOS / Linux 都支持），不依赖 /proc。
    """
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,ppid=,etime=,command="],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"拿不到进程列表：{e}") from e
    procs = []
    for line in (out.stdout or "").splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, etime, cmd = parts
        try:
            pid_i, ppid_i = int(pid), int(ppid)
        except ValueError:
            continue
        procs.append({"pid": pid_i, "ppid": ppid_i,
                      "age": parse_etime(etime), "etime": etime, "cmd": cmd})
    return procs


def ancestors(procs: list[dict], pid: int) -> set[int]:
    """本进程的祖先链（巡检/终止都要跳过它们，别把自己家拆了）。"""
    by_pid = {p["pid"]: p for p in procs}
    out, cur = set(), pid
    while cur in by_pid and cur not in out:
        out.add(cur)
        cur = by_pid[cur]["ppid"]
    return out


# ------------------------------------------------------------------ 判定


def script_hit(cmd: str, patterns: list[str] | None = None) -> str:
    """命令行里是否有参数命中脚本画像；命中则返回那个 basename。"""
    pats = [p.lower() for p in (patterns or DEFAULT_SCRIPT_PATTERNS)]
    for tok in (cmd or "").split():
        base = pathlib.Path(tok).name.lower()
        if not base:
            continue
        for pat in pats:
            if fnmatch.fnmatch(base, pat):
                return base
    return ""


def is_protected(cmd: str) -> bool:
    """真守护进程 / 巡检自身：永不命中、永不终止。"""
    c = cmd or ""
    return any(p in c for p in PROTECTED_CMDLINE)


def read_sa_root(pid: int):
    """读某个进程的 `SA_ROOT` 环境变量。

    返回：
      · 字符串        —— 该进程设了 SA_ROOT
      · None          —— 读到了环境，但没有 SA_ROOT（缺省 = 生产状态根）
      · _UNKNOWN      —— 读不到环境（按生产态一样报警，不当作已隔离）
    """
    # Linux：/proc/<pid>/environ
    ent = pathlib.Path(f"/proc/{pid}/environ")
    if ent.exists():
        try:
            for kv in ent.read_bytes().split(b"\0"):
                if kv.startswith(b"SA_ROOT="):
                    return kv.decode("utf-8", "replace").partition("=")[2]
            return None
        except OSError:
            return _UNKNOWN
    # macOS：`ps eww` 把环境变量附在命令行后面
    try:
        out = subprocess.run(["ps", "eww", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return _UNKNOWN
    text = out.stdout or ""
    if not text.strip():
        return _UNKNOWN
    env_readable = False
    for tok in text.split():
        if tok.startswith("SA_ROOT="):
            return tok.partition("=")[2]
        # 形如 KEY=VALUE 的 token 出现 → 环境确实拿到了（只是没有 SA_ROOT）
        head, sep, _ = tok.partition("=")
        if sep and head.replace("_", "").isalnum() and head[:1].isalpha():
            env_readable = True
    # 一个 KEY=VALUE 都没有 → 分不清「没设」与「读不到」，保守按未知处理（同样报警）
    return None if env_readable else _UNKNOWN


def state_verdict(pid: int, prod_root: pathlib.Path) -> tuple[str, str]:
    """该进程的「状态根」判定：production / sandbox / unknown。"""
    raw = read_sa_root(pid)
    if raw is _UNKNOWN:
        return "unknown", ""
    if not raw:
        return "production", ""
    try:
        p = pathlib.Path(raw).expanduser().resolve()
    except OSError:
        return "sandbox", raw
    if str(p) == str(pathlib.Path(prod_root).resolve()):
        return "production", str(p)
    return "sandbox", str(p)


def scan(min_age: int = DEFAULT_MIN_AGE, prod_root: pathlib.Path = DEFAULT_PROD_ROOT,
         patterns: list[str] | None = None,
         include_sandbox: bool = False,
         procs: list[dict] | None = None) -> dict:
    """巡检：返回 {hits, isolated, protected, skipped, min_age, prod_root}。

    hits       需处置：脚本画像 + 存活超阈值 + 状态根是生产（或未知）
    isolated   同样是滞留脚本，但 SA_ROOT 指到了别处（没连生产，可选清理）
    protected  被显式排除的真守护进程（只作信息，永不处置）
    """
    plist = procs if procs is not None else list_processes()
    skip = ancestors(plist, os.getpid())
    hits, isolated, protected = [], [], []
    for p in plist:
        cmd = p["cmd"]
        if p["pid"] in skip:
            continue
        if is_protected(cmd):
            if any(k in cmd for k in ("lark_bridge", "router.py", "worker_runner")):
                protected.append({"pid": p["pid"], "age": p["age"], "cmd": cmd[:200]})
            continue
        base = script_hit(cmd, patterns)
        if not base:
            continue
        verdict, root = state_verdict(p["pid"], prod_root)
        row = {"pid": p["pid"], "ppid": p["ppid"], "age": p["age"],
               "age_human": human_age(p["age"]), "script": base,
               "state_root": verdict, "sa_root": root, "cmd": cmd[:300]}
        stale = p["age"] >= int(min_age)
        if not stale:
            continue
        if verdict == "sandbox":
            if include_sandbox:
                row["reason"] = "沙箱隔离，但仍滞留未退"
                isolated.append(row)
            else:
                isolated.append(row)
        else:
            row["reason"] = ("连到生产状态根（未设 SA_ROOT，缺省即生产）"
                             if verdict == "production"
                             else "读不到 SA_ROOT，按生产态处理（宁报勿漏）")
            hits.append(row)
    return {"scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "min_age": int(min_age), "prod_root": str(prod_root),
            "hits": hits, "isolated": isolated, "protected": protected}


def human_age(seconds: int) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d{h}h{m}m"
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


# ------------------------------------------------------------------ 处置


def terminate(rows: list[dict], grace: float = 3.0, procs: list[dict] | None = None,
              prod_root: pathlib.Path = DEFAULT_PROD_ROOT) -> list[dict]:
    """终止给定进程（先 SIGTERM，宽限期后 SIGKILL）。返回逐条结果。

    二次防护：真守护进程、本进程与祖先链一律跳过（即使调用方传进来）。
    """
    plist = procs if procs is not None else list_processes()
    skip = ancestors(plist, os.getpid())
    results = []
    for row in rows:
        pid = int(row["pid"])
        cmd = row.get("cmd") or ""
        if pid in skip or is_protected(cmd):
            results.append({"pid": pid, "result": "skipped-protected"})
            continue
        # 再判一次状态根：从扫描到处置之间进程可能已被换掉（pid 复用）
        verdict, _ = state_verdict(pid, prod_root)
        if verdict != row.get("state_root"):
            results.append({"pid": pid, "result": f"skipped-changed（{row.get('state_root')} → {verdict}）"})
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            results.append({"pid": pid, "result": "already-gone"})
            continue
        except PermissionError as e:
            results.append({"pid": pid, "result": f"skipped-permission（{e}）"})
            continue
        deadline = time.time() + max(0.0, grace)
        alive = True
        while time.time() < deadline:
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            except PermissionError:
                pass
        if not alive:
            results.append({"pid": pid, "result": "terminated"})
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            results.append({"pid": pid, "result": "killed"})
        except ProcessLookupError:
            results.append({"pid": pid, "result": "terminated"})
        except PermissionError as e:
            results.append({"pid": pid, "result": f"skipped-permission（{e}）"})
    return results


def render(res: dict, as_json: bool = False, action: list[dict] | None = None) -> str:
    if as_json:
        out = dict(res)
        if action is not None:
            out["action"] = action
        return json.dumps(out, ensure_ascii=False, indent=2)
    lines = [
        f"滞留沙箱进程巡检：{res['scanned_at']}",
        f"  脚本画像 = 验证/演练一类脚本   存活阈值 = {human_age(res['min_age'])}",
        f"  生产状态根 = {res['prod_root']}",
    ]
    hits, isolated = res["hits"], res["isolated"]
    if hits:
        lines.append(f"  ⚠ 需处置 {len(hits)} 个（脚本滞留 且 连生产状态）：")
        for r in hits:
            lines.append(f"    · pid {r['pid']}（{r['age_human']}，{r['script']}，"
                         f"状态根={r['state_root']}）：{r['reason']}")
            lines.append(f"      命令行：{r['cmd'][:160]}")
    else:
        lines.append("  ✓ 未发现「脚本滞留 + 连生产状态」的进程")
    if isolated:
        lines.append(f"  · 另有 {len(isolated)} 个脚本进程已隔离（SA_ROOT 指到别处，仍在跑；默认不处置）：")
        for r in isolated:
            lines.append(f"    · pid {r['pid']}（{r['age_human']}，{r['script']}，"
                         f"SA_ROOT={r['sa_root'] or '?'}）")
    if res["protected"]:
        lines.append("  · 已排除的真守护进程（永不处置）："
                     + "、".join(f"pid {p['pid']}" for p in res["protected"]))
    if action:
        lines.append("  处置结果：")
        for a in action:
            lines.append(f"    · pid {a['pid']}：{a['result']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="stale_sandbox.py",
        description="巡检滞留的验证脚本进程（默认只报告；--kill 才终止）")
    ap.add_argument("--min-age", type=int, default=DEFAULT_MIN_AGE,
                    help=f"存活多久算滞留（秒，默认 {DEFAULT_MIN_AGE}）")
    ap.add_argument("--prod-root", default=str(DEFAULT_PROD_ROOT),
                    help="生产状态根（默认 = 本脚本所在目录）")
    ap.add_argument("--pattern", action="append", default=None,
                    help=f"追加脚本画像（glob，可多次；默认 {DEFAULT_SCRIPT_PATTERNS}）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--kill", action="store_true",
                    help="终止命中的进程（默认只报告）")
    ap.add_argument("--include-sandbox", action="store_true",
                    help="连「已隔离但仍滞留」的脚本进程一起清（需配合 --kill）")
    ap.add_argument("--grace", type=float, default=3.0,
                    help="SIGTERM 后等多久再 SIGKILL（秒，默认 3）")
    ap.add_argument("--strict", action="store_true",
                    help="发现需处置项时以退出码 1 结束（适合放巡检流水线）")
    a = ap.parse_args(argv)

    patterns = None
    if a.pattern:
        patterns = list(DEFAULT_SCRIPT_PATTERNS) + [p for p in a.pattern if p]
    try:
        procs = list_processes()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2
    res = scan(min_age=a.min_age, prod_root=pathlib.Path(a.prod_root),
               patterns=patterns, procs=procs)

    action = None
    if a.kill:
        targets = res["hits"] + (res["isolated"] if a.include_sandbox else [])
        action = terminate(targets, grace=a.grace, procs=procs,
                           prod_root=pathlib.Path(a.prod_root))

    print(render(res, as_json=a.json, action=action))
    if a.strict and res["hits"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())