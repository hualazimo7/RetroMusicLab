#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qt_ui.widgets — 几个薄封装，把「现代感」里 QSS 做不到的部分补上。

只有三样东西需要代码而不是 QSS：

1. **勾号图标** —— QSS 的 ``::indicator`` 只能贴图，画不了线。启动时用
   QPainter 生成 16×16 的 PNG，落在临时目录（v4.44 约定：不写 C 盘）。
2. **卡片阴影** —— QGraphicsDropShadowEffect。浅色下用很轻的一层，
   深色下几乎不用（深色背景上的阴影等于糊一层灰）。
3. **分段控件** —— 两个 checkable 按钮组成的浅色/深色切换。
"""

import os

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QPushButton, QVBoxLayout,
)

import tmpdir

_ICON_CACHE = {}


def icon_dir():
    d = os.path.join(tmpdir.init(sweep_old=False), "qt_ui_icons")
    os.makedirs(d, exist_ok=True)
    return d


def check_icon(color):
    """生成（并缓存）一个对勾 PNG，返回正斜杠路径。必须在 QApplication 之后调。"""
    key = color.lower()
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    name = "check_%s.png" % key.lstrip("#")
    path = os.path.join(icon_dir(), name)
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(color), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPolyline([QPointF(3.6, 8.6), QPointF(6.6, 11.5), QPointF(12.4, 4.5)])
    p.end()
    pm.save(path)
    out = path.replace("\\", "/")
    _ICON_CACHE[key] = out
    return out


def chevron_icon(color):
    """下拉箭头（V 形）。QSS 用 border 画三角在 Qt6 上不生效，只能贴图。"""
    key = "chev_" + color.lower()
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    name = "chev_%s.png" % color.lower().lstrip("#")
    path = os.path.join(icon_dir(), name)
    pm = QPixmap(12, 12)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(color), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPolyline([QPointF(3.0, 4.6), QPointF(6.0, 7.6), QPointF(9.0, 4.6)])
    p.end()
    pm.save(path)
    out = path.replace("\\", "/")
    _ICON_CACHE[key] = out
    return out


def radio_dot(color):
    """单选按钮里的实心圆点（16×16，直径 6）。

    ``QRadioButton::indicator`` 与 ``QCheckBox::indicator`` 同一个限制：
    只能贴图。外圈圆角用 QSS 的 ``border-radius`` 画，中间的点点用这张图。
    """
    key = "radio_" + color.lower()
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    name = "radio_%s.png" % color.lower().lstrip("#")
    path = os.path.join(icon_dir(), name)
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(8.0, 8.0), 3.2, 3.2)
    p.end()
    pm.save(path)
    out = path.replace("\\", "/")
    _ICON_CACHE[key] = out
    return out


def card(parent=None, padding=16, spacing=10):
    """圆角卡片 + 极轻阴影。返回 (QFrame, 内部 QVBoxLayout)。"""
    f = QFrame(parent)
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(padding, padding - 2, padding, padding - 2)
    lay.setSpacing(spacing)
    shadow = QGraphicsDropShadowEffect(f)
    shadow.setBlurRadius(16)
    shadow.setOffset(0, 2)
    shadow.setColor(QColor(0, 0, 0, 22))
    f.setGraphicsEffect(shadow)
    return f, lay


def hairline(parent=None):
    """1px 分隔线（用 QFrame 而不是 QSS border，避免撑高布局）。"""
    line = QFrame(parent)
    line.setObjectName("hairline")
    line.setFixedHeight(1)
    return line


class Segmented(QHBoxLayout):
    """两段式切换（浅色 / 深色）。选中项靠 QSS 的 :checked 上色。"""

    def __init__(self, items, on_pick, parent=None):
        super().__init__()
        self.setSpacing(6)
        self.buttons = {}
        for key, text in items:
            b = QPushButton(text, parent)
            b.setCheckable(True)
            b.setFixedWidth(62)
            b.clicked.connect(lambda _=False, k=key: self.pick(k, emit=True))
            self.addWidget(b)
            self.buttons[key] = b
        self._on_pick = on_pick
        self.buttons[items[0][0]].setChecked(True)

    def pick(self, key, emit=False):
        for k, b in self.buttons.items():
            b.setChecked(k == key)
        if emit and self._on_pick:
            self._on_pick(key)

    def current(self):
        for k, b in self.buttons.items():
            if b.isChecked():
                return k
        return None
