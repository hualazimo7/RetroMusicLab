#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_web_pyi.py — 用 **PyInstaller** 打包 Web 版（Nuitka 的备胎）。

## 为什么需要这条备胎

2026-10-23 用户实测：另一台 Win11 上双击 `MSU1Converter-web.exe`
**连日志都没有**，诊断脚本拿到探针退出码：

    probe exit code = -1073741795   →   0xC000001D  STATUS_ILLEGAL_INSTRUCTION

不是杀软、不是 Smart App Control、不是权限、不是文件损坏 ——
**进程起来了，但 CPU 不认识第一条指令**。

排除了编译选项（Nuitka 4.3.1 确实会给 zig 传 `-march=x86_64`）、
排除了 32 位系统（`PROCESSOR_ARCHITECTURE = AMD64`）、
排除了下载锁定（无 Zone.Identifier）。

剩下的解释只有一个：**zig 生成的 x86-64 代码在这台机器的 CPU 上跑不了**。
（zig 的 `zig cc` 不带 `-target` 时按**本机** CPU 出码，而本机是
 Ryzen 9 8940HX / Zen4，带 AVX-512。）

而 v4.44 的 tkinter 版是 **PyInstaller** 打的（PE 里有 `pyi-` / `MEIPASS`
标记，没有一处 `Nuitka`），用户反馈那套是能便携使用的 ——
**PyInstaller 的 bootloader 是 MSVC 编译的 baseline 二进制，天然没有这个问题。**

所以：Nuitka 留给「启动快」（1.9 秒），PyInstaller 作为「一定能跑」的兜底。

## 用法

    C:/Python314/python.exe build_web_pyi.py
    C:/Python314/python.exe build_web_pyi.py --dry

