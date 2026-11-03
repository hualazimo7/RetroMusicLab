# -*- coding: utf-8 -*-
"""临时实验：NDS STRM 解码（跑完即删）。"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import stream_audio as sa

F = (r"D:\MSU1Downloads\joshw\Nintendo DS\Adventures of Pinocchio "
     r"[Peter Pan's Playground] (2008-09-18)(-)(Phoenix)[NDS]\STRM_MAINMENU.strm")

d = open(F, "rb").read()
print("文件大小:", len(d))
u16 = lambda b, o: struct.unpack("<H", b[o:o + 2])[0]
u32 = lambda b, o: struct.unpack("<I", b[o:o + 4])[0]
s16 = lambda b, o: struct.unpack("<h", b[o:o + 2])[0]

print("magic:", d[0:4], " bom:", d[4:6].hex())
print("version: 0x%04X" % u16(d, 6))
print("file_size:", u32(d, 8), " header_size:", u16(d, 0x0C),
      " blocks:", u16(d, 0x0E))
print("HEAD:", d[0x10:0x14], " size:", u32(d, 0x14))
print("codec:", u16(d, 0x18), " loop:", u16(d, 0x1A))
print("rate(u16):", u16(d, 0x1C), " reserved:", u16(d, 0x1E))
print("time:", u32(d, 0x20), " loop_start:", u32(d, 0x24),
      " loop_len:", u32(d, 0x28))
print("0x2C:", u32(d, 0x2C), " 0x30:", u32(d, 0x30),
      " 0x34:", u32(d, 0x34), " 0x38:", u32(d, 0x38),
      " 0x3C:", u32(d, 0x3C))

i = d.find(b"DATA", 0x10)
print("\nDATA at 0x%X  size=%d  头部: %s"
      % (i, u32(d, i + 4), d[i:i + 12].hex(" ")))
size = u32(d, i + 4)
data = d[i + 8:i + 8 + size]
print("DATA 实际取到:", len(data))

rate = u16(d, 0x1C)

# 方式 A：不分块（只读开头 4 字节头）
hist, step = s16(data, 0), u16(data, 2)
print("\nA) 不分块: hist=%d step=%d" % (hist, step))
a, _ = sa._ima_decode(data[4:], rate, hist=hist, step_index=step)
print("   samples=%d  %.1fs  peak=%d" % (len(a), len(a) / rate, int(np.abs(a).max())))

# 方式 B：每 0x200 一块，每块 4 字节头
BLK = 0x200
out = []
for off in range(0, len(data), BLK):
    blk = data[off:off + BLK]
    if len(blk) < 5:
        break
    h, s = s16(blk, 0), u16(blk, 2)
    pcm, _ = sa._ima_decode(blk[4:], rate, hist=h, step_index=s)
    out.append(pcm)
b = np.concatenate(out) if out else np.zeros(0, np.int16)
print("B) 分块 %d 块: samples=%d  %.1fs  peak=%d"
      % (len(out), len(b), len(b) / rate, int(np.abs(b).max()) if len(b) else 0))

# 每块的 hist 是否与上一块末尾连续？（判断分块是否正确）
if len(out) >= 2:
    tail = int(out[0][-1])
    head2 = s16(data, BLK)
    print("\n块0 末样本=%d  块1 头 hist=%d  差=%d"
          % (tail, head2, abs(tail - head2)))
