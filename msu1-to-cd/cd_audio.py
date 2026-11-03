#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cd_audio.py — CD-DA 音轨（.ape / .flac / .wav / .ogg …）→ WAV（v4.30）。

======================================================================
为什么需要它
======================================================================
PCE-CD / MD-CD / Sega-CD 这些光盘机种的 rip 是**混装**的::

    KMCD3005.hes                  ← 芯片音源（libgme 解）
    KMCD3005.m3u                  ← 曲目表（HES 行 + .ape 行混排）
    KMCD3005_04 Overture.ape      ← CD-DA 音轨（Red Book，本模块解）

CD 音轨**是游戏音源的一部分**（很多 BGM/主题曲只在 CD 轨上，芯片音源里没有），
所以必须一起转。区别只在解码器：芯片音源走 libgme，CD 音轨走 **PyAV(FFmpeg)**。

======================================================================
三条硬约束
======================================================================

1. **输出格式必须与芯片音源完全一致**：44.1kHz / 16bit / 立体声。
   MSU-1 与 CD-DA 都不接受别的采样率；用 swresample 一次到位（不自己写重采样）。

2. **不能过 float32 再落盘**：``AudioResampler(format="s16")`` 直接出 int16，
   保证「解码 → 落盘」只有一次量化。走 float 会引入 dither，同一份音源
   两次转出来的字节就不一样了（云盘 MD5 去重会失效）。

3. **取消要立刻生效**：APE 是整轨解码，一首 3 分钟的歌 0.4 秒能解完，
   但长轨（10 分钟以上）要几秒 —— 必须在帧循环里查 cancel，并删掉半成品。

