#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vgm_audio.py — 调 ``vgmstream-cli.exe`` 解 FFmpeg 认不出的容器（v5.9.12）。

## 为什么需要它

这一批新机种里绝大多数是**预录流**，FFmpeg 一行 ``av.open()`` 就解
（见 ``stream_audio.PYAV_KINDS``）。但有两族 FFmpeg **明确不支持**：

* **Audiokinetic Wwise**（``.wem``）—— PS4 的 224 个 BGM。
  ``fmt`` 的 wFormatTag 是 **0xFFFF**，FFmpeg 的 RIFF 解析器不认这个 tag，
  于是 ``av.open()`` 能打开、``streams.audio[0].codec_context`` 却是 ``None``。
  它的编码是 Wwise 自制的 **Custom Vorbis**：不是 Ogg 封装，
  **连 ``\\x05vorbis`` setup 头都没有** —— 整个文件里搜不到 "vorbis"
  这六个字母（实测 224 个全是如此），codebook 是靠几十 KB 的模板表在
  解码器里**重建**出来的。
* **FMOD FSB5**（``.fsb``）—— Wii U 的 12 个。FFmpeg 认得 FSB5 容器，
  但里面的 Nintendo DSP ADPCM 直接回
  ``PatchWelcomeError: Not yet implemented in FFmpeg, patches welcome``。

这两族 ``vgmstream`` 全都实现了（Wwise Vorbis 就是它的 ``vorbis_custom``
模块，即 ww2ogg 的 C 移植）。把那份 codebook 重建逻辑抄成 Python 不现实
（``vorbis_custom_utils_wwise.c`` 光逻辑就有 550~600 行，还依赖一张预编译的
codebook 表）—— 所以这里**随包带**一份 ``vgmstream-cli.exe``。

## 为什么是「随包带」而不是自己编

工程里其它外挂解码器（``psf2wav`` / ``usf2wav`` / ``vio2wav`` / ``ssf2wav``
/ ``gsf2wav`` / ``wsr2wav`` / ``kss2wav`` / ``qsf2wav``）都是 ``zig cc``
从源码编出来的，因为它们各自只有一个 C 文件。vgmstream 是几十万行的项目，
自己编不划算；而它是 **ISC 许可**（见 ``vgmstream-COPYING.txt``），
允许随包分发，义务只有一条：附上版权声明。

## 它带 11 个 DLL，缺一不可

``vgmstream-cli.exe`` 是**动态链接**的，而且那些 DLL 是**载入时**解析的
（不是延迟加载）—— 少任何一个都直接 ``exit 127``，报
``error while loading shared libraries: xxx.dll``。实测这 11 个缺一不可::

    avcodec-vgmstream-59.dll    avformat-vgmstream-59.dll
    avutil-vgmstream-57.dll     swresample-vgmstream-4.dll
    libatrac9.dll   libcelt-0061.dll  libcelt-0110.dll
    libg719_decode.dll  libmpg123-0.dll  libspeex-1.dll  libvorbis.dll

⚠ 别想着「我只用 Vorbis，把 avcodec 那几个删了能省 6 MB」——
  实测删掉就是 127，程序起都起不来（``avutil`` 是第一个报的）。

## 输出口径

``vgmstream-cli`` 只能按**原生采样率/声道**出 WAV（没有重采样选项），
所以 :func:`decode_pcm` 返回的是**原生**的 ``(int16 交错, rate, nch)``，
重采样交给 ``stream_audio._resample_to_out`` —— 和其它原生解码器一致。
Wwise 的 4 声道（FL FR BL BR，实测 195/224 个是这种）由 SWR 按 ``quad``
布局降混到立体声。

⚠ ``.bnk``（Wwise SoundBank）**不在这里**：它是容器不是曲目，
实测 vgmstream 也打不开（``failed opening``），见 ``dl_convert.NON_AUDIO_EXTS``。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave

import numpy as np

import winspawn

#: 本模块负责的扩展名。``.bnk`` 是容器、``.btsnd`` 走原生（见 stream_audio），
#: 都不在这里。
EXTS = (".wem", ".fsb")

DECODER_NAME = "vgmstream-cli.exe"

#: 0xc0000142 = DLL 初始化失败。**偶发**的，多起几次基本都能过
#: （和 psf2wav / usf2wav 那边同一个根因，所以同一个对策）。
_DLL_INIT_FAILED = 0xC0000142
_SPAWN_RETRY = 3

#: 闪一次黑框的启动参数（``CREATE_NO_WINDOW`` + ``SW_HIDE``）。
#: ``vgmstream-cli.exe`` 是控制台程序，本程序是窗口程序 —— 不带这个
#: 每转一首都会闪一次黑框（README ⚠40/⚠42）。
_silent_kw = winspawn.silent_kw
_rc_u32 = winspawn.rc_u32


