#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/check_web_exe.py — 对**打包后的 exe** 做一次真实验收。

源码树里跑门禁（``test_web_ui.py``）证明的是「代码对」，证明不了
「打出来的包对」。打包最常见的坑是：模块漏了（惰性 import 静态分析看不到）、
DLL 没带（libgme / ffmpeg）、数据文件路径算错（前端静态文件找不到）。
这些在源码里全都正常，只有 exe 会犯。

所以这里**只碰 exe**：拉起它 → 用 HTTP API 驱动 → 真转一首 → 关掉。

## 怎么「关掉」

外壳是 `exe → Edge --app` 两层。杀 exe 会留下一个孤儿 Edge 窗口，所以要
`taskkill /T`（连带子进程）而不是 `terminate()`。

跑法::

    python vendor/check_web_exe.py
    python vendor/check_web_exe.py --exe <别的路径>
    python vendor/check_web_exe.py --keep      # 跑完不关，留着看
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)          # _txtp_fixture 就在本目录

import tmpdir                                                   # noqa: E402
tmpdir.init(sweep_old=False)

#: 正式产物是 ``build_web_pyi.py``（PyInstaller，约 62 MB）的 ``-pyi`` 目录。
#: ``build/MSU1Converter-web`` 是 ``build_web.py``（Nuitka）的旧线，只作兜底 ——
#: 之前 DEFAULT_EXE 只写了 Nuitka 那条，正式线打完包直接跑门禁会报「找不到 exe」。
_EXE_CANDIDATES = (
    os.path.join(ROOT, "build", "RetroMusicLab-pyi", "RetroMusicLab.exe"),
    os.path.join(ROOT, "build", "MSU1Converter-web-pyi", "MSU1Converter-web.exe"),
    os.path.join(ROOT, "build", "MSU1Converter-web", "MSU1Converter-web.exe"),
    os.path.join(ROOT, "dist", "MSU1Converter-web.exe"),
)
DEFAULT_EXE = next((p for p in _EXE_CANDIDATES if os.path.isfile(p)),
                   _EXE_CANDIDATES[0])
SPC = os.path.join(ROOT, "vendor", "sample.spc")

#: PSF 的真样本（可选）。PSF 存的是 PS1 的内存快照，**解码还要一份 BIOS**，
#: 两样都是用户自己机器上的东西、都不进仓库 —— 所以只能由环境变量指过来：
#:
#:     set MSU1_PSF_SAMPLE=D:\samples\foo.psf
#:
#: 不给就跳过「真解一首」，其余 PSF 检查照跑（解码器在不在包里才是最常犯的错）。
PSF_SAMPLE = os.environ.get("MSU1_PSF_SAMPLE", "").strip()

#: WSR 真样本（192 KB，在仓库里）。用来验「打包后 wsr2wav.exe 真能跑」——
#: 曲目数是**实测扫描**出来的，所以这一条等价于「整条 WSR 链路在包里活着」。
WSR_SAMPLE = os.path.join(ROOT, "vendor", "samples", "wsr", "SWJ-BANC3C.wsr")

BASE = "http://127.0.0.1:47913"      # 与 web_ui/shell.py 的 FIXED_PORT 一致

_PASS = 0
_FAIL = 0


