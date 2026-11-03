# -*- coding: utf-8 -*-
"""临时验收脚本(不入库): 截 vendor/ui_*.png + 打印 team-lead 要的四项数据。

用 ctypes/GDI 抓窗口像素, 手写 PNG, 不依赖 Pillow。
"""
import ctypes
import os
import struct
import sys
import time
import tkinter as tk
import zlib
from ctypes import wintypes

PROJ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "msu1-to-cd")
sys.path.insert(0, PROJ)
os.chdir(PROJ)
sys.argv = ["msu1_gui.py", "--no-drop"]

import msu1_gui as g  # noqa: E402
import retro_theme as theme  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
SRCCOPY, DIB_RGB_COLORS, PW_RENDERFULLCONTENT = 0x00CC0020, 0, 2


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def write_png(path, w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb[y * w * 3:(y + 1) * w * 3]) for y in range(h))
    comp = zlib.compress(raw, 6)

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    blob = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", comp) + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(blob)


def grab(hwnd):
    rect = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetWindowDC(wintypes.HWND(hwnd))
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    if not user32.PrintWindow(wintypes.HWND(hwnd), mem, PW_RENDERFULLCONTENT):
        gdi32.BitBlt(mem, 0, 0, w, h, hdc, 0, 0, SRCCOPY)
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(wintypes.HWND(hwnd), hdc)
    src = buf.raw
    rgb = bytearray(w * h * 3)
    for i in range(w * h):
        b0, g0, r0 = src[i * 4], src[i * 4 + 1], src[i * 4 + 2]
        rgb[i * 3:i * 3 + 3] = bytes((r0, g0, b0))
    return w, h, rgb


def px(rgb, w, x, y):
    i = (y * w + x) * 3
    return "%02x%02x%02x" % (rgb[i], rgb[i + 1], rgb[i + 2])


def find_label(widget, needle):
    for ch in widget.winfo_children():
        try:
            if ch.winfo_class() == "TLabel" and needle in str(ch.cget("text")):
                return ch
        except tk.TclError:
            pass
        hit = find_label(ch, needle)
        if hit is not None:
            return hit
    return None


root = tk.Tk()
app = g.App(root)
root.update_idletasks()
root.update()
time.sleep(0.4)

# ---------- 主窗 ----------
hwnd = int(root.wm_frame(), 16)
w, h, rgb = grab(hwnd)
write_png(os.path.join(PROJ, "vendor", "ui_main.png"), w, h, rgb)
logw = app.logw
root_bot = root.winfo_rooty() + root.winfo_height()
print("== 主窗 ==")
print(f"外框尺寸(含标题栏/边框) = {w}x{h}")
print(f"客户区 = {root.winfo_width()}x{root.winfo_height()} "
      f"| req_h = {root.winfo_reqheight()} | geometry = {root.geometry()}")
print(f"横幅 = {app.banner.winfo_width()}x{app.banner.winfo_height()}")
print(f"logw.winfo_height() = {logw.winfo_height()}  (要求 >= 90)")
print(f"logw 底边 = {logw.winfo_rooty() + logw.winfo_height()} "
      f"< root 底边 = {root_bot}  -> "
      f"{logw.winfo_rooty() + logw.winfo_height() < root_bot}")
print(f"dark_title_bar(root) = {g.apply_dark_title_bar(root)}")

# ---------- 下载窗 ----------
app.open_download()
dlg = app._dlg
dlg.update_idletasks()
dlg._load_platform("spc")
dlg.update_idletasks()
dlg.update()
time.sleep(0.4)

cols = ("name", "mtime", "size", "state")
widths = [int(dlg.tree.column(c, "width")) for c in cols]
tree_w = dlg.tree.winfo_width()
print("\n== 下载窗 ==")
print(f"列宽 {dict(zip(cols, widths))} | 合计 = {sum(widths)} "
      f"| tree.winfo_width() = {tree_w} -> {sum(widths)} <= {tree_w}")
print(f"列 stretch = {[bool(dlg.tree.column(c, 'stretch')) for c in cols]}")
print(f"行数 = {len(dlg.tree.get_children())}")

hint = find_label(dlg, "选中机种即可看到列表")
if hint is not None:
    dlg_bot = dlg.winfo_rooty() + dlg.winfo_height()
    h_bot = hint.winfo_rooty() + hint.winfo_height()
    print(f"左栏提示标签: y={hint.winfo_rooty()} h={hint.winfo_height()} "
          f"底边={h_bot} | 窗口 client 底边={dlg_bot} -> 被裁 = {h_bot > dlg_bot}")
else:
    print("左栏提示标签: 未找到")

print(f"进度条 value(加载完成后) = {dlg.progress.cget('value')}")
dw, dh, drgb = grab(int(dlg.wm_frame(), 16))
write_png(os.path.join(PROJ, "vendor", "ui_download.png"), dw, dh, drgb)
bar_y = dlg.progress.winfo_rooty() - dlg.winfo_rooty() + 4
bar_x = dlg.progress.winfo_rootx() - dlg.winfo_rootx() + 30
print(f"进度条像素(value=100 时) = {px(drgb, dw, bar_x + 8, bar_y + 35)}")
dlg.progress["value"] = 0
dlg.update_idletasks()
dlg.update()
time.sleep(0.2)
dw2, dh2, drgb2 = grab(int(dlg.wm_frame(), 16))
print(f"进度条像素(value=0  时) = {px(drgb2, dw2, bar_x + 8, bar_y + 35)}")
print(f"dark_title_bar(dlg)  = {g.apply_dark_title_bar(dlg)}")

# ---------- 添加音源窗 ----------
pd = g.PickDialog(root, PROJ)
pd.update_idletasks()
pd.update()
print(f"\n== 添加音源窗 ==\ndark_title_bar(pd)   = {g.apply_dark_title_bar(pd)}")
pd.destroy()

dlg._shutdown()
app._on_close()
print("\nCLEAN EXIT")
