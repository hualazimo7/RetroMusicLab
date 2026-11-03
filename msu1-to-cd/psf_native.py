# -*- coding: utf-8 -*-
"""PSF / minipsf（PS1 芯片音源）→ WAV：**原生解码，不经过 tkinter / foobar2000**。

和 ``psx_txtp`` 的分工
----------------------
``.txtp`` 包里的音轨是 CD-XA 或裸 PCM，纯 Python 就能解（见 ``psx_txtp``）。
``.psf`` 完全不是这回事：它存的是**一份 PS1 内存快照 + 一段可执行的
PS-X EXE**，要出声必须真的模拟 R3000A CPU 和 SPU 的 24 个声道。

所以这里不自己算，而是调 ``psf2wav.exe``（源码在 ``tools/psf2wav/``，
由 psflib + Highly Experimental + zlib 编成）。本模块只负责三件事：
**找解码器、找 BIOS、把 WAV 读回 PCM 交给主流水线**。

BIOS：绕不过去的一道门槛
------------------------
Highly Experimental 的 PS1 核心**必须**有 BIOS，而且不是随便一份 PS1 BIOS
就行 —— 它要在 BIOS 偏移 0x80 处读到 "Highly Experimental" 那段 banner。
标准做法是用 HE 自带的 ``mkhebios`` 把一份 **4MB 的 PS2 BIOS** 压成
**512KB 的 HE BIOS**，这一步会顺带打进 A0/B0/C0 三张调用表桩、异常向量上的
中断派发、事件系统和定时器 2 的 VSync 中断。

实测对照（同一首 SYS_ME_00.psf）：

===============  ==============  ===========  ==========
方案             SPU 寄存器写入  key-on 非零  输出
===============  ==============  ===========  ==========
只有手写 HLE 桩  644 次          0 次         全 0 静音
真 BIOS + mkhebios  4832 次      32 次        peak 16691
===============  ==============  ===========  ==========

只靠 HLE 桩时，驱动初始化完就跑到 ``0x80100414`` 的 ``beq $0,$0,-1``
（停机自旋）不动了 —— 它在等 VSync 中断回调。所以**没有 BIOS 就不该硬撑**，
直接报错、把话说清楚。

**本模块不分发任何 BIOS**。运行时用用户自己机器上的文件，查找顺序见
:func:`find_bios`；找到之后用 :func:`set_bios` 记住路径即可。
"""

import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

import winspawn                       # 起子进程不弹黑框，见 winspawn.py

__all__ = [
    "PSF1_EXTS", "PSF2_EXTS", "PSF_ALL_EXTS", "PSF_LIB_EXTS",
    "DECODER_NAME", "BIOS_ENV", "BIOS_SIZES", "BIOS_HOME",
    "app_dir", "find_decoder", "decoder_ready",
    "find_bios", "set_bios", "check_bios", "bios_hint", "conf_path",
    "bios_home", "adopt", "autodiscover",
    "decode_pcm", "available", "status", "length_tag",
]

#: PS1 的 PSF（本模块支持）
PSF1_EXTS = (".psf", ".psf1", ".minipsf", ".minipsf1")
#: PS2 的 PSF2（v5.4 起支持：psf2wav 已实现 version 2 路径 —— psf2fs VFS +
#: psx_execute 按 count 填满，采样率 48000；与上游 plugin.cc 逐样本一致，
#: 见 vendor/_check_psf2_ref.py）。同样需要 BIOS（HE 的 PS2 核心）。
PSF2_EXTS = (".psf2", ".minipsf2")
PSF_ALL_EXTS = PSF1_EXTS + PSF2_EXTS
#: 曲目引用的库文件，本身不是曲目
PSF_LIB_EXTS = (".psflib", ".psf2lib")

DECODER_NAME = "psf2wav.exe"
#: 指定 BIOS 的环境变量
BIOS_ENV = "MSU1_PSF_BIOS"
#: 记 BIOS 路径的配置文件名（放在程序目录）
BIOS_CONF = "psf_bios.txt"
#: 程序目录下的 BIOS 存放夹 —— **「集成」的落点**
#:
#: ``psf_bios.txt`` 只是一行路径，源文件一挪就失效；这个目录里放的是
#: **副本**，跟着 exe 走。换版本、清 ``%APPDATA%``、把源文件删了，
#: 只要 ``bios/`` 还在就照样能用。
BIOS_HOME = "bios"
#: 合法的 BIOS 尺寸：4MB 的 PS2 BIOS / 512KB 的 HE BIOS
BIOS_SIZES = (0x400000, 0x80000)

