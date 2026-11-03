#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chip_decode.py — 多格式芯片音源的子进程解码层。

职责（设计 §4/§5）：
  · 按「音频秒数」而不是「文件数」把 (文件, 轨区间) 切成 run —— HES 单文件 256 轨，
    按文件数 batching 会让临时盘爆到 GB 级（PRD R4）。
  · 每个 run 起一个子进程解码（libgme 会卡死，必须隔离），Popen 轮询支持取消与超时。
  · 解码前测每轨首帧峰值，< 300 视为空轨跳过（FR-2.3a / PRD T8）。
  · 完全复用 core 的 `_load_gme` / `_spc_decode_track` / `_apply_fade` / `_SpoolPcm`
    / `safe_filename` / `cleanup_spc_temp`，**不复制任何解码逻辑**（设计 §4.1）。

⚠ 三条硬约束（实测结论，勿再"优化"）：
  1. 禁止调用该库的 identify_file / identify_header 接口（本机必段错误）。
  2. 禁止 gme_set_autoload_playback_limit（不产生 ended 事件），时长一律自算。
  3. libgme 自己把单声道补成 L=R，**不要二次立体声化、不要归一化**。

帧格式 v2（设计 §5.3）：
    0x00 4B  magic b"CHP2"
    0x04 u32 ntrack
    每轨: u16 namelen + name(UTF-8) + u32 pcmlen + pcm
          + u32 flags(bit0=cut_by_policy) + f64 planned_secs + f64 actual_secs
    尾部: 逐条 skip 记录（JSON 行），对应"单文件失败不影响整批"（AC-19）
