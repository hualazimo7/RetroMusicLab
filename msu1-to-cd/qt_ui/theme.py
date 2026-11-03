#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qt_ui.theme — Qt 界面的配色令牌与 QSS 渲染。

## 设计原则

**令牌层只管语义，QSS 只管形态。** 换风格只动这两处，界面代码一行不改。

令牌键（与 `retro_theme.py` 的语义位对齐，便于 6 套复古主题自动映射）：

    bg          窗口底
    card        卡片 / 输入框底
    card2       次级卡片 / hover 底
    border      描边
    fg          主文本
    fg2         次要文本
    fg3         更弱（占位符 / 禁用）
    accent      主强调（主按钮 / 选中 / 进度）
    accent_fg   强调色上的文字
    sel         列表选中底
    log_bg      日志区底

## 8 套主题

`modern_light` / `modern_dark` 是本版新增的现代配色；`famicom` / `nes` /
`gameboy` / `pce` / `megadrive` / `sfc` 直接从 `retro_theme.THEMES` 映射过来
—— 老用户切过去还是原来那个味道，但控件形态是新的。
"""

import os

# ⚠ 只能 import **纯数据** 模块。`retro_theme` 顶层 `import tkinter`，
# 而 Qt 版把 tkinter 整个排除了 —— 一 import 就 ImportError，
# 界面直接起不来（打包后实测：check_theme / check_main_window 双双抛异常）。
import retro_palette as rp

# ---------------------------------------------------------------- 现代配色
MODERN_LIGHT = {
    "label": "Modern 浅色",
    "dark": False,
    "bg": "#F4F4F5", "card": "#FFFFFF", "card2": "#FAFAFA",
    "border": "#E4E4E7", "fg": "#18181B", "fg2": "#71717A", "fg3": "#A1A1AA",
    "accent": "#4F46E5", "accent_fg": "#FFFFFF",
    "sel": "#EEF2FF", "log_bg": "#FAFAFA",
    "check": "#FFFFFF",
}

MODERN_DARK = {
    "label": "Modern 深色",
    "dark": True,
    "bg": "#18181B", "card": "#27272A", "card2": "#2E2E33",
    "border": "#3F3F46", "fg": "#FAFAFA", "fg2": "#A1A1AA", "fg3": "#71717A",
    "accent": "#818CF8", "accent_fg": "#1E1B4B",
    "sel": "#312E81", "log_bg": "#1F1F23",
    "check": "#1E1B4B",
}


def _from_retro(key):
    """把 retro_palette 的 6 套配色映射成 Qt 令牌。

    语义位对照：cyan=主强调、line=描边、sel=选中底、card2=hover 底。
    这套映射是**唯一**的地方，改映射不要动 retro_palette 本身
    （那边还有 WCAG 对比度自检 `python retro_theme.py --all`）。
    """
    t = rp.THEMES[key]
    p = t["palette"]
    return {
        "label": t["label"],
        "dark": _is_dark(p["bg"]),
        "bg": p["bg"], "card": p["card"], "card2": p["card2"],
        "border": p["line"], "fg": p["text"],
        "fg2": p["muted"], "fg3": p["dim"],
        "accent": p["cyan"], "accent_fg": t["on_accent"],
        "sel": p["sel"], "log_bg": p["panel"],
        "check": t["on_accent"],
    }


def _is_dark(hex_color):
    """按相对亮度判深浅，决定勾号用白还是深色。"""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128


def registry():
    """有序的 {key: 令牌}。Modern 两套在前，复古六套在后。"""
    out = {"modern_light": MODERN_LIGHT, "modern_dark": MODERN_DARK}
    for k in rp.theme_keys():
        out["retro_" + k] = _from_retro(k)
    return out


FONT = "'Microsoft YaHei UI', 'Segoe UI', sans-serif"
FONT_MONO = "'Cascadia Mono', 'Consolas', monospace"


def qss(t, check_icon="", chevron_icon="", radio_icon=""):
    """把令牌渲染成完整 QSS。

    ``check_icon`` / ``chevron_icon`` / ``radio_icon`` 是 PNG 的绝对路径
    （正斜杠）。QSS 的 ``::indicator`` 和 ``::down-arrow`` 只能引用图片，
    画不了线 —— 见 ``widgets.check_icon()`` 等三个生成函数。
    """
    d = t
    return f"""
    QWidget {{
        background: {d['bg']};
        color: {d['fg']};
        font-family: {FONT};
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background: {d['bg']}; }}

    /* ---- 卡片 ---- */
    QFrame#card {{
        background: {d['card']};
        border: 1px solid {d['border']};
        border-radius: 12px;
    }}
    QFrame#hairline {{ background: {d['border']}; max-height: 1px; border: none; }}

    /* ---- 文本 ---- */
    QLabel {{ background: transparent; }}
    QLabel#h1 {{ font-size: 21px; font-weight: 600; }}
    QLabel#h2 {{ font-size: 13px; font-weight: 600; }}
    QLabel#muted {{ color: {d['fg2']}; font-size: 12px; }}
    QLabel#hint {{ color: {d['fg3']}; font-size: 11px; }}
    QLabel#status {{ color: {d['fg2']}; font-size: 12px; }}
    QLabel#mono {{ font-family: {FONT_MONO}; font-size: 11px; color: {d['fg2']}; }}

    /* ---- 按钮 ---- */
    QPushButton {{
        background: transparent;
        color: {d['fg']};
        border: 1px solid {d['border']};
        border-radius: 8px;
        padding: 7px 14px;
    }}
    QPushButton:hover {{ background: {d['sel']}; }}
    QPushButton:pressed {{ background: {d['card2']}; }}
    QPushButton:disabled {{ color: {d['fg3']}; border-color: {d['border']}; }}
    QPushButton#primary {{
        background: {d['accent']};
        color: {d['accent_fg']};
        border: none;
        font-weight: 600;
        padding: 9px 22px;
    }}
    QPushButton#primary:hover {{ background: {d['accent']}; }}
    QPushButton#primary:disabled {{ background: {d['border']}; color: {d['fg3']}; }}
    QPushButton#ghost {{ border: none; color: {d['fg2']}; }}
    QPushButton#ghost:hover {{ color: {d['fg']}; background: {d['sel']}; }}
    QPushButton#danger {{ border: none; color: {d['fg2']}; }}
    QPushButton#danger:hover {{ color: {d['fg']}; background: {d['sel']}; }}
    /* 播放条上的小方按钮：正方形、无边框、hover 才显底 */
    QPushButton#small {{
        border: none;
        padding: 0px;
        border-radius: 7px;
        font-size: 13px;
        min-height: 26px;
    }}
    QPushButton#small:hover {{ background: {d['sel']}; }}
    QPushButton#small:disabled {{ color: {d['fg3']}; }}
    QPushButton:checked {{
        background: {d['accent']};
        color: {d['accent_fg']};
        border: none;
        font-weight: 600;
    }}

    /* ---- 输入 ---- */
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
        background: {d['card']};
        border: 1px solid {d['border']};
        border-radius: 8px;
        padding: 6px 10px;
        selection-background-color: {d['accent']};
        selection-color: {d['accent_fg']};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border: 1px solid {d['accent']};
    }}
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
        color: {d['fg3']};
    }}
    QComboBox::drop-down {{ border: none; width: 24px; }}
    QComboBox::down-arrow {{
        image: url({chevron_icon});
        width: 12px; height: 12px;
        margin-right: 7px;
    }}
    QComboBox QAbstractItemView {{
        background: {d['card']};
        border: 1px solid {d['border']};
        border-radius: 8px;
        padding: 4px;
        selection-background-color: {d['sel']};
        selection-color: {d['fg']};
        outline: none;
    }}
    QSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
        width: 0px; border: none; background: transparent;
    }}

    /* ---- 复选框 ---- */
    QCheckBox {{ background: transparent; spacing: 8px; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px;
        border: 1.5px solid {d['border']};
        border-radius: 4px;
        background: {d['card']};
    }}
    QCheckBox::indicator:hover {{ border-color: {d['accent']}; }}
    QCheckBox::indicator:checked {{
        background: {d['accent']};
        border-color: {d['accent']};
        image: url({check_icon});
    }}

    /* ---- 单选 ---- */
    QRadioButton {{ background: transparent; spacing: 8px; }}
    QRadioButton::indicator {{
        width: 16px; height: 16px;
        border: 1.5px solid {d['border']};
        border-radius: 8px;
        background: {d['card']};
    }}
    QRadioButton::indicator:hover {{ border-color: {d['accent']}; }}
    QRadioButton::indicator:checked {{
        background: {d['accent']};
        border-color: {d['accent']};
        image: url({radio_icon});
    }}

    /* ---- 列表 / 表格 ---- */
    QListWidget, QTableWidget {{
        background: transparent;
        border: none;
        outline: none;
        padding: 0px;
    }}
    QListWidget::item {{
        padding: 9px 10px; border-radius: 8px; color: {d['fg']};
    }}
    QListWidget::item:hover {{ background: {d['sel']}; }}
    QListWidget::item:selected {{ background: {d['sel']}; color: {d['fg']}; }}
    QTableWidget::item {{ padding: 6px 8px; }}
    QTableWidget::item:selected {{ background: {d['sel']}; color: {d['fg']}; }}
    QHeaderView {{ background: transparent; border: none; }}
    QHeaderView::section {{
        background: transparent;
        color: {d['fg2']};
        border: none;
        border-bottom: 1px solid {d['border']};
        padding: 6px 8px;
        font-size: 12px;
    }}
    QTableCornerButton::section {{ background: transparent; border: none; }}

    /* ---- 日志 ---- */
    QTextEdit#log, QPlainTextEdit#log {{
        background: transparent;
        border: none;
        padding: 0px;
        font-family: {FONT_MONO};
        font-size: 12px;
        color: {d['fg']};
    }}

    /* ---- 进度条 ---- */
    QProgressBar {{
        background: {d['border']};
        border: none;
        border-radius: 5px;
        text-align: center;
        color: transparent;
    }}
    QProgressBar::chunk {{ background: {d['accent']}; border-radius: 5px; }}

    /* ---- 滑块（播放进度 / 音量）---- */
    QSlider {{ background: transparent; }}
    QSlider::groove:horizontal {{
        height: 4px; background: {d['border']}; border-radius: 2px;
    }}
    QSlider::sub-page:horizontal {{ background: {d['accent']}; border-radius: 2px; }}
    QSlider::add-page:horizontal {{ background: {d['border']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        width: 11px; height: 11px; margin: -4px 0;
        border-radius: 6px; background: {d['accent']};
        border: 2px solid {d['card']};
    }}
    QSlider::handle:horizontal:hover {{ background: {d['fg']}; }}

    /* ---- 滚动条 ---- */
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{
        background: {d['border']}; border-radius: 5px; min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {d['fg3']}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{
        background: {d['border']}; border-radius: 5px; min-width: 28px;
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

    /* ---- 菜单 / 提示 ---- */
    QMenu {{
        background: {d['card']};
        border: 1px solid {d['border']};
        border-radius: 8px;
        padding: 6px;
    }}
    QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {d['sel']}; }}
    QToolTip {{
        background: {d['card']};
        color: {d['fg']};
        border: 1px solid {d['border']};
        border-radius: 6px;
        padding: 5px 8px;
    }}

    /* ---- 分割条 ---- */
    QSplitter::handle {{ background: transparent; width: 8px; }}
    """
