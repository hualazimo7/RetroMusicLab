#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aif_audio.py — 3DO 的 AIFF / AIFC（含 SDX2）→ PCM。

## 为什么单独一个模块

3DO 的音源不是「芯片合成」也不是「主机内存快照」，而是**游戏自己存好的
流式音频**，容器就是苹果的 AIFF/AIFC。所以它和 ``.adx`` / ``.hwas`` / ``.npsf``
是同一类（一个文件 = 一首成品），归 :mod:`stream_audio` 统一调度，
真正的解析与解码放这里。

## 3DO 实际用到的东西（全部来自真实包，不是猜的）

| 容器 | 编码 | 例子 | 说明 |
|---|---|---|---|
| ``FORM....AIFF`` | 8bit 有符号 PCM | Real Pinball 的 ``BGM.aiff`` | 22050Hz / 单声道 / 1,987,500 帧 |
| ``FORM....AIFC`` | ``SDX2`` | Psychic Detective 的 ``BD Music.M11k.SQXD.aifc`` | 11025Hz / 单声道 / 175,800 帧 |
| ``FORM....AIFF`` | 4bit（XA-ADPCM） | Road Rash 等 | ⚠ 本模块**暂不支持**，见下 |
| ``.MOD`` | ProTracker | Crash 'N Burn | 已由 :mod:`mod_audio` 负责，与本模块无关 |

## SDX2 算法（照抄 vgmstream ``src/coding/sdx2_decoder.c``）

「2:1 Squareroot-delta-exact」：一个字节一个样本，**偶数=绝对值模式，奇数=增量模式**。

```c
if (!(sample_byte & 1)) hist = 0;        /* 偶数：清历史，等于重新定基准 */
sample = hist + squares[sample_byte + 128];
hist   = clamp16(sample);
```

``squares[i] = i < 0 ? -i*i*2 : i*i*2``（i 取 -128..127）。注意它是**平方**不是线性，
所以这个「增量」在大跳变时给得特别猛 —— 这也正是它能压到 2:1 还能保住瞬态的原因。

⚠ 两个必须照抄的点：

1. **clamp16 只作用在输出上，``hist`` 存的是钳位后的值**（vgmstream 里
   ``hist = outbuf[...] = clamp16(sample)`` 是同一句）。写成「hist 存未钳位值」
   在长音频上会累积漂移。
2. **偶数字节要清零**。漏了这条，静音段会被前一段的历史拖出一条直流斜坡。

## 采样率不是整数

