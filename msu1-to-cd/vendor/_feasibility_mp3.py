#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_feasibility_mp3.py — MP3 转换路径的**可行性研究**（v5.9.18）。

用户报「转 MP3 有持续杂音」，要求研究转换路径是否可行。这个脚本把
libsndfile 的 MP3 编码器在各种输入条件下都过一遍，量化「哪里能用、
哪里不能用」。

测的维度：

  ① 输入 dtype：int16 / float32 / int32 —— 找杂音的根因
  ② 时长：0.1s / 0.5s / 1s / 5s / 30s —— 短曲目的帧对齐开销
  ③ 采样率：8000..48000 —— libsndfile 支持哪些、哪些会被改
  ④ 声道：1 / 2 —— 单声道会不会被强转
  ⑤ 极端信号：静音 / 满量程方波 / 白噪 —— 会不会削顶、会不会崩
  ⑥ 码率模式：默认 / CONSTANT / VARIABLE —— 码率与音质的取舍
  ⑦ ID3 APIC 插入前后：SNR 是否变化（封面不能碰音频）
  ⑧ 编码器延迟：解码回来的帧数与源是否一致（gapless 是否有效）

用法::

    py -3.14 vendor/_feasibility_mp3.py
    py -3.14 vendor/_feasibility_mp3.py --long   # 加上 30 秒那档
