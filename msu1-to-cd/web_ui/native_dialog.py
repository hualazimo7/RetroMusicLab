#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""native_dialog.py — Win32 原生文件/目录对话框（ctypes，零依赖）。

## 为什么要自己写

Web 版的界面在浏览器里，`<input type="file">` 只给文件内容**不给路径**
—— 安全限制。但转换必须拿到真实路径（一张盘 1.28 GB，不可能先上传再处理）。

所以对话框得由 Python 侧弹，用 comdlg32 的原生 API：
* 选文件 → ``GetOpenFileNameW``（支持多选）
* 选目录 → ``SHBrowseForFolderW``（``BIF_NEWDIALOGSTYLE`` 是新式可调整大小的那个）

不用 tkinter：Web 版要把它排除在打包之外（省 20+ MB，也避免和 Qt 版的
依赖图纠缠）。
"""

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
comdlg32 = ctypes.windll.comdlg32
shell32 = ctypes.windll.shell32
ole32 = ctypes.windll.ole32

# -- 常量 -----------------------------------------------------------------
OFN_READONLY = 0x00000001
OFN_HIDEREADONLY = 0x00000004
OFN_NOCHANGEDIR = 0x00000008
OFN_PATHMUSTEXIST = 0x00000800
OFN_FILEMUSTEXIST = 0x00001000
OFN_ALLOWMULTISELECT = 0x00000200
OFN_EXPLORER = 0x00080000

BIF_RETURNONLYFSDIRS = 0x0001
BIF_NEWDIALOGSTYLE = 0x0040
BIF_EDITBOX = 0x0010

#: 音源过滤器（顺序即默认选中项）
#:
#: ⚠ v5.4：这张表必须和 ``convert_job.classify`` 认识的格式**保持同步**。
#: 它只影响「添加音源」对话框里能看见什么 —— 漏了某类后缀，用户就**根本
#: 选不到**那个文件（Windows 的 OFN 对话框会把它隐藏掉），症状是
#: 「我明明有这个文件，对话框里却没有」。
FILTER_PARTS = [
    ("全部支持的音源", "*.cue;*.iso;*.bin;*.img;*.spc;*.pcm;*.mp3;*.vgm;*.vgz;"
                       "*.nsf;*.nsfe;*.gbs;*.hes;*.kss;*.ay;*.sap;*.sid;"
                       "*.txtp;*.xa;*.sgc;*.wsr;"
                       "*.psf;*.minipsf;*.psf1;*.minipsf1;"
                       "*.psf2;*.minipsf2;"
                       "*.usf;*.miniusf;"
                       "*.2sf;*.mini2sf;"
                       "*.ssf;*.minissf;*.dsf;*.minidsf;"
                       "*.gsf;*.minigsf;"
                       "*.adx;*.mod;*.hwas;*.swav;*.dvi;*.strm;*.npsf;"
                       "*.aif;*.aiff;*.aifc"),
    ("光盘镜像", "*.cue;*.iso;*.bin;*.img"),
    ("SPC / 芯片音源", "*.spc;*.vgm;*.vgz;*.nsf;*.nsfe;*.gbs;*.hes;*.kss;*.ay;*.sap;*.sid"),
    ("PSX 音频（.txtp 索引 / .xa）", "*.txtp;*.xa"),
    # PSF 是 PS1/PS2 的内存快照，解码要一份 BIOS —— 选 BIOS 用下面的 BIOS_PARTS
    ("PS1 / PS2 音源（.psf / .psf2）",
     "*.psf;*.minipsf;*.psf1;*.minipsf1;*.psf2;*.minipsf2"),
    ("N64 音源（.usf）", "*.usf;*.miniusf"),
    ("NDS 音源（.2sf）", "*.2sf;*.mini2sf"),
    ("Saturn / Dreamcast（.ssf / .dsf）",
     "*.ssf;*.minissf;*.dsf;*.minidsf"),
    ("GBA 音源（.gsf）", "*.gsf;*.minigsf"),
    # v5.4：SGC（Game Gear / Master System / Coleco 快照）。
    # ⚠ 之前漏了 —— SGC 接线后忘了把后缀加进过滤器，用户点「添加音源」
    #   时 Windows 的 OFN 对话框会直接把它隐藏掉，表现就是「文件明明在，
    #   对话框里却没有」。
    ("Game Gear / Master System（.sgc）", "*.sgc"),
    ("WonderSwan（.wsr）", "*.wsr"),
    # v5.4：流式音频。这批一直能转（stream_audio），但**从来没进过过滤器**
    # —— 同样是「能转但选不到」。
    ("流式音频（.adx / .mod / .hwas / .swav / .dvi / .strm / .npsf）",
     "*.adx;*.mod;*.hwas;*.swav;*.dvi;*.strm;*.npsf"),
    # v5.6：3DO 的 AIFF/AIFC（SDX2 / PCM）。单独一组 —— 它既不是芯片音源，
    # 也不是光盘镜像，用户从 3DO 包里看到的就是一堆 .aif。
    ("3DO 音源（.aif / .aifc）", "*.aif;*.aiff;*.aifc"),
    ("MSU-1 / MP3", "*.pcm;*.mp3"),
    ("所有文件", "*.*"),
]

#: 选 BIOS 用的过滤器。只认 .bin —— 4MB 的 PS2 BIOS 或 512KB 的 HE BIOS，
#: 两者扩展名都是 .bin（scph39001.bin / scph30004r.bin / he_bios.bin…）。
BIOS_PARTS = [
    ("PS1/PS2 BIOS（*.bin）", "*.bin"),
    ("所有文件", "*.*"),
]


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", wintypes.LPARAM),
        ("lpfnHook", wintypes.LPVOID),
        ("lpTemplateName", wintypes.LPCWSTR),
        ("pvReserved", wintypes.LPVOID),
        ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]


class BROWSEINFOW(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", wintypes.LPVOID),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", wintypes.UINT),
        ("lpfn", wintypes.LPVOID),
        ("lParam", wintypes.LPARAM),
        ("iImage", ctypes.c_int),
    ]


# ── Win32 函数签名声明 ───────────────────────────────────────────────────
#
# ⚠⚠⚠ 这一段**不是**可有可无的样板。ctypes 在没声明 ``restype`` 时，一律把
#   返回值按 32 位 ``c_int`` 处理。而下面这几个 API 返回的都是 **64 位指针**：
#
#       GetForegroundWindow()  → HWND              （窗口句柄）
#       SHBrowseForFolderW()   → PIDLIST_ABSOLUTE  （堆上的 ITEMIDLIST 指针）
#
#   64 位下堆指针动辄是 ``0x00000155_0F1E57C0`` 这种 40+ 位的值，按 32 位截
#   完只剩 ``0x0F1E57C0``。再把这个残值当指针传回去（SHGetPathFromIDListW /
#   CoTaskMemFree），Windows 立刻访问违例：
#
#       OSError: exception: access violation reading 0x000000000F1E57C0
#
#   用户看到的就是「点添加文件夹 → 弹窗 → 报错」，一个文件夹都加不进来。
#   实测复现：真值 0x1550f1e57c0 → 截断 0xf1e57c0 → 传进去必崩。
#
#   所以规矩只有一条：**凡是从 Win32 拿指针的，都必须声明 restype。**
#   新增 API 时别忘了回来加一行（``python -m web_ui.native_dialog --selftest``
#   会把漏网的揪出来）。
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND

comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
comdlg32.GetOpenFileNameW.restype = wintypes.BOOL

shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
shell32.SHBrowseForFolderW.restype = ctypes.c_void_p      # PIDLIST_ABSOLUTE

shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
shell32.SHGetPathFromIDListW.restype = wintypes.BOOL

ole32.CoInitialize.argtypes = [ctypes.c_void_p]
ole32.CoInitialize.restype = ctypes.c_long                # HRESULT
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None


def _owner_hwnd(parent_hwnd=0):
    """没给父窗口就用当前前台窗口 —— 否则对话框可能弹到应用后面去。"""
    return parent_hwnd or user32.GetForegroundWindow()


def _build_filter(parts=None):
    out = []
    for label, pattern in (parts or FILTER_PARTS):
        out.append(label)
        out.append(pattern)
    return "\0".join(out) + "\0\0"


def _split_multiselect(buf, maxlen):
    """``GetOpenFileNameW`` 多选时返回 ``目录\\0名1\\0名2\\0\\0``。

    ``buf.value`` 只能拿到第一段，必须自己按 \\0 切。
    """
    parts, i = [], 0
    while i < maxlen:
        j = i
        while j < maxlen and buf[j] != "\0":
            j += 1
        parts.append("".join(buf[i:j]))
        if j >= maxlen:
            break
        i = j + 1
        if i < maxlen and buf[i] == "\0":
            break
    return [p for p in parts if p]


def pick_files(title="选择音源", parent_hwnd=0, initial_dir="", multi=True,
               filter_parts=None):
    """弹原生「打开」对话框。返回绝对路径列表；用户取消返回 ``[]``。

    ``filter_parts`` 覆盖默认的音源过滤器（选 BIOS 时传 :data:`BIOS_PARTS`）。
    """
    buf = ctypes.create_unicode_buffer(65536)
    flags = OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST \
        | OFN_HIDEREADONLY | OFN_NOCHANGEDIR
    if multi:
        flags |= OFN_ALLOWMULTISELECT

    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = _owner_hwnd(parent_hwnd)
    ofn.lpstrFilter = _build_filter(filter_parts)
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = len(buf)
    ofn.lpstrTitle = title
    ofn.lpstrInitialDir = initial_dir or None
    ofn.Flags = flags
    ofn.lpstrDefExt = None

    if not comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return []                                  # 取消 或 出错

    if not multi:
        return [buf.value] if buf.value else []

    parts = _split_multiselect(buf, len(buf))
    if not parts:
        return []
    if len(parts) == 1:
        return [parts[0]]
    base = parts[0]
    return [_join(base, name) for name in parts[1:]]


def _join(base, name):
    import os
    if os.path.isabs(name):
        return name
    return os.path.join(base, name)


def pick_folder(title="选择目录", parent_hwnd=0):
    """弹原生「选择文件夹」对话框。返回路径；取消返回 ``""``。"""
    # SHBrowseForFolder 要求调用线程先初始化 COM（STA）。
    # ⚠ 只有 S_OK / S_FALSE（>= 0）才需要配对 CoUninitialize；失败时调了会
    #   把别的组件的 COM 引用计数减掉。
    hr = ole32.CoInitialize(None)
    try:
        display = ctypes.create_unicode_buffer(260)
        bi = BROWSEINFOW()
        bi.hwndOwner = _owner_hwnd(parent_hwnd)
        bi.pidlRoot = None
        bi.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
        bi.lpszTitle = title
        bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_EDITBOX
        bi.lpfn = None
        bi.lParam = 0
        bi.iImage = 0

        # 返回的是堆上的 PIDLIST_ABSOLUTE。restype 已在文件头声明成
        # c_void_p —— 不声明就会被截成 32 位，后面一用就访问违例。
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return ""
        try:
            path_buf = ctypes.create_unicode_buffer(260)
            if shell32.SHGetPathFromIDListW(pidl, path_buf):
                return path_buf.value
            return ""
        finally:
            ole32.CoTaskMemFree(pidl)
    finally:
        if hr >= 0:
            ole32.CoUninitialize()


def _selftest():
    """检查用到的 Win32 函数都声明了签名 —— 漏一个就可能截断指针。

    ``ctypes`` 的 ``argtypes`` 默认是 ``None``（= 没声明）。这个检查比
    「跑一次对话框看崩不崩」可靠得多：崩不崩取决于当时堆地址的高 32 位
    是不是恰好为 0，很多机器上是**偶发**的。
    """
    table = [
        (user32, "GetForegroundWindow"),
        (comdlg32, "GetOpenFileNameW"),
        (shell32, "SHBrowseForFolderW"),
        (shell32, "SHGetPathFromIDListW"),
        (ole32, "CoInitialize"),
        (ole32, "CoUninitialize"),
        (ole32, "CoTaskMemFree"),
    ]
    bad = [n for dll, n in table if getattr(dll, n).argtypes is None]
    for dll, n in table:
        fn = getattr(dll, n)
        print("  %-22s argtypes=%-38s restype=%s"
              % (n, fn.argtypes, getattr(fn.restype, "__name__", fn.restype)))
    print()
    if bad:
        print("✗ 这些函数没声明签名（64 位下会截断指针）：%s" % ", ".join(bad))
        return 1
    print("✓ 全部已声明")
    return 0


if __name__ == "__main__":
    import sys
    if "--folder" in sys.argv:
        print(repr(pick_folder()))
    elif "--selftest" in sys.argv:
        sys.exit(_selftest())
    else:
        print(repr(pick_files()))