#: Windows 的 ``STATUS_DLL_INIT_FAILED``（0xc0000142）。
#:
#: ``psf2wav.exe`` 是**控制台**程序，而本程序是 ``--windows-console-mode=disable``
#: 的窗口程序。从窗口程序里起控制台程序，Windows 每次都要临时新分配一个控制台：
#: 用户看到的就是**每转一首闪一次黑框**，偶尔还会因为拿不到控制台资源直接
#: 启动失败（0xc0000142）。静默参数统一在 :mod:`winspawn` 里，见那个文件的说明。
_DLL_INIT_FAILED = winspawn.DLL_INIT_FAILED
#: 0xc0000142 是**偶发**的，重试几次基本都能过
_SPAWN_RETRY = 3

#: 起子进程用的「绝不弹窗」参数（保持旧名字，调用点与门禁都在用）
_silent_kw = winspawn.silent_kw
#: 退出码转无符号 32 位
_rc_u32 = winspawn.rc_u32

#: 解码器 stdout 里的结果行
_RE_SEC = re.compile(r"seconds=([0-9.]+)\s+samples=(\d+)\s+peak=(-?\d+)")
_RE_BIOS_OK = re.compile(r"BIOS_OK\s+(.+?)\s+size=(\d+)")
#: PSF 标签块里的 ``length=分:秒.小数``
_RE_LEN = re.compile(r"^length=(\d+):([0-9.]+)\s*$", re.M)


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def app_dir():
    """程序目录 —— ``psf_bios.txt`` 就写在这儿。

    打包后**不能**只用 ``sys.executable`` 的目录：Nuitka onefile 会把它指到
    ``%TEMP%\\onefile_xxx``，配置写在那儿一退出就没了。所以打包态按
    「先挑不在临时目录里的那个」来定，源码态直接用本文件目录。
    """
    if not (getattr(sys, "frozen", False) or "__compiled__" in globals()):
        return os.path.dirname(os.path.abspath(__file__))

    tmp = os.path.abspath(tempfile.gettempdir()).lower()
    cands = []
    for p in (sys.argv[0] if sys.argv else "", sys.executable, __file__):
        if not p:
            continue
        d = os.path.dirname(os.path.abspath(p))
        if d not in cands:
            cands.append(d)
    for d in cands:
        if not d.lower().startswith(tmp):
            return d
    return cands[0] if cands else os.getcwd()


def _search_dirs():
    """可能放着 ``psf2wav.exe`` 的目录，按优先级。"""
    cands = []

    # Nuitka onefile 会把数据文件解到临时目录，__file__ 指那儿
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(here)

    # PyInstaller onefile
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(str(meipass))

    cands.append(app_dir())

    out = []
    for d in cands:
        if d and d not in out:
            out.append(d)
    return out


def find_decoder():
    """返回可用的 ``psf2wav.exe`` 完整路径；找不到返回 ``""``。"""
    for d in _search_dirs():
        p = os.path.join(d, DECODER_NAME)
        if os.path.isfile(p):
            return p
    # 开发机上可能放进 PATH
    p = shutil.which("psf2wav") or shutil.which("psf2wav.exe")
    return p or ""


def decoder_ready():
    """解码器在不在。返回 ``(bool, 说明)``。"""
    p = find_decoder()
    if p:
        return True, p
    return False, ("找不到 %s。它应该跟主程序放在一起；"
                   "源码运行时可以用 tools/psf2wav/build.py 重新编译。"
                   % DECODER_NAME)


# ---------------------------------------------------------------------------
# BIOS
# ---------------------------------------------------------------------------

def _size_ok(n):
    return n in BIOS_SIZES


