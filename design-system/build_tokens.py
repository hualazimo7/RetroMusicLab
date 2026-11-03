#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从代码生成设计令牌（唯一真源 = ``msu1-to-cd/retro_theme.py``）。

设计规范不能靠手抄色值 —— 手抄的东西一定会和代码漂移。本脚本把
``THEMES`` / ``FONT*`` 直接读出来，套上语义层（surface / text / accent…）
和尺度层（字号、间距、尺寸、动效），输出：

    design-system/tokens.json          机器可读令牌
    design-system/index.html           可视化规范页里的内嵌令牌块（自动回填）

因此**改配色只改 retro_theme.py，然后跑本脚本**，规范页与文档随之更新。

用法（必须用系统 Python 3.14，托管 3.13 没有 tkinter）::

    cd design-system
    /c/Python314/python.exe build_tokens.py

``check_design_system.py`` 会反过来校验 tokens.json 与代码零漂移。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "msu1-to-cd"
sys.path.insert(0, str(APP))

import retro_theme as rt  # noqa: E402  (需要先把 msu1-to-cd 放进 sys.path)

TOKENS_JSON = HERE / "tokens.json"
INDEX_HTML = HERE / "index.html"

#: 语义层：设计侧说「底板 / 强文字」，代码侧是 v3 遗留的颜色名。
#: ⚠ 两边都必须冻结 —— 改键名会波及 GUI 里 26 处引用（见 retro_theme.py 注释）。
SEMANTICS: dict[str, str] = {
    "surface.base": "bg",
    "surface.panel": "panel",
    "surface.raised": "card",
    "surface.sunken": "card2",
    "border.hairline": "line",
    "text.primary": "text",
    "text.secondary": "muted",
    "text.tertiary": "dim",
    "accent.primary": "cyan",
    "accent.secondary": "magenta",
    "accent.numeric": "amber",
    "status.success": "green",
    "status.danger": "red",
    "selection.bg": "sel",
    "selection.fg": "text",
    "on.accent": "onAccent",
}

#: WCAG 2.1 AA 自检清单（与 retro_theme._contrast_checks 的 25 条一一对应）。
#: fg / bg 是**角色名**：先在 palette 里找，找不到再去 derived 里找。
CONTRAST_PAIRS: tuple[tuple[str, str, str, float], ...] = (
    ("主文本 / 窗口底", "text", "bg", 4.5),
    ("主文本 / 分区底板", "text", "panel", 4.5),
    ("主文本 / 卡片底", "text", "card", 4.5),
    ("主文本 / 次级卡片", "text", "card2", 4.5),
    ("次要文本 / 窗口底", "muted", "bg", 4.5),
    ("次要文本 / 分区底板", "muted", "panel", 4.5),
    ("次要文本 / 卡片底", "muted", "card", 4.5),
    ("弱化文本 / 窗口底", "dim", "bg", 3.0),
    ("弱化文本 / 分区底板", "dim", "panel", 3.0),
    ("弱化文本 / 卡片底", "dim", "card", 3.0),
    ("主强调 / 窗口底", "cyan", "bg", 4.5),
    ("主强调 / 分区底板", "cyan", "panel", 4.5),
    ("主强调 / 卡片底", "cyan", "card", 4.5),
    ("次强调 / 分区底板", "magenta", "panel", 4.5),
    ("次强调 / 卡片底", "magenta", "card", 4.5),
    ("数值色 / 分区底板", "amber", "panel", 4.5),
    ("数值色 / 卡片底", "amber", "card", 4.5),
    ("成功 / 分区底板", "green", "panel", 4.5),
    ("失败 / 分区底板", "red", "panel", 4.5),
    ("失败 / 卡片底", "red", "card", 4.5),
    ("选中文字 / 选中底", "text", "sel", 4.5),
    ("选中文字（别名） / 选中底", "onSel", "sel", 4.5),
    ("主按钮文字 / 强调底", "onAccent", "cyan", 4.5),
    ("主按钮文字 / hover 底", "onAccent", "accentHover", 4.5),
    ("主按钮文字 / pressed 底", "onAccent", "accentPress", 4.5),
)

