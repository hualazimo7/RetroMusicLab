#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_cmp_encoder.py — libsndfile(MP3) vs lameenc(LAME) 编码质量对比。

背景：v5.9.18 修掉「int16 直传 → 削顶方波」后，全带 SNR 已经到 37~43 dB，
但用户仍反馈「声音一大就兹拉兹拉」。分频段量下来 16k-20k 只有 17 dB ——
怀疑是 libsndfile 内置 MP3 编码器（其实就是 LAME，但参数被写死）在高频段
不如手动调参的 LAME。

本脚本干三件事：
1. 校正 lameenc 的调用方式（``encode()`` 吃 **小端 int16 交织字节**，容易踩坑）；
2. 同一段源音频分别用 libsndfile / lameenc 编码，解码回来做**分频段 SNR**；
3. 打印体积、码率，判断是否值得换编码器。

用法::

    py -3.14 vendor/_cmp_encoder.py
    py -3.14 vendor/_cmp_encoder.py --src=path/to.flac
"""
import argparse
import os
import sys

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEFAULT_SRC = (r"D:\MSU1Downloads\joshw\Game Boy Advance\FLAC"
               r"\Advance Wars 2 - Black Hole Rising (2003-06-24)"
               r"(Intelligent Systems)(Nintendo)[GBA]\01_01 Advance Wars 2 Opening.flac")

BANDS = [(0, 500), (500, 2000), (2000, 4000), (4000, 8000),
         (8000, 12000), (12000, 16000), (16000, 20000)]


def band_snr(ref, got, sr, bands=BANDS):
    """逐频段 SNR。ref/got 必须是同长、已对齐的 (n, ch) float64。"""
    out = []
    n = len(ref)
    if n < 4096:
        return out
    # 整段 FFT（单声道混合），一次算完再按频段切能量
    spec_r = np.fft.rfft(ref.mean(axis=1))
    spec_g = np.fft.rfft(got.mean(axis=1))
    err = spec_r - spec_g
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    for lo, hi in bands:
        m = (freqs >= lo) & (freqs < hi)
        if not m.any():
            continue
        pr = float(np.sum(np.abs(spec_r[m]) ** 2))
        pe = float(np.sum(np.abs(err[m]) ** 2))
        if pe <= 0:
            out.append((lo, hi, 99.0))
        elif pr <= 0:
            out.append((lo, hi, -99.0))
        else:
            out.append((lo, hi, 10.0 * np.log10(pr / pe)))
    return out


def align(ref, got, max_lag=3000):
    """用中段互相关找最佳 lag，返回对齐后的 (ref, got) 及 lag。"""
    n = min(len(ref), len(got))
    if n < 8192:
        return ref, got, 0
    seg_len = min(65536, n // 3)
    s0 = n // 4
    a = ref[s0:s0 + seg_len, 0].astype(np.float64)
    best_lag, best_c = 0, -1e18
    for lag in range(-max_lag, max_lag + 1):
        p = s0 + lag
        if p < 0 or p + seg_len > len(got):
            continue
        c = float(np.dot(a, got[p:p + seg_len, 0].astype(np.float64)))
        if c > best_c:
            best_c, best_lag = c, lag
    lag = best_lag
    if lag >= 0:
        return ref[:n - lag], got[lag:n], lag
    return ref[-lag:n], got[:n + lag], lag


def snr_full(ref, got):
    err = ref - got
    pr = float(np.sum(ref ** 2))
    pe = float(np.sum(err ** 2))
    if pe <= 0:
        return 99.0
    return 10.0 * np.log10(pr / pe) if pr > 0 else -99.0


def corr(ref, got):
    a = ref.ravel().astype(np.float64)
    b = got.ravel().astype(np.float64)
    a = a - a.mean()
    b = b - b.mean()
    d = float(np.sqrt(np.dot(a, a) * np.dot(b, b)))
    return float(np.dot(a, b) / d) if d > 0 else 0.0


# ---------- 三种编码路径 ----------

def enc_libsndfile(pcm_i16, sr, path):
    """v5.9.18 现行路径：float32 归一化 + CONSTANT 320k。"""
    buf = pcm_i16.astype(np.float32) / 32768.0
    sf.write(path, buf, sr, format="MP3", subtype="MPEG_LAYER_III",
             compression_level=0.0, bitrate_mode="CONSTANT")
    return path


def enc_lameenc(pcm_i16, sr, path, bitrate=320, quality=2):
    """标准 LAME。encode() 要 **小端 int16 交织字节**。"""
    import lameenc
    ch = pcm_i16.shape[1] if pcm_i16.ndim == 2 else 1
    enc = lameenc.Encoder()
    enc.set_bit_rate(bitrate)
    enc.set_in_sample_rate(int(sr))
    enc.set_channels(int(ch))
    enc.set_quality(int(quality))
    data = enc.encode(np.ascontiguousarray(pcm_i16, dtype="<i2").tobytes())
    data += enc.flush()
    with open(path, "wb") as f:
        f.write(data)
    return path


def enc_lameenc_cbr(pcm_i16, sr, path, bitrate=320, quality=2):
    """LAME + 显式 CBR（禁 VBR）。"""
    import lameenc
    ch = pcm_i16.shape[1] if pcm_i16.ndim == 2 else 1
    enc = lameenc.Encoder()
    enc.set_bit_rate(bitrate)
    enc.set_in_sample_rate(int(sr))
    enc.set_channels(int(ch))
    enc.set_quality(int(quality))
    try:
        enc.set_vbr(0)          # 0 = CBR
    except Exception:
        pass
    data = enc.encode(np.ascontiguousarray(pcm_i16, dtype="<i2").tobytes())
    data += enc.flush()
    with open(path, "wb") as f:
        f.write(data)
    return path


def load(path, max_sec=None):
    a, sr = sf.read(path, dtype="int16", always_2d=True)
    if max_sec:
        a = a[: int(sr * max_sec)]
    return a, sr


def audio_bytes(path):
    """扣掉 ID3 块，只算音频段。"""
    with open(path, "rb") as f:
        b = f.read()
    if b[:3] == b"ID3" and len(b) > 10:
        sz = ((b[6] & 0x7F) << 21) | ((b[7] & 0x7F) << 14) | \
             ((b[8] & 0x7F) << 7) | (b[9] & 0x7F)
        end = 10 + sz
        if b[5] & 0x10:
            end += 10
        return len(b) - end
    return len(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--sec", type=float, default=0.0, help="只取前 N 秒（0=全曲）")
    ap.add_argument("--out", default=os.path.join(HERE, "_cmp_out"))
    args = ap.parse_args()

    src = args.src
    if not os.path.isfile(src):
        print("源文件不存在:", src)
        return 2
    os.makedirs(args.out, exist_ok=True)

    pcm, sr = load(src, args.sec or None)
    print(f"源: {os.path.basename(src)}  {sr} Hz  {pcm.shape[1]}ch  "
          f"{len(pcm)/sr:.2f}s  峰值={int(np.abs(pcm).max())}")
    print()

    cases = [
        ("libsndfile 320CBR", enc_libsndfile, {}),
        ("lameenc q2 320", enc_lameenc, {"bitrate": 320, "quality": 2}),
        ("lameenc q2 320CBR", enc_lameenc_cbr, {"bitrate": 320, "quality": 2}),
        ("lameenc q0 320CBR", enc_lameenc_cbr, {"bitrate": 320, "quality": 0}),
        ("lameenc q0 256CBR", enc_lameenc_cbr, {"bitrate": 256, "quality": 0}),
    ]

    rows = []
    for name, fn, kw in cases:
        p = os.path.join(args.out, name.replace(" ", "_") + ".mp3")
        try:
            fn(pcm, sr, p, **kw)
        except Exception as exc:
            print(f"{name:22s} 编码失败: {type(exc).__name__}: {exc}")
            continue
        dec, dsr = sf.read(p, dtype="int16", always_2d=True)
        assert dsr == sr, (dsr, sr)
        # 补齐声道
        if dec.shape[1] != pcm.shape[1]:
            dec = np.repeat(dec[:, :1], pcm.shape[1], axis=1)
        ref = pcm.astype(np.float64)
        got = dec.astype(np.float64)
        r2, g2, lag = align(ref, got)
        s = snr_full(r2, g2)
        c = corr(r2, g2)
        bands = band_snr(r2, g2, sr)
        ab = audio_bytes(p)
        kbps = ab * 8 / (len(pcm) / sr) / 1000
        rows.append((name, s, c, bands, ab, kbps, lag))
        print(f"{name:22s} SNR={s:6.2f} dB  相关={c:.4f}  lag={lag:5d}  "
              f"音频={ab:9d}B  {kbps:6.1f} kbps")

    if not rows:
        print("全部失败")
        return 1

    print()
    head = "编码器".ljust(22) + "全带".rjust(8)
    for lo, hi in BANDS:
        head += f"{lo//1000}-{hi//1000}k".rjust(9)
    print(head)
    print("-" * len(head.encode("gbk", "ignore")))
    for name, s, c, bands, ab, kbps, lag in rows:
        line = name.ljust(22) + f"{s:8.2f}"
        for _lo, _hi, v in bands:
            line += f"{v:9.1f}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
