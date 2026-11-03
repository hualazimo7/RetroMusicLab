"""``.miniusf`` / ``.usf`` → PCM：调 ``usf2wav.exe`` 的薄封装。

## 和 ``psf_native`` 的关系

形状完全一样（``decode_pcm`` 返回同一份 ``{ok, pcm, kind, title, seconds,
error}`` 契约），所以 ``convert_job`` 里两条分支几乎可以照抄。区别只有两点：

1. **不需要 BIOS**。N64 没有 BIOS 这个概念，核里自带 PIF/CIC 的 HLE 引导。
   所以这里没有 ``find_bios`` / ``check_bios`` 那一整套。
2. 解码器是 ``usf2wav.exe``（源码 ``tools/usf2wav/``，psflib + lazyusf2 + zlib）。

## 为什么时长只能靠标签

USF 不是流式格式，文件里没有「总采样数」这种东西 —— 它是**状态快照**，
时长完全来自 ``length`` / ``fade`` 标签。所以：

* 有 ``length`` → 按标签渲染，再追加 ``fade`` 段做线性淡出
* 没有 ``length`` → ``usf2wav`` 兜底渲染 180 秒，并在 stderr 说明一句

实测 263 个真实文件里 258 个有 ``length``，剩下 5 个是 ``sparse01~05``
（本来就是空测试文件）。

## 正确性

和上游 lazyusf2 自带的 ``test/dumpresampled.c``（编成 ``refdump.exe``）
逐样本比对过：``vendor/_check_usf_ref.py``，判据是**最大差必须为 0**。
"""

import os
import shutil
import struct
import subprocess
import sys
import tempfile

import winspawn

#: 静默启动参数（``CREATE_NO_WINDOW`` + ``SW_HIDE``）。
#: ``usf2wav.exe`` 是控制台程序，本程序是窗口程序 —— 不带这个每转一首都会
#: 闪一次黑框，偶尔还会抢不到控制台直接 0xc0000142（README ⚠40/⚠42）。
_silent_kw = winspawn.silent_kw
#: 退出码转无符号 32 位（Python 在 Windows 上会给负数）
_rc_u32 = winspawn.rc_u32

#: 本模块负责的扩展名。``.usflib`` 是采样库，不单独成音源。
USF_EXTS = (".miniusf", ".usf")

DECODER_NAME = "usf2wav.exe"

#: 0xc0000142 = DLL 初始化失败。**偶发**的，多起几次基本都能过
#: （和 psf2wav 那边同一个根因，所以同一个对策）。
_DLL_INIT_FAILED = 0xC0000142
_SPAWN_RETRY = 3


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
    """可能放着 ``usf2wav.exe`` 的目录，按优先级。"""
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
    """返回可用的 ``usf2wav.exe`` 完整路径；找不到返回 ``""``。"""
    for d in _search_dirs():
        p = os.path.join(d, DECODER_NAME)
        if os.path.isfile(p):
            return p
    p = shutil.which("usf2wav") or shutil.which("usf2wav.exe")
    return p or ""


def decoder_ready():
    """解码器在不在。返回 ``(bool, 说明)``。"""
    p = find_decoder()
    if p:
        return True, p
    return False, ("找不到 %s。它应该跟主程序放在一起；"
                   "源码运行时可以用 tools/usf2wav/build.py 重新编译。"
                   % DECODER_NAME)


def _read_wav(path):
    """读 16-bit PCM 的 WAV，返回 ``(pcm_bytes, seconds, channels, rate)``。

    只处理 ``usf2wav`` 自己写出来的那种（PCM / 16bit），不做「猜格式」。
    """
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


def _text(b):
    try:
        return b.decode("utf-8", "replace").strip()
    except Exception:                                   # noqa: BLE001
        return ""


def _last_err(err):
    """从解码器 stderr 里挑最后一行有用的（``ERR …`` / ``NOTE …``）。"""
    lines = [ln.strip() for ln in _text(err).splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("ERR"):
            return ln[3:].strip()
    for ln in reversed(lines):
        if ln.startswith("WARN") or ln.startswith("NOTE"):
            return ln[4:].strip()
    return lines[-1] if lines else ""


def decode_pcm(path, cancel=None, on_progress=None, secs=0.0):
    """把一个 ``.miniusf`` / ``.usf`` 解成 16bit/44.1k/立体声小端 PCM 字节。

    返回 ``dict``：``{ok, pcm, kind, title, seconds, error}`` ——
    和 :func:`psf_native.decode_pcm` 同一份口径。

    ``secs`` 为 0 时用文件自带的 ``length`` + ``fade`` 标签。
    """
    res = {"ok": False, "pcm": b"", "kind": "USF", "title": "",
           "seconds": 0.0, "error": ""}

    src = str(path)
    res["title"] = os.path.splitext(os.path.basename(src))[0]

    exe = find_decoder()
    if not exe:
        res["error"] = decoder_ready()[1]
        return res

    if on_progress:
        on_progress(0.05)

    fd, tmp = tempfile.mkstemp(prefix="usf_", suffix=".wav")
    os.close(fd)
    try:
        cmd = [exe, src, tmp]
        if secs:
            cmd.append("%.3f" % float(secs))
        for _try in range(_SPAWN_RETRY):
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        **_silent_kw())
            except OSError as e:
                res["error"] = "启动解码器失败：%s" % e
                return res
            out, err = proc.communicate()
            rc = proc.returncode
            if _rc_u32(rc) == _DLL_INIT_FAILED and _try + 1 < _SPAWN_RETRY:
                continue
            break

        if cancel is not None and cancel.is_set():
            res["error"] = "已取消"
            return res

        if rc != 0:
            msg = _last_err(err)
            if _rc_u32(rc) == _DLL_INIT_FAILED:
                res["error"] = ("解码器启动失败（0xc0000142，DLL 初始化失败）"
                                "已自动重试 %d 次仍不行 —— 通常是系统临时资源紧张，"
                                "关掉几个程序或稍后重试；反复出现请重启本程序。"
                                % _SPAWN_RETRY)
            elif rc == 2:
                res["error"] = msg or ("不是有效的 USF，或者配套的 "
                                       ".usflib 找不到")
            elif rc == 3:
                res["error"] = msg or "解码完成但输出 0 个样本"
            elif rc == 4:
                res["error"] = msg or "解码器内存分配失败"
            else:
                res["error"] = msg or ("解码器失败（退出码 %d）" % rc)
            return res

        try:
            pcm, seconds, channels, rate = _read_wav(tmp)
        except (ValueError, OSError) as e:
            res["error"] = "解码器输出不可用：%s" % e
            return res

        if not pcm:
            res["error"] = "解码器说成功，但 WAV 里没有采样"
            return res

        res["ok"] = True
        res["pcm"] = pcm
        res["seconds"] = seconds
        res["kind"] = "USF %dch/%d" % (channels, rate)
        if on_progress:
            on_progress(1.0)
        return res
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def available():
    """给界面用：USF 功能现在能不能用。返回 ``(bool, 原因/解码器路径)``。"""
    return decoder_ready()


def status(force=False):
    """给 ``/api/diag`` 用的一句话。``force`` 只为和 psf_native 对齐。"""
    del force
    ok, why = decoder_ready()
    return {"ok": ok, "detail": why}
