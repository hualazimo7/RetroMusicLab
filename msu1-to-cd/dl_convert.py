#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dl_convert.py — 下载窗口「下载后自动转 WAV」的转换基座（v4.0 新增）。

设计文档：deliverables/software-company/dl-convert-design-2026-10-18.md
PRD：     deliverables/software-company/dl-convert-prd-2026-10-18.md

======================================================================
三条不可动摇的前提（2026-10-18 实测定案，详见设计 §0）
======================================================================

1. **解码侧零改动**：本模块**一行解码代码都不写**。全部 WAV 产出能力来自
   v3.9 已跑通的 :func:`spc_wav.convert` + :func:`chip_decode.load_chip_paths`。
   下载窗口做的只是「镜像主窗口已有的分流写法」（``msu1_gui.py:1190`` 与
   ``msu1_gui.py:1254``）。``spc_wav.py`` / ``chip_decode.py`` /
   ``chip_formats.py`` / ``msu1_to_cd.py`` / ``joshw_dl.py`` 本次一行不动。

2. **必须按扩展名分流**（:func:`policy_for`，H1）::

       policy = "id666" if ext == ".spc" else "chip"

   ``.spc`` 在 ``chip_formats.CHIP_EXTS`` 里，统一走 chip **也能出声**，
   但结尾会**多压一道 1500ms 淡出**（``chip_decode.py:234`` 无条件
   ``_apply_fade``），与用户自己在主窗口转出来的听感不同（PRD B14/B15 实测：
   首个差异在第 2.5 秒、最大样本差 6821/32768 ≈ 21%）。
   **不要图省事统一走 chip，也不要为了「统一」去改 chip_decode。**

3. **调用粒度 = 单文件一次 convert**（H2）。理由不是「批量会整批失败」
   （``load_chip_paths`` 内部已有逐文件 except 兜底），而是：
   ① 只有单文件粒度才能给用户**逐文件准确反馈**；
   ② 才能把**单文件 300s 超时**隔离开。
   **硬性配套**：``used`` 集合与 ``start_idx`` 必须**跨调用共享**，
   否则每个文件都从 ``01_`` 开始编号、跨包互相覆盖。

======================================================================
线程铁律（违反会出事）
======================================================================

* 工作线程**绝对不能碰 Tk**。三个解码回调（``on_log`` / ``on_progress`` /
  ``on_stage``）与事件回调 :attr:`ConvertWorker._emit` **只能 ``queue.put``**。
  历史上跨线程调 Tk 导致解释器重入、界面卡死。
* 判重（``os.stat``）、``probe``、解码**全在消费线程**。主线程只入队 ——
  上百条目放主线程必然卡界面。
* 主线程**永不 join()** 转换线程（会卡界面 300s）。只在取消时置 Event。
* **禁止**在消费线程里 ``join(300)`` 然后放弃 —— 那个线程会继续改
  ``used`` / ``start_idx`` 造成编号竞争与覆盖。超时靠**每文件独立 cancel
  Event** + 主线程巡检（见 :meth:`ConvertWorker.current`）。
* 任何识别/转换失败必须**降级 + 明确日志**，绝不抛异常中断整批。
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
import wave
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import spc_wav
import stream_audio

# v4.24: 多轨合并（一个源产出多个 WAV 时再拼一个整轨文件）。
# 模块级 import 而非惰性 import —— PyInstaller 靠静态分析收集依赖。
try:
    import wavmerge
except Exception:                                    # noqa: BLE001
    wavmerge = None

# v4.30: CD-DA 音轨解码（PyAV/FFmpeg）。同样模块级 import —— PyInstaller
# 只认静态导入，惰性 import 会让 av 的 DLL 收不进去。
try:
    import cd_audio
except Exception:                                    # noqa: BLE001
    cd_audio = None

# v4.41: PS1 的「.txtp 包」原生解码（.txtp 索引 -> .xa/.DA）。
# 这是**纯 stdlib** 的轻模块，不依赖 libgme / PyAV —— 导入失败也只影响
# .txtp 一种扩展名，所以用 try 兜住即可。
try:
    import psx_txtp
except Exception:                                    # noqa: BLE001
    psx_txtp = None

# v5.1-web-psf: PSF 系（.psf/.minipsf）原生解码。
# ⚠ 它**不是**纯 Python —— 真正干活的是同级的 psf2wav.exe，还需要一份 BIOS。
# 所以这里只兜住「模块在不在」，真正能不能解由 psf_native.decode_pcm 报。
try:
    import psf_native
except Exception:                                    # noqa: BLE001
    psf_native = None

# v5.3: N64 的 USF 系（.miniusf/.usf）原生解码。
# 和 psf_native 同一个形状，但**不需要 BIOS** —— N64 没有这东西。
# 真正干活的是同级的 usf2wav.exe（psflib + lazyusf2 + zlib）。
try:
    import usf_native
except Exception:                                    # noqa: BLE001
    usf_native = None

# v5.3: NDS 的 2SF 系（.mini2sf/.2sf）原生解码。
# 和 usf_native 同一个形状，也**不需要 BIOS** —— vio2sf 的 bios.c 是 HLE。
# 真正干活的是同级的 vio2wav.exe（psflib + vio2sf + zlib）。
try:
    import vio_native
except Exception:                                    # noqa: BLE001
    vio_native = None

# v5.3: Saturn/Dreamcast 的 SSF/DSF 原生解码。
# 一个 exe 管两种机器（核里 version 1 = Saturn、2 = Dreamcast），
# 也**不需要 BIOS**。真正干活的是同级的 ssf2wav.exe
# （psflib + Highly Theoretical + zlib）。
try:
    import ssf_native
except Exception:                                    # noqa: BLE001
    ssf_native = None

# v5.3: GBA 的 GSF/minigsf 原生解码。核是 mGBA 0.10.5 的最小核
# （MINIMAL_CORE=2，只留 GBA），**不需要 BIOS**。真正干活的是同级的
# gsf2wav.exe（psflib + mGBA + zlib）。
try:
    import gsf_native
except Exception:                                    # noqa: BLE001
    gsf_native = None

# v5.9.10: Capcom QSound 的 QSF/miniqsf 原生解码。容器是 PSF（版本字节 0x41），
# 里面装的是 Z80 程序 ROM + SMP 采样 ROM 的块列表，要模拟 Z80 + QSound DSP。
# **不需要 BIOS**（QSound 是街机板上的独立声音子系统）。真正干活的是同级的
# qsf2wav.exe（psflib + highly_quixotic + zlib），内部已重采样到 44100。
try:
    import qsf_native
except Exception:                                    # noqa: BLE001
    qsf_native = None

