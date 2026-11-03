# -*- coding: utf-8 -*-
"""``.s98``（PC-98 / PC-88 的 YM2608 音源）→ PCM：调 ``s982wav.exe`` 的薄封装。

## 为什么 .s98 要单独走外挂解码器

``.s98`` 是 **YM2608（OPNA）的寄存器写入日志** —— 和 ``.vgm`` 同族，文件里
没有音频，只有「第几个 tick 往哪个寄存器写什么」。要出声必须真跑芯片模拟。

工程里的 libgme 走不通，**这一点是读源码确认的**，不是凭「工程里没有核心」：

``gme/Vgm_Emu_Impl.cpp`` 的 ``run_commands()`` 只实现 5 条芯片指令 ——

========================  ==========================================
VGM 指令                   芯片
========================  ==========================================
``0x4F`` cmd_gg_stereo     SN76489 立体声
``0x50`` cmd_psg           SN76489
``0x51`` cmd_ym2413        YM2413（OPLL）
``0x52`` cmd_ym2612_port0  YM2612 端口 0
``0x53`` cmd_ym2612_port1  YM2612 端口 1
========================  ==========================================

YM2608 的 VGM 指令是 ``0x56`` / ``0x57``，会落到 ``default:`` 分支被
``pos += command_len(cmd) - 1`` **静默跳过**（只挂一句 "Unknown stream event"
警告）。也就是说「``.s98`` → ``.vgm`` → libgme」这条路**能跑通但输出精确
静音**，属于最坏的那种失败：不报错、不抛异常，只是没声音。

所以这里直接接 ymfm（Aaron Giles，BSD-3，MAME 在用的 OPN 家族核心），
它完整实现了 YM2608：FM 6 通道 4 算子 + SSG（AY-3-8910 兼容）+ ADPCM-A/B。

## 已经验证到什么程度

不是「能出声就算过」。用**自己合成的、音高已知的** ``.s98`` 逐级核对过：

===========  ==========  ==============
项目          期望          实测
===========  ==========  ==============
FM 音高       440.10 Hz    440.01 Hz
SSG 音高      998.40 Hz    998.40 Hz
===========  ==========  ==============

这一条同时钉住了：头部解析、tick 计时（10 ms）、端口/寄存器映射、
YM2608 时钟（7987200 Hz）、FM 核心、SSG 核心、隔直、重采样。

真实素材（joshw 的 Ys / PC-88 包 18 首）：全部解到，峰值 7064~27649
（无削波），总时长 1793 秒，解码耗时 55 秒（≈33× 实时）。

## 曲名：用文件自带的 tag，不用文件名

``.s98`` 头里有 ``tagOfs``，v0/v1 那里是纯文本曲名。实测 18/18 都有，
而且和包里 ``Ys.txt`` 的英文曲名逐条对得上（``Feena`` / ``Fountain of Love``
/ ``The Syonin`` …）。所以文件名是 ``01_ys_01_Feena`` 这种，信息量比
``01_ys_01`` 大。

## 时长：文件自带，不用调用方给

``.s98`` 数据流以 ``0xFD`` 显式结束，``tick 数 × tickMult / tickDiv`` 就是
精确长度。所以 :func:`load_tracks` **忽略 ``secs``**（那是给「格式里没有
时长、只能瞎猜 150 秒」的格式用的）—— 按 ``secs=150`` 截断的话，实测
15 号曲（231 秒）会被砍掉 81 秒。
"""

import os
import struct
import subprocess
import sys
import tempfile

import numpy as np

import chip_formats as cf
import winspawn

#: 静默启动（``CREATE_NO_WINDOW`` + ``SW_HIDE``），理由同 ``wsr_native`` /
#: ``kss_native``：主程序是窗口程序，解码器是控制台程序，不带这个每转一首
#: 闪一次黑框。
_silent_kw = winspawn.silent_kw
_rc_u32 = winspawn.rc_u32