#: 字体角色。Tk 用「磅」，Windows 96dpi 下 1pt ≈ 1.333px，px 供前端/文档对照。
TYPE_ROLES: tuple[dict, ...] = (
    {"token": "font.title", "role": "分区大标题 / 对话框标题",
     "family": rt.FONT_TITLE[0], "sizePt": rt.FONT_TITLE[1], "px": 17, "weight": 700,
     "lineHeightPx": 24},
    {"token": "font.body", "role": "正文、列表、按钮、表单",
     "family": rt.FONT[0], "sizePt": rt.FONT[1], "px": 12, "weight": 400,
     "lineHeightPx": 20},
    {"token": "font.body-bold", "role": "按钮强调、分区标题、表头",
     "family": rt.FONT_B[0], "sizePt": rt.FONT_B[1], "px": 12, "weight": 700,
     "lineHeightPx": 20},
    {"token": "font.caption", "role": "辅助说明、小按钮、次要标签",
     "family": rt.FONT_S[0], "sizePt": rt.FONT_S[1], "px": 11, "weight": 400,
     "lineHeightPx": 16},
    {"token": "font.mono", "role": "日志、数值、时长、路径",
     "family": rt.FONT_MONO[0], "sizePt": rt.FONT_MONO[1], "px": 12, "weight": 400,
     "lineHeightPx": 18},
)

#: 间距阶：4px 基准。左列是规范名，右列是 tkinter padding 里实际用到的数。
SPACE_SCALE: dict[str, int] = {
    "space.0": 0, "space.0-5": 2, "space.1": 4, "space.1-5": 6,
    "space.2": 8, "space.2-5": 10, "space.3": 12, "space.4": 16,
    "space.6": 24, "space.8": 32,
}

#: 组件实测尺寸（全部来自 retro_theme.install，改这里必须同步改代码）。
SIZES: dict[str, dict] = {
    "window.main": {"value": "1260×1020", "note": "目标尺寸；最小 1080×780，两列 PanedWindow 可拖"},
    "window.download": {"value": "1400×720", "note": "下载窗三列"},
    "window.dialog": {"value": "760×540", "note": "登录/确认类对话框基准"},
    "control.height": {"value": 26, "note": "按钮 padding (10,5) + 12px 字 + 1px 边"},
    "row.height": {"value": 22, "note": "Treeview 行高"},
    "progress.thickness": {"value": 8, "note": "进度条厚度"},
    "slider.size": {"value": 14, "note": "试听进度滑块 14×14"},
    "scrollbar.width": {"value": 10, "note": "滚动条宽度"},
    "combobox.arrow": {"value": 13, "note": "下拉箭头尺寸"},
    "tree.indent": {"value": 14, "note": "树形缩进"},
}

PADDINGS: dict[str, dict] = {
    "button.hero": {"x": 16, "y": 6, "token": "space.4 / space.1-5（主操作，一屏仅一个）"},
    "button": {"x": 10, "y": 5, "token": "space.2-5 / space.1-5"},
    "button.small": {"x": 6, "y": 2, "token": "space.1-5 / space.0-5"},
    "entry": {"x": 5, "y": 3, "token": "控件内 5/3（非 4 的整数倍，历史值，勿改）"},
    "combobox": {"x": 5, "y": 3, "token": "同上"},
    "checkbox": {"x": 2, "y": 1, "token": "space.0-5 / —"},
    "tree.heading": {"x": 6, "y": 4, "token": "space.1-5 / space.1"},
    "labelframe": {"x": 10, "y": 5, "token": "space.2-5 / space.1-5"},
    "labelframe.label": {"x": 2, "y": 0, "token": "space.0-5 / space.0"},
    "section.gap": {"x": 8, "y": 6, "token": "分区之间 space.2 / space.1-5"},
}


