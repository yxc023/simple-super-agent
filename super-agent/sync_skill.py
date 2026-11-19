#!/usr/bin/env python3
"""把 router 契约同步到各处。

**单一真源**：`super-agent/skill/router-system.md`（router 的操作契约正文）
             `super-agent/skill/skill-frontmatter.md`（skill 的 YAML 头）

生成物：
  - `super-agent/skill/SKILL.md`                       = frontmatter + router-system.md
  - `<repo>/.pi/skills/super-agent/SKILL.md`            （项目级，需要目录受信任）
  - `~/.pi/agent/skills/super-agent/SKILL.md`            （用户级，任何 cwd 都生效）

以前 SKILL.md 与 router-system.md 是两份手抄的副本，已经漂移过一次
（SKILL.md 缺了触发闸门与 Decision 的 requester 字段）。
现在 SKILL.md 由脚本生成，**不要再手改它**。

    python3 super-agent/sync_skill.py          # 生成 + 同步
    python3 super-agent/sync_skill.py --check  # 只检查是否一致（CI 用）
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent
BODY = ROOT / "skill" / "router-system.md"
FRONTMATTER = ROOT / "skill" / "skill-frontmatter.md"
GENERATED = ROOT / "skill" / "SKILL.md"
TARGETS = [
    ROOT.parent / ".pi" / "skills" / "super-agent" / "SKILL.md",
    pathlib.Path.home() / ".pi" / "agent" / "skills" / "super-agent" / "SKILL.md",
]


def build() -> str:
    """生成 SKILL.md 的内容：YAML 头 + 契约正文。"""
    if not BODY.exists():
        raise SystemExit(f"找不到契约正文：{BODY}")
    if not FRONTMATTER.exists():
        raise SystemExit(f"找不到 frontmatter：{FRONTMATTER}")
    fm = FRONTMATTER.read_text(encoding="utf-8").rstrip("\n")
    body = BODY.read_text(encoding="utf-8").lstrip("\n")
    return fm + "\n\n" + body


def main() -> None:
    ap = argparse.ArgumentParser(description="生成并同步 router skill")
    ap.add_argument("--check", action="store_true",
                    help="只检查是否一致（生成物与目标有差异时退出码 1）")
    ap.add_argument("--only", choices=["generated", "targets"],
                    help="只处理生成物或只处理同步目标（默认都处理）")
    a = ap.parse_args()

    content = build()
    ok = True

    if a.only != "targets":
        if a.check:
            same = GENERATED.exists() and GENERATED.read_text(encoding="utf-8") == content
            print(f"{'OK ' if same else 'DIFF'}  {GENERATED}")
            ok = ok and same
        else:
            GENERATED.write_text(content, encoding="utf-8")
            print(f"生成 → {GENERATED}")

    if a.only != "generated":
        for t in TARGETS:
            if a.check:
                same = t.exists() and t.read_text(encoding="utf-8") == content
                print(f"{'OK ' if same else 'DIFF'}  {t}")
                ok = ok and same
                continue
            try:
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(GENERATED, t)
                print(f"同步 → {t}")
            except OSError as e:
                print(f"✖ 同步失败 {t}：{e}", file=sys.stderr)
                ok = False

    if a.check and not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
