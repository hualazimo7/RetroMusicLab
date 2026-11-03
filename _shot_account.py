#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_shot_account.py — 账号切换功能截图（按钮位置 + 两个对话框）。"""

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

import ctypes                 # noqa: E402
from ctypes import wintypes   # noqa: E402

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32


def shot_pw(win, name):
    """PrintWindow 抓**自己的**窗口内容（不受遮挡影响）。

    主窗口仍然用屏幕 BitBlt（`st.shot`，行距正常）。但对话框会被主窗
    盖住一部分 —— 屏幕抓图会抓到下层的内容，只能用 PrintWindow。
    """
    win.update_idletasks()
    win.update()
    hwnd = int(win.winfo_id())
    parent = _user32.GetParent(wintypes.HWND(hwnd)) or hwnd
    rect = wintypes.RECT()
    _user32.GetWindowRect(wintypes.HWND(parent), ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top

    hdc = _user32.GetWindowDC(wintypes.HWND(parent))
    mem = _gdi32.CreateCompatibleDC(hdc)
    bmp = _gdi32.CreateCompatibleBitmap(hdc, w, h)
    _gdi32.SelectObject(mem, bmp)
    _user32.PrintWindow(wintypes.HWND(parent), mem, 2)   # PW_RENDERFULLCONTENT

    bi = st.BI()
    bi.bmiHeader.biSize = ctypes.sizeof(st.BIH)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    _gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    _gdi32.DeleteObject(bmp)
    _gdi32.DeleteDC(mem)
    _user32.ReleaseDC(wintypes.HWND(parent), hdc)

    src = buf.raw
    rgb = bytearray(w * h * 3)
    for i in range(w * h):
        rgb[i * 3:i * 3 + 3] = bytes((src[i * 4 + 2], src[i * 4 + 1], src[i * 4]))
    st.write_png(os.path.join(st.PROJ, "vendor", name), w, h, rgb)
    print("  -> %s (%dx%d, %d KB)" % (name, w, h,
                                      os.path.getsize(os.path.join(
                                          st.PROJ, "vendor", name)) // 1024))


def settle(root, top, n=40):
    top.deiconify()
    top.lift()
    top.attributes("-topmost", True)
    try:
        top.focus_force()
    except tk.TclError:
        pass
    for _ in range(n):
        root.update()
        time.sleep(0.03)
    time.sleep(0.9)          # DWM 合成要一点时间，否则会抓到下层窗口的残留
    root.update()
    top.update()


def main():
    root = tk.Tk()
    app = g.App(root)
    root.update()
    settle(root, root, 25)
    print("主窗（▾ 在 ☁ 上传云盘 右侧）:")
    st.shot(root, "ui_account_main.png")

    dd = g.DownloadDialog(root)
    dd.geometry("1400x720+50+50")
    try:
        _k = next(p.get("key") for p in g.jw.PLATFORMS if p.get("key"))
        dd._load_platform(_k)
    except Exception:                  # noqa: BLE001
        pass
    settle(root, dd, 50)
    print("下载窗:")
    st.shot(dd, "ui_account_download.png")

    # ⚠ 建完立刻置 _closed：__init__ 里排了 after(80, _begin)，
    # 不拦的话它会真的清登录态、开浏览器。
    # ⚠ 下载窗的 topmost 必须先关掉，否则抓屏抓到的是它（会盖住新窗口）。
    dd.attributes("-topmost", False)
    root.attributes("-topmost", False)
    sd = g.SwitchAccountDialog(root, "switch")
    sd._closed = True
    settle(root, sd.top)
    print("切换账号窗:")
    shot_pw(sd.top, "ui_account_switch.png")

    sd._finish(True, "示例用户B", "")
    settle(root, sd.top, 12)
    print("切换成功:")
    shot_pw(sd.top, "ui_account_switch_ok.png")
    try:
        sd.top.destroy()
    except tk.TclError:
        pass

    sd2 = g.SwitchAccountDialog(root, "logout")
    sd2._closed = True
    settle(root, sd2.top)
    print("退出登录窗:")
    shot_pw(sd2.top, "ui_account_logout.png")
    try:
        sd2.top.destroy()
    except tk.TclError:
        pass

    dd._closed = True
    dd._shutdown()
    root.update()
    root.destroy()
    print("done")


if __name__ == "__main__":
    main()
