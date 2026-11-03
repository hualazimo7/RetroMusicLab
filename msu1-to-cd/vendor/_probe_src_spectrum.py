#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_probe_src_spectrum.py — 源音频的频段能量分布 + MP3 预回声体检。

为什么要做这个：

    v5.9.18 修完编码 bug 后，全带 SNR 已到 37~43 dB，但分频段量下来
    ``16k-20k`` 只有 17~19 dB。**不能直接下结论说「高频烂」** ——
    如果源本身在 16k 以上就没多少能量，那个频段的 SNR 只是在拿噪声
    除以噪声，数字难看但没有听觉意义。

    所以先量「源每个频段占总能量的比例（dB）」，再量 MP3 编码后该频段
    的实际误差电平（dBFS）。只有「源有能量 且 误差电平可闻」的频段，
    才是用户听到的「兹拉兹拉」。

另外量 **预回声**（pre-echo）：MP3 是 MDCT 编码，强瞬态前面会出现
一小段噪声（编码器延迟窗 576 样本）。这是「鼓点/爆音前有沙沙声」的
经典成因，和全带 SNR 无关。

用法::

    py -3.14 vendor/_probe_src_spectrum.py
"""
import argparse
import os
import sys

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_FLAC = (r"D:\MSU1Downloads\joshw\Game Boy Advance\FLAC"
                r"\Advance Wars 2 - Black Hole Rising (2003-06-24)"
                r"(Intelligent Systems)(Nintendo)[GBA]\01_01 Advance Wars 2 Opening.flac")
DEFAULT_MP3 = (r"D:\MSU1Downloads\joshw\Game Boy Advance\MP3"
               r"\Advance Wars 2 - Black Hole Rising (2003-06-24)"
               r"(Intelligent Systems)(Nintendo)[GBA]\01_01 Advance Wars 2 Opening.mp3")

BANDS = [(0, 500), (500, 2000), (2000, 4000), (4000, 8000),
         (8000, 12000), (12000, 16000), (16000, 20000), (20000, 22050)]


def band_energy_db(pcm, sr):
    """每频段能量，相对**全带**的 dB。"""
    x = pcm.mean(axis=1).astype(np.float64)
    spec = np.fft.rfft(x)
    p = np.abs(spec) ** 2
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    total = float(p.sum())
    out = []
    for lo, hi in BANDS:
        m = (freqs >= lo) & (freqs < hi)
        e = float(p[m].sum()) if m.any() else 0.0
        out.append((lo, hi, 10 * np.log10(e / total) if total > 0 and e > 0 else -999.0))
    return out


def dbfs(x):
    """绝对电平 dBFS（满量程 32768）。"""
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    return 20 * np.log10(rms / 32768.0) if rms > 0 else -999.0


def pre_echo(pcm, sr, win_ms=20.0):
    """在源最响的瞬态附近，量「瞬态之前 win_ms」窗口里的误差电平。

    返回 (瞬态位置, 瞬态前误差 dBFS, 瞬态处信号 dBFS)。
    """
    mono = pcm.mean(axis=1)
    hop = int(sr * 0.01)
    n = len(mono) // hop
    if n < 10:
        return -1, -999.0, -999.0
    frames = mono[: n * hop].reshape(n, hop)
    energy = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    # 找「前 100ms 安静、当前 10ms 突然很响」的位置：差分最大的点
    diff = np.zeros(n)
    diff[10:] = energy[10:] - energy[:-10]
    idx = int(np.argmax(diff))
    pos = idx * hop
    return pos, energy[idx], energy[idx]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flac", default=DEFAULT_FLAC)
    ap.add_argument("--mp3", default=DEFAULT_MP3)
    args = ap.parse_args()

    if not os.path.isfile(args.flac):
        print("FLAC 不存在:", args.flac)
        return 2
    src, sr = sf.read(args.flac, dtype="int16", always_2d=True)
    print(f"源: {os.path.basename(args.flac)}")
    print(f"    {sr} Hz / {src.shape[1]}ch / {len(src)/sr:.2f}s / "
          f"峰值 {int(np.abs(src).max())} / 整体 {dbfs(src):.1f} dBFS")
    print()

    print("【1】源的分频段能量（相对全带，dB）")
    be = band_energy_db(src, sr)
    for lo, hi, v in be:
        bar = "#" * max(0, int((v + 60) / 2)) if v > -999 else ""
        print(f"    {lo:5d}-{hi:5d} Hz : {v:8.1f} dB  {bar}")
    print()

    if not os.path.isfile(args.mp3):
        print("MP3 不存在，跳过误差分析:", args.mp3)
        return 0
    dec, dsr = sf.read(args.mp3, dtype="int16", always_2d=True)
    if dsr != sr:
        print(f"采样率不一致 {dsr} vs {sr}，跳过")
        return 0
    n = min(len(src), len(dec))
    a = src[:n].astype(np.float64)
    b = dec[:n].astype(np.float64)
    err = a - b
    print(f"MP3: {os.path.basename(args.mp3)}  解码 {len(dec)/dsr:.2f}s / "
          f"峰值 {int(np.abs(dec).max())}")
    print()
    print("【2】编码误差的分频段绝对电平（dBFS；越低越好，-60 以下基本不可闻）")
    eb = band_energy_db(err.astype(np.int16), sr) if np.abs(err).max() < 32767 else []
    spec_e = np.abs(np.fft.rfft(err.mean(axis=1))) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    for (lo, hi, src_db) in be:
        m = (freqs >= lo) & (freqs < hi)
        if not m.any():
            continue
        pe = float(spec_e[m].sum())
        # 该频段误差的 RMS（Parseval，已按 FFT 长度归一）
        rms = np.sqrt(pe / (n * n)) if pe > 0 else 0.0
        lvl = 20 * np.log10(rms / 32768.0) if rms > 0 else -999.0
        flag = "  <-- 源有能量" if src_db > -40 else ""
        print(f"    {lo:5d}-{hi:5d} Hz : 误差 {lvl:8.1f} dBFS   源 {src_db:8.1f} dB{flag}")
    print()

    print("【3】预回声（最强瞬态之前 20ms 的误差电平）")
    pos = -1
    mono = a.mean(axis=1)
    hop = int(sr * 0.01)
    nf = len(mono) // hop
    if nf > 12:
        frames = mono[: nf * hop].reshape(nf, hop)
        e = np.sqrt((frames ** 2).mean(axis=1))
        d = np.zeros(nf)
        d[10:] = e[10:] - e[:-10]
        idx = int(np.argmax(d))
        pos = idx * hop
        pre0 = max(0, pos - int(sr * 0.02))
        pre_err = err[pre0:pos]
        at_sig = a[pos:pos + hop]
        print(f"    瞬态位置 {pos/sr:.2f}s   瞬态处信号 {dbfs(at_sig):.1f} dBFS")
        print(f"    瞬态前 20ms 误差 {dbfs(pre_err):.1f} dBFS "
              f"(峰值 {int(np.abs(pre_err).max())})")
        # 对比：整曲平均误差
        print(f"    整曲误差 {dbfs(err):.1f} dBFS")
    print()

    # 结论提示
    hi_bands = [(lo, hi, v) for lo, hi, v in be if lo >= 12000]
    print("【4】判读")
    if hi_bands and max(v for _l, _h, v in hi_bands) < -50:
        print("    12k 以上源能量极低（< -50 dB）→ 高频段 SNR 难看但**听不出来**，")
        print("    「兹拉」不是高频细节丢失造成的。")
    else:
        print("    12k 以上源有明显能量 → 高频段误差确实可闻，考虑提高码率/换格式。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