def _derived(theme_key: str) -> dict[str, str]:
    """派生色：不写进 THEMES，由强调色实时算出（切主题时重算）。"""
    old = rt._current_theme
    rt.set_current(theme_key)
    p = rt.PALETTE
    derived = {
        "onAccent": rt.THEMES[theme_key]["on_accent"],
        "onSel": rt.PALETTE["text"],
        "accentHover": rt.mix_color(p["cyan"], "#ffffff", 0.10),
        "accentPress": rt._shade(p["cyan"], 0.82),
        "button.bg": p["card2"],
        "button.hover": rt._mix(p["card2"], p["line"], 0.55),
        "check.ring": rt._mix(p["line"], p["muted"], 0.30),
    }
    rt.set_current(old)
    return derived


def build() -> dict:
    themes: dict[str, dict] = {}
    for key in rt.THEME_ORDER:
        info = rt.THEMES[key]
        themes[key] = {
            "label": info["label"],
            "palette": dict(info["palette"]),
            "derived": _derived(key),
        }

    return {
        "meta": {
            "product": "RetroMusic Lab",
            "app": "MSU1Converter",
            "version": "1.0.0",
            "generated": _dt.date.today().isoformat(),
            "source": "msu1-to-cd/retro_theme.py",
            "generator": "design-system/build_tokens.py",
            "note": "本文件由脚本生成，请勿手改。改 retro_theme.py 后重跑生成命令。",
        },
        "color": {
            "order": list(rt.THEME_ORDER),
            "default": rt.DEFAULT_THEME,
            "semantics": SEMANTICS,
            "themes": themes,
        },
        "type": {
            "families": {
                "ui": rt.FONT[0],
                "mono": rt.FONT_MONO[0],
                "bannerStack": list(rt.BANNER_FONT_STACK),
            },
            "roles": list(TYPE_ROLES),
        },
        "space": {"base": 4, "scale": SPACE_SCALE},
        "size": SIZES,
        "padding": PADDINGS,
        "radius": {"none": 0, "note": "全局 0：像素硬边，禁止圆角"},
        "border": {"hairline": 1, "note": "1px solid line；clam 下 relief 必须 solid"},
        "elevation": {
            "flat": "无阴影，靠 panel/card/line 三级明度分层",
            "note": "复古皮肤不用投影；阴影会被读成『网页卡片』而不是『机身贴纸』",
        },
        "motion": {
            "instant": 0,
            "bannerFrame": rt.BANNER_INTERVAL_MS,
            "hover": 120,
            "note": "只允许即时切换；不做位移/缩放动画",
        },
        "contrast": {
            "standard": "WCAG 2.1 AA",
            "pairs": [
                {"label": lb, "fg": fg, "bg": bg, "min": mn}
                for lb, fg, bg, mn in CONTRAST_PAIRS
            ],
        },
    }


def inject_html(tokens: dict) -> bool:
    """把令牌回填进 index.html 的 ``<script id="design-tokens">`` 块。"""
    if not INDEX_HTML.exists():
        return False
    html = INDEX_HTML.read_text(encoding="utf-8")
    payload = json.dumps(tokens, ensure_ascii=False, indent=1)
    new, n = re.subn(
        r'(<script id="design-tokens" type="application/json">)(.*?)(</script>)',
        lambda m: m.group(1) + "\n" + payload + "\n" + m.group(3),
        html, count=1, flags=re.S,
    )
    if not n:
        return False
    if new != html:
        INDEX_HTML.write_text(new, encoding="utf-8")
    return True


def main() -> int:
    tokens = build()
    TOKENS_JSON.write_text(
        json.dumps(tokens, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    injected = inject_html(tokens)
    n_hex = sum(len(t["palette"]) for t in tokens["color"]["themes"].values())
    print("tokens.json  ->", TOKENS_JSON)
    print("  主题 %d 套 / 色值 %d 个 / 字体 %d 角色 / 间距 %d 阶"
          % (len(tokens["color"]["themes"]), n_hex,
             len(tokens["type"]["roles"]), len(tokens["space"]["scale"])))
    print("index.html  ->", "已回填内嵌令牌" if injected else "跳过（未找到标记块）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
