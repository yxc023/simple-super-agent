/**
 * Router 护栏（super-agent 专用）
 *
 * 目的：让 router **在机制上无法自己干活**。
 *
 * 背景：常驻 router session 收到用户消息后，曾经出现过「自己去翻代码、自己批准旧任务、
 * 迟迟不派工」的情况 —— 它有 bash/read/write，就能自己动手，于是它就自己动手了。
 * 但 router 的职责是**调度**：判断 → 派工 → 结束。调查与执行是 worker 的活。
 *
 * 这里的做法不是靠提示词约束（提示词会被长上下文冲淡），而是直接掐掉能力：
 *   · bash  —— 只允许调用 Task Center（sa.py），其余命令一律拒绝
 *   · read  —— 只允许读任务/上下文/回执目录与 /tmp，不允许读框架源码
 *   · write —— 只允许写 /tmp（Decision 草稿）
 *
 * 结果：router 拿到「查一下我有没有待审批」这种请求时，**没有任何办法自己去查**，
 * 只能派 worker。这正是我们要的。
 */

import * as path from "node:path";
import * as fs from "node:fs";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// 自定位：本文件就在 super-agent/ 目录里，REPO = 它的上一级。
// 不写死绝对路径 —— 换台机器 / 复制到别的仓库也能直接用。
const SA = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(SA, "..");

/** Task Center 允许的子命令白名单 */
const SA_COMMANDS = new Set([
  "tasks-list",
  "show",
  "list",
  "submit-decision",
  "outbox-add",
  "outbox-list",
  "ack",
  "note",
  "events",
]);

/** shell 里的连接/替换符号 —— 出现即拒绝（防止 `&&`/`;`/`|` 绕过白名单） */
const SHELL_METACHARS = /[;&|`$()\n\r<>]/;

/** router 可以读的任务相关目录 */
const READ_ROOTS = [
  path.join(SA, "tasks"),
  path.join(SA, "channels"),
  path.join(SA, "router"),
  path.join(SA, "inbox"),
];

/** 允许读的单个文件 */
const READ_FILES = [path.join(SA, "registry.json")];

function under(p: string, root: string): boolean {
  return p === root || p.startsWith(root + path.sep);
}

function isTaskRead(p: string): boolean {
  if (READ_FILES.includes(p)) return true;
  if (READ_ROOTS.some((r) => under(p, r))) return true;
  // /tmp 下的产出物（worker 的结果，需要亲自打开核对）
  if (under(p, "/tmp")) return true;
  return false;
}

/**
 * 判断一条 bash 命令是不是「只调用 Task Center 的 sa.py」。
 *
 * 判定方式：按 shell 词法切分（不处理引号嵌套，遇到元字符直接判否），
 * 要求第一个 token 是 python3、第二个 token 指向本仓库的 sa.py、
 * 第三个 token 在子命令白名单里。
 */
export function isTaskCenterCommand(cmd: string): boolean {
  const raw = String(cmd ?? "").trim();
  if (!raw) return false;
  if (SHELL_METACHARS.test(raw)) return false;
  const argv = raw.split(/\s+/).filter(Boolean);
  if (argv.length < 3) return false;
  if (!/^(python3|python)$/.test(argv[0])) return false;
  const script = path.resolve(REPO, argv[1]);
  if (script !== path.join(SA, "sa.py")) return false;
  return SA_COMMANDS.has(argv[2]);
}

/**
 * 扩展入口。返回 undefined 表示放行；返回 { block, reason } 表示拦截。
 */
export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event) => {
    const name = event.toolName;

    // ── bash：只允许 Task Center ──
    if (name === "bash") {
      const cmd = String((event.input as any).command ?? "").trim();
      // 不能用正则匹配整条命令 —— `python3 sa.py && rm -rf ~` 也会命中前缀。
      // 这里做的是**词法解析**：整条命令必须恰好是
      //   python3 <…>/super-agent/sa.py <子命令> [参数…]
      // 出现任何 shell 元字符（&& ; | ` $() 换行 重定向）一律拒绝。
      const allowed = isTaskCenterCommand(cmd);
      if (!allowed) {
        return {
          block: true,
          reason:
            "router 只通过 Task Center 操作任务：\n" +
            "  python3 " + path.join(SA, "sa.py") + " <子命令>\n" +
            "可用子命令：tasks-list / show / list / submit-decision / outbox-add / ack / note。\n" +
            "查代码、跑脚本、grep、调外部系统 —— 这些都是 worker 的活，请派 worker。",
        };
      }
      return undefined;
    }

    // ── read：只允许任务上下文与产出物 ──
    if (name === "read") {
      const raw = String((event.input as any).path ?? "");
      const p = path.isAbsolute(raw) ? path.normalize(raw) : path.normalize(path.join(REPO, raw));
      if (!isTaskRead(p)) {
        return {
          block: true,
          reason:
            "router 只读任务相关文件（tasks/ channels/ router/ inbox/ 与 /tmp 下的产出物）。\n" +
            "读业务代码不是你的职责 —— 把这件事写进指派单，派 worker 去做。",
        };
      }
      return undefined;
    }

    // ── write / edit：只允许 /tmp（Decision 草稿）──
    if (name === "write" || name === "edit") {
      const raw = String((event.input as any).path ?? "");
      const p = path.isAbsolute(raw) ? path.normalize(raw) : path.normalize(path.join(REPO, raw));
      if (!under(p, "/tmp")) {
        return {
          block: true,
          reason:
            "router 只能把 Decision 草稿写到 /tmp/。\n" +
            "改框架代码、改任务文件、跑脚本 —— 派 worker 去做（并在指派单里说清要改什么）。",
        };
      }
      return undefined;
    }

    // ── 其它工具（grep/find/ls/edit…）全部拦掉 ──
    return {
      block: true,
      reason:
        `router 不使用 ${name} 工具。\n` +
        "你的职责是判断与派工：读任务上下文 → 写 Decision → 提交 → 结束。\n" +
        "调查、检索、执行类工作一律写进指派单派给 worker。",
    };
  });
}
