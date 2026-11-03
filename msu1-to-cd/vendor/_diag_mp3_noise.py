#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_diag_mp3_noise.py — 查「转出来的 MP3 有持续杂音」。

思路：把一段**真音频**走一遍 ``flac_out.encode_pcm(fmt="mp3")``，再解码回来，
和源做对齐后算 SNR / 相关系数。SNR 低 = 真有杂音；SNR 正常（>20dB）说明
编码没问题，杂音来自别处（播放器 / 采样率 / 声道 / ID3 块）。

同时对比「带封面」和「不带封面」两条路 —— 如果只有带封面那条坏，
就是 ID3 插入把音频写坏了。

用法::

    py -3.14 vendor/_diag_mp3_noise.py
    py -3.14 vendor/_diag_mp3_noise.py --src=path/to.flac
"""
import argparse
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEFAULT_SRC = (r"D:\MSU1Downloads\joshw\Game Boy Advance\FLAC"
               r"\Advance Wars 2 - Black Hole Rising (2003-06-24)"
               r"(Intelligent Systems)(Nintendo)[GBA]\01_01 Advance Wars 2 Opening.flac")
COVER = (r"D:\MSU1Downloads\joshw\Game Boy Advance\FLAC"
         r"\Advance Wars 2 - Black Hole Rising (2003-06-24)"
         r"(Intelligent Systems)(Nintendo)[GBA]\cover.png")


def align_and_snr(ref, got, max_lag=4096):
    """在 ±max_lag 内找最佳对齐，返回 (lag, snr_db, corr)。"""
    import numpy as np
    ref = ref.astype(np.float64)
    got = got.astype(np.float64)
    n = min(len(ref), len(got))
    if n < 8192:
        return 0, -99.0, 0.0
    # 用中段做互相关，避开首尾（编码器延迟 / 填充）
    seg = slice(n // 4, n // 4 + 65536)
    a = ref[seg]
    best = (0, -1e18)
    for lag in range(-max_lag, max_lag + 1, 1):
        s = n // 4 + lag
        if s < 0 or s + 65536 > len(got):
            continue
        b = got[s:s + 65536]
        c = float(np.dot(a, b))
        if c > best[1]:
            best = (lag, c)
    lag = best[0]
    # 按 lag 对齐整段
    if lag >= 0:
        r = ref[:n - lag]
        g = got[lag:n]
    else:
        r = ref[-lag:n]
        g = got[:n + lag]
    m = min(len(r), len(g))
    r, g = r[:m], g[:m]
    err = g - r
    ps = float(np.dot(r, r))
    pe = float(np.dot(err, err))
    snr = 10.0 * np.log10(ps / pe) if pe > 1e-12 and ps > 1e-12 else 99.0
    corr = float(np.dot(r, g) / (np.sqrt(ps * float(np.dot(g, g))) + 1e-18))
    return lag, snr, corr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--cover", default=COVER)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    import numpy as np
    import soundfile as sf
    import flac_out

    print("源：%s" % args.src, flush=True)
    print("封面：%s（%s）" % (args.cover, "有" if os.path.isfile(args.cover) else "无"),
          flush=True)
    data, sr = sf.read(args.src, dtype="int16", always_2d=True)
    print("源：%d 帧 / %d Hz / %d ch" % (data.shape[0], sr, data.shape[1]), flush=True)
    print("源峰值：%d（int16 满量程 32767）" % int(np.abs(data).max()), flush=True)
    raw = np.ascontiguousarray(data).tobytes()

    tmp = tempfile.mkdtemp(prefix="mp3noise_")
    cases = [("不带封面", None), ("带封面", args.cover if os.path.isfile(args.cover) else None)]
    try:
        for label, cov in cases:
            dst = os.path.join(tmp, "t_%s.mp3" % ("cover" if cov else "plain"))
            ok, _p, detail = flac_out.encode_pcm(raw, dst, sr, data.shape[1],
                                                 cover=cov, fmt="mp3")
            print("\n[%s] ok=%s %s" % (label, ok, detail), flush=True)
            if not ok:
                continue
            got, gsr = sf.read(dst, dtype="int16", always_2d=True)
            print("  解码回来：%d 帧 / %d Hz / %d ch  文件 %d B"
                  % (got.shape[0], gsr, got.shape[1], os.path.getsize(dst)), flush=True)
            if gsr != sr or got.shape[1] != data.shape[1]:
                print("  ⚠ 采样率/声道变了！源 %d/%d vs 回来 %d/%d"
                      % (sr, data.shape[1], gsr, got.shape[1]), flush=True)
            # 单声道化做 SNR（避免声道串扰干扰判断）
            ref = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
            gm = got.mean(axis=1) if got.shape[1] > 1 else got[:, 0]
            lag, snr, corr = align_and_snr(ref, gm)
            print("  对齐 lag=%d   SNR=%.2f dB   相关=%.5f" % (lag, snr, corr), flush=True)
            # 左右声道分别算，查串扰
            if data.shape[1] == 2 and got.shape[1] == 2:
                for ch in (0, 1):
                    _l, s2, c2 = align_and_snr(data[:, ch], got[:, ch])
                    print("    声道%d SNR=%.2f dB 相关=%.5f" % (ch, s2, c2), flush=True)
                # 左右是否被互换/混音
                _l, s_cross, c_cross = align_and_snr(data[:, 0], got[:, 1])
                print("    L→R 交叉 SNR=%.2f dB 相关=%.5f（应远低于同声道）"
                      % (s_cross, c_cross), flush=True)
    finally:
        if args.keep:
            print("\n保留：%s" % tmp, flush=True)
        else:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
