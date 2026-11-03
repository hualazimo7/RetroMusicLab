"""``.wsr``（WonderSwan Sound Rip）→ PCM：调 ``wsr2wav.exe`` 的薄封装。

## 为什么需要外挂解码器

WSR 是「裸 WonderSwan ROM 镜像」，播放要靠 V30MZ（80186）+ WS 4 通道声卡
的完整模拟。本工程原有的 ``libgme.dll``（含 kode54 分支）**没有 WonderSwan
机种**，所以只能另找核。这里用的是 ``beetle-wswan-libretro``（mednafen 的
WonderSwan 核）编成的最小 libretro 前端 ``wsr2wav.exe``，源码在
``tools/wswan2wav/``。

## 曲号在 AX 寄存器里（这是全篇的关键）

WSR 规范（atwiki「soundfile」WSR 页）写明启动时：

    CS = 0xFFFF, IP = 0x0000, **AX = 演奏する曲データの番号**
    「通常のWonderSwanエミュレータでは設定されない」

也就是说曲号**不在文件里**，而是通过 AX 寄存器传进去。普通模拟器
（包括这个核）复位时把 AX 清 0，于是 ROM 的启动桩读到 0 号曲就什么都不放
—— 这就是「WSR 转出来全静音」的根因。反汇编 SWJ-BANC3C.wsr 的启动桩
（F000:FE00）可以印证：

    FA            cli
    8B D0         mov  dx, ax        ← 先把曲号存起来
    ...
    52            push dx            ← 曲号入栈
    9A DB 4E 00 F0   call far F000:4EDB   ← 驱动初始化
    58            pop  ax            ← 取回曲号
    3D 85 00      cmp  ax, 0x0085
    73 05         jae  skip          ← AX>=0x85 视为「不播」
    9A 42 50 00 F0   call far F000:5042   ← 播放曲号 AX

``wsr2wav.exe`` 在 ``retro_load_game`` 之后（核已 Reset 完、还没跑第一条
指令）调 ``v30mz_set_reg(NEC_AW, track)``，正好被 ROM 吃到。

## 曲目数是「实测」出来的

每个 rip 的驱动布局都不一样（分组表地址、bank 编号、通道槽位分配全不同），
硬解析不可靠 —— 实测 4 个 joshw 样本里只有 1 个能靠字节签名定位分组表。
所以这里用**音频实测**：``wsr2wav.exe --scan`` 逐个曲号跑 1 秒，报峰值，
峰值 >= 阈值就算「有曲」。扫描很快（约 120 倍实时），166 个曲号 8 秒内跑完。

结果和结构校验对得上：SWJ-BANC3C.wsr 的 122 首有声曲目，正好是分组表里
29 + 104 = 133 个条目中排掉 11 首空曲（全通道指向 ``00 00 00 00 ff ff``
的空数据）后的集合。
"""

import os
import shutil
import struct
import subprocess
import sys
import tempfile

import winspawn

#: 静默启动参数（``CREATE_NO_WINDOW`` + ``SW_HIDE``），理由同 ``vio_native``：
#: 本程序是窗口程序，解码器是控制台程序，不带这个每转一首闪一次黑框。
_silent_kw = winspawn.silent_kw
_rc_u32 = winspawn.rc_u32

#: 本模块负责的扩展名。
WSR_EXTS = (".wsr",)

DECODER_NAME = "wsr2wav.exe"

#: 0xc0000142 = DLL 初始化失败，偶发，多起几次基本都能过。
_DLL_INIT_FAILED = 0xC0000142
_SPAWN_RETRY = 3

#: 探针：每个曲号跑多久、最多探多少个、连续多少个静音就收工。
PROBE_SECS = 1.0
PROBE_MAX = 256
PROBE_STOP_SILENT = 24
#: 峰值低于这个就当静音。WS 声卡满量程 32767，实测空曲是精确的 0。
PROBE_PEAK_MIN = 32


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
    """返回可用的 ``wsr2wav.exe`` 完整路径；找不到返回 ``""``。"""
    for d in _search_dirs():
        p = os.path.join(d, DECODER_NAME)
        if os.path.isfile(p):
            return p
    p = shutil.which("wsr2wav") or shutil.which("wsr2wav.exe")
    return p or ""


def decoder_ready():
    """解码器在不在。返回 ``(bool, 说明)``。"""
    p = find_decoder()
    if p:
        return True, p
    return False, ("找不到 %s。它应该跟主程序放在一起；"
                   "源码运行时可以用 tools/wswan2wav/build.py 重新编译。"
                   % DECODER_NAME)


