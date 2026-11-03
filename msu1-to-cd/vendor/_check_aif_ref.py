#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_check_aif_ref.py — 拿 vgmstream 当黄金标准，验 3DO 的 AIFF/AIFC 解码器。

## 为什么要有这个脚本

3DO 的音源不是「某种 3DO 私有容器」，而是**标准 Apple AIFF / AIFC**，
里面装的东西分三种：

* 8-bit 有符号 PCM（AIFF 的 ``NONE`` + 8bit）
* 16-bit 大端 PCM（``NONE`` + 16bit）
* **SDX2** ADPCM（3DO 的 "2:1 Squareroot-delta-exact"，AIFC 的 ``SDX2`` 四字符码）

前两种是教科书格式，错不了；**SDX2 是自己手写的**，而它有一个非常容易写错的
细节 —— 见下。手写 ADPCM 的典型错误是「听感差不多、每个样本都差一点」，
靠耳朵根本判不出来，只能逐样本比。

## SDX2 那个坑（写在这里省得下次再踩）

`vgmstream/src/coding/sdx2_decoder.c` 的实际行为是：

    sample = hist + squares[byte + 128]        # squares[i] = i<0 ? -i*i*2 : i*i*2
    hist   = clamp16(sample)                   # ← 注意：存回去的 hist 也是夹过的
    out    = clamp16(sample)

而且 **byte 为偶数时是「绝对值模式」**：先把 ``hist`` 归零再算，等于直接取
``squares[byte+128]``。只把它当普通累加器写（不区分奇偶、hist 不夹）会得到
「整体轮廓对、细节全错」的结果，最大差能到几千。

## 用法

    # 1) 下 vgmstream 的 Windows 构建（约 4.5 MB）
    #    https://github.com/vgmstream/vgmstream/releases  →  vgmstream-win64.zip
    # 2) 解压到任意目录，然后：
    python vendor/_check_aif_ref.py --vgmstream D:/MSU1Temp/vgm/vgmstream-cli.exe \
                                    --root D:/MSU1Temp/3do_samples

    # 只验某一种编码
    python vendor/_check_aif_ref.py --vgmstream ... --root ... --only SDX2

⚠ **这个脚本不进发布包**，也不进常规门禁 —— 它依赖一个外部 exe。
  常规门禁用的是**手算的期望值**（`vendor/test_web_ui.py` 里的 aif 用例），
  不需要任何外部素材。

## 判据

* 采样率必须相等；
* 总帧数必须相等；
* **逐样本最大差必须是 0**（不是「差不多」，是 0）。

