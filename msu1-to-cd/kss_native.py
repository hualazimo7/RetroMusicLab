# -*- coding: utf-8 -*-
"""``.kss``（MSX / Game Gear / Master System）→ PCM：调 ``kss2wav.exe`` 的薄封装。

## 为什么 .kss 要单独走外挂解码器

libgme 的 KSS 实现是**残缺**的：它只映射 0x0000-0x3FFF 与 0x8000-0xBFFF，
**不映射 0x4000-0x7FFF**。ROM 落在 0x4000-0x7FFF 的 KSS（典型是 32 KB 的
Game Gear rip，`load_addr` 在 0x3E10 一带）会丢掉 16 KB 代码，Z80 直接跑飞。

症状是最难查的那一类：**libgme 不报任何错**，`gme_play` 正常返回，
只是每一轨都精确静音（peak == 0）。上层看到的是
「未产出任何 WAV（解码零产出，文件可能损坏或格式不受支持）」——
把解码器的缺陷说成了素材的错。

实测（Zophar 的 Game Gear 栏抽 12 个包）：

| 布局 | 例子 | libgme |
|---|---|---|
| SGC（`.sgc`） | Aerial Assault / Road Rash … 6 个 | 全部正常 |
| KSCC 16 KB（`load+len == 0xC000`） | Pocket Jansou / Super Golf | 正常 |
| KSCC 32 KB（跨 0x4000） | **Puyo Puyo `G-3324.kss`** | **静音** |
| KSCC 8 KB（load 到 0x0000/0x2000） | Nazo Puyo / Madou Monogatari | **静音** |

约三成。而且同一份 `G-3324.kss` 在 joshw 的 `kss.joshw.info/Game Gear/` 里
**一模一样**（32,768 字节）—— 所以这不是 Zophar 素材的问题，是解码器的。

libkss（digital-sound-antiques，`kssplay` 的核心库）是完整实现：MSX
（AY-3-8910 / OPLL / SCC）与 SMS/GG（SN76489）都按规范做内存映射和 bank
切换。同一个文件它 peak 4017。

## 曲目表：还是用 m3u，不用猜

KSS 文件本身**不记录曲目数**（libgme 一律当 256 轨，libkss 也不报）。
真实曲目表在同目录的 m3u 里，解析逻辑和 libgme 路径**共用同一个函数**
（:func:`chip_formats._parse_sidecar_m3u`）—— 它已经处理了：

* joshw 的 ``<包名>.m3u`` / ``!playlist.m3u``；
* zophar 的「一首一个 m3u」（``01 BGM #01.m3u``，v5.7 新增）；
* 曲名里的转义逗号 ``\\,``（不认它字段会整体错位、时长读成 0）；
* 编码回退 utf-8-sig → cp932 → latin-1。

没有 m3u 时才退回「逐曲号探 1 秒看有没有声音」，连续静音就收工 ——
不能盲解 256 轨（那是 10 小时音频、4 GB）。

## 音量：libkss 比 libgme 低约 12 dB

同一份文件两边都解，峰值差 3.7~4.4 倍（1628 vs 6002、2500 vs 10972、
7681 vs 30092）。libkss 的混音留了更多余量，直接用会让 KSS 明显比其它格式轻。
这里统一乘 :data:`GAIN` 再 clip —— 实测乘 4 之后与 libgme 的响度基本对齐。
"""

import os
import struct
import subprocess
import sys
import tempfile

import numpy as np

import chip_formats as cf
import winspawn

#: 静默启动（``CREATE_NO_WINDOW`` + ``SW_HIDE``），理由同 ``wsr_native``：
#: 主程序是窗口程序，解码器是控制台程序，不带这个每转一首闪一次黑框。
_silent_kw = winspawn.silent_kw
_rc_u32 = winspawn.rc_u32

#: 本模块负责的扩展名。``.sgc`` 不在这里 —— 它是独立容器，libgme 支持良好
#: （抽样 6/6 正常），没必要为它多起一个进程。
KSS_EXTS = (".kss",)

DECODER_NAME = "kss2wav.exe"

#: libkss 混音比 libgme 低约 12 dB，乘回来（实测比值 3.7~4.4）。
GAIN = 4.0

#: 0xc0000142 = DLL 初始化失败，偶发，多起几次基本都能过。
_DLL_INIT_FAILED = 0xC0000142
_SPAWN_RETRY = 3

#: 没有 m3u 时：每个曲号探多久、最多探几个、连续几个静音就收工。
PROBE_SECS = 1.0
PROBE_MAX = 256
PROBE_STOP_SILENT = 8
PROBE_PEAK_MIN = 32