======================================================================
命名 / 编号
======================================================================
与 ``spc_wav.write_tracks`` **同一套规则**：``NN_标题.wav``，``used`` 集合
跨文件共享做同名避让（同一个 WAV 目录里芯片音源与 CD 音轨编号连续）。
标题从文件名剥掉 ``<包名>_NN `` 前缀：``KMCD3005_04 Overture.ape`` → ``Overture``。
"""

from __future__ import annotations

import os
import re
import wave
from typing import Callable, Optional, Set

__all__ = ["CD_EXTS", "is_cd_audio", "available", "probe", "convert_one"]

#: 算作「CD 音轨」的扩展名。**不含 .mp3** —— 那是 MSU-1 源，不是 CD-DA 抓轨；
#: 也不含 .cue/.bin（那是整张镜像，不是单轨）。
CD_EXTS = (".ape", ".flac", ".wav", ".ogg", ".m4a", ".wv", ".tta")

#: 输出格式（与 spc_wav 完全一致）
OUT_RATE = 44100
OUT_CH = 2
OUT_WIDTH = 2

#: 每帧最多攒多少样本再落盘（防长轨把内存吃满）
_FLUSH_SAMPLES = OUT_RATE * 30

#: 从文件名剥曲目编号：``KMCD3005_04 Overture`` / ``04 - Overture`` / ``04.``
_NUM_PREFIX = re.compile(r"^(?:[A-Za-z][A-Za-z0-9]*[_-])?\d{2,3}[\s._-]+(.*)$")


def is_cd_audio(path) -> bool:
    """扩展名是否属于 CD 音轨。只看扩展名，不读盘。"""
    try:
        return os.path.splitext(str(path or ""))[1].lower() in CD_EXTS
    except (TypeError, ValueError):
        return False


def available() -> bool:
    """PyAV 是否可用（缺了就只能报错，不能假装成功）。"""
    try:
        import av  # noqa: F401
    except Exception:                                # noqa: BLE001
        return False
    return True


def _av():
    import av
    return av


def _title_of(path) -> str:
    """``KMCD3005_04 Overture.ape`` -> ``Overture``（剥不掉就用原名）。"""
    import msu1_to_cd as core
    stem = os.path.splitext(os.path.basename(str(path or "")))[0]
    m = _NUM_PREFIX.match(stem)
    name = (m.group(1) if m and m.group(1).strip() else stem).strip()
    return core.safe_filename(name, stem or "track")


def probe(path) -> dict:
    """CD 音轨的时长/声道/编码。**永不抛异常**（与 chip_formats.probe 同契约）。"""
    out = {"ok": False, "secs": 0.0, "rate": 0, "channels": 0,
           "codec": "", "title": _title_of(path), "error": ""}
    if not available():
        out["error"] = "缺少 PyAV(FFmpeg)，无法解码 CD 音轨"
        return out
    try:
        av = _av()
        with av.open(str(path)) as c:
            if not c.streams.audio:
                out["error"] = "文件里没有音频流"
                return out
            st = c.streams.audio[0]
            out["rate"] = int(st.rate or 0)
            out["channels"] = int(getattr(st, "channels", 0) or 0)
            out["codec"] = str(getattr(st.codec_context, "name", "") or "")
            dur = 0.0
            try:
                if st.duration and st.time_base:
                    dur = float(st.duration * st.time_base)
                elif c.duration:
                    dur = float(c.duration) / float(av.time_base)
            except Exception:                        # noqa: BLE001
                dur = 0.0
            out["secs"] = max(0.0, dur)
            out["ok"] = True
    except Exception as exc:                         # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
    return out


def convert_one(src, out_dir, *, used: Optional[Set[str]] = None,
                start_idx: int = 0, max_secs: float = 0.0,
                cancel=None, on_log: Optional[Callable[[str], None]] = None,
                on_progress: Optional[Callable[..., None]] = None) -> dict:
    """解一首 CD 音轨 -> ``NN_标题.wav``（44.1k/16bit/2ch）。

    返回结构与 :func:`spc_wav.convert` **同形**（``ok/fail/files/seconds/
    errors/last_idx``），这样 ``dl_convert`` 的记账逻辑一行都不用改。

    Args:
        max_secs: 单曲上限（0 = 不限制）。与芯片音源同一个旋钮，超了截断 + 淡出。
        used: 跨文件共享的同名避让集合（**必须传同一个 set**，否则跨包覆盖）。
    """
    log = on_log or (lambda _t: None)
    prog = on_progress or (lambda *_a: None)
    stat = {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
            "errors": [], "last_idx": int(start_idx or 0)}
    src = str(src or "")
    if not src or not os.path.isfile(src):
        stat["errors"] = ["源文件不存在"]
        return stat
    if not available():
        stat["errors"] = ["缺少 PyAV(FFmpeg)，无法解码 CD 音轨"]
        return stat

    import msu1_to_cd as core
    os.makedirs(out_dir, exist_ok=True)
    used = set() if used is None else used
    idx = int(start_idx or 0) + 1
    title = _title_of(src)

    # ---- 目标文件名（与 write_tracks 同规则：NN_名字.wav + 同名避让）----
    base = title
    name = base
    n = 2
    while name.lower() in used:
        name = "%s (%d)" % (base, n)
        n += 1
    final = os.path.join(out_dir, "%02d_%s.wav" % (idx, name))
    part = final + ".part"

    try:
        max_secs = max(0.0, float(max_secs or 0.0))
    except (TypeError, ValueError):
        max_secs = 0.0

    av = _av()
    written = 0
    try:
        with av.open(src) as container:
            if not container.streams.audio:
                raise RuntimeError("文件里没有音频流")
            st = container.streams.audio[0]
            # ⚠ 直接出 s16（int16），不经 float —— 见模块头「硬约束 2」。
            res = av.AudioResampler(format="s16", layout="stereo", rate=OUT_RATE)
            with wave.open(part, "wb") as w:
                w.setnchannels(OUT_CH)
                w.setsampwidth(OUT_WIDTH)
                w.setframerate(OUT_RATE)
                buf = bytearray()
                limit = int(max_secs * OUT_RATE) if max_secs > 0 else 0

                def _push(frames):
                    nonlocal written
                    for rf in frames:
                        # ⚠ 必须按 rf.samples 切片：PyAV 的 plane 缓冲区带对齐
                        #   填充，直接 bytes() 会多出尾部几十毫秒的脏样本
                        #   （实测 78.9s 的轨道写出来 79.4s）。
                        raw = bytes(rf.planes[0])[:rf.samples * OUT_CH * OUT_WIDTH]
                        if limit and written + len(raw) // (OUT_CH * OUT_WIDTH) > limit:
                            keep = (limit - written) * OUT_CH * OUT_WIDTH
                            if keep > 0:
                                buf.extend(raw[:keep])
                            written = limit
                            return True
                        buf.extend(raw)
                        written += len(raw) // (OUT_CH * OUT_WIDTH)
                    return False

                stop = False
                for fr in container.decode(st):
                    if cancel is not None and cancel.is_set():
                        raise _Cancelled()
                    if _push(res.resample(fr)) or (limit and written >= limit):
                        stop = True
                        break
                    if len(buf) >= _FLUSH_SAMPLES * OUT_CH * OUT_WIDTH:
                        w.writeframes(bytes(buf))
                        buf.clear()
                        prog(written / float(OUT_RATE))
                if not stop:
                    _push(res.resample(None))       # 冲刷重采样器尾巴
                if buf:
                    w.writeframes(bytes(buf))
        if cancel is not None and cancel.is_set():
            raise _Cancelled()

        # 截断时补 1.5s 淡出（与芯片音源 `chip_decode` 同一手感）
        if limit and written >= limit:
            _fade_file(part, core, OUT_RATE)
        os.replace(part, final)
    except _Cancelled:
        _unlink(part)
        stat["fail"] = 0                       # 取消不算失败（与 chip_decode 一致）
        stat["errors"] = []
        return stat
    except Exception as exc:                    # noqa: BLE001
        _unlink(part)
        msg = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        stat["errors"] = [msg]
        log("CD 音轨失败 %s — %s" % (os.path.basename(src), msg))
        return stat
    finally:
        pass

    secs = written / float(OUT_RATE)
    used.add(name.lower())
    stat.update({"ok": 1, "fail": 0, "files": [final], "seconds": secs,
                 "last_idx": idx, "errors": []})
    log("  WAV CD: %s  %d:%04.1f  %.1fMB"
        % (os.path.basename(final), int(secs // 60), secs % 60,
           os.path.getsize(final) / 1048576.0))
    prog(secs)
    return stat


class _Cancelled(Exception):
    """用户点了停止 / 本文件超时。**不计入失败**。"""


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _fade_file(path, core, rate):
    """把已落盘的 WAV 末尾 1.5 秒做淡出（原地重写）。

    只用于「被 max_secs 截断」的场景 —— 不截断就不该动音频，一个字节都不改。
    """
    try:
        with wave.open(path, "rb") as w:
            nch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
        pcm = core._apply_fade(raw, 1500, sr)
        with wave.open(path, "wb") as w:
            w.setnchannels(nch)
            w.setsampwidth(sw)
            w.setframerate(sr)
            w.writeframes(pcm)
    except Exception:                            # noqa: BLE001
        pass                                     # 淡出失败不影响音频有效性
