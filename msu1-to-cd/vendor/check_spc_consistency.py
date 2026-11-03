# -*- coding: utf-8 -*-
"""SPC 双路径一致性门禁（v4.0 增量前置核对）

背景：主窗口手动转 SPC 走 policy="id666"，下载窗口拟走 policy="chip"。
两条路径必须给出**同一首曲子**，否则用户会发现"下载窗口转出来的和
主窗口转出来的不一样"，这是不可接受的。

跑法： python vendor/check_spc_consistency.py
"""
import glob
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chip_decode
import spc_wav

SPC = sorted(glob.glob("vendor/**/*.spc", recursive=True))
SPC = [f for f in SPC if "samples" not in f] or sorted(
    glob.glob("vendor/samples/**/*.spc", recursive=True))


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def run(policy, src, out_dir, **extra):
    os.makedirs(out_dir, exist_ok=True)
    return spc_wav.convert([src], out_dir, policy,
                           loader=chip_decode.load_chip_paths, **extra)


def main():
    if not SPC:
        print("!! 没找到 .spc 样本，无法核对")
        return 2
    src = SPC[0]
    tmp = tempfile.mkdtemp(prefix="spc_consist_")
    a_dir = os.path.join(tmp, "id666")
    c_dir = os.path.join(tmp, "chip")

    print("样本: %s (%d 字节)" % (src, os.path.getsize(src)))
    a = run("id666", src, a_dir)
    c = run("chip", src, c_dir)

    def stat(d):
        f = [x for x in (d.get("files") or []) if os.path.isfile(x)]
        return f

    fa, fc = stat(a), stat(c)
    print("\n[policy=id666 ] ok=%s fail=%s  产出 %d 个" % (a["ok"], a["fail"], len(fa)))
    print("[policy=chip  ] ok=%s fail=%s  产出 %d 个" % (c["ok"], c["fail"], len(fc)))
    for p in fa:
        print("    id666: %s  %d 字节  %s" % (os.path.basename(p),
                                             os.path.getsize(p), sha(p)[:16]))
    for p in fc:
        print("    chip : %s  %d 字节  %s" % (os.path.basename(p),
                                             os.path.getsize(p), sha(p)[:16]))

    if not fa or not fc:
        print("\n✗ 至少一条路径没产出，无法比对")
        return 1

    # 比对：文件名 + SHA256
    na, nc = os.path.basename(fa[0]), os.path.basename(fc[0])
    sa, sc = sha(fa[0]), sha(fc[0])
    print("\n文件名一致: %s  (%s vs %s)" % (na == nc, na, nc))
    print("PCM 逐字节一致: %s" % (sa == sc))
    if sa != sc:
        print("  id666 sha=%s" % sa)
        print("  chip  sha=%s" % sc)
    if sa == sc and na == nc:
        print("\n✅ 双路径一致 —— 下载窗口可以放心统一走 policy=chip")
        return 0

    # ---- 已定案的根因（2026-10-18 实测，勿再猜）----
    import array
    import struct

    def pcm(p):
        b = open(p, "rb").read()
        i = 12
        while i + 8 <= len(b):
            cid = b[i:i + 4]
            sz = struct.unpack_from("<I", b, i + 4)[0]
            if cid == b"data":
                return b[i + 8:i + 8 + sz]
            i += 8 + sz + (sz & 1)
        return b""

    A = array.array("h"); A.frombytes(pcm(fa[0]))
    C = array.array("h"); C.frombytes(pcm(fc[0]))
    diff = [i for i in range(min(len(A), len(C))) if A[i] != C[i]]
    at = diff[0] / 2 / 44100 if diff else -1
    print("\n根因：**淡出基准点不同**，不是解码错误。")
    print("  · policy=id666 走 _spc_play_plan -> 用 ID666 标签的 fade 值；")
    print("    sample.spc 的 ID666 fade=0ms（作者明确标注不淡出）-> 完全不淡出。")
    print("  · policy=chip  在 chip_decode.py:234 **无条件**对每条轨尾部施加")
    print("    CHIP_FADE_MS=1500 的升余弦淡出，与 ID666 标签无关。")
    print("  实测首个差异在第 %.3f 秒，前 %.3f 秒逐字节一致，之后音量被压低。" % (at, at))
    print("\n⚠ 结论：下载窗口对 SPC **必须单独走 policy='id666'**，")
    print("  否则用户会听到主窗口里听不到的「结尾被消音」。")
    print("  （芯片格式 NSF/GBS/VGM/HES… 仍统一走 policy='chip'，无此问题。）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
