#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""retro_theme 预览脚本：把所有主题 style 摆一屏，人眼验收 + 自动回读验证。

用法::

    python vendor/preview_theme.py            # 1.5s 后打印样式回读，2.5s 自动关闭
    python vendor/preview_theme.py --hold     # 常驻（用于截图 / 手工观察）
    python vendor/preview_theme.py --native   # 额外加一张 "⑤ 原生控件" 卡
                                              # (tk.Listbox / tk.Text，验证 option DB)

脚本不修改任何被测文件，也不写死路径，直接以父目录的 ``retro_theme`` 为准。
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import retro_theme as rt  # noqa: E402  (需要在 sys.path 调整之后导入)

WINDOW_W = 820
WINDOW_H = 760

#: 需要在报告里回读的 (style 名, 选项) 组合
_LOOKUP_TARGETS: tuple[tuple[str, str], ...] = (
    ("TFrame", "background"),
    ("Card.TFrame", "background"),
    ("TLabel", "foreground"),
    ("TLabel", "background"),
    ("Title.TLabel", "font"),
    ("Accent.TLabel", "foreground"),
    ("TButton", "background"),
    ("TButton", "foreground"),
    ("Accent.TButton", "background"),
    ("Accent.TButton", "foreground"),
    ("Small.TButton", "font"),
    ("TEntry", "fieldbackground"),
    ("TEntry", "foreground"),
    ("TSpinbox", "fieldbackground"),
    ("TCombobox", "fieldbackground"),
    ("TCombobox", "arrowcolor"),
    ("TCheckbutton", "indicatorforeground"),
    ("TRadiobutton", "indicatorforeground"),
    ("Treeview", "background"),
    ("Treeview", "foreground"),
    ("Treeview", "rowheight"),
    ("Treeview.Heading", "background"),
    ("Treeview.Heading", "foreground"),
    ("Horizontal.TProgressbar", "background"),
    ("Horizontal.TProgressbar", "troughcolor"),
    ("Horizontal.TProgressbar", "thickness"),
    ("Vertical.TScrollbar", "troughcolor"),
    ("Vertical.TScrollbar", "background"),
    ("Card.TLabelframe", "background"),
    ("Card.TLabelframe.Label", "foreground"),
    ("TSeparator", "background"),
)


def named_font(root: tk.Misc, size: int = 9, mono: bool = False,
               bold: bool = False) -> tkfont.Font:
    """按名字缓存一个 ``tkfont.Font``，供原生 tk 控件与 tag 使用。

    命名字体的好处：一处改全家生效（以后做字号缩放只改这里），
    而且 ``tk.Text`` / ``tk.Listbox`` / ``Treeview.tag_configure`` 都吃它。

    Args:
        root: 主窗口。
        size: 字号。
        mono: ``True`` 用等宽族（Consolas）。
        bold: ``True`` 加粗。

    Returns:
        可复用的 :class:`tkinter.font.Font`。
    """
    family = rt.FONT_MONO[0] if mono else rt.FONT[0]
    name = "RetroPreview{0}{1}{2}".format(size, "M" if mono else "", "B" if bold else "")
    try:
        return tkfont.nametofont(name, root=root)
    except tk.TclError:
        return tkfont.Font(root=root, name=name, family=family, size=size,
                           weight="bold" if bold else "normal")


