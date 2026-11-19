#!/usr/bin/env python3
"""把线上已渲染的报告页回填成博客仓库的 Markdown 源文件。

背景：有一次一批报告只同步到了对象存储，源码没进 git 仓库，导致部署时的
裁剪逻辑把它们从线上删掉。本脚本用于把孤儿页面拉回仓库，让仓库重新成为
线上内容的真源。

用法：
  # 仓库路径默认取环境变量 BLOG_REPO，也可以用 --repo 显式指定
  export BLOG_REPO=~/develop/blog-repo
  python3 scripts/backfill_orphan_reports.py 2026-11-07 2026-11-08
  python3 scripts/backfill_orphan_reports.py --range 2026-11-03 2026-11-09
  python3 scripts/backfill_orphan_reports.py --base https://blog.example.com/reports/
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

DEFAULT_BASE = "https://blog.example.com/reports/"


class Prose(HTMLParser):
    """把 <div class="prose"> 内的渲染结果转回 Markdown。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []          # 标签栈
        self.in_prose = False
        self.skip = 0            # >0 时丢弃内容（script/style/导航）
        self.list_stack = []     # 每个栈元素是 'ul' / 'ol'
        self.item_index = []
        self.in_pre = False
        self.pre_buf = []
        self.link_stack = []
        self.quote_depth = 0

    # ---------- 辅助 ----------
    def _emit(self, s):
        if self.skip or self.in_pre:
            if self.in_pre:
                self.pre_buf.append(s)
            return
        self.out.append(s)

    def _block(self, s):
        self._emit(s)
        self._emit("\n\n")

    # ---------- 解析 ----------
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div" and a.get("class", "").find("prose") >= 0:
            self.in_prose = True
            return
        if not self.in_prose:
            return
        if tag in ("script", "style"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "p":
            self._emit("" if self.quote_depth else "\n\n")
        elif tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._block("---")
        elif tag in ("ul", "ol"):
            self.list_stack.append(tag)
            self.item_index.append(0)
            self._emit("\n")
        elif tag == "li":
            if not self.list_stack:
                return
            depth = len(self.list_stack)
            indent = "  " * (depth - 1)
            marker = "- " if self.list_stack[-1] == "ul" else f"{self.item_index[-1] + 1}. "
            self.item_index[-1] += 1
            self._emit("\n" + indent + marker)
        elif tag == "blockquote":
            self._emit("\n\n> ")
            self.quote_depth += 1
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self.in_pre:
            self._emit("`")
        elif tag == "pre":
            self.in_pre = True
            self.pre_buf = []
        elif tag == "a":
            self.link_stack.append(a.get("href", ""))
            self._emit("[")
        elif tag == "img":
            alt = a.get("alt", "")
            self._emit(f"![{alt}]({a.get('src','')})")
        elif tag == "table":
            self._emit("\n\n")
        elif tag in ("tr", "td", "th"):
            self._emit(" | ")

    def handle_endtag(self, tag):
        if not self.in_prose:
            return
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "pre":
            code = "".join(self.pre_buf).strip("\n")
            self.in_pre = False
            self.out.append("\n\n```\n" + code + "\n```\n\n")
            self.pre_buf = []
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("\n\n")
        elif tag == "p":
            self._emit("" if self.quote_depth else "\n\n")
        elif tag == "blockquote":
            self.quote_depth = max(0, self.quote_depth - 1)
            self._emit("\n")
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
                self.item_index.pop()
            self._emit("\n")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code":
            self._emit("`")
        elif tag == "a":
            href = self.link_stack.pop() if self.link_stack else ""
            self._emit(f"]({href})")
        elif tag in ("td", "th"):
            self._emit(" |")

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_pre:
            self.pre_buf.append(data)
            return
        if not self.in_prose or not data.strip():
            return
        self._emit(data)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def to_markdown(page: str):
    """返回 (title, desc, markdown)。找不到正文块时抛 SystemExit。"""
    i = page.find('<div class="prose">')
    if i < 0:
        raise SystemExit("no prose block")
    j = page.find('<div class="article-footer', i)
    body = page[i:j if j > 0 else len(page)]
    p = Prose()
    p.in_prose = True
    p.feed(body)
    md = "".join(p.out)

    title = ""
    m = re.search(r'<h1 class="article-title">(.*?)</h1>', page, re.S)
    if m:
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
    desc = ""
    m = re.search(r'<p class="article-lede">(.*?)</p>', page, re.S)
    if m:
        desc = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()

    # 清理多余空行与行尾空白
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = md.strip() + "\n"
    return title, desc, md


def parse_args(argv):
    p = argparse.ArgumentParser(description="回填线上孤儿报告页到博客仓库源码")
    p.add_argument("dates", nargs="*", help="要回填的日期（YYYY-MM-DD）")
    p.add_argument("--repo", default=os.environ.get("BLOG_REPO", ""),
                   help="博客仓库路径（默认取环境变量 BLOG_REPO）")
    p.add_argument("--base", default=os.environ.get("BLOG_BASE", DEFAULT_BASE),
                   help=f"线上报告页根路径（默认 {DEFAULT_BASE}）")
    p.add_argument("--out-dir", default="src/content/reports",
                   help="源码输出目录（相对博客仓库，默认 src/content/reports）")
    p.add_argument("--range", nargs=2, metavar=("START", "END"),
                   help="按日期区间回填，含首尾（YYYY-MM-DD YYYY-MM-DD）")
    return p.parse_args(argv)


def date_range(start: str, end: str):
    from datetime import date, timedelta
    y1, m1, d1 = (int(x) for x in start.split("-"))
    y2, m2, d2 = (int(x) for x in end.split("-"))
    cur, last = date(y1, m1, d1), date(y2, m2, d2)
    while cur <= last:
        yield cur.isoformat()
        cur += timedelta(days=1)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if not args.repo:
        print("✖ 需要博客仓库路径：--repo 或环境变量 BLOG_REPO", file=sys.stderr)
        return 2
    repo = Path(args.repo).expanduser()
    out_dir = repo / args.out_dir
    base = args.base if args.base.endswith("/") else args.base + "/"

    dates = list(args.dates)
    if args.range:
        dates.extend(date_range(*args.range))
    if not dates:
        print("✖ 没有指定日期（位置参数或 --range）", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    for d in dates:
        try:
            title, desc, md = to_markdown(fetch(base + d))
        except SystemExit as e:
            print(f"✖ {d}: {e}")
            continue
        except Exception as e:  # 网络/解析异常都只跳过这一篇
            print(f"✖ {d}: {e}")
            continue
        if len(md) < 500:
            print(f"✖ {d}: 正文过短（{len(md)}），跳过")
            continue
        fm = [
            "---",
            f'title: "{title}"',
            f"date: {d}",
            f'description: "{desc.replace(chr(34), chr(39))}"',
            "tags: []",
            "---",
            "",
        ]
        out = out_dir / f"{d}.md"
        out.write_text("\n".join(fm) + md, encoding="utf-8")
        print(f"✔ {d} → {out}（{len(md)} 字符）｜{title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
