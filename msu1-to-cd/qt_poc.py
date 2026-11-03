#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qt_poc.py — Qt 界面可行性验证（不是最终界面，只验四件事）。

1. PySide6 能起窗口、中文能显示
2. 双模式（浅/深）QSS 能即时切换
3. 圆角卡片 + 扁平按钮 + 自绘复选框 + 分段控件 的观感是否够「现代」
4. 打包后体积与启动时间（这条决定整条路线成不成立）

跑法：
    python qt_poc.py            # 开窗口
    python qt_poc.py --shot 浅  # 无头渲染一张 PNG 就退出（给门禁/截图用）
"""

import os
import sys

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QGraphicsDropShadowEffect,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QProgressBar, QPushButton, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------- 令牌
LIGHT = {
    "bg": "#F4F4F5", "card": "#FFFFFF", "border": "#E4E4E7",
    "fg": "#18181B", "fg2": "#71717A", "fg3": "#A1A1AA",
    "accent": "#4F46E5", "accent_hover": "#4338CA", "accent_fg": "#FFFFFF",
    "field": "#FFFFFF", "field_border": "#D4D4D8",
    "sel": "#EEF2FF", "log_bg": "#FAFAFA",
}
DARK = {
    "bg": "#18181B", "card": "#27272A", "border": "#3F3F46",
    "fg": "#FAFAFA", "fg2": "#A1A1AA", "fg3": "#71717A",
    "accent": "#818CF8", "accent_hover": "#6366F1", "accent_fg": "#1E1B4B",
    "field": "#1F1F23", "field_border": "#52525B",
    "sel": "#312E81", "log_bg": "#1F1F23",
}

FONT = "'Microsoft YaHei UI', 'Segoe UI', sans-serif"


def _icon_dir():
    """勾号图标放临时目录（v4.44 约定：不写 C 盘系统 temp）。"""
    import tmpdir
    d = os.path.join(tmpdir.init(sweep_old=False), "qt_ui_icons")
    os.makedirs(d, exist_ok=True)
    return d


def _make_check(color, name):
    """画一个 16x16 的对勾 PNG —— QSS 的 indicator 只能靠图片，不能画线。"""
    path = os.path.join(_icon_dir(), name)
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(color), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPolyline([QPointF(3.5, 8.6), QPointF(6.6, 11.6), QPointF(12.5, 4.4)])
    p.end()
    pm.save(path)
    return path.replace("\\", "/")


CHECK_LIGHT = ""
CHECK_DARK = ""


def _prepare_icons():
    """⚠ 必须在 QApplication 之后调用 —— QPixmap 没有 app 会直接崩。"""
    global CHECK_LIGHT, CHECK_DARK
    CHECK_LIGHT = _make_check("#FFFFFF", "check_light.png")
    CHECK_DARK = _make_check("#1E1B4B", "check_dark.png")
    LIGHT["check"] = CHECK_LIGHT
    DARK["check"] = CHECK_DARK


def qss(t):
    """把令牌渲染成 QSS。所有圆角/间距/字号都在这里，改风格只改这张表。"""
    return f"""
    QWidget {{
        background: {t['bg']};
        color: {t['fg']};
        font-family: {FONT};
        font-size: 13px;
    }}
    QFrame#card {{
        background: {t['card']};
        border: 1px solid {t['border']};
        border-radius: 12px;
    }}
    QLabel#h1 {{ font-size: 22px; font-weight: 600; }}
    QLabel#h2 {{ font-size: 13px; font-weight: 600; }}
    QLabel#muted {{ color: {t['fg2']}; font-size: 12px; }}
    /* ⚠ QLabel / QCheckBox 的 sizePolicy 是 Preferred，会被 QVBoxLayout 拉高，
       默认背景会露出一条灰带（POC 第一版就是这样）。统一透明。 */
    QLabel, QCheckBox {{ background: transparent; }}

    QPushButton {{
        background: transparent;
        color: {t['fg']};
        border: 1px solid {t['border']};
        border-radius: 8px;
        padding: 7px 14px;
    }}
    QPushButton:hover {{ background: {t['sel']}; }}
    QPushButton#primary {{
        background: {t['accent']};
        color: {t['accent_fg']};
        border: none;
        font-weight: 600;
        padding: 9px 20px;
    }}
    QPushButton#primary:hover {{ background: {t['accent_hover']}; }}
    QPushButton#ghost {{ border: none; color: {t['fg2']}; }}
    QPushButton#ghost:hover {{ color: {t['fg']}; background: {t['sel']}; }}
    /* 分段控件（浅色/深色）靠 :checked 表示选中 */
    QPushButton:checked {{
        background: {t['accent']};
        color: {t['accent_fg']};
        border: none;
        font-weight: 600;
    }}

    QLineEdit, QComboBox, QSpinBox {{
        background: {t['field']};
        border: 1px solid {t['field_border']};
        border-radius: 8px;
        padding: 6px 10px;
        selection-background-color: {t['accent']};
        selection-color: {t['accent_fg']};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border: 1px solid {t['accent']};
    }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {t['card']};
        border: 1px solid {t['border']};
        border-radius: 8px;
        selection-background-color: {t['sel']};
        outline: none;
    }}

    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px;
        border: 1.5px solid {t['field_border']};
        border-radius: 4px;
        background: {t['field']};
    }}
    QCheckBox::indicator:checked {{
        background: {t['accent']};
        border-color: {t['accent']};
        image: url({t.get('check', '')});
    }}

    QListWidget {{
        background: transparent;
        border: none;
        outline: none;
        padding: 0px;
    }}
    QListWidget::item {{
        padding: 9px 10px; border-radius: 8px; color: {t['fg']};
    }}
    QListWidget::item:hover {{ background: {t['sel']}; }}
    QListWidget::item:selected {{
        background: {t['sel']}; color: {t['fg']};
    }}

    QTextEdit {{
        background: transparent;
        border: none;
        padding: 0px;
        font-family: 'Cascadia Mono', 'Consolas', monospace;
        font-size: 12px;
        color: {t['fg2']};
    }}

    QSpinBox::up-button, QSpinBox::down-button {{
        width: 0px; border: none; background: transparent;
    }}

    QProgressBar {{
        background: {t['border']};
        border: none;
        border-radius: 5px;
        height: 10px;
        text-align: center;
    }}
    QProgressBar::chunk {{
        background: {t['accent']};
        border-radius: 5px;
    }}
    QScrollBar:vertical {{
        background: transparent; width: 10px; margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {t['field_border']}; border-radius: 5px; min-height: 24px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    """


def card(parent=None):
    f = QFrame(parent)
    f.setObjectName("card")
    shadow = QGraphicsDropShadowEffect(f)
    shadow.setBlurRadius(18)
    shadow.setOffset(0, 2)
    shadow.setColor(QColor(0, 0, 0, 26))
    f.setGraphicsEffect(shadow)
    return f


def build():
    w = QWidget()
    w.resize(1080, 760)
    w.setWindowTitle("RetroMusic Lab · Qt POC")

    root = QVBoxLayout(w)
    root.setContentsMargins(20, 18, 20, 18)
    root.setSpacing(14)

    # 顶栏
    top = QHBoxLayout()
    title = QLabel("RetroMusic Lab")
    title.setObjectName("h1")
    sub = QLabel("SPC · PCM · CD AUDIO — 8/16-BIT 音乐工作站")
    sub.setObjectName("muted")
    col = QVBoxLayout()
    col.setSpacing(2)
    col.addWidget(title)
    col.addWidget(sub)
    top.addLayout(col)
    top.addStretch(1)
    seg = QHBoxLayout()
    seg.setSpacing(6)
    b_light = QPushButton("浅色")
    b_dark = QPushButton("深色")
    for b in (b_light, b_dark):
        b.setCheckable(True)
        b.setFixedWidth(64)
    b_light.setChecked(True)
    seg.addWidget(b_light)
    seg.addWidget(b_dark)
    top.addLayout(seg)
    root.addLayout(top)

    # 主体两栏
    body = QHBoxLayout()
    body.setSpacing(14)

    left = QVBoxLayout()
    left.setSpacing(14)

    c1 = card()
    l1 = QVBoxLayout(c1)
    l1.setContentsMargins(16, 14, 16, 14)
    l1.addWidget(_h2("音源"))
    lst = QListWidget()
    for name, fmt, n, size, dur in (
        ("Castlevania - SotN (PS1)", "光盘镜像", "41", "1.28 GB", "52:16"),
        ("Chrono Trigger (SPC)", "SPC", "64", "12.4 MB", "1:38:02"),
        ("Silence Hill (XA)", "光盘镜像", "200", "588 MB", "1:03:51"),
    ):
        it = QListWidgetItem(f"{name}    ·    {fmt}    ·    {n} 曲    ·    {size}    ·    {dur}")
        lst.addItem(it)
    lst.setFixedHeight(150)
    l1.addWidget(lst)
    left.addWidget(c1)

    c2 = card()
    l2 = QVBoxLayout(c2)
    l2.setContentsMargins(16, 14, 16, 14)
    l2.addWidget(_h2("输出格式"))
    for txt, on in (("音频 CD 镜像（BIN+CUE）", True), ("WAV（Pico-Plus）", True),
                    ("多轨合并成单文件", False)):
        cb = QCheckBox(txt)
        cb.setChecked(on)
        l2.addWidget(cb)
    left.addWidget(c2)

    c3 = card()
    l3 = QVBoxLayout(c3)
    l3.setContentsMargins(16, 14, 16, 14)
    l3.addWidget(_h2("选项"))
    row = QHBoxLayout()
    row.setSpacing(10)
    row.addWidget(QLabel("输出目录"))
    ed = QLineEdit("C:\\Users\\xqh\\Desktop\\out")
    row.addWidget(ed, 1)
    row.addWidget(QPushButton("浏览…"))
    l3.addLayout(row)

    row2 = QHBoxLayout()
    row2.setSpacing(10)
    row2.addWidget(QLabel("播放长度"))
    cbx = QComboBox()
    cbx.addItems(["单遍（按 ID666 标注时长，推荐）", "循环两遍", "自然长度"])
    row2.addWidget(cbx, 1)
    row2.addWidget(QLabel("速度"))
    sp = QSpinBox()
    sp.setRange(50, 200)
    sp.setValue(100)
    sp.setSuffix(" %")
    row2.addWidget(sp)
    l3.addLayout(row2)

    row3 = QHBoxLayout()
    row3.setSpacing(10)
    row3.addWidget(QLabel("界面主题"))
    th = QComboBox()
    th.addItems(["Modern 浅色", "Modern 深色", "红白机 Famicom",
                 "美版 NES", "Game Boy", "PC Engine", "Mega Drive", "超任"])
    row3.addWidget(th, 1)
    l3.addLayout(row3)
    left.addWidget(c3)
    left.addStretch(1)

    body.addLayout(left, 3)

    right = QVBoxLayout()
    right.setSpacing(14)
    c4 = card()
    l4 = QVBoxLayout(c4)
    l4.setContentsMargins(16, 14, 16, 14)
    l4.addWidget(_h2("日志"))
    te = QTextEdit()
    te.setReadOnly(True)
    te.setPlainText(
        "开始: 3 个音源 | CD + WAV\n"
        "输出: C:\\Users\\xqh\\Desktop\\out\n"
        "▶ Castlevania - SotN (PS1)  (光盘镜像)\n"
        "   策略：抽 41 条 CD-DA 轨，按原长转 WAV\n"
        "   WAV 01/41: 01_夜曲.wav  3:42  37.4MB\n"
        "   ✅ 已完成 12/41"
    )
    l4.addWidget(te)
    right.addWidget(c4, 1)
    body.addLayout(right, 2)

    root.addLayout(body, 1)

    # 底部操作条
    bottom = QHBoxLayout()
    bottom.setSpacing(10)
    go = QPushButton("开始转换")
    go.setObjectName("primary")
    bottom.addWidget(go)
    for txt in ("取消", "打开输出目录", "上传云盘"):
        bottom.addWidget(QPushButton(txt))
    bottom.addStretch(1)
    pb = QProgressBar()
    pb.setValue(30)
    pb.setTextVisible(False)
    pb.setFixedWidth(260)
    pb.setFixedHeight(10)
    bottom.addWidget(pb)
    root.addLayout(bottom)

    def switch(theme):
        w.setStyleSheet(qss(theme))

    b_light.clicked.connect(lambda: (b_light.setChecked(True), b_dark.setChecked(False), switch(LIGHT)))
    b_dark.clicked.connect(lambda: (b_dark.setChecked(True), b_light.setChecked(False), switch(DARK)))
    switch(LIGHT)
    return w, switch


def _h2(text):
    lab = QLabel(text)
    lab.setObjectName("h2")
    return lab


def main():
    app = QApplication(sys.argv)
    _prepare_icons()                 # QPixmap 依赖 QApplication，必须在这里
    w, switch = build()
    if "--shot" in sys.argv:
        # 无头渲染：先浅后深各存一张，供门禁/汇报用
        for name, theme in (("light", LIGHT), ("dark", DARK)):
            switch(theme)
            w.show()
            app.processEvents()
            pix = w.grab()
            pix.save(f"qt_poc_{name}.png")
            print(f"已保存 qt_poc_{name}.png  {pix.width()}x{pix.height()}")
        return 0
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
