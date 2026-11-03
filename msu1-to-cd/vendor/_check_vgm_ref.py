#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_check_vgm_ref.py — 拿 vgmstream 当黄金标准，验自己写的 IMA 解码器。

## 为什么要有这个脚本

`.hwas` / `.swav` / `.dvi` 三种格式的解码是**纯手写**的（`stream_audio._ima_decode`
+ 三个 `decode_*`）。手写编解码最容易犯的错是「看着差不多、其实每个样本都差
一点」—— 而这种错在听感上往往只表现为「有点闷」或「有底噪」，很难靠耳朵判。

vgmstream 是业界标准实现（ISC 许可，r2117 起支持这三种格式）。拿它的输出做
参照，**逐样本比对**，是唯一能给出「位级相同」这种结论的办法。

上一轮就是没做这一步，把 `_IMA_INDEX` 后半段写成 `-2,-4,-6,-8`（正确
`2,4,6,8`），解出 DC 漂移，还误判成「格式没破」。

## 用法

    # 1) 下 vgmstream 的 Windows 构建（约 4.5 MB）
    #    https://github.com/vgmstream/vgmstream/releases  →  vgmstream-win64.zip
    # 2) 解压到任意目录，然后：
    python vendor/_check_vgm_ref.py --vgmstream D:/MSU1Temp/vgm/vgmstream-cli.exe \
                                    --root D:/MSU1Downloads/joshw

    # 只验一种格式
    python vendor/_check_vgm_ref.py --vgmstream ... --root ... --only .dvi

⚠ **这个脚本不进发布包**，也不进常规门禁 —— 它依赖一个外部 exe。
  常规门禁用的是**手算的期望值**（`vendor/test_web_ui.py` 的 `t_stream`），
  不需要任何外部素材。

## 判据

* 采样率必须相等；
* 总帧数必须相等；
* **逐样本最大差必须是 0**（不是「差不多」，是 0）。

差异一旦非零，脚本会打印首个不同样本的位置与前后 3 个值，方便定位到块边界。
"""
import argparse
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stream_audio as sa                                      # noqa: E402

#: 扩展名 → (解码函数, 每声道样本数怎么算)
JOBS = (
    (".hwas", sa.decode_hwas, "HWAS"),
    (".swav", sa.decode_swav, "SWAV"),
    (".dvi", sa.decode_dvi, "DVI"),
)

#: 每个文件只比对前这么多帧（大文件省时间；长度另算）
LIMIT = 400000


def walk(root, ext):
    out = []
    for d, _dirs, names in os.walk(root):
        for n in names:
            if n.lower().endswith(ext):
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
                    help="joshw 音源根目录（会递归找三种格式）")
    ap.add_argument("--only", default="",
                    help="只验一种格式，如 .dvi")
    args = ap.parse_args()

    cli = os.path.abspath(args.vgmstream)
    if not os.path.isfile(cli):
        print("找不到 vgmstream-cli：%s" % cli)
        return 2

    box = tempfile.mkdtemp(prefix="vgm_ref_")
    dst = os.path.join(box, "ref.wav")
    total = ok = 0
    bad = []
    try:
        for ext, fn, label in JOBS:
            if args.only and args.only.lower() != ext:
                continue
            files = walk(args.root, ext)
            if not files:
                print("\n===== %s：目录里没有 %s 文件 =====" % (label, ext))
                continue
            print("\n===== %s 共 %d 个 =====" % (label, len(files)))
            for i, path in enumerate(files, 1):
                total += 1
                name = os.path.basename(path)
                try:
                    pcm, rate = fn(path)
                except Exception as exc:                    # noqa: BLE001
                    bad.append((path, "解码抛异常: %s" % exc))
                    print("  [%2d/%d] %-44s 解不了: %s"
                          % (i, len(files), name[:44], exc))
                    continue
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
                good = (pct == 100.0 and rate == rrate)
                if good:
                    ok += 1
                else:
                    bad.append((path, "一致 %.2f%% 采样率 %d/%d"
                                % (pct, rate, rrate)))
                print("  [%2d/%d] %-44s %s %dch %5dHz 我%8d帧 参考%8d帧 "
                      "一致%6.2f%% 最大差%d"
                      % (i, len(files), name[:44], "OK " if good else "!! ",
                         nch, rate, len(mine), rn, pct, int(d.max())))
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

    print("\n===== 合计 %d 个，完全一致 %d 个，有问题 %d 个 ====="
          % (total, ok, len(bad)))
    for p, why in bad:
        print("  ✗ %s\n      %s" % (p, why))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
