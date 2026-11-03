"""六个新机种的「现成方案」端到端验证。

不是只看 `av.open()` 能不能开，而是**真解完、真重采样、真落盘**，
再检查产物是不是正常音频（时长 / RMS / 声道 / 有没有静音或削顶）。

PS3 / PSP / Wii / 3DS 走 PyAV（FFmpeg），GC 走 vgmstream 对照。
"""

import glob
import os
import wave

import numpy as np

JOSHW = r"D:\MSU1Downloads\joshw"

SAMPLES = [
    ("PS3", "PlayStation 3", "*.XVAG"),
    ("PS3", "PlayStation 3", "*.AT3"),
    ("PSP", "PSP", "*.hca"),
    ("Wii", "Wii", "*.brstm"),
    ("3DS", "Nintendo 3DS", "*.laac"),
]


def convert_pyav(src, out_wav):
    """PyAV：解码 → s16 立体声 → 44100，写 WAV。"""
    import av
    parts = []
    rate = 0
    with av.open(src) as c:
        if not c.streams.audio:
            raise RuntimeError("没有音频流")
        st = c.streams.audio[0]
        rate = st.rate or c.streams.audio[0].codec_context.sample_rate
        res = av.AudioResampler(format="s16", layout="stereo", rate=44100)
        for fr in c.decode(st):
            for rf in res.resample(fr):
                parts.append(bytes(rf.planes[0])[:rf.samples * 4])
        for rf in res.resample(None):
            parts.append(bytes(rf.planes[0])[:rf.samples * 4])
    raw = b"".join(parts)
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(raw)
    return raw, rate


def check(wav_path):
    with wave.open(wav_path, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if a.size == 0:
        return "空"
    seg = max(1, a.size // 8)
    rms = [float(np.sqrt((a[i * seg:(i + 1) * seg] ** 2).mean())) * 100
           for i in range(8)]
    return ("%6.2fs  RMS %5.2f%%  分段 %s  削顶 %.3f%%"
            % (n / 44100.0, float(np.sqrt((a ** 2).mean())) * 100,
               " ".join("%.1f" % x for x in rms),
               float((np.abs(a) >= 0.9999).mean()) * 100))


def main():
    out = os.path.join(os.environ.get("TEMP", "."), "_new6_check")
    os.makedirs(out, exist_ok=True)
    for tag, sub, pat in SAMPLES:
        files = sorted(glob.glob(os.path.join(JOSHW, sub, "*", pat)))
        if not files:
            print("%-4s %-10s 没有样本" % (tag, pat))
            continue
        print("%-4s %-10s 共 %d 个，逐个转："
              % (tag, pat, len(files)))
        ok = bad = 0
        for f in files:
            w = os.path.join(out, "%s_%s.wav"
                             % (tag, os.path.basename(f).replace(".", "_")))
            try:
                _, rate = convert_pyav(f, w)
                print("   ✓ %-34s %s  (源 %d Hz)"
                      % (os.path.basename(f)[:34], check(w), rate))
                ok += 1
            except Exception as exc:                  # noqa: BLE001
                print("   ✗ %-34s %s: %s"
                      % (os.path.basename(f)[:34],
                         type(exc).__name__, str(exc)[:90]))
                bad += 1
        print("   → 成功 %d / 失败 %d\n" % (ok, bad))


if __name__ == "__main__":
    main()
