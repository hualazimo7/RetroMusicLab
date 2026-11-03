#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进度条校验: 用真实 SPC 跑一遍 ConvertWorker, 检查 progress 消息。

断言重点 (旧版就是在这里失效的: 只有一个音源时进度永远 0%):
  1. progress 消息条数 >= 曲目数的一半 (真的在动, 不是只在音源切换时动一次)
  2. pct 单调不减, 且封顶 99.9 (100 由界面在 done 时给)
  3. pct 覆盖 0 -> >90 的全程, 且中途有 3 个以上采样落在 5..95 之间
  4. 服务端算出的剩余时间存在且为正 (数据够多之后)

用法: python vendor/test_progress.py <SPC 文件夹> [--cd]
"""
import os
import queue
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import msu1_gui as gui          # noqa: E402
import spc_wav                  # noqa: E402


def collect_pct(msgs):
    return [(m[1], m[3], m[4]) for m in msgs if m[0] == "progress"]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_cd = "--cd" in sys.argv
    if not args:
        print("用法: python vendor/test_progress.py <SPC 文件夹> [--cd]")
        return 2
    src = os.path.abspath(args[0])
    files = spc_wav.collect_spc([src])
    if not files:
        print("没有 .spc")
        return 2
    files = files[:10]                      # 10 首足够看趋势, 也跑得快

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_prog_out")
    shutil.rmtree(out, ignore_errors=True)
    # 子集音源目录: 只放这 10 首, 保证界面看到的曲目数 = 实际处理数
    sel = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_prog_src")
    shutil.rmtree(sel, ignore_errors=True)
    os.makedirs(sel)
    for f in files:
        shutil.copy2(f, os.path.join(sel, os.path.basename(f)))

    # 按界面同样的方式算预估: 一遍时长之和, 再乘遍数
    # (只出 WAV = 1 遍; 要 CD = 解码 1 遍 + 写 BIN 1 遍, 再加 WAV 就 3 遍)
    secs = sum((spc_wav.probe(f)[2] or 0.0) for f in files)
    passes = 1 + (2 if do_cd else 0)
    opts = {
        "do_cd": do_cd, "do_wav": True, "out_dir": out,
        "album": "", "performer": "", "gap": 2.0,
        "verify": False, "cd_wav": False, "policy": "id666",
        "speed": 1.0, "max_secs": 0.0, "total_tracks": len(files),
        "est_secs": secs * passes,
    }
    q = queue.Queue()
    ev = threading.Event()
    w = gui.ConvertWorker([{"label": os.path.basename(src), "path": sel,
                            "kind": "SPC"}], opts, q, ev)
    t0 = time.time()
    w.start()
    w.join()
    wall = time.time() - t0

    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    pts = collect_pct(msgs)
    pcts = [p[0] for p in pts]
    etas = [p[2] for p in pts]

    problems = []
    if len(pts) < len(files) // 2:
        problems.append(f"progress 只有 {len(pts)} 条, 少于曲目数 {len(files)}/2")
    if any(b < a - 1e-6 for a, b in zip(pcts, pcts[1:])):
        problems.append("pct 出现回退")
    if pcts and pcts[-1] > 99.95:
        problems.append("pct 提前打满 100")
    mid = [p for p in pcts if 5 < p < 95]
    if len(mid) < 3:
        problems.append(f"中间态采样太少 ({len(mid)} 个): {pcts[:6]}")
    if pcts and pcts[-1] < 90:
        problems.append(f"结束时 pct 只到 {pcts[-1]:.1f}%, 进度条走不满")
    late = [e for e in etas if e is not None]
    if len(late) < 2 or any(e < 0 for e in late):
        problems.append(f"剩余时间异常: {etas[:4]}")

    print(f"曲目 {len(files)} 首, 实际耗时 {wall:.1f}s, "
          f"progress 消息 {len(pts)} 条")
    print("pct 采样:", " ".join(f"{p:.0f}" for p in pcts))
    print("剩余时间样本:",
          ", ".join(f"{gui.App._fmt_dur(e)}" if e is not None else "None"
                    for e in etas[:6]), "...",
          (f"{gui.App._fmt_dur(etas[-1])}" if etas and etas[-1] is not None
           else "None"))
    print("日志尾部:",
          [m[1] for m in msgs if m[0] == "log"][-2:])
    errs = [m[1] for m in msgs if m[0] == "error"]
    if errs:
        problems.append("worker 报错: " + "; ".join(errs))
        print("ERR", errs)
    shutil.rmtree(out, ignore_errors=True)
    shutil.rmtree(sel, ignore_errors=True)
    print("PROGRESS_TEST", "PASS" if not problems else "FAIL | " + " | ".join(problems))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
