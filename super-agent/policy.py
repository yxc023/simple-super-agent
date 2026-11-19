#!/usr/bin/env python3
"""准入策略 —— Step 1：**谁能发起命令**。

规则来自用户 2026-11-18 的明确要求：

  1. **私聊**：只允许 owner 本人发起命令（别人的私聊一律不受理）。
  2. **群聊**：只允许 owner 指定过的群；在这些群里，**其他人也可以发起命令**。
  3. **任务级临时授权**：某个任务里临时加了人 → 这个人可以在**该任务的会话里**
     发言（典型场景：bot 问 A 的事，A 来回复 bot）。

设计取舍（为什么这样切）：

  · **只判「发起命令」这一件事**，不判「能看什么」—— 读类动作默认放行，
    否则群里天天被拒，可用性归零。
  · **任务授权是任务的属性**，存在任务 state 的 `authorized_actors` 里，
    跟着任务走、随任务归档；看板能直接展示与撤销。
  · **授权只在任务自己的会话里有效**：被授权的人在别的群里说话不算数。
  · **判定入口只有一个**（`check_command`），桥接的「事件路径」与「轮询兜底路径」
    都调它 —— 门只有一处，不会出现「长连接放行、轮询拦住」的不一致。
"""
from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parent


def policy_file() -> pathlib.Path:
    """策略文件路径：SA_POLICY_FILE > 代码目录下的 policy.json。

    自检/演练用 SA_POLICY_FILE 指向临时策略（可以放行一切），
    避免新规则把既有测试里的假事件全挡掉。
    """
    raw = (os.environ.get("SA_POLICY_FILE") or "").strip()
    if raw:
        return pathlib.Path(raw).expanduser().resolve()
    return ROOT / "policy.json"


# 判定结果码（与 Task Center 的 reason_code 同一套语义）
ALLOW = "allow"
DENY_PRIVATE = "deny_private_chat"
DENY_GROUP = "deny_group_not_allowed"
DENY_NO_POLICY = "deny_no_policy"
ALLOW_GROUP = "allow_group"
ALLOW_OWNER = "allow_owner"
ALLOW_TASK_GRANT = "allow_task_grant"


# ------------------------------------------------------------------ 配置

DEFAULT_POLICY = {
    "owner": {"open_id": "", "name": ""},
    "private_chat": {"owner_only": True},
    "groups": {"allow": []},
    "people": {},
    "grant_ttl_hours": 48,
}


def load_policy() -> dict:
    """读策略配置（坏文件退回默认，不让配置问题挡住消息处理）。"""
    cfg = json.loads(json.dumps(DEFAULT_POLICY))
    try:
        raw = json.loads(policy_file().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in ("owner", "private_chat", "groups", "people"):
                    if isinstance(v, dict):
                        cfg[k].update(v)
                    # 类型不对（owner 写成字符串之类）就保留默认结构，
                    # 否则后面 cfg["owner"].get(...) 会 AttributeError
                elif k == "grant_ttl_hours":
                    try:
                        cfg[k] = int(v)
                    except (TypeError, ValueError):
                        pass
                else:
                    cfg[k] = v
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_policy(cfg: dict) -> dict:
    policy_file().write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return cfg


def owner_id(cfg: dict | None = None) -> str:
    return str((cfg or load_policy()).get("owner", {}).get("open_id") or "")


def resolve_person(name: str, cfg: dict | None = None) -> str:
    """按名字查人员目录 → open_id。支持全名或包含匹配。"""
    cfg = cfg or load_policy()
    people = cfg.get("people") or {}
    name = (name or "").strip()
    if not name:
        return ""
    # 也允许直接给 open_id
    if name.startswith("ou_"):
        return name
    if name in people:
        return str(people[name])
    # 包含匹配：只认「至少 2 个字」且长度差不超过 2 的候选，
    # 避免单字名字命中「甲乙丙」这类无关条目
    for k, v in people.items():
        if len(name) >= 2 and (name in k or k in name) and abs(len(k) - len(name)) <= 2:
            return str(v)
    return ""


def person_name(open_id: str, cfg: dict | None = None) -> str:
    cfg = cfg or load_policy()
    for k, v in (cfg.get("people") or {}).items():
        if str(v) == str(open_id):
            return k
    return open_id


# ------------------------------------------------------------------ 任务级授权

def _tasks_dir() -> pathlib.Path:
    raw = (os.environ.get("SA_TASKS_DIR") or "").strip()
    if raw:
        return pathlib.Path(raw).expanduser().resolve()
    base = (os.environ.get("SA_ROOT") or "").strip()
    return (pathlib.Path(base).expanduser().resolve() if base else ROOT) / "tasks"


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse(ts) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=_now().tzinfo)


