#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_probe_igdb.py — IGDB 补封面的命中率实测（写文档 / 验收用）。

「命中」的定义和 `vendor/_audit_cover_hit.py` 一致：**随机抽 N 条 joshw
条目，有几条能挑出一张封面**。同一个随机种子、同一套归一化、同一个
`cover_art.pick` —— 这样两张表可以直接对比。

只**搜**不**下**图：一次搜索 1 个请求，下图片还要再来一次，33 个机种
× 10 条就是 660 个请求。对方是 Zaparoo 的公益后端，别刷。

跑法::

    py -3.14 vendor/_probe_igdb.py                 # 全机种，每机种 10 条
    py -3.14 vendor/_probe_igdb.py --n 20
    py -3.14 vendor/_probe_igdb.py --keys switch,pc,x360
    py -3.14 vendor/_probe_igdb.py --gap 0.6       # 更客气一点

⚠ 会真联网。``design.zaparoo.org`` 不是 IGDB 官方 API（原因见
``cover_igdb.py`` 开头），它挂了这个探针会如实报错，不会静默给 0。
"""

import argparse
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import cover_art as ca                             # noqa: E402
import cover_igdb as cg                            # noqa: E402
import joshw_source as js                          # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--n", type=int, default=10, help="每个机种抽几条（默认 10）")
    ap.add_argument("--keys", default="", help="只测这些机种，逗号分隔")
    ap.add_argument("--gap", type=float, default=cg.MIN_GAP,
                    help="请求间隔秒（默认 %.2f）" % cg.MIN_GAP)
    ap.add_argument("--verbose", action="store_true", help="逐条打印匹配结果")
    a = ap.parse_args(argv)

    cg.MIN_GAP = max(0.15, a.gap)
    keys = ([k.strip() for k in a.keys.split(",") if k.strip()]
            or [p["key"] for p in js.PLATFORMS])
    rnd = random.Random(20261028)                  # 固定种子，数字可复现

    tot_hit = tot_all = 0
    rows_out = []
    print("== IGDB 命中率（每机种抽 %d 条，间隔 %.2fs）==" % (a.n, cg.MIN_GAP),
          flush=True)
    for key in keys:
        if not cg.supported(key):
            print("%-12s 跳过（没有 IGDB 平台映射）" % key, flush=True)
            continue
        try:
            items, _ts, src = js.best_index(key)
        except Exception as exc:                     # noqa: BLE001
            print("%-12s joshw 索引读不到：%s" % (key, exc), flush=True)
            continue
        if not items:
            continue
        sample = rnd.sample(items, min(a.n, len(items)))
        hit = 0
        t0 = time.time()
        for e in sample:
            title = str(e.get("name") or "")
            if not title:
                continue
            try:
                row, why = cg.search(key, title)
            except Exception as exc:                 # noqa: BLE001
                print("    ! %s → %s" % (title[:36], exc), flush=True)
                continue
            if row:
                hit += 1
                if a.verbose:
                    print("    ✓ %-42s → %s" % (title[:42], row.get("name")),
                          flush=True)
            elif a.verbose:
                print("    ✗ %-42s → %s" % (title[:42], why), flush=True)
        tot_hit += hit
        tot_all += len(sample)
        pct = round(100.0 * hit / max(1, len(sample)))
        rows_out.append((key, len(items), len(sample), hit, pct,
                         cg.PLATFORM_IDS[key]))
        print("%-12s 索引 %5d 条(%s) · 抽样 %2d → 命中 %2d (%3d%%) · %.1fs"
              % (key, len(items), src, len(sample), hit, pct, time.time() - t0),
              flush=True)

    print("\n| 机种 | IGDB 平台 | 抽样 | 命中 | 命中率 |")
    print("|---|---|---|---|---|")
    for key, _ni, ns, hit, pct, ids in rows_out:
        print("| `%s` | %s | %d | %d | **%d%%** |"
              % (key, ",".join(str(i) for i in ids), ns, hit, pct))
    print("\n合计 **%d / %d = %d%%**"
          % (tot_hit, tot_all, round(100.0 * tot_hit / max(1, tot_all))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
