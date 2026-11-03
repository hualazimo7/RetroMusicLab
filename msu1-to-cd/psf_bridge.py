# -*- coding: utf-8 -*-
"""PSF（PS1/PS2 芯片音源）→ WAV 的「半自动」桥接。

⚠ **本模块只剩 tkinter 版（`msu1_gui.py`）在用。Web 版不用它。**
v5.1-web-psf 起 Web 版是**原生解码**：`psf_native.py` → 同级的 `psf2wav.exe`
（psflib + Highly Experimental，用 zig 编的）+ 一份 BIOS。详见 README ⚠33。

下面「本机没有任何 C 编译器」那句**是错的** —— `pip install ziglang` 就有了，
整个原生方案就是靠这一条翻的盘。留着这段是因为它记录了当初为什么走桥接。

为什么只能半自动
----------------
PSF 文件里存的不是波形，而是**一份 PS1 内存快照 + CPU/SPU 寄存器**。
要出声必须模拟 R3000A CPU 和 SPU 的 24 个声道 —— 这不是"写个解析器"能解决的：

* 纯 Python 解释 MIPS 大约比实时慢 30 倍（3 分钟的曲子要 1.5 小时），不可用；
* ~~本机没有任何 C 编译器（gcc / cl / clang / cmake 全无），没法把开源的
  `psflib` / `highly_experimental` 编成独立 exe；~~（已被 zig 推翻，见上）
* `mitsuman/psf2wav` 只有 Linux 源码、无 Windows 预编译产物。

所以本模块**不自己解码**，而是驱动用户本机已装的 **foobar2000 + foo_psf**：
把整包载入它的播放列表，并预先写好一个 WAV 转换预设，用户在 foobar2000 里
「全选 → 右键 Convert → 选 MSU1-WAV → 开始」即可批量出 WAV。

为什么不做全自动
----------------
实测 foobar2000 **v2.26.1 的转换器没有任何命令行入口**（六种写法全部无效）：

    /convert /dest:… /codec:WAV        → 无产物
    /convert /dest:…                   → 无产物
    /convert /dest:… /preset:"[default]" → 无产物
    /context_command:"Convert"         → 只打开窗口
    /context_command:"Convert to same folder" → 只打开窗口
    /context_command:"Quick Convert"   → 无产物
    /playlist_command:"Quick Convert"  → 无产物

`foo_converter.dll` 里确实有 `Convert` / `Quick Convert` 两个命令名，
但只能从 GUI 触发。硬走窗口消息/按键模拟太脆（版本一更新、界面一本地化就废），
宁可把话说清楚，也不做一个"看着能点、实际经常不灵"的按钮。

许可
----
foobar2000 的许可禁止再分发其二进制，所以这里只做「检测 + 调用」，
**绝不**把便携版打进我们的 exe。预设置只往它自己的 profile 里新增一个文件，
不覆盖用户已有的任何预设。
"""

import os
import subprocess

__all__ = [
    "PSF_TRACK_EXTS", "PSF_LIB_EXTS", "WAV_ENCODER_ID", "PRESET_NAME",
    "find_foobar", "profile_dir", "preset_dir", "ensure_wav_preset",
    "collect_tracks", "count_tracks", "open_for_convert", "guidance",
]

#: PSF 曲目扩展名（一个包里几十首，每首一个文件）
PSF_TRACK_EXTS = (".psf", ".psf1", ".minipsf", ".minipsf1", ".psf2", ".minipsf2")
#: PSF 库文件（曲目引用它，本身不是曲目）
PSF_LIB_EXTS = (".psflib", ".psf2lib")

#: foobar2000 内置 WAV 编码器的 encodingID。
#: 来源：`foo_converter.dll` 里唯一硬编码的编码器 GUID（本机 v2.26.1 实测，
#: 二进制中该 GUID 只出现 1 次），也是 foobar2000 默认转换预设用的那个。
WAV_ENCODER_ID = "1E0FBBB1-A82E-4752-B408-6BCF3F509802"
#: 我们写的预设名（= 预设文件名去掉 .ini）。刻意不用 [default]，
#: 免得和用户自己的默认预设撞名。
PRESET_NAME = "MSU1-WAV"


