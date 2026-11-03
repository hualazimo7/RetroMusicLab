#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_smoke_cover_live.py — 封面线程的**联网**冒烟（真抓）。

和 ``_smoke_cover_worker.py`` 的分工：那个验接线（不联网、可进 CI），
这个验真链路（真拉 libretro 索引、真下 PNG、真写缓存）。

跑法::

    python vendor/_smoke_cover_live.py            # 默认 SNES 3 个
    python vendor/_smoke_cover_live.py s98 3      # 指定机种
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

import cover_art                                  # noqa: E402

CASES = {
    "spc": ["Chrono Trigger [SNES]",
            "Final Fantasy III [SNES]",
            "Super Metroid (1994-03-19)(Nintendo)[SNES]"],
    "s98": ["Ys (1987-06-21)(Nihon Falcom)[PC-98]",
            "Rance (1989-07-01)(Alice Soft)[PC-98]"],
    "nsf": ["Super Mario Bros. [NES]",
            "Mega Man 2 [NES]"],
    "smd": ["Sonic the Hedgehog [SMD]",
            "Streets of Rage 2 [SMD]"],
}


def main(argv):
    key = (argv[1] if len(argv) > 1 else "spc").strip().lower()
    n = int(argv[2]) if len(argv) > 2 else 3
    titles = CASES.get(key, [])
    if not titles:
        print("[!!] 没有 %s 的测试用例，可选：%s" % (key, ", ".join(CASES)))
        return 1

    print("机种 %s → libretro 目录 %s" % (key, cover_art.platform_dirs(key)))
    t0 = time.time()
    items = cover_art.list_covers(key)
    print("索引 %d 条，耗时 %.1f 秒（缓存文件 %s）"
          % (len(items), time.time() - t0,
             os.path.join(cover_art._idx_dir(), key + ".json")))
    if not items:
        print("[!!] 索引是空的 —— 网络不通或目录名错了")
        return 1

    ok = fail = 0
    for t in titles[:n]:
        t1 = time.time()
        good, path, why = cover_art.fetch(key, t)
        dt = time.time() - t1
        if good:
            ok += 1
            size = os.path.getsize(path)
            print("[ok] %-46s %6.1f KB  %.2fs  %s"
                  % (t, size / 1024.0, dt, os.path.basename(path)))
            # 真 PNG 吗？站点对 404 也回 200 的 HTML，光看文件存在不够。
            with open(path, "rb") as fh:
                head = fh.read(8)
            assert head == b"\x89PNG\r\n\x1a\n", "不是 PNG: %r" % head
        else:
            fail += 1
            print("[--] %-46s %s  (%.2fs)" % (t, why, dt))

    print("\n命中 %d / 未命中 %d" % (ok, fail))
    print("缓存统计：%s" % (cover_art.stats(),))
    # 反查：转码时就是走这条路
    if ok:
        t = titles[0]
        print("cover_for_title(%r) -> %s" % (t, cover_art.cover_for_title(t)))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