def read_footer(path):
    """读 WSR 尾部 32 字节头。

    布局（atwiki「soundfile」WSR 页）::

        0xFFFE0  4B  "WSRF"
        0xFFFE4  1B  Version
        0xFFFE5  1B  最初の曲番号（first song number）
        0xFFFE6 10B  予約領域
        0xFFFF0  5B  x86 スタートアップ（far JMP）
        0xFFFF5  1B  予約
        0xFFFF6  2B  メーカーコード
        0xFFFF8  1B  タイトルコード
        0xFFFF9  1B  バージョン
        0xFFFFA  1B  ROM容量
        0xFFFFB  1B  RAM容量/EEPROM容量
        0xFFFFC  1B  ROMのwait数
        0xFFFFD  1B  Sub System LSI
        0xFFFFE  2B  チェックサム

    不是 WSR（没有 ``WSRF``）时 ``ok=False``，但 ``first_song`` 仍给 0。
    """
    res = {"ok": False, "size": 0, "first_song": 0, "version": 0,
           "rom_cap": 0, "checksum": 0, "error": ""}
    try:
        size = os.path.getsize(path)
    except OSError as e:
        res["error"] = "读不到文件：%s" % e
        return res
    res["size"] = size
    if size < 64:
        res["error"] = "文件太小（%d 字节），不像 WSR" % size
        return res
    try:
        with open(path, "rb") as f:
            f.seek(size - 32)
            tail = f.read(32)
    except OSError as e:
        res["error"] = "读不到文件：%s" % e
        return res
    if len(tail) < 32 or tail[:4] != b"WSRF":
        res["error"] = "尾部没有 WSRF 标记，不是 WSR 文件"
        return res
    res["ok"] = True
    res["version"] = tail[4]
    res["first_song"] = tail[5]
    res["rom_cap"] = tail[0x1A]
    res["checksum"] = struct.unpack("<H", tail[0x1E:0x20])[0]
    return res


def _m3u_secs(s):
    """``"1:11"`` -> 71.0；``"1:02:03"`` -> 3723.0；空/异常 -> 0.0。"""
    s = (s or "").strip()
    if not s:
        return 0.0
    try:
        parts = s.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return (int(parts[0]) * 3600 + int(parts[1]) * 60
                    + float(parts[2]))
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def read_playlist(path):
    """读同目录的 m3u 曲目表，返回 ``[(track_no, title, secs), …]``。

    joshw 的 WonderSwan 包**一定带 m3u**（一包一个主表 + 每曲一个小表），
    格式和 KSS/SGC 那套一样::

        SWJ-BAN01A.wsr::WSR,0,BGM #01,1:52,,10

    字段：``文件名::格式,曲号,曲名,时长,fade,循环``。

    ⚠ **这是曲目数的权威来源，比实测扫描可靠得多。** 实测
    ``SWJ-BAN01A.wsr``（Card Captor Sakura）：m3u 只列 **33 首**，而「逐曲号
    跑 1 秒看有没有声音」的扫描会报 **121 首** —— 多出来的是噪声/残响。
    按 121 首 × 150 秒渲染 = 5 小时音频、2.1 GB，用户看到的就是「卡住不动」。

    没有 m3u 时返回空列表，调用方回落到扫描。
    """
    src = str(path or "")
    d = os.path.dirname(os.path.abspath(src))
    stem = os.path.splitext(os.path.basename(src))[0]
    out = []
    seen = set()
    for name in (stem + ".m3u", "!playlist.m3u"):
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "::" not in line:
                        continue
                    parts = line.split("::", 1)[1].split(",")
                    if len(parts) < 2:
                        continue
                    raw = parts[1].strip()
                    try:
                        no = (int(raw[1:], 16) if raw.startswith("$")
                              else int(raw, 10))
                    except (TypeError, ValueError):
                        continue
                    if no in seen:
                        continue
                    seen.add(no)
                    title = parts[2].strip() if len(parts) > 2 else ""
                    secs = _m3u_secs(parts[3]) if len(parts) > 3 else 0.0
                    out.append((no, title, secs))
        except OSError:
            continue
        if out:
            break
    return out


