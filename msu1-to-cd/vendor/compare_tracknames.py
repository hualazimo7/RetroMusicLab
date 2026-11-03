#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/compare_tracknames.py — 曲名「改前 vs 改后」逐格式对照（修复影响面评估）。

## 为什么需要这个脚本
修`gme_track_info` 调用约定（`gme_info_t**` + 字段对齐）之后，
`_chip_track_name()` 第一次真正拿得到 libgme 的 `song`。这**理论上**会改变
NSF/NSFE/GBS/HES 的 WAV 文件名 —— 而文件名是用户可见产物，改动必须先报备。

但"理论上会变"不等于"真的会变"。本脚本把两种情况**并排跑出来**，用数据回答，
而不是靠推断：

  · 「改前」= 复现旧 bug 的效果（libgme 侧字段全None）→ 走文件名兜底
  · 「改后」= 真实的 core._gme_track_info()返回值

## 关键判据：曲名质量闸门
libgme 的 song 并非总是有用。本脚本同时报告：
  · song 为空            -> 必然走兜底，不算变化
  · song 等于文件名主体-> 无信息量，按要求不用
  · song 是真曲名-> 采用（这是改进）
所以「哪些格式真的会变」和「变了之后是变好还是变差」都能一眼看出。

用法:
    python vendor/compare_tracknames.py
    python vendor/compare_tracknames.py --dir vendor/samples --per 2
"""

import argparse
import ctypes
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chip_formats as cf                # noqa: E402
import msu1_to_cd as core                          # noqa: E402

#: 要对比的格式 -> (子目录, 通配)
FMTS = [("NSF", "nsf", "*.nsf"), ("NSFE", "nsfe", "*.nsfe"),
        ("GBS", "gbs", "*.gbs"), ("HES", "hes", "*.hes"),
        ("VGM", "smd", "*.vgm"), ("GYM", "gym", "*.gym"),
        ("AY", "ay", "*.ay"), ("SAP", "sap", "*.sap"),
        ("KSS", "kss", "*.kss"), ("SPC", "", "sample.spc")]


def name_before(path, fmt, info, ti, ntrack):
    """复现「改前」：libgme 侧字段全 None（旧 bug 的实际效果）。"""
    import chip_decode
    return chip_decode._chip_track_name(
        path, fmt, info, None, ti, ntrack)     # libgme_info=None


def name_after(path, fmt, info, libinfo, ti, ntrack):
    """「改后」：真实 libgme 字段。"""
    import chip_decode
    return chip_decode._chip_track_name(
        path, fmt, info, libinfo, ti, ntrack)


def quality(song, filename_base):
    """判断 libgme 的 song 是否有采用价值。返回 (等级, 说明)。

    等级：'empty' 空串 / 'same' 等于文件名主体（无信息量）/ 'good' 真曲名
    """
    s = (song or "").strip()
    if not s:
        return "empty", "空串 -> 走兜底"
    fn = (filename_base or "").strip()
    # 文件名主体去掉前导序号后与曲名比（"204 People Without Hope" -> "People..."）
    import re
    fn_body = re.sub(r"^\d+\s+", "", fn).strip()
    if s.lower() == fn.lower() or s.lower() == fn_body.lower():
        return "same", "等于文件名主体 -> 无信息量"
    return "good", "真曲名 -> 采用"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(ROOT, "vendor", "samples"))
    ap.add_argument("--per", type=int, default=1, help="每格式几个样本")
    ap.add_argument("--max-tracks", type=int, default=3)
    args = ap.parse_args()

    import glob
    import chip_decode

    gme = core._load_gme()
    changed_total = 0
    rows = []

    print("=" * 78)
    print("曲名影响面：NSF/NSFE/GBS/HES 等格式 「改前 -> 改后」")
    print("=" * 78)

    for fmt, sub, pat in FMTS:
        if sub:
            files = [f for f in sorted(glob.glob(
                os.path.join(args.dir, sub, pat)))
                if "_rejected" not in f.split(os.sep)][:args.per]
        else:
            p = os.path.join(ROOT, "vendor", pat)
            files = [p] if os.path.exists(p) else []
        if not files:
            continue
        for path in files:
            info = cf.probe(path)
            emu = ctypes.c_void_p()
            err = gme.gme_open_file(os.path.abspath(path).encode("utf-8"),
                                    ctypes.byref(emu), 44100)
            if err:
                print(f"\n### {fmt}  {os.path.basename(path)}: open 失败")
                continue
            try:
                ntrack = gme.gme_track_count(emu)
                base = os.path.splitext(os.path.basename(path))[0]
                print(f"\n### {fmt}  {os.path.basename(path)[:56]}")
                print(f"    轨数={ntrack}  文件名主体={base[:60]!r}")
                shown = min(ntrack, args.max_tracks)
                for ti in range(shown):
                    gme.gme_start_track(emu, ti)
                    lib = core._gme_track_info(gme, emu, ti)
                    nb = name_before(path, info.fmt, info, ti, ntrack)
                    na = name_after(path, info.fmt, info, lib, ti, ntrack)
                    song = (lib or {}).get("song", "")
                    lvl, why = quality(song, base)
                    mark = "  <== 变化" if nb != na else ""
                    print(f"    [{ti}] song={song[:44]!r}  [{lvl}: {why}]")
                    print(f"         改前: {nb}")
                    print(f"         改后: {na}{mark}")
                    if nb != na:
                        changed_total += 1
                        rows.append((fmt, os.path.basename(path), ti, nb, na))
                if ntrack > shown:
                    print(f"    ... 共 {ntrack} 轨")
            finally:
                gme.gme_delete(emu)

    print("\n" + "=" * 78)
    print(f"汇总：共 {changed_total} 处曲名变化")
    if rows:
        print("\n变化清单（改前 -> 改后）：")
        for fmt, f, ti, a, b in rows:
            print(f"  [{fmt}] {f[:40]} 轨{ti}")
            print(f"      {a}")
            print(f"   -> {b}")
    else:
        print("  **没有任何曲名变化** —— 修复只影响元数据正确性与内存，"
              "不改变用户可见文件名。")
    print("\n说明：NSF/GBS/HES 本身**没有 song 字段**（libgme 返回空串），")
    print("      故这三类必然走文件名兜底；NSFE 的逐轨曲名来自 chip_formats 的")
    print("      tlbl 块（优先级 1a，高于 libgme song），也不受影响。")
    return 0


if __name__ == "__main__":
    sys.exit(main())