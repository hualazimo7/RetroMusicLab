#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_check_mp3_quality.py — 给已有的 MP3 产物**体检**（v5.9.18）。

**只读，不改任何文件。**

## 为什么需要它

v5.9.15~17 转出来的 MP3 **内容是坏的**（libsndfile 不认 int16 输入，整数值
被当 float 样本解释 → 削顶成方波 → 满耳持续杂音），但结构指标全对：

* 帧数对（时长没变）
* 采样率、声道对
* 文件大小甚至更像「320 kbps」

所以「能播放、时长对、体积对」**完全不能**证明它没坏。唯一可靠的判据是
**解码回来和源逐样本比**（SNR / 相关系数）。

## 怎么比

优先拿同名的 ``FLAC/<游戏>/xx.flac`` 当基准（无损，等价于原始 PCM）；
没有 FLAC 就跳过（不拿 MP3 比 MP3 —— 两次有损无法定位是谁坏）。

## 判据

| 指标 | 正常 320kbps | 坏文件实测 | 阈值 |
|---|---|---|---|
| SNR | 35~40 dB | **-2 ~ -4 dB** | ≥ 20 dB |
| 相关系数 | 0.9999 | 0.38 | ≥ 0.99 |

用法::

    py -3.14 vendor/_check_mp3_quality.py --root=D:\\MSU1Downloads\\joshw
    py -3.14 vendor/_check_mp3_quality.py --root=... --limit=50
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
    if n < 4410:                       # 不足 0.1 秒，没统计意义
        return None, None
    r = ref[:n].astype(np.float64)
    g = got[:n].astype(np.float64)
    e = g - r
    ps, pe = float(np.dot(r, r)), float(np.dot(e, e))
    if ps <= 1e-9:
        return None, None
    snr = 10.0 * math.log10(ps / pe) if pe > 1e-12 else 99.0
    corr = float(np.dot(r, g) / (math.sqrt(ps * float(np.dot(g, g))) + 1e-18))
    return snr, corr


def _pairs(root):
    """产出 ``(mp3 路径, 同名 flac 路径或 "")``。

    目录约定是 ``<机种>/MP3/<游戏>/xx.mp3``，所以要认「父目录叫 MP3」，
    不是「自己叫 MP3」—— 后者只会匹配到 MP3 这一层（里面只有子目录）。
    """
    for dp, dns, fns in os.walk(root):
        if os.path.basename(os.path.dirname(dp)).upper() != "MP3":
            continue
        game = os.path.basename(dp)
        plat = os.path.dirname(os.path.dirname(dp))
        flac_dir = os.path.join(plat, "FLAC", game)
        for f in sorted(fns):
            if not f.lower().endswith(".mp3"):
                continue
            mp3 = os.path.join(dp, f)
            cand = os.path.join(flac_dir, os.path.splitext(f)[0] + ".flac")
            yield mp3, (cand if os.path.isfile(cand) else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"D:/MSU1Downloads/joshw")
    ap.add_argument("--limit", type=int, default=0, help="最多查几个（调试）")
    ap.add_argument("--all", action="store_true",
                    help="连没有 FLAC 基准的也列出来（只报，不算分）")
    args = ap.parse_args()

    import numpy as np
    import soundfile as sf

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print("库根不存在：%s" % root, flush=True)
        return 2

    print("体检：%s\n" % root, flush=True)
    bad, good, skip = [], [], []
    n = 0
    for mp3, flac in _pairs(root):
        n += 1
        if args.limit and n > args.limit:
            break
        if not flac:
            skip.append(mp3)
            if args.all:
                print("  -  无 FLAC 基准  %s" % os.path.relpath(mp3, root),
                      flush=True)
            continue
        try:
            a, _sr = sf.read(flac, dtype="int16", always_2d=True)
            b, _sr2 = sf.read(mp3, dtype="int16", always_2d=True)
        except Exception as e:                   # noqa: BLE001
            print("  !  读不了 %s: %s" % (os.path.basename(mp3), e), flush=True)
            continue
        s, c = _snr(a.mean(1) if a.shape[1] > 1 else a[:, 0],
                    b.mean(1) if b.shape[1] > 1 else b[:, 0])
        if s is None:
            skip.append(mp3)
            continue
        rel = os.path.relpath(mp3, root)
        if s >= 20.0 and c >= 0.99:
            good.append((rel, s, c))
        else:
            bad.append((rel, s, c))
            print("  ✗  %-58s SNR %6.1f dB  相关 %.3f" % (rel[:58], s, c),
                  flush=True)

    print("\n%s" % ("=" * 60), flush=True)
    print("MP3 共 %d 个：正常 %d / **坏的 %d** / 无基准跳过 %d"
          % (len(good) + len(bad) + len(skip), len(good), len(bad), len(skip)),
          flush=True)
    if bad:
        print("\n坏的文件（内容已毁，**只能重转**，补封面也救不回来）：",
              flush=True)
        for rel, s, c in bad[:20]:
            print("   %s" % rel, flush=True)
        if len(bad) > 20:
            print("   …… 还有 %d 个" % (len(bad) - 20), flush=True)
        print("\n修法：升级到 v5.9.18 后，在下载页点一次「转 MP3」。"
              "\n      v5.9.18 的记账带了生产版本，会自动认出这批坏文件并重转，"
              "\n      不用手删。", flush=True)
    elif good:
        print("\n全部正常。", flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
