#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_s98_fixture.py — 造一个最小的 PC-98 ``.s98``，给两套门禁共用。

为什么单独一个文件：源码门禁（``test_web_ui.py`` / ``_e2e_v5912_platforms.py``）
与 exe 验收（``check_web_exe.py``）都要用同一份样本，各写一遍必然漂移。

**为什么不用真实素材**：真实素材要下 joshw 的 PC-88 包（~20 MB），
而这里要验的只是「``s982wav.exe`` 在这个包里能不能跑、能不能出声」。
合成一个 1.2 秒的单音文件，**音高是算出来的已知值**，比听感可靠得多。

## 文件长什么样

头部 0x20 字节（照 libvgm ``player/s98player.cpp`` 的 ``LoadFile()``）::

    0x00  3B  "S98"
    0x03  1B  版本**字符** '1'（不是二进制 1）
    0x04  4B  tickMult = 10      （LE）
    0x08  4B  tickDiv  = 1000    （LE）
    0x0C  4B  compression = 0
    0x10  4B  tagOfs   → 曲名区
    0x14  4B  dataOfs  → 数据流
    0x18  4B  loopOfs  = 0（不循环）

数据流每条命令：

* ``0xFF``            → 前进 1 tick（10 ms）
* ``0xFE <leb128>``   → 前进 2 + var tick
* ``0xFD``            → 结束
* ``<cmd> <reg> <val>``（**3 字节**）→ 写寄存器；``cmd = (dev<<1)|port``

## 为什么只留 op4 出声

YM2612 / OPNA 的**算法 0** 是「op1→op2→op3→op4」的串联，**只有 op4 是载波**。
四个算子的 TL（0x40/0x44/0x48/0x4C）必须把前三个设成 0x7F（衰减到底），
只留 0x4C = 0x00。否则听到的是静音 —— 而静音看起来像「解码器坏了」。

## 音高

    f = fnum × MULT × clock / (288 × 2^(20−block))

``block=4``、``fnum=1040``、``MULT=1`` → 440.10 Hz。