def _mark(cond, label, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [OK] %s" % label, flush=True)
    else:
        _FAIL += 1
        print("  [!!] %s%s" % (label, ("  → " + str(detail)) if detail else ""), flush=True)
    return bool(cond)


def _skip(label, detail=""):
    """跳过一项（不算失败）。只给「素材/BIOS 在用户机器上」这类客观缺料用。"""
    print("  [--] %s%s" % (label, ("  → " + str(detail)) if detail else ""), flush=True)


def _wav_peak(path):
    """读 16bit PCM WAV 的最大绝对值。

    全 0 = 「解码器说成功、其实什么都没出」，是 PSF 这条链路最典型的假成功
    （HLE 桩凑不齐环境时就是这样），所以必须查，不能只看文件在不在。
    """
    import struct
    with open(path, "rb") as f:
        data = f.read()
    pos, pcm = 12, b""
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if cid == b"data":
            pcm = data[pos + 8:pos + 8 + size]
            break
        pos += 8 + size + (size & 1)
    if not pcm:
        return 0
    n = len(pcm) // 2
    return max(abs(v) for v in struct.unpack("<%dh" % n, pcm[:n * 2]))


def _req(path, body=None, timeout=15):
    data = None
    hdrs = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:                                            # noqa: BLE001
        return 0, b""


def _jget(path, timeout=15):
    code, raw = _req(path, None, timeout)
    try:
        return code, json.loads(raw.decode("utf-8"))
    except Exception:                                            # noqa: BLE001
        return code, {}


def _jpost(path, body=None, timeout=15):
    code, raw = _req(path, body if body is not None else {}, timeout)
    try:
        return code, json.loads(raw.decode("utf-8"))
    except Exception:                                            # noqa: BLE001
        return code, {}


def _wait(cond, timeout, step=0.15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


def kill_tree(pid):
    """连同 Edge 子进程一起收掉。"""
    subprocess.call(["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _dpi_aware():
    """让本进程按**物理像素**量窗口，别被 DPI 虚拟化骗了。

    这台机器是 1920×1080 @150%，Python 进程默认是 DPI 不感知的，于是
    ``GetWindowRect`` 返回的是**除以 1.5 的虚拟坐标**：一个最大化到
    1920×1106 的窗口会被量成 1280×737 —— 于是「窗口尺寸 ≥780」这条断言
    在窗口明明正常的时候红了。真凶不是窗口，是尺子。
    """
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # PROCESS_SYSTEM_DPI_AWARE
        return True
    except Exception:                                    # noqa: BLE001 老系统
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            return True
        except Exception:                                # noqa: BLE001
            return False


_DPI_DONE = False


def app_windows(title_part="RetroMusic Lab"):
    """枚举标题含 ``title_part`` 的可见顶层窗口，返回 ``[(标题, 宽, 高)]``。

    为什么要这个：以前这套门禁**只打 HTTP，从不开窗口**。于是「窗口到底有没有
    出来、标题对不对、是不是最小化」从来没被验过。实测踩到的坑：页面还没加载完时
    窗口标题是 ``127.0.0.1_/``（URL 的 host），加载失败就一直停在那儿 ——
    用户看到的就是一个标题「127.0.0.1」的窗口，完全不知道它是我们的。
    """
    global _DPI_DONE
    import ctypes
    import ctypes.wintypes as wt
    if not _DPI_DONE:
        _dpi_aware()
        _DPI_DONE = True
    u = ctypes.windll.user32
    out = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def _cb(h, _l):
        if u.IsWindowVisible(h):
            n = u.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, b, n + 1)
                if title_part in b.value:
                    r = wt.RECT()
                    u.GetWindowRect(h, ctypes.byref(r))
                    out.append((b.value, r.right - r.left, r.bottom - r.top))
        return True

    u.EnumWindows(CB(_cb), 0)
    return out


def work_area():
    """屏幕可用区（物理像素）。窗口尺寸要跟它比，不能跟写死的数字比。"""
    global _DPI_DONE
    import ctypes
    import ctypes.wintypes as wt
    if not _DPI_DONE:
        _dpi_aware()
        _DPI_DONE = True
    r = wt.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
    return (r.right - r.left, r.bottom - r.top)


def wait_windows(timeout=45.0):
    """等应用窗口出现，返回 ``[(标题, 宽, 高)]``（超时返回空）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hits = app_windows()
        if hits:
            return hits
        time.sleep(0.25)
    return []


#: 打包时会跟出去的本地二进制。**任何一个带 AVX 都不行** —— 见 README ⚠51。
#:
#: ``psf2wav.exe`` 曾经用 ``zig cc`` 默认参数编译（= 按编译机的 CPU 出码），
#: 在带 AVX-512 的机器上编出来的包，换到老 CPU 上第一条指令就
#: ``0xC000001D``，而界面把它报成「解码器拒绝了这份 BIOS」—— 用户去查
#: BIOS 查了半天，其实是解码器自己跑不起来。这种回归必须被门禁挡住。
_LOCAL_BINS = ("psf2wav.exe", "libgme.dll", "zlib1.dll",
               # v5.9.10：QSF 的解码器，同样是 ``zig cc`` 编的 ——
               # 和 psf2wav.exe 一样必须验「不含 AVX」。
               "qsf2wav.exe",
               "libstdc++-6.dll", "libgcc_s_seh-1.dll",
               "libwinpthread-1.dll")

#: ⚠ ``vgmstream-cli.exe`` 和那 11 个 DLL **故意不进上面的 AVX 清单**。
#: 实测扫出来 ``vgmstream-cli.exe`` 有 6 处 ``vzeroupper``、
#: ``avcodec-vgmstream-59.dll`` 有 35 处 —— 都远低于「确认有 AVX」的阈值
#: （实测带 AVX 的二进制是 72 处），说明那是 FFmpeg 的**运行时派发**：
#: 先 CPUID 再决定走不走 AVX 分支，老 CPU 自动退回 SSE。
#: 而且这是第三方预编译产物，真有问题我们也改不了 —— 把它放进清单只会
#: 制造一条永远红着的假告警。真要在老 CPU 上验，只能跑一次真解码。

#: AVX 函数几乎都会在出口插一条 ``vzeroupper``（字节 ``C5 F8 77``）。
#: 这个三字节序列在 250 KB 代码段里随机出现期望值约 0.015 次 ——
#: 实测到 72 次就是铁证，0 次就是没有 AVX。
#:
#: ⚠ 别用「扫 EVEX 前缀（0x62）」当判据：立即数里偶然的 0x62 会被当成
#:   前缀，连纯 baseline 的 libstdc++ 都能扫出上百个假阳性（实测）。
_VZEROUPPER = b"\xc5\xf8\x77"


def _code_sections(path):
    """yield 每个可执行段的字节 —— 只扫 .text，别把数据段算进来。"""
    import struct
    with open(path, "rb") as f:
        d = f.read()
    if d[:2] != b"MZ":
        return
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    if d[pe:pe + 4] != b"PE\0\0":
        return
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    optsz = struct.unpack_from("<H", d, pe + 20)[0]
    base = pe + 24 + optsz
    for i in range(nsec):
        off = base + i * 40
        vsize, _vaddr, rawsize, rawptr = struct.unpack_from("<IIII", d, off + 8)
        chars = struct.unpack_from("<I", d, off + 36)[0]
        if chars & 0x20000000:                # IMAGE_SCN_MEM_EXECUTE
            yield d[rawptr:rawptr + (min(rawsize, vsize) or rawsize)]


def _check_no_avx():
    """本地二进制里不能有 AVX —— 否则换台老 CPU 就 0xC000001D。"""
    print("  本地二进制指令集（带 AVX 的话，老 CPU 一启动就挂）", flush=True)
    for name in _LOCAL_BINS:
        p = os.path.join(ROOT, name)
        if not os.path.isfile(p):
            continue
        n = sum(code.count(_VZEROUPPER) for code in _code_sections(p))
        _mark(n == 0, "%s 不含 AVX（vzeroupper=%d）" % (name, n),
              "重编时加 -mcpu=baseline，见 README ⚠51")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    exe = DEFAULT_EXE
    if "--exe" in argv:
        exe = argv[argv.index("--exe") + 1]
        # ⚠ 必须转绝对路径。传相对路径时子进程里的 ``sys.executable`` 也是
        #   相对的，``app_dir()`` 再 abspath 一次就相对到了**子进程的工作目录**，
        #   bios_home 会被拼成 ``<dir>/build/xxx/bios`` 这种多一层的假路径，
        #   门禁报「bios_home 不对」—— 而产物其实完全正常（实测踩过）。
        exe = os.path.abspath(exe)

    print("Web 版 exe 验收", flush=True)
    if not os.path.isfile(exe):
        print("找不到 exe: %s" % exe, flush=True)
        return 2
    size_mb = os.path.getsize(exe) / 1048576.0
    print("  exe: %s（%.1f MB）" % (exe, size_mb), flush=True)
    _mark(size_mb > 20, "体积合理（>20 MB，说明依赖真的打进去了）", "%.1f MB" % size_mb)

    _check_no_avx()

    # 端口必须是空的，否则连到的是上一轮残留
    code, _ = _jget("/api/state", timeout=1)
    if code:
        print("端口 47913 已被占用，先关掉在跑的实例", flush=True)
        return 2

    t0 = time.time()
    # PSF 的 BIOS：exe 里不会有 psf_bios.txt（那是每台机器自己的路径），
    # 所以从源码树问一句，再用环境变量喂给子进程。不给也不会让门禁失败 ——
    # 「解码器在不在包里」才是打包才会犯的错，BIOS 属于运行环境。
    psf_bios = ""
    try:
        import psf_native
        psf_bios = psf_native.find_bios()[0]
    except Exception:                                            # noqa: BLE001
        psf_bios = ""
    child_env = os.environ.copy()
    # ⚠ PyInstaller onefile 每次启动都会把 ~136MB 解包到 `%TEMP%\_MEIxxxxx`，
    #   正常退出会自己删，但**被强杀 / 崩溃**就留在那儿。反复跑门禁能攒出
    #   几十个（实测 47 个 = 6.2 GB），直接把系统盘塞满。
    #   把 TEMP 指到项目所在的盘，至少不污染 C 盘；而且同一个目录方便一次性清。
    _tmp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "_pyi_unpack_tmp")
    try:
        os.makedirs(_tmp, exist_ok=True)
        child_env["TEMP"] = _tmp
        child_env["TMP"] = _tmp
    except OSError:
        pass
    if psf_bios:
        child_env["MSU1_PSF_BIOS"] = psf_bios
        print("  PSF BIOS: %s" % psf_bios, flush=True)

    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=child_env)
    print("  已拉起 pid=%d，计时开始…" % proc.pid, flush=True)

    try:
        # ---- 启动时间 ----
        ok = _wait(lambda: _jget("/api/state", timeout=2)[0] == 200, 180)
        t_api = time.time() - t0
        _mark(ok, "后端在 180 秒内起来", "%.1f s" % t_api)
        if not ok:
            return 1
        print("     后端就绪 %.2f s" % t_api, flush=True)

        # ---- 窗口是不是真的出来了（以前完全没验）----
        wins = wait_windows(45)
        _mark(bool(wins), "应用窗口真的出来了", wins)
        if wins:
            title, ww, hh = wins[0]
            _mark(title.startswith("RetroMusic Lab"),
                  "窗口标题是页面标题（不是 127.0.0.1）", title)
            # 跟**屏幕可用区**比，不跟写死的 1200×780 比：
            # 小屏笔记本上写死的数字必然误报，而这里真正要守的是
            # 「窗口真出来了、不是最小化、不是被压成一条」。
            wa_w, wa_h = work_area()
            _mark(ww >= min(1000, wa_w * 0.8) and hh >= min(600, wa_h * 0.6),
                  "窗口尺寸正常（不是最小化 / 不是一条缝）",
                  "%dx%d，屏幕可用区 %dx%d" % (ww, hh, wa_w, wa_h))
            _mark(len(wins) == 1,
                  "只有一个应用窗口（没被会话恢复的旧窗口抢前台）", len(wins))
        else:
            _mark(False, "窗口标题", "没窗口")
            _mark(False, "窗口尺寸", "没窗口")
            _mark(False, "窗口唯一性", "没窗口")

        # ---- 静态文件真的在包里 ----
        code, raw = _req("/")
        _mark(code == 200 and b"RetroMusic Lab" in raw,
              "index.html 打进包里了", "%s / %d B" % (code, len(raw)))
        code, raw = _req("/app.css")
        _mark(code == 200 and len(raw) > 5000, "app.css 打进包里了",
              "%s / %d B" % (code, len(raw)))
        code, raw = _req("/app.js")
        _mark(code == 200 and len(raw) > 5000, "app.js 打进包里了",
              "%s / %d B" % (code, len(raw)))

        # v5.9.6：频谱面板必须**真的**跟着 exe 走，而且**两页都要**。
        # 「app.js 有内容」查不出漏打包 —— WEB_STATIC 里 index.html / app.js
        # 是两条独立记录，漏一条界面照样能开，只是右栏空一块。
        # 实现抽到 viz.js 后多一层风险：viz.js 漏进包 → 两个页面同时
        # 报 "VizKit is not defined"，音频区整块失效，而 index.html 看起来
        # 完全正常。所以 viz.js 必须单独验。
        _c3, _h3 = _req("/viz.js")
        _mark(_c3 == 200 and b"createMediaElementSource" in _h3
              and b"VizKit" in _h3,
              "viz.js 打进包里了（两页共用，漏了就是 VizKit is not defined）",
              "%s / %d B" % (_c3, len(_h3)))
        _c4, _h4 = _req("/app.js")
        _mark(b"VizKit.create(" in _h4,
              "app.js 走 VizKit（不再是内联的那份）", "%d B" % len(_h4))
        _c2, _h2 = _req("/")
        _mark(b"vizCanvas" in _h2 and b"spMax" not in _h2,
              "index.html 是 v5.9.6 的（有 vizCanvas、没有 spMax）",
              "%d B" % len(_h2))
        _c5, _h5 = _req("/download.html")
        _mark(_c5 == 200 and b"dlVizCanvas" in _h5,
              "download.html 有下载页频谱面板（dlVizCanvas）",
              "%s / %d B" % (_c5, len(_h5)))
        _c6, _h6 = _req("/download.js")
        _mark(b"VizKit.create(" in _h6 and b"viz.init()" in _h6,
              "download.js 也接了 VizKit（试听区同样有频谱）",
              "%d B" % len(_h6))

        # ---- v5.9.7：FLAC 输出必须真的能用，不能只是「界面有按钮」 ----
        # 这是最容易漏打包的一类：flac_out 是 spc_wav.write_tracks 里的
        # 函数内 import，PyInstaller 的静态分析本来就容易看不到；漏了的
        # 表现极其隐蔽 —— 界面正常、选 WAV 正常，一选 FLAC 整批报
        # 「FLAC 编码器不可用：ModuleNotFoundError」。
        # 所以这里不看文件在不在，直接问 diag 要**运行时**结论。
        _cd, _dd = _jget("/api/diag")
        _fl = (_dd.get("flac") or {}) if isinstance(_dd, dict) else {}
        _mark(bool(_fl.get("ok")),
              "exe 里 FLAC 编码器真的可用（不是只有按钮）",
              str(_fl.get("why"))[:60])
        _mark(bool(((_dd.get("modules") or {}).get("flac_out"))
                   if isinstance(_dd, dict) else False),
              "flac_out 模块打进包里了", "")
        _mark(b'data-fmt="flac"' in _h2,
              "index.html 有 FLAC 输出卡片", "%d B" % len(_h2))
        # ---- v5.9.15：MP3 同样要「运行时可用」，不是只有按钮 ----
        # 走的是同一个 libsndfile（自带 LAME），所以理论上 FLAC 能用 MP3 就能用；
        # 但打包时 `format='MP3'` 依赖 libsndfile 编进了 LAME —— 真漏了的话
        # 症状是「选 MP3 整批报 MP3 编码器不可用」，比 FLAC 更隐蔽（没人会想到
        # 无损能编、有损不能编）。这里直接问 diag 要运行时结论。
        _mp = (_dd.get("mp3") or {}) if isinstance(_dd, dict) else {}
        _mark(bool(_mp.get("ok")),
              "exe 里 MP3 编码器真的可用（libsndfile 带 LAME）",
              str(_mp.get("why"))[:60])
        _mark(b'data-fmt="mp3"' in _h2,
              "index.html 有 MP3 输出卡片", "%d B" % len(_h2))

        # ---- v5.9.18：MP3 音质修复依赖的 libsndfile 版本 ----
        # 修「持续杂音」靠两件事：① 自己把 int16 归一化成 float32；
        # ② `bitrate_mode="CONSTANT"` 才拿得到 320 kbps CBR。
        # ② 是 libsndfile **1.2.0** 才有的接口 —— 打包时带了个 1.1.x 的
        # DLL 就会静默降级成 VBR（音质仍对，但码率不对，说明书对不上）。
        # 这里直接读 exe 自报的版本，跟源码环境比一比。
        _why_mp = str(_mp.get("why") or "")
        try:
            import re as _re2
            _mv = _re2.search(r"libsndfile\s+([0-9.]+)", _why_mp)
            _exe_ver = _mv.group(1) if _mv else ""
        except Exception:                            # noqa: BLE001
            _exe_ver = ""
        try:
            import soundfile as _sf2
            _src_ver = str(getattr(_sf2, "__libsndfile_version__", ""))
        except Exception:                            # noqa: BLE001
            _src_ver = ""
        _mark(bool(_exe_ver),
              "exe 自报了 libsndfile 版本（能判 CBR 是否真生效）",
              _why_mp[:60] or "?")
        _mark(bool(_exe_ver) and _exe_ver.split(".")[:2] >= ["1", "2"],
              "exe 里 libsndfile ≥ 1.2（bitrate_mode 才支持，否则只有 VBR）",
              _exe_ver or "?")
        _mark(not _src_ver or not _exe_ver or _exe_ver == _src_ver,
              "exe 里的 libsndfile 与源码环境同版本（打包没换 DLL）",
              "exe %s / 源码 %s" % (_exe_ver or "?", _src_ver or "?"))

        # ---- v5.9.16：版本号三处一致 ----
        # 打包时最容易漏的就是这个：`build_web_pyi.VER` 改了、`server.APP_VERSION`
        # 忘了改，于是「exe 文件名说 5.9.16、接口自报 5.9.15」—— 用户报 bug 时
        # 说的版本号是错的，排查方向直接跑偏。这里以源码的 APP_VERSION 为准，
        # 要求接口自报值和页面顶栏都跟它一致。
        try:
            import re as _re
            _sv = open(os.path.join(ROOT, "web_ui", "server.py"),
                       encoding="utf-8").read()
            _want = _re.search(r'APP_VERSION = "([^"]+)"', _sv).group(1)
        except Exception:                            # noqa: BLE001
            _want = ""
        _mark(bool(_want) and (_dd or {}).get("version") == _want,
              "exe 接口自报版本与源码一致（%s）" % (_want or "?"),
              str((_dd or {}).get("version")))
        _mark(bool(_want) and ('brand-ver">%s' % _want).encode() in _h2,
              "页面顶栏版本号也一致（不会「exe 新、界面旧」）",
              _want or "?")
        _mark(b"dlConvMp3" in _h5,
              "download.html 有「转 MP3」按钮", "%d B" % len(_h5))
        # v5.9.9：两个「下载后自动转」复选框已按用户要求删掉，换成工具条上
        # 两颗独立按钮。这里跟着改断言 —— 验的是「按钮在」，不再验复选框。
        _mark(b"dlConvWav" in _h5 and b"dlConvFlac" in _h5,
              "download.html 有「转 WAV」「转 FLAC」两颗按钮",
              "%d B" % len(_h5))
        _mark(b"dlAutoFlac" not in _h5 and b"dlAutoWav" not in _h5,
              "旧的「下载后自动转」复选框确实没了", "%d B" % len(_h5))
        _mark(b"fmt: fmt" in _h6,
              "download.js 点按钮时把 fmt 传给后端", "%d B" % len(_h6))
        code, _ = _req("/server.py")
        _mark(code == 404, "源码没被暴露（server.py → 404）", code)

        # ---- 换机依赖审计：新加的退路必须真的在包里 ----
        #
        # 这几条都是「开发机上永远正常、换台电脑才炸」的形态，所以打包后
        # 必须逐个确认它们跟着 exe 走了 —— 端点在内、退路代码在内。
        code, d = _jget("/api/whoami")
        _mark(code == 200 and d.get("app") == "RetroMusic Lab",
              "GET /api/whoami 在包里（端口占用时靠它分辨是不是自己人）",
              "%s / %s" % (code, d))
        # ⚠ 这里**不能**跟 proc.pid 比：PyInstaller onefile 是两段进程 ——
        #   bootloader（proc.pid）解包后另起一个子进程跑 Python，os.getpid()
        #   拿到的是子进程的 pid。比下去必然不等（实测 58252 vs 35600）。
        _mark(isinstance(d.get("pid"), int) and d["pid"] > 0,
              "/api/whoami 带回了真实 pid", d.get("pid"))

        try:
            with open(os.path.join(ROOT, "web_ui", "shell.py"),
                      "r", encoding="utf-8") as f:
                sh_src = f.read()
        except OSError:
            sh_src = ""
        _mark("def port_is_ours(" in sh_src,
              "shell 有 port_is_ours()（分辨端口占用者）")
        _mark("not port_is_ours(want)" in sh_src,
              "端口被**别人**占用时换随机端口，而不是判死")

        try:
            with open(os.path.join(ROOT, "joshw_dl.py"),
                      "r", encoding="utf-8") as f:
                dl_src = f.read()
        except OSError:
            dl_src = ""
        _mark("def _extract_zip_stdlib(" in dl_src,
              "有 zipfile 退路（没有 tar.exe 时 .zip 也能解）")
        _mark("_extract_with_py7zr(archive, target, cancel, timeout)"
              in dl_src,
              "bsdtar 跑挂了也会换备用解压器（不只是「文件不存在」时）")

        # ---- 流式音频：.adx（PyAV 转手）/ .mod（自写回放器）必须真的在包里 ----
        #
        # 这一条查的是**打进 PYZ 的字节码**，不是源码树 —— 加模块时最容易
        # 犯的错就是「源码跑得通、打包被 nofollow 掉了」，运行时才 ImportError。
        # ``stream_audio`` 里对 ``mod_audio`` 是**函数内 import**，所以额外
        # 直接查 ``mod_audio`` 这个模块本身在不在。
        try:
            from PyInstaller.archive.readers import CArchiveReader
            import types as _types
            _arc = CArchiveReader(os.path.abspath(exe))
            _pyz = _arc.open_embedded_archive("PYZ.pyz")
            _names = set()

            def _walk(c):
                _names.update(c.co_names)
                for k in c.co_consts:
                    if isinstance(k, _types.CodeType):
                        _walk(k)

            _walk(_pyz.extract("stream_audio"))
            has_stream = {"read_hwas_header", "convert_one",
                          "kind_of"} <= _names
            _mod_names = set()

            def _walk2(c):
                _mod_names.update(c.co_names)
                for k in c.co_consts:
                    if isinstance(k, _types.CodeType):
                        _walk2(k)

            _walk2(_pyz.extract("mod_audio"))
            has_mod = {"parse", "render", "probe", "_run"} <= _mod_names
        except Exception as exc:                     # noqa: BLE001
            has_stream = False
            has_mod = False
            print("  （读 PYZ 失败：%s）" % exc, flush=True)
        _mark(has_stream, "stream_audio 打进包了（.adx/.mod 分派在）")
        _mark(has_mod, "mod_audio 打进包了（MOD 回放器在，函数内 import 没被漏掉）")

        # ---- ctypes 签名：漏声明 restype 会截断 64 位指针 ----
        #
        # 这条是静态检查（读源码文本），因为它防的是「以后有人加了个新 API
        # 忘了写 restype」。运行时是查不出来的：截断后崩不崩取决于当时堆
        # 地址的高 32 位是否恰好为 0，很多机器上是偶发的。
        #
        # 事故原型：SHBrowseForFolderW 返回 PIDLIST_ABSOLUTE（堆指针），
        # 没声明 restype 时 ctypes 按 c_int 返回 → 42 位的 0x1550f1e57c0
        # 被截成 0xf1e57c0 → 再传给 SHGetPathFromIDListW 就
        # "access violation reading 0x000000000F1E57C0"，
        # 用户看到的是一点「添加文件夹」就报错。
        try:
            with open(os.path.join(ROOT, "web_ui", "native_dialog.py"),
                      "r", encoding="utf-8") as f:
                nd_src = f.read()
        except OSError:
            nd_src = ""
        for fn_name in ("SHBrowseForFolderW", "SHGetPathFromIDListW",
                        "GetForegroundWindow", "GetOpenFileNameW"):
            _mark("%s.restype" % fn_name in nd_src,
                  "native_dialog 给 %s 声明了 restype" % fn_name,
                  "漏了会在 64 位下截断指针 → 访问违例")

        # ---- 打包漏件的第一手证据 ----
        code, d = _jget("/api/diag")
        _mark(code == 200 and d.get("ok"), "GET /api/diag", code)
        mods = d.get("modules") or {}
        _mark(bool(mods) and all(mods.values()), "打包后依赖全部可用",
              [k for k, v in mods.items() if not v])
        _mark(d.get("libgme") == "ok", "libgme.dll 能加载", d.get("libgme"))
        _mark(d.get("frozen") is True, "frozen=True（走的是打包分支）",
              d.get("frozen"))
        psfd = d.get("psf") or {}
        _mark(bool(psfd.get("decoder")), "主 diag：psf2wav.exe 在包里",
              psfd.get("decoder_path") or psfd)

        # ---- WSR：wsr2wav.exe 是不是也带上了 ----
        # 这是最典型的坏包形态：wsr_native.py 打进去了（PYZ 里的纯 py），
        # 但 wsr2wav.exe 是个普通文件、靠 EXTRA_EXE 才带 —— 忘了加就表现成
        # 「.wsr 加得进来、一按转换说找不到解码器」。diag 分开报就是为了这个。
        wsrd = d.get("wsr") or {}
        _mark(bool(wsrd.get("decoder")), "主 diag：wsr2wav.exe 在包里",
              wsrd.get("decoder_path") or wsrd)
        # 加一个真 .wsr 进去：证明「扩展名识别 + 归成芯片音源」这条在包里通。
        # ⚠ 这里**不**做整包转换 —— 122 首 × 150 秒 = 3.2 GB，验收跑不起。
        #   真解码的逐样本验证在 vendor/test_wsr.py（源码树，27 项）。
        #   打包态最容易出错的「wsr2wav.exe 路径算不对」由上面那条 diag 覆盖：
        #   frozen 下 find_decoder() 走 sys._MEIPASS，decoder_path 会指到
        #   _MEIxxxx 解包目录，不是源码树。
        if os.path.isfile(WSR_SAMPLE):
            _jpost("/api/clear", {})
            code, d2 = _jpost("/api/add_sources", {"paths": [WSR_SAMPLE]})
            _mark(code == 200 and d2.get("added") == 1, "加 .wsr 音源", d2)
            code, st2 = _jget("/api/state")
            srcs2 = st2.get("sources") or []
            _mark(bool(srcs2) and srcs2[0].get("kind") == "芯片音源"
                  and int(srcs2[0].get("size") or 0) == 196608,
                  "归成芯片音源、大小对", srcs2[:1])
            _jpost("/api/clear", {})
        else:
            _skip("真加一个 .wsr", "缺样本 %s" % WSR_SAMPLE)

        # ---- KSS：kss2wav.exe 是不是也带上了（v5.8） ----
        # 和 WSR 同一个坑：kss_native.py 进了 PYZ，kss2wav.exe 靠 EXTRA_EXE。
        # 少了的表现是「.kss 加得进来、一按转换说找不到解码器」。
        # ⚠ 这里**不**跑真解码：要 32 KB 的真实 GG KSS 才看得出差别
        #   （16 KB 的 libgme 自己也能解，测了等于没测）。真解码在
        #   vendor/_e2e_kss_dl.py，用下载页真下的包。
        kssd = d.get("kss") or {}
        _mark(bool(kssd.get("decoder")), "主 diag：kss2wav.exe 在包里",
              kssd.get("decoder_path") or kssd)
        _mark(bool(mods.get("kss_native")),
              "kss_native 模块在包里（__import__ 动态加载，HIDDEN 漏了就缺）",
              mods.get("kss_native"))

        # ---- QSF：qsf2wav.exe 是不是也带上了（v5.9.10） ----
        # 同一个坑的第三次复现：qsf_native.py 进 PYZ，qsf2wav.exe 靠 EXTRA_EXE。
        # ⚠ 这条 diag 和 PSF 的区别是**没有 bios 字段** —— QSound 是街机板上的
        #   独立声音子系统，没有主机 BIOS。若哪天有人在 qsf 段里塞了 bios，
        #   说明有人照抄了 psf 的结构，那是个概念性错误，这里顺手挡住。
        qsfd = d.get("qsf") or {}
        _mark(bool(qsfd.get("decoder")), "主 diag：qsf2wav.exe 在包里",
              qsfd.get("decoder_path") or qsfd)
        _mark(bool(mods.get("qsf_native")),
              "qsf_native 模块在包里（延迟 import，HIDDEN 漏了就缺）",
              mods.get("qsf_native"))
        _mark("bios" not in qsfd,
              "qsf 段里没有 bios 字段（QSound 不需要 BIOS）", sorted(qsfd))

        # ---- Pivotal PSF（GameCube）：**没有外挂 exe**，只看模块（v5.9.11） ----
        # 这条和上面几段都不一样：pivotal_psf.py 是纯 Python，不需要 .exe、
        # 不需要 BIOS。它要防的是**延迟 import 被静态分析漏掉** —— 三个调用点
        # （convert_job / dl_convert / psx_txtp）都是先判魔数再 import，
        # PyInstaller 看不到，漏了的表现是「.psf 一转换就 ModuleNotFoundError」。
        # ⚠ 顺带挡住一个概念性错误：这一段**不该有 decoder/bios 字段**，
        #   有人照抄 psf 的结构就说明他以为这条路也要外挂解码器。
        pvd = d.get("pivotal") or {}
        _mark(bool(mods.get("pivotal_psf")),
              "pivotal_psf 模块在包里（三处延迟 import，HIDDEN 漏了就缺）",
              mods.get("pivotal_psf"))
        _mark(bool(pvd.get("ok")), "主 diag：pivotal 段可用",
              pvd.get("error") or pvd)
        _mark(int(pvd.get("rate") or 0) == 44100,
              "pivotal 出厂采样率是 44100", pvd.get("rate"))
        _mark("bios" not in pvd,
              "pivotal 段里没有 bios 字段（Pivotal 是预录流，没有快照）",
              sorted(pvd))

        # ---- VGM：Wwise / FMOD（v5.9.12）----
        # 这一条是**全工程唯一随包带第三方解码器**的链路，也是最脆的一条：
        # ``vgmstream-cli.exe`` 旁边必须有 **11 个 DLL**，而且它们是
        # **载入时**硬依赖（不是用到才加载）。实测只拷 exe + libvorbis.dll
        # 报 ``libspeex-1.dll``；补到 8 个（想省掉 FFmpeg 那 4 个约 6MB）
        # 仍报 ``avutil-vgmstream-57.dll`` —— 一个都砍不掉。
        # 缺任何一个的表现都一样：「.wem / .fsb 一转换就说找不到解码器」，
        # 界面上看不出少的是哪一环，所以必须在这里点名验。
        vgd = d.get("vgm") or {}
        _mark(bool(vgd.get("ok")), "主 diag：vgm 段可用（exe + 11 个 DLL 齐）",
              vgd.get("why") or vgd.get("error") or vgd)
        _mark(os.path.basename(str(vgd.get("decoder") or ""))
              == "vgmstream-cli.exe",
              "vgm 段的解码器是 vgmstream-cli.exe", vgd.get("decoder"))
        _mark(bool(mods.get("vgm_audio")),
              "vgm_audio 模块在包里（两处延迟 import，HIDDEN 漏了就缺）",
              mods.get("vgm_audio"))
        _mark(sorted(vgd.get("exts") or []) == [".fsb", ".wem"],
              "vgm 段报的 exts 是 ['.fsb', '.wem']", vgd.get("exts"))

        # ---- S98：s982wav.exe 是不是也带上了（v5.9.13） ----
        # 和 WSR / KSS / QSF 同一个坑：s98_native.py 进了 PYZ，s982wav.exe
        # 靠 EXTRA_EXE 才带。少了的表现是「.s98 加得进来、一按转换说找不到
        # 解码器」。
        #
        # ⚠ 这一段**不能省成「和 vgm 段一样」**：ymfm 和 vgmstream 是两个
        #   完全独立的第三方解码器，没有任何共用文件 —— 只验 vgm 段会漏掉
        #   「vgmstream 在、s982wav 不在」这种包（.wem 能转、.s98 转不了）。
        s98d = d.get("s98") or {}
        _mark(bool(s98d.get("ok")),
              "主 diag：s98 段可用（s98_native + s982wav.exe 齐）",
              s98d.get("why") or s98d.get("error") or s98d)
        _mark(os.path.basename(str(s98d.get("decoder") or "")) == "s982wav.exe",
              "s98 段的解码器是 s982wav.exe", s98d.get("decoder"))
        _mark(bool(mods.get("s98_native")),
              "s98_native 模块在包里（__import__ 动态加载，HIDDEN 漏了就缺）",
              mods.get("s98_native"))
        _mark(sorted(s98d.get("exts") or []) == [".s98"],
              "s98 段报的 exts 是 ['.s98']", s98d.get("exts"))

        # ---- 打包态**真解码** .s98（合成样本，不依赖外部素材）----
        #
        # 上面 4 条只证明「文件在包里」。真正的坑是「文件在、但跑不起来」
        # —— 症状和漏带文件一模一样：diag 全绿，一按转换就失败。
        #
        # 这里真解一遍，并且**验音高**。合成样本的音高是算出来的已知值
        # （``_s98_fixture.EXPECT_HZ`` = 440.10 Hz），比「有声音就行」可靠得多：
        # 音高错半个八度、或者 SSG/FM 混音比例错了，峰值照样正常。
        import struct as _struct
        import wave as _wave
        from _s98_fixture import EXPECT_HZ, EXPECT_SECS, make_s98
        _jpost("/api/clear", {})
        s98_fx = make_s98(os.path.join(tmpdir.root(), "fixture.s98"))
        code, d = _jpost("/api/add_sources", {"paths": [s98_fx]})
        _mark(code == 200 and d.get("added") == 1, "加合成的 .s98", d)
        code, st = _jget("/api/state")
        _srcs = st.get("sources") or []
        _mark(bool(_srcs) and _srcs[0].get("kind") == "芯片音源",
              "归成芯片音源（和 .spc / .vgm 同一类）",
              [x.get("kind") for x in _srcs])
        _mark(bool(_srcs)
              and abs(float(_srcs[0].get("total_secs") or 0) - EXPECT_SECS) < 0.05,
              "探到精确时长 %.2fs（s98_ticks，不是猜的）" % EXPECT_SECS,
              [x.get("total_secs") for x in _srcs])

        out_s98 = os.path.join(tmpdir.root(), "s98_out")
        _jpost("/api/start", {"opts": {"out_dir": out_s98,
                                       "do_cd": False, "do_wav": True}})
        ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 180)
        code, st = _jget("/api/state")
        res = st.get("result") or {}
        _mark(ok and st.get("state") == "done"
              and int(res.get("ok") or 0) >= 1 and int(res.get("fail") or 0) == 0,
              "打包态真解码 .s98：产出 1 首 WAV、0 失败", res)

        _wavs = ([os.path.join(out_s98, x) for x in sorted(os.listdir(out_s98))
                  if x.lower().endswith(".wav")]
                 if os.path.isdir(out_s98) else [])
        if _wavs:
            with _wave.open(_wavs[0], "rb") as _w:
                _nch, _sw, _rate = _w.getnchannels(), _w.getsampwidth(), _w.getframerate()
                _raw = _w.readframes(_w.getnframes())
            _sm = _struct.unpack("<%dh" % (len(_raw) // 2), _raw)
            _peak = max(abs(v) for v in _sm)
            _mark((_nch, _sw, _rate) == (2, 2, 44100)
                  and abs(len(_sm) / 2.0 / 44100.0
                          - (EXPECT_SECS + 1.5)) < 0.05,
                  "产物 2ch/16bit/44100、%.2f 秒（= %.2f + 余音 1.5）"
                  % (len(_sm) / 2.0 / 44100.0, EXPECT_SECS),
                  "%dch/%dbit/%dHz" % (_nch, _sw * 8, _rate))
            _mark(_peak > 500, "有声音（峰值 %d，不是 libgme 那种精确静音）" % _peak)
            # 过零率估音高：0.3~0.8 秒是稳定段（跳过起音、避开松键）
            _L = _sm[0::2]
            _a, _b = int(0.3 * 44100), int(0.8 * 44100)
            _seg = _L[_a:_b]
            _zc = sum(1 for _i in range(1, len(_seg))
                      if (_seg[_i - 1] < 0) != (_seg[_i] < 0))
            _hz = _zc / 2.0 / ((_b - _a) / 44100.0)
            _mark(abs(_hz - EXPECT_HZ) / EXPECT_HZ < 0.02,
                  "音高 %.1f Hz（期望 %.1f，误差 <2%%）" % (_hz, EXPECT_HZ))
        _jpost("/api/clear", {})

        # ---- 芯片音源：libgme.dll 是否带上了 ----
        out = os.path.join(tmpdir.root(), "webexe_out")
        code, d = _jpost("/api/add_sources", {"paths": [SPC]})
        _mark(code == 200 and d.get("added") == 1, "加 sample.spc", d)
        code, d = _jget("/api/tracks?i=0")
        _mark(bool((d.get("tracks") or [])), "探到曲目（说明 libgme 可用）",
              d.get("note") or d.get("tracks"))

        # ---- 真转一首 ----
        code, d = _jpost("/api/start", {"opts": {"out_dir": out,
                                                 "do_cd": False, "do_wav": True}})
        _mark(code == 200, "启动转换", d)
        ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 240)
        code, st = _jget("/api/state")
        _mark(ok, "转换在 240 秒内结束", st.get("state"))
        _mark(st.get("state") == "done", "state=done", st.get("state"))
        res = st.get("result") or {}
        _mark(int(res.get("ok") or 0) >= 1, "产出 ≥1 首 WAV", res)
        _mark(int(res.get("fail") or 0) == 0, "没有失败", res)

        outs = st.get("outputs") or []
        _mark(bool(outs), "输出已登记", len(outs))
        if outs:
            code, raw = _req("/api/audio?id=%d" % outs[0]["id"], timeout=30)
            _mark(code == 200 and raw[:4] == b"RIFF", "试听返回 RIFF",
                  "%s / %r" % (code, raw[:8]))

        # ---- v6.0.0：OGG（Vorbis）输出，四件事都要真的成立 ----
        #
        # 为什么单独验一遍：libsndfile 的 `format="OGG"` 依赖**打包进去的那个
        # DLL 编了 libvorbis**。FLAC / MP3 能用不代表 OGG 能用 —— 官方 Windows
        # 预编译包历史上就有过「带 LAME、不带 Vorbis」的组合。真漏了的表现是
        # 「界面有卡片、一点就整批报 OGG 编码器不可用」，和 FLAC 那次同型。
        # 所以这里既问 diag 的运行时结论，也**真转一首**再读回来。
        _og = (_dd.get("ogg") or {}) if isinstance(_dd, dict) else {}
        _mark(bool(_og.get("ok")),
              "exe 里 OGG(Vorbis) 编码器真的可用（libsndfile 带 libvorbis）",
              str(_og.get("why"))[:60])
        _mark(b'data-fmt="ogg"' in _h2,
              "index.html 有 OGG 输出卡片", "%d B" % len(_h2))
        _mark(b"dlConvOgg" in _h5,
              "download.html 有「转 OGG」按钮", "%d B" % len(_h5))
        _mark(b'"#dlConvOgg"' in _h6,
              "download.js 的 CONV_BTNS 认 OGG 那颗按钮", "%d B" % len(_h6))
        _mark(b'FMT_PRIORITY = ["ogg"' in _h4 and b'"ogg"' in _h4,
              "app.js 把 ogg 列进格式优先级（选卡片才真能传出 fmt）",
              "%d B" % len(_h4))

        out_ogg = os.path.join(tmpdir.root(), "webexe_ogg")
        _jpost("/api/add_sources", {"paths": [SPC]})
        _jpost("/api/start", {"opts": {"out_dir": out_ogg, "do_cd": False,
                                       "do_wav": True, "fmt": "ogg"}})
        ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 240)
        code, st2 = _jget("/api/state")
        res2 = st2.get("result") or {}
        _mark(ok and st2.get("state") == "done"
              and int(res2.get("ok") or 0) >= 1
              and int(res2.get("fail") or 0) == 0,
              "打包态真转 OGG：≥1 首、0 失败", res2)

        _oggs = []
        for _r, _ds, _fs in os.walk(out_ogg):
            _oggs += [os.path.join(_r, _f) for _f in _fs
                      if _f.lower().endswith(".ogg")]
        _oggs.sort()
        _mark(bool(_oggs), "产出 .ogg 文件", len(_oggs))
        if _oggs:
            with open(_oggs[0], "rb") as _f:
                _head = _f.read(4)
                _osz = os.path.getsize(_oggs[0])
            _mark(_head == b"OggS",
                  "文件真的是 Ogg 容器（不是改了个后缀的 WAV）",
                  "%r / %d B" % (_head, _osz))
            try:
                import soundfile as _sf3
                _d3, _sr3 = _sf3.read(_oggs[0], dtype="int16", always_2d=True)
                _pk3 = int(abs(_d3).max()) if _d3.size else 0
                _sec3 = len(_d3) / float(_sr3 or 1)
                _mark(_sr3 == 44100 and _sec3 > 1.0 and _pk3 > 500,
                      "解回来是 44100Hz、%.2fs、峰值 %d（不是静音/噪声）"
                      % (_sec3, _pk3),
                      "%dch/%dHz" % (_d3.shape[1] if _d3.size else 0, _sr3))
                # 体积比：同一首的 WAV 还在 out 里，OGG 应该只有它的三成上下。
                # 写反了（比如编码器被当成无损）这条会立刻炸。
                _wsz = 0
                for _r2, _d2, _f2 in os.walk(out):
                    for _fn in _f2:
                        if _fn.lower().endswith(".wav"):
                            _wsz = os.path.getsize(os.path.join(_r2, _fn))
                            break
                    if _wsz:
                        break
                _mark(not _wsz or _osz < _wsz * 0.6,
                      "OGG 比同曲 WAV 小（%d B vs %d B）" % (_osz, _wsz))
            except Exception as _e:                  # noqa: BLE001
                _mark(False, "exe 产出的 .ogg 能被 libsndfile 读回来", repr(_e))
        _logs2 = " ".join(row[1] for row in (st2.get("logs") or []))
        _mark("落盘自检" in _logs2 and "OGG" in _logs2,
              "日志里有 OGG 落盘自检汇总行（🛡 … SNR 最低 …）",
              _logs2[-60:].replace("\n", " ") or "?")
        _jpost("/api/clear", {})

        # ---- 打包后才可能犯的错：日志里不该有 ImportError ----
        logs = " ".join(row[1] for row in (st.get("logs") or []))
        _mark("ImportError" not in logs and "ModuleNotFoundError" not in logs,
              "日志里没有 ImportError", logs[:200])

        # ---- .txtp（纯 Python 解码，不依赖 foobar2000）----
        # 这一项专门抓「psx_txtp 被 nofollow 掉了」：源码里跑得好好的，
        # 打包后一按转换就 ImportError。样本与源码门禁共用 _txtp_fixture。
        from _txtp_fixture import make_pack
        pack = make_pack(tmpdir.root())
        a_txtp = os.path.join(pack, "A.txtp")
        b_txtp = os.path.join(pack, "B.txtp")
        _jpost("/api/clear", {})
        code, d = _jpost("/api/add_sources", {"paths": [a_txtp, b_txtp]})
        _mark(code == 200 and d.get("added") == 2, "加 2 个 .txtp", d)
        code, st = _jget("/api/state")
        srcs = st.get("sources") or []
        _mark(bool(srcs) and all(s.get("kind") == "PSX 音频" for s in srcs),
              "归成 PSX 音频", [s.get("kind") for s in srcs])
        _mark(bool(srcs) and all(float(s.get("total_secs") or 0) > 0
                                 for s in srcs),
              "探到时长（说明 psx_txtp 在包里）",
              [s.get("total_secs") for s in srcs])

        out2 = os.path.join(tmpdir.root(), "webexe_txtp")
        code, d = _jpost("/api/start", {"opts": {"out_dir": out2, "do_cd": True,
                                                 "do_wav": True}})
        _mark(code == 200, "启动 .txtp 转换", d)
        ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 240)
        code, st = _jget("/api/state")
        _mark(ok and st.get("state") == "done", ".txtp 转换完成", st.get("state"))
        res = st.get("result") or {}
        _mark(int(res.get("ok") or 0) == 2 and int(res.get("fail") or 0) == 0,
              ".txtp 转出 2 首", res)
        names = sorted(os.listdir(out2)) if os.path.isdir(out2) else []
        _mark(any(n.lower().endswith(".cue") for n in names),
              ".txtp 也能出 CUE（走主流水线）", "、".join(names))

        # ---- 子流式 .txtp（0 字节，子流号在文件名里）------------------
        # 真实 PS1 下载包里最常见的就是这个形态：BGM.XA + 一堆 0 字节的
        # BGM.XA#01.txtp。它不依赖任何外部模块，所以打包不会坏 ——
        # 但**口径**会坏（序号按 channel 挑 / 三首同长），exe 里必须一样对。
        from _txtp_fixture import make_stream_pack, STREAM_SECTORS, stream_seconds
        spack = make_stream_pack(tmpdir.root())
        nums = sorted(STREAM_SECTORS)
        spaths = [os.path.join(spack, "BGM.XA#%02d.txtp" % n) for n in nums]
        _jpost("/api/clear", {})
        code, d = _jpost("/api/add_sources",
                         {"paths": spaths + [os.path.join(spack,
                                                          "BGM.XA#09.txtp")]})
        _mark(code == 200 and d.get("added") == 4,
              "子流式：4 个 0 字节 .txtp 都能加", d)
        code, st = _jget("/api/state")
        ssrc = st.get("sources") or []
        exp_secs = sorted(round(stream_seconds(STREAM_SECTORS[n]), 3) for n in nums)
        got_secs = sorted(round(float(s.get("total_secs") or 0), 3)
                          for s in ssrc if float(s.get("total_secs") or 0) > 0)
        _mark(got_secs == exp_secs,
              "子流式：三首时长各不同（按各自扇区算）",
              [s.get("total_secs") for s in ssrc])

        out3 = os.path.join(tmpdir.root(), "webexe_stream")
        code, d = _jpost("/api/start", {"opts": {"out_dir": out3, "do_cd": True,
                                                 "do_wav": True}})
        _mark(code == 200, "启动子流式转换", d)
        ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 240)
        code, st = _jget("/api/state")
        _mark(ok and st.get("state") == "done", "子流式转换完成", st.get("state"))
        res = st.get("result") or {}
        _mark(int(res.get("ok") or 0) == 3 and int(res.get("fail") or 0) == 1,
              "子流式：3 首成功 + 越界那条 1 失败", res)
        snames = sorted(os.listdir(out3)) if os.path.isdir(out3) else []
        swavs = [n for n in snames if n.lower().endswith(".wav")]
        _mark(len(swavs) == 3, "子流式：出 3 个 WAV", "、".join(swavs))
        _mark([w[:2] for w in swavs] == ["01", "02", "03"],
              "子流式：编号连续 01/02/03", "、".join(swavs))
        ssz = [os.path.getsize(os.path.join(out3, w)) for w in swavs]
        _mark(len(set(ssz)) == 3 and all(ssz),
              "子流式：三个 WAV 大小各不相同（没写成同一段）", ssz)

        # ---- 加整个 PS1 包目录（用户解压完直接拖目录）------------------
        # classify() 对目录没有 .txtp 分支时会归成「未知」直接拒收 ——
        # 用户只能一个个挑 20 多个文件，这才是「解压出来转不了」的第一步。
        _jpost("/api/clear", {})
        code, d = _jpost("/api/add_sources", {"paths": [spack]})
        _mark(code == 200 and d.get("added") == 1,
              "子流式：整个包目录能加进音源", d)
        code, st = _jget("/api/state")
        dsrc = (st.get("sources") or [{}])[0]
        _mark(dsrc.get("kind") == "PSX 音频",
              "子流式：目录归成 PSX 音频", dsrc.get("kind"))
        _mark(int(dsrc.get("n_tracks") or 0) == 4,
              "子流式：轨数 = 目录里 .txtp 个数", dsrc.get("n_tracks"))

        out4 = os.path.join(tmpdir.root(), "webexe_streamdir")
        code, d = _jpost("/api/start", {"opts": {"out_dir": out4, "do_cd": True,
                                                 "do_wav": True}})
        _mark(code == 200, "启动目录转换", d)
        ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 240)
        code, st = _jget("/api/state")
        _mark(ok and st.get("state") == "done", "目录转换完成", st.get("state"))
        res = st.get("result") or {}
        _mark(int(res.get("ok") or 0) == 3, "目录转出 3 首（坏的那条跳过）", res)
        dnames = sorted(os.listdir(out4)) if os.path.isdir(out4) else []
        _mark(len([n for n in dnames if n.lower().endswith(".wav")]) == 3,
              "目录出 3 个 WAV", "、".join(dnames))
        dcue = [n for n in dnames if n.lower().endswith(".cue")]
        _mark(len(dcue) == 1, "整目录合成 1 个 CUE", "、".join(dcue))

        # ---- PSF 系（PS1 芯片音源）--------------------------------------
        # 这条链路和别的格式不一样：真正干活的是**外挂的 psf2wav.exe**，
        # 不是 Python 模块。所以最典型的坏包是「psf_native.py 进去了、
        # psf2wav.exe 忘了带」—— 源码树里永远正常，只有 exe 会犯。
        code, d = _jget("/api/psf/status")
        _mark(code == 200 and d.get("ok"), "GET /api/psf/status", code)
        _mark(bool(d.get("decoder")), "psf2wav.exe 跟主程序在一起",
              d.get("decoder_path") or d.get("why"))
        _mark(os.path.basename(str(d.get("decoder_path") or "")) == "psf2wav.exe",
              "解码器路径确实指向 psf2wav.exe", d.get("decoder_path"))

        # v5.2.0.5：BIOS 固化目录必须落在 **exe 所在目录** 下的 bios/。
        # 这条只有打包态才会犯：源码态 app_dir() 是源码目录、打包态是 exe
        # 目录，写错地方就等于「固化了但下次找不到」，界面又变成「未指定」。
        _bh = os.path.abspath(str(d.get("bios_home") or ""))
        _want = os.path.abspath(os.path.join(os.path.dirname(exe), "bios"))
        _mark(_bh == _want, "bios_home 指向 exe 旁边的 bios/", _bh)
        if os.path.isdir(_want):
            _mark(os.path.isfile(os.path.join(_want, "SCPH30004R.bin"))
                  or any(x.lower().endswith(".bin") for x in os.listdir(_want)),
                  "产物目录的 bios/ 里有 .bin", os.listdir(_want)[:3])
        else:
            _skip("产物目录没有 bios/（源码树没固化过），跳过", "")

        if psf_bios:
            _mark(bool(d.get("bios")), "exe 认得出 BIOS（走 MSU1_PSF_BIOS）",
                  d.get("why"))
            _mark(d.get("available") is True, "PSF 状态：可用", d)
        else:
            _skip("本机没有 BIOS，PSF 只验到「解码器在包里」", "")

        # 真解一首：样本 + BIOS 都在才做（两样都不进仓库）
        if PSF_SAMPLE and os.path.isfile(PSF_SAMPLE) and psf_bios \
                and d.get("decoder"):
            _jpost("/api/clear", {})
            code, d = _jpost("/api/add_sources", {"paths": [PSF_SAMPLE]})
            _mark(code == 200 and d.get("added") == 1, "加 1 个 .psf", d)
            code, st = _jget("/api/state")
            src = (st.get("sources") or [{}])[0]
            _mark(src.get("kind") == "PSF", "归成 PSF", src.get("kind"))

            out_psf = os.path.join(tmpdir.root(), "webexe_psf")
            code, d = _jpost("/api/start", {"opts": {"out_dir": out_psf,
                                                     "do_cd": False,
                                                     "do_wav": True}})
            _mark(code == 200, "启动 PSF 转换", d)
            ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 300)
            code, st = _jget("/api/state")
            res = st.get("result") or {}
            _mark(ok and st.get("state") == "done", "PSF 转换完成",
                  st.get("state"))
            _mark(int(res.get("ok") or 0) == 1 and int(res.get("fail") or 0) == 0,
                  "PSF 转出 1 首", res)
            pnames = sorted(os.listdir(out_psf)) if os.path.isdir(out_psf) else []
            wav = next((os.path.join(out_psf, n) for n in pnames
                        if n.lower().endswith(".wav")), "")
            _mark(bool(wav), "PSF 产出 WAV", "、".join(pnames))
            if wav:
                peak = _wav_peak(wav)
                _mark(peak > 100, "PSF 输出不是全静音（peak > 100）", peak)
        else:
            _skip("没给 MSU1_PSF_SAMPLE 或缺 BIOS：跳过 PSF 真解码",
                  PSF_SAMPLE or "（环境变量为空）")

        # ---- 网易云云盘（v5.2 移植：登录窗 / 换号 / 上传范围）--------------
        # 这一段抓的全是「源码里全好、只有 exe 会犯」的坏法：
        #   1) cloud.js / cloud.css 忘了加进 WEB_STATIC → 点上传按钮没反应
        #   2) web_ui.cloud_api 被静态分析漏掉 → 点上传报「模块没打进来」
        #   3) segno 没打进去 → 扫码页永远申请不出二维码
        for name in ("/cloud.js", "/cloud.css"):
            code, raw = _req(name)
            _mark(code == 200 and len(raw) > 800,
                  "%s 打进包里了" % name, "%s / %d B" % (code, len(raw)))

        # theme.js 漏了的表现特别阴：页面全正常，只有点「复古配色」那颗按钮
        # 没反应，错误只出现在 DevTools 控制台的 "ThemeKit is not defined"。
        code, raw = _req("/theme.js")
        _mark(code == 200 and len(raw) > 1500,
              "theme.js 打进包里了", "%s / %d B" % (code, len(raw)))
        code, d = _jget("/api/themes")
        _mark(code == 200 and len(d.get("themes") or []) == 8,
              "GET /api/themes 给 8 套配色", len(d.get("themes") or []))
        retro = [t for t in (d.get("themes") or []) if str(t.get("key", "")).startswith("retro:")]
        _mark(len(retro) == 6 and all(len(t.get("vars") or {}) >= 20 for t in retro),
              "6 套复古配色都带完整变量", [len(t.get("vars") or {}) for t in retro])

        code, d = _jget("/api/cloud/status")
        _mark(code == 200 and d.get("ok"), "GET /api/cloud/status", code)
        deps = d.get("deps") or {}
        for k in ("soundfile", "Crypto", "segno"):
            _mark(bool(deps.get(k)), "云盘依赖 %s 在包里" % k, deps.get(k))
        _mark(d.get("can_upload") is True, "exe 里云盘可用（can_upload）", d)
        _mark("cloud_api" not in (d.get("why") or ""),
              "cloud_api 模块确实打进包了", d.get("why"))
        # 浏览器登录要用系统 Chrome / Edge；没有也只能说清楚，不算失败
        if d.get("can_browser"):
            _mark(True, "找到可用的浏览器（浏览器登录可用）", d.get("browser"))
        else:
            _skip("本机没装 Chrome / Edge：浏览器登录不可用（粘贴 Cookie 仍可）",
                  "")
        # 扫码只验「矩阵能出来」，不真扫 —— 这一步会打一次网易云接口，
        # 没网时给 [--]，**跳过不算通过**。
        code, q = _jpost("/api/cloud/qr_start", {})
        if q.get("ok"):
            _mark(len(q.get("rows") or []) == int(q.get("n") or 0) > 0,
                  "exe 里能申请二维码（segno 可用）",
                  "%s×%s" % (q.get("n"), q.get("n")))
        else:
            _skip("二维码申请失败（多半没网）：%s" % (q.get("error") or "")[:50], "")
        # 粘贴 Cookie 的报错路径：不会上传、不会弹窗，纯字符串校验
        code, d = _jpost("/api/cloud/login_cookie", {"cookie": "a=1; b=2"})
        _mark(d.get("ok") is False and "MUSIC_U" in (d.get("error") or ""),
              "exe 里粘贴 Cookie 的校验说得清原因", d.get("error"))

        # ---- 下载页（v5.1 移植）------------------------------------------
        # 这一整段是「源码里全好、只有 exe 会犯」的重灾区：
        #   1) 静态件漏打包 → 点「音源下载」404
        #   2) 模块被 nofollow 掉 → 页面能开，一按开始 ModuleNotFoundError
        #   3) index/ 没打包 → 退化成联网全量扫描（慢，且离线直接废）
        code, raw = _req("/download.html")
        _mark(code == 200 and "音源下载".encode("utf-8") in raw,
              "download.html 打进包里了", "%s / %d B" % (code, len(raw)))
        code, raw = _req("/download.css")
        _mark(code == 200 and len(raw) > 3000, "download.css 打进包里了",
              "%s / %d B" % (code, len(raw)))
        code, raw = _req("/download.js")
        _mark(code == 200 and len(raw) > 5000, "download.js 打进包里了",
              "%s / %d B" % (code, len(raw)))

        code, d = _jget("/api/dl/diag")
        _mark(code == 200 and d.get("ok"), "GET /api/dl/diag", code)
        # 逐项点名，失败时能一眼看出是哪个模块没进去
        # zophar_source 是 v5.7 新增的第二家音源站。它只被 dl_api 的
        # try/except 可选 import 引用 —— PyInstaller 静态分析算不出来，
        # 漏在 HIDDEN 外面就会「源码树正常、打包后点 Game Gear 报模块缺失」。
        for k in ("joshw_source", "zophar_source", "joshw_dl", "dl_convert",
                  "zh_titles", "zh_online", "psf_native", "py7zr",
                  # v5.9.14 封面。同样是 dl_api 的 try/except 可选 import，
                  # 漏了的表现最隐蔽：下载照常、日志里一行 🖼 都没有、
                  # 转出来的 FLAC 没封面 —— 全是「静默少一个功能」。
                  "cover_art",
                  # v6.0.5 IGDB 兜底源。**必须是单独一条**：只漏它的时候
                  # 上面 cover_art 那条照样绿，而用户看到的是「Xbox 360 还是
                  # 一张图都抓不到」—— 和没做这个功能一模一样。
                  "cover_igdb"):
            _mark(bool(d.get(k)), "下载页依赖 %s 在包里" % k, d.get(k))
        # 封面：不只问「模块在不在」，还要问「登记了多少机种」「缓存目录在哪」。
        # 模块在、PLATFORM_DIRS 空的话，界面一切正常但一张图都抓不到。
        _cv = d.get("covers") or {}
        _mark(int(_cv.get("sources") or 0) >= 28,
              "封面映射表打进包里（≥28 个机种）", _cv.get("sources"))
        _mark(bool(_cv.get("root")), "封面缓存目录算得出来", _cv.get("root"))
        # v6.0.5：IGDB 的平台映射表也得在包里。模块在、PLATFORM_IDS 空的话
        # 表现为「日志里永远只有 libretro 那一行」，看不出是打包漏了。
        _mark(int(d.get("cover_igdb_platforms") or 0) == 33,
              "IGDB 平台映射 33 个机种都在包里",
              d.get("cover_igdb_platforms"))
        # v6.0.6：衍生 WAV 去重。两个函数在 exe 里取不到的表现是
        # 「FLAC 数量还是 MP3 的两倍」，而界面一切正常 —— 只能靠点名。
        _mark(bool(d.get("derived_wav")),
              "衍生 WAV 去重的两个函数在包里"
              "（is_derived_wav / drop_derived_wavs）", d.get("derived_wav"))
        _mark(str(d.get("audio_writer_version") or "") == "6.0.12",
              "转换器版本是 6.0.12（打包漏了改动时这个数字对不上）",
              d.get("audio_writer_version"))
        # v6.0.7：多封面上限。0 / 缺失 = 常量没打进去，轮播会退化。
        _mark(int(d.get("cover_limit") or 0) == 6,
              "每游戏封面上限 6 张在包里（多封面轮播靠它）",
              d.get("cover_limit"))
        # v6.0.8：「更新全部列表」的并发数。0 / 缺失 = 常量没打进去，
        # 那颗按钮要么报「没有可更新的机种」，要么串行跑十几分钟。
        _mark(int(d.get("refresh_workers") or 0) == 3,
              "「更新全部列表」并发 3 路在包里", d.get("refresh_workers"))
        # v6.0.11：封面根改到 ``<下载根>/Artworks``。这个断言能抓住两类
        # 打包漏项：cover_art 的 set_cache_root 没打进去（root 还指
        # %LOCALAPPDATA%），或者 dl_api 没在服务启动时调它。
        _cov = d.get("covers") or {}
        _mark("Artworks" in str(_cov.get("root") or ""),
              "封面根在 <下载根>/Artworks 下（用户要求的位置）",
              _cov.get("root"))
        _mark("games" in _cov,
              "封面统计报得出「游戏数」—— 说明认的是三层结构"
              "（<根>/<机种>/<游戏名>/<图>）", sorted(_cov.keys()))
        # PSF 的解码器是外挂 exe，不在 modules 里 —— 单独点名
        _mark(bool(d.get("psf_decoder")), "下载页也能看到 psf2wav.exe",
              d.get("psf_decoder"))
        _mark(int(d.get("platforms") or 0) == 33,
              "机种表 33 个（复古 15 + 现代 13 + 计算机街机 5）", d.get("platforms"))
        # ⚠ 中文词典的**条数**。这一条比「zh_titles 模块在包里」重要得多：
        #   模块在、zh_titles_user.json 没打进去的话，下载页「中文名」那列
        #   几乎全是「—」（实测 1918 条里只剩 321 条），而模块检查照样是绿的。
        #   内置字面量约 1200 条，带上 JSON 是 2.8 万条 —— 用 2 万当门槛。
        _mark(int(d.get("zh_dict") or 0) > 20000,
              "中文词典条数正常（>2 万）", d.get("zh_dict"))

        code, d = _jget("/api/dl/platforms")
        groups = d.get("groups") or []
        _mark(code == 200 and len(groups) == 3, "机种分成 3 组", len(groups))
        n_plat = sum(len(g.get("items") or []) for g in groups)
        _mark(n_plat == 33, "共 33 个机种", n_plat)
        # v5.9：Zophar 那组已撤（joshw 本来就有 GG/SMS，见 joshw_source 注释）
        _mark(not [g for g in groups if g.get("group") == "Zophar 原声"],
              "v5.9：「Zophar 原声」组已移除",
              [g.get("group") for g in groups])
        # 三个新机种必须真在包里 —— 它们靠 PLATFORMS 的 path 字段指到容器站子目录，
        # path 拼错的话界面上照样列出条目，点进去 404，只有真跑才看得见。
        keys = {i.get("key") for g in groups for i in (g.get("items") or [])}
        for k, why in (("kss_gg", "Game Gear（kss 容器站拆出来）"),
                       ("kss_sms", "Master System（kss 容器站拆出来）"),
                       ("switch", "Nintendo Switch（v5.9 新增）")):
            _mark(k in keys, "机种表含 %s" % why, sorted(keys)[:6])
        # hoot 站 19 个目录里只有 2 个是 .7z 音源，其余是 Hoot 的 .zip ROM
        n_hoot = len([k for k in keys if str(k).startswith("hoot_")])
        _mark(n_hoot == 2, "hoot 只拆出 2 个真音源（其余 17 个是 .zip ROM）", n_hoot)
        _mark("ps5" not in keys, "ps5 已移除（apex 连不上，点下去必失败）")

        # 内置索引快照：没有 index/ 就会去联网扫，这里 60 秒内出不来
        code, d = _jpost("/api/dl/load", {"key": "spc"}, timeout=30)
        _mark(code == 200 and d.get("ok"), "POST /api/dl/load spc", d.get("error"))
        ok = _wait(lambda: not _jget("/api/dl/state")[1].get("scanning"), 60)
        code, st = _jget("/api/dl/state")
        _mark(ok, "索引加载在 60 秒内完成（没退化成联网扫描）", st.get("status"))
        n_cnt = int(st.get("count") or 0)
        _mark(n_cnt >= 1000,
              "读到内置快照（说明 index/ 打进包了）", n_cnt)
        _mark(bool(st.get("rows")), "状态里带回条目行",
              len(st.get("rows") or []))
        _mark(st.get("key") == "spc", "当前机种是 spc", st.get("key"))

        # 全选：走的是后端状态机（前端只负责显示）
        code, d = _jpost("/api/dl/select", {"mode": "all"})
        _mark(code == 200 and d.get("ok"), "全选", d)
        code, st = _jget("/api/dl/state")
        _mark(int(st.get("sel") or 0) == int(st.get("count") or 0) > 0,
              "全选后 sel == count", "%s / %s" % (st.get("sel"), st.get("count")))
        _jpost("/api/dl/select", {"mode": "none"})
        code, st = _jget("/api/dl/state")
        _mark(int(st.get("sel") or 0) == 0, "清空选择后 sel=0", st.get("sel"))

        # 「开始下载」不真跑（要联网、要几分钟）。只验它能被拒/能进队列：
        # 未选任何条目时必须明确报错，而不是静默什么都不做。
        code, d = _jpost("/api/dl/start", {})
        _mark(code == 200 and not d.get("ok"),
              "没选条目时 start 明确报错", d)

        # ---- 下载页试听（v5.2-web-play）----
        # 路由在 server.py、实现在 dl_api.py，两个都得在包里；静态页也得带
        # 试听面板的 DOM，否则「接口通了但界面没这块」这种半拉子事查不出来。
        code, d = _jget("/api/dl/wavs?force=1")
        _mark(code == 200 and d.get("ok") is True,
              "GET /api/dl/wavs 可用", "%s %s" % (code, d.get("error")))
        _mark("wavs" in d and isinstance(d.get("wavs"), list),
              "wavs 是列表", type(d.get("wavs")).__name__)
        # 越界必须 404（打包后这条路径同样要挡住）
        code, _raw = _req("/api/dl/audio?rel=..%2F..%2Fsecret.wav")
        _mark(code == 404, "越界路径被挡（404）", code)
        code, _raw = _req("/api/dl/audio?rel=nope.wav")
        _mark(code == 404, "不存在的 WAV 返回 404", code)
        code, raw = _req("/download.html")
        _mark(b"dlPlayList" in raw and b"dlPlayBar" in raw,
              "打包后的下载页带试听面板", len(raw))
        # 音量控件（v5.2.4）：包里必须有这三样，少一样就是「打包时漏了新文件」
        _mark(b'id="dlVol"' in raw and b'id="dlVolBtn"' in raw,
              "打包后的下载页带音量控件", len(raw))
        code, raw = _req("/download.js")
        _mark(b"/api/dl/wavs" in raw and b"/api/dl/audio" in raw,
              "打包后的 download.js 走试听接口", len(raw))
        _mark(b"dl_vol" in raw and b"localStorage" in raw,
              "打包后的 download.js 会记住音量", len(raw))
        code, raw = _req("/download.css")
        _mark(b"--vol-pct" in raw and b".dl-vol-btn" in raw,
              "打包后的 download.css 带音量样式", len(raw))
        # v5.9 机种栏折叠：分组头从 div 变 button，靠 .dl-grp-body[hidden] 收起。
        # 这两个标记缺任何一个，「33 个机种平铺一屏」就会回来 —— 而它在
        # 界面上看着只是「列表很长」，不会报错，只有肉眼能发现。
        _mark(b".dl-grp-body" in raw and b".dl-grp-arrow" in raw,
              "打包后的 download.css 带机种分组折叠样式", len(raw))
        code, raw = _req("/download.js")
        _mark(b"rml-dl-grp" in raw and b"setGroupOpen" in raw,
              "打包后的 download.js 带机种分组折叠逻辑（含 localStorage 记忆）", len(raw))
        # v5.9.1 虚拟滚动：大机种（Switch 3512 条）以前只渲染前 800 行，
        # 底部写「还有 N 条未显示」。现在只渲染可视窗口，靠 .dl-vs 撑假高度。
        # 这三条缺一条都会退回「翻不到底」，而且**不会报错**，只能靠这里守。
        code, raw = _req("/download.css")
        _mark(b".dl-vs" in raw and b".dl-vs-win" in raw,
              "打包后的 download.css 带虚拟滚动两层结构", len(raw))
        code, raw = _req("/download.js")
        _mark(b"renderWindow" in raw and b"ROW_H" in raw,
              "打包后的 download.js 走虚拟滚动渲染", len(raw))
        _mark(b"RENDER_MAX" not in raw,
              "截断上限 RENDER_MAX 已彻底移除（不会有人偷偷加回来）", len(raw))
        # v5.9.2 清空试听列表：按钮 + 接口 + 「null 哨兵」三样都得在包里。
        # 最后一条守的是那个很隐蔽的坑：清空后签名是空串，若拿 "" 当
        # 「强制重建」的哨兵，两者相等就直接 return —— 计数变 0、列表还是旧的。
        code, raw = _req("/download.html")
        _mark(b'id="dlPlayClear"' in raw, "打包后的下载页有「清空」按钮", len(raw))
        code, raw = _req("/download.js")
        _mark(b"/api/dl/wav_clear" in raw and b"wavSig: null" in raw,
              "打包后的 download.js 会清空试听列表（含 null 哨兵）", len(raw))

        # v5.9.3 状态列靠磁盘事实 + 保存目录以用户设定为准。
        # 这几条守的是「重启后状态列全空」和「换了保存目录还按默认目录扫」
        # 两个真实故障 —— 都是静默的（不报错、只是显示空白），只能靠这里盯。
        code, raw = _req("/api/dl/state")
        dls = json.loads(raw.decode("utf-8") or "{}")
        _mark("n_on_disk" in dls, "打包后的 /api/dl/state 带 n_on_disk",
              sorted(k for k in dls if k in ("n_on_disk", "scan")))
        _mark(isinstance(dls.get("scan"), dict)
              and "dir" in dls["scan"] and "limited" in dls["scan"],
              "打包后的 state 带 scan（按哪个目录扫的）", dls.get("scan"))
        _mark(os.path.abspath(dls["scan"].get("dir") or "").lower()
              == os.path.abspath(dls.get("dest") or "").lower(),
              "scan.dir 就是用户设定的保存目录（不绑死本机）",
              "%s / %s" % (dls["scan"].get("dir"), dls.get("dest")))
        code, raw = _req("/download.js")
        _mark(b"d.scan.dir" in raw and b"destToast" in raw,
              "打包后的 download.js 改目录会按新目录重扫并提示", len(raw))

        # v5.9.4 行内按钮 + 上传选游戏。四条都是「静默失效」型：
        # 少了按钮不报错（只是没有入口）、少了 deep 不报错（只是说没有 WAV）、
        # 少了 games 不报错（只是全量上传）。
        code, raw = _req("/download.html")
        _mark(b'class="c-act"' in raw,
              "打包后的下载页多了一列「操作」（行内按钮的落脚点）", len(raw))
        code, raw = _req("/download.js")
        _mark(b"function actOf" in raw and b"/api/dl/convert_wav" in raw
              and b"/api/dl/wav_add" in raw,
              "打包后的 download.js 有「转 WAV / ＋试听」两颗行内按钮", len(raw))
        code, raw = _req("/cloud.js")
        _mark(b"function chooseGames" in raw and b"o.games" in raw,
              "打包后的 cloud.js 能选「这次传哪几个游戏」", len(raw))

        # ---- v6.0.1：四条用户改动，打包后还在不在 ----
        # 守的是**打包漏文件**：新增的 JS / CSS 是静态资源，漏了不报错，
        # 表现是「功能凭空消失」。封面接口再补两条真 HTTP 的负向断言。
        code, raw = _req("/download.html")
        _mark(b'id="dlCoverBox"' in raw and b'id="dlCoverImg"' in raw,
              "打包后的下载页带封面区（日志与试听之间）", len(raw))
        code, raw = _req("/download.css")
        _mark(b"--dl-cols" in raw and b".c-grip" in raw,
              "打包后的 download.css 带列宽变量与拖拽把手", len(raw))
        code, raw = _req("/download.js")
        _mark(b"function initCols" in raw and b"function showCover" in raw
              and b"/api/dl/cover" in raw,
              "打包后的 download.js 有列宽拖动与封面切换", len(raw))
        code, raw = _req("/cloud.js")
        _mark(b'name="clFmt"' in raw and b"fmt: pick.fmt" in raw,
              "打包后的 cloud.js 能选 FLAC / MP3 并把它发出去", len(raw))
        # ⚠ 这两条必须打真 exe 跑：封面接口是「前端给游戏名、后端算路径」，
        #   打包后 mimetypes / 路径拼接任一处出问题都只在这条路上显形。
        code, _raw = _req("/api/dl/cover?game=..%2F..%2FWindows")
        _mark(code == 404, "封面接口挡穿越（404）", code)
        code, _raw = _req("/api/dl/cover?game=NoSuchGameInThisMachine")
        _mark(code == 404, "没有封面时 404（前端显示 ♪，不弹错）", code)
        code, raw = _req("/api/dl/wav_games")
        wg = json.loads(raw.decode("utf-8") or "{}")
        _mark("games" in wg and "total" in wg,
              "打包后的 /api/dl/wav_games 能列出已转好的游戏", sorted(wg))

        # ---- v6.0.9 / v6.0.10：每一列都能拖宽窄，且互不影响 ----
        # v6.0.9 的根因是默认列宽溢出 30px + 名称列贴死下限 + 布局没就绪就
        # 量宽度；v6.0.10 又换了分配模型（拖谁只改谁，超了出横向滚动条）。
        # 这几条缺任何一条，症状都会回来，而且**不报错**（只是拖不动 /
        # 拖了别的列跟着缩），只能靠这里守。
        code, raw = _req("/download.js")
        _mark(b"function colLaidOut" in raw and b"function colDefaults" in raw,
              "打包后的 download.js 会等布局就绪再算列宽（boot() 里 clientWidth 常是 0）",
              len(raw))
        _mark(b"function colRoom" not in raw and b"function colGive" not in raw,
              "打包后的 download.js 里没有 colRoom / colGive"
              "（它们就是「互相借宽度」，有它们在拖一列会连累别列）", len(raw))
        _mark(b"cols[key] = Math.max(COL_MIN[key], Math.round(start + ev.clientX - startX));"
              in raw,
              "打包后的 colDrag 只写 cols[key]（拖动不影响其它列）", len(raw))
        _mark(b'w.style.setProperty("--dl-w"' in raw
              and b"function colSyncHead" in raw,
              "打包后的 download.js 会撑开表格（--dl-w）并让表头跟随横向滚动",
              len(raw))
        _mark(b"requestAnimationFrame(() => requestAnimationFrame(refit))" in raw,
              "打包后的 download.js 等两帧再校一次列宽", len(raw))
        _mark(b'["c-sel", "sel"]' not in raw,
              "复选框列不挂拖拽把手（空 span 高度 0，把手跟着 0 高永远点不中）",
              len(raw))
        _mark(b'["c-act", "act"]' in raw,
              "打包后的 download.js 操作列也挂了把手（六列可调，只留复选框列）",
              len(raw))
        code, raw = _req("/download.css")
        _mark(b"min-height: 18px" in raw,
              "打包后的 download.css 给表头格子撑了 min-height"
              "（c-sel / c-act 是空 span，不撑的话把手 0 高、点不中）", len(raw))
        _mark(b"min-width: var(--dl-w" in raw and b"overflow: auto" in raw,
              "打包后的 download.css 用 --dl-w 撑开表格、.dl-rows 双轴滚动",
              len(raw))

        # v5.9.5：SPC 包的转换不再「0% 卡住」。
        # 打包后最容易出问题的是 worker **不认新参数** —— 那会让整批解码直接
        # 失败，界面只显示「SPC 解码失败」，反而比原来更难查。所以直接拿
        # 打包出来的 exe 跑一次 worker，对比「有 / 没有 --spc-cap」的产出长度。
        sample = os.path.join(ROOT, "vendor", "sample.spc")
        if os.path.isfile(sample):
            import struct
            import tempfile

            def _run_spc_worker(cap):
                """跑一次 worker，返回 (退出码, PCM 总字节, stderr)。

                ⚠ 临时目录必须自己清 —— 这个脚本每跑一次会解两次包，
                   不清的话 ``D:/MSU1Temp`` 里会攒下一堆 ``spc_cap_*``
                   （`tmpdir` 把 TEMP 指到了项目盘，本来是为了不塞满 C 盘）。
                """
                d = tempfile.mkdtemp(prefix="spc_cap_")
                out = os.path.join(d, "a.spcbin")
                try:
                    cmd = [exe, "--spc-decode", out, "--spc-length", "natural"]
                    if cap:
                        cmd += ["--spc-cap", str(cap)]
                    cmd.append(sample)
                    try:
                        p = subprocess.run(cmd, capture_output=True, timeout=180)
                    except subprocess.TimeoutExpired:
                        return -9, 0, "超时 180s"
                    if p.returncode != 0:
                        return (p.returncode, 0,
                                p.stderr.decode("utf-8", "replace")[-300:])
                    total = 0
                    with open(out, "rb") as f:
                        (n,) = struct.unpack("<I", f.read(4))
                        for _ in range(n):
                            (nl,) = struct.unpack("<H", f.read(2))
                            f.read(nl)
                            (pl,) = struct.unpack("<I", f.read(4))
                            f.seek(pl, 1)
                            total += pl
                    return 0, total, ""
                finally:
                    try:
                        if os.path.exists(out):
                            os.remove(out)
                        os.rmdir(d)
                    except OSError:
                        pass

            rc1, n1, e1 = _run_spc_worker(None)
            rc2, n2, e2 = _run_spc_worker(2)
            _mark(rc1 == 0 and rc2 == 0,
                  "打包后的 worker 认 --spc-cap（不认就会整批解码失败）",
                  e2 or e1)
            _mark(n1 > n2 > 0 and n2 <= 2 * 176400 + 64,
                  "打包后的 worker 按 --spc-cap 提前停手",
                  "%.2f 秒 → %.2f 秒" % (n1 / 176400.0, n2 / 176400.0))

        # ---- CD-DA（PyAV + ffmpeg DLL）：只在有素材时验 ----
        cue = os.path.join(ROOT, "output",
                           "Ginga_Fukei_Densetsu_-_Sapphire_(NTSC-J)_[HCD5080].cue")
        if os.path.isfile(cue):
            _jpost("/api/clear", {})
            _jpost("/api/add_sources", {"paths": [cue]})
            code, st = _jget("/api/state")
            src = (st.get("sources") or [{}])[0]
            _mark(int(src.get("n_tracks") or 0) > 0,
                  "镜像能探到轨数（说明 CUE 解析正常）", src.get("n_tracks"))
            print("     （CD-DA 真解码较慢，这里只验元信息，不算启动时间）", flush=True)

        # ---- 第二个实例：必须安静退出，不能把第一个实例的服务带走 ----
        # 这是 2026-10-22 用户报「127.0.0.1 拒绝连接」的直接防线。
        # ThreadedServer 曾经是 allow_reuse_address=True，而 **Windows 的
        # SO_REUSEADDR 允许两个 socket 绑同一端口**（Unix 只是复用 TIME_WAIT）。
        # 于是「固定端口当单实例锁」从没生效：第二个实例照样绑上、起自己的服务、
        # 再拉一个 Edge；那个 Edge 发现 user-data-dir 被前一个 Edge 占着，就把
        # URL 转交过去然后自己退出 → 第二个实例的 proc.wait() 立刻返回 →
        # 它带着自己的服务一起退出 → 用户窗口失联，标题停在 "127.0.0.1"。
        p2 = subprocess.Popen([exe], cwd=os.path.dirname(exe),
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        try:
            rc2 = p2.wait(timeout=30)
            _mark(rc2 == 0, "第二实例安静退出（returncode=0）", rc2)
        except subprocess.TimeoutExpired:
            _mark(False, "第二实例 30 秒没退（很可能弹了 MessageBox 在等人点）",
                  "")
            kill_tree(p2.pid)
        _mark(proc.poll() is None, "第一实例没被第二实例带走", proc.poll())
        _mark(bool(app_windows()), "第一实例的窗口还在", app_windows())
    finally:
        if "--keep" in argv:
            print("\n--keep：exe 留着不关（pid=%d）" % proc.pid, flush=True)
        else:
            kill_tree(proc.pid)
            try:
                proc.wait(10)
            except Exception:                                    # noqa: BLE001
                pass

    print("\n" + "=" * 46, flush=True)
    print("exe 验收: %d 通过 / %d 失败" % (_PASS, _FAIL), flush=True)
    print("=" * 46, flush=True)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
