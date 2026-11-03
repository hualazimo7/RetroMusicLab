# -*- coding: utf-8 -*-
"""``.qsf`` / ``.miniqsf`` → PCM：调 ``qsf2wav.exe``。

## 这个格式到底是什么

QSF 是 PSF 家族里给 **Capcom QSound** 用的那一支，容器版本字节 **0x41**
（PS1 = 0x01、PS2 = 0x02、Saturn = 0x11、DC = 0x12、N64 = 0x21、QSound = 0x41）。
覆盖 CPS1 / CPS2 / CP-ZN1 / CP-ZN2 上带 QSound 的街机板。

它和 PSF 的**唯一实质差异**在 program section：PSF 里放的是一段可执行程序，
QSF 里放的是一串**块**（每块 11 字节头 + 载荷）::

    char   id[3];        // "Z80" | "SMP" | "KEY"
    uint32 dataofs;      // 载荷写进该块缓冲区的偏移（小端）
    uint32 datasize;     // 载荷长度（小端）
    uint8  data[datasize];

* ``Z80``  Z80 程序 ROM（CPS2 常见 128K / 256K，靠端口 ``0xC000+`` 换 16K 页）
* ``SMP``  QSound 采样 ROM（16 位**大端** PCM，CPS2 常见 4MB / 8MB）
* ``KEY``  CPS1 的 Kabuki 解密密钥，正好 11 字节

一个包里两种文件：

* ``<游戏>.qsflib`` —— 完整 Z80 程序 + 全部采样，**全曲共用**
* ``NN - 曲名.miniqsf`` —— **12 字节**，只有一块 ``"Z80"``：
  ``dataofs=0x21`` / ``datasize=1`` / ``data=曲号``。
  它就是往 Z80 ROM 的 **0x21 这一个字节**里写曲号，驱动把那个字节当
  「当前曲目」。所以一首歌一个文件，45 首的差异**只有这一个字节**。

⚠ 曲号 ≠ 文件序号：实测 Mega Man 2 的第 5 首《Plant Man》是曲号 ``0x0E``。

## 为什么必须模拟

和 PSF 一样，QSF 里没有波形。要出声得跑 Z80 程序、由它驱动 QSound DSP：
16 个声道的 16 位 PCM 播放、ADPCM 回放、声像、回声。这不是解析器能干的活。
真正干活的是同级的 ``qsf2wav.exe``（源码 ``tools/qsf2wav/``，
psflib + highly_quixotic + zlib）。

## 不需要 BIOS

这一点比 PS1 的 PSF 简单得多：QSound 是街机板上的独立声音子系统，
没有「主机 BIOS」这个概念。所以本模块**没有** ``find_bios`` / ``set_bios``
那一套，也不该有人照抄 ``psf_native`` 把 BIOS 检查带过来。

## 采样率：原生 24038，出厂就出 44100

``qsound_clear_state()`` 里写死 ``qsound_set_rates(state, 8000000, 250, 24038)``，
而 ``device_start_qsound_ctr(60000000)`` 返回 ``60000000/2/1248 = 24038.46``
—— 这是 QSound DAC 的**硬件节拍**。混音器每输出一个样本就按这个时钟推进一次
DSP，所以**把数字改大只会让整首变调**，只能真重采样。

``qsf2wav.exe`` 内部用 64 抽头 Kaiser 加窗 sinc 多相 FIR 直接出 **44100**，
和 ``spc_wav.SR`` / ``gsf_native`` 的口径一致。所以这里**不用传任何采样率
参数** —— 主流水线拿到的就是 44100，``write_tracks`` 那层不需要特殊处理。

（``qsf2wav.exe --rate 24038`` 是研究用的 1:1 直通分支，本模块不用它。）

## 正确性

* 容器 / 块结构：``vendor/_probe_qsf.py``（22 条断言）逐字节核对过，
  含「11 字节头 + 载荷严丝合缝铺满整个 exe 段」的算术自洽验证。
* 全量实测：45/45 首成功、0 静音、时长与 ``length+fade`` 标签一致（±20ms）、
  同一首解两次逐字节相同。
* 重采样保真：``--rate 24038`` 与 ``--rate 44100`` 的 peak 值一致
  （44100 略高是 sinc 过冲的正常表现，不是增益错误）。

## 许可（分发时必须能提供源码）

``psflib`` = MIT；``highly_quixotic`` = **GPL-3.0**；``zlib`` = zlib 许可。
``qsf2wav.exe`` 作为**独立进程**被调用属于聚合，主程序不受传染；但分发这个
二进制时须能提供 ``tools/qsf2wav/`` 下的完整源码（已随仓库提供）。
"""

import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

import winspawn

__all__ = [
    "QSF_EXTS", "QSF_LIB_EXTS", "DECODER_NAME", "OUT_RATE", "SRC_RATE",
    "app_dir", "find_decoder", "decoder_ready", "decode_pcm",
    "available", "status", "length_tag",
]

#: 本模块负责的曲目扩展名
QSF_EXTS = (".qsf", ".miniqsf")
#: 配套库后缀（不是曲目，一个包里通常只有一个）
QSF_LIB_EXTS = (".qsflib",)

