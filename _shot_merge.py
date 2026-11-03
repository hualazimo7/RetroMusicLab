#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_shot_merge.py — 多轨合并开关的截图验收（v4.24）。

两张图：
* `ui_merge_main.png`     主窗 ② 输出格式：合并复选框勾上后的样子
* `ui_merge_download.png` 下载窗：「自动转 WAV」+「多轨合并」同时勾上

⚠ 只改复选框状态，**不真转** —— 所以截出来是"设置态"，不是运行态。
"""

import os
import sys
import time
import tkinter as tk

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.join(PROJ, "msu1-to-cd"))
os.chdir(PROJ)

import _shot_theme as st      # noqa: E402

sys.argv = ["msu1_gui.py", "--no-drop"]
import msu1_gui as g          # noqa: E402


def main():
    root = tk.Tk()
    app = g.App(root)
    root.update()
    app.var_merge.set(True)
    app.log("🔗 多轨合并已开启：每个音源除分轨外，另出一个「源名（合并）.wav」")
    root.lift()
    root.attributes("-topmost", True)
    time.sleep(0.8)
    root.update()
    print("主窗（合并勾上）:")
    st.shot(root, "ui_merge_main.png")

    dd = g.DownloadDialog(root)
    dd.geometry("1400x720+60+60")
    try:
        _k = next(p.get("key") for p in g.jw.PLATFORMS if p.get("key"))
        dd._load_platform(_k)
    except Exception:                  # noqa: BLE001
        pass
    dd.var_auto_wav.set(True)
    dd.var_merge.set(True)
    for _ in range(80):
        root.update()
        time.sleep(0.03)
    dd.lift()
    dd.attributes("-topmost", True)
    time.sleep(0.6)
    root.update()
    print("下载窗（自动转 WAV + 合并都勾上）:")
    st.shot(dd, "ui_merge_download.png")

    dd._closed = True
    dd._shutdown()
    root.attributes("-topmost", False)
    root.update()
    root.destroy()
    print("done")


if __name__ == "__main__":
    main()
