# -*- coding: utf-8 -*-
"""从 retro_theme.app_icon() 导出多尺寸 Windows 图标（.ico）。

设计要点
--------
* **单一事实源**：像素图案不在这里重写一遍，而是直接调用
  :func:`retro_theme.app_icon` 画到临时 Tk 窗口，再用 ``PhotoImage.get``
  读回像素。以后改绘制代码，exe 图标自动跟着变，不会两处走样。
* **放大用最近邻**：32×32 是设计基准尺寸，放大到 48/64/128/256 必须
  复制像素，双线性会把像素硬边糊成毛边，与 radius=0 的视觉语言冲突。
* **缩小用面积平均**：16/24px 是缩小，点采样会丢掉 1px 描边，必须按
  覆盖面积加权。这两条方向相反的规则不能共用一份缩放代码。
* **角落透明**：从四个角做洪水填充，把与边缘连通的底色像素变透明，
  于是 app_icon() 里那圈「像素切角」变成真正的透明切角，任务栏和
  标题栏上不会糊出一块米黄方块。内部同色像素不受影响。
* **PNG 帧只用于 ≥128px**：ICO 允许内嵌 PNG，但 Windows 对**小尺寸**
  PNG 帧的支持历来不稳（部分 shell 视图会留空）。行业惯例是混合：
  ≤64px 用 32bpp BMP(DIB)，≥128px 用 PNG。BMP 帧合计约 34KB，
  换来的是 16px 图标在任何 Windows 版本上都不掉帧。

用法::

    python vendor/make_app_ico.py                # 默认 famicom 主题
    python vendor/make_app_ico.py --theme pce    # 换主题取色
    python vendor/make_app_ico.py --preview      # 额外导出 256px PNG 供目视检查

产物：``assets/app.ico``（列在 MSU1Converter.spec 的 ``icon=`` 中）。
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

BASE = 32                      # 设计基准边长，与 app_icon() 内 size 一致
TARGETS = (16, 24, 32, 48, 64, 128, 256)
PNG_MIN = 128                  # 该尺寸及以上用 PNG 帧，以下用 BMP 帧
OUT_ICO = os.path.join(_ROOT, "assets", "app.ico")
OUT_PREVIEW = os.path.join(_ROOT, "assets", "app_256.png")


def _read_pixels() -> list[list[tuple[int, int, int]]]:
    """建一个隐藏 Tk 窗口，调用 app_icon() 并读回 32×32 像素。

    ``app_icon`` 内部用 ``PhotoImage.put`` 逐行写入，``get()`` 读回来的
    是调色板解析后的真实 RGB，因此不需要在这里认识任何一个色名。
    """
    import tkinter as tk

    import retro_theme

    root = tk.Tk()
    try:
        root.withdraw()
        retro_theme.app_icon(root)
        image = root._retro_icon                      # type: ignore[attr-defined]

        pixels: list[list[tuple[int, int, int]]] = []
        for y in range(BASE):
            row: list[tuple[int, int, int]] = []
            for x in range(BASE):
                value = image.get(x, y)
                # Tk 8.6.10+ 返回 (r,g,b) 元组；老版本返回 "r g b" 字符串
                if isinstance(value, str):
                    r, g, b = (int(v) for v in value.split()[:3])
                else:
                    r, g, b = (int(v) for v in value[:3])
                row.append((r, g, b))
            pixels.append(row)
        return pixels
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _cutout(pixels) -> list[list[tuple[int, int, int, int]]]:
    """把与画布边缘连通的底色像素设为透明（保留内部的同色描边）。

    底色取自 (0,0)，也就是 app_icon() 里的 ``dark``。洪水填充而不是
    「凡等于底色即透明」，否则外框内部的深色描边和卷轴孔外沿会被挖空。
    """
    rgb = [[(r, g, b, 255) for (r, g, b) in row] for row in pixels]
    corner = pixels[0][0]
    stack = [(0, 0), (BASE - 1, 0), (0, BASE - 1), (BASE - 1, BASE - 1)]
    seen = set()
    while stack:
        x, y = stack.pop()
        if not (0 <= x < BASE and 0 <= y < BASE) or (x, y) in seen:
            continue
        if pixels[y][x] != corner:
            continue
        seen.add((x, y))
        r, g, b, _ = rgb[y][x]
        rgb[y][x] = (r, g, b, 0)
        stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
    return rgb


def _resize(pixels, size: int):
    """放大走最近邻（保硬边），缩小走面积平均（保描边）。"""
    if size == BASE:
        return [list(row) for row in pixels]

    if size > BASE:
        ratio = BASE / size
        return [[pixels[min(BASE - 1, int(y * ratio))][min(BASE - 1, int(x * ratio))]
                 for x in range(size)] for y in range(size)]

    # 缩小：按覆盖面积加权，RGB 以 alpha 预乘后再还原，避免透明边缘发黑
    ratio = BASE / size
    out = []
    for y in range(size):
        row = []
        y0, y1 = y * ratio, (y + 1) * ratio
        for x in range(size):
            x0, x1 = x * ratio, (x + 1) * ratio
            wr = wg = wb = wa = wsum = 0.0
            for sy in range(int(y0), min(BASE, int(y1) + 1)):
                oy = min(y1, sy + 1) - max(y0, sy)
                if oy <= 0:
                    continue
                for sx in range(int(x0), min(BASE, int(x1) + 1)):
                    ox = min(x1, sx + 1) - max(x0, sx)
                    if ox <= 0:
                        continue
                    w = ox * oy
                    r, g, b, a = pixels[sy][sx]
                    alpha = a / 255.0
                    wr += r * alpha * w
                    wg += g * alpha * w
                    wb += b * alpha * w
                    wa += a * w
                    wsum += w
            if wsum <= 0:
                row.append((0, 0, 0, 0))
                continue
            alpha = wa / wsum
            if alpha < 1e-6:
                row.append((0, 0, 0, 0))
            else:
                k = 255.0 / wa
                row.append((min(255, round(wr * k)), min(255, round(wg * k)),
                            min(255, round(wb * k)), round(alpha)))
        out.append(row)
    return out


def _png(rows, size: int) -> bytes:
    """把 RGBA 行编码成 PNG（手写，仅用标准库 zlib）。"""
    raw = bytearray()
    for row in rows:
        raw.append(0)                                  # 每行滤波器 None
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def _bmp(rows, size: int) -> bytes:
    """编成 ICO 内嵌的 32bpp BMP 帧（BITMAPINFOHEADER + BGRA + AND 掩码）。"""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)

    xor = bytearray()
    for row in reversed(rows):                         # DIB 是 bottom-up
        for r, g, b, a in row:
            xor += bytes((b, g, r, a))

    # 32bpp 下 alpha 已足够，但 AND 掩码仍是格式要求；每行按 4 字节对齐
    stride = ((size + 31) // 32) * 4
    and_mask = bytearray()
    for row in reversed(rows):
        bits = bytearray(stride)
        for x, (_r, _g, _b, a) in enumerate(row):
            if a == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        and_mask += bits

    return header + bytes(xor) + bytes(and_mask)


def _ico(frames: list[tuple[int, bytes]]) -> bytes:
    """按 ICO 容器格式拼装（每帧为 BMP 或内嵌 PNG）。"""
    head = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries = bytearray()
    body = bytearray()
    for size, data in frames:
        dim = 0 if size >= 256 else size                # 256 在目录里记作 0
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        body += data
    return bytes(head + entries + body)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 exe 图标")
    parser.add_argument("--theme", default=None, help="取色主题名（默认 famicom）")
    parser.add_argument("--preview", action="store_true", help="导出 256px PNG 预览")
    args = parser.parse_args()

    if args.theme:
        import retro_theme
        if args.theme not in retro_theme.THEMES:
            print("未知主题 %r，可选：%s" % (args.theme, ", ".join(retro_theme.THEMES)))
            return 2
        retro_theme.PALETTE.clear()
        retro_theme.PALETTE.update(retro_theme.THEMES[args.theme]["palette"])
        print("取色主题：%s" % args.theme)

    pixels = _cutout(_read_pixels())

    def frame(size: int) -> tuple[int, bytes]:
        rows = _resize(pixels, size)
        return (size, _png(rows, size) if size >= PNG_MIN else _bmp(rows, size))

    frames = [frame(size) for size in TARGETS]

    os.makedirs(os.path.dirname(OUT_ICO), exist_ok=True)
    blob = _ico(frames)
    with open(OUT_ICO, "wb") as fh:
        fh.write(blob)

    if args.preview:
        with open(OUT_PREVIEW, "wb") as fh:
            fh.write(frames[TARGETS.index(256)][1])

    print("%-6s %s" % ("帧", "字节"))
    for size, data in frames:
        print("%-6s %s" % ("%dpx" % size, len(data)))
    print("-" * 20)
    print("总计   %s 字节 → %s" % (len(blob), OUT_ICO))
    if args.preview:
        print("预览   %s" % OUT_PREVIEW)

    if blob[:4] != b"\x00\x00\x01\x00":
        print("ICO 文件头异常")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
