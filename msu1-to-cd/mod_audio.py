"""ProTracker ``.MOD`` 回放 —— 纯 Python，不需要任何主机模拟器。

为什么单独一个模块
==================
``.MOD`` 既不是「某个主机的音频流」（像 ``.adx``），也不是「某个芯片的寄存器
日志」（像 ``.spc``），它是一份**乐谱 + 采样**：

    偏移 0      20 字节   标题
    偏移 20     30×31     31 个采样头（22 字节名字 + 2 字节长度/字 + 微调
                          + 音量 + 2 字节循环起点/字 + 2 字节循环长度/字）
    偏移 950     1 字节   乐曲长度（order 表用到几项）
    偏移 951     1 字节   循环回哪个 order
    偏移 952   128 字节   order 表（每个字节 = 一个模式号）
    偏移 1080    4 字节   魔数（``M.K.`` / ``M!K!`` / ``4CHN`` / ``8CHN`` …）
    偏移 1084  1024×N     模式数据（每行每声道 4 字节）
    之后                 采样数据（8bit **有符号** PCM，长度按「字」记）

要出声只能自己当回放器：按 tick 推进序列、按效果改音高和音量、按 Amiga 周期
表算播放速率、把 4 个声道混成一路。好在这些都是纯算术，跟 CPU 模拟无关 ——
这就是 8 个机种里除 ADX / HWAS 之外**唯一纯数据就能还原**的格式。

实测（``D:\\MSU1Downloads\\joshw\\Game Boy Advance`` 下 007 夜火的 10 首）：
10 个文件按 ``1084 + 模式数×1024 + Σ采样长度×2`` 算出来与文件大小**逐个相等**，
说明是零压缩的标准 MOD，没有厂商私货。

音高换算
========
Amiga PAL 时钟 7093789.2 Hz，周期表 ``period`` 与实际频率的关系是

    freq = 7093789.2 / (2 × period)

period=428（C-2）→ 8287 Hz，正是 ProTracker 的标准值。微调（finetune）以
1/8 半音为单位，所以频率再乘 ``2 ** (finetune / 96)``。

两个容易写错的点
================
1. **颤音是「临时偏移」不是「累积」**。真实回放器每 tick 算
   ``period + 波形偏移`` 来取音高，基准 period 不动。写成 ``period += 偏移``
   会让音高一路飘走（第一版就是这么错的，谱峰全跑到 0 Hz 附近）。
2. **琶音要按 ``tick % 3`` 轮转**：0→原音、1→+高音、2→+低音（参数高半字节
   在前）。写成「一直加同一个偏移」就变成一个固定和弦音，听不出琶音。
"""

import os
from typing import Callable, Optional

import numpy as np

OUT_RATE = 44100

#: 认得出来的魔数。前面几个是 4 声道，后面几个是 8 声道。
MAGICS = (b"M.K.", b"M!K!", b"M&K!", b"4CHN",
          b"FLT4", b"8CHN", b"OKTA", b"OCTA", b"CD81", b"FLT8")
_EIGHT_CH = (b"8CHN", b"OKTA", b"OCTA", b"CD81", b"FLT8", b"FLT4")

#: ProTracker 周期表，C-1 起 8 个八度共 96 个音。低三个八度是原始表，往上
#: 是逐级折半（老 tracker 的扩展方式，不是重新算的等分平均律）。
PERIODS = (
    1712, 1616, 1524, 1440, 1356, 1280, 1208, 1140, 1076, 1016, 960, 907,
    856, 808, 762, 720, 678, 640, 604, 570, 538, 508, 480, 453,
    428, 404, 381, 360, 339, 320, 302, 285, 269, 254, 240, 226,
    214, 202, 190, 180, 170, 160, 151, 143, 135, 127, 120, 113,
    107, 101, 95, 90, 85, 80, 75, 71, 67, 63, 60, 56,
    53, 50, 47, 45, 42, 40, 37, 35, 33, 31, 30, 28,
    26, 25, 23, 22, 21, 20, 18, 17, 16, 15, 15, 14,
    13, 12, 11, 11, 10, 10, 9, 8, 8, 7, 7, 6,
)
_PERIOD_NOTE = {p: i + 1 for i, p in enumerate(PERIODS)}
_CLOCK = 7093789.2

