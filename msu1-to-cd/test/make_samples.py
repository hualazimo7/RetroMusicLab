#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成两份带 MSU1 文件头的合成 PCM, 用于验证 msu1_to_cd.py。"""
import math
import struct
import os

OUT = os.path.join(os.path.dirname(__file__))
SR = 44100
CH = 2


def make(path, freq, seconds, loop=False):
    n = int(SR * seconds)
    pcm = bytearray()
    for i in range(n):
        s = int(0.4 * 32767 * math.sin(2 * math.pi * freq * i / SR))
        pcm += struct.pack("<h", s) * CH  # 小端 int16, 立体声
    # MSU1 头: "MSU1" + loop point(uint32 LE, 采样点; 0=不循环)
    header = b"MSU1" + struct.pack("<I", 0 if not loop else 1000)
    with open(path, "wb") as f:
        f.write(header + pcm)
    print(f"wrote {path}: {len(pcm)} 字节 PCM, {seconds}s")


if __name__ == "__main__":
    make(os.path.join(OUT, "demo-1.pcm"), 440.0, 3.0)
    make(os.path.join(OUT, "demo-2.pcm"), 660.0, 4.0)
    make(os.path.join(OUT, "demo-0.pcm"), 220.0, 2.0)  # 轨 0 通常静音/菜单
