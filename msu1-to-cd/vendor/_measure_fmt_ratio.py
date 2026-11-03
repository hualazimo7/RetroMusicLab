# -*- coding: utf-8 -*-
"""vendor/_measure_fmt_ratio.py — 用**真实库里的 WAV** 量 FLAC/MP3/OGG 的体积比。

为什么要有这个：说明书里「约 WAV 的 28%」这种数字必须能量出来，不能拍。
样本取自用户自己的 WAV 库，跨机种抽样，编完立刻删临时产物。

    py -3.14 vendor/_measure_fmt_ratio.py --root "D:/MSU1Downloads/joshw" --n 8
"""
import argparse
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import numpy as np                                              # noqa: E402
import soundfile as sf                                          # noqa: E402
import flac_out                                                 # noqa: E402


def pick(root, n):
    """跨机种抽样：每个含 WAV 的机种目录里最多抽 2 首，凑够 n 首。"""
    pools = []
    for d in sorted(os.listdir(root)):
        p = os.path.join(root, d, "WAV")
        if not os.path.isdir(p):
            continue
        got = []
        for r, _ds, fs in os.walk(p):
            got += [os.path.join(r, f) for f in fs if f.lower().endswith(".wav")]
            if len(got) >= 2:
                break
        if got:
            pools.append((d, got))
    out = []
    random.seed(20261027)
    for _d, got in pools:
        out += got[:2]
    random.shuffle(out)
    return out[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"D:\MSU1Downloads\joshw")
    ap.add_argument("--n", type=int, default=8)
    a = ap.parse_args()

    files = pick(a.root, a.n)
    if not files:
        print("没找到 WAV 样本")
        return 1

    tmp = tempfile.mkdtemp(prefix="fmt_ratio_")
    rows = []
    print("%-52s %8s %8s %8s %8s" % ("样本", "WAV", "FLAC", "MP3", "OGG"))
    for i, f in enumerate(files):
        print("  [读] %s" % f, flush=True)
        try:
            data, sr = sf.read(f, dtype="int16", always_2d=True)
        except Exception as e:                                  # noqa: BLE001
            print("  跳过 %s（%s）" % (os.path.basename(f), e))
            continue
        secs = len(data) / float(sr)
        if secs < 20:                    # 太短的曲子体积比不稳，没参考价值
            continue
        wav = os.path.getsize(f)
        got = {}
        for fmt in ("flac", "mp3", "ogg"):
            out = os.path.join(tmp, "t%02d.%s" % (i, fmt))
            try:
                ok, _p, _why = flac_out._write(out, data, sr, 0.5, fmt=fmt)
                if not ok:
                    raise OSError(_why)
                got[fmt] = os.path.getsize(out)
            except Exception as e:                              # noqa: BLE001
                got[fmt] = 0
                print("  %s 编码失败：%s" % (fmt, e))
            finally:
                if os.path.exists(out):
                    os.remove(out)
        rows.append((wav, got.get("flac", 0), got.get("mp3", 0), got.get("ogg", 0)))
        print("%-52s %8.1f %8.1f %8.1f %8.1f  (%.1fs)"
              % (os.path.basename(f)[:50], wav / 1024.0, got.get("flac", 0) / 1024.0,
                 got.get("mp3", 0) / 1024.0, got.get("ogg", 0) / 1024.0, secs))

    if rows:
        w = sum(r[0] for r in rows) or 1
        print("\n合计 %d 首：" % len(rows))
        for k, idx in (("FLAC", 1), ("MP3", 2), ("OGG", 3)):
            s = sum(r[idx] for r in rows)
            print("  %-4s 占 WAV 的 %.1f%%" % (k, s * 100.0 / w))
    try:
        os.rmdir(tmp)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
