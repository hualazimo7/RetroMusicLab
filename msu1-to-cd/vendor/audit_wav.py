# -*- coding: utf-8 -*-
"""批量体检 WAV 输出: 时长 / 连续性 / 音量 / 结尾, 用来抓"半缓冲"这类
会在听感上变成"变慢、节奏错乱"的缺陷。

关键指标
  same_ratio  相邻样本完全相同的比例。缓冲区半填充的坏文件会高达 60%+;
              正常音乐(含 SNES 原生低采样率)通常 < 5%。
  gaps        内部 >=0.5s 的静音段数。异常时会出现等间隔的规律静音。
  peak        峰值, 用于发现削波或近乎无声。
"""
import glob
import os
import sys
import wave

import numpy as np


def audit(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    mono = a.reshape(-1, ch).mean(axis=1)
    dur = n / sr
    same = float((np.diff(mono) == 0).mean()) if len(mono) > 2 else 0.0
    win = int(sr * 0.01)
    m = len(mono) // win
    env = np.sqrt((mono[:m * win].reshape(m, win) ** 2).mean(axis=1))
    peak = float(np.abs(mono).max())
    thr = max(1e-4, env.max() * 0.02)
    sil = env < thr
    gaps, i = [], 0
    while i < m:
        if sil[i]:
            j = i
            while j < m and sil[j]:
                j += 1
            if (j - i) * 0.01 >= 0.5:
                gaps.append(round((j - i) * 0.01, 2))
            i = j
        else:
            i += 1
    tail = env[-50:].mean() if m > 50 else 0.0
    return {"dur": dur, "same": same, "gaps": gaps, "peak": peak,
            "rms": float(env.mean()), "tail": float(tail)}


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "WAV_输出"
    files = sorted(glob.glob(os.path.join(d, "*.wav")))
    if not files:
        print("没有 WAV:", d)
        return 1
    bad_same, bad_gap, bad_peak = [], [], []
    tot = 0.0
    print("%-40s %7s %6s %5s %6s %6s" % ("文件", "时长", "相同%", "隙数", "峰值", "尾部"))
    for f in files:
        r = audit(f)
        tot += r["dur"]
        flag = ""
        if r["same"] > 0.10:
            bad_same.append((os.path.basename(f), r["same"]))
            flag += " SAME"
        if len(r["gaps"]) > 8:
            bad_gap.append((os.path.basename(f), len(r["gaps"])))
            flag += " GAPS"
        if r["peak"] < 0.02:
            bad_peak.append((os.path.basename(f), r["peak"]))
            flag += " QUIET"
        if r["same"] > 0.10 or len(r["gaps"]) > 8 or r["peak"] < 0.02:
            print("%-40s %7.1f %6.1f %5d %6.3f %6.4f%s"
                  % (os.path.basename(f)[:40], r["dur"], r["same"] * 100,
                     len(r["gaps"]), r["peak"], r["tail"], flag))
    print("\n合计 %d 首, 总时长 %d:%04.1f" % (len(files), tot // 60, tot % 60))
    print("相邻样本重复超标: %d 首" % len(bad_same))
    print("规律静音段偏多: %d 首" % len(bad_gap))
    print("几乎无声: %d 首" % len(bad_peak))
    if bad_same:
        for n, v in bad_same[:6]:
            print("   SAME %-34s %.1f%%" % (n[:34], v * 100))
    return 0 if not (bad_same or bad_peak) else 2


if __name__ == "__main__":
    sys.exit(main())