def grant_actor(task_id: str, open_id: str, *, name: str = "", reason: str = "",
                chat_id: str = "", question: str = "",
                ttl_hours: int | None = None) -> dict:
    """给某个任务临时加一个人（可发消息）。

    存在任务 state 的 `authorized_actors` 里 —— 授权是**任务的属性**，
    跟着任务走，随任务一起归档。
    """
    st_path = _tasks_dir() / task_id / "state.json"
    try:
        st = json.loads(st_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"无法读取任务状态：{task_id}（{e}）") from e
    cfg = load_policy()
    ttl = ttl_hours if ttl_hours is not None else int(cfg.get("grant_ttl_hours", 48))
    expires = (_now() + timedelta(hours=ttl)).isoformat(timespec="seconds")
    grants = st.get("authorized_actors")
    if not isinstance(grants, list):
        grants = []
    grants = [g for g in grants if isinstance(g, dict) and str(g.get("open_id")) != str(open_id)]
    grants.append({
        "open_id": open_id,
        "name": name or person_name(open_id, cfg),
        "reason": reason,
        "question": question,
        "chat_id": chat_id,
        "granted_at": _now().isoformat(timespec="seconds"),
        "expires_at": expires,
        "used_count": 0,
    })
    st["authorized_actors"] = grants
    st_path.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return grants[-1]


