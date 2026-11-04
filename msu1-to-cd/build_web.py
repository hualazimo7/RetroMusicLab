#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_web.py — 用 Nuitka 把 Web 版打包成单个 exe。

## 为什么换成 Nuitka

v5.0-qt 是 PyInstaller ``--onefile``，实测**冷启动 9 秒**。原因不是 Python
慢，而是 onefile 每次启动都要把 ~100 MB 解包到 ``%TEMP%\\_MEIxxxx``。Nuitka
把 Python 代码编译成 C，再用 zstd 压缩自解包，同样的机器上能快 3～5 倍。

Web 版又比 Qt 版少了一整块 PySide6（约 90 MB），所以体积和启动都应该明显好于
Qt 版。

## 用法

    C:/Python314/python.exe build_web.py              # onefile（单文件）
    C:/Python314/python.exe build_web.py --standalone  # 免解包目录版（最快启动）
    C:/Python314/python.exe build_web.py --probe       # 7 MB 探针（排查换机打不开）
    C:/Python314/python.exe build_web.py --dry         # 只打印命令

产物：``build/MSU1Converter-web/MSU1Converter-web.exe``
探针：``build/probe/MSU1Converter-Probe.exe``（自动复制一份到 ``dist/``）
"""

import glob
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "build", "MSU1Converter-web")
NAME = "MSU1Converter-web"
VER = "5.2.0.6"

#: 静态前端文件（**只这些**，别把 .py 也塞进去 —— 那等于送源码）
#:
#: ⚠ 主界面（index/app）和下载页（download）是两套独立静态资源。
#:   漏掉任何一件，对应页面在打包后就是 404 —— 而且表现很迷惑：
#:   ``--app`` 窗口会显示「找不到文件」，但主界面看起来完全正常。
WEB_STATIC = ("index.html", "app.css", "app.js",
              "download.html", "download.css", "download.js",
              # 云盘登录窗 / 上传范围（两页共用）
              "cloud.js", "cloud.css",
              # 6 套复古主题的注入器（两页共用）。漏了它复古配色点了没反应，
              # 只在浏览器控制台报 "ThemeKit is not defined"。
              "theme.js",
              # 频谱 / 波形可视化（两页共用）。漏了它两个页面的音频播放区
              # 直接报 "VizKit is not defined"，试听功能整块失效。
              "viz.js")

#: ``web_ui/player/`` 下的播放器模块（内核 / 两套皮肤 / 三个纯逻辑模块）。
#:
#: ⚠ **自动扫描，不写死文件名**：加一套皮肤或一个纯逻辑模块时，打包脚本
#:   不该再改一次 —— 漏改的表现是「浏览器里一切正常，EXE 里 404、
#:   PlayerCore is not defined」，而且两套产物（Nuitka / PyInstaller）
#:   各有一份清单，漏一处就只坏一边，极难查。
#:   排除 ``*.test.js``：那是 node 跑的单测，不进产物。
PLAYER_DIR = os.path.join(ROOT, "web_ui", "player")


def _player_static():
    if not os.path.isdir(PLAYER_DIR):
        return ()
    out = []
    for n in sorted(os.listdir(PLAYER_DIR)):
        if not n.endswith((".js", ".css")):
            continue
        if n.endswith(".test.js"):
            continue
        out.append("player/" + n)
    return tuple(out)


WEB_STATIC_SUB = _player_static()

#: libgme 及其 mingw 依赖，必须与 exe 同级（msu1_to_cd 按 __file__ 同目录找）
#:
#: ⚠ ``zlib1.dll`` **不在这里**（v5.2.0.6 起）。放开 tkinter 之后 Tk 自己也
#:   带一份 ``zlib1.dll``，Nuitka 的 dll-files 插件会先把它当 dll 放进包根，
#:   我们再 ``--include-data-files`` 一份同名的就撞车：
#:
#:       FATAL: Error, data file to be placed in distribution as
#:       'zlib1.dll' conflicts with dll 'zlib1.dll'
#:
#:   zlib 1.x 之间保持 ABI 兼容（导出符号名不变），libgme 用 Tk 带的那份
#:   照样能加载 —— 打包后靠 ``/api/diag`` 的 ``libgme: ok`` 和一次真实 SPC
#:   转换来确认，不靠猜。
DLLS = ("libgme.dll", "libstdc++-6.dll",
        "libgcc_s_seh-1.dll", "libwinpthread-1.dll")

#: 额外的可执行文件，同样要跟主程序放在一起
#:
#: ``psf2wav.exe`` 是 PSF 系（PS1 芯片音源）的解码器，由 ``tools/psf2wav/``
#: 里的源码编译而来（psflib + Highly Experimental + zlib）。**它不是一个
#: Python 模块**，Nuitka 不会自动带上 —— 漏了的表现是「.psf 加得进列表、
#: 一按转换就报找不到 psf2wav.exe」，而源码树里永远正常。
#:
#: 落地位置与 DLLS 一致（解包根 = exe 同级），正好对上
#: ``psf_native._search_dirs()`` 的第一个候选。
EXTRA_EXE = ("psf2wav.exe",)

#: 非 Python 的**数据文件**，必须解包到包根（= 模块 ``__file__`` 同目录）
#:
#: ``zh_titles_user.json`` 是游戏名中文词典的**主体**（约 2.7 万条）。
#: ``zh_titles.py`` 里的字面量只有一千多条，剩下全靠这个 JSON 在 import 时
#: 合并进 ``GAME_ZH``。**它不是一个 Python 模块，Nuitka 不会自动带上**。
#:
#: 漏了的表现极具迷惑性：程序一切正常，只有下载页「中文名」那一列几乎全是
#: 「—」（实测 SPC 机种 1918 条里只剩 321 条有名字，源码态是 1900 条）。
#: 因为 ``zh_titles`` 模块本身在包里，任何「模块在不在」的检查都是绿的。
DATA_FILES = ("zh_titles_user.json",)

#: 明确不跟进去的模块 —— 每一条都是体积或坑
NOFOLLOW = [
    # 别的界面栈。
    #
    # ⚠⚠ tkinter 那套**不能**再排掉了（v5.2.0.6）。它是「本机一个浏览器都
    #   没有」时的**最后一条路**（``web_ui/shell.py`` 的 ``_tk_fallback``）。
    #   排掉就等于：精简版 Win10 / 企业镜像上双击 exe → 找不到 Edge → 退回
    #   默认浏览器 → 也没有 → 只剩一个「请手动打开这个地址」的弹窗。
    #   用户的原话是「任何一台 win10、win11 都能正常运行」，所以宁可
    #   体积 +约 10 MB 也要留着这套界面。
    #   turtle / turtledemo / idlelib 是 tkinter 自带的玩具和 IDE，仍然排掉。
    "turtle", "turtledemo", "idlelib",
    "PySide6", "shiboken6", "qt_ui", "msu1_qt", "qt_poc",
    # ⚠ 下面这 6 个**曾经**在这儿（「Web 版还没移植下载窗口」时的老账）。
    #   v5.1 已经把下载窗口整套移植过来了，再排掉就是：
    #     下载页能打开、能列机种，但一按「开始」就
    #     ``ModuleNotFoundError: No module named 'joshw_source'``。
    #   —— 所以 dl_convert / joshw_dl / joshw_source / zh_titles /
    #      zh_online **全部放开**。
    #   ⚠ psx_txtp 同理**不能**放这儿 —— .txtp 已接进主流水线
    #     （convert_job 里函数内 import），排掉它等于打包后
    #     「加得进音源、一按转换就失败」。
    #   ⚠ psf_native 同理**不能**放这儿 —— .psf 走原生解码，
    #     convert_job 里也是函数内 import。
    # 试听改由浏览器做（<audio>），本地播放器不再需要
    "player", "miniaudio",
    # 科学计算栈：numpy 要留（msu1_to_cd / soundfile 依赖），这三个纯属拖累
    "matplotlib", "scipy", "pandas", "IPython", "jupyter",
    # 开发期工具
    "websocket", "PIL",
]


def build_cmd(standalone=False):
    py = sys.executable
    cmd = [py, "-m", "nuitka", os.path.join(ROOT, "msu1_web.py")]
    cmd += [
        "--assume-yes-for-downloads",
        "--windows-console-mode=disable",
        "--output-dir=" + OUT_DIR,
        "--output-filename=%s.exe" % NAME,
        "--company-name=RetroMusic Lab",
        "--product-name=RetroMusic Lab",
        "--file-description=RetroMusic Lab 游戏音乐转换器（Web 版）",
        "--file-version=" + VER,
        "--product-version=" + VER,
        "--copyright=RetroMusic Lab",
        # 别把 .py 源码塞进包里
        "--nofollow-import-to=" + ",".join(NOFOLLOW),
    ]

    icon = os.path.join(ROOT, "assets", "app.ico")
    if os.path.isfile(icon):
        cmd.append("--windows-icon-from-ico=" + icon)

    # 前端静态资源：落到 <dist>/web_ui/，server.py 的 WEB_DIR 正好指这儿
    for n in WEB_STATIC + WEB_STATIC_SUB:
        src = os.path.join(ROOT, "web_ui", n)
        cmd.append("--include-data-files=%s=web_ui/%s" % (src, n))

    # libgme 家族：与 exe 同级
    for n in DLLS:
        src = os.path.join(ROOT, n)
        if os.path.isfile(src):
            cmd.append("--include-data-files=%s=%s" % (src, n))

    # PSF 解码器：同样与 exe 同级。缺了它 .psf 就解不了（见 EXTRA_EXE 注释）
    for n in EXTRA_EXE:
        src = os.path.join(ROOT, n)
        if os.path.isfile(src):
            cmd.append("--include-data-files=%s=%s" % (src, n))
        else:
            print("⚠ 没找到 %s，打包后 PSF 音源将无法解码（"
                  "先跑 tools/psf2wav/build.py）" % n, flush=True)

    # 中文词典：必须落在**包根**（不是 web_ui/ 下）——
    # ``zh_titles._load_user_dict()`` 按「模块 __file__ 同目录」找它。
    # onefile 下编译进去的模块 __file__ 就指向解包根，所以 = 包根。
    for n in DATA_FILES:
        src = os.path.join(ROOT, n)
        if os.path.isfile(src):
            cmd.append("--include-data-files=%s=%s" % (src, n))
        else:
            print("⚠ 缺 %s —— 打包后下载页的中文名会大面积缺失" % n, flush=True)

    # PyAV 的 ffmpeg DLL：**不要**手动搬。
    #
    # Nuitka 的 dll-files 插件会自己把 av.libs 里的 DLL 依赖扫出来并放到
    # ``av.libs/``，和 ``av/__init__.py`` 里那段 delvewheel 补丁对得上
    # （它按 ``../av.libs`` 找）。手动再加一遍会撞车：
    #
    #     FATAL: Error, data file to be placed in distribution as
    #     'av.libs\avcodec-63-….dll' conflicts with dll 'av.libs\avcodec-63-….dll'
    #
    # （``--include-data-dir`` 那条路更糟：文件名形如 ``avcodec-63-<hash>.dll``，
    #  命中 Nuitka 的 `_re_versioned_shared_library_suffix` 过滤器 → 报
    #  "No data files in directory" 然后静默什么都不带。）
    cmd.append("--include-package=av")

    # 惰性 import 的模块，静态分析看不到，必须显式点名
    for m in ("chip_formats", "chip_decode", "cd_audio", "wavmerge",
              "cloud_upload", "netease_cloud", "netease_cookie", "tmpdir",
              "Crypto.Cipher.AES", "soundfile",
              # v5.9.7 FLAC 输出：spc_wav.write_tracks 里的函数内 import，
              # 漏了就是「选 WAV 正常、一选 FLAC 整批报编码器不可用」。
              "flac_out",
              # v5.1 下载页：dl_api 是 server.py 里函数内 import 的，
              # 它自己再 try/except 拉下面这 6 个。
              # 全部点名 —— 少一个就是「下载页打得开、按开始就崩」。
              "web_ui.dl_api", "dl_convert", "joshw_dl", "joshw_source",
              "zh_titles", "zh_online",
              # PSF 系（PS1 芯片音源）。convert_job 里是函数内 import，
              # 静态分析看不到 —— 漏了就是「.psf 加得进、一按转换就
              # ModuleNotFoundError」。它还要一个同级的 psf2wav.exe（见 EXTRA_EXE）。
              #
              # ⚠ 注意：老的 ``psf_bridge``（转手拉 foobar2000 那套）**不再需要**。
              #   PSF 现在由 psf_native 原生解码，Web 版一行都不碰 foobar2000。
              #   psf_bridge.py 仍留在源码树里 —— tkinter 版还在用它。
              "psf_native",
              # 云盘账号层（浏览器登录 / 粘贴 Cookie / 扫码 / 换号）。
              # server.py 与 dl_api.py 都是**函数内 import** 它，
              # 静态分析看不到 —— 漏了就是「点上传云盘报 cloud_api 没打进来」。
              # 它背后还要 netease_cookie（CDP 取 cookie）+ segno（二维码矩阵）。
              "web_ui.cloud_api", "segno",
              # v5.2.0.6 兜底界面：本机没有浏览器时起这套（见
              # web_ui/shell.py 的 _tk_fallback）。全是函数内 import，
              # 静态分析看不到 —— 漏一个就是「切 tk 时报 ModuleNotFoundError」，
              # 而那台机器本来就没有别的界面可用，等于彻底打不开。
              "msu1_gui", "retro_theme", "spc_wav",
              "tkinter", "tkinter.ttk", "tkinter.filedialog",
              "tkinter.messagebox", "tkinter.scrolledtext",
              "tkinter.font"):
        cmd.append("--include-module=" + m)

    # 7z 解压：joshw 的包全是 .7z。py7zr 底下挂着一串 C 扩展
    # （pyzstd / inflate64 / pybcj / ppmd / pycryptodomex / multivolumefile），
    # 交给 --include-package 让它自己扫，别手工一个个点。
    cmd.append("--include-package=py7zr")

    # 下载页的内置索引快照（30 个机种 × <key>.json.gz，共约 2.1 MB）。
    #
    # ⚠ 不打包它的后果：`joshw_source.load_bundled()` 找不到快照 → 退化成
    #   联网全量扫描。表现是「点了机种转半天 / 直接超时」，而且**离线就完全
    #   用不了**。源码树里永远正常（index/ 就在旁边），只有 exe 会犯。
    #
    # 落到解包根 `index/`，正好对上 joshw_source.bundled_dir() 的
    # `<__file__ 目录>/index` 分支。
    index_dir = os.path.join(ROOT, "index")
    if os.path.isdir(index_dir):
        cmd.append("--include-data-dir=%s=index" % index_dir)
    else:
        print("⚠ 没找到 index/ 目录，下载页将退化为联网扫描", flush=True)

    cmd.append("--onefile" if not standalone else "--standalone")
    return cmd


PROBE_NAME = "MSU1Converter-Probe"
PROBE_OUT = os.path.join(ROOT, "build", "probe")


def probe_cmd():
    """探针的打包命令 —— **必须和主程序用同一套 onefile 参数**。

    探针的全部意义就是「换一个 7 MB 的空壳，走一模一样的那条路」。
    只要这里和 :func:`build_cmd` 的 onefile 参数不一致（比如漏了
    ``--windows-console-mode=disable``，或者用了 standalone），
    测出来的结论就不再能代表主程序 —— 那还不如不做。

    所以刻意**不加**任何 include/NOFOLLOW：probe.py 只用 ctypes + 标准库，
    依赖越少，噪声越小。
    """
    cmd = [sys.executable, "-m", "nuitka", os.path.join(ROOT, "probe.py")]
    cmd += [
        "--assume-yes-for-downloads",
        "--windows-console-mode=disable",
        "--output-dir=" + PROBE_OUT,
        "--output-filename=%s.exe" % PROBE_NAME,
        "--company-name=RetroMusic Lab",
        "--product-name=RetroMusic Lab",
        "--file-description=启动探针（判定本机能否运行 onefile 程序）",
        "--file-version=1.0",
        "--product-version=1.0",
    ]
    icon = os.path.join(ROOT, "assets", "app.ico")
    if os.path.isfile(icon):
        cmd.append("--windows-icon-from-ico=" + icon)
    cmd.append("--onefile")
    return cmd


def build_probe(argv):
    cmd = probe_cmd()
    print("Nuitka 打包 · 探针 %s" % PROBE_NAME, flush=True)
    print(" ".join('"%s"' % c if " " in c else c for c in cmd), flush=True)
    if "--dry" in argv:
        return 0

    os.makedirs(PROBE_OUT, exist_ok=True)
    t0 = __import__("time").time()
    rc = subprocess.call(cmd, cwd=ROOT)
    print("\n退出码 %d，耗时 %.1f 分钟"
          % (rc, (__import__("time").time() - t0) / 60.0), flush=True)
    if rc != 0:
        return rc

    exe = os.path.join(PROBE_OUT, "%s.exe" % PROBE_NAME)
    if os.path.isfile(exe):
        print("产物: %s（%.1f MB）"
              % (exe, os.path.getsize(exe) / 1048576.0), flush=True)
        dst = os.path.join(ROOT, "dist", "%s.exe" % PROBE_NAME)
        try:
            shutil.copy2(exe, dst)
            print("已复制到 %s —— 和 MSU1Converter-web.exe 放同一个文件夹"
                  "就能用" % dst, flush=True)
        except OSError as exc:               # noqa: BLE001
            print("⚠ 复制到 dist/ 失败：%s" % exc, flush=True)
    else:
        print("产物: 没找到 exe", flush=True)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--probe" in argv:
        return build_probe(argv)
    cmd = build_cmd(standalone="--standalone" in argv)

    # ⚠ 本机没装 MSVC，Nuitka 会自己往私有 pip 空间里装 ziglang（约 99 MB）。
    # 走 PyPI 实测会卡死在 32 MB 不动（2026-10-22 卡了 25 分钟），换清华镜像
    # 8 秒下完。这里只在用户没自己指定镜像时兜底，不覆盖已有设置。
    os.environ.setdefault("PIP_INDEX_URL",
                          "https://pypi.tuna.tsinghua.edu.cn/simple")
    os.environ.setdefault("PIP_DEFAULT_TIMEOUT", "60")

    print("Nuitka 打包 · %s" % NAME, flush=True)
    print("输出目录: %s" % OUT_DIR, flush=True)
    print(" ".join('"%s"' % c if " " in c else c for c in cmd), flush=True)
    if "--dry" in argv:
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = __import__("time").time()
    rc = subprocess.call(cmd, cwd=ROOT)
    dt = __import__("time").time() - t0
    print("\n退出码 %d，耗时 %.1f 分钟" % (rc, dt / 60.0), flush=True)
    if rc != 0:
        return rc

    exe = os.path.join(OUT_DIR, "%s.exe" % NAME)
    if os.path.isfile(exe):
        print("产物: %s（%.1f MB）" % (exe, os.path.getsize(exe) / 1048576.0),
              flush=True)
    else:
        cand = glob.glob(os.path.join(OUT_DIR, "**", "%s.exe" % NAME), recursive=True)
        print("产物: %s" % (cand[0] if cand else "没找到 exe"), flush=True)

    # BIOS 固化目录：**不**打进 exe，而是复制到 exe 旁边。
    #
    # 为什么不塞进 onefile：
    #   * 版权 —— BIOS 是索尼的文件，不该混进可执行体里被随手转发；
    #   * 体积 —— onefile 每次启动都要把内容解包到 %TEMP%，+4 MB 白花时间。
    #
    # 为什么不留在源码目录：运行时 ``app_dir()`` 指向 **exe 所在目录**
    # （不是源码目录），``find_bios()`` 第一项扫的就是 ``<app_dir>/bios``。
    # 复制过去之后，用户把 exe 和 bios/ 一起拷走，换机、换版本都不用再指定
    # （见 README ⚠45）。
    #
    # ⚠ 目标目录要取 **exe 实际所在目录**，不能直接用 OUT_DIR：
    #   ``--standalone`` 时 exe 在 ``<OUT_DIR>/msu1_web.dist/`` 里，
    #   放 OUT_DIR 就等于放到了上一级 —— 运行时扫不到，白带（实测踩过）。
    bios_src = os.path.join(ROOT, "bios")
    exe_dir = (os.path.dirname(exe) if os.path.isfile(exe) else OUT_DIR)
    if os.path.isdir(bios_src):
        bios_dst = os.path.join(exe_dir, "bios")
        try:
            shutil.copytree(bios_src, bios_dst, dirs_exist_ok=True)
            n = len([x for x in os.listdir(bios_dst)
                     if x.lower().endswith(".bin")])
            print("BIOS: 已带 %d 份到 %s（运行时自动找到，不用再指定）"
                  % (n, bios_dst), flush=True)
        except OSError as exc:               # noqa: BLE001
            print("⚠ 复制 bios/ 到产物目录失败：%s" % exc, flush=True)
    else:
        print("提示: 源码目录没有 bios/ —— 打包后首次运行需指定一次 BIOS"
              "（指定后会自动复制到 exe 旁边的 bios/）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
