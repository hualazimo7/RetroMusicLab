# -*- coding: utf-8 -*-
"""侦察：libretro 一个游戏到底有几张图可用。

三个 kind（Boxarts / Titles / Snaps）各有多少条、同一个游戏能匹配到几条、
index 变大多少。结论直接决定「多封面」怎么做。
"""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cover_art as ca                                        # noqa: E402

BASE = ca.BASE

# 挑几个典型机种：一个条目少的、一个条目多的、用户截图里那个
KEYS = ["psp", "snes", "ps1"]


def list_kind(pdir, kind):
    url = "%s/%s/%s/" % (BASE, urllib.parse.quote(pdir, safe=""), kind)
    data, why = ca._get(url, ca.MAX_LIST_BYTES)
    if not data:
        return [], why
    return ca._parse_listing(data), ""


def main():
    for key in KEYS:
        dirs = ca.platform_dirs(key)
        print("=" * 70)
        print("%s -> %s" % (key, dirs))
        for d in dirs:
            for kind in ca.KINDS:
                names, why = list_kind(d, kind)
                print("  %-16s %-14s %6d 条 %s"
                      % (d, kind, len(names), why[:40]))
        print()


if __name__ == "__main__":
    main()