DECODER_NAME = "qsf2wav.exe"

#: 出厂输出采样率（主流水线的房子采样率）
OUT_RATE = 44100
#: QSound 的原生采样率 —— 只用于文档与门禁断言，绝不往解码器传
SRC_RATE = 24038

#: 静默启动参数（``CREATE_NO_WINDOW`` + ``SW_HIDE``）。
#: ``qsf2wav.exe`` 是控制台程序，本程序是窗口程序 —— 不带这个每转一首都会
#: 闪一次黑框，偶尔还会抢不到控制台直接 0xc0000142（README ⚠40/⚠42）。
_silent_kw = winspawn.silent_kw
#: 退出码转无符号 32 位（Python 在 Windows 上会给负数）
_rc_u32 = winspawn.rc_u32

#: 0xc0000142 = DLL 初始化失败。**偶发**的，多起几次基本都能过
#: （和 psf2wav / usf2wav / vio2wav / ssf2wav / gsf2wav 同一个根因，同一个对策）。
_DLL_INIT_FAILED = winspawn.DLL_INIT_FAILED
_SPAWN_RETRY = 3


def app_dir():
    """程序目录。打包态按「先挑不在临时目录里的那个」来定。

    不能只用 ``sys.executable`` 的目录：Nuitka onefile 会把它指到
    ``%TEMP%\\onefile_xxx``，那样找解码器就找错地方了。
    """
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
    """可能放着 ``qsf2wav.exe`` 的目录，按优先级。"""
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
    """返回可用的 ``qsf2wav.exe`` 完整路径；找不到返回 ``""``。"""
    for d in _search_dirs():
        p = os.path.join(d, DECODER_NAME)
        if os.path.isfile(p):
            return p
    p = shutil.which("qsf2wav") or shutil.which("qsf2wav.exe")
    return p or ""


def decoder_ready():
    """解码器在不在。返回 ``(bool, 说明)``。"""
    p = find_decoder()
    if p:
        return True, p
    return False, ("找不到 %s。它应该跟主程序放在一起；"
                   "源码运行时可以用 tools/qsf2wav/build.py 重新编译。"
                   % DECODER_NAME)