def build_ui(root: tk.Tk, with_native: bool = False) -> dict[str, object]:
    """搭出主界面用到的全部控件，返回需要后续引用的控件表。"""
    widgets: dict[str, object] = {}

    # --- 横幅 --------------------------------------------------------------
    banner = rt.RetroBanner(root, width=WINDOW_W, height=92)
    banner.pack(side="top", fill="x")
    banner.start()
    widgets["banner"] = banner

    body = ttk.Frame(root, style="TFrame", padding=(10, 10, 10, 6))
    body.pack(side="top", fill="both", expand=True)
    body.columnconfigure(0, weight=3, uniform="col")
    body.columnconfigure(1, weight=2, uniform="col")
    body.rowconfigure(1, weight=1)

    # --- ① 源文件 ----------------------------------------------------------
    card1 = ttk.Labelframe(body, text="① 源文件", style="Card.TLabelframe")
    card1.grid(row=0, column=0, padx=(0, 8), pady=(0, 8), sticky="nsew")
    card1.columnconfigure(0, weight=1)

    ttk.Label(card1, text="SPC / MSU1 目录或压缩包", style="TLabel").grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
    entry = ttk.Entry(card1, width=30)
    entry.insert(0, r"D:\music\chrono_trigger_msu1")
    entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))
    ttk.Button(card1, text="浏览…", style="Small.TButton", width=6).grid(
        row=1, column=1, sticky="e")
    chk = ttk.Checkbutton(card1, text="导出后校验 WAV 时长", style="TCheckbutton")
    chk.state(["selected"])
    chk.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 2))

    fmt_row = ttk.Frame(card1, style="TFrame")
    fmt_row.grid(row=3, column=0, columnspan=2, sticky="w")
    fmt = tk.StringVar(value="cd")
    ttk.Radiobutton(fmt_row, text="音频 CD 镜像", value="cd",
                    variable=fmt, style="TRadiobutton").pack(side="left")
    ttk.Radiobutton(fmt_row, text="Pico-Plus WAV", value="wav",
                    variable=fmt, style="TRadiobutton").pack(side="left", padx=(10, 0))

    # --- ② 输出选项 --------------------------------------------------------
    card2 = ttk.Labelframe(body, text="② 输出选项", style="Card.TLabelframe")
    card2.grid(row=0, column=1, pady=(0, 8), sticky="nsew")
    card2.columnconfigure(1, weight=1)

    ttk.Label(card2, text="采样率", style="TLabel").grid(row=0, column=0, sticky="w", pady=2)
    combo = ttk.Combobox(card2, values=("44100 Hz", "48000 Hz", "32000 Hz"),
                         state="readonly", width=12)
    combo.current(0)
    combo.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=2)

    ttk.Label(card2, text="轨道数", style="TLabel").grid(row=1, column=0, sticky="w", pady=2)
    spin = ttk.Spinbox(card2, from_=1, to=99, width=12)
    spin.set(18)
    spin.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=2)

    ttk.Label(card2, text="目标音量 -6.0 dB", style="Mono.TLabel").grid(
        row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

    btn_row = ttk.Frame(card2, style="TFrame")
    btn_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
    ttk.Button(btn_row, text="预览").pack(side="left")
    ttk.Button(btn_row, text="开始转换", style="Accent.TButton").pack(side="left", padx=6)
    ttk.Button(btn_row, text="重置", style="Small.TButton").pack(side="left")
    ttk.Separator(card2, orient="horizontal").grid(
        row=4, column=0, columnspan=2, sticky="ew", pady=(8, 4))
    ttk.Label(card2, text="提示：转换过程可随时中断，已完成的轨道会保留。",
              style="Muted.TLabel").grid(row=5, column=0, columnspan=2, sticky="w")

    # --- ③ 曲目列表 --------------------------------------------------------
    card3 = ttk.Labelframe(body, text="③ 曲目列表", style="Card.TLabelframe")
    card3.grid(row=1, column=0, padx=(0, 8), sticky="nsew")
    card3.columnconfigure(0, weight=1)
    card3.rowconfigure(0, weight=1)

    tree_wrap = ttk.Frame(card3, style="Card.TFrame")
    tree_wrap.grid(row=0, column=0, sticky="nsew")
    tree_wrap.columnconfigure(0, weight=1)
    tree_wrap.rowconfigure(0, weight=1)

    tree = ttk.Treeview(tree_wrap, columns=("time", "size", "state"),
                        show="tree headings", height=8)
    tree.heading("#0", text="曲目")
    tree.heading("time", text="时长")
    tree.heading("size", text="大小")
    tree.heading("state", text="状态")
    tree.column("#0", width=190)
    tree.column("time", width=70, anchor="e")
    tree.column("size", width=80, anchor="e")
    tree.column("state", width=90, anchor="w")
    tree.grid(row=0, column=0, sticky="nsew")

    vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=tree.yview)
    vsb.grid(row=0, column=1, sticky="ns")
    hsb = ttk.Scrollbar(card3, orient="horizontal", command=tree.xview)
    hsb.grid(row=1, column=0, sticky="ew", pady=(4, 0))
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

    # 界面侧会自己 tag_configure，这里验证主题底色下不冲突
    tree.tag_configure("grp", background=rt.PALETTE["panel"], foreground=rt.PALETTE["amber"])
    tree.tag_configure("ok", foreground=rt.PALETTE["green"])
    tree.tag_configure("bad", foreground=rt.PALETTE["red"])
    tree.insert("", "end", text="Chrono Trigger MSU1 (18 轨)", open=True, tags=("grp",))
    rows = (("01 A Premonition", "0:42", "7.1 MB", "done"),
            ("02 Guardia Millennial Fair", "3:12", "32.3 MB", "done"),
            ("03 Secret of the Forest", "2:58", "29.9 MB", "running"),
            ("04 Frog's Theme", "1:47", "18.0 MB", "pending"),
            ("05 Battle with Magus", "2:31", "25.4 MB", "failed"))
    for name, length, size, state in rows:
        tags = {"done": ("ok",), "failed": ("bad",)}.get(state, ())
        tree.insert("", "end", text=name, values=(length, size, state), tags=tags)
    tree.selection_set(tree.get_children("")[1] if tree.get_children("") else "")
    widgets["tree"] = tree

    # --- ④ 进度 ------------------------------------------------------------
    card4 = ttk.Labelframe(body, text="④ 进度", style="Card.TLabelframe")
    card4.grid(row=1, column=1, sticky="nsew")
    card4.columnconfigure(0, weight=1)

    prog = ttk.Progressbar(card4, mode="determinate", maximum=100, value=64)
    prog.grid(row=0, column=0, sticky="ew", pady=(2, 6))
    ttk.Label(card4, text="64%  ·  轨道 12/18", style="Accent.TLabel").grid(
        row=1, column=0, sticky="w")
    ttk.Label(card4, text="正在写入 disc image (CDDA track 12)…", style="Muted.TLabel",
              wraplength=250, justify="left").grid(row=2, column=0, sticky="w", pady=(4, 0))
    indet = ttk.Progressbar(card4, mode="indeterminate")
    indet.grid(row=3, column=0, sticky="ew", pady=(10, 0))
    indet.start(60)
    ttk.Separator(card4, orient="horizontal").grid(row=4, column=0, sticky="ew", pady=(10, 6))
    ttk.Label(card4, text="输出: D:\\out\\chrono_trigger.cue", style="Mono.TLabel",
              wraplength=250, justify="left").grid(row=5, column=0, sticky="w")
    widgets["progress"] = prog

    # --- ⑤ 原生控件（可选，--native 才显示）--------------------------------
    # 真实界面里 ① 音源 是 tk.Listbox、日志是 tk.Text，它们的配色走的是
    # option database 而不是 ttk style，所以单独开一张卡做视觉校验。
    if with_native:
        card5 = ttk.Labelframe(body, text="⑤ 原生控件（option DB 着色）",
                               style="Card.TLabelframe")
        card5.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        card5.columnconfigure(0, weight=1, uniform="nat")
        card5.columnconfigure(1, weight=1, uniform="nat")

        listbox = tk.Listbox(card5, height=4, font=named_font(root),
                             activestyle="none", highlightthickness=1,
                             highlightbackground=rt.PALETTE["line"],
                             highlightcolor=rt.PALETTE["cyan"],
                             borderwidth=0, selectmode="extended")
        for name in ("Chrono Trigger MSU1", "Final Fantasy VI MSU1",
                     "Secret of Mana MSU1"):
            listbox.insert("end", name)
        listbox.selection_set(1)
        listbox.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        text = tk.Text(card5, height=4, wrap="word", borderwidth=0,
                       font=named_font(root, mono=True), padx=6, pady=4)
        text.insert("1.0", "日志: libgme 0.6.4 已加载\n"
                           "轨道 12/18 转换中…\n"
                           "输出 D:\\out\\chrono_trigger.cue\n")
        text.grid(row=0, column=1, sticky="nsew")
        widgets["listbox"] = listbox
        widgets["text"] = text

    # --- 状态行（贴在窗口底色上，用 Window.* 系列）--------------------------
    status = ttk.Frame(root, style="TFrame", padding=(10, 2, 10, 8))
    status.pack(side="bottom", fill="x")
    ttk.Label(status, text="● 就绪", style="Window.Accent.TLabel").pack(side="left")
    ttk.Label(status, text="18 轨 / 512 MB / libgme 0.6.4", style="Window.Muted.TLabel").pack(
        side="left", padx=(12, 0))
    ttk.Label(status, text="msu1-to-cd  ·  v2.1", style="Window.Dim.TLabel").pack(side="right")

    return widgets


