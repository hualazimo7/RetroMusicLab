#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pivotal_psf.py — Pivotal Games 的 ``.psf``（GameCube / Xbox / PS2）→ PCM。

## 这是什么格式，为什么单开一个模块

`.psf` 这个扩展名在本工程里已经有两个主人了：

* **PS1 / PS2 的 PSF**（``psf_native``）—— 内存快照 + 可执行程序，要跑
  CPU + SPU 模拟器，靠外挂的 ``psf2wav.exe`` 和一份 BIOS；
* **Capcom QSound 的 QSF**（``qsf_native``）—— 同一个容器，版本字节 0x41。

现在多出第三个：**Pivotal Games**（《Conflict: Desert Storm》系列、
《The Great Escape》）自家引擎的流式音频。它借用了 ``PSF`` 这 3 个字节当魔数，
但**和 PSF 家族毫无关系** —— 没有程序段、没有快照，就是一包压好的 ADPCM。

判据只有一条，看**第 4 个字节**（类型字节）::

    PS1 / PS2 PSF :  0x01 0x02 0x11 0x12 0x21 0x22
    Pivotal      :  0x31 0x40 0x60 0x80 0x81 0xA1 0xC0 0xD1

⚠ **0x01 与 0x21 两边都用**（Pivotal 的单声道变体 vs PS1/PS2 的版本号）。
  本模块因此**不认**这两个值 —— 撞名时一律让给 PSF 家族。实测 joshw 的
  GameCube 包里只有 0x80 / 0x60 / 0x31 三种，没有这个歧义。

## 结构（照 vgmstream ``src/meta/psf.c`` 逐条核对）

### 单片（``PSF`` + 类型字节）

===========  ========================================================
偏移          含义
===========  ========================================================
0x00          ``PSF`` 魔数
0x03          u8 类型字节（见上表）
0x04          u32le **config**：bits20-31 = 采样率代号，bits0-19 = 块数
0x08          .. 音频数据（DSP 编码时这里还有每声道 0x60 字节的 DSP 头）
===========  ========================================================

采样率代号：``3763 → 44100``、``1365 → 16000``、``940 → 11050``、
``460 → 5000``，其余按 ``代号 × 11.72`` 近似。

数据长度 = ``块数 × 交错字节数 × 声道数``。实测 ``STREAM__000.psf``：
config = ``0xEB31F020`` → 代号 ``(>>20)&0xFFF`` = 3763 → **44100 Hz**，
块数 ``& 0xFFFFF`` = 127008 → ``127008 × 0x10 × 2`` = **4,064,256**
= 文件长 − 8，**分毫不差**。

### 分段（``PSF\\x60`` / ``PSF\\x31``）

===========  ========================================================
偏移          含义
===========  ========================================================
0x00          ``PSF`` + 0x60 / 0x31
0x04          u32le 段总数（``transition_count``）
0x08          ..  段表，每项 **0x0C** 字节
===========  ========================================================

段表每项：

===========  ========================================================
项内偏移      含义
===========  ========================================================
+0x00         u32le 该段的 PSF 数据在文件里的偏移
+0x04         u16le track0 的下一段号
+0x06         u16le track1 的下一段号
+0x08         u16le track2 的下一段号
+0x0A         u16le track3 的下一段号
===========  ========================================================

**段 0 不是音频**，它只存 4 条 track 的入口段号。子流编号：

=======  ======================================================
子流     含义
=======  ======================================================
1        ``full`` —— 4 条 track 依次串起来
2 ~ 5    ``track1`` ~ ``track4`` —— 单条 track 自己循环
6 及以后  单个段
=======  ======================================================

子流总数 = ``1 + 4 + (段总数 − 1)``。实测 ``STREAM__004.psf`` 段总数 161
→ 165 条子流，与 ``vgmstream -m`` 报的 ``stream count: 165`` 一致。

⚠ 段表里的「下一段号」是**链式**的：从段 0 的入口出发，一路读 next 走到
  重复为止 —— 重复处就是循环点。所以「时长」不是简单地把所有段加起来，
  而是**按序列走出来的**。:func:`walk_sequence` 逐行照抄了 vgmstream 的
  遍历逻辑（含 ``full`` 换 track 的那段特殊处理）。

## 两种编码

* **PlayStation 4-bit ADPCM（Pivotal 变体）** —— 类型 0x80/0x81/0xC0/0xA1。
  16 字节一块，**1 字节头 + 15 字节数据 = 30 个样本**。
  标准 PSX 只取 28 个（把第 16 字节当 padding 丢掉），Pivotal 把它也用上了
  —— 这就是「Pivotal」三个字的全部含义。交错单位 0x10。