__all__ = [
    "policy_for", "is_convertible", "wav_scan_numbering", "estimate_files",
    "is_derived_wav", "drop_derived_wavs",
    "probe_worker", "ConvertManifest", "ConvertWorker", "read_last_skips",
    "MANIFEST_NAME", "MANIFEST_VERSION", "BYTES_PER_SEC",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: manifest 文件名（落在 ``<WAV 目录>/`` 下）
MANIFEST_NAME = ".msu1-convert.json"
#: manifest 结构版本。结构变更时整体重建（不试图迁移）。
MANIFEST_VERSION = 1

#: 写音频的版本号。记进 manifest 的 ``by`` 字段。
#:
#: 为什么需要它：v5.9.15~17 转出来的 **MP3 内容是坏的**（libsndfile 不认
#: int16 输入，整数值被当 float 样本解释 → 满耳杂音），但结构指标全对
#: （时长、采样率、声道、体积），所以那些文件看起来完全正常。
#: 用户升级后再点「转 MP3」，``already_done()`` 会说「转过了」直接跳过 ——
#: 坏文件永远留在那儿。记下生产版本，才能识别出「这批是坏的、必须重转」。
#: ⚠ **这是编码器能力标记，不是产品版本号**。产品版本见
#: ``web_ui/server.APP_VERSION``（对外 1.0）。这里必须保持 ≥5.9.18：
#: 低于它，``BAD_BEFORE`` 会把新写出来的 MP3 也判成「旧版坏文件」。
AUDIO_WRITER_VERSION = "6.0.12"

#: ``fmt`` -> 「低于这个版本写出来的产物内容不可信」。空 = 不用管。
#:
#: 只有 MP3 上榜：FLAC / WAV 一直是逐样本无损（有门禁盯着），没坏过。
#: v6.0.0 新增的 OGG **不上榜** —— 它从诞生起就走 float32 归一化 + 落盘自检，
#: 不存在「历史坏产物」这回事。
#:
#: ⚠ 比较走 :func:`_ver_tuple`（数字元组），所以 5.9.18 < 6.0.0 成立，
#:   升到 6.0.0 之后老 MP3 仍然会被正确判成「需重转」。
BAD_BEFORE = {"mp3": "5.9.18"}


def _ver_tuple(v):
    """``"5.9.18"`` -> ``(5, 9, 18)``。解析不了当 ``(0,)``（视为很旧）。"""
    try:
        parts = []
        for x in str(v or "").strip().split("."):
            parts.append(int(x))
        return tuple(parts) if parts else (0,)
    except (TypeError, ValueError):
        return (0,)


def _ver_lt(a, b):
    """``a`` 比 ``b`` 旧吗。位数不齐时补零（``5.9`` == ``5.9.0``）。"""
    ta, tb = _ver_tuple(a), _ver_tuple(b)
    n = max(len(ta), len(tb))
    ta = ta + (0,) * (n - len(ta))
    tb = tb + (0,) * (n - len(tb))
    return ta < tb

#: 44.1kHz × 2ch × 16bit = 176400 字节/秒。与 spc_wav.BYTES_PER_SEC 同源，
#: 这里独立定义是为了让本模块**不依赖 spc_wav 的内部命名**（口径相同即可）。
BYTES_PER_SEC = 44100 * 2 * 2

#: 输出文件名里编号的正则：``01_Track`` -> 1（见 :func:`wav_scan_numbering`）
_IDX_RE = re.compile(r"^(\d+)_")

#: 「本程序写出来的成品音频」扩展名（小写，带点）。
#:
#: :func:`wav_scan_numbering` 用它判断目录里哪些文件算「上一轮的产物」——
#: 漏一个扩展名，那个格式的第二轮转换就会从头编号、把上一轮**逐个覆盖**。
#: 所以加格式时**必须**同步这里（v5.9.15 加 mp3 就踩过这个坑的预言）。
_AUDIO_EXTS = (".wav", ".flac", ".mp3")


def _skip_to_text(item) -> str:
    """``last_skips`` 的单条 -> 人类可读文本。

    ⚠ **不能直接 ``str(item)``**：实测 chip_decode 填的是 **dict**，形如
    ``{'t': 'skip', 'path': 'D:\\\\...\\\\trunc.nsf', 'msg': '解码库打开失败: ...'}``。
    直接 str() 会把 ``{'t': 'skip', 'path': ...}`` 这种 Python 字面量
    **原样打到用户界面上**（实测已复现），比不给提示还糟。
    """
    if isinstance(item, dict):
        msg = str(item.get("msg") or "").strip()
        path = str(item.get("path") or "")
        if msg and path:
            return "%s（%s）" % (msg, os.path.basename(path))
        return msg or os.path.basename(path) or "未知原因"
    text = str(item).strip()
    return text or "未知原因"


def read_last_skips() -> Optional[str]:
    """安全读取 :func:`chip_decode.load_chip_paths` 的 ``last_skips``（B18）。

    ============================ 为什么需要这个 ============================
    单文件解码失败**不抛异常**，而是落在 ``chip_decode`` 的**函数属性**
    ``load_chip_paths.last_skips`` 里（``chip_decode.py:571`` 填充、
    ``:581`` 赋值），然后**无人问津**。所以转换统计会是::

        {'ok': 0, 'fail': 0, 'errors': [], 'files': []}

    只看 ``fail`` 会把它当成功 -> **静默**（PM 实测：截断/全零/合法头+零数据
    三种坏 NSF 都是这个值，且它们的 ``probe`` 还都判为合法 NSF，
    **光靠 probe 过滤挡不住**，必须转换后判产出数）。

    ⚠ 三个必须 getattr 的理由：
      1. **chip 分支专有** —— ``policy="id666"`` 走 ``core.load_spc_paths``，
         压根不调它；
      2. 它是**函数属性**（v3.9 遗留写法），不是模块常量，**未调用过就不存在**；
      3. 未来改名/移除 -> 直接属性访问会 AttributeError 打断转换。

    任何异常返回 ``None`` —— **读不到绝不影响主流程**。

    Returns:
        拼接后的跳轨说明（最多 5 条，**已转成可读文本**），或 ``None``
        （无跳过 / 读不到）。
    """
    try:
        import chip_decode
    except Exception:                                    # noqa: BLE001
        return None
    try:
        loader = getattr(chip_decode, "load_chip_paths", None)
        if loader is None:
            return None
        skips = getattr(loader, "last_skips", None)
        if not skips:
            return None
        return "、".join(_skip_to_text(x) for x in list(skips)[:5])
    except Exception:                                    # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# H1：格式分流（唯一实现点）
# ---------------------------------------------------------------------------

def _cd_exts() -> tuple:
    """CD 音轨扩展名（懒取 ``cd_audio.CD_EXTS``，模块缺失时退回空元组）。

    单独抽出来是为了让 :func:`policy_for` / :func:`is_convertible` 在
    ``cd_audio`` 打不开时**行为与 v4.29 一致**（只转芯片音源），
    而不是抛异常把整批队列带崩。
    """
    try:
        import cd_audio
        return tuple(cd_audio.CD_EXTS)
    except Exception:                                    # noqa: BLE001
        return ()


def _psf_exts() -> tuple:
    """PSF 系扩展名（PS1 的 ``.psf`` / ``.minipsf`` / ``.psf1`` / ``.minipsf1``
    + PS2 的 ``.psf2`` / ``.minipsf2``）。

    与 :func:`_cd_exts` 同思路：``psf_native`` 缺失时退回硬编码元组，
    让整批队列按「不认识这个扩展名」跳过，而不是抛异常带崩。
    """
    if psf_native is not None:
        try:
            return tuple(psf_native.PSF_ALL_EXTS)
        except Exception:                                # noqa: BLE001
            pass
    return (".psf", ".psf1", ".minipsf", ".minipsf1", ".psf2", ".minipsf2")


def _usf_exts() -> tuple:
    """USF 系扩展名（``.miniusf`` / ``.usf``）。

    与 :func:`_psf_exts` 同思路：``usf_native`` 缺失时退回空元组，
    让整批队列按「不认识这个扩展名」跳过，而不是抛异常带崩。
    """
    if usf_native is not None:
        try:
            return tuple(usf_native.USF_EXTS)
        except Exception:                                # noqa: BLE001
            pass
    return (".miniusf", ".usf")


def _vio_exts() -> tuple:
    """2SF 系扩展名（``.mini2sf`` / ``.2sf``）。

    与 :func:`_usf_exts` 同思路：``vio_native`` 缺失时退回硬编码元组，
    让整批队列按「不认识这个扩展名」跳过，而不是抛异常带崩。
    """
    if vio_native is not None:
        try:
            return tuple(vio_native.VIO_EXTS)
        except Exception:                                # noqa: BLE001
            pass
    return (".mini2sf", ".2sf")


def _ssf_exts() -> tuple:
    """SSF/DSF 系扩展名（``.ssf`` / ``.minissf`` / ``.dsf`` / ``.minidsf``）。

    与 :func:`_vio_exts` 同思路：``ssf_native`` 缺失时退回硬编码元组。
    """
    if ssf_native is not None:
        try:
            return tuple(ssf_native.SSF_EXTS)
        except Exception:                                # noqa: BLE001
            pass
    return (".ssf", ".minissf", ".dsf", ".minidsf")


def _gsf_exts() -> tuple:
    """GSF 系扩展名（``.gsf`` / ``.minigsf``）。

    与 :func:`_ssf_exts` 同思路：``gsf_native`` 缺失时退回硬编码元组。
    """
    if gsf_native is not None:
        try:
            return tuple(gsf_native.GSF_EXTS)
        except Exception:                                # noqa: BLE001
            pass
    return (".gsf", ".minigsf")


def _qsf_exts() -> tuple:
    """QSF 系扩展名（``.qsf`` / ``.miniqsf``）。

    与 :func:`_gsf_exts` 同思路：``qsf_native`` 缺失时退回硬编码元组。
    ⚠ ``.qsflib`` **不在**这里 —— 那是 45 首共用的 4MB 采样库，单独转没有
    意义，留着它由 :data:`SKIP_REASONS` 给出说明。
    """
    if qsf_native is not None:
        try:
            return tuple(qsf_native.QSF_EXTS)
        except Exception:                                # noqa: BLE001
            pass
    return (".qsf", ".miniqsf")


def policy_for(src: str) -> str:
    """按扩展名分流（H1）。``.spc`` -> ``"id666"``，其余 -> ``"chip"``。

    这是「别调错入口」的一行守卫，**不是**要实现什么能力。两条路径 v3.9
    都已跑通，本函数只负责选对：

    * ``"id666"`` 走 ``core.load_spc_paths`` → ``_spc_play_plan``，用 ID666
      标签里的时长与 fade 值（作者标 fade=0 就是完全不淡出）；
    * ``"chip"`` 走 ``chip_decode.load_chip_paths``，结尾**无条件** 1500ms
      淡出（``chip_decode.py:234``）—— 那对无 ID666 标签的 NSF/HES 是正确
      行为，但对 SPC 就是「擅自消音」。

    入队时定一次、消费时复核一次（防御性冗余，防止有人未来从别的入口塞
    路径进来）。

    Args:
        src: 源文件路径或文件名，大小写与有无目录都不影响判定。

    Returns:
        ``"id666"`` / ``"txtp"`` / ``"cd"`` / ``"chip"``；空输入按 ``"chip"``
        （更保守的一侧）。

    v4.41 起多了 ``"txtp"``：PS1 的 JoshW 包用 ``.txtp`` 做轨道索引（一行
    ``sound/xxx.xa #I 2153088``），指向包内 ``.xa``（CD-XA 扇区）或 ``.DA``
    （裸 PCM16LE）。这两种都能**纯原生解**，见 :mod:`psx_txtp`。

    v5.1-web-psf 起多了 ``"psf"``：``.psf`` / ``.minipsf`` 是 PS1 的内存快照，
    要真的模拟 R3000A + SPU —— 走 :mod:`psf_native` → 同级的 ``psf2wav.exe``。
    **需要一份 BIOS**，没有就明确失败，绝不产出静音 WAV。
    """
    ext = os.path.splitext(str(src or ""))[1].lower()
    if ext == ".spc":
        return "id666"
    # v4.41: PS1 的 .txtp 轨道索引（JoshW 包）—— 它指向包内 sound/ 下的
    # .xa / .DA，解出来就是成品 PCM，**不需要模拟器也不需要 foobar2000**。
    # 必须放在 `_cd_exts()` 之前判：两者都不重叠，但语义上它更接近 chip。
    if ext == ".txtp":
        return "txtp"
    # v5.2.4: 独立的 .XA（JoshW 的平铺包：「15 个 .XA + 一个 .PSF」）。
    # 以前这种包**只转得出那个 .PSF**，15 个 XA 全丢 —— 因为 .xa 既不在
    # CHIP_EXTS 里，也没被当成一个音源。解码走 psx_txtp（纯 Python）。
    if ext == ".xa":
        return "xa"
    # v5.1-web-psf: PSF 系。放在 `_cd_exts()` 之前 —— `.psf` 不是 CD 音轨。
    if ext in _psf_exts():
        # v5.9.11：``.psf`` 撞名。GameCube 的《Conflict》系列也用这个扩展名，
        # 但那是 Pivotal 自家的**预录流**（PSX-ADPCM / NGC-DSP），没有程序段、
        # 不需要 BIOS，纯 Python 就能解（:mod:`pivotal_psf`，与 vgmstream
        # 逐样本最大差 0）。判据是第 4 个字节，两边互不重叠。
        # ⚠ 判错了症状很误导：会把能解的包报成「BIOS 有问题」。
        try:
            import pivotal_psf
            if pivotal_psf.is_pivotal(src):
                return "pivotal"
        except Exception:                            # noqa: BLE001
            pass
        return "psf"
    # v5.3: N64 的 USF 系（.miniusf / .usf）。同样不是 CD 音轨 —— 它是
    # 「状态快照 + .usflib 采样库」，要模拟 R4300 + RSP。走 usf2wav.exe。
    if ext in _usf_exts():
        return "usf"
    # v5.3: NDS 的 2SF 系（.mini2sf / .2sf）。同样是「快照 + .2sflib ROM 库」，
    # 要模拟 ARM9/ARM7 双核 + DeSmuME 的 SPU。走 vio2wav.exe。
    if ext in _vio_exts():
        return "vio"
    # v5.3: Saturn/Dreamcast 的 SSF/DSF 系。快照里是「驱动程序 + 声音 RAM」，
    # 要跑 SH-2 + SCSP（Saturn）或 ARM7 + AICA（Dreamcast）。走 ssf2wav.exe。
    if ext in _ssf_exts():
        return "ssf"
    # v5.3: GBA 的 GSF 系（.gsf / .minigsf）。快照里是**一整个 GBA ROM**，
    # 要模拟 ARM7TDMI + GBA 的 DMA/定时器。走 gsf2wav.exe。
    if ext in _gsf_exts():
        return "gsf"
    # v5.9.10: Capcom QSound 的 QSF 系（.qsf / .miniqsf）。同样是「程序 ROM
    # + 采样 ROM」，要模拟 Z80 + QSound DSP 的 16 个声道。走 qsf2wav.exe。
    # ⚠ 必须在 `_cd_exts()` 之前判 —— `.qsf` 不是 CD 音轨。
    if ext in _qsf_exts():
        return "qsf"
    # v5.2.6: 流式音频（.adx / .mod / .hwas / .swav / .dvi）。同样必须在
    # `_cd_exts()` 之前判 —— 它们虽然是「一条文件一首」，但采样率/声道/
    # 分块规则都不是 CD 音轨那一套。
    # v5.2.7: `.hwas`/`.swav`/`.dvi` 三个后缀已接上（纯 Python + numpy，
    #   与 vgmstream r2117 逐样本一致），不再是「不支持」。
    if ext in stream_audio.ALL_EXTS:
        return "stream"
    # v4.30: CD-DA 音轨（.ape/.flac/.wav…）走 PyAV(FFmpeg)，不是 libgme。
    # PCE-CD 这类包是「.hes + .ape」混装，CD 轨也是游戏音源的一部分。
    if ext in _cd_exts():
        return "cd"
    return "chip"


def is_convertible(src: str) -> bool:
    """这个路径是不是本功能能转的音源（芯片音源 **或** CD-DA 音轨）。

    v4.30 起包含 CD 音轨（``cd_audio.CD_EXTS``）—— PCE-CD 这类包是
    「芯片音源 + CD 音轨」混装，两者都属于游戏音源，都要转。

    ⚠ **只能用** :data:`chip_formats.CHIP_EXTS` 判定，
    **不能用** ``spc_wav.collect_supported()`` / ``chip_formats.collect_supported()``
    —— 后者用的是 ``ALL_EXTS``（含 ``.cue`` / ``.bin`` / ``.mp3``），
    会把 MSU-1 与 CD 镜像文件误收进转换队列（PRD FR-C3.2）。

    Args:
        src: 待判定路径。

    Returns:
        True 表示在 11 种受支持扩展名之内。
    """
    ext = os.path.splitext(str(src or ""))[1].lower()
    if not ext:
        return False
    # v4.41: PS1 的 .txtp（JoshW 包轨道索引）。不进 CHIP_EXTS —— 它既不是
    # 芯片格式，也解析不出「格式 id」，所以单独放行。
    if ext == ".txtp":
        return True
    # v5.2.4: 独立 .XA —— 平铺的 PS1 包（没有 .txtp 索引）靠它才转得出来。
    if ext == ".xa":
        return True
    # v5.1-web-psf: PSF 系（PS1 内存快照）。同样不在 CHIP_EXTS 里 ——
    # libgme 的 psx 分支本工程没编，它走的是外挂的 psf2wav.exe。
    if ext in _psf_exts():
        return True
    # v5.3: N64 的 USF 系（.miniusf / .usf）—— 走外挂的 usf2wav.exe。
    # ⚠ `.usflib` **不在这里**：那是配套的采样库，单独转没有意义，
    #   留着它由 SKIP_REASONS 给出「只配合 .miniusf 用」的说明。
    if ext in _usf_exts():
        return True
    # v5.3: NDS 的 2SF 系（.mini2sf / .2sf）—— 走外挂的 vio2wav.exe。
    # ⚠ `.2sflib` **不在这里**：那是配套的 ROM 库，单独转没有意义，
    #   留着它由 SKIP_REASONS 给出「只配合 .mini2sf 用」的说明。
    if ext in _vio_exts():
        return True
    # v5.3: SSF/DSF 系 —— 走外挂的 ssf2wav.exe。
    # ⚠ `.ssflib` / `.dsflib` **不在这里**：那是配套库，单独转没有意义。
    if ext in _ssf_exts():
        return True
    # v5.3: GBA 的 GSF 系 —— 走外挂的 gsf2wav.exe。
    # ⚠ `.gsflib` **不在这里**：那是配套的 ROM 库，单独转没有意义。
    if ext in _gsf_exts():
        return True
    # v5.9.10: Capcom QSound 的 QSF 系 —— 走外挂的 qsf2wav.exe。
    # ⚠ `.qsflib` **不在这里**：那是全曲共用的采样库，单独转没有意义。
    if ext in _qsf_exts():
        return True
    # v5.2.6: 流式音频（.adx / .mod）。
    if ext in stream_audio.ALL_EXTS:
        return True
    if ext in _cd_exts():
        return True
    try:
        import chip_formats
    except Exception:                                    # noqa: BLE001
        # chip_formats 缺失时只放行 .spc —— 它走 id666，不需要芯片解码器。
        return ext == ".spc"
    return ext in chip_formats.CHIP_EXTS


def is_derived_wav(path: str) -> bool:
    """这个 ``.wav`` 是不是「同目录里另一个音源转出来的产物」。

    ## 为什么需要这个判据（v6.0.6 修的一个真 bug）

    下载页的「转 WAV」把 WAV **写在源文件旁边**：``xxx.fsb`` → ``xxx.fsb.wav``。
    于是那个目录里同一首曲子有两个文件，而**两个都** ``is_convertible()`` 为真：

    | 文件 | policy | 说明 |
    |---|---|---|
    | ``xxx.fsb`` | ``stream`` | 原始音源 |
    | ``xxx.fsb.wav`` | ``cd`` | 我们自己刚转出来的 |

    用户先点「转 MP3」（当时只有 ``.fsb``）得到 10 首，再点「转 FLAC」
    （此时 ``.fsb.wav`` 已经在）得到 **20 首** —— 每首出现两次，
    ``01_xxx.flac`` 和 ``02_xxx.fsb.flac`` 内容一模一样。

    判据只有一条：``A.wav`` 是衍生品 ⟺ 同目录的 ``A`` 存在，且 ``A``
    本身也是音源。这样**不会误伤**那些「正片源就是 .wav」的机种
    （它们的 ``song.wav`` 去掉 ``.wav`` 之后不是音源，照常保留）。
    """
    s = str(path or "")
    if not s.lower().endswith(".wav"):
        return False
    base = s[:-4]
    if not base:
        return False
    try:
        return is_convertible(base) and os.path.isfile(base)
    except OSError:
        return False


def drop_derived_wavs(paths: Sequence[str]) -> List[str]:
    """从一批音源里去掉「由同目录另一个音源转出来的 WAV」。

    见 :func:`is_derived_wav` —— 那是判据，这里只是批量应用。
    同一批里同时出现 ``A`` 和 ``A.wav`` 时也会丢掉 ``A.wav``（哪怕 ``A``
    还没落盘，比如压缩包清单还没解压）。

    ⚠ 只在**枚举音源**的地方调，别拿去过滤用户手选的单文件列表 ——
    用户明确点了某个 ``.wav``，就该转它。

    Args:
        paths: 待过滤的路径序列（保序）。

    Returns:
        过滤后的列表（新对象，不改入参）。
    """
    items = [str(p) for p in (paths or ()) if p]
    if len(items) < 2:
        return items
    have = set(os.path.normcase(os.path.normpath(p)) for p in items)
    out = []
    for p in items:
        if p.lower().endswith(".wav"):
            base = p[:-4]
            try:
                if is_convertible(base) and (
                        os.path.normcase(os.path.normpath(base)) in have
                        or os.path.isfile(base)):
                    continue
            except OSError:
                pass
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# 「为什么这个文件转不了」—— 给用户一句能读懂的话（v5.2.6）
# ---------------------------------------------------------------------------
#
# 起因：joshw 的 8 个机种包实测 665 个音频文件，本工具只能转其中 17 个
# （Dreamcast 的 .adx 7 个、GBA 的 .mod 10 个）。剩下 648 个**不是参数
# 没调对**，而是缺主机模拟器核心 —— 但用户看不到这件事：他看到的只是
# 「下载完、解压完，一首 WAV 都没出来」。
#
# 所以这里给每个后缀一句原因。文案只写**实测确认过**的东西（数据出处
# README ⚠58：每个格式的文件大小区间、容器头、频谱判据都是逐个量过的），
# 不猜、不写「可能」「暂不支持」这种等于没说的词。
#
# ⚠ 文案里**不要用圆括号**：外层拼句时已经套了一层「N 个 .ext（原因）」，
#   里面再套一层括号会变成「（…（…）…）」，日志区里极难读。
SKIP_REASONS: Dict[str, str] = {
    # ---- 内存快照族：文件本身只有一两百字节，音乐在几 MB 的 .XXXlib 里 ----
    # ⚠ `.minigsf` 也已经**不在**这张表里 —— v5.3 接上了：
    #   外挂 gsf2wav.exe（psflib + mGBA 最小核），与上游 plugin.cc 逐样本一致。
    ".gsflib": "GBA 的 ROM 采样库，只配合 .minigsf 用",
    # ⚠ `.miniusf` / `.usf` 已经**不在**这张表里 —— v5.3 接上了：
    #   外挂 usf2wav.exe（psflib + lazyusf2），与上游 dumpresampled 逐样本一致。
    ".usflib": "N64 的 ROM 采样库，只配合 .miniusf 用",
    # ⚠ `.mini2sf` / `.2sf` 也已经**不在**这张表里 —— v5.3 接上了：
    #   外挂 vio2wav.exe（psflib + vio2sf），与上游 plugin.cc 逐样本一致。
    ".2sflib": "NDS 的 ROM 采样库，只配合 .mini2sf 用",
    # ⚠ `.ssf` / `.minissf` / `.dsf` / `.minidsf` 也已经**不在**这张表里 ——
    #   v5.3 接上了：外挂 ssf2wav.exe（psflib + Highly Theoretical），
    #   与上游 plugin.cc 逐样本一致。
    ".ssflib": "Saturn 的声音采样库，只配合 .ssf 用",
    ".dsflib": "Dreamcast 的声音采样库，只配合 .dsf 用",
    # ⚠ `.qsf` / `.miniqsf` **不在**这张表里 —— v5.9.10 接上了：
    #   外挂 qsf2wav.exe（psflib + highly_quixotic），内部已重采样到 44100。
    #   留着的只有 `.qsflib` —— 45 首共用一份 4MB 采样 ROM，单独转没意义。
    ".qsflib": "Capcom QSound 的采样库，只配合 .miniqsf 用",
    # ⚠ `.psf` / `.minipsf` / `.psf1` / `.minipsf1` 已经**不在**这张表里 ——
    #   v5.2 接上了：外挂 psf2wav.exe（psflib + Highly Experimental），
    #   与上游 plugin.cc 逐样本一致。
    # ⚠ `.psf2` / `.minipsf2` 也**不在**这张表里 —— v5.4 接上了：
    #   同一把 psf2wav.exe 走 psf2fs VFS 装载路径，与上游 plugin.cc 逐样本一致。
    #   现在只剩 .psflib / .psf2lib 这类「库文件」被拒。
    # ⚠ `.hwas` / `.swav` / `.dvi` 已经**不在**这张表里 —— v5.2.7 接上了：
    #   纯 Python + numpy 解码，与 vgmstream r2117 逐样本一致（78 个文件全 100%）。
    #   表里留着它们会让 `explain_skipped` 把能转的格式说成转不了。
    # ---- 还没支持的机种 ----
    # v5.4: `.sgc` 已移出本表 —— 改用 **kode54/Game_Music_Emu** 重编 libgme 后，
    #   SGC 和别的 libgme 格式走完全同一条路（Sgc_Emu + Sms_Apu + Z80_Cpu）。
    #   ⚠ 老注释写的是「libgme 有这个机种，只是本工程没编进去」——**这是错的**，
    #     官方 libgme 0.6.x 从来没有 SGC/WS，只有 kode54 的 fork 才有。
    #     那句注释误导过一整轮排查（去重编官方 libgme，白忙）。
    # v5.5: `.wsr` 也移出本表了 —— WonderSwan 的曲号是**通过 AX 寄存器**
    #   传进去的（WSR 规范：CS=0xFFFF, IP=0, AX=曲号），普通模拟器复位时把
    #   AX 清 0，所以 ROM 的启动桩什么都不放 —— 这才是「WSR 转出来全静音」的
    #   根因，不是「没有核」。现在用 beetle-wswan（mednafen 核）编的
    #   `wsr2wav.exe`，在 retro_load_game 之后直接 `v30mz_set_reg(NEC_AW, N)`。
    #   见 `wsr_native.py` 与 `tools/wswan2wav/`。
    # ---- 还没接的机种 ----
    # v5.6: `.aif` 已移出本表 —— 3DO 的 AIFF/AIFC **不需要整机模拟**。
    #   那是游戏自己存好的流式音频（和 .adx/.hwas 同类），容器就是苹果 AIFF：
    #   8bit PCM / 16bit PCM / SDX2（3DO 自家的 2:1 平方根增量 ADPCM）。
    #   纯 Python + numpy 解，与 vgmstream 逐样本最大差 0（4 个真实样本）。
    #   ⚠ 旧注释写的「需要 ARM60 + DSP + CEL 整机模拟」是**错的** ——
    #     那是 3DO 的**游戏程序**才需要的东西，预录 BGM 不需要。
    #     这个误解让它一直挂在「转不了」里，直到 v5.6 才拆掉。
    #   仍未接：4bit AIFF（XA-ADPCM，Road Rash 那类）—— 见 aif_audio 的说明。
}

#: 不是音频、也不该算进「转不了」的文件 —— 播放列表与元数据。
#: 单独列出来是为了别把「49 个转不了」说成 49 个音频（其实是 48 音 + 1 表）。
#:
#: v5.9.11 加了 ``.txth``：Nintendo 3DS 的包里每个 ``.laac`` 都配一个同名的
#: ``.laac.txth``（vgmstream 的描述文件，写着 ``codec = FFMPEG`` /
#: ``sample_rate = 48000`` / 循环点）。它**不是音频**，只是参数说明 ——
#: 不列在这里的话，3DS 包会显示「219 个转不了」，而其实 219 首全都能转。
NON_AUDIO_EXTS = (".m3u", ".m3u8", ".txt", ".txth", ".tags", ".nfo", ".log",
                  ".md", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ini",
                  ".xml",
                  # v5.9.12：Wwise 的 SoundBank。**实测里面没有音频** ——
                  # AC4 那个 .bnk 只有 BKHD + HIRC + STID 三个块，
                  # 没有 DIDX/DATA（音频表 + 数据），纯粹是层级/元数据。
                  # 真正的音频是旁边那 224 个 .wem，已经各自成曲目了。
                  # 不登记的话，PS4 那个包会多报「2 个转不了」，其实是误伤。
                  ".bnk",
                  # 顺带补上压缩包：joshw 的包里常带 ``!Extras.7z``
                  # （里面是 QuickBMS 脚本或说明），它显然不是音源。
                  ".7z", ".zip", ".rar")


def explain_skipped(paths: Sequence[str], limit: int = 3) -> str:
    """一批文件 → 一句「多少个转不了、什么格式、为什么」。

    ⚠ **可以放心把整批文件全传进来**：函数内部会自己滤掉
    :func:`is_convertible` 为真的路径。这一层是故意加的自保 —— 曾经在
    核对脚本里直接传了整目录，结果输出成「10 个 .mod（不认识的格式）」，
    把**已经能转**的格式说成不认识，比不说话还糟。

    Args:
        paths: 文件路径序列（只用到扩展名，不看目录）。
        limit: 最多点名几种格式，超出的归到「另 N 种」。

    Returns:
        ``""`` 表示没有需要解释的 —— 空输入、全是能转的、或者包里全是
        播放列表 / 封面 / 说明文件（那属于「本来就没音频」，不该说成
        「转不了」）。否则形如::

            48 个转不了：48 个 .minigsf（GBA 内存快照…）；1 个 .m3u（…）

    v5.2.6 新增。替代下载页原先那句「包里没有本工具能直接转换的音源
    （.minigsf、.m3u…）」—— 把扩展名一列了事，对用户等于什么都没说。
    """
    counts: Dict[str, int] = {}
    for p in paths or ():
        s = str(p or "")
        ext = os.path.splitext(s)[1].lower()
        if not ext:
            continue
        if ext not in NON_AUDIO_EXTS and is_convertible(s):
            continue                      # 能转的别算进「转不了」
        counts[ext] = counts.get(ext, 0) + 1
    if not counts:
        return ""

    audio = {e: n for e, n in counts.items() if e not in NON_AUDIO_EXTS}
    if not audio:
        return ""

    total = sum(audio.values())
    # 排序：先按数量降序；同数量时**认识的格式排前面**，免得某个冷门后缀
    # 把「.minigsf ×48」挤出 limit 之外，只剩「1 个 .xyz（不认识的格式）」。
    order = sorted(audio.items(),
                   key=lambda kv: (-kv[1], kv[0] not in SKIP_REASONS, kv[0]))
    parts = []
    for ext, n in order[:limit]:
        reason = SKIP_REASONS.get(ext)
        parts.append("%d 个 %s（%s）"
                     % (n, ext, reason or "不认识的格式"))
    if len(order) > limit:
        parts.append("另 %d 种格式" % (len(order) - limit))
    # 组间用「；」不用「、」—— 每组的括号里已经有逗号，「、」会让分组边界糊掉。
    return "%d 个转不了：%s" % (total, "；".join(parts))


def walk_files(dest, max_depth: int = 3, cap: int = 8000) -> List[str]:
    """列出目录下的**所有**文件（限深限量），给 :func:`explain_skipped` 用。

    为什么要递归：PS1 包是 ``sound/xxx.xa`` 这种一层子目录，某些 JoshW 包
    还会多套一层；只扫本层会漏。为什么要限量：大包解出来可能上万个文件，
    全量 ``os.walk`` 会把下载线程卡住，而这里只需要「有哪些扩展名」。

    Args:
        dest: 目标目录。
        max_depth: 最多往下几层（0 = 只本层文件，不递归）。
        cap: 最多返回多少个路径，到了就提前返回。

    Returns:
        绝对路径列表；目录不存在时返回 ``[]``。
    """
    out: List[str] = []
    d = str(dest or "")
    if not os.path.isdir(d):
        return out
    base = os.path.abspath(d).count(os.sep)
    for root, dirs, names in os.walk(d):
        if os.path.abspath(root).count(os.sep) - base >= max_depth:
            dirs[:] = []
        for n in names:
            out.append(os.path.join(root, n))
            if len(out) >= cap:
                return out
    return out


# ---------------------------------------------------------------------------
# 编号续编扫描（R7：第二次会话静默覆盖用户已有 WAV）
# ---------------------------------------------------------------------------

def wav_scan_numbering(wav_dir: str) -> Tuple[int, Set[str]]:
    """扫描 ``WAV/`` 得出「续编起点」与「已占用文件名集合」。

    ============================ 为什么必须有这个函数 ============================
    ``spc_wav.write_tracks`` 的编号是 ``f"{idx:02d}_{name}.wav"``，``idx`` 从
    传入的 ``start_idx`` 递增。若用户**第二次**点下载（新会话）而
    ``start_idx`` 仍从 0 开始，而 ``WAV/`` 里已有 ``01_~37_``，新产出会
    **逐个覆盖旧文件**，并且 ``used`` 为空 → 连重名都不避让。实测：
    预置 25 字节的 ``01_Stage Clear.wav``，转一次后变成 705644 字节、
    头部 ``RIFF`` —— 用户旧数据被直接覆盖，无任何提示（设计 §5.4 / R7）。

    ============================ 关键细节：键要剥两层 ============================
    ``write_tracks`` 的落盘名与记账键**不是同一个东西**::

        name = core.safe_filename(tr["name"], ...)   # 裸曲名, 无序号无扩展名
        while name.lower() in used: name = f"{base} ({n})"
        used.add(name.lower())                       # ← 键 = **裸曲名** lower()
        wavp = os.path.join(out_dir, f"{idx:02d}_{name}.wav")   # ← 序号/扩展名只在这

    所以扫描侧注入 ``used`` 时必须剥**两层**：先去 ``.wav``，再去 ``NN_`` 前缀。

    实测（架构师复刻 + 我用真实 write_tracks 验证）::

        盘上是 '01_Stage Clear.wav'
          注入 '01_stage clear'（带序号）-> 'Stage Clear' 查不到 -> 不避让 ✗
          注入 'stage clear'（去序号）  -> 'Stage Clear' 查到   -> 避让✓

    ⚠ **两种错写都不报错**，只让重名避让静默失效：
    ``name.lower() + ".wav"`` 与 ``name[:-4]``（忘了去序号）。
    ⚠ ``max_idx`` 必须**照常**从 ``NN_`` 前缀里取 —— 前缀在键上要去掉，
    在编号上要用它。

    Args:
        wav_dir: WAV 输出目录。不存在 / 读不到时返回 ``(0, set())``，
            当作空目录处理（不抛异常）。

    Returns:
        ``(max_idx, used)``：``max_idx`` 是目录里出现过的**最大编号**
        （调用方从 ``max_idx + 1`` 续编）；``used`` 是所有成品音频的
        **裸曲名小写**集合（已剥 ``NN_`` 前缀与扩展名，与
        ``write_tracks`` 的键严格一致）。

    ⚠ v5.9.7：**``.flac`` 必须一起扫**。``write_tracks(fmt="flac")`` 落盘的
    是 ``01_曲名.flac``，只认 ``.wav`` 的话这个函数会返回 ``(0, set())``
    —— 于是「第二次点下载」时 ``start_idx`` 又从 1 开始、``used`` 又为空，
    正好把上一轮转好的 FLAC **逐个覆盖**。这正是本函数存在的理由（R7），
    换个扩展名它就原样复发。
    ⚠ v5.9.15：``.mp3`` 同理（这就是上面那句话预言的复发点）。扩展名
    改成从 ``_AUDIO_EXTS`` 查，加格式只改那一处。
    """
    max_idx = 0
    used: Set[str] = set()
    try:
        names = os.listdir(wav_dir or ".")
    except OSError:
        return 0, used
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext.lower() not in _AUDIO_EXTS:
            continue
        if not stem:
            continue
        m = _IDX_RE.match(stem)
        if m:
            try:
                max_idx = max(max_idx, int(m.group(1)))
            except ValueError:                 # 理论上不会发生，防御性保留
                pass
            # ② 再去 "NN_" 前缀 -> 裸曲名, 与 write_tracks 的 used.add 对齐
            stem = stem[m.end():]
        if not stem:
            continue
        used.add(stem.lower())
    return max_idx, used


# ---------------------------------------------------------------------------
# 体量预估（FR-C2.4 / AC-C22）
# ---------------------------------------------------------------------------

def estimate_files(paths: Sequence[str], chip_secs: float = 150.0) -> Dict[str, float]:
    """预估这批文件能产出多少首 / 多少秒 / 多少字节。**永不抛异常**。

    口径（PRD FR-C2.4）：对每个文件用 ``chip_formats.probe()`` 读
    ``n_tracks`` 与时长（``exact_secs`` 或按 ``chip_secs`` 估），累加得总
    首数与总秒数；体积 = 总秒数 × :data:`BYTES_PER_SEC`。

    ⚠ **逐文件 try/except**：任一文件 ``probe()`` 抛异常 / 超时 → 按
    **1 首**计入并继续，**不得中断整个刷新**（FR-C2.5）。

    ⚠ 这是**估算**，界面显示必须带 ``≈``。它不参与任何控制流。

    Args:
        paths: 源文件路径列表。
        chip_secs: 无文件自带时长时的默认单遍秒数（决策 D3 = 150）。

    Returns:
        ``{"files": 文件数, "tracks": 总首数, "secs": 总秒数,
        "bytes": 预估字节, "bad": probe 失败的文件数}``。``files`` / ``bad``
        是 float（JSON 友好），用时由调用方 ``int()``。
    """
    try:
        default_secs = float(chip_secs)
    except (TypeError, ValueError):
        default_secs = 150.0
    if default_secs <= 0:
        default_secs = 150.0

    n_files = 0
    n_tracks = 0
    secs = 0.0
    bad = 0
    try:
        import chip_formats
    except Exception:                                # noqa: BLE001
        chip_formats = None                           # type: ignore[assignment]

    for p in (paths or ()):
        path = str(p or "")
        if not path:
            continue
        if not is_convertible(path):
            continue
        n_files += 1
        try:
            # v4.30: CD-DA 音轨（.ape/.flac…）用 PyAV 读真实时长。
            # 拿不到时长就按默认 150s 估（与 chip 同口径：宁高不低）。
            if policy_for(path) == "cd":
                est = {}
                if cd_audio is not None:
                    try:
                        est = cd_audio.probe(path)
                    except Exception:                # noqa: BLE001
                        est = {}
                one = float(est.get("secs") or 0.0)
                n_tracks += 1
                secs += one if one > 0.2 else default_secs
                continue
            # v4.41: .txtp —— 时长能从目标文件大小精确算出来（.xa 每扇区
            # 2016 采样/37800Hz，.DA 是 size/(rate*ch*2)），所以这里给的是
            # **真实值**而不是 150s 兜底。恶魔城包 41 首按 150s 会估成
            # 6.2GB（实际约 3.4GB），磁盘门禁会误判「空间不足」。
            if policy_for(path) == "txtp":
                one = 0.0
                if psx_txtp is not None:
                    try:
                        est = psx_txtp.probe_txtp(path)
                        if est.get("ok"):
                            one = float(est.get("seconds") or 0.0)
                    except Exception:                # noqa: BLE001
                        one = 0.0
                n_tracks += 1
                secs += one if one > 0.2 else default_secs
                continue
            # v5.2.4: 独立 .XA —— 扇区数 × 0.0533s 就是准确时长，
            # 但**要真的扫一遍扇区头**（文件 5MB，几十毫秒，可接受）。
            # 一个 .XA 里可能有多条子流，轨数按子流数算，别按文件数算。
            if policy_for(path) == "xa":
                one = 0.0
                n_streams = 1
                if psx_txtp is not None:
                    try:
                        est = psx_txtp.probe_xa(path)
                        if est.get("ok"):
                            one = float(est.get("seconds") or 0.0)
                            n_streams = max(1, int(est.get("streams") or 1))
                    except Exception:                # noqa: BLE001
                        one = 0.0
                n_tracks += n_streams
                secs += one if one > 0.2 else default_secs * n_streams
                continue
            if policy_for(path) == "stream":
                # 流式音频 —— 一条文件一首。时长只读头部就够：
                #   .hwas/.swav/.dvi 有采样率 + 样本数，一除就出来；
                #   .adx 问 PyAV；.mod 走 mod_audio.probe 的序列扫描。
                # 全部**不解码**，几十个文件也不卡。
                one = 0.0
                try:
                    one = float(stream_audio.probe(path).get("secs") or 0.0)
                except Exception:                # noqa: BLE001
                    one = 0.0
                n_tracks += 1
                secs += one if one > 0.2 else default_secs
                continue
            if policy_for(path) == "psf":
                # v5.1-web-psf: PSF 的时长在文件尾的 [TAG] 块里（length=分:秒），
                # 只读尾部 256KB 就能拿到 —— **不要**为了估算去起模拟器
                # （一个包几十首，每首 0.4 秒就是半分钟）。
                one = 0.0
                if psf_native is not None:
                    try:
                        one = float(psf_native.length_tag(path) or 0.0)
                    except Exception:                # noqa: BLE001
                        one = 0.0
                n_tracks += 1
                secs += one if one > 0.2 else default_secs
                continue
            if chip_formats is None:
                raise RuntimeError("chip_formats 不可用")
            info = chip_formats.probe(path)
            # ⚠⚠ **只用 n_tracks，绝不用 ChipInfo.n_tracks_used**（2026-10-18 回滚）。
            #
            # 曾试过用 `n_tracks_used`(HES head[0x15]) 做估算，**实测证明有害，已回滚**：
            #   ① `0x15` 不是轨数。实测 7/7 满足 `size == (0x15+1) × 256`
            #      —— 它是「预分配槽位数」(每槽 256 字节)，不是占用量。
            #   ② 真解码数产出（7 样本）后看清了全貌：
            #        总量 0x15=761 vs 产出 703 -> **1.08，几乎完美**
            #        但单文件 0x15/产出 跨 **0.42 ~ 5.52**（7 个里 3 个低估）
            #      -> 要「绝不低估」就得乘安全系数，**临界点 = 1792/761 = 2.355**
            #         （取 2.4 是四舍五入；写 2.4 会让人复算不出临界点）
            #         761×2.355 = 1793 ≈ 256 口径的 1792 -> **收益归零**。
            #         再往上（如 2.55）就是**负收益**，比现状更差。
            #   ③ 低估正是本功能**最危险**的方向：会让转换写到一半撞磁盘门禁暂停，
            #      而高估只是数字难看、零实际损失（见 `_disk_ok` 的 256MB 地板）。
            #   所以估算**宁可高估 11 倍也不低估 2 倍**，一律走 n_tracks(=256)。
            #
            # ---- v4.29 唯一的例外：同目录 m3u 给出了真实曲目表 ----
            # HES 的 256 是**预分配槽位**，PCE-CD 包的真实曲目表在同目录 .m3u
            # 里（曲号可能不连续、每轨带长度）。按 256×150s 估 = 6.3 GB/包，
            # 磁盘剩不到 7GB 时 `_disk_ok` 直接判死 -> 转换**根本不会开始**
            # （用户看到的就是「下载完转不了」）。有 m3u 时按真实曲目与长度估，
            # 口径是「真实值」而不是「安全上限」；真写超了门禁会暂停再续，
            # 那是设计内的安全行为（见 `_disk_ok` / `cv_paused`）。
            ids = list(getattr(info, "track_ids", None) or ())
            if ids:
                lens = getattr(info, "track_lengths", None) or {}
                total_secs = 0.0
                for tid in ids:
                    try:
                        one = float(lens.get(tid, 0.0) or 0.0)
                    except (TypeError, ValueError):
                        one = 0.0
                    total_secs += one if one > 0.2 else default_secs
                n_tracks += len(ids)
                secs += total_secs
                continue
            n = int(getattr(info, "n_tracks", 0) or 0)
            if n <= 0:
                n = 1
            per = float(getattr(info, "per_track_secs", 0.0) or 0.0)
            if per <= 0:
                per = default_secs
            # 多轨文件的 per_track_secs 已是「单轨」；总时长 = 单轨 × 轨数。
            # ⚠ 显示层必须保留「≈」（FR-C2.4）：256 是**解码分母**口径，
            #   实际产出行数由 chip_decode 按「首帧峰值<300 跳空轨」决定，
            #   静态估算无法预知 —— 宁可报大数也不报小数。
            n_tracks += n
            secs += per * n
        except Exception:                            # noqa: BLE001
            # FR-C2.5：坏文件按 1 首计入，不中断整批。
            bad += 1
            n_tracks += 1
            secs += default_secs
    return {"files": float(n_files), "tracks": float(n_tracks),
            "secs": float(secs), "bytes": float(secs * BYTES_PER_SEC),
            "bad": float(bad)}


#: FLAC 相对 WAV 的体积系数（**保守**：实测 0.34~0.36）。
#:
#: 只用于**界面显示**的体积预估，不进磁盘门禁 —— 门禁宁可高估（见
#: ``_estimate_bytes``）。取 0.40 而不是实测的 0.35，是因为压缩率跟内容
#: 强相关：纯静音能压到 1%，而满幅噪声只到 60%。显示「≈1.2GB」而实际
#: 写了 1.4GB，比反过来好。
FLAC_BYTES_RATIO = 0.40

#: MP3 相对 WAV 的体积系数（v5.9.15）。
#:
#: 实测 **0.2268**，而且**与内容无关** —— libsndfile 的 MP3 写入器走
#: LAME 的 **320 kbps CBR**（正弦和噪声都是 320.0 kbps，见
#: ``vendor/_e2e_v5915_mp3.py`` 第 3 节）。所以这里不是「估」而是「算得准」：
#: 0.2268 ≈ 320 / 1411.2，取 **0.24** 留一点容器开销的余量。
#:
#: ⚠ 别照抄「MP3 约 1/9」那种常识 —— 那是 128 kbps 的数字。320 kbps
#:   下 MP3 比 FLAC 还大（FLAC 实测 0.34~0.46），这是取舍不是 bug：
#:   libsndfile 不暴露码率旋钮（``compression_level`` 只在 0.8 附近
#:   微调到 264 kbps，1.0 直接报错），要真降码率得外挂 lame.exe。
MP3_BYTES_RATIO = 0.24

#: OGG 相对 WAV 的体积系数（v6.0.0）。
#:
#: 实测 **0.268**（真实曲库 9 首、跨 7 个机种、32~159 秒，q10 / 约 395 kbps VBR；
#: 单首区间 21%~29%）。取 **0.30** 留余量 —— Vorbis 是 VBR，**体积随内容浮动**
#: （纯正弦只要 6%，白噪能到 40%），所以这个数只能当估算，不能像 MP3 那样当准数。
#: 预估偏大比偏小好：偏小会让用户以为磁盘够用，写到一半满盘。
OGG_BYTES_RATIO = 0.30

#: 格式 → 体积折算系数。``wav`` 不在表里（= 不折）。
#:
#: v5.9.15 前是 ``if fmt == "flac"`` 的三处散装判断；加了 mp3 之后
#: 再散装就一定会漏一处（漏了的表现是「预估 3 GB、实际 300 MB」），
#: 所以统一收成这张表，加新格式只改这里。
FMT_BYTES_RATIO = {"flac": FLAC_BYTES_RATIO, "mp3": MP3_BYTES_RATIO,
                   "ogg": OGG_BYTES_RATIO}


def probe_worker(paths: Sequence[str], chip_secs: float,
                 on_event: Callable[[str, dict], None],
                 token: object = None, fmt: str = "wav") -> None:
    """体量预估的**后台线程函数**（只 :meth:`on_event` 回传，**不碰 Tk**）。

    设计 §5.1 / FR-C2.4：``probe()`` 单文件硬上限 8 KB，但上百文件仍会卡
    界面，所以必须放到工作线程。

    Args:
        paths: 待预估的源文件列表。
        chip_secs: 默认单遍秒数。
        on_event: ``on_event(kind, payload)``；本函数只会发
            ``"cv_estimate"``，payload 含 ``token``（供调用方丢弃过期结果）。
        token: 原样回传给调用方做「代际号」比对。
    """
    try:
        est = estimate_files(paths, chip_secs)
    except Exception as exc:                          # noqa: BLE001
        est = {"files": 0.0, "tracks": 0.0, "secs": 0.0, "bytes": 0.0, "bad": 0.0}
        payload = {"token": token, "error": "%s: %s" % (type(exc).__name__, exc)}
        try:
            on_event("cv_estimate", payload)
        except Exception:                             # noqa: BLE001
            pass
        return
    # v5.9.7：FLAC 的实际落盘只有 WAV 的 ~35%，不折算的话界面上
    # 「预计 3.2 GB」而实际只写 1.1 GB —— 用户会以为程序算错了，
    # 更糟的是可能因此不敢下（以为装不下）。
    # v5.9.15：MP3 同理（约 1/9），系数见 ``FMT_BYTES_RATIO``。
    _ratio = FMT_BYTES_RATIO.get(str(fmt or "").strip().lower())
    if _ratio:
        est["bytes"] = float(est.get("bytes") or 0.0) * _ratio
    est["token"] = token
    est["error"] = ""
    try:
        on_event("cv_estimate", est)
    except Exception:                                 # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# manifest（FR-C8.2 / 设计 §4）
# ---------------------------------------------------------------------------

class ConvertManifest:
    """转 WAV 记账：``<WAV 目录>/.msu1-convert.json``。**永不抛异常**。

    格式（``MANIFEST_VERSION = 1``）::

        {"version": 1,
         "entries": {
           "D:\\\\Music\\\\JoshW\\\\SNES\\\\Some Pack\\\\01 Track.spc": {
             "mtime": 1758000000.0, "size": 66060,
             "n_wav": 3, "policy": "id666",
             "wavs": ["01_Track.wav", "02_Track (2).wav"]}}}

    * **键**：源文件绝对路径（``os.path.normpath`` 后统一 ``.lower()``）。
      Windows 大小写不敏感 → 读侧归一化即可，无需 ``case_sensitive`` 选项；
    * **为什么不用 SHA**：SHA 要读全文件。256 轨 HES 一个包就几百 MB，
      入队判重时逐个读全盘会把消费线程拖死。``(mtime, size)`` 是
      POSIX/Windows 通用的廉价指纹，配合 WAV 存在性双校验，本场景
      （用户自己下载的产物）误判概率可忽略；
    * **只增不减**（FR-C8.4）：源文件被用户删了，其条目保留（几十字节，
      误删风险高得多）。

    ⚠ **并发说明**：当前**单写者**（只有转换线程调 :meth:`record`），主线程
    只在 ``_start_download`` 时 :meth:`load` 读一次 → **无需加锁**。读侧撞上
    写侧由 ``os.replace`` 的原子性兜底（读者要么看到旧完整文件、要么看到新
    完整文件，**不会读到半个 JSON**）。
    若将来出现第二个写者（例如 P1 的「↻ 重试失败的」按钮起新 worker），
    **必须** ① :meth:`record` / :meth:`_save` 加 ``threading.Lock``；
    ② 临时文件名加 ``threading.get_ident()``。
    """

    def __init__(self, path: str) -> None:
        self.path = str(path or "")
        self._data: Dict[str, dict] = {"version": MANIFEST_VERSION, "entries": {}}
        self._errs = 0
        #: 是否已经从磁盘读过一次。:meth:`ensure_loaded` 靠它做幂等。
        #: ⚠ v6.0.6 加这个标志的原因：构造不读盘，而「忘了 load」的表现是
        #:   **所有查询都返回空** —— 静默失效，不报错。``_drop_duplicate_products``
        #:   就踩过：门禁里直接构造 Worker（没走 ``run()``），自愈一声不响
        #:   返回 0，看起来像「本来就没问题」。
        self._loaded = False

    def ensure_loaded(self) -> int:
        """幂等 load：已经读过就返回现有条数，**不重读盘**。

        给「不知道自己是不是第一个用的人」的调用点用（自愈、格式检查）。
        生产路径里 ``_bucket_for`` / ``run()`` 都会显式 :meth:`load`，
        这里只是兜底，不会多读一次。
        """
        if self._loaded:
            return len(self._data.get("entries") or {})
        return self.load()

    # -- 读 ---------------------------------------------------------------
    def load(self) -> int:
        """读 manifest，返回 entries 数。

        读不到 / 解析失败 / 版本不符 / 字段缺失 → 按「**全部未转过**」处理
        并整体重建，**绝不抛异常中断下载**（FR-C8.2）。
        """
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError, TypeError):
            return 0
        if not isinstance(raw, dict):
            return 0
        try:
            version = int(raw.get("version", 0))
        except (TypeError, ValueError):
            return 0
        if version != MANIFEST_VERSION:
            return 0                       # 结构变更 -> 整体重建
        ents = raw.get("entries")
        if not isinstance(ents, dict):
            return 0
        clean: Dict[str, dict] = {}
        for key, rec in ents.items():
            if isinstance(key, str) and isinstance(rec, dict):
                clean[key] = rec
        self._data = {"version": MANIFEST_VERSION, "entries": clean}
        self._loaded = True
        return len(clean)

    def already_done(self, src: str, fmt: str = "") -> bool:
        """True 表示这个源文件已转过、可以跳过。**三校验缺一不可**。

        校验①：源文件 ``(mtime, size)`` 指纹一致 —— 源被重下/替换后两者都变，
        必须重转。
        ⚠ **这是「不能用『WAV 目录非空』当已转过」的根本原因**：目录非空时
        里面的 WAV 可能是**上一次会话的旧产物**，拿它冒充新源文件的转换结果
        是本功能最危险的静默错误（R6）。

        校验②：记账里的 WAV **至少一个仍在磁盘** —— 用户可能手删了。
        实现按**全量探**（``n_wav`` 通常 1~256，N 次 ``isfile`` 量级可忽略），
        这样「用户删了中间几首」也不会触发整包重转。与校验①的分工是：
        ①管「是不是同一个源」，②管「产物还在不在」。

        Args:
            src: 源文件绝对路径。
            fmt: 这次要转的格式（``"wav"`` / ``"flac"`` / ``"mp3"``）。
                给了才会查「产物是不是坏的旧版本写的」——见校验③。

        Returns:
            True = 可跳过；False = 必须重转（含源文件已不存在的情况）。
        """
        try:
            st = os.stat(src)
        except (OSError, TypeError, ValueError):
            return False                   # 源都没了，无从谈起
        rec = self._data["entries"].get(os.path.normpath(str(src)).lower())
        if not isinstance(rec, dict):
            return False
        # ③ 版本：v5.9.18 之前的 MP3 **内容**是坏的（int16 直传 → 满耳杂音）。
        #    那种文件结构指标全对，只有解码回来和源比才知道坏了，所以只能
        #    靠记账里的生产版本判。旧记账没有 ``by`` 字段 → 视为很旧 → 重转。
        #    这是**唯一**能让用户「升级后点一次转 MP3 就自动修好」的机制。
        need = BAD_BEFORE.get(str(fmt or "").strip().lower())
        if need and _ver_lt(rec.get("by") or "", need):
            return False
        try:
            if int(rec.get("size", -1)) != int(st.st_size):
                return False
        except (TypeError, ValueError):
            return False
        try:
            if abs(float(rec.get("mtime", -1.0)) - float(st.st_mtime)) > 0.001:
                return False
        except (TypeError, ValueError):
            return False
        wavs = rec.get("wavs") or []
        if not isinstance(wavs, (list, tuple)) or not wavs:
            return False
        # ④ v6.0.2：产物扩展名与目标格式**对不上** → 必须重转。
        #
        # 起因：流式音频（.brstm / .adx / .xvag / .laac…）与 CD 音轨走的是
        # 各模块自己的 ``convert_one``，它们只写 WAV —— 于是 ``FLAC/<游戏>/``
        # 里躺着 ``01_xxx.wav``，记账里记的也是 ``.wav`` 的名字。
        # 这种记录「看起来转过」（指纹对得上、文件也都在），光靠 ①②③ 判不出来。
        # 不加这一条，用户升级后**再点一次「转 FLAC」只会得到「已转过，跳过」**，
        # 目录里继续躺着 WAV，而且他没有任何办法自救。
        #
        # 只认「扩展名」这一个事实，不按版本号一刀切 —— 否则全世界的 FLAC
        # 库（芯片音源转的，一直是好的）都要被重编一遍。
        want = str(fmt or "").strip().lower()
        if want in spc_wav.ENC_FMTS and not any(
                str(n).lower().endswith("." + want) for n in wavs):
            return False
        base = os.path.dirname(self.path) or os.curdir
        for name in wavs:
            try:
                if os.path.isfile(os.path.join(base, str(name))):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def entry(self, src: str) -> Optional[dict]:
        """取某个源文件的记账条目（不存在返回 ``None``）。只读，不抛。"""
        try:
            return self._data["entries"].get(os.path.normpath(str(src)).lower())
        except (TypeError, ValueError):
            return None

    def __len__(self) -> int:
        return len(self._data["entries"])

    def all_records(self) -> List[Tuple[str, dict]]:
        """所有记账条目 ``[(归一化键, 记录), ...]``。只读快照，不抛。"""
        out: List[Tuple[str, dict]] = []
        for k, v in list((self._data.get("entries") or {}).items()):
            if isinstance(v, dict):
                out.append((str(k), v))
        return out

    def derived_src_keys(self) -> List[str]:
        """记账里「源文件本身就是别的源转出来的 WAV」那些键（v6.0.6）。

        这些是**脏记录**：v6.0.6 及以前枚举音源时没区分「原始音源」和
        「我们自己转出来的 ``xxx.fsb.wav``」，于是同一首曲子被转了两遍
        （``01_xxx.flac`` + ``02_xxx.fsb.flac``，内容完全一样）。

        判据见 :func:`is_derived_wav`。返回的键可直接喂给 :meth:`forget`。

        ⚠ 只在 Windows 上 `isfile` 对大小写不敏感才成立 —— 键是 ``lower()``
        过的，Linux 上会判不出来。本功能只在 Windows 发布，够用。
        """
        out: List[str] = []
        for key, _rec in self.all_records():
            try:
                if is_derived_wav(key):
                    out.append(key)
            except Exception:                # noqa: BLE001
                continue
        return out

    def stale_records(self, fmt: str) -> List[Tuple[str, List[str]]]:
        """列出「记账说转过、产物扩展名却不是 ``fmt``」的记录（v6.0.2）。

        只挑**明显是坏记录**的那种：名单里一个目标扩展名的文件都没有，
        但至少有一个 ``.wav``。这正是 v6.0.1 及更早把流式音频转 FLAC/MP3 时
        留下的形状（目录是 FLAC，产物是 ``01_xxx.wav``）。

        Returns:
            ``[(源文件键, 产物名列表), ...]``。键是 ``already_done`` 用的
            归一化键（``normpath`` + ``lower``），可直接喂给 :meth:`forget`。
        """
        want = str(fmt or "").strip().lower()
        if want not in spc_wav.ENC_FMTS:
            return []
        out: List[Tuple[str, List[str]]] = []
        for key, rec in list(self._data["entries"].items()):
            names = [str(n) for n in ((rec or {}).get("wavs") or ()) if str(n)]
            if not names:
                continue
            if any(n.lower().endswith("." + want) for n in names):
                continue                     # 已有正确格式的产物
            if not any(n.lower().endswith(".wav") for n in names):
                continue                     # 不是「只写了 WAV」那种，别碰
            out.append((key, names))
        return out

    def forget(self, src: str) -> bool:
        """删掉某个源文件的记账条目（**不删产物**）。不存在返回 False。
        唯一用途是 v6.0.2 的「格式自愈」：旧版本把流式音频转 FLAC/MP3 时只写了
        WAV，产物删掉之后，那条记账也必须一起清掉 —— 留着它只会让
        ``already_done()`` 继续拿一批已经不存在的文件名去校验。
        """
        try:
            key = os.path.normpath(str(src)).lower()
        except (TypeError, ValueError):
            return False
        if key not in self._data["entries"]:
            return False
        self._data["entries"].pop(key, None)
        self._save()
        return True

    # -- 写 ---------------------------------------------------------------
    def record(self, src: str, wavs: Sequence[str], policy: str = "") -> bool:
        """登记一次成功转换，并**立即原子落盘**。

        为什么每文件立即写而不是批次末统一写：中途崩溃 / 用户取消 / 关机 →
        批次末写等于**全部记账丢失**，下次全部重转，6.3 GB 白跑一遍。

        Args:
            src: 源文件绝对路径。
            wavs: 产出的 WAV **文件名**（只存 basename，目录恒为 ``WAV/``）。
            policy: 该文件走的 policy（``"id666"`` / ``"chip"``），仅记录用。

        Returns:
            True = 内存记账成功（落盘可能失败，见 :attr:`errors`）。
        """
        try:
            st = os.stat(src)
        except (OSError, TypeError, ValueError):
            return False                   # 源都没了，记了也没意义
        names = [str(n) for n in (wavs or ()) if str(n)]
        try:
            key = os.path.normpath(str(src)).lower()
        except (TypeError, ValueError):
            return False
        self._data["entries"][key] = {
            "mtime": round(float(st.st_mtime), 3),
            "size": int(st.st_size),
            "n_wav": len(names),
            "policy": str(policy or ""),
            "wavs": names,
            # v5.9.18：记下是哪个版本写的。MP3 在 5.9.18 之前写出来的是
            # 坏内容（见 BAD_BEFORE），没有这个字段就没法识别、没法重转。
            "by": AUDIO_WRITER_VERSION,
        }
        self._loaded = True                 # 内存里已经有内容了，别再重读盘
        self._save()        # 每文件立即落盘，不攒批次
        return True

    def _save(self) -> bool:
        """原子写：同目录临时文件 + ``fsync`` + :func:`os.replace`。

        * :func:`os.replace` 保证读者（主线程）永远看到完整的旧版或新版，
          **不会读到半个 JSON**；
        * ``fsync`` 保证断电后内容真的落盘 —— 不 fsync 时可能出现
          「manifest 里有记账但内容是空的」→ 下次读到就当已转过 →
          **静默丢产物**（R6 同族）。

        ``OSError`` 一律吞掉并计数：**绝不因记账失败中断转换**。下次会因
        指纹不匹配而重转 —— 重转是安全的，记账崩溃才危险。

        Returns:
            True = 落盘成功。
        """
        if not self.path:
            return False
        tmp = "%s.%d.tmp" % (self.path, os.getpid())   # 同目录、同盘才原子
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            return True
        except (OSError, TypeError, ValueError):
            self._errs += 1
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    @property
    def errors(self) -> int:
        """落盘失败计数（只用于日志）。"""
        return self._errs