def report_native_widgets(root: tk.Misc) -> int:
    """冒烟检查 option database 里的字体/颜色能否被 tk 原生控件解析。

    Tk 把 ``-font`` 当「字体描述列表」解析，族名带空格（``Microsoft YaHei UI``）
    必须在串里加花括号，否则 ``tk.Listbox`` / ``tk.Menu`` 一创建就抛
    ``expected integer but got "YaHei"``。这里每次预览都真建一次控件来兜住这类回归。

    Args:
        root: 已 install 过主题的窗口。

    Returns:
        失败个数（0 表示全部通过）。
    """
    failures = 0
    factories = (
        ("tk.Listbox", lambda: tk.Listbox(root, height=2)),
        ("tk.Text", lambda: tk.Text(root, height=2)),
        ("tk.Menu", lambda: tk.Menu(root)),
        ("ttk.Spinbox", lambda: ttk.Spinbox(root, from_=0, to=1)),
        ("ttk.Combobox", lambda: ttk.Combobox(root, values=("a",))),
    )
    print("-" * 66)
    print("原生控件冒烟检查 (option database 字体/颜色)")
    for label, factory in factories:
        try:
            widget = factory()
            widget.destroy()
            print("  %-14s OK" % label)
        except tk.TclError as exc:
            failures += 1
            print("  %-14s FAIL  %s" % (label, exc))
    print("-" * 66)
    return failures