> ⚠ 分母是 **288** 不是 144。很多资料写
> ``f = fnum × clock / (144 × 2^(20−block))``，那是把 fnum 当成
> 「已经 ×2 过的 12 位值」。ymfm 的实现（``compute_phase_step``）是::
>
>     fnum = bitfield(block_freq, 0, 11) << 1;      // ← 先 ×2
>     phase_step = (fnum << block) >> 2;            // ← 再 ÷4
>     phase_step = (phase_step * cache.multiple) >> 1;   // multiple 是 x.1
>
> 合起来就是 ``fnum × 2^block × MULT / 2``，于是分母多一个 2。
> 照 144 算出来的 fnum 会**正好低一个八度**（实测 440 → 220），
> 而波形完全正常、只是音高错 —— 很容易被当成「听感差异」放过去。
"""

import os
import struct

#: S98 v0/v1 的默认时钟（libvgm 写死）。fnum 的换算要用它。
CLOCK = 7987200

#: 期望音高。``test`` 段会拿它跟实测频谱比。
EXPECT_HZ = 1040.0 * CLOCK / (288.0 * 65536.0)     # 440.1017...

#: 音符长度（tick 数）。tick = tickMult/tickDiv = 10 ms → 100 tick = 1.0 秒。
NOTE_TICKS = 100
#: 松键之后的余音 tick 数（20 tick = 0.2 秒）。
RELEASE_TICKS = 20

TICK_MULT = 10
TICK_DIV = 1000

TAG = b"Fixture Tone\x00"


def _reg_writes():
    """返回 [(reg, val), ...]，全部是**端口 0**（FM）的写入。"""
    w = []
    w.append((0x22, 0x00))          # LFO 关
    w.append((0x27, 0x00))          # 通道 3 普通模式（不是特殊/CSM）
    w.append((0x28, 0x00))          # 先全部松键
    w.append((0x2B, 0x00))          # DAC 关（OPNA 的 bit7）

    # DT/MULT：四个算子都 MULT=1、DT=0
    for off in (0x30, 0x34, 0x38, 0x3C):
        w.append((off, 0x01))

    # TL：只留 op4（算法 0 的载波），其余衰减到底
    for i, off in enumerate((0x40, 0x44, 0x48, 0x4C)):
        w.append((off, 0x00 if i == 3 else 0x7F))

    # AR=31（最快起音），DR=0（不衰减，保持住）
    for off in (0x50, 0x54, 0x58, 0x5C):
        w.append((off, 0x1F))
    for off in (0x60, 0x64, 0x68, 0x6C):
        w.append((off, 0x00))
    # SL=0 / RR=15（松键后快速收尾）
    for off in (0x80, 0x84, 0x88, 0x8C):
        w.append((off, 0x0F))
    # SSG-EG 全关
    for off in (0x90, 0x94, 0x98, 0x9C):
        w.append((off, 0x00))

    w.append((0xB0, 0x00))          # FB=0 / ALG=0（串联，载波是 op4）
    w.append((0xB4, 0xC0))          # 声像 L+R

    # 音高：block=4，fnum=520
    #
    # ⚠ **`0xA0-0xAF` 是「锁存对」，顺序和分工都不能猜。** ymfm 的实现
    #   （`ymfm_opn.cpp` 的 `opn_registers_base::write`）是：
    #
    #       0xA4（bit2=1，上半）→ 只**锁存**到 0xB8|ch，取低 6 位 = block(3)+fnum高(3)
    #       0xA0（bit2=0，下半）→ **应用**：0xA0 = data，0xA4 = 刚才锁存的值
    #
    #   也就是说 **0xA4 是 block/fnum 高位、0xA0 才是 fnum 低字节**，
    #   和「0xA0 是高位」的直觉相反，而且**必须先写 0xA4 再写 0xA0**
    #   （0xA0 那一下才把整对提交）。
    #
    #   写反的症状极具迷惑性：载波**照常振荡**，但只有高位生效 ——
    #   实测 440 Hz 变成约 1.8 Hz 的慢摇摆（周期半秒），波形看着像
    #   「一大段直流在漂」，很容易误判成隔直或重采样出了问题。
    fnum = 1040
    w.append((0xA4, ((4 & 0x07) << 3) | ((fnum >> 8) & 0x07)))   # block + fnum 高
    w.append((0xA0, fnum & 0xFF))                                 # fnum 低（提交）

    w.append((0x28, 0xF0))          # key on：ch0 的四个算子全开
    return w


def make_s98(path):
    """在 ``path`` 写一个 1.2 秒的 .s98，返回 ``path``。"""
    body = bytearray()
    for reg, val in _reg_writes():
        body += bytes((0x00, reg, val))         # dev 0 / port 0
    body += b"\xFF" * NOTE_TICKS
    body += bytes((0x00, 0x28, 0x00))           # key off
    body += b"\xFF" * RELEASE_TICKS
    body += b"\xFD"                             # 显式结束标记

    tag_ofs = 0x20
    data_ofs = tag_ofs + len(TAG)

    head = bytearray()
    head += b"S98"
    head += b"1"                                # 版本**字符**
    head += struct.pack("<I", TICK_MULT)
    head += struct.pack("<I", TICK_DIV)
    head += struct.pack("<I", 0)                # compression
    head += struct.pack("<I", tag_ofs)
    head += struct.pack("<I", data_ofs)
    head += struct.pack("<I", 0)                # loopOfs
    head += b"\x00" * (0x20 - len(head))        # 补到 0x20（probe 要求 ≥0x20）

    with open(path, "wb") as fh:
        fh.write(bytes(head))
        fh.write(TAG)
        fh.write(bytes(body))
    return path


#: 期望总 tick 数。``chip_formats.probe()`` 会把它换成秒。
TOTAL_TICKS = NOTE_TICKS + RELEASE_TICKS
#: 期望时长（秒）。``s98_native.TAIL_SECS`` 的余音**不在**这里面。
EXPECT_SECS = TOTAL_TICKS * TICK_MULT / float(TICK_DIV)     # 1.2


def main():
    import sys
    import tempfile
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        tempfile.mkdtemp(prefix="s98fx_"), "fixture.s98")
    make_s98(out)
    print("写出 %s（%d 字节，%.2f 秒，期望 %.2f Hz）"
          % (out, os.path.getsize(out), EXPECT_SECS, EXPECT_HZ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
