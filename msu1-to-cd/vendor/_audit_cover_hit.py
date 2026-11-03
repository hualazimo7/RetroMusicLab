#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_audit_cover_hit.py — 封面命中率实测（写文档用）。

「命中率」的定义只有一个：**随机抽 N 条 joshw 条目，有几条能挑出一张封面**。
所以样本必须来自 joshw 的索引（``joshw_source.best_index``），不是 libretro。

v6.0.4 起**默认跑全部 33 个机种**（以前只跑 14 个硬编码 key），并且把
「没有封面源」的机种单独列出来 —— 用户问「能覆盖所有机种吗」时，
这张表就是答案。没映射的机种不抽样（抽了也全是 0），直接标 ``—``。

跑法::

    py -3.14 vendor/_audit_cover_hit.py            # 全机种，每机种 40 条
    py -3.14 vendor/_audit_cover_hit.py 80         # 每机种 80 条
    py -3.14 vendor/_audit_cover_hit.py 40 --keys spc,nsf
"""

import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import cover_art as ca                             # noqa: E402
import joshw_source as js                          # noqa: E402

#: 老版本只跑这 14 个（当时只登记了这些）。留着只为 ``--keys`` 的默认口径
#: 兼容旧文档，正常跑走 ``PLATFORMS`` 全表。
KEYS = ["spc", "nsf", "gbs", "gsf", "smd", "hes", "usf", "ssf", "dsf",
        "2sf", "s98", "psf", "3do", "wsr"]


def main(argv):
    n = 40
    keys = [p["key"] for p in js.PLATFORMS]
    rest = []
    for a in argv[1:]:
        if a.startswith("--keys="):
            keys = [k.strip() for k in a.split("=", 1)[1].split(",") if k.strip()]
        elif a.isdigit():
            n = int(a)
        else:
            rest.append(a)
    rnd = random.Random(20261027)                  # 固定种子，数字可复现
    tot_hit = tot_all = 0
    rows = []
    no_src = []
    for key in keys:
        if not ca.supported(key):
            no_src.append(key)
            continue
        try:
            items, _ts, src = js.best_index(key)
        except Exception as exc:                   # noqa: BLE001
            print("%-12s 索引读不到：%s" % (key, exc), flush=True)
            continue
        if not items:
            continue
        sample = rnd.sample(items, min(n, len(items)))
        covers = ca.list_covers(key)
        if not covers:
            print("%-12s 封面列表是空的（目录名错了？）" % key, flush=True)
            continue
        hit = sum(1 for e in sample if ca.pick(covers, e.get("name") or ""))
        tot_hit += hit
        tot_all += len(sample)
        rows.append((key, src, len(items), len(covers), len(sample), hit))
        print("%-12s 索引 %5d 条(%s) · 封面库 %5d 条 · 抽样 %3d → 命中 %3d (%d%%)"
              % (key, len(items), src, len(covers), len(sample), hit,
                 round(100.0 * hit / len(sample))), flush=True)

    print("\n| 机种 | 封面库条目 | 抽样 | 命中 | 命中率 |")
    print("|---|---|---|---|---|")
    for key, _src, _ni, nc, ns, hit in rows:
        print("| `%s` | %d | %d | %d | **%d%%** |"
              % (key, nc, ns, hit, round(100.0 * hit / ns)))
    for key in no_src:
        print("| `%s` | — | — | — | **无封面源** |" % key)
    print("\n合计 **%d / %d = %d%%**（%d 个机种有源 / 共 %d 个）"
          % (tot_hit, tot_all, round(100.0 * tot_hit / max(1, tot_all)),
             len(rows), len(keys)))
    if no_src:
        print("没有封面源的机种：%s" % ", ".join(no_src))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