def check_bios(path):
    """验证一份 BIOS 能不能用。返回 ``{ok, size, kind, error}``。

    只做尺寸判断 + 交给解码器 ``--check-bios`` 真跑一遍转换，不猜。
    """
    res = {"ok": False, "size": 0, "kind": "", "error": ""}
    p = str(path or "").strip().strip('"')
    if not p:
        res["error"] = "没有给 BIOS 路径"
        return res
    if not os.path.isfile(p):
        res["error"] = "文件不存在：%s" % p
        return res
    try:
        n = os.path.getsize(p)
    except OSError as exc:
        res["error"] = "读不到文件大小：%s" % exc
        return res
    res["size"] = n
    if not _size_ok(n):
        res["error"] = ("大小 %d 字节不对，需要 4MB 的 PS2 BIOS"
                        "（如 scph39001.bin）或 512KB 的 HE BIOS" % n)
        return res
    res["kind"] = "PS2 BIOS（4MB，会转成 HE BIOS）" if n == 0x400000 else "HE BIOS（512KB）"

    exe = find_decoder()
    if not exe:
        res["error"] = "找不到 %s，无法验证" % DECODER_NAME
        return res
    try:
        r = subprocess.run([exe, "--check-bios", p],
                           capture_output=True, timeout=120,
                           **_silent_kw())
    except (OSError, subprocess.SubprocessError) as exc:
        res["error"] = "验证时出错：%s" % exc
        return res
    if r.returncode == 0 and _RE_BIOS_OK.search(_text(r.stdout)):
        res["ok"] = True
        return res
    hint = _exit_hint(r.returncode)
    if hint:
        res["error"] = hint
        return res
    res["error"] = (_last_err(r.stderr) or
                    "解码器拒绝了这份 BIOS（退出码 %d）" % r.returncode)
    return res


#: 解码器**自己崩了**时的退出码 → 人话。
#:
#: ⚠ 这张表是踩出来的（v5.2.2）。原来不管什么退出码都报
#:   「解码器拒绝了这份 BIOS」，用户看到就去怀疑 BIOS 文件坏了，
#:   重新指定、换文件、查大小 —— 全都白费。实际上：
#:
#:   退出码 3221225501 = 0xC000001D = STATUS_ILLEGAL_INSTRUCTION
#:
#:   意思是 **CPU 不认识解码器里的指令**，跟 BIOS 一个字节的关系都没有。
#:   （成因：psf2wav.exe 用 ``zig cc`` 编译，而 zig 默认按编译机的 CPU 出码，
#:     带 AVX-512 的机器上编出来的包，换到老 CPU 上第一条指令就挂。）
#:
#:   把「解码器跑不起来」和「BIOS 不合格」混成一句话，代价是一次无谓的
#:   排障往返。所以这里必须先判退出码，再谈 BIOS。
_EXIT_HINTS = {
    -1073741795: ("解码器无法在这台机器上运行：CPU 指令集不兼容"
                  "（0xC000001D）。这不是 BIOS 的问题 —— 换用 baseline 版"
                  "解码器即可（重新编译 psf2wav.exe）"),
    -1073741819: "解码器崩溃了：访问违例（0xC0000005），与 BIOS 无关",
    -1073741515: "解码器缺依赖 DLL（0xC0000135），与 BIOS 无关",
    -1073741701: "解码器文件损坏或位数不匹配（0xC000007B）",
    -1073741571: "解码器栈溢出（0xC00000FD）",
}


def _exit_hint(rc):
    """退出码是 NTSTATUS（负数）时给出人话，否则返回空串。"""
    return _EXIT_HINTS.get(rc, "")


def bios_home():
    """程序目录下那个放 BIOS 副本的夹子（``<exe 所在目录>\\bios``）。"""
    return os.path.join(app_dir(), BIOS_HOME)


