#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""16-bit 复古游戏机主题（纯 tkinter，零第三方依赖，零图片资源）.

本模块为 RetroMusic Lab（原 MSU1 转换器）提供统一的**浅色**视觉皮肤：
主机机身色的纸底 + 墨黑字 + 单一强调色，像素风横幅与像素图标都是现场绘制。

> v4.16 起由「暗色 synthwave 霓虹」改为「浅色 8-bit 印刷」。
> v4.18 起内置 6 套**主机风格**主题（红白机 / 美版 NES / Game Boy /
> PC Engine / Mega Drive / 超任），主界面可实时切换并记住选择。
> 旧暗色值（需要回退时直接换回：
> bg #0e0c1d / panel #181430 / card #221c40 / card2 #2a2350 /
> line #3b3266 / text #ece9ff / muted #a49dc9 / dim #6f689a /
> cyan #35e0f0 / magenta #ff5fa2 / amber #ffcc4d / green #7ef0a0 /
> red #ff6b6b / sel #2b4a6f；_ON_ACCENT #06121a）。

对外接口（冻结，勿改签名）::

    PALETTE / FONT / FONT_S / FONT_B / FONT_TITLE / FONT_MONO
    install(root) -> ttk.Style
    RetroBanner(master, width=820, height=92, **kw)   # 动画横幅
    app_icon(root) -> tk.PhotoImage                    # 32x32 像素图标

    THEMES / theme_keys() / theme_label(key) / current_theme()
    set_current(key) -> bool        # 只换常量，不碰任何界面（启动时用）
    apply_theme(key) -> bool        # 换常量 + 重刷已 install 的窗口 + 皮肤钩子
    on_theme_skinned(widget, fn)    # 注册「tk 原生控件重上色」回调

用法::

    import tkinter as tk
    from tkinter import ttk
    import retro_theme as rt

    root = tk.Tk()
    rt.set_current("nes")            # 可选：启动时先定主题（默认 famicom）
    style = rt.install(root)
    rt.app_icon(root)
    banner = rt.RetroBanner(root)
    banner.pack(fill="x")
    banner.start()
    ...
    rt.apply_theme("gameboy")        # 运行中切换（样式 + tk 原生控件一起刷）
    ...
    banner.stop()   # 关窗前调用（或依赖内部 <Destroy> 自动清理）

命令行自检::

    python retro_theme.py          # 当前主题的调色板对比度自检表
    python retro_theme.py --all    # 全部主题逐套自检（改配色后必跑）

设计原则：
  * 只使用 `clam` 主题做底（Windows 原生主题不允许改色）。
  * 所有像素画（星空 / 卡带图标 / 图标位图）都用 Canvas 图元或
    ``PhotoImage.put`` 现场绘制，不依赖任何外部图片文件。
  * 动画只重绘「会动的图元」（频谱条 + 峰值亮边），静态图层只画一次。
  * **所有主题都是浅色底**（用户明确不要深色）。因此 ``IS_LIGHT`` 恒为
    True，``_tint`` / ``_peak_color`` 的方向口径对每套主题都一致。
