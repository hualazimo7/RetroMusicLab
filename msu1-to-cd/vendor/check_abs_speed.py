# -*- coding: utf-8 -*-
"""绝对速度复核: 用 spc2midi 的 MIDI 音符时间戳对照 WAV 的实际发声长度。

MIDI 的 output_duration 字段来自 ID666(不可信), 但每个 note-on/off 的
delta time 是 spc2midi 按 SPU 采样计数独立推算的, 与 ID666 无关。
把它换算成秒, 与 WAV 包络里"最后一声"的位置比对, 即可判定 libgme 的
渲染速度是否与真实演奏速度一致。
"""
import json
import os
import struct
import sys
import wave

import numpy as np


def read_midi_last_time(path):
    """解析标准 MIDI, 返回 (division, 最后事件对应的 tick, tempo_us)。"""
    with open(path, "rb") as f:
        data = f.read()
    assert data[:4] == b"MThd", "不是 MIDI"
    hlen = struct.unpack(">I", data[4:8])[0]
    fmt, ntrk, div = struct.unpack(">HHH", data[8:10] + data[10:12] + data[12:14])
    pos = 8 + hlen
    last_tick = 0
    tempo = 500000
    for _ in range(ntrk):
        assert data[pos:pos + 4] == b"MTrk"
        tlen = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        p = pos + 8
        end = p + tlen
        tick = 0
        running = None
        while p < end:
            # delta time (varint)
            dt = 0
            while True:
                b = data[p]
                p += 1
                dt = (dt << 7) | (b & 0x7F)
                if not b & 0x80:
                    break
            tick += dt
            status = data[p]
            if status & 0x80:
                p += 1
                running = status
            else:
                status = running
            hi = status & 0xF0
            if status == 0xFF:
                meta = data[p]
                p += 1
                ln = 0
                while True:
                    b = data[p]
                    p += 1
                    ln = (ln << 7) | (b & 0x7F)
                    if not b & 0x80:
                        break
                if meta == 0x51 and ln == 3:
                    tempo = (data[p] << 16) | (data[p + 1] << 8) | data[p + 2]
                p += ln
            elif status in (0xF0, 0xF7):
                ln = 0
                while True:
                    b = data[p]
                    p += 1
                    ln = (ln << 7) | (b & 0x7F)
                    if not b & 0x80:
                        break
                p += ln
            elif hi in (0xC0, 0xD0):
                p += 1
            else:
                p += 2
        last_tick = max(last_tick, tick)
        pos = end
    return div, last_tick, tempo


def wav_active_span(path):
    """返回 (总时长, 最后超过阈值发声的时刻, 首个发声时刻)。"""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    mono = a.reshape(-1, 2).mean(axis=1)
    win = int(sr * 0.02)
    m = len(mono) // win
    env = np.sqrt((mono[:m * win].reshape(m, win) ** 2).mean(axis=1))
    thr = max(1e-4, env.max() * 0.005)
    idx = np.where(env >= thr)[0]
    first = idx[0] * 0.02 if len(idx) else 0.0
    last = idx[-1] * 0.02 if len(idx) else 0.0
    return n / sr, first, last


def main():
    mid = sys.argv[1]
    wav = sys.argv[2]
    div, last_tick, tempo = read_midi_last_time(mid)
    sec_from_midi = last_tick / div * (tempo / 1e6)
    dur, first, last = wav_active_span(wav)
    print("MIDI: division=%d  last_tick=%d  tempo=%dus(%.1f BPM)"
          % (div, last_tick, tempo, 60e6 / tempo))
    print("      最后一个事件 = %.2f 秒" % sec_from_midi)
    print("WAV : 文件时长 = %.2f 秒, 发声区间 %.2f ~ %.2f 秒" % (dur, first, last))
    if last > 0.1:
        ratio = sec_from_midi / last
        print("      比值 MIDI/WAV = %.4f  (>1 表示 WAV 偏慢)" % ratio)
        if 0.97 <= ratio <= 1.03:
            print("      结论: 一致 —— WAV 速度正常")
        else:
            print("      结论: 不一致 —— WAV 速度偏差 %.1f%%" % ((ratio - 1) * 100))


if __name__ == "__main__":
    main()
