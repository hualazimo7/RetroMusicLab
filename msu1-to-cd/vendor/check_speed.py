# -*- coding: utf-8 -*-
"""独立复核: 同一 SPC 在不同渲染采样率下, 音乐内容的时间轴是否一致。

方法: 以两个不同的 rate 各渲染 10 秒真实音频, 计算 10ms RMS 包络,
再用互相关求两者的时间偏移。若 libgme 的 rate 参数处理正确, 两条包络
应严格对齐(偏移 ≈ 0); 若存在"变速", 就会给出明确的拉伸倍数。
"""
import ctypes
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import msu1_to_cd as core


def render(path, rate, secs=10.0):
    gme = core._load_gme()
    emu = ctypes.c_void_p()
    err = gme.gme_open_file(os.path.abspath(path).encode("utf-8"),
                            ctypes.byref(emu), rate)
    if err:
        raise RuntimeError(err.decode("latin-1", "replace"))
    gme.gme_start_track(emu, 0)
    chunk = 4096
    buf = (ctypes.c_short * (chunk * 2))()
    out = []
    total = int(rate * secs)
    got = 0
    while got < total:
        n = min(chunk, total - got)
        e = gme.gme_play(emu, n, buf)
        if e:
            raise RuntimeError(e.decode("latin-1", "replace"))
        out.append(np.ctypeslib.as_array(buf, shape=(n * 2,)).copy())
        got += n
    gme.gme_delete(emu)
    pcm = np.concatenate(out).astype(np.float64) / 32768.0
    mono = pcm.reshape(-1, 2).mean(axis=1)
    return mono


def envelope(x, rate, win_ms=10.0):
    w = int(rate * win_ms / 1000.0)
    n = len(x) // w
    return np.sqrt((x[:n * w].reshape(n, w) ** 2).mean(axis=1))


def best_lag(a, b, max_lag=300):
    """互相关求 b 相对 a 的偏移(以 10ms 窗为单位)。正= b 落后。"""
    a = a - a.mean()
    b = b - b.mean()
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    best, best_lag_v = -1e18, 0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x, y = a[lag:], b[:n - lag] if lag else b
        else:
            x, y = a[:n + lag], b[-lag:]
        m = min(len(x), len(y))
        if m < 50:
            continue
        c = float(np.dot(x[:m], y[:m]))
        if c > best:
            best, best_lag_v = c, lag
    return best_lag_v


def main():
    spc = sys.argv[1] if len(sys.argv) > 1 else \
        r"C:\Users\MY-XQH\Desktop\Chrono Trigger (EMU).zophar\101 Presentiment.spc"
    print("源:", os.path.basename(spc))
    ref_rate = 44100
    ref = render(spc, ref_rate)
    e_ref = envelope(ref, ref_rate)
    print(f"  基准 rate={ref_rate}  包络窗数={len(e_ref)} (每窗 10ms)")

    for rate in (32000, 22050, 48000):
        x = render(spc, rate)
        e = envelope(x, rate)
        lag = best_lag(e_ref, e)
        # 若内容被拉伸 f 倍, 则第 k 个 10ms 窗应偏移 (f-1)*k;
        # 用中段窗口估计实际拉伸比例
        idx = np.arange(len(e_ref))
        # 粗略: 用包络的累积能量做时间规整, 估计缩放
        c_ref = np.cumsum(e_ref)
        c_x = np.cumsum(e[:len(e_ref)])
        c_ref /= c_ref[-1]
        c_x /= c_x[-1]
        # 找到 x 的 90% 能量位置 在 ref 上的对应位置
        def pos_at(curve, frac):
            return int(np.searchsorted(curve, frac))
        f90 = pos_at(c_x, 0.90) / max(1, pos_at(c_ref, 0.90))
        print(f"  rate={rate:>5}  互相关偏移={lag * 10:>6.0f} ms   "
              f"90%能量位置比={f90:.4f}  ({len(x) / rate:.2f}s)")


if __name__ == "__main__":
    main()