#: 一首曲子最长回放这么久（秒）。防的是「坏文件里 Bxx 无限跳回开头」把转换
#: 卡死 —— 真曲子不会有 20 分钟的单趟。
MAX_PLAY_SECS = 20 * 60

#: 混音时的总增益。8bit 采样（-128..127）× 音量（0..64）× 4 声道，理论峰值
#: 4×127×64 = 32512，正好落在 int16 里不用再缩放，也就不会因为缩放丢低位。
_GAIN = 1.0


def _is_mod_magic(raw: bytes) -> bool:
    return len(raw) >= 1084 and raw[1080:1084] in MAGICS


def is_mod(path) -> bool:
    """按魔数判断，**不看扩展名** —— ``.MOD`` 在不同来源里大小写混乱。"""
    try:
        with open(path, "rb") as f:
            return _is_mod_magic(f.read(1084))
    except OSError:
        return False


class _Sample(object):
    __slots__ = ("name", "data", "vol", "finetune", "loop_start", "loop_len")

    def __init__(self):
        self.name = ""
        self.data = np.zeros(0, dtype=np.float64)
        self.vol = 64
        self.finetune = 0
        self.loop_start = 0
        self.loop_len = 0

    @property
    def length(self):
        return self.data.size

    @property
    def loops(self):
        # ProTracker 的约定：循环长度 <= 2 视为不循环（1 字是「无循环」占位）
        return self.loop_len > 2 and self.loop_start + 2 <= self.length


class Song(object):
    def __init__(self):
        self.title = ""
        self.nch = 4
        self.song_len = 1
        self.restart = 0
        self.orders = []
        self.patterns = 0
        self.samples = []
        self.pat_raw = b""


def parse(path) -> Song:
    """读一个 ``.MOD``。格式不对就抛 ``ValueError``（调用方负责转成日志）。"""
    with open(path, "rb") as f:
        raw = f.read()
    if not _is_mod_magic(raw):
        raise ValueError("不是 MOD（魔数不对）")

    s = Song()
    magic = raw[1080:1084]
    s.nch = 8 if magic in _EIGHT_CH else 4
    s.title = raw[:20].split(b"\x00")[0].decode("latin-1", "replace").strip()

    heads = []
    for i in range(31):
        o = 20 + i * 30
        name = raw[o:o + 22].split(b"\x00")[0].decode("latin-1", "replace")
        length = (raw[o + 22] << 8) | raw[o + 23]        # 单位：字
        finetune = raw[o + 24]
        if finetune > 7:                                  # 有符号 4bit
            finetune -= 16
        vol = min(64, raw[o + 25])
        rep_start = (raw[o + 26] << 8) | raw[o + 27]      # 单位：字
        rep_len = (raw[o + 28] << 8) | raw[o + 29]        # 单位：字
        heads.append((name, length, finetune, vol, rep_start, rep_len))

    s.song_len = raw[950] or 1
    s.restart = raw[951]
    s.orders = list(raw[952:952 + 128])
    s.patterns = max(s.orders[:s.song_len] or [0]) + 1

    pat_end = 1084 + s.patterns * 1024
    off = pat_end
    for name, length, finetune, vol, rep_start, rep_len in heads:
        nbytes = length * 2
        chunk = raw[off:off + nbytes]
        off += nbytes
        smp = _Sample()
        smp.name = name
        smp.vol = vol
        smp.finetune = finetune
        if chunk:
            # 8bit **有符号**。长度字段是字数，所以 chunk 长度就是样本数。
            smp.data = np.frombuffer(chunk, dtype=np.int8).astype(np.float64)
        if rep_len > 1:
            smp.loop_start = rep_start * 2
            smp.loop_len = rep_len * 2
            if smp.loop_start + smp.loop_len > smp.length:
                smp.loop_len = max(0, smp.length - smp.loop_start)
        s.samples.append(smp)

    # 模式数据整体留着，回放时按 (模式, 行, 声道) 定位，省得反复切片
    s.pat_raw = raw[1084:pat_end]
    if len(s.pat_raw) < s.patterns * 1024:
        raise ValueError("模式数据被截断")
    return s