# ---------------------------------------------------------------------------
# 取消代理：把「全局取消」与「本文件超时」合成一个对象
# ---------------------------------------------------------------------------

class _CancelPair:
    """``is_set()`` = 任一 Event 置位。仅供 :func:`spc_wav.convert` 使用。

    为什么需要它：设计要求**每文件独立 cancel Event**（超时只影响单个文件），
    而「■ 停止」/关窗口又要能中止当前文件。``spc_wav.convert(cancel=...)``
    只要求对象有 ``.is_set()``（PRD B16），所以把两个 Event 或起来即可，
    **不必**放弃 per-file 粒度。
    """

    __slots__ = ("_a", "_b")

    def __init__(self, a: threading.Event, b: threading.Event) -> None:
        self._a = a
        self._b = b

    def is_set(self) -> bool:
        try:
            return bool(self._a.is_set() or self._b.is_set())
        except Exception:                                # noqa: BLE001
            return True                                 # 读不到就当已取消（保守）


# ---------------------------------------------------------------------------
# 单线程消费队列（T2）
# ---------------------------------------------------------------------------

class _OutBucket:
    """**一个输出目录**的记账：编号起点 / 重名集合 / manifest。

    为什么需要它：v5.2-web-game 起「每个游戏一个子文件夹」——
    ``WAV/`` 底下变成 ``WAV/<游戏名>/``。编号与重名避让必须**按目录各算各的**：
    每个游戏都从 ``01_`` 起，否则第 2 个游戏的第一首会变成 ``24_xxx.wav``。
    清单同理（``WAV/<游戏名>/.wav_manifest.json``），否则 A 游戏转过的记录
    会让 B 游戏里同名的 ``01.psf`` 被误判成「已转过」直接跳过。
    """

    __slots__ = ("dir", "used", "start_idx", "manifest", "numbered")

    def __init__(self, path: str) -> None:
        self.dir = str(path or "")
        self.used: Set[str] = set()
        self.start_idx = 0
        self.manifest = ConvertManifest(os.path.join(self.dir, MANIFEST_NAME))
        self.numbered = False


