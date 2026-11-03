# -*- coding: utf-8 -*-
"""v6.0.7 实测：一个游戏到底能抓到几张、分别是什么 kind。

用用户截图里那个游戏（PSP 的 Amnesia）—— 它在 libretro 的
``Named_Boxarts`` 里**没有**，正好验证「IGDB 兜底 + 多图」这条路。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cover_art as ca                                        # noqa: E402
import cover_igdb as cg                                       # noqa: E402

TITLE = "Amnesia (2011-08-11)(Design Factory, Otomate)(Idea Factory)[PSP]"
KEY = "psp"
LIMIT = 6


def show(tag, got):
    print("  %-9s %d 张" % (tag, len(got)))
    for p, kind in got:
        sz = os.path.getsize(p) if os.path.isfile(p) else -1
        print("     [%-10s] %8d B  %s" % (kind, sz, os.path.basename(p)))


def main():
    print("游戏：%s" % TITLE)
    print("缓存根：%s" % ca.cache_root())
    print()

    print("① libretro（只认 Named_Boxarts）")
    lb = ca.fetch_all(KEY, TITLE, limit=LIMIT)
    show("libretro", lb)
    print()

    print("② IGDB（cover + screenshots + extra_images）")
    ig = cg.fetch_all(KEY, TITLE, limit=LIMIT - len(lb))
    show("IGDB", ig)
    print()

    print("③ 合并后的展示顺序（轮播就按这个来）")
    allimg = lb + ig
    for i, (p, kind) in enumerate(allimg):
        mark = "  ← 嵌音频用这张" if kind in ca.BOXART_KINDS and i == 0 else ""
        print("   %d. [%-10s] %s%s" % (i, kind, os.path.basename(p), mark))
    print()

    print("④ 嵌入环节（cover_for_title）")
    best = ca.cover_for_title(TITLE)
    print("   %r" % (os.path.basename(best) if best else ""))
    kind_of = dict((p, k) for p, k in allimg)
    print("   kind = %s" % kind_of.get(best, "?"))
    ok = (not best) or (kind_of.get(best) in ca.BOXART_KINDS)
    print("   是盒绘: %s" % ("是" if ok else "!! 不是，嵌错了"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
