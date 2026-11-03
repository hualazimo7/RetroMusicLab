#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""retro_palette.py — 6 套复古主题的**纯数据**（不含任何 tkinter）。

## 为什么要单独一个模块

v5.0 做 Qt 界面时，`qt_ui.theme` 需要复用这 6 套配色（老用户切过去还是原来
那个味道）。但它原来是长在 `retro_theme.py` 里的，而那个文件**顶层
`import tkinter`** —— Qt 版把 tkinter 排除了，一 import 就 ImportError，
整个界面起不来（门禁实测：`check_theme` / `check_main_window` 双双抛异常）。

所以把「颜色」和「怎么画」拆开：

    retro_palette.py   ← 本文件：只有 dict / tuple / 两个查表函数
    retro_theme.py     ← tkinter 皮肤：从这里 import THEMES 后照旧用

`retro_theme.py` 的对外接口（`THEMES` / `THEME_ORDER` / `theme_keys()` /
`theme_label()`）一个都没变，tkinter 版一行代码都不用改。

## 改颜色的规矩

每套配色都过了 WCAG 2.1 自检（`python retro_theme.py --all`）：
正文 4.5:1、辅助 3.0:1。改任何一格都必须重跑那条命令。
"""

from __future__ import annotations

THEMES: dict[str, dict] = {
    "famicom": {
        "label": "红白机 Famicom",
        "palette": {
            # 暖米黄机身 + 砖红：最贴近「红白机 + 说明书纸」的一组
            "bg":      "#efe6d3",   # 窗口底（米黄）
            "panel":   "#faf5e9",   # 分区底板（浅米白）
            "card":    "#fffdf7",   # 卡片 / 输入框底（近白）
            "card2":   "#f2e7d0",   # 次级卡片 / hover
            "line":    "#cdb489",   # 描边（暖沙）
            "text":    "#2c2437",   # 主文本（墨紫黑）
            "muted":   "#6b6152",
            "dim":     "#8a8173",
            "cyan":    "#b33428",   # 主强调：Famicom 砖红
            "magenta": "#1c6b78",   # 次强调：深青
            "amber":   "#9d6412",
            "green":   "#2f7a3d",
            "red":     "#8f1d18",
            "sel":     "#f7e2b6",   # 列表选中底（暖沙金）
        },
        "on_accent": "#fffaf0",
    },
    "nes": {
        "label": "美版 NES",
        # 冷灰塑料机身 + NES 紫：和红白机一眼能分开，且紫色在大面积浅灰上
        # 读作「美版任天堂」而不是「又一个红」。
        "palette": {
            "bg":      "#d6d4cf",
            "panel":   "#e7e5e0",
            "card":    "#f6f5f2",
            "card2":   "#dbd9d3",
            "line":    "#a3a19b",
            "text":    "#1d1f24",
            "muted":   "#54585e",
            "dim":     "#6e7278",
            "cyan":    "#4a2a7a",
            "magenta": "#a8291f",
            "amber":   "#7a5a10",
            "green":   "#1f6b3a",
            "red":     "#8c1a14",
            "sel":     "#cfc4e4",
        },
        "on_accent": "#fbf8ff",
    },
    "gameboy": {
        "label": "Game Boy（DMG）",
        # DMG 那块只有 4 级灰绿的 LCD：整窗都压在橄榄绿里，
        # 强调色用最深的 #2a4c19，等于屏幕上最黑的那一级。
        "palette": {
            "bg":      "#c3cc9c",
            "panel":   "#d3dcb4",
            "card":    "#e5eacd",
            "card2":   "#c8d2a2",
            "line":    "#8d9a66",
            "text":    "#16290f",
            "muted":   "#3f5326",
            "dim":     "#5b6c39",
            "cyan":    "#2a4c19",
            "magenta": "#6b4710",
            "amber":   "#6b4710",
            "green":   "#1f5c24",
            "red":     "#7d2610",
            "sel":     "#aebd7d",
        },
        "on_accent": "#f1f6dc",
    },
    "pce": {
        "label": "PC Engine",
        # 白机身 + 橙红 logo：比红白机更「冷白」，强调色偏橙。
        "palette": {
            "bg":      "#e8e3da",
            "panel":   "#f5f1e9",
            "card":    "#fffdf8",
            "card2":   "#ded9cd",
            "line":    "#bdb3a3",
            "text":    "#29251f",
            "muted":   "#5b5449",
            "dim":     "#847c6f",
            "cyan":    "#a83712",
            "magenta": "#1f5f7a",
            "amber":   "#8a5a10",
            "green":   "#2c6b3c",
            "red":     "#8f2214",
            "sel":     "#f3d6bd",
        },
        "on_accent": "#fffaf2",
    },
    "megadrive": {
        "label": "Mega Drive",
        # MD 的「16-BIT」是蓝色斜体 + 金色字：这里做蓝 + 金，
        # 中性灰底（不用黑机身，用户不要深色）。
        "palette": {
            "bg":      "#dbdad5",
            "panel":   "#ebe9e3",
            "card":    "#f8f7f3",
            "card2":   "#d4d3cc",
            "line":    "#a6a49d",
            "text":    "#1b1c20",
            "muted":   "#53565a",
            "dim":     "#6f7276",
            "cyan":    "#1c3f8f",
            "magenta": "#8a5c0c",
            "amber":   "#8a5c0c",
            "green":   "#1e6b3a",
            "red":     "#8c1a14",
            "sel":     "#ccd6ea",
        },
        "on_accent": "#f7faff",
    },
    "sfc": {
        "label": "超任 Super Famicom",
        # SFC 机身是暖灰塑料，四色按钮里紫最能代表它。
        "palette": {
            "bg":      "#d8d4cb",
            "panel":   "#eae6dd",
            "card":    "#faf8f3",
            "card2":   "#d1cdc3",
            "line":    "#a8a29a",
            "text":    "#25222a",
            "muted":   "#565153",
            "dim":     "#716c68",
            "cyan":    "#5a3d8c",
            "magenta": "#2c6b5e",
            "amber":   "#7a570c",
            "green":   "#2a6b3a",
            "red":     "#8c1a14",
            "sel":     "#d9cfe9",
        },
        "on_accent": "#fbf9ff",
    },
}

#: 主题显示顺序（下拉框按这个顺序列，第一项是默认）
THEME_ORDER: tuple[str, ...] = ("famicom", "nes", "gameboy", "pce",
                                "megadrive", "sfc")
DEFAULT_THEME = "famicom"


def theme_keys() -> tuple[str, ...]:
    """按显示顺序返回全部主题 key。"""
    return THEME_ORDER


def theme_label(key: str) -> str:
    """返回主题的中文显示名；未知 key 原样返回。"""
    entry = THEMES.get(key)
    return entry["label"] if entry else str(key)
