# -*- coding: utf-8 -*-
"""临时脚本：单横幅逐套主题截图（只 80px 高，便于看清标题字体/配色）。

用法：python _shot_banner.py
"""
import os
import sys
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.join(HERE, "msu1-to-cd")
sys.path.insert(0, HERE)
sys.path.insert(0, PROJ)
os.chdir(PROJ)

from _shot_theme import shot  # noqa: E402

import retro_theme as rt  # noqa: E402


def main():
    root = tk.Tk()
    root.title("banner preview")
    rt.install(root)
    banner = rt.RetroBanner(root, height=80)
    banner.pack(fill="x")
    root.geometry("1260x104+0+0")
    banner.start()
    root.update()
    root.lift()
    root.attributes("-topmost", True)
    for key in rt.theme_keys():
        rt.apply_theme(key)
        banner.refresh()      # 横幅是 Canvas：不 refresh 就还停在上一套颜色
        root.update()
        time.sleep(0.35)
        root.update()
        shot(root, "_banner_%s.png" % key)
    banner.stop()
    rt.apply_theme(rt.DEFAULT_THEME)
    root.destroy()
    print("BANNER OK")


if __name__ == "__main__":
    main()
