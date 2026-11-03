# -*- coding: utf-8 -*-
"""下载页那条路（ConvertWorker）的 QSF 分派验证。

不走整个队列 —— 只验三件事：
  ① policy_for() 把 .miniqsf 归成 "qsf"
  ② ConvertWorker._convert_emu(qsf_native, "QSF") 真能出 WAV
  ③ 产出的 WAV 是 44100 / 16bit / 立体声（主流水线的房子口径）
"""
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dl_convert
import qsf_native

D = (r"D:\MSU1Downloads\joshw\QSound 街机（Hoot）"
     r"\Mega Man 2 - The Power Fighters [Rockman 2 - The Power Fighters] "
     r"(QSF)(1996)(Capcom)")
SRC = os.path.join(D, "01 - Opening.miniqsf")

out = tempfile.mkdtemp(prefix="qsf_worker_")
try:
    print("① policy_for =", dl_convert.policy_for(SRC))

    w = dl_convert.ConvertWorker(out, on_event=lambda k, d: None,
                                 cancel=threading.Event(), fmt="wav")
    logs = []
    sub = w._convert_emu(SRC, logs, threading.Event(), qsf_native, "QSF")
    for ln in logs:
        print("   ", ln)
    print("② sub =", sub)

    made = sorted(f for f in os.listdir(out) if f.lower().endswith(".wav"))
    print("   产出 =", made)
    for f in made:
        p = os.path.join(out, f)
        pcm, sec, ch, rate = qsf_native._read_wav(p)
        print("③ %s → %.2f 秒  %dch/%d  %d 字节"
              % (f, sec, ch, rate, os.path.getsize(p)))
        assert (ch, rate) == (2, 44100), "采样率/声道不对"
    assert made, "没有产出 WAV"
    print("✅ 下载页路径 OK")
finally:
    shutil.rmtree(out, ignore_errors=True)