class _Chan(object):
    __slots__ = ("smp", "period", "note", "vol", "pos", "ended",
                 "target", "vib_pos", "vib_spd", "vib_dep", "vib_wave",
                 "vib_off", "trem_pos", "trem_spd", "trem_dep", "trem_wave",
                 "fx", "param", "mem", "period_eff",
                 "pend_note", "pend_smp", "pend_tick",
                 "loop_row", "loop_cnt")

    def __init__(self):
        self.smp = None
        self.period = 0
        self.note = 0
        self.vol = 0
        self.pos = 0.0
        self.ended = True
        self.target = 0
        self.vib_pos = 0
        self.vib_spd = 0
        self.vib_dep = 0
        self.vib_wave = 0
        self.vib_off = 0
        self.trem_pos = 0
        self.trem_spd = 0
        self.trem_dep = 0
        self.trem_wave = 0
        self.fx = 0
        self.param = 0
        self.mem = 0
        self.period_eff = 0
        self.pend_note = 0
        self.pend_smp = 0
        self.pend_tick = -1
        self.loop_row = -1
        self.loop_cnt = 0


def _sine_table():
    """ProTracker 的颤音/音量颤音用 64 项正弦表（幅度 0..255，居中 128）。"""
    return np.array([int(round(255.0 * np.sin(2 * np.pi * i / 64.0)))
                     for i in range(64)], dtype=np.int32)


_SINE = _sine_table()
_RAMP = np.array(list(range(0, 128, 4)) + list(range(-128, 0, 4)),
                 dtype=np.int32)
_SQUARE = np.array([255] * 32 + [0] * 32, dtype=np.int32)
_WAVES = (_SINE, _RAMP, _SQUARE)


def _wave(kind, pos):
    return int(_WAVES[kind % 3][pos % 64])


def _note_period(note):
    if 1 <= note <= len(PERIODS):
        return PERIODS[note - 1]
    return 0


def _step_of(period, finetune, rate):
    """Amiga 周期 -> 每输出一个样本要前进多少个采样点。"""
    if period <= 0:
        return 0.0
    freq = _CLOCK / (2.0 * period) * (2.0 ** (finetune / 96.0))
    return freq / float(rate)


def _slide_toward(cur, target, amount):
    if target <= 0:
        return cur
    if cur < target:
        cur += amount
        if cur > target:
            cur = target
    elif cur > target:
        cur -= amount
        if cur < target:
            cur = target
    return cur


def _vol_slide(ch, param, tick):
    """Axy / 5xy / 6xy 共用的音量滑音：高半字节上、低半字节下。"""
    if not tick:
        return
    hi, lo = (param >> 4) & 0x0F, param & 0x0F
    if hi:
        ch.vol = min(64, ch.vol + hi)
    elif lo:
        ch.vol = max(0, ch.vol - lo)


