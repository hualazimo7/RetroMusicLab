#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_probe_mp3_frames.py — 手工解析 MP3 帧头，查「帧结构是否自洽」。

为什么必须手工解析：

    ``soundfile`` 解码 MP3 时用的是 libsndfile 自己的解码器。如果**编码器**
    写出的帧头和真实帧长不一致（例如 header 声称 320kbps=1044 字节，实际
    只写了 800 字节），libsndfile 可能靠自己的容错逻辑硬解出来，SNR 看着
    很好；而**别的播放器**严格按帧长跳转就会失步 —— 失步后的表现正是
    「持续杂音 / 兹拉兹拉」。

    所以这里绕开所有解码器，纯按 ISO 11172-3 手工走帧：
    读帧头 → 算该帧应有长度 → 从「当前帧起点 + 应有长度」处找下一个同步字
    → 对得上就是自洽，对不上就是编码器在骗人。

用法::

    py -3.14 vendor/_probe_mp3_frames.py <file.mp3> [more.mp3 ...]
    py -3.14 vendor/_probe_mp3_frames.py --dir "D:\\...\\MP3"
"""
import argparse
import os
import sys

BITRATES_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128,
                  160, 192, 224, 256, 320, 0]        # kbps
BITRATES_V2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80,
                  96, 112, 128, 144, 160, 0]
SRATES_V1 = [44100, 48000, 32000, 0]
SRATES_V2 = [22050, 24000, 16000, 0]
SRATES_V25 = [11025, 12000, 8000, 0]


def id3_len(b):
    if len(b) < 10 or b[:3] != b"ID3":
        return 0
    sz = ((b[6] & 0x7F) << 21) | ((b[7] & 0x7F) << 14) | \
         ((b[8] & 0x7F) << 7) | (b[9] & 0x7F)
    end = 10 + sz
    if b[5] & 0x10:            # footer present
        end += 10
    return end


def parse_frame(b, p):
    """解析 p 处的帧头，返回 dict 或 None。"""
    if p + 4 > len(b):
        return None
    h = b[p:p + 4]
    if h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
        return None
    ver_bits = (h[1] >> 3) & 0x03       # 0=2.5 1=reserved 2=2 3=1
    layer_bits = (h[1] >> 1) & 0x03     # 1=III
    if ver_bits == 1 or layer_bits == 0:
        return None
    br_idx = (h[2] >> 4) & 0x0F
    sr_idx = (h[2] >> 2) & 0x03
    if br_idx in (0, 15) or sr_idx == 3:
        return None
    pad = (h[2] >> 1) & 0x01
    ch_mode = (h[3] >> 6) & 0x03        # 3 = mono
    if ver_bits == 3:                   # MPEG-1
        br = BITRATES_V1_L3[br_idx] * 1000
        sr = SRATES_V1[sr_idx]
        spf = 1152
    elif ver_bits == 2:                 # MPEG-2
        br = BITRATES_V2_L3[br_idx] * 1000
        sr = SRATES_V2[sr_idx]
        spf = 576
    else:                               # MPEG-2.5
        br = BITRATES_V2_L3[br_idx] * 1000
        sr = SRATES_V25[sr_idx]
        spf = 576
    if br == 0 or sr == 0:
        return None
    # Layer III 帧长
    if ver_bits == 3:
        flen = (144 * br // sr) + pad
    else:
        flen = (72 * br // sr) + pad
    return {"off": p, "ver": {3: "1", 2: "2", 0: "2.5"}[ver_bits],
            "kbps": br // 1000, "sr": sr, "ch": 1 if ch_mode == 3 else 2,
            "spf": spf, "flen": flen, "pad": pad,
            "crc": 0 if (h[1] & 0x01) else 1,
            "mode": ["stereo", "joint", "dual", "mono"][ch_mode]}


def walk(path, limit=4000):
    with open(path, "rb") as f:
        b = f.read()
    start = id3_len(b)
    p = start
    frames = []
    bad = []
    while p + 4 <= len(b) and len(frames) < limit:
        fr = parse_frame(b, p)
        if fr is None:
            # 往后找下一个同步字
            q = p + 1
            while q + 4 <= len(b) and parse_frame(b, q) is None:
                q += 1
            if q + 4 > len(b):
                break
            bad.append((p, q - p))
            p = q
            continue
        nxt = p + fr["flen"]
        # 该帧后面必须紧跟下一个合法帧头（或正好到文件尾）
        ok = (nxt == len(b)) or (nxt + 4 <= len(b) and parse_frame(b, nxt) is not None)
        if not ok:
            bad.append((p, fr["flen"]))
            q = p + 1
            while q + 4 <= len(b) and parse_frame(b, q) is None:
                q += 1
            p = q
            continue
        frames.append(fr)
        p = nxt
    return {"id3": start, "size": len(b), "frames": frames,
            "bad": bad, "end": p, "tail": len(b) - p}


def report(path):
    r = walk(path)
    fr = r["frames"]
    name = os.path.basename(path)
    if not fr:
        print(f"  {name}: 没解析出任何帧（ID3={r['id3']}B 尾部剩余={r['tail']}B）")
        return False
    kbps = {f["kbps"] for f in fr}
    srs = {f["sr"] for f in fr}
    chs = {f["ch"] for f in fr}
    modes = {f["mode"] for f in fr}
    vers = {f["ver"] for f in fr}
    dur = sum(f["spf"] for f in fr) / list(srs)[0] if len(srs) == 1 else 0
    ok = True
    print(f"  {name}")
    print(f"    ID3 {r['id3']:>7d}B  音频 {r['size']-r['id3']:>9d}B  "
          f"帧 {len(fr):>6d}  时长 {dur:6.2f}s  "
          f"MPEG{','.join(sorted(vers))} {','.join(str(x) for x in sorted(srs))}Hz "
          f"{','.join(str(x) for x in sorted(chs))}ch "
          f"{','.join(sorted(modes))} {','.join(str(x) for x in sorted(kbps))}kbps")
    if len(kbps) > 1:
        print(f"    ⚠ 码率不恒定: {sorted(kbps)}")
        ok = False
    if len(srs) > 1 or len(vers) > 1:
        print(f"    ⚠ 采样率/版本混用")
        ok = False
    if r["bad"]:
        print(f"    ✗ 有 {len(r['bad'])} 处帧长对不上（前 3 处: "
              f"{[(a, c) for a, c in r['bad'][:3]]}）")
        ok = False
    if r["tail"] > 0:
        print(f"    ⚠ 尾部剩余 {r['tail']}B 未成帧")
    if ok:
        print(f"    ✓ 帧结构自洽（每帧声明长度 = 到下一个同步字的距离）")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--dir", default="")
    ap.add_argument("--limit", type=int, default=0, help="只查前 N 个")
    args = ap.parse_args()

    files = list(args.files)
    if args.dir:
        for dp, _dn, fn in os.walk(args.dir):
            for f in fn:
                if f.lower().endswith(".mp3"):
                    files.append(os.path.join(dp, f))
    if args.limit:
        files = files[:args.limit]
    if not files:
        print("没有输入文件")
        return 2

    good = 0
    for f in files:
        if report(f):
            good += 1
        print()
    print(f"合计 {good}/{len(files)} 个帧结构自洽")
    return 0 if good == len(files) else 1


if __name__ == "__main__":
    sys.exit(main())
