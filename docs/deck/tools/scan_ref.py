#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_ref.py —— 在参考实现目录里扫"像参数的东西"，输出候选数值清单

设计约束（重要）：
  本脚本只输出 **键名 / 数值 / 颜色 / 时长** 这类短片段（上限 48 字符），
  不输出函数体、不输出整行代码、不复制任何素材。
  目的是拿到「点阵多少列、三段色分界在哪、四档透光多亮」这些数字，
  让 RetroMusicLab 自己的实现能对齐观感，而不是搬运别人的实现。

用法：

    py -3.14 docs/deck/tools/scan_ref.py "D:\\path\\to\\extracted"
    py -3.14 docs/deck/tools/scan_ref.py "D:\\path\\to\\extracted" --out params.json
    py -3.14 docs/deck/tools/scan_ref.py <dir> --ext js,css,html --max 60

不指定 --out 时只在控制台打印，方便先看一眼。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys

# 扫描范围：Web 栈 / Qt / Flutter / Python 都可能出现
DEFAULT_EXT = "js,css,html,htm,json,qml,dart,py,ts,tsx,scss,less,vue,svelte"

# 单文件体积上限：小于它的才读（大的通常是打包产物，按需自己调 --max-mb）
MAX_BYTES = 8 * 1024 * 1024

# 目录黑名单
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".cache"}

# ── 规则表 ────────────────────────────────────────────────────────────────
# (类别, 正则, 说明)  —— 每条只取第 1 个分组作为"值"
RULES: list[tuple[str, re.Pattern, str]] = [
    # 点阵几何
    ("cols", re.compile(r"(?:DOT_)?(?:COLS?|COLUMNS?|BARS?|barCount|bar_count|dotCols|numBars)\s*[:=]\s*(\d{1,3})", re.I),
     "点阵列数"),
    ("rows", re.compile(r"(?:DOT_)?(?:ROWS?|LEVELS?|dotRows|numRows|steps)\s*[:=]\s*(\d{1,3})", re.I),
     "点阵行数"),
    ("cell", re.compile(r"(?:dotSize|dotRadius|dot_radius|cellSize|cell_size|dotGap|dot_gap)\s*[:=]\s*([\d.]{1,8}(?:px|%|rem)?)", re.I),
     "点直径 / 间距"),
    ("gap", re.compile(r"(?:gap|spacing)\s*:\s*([\d.]{1,8}(?:px|%))", re.I),
     "间距（带单位）"),

    # 三段色分界
    ("band", re.compile(r"(?:BAND_?EDGE|bandEdge|threshold|tier|band_?split|cut_?off)\s*[:=]\s*\[?\s*([\d.]{1,6}(?:\s*,\s*[\d.]{1,6}){0,2})", re.I),
     "三段色分界（归一化高度）"),
    ("grad-pct", re.compile(r"linear-gradient\(([^)]{10,240})\)", re.I),
     "渐变里的百分比序列 = 三段色分界最可能的来源"),

    # 透光档位
    ("glow", re.compile(r"(?:GLOW|glow|intensity|translucen\w*|lighting)\w*\s*[:=]\s*\{?([^{};]{4,90})", re.I),
     "四档透光的强度表"),
    ("opacity", re.compile(r"opacity\s*[:=]\s*([\d.]{1,6})", re.I),
     "不透明度"),

    # 光晕
    ("blur", re.compile(r"blur(?:\(\s*|:\s*)([\d.]{1,6})", re.I), "模糊半径 px"),
    ("shadow", re.compile(r"box-shadow\s*:\s*([^;]{6,120})", re.I), "阴影（层数 + 半径）"),
    ("drop-shadow", re.compile(r"drop-shadow\(([^)]{4,80})\)", re.I), "滤镜阴影"),

    # 动画
    ("anim-dur", re.compile(r"animation(?:-duration)?\s*:\s*[^;]{0,60}?([\d.]{1,6})s", re.I), "动画时长 s"),
    ("keyframes", re.compile(r"@keyframes\s+([\w-]{1,40})", re.I), "关键帧名字"),
    ("transition", re.compile(r"transition\s*:\s*([^;]{4,90})", re.I), "过渡"),

    # 材质噪声
    ("turbulence", re.compile(r"baseFrequency\s*[:=]\s*[\"']?([\d.]{1,8})", re.I), "噪声频率"),
    ("octaves", re.compile(r"numOctaves\s*[:=]\s*[\"']?(\d{1,2})", re.I), "噪声阶数"),

    # 音频驱动
    ("fft", re.compile(r"fftSize\s*[:=]\s*(\d{3,5})", re.I), "FFT 大小"),
    ("smoothing", re.compile(r"smoothingTimeConstant\s*[:=]\s*([\d.]{1,6})", re.I), "平滑系数"),
    ("rms-lerp", re.compile(r"(?:lerp|smooth|decay)\s*[:=(]\s*([\d.]{1,6})", re.I), "平滑 / 衰减系数"),

    # 时长基准
    ("seconds", re.compile(r"(?:CYCLE|PERIOD|INTERVAL|DURATION)\w*\s*[:=]\s*(\d{1,3})", re.I), "周期常量"),
]