def _do_effect(ch, tick, state):
    """处理当前行的效果。``tick == 0`` 是行首，其余是后续 tick。

    参数为 0 时沿用上一次的值（``mem``），这是 ProTracker 的既定行为，
    漏掉会让音量滑音、滑音到目标音这类效果直接哑掉。
    """
    fx, param = ch.fx, ch.param
    if fx == 0x0:                                   # 琶音（在 _tick_period 里算）
        pass
    elif fx == 0x1:                                 # 上滑音
        if param:
            ch.mem = param
        if tick:
            ch.period = max(113, ch.period - ch.mem)
    elif fx == 0x2:                                 # 下滑音
        if param:
            ch.mem = param
        if tick:
            ch.period = min(1712, ch.period + ch.mem)
    elif fx == 0x3:                                 # 滑音到目标音
        if param:
            ch.mem = param
        if tick and ch.target:
            ch.period = _slide_toward(ch.period, ch.target, ch.mem)
    elif fx == 0x4:                                 # 颤音
        if param:
            ch.vib_spd = (param >> 4) & 0x0F
            ch.vib_dep = param & 0x0F
        if tick:
            ch.vib_off = (_wave(ch.vib_wave, ch.vib_pos) - 128) * ch.vib_dep // 32
            ch.vib_pos = (ch.vib_pos + ch.vib_spd) % 64
    elif fx == 0x5:                                 # 滑音到目标音 + 音量滑音
        if param:
            ch.mem = param
        if tick and ch.target:
            ch.period = _slide_toward(ch.period, ch.target, ch.mem)
        _vol_slide(ch, param, tick)
    elif fx == 0x6:                                 # 颤音 + 音量滑音
        if param:
            ch.vib_spd = (param >> 4) & 0x0F
            ch.vib_dep = param & 0x0F
        if tick:
            ch.vib_off = (_wave(ch.vib_wave, ch.vib_pos) - 128) * ch.vib_dep // 32
            ch.vib_pos = (ch.vib_pos + ch.vib_spd) % 64
        _vol_slide(ch, param, tick)
    elif fx == 0x7:                                 # 音量颤音
        if param:
            ch.trem_spd = (param >> 4) & 0x0F
            ch.trem_dep = param & 0x0F
        if tick:
            ch.trem_pos = (ch.trem_pos + ch.trem_spd) % 64
    elif fx == 0x9:                                 # 采样起点（单位 256 字节）
        if param:
            ch.mem = param
        if tick == 0:
            ch.pos = float(ch.mem * 256)
    elif fx == 0xA:                                 # 音量滑音
        if param:
            ch.mem = param
        _vol_slide(ch, ch.mem, tick)
    elif fx == 0xB:                                 # 跳 order
        if tick == 0:
            state["jump"] = param
    elif fx == 0xC:                                 # 设音量
        if tick == 0:
            ch.vol = min(64, param)
    elif fx == 0xD:                                 # 断行（参数是 BCD 行号）
        if tick == 0:
            state["break"] = (param >> 4) * 10 + (param & 0x0F)
    elif fx == 0xE:                                 # 扩展
        sub, val = (param >> 4) & 0x0F, param & 0x0F
        if sub == 0x1:                              # 细上滑（行首一次性）
            if tick == 0:
                ch.period = max(113, ch.period - val)
        elif sub == 0x2:                            # 细下滑
            if tick == 0:
                ch.period = min(1712, ch.period + val)
        elif sub == 0x4:                            # 设颤音波形
            if tick == 0:
                ch.vib_wave = val & 0x03
        elif sub == 0x6:                            # 行循环
            if tick == 0:
                if val == 0:
                    if ch.loop_row < 0:
                        ch.loop_row = state["row"]
                        ch.loop_cnt = 0
                elif ch.loop_row >= 0:
                    if ch.loop_cnt < val:
                        ch.loop_cnt += 1
                        state["loop_to"] = ch.loop_row
                    else:
                        ch.loop_row = -1
                        ch.loop_cnt = 0
        elif sub == 0xA:                            # 细音量上
            if tick == 0:
                ch.vol = min(64, ch.vol + val)
        elif sub == 0xB:                            # 细音量下
            if tick == 0:
                ch.vol = max(0, ch.vol - val)
        elif sub == 0xC:                            # 到第 val tick 切音
            if tick == val:
                ch.vol = 0
        elif sub == 0xD:                            # 到第 val tick 才起音
            pass                                    # 行首已经登记，见 _run
    elif fx == 0xF:                                 # 设速度 / BPM
        if tick == 0:
            if param and param < 0x20:
                state["speed"] = param
            elif param >= 0x20:
                state["bpm"] = param