"""

import ctypes
import json
import os
import struct
import subprocess
import sys
import tempfile
import time

import chip_formats as cf
# 起 worker 子进程时别弹控制台（见 winspawn.py 的说明）
import winspawn
# v4.44: 临时目录统一走 D 盘（chip_decode 也可能被单独 import，
# 不依赖 msu1_to_cd 先跑 init）。幂等，重复调用无副作用。
import tmpdir
tmpdir.init()

SR = 44100
BYTES_PER_SEC = SR * 2 * 2          # 16bit × 2ch
CHUNK = 4096                        # 每次 gme_play 的帧数(必须 <= 65535)

# 批次切分阈值（设计 §5.4）
CHIP_RUN_SECS = 1800.0      # 单个 run 的音频秒数上限 -> 峰值 spool ≈ 318 MB
CHIP_MAX_TRACKS = 32         # 单个 run 内单文件最多轨数（短轨时防止切太碎）
CHIP_MAX_FILES = 4           # 单个 run 内最多文件数

# 空轨判定阈值，与 spc_wav._limit_len 注释里的 _SIL_PEAK 同量级（FR-2.3a）
SIL_PEAK = 300
# 首帧预检的帧数：约 0.093s，够判有无声音又不浪费时间
PROBE_FRAMES = 4096

#: 是否运行在「已打包」的程序里 —— 与 msu1_to_cd._IS_FROZEN 同一份判断。
#: ⚠ Nuitka 不设 ``sys.frozen``（那是 PyInstaller 的），只注入 ``__compiled__``。
#: 只判 frozen 会把脚本路径拼进 worker 命令，而 Nuitka 包里没有那个 .py。
_IS_FROZEN = bool(getattr(sys, "frozen", False)) or ("__compiled__" in globals())

_CHIP_TIMEOUT_BASE = 60.0    # 超时 = base + run音频秒/2 + 30，上限 1800
_CHIP_TIMEOUT_MAX = 1800.0

FRAME_MAGIC = b"CHP2"
SKIP_MAGIC = b"\x00SKP"
_FLAG_CUT = 1


class ChipCancelled(Exception):
    """用户点了取消。不计入 fail（设计 §T02 第 3 条）。"""


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 曲名构造（设计 §6.2）
# ---------------------------------------------------------------------------

def _dec(b):
    """bytes -> str（libgme 给的是 UTF-8，失败退 latin-1）。"""
    if not b:
        return ""
    if isinstance(b, str):
        return b.strip()
    try:
        return b.decode("utf-8").strip()
    except (UnicodeDecodeError, AttributeError):
        return bytes(b).decode("latin-1", "replace").strip()


def _fmt_track_name(named, path, ti, ntrack):
    """单轨不加序号前缀（文件名干净），多轨强制加序号（FR-2.3）。"""
    import re
    import msu1_to_cd as core
    if ntrack > 1:
        return core.safe_filename(named)
    base = os.path.splitext(os.path.basename(path))[0]
    stripped = re.sub(r"^\d{2}_", "", named)
    return core.safe_filename(stripped if stripped != named else base)


def _useful_song(song, path):
    """判断 libgme 给的曲名**有没有采用价值**（FR-2.3 曲名质量闸门）。

    修好 `gme_track_info` 调用约定后，libgme 的 song 第一次真正拿得到值。
    但实测它并非总是有用—— 实测 10 种格式里：
      · NSF / GBS / HES / SAP / KSS：song恒为**空串**（这些格式本来就没有
        曲名字段），必须走兜底；
      · VGM/GYM/AY：song 是**真曲名**，比"文件名 + 轨号"更好。

    所以规则是：**非空、且不等于文件名主体**时才用，否则保留兜底。
    「等于文件名」这条很重要：那种情况下 libgme 只是把文件名抄回来，
    用了没有增量信息，还会因为两边截断长度不同而把名字改短/改长。

    返回可采用的曲名，不可用时返回 ""。
    """
    s = (song or "").strip()
    if not s:
        return ""
    base = os.path.splitext(os.path.basename(path))[0].strip()
    import re
    # 文件名主体去掉前导序号（"204 People Without Hope" -> "People Without Hope"）
    fn_body = re.sub(r"^\d+\s+", "", base).strip()
    if s.lower() == base.lower() or s.lower() == fn_body.lower():
        return ""                       # 只是文件名回抄，无信息量
    return s


def _chip_track_name(path, fmt, info, libgme_info, ti, ntrack, meta=None):
    """曲名优先级（FR-2.3 / 设计 §6.2）：

      1a NSFE 逐轨标题（最可靠）
      1b libgme 的 track_name / song  —— **须过 _useful_song 质量闸门**
      1c VGM 的 GD3 曲名
      1d NSF/GBS 的 song 字段是「专辑名」不是曲名，只在单轨时用
      2   文件名 + 轨号
    返回已过 safe_filename 的名字。

    ⚠ `libgme_info` 是 :func:`core._gme_track_info` 返回的 **dict**，不再是
      ctypes 结构体 —— 那块内存已被 gme_free_info 释放，再当结构体读就是
      use-after-free（比错位更难查）。故下面一律用 ``.get()``。
    """
    meta = meta if meta is not None else info
    track_names = getattr(meta, "track_names", None) or []
    if track_names and ti < len(track_names) and track_names[ti]:
        return _fmt_track_name(f"{ti + 1:02d}_{track_names[ti]}", path, ti, ntrack)

    # 1a' v4.29: 同目录 m3u 的曲名（HES 没有内嵌曲名，这是唯一的真实标题来源）。
    # 不给 ti 前缀 —— 落盘时 write_tracks 已经会加 "NN_"，两处都加会变成
    # "01_01_Konami Logo.wav"（旧行为就是这样，实测确认过）。
    titles = getattr(meta, "track_titles", None) or {}
    title = titles.get(ti) if isinstance(titles, dict) else ""
    if title:
        return _fmt_track_name(title, path, ti, ntrack)

    name = ""
    if isinstance(libgme_info, dict):
        raw = libgme_info.get("track_name") or libgme_info.get("song") or ""
        name = _useful_song(raw, path)
    if not name and fmt in ("VGM", "VGZ") and getattr(meta, "title", ""):
        name = meta.title
    if not name and ntrack == 1 and getattr(meta, "title", ""):
        name = meta.title
    if name:
        return _fmt_track_name(f"{ti + 1:02d}_{name}", path, ti, ntrack)

    base = os.path.splitext(os.path.basename(path))[0]
    return _fmt_track_name(f"{base}_{ti + 1:02d}", path, ti, ntrack)


# ---------------------------------------------------------------------------
# worker 主体（在子进程中跑）
# ---------------------------------------------------------------------------

def _peak_of(raw):
    """int16 小端字节流的峰值（与 spc_wav._peak_of 同义，此处独立以免循环 import）。"""
    import array
    a = array.array("h")
    a.frombytes(raw)
    return max(max(a), -min(a)) if a else 0


def _track_peak(gme, emu, sr, nframes=PROBE_FRAMES):
    """解码前预检：读首 nframes 帧测峰值，判断是否空轨（FR-2.3a）。

    只在**确实要跳过**时才付出代价：预检读的是真实解码流的前缀，
    之后 gme_start_track 重新定位即可，不影响正式解码。
    """
    n = max(1, min(int(nframes), 65535))
    buf = (ctypes.c_short * (n * 2))()
    e = gme.gme_play(emu, n * 2, buf)
    if e:
        return 0
    raw = ctypes.string_at(ctypes.addressof(buf), n * 2 * ctypes.sizeof(ctypes.c_short))
    return _peak_of(raw)


def _track_secs(m3u_secs, dur):
    """单轨解码时长：m3u 标注优先，用户设的「单曲最长」仍是硬上限。

    v4.29 新增。为什么不能一律用 ``dur.secs``：HES 的循环 BGM 永远不会
    ``ended``，没有 m3u 时只能按「默认单遍 150s」硬截 —— 实测 HCD9005 的
    63 首因此产出 8334 秒（1.4 GB），而 m3u 里这些曲目平均只有 40 秒。
    """
    try:
        secs = float(m3u_secs or 0.0)
    except (TypeError, ValueError):
        secs = 0.0
    if secs <= 0.2:                      # 未标注 / 明显无效 -> 走原决策链
        return float(dur.secs)
    if getattr(dur, "source", "") == "user" and dur.secs > 0:
        return min(secs, float(dur.secs))
    return secs


def _decode_one(core, gme, path, fmt, info, dur, speed, out,
                first_track=0, track_count=None, log=None):
    """解一个文件的一个轨区间，逐轨 yield (name, pcm, cut, planned, actual)。

    与 core._spc_decode_file 的差异只有 4 处（设计 §4.3）：
      1 时长来源  2 stop_on_silence/lead_sil_limit  3 曲名构造  4 错误前缀
    其余（open → start_track → track_info → play → delete）逐行相同。

    空轨在这里被识别并 yield (None, ...) 标记，由调用方记 `⏭`。
    """
    sp = max(0.5, min(2.0, float(speed or 1.0)))
    sr = int(round(44100 / sp))
    # 走 core.gme_open: 非 ASCII 路径时自动退回 gme_open_data（见其 docstring）
    err, emu, _hold = core.gme_open(gme, path, sr)
    if err:
        raise RuntimeError("解码库打开失败: " + err.decode("latin-1", "replace"))
    try:
        ntrack = gme.gme_track_count(emu)
        if ntrack <= 0:
            raise RuntimeError("文件未包含可识别音轨")
        last = ntrack if track_count is None else min(ntrack, first_track + track_count)
        # v4.29: 同目录 m3u 给了真实曲目表 -> **只解列出来的曲号**（顺序按 m3u）。
        # 没有 m3u 时 want 为空，退回「按区间全扫」的旧行为（零回归）。
        want = []
        for t in (getattr(info, "track_ids", None) or ()):
            try:
                t = int(t)
            except (TypeError, ValueError):
                continue
            if 0 <= t < ntrack:
                want.append(t)
        todo = [t for t in want if first_track <= t < last] if want \
            else list(range(first_track, last))
        lens = getattr(info, "track_lengths", None) or {}
        for ti in todo:
            e = gme.gme_start_track(emu, ti)
            if e:
                raise RuntimeError("start_track 失败: " + e.decode("latin-1", "replace"))
            # gme_track_info 对某些格式可能返回错误串；不 raise，降级为文件名+轨号（H8）
            # ⚠ 走 core._gme_track_info()：它用正确的 `gme_info_t**` 调用约定
            #   + gme_free_info 释放。旧写法（本地 new 结构体 + 传单指针）
            #   会把 DLL 写的结构体指针当成字段读，且每轨泄漏 —— 见
            #   core._GmeInfo 的 docstring。
            libinfo = core._gme_track_info(gme, emu, ti)
            # v4.29: m3u 标注了本轨长度就按它解（循环 BGM 不会再一律拉满 150s）
            secs = _track_secs(lens.get(ti, 0.0), dur)

            # 空轨预检（FR-2.3a）：峰值 < 300 视为未使用轨，跳过不产出 WAV
            peak = _track_peak(gme, emu, sr)
            gme.gme_start_track(emu, ti)      # 预检消耗了帧，重新定位
            if peak < SIL_PEAK:
                if log:
                    log(f"[chip] skip empty track {ti} peak={peak}")
                yield (None, None, False, secs, 0.0)
                continue

            pcm = core._spc_decode_track(
                gme, emu, sr, max_secs=secs,
                stop_on_silence=dur.stop_on_silence,
                lead_sil_limit=dur.lead_sil_limit,
                # v4.4：芯片格式用 libgme 官方 ended 收尾。
                # 芯片音乐里同包的轨长短差异极大（1.25s 音效 ~ 无限循环 BGM），
                # 「连续 2 秒静音」判定对两类都会错，交给格式实现方判定更可靠。
                # ⚠ SPC 路径不传此参数，行为逐字节不变。
                stop_on_ended=True)
            # 已知确切时长却被截短 -> 记 cut（FR-3.4）
            actual = len(pcm) / float(sr * 2 * 2)
            cut = actual < secs - 0.05
            pcm = core._apply_fade(bytes(pcm), cf.CHIP_FADE_MS, sr)
            name = _chip_track_name(path, fmt, info, libinfo, ti, ntrack)
            yield (name, pcm, cut, secs, actual)
    finally:
        gme.gme_delete(emu)


def _write_track(f, name, pcm, cut, planned, actual):
    """按 v2 帧格式写一条轨（设计 §5.3）。"""
    nb = name.encode("utf-8")
    f.write(struct.pack("<H", len(nb)))
    f.write(nb)
    f.write(struct.pack("<I", len(pcm)))
    f.write(pcm)
    f.write(struct.pack("<I", _FLAG_CUT if cut else 0))
    f.write(struct.pack("<d", float(planned)))
    f.write(struct.pack("<d", float(actual)))


def run_worker(out_path, in_paths, *, default_secs=0.0, cap_secs=600.0,
               fade_ms=1500, speed=1.0, lead_sil=10.0, user_max_secs=0.0):
    """worker 主体：解码 in_paths 里的芯片音源，写 v2 帧文件到 out_path。

    in_paths 的元素可以是纯路径，也可以是 "path\\x1ffirst\\x1fcount"
    （_plan_runs 切出来的轨区间），后者由 _split_item 还原。
    每个文件独立 try/except（AC-19）。
    """
    import faulthandler
    import msu1_to_cd as core
    faulthandler.enable(file=sys.stderr)   # 段错误时把 Python 栈写 stderr（设计 §5.6）

    gme = core._load_gme()
    ntrack = 0
    skips = []
    with open(out_path, "wb") as f:
        f.write(FRAME_MAGIC)
        f.write(struct.pack("<I", 0))      # 占位，稍后回填
        for item in in_paths or ():
            path, first, count = _split_item(item)
            try:
                info = cf.probe(path)
                fmt = info.fmt or ""
                # 时长链：worker 与界面调同一个纯函数，结果必然一致（设计 §5.1）
                dur = cf.plan_duration(
                    info, user_max_secs=user_max_secs,
                    default_secs=default_secs, hard_cap_secs=cap_secs)
                sys.stderr.write(
                    f"[chip] {os.path.basename(path)} fmt={fmt} "
                    f"n={info.n_tracks} plan={dur.secs:.2f}s/{dur.source} "
                    f"sil={dur.stop_on_silence} tracks={first}+{count}\n")
                sys.stderr.flush()
                for name, pcm, cut, planned, actual in _decode_one(
                        core, gme, path, fmt, info, dur, speed, f,
                        first_track=first, track_count=count,
                        log=lambda m: (sys.stderr.write(m + "\n"),
                                       sys.stderr.flush())):
                    if name is None:
                        continue          # 空轨：不产出
                    _write_track(f, name, pcm, cut, planned, actual)
                    ntrack += 1
            except Exception as e:                       # noqa: BLE001
                # 单个文件失败 -> 记一条 skip，继续下一个（AC-19）
                skips.append({"t": "skip", "path": path, "msg": str(e)})
                sys.stderr.write(f"[chip] FAIL {os.path.basename(path)}: {e}\n")
                sys.stderr.flush()
        # 回填真实轨数
        f.flush()
        f.seek(4)
        f.write(struct.pack("<I", ntrack))
        f.seek(0, os.SEEK_END)
        # skip 记录写在**尾部**（设计 §5.7），保证前面的轨区连续可顺序读
        if skips:
            f.write(SKIP_MAGIC)
            blob = json.dumps(skips, ensure_ascii=False).encode("utf-8")
            f.write(struct.pack("<I", len(blob)))
            f.write(blob)
    return ntrack


def _split_item(item):
    """还原 "path\\x1ffirst\\x1fcount" -> (path, first, count)。

    count == -1（或缺省/None）表示"从 first 到文件末尾"。
    """
    if isinstance(item, (tuple, list)):
        p = item[0]
        first = int(item[1]) if len(item) > 1 else 0
        count = item[2] if len(item) > 2 else None
        return p, first, _norm_count(count)
    s = str(item)
    if "\x1f" in s:
        parts = s.split("\x1f")
        try:
            return parts[0], int(parts[1]), _norm_count(int(parts[2]))
        except (ValueError, IndexError):
            return s, 0, None
    return s, 0, None


def _norm_count(count):
    """把 -1 / None 归一成 None（= 全部剩余轨）。"""
    if count is None:
        return None
    try:
        c = int(count)
    except (TypeError, ValueError):
        return None
    return None if c < 0 else c


# ---------------------------------------------------------------------------
# 父进程侧：切分 / 拉起 worker / 读帧
# ---------------------------------------------------------------------------

def _chip_worker_cmd(out_path, in_paths, *, default_secs=150.0, cap_secs=600.0,
                     fade_ms=1500, speed=1.0, lead_sil=10.0):
    """构造 worker 命令行（设计 §5.1）。**只传标量旋钮，不传 per-file 时长表** ——
    worker 自己调 chip_formats.probe + plan_duration，保证界面与 worker 算出的
    时长必然一致，且不需要 sidecar 文件、不存在两边版本漂移。"""
    if _IS_FROZEN:
        # 打包后必须问 msu1_to_cd.worker_launcher()：Nuitka onefile 的
        # sys.executable 指向一个**不存在**的虚拟 python.exe（见那边的注释）。
        # 函数内 import 是为了避开与 msu1_to_cd 的循环依赖。
        import msu1_to_cd as _core
        base = [_core.worker_launcher()]
    else:
        base = [sys.executable, os.path.abspath(__file__)]
    # 轨区间用 "path\x1ffirst\x1fcount" 编码进 argv（worker 侧 _split_item 还原）。
    # 纯路径（first=0/无区间）也统一编码，解码侧逻辑只有一条分支。
    args = [_encode_item(p) for p in in_paths]
    return (base + ["--chip-decode", out_path,
                    "--chip-secs", repr(float(default_secs)),
                    "--chip-cap", repr(float(cap_secs)),
                    "--chip-fade", str(int(fade_ms)),
                    "--chip-speed", repr(float(speed)),
                    "--chip-lead", str(int(lead_sil))]
            + args)


def _encode_item(item):
    """(path, first, count) -> "abs_path\\x1ffirst\\x1fcount"。"""
    if isinstance(item, (tuple, list)):
        p = item[0]
        first = int(item[1]) if len(item) > 1 else 0
        count = item[2] if len(item) > 2 else None
        return f"{os.path.abspath(p)}\x1f{first}\x1f{-1 if count is None else int(count)}"
    return os.path.abspath(item)


def _plan_runs(items):
    """items: [(path, n_tracks, per_track_secs)] -> [[(path, first, count), ...], ...]

    切分规则（设计 §5.4）：累积「轨数 × per_track_secs」超过 CHIP_RUN_SECS，
    或单 run 轨数超 CHIP_MAX_TRACKS，或文件数超 CHIP_MAX_FILES，即开新 run。
    单个文件自身就超阈值时，按 CHIP_RUN_SECS 把它的轨区间切片，
    保证一个 run 的磁盘占用有界（H5：256 轨 HES → 22 个 run，峰值 spool ≤ 400MB）。
    """
    runs, cur = [], []
    cur_secs = 0.0
    cur_files = 0

    def flush():
        nonlocal cur, cur_secs, cur_files
        if cur:
            runs.append(cur)
        cur, cur_secs, cur_files = [], 0.0, 0

    for it in items or ():
        path, n, per = _as_item(it)
        n = max(1, int(n or 1))
        per = max(0.5, float(per or cf.CHIP_DEFAULT_SECS_DEFAULT))
        # 这个文件内部按 CHIP_RUN_SECS 切片
        start = 0
        while start < n:
            if cur and (cur_files >= CHIP_MAX_FILES
                        or cur_secs + per > CHIP_RUN_SECS
                        or _cur_tracks(cur) + 1 > CHIP_MAX_TRACKS):
                flush()
            # 本切片最多再放多少秒（首个切片至少 1 轨，避免 take=0 死循环）
            room = CHIP_RUN_SECS - cur_secs
            take = int(room // per) if room >= per else 1
            take = max(1, min(take, n - start))
            cur.append((path, start, take))
            cur_secs += take * per
            cur_files += 1
            start += take
            if cur_secs >= CHIP_RUN_SECS or cur_files >= CHIP_MAX_FILES \
                    or _cur_tracks(cur) >= CHIP_MAX_TRACKS:
                flush()
        # 文件结束时不强制 flush：下一个同 run 的文件若还装得下就合并（省 spawn）
    flush()
    return runs


def _as_item(it):
    if isinstance(it, (tuple, list)):
        return it[0], (it[1] if len(it) > 1 else 1), (it[2] if len(it) > 2 else None)
    return it, 1, None


def _cur_tracks(cur):
    return sum(int(c or 1) for _p, _f, c in cur)


def _run_worker(cmd, timeout, cancel=None):
    """跑一个 chip worker，返回 (returncode, stderr_text)（设计 §5.5）。

      - stderr 落临时文件而非 PIPE，避免管道写满导致子进程死锁；
      - 100ms 轮询，响应取消（AC-15 要求 10s 内停止）；
      - 取消/超时一律 kill 后**直接删 out_path 不读它** —— 半截帧文件会让
        _read_chip_frame 抛"结果被截断"，掩盖真正的原因。
    """
    errf = tempfile.NamedTemporaryFile(delete=False, prefix="chiperr_",
                                           suffix=".chiperr")
    err_path = errf.name
    errf.close()
    out_path = cmd[cmd.index("--chip-decode") + 1] if "--chip-decode" in cmd else None
    # 传文件路径(而不是已关闭的文件对象)给 Popen —— Windows 上 Popen 需要
    # 一个可用的 OS 句柄, 传关闭后的 NamedTemporaryFile 会抛
    # "I/O operation on closed file"。
    with open(err_path, "wb") as errfh:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errfh,
                                **winspawn.silent_kw())
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.wait(3)
                _rm(err_path)
                if out_path:
                    _rm(out_path)
                raise ChipCancelled()
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait(3)
                _rm(err_path)
                if out_path:
                    _rm(out_path)
                raise RuntimeError(f"芯片音源解码超时({timeout:.0f}s)")
            time.sleep(0.1)
        with open(err_path, encoding="utf-8", errors="replace") as f:
            return proc.returncode, f.read()
    finally:
        _rm(err_path)


def _read_chip_frame(path):
    """读 v2 帧文件 -> 音轨列表。PCM 落盘为 _SpoolPcm（不进内存）。

    布局：magic + u32 ntrack + ntrack 条轨 + （可选）SKIP 尾段。
    返回 {"tracks": [...], "skips": [{path,msg}...]}。
    """
    import msu1_to_cd as core
    with open(path, "rb") as f:
        header = f.read(8)
        if len(header) < 8 or header[:4] != FRAME_MAGIC:
            raise RuntimeError("芯片音源解码结果为空或已损坏")
        (ntrack,) = struct.unpack_from("<I", header, 4)
        tracks = []
        spool_dir = tempfile.mkdtemp(prefix="chip_pcm_")
        for i in range(ntrack):
            (nlen,) = struct.unpack("<H", f.read(2))
            if not nlen:
                raise RuntimeError(f"芯片音源解码结果被截断 (轨 {i})")
            name = f.read(nlen).decode("utf-8", "replace")
            (plen,) = struct.unpack("<I", f.read(4))
            pcm_path = os.path.join(spool_dir, f"{i:04d}.pcm")
            remaining = plen
            with open(pcm_path, "wb") as pf:
                while remaining > 0:
                    chunk = f.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise RuntimeError("芯片音源解码结果被截断")
                    pf.write(chunk)
                    remaining -= len(chunk)
            f.read(4)                  # flags
            f.read(8)                  # planned_secs
            f.read(8)                  # actual_secs
            tracks.append({"name": name, "pcm": core._SpoolPcm(pcm_path, spool_dir),
                           "little_endian": True})
        # 尾段：skip 记录（该 run 内失败的文件，不影响已产出的轨）
        skips = []
        tail = f.read(len(SKIP_MAGIC))
        if tail == SKIP_MAGIC:
            (blen,) = struct.unpack("<I", f.read(4))
            try:
                skips = json.loads(f.read(blen).decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                skips = []
        return tracks, skips


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def load_chip_paths(paths, *, default_secs=0.0, cap_secs=600.0, fade_ms=1500,
                    speed=1.0, lead_sil=10.0, cancel=None, on_progress=None,
                    user_max_secs=0.0):
    """多个芯片音源文件 -> 归一化音轨列表（与 core.load_spc_paths 同签名语义）。

    按音频秒数切 run，逐 run 拉子进程；每 run 结束 PCM 落盘为 _SpoolPcm 并
    追加到 tracks（不在内存累积）。整体失败抛 RuntimeError，单文件失败只记 skip。

    ⚠ ``.wsr`` 是**例外**：libgme 没有 WonderSwan 机种，它走外挂的
    ``wsr2wav.exe``（beetle-wswan 核），在**本进程内**逐曲号渲染。
    为了不打乱输出顺序（一个文件夹里 WSR 和 GBS 混着放时，音轨要按文件名
    排），这里按原顺序切块：WSR 单独处理，中间连续的其它文件照旧批量交给
    worker。
    """
    prog = on_progress or (lambda *_a: None)
    paths = list(paths or ())
    tracks = []
    stat_skips = []
    done = [0]                       # 已处理文件数（进度用，闭包里改）

    def _flush(items):
        """把一批非 WSR 文件交给 libgme worker（内部再按秒数切 run）。"""
        if not items:
            return
        runs = _plan_runs(items)
        for ri, run in enumerate(runs, 1):
            if cancel is not None and cancel.is_set():
                raise ChipCancelled()
            run_secs = 0.0
            for path, first, count in run:
                try:
                    info = cf.probe(path)
                    run_secs += (count or info.n_tracks or 1) * info.per_track_secs
                except Exception:                        # noqa: BLE001
                    pass
            timeout = min(_CHIP_TIMEOUT_MAX,
                          _CHIP_TIMEOUT_BASE + run_secs / 2.0 + 30.0)
            out_path = None
            try:
                tf = tempfile.NamedTemporaryFile(delete=False, prefix="chipbin_",
                                         suffix=".chipbin")
                tf.close()
                out_path = tf.name
                cmd = _chip_worker_cmd(out_path, run, default_secs=default_secs,
                                       cap_secs=cap_secs, fade_ms=fade_ms,
                                       speed=speed, lead_sil=lead_sil)
                rc, err = _run_worker(cmd, timeout, cancel)
                if rc != 0:
                    msg = (err or "").strip()
                    if "CHIPDECODE_ERROR" in msg:
                        msg = msg.split("CHIPDECODE_ERROR", 1)[1].strip(": ")
                    else:
                        msg = (msg or f"退出码 {rc}") + " | argv=" + repr(cmd) + \
                            " | inputs=" + ", ".join(os.path.basename(p) for p, _f, _c in run)
                    raise RuntimeError("芯片音源解码失败: " + msg)
                got, skips = _read_chip_frame(out_path)
                tracks.extend(got)
                stat_skips.extend(skips)
                done[0] += len(run)
                secs = sum(len(t["pcm"]) for t in tracks) / BYTES_PER_SEC
                prog(done[0], len(paths), secs)
            finally:
                if out_path and os.path.exists(out_path):
                    _rm(out_path)

    chunk = []
    for p in paths:
        # 老 GUI / Qt 的「只解第 N 轨」约定是 `path\x1ffirst\x1fcount`，
        # 这种 item 不以 .wsr 结尾，得先拆出真正的路径再判断。
        base = str(p).split("\x1f", 1)[0]
        # 外挂解码器：libgme 没有 WonderSwan 机种（.wsr），KSS 的实现又残缺
        # （不映射 0x4000-0x7FFF，32 KB 的 GG rip 会全静音）。两者都走自己的 exe。
        #
        # v5.9.13 补第三种：`.s98`（PC-98 的 YM2608）。libgme 的 VGM 引擎只实现
        # SN76489 / YM2413 / YM2612 三个芯片指令，**没有 YM2608 分支** ——
        # 所以「转成 VGM 交给 libgme」这条路能跑但输出精确静音（不报错）。
        # 走 s98_native → s982wav.exe（ymfm 核）。
        native = ""
        if base.lower().endswith(cf.WSR_EXTS):
            native = "wsr_native"
        elif base.lower().endswith(cf.KSS_EXTS):
            native = "kss_native"
        elif base.lower().endswith(cf.S98_EXTS):
            native = "s98_native"
        if native:
            _flush(chunk)
            chunk = []
            if cancel is not None and cancel.is_set():
                raise ChipCancelled()
            only = None
            parts = str(p).split("\x1f")
            if len(parts) >= 3:
                try:
                    only = int(parts[1]) if int(parts[2]) == 1 else None
                except ValueError:
                    only = None
            try:
                mod = __import__(native)
                got = mod.load_tracks(
                    base, secs=(default_secs or 150.0), cancel=cancel, only=only,
                    # 一个 WSR / KSS 包可能要解几十首、跑几分钟 —— 不按曲报进度，
                    # 界面就是「一直无进度」（实测踩过）。
                    on_progress=lambda d, t: prog(
                        done[0] + float(d) / max(1.0, float(t)), len(paths),
                        sum(len(x["pcm"]) for x in tracks) / BYTES_PER_SEC))
                tracks.extend(got)
            except Exception as e:                       # noqa: BLE001
                if cancel is not None and cancel.is_set():
                    raise ChipCancelled()
                stat_skips.append({"path": base, "msg": str(e)})
            done[0] += 1
            prog(done[0], len(paths),
                 sum(len(t["pcm"]) for t in tracks) / BYTES_PER_SEC)
        else:
            info = cf.probe(p)
            chunk.append((p, info.n_tracks or 1, info.per_track_secs))
    _flush(chunk)

    # 曲名去重（与 core.load_spc_paths 一致，避免同名互相覆盖）
    import msu1_to_cd as core
    for tr, new_name in zip(tracks, core.dedupe_names([t["name"] for t in tracks])):
        tr["name"] = new_name
    load_chip_paths.last_skips = stat_skips
    return tracks


def load_chip(path, **kw):
    """单个芯片音源文件 -> 归一化音轨列表。"""
    return load_chip_paths([path], **kw)


# ---------------------------------------------------------------------------
# worker 分发（由 core.maybe_run_spc_worker 调用）
# ---------------------------------------------------------------------------

def _parse_knv(args):
    """解析 --chip-* 旋钮。返回 (kv, 剩余的位置参数)。

    必须**成对消费** "--chip-secs 150.0" —— 只过滤掉 "--xxx" 会把 150.0
    当成输入文件名传进 worker（实测踩过：worker 依次去打开 "150.0"/"600.0"）。
    """
    kv = {"default_secs": 0.0, "cap_secs": 600.0, "fade_ms": 1500,
          "speed": 1.0, "lead_sil": 10.0}
    keys = {"--chip-secs": "default_secs", "--chip-cap": "cap_secs",
            "--chip-fade": "fade_ms", "--chip-speed": "speed",
            "--chip-lead": "lead_sil"}
    rest = []
    i = 0
    while i < len(args):
        k = args[i]
        if k in keys and i + 1 < len(args):
            try:
                kv[keys[k]] = float(args[i + 1])
            except ValueError:
                pass
            i += 2                      # 成对消费 flag + value
            continue
        if k.startswith("--"):
            i += 1                      # 未知 flag：跳过它自己（值留给 rest）
            continue
        rest.append(args[i])
        i += 1
    return kv, rest


def _run_chip_worker():
    """以子进程 worker 身份解码并退出（设计 §5.2）。"""
    try:
        i = sys.argv.index("--chip-decode")
        out_p = sys.argv[i + 1]
        kv, in_paths = _parse_knv(sys.argv[i + 2:])
        if not in_paths:
            raise RuntimeError("缺少输入芯片音源文件")
        run_worker(out_p, in_paths, **kv)
    except Exception as e:                              # noqa: BLE001
        sys.stderr.write("CHIPDECODE_ERROR: " + str(e) + "\n")
        sys.stderr.flush()
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    _run_chip_worker()