def _read_wav(path):
    """读 16-bit PCM 的 WAV，返回 ``(pcm_bytes, seconds, channels, rate)``。

    只处理 ``qsf2wav`` 自己写出来的那种（PCM / 16bit），不做「猜格式」。
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
    """把子进程输出转成 str（UTF-8 优先，退化到 GBK / latin-1）。"""
    if not b:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("latin-1", "replace")


def _last_err(err):
    """从解码器 stderr 里挑最后一条 ``ERR`` 行，没有就返回最后一行非空。"""
    lines = [ln.strip() for ln in _text(err).splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("ERR"):
            return ln[3:].strip()
    for ln in reversed(lines):
        if ln.startswith("WARN") or ln.startswith("NOTE"):
            return ln[4:].strip()
    return lines[-1] if lines else ""


def decode_pcm(path, cancel=None, on_progress=None, secs=0.0):
    """把一个 ``.qsf`` / ``.miniqsf`` 解成 16bit/44.1k/立体声小端 PCM 字节。

    返回 ``dict``：``{ok, pcm, kind, title, seconds, error}`` ——
    和 :func:`gsf_native.decode_pcm` / :func:`psf_native.decode_pcm`
    同一份口径，这样 QSF 才能和别的机种走同一条主流水线。

    ``secs`` 为 0 时用文件自带的 ``length`` + ``fade`` 标签（没有就按
    ``qsf2wav`` 的默认 180 秒）。

    ⚠ **不传采样率参数**：``qsf2wav.exe`` 出厂就重采样到 44100
    （见模块注释），主流水线的 ``write_tracks`` 恒写 44100，两边对得上。
    """
    res = {"ok": False, "pcm": b"", "kind": "QSF", "title": "",
           "seconds": 0.0, "error": ""}

    src = str(path)
    res["title"] = os.path.splitext(os.path.basename(src))[0]

    exe = find_decoder()
    if not exe:
        res["error"] = decoder_ready()[1]
        return res

    if on_progress:
        on_progress(0.05)

    fd, tmp = tempfile.mkstemp(prefix="qsf_", suffix=".wav")
    os.close(fd)
    try:
        # 位置参数顺序是 `qsf2wav <in> <out> [秒数]`，秒数必须在 --rate 之前
        # （argparse 手写版按顺序扫，见到 --rate 就只认采样率了）。
        cmd = [exe, src, tmp]
        if secs:
            cmd.append("%.3f" % float(secs))
        for _try in range(_SPAWN_RETRY):
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        **_silent_kw())
            except OSError as exc:
                res["error"] = "启动解码器失败：%s" % exc
                return res
            # 长曲子解一首要 1~3 秒，期间要能响应「停止」。和 psf_native 同款
            # 轮询 —— 直接 communicate() 会让停止按钮等到整首跑完才生效。
            while True:
                try:
                    out, err = proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set():
                        proc.kill()
                        proc.communicate()
                        res["error"] = "已取消"
                        return res
                    if on_progress:
                        on_progress(0.5)
            rc = proc.returncode
            if _rc_u32(rc) != _DLL_INIT_FAILED:
                break
            if _try + 1 < _SPAWN_RETRY:
                time.sleep(0.25 * (_try + 1))

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
            elif rc == 1:
                # 最常见的一类：同目录的 .qsflib 不在，或者 miniqsf 的补丁
                # 打不上。解码器自己会给出具体原因（"不是合法的 QSF" /
                # "找不到 _lib" 之类），优先原样透传。
                res["error"] = msg or ("不是有效的 QSF，或者同目录的 "
                                       ".qsflib 找不到")
            elif rc == 3:
                # 曲号没打对、或包里缺 SMP 块 —— 解码器已经把这两条写在
                # stderr 的第二行，msg 会带上。
                res["error"] = msg or "解码完成但输出全静音"
            else:
                res["error"] = msg or ("解码器失败（退出码 %d）" % rc)
            return res

        try:
            pcm, seconds, channels, rate = _read_wav(tmp)
        except (ValueError, OSError) as exc:
            res["error"] = "解码器输出不可用：%s" % exc
            return res

        if not pcm:
            res["error"] = "解码器说成功，但 WAV 里没有采样"
            return res

        res["ok"] = True
        res["pcm"] = pcm
        res["seconds"] = seconds
        res["kind"] = "%s %dch/%d" % (res["kind"], channels, rate)
        if on_progress:
            on_progress(1.0)
        return res
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def available():
    """给界面用：QSF 功能现在能不能用。返回 ``(bool, 原因/解码器路径)``。"""
    return decoder_ready()


def status(force=False):
    """给 ``/api/diag`` 用的一句话。``force`` 只为和 psf_native 对齐。"""
    del force
    ok, why = decoder_ready()
    return {"ok": ok, "detail": why}


# ---------------------------------------------------------------------------
# 标签
# ---------------------------------------------------------------------------

#: PSF 标签块里的 ``length=分:秒.小数``
_RE_LEN = re.compile(r"^length=(\d+):([0-9.]+)\s*$", re.M)


def length_tag(path):
    """读 QSF 自带的 ``length`` 标签（秒）。读不到返回 ``0.0``。

    QSF 的二进制负载后面跟一段 ``[TAG]`` 文本块，格式和 PSF 完全一样
    （``length=0:45`` 这种 ``分:秒``）。实测这个包 45 首**全部**带标签。

    ⚠ 和 ``psf_native.length_tag`` 一样**只读尾部 256 KB，绝不启动解码器**
    —— 下载页要按它估磁盘占用，逐文件起一次模拟器是不可接受的。
    """
    try:
        n = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, n - 262144))
            tail = f.read()
    except OSError:
        return 0.0
    i = tail.rfind(b"[TAG]")
    if i < 0:
        return 0.0
    m = _RE_LEN.search(tail[i:].decode("utf-8", "replace"))
    if not m:
        return 0.0
    try:
        return int(m.group(1)) * 60.0 + float(m.group(2))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# 命令行（没有界面时也能用）
# ---------------------------------------------------------------------------

def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(
        prog="qsf_native",
        description="QSF / miniqsf（Capcom QSound）→ WAV 的原生解码（不需要 BIOS）")
    ap.add_argument("--status", action="store_true", help="打印当前可用性")
    ap.add_argument("--decode", nargs=2, metavar=("QSF", "WAV"),
                    help="解码单个 QSF 到 WAV")
    ap.add_argument("--secs", type=float, default=0.0,
                    help="渲染秒数，0 = 用文件自带的 length")
    a = ap.parse_args(argv)

    if a.decode:
        src, dst = a.decode
        r = decode_pcm(src, secs=a.secs)
        if not r["ok"]:
            print("❌ %s" % r["error"])
            return 1
        with open(dst, "wb") as f:
            f.write(_wav_bytes(r["pcm"]))
        print("✅ %s  %.2f 秒 → %s" % (r["kind"], r["seconds"], dst))
        return 0

    ok, why = decoder_ready()
    print("解码器：%s" % (why if ok else "❌ %s" % why))
    print("可用性：%s" % ("✅ 可用" if ok else "❌ 不可用"))
    print("采样率：出厂 %d Hz（QSound 原生 %d Hz，已在解码器内重采样）"
          % (OUT_RATE, SRC_RATE))
    return 0 if ok else 1


def _wav_bytes(pcm, channels=2, rate=OUT_RATE, bits=16):
    """把 PCM 包成最小的 WAV（只给命令行 --decode 用）。"""
    import io
    n = len(pcm)
    hdr = io.BytesIO()
    hdr.write(b"RIFF")
    hdr.write(struct.pack("<I", 36 + n))
    hdr.write(b"WAVEfmt ")
    hdr.write(struct.pack("<IHHIIHH", 16, 1, channels, rate,
                          rate * channels * bits // 8, channels * bits // 8, bits))
    hdr.write(b"data")
    hdr.write(struct.pack("<I", n))
    return hdr.getvalue() + pcm


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