def app_dir():
    """程序目录。打包态按「先挑不在临时目录里的那个」来定。"""
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
    """可能放着 ``vgmstream-cli.exe`` 的目录，按优先级。"""
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
    """返回可用的 ``vgmstream-cli.exe`` 完整路径；找不到返回 ``""``。"""
    for d in _search_dirs():
        p = os.path.join(d, DECODER_NAME)
        if os.path.isfile(p):
            return p
    return shutil.which("vgmstream-cli") or shutil.which("vgmstream-cli.exe") or ""


def decoder_ready():
    """解码器在不在。返回 ``(bool, 说明)``。"""
    p = find_decoder()
    if p:
        return True, p
    return False, ("找不到 %s（它要和 11 个 DLL 一起放在主程序旁边）。"
                   "缺了它 .wem / .fsb 转不了。" % DECODER_NAME)


def available():
    """给界面自检用。返回 ``(bool, 解码器路径或原因)``。"""
    return decoder_ready()


def _run(args, timeout=None):
    """跑一次解码器，返回 ``(rc, stdout, stderr)``。DLL 初始化失败会自动重试。"""
    rc, out, err = -1, b"", b""
    for _try in range(_SPAWN_RETRY):
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, **_silent_kw())
        except OSError as e:
            return -1, b"", ("启动解码器失败：%s" % e).encode("utf-8", "replace")
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
        if _rc_u32(rc) == _DLL_INIT_FAILED and _try + 1 < _SPAWN_RETRY:
            continue
        break
    return rc, out, err


def _last_err(err):
    """从 stderr 里挑一句最有用的当错误文案。"""
    txt = (err or b"").decode("utf-8", "replace").strip()
    if not txt:
        return ""
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    for ln in lines:
        low = ln.lower()
        if "error" in low or "fail" in low or "unsupported" in low:
            return ln[:200]
    return lines[-1][:200]


def _lp(path):
    """Windows 长路径（超过 MAX_PATH 时加 ``\\\\?\\`` 前缀）。

    v5.9.12：PS4 的 AC4 包里有 38 个 ``.txtp`` 的完整路径 263~315 字符。
    ``os.path.isfile`` 对它们返回 **False**（不抛异常），vgmstream 也打不开。
    实测加了 ``\\\\?\\`` 之后 Python 的 ``open`` 和 vgmstream 都正常
    （两边都验过：probe 返回 4ch/48000/Custom Vorbis）。

    这里做 try 是为了让 vgm_audio 能单独跑（单元测试 / 手工试解码器时
    不必把整个 msu1_to_cd 拖进来）。
    """
    try:
        import msu1_to_cd as core
        return core.long_path(path)
    except Exception:                            # noqa: BLE001
        return str(path or "")


