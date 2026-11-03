# -*- coding: utf-8 -*-
"""侦察 2：libretro 三个 kind 的文件名是否一致 + 一个游戏能匹到几条。

两个关键问题：
1. ``Named_Boxarts`` / ``Named_Titles`` / ``Named_Snaps`` 里同一个游戏
   的文件名是不是同一个？如果是，就**不用扩 index** —— 拿 boxart 的名字
   去另外两个目录直接取同名即可。
2. 同一个游戏在 Boxarts 里能匹到几个地区变体？（这决定「若干张」的上限）
"""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cover_art as ca                                        # noqa: E402

BASE = ca.BASE


def list_kind(pdir, kind):
    url = "%s/%s/%s/" % (BASE, urllib.parse.quote(pdir, safe=""), kind)
    data, _why = ca._get(url, ca.MAX_LIST_BYTES)
    return ca._parse_listing(data) if data else []


def main():
    # 用 psp（PLATFORM_DIRS 里确认存在）和几个机种 key
    keys = ["psp", "playstation", "snes", "gba"]
    for key in keys:
        dirs = ca.platform_dirs(key)
        if not dirs:
            print("%-14s 没有登记目录" % key)
            continue
        d = dirs[0]
        sets = {}
        for kind in ca.KINDS:
            sets[kind] = set(list_kind(d, kind))
        print("=" * 74)
        print("%s  (%s)" % (key, d))
        for kind in ca.KINDS:
            print("  %-14s %5d 条" % (kind, len(sets[kind])))
        b = sets["Named_Boxarts"]
        if b:
            t = sets["Named_Titles"]
            s = sets["Named_Snaps"]
            print("  Boxarts∩Titles: %d  (占 boxarts 的 %.0f%%)"
                  % (len(b & t), 100.0 * len(b & t) / len(b)))
            print("  Boxarts∩Snaps : %d  (占 boxarts 的 %.0f%%)"
                  % (len(b & s), 100.0 * len(b & s) / len(b)))
        # 一个游戏能匹到几条 boxart
        for title in ("Amnesia", "Final Fantasy VII", "Chrono Trigger"):
            items = [{"dir": d, "name": n} for n in sets["Named_Boxarts"]]
            one = ca.pick(items, title)
            alls = ca.pick_all(items, title) if hasattr(ca, "pick_all") else []
            print("  「%s」→ pick=%r  pick_all=%d 条"
                  % (title, one, len(alls) if alls else 0))
        print()


if __name__ == "__main__":
    main()
