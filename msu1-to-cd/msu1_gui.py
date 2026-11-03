#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
msu1_gui.py - 游戏音乐转换器 可视化客户端

音源:
  * MSU-1 游戏文件夹 (内含 *.pcm)
  * 光盘镜像 (.cue + .bin/.iso): PS1 / PCECD / MDCD(Sega CD) / Saturn 等 CD-DA 音轨
  * MP3
  * SPC (SNES-SPC700): 单个 .spc 或含 .spc 的文件夹 (libgme 解码)

输出:
  * 音频 CD 镜像 (BIN+CUE)  -> ImgBurn 烧 CD-R
  * WAV (Pico-Plus)         -> 16-bit / 44100Hz / 立体声, 拷到 SD 卡播放

仅依赖 Python 标准库 (tkinter), 复用 msu1_to_cd.py / spc_wav.py。
运行: python msu1_gui.py
"""

import ctypes
import math
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, font as tkfont, messagebox, scrolledtext, ttk

import msu1_to_cd as core
import retro_theme as theme
import spc_wav
import zh_online
import zh_titles

# 音源下载(joshw.info 镜像)两个模块是可选依赖: 打包时若漏收, 界面仍要能开,
# 只是下载窗口会给出明确提示, 而不是启动就 ImportError。
try:
    import joshw_source as jw
except Exception:                                    # noqa: BLE001
    jw = None
try:
    import joshw_dl
except Exception:                                    # noqa: BLE001
    joshw_dl = None

# 多格式芯片音乐(T01/T02)同样是可选依赖, 但**模块级 import**而不是惰性 import:
# PyInstaller 靠静态分析收集依赖, 惰性 import 会让 chip_formats/chip_decode
# 漏进 exe(设计文档风险 H16)。缺了不影响 SPC / MSU-1 / cue / mp3 的老路径,
# 只是多格式识别与芯片解码会给出明确提示。
try:
    import chip_formats
except Exception:                                    # noqa: BLE001
    chip_formats = None
try:
    import chip_decode
except Exception:                                    # noqa: BLE001
    chip_decode = None
# v4.40: PSF（PS1/PS2 芯片音源）→ WAV 的半自动桥接。
# 本工具不内置 PS1 模拟器，改为驱动用户自己的 foobar2000 + foo_psf。
# 模块只依赖标准库，缺失时只影响 PSF 那条路，不影响其它格式。
try:
    import psf_bridge
except Exception:                                    # noqa: BLE001
    psf_bridge = None
# v4.41: PS1 的「.txtp 包」原生解码（.txtp 索引 -> .xa / .DA）。
# 与 psf_bridge 相反 —— 这条路**不需要 foobar2000、不需要模拟器**，
# 因为 .txtp 指向的是标准 CD-XA 扇区或裸 PCM，纯 stdlib 就能解。
try:
    import psx_txtp
except Exception:                                    # noqa: BLE001
    psx_txtp = None

# 网易云云盘上传(v4.19)同样是可选依赖: 需要 soundfile(FLAC 编码) /
# pycryptodome(接口加密) / segno(二维码矩阵)。缺任何一个都只让「上传云盘」
# 按钮给出明确提示, 不影响转换主流程, 更不会启动就 ImportError。
try:
    import netease_cloud as ncm
except Exception:                                    # noqa: BLE001
    ncm = None

# netease_cookie 只依赖标准库（内置 WebSocket 客户端 + CDP），因此**永远不会**
# 因为缺包而失败；单独 try 是为了万一语法/平台问题不拖垮主程序。
try:
    import netease_cookie as nck
except Exception:                                    # noqa: BLE001
    nck = None

# v4.0: 下载窗口「下载后自动转 WAV」的转换基座。同样是**模块级 import**
# 而不是惰性 import —— 与上面 chip_formats/chip_decode 同款理由（PyInstaller
# 靠静态分析收集依赖, 惰性 import 会让 dl_convert 漏进 exe）。
#
# ⚠ dl_convert 内部**一行解码代码都没有**: 它只负责分流(一行)、队列线程、
#   manifest 与磁盘门禁, 真正的解码全部委托给 spc_wav / chip_decode
#   （v3.9 已跑通的两条路径）。缺了它下载窗口照常可用, 只是没有转 WAV。
try:
    import dl_convert
except Exception:                                    # noqa: BLE001
    dl_convert = None

# v4.24: 多轨合并（一个音源产出多个 WAV 时再拼一个整轨文件）。
# 与上面同理 —— 模块级 import 才会被 PyInstaller 静态收集。
try:
    import wavmerge
except Exception:                                    # noqa: BLE001
    wavmerge = None

# v4.30: CD-DA 音轨解码（PyAV/FFmpeg）。同样模块级 import —— 少了它
# 「下载后转 WAV」会把 .ape 整批跳过（cd_audio=None 时 is_convertible 也不放行）。
try:
    import cd_audio
except Exception:                                    # noqa: BLE001
    cd_audio = None

APP_TITLE = "RetroMusic Lab · 游戏音乐转换器"
APP_VER = "4.44"   # 4.44: 临时文件统一放 D:\MSU1Temp（不再吃 C 盘）+ 24h 旧残留自动回收

# 日志区的底色/字色：底色取 card 与 bg 之间（「纸」而不是「白纸」），字色用
# 主题的墨色。**必须现算**——v4.18 起主题可切，写死常量换个主题就跟不上。
def log_colors():
    """返回当前主题下日志区的 (底色, 字色)。"""
    p = theme.PALETTE
    return theme.mix_color(p["card"], p["bg"], 0.45), p["text"]

# ---- 多格式芯片音乐: 界面侧的常量 -------------------------------------------
# ③ 区「默认单遍」Spinbox 的默认值与范围(设计 §3.4)。硬顶 600s 与
# msu1_to_cd._SPC_MAX_SECS 一致, 这里只做界面钳制。
CHIP_DEFAULT_SECS = 150.0
CHIP_SECS_MIN = 30
CHIP_SECS_MAX = 600
# 非 SPC 音源的尾部淡出秒数(与 spc_wav._limit_len 的 1500ms 同一常量语义)。
CHIP_FADE_SECS = 1.5
# 体量提醒阈值: 超过 2 GiB 才弹确认框(设计 §6.4 / 风险 R3)。
CHIP_WARN_BYTES = 2 * 1024 ** 3

# 时长决策链的来源 -> 时长列的后缀标记(设计 §3.5)。
#   无后缀 = 时长来自文件自带(精确); ◂ = 取自循环点; * = 按策略估算
_EXACT_SRC = ("id666", "nsfe_length", "vgm_samples")

# 格式族 -> 徽标色。Treeview 的 tag 作用于**整行**(ttk 不支持单格着色,
# 设计 §7.3 已说明), 所以"格式徽标按族着色"只能落在行前景色上。
FMT_FAMILY_COLOR = {
    "NSF": "cyan", "NSFE": "cyan",                    # NES / FC
    "SPC": "green",                                  # SNES
    "GBS": "green",                                  # Game Boy
    "HES": "magenta",                                # PC Engine / TurboGrafx
    "VGM": "amber", "VGZ": "amber",                  # Mega Drive / Sega CD
    "MP3": "text",
    "光盘镜像": "text",
    "MSU-1": "text",
}


# ---------------------------------------------------------------------------
# 多格式芯片音乐: chip_formats 的薄适配层
# ---------------------------------------------------------------------------
# 为什么要有这一层(而不是让 App 直接摸 chip_formats.xxx):
#   1. chip_formats 是 T01 另一个工程师交付的模块, 字段/函数签名仍在冻结期。
#      界面侧只依赖本文件里这一处, 对方改签名时只需要改这里, 不用全 App 找。
#   2. chip_formats 缺失时(打包漏收 / 开发中途)整个界面必须照常可用:
#      SPC / MSU-1 / cue / mp3 走老路径, 多格式入口给出明确提示而不是崩。
#   3. 时长决策链 plan_duration 若签名与设计不符, 这里退化成"按 ChipInfo 的
#      字段自己算"(设计 §3.1 的同一套优先级), 保证列表里显示的时长与
#      worker 侧一致。
class Chip:
    """chip_formats / chip_decode 的可用性与元信息封装(全部不抛异常)。"""

    @staticmethod
    def ready():
        """chip_formats 是否可用。False 时多格式入口一律给出明确提示。"""
        return chip_formats is not None

    @staticmethod
    def exts():
        """芯片音源扩展名元组; 不可用时返回空元组。"""
        if chip_formats is None:
            return ()
        for attr in ("CHIP_EXTS", "P0_CHIP_EXTS", "ALL_EXTS"):
            value = getattr(chip_formats, attr, None)
            if value:
                return tuple(str(v).lower() for v in value)
        return ()

    @staticmethod
    def supported(fmt):
        """这个格式 id 是否属于本工具的交付范围(P0 六种 + SPC)。"""
        if chip_formats is None or not fmt:
            return False
        p0 = tuple(getattr(chip_formats, "P0_ENABLED", ()) or ())
        return str(fmt).upper() in p0 or str(fmt).upper() == "SPC"

    @staticmethod
    def probe(path):
        """probe(path) -> ChipInfo 或 None。永不抛异常。"""
        if chip_formats is None:
            return None
        try:
            return chip_formats.probe(path)
        except Exception:                            # noqa: BLE001
            return None

    @staticmethod
    def collect_files(paths):
        """展开目录里的芯片音源文件列表; 不可用时返回空表。"""
        if chip_formats is None:
            return []
        fn = getattr(chip_formats, "collect_files", None)
        if fn is None:
            return []
        try:
            return list(fn(list(paths)))
        except Exception:                            # noqa: BLE001
            return []

    @staticmethod
    def human_size(n):
        """字节 -> 人类可读。优先用 chip_formats 的实现以保证全程序一致。"""
        value = max(0, int(n or 0))
        if chip_formats is not None:
            fn = getattr(chip_formats, "human_size", None)
            if fn is not None:
                try:
                    return fn(value)
                except Exception:                    # noqa: BLE001
                    pass
        val = float(value)
        for unit in ("B", "KB", "MB"):
            if val < 1024.0:
                return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
            val /= 1024.0
        return f"{val:.1f} GB"

    @staticmethod
    def reason(path, info):
        """不支持的原因文案(FR-4.2: 可执行 —— 说清"是什么格式/为什么/怎么办")。

        优先用 chip_formats.unsupported_reason; 但它对「已知却不支持」(USF 等)
        与「扩展名不认识」都可能只回一句"无法识别的格式" —— 那不满足 FR-4.2,
        用户看完还是不知道该干什么。所以这里对**信息量不足**的文案做兜底:
        自己按三类(已知不支持 / 扩展名不认识 / 内容不是芯片音乐)拼一条完整的。
        """
        name = os.path.basename(str(path or ""))
        if chip_formats is None:
            return (f"⚠ {name}：多格式支持模块未就绪"
                    f"(缺少 chip_formats.py)，请用 SPC / MSU-1 文件夹 / "
                    f"光盘镜像 / MP3")
        if info is None:
            # 调用方拿不到 ChipInfo(例如扩展名根本不在注册表里)时自己探一次,
            # 否则 chip_formats 会因为 info 为空而回"文件为空或无法读取",
            # 那不是用户能执行的话。
            info = Chip.probe(path)
        text = ""
        fn = getattr(chip_formats, "unsupported_reason", None)
        if fn is not None:
            try:
                text = str(fn(path, info) or "")
            except Exception:                        # noqa: BLE001
                text = ""
        # 信息量判定: 必须出现文件名、扩展名或格式名三者之一才算"可执行"
        ext = (getattr(info, "ext_declared", "") or
               os.path.splitext(name)[1].lower())
        fmt = getattr(info, "fmt", None)
        informative = bool(name and name in text)
        if not informative and ext:
            informative = ext in text
        if not informative and fmt:
            informative = str(fmt) in text
        if text and informative:
            return text if text.startswith("⚠") else f"⚠ {text}"

        # ---- 兜底: 按 FR-4.2 三类自己拼 ----
        systems = getattr(chip_formats, "SYSTEM_NAMES", {}) or {}
        if fmt and not Chip.supported(fmt):
            # 第一类: 知道是什么格式, 但本工具没内置该机种
            system = systems.get(str(fmt), str(fmt))
            return (f"⚠ {name}：暂不支持 {fmt}（{system}）格式 —— "
                    f"本工具尚未内置该机种模拟器。"
                    f"可先用对应模拟器导出 WAV，再拖进本窗口")
        p2 = getattr(chip_formats, "P2_EXTS", {}) or {}
        if ext and ext in p2:
            # 第一类的另一个入口: 扩展名已知但不在格式表里
            system = systems.get(str(p2[ext]), str(p2[ext]))
            return (f"⚠ {name}：暂不支持 {ext}（{system}）格式 —— "
                    f"本工具尚未内置该机种模拟器。"
                    f"可先用对应模拟器导出 WAV，再拖进本窗口")
        if ext:
            # 第二类: 扩展名不认识
            return (f"⚠ {name}：不认识的扩展名 {ext} —— "
                    f"本工具支持 SPC / NSF / NSFE / GBS / VGM / VGZ / HES / "
                    f"光盘镜像(CUE+BIN) / MP3")
        # 第三类: 没有任何可依据的标识
        return (f"⚠ {name}：无法识别这是什么格式 —— "
                f"本工具支持 SPC / NSF / NSFE / GBS / VGM / VGZ / HES / "
                f"光盘镜像(CUE+BIN) / MP3")


    @staticmethod
    def conflict_note(path, info):
        """扩展名与魔数不符时的提示; 不冲突返回 ""。"""
        if not getattr(info, "conflict", False):
            return ""
        ext = getattr(info, "ext_declared", "") or os.path.splitext(
            str(path))[1].lower()
        fmt = getattr(info, "fmt", None) or "未知"
        return (f"⚠ {os.path.basename(str(path))}：扩展名与实际格式不符"
                f"（{ext} → {fmt}），已按实际格式处理")

    @staticmethod
    def plan(info, default_secs, user_max_secs=0.0, hard_cap_secs=600.0):
        """时长决策链(设计 §3.1)。返回 ``(秒, 是否精确)``。

        优先调 ``chip_formats.plan_duration``(与 worker 侧同一函数, 保证
        列表显示与实际解码一致); 签名不符时按 ChipInfo 的字段自己算同一套
        优先级, 结果等价。
        """
        default_secs = max(1.0, float(default_secs or CHIP_DEFAULT_SECS))
        cap = float(hard_cap_secs or 600.0)
        user = max(0.0, float(user_max_secs or 0.0))
        if chip_formats is not None:
            fn = getattr(chip_formats, "plan_duration", None)
            if fn is not None:
                try:
                    out = fn(info, user_max_secs=user,
                             default_secs=default_secs,
                             hard_cap_secs=cap)
                    secs = float(getattr(out, "secs", 0.0) or 0.0)
                    src = str(getattr(out, "source", "") or "")
                    if secs > 0:
                        return Chip._clamp(secs, cap), src in _EXACT_SRC
                except Exception:                    # noqa: BLE001
                    pass                             # 退化成下面这段本地计算
        return Chip._plan_local(info, default_secs, user, cap)

    @staticmethod
    def _plan_local(info, default_secs, user, cap):
        """不依赖 chip_formats.plan_duration 的等价实现(优先级见设计 §3.1)。"""
        if user > 0:
            return Chip._clamp(user, cap), False
        exact = float(getattr(info, "exact_secs", 0.0) or 0.0)
        if exact > 0.2:
            return Chip._clamp(exact, cap), True
        loop = float(getattr(info, "loop_end_secs", 0.0) or 0.0)
        if 0.2 < loop <= 3600:
            return loop, False
        return Chip._clamp(default_secs, cap), False

    @staticmethod
    def _clamp(secs, cap):
        return max(1.0, min(float(cap or 600.0), float(secs or 0.0)))

    @staticmethod
    def decode_ready():
        """芯片解码模块是否就绪(界面据此禁用/提示, 不在导入期崩)。"""
        return chip_decode is not None




def base_dir():
    """脚本/冻结 exe 的基础目录, 用于确定默认输出位置."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def sanitize_name(name):
    """文件夹名 -> 安全的文件名 (去除 Windows 非法字符)"""
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "music"


def enable_file_drop(root, pending):
    """给 Tk 窗口挂上 Windows 原生拖放 (WM_DROPFILES), 不依赖第三方库。

    pending 是一个 queue.Queue: 窗口过程把拖入的路径列表放进去, 由 Tk 侧
    定时取出再处理。返回 True 表示挂载成功。

    **为什么必须走队列, 不能在窗口过程里直接调 Tk**:
    系统拖放时窗口过程运行在 Shell 的模态拖放循环内 (DoDragDrop), 此时
    任何 Tcl/Tk 调用都会重入解释器的 notifier。Tk 不是可重入的, 重入会
    直接把进程打死 (表现为拖完就闪退, 且没有 Python 异常可抓)。所以窗口
    过程里只做两件纯 C 的事: 取路径、入队, 一个 Tcl 函数都不碰。

    另外整个窗口过程套 try/except: 未捕获的 Python 异常穿过 C 栈会直接
    终止进程, 比抛到 Tk 更糟。

    实现: DragAcceptFiles 给顶层窗口加上 WS_EX_ACCEPTFILES, 再子类化窗口
    过程拦截 WM_DROPFILES。回调对象必须长期存活, 否则被 GC 后系统再次回调
    会崩溃, 因此挂在 root 上。鼠标落在子控件(列表/标签)上时, 系统会沿父链
    向上找到已注册的顶层窗口, 所以注册顶层即覆盖整个窗口。
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:                                # noqa: BLE001
        return False

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)
    GWLP_WNDPROC = -4
    GA_ROOT = 2
    WM_DROPFILES = 0x0233

    # winfo_id() 是客户区子窗口, 真正接收 drop 的是它的顶层窗口
    hwnd = 0
    try:
        child = root.winfo_id()
        try:
            hwnd = user32.GetAncestor(child, GA_ROOT) or 0
        except Exception:                            # noqa: BLE001
            hwnd = 0
        if not hwnd:
            hwnd = user32.GetParent(child) or child
    except Exception:                                # noqa: BLE001
        return False
    if not hwnd:
        return False

    # 64 位用 Ptr 版, 32 位回退到普通版
    try:
        set_long = user32.SetWindowLongPtrW
        get_long = user32.GetWindowLongPtrW
    except AttributeError:
        set_long = user32.SetWindowLongW
        get_long = user32.GetWindowLongW
    get_long.restype = ctypes.c_void_p
    get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    set_long.restype = ctypes.c_void_p
    set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]

    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                       wintypes.LPWSTR, wintypes.UINT]
    shell32.DragFinish.restype = None
    shell32.DragFinish.argtypes = [wintypes.HANDLE]
    user32.CallWindowProcW.restype = LRESULT
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                       wintypes.UINT, wintypes.WPARAM,
                                       wintypes.LPARAM]

    def _drop_paths(hdrop):
        h = wintypes.HANDLE(hdrop)
        n = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
        out = []
        for i in range(n):
            ln = shell32.DragQueryFileW(h, i, None, 0)
            if not ln:
                continue
            buf = ctypes.create_unicode_buffer(ln + 1)
            shell32.DragQueryFileW(h, i, buf, ln + 1)
            if buf.value:
                out.append(buf.value)
        return out

    def _proc(h, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            # ---- 这里绝对不许调用任何 Tcl / tkinter 函数 ----
            try:
                try:
                    paths = _drop_paths(wparam)
                finally:
                    shell32.DragFinish(wintypes.HANDLE(wparam))  # 必须释放
                if paths:
                    pending.put_nowait(paths)
            except Exception:                        # noqa: BLE001
                pass
            return 0
        try:
            return user32.CallWindowProcW(old_proc, h, msg, wparam, lparam)
        except Exception:                            # noqa: BLE001
            return 0

    try:
        shell32.DragAcceptFiles(wintypes.HWND(hwnd), True)
        old_proc = get_long(wintypes.HWND(hwnd), GWLP_WNDPROC)
        proc = WNDPROC(_proc)
        set_long(wintypes.HWND(hwnd), GWLP_WNDPROC,
                 ctypes.cast(proc, ctypes.c_void_p))
    except Exception:                                # noqa: BLE001
        return False

    root._drop_proc = proc        # 防止回调被回收
    root._drop_old = old_proc
    return True


_DBG = None


def dbg(msg):
    """写一行诊断到 crash.log (没开日志就什么也不做)。"""
    if _DBG is None:
        return
    try:
        _DBG.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:                                # noqa: BLE001
        pass


_FONT_CACHE = {}


def retro_font(spec):
    """把 theme.FONT* 这种 tuple 转成命名 tkfont.Font, 供 tk.* 原生控件/Treeview tag 使用。

    注意: **传 tuple 本身是合法的** —— tkinter 会把它拼成合法的 Tcl 列表
    (`-font {Microsoft YaHei UI} 9`), 族名天然算一个元素, Tk 解析没问题。
    (曾经这里以为是 tuple 的锅, 实际是 retro_theme 早期版本往 option database 里
    写了不带花括号的裸串 `Microsoft YaHei UI 9`, 才炸出
    `expected integer but got "YaHei"`; 该 bug 已由 theme-engineer 修复。)

    用命名/缓存字体对象的真实好处是工程性的: 同一组字号只创建一次, 且日后想整体
    替换字族只要改 theme.FONT*, 不用回头翻每个 tk 控件的调用点。
    """
    key = tuple(spec)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    family = key[0] if key else "TkDefaultFont"
    size = key[1] if len(key) > 1 else 9
    style = key[2:]
    font = tkfont.Font(family=family, size=size,
                       weight="bold" if "bold" in style else "normal",
                       slant="italic" if "italic" in style else "roman")
    _FONT_CACHE[key] = font
    return font


def apply_dark_title_bar(win):
    """把 Windows 原生标题栏切成深色(DWM immersive dark mode)。

    主题模块没提供 `dark_title_bar` 时静默跳过(向后兼容老版本 retro_theme);
    调用失败也不抛异常, 只返回 None —— 标题栏是纯观感, 不值得让它拖垮界面。
    """
    if not hasattr(theme, "dark_title_bar"):
        return None
    try:
        return theme.dark_title_bar(win)
    except Exception:                                # noqa: BLE001
        return None


def skin_log(widget):
    """给 scrolledtext.ScrolledText 套上复古配色。

    ttk 主题管不到 Text(它是 tk 原生控件), 更管不到 ScrolledText 自带的那根
    tk.Scrollbar —— Windows 会按系统主题画它, `bg`/`troughcolor` 全部被忽略,
    永远是一条 #f0f0f0 的系统灰条(实测取到的像素), 跟日志区的米白对不上。
    所以这里把它整个换成 ttk.Scrollbar(走 Vertical.TScrollbar 样式), 再挂回
    同一个 yview。
    """
    p = theme.PALETTE
    bg, fg = log_colors()
    try:
        widget.configure(bg=bg, fg=fg,
                         insertbackground=p["cyan"],
                         selectbackground=p["sel"],
                         # 选区用墨字：浅色沙底上红字会被读成「错误」
                         selectforeground=p["text"],
                         highlightthickness=0, borderwidth=0,
                         relief="flat")
    except tk.TclError:
        return
    # 首屏欢迎块用的两个 tag: 标题行走等宽霓虹青, 说明行走弱化灰紫。
    try:
        widget.tag_configure("banner", foreground=p["cyan"],
                             font=retro_font(("Consolas", 10, "bold")))
        widget.tag_configure("hint", foreground=p["muted"],
                             font=retro_font(theme.FONT_S))
    except tk.TclError:
        pass
    vbar = getattr(widget, "vbar", None)
    if vbar is None:
        return
    try:
        host = widget.frame
    except AttributeError:
        return
    try:
        vbar.destroy()
        newbar = ttk.Scrollbar(host, orient="vertical", command=widget.yview)
        newbar.pack(side="right", fill="y")
        widget.configure(yscrollcommand=newbar.set)
        widget.vbar = newbar
    except tk.TclError:
        pass


#: 本工具自己能解的扩展名（libgme 芯片格式 + miniaudio 能读的音频容器 + CD 镜像）
SELF_PLAYABLE_EXTS = frozenset(
    {".spc", ".nsf", ".nsfe", ".gbs", ".hes", ".kss", ".sap", ".gym", ".ay",
     ".vgm", ".vgz", ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".cue",
     ".bin", ".iso", ".img", ".raw"})


def find_foobar2000():
    """找可用的 foobar2000.exe。

    ⚠ 这个**不是**"内置"：我们只是调用用户自己装的那一份。
    foobar2000 的许可禁止第三方再分发其二进制，但**自用完全没问题** ——
    所以正确的做法是"检测 + 调用"，而不是把它打包进我们的 exe。
    """
    cands = []
    for drive in ("C:", "D:", "E:", "F:"):
        root = drive + os.sep
        for sub in ("foobar2000", os.path.join("Program Files", "foobar2000"),
                    os.path.join("Program Files (x86)", "foobar2000"),
                    os.path.join("tools", "foobar2000"),
                    os.path.join("Program Files", "foobar2000 v2")):
            cands.append(os.path.join(root, sub, "foobar2000.exe"))
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        cands.append(os.path.join(appdata, "foobar2000", "foobar2000.exe"))
    for c in cands:
        try:
            if c and os.path.isfile(c):
                return c
        except OSError:
            continue
    return ""


def _count_audio_in(folder):
    """数一下文件夹里有多少个「像音频」的文件（只用于日志提示，不影响播放）。"""
    exts = (".miniusf", ".minigsf", ".minissf", ".minidsf", ".mini2sf",
            ".minipsf", ".usf", ".gsf", ".ssf", ".dsf", ".2sf", ".wsr", ".aif",
            ".psf", ".psf2", ".spc", ".nsf", ".nsfe", ".gbs", ".hes", ".kss",
            ".sap", ".gym", ".ay", ".vgm", ".vgz", ".wav", ".mp3", ".flac")
    n = 0
    try:
        for fn in os.listdir(folder):
            if os.path.splitext(fn)[1].lower() in exts:
                n += 1
    except OSError:
        return 0
    return n


def open_with_foobar(target, log):
    """把**文件或整个文件夹**交给 foobar2000。返回 True 表示已交出去。

    ``target`` 是文件夹时，foobar2000 会递归扫描、把它认识的音频全部加进
    播放列表 —— 这正是「一个包里几十首」需要的。**传单个文件只会播一首**，
    而且万一那首的格式它不认，就整个包都听不了（用户实测报障的就是这个）。

    ⚠ 为什么不做"转成 WAV 再回内置播放器"：实测 foobar2000 的
    `/convert` 命令行有 `/output:` 参数不认（报 Unknown commandline parameter）、
    且转换任务不真正执行（rc=0 但无产物）—— 它的转换依赖 GUI 交互，
    没法在后台静默完成。所以这里只做最可靠的"打开即播放"。
    """
    if not os.path.exists(target):
        log("❌ 路径不存在：%s" % target)
        return False
    exe = find_foobar2000()
    if not exe:
        log("❌ 没找到 foobar2000 —— 装好后即可用它播放这个格式"
            "（本工具不会内置它：那是再分发，违反其许可）")
        return False
    is_dir = os.path.isdir(target)
    try:
        subprocess.Popen([exe, target])
    except OSError as e:
        log("❌ 启动 foobar2000 失败：%s" % e)
        return False
    if is_dir:
        n = _count_audio_in(target)
        log("▶ 已把整个文件夹交给 foobar2000（%d 首）：%s"
            % (n, os.path.basename(target)))
    else:
        log("▶ 已交给 foobar2000 播放：%s" % os.path.basename(target))
    return True


def _psf_pack_dir(path):
    """把「文件夹 / 单个 PSF 文件」统一成包目录；不是 PSF 包返回 ``""``。"""
    if psf_bridge is None:
        return ""
    p = str(path or "")
    if os.path.isdir(p):
        return p if psf_bridge.count_tracks(p) > 0 else ""
    ext = os.path.splitext(p)[1].lower()
    if ext in psf_bridge.PSF_TRACK_EXTS:
        d = os.path.dirname(p)
        return d or ""
    return ""


def psf_convert_flow(target, log, parent=None):
    """PSF → WAV 的半自动流程。返回 True 表示已把活儿交出去。

    为什么不是"全自动"：PSF 要出声必须模拟 PS1 的 CPU + SPU，而
    foobar2000 v2.26 的转换器**没有任何命令行入口**（六种写法实测全无效，
    详见 psf_bridge 模块注释）。硬做窗口消息/按键模拟太脆，所以这里把
    「准备」做到位（预设、整包载入、输出目录），把最后三下点击留给用户。

    输出目录固定放在包内的 ``WAV`` 子目录 —— 与包放在一起，
    用户转换完直接在原地就能找到，不用再去 foobar2000 里翻。
    """
    if psf_bridge is None:
        log("❌ PSF 桥接模块未就绪（缺少 psf_bridge.py）")
        return False
    folder = _psf_pack_dir(target)
    if not folder:
        log("❌ 这个路径里没有 PSF 曲目：%s" % os.path.basename(str(target)))
        return False

    n = psf_bridge.count_tracks(folder)
    # normpath：folder 来自 Tk/拖放，可能是 "D:/a/b" 形式，
    # 直接拼会得到 "D:/a/b\WAV" 这种混着两种分隔符的路径。
    out_dir = os.path.normpath(os.path.join(folder, "WAV"))
    exe = psf_bridge.find_foobar()

    if not exe:
        log("❌ 没找到 foobar2000 —— PSF 需要它（+ foo_psf 组件）才能转 WAV")
        try:
            messagebox.showwarning(
                "需要 foobar2000",
                psf_bridge.guidance("", folder, out_dir, n_tracks=n),
                parent=parent)
        except tk.TclError:
            pass
        return False

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        log("❌ 无法创建输出目录 %s：%s" % (out_dir, e))
        return False

    pre = psf_bridge.ensure_wav_preset(exe, out_dir)
    if not pre["ok"]:
        # 预设写不进去不算致命：用户仍可在 Convert 对话框里手动选 WAV，
        # 只是少了「一步到位」。所以照常把包交出去，但把原因说清楚。
        log("⚠ 没能写入转换预设：%s" % pre["error"])
        log("   不影响转换，只是要在 Convert 对话框里手动选 WAV 和输出目录")
    elif pre["created"]:
        log("✔ 已写入 WAV 转换预设「%s」→ %s"
            % (pre["name"], os.path.basename(pre["path"])))
    else:
        log("ℹ 复用已有转换预设「%s」（没覆盖你原来的设置）" % pre["name"])

    ok, err = psf_bridge.open_for_convert(exe, folder)
    if not ok:
        log("❌ %s" % err)
        return False

    log("▶ 已把 PSF 整包（%d 首）交给 foobar2000 转换，输出目录：%s"
        % (n, out_dir))
    try:
        messagebox.showinfo(
            "PSF → WAV（半自动）",
            psf_bridge.guidance(exe, folder, out_dir,
                                preset_name=pre["name"], n_tracks=n),
            parent=parent)
    except tk.TclError:
        pass
    return True



def popup_open_menu(widget, ev, path, log, folder=""):
    """列表右键菜单：「用外部播放器打开」/「打开所在文件夹」。

    ⚠⚠ 本工具**不内置**任何第三方播放器。foobar2000 的许可原文：
        "Only unmodified installers can be redistributed; redistribution of
         foobar2000 binaries in any other form is not permitted."
    也就是说把便携版文件放进我们的目录一起发是**明确禁止**的。
    所以对 `.miniusf/.minigsf/.ssf` 这类我们解不了的格式，只有两条合法路径：
    ① 用户自己装播放器并建立文件关联，我们只把文件交出去（这里做的）；
    ② 自己移植对应机种的模拟器（工程量极大，未做）。
    """
    m = tk.Menu(widget, tearoff=0)
    # v4.40: PSF 包（PS1/PS2 芯片音源）走专门的「转 WAV」入口。
    # 放在最前面 —— 用户对 PSF 的诉求是"要 WAV"，不是"想听一遍"。
    pack = _psf_pack_dir(folder or path)
    if pack:
        n = psf_bridge.count_tracks(pack)
        m.add_command(
            label="🎵 用 foobar2000 转 WAV（%d 首，半自动）" % n,
            command=lambda: psf_convert_flow(pack, log))
        m.add_separator()
    if folder and os.path.isdir(folder):
        # 「一个包几十首」的场景：整包交给 foobar2000 才听得全
        n = _count_audio_in(folder)
        m.add_command(
            label="▶ 用 foobar2000 播放整个文件夹（%d 首）" % n,
            command=lambda: open_with_foobar(folder, log))
        m.add_separator()
    m.add_command(label="▶ 用外部播放器打开这个文件",
                  command=lambda: _open_external(path, log))
    m.add_command(label="📂 打开所在文件夹",
                  command=lambda: _open_folder_of(path, log))
    try:
        m.tk_popup(ev.x_root, ev.y_root)
    except tk.TclError:
        pass
    finally:
        try:
            m.grab_release()
        except tk.TclError:
            pass


def _open_external(path, log):
    """交给 Windows 按文件关联启动。没有关联时会弹系统的「选择应用」对话框。"""
    try:
        os.startfile(path)              # noqa: S606  Windows only
    except OSError as e:
        log("❌ 无法用外部程序打开：%s" % e)
    except Exception as e:                                # noqa: BLE001
        log("❌ 打开失败：%s" % e)


def _open_folder_of(path, log):
    try:
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        os.startfile(folder)            # noqa: S606
    except Exception as e:                                # noqa: BLE001
        log("❌ 打开文件夹失败：%s" % e)


def hook_crash_log():
    """把未捕获异常与段错误写进 exe 同目录的 crash.log。

    拖放走的是 Windows 原生窗口过程, 一旦里面出问题往往直接消失、什么都
    看不到。有了这个文件, 下次再出问题至少能定位到栈。
    """
    try:
        path = os.path.join(base_dir(), "crash.log")
        log = open(path, "a", encoding="utf-8", buffering=1)
    except Exception:                                # noqa: BLE001
        return None
    try:
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                  f"启动 v{APP_VER} =====\n")
    except Exception:                                # noqa: BLE001
        pass
    try:
        import faulthandler
        faulthandler.enable(file=log, all_threads=True)
    except Exception:                                # noqa: BLE001
        pass

    def _hook(tp, val, tb):
        try:
            traceback.print_exception(tp, val, tb, file=log)
        except Exception:                            # noqa: BLE001
            pass
        try:
            sys.__excepthook__(tp, val, tb)
        except Exception:                            # noqa: BLE001
            pass

    global _DBG
    _DBG = log
    sys.excepthook = _hook
    try:
        threading.excepthook = lambda a: _hook(a.exc_type, a.exc_value,
                                               a.exc_traceback)
    except Exception:                                # noqa: BLE001
        pass
    return log


#: CD-DA 音轨扩展名（v4.30）。与 ``cd_audio.CD_EXTS`` 同源，这里留一份常量
#: 是为了**不把 cd_audio（连带 PyAV）拉进主窗口的 import 链**。
_CD_AUDIO_EXTS = (".ape", ".flac", ".wav", ".ogg", ".m4a", ".wv", ".tta")

#: 列表「格式」列里 CD 音轨的显示名（v4.31）。``_fill_meta`` 靠它反推 kind，
#: 所以别再散落字面量。
CD_FMT = "CD-DA"

SUPPORTED_EXTS = ((".spc", ".cue", ".mp3") + core.DISC_IMG_EXTS + _CD_AUDIO_EXTS
                  + tuple(Chip.exts()))

# PickDialog 的「芯片音乐」筛选项。chip_formats 缺失时这一项自动不出现在
# 下拉里(而不是列出后什么都选不到)。
_CHIP_FILTER_EXTS = (".nsf", ".nsfe", ".gbs", ".vgm", ".vgz", ".hes")



def _session_lock_path():
    return os.path.join(base_dir(), ".running")


def _pid_alive(pid):
    """另一个同名进程是否还活着 (用于区分"上次异常退出"和"另一个实例在跑")。"""
    if pid <= 0:
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)     # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong(0)
        alive = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(alive) and code.value == 259     # STILL_ACTIVE
    except Exception:                                # noqa: BLE001
        return False


#: ⚠ 历史包袱（v4.31 起停用）：v4.30 及以前是「上次异常退出 -> 本次不挂拖放」。
#:
#: 为什么撤掉：那个自动降级造成的故障，比它防的风险**更常见** ——
#: 进程被任务管理器结束、断电、与本功能无关的 Tk 关窗崩溃，都会让下次启动
#: 静默丢掉拖放；提示只写在日志里（用户不看），而且**不会自愈**（下次启动
#: 读到同一个死 pid，又判定崩溃）。用户看到的就是「拖拽突然坏了」。
#:
#: v3 那次拖放崩溃的真正根因（DoDragDrop 模态循环内重入 Tcl）早在
#: `enable_file_drop` 里解决了：窗口过程**只入队、一个 Tcl 函数都不碰**。
#: 所以现在只提示、不降级；真要排查就加 `--no-drop`。
_CRASH_STREAK_TTL = 24 * 3600


def take_session_lock():
    """记录本次运行。返回 ``(上次是否异常退出, 连续异常退出次数)``。

    锁文件格式 ``"<pid> <streak>"``；兼容旧版纯数字 ``"<pid>"``（视为 1 次）。
    正常关闭时 :func:`release_session_lock` 删掉文件，计数归零。

    用 --drop 可强制开启拖放, --no-drop 强制关闭。
    """
    path = _session_lock_path()
    prev_crash = False
    streak = 0
    try:
        if os.path.exists(path):
            old, old_streak, old_ts = 0, 1, 0.0
            try:
                with open(path, encoding="utf-8") as f:
                    parts = (f.read().strip() or "0").replace(",", " ").split()
                if parts:
                    old = int(parts[0])
                if len(parts) > 1:
                    old_streak = max(1, int(parts[1]))
                if len(parts) > 2:
                    old_ts = float(parts[2])
            except (OSError, ValueError):
                old, old_streak, old_ts = 0, 1, 0.0
            prev_crash = not _pid_alive(old)
            # 崩溃计数只对「近期连续崩溃」有意义：超过一天就当作陈迹清零。
            # 否则一次旧崩溃（外加用户习惯用任务管理器结束进程、根本没机会
            # 触发 WM_DELETE_WINDOW）会把拖放永久钉死 —— 那正是 v4.30 的坑。
            if old_ts > 0 and (time.time() - old_ts) > _CRASH_STREAK_TTL:
                old_streak = 0
            streak = min(99, old_streak + 1) if prev_crash else 0
        with open(path, "w", encoding="utf-8") as f:
            f.write("%d %d %d" % (os.getpid(), streak, int(time.time())))
        return prev_crash, streak
    except OSError:
        return False, 0


def release_session_lock():
    try:
        os.remove(_session_lock_path())
    except OSError:
        pass


class _DirMeta:
    """目录型芯片音源的**合成**元信息(把第一层所有文件的轨数/时长加起来)。

    它只喂给 ``add_source`` 的 ``meta`` 形参, 字段名与 ChipInfo 对齐,
    这样 ``_fill_meta`` 一份代码就能同时处理"单文件 ChipInfo"与
    "目录合成值", 不需要两条分支。

    额外带一个 ``total_secs``: 目录里每个文件的单轨时长不同, 只靠
    ``n_tracks × est_secs`` 是估不准的, 所以合成时直接累加真实总秒数。
    """

    __slots__ = ("fmt", "n_tracks", "est_secs", "exact", "size", "dur_src",
                 "ok", "total_secs", "cd_tracks", "cd_secs")

    def __init__(self, fmt="", n_tracks=0, est_secs=0.0, exact=False,
                 size=0, dur_src="", ok=True, total_secs=None,
                 cd_tracks=0, cd_secs=0.0):
        self.fmt = fmt
        self.n_tracks = n_tracks
        self.est_secs = est_secs
        self.exact = exact
        self.size = size
        self.dur_src = dur_src
        self.ok = ok
        self.total_secs = est_secs * max(1, n_tracks) \
            if total_secs is None else float(total_secs)
        # v4.31: 目录里混装的 CD 音轨（PCE-CD 的 .ape）。n_tracks / total_secs
        # 是**合计值**；这两个字段单独记"其中的 CD 部分"，因为 CD 音轨
        # 不吃「单曲最长」钳制（见 ConvertWorker._chip_secs_of）。
        self.cd_tracks = max(0, int(cd_tracks or 0))
        self.cd_secs = max(0.0, float(cd_secs or 0.0))


class _CdMeta:
    """CD-DA 音轨的元信息（v4.31），字段与 ChipInfo 对齐，供 ``_fill_meta`` 复用。

    一个目录里的多首 CD 音轨能在列表里合成**一条**：``n_tracks`` 是首数，
    ``total_secs`` 是它们的总时长（不是单曲时长）—— 与 ``_DirMeta`` 同口径。

    ⚠ 时长来自 ``cd_audio.probe()``（FFmpeg 读文件头），拿不到就填 0：
    列表显示「—」，转换时按原长走，绝不拿一个猜的值去截断。
    """

    __slots__ = ("fmt", "n_tracks", "est_secs", "exact", "size", "dur_src",
                 "ok", "total_secs", "conflict")

    def __init__(self, n_tracks=1, total_secs=0.0, size=0, dur_src="ffmpeg"):
        self.fmt = CD_FMT
        self.n_tracks = max(1, int(n_tracks or 1))
        self.total_secs = max(0.0, float(total_secs or 0.0))
        self.est_secs = (self.total_secs / self.n_tracks
                         if self.total_secs > 0 else 0.0)
        self.exact = self.total_secs > 0
        self.size = max(0, int(size or 0))
        self.dur_src = dur_src
        self.ok = True
        self.conflict = False


class PickDialog(tk.Toplevel):
    """统一添加对话框: 一个界面里既能多选文件, 也能多选文件夹。

    Windows 的原生"打开"对话框只允许选文件, "选择文件夹"对话框只允许选
    目录, 二者不能混选——所以这里自己画一个: 上方盘符/上级/路径, 中间列
    出目录内容(文件夹始终显示, 文件按格式过滤), 支持 Ctrl/Shift 多选。
    双击文件夹进入, 双击文件直接确定。点"添加选中项"返回选中路径列表。
    """

    FILTERS = (
        ("支持的格式", "sup"),
        ("全部文件", "all"),
        ("SPC 音乐 (*.spc)", (".spc",)),
        ("芯片音乐 (*.nsf, *.nsfe, *.gbs, *.vgm, *.vgz, *.hes)",
         _CHIP_FILTER_EXTS),
        ("光盘镜像 (*.cue, *.bin, *.iso)", (".cue",) + core.DISC_IMG_EXTS),
        ("CD 音轨 (*.ape, *.flac, *.wav, *.ogg)", _CD_AUDIO_EXTS),
        ("MP3 (*.mp3)", (".mp3",)),
    )

    def __init__(self, master, start_dir=None):
        super().__init__(master)
        self.title("添加音源 — 可多选文件与文件夹")
        self.transient(master)
        self.configure(bg=theme.PALETTE["bg"])
        self.geometry("760x540")
        self.minsize(600, 420)
        self.result = []
        self._rows = {}          # tree iid -> (path, is_dir)
        self._seq = 0

        self.cur = os.path.normpath(start_dir or os.path.expanduser("~"))
        if not os.path.isdir(self.cur):
            self.cur = os.path.expanduser("~")

        self._build()
        self._go(self.cur)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        # 标题栏深色: 等窗口真正 map 出来再设, 否则 DWM 属性会被重置。
        self.after(50, lambda: apply_dark_title_bar(self))

    # ---- 构建 -------------------------------------------------------------
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 4))

        self.var_drive = tk.StringVar()
        drives = self._drives()
        cb = ttk.Combobox(top, textvariable=self.var_drive, width=6,
                          state="readonly", values=drives)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._go(self.var_drive.get()))

        ttk.Button(top, text="上级", width=8,
                   command=self._up).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="桌面", width=8,
                   command=lambda: self._go(self._special("Desktop"))
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="下载", width=8,
                   command=lambda: self._go(self._special("Downloads"))
                   ).pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(self)
        row2.pack(fill="x", padx=10, pady=2)
        ttk.Label(row2, text="路径:", style="Window.TLabel").pack(side="left")
        self.var_path = tk.StringVar(value=self.cur)
        ent = ttk.Entry(row2, textvariable=self.var_path)
        ent.pack(side="left", fill="x", expand=True, padx=6)
        ent.bind("<Return>", lambda _e: self._go(self.var_path.get()))

        row3 = ttk.Frame(self)
        row3.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(row3, text="显示:", style="Window.TLabel").pack(side="left")
        self.var_filter = tk.StringVar(value=self.FILTERS[0][0])
        cbf = ttk.Combobox(row3, textvariable=self.var_filter, width=22,
                           state="readonly",
                           values=tuple(n for n, _v in self.FILTERS))
        cbf.pack(side="left", padx=6)
        cbf.bind("<<ComboboxSelected>>", lambda _e: self._fill())
        ttk.Label(row3, text="文件夹始终显示；可 Ctrl / Shift 多选",
                  style="Window.Muted.TLabel", font=theme.FONT_S
                  ).pack(side="left", padx=6)

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=10)
        self.tree = ttk.Treeview(mid, columns=("name", "type"),
                                 show="headings", selectmode="extended")
        self.tree.heading("name", text="名称")
        self.tree.heading("type", text="类型")
        self.tree.column("name", width=520, anchor="w")
        self.tree.column("type", width=110, anchor="w")
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._upd_status())

        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=10, pady=8)
        self.var_status = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self.var_status,
                  style="Window.Accent.TLabel").pack(side="left")
        ttk.Button(bot, text="取消", width=10, style="Small.TButton",
                   command=self.destroy).pack(side="right")
        ttk.Button(bot, text="✔ 添加选中项", width=14, style="Accent.TButton",
                   command=self._ok).pack(side="right", padx=6)

    # ---- 工具 -------------------------------------------------------------
    @staticmethod
    def _drives():
        return [f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                if os.path.exists(f"{c}:\\")]

    @staticmethod
    def _special(name):
        """桌面/下载等已知文件夹; 不存在则退回用户主目录。"""
        home = os.path.expanduser("~")
        p = os.path.join(home, name)
        return p if os.path.isdir(p) else home

    def _mode(self):
        for label, mode in self.FILTERS:
            if label == self.var_filter.get():
                return mode
        return "sup"

    def _accept(self, name, is_dir):
        if is_dir:
            return True
        low = name.lower()
        mode = self._mode()
        if mode == "all":
            return True
        if mode == "sup":
            return low.endswith(SUPPORTED_EXTS)
        return low.endswith(mode)

    def _icon(self, name, is_dir, path=None):
        if is_dir:
            return "📁", "文件夹"
        low = name.lower()
        if low.endswith(".spc"):
            return "🎮", "SPC 音乐"
        if low.endswith(_CHIP_FILTER_EXTS):
            # 按内容(魔数)判格式, 显示的是真实格式而不是扩展名 —— .vgm 里
            # 装 gzip(VGZ)、.spc 改名成 .nsf 都要按实际内容显示。
            info = Chip.probe(path or name)
            fmt = getattr(info, "fmt", None) if info is not None else None
            label = str(fmt) if fmt else "芯片音乐"
            return "🎮", f"{label} 芯片音乐"
        if low.endswith(".pcm"):
            return "🎵", "MSU-1 音轨"
        if low.endswith((".cue",) + core.DISC_IMG_EXTS):
            return "💿", "光盘镜像"
        if low.endswith(".mp3"):
            return "🎵", "MP3"
        return "·", os.path.splitext(name)[1].lstrip(".").upper() or "文件"

    # ---- 导航 -------------------------------------------------------------
    def _go(self, path):
        path = os.path.normpath(str(path).strip().strip('"'))
        if os.path.isfile(path):
            path = os.path.dirname(path)
        if not os.path.isdir(path):
            messagebox.showinfo("提示", f"目录不存在:\n{path}", parent=self)
            self.var_path.set(self.cur)
            return
        self.cur = path
        self.var_path.set(path)
        drive = os.path.splitdrive(path)[0] + "\\"
        if drive in self._drives():
            self.var_drive.set(drive)
        self._fill()

    def _up(self):
        parent = os.path.dirname(self.cur.rstrip("\\/"))
        self._go(parent if parent else self.cur)

    def _fill(self):
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()
        self._seq = 0
        try:
            entries = list(os.scandir(self.cur))
        except OSError as e:
            self._upd_status(f"无法读取目录: {e}")
            return
        dirs, files = [], []
        for e in entries:
            try:
                is_dir = e.is_dir()
            except OSError:
                continue
            if not self._accept(e.name, is_dir):
                continue
            (dirs if is_dir else files).append((e.name, is_dir))

        key = lambda it: spc_wav._natural_key(it[0])   # noqa: SLF001
        for name, is_dir in sorted(dirs, key=key) + sorted(files, key=key):
            path = os.path.join(self.cur, name)
            icon, kind = self._icon(name, is_dir, path)
            self._seq += 1
            iid = str(self._seq)
            self._rows[iid] = (path, is_dir)
            self.tree.insert("", "end", iid=iid,
                             values=(f"{icon} {name}", kind))
        self._upd_status()

    def _on_double(self, _e):
        sel = self.tree.selection()
        if not sel:
            return
        path, is_dir = self._rows[sel[0]]
        if is_dir:
            self._go(path)
        else:
            self.result = [path]
            self.destroy()

    def _upd_status(self, extra=""):
        sel = self.tree.selection()
        n_dir = sum(1 for i in sel if self._rows[i][1])
        n_file = len(sel) - n_dir
        parts = []
        if n_file:
            parts.append(f"文件 {n_file}")
        if n_dir:
            parts.append(f"文件夹 {n_dir}")
        txt = ("已选 " + " + ".join(parts)) if parts else "尚未选择"
        if extra:
            txt = extra
        self.var_status.set(txt)

    def _ok(self):
        out = [self._rows[i][0] for i in self.tree.selection()]
        if not out:
            messagebox.showinfo("提示", "请先选中文件或文件夹（可多选）",
                                parent=self)
            return
        self.result = out
        self.destroy()

    # ---- 调用 -------------------------------------------------------------
    @classmethod
    def ask(cls, master, start_dir=None):
        dlg = cls(master, start_dir)
        dlg.grab_set()
        master.wait_window(dlg)
        return dlg.result


def _cd_files(path):
    """path（文件或目录，只看一层）里的 CD 音轨文件列表。

    只按扩展名筛（``cd_audio.is_cd_audio`` **不打开文件**），所以拖入一个
    几百 MB 的包也不会有 IO 停顿；真正的时长探测在 :func:`_cd_meta` 里按需做。

    模块级而不是 App 的方法：``ConvertWorker``（真正干转换的那个类）也要用，
    而它不继承 App。
    """
    if cd_audio is None:
        return []
    try:
        if os.path.isdir(path):
            out = []
            for n in sorted(os.listdir(path)):
                q = os.path.join(path, n)
                if os.path.isfile(q) and cd_audio.is_cd_audio(q):
                    out.append(q)
            return out
        if os.path.isfile(path) and cd_audio.is_cd_audio(path):
            return [path]
    except OSError:
        return []
    return []


def _cd_meta(path):
    """CD 音轨的列表元信息：首数 + 总时长 + 体积。"""
    files = _cd_files(path)
    if not files:
        return None
    secs, size = 0.0, 0
    for f in files:
        try:
            size += os.path.getsize(f)
        except OSError:
            pass
        if cd_audio is not None:
            try:
                info = cd_audio.probe(f)
            except Exception:                        # noqa: BLE001
                info = {}
            if info.get("ok"):
                secs += float(info.get("secs") or 0.0)
    return _CdMeta(len(files), secs, size)


def _disc_meta(path):
    """光盘镜像（CUE+BIN）的列表元信息（v4.35）：首数 + 总时长 + 体积。

    只解析 CUE 文本 + stat BIN，**不读音频数据** —— 镜像动辄几百 MB，
    为了显示一个时长去读一遍 PCM 是不能接受的（实测 340MB 的 PS1 盘
    只 stat 就够，0.01 秒级）。

    ⚠ ``fmt`` 必须留空：``_fill_meta`` 见到 ``CD_FMT`` 会把 kind 改成 ``CD``，
    镜像就会走错分支（当单轨 CD 音轨处理，不再抽轨）。
    """
    try:
        info = core.probe_disc_audio(path)
    except Exception:                                # noqa: BLE001
        return None
    if not info.get("n_tracks"):
        return None
    meta = _CdMeta(int(info["n_tracks"]), float(info["total_secs"]),
                   int(info["size"]), dur_src="cue")
    meta.fmt = ""
    return meta


def _with_cd_meta(meta, path):
    """把目录里的 CD 音轨并进芯片音源的合成元信息（v4.31）。

    PCE-CD 包是「.hes + .ape」混装：``_dir_meta`` 只数芯片音源，这里把同目录
    的 CD 音轨也计进首数/时长/体积 —— 否则列表显示「6 首」而实际产出 7 个
    WAV，进度分母也少一个。

    只用于 **CHIP 目录**；纯 CD 目录走 :func:`_cd_meta`，别在这里绕一圈。
    """
    cd = _cd_meta(path)
    if cd is None:
        return meta
    return _DirMeta(
        fmt=str(getattr(meta, "fmt", "") or ""),
        n_tracks=int(getattr(meta, "n_tracks", 0) or 0) + cd.n_tracks,
        est_secs=float(getattr(meta, "est_secs", 0.0) or 0.0),
        exact=bool(getattr(meta, "exact", False)),
        size=int(getattr(meta, "size", 0) or 0) + cd.size,
        dur_src=str(getattr(meta, "dur_src", "") or ""),
        ok=bool(getattr(meta, "ok", True)),
        total_secs=(float(getattr(meta, "total_secs", 0.0) or 0.0)
                    + cd.total_secs),
        cd_tracks=cd.n_tracks,
        cd_secs=cd.total_secs,
    )


def _merge_subs(*subs):
    """把多个 ``convert`` 统计字典拼成一个（字段同形）。

    v4.31 用它把「芯片音源」与「同目录 CD 音轨」的产出并成一次 ``_report``。
    ⚠ 必须并成**一次**：``_report`` 里会调 ``_merge_wavs`` 生成
    ``<源名>（合并）.wav``，分两次汇报会在同一目录生成两个同名文件，
    后者把前者覆盖掉（症状：合并轨少了一半内容）。
    """
    out = {"ok": 0, "fail": 0, "files": [], "seconds": 0.0,
           "errors": [], "last_idx": 0}
    for s in subs:
        if not s:
            continue
        out["ok"] += int(s.get("ok") or 0)
        out["fail"] += int(s.get("fail") or 0)
        out["files"] += list(s.get("files") or ())
        out["seconds"] += float(s.get("seconds") or 0.0)
        out["errors"] += list(s.get("errors") or ())
        out["last_idx"] = max(out["last_idx"], int(s.get("last_idx") or 0))
    return out


def _supported_kwargs(fn, **kwargs):
    """只保留 ``fn`` 真正接受的关键字参数。

    芯片解码管线(T02)与界面(T03)是并行开发的, 调用方不能假设对方已经
    把 ``loader`` / ``chip_secs`` 接上。逐个探测一次签名, 缺哪个就少传哪个,
    调用方据此判断"这条路径能不能走", 而不是等 TypeError 崩在用户面前。
    """
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind == inspect.Parameter.VAR_KEYWORD
           for p in params.values()):
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in params}


class ConvertWorker(threading.Thread):
    """后台转换线程: 逐个处理音源队列, 通过 queue 汇报进度."""

    def __init__(self, sources, opts, msgq, cancel_event):
        super().__init__(daemon=True)
        self.sources = sources
        self.opts = opts
        self.q = msgq
        self.cancel_event = cancel_event
        # 所有音源平铺到同一个输出目录: 跨音源共用的去重集合与连续序号
        self.used_wav = set()
        self.used_cd = set()
        self.wav_idx = 0
        self.total_wav = int(opts.get("total_tracks") or 0)
        # ---- 进度模型 ----
        # est_secs > 0 时按 "已处理音频秒 / 预计总音频秒" 算百分比 (最准, 能出
        # 稳定的剩余时间); 未知时退化为 "音源序号 + 音源内轨数" 两级进度。
        self.n_src = max(1, len(sources))
        self.cur_src = 0            # 当前音源序号 (1 起)
        self.n_in_src = 0           # 当前音源总轨数 (0 = 未知)
        self.done_in_src = 0        # 当前音源本遍已完成轨数
        self.pass_idx = 0           # 当前音源已进入第几遍 (解码/CD/WAV)
        self.src_passes = 1         # 当前音源总共要过几遍
        self.est_secs = float(opts.get("est_secs") or 0.0)
        self.done_secs = 0.0        # 已处理音频秒数 (解码 + 写盘各算一遍)
        self.t0 = time.time()
        self._rate = 0.0            # 音频秒 / 墙钟秒, 指数平滑
        self._text = "准备中…"

    # ---- 汇报 -------------------------------------------------------------
    def log(self, text):
        self.q.put(("log", text))

    def status(self, text):
        self.q.put(("status", text))

    # ---- 进度 -------------------------------------------------------------
    def _pct(self):
        """已完成百分比, 封顶 99.9 (100 只在真正完成时给)。"""
        if self.est_secs > 0:
            return min(99.9, self.done_secs / self.est_secs * 100.0)
        # 兜底模型: 音源之间等分, 音源内部再按"遍数 × 本遍轨数"细分
        p = min(1.0, self.done_in_src / self.n_in_src) if self.n_in_src else 0.0
        frac = min(1.0, (self.pass_idx + p) / max(1, self.src_passes))
        return min(99.9, ((self.cur_src - 1) + frac) / self.n_src * 100.0)

    def _eta(self, pct, elapsed):
        """剩余秒数; 数据不足时返回 None (界面显示"计算中")。"""
        if self.est_secs > 0 and self.done_secs > 0.5:
            inst = self.done_secs / elapsed
            self._rate = inst if self._rate <= 0 else self._rate * 0.75 + inst * 0.25
            if self._rate > 1e-6:
                return max(0.0, (self.est_secs - self.done_secs) / self._rate)
        if pct >= 1.5:
            # 没有预估值时按"已完成比例线性外推", 够用且不会出现负值
            return max(0.0, elapsed * (100.0 - pct) / pct)
        return None

    def prog(self, text=None):
        """把当前进度推给界面。

        不在 worker 侧节流: 消息本来就按"一轨一条"的粒度发, 频率很低; 真正的
        合并放在界面侧(每次轮询只渲染最后一条)。之前试过节流, 结果只有 2~3
        首的短任务最后一条被丢掉, 进度停在半路。
        """
        if text is not None:
            self._text = text
        elapsed = max(1e-6, time.time() - self.t0)
        pct = self._pct()
        self.q.put(("progress", pct, self._text, elapsed,
                    self._eta(pct, elapsed)))

    def _add_secs(self, secs):
        try:
            self.done_secs += max(0.0, float(secs or 0.0))
        except (TypeError, ValueError):
            pass

    def _passes_for(self, kind):
        """这个音源要过几遍: 解码一遍、写 CD 一遍、写 WAV 一遍, 各自计时。"""
        o = self.opts
        if kind == "SPC" and o["do_cd"]:
            return 2 + (1 if o["do_wav"] else 0)     # 解码 + CD (+ WAV)
        if kind == "SPC":
            return 1                                 # 解码与写盘同一批完成
        if kind in ("CHIP", "CD"):
            # 多格式芯片音源只产出 WAV(PRD §1.2 明确"CD 镜像不做多格式扩展"),
            # 所以即使勾了 CD 也只有"解码+写盘"这一遍, 进度不会被算成 2~3 遍。
            # v4.31: CD 音轨同理 —— 它本身就是 CD-DA，再封一次 BIN 没有意义。
            return 1
        return (1 if o["do_cd"] else 0) + (1 if o["do_wav"] else 0)

    def begin_pass(self):
        """进入本音源的下一个处理遍, 本遍轨数从 0 重新计。"""
        self.pass_idx = min(self.pass_idx + 1, max(0, self.src_passes - 1))
        self.done_in_src = 0

    # ---- CD: BIN+CUE ------------------------------------------------------
    def convert_cd(self, tracks, out_dir, name):
        cd_opts = {
            "gap": self.opts["gap"], "album": self.opts["album"] or name,
            "performer": self.opts["performer"], "use_cdtext": True,
            "verify": self.opts["verify"],
            # 试听 WAV 也平铺到同一个输出目录, 不再建 wav_preview 子目录
            "wav_dir": out_dir if self.opts["cd_wav"] else None,
            "cd_wav": False,
        }
        bin_path, cue_path, infos, sector = core.tracks_to_cd(
            tracks, out_dir, name, cd_opts,
            on_progress=self._track_prog, on_stage=self._stage)
        for i, t in enumerate(infos, 1):
            dur = t["nbytes"] / 4 / 44100  # 16bit*2ch
            self.log(f"   CD 轨 {i:02d}/{len(infos):02d}: {t['title']} "
                     f"({dur/60:.1f} 分钟)")
        return len(infos)

    # ---- WAV (Pico-Plus) --------------------------------------------------
    def write_wav(self, tracks, out_dir, max_secs=None):
        """写 WAV: 序号与文件名去重跨音源连续, 全部落在 out_dir 根目录。

        ``max_secs`` 传 ``None`` 表示沿用界面上的「单曲最长」；传 0.0 表示
        **本类音源不截断**（光盘镜像用，见 ``_do_other``）。
        """
        cap = self.opts["max_secs"] if max_secs is None else max_secs
        sub = spc_wav.write_tracks(
            tracks, out_dir, cap,
            on_log=self.log, on_progress=self._track_prog,
            cancel=self.cancel_event, used=self.used_wav,
            start_idx=self.wav_idx, total=self.total_wav or None)
        self.wav_idx = sub["last_idx"]
        return sub

    def _unique_cd_name(self, name):
        """同一输出目录下多个音源重名时加 (2)(3) 后缀, 避免 BIN/CUE 互相覆盖。"""
        base, n = name, 2
        while name.lower() in self.used_cd:
            name = f"{base} ({n})"
            n += 1
        self.used_cd.add(name.lower())
        return name

    def _track_prog(self, i, n, name, secs=0.0):
        """一轨完成: 计数 +1、累计音频秒数, 并把百分比/剩余时间推给界面。"""
        self.done_in_src += 1
        self._add_secs(secs)
        self.prog(f"{name}  ({i}/{n})")

    def _decode_prog(self, done, total, secs=0.0):
        """一批 SPC 解码完成 (还没写盘), 只累计已解码的音频秒数。"""
        self.done_in_src = done
        self.n_in_src = total
        self._add_secs(secs)
        self.prog(f"解码 SPC {done}/{total}")

    def _stage(self, *args):
        """耗时但无细粒度数字的阶段, 只更新状态文字。

        两种来源: spc_wav.convert 的 (批序号, 批总数, 本批曲数);
        tracks_to_cd 的单条文本(如 "回读校验 92 轨…")。
        """
        label = "芯片音源" if self.opts.get("cur_kind") == "CHIP" else "SPC"
        if len(args) >= 3:
            text = f"解码{label} 第 {args[0]}/{args[1]} 批 ({args[2]} 首)…"
        else:
            text = str(args[0]) if args else ""
        self.prog(text)


    # ---- 主流程 -----------------------------------------------------------
    def run(self):
        opts = self.opts
        results = []
        n_src = len(self.sources)
        out_dir = opts["out_dir"]
        os.makedirs(out_dir, exist_ok=True)
        self.log(f"输出目录: {out_dir}  (所有音源平铺在此, 不建子目录)")

        n_wav_src = 0      # 产出了 WAV 的音源数
        n_cd_skip = 0      # 因"只支持 WAV"被跳过 CD 的音源数

        for si, src in enumerate(self.sources, 1):
            if self.cancel_event.is_set():
                break
            name = self._unique_cd_name(sanitize_name(src["label"]))
            self.cur_src = si
            self.done_in_src = 0
            self.n_in_src = 0
            self.pass_idx = 0
            self.src_passes = self._passes_for(src["kind"])
            self.opts["cur_kind"] = src["kind"]
            self.q.put(("src", si, n_src, src["label"]))

            self.prog(f"[{si}/{n_src}] {src['label']}")
            self.log(f"▶ {src['label']}  ({src['kind']})")
            # 逐条说明本源的策略与跳过项 —— 绝不允许静默丢弃(FR-4.3 / PRD §1.2)
            plan = self._plan_line(src)
            if plan:
                self.log("   " + plan)
            if src["kind"] == "CHIP" and opts["do_cd"]:
                n_cd_skip += 1
                self.log(f"   ⚠ {src['label']}：多格式芯片音源仅输出 WAV，"
                         f"不参与 CD 镜像（已跳过 CD）")
            elif src["kind"] == "CD" and opts["do_cd"]:
                n_cd_skip += 1
                self.log(f"   ⚠ {src['label']}：CD 音轨本身就是 44.1k/16bit "
                         f"立体声，只输出 WAV（不再重封 BIN+CUE）")
            try:
                if src["kind"] == "SPC":
                    self._do_spc(src, name, out_dir, results)
                elif src["kind"] == "CHIP":
                    self._do_chip(src, name, out_dir, results)
                elif src["kind"] == "CD":
                    self._do_cd_audio(src, name, out_dir, results)
                else:
                    self._do_other(src, name, out_dir, results)
            except Exception as e:                   # noqa: BLE001
                self.q.put(("error", f"{src['label']}: {e}"))
                return
            if any(fmt == "WAV" for fmt, _n, _c in results[-1:]):
                n_wav_src += 1

        # 汇总: 让用户一眼看到"哪些真的转了、哪些被跳过"(不静默丢弃)
        self.q.put(("chip_summary", (n_wav_src, n_cd_skip)))

        if self.cancel_event.is_set():
            self.q.put(("cancelled",))
        else:
            self.q.put(("done", results))

    def _plan_line(self, src):
        """本源的回放/淡出策略说明(③ 区那一行也用同一套文案)。"""
        kind = src.get("kind")
        if kind == "CHIP":
            per = float(src.get("est_secs") or 0.0)
            exact = bool(src.get("exact"))
            fmt = src.get("fmt") or "芯片音源"
            n = max(1, int(src.get("n_tracks") or 0))
            head = f"{fmt}"
            if n > 1:
                head += f" ×{n} 首"
            if per > 0:
                mark = ("文件自带时长" if exact
                        else f"默认单遍 + 尾部 {CHIP_FADE_SECS:g}s 淡出")
                # 与界面口径一致: 时长列/策略文案显示**单轨**, 合计按总秒算
                cap = float(self.opts.get("max_secs") or 0.0)
                per = min(per, cap) if cap > 0 else per
                return (f"策略：{head} — 每首 {App._fmt_dur(per)}（{mark}），"
                        f"合计约 {App._fmt_dur(per * n)}")
            return f"策略：{head} — 按默认单遍截取 + 尾部 {CHIP_FADE_SECS:g}s 淡出"
        if kind == "SPC":
            policy = self.opts.get("policy_label") or self.opts.get("policy")
            return f"策略：SPC — {policy}"
        if kind == "CD":
            n = max(1, int(src.get("n_tracks") or 0))
            per = float(src.get("est_secs") or 0.0)
            total = float(src.get("total_secs") or 0.0) or per * n
            tail = f"，合计约 {App._fmt_dur(total)}" if total > 0 else ""
            return (f"策略：CD-DA 音轨 ×{n} — 按原长转 WAV"
                    f"（不截断、不淡出{tail}）")
        if kind == "光盘镜像":
            n = max(1, int(src.get("n_tracks") or 0))
            total = float(src.get("total_secs") or 0.0)
            tail = f"，合计约 {App._fmt_dur(total)}" if total > 0 else ""
            # v4.39: 纯数据盘的 XA 池里音效/语音占大头（DACLUA 229 条里 196 条
            # 是音效），勾了「只导音乐」就要在这行说清楚，否则用户对不上数字。
            music = ("，只导音乐（滤掉音效/语音）"
                     if (getattr(self, "opts", None) or {}).get("xa_music_only")
                     else "")
            # v4.43: 切曲会改变产出文件数，策略行要提前说明
            split = ("，并按静音间隔切曲"
                     if (getattr(self, "opts", None) or {}).get("xa_split")
                     else "")
            return (f"策略：光盘镜像 ×{n} 条音轨 — 抽轨按原长转 WAV"
                    f"（数据轨跳过、不截断、不淡出{tail}）"
                    f"；无 CD-DA 音轨或整轨静音时改抽数据轨的 XA-ADPCM"
                    f"{music}{split}")
        return ""



    # SPC: 音高/时长来自 ID666, 播放长度与时钟补偿都走 spc_wav 管线
    def _do_spc(self, src, name, out_dir, results):
        opts = self.opts
        files = spc_wav.collect_spc([src["path"]])
        if not files:
            self.log(f"   ⚠ 未发现 .spc, 跳过")
            return
        self.log(f"   {len(files)} 首 SPC")
        self.n_in_src = len(files)

        if not opts["do_cd"]:
            # 只出 WAV: 分批解码, 内存与临时磁盘占用有界
            sub = spc_wav.convert(
                files, out_dir, opts["policy"],
                on_log=self.log, on_progress=self._track_prog,
                cancel=self.cancel_event, speed=opts["speed"],
                max_secs=opts["max_secs"], used=self.used_wav,
                start_idx=self.wav_idx, total=self.total_wav or None,
                on_stage=self._stage)
            self.wav_idx = sub["last_idx"]
            self._report(results, "WAV", name, sub)
            return

        # 同时要 CD: 一次性解码, 同一份 PCM 写 CD 和 WAV, 不重复解码
        tracks = core.load_spc_paths(files, length_policy=opts["policy"],
                                     speed=opts["speed"],
                                     on_progress=self._decode_prog)
        try:
            if len(tracks) > 99:
                self.log("   ⚠ 超过 99 轨, 音频 CD 只保留前 99 轨能力")
            self.begin_pass()                    # 解码是第 0 遍, CD 是第 1 遍
            n = self.convert_cd(tracks, out_dir, name)
            results.append(("CD", name, n))
            if opts["do_wav"]:
                self.begin_pass()                # WAV 是最后一遍
                sub = self.write_wav(tracks, out_dir)
                self._report(results, "WAV", name, sub)
        finally:
            core.cleanup_spc_temp(tracks)

    # NSF/GBS/VGM/HES 等多格式芯片音源: 走 chip_decode 的子进程管线。
    # 与 _do_spc 的差别只有"时长来源"和"曲名策略"(设计 §4.3), 其余复用同一套
    # write_tracks, 所以输出规格与 SPC 逐字节一致。
    def _do_chip(self, src, name, out_dir, results):
        opts = self.opts
        if not Chip.ready() or chip_decode is None:
            self.log("   ⚠ 多格式解码模块未就绪（缺少 chip_formats.py / "
                     "chip_decode.py），已跳过")
            return
        files = Chip.collect_files([src["path"]])
        if not files:
            self.log("   ⚠ 未发现支持的芯片音源, 跳过")
            return
        n_tracks = 0
        for f in files:
            info = Chip.probe(f)
            n_tracks += max(1, int(getattr(info, "n_tracks", 0) or 0)) \
                if info is not None else 1
        self.log(f"   {len(files)} 个芯片音源文件, 预计 {n_tracks} 首")
        # v4.31: 同目录可能还有 CD 音轨（PCE-CD 的 .ape），一并算进进度分母
        cd_files = _cd_files(src["path"])
        self.n_in_src = max(1, n_tracks + len(cd_files))
        self.done_in_src = 0

        loader = getattr(chip_decode, "load_chip_paths", None)
        if loader is None:
            self.log("   ⚠ chip_decode 未提供 load_chip_paths, 已跳过")
            return
        extra = _supported_kwargs(
            spc_wav.convert,
            chip_secs=opts.get("chip_secs", CHIP_DEFAULT_SECS),
            loader=loader)
        if "loader" not in extra:
            # T02 还没接完(spc_wav.convert 尚不接受 loader): 明确说清原因,
            # 而不是让用户对着一个 TypeError 猜。
            self.log("   ⚠ spc_wav.convert 尚未接入芯片解码管线"
                     "（缺 loader 参数），本音源已跳过")
            return
        sub = spc_wav.convert(
            files, out_dir, "chip",
            on_log=self.log, on_progress=self._track_prog,
            cancel=self.cancel_event, speed=opts["speed"],
            max_secs=opts["max_secs"], used=self.used_wav,
            start_idx=self.wav_idx, total=self.total_wav or None,
            on_stage=self._stage, **extra)

        self.wav_idx = sub["last_idx"]
        # v4.31: PCE-CD 这类包是「.hes + .ape」混装 —— 芯片音源转完，把
        # 同目录的 CD 音轨接上（编号接着走）。**并成一次 _report**，否则
        # 「多轨合并」会生成两个同名文件互相覆盖，见 _merge_subs 的注释。
        cd_sub = self._convert_cd_files(src["path"], out_dir, cd_files)
        self._report(results, "WAV", name, _merge_subs(sub, cd_sub))

    def _do_cd_audio(self, src, name, out_dir, results):
        """纯 CD 音轨音源（单文件或「只含 .ape」的目录）-> WAV（v4.31）。"""
        files = _cd_files(src["path"])
        if not files:
            self.log("   ⚠ 未发现 CD 音轨, 跳过")
            return
        self.n_in_src = len(files)
        self.done_in_src = 0
        sub = self._convert_cd_files(src["path"], out_dir, files)
        if sub is None:
            return
        self._report(results, "WAV", name, sub)

    def _convert_cd_files(self, path, out_dir, files=None):
        """把一批 CD 音轨转成 WAV，返回统计字典（结构同 ``spc_wav.convert``）。

        ⚠ ``max_secs`` 固定 0：CD 音轨是**完整曲目**，不是循环 BGM。用户设的
        「单曲最长」是用来猜芯片音源循环终点的，拿它去砍 CD 音轨会把一首
        3 分钟的主题曲截成 150 秒（下载窗那条路径也是 0，保持一致）。
        """
        if files is None:
            files = _cd_files(path)
        if not files:
            return None
        if cd_audio is None or not cd_audio.available():
            self.log(f"   ⚠ 检测到 {len(files)} 首 CD 音轨，但当前缺少 "
                     f"FFmpeg 组件（PyAV），已跳过")
            return None
        self.log(f"   {len(files)} 首 CD 音轨（.ape 等），按原长转 WAV")
        agg = {"ok": 0, "fail": 0, "files": [], "seconds": 0.0,
               "errors": [], "last_idx": self.wav_idx}
        for i, f in enumerate(files, 1):
            if self.cancel_event.is_set():
                break
            self.prog(f"CD 音轨 {i}/{len(files)}：{os.path.basename(f)}")
            sub = cd_audio.convert_one(
                f, out_dir, used=self.used_wav, start_idx=self.wav_idx,
                max_secs=0.0, cancel=self.cancel_event, on_log=self.log)
            self.wav_idx = sub["last_idx"]
            self.done_in_src += 1
            self._add_secs(sub["seconds"])
            agg["ok"] += sub["ok"]
            agg["fail"] += sub["fail"]
            agg["files"] += list(sub.get("files") or ())
            agg["seconds"] += float(sub.get("seconds") or 0.0)
            agg["errors"] += list(sub.get("errors") or ())
            agg["last_idx"] = sub["last_idx"]
        return agg

    # MSU-1 / 光盘镜像 / MP3: 已经是 44.1k PCM, 不走 SPC 参数
    def _do_other(self, src, name, out_dir, results):
        opts = self.opts
        self.prog("读取音源…")
        # 纯数据盘的 XA 可能好几十分钟（Silent Hill 实测 63.8 分钟 / 200 条流），
        # 解码期间不报进度会让人以为卡死 —— 把进度回调一路传到 extract_xa_audio。
        xa_stats = {}
        tracks = core.load_source(
            src["path"],
            on_progress=lambda f: self.prog(f"解 XA 音频… {f * 100:.0f}%"),
            xa_music_only=bool(opts.get("xa_music_only")),
            xa_stats=xa_stats,
            xa_split=bool(opts.get("xa_split")))
        if not tracks:
            self.log("   ⚠ 未找到音轨, 跳过")
            core.cleanup_spc_temp(tracks)
            return
        self.n_in_src = len(tracks)
        self.done_in_src = 0
        try:
            self.log(f"   {len(tracks)} 条音轨")
            # v4.36: 有些 PS1 盘的 CD-DA 轨整轨静音，音乐其实在数据轨的
            # XA-ADPCM 里。core.load_source 会自动改抽 XA，这里把它说出来，
            # 否则用户看到轨名从「- 02」变成「- XA」会以为哪里错了。
            if any(" - XA" in t.get("name", "") for t in tracks):
                secs = sum(t.get("xa_secs") or 0.0 for t in tracks)
                self.log("   ℹ 没有可用的 CD-DA 音轨（整轨静音或纯数据盘），"
                         f"已改抽数据轨里的 XA-ADPCM（{len(tracks)} 条, "
                         f"{App._fmt_dur(secs)}）")
                # v4.39: 说清「只导音乐」滤掉了多少，否则用户对不上 229 这个数
                total = int(xa_stats.get("total") or 0)
                skipped = int(xa_stats.get("skipped") or 0)
                if skipped:
                    self.log(f"   ℹ 已开启「XA 只导音乐」：{total} 条 XA 流里滤掉 "
                             f"{skipped} 条音效/语音，保留 {len(tracks)} 条音乐")
                # v4.43: 按静音切曲的结果也要说出来，否则轨数从 1 变 4 会莫名其妙
                n_split = int(xa_stats.get("split_streams") or 0)
                if n_split:
                    self.log(f"   ℹ 已开启「XA 按静音切曲」：{n_split} 条流切成 "
                             f"{len(tracks)} 段")
                elif opts.get("xa_split"):
                    self.log("   ℹ 已开启「XA 按静音切曲」，但没找到 ≥"
                             f"{core._XA_SPLIT_MIN_GAP:g}s 的静音间隔，保持整条")
            if opts["do_cd"]:
                if len(tracks) > 99:
                    self.log("   ⚠ 超过 99 轨, 音频 CD 只保留前 99 轨能力")
                n = self.convert_cd(tracks, out_dir, name)
                results.append(("CD", name, n))
            if opts["do_wav"]:
                if opts["do_cd"]:
                    self.begin_pass()            # CD 是第 0 遍, WAV 是第 1 遍
                # ⚠ 光盘镜像（PS1/PCE-CD/MD-CD 的 CUE+BIN）不吃「单曲最长」：
                #   抽出来的是**完整曲目**，不是循环 BGM，截断等于毁数据。
                #   与 v4.31 给 .ape 定的口径一致（见 _convert_cd_files 的 max_secs=0）。
                cap = 0.0 if src.get("kind") == "光盘镜像" else None
                sub = self.write_wav(tracks, out_dir, max_secs=cap)
                self._report(results, "WAV", name, sub)
        finally:
            core.cleanup_spc_temp(tracks)

    def _report(self, results, fmt, name, sub):
        if sub["fail"]:
            self.log(f"   ❌ {fmt} 失败 {sub['fail']} 首: "
                     + "; ".join(sub["errors"][:3]))
        self.log(f"   ✅ {fmt} 完成 {sub['ok']} 首, "
                 f"{int(sub['seconds'] // 60)}:{sub['seconds'] % 60:04.1f}")
        # v4.24: 一个音源出多个 WAV 时再拼一个整轨（分轨全留）
        if fmt == "WAV":
            self._merge_wavs(name, sub)
        results.append((fmt, name, sub["ok"]))

    def _merge_wavs(self, name, sub):
        """把**本音源**刚产出的分轨拼成一个整轨文件（v4.24）。

        放在 ``_report`` 里，是因为四个产出点（SPC 只出 WAV / SPC 出 CD+WAV /
        芯片音源 / 其它 PCM）最后都走它 —— 只写一处就不会漏。
        """
        if not self.opts.get("merge_wav") or wavmerge is None:
            return
        files = [str(p) for p in (sub.get("files") or ()) if p]
        if len(files) < 2:
            return                     # 单轨音源没有合并的意义
        # 主窗：分轨本来就是「重转即覆盖同名」，合并文件保持一致行为，
        # 不用唯一名 —— 否则转两次就多出「（合并） 2.wav」垃圾。
        out = wavmerge.merge_target(self.opts["out_dir"], name)
        self.prog(f"合并 {len(files)} 首…")
        res = wavmerge.merge_wavs(files, out, on_log=None,
                                  cancel=self.cancel_event)
        if res.get("ok"):
            secs = float(res.get("seconds") or 0)
            self.log("   🔗 已合并 %d 首 → %s（%d:%04.1f）"
                     % (res.get("files") or len(files),
                        os.path.basename(out), secs // 60, secs % 60))
        elif not self.cancel_event.is_set():
            self.log("   ⚠ 合并失败：%s" % (res.get("error") or "未知错误"))


# ---------------------------------------------------------------------------
# 音源下载窗口 (joshw.info 全机种镜像)
# ---------------------------------------------------------------------------


_COL_KEYS = ("name", "zh", "mtime", "size", "state")


def _cols_store_path():
    try:
        d = os.path.join(os.environ["LOCALAPPDATA"], "MSU1Converter")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "download_cols.json")
    except (KeyError, OSError):
        return None


def load_col_widths():
    """读上次手动调过的列宽; 读不到就返回空 dict。"""
    import json
    path = _cols_store_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        return {k: int(v) for k, v in data.items() if k in _COL_KEYS}
    except Exception:                                # noqa: BLE001
        return {}


def save_col_widths(widths):
    import json
    path = _cols_store_path()
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({k: int(v) for k, v in widths.items()}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


# ---- 界面主题选择（v4.18）--------------------------------------------------
# 存在 %LOCALAPPDATA%\MSU1Converter\ui_theme.json，与列宽同一个目录。
# 读不到 / 文件坏了 / 主题被删掉，一律静默退回默认主题 —— 皮肤偏好不值得弹框。

def _ui_store_path(name):
    """返回 %LOCALAPPDATA%\\MSU1Converter\\<name>；拿不到目录时返回 None。"""
    try:
        d = os.path.join(os.environ["LOCALAPPDATA"], "MSU1Converter")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, name)
    except (KeyError, OSError):
        return None


def load_theme_key():
    """读上次选的主题 key；任何异常都退回默认主题。"""
    import json
    path = _ui_store_path("ui_theme.json")
    if not path or not os.path.isfile(path):
        return theme.DEFAULT_THEME
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        key = data.get("theme") if isinstance(data, dict) else None
    except Exception:                                # noqa: BLE001
        return theme.DEFAULT_THEME
    return key if key in theme.THEMES else theme.DEFAULT_THEME


def save_theme_key(key):
    """把主题选择写盘（tmp + os.replace，写失败静默）。"""
    import json
    path = _ui_store_path("ui_theme.json")
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"theme": str(key)}, fh)
        os.replace(tmp, path)
    except OSError:
        pass

class DownloadDialog(tk.Toplevel):
    """按机种浏览 joshw.info 子站镜像, 批量下载 .7z 音源包。

    线程模型 (与主窗口完全一致, 改这里前先读 README 第 4 条):
      * 扫描 / 下载全部在工作线程里跑, **绝不**在工作线程里调用任何 Tk 方法;
      * 工作线程只往 self.q 塞元组, 主线程用 after 轮询取事件并刷新界面;
      * 关闭窗口前先 after_cancel 掉轮询, 并置 _closed 让迟到的工作线程事件
        早退, 否则会出现 "invalid command name ..." 这类 Tcl 报错。

    一次扫描要抓 27 个字母目录页 (spc/s/ 单页 410 条), 所以必须有缓存:
    命中的机种秒开, 未命中的才起线程扫, 进度条按"页"推进。
    """

    GROUP_ORDER = ("复古主机", "现代主机", "计算机与街机")
    POLL_MS = 100            # 主线程取队列间隔(ms)
    PAINT_SEC = 0.2          # 进度条最多每 200ms 刷一次, 免得被事件淹没
    MAX_ROWS = 4000          # 单次最多往 Treeview 塞多少行, 超了提示用搜索
    # 字号统一指向 retro_theme(属性名保持不变, 旧的引用处不用全改)。
    # ⚠ 这里**只能**放字体常量: 颜色必须现取 theme.PALETTE[...]，写成类属性
    #   会在 import 时就冻结成「红白机」那一套，换主题后永远不跟着变。
    FONT = theme.FONT
    FONT_S = theme.FONT_S

    def __init__(self, master):
        super().__init__(master)
        self.title("音源下载 — joshw.info 全机种镜像")
        # 刻意**不**调 transient(master)：附属窗口会被 Win32 去掉
        # WS_MINIMIZEBOX / WS_MAXIMIZEBOX，标题栏只剩一个"关闭"。
        # 代价是失去"永远压在主窗口之上"的保证，改用 open_download 里的 lift() 补。
        # ttk 主题是进程级全局的(主窗口已 install), 这里只需把 Toplevel 自身的
        # 底色刷掉 —— 否则窗口边缘会露出一圈系统默认的浅灰。
        self.configure(bg=theme.PALETTE["bg"])
        # 高度 640 -> 700: 顶部多了一条 60px 横幅。
        # v4.5: 980 -> 1240 —— 布局改三列（机种 260 / 文件列表 / 日志 ~390），
        #       mid 至少要 520 才放得下「名称+中文名+日期」三列，否则日期被挤没。
        self.geometry("1400x720")
        self.minsize(1180, 620)
        self._zoomed = False
        self._pre_zoom = ""

        # ---- 生命周期 / 线程 ----
        self._closed = False
        self.q = queue.Queue()

        # ---- 试听播放器（v4.9）----
        # 只播**已下载到本地**的音源（用户确认「下载后再播放也可以」）——
        # 这样不必为试听去拉远程包，也就不引入「试听即下载」的隐式流量。
        self.player = None
        self._play_key = ""         # 当前试听对应的条目 url（判重用）
        self._play_path = ""
        self._play_track = 0
        self._track_list = []
        self._loading = False
        self._after_player = None
        self._entry_of_iid = {}     # iid -> 条目（_render_rows 每轮重建）
        try:
            import player as _pm
            self.player = _pm.PcmPlayer()
        except Exception:                                    # noqa: BLE001
            self.player = None
        self._after_poll = None
        self._after_filter = None
        self._scan_tok = 0                    # 机种切换代际号, 丢弃过期事件
        self._cancel_scan = threading.Event()
        self._scanning = False

        # ---- 数据 ----
        self.entries = []                     # 当前机种全部条目
        self.visible = []                     # 过滤后可见条目
        self._zh_logged = set()               # 已提示过覆盖率的机种
        self._zh_cancel = threading.Event()   # 翻译中止信号
        self._zh_running = False
        self.sel = set()                      # 已选条目 url (跨过滤保留)
        self._states = {}                     # url -> 状态文字
        self.iid_of = {}                      # url -> Treeview iid
        self._platform_iid = {}               # key -> Treeview iid
        self.cur_key = None
        self.cur_name = ""
        self._dest_auto = True                # 保存目录是否仍随机种自动变化
        self._guard_dest = False              # 程序写 var_dest 时抑制 trace

        # ---- 下载状态 ----
        self.dl = None
        self._downloading = False
        self._dl_total = 0
        self._dl_shown = 0.0
        self._dl_finished = 0
        self._dl_files_done = 0
        self._dl_ok = 0
        self._dl_fail = 0
        self._dl_skip = 0
        self._dl_expect = 0
        self._inflight = 0
        self._dl_extracted = 0        # 解压成功的包数
        self._dl_ex_files = 0         # 解出来的文件总数
        self._dl_ex_failed = 0        # 解压失败的包数
        self._size_of_path = {}               # 规范化目标路径 -> 字节
        self._url_of_path = {}                # 规范化目标路径 -> url
        self._url_of_base = {}                # 文件名 -> url
        self._last_paint = 0.0
        self._disk = {}                       # 落盘文件名 -> 字节 (一次 scandir, 供「✓ 已下载」用)
        self._disk_root = None

        # ---- v4.23: 云盘上传（下载窗）----
        # 与主窗各自一套状态, 但**共用** CloudUploader; 事件走 self.q。
        self._cloud_busy = False
        self._cloud_cancel = threading.Event()

        # ---- v4.0: 下载后自动转 WAV ----
        # 全部状态都放在这里初始化, 避免「某个属性只在开过开关后才存在」导致
        # 属性错误(这类 bug 在 _finish_download / _shutdown 里最难查)。
        self._cv = None                    # dl_convert.ConvertWorker 或 None(没开/模块缺)
        self._cv_cancel = None              # threading.Event —— **与 joshw_dl 同一个对象**(R0b)
        self._cv_queued = 0                 # 累计入队**文件**数(转换阶段分母, 架构师定案)
        self._cv_done = 0                   # 已处理(成功+失败+跳过)
        self._cv_ok = 0                     # 成功文件数
        self._cv_fail = 0                   # 失败文件数
        self._cv_skip = 0                   # 已转过而跳过
        self._cv_bytes = 0                  # 累计产物体积(用 getsize 累加, 不用秒×176400)
        self._cv_wav_dir = None             # 冻结的绝对输出目录(A5: 转换期间切机种不影响)
        self._cv_current = None             # 最近一次在转文件名(仅状态行用)
        self._cv_src_finished = False       # all_done 是否已到
        self._cv_abandoned = 0              # 因磁盘不足放弃的文件数
        self._pending_resume = []           # 「已下载但未转过」的条目(只进转换队列)
        self._cv_pkg_logged = set()         # 已打过策略汇总行的包(FR-C10.3 每包一行)
        self._est_token = 0                 # 体量预估代际号(丢弃过期结果)
        self._after_est = None              # 预估的防抖 after id
        self._finished = False              # 本轮是否已收尾(_finish_download 重入守卫)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_poll = self.after(self.POLL_MS, self._poll)
        self._load_platforms()
        self.after(50, lambda: apply_dark_title_bar(self))

    # ---- 构建界面 -----------------------------------------------------------
    def _build(self):
        # 顶部 60px 小横幅: 与主窗口同一套视觉。不重复窗口标题文字 —— 标题栏
        # 已经有「音源下载 — joshw.info 全机种镜像」了。
        self.banner = theme.RetroBanner(self, height=60)
        self.banner.pack(fill="x", padx=0, pady=(0, 4))
        self.banner.start()

        # 三列用 **ttk.PanedWindow**（v4.6）：用户可拖动分隔条自行分配宽度。
        # 为什么不是 pack：pack 的 expand 按"先到先得"分配，中间列的文件列表
        # 请求宽度极大（名称/中文名/日期/大小/状态五列），实测会把日志列整个
        # 挤出可视区（宽度变 0）。为什么不是 grid：grid 能保证份额，但**份额
        # 写死在代码里** —— 日志列固定 300px 时，长路径/长曲名显示不下，
        # 只能横向滚动（用户实测报障）。PanedWindow 让用户自己拖。
        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=8)
        self._body = body

        # 左侧: 机种树 (固定 260px)
        left = ttk.Frame(body, width=260)
        left.pack_propagate(False)
        body.add(left, weight=0)
        ttk.Label(left, text="机种", style="Window.Accent.TLabel",
                  font=theme.FONT_B).pack(anchor="w")
        lw = ttk.Frame(left)
        lw.pack(fill="both", expand=True, pady=(4, 2))
        self.tv_plat = ttk.Treeview(lw, columns=("fmt",), show="tree headings",
                                    selectmode="browse")
        self.tv_plat.heading("#0", text="机种")
        self.tv_plat.heading("fmt", text="格式")
        self.tv_plat.column("#0", width=176, anchor="w")
        self.tv_plat.column("fmt", width=58, anchor="center")
        self.tv_plat.tag_configure("grp", font=retro_font(theme.FONT_B),
                                   foreground=theme.PALETTE["amber"])
        ps = ttk.Scrollbar(lw, orient="vertical", command=self.tv_plat.yview)
        self.tv_plat.configure(yscrollcommand=ps.set)
        self.tv_plat.pack(side="left", fill="both", expand=True)
        ps.pack(side="left", fill="y")
        self.tv_plat.bind("<<TreeviewSelect>>", self._on_platform_select)
        ttk.Label(left, text="选中机种即可看到列表（自带内置索引）；"
                            "缺新条目时点「清除缓存」重扫",
                  style="Window.Muted.TLabel", font=theme.FONT_S,
                  wraplength=238, justify="left").pack(anchor="w", pady=(2, 6))

        # 右侧
        mid = ttk.Frame(body)
        body.add(mid, weight=1)

        # 第三列：日志（独立成列，占满整列高度）
        # ⚠ 必须 pack_propagate(False) + 显式 width：中间列的文件列表带着
        #   「名称/中文名/日期/大小/状态」五列，请求宽度很大；若让 pack 按请求
        #   分配，本列会被整个挤出可视区（实测 1240 宽时日志列宽度变成 0）。
        right = ttk.Frame(body, width=380)
        right.pack_propagate(False)
        body.add(right, weight=0)

        srow = ttk.Frame(mid)
        srow.pack(fill="x")
        ttk.Label(srow, text="搜索:", style="Window.TLabel").pack(side="left")
        self.var_search = tk.StringVar(value="")
        self.var_search.trace_add("write", lambda *_a: self._schedule_filter())
        ttk.Entry(srow, textvariable=self.var_search).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(srow, text="✕", width=3,
                   style="Small.TButton",
                   command=lambda: self.var_search.set("")).pack(side="left")
        self.var_count = tk.StringVar(value="共 0 条，已选 0 条")
        ttk.Label(srow, textvariable=self.var_count,
                  style="Window.Accent.TLabel").pack(side="left", padx=(10, 0))
        # 标题栏已经有系统的最小化/最大化按钮了(去掉 transient 之后才有)，
        # 这里再给一个窗口内的，方便贴着列表区操作。
        self.btn_zoom = ttk.Button(srow, text="⛶", width=3,
                                   style="Small.TButton",
                                   command=self._toggle_maximize)
        self.btn_zoom.pack(side="left", padx=(10, 0))
        self.bind("<F5>", lambda _e: self._toggle_maximize())
        self.bind("<Escape>", self._on_escape)

        tw = ttk.Frame(mid)
        tw.pack(fill="both", expand=True, pady=6)
        self.tree = ttk.Treeview(
            tw, columns=("play", "name", "zh", "mtime", "size", "state"),
            show="headings", selectmode="extended")
        # 列宽合计必须 <= 树宽, 否则靠后的列会被直接切掉(原来 430+100+92+112=734
        # 就比右侧可用宽度(~660)大, 日期只剩 "2021"、大小/状态完全看不见)。
        # 加了「中文名」后 260+130+120+70+80=660, 只剩一点余量, 只有名称列 stretch。
        # v4.9: 首列 "▶" —— 点它试听**已下载到本地**的音源（没下载会提示）。
        for col, text, width, anchor, stretch in (
                ("play", "▶", 32, "center", False),
                ("name", "名称", 236, "w", True),
                ("zh", "中文名", 130, "w", False),
                ("mtime", "日期", 116, "center", False),
                ("size", "大小", 66, "e", False),
                ("state", "状态", 78, "w", False)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width,
                             minwidth=(160 if col == "name" else 40),
                             anchor=anchor, stretch=stretch)
        saved = load_col_widths()                    # 用户上次拖过的列宽优先
        if saved:
            for _c, _w in saved.items():
                try:
                    self.tree.column(_c, width=_w, stretch=False)
                except tk.TclError:
                    pass
        self._last_cols = None
        for _ev in ("<B1-Motion>", "<ButtonRelease-1>"):
            self.tree.bind(_ev, self._remember_cols, add="+")
        # v4.9 试听：点首列 ▶ 播放该行。
        # ⚠ 这里必须用**本方法内唯一**的上下文做锚点 —— 上一版用
        #   `self.tree.pack(...)` 做锚点，结果替换命中了 PickDialog 的同名行
        #   （`str.replace(..., 1)` 取第一处），绑定加到了别的类的树上：
        #   下载窗口的 ▶ 完全没反应，而 PickDialog 里点第一列会抛
        #   AttributeError（它没有 _on_tree_click）。
        self.tree.bind("<Button-1>", self._on_tree_click, add="+")
        # 右键：对解不了的格式（N64 的 .miniusf 等）交给系统关联的外部播放器
        self.tree.bind("<Button-3>", self._on_tree_right_click, add="+")
        # 机翻里中英混杂的"半吊子"整行压暗, 提醒这条大概率需要人工修。
        # 注意: ttk tag 只认 image/background/foreground/font/padding 等, 传
        # selectbackground 会让整条调用抛 TclError —— 这块曾因缩进错位+参数非法
        # 连续静默失效两次, 改完务必用 tag_configure("weak","foreground") 回读确认。
        try:
            self.tree.tag_configure("weak", foreground=theme.PALETTE["dim"])
        except tk.TclError:
            pass
            self.tree.column(col, width=width, minwidth=(200 if col == "name" else 40),
                             anchor=anchor, stretch=stretch)
        ts = ttk.Scrollbar(tw, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ts.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ts.pack(side="left", fill="y")
        # 横向滚动条: 列宽总和超过窗口时还能滑过去(之前只能被裁掉)
        self.hbar = ttk.Scrollbar(tw, orient="horizontal",
                                  command=self.tree.xview)
        self.tree.configure(xscrollcommand=self.hbar.set)
        self.hbar.pack(side="bottom", fill="x", before=self.tree)
        self.tree.bind("<<TreeviewSelect>>", self._on_row_select)
        self.tree.bind("<Double-1>", self._on_row_double)

        # 保存目录
        drow = ttk.Frame(mid)
        drow.pack(fill="x", pady=(0, 4))
        ttk.Label(drow, text="保存目录:", style="Window.TLabel").pack(side="left")
        self.var_dest = tk.StringVar(value=self._default_dest(""))
        self.var_dest.trace_add("write", self._on_dest_change)
        ttk.Entry(drow, textvariable=self.var_dest).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(drow, text="浏览…", style="Small.TButton",
                   command=self._pick_dest).pack(side="left")

        # 操作行
        arow = ttk.Frame(mid)
        arow.pack(fill="x", pady=(0, 4))
        ttk.Label(arow, text="并发:", style="Window.TLabel").pack(side="left")
        self.var_workers = tk.StringVar(value="2")
        self.spin_workers = ttk.Spinbox(arow, from_=1, to=4, width=3,
                                        textvariable=self.var_workers)
        self.spin_workers.pack(side="left", padx=(4, 10))
        self.btn_start = ttk.Button(arow, text="开始下载",
                                    style="Hero.TButton",
                                    command=self._start_download, state="disabled")
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(arow, text="停止",
                                   style="Small.TButton",
                                   command=self._stop_download, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        ttk.Button(arow, text="打开文件夹", style="Small.TButton",
                   command=self._open_dir).pack(side="left", padx=4)
        # v4.23: 上传云盘。放**第一行**末尾（实测这一行还有约 240px 余量），
        # 而不是 arow2 —— 那行已有 4 个按钮, 再加一个会把「翻译未收录」挤到
        # 折行; 也不放「解压器/预估」行, 那里离主操作区太远, 用户找不到。
        self.btn_cloud = ttk.Button(arow, text="上传云盘", width=12,
                                    style="Small.TButton",
                                    command=self.upload_cloud)
        self.btn_cloud.pack(side="left", padx=(12, 0))
        # v4.26: 账号菜单（切换账号 / 退出登录）。单独一个「▾」，不把菜单挂在
        # 「☁ 上传云盘」的右键上 —— 右键没有任何视觉提示，用户找不到。
        self.btn_cloud_acct = ttk.Button(
            arow, text="▾", width=2, style="Small.TButton",
            command=lambda: popup_cloud_account_menu(
                self.btn_cloud_acct, None, self, self._log, owner=self))
        self.btn_cloud_acct.pack(side="left", padx=(0, 4))

        # v4.5：第三列日志占走 300px 后，中间列放不下一整排 8 个按钮
        # （实测 1240 宽时「翻译未收录」被整块裁掉）。故选/缓存类操作
        # 单独折到第二行 —— 比缩按钮文字更可靠，也不会因字体差异再被裁。
        arow2 = ttk.Frame(mid)
        arow2.pack(fill="x", pady=(0, 4))
        ttk.Button(arow2, text="全选", width=6, style="Small.TButton",
                   command=self._select_all).pack(side="left")
        ttk.Button(arow2, text="反选", width=6, style="Small.TButton",
                   command=self._invert_selection).pack(side="left", padx=4)
        ttk.Button(arow2, text="清除缓存", style="Small.TButton",
                   command=self._clear_cache).pack(side="left", padx=4)
        self.btn_zh = ttk.Button(arow2, text="翻译未收录",
                                 style="Small.TButton",
                                 command=self._toggle_zh_translate)
        self.btn_zh.pack(side="left", padx=4)

        # ---- 自动解压 ----
        # joshw 托管的包除 SNES 的裸 .spc 外全是 .7z。下载完自动解开,
        # 用户就能把解出来的 .nsf/.gbs/.vgm/.hes 直接拖进主窗口。
        erow = ttk.Frame(mid)
        erow.pack(fill="x", pady=(0, 4))
        self.var_auto_extract = tk.BooleanVar(value=True)
        self.var_auto_extract_cb = ttk.Checkbutton(
            erow, variable=self.var_auto_extract, text="下载后自动解压")
        self.var_auto_extract_cb.pack(side="left", padx=(0, 10))
        self.var_del_archive = tk.BooleanVar(value=False)
        self.var_del_archive_cb = ttk.Checkbutton(
            erow, variable=self.var_del_archive, text="解压后删除原 .7z 包")
        self.var_del_archive_cb.pack(side="left", padx=(0, 10))

        # ---- 自动转 WAV(v4.0) ----
        # ⚠ 门禁(FR-C1.2/C1.3): 芯片包需要「下载后自动解压」才能拿到源文件,
        #   所以关掉自动解压时本开关置灰。但 **SPC 是裸文件、不经解压**,
        #   门禁提示文案必须限定「芯片包」, 否则用户会以为 SPC 也转不了
        #   (AC-C23)。libgme/chip_decode 缺失同理 —— SPC 走 policy=id666,
        #   只依赖主 spc_wav/msu1_to_cd, **仍应可转**(AC-C11)。
        self.var_auto_wav = tk.BooleanVar(value=False)   # 默认关, 且**不持久化**(FR-C1.5)
        self.var_auto_wav_cb = ttk.Checkbutton(
            erow, variable=self.var_auto_wav, text="下载后自动转 WAV")
        self.var_auto_wav_cb.pack(side="left", padx=(0, 10))
        # v4.24: 多轨合并成单文件。一个 NSF/VGM 动辄几十轨，转出来是几十个
        # WAV；勾上这个会**另外**再拼一个整轨文件（分轨全留，方便单独听）。
        # 依赖「自动转 WAV」—— 没开转 WAV 时置灰，否则用户会以为它能
        # 把磁盘上已有的分轨合起来（那只在下次下载/重转时才生效）。
        self.var_merge = tk.BooleanVar(value=False)      # 默认关，不持久化
        self.var_merge_cb = ttk.Checkbutton(
            erow, variable=self.var_merge, text="多轨合并成单文件")
        self.var_merge_cb.pack(side="left", padx=(0, 10))

        # ---- crow: 解压器标签 + 体量预估 ----
        # 「解压器: tar.exe」**必须独占一行**(PRD B9 / AC-C20): 它是 erow 的
        # 最后一个控件, erow 再塞第三个复选框会被挤掉。
        crow = ttk.Frame(mid)
        crow.pack(fill="x", pady=(0, 4))
        self.var_extract_tar = tk.StringVar(value="")
        ttk.Label(crow, textvariable=self.var_extract_tar,
                  style="Window.Muted.TLabel", font=theme.FONT_S
                  ).pack(side="left", padx=(0, 12))
        # 体量预估(AC-C22): 未勾选显示 —, 勾选后 ≈ N 首 / X GB
        self.var_wav_estimate = tk.StringVar(value="—")
        ttk.Label(crow, textvariable=self.var_wav_estimate,
                  style="Window.Muted.TLabel", font=theme.FONT_S
                  ).pack(side="left")

        # 把解压器状态摆出来: 找不到 tar.exe 时用户能提前看到, 而不是等下载完
        # 才发现没解开。路径串很长, 所以用短标签而不是塞进状态行。
        tar_exe = joshw_dl.find_tar() if joshw_dl is not None else None
        if joshw_dl is None:
            self.var_extract_tar.set("")
        elif tar_exe:
            self.var_extract_tar.set(f"解压器: {os.path.basename(tar_exe)}")
        else:
            self.var_extract_tar.set(
                "⚠ 未找到解压器，下载后需手动解压")
        ttk.Label(
            mid,
            text="※ 下载回来的 NSF / GBS / VGM / HES 会自动解压，"
                 "可直接拖进主窗口转换",
            style="Window.Muted.TLabel", font=theme.FONT_S,
            wraplength=600, justify="left",
        ).pack(anchor="w", pady=(0, 2))
        ttk.Label(
            mid,
            text="※ 勾上「下载后自动转 WAV」后，产物平铺在「保存目录\\WAV\\」，"
                 "源文件与 .7z 保持原样；随时点「停止」可连下载/解压/转换一起停",
            style="Window.Muted.TLabel", font=theme.FONT_S,
            wraplength=600, justify="left",
        ).pack(anchor="w", pady=(0, 2))

        # 门禁随「自动解压」勾选变化(FR-C1.2); 勾选变化也触发体量预估(AC-C22)
        self.var_auto_extract.trace_add("write", self._on_auto_extract_toggle)
        self.var_auto_wav.trace_add("write", self._on_auto_wav_toggle)
        self.var_merge.trace_add("write", self._on_merge_toggle)
        self._update_gate_hint()

        # 进度 + 状态
        prow = ttk.Frame(mid)
        prow.pack(fill="x", pady=(2, 2))
        self.progress = ttk.Progressbar(prow, mode="determinate", maximum=100,
                                        style="TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True)
        self.var_status = tk.StringVar(value="就绪 — 请在左侧选择机种")
        ttk.Label(mid, textvariable=self.var_status, anchor="w",
                  style="Window.Muted.TLabel").pack(fill="x", pady=(0, 6))

        # 试听条（v4.9）：先 pack，日志占剩余高度
        self._build_preview_bar(right)

        # 第三列：日志占满整列（原先是底部 4 行，v4.5 改为独立列）。
        # width=46 是"请求宽度"，决定第三列的大致宽度（约 390px）；
        # fill="both"+expand 让它纵向吃满、横向也随窗口拉伸。
        logf = ttk.LabelFrame(right, text=" 日志 ", style="Card.TLabelframe")
        logf.pack(fill="both", expand=True)
        # v4.6：日志改成 **wrap="word" 自动换行**。
        # 原先是 wrap="none" + 固定 300px 列宽 —— 长路径（如转出的 WAV 全路径）
        # 直接被右边缘切掉，用户报障「日志栏宽度固定，无法将信息显示全面」。
        # 自动换行保证**任何长度的内容都能看全**；配合可拖动的列宽，
        # 想要宽行就把日志列拖大（PanedWindow），两边都满足。
        # 不用 scrolledtext.ScrolledText：它自带的是 tk.Scrollbar，在深色窗口里
        # 是一条系统白条（见 skin_log 注释）；这里用 tk.Text + ttk.Scrollbar。
        logbox = ttk.Frame(logf)
        logbox.pack(fill="both", expand=True, padx=6, pady=6)
        logbox.rowconfigure(0, weight=1)
        logbox.columnconfigure(0, weight=1)
        self.logw = tk.Text(logbox, width=40, height=4, wrap="word",
                            state="disabled",
                            font=retro_font(theme.FONT_MONO))
        vs = ttk.Scrollbar(logbox, orient="vertical", command=self.logw.yview)
        self.logw.configure(yscrollcommand=vs.set)
        self.logw.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        skin_log(self.logw)   # 对 tk.Text：只上色，不动滚动条（已用 grid 布好）
        # 主窗换主题时把本窗口的 tk 原生部分（日志 Text / 列表 tag / 横幅）也刷一遍。
        # apply_theme 只对 install 过的窗口重跑样式；本窗口没单独 install
        # （ttk 样式是进程级全局的，会自动跟着变），但 Text 和 tag 得手动来。
        theme.on_theme_skinned(self, self._reskin_theme)

        if jw is None or joshw_dl is None:
            missing = "joshw_source.py" if jw is None else "joshw_dl.py"
            self._log(f"❌ 缺少 {missing}，下载功能不可用（其余界面正常）")

    # ---- 小工具 -------------------------------------------------------------
    def _reskin_theme(self):
        """主窗换主题后重刷本窗口的 tk 原生部分（v4.18）。

        ttk 样式是进程级全局的，主窗 install 一遍本窗口就跟着变；
        这里只补三样「样式表管不到」的：窗口底色、日志 Text、Treeview 的行 tag。
        """
        try:
            self.configure(bg=theme.PALETTE["bg"])
        except tk.TclError:
            return
        try:
            skin_log(self.logw)
        except Exception:                            # noqa: BLE001
            pass
        p = theme.PALETTE
        for name, tag, color in (("tv_plat", "grp", p["amber"]),
                                 ("tree", "weak", p["dim"])):
            widget = getattr(self, name, None)
            if widget is None:
                continue
            try:
                widget.tag_configure(tag, foreground=color)
            except tk.TclError:
                pass
        banner = getattr(self, "banner", None)
        if banner is not None:
            try:
                banner.refresh()
            except Exception:                        # noqa: BLE001
                pass

    # ---- 试听（v4.9）：只播已下载到本地的音源 ---------------------------

    @staticmethod
    def _fmt_time(sec):
        try:
            sec = max(0, int(float(sec)))
        except (TypeError, ValueError):
            sec = 0
        return "%d:%02d" % (sec // 60, sec % 60)

    def _build_preview_bar(self, parent):
        """下载窗第三列底部的试听条。"""
        bar = ttk.LabelFrame(parent, text=" 试听 ", style="Card.TLabelframe")
        bar.pack(fill="x", pady=(4, 0))
        r1 = ttk.Frame(bar)
        r1.pack(fill="x", padx=8, pady=(6, 2))
        self.btn_play = ttk.Button(r1, text="▶", width=3, style="Small.TButton",
                                   state="disabled", command=self._toggle_play)
        self.btn_play.pack(side="left")
        self.btn_pstop = ttk.Button(r1, text="■", width=3,
                                    style="Small.TButton", state="disabled",
                                    command=self._stop_play)
        self.btn_pstop.pack(side="left", padx=(4, 6))
        self.play_prog = ttk.Progressbar(r1, mode="determinate", maximum=1000,
                                         style="TProgressbar")
        self.play_prog.pack(side="left", fill="x", expand=True)
        self.play_prog.bind("<Button-1>", self._on_prog_click)
        self.play_prog.bind("<B1-Motion>", self._on_prog_click)
        self.var_ptime = tk.StringVar(value="0:00")
        ttk.Label(r1, textvariable=self.var_ptime, style="Window.Muted.TLabel",
                  font=theme.FONT_S).pack(side="left", padx=(6, 0))

        # 第二行：曲名（可伸缩） + 换轨 ◀▶
        # ⚠ 音量**不能放第一行** —— 300px 的列宽下，"▶■ + 进度条 + 时间 + 音量"
        #   会把进度条压到只剩几十像素（实测截图确认）。
        r2 = ttk.Frame(bar)
        r2.pack(fill="x", padx=8, pady=(0, 6))
        # 换轨按钮（v4.17）：**先 pack side="right" 再 pack 曲名**。
        #   曲名 label 带 expand=True，先 pack 会把右边缘空间全吃掉，
        #   ◀▶ 会被挤到可视区外（主窗那边就是这么排的，照抄顺序）。
        #   先 pack 的 next 贴最右，后 pack 的 prev 落在它左边 → 视觉上 ◀▶。
        self.btn_next = ttk.Button(r2, text="▶", width=2,
                                   style="Small.TButton", state="disabled",
                                   command=lambda: self._switch_track(1))
        self.btn_next.pack(side="right")
        self.btn_prev = ttk.Button(r2, text="◀", width=2,
                                   style="Small.TButton", state="disabled",
                                   command=lambda: self._switch_track(-1))
        self.btn_prev.pack(side="right", padx=(0, 2))
        self.var_pname = tk.StringVar(value="点列表 ▶ 试听（需先下载）")
        ttk.Label(r2, textvariable=self.var_pname, style="Window.Muted.TLabel",
                  font=theme.FONT_S, anchor="w",
                  wraplength=170, justify="left").pack(side="left", fill="x",
                                                       expand=True)
        # 第三行：音量。**必须独占一行** —— 放在曲名行右侧时，300px 的列宽
        # 分给滑块只剩几十像素，拖动范围太小（用户实测报障"滑块太短"）。
        # 独占一行 + fill="x" 能拿到约 200px，调节精度提高 3 倍。
        r3 = ttk.Frame(bar)
        r3.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(r3, text="VOL", style="Mono.TLabel").pack(side="left",
                                                            padx=(0, 4))
        self.var_vol = tk.DoubleVar(value=100.0)
        ttk.Scale(r3, from_=0, to=100, orient="horizontal", length=140,
                  variable=self.var_vol,
                  command=self._on_volume).pack(side="left", fill="x",
                                                expand=True)
        self.var_vol_txt = tk.StringVar(value="100%")
        ttk.Label(r3, textvariable=self.var_vol_txt,
                  style="Window.Muted.TLabel", font=theme.FONT_S,
                  width=4, anchor="e").pack(side="left", padx=(4, 0))

    def _on_tree_click(self, ev):
        """点首列 ▶ → 试听该行（本地已下载的音源）。"""
        if self.player is None:
            return
        try:
            if self.tree.identify_column(ev.x) != "#1":
                return
            iid = self.tree.identify_row(ev.y)
        except tk.TclError:
            return
        if iid:
            self._preview_row(iid)

    def _on_tree_right_click(self, ev):
        """右键：用外部播放器打开已下载的文件。"""
        try:
            iid = self.tree.identify_row(ev.y)
        except tk.TclError:
            return
        if not iid:
            return
        e = self._entry_of_iid.get(iid)
        if e is None:
            return
        path = self._find_local_audio(e)
        if not path:
            self._log("⏸ 尚未下载：%s —— 先下载才能用外部播放器打开"
                      % str(e.get("name") or "")[:46])
            return
        popup_open_menu(self, ev, path, self._log, self._local_folder_of(e))

    def _preview_row(self, iid):
        e = self._entry_of_iid.get(iid)
        if e is None:
            return
        path = self._find_local_audio(e)
        if not path:
            self._log("⏸ 尚未下载：%s —— 下载到本地后才能试听"
                      % str(e.get("name") or "")[:46])
            self.var_pname.set("未下载 · 请先「开始下载」")
            return
        if path == self._play_path and self.player.is_active():
            self._toggle_play()
            return
        # 本工具解不了的格式 → 整包交给 foobar2000（而不是只丢一个文件过去）
        if os.path.splitext(path)[1].lower() not in SELF_PLAYABLE_EXTS:
            folder = self._local_folder_of(e)
            open_with_foobar(folder or path, self._log)
            # v4.40: PSF 用户点 ▶ 多半是想听一下，但真正的诉求通常是"要 WAV"。
            # 顺手把转 WAV 的入口说出来，否则他得自己猜右键菜单里有。
            if _psf_pack_dir(folder or path):
                self._log("   ℹ 要转成 WAV：右键该条目 → 「🎵 用 foobar2000 转 WAV」")
            # v4.41: PS1 的 .txtp 包**能全自动转**（原生解 .xa/.DA），
            # 只是内置播放器放不了。这里说清「怎么转」，避免用户以为它和
            # PSF 一样必须手动。
            elif os.path.splitext(path)[1].lower() == ".txtp":
                self._log("   ℹ 本包是 PS1 原生索引（.txtp），可直接转 WAV："
                          "勾上「下载后自动转 WAV」再点「开始下载」—— "
                          "已下载的包不会重下，只补转换")
            return
        self._start_load(path, 0)

    def _find_local_audio(self, entry):
        """在保存目录里找该条目**已下载**的音频文件；找不到返回空串。

        ⚠ 实测目录结构（2026-10-18，用户的 NES-FC 目录）：
            保存目录/
              <包名>[机种]/            ← 解压出来是**文件夹**，不是散文件
                  <包名>.nsf / .spc / .m3u
              WAV/                      ← 勾了「自动转 WAV」时产物在这
        所以必须先匹配**目录名**再进去找；只扫顶层文件会一个都匹配不到。
        （这一版最初的实现只扫顶层，实测 0 命中，才改成的现在这样。）
        """
        root = self._dest_root()
        if not root or not os.path.isdir(root):
            return ""
        stem = self._file_name(entry.get("name") or "")
        if stem.lower().endswith(".7z"):
            stem = stem[:-3]
        low = stem.lower()
        # 原始音源扩展名（不含 .wav —— WAV 是转换产物，单独在下面找）。
        # ⚠ 必须**包含本工具解不了的 7 种**（USF/SSF/DSF/2SF/GSF/WSR/AIF）：
        #   它们是真实存在的音源文件，找得到才能给用户「暂不支持」的明确提示；
        #   漏掉它们的话，用户明明下载了 N64 的 .usf，点 ▶ 却被告知「尚未下载」。
        exts = (".cue", ".spc", ".nsf", ".nsfe", ".gbs", ".hes", ".kss",
                ".sap", ".gym", ".ay", ".vgm", ".vgz",
                ".usf", ".ssf", ".dsf", ".2sf", ".gsf", ".wsr", ".aif",
                # joshw 实际下发的是 mini* 变体（一个包几十首）
                ".miniusf", ".minigsf", ".minissf", ".minidsf",
                ".mini2sf", ".minipsf",
                # v4.40: PSF 家族的主名/psf1/psf2 变体。原先只登记了
                # `.minipsf`，用户下回来的 `.psf` / `.psf2` 会找不到 →
                # 点 ▶ 反而被告知「尚未下载」。直接从 psf_bridge 取，
                # 免得两边各写一份又漂移。
                # v4.41: 加 `.txtp` —— PS1 的 JoshW 包根目录就是 41 个
                # `.txtp`，不含它的话点 ▶ 会说「尚未下载」，与事实不符。
                ".txtp",
                ) + (psf_bridge.PSF_TRACK_EXTS if psf_bridge is not None else ())
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return ""
        # ① 同名目录（最常见）
        for fn in names:
            full = os.path.join(root, fn)
            if os.path.isdir(full) and fn.lower().startswith(low):
                hit = self._first_audio_in(full, exts)
                if hit:
                    return hit
        # ② 同名散文件（老版本或手动放的文件）
        for fn in names:
            full = os.path.join(root, fn)
            if not os.path.isfile(full):
                continue
            if (os.path.splitext(fn)[1].lower() in exts
                    and fn.lower().startswith(low)):
                return full
        # ③ 转出的 WAV
        wavdir = os.path.join(root, "WAV")
        if os.path.isdir(wavdir):
            try:
                for fn in sorted(os.listdir(wavdir)):
                    if fn.lower().endswith(".wav") and low in fn.lower():
                        return os.path.join(wavdir, fn)
            except OSError:
                pass
        return ""

    def _local_folder_of(self, entry):
        """该条目解压出来的文件夹（没有则空串）。

        用途：**整包交给 foobar2000**。一个 N64 包里有几十个 `.miniusf`，
        只传其中一个文件的话，一是只播一首、二是万一那首的格式 foobar 不认，
        整个包就都听不了。
        """
        root = self._dest_root()
        if not root or not os.path.isdir(root):
            return ""
        stem = self._file_name(entry.get("name") or "")
        if stem.lower().endswith(".7z"):
            stem = stem[:-3]
        low = stem.lower()
        try:
            for fn in sorted(os.listdir(root)):
                full = os.path.join(root, fn)
                if os.path.isdir(full) and fn.lower().startswith(low):
                    return full
        except OSError:
            pass
        return ""

    @staticmethod
    def _first_audio_in(folder, exts):
        """目录里的第一个音频文件。``.cue`` 优先（CD 镜像），``.m3u`` 跳过。"""
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return ""
        for fn in names:
            if fn.lower().endswith(".cue"):
                return os.path.join(folder, fn)
        for fn in names:
            if os.path.splitext(fn)[1].lower() in exts:
                return os.path.join(folder, fn)
        return ""

    def _start_load(self, path, track):
        # 本工具解不了的格式（N64 的 .miniusf、NDS 的 .2sf…）→ 交给 foobar2000
        if os.path.splitext(path)[1].lower() not in SELF_PLAYABLE_EXTS:
            open_with_foobar(path, self._log)
            return
        if self.player is None or self._loading:
            return
        self._loading = True
        self._play_path = path
        self._play_track = int(track)
        try:
            import player as _pm
            self._track_list = _pm.AudioClip.list_tracks(path) or [(0, "整首")]
        except Exception:                                    # noqa: BLE001
            self._track_list = [(0, "整首")]
        self.var_pname.set("解码中… %s" % os.path.basename(path))
        self.btn_play["state"] = "disabled"
        # 解码期间禁掉换轨：连点会让 _start_load 反复早退，看起来像「点了没反应」
        self.btn_prev["state"] = "disabled"
        self.btn_next["state"] = "disabled"
        threading.Thread(target=self._load_bg, args=(path, int(track)),
                         daemon=True).start()

    def _load_bg(self, path, track):
        """工作线程：只解码 + 投消息，绝不碰 Tk。"""
        try:
            import player as _pm
            ext = os.path.splitext(path)[1].lower()
            if ext in (".cue",) + core.DISC_IMG_EXTS:
                clip = _pm.AudioClip.from_cue(path, track)
            elif ext in (".wav", ".mp3", ".flac", ".ogg", ".m4a"):
                clip = _pm.AudioClip.from_audio_file(path)
            else:
                clip = _pm.AudioClip.from_chip(path, track)
            self.q.put(("play_ready", clip))
        except Exception as e:                               # noqa: BLE001
            self.q.put(("play_error", str(e)))

    def _on_play_ready(self, clip):
        self._loading = False
        if self.player is None:
            return
        try:
            self.player.load(clip)
            self.player.play()
        except Exception as e:                               # noqa: BLE001
            self._log("❌ 试听失败：%s" % e)
            self.var_pname.set("试听失败")
            return
        self._refresh_player_ui()
        self._start_player_poll()
        self._log("▶ 试听 %s（%s，%s）" % (clip.name, clip.detail,
                                          self._fmt_time(clip.secs)))

    def _on_play_error(self, text):
        self._loading = False
        self.var_pname.set("试听失败")
        self.btn_play["state"] = "disabled"
        self._log("❌ 试听失败：" + text)

    def _toggle_play(self):
        pl = self.player
        if pl is None or pl.clip is None:
            return
        try:
            if pl.is_active():
                if pl.is_paused():
                    pl.resume()
                else:
                    pl.pause()
            else:
                pl.play()
        except Exception as e:                               # noqa: BLE001
            self._log("❌ 播放失败：%s" % e)
            return
        self._refresh_player_ui()
        self._start_player_poll()

    def _on_volume(self, val):
        """音量滑块（0~100）—— 下载窗版本。"""
        try:
            v = float(val)
        except (TypeError, ValueError):
            return
        if self.player is not None:
            self.player.set_volume(v / 100.0)

    def _stop_play(self):
        if self.player is not None:
            self.player.stop()
        self._refresh_player_ui()

    def _on_prog_click(self, ev):
        pl = self.player
        if pl is None or not pl.is_active() or pl.duration() <= 0:
            return
        try:
            w = max(1, self.play_prog.winfo_width())
        except tk.TclError:
            return
        frac = min(1.0, max(0.0, (ev.x - 2) / float(w - 4)))
        pl.seek(frac * pl.duration())
        self._refresh_player_ui()

    def _switch_track(self, delta):
        if not self._play_path or len(self._track_list) <= 1:
            return
        n = len(self._track_list)
        self._start_load(self._play_path, (self._play_track + delta) % n)

    def _start_player_poll(self):
        if self._after_player is not None or self._closed:
            return
        self._after_player = self.after(200, self._poll_player)

    def _poll_player(self):
        self._after_player = None
        pl = self.player
        if pl is None or self._closed:
            return
        if not pl.is_active():
            if pl.has_finished():
                self._refresh_player_ui()
                pl.stop()
                self._refresh_player_ui()
            return
        self._refresh_player_ui()
        self._start_player_poll()

    def _refresh_player_ui(self):
        pl = self.player
        if pl is None:
            return
        clip = pl.clip
        dur = pl.duration()
        pos = pl.position()
        try:
            self.play_prog["value"] = (max(0, min(1000, int(pos / dur * 1000)))
                                       if dur > 0 else 0)
            self.var_ptime.set("%s/%s" % (self._fmt_time(pos),
                                          self._fmt_time(dur)))
            self.btn_play["state"] = "normal" if clip is not None else "disabled"
            self.btn_pstop["state"] = "normal" if pl.is_active() else "disabled"
            self.btn_play["text"] = "⏸" if pl.is_playing() else "▶"
            if clip is not None:
                nm = clip.name or ""
                if len(nm) > 26:
                    nm = nm[:25] + "…"
                self.var_pname.set("%s · %s" % (nm, clip.detail))
            # 换轨只用「多轨 + 正在播」两个条件（与主窗同口径）：
            # 没在播时按 ◀▶ 没有意义，且 _start_load 会再解一遍。
            multi = len(self._track_list) > 1 and pl.is_active()
            self.btn_prev["state"] = "normal" if multi else "disabled"
            self.btn_next["state"] = "normal" if multi else "disabled"
        except tk.TclError:
            pass

    def _log(self, text):
        """追加一行日志(只读 Text)。窗口已销毁时静默返回。"""
        if self._closed:
            return
        try:
            self.logw.config(state="normal")
            self.logw.insert("end", text + "\n")
            self.logw.see("end")
            self.logw.config(state="disabled")
        except tk.TclError:
            pass

    def _status(self, text):
        dbg(text)
        if self._closed:
            return
        try:
            self.var_status.set(text)
        except tk.TclError:
            pass

    # ---- v4.0: 自动转 WAV 的门禁 / 体量预估 / 入队 ------------------------
    def _chip_ready(self):
        """芯片解码链路是否就绪(chip_formats + chip_decode)。"""
        return bool(chip_formats is not None and chip_decode is not None
                    and Chip.ready())

    def _on_auto_extract_toggle(self, *_a):
        """「自动解压」变化 -> 重新算门禁(芯片包需要它才拿得到源文件)。"""
        self._update_gate_hint()

    def _on_auto_wav_toggle(self, *_a):
        """「自动转 WAV」变化 -> 门禁 + 体量预估 + 一次性说明日志。"""
        self._update_gate_hint()
        self._schedule_estimate()
        if self._closed:
            return
        try:
            if self.var_auto_wav.get() and dl_convert is not None:
                self._log(f"🎵 已开启「下载后自动转 WAV」：输出到 "
                          f"{self._wav_dir()}，源文件与 .7z 保留")
        except tk.TclError:
            pass

    def _on_merge_toggle(self, *_a):
        """「多轨合并成单文件」变化 -> 门禁 + 一条说明日志（v4.24）。

        合并产物与分轨**并存**（用户拍板"保留分轨"），所以这里要把命名规则
        和间隔说清楚，免得用户以为原文件被替换掉了。
        """
        try:
            if self.var_merge.get():
                self._log("🔗 已开启「多轨合并成单文件」：每个音源除分轨外，"
                          "另出一个「源名（合并）.wav」（曲间 0.5 秒静音，"
                          "分轨全部保留）")
        except tk.TclError:
            pass
        self._update_gate_hint()

    def _chip_blocked(self):
        """芯片包路径是否被门禁挡住。返回 ``(blocked, reason)``。

        ============================ 门禁语义（最易踩）============================
        * **自动解压关闭** 只挡**芯片包**（要解压才拿得到 .nsf/.gbs 等）；
          SPC 是裸文件、不经解压 -> **不挡**（FR-C1.2 / AC-C23）。
        * **chip_decode 缺失** 同样只挡芯片包；SPC 走 ``policy="id666"``，
          只依赖 spc_wav/msu1_to_cd -> **仍可转**（FR-C1.3 / AC-C11）。
        * 只有 ``dl_convert`` 本身缺失才真的什么都转不了（那种情况直接禁用）。
        """
        if dl_convert is None:
            return True, ("⚠ 转 WAV 模块未就绪（缺少 dl_convert.py），"
                          "下载与解压不受影响")
        if not self._chip_ready():
            return True, ("⚠ 芯片解码模块未就绪，无法转芯片包"
                          "（下载与解压不受影响）")
        try:
            if not self.var_auto_extract.get():
                return True, ("⚠ 需先勾选「下载后自动解压」才能转芯片包"
                              "（可先下载后手动解压再转；SPC 不受此限制）")
        except tk.TclError:
            return True, "⚠ 无法读取「下载后自动解压」开关"
        return False, ""

    def _update_gate_hint(self):
        """门禁置灰 + 提示。

        ⚠ **只在 ``dl_convert`` 整个缺失时禁用复选框**。
        「自动解压关」「chip_decode 缺」这两条**只提示不禁用** ——
        因为它们只影响芯片包，而 SPC 是裸文件仍要能转（AC-C11/AC-C23）。
        一把禁掉会让 SNES 用户以为整个功能坏了。
        """
        if self._closed:
            return
        try:
            want = bool(self.var_auto_wav.get())
        except tk.TclError:
            return
        blocked, reason = self._chip_blocked()
        try:
            # 运行中一律锁住(FR-C1.4); 空闲时只有 dl_convert 缺失才禁用。
            # ⚠ **判据用 _cv_busy() 而不是 _downloading** —— 转换排空期
            #   「下载已结束但转换在跑」时开关同样必须锁住（B17 第3 条）。
            if dl_convert is None or self._downloading or self._cv_busy():
                state = "disabled"
            else:
                state = "normal"
            self.var_auto_wav_cb.config(state=state)
            # v4.24: 合并没有「自动转 WAV」就没有意义（它合的是**本轮转出来**
            # 的分轨）。这里只置灰不擅改用户勾选 —— 用户先勾合并再去勾转 WAV
            # 也能用，只是暂时点不动。
            try:
                self.var_merge_cb.config(
                    state="normal" if (state == "normal" and want)
                    else "disabled")
            except tk.TclError:
                pass
        except tk.TclError:
            pass
        if blocked and want:
            self._log(f"{reason}；SPC（SNES 裸文件）不受影响，仍会转 WAV")
        if not want:
            try:
                self.var_wav_estimate.set("—")
            except tk.TclError:
                pass

    def _wav_dir(self):
        """WAV 输出目录 = ``<_dest_root()>/WAV``（**必须**用 ``_dest_root``）。

        ``_dest_root()`` 已含机种层；用 ``var_dest.get()`` 会落成
        ``SNES/WAV`` 与 ``SNES-SNES/WAV`` 两种不一致的路径（PRD B10 / AC-C21）。
        """
        return os.path.join(self._dest_root(), "WAV")

    def _schedule_estimate(self, *_a):
        """勾选/选中变化 -> 防抖 >=220ms 后起后台线程预估（FR-C2.4）。"""
        if self._closed:
            return
        if self._after_est is not None:
            try:
                self.after_cancel(self._after_est)
            except tk.TclError:
                pass
            self._after_est = None
        self._after_est = self.after(220, self._run_estimate)

    def _estimate_paths(self):
        """当前已选条目对应的「待转源文件」列表（供预估用）。

        ⚠ 只做 ``os.path`` 拼接与目录列举，**不做 probe**。
        probe 全在 dl_convert 的后台线程里，否则上百文件会卡界面（R10）。
        """
        if dl_convert is None:
            return []
        out = []
        dest_root = self._dest_root()
        for e in self.entries:
            if e["url"] not in self.sel:
                continue
            target = os.path.join(dest_root, self._file_name(e["name"]))
            out.append(target)
            if joshw_dl is None or not joshw_dl.is_archive(target):
                continue
            try:
                d = joshw_dl.extract_dir_for(target, dest_root)
                if not os.path.isdir(d):
                    continue
                for name in os.listdir(d):
                    p = os.path.join(d, name)
                    if os.path.isfile(p) and dl_convert.is_convertible(p):
                        out.append(p)
            except OSError:
                continue
        # v6.0.6：去掉「音源转出来的 WAV」（``xxx.fsb`` + ``xxx.fsb.wav``），
        # 否则估算和入队都会把同一首算两遍。压缩包（.7z）不受影响。
        return dl_convert.drop_derived_wavs(out)

    def _run_estimate(self):
        """起后台线程跑 ``dl_convert.probe_worker``；**只发事件，不碰 Tk**。"""
        self._after_est = None
        if self._closed:
            return
        try:
            if not self.var_auto_wav.get():
                return
        except tk.TclError:
            return
        self._est_token += 1
        tok = self._est_token
        paths = self._estimate_paths()
        threading.Thread(
            target=dl_convert.probe_worker,
            args=(paths, 150.0, self._cv_emit, tok),
            name="dl-estimate", daemon=True).start()

    @staticmethod
    def _human(n):
        """字节 -> 人类可读, 优先用核心模块的实现以保持全程序一致。"""
        if jw is not None:
            try:
                return jw.human_size(int(n or 0))
            except Exception:                        # noqa: BLE001
                pass                                 # 退化成下面这份实现
        val = float(n or 0)
        for unit in ("B", "KB", "MB"):
            if val < 1024.0:
                return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
            val /= 1024.0
        return f"{val:.1f} GB"

    @staticmethod
    def _human_size_cell(size):
        """列表「大小」列文本: 0 字节显示为「—」(站点未给大小), 而不是「0 B」。"""
        try:
            value = int(size or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            return "—"
        return DownloadDialog._human(value)

    @staticmethod
    def _stamp_day(ts):
        """快照/缓存时间戳 -> ``YYYY-MM-DD``; 非法或缺失时返回「日期未知」。"""
        try:
            value = float(ts)
        except (TypeError, ValueError):
            return "日期未知"
        if value <= 0:
            return "日期未知"
        try:
            return time.strftime("%Y-%m-%d", time.localtime(value))
        except (OSError, ValueError, OverflowError):
            return "日期未知"

    @staticmethod
    def _size_matches(actual, expect):
        """落盘大小是否与索引大小一致。

        Apache autoindex 的 Size 列是四舍五入过的 (如 3.3K 实为 3365 字节),
        严格 == 会让「已存在」永远判不掉, 每次重复下载。给 3% / 2KB 的容差。
        """
        try:
            actual = int(actual)
            expect = int(expect)
        except (TypeError, ValueError):
            return False
        if expect <= 0:
            return actual > 0
        if actual == expect:
            return True
        return abs(actual - expect) <= max(2048, expect * 0.03)

    @staticmethod
    def _plat_folder(name):
        """机种显示名 -> 目录名。'SNES / SFC' 落成 'SNES-SFC' 而不是 'SNES _ SFC'。"""
        s = re.sub(r"\s*/\s*", "-", str(name or "JoshW"))
        s = re.sub(r"\s*&\s*", "+", s)
        return sanitize_name(s)

    @staticmethod
    def _file_name(name):
        """条目名 -> 落盘文件名。

        优先借 joshw_dl.sanitize_filename: Downloader.add() 内部用的就是它,
        两边规则一致才能保证「已存在则跳过」判断的路径与真实落盘路径对得上
        (它会去尾部点/空格、截断到 120 字符、规避 CON/NUL 等设备名)。
        """
        raw = str(name or "download")
        if not raw.lower().endswith(".7z"):
            raw += ".7z"
        if joshw_dl is not None:
            try:
                return joshw_dl.sanitize_filename(raw)
            except Exception:                        # noqa: BLE001
                pass                                 # 退化成下面这份实现
        return sanitize_name(raw)

    def _default_dest(self, name):
        base = os.path.join(os.path.expanduser("~"), "Music", "JoshW")
        return os.path.join(base, self._plat_folder(name)) if name else base

    def _dest_root(self):
        """实际落盘根目录。

        默认保存目录本身已带机种名(…/Music/JoshW/SNES), 此时不再重复追加一层,
        避免出现 SNES/SNES/xxx.7z; 用户手改成别的目录时才按机种建子目录。
        """
        base = (self.var_dest.get() or "").strip() or self._default_dest(
            self.cur_name)
        plat = self._plat_folder(self.cur_name or "JoshW")
        tail = os.path.basename(os.path.normpath(base))
        if tail.lower() == plat.lower():
            return os.path.normpath(base)
        return os.path.normpath(os.path.join(base, plat))

    # ---- 机种树 -------------------------------------------------------------
    def _load_platforms(self):
        tv = self.tv_plat
        try:
            tv.delete(*tv.get_children())
        except tk.TclError:
            return
        self._platform_iid = {}
        if jw is None:
            self._status("音源模块未就绪 — 无法列出机种")
            return
        try:
            plats = list(jw.PLATFORMS)
        except Exception as e:                       # noqa: BLE001
            self._log(f"❌ 读取机种列表失败: {e}")
            self._status("读取机种列表失败")
            return

        groups = {}
        for p in plats:
            if not isinstance(p, dict) or not p.get("key"):
                continue
            groups.setdefault(p.get("group") or "其他", []).append(p)

        order = [g for g in self.GROUP_ORDER if g in groups]
        order += sorted(g for g in groups if g not in self.GROUP_ORDER)
        for g in order:
            gid = f"g:{g}"
            try:
                tv.insert("", "end", iid=gid,
                          text=f"{g}（{len(groups[g])}）", tags=("grp",))
            except tk.TclError:
                continue
            for p in groups[g]:
                key = str(p.get("key"))
                try:
                    tv.insert(gid, "end", iid=f"p:{key}",
                              text=p.get("name") or key,
                              # 主格式由 joshw_source.PLATFORMS 提供, 这里只读不维护
                              # 第二份映射; 读不到就显示「—」而不是空白
                              values=((p.get("fmt") or "—"),))
                except tk.TclError:
                    continue
                self._platform_iid[key] = f"p:{key}"

        self._status(f"共 {len(self._platform_iid)} 个机种 — 请在左侧选择")

    def _on_platform_select(self, _evt=None):
        try:
            sel = self.tv_plat.selection()
        except tk.TclError:
            return
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("g:"):                    # 分组行不可选
            return
        key = iid[2:] if iid.startswith("p:") else iid
        if key and key != self.cur_key:
            self._load_platform(key)

    # ---- 载入一个机种 -------------------------------------------------------
    def _load_platform(self, key, force_scan=False):
        """切换机种: 本地缓存 -> 内置索引快照 -> 后台联网扫描。

        force_scan=True 时跳过前两级直接联网（「🗑 清除缓存」用）。
        """
        meta = {}
        if jw is not None:
            try:
                meta = jw.platform(key) or {}
            except Exception:                        # noqa: BLE001
                meta = {}
        self._scan_tok += 1
        tok = self._scan_tok

        # 让上一个机种的扫描线程尽快退出, 并换一个新的事件
        try:
            self._cancel_scan.set()
        except Exception:                            # noqa: BLE001
            pass
        self._cancel_scan = threading.Event()
        cancel = self._cancel_scan

        self.cur_key = key
        self.cur_name = str(meta.get("name") or key)
        self.entries = []
        self.visible = []
        self.sel = set()
        self._states = {}
        self.iid_of = {}
        try:
            self.tree.delete(*self.tree.get_children())
        except tk.TclError:
            return
        if self.var_search.get():
            self.var_search.set("")                  # 触发一次过滤刷新
        self._update_count()
        if self._dest_auto:
            self._set_dest(self._default_dest(self.cur_name))

        # ---- 1) 本地缓存 / 2) 内置索引快照 ----------------------------------
        # best_index 的顺序: 新鲜的本地缓存 -> 内置快照（不看 TTL）-> none
        items = None
        stamp = 0.0
        source = "none"
        if jw is not None and not force_scan:
            try:
                items, stamp, source = jw.best_index(key)
            except Exception as e:                   # noqa: BLE001
                items, stamp, source = None, 0.0, "none"
                self._log(f"⚠ 读取索引失败（改为重新扫描）: {e}")
        if source == "cache":
            self._status(f"{self.cur_name} — 正在读取缓存…")
            self._apply_entries(items, from_cache=True)
            return
        if source == "bundled":
            day = self._stamp_day(stamp)
            self._log(f"ℹ 使用内置索引（快照日期 {day}），"
                      f"如需最新列表点「🗑 清除缓存」重扫")
            self._apply_entries(items, tag=f"（内置索引 {day}）")
            return

        # ---- 3) 都没有: 后台联网扫描 ----------------------------------------
        self._scanning = True
        self._sync_buttons()
        try:
            self.progress["value"] = 0
        except tk.TclError:
            pass
        self._status(f"扫描中 0/27 页… ({self.cur_name})")
        if force_scan:
            self._log(f"▶ 重新扫描 {key} — {meta.get('host') or key}，约 30 秒")
        else:
            self._log(f"▶ 扫描 {key} — {meta.get('host') or key}，首次约 30 秒")
        threading.Thread(target=self._scan_thread, args=(key, tok, cancel),
                         daemon=True).start()

    def _scan_thread(self, key, tok, cancel):
        """工作线程: 只跑网络扫描, 只往队列塞事件, 不碰 Tk。"""
        def progress(done, total, msg=""):
            if self._closed:
                return
            try:
                self.q.put(("scan_progress", tok, int(done), int(total), str(msg)))
            except Exception:                        # noqa: BLE001
                pass

        try:
            items = jw.scan_platform(key, progress=progress, cancel=cancel)
        except Exception as e:                       # noqa: BLE001
            if self._closed:
                return
            try:
                self.q.put(("scan_error", tok, f"{type(e).__name__}: {e}"))
            except Exception:                        # noqa: BLE001
                pass
            return
        if self._closed:
            return
        try:
            self.q.put(("scan_done", tok, items))
        except Exception:                            # noqa: BLE001
            pass

    def _apply_entries(self, items, from_cache=False, tag=None):
        """把条目列表落到界面 (主线程)。字段与 joshw_source 的契约一致。

        Args:
            items: 条目列表 (name/url/size/mtime/letter)。
            from_cache: 数据来自本地缓存时置 True, 状态行后缀「（缓存）」。
            tag: 显式指定状态行后缀 (如「（内置索引 2026-10-18）」);
                 ``None`` 时按 ``from_cache`` 推断。
        """
        norm = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            url = str(it.get("url") or "")
            if not url:
                continue
            name = str(it.get("name") or os.path.basename(url) or url)
            try:
                size = int(it.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            norm.append({"name": name, "url": url, "size": max(0, size),
                         "mtime": str(it.get("mtime") or ""),
                         "letter": str(it.get("letter") or "")})
        self.entries = norm
        self._scanning = False
        self._render_rows()
        total = sum(e["size"] for e in norm)
        # 扫描完成不置 100: 进度条只在"正在下载"时才有意义, 空闲时保持 0,
        # 否则满格青色容易被读成"已经下完了"。
        try:
            self.progress["value"] = 0
        except tk.TclError:
            pass
        if tag is not None:
            tail = tag
        else:
            tail = "（缓存）" if from_cache else ""
        self._status(f"{self.cur_key} · {len(norm):,} 条 · "
                     f"{self._human(total)}{tail}")
        self._log_zh_coverage()
        self._log(f"✅ {self.cur_name}: {len(norm):,} 条 / "
                  f"{self._human(total)}{tail}")
        self._sync_buttons()

    # ---- 游戏列表 -----------------------------------------------------------
    def _schedule_filter(self):
        """搜索框防抖: 每敲一个字就重建 4000 行会卡, 合并到 220ms 后一次。"""
        if self._closed:
            return
        if self._after_filter is not None:
            try:
                self.after_cancel(self._after_filter)
            except Exception:                        # noqa: BLE001
                pass
            self._after_filter = None
        try:
            self._after_filter = self.after(220, self._run_filter)
        except tk.TclError:
            self._after_filter = None

    def _run_filter(self):
        self._after_filter = None
        self._render_rows()

    def _refresh_disk(self):
        """扫一遍保存目录, 记住已存在文件的文件名与大小。

        只扫一层 (下载目录是平铺的), 4000 条以内一次 scandir 足够快;
        结果用于列表里的「✓ 已下载」标记和下载前的跳过判断。
        """
        root = self._dest_root() if self.cur_key else ""
        disk = {}
        if root and os.path.isdir(root):
            try:
                with os.scandir(root) as it:
                    for de in it:
                        try:
                            if de.is_file() and not de.name.endswith(".part"):
                                disk[de.name] = de.stat().st_size
                        except OSError:
                            pass
                # 下载的 .7z 会解到 <包名>/ 子目录里，光扫一层看不到产物，
                # 列表上的「✓ 已下载」就不会亮。再进一层把产物也收进来(深度限 2，
                # 避免大目录树拖慢刷新)。产物文件名与索引里的名字不同,
                # 所以同时按"去掉扩展名"登记一份，供 _render_rows 的近似匹配使用。
                try:
                    with os.scandir(root) as it:
                        for de in it:
                            if not de.is_dir():
                                continue
                            sub = os.path.join(root, de.name)
                            with os.scandir(sub) as it2:
                                for f in it2:
                                    try:
                                        if f.is_file() and not f.name.endswith(".part"):
                                            disk.setdefault(f.name, f.stat().st_size)
                                            stem = os.path.splitext(f.name)[0]
                                            disk.setdefault(stem, f.stat().st_size)
                                    except OSError:
                                        pass
                except OSError:
                    pass
            except OSError:
                pass
        self._disk = disk
        self._disk_root = root

    # ---- 联网补中文名(谷歌翻译) --------------------------------------------
    ZH_LIMIT = 3000

    def _zh_pending(self):
        """当前机种还没有中文名的标题本体(去重, 保序)。"""
        seen, out = set(), []
        for e in self.entries:
            if zh_titles.zh_name(e["name"]):
                continue
            core = zh_titles.core_title(e["name"])
            key = core.lower()
            if not core or key in seen:
                continue
            seen.add(key)
            out.append(core)
        return out

    def _toggle_zh_translate(self):
        if self._zh_running:
            self._zh_cancel.set()
            self._status("正在停止翻译…")
            return
        if self._scanning or self._downloading:
            self._status("扫描/下载中，稍后再翻译")
            return
        pending = self._zh_pending()
        if not pending:
            self._log("ℹ 这个机种的条目都已有中文名")
            return
        limit = min(len(pending), self.ZH_LIMIT)
        try:
            ok = messagebox.askyesno(
                "翻译未收录条目",
                f"当前机种有 {len(pending):,} 条没有中文名。\n\n"
                f"本次联网（谷歌翻译）翻前 {limit:,} 条，"
                f"大约 {limit // 12 * 2} 秒。\n"
                "结果写入 zh_titles_user.json，以后直接生效。\n\n"
                "现在开始？",
                parent=self)
        except tk.TclError:
            ok = False
        if not ok:
            return
        self._zh_cancel.clear()
        self._zh_running = True
        self.btn_zh.configure(text="停止翻译")
        threading.Thread(target=self._zh_worker, args=(pending[:limit],),
                         daemon=True).start()

    def _zh_worker(self, titles):
        sink = {}
        err = None
        try:
            zh_online.translate_titles(
                titles,
                on_progress=lambda d, t: self.q.put(("zh_progress", d, t)),
                cancel=self._zh_cancel, sink=sink)
        except Exception as exc:                       # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        self.q.put(("zh_done", sink, err))

    def _toggle_maximize(self):
        """窗口内最大化 / 还原。标题栏的系统按钮同样可用，两条路等价。"""
        try:
            if str(self.state()) == "zoomed":
                self.state("normal")
                if self._pre_zoom:
                    self.geometry(self._pre_zoom)
                self._zoomed = False
            else:
                self._pre_zoom = self.geometry()
                self.state("zoomed")
                self._zoomed = True
        except tk.TclError:
            return
        try:
            self.btn_zoom.configure(text="❐" if self._zoomed else "⛶")
        except tk.TclError:
            pass

    def _on_escape(self, _event=None):
        """Esc: 最大化时先还原, 否则关闭下载窗口。"""
        if self._zoomed:
            self._toggle_maximize()
            return
        self._on_close()

    def _remember_cols(self, _event=None):
        """拖动列分隔线后把当前宽度记下来(持久化), 不再被程序改回去。"""
        try:
            widths = {c: int(self.tree.column(c, "width")) for c in _COL_KEYS}
        except tk.TclError:
            return
        if widths != self._last_cols:
            self._last_cols = widths
            # 一旦用户动过宽度就全列固定, 不再让 Tk 替我们"自作主张"伸缩
            for _c, _st in (("name", False), ("zh", False), ("mtime", False),
                            ("size", False), ("state", False)):
                try:
                    self.tree.column(_c, stretch=_st)
                except tk.TclError:
                    pass
            save_col_widths(widths)

    def _log_zh_coverage(self):
        """每个机种只提示一次中文名覆盖率, 让用户知道「—」不是 bug。"""
        key = self.cur_key
        if key in self._zh_logged:
            return
        n = len(self.entries)
        self._zh_logged.add(key)
        if not n:
            return
        hit = sum(1 for e in self.entries if zh_titles.zh_name(e["name"]))
        weak = sum(1 for e in self.entries if zh_titles.zh_weak(e["name"])
                   and zh_titles.zh_name(e["name"]))
        self._log(f"ℹ 中文名覆盖 {hit:,}/{n:,}（{hit * 100.0 / n:.0f}%），"
                  f"其中 {weak:,} 条是机器直译、压暗显示，建议按需人工修正")

    def _render_rows(self):
        """按搜索词重建游戏列表, 并恢复仍然可见的选中项。"""
        if self._closed:
            return
        root = self._dest_root() if self.cur_key else ""
        if root != self._disk_root:
            self._refresh_disk()
        kw = self.var_search.get().strip().lower()
        items = self.entries
        if kw:
            # 英文原名和中文名都能搜到
            items = [e for e in items
                     if kw in e["name"].lower()
                     or kw in zh_titles.zh_name(e["name"]).lower()]
        self.visible = items
        shown = items[:self.MAX_ROWS]
        try:
            self.tree.delete(*self.tree.get_children())
        except tk.TclError:
            return
        self.iid_of = {}
        self._entry_of_iid = {}
        for e in shown:
            state = self._states.get(e["url"], "")
            if not state and self._disk:
                sz = self._disk.get(self._file_name(e["name"]))
                if sz is not None and self._size_matches(sz, e["size"]):
                    state = "✓ 已下载"
                    self._states[e["url"]] = state
            try:
                tags = ("weak",) if zh_titles.zh_weak(e["name"]) else ()
                iid = self.tree.insert(
                    "", "end",
                    values=("▶", e["name"], zh_titles.zh_or_dash(e["name"]),
                            e["mtime"], self._human_size_cell(e["size"]), state),
                    tags=tags)
            except tk.TclError:
                return
            self.iid_of[e["url"]] = iid
            self._entry_of_iid[iid] = e          # 试听时从 iid 反查条目
        want = [self.iid_of[u] for u in self.sel if u in self.iid_of]
        if want:
            try:
                self.tree.selection_set(want)
            except tk.TclError:
                pass
        self._update_count()

    def _update_count(self):
        n = len(self.entries)
        txt = f"共 {n:,} 条，已选 {len(self.sel):,} 条"
        if len(self.visible) != n:
            txt += f"（显示 {len(self.visible):,} 条）"
        if len(self.visible) > self.MAX_ROWS:
            txt += f"，仅列前 {self.MAX_ROWS:,} 行"
        try:
            self.var_count.set(txt)
        except tk.TclError:
            pass

    def _on_row_select(self, _evt=None):
        """用户改选择: 只重算可见行的增删, 隐藏行的选择保留。"""
        try:
            chosen = set(self.tree.selection())
        except tk.TclError:
            return
        visible = set(self.iid_of)
        now = {u for u, iid in self.iid_of.items() if iid in chosen}
        self.sel = (self.sel - visible) | now
        self._update_count()

    def _render_selection(self):
        try:
            self.tree.selection_set([self.iid_of[u] for u in self.sel
                                     if u in self.iid_of])
        except tk.TclError:
            pass
        self._update_count()

    def _select_all(self):
        self.sel |= set(self.iid_of)
        self._render_selection()

    def _invert_selection(self):
        visible = set(self.iid_of)
        self.sel = (self.sel - visible) | (visible - self.sel)
        self._render_selection()

    def _on_row_double(self, evt=None):
        """双击某行 = 只下这一项。"""
        try:
            iid = self.tree.identify_row(evt.y) if evt is not None else self.tree.focus()
        except tk.TclError:
            return
        if not iid:
            return
        url = next((u for u, i in self.iid_of.items() if i == iid), None)
        if not url:
            return
        self.sel = {url}
        self._render_selection()
        self._start_download()

    # ---- 保存目录 -----------------------------------------------------------
    def _on_dest_change(self, *_a):
        if self._guard_dest:
            return
        self._dest_auto = False       # 用户手改过, 切机种时不再覆盖

    def _set_dest(self, path):
        self._guard_dest = True
        try:
            self.var_dest.set(path)
        except tk.TclError:
            pass
        finally:
            self._guard_dest = False

    def _pick_dest(self):
        d = filedialog.askdirectory(title="选择保存目录", parent=self)
        if d:
            self._set_dest(os.path.normpath(d))
            self._dest_auto = False

    def _open_dir(self):
        d = self._dest_root() if self.cur_key else (
            (self.var_dest.get() or "").strip())
        if d and os.path.isdir(d):
            try:
                os.startfile(d)  # noqa: S606  Windows
            except Exception as e:                   # noqa: BLE001
                messagebox.showinfo("提示", f"打不开目录:\n{d}\n{e}", parent=self)
        else:
            messagebox.showinfo("提示", f"目录还不存在:\n{d}", parent=self)

    # ---- 云盘上传（v4.23）--------------------------------------------------
    def _cloud_wav_files(self):
        """要上传的 WAV 与所在目录。

        优先**当前机种**的 WAV 目录（``<保存目录>/<机种>/WAV``）—— 用户眼前
        看到的就是这个机种；只有在它为空、而本轮冻结目录（``_cv_wav_dir``，
        下载途中改过保存目录才会不同）里有东西时才回退，免得刚转完切个机种
        就"找不到文件"。
        """
        cur = self._wav_dir()
        wavs = collect_wavs(cur)
        if wavs:
            return wavs, cur
        frozen = self._cv_wav_dir
        if frozen and os.path.normpath(frozen) != os.path.normpath(cur):
            wavs = collect_wavs(frozen)
            if wavs:
                return wavs, frozen
        return [], cur

    def upload_cloud(self):
        """把下载窗转出来的 WAV 传到网易云云盘。

        目录结构与主窗不同：这里 WAV 平铺在 ``<保存目录>/<机种>/WAV``，
        所以「要传哪些」由 ``_cloud_wav_files()`` 决定，其余（登录 / 确认 /
        逐首上传 / 阶段日志）与主窗同一套代码。
        """
        if ncm is None:
            messagebox.showerror("缺少依赖", _CLOUD_MISSING_MSG, parent=self)
            return
        if self._cloud_busy:
            messagebox.showinfo("提示", "上一次上传还在跑，等它结束再来",
                                parent=self)
            return
        wavs, folder = self._cloud_wav_files()
        if not wavs:
            messagebox.showinfo(
                "提示",
                "这个机种的 WAV 目录里还没有文件：\n%s\n\n"
                "请先勾选「下载后自动转 WAV」并完成一次下载转换。" % folder,
                parent=self)
            return

        # v4.25：合并文件与分轨并存时先问清楚传哪一份（内容重复，
        # 不选就会把同一张专辑传两遍）。放在登录之前，取消不白弹登录窗。
        wavs = plan_cloud_upload(self, wavs)
        if wavs is None:
            self._log("☁ 已取消上传")
            return
        if not wavs:
            return

        sess = ensure_cloud_session(self, self._log, self._status)
        if sess is None:
            return
        if not confirm_cloud_upload(self, wavs):
            return

        self._cloud_cancel.clear()               # 上一轮关窗留下的信号
        self._cloud_busy = True
        try:
            self.btn_cloud.config(state="disabled")
        except tk.TclError:
            return
        self._sync_buttons()                     # 上传期间不许再开下载
        self._log("=" * 42)
        self._log("☁ 开始上传 %d 个 WAV → 网易云云盘" % len(wavs))
        self._log("   来源: %s" % folder)
        self._status("☁ 上传中…")
        threading.Thread(target=self._cloud_worker, args=(wavs, sess),
                         daemon=True).start()

    def _cloud_worker(self, wavs, sess):
        """后台上传。**只往 self.q 塞事件**，一个 Tk 对象都不碰。"""
        CloudUploader(
            sess,
            lambda kind, payload: self.q.put(("cloud", kind, payload)),
            cancel=self._cloud_cancel,
        ).run(wavs)

    def _on_cloud_event(self, action, payload):
        """主线程侧：把上传线程的事件翻成日志 / 状态行。"""
        if self._closed:
            return
        if action == "log":
            self._log(payload)
        elif action == "prog":
            self._status("☁ 上传中 %d%%" % int(payload or 0))
        elif action == "done":
            ok, skip, fail = payload
            self._cloud_busy = False
            try:
                self.btn_cloud.config(state="normal")
            except tk.TclError:
                return
            self._sync_buttons()
            self._log("-" * 42)
            self._log("☁ 云盘上传结束：成功 %d / 跳过 %d / 失败 %d"
                      % (ok, skip, fail))
            if fail:
                self._log("   （失败的多半是版权校验拦截或网络抖动，可稍后重试）")
            self._status("☁ 上传完成：成功 %d / 跳过 %d / 失败 %d"
                         % (ok, skip, fail))
            messagebox.showinfo(
                "上传结束",
                "成功 %d 首，跳过 %d 首（云端已有），失败 %d 首。\n\n"
                "打开网易云音乐 → 我的音乐云盘 即可查看。"
                % (ok, skip, fail), parent=self)

    # ---- 下载 ---------------------------------------------------------------
    def _sync_buttons(self):
        # v4.0 (B17): 「运行中」的定义**扩到转换**。
        # v3.9 的 running 只看 _downloading, 于是转换排空期（dl 已是 None、
        # _downloading 已是 False, 但转换线程还在跑）按钮全灰 ——
        # 用户唯一的逃生出口被关掉了, 只能杀进程。
        dl_busy = bool(self._downloading)
        cv_busy = self._cv_busy()
        running = dl_busy or cv_busy
        busy = self._scanning or running or self._cloud_busy
        state = ("disabled" if (busy or jw is None or joshw_dl is None)
                 else "normal")
        try:
            self.btn_start.config(state=state)
            # ⚠ **停止按钮与三个复选框必须用不同判据**（B17 第3 条）:
            #   复选框运行中要 disabled（改了不生效反而误导）,
            #   停止按钮运行中必须保持 **normal**（用户唯一的逃生出口）。
            #   转换排空期是「下载已结束但转换在跑」, 此时按钮仍要可点。
            self.btn_stop.config(state="normal" if running else "disabled")
            self.spin_workers.config(state="disabled" if dl_busy else "normal")
            # 解压开关在下载途中锁住: 改它不会影响已经跑起来的线程,
            # 让用户以为改了就能生效反而更困惑。
            lock = "disabled" if running else "normal"
            self.var_auto_extract_cb.config(state=lock)
            self.var_del_archive_cb.config(state=lock)
            # v4.0: 第三个复选框同样锁住(FR-C1.4)。**零新增锁定机制** ——
            # 直接复用上面这个 lock, 运行中三个开关一律 disabled。
            # ⚠ 顺序: 先按 lock 锁上, 再让 _update_gate_hint 决定「要不要连
            #   dl_convert 缺失也禁用」—— 它**只在 dl_convert is None 或
            #   转换在跑时**才把 state 改成 disabled(见该方法)。
            self.var_auto_wav_cb.config(state=lock)
            self._update_gate_hint()
            # v4.23: 上传云盘与下载/转换**互斥**（同一批 WAV 正在被写时会传到
            # 半成品）。上传本身跑着时也置灰，防止重复点。
            btn_cloud = getattr(self, "btn_cloud", None)
            if btn_cloud is not None:
                btn_cloud.config(state="disabled" if busy else "normal")
        except tk.TclError:
            pass

    def _start_download(self):
        if self._downloading or self._scanning:
            return
        if joshw_dl is None:
            messagebox.showwarning("提示", "下载模块未就绪（缺少 joshw_dl.py）",
                                   parent=self)
            return
        if not self.cur_key:
            messagebox.showinfo("提示", "请先在左侧选择一个机种", parent=self)
            return
        chosen = [e for e in self.entries if e["url"] in self.sel]
        if not chosen:
            messagebox.showinfo(
                "提示", "请先选中要下载的条目（Ctrl / Shift 可多选）", parent=self)
            return

        dest_root = self._dest_root()
        try:
            os.makedirs(dest_root, exist_ok=True)
        except OSError as e:
            messagebox.showerror("无法创建目录", f"{dest_root}\n{e}", parent=self)
            return

        todo = []
        skipped = 0
        self._size_of_path = {}
        self._url_of_path = {}
        self._url_of_base = {}
        self._pending_resume = []
        # v4.0(H3/AC-C16): 开关打开时, **已下载的条目也必须能转 WAV**。
        # 原来的 `continue` 会让用户第二次点下载时一条 WAV 都转不出来
        # (第一次没开转换 / 转换失败 -> 之后永远补不上)。
        want_wav = bool(dl_convert is not None and self.var_auto_wav.get())
        for e in chosen:
            target = os.path.join(dest_root, self._file_name(e["name"]))
            key = os.path.normpath(target)
            try:
                have_archive = (e["size"] > 0 and os.path.isfile(target)
                                and self._size_matches(os.path.getsize(target),
                                                       e["size"]))
                # 解压产物也算"已就位"：用户删了 .7z(或勾了删包)、只留解压产物目录时，
                # 不该重复下载几 MB 再解一遍。规则与下载时落盘一致(见 joshw_dl.has_payload)。
                have_payload = bool(joshw_dl) and joshw_dl.has_payload(target, dest_root)
                if have_archive or have_payload:
                    skipped += 1
                    tail = ("（解压产物已在）"
                            if have_payload and not have_archive else "")
                    if not want_wav:
                        # ---- v3.9 原样, 零回归 ----
                        self._states[e["url"]] = "⏭ 已存在"
                        self._log(f"⏭ 已存在，跳过: {os.path.basename(target)}"
                                  + tail)
                        continue
                    # 开关打开: 不 continue, 落到下面入队(源文件已在盘上,
                    # 不必重下/重解)。**只进转换队列, 不进 self.dl**。
                    self._pending_resume.append((e["url"], target))
                    self._states[e["url"]] = "⏭ 已存在 → 待转"
                    self._log(f"⏭ 已存在，跳过下载: {os.path.basename(target)}"
                              + tail + "（已排入转 WAV 队列）")
                    continue
            except OSError:
                pass
            todo.append((e["url"], target, e["size"]))
            self._size_of_path[key] = e["size"]
            self._url_of_path[key] = e["url"]
            self._url_of_base[os.path.basename(key)] = e["url"]
            self._states[e["url"]] = "等待…"
        self._refresh_row_states()

        if not todo and not (want_wav and self._pending_resume):
            self._status(f"全部 {skipped} 个已存在，无需下载")
            self._log("✅ 没有需要下载的文件")
            return

        try:
            workers = int(str(self.var_workers.get()).strip())
        except (TypeError, ValueError):
            workers = 2
        workers = max(1, min(4, workers))
        try:
            auto_ex = bool(self.var_auto_extract.get())
            del_arc = bool(self.var_del_archive.get())
        except tk.TclError:
            auto_ex, del_arc = True, False
        try:
            self.dl = joshw_dl.Downloader(
                workers=workers, on_event=self._on_dl_event,
                auto_extract=auto_ex, delete_archive=del_arc,
                extract_dir=dest_root)
            for url, target, size in todo:
                self.dl.add(url, target, size)
        except Exception as e:                       # noqa: BLE001
            self.dl = None
            self._log(f"❌ 创建下载任务失败: {e}")
            messagebox.showerror("启动失败", f"无法创建下载任务:\n{e}", parent=self)
            return

        self._downloading = True
        self._dl_total = max(1, sum(s for _u, _t, s in todo)) if todo else 1
        self._dl_shown = 0.0
        self._dl_finished = 0
        self._dl_files_done = 0
        self._dl_ok = 0
        self._dl_fail = 0
        self._dl_skip = skipped
        self._dl_expect = len(todo)
        self._dl_extracted = 0
        self._dl_ex_files = 0
        self._dl_ex_failed = 0
        self._inflight = 0
        self._last_paint = 0.0

        # ---- v4.0 M9: 转 WAV 统计归零 + 建ConvertWorker ----
        # ⚠ 输出目录**在这里冻结成绝对路径**(A5): 转换期间用户切机种会改
        #   cur_name / var_dest, 但已在转的任务必须写到同一个 WAV/ 里。
        self._cv_queued = 0
        self._cv_done = 0
        self._cv_ok = 0
        self._cv_fail = 0
        self._cv_skip = 0
        self._cv_bytes = 0
        self._cv_current = None
        self._cv_src_finished = False
        self._cv_abandoned = 0
        self._cv_pkg_logged = set()
        self._cv_wav_dir = self._wav_dir()
        self._cv = None
        self._finished = False             # 每轮重置收尾守卫(AC-C13 可再次开始)
        if want_wav and dl_convert is not None:
            try:
                self._cv = self._make_convert_worker()
            except Exception as e:                   # noqa: BLE001
                self._cv = None
                self._log(f"⚠ 转 WAV 启动失败，本次只下载不解码: {e}")

        try:
            self.progress["value"] = 0
        except tk.TclError:
            pass
        if todo:
            self._status(f"下载中 0/{len(todo)} · 0 B / "
                         f"{self._human(self._dl_total)}")
            self._log(f"⬇ 开始下载 {len(todo)} 个文件（并发 {workers}）"
                      f"→ {dest_root}"
                      + (f"，跳过已存在 {skipped} 个" if skipped else ""))
        else:
            # 全部已存在但开了转 WAV: 没有下载, 状态行不能说「下载中」
            self._status(f"全部 {skipped} 个已存在，开始转 WAV…")
            self._log(f"✅ 无需下载（{skipped} 个已存在），"
                      f"开始转换已有文件为 WAV")
        if auto_ex:
            if del_arc:
                self._log("📦 下载完成后自动解压，并删除原 .7z 包")
            else:
                self._log("📦 下载完成后自动解压（原 .7z 包保留，"
                          "可勾选上方选项自动删除）")
        else:
            self._log("📦 自动解压已关闭，下载后请手动解压")
        # ---- v4.0 M10: 转 WAV 计划行（FR-C10.3 / UI-C5）----
        if self._cv is not None:
            self._log(f"🎵 下载后自动转 WAV 已开启：输出 {self._cv_wav_dir}"
                      f"（平铺，编号跨包连续）；源文件与 .7z 保持原样")
            if self._pending_resume:
                self._log(f"   · {len(self._pending_resume)} 个条目已存在，"
                          f"直接转不重下")
        self._sync_buttons()

        try:
            self.dl.start()
        except Exception as e:                       # noqa: BLE001
            self._downloading = False
            self.dl = None
            self._log(f"❌ 下载线程启动失败: {e}")
            self._status(f"下载线程启动失败：{e}")
            # v4.0: 下载起不来时**必须**把转换线程也收掉, 否则它会永远
            # 等「源已结束」(all_done 永远不会来) 而悬着不退出。
            if self._cv is not None:
                try:
                    self._cv.mark_src_finished()
                    self._cv.cancel()
                except Exception:                    # noqa: BLE001
                    pass
                self._cv = None
            self._sync_buttons()
            return
        # ---- v4.0: 把「已存在」条目的源文件排进转换队列 ----
        # ⚠ 必须在 dl.start() **之后** —— 万一没有待下载项, Downloader.start()
        #   立刻结束并由 supervisor 发 all_done, 我们才能保证 all_done 一定
        #   在这些 resume 项入队**之后**才到（否则 _src_finished 会过早置位）。
        if self._cv is not None:
            self._enqueue_pending_resume()

    def _make_convert_worker(self):
        """建ConvertWorker（**只在开关打开且 dl_convert 可用时调**）。

        cancel Event **必须与 :class:`joshw_dl.Downloader` 共用同一个对象**
        （PRD FR-C9.1 / R0b）：这样「■ 停止」的 ``self.dl.cancel()`` 一句话就
        同时中止下载、解压、转换三段，无需改动 :meth:`_stop_download` 的下载部分。
        """
        cancel = getattr(self.dl, "_cancel_event", None)
        if not isinstance(cancel, threading.Event):
            # Downloader 不可用/结构变了 -> 自建一个, 至少转换能停
            cancel = threading.Event()
        worker = dl_convert.ConvertWorker(
            self._cv_wav_dir, self._cv_emit, cancel,
            merge_wav=bool(self.var_merge.get()))
        self._cv_cancel = cancel
        worker.start()
        return worker

    def _enqueue_pending_resume(self):
        """把「已下载但未转过」的条目排进转换队列（只转换，不重下）。

        ============================ R11: 必须按目录去重 ============================
        ``joshw_dl.payload_files`` 会对整个产物目录做一次 scandir。对上百个
        「已存在」条目循环调用 = **O(N²) 的目录扫描**，主线程必然卡界面。
        这里按 ``extract_dir_for()`` **去重，每个目录只扫一次**，结果缓存复用。

        ⚠ 只做目录列举与扩展名过滤（``CHIP_EXTS``），**不做 probe / 不 stat
        不解码** —— 判重与解码全在消费线程（§1.2）。
        """
        if self._cv is None or not self._pending_resume:
            return
        dest_root = self._dest_root()
        by_dir = {}                       # 目录 -> [源文件...]
        n = 0
        for url, target in self._pending_resume:
            try:
                if joshw_dl is not None and joshw_dl.is_archive(target):
                    d = joshw_dl.extract_dir_for(target, dest_root)
                    if d not in by_dir:
                        by_dir[d] = self._list_payload(d)
                    srcs = by_dir[d]
                elif os.path.splitext(target)[1].lower() == ".spc":
                    srcs = [target]          # 裸 SPC: 直接就是源文件
                else:
                    srcs = []
            except OSError:
                srcs = []
            if not srcs:
                self._log(f"⏭ {os.path.basename(target)}："
                          + self._payload_hint(target))
                continue
            # 每个包首次入队打一行策略汇总（FR-C10.3）
            self._log_pkg_policy(os.path.basename(target), srcs)
            for s in srcs:
                self._cv_enqueue(s, url, pkg=os.path.basename(target))
                n += 1
        self._pending_resume = []
        self._cv_queued = n                 # 补上分母（此时还没 dl 事件进来）
        if n:
            self._log(f"🎵 已排队 {n} 个已存在文件待转 WAV")
            self._paint_convert_progress(force=True)

    @staticmethod
    def _list_payload(d):
        """产物目录里的芯片音源（**按 CHIP_EXTS 过滤**）。

        ⚠ 不能用 ``collect_supported()``（含 .cue/.bin/.mp3，会误收 MSU-1）。

        v6.0.6：返回前过一遍 ``drop_derived_wavs`` —— 这个目录里通常同时有
        ``xxx.fsb`` 和 ``xxx.fsb.wav``（后者是我们自己转出来的），
        两个都 ``is_convertible`` 为真，不去重就是同一首转两遍。
        """
        out = []
        try:
            for name in sorted(os.listdir(d)):
                p = os.path.join(d, name)
                if os.path.isfile(p) and dl_convert.is_convertible(p):
                    out.append(p)
        except OSError:
            return []
        return dl_convert.drop_derived_wavs(out)

    @staticmethod
    def _payload_hint(d):
        """产物目录「一个能转的都没有」时，说清楚**为什么**以及**怎么办**。

        ==================== 这条日志是被用户报障逼出来的 ====================
        原先这里是一句写死的话：「未找到可转换的芯片音源文件，已跳过」。
        用户下载了 PS1 的恶魔城音源（包内是 41 个 ``.txtp`` + ``sound/``
        下的 ``.xa`` / ``.DA``）后，看到的就是这一句 —— 它既没说是**什么**
        格式没被认，也没说**下一步**该干什么，等于把排查成本全丢给用户。

        现在按目录内容给出三种具体结论：

        * 有 PSF（``.minipsf`` 等）→ 指到右键菜单的 foobar2000 半自动流程；
        * 有 ``.txtp`` 但没转成 → 说明索引在但目标缺失（包不完整）；
        * 其它 → 老实说没认出格式，并给出实际看到的扩展名，便于反馈。

        Args:
            d: 已解压的产物目录（或单个文件路径）。

        Returns:
            一句中文结论（不含前缀，调用方自己加 ``⏭ 包名：``）。
        """
        p = str(d or "")
        folder = p if os.path.isdir(p) else (os.path.dirname(p) or ".")
        # ① PSF：要模拟器，但本工具能驱动 foobar2000 半自动转
        if psf_bridge is not None:
            try:
                n_psf = psf_bridge.count_tracks(folder)
            except Exception:                        # noqa: BLE001
                n_psf = 0
            if n_psf:
                return ("本包 %d 首是 PSF（PS1/PS2 内存快照，要模拟器才能出声），"
                        "无法全自动转 —— 请右键该条目选「🎵 用 foobar2000 转 WAV」"
                        % n_psf)
        # ② .txtp 索引在、但目标文件不在（包被截断 / 只解压了一半）
        try:
            n_txtp = sum(1 for n in os.listdir(folder)
                         if n.lower().endswith(".txtp")
                         and os.path.isfile(os.path.join(folder, n)))
        except OSError:
            n_txtp = 0
        if n_txtp:
            return ("本包有 %d 个 .txtp 索引，但它们指向的 .xa/.DA 不在包里 —— "
                    "包可能没解压完整，重新下载一次" % n_txtp)
        # ③ 真没认出来：把实际看到的扩展名报出来，别让用户猜
        exts = {}
        try:
            for n in os.listdir(folder):
                f = os.path.join(folder, n)
                if not os.path.isfile(f):
                    continue
                e = os.path.splitext(n)[1].lower()
                if e:
                    exts[e] = exts.get(e, 0) + 1
        except OSError:
            pass
        if exts:
            top = sorted(exts.items(), key=lambda kv: -kv[1])[:4]
            seen = "、".join("%s×%d" % (e, c) for e, c in top)
            return ("没认出可转的音源（看到的是 %s），已跳过" % seen)
        return "未找到可转换的音源文件，已跳过"

    def _log_pkg_policy(self, pkg_name, srcs):
        """每包首次入队打一行策略汇总（FR-C10.3 / AC-C24）。

        「能在日志里一眼数出本批有几个 SPC、几个芯片文件」—— 所以只打一次，
        且带完整路径上下文。
        """
        if pkg_name in self._cv_pkg_logged:
            return
        self._cv_pkg_logged.add(pkg_name)
        n_spc = sum(1 for s in srcs
                    if os.path.splitext(s)[1].lower() == ".spc")
        # v4.30: CD 音轨（.ape/.flac…）单独计数 —— 它走 PyAV，不吃 chip_secs
        n_cd = sum(1 for s in srcs
                   if os.path.splitext(s)[1].lower() in _CD_AUDIO_EXTS)
        # v4.41: PS1 .txtp 索引单独计数 —— 原生解 .xa/.DA，不要 foobar2000
        n_txtp = sum(1 for s in srcs
                     if os.path.splitext(s)[1].lower() == ".txtp")
        n_chip = len(srcs) - n_spc - n_cd - n_txtp
        bits = []
        if n_chip:
            bits.append(f"芯片 {n_chip} 个 = 按格式/曲目表")
        if n_spc:
            bits.append(f"SPC {n_spc} 个 = ID666 淡出")
        if n_cd:
            bits.append(f"CD 音轨 {n_cd} 个 = 原样转 WAV")
        if n_txtp:
            bits.append(f"PS1 索引 {n_txtp} 个 = 原生解 XA/PCM")
        self._log(f"🎵 {pkg_name}：{len(srcs)} 个文件 → 策略 "
                  + (" / ".join(bits) if bits else "无"))

    # ---- v4.0: 转换事件入口（工作线程 -> q -> 主线程，与 _on_dl_event 同构）
    def _cv_emit(self, kind, payload):
        """转换线程 / 预估线程回调：**只入队, 一个 Tk 方法都不碰**。

        ``_closed`` 时**立即早退** —— 窗口销毁后再 put 会让 Tcl 报
        "invalid command name"（R5）。
        """
        if self._closed:
            return
        try:
            self.q.put(("cv", kind, payload))
        except Exception:                            # noqa: BLE001
            pass

    def _cv_enqueue(self, src, url, pkg="", n_hint=0):
        """主线程唯一的入队路径。**只 put, 不判定、不解码、不 stat**。

        ``policy`` 在这里按扩展名定一次（主线程已有 ``os.path.splitext``，
        零成本），消费线程再复核一次 —— 两处都判是防御性冗余（设计 §2.1）。
        """
        cv = self._cv
        if cv is None or not src:
            return
        try:
            cv.enqueue({"src": src, "url": url, "pkg": pkg,
                        "policy": dl_convert.policy_for(src),
                        "n_hint": int(n_hint or 0)})
        except Exception:                            # noqa: BLE001
            return
        self._cv_queued += 1
        self._set_state(url, "⏳ 待转 WAV")

    def _cv_busy(self):
        """转换侧是否还有活（**B17 判据**）。

        返回 True 表示「转换线程还活着」或「队列里还有没被放弃的项」。
        ⚠ **不用** ``is_alive()`` 之外的自定义判据 ——
        :class:`~dl_convert.ConvertWorker` 继承 ``threading.Thread``，
        **自带** ``is_alive()``；**不要自己再定义一个同名方法**，
        会让 ``start()`` 之后的存活判断全部失效（连带把 B17 弄坏）。
        """
        cv = self._cv
        if cv is None:
            return False
        try:
            if cv.is_alive():
                return True
        except Exception:                            # noqa: BLE001
            pass
        # 线程刚起还没跑起来 / 已退出但队列非空: 都算「还有活」
        try:
            return not cv._q.empty() and not self._cv_abandoned
        except Exception:                            # noqa: BLE001
            return False

    def _stop_download(self):
        """「■ 停止」= 下载 + 解压 + 转 WAV **三段联动**（B17/B20/B21）。

        ⚠ **早退条件只看「三段是否都没在跑」**。v3.9 原文是
        ``if self.dl is None or not self._downloading: return`` —— 那是
        「下载视角」的正确判据，但转换排空期 ``self.dl`` 可能已是 None、
        ``_downloading`` 可能已是 False，而转换线程还在跑（几分钟），
        原条件会让**停止按钮彻底点不动**，用户只能杀进程。

        ============================ 三段分别停什么 ============================
        ① 下载 + 解压：``dl.cancel()``（同一个 Event 也被转换共用，见 M9）
        ② 转换队列  ：``cv.cancel()`` 置全局 Event + drain 未开始项
        ③ 当前文件  ：``cancel()`` 内部连带置 ``cv._cur["cancel"]``
           （由 dl_convert 负责；GUI 不直接碰那个 dict）
        """
        dl_busy = bool(self.dl is not None and self._downloading)
        cv_busy = self._cv_busy()
        if not (dl_busy or cv_busy):
            return
        if dl_busy:
            try:
                self.dl.cancel()       # ① 下载 + ② 解压(tar 子进程随之终止)
            except Exception as e:                   # noqa: BLE001
                self._log(f"⚠ 取消下载失败: {e}")
        cv = self._cv
        if cv is not None:
            # ③ 转换: 置 Event（全局 + 当前文件）+ 唤醒磁盘门禁 + 丢弃未开始项。
            # ⚠ **不 join** —— join 会卡住界面直到解码收手（最长 300s+）。
            try:
                cv.cancel()
            except Exception as e:                   # noqa: BLE001
                self._log(f"⚠ 取消转 WAV 失败: {e}")
            self._log(f"⛔ 已停止转 WAV（已产出 {self._cv_ok} 个文件 / "
                      f"{self._human(self._cv_bytes)}，队列剩余已丢弃）")
        self._status("正在停止…（下载 / 解压 / 转 WAV 都会收尾）")
        self._log("⛔ 已请求停止：下载 / 解压 / 转 WAV")
        try:
            self.btn_stop.config(state="disabled")
        except tk.TclError:
            pass

    def _on_dl_event(self, kind, payload):
        """下载线程回调: 只入队, 一个 Tk 方法都不碰。"""
        if self._closed:
            return
        try:
            self.q.put(("dl", kind, payload))
        except Exception:                            # noqa: BLE001
            pass

    def _url_for(self, path):
        if not path:
            return None
        try:
            norm = os.path.normpath(str(path))
        except Exception:                            # noqa: BLE001
            return None
        url = self._url_of_path.get(norm)
        if url:
            return url
        return self._url_of_base.get(os.path.basename(norm))

    def _size_for(self, path):
        if not path:
            return 0
        try:
            norm = os.path.normpath(str(path))
        except Exception:                            # noqa: BLE001
            return 0
        if norm in self._size_of_path:
            return self._size_of_path[norm]
        base = os.path.basename(norm)
        for k, v in self._size_of_path.items():
            if os.path.basename(k) == base:
                return v
        return 0

    def _set_state(self, url, text):
        if not url:
            return
        self._states[url] = text
        iid = self.iid_of.get(url)
        if not iid:
            return
        try:
            vals = list(self.tree.item(iid, "values"))
            if len(vals) >= 4:
                vals[3] = text
                self.tree.item(iid, values=vals)
        except tk.TclError:
            pass

    def _refresh_row_states(self):
        for url, text in list(self._states.items()):
            self._set_state(url, text)

    def _paint_progress(self, force=False):
        now = time.time()
        if not force and (now - self._last_paint) < self.PAINT_SEC:
            return
        self._last_paint = now
        pct = 100.0 * self._dl_shown / max(1.0, float(self._dl_total))
        try:
            self.progress["value"] = max(0.0, min(100.0, pct))
        except tk.TclError:
            return
        self._status(f"下载中 {self._dl_files_done}/{self._dl_expect} 个 · "
                     f"{self._human(int(self._dl_shown))} / "
                     f"{self._human(self._dl_total)}")

    def _handle_dl(self, kind, payload):
        """主线程处理下载事件。"""
        payload = payload if isinstance(payload, dict) else {}
        if kind == "file_start":
            url = self._url_for(payload.get("file"))
            if url:
                self._set_state(url, "下载中…")
            try:
                self._inflight = int(payload.get("size") or 0)
            except (TypeError, ValueError):
                self._inflight = 0
            return

        if kind == "progress":
            try:
                done = float(payload.get("done") or 0)
            except (TypeError, ValueError):
                done = 0.0
            moved = self._dl_finished + max(0.0, min(done, float(
                self._inflight or done)))
            if moved > self._dl_shown:
                self._dl_shown = min(moved, float(self._dl_total))
            self._paint_progress()
            return

        if kind == "file_done":
            url = self._url_for(payload.get("file"))
            size = self._size_for(payload.get("file"))
            self._dl_files_done += 1
            self._dl_finished += max(0, size)
            self._dl_shown = max(self._dl_shown, float(self._dl_finished))
            name = os.path.basename(str(payload.get("file") or "?"))
            if payload.get("ok"):
                self._dl_ok += 1
                self._set_state(url, "✅ 完成")
                self._log(f"✅ {name}  {self._human(size)}")
                # ---- v4.0 M13: SPC 裸文件路径（FR-C3.1, **最易漏点**）----
                # SNES 的 SPC 是裸文件、**不经解压**, 所以这里也必须入队,
                # 否则只接 extract_done 的话 SPC 永远转不出来(AC-C2)。
                # ⚠ 只认 .spc: .7z 要等 extract_done 才有源文件。
                if (self._cv is not None
                        and name.lower().endswith(".spc")):
                    src = str(payload.get("file") or "")
                    self._cv_enqueue(src, url, pkg=name)
                    self._log_pkg_policy(name, [src])
                    self._set_state(url, "⏳ 待转 WAV")
            else:
                self._dl_fail += 1
                self._set_state(url, "❌ 失败")
                reason = str(payload.get("error") or "下载失败")
                self._log(f"❌ {name}: {reason}")
                if "404" in reason:
                    self._log("ℹ 该条目可能已从站点移除，"
                              "点「🗑 清除缓存」重扫可拿到最新列表")
            self._paint_progress(force=True)
            return

        # ---- 自动解压事件(joshw_dl) ----
        if kind == "extract_start":
            name = os.path.basename(str(payload.get("file") or "?"))
            self._set_state(self._url_for(payload.get("file")), "📦 解压中…")
            self._status(f"解压中 {name} …")
            self._log(f"📦 正在解压 {name} → "
                      f"{os.path.basename(str(payload.get('dest') or ''))}")
            return

        if kind == "extract_done":
            name = os.path.basename(str(payload.get("file") or "?"))
            count = int(payload.get("count") or 0)
            self._dl_extracted += 1
            self._dl_ex_files += count
            state = "📦 已解压"
            if payload.get("archive_removed"):
                state = "📦 已解压(删包)"
            self._set_state(self._url_for(payload.get("file")), state)
            dest = str(payload.get("dest") or "")
            self._log(f"✅ 已解压 {name} → {dest}（{count} 个文件）")
            files = payload.get("files") or []
            for f in list(files)[:5]:
                self._log(f"     · {os.path.basename(str(f))}")
            if len(files) > 5:
                self._log(f"     · … 另有 {len(files) - 5} 个文件")
            if count:
                self._status(f"已下载并解压 {self._dl_extracted} 个包 / "
                             f"{self._dl_ex_files} 个文件")
            self._refresh_disk()          # 让列表立刻显示「✓ 已下载」
            # ---- v4.0 M14: 芯片包路径（FR-C3.2）----
            # ⚠ 过滤**必须**用 chip_formats.CHIP_EXTS, 不得用 collect_supported()
            #   （后者含 .cue/.bin/.mp3, 会把 MSU-1 与 CD 镜像误收进来）。
            # ⚠ R9: files 数量应与 count 一致; 不一致时**告警而非静默**。
            if self._cv is not None:
                if count and len(files) != int(count):
                    self._log(f"⚠ {name}：解压产物 {len(files)} 个与统计"
                              f" {count} 个不一致，按实际列表处理")
                srcs = dl_convert.drop_derived_wavs(
                    [str(f) for f in files if dl_convert.is_convertible(f)])
                if srcs:
                    self._log_pkg_policy(name, srcs)
                    for f in srcs:
                        self._cv_enqueue(f, self._url_for(payload.get("file")),
                                         pkg=name)
                else:
                    # v4.41: 以前这里**什么都不说** —— 用户勾了「下载后自动
                    # 转 WAV」、下载也成功了，却没有任何产出也没有任何解释。
                    # 现在按目录内容给出具体原因与下一步（PSF → foobar2000）。
                    self._log(f"⏭ {name}：" + self._payload_hint(dest))
            return

        if kind == "extract_failed":
            name = os.path.basename(str(payload.get("file") or "?"))
            self._dl_ex_failed += 1
            self._set_state(self._url_for(payload.get("file")), "⚠ 解压失败")
            reason = str(payload.get("error") or "解压失败")
            self._log(f"⚠ {name} 解压失败: {reason}")
            tail = str(payload.get("reason") or "")
            if tail:
                self._log(f"ℹ {tail}")
            return

        if kind == "all_done":
            self._on_dl_all_done()

    # ---- v4.0 M15/M16: all_done ≠ 整批完成 ----
    def _on_dl_all_done(self):
        """下载 + 解压全部结束。

        ⚠ ``all_done`` **不再等于「整批完成」**（PRD B11/B17 / 设计 §3.1）：
        转换队列可能还堵着几十个文件。若这里就调 ``_finish_download()``，
        它会同时置 ``_downloading=False`` 与 ``self.dl=None`` ->
        ``_stop_download`` 早退 -> **排空期「■ 停止」完全失效**（AC-C12②）。

        所以：
        * 开关关闭 / 模块缺 -> **立即**走原路径（零回归，FR-C7.3）；
        * 开关打开 -> 只置标志，由消费线程在「队列空 + 源已结束」时发
          ``cv_thread_exit``，那时才调 ``_finish_download()``。
        """
        self._cv_src_finished = True
        if self._cv is None:
            self._finish_download()
            return
        try:
            self._cv.mark_src_finished()
        except Exception as e:                       # noqa: BLE001
            self._log(f"⚠ 标记下载结束失败: {e}")
            self._cv = None
            self._finish_download()
            return
        left = 0
        try:
            left = self._cv.stats().get("left", 0)
        except Exception:                            # noqa: BLE001
            pass
        if left:
            self._status(f"下载完成，正在收尾转换…（剩 {left} 个文件）")
        else:
            self._status("下载完成，正在收尾转换…")

    def _finish_download(self):
        """收尾：归零进度、汇总、解锁按钮。**只能被调用一次**。

        ⚠ v4.0 加了 **重入守卫**：取消场景下 ``cv_thread_exit`` 与
        ``all_done`` 可能**都**到达（点停止后 Downloader 也会发
        ``all_done``），两条路径都会调本方法。没有守卫就会：
        进度归零两次、汇总日志打两遍、**失败汇总弹窗弹两次**。
        """
        if self._finished:
            return
        self._finished = True
        self._downloading = False
        self._dl_shown = float(self._dl_total)
        # 下载结束把进度归零(见 _apply_entries 里的同一条约定)
        try:
            self.progress["value"] = 0
        except tk.TclError:
            pass
        tail = f"，跳过 {self._dl_skip} 个" if self._dl_skip else ""
        ex = ""
        if self._dl_extracted:
            ex = f"，已下载并解压 {self._dl_ex_files} 个文件"
        elif self._dl_ex_failed:
            ex = f"，{self._dl_ex_failed} 个包解压失败（已保留原包）"
        self._status(f"完成 — 成功 {self._dl_ok} 个，失败 {self._dl_fail} 个"
                     f"{tail}{ex}")
        self._log("=" * 42)
        self._log(f"🎉 下载结束: 成功 {self._dl_ok}，失败 {self._dl_fail}{tail}{ex}")
        if self._dl_ex_failed:
            self._log(f"⚠ {self._dl_ex_failed} 个包没能自动解开，"
                      f"原 .7z 已保留，可手动解压后拖进主窗口")
        if self._dl_extracted:
            self._log("💡 下载回来的 NSF / GBS / VGM / HES 现在可以直接"
                      "拖进主窗口转换了")
        # ---- v4.0 M17: WAV 汇总两行（**追加在原句之后, 不改原句**, FR-C6.4）----
        # ⚠ B10: 必须显示 _dest_root()/WAV, 用户填的目录可能不带机种层。
        if self._cv_queued or self._cv_ok or self._cv_skip or self._cv_fail:
            self._log(f"📁 WAV 输出目录：{self._cv_wav_dir or self._wav_dir()}")
            self._log(f"🎵 转出 {self._cv_ok} 个文件 / "
                      f"{self._human(self._cv_bytes)}"
                      f"（成功 {self._cv_ok} 个 · 失败 {self._cv_fail}"
                      f" · 已转过 {self._cv_skip}）"
                      + (f" · 磁盘不足放弃 {self._cv_abandoned}" if
                         self._cv_abandoned else ""))
            self._log(f"   · 本次共排队 {self._cv_queued} 个源文件")
        self._refresh_disk()                 # 让列表立刻显示「✓ 已下载」
        self.dl = None
        self._cv = None                     # v4.0: 转换已排空, 释放引用
        self._cv_current = None
        self._sync_buttons()
        # 只有失败时才弹模态框: 全部成功时用状态行+日志收尾即可,
        # 否则用户正在切机种/搜索会被弹窗打断。
        if self._dl_fail or self._dl_ex_failed:
            try:
                messagebox.showwarning(
                    "下载完成(有失败)",
                    f"成功 {self._dl_ok} 个，失败 {self._dl_fail} 个{tail}\n"
                    + (f"解压失败 {self._dl_ex_failed} 个（原包已保留）。\n"
                       if self._dl_ex_failed else "")
                    + f"失败原因见日志。", parent=self)
            except tk.TclError:
                pass


    # ---- 缓存 ---------------------------------------------------------------
    # ---- v4.0 M19/M20: 转换事件消费（主线程侧）--------------------------
    def _paint_convert_progress(self, force=False):
        """转换阶段进度。**每次按新分母重算百分比**（禁止沿用旧 value）。

        否则分母一变（``extract_done`` 让分母增长），用户会看到进度条**倒退**。
        """
        now = time.time()
        if not force and (now - self._last_paint) < self.PAINT_SEC:
            return
        self._last_paint = now
        done, total = self._cv_done, self._cv_queued
        pct = 100.0 * done / max(1, total)
        try:
            self.progress["value"] = max(0.0, min(100.0, pct))
        except tk.TclError:
            return
        head = (f"转 WAV {done}/{total}" if total else "转 WAV 收尾")
        self._status(f"{head} · 已产出 {self._cv_ok} 个文件 / "
                     f"{self._human(self._cv_bytes)}"
                     + (f" · 失败 {self._cv_fail}" if self._cv_fail else ""))

    def _check_conv_timeout(self):
        """主线程巡检单文件超时（**每 100ms 一次, 不新建线程/watchdog**）。

        ⚠ **绝不** ``join(300)`` 然后放弃 —— Python 无法安全杀死一个正在解码
        的线程，那个线程会继续往 ``WAV/`` 写文件、继续改 ``used``/``start_idx``
        -> 编号竞争 + 覆盖（设计 §3.4）。
        正确做法是置**本文件独立**的 cancel Event：解码在下一个 run 边界
        抛 ``ChipCancelled``、``write_tracks`` 在下一轨开头 break（**正在写的
        那一轨会写完整**，不留半截 WAV）。
        """
        cv = self._cv
        if cv is None:
            return
        try:
            cur = cv.current()
        except Exception:                            # noqa: BLE001
            return
        if not cur:
            return
        try:
            secs = time.time() - float(cur.get("t0") or 0)
        except (TypeError, ValueError):
            return
        src = str((cur.get("job") or {}).get("src") or "")
        name = os.path.basename(src)
        if secs > dl_convert.ConvertWorker.STUCK_GRACE_S and not cur.get("stuck"):
            cur["stuck"] = True
            self._log(f"⚠ 解码子进程疑似卡死（{secs:.0f}s）：{name}，"
                      f"可点「■ 停止」中止（转换线程不卡界面，界面仍可关闭）")
        elif (secs > dl_convert.ConvertWorker.FILE_TIMEOUT_S
                and not cur.get("timed_out")):
            cur["timed_out"] = True        # 去重：只报一次
            try:
                cur["cancel"].set()        # 只置**本文件**的 cancel
            except Exception:              # noqa: BLE001
                pass
            self._log(f"⚠ 转 WAV 超时（>{dl_convert.ConvertWorker.FILE_TIMEOUT_S:.0f}s）："
                      f"{name}，已跳过")
            self._status(f"⚠ {name} 转 WAV 超时，已跳过")

    def _handle_cv(self, kind, payload):
        """主线程消费转换事件。**这里才可以碰 Tk**。

        ⚠ ``_closed`` 早退必须在这里**也**判一次 —— 防「入队早于关闭」的竞态
        （R5.4）。
        """
        if self._closed:
            return
        payload = payload if isinstance(payload, dict) else {}
        name = os.path.basename(str(payload.get("src") or ""))

        if kind == "cv_skip":
            self._cv_done += 1
            self._cv_skip += 1
            self._set_state(payload.get("url"), "⏭ 已转过")
            n = int(payload.get("n_wav") or 0)
            self._log(f"⏭ 已转过，跳过: {name}"
                      + (f"（上次 {n} 首）" if n else ""))
            self._paint_convert_progress()
            return

        if kind == "cv_file_done":
            self._cv_done += 1
            self._cv_bytes += int(payload.get("bytes") or 0)
            n = int(payload.get("n_wav") or 0)
            policy = str(payload.get("policy") or "")
            mark = f" [{policy}]" if policy else ""      # FR-C10.3 必带
            if n:
                self._cv_ok += 1
                self._set_state(payload.get("url"), f"🎵 已转{n}首")
                self._log(f"✅ 已转 WAV：{name} → {n} 首 / "
                          f"{self._human(payload.get('bytes'))}{mark}")
            else:
                # 解码跑完了但一首都没产出：算失败, 但**原因必须写出来**
                self._cv_fail += 1
                self._set_state(payload.get("url"), "⚠ 转失败")
                self._log(f"⚠ {name}：解码完成但没有产出 WAV{mark}")
                for err in (payload.get("errors") or [])[:2]:
                    self._log(f"     · {str(err).splitlines()[0]}")
            self._paint_convert_progress()
            return

        if kind == "cv_merged":
            # v4.24: 合并结果**单独一条事件**，不混进 cv_log —— 后者只显示
            # 「ℹ」开头的行（FR-C10.4 的降噪规则），合并成功要明确告诉用户
            # 文件叫什么、有多长，否则他找不到。
            src = os.path.basename(str(payload.get("src") or ""))
            if payload.get("error"):
                self._log(f"⚠ 合并失败：{src} — {payload.get('error')}")
            else:
                secs = float(payload.get("seconds") or 0)
                self._log("🔗 已合并 %d 首 → %s（%d:%04.1f）"
                          % (int(payload.get("files") or 0),
                             os.path.basename(str(payload.get("path") or "")),
                             secs // 60, secs % 60))
            return

        if kind == "cv_file_failed":
            self._cv_done += 1
            self._cv_fail += 1
            self._set_state(payload.get("url"), "⚠ 转失败")
            policy = str(payload.get("policy") or "")
            mark = f" [{policy}]" if policy else ""
            reason = str(payload.get("error") or "未知错误")
            name = os.path.basename(str(payload.get("src") or ""))
            self._log(f"⚠ 转 WAV 失败：{name}{mark} — "
                      f"{reason.splitlines()[0] if reason else '未知错误'}")
            # ---- B18 零产出：给**可执行**的提示（K1/K2）----
            # 坏文件**不写 manifest 成功记录** -> 下次还会重试。用户要知道
            # 「为什么没产出」+「怎么办」，而不是只看到一个失败计数。
            if payload.get("zero_output"):
                self._set_state(payload.get("url"), "⚠ 零产出")
                self._log(f"ℹ {name} 没有产出 WAV，请手动检查：文件可能"
                          f"已损坏、截断，或该子系统的音频不受支持；"
                          f"换个播放器/解压器验证后再试一次。"
                          f"（本文件不会被记为已转换，下次会重试）")
            skips = payload.get("skips")
            if skips:
                self._log(f"ℹ {name} 被跳过的轨道: {skips}")
            for detail in (payload.get("detail") or [])[:5]:
                self._log(f"     · {detail}")
            self._paint_convert_progress()
            return

        if kind == "cv_paused":
            # 事件自带节流(PAUSE_LOG_S), 这里不再刷屏, 只更新状态行
            self._status(f"⚠ 磁盘空间不足，转换已暂停"
                         f"（剩 {payload.get('left', '?')} 个文件；"
                         f"清理空间后自动继续）")
            return

        if kind == "cv_abandoned":
            left = int(payload.get("left") or 0)
            self._cv_abandoned += left
            # 收敛: 明确告知怎么补转, 否则用户以为程序卡死了
            self._log(f"⚠ 磁盘空间不足，剩余 {left} 个文件未转换"
                      f"（清理空间后重新勾选「自动转 WAV」再点下载即可补转）")
            self._status(f"⚠ 磁盘空间不足，{left} 个文件未转换")
            return

        if kind == "cv_error":
            self._log(f"⚠ 转 WAV 线程异常：{payload.get('error')}")
            self._status(f"⚠ 转 WAV 出错：{payload.get('error')}")
            return

        if kind == "cv_estimate":
            # 体量预估结果（AC-C22）。代际号不符 -> 丢弃过期结果。
            try:
                if int(payload.get("token", -1)) != self._est_token:
                    return
            except (TypeError, ValueError):
                return
            if payload.get("error"):
                self.var_wav_estimate.set("≈ 预估失败")
                return
            tracks = int(payload.get("tracks") or 0)
            nbytes = float(payload.get("bytes") or 0)
            self.var_wav_estimate.set(f"≈ {tracks} 首 / {self._human(nbytes)}")
            return

        if kind == "cv_thread_exit":
            # 队列排空 + 源已结束 -> 现在才收尾（AC-C17：🎉 必须晚于最后一个 WAV）
            self._cv_done = self._cv_queued
            self._cv = None
            self._cv_current = None
            # ⚠ 产出首数用 worker 报的**真实累计**，不能用 self._cv_queued ——
            #   后者是「排队的文件数」，零产出的文件也计入。实测会出现
            #   「失败 1 个」却打「共产出 1 首」的矛盾行。
            n_tracks = int(payload.get("wavs") or 0)
            self._log(f"🎵 转 WAV 结束：成功 {self._cv_ok} 个文件，"
                      f"失败 {self._cv_fail}，已转过 {self._cv_skip}，"
                      f"共产出 {n_tracks} 首 / "
                      f"{self._human(self._cv_bytes)}")
            # v4.24: 合并统计单独一行 —— 只报成功，失败已在 cv_merged 里
            # 逐条说过（这里再报一遍会让用户以为又失败了一次）。
            n_merged = int(payload.get("merged") or 0)
            if n_merged:
                self._log(f"🔗 另合并出 {n_merged} 个整轨文件（分轨保留，"
                          f"命名「源名（合并）.wav」）")
            # ⚠ 无论正常收尾还是用户点过停止, 都要走 _finish_download:
            #   它负责 progress 归零、解锁按钮、弹失败汇总框。
            #   这也是 AC-C12② 的关键: _finish_download 是**唯一**收尾入口,
            #   而它被延后到队列排空之后, 所以排空期 self.dl 仍非 None、
            #   _downloading 仍为 True -> 「■ 停止」不会早退（B17）。
            # ⚠ 收尾后**立刻**重算按钮: 此时 cv 已退出, 停止按钮应变灰,
            #   三个复选框应解锁 —— 否则用户被留在「运行中」状态里。
            self._finish_download()
            self._sync_buttons()
            return

        if kind == "cv_log":
            # ⚠ 解码明细**不逐行刷屏**（FR-C10.4: 会淹没下载日志）。
            #   只有 dl_convert 主动标了「重要」的才进日志区 —— 目前两种:
            #   ① B21「停止瞬间正在转换, 已产出 N 首(保留)」;
            #   ② 有轨道被跳过。
            #   其余（每轨一行 "WAV 01/02: ..."）全部丢弃。
            for line in (payload.get("detail") or [])[:3]:
                text = str(line)
                if text.startswith("ℹ"):
                    self._log(text)
            return

        # cv_progress / cv_stage 等明细 kind: **不进界面**。
        # 解码日志会淹没下载日志（FR-C10.4），成功时直接丢弃；
        # 失败时明细已随 cv_file_failed 展开。

    def _clear_cache(self):
        """清除站点索引缓存（语义是「重扫站点」，与 ``WAV/`` 无关）。

        ⚠ v4.0 **刻意不删** ``<WAV 目录>/.msu1-convert.json``（FR-C8.5 / R10）：
        那是转 WAV 的记账文件，删掉会导致下次全部重转、重复占几 GB。
        「没有代码就没有 bug」—— 这个方法**不需要**为它加任何清理逻辑。
        """
        if jw is None:
            return
        key = self.cur_key
        if key:
            if not messagebox.askyesno(
                    "清除缓存",
                    f"清除「{self.cur_name}」的目录缓存并重新联网扫描？\n"
                    f"重扫约 30 秒（内置索引快照也会被跳过）。", parent=self):
                return
            try:
                jw.clear_cache(key)
            except Exception as e:                   # noqa: BLE001
                self._log(f"❌ 清除缓存失败: {e}")
                return
            self._log(f"🗑 已清除 {key} 的缓存，开始重扫")
            self._load_platform(key, force_scan=True)
            return
        if not messagebox.askyesno(
                "清除缓存",
                "当前没有选中机种。\n清除全部机种的目录缓存？", parent=self):
            return
        try:
            jw.clear_cache(None)
        except Exception as e:                       # noqa: BLE001
            self._log(f"❌ 清除缓存失败: {e}")
            return
        self._log("🗑 已清除全部机种的缓存")

    # ---- 队列轮询 / 关闭 ----------------------------------------------------
    def _poll(self):
        self._after_poll = None
        try:
            while True:
                ev = self.q.get_nowait()
                try:
                    self._dispatch(ev)
                except Exception as e:               # noqa: BLE001
                    self._log(f"⚠ 界面刷新异常: {e}")
                    traceback.print_exc()
        except queue.Empty:
            pass
        except Exception:                            # noqa: BLE001
            traceback.print_exc()
        # ---- v4.0 M20/M21: 单文件超时巡检 ----
        # 放在队列消费之后: 先把本轮事件处理完, 再看有没有卡住的文件。
        # 复用**已有的** after 轮询, 不新建 watchdog 线程。
        if not self._closed and self._cv is not None:
            try:
                self._check_conv_timeout()
            except Exception as e:                   # noqa: BLE001
                self._log(f"⚠ 超时巡检异常: {e}")
        if not self._closed:
            try:
                self._after_poll = self.after(self.POLL_MS, self._poll)
            except tk.TclError:
                self._after_poll = None

    def _dispatch(self, ev):
        kind = ev[0]
        if kind == "zoom_changed":
            return
        if kind == "cloud":
            self._on_cloud_event(ev[1], ev[2] if len(ev) > 2 else None)
            return
        if kind == "play_ready":
            self._on_play_ready(ev[1])
            return
        if kind == "play_error":
            self._on_play_error(ev[1])
            return
        if kind == "zh_progress":
            _k, done, total = ev
            try:
                self.progress["value"] = max(
                    0.0, min(100.0, 100.0 * done / max(1, total)))
            except tk.TclError:
                return
            self._status(f"翻译中 {done}/{total}…")
            return
        if kind == "zh_done":
            _k, pairs, err = ev
            self._zh_running = False
            try:
                self.btn_zh.configure(text="翻译未收录")
                self.progress["value"] = 0
            except tk.TclError:
                pass
            added = 0
            if pairs:
                added = zh_titles.add_pairs(pairs)
                try:
                    path = zh_titles.save_pairs(pairs)
                    self._log(f"✅ 翻译完成：{len(pairs)} 条（新增 {added}），"
                              f"已写入 {path}")
                except Exception as exc:              # noqa: BLE001
                    self._log(f"⚠ 翻译完成 {len(pairs)} 条，但写入词典失败：{exc}")
                self._render_rows()
            if err:
                self._log(f"❌ 翻译中断：{err}（已翻好的 {len(pairs)} 条仍然保留）")
                self._status(f"翻译中断 — 已保留 {len(pairs)} 条")
            else:
                self._status(f"翻译完成 — 新增 {added} 条中文名")
            return
        if kind == "scan_progress":
            _k, tok, done, total, msg = ev
            if tok != self._scan_tok:
                return                               # 上一个机种的残留事件
            total = max(1, total)
            try:
                self.progress["value"] = max(
                    0.0, min(100.0, 100.0 * done / total))
            except tk.TclError:
                return
            self._status(f"扫描中 {done}/{total} 页…"
                         + (f" {msg}" if msg else ""))
            return
        if kind == "scan_done":
            _k, tok, items = ev
            if tok != self._scan_tok:
                return
            # 扫完 scan_platform 已把结果写进缓存, 所以后缀按「缓存」显示
            self._apply_entries(items, from_cache=True)
            return
        if kind == "scan_error":
            _k, tok, msg = ev
            if tok != self._scan_tok:
                return
            self._scanning = False
            self._status(f"扫描失败：{msg}")
            self._log(f"❌ 扫描 {self.cur_key} 失败: {msg}")
            try:
                self.progress["value"] = 0
            except tk.TclError:
                pass
            self._sync_buttons()
            return
        if kind == "dl":
            self._handle_dl(ev[1], ev[2] if len(ev) > 2 else None)
            return
        if kind == "cv":                             # v4.0 M22
            self._handle_cv(ev[1], ev[2] if len(ev) > 2 else None)

    def _on_close(self):
        # 释放试听播放器（v4.9）
        try:
            if getattr(self, "player", None) is not None:
                self.player.close()
                self.player = None
        except Exception:                                    # noqa: BLE001
            pass
        try:
            if getattr(self, "_after_player", None) is not None:
                self.after_cancel(self._after_player)
                self._after_player = None
        except Exception:                                    # noqa: BLE001
            pass
        running = self._downloading
        try:
            running = bool(self.dl is not None and self.dl.is_running())
        except Exception:                            # noqa: BLE001
            pass
        if self._cv is not None:
            running = True
        if running:
            extra = ("\n转 WAV 也会中止（已产出的 WAV 会保留）。"
                     if self._cv is not None else "")
            if not messagebox.askyesno(
                    "确认退出",
                    "下载仍在进行，退出会中止未完成的文件。\n"
                    f"确定要停止并关闭吗？{extra}",
                    parent=self):
                return
            try:
                if self.dl is not None:
                    self.dl.cancel()
            except Exception:                        # noqa: BLE001
                pass
        self._shutdown()

    def _shutdown(self):
        self._zh_cancel.set()      # 翻译线程立刻收手
        """置 _closed 让所有回调早退, 取消 after 轮询, 然后销毁。

        ⚠ v4.0 M24 **顺序是硬要求**（设计 §2.3 / R5）::

            置 _closed → after_cancel → 取消预估 → 停转换 → 停下载 → destroy

        ``daemon=True`` 只保证进程退得出去，**不保证**转换线程不在
        ``destroy()`` 之后继续往 ``WAV/`` 写文件。
        """
        self._closed = True
        try:
            self._cancel_scan.set()
        except Exception:                            # noqa: BLE001
            pass
        # v4.23: 关窗后立刻叫停上传（daemon 线程不会阻塞退出, 但让它别再
        # 一首一首往云盘传下去 —— 已经没有界面能反馈进度了）。
        try:
            self._cloud_cancel.set()
        except Exception:                            # noqa: BLE001
            pass
        for attr in ("_after_poll", "_after_filter", "_after_est"):
            aid = getattr(self, attr, None)
            if aid is not None:
                try:
                    self.after_cancel(aid)
                except Exception:                    # noqa: BLE001
                    pass
                setattr(self, attr, None)
        # v4.0: 先停转换。它与 Downloader 共用同一个 cancel Event，但这里还要
        # **drain 队列**并唤醒可能卡在磁盘门禁的线程，让它尽快走到出口。
        cv = self._cv
        if cv is not None:
            try:
                cv.cancel()
            except Exception:                        # noqa: BLE001
                pass
        if self._cv_cancel is not None:
            try:
                self._cv_cancel.set()
            except Exception:                        # noqa: BLE001
                pass
        try:
            if self.dl is not None and self.dl.is_running():
                self.dl.cancel()
        except Exception:                            # noqa: BLE001
            pass
        # 横幅的 scanline 动画是靠 after 自循环的, 必须在 destroy 之前停掉,
        # 否则销毁后那一跳会报 "invalid command name ...0000C0D8E4B0"。
        banner = getattr(self, "banner", None)
        if banner is not None:
            try:
                banner.stop()
            except Exception:                        # noqa: BLE001
                pass
        try:
            self.destroy()
        except Exception:                            # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 网易云扫码登录（v4.19）
# ---------------------------------------------------------------------------

def _qr_matrix(text):
    """文本 → 二维码矩阵 ``list[list[int]]``（四周含 4 格 quiet zone）。

    ⚠⚠ 用 **segno** 而不是 ``qrcode``：后者一 import 就会把 PIL 的
    ``image.styles.moduledrawers.pil`` 拉进来，实测让 exe 多出 **6.3MB**
    （还连带 lxml 3MB）—— 而我们只要矩阵、压根不画图片。
    segno 是纯 Python 零依赖，整包 549KB，出矩阵只要 ``qr.matrix``。

    quiet zone 不能省：四周不留白，手机扫码器对不上焦。
    """
    import segno
    qr = segno.make(text, error="m")
    body = [list(row) for row in qr.matrix]
    n = len(body)
    size = n + 8                     # 四周各 4 格
    out = [[0] * size for _ in range(size)]
    for y in range(n):
        for x in range(n):
            out[y + 4][x + 4] = 1 if body[y][x] else 0
    return out


class NeteaseLoginDialog:
    """登录窗口，三个标签页：**浏览器登录** / **粘贴 Cookie** / **扫码**。

    ## 为什么默认是「浏览器登录」

    网易云给网页扫码登录加了 **8821 行为验证码**风控：手机点确认会提示
    「请切换其他登陆方式或升级新版本再试」。这是服务端拒签，不是我们加密写错。

    8821 拦的是「非官方客户端调用登录接口」。所以这里换个思路：**不调用登录
    接口**，而是开一个真浏览器窗口让用户走官方网页登录，登录完把浏览器**已经
    持有**的 cookie 通过 DevTools Protocol 取走。服务端看到的是正常 web 登录，
    风控没有理由拦。

    而且 profile 是专属目录且持久化（见 ``netease_cookie``），登录一次之后
    每次只要 1 秒左右，用户基本无感。

    ⚠⚠ **二维码必须纯黑白**（白底 + 纯黑块），它**不跟随主题配色**。
    砖红/橄榄绿的二维码手机扫码器很难认 —— 这是全项目唯一一处刻意不套主题
    的地方，改之前先想清楚。

    所有网络请求都在后台线程跑，结果经 ``self._q`` 回主线程消费：Tk 只能在
    主线程碰，这条线不能破。
    """

    def __init__(self, parent, session):
        self.session = session
        self.ok = False
        self._q = queue.Queue()
        self._key = ""
        self._after = None
        self._closed = False
        self._done = False          # 登录已成功 —— 只停轮询，不影响 _close 关窗
        self._cookie_busy = False
        self._bl = None             # 正在跑的 BrowserLogin（后台线程写、主线程读）
        self._br_stop = False       # 通知后台线程停手（关窗时置 True）
        self._br_busy = False

        p = theme.PALETTE
        self.top = tk.Toplevel(parent)
        self.top.title("登录网易云音乐")
        self.top.configure(bg=p["bg"])
        self.top.resizable(False, False)
        self.top.transient(parent)

        wrap = ttk.Frame(self.top, style="Panel.TFrame")
        wrap.pack(fill="both", expand=True, padx=12, pady=10)

        self.nb = ttk.Notebook(wrap)
        self.nb.pack(fill="both", expand=True)

        tab_br = ttk.Frame(self.nb, style="Panel.TFrame")
        tab_ck = ttk.Frame(self.nb, style="Panel.TFrame")
        tab_qr = ttk.Frame(self.nb, style="Panel.TFrame")
        self.nb.add(tab_br, text="  浏览器登录  ")
        self.nb.add(tab_ck, text="  粘贴 Cookie  ")
        self.nb.add(tab_qr, text="  扫码（可能被风控）  ")
        self._build_browser_tab(tab_br)
        self._build_cookie_tab(tab_ck)
        self._build_qr_tab(tab_qr)
        self.nb.select(0)           # 默认停在最省事的那页

        foot = ttk.Frame(wrap, style="Panel.TFrame")
        foot.pack(fill="x", pady=(8, 0))
        ttk.Button(foot, text="关闭", style="Small.TButton",
                   command=self._close).pack(side="right")

        self.top.protocol("WM_DELETE_WINDOW", self._close)
        self.top.update_idletasks()
        # 固定尺寸：三个 Tab 内容高度不同，不锁死的话每切一次窗口就跳一下。
        w, h = 470, 472
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            x = px + max(0, (pw - w) // 2)
            y = py + max(0, (ph - h) // 3)
        except Exception:                            # noqa: BLE001
            x, y = 200, 150
        self.top.geometry("%dx%d+%d+%d" % (w, h, x, y))

        threading.Thread(target=self._fetch_key, daemon=True).start()
        self._tick()

    # ---- 标签页 0：浏览器登录（推荐）--------------------------------------
    def _build_browser_tab(self, box):
        pad = {"padx": 10}

        ttk.Label(box, text="用浏览器登录（推荐）", style="Sub.TLabel"
                  ).pack(anchor="w", pady=(8, 2), **pad)
        ttk.Label(box,
                  text="会打开一个独立浏览器窗口，在里面正常登录一次即可。\n"
                       "之后每次上传都会自动复用，不用再登录。",
                  style="Muted.TLabel", font=theme.FONT_S, justify="left",
                  wraplength=430).pack(anchor="w", **pad, pady=(0, 12))

        row = ttk.Frame(box, style="Panel.TFrame")
        row.pack(fill="x", **pad)
        self.btn_br = ttk.Button(row, text="打开浏览器登录", style="Accent.TButton",
                                 width=16, command=self._start_browser_login)
        self.btn_br.pack(side="left")

        self.var_br = tk.StringVar(value="尚未开始")
        ttk.Label(box, textvariable=self.var_br, style="Muted.TLabel",
                  font=theme.FONT_S, wraplength=430, justify="left"
                  ).pack(anchor="w", **pad, pady=(10, 8))

        tips = ttk.Frame(box, style="Panel.TFrame")
        tips.pack(fill="x", **pad, pady=(2, 0))
        for line in (
            "· 用的是独立 profile，不会动你日常那个 Chrome",
            "· 扫码或账号密码都行，走的是网易云官方网页",
            "· 登录成功后浏览器会自动关闭",
            "· 凭据只存在本机，不会发往任何第三方",
        ):
            ttk.Label(tips, text=line, style="Muted.TLabel",
                      font=theme.FONT_S).pack(anchor="w")

        ttk.Separator(box, orient="horizontal").pack(fill="x", padx=10, pady=(16, 10))
        ttk.Label(box, text="为什么不直接扫码？", style="Sub.TLabel"
                  ).pack(anchor="w", **pad)
        ttk.Label(box,
                  text="网易云给网页扫码加了行为验证码风控（8821），"
                       "手机点确认时会被拒。\n"
                       "浏览器登录走的是官方网页流程，不触发这套风控。",
                  style="Muted.TLabel", font=theme.FONT_S, justify="left",
                  wraplength=430).pack(anchor="w", **pad, pady=(2, 8))


    # ---- 标签页 1：扫码 ---------------------------------------------------
    def _build_qr_tab(self, box):
        p = theme.PALETTE
        inner = ttk.Frame(box, style="Panel.TFrame")
        inner.pack(expand=True)

        ttk.Label(inner, text="用网易云音乐 App 扫描下方二维码",
                  style="Sub.TLabel").pack(anchor="w")
        ttk.Label(inner, text="手机端：我的 → 右上角「扫一扫」",
                  style="Muted.TLabel", font=theme.FONT_S).pack(anchor="w", pady=(2, 8))

        self.canvas = tk.Canvas(inner, width=248, height=248, bg="#ffffff",
                                highlightthickness=1,
                                highlightbackground=p["line"])
        self.canvas.pack()

        self.var_msg = tk.StringVar(value="正在申请二维码…")
        ttk.Label(inner, textvariable=self.var_msg, style="Muted.TLabel",
                  font=theme.FONT_S, wraplength=300).pack(pady=(8, 6))

        # 撞 8821 的人第一眼就能看到出路，不用去猜。
        ttk.Label(inner,
                  text="手机上提示「请切换其他登陆方式」？\n"
                       "→ 那是风控（8821），请切到「粘贴 Cookie」页",
                  style="Muted.TLabel", font=theme.FONT_S,
                  wraplength=300, justify="left").pack(anchor="w")

    # ---- 标签页 2：粘贴 Cookie --------------------------------------------
    def _build_cookie_tab(self, box):
        pad = {"padx": 10}

        ttk.Label(box, text="从浏览器复制 Cookie", style="Sub.TLabel"
                  ).pack(anchor="w", pady=(8, 2), **pad)
        ttk.Label(box,
                  text="1. 浏览器打开 music.163.com 并登录\n"
                       "2. 按 F12 → Network，刷新页面\n"
                       "3. 点任意一条 music.163.com 请求\n"
                       "4. 右侧 Request Headers，复制 Cookie 整行",
                  style="Muted.TLabel", font=theme.FONT_S, justify="left"
                  ).pack(anchor="w", **pad, pady=(0, 6))

        # ⚠ 这里必须 fill="x" 而不是 fill="both"+expand：后者会让多行输入框
        # 一路撑到窗口底部，把按钮挤成一条缝（第一版就是这么翻车的）。
        holder = ttk.Frame(box, style="Panel.TFrame")
        holder.pack(fill="x", padx=10)
        self.txt_cookie = tk.Text(holder, height=5, wrap="word",
                                  font=retro_font(theme.FONT_S),
                                  relief="solid", borderwidth=1)
        vs = ttk.Scrollbar(holder, orient="vertical", command=self.txt_cookie.yview)
        self.txt_cookie.configure(yscrollcommand=vs.set)
        self.txt_cookie.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        holder.columnconfigure(0, weight=1)
        skin_log(self.txt_cookie)   # 复用日志区的 tk.Text 上色（只上色，不动布局）

        row = ttk.Frame(box, style="Panel.TFrame")
        row.pack(fill="x", padx=10, pady=(10, 0))
        # width 要给够，否则 "验证并登录" 五个汉字会被折成两行
        self.btn_ck = ttk.Button(row, text="验证并登录", style="Accent.TButton",
                                 width=14, command=self._start_cookie_login)
        self.btn_ck.pack(side="left")

        self.var_ck = tk.StringVar(
            value="提示：MUSIC_U 是 HttpOnly，document.cookie 拿不到，"
                  "必须从请求头里复制。")
        ttk.Label(box, textvariable=self.var_ck, style="Muted.TLabel",
                  font=theme.FONT_S, wraplength=420, justify="left"
                  ).pack(anchor="w", padx=10, pady=(10, 8))

    # ---- 后台线程 ---------------------------------------------------------
    def _fetch_key(self):
        try:
            key, content = self.session.qr_new()
            self._q.put(("key", key, _qr_matrix(content)))
        except Exception as exc:                     # noqa: BLE001
            self._q.put(("fatal", str(exc)))

    def _check_worker(self):
        try:
            code, message = self.session.qr_check(self._key)
            self._q.put(("status", message, code))
        except Exception as exc:                     # noqa: BLE001
            self._q.put(("status", "网络错误，重试中…（%s）" % str(exc)[:40], 0))

    def _cookie_worker(self, raw):
        try:
            ok, msg = self.session.login_with_cookie(raw)
        except Exception as exc:                     # noqa: BLE001
            ok, msg = False, "验证出错：%s" % str(exc)[:80]
        self._q.put(("cookie", ok, msg))

    def _browser_worker(self):
        """后台：开浏览器 → 等登录 → 取 cookie → 验证 → 关浏览器。"""
        bl = nck.BrowserLogin(silent=False)
        self._bl = bl
        try:
            if not bl.start():
                self._q.put(("br", False, bl.error or "浏览器启动失败"))
                return
            self._q.put(("br_stat", "浏览器已打开，请在其中完成登录…"))
            try:
                header = bl.wait_cookie(timeout=300, interval=1.2,
                                        stop=lambda: self._br_stop)
            except RuntimeError as exc:
                if not self._br_stop:
                    self._q.put(("br", False, str(exc)[:100]))
                return
            if not header:
                if not self._br_stop:
                    self._q.put(("br", False, "等了 5 分钟还没登录，已放弃"))
                return
            self._q.put(("br_stat", "已取到登录凭据，正在向网易云核验…"))
            ok, msg = self.session.login_with_cookie(header)
            self._q.put(("br", ok, msg))
        except Exception as exc:                     # noqa: BLE001
            self._q.put(("br", False, "出错：%s" % str(exc)[:100]))
        finally:
            bl.close()
            self._bl = None

    # ---- 主线程 -----------------------------------------------------------
    def _start_browser_login(self):
        if self._br_busy or self._done or self._closed:
            return
        if nck is None:
            self.var_br.set("❌ 浏览器登录模块不可用，请改用「粘贴 Cookie」")
            return
        name, exe = nck.find_browser()
        if not exe:
            self.var_br.set("❌ 没找到 Chrome / Edge，"
                            "请改用「粘贴 Cookie」页")
            return
        self._br_busy = True
        self.btn_br.config(state="disabled")
        self.var_br.set("正在启动 %s…" % name)
        threading.Thread(target=self._browser_worker, daemon=True).start()

    def _start_cookie_login(self):
        if self._cookie_busy or self._closed:
            return
        raw = self.txt_cookie.get("1.0", "end").strip()
        if not raw:
            self.var_ck.set("❌ 输入框是空的，请先粘贴 Cookie")
            return
        self._cookie_busy = True
        self.btn_ck.config(state="disabled")
        self.var_ck.set("正在验证…（会向网易云拉一次账号信息）")
        threading.Thread(target=self._cookie_worker, args=(raw,),
                         daemon=True).start()

    def _finish(self):
        """登录成功：置 ok、停掉扫码轮询、稍等关窗（让人看清昵称）。

        ⚠ 不能拿 ``_closed`` 当"已完成"用 —— ``_close()`` 开头就是
        ``if self._closed: return``，那样置了标志窗口反而关不掉。
        """
        if self._done:
            return
        self._done = True
        self.ok = True
        self.top.after(600, self._close)

    def _tick(self):
        if self._closed or self._done:
            return
        try:
            while True:
                item = self._q.get_nowait()
                kind = item[0]
                if kind == "key":
                    self._key = item[1]
                    self._draw(item[2])
                    self.var_msg.set("等待手机扫码…")
                    self._schedule_check(600)
                elif kind == "fatal":
                    self.var_msg.set("申请二维码失败：%s" % item[1])
                elif kind == "status":
                    _, message, code = item
                    self.var_msg.set(message)
                    if code == 803:
                        self._finish()
                    elif code == 8821:
                        # 风控：别再傻等，直接把焦点送过去让用户粘 Cookie。
                        self.var_msg.set(message)
                        self.nb.select(1)
                    elif code == 800:
                        self.var_msg.set("二维码已过期，请关掉重开")
                    elif code == 0:
                        self._schedule_check(4000)   # 出错退避，别打死接口
                    else:
                        self._schedule_check(1500)
                elif kind == "cookie":
                    _, ok, msg = item
                    self._cookie_busy = False
                    self.btn_ck.config(state="normal")
                    self.var_ck.set(("✅ 登录成功：%s" % msg) if ok
                                    else ("❌ %s" % msg))
                    if ok:
                        self._finish()
                elif kind == "br_stat":
                    self.var_br.set(item[1])
                elif kind == "br":
                    _, ok, msg = item
                    self._br_busy = False
                    self.btn_br.config(state="normal")
                    self.var_br.set(("✅ 登录成功：%s" % msg) if ok
                                    else ("❌ %s" % msg))
                    if ok:
                        self._finish()
        except queue.Empty:
            pass
        self._after = self.top.after(120, self._tick)

    def _schedule_check(self, delay_ms):
        if self._closed or self._done:
            return
        self.top.after(delay_ms, lambda: threading.Thread(
            target=self._check_worker, daemon=True).start())

    def _draw(self, matrix):
        n = len(matrix)
        cell = max(3, 248 // max(1, n))      # 与 _build_qr_tab 里的 canvas 尺寸一致
        size = n * cell
        self.canvas.config(width=size, height=size)
        self.canvas.delete("all")
        for y, row in enumerate(matrix):
            for x, on in enumerate(row):
                if on:
                    self.canvas.create_rectangle(
                        x * cell, y * cell, (x + 1) * cell, (y + 1) * cell,
                        fill="#000000", outline="")

    def _close(self):
        if self._closed:
            return
        self._closed = True
        # 先让后台线程停手，再关浏览器。⚠ 顺序不能反：先 close 浏览器的话，
        # 后台线程正在 wait_cookie 里，会把「连不上」当成网络错误继续重试。
        self._br_stop = True
        bl = self._bl
        if bl is not None:
            # close() 里有 proc.wait(4)，不能放主线程 —— 否则关窗要卡 4 秒。
            threading.Thread(target=bl.close, daemon=True).start()
        if self._after is not None:
            try:
                self.top.after_cancel(self._after)
            except Exception:                        # noqa: BLE001
                pass
            self._after = None
        try:
            self.top.destroy()
        except Exception:                            # noqa: BLE001
            pass

    @classmethod
    def ask(cls, parent, session):
        """模态打开，返回是否登录成功。

        ⚠ 会**先静默**尝试从内置浏览器 profile 恢复登录态（实测约 0.7s），
        成功就直接返回、**不建窗口** —— 这是「只需登录一次」的关键：
        用户第二次点上传时看到的是状态栏一句「正在恢复登录态…」，
        而不是又弹一个登录框。
        """
        if nck is not None and nck.has_profile():
            header = ""
            try:
                header = nck.quick_cookie()
            except Exception:                        # noqa: BLE001
                header = ""
            if header:
                try:
                    ok, _msg = session.login_with_cookie(header)
                except Exception:                    # noqa: BLE001
                    ok = False
                if ok:
                    return True
        dlg = cls(parent, session)
        parent.wait_window(dlg.top)
        return dlg.ok


# ---------------------------------------------------------------------------
# 云盘上传执行器（v4.23：主窗「输出目录」与下载窗「WAV 目录」共用）
# ---------------------------------------------------------------------------
# 为什么抽出来：v4.19 那版整套逻辑（登录校验 / 体积确认 / 逐首上传 / 阶段
# 日志）全写在 App 里，下载窗要复用得把 130 行连同 msgq 一起复制一遍。
# 复制体迟早会漂移（本项目已有两窗同构代码打错靶的先例），所以这里把
# **与 Tk 无关的部分**提出来，两个窗口只保留「按钮 + 队列翻译」。
_CLOUD_MISSING_MSG = (
    "云盘上传需要 soundfile / pycryptodome / segno。\n\n"
    "pip install soundfile pycryptodome segno\n\n"
    "装好后重启本程序。")


# v5.0-qt: 这一组清单整理函数搬到了 cloud_upload.py（Qt 版也要用，
# 而 Qt 版不能 import 本模块）。这里改成引用，保持单一事实源。
from cloud_upload import (  # noqa: E402
    collect_wavs, is_merged_wav, split_merged,
    fmt_bytes as _fmt_bytes, sum_size as _sum_size)


def _ask_cloud_scope(parent, merged, tracks):
    """弹窗三选一。返回选中的路径列表；用户取消返回 ``None``。

    ⚠ 这是个**阻塞式模态窗**（``wait_window``），只能从主线程调 ——
    两个 ``upload_cloud`` 都在按钮回调里，满足这个前提。
    """
    m_bytes, t_bytes = _sum_size(merged), _sum_size(tracks)
    # 内容是否"重复"：合并文件就是分轨拼出来的，字节数应当接近
    # （合并多出的那部分是曲间静音的 0.5s × N）。差得远说明用户只对
    # 部分源做了合并 —— 那种情况下不能说"重复"，得让用户自己决定。
    near = (m_bytes > 0 and t_bytes > 0
            and abs(m_bytes - t_bytes) <= max(m_bytes, t_bytes) * 0.25)
    hint = ("两种内容基本重复（合并文件就是这些分轨拼起来的），"
            "全传等于占双倍空间。" if near
            else "合并文件与分轨的体量对不上（可能只有部分源做了合并），"
                 "请按需要选。")

    top = tk.Toplevel(parent)
    top.title("选择上传内容")
    top.resizable(False, False)
    top.transient(parent)
    top.minsize(540, 0)                  # 太窄的话说明文字会挤成一坨
    try:
        bg = ttk.Style().lookup("TFrame", "background")
        if bg:
            top.configure(bg=bg)
    except tk.TclError:
        pass

    choice = tk.IntVar(master=top, value=0)
    body = ttk.Frame(top, padding=(18, 14, 18, 6))
    body.pack(fill="both", expand=True)

    ttk.Label(body, text="这个目录里既有合并文件，也有分轨",
              font=theme.FONT_B).pack(anchor="w")
    ttk.Label(
        body,
        text="· 合并文件 %d 个，共 %s　（每个源一个整轨）\n"
             "· 分轨　　 %d 个，共 %s　（每首一个文件）"
             % (len(merged), _fmt_bytes(m_bytes),
                len(tracks), _fmt_bytes(t_bytes)),
        justify="left").pack(anchor="w", pady=(6, 8))

    if hint:
        ttk.Label(body, text=hint, justify="left", wraplength=520,
                  style="Window.Muted.TLabel"
                  ).pack(anchor="w", pady=(0, 10))

    opts = [
        ("只传合并文件（%d 个）— 整张连着听" % len(merged), list(merged)),
        ("只传分轨（%d 个）— 云盘里能单曲播放" % len(tracks), list(tracks)),
        ("全都传（%d 个）— 内容会重复" % (len(merged) + len(tracks)),
         list(merged) + list(tracks)),
    ]
    for idx, (text, _lst) in enumerate(opts):
        ttk.Radiobutton(body, text=text, value=idx, variable=choice
                        ).pack(anchor="w", pady=2)

    bottom = ttk.Frame(top, padding=(18, 4, 18, 14))
    bottom.pack(fill="x")
    result = {"picked": None}

    def _ok(_evt=None):
        result["picked"] = opts[choice.get()][1]
        top.destroy()

    def _cancel(_evt=None):
        result["picked"] = None
        top.destroy()

    ttk.Button(bottom, text="取消", width=10, command=_cancel).pack(side="right",
                                                                   padx=(6, 0))
    ttk.Button(bottom, text="开始上传", width=12,
               command=_ok).pack(side="right")
    top.bind("<Return>", _ok)
    top.bind("<Escape>", _cancel)
    top.protocol("WM_DELETE_WINDOW", _cancel)

    top.update_idletasks()
    # 居中到父窗（父窗可能是主窗，也可能是下载窗）
    try:
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = top.winfo_width(), top.winfo_height()
        x = px + max(0, (pw - w) // 2)
        y = py + max(0, (ph - h) // 3)
        top.geometry("+%d+%d" % (x, y))
    except tk.TclError:
        pass
    top.grab_set()
    top.focus_force()
    parent.wait_window(top)
    return result["picked"]


def plan_cloud_upload(parent, wavs):
    """决定这次到底传哪些 WAV，返回列表；用户取消返回 ``None``。

    ============================ 为什么需要这一层 ============================
    v4.24 加了「多轨合并」后，``collect_wavs`` 只看扩展名，会把
    ``X（合并）.wav`` 和它的 44 个分轨**一起**收进上传列表。合并文件就是
    那些分轨拼出来的，全传等于把同一张专辑传两遍，云盘空间直接翻倍，
    而且云端列表里会出现一堆「X（合并）」夹在曲目中间。

    所以上传前把选择权交回用户：只传合并 / 只传分轨 / 全传。**默认
    「只传合并文件」**——合并是用户自己勾出来的显式意图。

    只有一种形态时不弹窗（老行为完全不变），避免每次上传都打扰。
    """
    merged, tracks = split_merged(wavs)
    if not merged or not tracks:
        return list(wavs or ())
    return _ask_cloud_scope(parent, merged, tracks)


def ensure_cloud_session(parent, log, status=None):
    """拿到一个**真能拉到账号信息**的 session；拿不到返回 None。

    ⚠ 只看 jar 里有没有 MUSIC_U 是不够的 —— 存下来的 Cookie 可能早过期，
    那样会一路走到上传才炸（v4.20 踩过）。所以这里真拉一次 ``refresh_profile``。
    ``status`` 是可选的「写状态行」回调，用来在静默恢复登录态那 0.7s 里
    给用户一点反馈，别看着像卡死。
    """
    if ncm is None:
        messagebox.showerror("缺少依赖", _CLOUD_MISSING_MSG, parent=parent)
        return None
    sess = ncm.NeteaseSession()
    sess.loads(ncm.load_cookie())
    authed = False
    if sess.is_logged_in():
        try:
            authed = bool(sess.refresh_profile())
        except Exception:                        # noqa: BLE001
            authed = False
    if authed:
        log("☁ 已登录：%s" % (sess.user_name or "（未取到昵称）"))
        return sess

    log("☁ 需要登录网易云")
    if status is not None and nck is not None and nck.has_profile():
        try:
            status("☁ 正在恢复登录态…")
            parent.update_idletasks()
        except Exception:                        # noqa: BLE001
            pass
    granted = NeteaseLoginDialog.ask(parent, sess)
    if status is not None:
        try:
            status("")
        except Exception:                        # noqa: BLE001
            pass
    if not granted:
        log("☁ 已取消登录，上传中止")
        return None
    try:
        ncm.save_cookie(sess.dumps())
    except Exception:                            # noqa: BLE001
        pass
    log("☁ 登录成功：%s" % (sess.user_name or "（未取到昵称）"))
    return sess


def confirm_cloud_upload(parent, wavs):
    """体积预估 + 版权提醒，返回是否继续。"""
    # 预估体积：FLAC 大约是 WAV 的 35%（实测纯方波 13%，带噪声的 34%，
    # 取个偏保守的值让用户别被容量吓到）。
    try:
        need_mb = sum(os.path.getsize(p) for p in wavs) / 1e6 * 0.4
    except OSError:
        need_mb = 0.0
    return messagebox.askyesno(
        "确认上传",
        "将把 %d 个 WAV 转成 FLAC 上传到你的网易云云盘。\n\n"
        "预计占用约 %.0f MB。\n"
        "免费额度通常 5 GB，超出需要会员扩容。\n\n"
        "注意：平台已有版权的曲目可能被拒绝或匹配成平台版本。\n\n继续？"
        % (len(wavs), need_mb), parent=parent)


# --------------------------------------------------------------------------
# 账号：看当前是谁 / 换一个 / 退出（v4.26）
# --------------------------------------------------------------------------
def cloud_account_label():
    """当前保存的账号昵称 —— **只从本地文件读，不联网**。

    菜单必须秒开，为了显示一个名字去拉接口是不能接受的；这也解释了为什么
    这里读的是 ``netease.json`` 里的 ``user_name``（登录时写进去的），
    而不是现拉 ``refresh_profile()``。
    """
    if ncm is None:
        return ""
    try:
        return str((ncm.load_cookie() or {}).get("user_name") or "")
    except Exception:                            # noqa: BLE001
        return ""


class SwitchAccountDialog:
    """「切换账号 / 退出登录」的进度窗，两种模式共用同一套线程与队列。

    ==================== 为什么必须单独一个窗 ====================
    ``NeteaseLoginDialog`` 的三页都在做同一件事：**把 cookie 拿回来**。
    而换号的第一步是**先把已有登录态清掉** —— 不清的话，浏览器带着旧号的
    cookie 一起来，``quick_cookie()`` 立刻命中，用户看到的还是原来那个号，
    连登录框都不会出现（这就是「怎么点都换不了」的原因）。

    方向相反，硬塞进登录窗会让「登录」和「换号」两条流程互相干扰，
    所以这里独立实现，只在最后一步复用 ``NeteaseSession``。
    """

    def __init__(self, parent, mode="switch"):
        self.mode = mode
        self.ok = False
        self.user = ""
        self._stop = False
        self._done = False
        self._closed = False
        self._q = queue.Queue()
        self._after = None

        self.top = tk.Toplevel(parent)
        self.top.title("切换网易云账号" if mode == "switch" else "退出网易云登录")
        self.top.resizable(False, False)
        self.top.transient(parent)
        self.top.minsize(520, 0)             # 太窄说明文字会挤成一坨
        # ⚠ 底色一律用**同一个**颜色，并且文字用原生 tk.Label 显式带上 bg。
        # 用 ttk.Frame + ttk.Label 时，Label 的背景取的是 style 默认值而不是
        # 容器色，窗口里会出现一块明显的色差（v4.26 前两版截图都是这样）。
        _bg = theme.PALETTE["panel"]
        _fg = theme.PALETTE["text"]
        self.top.configure(bg=_bg)

        body = tk.Frame(self.top, bg=_bg)
        body.pack(fill="both", expand=True, padx=18, pady=14)

        who = cloud_account_label() or "（未登录）"
        if mode == "switch":
            intro = ("当前账号：%s\n\n"
                     "接下来会做三件事：\n"
                     "① 清空本机保存的登录态（含内置浏览器的 cookie）\n"
                     "② 打开浏览器窗口，请在里面登录要换成的那个账号\n"
                     "③ 登录完成后自动保存并关窗，无需回到这里操作" % who)
        else:
            intro = ("当前账号：%s\n\n"
                     "只清掉本机保存的登录态，云端文件一个都不动。\n"
                     "下次上传时会重新要求登录。" % who)

        tk.Label(body, text=intro, bg=_bg, fg=_fg, font=theme.FONT_S,
                 justify="left", wraplength=470).pack(anchor="w")

        self.var_status = tk.StringVar(master=self.top, value="准备中…")
        tk.Label(body, textvariable=self.var_status, bg=_bg, fg=_fg,
                 font=theme.FONT_S, justify="left", wraplength=470
                 ).pack(anchor="w", pady=(12, 0))

        bottom = tk.Frame(self.top, bg=_bg)
        bottom.pack(fill="x", padx=18, pady=(0, 14))
        self.btn_main = ttk.Button(bottom, text="取消", width=10,
                                   command=self._on_button)
        self.btn_main.pack(side="right")

        self.top.protocol("WM_DELETE_WINDOW", self._on_button)
        self.top.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.top.winfo_width(), self.top.winfo_height()
            self.top.geometry("+%d+%d" % (px + max(0, (pw - w) // 2),
                                          py + max(0, (ph - h) // 3)))
        except tk.TclError:
            pass
        self.top.after(80, self._begin)

    # ---- 主线程 -----------------------------------------------------------
    def _begin(self):
        if self._closed:
            return
        try:
            self.top.grab_set()
            self.top.focus_force()
        except tk.TclError:
            pass
        threading.Thread(target=self._worker, daemon=True).start()
        self._after = self.top.after(180, self._poll)

    def _poll(self):
        """消费工作线程的事件。**Tk 只在这里被碰**（项目铁律）。"""
        if self._closed:
            return
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "ready":
                    self.var_status.set(
                        "浏览器已打开。请在里面登录要使用的账号。\n"
                        "登录成功后本窗口会自动关闭（最多等 4 分钟）。")
                elif kind == "done":
                    self._finish(*payload)
                    return
        except queue.Empty:
            pass
        self._after = self.top.after(180, self._poll)

    def _finish(self, ok, msg, err):
        self._done = True
        self.ok = bool(ok)
        if ok:
            self.user = msg
            self.var_status.set("✅ 已切换为：%s" % msg if self.mode == "switch"
                                else "✅ 已退出登录")
            self.btn_main.config(text="完成", command=self._close)
            self.top.after(1200, self._close)
        else:
            self.var_status.set("❌ %s" % (err or msg or "操作失败"))
            self.btn_main.config(text="关闭", command=self._close)
            try:
                self.top.grab_release()
            except tk.TclError:
                pass

    def _on_button(self):
        if self._done:
            self._close()
            return
        self._stop = True                    # wait_cookie 每 1s 检查一次
        self.var_status.set("正在取消…（浏览器窗口会跟着关掉）")
        try:
            self.btn_main.config(state="disabled")
        except tk.TclError:
            pass
        self.top.after(1800, self._close)    # 兜底：worker 卡住也要能关

    def _close(self):
        if self._closed:
            return
        self._closed = True
        if self._after is not None:
            try:
                self.top.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        try:
            self.top.destroy()
        except tk.TclError:
            pass

    # ---- 工作线程（**只碰网络/浏览器/文件，不碰 Tk**）---------------------
    def _worker(self):
        try:
            if self.mode == "logout":
                file_ok = True
                if ncm is not None:
                    file_ok = ncm.clear_cookie()
                ok, msg = nck.clear_browser_cookies()
                if ok and file_ok:
                    self._q.put(("done", (True, "", "")))
                else:
                    self._q.put(("done", (False, "", msg or "删除登录文件失败")))
                return

            # 换号：先删旧号的存档 —— 中途取消也不会留着旧号，
            # 否则下次上传又会静默恢复成原来那个账号。
            if ncm is not None:
                ncm.clear_cookie()
            res = nck.switch_account(
                on_ready=lambda: self._q.put(("ready", None)),
                stop=lambda: self._stop)
            if not res.get("ok"):
                self._q.put(("done", (False, "", res.get("error") or "未取到登录态")))
                return
            sess = ncm.NeteaseSession()
            sess.set_cookie_header(res.get("header", ""))
            try:
                sess.refresh_profile()       # 取昵称/userId，失败不影响上传
            except Exception:                # noqa: BLE001
                pass
            try:
                ncm.save_cookie(sess.dumps())
            except Exception:                # noqa: BLE001
                pass
            self._q.put(("done", (True, sess.user_name or "（未取到昵称）", "")))
        except Exception as exc:             # noqa: BLE001
            self._q.put(("done", (False, "", str(exc)[:160])))

    # ---- 入口 -------------------------------------------------------------
    @classmethod
    def ask(cls, parent, mode="switch"):
        """模态打开；返回对话框实例（读 ``.ok`` / ``.user``）。"""
        if nck is None or ncm is None:
            messagebox.showerror("缺少依赖", _CLOUD_MISSING_MSG, parent=parent)
            return None
        dlg = cls(parent, mode)
        parent.wait_window(dlg.top)
        return dlg


def popup_cloud_account_menu(widget, ev, parent, log, owner=None):
    """「☁ 上传云盘」右边那个「▾」的菜单：看当前账号 / 换一个 / 退出。

    ⚠ ``ev`` 允许为 ``None``：按钮点开时没有鼠标事件，就在按钮正下方弹。
    ``owner`` 是持有 ``_cloud_busy`` 的那个对象（主窗 App / 下载窗），
    用来挡住「上传中换号」—— 换号会清 cookie，正在传的那首会当场失败。
    """
    m = tk.Menu(widget, tearoff=0)
    who = cloud_account_label()
    m.add_command(label="☁ 当前账号：%s" % (who or "未登录（下次上传时登录）"),
                  state="disabled")
    m.add_separator()
    m.add_command(label="⇄ 切换账号…",
                  command=lambda: _run_account_action(owner, parent, log, "switch"))
    m.add_command(label="⎋ 退出登录",
                  command=lambda: _run_account_action(owner, parent, log, "logout"))
    try:
        if ev is None:
            x, y = widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height()
        else:
            x, y = ev.x_root, ev.y_root
        m.tk_popup(x, y)
    except tk.TclError:
        pass
    finally:
        try:
            m.grab_release()
        except tk.TclError:
            pass


def _run_account_action(owner, parent, log, mode):
    """菜单项的实际动作。"""
    if nck is None or ncm is None:
        messagebox.showerror("缺少依赖", _CLOUD_MISSING_MSG, parent=parent)
        return
    # ⚠ 上传中不能换号：清 cookie 会让正在传的那一首当场失败，
    # 而且云端会留下已登记但没提交的 key。
    if owner is not None and getattr(owner, "_cloud_busy", False):
        messagebox.showinfo("提示", "正在上传，等这一批传完再换账号。",
                            parent=parent)
        return
    # 全新用户（既没存档也没浏览器登录态）没有「旧号」可清，直接登录更省事。
    # ⚠ 判据必须带上 has_profile()：只删了 netease.json 时，浏览器一开
    # 还是旧号，走普通登录会被 quick_cookie 静默恢复成原来那个人。
    fresh = (not cloud_account_label() and not nck.has_profile())
    if mode == "switch" and fresh:
        sess = ncm.NeteaseSession()
        if NeteaseLoginDialog.ask(parent, sess):
            ncm.save_cookie(sess.dumps())
            log("☁ 登录成功：%s" % (sess.user_name or "（未取到昵称）"))
        return
    dlg = SwitchAccountDialog.ask(parent, mode)
    if dlg is None or not dlg.ok:
        return
    if mode == "switch":
        log("☁ 已切换账号：%s" % (dlg.user or "（未取到昵称）"))
    else:
        log("☁ 已退出登录（云端文件不受影响）")


# v5.0-qt: CloudUploader 已搬到 cloud_upload.py —— Qt 版也要用它，而 Qt 版
# 不能 import 本模块（会把 tkinter 整个拖进 PyInstaller 依赖图）。
# 这里保留同名引用，老代码一行不用改。
from cloud_upload import CloudUploader  # noqa: E402


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_TITLE}  v{APP_VER}")
        # 主题: 先按上次选择定调色板, 再 install（顺序不能反 —— install 读的
        # 就是 PALETTE 当前值）。install 之后所有 ttk 控件(含子窗口)自动生效。
        self._theme_key = load_theme_key()
        theme.set_current(self._theme_key)
        self.style = theme.install(root)
        theme.app_icon(root)
        # 标题栏深色: 等窗口 map 出来再设(DWM 属性在 map 后设才不被重置)。
        root.after(50, lambda: apply_dark_title_bar(root))
        # 高度: 810 -> 1020。不是 810+92 的算术 —— 实测本主题下内容自然高度是 996px
        # (Card.TLabelframe 每块自带 10/8 内边距, 6 块共 +96px; 按钮 33px 高)。
        # 照 902 开窗会把「日志」这块唯一可伸缩的区域挤成 1px(实测 winfo_height=1),
        # 所以按实测值给: 1020 让日志能站住 5 行左右, 最小 980 仍保留 2~3 行。
        # 窗口高度不是 810+92 的算术, 也不是拍脑袋的 1020 —— 实测本主题下内容自然
        # 高度 948px(Card.TLabelframe 内边距 10/5 ×6 块 + 33px 高的按钮 + 80px 横幅),
        # 而 902 的窗口会把「日志」这块唯一可伸缩的区域挤成 1px(实测 winfo_height=1)。
        # 也不能死给一个高度后交给窗口管理器居中: Windows 按**整屏**居中而不按工作区,
        # 1080p 上会被摆到 y=57, 底边压到任务栏(上沿 1032), 日志最下面一截被盖住。
        # 所以自己算位置——水平居中、贴顶, 高度按「屏幕高 - 标题栏31 - 边框8 - 任务栏48」钳制。
        # 980 -> 1020: 加上首屏欢迎块(3 行)后日志区需要更多高度才不显得空;
        # 1080p 屏会被 avail_h 自动钳到 993, 不会超出工作区。
        want_w, want_h = 1260, 1020
        avail_h = root.winfo_screenheight() - 87
        height = max(780, min(want_h, avail_h))
        pos_x = max(0, (root.winfo_screenwidth() - want_w) // 2)
        root.geometry(f"{want_w}x{height}+{pos_x}+0")
        root.minsize(1080, max(780, height - 40))

        self.sources = []   # [{"label","path","kind","iid","fmt",...}]
        self.msgq = queue.Queue()

        # 试听播放器（v4.7）。player 为 None 表示音频子系统不可用，
        # 界面照常工作，只是 ▶ 点了会提示不可用。
        self.player = None
        self._play_path = ""        # 当前载入试听的源文件路径
        self._play_track = 0        # 当前轨序号
        self._track_list = []       # [(pos, label)]，多轨时给 ◀ ▶ 用
        self._loading = False       # 正在后台解码（防重复点击）
        self._after_player = None   # 播放进度轮询句柄
        try:
            import player as _pm
            self.player = _pm.PcmPlayer()
        except Exception:                                    # noqa: BLE001
            self.player = None
        self.cancel_event = threading.Event()
        self.worker = None

        # ---- 音源列表状态 ----
        # iid 必须单调自增, 绝不能用 sources 的下标: remove_selected 删中间行
        # 会让后面所有下标平移, tree.delete(idx) 与 sources.pop(idx) 直接错位
        # (设计 §7.4)。_idx_of_iid 负责 iid -> 下标的双向映射。
        self._src_seq = 0
        self._idx_of_iid = {}
        # _kind_of_file 只做判定、不打日志(它是 staticmethod, 没有 self.log),
        # 冲突/不支持文案攒在这里, 由 add_paths 统一输出, 顺序才是拖放顺序。
        self._pending_notes = []

        self._probe_cache = {}      # (路径, mtime_ns, 大小) -> ChipInfo 或 SPC 秒数
        self._info_job = None       # 合并刷新用的 after id
        self._dlg = None            # 音源下载窗口(单例复用)
        self._closing = False
        self._run_t0 = time.time()  # 本轮转换起始时刻(算已用/剩余)


        self._build_ui()
        self._welcome_log()                   # 首屏欢迎块, 避免日志区一片空白
        self.root.update_idletasks()          # 先建好窗口句柄, 拖放才能挂上
        self.dropq = queue.Queue()

        argv = sys.argv[1:]
        prev_crash, crash_streak = take_session_lock()
        want_drop = "--no-drop" not in argv
        if prev_crash and want_drop:
            # v4.31: **只提示、不降级**。v4.30 及以前这里会 want_drop=False，
            # 结果是「一次异常退出 -> 以后每次启动都不挂拖放」，而且提示只写
            # 在日志里、又不会自愈 —— 用户看到的就是「拖拽突然坏了」。
            # 详见 _CRASH_STREAK_TTL 上方的注释。
            self.log("ℹ 上次没有正常退出（本次仍挂载拖放）。")
            self.log("   若拖入文件时程序闪退，请加 --no-drop 参数启动。")
            dbg(f"上次异常退出（streak={crash_streak}），仍挂载拖放")
        self.drop_enabled = enable_file_drop(self.root, self.dropq) if want_drop else False
        dbg(f"拖放挂载: {self.drop_enabled} (want={want_drop}, prev_crash={prev_crash})")
        if want_drop and not self.drop_enabled:
            self.log("提示: 当前环境不支持拖放, 请用「＋ 添加音源…」按钮添加")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_drop = self.root.after(120, self._poll_drop)
        self._after_msg = self.root.after(100, self._poll_queue)

    def _on_close(self):
        # 释放试听播放器（停设备），再走原有清理
        try:
            if getattr(self, "player", None) is not None:
                self.player.close()
                self.player = None
        except Exception:                                    # noqa: BLE001
            pass
        try:
            if getattr(self, "_after_player", None) is not None:
                self.root.after_cancel(self._after_player)
                self._after_player = None
        except Exception:                                    # noqa: BLE001
            pass
        self._closing = True
        # _info_job 也是 after 回调, 必须一起取消: 窗口销毁后它再触发就是
        # `invalid command name "..._run_scheduled_info"`(Tk 会把它打到 stderr,
        # 看起来像崩溃)。这与 _after_drop / _after_msg 是同一类问题。
        for attr in ("_after_drop", "_after_msg", "_info_job"):
            aid = getattr(self, attr, None)
            if aid is not None:
                try:
                    self.root.after_cancel(aid)
                except Exception:                    # noqa: BLE001
                    pass
                setattr(self, attr, None)
        release_session_lock()
        banner = getattr(self, "banner", None)
        if banner is not None:
            try:
                banner.stop()
            except Exception:                        # noqa: BLE001
                pass
        try:
            self.root.destroy()
        except Exception:                            # noqa: BLE001
            pass

    # ---- 拖放轮询 -----------------------------------------------------------
    def _poll_drop(self):
        """从拖放队列取路径。窗口过程里不能碰 Tk, 只能在这里处理。"""
        try:
            while True:
                paths = self.dropq.get_nowait()
                try:
                    self._on_drop(paths)
                except Exception as e:               # noqa: BLE001
                    self.log(f"❌ 处理拖放内容失败: {e}")
                    traceback.print_exc()
        except queue.Empty:
            pass
        except Exception:                            # noqa: BLE001
            traceback.print_exc()
        if not self._closing:
            self._after_drop = self.root.after(120, self._poll_drop)

    # ---- UI ---------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 3}

        # 顶部复古横幅(固定 92px, 不伸缩): 渐变天空 + 扫描线 + 像素字。
        # 它只占高度, 不参与 expand —— 日志区依旧是唯一可伸缩的控件。
        self.banner = theme.RetroBanner(self.root, height=80)
        self.banner.pack(fill="x", padx=0, pady=(0, 4))
        self.banner.start()

        # 两列（v4.6）：左 = 主流程（①~④ + 按钮 + 进度），右 = 日志。
        # 用 PanedWindow 让用户能拖分隔条 —— 日志固定宽度时长路径（转出的
        # WAV 全路径）显示不全，与下载窗口是同一套取舍。
        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True)
        self._body = body
        leftcol = ttk.Frame(body)
        # 500 而不是 420：播放条第一行要放下「▶ ■ + 进度条 + 时间 + 🔊 + 滑块」，
        # 实测 420 时滑块只剩 37px（可用宽度被 padding 吃掉后只有 366）。
        logcol = ttk.Frame(body, width=500)
        logcol.pack_propagate(False)
        body.add(leftcol, weight=1)
        body.add(logcol, weight=0)

        # ① 音源
        frm_src = ttk.LabelFrame(leftcol, text=" ① 音源 ",
                                 style="Card.TLabelframe")
        # v4.6：日志移到右列后，左列需要一个 expand 的控件吸收多余高度 ——
        # 否则底部留一大片空白。让音源列表吃这个空间最合理（列表越大越好用）。
        frm_src.pack(fill="both", expand=True, **pad)
        # 「把文件拖进来…」这句挪进了列表空态（_make_empty_state）：
        # 有文件时它是废话，没文件时它出现在该出现的位置。
        self._ui_list_create(frm_src)
        btns = ttk.Frame(frm_src, style="Panel.TFrame")
        btns.pack(side="left", fill="y", padx=4, pady=6)
        # 四个按钮一律 grid 同一列 + sticky="ew"：宽度取最宽的那个，
        # 不会出现「添加音源比清空宽一截」的错位（v4.28 修）。
        # ⚠ 图标只用几何符号（＋ ↓ − ✕）：emoji 是彩色位图，各系统渲染不一，
        #   和像素皮肤打架；几何符号是单色字形，任何字体下都对齐。
        btns.columnconfigure(0, weight=1)
        ttk.Button(btns, text="＋ 添加音源…", style="TButton",
                   command=self.add_sources).grid(
                       row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(btns, text="↓ 下载音源…", style="TButton",
                   command=self.open_download).grid(
                       row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(btns, text="− 移除选中", style="Small.TButton",
                   command=self.remove_selected).grid(
                       row=2, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(btns, text="✕ 清空", style="Small.TButton",
                   command=self.clear_sources).grid(
                       row=3, column=0, sticky="ew")
        ttk.Label(btns, text="支持多选", style="Muted.TLabel",
                  font=theme.FONT_S,
                  justify="left").grid(row=4, column=0, sticky="w", pady=(6, 0))

        self.var_info = tk.StringVar(value="尚未添加音源")
        # 直接贴在窗口 bg 上的标签必须用 Window.* 系样式(background=bg),
        # 否则会带出 panel 底色的方块。
        ttk.Label(leftcol, textvariable=self.var_info,
                  style="Window.Accent.TLabel").pack(anchor="w", padx=18)
        # v4.28: 原来这里是三段说明（输出规则 / 单声道 / 多轨），占了 69px，
        # 把底部的状态行挤出了窗口（实测 leftcol 需 946、可用 936）。
        # 收敛成一行：只留"输出到底落在哪"这一条用户真正会找的信息，
        # 声道与多轨的说明在转换日志里本来就会逐条报。
        ttk.Label(
            leftcol,
            text="输出目录: 添加文件夹时自动设为「源文件夹名+output」；"
                 "结果平铺在同一目录，不建子目录",
            style="Window.Muted.TLabel", font=theme.FONT_S,
        ).pack(anchor="w", padx=18)


        # ② 输出格式
        fmt = ttk.LabelFrame(leftcol, text=" ② 输出格式 ",
                             style="Card.TLabelframe")
        fmt.pack(fill="x", **pad)
        self.var_cd = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            fmt, variable=self.var_cd,
            text="音频 CD 镜像 (BIN+CUE) — 用 ImgBurn 烧 CD-R, 普通 CD 机播放",
        ).pack(anchor="w", padx=10, pady=2)
        self.var_wav = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            fmt, variable=self.var_wav,
            text="WAV (Pico-Plus) — 16-bit/44.1kHz/立体声, 拷到 SD 卡菜单播放",
        ).pack(anchor="w", padx=10, pady=2)
        # v4.24: 多轨合并。NSF / GBS / VGM / HES 一个文件里几十轨，转出来就是
        # 几十个 WAV；勾上它**额外**再拼一个整轨文件（分轨全留，方便单独听）。
        # 依赖上面的 WAV 勾选 —— 没勾 WAV 时置灰（见 _sync_merge_state）。
        self.var_merge = tk.BooleanVar(value=False)
        self.cb_merge = ttk.Checkbutton(
            fmt, variable=self.var_merge,
            text="多轨合并成单文件 — 每个音源除分轨外，另出一个"
                 "「源名（合并）.wav」，曲间 0.5 秒静音",
        )
        self.cb_merge.pack(anchor="w", padx=10, pady=2)
        self.var_wav.trace_add("write", lambda *_a: self._sync_merge_state())
        self._sync_merge_state()

        # ③ 回放与淡出
        # 标题从「SPC 专属选项」改成通用表述: NSF/GBS/VGM/HES 同样要决定
        # "放多久" 与 "怎么淡出"(PRD UI-4)。
        spc = ttk.LabelFrame(leftcol, text=" ③ 回放与淡出 ",
                             style="Card.TLabelframe")
        spc.pack(fill="x", **pad)
        ttk.Label(spc, text="播放长度与速度补偿仅对 SPC 生效；"
                             "「默认单遍」仅对 NSF / GBS / VGM / HES 等芯片音源生效",
                  style="Muted.TLabel", font=theme.FONT_S,
                  justify="left").pack(anchor="w", padx=10)
        row = ttk.Frame(spc, style="Panel.TFrame")
        row.pack(fill="x", padx=10, pady=2)
        ttk.Label(row, text="SPC 播放长度:").pack(side="left")
        self.var_policy = tk.StringVar(value=spc_wav.DEFAULT_POLICY_LABEL)
        self.cb_policy = ttk.Combobox(row, textvariable=self.var_policy, width=32,
                                     state="readonly",
                                     values=tuple(spc_wav.POLICIES))
        self.cb_policy.pack(side="left", padx=6)
        ttk.Label(row, text="速度补偿:").pack(side="left", padx=(10, 0))
        self.var_speed = tk.StringVar(value="100")
        self.cb_speed = ttk.Combobox(row, textvariable=self.var_speed, width=6,
                                     state="normal",
                                     values=("100", "110", "115", "120", "125",
                                             "130", "138", "150", "200"))
        self.cb_speed.pack(side="left", padx=6)
        ttk.Label(row, text="%").pack(side="left")

        row2 = ttk.Frame(spc, style="Panel.TFrame")
        row2.pack(fill="x", padx=10, pady=2)
        ttk.Label(row2, text="默认单遍:").pack(side="left")
        self.var_chip_secs = tk.StringVar(value=str(int(CHIP_DEFAULT_SECS)))
        self.spin_chip_secs = ttk.Spinbox(row2, textvariable=self.var_chip_secs,
                                          width=7, from_=CHIP_SECS_MIN,
                                          to=CHIP_SECS_MAX, increment=15)
        self.spin_chip_secs.pack(side="left", padx=6)
        ttk.Label(row2, text="秒   （芯片音源读不到自带时长时按这个截取，"
                             f"尾部自动 {CHIP_FADE_SECS:g}s 淡出）",
                  style="Muted.TLabel", font=theme.FONT_S).pack(side="left")

        row3 = ttk.Frame(spc, style="Panel.TFrame")
        row3.pack(fill="x", padx=10, pady=(0, 2))
        ttk.Label(row3, text="单曲最长:").pack(side="left")
        self.var_max = tk.StringVar(value="0")
        ttk.Spinbox(row3, textvariable=self.var_max, width=7,
                    from_=0, to=600, increment=15).pack(side="left", padx=6)
        ttk.Label(row3, text="秒   （全局生效，0 表示不截断；44.1kHz 下体积只跟"
                             "时长有关，截断处自动 1.5s 淡出）",
                  style="Muted.TLabel", font=theme.FONT_S).pack(side="left")

        # 逐源策略说明: 用户能在转换前就看清"每个音源会怎么处理"。
        self.var_plan = tk.StringVar(value="")
        ttk.Label(spc, textvariable=self.var_plan, style="Muted.TLabel",
                  font=theme.FONT_S, justify="left",
                  wraplength=760).pack(anchor="w", padx=10, pady=(0, 5))


        # ④ 选项
        opt = ttk.LabelFrame(leftcol, text=" ④ 选项 ",
                             style="Card.TLabelframe")
        opt.pack(fill="x", **pad)
        r1 = ttk.Frame(opt, style="Panel.TFrame")
        r1.pack(fill="x", padx=10, pady=2)
        ttk.Label(r1, text="输出目录:").pack(side="left")
        self.var_out = tk.StringVar(value=os.path.join(base_dir(), "输出"))
        ttk.Entry(r1, textvariable=self.var_out).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(r1, text="浏览…", style="Small.TButton",
                   command=self.pick_out).pack(side="left")

        r2 = ttk.Frame(opt, style="Panel.TFrame")
        r2.pack(fill="x", padx=10, pady=2)
        ttk.Label(r2, text="专辑名(CD-TEXT, 留空用源名):").pack(side="left")
        self.var_album = tk.StringVar(value="")
        ttk.Entry(r2, textvariable=self.var_album, width=18).pack(side="left", padx=6)
        ttk.Label(r2, text="演奏者:").pack(side="left")
        self.var_performer = tk.StringVar(value="")
        ttk.Entry(r2, textvariable=self.var_performer, width=12).pack(side="left", padx=6)
        ttk.Label(r2, text="轨间距(秒):").pack(side="left")
        self.var_gap = tk.StringVar(value="2.0")
        ttk.Spinbox(r2, from_=0, to=10, increment=0.5, width=5,
                    textvariable=self.var_gap).pack(side="left", padx=6)

        r3 = ttk.Frame(opt, style="Panel.TFrame")
        r3.pack(fill="x", padx=10, pady=(0, 5))
        self.var_verify = tk.BooleanVar(value=True)
        ttk.Checkbutton(r3, variable=self.var_verify,
                        text="转换后回读校验").pack(side="left", padx=(0, 14))
        self.var_cd_wav = tk.BooleanVar(value=False)
        ttk.Checkbutton(r3, variable=self.var_cd_wav,
                        text="导出 CD 模式 WAV 试听").pack(side="left", padx=(0, 14))
        # v4.39: 纯数据盘（音乐在 XA 里）默认只导音乐。一张 PS1 盘的 XA 常有两
        # 三百条流，其中绝大多数是音效/语音；用户要的是 OST。判据是时长+立体声
        # 宽度（见 core._XA_MUSIC_* 注释），不勾就照旧全导（229 条那种）。
        self.var_xa_music = tk.BooleanVar(value=True)
        ttk.Checkbutton(r3, variable=self.var_xa_music,
                        text="XA 只导音乐(滤掉音效/语音)").pack(side="left")
        # v4.43: PS1 的 XA 里没有 EOR 曲界标记，一首接一首只靠中间留静音。
        # 勾上就按静音间隔切成多段（一段一个文件）。默认关 —— 有的盘整条就是
        # 一首十几分钟组曲，乱切等于毁数据。
        self.var_xa_split = tk.BooleanVar(value=False)
        ttk.Checkbutton(r3, variable=self.var_xa_split,
                        text="XA 按静音切曲").pack(side="left", padx=(10, 0))

        # 界面主题（v4.18）。放在 ④「选项」里而不是另起一栏：
        # 主窗高度已经很紧（1080p 上会被 avail_h 钳到 993），多一行要靠
        # 分区内部消化，不能新增独立区块。
        r4 = ttk.Frame(opt, style="Panel.TFrame")
        r4.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(r4, text="界面主题:").pack(side="left")
        self.var_theme = tk.StringVar(value=theme.theme_label(self._theme_key))
        self.cb_theme = ttk.Combobox(
            r4, textvariable=self.var_theme, width=20, state="readonly",
            values=tuple(theme.theme_label(k) for k in theme.theme_keys()))
        self.cb_theme.pack(side="left", padx=6)
        self.cb_theme.bind("<<ComboboxSelected>>", self._on_theme_pick)
        ttk.Label(r4, text="切换立即生效，会记住选择",
                  style="Muted.TLabel", font=theme.FONT_S).pack(side="left")

        # 控制
        ctl = ttk.Frame(leftcol)
        ctl.pack(fill="x", **pad)
        self.btn_start = ttk.Button(ctl, text="开始转换", style="Hero.TButton",
                                    command=self.start)
        self.btn_start.pack(side="left", padx=(8, 6))
        self.btn_cancel = ttk.Button(ctl, text="取消", style="Small.TButton",
                                     command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=4)
        ttk.Button(ctl, text="打开输出目录", style="Small.TButton",
                   command=self.open_out).pack(side="left", padx=4)
        # v4.19 云盘上传: 转换前禁用 —— 输出目录里还没有 WAV, 点了也只能弹
        # 「没有可上传的文件」, 不如直接不给点。done 事件里再放开。
        self.btn_cloud = ttk.Button(ctl, text="上传云盘", style="Small.TButton",
                                    command=self.upload_cloud, state="disabled")
        self.btn_cloud.pack(side="left", padx=(4, 0))
        # v4.26: 账号菜单（切换账号 / 退出登录），与下载窗同一套实现
        self.btn_cloud_acct = ttk.Button(
            ctl, text="▾", width=2, style="Small.TButton",
            command=lambda: popup_cloud_account_menu(
                self.btn_cloud_acct, None, self.root, self.log, owner=self))
        self.btn_cloud_acct.pack(side="left", padx=(0, 4))

        # 进度
        prog = ttk.LabelFrame(leftcol, text=" 进度 ",
                              style="Card.TLabelframe")
        prog.pack(fill="x", **pad)
        prow = ttk.Frame(prog, style="Panel.TFrame")
        prow.pack(fill="x", padx=10, pady=(6, 6))
        # 进度条本体固定 10px 高。ttk.Progressbar 在 clam 下会按元素最小高度
        # 撑到 18px，0% 时整条只剩一个白框 —— 看着像"输入框"而不是进度条。
        # 用固定高度的容器 + pack_propagate(False) 把高度钉死。
        bar_holder = ttk.Frame(prow, style="Panel.TFrame", height=10, width=10)
        bar_holder.pack_propagate(False)
        bar_holder.pack(side="left", fill="x", expand=True, pady=4)
        self.progress = ttk.Progressbar(bar_holder, mode="determinate",
                                        maximum=100, style="TProgressbar")
        self.progress.pack(fill="both", expand=True)
        self.var_pct = tk.StringVar(value="0%")
        ttk.Label(prow, textvariable=self.var_pct, width=6, anchor="e",
                  style="Accent.TLabel",
                  font=theme.FONT_MONO).pack(side="left", padx=(8, 0))

        # 状态行从「进度」框里搬出来，单独一行贴在窗口底：
        #   ① 放在 Labelframe 里会被框的下边界裁掉半行字（实测 v4.28 前就裁）；
        #   ② 语义上它也不是"进度"的一部分，而是整窗的页脚。
        srow = ttk.Frame(leftcol)
        srow.pack(fill="x", padx=10, pady=(2, 4))
        self.var_status = tk.StringVar(value="就绪 — 请先添加音源")
        ttk.Label(srow, textvariable=self.var_status, anchor="w",
                  style="Window.TLabel").pack(side="left")
        self.var_eta = tk.StringVar(value="")
        ttk.Label(srow, textvariable=self.var_eta, anchor="e",
                  style="Window.Mono.TLabel").pack(side="right")

        # 日志
        # 播放条（先 pack，日志占剩余高度；反过来的话日志会把播放条挤出窗口）
        self._build_player_bar(logcol, pad)

        # 日志 —— v4.6 起独立成右侧一列（用户要求「日志单独放一列」）。
        # 用 tk.Text + ttk.Scrollbar 而不用 scrolledtext.ScrolledText：
        #   ① wrap="word" 自动换行，长路径不再被右边缘切掉；
        #   ② ScrolledText 自带的是 tk.Scrollbar，深色窗口里会是系统白条
        #      （见 skin_log 注释）。
        logf = ttk.LabelFrame(logcol, text=" 日志 ", style="Card.TLabelframe")
        logf.pack(fill="both", expand=True, **pad)
        logbox = ttk.Frame(logf)
        logbox.pack(fill="both", expand=True, padx=6, pady=6)
        logbox.rowconfigure(0, weight=1)
        logbox.columnconfigure(0, weight=1)
        self.logw = tk.Text(logbox, width=48, height=4, wrap="word",
                            state="disabled",
                            font=retro_font(theme.FONT_MONO))
        vs = ttk.Scrollbar(logbox, orient="vertical", command=self.logw.yview)
        self.logw.configure(yscrollcommand=vs.set)
        self.logw.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        skin_log(self.logw)   # 对 tk.Text 只上色，不动滚动条（上面已布好）
        # 换主题时 Text 的颜色不会自己变（tk 原生控件的配色写在实例上），
        # 登记一个回调让 retro_theme 切主题后叫我们一声。
        theme.on_theme_skinned(self.root, self._reskin_theme)

        # 影响 WAV 体积的选项变化后, 重新估算底部那行预算
        for var in (self.var_max, self.var_policy, self.var_speed,
                    self.var_chip_secs):
            var.trace_add("write", lambda *_a: self._schedule_info())
        # 列表增删也会改变"哪些控件可用"(只有芯片音源时才需要「默认单遍」)
        self._sync_chip_controls()

    # ---- 界面主题（v4.18）--------------------------------------------------
    def _on_theme_pick(self, _event=None):
        """下拉框选中新主题：切皮肤 + 记住选择。

        ttk 控件的重绘由 retro_theme 负责（重跑 install）；画布横幅、
        tk.Text 日志这些「样式表管不到」的，走 _reskin_theme 回调。
        """
        label = self.var_theme.get()
        key = None
        for candidate in theme.theme_keys():
            if theme.theme_label(candidate) == label:
                key = candidate
                break
        if not key or key == self._theme_key:
            return
        self._theme_key = key
        theme.apply_theme(key)          # 内部会回调 _reskin_theme
        save_theme_key(key)
        self.log("🎨 界面主题已切换为「%s」，下次启动沿用。" % theme.theme_label(key))

    def _reskin_theme(self):
        """切主题后重刷「样式表管不到」的部分：日志 Text / 横幅 / 列表 tag。

        由 retro_theme.apply_theme 回调；任何一步失败都只跳过它自己，
        绝不让换皮肤把主程序带崩。
        """
        try:
            skin_log(self.logw)
        except Exception:                            # noqa: BLE001
            pass
        self._retag_list()
        self._reskin_empty_state()
        banner = getattr(self, "banner", None)
        if banner is not None:
            try:
                banner.refresh()
            except Exception:                        # noqa: BLE001
                pass
        try:
            self.root.configure(bg=theme.PALETTE["bg"])
        except tk.TclError:
            pass

    def _retag_list(self):
        """把音源列表的行 tag 颜色刷成当前主题。

        Treeview 的 tag 前景色是**写在控件里**的，不像 ttk 样式那样能全局
        重配，所以换主题必须自己重设一遍，否则格式族颜色还停在上一套。
        """
        tree = getattr(self, "src_tree", None)
        if tree is None:
            return
        try:
            for tag, color in FMT_FAMILY_COLOR.items():
                tree.tag_configure(
                    "fmt_" + tag, foreground=theme.PALETTE.get(color, color))
            tree.tag_configure("unsup", foreground=theme.PALETTE["dim"])
            tree.tag_configure("warn", foreground=theme.PALETTE["amber"])
            tree.tag_configure("weak", foreground=theme.PALETTE["dim"])
        except tk.TclError:
            pass

    def _reskin_empty_state(self):
        """空态的底色/字色写在实例上（tk.Label 不吃 ttk 样式）→ 切主题要重刷。

        与 `_retag_list` 同一类坑：不刷的话换皮肤后空态还是上一套颜色，
        在白底主题上会出现一块米黄方块。
        """
        box = getattr(self, "_empty_box", None)
        if box is None:
            return
        p = theme.PALETTE
        try:
            box.configure(bg=p["card"])
            kids = box.winfo_children()
            for w, color in zip(kids, (p["cyan"], p["amber"], p["muted"])):
                w.configure(bg=p["card"], fg=color)
        except tk.TclError:
            pass

    # ---- 音源列表: 渲染适配层(设计 §8.2) ----------------------------------    # 6 个方法把"列表长什么样"与"列表怎么存数据"彻底分开。Treeview 与
    # Listbox 各一套实现, 由 LIST_MODE 决定。回滚只需把 LIST_MODE 改成
    # "list", self.sources / add_paths / _refresh_info / _estimate /
    # ConvertWorker 一行都不用动。
    LIST_MODE = "tree"          # "tree"(表格) | "list"(回退到 Listbox)
    LIST_HEIGHT = 6

    # ---- 试听播放（v4.7）--------------------------------------------------

    @staticmethod
    def _fmt_time(sec) -> str:
        try:
            sec = max(0, int(float(sec)))
        except (TypeError, ValueError):
            sec = 0
        return "%d:%02d" % (sec // 60, sec % 60)

    def _build_player_bar(self, parent, pad):
        """右下角播放条：▶ 播放/暂停、■ 停止、进度条（可点跳转）、◀▶ 换轨。"""
        bar = ttk.LabelFrame(parent, text=" 播放 ", style="Card.TLabelframe")
        bar.pack(fill="x", **pad)

        r1 = ttk.Frame(bar, style="Panel.TFrame")
        r1.pack(fill="x", padx=8, pady=(6, 2))
        self.btn_play = ttk.Button(r1, text="▶", width=3,
                                   style="Small.TButton", state="disabled",
                                   command=self._toggle_play)
        self.btn_play.pack(side="left")
        self.btn_stop = ttk.Button(r1, text="■", width=3,
                                   style="Small.TButton", state="disabled",
                                   command=self._stop_play)
        self.btn_stop.pack(side="left", padx=(4, 8))
        self.play_prog = ttk.Progressbar(r1, mode="determinate", maximum=1000,
                                         style="TProgressbar")
        self.play_prog.pack(side="left", fill="x", expand=True)
        # 点/拖进度条 = seek（试听时最常用的操作就是跳着听）
        self.play_prog.bind("<Button-1>", self._on_prog_click)
        self.play_prog.bind("<B1-Motion>", self._on_prog_click)
        self.var_play_time = tk.StringVar(value="0:00 / 0:00")
        ttk.Label(r1, textvariable=self.var_play_time,
                  style="Mono.TLabel").pack(side="left", padx=(8, 0))
        # 音量（v4.13）。只作用在**送进音频设备的那一份 PCM**上，
        # 不改 clip 本体，所以调小后再调回来不会有任何音质损失。
        #
        # ⚠ 布局取舍（四版都截图验证过，别再回头）：
        #   ① 滑块 96 + 独立百分比 Label → 百分比被挤出可视区；
        #   ② 滑块 84 + Label(width=3) → "100%" 4 字符装不下，裁成空；
        #   ③ 图标与数字合并(width=7) → emoji 实际占宽 >1 字符，显示成 "🔊 1"；
        #   ④ **本版：只留图标，滑块给到 110px** —— 用户要的是"调节范围大"，
        #      数字不是必需；420px 的列放下「按钮+进度+时间+图标+110px 滑块」正合适。
        # ⚠ 必须用 side="right" 从右边缘占位。用 side="left" 时，前面那个
        #   expand=True 的进度条会把剩余空间全吃掉，滑块被压成 30px —— 设了
        #   length=110 也没用（实测截图确认）。side="right" 的控件按请求宽度
        #   贴右边排，不受左边 expand 影响。
        self.var_vol = tk.DoubleVar(value=100.0)
        # 图标和滑块装进同一个 Frame 再整体贴右 —— 分别 side="right" 的话
        # 图标会被滑块挤到边界外（实测截图里图标消失）。
        vbox = ttk.Frame(r1, style="Panel.TFrame")
        vbox.pack(side="right", padx=(8, 0))
        # 音量图标用等宽「VOL」而不是 🔊：emoji 是彩色位图，跟像素皮肤打架，
        # 而且实际占宽 > 1 字符，之前把百分比挤没了（见上面的四版布局取舍）。
        ttk.Label(vbox, text="VOL", style="Mono.TLabel").pack(side="left",
                                                              padx=(0, 4))
        ttk.Scale(vbox, from_=0, to=100, orient="horizontal", length=104,
                  variable=self.var_vol,
                  command=self._on_volume).pack(side="left")

        r2 = ttk.Frame(bar, style="Panel.TFrame")
        r2.pack(fill="x", padx=8, pady=(0, 6))
        self.var_play_name = tk.StringVar(value="点列表里的 ▶ 试听")
        ttk.Label(r2, textvariable=self.var_play_name, style="Sub.TLabel",
                  anchor="w").pack(side="left", fill="x", expand=True)
        self.btn_prev = ttk.Button(r2, text="◀", width=2, style="Small.TButton",
                                   state="disabled",
                                   command=lambda: self._switch_track(-1))
        self.btn_prev.pack(side="left")
        self.btn_next = ttk.Button(r2, text="▶", width=2, style="Small.TButton",
                                   state="disabled",
                                   command=lambda: self._switch_track(1))
        self.btn_next.pack(side="left", padx=(2, 0))

    # -- 事件 --------------------------------------------------------------

    def _on_list_click(self, ev):
        """点列表第一列（▶）→ 试听该行。其它列交给 Treeview 自己的选择逻辑。"""
        if self.src_tree is None or self.player is None:
            return
        try:
            if self.src_tree.identify_column(ev.x) != "#1":
                return
            iid = self.src_tree.identify_row(ev.y)
        except tk.TclError:
            return
        if iid:
            self._play_source(iid)

    def _on_list_right_click(self, ev):
        """列表右键：用外部播放器打开 / 打开所在文件夹。"""
        try:
            iid = self.src_tree.identify_row(ev.y)
        except tk.TclError:
            return
        if not iid:
            return
        idx = self._idx_of_iid.get(iid)
        if idx is None or not (0 <= idx < len(self.sources)):
            return
        path = self.sources[idx].get("path") or ""
        if not path or not os.path.exists(path):
            return
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        popup_open_menu(self, ev, path, self.log, folder)

    def _play_source(self, iid):
        idx = self._idx_of_iid.get(iid)
        if idx is None or not (0 <= idx < len(self.sources)):
            return
        path = self.sources[idx].get("path") or ""
        if not path or not os.path.exists(path):
            self.log("❌ 试听失败：源文件不存在")
            return
        # 再点同一行 = 播放/暂停切换（常见操作，不必去找下面的按钮）
        if path == self._play_path and self.player.is_active():
            self._toggle_play()
            return
        self._start_load(path, 0)

    def _start_load(self, path, track):
        """在后台线程解码，避免 150 秒的芯片曲目把界面卡住。"""
        # 本工具解不了的格式（N64 的 .miniusf、NDS 的 .2sf…）→ 交给 foobar2000
        if os.path.splitext(path)[1].lower() not in SELF_PLAYABLE_EXTS:
            open_with_foobar(path, self.log)
            return
        if self.player is None:
            self.log("❌ 试听不可用：音频子系统未初始化")
            return
        if self._loading:
            return
        self._loading = True
        self._play_path = path
        self._play_track = int(track)
        try:
            import player as _pm
            self._track_list = _pm.AudioClip.list_tracks(path) or [(0, "整首")]
        except Exception:                                    # noqa: BLE001
            self._track_list = [(0, "整首")]
        self.var_play_name.set("解码中… %s" % os.path.basename(path))
        self.btn_play["state"] = "disabled"
        threading.Thread(target=self._load_bg, args=(path, int(track)),
                         daemon=True).start()

    def _load_bg(self, path, track):
        """工作线程：只解码 + 投消息，绝不碰 Tk。"""
        try:
            import player as _pm
            ext = os.path.splitext(path)[1].lower()
            if ext in (".cue",) + core.DISC_IMG_EXTS:
                clip = _pm.AudioClip.from_cue(path, track)
            elif ext in (".wav", ".mp3", ".flac", ".ogg", ".m4a"):
                clip = _pm.AudioClip.from_audio_file(path)
            else:
                # 芯片音源：完整曲目（用户选的 ②B），时长交给 plan_duration
                clip = _pm.AudioClip.from_chip(path, track)
            self.msgq.put(("play_ready", clip))
        except Exception as e:                               # noqa: BLE001
            self.msgq.put(("play_error", str(e)))

    def _on_play_ready(self, clip):
        self._loading = False
        if self.player is None:
            return
        try:
            self.player.load(clip)
            self.player.play()
        except Exception as e:                               # noqa: BLE001
            self.log("❌ 试听失败：%s" % e)
            self.var_play_name.set("试听失败")
            return
        self._refresh_player_ui()
        self._start_player_poll()
        self.log("▶ 试听 %s（%s，%s）" % (
            clip.name, clip.detail, self._fmt_time(clip.secs)))

    def _on_play_error(self, text):
        self._loading = False
        self.var_play_name.set("试听失败")
        self.btn_play["state"] = "disabled"
        self.log("❌ 试听失败：" + text)

    def _toggle_play(self):
        pl = self.player
        if pl is None or pl.clip is None:
            return
        try:
            if pl.is_active():
                if pl.is_paused():
                    pl.resume()
                else:
                    pl.pause()
            else:
                pl.play()
        except Exception as e:                               # noqa: BLE001
            self.log("❌ 播放失败：%s" % e)
            return
        self._refresh_player_ui()
        self._start_player_poll()

    def _on_volume(self, val):
        """音量滑块（0~100）。播放中调也立即生效。

        音量只乘在**送进音频设备的那一份 PCM** 上（见 player.scale_pcm），
        clip 本体不动 —— 调小再调回来没有任何音质损失。
        """
        try:
            v = float(val)
        except (TypeError, ValueError):
            return
        if self.player is not None:
            self.player.set_volume(v / 100.0)
        try:
            self.var_vol_txt.set("%d%%" % int(round(v)))
        except tk.TclError:
            pass

    def _stop_play(self):
        if self.player is not None:
            self.player.stop()
        self._refresh_player_ui()

    def _on_prog_click(self, ev):
        pl = self.player
        if pl is None or not pl.is_active() or pl.duration() <= 0:
            return
        try:
            w = max(1, self.play_prog.winfo_width())
        except tk.TclError:
            return
        frac = min(1.0, max(0.0, (ev.x - 2) / float(w - 4)))
        pl.seek(frac * pl.duration())
        self._refresh_player_ui()

    def _switch_track(self, delta):
        if not self._play_path or len(self._track_list) <= 1:
            return
        n = len(self._track_list)
        nxt = (self._play_track + delta) % n
        self._start_load(self._play_path, nxt)

    # -- 进度轮询（音频回调在别的线程，UI 只轮询状态）-----------------------

    def _start_player_poll(self):
        # ⚠ App 用的是 _closing（DownloadDialog 才是 _closed）—— 这里曾写成
        # _closed 导致 AttributeError 被 _poll_queue 吞掉、轮询根本起不来，
        # 症状是「能出声但进度条不动、时间停在 0:00」。
        if self._after_player is not None:
            return
        if getattr(self, "_closing", False):
            return
        self._after_player = self.root.after(200, self._poll_player)

    def _poll_player(self):
        self._after_player = None
        pl = self.player
        if pl is None:
            return
        if not pl.is_active():
            if pl.has_finished():
                self._refresh_player_ui()
                pl.stop()                 # 释放设备，回到「未播放」状态
                self._refresh_player_ui()
            return
        self._refresh_player_ui()
        self._start_player_poll()

    def _refresh_player_ui(self):
        pl = self.player
        if pl is None:
            return
        clip = pl.clip
        dur = pl.duration()
        pos = pl.position()
        try:
            if dur > 0:
                self.play_prog["value"] = max(0, min(1000, int(pos / dur * 1000)))
            else:
                self.play_prog["value"] = 0
            self.var_play_time.set("%s / %s" % (self._fmt_time(pos),
                                                self._fmt_time(dur)))
            active = pl.is_active()
            self.btn_play["state"] = "normal" if (clip is not None) else "disabled"
            self.btn_stop["state"] = "normal" if active else "disabled"
            self.btn_play["text"] = "⏸" if pl.is_playing() else "▶"
            if clip is not None:
                # 曲名常常很长（NSF 文件名带版本/厂商/日期），420px 的播放条
                # 直接放会溢出把 ◀▶ 挤出去 —— 截断到 30 字符加省略号。
                nm = clip.name or ""
                if len(nm) > 30:
                    nm = nm[:29] + "…"
                self.var_play_name.set("%s · %s" % (nm, clip.detail))
            multi = len(self._track_list) > 1 and active
            self.btn_prev["state"] = "normal" if multi else "disabled"
            self.btn_next["state"] = "normal" if multi else "disabled"
        except tk.TclError:
            pass

    def _ui_list_create(self, parent):
        """建列表控件(建在 parent 里)并绑好滚动条。"""
        if self.LIST_MODE == "list":
            self.listbox = tk.Listbox(
                parent, height=self.LIST_HEIGHT, selectmode="extended",
                bg=theme.PALETTE["card"], fg=theme.PALETTE["text"],
                selectbackground=theme.PALETTE["sel"],
                selectforeground=theme.PALETTE["text"],
                highlightthickness=0, borderwidth=0, activestyle="none",
                font=retro_font(theme.FONT))
            self.listbox.pack(side="left", fill="both", expand=True,
                              padx=(8, 4), pady=6)
            sb = ttk.Scrollbar(parent, command=self.listbox.yview)
            sb.pack(side="left", fill="y")
            self.listbox.config(yscrollcommand=sb.set)
            self.src_tree = None
            self.list_sb = sb
            self._make_empty_state(parent)
            return
        self.listbox = None
        # show="headings" 必须显式写: Treeview 默认是 "tree", 会多出一列 #0
        # 树形缩进, 把右边的列全挤掉(设计 §7.5 陷阱 #4)。
        self.src_tree = ttk.Treeview(
            parent, columns=("play", "name", "fmt", "tracks", "size", "secs"),
            show="headings", selectmode="extended", height=self.LIST_HEIGHT)
        # v4.7: 首列 "▶" 是播放入口（点这一格就试听该行）。
        # Treeview 不能嵌真按钮，所以用一列字符 + Button-1 命中列号实现，
        # 视觉上是一列 ▶，行为上等价于「每行一个按钮」。
        for key, text, width, anchor in (
                ("play", "▶", 32, "center"),
                ("name", "名称", 264, "w"), ("fmt", "格式", 74, "center"),
                ("tracks", "曲目数", 56, "e"), ("size", "大小", 68, "e"),
                ("secs", "时长", 64, "e")):
            self.src_tree.heading(key, text=text)
            self.src_tree.column(key, width=width, minwidth=40, anchor=anchor,
                                 stretch=(key == "name"))
        self.src_tree.column("play", width=32, minwidth=32, stretch=False)
        # 行高 22px: 主题里的 Treeview 默认行高偏挤, 22 才放得下 12px 点阵字
        # 的上下留白。局部设置, 不动 retro_theme 的全局皮肤。
        try:
            self.style.configure("Treeview", rowheight=22)
        except tk.TclError:
            pass
        # 行 tag: 格式族着色 + 不支持置灰。ttk 的 tag 作用于整行(不能只给
        # 一格上色), 所以"格式徽标按族着色"只能落在行前景色上(设计 §7.3)。
        for tag, color in FMT_FAMILY_COLOR.items():
            try:
                self.src_tree.tag_configure(
                    "fmt_" + tag, foreground=theme.PALETTE.get(color, color))
            except tk.TclError:
                pass
        try:
            self.src_tree.tag_configure("unsup",
                                        foreground=theme.PALETTE["dim"])
            self.src_tree.tag_configure("warn",
                                        foreground=theme.PALETTE["amber"])
        except tk.TclError:
            pass
        sb = ttk.Scrollbar(parent, command=self.src_tree.yview)
        self.src_tree.configure(yscrollcommand=sb.set)
        self.src_tree.pack(side="left", fill="both", expand=True,
                           padx=(8, 4), pady=6)
        sb.pack(side="left", fill="y")
        self.list_sb = sb
        self.src_tree.bind("<Button-1>", self._on_list_click, add="+")
        # 右键：本工具解不了的格式（USF/2SF/WSR…）交给系统关联的外部播放器。
        # 不内置任何第三方软件 —— foobar2000 的许可明确写「只允许再分发未修改的
        # 安装程序，其它形式的二进制再分发一律不允许」，所以只能走"用户自己装、
        # 我们调用"这条路。
        self.src_tree.bind("<Button-3>", self._on_list_right_click, add="+")
        self._make_empty_state(parent)

    # -- 空态（v4.28）-------------------------------------------------------
    def _make_empty_state(self, parent):
        """在列表中央盖一块两行提示，列表有数据时自动隐藏。

        为什么不用第三行 `ttk.Label` 写在列表上方：那句话在**有文件时是废话**，
        而空列表时的正确位置是列表正中（用户的视线已经在那儿）。

        ⚠ 必须 tk.Frame/tk.Label 显式给 bg：ttk.Label 的 background 不跟随容器，
          否则会在 card 底上盖出 panel 色方块（README ⚠7 同款坑）。
        ⚠ 用 ``place(in_=控件)`` 相对**列表本身**定位，不是相对整个分区 ——
          按钮列占了右侧 110px，按分区宽算会偏右。
        """
        card = theme.PALETTE["card"]
        box = tk.Frame(parent, bg=card, bd=0, highlightthickness=0)
        lb1 = tk.Label(box, text="＋ 把音源拖进来", bg=card,
                       fg=theme.PALETTE["cyan"], font=retro_font(theme.FONT_B))
        lb1.pack()
        lb2 = tk.Label(box, text=".spc  .nsf  .vgm  .hes  .kss  .sap  .gym  .ay  .gbs",
                       bg=card, fg=theme.PALETTE["amber"],
                       font=retro_font(theme.FONT_MONO))
        lb2.pack(pady=(3, 0))
        lb3 = tk.Label(box, text="或点右侧「添加音源…」", bg=card,
                       fg=theme.PALETTE["muted"], font=retro_font(theme.FONT_S))
        lb3.pack(pady=(2, 0))
        # 点空态 = 添加音源（比让用户再去找按钮少一步）
        for w in (box, lb1, lb2, lb3):
            w.bind("<Button-1>", lambda _e: self.add_sources())
            try:
                w.configure(cursor="hand2")
            except tk.TclError:
                pass
        self._empty_box = box
        self._sync_empty_state()

    def _sync_empty_state(self):
        """按**控件里的行数**（不是 self.sources）决定空态显隐。

        用行数而不是 len(self.sources)：删除/清空时列表已经改完、sources 可能
        还没重新赋值，按 sources 判断会出现「列表空了但提示不出来」。
        """
        box = getattr(self, "_empty_box", None)
        if box is None:
            return
        try:
            if self.LIST_MODE == "list" or self.src_tree is None:
                host, n = self.listbox, self.listbox.size()
            else:
                host, n = self.src_tree, len(self.src_tree.get_children())
            if n:
                box.place_forget()
            else:
                box.place(in_=host, relx=0.5, rely=0.46, anchor="center")
                box.lift()
        except tk.TclError:
            pass

    def _ui_list_insert(self, entry, cells):
        """插入一行。``entry["iid"]`` 是调用方分配的单调自增行 id。"""
        if self.LIST_MODE == "list" or self.src_tree is None:
            self.listbox.insert("end", f"{entry['prefix']} {entry['label']}  "
                                       f"({entry['kind']})")
            self._sync_empty_state()
            return
        self.src_tree.insert("", "end", iid=entry["iid"], values=cells,
                             tags=self._tags_for(entry))
        self._sync_empty_state()

    def _ui_list_delete(self, iids):
        """按 iid 删若干行。Listbox 模式下 iid 就是显示序号(自增, 不复用)。"""
        if not iids:
            return
        if self.LIST_MODE == "list" or self.src_tree is None:
            # Listbox 的 delete 只吃下标, 所以先把 iid 换算回"显示序号"。
            order = {e["iid"]: i for i, e in enumerate(self.sources)}
            idxs = sorted((order[i] for i in iids if i in order), reverse=True)
            for i in idxs:
                self.listbox.delete(i)
            self._sync_empty_state()
            return
        for iid in iids:
            self.src_tree.delete(iid)
        self._sync_empty_state()

    def _ui_list_clear(self):
        """清空全部行。"""
        if self.LIST_MODE == "list" or self.src_tree is None:
            self.listbox.delete(0, "end")
            self._sync_empty_state()
            return
        # 空树 delete() 会 TclError, 所以先取 children
        kids = self.src_tree.get_children()
        if kids:
            self.src_tree.delete(*kids)
        self._sync_empty_state()

    def _ui_list_selected_entries(self):
        """当前选中的行对应的 entry, **按显示顺序**返回。"""
        if self.LIST_MODE == "list" or self.src_tree is None:
            idxs = set(self.listbox.curselection())
        else:
            idxs = {self._idx_of_iid[i] for i in self.src_tree.selection()
                    if i in self._idx_of_iid}
        return [e for i, e in enumerate(self.sources) if i in idxs]

    def _ui_list_see(self, iid):
        """滚动到指定行(Listbox 只能滚到末尾, 与原来的 see("end") 一致)。"""
        if self.LIST_MODE == "list" or self.src_tree is None:
            self.listbox.see("end")
            return
        try:
            self.src_tree.see(iid)
        except tk.TclError:
            pass

    def _tags_for(self, entry):
        """这一行该用哪个前景色 tag。"""
        if self.LIST_MODE == "list" or self.src_tree is None:
            return ()
        if not entry.get("ok", True):
            return ("unsup",)
        if entry.get("warn"):
            return ("warn",)
        fmt = str(entry.get("fmt") or entry.get("kind") or "")
        if fmt in FMT_FAMILY_COLOR:
            return ("fmt_" + fmt,)
        return ()

    @staticmethod
    def _fmt_secs_cell(entry):
        """时长列文本(设计 §3.5): ``2:31`` / ``1:45◂`` / ``1:45*``。"""
        secs = float(entry.get("est_secs") or 0.0)
        if secs <= 0:
            return "—"
        h, rem = divmod(int(round(secs)), 3600)
        m, s = divmod(rem, 60)
        text = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        exact = bool(entry.get("exact"))
        loop = str(entry.get("dur_src") or "") == "gbs_loop"
        return text + ("" if exact else ("◂" if loop else "*"))


    # ---- 音源 ---------------------------------------------------------------
    def add_source(self, path, kind, prefix, meta=None):
        """把一个音源加进列表。返回 True=真的加进去了, False=重复。

        ``meta`` 是 ChipInfo(芯片音源)或 None(其它类型)。签名对旧调用点
        保持向后兼容: ``meta`` 可以省, 这时只用扩展名给出的 kind。
        """
        path = os.path.normpath(path)
        if any(s["path"] == path for s in self.sources):
            return False
        label = os.path.basename(path.rstrip("\\/")) or path
        size = 0
        if os.path.isfile(path):
            # 单文件去掉扩展名, 避免输出成 "xxx.spc.bin" 这种名字
            label = os.path.splitext(label)[0] or label
        self._src_seq += 1
        iid = f"s{self._src_seq}"
        entry = {"label": label, "path": path, "kind": kind, "iid": iid,
                 "prefix": prefix, "fmt": "", "n_tracks": 0, "est_secs": 0.0,
                 "total_secs": 0.0, "exact": False, "dur_src": "", "size": 0,
                 "ok": True, "warn": False, "cd_tracks": 0, "cd_secs": 0.0}
        if meta is not None:
            self._fill_meta(entry, path, meta)
        if os.path.isfile(path) and not entry["size"]:
            try:
                entry["size"] = os.path.getsize(path)
            except OSError:
                entry["size"] = 0
        self.sources.append(entry)
        self._idx_of_iid[iid] = len(self.sources) - 1
        cells = (
            "▶",
            f"{prefix} {label}",
            self._fmt_cell(entry),
            str(entry["n_tracks"]) if entry["n_tracks"] > 0 else "—",
            Chip.human_size(entry["size"]) if entry["size"] else "—",
            self._fmt_secs_cell(entry),
        )
        self._ui_list_insert(entry, cells)
        self._ui_list_see(iid)
        self._schedule_info()
        return True

    def _fill_meta(self, entry, path, meta):
        """把 ChipInfo(或目录合成值)的字段抄进 entry, 并算出计划时长。"""
        entry["fmt"] = str(getattr(meta, "fmt", "") or "")
        entry["n_tracks"] = max(0, int(getattr(meta, "n_tracks", 0) or 0))
        entry["ok"] = bool(getattr(meta, "ok", True))
        entry["size"] = max(0, int(getattr(meta, "size", 0) or 0))
        note = Chip.conflict_note(path, meta)
        if note:
            self._pending_notes.append(note)
        if entry["fmt"]:
            # CD-DA 不能落进 CHIP：解码器（PyAV vs libgme）与「只出 WAV」
            # 的规则都不同，走错分支会把 .ape 丢给 libgme 而静默失败。
            if entry["fmt"] == CD_FMT:
                entry["kind"] = "CD"
            else:
                entry["kind"] = "SPC" if entry["fmt"] == "SPC" else "CHIP"
        # 单文件走同一条时长决策链(与 worker 侧同一个纯函数), 目录直接用合成值
        if hasattr(meta, "est_secs"):
            entry["est_secs"] = float(getattr(meta, "est_secs", 0.0) or 0.0)
            entry["exact"] = bool(getattr(meta, "exact", False))
            entry["dur_src"] = str(getattr(meta, "dur_src", "") or "")
            entry["total_secs"] = float(getattr(meta, "total_secs", 0.0) or 0.0)
            # v4.31: 目录里混装 CD 音轨时，单独记下 CD 那部分（不进钳制）
            entry["cd_tracks"] = max(0, int(getattr(meta, "cd_tracks", 0) or 0))
            entry["cd_secs"] = max(0.0, float(getattr(meta, "cd_secs", 0.0) or 0.0))
        else:
            secs, exact, src = self._plan_for(path, meta)
            entry["est_secs"] = secs
            entry["exact"] = exact
            entry["dur_src"] = src
            entry["total_secs"] = secs * max(1, entry["n_tracks"])


    def _fmt_cell(self, entry):
        """格式列文本 —— **只放格式名，不带轨数**。

        ⚠ 早先是「HES ×199」这种写法，曲目数在 74px 的列里放不下三位数：
        实测 PCE-CD 的包（196 首 HES + 3 首 CD）显示成「HES ×19」，末位被裁掉，
        看起来像少了一百首。轨数本来就有独立的「曲目数」列，这里不该重复。
        """
        fmt = str(entry.get("fmt") or "")
        if fmt:
            return fmt
        kind = str(entry.get("kind") or "")
        return "芯片" if kind == "CHIP" else (kind or "—")

    def _reindex(self):
        """下标平移后重建 iid -> 下标映射(删中间行时必须调)。"""
        self._idx_of_iid = {e["iid"]: i for i, e in enumerate(self.sources)}

    def _schedule_info(self):
        """合并刷新: 一次拖入几十项时不要每次都全量重算预算。"""
        if self._info_job is not None:
            try:
                self.root.after_cancel(self._info_job)
            except Exception:                        # noqa: BLE001
                pass
        self._info_job = self.root.after(60, self._run_scheduled_info)

    def _run_scheduled_info(self):
        self._info_job = None
        self._refresh_info()
        self._sync_chip_controls()

    # ---- v4.31: CD-DA 音轨（PCE-CD / MD-CD 的「另一半」）------------------
    # 收集与探测都在**模块级** _cd_files / _cd_meta：ConvertWorker 也要用，
    # 而它不继承 App（App 只管列表，Worker 只管转换）。
    def _meta_for(self, path, kind):
        """给 ``add_source`` 准备 meta。拿不到就返回 None（列表显示「—」）。"""
        if kind in ("CHIP", "SPC"):
            if not os.path.isdir(path):
                return Chip.probe(path)
            meta = self._dir_meta(path)
            # v4.31: CHIP 目录可能混装 CD 音轨（PCE-CD 的 .ape），并进去
            if kind == "CHIP" and meta is not None:
                meta = _with_cd_meta(meta, path)
            return meta
        if kind == "CD":
            return _cd_meta(path)
        # v4.35: 光盘镜像（PS1/SegaCD 的 cue+bin）也填曲目数/大小/时长，
        # 否则列表三列全是「—」，用户看不出这张盘里到底有几首。
        if kind == "光盘镜像":
            return _disc_meta(path)
        return None

    @staticmethod
    def _kind_of_dir(d):
        """按目录内容判断音源类型。

        优先级(设计 §7.6 / FR-1.4): .pcm -> MSU-1 最优先(不变);
        纯 .spc -> SPC(不变, AC-14); 其余含芯片扩展名 -> CHIP。
        **纯 .spc 目录必须仍然判成 SPC**: 若让 CHIP 分支抢在前面,
        load_spc_dir 就不会再被调用, SPC 路径的零回归(AC-14)直接破。

        v4.31 起: CHIP 目录里若**同时**有 CD 音轨（PCE-CD 典型结构），
        仍判 CHIP —— ``_do_chip`` 会顺带把同目录的 CD 音轨也转掉；
        只有「纯 CD 音轨」的目录才判成 CD。
        """
        try:
            names = [f.lower() for f in os.listdir(d)
                     if os.path.isfile(os.path.join(d, f))]
        except OSError:
            return "MSU-1"
        if any(n.endswith(".pcm") for n in names):
            return "MSU-1"
        if any(n.endswith(".spc") for n in names):
            return "SPC"
        if any(n.endswith(Chip.exts()) for n in names):
            return "CHIP"
        # v4.34: 光盘镜像文件夹（PS1 / Sega CD 的 cue+bin 整包）。
        # 判据必须**同时**有 .cue 和镜像文件：只有 .cue 的目录可能只是描述
        # .ape 的播放列表（PCE-CD 那种），那种仍要落到下面的 CD 音轨分支。
        # v4.38: 镜像不再只认 .bin —— PCE-CD 主流是 .cue + .iso（单文件，
        # 音频与数据交织）。只认 .bin 会让整包被误判成 MSU-1，最后报「未找到音轨」。
        if any(n.endswith(".cue") for n in names) and \
                any(n.endswith(core.DISC_IMG_EXTS) for n in names):
            return "光盘镜像"
        if any(n.endswith(_CD_AUDIO_EXTS) for n in names):
            return "CD"
        return "MSU-1"

    @staticmethod
    def _kind_of_file(f):
        """按扩展名+魔数判断类型 -> ``(kind, 前缀, 原因)`` 三元组。

        **统一返回 3 元组**(设计 §7.6): 不支持时 kind 为 None, 第三个元素
        是 FR-4.2 的可执行文案。旧代码返回 2 元组, 调用点必须同步改成三元
        解包 —— 全项目只有 ``add_paths`` 一处调用(设计风险 H12)。
        """
        low = f.lower()
        if low.endswith(".spc"):
            return "SPC", "🎮", ""
        if low.endswith((".cue",) + core.DISC_IMG_EXTS):
            return "光盘镜像", "💿", ""
        if low.endswith(".mp3"):
            return "MP3", "🎵", ""
        # v4.31: CD-DA 音轨（.ape/.flac/.wav/.ogg…）。PCE-CD / MD-CD 的包是
        # 「芯片音源 + CD 音轨」混装，CD 部分同样属于游戏音源，要一起转。
        if low.endswith(_CD_AUDIO_EXTS):
            return "CD", "◎", ""
        if low.endswith(Chip.exts()):
            info = Chip.probe(f)
            if info is None:
                return None, "", Chip.reason(f, None)
            if getattr(info, "ok", False) and Chip.supported(
                    getattr(info, "fmt", None)):
                fmt = str(info.fmt)
                # SPC 的 kind 仍是 SPC(走 _do_spc 老路径, 零回归);
                # 其余 P0 格式统一归到 CHIP。
                return ("SPC" if fmt == "SPC" else "CHIP"), "🎮", ""
            return None, "", Chip.reason(f, info)
        # 不认识的扩展名: 若 chip_formats 能按魔数认出是 P0 格式也接受
        # (设计 §2.5 冲突表最后一行), 否则给 FR-4.2 第二类文案。
        if Chip.ready():
            info = Chip.probe(f)
            if info is not None and getattr(info, "ok", False) and \
                    Chip.supported(getattr(info, "fmt", None)):
                return "CHIP", "🎮", ""
        return None, "", Chip.reason(f, None)


    def _on_drop(self, paths):
        """拖放入口: 文件夹按内容识别类型, 文件按扩展名识别。"""
        self.add_paths(paths, from_drop=True)

    def add_sources(self):
        """统一添加入口: 同一个对话框里既能多选文件, 也能多选文件夹。"""
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo("提示", "正在转换中，请等完成后再添加音源")
            return
        start = os.path.expanduser("~")
        if self.sources:
            last = self.sources[-1]["path"]
            start = last if os.path.isdir(last) else os.path.dirname(last)
        picked = PickDialog.ask(self.root, start)
        if picked:
            self.add_paths(picked)

    def add_paths(self, paths, from_drop=False):
        """把一批文件/文件夹加入音源列表 (拖放与对话框共用同一套识别逻辑)。"""
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo("提示", "正在转换中，请等完成后再添加音源")
            return 0
        paths = [os.path.normpath(str(p)) for p in paths]
        dirs = [p for p in paths if os.path.isdir(p)]
        files = [p for p in paths if os.path.isfile(p)]
        skipped = len(paths) - len(dirs) - len(files)        # 解析不了的项
        unsup = 0                                            # 格式不支持(FR-4.3)
        self._pending_notes = []

        n_before = len(self.sources)
        for d in dirs:
            kind = self._kind_of_dir(d)
            meta = self._meta_for(d, kind)
            if not self.add_source(d, kind, "📁", meta):
                skipped += 1
        for i, f in enumerate(files):
            # 三元解包: 不支持时 kind is None, reason 是可执行文案(H12)
            kind, prefix, reason = self._kind_of_file(f)
            if kind is None:
                if f.lower().endswith(".pcm"):
                    self.log(f"⚠ {os.path.basename(f)}：MSU-1 音轨不能单独添加，"
                             f"请添加它所在的整个文件夹")
                else:
                    self.log(reason or
                             f"⚠ 已忽略不支持的文件: {os.path.basename(f)}")
                unsup += 1
                skipped += 1
                continue
            meta = self._meta_for(f, kind)
            if not self.add_source(f, kind, prefix, meta):
                skipped += 1
            # 一次拖入几十项时每 40 项让出一次 UI, 否则大目录会卡住窗口
            if (i + 1) % 40 == 0:
                try:
                    self.root.update_idletasks()
                except tk.TclError:
                    pass

        # 冲突提示(扩展名与魔数不符)在 add_paths 统一按拖放顺序输出
        for note in self._pending_notes:
            self.log(note)
        self._pending_notes = []

        added = len(self.sources) - n_before
        msg = f"{'拖放' if from_drop else ''}添加 {added} 项"
        if skipped:
            msg += f"（跳过 {skipped} 项"
            msg += f"，其中格式不支持 {unsup} 项" if unsup else ""
            msg += "）"
        self.log(msg)

        # 含文件夹时, 输出目录 = 源文件夹同级 + "<文件夹名>output"
        if dirs:
            src = os.path.normpath(dirs[0])
            out = os.path.join(os.path.dirname(src),
                               os.path.basename(src) + "output")
            self.var_out.set(out)
            self.log(f"输出目录 → {out}")
        self._schedule_info()
        return added

    def _dir_meta(self, d):
        """目录型音源的汇总元信息(只统计第一层, 与 collect_* 一致)。

        目录的 ChipInfo 是"把里面每个文件的轨数/时长加起来"的合成值, 所以
        ``kind`` 仍由 ``_kind_of_dir`` 决定, 这里只补 fmt/n_tracks/est_secs/
        total_secs。total_secs 逐文件累加真实的总秒数(n_tracks × 单轨),
        不能用 est_secs × 总轨数 —— 各文件的单轨时长并不相同。
        """
        files = Chip.collect_files([d])
        if not files:
            return None
        n_tracks = 0
        secs = 0.0
        total = 0.0
        exact = True
        fmt = ""
        size = 0
        dur_src = ""
        for f in files:
            info = self._probe_cache_get(f)
            if info is None:
                continue
            n = max(1, int(getattr(info, "n_tracks", 0) or 0))
            one, one_exact, one_src = self._plan_for(f, info)
            n_tracks += n
            secs += one
            total += one * n
            exact = exact and one_exact
            dur_src = dur_src or one_src
            fmt = fmt or str(getattr(info, "fmt", "") or "")
            try:
                size += os.path.getsize(f)
            except OSError:
                pass
        return _DirMeta(fmt=fmt, n_tracks=n_tracks, est_secs=secs, exact=exact,
                        size=size, dur_src=dur_src, ok=True, total_secs=total)


    def _probe_cache_get(self, f):
        """按 (路径, mtime_ns, 大小) 缓存读文件头, 反复添加不重复读盘。"""
        try:
            st = os.stat(f)
            key = (f, st.st_mtime_ns, st.st_size)
        except OSError:
            key = (f, 0, 0)
        if key in self._probe_cache:
            return self._probe_cache[key]
        info = Chip.probe(f)
        self._probe_cache[key] = info
        return info

    def _plan_for(self, f, info=None):
        """本音源的 ``(计划秒数, 是否精确, 来源标签)``。"""
        if info is None:
            info = self._probe_cache_get(f)
        if info is None:
            return 0.0, False, ""
        secs, exact = Chip.plan(info, self._chip_default_secs(),
                                self._max_secs())
        src = str(getattr(info, "exact_src", "") or "")
        if not src:
            loop = float(getattr(info, "loop_end_secs", 0.0) or 0.0)
            src = "gbs_loop" if 0.2 < loop <= 3600 else (
                "user" if self._max_secs() > 0 else "default")
        return secs, exact, src

    def _probe_secs(self, f):
        """SPC 时长(秒), 按 (路径, mtime, 大小) 缓存, 反复添加不重复读盘。"""
        try:
            st = os.stat(f)
            key = ("spc", f, st.st_mtime_ns, st.st_size)
        except OSError:
            key = ("spc", f, 0, 0)
        if key in self._probe_cache:
            return self._probe_cache[key]
        try:
            v = spc_wav.probe(f)[2] or 0.0
        except Exception:                            # noqa: BLE001
            v = 0.0
        self._probe_cache[key] = v
        return v


    def _has_chip(self):
        return any(s.get("kind") == "CHIP" for s in self.sources)

    def _has_spc(self):
        return any(s.get("kind") == "SPC" for s in self.sources)

    def _refresh_info(self):
        """汇总音源, 并对 SPC 估算 WAV 体积(44.1kHz 下 = 时长 × 10.1MB/分钟)。

        **SPC-only 分支逐行保留原样**(AC-14: 纯 SPC 的汇总文案必须与改造前
        逐字相同); 只有列表里出现芯片音源时才走新的"按格式分组"文案。
        """
        if not self.sources:
            self.var_info.set("尚未添加音源")
            self._refresh_plan_text()
            return
        if not self._has_chip():
            secs_list = []
            for s in self.sources:
                if s["kind"] != "SPC":
                    continue
                try:
                    files = spc_wav.collect_spc([s["path"]])
                except OSError:
                    continue
                for f in files:
                    secs_list.append(self._probe_secs(f))
            txt = f"共 {len(self.sources)} 个音源"
            if secs_list:
                mult = 2.0 if spc_wav.POLICIES.get(
                    self.var_policy.get()) == "id666_x2" else 1.0
                cap = self._max_secs()
                secs = sum((min(s * mult, cap) if cap > 0 else s * mult)
                           for s in secs_list)
                gb = secs * spc_wav.BYTES_PER_SEC / 1073741824 / self._speed()
                txt += (f"（其中 SPC {len(secs_list)} 首，WAV 约 "
                        f"{int(secs // 60)}:{secs % 60:02.0f}，{gb:.2f} GB）")
            # v4.35: 光盘镜像也报一下曲目数与总时长（列表三列现在有值了）
            n_disc = sum(int(s.get("n_tracks") or 0) for s in self.sources
                         if s["kind"] == "光盘镜像")
            if n_disc:
                dsec = sum(float(s.get("total_secs") or 0.0) for s in self.sources
                           if s["kind"] == "光盘镜像")
                txt += (f"（光盘镜像 {n_disc} 条 CD-DA 音轨，"
                        f"{int(dsec // 60)}:{dsec % 60:04.1f}）")
            self.var_info.set(txt)
            self._refresh_plan_text()
            return

        # ---- 含芯片音源: 按格式分组(FR-5.1) ----
        groups = {}
        for s in self.sources:
            key = str(s.get("fmt") or s.get("kind") or "?")
            g = groups.setdefault(key, [0, 0])
            g[0] += 1
            g[1] += max(1, int(s.get("n_tracks") or 0))
        cap = self._max_secs()
        secs = 0.0
        unknown = False
        for s in self.sources:
            if s.get("kind") != "CHIP":
                continue
            # total_secs = 全部轨数 × 每轨时长; est_secs 只是**单轨**时长,
            # 直接拿它求和会把 HES 256 轨少算 256 倍(设计 §3.5 明确时长列
            # 显示单轨, 但体积/进度必须按总秒算)。
            one = float(s.get("total_secs") or 0.0)
            if one <= 0:
                unknown = True
                continue
            per = (one / max(1, int(s.get("n_tracks") or 1)))
            one = (min(per, cap) if cap > 0 else per) * max(
                1, int(s.get("n_tracks") or 1))
            secs += one
        txt = f"共 {len(self.sources)} 个音源 · " + " · ".join(
            f"{k} {v[0]} 个({v[1]} 首)" for k, v in sorted(groups.items()))
        if secs > 0 and not unknown:
            gb = secs * spc_wav.BYTES_PER_SEC / 1073741824 / self._speed()
            txt += (f" · WAV 约 {self._fmt_dur(secs)}，{gb:.2f} GB")
        elif unknown:
            txt += " · 时长按各音源自带信息与「默认单遍」估算"
        self.var_info.set(txt)
        self._refresh_plan_text()
        self._mark_warn_rows()

    def _chip_secs_of(self, entry):
        """本音源(芯片)计入体积/进度的**总**音频秒数, 已按单曲最长钳制。

        v4.31: 目录里混装的 CD 音轨（``cd_tracks``/``cd_secs``）要单独加。
        CD 音轨按原长转、**不吃「单曲最长」钳制** —— 拿给芯片音源猜循环终点
        的那个上限去砍 CD，会把一首 3 分钟的主题曲截成 150 秒。
        """
        n = max(1, int(entry.get("n_tracks") or 1))
        cd_n = max(0, int(entry.get("cd_tracks") or 0))
        cd_secs = max(0.0, float(entry.get("cd_secs") or 0.0))
        n_chip = max(0, n - cd_n)
        per = float(entry.get("est_secs") or 0.0)
        if per <= 0:
            total = float(entry.get("total_secs") or 0.0) - cd_secs
            per = total / n_chip if n_chip > 0 else 0.0
        if per <= 0:
            return cd_secs
        cap = self._max_secs()
        per = min(per, cap) if cap > 0 else per
        return per * n_chip + cd_secs

    def _mark_warn_rows(self):
        """预计产出的单行体积 >= 2 GiB 时把整行标成 amber(设计 §7.3)。"""
        per_byte = spc_wav.BYTES_PER_SEC / self._speed()
        for entry in self.sources:
            if entry.get("kind") != "CHIP":
                continue
            secs = self._chip_secs_of(entry)
            entry["warn"] = secs > 0 and secs * per_byte >= CHIP_WARN_BYTES
        self._refresh_rows()


    def _refresh_rows(self):
        """按 entry 里的 warn/ok 重新刷全部行的 tag 与文案(体量估算变化时用)。"""
        for entry in self.sources:
            if self.LIST_MODE == "list" or self.src_tree is None:
                return                                  # Listbox 无行级样式
            iid = entry.get("iid")
            if not iid:
                continue
            try:
                if not self.src_tree.exists(iid):
                    continue
                self.src_tree.item(iid, tags=self._tags_for(entry))
            except tk.TclError:
                continue

    def _refresh_plan_text(self):
        """③ 区底部那行: 逐源说明会用什么回放/淡出策略。"""
        if not hasattr(self, "var_plan"):
            return
        lines = []
        for s in self.sources:
            if s.get("kind") == "CHIP":
                per = float(s.get("est_secs") or 0.0)
                n = int(s.get("n_tracks") or 0)
                cd_n = int(s.get("cd_tracks") or 0)
                chip_n = max(0, n - cd_n)
                head = str(s.get("fmt") or "芯片音源")
                if chip_n > 1:
                    head += f" ×{chip_n}"
                # v4.31: 目录里混装 CD 音轨时分开写，否则「每首 2:30 ×7」
                # 与「合计」对不上（CD 不是 2:30，也不吃单曲上限）。
                if cd_n > 0:
                    head += f" + CD-DA ×{cd_n}"
                if per > 0:
                    mark = ("按文件自带时长（每首）"
                            if s.get("exact")
                            else f"按默认单遍 + 尾部 {CHIP_FADE_SECS:g}s 淡出")
                    tail = (f"，合计约 {self._fmt_dur(self._chip_secs_of(s))}"
                            if n > 1 else "")
                    lines.append(f"{s['label']} → {head} 每首 "
                                 f"{self._fmt_dur(per)}{tail}（{mark}）")
                else:
                    lines.append(f"{s['label']} → {head} 时长未知，"
                                 f"将按「单曲最长」截取")
            elif s.get("kind") == "SPC":
                lines.append(f"{s['label']} → SPC "
                             f"{self.var_policy.get()}")
            elif s.get("kind") == "CD":
                # v4.31: CD 音轨按原长转，不受「单曲最长」约束 —— 那句话
                # 是给芯片音源猜循环终点的，用在这里会把整首主题曲砍断。
                n = int(s.get("n_tracks") or 0)
                total = float(s.get("total_secs") or 0.0)
                head = f"CD-DA ×{n}" if n > 1 else "CD-DA"
                tail = (f"（合计 {self._fmt_dur(total)}）"
                        if total > 0 else "")
                lines.append(f"{s['label']} → {head} 按原长转 WAV，"
                             f"不截断{tail}")

        if not lines:
            self.var_plan.set("")
            return
        shown = lines[:4]
        text = "策略： " + "；".join(shown)
        if len(lines) > len(shown):
            text += f"；… 另有 {len(lines) - len(shown)} 个音源同策略"
        self.var_plan.set(text)

    def _estimate(self):
        """预估 (总曲数, 总音频秒数)。

        秒数按"遍数"放大: 只出 WAV 时解码与写盘在同一批里一次完成, 算 1 遍;
        要 CD 时先整批解码(1 遍)再写 BIN(1 遍); 再出 WAV 就加 1 遍。
        估不出来就返回 0, 进度退化成"音源×轨数"模型, 照样会动。

        改造点(FR-5.2): 原实现在"存在非 SPC 音源"时直接 ``return 0, 0.0``,
        于是混合格式的进度条永远只能靠兜底模型。现在对芯片音源改用**我们自己
        从文件头解析出的**轨数与时长(界面线程只读文件头, 微秒级, 不碰 libgme)。
        **SPC-only 分支逐行保留原样**(AC-14)。
        """
        if not self.sources:
            return 0, 0.0
        if not self._has_chip():
            # 自然结束模式的实际时长无法预估, 只用轨数模型, 免得百分比提前走满
            if spc_wav.POLICIES.get(self.var_policy.get()) == "natural":
                return sum(len(spc_wav.collect_spc([s["path"]]))
                           for s in self.sources), 0.0
            mult = 2.0 if spc_wav.POLICIES.get(
                self.var_policy.get()) == "id666_x2" else 1.0
            cap = self._max_secs()
            n, secs = 0, 0.0
            for s in self.sources:
                try:
                    files = spc_wav.collect_spc([s["path"]])
                except OSError:
                    continue
                n += len(files)
                for f in files:
                    v = self._probe_secs(f) * mult
                    secs += (min(v, cap) if cap > 0 else v)
            passes = (1 if self.var_wav.get() else 0) + \
                     (2 if self.var_cd.get() else 0)
            return n, secs / self._speed() * max(1, passes)

        n, secs = 0, 0.0
        unknown = False
        cap = self._max_secs()
        mult = 2.0 if spc_wav.POLICIES.get(
            self.var_policy.get()) == "id666_x2" else 1.0
        chip_secs = 0.0
        for s in self.sources:
            kind = s.get("kind")
            if kind == "CHIP":
                # 芯片源只出 WAV(不做 CD 镜像), 所以恒定 1 遍;
                # 秒数是"全部轨道加起来"(单轨 × 轨数), 不是单轨时长。
                n += max(1, int(s.get("n_tracks") or 0))
                one = self._chip_secs_of(s)
                if one <= 0:
                    unknown = True
                    continue
                chip_secs += one
            elif kind == "CD":
                # v4.31: CD 音轨只出 WAV（1 遍），时长由 ffmpeg 探测得到。
                # 探不到就置 unknown —— 宁可让进度走兜底模型，也不猜一个
                # 数出来让百分比骗人。累计进 chip_secs（同样只吃 WAV 一遍）。
                n += max(1, int(s.get("n_tracks") or 0))
                one = float(s.get("est_secs") or 0.0)
                if one <= 0:
                    unknown = True
                    continue
                chip_secs += one
            elif kind == "SPC":
                try:
                    files = spc_wav.collect_spc([s["path"]])
                except OSError:
                    continue
                n += len(files)
                for f in files:
                    v = self._probe_secs(f) * mult
                    secs += (min(v, cap) if cap > 0 else v)
            else:
                # 光盘镜像 / MP3 / MSU-1: 时长要真读文件才知道, 不猜
                n += 1
                unknown = True
        # 任一项估不出来就整体退回 0.0, 让 ConvertWorker._pct() 走兜底两级
        # 进度模型 —— 不会崩, 也不会显示一个错的百分比。
        passes = (1 if self.var_wav.get() else 0) + (2 if self.var_cd.get() else 0)
        if unknown:
            return n, 0.0
        # 芯片源不吃 CD 那一遍, 分开算再合并, 免得高估 2~3 倍
        total = (chip_secs * max(1, (1 if self.var_wav.get() else 0))
                 + secs * max(1, passes))
        return n, total / self._speed()


    def _speed(self):
        try:
            v = float(str(self.var_speed.get()).strip().rstrip("%"))
        except (TypeError, ValueError):
            return 1.0
        if v > 10:
            v /= 100.0
        return max(0.5, min(2.0, v))

    def _max_secs(self):
        try:
            return max(0.0, min(3600.0, float(str(self.var_max.get()).strip())))
        except (TypeError, ValueError):
            return 0.0

    def _chip_default_secs(self):
        """③ 区「默认单遍」读数。非法/越界都回落到 150s 并钳到 [30, 600]。"""
        try:
            v = float(str(self.var_chip_secs.get()).strip())
        except (TypeError, ValueError, AttributeError):
            v = CHIP_DEFAULT_SECS
        if v != v or v <= 0:                       # NaN 或 <=0 都当没填
            v = CHIP_DEFAULT_SECS
        return max(float(CHIP_SECS_MIN), min(float(CHIP_SECS_MAX), v))

    def _sync_chip_controls(self):
        """按列表内容启用/禁用 ③ 区的控件。

        * 「默认单遍」只对芯片音源有意义 -> 列表里没有 CHIP 时置灰;
        * 「SPC 播放长度」与「速度补偿」只对 SPC 有意义 -> 没有 SPC 时置灰。
        """
        chip_ok = self._has_chip()
        spc_ok = self._has_spc()
        try:
            self.spin_chip_secs.config(
                state="normal" if chip_ok else "disabled")
            self.cb_policy.config(state="readonly" if spc_ok else "disabled")
            self.cb_speed.config(state="normal" if spc_ok else "disabled")
        except (tk.TclError, AttributeError):
            pass

    def remove_selected(self):
        entries = self._ui_list_selected_entries()
        if not entries:
            return
        iids = [e["iid"] for e in entries]
        # Listbox 模式下 _ui_list_delete 需要"删除前"的下标, 所以先算好
        # order 快照(它按 self.sources 当前顺序建立)。
        self._ui_list_delete(iids)
        dead = set(iids)
        self.sources = [e for e in self.sources if e["iid"] not in dead]
        for iid in iids:
            self._idx_of_iid.pop(iid, None)
        # 删中间行会让后面所有下标平移, 必须重建映射
        self._reindex()
        self._refresh_info()

    def clear_sources(self):
        self.sources.clear()
        self._idx_of_iid.clear()
        self._ui_list_clear()
        self._refresh_info()

    def pick_out(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.var_out.set(os.path.normpath(d))

    def _sync_merge_state(self):
        """WAV 没勾时把「多轨合并」置灰（v4.24）。

        没有 WAV 输出，合并就无从谈起；但**不擅自取消勾选** —— 用户先勾
        合并、再勾 WAV 也能用，取消 WAV 时保留他的选择更不容易被当成 bug。
        """
        cb = getattr(self, "cb_merge", None)
        if cb is None:
            return
        try:
            cb.config(state="normal" if self.var_wav.get() else "disabled")
        except tk.TclError:
            pass

    def open_out(self):
        out = self.var_out.get()
        if os.path.isdir(out):
            os.startfile(out)  # noqa: S606  Windows
        else:
            messagebox.showinfo("提示", f"输出目录尚未创建:\n{out}")

    # ---- 云盘上传（v4.19）--------------------------------------------------
    def upload_cloud(self):
        """输出目录里的 WAV → FLAC → 网易云云盘。

        为什么要转 FLAC：云盘接口**只收 mp3/flac**，官方客户端能拖 WAV 是
        它自己先转了一道码。FLAC 是逐样本无损的，不会掉音质。

        v4.23 起登录校验 / 体积确认 / 逐首上传都交给模块级
        ``ensure_cloud_session`` + ``CloudUploader``（与下载窗共用一套），
        本方法只负责「取目录 → 按钮状态 → 起线程」。
        """
        if ncm is None:
            messagebox.showerror("缺少依赖", _CLOUD_MISSING_MSG)
            return
        out = self.var_out.get()
        if not os.path.isdir(out):
            messagebox.showinfo("提示", "输出目录还不存在：\n%s" % out)
            return
        wavs = collect_wavs(out)
        if not wavs:
            messagebox.showinfo("提示",
                                "输出目录里没有 WAV 文件：\n%s\n\n先转换一次再来。" % out)
            return
        if getattr(self, "_cloud_busy", False):
            messagebox.showinfo("提示", "上一次上传还在跑，等它结束再来")
            return

        # v4.25：合并文件与分轨并存时先问清楚传哪一份 —— 两者内容重复，
        # 不选就会把同一张专辑传两遍。登录之前问，取消时不会先弹登录窗。
        wavs = plan_cloud_upload(self.root, wavs)
        if wavs is None:
            self.log("☁ 已取消上传")
            return
        if not wavs:
            return

        sess = ensure_cloud_session(self.root, self.log, self.var_status.set)
        if sess is None:
            return
        if not confirm_cloud_upload(self.root, wavs):
            return

        self._cloud_busy = True
        self.btn_cloud.config(state="disabled")
        self.log("=" * 62)
        self.log("☁ 开始上传 %d 个文件到网易云云盘…" % len(wavs))
        self.var_status.set("☁ 上传中…")
        threading.Thread(target=self._cloud_worker, args=(wavs, sess),
                         daemon=True).start()

    def _cloud_worker(self, wavs, sess):
        """后台上传。**只往 msgq 塞事件**，一个 Tk 对象都不碰。"""
        CloudUploader(
            sess,
            lambda kind, payload: self.msgq.put(("cloud", kind, payload)),
        ).run(wavs)

    def _on_cloud_event(self, action, payload):
        """主线程侧：把上传线程的事件翻成日志/状态栏。"""
        if action == "log":
            self.log(payload)
        elif action == "prog":
            self.var_status.set("☁ 上传中 %d%%" % int(payload or 0))
        elif action == "done":
            ok, skip, fail = payload
            self._cloud_busy = False
            self.btn_cloud.config(state="normal")
            self.log("-" * 62)
            self.log("☁ 云盘上传结束：成功 %d / 跳过 %d / 失败 %d"
                     % (ok, skip, fail))
            if fail:
                self.log("   （失败的多半是版权校验拦截或网络抖动，可稍后重试）")
            self.var_status.set("☁ 上传完成：成功 %d / 跳过 %d / 失败 %d"
                                % (ok, skip, fail))
            messagebox.showinfo(
                "上传结束",
                "成功 %d 首，跳过 %d 首（云端已有），失败 %d 首。\n\n"
                "打开网易云音乐 → 我的音乐云盘 即可查看。"
                % (ok, skip, fail))

    def open_download(self):
        """打开「按机种下载音源」窗口 (单例: 已开则提到前面)。"""
        dlg = getattr(self, "_dlg", None)
        if dlg is not None:
            try:
                if dlg.winfo_exists():
                    dlg.deiconify()
                    dlg.lift()
                    dlg.focus_set()
                    return
            except Exception:                        # noqa: BLE001
                pass
        self._dlg = DownloadDialog(self.root)
        self.log("⬇ 打开音源下载窗口 (joshw.info 全机种镜像)")

    def _welcome_log(self):
        """首屏欢迎块。

        日志区是唯一可伸缩的控件, 默认一大片空白, 看着像没加载完。这里写三行,
        顺便把用法说清楚。只在启动时写一次(log 之后的内容照常追加在后面)。
        """
        try:
            self.logw.config(state="normal")
            # 标题行刻意短：日志区宽 500px、等宽 12px，一行放不下全部信息时
            # Tk 会自己折行，折出来的半截 "· BIN+CUE" 很难看（v4.28 拆两行）。
            self.logw.insert(
                "end",
                f" ▄▀▄ RetroMusic Lab v{APP_VER}\n",
                "banner")
            self.logw.insert(
                "end",
                "     SPC / PCM → WAV · BIN+CUE\n",
                "hint")
            self.logw.insert(
                "end",
                " ① 添加音源或下载音源   ② 选输出格式   ③ 开始转换\n",
                "hint")
            self.logw.insert(
                "end",
                " 提示：.spc / .nsf / .gbs / .vgm / .hes 文件夹可以直接拖进窗口；"
                "下载的音源会自动解压\n",
                "hint")

            self.logw.config(state="disabled")
        except tk.TclError:
            pass

    def log(self, text):
        dbg(text)
        self.logw.config(state="normal")
        self.logw.insert("end", text + "\n")
        self.logw.see("end")
        self.logw.config(state="disabled")

    # ---- 转换控制 -------------------------------------------------------------
    def start(self):
        if not self.sources:
            messagebox.showwarning("提示", "请先添加至少一个音源")
            return
        if not (self.var_cd.get() or self.var_wav.get()):
            messagebox.showwarning("提示", "请至少选择一种输出格式 (CD / WAV)")
            return
        try:
            gap = float(self.var_gap.get())
        except ValueError:
            gap = 2.0
        out_dir = self.var_out.get().strip()
        if not out_dir:
            messagebox.showwarning("提示", "请设置输出目录")
            return

        # 体量保护(设计 §6.4 / 风险 R3): 只告知 + 让用户决策, **不自动收紧**
        # 默认时长(用户已拍板统一 150s)。HES 单文件 256 轨能到 38400s ->
        # 38400 × 176400 B ≈ 6.3 GB, 足以把 SD 卡撑爆, 必须提前说清。
        est_bytes = self._estimate_bytes()
        if est_bytes > CHIP_WARN_BYTES:
            gb = est_bytes / 1024 ** 3
            if not messagebox.askyesno(
                    "体量提示",
                    f"按当前设置预计产出约 {gb:.2f} GB WAV\n"
                    f"（主要是多轨芯片音源）。\n\n"
                    f"建议：把「单曲最长」设为 60~90 秒可显著减小体积。\n\n"
                    f"继续转换？"):
                return

        # 全是芯片音源却只勾了 CD: 明确告知"只能出 WAV"并强制打开 WAV,
        # 免得跑完 0 个文件还一头雾水(设计 §7.9)。
        if self.var_cd.get() and not self.var_wav.get() and \
                all(s.get("kind") == "CHIP" for s in self.sources):
            if not messagebox.askyesno(
                    "输出格式确认",
                    "所选均为多格式芯片音源（NSF / GBS / VGM / HES 等），"
                    "本工具仅支持其输出 WAV。\n\n是否继续并输出 WAV？"):
                return
            self.var_wav.set(True)

        # 全为 SPC 时能预先算出总曲数与总音频秒数: 日志分母显示 "05/92",
        # 进度条也能按"已处理音频秒"精确推进, 剩余时间才靠得住
        total_tracks, est_secs = self._estimate()
        opts = {
            "do_cd": self.var_cd.get(),
            "do_wav": self.var_wav.get(),
            "out_dir": out_dir,
            "album": self.var_album.get().strip(),
            "performer": self.var_performer.get().strip(),
            "gap": gap,
            "verify": self.var_verify.get(),
            "cd_wav": self.var_cd_wav.get(),
            "policy": spc_wav.POLICIES[self.var_policy.get()],
            "policy_label": self.var_policy.get(),
            "speed": self._speed(),
            "max_secs": self._max_secs(),
            "chip_secs": self._chip_default_secs(),
            "total_tracks": total_tracks,
            "est_secs": est_secs,
            "cur_kind": "",
            # v4.24: 多轨合并（一个音源出多个 WAV 时再拼一个整轨文件）
            "merge_wav": bool(self.var_merge.get() and self.var_wav.get()),
            # v4.39: XA 只导音乐（滤掉音效/语音）
            "xa_music_only": bool(self.var_xa_music.get()),
            # v4.43: XA 按静音间隔切曲
            "xa_split": bool(self.var_xa_split.get()),
        }
        self.log("=" * 62)
        self.log(f"开始: {len(self.sources)} 个音源 | "
                 + " + ".join([x for x in (
                     "CD" if opts["do_cd"] else "",
                     "WAV" if opts["do_wav"] else "") if x]))
        self.log(f"输出: {out_dir} (平铺, 无子目录)")
        if self._has_spc():
            self.log(f"SPC: {self.var_policy.get()} | "
                     f"速度补偿 {opts['speed'] * 100:.0f}% | "
                     f"单曲最长 "
                     + (f"{opts['max_secs']:.0f}s" if opts["max_secs"] > 0 else "不限"))
        if self._has_chip():
            self.log(f"芯片音源: 默认单遍 {opts['chip_secs']:.0f}s | "
                     f"尾部 {CHIP_FADE_SECS:g}s 淡出 | "
                     f"单曲最长 "
                     + (f"{opts['max_secs']:.0f}s" if opts["max_secs"] > 0 else "不限"))
            if opts["do_cd"]:
                n_chip = sum(1 for s in self.sources if s.get("kind") == "CHIP")
                n_cd = sum(1 for s in self.sources if s.get("kind") == "CD")
                parts = []
                if n_chip:
                    parts.append(f"{n_chip} 个芯片音源")
                if n_cd:
                    parts.append(f"{n_cd} 个 CD 音轨")
                if parts:
                    self.log(f"⚠ 其中 {' / '.join(parts)} 仅输出 WAV, "
                             f"不参与 CD 镜像（转换时会逐条提示）")
        self.cancel_event.clear()
        self._run_t0 = time.time()
        self._set_progress(0.0, "准备中…", 0.0, None)
        self.btn_start.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.worker = ConvertWorker(list(self.sources), opts,
                                    self.msgq, self.cancel_event)
        self.worker.start()

    def _estimate_bytes(self):
        """按当前设置估算产出的 WAV 总字节(体量保护用)。"""
        _n, secs = self._estimate()
        if secs <= 0:
            return 0
        return secs * spc_wav.BYTES_PER_SEC


    def cancel(self):
        self.cancel_event.set()
        self.var_status.set("正在取消… (当前文件写完后停止)")
        self.var_eta.set("")
        self.btn_cancel.config(state="disabled")

    # ---- 队列轮询 -------------------------------------------------------------
    @staticmethod
    def _fmt_dur(secs):
        """秒 -> 1:02:03 / 2:05"""
        secs = max(0, int(round(secs)))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _set_progress(self, pct, text=None, elapsed=None, eta=None):
        """刷新进度条 / 百分比 / 状态文字 / 剩余时间。"""
        try:
            pct = max(0.0, min(100.0, float(pct)))
        except (TypeError, ValueError):
            pct = 0.0
        self.progress["value"] = pct
        # 用 int 向下取整: 99.9% 不能显示成 100%, 那会让人以为已经结束
        self.var_pct.set(f"{int(pct)}%")
        if text:
            self.var_status.set(text)
        if elapsed is None:
            return
        if pct >= 100.0:
            self.var_eta.set(f"用时 {self._fmt_dur(elapsed)} · 完成")
        elif eta is None:
            self.var_eta.set(f"已用 {self._fmt_dur(elapsed)} · 剩余 计算中…")
        elif eta < 1:
            self.var_eta.set(f"已用 {self._fmt_dur(elapsed)} · 剩余 即将完成")
        else:
            self.var_eta.set(f"已用 {self._fmt_dur(elapsed)} · "
                             f"剩余约 {self._fmt_dur(eta)}")

    def _apply_progress(self, msg):
        try:
            self._set_progress(*msg[1:])
        except Exception:                            # noqa: BLE001
            traceback.print_exc()

    def _poll_queue(self):
        latest = None
        try:
            while True:
                msg = self.msgq.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    latest = msg        # 同一轮只渲染最后一条, 不刷界面
                    continue
                if latest is not None:
                    # 先落地进度再处理"完成/取消", 否则 99.9% 会盖掉 100%
                    self._apply_progress(latest)
                    latest = None
                if kind == "play_ready":
                    # 试听解码完成（工作线程只投消息，这里在主线程碰 Tk）
                    self._on_play_ready(msg[1])
                    continue
                if kind == "play_error":
                    self._on_play_error(msg[1])
                    continue
                if kind == "cloud":
                    # 云盘上传线程的事件（log / prog / done）
                    self._on_cloud_event(msg[1], msg[2] if len(msg) > 2 else None)
                    continue
                if kind == "log":
                    self.log(msg[1])
                elif kind == "status":
                    self.var_status.set(msg[1])
                elif kind == "src":
                    _, i, n, label = msg
                    self.var_status.set(f"[{i}/{n}] {label}")
                elif kind == "error":
                    self.log("❌ " + msg[1])
                    self._finish("转换失败")
                elif kind == "chip_summary":
                    # "N 个已转 WAV / M 个跳过" —— 跳过必须可见, 不能静默。
                    # 结果挂到 self._chip_summary, 由随后的 done 分支写进状态行
                    # (同一批消息里 chip_summary 一定排在 done 之前)。
                    self._chip_summary = (msg[1] if len(msg) > 1 else (0, 0))
                elif kind == "done":
                    self._set_progress(100.0, "全部完成",
                                       time.time() - self._run_t0)
                    self.log("=" * 62)
                    self.log("🎉 全部完成!")
                    for fmtname, name, n in msg[1]:
                        self.log(f"   [{fmtname}] {name}: {n} 轨")
                    wav_n, skip_n = getattr(self, "_chip_summary", (0, 0))
                    if skip_n:
                        self.log(f"📋 汇总: {wav_n} 个音源已转 WAV，"
                                 f"{skip_n} 个跳过 CD 镜像"
                                 f"（芯片音源 / CD 音轨仅支持 WAV）")
                    self._finish("✅ 转换完成")
                    self.btn_cloud.config(state="normal")
                    if skip_n:
                        self.var_status.set(
                            f"✅ 完成 · {wav_n} 个已转 WAV / {skip_n} 个跳过")
                    messagebox.showinfo(
                        "完成", f"转换完成! 输出目录:\n{self.var_out.get()}")

                elif kind == "cancelled":
                    self.var_eta.set("")
                    self.log("⛔ 已取消")
                    self._finish("已取消")
        except queue.Empty:
            pass
        except Exception:                            # noqa: BLE001
            traceback.print_exc()
        if latest is not None:
            self._apply_progress(latest)
        if not self._closing:
            self._after_msg = self.root.after(100, self._poll_queue)

    def _finish(self, status):
        self.var_status.set(status)
        self.btn_start.config(state="normal")
        self.btn_cancel.config(state="disabled")


# ---------------------------------------------------------------------------
# 无界面模式
# ---------------------------------------------------------------------------

def _make_disc_fixture(base):
    """生成合成光盘镜像 (2 条 AUDIO 音轨, big-endian CD-DA) 用于自检."""
    import shutil
    d = os.path.join(base, "selftest_disc")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    bin_path = os.path.join(d, "game.bin")
    cue_path = os.path.join(d, "game.cue")

    def track_bytes(seconds, freq):
        n = int(seconds * 44100)
        little = bytearray()
        for i in range(n):
            v = int(18000 * math.sin(2 * math.pi * freq * i / 44100))
            little += struct.pack("<h", v)
            little += struct.pack("<h", v)  # 立体声
        return core.le_to_be(bytes(little))  # -> big-endian

    t1 = track_bytes(2, 440)
    t2 = track_bytes(3, 660)
    # 轨1 占 0..150 扇区; 轨2 从 300 扇区开始 (中间留 150 扇区间隙)
    gap = (300 - 150) * core.SECTOR
    with open(bin_path, "wb") as fh:
        fh.write(t1)
        fh.write(b"\x00" * gap)
        fh.write(t2)
    cue = ('FILE "game.bin" BINARY\n'
           '  TRACK 01 AUDIO\n'
           '    INDEX 01 00:00:00\n'
           '  TRACK 02 AUDIO\n'
           '    INDEX 01 00:04:00\n')
    with open(cue_path, "w", encoding="utf-8") as f:
        f.write(cue)
    return cue_path


def _make_mp3_fixture(base):
    """生成合成 MP3 (静音帧) 用于自检 MP3 解码管线."""
    import shutil
    d = os.path.join(base, "selftest_mp3")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413  # MPEG1 L3 44.1kHz 128kbps 静音帧
    p = os.path.join(d, "silence.mp3")
    with open(p, "wb") as f:
        f.write(frame * 20)
    return p


def _run_worker(sources, opts):
    """同步跑一遍 ConvertWorker, 收集日志与结果(自检/命令行用)。"""
    q = queue.Queue()
    ev = threading.Event()
    w = ConvertWorker(sources, opts, q, ev)
    w.run()
    logs, results, errors = [], [], []
    while not q.empty():
        m = q.get_nowait()
        if m[0] == "log":
            logs.append(m[1])
        elif m[0] == "done":
            results = m[1]
        elif m[0] in ("error", "warn"):
            errors.append(m[1])
    return results, errors, logs


def _default_opts(out_dir, **kw):
    opts = {
        "do_cd": False, "do_wav": True, "out_dir": out_dir,
        "album": "", "performer": "", "gap": 2.0, "verify": True,
        "cd_wav": False, "policy": "id666",
        "policy_label": spc_wav.DEFAULT_POLICY_LABEL,
        "speed": 1.0, "max_secs": 0.0,
        "chip_secs": CHIP_DEFAULT_SECS,
        "total_tracks": 0, "cur_kind": "", "merge_wav": False,
        "xa_music_only": False, "xa_split": False,
    }
    opts.update(kw)
    return opts


def run_spc_wav(argv):
    """--convert <输出目录> [--policy p] [--speed 100] [--max-secs N] <spc...>

    纯 SPC -> WAV 批量转换, 不开窗口(重跑整张专辑用)。
    """
    args = argv[1:]
    opts = {"policy": "id666", "speed": 1.0, "max_secs": 0.0}
    # v4.39: 纯数据盘改抽 XA 时只留音乐（滤掉音效/语音）
    xa_music_only = "--xa-music-only" in args
    if xa_music_only:
        args = [a for a in args if a != "--xa-music-only"]
    # v4.43: XA 按静音间隔切曲
    xa_split = "--xa-split" in args
    if xa_split:
        args = [a for a in args if a != "--xa-split"]
    for flag, key in (("--policy", "policy"), ("--speed", "speed"),
                      ("--max-secs", "max_secs")):
        if flag in args:
            i = args.index(flag)
            try:
                val = args[i + 1]
            except IndexError:
                val = ""
            if key == "policy":
                opts["policy"] = val or "id666"
            elif key == "speed":
                try:
                    v = float(val)
                    opts["speed"] = max(0.5, min(2.0, v / 100.0 if v > 10 else v))
                except ValueError:
                    opts["speed"] = 1.0
            else:
                try:
                    opts["max_secs"] = max(0.0, float(val))
                except ValueError:
                    opts["max_secs"] = 0.0
            args = args[:i] + args[i + 2:]
    if not args:
        print("用法: --convert <输出目录> [--policy id666|id666_x2|natural] "
              "[--speed 100|115|120] [--max-secs 秒] [--xa-music-only] "
              "[--xa-split] <spc / CD 音轨(.ape/.flac) / 光盘镜像(.cue+.bin/.iso) 文件或文件夹...>")
        return 2
    out_dir, inputs = args[0], args[1:]
    files = spc_wav.collect_spc(inputs)
    if not files:
        # v4.30: 没有 .spc 但有 CD 音轨（.ape/.flac…）时转 CD 音轨。
        # 这条路也是「打包后 CD 解码是否真的可用」的端到端验证入口。
        cds = _collect_cd_audio(inputs)
        if cds:
            return _convert_cd_cli(cds, out_dir)
        # v4.34: 光盘镜像（.cue/.bin，含「整包文件夹」）→ 抽 CD-DA 轨转 WAV。
        # 放在最后：镜像优先级低于 .spc 与单轨 CD 音轨，不影响老路径。
        if any(_looks_like_disc(p) for p in inputs):
            return _convert_disc_cli(inputs, out_dir, xa_music_only, xa_split)
        print("没有找到 .spc / CD 音轨 / 光盘镜像 文件")
        return 2
    print(f"{len(files)} 首 → {out_dir} (policy={opts['policy']}, "
          f"speed={opts['speed'] * 100:.0f}%, max_secs={opts['max_secs']:.0f})",
          flush=True)
    stat = spc_wav.convert(files, out_dir, opts["policy"], on_log=print,
                           speed=opts["speed"], max_secs=opts["max_secs"])
    print(f"结果: ok={stat['ok']} fail={stat['fail']} "
          f"{stat['seconds'] / 60:.1f} 分钟", flush=True)
    return 0 if stat["ok"] and not stat["fail"] else 1


def _collect_cd_audio(inputs):
    """CLI 用：从文件/文件夹（只扫一层）里挑出 CD 音轨。"""
    if cd_audio is None:
        return []
    out = []
    for p in inputs or ():
        p = str(p)
        if os.path.isdir(p):
            try:
                for n in sorted(os.listdir(p)):
                    q = os.path.join(p, n)
                    if os.path.isfile(q) and cd_audio.is_cd_audio(q):
                        out.append(q)
            except OSError:
                continue
        elif os.path.isfile(p) and cd_audio.is_cd_audio(p):
            out.append(p)
    return out


def _convert_cd_cli(files, out_dir, max_secs=0.0):
    """CLI：批量把 CD 音轨转 WAV（编号连续、同名避让）。"""
    print("%d 首 CD 音轨 → %s" % (len(files), out_dir), flush=True)
    used, idx, ok, fail = set(), 0, 0, 0
    for f in files:
        r = cd_audio.convert_one(f, out_dir, used=used, start_idx=idx,
                                 max_secs=max_secs, on_log=print)
        idx = r["last_idx"]
        if r["ok"]:
            ok += 1
        else:
            fail += 1
            print("   ❌ %s — %s" % (os.path.basename(f), r["errors"][:1]))
    print("结果: ok=%d fail=%d" % (ok, fail), flush=True)
    return 0 if ok and not fail else 1


def _looks_like_disc(p):
    """CLI 用：这个输入是不是光盘镜像（.cue/镜像文件，或含 cue+镜像的文件夹）。

    v4.38: 镜像文件不再只认 .bin —— PCE-CD 的 ``.cue + .iso`` 是单文件镜像。
    """
    p = str(p)
    if os.path.isfile(p):
        return p.lower().endswith((".cue",) + core.DISC_IMG_EXTS)
    if os.path.isdir(p):
        try:
            names = [n.lower() for n in os.listdir(p)]
        except OSError:
            return False
        return any(n.endswith(".cue") for n in names) and \
            any(n.endswith(core.DISC_IMG_EXTS) for n in names)
    return False


def _convert_disc_cli(inputs, out_dir, xa_music_only=False, xa_split=False):
    """CLI：光盘镜像 → 抽 CD-DA 轨 → WAV。

    ⚠ 镜像不吃 ``--max-secs``：抽出来的是完整曲目，不是循环 BGM
    （与主窗 ``_do_other`` 对 ``kind == "光盘镜像"`` 的处理一致）。

    ``xa_music_only``（v4.39）：纯数据盘改抽 XA 时只留音乐（滤掉音效/语音）。
    ``xa_split``（v4.43）：XA 按静音间隔切曲。
    """
    paths = [str(p) for p in inputs or ()]
    try:
        tracks = []
        stats = {}
        for p in paths:
            tracks += core.load_source(p, xa_music_only=xa_music_only,
                                       xa_stats=stats, xa_split=xa_split)
    except (RuntimeError, SystemExit) as e:
        print("镜像解析失败: %s" % e, flush=True)
        return 2
    if not tracks:
        print("镜像里没有 AUDIO(CD-DA) 音轨", flush=True)
        return 2
    if stats.get("skipped"):
        print("XA 只导音乐: %d 条流里滤掉 %d 条音效/语音，保留 %d 条"
              % (stats["total"], stats["skipped"], stats["kept"]), flush=True)
    if stats.get("split_streams"):
        print("XA 按静音切曲: %d 条流切成 %d 段"
              % (stats["split_streams"], stats["segments"]), flush=True)
    print("%d 条音轨 → %s" % (len(tracks), out_dir), flush=True)
    # v4.44: 写完必须清临时 PCM。以前这条 CLI 路径**漏了** cleanup，
    # 大体积落盘（_SpoolPcm）会留在临时目录里（v4.44 前是 C 盘 %TEMP%，
    # 实测 FF7 Disc B 一次就留 186MB）。GUI 那条路径三处都有清，唯独这里没有。
    try:
        stat = spc_wav.write_tracks(tracks, out_dir, 0.0, on_log=print)
    finally:
        core.cleanup_spc_temp(tracks)
    print("结果: ok=%d fail=%d %.1f 分钟"
          % (stat["ok"], stat["fail"], stat["seconds"] / 60), flush=True)
    return 0 if stat["ok"] and not stat["fail"] else 1


def run_selftest(extra_spc):
    """打包自检: MSU-1 样本 + 合成光盘镜像 + 合成 MP3 (+ 可选 SPC) 各跑一遍。"""
    import shutil
    import wave
    base = base_dir()
    out = os.path.join(base, "selftest_out")
    shutil.rmtree(out, ignore_errors=True)

    # MSU-1 样本目录: exe 在 dist/ 下, 样本在项目根的 test/, 两处都找
    pcm_dir = None
    for cand in (os.path.join(os.getcwd(), "test"), os.path.join(base, "test"),
                 os.path.join(os.path.dirname(base), "test")):
        if os.path.isdir(cand) and any(f.lower().endswith(".pcm")
                                       for f in os.listdir(cand)):
            pcm_dir = cand
            break

    sources = [
        {"label": "game.cue", "path": _make_disc_fixture(base), "kind": "光盘镜像"},
        {"label": "silence.mp3", "path": _make_mp3_fixture(base), "kind": "MP3"},
    ]
    if pcm_dir:
        sources.insert(0, {"label": "test", "path": pcm_dir, "kind": "MSU-1"})
    spc_src = None
    if extra_spc:
        spc_src = os.path.abspath(extra_spc[0])
        sources.append({"label": "spc", "path": spc_src, "kind": "SPC"})

    n_spc = len(spc_wav.collect_spc([spc_src])) if spc_src else 0
    _results, errors, _logs = _run_worker(
        sources, _default_opts(out, do_cd=True, do_wav=True,
                               album="SelfTest", performer="Test",
                               total_tracks=(3 + 2 + 1 + n_spc) if pcm_dir
                               else (2 + 1 + n_spc)))

    # ---- 平铺校验: 输出目录下不允许出现任何子目录 ----
    names = os.listdir(out) if os.path.isdir(out) else []
    subdirs = sorted(d for d in names if os.path.isdir(os.path.join(out, d)))
    wavs = sorted(f for f in names if f.lower().endswith(".wav"))
    bins = sorted(f for f in names if f.lower().endswith(".bin"))
    cues = sorted(f for f in names if f.lower().endswith(".cue"))

    problems = []
    if subdirs:
        problems.append("存在子目录:" + ",".join(subdirs))
    if not bins:
        problems.append("缺少 BIN")
    if not cues:
        problems.append("缺少 BIN/CUE 配对: " + f"{len(bins)}/{len(cues)}")
    expect_wav = (3 if pcm_dir else 0) + 2 + 1 + n_spc   # test + 光盘 + MP3 + SPC
    if len(wavs) != expect_wav:
        problems.append(f"WAV 数量 {len(wavs)} != 预期 {expect_wav}")
    if not pcm_dir:
        print("WARN 未找到 test/*.pcm 样本, 跳过 MSU-1 分支")

    # 平铺后序号必须跨音源连续, 否则说明有同名覆盖
    idxs = []
    for f in wavs:
        m = re.match(r"^(\d{2,})_", f)
        if m:
            idxs.append(int(m.group(1)))
    if sorted(idxs) != list(range(1, len(wavs) + 1)):
        problems.append(f"WAV 序号不连续: {sorted(idxs)[:10]}")

    # 每个 WAV 必须是 16bit / 44100 / 立体声
    bad_fmt = []
    for f in wavs:
        p = os.path.join(out, f)
        try:
            with wave.open(p, "rb") as w:
                if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (2, 2, 44100):
                    bad_fmt.append(f)
        except Exception:                            # noqa: BLE001
            bad_fmt.append(f)

    shutil.rmtree(out, ignore_errors=True)
    shutil.rmtree(os.path.join(base, "selftest_disc"), ignore_errors=True)
    shutil.rmtree(os.path.join(base, "selftest_mp3"), ignore_errors=True)
    # ---- v4.0: 下载后自动转 WAV 的转换基座自检 ----
    # 目的: 打包后最怕的是「dl_convert 没进exe」-> 开关静默禁用, 而
    # --selftest 只验主窗口会全绿、看不出问题。所以这里**必须**单独点名
    # dl_convert 并跑一遍分流 + manifest + 编号续编（都不需要真解码）。
    cv_problems = []
    if dl_convert is None:
        cv_problems.append("dl_convert 未打进包（转 WAV 功能会禁用）")
    else:
        try:
            if dl_convert.policy_for("x.spc") != "id666":
                cv_problems.append("policy_for(.spc) 应为 id666")
            if dl_convert.policy_for("x.nsf") != "chip":
                cv_problems.append("policy_for(.nsf) 应为 chip")
            for ext in (".nsfe", ".gbs", ".vgm", ".vgz", ".hes",
                        ".kss", ".gym", ".sap", ".ay"):
                if dl_convert.policy_for("x" + ext) != "chip":
                    cv_problems.append(f"policy_for({ext}) 应为 chip")
            cvdir = os.path.join(base, "selftest_dlconvert")
            shutil.rmtree(cvdir, ignore_errors=True)
            os.makedirs(cvdir, exist_ok=True)
            wav_dir = os.path.join(cvdir, "WAV")
            os.makedirs(wav_dir, exist_ok=True)
            # 预置一个 25 字节的"用户旧数据"占位 -> 验编号续编不覆盖（R7）
            with open(os.path.join(wav_dir, "01_Stage Clear.wav"), "wb") as fh:
                fh.write(b"OLD-USER-DATA-25BYTE")
            max_idx, used = dl_convert.wav_scan_numbering(wav_dir)
            if max_idx != 1:
                cv_problems.append(f"wav_scan_numbering max_idx={max_idx} != 1")
            # ⚠ used 的键必须是**裸曲名**: 既不含 ".wav" 也**不含 "NN_" 前缀**
            #   （对齐 spc_wav.py:154 的 used.add(name.lower())）。
            #   带序号注入不会报错，只让重名避让静默失效。
            if "stage clear" not in used:
                cv_problems.append(
                    "wav_scan_numbering used 键未对齐裸曲名（缺 'stage clear'）")
            if "01_stage clear" in used:
                cv_problems.append(
                    "wav_scan_numbering used 键残留 NN_ 前缀（不应有 "
                    "'01_stage clear'）")
            man = dl_convert.ConvertManifest(
                os.path.join(wav_dir, dl_convert.MANIFEST_NAME))
            if man.load() != 0:
                cv_problems.append("空 manifest load() 应为 0")
            with open(os.path.join(cvdir, "probe.nsf"), "wb") as fh:
                fh.write(b"NESM\x1a" + b"\x00" * 200)
            if man.already_done(os.path.join(cvdir, "probe.nsf")):
                cv_problems.append("未记账的文件 already_done 应为 False")
            shutil.rmtree(cvdir, ignore_errors=True)
        except Exception as e:                       # noqa: BLE001
            cv_problems.append("dl_convert 自检异常: %s: %s"
                               % (type(e).__name__, e))

    for e in errors:
        print("ERR", e)
    ok = not problems and not bad_fmt and not errors and not cv_problems
    print("SELFTEST", "PASS" if ok else "FAIL",
          "" if not problems else "| " + " | ".join(problems),
          "" if not bad_fmt else "| badfmt:" + ",".join(bad_fmt),
          "" if not cv_problems else "| dlconvert: " + " | ".join(cv_problems))
    return 0 if ok else 1


def set_dpi_awareness() -> bool:
    """让打包后的 exe 走真分辨率, 不做 DPI 虚拟化。

    PyInstaller 的窗口版 bootloader 默认不带 DPI 感知, 125% 缩放下 Windows 会
    骗它"屏幕只有 1536x864", 于是 winfo_screenheight() 拿到 864, 自适应高度被
    钳到 780, 整个窗口还被按比例缩放 —— 实测外框只有 670x822(比源码运行的
    836x1019 小一圈), 字体发虚。声明感知后屏幕尺寸、窗口尺寸、字体都跟源码
    运行一致。必须在创建 Tk 之前调用。
    """
    if sys.platform != "win32":
        return False
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
        return True
    except Exception:                                # noqa: BLE001
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()        # 老系统回退
        return True
    except Exception:                                # noqa: BLE001
        return False


def run_cloud_selftest():
    """打包后点名自检「云盘上传」的三个可选依赖是否真的进了 exe。

    为什么不做进 ``--selftest``：``netease_cloud`` 是**可选依赖**，漏收时
    程序照常启动、转换照常能用，只有点「上传云盘」才报缺包 —— 这种静默降级
    在打包环节完全看不出来。所以必须有一条能单独点名验证的入口。

    除了 import，还会真跑一次 WAV→FLAC 与一次 weapi 加密：
    libsndfile 的 DLL 没被打进去时，import soundfile 是**成功**的，
    真正报错发生在第一次 ``sf.write``。
    """
    ok = [True]
    import tempfile

    def line(name, passed, detail=""):
        ok[0] = ok[0] and bool(passed)
        print("%-14s %-5s %s" % (name, "OK" if passed else "FAIL", detail))

    if ncm is None:
        line("netease_cloud", False, "模块没打进来，云盘按钮会提示缺依赖")
    else:
        line("netease_cloud", True, "已加载")

    try:
        import soundfile as sf
        line("soundfile", True, "%s / libsndfile %s"
             % (sf.__version__, sf.__libsndfile_version__))
    except Exception as exc:                         # noqa: BLE001
        line("soundfile", False, str(exc))

    try:
        from Crypto.Cipher import AES
        key = b"0123456789abcdef"
        blob = AES.new(key, AES.MODE_CBC, key).encrypt(b"x" * 16)
        line("pycryptodome", len(blob) == 16, "AES-128-CBC 输出 %d 字节" % len(blob))
    except Exception as exc:                         # noqa: BLE001
        line("pycryptodome", False, str(exc))

    try:
        n = len(_qr_matrix("https://music.163.com/login?codekey=probe"))
        line("segno", n >= 29, "矩阵 %d 阶（含 quiet zone）" % n)
    except Exception as exc:                         # noqa: BLE001
        line("segno", False, str(exc))

    # 浏览器登录：不启浏览器（自检要快），只验模块进没进来 + cookie 归并对不对。
    # 真正的起停取 cookie 由 vendor/check_cookie_cdp.py 覆盖。
    if nck is None:
        line("浏览器登录", False, "netease_cookie 没打进来，只剩粘贴 Cookie 一条路")
    else:
        name, exe = nck.find_browser()
        probe = nck.cookie_header_from_cdp(
            [{"name": "MUSIC_U", "value": "v", "domain": ".163.com", "path": "/"}])
        good = (probe == "MUSIC_U=v")
        line("浏览器登录", good,
             "%s%s / cookie归并 %s" % (name or "无浏览器", " 已就绪" if exe else "（未安装）",
                                    "OK" if good else "FAIL"))

    if ncm is not None:
        work = tempfile.mkdtemp(prefix="cloud_st_")
        try:
            import wave
            src = os.path.join(work, "probe.wav")
            with wave.open(src, "wb") as fh:
                fh.setnchannels(2)
                fh.setsampwidth(2)
                fh.setframerate(44100)
                fh.writeframes(b"\x00\x01" * 2 * 4410)
            done, flac, detail = ncm.wav_to_flac(src)
            size = ("%dB" % os.path.getsize(flac)) if done else ""
            line("WAV→FLAC", done, detail + ((" / " + size) if size else ""))
        except Exception as exc:                     # noqa: BLE001
            line("WAV→FLAC", False, str(exc))
        finally:
            import shutil
            shutil.rmtree(work, ignore_errors=True)

        try:
            p = ncm.weapi_params({"type": 1})
            line("weapi加密", len(p.get("encSecKey", "")) == 256 and bool(p.get("params")),
                 "encSecKey %d 位" % len(p.get("encSecKey", "")))
        except Exception as exc:                     # noqa: BLE001
            line("weapi加密", False, str(exc))

        # Cookie 解析：8821 风控下这是唯一能登进去的路，不能只在源码里靠得住
        try:
            got = ncm.clean_cookie_input("Cookie: MUSIC_U=probe; __csrf=x")
            line("Cookie解析", got == "MUSIC_U=probe; __csrf=x", got)
        except Exception as exc:                     # noqa: BLE001
            line("Cookie解析", False, str(exc))

    print("CLOUD SELFTEST " + ("PASS" if ok[0] else "FAIL"))
    return 0 if ok[0] else 1


def run_cdp_selftest():
    """端到端验证**打包后**的浏览器登录链路（真启无头 Chrome，一次性 profile）。

    为什么不能只靠 ``--cloud-selftest``：那里只证明模块 import 进来了、
    cookie 归并函数逻辑对。而这条链路真正依赖的是
    「能不能拉起 chrome.exe」「能不能连上调试端口」「能不能读到 cookie」，
    这三件事在 frozen 环境里出问题的概率远高于普通代码。

    约 3 秒，不碰用户真实 profile（用完即删）。
    """
    import shutil
    import tempfile
    import time

    def line(name, state, detail=""):
        print("%-14s %-5s %s" % (name, state, detail))

    if nck is None:
        line("浏览器登录", "FAIL", "netease_cookie 没打进来")
        return 1
    name, exe = nck.find_browser()
    if not exe:
        line("浏览器登录", "SKIP", "本机没装 Chrome/Edge，只能用粘贴 Cookie")
        return 0

    tmp = tempfile.mkdtemp(prefix="cdp_st_")
    bl = nck.BrowserLogin(silent=True, profile=tmp)
    ok = [True]

    def mark(passed, label, detail=""):
        ok[0] = ok[0] and bool(passed)
        line(label, "OK" if passed else "FAIL", detail)

    try:
        t0 = time.time()
        started = bl.start(timeout=20)
        mark(started, "启动浏览器",
             "%s %.1fs%s" % (name, time.time() - t0, "" if started else " " + bl.error))
        if not started:
            return 1

        mark(bl.fetch_cookie_header() == "", "未登录判定", "取到空串")

        ws = bl._connect()
        bl._mid += 1
        ws.call(bl._mid, "Network.setCookie",
                {"name": "MUSIC_U", "value": "selftest_value",
                 "domain": ".music.163.com", "path": "/"})
        h = bl.wait_cookie(timeout=8, interval=0.3)
        mark("MUSIC_U=selftest_value" in h, "取回 cookie", h[:40])
    except Exception as exc:                         # noqa: BLE001
        mark(False, "浏览器链路", "%s: %s" % (type(exc).__name__, exc))
    finally:
        proc = bl.proc
        bl.close()
        ended = proc is not None and proc.poll() is not None
        mark(ended, "关闭浏览器", "进程已退出")
        shutil.rmtree(tmp, ignore_errors=True)

    print("CDP SELFTEST " + ("PASS" if ok[0] else "FAIL"))
    return 0 if ok[0] else 1


def run_merge_selftest():
    """验证多轨合并**打包后**真能跑（v4.24）。

    为什么单独一条：``wavmerge`` 是纯 stdlib 模块，最容易「以为它一定打得
    进去」。这里在临时目录造 3 个 WAV 走一遍真合并，顺带验帧数与文件长度，
    不依赖任何外部样本，0.2 秒跑完。
    """
    print("merge selftest")
    ok = [True]

    def mark(good, label, detail=""):
        if not good:
            ok[0] = False
        print("%-16s %s   %s" % (label, "OK" if good else "FAIL", detail))

    if wavmerge is None:
        mark(False, "wavmerge", "模块没打进来，合并开关会静默失效")
        print("MERGE SELFTEST FAIL")
        return 1
    mark(True, "wavmerge", "已加载")

    import shutil
    import tempfile
    import wave

    tmp = tempfile.mkdtemp(prefix="msu1_merge_")
    try:
        srcs = []
        for i, frames in enumerate((1000, 1500, 700)):
            path = os.path.join(tmp, "s%d.wav" % i)
            with wave.open(path, "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(44100)
                w.writeframes(struct.pack("<h", (i + 1) * 100) * (frames * 2))
            srcs.append(path)
        out = wavmerge.merge_target(tmp, "probe")
        res = wavmerge.merge_wavs(srcs, out)
        mark(bool(res.get("ok")), "合并",
             res.get("error") or os.path.basename(out))
        if res.get("ok"):
            gap = int(round(44100 * wavmerge.DEFAULT_GAP_MS / 1000.0))
            want = 1000 + 1500 + 700 + gap * 2
            with wave.open(out, "rb") as w:
                got = w.getnframes()
            mark(got == want, "帧数", "%d / %d" % (got, want))
            mark(os.path.getsize(out) == 44 + want * 4, "文件长度",
                 "%d B" % os.path.getsize(out))
        bad = wavmerge.merge_wavs([srcs[0]], os.path.join(tmp, "x.wav"))
        mark(bad.get("ok") is False, "拒绝单文件", bad.get("error") or "")
    except Exception as exc:                         # noqa: BLE001
        mark(False, "合并流程", "%s: %s" % (type(exc).__name__, exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("MERGE SELFTEST " + ("PASS" if ok[0] else "FAIL"))
    return 0 if ok[0] else 1


def run_cd_selftest():
    """验证 CD 音轨解码**打包后**真能跑（v4.30）。

    为什么单独一条：``cd_audio`` 依赖 PyAV，而 PyAV 的 FFmpeg DLL 在
    ``av.libs/``（包**外**），``collect_all`` 收不到 —— 打漏了不会报错，
    只会在用户点「转 WAV」时静默失败。这里现造一个 FLAC 走完整条链路：
    ``av.open`` → 重采样 → 落 WAV，**不依赖任何外部样本**。
    """
    print("cd-audio selftest")
    ok = [True]

    def mark(good, label, detail=""):
        if not good:
            ok[0] = False
        print("%-16s %s   %s" % (label, "OK" if good else "FAIL", detail))

    if cd_audio is None:
        mark(False, "cd_audio", "模块没打进来，CD 音轨会全部跳过")
        print("CD SELFTEST FAIL")
        return 1
    mark(True, "cd_audio", "已加载")
    mark(cd_audio.available(), "PyAV", "av %s" % _av_version())

    import shutil
    import tempfile
    import wave

    tmp = tempfile.mkdtemp(prefix="msu1_cd_")
    try:
        src = os.path.join(tmp, "PACK_01 Probe.wav")
        _write_tone_wav(src, seconds=1.5)
        info = cd_audio.probe(src)
        mark(info["ok"], "probe", "%s %.2fs %dHz" % (info["codec"], info["secs"],
                                                    info["rate"]))
        res = cd_audio.convert_one(src, tmp, used=set(), start_idx=0)
        mark(res["ok"] == 1, "转换", str(res["errors"][:1]) if res["errors"] else
             os.path.basename(res["files"][0]) if res["files"] else "")
        if res.get("files"):
            with wave.open(res["files"][0], "rb") as w:
                fmt = (w.getnchannels(), w.getsampwidth() * 8, w.getframerate())
                secs = w.getnframes() / float(w.getframerate())
            mark(fmt == (2, 16, 44100), "格式", "%dch/%dbit/%dHz" % fmt)
            mark(abs(secs - 1.5) < 0.05, "时长", "%.3fs / 1.500s" % secs)
    except Exception as exc:                         # noqa: BLE001
        mark(False, "CD 流程", "%s: %s" % (type(exc).__name__, exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("CD SELFTEST " + ("PASS" if ok[0] else "FAIL"))
    return 0 if ok[0] else 1


def run_psf_selftest():
    """验证 PSF 桥接模块**打包后**真能用（v4.40）。

    为什么单独一条：``psf_bridge`` 在 ``msu1_gui`` 里是
    ``try: import psf_bridge`` —— 打漏了不会崩，只会在用户右键
    「用 foobar2000 转 WAV」时静默失效（提示"模块未就绪"）。所以必须
    在 exe 里点名验证：模块在、能写预设、能收集曲目。

    全离线：自己造一个假的 foobar2000 便携版目录，**不碰**用户真实的
    profile，也不需要真的装 foobar2000。
    """
    print("psf selftest")
    ok = [True]

    def mark(good, label, detail=""):
        if not good:
            ok[0] = False
        print("%-16s %s   %s" % (label, "OK" if good else "FAIL", detail))

    if psf_bridge is None:
        mark(False, "psf_bridge", "模块没打进来，PSF 转 WAV 会失效")
        print("PSF SELFTEST FAIL")
        return 1
    mark(True, "psf_bridge", "已加载")
    mark(len(psf_bridge.PSF_TRACK_EXTS) == 6, "扩展名",
         "、".join(psf_bridge.PSF_TRACK_EXTS))

    import shutil
    import tempfile
    base = os.path.join(tempfile.gettempdir(), "msu1_psf_selftest")
    shutil.rmtree(base, ignore_errors=True)
    try:
        fbroot = os.path.join(base, "foobar2000")
        os.makedirs(os.path.join(fbroot, "profile"), exist_ok=True)
        exe = os.path.join(fbroot, "foobar2000.exe")
        open(exe, "wb").write(b"MZ")
        open(os.path.join(fbroot, "portable_mode_enabled"), "w").write("")
        mark(psf_bridge.profile_dir(exe) == os.path.join(fbroot, "profile"),
             "profile", "便携版")

        pack = os.path.join(base, "pack")
        os.makedirs(pack, exist_ok=True)
        for i in range(3):
            open(os.path.join(pack, "T%02d.minipsf" % i), "wb").write(b"PSF\x01")
        open(os.path.join(pack, "G.psflib"), "wb").write(b"PSF\x01")
        mark(psf_bridge.count_tracks(pack) == 3, "曲目数", "3（.psflib 不算）")

        out = os.path.join(base, "WAV")
        r = psf_bridge.ensure_wav_preset(exe, out)
        mark(r["ok"] and r["created"], "写预设", os.path.basename(r["path"]))
        body = open(r["path"], encoding="utf-8").read()
        mark(psf_bridge.WAV_ENCODER_ID in body, "编码器", "WAV GUID")
        mark(os.path.normpath(out) in body, "输出目录", os.path.normpath(out))

        r2 = psf_bridge.ensure_wav_preset(exe, os.path.join(base, "X"))
        mark(r2["ok"] and not r2["created"], "不覆盖", "复用已有预设")

        g = psf_bridge.guidance(exe, pack, out, n_tracks=3)
        mark("Ctrl+A" in g and "Convert" in g, "指引", "含全选 + Convert")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("PSF SELFTEST PASS" if ok[0] else "PSF SELFTEST FAIL")
    return 0 if ok[0] else 1


def run_txtp_selftest():
    """验证 PS1 ``.txtp`` 原生解码**打包后**真能用（v4.41）。

    为什么单独一条：``psx_txtp`` 在 ``msu1_gui`` / ``dl_convert`` 里都是
    ``try: import psx_txtp`` —— 打漏了不会崩，只会让下载窗把 41 个 ``.txtp``
    全判成「未找到可转换的音源文件」（这正是用户报障的现象）。所以必须在
    exe 里点名验证：模块在、索引能解析、XA 与裸 PCM 两条路都能解出声。

    全离线：自己合成
      * 一个标准 CD-XA 扇区文件（12B sync + 4B header + 8B subheader + 2324B）；
      * 一个裸 PCM16LE 文件 + ``.DA.TXTH``；
      * 两个 ``.txtp`` 索引 + ``!tags.m3u``；
    再跑一遍 ``convert``，检查编号 01/02 连续、时长接近、输出非静音。
    """
    print("txtp selftest")
    ok = [True]

    def mark(good, label, detail=""):
        if not good:
            ok[0] = False
        print("%-16s %s   %s" % (label, "OK" if good else "FAIL", detail))

    if psx_txtp is None:
        mark(False, "psx_txtp", "模块没打进来，PS1 的 .txtp 包会转不出来")
        print("TXTP SELFTEST FAIL")
        return 1
    mark(True, "psx_txtp", "已加载")

    import shutil
    import struct
    import tempfile
    import wave

    base = os.path.join(tempfile.gettempdir(), "msu1_txtp_selftest")
    shutil.rmtree(base, ignore_errors=True)
    try:
        pack = os.path.join(base, "pack")
        snd = os.path.join(pack, "sound")
        os.makedirs(snd, exist_ok=True)

        # ---- ① 合成一个 XA 扇区文件：3 个音频扇区 ----
        # 扇区布局（**2352 字节，一个不能少** —— 我第一版写成 2348 就
        # 被自己的断言抓住了）：
        #   12B sync + 4B header + 8B subheader + 2324B data + 4B EDC
        # 判定要求（见 msu1_to_cd.xa_sector_groups）：
        #   ① sync == 00 FF*10 00   ② header[3]（偏移 15）= mode = 2
        #   ③ subheader 的 4 字节在 16..19 与 20..23 **重复**
        #   ④ submode 同时带 0x20(Form2) 与 0x04(Audio)
        #
        # coding 字节（sub[3]）写成 0x01 = 立体声 / 37800Hz / 4bit，
        # 这是规范值（v4.42 修正：早先误以为是「8bit 单声道」）。
        # 解码器不看这个字节，这条断言就是在守那个决定。
        nsec = 3
        sync = b"\x00" + b"\xff" * 10 + b"\x00"
        # 数据区结构：18 个 sound group × 128B（16B header + 112B data），
        # 只用到前 2304 字节，后面 20 字节空着。
        # 16B header 按真实布局 = [sf0..sf3][重复][sf4..sf7][重复]，
        # 这里 8 个子帧都填 0x01 -> filter=0、shift=1；
        # data 每字节 0x11（高低半字节都是 1）-> 解出来恒定 2048，
        # 既不静音也不会削顶。
        _grp = bytes([0x01] * 16) + bytes([0x11]) * 112
        _payload = _grp * 18 + bytes(2324 - 18 * 128)
        xa = os.path.join(snd, "T.xa")
        with open(xa, "wb") as fh:
            for _ in range(nsec):
                fh.write(sync)
                fh.write(b"\x00\x00\x01\x02")            # min/sec/frame/mode=2
                fh.write(bytes([1, 1, 0x64, 0x01]) * 2)  # subheader ×2（重复）
                fh.write(_payload)
                fh.write(b"\x00" * 4)                    # EDC
        mark(os.path.getsize(xa) == nsec * 2352, "XA 扇区",
             "%d B = %d×2352" % (os.path.getsize(xa), nsec))

        # ---- ② 合成裸 PCM：0.5 秒 44.1k 立体声 ----
        # 22050 帧 × 2 声道 = 44100 个样本，每个样本 +3000 / -3000 交替。
        da = os.path.join(snd, "T.DA")
        pcm = struct.pack("<44100h", *([3000, -3000] * 22050))
        with open(da, "wb") as fh:
            fh.write(pcm)
        with open(os.path.join(snd, ".DA.TXTH"), "w", encoding="utf-8") as fh:
            fh.write("codec = PCM16LE\nchannels = 2\nsample_rate = 44100\n"
                     "num_samples = %d\n" % (len(pcm) // 4))

        # ---- ③ 索引 + 曲名表 ----
        open(os.path.join(pack, "A.txtp"), "w", encoding="utf-8").write(
            "sound/T.xa #I 2153088\n")
        open(os.path.join(pack, "B.txtp"), "w", encoding="utf-8").write(
            "sound/T.DA\n")
        open(os.path.join(pack, "!tags.m3u"), "w", encoding="utf-8").write(
            "# %TITLE 测试曲A\nA.txtp\n# %TITLE 测试曲B\nB.txtp\n")

        mark(psx_txtp.is_txtp("x.TXTP") and not psx_txtp.is_txtp("x.xa"),
             "扩展名", ".txtp 大小写不敏感")
        mark(len(psx_txtp.list_txtp(pack)) == 2, "索引数", "2")

        info = psx_txtp.parse_txtp(os.path.join(pack, "A.txtp"))
        mark(info["ok"] and info["target"].lower().endswith("t.xa"), "解析",
             os.path.basename(info["target"]))
        # #I 是**循环点**不是起点偏移：解析出来只当诊断值，不参与时长
        mark(info["loop"] == 2153088, "#I 循环点", "只记录不参与时长")

        titles = psx_txtp.load_titles(pack)
        mark(titles.get("A.txtp") == "测试曲A", "曲名表",
             titles.get("A.txtp", "?"))

        pr = psx_txtp.probe_txtp(os.path.join(pack, "A.txtp"))
        # 3 扇区 × 2016 采样 / 37800 Hz = 0.16s
        mark(pr["ok"] and abs(pr["seconds"] - 0.16) < 0.02, "时长 XA",
             "%.3fs" % pr["seconds"])
        pr2 = psx_txtp.probe_txtp(os.path.join(pack, "B.txtp"))
        mark(pr2["ok"] and abs(pr2["seconds"] - 0.5) < 0.02, "时长 PCM",
             "%.3fs" % pr2["seconds"])

        # ---- ④ 真转一遍 ----
        out = os.path.join(base, "WAV")
        logs = []
        st = psx_txtp.convert(
            [os.path.join(pack, "A.txtp"), os.path.join(pack, "B.txtp")],
            out, on_log=logs.append)
        mark(st["ok"] == 2 and st["fail"] == 0, "转换", "ok=2 fail=0")
        names = sorted(os.listdir(out))
        mark(len(names) == 2, "产出数", "、".join(names))
        # 编号必须 01/02 连续 —— v4.41 修过一个 `+1` 双递增 bug（01 之后跳 03）
        mark(names and names[0].startswith("01_") and names[1].startswith("02_"),
             "编号连续", "01 → 02（不是 01 → 03）")
        mark(names and "测试曲A" in names[0], "曲名生效", names[0])

        # 两条都要**有声音**：全 0 说明解码路径没真正跑起来
        for fn in names:
            with wave.open(os.path.join(out, fn), "rb") as w:
                raw = w.readframes(w.getnframes())
                rate = w.getframerate()
                ch = w.getnchannels()
            peak = 0
            for i in range(0, len(raw) - 1, 2 * 97):    # 抽样即可
                v = struct.unpack_from("<h", raw, i)[0]
                peak = max(peak, abs(v))
            mark(rate == 44100 and ch == 2 and peak > 0, "出声 " + fn[:2],
                 "%dHz/%dch peak=%d" % (rate, ch, peak))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("TXTP SELFTEST PASS" if ok[0] else "TXTP SELFTEST FAIL")
    return 0 if ok[0] else 1


def _av_version():
    try:
        import av
        return av.__version__
    except Exception:                                # noqa: BLE001
        return "?"


def _write_tone_wav(path, seconds=1.5, rate=44100):
    """现造一个 440Hz 正弦 WAV（16bit 立体声），给 cd selftest 用。

    ⚠ 用 stdlib ``wave`` 而不是 soundfile：自检要验的是「PyAV 能不能开文件
      + 重采样 + 落盘」，不需要压缩编码；少一个依赖就少一个失败变量。
    """
    import array
    import math
    import wave
    n = int(seconds * rate)
    pcm = array.array("h")
    for i in range(n):
        v = int(11000 * math.sin(2 * math.pi * 440 * i / rate))
        pcm.append(v)
        pcm.append(v)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def main():
    set_dpi_awareness()

    # 隐藏 worker 模式: 由 core.load_spc_paths 以子进程调用本 exe 执行 libgme 解码。
    # 必须在创建 Tk 窗口之前分发, 否则会弹窗且永远不产出结果文件。
    core.maybe_run_spc_worker()

    # 窗口版 exe 没有控制台, sys.stdout 是 None; 命令行模式的输出落到 convert.log
    if sys.stdout is None:
        try:
            sys.stdout = sys.stderr = open(
                os.path.join(base_dir(), "convert.log"), "w", encoding="utf-8")
        except OSError:
            pass

    # 输出被**重定向到文件**时，Python 按系统区域编码（中文 Windows 是 cp936/GBK）
    # 写日志，遇到 ⚠ / ✅ 直接抛 UnicodeEncodeError —— 一次成功的转换会变成
    # rc=1（v4.36 实测：exe --convert 产出了 WAV，随后崩在「⚠ 有削波」这行）。
    # 只对非终端输出改成 UTF-8 + replace；控制台保持原样，免得改坏现有显示。
    for _s in (sys.stdout, sys.stderr):
        try:
            if _s is not None and not _s.isatty():
                _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    hook_crash_log()

    argv = sys.argv[1:]
    if argv and argv[0] == "--convert":
        sys.exit(run_spc_wav(argv))
    if "--cloud-selftest" in argv:
        # 点名验证云盘上传的三个可选依赖（见 run_cloud_selftest 的注释）
        sys.exit(run_cloud_selftest())
    if "--cdp-selftest" in argv:
        # 端到端验证浏览器登录（真启无头 Chrome），见 run_cdp_selftest
        sys.exit(run_cdp_selftest())
    if "--merge-selftest" in argv:
        # 验证多轨合并模块真的打进了 exe（见 run_merge_selftest）
        sys.exit(run_merge_selftest())
    if "--cd-selftest" in argv:
        # 验证 CD 音轨解码（PyAV + av.libs）真的打进了 exe（见 run_cd_selftest）
        sys.exit(run_cd_selftest())
    if "--psf-selftest" in argv:
        # 验证 PSF 桥接模块真的打进了 exe（见 run_psf_selftest）
        sys.exit(run_psf_selftest())
    if "--txtp-selftest" in argv:
        # 验证 PS1 .txtp 原生解码真的打进了 exe（见 run_txtp_selftest）
        sys.exit(run_txtp_selftest())
    if "--selftest" in argv:
        rest = [a for a in argv if a != "--selftest"]
        sys.exit(run_selftest(rest))

    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:                                # noqa: BLE001
        pass
    App(root)
    root.mainloop()
    release_session_lock()


if __name__ == "__main__":
    main()
