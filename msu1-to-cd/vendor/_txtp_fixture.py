#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_txtp_fixture.py — 造一个最小的 PS1 ``.txtp`` 包，给两套门禁共用。

为什么单独一个文件：源码门禁（``test_web_ui.py``）与 exe 验收
（``check_web_exe.py``）都要用同一份样本，各写一遍必然漂移 ——
一边改了扇区布局，另一边还在验旧口径。

包结构（``base/psxpack/``）：

    sound/T.xa          8 个 XA-ADPCM 音频扇区（2352 B/扇区）
    sound/T.DA          1.0 秒 44.1k 立体声裸 PCM
    sound/.DA.TXTH      裸 PCM 的参数（sample_rate / channels）
    A.txtp              → sound/T.xa（带 #I 循环点）
    B.txtp              → sound/T.DA
    C.txtp              → sound/missing.xa（**故意缺失**，验报错路径）
    !tags.m3u           曲名表：A = 测试曲A，B = 测试曲B

扇区布局一个字节都不能少（``msu1_to_cd.xa_sector_groups`` 的判定条件）：
12B sync + 4B header + 8B subheader + 2324B data + 4B EDC = 2352。
其中 sync 是 ``00 FF*10 00``、header[3] == 2（mode 2）、subheader 8 字节
前后重复、submode 同时带 0x20(Form2) 与 0x04(Audio)。
"""

import os
import struct

SECTOR = 2352
N_SECTORS = 8
#: 8 扇区 × 2016 采样 / 37800Hz = 0.4267 秒（解码会重采样到 44.1k，时长不变）
XA_SECONDS = N_SECTORS * 2016.0 / 37800.0
DA_SECONDS = 1.0


def make_pack(base):
    """在 ``base`` 下建好 ``psxpack/`` 并返回包目录。"""
    pack = os.path.join(base, "psxpack")
    snd = os.path.join(pack, "sound")
    os.makedirs(snd, exist_ok=True)

    sync = b"\x00" + b"\xff" * 10 + b"\x00"
    # 一个 sound group = 16B 头 + 112B 数据；18 组 = 2304B，剩 20B 空着。
    # 头里 8 个子帧全填 0x01（filter=0, shift=1），数据全 0x11
    # —— 解出来恒定 2048，既不静音也不削顶。
    grp = bytes([0x01] * 16) + bytes([0x11]) * 112
    payload = grp * 18 + bytes(2324 - 18 * 128)
    with open(os.path.join(snd, "T.xa"), "wb") as fh:
        for _ in range(N_SECTORS):
            fh.write(sync)
            fh.write(b"\x00\x00\x01\x02")            # min/sec/frame/mode=2
            fh.write(bytes([1, 1, 0x64, 0x01]) * 2)  # subheader ×2（重复）
            fh.write(payload)
            fh.write(b"\x00" * 4)                    # EDC

    # 裸 PCM：44100 帧 × 2 声道 = 88200 个样本，+3000 / -3000 交替
    pcm = struct.pack("<88200h", *([3000, -3000] * 44100))
    with open(os.path.join(snd, "T.DA"), "wb") as fh:
        fh.write(pcm)
    with open(os.path.join(snd, ".DA.TXTH"), "w", encoding="utf-8") as fh:
        fh.write("codec = PCM16LE\nchannels = 2\nsample_rate = 44100\n"
                 "num_samples = %d\n" % (len(pcm) // 4))

    with open(os.path.join(pack, "A.txtp"), "w", encoding="utf-8") as fh:
        fh.write("sound/T.xa #I 2153088\n")     # #I 是循环点，不参与时长
    with open(os.path.join(pack, "B.txtp"), "w", encoding="utf-8") as fh:
        fh.write("sound/T.DA\n")
    with open(os.path.join(pack, "C.txtp"), "w", encoding="utf-8") as fh:
        fh.write("sound/missing.xa\n")
    with open(os.path.join(pack, "!tags.m3u"), "w", encoding="utf-8") as fh:
        fh.write("# %TITLE 测试曲A\nA.txtp\n# %TITLE 测试曲B\nB.txtp\n")
    return pack


# ══════════════════════════════════════════════════════════════════════
#  第二种形态：**一个 XA 里有多条子流**，子流号写在 .txtp 的文件名里
# ══════════════════════════════════════════════════════════════════════
#: 子流号 -> 扇区数（故意各不相同，好验证「每首时长不同」）
STREAM_SECTORS = {1: 6, 2: 4, 3: 2}
#: 子流号 -> subheader 里的 channel 字节
#: ⚠ 故意让「序号」和「channel 字节」不一致（channel 从 0 起），
#:   这样能验证实现走的是「第几条流」而不是「channel == N」。
STREAM_CHANNELS = {1: 0, 2: 1, 3: 2}


def stream_seconds(n):
    return n * 2016.0 / 37800.0


def make_stream_pack(base):
    """造一个「多子流 XA + 空 .txtp」的包，返回包目录。

    这就是用户报的那种：解压出来是 ``BGM.XA`` 加一堆 **0 字节**的
    ``BGM.XA#01.txtp`` / ``#02`` …。早先 ``parse_txtp`` 见到空文件直接报
    「.txtp 是空的」，整包 PS1 音源一首也转不出来。
    """
    pack = os.path.join(base, "streampack")
    os.makedirs(pack, exist_ok=True)

    sync = b"\x00" + b"\xff" * 10 + b"\x00"
    grp = bytes([0x01] * 16) + bytes([0x11]) * 112
    payload = grp * 18 + bytes(2324 - 18 * 128)

    xa = os.path.join(pack, "BGM.XA")
    with open(xa, "wb") as fh:
        # 按子流交错写，模拟真实 XA 的排布（解码要按 channel 挑扇区）
        order = []
        for n, sec in STREAM_SECTORS.items():
            order.extend([n] * sec)
        order.sort(key=lambda n: (n % 3, n))     # 交错一下，别顺序排列
        for n in order:
            ch = STREAM_CHANNELS[n]
            fh.write(sync)
            fh.write(b"\x00\x00\x01\x02")            # min/sec/frame/mode=2
            fh.write(bytes([1, ch, 0x64, 0x01]) * 2)  # subheader ×2（重复）
            fh.write(payload)
            fh.write(b"\x00" * 4)                     # EDC

    # 0 字节的「文件名式子流」.txtp
    for n in sorted(STREAM_SECTORS):
        open(os.path.join(pack, "BGM.XA#%02d.txtp" % n), "wb").close()
    # 越界的子流：验证报错说人话，而不是抛 IndexError
    open(os.path.join(pack, "BGM.XA#09.txtp"), "wb").close()
    return pack