"""

from __future__ import annotations

import math
import random
import sys
import tkinter as tk
import weakref
from tkinter import ttk
from tkinter import font as tkfont

__all__ = [
    "PALETTE",
    "FONT",
    "FONT_S",
    "FONT_B",
    "FONT_TITLE",
    "FONT_MONO",
    "BANNER_TITLE",
    "THEMES",
    "THEME_ORDER",
    "DEFAULT_THEME",
    "theme_keys",
    "theme_label",
    "current_theme",
    "set_current",
    "apply_theme",
    "on_theme_skinned",
    "banner_title_font",
    "mix_color",
    "install",
    "RetroBanner",
    "app_icon",
    "dark_title_bar",
    "report_contrast",
]


# ---------------------------------------------------------------------------
# 颜色工具（纯函数；放在调色板之前，因为主题表要用 _mix/_shade 推导
# hover / pressed 色，模块加载时就要算出来）
# ---------------------------------------------------------------------------

def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    """把 ``#rrggbb`` 解析成 (r, g, b)。"""
    value = color.lstrip("#")
    if len(value) == 3:  # 兼容 #rgb 简写
        value = "".join(ch * 2 for ch in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    """把浮点 RGB 钳制到 0..255 后格式化成 ``#rrggbb``。"""
    r, g, b = (max(0, min(255, int(round(channel)))) for channel in rgb)
    return "#%02x%02x%02x" % (r, g, b)


def _mix(color_a: str, color_b: str, t: float) -> str:
    """线性插值两个颜色，``t=0`` 返回 a，``t=1`` 返回 b。"""
    t = max(0.0, min(1.0, float(t)))
    a = _hex_to_rgb(color_a)
    b = _hex_to_rgb(color_b)
    return _rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def _shade(color: str, factor: float) -> str:
    """按比例压暗 / 提亮（factor < 1 压暗）。"""
    return _rgb_to_hex(tuple(channel * factor for channel in _hex_to_rgb(color)))


def mix_color(color_a: str, color_b: str, t: float) -> str:
    """公开的线性混色：``t=0`` 得 a、``t=1`` 得 b。

    GUI 侧算「日志纸底」这类中间色时用这个，别去调下划线私有函数
    （私有函数随时可能改名/挪位置）。
    """
    return _mix(color_a, color_b, t)


# ---------------------------------------------------------------------------
# 主题注册表（v4.18：6 套主机风格，主界面可切换）
# ---------------------------------------------------------------------------
#
# ⚠ 键名是**语义位**，不是颜色名，别按名字猜：
#     cyan    = 主强调（选中 / 进度 / 主按钮 / 横幅标题）
#     magenta = 次强调（装饰、频谱条另一端、地平线）
#     amber   = 数值 / 等宽字体提示
#   历史原因沿用 v3 的键名，改名会波及 GUI 里 26 处引用，故冻结。
#
# 每套配色都过了 WCAG 2.1 自检（`python retro_theme.py --all`）：
# 正文 4.5:1、辅助 3.0:1。改任何一格颜色都必须重跑那条命令。
# v5.0-qt: 配色数据搬到了 retro_palette.py（纯数据，不 import tkinter）。
# 这里改成引用，对外接口 THEMES / THEME_ORDER / DEFAULT_THEME 保持不变。
from retro_palette import THEMES, THEME_ORDER, DEFAULT_THEME  # noqa: E402


#: 当前主题 key（由 set_current / apply_theme 维护）
_current_theme = DEFAULT_THEME

#: 当前生效的调色板。**必须原地 mutate**：GUI 侧大量 `PALETTE["x"]` 在
#: 运行时取值，重新赋值新 dict 会让已经 import 的引用指向旧字典。
PALETTE: dict[str, str] = dict(THEMES[DEFAULT_THEME]["palette"])

FONT = ("Microsoft YaHei UI", 9)
FONT_S = ("Microsoft YaHei UI", 8)
FONT_B = ("Microsoft YaHei UI", 9, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 13, "bold")
FONT_MONO = ("Consolas", 9)

#: 横幅主标题字体候选（从最「极客」往下退）。Cascadia Code 是 Windows
#: Terminal / VS Code 的默认编程字体，等宽 + 明显字重，比 Consolas 更有
#: 「终端 / 代码」气质；都没有时退 Consolas。
BANNER_FONT_STACK: tuple[str, ...] = (
    "Cascadia Code", "Cascadia Mono", "Consolas",
    "DejaVu Sans Mono", "Courier New",
)

#: Accent 按钮上的字色（深色强调底上用浅字，对比度 ≥4.5:1）
_ON_ACCENT = THEMES[DEFAULT_THEME]["on_accent"]
#: **选中底**（sel）上的字色。必须用墨色而不是强调色：浅色主题下选中行
#: 若整行转成强调色，会和日志里的「失败」撞语义（v4.16 第一版就踩了）。
_ON_SEL = PALETTE["text"]
#: Accent 按钮 hover / pressed 底（由强调色推导，切主题时一起重算）
_ACCENT_HOVER = _mix(PALETTE["cyan"], "#ffffff", 0.10)
_ACCENT_PRESS = _shade(PALETTE["cyan"], 0.82)

#: 主题明暗极性：True = 浅色底。少数「往白里混」的颜色表达式要据此翻转
#: （例如 banner 的峰值亮边——深色主题混白=变亮，浅色主题必须混深才看得见）。
#: 目前 6 套主题全是浅色，保留这个开关是为了将来加暗色主题时不用翻代码。
IS_LIGHT = True

#: RetroBanner 动画帧间隔（毫秒）
BANNER_INTERVAL_MS = 80

#: 横幅主标题文案（v4.18 由 MSU1 CONVERTER 改名；程序主体不再是"只转 MSU1"）
BANNER_TITLE = "RetroMusic Lab"


# ---------------------------------------------------------------------------
# 颜色工具（依赖「当前」调色板的那几个：切主题后自动按新色计算）
# ---------------------------------------------------------------------------

def _tint(color: str, amount: float) -> str:
    """把颜色往**窗口底色**方向稀释，``amount=0`` 保持原色，``1`` 等于底色。

    浅色主题里画「网格线 / 分隔线」要的是**淡**，而不是 :func:`_shade` 那种
    压暗 —— 深色底压暗=更暗，浅色底压暗=更醒目，正好反了。所以凡是
    「线要比背景弱」的地方一律用 _tint，别用 _shade（v4.16 换肤踩过）。
    """
    return _mix(color, PALETTE["bg"], amount)


def _peak_color(color: str) -> str:
    """频谱「峰值亮边」的颜色：深色底混白变亮，浅色底要压暗才看得见。"""
    if IS_LIGHT:
        return _shade(color, 0.55)
    return _mix(color, "#ffffff", 0.55)


def _luminance(color: str) -> float:
    """WCAG 2.1 相对亮度。"""
    channels = []
    for value in _hex_to_rgb(color):
        srgb = value / 255.0
        channels.append(srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(fg: str, bg: str) -> float:
    """计算前景 / 背景的 WCAG 对比度（1.0 ~ 21.0）。"""
    light, dark = _luminance(fg), _luminance(bg)
    if light < dark:
        light, dark = dark, light
    return (light + 0.05) / (dark + 0.05)


def _contrast_checks() -> tuple[tuple[str, str, str, float], ...]:
    """按**当前**主题现算自检清单：(说明, 前景, 背景, 最低要求)。

    正文 4.5:1、辅助说明 3.0:1（WCAG 2.1 AA）。做成函数而不是模块级常量
    的原因：切主题后 ``PALETTE`` 里的值变了，常量会永远停在导入时那一套。
    """
    p = PALETTE
    return (
        ("主文本 text / bg",        p["text"], p["bg"], 4.5),
        ("主文本 text / panel",     p["text"], p["panel"], 4.5),
        ("主文本 text / card",      p["text"], p["card"], 4.5),
        ("主文本 text / card2",     p["text"], p["card2"], 4.5),
        ("次要文本 muted / bg",     p["muted"], p["bg"], 4.5),
        ("次要文本 muted / panel",  p["muted"], p["panel"], 4.5),
        ("次要文本 muted / card",   p["muted"], p["card"], 4.5),
        ("弱化文本 dim / bg",       p["dim"], p["bg"], 3.0),
        ("弱化文本 dim / panel",    p["dim"], p["panel"], 3.0),
        ("弱化文本 dim / card",     p["dim"], p["card"], 3.0),
        ("强调 cyan / bg",          p["cyan"], p["bg"], 4.5),
        ("强调 cyan / panel",       p["cyan"], p["panel"], 4.5),
        ("强调 cyan / card",        p["cyan"], p["card"], 4.5),
        ("强调 magenta / panel",    p["magenta"], p["panel"], 4.5),
        ("强调 magenta / card",     p["magenta"], p["card"], 4.5),
        ("数值 amber / panel",      p["amber"], p["panel"], 4.5),
        ("数值 amber / card",       p["amber"], p["card"], 4.5),
        ("成功 green / panel",      p["green"], p["panel"], 4.5),
        ("失败 red / panel",        p["red"], p["panel"], 4.5),
        ("失败 red / card",         p["red"], p["card"], 4.5),
        ("选中底 text / sel",       p["text"], p["sel"], 4.5),
        ("选中底 _ON_SEL / sel",    _ON_SEL, p["sel"], 4.5),
        ("主按钮文字 / cyan",       _ON_ACCENT, p["cyan"], 4.5),
        ("主按钮文字 / cyan hover", _ON_ACCENT, _ACCENT_HOVER, 4.5),
        ("主按钮文字 / cyan press", _ON_ACCENT, _ACCENT_PRESS, 4.5),
    )


def report_contrast(stream=None, theme: str | None = None) -> int:
    """打印对比度自检表，返回不达标条目数。

    Args:
        stream: 输出流，默认 ``sys.stdout``。
        theme: 指定主题 key；``None`` 表示检查当前主题。

    Returns:
        不达标的组合数量；0 表示全部通过。
    """
    out = stream if stream is not None else sys.stdout
    saved = _current_theme
    if theme is not None:
        if theme not in THEMES:
            out.write("未知主题: %s\n" % theme)
            return 1
        set_current(theme)
    failures = 0
    try:
        label = THEMES[_current_theme]["label"]
        out.write("=" * 62 + "\n")
        out.write("retro_theme 对比度自检 — %s (%s)\n" % (label, _current_theme))
        out.write("WCAG 2.1: 正文 4.5 / 辅助 3.0\n")
        out.write("=" * 62 + "\n")
        for name, fg, bg, threshold in _contrast_checks():
            ratio = _contrast(fg, bg)
            ok = ratio >= threshold
            if not ok:
                failures += 1
            out.write("%-26s %s on %s  %6.2f:1  %s\n" % (
                name, fg, bg, ratio, "OK" if ok else "WARN(<%.1f)" % threshold))
        out.write("-" * 62 + "\n")
        if failures:
            out.write("结果: %d 项未达标\n" % failures)
        else:
            out.write("结果: 全部达标 (0 项警告)\n")
        out.write("=" * 62 + "\n")
    finally:
        if theme is not None:
            set_current(saved)
    return failures


# ---------------------------------------------------------------------------
# 主题切换（v4.18）
# ---------------------------------------------------------------------------
#
# 分工：
#   * set_current(key)  —— 只改模块级常量（PALETTE / _ON_ACCENT / ...），
#     不碰任何界面。启动时在 install() 之前调它。
#   * apply_theme(key)  —— set_current + 对所有 install 过的窗口重跑 install
#     （ttk 样式全部重配，ttk 控件自动变色）+ 执行「tk 原生控件」的重上色钩子。
#
# ⚠ 为什么要钩子：ttk 主题改的是样式表，控件会自己重绘；而 tk.Text /
#   tk.Listbox 的颜色是**建控件时写死**在实例上的，样式表管不到。GUI 侧
#   用 on_theme_skinned() 把这些控件的上色函数登记进来即可。

#: 已 install 过的窗口（弱引用：窗口销毁后不留死对象）
_ROOTS: list = []
#: 原生控件重上色钩子：[(弱引用到归属控件, 回调)]
_SKIN_HOOKS: list = []
#: 字体族名缓存（探测 tkfont.families() 有开销，且每次切主题都要用）
_FAMILY_CACHE: dict[str, set] = {}
_TITLE_FONT_CACHE: dict[tuple, object] = {}


def _font_families(widget: tk.Misc) -> set:
    """本机可用字体族名集合（一次探测，进程内缓存；失败返回空集合）。"""
    cached = _FAMILY_CACHE.get("families")
    if cached is not None:
        return cached
    try:
        families = set(tkfont.families(widget))
    except (tk.TclError, RuntimeError):      # pragma: no cover - 无显示环境
        families = set()
    if families:
        _FAMILY_CACHE["families"] = families
    return families


def banner_title_font(widget: tk.Misc, size: int):
    """取横幅标题用的字体对象（等宽 + bold，按 :data:`BANNER_FONT_STACK` 退让）。

    Args:
        widget: 任意活着的控件（用来借它的 Tcl 解释器）。
        size: 字号（pt）。

    Returns:
        :class:`tkinter.font.Font`，带 ``measure()`` —— 逐字排字距时要靠它算宽度。
        连 Courier 都取不到时返回 ``None``（调用方退成单行 create_text）。
    """
    families = _font_families(widget)
    family = BANNER_FONT_STACK[-1]
    for candidate in BANNER_FONT_STACK:
        if candidate in families:
            family = candidate
            break
    key = (family, int(size), "bold")
    # ⚠ 缓存必须带上「这个 Font 绑在哪个 Tcl 解释器上」。tkinter 的 Font 是
    # 按解释器创建的，root 一销毁它就成了死对象，再 measure() 会抛
    # ``TclError: cannot invoke "font" command: application has been destroyed``。
    # 进程内只有一个 root 时永远撞不到，但「关掉再开一个 Tk()」就会撞到
    # （vendor/test_drop.py 就是这么测的）。所以这里比对解释器身份，换了就重建。
    cached = _TITLE_FONT_CACHE.get(key)
    if cached is not None and cached[0] is widget.tk:
        return cached[1]
    try:
        font = tkfont.Font(root=widget, family=family, size=int(size),
                           weight="bold")
    except (tk.TclError, RuntimeError):      # pragma: no cover
        try:
            font = tkfont.Font(root=widget, family="Courier", size=int(size),
                               weight="bold")
        except (tk.TclError, RuntimeError):
            return None
    _TITLE_FONT_CACHE[key] = (widget.tk, font)
    return font


# theme_keys / theme_label 一并从 retro_palette 转发（同一份事实源）
from retro_palette import theme_keys, theme_label  # noqa: E402,F401


def current_theme() -> str:
    """返回当前生效的主题 key。"""
    return _current_theme


def set_current(key: str) -> bool:
    """切换模块级常量到指定主题（**不动界面**）。

    Args:
        key: 主题 key（见 :func:`theme_keys`）；未知 key 时退回默认主题。

    Returns:
        ``True`` 表示 key 有效并已生效。
    """
    global _current_theme, _ON_ACCENT, _ON_SEL, _ACCENT_HOVER, _ACCENT_PRESS
    global IS_LIGHT
    ok = key in THEMES
    if not ok:
        key = DEFAULT_THEME
    entry = THEMES[key]
    # ⚠ 必须原地 mutate：GUI 里已经 import 了 PALETTE 这个 dict 对象，
    #   重新赋值会让那些引用继续指向旧字典（改一个不生效，排查半天）。
    PALETTE.clear()
    PALETTE.update(entry["palette"])
    _ON_ACCENT = entry["on_accent"]
    _ON_SEL = PALETTE["text"]
    _ACCENT_HOVER = _mix(PALETTE["cyan"], "#ffffff", 0.10)
    _ACCENT_PRESS = _shade(PALETTE["cyan"], 0.82)
    # 6 套主题全是浅色；保留开关是为了将来加暗色主题
    IS_LIGHT = _luminance(PALETTE["bg"]) > 0.45
    _current_theme = key
    return ok


def on_theme_skinned(widget: tk.Misc, fn) -> None:
    """登记「tk 原生控件重上色」回调，切主题时自动重跑。

    Args:
        widget: 回调所属的控件（通常是窗口）。它被销毁后回调自动失效，
            不会在已关闭的下载窗上再刷一次颜色。
        fn: 无参可调用对象，内部自己 ``widget.configure(bg=...)`` 即可。
            ⚠ 若传的是**绑定方法**，这里用 :class:`weakref.WeakMethod` 存，
            否则列表会一直攥着窗口实例，关掉的下载窗永远回收不掉。
    """
    try:
        ref = weakref.ref(widget)
    except TypeError:          # pragma: no cover - 极少数不可弱引用的对象
        return
    try:
        fn_ref = weakref.WeakMethod(fn)
    except TypeError:          # 普通函数 / lambda 不可弱引用
        fn_ref = lambda: fn     # noqa: E731
    _SKIN_HOOKS.append((ref, fn_ref))


def _remember_root(root: tk.Misc) -> None:
    """把窗口记进「切主题时要重刷样式」的名单（去重 + 弱引用）。"""
    for ref in list(_ROOTS):
        obj = ref()
        if obj is None:
            _ROOTS.remove(ref)
        elif obj is root:
            return
    try:
        _ROOTS.append(weakref.ref(root))
    except TypeError:          # pragma: no cover
        pass


def apply_theme(key: str) -> bool:
    """运行中切换主题：改常量 → 重刷所有已 install 的窗口 → 跑原生控件钩子。

    Args:
        key: 主题 key。

    Returns:
        ``True`` 表示 key 有效。窗口重刷过程中的 TclError 一律吞掉
        （窗口正在销毁时属正常现象，主题切换绝不能把主程序带崩）。
    """
    ok = set_current(key)

    for ref in list(_ROOTS):
        root = ref()
        if root is None:
            _ROOTS.remove(ref)
            continue
        try:
            if not root.winfo_exists():
                continue
            install(root)
        except (tk.TclError, AttributeError, RuntimeError):
            continue

    for ref, fn_ref in list(_SKIN_HOOKS):
        widget = ref()
        fn = fn_ref() if callable(fn_ref) else None
        if widget is None or fn is None:
            # 窗口已销毁 / 绑定方法的目标已回收：把钩子摘掉，别再试
            _SKIN_HOOKS.remove((ref, fn_ref))
            continue
        try:
            if not widget.winfo_exists():
                _SKIN_HOOKS.remove((ref, fn_ref))
                continue
            fn()
        except (tk.TclError, AttributeError, RuntimeError):
            continue
    return ok


# ---------------------------------------------------------------------------
# ttk 主题安装
# ---------------------------------------------------------------------------

def install(root: tk.Misc) -> ttk.Style:
    """把 root 的 ttk 主题切换成当前主题（默认红白机）。

    会做四件事：
      1. ``root.configure(bg=PALETTE["bg"])`` 把原生窗口底色也刷成主题色；
      2. ``ttk.Style(root).theme_use("clam")`` —— 只有 clam 允许自由改色；
      3. 逐项配置 GUI 会直接引用的全部 style 名，并写入 tk 原生控件的
         option database（Text / Listbox / Menu / Combobox 下拉列表）；
      4. 把 root 登记进「切主题时要重刷」的名单（见 :func:`apply_theme`）。

    本函数可对同一个窗口重复调用 —— 这正是运行中换主题的实现方式：
    样式重新 configure 一遍，所有 ttk 控件立刻按新色重绘。

    Args:
        root: 主窗口（Tk 或 Toplevel），install 也可对 Toplevel 单独调用。

    Returns:
        配置完成的 :class:`ttk.Style` 实例。
    """
    root.configure(bg=PALETTE["bg"])
    _remember_root(root)

    style = ttk.Style(root)
    _use_clam(style)

    bg = PALETTE["bg"]
    panel = PALETTE["panel"]
    card = PALETTE["card"]
    card2 = PALETTE["card2"]
    line = PALETTE["line"]
    text = PALETTE["text"]
    muted = PALETTE["muted"]
    dim = PALETTE["dim"]
    cyan = PALETTE["cyan"]
    magenta = PALETTE["magenta"]
    amber = PALETTE["amber"]
    sel = PALETTE["sel"]

    # --- 全局默认 -----------------------------------------------------------
    style.configure(
        ".",
        background=bg,
        foreground=text,
        font=FONT,
        bordercolor=line,
        darkcolor=card,
        lightcolor=card,
        troughcolor=card,
        focuscolor=cyan,
        selectbackground=sel,
        selectforeground=_ON_SEL,
        fieldbackground=card,
    )

    # --- Frame --------------------------------------------------------------
    # TFrame      -> 窗口底色，安全用作普通容器
    # Panel.TFrame-> 分区底板 (panel)，用于包一层做「外框」
    # Card.TFrame -> 卡片底 (card)，比 panel 亮一档，用于分区内的次级块
    style.configure("TFrame", background=bg, borderwidth=0, relief="flat")
    style.configure("Panel.TFrame", background=panel, borderwidth=0, relief="flat")
    style.configure("Card.TFrame", background=card, borderwidth=0, relief="flat")

    # --- Label --------------------------------------------------------------
    # 默认 Label 系列 = panel 底（绝大多数标签活在 Card.TLabelframe 里）。
    # 直接贴在窗口底 (bg) 上的标签请用 Window.* 系列。
    for suffix in ("TLabel", "Title.TLabel", "Sub.TLabel", "Muted.TLabel",
                   "Accent.TLabel", "Mono.TLabel"):
        style.configure(suffix, background=panel)
    style.configure("TLabel", foreground=text)
    style.configure("Title.TLabel", foreground=text, font=FONT_TITLE)
    style.configure("Sub.TLabel", foreground=muted, font=FONT)
    style.configure("Muted.TLabel", foreground=muted, font=FONT_S)
    style.configure("Accent.TLabel", foreground=cyan, font=FONT_B)
    style.configure("Mono.TLabel", foreground=amber, font=FONT_MONO)

    # 贴在窗口底色上的同族标签（背景 = bg）
    style.configure("Window.TLabel", background=bg, foreground=text)
    style.configure("Window.Muted.TLabel", background=bg, foreground=muted, font=FONT_S)
    style.configure("Window.Dim.TLabel", background=bg, foreground=dim, font=FONT_S)
    style.configure("Window.Accent.TLabel", background=bg, foreground=cyan, font=FONT_B)
    style.configure("Window.Mono.TLabel", background=bg, foreground=amber, font=FONT_MONO)

    # --- Button -------------------------------------------------------------
    # 浅色主题下 card(#fffdf7) 与 panel(#faf5e9) 几乎同色，按钮会"消失"。
    # 所以按钮统一用 card2（暖沙）做底，hover 再压深一档，pressed 落到砖红。
    btn_bg = card2
    btn_hover = _mix(card2, line, 0.55)
    style.configure(
        "TButton",
        background=btn_bg,
        foreground=text,
        bordercolor=line,
        lightcolor=btn_bg,
        darkcolor=btn_bg,
        focuscolor=btn_hover,
        # ⚠ clam 在 relief="flat" 下**完全不画边框**（实测 1px/2px 都一样），
        #   浅色主题里按钮就只剩一块比底板略深的沙色，像个标签。
        #   要「像素按钮」的硬边必须用 solid。
        relief="solid",
        borderwidth=1,
        padding=(10, 5),
        font=FONT,
        anchor="center",
    )
    style.map(
        "TButton",
        background=[("pressed", cyan), ("active", btn_hover), ("disabled", panel)],
        foreground=[("pressed", _ON_ACCENT), ("disabled", dim)],
        lightcolor=[("pressed", cyan), ("active", btn_hover)],
        darkcolor=[("pressed", cyan), ("active", btn_hover)],
        bordercolor=[("focus", cyan), ("active", _mix(line, cyan, 0.35))],
        focuscolor=[("active", btn_hover)],
    )

    style.configure(
        "Accent.TButton",
        background=cyan,
        foreground=_ON_ACCENT,
        bordercolor=cyan,
        lightcolor=_ACCENT_HOVER,
        darkcolor=_ACCENT_PRESS,
        focuscolor=_ACCENT_PRESS,
        font=FONT_B,
    )
    style.map(
        "Accent.TButton",
        background=[("pressed", _ACCENT_PRESS), ("active", _ACCENT_HOVER), ("disabled", card)],
        foreground=[("pressed", _ON_ACCENT), ("disabled", dim)],
        lightcolor=[("pressed", _ACCENT_PRESS), ("active", _ACCENT_HOVER)],
        darkcolor=[("pressed", _ACCENT_PRESS), ("active", _ACCENT_HOVER)],
        bordercolor=[("disabled", line)],
        focuscolor=[("active", _ACCENT_PRESS)],
    )

    # Hero.TButton —— 全窗**唯一**的主操作（开始转换 / 开始下载）。
    # 与 Accent 同色，只靠"更大一圈 + 更宽的横向留白"拉开差距：
    # 字号不新增（仍是 9pt bold），否则会破坏 5 档字号规范。
    # ⚠ 一屏只允许一个 Hero；出现两个就等于没有主操作。
    style.configure(
        "Hero.TButton",
        background=cyan,
        foreground=_ON_ACCENT,
        bordercolor=_ACCENT_PRESS,
        lightcolor=_ACCENT_HOVER,
        darkcolor=_ACCENT_PRESS,
        focuscolor=_ACCENT_PRESS,
        font=FONT_B,
        padding=(16, 6),
        relief="solid",
        borderwidth=1,
    )
    style.map(
        "Hero.TButton",
        background=[("pressed", _ACCENT_PRESS), ("active", _ACCENT_HOVER),
                    ("disabled", card)],
        foreground=[("pressed", _ON_ACCENT), ("disabled", dim)],
        lightcolor=[("pressed", _ACCENT_PRESS), ("active", _ACCENT_HOVER)],
        darkcolor=[("pressed", _ACCENT_PRESS), ("active", _ACCENT_HOVER)],
        bordercolor=[("disabled", line)],
        focuscolor=[("active", _ACCENT_PRESS)],
    )

    style.configure(
        "Small.TButton",
        background=btn_bg,
        foreground=text,
        bordercolor=line,
        lightcolor=btn_bg,
        darkcolor=btn_bg,
        relief="solid",
        borderwidth=1,
        padding=(6, 2),
        font=FONT_S,
    )
    style.map(
        "Small.TButton",
        background=[("pressed", cyan), ("active", btn_hover), ("disabled", panel)],
        foreground=[("pressed", _ON_ACCENT), ("active", text), ("disabled", dim)],
        lightcolor=[("pressed", cyan), ("active", btn_hover)],
        darkcolor=[("pressed", cyan), ("active", btn_hover)],
        bordercolor=[("focus", cyan), ("active", _mix(line, cyan, 0.35))],
    )

    # --- Entry / Spinbox / Combobox ----------------------------------------
    # 注意: ttk.Entry / ttk.Spinbox 在 Tk 8.6/9.0 上没有 -insertcolor 选项，
    # 插入光标跟随文本色。这里仍然写入 insertcolor，供支持该选项的
    # Tk 构建（Tk 9.1+）直接生效；需要青色光标时请用 tk.Entry(insertbackground=cyan)。
    for entry_style in ("TEntry", "TSpinbox"):
        style.configure(
            entry_style,
            fieldbackground=card,
            background=card,
            foreground=text,
            bordercolor=line,
            lightcolor=card,
            darkcolor=card,
            insertcolor=cyan,
            selectbackground=sel,
            selectforeground=_ON_SEL,
            arrowcolor=muted,
            insertwidth=2,
            relief="flat",
            borderwidth=1,
            padding=(5, 3),
        )
        style.map(
            entry_style,
            bordercolor=[("focus", cyan), ("hover", _shade(cyan, 0.5))],
            lightcolor=[("focus", cyan)],
            darkcolor=[("focus", cyan)],
            # background 在 clam 里被 Entry.field 的边框区域复用；
            # 必须显式覆盖，否则会残留 clam 默认的 readonly '#dcdad5'
            background=[("readonly", card2), ("disabled", panel)],
            arrowcolor=[("active", cyan), ("disabled", dim)],
            fieldbackground=[("readonly", card2), ("disabled", panel)],
            foreground=[("disabled", dim), ("readonly", text)],
            selectbackground=[("!disabled", sel)],
            selectforeground=[("!disabled", _ON_SEL)],
        )

    style.configure(
        "TCombobox",
        fieldbackground=card,
        background=card,
        foreground=text,
        bordercolor=line,
        lightcolor=card,
        darkcolor=card,
        arrowcolor=muted,
        arrowsize=13,
        selectbackground=sel,
        selectforeground=_ON_SEL,
        padding=(5, 3),
        relief="flat",
        borderwidth=1,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", card), ("disabled", panel)],
        background=[("active", card2), ("pressed", card2)],
        foreground=[("disabled", dim)],
        arrowcolor=[("active", cyan), ("disabled", dim)],
        bordercolor=[("focus", cyan), ("hover", _shade(cyan, 0.5))],
        lightcolor=[("focus", cyan)],
        darkcolor=[("focus", cyan)],
        selectbackground=[("readonly", sel)],
        selectforeground=[("readonly", _ON_SEL)],
    )

    # 下拉弹出列表是原生 Listbox，只能通过 option database 着色
    _style_option_db(root)

    # --- Checkbutton / Radiobutton -----------------------------------------
    # 未选中描边用「line→muted」的中间色：既有像素感的暗边，又不至于糊成一团。
    _ring = _mix(line, muted, 0.30)
    for button_style in ("TCheckbutton", "TRadiobutton"):
        style.configure(
            button_style,
            background=panel,
            foreground=text,
            focuscolor=cyan,
            indicatorbackground=card,
            indicatorforeground=cyan,
            upperbordercolor=_ring,
            lowerbordercolor=_ring,
            padding=(2, 1),
            font=FONT,
        )
        style.map(
            button_style,
            background=[("active", panel), ("selected", panel)],
            foreground=[("disabled", dim)],
            upperbordercolor=[("focus", cyan), ("selected", cyan)],
            lowerbordercolor=[("focus", cyan), ("selected", cyan)],
        )
    # 勾选框：选中态整块 cyan + 深色勾（游戏机开关的观感，对比度 11.8:1）
    style.map(
        "TCheckbutton",
        indicatorbackground=[("disabled", panel), ("pressed", cyan),
                             ("selected", cyan), ("active", card2)],
        indicatorforeground=[("disabled", dim), ("selected", _ON_ACCENT)],
    )
    # 单选钮：选中态保持深色圆底 + cyan 圆点，避免变成「实心大圆」
    style.map(
        "TRadiobutton",
        indicatorbackground=[("disabled", panel), ("pressed", cyan),
                             ("selected", card), ("active", card2)],
        indicatorforeground=[("disabled", dim), ("selected", cyan)],
    )
    # 贴在窗口底上的同族控件
    for button_style in ("Window.TCheckbutton", "Window.TRadiobutton"):
        style.configure(button_style, background=bg)
        style.map(button_style, background=[("active", bg), ("selected", bg)])

    # clam 自带的勾选符号在 12px 下画成了「✕」形，容易被读成「关闭」；
    # 这里换成手工像素画（青色方块 + 深色勾），顺便让勾选框更像卡带开关。
    _install_pixel_checkbutton(style, root)

    # --- Treeview -----------------------------------------------------------
    style.configure(
        "Treeview",
        background=card,
        fieldbackground=card,
        foreground=text,
        bordercolor=line,
        lightcolor=card,
        darkcolor=card,
        borderwidth=1,
        relief="flat",
        rowheight=22,
        font=FONT,
        indent=14,
    )
    style.map(
        "Treeview",
        background=[("selected", sel)],
        # 选中行用墨字（不是强调红）：浅色底上整行转红会被读成「失败」
        foreground=[("selected", _ON_SEL)],
    )
    style.configure(
        "Treeview.Heading",
        background=panel,
        foreground=muted,
        bordercolor=line,
        lightcolor=panel,
        darkcolor=panel,
        relief="flat",
        borderwidth=1,
        padding=(6, 4),
        font=FONT_B,
    )
    style.map(
        "Treeview.Heading",
        background=[("active", card2), ("pressed", card)],
        foreground=[("active", cyan)],
        lightcolor=[("active", card2)],
        darkcolor=[("active", card2)],
    )

    # --- Progressbar --------------------------------------------------------
    # clam 的进度条填充色就是 pbar 元素的 background；槽是 troughcolor。
    for pb_style in ("TProgressbar", "Horizontal.TProgressbar", "Vertical.TProgressbar"):
        style.configure(
            pb_style,
            background=cyan,
            troughcolor=card,
            bordercolor=line,
            lightcolor=cyan,
            darkcolor=_shade(cyan, 0.75),
            arrowcolor=cyan,
            thickness=8,
            borderwidth=1,
            relief="flat",
        )

    # --- Scale（试听进度条）------------------------------------------------
    # clam 默认的 Scale 是系统灰，在米白底上几乎看不出「已播到哪」。
    # 滑块走砖红、槽走 card2，和进度条同一套语义。
    for sc_style in ("TScale", "Horizontal.TScale"):
        style.configure(
            sc_style,
            background=cyan,
            troughcolor=card2,
            bordercolor=line,
            # ⚠ lightcolor/darkcolor 决定**槽的外框**：留成强调色会让整根槽
            #   套一圈红边（实测像个红盒子）。这里必须给沙色，让红色只留在滑块上。
            lightcolor=line,
            darkcolor=line,
            gripcount=0,
            sliderlength=14,
            sliderthickness=14,
            relief="flat",
        )

    # --- Scrollbar ----------------------------------------------------------
    for sb_style in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(
            sb_style,
            background=line,
            troughcolor=card,
            bordercolor=card,
            lightcolor=line,
            darkcolor=line,
            arrowcolor=muted,
            arrowsize=9,
            width=10,
            relief="flat",
            borderwidth=0,
        )
        style.map(
            sb_style,
            background=[("pressed", cyan), ("active", _shade(cyan, 0.8))],
            lightcolor=[("active", cyan)],
            darkcolor=[("active", _shade(cyan, 0.6))],
            arrowcolor=[("active", cyan)],
        )

    # --- Labelframe（分区卡片）---------------------------------------------
    # 用 panel 底 + 1px line 描边模拟圆角卡片；标题用 cyan 9 号 bold，
    # 界面侧的「① ② ③ ④」前缀由 text= 传入，主题这边不动。
    style.configure(
        "TLabelframe",
        background=panel,
        bordercolor=line,
        lightcolor=panel,
        darkcolor=panel,
        relief="solid",
        borderwidth=1,
        padding=(8, 6),
    )
    style.configure(
        "TLabelframe.Label",
        background=panel,
        foreground=cyan,
        font=FONT_B,
        padding=(2, 0),
    )
    style.configure(
        "Card.TLabelframe",
        background=panel,
        bordercolor=line,
        lightcolor=panel,
        darkcolor=panel,
        relief="solid",
        borderwidth=1,
        # 纵向内边距 5px（原 8px）：主窗 6 个分区省下 36px 自然高度，
        # 1080p 屏上窗口能压到 ~960px 而不裁切（由 ui-engineer 实测提出）
        padding=(10, 5),
        labelmargins=(10, 2),
    )
    style.configure(
        "Card.TLabelframe.Label",
        background=panel,
        foreground=cyan,
        font=FONT_B,
        padding=(2, 0),
    )

    # --- Separator ----------------------------------------------------------
    style.configure("TSeparator", background=line, bordercolor=line,
                    lightcolor=line, darkcolor=line)

    # 防止 Style 被 GC（部分 Tk 构建里 Style 持有解释器状态）
    try:
        root._retro_style = style  # type: ignore[attr-defined]
    except (AttributeError, tk.TclError):
        pass
    return style


def _use_clam(style: ttk.Style) -> None:
    """切到 clam 主题（不可用时保持现状，保证不抛异常）。"""
    try:
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError:  # pragma: no cover - 极少见的无 clam 构建
        pass


#: 像素勾选符号：8x8 内区里的「✓」坐标（相对 12x12 图标的绝对坐标）
_TICK_PIXELS: tuple[tuple[int, int], ...] = (
    (8, 3), (7, 4), (8, 4),                      # 长臂顶端
    (2, 5), (6, 5), (7, 5),                      # 短臂起点 + 长臂
    (3, 6), (5, 6), (6, 6),
    (4, 7), (5, 7),
    (4, 8),                                      # 拐点
)

#: 保持 PhotoImage 引用，防止 image element 使用的位图被 GC
_CHECK_IMAGE_KEEPALIVE: list[tk.PhotoImage] = []


def _pixel_check_image(root: tk.Misc, checked: bool, dim: bool = False) -> tk.PhotoImage:
    """画一枚像素勾选框位图（12x12 图标 + 右侧 3px 留白，共 15x12）。

    Args:
        root: 用于创建 PhotoImage 的窗口。
        checked: ``True`` 画选中态（青色实心 + 深色勾）。
        dim: ``True`` 画禁用态（压暗，避免禁用项看起来仍可点）。

    Returns:
        15x12 的 PhotoImage。
    """
    side = 12
    margin = 3
    panel = PALETTE["panel"]
    card = PALETTE["card"]
    cyan = PALETTE["cyan"]
    ring = _mix(PALETTE["line"], PALETTE["muted"], 0.50)
    fill = _shade(cyan, 0.42) if dim else cyan
    inner = _shade(card, 0.85) if dim else card
    tick = PALETTE["panel"] if dim else _ON_ACCENT
    outline = _shade(ring, 0.75) if dim else ring
    grid = [[panel for _ in range(side + margin)] for _ in range(side)]

    # 外框（1px）：1..10 之间
    for i in range(1, 11):
        grid[1][i] = outline
        grid[10][i] = outline
        grid[i][1] = outline
        grid[i][10] = outline
    # 内区：2..9
    for y in range(2, 10):
        for x in range(2, 10):
            grid[y][x] = inner

    if checked:
        for i in range(1, 11):
            grid[1][i] = fill
            grid[10][i] = fill
            grid[i][1] = fill
            grid[i][10] = fill
        for y in range(2, 10):
            for x in range(2, 10):
                grid[y][x] = fill
        for x, y in _TICK_PIXELS:
            grid[y][x] = tick

    image = tk.PhotoImage(master=root, width=side + margin, height=side)
    image.put(" ".join("{" + " ".join(row) + "}" for row in grid))
    _CHECK_IMAGE_KEEPALIVE.append(image)
    return image


def _install_pixel_checkbutton(style: ttk.Style, root: tk.Misc) -> None:
    """把 TCheckbutton 的指示器换成手工像素位图（clam 默认勾号在小尺寸下像「✕」）。

    幂等：同一解释器内重复调用直接返回，不会重复 element_create。

    Args:
        style: 已切到 clam 的 Style。
        root: 主窗口（用于创建 PhotoImage）。
    """
    element = "Retro.Checkbutton.indicator"
    try:
        if element in style.element_names():
            return
    except tk.TclError:  # pragma: no cover
        return

    try:
        off_image = _pixel_check_image(root, checked=False)
        on_image = _pixel_check_image(root, checked=True)
        off_dim = _pixel_check_image(root, checked=False, dim=True)
        on_dim = _pixel_check_image(root, checked=True, dim=True)
        style.element_create(
            element, "image", off_image,
            ("disabled", "selected", on_dim),
            ("disabled", off_dim),
            ("selected", on_image),
            border=0, sticky="")
        style.layout("TCheckbutton", [
            ("Checkbutton.padding", {
                "sticky": "nswe",
                "children": [
                    (element, {"side": "left", "sticky": ""}),
                    ("Checkbutton.focus", {
                        "side": "left", "sticky": "w",
                        "children": [("Checkbutton.label", {"sticky": "nswe"})],
                    }),
                ],
            }),
        ])
    except tk.TclError:  # pragma: no cover - 极少数 Tk 构建不支持 image element
        return
    try:
        root._retro_check_images = (off_image, on_image, off_dim, on_dim)  # type: ignore[attr-defined]
    except (AttributeError, tk.TclError):
        pass


def _font_spec(font: tuple) -> str:
    """把字体元组转成 option database 能安全解析的 ``-font`` 描述串。

    Tk 把 ``-font`` 当作「字体描述列表」解析，族名里带空格（例如
    ``Microsoft YaHei UI``）必须用花括号包起来，否则 ``YaHei`` 会被当成字号，
    触发 ``expected integer but got "YaHei"``（tk.Listbox / tk.Menu 会直接崩）。

    Args:
        font: ``("Microsoft YaHei UI", 9)`` 这类字体元组。

    Returns:
        ``"{Microsoft YaHei UI} 9"`` 形式的描述串。
    """
    family = str(font[0]).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    parts = ["{%s}" % family]
    parts.extend(str(item) for item in font[1:])
    return " ".join(parts)


def _style_option_db(root: tk.Misc) -> None:
    """给 tk 原生控件（下拉列表 / 菜单 / 文本 / 列表）写 option database 默认色。"""
    font_spec = _font_spec(FONT)
    mono_spec = _font_spec(FONT_MONO)
    defaults = {
        # Combobox 下拉列表
        "*TCombobox*Listbox.background": PALETTE["card"],
        "*TCombobox*Listbox.foreground": PALETTE["text"],
        "*TCombobox*Listbox.selectBackground": PALETTE["sel"],
        "*TCombobox*Listbox.selectForeground": _ON_SEL,
        "*TCombobox*Listbox.borderWidth": "1",
        "*TCombobox*Listbox.font": font_spec,
        # 原生 Text
        "*Text.background": PALETTE["card"],
        "*Text.foreground": PALETTE["text"],
        "*Text.insertBackground": PALETTE["cyan"],
        "*Text.selectBackground": PALETTE["sel"],
        "*Text.selectForeground": _ON_SEL,
        "*Text.font": mono_spec,
        "*Text.borderWidth": "1",
        "*Text.highlightThickness": "1",
        "*Text.highlightBackground": PALETTE["line"],
        "*Text.highlightColor": PALETTE["cyan"],
        # 原生 Listbox
        "*Listbox.background": PALETTE["card"],
        "*Listbox.foreground": PALETTE["text"],
        "*Listbox.selectBackground": PALETTE["sel"],
        "*Listbox.selectForeground": _ON_SEL,
        "*Listbox.highlightThickness": "1",
        "*Listbox.highlightBackground": PALETTE["line"],
        "*Listbox.highlightColor": PALETTE["cyan"],
        "*Listbox.borderWidth": "0",
        "*Listbox.font": font_spec,
        # 右键菜单
        "*Menu.background": PALETTE["panel"],
        "*Menu.foreground": PALETTE["text"],
        "*Menu.activeBackground": PALETTE["sel"],
        "*Menu.activeForeground": _ON_SEL,
        "*Menu.selectColor": PALETTE["cyan"],
        "*Menu.borderWidth": "1",
        "*Menu.relief": "flat",
        "*Menu.font": font_spec,
        # Canvas（例如拖放区）默认也走夜空底
        "*Canvas.background": PALETTE["bg"],
        "*Canvas.highlightThickness": "0",
        # 原生 Entry（透明光标 / 选中色统一）
        "*Entry.insertBackground": PALETTE["cyan"],
        "*Entry.selectBackground": PALETTE["sel"],
        "*Entry.selectForeground": _ON_SEL,
    }
    add = root.option_add
    for pattern, value in defaults.items():
        try:
            add(pattern, value)
        except tk.TclError:  # 某些 pattern 在个别平台不认，忽略
            continue


# ---------------------------------------------------------------------------
# 深色标题栏（Windows 10/11 DWM）
# ---------------------------------------------------------------------------

#: DWMWA_USE_IMMERSIVE_DARK_MODE；Win10 1809 之前是 19，之后是 20，两个都试
_DWMWA_DARK_CANDIDATES = (20, 19)
#: GWL_STYLE / WS_CAPTION：没有标题栏的窗口（overrideredirect）不去折腾
_GWL_STYLE = -16
_WS_CAPTION = 0x00C00000
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020


def dark_title_bar(win: tk.Misc) -> bool:
    """让 Windows 原生标题栏跟随主题明暗（浅色主题下强制浅色标题栏）。

    函数名沿用历史接口（GUI 侧已按此名调用，不轻易改）。浅色主题下**必须显式
    写 0**：DWM 会记住上一次设置，如果只「不设置」，用户上次跑过深色版就会
    残留一条深色标题栏，跟米白窗口撞成两截。

    实现要点：
      * Tk 在 Windows 上 ``winfo_id()`` 返回的是**客户区子窗口**，必须
        ``GetParent()`` 才是带标题栏的顶层 HWND；``GetParent`` 返回 0 时退回原值。
      * 先试属性 20（Win10 1809+ / Win11），失败再试 19（1809 之前）。
      * 窗口没有 ``WS_CAPTION``（无边框窗口）时直接返回 ``False``，不抛异常。
      * 幂等：重复调用只是再设一遍同一属性；窗口已 mapped 后调用同样生效
        （设置后强制一次 ``SWP_FRAMECHANGED`` 重绘非客户区）。

    Args:
        win: ``tk.Tk`` 或 ``tk.Toplevel`` 实例。

    Returns:
        ``True`` 表示 DWM 已接受深色设置；非 Windows、无标题栏、DWM 拒绝或
        任何异常都返回 ``False``（本函数**绝不抛异常**）。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # pragma: no cover - 理论不可达
        return False

    try:
        win.update_idletasks()
        client_id = int(win.winfo_id())
    except (tk.TclError, ValueError, TypeError):
        return False

    try:
        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
    except (AttributeError, OSError):  # pragma: no cover
        return False

    try:
        # --- 拿到顶层 HWND -------------------------------------------------
        try:
            user32.GetParent.restype = wintypes.HWND
            user32.GetParent.argtypes = [wintypes.HWND]
            parent = user32.GetParent(wintypes.HWND(client_id))
        except Exception:
            parent = 0
        hwnd = parent or client_id
        if not hwnd:
            return False

        # --- 没有标题栏就别设 ---------------------------------------------
        try:
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            style = user32.GetWindowLongW(wintypes.HWND(hwnd), _GWL_STYLE)
            if not (style & _WS_CAPTION):
                return False
        except Exception:
            pass  # 取不到样式就继续尝试，交给 DWM 判定

        # --- 逐个候选属性尝试 ---------------------------------------------
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        applied = False
        value = ctypes.c_int(0 if IS_LIGHT else 1)
        for attribute in _DWMWA_DARK_CANDIDATES:
            try:
                result = dwmapi.DwmSetWindowAttribute(
                    wintypes.HWND(hwnd), wintypes.DWORD(attribute),
                    ctypes.byref(value), ctypes.sizeof(value))
            except Exception:
                continue
            if result == 0:
                applied = True
                break
        if not applied:
            return False

        # --- 强制重绘非客户区，让标题栏颜色立刻生效 ------------------------
        try:
            user32.SetWindowPos(
                wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE
                | _SWP_FRAMECHANGED)
        except Exception:
            pass
        return True
    except Exception:
        # 任何 DWM / user32 层的意外都吞掉：主题绝不能把主程序带崩
        return False


# ---------------------------------------------------------------------------
# 动画面板
# ---------------------------------------------------------------------------

class RetroBanner(tk.Canvas):
    """程序化绘制的像素横幅（淡网点 + 透视网格 + 频谱 + 像素卡带）。

    所有画面元素都用 Canvas 图元实时绘制，无外部图片。静态图层只画一次，
    动画循环每帧只更新 14 根频谱条的矩形与峰值亮边（28 次 ``coords`` 调用）。

    Args:
        master: 父容器。
        width: 初始宽（像素）。
        height: 初始高（像素）。
        **kw: 透传给 :class:`tkinter.Canvas`（``bg`` 默认取调色板 bg）。
    """

    STAR_COUNT = 60
    BAR_COUNT = 14
    GRID_H_LINES = 5
    GRID_V_LINES = 8          # 放射线数量（从消失点向底边发散）
    CASSETTE_W = 40
    CASSETTE_H = 28
    #: 宽度小于该值时隐藏像素卡带图标
    NARROW_WIDTH = 600
    #: 主标题文案（改这里就够，不用翻 _draw_caption）
    TITLE_TEXT = BANNER_TITLE

    def __init__(self, master: tk.Misc, width: int = 820, height: int = 92, **kw):
        kw.setdefault("bg", PALETTE["bg"])
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        kw.setdefault("takefocus", 0)
        super().__init__(master, width=width, height=height, **kw)

        self._size: tuple[int, int] = (max(1, int(width)), max(1, int(height)))
        self._after_id: str | None = None
        self._idle_id: str | None = None
        self._frame = 0
        self._bars: list[dict] = []
        self._bar_rects: list[int] = []
        self._bar_peaks: list[int] = []
        self._bar_colors: list[str] = []

        self._rebuild(self._size[0], self._size[1])
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")

    # -- 公开 API ----------------------------------------------------------
    def start(self) -> None:
        """启动动画循环（80ms 一帧）。重复调用安全。"""
        if self._after_id is not None:
            return
        if not self._alive():
            return
        try:
            self._after_id = self.after(BANNER_INTERVAL_MS, self._tick)
        except tk.TclError:
            self._after_id = None

    def stop(self) -> None:
        """取消动画循环。可重复调用；窗口销毁后调用不抛异常。"""
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except (tk.TclError, ValueError):
                pass
            self._after_id = None
        if self._idle_id is not None:
            try:
                self.after_cancel(self._idle_id)
            except (tk.TclError, ValueError):
                pass
            self._idle_id = None

    def refresh(self) -> None:
        """按**当前主题**重画整个横幅（切主题后由 GUI 调用）。

        会先停掉动画再重画：``_rebuild`` 里 ``delete("all")`` 之后，旧图元
        句柄全部失效，动画循环若在中间跑一帧就会 ``coords`` 到不存在的 id。
        重画完把动画接回去（原本在跑才接，避免把"停住的横幅"偷偷启动）。
        """
        was_running = self._after_id is not None
        self.stop()
        if not self._alive():
            return
        try:
            self.configure(bg=PALETTE["bg"])
        except tk.TclError:
            return
        self._rebuild(*self._size)
        if was_running:
            self.start()

    # -- 内部：生命周期 -----------------------------------------------------
    def _alive(self) -> bool:
        """控件是否仍然存在（销毁后所有 Tcl 调用都会抛 TclError）。"""
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _on_destroy(self, event: tk.Event) -> None:
        """窗口销毁时自动清理 after 回调，避免 'invalid command name'。"""
        if event.widget is self:
            self.stop()

    def _on_configure(self, event: tk.Event) -> None:
        """尺寸变化后按新宽高重排（after_idle 去抖，避免连续重绘）。"""
        if event.widget is not self:
            return
        width = max(1, int(event.width))
        height = max(1, int(event.height))
        if (width, height) == self._size:
            return
        self._size = (width, height)
        if self._idle_id is not None:
            try:
                self.after_cancel(self._idle_id)
            except (tk.TclError, ValueError):
                pass
            self._idle_id = None
        if not self._alive():
            return
        try:
            self._idle_id = self.after_idle(self._flush_resize)
        except tk.TclError:
            self._idle_id = None

    def _flush_resize(self) -> None:
        self._idle_id = None
        if not self._alive():
            return
        self._rebuild(*self._size)
        self._draw_frame(self._frame)

    # -- 内部：分层绘制 -----------------------------------------------------
    def _rebuild(self, width: int, height: int) -> None:
        """重建静态图层与频谱条骨架。"""
        self._size = (width, height)
        try:
            self.delete("all")
        except tk.TclError:
            return
        self._bars = []
        self._bar_rects = []
        self._bar_peaks = []
        self._bar_colors = []

        self._draw_sky(width, height)
        self._draw_grid(width, height)
        self._draw_bars(width, height)
        if width >= self.NARROW_WIDTH:
            self._draw_cassette(width, height)
        self._draw_caption(width, height)
        self._draw_divider(width, height)
        self._draw_frame(self._frame)

    def _draw_sky(self, width: int, height: int) -> None:
        """a. 底色 + 60 颗像素点（浅色主题下读作「半调网点」，不是星空）。

        底色直接由 Canvas 自身的 ``bg`` 提供（不再画一块整幅矩形，省 1 个图元）。
        """
        # 上方 8-bit 阶梯「天空」：4 段等宽色块，刻意保留色阶断层而不是平滑渐变
        # —— 主机画面本来就只有这么多色阶，比渐变更有年代感。
        band_h = max(4, int(height * 0.55))
        steps = 4
        for i in range(steps):
            y0 = band_h * i // steps
            y1 = band_h * (i + 1) // steps
            t = 0.75 - 0.55 * (i / float(steps - 1))     # 顶亮 → 接近地平线变回底色
            self.create_rectangle(0, y0, width, y1,
                                  fill=_mix(PALETTE["bg"], PALETTE["panel"], t),
                                  outline="", tags=("sky",))
        rng = random.Random(0x5EED)  # 固定种子：每次启动点位一致
        # 点只落在上半部，避免压到副标题与透视网格影响可读性
        star_bottom = max(2, int(height * 0.55))
        for index in range(self.STAR_COUNT):
            x = rng.randint(1, max(1, width - 2))
            y = rng.randint(1, star_bottom)
            size = 2 if index % 5 == 0 else 1
            # 浅色底下「白点」等于隐形，改用墨色/砖红的淡网点
            color = PALETTE["cyan"] if index % 3 else PALETTE["text"]
            color = _mix(color, PALETTE["bg"], rng.uniform(0.35, 0.78))
            self.create_rectangle(x, y, x + size, y + size,
                                  fill=color, outline="", tags=("star",))

    def _draw_grid(self, width: int, height: int) -> None:
        """b. 标准 synthwave 透视网格（8 条放射线 + 5 条水平线 + 地平线双描边）。

        消失点固定在画面中心偏下 ``(w/2, h*0.62)``：
          * 放射线从消失点向画面底边发散，越靠外间距越大（幂次分布）；
          * 水平线在消失点以下，行距按几何级数向下拉开（近处更疏）；
          * 颜色做二次插值：远端极淡（_tint 0.80）→ 近端稍深（_tint 0.55）。
            ⚠ 浅色底上「线要淡」必须混底色（_tint），用 _shade 会越压越黑。

        共 15 个静态图元，动画不碰这里。
        """
        vanish_x, horizon = self._grid_origin(width, height)
        near = _tint(PALETTE["cyan"], 0.42)
        far = _tint(PALETTE["magenta"], 0.66)
        # 留 4px 余量：第 5 条水平线若落在外框边界上会被裁掉，只剩 4 条可见
        depth = max(1.0, float(height - 4) - horizon)

        # 1) 地平线：2px 淡青打底 + 1px 砖红实线压在上面（像印刷的套色偏移）
        self.create_line(0, horizon, width, horizon,
                         fill=_tint(PALETTE["magenta"], 0.42), width=2,
                         tags=("grid", "horizon"))
        self.create_line(0, horizon, width, horizon,
                         fill=_tint(PALETTE["cyan"], 0.12), width=1,
                         tags=("grid", "horizon"))

        # 2) 放射线：对称分布、外宽内窄（|t|^1.6 让边缘间距递增）
        half_span = width * 0.55
        radial = _mix(far, near, 0.5)
        for i in range(self.GRID_V_LINES):
            t = ((i + 0.5) / self.GRID_V_LINES) * 2.0 - 1.0     # -0.875 … 0.875
            x_bottom = vanish_x + math.copysign(half_span * abs(t) ** 1.6, t)
            self.create_line(vanish_x, horizon, x_bottom, height,
                             fill=radial, width=1, tags=("grid", "radial"))

        # 3) 水平线：行距按几何级数递增（近处更疏），且首条与地平线拉开 ≥4px，
        #    否则会被地平线那 2px 微光吞掉，视觉上只剩 4 条。
        ratio = 1.18
        gap = depth * (ratio - 1.0) / (ratio ** self.GRID_H_LINES - 1.0)
        y = horizon
        for i in range(self.GRID_H_LINES):
            y += gap * (ratio ** i)
            t = (y - horizon) / depth
            self.create_line(0, y, width, y,
                             fill=_mix(far, near, t), width=1,
                             tags=("grid", "hbar"))

    @staticmethod
    def _grid_origin(width: int, height: int) -> tuple[float, float]:
        """返回网格消失点 ``(x, y)``：水平居中、垂直位于 62% 高度处。"""
        return width * 0.5, height * 0.62

        # 纵向线：从灭点向底部发散，分三段做青→品红渐变
        span = max(1.0, float(width))
        for i in range(self.GRID_V_LINES):
            offset = (i - (self.GRID_V_LINES - 1) / 2.0) / ((self.GRID_V_LINES - 1) / 2.0)
            x_bottom = vanish_x + offset * span * 0.62
            prev_x, prev_y = vanish_x, horizon
            for seg in range(1, 4):
                t = seg / 3.0
                x = vanish_x + (x_bottom - vanish_x) * t
                y = horizon + depth * t
                color = _shade(_mix(cyan, magenta, 0.2 + 0.7 * (seg - 1) / 2.0), 0.6)
                self.create_line(prev_x, prev_y, x, y, fill=color,
                                 width=1, tags=("grid",))
                prev_x, prev_y = x, y

    def _draw_bars(self, width: int, height: int) -> None:
        """c. 右侧 14 根频谱条骨架（动画只更新这些图元）。"""
        count = self.BAR_COUNT
        area_w = int(min(300, max(120, width * 0.34)))
        right_pad = 12
        gap = 3
        bar_w = max(3, (area_w - (count - 1) * gap) // count)
        area_w = bar_w * count + (count - 1) * gap
        left = max(0, width - right_pad - area_w)
        base_y = height - 8
        # 高度只占可用空间的 2/3，避免频谱压过标题、抢走版面重心
        max_h = max(6, int((base_y - 10) * 0.66))

        rng = random.Random(0xC0FFEE)
        for i in range(count):
            x = left + i * (bar_w + gap)
            t = i / float(count - 1) if count > 1 else 0.0
            color = _mix(PALETTE["cyan"], PALETTE["magenta"], t)
            self._bar_colors.append(color)
            self._bars.append({
                "x": x,
                "w": bar_w,
                "base_y": base_y,
                "max_h": max_h,
                "phase": rng.uniform(0.0, math.tau),
                "speed": rng.uniform(0.16, 0.34),
                "swing": rng.uniform(0.55, 1.0),
                "peak": 0.0,
            })
            self._bar_rects.append(self.create_rectangle(
                x, base_y - 2, x + bar_w, base_y,
                fill=color, outline="", tags=("bar",)))
            self._bar_peaks.append(self.create_line(
                x, base_y - 3, x + bar_w, base_y - 3,
                fill=_peak_color(color), width=1, tags=("bar",)))

    def _draw_cassette(self, width: int, height: int) -> None:
        """d. 左侧像素卡带图标（约 40x28，深青描边 + 琥珀标签）。"""
        w = self.CASSETTE_W
        h = self.CASSETTE_H
        x0 = 14
        y0 = max(4, (height - h) // 2 - 2)
        x1 = x0 + w
        y1 = y0 + h

        self.create_rectangle(x0, y0, x1, y1,
                              fill=PALETTE["card"], outline=PALETTE["magenta"],
                              width=1, tags=("cassette",))
        # 顶部标签条（琥珀）
        self.create_rectangle(x0 + 3, y0 + 3, x1 - 3, y0 + 10,
                              fill=PALETTE["amber"], outline="", tags=("cassette",))
        # 标签条上的两道「手写线」
        self.create_rectangle(x0 + 5, y0 + 5, x1 - 12, y0 + 6,
                              fill=_shade(PALETTE["amber"], 0.55), outline="",
                              tags=("cassette",))
        self.create_rectangle(x0 + 5, y0 + 8, x1 - 18, y0 + 9,
                              fill=_shade(PALETTE["amber"], 0.55), outline="",
                              tags=("cassette",))
        # 两个卷轴孔
        for cx in (x0 + 12, x1 - 12):
            cy = y1 - 8
            self.create_oval(cx - 5, cy - 5, cx + 5, cy + 5,
                             fill=PALETTE["bg"], outline=PALETTE["cyan"], width=1,
                             tags=("cassette",))
            self.create_oval(cx - 2, cy - 2, cx + 2, cy + 2,
                             fill=_shade(PALETTE["magenta"], 0.85), outline="",
                             tags=("cassette",))
        # 卷轴之间的磁带窗口
        self.create_rectangle(x0 + 18, y1 - 11, x1 - 18, y1 - 5,
                              fill=_shade(PALETTE["card2"], 1.0),
                              outline=PALETTE["line"], width=1, tags=("cassette",))
        # 底部两颗固定螺丝
        for cx in (x0 + 4, x1 - 4):
            self.create_rectangle(cx - 1, y1 - 4, cx + 1, y1 - 2,
                                  fill=PALETTE["dim"], outline="", tags=("cassette",))

    def _draw_caption(self, width: int, height: int) -> None:
        """e. 主标题 + 副标题（窄屏时靠左，宽屏时排在卡带右侧）。

        文字整体压在透视网格地平线之上，保证「SPC · PCM · CD AUDIO」不被网格线穿过；
        高度不足 80px（例如界面里 60px 的紧凑横幅）时自动缩小字号并压缩行距。

        标题用**等宽编程字体 + 手工字距**（v4.18）：主机只认识等宽字模，
        逐字符排 + 每字加一点间隙，看起来就像终端/代码编辑器，比默认的
        Consolas 连排更像「geek 面板」。
        """
        has_cassette = width >= self.NARROW_WIDTH
        text_x = 74 if has_cassette else 16
        horizon = self._grid_origin(width, height)[1]
        compact = height < 80
        title_size = 13 if compact else 16
        # ⚠ 两行整体要压在**地平线之上**：地平线画在 62% 高度处，副标题若骑在
        #   线上，会被那 2px 双描边 + 第一条横网格穿过，读起来像被划掉
        #   （v4.18 加长副标题后实测踩到）。所以比例从 0.52 收到 0.44，
        #   行距也跟着收 —— 80px 高时副标题底边离地平线还有 5px 余量。
        title_y = int(horizon * (0.42 if compact else 0.44))
        subtitle_y = min(title_y + (13 if compact else 17), horizon - 6)

        # 阴影先画（向右下偏 2px），再压主色 —— 8-bit 字体常见的硬投影。
        # ⚠ 阴影色必须用**中性灰**（text 往底色稀释），不能用强调色稀释：
        #   砖红混米黄会调出一坨土黄（#dccd9c），压在主色下面像脏了一块。
        self._draw_title(self.TITLE_TEXT, text_x + 2, title_y + 2,
                         title_size, _tint(PALETTE["text"], 0.70))
        self._draw_title(self.TITLE_TEXT, text_x, title_y,
                         title_size, PALETTE["cyan"])

        self.create_text(
            text_x + 1, subtitle_y,
            text="SPC · PCM · CD AUDIO  —  8/16-BIT 音乐工作站",
            anchor="w",
            fill=PALETTE["dim"],
            font=("Microsoft YaHei UI", 8),
            tags=("caption",),
        )

    def _draw_title(self, text: str, x: float, y: float,
                    size: int, fill: str) -> float:
        """逐字符画标题（等宽字体 + 手工字距），返回右端 x 坐标。

        Canvas 没有 letter-spacing，想让字距变宽只能一个字一个字排：
        每字符的推进量 = 该字符实际宽度 + 间隙。间隙取字号的 1/8，
        16pt 下约 2px —— 少了看不出来，多了会散架。
        """
        font = banner_title_font(self, size)
        if font is None:      # 字体子系统不可用的极端情况：退回单行绘制
            self.create_text(x, y, text=text, anchor="w", fill=fill,
                             font=(BANNER_FONT_STACK[-1], size, "bold"),
                             tags=("caption",))
            return x
        gap = max(1, int(round(size / 8.0)))
        cursor = float(x)
        for ch in text:
            self.create_text(cursor, y, text=ch, anchor="w", fill=fill,
                             font=font, tags=("caption",))
            cursor += font.measure(ch) + gap
        return cursor

    def _draw_divider(self, width: int, height: int) -> None:
        """f. 底部 1px 分割线（砖红，往底色稀释到 45%）。"""
        self.create_line(0, height - 1, width, height - 1,
                         fill=_tint(PALETTE["cyan"], 0.45), width=1,
                         tags=("divider",))

    # -- 内部：动画 ---------------------------------------------------------
    def _tick(self) -> None:
        """定时器回调：推进一帧并重新排下一次。"""
        self._after_id = None
        if not self._alive():
            return
        self._frame += 1
        try:
            self._draw_frame(self._frame)
            self._after_id = self.after(BANNER_INTERVAL_MS, self._tick)
        except tk.TclError:
            self._after_id = None

    def _draw_frame(self, frame: int) -> None:
        """只更新会动的图元：14 根频谱条 + 14 条峰值亮边。"""
        if not self._bars:
            return
        try:
            for index, spec in enumerate(self._bars):
                base_y = spec["base_y"]
                max_h = spec["max_h"]
                wave = 0.5 + 0.5 * math.sin(
                    frame * spec["speed"] + spec["phase"] + index * 0.42)
                jitter = 0.5 + 0.5 * math.sin(frame * 0.19 + index * 1.7)
                level = spec["swing"] * (0.25 + 0.75 * wave) * (0.55 + 0.45 * jitter)
                level = max(0.06, min(1.0, level))
                bar_h = max(2, int(level * max_h))
                x = spec["x"]
                bar_w = spec["w"]

                self.coords(self._bar_rects[index], x, base_y - bar_h, x + bar_w, base_y)

                peak = spec["peak"]
                if level >= peak:
                    peak = level
                else:
                    peak = max(level, peak - 0.035)
                spec["peak"] = peak
                peak_y = base_y - max(3, int(peak * max_h)) - 2
                self.coords(self._bar_peaks[index], x, peak_y, x + bar_w, peak_y)
        except tk.TclError:
            # 控件在绘制途中被销毁：静默退出，不要再排下一帧
            self.stop()


# ---------------------------------------------------------------------------
# 窗口图标（32x32，PhotoImage.put 逐行写入）
# ---------------------------------------------------------------------------

def app_icon(root: tk.Misc) -> tk.PhotoImage:
    """生成 32x32 像素图标（深底 + 青色描边卡带 + 音符）并设置到窗口。

    图标完全由代码逐像素写入，不依赖任何图片文件；返回的
    :class:`tk.PhotoImage` 会挂在 ``root._retro_icon`` 上防止被 GC。

    Args:
        root: 要设置图标的主窗口。

    Returns:
        该图标对象（调用方可忽略）。
    """
    size = 32
    dark = PALETTE["bg"]
    body = PALETTE["panel"]
    edge = PALETTE["cyan"]
    label = PALETTE["amber"]
    note = PALETTE["magenta"]

    pixels = [[dark for _ in range(size)] for _ in range(size)]

    def fill(x0: int, y0: int, x1: int, y1: int, color: str) -> None:
        """把 [x0,x1) x [y0,y1) 区域刷成 color（自动裁边）。"""
        for y in range(max(0, y0), min(size, y1)):
            row = pixels[y]
            for x in range(max(0, x0), min(size, x1)):
                row[x] = color

    # 1) 外框：1px 青色描边 + 深色内芯（切角做成像素化的圆角）
    fill(2, 3, 30, 29, edge)
    fill(3, 4, 29, 28, body)
    fill(2, 2, 30, 3, dark)
    fill(2, 29, 30, 30, dark)
    fill(1, 4, 3, 28, dark)
    fill(29, 4, 31, 28, dark)
    # 四角像素切角
    fill(2, 3, 4, 5, dark)
    fill(28, 3, 30, 5, dark)
    fill(2, 27, 4, 29, dark)
    fill(28, 27, 30, 29, dark)

    # 2) 卡带本体
    fill(6, 8, 26, 24, PALETTE["card"])
    fill(6, 8, 26, 9, edge)
    fill(6, 23, 26, 24, edge)
    fill(6, 8, 7, 24, edge)
    fill(25, 8, 26, 24, edge)

    # 3) 琥珀标签条
    fill(9, 11, 23, 15, label)
    fill(9, 11, 23, 12, _shade(label, 0.62))

    # 4) 两个卷轴孔
    for cx in (12, 20):
        fill(cx - 3, 17, cx + 3, 21, PALETTE["bg"])
        fill(cx - 3, 17, cx + 3, 18, note)
        fill(cx - 1, 18, cx + 1, 20, note)

    # 5) 右上角一枚小音符（区别于「就是一张卡带」）
    fill(23, 4, 25, 6, note)
    fill(25, 3, 27, 9, note)
    fill(22, 5, 24, 7, note)

    image = tk.PhotoImage(master=root, width=size, height=size)
    image.put(" ".join("{" + " ".join(row) + "}" for row in pixels))

    try:
        root.iconphoto(True, image)
    except tk.TclError:  # 无窗口系统的环境（例如纯 Tcl 测试）忽略
        pass
    # 必须持有引用，否则 PhotoImage 会被 GC 导致图标消失
    try:
        root._retro_icon = image  # type: ignore[attr-defined]
    except (AttributeError, tk.TclError):
        pass
    return image


# ---------------------------------------------------------------------------
# 命令行自检
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--all" in sys.argv[1:]:
        # 逐套自检：改任何一格颜色后都跑这条，退出码 1 表示有主题不达标
        total = 0
        for _key in THEME_ORDER:
            total += report_contrast(theme=_key)
        sys.stdout.write("\n合计未达标: %d 项（%d 套主题）\n"
                         % (total, len(THEME_ORDER)))
        sys.exit(1 if total else 0)
    failed = report_contrast()
    sys.exit(1 if failed else 0)