def adopt(path):
    """把一份 BIOS **复制**进程序目录的 ``bios/`` —— 这就是「集成进软件」。

    用户的原话是「本机有 PS2 BIOS，能不能集成进软件里，不要再次指定」。
    ``psf_bios.txt`` 只记一行路径，源文件一挪（桌面那个临时文件夹被清理、
    换个盘、重装系统）就得重新指定；复制一份到程序旁边才是真的「集成」：
    以后 :func:`find_bios` 扫 ``bios/`` 就能直接命中。

    返回 ``(落点路径, 错误说明)``。失败**不致命** —— 程序可能装在
    ``Program Files`` 或只读介质上，那就退回只写 ``psf_bios.txt``。
    """
    src = os.path.abspath(str(path or "").strip().strip('"'))
    home = os.path.abspath(bios_home())
    if os.path.dirname(src).lower() == home.lower():
        return src, ""                      # 本来就在里头，不用再复制一遍
    dst = os.path.join(home, os.path.basename(src))
    try:
        os.makedirs(home, exist_ok=True)
        shutil.copy2(src, dst)              # copy2 保时间戳，跟源文件对得上
    except OSError as exc:
        return "", "复制到 %s 失败：%s" % (home, exc)
    return dst, ""


def _is_bundled(path):
    """这份 BIOS 是不是已经固化在程序目录的 ``bios/`` 里。"""
    try:
        return (os.path.dirname(os.path.abspath(str(path))).lower()
                == os.path.abspath(bios_home()).lower())
    except (TypeError, ValueError):
        return False


def _conf_paths():
    """``psf_bios.txt`` 的候选位置（前面的优先）。

    第一处是程序目录（绿色版：配置跟着程序走）；程序目录不可写时
    （装进 Program Files、或放在只读介质上）退到 ``%APPDATA%``。
    """
    out = [os.path.join(app_dir(), BIOS_CONF)]
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    alt = os.path.join(base, "RetroMusic Lab", BIOS_CONF)
    if alt not in out:
        out.append(alt)
    return out


def _conf_path():
    """当前实际在用（或将要写）的那一份。"""
    for p in _conf_paths():
        if os.path.isfile(p):
            return p
    return _conf_paths()[0]


def conf_path():
    """配置文件（``psf_bios.txt``）的完整路径 —— 界面要显示给用户看。"""
    return _conf_path()


def find_bios():
    """找一份能用的 BIOS，返回 ``(路径, 说明)``；找不到返回 ``("", 原因)``。

    查找顺序（前面的赢）：

    1. 环境变量 ``MSU1_PSF_BIOS``
    2. ``psf_bios.txt``（一行，写完整路径）—— :func:`set_bios` 写它。
       程序目录一份、``%APPDATA%\\RetroMusic Lab`` 一份，都能读
    3. 程序目录下的 ``bios/`` 里任何尺寸合法的 ``.bin``
    4. 常见模拟器目录（RetroArch / PCSX2 / ePSXe）—— 只扫一层，不做全盘搜索
    """
    tried = []

    env = os.environ.get(BIOS_ENV, "").strip().strip('"')
    if env:
        c = check_bios(env)
        if c["ok"]:
            return env, "环境变量 %s" % BIOS_ENV
        tried.append("%s → %s" % (BIOS_ENV, c["error"] or "不可用"))

    for conf in _conf_paths():
        if not os.path.isfile(conf):
            continue
        try:
            with open(conf, "r", encoding="utf-8") as f:
                saved = f.readline().strip()
        except OSError:
            saved = ""
        if not saved:
            continue
        c = check_bios(saved)
        if c["ok"]:
            # 配置里写的就是 bios/ 里那份副本时，说「已集成」比说
            # 「psf_bios.txt」有用 —— 后者对用户没有信息量。
            return saved, ("已集成（程序目录 bios/）" if _is_bundled(saved)
                           else BIOS_CONF)
        tried.append("%s → %s" % (BIOS_CONF, c["error"] or "不可用"))

    for d in _bios_dirs():
        hit = _scan_bios_dir(d)
        if hit:
            if _is_bundled(hit):
                return hit, "已集成（程序目录 bios/）"
            return hit, "自动发现（%s）" % d
    if _bios_dirs():
        tried.append("已扫过这些目录但没有合法的 BIOS：%s"
                     % "；".join(_bios_dirs()))

    return "", ("没找到可用的 PS1/PS2 BIOS。" +
                ("（%s）" % "；".join(tried) if tried else ""))