AIFF 的采样率是 **80 位扩展浮点**（extended，10 字节），不是整数。
本项目实测到的两个值：22050 和 11025。解析见 :func:`_read_f80`。
"""

import os
import struct
from typing import Callable, Optional

import numpy as np

#: 本模块负责的扩展名
ALL_EXTS = (".aif", ".aiff", ".aifc")

#: 支持 / 已知的编码
CODEC_PCM8 = "PCM8"
CODEC_PCM16BE = "PCM16BE"
CODEC_SDX2 = "SDX2"
CODEC_XA4 = "XA4"            # 4bit AIFF = XA-ADPCM，暂不支持

CODEC_NAMES = {
    CODEC_PCM8: "8-bit PCM (AIFF)",
    CODEC_PCM16BE: "16-bit PCM (AIFF)",
    CODEC_SDX2: "SDX2 ADPCM (3DO)",
    CODEC_XA4: "XA-ADPCM (AIFF 4bit)",
}

#: 这些后缀也可能是别的容器（.aif 被 WAV 冒名的情况见过），所以解析前先验 FORM
_FORM_MAGIC = b"FORM"


class _Cancelled(Exception):
    """与 stream_audio._Cancelled 同义 —— 只是不想反向依赖它。"""


# ---------------------------------------------------------------------------
# 头解析
# ---------------------------------------------------------------------------

def _read_f80(buf):
    """AIFF 的 80 位扩展浮点 → float。

    结构：``sign(1) | exponent(15) | mantissa(64，无隐含位)``。
    值 = mantissa × 2^(exponent - 16383 - 63)。

    实测两个样本都是规约数（``400e2b11...`` = 22050，``400dac44...`` = 11025），
    但为了不在一份怪文件上崩，指数为 0 时按 0 处理。
    """
    if len(buf) < 10:
        raise ValueError("COMM 太短，读不到采样率")
    exp = ((buf[0] & 0x7F) << 8) | buf[1]
    mant = int.from_bytes(buf[2:10], "big")
    if exp == 0 and mant == 0:
        return 0.0
    val = mant * (2.0 ** (exp - 16383 - 63))
    return -val if (buf[0] & 0x80) else val


def read_header(path) -> dict:
    """走一遍 chunk，返回容器信息。**抛异常**（probe 里再兜）。"""
    p = str(path)
    size = os.path.getsize(p)
    with open(p, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != _FORM_MAGIC:
            raise ValueError("不是 AIFF/AIFC（缺 FORM 头）")
        form = head[8:12]
        if form not in (b"AIFF", b"AIFC"):
            raise ValueError("FORM 类型是 %r，不是 AIFF/AIFC" % form)

        info = {
            "form": form.decode("ascii"),
            "aifc": form == b"AIFC",
            "channels": 0, "frames": 0, "bits": 0, "rate": 0.0,
            "codec": "", "data_off": 0, "data_len": 0,
            "name": "", "loop_start": -1, "loop_end": -1,
            "size": size,
        }

        off = 12
        comm_seen = False
        data_seen = False
        while off + 8 <= size:
            f.seek(off)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid = hdr[:4]
            csz = struct.unpack(">I", hdr[4:8])[0]
            body_off = off + 8

            if cid == b"COMM":
                if comm_seen:
                    raise ValueError("COMM 出现了两次")
                comm_seen = True
                f.seek(body_off)
                body = f.read(min(csz, 64))
                if len(body) < 18:
                    raise ValueError("COMM 只有 %d 字节" % len(body))
                info["channels"] = struct.unpack(">h", body[0:2])[0]
                info["frames"] = struct.unpack(">I", body[2:6])[0]
                info["bits"] = struct.unpack(">h", body[6:8])[0]
                info["rate"] = _read_f80(body[8:18])
                if info["aifc"]:
                    if len(body) < 22:
                        raise ValueError("AIFC 的 COMM 缺 compressionType")
                    ctype = body[18:22]
                    name_len = body[22] if len(body) > 22 else 0
                    info["name"] = body[23:23 + name_len].decode("latin-1", "replace")
                    info["codec"] = _codec_of(ctype, info["bits"])
                else:
                    info["codec"] = _codec_of(b"", info["bits"])

            elif cid == b"SSND":
                if data_seen:
                    raise ValueError("SSND 出现了两次")
                data_seen = True
                f.seek(body_off)
                b8 = f.read(8)
                if len(b8) < 8:
                    raise ValueError("SSND 太短")
                offset = struct.unpack(">I", b8[0:4])[0]
                info["data_off"] = body_off + 8 + offset
                info["data_len"] = max(0, csz - 8 - offset)

            elif cid in (b"MARK", b"INST"):
                # 只取循环点用于显示；转换本身不循环（和其他格式一致，按秒渲染）
                if csz and csz < 4096:
                    f.seek(body_off)
                    blob = f.read(csz)
                    _maybe_loop(blob, cid, info)

            off = body_off + csz + (csz & 1)      # chunk 按偶数字节对齐

    if not comm_seen:
        raise ValueError("没有 COMM 块")
    if not data_seen:
        raise ValueError("没有 SSND 块")
    if info["rate"] <= 0:
        raise ValueError("采样率非法：%r" % info["rate"])
    if info["channels"] <= 0:
        raise ValueError("声道数非法：%r" % info["channels"])
    info["secs"] = info["frames"] / float(info["rate"]) if info["rate"] else 0.0
    return info


def _codec_of(ctype: bytes, bits: int) -> str:
    """compressionType（AIFC）或位深（AIFF）→ 本项目内部的编码名。"""
    if ctype:
        if ctype == b"SDX2":
            return CODEC_SDX2
        if ctype == b"NONE":
            return CODEC_PCM16BE if bits == 16 else CODEC_PCM8
        if ctype in (b"twos", b"sowt"):
            return CODEC_PCM16BE
        if ctype in (b"raw ", b"in8", b"in24"):
            return CODEC_PCM8
        raise ValueError("不认识的 AIFC 编码 %r（3DO 只见过 SDX2）" % ctype)
    if bits == 8:
        return CODEC_PCM8
    if bits == 16:
        return CODEC_PCM16BE
    if bits == 4:
        return CODEC_XA4
    raise ValueError("不认识的 AIFF 位深 %d" % bits)


def _maybe_loop(blob: bytes, cid: bytes, info: dict):
    """从 MARK 里捡 loop 标记（``-1`` 表示没有）。

    AIFF 的循环点是 MARK + INST 两处配合，这里只做**展示用**的粗读：
    找到 id 为 1 的标记，把位置记下来；INST 的 sustainLoop 更准但结构更绕，
    且转换不循环，所以不解析。
    """
    try:
        if cid == b"MARK":
            n = struct.unpack(">H", blob[0:2])[0]
            off = 2
            for _ in range(n):
                mid = struct.unpack(">h", blob[off:off + 2])[0]
                pos = struct.unpack(">I", blob[off + 2:off + 6])[0]
                ln = blob[off + 6]
                if mid == 1:
                    info["loop_start"] = pos
                off += 7 + ln + (ln & 1)
    except Exception:                                # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------

def _squares_table():
    """``squares[i] = i < 0 ? -i*i*2 : i*i*2``，i 取 -128..127，下标 +128。

    与 vgmstream 里那张 256 项常量表逐值等价（``squares[0] = -32768``、
    ``squares[128] = 0``、``squares[255] = 32258``）。
    """
    i = np.arange(-128, 128, dtype=np.int32)
    return np.where(i < 0, -(i * i) * 2, (i * i) * 2).astype(np.int32)


_SQUARES = _squares_table()


def decode_sdx2(data: bytes) -> np.ndarray:
    """SDX2 → int16 一维（单声道）。

    向量化做法：偶数字节会把历史清零，所以把数据切成「以偶数字节开头」的若干段，
    每段内是 delta 的**前缀和**；段首的前缀和基准减掉即可。等价于逐样本循环，
    但在 200 万样本上快两个数量级。
    """
    if not data:
        return np.zeros(0, dtype=np.int16)
    b = np.frombuffer(data, dtype=np.int8).astype(np.int32)
    delta = _SQUARES[b + 128]

    # 段起点：偶数字节（bit0 == 0）的位置，加上 0
    resets = np.flatnonzero((b & 1) == 0)
    if resets.size == 0 or resets[0] != 0:
        resets = np.concatenate(([0], resets))

    cum = np.cumsum(delta, dtype=np.int64)
    out = np.empty(b.size, dtype=np.int64)

    # 每段的基准 = 段首前一个位置的前缀和（段首本身要从 0 起算）
    starts = resets
    ends = np.concatenate((resets[1:], [b.size]))
    for s, e in zip(starts.tolist(), ends.tolist()):
        base = cum[s - 1] if s > 0 else 0
        out[s:e] = cum[s:e] - base

    np.clip(out, -32768, 32767, out=out)
    return out.astype(np.int16)


def _decode_pcm8(data: bytes) -> np.ndarray:
    """8bit 有符号 PCM → int16（左移 8 位，不做缩放）。"""
    return (np.frombuffer(data, dtype=np.int8).astype(np.int16) << 8)


def _decode_pcm16be(data: bytes) -> np.ndarray:
    n = len(data) // 2
    return np.frombuffer(data[:n * 2], dtype=">i2").astype(np.int16)


def decode_pcm(path, cancel=None, on_progress: Optional[Callable] = None):
    """AIFF/AIFC → ``(int16 交错一维, rate)``。

    与 :mod:`stream_audio` 里其他 ``decode_*`` 同契约：
    ``on_progress(0..1)``、``cancel.is_set()`` 时抛 ``_Cancelled``。
    """
    h = read_header(path)
    codec = h["codec"]
    if codec == CODEC_XA4:
        raise ValueError("AIFF 4bit 是 XA-ADPCM（Road Rash 那类），本工具还没接")

    if cancel is not None and cancel.is_set():
        raise _Cancelled()

    with open(str(path), "rb") as f:
        f.seek(h["data_off"])
        body = f.read(h["data_len"])
    if on_progress is not None:
        try:
            on_progress(0.15)
        except Exception:                        # noqa: BLE001
            pass

    if codec == CODEC_SDX2:
        mono = decode_sdx2(body)
    elif codec == CODEC_PCM8:
        mono = _decode_pcm8(body)
    elif codec == CODEC_PCM16BE:
        mono = _decode_pcm16be(body)
    else:
        raise ValueError("没有 %s 的解码器" % codec)

    if cancel is not None and cancel.is_set():
        raise _Cancelled()

    nch = h["channels"]
    if nch > 1:
        # 多声道按交错排列；按帧截齐
        n = (mono.size // nch) * nch
        mono = mono[:n]

    if on_progress is not None:
        try:
            on_progress(1.0)
        except Exception:                        # noqa: BLE001
            pass

    # 按 COMM 的帧数截断：SSND 尾部可能有对齐填充
    frames = h["frames"]
    if frames and mono.size > frames * nch:
        mono = mono[:frames * nch]
    return mono, int(round(h["rate"]))


def probe(path) -> dict:
    """时长 / 采样率 / 声道 / 编码。**永不抛异常**（与 chip_formats.probe 同契约）。"""
    out = {"ok": False, "secs": 0.0, "rate": 0, "channels": 0,
           "codec": "", "title": os.path.splitext(os.path.basename(str(path)))[0],
           "error": ""}
    try:
        h = read_header(path)
    except Exception as exc:                     # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        return out
    out.update({
        "ok": True,
        "secs": h["secs"],
        "rate": int(round(h["rate"])),
        "channels": h["channels"],
        "codec": CODEC_NAMES.get(h["codec"], h["codec"]),
    })
    return out
