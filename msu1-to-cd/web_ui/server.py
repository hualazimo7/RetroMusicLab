#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server.py — Web 版的后端：静态文件 + REST API + 任务状态。

## 为什么要自己写 HTTP server

pywebview 在本机不可用（WebView2 控件导航永不启动，已用最小例子 + Python
3.13/3.14 双版本确证），所以外壳改用系统 Edge 的 ``--app`` 模式。既然外壳是
浏览器，后端就得自己起一个 http server。

## 三条必须记住的约束

1. **必须多线程**（``ThreadingTCPServer``）。单线程时浏览器并行请求 css/js，
   第二个会堵在队列里 → 资源加载不完 → 页面看起来像「JS 没执行」。
2. **必须禁缓存**（``Cache-Control: no-store``）。WebView2/Edge 的 InPrivate
   也缓存，改完前端文件不生效会让人怀疑人生。
3. **长任务必须离开请求线程**。转换跑几分钟，请求线程要立刻返回，
   进度靠 ``GET /api/state?since=N`` 轮询（增量拉日志）。

## 独立运行

    python -m web_ui.server            # 起在随机端口，打印 URL
    python -m web_ui.server --port 8080
"""

import json
import mimetypes
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import http.server

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import msu1_to_cd as core                                        # noqa: E402
import chip_formats                                              # noqa: E402
import stream_audio                                              # noqa: E402
import dl_convert                                                # noqa: E402
from convert_job import (ConvertJob, classify, fmt_dur, fmt_size,   # noqa: E402
                         TXTP_EXT, USF_EXTS, VIO_EXTS, SSF_EXTS, GSF_EXTS)

# ⚠ 这里必须是**包内绝对导入**，不能写成裸 ``import native_dialog``。
# Nuitka 是静态解析 import 的：写成 ``try: from . import x / except: import x``
# 会把两个分支都跟进去，裸的那条解析不到 → 打包时报 Unresolved import，
# 或者更糟：静默过了、运行时 ImportError。所以统一走包路径，
# 跑法固定为 ``python -m web_ui.server``。
from web_ui import native_dialog                                  # noqa: E402

WEB_DIR = os.path.dirname(os.path.abspath(__file__))

#: 身份标识 —— ``GET /api/whoami`` 回它，``web_ui/shell.py`` 拿它分辨
#: 「端口上蹲着的是我们自己（第二个实例）」还是「别的程序占了这个端口」。
#: 改这个字符串等于改协议，shell 那边的 ``_WHOAMI_APP`` 要一起改。
APP_ID = "RetroMusic Lab"

#: 产品版本 —— 单一真源。``/api/diag`` 回它，界面上的 ``.brand-ver`` 徽标
#: 与 ``<title>`` 也写同一个数（HTML 里是静态文本，改版本时**两处都要动**：
#: 本常量 + ``web_ui/index.html`` 的 ``<title>`` / ``brand-ver``）。
#: ⚠ 历史上这里漂过：界面停在 5.2、实际已经是 5.5（2026-10-25 修）。
APP_VERSION = "6.0.12"

#: 只允许这些静态文件出去 —— 别把整个目录暴露成可下载
STATIC_OK = {"index.html", "app.css", "app.js", "favicon.ico", "logo.svg",
             "download.html", "download.css", "download.js",
             # 云盘登录窗 / 上传范围：两页共用，见 web_ui/cloud.js
             "cloud.js", "cloud.css",
             # 6 套复古主题的注入器：两页共用，见 web_ui/theme.js
             "theme.js",
             # 频谱 / 波形可视化（VizKit）：两页共用，见 web_ui/viz.js
             "viz.js"}

#: 下载页的状态单例。**延迟创建** —— 首次进下载页才 import
#: ``joshw_source`` / ``joshw_dl`` / ``dl_convert``，主界面启动不受它们拖累。
_DL = None
_DL_LOCK = threading.Lock()


def dl_state():
    """拿下载页状态单例（首次调用时创建）。"""
    global _DL
    if _DL is not None:
        return _DL
    with _DL_LOCK:
        if _DL is None:
            from web_ui import dl_api
            _DL = dl_api.DownloadState()
    return _DL


#: 云盘账号单例住在 ``cloud_api`` 里（下载页也要用，而 dl_api 不能 import
#: 本模块 —— 那会循环 import）。这里只做一次转手。
def cloud_auth():
    """拿云盘账号单例（首次调用时创建）。"""
    from web_ui.cloud_api import auth
    return auth()


#: 日志环形缓冲上限（超了丢最旧的，避免长时间跑把内存吃满）
LOG_MAX = 3000


def _probe_spc(path, entry):
    """给 SPC 补上「几首 / 多长」。

    只读 ID666 头，不加载 gme —— 加 92 个文件的目录也是毫秒级。
    ``spc_wav.probe`` 返回 ``(曲名, 游戏名, 秒数, 淡出毫秒)``，**不含轨数**，
    所以目录就按 .spc 文件个数算，单文件恒为 1 轨。
    """
    try:
        import spc_wav
    except ImportError:
        return
    if os.path.isdir(path):
        try:
            names = sorted(n for n in os.listdir(path) if n.lower().endswith(".spc"))
        except OSError:
            return
        if not names:
            return
        entry["n_tracks"] = len(names)
        total = 0.0
        for n in names:
            try:
                total += float(spc_wav.probe(os.path.join(path, n))[2] or 0)
            except Exception:                    # noqa: BLE001 单个坏文件不影响整体
                pass
        entry["total_secs"] = total
        return
    try:
        title, game, secs, _fade = spc_wav.probe(path)
        entry["n_tracks"] = 1
        entry["total_secs"] = float(secs or 0)
        if game and not entry["label"].startswith(game):
            entry["label"] = "%s (%s)" % (entry["label"], game)
    except Exception:                            # noqa: BLE001 头坏了就只显示文件名
        pass


def _probe_txtp(path, entry):
    """给 ``.txtp`` 补上「几首 / 多长」。返回要写进日志的警告（没有就空串）。

    ``.txtp`` 一行一个索引，天然就是单轨。``probe_txtp`` 只 stat 目标文件
    再按扇区数换算，**不读音频**，所以加一整个 PS1 包（几十个 .txtp）
    也是毫秒级。目标缺失时如实返回原因，别让用户以为加上了就能转。

    目录也算一种音源（``BGM.XA`` + 一堆 ``.txtp`` 就是 PS1 包解压后的原样）：
    这时轨数 = ``.txtp`` 个数，时长 = 各自时长之和。探不出来的（越界子流、
    目标缺失）不参与求和，但会在返回值里点明有几首，别让总数对不上。
    """
    try:
        import psx_txtp
    except ImportError:
        entry["n_tracks"] = 0
        return ".txtp 解码模块未打包（psx_txtp）"
    if os.path.isdir(path):
        paths = psx_txtp.list_txtp(path)
        if not paths:
            entry["n_tracks"] = 0
            return "目录里没有 .txtp"
        entry["n_tracks"] = len(paths)
        total, bad = 0.0, 0
        for one in paths:
            try:
                pr = psx_txtp.probe_txtp(one)
            except Exception:                    # noqa: BLE001 单个坏索引不影响整体
                bad += 1
                continue
            if pr.get("ok"):
                total += float(pr.get("seconds") or 0)
            else:
                bad += 1
        entry["total_secs"] = total
        return ("%d 首探不出时长（越界子流或目标缺失），仍可加进列表"
                % bad) if bad else ""
    entry["n_tracks"] = 1
    try:
        pr = psx_txtp.probe_txtp(path)
    except Exception as exc:                     # noqa: BLE001 坏索引不影响加音源
        return "探测失败: %s" % exc
    if not pr.get("ok"):
        return pr.get("error") or "探测失败"
    # 直接加一个 .xa（平铺的 PS1 包）：轨数 = 子流数，不是恒 1。
    # probe_txtp 对 .xa 会把 streams 塞进 stream 字段，见 psx_txtp.probe_txtp。
    if psx_txtp.is_xa(path):
        entry["n_tracks"] = max(1, int(pr.get("stream") or 1))
    entry["total_secs"] = float(pr.get("seconds") or 0)
    entry["size"] = int(pr.get("size") or entry.get("size") or 0)
    return ""


def _probe_psf(path, entry):
    """给 PSF 补上时长 —— 读文件尾 ``[TAG]`` 里的 ``length``，**不起模拟器**。

    一个 ``.psf`` 就是一轨（它是一份内存快照，不是多曲容器）。时长探不到
    （没写 length 标签）就留 0，界面上显示「1 首 / --」，不能编一个数。
    """
    entry["n_tracks"] = 1
    try:
        import psf_native
        entry["total_secs"] = float(psf_native.length_tag(path) or 0.0)
    except Exception:                            # noqa: BLE001 只是显示用
        pass


def _chip_files(path):
    """音源路径 -> 里面的芯片音源文件列表（单个文件就返回它自己）。"""
    p = str(path or "")
    if os.path.isfile(p):
        return [p]
    try:
        import msu1_to_cd as _core
        exts = _core.CHIP_EXTS
        names = sorted(n for n in os.listdir(p) if n.lower().endswith(exts))
    except (OSError, ImportError):
        return []
    return [os.path.join(p, n) for n in names]


def _probe_chip(path, entry):
    """芯片音源：填轨数 / 总时长。

    只读头，**不解码**。WSR 走同目录 m3u（零成本，曲号/曲名/时长都有），
    没有 m3u 的才回落到实测扫描（约 1.5 秒，按 path+mtime+size 缓存）。

    以前芯片音源没有这个分支，左栏一律显示「芯片音源」而不是「33 轨」——
    用户看不出这个包到底有多少首，配上没有进度的转换就很容易以为卡死了。
    """
    try:
        import chip_formats as _cf
    except ImportError:
        return
    files = _chip_files(path)
    if not files:
        return
    # 只探前 16 个（一个包通常就 1 个），多出来的按均值外推 —— 免得一个装着
    # 上百个无 m3u 的 WSR 的目录在「添加音源」时卡几分钟。
    head = files[:16]
    n_tracks, total = 0, 0.0
    for f in head:
        try:
            info = _cf.probe(f)
        except Exception:                        # noqa: BLE001 只是显示用
            continue
        if not info.ok:
            continue
        n_tracks += info.n_tracks or 0
        # ⚠ 别自己重算时长。这里以前只认 ``track_lengths``（m3u 的逐轨长度），
        #   ``exact_secs`` 完全没看 —— 而 VGM 的采样数、GBS 的 loop_end、
        #   GYM 的帧数、S98 的 tick 都是「文件自带的精确时长」，全落在
        #   ``exact_secs`` 里。结果是这些格式在左栏**一律显示 150 秒**
        #   （实测：1.2 秒的 .s98 显示成 150 秒）。
        #   ``ChipInfo.total_secs`` 已经按
        #   「track_lengths → exact_secs → loop_end_secs」的优先级算好了。
        got = float(info.total_secs or 0)
        total += got if got > 0 else float(info.n_tracks or 0) * 150.0
    if len(files) > len(head) and n_tracks:
        k = float(len(files)) / float(len(head))
        n_tracks = int(round(n_tracks * k))
        total *= k
    entry["n_tracks"] = n_tracks
    entry["total_secs"] = total


def _probe_stream(path, entry):
    """给流式音频（``.adx`` / ``.hwas``）补时长 —— 只读头，不解码。

    一条文件就是一轨。``.hwas`` 头里有采样率和字节数，一除就出来；
    ``.adx`` 交给 PyAV 读流时长。两个都是毫秒级，几十个文件也不卡。
    """
    entry["n_tracks"] = 1
    try:
        entry["total_secs"] = float(stream_audio.probe(path).get("secs") or 0.0)
    except Exception:                            # noqa: BLE001 只是显示用
        pass


def _probe_tracks(src):
    """把一个音源的**曲目清单**列出来（只读头/文本，不解码音频）。

    这是「中间那张表」的数据源。以前中间表直接抄左栏的音源列表，两处显示
    同一批东西，纯属浪费屏位 —— 用户要看的是「这一张盘里到底有哪些曲子」。

    返回 ``[{"n", "title", "secs", "kind"}, …]``；列不出来就返回空 + 说明。
    """
    p, kind = src["path"], src["kind"]
    rows = []

    if kind == "光盘镜像":
        if os.path.isdir(p):
            try:
                cues = [os.path.join(p, n) for n in sorted(os.listdir(p))
                        if n.lower().endswith(".cue")]
            except OSError:
                cues = []
        else:
            cues = [p]
        plan = []
        for one in cues:
            try:
                plan += core.disc_track_plan(one)
            except RuntimeError:
                continue
        for t in plan:
            rows.append({
                "n": int(t.get("num") or len(rows) + 1),
                "title": str(t.get("title") or ""),
                "secs": round((t["end"] - t["start"]) / 75.0, 2),
                "kind": "CD-DA",
            })
        return rows

    if kind == "SPC":
        try:
            import spc_wav
        except ImportError:
            return rows
        if os.path.isdir(p):
            try:
                names = sorted(n for n in os.listdir(p) if n.lower().endswith(".spc"))
            except OSError:
                names = []
            for i, n in enumerate(names, 1):
                stem = os.path.splitext(n)[0]
                title, secs = stem, 0.0
                try:
                    t, _g, s, _f = spc_wav.probe(os.path.join(p, n))
                    title, secs = (t or stem), float(s or 0)
                except Exception:                # noqa: BLE001 单个坏文件不影响列表
                    pass
                rows.append({"n": i, "title": title, "secs": secs, "kind": "SPC"})
            return rows
        try:
            title, _g, secs, _f = spc_wav.probe(p)
        except Exception:                        # noqa: BLE001
            title, secs = src["label"], 0.0
        rows.append({"n": 1, "title": title or src["label"],
                     "secs": float(secs or 0), "kind": "SPC"})
        return rows

    if kind == "PSX 音频":
        try:
            import psx_txtp
        except ImportError:
            return rows
        # 目录：一个 .txtp 一行（PS1 包解压后的原样）
        if os.path.isdir(p):
            try:
                titles = psx_txtp.load_titles(p)
            except OSError:
                titles = {}
            for i, one in enumerate(psx_txtp.list_txtp(p), 1):
                stem = os.path.basename(one)
                try:
                    pr = psx_txtp.probe_txtp(one)
                except Exception:                # noqa: BLE001 单个坏索引不影响列表
                    pr = {}
                rows.append({
                    "n": i,
                    "title": titles.get(stem) or os.path.splitext(stem)[0],
                    "secs": round(float(pr.get("seconds") or 0), 2),
                    "kind": pr.get("kind") or "PSX",
                })
            return rows
        pr = {}
        try:
            pr = psx_txtp.probe_txtp(p)
        except Exception:                        # noqa: BLE001
            pr = {}
        title = src["label"]
        try:
            stem = os.path.basename(p)
            title = psx_txtp.load_titles(
                os.path.dirname(os.path.abspath(p))).get(stem) or src["label"]
        except Exception:                        # noqa: BLE001
            pass
        # 直接加的 .xa：一个文件可能有多条子流，要按**子流**列，别缩成一行
        # （否则用户以为「15 个文件 = 15 首」，实际转换出来是另一回事）。
        # 排序与 psx_txtp.decode_xa_tracks 一致：扇区多的在前。
        if psx_txtp.is_xa(p):
            try:
                counts = psx_txtp._group_counts(p)      # noqa: SLF001 同一个模块的缓存
            except Exception:                           # noqa: BLE001
                counts = {}
            if len(counts) > 1:
                keys = sorted(counts, key=lambda k: (-counts[k], k))
                stem_name = os.path.splitext(os.path.basename(p))[0]
                for i, k in enumerate(keys, 1):
                    rows.append({
                        "n": i,
                        "title": "%s #%d" % (stem_name, i),
                        "secs": round(counts[k] * core._XA_SECTOR_SECS, 2),
                        "kind": "XA#%d" % i,
                    })
                return rows
        rows.append({
            "n": 1,
            "title": title,
            "secs": round(float(pr.get("seconds") or 0), 2),
            "kind": pr.get("kind") or "PSX",
        })
        return rows

    if kind == "PSF":
        # 一个 .psf 就是一轨。以前这里没分支，中间那张表对 PSF 音源是**空白**，
        # 用户以为没加进去。
        secs = 0.0
        try:
            import psf_native
            secs = float(psf_native.length_tag(p) or 0.0)
        except Exception:                        # noqa: BLE001 只是显示用
            pass
        rows.append({"n": 1, "title": src["label"], "secs": round(secs, 2),
                     "kind": "PSF"})
        return rows

    if kind == "流式音频":
        # 一条文件就是一轨（.adx / .hwas 都是「一首成品」，不是多曲容器）。
        secs = 0.0
        try:
            secs = float(stream_audio.probe(p).get("secs") or 0.0)
        except Exception:                        # noqa: BLE001 只是显示用
            pass
        ext = os.path.splitext(p)[1].lstrip(".").upper() or "STREAM"
        rows.append({"n": 1, "title": src["label"], "secs": round(secs, 2),
                     "kind": ext})
        return rows

    if kind == "芯片音源":
        # 只有「带曲目表」的芯片音源才列得出来（WSR 的同目录 m3u 就是这种，
        # 有曲号/曲名/真实时长）。NSF/GBS 那些的曲名要靠 libgme 解码才知道，
        # 这里不为了显示一张表去解一遍音频 —— 返回空，界面照旧显示提示。
        files = _chip_files(p)
        if len(files) != 1:
            # 一个包里多个芯片文件时，逐文件混成一张表会看不出归属，不如不列
            return rows
        try:
            import chip_formats as _cf
            info = _cf.probe(files[0])
        except Exception:                        # noqa: BLE001 只是显示用
            return rows
        if not info.ok:
            return rows
        titles = info.track_titles or {}
        lens = info.track_lengths or {}
        ids = info.track_ids or []
        if not ids or not (titles or lens):
            return rows
        for i, no in enumerate(ids, 1):
            rows.append({
                "n": i,
                "title": titles.get(no) or ("曲 %02d" % i),
                "secs": round(float(lens.get(no) or 0.0), 2),
                "kind": "WSR" if info.fmt == "WSR" else (info.fmt or ""),
            })
        return rows

    if kind == "MSU-1" and os.path.isdir(p):
        try:
            names = sorted(n for n in os.listdir(p) if n.lower().endswith(".pcm"))
        except OSError:
            names = []
        for i, n in enumerate(names, 1):
            full = os.path.join(p, n)
            try:
                secs = max(0.0, (os.path.getsize(full) - 44) / 4.0 / 44100.0)
            except OSError:
                secs = 0.0
            rows.append({"n": i, "title": os.path.splitext(n)[0],
                         "secs": round(secs, 2), "kind": "PCM"})
        return rows

    return rows


def _native_status(modname):
    """外挂解码器链路可用性：模块 + 那个不是 Python 模块的 exe，两件分开报。

    只报模块会漏掉最典型的坏包 —— ``wsr_native.py`` / ``kss_native.py``
    打进去了、但 ``wsr2wav.exe`` / ``kss2wav.exe`` 忘了带（它们是普通文件，
    PyInstaller 靠 ``EXTRA_EXE`` 才带上）。那时界面表现是
    「.wsr / .kss 加得进来、一按转换就说找不到解码器」。
    """
    try:
        mod = __import__(modname)
    except Exception as exc:                    # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    ok, why = mod.decoder_ready()
    return {"ok": ok, "decoder": os.path.basename(why) if ok else "",
            "decoder_path": why if ok else "", "why": "" if ok else why}


def _wsr_status():
    """WonderSwan：``wsr_native`` + ``wsr2wav.exe``。"""
    return _native_status("wsr_native")


def _kss_status():
    """KSS（MSX / GG / SMS）：``kss_native`` + ``kss2wav.exe``（libkss）。

    v5.8 起 KSS 不再走 libgme —— 它的 KSS 实现不映射 0x4000-0x7FFF，
    32 KB 的 Game Gear rip 会每轨静音且**不报错**。
    """
    return _native_status("kss_native")


def _s98_status():
    """S98（PC-98 / PC-88 的 YM2608）：``s98_native`` + ``s982wav.exe``（v5.9.13）。

    和 WSR / KSS / QSF 同一个坑：``s98_native.py`` 进了 PYZ，
    ``s982wav.exe`` 靠 ``EXTRA_EXE`` 才带 —— 少了的表现是
    「.s98 加得进来、一按转换说找不到解码器」。

    ⚠ 和 ``vgm`` 段**不是一回事**：那段是 vgmstream（Wwise/FMOD 容器），
    这段是 ymfm（YM2608 芯片模拟）。两者都是第三方解码器，但没有任何共用文件。
    """
    st = _native_status("s98_native")
    try:
        st["exts"] = list(__import__("s98_native").S98_EXTS)
    except Exception:                           # noqa: BLE001
        st["exts"] = []
    return st


def _qsf_status():
    """Capcom QSound 街机：``qsf_native`` + ``qsf2wav.exe``（v5.9.10）。

    和 WSR / KSS 同一个坑：``qsf_native.py`` 进了 PYZ，``qsf2wav.exe``
    靠 ``EXTRA_EXE`` 才带 —— 少了的表现是「.miniqsf 加得进来、
    一按转换说找不到解码器」。

    ⚠ 这里**没有 BIOS 那一层**。QSound 是街机板上的独立声音子系统，
    没有主机 BIOS 这回事；照抄 ``psf`` 的 diag 结构会让人误以为缺 BIOS。
    """
    return _native_status("qsf_native")


def _pivotal_status():
    """GameCube 的 Pivotal PSF（v5.9.11）—— **纯 Python，没有外挂 exe**。

    和 WSR / KSS / QSF 那些「模块 + exe 两件套」不同，这条路只需要
    ``pivotal_psf.py`` + numpy，所以只报模块在不在、采样率是多少。

    ⚠ 但「模块在」这件事**必须在这里可见**：``convert_job`` /
    ``dl_convert`` / ``psx_txtp`` 里都是**判魔数才 import** 的延迟导入，
    PyInstaller 静态分析看不到 —— 打漏了的表现是「.psf 一转换就
    ModuleNotFoundError」，界面上完全看不出少的是哪一环。
    """
    try:
        import pivotal_psf
    except Exception as exc:                    # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "decoder": "pivotal_psf.py（纯 Python）",
            "rate": int(getattr(pivotal_psf, "OUT_RATE", 0)),
            "channels": int(getattr(pivotal_psf, "OUT_CH", 0)),
            "why": ""}


def _vgm_status():
    """Wwise / FMOD 容器：``vgm_audio`` + ``vgmstream-cli.exe``（v5.9.12）。

    ⚠ **全工程唯一一个随包带第三方解码器的模块**。FFmpeg 明确不支持这两族：
    ``.wem``（Wwise Custom Vorbis）能开容器但 ``codec_context`` 是 None；
    ``.fsb``（FMOD FSB5）直接 ``PatchWelcomeError: Not yet implemented``。

    和 WSR / KSS / QSF 一样是「模块 + exe 两件套」，但**还多一层**：
    exe 旁边必须有 **11 个 DLL**，缺一个就是 ``exit 127``（载入时硬依赖）。
    所以这里把 ``available()`` 的第二个返回值（缺失说明）原样报出来，
    否则用户只会看到「.wem 一转换就说找不到解码器」，根本不知道少的是
    哪一环。``vgm_audio.decoder_ready()`` 里已经做了这层自检。
    """
    try:
        import vgm_audio
    except Exception as exc:                    # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    ok, why = vgm_audio.decoder_ready()
    return {"ok": bool(ok),
            "decoder": os.path.basename(why) if ok else "",
            "decoder_path": why if ok else "",
            "exts": list(getattr(vgm_audio, "EXTS", ())),
            "why": "" if ok else why}


def _diag():
    """``GET /api/diag`` —— 「为什么这个功能不能用」的第一手证据。

    打包后最常见的问题是**某个可选依赖没带上**（libgme / ffmpeg / soundfile），
    表现是某个格式一转就报错，而界面上看不出是哪一环。这里一次把所有
    关键依赖的可用性列出来，省得靠猜。
    """
    def has(name):
        try:
            __import__(name)
            return True
        except Exception:                    # noqa: BLE001
            return False

    gme = "ok"
    try:
        core._load_gme()                     # 真的把 libgme.dll 及其依赖载进来
    except Exception as exc:                 # noqa: BLE001
        gme = "ERR: %s" % exc

    # PSF 系：模块 + 那个不是 Python 模块的 psf2wav.exe + BIOS，三件分开报。
    # 只报模块会漏掉最典型的一种坏包（.py 进去了、.exe 忘了带）。
    psf = {"ok": False, "error": "psf_native 没打进来"}
    try:
        import psf_native
        st = psf_native.status()
        psf = {"ok": st["available"], "decoder": st["decoder"],
               "decoder_path": st["decoder_path"], "bios": st["bios"],
               "bios_kind": st["bios_kind"], "why": st["why"]}
    except Exception as exc:                 # noqa: BLE001
        psf = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    return {
        "ok": True,
        "app": APP_ID,
        "version": APP_VERSION,
        "frozen": bool(getattr(core, "_IS_FROZEN", False)),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "temp": tmpdir_root(),
        "modules": {
            "numpy": has("numpy"),
            "av": has("av"),
            "soundfile": has("soundfile"),
            "Crypto": has("Crypto"),
            "chip_formats": has("chip_formats"),
            "chip_decode": has("chip_decode"),
            "cd_audio": has("cd_audio"),
            "wavmerge": has("wavmerge"),
            "cloud_upload": has("cloud_upload"),
            "psx_txtp": has("psx_txtp"),
            "psf_native": has("psf_native"),
            "wsr_native": has("wsr_native"),
            # v5.9.10: Capcom QSound 的 QSF。和 wsr_native / kss_native 一样
            # 是 convert_job / dl_convert 里的延迟 import，静态分析看不到。
            # 模块在不算数 —— qsf2wav.exe 是否带上由下面的 "qsf" 段单独报。
            "qsf_native": has("qsf_native"),
            # v5.9.11: GameCube 的 Pivotal PSF（.psf 撞名的另一支）。同样是
            # 延迟 import —— 三处调用点都先判魔数再决定走不走它。纯 Python，
            # 没有外挂 exe，所以只在这里报模块；采样率由下面的 "pivotal" 段报。
            "pivotal_psf": has("pivotal_psf"),
            # v5.9.12: Wwise（.wem）/ FMOD（.fsb）走 vgmstream。同样是延迟
            # import（只有真遇到这两种扩展名才付一次进程启动的账）。
            # 模块在不算数 —— exe + 11 个 DLL 是否带上由下面的 "vgm" 段报。
            "vgm_audio": has("vgm_audio"),
            # v5.8: KSS 改走 libkss。和 wsr_native 同一个分流点（__import__
            # 动态加载），静态分析同样看不到 —— 必须在这里可见。
            "kss_native": has("kss_native"),
            # v5.9.13: S98（PC-98 的 YM2608）。同一个分流点（第三个
            # __import__ 动态加载），静态分析看不到 —— 必须在这里可见。
            "s98_native": has("s98_native"),
            # v5.6: 3DO 的 AIFF/AIFC。**必须在 diag 里可见** —— 它是
            # stream_audio 里的函数内 import，PyInstaller 静态分析看不到，
            # 打漏了的表现是「3DO 包一点转换就 ModuleNotFoundError」，
            # 而界面上完全看不出少的是哪一环。这里直接暴露模块是否在包里。
            "aif_audio": has("aif_audio"),
            # 下载页那套（v5.1）。放主 diag 里是因为它们和主流水线共用
            # 内核模块（dl_convert 依赖 cd_audio/chip_decode/wavmerge），
            # 打漏了表现是「下载页能开、一按开始就 ModuleNotFoundError」。
            "joshw_source": has("joshw_source"),
            "joshw_dl": has("joshw_dl"),
            "dl_convert": has("dl_convert"),
            "zh_titles": has("zh_titles"),
            "zh_online": has("zh_online"),
            "py7zr": has("py7zr"),
            # v5.9.7：FLAC 输出。模块在不算数 —— 还要 libsndfile 真带
            # FLAC 编码器，见下面的 "flac" 段（模块在、编码器缺的包是
            # 真实存在的：某些精简版 libsndfile 只编 WAV/OGG）。
            "flac_out": has("flac_out"),
        },
        "libgme": gme,
        "psf": psf,
        # WSR：模块 + 那个不是 Python 模块的 wsr2wav.exe，分开报
        # （只报模块会漏掉最典型的坏包：.py 进去了、.exe 忘了带）。
        "wsr": _wsr_status(),
        # KSS：同上，模块 + kss2wav.exe 分开报（v5.8）
        "kss": _kss_status(),
        # S98（v5.9.13）：同上，模块 + s982wav.exe 分开报。
        # ⚠ 和下面的 "vgm" 段不是一回事 —— 那是 vgmstream（Wwise/FMOD 容器），
        # 这是 ymfm（YM2608 芯片模拟），两者没有共用文件。
        "s98": _s98_status(),
        # QSF：同上，模块 + qsf2wav.exe 分开报（v5.9.10）。
        # ⚠ **没有 BIOS 字段** —— QSound 是街机板上的独立声音子系统。
        "qsf": _qsf_status(),
        # Pivotal PSF（v5.9.11）：纯 Python，只报模块 + 采样率。
        # ⚠ 和上面的 psf 段**不是一回事** —— 那是 PS1/PS2 的 PSF（要靠
        # psf2wav.exe + BIOS），这是 GameCube 的 Pivotal 变体（自带解码）。
        # 两者扩展名都叫 .psf，靠文件头第 4 字节区分。
        "pivotal": _pivotal_status(),
        # VGM（v5.9.12）：Wwise .wem / FMOD .fsb，模块 + vgmstream-cli.exe
        # + **11 个 DLL** 一起报。这是全工程唯一随包带第三方解码器的地方，
        # 也是唯一「缺一个 DLL 就 exit 127」的链路 —— 必须在这里一眼看到。
        "vgm": _vgm_status(),
        # FLAC（v5.9.7）：available() 会真去问 libsndfile 要格式表，
        # 比 has("soundfile") 强 —— 后者为真但编不了 FLAC 的情况是有的。
        "flac": _flac_status(),
        # MP3（v5.9.15）：同一个问法。libsndfile 自带 LAME，正常情况下
        # 一定可用；打包漏了 DLL 时这里会是 false，前端据此隐藏 MP3 卡片。
        "mp3": _fmt_status("mp3"),
        # OGG（v6.0.0）：同一个问法，libsndfile 自带 libvorbis。
        # 前端的 FMT_CARDS 按这个键探测，缺了就是「OGG 卡片永远灰着」。
        "ogg": _fmt_status("ogg"),
    }


def _fmt_status(fmt="flac"):
    """某个编码容器的可用性。**永不抛异常** —— diag 自己不能因为探测而挂掉。

    v5.9.15：从 ``_flac_status()`` 抽出来。``available()`` 返回
    ``(bool, 原因)``，原因在可用时是空串、不可用时是一句人话（缺哪个模块 /
    哪个格式没编进去），直接透给前端显示。
    """
    try:
        import flac_out
        ok, why = flac_out.available(fmt)
        st = {"ok": bool(ok), "why": why}
        if str(fmt).lower() == "flac":
            # 只有 FLAC 有压缩级别这个旋钮（MP3 走 LAME 默认质量）。
            st["level"] = flac_out.LEVEL
        return st
    except Exception as exc:                 # noqa: BLE001
        return {"ok": False, "why": "%s: %s" % (type(exc).__name__, exc)}


def _flac_status():
    """FLAC 编码器状态（``_fmt_status("flac")`` 的别名，保留老调用点）。"""
    return _fmt_status("flac")


def tmpdir_root():
    try:
        import tmpdir
        return tmpdir.root()
    except Exception:                        # noqa: BLE001
        return ""


def drop_root():
    """浏览器拖入的文件落盘根（**不是**临时目录）。

    ⚠ 为什么不能用 ``tmpdir``：

    1. 默认输出目录是「音源同级的 output」，音源在临时目录里就会把输出
       也算进临时目录 —— 用户转完根本找不到 WAV。
    2. ``tmpdir.sweep()`` 会按前缀清旧目录，拖入的东西会被当垃圾删掉。

    所以和下载页 ``DownloadState._default_dest`` 同一套逻辑：落在
    「临时目录所在盘 / MSU1Downloads / 拖入」下，看得见、也好找。
    """
    try:
        import tmpdir
        if not tmpdir.is_fallback():
            base = os.path.join(os.path.dirname(tmpdir.root()) or "D:\\",
                                "MSU1Downloads")
        else:
            base = os.path.join(os.path.expanduser("~"), "MSU1Downloads")
    except Exception:                        # noqa: BLE001
        base = os.path.join(os.path.expanduser("~"), "MSU1Downloads")
    return os.path.join(base, "拖入")


def _psf_status(force=False):
    """PSF 系现在能不能用（界面右上那块「PS1 音源」的状态就是它）。

    三件事分开报，因为坏法不同、修法也不同：

    * ``decoder`` 假 → ``psf2wav.exe`` 没跟主程序放在一起
    * ``bios`` 空    → 用户还没指定 BIOS（这是**最常见**的一种）
    * 两者都真       → 可用
    """
    try:
        import psf_native
    except Exception as exc:                 # noqa: BLE001
        return {"ok": False, "available": False, "decoder": False,
                "decoder_path": "", "bios": "", "bios_kind": "",
                "why": "psf_native 模块没打进来：%s" % exc,
                "hint": "", "config": "", "exts": []}
    d = psf_native.status(force=force)
    d["ok"] = True
    return d


def _cloud_status():
    """云盘账号状态（两个页面的「上传云盘」按钮都靠它决定下一步）。

    包一层是为了「模块没打进包」时也能给出一句人话 —— 直接让
    ``ImportError`` 冒到前端，用户看到的是 ``HTTP 500``，看不出是漏件。
    """
    try:
        from web_ui.cloud_api import CloudAuth      # noqa: F401
    except Exception as exc:                 # noqa: BLE001
        return {"ok": False, "logged_in": False, "why":
                "cloud_api 模块没打进来：%s" % exc,
                "deps": {}, "job": None, "qr": {}, "hint": "",
                "user_name": "", "can_upload": False, "can_browser": False}
    return cloud_auth().status()


def _cloud_plan(folder, games=None):
    """列出某个目录里「能传什么」，供前端弹「合并 / 分轨 / 全传」三选一。

    判定全在 ``cloud_api.plan_upload`` 里 —— HTTP 层只负责把目录解析出来，
    别把「什么算合并文件」这种口径写进这里（两处会漂移）。

    ``games`` 非空时只算这几个游戏子目录（``WAV/<游戏名>/``）里的文件。
    下载页的产物就是按游戏分目录的，用户要能挑「这次只传这几个」。
    """
    try:
        from web_ui.cloud_api import plan_upload
    except Exception as exc:                 # noqa: BLE001
        return {"ok": False, "error": "cloud_api 模块不可用：%s" % exc}
    folder = str(folder or "").strip()
    if not folder or not os.path.isdir(folder):
        return {"ok": False, "error": "目录还不存在，先转换一次", "dir": folder}
    try:
        import cloud_upload as cu
        # ⚠ ``collect_wavs(*folders)`` 是**可变参数**，别传列表：
        #   传 ``[folder]`` 会让它拿列表去 ``os.path.isdir`` 然后 TypeError。
        #   （这条以前就是这么写的 —— 主界面「上传云盘」一按就 500。）
        # ⚠ ``deep=True`` 是必须的：下载页的产物在 ``WAV/<游戏名>/`` 里，
        #   只扫一层会一首也找不到 → 「这个目录里没有 WAV」（v5.9.4 修的）。
        wavs = cu.collect_wavs(folder, deep=True)
    except Exception as exc:                 # noqa: BLE001
        return {"ok": False, "error": "收集 WAV 失败：%s" % exc, "dir": folder}
    want = {str(g).lower() for g in (games or ()) if g}
    if want:
        keep = []
        for p in wavs:
            rel = os.path.relpath(p, folder).replace("\\", "/")
            if rel.split("/")[0].lower() in want:
                keep.append(p)
        wavs = keep
    if not wavs:
        return {"ok": False,
                "error": "选中的游戏里没有 WAV" if want else "这个目录里没有 WAV",
                "dir": folder}
    d = plan_upload(wavs)
    d["dir"] = folder
    d["count"] = len(wavs)
    return d


def _shade(hex_color, amount):
    """把 ``#rrggbb`` 调亮（``amount > 0``）或调暗（``amount < 0``）。

    ``amount`` ∈ [-1, 1]。调不亮/调不暗就原样返回 —— 复古那 6 套配色里有
    纯黑纯白，任何异常都不该让主题面板整个空掉。
    """
    h = str(hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return hex_color
    if amount >= 0:
        r, g, b = (int(v + (255 - v) * amount) for v in (r, g, b))
    else:
        r, g, b = (int(v * (1 + amount)) for v in (r, g, b))
    return "#%02X%02X%02X" % tuple(max(0, min(255, v)) for v in (r, g, b))


def _rgba(hex_color, alpha):
    """``#rrggbb`` + 透明度 → ``rgba(r, g, b, a)``。"""
    h = str(hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "rgba(0, 0, 0, %s)" % alpha
    return "rgba(%d, %d, %d, %s)" % (r, g, b, alpha)


#: ``retro_palette`` 的键 → ``app.css`` 的变量名。
#:
#: ⚠ 这套映射是「6 套复古主题能不能真的套上」的唯一真相。app.css 里
#: ``html[data-theme="dark"]`` 定义了 26 个变量，这里必须**一个不落** ——
#: 少一个的表现是「切过去某个控件颜色没变」，而且只有那一处看得出来。
def _palette_vars(pal, on_accent):
    cyan = pal.get("cyan") or "#8B5CF6"
    magenta = pal.get("magenta") or "#22D3EE"
    line = pal.get("line") or "#888888"
    card2 = pal.get("card2") or pal.get("card") or "#EEEEEE"
    green = pal.get("green") or "#2F7A3D"
    amber = pal.get("amber") or "#9D6412"
    return {
        "--bg": pal.get("bg") or "#F5F5F8",
        "--glow-a": _rgba(cyan, ".10"),
        "--glow-b": _rgba(magenta, ".06"),
        "--surface": pal.get("panel") or "#FFFFFF",
        "--surface-2": pal.get("card") or "#F7F7FA",
        "--surface-3": card2,
        "--surface-hi": _shade(card2, -0.06),
        "--border": _rgba(line, ".55"),
        "--border-2": line,
        "--border-3": _shade(line, -0.12),
        "--text": pal.get("text") or "#15151A",
        "--text-2": pal.get("muted") or "#5E5E6C",
        "--text-3": pal.get("dim") or "#9A9AA8",
        "--accent": cyan,
        "--accent-2": magenta,
        "--accent-soft": _rgba(cyan, ".14"),
        "--accent-line": _rgba(cyan, ".5"),
        # 复古配色里的强调色本来就按「浅底上够读」选的（WCAG 过了），
        # 所以直接用 cyan，不用再调亮
        "--accent-text": cyan,
        "--ok": green,
        "--ok-soft": _rgba(green, ".14"),
        "--warn": amber,
        "--warn-soft": _rgba(amber, ".14"),
        "--shadow-1": "0 1px 2px rgba(0,0,0,.10)",
        "--shadow-2": "0 2px 4px rgba(0,0,0,.07), 0 12px 32px rgba(0,0,0,.09)",
        "--grad": "linear-gradient(135deg, %s, %s)" % (cyan, magenta),
        "--grad-soft": "linear-gradient(135deg, %s, %s)"
                       % (_rgba(cyan, ".18"), _rgba(magenta, ".08")),
        "--scheme": "light",
        # 主按钮上的文字色（深色强调色要配浅字）
        "--on-accent": on_accent or "#FFFFFF",
    }


def _themes_payload():
    """全部主题：2 套现代（前端写死在 app.css 里）+ 6 套复古（这里翻成变量）。

    复古那 6 套配色原本只给 tkinter / Qt 版用（``retro_palette.THEMES``，
    纯数据、零依赖）。这里把它翻成 CSS 变量直接喂前端 —— 老用户切到 Web 版
    还是原来那个味道，不用重新适应一套新配色。

    前端拿到后自己 ``style.setProperty``，**不依赖 app.css 里任何默认值** ——
    因为 ``data-theme="retro"`` 不匹配 ``html[data-theme="dark"|"light"]``，
    那两个块里的变量根本不会生效。
    """
    themes = [
        {"key": "dark", "label": "深色", "group": "现代",
         "swatch": ["#8B5CF6", "#22D3EE", "#09090C"], "vars": {}},
        {"key": "light", "label": "浅色", "group": "现代",
         "swatch": ["#6D4AFF", "#0891B2", "#F5F5F8"], "vars": {}},
    ]
    try:
        import retro_palette as rp
    except Exception:                        # noqa: BLE001 没打进来就只给 2 套
        rp = None
    if rp is not None:
        for key in rp.THEME_ORDER:
            entry = rp.THEMES.get(key) or {}
            pal = entry.get("palette") or {}
            if not pal:
                continue
            themes.append({
                "key": "retro:" + key,
                "label": entry.get("label") or key,
                "group": "复古",
                "swatch": [pal.get("cyan"), pal.get("magenta"), pal.get("bg")],
                "vars": _palette_vars(pal, entry.get("on_accent")),
            })
    return {"ok": True, "themes": themes}


def _pick_scope(cu, wavs, body):
    """按前端选的范围挑出要传的 WAV，返回 ``(列表, 空时的原因)``。

    真正的口径在 ``cloud_api.pick_scope`` 里 —— 下载页也要用同一份判定，
    放两处迟早漂移（本项目已有两窗同构代码打错靶的先例）。
    """
    from web_ui.cloud_api import pick_scope
    return pick_scope(wavs, scope=body.get("scope") or "",
                      merged=body.get("merged"))


def default_out_dir(src):
    """音源路径 → 默认输出目录（**单一实现点**）。

    规则是「output 放在**音源所在那一层**」：

    * **目录音源** → ``<父级>\\<包名>output``
    * **文件音源** → ``<所在目录>\\output``

    ⚠ 两条都踩过坑，改之前先读：

    1. 目录音源如果算成 ``<包名>\\<包名>output``（把 output 放进目录内部），
       路径长度直接翻倍。实测一个 NDS 包（``Ace Attorney Investigations -
       Miles Edgeworth [Gyakuten Kenji] ...``）因此撞上 Windows
       MAX_PATH(260)，报 **「Errno 2 No such file or directory」** —— 这个
       错误码看起来像「目录没建」，而 ``ConvertJob.run()`` 开头明明有
       ``makedirs``。超长路径在 Windows 上就是这个码。
    2. 文件音源不能拼文件名（``101 xxx Prologueoutput``）—— 一个包里几十个
       ``.mini2sf`` 会各自建一个目录，编号也断成每目录从 01 起。
    """
    p = str(src or "").rstrip("\\/")
    if os.path.isdir(p):
        parent = os.path.dirname(p) or p
        return os.path.join(parent, os.path.basename(p) + "output")
    return os.path.join(os.path.dirname(p) or ".", "output")


def default_opts():
    return {
        "out_dir": "",
        # v5.4：输出格式**默认一个都不勾**。以前默认全勾（BIN+CUE 和 WAV
        # 都出），一次转换白写两倍数据；用户明确要哪一种就勾哪一种。
        # 前端点「开始转换」时也会挡住「两个都没选」的情况。
        "do_cd": False,
        "do_wav": False,
        #: 分轨音频的容器：``"wav"`` / ``"flac"``。**只在 do_wav 为真时有意义**。
        #: 两者音频规格完全相同（16bit/44100/立体声），FLAC 逐样本无损、
        #: 体积约 35% —— 换的只是容器，不是音质。
        "fmt": "wav",
        "policy": "auto",
        "speed": 0.0,
        "max_secs": 150.0,
        "default_secs": 150.0,
        "gap": 2.0,
        "album": "",
        "performer": "",
        "verify": False,
        "cd_wav": False,
        "xa_music_only": False,
        "xa_split": False,
        "merge_wav": False,
    }


#: 目录递归扫描的深度上限。游戏音乐目录常见「游戏名/专辑/曲目」两三层，
#: 再深基本是人为嵌套；限死是为了防止用户误选盘符根目录时把界面卡死。
_SCAN_DEPTH_MAX = 6

#: 一次最多收多少个音源。5000 已远超正常需求（一个游戏几十首），
#: 命中上限基本说明选错了目录 —— 宁可截断并提示，也不要让界面卡住。
_SCAN_ITEMS_MAX = 5000

#: 可解码音源扩展名（小写）。单一真源在 ``chip_formats.ALL_EXTS``，
#: 这里只做一次大小写归一，避免 ``endswith`` 区分大小写漏掉 ``.SPC``。
#:
#: 末尾额外加的 ``.xa``（v5.2.4）与流式音频 ``.adx``/``.hwas``（v5.2.6）
#: **故意不进 ALL_EXTS**：那份清单是「交给 libgme 解码」的口径，而这几类
#: 由 ``psx_txtp`` / ``stream_audio`` 原生解，混进去会让 tkinter 版
#: （v4.44 留档）的批量转换去调错解码器。
#:
#: ⚠ v5.4 补上了 USF / 2SF / SSF-DSF / GSF 四家族与 ``.txtp``。
#:   缺了它们的症状极隐蔽：``classify()`` 对这些目录**故意**返回「未知」
#:   （为了逐文件展开、一首坏掉只废那一首），于是走到 ``scan_dir_sources``；
#:   而那里只认 ``_SRC_EXTS`` —— 结果「一个装着 45 个 .mini2sf 的目录」
#:   扫出来是 **0 个音源**，界面表现就是「添加文件夹后什么都没有」。
#:   实测：``12 Family Games (2010-10)(-)(Phoenix)[NDS]`` 正是这样。
_SRC_EXTS = (tuple(e.lower() for e in chip_formats.ALL_EXTS)
             + (".xa", TXTP_EXT.lower())
             + tuple(stream_audio.ALL_EXTS)
             + tuple(USF_EXTS) + tuple(VIO_EXTS)
             + tuple(SSF_EXTS) + tuple(GSF_EXTS))


def _package_subdirs(d):
    """直接子目录里有多少个本身就能归成音源（也就是「一个包」）。"""
    n = 0
    try:
        for name in os.listdir(d):
            sub = os.path.join(d, name)
            if os.path.isdir(sub) and classify(sub) != "未知":
                n += 1
    except OSError:
        pass
    return n


def _no_source_reason(path):
    """文件夹里一个能转的都没有 → 说清为什么（v5.2.6）。

    原来只会回「文件夹里没有可识别的音源」—— 用户选了 ``Nintendo DS`` 下面
    某个游戏目录，看到的就这一句，既不知道是下错了还是程序不支持。
    现在按扩展名给原因（内存快照要模拟器核心 / 容器已解但负载没破 /
    libgme 没编该机种），见 ``dl_convert.SKIP_REASONS`` 与 README ⚠58。
    """
    hint = dl_convert.explain_skipped(dl_convert.walk_files(path))
    return hint or "文件夹里没有可识别的音源"


def scan_dir_sources(root):
    """把 ``root`` 展开成**音源路径列表**（目录和文件混合），返回 (列表, 是否截断)。

    ⚠ 规则必须和 :func:`convert_job.classify` 的分级保持一致，否则会把
    「一个包」拆散：

    * **子目录本身能被 classify 归类**（含 ``.txtp`` 的 PS1 下载包、含
      ``.pcm`` 的 MSU-1 包、``.cue``+镜像、一堆 ``.spc`` …）→ 把**这个子目录**
      当成一个音源加进去。一个包就是一个音源，曲目在包里，左栏不该被
      23 个 txtp 刷屏。
    * 归不了类 → 继续往下走（典型是 ``D:\\音乐\\`` 下面一层一个游戏）。
    * 散落在各层的可解码文件 → 各自成为一个音源。

    ⚠ 为什么不用 ``chip_formats.collect_supported()``：那个函数**只扫一层**
    （设计 A8，为芯片音源的平铺目录优化），也只会返回文件 —— 既进不了子
    目录，又表达不了「目录即音源」。用户点「添加文件夹」时选的经常是带
    子目录的层级，只扫一层会漏掉全部内容，表现就是「选了文件夹一个都没加进来」。

    排序用 ``chip_formats._natural_key``：保证 ``track2`` 排在 ``track10``
    前面，和左栏音源列表、曲目表用的是同一套规则。
    """
    out, seen = [], set()
    root = os.path.abspath(root)
    base = root.rstrip("\\/").count(os.sep)

    def push(p):
        k = p.lower()
        if k not in seen:
            seen.add(k)
            out.append(p)

    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort(key=chip_formats._natural_key)
            depth = dirpath.count(os.sep) - base
            descend = []
            for d in dirnames:
                sub = os.path.join(dirpath, d)
                if classify(sub) != "未知":
                    push(sub)          # 整个子目录就是一个音源包
                elif depth + 1 < _SCAN_DEPTH_MAX:
                    descend.append(d)
            dirnames[:] = descend
            for n in sorted(filenames, key=chip_formats._natural_key):
                if n.lower().endswith(_SRC_EXTS):
                    push(os.path.join(dirpath, n))
            if len(out) >= _SCAN_ITEMS_MAX:
                return out[:_SCAN_ITEMS_MAX], True
    except OSError:
        pass
    return out, False


class AppState:
    """全局状态。所有字段的读写都在 ``self.lock`` 里。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.sources = []          # [{label, path, kind, n_tracks, total_secs, size}]
        self.opts = default_opts()
        self.logs = []             # [[seq, text], ...]
        self._seq = 0
        self.state = "idle"        # idle | running | done | failed | cancelled
        self.progress = 0.0
        self.status = ""
        #: 已经**写盘完成**的曲目数（ConvertJob 的 on_track 回调）。
        #: 曲目表的「已输出 / 转换中 / 等待」必须看这个，不能拿 progress 反推 ——
        #: 解码阶段也报进度（0..50%），拿它反推会在还没写任何文件时就说
        #: 「已输出 32 首」（v5.9.5 实测拍出来的图就是这样）。
        self.done_tracks = 0
        self.total_tracks = 0
        self.result = None
        self.error = ""
        self.job = None
        self.thread = None
        self.outputs = []          # [{"id", "name", "path", "secs"}]
        self._out_seq = 0
        self._track_cache = {}     # path -> [曲目行]（曲目清单只探一次）
        #: 云盘上传的独立状态 —— 别跟转换的 progress 抢同一个字段
        self.upload = {"running": False, "progress": 0,
                       "ok": 0, "skip": 0, "fail": 0, "error": "",
                       "user": "", "count": 0}

    # -- 日志 -------------------------------------------------------------
    def log(self, msg):
        with self.lock:
            self._seq += 1
            self.logs.append([self._seq, str(msg)])
            if len(self.logs) > LOG_MAX:
                del self.logs[:len(self.logs) - LOG_MAX]

    def logs_since(self, since):
        with self.lock:
            return [row for row in self.logs if row[0] > since], self._seq

    # -- 音源 -------------------------------------------------------------
    def add_paths(self, paths):
        """加音源并探测元信息。返回 (新增数, 被跳过的 [(path, 原因)])。

        ``paths`` 里可以混着**文件夹**。文件夹分两种：

        * 本身就能被 :func:`classify` 归类（PS1 下载包、MSU-1 包、含 cue 的
          目录…）→ **整个目录就是一个音源**，直接加，不拆
        * 归不了类 → 交给 :func:`scan_dir_sources` 展开成若干音源

        这样「添加文件夹」和「加单个包」走同一条路，不会把包拆散。
        """
        added, skipped = 0, []
        expanded = []
        for raw in paths:
            p = os.path.normpath(str(raw))
            if os.path.isdir(p):
                # 目录本身能归类、而且下面没有「子包」→ 整个目录就是一个音源
                # （PS1 下载包 / MSU-1 包 / 一叠平铺的 .spc 都是这种）。
                # ⚠ 但如果它下面还套着若干子包，就必须展开 —— 否则
                #   「根目录有一个散落 .spc、子目录里才是正片」这种最常见的
                #   整理方式会被判成 SPC 包，只收 1 轨，子目录全丢。
                if classify(p) != "未知" and not _package_subdirs(p):
                    expanded.append(p)
                    continue
                found, cut = scan_dir_sources(p)
                if not found:
                    skipped.append((p, _no_source_reason(p)))
                    continue
                name = os.path.basename(p.rstrip("\\/")) or p
                self.log("   扫描「%s」：找到 %d 个音源" % (name, len(found)))
                if cut:
                    self.log("   ⚠ 音源超过 %d 个，只收前 %d 个（是不是选到盘符根目录了？）"
                             % (_SCAN_ITEMS_MAX, _SCAN_ITEMS_MAX))
                expanded.extend(found)
            else:
                # 不存在就当场判掉。否则带合法扩展名的假路径会一路走到
                # classify()，被当成音源加进列表（size=0、探测失败），
                # 用户看到的是一堆加不进来的空条目。
                if not os.path.exists(p):
                    skipped.append((p, "路径不存在"))
                    continue
                expanded.append(p)

        for p in expanded:
            with self.lock:
                if any(s["path"] == p for s in self.sources):
                    skipped.append((p, "已在列表里"))
                    continue
            kind = classify(p)
            if kind == "未知":
                ext = os.path.splitext(p)[1].lower()
                reason = dl_convert.SKIP_REASONS.get(ext)
                if reason:
                    # 8 机种那批（.minigsf/.mini2sf/.psf2/.dsf/.hwas/.dvi…）
                    # 全走这里。以前一律回「不认识的文件类型」—— 用户拿着一堆
                    # .minigsf 只会以为是自己下错了（v5.2.6 改）。
                    skipped.append((p, "%s 转不了：%s" % (ext, reason)))
                elif ext in (".psf", ".minipsf", ".psf1", ".minipsf1",
                             ".psf2", ".minipsf2"):
                    # 正常情况下 classify 会给出 "PSF"（PS1/PS2）或
                    # "GameCube 音频"（Pivotal，v5.9.11），走不到这里；
                    # 只有 psf_native 整个模块缺失时才落到这个兜底。
                    # ⚠ 文案要带上 pivotal_psf —— `.psf` 有两个主人，
                    #   只提 psf2wav.exe 会让 GC 用户找错方向。
                    skipped.append(
                        (p, "PSF 解码模块没装上（psf_native / psf2wav.exe / "
                            "pivotal_psf），请确认用的是完整包"))
                elif ext in (".miniusf", ".usf"):
                    # 同上：只有 usf_native 缺失时才落到这里。
                    # ⚠ 这两个后缀**不在** SKIP_REASONS 里（v5.3 已接上），
                    #   所以必须单独兜一句，否则会误报「不认识的文件类型」。
                    skipped.append(
                        (p, "USF 解码模块没装上（usf_native / usf2wav.exe），"
                            "请确认用的是完整包"))
                elif ext in (".mini2sf", ".2sf"):
                    skipped.append(
                        (p, "2SF 解码模块没装上（vio_native / vio2wav.exe），"
                            "请确认用的是完整包"))
                elif ext in (".ssf", ".minissf", ".dsf", ".minidsf"):
                    skipped.append(
                        (p, "SSF/DSF 解码模块没装上（ssf_native / ssf2wav.exe），"
                            "请确认用的是完整包"))
                elif ext in (".gsf", ".minigsf"):
                    # 同上：只有 gsf_native 缺失时才落到这里。
                    # ⚠ `.minigsf` **不在** SKIP_REASONS 里（v5.3 已接上）。
                    skipped.append(
                        (p, "GSF 解码模块没装上（gsf_native / gsf2wav.exe），"
                            "请确认用的是完整包"))
                elif ext in (".qsf", ".miniqsf"):
                    # 同上：只有 qsf_native 缺失时才落到这里（v5.9.10）。
                    # ⚠ 文案里**别提 BIOS** —— QSound 不需要。
                    skipped.append(
                        (p, "QSF 解码模块没装上（qsf_native / qsf2wav.exe），"
                            "请确认用的是完整包"))
                else:
                    skipped.append((p, "不认识的文件类型"))
                continue
            label = os.path.basename(p.rstrip("\\/")) or p
            if os.path.isfile(p):
                label = os.path.splitext(label)[0] or label
            entry = {"label": label, "path": p, "kind": kind,
                     "n_tracks": 0, "total_secs": 0.0, "size": 0}
            if os.path.isfile(p):
                try:
                    entry["size"] = os.path.getsize(p)
                except OSError:
                    pass
            if kind == "光盘镜像":
                try:
                    meta = core.probe_disc_audio(p)
                    entry["n_tracks"] = meta.get("n_tracks") or 0
                    entry["total_secs"] = meta.get("total_secs") or 0.0
                    entry["size"] = meta.get("size") or entry["size"]
                except (RuntimeError, OSError) as exc:
                    self.log("   ⚠ 探测 %s 失败: %s" % (os.path.basename(p), exc))
            elif kind == "SPC":
                _probe_spc(p, entry)
            elif kind == "PSX 音频":
                warn = _probe_txtp(p, entry)
                if warn:
                    self.log("   ⚠ %s 探测不全: %s" % (label, warn))
            elif kind == "PSF":
                _probe_psf(p, entry)
            elif kind == "流式音频":
                _probe_stream(p, entry)
            elif kind == "芯片音源":
                _probe_chip(p, entry)
            with self.lock:
                self.sources.append(entry)
            added += 1
            self.log("   + %s  (%s)" % (label, kind))
        return added, skipped

    # -- 输出文件 ---------------------------------------------------------
    def register_outputs(self, out_dir):
        """转换结束后扫一遍输出目录，把 WAV 登记成可试听的 id。"""
        found = []
        try:
            names = sorted(os.listdir(out_dir))
        except OSError:
            return found
        with self.lock:
            for n in names:
                if not n.lower().endswith(".wav"):
                    continue
                full = os.path.join(out_dir, n)
                self._out_seq += 1
                item = {"id": self._out_seq, "name": n, "path": full,
                        "size": os.path.getsize(full) if os.path.isfile(full) else 0}
                found.append(item)
            self.outputs = found
        return found

    def find_output(self, oid):
        with self.lock:
            for it in self.outputs:
                if it["id"] == oid:
                    return it
        return None

    # -- 曲目清单 ---------------------------------------------------------
    def tracks_of(self, idx):
        """按需探测并缓存。``None`` = 索引越界。"""
        with self.lock:
            if not (0 <= idx < len(self.sources)):
                return None
            src = dict(self.sources[idx])
        key = src["path"]
        with self.lock:
            if key in self._track_cache:
                return self._track_cache[key]
        rows = _probe_tracks(src)
        with self.lock:
            self._track_cache[key] = rows
        return rows

    # -- 快照 -------------------------------------------------------------
    def snapshot(self, since=0):
        logs, seq = self.logs_since(since)
        with self.lock:
            return {
                "state": self.state,
                "progress": round(self.progress, 1),
                "status": self.status,
                "done_tracks": self.done_tracks,
                "total_tracks": self.total_tracks,
                "error": self.error,
                "result": self.result,
                "sources": list(self.sources),
                "opts": dict(self.opts),
                "outputs": list(self.outputs),
                "upload": dict(self.upload),
                "logs": logs,
                "seq": seq,
            }


# ══════════════════════════════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════════════════════════════
def make_handler(state):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "RetroMusicLab/5.1"

        # -- 基础设施 -----------------------------------------------------
        def log_message(self, fmt, *args):
            pass                                  # 别刷屏，日志走 state.log

        def handle_one_request(self):
            """浏览器关窗口时会直接掐连接 —— 这不是错误，别打堆栈。

            默认实现在 ``rfile.readline`` 上抛 ConnectionResetError，
            socketserver 会当成「处理请求时异常」打一整段 traceback，
            关一次窗口刷一屏，日志就废了。
            """
            try:
                super().handle_one_request()
            except (ConnectionResetError, ConnectionAbortedError,
                    BrokenPipeError, TimeoutError):
                self.close_connection = True

        def end_headers(self):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            super().end_headers()

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, msg, code=400):
            self._json({"ok": False, "error": str(msg)}, code)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:                     # noqa: BLE001
                return {}

        # -- GET ----------------------------------------------------------
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            # 「这个端口上蹲着的到底是不是我们自己」—— 给 web_ui/shell.py 用。
            # 端口 47913 被**别的程序**占住时，老逻辑会当成「已经有一个实例在跑」
            # 然后弹框让用户去任务管理器结束 MSU1Converter-web.exe（那儿根本没有），
            # 程序从此永远起不来。有了这个端点，shell 能分辨出「是别人」并换端口。
            if path == "/api/whoami":
                self._json({"app": APP_ID, "pid": os.getpid()})
                return

            if path == "/api/state":
                since = int((qs.get("since") or ["0"])[0] or 0)
                self._json(state.snapshot(since))
                return

            if path == "/api/diag":
                self._json(_diag())
                return

            # 全部主题（2 套现代 + 6 套复古）—— 复古那 6 套是把
            # retro_palette.THEMES 翻成 CSS 变量，前端 setProperty 即可
            if path == "/api/themes":
                self._json(_themes_payload())
                return

            # PSF 系（PS1 芯片音源）能不能用 —— 主界面右侧那块状态就是它
            if path == "/api/psf/status":
                self._json(_psf_status())
                return

            # ---- 网易云云盘：账号状态 / 扫码轮询 / 上传清单 ----
            # ⚠ 这三条必须是 GET + 高频轮询：浏览器登录要等用户操作（最长
            #   4 分钟），前端每 0.5 秒拉一次 status 看 job 走到哪一步。
            if path == "/api/cloud/status":
                self._json(_cloud_status())
                return

            if path == "/api/cloud/qr_poll":
                self._json(cloud_auth().qr_poll())
                return

            if path == "/api/cloud/plan":
                # ?dir= 缺省时用主界面的输出目录（下载页会显式传 wav 目录）
                d = (qs.get("dir") or [""])[0]
                if not d:
                    with state.lock:
                        d = (state.opts or {}).get("out_dir") or ""
                # ?games=A|B —— 只算这几个游戏子目录。用 ``|`` 分隔而不是逗号：
                # Windows 目录名里不能有 ``|``，而逗号在游戏名里很常见
                # （"Armored Unit (1999-11-...)..." 这种）。
                g = (qs.get("games") or [""])[0]
                games = [x for x in g.split("|") if x] if g else None
                self._json(_cloud_plan(d, games))
                return

            # ---- 下载页（joshw.info 全机种镜像）----
            if path == "/api/dl/diag":
                # 打包后「下载页打得开、一按开始就 ModuleNotFoundError」的第一手证据。
                # 门禁（vendor/check_web_exe.py）靠它抓「模块被 nofollow 掉了」。
                d = dl_state().diag()
                d["ok"] = True
                self._json(d)
                return

            if path == "/api/dl/platforms":
                groups, err = dl_state().platforms()
                self._json({"ok": not err, "groups": groups, "error": err})
                return

            if path == "/api/dl/state":
                since = int((qs.get("since") or ["0"])[0] or 0)
                self._json(dl_state().snapshot(since))
                return

            if path == "/api/audio":
                self._audio(qs)
                return

            # ---- 下载页试听（播放下载流程转出来的 WAV）----
            if path == "/api/dl/wavs":
                # force=1 跳过 1.5 秒扫描缓存（用户手点「刷新」时用）
                force = bool((qs.get("force") or [""])[0])
                self._json(dl_state().wav_list(force=force))
                return

            if path == "/api/dl/wav_games":
                # 已经转好的游戏清单（上传云盘时让用户挑要传哪些）
                self._json(dl_state().wav_games())
                return

            if path == "/api/dl/audio":
                # ⚠ 只收**相对**路径，越界判定在 dl_api.wav_path() 里
                #   （commonpath 挡住 ../.. 穿越）。
                rel = (qs.get("rel") or [""])[0]
                full = dl_state().wav_path(rel)
                if not full:
                    self._err("wav not found", 404)
                    return
                self._send_wav(full)
                return

            if path == "/api/dl/covers":
                # v6.0.7：一个游戏有多张封面，试听时轮播。前端先问「有几张」，
                # 再按 i=0..n-1 逐张取。**只回数量，不回图** —— 图走下面的
                # /api/dl/cover，那样浏览器能各自缓存、也能懒加载。
                game = (qs.get("game") or [""])[0]
                try:
                    paths = dl_state().cover_paths(game)
                except Exception:                # noqa: BLE001
                    paths = []
                self._json({"ok": True, "n": len(paths)})
                return

            if path == "/api/dl/cover":
                # 试听时的封面（v6.0.1）。**只认游戏名**，路径由后端算 ——
                # 前端拼绝对路径必然出错（保存目录只有后端知道）。
                # 找不到就 404，前端显示音符占位，不弹错误。
                #
                # v6.0.7：多了 ``i``（第几张，0 起）。越界一律夹到范围内 ——
                # 前端可能在「取完数量」和「取图」之间被用户切了游戏，
                # 这时候回 404 会让轮播卡住，夹一下更稳。
                game = (qs.get("game") or [""])[0]
                try:
                    idx = int((qs.get("i") or ["0"])[0])
                except (TypeError, ValueError):
                    idx = 0
                paths = dl_state().cover_paths(game)
                if not paths:
                    self._err("cover not found", 404)
                    return
                idx = max(0, min(idx, len(paths) - 1))
                full = paths[idx]
                try:
                    data = open(full, "rb").read()
                except OSError as exc:
                    self._err("cover unreadable: %s" % exc, 404)
                    return
                ctype = (mimetypes.guess_type(full)[0]
                         or "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                # 封面一旦落下就不会变，但用户可能重转补图 —— 60 秒足够，
                # 又不至于让人对着旧图以为没生效。
                self.send_header("Cache-Control", "max-age=60")
                self.end_headers()
                self.wfile.write(data)
                return

            if path == "/api/tracks":
                try:
                    idx = int((qs.get("i") or ["0"])[0])
                except ValueError:
                    self._err("bad index", 400)
                    return
                rows = state.tracks_of(idx)
                if rows is None:
                    self._err("音源不存在", 404)
                    return
                self._json({"ok": True, "tracks": rows,
                            "note": "" if rows else "这个音源列不出曲目清单"})
                return

            if path == "/":
                path = "/index.html"
            name = path.lstrip("/")
            if name in STATIC_OK:
                self._static(name)
                return

            self._err("not found: %s" % path, 404)

        def _static(self, name):
            full = os.path.join(WEB_DIR, name)
            if not os.path.isfile(full):
                self._err("missing: %s" % name, 404)
                return
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if name.endswith(".js"):
                ctype = "text/javascript; charset=utf-8"
            elif name.endswith(".css"):
                ctype = "text/css; charset=utf-8"
            elif name.endswith(".html"):
                ctype = "text/html; charset=utf-8"
            data = open(full, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _audio(self, qs):
            """试听：只放行 state.outputs 里登记过的文件，支持 Range。"""
            try:
                oid = int((qs.get("id") or ["0"])[0])
            except ValueError:
                self._err("bad id", 400)
                return
            item = state.find_output(oid)
            if not item or not os.path.isfile(item["path"]):
                self._err("audio not found", 404)
                return
            self._send_wav(item["path"])

        def _send_wav(self, full):
            """把一个 WAV 文件发出去（支持 Range）。

            主界面（``/api/audio``）和下载页（``/api/dl/audio``）共用这一段：
            路径**谁给谁负责鉴权**，这里只管发字节 —— 别在这里加业务判断。
            """
            size = os.path.getsize(full)
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            if rng and rng.startswith("bytes="):
                spec = rng[6:].split(",")[0]
                a, _, b = spec.partition("-")
                if a:
                    start = int(a)
                if b:
                    end = min(int(b), size - 1)
                if start > end:
                    start, end = 0, size - 1
            length = end - start + 1
            code = 206 if rng else 200
            self.send_response(code)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if code == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            with open(full, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        # -- POST ---------------------------------------------------------
        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            # ⚠ 二进制上传（拖拽兜底）**不能**走 _body()：它按 JSON 读，
            #   会先把 Content-Length 那么多字节全部吃掉，分支里再 read
            #   就只能干等到超时。所以这里提前分流，body 由分支自己读。
            if path == "/api/drop_blob":
                try:
                    self._route(path, {})
                except Exception as exc:          # noqa: BLE001 兜底给前端
                    traceback.print_exc()
                    self._err("%s: %s" % (type(exc).__name__, exc), 500)
                return
            body = self._body()
            try:
                self._route(path, body)
            except Exception as exc:              # noqa: BLE001 兜底给前端
                traceback.print_exc()
                self._err("%s: %s" % (type(exc).__name__, exc), 500)

        def _dl_route(self, path, body):
            """下载页的 POST 接口。**判定全在 dl_api.DownloadState 里**，
            这里只做参数转手 —— 别把业务口径写进 HTTP 层。"""
            dl = dl_state()

            if path == "/api/dl/load":
                r = dl.load(body.get("key"), bool(body.get("force")))

            elif path == "/api/dl/select":
                n = dl.select(body.get("urls"), body.get("mode") or "set")
                r = {"ok": True, "selected": n}

            elif path == "/api/dl/set_opts":
                # ⚠ 改目录必须走 dl.set_dest()：它会把选择落盘（换机也能找回），
                #   并重扫磁盘快照。直接赋 dl.dest 的话重启就丢，
                #   「状态」列会按默认目录去算，看着像「全都没下过」。
                if body.get("dest"):
                    dl.set_dest(body["dest"])
                with dl.lock:
                    if "workers" in body:
                        try:
                            dl.opts["workers"] = max(
                                1, min(4, int(body["workers"])))
                        except (TypeError, ValueError):
                            pass
                    for k in ("auto_extract", "del_archive",
                              "auto_wav", "auto_flac",
                              # v5.9.15：MP3（与上两个三者互斥）
                              "auto_mp3", "merge_wav",
                              # v5.9.14：封面（下载时顺手抓一张）
                              "auto_cover"):
                        if k in body:
                            dl.opts[k] = bool(body[k])
                    # v5.9.7：WAV / FLAC 在**后端**也强制互斥。
                    # 前端勾一个会自动取消另一个，但接口是公开的 ——
                    # 两个都为真时 ConvertWorker 只会走 FLAC（见 dl_api 里
                    # 「auto_flac 优先」），opts 里却留着 auto_wav=True，
                    # 于是「待转 WAV」这类文案会和实际产物对不上。
                    # 以最后到达的那个为准，把另一个清掉。
                    #
                    # v5.9.15：三选一（wav / flac / mp3）。三条分支写全 ——
                    # 别改成循环 + 动态键名，门禁是按源码文本断言的
                    # （``dl.opts["auto_wav"] = False`` 必须真的出现在源码里），
                    # 而且显式写出来更好读：一眼能看出「谁清掉谁」。
                    if "auto_mp3" in body and dl.opts["auto_mp3"]:
                        dl.opts["auto_wav"] = False
                        dl.opts["auto_flac"] = False
                        dl.opts["merge_wav"] = False
                    elif "auto_flac" in body and dl.opts["auto_flac"]:
                        dl.opts["auto_wav"] = False
                        dl.opts["auto_mp3"] = False
                        dl.opts["merge_wav"] = False
                    elif "auto_wav" in body and dl.opts["auto_wav"]:
                        dl.opts["auto_flac"] = False
                        dl.opts["auto_mp3"] = False
                    # 「状态」列是**按保存目录扫出来的**，用户改完目录最想知道
                    # 的就是「扫的是不是我刚填的那个、扫到了多少」。这里一并回给
                    # 前端，好让它在改完目录时给一句明确反馈 —— 否则目录填错
                    # （比如填成不存在的路径）时状态列一片空白，看着像功能坏了。
                    r = {"ok": True, "opts": dict(dl.opts), "dest": dl.dest,
                         "dest_exists": bool(dl.dest) and os.path.isdir(dl.dest),
                         "scan": {"files": len(dl._disk),
                                  "dirs": len(dl._disk_dirs),
                                  "wav": len(dl._wav_games),
                                  "limited": bool(dl._disk_limited)}}

            elif path == "/api/dl/start":
                r = dl.start(body or {})

            elif path == "/api/dl/stop":
                r = dl.stop()

            elif path == "/api/dl/zh":
                r = dl.translate()

            elif path == "/api/dl/wav_clear":
                # 清空试听列表 —— **只隐藏，不删 WAV 文件**（理由见
                # dl_api.DownloadState.wav_clear 的 docstring）
                r = dl.wav_clear()

            elif path == "/api/dl/wav_add":
                # 把「已转 WAV」的游戏加入试听列表（清空之后也能挑回来）
                r = dl.wav_add(body.get("urls") or [])

            elif path == "/api/dl/convert_wav":
                # 单条「转 WAV / 转 FLAC / 转 MP3」：已下载/已解压但没转的，
                # 不重下也能转。v5.9.9 起格式由 body["fmt"] 显式指定（底部那
                # 几颗按钮各传各的），不传时退回 opts 里的自动转开关 ——
                # 老前端 / 门禁仍按这个走。白名单在 dl_api._norm_fmt()。
                r = dl.convert_wav(body.get("urls") or [], body.get("fmt"))

            elif path == "/api/dl/fetch_covers":
                # v6.0.4：手动「下载封面」（替代原来的「顺带下载封面」复选框）。
                # 作用于**勾选的条目**：抓图 → 落 cover.png → 给已有 FLAC/MP3 补图。
                # 不下载音源、不转码，也不改 phase/busy（封面不算「忙」）。
                r = dl.fetch_covers(body.get("urls") or [])

            elif path == "/api/dl/clear_cache":
                r = dl.clear_cache(body.get("key"))

            # v6.0.8：「更新全部列表」一颗按钮跑完 33 个机种。
            # 原来是逐个机种点「清除缓存」重扫，33 遍。
            # 只写缓存、不动当前视图，进度走 /api/dl/state 的 refresh 字段。
            elif path == "/api/dl/refresh_all":
                r = dl.refresh_all()

            elif path == "/api/dl/refresh_cancel":
                r = dl.cancel_refresh()

            elif path == "/api/dl/pick_dest":
                picked = native_dialog.pick_folder(
                    title="选择下载保存目录",
                    parent_hwnd=body.get("hwnd") or 0)
                r = {"ok": True, "path": picked or ""}

            elif path == "/api/dl/open_dir":
                # ⚠「开哪个目录」的判定在 dl_api.open_dir_target() 里 ——
                #   这里只负责把资源管理器拉起来。原来这里写的是
                #   ``dl.wav_dir or dl.dest``，而 wav_dir 在 start() 时就冻成
                #   <保存目录>/WAV 了（**哪怕目录还没建**），于是没转过 WAV
                #   的用户点这颗按钮只会看到「目录不存在」。
                d, tag, note = dl.open_dir_target()
                if not d:
                    r = {"ok": False, "error": note}
                else:
                    try:
                        os.startfile(d)          # noqa: S606 Windows 专用
                        r = {"ok": True, "path": d, "target": tag, "note": note}
                    except Exception as exc:     # noqa: BLE001
                        r = {"ok": False, "error": str(exc)}

            elif path == "/api/dl/upload_cloud":
                r = dl.upload_cloud(body.get("scope") or "",
                                    body.get("games") or None,
                                    body.get("fmt") or "flac")

            else:
                self._err("unknown dl api: %s" % path, 404)
                return

            self._json(r)

        def _cloud_route(self, path, body):
            """网易云云盘的账号动作。**判定全在 cloud_api.CloudAuth 里**。

            注意这里**没有** ``/status`` / ``/qr_poll`` / ``/plan`` —— 那三条
            是 GET（前端高频轮询），在 ``do_GET`` 里。

            耗时动作的分工：
            * ``login_browser`` / ``switch`` → 立刻返回，进度靠 status 轮询
            * ``login_cookie`` → 同步（一次网络往返，1-3 秒）
            * ``qr_start`` → 同步（一次网络往返，约 0.3 秒）
            * ``logout`` → 同步（要起 headless 浏览器清 cookie，2-5 秒）
            """
            auth = cloud_auth()

            if path == "/api/cloud/login_browser":
                self._json(auth.start_browser())
                return

            if path == "/api/cloud/cancel":
                self._json(auth.cancel())
                return

            if path == "/api/cloud/dismiss":
                self._json(auth.dismiss())
                return

            if path == "/api/cloud/login_cookie":
                self._json(auth.login_cookie(body.get("cookie") or ""))
                return

            if path == "/api/cloud/qr_start":
                self._json(auth.qr_start())
                return

            if path == "/api/cloud/switch":
                self._json(auth.start_switch())
                return

            if path == "/api/cloud/logout":
                self._json(auth.logout())
                return

            self._err("unknown cloud api: %s" % path, 404)

        def _route(self, path, body):
            # ---- 下载页（joshw.info 全机种镜像）----
            if path.startswith("/api/dl/"):
                self._dl_route(path, body)
                return

            # ---- 网易云云盘账号（浏览器登录 / 粘贴 Cookie / 扫码 / 换号）----
            if path.startswith("/api/cloud/"):
                self._cloud_route(path, body)
                return

            if path == "/api/pick_files":
                picked = native_dialog.pick_files(
                    title="选择音源（可多选）",
                    parent_hwnd=body.get("hwnd") or 0)
                self._json({"ok": True, "paths": picked or []})
                return

            # 「添加文件夹」—— 只返回文件夹路径，真正的展开交给
            # /api/add_sources（add_paths 认识目录，会递归扫描）。
            if path == "/api/pick_folder_sources":
                picked = native_dialog.pick_folder(
                    title="选择音源文件夹（含子目录）",
                    parent_hwnd=body.get("hwnd") or 0)
                self._json({"ok": True, "path": picked or ""})
                return

            if path == "/api/pick_outdir":
                picked = native_dialog.pick_folder(
                    title="选择输出目录",
                    parent_hwnd=body.get("hwnd") or 0)
                self._json({"ok": True, "path": picked or ""})
                return

            # ---- PSF 系（PS1 芯片音源）的 BIOS 设置 ----
            # 状态查询走 do_GET 的 /api/psf/status（前端用 GET）
            if path == "/api/psf/set_bios":
                self._psf_set_bios(body)
                return

            if path == "/api/psf/autofind":
                self._psf_autofind()
                return

            if path == "/api/add_sources":
                paths = body.get("paths") or []
                added, skipped = state.add_paths(paths)
                for p, why in skipped:
                    # 短原因（「路径不存在」）用括号；长原因（「33 个转不了：
                    # 29 个 .hwas（…）」）自己就带括号，再套一层会变成
                    # 「（…（…）…）」，日志区里根本读不下去 —— 换成破折号。
                    sep = " —— " if ("（" in why or len(why) > 24) else "（"
                    tail = "" if sep == " —— " else "）"
                    state.log("   ⚠ 跳过 %s%s%s%s"
                              % (os.path.basename(p), sep, why, tail))
                if added:
                    state.log("已添加 %d 个音源" % added)
                self._json({"ok": True, "added": added,
                            "skipped": [{"path": p, "why": w} for p, w in skipped]})
                return

            if path == "/api/drop_blob":
                # v5.4：浏览器拖拽的兜底 —— 拿不到本地路径时前端把**文件内容**
                # POST 上来，这里原样写到临时目录。
                #
                # 为什么要保持相对目录结构：`.mini2sf` 靠 `_lib` 标签去**同目录**
                # 找 `.2sflib`（usf/gsf 同理），全拍平到一个目录反而能用，
                # 但重名文件会互相覆盖；按原结构落盘最稳。
                rel = urllib.parse.unquote(self.headers.get("X-Rel-Path") or "")
                sess = urllib.parse.unquote(self.headers.get("X-Drop-Session") or "")
                if not rel or not sess:
                    self._err("缺少 X-Rel-Path / X-Drop-Session")
                    return
                # 只允许「相对路径」：含 .. 段的一律拒绝（不是过滤掉就算 ——
                # 静默改写路径会让攻击面和正常行为混在一起，看不出区别）
                raw_parts = rel.replace("\\", "/").split("/")
                if any(p == ".." for p in raw_parts):
                    self._err("非法路径")
                    return
                parts = [p for p in raw_parts if p not in ("", ".")]
                if not parts:
                    self._err("非法路径")
                    return
                sess_safe = re.sub(r"[^0-9A-Za-z_-]", "", sess)[:40] or "s"
                root = os.path.join(drop_root(), sess_safe)
                dst = os.path.join(root, *parts)
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    n = int(self.headers.get("Content-Length") or 0)
                    remain = n
                    with open(dst, "wb") as fh:
                        while remain > 0:
                            chunk = self.rfile.read(min(remain, 1 << 20))
                            if not chunk:
                                break
                            fh.write(chunk)
                            remain -= len(chunk)
                except OSError as exc:
                    self._err("写临时文件失败：%s" % exc)
                    return
                self._json({"ok": True, "written": n - remain})
                return

            if path == "/api/drop_commit":
                # 所有 blob 传完后由前端调一次：把整个临时目录当音源收进来。
                # 交给 add_paths 是为了复用它的全部规则（整包 vs 逐文件、
                # 去重、跳过原因），不在这里另写一套。
                sess = str(body.get("session") or "")
                sess_safe = re.sub(r"[^0-9A-Za-z_-]", "", sess)[:40] or "s"
                root = os.path.join(drop_root(), sess_safe)
                if not os.path.isdir(root):
                    self._err("没有收到任何文件")
                    return
                # 把**临时目录下的条目**交给 add_paths，而不是临时目录本身 ——
                # 否则音源名会变成随机的 session id（`dndrop/d1a2b3c4`）。
                # 单条目直接传它（单文件 → 文件名，单文件夹 → 文件夹名）；
                # 多条目逐个传，各自成一个音源（和「一次拖 5 个文件」的直觉一致）。
                try:
                    entries = sorted(os.listdir(root))
                except OSError as exc:
                    self._err("读临时目录失败：%s" % exc)
                    return
                if not entries:
                    self._err("没有收到任何文件")
                    return
                targets = ([os.path.join(root, entries[0])] if len(entries) == 1
                           else [os.path.join(root, e) for e in entries])
                added, skipped = state.add_paths(targets)
                if added:
                    state.log("拖入 %d 个音源" % added)
                self._json({"ok": True, "added": added,
                            "skipped": [{"path": p, "why": w}
                                        for p, w in skipped]})
                return

            if path == "/api/remove_source":
                idx = int(body.get("index", -1))
                with state.lock:
                    if 0 <= idx < len(state.sources):
                        gone = state.sources.pop(idx)
                        state.log("− 移除 %s" % gone["label"])
                self._json({"ok": True})
                return

            if path == "/api/remove_sources":
                # v5.4：批量删除（左栏「删除选中」）。索引由前端**倒序**排好，
                # 这里再兜一层：去重 + 过滤越界 + 从大到小删，防止前端漏排
                # 导致「删一个、后面的全错位」。
                raw = body.get("indexes") or []
                idxs = sorted({int(x) for x in raw
                               if str(x).lstrip("-").isdigit()}, reverse=True)
                removed = []
                with state.lock:
                    for i in idxs:
                        if 0 <= i < len(state.sources):
                            removed.append(state.sources.pop(i))
                    for gone in reversed(removed):
                        state.log("− 移除 %s" % gone["label"])
                self._json({"ok": True, "removed": len(removed)})
                return

            if path == "/api/clear_sources":
                # v5.4：只清音源列表。**刻意**不动 outputs / logs / 状态 ——
                # `/api/clear` 是「整个任务重来」，而这里用户往往只是想换一批
                # 音源，已经转好的 WAV 不能跟着消失。
                with state.lock:
                    n = len(state.sources)
                    state.sources = []
                    # 曲目缓存按 path 存，音源都清空了就该一起丢 ——
                    # 否则重新加同一个文件时会拿到上一次的旧行（含旧时长）。
                    state._track_cache.clear()
                    state.log("清空音源列表（%d 个）" % n)
                self._json({"ok": True, "cleared": n})
                return

            if path == "/api/clear":
                with state.lock:
                    state.sources = []
                    state.outputs = []
                    state.logs = []
                    state._seq = 0
                    state.state = "idle"
                    state.progress = 0.0
                    state.status = ""
                    state.result = None
                    state.error = ""
                self._json({"ok": True})
                return

            if path == "/api/start":
                self._start(body.get("opts") or {})
                return

            if path == "/api/cancel":
                with state.lock:
                    job = state.job
                if job is not None:
                    job.cancel()
                    state.log("⛔ 收到取消请求…")
                self._json({"ok": True})
                return

            if path == "/api/upload_cloud":
                self._upload_cloud(body)
                return

            # 在资源管理器里打开输出目录（tkinter 版那颗「打开输出目录」）
            if path == "/api/open_outdir":
                self._open_outdir(body.get("dir") or "")
                return

            if path == "/api/open_downloader":
                self._open_downloader(bool(body.get("dry")))
                return

            self._err("unknown api: %s" % path, 404)

        # -- 转换 ---------------------------------------------------------
        def _start(self, opts):
            with state.lock:
                if state.state == "running":
                    self._err("已经有一个转换在跑", 409)
                    return
                if not state.sources:
                    self._err("还没有音源", 400)
                    return
                merged = dict(default_opts())
                merged.update(opts or {})
                # v5.4：输出格式改成「默认一个都不勾」之后，必须挡住
                # 「两个都没选就点开始」—— 否则 ConvertJob 两个分支都不进，
                # 用户看到的是「转换完成」但一个文件都没有。
                if not (merged.get("do_cd") or merged.get("do_wav")):
                    self._err("没选输出格式 —— BIN+CUE 或音频分轨"
                              "（WAV / FLAC / MP3 / OGG）至少勾一个", 400)
                    return
                # 容器只认白名单里的值。前端传了别的（或压根没传）一律退回
                # wav，别让一个拼错的值把整批转换变成 0 字节文件。
                # v5.9.15：白名单从「flac / 其余」扩成 flac / mp3 / wav，
                # 收口在 flac_out.WRITE_FMTS —— 加格式只改那一处。
                _f = str(merged.get("fmt") or "").strip().lower()
                # v6.0.0：真的按注释说的收口在 flac_out.WRITE_FMTS 了 ——
                # 以前注释这么写、代码却是硬编码元组，加 OGG 时差点漏掉
                # 这一处（漏了的表现是「界面选了 OGG，转出来是 WAV」）。
                try:
                    import flac_out as _fo
                    _allow = _fo.WRITE_FMTS
                except Exception:                # noqa: BLE001
                    _allow = ("flac", "mp3", "ogg")
                merged["fmt"] = _f if _f in _allow else "wav"
                # FLAC / MP3 选项只在编码器真的可用时才有意义。前端会隐藏
                # 按钮，但接口是公开的 —— 后端自己再挡一道，报错比静默出 WAV 好。
                if merged["fmt"] != "wav" and merged.get("do_wav"):
                    try:
                        import flac_out
                        _av, _why = flac_out.available(merged["fmt"])
                    except Exception as _exc:        # noqa: BLE001
                        _av, _why = False, str(_exc)
                    if not _av:
                        self._err("%s 编码器不可用：%s"
                                  % (merged["fmt"].upper(), _why), 400)
                        return
                merged["out_dir"] = str(merged.get("out_dir") or "").strip()
                # 只认绝对路径。前端曾经把 #pathText 的**提示文字**当路径发上来，
                # 结果在磁盘上建了个名叫「（默认：音源同级的 output 目录）」的目录。
                # 后端自己再收一道口，别指望每个客户端都老实。
                if not os.path.isabs(merged["out_dir"]):
                    # 规则见 default_out_dir() 的 docstring（含两个踩过的坑）
                    merged["out_dir"] = default_out_dir(state.sources[0]["path"])
                state.opts = merged
                sources = list(state.sources)
                state.state = "running"
                state.progress = 0.0
                state.status = "准备中…"
                state.done_tracks = 0
                state.total_tracks = 0
                state.result = None
                state.error = ""
                state.outputs = []

            state.log("══ 开始转换 ══")
            state.log("输出到: %s" % merged["out_dir"])
            job = ConvertJob(
                sources, merged,
                on_log=state.log,
                on_progress=lambda p, t: self._set_progress(p, t),
                on_track=lambda i, n: self._set_track(i, n),
            )
            with state.lock:
                state.job = job

            def work():
                try:
                    res = job.run()
                    state.register_outputs(merged["out_dir"])
                    with state.lock:
                        state.result = res
                        state.state = "cancelled" if res.get("cancelled") else "done"
                        state.progress = 100.0 if not res.get("cancelled") else state.progress
                        state.status = "已取消" if res.get("cancelled") else "完成"
                    state.log("══ %s：成功 %d 首 / 失败 %d 首 / 共 %s ══"
                              % ("已取消" if res.get("cancelled") else "转换结束",
                                 res.get("ok", 0), res.get("fail", 0),
                                 fmt_dur(res.get("seconds", 0))))
                except Exception as exc:          # noqa: BLE001
                    traceback.print_exc()
                    with state.lock:
                        state.state = "failed"
                        state.error = str(exc)
                        state.status = "失败"
                    state.log("❌ 转换失败：%s" % exc)
                finally:
                    with state.lock:
                        state.job = None

            t = threading.Thread(target=work, daemon=True)
            with state.lock:
                state.thread = t
            t.start()
            self._json({"ok": True})

        def _set_progress(self, pct, text):
            with state.lock:
                state.progress = float(pct)
                state.status = str(text)

        def _set_track(self, i, n):
            """ConvertJob 每写完一个 WAV 调一次 —— 曲目表的真实「已输出」数。

            以前这里是 ``lambda i, n: None``，回调被丢掉，前端只好拿 progress
            反推已输出几首。解码阶段开始报进度之后（v5.9.5）那个反推就错了：
            进度 35% 时其实一个文件都还没写，界面却说「已输出 32 首」。
            """
            with state.lock:
                state.done_tracks = int(i or 0)
                state.total_tracks = int(n or 0)

        # -- PSF 的 BIOS 设置 ------------------------------------------------
        def _psf_set_bios(self, body):
            """``POST /api/psf/set_bios``

            body: ``{"pick": true, "hwnd": N}`` → 弹原生对话框选；
                  ``{"path": "..."}`` → 直接用这个路径。
            验证不过就**不写**配置 —— 免得用户以为设好了、转换时才炸。
            """
            try:
                import psf_native
            except Exception as exc:         # noqa: BLE001
                self._err("psf_native 不可用：%s" % exc, 500)
                return

            p = str(body.get("path") or "").strip().strip('"')
            if not p and body.get("pick"):
                picked = native_dialog.pick_files(
                    title="选择 PS1/PS2 BIOS（4MB 的 .bin，或 512KB 的 HE BIOS）",
                    parent_hwnd=body.get("hwnd") or 0,
                    multi=False,
                    filter_parts=native_dialog.BIOS_PARTS)
                p = picked[0] if picked else ""
                if not p:
                    # 用户点了取消 —— 不是错误，别让前端弹红条
                    self._json({"ok": True, "cancelled": True, "error": ""})
                    return

            r = psf_native.set_bios(p)
            if r.get("ok"):
                state.log("🔧 PSF BIOS 已设置：%s（%s）" % (r["path"], r["kind"]))
                if r.get("bundled"):
                    state.log("   📦 已复制到程序目录 bios/，以后不用再指定")
                elif r.get("bundle_note"):
                    state.log("   ⚠ 没能复制到程序目录：%s" % r["bundle_note"])
            r["cancelled"] = False
            r["status"] = _psf_status(force=True)
            self._json(r)

        def _psf_autofind(self):
            """``POST /api/psf/autofind`` —— 全自动找 BIOS，找到就固化。

            扫本机常见位置（程序目录 ``bios/``、RetroArch / PCSX2 /
            DuckStation / ePSXe 的 bios 目录、桌面与下载目录），
            命中就复制进程序目录 ``bios/`` 并写配置 —— 以后**不用再指定**。

            找不到不是错误：返回 ``ok:false`` + ``why``（说明扫过哪些目录）。
            """
            try:
                import psf_native
            except Exception as exc:         # noqa: BLE001
                self._err("psf_native 不可用：%s" % exc, 500)
                return
            try:
                r = psf_native.autodiscover()
            except Exception as exc:         # noqa: BLE001
                self._err("自动查找出错：%s" % exc, 500)
                return
            if r.get("ok"):
                state.log("🔍 自动找到 PSF BIOS：%s（%s）"
                          % (r.get("path"), r.get("note")))
                if r.get("bundled"):
                    state.log("   📦 已复制到程序目录 bios/，以后不用再指定")
            else:
                state.log("🔍 自动查找没找到 BIOS：%s" % (r.get("why") or ""))
            r["status"] = _psf_status(force=True)
            self._json(r)

        # -- 「从网络下载」 --------------------------------------------------
        def _open_downloader(self, dry=False):
            """拉起同目录的 tkinter 版 ``MSU1Converter.exe``（**兼容保留**）。

            v5.1 起下载窗口已经整套移植成 Web 页（``/download.html``），
            主界面那个按钮改成**页内跳转**了，不再走这里。
            这条路由留着是给「想用 tkinter 版原生窗口」的人 / 外部脚本调用；
            没有调用方也不影响运行。
            """
            names = ("MSU1Converter.exe", "MSU1Converter-v4.44.exe")
            roots = []
            # ⚠ 打包后要用 argv[0] 所在目录，不是 sys.executable 所在目录：
            # Nuitka onefile 的 sys.executable 在临时解包目录里。
            here = os.path.dirname(os.path.abspath(sys.argv[0] or __file__))
            roots += [here, os.path.join(here, "dist"),
                      os.path.join(os.path.dirname(here), "dist"),
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]

            found = ""
            for r in roots:
                for n in names:
                    cand = os.path.join(r, n)
                    if os.path.isfile(cand):
                        found = cand
                        break
                if found:
                    break
            if not found:
                self._err("没找到 tkinter 版 MSU1Converter.exe。"
                          "把它和本程序放同一目录，或先用那一版下载音源。", 404)
                return
            if dry:                              # 门禁只查「找不找得到」，不真启动
                self._json({"ok": True, "exe": found, "dry": True})
                return
            try:
                subprocess.Popen([found], cwd=os.path.dirname(found),
                                 close_fds=True)
            except OSError as exc:
                self._err("启动失败: %s" % exc, 500)
                return
            state.log("↗ 已启动 tkinter 版下载窗口：%s" % os.path.basename(found))
            self._json({"ok": True, "exe": found})

        # -- 「打开输出目录」 -------------------------------------------------
        def _open_outdir(self, explicit=""):
            """在资源管理器里打开输出目录（tkinter 版那颗按钮的 Web 版）。

            ``explicit`` 可以指一个具体目录；留空就用当前输出目录。
            **目录不存在要明说** —— ``os.startfile`` 对不存在的路径抛的
            ``FileNotFoundError`` 里带的是英文路径，用户看不出是「还没转换」。
            """
            d = str(explicit or "").strip()
            if not d:
                with state.lock:
                    d = (state.opts or {}).get("out_dir") or ""
            if not d:
                self._err("还没设输出目录，先转换一次", 400)
                return
            if not os.path.isdir(d):
                self._err("目录不存在：%s（还没转换过，或者这个目录已经被移走/改名）"
                          % d, 400)
                return
            try:
                os.startfile(d)                  # noqa: S606 Windows 专用
            except Exception as exc:             # noqa: BLE001
                self._err("打不开资源管理器：%s" % exc, 500)
                return
            self._json({"ok": True, "path": d})

        # -- 云盘 ---------------------------------------------------------
        def _upload_cloud(self, body):
            """WAV → FLAC → 网易云云盘。

            ⚠ ``cloud_upload.CloudUploader`` 的真实签名是
            ``(sess, emit, cancel).run(wavs)`` —— 不是 ``(log=…).run(wavs, merged=…)``。
            ``emit`` 是 ``(kind, payload)`` 回调，kind ∈ log/prog/done。

            登录态交给 ``cloud_auth().ensure()``：先看存盘 cookie 还能不能用，
            不能用就试着从内置浏览器 profile 静默取一次，两条都失败才返回
            ``need_login`` 让前端弹登录窗 —— **不再让用户去开 tkinter 版**。
            """
            try:
                import cloud_upload as cu
            except ImportError as exc:
                self._err("云盘模块不可用: %s" % exc, 500)
                return

            with state.lock:
                if state.upload["running"]:
                    self._err("已经有一个上传在跑了", 409)
                    return

            out_dir = (state.opts or {}).get("out_dir") or ""
            if not out_dir or not os.path.isdir(out_dir):
                self._err("输出目录还不存在，先转换一次", 400)
                return
            # ⚠ ``collect_wavs(*folders)`` 是可变参数 —— 传 ``[out_dir]`` 会
            #   让 os.path.isdir 收到一个列表，直接 TypeError。
            wavs = cu.collect_wavs(out_dir)
            if not wavs:
                self._err("输出目录里没有 WAV", 400)
                return

            wavs, why = _pick_scope(cu, wavs, body)
            if not wavs:
                self._err(why or "这个范围里没有可上传的文件", 400)
                return

            # 登录态**同步**确认一次（静默恢复最多约 1 秒）。
            # ⚠ 必须放在起线程之前 —— 前端要根据 need_login 决定弹不弹登录窗，
            #   丢进后台线程的话前端只能拿到一个「已开始」，然后眼睁睁看着
            #   日志里冒出一句「未登录」。
            got = cloud_auth().ensure(log=state.log)
            if not got.get("ok"):
                self._json({"ok": False,
                            "need_login": bool(got.get("need_login")),
                            "error": got.get("error") or "云盘未登录",
                            "count": len(wavs)})
                return
            sess = got["session"]
            user = sess.user_name or ""
            # v6.0.1：上传容器可选（flac / mp3）。不认识的值退回 flac。
            fmt = cu.norm_fmt(body.get("fmt"))

            def emit(kind, payload=None):
                if kind == "log":
                    state.log("   ☁ %s" % payload)
                elif kind == "prog":
                    with state.lock:
                        state.upload["progress"] = int(payload or 0)
                elif kind == "done":
                    ok, skip, fail = payload
                    with state.lock:
                        state.upload.update(ok=int(ok), skip=int(skip), fail=int(fail))

            def work():
                with state.lock:
                    state.upload.update(running=True, progress=0,
                                        ok=0, skip=0, fail=0, error="",
                                        user=user, count=len(wavs), fmt=fmt)
                try:
                    state.log("☁ 开始上传 %d 个文件（%s / %s）…"
                              % (len(wavs), user or "未知账号", fmt.upper()))
                    cu.CloudUploader(sess, emit, threading.Event(),
                                     fmt=fmt).run(wavs)
                    state.log("☁ 上传结束：成功 %d / 跳过 %d / 失败 %d"
                              % (state.upload["ok"], state.upload["skip"],
                                 state.upload["fail"]))
                except Exception as exc:              # noqa: BLE001
                    state.log("❌ 上传失败：%s" % exc)
                    with state.lock:
                        state.upload["error"] = str(exc)
                finally:
                    with state.lock:
                        state.upload["running"] = False

            threading.Thread(target=work, daemon=True).start()
            self._json({"ok": True, "count": len(wavs), "user": user})

    return Handler


class ThreadedServer(socketserver.ThreadingTCPServer):
    """⚠ 必须多线程 —— 单线程会卡死浏览器的并行资源请求。

    ⚠⚠ ``allow_reuse_address`` **必须是 False**。
    它映射到 ``SO_REUSEADDR``，而 **Windows 的 SO_REUSEADDR 语义和 Unix 不同**：
    Unix 上它只允许复用 TIME_WAIT 的地址；Windows 上它允许**两个 socket 绑同一个
    端口**（后绑的抢包）。

    这个类原来是 ``True``（抄来的默认值），结果「固定端口 47913 兼作单实例锁」
    这个设计**从来没生效过** —— 第二个实例照样能绑上，起自己的服务、再拉一个
    Edge；那个 Edge 发现 ``--user-data-dir`` 已被前一个 Edge 占着，就把 URL
    转交过去然后自己退出 → 第二个实例的 ``proc.wait()`` 立刻返回 → 它带着服务
    一起退出 → 用户那个窗口就连不上了。

    实测（2026-10-22）：双击第二次时进程 1.7 秒就退出，日志里还留着
    「safe-delete 删不掉 Sessions 文件」（因为前一个 Edge 还占着）。
    """
    allow_reuse_address = False
    daemon_threads = True


def _bios_autoboot(state):
    """启动时在后台把 BIOS **固化**进程序目录 —— 静默，失败就算了。

    用户的原话：「本机有 PS2 BIOS，能不能集成进软件里，不要再次指定」。
    三种情况：

    * 已经在 ``<程序目录>\\bios\\`` 里 → 什么都不做（``autodiscover`` 直接返回）
    * 能从 ``psf_bios.txt`` / 环境变量 / 模拟器目录找到 → 复制一份进 ``bios/``
    * 找不到 → 不管，界面上给「自动查找 BIOS」按钮

    ⚠ 必须放后台线程：``find_bios`` 会逐个目录跑解码器验证，最坏要几秒；
    卡在 ``serve()`` 里会让窗口启动变慢（用户对启动速度很敏感）。
    """
    try:
        import psf_native
        before = psf_native.status(force=True)
        if before.get("bios_bundled"):
            return                       # 早就固化了，别白扫一遍
        r = psf_native.autodiscover()
        if r.get("ok") and r.get("bundled"):
            state.log("📦 PSF BIOS 已集成到程序目录：%s" % r["path"])
            state.log("   以后换版本、源文件被删都不用再指定")
    except Exception:                    # noqa: BLE001
        pass                             # 这只是个便利功能，绝不该影响启动


def serve(state=None, port=0, host="127.0.0.1"):
    """起服务。返回 (httpd, port, state)。``port=0`` 由系统分配。"""
    state = state or AppState()
    httpd = ThreadedServer((host, port), make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=_bios_autoboot, args=(state,), daemon=True).start()
    # v6.0.11：封面根改成 ``<下载根>/Artworks/<机种>/<游戏名>/``。
    # 顺手把旧位置（``%LOCALAPPDATA%\MSU1Converter\covers``）的图搬过来，
    # 搬的是副本、旧目录保留，后台线程，失败不影响服务。
    try:
        dl_state().start_cover_migration()
    except Exception:                             # noqa: BLE001
        pass
    return httpd, httpd.server_address[1], state


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 0
    httpd, real, state = serve(port=port)
    url = "http://127.0.0.1:%d/" % real
    print("RetroMusic Lab 后端已启动: %s" % url, flush=True)
    print("（浏览器打开即可；关掉本进程服务就停）", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n退出", flush=True)
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