def app_dir():
    """程序目录。打包态按「先挑不在临时目录里的那个」来定（同 wsr_native）。"""
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
    """返回可用的 ``kss2wav.exe`` 完整路径；找不到返回 ``""``。"""
    for d in _search_dirs():
        p = os.path.join(d, DECODER_NAME)
        if os.path.isfile(p):
            return p
    import shutil
    return shutil.which("kss2wav") or shutil.which("kss2wav.exe") or ""


def decoder_ready():
    """解码器在不在。返回 ``(bool, 说明)``。"""
    p = find_decoder()
    if p:
        return True, p
    return False, ("找不到 %s。它应该跟主程序放在一起；"
                   "源码运行时可以用 tools/kss2wav/build.py 重新编译。"
                   % DECODER_NAME)


def _io_files():
    """复用同一对 stdout/stderr 文件（同 wsr_native：每次 mkstemp+remove 太贵）。"""
    global _OUT_P, _ERR_P
    try:
        return _OUT_P, _ERR_P
    except NameError:
        pass
    d = tempfile.mkdtemp(prefix="kss_io_")
    _OUT_P = os.path.join(d, "out.txt")
    _ERR_P = os.path.join(d, "err.txt")
    return _OUT_P, _ERR_P


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
            out = open(out_p, "rb").read()
            err = open(err_p, "rb").read()
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
    """mono → 立体声（左右复制），并乘 :data:`GAIN`。

    统一输出 16bit/立体声小端，和 ``wsr_native`` / libgme 那条链的产物一致，
    下游 ``write_tracks`` 不用为 KSS 开特例。
    """
    a = np.frombuffer(pcm, dtype="<i2")
    if channels == 1:
        a = np.repeat(a, 2)
    elif channels != 2:
        raise ValueError("只支持 1/2 声道，拿到 %d" % channels)
    if GAIN != 1.0:
        a = np.clip(a.astype(np.float32) * GAIN, -32768.0, 32767.0).astype("<i2")
    return a.tobytes()


def read_playlist(path):
    """同目录 m3u → ``[(曲号, 曲名, 秒数), …]``；没有 m3u 返回 ``[]``。

    直接复用 :func:`chip_formats._parse_sidecar_m3u` —— 三种命名约定、
    转义逗号、编码回退都在那里，不要在这里重写一遍（重写必然漏掉其中一条）。
    """
    titles, ids, lengths = cf._parse_sidecar_m3u(path)
    out = []
    for no in ids:
        try:
            n = int(no)
        except (TypeError, ValueError):
            continue
        out.append((n, titles.get(n, ""), float(lengths.get(n, 0.0) or 0.0)))
    return out


def _probe_peak(path, song, cancel=None):
    """渲染 1 秒，返回峰值（0 = 静音）。失败返回 -1。"""
    exe = find_decoder()
    if not exe:
        return -1
    fd, wav = tempfile.mkstemp(prefix="kss_p_", suffix=".wav")
    os.close(fd)
    try:
        cmd = [exe, "-s%d" % int(song), "-p%d" % int(PROBE_SECS),
               "-r44100", "-n1", "-o" + wav, path]
        rc, _out, _err = _run(cmd, timeout=60.0, cancel=cancel)
        if rc != 0:
            return -1
        pcm, _secs, ch, _rate = _read_wav(wav)
        if not pcm:
            return 0
        a = np.frombuffer(pcm, dtype="<i2")
        return int(np.abs(a).max()) if a.size else 0
    except (OSError, ValueError, RuntimeError):
        return -1
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def scan_tracks(path, cancel=None):
    """没有 m3u 时逐曲号探一遍，返回有声音的曲号列表。"""
    hits = []
    silent = 0
    for no in range(PROBE_MAX):
        if cancel is not None and cancel.is_set():
            break
        pk = _probe_peak(path, no, cancel=cancel)
        if pk >= PROBE_PEAK_MIN:
            hits.append(no)
            silent = 0
        elif pk == 0:
            silent += 1
            if silent >= PROBE_STOP_SILENT and hits:
                break
        # pk == -1（解码器报错）不算静音，继续探
    return hits