def autodiscover():
    """全自动找一次 BIOS，找到就**固化**进程序目录。

    返回 ``{ok, path, kind, note, why, bundled}``。

    界面上的「自动查找」按钮走这里；启动时如果还没 BIOS，后台也跑一遍
    （见 ``web_ui/server.py`` 的 ``_psf_status``）。找不到不会抛异常，
    只是 ``ok=False`` + ``why`` 里说明扫过哪些目录。
    """
    hit, note = find_bios()
    if hit and not _is_bundled(hit):
        # 已经能用，但还没固化 —— 顺手复制一份进 bios/，免得源文件被清理
        r = set_bios(hit)
        if r.get("ok"):
            return {"ok": True, "path": r["path"], "kind": r["kind"],
                    "note": ("已集成（程序目录 bios/）" if r.get("bundled")
                             else note),
                    "why": "", "bundled": bool(r.get("bundled"))}
    if hit:
        return {"ok": True, "path": hit, "kind": check_bios(hit)["kind"],
                "note": note, "why": "", "bundled": _is_bundled(hit)}
    return {"ok": False, "path": "", "kind": "", "note": "",
            "why": note, "bundled": False}


def _bios_dirs():
    """可能放着 BIOS 的目录。**只扫一层**，绝不递归全盘。

    第一项永远是程序目录的 ``bios/`` —— 那是 :func:`adopt` 固化副本的地方，
    也是「集成进软件」之后真正起作用的那一份。
    """
    dirs = [bios_home()]

    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    local = (os.environ.get("LOCALAPPDATA")
             or os.path.join(home, "AppData", "Local"))
    doc = os.path.join(home, "Documents")

    bases = [
        # RetroArch（各版本目录名不一样，常见三种都列上）
        os.path.join(appdata, "RetroArch", "system"),
        os.path.join(appdata, "RetroArch-Win64", "system"),
        os.path.join(doc, "RetroArch", "system"),
        # PCSX2：新版把配置丢在 Documents，便携版在安装目录，老版在 APPDATA
        os.path.join(doc, "PCSX2", "bios"),
        os.path.join(appdata, "PCSX2", "bios"),
        os.path.join(local, "PCSX2", "bios"),
        # DuckStation / ePSXe / mednafen
        os.path.join(doc, "DuckStation", "bios"),
        os.path.join(appdata, "DuckStation", "bios"),
        os.path.join(doc, "ePSXe", "bios"),
        os.path.join(home, "ePSXe", "bios"),
        # 用户最常「随手一放」的几个地方（这次实测就是这么来的：
        # 桌面上一个 05_待分类\\新建文件夹\\…\\bios\\SCPH30004R.bin）
        os.path.join(home, "Desktop", "bios"),
        os.path.join(home, "OneDrive", "Desktop", "bios"),
        os.path.join(home, "Downloads", "bios"),
        os.path.join(home, "Downloads", "PS2", "bios"),
        os.path.join(home, "PS2", "bios"),
    ]
    for drive in ("C:", "D:", "E:", "F:", "G:"):
        root = drive + os.sep
        for sub in ("RetroArch\\system", "PCSX2\\bios", "ePSXe\\bios",
                    "bios", "PS2\\bios",
                    "Emulators\\PCSX2\\bios", "Emulators\\RetroArch\\system",
                    "Emulators\\PS2\\bios", "Games\\PCSX2\\bios"):
            bases.append(os.path.join(root, sub))
    for b in bases:
        if b not in dirs:
            dirs.append(b)
    return [d for d in dirs if os.path.isdir(d)]


def _scan_bios_dir(d):
    """在一个目录里找第一份尺寸合法的 .bin。"""
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return ""
    for n in names:
        if not n.lower().endswith(".bin"):
            continue
        p = os.path.join(d, n)
        try:
            if _size_ok(os.path.getsize(p)):
                c = check_bios(p)
                if c["ok"]:
                    return p
        except OSError:
            continue
    return ""