def _tick_period(ch, tick):
    """这一 tick 实际用的周期：基准周期 + 颤音偏移，琶音时按 tick%3 轮转。"""
    if ch.fx == 0x0 and ch.param:
        phase = tick % 3
        if phase == 0:
            p = ch.period
        else:
            off = (ch.param & 0x0F) if phase == 2 else ((ch.param >> 4) & 0x0F)
            p = _note_period(ch.note + off) or ch.period
        return p
    return ch.period + ch.vib_off


def _start_note(song, ch, smp_idx, note):
    """起一个音：换采样、设初始周期、复位播放位置。"""
    if smp_idx:
        s = song.samples[smp_idx - 1]
        ch.smp = s
        ch.vol = s.vol
        ch.pos = 0.0
        ch.ended = s.length == 0
        ch.trem_pos = 0
        ch.vib_pos = 0
        ch.vib_off = 0
    if note:
        ch.note = note
        ch.period = _note_period(note)
        ch.target = ch.period
        if ch.smp is not None:
            ch.ended = False
    ch.period_eff = ch.period


def _mix(chans, n, rate, buf):
    """把这一 tick 的所有声道混进 ``buf``（长度 n，float64）。"""
    for ch in chans:
        smp = ch.smp
        if smp is None or ch.ended or smp.length == 0:
            continue
        step = _step_of(ch.period_eff, smp.finetune, rate)
        if step <= 0:
            continue

        vol = ch.vol
        if ch.trem_dep:
            vol = vol + (_wave(ch.trem_wave, ch.trem_pos) - 128) * ch.trem_dep // 64
            vol = 0 if vol < 0 else (64 if vol > 64 else vol)

        looping = smp.loops
        ls, ll = smp.loop_start, smp.loop_len
        le = ls + ll
        total = smp.length

        if vol <= 0:
            # 音量为 0 不出声，但播放位置照样要走（否则后面接回来会错位）
            ch.pos += step * n
            if looping:
                if ch.pos >= le:
                    ch.pos = ls + (ch.pos - le) % ll
            elif ch.pos >= total:
                ch.ended = True
            continue

        idx = ch.pos + step * np.arange(n, dtype=np.float64)
        if looping:
            idx = np.where(idx >= le, ls + np.mod(idx - le, ll), idx)
            ch.pos = ch.pos + step * n
            if ch.pos >= le:
                ch.pos = ls + (ch.pos - le) % ll
            ok = n
        else:
            ch.pos = ch.pos + step * n
            ok = int(np.searchsorted(idx, total, side="left"))
            if ok <= 0:
                ch.ended = True
                continue
            if ok < n:
                idx = idx[:ok]
                ch.ended = True

        i0 = idx.astype(np.int32)
        np.clip(i0, 0, total - 1, out=i0)
        frac = idx - i0
        i1 = np.minimum(i0 + 1, total - 1)
        data = smp.data
        buf[:ok] += (data[i0] * (1.0 - frac) + data[i1] * frac) * vol