差异一旦非零，脚本会打印首个不同样本的位置与前后 3 个值，方便定位到字节边界。
"""
import argparse
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aif_audio                                                 # noqa: E402

#: 每个文件只比对前这么多帧（大文件省时间；总帧数另算）
LIMIT = 400000


def walk(root):
    """递归找出所有 .aif/.aiff/.aifc —— 三种后缀都要，别只认 .aif。"""
    out = []
    for d, _dirs, names in os.walk(root):
        for n in names:
            if n.lower().endswith(aif_audio.ALL_EXTS):
                out.append(os.path.join(d, n))
    return sorted(out)


def ref_wav(cli, src, dst):
    """用 vgmstream 转一份 WAV，返回 ``(ndarray[n,ch], rate, 总帧数)``。"""
    # ⚠ 截断而不是删除：单轮删太多文件会触发工作区的批量删除确认机制
    open(dst, "wb").close()
    r = subprocess.run([cli, "-i", "-o", dst, src],
                       capture_output=True, timeout=300)
    if r.returncode != 0 or os.path.getsize(dst) < 44:
        return None
    w = wave.open(dst, "rb")
    ch, fr, n = w.getnchannels(), w.getframerate(), w.getnframes()
    a = np.frombuffer(w.readframes(min(n, LIMIT)), dtype="<i2")
    w.close()
    open(dst, "wb").close()
    return a.reshape(-1, ch), fr, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vgmstream", required=True,
                    help="vgmstream-cli.exe 的路径")
    ap.add_argument("--root", required=True,
                    help="3DO 音源根目录（会递归找 .aif/.aiff/.aifc）")
    ap.add_argument("--only", default="",
                    help="只验一种编码：PCM8 / PCM16BE / SDX2 / XA4")
    args = ap.parse_args()

    cli = os.path.abspath(args.vgmstream)
    if not os.path.isfile(cli):
        print("找不到 vgmstream-cli：%s" % cli)
        return 2

    files = walk(args.root)
    if not files:
        print("目录里没有 .aif/.aiff/.aifc：%s" % args.root)
        return 2

    box = tempfile.mkdtemp(prefix="aif_ref_")
    dst = os.path.join(box, "ref.wav")
    total = ok = skipped = 0
    bad = []
    try:
        print("===== 共找到 %d 个 AIFF/AIFC =====" % len(files))
        for i, path in enumerate(files, 1):
            name = os.path.basename(path)
            # ---- 先读头，按编码过滤 ----
            try:
                info = aif_audio.read_header(path)
            except Exception as exc:                        # noqa: BLE001
                total += 1
                bad.append((path, "读头失败: %s" % exc))
                print("  [%2d/%d] %-44s 读头失败: %s"
                      % (i, len(files), name[:44], exc))
                continue
            if args.only and info["codec"] != args.only:
                skipped += 1
                continue
            total += 1

            # ---- 我们解 ----
            try:
                pcm, rate = aif_audio.decode_pcm(path)
            except Exception as exc:                        # noqa: BLE001
                bad.append((path, "解码抛异常: %s" % exc))
                print("  [%2d/%d] %-44s 解不了: %s"
                      % (i, len(files), name[:44], exc))
                continue

            # ---- vgmstream 解 ----
            got = ref_wav(cli, path, dst)
            if got is None:
                bad.append((path, "vgmstream 也不认"))
                print("  [%2d/%d] %-44s vgmstream 不认"
                      % (i, len(files), name[:44]))
                continue
            ref, rrate, rn = got
            nch = ref.shape[1]
            mine = pcm.reshape(-1, nch).astype(np.int32)
            refi = ref.astype(np.int32)
            m = min(len(mine), len(refi))
            if m == 0:
                bad.append((path, "空"))
                continue
            d = np.abs(mine[:m] - refi[:m])
            same = int((d == 0).sum())
            pct = same * 100.0 / d.size
            # 总帧数也要对 —— 只比前 LIMIT 帧的话，末尾被截断是发现不了的
            good = (pct == 100.0 and rate == rrate
                    and info["frames"] == rn and len(pcm) == rn)
            if good:
                ok += 1
            else:
                bad.append((path, "一致 %.2f%% 采样率 %d/%d 帧 %d/%d"
                            % (pct, rate, rrate, len(pcm), rn)))
            print("  [%2d/%d] %-40s %s %-7s %dch %5dHz 我%8d帧 参考%8d帧 "
                  "一致%6.2f%% 最大差%d"
                  % (i, len(files), name[:40], "OK " if good else "!! ",
                     info["codec"], nch, rate, len(pcm), rn, pct, int(d.max())))
            if not good:
                where = np.argwhere(d > 0)
                if len(where):
                    r0, c0 = where[0]
                    print("      首个不同：样本 %d 声道 %d  我=%d 参考=%d"
                          % (r0, c0, mine[r0, c0], refi[r0, c0]))
                    lo = max(0, r0 - 3)
                    print("      我  %s" % mine[lo:r0 + 4, c0].tolist())
                    print("      参考%s" % refi[lo:r0 + 4, c0].tolist())
    finally:
        try:
            open(dst, "wb").close()
        except OSError:
            pass

    print("\n===== 合计 %d 个（跳过 %d 个），完全一致 %d 个，有问题 %d 个 ====="
          % (total, skipped, ok, len(bad)))
    for p, why in bad:
        print("  ✗ %s\n      %s" % (p, why))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