def probe(path, subsong=0):
    """读元信息（走 ``-I``，只解析不解码）。**永不抛异常**。

    Returns:
        ``{ok, secs, rate, channels, codec, subsongs, encoding, error}``
    """
    out = {"ok": False, "secs": 0.0, "rate": 0, "channels": 0, "codec": "",
           "subsongs": 0, "encoding": "", "error": ""}
    src = str(path or "")
    if not src or not os.path.isfile(_lp(src)):
        out["error"] = "文件不存在"
        return out
    exe = find_decoder()
    if not exe:
        out["error"] = decoder_ready()[1]
        return out

    args = [exe, "-I"]
    if subsong:
        args += ["-s", str(int(subsong))]
    args.append(_lp(src))
    try:
        rc, stdout, stderr = _run(args, timeout=60)
    except subprocess.TimeoutExpired:
        out["error"] = "读取元信息超时"
        return out
    if rc != 0:
        out["error"] = _last_err(stderr) or ("解码器失败（退出码 %d）" % rc)
        return out
    try:
        j = json.loads(stdout.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        out["error"] = "解码器输出的元信息不是合法 JSON"
        return out

    rate = int(j.get("sampleRate") or 0)
    # ⚠ 时长取哪个字段，**两种 txtp 各说各话**（v5.9.12 实测，两个都踩过）：
    #
    #   ``numberOfSamples`` = 素材全长；``playSamples`` = 「会被播出来的样本数」。
    #   对裸 ``.wem`` / ``.fsb`` 两者相等，对 txtp 就看它是哪一种：
    #
    #   ① **flat / segmented**（裁一段、或者拼几段）→ 要用 ``playSamples``。
    #      实测 AC4 的 0318：txtp 里写 ``#b 69.14285714285721``，
    #      numberOfSamples=3373715（70.29s）是素材全长，
    #      playSamples=3318857（69.14s）才等于真正解出来的长度。
    #      差 1.15 秒 —— 这个差会直接进循环点计算（MSU-1 靠时长推 loop），
    #      所以必须先取对。
    #
    #   ② **layered**（同一段裁成两段叠起来）→ 要用 ``numberOfSamples``。
    #      实测 AC4 的 0599：``layout = layered (2 layers)``，
    #      numberOfSamples=3396033（70.75s）= 输出长度，
    #      而 playSamples=7272066（151.50s）= **两层之和**，根本不是时长。
    #
    #   统一判据（两种都覆盖，且不需要解析 layout 字符串）：
    #   ``playSamples`` 只在 **不超过** ``numberOfSamples`` 时才可信。
    #   分层时它必然 > numberOfSamples（和 ≥ 最大值），自动落回后者。
    _total = int(j.get("numberOfSamples") or 0)
    _play = int(j.get("playSamples") or 0)
    samples = _play if 0 < _play <= _total else _total
    out.update({
        "ok": True,
        "rate": rate,
        "channels": int(j.get("channels") or 0),
        "secs": (samples / float(rate)) if rate > 0 else 0.0,
        "codec": str(j.get("encoding") or ""),
        "encoding": str(j.get("metadataSource") or ""),
        "subsongs": int((j.get("streamInfo") or {}).get("total") or 0),
    })
    return out


def _read_wav(path):
    """读 WAV，返回 ``(pcm_bytes, seconds, channels, rate)``。

    vgmstream 出的是**标准 PCM16 WAV**（format tag = 1，实测 4 声道也是），
    所以直接用标准库 ``wave`` 就够，不需要自己解 RIFF。
    """
    with wave.open(path, "rb") as w:
        nch = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        frames = w.getnframes()
        pcm = w.readframes(frames)
    if width != 2:
        raise ValueError("解码器输出的不是 16bit（%d 字节/样本）" % width)
    secs = (frames / float(rate)) if rate > 0 else 0.0
    return pcm, secs, nch, rate


def decode_pcm(path, subsong=0, cancel=None, on_progress=None):
    """解成**原生** ``(int16 交错 bytes, rate, nch)``。重采样由调用方做。

    返回 ``dict``：``{ok, pcm, rate, channels, seconds, kind, error}``。

    ⚠ 一定要带 ``-i``（ignore looping）。不带的话 vgmstream 会按默认
      「循环 2 遍 + 淡出 10 秒」渲染 —— 一首 35 秒的 BGM 会变成 80 秒，
      而且尾巴是人工淡出。实测就是这么发现的。
    """
    res = {"ok": False, "pcm": b"", "rate": 0, "channels": 0,
           "seconds": 0.0, "kind": "", "error": ""}
    src = str(path or "")
    if not src or not os.path.isfile(_lp(src)):
        res["error"] = "文件不存在"
        return res
    exe = find_decoder()
    if not exe:
        res["error"] = decoder_ready()[1]
        return res

    if on_progress:
        on_progress(0.05)

    fd, tmp = tempfile.mkstemp(prefix="vgm_", suffix=".wav")
    os.close(fd)
    try:
        args = [exe, "-i", "-o", tmp]
        if subsong:
            args += ["-s", str(int(subsong))]
        args.append(_lp(src))
        try:
            rc, _stdout, stderr = _run(args)
        except subprocess.TimeoutExpired:
            res["error"] = "解码超时"
            return res

        if cancel is not None and cancel.is_set():
            res["error"] = "已取消"
            return res
        if rc != 0:
            if _rc_u32(rc) == _DLL_INIT_FAILED:
                res["error"] = ("解码器启动失败（0xc0000142，DLL 初始化失败）"
                                "已自动重试 %d 次仍不行 —— 通常是系统临时资源紧张，"
                                "关掉几个程序或稍后重试。" % _SPAWN_RETRY)
            else:
                res["error"] = _last_err(stderr) or ("解码器失败（退出码 %d）" % rc)
            return res

        try:
            pcm, secs, nch, rate = _read_wav(tmp)
        except (wave.Error, EOFError, ValueError, OSError) as e:
            res["error"] = "解码器输出不可用：%s" % e
            return res
        if not pcm:
            res["error"] = "解码器说成功，但 WAV 里没有采样"
            return res

        res.update({"ok": True, "pcm": pcm, "rate": rate, "channels": nch,
                    "seconds": secs, "kind": "vgmstream %dch/%d" % (nch, rate)})
        if on_progress:
            on_progress(1.0)
        return res
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def decode_out(path, subsong=0, cancel=None, on_progress=None):
    """解成 **44100 / 16bit / 立体声** 的裸 PCM 字节。

    给 ``psx_txtp`` 那条路用 —— 它要求「44100 立体声小端 PCM」，
    和其它 txtp 目标（``.xa`` / ``.DA`` / ``.psf``）同一口径。

    ⚠ 传进来的 ``path`` 可以是 ``.wem`` / ``.fsb``，**也可以是 ``.txtp``**
      —— PS4 的 ``.txtp`` 是 WWISER 生成的 Wwise 事件/序列定义
      （分层 + 循环 + 裁剪），vgmstream 会自己按它把多个 wem 拼成一首。
      这时必须把 **txtp 本身**交给 vgmstream，交给它指向的 wem 就只有一段了。
    """
    import stream_audio
    r = decode_pcm(path, subsong=subsong, cancel=cancel,
                   on_progress=on_progress)
    if not r["ok"]:
        raise RuntimeError(r["error"] or "vgmstream 解码失败")
    pcm = np.frombuffer(r["pcm"], dtype="<i2")
    return stream_audio._resample_to_out(pcm, r["rate"], r["channels"]).tobytes()