class ConvertWorker(threading.Thread):
    """单线程串行转换消费者。**永不碰 Tk**，所有 UI 更新经 ``on_event``。

    设计要点（逐条对应设计文档）：

    ============================ 职责边界 ============================
    * **不碰 Tk**；``on_log`` / ``on_progress`` / ``on_stage`` 三个解码回调
      也只发事件（``cv_log`` / ``cv_progress`` / ``cv_stage``），
      由 GUI 侧自行节流；
    * **不改** ``DownloadDialog._dl_*`` 任何下载统计；
    * **不调** ``_finish_download``（由 GUI 收到 ``cv_thread_exit`` 后自己调）；
    * 并发**恒为 1**，绝不进 ``joshw_dl`` 的下载线程池（转换抢 CPU 会让
      下载吞吐腰斩，R1）。

    ============================ 事件 kind ============================
    ``cv_skip`` / ``cv_file_done`` / ``cv_file_failed`` / ``cv_paused`` /
    ``cv_timeout`` / ``cv_stuck`` / ``cv_error`` / ``cv_abandoned`` /
    ``cv_log`` / ``cv_progress`` / ``cv_stage`` / ``cv_thread_exit``
    """

    #: 队列 ``get`` 的轮询间隔（秒）。0.2s 让「收尾判定」最多延迟 0.2s。
    POLL_S = 0.2
    #: 单文件超时（秒）。主线程巡检 :meth:`current` 超时后置**本文件** cancel。
    FILE_TIMEOUT_S = 300.0
    #: 「疑似卡死」提示阈值（秒）。底层 ``chip_decode`` 子进程超时上限是 1800s，
    #: 超过这个点基本就是子进程 hang，用户需要知道可以点停止（R4）。
    STUCK_GRACE_S = 1800.0
    #: 芯片格式「默认单遍」秒数（决策 D3）。
    CHIP_SECS = 150.0
    #: 磁盘硬底余量（字节）。低于此值一律暂停消费。
    DISK_RESERVE = 256 * 1024 * 1024
    #: 「磁盘持续不足」的判定轮数（每轮 :attr:`PAUSE_POLL_S` 秒）。
    #: 达到它且源已结束 -> 发 ``cv_abandoned`` 放弃剩余并正常收尾，
    #: 否则界面会永远停在「磁盘空间不足」（设计 §2.5 收敛保证）。
    DISK_STALL_ROUNDS = 3
    #: 磁盘门禁下的轮询间隔（秒）。同时是取消时的唤醒上限。
    PAUSE_POLL_S = 0.5
    #: ``cv_paused`` 的最小间隔（秒），防日志刷屏。
    PAUSE_LOG_S = 2.0

    def __init__(self, wav_dir: str,
                 on_event: Callable[[str, dict], None],
                 cancel: threading.Event,
                 merge_wav: bool = False,
                 group_by_game: bool = False,
                 fmt: str = "wav") -> None:
        super().__init__(name="dl-convert-0", daemon=True)   # daemon 必须
        self._wav_dir = str(wav_dir or "")
        #: v5.9.7：输出容器，``"wav"`` / ``"flac"`` / ``"mp3"``（v5.9.15）
        #: / ``"ogg"``（v6.0.0）。
        #: **只换容器，不换音频**（都是 16bit/44100/立体声；FLAC 逐样本无损，
        #: MP3 / OGG 有损 —— 这是「换容器会动音频」的例外）。
        #: 全类只有**两处**真正用到它：
        #: ① 传给 ``spc_wav.write_tracks``（芯片 / PSF / 外挂解码器 / txtp
        #:    这几条「先拿到 PCM」的路径，直接写目标格式）；
        #: ② ``_reencode_to_fmt``（stream / cd / xa / pivotal 那几条只写 WAV
        #:    的路径，转完再补编码）。
        #: ⚠ v6.0.2 之前这里写的是「只有一处」—— 正是这个错误前提让 ② 一直
        #:   缺失：Wii 的 .brstm 点「转 FLAC」出来的是 .wav。别在分支里各写
        #:   一份格式判断，两处已经够多了。
        self._fmt = str(fmt or "").strip().lower()
        # v6.0.0：白名单跟 spc_wav.ENC_FMTS 走（flac / mp3 / ogg），
        # 不再手写元组 —— 加格式时这里漏一处，表现是「传了 ogg 却出 WAV」，
        # 静默降级，比报错难查得多。
        if self._fmt not in spc_wav.ENC_FMTS:
            self._fmt = "wav"
        #: v5.2-web-game：按游戏名再分一层子文件夹。
        #: 下载流程的目录结构是 ``<下载根>/<包名>/...``，而 ``wav_dir`` 是
        #: ``<下载根>/WAV`` —— 所以「游戏名」= 源文件相对 ``<下载根>`` 的
        #: 第一层目录名（也就是用户在列表里看到的那个包名）。
        #:
        #: ⚠ **默认关**。这个规则只能从路径推断，而「某个目录的子目录里放着
        #: 音源」并不是下载流程独有的形态（用户手动整理、测试夹具都长这样）。
        #: 无差别开启会把 ``D:\\music\\spc\\xx.spc`` 的产物塞进
        #: ``WAV/spc/`` —— 对没走下载流程的人来说是莫名其妙的目录结构。
        #: 所以只有**确认自己就是下载流程**的调用方（``web_ui.dl_api``）
        #: 才显式传 True。
        self._group_by_game = bool(group_by_game)
        self._emit_cb = on_event
        #: 与 ``joshw_dl.Downloader._cancel_event`` **同一个 Event 对象**
        #: （PRD FR-C9.1 / B16：「■ 停止」无需改动即三段联动）。
        self._cancel = cancel if cancel is not None else threading.Event()
        self._gate = threading.Event()           # 磁盘门禁；set = 可消费
        self._gate.set()
        self._q: "queue.Queue[dict]" = queue.Queue()   # 无界，见设计 §2.4
        self._src_finished = False
        #: 当前正在写的输出目录的记账（每个游戏一个桶）
        self._bucket = _OutBucket(self._wav_dir)
        self._buckets: Dict[str, _OutBucket] = {
            os.path.abspath(self._wav_dir): self._bucket}
        #: v4.24「多轨合并成单文件」：一个源产出 ≥2 个 WAV 时，再拼一个整轨
        #: 文件（分轨**全部保留**）。默认关 —— 老行为零变化。
        #:
        #: ⚠ v5.9.7：**FLAC 模式下强制关掉**。``wavmerge`` 读盘用的是标准库
        #: ``wave``，喂 .flac 进去会直接抛「不是 WAV」。与其让每首合并都失败
        #: 刷一屏红字，不如在这里就关掉 —— 前端也同步禁用了那个复选框。
        self.merge_wav = (bool(merge_wav) and wavmerge is not None
                          and self._fmt == "wav")
        self._merged = 0                         # 本次合并出的文件数
        self._merge_fail = 0
        self._cur: Optional[dict] = None         # {'job','t0','cancel','timed_out'}
        self._disk_stall = 0
        self._last_pause = 0.0
        # 统计（cv_thread_exit 的 payload 来源；GUI 侧只做展示聚合）
        self._ok = 0
        self._fail = 0
        self._skip = 0
        self._wavs = 0
        self._bytes = 0
        self._secs = 0.0
        self._t0 = time.time()
        #: 测试钩子：``self.convert_fn`` 存在时用它替代 ``spc_wav.convert``。
        #: 生产路径恒为 None（设计 §7.2 的假 convert 注入点）。
        self.convert_fn: Optional[Callable[..., dict]] = None
        #: 测试钩子：``self.disk_free_fn`` 存在时用它替代 ``shutil.disk_usage``。
        self.disk_free_fn: Optional[Callable[[], float]] = None

    # ---- 主线程可调的三个方法，全部无锁（Queue / Event 本身线程安全）----
    def enqueue(self, job: dict) -> None:
        """入队一个转换任务（主线程唯一入口，**只 put，不判定不解码**）。

        元素结构见设计 §2.1。``url`` 必填：``convert`` 的回调只带文件路径、
        **不带 URL**，靠它才能把状态写到正确的行。
        """
        self._q.put(dict(job or {}))

    def mark_src_finished(self) -> None:
        """收到 ``all_done`` 时调。只置标志，**不阻塞**。"""
        self._src_finished = True

    def cancel(self) -> None:
        """请求停止。**只置 Event + 唤醒门禁 + 丢弃未开始项，绝不 join**。

        为什么要丢弃未开始项（FR-C9.5）：否则取消后还要把上百个文件
        逐个跑一遍，用户点了停止却等半天。而且逐个回写「已取消」会产生
        几千行无意义刷新（FR-C8.4 同理）。
        """
        self._cancel.set()
        self._gate.set()            # 唤醒可能卡在磁盘门禁的 self
        cur = self._cur
        if cur is not None:
            try:
                cur["cancel"].set()             # 让当前文件在下一个边界收手
            except Exception:                    # noqa: BLE001
                pass
        self._drain()

    def wait(self, timeout: float = 5.0) -> bool:
        """等待线程退出。**仅供脚本 / 测试用，GUI 不调**（会卡界面）。"""
        try:
            self.join(timeout)
        except RuntimeError:                      # 还没 start() 就 join
            return True
        return not self.is_alive()

    def current(self) -> Optional[dict]:
        """当前正在转换的文件，供主线程超时巡检。

        Returns:
            ``{"job":…, "t0": 时间戳, "cancel": 本文件 Event,
            "timed_out": bool}`` 或 ``None``（空闲 / 已退出）。

        ⚠ 调用方**主线程**据 ``t0`` 判断超时，超时后 ``["cancel"].set()``。
        这里**只读不写**，避免跨线程竞争。
        """
        return self._cur

    def stats(self) -> dict:
        """当前统计快照。"""
        return {"ok": self._ok, "fail": self._fail, "skip": self._skip,
                "wavs": self._wavs, "bytes": self._bytes,
                "merged": self._merged, "merge_fail": self._merge_fail,
                "secs": round(time.time() - self._t0, 1),
                "left": self._q.qsize()}

    # ---- 内部：事件 --------------------------------------------------------
    def _emit(self, kind: str, payload: Optional[dict] = None) -> None:
        """发事件。**回调异常一律吞掉**（与 joshw_dl._emit 同款）。

        GUI 侧实现必须只 ``q.put`` —— 这是线程铁律的落点。
        """
        callback = self._emit_cb
        if callback is None:
            return
        try:
            callback(kind, dict(payload or {}))
        except Exception:                            # noqa: BLE001
            pass

    # ---- 内部：解码回调（**只发事件，一个 Tk 方法都不碰**）--------------
    def _on_log(self, text=None, *args):
        """解码明细。**默认不进下载窗口日志区**（会淹没下载日志，FR-C10.4）。

        单文件失败时由 :meth:`_convert_one` 把缓冲的明细随 ``cv_file_failed``
        一起回传（设计 §10 A2）；成功时丢弃。

        ⚠ 用 ``*args`` 收下所有形态：``spc_wav`` 给的是单参数文本，
        下游可能给 ``(level, text)`` 之类 -> 拼成一行字符串即可。
        """
        parts = [str(a) for a in ((text,) + args) if a is not None]
        self._emit("cv_log", {"text": " ".join(parts)})

    def _on_progress(self, *args):
        """音轨级进度。只发事件。

        ⚠ **必须用 ``*args`` 而不是固定 4 个位置参数**。两个调用方的签名
        **不一样**，实测踩过：

        * ``chip_decode.load_chip_paths`` → ``prog(len(tracks), ri, secs)``
          —— **3 个**参数（累计音轨数, run 序号, 本 run 秒数），语义是
          「按 run 报进度」；
        * ``spc_wav.write_tracks`` → ``prog(idx, total, name, secs)``
          —— **4 个**（序号, 总数, 曲名, 本轨秒数）。

        写成固定 4 参会让芯片路径**每个文件都炸**：
        ``TypeError: _on_progress() missing 1 required positional argument``
        被 :func:`chip_decode.load_chip_paths` 逐文件兜住 ->
        ``stat["fail"] += 1`` -> **静默零产出**（实测：NSF 全部转不出 WAV，
        表现为「转换完成但没有产出 WAV」）。
        """
        payload = {"n": len(args)}
        if len(args) >= 1:
            payload["idx"] = args[0]
        if len(args) >= 2:
            payload["total"] = args[1]
        if len(args) >= 3:
            payload["name"] = args[2]
        if len(args) >= 4:
            payload["secs"] = args[3]
        self._emit("cv_progress", payload)

    def _on_stage(self, *args):
        """批次开始。只发事件。

        ⚠ 同 :meth:`_on_progress`：``spc_wav.convert`` 调 ``stage(bi, nb, n)``
        是 3 参，但下游实现可能给别的个数 -> 一律 ``*args`` 收下。
        """
        payload = {"n": len(args)}
        for i, v in enumerate(args[:3]):
            payload["bi" if i == 0 else ("bt" if i == 1 else "bs")] = v
        self._emit("cv_stage", payload)

    # ---- 内部：编号 / 磁盘 --------------------------------------------------
    def _ensure_numbering(self) -> None:
        """**首次消费前**扫描 ``WAV/``，续编编号并注入已占用名（R7）。

        不做这一步的实测后果：预置 25 字节的 ``01_Stage Clear.wav``，
        第二次会话转一次后变成 705644 字节的 ``RIFF`` 头 —— 用户旧数据
        被静默覆盖，无任何提示。
        """
        if self._bucket.numbered:
            return
        self._scan_bucket(self._bucket)

    # ---- 内部：输出目录（按游戏名分文件夹）-------------------------------
    @staticmethod
    def _folder_name(name: str) -> str:
        """包名 -> 能当文件夹名的字符串。

        只做**最小**清洗：Windows 非法字符换成 ``_``，去掉首尾空格与点，
        再截到 100 字符。**不转拼音、不删括号** —— 用户要的就是他看到的那个
        名字（``Final Fantasy VII (Japan)``），改了他反而找不到。
        """
        s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(name or ""))
        s = s.strip().strip(".").strip()
        if len(s) > 100:
            s = s[:100].rstrip()
        return s or "未命名"

    def _bucket_for(self, src: str) -> "_OutBucket":
        """这个源该写进哪个目录。

        下载流程的目录结构是 ``<下载根>/<包名>/…``，``wav_dir`` 是
        ``<下载根>/WAV``。所以「游戏名」= ``src`` 相对 ``<下载根>`` 的
        **第一层目录名** —— 就是用户在下载列表里看到的那个包名。

        三种情况都回落到扁平的 ``WAV/``：

        * ``group_by_game=False``（老行为）
        * 文件直接躺在 ``<下载根>`` 里（相对路径只有一层，没有游戏名可用）
        * 相对路径算不出来（跨盘等）

        ⚠ 每个桶的编号/重名/清单**各算各的**，见 :class:`_OutBucket`。
        """
        if not self._group_by_game:
            return self._default_bucket()
        root = os.path.abspath(self._wav_dir)
        base = os.path.dirname(root)
        try:
            rel = os.path.relpath(os.path.abspath(str(src)), base)
        except (OSError, ValueError):
            return self._default_bucket()
        parts = [p for p in rel.replace("\\", "/").split("/")
                 if p not in ("", ".", "..")]
        # 只有一层（文件直接在下载根下）→ 没有游戏名
        if len(parts) < 2:
            return self._default_bucket()
        folder = self._folder_name(parts[0])
        out = os.path.join(root, folder)
        key = os.path.abspath(out)
        b = self._buckets.get(key)
        if b is not None:
            return b
        try:
            os.makedirs(out, exist_ok=True)
        except OSError as exc:
            # 建不了目录不能把这首曲子吞了 —— 退回扁平目录，至少能转出来
            self._emit("cv_log", {"text": "⚠ 建不了子目录 %s：%s（改写到 WAV/）"
                                          % (folder, exc)})
            return self._default_bucket()
        b = _OutBucket(out)
        try:
            b.manifest.load()
        except Exception:                        # noqa: BLE001 清单坏了不致命
            pass
        self._buckets[key] = b
        # 续编：这个文件夹里可能已经有上次转的 01_xxx.wav
        self._scan_bucket(b)
        self._emit("cv_log", {"text": "📁 %s → %s/" % (folder, folder)})
        return b

    def _default_bucket(self) -> "_OutBucket":
        return self._buckets[os.path.abspath(self._wav_dir)]

    def _scan_bucket(self, b: "_OutBucket") -> None:
        """扫某个目录，续编编号并注入已占用名（等价于 ``_ensure_numbering``）。"""
        if b.numbered:
            return
        b.numbered = True
        try:
            max_idx, used = wav_scan_numbering(b.dir)
        except OSError:
            return
        b.start_idx = max_idx
        b.used = used

    def _disk_ok(self, need_bytes: int) -> bool:
        """剩余空间够不够写这个文件。

        ``shutil.disk_usage`` 失败 → **放行**：探测失败不该卡住用户（设计 §2.5）。

        ⚠ **判断式 ``free >= max(DISK_RESERVE, need)``，两个细节都是刻意为之**：

        1. **``>=`` 而非 ``>``** —— 空间**恰好等于**所需时必须放行。原写法 ``>``
           会把 ``free == need`` 拦下，而这不是任何真实风险。
        2. **不叠 1.3 倍余量** —— 那个系数原本是「防低估」，但实测证明``need``
           的误差方向**确定为总是高估**（见下），它在防一件不会发生的事。
           去掉它换来的是实打实的可用性：
           转 JC63011（真实产出 0.44 GiB）原先需 8.20 GiB 空闲（**18.5 倍**），
           现在只需 6.31 GiB —— 8 GB 空闲的机器从「转不了」变成「能转」。

        **「need ≥ 真实产出」的三层依据**（第3 层最强，且可被代码复核）：

        | 层 | 依据 | 强度 |
        |---|---|---|
        | 1 | 7 个真实 HES 样本产出 18~183 轨 | 样本 |
        | 2 | 误差方向确定为**总是高估** | 样本 + 方向 |
        | 3 | **``gme_track_count`` 对 HES 恒返 256（实测 7/7）→ 解码产出 ≤ 256** | **外部库行为** |

        第 3 层才是根本保证：真正决定解码多少轨的是
        ``chip_decode`` 里的 ``ntrack = gme.gme_track_count(emu)``，
        **不是** ``_probe_hes`` 写死的 ``n_tracks=256``（后者只用于估算与切片）。
        ⚠️ **「256 ≥ 真实产出」是推断，不是证明。**
        依据是「**至今没见过超过 256 的**」（7 样本产出 18~183），
        而**不是**「不可能超过」。若遇到产出 > 256 轨的 HES，需重新评估本判断式。
        ⚠️ 另有一个**与门禁无关的独立风险**（建议 PM 记 P2）：
        ``_probe_hes`` 写死 ``n_tracks=256``；若真有 >256 轨的 HES，
        ``chip_decode._plan_runs`` 会按它切片 -> **静默丢掉超出部分**。
        目前 7 样本无一触发（最大 183），但这是**截断风险**，与门禁是两回事。

        ⚠️ **256 MB 地板（``DISK_RESERVE``）禁止当「多余保险」删掉** ——
        它不是系数，是文件系统与写缓冲的**真实需求**，删掉会写出坏文件。
        """
        free = None
        if self.disk_free_fn is not None:
            try:
                free = float(self.disk_free_fn())
            except Exception:                        # noqa: BLE001
                free = None
        if free is None:
            try:
                free = float(shutil.disk_usage(self._wav_dir or ".").free)
            except OSError:
                return True
            except Exception:                        # noqa: BLE001
                return True
        try:
            need = int(need_bytes)
        except (TypeError, ValueError):
            need = 0
        # v4.3：去掉 1.3 倍冗余余量（实测 need 误差方向为「总是高估」，
        # 该系数在防不会发生的事），并把 `>` 改成 `>=`（恰好够也放行）。
        # ⚠⚠ 这里曾并存过一段「④a 只改运算符、**不动 1.3**，1.3 在此无害」的
        #   注释 —— 它已被真解码实测推翻（need 高估 1.4~14.2 倍），留着会与
        #   上一段 docstring 直接矛盾。**改结论必须连带清掉旧结论的副本**，
        #   否则代码里会同时存在「1.3 无害」与「已去掉 1.3」两句话。
        # 三层依据与边界条件见上面 _disk_ok 的 docstring。
        return free >= max(float(self.DISK_RESERVE), need)

    def _estimate_bytes(self, src: str) -> int:
        """预估这个源文件会写多少字节（**只用于磁盘门禁，宁可高估**）。

        用 :func:`estimate_files` 的同一口径（probe × 时长 × 176400）。
        门禁只关心「够不够」，高估几百 MB 无所谓；低估才会写满盘。
        probe 失败时按 1 首 × 150s 估（约 26 MB）。

        ⚠ v5.9.7：FLAC 模式下**必须折算**。不折的话，一个实际只写 1.1 GB
        的包会被估成 3.2 GB —— 磁盘剩 2 GB 的用户会被门禁判「空间不足」，
        连续 3 轮后直接 ``cv_abandoned`` **放弃剩余曲目**，而盘明明够。
        系数仍取 0.40（高于实测 0.35），保留 14% 余量；再加上门禁本身
        有 256 MB 地板（``DISK_RESERVE``），低估的空间已被兜住。
        v5.9.15：MP3 走同一张表（``FMT_BYTES_RATIO``），约 0.14。
        """
        ratio = FMT_BYTES_RATIO.get(self._fmt, 1.0)
        try:
            est = estimate_files([src], self.CHIP_SECS)
            n = float(est.get("bytes") or 0.0)
            return int(n * ratio)
        except Exception:                            # noqa: BLE001
            base = self.CHIP_SECS * BYTES_PER_SEC
            return int(base * ratio)

    def _convert_txtp(self, src: str, log_buf: list, job_cancel) -> dict:
        """PS1 ``.txtp`` 轨道索引 -> WAV（v4.41）。

        返回结构与 :func:`spc_wav.convert` **同形**，记账段同样零改动。

        ⚠ 这是**全原生**路径：``.txtp`` 里的目标要么是 ``.xa``（标准 CD-XA
        扇区，复用 ``msu1_to_cd`` 那套已验证解码器），要么是 ``.DA``（裸
        PCM16LE，读出来就是成品）。**不需要 foobar2000、不需要模拟器** ——
        这一点与 ``.minipsf`` 完全不同（那种包是内存快照，非模拟器不可）。

        Args:
            src: ``.txtp`` 文件路径（**不是**包目录）。
            log_buf: 日志收集列表（工作线程只往这里 append）。
            job_cancel: 本文件的超时/取消 Event。

        Returns:
            ``{ok, fail, files, seconds, errors, last_idx}``。
        """
        if psx_txtp is None:
            return {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
                    "errors": ["PS1 .txtp 模块不可用（psx_txtp 导入失败）"],
                    "last_idx": self._bucket.start_idx}
        return psx_txtp.convert(
            [src], self._bucket.dir,
            used=self._bucket.used, start_idx=self._bucket.start_idx, max_secs=0.0,
            cancel=_CancelPair(self._cancel, job_cancel),
            on_log=log_buf.append, on_progress=self._on_progress)

    def _convert_psf(self, src: str, log_buf: list, job_cancel) -> dict:
        """PSF 系（``.psf`` / ``.minipsf``）-> WAV（v5.1-web-psf）。

        返回结构与 :func:`spc_wav.convert` **同形**，记账段零改动。

        ⚠ 这条路径**不是纯 Python**：真正干活的是同级的 ``psf2wav.exe``
        （psflib + Highly Experimental），而且**绕不过一份 BIOS**。
        没有 BIOS / 没有解码器时 `psf_native.decode_pcm` 会返回一条人话错误，
        这里原样交给 `fail` —— **绝不产出静音 WAV 冒充成功**。

        Args:
            src: ``.psf`` 文件路径。
            log_buf: 日志收集列表（工作线程只往这里 append）。
            job_cancel: 本文件的超时/取消 Event。

        Returns:
            ``{ok, fail, files, seconds, errors, last_idx}``。
        """
        if psf_native is None:
            return {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
                    "errors": ["PSF 模块不可用（psf_native 导入失败）"],
                    "last_idx": self._bucket.start_idx}
        cancel = _CancelPair(self._cancel, job_cancel)
        r = psf_native.decode_pcm(src, cancel=cancel,
                                  on_progress=self._on_progress)
        if not r["ok"]:
            return {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
                    "errors": [r["error"]], "last_idx": self._bucket.start_idx}
        log_buf.append("   ℹ .psf → %s（%.2f 秒）" % (r["kind"], r["seconds"]))
        # 写盘交给 spc_wav.write_tracks —— 命名/去重/编号/削波提示全复用，
        # 与 .txtp / CD 音轨走同一套（别在这里另写一份）。
        return spc_wav.write_tracks(
            [{"name": r["title"], "pcm": r["pcm"], "little_endian": True}],
            self._bucket.dir, max_secs=0.0, on_log=log_buf.append,
            cancel=cancel, used=self._bucket.used,
            start_idx=self._bucket.start_idx, fmt=self._fmt)

    def _convert_emu(self, src: str, log_buf: list, job_cancel,
                     mod, label: str) -> dict:
        """外挂解码器家族（USF / 2SF / SSF / DSF / GSF / QSF）-> WAV（v5.4）。

        这六类（连同 PSF）都是「快照/ROM + 各自机种的 CPU 模拟」，libgme
        里根本没有实现 —— 真正干活的是同级的外挂 exe：
        ``usf2wav.exe`` / ``vio2wav.exe`` / ``ssf2wav.exe`` / ``gsf2wav.exe``
        / ``qsf2wav.exe``。

        ⚠ **v5.4 之前这条不存在，是个真 bug**：``policy_for()`` 早就返回
        ``"usf"`` / ``"vio"`` / ``"ssf"`` / ``"gsf"``，但 :class:`ConvertWorker`
        的分派只有 cd/txtp/xa/psf/stream/id666 —— 四个 policy 全落到 ``else``
        分支，被当成 libgme 的芯片格式喂进 ``spc_wav.convert``，libgme 报
        「不认识的格式」→ 零产出 → 用户看到「解码零产出，文件可能损坏或
        格式不受支持」。实测 ``Akko ni Omakase! Brain Shock`` 的 8 个
        ``.mini2sf`` 全中招（而 ``vio_native.decode_pcm`` 单独调是好的，
        ``vio2wav.exe`` 直接跑也出 12,348,044 字节的完整 WAV）。

        另一个入口 :mod:`convert_job` 早就接了这四类，所以「Web 转换页
        能转、下载页转不了」—— 两条流程的分派表必须同步，这是教训。

        Args:
            src: 源文件路径。
            log_buf: 日志收集列表（工作线程只往这里 append）。
            job_cancel: 本文件的超时/取消 Event。
            mod: 已导入的 native 模块（``usf_native`` 等）；None 表示不可用。
            label: 日志里显示的家族名（``"USF"`` / ``"2SF"`` / ``"SSF"`` /
                ``"GSF"`` / ``"QSF"``）。

        Returns:
            ``{ok, fail, files, seconds, errors, last_idx}`` —— 与
            :func:`spc_wav.convert` 同形，记账段零改动。
        """
        if mod is None:
            return {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
                    "errors": ["%s 解码模块不可用（导入失败）" % label],
                    "last_idx": self._bucket.start_idx}
        cancel = _CancelPair(self._cancel, job_cancel)
        r = mod.decode_pcm(src, cancel=cancel, on_progress=self._on_progress)
        if not r["ok"]:
            return {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
                    "errors": [r["error"]], "last_idx": self._bucket.start_idx}
        log_buf.append("   ℹ %s → %s（%.2f 秒）"
                       % (os.path.splitext(src)[1].lower(),
                          r["kind"], r["seconds"]))
        # 写盘交给 spc_wav.write_tracks —— 命名/去重/编号/削波提示全复用，
        # 与 .psf / .txtp / CD 音轨走同一套（别在这里另写一份）。
        return spc_wav.write_tracks(
            [{"name": r["title"], "pcm": r["pcm"], "little_endian": True}],
            self._bucket.dir, max_secs=0.0, on_log=log_buf.append,
            cancel=cancel, used=self._bucket.used,
            start_idx=self._bucket.start_idx, fmt=self._fmt)

    def _convert_xa(self, src: str, log_buf: list, job_cancel) -> dict:
        """独立 ``.XA``（CD-XA 音频流）-> WAV（v5.2.4）。

        返回结构与 :func:`spc_wav.convert` **同形**，记账段零改动。

        ⚠ 以前这条不存在，于是 JoshW 那种「15 个 ``.XA`` + 一个 ``.PSF``」
        的平铺包**只转得出 PSF 那一首** —— 用户看到的就是「只有菜单音乐」。
        ``is_convertible()`` 不放行 ``.xa``，队列里根本没有它们。

        Args:
            src: ``.XA`` 文件路径。
            log_buf: 日志收集列表（工作线程只往这里 append）。
            job_cancel: 本文件的超时/取消 Event。

        Returns:
            ``{ok, fail, files, seconds, errors, last_idx}``。
        """
        if psx_txtp is None:
            return {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
                    "errors": ["PS1 XA 模块不可用（psx_txtp 导入失败）"],
                    "last_idx": self._bucket.start_idx}
        return psx_txtp.convert(
            [src], self._bucket.dir,
            used=self._bucket.used, start_idx=self._bucket.start_idx, max_secs=0.0,
            cancel=_CancelPair(self._cancel, job_cancel),
            on_log=log_buf.append, on_progress=self._on_progress)

    def _convert_stream(self, src: str, log_buf: list, job_cancel) -> dict:
        """流式音频（.adx / .mod / .hwas / .swav / .dvi / .XVAG / .hca…）-> WAV。

        返回结构与 :func:`spc_wav.convert` 同形，调用点的记账段直接复用。
        """
        return stream_audio.convert_one(
            src, self._bucket.dir,
            used=self._bucket.used, start_idx=self._bucket.start_idx, max_secs=0.0,
            cancel=_CancelPair(self._cancel, job_cancel),
            on_log=log_buf.append, on_progress=self._on_progress)

    def _convert_pivotal(self, src: str, log_buf: list, job_cancel) -> dict:
        """GameCube 的 Pivotal ``.psf`` -> WAV（v5.9.11）。

        和 ``_convert_psf`` 的区别：**不需要 BIOS、不需要模拟器** ——
        它是预录的 PSX-ADPCM / NGC-DSP 流，纯 Python 解（:mod:`pivotal_psf`，
        与 vgmstream 逐样本最大差 0）。所以它更接近 ``_convert_stream``。

        返回结构与 :func:`spc_wav.convert` 同形。
        """
        import pivotal_psf
        return pivotal_psf.convert_one(
            src, self._bucket.dir,
            used=self._bucket.used, start_idx=self._bucket.start_idx, max_secs=0.0,
            cancel=_CancelPair(self._cancel, job_cancel),
            on_log=log_buf.append, on_progress=self._on_progress)

    def _convert_cd(self, src: str, log_buf: list, job_cancel) -> dict:
        """CD-DA 音轨 -> WAV（v4.30）。

        返回结构与 :func:`spc_wav.convert` **同形**，所以调用点的记账段
        （体积统计 / 零产出判定 / manifest / 连续编号）完全复用。
        """
        if cd_audio is None:
            return {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
                    "errors": ["CD 音轨模块不可用（cd_audio 导入失败）"],
                    "last_idx": self._bucket.start_idx}
        return cd_audio.convert_one(
            src, self._bucket.dir,
            used=self._bucket.used, start_idx=self._bucket.start_idx, max_secs=0.0,
            cancel=_CancelPair(self._cancel, job_cancel),
            on_log=log_buf.append, on_progress=self._on_progress)

    def _note_cd_tracks(self, src: str, url: str) -> None:
        """PCE-CD 包：把「同目录还有 CD 音轨」这件事写进日志（ℹ 行）。

        为什么必须说：这类包是 **HES（芯片）+ .ape（CD-DA）混装**，
        我们只转 HES。不说的话用户只会看到「转了 N 首」和自己预期的
        「整张 OST」对不上，然后把它当成转换失败（实测用户报障的表述
        就是「pce-cd 既有芯片音源又有 CD 声像，转 wav 失败」）。

        CD 轨的正确处理方式不是转，而是**用别的工具听 / 换 .cue 走 CD 流程**
        （.ape 是 Red Book 音轨的无损抓轨，塞进 MSU-1 会把整轨当一首曲子）。
        """
        if os.path.splitext(src)[1].lower() != ".hes":
            return
        try:
            import chip_formats as _cf
            info = _cf.probe(src)
        except Exception:                            # noqa: BLE001
            return
        cd = list(getattr(info, "cd_tracks", None) or ())
        if not cd:
            return
        ids = list(getattr(info, "track_ids", None) or ())
        can = bool(cd_audio is not None and cd_audio.available())
        self._emit("cv_log", {"src": src, "url": url, "detail": [
            "ℹ %s：本包另有 %d 首 CD 音轨（.ape）%s"
            % (os.path.basename(src), len(cd),
               "，将与芯片音源一并转成 WAV（编号接在后面）" if can
               else "，但当前缺少 FFmpeg 组件，只能跳过（芯片音源 %s 照转）"
                    % (("按 .m3u 取 %d 首" % len(ids)) if ids else "全槽扫描"))]})

    def _drain(self) -> int:
        """丢弃队列里未开始项。返回丢弃数量。

        每丢一项必须 ``task_done()``，否则任何 ``join()`` 会挂死。
        **不回写状态**（FR-C8.4：避免几千行无意义刷新）。
        """
        dropped = 0
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return dropped
            dropped += 1
            try:
                self._q.task_done()
            except ValueError:                        # pragma: no cover
                pass

    # ---- 线程主体 ----------------------------------------------------------
    def run(self) -> None:                            # noqa: D401 - 线程入口
        """消费循环。**无论正常 / 异常 / 取消，退出必经 finally 发事件。**

        收尾判定必须是 ``_src_finished and _q.empty()`` **两个条件同时满足**：
        只看队列空会**早退**（第一个包还没解压完时队列瞬时为空）。
        """
        try:
            self._bucket.manifest.load()
            self._ensure_numbering()
            while True:
                if self._cancel.is_set():
                    break
                if not self._gate.is_set():
                    if self._wait_disk():
                        break                        # 放弃剩余 -> 正常收尾
                    continue
                try:
                    job = self._q.get(timeout=self.POLL_S)
                except queue.Empty:
                    if self._src_finished and self._q.empty():
                        break                       # 源已结束且队列空 -> 正常收尾
                    continue
                try:
                    self._convert_one(job)
                except Exception as exc:             # 单文件兜底（第三重）
                    self._fail += 1
                    self._emit("cv_file_failed", {
                        "src": str(job.get("src") or "?"),
                        "url": str(job.get("url") or ""),
                        "policy": str(job.get("policy") or ""),
                        "error": "%s: %s" % (type(exc).__name__, exc)})
                finally:
                    try:
                        self._q.task_done()
                    except ValueError:                # pragma: no cover
                        pass
        except Exception as exc:                      # 最外层：线程永不静默死亡
            self._emit("cv_error", {"error": "%s: %s" % (type(exc).__name__, exc)})
        finally:
            # ⚠⚠ **cv_thread_exit 必须无条件发出**（QA 对账发现这条路径零覆盖）。
            #   它是 GUI 唯一的收尾信号 —— :meth:`DownloadDialog._handle_cv` 收到它
            #   才会调 `_finish_download()`。若这里丢了事件，**下载窗口会永久卡死**：
            #   开始/停止按钮锁死、进度条不动，用户只能杀进程。
            #   所以 stats() 也要兜住: 它要是抛异常, 就连 cv_thread_exit 一起吞了。
            self._cur = None
            try:
                stats = self.stats()
            except Exception:                          # noqa: BLE001
                stats = {"ok": self._ok, "fail": self._fail,
                         "skip": self._skip, "wavs": self._wavs,
                         "bytes": self._bytes, "secs": 0.0, "left": 0}
            self._emit("cv_thread_exit", stats)

    def _wait_disk(self) -> bool:
        """卡在磁盘门禁时的等待与收敛。

        Returns:
            True = 已**放弃剩余**（发了 ``cv_abandoned``），调用方应退出循环；
            False = 继续等。
        """
        now = time.time()
        if now - self._last_pause >= self.PAUSE_LOG_S:
            self._last_pause = now
            self._emit("cv_paused", {"reason": "disk",
                                     "left": self._q.qsize()})
        # 每轮重新探测：用户删文件后自动恢复，**不加按钮**（PM 定案）
        if self._disk_ok(0):
            self._disk_stall = 0
            self._gate.set()
            return False
        self._disk_stall += 1
        # 收敛保证：源已结束 + 队列非空 + 磁盘**持续**不足 -> 放弃剩余。
        # 没有这条，界面会永远停在「磁盘空间不足」，用户只能杀进程。
        if (self._src_finished and not self._q.empty()
                and self._disk_stall >= self.DISK_STALL_ROUNDS):
            left = self._drain()
            self._emit("cv_abandoned", {"left": left})
            return True
        self._gate.wait(self.PAUSE_POLL_S)      # cancel() 会立刻唤醒
        return False

    # ---- v6.0.2：「只出 WAV」的分支补编码 ---------------------------------
    def _reencode_to_fmt(self, sub: dict, log_buf: List[str]) -> dict:
        """把 WAV-only 分支的产物补编码成 ``self._fmt``，**就地**改 ``sub``。

        为什么需要这一步（v6.0.2 修的真实缺陷）：

        ``_convert_stream`` / ``_convert_cd`` / ``_convert_xa`` /
        ``_convert_pivotal`` 调的是各模块自己的 ``convert_one``，那几个函数
        **只会写 WAV**（签名里根本没有 fmt）；只有「解码出 PCM 再交给
        ``spc_wav.write_tracks(fmt=…)``」的几条路径才会直接落 FLAC/MP3/OGG。

        结果是：Wii 的 ``.brstm``、PS3 的 ``.xvag``、3DS 的 ``.laac``、
        PS2 的 ``.adx``、CD 音轨……点「转 FLAC」之后，``FLAC/<游戏>/`` 里
        躺着的其实是 ``01_xxx.wav`` —— 目录名是 FLAC、内容是 WAV、体积一分
        没省。用户报的正是这个（机种 Wii，下载完点「转 FLAC」/「转 MP3」）。

        为什么不改那四个 ``convert_one`` 的签名：它们各自还要处理重采样、
        截断淡出、``used`` 去重、``start_idx`` 连续编号，把 fmt 塞进去等于在
        四个模块里各写一遍编码分支。这里统一做「WAV → 目标格式」只写一次，
        而且**天然覆盖以后新增的任何 WAV-only 解码器**。

        幂等：产物扩展名已经是目标格式时原样保留（芯片那几条路径会走到这里）。

        Args:
            sub: 某个分支返回的统计字典（``files`` 会被替换成新路径）。
            log_buf: 本文件的日志缓冲（调用点最后 5 行会随事件上报）。

        Returns:
            同一个 ``sub``（就地修改，调用点少写一次赋值）。
        """
        fmt = str(self._fmt or "wav").lower()
        files = list(sub.get("files") or ())
        if fmt == "wav" or not files:
            return sub
        try:
            import flac_out
        except Exception as exc:                     # noqa: BLE001
            msg = "编码模块不可用：%s" % exc
            log_buf.append("❌ " + msg)
            sub.setdefault("errors", []).append(msg)
            return sub
        try:
            _av, _why = flac_out.available(fmt)
        except Exception as exc:                     # noqa: BLE001
            _av, _why = False, str(exc)
        if not _av:
            msg = "%s 编码器不可用：%s" % (fmt.upper(), _why)
            log_buf.append("❌ " + msg)
            sub.setdefault("errors", []).append(msg)
            return sub

        # 封面：与 ``spc_wav.write_tracks`` 同一口径 —— 先看**本游戏目录**，
        # 下载页会在转换前把 cover.png 复制进去。
        cover = ""
        try:
            _here = os.path.join(self._bucket.dir, "cover.png")
            if os.path.isfile(_here):
                cover = _here
        except Exception:                            # noqa: BLE001
            cover = ""

        out: List[str] = []
        n_enc = 0
        for p in files:
            try:
                ext = os.path.splitext(str(p))[1].lower()
            except (TypeError, ValueError):
                continue
            if ext == "." + fmt:
                out.append(p)                        # 已经是目标格式
                continue
            if ext != ".wav" or not os.path.isfile(p):
                out.append(p)                        # 不是 WAV，别动它
                continue
            dst = os.path.splitext(p)[0] + "." + fmt
            try:
                with wave.open(p, "rb") as w:
                    sr = int(w.getframerate())
                    nch = int(w.getnchannels())
                    raw = w.readframes(w.getnframes())
                ok2, _p2, detail = flac_out.encode_pcm(
                    raw, dst, sr=sr, channels=nch,
                    cover=cover or None, fmt=fmt)
                if not ok2:
                    raise OSError(detail or "编码失败")
            except Exception as exc:                 # noqa: BLE001
                # 编码失败**不删 WAV**：音频已经解出来了，留着总比丢掉强，
                # 用户至少能听，也知道是「编码这一步」没成。
                msg = "%s → %s 失败：%s" % (os.path.basename(p), fmt.upper(), exc)
                log_buf.append("❌ " + msg)
                sub.setdefault("errors", []).append(msg)
                out.append(p)
                continue
            try:
                os.remove(p)                         # 中间 WAV，编完即删
            except OSError:
                pass
            out.append(dst)
            n_enc += 1
            try:
                log_buf.append("   %s %s  %.1fMB"
                               % (fmt.upper(), os.path.basename(dst),
                                  os.path.getsize(dst) / 1048576.0))
            except OSError:
                pass
        if n_enc:
            line = ("   ℹ %d 个 WAV 已编码成 %s（中间文件已删除）"
                    % (n_enc, fmt.upper()))
            log_buf.append(line)
            self._emit("cv_log", {"text": line})
        sub["files"] = out
        return sub

    def _drop_duplicate_products(self, src: str, log_buf: List[str]) -> int:
        """清掉「把 ``xxx.fsb`` 和 ``xxx.fsb.wav`` 当成两首」留下的重复产物。

        ## 这条修的是什么（v6.0.6，用户报的）

        > 「同一个游戏转出的 flac 数量和 MP3 数量不同，以 PS4 为例…」

        根因见 :func:`is_derived_wav`：目录里 ``xxx.fsb`` 和 ``xxx.fsb.wav``
        都被当成音源，各转一遍。实测 ``Assetto Corsa`` 是 10 首 → 20 个 FLAC
        （``01_xxx.flac`` 和 ``02_xxx.fsb.flac`` 字节数完全相同）。

        ## 为什么是「清整个目录」而不是「只删多出来的那几个」

        编号是目录级的（``_scan_bucket`` 按已有最大号续编）。只删掉 10 个
        重复的，剩下的就是 ``01, 03, 05 …`` 一列带窟窿的号 —— 用户会以为
        丢了文件。所以这里把本目录里**记账覆盖的、目标格式的产物全删**、
        记账全清，让这一批从 ``01`` 重新编。代价是重新解码一遍，但
        这是一次性的、且结果是唯一正确的。

        ## 边界（收得很窄，别放宽）

        * 只在 ``flac`` / ``mp3`` / ``ogg`` 上做 —— **绝不在 ``wav`` 上做**，
          因为 WAV 桶的「产物」就是 ``xxx.fsb.wav`` 本身，删了等于删源；
        * 只删**记账名单里点过名的**文件，用户在目录里手放的东西一个不碰；
        * 没有「衍生 WAV 源」记录时直接返回 0，正常目录零开销。

        Args:
            src: 当前处理的源文件（只为日志措辞，不参与筛选）。
            log_buf: 本文件的日志缓冲（写一行说明，用户能看出发生了什么）。

        Returns:
            实际删掉的文件数。
        """
        fmt = str(self._fmt or "wav").lower()
        if fmt not in ("flac", "mp3", "ogg"):
            return 0
        man = getattr(self._bucket, "manifest", None)
        if man is None:
            return 0
        try:
            # ensure_loaded：生产路径里 _bucket_for / run() 已经 load 过，这里是
            # 幂等的空操作；但**不能省** —— 忘了 load 的表现是所有查询返回空，
            # 自愈一声不响地返回 0，看起来跟「本来就干净」一模一样。
            man.ensure_loaded()
            if not man.derived_src_keys():
                return 0
            records = man.all_records()
        except Exception:                  # noqa: BLE001
            return 0
        removed = 0
        for key, rec in records:
            for n in ((rec or {}).get("wavs") or ()):
                if not str(n).lower().endswith("." + fmt):
                    continue
                try:
                    p = os.path.join(self._bucket.dir, str(n))
                except (TypeError, ValueError):
                    continue
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                        removed += 1
                except OSError:
                    pass
            try:
                man.forget(key)            # 记账一起清，编号才能从 01 重来
            except Exception:              # noqa: BLE001
                pass
        if not removed:
            return 0
        line = ("   ℹ 清掉 %d 个旧的 %s（旧版本把「音源」和「音源转出的 WAV」"
                "当成两首，每首都转了两遍）—— 重新从 01 编号"
                % (removed, fmt.upper()))
        log_buf.append(line)
        # 和 _drop_stale_products 一样直接推日志面板：这条不是失败明细，
        # 只在失败时随 cv_log 上报的话用户永远看不到。
        self._emit("cv_log", {"text": line})
        self._bucket.numbered = False
        self._scan_bucket(self._bucket)
        return removed

    def _drop_stale_products(self, src: str, log_buf: List[str]) -> int:
        """清掉整个输出目录里「记账说转过、扩展名却不是目标格式」的旧产物。

        ## 为什么必须删，而不是「转完覆盖就行」

        ``_scan_bucket`` 的编号是**接着目录里已有的最大号往后编**的（那是
        「再下一批新游戏」的正常需求）。所以 FLAC 目录里如果还躺着上次留下的
        ``01``~``31`` 号 WAV，重转出来的就是 ``32``~``62`` 号 —— 用户拿到的
        是「31 个旧 WAV + 31 个新 FLAC」，比不修还乱。

        ## 为什么一次清**整个目录**，而不是只清当前这个源

        编号是目录级的：只清一个源的产物，目录里还剩 30 个旧 WAV，
        ``start_idx`` 照样是 30，第一个新文件就从 31 开始。所以第一个需要
        重转的源一进来，就把这个目录里**所有**坏记录一次清完 —— 后面的源
        自然从 01 接上。

        删的判据刻意收得很窄（见 :meth:`ConvertManifest.stale_records`）：
        **只删本程序自己记过账、且名单里一个目标扩展名都没有的 .wav**，
        用户在目录里手放的东西一个都不碰。

        Args:
            src: 当前正在处理的源文件（只为日志措辞用，不参与筛选）。
            log_buf: 本文件的日志缓冲（会写一行说明，用户能看出发生了什么）。

        Returns:
            实际删掉的文件数。
        """
        fmt = str(self._fmt or "wav").lower()
        if fmt not in spc_wav.ENC_FMTS:
            return 0
        man = getattr(self._bucket, "manifest", None)
        if man is None:
            return 0
        try:
            stale = man.stale_records(fmt)
        except Exception:                  # noqa: BLE001
            return 0
        if not stale:
            return 0
        removed = 0
        for key, names in stale:
            for n in names:
                if not n.lower().endswith(".wav"):
                    continue
                try:
                    p = os.path.join(self._bucket.dir, n)
                except (TypeError, ValueError):
                    continue
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                        removed += 1
                except OSError:
                    pass
            try:
                man.forget(key)            # 记账一起清，编号才能从 01 重来
            except Exception:              # noqa: BLE001
                pass
        if not removed:
            return 0
        line = ("   ℹ 清掉 %d 个上次留下的 WAV（旧版本转 %s 时只写了 WAV）"
                % (removed, fmt.upper()))
        log_buf.append(line)
        # 直接推到日志面板：这条不是失败明细，只在失败时随 cv_log 上报的话
        # 用户永远看不到「旧产物被清了」这件事。
        self._emit("cv_log", {"text": line})
        # 重新扫一遍目录：``used`` / ``start_idx`` 都按**删除后**的实际内容算
        self._bucket.numbered = False
        self._scan_bucket(self._bucket)
        return removed

    # ---- 单文件转换（**三重异常边界**）------------------------------------
    def _convert_one(self, job: dict) -> None:
        """判重 -> 磁盘门禁 -> policy 分流 -> 单文件 convert -> 记账。

        三重异常边界：
        ① :meth:`run` 里的 ``except``（单文件整体）；
        ② 本方法内 :func:`spc_wav.convert` 调用段（解码 / 写盘）；
        ③ 三个解码回调各自的 ``except``（见 :meth:`_emit`）。
        """
        src = str((job or {}).get("src") or "")
        url = str((job or {}).get("url") or "")
        pkg = str((job or {}).get("pkg") or "")

        # ★ B21: **尽早**挂上 _cur, 把「停止按钮够不到」的窗口压到最小。
        #   这个窗口落在「刚出队」—— 用户最容易点停止的时刻。若用户此刻点停止,
        #   cancel() 读到 _cur is None 就什么都没置位, 本轮转换停不下来。
        # ⚠ **真正的防线不在这里**: 全局 self._cancel 已在 composite 里(§3.3),
        #   所以即使这一行还没执行, 最坏也只是「多跑一个文件的开头」。
        #   _cur 置位只是锦上添花 —— 竞态窗口无法根除, 只能压缩。
        # ⚠ 这里给一个独立的 job_cancel; 真正进 convert() 的是
        #   _CancelPair(全局, job_cancel), 见下面 ③。
        job_cancel = threading.Event()
        self._cur = {"job": job, "cancel": job_cancel,
                     "t0": time.time(), "timed_out": False}
        # 单独发一个「轮到谁了」的事件。
        # ⚠ ``cv_progress`` 里**没有**文件名：它的参数按解码路径不同而不同
        #   （见 :meth:`_on_progress`），有的路径第 3 个参数是曲名、有的是秒数，
        #   界面没法从里面反推源文件。所以这里明确发一次。
        #   界面拿它写状态行「正在转 xxx」+ 限流心跳的当前文件名。
        self._emit("cv_file_start", {"src": src, "url": url, "pkg": pkg,
                                     "policy": str((job or {}).get("policy") or "")})
        # 切到这个源该写的目录（每个游戏一个子文件夹）。换桶之后所有
        # ``self._bucket.*`` 自动指向新目录，编号/重名/清单各算各的。
        # finally 里必须换回来 —— 否则下一个文件会写进上一个游戏的目录。
        prev = self._bucket
        try:
            self._bucket = self._bucket_for(src)
            self._convert_one_inner(job, src, url, pkg, job_cancel)
        finally:
            self._bucket = prev
            self._cur = None

    def _convert_one_inner(self, job, src, url, pkg, job_cancel) -> None:
        """``_convert_one`` 的主体（**三层 try/finally 都已由外层兜住**）。"""
        if not src or not os.path.isfile(src):
            self._fail += 1
            self._emit("cv_file_failed", {"src": src, "url": url, "pkg": pkg,
                                          "policy": "", "error": "源文件不存在"})
            return

        # 本文件的日志缓冲。**提到判重之前**是因为 v6.0.2 的「格式自愈」
        # （_drop_stale_products）要往里写一行说明，而那一步发生在 ① 之后。
        log_buf: List[str] = []

        # ---- v6.0.6：重复产物自愈（**必须排在 ① 之前**）----------------------
        # 「旧版本把 xxx.fsb 和 xxx.fsb.wav 当成两首」留下的目录里，那 10 个
        # 真正的源（.fsb）**全都 already_done 为真** —— 排在 ① 之后就永远
        # 走不到这里，用户点十次「转 FLAC」也还是 20 个文件。所以这一步必须
        # 先跑：它删掉整目录的旧产物 + 清空记账，让下面的 ① 自然判成「没转过」。
        self._drop_duplicate_products(src, log_buf)

        # ① 已转过？（H3：源文件指纹 + WAV 存在性双校验，**绝不用「目录非空」**）
        #     v5.9.18 加第三道：MP3 的老产物内容是坏的，按记账版本判重转。
        if self._bucket.manifest.already_done(src, self._fmt):
            self._skip += 1
            rec = self._bucket.manifest.entry(src) or {}
            self._emit("cv_skip", {"src": src, "url": url, "pkg": pkg,
                                   "reason": "done",
                                   "n_wav": int(rec.get("n_wav", 0) or 0)})
            # v4.24: 已经转过、但用户**刚勾上**「合并」时不能重转一遍
            # （几十首要重解码几分钟），直接用 manifest 记账里的分轨拼 ——
            # 纯字节拷贝，44 首 / 61MB 约 0.2 秒。
            if self.merge_wav:
                self._merge_for(src, rec.get("wavs") or (), force=False)
            return

        # ---- v6.0.2：格式自愈 ----------------------------------------------
        # 「记账里有这个源，但产物是 .wav、目标却是 FLAC/MP3」= v6.0.1 及更早
        # 把流式音频转 FLAC/MP3 时留下的假产物。删掉它们再重转，编号才能从
        # 01 重新开始；否则会接着 31 往后编，用户拿到 32~62 号 + 一堆旧 WAV。
        self._drop_stale_products(src, log_buf)

        # ② 磁盘够吗？不足则**任务不出队**（在 ``get`` 之前就该判，但那时还没
        #    拿到 src；所以这里放回队尾 + 关门禁，见 ``_gate`` 判定顺序）。
        need = self._estimate_bytes(src)
        if not self._disk_ok(need):
            self._gate.clear()
            self._disk_stall = max(1, self._disk_stall)
            try:
                self._q.put(dict(job))          # 放回队尾，队列保留不丢
            except Exception:                    # noqa: BLE001
                self._fail += 1
                self._emit("cv_file_failed", {
                    "src": src, "url": url, "pkg": pkg,
                    "error": "磁盘空间不足，且任务无法重新入队"})
            return

        # ③ 格式分流（H1）—— policy_for 是唯一实现点；入队时定过一次，这里复核
        policy = policy_for(src)
        job_policy = str((job or {}).get("policy") or "")
        if job_policy and job_policy != policy:
            # 入队时的判断与当前文件不符（理论上不会发生）——以现文件为准。
            policy = policy

        try:
            if policy == "cd":
                # v4.30: CD-DA 音轨（.ape/.flac/.wav…）—— PyAV 解码，产出格式
                # 与芯片音源一致（44.1k/16bit/2ch），编号接在同一套 used/start_idx 上。
                extra = {}
            elif policy == "txtp":
                # v4.41: PS1 .txtp 索引 —— 纯 stdlib 原生解，不吃 chip_secs/loader。
                extra = {}
            elif policy == "xa":
                # v5.2.4: 独立 .XA —— 同一条原生解码路径，只是没有索引文件。
                extra = {}
            elif policy == "psf":
                # v5.1-web-psf: PSF 系 —— 外挂 psf2wav.exe + BIOS，
                # 同样不吃 chip_secs/loader（那些是 libgme 的参数）。
                extra = {}
            elif policy == "pivotal":
                # v5.9.11: GameCube 的 Pivotal .psf —— **不用 BIOS、不用模拟器**，
                # 纯 Python 解预录流，所以参数口径与 stream 那一族一致。
                extra = {}
            elif policy == "stream":
                # v5.2.6: 流式音频 —— PyAV / 纯 Python IMA，不吃 libgme 的参数。
                extra = {}
            elif policy in ("usf", "vio", "ssf", "gsf", "qsf"):
                # v5.4: 外挂解码器家族（USF / 2SF / SSF-DSF / GSF）——
                # 同样不吃 libgme 的 chip_secs/loader（那些是 libgme 的参数）。
                # v5.9.10: QSF（Capcom QSound）也归这一族。
                extra = {}
            elif policy == "id666":
                extra: Dict[str, object] = {}      # id666 分支不吃 chip_secs/loader
            else:
                try:
                    import chip_decode
                except Exception as exc:           # noqa: BLE001
                    self._fail += 1
                    self._emit("cv_file_failed", {
                        "src": src, "url": url, "pkg": pkg, "policy": policy,
                        "error": "芯片解码模块不可用: %s" % exc})
                    return
                extra = {"chip_secs": self.CHIP_SECS,
                         "loader": chip_decode.load_chip_paths}
            # v4.29: PCE-CD 包 = 芯片音源（.hes）+ CD 音轨（.ape）在同一个目录。
            # 两者都要转，但解码器不同 —— 这里给一句「本包有哪些」，便于对数。
            self._note_cd_tracks(src, url)
            convert = self.convert_fn or spc_wav.convert
            # ④ 单文件一次 convert；used / start_idx **跨调用共享**（H2）
            #
            # ★ B20: cancel 传**复合对象**，不是裸 job_cancel。
            #   停止按钮置的是全局 self._cancel, 而 300s 超时置的是 job_cancel
            #   —— 只传后者, 停止按钮**完全够不到**正在转的文件（症状: 队列丢了、
            #   按钮灰了, 但当前文件一路跑完才停）。
            #   spc_wav / chip_decode 对 cancel 的全部调用点**只用 .is_set()**,
            #   所以任何带 is_set() 的对象都能替代 Event, 不必继承。
            # ★ B21: 全局 _cancel 在 composite 里 -> **空窗期的真正防线**。
            #   只靠「保证 _cur 一定非空」做不到（竞态无法根除）。
            #
            # v4.30: CD-DA 音轨走 cd_audio（PyAV），返回结构与 spc_wav.convert
            # 同形 -> 下面的记账段（体积/零产出/manifest/编号）**一行都不用改**。
            if policy == "cd":
                sub = self._convert_cd(src, log_buf, job_cancel)
            elif policy == "txtp":
                sub = self._convert_txtp(src, log_buf, job_cancel)
            elif policy == "xa":
                sub = self._convert_xa(src, log_buf, job_cancel)
            elif policy == "psf":
                sub = self._convert_psf(src, log_buf, job_cancel)
            elif policy == "pivotal":
                sub = self._convert_pivotal(src, log_buf, job_cancel)
            elif policy == "stream":
                sub = self._convert_stream(src, log_buf, job_cancel)
            elif policy == "usf":
                sub = self._convert_emu(src, log_buf, job_cancel,
                                        usf_native, "USF")
            elif policy == "vio":
                sub = self._convert_emu(src, log_buf, job_cancel,
                                        vio_native, "2SF")
            elif policy == "ssf":
                sub = self._convert_emu(src, log_buf, job_cancel,
                                        ssf_native, "SSF")
            elif policy == "gsf":
                sub = self._convert_emu(src, log_buf, job_cancel,
                                        gsf_native, "GSF")
            elif policy == "qsf":
                sub = self._convert_emu(src, log_buf, job_cancel,
                                        qsf_native, "QSF")
            else:
                # v5.9.7：fmt 只在**真用 spc_wav.convert** 时传。
                # self.convert_fn 是测试注入钩子，签名是老的
                # ``(paths, out, policy, cancel=..., **kw)`` —— 往里塞新参数
                # 会让一批老 QA 脚本 TypeError，而它们测的其实是队列/取消，
                # 与输出格式无关。
                _cvt_extra = dict(extra)
                if self.convert_fn is None:
                    _cvt_extra["fmt"] = self._fmt
                sub = convert(
                    [src], self._bucket.dir, policy,
                    on_log=log_buf.append,
                    on_progress=self._on_progress,
                    on_stage=self._on_stage,
                    cancel=_CancelPair(self._cancel, job_cancel),
                    speed=1.0, max_secs=0.0,
                    used=self._bucket.used, start_idx=self._bucket.start_idx,
                    total=None, **_cvt_extra)
            if not isinstance(sub, dict):
                sub = {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
                       "errors": ["convert 未返回统计字典"], "last_idx":
                       self._bucket.start_idx}
            # ★ v6.0.2：WAV-only 分支（stream / cd / xa / pivotal）在这里补编码。
            #   必须放在下面的 ⑤ 统计之前 —— 体积、``wavs`` 名单、manifest 记账
            #   都读 ``sub["files"]``，晚一步就会把已删掉的 WAV 名字记进账，
            #   下次 ``already_done()`` 找不到文件 → 每点一次都重转一遍。
            sub = self._reencode_to_fmt(sub, log_buf)
            # 编号跨文件 / 跨包连续（spc_wav 的 chip 分支已自己加过 start_idx，
            # 这里再兜一次也不会重复加：last_idx 是**最终序号**）
            try:
                self._bucket.start_idx = max(self._bucket.start_idx,
                                      int(sub.get("last_idx") or 0))
            except (TypeError, ValueError):
                pass
        except Exception as exc:                     # ②：解码 / 写盘异常
            self._fail += 1
            detail = list(log_buf[-5:])
            # FR-C10.4：内部 5 行解码日志**不刷屏**，只在**该文件失败**时
            # 作为 cv_log 事件发一次（GUI 侧把它并入失败日志）。
            if detail:
                self._emit("cv_log", {"src": src, "url": url, "detail": detail})
            self._emit("cv_file_failed", {
                "src": src, "url": url, "pkg": pkg, "policy": policy,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "detail": detail,                   # 仅失败时展开明细
                "timed_out": bool(self._cur.get("timed_out"))
                if self._cur else False})
            return

        # ⑤ 统计：体积必须用 files 的实际 getsize，**不用 seconds×176400**
        #    （后者含被跳过的空轨，虚高 —— PRD FR-C11.3）。
        wavs: List[str] = []
        nbytes = 0
        for p in (sub.get("files") or ()):
            try:
                if os.path.isfile(p):
                    wavs.append(os.path.basename(p))
                    nbytes += os.path.getsize(p)
            except (OSError, TypeError, ValueError):
                continue
        n_ok = int(sub.get("ok") or 0)
        errors = list(sub.get("errors") or ())[:2]

        # ---- B18: 零产出静默失败 ----
        # 单文件解码失败**不抛异常**, 落在 chip_decode 的 last_skips 里 ->
        # 统计是 {'ok':0,'fail':0,'errors':[],'files':[]}。只看 fail 会判成功。
        # ⚠ 光靠 probe 挡不住: 实测截断 / 全零 / 合法头+零数据三种坏 NSF,
        #   probe **全部**判为合法 NSF（只看魔数 NESM）。必须转换后判产出数。
        # ⚠ 判据是「实际落盘的 WAV 数」, 不是 ok/fail/errors —— 这三者
        #   在「停止瞬间」的场景里都返回 0 但落盘数不同（B21 判据）。
        if not wavs or n_ok <= 0:
            skips = read_last_skips()
            self._fail += 1
            reason = ("未产出任何 WAV（解码零产出，文件可能损坏或格式不受支持）")
            if skips:
                reason += "；跳过原因：" + skips
            self._emit("cv_file_failed", {
                "src": src, "url": url, "pkg": pkg, "policy": policy,
                "error": reason, "skips": skips, "zero_output": True,
                # n_wav / n_files 显式给 0：GUI 与日志都按「成功事件的同款
                # 字段」读这两个键，缺了会让统计口径不一致。
                "n_wav": 0, "n_files": 0, "bytes": 0,
                "detail": list(log_buf[-3:])})
            # ★ **manifest 不写成功记录**（B18 最关键的一点）。
            #   写了的话下次 already_done() 指纹匹配会跳过它 -> 永永不重试,
            #   用户还以为已经转过了。「静默 + 不可恢复」比「报错」更糟。
            return

        # ---- 停止瞬间已产出: 如实告知, 不静默、不假装什么都没产 ----
        # 空窗期（B21）最坏是「在写第一轨前被中止」, 但若首批第一个检查点之前
        # 停下, 可能留下该文件的第一个 WAV。这**不违反**「已产出 WAV 一律保留」,
        # 但必须让用户知道（FR-C10.4 的降级告知精神）。
        if self._cancel.is_set():
            self._emit("cv_log", {"src": src, "url": url, "detail": [
                "ℹ 停止瞬间正在转换: %s，已产出 %d 首（保留）"
                % (os.path.basename(src), n_ok)]})

        # ⑥ 记账：**每文件立即原子落盘**（中途崩溃最多丢一条，不丢全部）
        try:
            self._bucket.manifest.record(src, wavs, policy)
        except Exception:                            # noqa: BLE001
            pass

        # ⑥b v4.24: 多轨合并（分轨全留，另出一个整轨文件）
        self._merge_for(src, wavs)

        self._wavs += len(wavs)
        self._bytes += nbytes
        self._secs += float(sub.get("seconds") or 0.0)
        self._ok += 1
        if skips_note := read_last_skips():
            self._emit("cv_log", {"src": src, "url": url, "detail": [
                "ℹ %s: 有轨道被跳过（%s）"
                % (os.path.basename(src), skips_note)]})
        self._emit("cv_file_done", {
            "src": src, "url": url, "pkg": pkg, "policy": policy,
            "n_wav": n_ok, "n_files": len(wavs), "bytes": nbytes,
            "errors": errors,
            "detail": list(log_buf[-5:]) if errors else []})

    # ---- v4.24: 多轨合并 ---------------------------------------------------
    def _merge_for(self, src: str, names, force: bool = True) -> None:
        """把**同一个源**产出的分轨再拼成一个整轨文件（分轨全部保留）。

        为什么落在这里而不是"整批转完再统一合"：
        ① 只有这里才知道「哪些 WAV 属于同一个源」（``names`` 就是）；
        ② 单个源失败/被中断时，别的源不受影响；
        ③ 已转过的源（``already_done`` 分支）能用 manifest 记账里的名字
           补合并，不必重新解码几十首。

        Args:
            force: True = 无条件重写（刚转完，分轨是新的）；
                   False = 合并文件已存在就跳过（补合并路径：源没变，
                   昨天已经合过了，不该每点一次下载就重拷一遍几百 MB）。

        ⚠ 合并**失败不改统计口径**（``_ok`` / ``_wavs`` / ``_bytes`` 都不动）：
        分轨已经成功了，合并只是附加产物，失败不该让用户看到「转 WAV 失败」。
        """
        if not self.merge_wav or wavmerge is None:
            return
        wavs: List[str] = []
        for n in (names or ()):
            try:
                p = os.path.join(self._bucket.dir, str(n))
            except (TypeError, ValueError):
                continue
            if os.path.isfile(p):
                wavs.append(p)
        if len(wavs) < 2:
            return                     # 单轨源（SPC 常见）没有合并的意义
        stem = os.path.splitext(os.path.basename(src))[0] or "merged"
        out = wavmerge.merge_target(self._bucket.dir, stem)
        if not force and os.path.exists(out):
            return                     # 补合并：已经有了，别重复拷
        res = wavmerge.merge_wavs(wavs, out, cancel=self._cancel)
        if res.get("ok"):
            self._merged += 1
            self._emit("cv_merged", {
                "src": src, "url": "", "path": res["path"],
                "files": res["files"], "seconds": res["seconds"],
                "bytes": res["bytes"]})
        elif self._cancel.is_set():
            pass                       # 用户点了停止，不算失败
        else:
            self._merge_fail += 1
            self._emit("cv_merged", {"src": src, "url": "",
                                     "error": res.get("error") or "未知错误"})


if __name__ == "__main__":  # 手动自检: python dl_convert.py <文件...>
    import sys
    for path in sys.argv[1:]:
        print("%-28s -> policy=%-6s convertible=%s"
              % (os.path.basename(path), policy_for(path), is_convertible(path)))
    print(json.dumps(estimate_files(sys.argv[1:]), ensure_ascii=False))
