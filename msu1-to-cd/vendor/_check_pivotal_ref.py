"""pivotal_psf.py vs vgmstream：逐样本比对（含分段子流）。

判据和 `.hwas/.swav/.dvi` 那轮一样：**采样率相等、帧数相等、逐样本最大差 0**。

用法：
    python vendor/_check_pivotal_ref.py [--vgmstream D:/MSU1Temp/vgm/vgmstream-cli.exe]
"""

import argparse
import glob
import os
import subprocess
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

JOSHW = r"D:\MSU1Downloads\joshw\GameCube"

#: (相对路径, 子流号) —— 覆盖单流 + 分段 + 两种编码
CASES = [
    ("stream/STREAM__000.psf", 1),
    ("stream/STREAM__001.psf", 1),
    ("stream/STREAM__002.psf", 2),
    ("stream/STREAM__002.psf", 3),
    ("stream/STREAM__004.psf", 5),
    ("stream/STREAM__004.psf", 1),
    ("stream/STREAM__053.psf", 3),
    ("stream/STREAM__096.psf", 5),
    ("stream/STREAM__145.psf", 2),
]


def ref(vgm, src, subsong, out_wav):
    if os.path.exists(out_wav):
        os.remove(out_wav)
    r = subprocess.run([vgm, "-s", str(subsong), "-i", "-o", out_wav, src],
                       capture_output=True, text=True)
    if not os.path.isfile(out_wav):
        raise RuntimeError("vgmstream 失败：%s %s"
                           % (r.stdout[-200:], r.stderr[-200:]))
    with wave.open(out_wav, "rb") as w:
        nch, sw, rate, n = (w.getnchannels(), w.getsampwidth(),
                            w.getframerate(), w.getnframes())
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype="<i2"), rate, nch, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vgmstream", default=r"D:\MSU1Temp\vgm\vgmstream-cli.exe")
    ap.add_argument("--root", default=JOSHW)
    a = ap.parse_args()

    import pivotal_psf

    pkg = glob.glob(os.path.join(a.root, "*", "stream", "STREAM__000.psf"))
    if not pkg:
        print("找不到 GameCube 样本包")
        return 2
    base = os.path.dirname(os.path.dirname(pkg[0]))
    tmp = os.path.join(os.environ.get("TEMP", "."), "_pivotal_ref")
    os.makedirs(tmp, exist_ok=True)

    bad = 0
    for rel, sub in CASES:
        src = os.path.join(base, rel.replace("/", os.sep))
        if not os.path.isfile(src):
            print("  跳过（不存在）%s" % rel)
            continue
        tag = "%-24s #%d" % (os.path.basename(rel), sub)
        try:
            mine, rate, nch = pivotal_psf.decode_any(src, sub)
        except Exception as exc:                      # noqa: BLE001
            print("  ✗ %s  我的解码器报错：%s: %s"
                  % (tag, type(exc).__name__, str(exc)[:90]))
            bad += 1
            continue
        try:
            r, rrate, rnch, rn = ref(a.vgmstream, src, sub,
                                     os.path.join(tmp, "ref.wav"))
        except Exception as exc:                      # noqa: BLE001
            print("  ? %s  vgmstream 打不开：%s" % (tag, str(exc)[:90]))
            continue
        m = min(len(mine), len(r))
        diff = np.abs(mine[:m].astype(np.int64) - r[:m].astype(np.int64))
        ok = (rate == rrate and nch == rnch and len(mine) == len(r)
              and int(diff.max() or 0) == 0)
        print("  %s %s  我的 %d 帧 / vgm %d 帧  %d Hz %dch  最大差 %d  不一致 %d"
              % ("✓" if ok else "✗", tag, len(mine) // max(1, nch),
                 rn, rate, nch, int(diff.max() or 0) if m else -1,
                 int((diff > 0).sum())))
        if not ok:
            bad += 1
    print("\n%s" % ("全部一致 ✓" if not bad else "有 %d 项不一致 ✗" % bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
