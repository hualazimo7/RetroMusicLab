#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_xdec_mp3.py — 三解码器交叉验证 + FFmpeg(libmp3lame) 基准编码。

为什么非做不可：

    ``soundfile`` 编出来的 MP3 用 ``soundfile`` 解回来，SNR 好看 —— 但这是
    **同一个 libsndfile 生态**。万一编码器写的东西不合标准、只是 libsndfile
    自己容错解得动，用户换个播放器就是「持续杂音」。

    所以这里用三个**互相独立**的解码器解同一个文件：

        libsndfile（soundfile）
        FFmpeg      （PyAV 自带）
        dr_mp3      （miniaudio 自带）

    三者解出来的波形应该几乎逐样本一致。只要有一个明显不同，说明文件
    不标准 —— 那就是真 bug。

    顺带用 FFmpeg 的 ``libmp3lame`` 做一个 **320k CBR 基准编码**，跟
    libsndfile 的编码比 SNR，判断该不该换编码器。

用法::

    py -3.14 vendor/_xdec_mp3.py <src.flac> <a.mp3>
    py -3.14 vendor/_xdec_mp3.py --default
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

BANDS = [(0, 2000), (2000, 8000), (8000, 16000), (16000, 20000)]


# ---------------- 三个解码器 ----------------

def dec_soundfile(path):
    a, sr = sf.read(path, dtype="int16", always_2d=True)
    return a, sr


def dec_pyav(path):
    import av
    with av.open(path) as c:
        st = c.streams.audio[0]
        sr = st.rate
        ch = st.channels
        chunks = []
        for fr in c.decode(st):
            arr = fr.to_ndarray()          # (ch, n) float or int
            chunks.append(arr)
    if not chunks:
        raise RuntimeError("PyAV 解出 0 帧")
    a = np.concatenate(chunks, axis=1)
    a = a.T                            # (n, ch)
    if a.shape[1] != ch:
        a = a.reshape(-1, ch)
    # 统一成 int16
    if a.dtype.kind == "f":
        a = np.clip(np.round(a * 32768.0), -32768, 32767).astype(np.int16)
    else:
        a = a.astype(np.int16)
    return a, sr


def dec_miniaudio(path):
    import miniaudio
    d = miniaudio.decode_file(path, output_format=miniaudio.SampleFormat.SIGNED16)
    a = np.frombuffer(d.samples, dtype=np.int16)
    ch = d.nchannels
    a = a.reshape(-1, ch)
    return a, d.sample_rate


DECODERS = [("libsndfile", dec_soundfile),
            ("FFmpeg", dec_pyav),
            ("dr_mp3", dec_miniaudio)]


# ---------------- 度量 ----------------

