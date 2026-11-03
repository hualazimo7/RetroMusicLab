#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_probe_union.py — 两个封面源的**并集**命中率。

## 为什么要这张表

``_audit_cover_hit.py`` 只测 libretro，``_probe_igdb.py`` 只测 IGDB，
两张表各自看都容易误判：

* libretro 在 GBA / Saturn 有 98%，看着很好 —— 但 Xbox 360 是 **0%**；
* IGDB 在 PS4 / GC 有 100%，看着更好 —— 但 SNES 只有 70%。

而程序里是**先 libretro、没找到才问 IGDB**（``dl_api._cover_one``），
所以用户实际拿到的是**并集**：

    P(有封面) = 1 - (1 - P_libretro) × (1 - P_igdb)

这张表把三列并排放，回答的就是「IGDB 到底补了多少」。

## 口径

和两张单表**完全一致**：同一个随机种子（20261028）、同一套
``cover_art.norm_title`` / ``cover_art.pick``、同一个「命中」定义
（随机抽 N 条 joshw 条目，有几条能挑出一张封面）。

⚠ 只**比匹配**、不**下图** —— 下 250 张图纯属浪费对方带宽，而且
「能不能挑出来」和「能不能下下来」是两件事（后者在 v6.0.5 的
端到端验收里单独验过）。

跑法::

    py -3.14 vendor/_probe_union.py                # 全机种，每机种 10 条
    py -3.14 vendor/_probe_union.py --n 20
    py -3.14 vendor/_probe_union.py --keys pc,x360
"""

import argparse
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import cover_art as ca                             # noqa: E402
import cover_igdb as cg                            # noqa: E402
import joshw_source as js                          # noqa: E402


def _libretro_hit(key, title, cache):
    """libretro 能不能挑出来。``cache`` 是机种 → 目录列表（避免重复拉索引）。"""
    if not ca.supported(key):
        return False
    if key not in cache:
        try:
            cache[key] = ca.list_covers(key)
        except Exception:                           # noqa: BLE001
            cache[key] = []
    items = cache[key]
    return bool(items) and bool(ca.pick(items, title))


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--n", type=int, default=10, help="每个机种抽几条（默认 10）")
    ap.add_argument("--keys", default="", help="只测这些机种，逗号分隔")
    ap.add_argument("--gap", type=float, default=cg.MIN_GAP, help="请求间隔秒")
    a = ap.parse_args(argv)

    cg.MIN_GAP = max(0.15, a.gap)
    keys = ([k.strip() for k in a.keys.split(",") if k.strip()]
            or [p["key"] for p in js.PLATFORMS])
    rnd = random.Random(20261028)

    cache = {}
    rows, tot = [], [0, 0, 0, 0]        # 抽样 / libretro / IGDB / 并集
    for key in keys:
        try:
            items, _ts, _src = js.best_index(key)
        except Exception as exc:                    # noqa: BLE001
            print("%-12s 索引读不到：%s" % (key, exc), flush=True)
            continue
        if not items:
            continue
        sample = rnd.sample(items, min(a.n, len(items)))
        n = n_ca = n_cg = n_any = 0
        for e in sample:
            title = str(e.get("name") or "")
            if not title:
                continue
            n += 1
            ca_ok = _libretro_hit(key, title, cache)
            cg_ok = False
            if not ca_ok:                           # 程序里也是失手才问 IGDB
                try:
                    row, _why = cg.search(key, title)
                    cg_ok = bool(row)
                except Exception:                   # noqa: BLE001
                    cg_ok = False
            n_ca += 1 if ca_ok else 0
            n_cg += 1 if cg_ok else 0
            n_any += 1 if (ca_ok or cg_ok) else 0
        tot[0] += n
        tot[1] += n_ca
        tot[2] += n_cg
        tot[3] += n_any
        rows.append((key, n, n_ca, n_cg, n_any))
        print("%-12s 抽样 %2d → libretro %2d%% · IGDB 补 %2d%% · 并集 %3d%%"
              % (key, n, round(100.0 * n_ca / max(1, n)),
                 round(100.0 * n_cg / max(1, n)),
                 round(100.0 * n_any / max(1, n))), flush=True)

    print("\n| 机种 | 抽样 | libretro | IGDB 补上 | **并集** |")
    print("|---|---|---|---|---|")
    for key, n, n_ca, n_cg, n_any in rows:
        print("| `%s` | %d | %d%% | %d%% | **%d%%** |"
              % (key, n, round(100.0 * n_ca / max(1, n)),
                 round(100.0 * n_cg / max(1, n)),
                 round(100.0 * n_any / max(1, n))))
    print("\n合计：抽样 %d · libretro **%d%%** · IGDB 再补 **%d%%** · "
          "并集 **%d%%**"
          % (tot[0], round(100.0 * tot[1] / max(1, tot[0])),
             round(100.0 * tot[2] / max(1, tot[0])),
             round(100.0 * tot[3] / max(1, tot[0]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
