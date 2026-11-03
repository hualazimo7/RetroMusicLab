#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_chip_batching.py — _plan_runs 批次切分验证。

不需要样本 / DLL，合成输入即可跑。验证设计 §5.4 的三条不变量：
    1. **无丢失、无重叠**：每个文件的每个轨恰好出现在一个 run 里一次
    2. **磁盘峰值有界**：任一 run 的音频秒数 ≤ CHIP_RUN_SECS
    3. **run 数合理**：HES 256 轨不会被塞进一个 run（避免 6.3 GB 临时盘）

跑法：python vendor/test_chip_batching.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chip_decode as cd                                 # noqa: E402

_GREEN, _RED, _YEL, _RST = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    _GREEN = _RED = _YEL = _RST = ""

_pass = 0
_fail = 0
BPS = 176400          # 44.1k * 16bit * 2ch


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  {_GREEN}PASS{_RST} {name}" + (f"  {detail}" if detail else ""))
    else:
        _fail += 1
        print(f"  {_RED}FAIL{_RST} {name}  {detail}")
    return bool(cond)


def analyse(items):
    """跑 _plan_runs 并算出每个 run 的秒数/轨数。"""
    per = {}
    for it in items:
        p, n, s = cd._as_item(it)
        per[p] = (max(1, int(n or 1)), max(0.5, float(s or 150.0)))
    runs = cd._plan_runs(items)
    info = []
    for r in runs:
        secs = sum((c or per[p][0]) * per[p][1] for p, _f, c in r)
        tracks = sum((c or per[p][0]) for p, _f, c in r)
        info.append((secs, tracks, len(r), r))
    return runs, info, per


def check_invariants(label, items, expect_runs=None):
    print(f"\n{_YEL}=== {label}{_RST}")
    runs, info, per = analyse(items)
    segs = sum(len(r) for r in runs)
    mx = max(i[1] for i in info) if info else 0
    mxs = max(i[0] for i in info) if info else 0
    print(f"    runs={len(runs)} 段数={segs} 单run最多轨={mx} "
          f"单run最长音频={mxs:.0f}s 峰值spool≈{mxs * BPS / 1048576:.0f}MB")

    # 1 无丢失无重叠
    seen = {}
    for r in runs:
        for p, f, c in r:
            n = per[p][0]
            c = n - f if c is None else c
            for i in range(f, f + c):
                seen.setdefault(p, []).append(i)
    ok = True
    for p, (n, _s) in per.items():
        got = sorted(seen.get(p, []))
        if got != list(range(n)):
            ok = False
            print(f"      BAD {p}: 得到 {len(got)} 轨, 期望 {n}")
    check(f"{label} 轨无丢失无重叠", ok)

    # 2 磁盘峰值有界（允许 CHIP_MAX_TRACKS 造成的少量超出）
    over = [i for i in info if i[0] > cd.CHIP_RUN_SECS + 1]
    check(f"{label} 单run音频秒数 ≤ {cd.CHIP_RUN_SECS:.0f}s",
          not over, f"超出 {len(over)} 个")

    # 3 单 run 文件数 ≤ CHIP_MAX_FILES
    bad = [i for i in info if i[2] > cd.CHIP_MAX_FILES]
    check(f"{label} 单run文件数 ≤ {cd.CHIP_MAX_FILES}", not bad)

    if expect_runs is not None:
        check(f"{label} run 数 == {expect_runs}（设计 §5.4 表）",
              len(runs) == expect_runs, f"实际 {len(runs)}")
    return runs, info


def main():
    print(f"阈值: CHIP_RUN_SECS={cd.CHIP_RUN_SECS} "
          f"CHIP_MAX_TRACKS={cd.CHIP_MAX_TRACKS} "
          f"CHIP_MAX_FILES={cd.CHIP_MAX_FILES}")
    print(f"spool 换算: {cd.CHIP_RUN_SECS:.0f}s × 176400B = "
          f"{cd.CHIP_RUN_SECS * BPS / 1048576:.0f} MB")

    # 设计 §5.4 的表格逐行复现
    check_invariants("SPC 1轨×120s", [("a.spc", 1, 120)], expect_runs=1)
    check_invariants("NSF 8轨×150s", [("a.nsf", 8, 150)], expect_runs=1)
    check_invariants("VGM 6文件×18s",
                     [(f"v{i}.vgm", 1, 18) for i in range(6)], expect_runs=2)
    # HES 256 轨 × 150s = 38400s / 1800 ≈ 22 个 run
    runs, info = check_invariants("HES 256轨×150s", [("a.hes", 256, 150)])
    check("HES 被切成多个 run（不是 1 个巨无霸）", len(runs) >= 20,
          f"{len(runs)} 个")
    check("HES 峰值 spool ≤ 400MB（AC-17/H5）",
          max(i[0] for i in info) * BPS / 1048576 <= 400,
          f"{max(i[0] for i in info) * BPS / 1048576:.0f}MB")

    check_invariants("GBS 12轨×520s", [("a.gbs", 12, 520.25)])
    check_invariants("混批 HES+GBS+NSF",
                     [("h.hes", 256, 150), ("g.gbs", 12, 520.25),
                      ("n.nsf", 8, 150)])
    check_invariants("空输入", [])
    check_invariants("单文件 1 轨", [("x.vgm", 1, 18)], expect_runs=1)

    # 每 run 段数上限
    runs, info, per = analyse([(f"f{i}.nsf", 8, 150) for i in range(10)])
    bad = [i for i in info if i[1] > cd.CHIP_MAX_TRACKS]
    check("单run轨数 ≤ CHIP_MAX_TRACKS(32)", not bad,
          f"最大 {max(i[1] for i in info)}")

    print(f"\n结果: {_pass} 通过 / {_fail} 失败")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