def set_bios(path):
    """验证并记住 BIOS 路径。返回 ``{ok, error, path, kind, config, bundled}``。

    验证不过就不写 —— 免得用户以为设置成功了，转换时才炸。

    验证通过后**先把文件复制进程序目录的 ``bios/``**（见 :func:`adopt`），
    再写 ``psf_bios.txt``。这样「指定一次」之后就真的不用再指定了：
    配置文件丢了、源文件被清理了，``bios/`` 里的副本还在。
    """
    c = check_bios(path)
    if not c["ok"]:
        return {"ok": False, "error": c["error"] or "BIOS 不可用"}
    p = os.path.abspath(str(path).strip().strip('"'))

    bundled, why = adopt(p)
    if bundled:
        p = bundled                        # 以后就认副本这份

    last = ""
    for conf in _conf_paths():
        try:
            d = os.path.dirname(conf)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            with open(conf, "w", encoding="utf-8") as f:
                f.write(p + "\n")
        except OSError as exc:
            last = "%s：%s" % (conf, exc)
            continue
        _invalidate()
        return {"ok": True, "error": "", "path": p, "kind": c["kind"],
                "config": conf, "bundled": bool(bundled),
                "bundle_note": "" if bundled else why}

    # 配置写不进去，但副本已经落在 bios/ 里了 —— 那也照样能用
    if bundled:
        _invalidate()
        return {"ok": True, "error": "", "path": p, "kind": c["kind"],
                "config": "", "bundled": True, "bundle_note": ""}
    return {"ok": False, "error": "写不进 %s（%s）" % (BIOS_CONF, last),
            "bundled": False, "bundle_note": why}


def bios_hint():
    """给界面用的一句话说明（PSF 为什么需要 BIOS）。"""
    return ("PSF 是 PS1 的 CPU+SPU 快照，解码需要一份 BIOS。"
            "请指定一份 4MB 的 PS2 BIOS（如 scph39001.bin），"
            "程序会把它转成 512KB 的 HE BIOS。"
            "本工具不分发 BIOS，用你自己机器上的那份。")


def length_tag(path):
    """读 PSF 自带的 ``length`` 标签（秒）。读不到返回 ``0.0``。

    PSF 的二进制负载后面跟一段 ``[TAG]`` 文本块，里面有
    ``length=0:8.054142``（``分:秒.小数``）。

    这里**只在尾部 256 KB 里找，绝不启动解码器** —— 下载页要按它估磁盘占用，
    逐文件起一次模拟器是不可接受的（一个包几十首）。
    """
    try:
        n = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, n - 262144))
            tail = f.read()
    except OSError:
        return 0.0
    i = tail.rfind(b"[TAG]")
    if i < 0:
        return 0.0
    m = _RE_LEN.search(tail[i:].decode("utf-8", "replace"))
    if not m:
        return 0.0
    try:
        return int(m.group(1)) * 60.0 + float(m.group(2))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------

def _text(b):
    """把子进程输出转成 str。

    解码器的错误文案是 UTF-8，但 Windows 上 CRT 可能按 GBK 输出；两种都试。
    """
    if not b:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("latin-1", "replace")


