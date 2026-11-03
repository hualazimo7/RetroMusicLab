#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面端到端校验: 真的开窗口跑一次转换, 检查进度条与文字。

覆盖的是旧版失效的那条路径: 只有一个音源(一个 SPC 文件夹)时,
进度条是不是还能一格格往前走、百分比和剩余时间是不是真的有内容。

用法: python vendor/test_progress_gui.py <SPC 文件夹>
"""
import os
import shutil
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import msu1_gui as gui          # noqa: E402
import spc_wav                  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "_gui_src")
OUT = os.path.join(HERE, "_gui_out")


def main():
    if len(sys.argv) < 2:
        print("用法: python vendor/test_progress_gui.py <SPC 文件夹>")
        return 2
    files = spc_wav.collect_spc([sys.argv[1]])[:6]
    if not files:
        print("没有 .spc")
        return 2
    shutil.rmtree(SRC, ignore_errors=True)
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(SRC)
    for f in files:
        shutil.copy2(f, os.path.join(SRC, os.path.basename(f)))

    gui.messagebox.showinfo = lambda *a, **k: None   # 完成弹窗会卡住测试

    root = tk.Tk()
    app = gui.App(root)
    app.var_out.set(OUT)
    app.add_paths([SRC])
    if not app.sources:
        print("添加音源失败")
        return 2
    app.var_cd.set(False)
    app.var_wav.set(True)
    app.start()

    samples = []
    state = {"done": False}

    def tick():
        samples.append((app.progress["value"], app.var_pct.get(),
                        app.var_status.get(), app.var_eta.get()))
        if state["done"]:
            if not app.btn_start["state"] == "disabled":
                root.after(300, finish)
            return
        if app.worker is not None and not app.worker.is_alive():
            state["done"] = True
            root.after(300, tick)          # 再转两轮, 把 done 消息消化完
            return
        root.after(250, tick)

    code = {"v": 1}

    def finish():
        vals = [s[0] for s in samples]
        pcts = [s[1] for s in samples]
        etas = [s[3] for s in samples]
        app._on_close()

        problems = []
        moving = [v for v in vals if 0 < v < 100]
        if len(moving) < 2:
            problems.append(f"进度条几乎没动: {vals}")
        if any(b < a - 0.01 for a, b in zip(vals, vals[1:])):
            problems.append("进度条回退")
        if not vals or vals[-1] < 99.9:
            problems.append(f"结束没走满: {vals[-1] if vals else None}")
        mid = [p for p in pcts if p not in ("0%", "100%")]
        if len(mid) < 2:
            problems.append(f"百分比几乎没变: {pcts}")
        if not any("剩余约" in e or "即将完成" in e for e in etas):
            problems.append(f"剩余时间没出现: {etas[:5]}")
        if "100%" not in pcts:
            problems.append("结束时没有 100%")

        print("进度采样:", " ".join(f"{v:.0f}%" for v in vals[:14]),
              "..." if len(vals) > 14 else "")
        print("百分比:", " ".join(dict.fromkeys(pcts)))
        print("剩余时间尾部:", [e for e in etas if e][-2:])
        print("GUI_PROGRESS", "PASS" if not problems else "FAIL | " + " | ".join(problems))
        code["v"] = 0 if not problems else 1

    root.after(400, tick)
    root.mainloop()
    shutil.rmtree(SRC, ignore_errors=True)
    shutil.rmtree(OUT, ignore_errors=True)
    return code["v"]


if __name__ == "__main__":
    sys.exit(main())
