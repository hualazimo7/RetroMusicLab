#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_probe_v596_job.py — 绕开 HTTP，直接跑 ConvertJob，让异常原样冒出来。

背景：/api/state 的 state 永远停在 running、job 已经是 None、worker 线程已退出
      —— 说明 work() 里 ``res = job.run()`` 或 ``register_outputs()`` 抛了
      **不是 Exception 子类**的东西（``except Exception`` 接不住），
      finally 把 job 置空后线程就没了。

直接跑一遍，谁抛的、抛的什么，一眼看到。

    py -3.14 vendor/_probe_v596_job.py
"""

import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

SRC = (r"D:\MSU1Downloads\joshw\WonderSwan"
       r"\Card Captor Sakura - Sakura to Fushigi na Clow Card "
       r"(1999-12-02)(SIMS)(Bandai)[WS]")


def main():
    import convert_job
    from web_ui.server import scan_dir_sources

    out = os.path.join(os.environ.get("TEMP") or r"C:\Windows\Temp",
                       "rml_probe_job_out")
    paths, _trunc = scan_dir_sources(SRC)
    print("音源路径：%s" % paths, flush=True)
    srcs = [{"label": os.path.splitext(os.path.basename(p))[0],
             "path": p, "kind": convert_job.classify(p)} for p in paths]
    print("归类：%s" % [(s["label"], s["kind"]) for s in srcs], flush=True)

    opts = {
        "out_dir": out, "do_cd": False, "do_wav": True,
        "max_secs": 150.0, "default_secs": 150.0, "gap": 2, "speed": 0.0,
        "xa_music_only": False, "xa_split": False, "merge_wav": False,
        "verify": False, "cd_wav": False, "album": "", "performer": "",
    }

    seen = []

    def on_log(m):
        seen.append(("log", m))

    def on_prog(p, t):
        seen.append(("prog", p, t))

    def on_track(i, n):
        seen.append(("track", i, n))

    job = convert_job.ConvertJob(srcs, opts, on_log=on_log,
                                 on_progress=on_prog, on_track=on_track)
    t0 = time.time()
    try:
        res = job.run()
        print("run() 正常返回，用时 %.1fs" % (time.time() - t0), flush=True)
        print("结果：%s" % {k: res.get(k) for k in ("ok", "fail", "seconds",
                                                   "cancelled")}, flush=True)
    except BaseException:                                    # noqa: BLE001
        print("!! run() 抛了：", flush=True)
        traceback.print_exc()
        return 1

    print("最后 6 条回调：", flush=True)
    for row in seen[-6:]:
        print("   %r" % (row,), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
