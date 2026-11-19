#!/usr/bin/env python3
"""把已发布的报告页里的插图（figure.fig + assets/*.png）去掉，只保留内联 SVG 图表。

用法：
  python3 scripts/strip_illustrations.py --root ~/develop/blog-repo/public/news 2026/11/13
  # root 也可以用环境变量 BLOG_NEWS_ROOT 指定
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

FIG = re.compile(r'\s*<figure class="fig">.*?</figure>', re.S)
# header 卡片：把「N 张配图」换成「N 内联图表」
CARD = re.compile(r'<div class="n">(\d+)</div><div class="l">张配图</div>')
FOOT = re.compile(r'(\d+) 张插画')
HERO = re.compile(r'(\d+)\s*张')


def strip(root: Path, date: str) -> None:
    """就地改写 root/date/index.html，并删掉该页目录下的 png 插画。"""
    base = root / date
    idx = base / "index.html"
    if not idx.exists():
        print(f"✖ {date}: 不存在")
        return
    h = idx.read_text(encoding="utf-8")
    before = h.count("<figure")

    h = FIG.sub("", h)
    # og:image 指向站点默认图
    h = re.sub(r'<meta property="og:image" content="assets/[^"]*">',
               '<meta property="og:image" content="https://blog.example.com/img/og-default.png">', h)
    # 卡片：N 张配图 → 内联 SVG 数
    svg_n = h.count("<svg")
    h = CARD.sub(lambda m: f'<div class="n">{svg_n}</div><div class="l">内联图表</div>', h)
    # 页脚
    h = FOOT.sub("", h)
    # hero 里的「N 张」计数
    h = HERO.sub(f"{svg_n} 内联图表", h, count=0)
    # 清理图注里指向插画的说明
    h = re.sub(r'（图源：本期生成插画）', "", h)

    idx.write_text(h, encoding="utf-8")

    assets = base / "assets"
    removed = 0
    if assets.exists():
        for f in assets.glob("*.png"):
            f.unlink()
            removed += 1
        try:
            assets.rmdir()
        except OSError:
            pass

    print(f"✔ {date}: 移除 {before} 个插画块 / {removed} 张 png，内联图表 {svg_n} 个；"
          f"剩余 img 标签 {h.count('<img')}")


def main(argv=None):
    p = argparse.ArgumentParser(description="去掉报告页里的插画，只留内联 SVG 图表")
    p.add_argument("dates", nargs="+", help="页面目录名（相对 root，如 2026/11/13）")
    p.add_argument("--root", default=os.environ.get("BLOG_NEWS_ROOT", ""),
                   help="报告页根目录（默认取环境变量 BLOG_NEWS_ROOT）")
    args = p.parse_args(argv or sys.argv[1:])
    if not args.root:
        print("✖ 需要报告页根目录：--root 或环境变量 BLOG_NEWS_ROOT", file=sys.stderr)
        return 2
    root = Path(args.root).expanduser()
    for d in args.dates:
        strip(root, d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