"""
import argparse
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _sig(seconds, sr, ch, kind="tone", amp=0.55):
    """造测试信号。kind: tone / noise / silence / square。"""
    import numpy as np
    n = max(1, int(round(sr * seconds)))
    t = np.arange(n) / float(sr)
    if kind == "tone":
        # 多频 + 渐强包络：非线性失真会产生明显互调
        w = (0.45 * np.sin(2 * math.pi * 440.0 * t)
             + 0.30 * np.sin(2 * math.pi * 1330.0 * t)
             + 0.20 * np.sin(2 * math.pi * 3170.0 * t))
        w = w * (0.15 + 0.85 * (t / max(t[-1], 1e-9)))
    elif kind == "noise":
        rng = np.random.default_rng(12345)
        w = rng.standard_normal(n) * 0.3
    elif kind == "square":
        w = np.sign(np.sin(2 * math.pi * 220.0 * t))
    else:
        w = np.zeros(n)
    w = np.clip(w * amp / max(1e-9, float(np.abs(w).max()) or 1.0), -1.0, 1.0)
    pcm = np.clip(w * 32767.0, -32768, 32767).astype("<i2")
    if ch > 1:
        pcm = np.stack([pcm] * ch, axis=1)
    else:
        pcm = pcm.reshape(-1, 1)          # 统一成 2D，下游少一堆分支
    return np.ascontiguousarray(pcm)


def _snr(ref, got):
    import numpy as np
    n = min(len(ref), len(got))
    r = ref[:n].astype(np.float64)
    g = got[:n].astype(np.float64)
    e = g - r
    ps, pe = float(np.dot(r, r)), float(np.dot(e, e))
    if ps <= 1e-9:
        return (99.0 if pe <= 1e-9 else -99.0), 1.0
    snr = 10.0 * math.log10(ps / pe) if pe > 1e-12 else 99.0
    corr = float(np.dot(r, g) / (math.sqrt(ps * float(np.dot(g, g))) + 1e-18))
    return snr, corr


def _row(label, ok, snr, corr, kbps, extra=""):
    print("  %-38s %-5s %7s %8s %8s  %s"
          % (label, "OK" if ok else "FAIL",
             ("%.1f" % snr) if snr is not None else "-",
             ("%.4f" % corr) if corr is not None else "-",
             ("%.0f" % kbps) if kbps is not None else "-", extra), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--long", action="store_true", help="加 30 秒档")
    args = ap.parse_args()

    import numpy as np
    import soundfile as sf
    import flac_out as fo

    tmp = tempfile.mkdtemp(prefix="feas_mp3_")
    print("soundfile %s / libsndfile %s" % (sf.__version__, sf.__libsndfile_version__),
          flush=True)
    print("临时目录 %s\n" % tmp, flush=True)

    def enc(arr, sr, ch, **kw):
        p = os.path.join(tmp, "x_%d.mp3" % abs(hash((arr.shape, sr, ch, str(kw)))))
        ok, _p, d = fo.encode_pcm(arr.tobytes(), p, sr, ch, fmt="mp3", **kw)
        return ok, p, d

    # ══ ① 输入 dtype：杂音的根因 ══
    print("[1] 输入 dtype（同一段 1 秒音频，只改 dtype）", flush=True)
    print("  %-38s %-5s %7s %8s %8s" % ("", "", "SNR", "相关", "kbps"), flush=True)
    base = _sig(1.0, 44100, 2)
    for dtype, conv in (
            ("int16（v5.9.15~17 的做法）", lambda a: a),
            ("float32 归一化（v5.9.18）", lambda a: a.astype(np.float32) / 32768.0),
            ("int32 左移 16", lambda a: a.astype(np.int32) << 16)):
        p = os.path.join(tmp, "dt_%s.mp3" % abs(hash(dtype)))
        arr = conv(base)
        try:
            sf.write(p, arr, 44100, format="MP3", subtype="MPEG_LAYER_III",
                     compression_level=0.0, bitrate_mode="CONSTANT")
        except Exception as e:
            _row(dtype, False, None, None, None, "写失败 %s" % str(e)[:30])
            continue
        got, _sr = sf.read(p, dtype="int16", always_2d=True)
        s, c = _snr(base[:, 0], got[:, 0])
        kb = os.path.getsize(p) * 8 / 1.0 / 1000
        _row(dtype, s >= 20, s, c, kb)

    # ══ ② 时长 ══
    print("\n[2] 时长（短曲目的帧对齐开销）", flush=True)
    print("  %-38s %-5s %7s %8s %8s" % ("", "", "SNR", "相关", "kbps"), flush=True)
    durs = [0.1, 0.5, 1.0, 5.0] + ([30.0] if args.long else [])
    for d in durs:
        a = _sig(d, 44100, 2)
        ok, p, det = enc(a, 44100, 2)
        if not ok:
            _row("%.1f 秒" % d, False, None, None, None, det[:40])
            continue
        got, _sr = sf.read(p, dtype="int16", always_2d=True)
        s, c = _snr(a[:, 0], got[:, 0])
        kb = os.path.getsize(p) * 8 / d / 1000
        _row("%.1f 秒（%d 帧）" % (d, a.shape[0]), s >= 18, s, c, kb,
             "解码帧数 %d（源 %d）" % (got.shape[0], a.shape[0]))

    # ══ ③ 采样率 ══
    print("\n[3] 采样率（MP3 只支持有限的几档）", flush=True)
    print("  %-38s %-5s %7s %8s %8s" % ("", "", "SNR", "相关", "kbps"), flush=True)
    for sr in (8000, 11025, 16000, 22050, 32000, 44100, 48000, 96000):
        a = _sig(1.0, sr, 2)
        ok, p, det = enc(a, sr, 2)
        if not ok:
            _row("%d Hz" % sr, False, None, None, None, det[:40])
            continue
        got, gsr = sf.read(p, dtype="int16", always_2d=True)
        s, c = _snr(a[:, 0], got[:, 0]) if gsr == sr else (None, None)
        kb = os.path.getsize(p) * 8 / 1.0 / 1000
        _row("%d Hz" % sr, gsr == sr and (s or 0) >= 15, s, c, kb,
             "" if gsr == sr else "⚠ 解码回来变成 %d Hz" % gsr)

    # ══ ④ 声道 ══
    print("\n[4] 声道数", flush=True)
    print("  %-38s %-5s %7s %8s %8s" % ("", "", "SNR", "相关", "kbps"), flush=True)
    for ch in (1, 2):
        a = _sig(1.0, 44100, ch)
        ok, p, det = enc(a, 44100, ch)
        if not ok:
            _row("%d 声道" % ch, False, None, None, None, det[:40])
            continue
        got, _sr = sf.read(p, dtype="int16", always_2d=True)
        s, c = _snr(a[:, 0], got[:, 0])
        kb = os.path.getsize(p) * 8 / 1.0 / 1000
        _row("%d 声道" % ch, got.shape[1] == ch and s >= 18, s, c, kb,
             "" if got.shape[1] == ch else "⚠ 解码回来 %d 声道" % got.shape[1])

    # ══ ⑤ 极端信号 ══
    print("\n[5] 极端信号（削顶 / 崩溃）", flush=True)
    print("  %-38s %-5s %7s %8s %8s" % ("", "", "SNR", "相关", "kbps"), flush=True)
    for kind, label in (("silence", "全静音"), ("square", "满量程方波"),
                        ("noise", "白噪")):
        a = _sig(1.0, 44100, 2, kind=kind, amp=1.0)
        ok, p, det = enc(a, 44100, 2)
        if not ok:
            _row(label, False, None, None, None, det[:40])
            continue
        got, _sr = sf.read(p, dtype="int16", always_2d=True)
        s, c = _snr(a[:, 0], got[:, 0])
        pk = int(np.abs(got).max())
        _row(label, ok, s, c, os.path.getsize(p) * 8 / 1000.0,
             "输出峰值 %d（源 %d）" % (pk, int(np.abs(a).max())))

    # ══ ⑥ 码率模式 ══
    print("\n[6] 码率模式（320k CBR 是这么来的）", flush=True)
    print("  %-38s %-5s %7s %8s %8s" % ("", "", "SNR", "相关", "kbps"), flush=True)
    a = _sig(5.0, 44100, 2)
    for cl, bm, label in (
            (None, None, "默认（不传参）"),
            (0.0, None, "compression_level=0.0"),
            (0.0, "VARIABLE", "level=0 + VARIABLE"),
            (0.0, "AVERAGE", "level=0 + AVERAGE"),
            (0.0, "CONSTANT", "level=0 + CONSTANT（v5.9.18）"),
            (0.2, "CONSTANT", "level=0.2 + CONSTANT")):
        p = os.path.join(tmp, "br_%s.mp3" % abs(hash(label)))
        kw = {}
        if cl is not None:
            kw["compression_level"] = cl
        if bm:
            kw["bitrate_mode"] = bm
        f32 = a.astype(np.float32) / 32768.0
        try:
            sf.write(p, f32, 44100, format="MP3", subtype="MPEG_LAYER_III", **kw)
        except Exception as e:
            _row(label, False, None, None, None, str(e)[:30])
            continue
        got, _sr = sf.read(p, dtype="int16", always_2d=True)
        s, c = _snr(a[:, 0], got[:, 0])
        _row(label, s >= 20, s, c, os.path.getsize(p) * 8 / 5.0 / 1000)

    # ══ ⑦ 封面插入前后 ══
    print("\n[7] 嵌 ID3 APIC 前后（封面不能碰音频）", flush=True)
    a = _sig(5.0, 44100, 2)
    ok, p, det = enc(a, 44100, 2)
    before, _sr = sf.read(p, dtype="int16", always_2d=True)
    s0, c0 = _snr(a[:, 0], before[:, 0])
    # 造一张小 PNG（不依赖外部文件）
    png = os.path.join(tmp, "c.png")
    import zlib
    import struct as _st

    def _mkpng(w, h):
        raw = b"".join(b"\x00" + bytes([(x * 7) % 256, (y * 11) % 256, 128] * 1)
                       * 0 + bytes([(x * 7) % 256, (y * 11) % 256, 128])
                       for y in range(h) for x in range(w))
        # 上面每行要连续，重写一遍更稳
        rows = []
        for y in range(h):
            row = b"\x00"
            for x in range(w):
                row += bytes([(x * 7) % 256, (y * 11) % 256, 128])
            rows.append(row)
        raw = b"".join(rows)

        def chunk(t, d):
            return (_st.pack(">I", len(d)) + t + d
                    + _st.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF))
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", _st.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6))
                + chunk(b"IEND", b""))
    with open(png, "wb") as fh:
        fh.write(_mkpng(64, 64))
    okp, why = fo.embed_id3_picture(p, png)
    after, _sr = sf.read(p, dtype="int16", always_2d=True)
    s1, c1 = _snr(a[:, 0], after[:, 0])
    _row("嵌入前", s0 >= 20, s0, c0, os.path.getsize(p) * 8 / 5.0 / 1000)
    _row("嵌入后（%s）" % ("成功" if okp else "失败"), s1 >= 20, s1, c1,
         os.path.getsize(p) * 8 / 5.0 / 1000,
         "音频段逐字节一致" if np.array_equal(before, after) else "⚠ 音频变了")
    _mark_pic = fo.has_picture(p)

    # ══ ⑧ 结论 ══
    print("\n[8] 结论", flush=True)
    print("  · libsndfile 的 MP3 编码器**能用**，但有两个坑：", flush=True)
    print("      ① 输入必须是 float32/int32，int16 会变成满耳杂音；", flush=True)
    print("      ② 不显式要求 CONSTANT 就只有 ~170 kbps VBR。", flush=True)
    print("  · 采样率只支持 8/11.025/12/16/22.05/24/32/44.1/48 kHz；", flush=True)
    print("      96000 会被拒或改档 —— 本工具恒 44100，不受影响。", flush=True)
    print("  · ID3 APIC 插在文件最前面，音频段不动（%s）。"
          % ("已验证" if np.array_equal(before, after) else "⚠ 有变化"), flush=True)
    print("  · 封面能读回：%s" % ("是" if _mark_pic else "否"), flush=True)

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n临时目录已清理", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
