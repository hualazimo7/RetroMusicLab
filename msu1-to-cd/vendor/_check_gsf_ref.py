"""用 kode54 `audacious-xsf/plugin.cc` 的 GSF 原始实现当黄金参照，
逐样本比对 `gsf2wav.exe` 的输出。

    C:/Python314/python.exe vendor/_check_gsf_ref.py --root "D:/MSU1Downloads/joshw/Nintendo GBA"

判据（三项全过才算通过）：

  1. **帧数完全相同**
  2. **逐样本最大绝对差 == 0**
  3. 样本数 > 0（防止「两边都是空」也算过）

为什么要这么严：两个工具共用同一个解码核（mGBA 最小核），所以**任何差异
都只能是外层代码的 bug** —— `.gsflib` 段拼接、ROM 尺寸的 2 的幂取整（+10 字节）、
`runFrame` 的推进粒度、`MIN_BUFFER_SIZE=576`、`length`/`fade` 解析、帧数换算、
淡出斜坡、WAV 写出。

⚠ GSF 的原生采样率是 32768（`get_srate(0x22)`），而 `gsf2wav` 出厂用 44100
（主流水线要求）。所以这里**两个率各比一遍**：

    --rate 32768   证明「接线抄对了」（和上游逐字节同构）
    --rate 44100   证明「出厂设置也对」（和 gsf2wav 默认值对齐）

`refdump.exe` 不在发布包里，用 `tools/gsf2wav/build.py --ref` 单独编。
"""
import argparse
import atexit
import os
import shutil
import struct
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
TOOL = os.path.join(PKG, "gsf2wav.exe")
REF = os.path.join(PKG, "tools", "gsf2wav", "ref", "refdump.exe")

#: 上游 `get_srate(0x22)` 的原生值。
NATIVE_RATE = 32768


def find_files(root):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith((".minigsf", ".gsf")):
                out.append(os.path.join(dirpath, f))
    out.sort()
    return out


def run(cmd):
    """跑一个子进程，返回 (returncode, stderr)。"""
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.returncode, r.stderr.decode("utf-8", "replace")


def read_wav_pcm(path):
    """读 WAV，返回 (pcm_bytes, nch, rate, bits)。只认标准 44 字节头。"""
    with open(path, "rb") as f:
        head = f.read(44)
        if len(head) < 44 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("不是有效的 WAV")
        nch = struct.unpack_from("<H", head, 22)[0]
        rate = struct.unpack_from("<I", head, 24)[0]
        bits = struct.unpack_from("<H", head, 34)[0]
        data = f.read()
    return data, nch, rate, bits