# 颜色：单独收集，不参与"值截断"逻辑
HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")
RGB_RE = re.compile(r"rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}[^)]{0,20}\)")

# 渐变里的百分数 —— 三段色的分界通常就写在这里
PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)%")

# 参考实现里可能出现的色名关键词（用于定位配色表所在文件）
COLOR_CTX = re.compile(r"(light|skin|theme|palette|color|band|ring|aura|ambient)", re.I)


def clean(value: str, limit: int = 48) -> str:
    """只留数值 / 短标识，砍掉任何像代码的尾巴。"""
    v = re.sub(r"\s+", " ", value).strip().strip("'\"")
    v = v.rstrip("，,;")
    if len(v) > limit:
        v = v[:limit] + "…"
    return v


def idents(root: pathlib.Path, exts: set[str], max_mb: int) -> list[pathlib.Path]:
    limit = max_mb * 1024 * 1024
    out: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        for fn in filenames:
            p = pathlib.Path(dirpath) / fn
            if p.suffix.lower().lstrip(".") not in exts:
                continue
            try:
                if p.stat().st_size > limit:
                    continue
            except OSError:
                continue
            out.append(p)
    return out


def read_text(p: pathlib.Path) -> str | None:
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return p.read_text(encoding=enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError:
            return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="scan a reference tree for tunable constants")
    ap.add_argument("root", help="directory of the extracted reference implementation")
    ap.add_argument("--out", help="write candidates to this JSON file")
    ap.add_argument("--ext", default=DEFAULT_EXT, help=f"extensions, default: {DEFAULT_EXT}")
    ap.add_argument("--max-mb", type=int, default=8, help="skip files larger than N MB (default 8)")
    ap.add_argument("--max", type=int, default=40, help="max hits per category (default 40)")
    args = ap.parse_args()

    root = pathlib.Path(args.root).expanduser()
    if not root.is_dir():
        print(f"[x] not a directory: {root}", file=sys.stderr)
        return 2

    exts = {e.strip().lower().lstrip(".") for e in args.ext.split(",") if e.strip()}
    files = idents(root, exts, args.max_mb)
    if not files:
        print(f"[x] no matching files under {root} (ext={','.join(sorted(exts))})", file=sys.stderr)
        return 2

    print(f"root : {root}")
    print(f"files: {len(files)}")
    print()

    hits: dict[str, list[dict]] = {}
    colors: dict[str, dict] = {}

    for p in files:
        text = read_text(p)
        if not text:
            continue
        rel = str(p.relative_to(root))

        for cat, rx, _desc in RULES:
            bucket = hits.setdefault(cat, [])
            if len(bucket) >= args.max:
                continue
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                if cat == "grad-pct":
                    # 只保留百分数序列：它是分界位置，其他内容属于样式细节
                    pcts = PCT_RE.findall(m.group(1))
                    if len(pcts) < 2:
                        continue
                    value = " -> ".join(f"{p}%" for p in pcts)
                else:
                    value = clean(m.group(1))
                bucket.append({"file": rel, "line": line, "value": clean(value, 72)})
                if len(bucket) >= args.max:
                    break

        # 颜色：按文件聚合，顺便记下这是不是配色相关文件
        if HEX_RE.search(text) or RGB_RE.search(text):
            entry = colors.setdefault(rel, {"hex": [], "rgb": [], "colorish_name": bool(COLOR_CTX.search(p.name))})
            hexes = HEX_RE.findall(text)
            rgb = RGB_RE.findall(text)
            for h in dict.fromkeys(hexes):
                if len(entry["hex"]) < 40:
                    entry["hex"].append(h.lower())
            for c in dict.fromkeys(rgb):
                if len(entry["rgb"]) < 20:
                    entry["rgb"].append(clean(c, 40))

    # ── 控制台输出：按类别，每类最多 12 条，避免刷屏 ──
    for cat, _rx, desc in RULES:
        bucket = hits.get(cat) or []
        if not bucket:
            continue
        print(f"== {cat} == {desc}")
        for h in bucket[:12]:
            print(f"   {h['file']}:{h['line']}   {h['value']}")
        if len(bucket) > 12:
            print(f"   ... {len(bucket) - 12} more")
        print()

    if colors:
        print("== colors == (files containing hex / rgb)")
        for rel, e in sorted(colors.items(), key=lambda kv: -len(kv[1]["hex"]))[:15]:
            mark = " *palette-name" if e["colorish_name"] else ""
            print(f"   {rel}   {len(e['hex'])} hex{mark}")
            if e["hex"]:
                print(f"      {' '.join(e['hex'][:12])}")
        print()

    if args.out:
        payload = {
            "_note": "只含键名/数值/颜色，不含代码。用于对齐观感参数。",
            "root": str(root),
            "fileCount": len(files),
            "hits": {k: v for k, v in hits.items()},
            "colors": colors,
        }
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written: {out}")

    # 按类别汇总一句，方便判断还缺什么
    missing = [c for c, _rx, _d in RULES if not hits.get(c)]
    if missing:
        print()
        print("[!] not found: " + ", ".join(missing))
        print("    these may be hardcoded differently, or sit in a binary/asset file.")
        print("    open the palette/visualizer source by hand and fill params.json.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