def _last_err(b):
    """从 stderr 里挑最后一条 ``ERR`` 行，没有就返回最后一行非空。"""
    lines = [ln.strip() for ln in _text(b).splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("ERR"):
            return ln[3:].strip()
    return lines[-1] if lines else ""


def _read_wav(path):
    """读 16-bit PCM 的 WAV，返回 ``(pcm_bytes, seconds, channels, rate)``。

    只处理本解码器自己写出来的那种（PCM / 16bit），遇到别的直接报错，
    不做「猜格式」。
    """
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("不是合法的 WAV")
    pos = 12
    fmt = None
    pcm = b""
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            pcm = body
        pos += 8 + size + (size & 1)
    if fmt is None:
        raise ValueError("WAV 里没有 fmt 块")
    _tag, channels, rate, _bps, _align, bits = fmt
    if bits != 16:
        raise ValueError("只支持 16bit，拿到 %dbit" % bits)
    seconds = (len(pcm) / float(channels * 2 * rate)) if rate else 0.0
    return pcm, seconds, channels, rate


def decode_pcm(path, cancel=None, on_progress=None, secs=0.0, bios=None):
    """把一个 ``.psf`` / ``.minipsf`` / ``.psf2`` / ``.minipsf2``
    解成 16bit/立体声小端 PCM 字节。

    * PS1（.psf）→ 采样率 44100
    * PS2（.psf2）→ 采样率 48000（psf2wav 按 version 自动选率）

    返回 ``dict``：``{ok, pcm, kind, title, seconds, error}`` ——
    与 :func:`psx_txtp.decode_pcm` 同一份口径，这样 PSF/PSF2 才能和 SPC /
    光盘镜像 / .txtp 走同一条主流水线（也才能出 BIN+CUE）。

    ``secs`` 为 0 时用 PSF 自带的 ``length`` 标签，再不行 60 秒。
    """
    res = {"ok": False, "pcm": b"", "kind": "PSF", "title": "",
           "seconds": 0.0, "error": ""}

    src = str(path)
    stem = os.path.basename(src)
    res["title"] = os.path.splitext(stem)[0]

    exe = find_decoder()
    if not exe:
        res["error"] = decoder_ready()[1]
        return res

    if bios:
        c = check_bios(bios)
        if not c["ok"]:
            res["error"] = "BIOS 不可用：%s" % c["error"]
            return res
        bios_path = str(bios)
    else:
        bios_path, why = find_bios()
        if not bios_path:
            res["error"] = "%s %s" % (why, bios_hint())
            return res

    if on_progress:
        on_progress(0.05)

    fd, tmp = tempfile.mkstemp(prefix="psf_", suffix=".wav")
    os.close(fd)
    try:
        cmd = [exe, src, tmp, "%.3f" % float(secs or 0.0), "--bios", bios_path]
        # 0xc0000142（DLL 初始化失败）是**偶发**的：多起几次基本都能过。
        # 只在这个码上重试 —— 别的退出码是「真错」，重试只是白等。
        for _try in range(_SPAWN_RETRY):
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        **_silent_kw())
            except OSError as exc:
                res["error"] = "启动解码器失败：%s" % exc
                return res
            while True:
                try:
                    out, err = proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set():
                        proc.kill()
                        proc.communicate()
                        res["error"] = "已取消"
                        return res
                    if on_progress:
                        on_progress(0.5)
            rc = proc.returncode
            if _rc_u32(rc) != _DLL_INIT_FAILED:
                break
            if _try + 1 < _SPAWN_RETRY:
                # 让系统的控制台/句柄回收一下再试，比立刻重起成功率高
                time.sleep(0.25 * (_try + 1))

        if rc == 0:
            try:
                pcm, seconds, channels, rate = _read_wav(tmp)
            except (OSError, ValueError) as exc:
                res["error"] = "读回 WAV 失败：%s" % exc
                return res
            if not pcm:
                res["error"] = "解码器说成功，但 WAV 里没有采样"
                return res
            res["ok"] = True
            res["pcm"] = pcm
            res["seconds"] = seconds
            res["kind"] = "PSF %dch/%d" % (channels, rate)
            if on_progress:
                on_progress(1.0)
            return res

        msg = _last_err(err)
        if _rc_u32(rc) == _DLL_INIT_FAILED:
            # 走到这儿说明重试 3 次都没起来。别把 0xc0000142 原样丢给用户，
            # 那串数字对谁都没用。
            res["error"] = ("解码器启动失败（0xc0000142，DLL 初始化失败）"
                            "已自动重试 %d 次仍不行 —— 通常是系统临时资源紧张，"
                            "关掉几个程序或稍后重试；反复出现请重启本程序。"
                            % _SPAWN_RETRY)
        elif rc == 4:
            res["error"] = "%s %s" % (msg or "缺少 BIOS。", bios_hint())
        elif rc == 3:
            res["error"] = (msg or "解码完成但输出全静音。")
        else:
            res["error"] = msg or ("解码器失败（退出码 %d）" % rc)
        return res
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def available():
    """给界面用：PSF 功能现在能不能用。返回 ``(bool, 原因/解码器路径)``。"""
    ok, why = decoder_ready()
    if not ok:
        return False, why
    bios, info = find_bios()
    if not bios:
        return False, "%s %s" % (info, bios_hint())
    return True, bios


#: ``status()`` 的缓存：``(时间戳, 结果)``。
#:
#: 为什么要缓存：``check_bios`` 会**真跑一次解码器**（要加载 BIOS、建 HE BIOS，
#: 约 0.3～0.5 秒）。界面每次轮询都跑一遍纯属浪费，而 BIOS 路径几分钟都不会变。
_STATUS_TTL = 4.0
_status_cache = (0.0, None)


