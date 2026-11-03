#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时截图：云盘「选择上传内容」对话框（v4.25）。用完删。"""

import os
import shutil
import sys
import tempfile
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "msu1-to-cd"))
sys.path.insert(0, HERE)

import _shot_theme as st                                    # noqa: E402
import msu1_gui as g                                        # noqa: E402


def _fake(path, mb):
    with open(path, "wb") as f:
        f.write(b"RIFF" + b"\0" * 4092)
        f.seek(int(mb * 1e6))
        f.write(b"\0")


def main():
    tmp = tempfile.mkdtemp(prefix="scope_shot_")
    try:
        merged = [os.path.join(tmp, "Adventures of Gilligan's Island（合并）.wav"),
                  os.path.join(tmp, "10-Yard Fight（合并）.wav")]
        tracks = [os.path.join(tmp, "%02d_Track %d.wav" % (i, i + 1))
                  for i in range(6)]
        for p in merged:
            _fake(p, 3.1)
        for p in tracks:
            _fake(p, 1.02)

        root = tk.Tk()
        app = g.App(root)                                   # noqa: F841
        root.update()

        def shoot():
            for w in root.winfo_children():
                if isinstance(w, tk.Toplevel):
                    try:
                        w.update_idletasks()
                        w.update()
                        st.shot(w, "ui_cloud_scope.png")
                    finally:
                        w.destroy()

        root.after(1200, shoot)
        picked = g._ask_cloud_scope(root, merged, tracks)
        print("picked:", "None（取消）" if picked is None
              else "%d 个（第一个是 %s）" % (len(picked), os.path.basename(picked[0])))
        root.destroy()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