def _run(cmd, timeout=120.0, cancel=None):
    """跑解码器，返回 ``(rc, stdout, stderr)``。取消/超时抛 ``RuntimeError``。

    ⚠ **stdout/stderr 必须重定向到文件，不能用 PIPE**。用 PIPE 时只能在
    进程退出后 ``communicate()`` 才读到内容，而 Windows 的匿名管道缓冲区只有
    4 KB —— ``--scan`` 输出 256 行（约 5.6 KB）时子进程写满就阻塞，永远不退出，
    ``poll()`` 一直返回 None。实测症状：``probe``（157 行，2.8 KB）正常，
    ``load_tracks``（256 行）卡死 110 秒以上。``chip_decode._run_worker``
    踩的是同一个坑，用的是同一个对策。

    ⚠ 而且这两个文件是**进程内复用**的（模块级，不每次 mkstemp + remove）。
    一整个 WSR 要跑 120 多次解码，每次删两个临时文件就是 240 多次删除 ——
    在有删除审计/回收站的环境里（本工程的开发沙箱就是）光删除就能拖上几分钟。
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
                    raise RuntimeError("解码超时（%.0f 秒）" % timeout)
                time.sleep(0.05)
        rc = proc.returncode
        with open(out_p, "rb") as f:
            out = f.read()
        with open(err_p, "rb") as f:
            err = f.read()
        if _rc_u32(rc) == _DLL_INIT_FAILED and _try + 1 < _SPAWN_RETRY:
            continue
        return rc, out, err
    return rc, out, err


#: 解码器的 stdout/stderr 落盘文件（进程内复用，见 :func:`_run`）
_io_dir = None
_io_out = None
_io_err = None


def _io_files():
    global _io_dir, _io_out, _io_err
    if _io_out is None:
        import atexit
        _io_dir = tempfile.mkdtemp(prefix="wsr_io_")
        _io_out = os.path.join(_io_dir, "out.txt")
        _io_err = os.path.join(_io_dir, "err.txt")
        atexit.register(shutil.rmtree, _io_dir, True)
    return _io_out, _io_err


def _text(b):
    try:
        return b.decode("utf-8", "replace").strip()
    except Exception:                                   # noqa: BLE001
        return ""


#: 探针缓存：key = (路径, 修改时间, 大小) → 有声曲号列表。
#: ``chip_formats.probe`` 在界面线程上被反复调用，不缓存会把 UI 拖住。
_scan_cache = {}


def _cache_key(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (os.path.abspath(path), int(st.st_mtime), st.st_size)


def scan_tracks(path, *, secs=PROBE_SECS, max_tracks=PROBE_MAX,
                stop_silent=PROBE_STOP_SILENT, cancel=None, use_cache=True):
    """实测哪些曲号有声音，返回升序曲号列表。**不抛异常**，失败返回 ``[]``。

    实现是跑一次 ``wsr2wav.exe <in> --scan lo hi --secs S``：一个进程里
    反复 load/run/unload，所以比「每曲起一个进程」快得多（实测 166 个
    曲号 6 秒音频只要 8 秒墙钟）。
    """
    key = _cache_key(path) if use_cache else None
    if key is not None and key in _scan_cache:
        return list(_scan_cache[key])

    exe = find_decoder()
    if not exe:
        return []

    foot = read_footer(path)
    lo = foot["first_song"] if foot["ok"] else 0
    # 曲号 0 常常是空占位，但有的 rip 确实从 0 开始，所以 from 0 起探；
    # lo 只用来决定「从哪开始算连续静音」，这里统一从 0 开始最省事。
    lo = 0
    hi = min(lo + max_tracks - 1, 0xFFFF)

    cmd = [exe, path, "--scan", str(lo), str(hi), "--secs", "%.3f" % float(secs)]
    try:
        rc, out, err = _run(cmd, timeout=600.0, cancel=cancel)
    except RuntimeError:
        raise
    except Exception:                                   # noqa: BLE001
        return []
    if rc != 0:
        return []

    hits = []
    run_silent = 0
    seen_any = False
    for ln in _text(out).splitlines():
        parts = ln.split("\t")
        if len(parts) != 4 or parts[0] != "TRACK":
            continue
        try:
            t = int(parts[1])
            peak = int(parts[2])
        except ValueError:
            continue
        if peak >= PROBE_PEAK_MIN:
            hits.append(t)
            seen_any = True
            run_silent = 0
        else:
            run_silent += 1
            # 已经出过曲、之后连着静音一大段 → 后面不用探了
            if seen_any and stop_silent and run_silent >= stop_silent:
                break

    if key is not None:
        _scan_cache[key] = list(hits)
    return hits


def _read_wav(path):
    """读 16bit PCM 的 WAV，返回 ``(pcm_bytes, seconds, channels, rate)``。"""
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


def decode_track(path, track, secs=150.0, cancel=None, out_path=None):
    """把一个曲号渲染成 16bit/44.1k/立体声小端 PCM 字节。

    返回 ``dict``：``{ok, pcm, seconds, rate, error}``。

    ``out_path`` 给了就让解码器**直接写这个 WAV**（调用方负责清理，通常放在
    暂存目录里由 ``cleanup_spc_temp`` 整目录删）。不给就自己开临时文件并删掉
    —— 批量解码时请务必给，理由见 :func:`_run` 的第二段 ⚠。
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
        fd, tmp = tempfile.mkstemp(prefix="wsr_", suffix=".wav")
        os.close(fd)
        wav = tmp
    try:
        cmd = [exe, path, wav, "--track", str(int(track)),
               "--secs", "%.3f" % float(secs)]
        rc, out, err = _run(cmd, timeout=max(120.0, float(secs) * 2.0 + 60.0),
                            cancel=cancel)
        if rc != 0:
            msg = _text(err).splitlines()
            msg = msg[-1] if msg else ""
            if _rc_u32(rc) == _DLL_INIT_FAILED:
                res["error"] = ("解码器启动失败（0xc0000142，DLL 初始化失败）"
                                "已自动重试 %d 次仍不行 —— 通常是系统临时资源紧张，"
                                "关掉几个程序或稍后重试。" % _SPAWN_RETRY)
            else:
                res["error"] = msg or ("解码器失败（退出码 %d）" % rc)
            return res
        pcm, seconds, channels, rate = _read_wav(wav)
        if not pcm:
            res["error"] = "解码器说成功，但 WAV 里没有采样"
            return res
        res["ok"] = True
        res["pcm"] = pcm
        res["seconds"] = seconds
        res["rate"] = rate
        if channels != 2:
            res["error"] = "解码器输出 %d 声道（预期 2）" % channels
        return res
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _safe_name(s):
    """曲名里可能有 `/` `:` `?` 之类不能进文件名的字符。"""
    bad = '\\/:*?"<>|'
    return "".join("_" if c in bad else c for c in (s or "")).strip()


