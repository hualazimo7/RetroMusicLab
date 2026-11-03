# -*- coding: utf-8 -*-
"""端到端：把 ConvertJob 跑在真实 QSF 包上（2 首），验主流水线。

只写 WAV，不写 CD（BIN+CUE 那条路与解码无关，另跑）。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import convert_job
import spc_wav

D = (r"D:\MSU1Downloads\joshw\QSound 街机（Hoot）"
     r"\Mega Man 2 - The Power Fighters [Rockman 2 - The Power Fighters] "
     r"(QSF)(1996)(Capcom)")

NAMES = ["01 - Opening.miniqsf", "05 - Plant Man.miniqsf"]

out = tempfile.mkdtemp(prefix="qsf_job_")
logs = []
srcs = []
for n in NAMES:
    p = os.path.join(D, n)
    srcs.append({"path": p, "label": os.path.splitext(n)[0],
                 "kind": convert_job.classify(p)})

print("分类：", [(s["label"], s["kind"]) for s in srcs])

opts = {
    "out_dir": out, "gap": 2, "album": "", "performer": "",
    "verify": False, "cd_wav": False,
    "do_cd": False, "do_wav": True, "fmt": "wav",
    "max_secs": 150.0, "xa_music_only": False, "xa_split": False,
}

job = convert_job.ConvertJob(srcs, opts, on_log=logs.append,
                             on_progress=lambda p, t: None,
                             on_track=lambda i, n: None)
res = job.run()

for ln in logs:
    print("   ", ln)
print("结果：", res)

made = sorted(f for f in os.listdir(out) if f.lower().endswith(".wav"))
print("产出：", made)
for f in made:
    p = os.path.join(out, f)
    pcm, sec, ch, rate = spc_wav._read_wav(p) if hasattr(spc_wav, "_read_wav") else (b"", 0, 0, 0)
    print("    %-28s %8.2f 秒  %dch/%d  %d 字节"
          % (f, sec, ch, rate, os.path.getsize(p)))

shutil.rmtree(out, ignore_errors=True)
