# -*- coding: utf-8 -*-
"""vendor/_probe_ogg_crash.py — 定位 libsndfile OGG 写入崩溃边界（临时诊断）。

现象：sf.write 一次性写 >10 秒的 OGG 会栈溢出（0xC00000FD），进程直接消失。
本脚本分别测「一次性写」和「分块写」，找出边界与规避方式。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

CHILD = os.path.join(HERE, "_probe_ogg_child.py")

CHILD_SRC = r'''
import os, sys
import numpy as np
import soundfile as sf

secs = float(sys.argv[1])
mode = sys.argv[2]
level = float(sys.argv[3])
n = int(secs * 44100)
t = np.arange(n) / 44100.0
d = (np.sin(t * 2 * np.pi * 440) * 8000).astype(np.int16)
d = np.stack([d, d], 1).astype(np.float32) / 32768.0
o = os.path.join(os.environ["TEMP"], "probe_ogg_%s_%s.ogg" % (mode, secs))
if mode == "one":
    sf.write(o, d, 44100, format="OGG", subtype="VORBIS", compression_level=level)
else:
    block = 44100 * 5
    with sf.SoundFile(o, "w", 44100, 2, subtype="VORBIS", format="OGG",
                      compression_level=level) as f:
        for i in range(0, n, block):
            f.write(d[i:i + block])
print("OK", os.path.getsize(o))
'''


def main():
    with open(CHILD, "w", encoding="utf-8") as f:
        f.write(CHILD_SRC)

    import soundfile as sf
    print("libsndfile %s / soundfile %s"
          % (sf.__libsndfile_version__, sf.__version__))

    print("\n[A] 一次性写（sf.write）")
    for secs in (8, 10, 12, 14, 16, 18, 20):
        r = subprocess.run([sys.executable, CHILD, str(secs), "one", "0.0"],
                           capture_output=True, text=True)
        print("  %5.0fs  rc=%-12d %s" % (secs, r.returncode,
                                         (r.stdout or r.stderr).strip()[-40:]))

    print("\n[B] 分块写（SoundFile.write，5 秒一块）")
    for secs in (20, 60, 150, 300):
        r = subprocess.run([sys.executable, CHILD, str(secs), "blk", "0.0"],
                           capture_output=True, text=True)
        print("  %5.0fs  rc=%-12d %s" % (secs, r.returncode,
                                         (r.stdout or r.stderr).strip()[-40:]))
    tmp = os.environ.get("TEMP", ".")
    for p in os.listdir(tmp):
        if p.startswith("probe_ogg_"):
            try:
                os.remove(os.path.join(tmp, p))
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