def align(ref, got, max_lag=3000):
    n = min(len(ref), len(got))
    if n < 8192:
        return ref, got, 0
    seg = min(65536, n // 3)
    s0 = n // 4
    a = ref[s0:s0 + seg, 0].astype(np.float64)
    best_lag, best_c = 0, -1e18
    for lag in range(-max_lag, max_lag + 1):
        p = s0 + lag
        if p < 0 or p + seg > len(got):
            continue
        c = float(np.dot(a, got[p:p + seg, 0].astype(np.float64)))
        if c > best_c:
            best_c, best_lag = c, lag
    lag = best_lag
    if lag >= 0:
        return ref[:n - lag], got[lag:n], lag
    return ref[-lag:n], got[:n + lag], lag


def snr_db(ref, got):
    e = ref - got
    pr = float(np.sum(ref ** 2))
    pe = float(np.sum(e ** 2))
    return 10 * np.log10(pr / pe) if pr > 0 and pe > 0 else 99.0


def band_snr(ref, got, sr):
    n = len(ref)
    if n < 4096:
        return []
    fr = np.fft.rfft(ref.mean(axis=1))
    fg = np.fft.rfft(got.mean(axis=1))
    fe = fr - fg
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    out = []
    for lo, hi in BANDS:
        m = (freqs >= lo) & (freqs < hi)
        if not m.any():
            continue
        pr = float(np.sum(np.abs(fr[m]) ** 2))
        pe = float(np.sum(np.abs(fe[m]) ** 2))
        out.append((lo, hi, 10 * np.log10(pr / pe) if pr > 0 and pe > 0 else 99.0))
    return out


def peak(a):
    return int(np.abs(a).max())


def clip_count(a, thr=32700):
    return int(np.sum(np.abs(a) >= thr))


# ---------------- FFmpeg 基准编码 ----------------

def enc_ffmpeg_lame(pcm_i16, sr, path, bitrate=320):
    import av
    ch = pcm_i16.shape[1]
    layout = "stereo" if ch == 2 else "mono"
    with av.open(path, "w", format="mp3") as c:
        st = c.add_stream("libmp3lame", rate=sr, layout=layout)
        st.bit_rate = bitrate * 1000
        # 交给编码器
        try:
            st.codec_context.format = "s16"
        except Exception:
            pass
        data = np.ascontiguousarray(pcm_i16)
        # 分块喂，避免一次性拷太大
        step = sr * 2
        for i in range(0, len(data), step):
            blk = data[i:i + step]
            fr = av.AudioFrame.from_ndarray(
                blk.T.reshape(1, -1).astype(np.int16),
                format="s16", layout=layout)
            fr.sample_rate = sr
            fr.pts = None
            for pkt in st.encode(fr):
                c.mux(pkt)
        for pkt in st.encode(None):
            c.mux(pkt)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("flac", nargs="?")
    ap.add_argument("mp3", nargs="?")
    ap.add_argument("--default", action="store_true")
    ap.add_argument("--sec", type=float, default=0.0)
    args = ap.parse_args()

    src_p, mp3_p = args.flac, args.mp3
    if args.default or not src_p:
        src_p, mp3_p = DEFAULT_FLAC, DEFAULT_MP3
    if not (os.path.isfile(src_p) and os.path.isfile(mp3_p)):
        print("文件不存在:", src_p, mp3_p)
        return 2

    src, sr = sf.read(src_p, dtype="int16", always_2d=True)
    if args.sec:
        src = src[: int(sr * args.sec)]
    print(f"源: {os.path.basename(src_p)}  {sr}Hz {src.shape[1]}ch "
          f"{len(src)/sr:.2f}s 峰值 {peak(src)} 削顶 {clip_count(src)}")
    print(f"待验: {os.path.basename(mp3_p)}")
    print()

    print("【1】三解码器交叉验证（同一文件，不同解码器）")
    print("    解码器         帧数        峰值    削顶   与源SNR   与源相关")
    base = None
    for name, fn in DECODERS:
        try:
            a, asr = fn(mp3_p)
        except Exception as exc:
            print(f"    {name:12s} 失败: {type(exc).__name__}: {exc}")
            continue
        if asr != sr:
            print(f"    {name:12s} 采样率 {asr} != {sr}，跳过")
            continue
        if a.shape[1] != src.shape[1]:
            a = np.repeat(a[:, :1], src.shape[1], axis=1)
        r2, g2, lag = align(src.astype(np.float64), a.astype(np.float64))
        s = snr_db(r2, g2)
        aa = r2.ravel() - r2.ravel().mean()
        bb = g2.ravel() - g2.ravel().mean()
        c = float(np.dot(aa, bb) / np.sqrt(np.dot(aa, aa) * np.dot(bb, bb)))
        print(f"    {name:12s} {len(a):8d} {peak(a):8d} {clip_count(a):6d} "
              f"{s:9.2f} {c:9.5f}  lag={lag}")
        if base is None:
            base = (name, a)
        else:
            n = min(len(base[1]), len(a))
            d = base[1][:n].astype(np.float64) - a[:n].astype(np.float64)
            ds = snr_db(base[1][:n].astype(np.float64), a[:n].astype(np.float64))
            print(f"        vs {base[0]}: 逐样本 SNR {ds:6.2f} dB  "
                  f"最大差 {int(np.abs(d).max())}")
    print()

    print("【2】FFmpeg(libmp3lame) 320k 基准编码，与 libsndfile 编码对比")
    ref = src.astype(np.float64)
    rows = []

    # a) 现有文件
    a, _ = dec_soundfile(mp3_p)
    if a.shape[1] != src.shape[1]:
        a = np.repeat(a[:, :1], src.shape[1], axis=1)
    r2, g2, _ = align(ref, a.astype(np.float64))
    rows.append(("现有 libsndfile 320CBR", r2, g2))

    # b) FFmpeg libmp3lame
    tmp = os.path.join(HERE, "_xdec_lame.mp3")
    try:
        enc_ffmpeg_lame(src, sr, tmp, 320)
        b, bsr = dec_soundfile(tmp)
        if b.shape[1] != src.shape[1]:
            b = np.repeat(b[:, :1], src.shape[1], axis=1)
        r3, g3, lag3 = align(ref, b.astype(np.float64))
        rows.append(("FFmpeg libmp3lame 320", r3, g3))
        print(f"    FFmpeg 编码完成: {os.path.getsize(tmp)}B  lag={lag3}")
    except Exception as exc:
        print(f"    FFmpeg 编码失败: {type(exc).__name__}: {exc}")

    print()
    head = "    编码器".ljust(28) + "全带".rjust(9)
    for lo, hi in BANDS:
        head += f"{lo//1000}-{hi//1000}k".rjust(10)
    print(head)
    for name, r, g in rows:
        line = "    " + name.ljust(24) + f"{snr_db(r, g):9.2f}"
        for _lo, _hi, v in band_snr(r, g, sr):
            line += f"{v:10.1f}"
        print(line)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