* **Nintendo DSP 4-bit ADPCM** —— 类型 0xD1。8 字节一帧、14 个样本，
  系数与初始历史存在每声道 0x60 字节的 DSP 头里（不在全局表里）。交错 0x08。

## 验证

``vendor/_check_pivotal_ref.py`` 拿 vgmstream 当黄金标准逐样本比对。
``PSF\\x80`` 的两个真实文件：**最大差 0、不一致样本 0**。
"""

import os
import struct

import numpy as np

#: 输出规格 —— 与全工程一致
OUT_RATE = 44100
OUT_CH = 2

#: 魔数前三字节
MAGIC = b"PSF"

#: 单片类型字节 → ``(编码, 声道数)``。
#: ⚠ 故意**不含** 0x01 / 0x21：那两个值与 PS1 / PS2 的 PSF 版本号撞名，
#:   判给 PSF 家族更安全（见模块注释）。
SINGLE_FLAGS = {
    0x80: ("psx_pivotal", 2),   # GameCube / Xbox，实测就是这一种
    0x81: ("psx_pivotal", 2),
    0xC0: ("psx_pivotal", 2),   # PS2 版用的是标准 PSX（28 样本），见下
    0x40: ("psx_pivotal", 1),
    0xA1: ("psx_pivotal", 2),
    0xD1: ("ngc_dsp", 2),       # Conflict: Desert Storm 2 (GC)
}

#: 分段文件的类型字节
SEG_FLAGS = (0x60, 0x31)

#: 段表项长度
SEG_ENTRY = 0x0C

#: 采样率代号表
_RATE_CODE = {3763: 44100, 1365: 16000, 940: 11050, 460: 5000}

#: PSX ADPCM 的 5 组预测系数
_PSX_K0 = (0, 60, 115, 98, 122)
_PSX_K1 = (0, 0, -52, -55, -60)


# ══════════════════════════════════════════════════════════════════════
#  识别
# ══════════════════════════════════════════════════════════════════════
def _head(path, n=0x10):
    try:
        with open(str(path), "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def is_pivotal(path) -> bool:
    """是不是 Pivotal 的 ``.psf``。**只看魔数，不看扩展名**（永不抛异常）。"""
    h = _head(path, 4)
    if len(h) < 4 or h[:3] != MAGIC:
        return False
    return h[3] in SINGLE_FLAGS or h[3] in SEG_FLAGS


def kind_of(path) -> str:
    """``"single"`` / ``"segmented"`` / ``""``。"""
    h = _head(path, 4)
    if len(h) < 4 or h[:3] != MAGIC:
        return ""
    if h[3] in SEG_FLAGS:
        return "segmented"
    if h[3] in SINGLE_FLAGS:
        return "single"
    return ""


# ══════════════════════════════════════════════════════════════════════
#  头解析
# ══════════════════════════════════════════════════════════════════════
def _rate_of(config):
    """config 的高 12 位 → 采样率。"""
    code = (config >> 20) & 0xFFF
    if code in _RATE_CODE:
        return _RATE_CODE[code]
    rate = int(round(code * 11.72))
    return rate if rate > 0 else 0


def read_single(data, base=0):
    """读一个**单片** PSF 的头。

    Args:
        data: 整个文件的字节（段也可能在中间，所以用 base 指位置）。
        base: 这个 PSF 在 ``data`` 里的起点。

    Returns:
        ``{ok, codec, channels, rate, blocks, data_off, data_size, samples,
        error}``。``samples`` 是**每声道**样本数。
    """
    out = {"ok": False, "codec": "", "channels": 0, "rate": 0, "blocks": 0,
           "data_off": 0, "data_size": 0, "samples": 0, "error": "",
           "coefs": None, "hist": None}
    if len(data) - base < 8 or data[base:base + 3] != MAGIC:
        out["error"] = "不是 PSF（魔数不是 PSF）"
        return out
    flags = data[base + 3]
    if flags not in SINGLE_FLAGS:
        out["error"] = "PSF 类型字节 0x%02X 不是 Pivotal 的单片类型" % flags
        return out
    codec, nch = SINGLE_FLAGS[flags]
    config = struct.unpack_from("<I", data, base + 4)[0]
    rate = _rate_of(config)
    if rate <= 0 or rate > 192000:
        out["error"] = "采样率非法：%d（config=0x%08X）" % (rate, config)
        return out
    blocks = config & 0xFFFFF
    ilv = 0x08 if codec == "ngc_dsp" else 0x10

    if codec == "ngc_dsp":
        # 每声道一个 0x60 字节的 DSP 头：+0x00 样本数、+0x1C 系数、+0x40 历史
        data_off = base + 0x08 + 0x60 * nch
        samples = struct.unpack_from(">i", data, base + 0x08)[0]
        coefs, hist = [], []
        for c in range(nch):
            h = base + 0x08 + 0x60 * c
            coefs.append(struct.unpack_from(">16h", data, h + 0x1C))
            hist.append(struct.unpack_from(">2h", data, h + 0x40))
        data_size = blocks * ilv * nch
        out.update({"coefs": coefs, "hist": hist})
        if samples <= 0:
            # 头里的样本数不可信时退回「按块算」：8 字节一帧 = 14 样本
            samples = (data_size // (ilv * nch)) * 14
    else:
        data_off = base + 0x08
        data_size = blocks * ilv * nch
        # 每块 30 个样本（Pivotal 变体把标准 PSX 的 padding 字节也用上了）
        samples = blocks * 30

    # 头里的长度不可信时以「文件还剩多少」为准
    avail = len(data) - data_off
    if data_size <= 0 or data_size > avail:
        data_size = max(0, avail)
    out.update({"ok": True, "codec": codec, "channels": nch, "rate": rate,
                "blocks": blocks, "data_off": data_off,
                "data_size": data_size, "samples": samples})
    return out


def read_segments(data):
    """读**分段** PSF 的段表。

    Returns:
        ``{ok, count, entries, error}``，``entries`` 是
        ``[(offset, (next0, next1, next2, next3)), ...]``。
    """
    out = {"ok": False, "count": 0, "entries": [], "error": ""}
    if len(data) < 8 or data[:3] != MAGIC or data[3] not in SEG_FLAGS:
        out["error"] = "不是分段 PSF（魔数不对）"
        return out
    count = struct.unpack_from("<I", data, 4)[0]
    if count <= 0 or count > 8192:
        out["error"] = "段总数非法：%d" % count
        return out
    if 0x08 + count * SEG_ENTRY > len(data):
        out["error"] = ("段表超出文件末尾（%d 段 × 12 字节 + 8 > %d 字节）"
                        % (count, len(data)))
        return out
    entries = []
    for k in range(count):
        p = 0x08 + k * SEG_ENTRY
        off = struct.unpack_from("<I", data, p)[0]
        nxt = struct.unpack_from("<4H", data, p + 4)
        entries.append((off, nxt))
    out.update({"ok": True, "count": count, "entries": entries})
    return out


# ══════════════════════════════════════════════════════════════════════
#  序列（4 条 track 的链式走法）
# ══════════════════════════════════════════════════════════════════════
def total_subsongs(count) -> int:
    """子流总数 = ``1(full) + 4(track1~4) + (段总数 − 1)``。"""
    return 1 + 4 + max(0, int(count) - 1)


def walk_sequence(entries, subsong=1):
    """走出某条子流的段序列。

    逐行照抄 vgmstream ``init_vgmstream_psf_segmented`` 的遍历循环。

    Args:
        entries: :func:`read_segments` 的 ``entries``。
        subsong: 1 = ``full``，2~5 = ``track1~4``，6+ = 单个段。

    Returns:
        ``(sequence, loop_start, loop_end)``。``loop_start`` / ``loop_end``
        是**序列下标**（不是样本），``-1`` 表示没有循环。
    """
    count = len(entries)
    if count <= 0:
        return [], -1, -1
    total = total_subsongs(count)
    if subsong < 1 or subsong > total:
        subsong = 1

    if subsong == 1:
        cur_track = 0
    elif subsong <= 5:
        cur_track = (subsong - 1) - 1          # 0..3
    else:
        seg = subsong - 1 - 4
        if seg >= count:
            seg = count - 1
        return [seg], -1, -1

    track = [[0] * 256 for _ in range(4)]
    tcount = [0] * 4
    sequence = []
    loop_flag = False
    loop_start = loop_end = -1
    cur_point = 0

    while len(sequence) < 512 and cur_track >= 0:
        nxt = entries[cur_point][1][cur_track] if cur_point < count else 0
        # 这条 track 之前走过这个段吗？
        repeat = -1
        for i in range(tcount[cur_track]):
            if track[cur_track][i] == nxt:
                repeat = i
                break
        if repeat >= 0:
            if subsong == 1:
                cur_track += 1
                if loop_flag:
                    loop_start = repeat
                    break
                if cur_track > 3:
                    cur_track = 0
                    loop_flag = True
                nxt = entries[cur_point][1][cur_track] if cur_point < count else 0
                if loop_flag:
                    loop_end = len(sequence)
            else:
                loop_flag = True
                loop_start = repeat
                loop_end = len(sequence) - 1
                break
        if tcount[cur_track] < 256:
            track[cur_track][tcount[cur_track]] = nxt
            tcount[cur_track] += 1
        sequence.append(nxt)
        cur_point = nxt
        if cur_point >= count:
            break
    return sequence, loop_start, loop_end


# ══════════════════════════════════════════════════════════════════════
#  解码：PlayStation 4-bit ADPCM（Pivotal 变体，30 样本/块）
# ══════════════════════════════════════════════════════════════════════
def _psx_pivotal_channel(data, off, nblocks, stride, per_ch, ch, cancel=None):
    """一个声道的 PSX ADPCM → int32。

    与标准 PSX 的两处差别（其余逐字相同）：

    1. **每块 30 个样本** —— 标准只取 1 头 + 14 数据 = 28 个，第 16 字节当
       padding；Pivotal 把 15 个数据字节全用上。
    2. **不做 ``+32`` 四舍五入** —— 预测项是纯 ``>> 6``。这一点是拿
       vgmstream 逐样本比出来的：加 32 会让 99.8% 的样本差 1~60，
       不加则**最大差 0**。
    """
    out = np.zeros(per_ch * 30, np.int32)
    h1 = h2 = 0
    oi = 0
    for k in range(per_ch):
        if cancel is not None and (k & 0x1FFF) == 0 and cancel.is_set():
            raise RuntimeError("已取消")
        p = off + (k * stride + ch) * 16
        sp = data[p]
        sh = sp & 0x0F
        if sh > 12:
            sh = 9
        fi = sp >> 4
        if fi > 4:
            fi = 0
        c0 = _PSX_K0[fi]
        c1 = _PSX_K1[fi]
        for i in range(15):
            b = data[p + 1 + i]
            for nib in (b & 0x0F, b >> 4):
                nn = nib if nib < 8 else nib - 16
                s = (nn << 12) >> sh
                s += (c0 * h1 + c1 * h2) >> 6
                h2, h1 = h1, s
                out[oi] = 32767 if s > 32767 else (-32768 if s < -32768 else s)
                oi += 1
    return out[:oi]


def _decode_psx(data, h, cancel=None):
    """整条 PSX 流 → ``(int16 交错, rate, nch)``。"""
    nch = h["channels"]
    off = h["data_off"]
    nbytes = h["data_size"]
    nblocks = nbytes // 16
    per_ch = nblocks // nch
    chans = [_psx_pivotal_channel(data, off, nblocks, nch, per_ch, c, cancel)
             for c in range(nch)]
    n = min(len(x) for x in chans) if chans else 0
    if nch == 1:
        return chans[0][:n].astype(np.int16, copy=False), h["rate"], 1
    st = np.empty(n * 2, np.int16)
    st[0::2] = chans[0][:n]
    st[1::2] = chans[1][:n]
    return st, h["rate"], 2


# ══════════════════════════════════════════════════════════════════════
#  解码：Nintendo DSP 4-bit ADPCM（8 字节一帧 = 14 样本）
# ══════════════════════════════════════════════════════════════════════
def _dsp_channel(data, off, nframes, stride, per_ch, ch, coefs, hist,
                 nsamples, cancel=None):
    """一个声道的 DSP ADPCM → int32。

    ⚠ 系数**必须从文件头读**，不能用通用的那张表 —— 不同游戏的系数不一样，
      用错表解出来是「像音乐但每个音都跑调」。

    ⚠ 增量口径（实测踩过）：``sample = (nibble × 2^scale_exp) << 11``，
      也就是**增量 = ``nibble << scale_exp``**。写成 ``(nibble << 8) >> scale_exp``
      在 ``scale_exp == 4`` 时碰巧相等（``2^4 == 256/2^4``），所以第一个帧
      能对上、后面全错 —— 这种「第一帧对、之后慢慢跑偏」的错最难认。
      另外 ``+1024`` 是加在**整条和式**上的，不是只给预测项做四舍五入。
    """
    out = np.zeros(per_ch * 14, np.int32)
    h1, h2 = int(hist[0]), int(hist[1])
    oi = 0
    for k in range(per_ch):
        if cancel is not None and (k & 0x1FFF) == 0 and cancel.is_set():
            raise RuntimeError("已取消")
        p = off + (k * stride + ch) * 8
        head = data[p]
        pred = (head >> 4) & 0x0F
        if pred > 7:
            pred = 7
        scale = 1 << (head & 0x0F)
        c1 = coefs[pred * 2]
        c2 = coefs[pred * 2 + 1]
        for i in range(7):
            b = data[p + 1 + i]
            for nib in (b >> 4, b & 0x0F):
                v = nib - 16 if nib >= 8 else nib
                s = ((v * scale) << 11) + 1024 + c1 * h1 + c2 * h2
                s >>= 11
                if s > 32767:
                    s = 32767
                elif s < -32768:
                    s = -32768
                h1, h2 = s, h1
                out[oi] = s
                oi += 1
    if nsamples > 0:
        return out[:min(oi, nsamples)]
    return out[:oi]


def _decode_dsp(data, h, cancel=None):
    """整条 DSP 流 → ``(int16 交错, rate, nch)``。"""
    nch = h["channels"]
    off = h["data_off"]
    nframes = h["data_size"] // 8
    per_ch = nframes // nch
    nsamples = h["samples"]
    chans = []
    for c in range(nch):
        chans.append(_dsp_channel(data, off, nframes, nch, per_ch, c,
                                  h["coefs"][c], h["hist"][c], nsamples, cancel))
    n = min(len(x) for x in chans) if chans else 0
    if nch == 1:
        return chans[0][:n].astype(np.int16, copy=False), h["rate"], 1
    st = np.empty(n * 2, np.int16)
    st[0::2] = chans[0][:n]
    st[1::2] = chans[1][:n]
    return st, h["rate"], 2


# ══════════════════════════════════════════════════════════════════════
#  对外：探测 / 解码
# ══════════════════════════════════════════════════════════════════════
def probe(path, subsong=1) -> dict:
    """时长 / 采样率 / 声道。**永不抛异常**（与 chip_formats.probe 同契约）。"""
    out = {"ok": False, "secs": 0.0, "rate": 0, "channels": 0,
           "codec": "", "title": "", "error": "", "subsongs": 0,
           "subsong": 0, "kind": ""}
    p = str(path or "")
    try:
        from cd_audio import _title_of
        out["title"] = _title_of(p)
    except Exception:                                # noqa: BLE001
        out["title"] = os.path.splitext(os.path.basename(p))[0]
    if not os.path.isfile(p):
        out["error"] = "文件不存在"
        return out
    kind = kind_of(p)
    out["kind"] = kind
    if not kind:
        out["error"] = "不是 Pivotal PSF（魔数/类型字节不匹配）"
        return out
    try:
        with open(p, "rb") as f:
            data = f.read()
        if kind == "single":
            h = read_single(data, 0)
            if not h["ok"]:
                out["error"] = h["error"]
                return out
            out.update({"ok": True, "rate": h["rate"],
                        "channels": h["channels"],
                        "codec": ("PSX ADPCM (Pivotal)" if h["codec"] == "psx_pivotal"
                                  else "Nintendo DSP ADPCM"),
                        "secs": h["samples"] / float(h["rate"]),
                        "subsongs": 1, "subsong": 1})
            return out
        seg = read_segments(data)
        if not seg["ok"]:
            out["error"] = seg["error"]
            return out
        total = total_subsongs(seg["count"])
        subsong = int(subsong or 1)
        if subsong < 1 or subsong > total:
            subsong = 1
        seq, _ls, _le = walk_sequence(seg["entries"], subsong)
        secs = 0.0
        rate = 0
        nch = 0
        for si in seq:
            if si >= seg["count"]:
                continue
            h = read_single(data, seg["entries"][si][0])
            if not h["ok"]:
                continue
            rate = rate or h["rate"]
            nch = nch or h["channels"]
            secs += h["samples"] / float(h["rate"])
        out.update({"ok": bool(seq), "rate": rate, "channels": nch,
                    "codec": "Pivotal 分段（%d 段）" % len(seq),
                    "secs": secs, "subsongs": total, "subsong": subsong})
        if not seq:
            out["error"] = "段序列为空"
        return out
    except Exception as exc:                         # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        return out


def decode_any(path, subsong=1, cancel=None, on_progress=None):
    """解成 ``(int16 交错, rate, 声道数)``。分段会按序列把各段拼起来。"""
    p = str(path or "")
    if not os.path.isfile(p):
        raise RuntimeError("文件不存在：%s" % p)
    with open(p, "rb") as f:
        data = f.read()
    kind = kind_of(p)
    if kind == "single":
        h = read_single(data, 0)
        if not h["ok"]:
            raise ValueError(h["error"])
        if h["codec"] == "ngc_dsp":
            return _decode_dsp(data, h, cancel)
        return _decode_psx(data, h, cancel)
    if kind != "segmented":
        raise ValueError("不是 Pivotal PSF（魔数/类型字节不匹配）")

    seg = read_segments(data)
    if not seg["ok"]:
        raise ValueError(seg["error"])
    total = total_subsongs(seg["count"])
    subsong = int(subsong or 1)
    if subsong < 1 or subsong > total:
        subsong = 1
    seq, _ls, _le = walk_sequence(seg["entries"], subsong)
    if not seq:
        raise ValueError("段序列为空")

    parts = []
    rate = 0
    nch = 0
    for n, si in enumerate(seq):
        if cancel is not None and cancel.is_set():
            raise RuntimeError("已取消")
        if on_progress is not None:
            try:
                on_progress(n / float(len(seq)))
            except Exception:                        # noqa: BLE001
                pass
        if si >= seg["count"]:
            continue
        h = read_single(data, seg["entries"][si][0])
        if not h["ok"]:
            continue
        if h["codec"] == "ngc_dsp":
            pcm, r, c = _decode_dsp(data, h, cancel)
        else:
            pcm, r, c = _decode_psx(data, h, cancel)
        rate = rate or r
        nch = max(nch, c)
        if c == 1 and nch == 2:
            pcm = np.repeat(pcm.reshape(-1, 1), 2, axis=1).reshape(-1)
        parts.append(pcm)
    if not parts:
        raise ValueError("所有段都解不出来")
    if on_progress is not None:
        try:
            on_progress(1.0)
        except Exception:                            # noqa: BLE001
            pass
    return np.concatenate(parts), (rate or OUT_RATE), (nch or 2)


def decode_pcm(path, subsong=1, cancel=None, on_progress=None) -> bytes:
    """→ 44100 / 16bit / 立体声**小端** PCM 字节（给主流水线用）。"""
    pcm, rate, nch = decode_any(path, subsong, cancel, on_progress)
    if pcm.size == 0:
        return b""
    if nch == 1:
        pcm = np.repeat(np.asarray(pcm).reshape(-1, 1), 2, axis=1).reshape(-1)
    if rate != OUT_RATE:
        # Pivotal 的样本几乎都是 44100；真遇到别的率才走重采样
        import stream_audio
        pcm = stream_audio._resample_to_out(pcm, rate, 2)
    return np.ascontiguousarray(pcm, dtype="<i2").tobytes()


def convert_one(src, out_dir, *, used=None, start_idx=0, max_secs=0.0,
                cancel=None, on_log=None, on_progress=None, **_kw):
    """解一个 Pivotal ``.psf`` → ``NN_标题.wav``。

    返回结构与 :func:`spc_wav.write_tracks` **同形**（``ok/fail/files/
    seconds/errors/last_idx``），这样 ``dl_convert`` 的记账段一行都不用改
    —— 与 ``stream_audio.convert_one`` / ``cd_audio.convert_one`` 同一个契约。

    ⚠ 写出走 :func:`spc_wav.write_tracks`，不另写一份「编号 + 同名避让 +
      截断淡出 + 体积统计」—— 那份逻辑已经在四个模块里各有一份了。
    """
    import spc_wav
    log = on_log or (lambda _t: None)
    start = int(start_idx or 0)
    try:
        pcm = decode_pcm(src, subsong=1, cancel=cancel, on_progress=on_progress)
    except Exception as exc:                         # noqa: BLE001 交调用方记账
        msg = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        log("❌ " + msg)
        return {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
                "errors": [msg], "last_idx": start}
    if not pcm:
        msg = "解不出音频（文件可能不完整）"
        log("❌ " + msg)
        return {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
                "errors": [msg], "last_idx": start}
    title = probe(src).get("title") or os.path.splitext(os.path.basename(src))[0]
    return spc_wav.write_tracks(
        [{"name": title, "pcm": pcm, "little_endian": True}],
        out_dir, max_secs, log, on_progress, cancel, used, start, None, "wav")