def _run(song, rate, out=None, max_secs=0.0, cancel=None,
         on_progress: Optional[Callable] = None):
    """回放。``out`` 为 ``None`` 时只走序列、不混音（用来算时长）。

    返回实际生成的样本帧数（单声道帧数）。
    """
    nch = song.nch
    chans = [_Chan() for _ in range(nch)]
    order = 0
    row = 0
    speed = 6
    bpm = 125
    state = {"row": 0, "speed": speed, "bpm": bpm,
             "jump": -1, "break": -1, "loop_to": -1}
    limit = int((max_secs if max_secs > 0 else MAX_PLAY_SECS) * rate)
    written = 0
    last_report = 0

    while 0 <= order < min(song.song_len, 128):
        pat = song.orders[order]
        if pat >= song.patterns:
            break
        state["row"] = row
        state["jump"] = -1
        state["break"] = -1
        state["loop_to"] = -1

        # ---- 行首：读每个声道的音符与效果 ----
        for c in range(nch):
            o = pat * 1024 + (row * nch + c) * 4
            b0 = song.pat_raw[o]
            b1 = song.pat_raw[o + 1]
            b2 = song.pat_raw[o + 2]
            b3 = song.pat_raw[o + 3]
            period = ((b0 & 0x0F) << 8) | b1
            smp_idx = (b0 & 0xF0) | (b2 >> 4)
            fx = b2 & 0x0F
            param = b3
            ch = chans[c]

            ch.fx, ch.param = fx, param
            note = _PERIOD_NOTE.get(period, 0) if period else 0

            # EDx：到第 x tick 才起音，这里只登记，真正起音在 tick 循环里
            if fx == 0xE and ((param >> 4) & 0x0F) == 0x0D and (param & 0x0F):
                ch.pend_note, ch.pend_smp = note, smp_idx
                ch.pend_tick = param & 0x0F
            elif period or smp_idx:
                _start_note(song, ch, smp_idx, note)

            # 3xx 的音符是「目标」，不是立刻起音
            if fx == 0x3 and note:
                ch.target = _note_period(note)
                ch.note = note
                if ch.period <= 0:
                    ch.period = ch.target
                ch.period_eff = ch.period
            ch.vib_off = 0

        # ---- 逐 tick 推进 ----
        tick = 0
        while tick < speed:
            if cancel is not None and cancel.is_set():
                return written
            n = int(round(rate * 2.5 / max(1, bpm)))
            if n < 1:
                n = 1
            if written + n > limit:
                n = limit - written
                if n <= 0:
                    return written

            for c in range(nch):
                ch = chans[c]
                _do_effect(ch, tick, state)
                ch.period_eff = _tick_period(ch, tick)
                if ch.pend_tick == tick:
                    _start_note(song, ch, ch.pend_smp, ch.pend_note)
                    ch.pend_tick = -1
                    ch.period_eff = ch.period

            if out is not None:
                buf = np.zeros(n, dtype=np.float64)
                _mix(chans, n, rate, buf)
                out[written:written + n] = np.clip(buf, -32768, 32767)
            written += n

            if on_progress is not None and written - last_report >= rate * 5:
                last_report = written
                on_progress(written / float(rate))

            speed = state["speed"]
            bpm = state["bpm"]
            tick += 1

        # ---- 行结束：跳转 / 断行 / 行循环 ----
        if state["loop_to"] >= 0:
            row = state["loop_to"]
            continue
        if state["jump"] >= 0:
            order = state["jump"]
            row = state["break"] if state["break"] >= 0 else 0
            continue
        if state["break"] >= 0:
            row = state["break"]
            order += 1
            continue
        row += 1
        if row >= 64:
            row = 0
            order += 1

    return written


def probe(path) -> dict:
    """只走序列算时长，不混音。给下载页的「预计时长」用。"""
    s = parse(path)
    frames = _run(s, OUT_RATE, out=None)
    return {"secs": frames / float(OUT_RATE), "rate": OUT_RATE,
            "channels": 2, "title": s.title, "samples": len(s.samples),
            "patterns": s.patterns, "orders": s.song_len}


def render(path, rate=OUT_RATE, max_secs=0.0, cancel=None,
           on_progress: Optional[Callable] = None):
    """回放成 int16 单声道数组（调用方再上混/重采样）。"""
    s = parse(path)
    frames = _run(s, rate, out=None, max_secs=max_secs)
    pcm = np.zeros(frames, dtype=np.int16)
    _run(s, rate, out=pcm, max_secs=max_secs, cancel=cancel,
         on_progress=on_progress)
    return pcm, rate


if __name__ == "__main__":                              # 手动试听用
    import sys
    import wave
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else (os.path.splitext(src)[0] + ".wav")
    pcm, r = render(src)
    st = np.repeat(pcm.reshape(-1, 1), 2, axis=1)
    with wave.open(dst, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(r)
        w.writeframes(st.tobytes())
    print("%s -> %s  %.1f 秒" % (src, dst, pcm.size / float(r)))