# ---------------------------------------------------------------------------
# 找 foobar2000 / 找它的 profile
# ---------------------------------------------------------------------------

def find_foobar():
    """找可用的 ``foobar2000.exe``；找不到返回 ``""``。

    便携版很常见地放在**盘符根目录**（本机就是 ``D:\\foobar2000``），
    所以除了 Program Files 也要扫各盘根目录下的 ``foobar2000``。
    """
    cands = []
    for drive in ("C:", "D:", "E:", "F:", "G:"):
        root = drive + os.sep
        for sub in ("foobar2000",
                    os.path.join("Program Files", "foobar2000"),
                    os.path.join("Program Files (x86)", "foobar2000"),
                    os.path.join("Program Files", "foobar2000 v2"),
                    os.path.join("tools", "foobar2000"),
                    os.path.join("软件", "foobar2000")):
            cands.append(os.path.join(root, sub, "foobar2000.exe"))
    for env in ("APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env, "")
        if not base:
            continue
        for sub in ("foobar2000", os.path.join("foobar2000", "foobar2000")):
            cands.append(os.path.join(base, sub, "foobar2000.exe"))
    for c in cands:
        try:
            if c and os.path.isfile(c):
                return c
        except OSError:
            continue
    return ""


def profile_dir(exe):
    """foobar2000 的 profile 目录；判不出返回 ``""``。

    便携版的标志是根目录下有 ``portable_mode_enabled`` 文件（本机就有），
    此时 profile 在 ``<root>/profile``；否则在 ``%APPDATA%`` 下
    （v2 用 ``foobar2000-v2``，老版用 ``foobar2000``）。
    """
    if not exe:
        return ""
    root = os.path.dirname(os.path.abspath(exe))
    try:
        if os.path.isfile(os.path.join(root, "portable_mode_enabled")):
            p = os.path.join(root, "profile")
            return p if os.path.isdir(p) else ""
    except OSError:
        pass
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return ""
    for name in ("foobar2000-v2", "foobar2000"):
        p = os.path.join(appdata, name)
        if os.path.isdir(p):
            return p
    return ""


def preset_dir(exe):
    """转换预设目录 ``<profile>/converter presets``；不存在则返回其父目录下
    的预期路径（由 :func:`ensure_wav_preset` 负责创建）。"""
    prof = profile_dir(exe)
    if not prof:
        return ""
    return os.path.join(prof, "converter presets")


# ---------------------------------------------------------------------------
# 写 WAV 转换预设
# ---------------------------------------------------------------------------

def _preset_body(out_dir):
    """预设 ini 的正文。

    字段与 foobar2000 自己写的 ``[default].ini`` 一致（本机实测格式）。
    取值上的几个决定：

    * ``fileNamePattern=%filename%`` —— 不用 ``%title%``：joshw 的 minipsf
      很多没有 title 标签，用 ``%title%`` 会生成一堆同名/空名文件；
      ``%filename%`` 一定非空且唯一。
    * ``ifExists=overwrite`` —— 输出目录是我们专建的，重跑应该覆盖旧产物，
      否则会每跑一次多出一批 ``(1)`` 副本。
    * ``transferPictures=0`` —— PSF 没有封面，省得它去找。
    * ``transferTags=1`` —— 保留曲名/游戏名，便于以后按标签整理。
    """
    return "\n".join([
        "encodingID=" + WAV_ENCODER_ID,
        "fileNamePattern=%filename%",
        # 统一成 Windows 反斜杠：传进来的可能是 "D:/x/y"，混着写会让
        # foobar2000 的预设读起来别扭（实测日志里出现过 "D:/a\b\WAV"）。
        "outputFolder=" + os.path.normpath(str(out_dir)),
        "ifExists=overwrite",
        "decodePostProcess=1",
        "transferPictures=0",
        "transferReplayGain=0",
        "transferTags=1",
        "keepPartial=0",
        "showOutput=1",
        "",
    ])


def ensure_wav_preset(exe, out_dir):
    """确保 foobar2000 里有名为 ``MSU1-WAV`` 的 WAV 转换预设。

    返回 ``dict``：``{ok, created, name, path, error}``。

    **绝不覆盖已存在的同名预设** —— 用户可能自己改过（比如改成 FLAC）。
    已存在时 ``created=False`` 并原样复用，日志里会说清楚"用的是已有预设"。
    """
    res = {"ok": False, "created": False, "name": PRESET_NAME,
           "path": "", "error": ""}
    if not exe:
        res["error"] = "没有可用的 foobar2000"
        return res
    pdir = preset_dir(exe)
    if not pdir:
        res["error"] = ("找不到 foobar2000 的 profile 目录"
                        "（便携版应有 portable_mode_enabled 文件）")
        return res
    path = os.path.join(pdir, PRESET_NAME + ".ini")
    res["path"] = path
    if os.path.isfile(path):
        res["ok"] = True
        return res                      # 已有 → 复用，不动它
    try:
        os.makedirs(pdir, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write(_preset_body(out_dir))
    except OSError as e:
        res["error"] = "写预设失败：%s" % e
        return res
    res["ok"] = True
    res["created"] = True
    return res


# ---------------------------------------------------------------------------
# 收集曲目 / 交给 foobar2000
# ---------------------------------------------------------------------------

def collect_tracks(folder):
    """列出文件夹里（只扫一层）的 PSF 曲目，按文件名排序。

    只扫一层是刻意的：joshw 的 PSF 包结构是
    ``<包名>/<曲目>.minipsf`` + ``<包名>/<库>.psflib``，再深就是别的包了。
    """
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for n in sorted(names):
        if os.path.splitext(n)[1].lower() in PSF_TRACK_EXTS:
            p = os.path.join(folder, n)
            if os.path.isfile(p):
                out.append(p)
    return out


def count_tracks(folder):
    """曲目数（不含 .psflib 库文件）。"""
    return len(collect_tracks(folder))


def open_for_convert(exe, folder):
    """把**整个文件夹**交给 foobar2000（它递归扫描、全部入播放列表）。

    返回 ``(ok, error)``。传文件夹而不是单个文件：一个包几十首，
    只传一个的话用户还得自己再找。
    """
    if not exe:
        return False, "没有可用的 foobar2000"
    if not os.path.isdir(folder):
        return False, "路径不是文件夹：%s" % folder
    try:
        subprocess.Popen([exe, folder])
    except OSError as e:
        return False, "启动 foobar2000 失败：%s" % e
    return True, ""


def guidance(foobar_exe, folder, out_dir, preset_name=PRESET_NAME,
             n_tracks=0):
    """给用户看的操作指引（多行文本）。

    刻意写成"照着点"的步骤，而不是"请自行转换"——用户要的是结果。
    """
    n = n_tracks or count_tracks(folder)
    lines = [
        "PSF 需要模拟器才能出声，本工具不内置。已为你准备好 foobar2000：",
        "",
        "1. foobar2000 已打开并载入整个包（%d 首）" % n,
        "2. 在它的播放列表里按 Ctrl+A 全选",
        "3. 右键 → Convert → 选预设「%s」→ 开始" % preset_name,
        "4. 输出会写到：%s" % out_dir,
        "",
        "预设「%s」已设为 WAV / 44.1kHz / 16bit / 立体声，"
        "文件名用原文件名。" % preset_name,
        "",
        "⚠ 预设只在你第一次用时写入；以后会一直复用。"
        "想改成 FLAC 之类，在 Convert 对话框里改完保存成同名预设即可。",
        "",
        "ℹ 万一输出不是 WAV（预设里的编码器 ID 是从 foo_converter 里取的，"
        "理论上就是内置 WAV 编码器）：在 Convert 对话框把 Output format 选成 "
        "WAV，再 Save preset 覆盖「%s」即可，之后不用再改。" % preset_name,
    ]
    if not foobar_exe:
        lines = ["没有找到 foobar2000。",
                 "PSF 需要它（+ foo_psf 组件）才能转 WAV。",
                 "装好便携版后放在任意盘根目录的 foobar2000 文件夹下即可被识别。"]
    return "\n".join(lines)