def load_tracks(path, *, secs=150.0, cancel=None, on_progress=None, only=None):
    """一个 ``.wsr`` → 归一化音轨列表（每条曲目一轨）。

    曲目表**优先取同目录 m3u**（权威：曲号 + 曲名 + 真实时长），没有才回落到
    实测扫描 + ``secs`` 默认时长。理由见 :func:`read_playlist`。

    ``only`` 给了就只解那一首（试听用 —— 老 GUI / Qt 的 ``path\\x1ffirst\\x1fcount``
    约定会传过来）。

    返回的 ``pcm`` 是 :class:`msu1_to_cd._SpoolPcm`（落盘暂存），因为
    100 多首 × 150 秒 × 176 KB/s 塞内存会直接把进程撑爆。

    ``on_progress(done, total)`` 按**曲**回调 —— 一个 WSR 可能要解几十首、
    跑几分钟，不报进度用户就会以为卡死（实测踩过）。
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
    spool_dir = tempfile.mkdtemp(prefix="wsr_pcm_")
    n = len(plan)
    try:
        for i, (no, title, length) in enumerate(plan):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("已取消")
            wav_path = os.path.join(spool_dir, "%04d.wav" % i)
            r = decode_track(path, no, secs=length, cancel=cancel,
                             out_path=wav_path)
            if not r["ok"]:
                # 单曲失败不拖垮整文件：跳过，继续下一首
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
        shutil.rmtree(spool_dir, ignore_errors=True)
        raise

    if not tracks:
        shutil.rmtree(spool_dir, ignore_errors=True)
        raise RuntimeError("%s：所有曲目都解码失败" % os.path.basename(path))
    return tracks


def available():
    """给界面用：WSR 功能现在能不能用。返回 ``(bool, 原因/解码器路径)``。"""
    return decoder_ready()


def status(force=False):
    """给 ``/api/diag`` 用的一句话。``force`` 只为和 psf_native 对齐。"""
    del force
    ok, why = decoder_ready()
    return {"ok": ok, "detail": why}