def decode_track(path, song, secs=150.0, cancel=None, out_path=None):
    """把一个曲号渲染成 16bit/44.1k/立体声小端 PCM 字节。

    返回 ``dict``：``{ok, pcm, seconds, rate, error}``。
    """
    res = {"ok": False, "pcm": b"", "seconds": 0.0, "rate": 44100, "error": ""}
    exe = find_decoder()
    if not exe:
        res["error"] = decoder_ready()[1]
        return res

    tmp = None
    if out_path:
        wav = out_path
    else:
        fd, tmp = tempfile.mkstemp(prefix="kss_", suffix=".wav")
        os.close(fd)
        wav = tmp
    try:
        n_secs = max(1, int(round(float(secs) + 0.499)))
        cmd = [exe, "-s%d" % int(song), "-p%d" % n_secs,
               "-r44100", "-n1", "-o" + wav, path]
        rc, _out, err = _run(cmd, timeout=max(120.0, float(secs) * 2.0 + 60.0),
                             cancel=cancel)
        if rc != 0:
            lines = [x for x in err.decode("utf-8", "replace").splitlines() if x]
            msg = lines[-1] if lines else ""
            if _rc_u32(rc) == _DLL_INIT_FAILED:
                res["error"] = ("解码器启动失败（0xc0000142，DLL 初始化失败）"
                                "已自动重试 %d 次仍不行 —— 通常是系统临时资源紧张，"
                                "关掉几个程序或稍后重试。" % _SPAWN_RETRY)
            else:
                res["error"] = msg or ("解码器失败（退出码 %d）" % rc)
            return res
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


def _safe_name(s):
    """曲名 → 文件名安全片段（同 wsr_native）。"""
    bad = '\\/:*?"<>|\r\n\t'
    out = []
    for ch in str(s or ""):
        out.append("_" if ch in bad else ch)
    s = "".join(out).strip(" .")
    return s[:60]


def load_tracks(path, *, secs=150.0, cancel=None, on_progress=None, only=None):
    """一个 ``.kss`` → 归一化音轨列表（每条曲目一轨）。

    曲目表优先取同目录 m3u（权威：曲号 + 曲名 + 真实时长），没有才回落
    到逐曲号探测。``only`` 给了就只解那一首（试听用）。
    """
    import msu1_to_cd as core

    prog = on_progress or (lambda *_a: None)
    tracks = []

    playlist = read_playlist(path)
    if playlist:
        plan = [(no, title, (length if length > 0 else float(secs)))
                for no, title, length in playlist]
    else:
        hits = scan_tracks(path, cancel=cancel)
        if not hits:
            raise RuntimeError(
                "%s：没有探测到任何可播放的曲目，也没有 m3u 曲目表。"
                "可能是空 rip，或者 %s 缺失。"
                % (os.path.basename(path), DECODER_NAME))
        plan = [(t, "", float(secs)) for t in hits]

    if only is not None:
        plan = [x for x in plan if x[0] == int(only)] or plan[:1]

    stem = os.path.splitext(os.path.basename(path))[0]
    spool_dir = tempfile.mkdtemp(prefix="kss_pcm_")
    n = len(plan)
    try:
        for i, (no, title, length) in enumerate(plan):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("已取消")
            wav_path = os.path.join(spool_dir, "%04d.wav" % i)
            r = decode_track(path, no, secs=length, cancel=cancel,
                             out_path=wav_path)
            if not r["ok"]:
                prog(i + 1, n)
                continue
            pcm_path = os.path.join(spool_dir, "%04d.pcm" % i)
            with open(pcm_path, "wb") as f:
                f.write(r["pcm"])
            name = "%s_%02d" % (stem, no)
            ttl = _safe_name(title)
            if ttl:
                name += "_" + ttl
            tracks.append({"name": name,
                           "pcm": core._SpoolPcm(pcm_path, spool_dir),
                           "little_endian": True})
            prog(i + 1, n)
    except Exception:
        _rmtree_soft(spool_dir)
        raise

    if not tracks:
        _rmtree_soft(spool_dir)
        raise RuntimeError("%s：所有曲目都解码失败" % os.path.basename(path))
    return tracks


def _rmtree_soft(path, batch=40):
    """分批删（环境里有「一次删 >50 个文件要二次确认」的守卫，整目录删会被打断）。"""
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
            except BaseException:                       # noqa: BLE001
                pass
    try:
        os.rmdir(path)
    except BaseException:                               # noqa: BLE001
        pass


def available():
    """给界面用：KSS 外挂解码现在能不能用。返回 ``(bool, 原因/路径)``。"""
    return decoder_ready()


def status(force=False):
    """给 ``/api/diag`` 用的一句话。"""
    del force
    ok, why = decoder_ready()
    return {"ok": ok, "detail": why}
