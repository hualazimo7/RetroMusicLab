#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_refix_mp3.py — 把 v5.9.15~17 转坏的 MP3 **就地重转**（v5.9.18）。

## 为什么需要它

v5.9.15~17 的 MP3 内容全是坏的（libsndfile 不认 int16 → 削顶方波 →
满耳杂音）。v5.9.18 修好了编码器，但**已经写出去的坏文件不会自己变好**。

两条修复路径：

1. **用户点一次「转 MP3」** —— v5.9.18 的记账带了 ``by`` 生产版本，
   ``already_done()`` 认出旧版本会重转。**推荐**，走完整流程。
2. **这个脚本** —— 直接从同名的 FLAC 重转（FLAC 是无损，等价于原始 PCM），
   不用重新解码音源，快得多。适合「已经有一整套 FLAC、只想修 MP3」的场景。

## 做法

对每个 ``<机种>/MP3/<游戏>/xx.mp3``：

1. 找 ``<机种>/FLAC/<游戏>/xx.flac``（去掉编号前缀后按**顺序**配对）；
2. 用 v5.9.18 的编码器重写成 ``xx.mp3``（320k CBR + 同名 ``cover.png``）；
3. 量 SNR 自证（< 20 dB 就报错，别把坏的当好的）。

## 用法

::

    py -3.14 vendor/_refix_mp3.py --root=D:\\MSU1Downloads\\joshw            # 干跑
    py -3.14 vendor/_refix_mp3.py --root=D:\\MSU1Downloads\\joshw --yes      # 真写

⚠ ``--yes`` 会**覆盖**已有的 MP3。覆盖前会把原文件大小/大小记进日志，
  但**不备份**（原文件内容是坏的，备份没有价值）。
"""
import argparse
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _snr(ref, got):
    import numpy as np
    n = min(len(ref), len(got))
    if n < 4410:
        return None
    r = ref[:n].astype("float64")
    g = got[:n].astype("float64")
    e = g - r
    ps, pe = float((r * r).sum()), float((e * e).sum())
    if ps <= 1e-9:
        return None
    return 10.0 * math.log10(ps / pe) if pe > 1e-12 else 99.0


def _games(root):
    """产出 ``(机种目录, 游戏名, MP3 目录, FLAC 目录)``。"""
    for plat in sorted(os.listdir(root)):
        pdir = os.path.join(root, plat)
        mp3_root = os.path.join(pdir, "MP3")
        if not os.path.isdir(mp3_root):
            continue
        flac_root = os.path.join(pdir, "FLAC")
        for game in sorted(os.listdir(mp3_root)):
            gdir = os.path.join(mp3_root, game)
            if not os.path.isdir(gdir):
                continue
            yield plat, game, gdir, os.path.join(flac_root, game)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"D:/MSU1Downloads/joshw")
    ap.add_argument("--yes", action="store_true", help="真写（不加只干跑）")
    ap.add_argument("--only", default="", help="只处理这些机种目录，逗号分隔")
    args = ap.parse_args()

    import numpy as np
    import soundfile as sf
    import flac_out

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print("库根不存在：%s" % root)
        return 2
    only = {x.strip() for x in args.only.split(",") if x.strip()}

    print("%s：%s\n" % ("重转" if args.yes else "干跑（加 --yes 才真写）", root),
          flush=True)
    tot = done = bad = skip = 0
    for plat, game, gdir, fdir in _games(root):
        if only and plat not in only:
            continue
        mp3s = sorted(f for f in os.listdir(gdir) if f.lower().endswith(".mp3"))
        if not mp3s:
            continue
        flacs = sorted(f for f in os.listdir(fdir) if f.lower().endswith(".flac")) \
            if os.path.isdir(fdir) else []
        if not flacs:
            print("  -  %s / %s：没有 FLAC 基准，跳过（用「转 MP3」重转）"
                  % (plat, game), flush=True)
            skip += len(mp3s)
            continue
        if len(flacs) != len(mp3s):
            print("  !  %s / %s：FLAC %d 个 vs MP3 %d 个 —— 只修**同名**的"
                  % (plat, game, len(flacs), len(mp3s)), flush=True)
        # ⚠ **必须按同名配对，不能按顺序**：顺序一旦错位，就会把 A 曲的音频
        #   写进 B 曲的文件（比杂音严重得多，而且听不出来是哪首错）。
        #   同名配不上就跳过，宁可少修不可错修。
        by_stem = {os.path.splitext(f)[0].lower(): f for f in flacs}
        pairs = []
        for m in mp3s:
            stem = os.path.splitext(m)[0]
            f = by_stem.get(stem.lower())
            if f:
                pairs.append((m, f))
            else:
                print("     - %-46s 没有同名 FLAC，跳过" % m[:46], flush=True)
                skip += 1
        if not pairs:
            continue
        img = os.path.join(gdir, "cover.png")
        if not os.path.isfile(img):
            img = os.path.join(fdir, "cover.png")
        img = img if os.path.isfile(img) else None

        print("  == %s / %s（%d 首）" % (plat, game, len(pairs)), flush=True)
        for m, f in pairs:
            tot += 1
            mp = os.path.join(gdir, m)
            fp = os.path.join(fdir, f)
            old = os.path.getsize(mp)
            try:
                data, sr = sf.read(fp, dtype="int16", always_2d=True)
            except Exception as e:                   # noqa: BLE001
                print("     ✗ %s：读 FLAC 失败 %s" % (m, e), flush=True)
                bad += 1
                continue
            if not args.yes:
                b, _s = sf.read(mp, dtype="int16", always_2d=True)
                s0 = _snr(data.mean(1) if data.shape[1] > 1 else data[:, 0],
                          b.mean(1) if b.shape[1] > 1 else b[:, 0])
                print("     · %-46s 现状 SNR %s dB → 将重转（%d B）"
                      % (m[:46], ("%.1f" % s0) if s0 is not None else "?", old),
                      flush=True)
                done += 1
                continue
            ok, _p, det = flac_out.encode_pcm(
                np.ascontiguousarray(data).tobytes(), mp, sr, data.shape[1],
                cover=img, fmt="mp3")
            if not ok:
                print("     ✗ %s：%s" % (m, det), flush=True)
                bad += 1
                continue
            # 自证：解码回来和 FLAC 比
            g, _s2 = sf.read(mp, dtype="int16", always_2d=True)
            s1 = _snr(data.mean(1) if data.shape[1] > 1 else data[:, 0],
                      g.mean(1) if g.shape[1] > 1 else g[:, 0])
            if s1 is None or s1 < 20.0:
                print("     ✗ %s：重转后 SNR %s dB，仍不正常！" %
                      (m, ("%.1f" % s1) if s1 is not None else "?"), flush=True)
                bad += 1
                continue
            done += 1
            print("     ✓ %-46s %d B → %d B  SNR %.1f dB  %s"
                  % (m[:46], old, os.path.getsize(mp), s1,
                     "带封面" if flac_out.has_picture(mp) else "无封面"),
                  flush=True)

    print("\n%s" % ("=" * 58), flush=True)
    print("%s：%d 个 MP3，%s %d / 失败 %d / 无基准跳过 %d"
          % ("重转完成" if args.yes else "干跑结果", tot,
             "已重转" if args.yes else "待重转", done, bad, skip), flush=True)
    if not args.yes and done:
        print("\n确认无误后加 --yes 真写。", flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
