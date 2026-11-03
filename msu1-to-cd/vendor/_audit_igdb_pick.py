#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_audit_igdb_pick.py — IGDB 兜底源「贴错封面」审计。

## 为什么要单独审

``_probe_igdb.py`` 只回答「**有没有**挑出一张图」，不回答「挑得**对不对**」。
而 ``cover_art.pick`` 有三档判据：

1. 归一化**全等** —— 不会错；
2. **副标题前缀**（必须带 `` - ``）—— 不会错，``Contra`` 匹不到
   ``Contra Hard Corps``；
3. ``difflib`` 相似度 ≥ 0.86 —— **这一档会错**。

第 3 档的具体危险：``Warcraft``（8 字符）vs ``Warcraft II``（11 字符），
长度差 3 在 ``max(6, len//2)`` 的粗筛以内，相似度 0.9 ≥ 0.86 → 判命中。
可那是两个不同的游戏。

## 这个脚本怎么判

把每一次命中的挑选**归到三档之一**，再把第 3 档的名字成对打出来，人眼扫一遍。
「贴错」的数量不是靠猜的，是靠这一张表看出来的。

跑法::

    py -3.14 vendor/_audit_igdb_pick.py --n 20
    py -3.14 vendor/_audit_igdb_pick.py --keys pc,switch,x360 --n 30
    py -3.14 vendor/_audit_igdb_pick.py --n 10 --only-fuzzy   # 只列模糊档

⚠ 会真联网（同一个公益后端，间隔照 ``cover_igdb.MIN_GAP``）。
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


def _classify(title, picked):
    """这一对（要的, 挑到的）属于哪一档。"""
    want, got = ca.norm_title(title), ca.norm_title(picked)
    if not want or not got:
        return "空"
    if want == got:
        return "全等"
    raw_want = ca._raw_title(title).lower()
    raw_got = ca._raw_title(picked).lower()
    if raw_want and raw_got and (raw_got.startswith(raw_want + " - ")
                                 or raw_want.startswith(raw_got + " - ")):
        return "副标题"
    return "模糊"


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--n", type=int, default=20, help="每个机种抽几条（默认 20）")
    ap.add_argument("--keys", default="", help="只测这些机种，逗号分隔")
    ap.add_argument("--gap", type=float, default=cg.MIN_GAP, help="请求间隔秒")
    ap.add_argument("--only-fuzzy", action="store_true",
                    help="只打印模糊档（人眼复核用）")
    a = ap.parse_args(argv)

    cg.MIN_GAP = max(0.15, a.gap)
    keys = ([k.strip() for k in a.keys.split(",") if k.strip()]
            or [p["key"] for p in js.PLATFORMS])
    rnd = random.Random(20261028)

    tally = {"全等": 0, "副标题": 0, "模糊": 0, "空": 0}
    fuzzy, miss = [], 0
    for key in keys:
        if not cg.supported(key):
            continue
        try:
            items, _ts, _src = js.best_index(key)
        except Exception as exc:                    # noqa: BLE001
            print("%-12s 索引读不到：%s" % (key, exc), flush=True)
            continue
        if not items:
            continue
        sample = rnd.sample(items, min(a.n, len(items)))
        for e in sample:
            title = str(e.get("name") or "")
            if not title:
                continue
            try:
                row, _why = cg.search(key, title)
            except Exception as exc:                # noqa: BLE001
                print("    ! %s → %s" % (title[:40], exc), flush=True)
                continue
            if not row:
                miss += 1
                continue
            got = str(row.get("name") or "")
            tier = _classify(title, got)
            tally[tier] = tally.get(tier, 0) + 1
            if tier == "模糊":
                fuzzy.append((key, title, got))
            elif tier == "全等" and not a.only_fuzzy:
                print("  = %-10s %s" % (key, title[:46]), flush=True)

    hit = tally["全等"] + tally["副标题"] + tally["模糊"]
    print("\n| 档位 | 数量 | 占命中 |")
    print("|---|---|---|")
    for k in ("全等", "副标题", "模糊"):
        print("| %s | %d | %d%% |"
              % (k, tally[k], round(100.0 * tally[k] / max(1, hit))))
    print("| **命中合计** | **%d** | |" % hit)
    print("| 未命中 | %d | |" % miss)

    if fuzzy:
        print("\n=== 模糊档（%d 条，逐条人眼复核）===" % len(fuzzy))
        for key, title, got in fuzzy:
            print("  [%s]\n    要的是 %s\n    挑到了 %s" % (key, title, got))
    else:
        print("\n模糊档为空 —— 没有任何一条是靠相似度兜进来的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
