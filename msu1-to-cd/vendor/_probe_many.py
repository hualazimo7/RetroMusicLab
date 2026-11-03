# -*- coding: utf-8 -*-
"""v6.0.7 实测 2：走真实的 ``_cover_many``，看**去重后**还剩几张。

重点验三件事：
1. 去重生效（libretro 的 boxart 和 IGDB 的 cover 是同一张图）；
2. 张数不超过 6；
3. 第一张是盒绘（嵌音频只用它）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cover_art as ca                                        # noqa: E402
from web_ui import dl_api as dmod                             # noqa: E402

CASES = [
    ("psp", "Amnesia (2011-08-11)(Design Factory, Otomate)(Idea Factory)[PSP]"),
]


def main():
    st = dmod.DownloadState()
    bad = 0
    for key, title in CASES:
        print("=" * 70)
        print("%s / %s" % (key, title))
        got = st._cover_many(key, title, None, dmod.COVER_LIMIT)
        print("  去重后 %d 张（上限 %d）：" % (len(got), dmod.COVER_LIMIT))
        for i, (p, tag) in enumerate(got):
            kind = ""
            for _p, k in ca.covers_for_title(title):
                if _p == p:
                    kind = k
                    break
            sz = os.path.getsize(p) if os.path.isfile(p) else -1
            print("    %d. [%-10s] %-9s %8d B  %s"
                  % (i, kind or "?", tag, sz, os.path.basename(p)))
        if len(got) > dmod.COVER_LIMIT:
            print("  !! 超过上限")
            bad += 1
        best = ca.cover_for_title(title)
        kinds = dict((p, k) for p, k in ca.covers_for_title(title))
        if best and kinds.get(best) not in ca.BOXART_KINDS:
            print("  !! 嵌入用的不是盒绘：%s" % kinds.get(best))
            bad += 1
        else:
            print("  嵌入用：%s  ✓" % (os.path.basename(best) if best else "（无）"))
        print()

    print("日志：")
    for row in st.logs[-6:]:
        print("   ", row[1])
    print()
    print("结论：%s" % ("全部通过" if not bad else "%d 项失败" % bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
