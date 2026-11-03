# -*- coding: utf-8 -*-
"""临时脚本：裁剪 + 放大 PNG 局部，用来看清字体/像素细节。

用法：python _zoom.py 源图.png 输出.png x y w h [倍数]
"""
import os
import sys
import tkinter as tk


def main():
    src, dst = sys.argv[1], sys.argv[2]
    x, y, w, h = (int(v) for v in sys.argv[3:7])
    zoom = int(sys.argv[7]) if len(sys.argv) > 7 else 2
    root = tk.Tk()
    root.withdraw()
    img = tk.PhotoImage(file=src)
    out = tk.PhotoImage(width=w, height=h)
    # 先把区域逐行拷出来（PhotoImage 没有裁剪 API，只能 get/put）
    for row in range(h):
        line = []
        for col in range(w):
            r, g, b = img.get(x + col, y + row)
            line.append("#%02x%02x%02x" % (r, g, b))
        out.put("{" + " ".join(line) + "}", to=(0, row))
    big = out.zoom(zoom)
    big.write(dst, format="png")
    print("->", dst, big.width(), "x", big.height())
    root.destroy()


if __name__ == "__main__":
    main()