#: 本模块负责的扩展名。单一真源在 ``chip_formats.S98_EXTS``。
S98_EXTS = cf.S98_EXTS

DECODER_NAME = "s982wav.exe"

#: 混音增益。**保持 1.0**，不学 ``kss_native.GAIN = 4.0``。
#:
#: 理由：libkss 比 libgme 低 12 dB 是**解码器本身的标定差异**，乘 4 是为了
#: 跟别的格式对齐。ymfm 没有这个问题 —— 实测 18 首峰值 7064~27649，
#: 最响的那首（``xx_12SEver``，27649）已经接近满刻度，再乘就削波了。
#: 安静的那几首（7064 / 7169）是**曲子本身安静**（Game Over 之类的短曲），
#: 抬增益只会让它们相对别的曲子变响，反而失真。
GAIN = 1.0

#: 0xc0000142 = DLL 初始化失败，偶发，多起几次基本都能过。
_DLL_INIT_FAILED = 0xC0000142
_SPAWN_RETRY = 3

#: 结束标记 0xFD 之后的余音。最后一个和弦还在响，直接截断会「咔」一下。
#: 1.5 秒和 ``chip_formats.CHIP_FADE_MS`` 同量级。
TAIL_SECS = 1.5


# ---------------------------------------------------------------------------
# 解码器定位
# ---------------------------------------------------------------------------

def app_dir():
    """程序目录。打包态按「先挑不在临时目录里的那个」来定（同 wsr_native）。"""
    if not (getattr(sys, "frozen", False) or "__compiled__" in globals()):
        return os.path.dirname(os.path.abspath(__file__))
    cands = []
    exe = sys.executable or ""
    if exe:
        cands.append(os.path.dirname(os.path.abspath(exe)))
    try:
        cands.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    tmp = os.path.abspath(tempfile.gettempdir()).lower()
    for d in cands:
        if d and not d.lower().startswith(tmp):
            return d
    return cands[0] if cands else ""


def _search_dirs():
    cands = [os.path.dirname(os.path.abspath(__file__))]
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
    """返回可用的 ``s982wav.exe`` 完整路径；找不到返回 ``""``。"""
    for d in _search_dirs():
        p = os.path.join(d, DECODER_NAME)
        if os.path.isfile(p):
            return p
    import shutil
    return shutil.which("s982wav") or shutil.which("s982wav.exe") or ""


def decoder_ready():
    """解码器在不在。返回 ``(bool, 说明)``。"""
    p = find_decoder()
    if p:
        return True, p
    return False, ("找不到 %s。它应该跟主程序放在一起；"
                   "源码运行时可以用 tools/s982wav/build.py 重新编译。"
                   % DECODER_NAME)


# ---------------------------------------------------------------------------
# 跑解码器 / 读 WAV
# ---------------------------------------------------------------------------

_IO_FILES = {}


def _io_files():
    """复用同一对 stdout/stderr 文件（同 kss_native：每次 mkstemp+remove 太贵）。"""
    p = _IO_FILES.get("dir")
    if p and os.path.isdir(p):
        return _IO_FILES["out"], _IO_FILES["err"]
    d = tempfile.mkdtemp(prefix="s98_io_")
    _IO_FILES["dir"] = d
    _IO_FILES["out"] = os.path.join(d, "out.txt")
    _IO_FILES["err"] = os.path.join(d, "err.txt")
    return _IO_FILES["out"], _IO_FILES["err"]