def revoke_actor(task_id: str, open_id: str) -> bool:
    st_path = _tasks_dir() / task_id / "state.json"
    if not st_path.is_file():
        return False
    try:
        st = json.loads(st_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    grants = st.get("authorized_actors") or []
    keep = [g for g in grants
            if isinstance(g, dict) and str(g.get("open_id")) != str(open_id)]
    if len(keep) == len(grants):
        return False
    st["authorized_actors"] = keep
    st_path.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def active_grants(task_id: str) -> list[dict]:
    """任务里当前有效的临时授权（过期的不算）。"""
    st_path = _tasks_dir() / task_id / "state.json"
    try:
        st = json.loads(st_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    now = _now()
    out = []
    for g in st.get("authorized_actors") or []:
        exp = _parse(g.get("expires_at"))
        if exp is None or exp > now:
            out.append(g)
    return out


def find_grant_for_sender(sender_id: str, chat_id: str = "",
                          thread_id: str = "") -> tuple[str, dict] | tuple[None, None]:
    """找「这个人在这个会话里被临时授权」的任务。

    返回 (task_id, grant)；没找到返回 (None, None)。

    约束：授权只在**该任务自己的会话**里有效 —— 被授权的人在别的群里说话
    不算数（否则「加一个人进来」等于「给他开了个全频道喇叭」）。
    """
    if not sender_id:
        return None, None
    tasks_dir = _tasks_dir()
    if not tasks_dir.is_dir():
        return None, None
    for d in sorted(tasks_dir.glob("T*")):
        try:
            st = json.loads((d / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        grants = st.get("authorized_actors") or []
        if not grants:
            continue
        ref = st.get("source_ref") or {}
        task_chat = str(ref.get("chat_id") or "")
        task_thread = str(ref.get("thread_id") or "")
        now = _now()
        for g in grants:
            if str(g.get("open_id")) != str(sender_id):
                continue
            exp = _parse(g.get("expires_at"))
            if exp is not None and exp <= now:
                continue
            # 会话必须对得上：授权里记了 chat_id 就必须一致；
            # 没记就用任务来源会话兜底。
            want_chat = str(g.get("chat_id") or "") or task_chat
            if want_chat:
                # 调用方没带 chat_id 时**不能**当作「不约束」——
                # 那等于授权可以在任何会话生效。查不到会话就宁可拒绝。
                if not chat_id or str(want_chat) != str(chat_id):
                    continue
            return d.name, g
    return None, None


def mark_grant_used(task_id: str, open_id: str) -> None:
    """授权被用过 → 计数 +1（审计用；默认不因次数耗尽而失效）。"""
    st_path = _tasks_dir() / task_id / "state.json"
    try:
        st = json.loads(st_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    hit = False
    for g in st.get("authorized_actors") or []:
        if not isinstance(g, dict):
            continue
        if str(g.get("open_id")) == str(open_id):
            g["used_count"] = int(g.get("used_count") or 0) + 1
            g["last_used_at"] = _now().isoformat(timespec="seconds")
            hit = True
            break
    if not hit:
        return
    st_path.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ 判定入口

def check_command(*, chat_type: str, chat_id: str, sender_id: str,
                  sender_name: str = "", thread_id: str = "",
                  cfg: dict | None = None) -> dict:
    """判定「这条消息能不能作为命令受理」。

    返回 {allow, reason_code, detail, grant_task, grant}。
    """
    cfg = cfg or load_policy()
    owner = str((cfg.get("owner") or {}).get("open_id") or "")
    is_p2p = (chat_type or "").lower() == "p2p"

    # 0) 没有策略配置 → 不拦（保持旧行为，避免配错就全线瘫痪）
    if not owner and not (cfg.get("groups") or {}).get("allow"):
        return {"allow": True, "reason_code": ALLOW, "detail": "未配置策略，按旧行为放行",
                "grant_task": None, "grant": None}

    # 1) owner 本人：任何会话都放行
    if owner and sender_id == owner:
        return {"allow": True, "reason_code": ALLOW_OWNER,
                "detail": "owner 本人", "grant_task": None, "grant": None}

    # 2) 任务级临时授权：**比群规则更具体**，先判。
    #    顺序很重要：被授权的人即使在允许群里，也必须拿到 grant 上下文 ——
    #    否则 router 不知道这条消息该延续哪个任务（他可能是来回答问题的）。
    task_id, grant = find_grant_for_sender(sender_id, chat_id=chat_id, thread_id=thread_id)
    if task_id:
        return {"allow": True, "reason_code": ALLOW_TASK_GRANT,
                "detail": f"任务 {task_id} 的临时授权（{grant.get('name') or sender_id}）",
                "grant_task": task_id, "grant": grant}

    # 3) 私聊：默认只允许 owner；owner_only=False 时任何私聊都受理
    if is_p2p:
        if (cfg.get("private_chat") or {}).get("owner_only", True):
            return {"allow": False, "reason_code": DENY_PRIVATE,
                    "detail": "私聊只允许 owner 本人发起命令", "grant_task": None, "grant": None}
        return {"allow": True, "reason_code": ALLOW,
                "detail": "私聊未限制 owner_only", "grant_task": None, "grant": None}

    # 4) 群聊：必须在 owner 指定过的群里
    allowed_groups = [str(g) for g in ((cfg.get("groups") or {}).get("allow") or [])]
    if not is_p2p and chat_id and chat_id in allowed_groups:
        return {"allow": True, "reason_code": ALLOW_GROUP,
                "detail": f"在 owner 指定的群里（{chat_id}），群成员均可发起命令",
                "grant_task": None, "grant": None}

    # 5) 其余一律拒绝
    if is_p2p:
        code, why = DENY_PRIVATE, "私聊只允许 owner 本人发起命令"
    else:
        code, why = DENY_GROUP, "该群不在 owner 指定的可发起命令的群里"
    return {"allow": False, "reason_code": code, "detail": why,
            "grant_task": None, "grant": None}


def policy_summary() -> dict:
    """给看板看的策略摘要。"""
    cfg = load_policy()
    return {
        "owner": cfg.get("owner"),
        "private_chat_owner_only": (cfg.get("private_chat") or {}).get("owner_only", True),
        "groups": (cfg.get("groups") or {}).get("allow") or [],
        "people_count": len(cfg.get("people") or {}),
        "grant_ttl_hours": cfg.get("grant_ttl_hours", 48),
    }
