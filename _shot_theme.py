# -*- coding: utf-8 -*-
"""临时截图脚本：主窗 + 下载窗，用于肉眼验收浅色主题。

用 ctypes + GDI 从**屏幕**抓（PrintWindow 对 Tk 会有缩放失真，实测行距被压扁），
手写 PNG，不依赖 Pillow。产物落在 vendor/ui_light_*.png。

用法：python _shot_theme.py [main]
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
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
SRCCOPY = 0x00CC0020


class BIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BI(ctypes.Structure):
    _fields_ = [("bmiHeader", BIH), ("bmiColors", wintypes.DWORD * 3)]


def grab_screen(x, y, w, h):
    hdc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, SRCCOPY)
    bi = BI()
    bi.bmiHeader.biSize = ctypes.sizeof(BIH)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, hdc)
    src = buf.raw
    rgb = bytearray(w * h * 3)
    for i in range(w * h):
        b0, g0, r0 = src[i * 4], src[i * 4 + 1], src[i * 4 + 2]
        rgb[i * 3:i * 3 + 3] = bytes((r0, g0, b0))
    return rgb


def write_png(path, w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb[y * w * 3:(y + 1) * w * 3]) for y in range(h))

    def ck(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF)

    open(path, "wb").write(
        b"\x89PNG\r\n\x1a\n"
        + ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + ck(b"IDAT", zlib.compress(raw, 6)) + ck(b"IEND", b""))


def shot(win, name):
    """按**真实窗口矩形**（GetWindowRect，含标题栏与边框）抓屏幕。"""
    win.update_idletasks()
    win.update()
    rect = wintypes.RECT()
    hwnd = int(win.winfo_id())
    parent = user32.GetParent(wintypes.HWND(hwnd))
    user32.GetWindowRect(wintypes.HWND(parent or hwnd), ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    rgb = grab_screen(rect.left, rect.top, w, h)
    out = os.path.join(PROJ, "vendor", name)
    write_png(out, w, h, rgb)
    print("  -> %s (%dx%d, %d KB)" % (name, w, h, os.path.getsize(out) // 1024))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.path.insert(0, PROJ)
    os.chdir(PROJ)
    sys.argv = ["msu1_gui.py", "--no-drop"]
    import msu1_gui as g

    root = tk.Tk()
    app = g.App(root)
    root.update()
    root.lift()
    root.attributes("-topmost", True)
    time.sleep(1.0)
    root.update()
    shot(root, "ui_light_main.png")

    if mode != "main":
        app.open_download()
        dlg = getattr(app, "_dlg", None)
        if dlg is None:
            print("  !! 下载窗没开起来")
        else:
            dlg.update()
            dlg.attributes("-topmost", True)
            try:
                dlg._load_platform("spc")
            except Exception as e:                        # noqa: BLE001
                print("  装载 spc 列表失败:", e)
            time.sleep(0.8)
            dlg.update()
            shot(dlg, "ui_light_download.png")
            dlg._shutdown()
    root.attributes("-topmost", False)
    try:
        app._on_close()
    except Exception:                                     # noqa: BLE001
        pass
    print("SHOT OK")


if __name__ == "__main__":
    main()
