#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/verify_p1_e2e.py — P1 四格式（KSS/GYM/SAP/AY）端到端验收。

验收判据（三条全过才算通过，缺素材的格式明确标"未验收"，不伪造）：
  1. probe() 正确识别格式（非靠扩展名蒙对）
  2. 转出的 WAV **非静音**（峰值 > SIL_PEAK=300，与 chip_decode 的空轨阈值同口径）
  3. 轨数与 libgme `gme_track_count()` 一致

用法:
    python vendor/verify_p1_e2e.py
    python vendor/verify_p1_e2e.py --secs 8     # 每个文件解8 秒（默认 6）
    python vendor/verify_p1_e2e.py --fmt ay    # 只跑某一种格式
"""

import argparse
import array
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chip_decode                                   # noqa: E402
import chip_formats as cf                # noqa: E402
import gme_probe                                    # noqa: E402
import msu1_to_cd as core                          # noqa: E402

SIL_PEAK = 300          # 与 chip_decode.SIL_PEAK 同口径
BYTES_PER_SEC = 44100 * 2 * 2

FMTS = ("kss", "gym", "sap", "ay")


def peak_of_pcm(pcm):
    """int16 小端字节流峰值。"""
    if isinstance(pcm, (bytes, bytearray, memoryview)):
        a = array.array("h")
        a.frombytes(bytes(pcm))
    else:                       # _SpoolPcm
        a = array.array("h")
        a.frombytes(bytes(pcm))
    return max(max(a), -min(a)) if a else 0


def run_one(gme, path, secs):
    """跑一个文件，返回结果 dict。"""
    base = os.path.basename(path)
    info = cf.probe(path)
    r = gme_probe.probe_one(gme, path)      # 不解码，只问元信息（快）

    row = {
        "file": base, "fmt": info.fmt, "probe_ok": info.ok,
        "conflict": info.conflict, "probe_n": info.n_tracks,
        "gme_n": r.get("ntrack"), "reason": info.reason,
        "tracks": 0, "max_peak": 0, "secs": 0.0, "err": "",
    }
    row["ntrack_match"] = (info.n_tracks == r.get("ntrack"))
    if not r["ok"]:
        row["err"] = f"gme_open_file: {r.get('err')}"
        return row

    try:
        tracks = chip_decode.load_chip(path, default_secs=secs)
    except Exception as e:                                    # noqa: BLE001
        row["err"] = f"{type(e).__name__}: {e}"
        return row

    row["tracks"] = len(tracks)
    for t in tracks:
        n = len(t["pcm"])
        row["secs"] += n / float(BYTES_PER_SEC)
        try:
            row["max_peak"] = max(row["max_peak"], peak_of_pcm(t["pcm"]))
        except Exception:                                    # noqa: BLE001
            pass
    row["skips"] = getattr(chip_decode.load_chip_paths, "last_skips", [])
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(ROOT, "vendor", "samples"))
    ap.add_argument("--secs", type=float, default=6.0)
    ap.add_argument("--fmt", choices=FMTS, action="append")
    args = ap.parse_args()
    fmts = tuple(args.fmt) if args.fmt else FMTS

    gme = core._load_gme()
    grand_ok = True
    summary = []

    for sub in fmts:
        pats = {"kss": "*.kss", "gym": "*.gym", "sap": "*.sap", "ay": "*.ay"}
        files = sorted(glob.glob(os.path.join(args.dir, sub, pats[sub])))
        files += sorted(glob.glob(os.path.join(args.dir, sub, pats[sub].upper())))
        files = sorted(set(files))
        print(f"\n{'=' * 78}\n### {sub.upper()}  ({len(files)} 个样本)")
        if not files:
            print("  !! 本轮无可用样本 —— 未验收")
            summary.append((sub.upper(), "未验收", "没有样本"))
            grand_ok = False
            continue

        fmt_ok = True
        for path in files:
            row = run_one(gme, path, args.secs)
            ok = (row["probe_ok"] and not row["err"]
                  and row["ntrack_match"] and row["max_peak"] > SIL_PEAK)
            if not ok:
                fmt_ok = False
            grand_ok = grand_ok and ok
            flag = "PASS" if ok else "FAIL"
            print(f"  [{flag}] {row['file']}")
            print(f"         fmt={row['fmt']} probe_n={row['probe_n']} "
                  f"gme_n={row['gme_n']} match={row['ntrack_match']} "
                  f"conflicts={row['conflict']}")
            print(f"         产出 {row['tracks']} 轨 / {row['secs']:.2f}s / "
                  f"峰值 {row['max_peak']}"
                  + (f"  reason={row['reason']}" if row['reason'] else ""))
            if row["err"]:
                print(f"         !! {row['err']}")
        summary.append((sub.upper(), "PASS" if fmt_ok else "FAIL",
                        f"{len(files)} 个样本"))
        grand_ok = grand_ok and fmt_ok

    print(f"\n{'=' * 78}\n### 汇总")
    for name, verdict, note in summary:
        print(f"  {name:6s} {verdict}")
    print(f"\n总判定: {'全部通过' if grand_ok else '存在未通过项'}")
    return 0 if grand_ok else 1


if __name__ == "__main__":
    sys.exit(main())