产物：``build/RetroMusicLab-pyi/RetroMusicLab.exe``
"""

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "build", "RetroMusicLab-pyi")
WORK_DIR = os.path.join(OUT_DIR, "work")
NAME = "RetroMusicLab"
VER = "1.0"

#: 与 build_web.py 保持一致 —— 两份清单必须同步改，否则「Nuitka 版好、
#: PyInstaller 版 404」这种问题极难查（同一份前端资源在两套产物里落点不同）。
WEB_STATIC = ("index.html", "app.css", "app.js",
              "download.html", "download.css", "download.js",
              "cloud.js", "cloud.css",
              # 6 套复古主题注入器 + 频谱可视化：两页共用
              "theme.js", "viz.js")

DLLS = ("libgme.dll", "libstdc++-6.dll",
        "libgcc_s_seh-1.dll", "libwinpthread-1.dll")

EXTRA_EXE = ("psf2wav.exe", "usf2wav.exe", "vio2wav.exe",
             "ssf2wav.exe", "gsf2wav.exe", "wsr2wav.exe", "kss2wav.exe",
             # v5.9.13：PC-98 / PC-88 的 YM2608（OPNA）。ymfm 核编出来的原生
             # 解码器，和 kss2wav / wsr2wav 一样靠这份清单才带出去 ——
             # 漏了的表现是「.s98 加得进来、一按转换说找不到解码器」。
             "s982wav.exe",
             # v5.9.10：Capcom QSound（CPS1/CPS2 街机）。和上面几个同一个坑 ——
             # 它是普通文件，靠这份清单才带出去，漏了的表现是
             # 「.miniqsf 加得进来、一按转换说找不到解码器」。
             "qsf2wav.exe")

DATA_FILES = ("zh_titles_user.json",)

#: v5.9.12：**随包带一个第三方解码器** —— 全工程第一次。
#:
#: 为什么非带不可：FFmpeg 明确不支持这两族，而它们各自都是一个机种的全部音源。
#:   ``.wem`` → Audiokinetic Wwise 的 **Custom Vorbis**（fmt tag 0xFFFF，
#:              codebook 靠几十 KB 模板表重建，文件里连 "vorbis" 六个字母都没有）
#:   ``.fsb`` → FMOD FSB5 里的 **Nintendo DSP 4-bit ADPCM**
#:   FFmpeg 对 .fsb 直接报 ``PatchWelcomeError: Not yet implemented in FFmpeg,
#:   patches welcome``；对 .wem 是能开容器但 ``codec_context`` 为 None。
#: 只有 vgmstream 这两族都吃，而且**还认得 PS4 的 .txtp 序列**（分层 + 循环 + 裁剪）——
#: 那 186 个 txtp 才是真曲目，裸 wem 只是素材。
#:
#: ⚠ **11 个 DLL 一个都不能少**。实测只拷 exe + libvorbis.dll →
#:   ``exit 127`` 报 ``libspeex-1.dll``；补到 8 个（想省掉 FFmpeg 那 4 个约 6MB）
#:   → 仍然 ``exit 127``，这次报 ``avutil-vgmstream-57.dll``。
#:   它们是**载入时**硬依赖，不是用到才加载，所以砍不掉。合计约 11MB。
#:
#: 许可：vgmstream 是 **ISC**，随包分发的唯一义务是附版权声明 ——
#:   所以 ``vgmstream-COPYING.txt`` 必须一起带，别当垃圾清了。
VGM_FILES = (
    "vgmstream-cli.exe",
    "avcodec-vgmstream-59.dll", "avformat-vgmstream-59.dll",
    "avutil-vgmstream-57.dll", "swresample-vgmstream-4.dll",
    "libatrac9.dll", "libcelt-0061.dll", "libcelt-0110.dll",
    "libg719_decode.dll", "libmpg123-0.dll", "libspeex-1.dll",
    "libvorbis.dll",
    "vgmstream-COPYING.txt",
)

#: 函数内 import / 动态 import 的模块，PyInstaller 的静态分析看不到。
#: 少一个的表现都是「源码树正常、打包后某个功能一按就 ModuleNotFoundError」。
HIDDEN = (
    "chip_formats", "chip_decode", "cd_audio", "wavmerge",
    "cloud_upload", "netease_cloud", "netease_cookie", "tmpdir",
    "Crypto.Cipher.AES", "soundfile",
    "web_ui.dl_api", "dl_convert", "joshw_dl", "joshw_source",
    # native_dialog 是 server.py 的模块级 import，静态分析本来就能看到；
    # 显式列出来是为了「改文件类型过滤器后忘了它也会被打进去」这件事有保障。
    "web_ui.native_dialog",
    "zh_titles", "zh_online", "psf_native", "usf_native", "vio_native",
    "ssf_native", "gsf_native", "wsr_native",
    # v5.9.10：Capcom QSound。convert_job / dl_convert 里都是延迟 import，
    # 静态分析看不到 —— 不列就是「.miniqsf 一转换就 ModuleNotFoundError」。
    "qsf_native",
    # kss_native 是 chip_decode.load_chip_paths 里用 __import__ 动态加载的
    # （和 wsr_native 同一个分流点），静态分析完全看不到 —— 不列就是
    # 「.kss 一转换就 ModuleNotFoundError」。
    "kss_native",
    # v5.9.13：s98_native 同样是在 chip_decode.load_chip_paths 里用
    # __import__ 动态加载的（第三个分流点），静态分析看不到。
    "s98_native",
    # aif_audio 是 stream_audio.probe()/decode_any() 里的函数内 import
    # （.aif 扩展名太泛，怕它把别的包拖崩，故意延迟到真正用的时候才导）。
    # 静态分析看不到 → 必须显式列，否则打包后 3DO 一转换就 ModuleNotFoundError。
    "aif_audio",
    # zophar_source 是 dl_api 的 try/except 可选 import（缺了只是少两个机种，
    # 不该让下载页打不开）—— 这种 import PyInstaller 一律算不出来，必须列。
    "zophar_source",
    # v5.9.12：vgmstream 的封装。stream_audio 与 psx_txtp 里**都是延迟 import**
    # （只有真遇到 .wem / .fsb 才付那次进程启动的账），静态分析看不到 ——
    # 不列就是「.wem 一转换就 ModuleNotFoundError」。它还要 12 个同级文件
    # （exe + 11 DLL + COPYING），见 VGM_FILES。
    "vgm_audio",
    # v5.9.11：GameCube 的 Pivotal PSF。convert_job / dl_convert / psx_txtp
    # 里**都是延迟 import**（判魔数才决定走不走它），静态分析看不到 ——
    # 不列就是「.psf 一转换就 ModuleNotFoundError」。
    "pivotal_psf",
    # v5.9.7：flac_out 是 spc_wav.write_tracks 里的函数内 import
    # （只有真选了 FLAC 才付 soundfile 的导入开销）。漏了的表现很隐蔽：
    # 打包后界面正常、选 WAV 正常，一选 FLAC 就整批报
    # 「FLAC 编码器不可用：ModuleNotFoundError」。
    "flac_out",
    # v5.9.14：封面。dl_api / spc_wav / convert_job 里**都是 try/except 的
    # 可选 import**（缺了只是少一个功能，不该让下载页打不开），静态分析
    # 一律算不出来。漏了的表现：下载照常、日志里没有一行 🖼、
    # 转出来的 FLAC 没有封面 —— 全是「静默少一个功能」，最难发现的那种。
    "cover_art",
    # v6.0.5：IGDB 封面兜底源。同样是 dl_api 里的 try/except 可选 import，
    # 静态分析看不到。漏了的表现和 cover_art 一样静默：日志里只有
    # 「libretro 没收录」，永远等不到 IGDB 那一行 —— 用户会以为
    # 「这版没做 IGDB」，而实际上代码全在、只是没打进去。
    "cover_igdb",
    "web_ui.cloud_api",
    "segno",
    # tkinter 兜底界面（本机一个浏览器都没有时的最后一条路）
    "msu1_gui", "retro_theme", "spc_wav",
    "tkinter", "tkinter.ttk", "tkinter.filedialog",
    "tkinter.messagebox", "tkinter.scrolledtext", "tkinter.font",
)

#: 整个包都要收的（C 扩展 / 自带数据文件，光靠 hidden-import 拿不到）
COLLECT_ALL = ("av", "py7zr", "soundfile")

#: 明确排掉，别把无关的大件拖进来
EXCLUDE = (
    "matplotlib", "scipy", "pandas", "IPython", "jupyter",
    "PySide6", "shiboken6", "qt_ui", "msu1_qt", "qt_poc",
    "player", "miniaudio", "websocket", "PIL",
    "turtle", "turtledemo", "idlelib",
)


def _exists(mod):
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def build_args():
    sep = ";" if os.name == "nt" else ":"
    args = [
        os.path.join(ROOT, "msu1_web.py"),
        "--onefile",
        "--noconsole",
        "--clean",
        "--noconfirm",
        "--name", NAME,
        "--distpath", OUT_DIR,
        "--workpath", WORK_DIR,
        "--specpath", OUT_DIR,
    ]

    icon = os.path.join(ROOT, "assets", "app.ico")
    if os.path.isfile(icon):
        args += ["--icon", icon]

    # 前端静态资源 → <MEIPASS>/web_ui/，对上 server.py 的 WEB_DIR
    for n in WEB_STATIC:
        src = os.path.join(ROOT, "web_ui", n)
        if os.path.isfile(src):
            args += ["--add-data", src + sep + "web_ui"]
        else:
            print("⚠ 缺前端文件 %s" % src, flush=True)

    # libgme 家族 + 四个外挂解码器（psf/usf/vio/ssf）+ 中文词典 → <MEIPASS>/ 根
    for n in DLLS + EXTRA_EXE + DATA_FILES:
        src = os.path.join(ROOT, n)
        if os.path.isfile(src):
            args += ["--add-data", src + sep + "."]
        else:
            print("⚠ 缺 %s" % src, flush=True)

    # v5.9.12：vgmstream 全家 → <MEIPASS>/ 根（和 exe 同级）。
    # vgm_audio.find_decoder() 是按「exe 同级」找的，落到子目录就找不到。
    # 缺 vgmstream-COPYING.txt 不算致命（程序能跑），但那是 ISC 的署名义务，
    # 所以只警告不中断，别让版权问题变成构建失败。
    _miss_vgm = []
    for n in VGM_FILES:
        src = os.path.join(ROOT, n)
        if os.path.isfile(src):
            args += ["--add-data", src + sep + "."]
        else:
            _miss_vgm.append(n)
    if _miss_vgm:
        print("⚠ 缺 %d 个 vgmstream 文件：%s\n"
              "  打包后 .wem / .fsb 会报「找不到解码器」（11 个 DLL 缺一不可）"
              % (len(_miss_vgm), ", ".join(_miss_vgm)), flush=True)

    # 内置索引快照（离线也能列下载列表）
    index_dir = os.path.join(ROOT, "index")
    if os.path.isdir(index_dir):
        args += ["--add-data", index_dir + sep + "index"]
    else:
        print("⚠ 没找到 index/，下载页将退化为联网扫描", flush=True)

    for m in HIDDEN:
        args += ["--hidden-import", m]

    for pkg in COLLECT_ALL:
        if _exists(pkg.split(".")[0]):
            args += ["--collect-all", pkg]
        else:
            print("⚠ 没装 %s，跳过 --collect-all" % pkg, flush=True)

    for m in EXCLUDE:
        args += ["--exclude-module", m]

    return args


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_args()

    print("PyInstaller 打包 · %s v%s" % (NAME, VER), flush=True)
    print("输出目录: %s" % OUT_DIR, flush=True)
    if "--dry" in argv:
        print(" ".join('"%s"' % a if " " in a else a for a in args), flush=True)
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = __import__("time").time()

    from PyInstaller.__main__ import run as pyi_run
    try:
        pyi_run(args)
    except SystemExit as exc:              # PyInstaller 用 sys.exit 报错
        if exc.code:
            print("\nPyInstaller 失败（exit %s）" % exc.code, flush=True)
            return exc.code

    dt = __import__("time").time() - t0
    exe = os.path.join(OUT_DIR, "%s.exe" % NAME)
    if os.path.isfile(exe):
        print("\n产物: %s（%.1f MB，耗时 %.1f 分钟）"
              % (exe, os.path.getsize(exe) / 1048576.0, dt / 60.0), flush=True)
    else:
        print("\n没找到产物 exe，耗时 %.1f 分钟" % (dt / 60.0), flush=True)
        return 1

    # BIOS 跟 exe 走（和 build_web.py 同一套理由：版权 + 免再指定）
    bios_src = os.path.join(ROOT, "bios")
    if os.path.isdir(bios_src):
        bios_dst = os.path.join(OUT_DIR, "bios")
        try:
            shutil.copytree(bios_src, bios_dst, dirs_exist_ok=True)
            print("BIOS: 已带到 %s" % bios_dst, flush=True)
        except OSError as exc:               # noqa: BLE001
            print("⚠ 复制 bios/ 失败：%s" % exc, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