def report_styles(style: ttk.Style) -> None:
    """回读各 style 实际生效的关键颜色，肉眼 / 日志都能核对。"""
    print("=" * 66)
    print("retro_theme 样式回读 (theme=%s)" % style.theme_use())
    print("=" * 66)
    for name, option in _LOOKUP_TARGETS:
        try:
            value = style.lookup(name, option)
        except tk.TclError as exc:  # pragma: no cover
            value = "<error: %s>" % exc
        print("%-26s %-16s %s" % (name, option, value if value != "" else "<empty>"))
    print("-" * 66)
    for mapped in ("TButton", "Accent.TButton", "TEntry", "Treeview", "Vertical.TScrollbar"):
        try:
            print("%-26s map(background) = %s" % (
                mapped, style.map(mapped, "background")))
        except tk.TclError as exc:  # pragma: no cover
            print("%-26s map error: %s" % (mapped, exc))
    print("=" * 66)


def main(argv: list[str]) -> int:
    hold = "--hold" in argv
    with_native = "--native" in argv

    root = tk.Tk()
    root.title("retro_theme 预览 — MSU1 CONVERTER")
    root.geometry("%dx%d" % (WINDOW_W, WINDOW_H if not with_native else WINDOW_H + 110))
    root.minsize(640, 560)

    style = rt.install(root)
    rt.app_icon(root)
    native_failures = report_native_widgets(root)
    if native_failures:
        print("警告: %d 个原生控件创建失败，主题 option database 有问题" % native_failures)
    build_ui(root, with_native=with_native)

    # 深色标题栏要在窗口 mapped 之后调用（这里模拟主程序的 after(50, ...)）
    def apply_dark_title_bar() -> None:
        print("dark_title_bar(root) -> %s" % rt.dark_title_bar(root))

    root.after(50, apply_dark_title_bar)

    def on_report() -> None:
        report_styles(style)
        sys.stdout.flush()

    root.after(1500, on_report)
    if not hold:
        root.after(2500, root.destroy)
        print("preview: 窗口 2.5s 后自动关闭（--hold 可常驻）")
    else:
        print("preview: --hold 模式，关闭窗口即退出")

    try:
        root.mainloop()
    except tk.TclError as exc:
        print("Tk 异常: %s" % exc)
        return 1
    print("preview: 正常退出")
    return 1 if native_failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