def cmp_pcm(a, b, limit=0):
    """逐样本比两段 s16le。返回 (n_samples, max_diff, first_diff_index, 上下文)。"""
    n = min(len(a), len(b)) // 2
    if limit and n > limit:
        n = limit
    if n == 0:
        return 0, 0, -1, ""
    sa = np.frombuffer(a[:n * 2], dtype="<i2").astype(np.int32)
    sb = np.frombuffer(b[:n * 2], dtype="<i2").astype(np.int32)
    diff = np.abs(sa - sb)
    max_diff = int(diff.max())
    if max_diff == 0:
        return n, 0, -1, ""
    first = int(np.argmax(diff > 0))
    lo = max(0, first - 3)
    hi = min(n, first + 4)
    ctx = "我 %s  参考 %s" % (sa[lo:hi].tolist(), sb[lo:hi].tolist())
    return n, max_diff, first, ctx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="minigsf 所在目录（递归）")
    ap.add_argument("--rate", type=int, default=NATIVE_RATE,
                    help="比对采样率：%d = 上游原生值，44100 = 出厂默认"
                         % NATIVE_RATE)
    ap.add_argument("--limit-files", type=int, default=0,
                    help="只测前 N 个文件（0 = 全测）")
    ap.add_argument("--limit-samples", type=int, default=0,
                    help="每个文件只比前 N 个样本（0 = 全比）")
    args = ap.parse_args()

    for p, name in ((TOOL, "gsf2wav.exe"), (REF, "refdump.exe")):
        if not os.path.isfile(p):
            print("找不到 %s：%s" % (name, p), file=sys.stderr)
            print("先编：C:/Python314/python.exe tools/gsf2wav/build.py"
                  + (" --ref" if name == "refdump.exe" else ""),
                  file=sys.stderr)
            return 2

    files = find_files(args.root)
    if args.limit_files:
        files = files[:args.limit_files]
    if not files:
        print("没找到 .minigsf / .gsf")
        return 2

    rate_s = str(args.rate)
    print("比对 %d 个文件（%s）· 采样率 %s\n" % (len(files), args.root, rate_s))

    tmp = tempfile.mkdtemp(prefix="gsf2ref_")

    # 跑完自动删 —— 这些参照比对会生成几百 MB PCM/WAV，
    # 不删就会一直攒在 %TEMP% 里（实测两天攒了 6.7 GB）。
    atexit.register(shutil.rmtree, tmp, True)
    pcm_ref = os.path.join(tmp, "ref.pcm")
    wav_my = os.path.join(tmp, "my.wav")

    n_ok = n_bad = n_skip = 0
    bad = []

    for idx, path in enumerate(files, 1):
        name = os.path.relpath(path, args.root)
        tag = "[%d/%d] %s" % (idx, len(files), name)

        for f in (pcm_ref, wav_my):
            if os.path.exists(f):
                os.remove(f)

        rc_ref, err_ref = run([REF, path, pcm_ref, "--rate", rate_s])
        if rc_ref != 0 or not os.path.isfile(pcm_ref):
            n_skip += 1
            print("%-64s SKIP  参考工具失败 (rc=%s) %s"
                  % (tag[:64], rc_ref, err_ref.strip().splitlines()[:1]))
            continue
        ref_size = os.path.getsize(pcm_ref)
        if ref_size == 0:
            n_skip += 1
            print("%-64s SKIP  参考输出 0 字节" % tag[:64])
            continue

        # ⚠ 两边都必须**带 fade**。参照的 play() 循环把淡出乘在原样本上，
        #   如果我只给 gsf2wav 传 --no-fade，凡是带 fade 标签的文件都会差
        #   一整条斜坡。
        rc_my, err_my = run([TOOL, path, wav_my, "--rate", rate_s])
        if rc_my != 0 or not os.path.isfile(wav_my):
            n_bad += 1
            bad.append((name, "我的工具失败 rc=%s %s"
                        % (rc_my, err_my.strip().splitlines()[:1])))
            print("%-64s FAIL  我的工具失败 rc=%s" % (tag[:64], rc_my))
            continue

        try:
            my_pcm, nch, rate, bits = read_wav_pcm(wav_my)
        except ValueError as e:
            n_bad += 1
            bad.append((name, "WAV 不合法: %s" % e))
            print("%-64s FAIL  %s" % (tag[:64], e))
            continue

        if len(my_pcm) != ref_size:
            n_bad += 1
            bad.append((name, "字节数不同 我 %d 参考 %d（差 %d 字节 = %.3f 秒）"
                        % (len(my_pcm), ref_size, len(my_pcm) - ref_size,
                           (len(my_pcm) - ref_size) / (float(args.rate) * 4))))
            print("%-64s FAIL  字节数 我 %d / 参考 %d"
                  % (tag[:64], len(my_pcm), ref_size))
            continue

        n, max_diff, first, ctx = cmp_pcm(my_pcm, open(pcm_ref, "rb").read(),
                                          args.limit_samples)
        if n == 0:
            n_skip += 1
            print("%-64s SKIP  0 样本" % tag[:64])
            continue
        if max_diff == 0:
            n_ok += 1
            print("%-64s OK    %dch %dHz %d样本 差0"
                  % (tag[:64], nch, rate, n))
        else:
            n_bad += 1
            bad.append((name, "最大差 %d @样本%d" % (max_diff, first)))
            print("%-64s FAIL  最大差 %d @样本%d  %s"
                  % (tag[:64], max_diff, first, ctx[:80]))

    print("\n" + "=" * 60)
    print("合计 %d 个：完全一致 %d，有问题 %d，跳过 %d"
          % (len(files), n_ok, n_bad, n_skip))
    if bad:
        print("\n有问题的：")
        for name, why in bad[:20]:
            print("  %s\n    %s" % (name, why))
    print("=" * 60)
    return 0 if n_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
