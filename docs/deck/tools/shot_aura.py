#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shot_aura.py —— 把 proto-aura.html 按指定参数组合渲染成 PNG

为什么要有它：五组外观参数（透光/灯光/氛围/动态/材质）组合起来有几百种，
靠人眼一档档点着看太慢。这个脚本让你一次拍一组、并排比对。

用法（在仓库根目录执行）：

    py -3.14 docs/deck/tools/shot_aura.py                      # 拍 4 套预设
    py -3.14 docs/deck/tools/shot_aura.py --preset cyber       # 只拍赛博霓虹
    py -3.14 docs/deck/tools/shot_aura.py --light gold --glow high \
            --ambient rainbow --dyn circle --mat metal --name my-test
    py -3.14 docs/deck/tools/shot_aura.py --all                # 拍参数全矩阵（慢）

依赖：playwright（`py -3.14 -m pip install playwright && py -3.14 -m playwright install chromium`）
      Pillow 仅用于 --optimize 压缩，没装也能出图。

输出：docs/deck/shots/<name>.png（可用 --outdir 改）
"""

from __future__ import annotations

import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve()
DECK = HERE.parent.parent                 # docs/deck
HTML = DECK / "proto-aura.html"

# 预设组合：覆盖四种灯光 × 两种材质 × 四档透光的代表性搭配
PRESETS = {
    "cyber":  dict(light="cyber",  glow="high", ambient="rainbow", dyn="circle",  mat="glass", live="1"),
    "gold":   dict(light="gold",   glow="mid",  ambient="gold",    dyn="breathe", mat="glass", live="1"),
    "silver": dict(light="silver", glow="low",  ambient="ice",     dyn="static",  mat="metal", live="1"),
    "copper": dict(light="copper", glow="mid",  ambient="violet",  dyn="pulse",   mat="metal", live="1"),
    "idle":   dict(light="copper", glow="low",  ambient="off",     dyn="static",  mat="glass", live="0"),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="渲染 proto-aura.html 并截图")
    p.add_argument("--preset", choices=sorted(PRESETS), help="用预置组合（不给则拍全部预设）")
    p.add_argument("--all", action="store_true", help="拍参数全矩阵（4 灯光 × 4 透光 × 4 氛围 × 4 动态）")
    p.add_argument("--light", choices=["copper", "gold", "silver", "cyber"])
    p.add_argument("--glow", choices=["off", "low", "mid", "high"])
    p.add_argument("--ambient", choices=["off", "gold", "green", "ice", "violet", "rainbow"])
    p.add_argument("--dyn", choices=["static", "breathe", "pulse", "circle"])
    p.add_argument("--mat", choices=["glass", "metal"])
    p.add_argument("--live", choices=["0", "1"], help="1 播放态 / 0 待机态")
    p.add_argument("--name", default="", help="输出文件名（不含扩展名）")
    p.add_argument("--outdir", default=str(DECK / "shots"))
    p.add_argument("--width", type=int, default=1180)
    p.add_argument("--height", type=int, default=620)
    p.add_argument("--dpr", type=int, default=1, help="设备像素比，2 更清晰但文件更大")
    p.add_argument("--pad", type=int, default=40, help="裁剪时围绕面板外扩的像素")
    p.add_argument("--optimize", action="store_true", default=True, help="用 Pillow 压成 256 色（默认开）")
    p.add_argument("--no-optimize", dest="optimize", action="store_false")
    return p


def variants(args: argparse.Namespace) -> list[tuple[str, dict]]:
    if args.all:
        out = []
        for light in ("copper", "gold", "silver", "cyber"):
            for glow in ("off", "low", "mid", "high"):
                for ambient in ("off", "gold", "ice", "rainbow"):
                    for dyn in ("static", "breathe", "pulse", "circle"):
                        out.append((
                            f"{light}-{glow}-{ambient}-{dyn}",
                            dict(light=light, glow=glow, ambient=ambient,
                                 dyn=dyn, mat="glass", live="1"),
                        ))
        return out

    if args.name or any([args.light, args.glow, args.ambient, args.dyn, args.mat, args.live]):
        st = dict(PRESETS["gold"])
        for k in ("light", "glow", "ambient", "dyn", "mat", "live"):
            v = getattr(args, k)
            if v is not None:
                st[k] = v
        name = args.name or "-".join(f"{k}{st[k]}" for k in ("light", "glow", "ambient", "dyn", "mat", "live"))
        return [(name, st)]

    if args.preset:
        return [(args.name or args.preset, PRESETS[args.preset])]

    return [(k, v) for k, v in PRESETS.items()]


def main() -> int:
    args = build_parser().parse_args()

    if not HTML.exists():
        print(f"[x] 找不到原型：{HTML}", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[x] 缺 playwright。装法：", file=sys.stderr)
        print("    py -3.14 -m pip install playwright", file=sys.stderr)
        print("    py -3.14 -m playwright install chromium", file=sys.stderr)
        print("    （不装也能看原型：直接双击 docs/deck/proto-aura.html）", file=sys.stderr)
        return 2

    outdir = pathlib.Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    url = HTML.as_uri()

    print(f"原型：{HTML}")
    print(f"输出：{outdir}")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=args.dpr,
        )
        page.goto(url)
        page.wait_for_timeout(600)          # 等首帧与字体

        for name, st in variants(args):
            page.evaluate(
                """(s) => {
                    const A = window.__aura;
                    Object.assign(A.st, s);
                    A.sync(); A.apply();
                }""",
                st,
            )
            # 呼吸/律动都有过渡，等画面稳定再拍
            page.wait_for_timeout(700)

            # 只截面板 + 外扩，不截整个视口 —— 手工裁剪容易把辉光裁掉
            box = page.evaluate(
                """() => {
                    const r = document.getElementById('panel').getBoundingClientRect();
                    return {x: r.x, y: r.y, w: r.width, h: r.height};
                }"""
            )
            pad = args.pad
            clip = {
                "x": max(0, box["x"] - pad),
                "y": max(0, box["y"] - pad),
                "width": min(args.width - max(0, box["x"] - pad), box["w"] + pad * 2),
                "height": min(args.height - max(0, box["y"] - pad), box["h"] + pad * 2),
            }

            path = outdir / f"{name}.png"
            page.screenshot(path=str(path), clip=clip)

            size = path.stat().st_size / 1024
            print(f"  {name:<34} {clip['width']}x{clip['height']}  {size:.0f} KB")

            if args.optimize:
                _optimize(path)

        browser.close()

    print("\n完成。")
    return 0


def _optimize(path: pathlib.Path) -> None:
    """深底渐变图用 256 色量化能掉 60% 体积，肉眼看不出差别。"""
    try:
        from PIL import Image
    except ImportError:
        return
    try:
        im = Image.open(path).convert("RGB")
        im = im.quantize(colors=256, method=Image.MEDIANCUT, dither=Image.FLOYDSTEINBERG)
        im.save(path, optimize=True)
    except Exception as exc:            # 压缩失败不该影响出图
        print(f"    （压缩失败，保留原图：{exc}）")


if __name__ == "__main__":
    raise SystemExit(main())