def _run(cmd, timeout=120.0, cancel=None):
    """跑解码器，返回 ``(rc, stdout, stderr)``。取消/超时抛 ``RuntimeError``。

    ⚠ stdout/stderr 必须**重定向到文件**，不能用 PIPE（Windows 匿名管道只有
    4 KB，子进程写满就阻塞，永远不退出）—— 同 ``chip_decode._run_worker``。
    """
    import time
    out_p, err_p = _io_files()
    rc, out, err = -1, b"", b""
    for _try in range(_SPAWN_RETRY):
        with open(out_p, "wb") as ofh, open(err_p, "wb") as efh:
            try:
                proc = subprocess.Popen(cmd, stdout=ofh, stderr=efh,
                                        **_silent_kw())
            except OSError as e:
                return -1, b"", ("启动解码器失败：%s" % e).encode("utf-8")
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.wait(3)
                    raise RuntimeError("已取消")
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait(3)
                    return -1, b"", "解码超时（%.0f 秒）".encode("utf-8")
                time.sleep(0.02)
            rc = proc.returncode
        try:
            with open(out_p, "rb") as f:
                out = f.read()
            with open(err_p, "rb") as f:
                err = f.read()
        except OSError:
            out, err = b"", b""
        if _rc_u32(rc) != _DLL_INIT_FAILED:
            break
    return rc, out, err


def _read_wav(path):
    """读 16bit PCM WAV，返回 ``(pcm_bytes, seconds, channels, rate)``。"""
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


def _to_stereo(pcm, channels):
    """统一输出 16bit/立体声小端，下游 ``write_tracks`` 不用为 S98 开特例。"""
    a = np.frombuffer(pcm, dtype="<i2")
    if channels == 1:
        a = np.repeat(a, 2)
    elif channels != 2:
        raise ValueError("只支持 1/2 声道，拿到 %d" % channels)
    if GAIN != 1.0:
        a = np.clip(a.astype(np.float32) * GAIN, -32768.0, 32767.0).astype("<i2")
    return a.tobytes()


# ---------------------------------------------------------------------------
# 对外：探测 / 解码 / 归一化音轨
# ---------------------------------------------------------------------------

def _safe_name(s):
    """曲名 → 文件名安全片段（同 kss_native / wsr_native）。"""
    bad = '\\/:*?"<>|\r\n\t'
    out = "".join("_" if c in bad else c for c in str(s or "")).strip(" .")
    return out[:80]


