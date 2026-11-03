# -*- coding: utf-8 -*-
"""临时审计(仅验证用): 找出「自身底色 != 父容器底色」的 ttk 控件。

ttk 没有透明背景, 标签/容器底色不一致就会在卡片里露出一块深色矩形。
"""
import os
import sys
import tkinter as tk
from tkinter import ttk

PROJ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "msu1-to-cd")
sys.path.insert(0, PROJ)
os.chdir(PROJ)
sys.argv = ["msu1_gui.py", "--drop"]

import msu1_gui as g  # noqa: E402
import retro_theme as theme  # noqa: E402

_orig_db = theme._style_option_db


def _patched_db(r):
    _orig_db(r)
    r.option_add("*Listbox.font", "{%s} %d" % (theme.FONT[0], theme.FONT[1]))
    r.option_add("*Text.font", "{%s} %d" % (theme.FONT_MONO[0], theme.FONT_MONO[1]))
    r.option_add("*Menu.font", "{%s} %d" % (theme.FONT[0], theme.FONT[1]))
    r.option_add("*TCombobox*Listbox.font",
                 "{%s} %d" % (theme.FONT[0], theme.FONT[1]))


theme._style_option_db = _patched_db

STYLE = None
SKIP = {"TSeparator", "TScrollbar", "TEntry", "TSpinbox", "TCombobox",
        "TProgressbar", "Treeview"}


def bg_of(widget):
    cls = widget.winfo_class()
    try:
        style = str(widget.cget("style") or "")
    except tk.TclError:
        style = ""
    if isinstance(widget, ttk.Widget):
        style = style or cls
        try:
            return str(STYLE.lookup(style, "background") or "?"), style
        except tk.TclError:
            return "?", style
    try:
        return str(widget.cget("background")), cls
    except tk.TclError:
        return "?", cls


def walk(widget, path=".", problems=None):
    if problems is None:
        problems = []
    for child in widget.winfo_children():
        cls = child.winfo_class()
        my_bg, my_style = bg_of(child)
        par_bg, _ = bg_of(widget)
        if cls == "TLabelframe":
            # Labelframe 的内部区域用自身 style 的 background 绘制
            if my_bg != par_bg:
                problems.append((child, my_style, my_bg, par_bg))
        elif cls in ("TLabel", "TCheckbutton", "TRadiobutton"):
            if my_bg != par_bg:
                problems.append((child, my_style, my_bg, par_bg))
        elif cls == "TFrame":
            if my_bg != par_bg:
                problems.append((child, my_style, my_bg, par_bg))
        walk(child, path, problems)
    return problems


root = tk.Tk()
STYLE = ttk.Style(root)      # 必须绑到同一个 Tk 实例, 否则查不到 install() 装的样式
app = g.App(root)
root.update_idletasks()

probs = walk(root)
print(f"MAIN: {len(probs)} 处底色不一致", flush=True)
for w, style, mine, parent in probs:
    print(f"  {w.winfo_class():10s} style={style:22s} self={mine} parent={parent}",
          flush=True)

app.open_download()
dlg = app._dlg
dlg.update_idletasks()
probs = walk(dlg)
print(f"DLG: {len(probs)} 处底色不一致", flush=True)
for w, style, mine, parent in probs:
    print(f"  {w.winfo_class():10s} style={style:22s} self={mine} parent={parent}",
          flush=True)

# PickDialog
pd = g.PickDialog(root, PROJ)
pd.update_idletasks()
probs = walk(pd)
print(f"PICK: {len(probs)} 处底色不一致", flush=True)
for w, style, mine, parent in probs:
    print(f"  {w.winfo_class():10s} style={style:22s} self={mine} parent={parent}",
          flush=True)
pd.destroy()

dlg._shutdown()
app._on_close()
print("AUDIT DONE", flush=True)