def _invalidate():
    global _status_cache
    _status_cache = (0.0, None)


def status(force=False):
    """界面用的一份汇总。返回 ``dict``：

    ``{available, decoder, decoder_path, bios, bios_kind, why, hint,
       config, exts}``

    ``force=True`` 跳过缓存（刚设置完 BIOS、或用户点了「重新检测」）。
    """
    global _status_cache
    now = time.time()
    if not force and _status_cache[1] is not None \
            and now - _status_cache[0] < _STATUS_TTL:
        return dict(_status_cache[1])

    d_ok, d_info = decoder_ready()
    bios, why = find_bios()
    kind = check_bios(bios)["kind"] if bios else ""
    res = {
        "available": bool(d_ok and bios),
        "decoder": bool(d_ok),
        "decoder_path": d_info if d_ok else "",
        "bios": bios,
        "bios_kind": kind,
        # 这份 BIOS 是怎么来的：环境变量 / psf_bios.txt / 已集成 / 自动发现
        "bios_src": why if bios else "",
        # 是否已经固化在程序目录的 bios/ 里 —— 界面据此决定要不要提示「固化」
        "bios_bundled": _is_bundled(bios) if bios else False,
        "bios_home": bios_home(),
        "why": "" if bios else why,
        "hint": bios_hint(),
        "config": _conf_path(),
        "exts": list(PSF_ALL_EXTS),
    }
    _status_cache = (now, res)
    return dict(res)


# ---------------------------------------------------------------------------
# 命令行（没有界面时也能用）
# ---------------------------------------------------------------------------

def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(
        prog="psf_native",
        description="PSF / minipsf（PS1）→ WAV 的原生解码（需要一份 BIOS）")
    ap.add_argument("--status", action="store_true", help="打印当前可用性")
    ap.add_argument("--set-bios", metavar="BIOS", help="验证并记住 BIOS 路径")
    ap.add_argument("--decode", nargs=2, metavar=("PSF", "WAV"),
                    help="解码单个 PSF 到 WAV")
    ap.add_argument("--secs", type=float, default=0.0,
                    help="渲染秒数，0 = 用 PSF 自带的 length")
    a = ap.parse_args(argv)

    if a.set_bios:
        r = set_bios(a.set_bios)
        if not r["ok"]:
            print("❌ %s" % r["error"])
            return 1
        print("✅ 已记住 BIOS（%s）\n   %s" % (r["kind"], r["path"]))
        return 0

    if a.decode:
        src, dst = a.decode
        r = decode_pcm(src, secs=a.secs)
        if not r["ok"]:
            print("❌ %s" % r["error"])
            return 1
        with open(dst, "wb") as f:
            f.write(_wav_bytes(r["pcm"]))
        print("✅ %s  %.2f 秒 → %s" % (r["kind"], r["seconds"], dst))
        return 0

    st = status(force=True)
    print("解码器：%s" % (st["decoder_path"] or "❌ 找不到 %s" % DECODER_NAME))
    print("可用性：%s" % ("✅ 可用" if st["available"] else "❌ 不可用"))
    print("BIOS  ：%s%s" % (st["bios"],
                          ("（%s）" % st["bios_kind"]) if st["bios_kind"] else ""))
    if not st["available"]:
        print("        %s" % st["why"])
        print("        %s" % st["hint"])
    print("配置  ：%s" % st["config"])
    return 0 if st["available"] else 1


def _wav_bytes(pcm, channels=2, rate=44100, bits=16):
    """把 PCM 包成最小的 WAV（只给命令行 --decode 用）。"""
    import io
    n = len(pcm)
    hdr = io.BytesIO()
    hdr.write(b"RIFF")
    hdr.write(struct.pack("<I", 36 + n))
    hdr.write(b"WAVEfmt ")
    hdr.write(struct.pack("<IHHIIHH", 16, 1, channels, rate,
                          rate * channels * bits // 8, channels * bits // 8, bits))
    hdr.write(b"data")
    hdr.write(struct.pack("<I", n))
    return hdr.getvalue() + pcm


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