def decode_track(path, secs=0.0, cancel=None, out_path=None):
    """把一个 ``.s98`` 渲染成 16bit/44.1k/立体声小端 PCM 字节。

    ``secs`` <= 0 表示「按文件自身的长度」（推荐，也是默认）——``.s98`` 有
    显式结束标记，比外部猜的时长可靠。给了正数就当硬截断（试听/调试用）。

    返回 ``dict``：``{ok, pcm, seconds, rate, error, meta}``。
    """
    res = {"ok": False, "pcm": b"", "seconds": 0.0, "rate": 44100,
           "error": "", "meta": {}}
    exe = find_decoder()
    if not exe:
        res["error"] = decoder_ready()[1]
        return res

    tmp = None
    if out_path:
        wav = out_path
    else:
        fd, tmp = tempfile.mkstemp(prefix="s98_", suffix=".wav")
        os.close(fd)
        wav = tmp
    try:
        cmd = [exe, "-t%.2f" % TAIL_SECS, "-r44100"]
        if secs and secs > 0:
            cmd.append("-p%.2f" % float(secs))
        cmd += ["-o", wav, path]

        # 超时按「音频时长 × 0.3 + 60」给：实测 33× 实时，留 10 倍余量。
        try:
            dur = cf.probe(path).exact_secs
        except Exception:                               # noqa: BLE001
            dur = 0.0
        if not dur:
            dur = float(secs) if secs and secs > 0 else 240.0
        timeout = max(120.0, dur * 0.3 + 60.0)

        rc, _out, err = _run(cmd, timeout=timeout, cancel=cancel)
        if rc != 0:
            lines = [x for x in err.decode("utf-8", "replace").splitlines() if x]
            msg = ""
            for ln in lines:
                if ln.startswith("ERR"):
                    msg = ln[3:].strip()
            if not msg:
                msg = lines[-1] if lines else ""
            if _rc_u32(rc) == _DLL_INIT_FAILED:
                res["error"] = ("解码器启动失败（0xc0000142，DLL 初始化失败）"
                                "已自动重试 %d 次仍不行 —— 通常是系统临时资源紧张，"
                                "关掉几个程序或稍后重试。" % _SPAWN_RETRY)
            else:
                res["error"] = msg or ("解码器失败（退出码 %d）" % rc)
            return res

        # stderr 的 `OK frames=... peak=...` 留着当诊断信息
        for ln in err.decode("utf-8", "replace").splitlines():
            if ln.startswith("OK "):
                for kv in ln[3:].split():
                    if "=" in kv:
                        k, _, v = kv.partition("=")
                        res["meta"][k] = v
                break

        pcm, seconds, ch, rate = _read_wav(wav)
        if not pcm:
            res["error"] = "解码器说成功，但 WAV 里没有采样"
            return res
        res["pcm"] = _to_stereo(pcm, ch)
        res["seconds"] = seconds
        res["rate"] = rate
        res["ok"] = True
        return res
    except Exception as e:                              # noqa: BLE001
        res["error"] = "%s: %s" % (type(e).__name__, e)
        return res
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _rmtree_soft(path, batch=40):
    """分批删（环境里有「一次删 >50 个文件要二次确认」的守卫）。"""
    if not path or not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for i in range(0, len(files), batch):
            for name in files[i:i + batch]:
                try:
                    os.remove(os.path.join(root, name))
                except BaseException:                   # noqa: BLE001
                    pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def load_tracks(path, *, secs=150.0, cancel=None, on_progress=None, only=None):
    """一个 ``.s98`` → 归一化音轨列表（**一条曲目一轨**）。

    ``.s98`` 一个文件就是一首，没有子曲号，所以 ``only`` 只用于「试听时
    仍然解这一首」的语义兼容，不做过滤。

    ``secs`` **被忽略** —— 见模块文档「时长」一节：按 150 秒截断会把实测
    231 秒的那首砍掉一大半。
    """
    import msu1_to_cd as core

    del secs, only                      # 见 docstring：这个格式不需要它们
    prog = on_progress or (lambda *_a: None)

    stem = os.path.splitext(os.path.basename(path))[0]
    spool_dir = tempfile.mkdtemp(prefix="s98_pcm_")
    pcm_path = os.path.join(spool_dir, "0000.pcm")
    wav_path = os.path.join(spool_dir, "0000.wav")
    try:
        if cancel is not None and cancel.is_set():
            raise RuntimeError("已取消")
        prog(0.0, 1.0)
        r = decode_track(path, cancel=cancel, out_path=wav_path)
        if not r["ok"]:
            raise RuntimeError("%s：%s" % (os.path.basename(path), r["error"]))
        with open(pcm_path, "wb") as f:
            f.write(r["pcm"])

        name = stem
        try:
            ttl = _safe_name(cf.probe(path).title)
        except Exception:                               # noqa: BLE001
            ttl = ""
        if ttl and ttl.lower() not in stem.lower():
            name = "%s_%s" % (stem, ttl)

        prog(1.0, 1.0)
        return [{"name": name,
                 "pcm": core._SpoolPcm(pcm_path, spool_dir),
                 "little_endian": True,
                 "seconds": r["seconds"]}]
    except Exception:
        _rmtree_soft(spool_dir)
        raise
    finally:
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except OSError:
            pass


def available():
    """给界面用：S98 外挂解码现在能不能用。返回 ``(bool, 原因/路径)``。"""
    return decoder_ready()


def status(force=False):
    """给 ``/api/diag`` 用的一句话。"""
    del force
    ok, why = decoder_ready()
    return {"ok": ok, "detail": why,
            "exts": list(S98_EXTS),
            "decoder": os.path.basename(why) if ok else ""}
