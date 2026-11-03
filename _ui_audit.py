# -*- coding: utf-8 -*-
"""临时 UI 体检截图：主窗 + 下载窗，按 **真实窗口框**（wm frame）抓屏。

与 `_shot_theme.py` 的区别：
  * 先设 DPI 感知，避免 125% 下坐标 ×1.25 错位；
  * 用 `win.wm_frame()` 拿 Windows 真实 HWND（含标题栏/边框），不再靠 GetParent 猜；
  * 窗口先挪到 (0,0)，保证不会被屏幕边缘裁掉。

用法：python _ui_audit.py [theme] [outdir]
"""
import ctypes
import os
import sys
import time
import tkinter as tk
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.join(HERE, "msu1-to-cd")
sys.path.insert(0, HERE)
sys.path.insert(0, PROJ)

import _shot_theme as st  # noqa: E402

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

user32 = ctypes.windll.user32


def shot_frame(win, name, outdir):
    win.update_idletasks()
    win.update()
    # 只抓**客户区**：GetWindowRect 在 DWM 下会把阴影/边框算进去，
    # 实测抓出 1067 高、真正窗口只有 866 —— 多出来的部分是别的窗口。
    hwnd = int(win.winfo_id())
    hw = wintypes.HWND(hwnd)
    # 置顶是我们自己进程的窗口，attributes 一定生效；SetForegroundWindow 对
    # 抢焦点的请求会被系统拒绝（实测抓到的是前台那个 Chrome）。
    try:
        win.attributes("-topmost", True)
        win.lift()
    except tk.TclError:
        pass
    time.sleep(0.45)
    win.update()
    rc = wintypes.RECT()
    user32.GetClientRect(hw, ctypes.byref(rc))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hw, ctypes.byref(pt))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 1 or h <= 1:
        print("  !! 窗口未就绪，跳过", name)
        return
    rgb = st.grab_screen(pt.x, pt.y, w, h)
    out = os.path.join(outdir, name)
    st.write_png(out, w, h, rgb)
    print("  -> %s (%dx%d, %d KB)" % (name, w, h, os.path.getsize(out) // 1024))


def main():
    theme = sys.argv[1] if len(sys.argv) > 1 else "famicom"
    outdir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(PROJ, "vendor")
    os.makedirs(outdir, exist_ok=True)
    os.chdir(PROJ)
    sys.argv = ["msu1_gui.py", "--no-drop"]

    import msu1_gui as g
    import retro_theme as rt

    root = tk.Tk()
    app = g.App(root)
    rt.set_current(theme)
    try:
        rt.apply_theme(theme)
    except Exception:
        pass
    root.geometry("+0+0")
    root.deiconify()
    root.lift()
    root.update()
    for _ in range(6):
        root.update()
    try:
        left = app.btn_start.master.master
        st_lab = getattr(app, "var_status", None)
        print("ROOT h=%d  leftcol h=%d req=%d  banner=%d" % (
            root.winfo_height(), left.winfo_height(), left.winfo_reqheight(),
            root.winfo_children()[0].winfo_height()))
        for c in left.winfo_children():
            print("   %-11s y=%4d..%4d" % (c.winfo_class(), c.winfo_y(),
                                           c.winfo_y() + c.winfo_height()))
    except Exception as exc:                     # noqa: BLE001
        print("probe skip", exc)
    shot_frame(root, "audit_main_%s.png" % theme, outdir)

    app.open_download()
    root.update()
    dlg = getattr(app, "_dlg", None)
    if dlg is not None:
        dlg.geometry("+40+40")
        dlg.update()
        for _ in range(6):
            root.update()
        shot_frame(dlg, "audit_download_%s.png" % theme, outdir)
    root.destroy()
    print("AUDIT SHOT OK")


if __name__ == "__main__":
    main()
