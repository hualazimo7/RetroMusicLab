#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
msu1_to_cd.py - 把游戏音乐转成两种可播放格式

  1) 音频 CD 镜像 (BIN+CUE)  -> 用 ImgBurn 烧 CD-R 在普通 CD 机播放
  2) WAV (16-bit/44.1kHz/立体声) -> 给 Pico-Plus / pico-infonesPlus 播放

支持三类源:
  A) SNES MSU-1 游戏文件夹: 内含 *.pcm (16-bit/44.1k/立体声/小端裸 PCM, 前 8 字节 MSU1 头)
  B) 光盘镜像 (BIN+CUE): PS1 / PCECD / MDCD(Sega CD) / Saturn 等
     音乐以 CD-DA 音轨形式存在, 从 CUE 定位 AUDIO 轨并切片 (big-endian CD-DA)
  C) SPC (SNES-SPC700): 用 libgme 解码为 16-bit/44.1k/立体声 小端 PCM
     (SPC 原生 32kHz, libgme 在打开时重采样到 44.1kHz)。单个 .spc 或含 .spc 的文件夹均可。

=== CD-DA 说明 ===
  Red Book 音频是大端(big-endian)存储; MSU-1 PCM 是小端, 脚本会做字节交换。
  每扇区 2352 字节, 每轨前插 2 秒(150 扇区)前导静音。

用法:
  # 一个 MSU-1 文件夹
  python msu1_to_cd.py ./pcm -o cd_output -n chrono --album "Chrono Trigger"

  # 一个光盘镜像 (cue 或 bin)
  python msu1_to_cd.py ./game.cue -o cd_output --album "Game OST"
  python msu1_to_cd.py ./game.bin -o cd_output --wav-out wav_output --verify
"""

import argparse
import array
import ctypes
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

# v4.44: 临时目录统一走 D 盘（见 tmpdir.py 顶部说明）。必须在任何
# tempfile.mkdtemp() 之前调用 —— 模块级执行，import 本模块即生效。
import tmpdir
tmpdir.init()

SECTOR = 2352          # CD-DA 每扇区音频字节数
FRAME = 75             # 每秒扇区数 (44.1kHz*16bit*2ch / 2352 = 75)

#: 光盘镜像的**镜像文件**扩展名（CUE 里 FILE 指向的东西）。
#: PCE-CD 的常见形态是「.cue + .iso」（单文件，音频轨与数据轨交织在同一个
#: 2352 字节扇区的镜像里），Sega CD / PS1 多是 .cue + .bin。v4.38 之前只认
#: .bin，PCE-CD 整包会被判成「不是光盘镜像」，最后报「未找到音轨」。
DISC_IMG_EXTS = (".bin", ".iso", ".img", ".raw")

try:
    import numpy as _np
    _HAVE_NUMPY = True
except Exception:
    _HAVE_NUMPY = False


def sector_to_mmssff(sector):
    m = sector // (60 * FRAME)
    r = sector % (60 * FRAME)
    s = r // FRAME
    f = r % FRAME
    return f"{m:02d}:{s:02d}:{f:02d}"


def detect_header(fp):
    with open(fp, "rb") as fh:
        magic = fh.read(4)
    return 8 if magic == b"MSU1" else 0


def read_pcm_bytes(fp, skip, channels):
    with open(fp, "rb") as fh:
        fh.seek(skip)
        raw = fh.read()
    if len(raw) % (2 * channels) != 0:
        raise ValueError(
            f"{fp}: 数据长度 {len(raw)} 不是 {2 * channels} 的倍数(声道数={channels}), 可能不是标准 MSU-1 PCM"
        )
    return raw


def le_to_be(raw):
    """小端 int16 字节 <-> 大端 int16 字节 (字节交换, 自身反函数)。

    接受 bytes 或磁盘暂存对象 _SpoolPcm(惰性读入)。
    """
    a = array.array("h")
    a.frombytes(raw.read_all() if hasattr(raw, "read_all") else raw)
    a.byteswap()
    return a.tobytes()


def pcm_le(tr):
    """取归一化音轨的小端 PCM 字节(bytes)。兼容磁盘暂存对象 _SpoolPcm。"""
    raw = tr["pcm"]
    if hasattr(raw, "read_all"):
        raw = raw.read_all()
    return le_to_be(raw) if not tr["little_endian"] else raw


def _swap16(raw):
    """16-bit 样本字节序交换 (自身反函数), 就地返回新 bytes。"""
    a = array.array("h")
    a.frombytes(raw)
    a.byteswap()
    return a.tobytes()


def natural_track_key(name):
    base = os.path.basename(name)
    nums = re.findall(r"\d+", base)
    n = int(nums[-1]) if nums else 0
    return (n, base.lower())


#: Windows 长路径前缀。加了它之后路径**不再做任何归一化**
#: （正斜杠不会被转成反斜杠、``.`` / ``..`` 也不会被解析），
#: 所以必须先 ``abspath`` 再拼。
_LONG_PREFIX = "\\\\?\\"

#: 超过这个长度就加前缀。真正的上限是 260，留 20 字符余量 ——
#: 因为调用方可能还要在末尾拼一个文件名（目录 + 文件名的额度是共享的）。
_LONG_AT = 240


def long_path(path):
    """把路径变成 Windows 能打开的长路径（v5.9.12）。

    ## 为什么需要

    Windows 的 ``MAX_PATH`` 是 **260 字符**（含盘符和结尾 ``\\0``）。
    超过之后 ``os.path.isfile()`` / ``open()`` 一律报
    **``FileNotFoundError: [Errno 2] No such file or directory``** ——
    这个错误码看起来像「文件不存在」，实际上是「路径太长」。

    实测（PS4 的《刺客信条4：自由呐喊》）：包里有 186 个 ``.txtp``，
    其中 **38 个的完整路径是 263~315 字符**（名字里塞满了
    ``(3198468865=LAND)(PLANTATIONS=INACTIVE)...`` 这种事件参数）。
    它们全部被 ``list_txtp`` 的 ``os.path.isfile`` 判成「不存在」，
    于是**静默消失**：界面上少 38 首，不报错、日志里也没有任何痕迹。
    这类「少东西但不报错」是最难被用户描述清楚的一类 bug。

    ## 做法

    ``\\\\?\\`` 前缀让 Win32 API 走「不解析、不归一化」的直通路径，
    长度上限提到约 32767。Python 的 ``open`` 和 vgmstream 这类原生程序
    都认（两边都实测过）。

    ⚠ 三个注意点：
      1. **必须先 abspath** —— 加了前缀就不再解析 ``.`` / ``..``，
         也不把 ``/`` 转成 ``\\``。
      2. **UNC 路径要单独拼** —— ``\\\\server\\share`` 得写成
         ``\\\\?\\UNC\\server\\share``，直接加前缀会变成非法路径。
      3. **只在真的超长时才加** —— 前缀会让一些老 API 和部分第三方库
         认不出来，能用短路径就别用。
    """
    p = str(path or "")
    if os.name != "nt" or not p or p.startswith(_LONG_PREFIX):
        return p
    if len(p) < _LONG_AT:
        return p
    p = os.path.abspath(p)
    if p.startswith("\\\\"):
        return _LONG_PREFIX + "UNC\\" + p[2:]
    return _LONG_PREFIX + p


# --------------------------------------------------------------------------
# 统一音源层: 任何源都归一化为 音轨列表
#   每条音轨 = {"name": str, "pcm": bytes(16-bit/44.1k/立体声), "little_endian": bool}
# --------------------------------------------------------------------------

def load_msu1_dir(input_path):
    files = []
    for f in os.listdir(input_path):
        if f.lower().endswith(".pcm"):
            files.append(os.path.join(input_path, f))
    files.sort(key=natural_track_key)
    tracks = []
    for fp in files:
        skip = detect_header(fp)
        raw = read_pcm_bytes(fp, skip, 2)
        tracks.append({
            "name": os.path.splitext(os.path.basename(fp))[0],
            "pcm": raw,
            "little_endian": True,
        })
    return tracks


def _cue_msf(s):
    """``mm:ss:ff`` -> 绝对扇区号。"""
    m = re.match(r"(\d+):(\d+):(\d+)", s)
    if not m:
        return None
    return (int(m.group(1)) * 60 + int(m.group(2))) * FRAME + int(m.group(3))


def parse_cue(cue_path):
    """解析 CUE, 返回裸音轨描述列表。

    每条含 ``file/num/type/start/index0/pregap/title``。
    ``start`` = ``INDEX 01``（真正开始播放的位置）；``index0`` = ``INDEX 00``
    （pregap 起点，不一定有）；``pregap`` = ``PREGAP`` 命令声明的虚拟前间隙。

    为什么要记 index0/pregap：单文件镜像里音频轨与数据轨交织，算「上一轨
    到哪结束」必须用**下一轨的 pregap 起点**，否则会把 pregap 或整条数据轨
    算进上一轨（v4.38 修的就是这个）。
    """
    tracks = []
    cur_file = None
    cur = None
    with open(cue_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            up = line.upper()
            if up.startswith("FILE"):
                m = re.match(r'FILE\s+"?([^"]+)"?\s*(\w+)?', line, re.I)
                if m:
                    if cur is not None:
                        tracks.append(cur)
                        cur = None
                    cur_file = m.group(1)
                continue
            if up.startswith("TRACK"):
                m = re.match(r'TRACK\s+(\d+)\s+(\w+)', line, re.I)
                if m:
                    if cur is not None:
                        tracks.append(cur)
                    cur = {"file": cur_file, "num": int(m.group(1)),
                           "type": m.group(2).upper(), "start": None,
                           "index0": None, "pregap": None, "title": ""}
                continue
            if up.startswith("PREGAP") and cur is not None:
                m = re.match(r"PREGAP\s+(\d+):(\d+):(\d+)", line, re.I)
                if m:
                    cur["pregap"] = _cue_msf(":".join(m.groups()))
                continue
            if up.startswith("INDEX"):
                m = re.match(r'INDEX\s+(\d+)\s+(\d+):(\d+):(\d+)', line, re.I)
                if m and cur is not None:
                    n = int(m.group(1))
                    pos = _cue_msf(":".join(m.groups()[1:]))
                    if n == 0:
                        cur["index0"] = pos
                    elif n == 1:
                        cur["start"] = pos
                continue
            if up.startswith("TITLE") and cur is not None:
                m = re.match(r'TITLE\s+"?([^"]+)"?', line, re.I)
                if m:
                    cur["title"] = m.group(1)
    if cur is not None:
        tracks.append(cur)
    return tracks


def _disc_cue_path(cue_or_bin):
    """镜像入口统一成 .cue：给镜像文件就找同名 .cue；给目录就找里面第一个 .cue。"""
    p = str(cue_or_bin)
    if os.path.isdir(p):
        cues = sorted(f for f in os.listdir(p) if f.lower().endswith(".cue"))
        if not cues:
            raise RuntimeError(f"{p}: 目录里没有 .cue 文件")
        return os.path.join(p, cues[0])
    if p.lower().endswith(DISC_IMG_EXTS):
        cand = os.path.splitext(p)[0] + ".cue"
        if os.path.exists(cand):
            return cand
        raise RuntimeError(
            f"{p}: 未找到对应的 .cue 文件, 无法定位音轨 (请提供 CUE)")
    return p


#: 判定字节序时每处采样的帧数；位置取 10% / 50% / 85%。
#: 只取开头不够 —— 很多音轨开头是淡入或前导静音，那一段判别不出来。
_ENDIAN_WIN = 8192
_ENDIAN_POS = (0.10, 0.50, 0.85)


def _int16(raw, little):
    """把字节流按指定字节序解释成 int16 序列（与本机字节序无关）。"""
    a = array.array("h")
    a.frombytes(raw)
    if (sys.byteorder == "little") != bool(little):
        a.byteswap()
    return a


def _lag1_acc(xs, ch=2, acc=None):
    """累加各声道 lag-1 自相关的 分子/分母（可跨多个窗口累加）。

    用累加而不是「各窗口分别求再平均」：静音窗口的分子分母都接近 0，
    直接平均会被它拉偏；累加等价于按能量加权，响的段落说了算。
    ``acc`` 会被**就地更新**（调用方复用同一个列表跨窗口累加）。
    """
    if acc is None:
        acc = [0.0, 0.0]
    num, den = acc
    n = (len(xs) // ch) * ch
    for c in range(ch):
        v = xs[c:n:ch]
        k = len(v)
        if k < 3:
            continue
        mean = sum(v) / k
        prev = v[0] - mean
        for i in range(1, k):
            cur = v[i] - mean
            num += cur * prev
            den += prev * prev
            prev = cur
        den += prev * prev
    acc[0] = num
    acc[1] = den
    return acc


def _detect_cd_endian(pcm, ch=2):
    """判断 CD-DA 音轨的字节序。返回 True 表示**小端**存储。

    Red Book 规定 CD-DA 是大端，但现实里的 BIN 有相当一部分是小端
    （某些抓轨/转档工具按本机字节序写出 16-bit 样本）。按大端硬读这类
    文件，等于把「低字节的随机性」搬到高位 —— 听感就是宽带白噪，也就是
    用户反馈的「刺耳的吱吱声」，不是原声。

    判据用**声道内 lag-1 自相关** r1：真实音频相邻采样高度相关
    （音乐 r1 常在 0.9 以上）；字节序错了 r1 会掉到 0 附近。该指标与
    音量无关，所以安静的音轨也判得出来。实测：
      - 真实音乐 CD 的 BIN：BE=-0.000 / LE=0.986  -> 小端
      - 大端合成的音乐：    BE=0.988  / LE=0.001  -> 大端

    两者都判不出来时（静音轨，两种解释的自相关一样）改用**直流偏移**
    裁决：真音频 DC 接近 0，错序会把 LSB 放大成明显直流偏置。
    实测 PS1 静音轨：两种解释 r1 都是 0.456，但 |DC| 是 1846 vs 8。
    """
    total = len(pcm) // (ch * 2)
    if total < 512:
        return False                      # 太短，按规范用大端
    be_acc = [0.0, 0.0]
    le_acc = [0.0, 0.0]
    dc_be = dc_le = 0.0
    for frac in _ENDIAN_POS:
        f0 = min(int(total * frac), max(0, total - _ENDIAN_WIN))
        raw = pcm[f0 * ch * 2:(f0 + _ENDIAN_WIN) * ch * 2]
        raw = raw[:len(raw) // (ch * 2) * (ch * 2)]
        if len(raw) < ch * 2 * 64:
            continue
        b = _int16(raw, False)
        l = _int16(raw, True)
        _lag1_acc(b, ch, be_acc)
        _lag1_acc(l, ch, le_acc)
        for arr, pick in ((b, "be"), (l, "le")):
            k = len(arr) // ch
            if k:
                m = max(abs(sum(arr[c:k * ch:ch]) / k) for c in range(ch))
                if pick == "be":
                    dc_be = max(dc_be, m)
                else:
                    dc_le = max(dc_le, m)
    r_be = be_acc[0] / be_acc[1] if be_acc[1] > 0 else 0.0
    r_le = le_acc[0] / le_acc[1] if le_acc[1] > 0 else 0.0
    if abs(r_be - r_le) < 0.08:
        return dc_le < dc_be
    return r_le > r_be


def _resolve_cue_file(base, fname, disc=""):
    """把 CUE 里的 FILE 名解析成磁盘上真实存在的路径（找不到返回 None）。

    现实里 CUE 与实际文件不同名很常见 —— 例如 CUE 写 ``FILE "Image.bin"``，
    而文件实际叫「游戏音乐cd.bin」（重命名过，或来自别的抓轨工具）。
    严格按名匹配会直接判成「没有 AUDIO 音轨」，整张盘都转不了。
    依次尝试：原名 → 大小写不敏感同名 → 目录里唯一的镜像 → 与 CUE 同名的镜像。
    """
    if not fname:
        return None
    direct = os.path.join(base, fname)
    if os.path.exists(direct):
        return direct
    try:
        names = os.listdir(base)
    except OSError:
        return None
    low = os.path.basename(fname).lower()
    for n in names:
        if n.lower() == low and os.path.isfile(os.path.join(base, n)):
            return os.path.join(base, n)
    imgs = [n for n in names if n.lower().endswith(DISC_IMG_EXTS)
            and os.path.isfile(os.path.join(base, n))]
    if len(imgs) == 1:
        return os.path.join(base, imgs[0])
    if disc:
        for n in imgs:
            if os.path.splitext(n)[0].lower() == disc.lower():
                return os.path.join(base, n)
    return None


def disc_track_plan(cue_or_bin):
    """解析 CUE + 各镜像的**文件大小**，得到 AUDIO 轨清单（不读音频数据）。

    返回 ``[{"path","sec_size","start","end","num","title","disc"}, …]``。
    只做元信息，几十毫秒；``extract_disc_audio`` 与列表里的「曲目数/时长」
    共用这一份切点计算，避免两处各算一套导致漂移。

    ⚠ 切点必须按**同一文件里的下一轨（任意类型）**算，不能只按「下一首
    AUDIO」（v4.38 修）。PCE-CD 的 ``.cue + .iso`` 是单文件镜像，音频轨与
    MODE1 数据轨交织在同一个 2352 字节扇区的文件里：

        TRACK 01 AUDIO       00:00:00
        TRACK 02 MODE1/2352  00:44:65   ← 数据轨夹在两首音频中间
        TRACK 03 AUDIO       02:26:14

    按「下一首 AUDIO」算，轨 1 会一直读到 02:26:14，把 3365~10964 这 7599
    个扇区（101 秒）的**数据**当音频读进来 —— 听感是曲子中间插一段强噪声。
    同理最后一首 AUDIO 的结束点是文件尾时，会把末尾的数据轨也读进来。
    """
    cue_path = _disc_cue_path(cue_or_bin)
    base = os.path.dirname(os.path.abspath(cue_path))
    #: 镜像名（CUE 文件名去扩展名），用于没有 TITLE 时的轨名
    disc = os.path.splitext(os.path.basename(cue_path))[0]

    byfile = {}
    for t in parse_cue(cue_path):
        if t["start"] is None:
            continue                     # 没有 INDEX 01 的轨（如纯 PREGAP）跳过
        byfile.setdefault(t["file"], []).append(t)

    plan = []
    for fname, trs in byfile.items():
        fp = _resolve_cue_file(base, fname, disc)
        if not fp:
            continue
        size = os.path.getsize(fp)
        if size % 2352 == 0:
            sec_size = 2352
        elif size % 2448 == 0:
            sec_size = 2448      # 含 96 字节 subchannel
        else:
            sec_size = 2352
        nsec = size // sec_size
        trs.sort(key=lambda x: x["start"])
        for i, t in enumerate(trs):
            if t["type"] != "AUDIO":
                continue                 # 数据轨只用来当**上一轨的结束点**
            start = t["start"]
            if i + 1 < len(trs):
                nxt = trs[i + 1]
                # 下一轨有 INDEX 00 就切在 pregap 起点（更准），否则切在 INDEX 01
                end = nxt["index0"] if nxt["index0"] is not None else nxt["start"]
            else:
                end = nsec
            if end <= start:
                continue
            plan.append({"path": fp, "sec_size": sec_size, "start": start,
                         "end": end, "num": t["num"], "title": t["title"],
                         "disc": disc})
    return plan


#: 估峰值时每轨取几块、每块几扇区（10 扇区 ≈ 0.13s）。3×10 扇区 ≈ 70KB/轨，
#: 一张 50 轨的盘约 3.5MB —— 列表要即时刷新，不能为了判静音读整轨。
_CDDA_PROBE_CHUNK = 10
_CDDA_PROBE_SPOTS = 3


def _cdda_peak_quick(plan, chunk=_CDDA_PROBE_CHUNK, spots=_CDDA_PROBE_SPOTS):
    """不整轨读，在每轨的头/中/尾各取一小块估峰值（0~1）。读不了返回 None。

    v4.43：主窗列表要判断「这条 AUDIO 轨到底有没有声音」。整轨读一遍几十 MB
    太贵，而静音轨是**整轨**静音（Sangokushi VI 实测 212s 峰值只有 17），
    抽三块足以分开「真音轨」和「静音占位轨」。

    字节序**不能**假设大端：静音轨的残值 17（0x0011）按小端读是 4352（0.13），
    静音轨就会被误判成有声轨。这里两种解释都算一遍取小的（理由见下方注释），
    既避开这个坑，又比 `_detect_cd_endian` 快一个数量级。
    """
    best = 0
    for p in plan:
        n = p["end"] - p["start"]
        if n <= 0:
            continue
        if n <= chunk * spots:
            at = [(p["start"], n)]
        else:
            step = (n - chunk) / float(max(1, spots - 1))
            at = [(p["start"] + int(step * k), chunk) for k in range(spots)]
        buf = bytearray()
        try:
            with open(p["path"], "rb") as fh:
                for s, cnt in at:
                    if p["sec_size"] == SECTOR:
                        fh.seek(s * SECTOR)
                        buf += fh.read(cnt * SECTOR)
                    else:
                        # 带 subchannel 的镜像必须逐扇区跳读
                        for k in range(cnt):
                            fh.seek((s + k) * p["sec_size"])
                            buf += fh.read(SECTOR)
        except OSError:
            return None
        raw = bytes(buf)
        if len(raw) < 8192:
            continue
        raw = raw[:len(raw) // 4 * 4]
        # 两种字节序各取一次峰值，取**小的**那个：真静音轨只有一种解释是
        # 安静的（残值 17），另一种会把 LSB 抬成 4352（0.13 满刻度）；
        # 真音轨两种解释都是满量程，取小也一样。所以这里不必跑
        # `_detect_cd_endian`（它每轨一段 Python 循环，50 轨要 2s+）。
        a_be = _int16(raw, False)
        a_le = _int16(raw, True)
        if a_be:
            m = min(max(max(a_be), -min(a_be)), max(max(a_le), -min(a_le)))
            best = max(best, m)
    return best / 32768.0


def _plan_info(plan):
    """按 CD-DA 轨报元信息（v4.37 起的原始口径）。"""
    total = sum((t["end"] - t["start"]) / 75.0 for t in plan)
    # 大小只算**含音轨**的 BIN（数据轨不算「音频体积」），与 CD 音轨列口径一致
    size = sum(os.path.getsize(f) for f in {t["path"] for t in plan})
    return {"n_tracks": len(plan), "total_secs": total, "size": size,
            "disc": plan[0]["disc"]}


def probe_disc_audio(cue_or_bin):
    """镜像的**元信息**（曲目数 / 总秒数 / 音轨数据大小），不读音频。

    给主窗列表的「曲目数 / 大小 / 时长」三列用。镜像动辄几百 MB，
    这里只 stat 文件 + 解析 CUE 文本，**绝不**为了显示时长去读一遍 PCM。

    ``cue_or_bin`` 可以是 .cue / .bin / **整包文件夹**（目录里所有 CUE 全算，
    与 ``load_source`` 的目录分支口径一致）。

    v4.43：有 AUDIO 轨**不等于**有声音。PS1 盘经常把音乐塞进数据轨的 XA，
    CD-DA 轨是一整条静音占位（Sangokushi VI 实测 212.3s / 峰值 17）。
    那时实际产出的是 XA（276.7s），列表却按 CD-DA 报，两边对不上。
    现在对 AUDIO 轨抽样估峰值，静音就改报 XA 的数字。
    """
    p = str(cue_or_bin)
    if os.path.isdir(p):
        try:
            names = os.listdir(p)
        except OSError:
            return {"n_tracks": 0, "total_secs": 0.0, "size": 0, "disc": ""}
        if not any(n.lower().endswith(DISC_IMG_EXTS) for n in names):
            return {"n_tracks": 0, "total_secs": 0.0, "size": 0, "disc": ""}
        cues = [os.path.join(p, n) for n in sorted(names)
                if n.lower().endswith(".cue")]
    else:
        cues = [p]

    plan = []
    for one in cues:
        try:
            plan += disc_track_plan(one)
        except RuntimeError:
            continue                     # 这个 CUE 没音频轨，跳过看下一个
    # 有 AUDIO 轨且抽样确实有声 -> 老口径直接返回（绝大多数盘走这条，零额外开销）
    if plan:
        pk = _cdda_peak_quick(plan)
        if pk is None or pk >= _CDDA_SILENT_PEAK:
            return _plan_info(plan)
    # 走到这里有两种情况：① 一条 AUDIO 轨都没有（v4.37 的纯数据盘）
    # ② AUDIO 轨整轨静音（v4.43）—— 两者实际产出都是数据轨里的 XA
    for one in cues:
        try:
            xa = probe_xa_audio(one)
        except (RuntimeError, OSError):
            continue
        if not xa["sectors"]:
            continue
        try:
            size = os.path.getsize(_disc_data_bin(one) or "")
        except OSError:
            size = 0
        return {"n_tracks": xa["groups"], "total_secs": xa["total_secs"],
                "size": size,
                "disc": os.path.splitext(os.path.basename(one))[0]}
    # 静音 CD-DA 且数据轨里也没有 XA：照旧报 CD-DA 的数字，别报 0
    return _plan_info(plan) if plan else {"n_tracks": 0, "total_secs": 0.0,
                                          "size": 0, "disc": ""}


def extract_disc_audio(cue_or_bin):
    """从光盘镜像 (BIN+CUE) 抽取所有 AUDIO(CD-DA) 音轨, 返回归一化音轨列表。

    轨名规则（v4.35）：CUE 里有 ``TITLE`` 就用 TITLE；没有就用
    **「镜像名 + 轨号」**（``<cue 文件名去扩展名> - 02``）—— 原来的
    ``track_02`` 完全看不出是哪张盘，用户提过。

    字节序（v4.36）：Red Book 是大端，但现实里的 BIN 有不少是小端。
    这里**每条轨自己判定**（``_detect_cd_endian``），不再硬编码大端 ——
    硬读小端文件会得到满量程白噪（用户反馈的「刺耳的吱吱声」）。
    """
    plan = disc_track_plan(cue_or_bin)
    result = []
    for p in plan:
        pcm = bytearray()
        with open(p["path"], "rb") as fh:
            if p["sec_size"] == SECTOR:
                # 纯音频镜像：整段连续读，省掉「每扇区一次 seek」
                fh.seek(p["start"] * SECTOR)
                pcm += fh.read((p["end"] - p["start"]) * SECTOR)
            else:
                # 带 96 字节 subchannel（2448/扇区）时必须逐扇区跳读
                for s in range(p["start"], p["end"]):
                    fh.seek(s * p["sec_size"])
                    pcm += fh.read(SECTOR)
        name = p["title"] or f"{p['disc']} - {p['num']:02d}"
        le = _detect_cd_endian(bytes(pcm))
        result.append({
            "name": name,
            "pcm": bytes(pcm),
            "little_endian": le,
        })
    if not result:
        raise RuntimeError("未在镜像中找到 AUDIO(CD-DA) 音轨, 仅支持 CD-DA 音轨抽取")
    return result


# --------------------------------------------------------------------------
# XA-ADPCM（PS1 数据轨里的音频）
#
#   很多 PS1 游戏根本没有 CD-DA 音乐，音乐/语音全在数据轨的 Form2 扇区里，
#   用 XA-ADPCM 编码。这类盘的 CD-DA 轨常常是**整轨静音**，只抽 CD-DA 会得到
#   一条长静音 —— 用户听不到原声（实测《三国志英杰传》SLPS-00309 就是这样：
#   Track2 全长 212s，峰值只有 17/32768）。
#
#   扇区布局（MODE2/2352）：
#     12B sync + 4B header + 8B subheader + 2324B data + 4B EDC
#     subheader[2]: bit5(0x20)=Form2、bit2(0x04)=Audio -> XA 音频扇区
#     data 前 2304B = 18 个 sound group x 128B
#
#   ⚠⚠ sound group 的内部布局（v4.42 重写）。这里之前理解错了，后果是
#   **所有 .xa 转出来的 WAV 都能听出旋律、但糊成一团**（v4.41 及更早）：
#   旧代码把 112B 数据当成「4 个 unit x 28B」，并把每个字节的高半字节
#   当左声道、低半字节当右声道 —— 那是 28 帧窗口内的乱序重排，
#   相当于每 0.74ms 把采样洗一次牌，包络还在、细节全毁。
#
#   正确布局（逐字核对 vgmstream/src/coding/xa_decoder.c 的注释 +
#   DuckStation/src/core/cdrom.cpp 的 DecodeXAADPCMChunks，两者一致）：
#     16B 头 = sf0 sf1 sf2 sf3 | 同一份重复 | sf4 sf5 sf6 sf7 | 同一份重复
#              （每 4 字节重复一次，是纠错冗余 —— 所以 [4..11] 恰好是
#                sf0..sf7 的连续 8 字节，两种实现都从这里取）
#     112B 数据 = 28 个 4 字节 word；word[j] 的**第 k 个字节**（k=0..3）
#              低半字节 = 子帧 2k，高半字节 = 子帧 2k+1，时间点都是 j
#     子帧 0/2/4/6 = 左声道，1/3/5/7 = 右声道；
#     子帧 2k / 2k+1 覆盖**第 k 个 28 帧窗口**（j 是窗口内的时间点）
#   → 所以「同一字节的高低半字节」确实是一对 L/R，但**相邻字节不是相邻时刻**，
#     相邻时刻在**相邻 word** 里。这就是旧代码错的地方。
#
#   参数字节：高 4 位 = filter(0~4)，低 4 位 = shift(0~12；>12 一律按 9)。
#   这条顺序是对的（按「高=shift」解会让 filter 落在 11~12 非法值）。
#   ADPCM：s = (nib << 12) >> shift; s += (k0[f]*h1 + k1[f]*h2 + 32) >> 6
#          h1/h2 **每个声道各一份**，跨组、跨扇区连续递推，且**不钳位**
#          （只钳位输出 —— vgmstream 明确注释 don't clamp hist）
#   采样率固定 37800Hz（4bit 立体声），要重采样到 MSU-1/声卡的 44.1k。
# --------------------------------------------------------------------------

#: 补到 16 项：filter 是 4bit 字段，0~3 是规范值、4 是 PSX 扩展，
#: 5~15 按 0 处理（vgmstream 用 VGM_ASSERT 报「incorrect coefs」后照样取 0）
_XA_K0 = (0, 60, 115, 98, 122) + (0,) * 11
_XA_K1 = (0, 0, -52, -55, -60) + (0,) * 11
_XA_RATE = 37800
_XA_PER_SECTOR = 2016                 # 每扇区每声道帧数：18 组 x 112 帧
_XA_SECTOR_SECS = _XA_PER_SECTOR / _XA_RATE      # ≈0.0533s


def _disc_data_bin(cue_or_bin):
    """镜像里**数据轨**（非 AUDIO）指向的镜像文件；纯音频盘返回 None。

    XA 只可能出现在 MODE1/MODE2 数据轨里。如果整张盘都是 AUDIO 轨，就
    没有数据轨可扫 —— 否则会把音频轨的字节当成扇区头去扫，静音轨那种
    ``F0 FF F0 FF`` 填充恰好能骗过 subheader 判定，解出一堆假音频。
    """
    cue = _disc_cue_path(cue_or_bin)
    base = os.path.dirname(os.path.abspath(cue))
    disc = os.path.splitext(os.path.basename(cue))[0]
    fallback = None
    for t in parse_cue(cue):
        fp = _resolve_cue_file(base, t["file"], disc)
        if not fp:
            continue
        if t.get("type") != "AUDIO":
            return fp
        if fallback is None:
            fallback = fp
    return None


#: CD 扇区同步头：00 + FF x10 + 00
_CD_SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


def _xa_scan_start(fh):
    """返回 XA 扇区扫描的起始字节偏移。

    两种 ``.xa`` 并存：

    * **裸扇区流** —— 从 0 开始就是 2352 字节扇区（光盘镜像里那种）
    * **RIFF 包裹** —— ``RIFF <size> CDXA`` + ``fmt `` + ``data``，音频从
      ``data`` 的负载开始。JoshW 的 PS1 包解压出来就是这个样子（实测
      Armored Core 的 15 个 ``.XA`` 全是 44 字节头 + 2101 个整扇区）。

    ⚠ 偏移不算出来就直接按 2352 步进扫，会**整包一个扇区都匹配不上**
    （44 字节的错位让每次步进都落在扇区中间），表现就是「文件里没有
    XA 音频扇区」。
    """
    try:
        fh.seek(0, 2)
        total = fh.tell()
        fh.seek(0)
        head = fh.read(12)
    except OSError:
        return 0
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"CDXA":
        return 0
    off = 12
    while off + 8 <= total:
        fh.seek(off)
        hdr = fh.read(8)
        if len(hdr) < 8:
            break
        cid = hdr[:4]
        sz = struct.unpack("<I", hdr[4:8])[0]
        if cid == b"data":
            return off + 8
        if sz <= 0:
            break
        off += 8 + sz + (sz & 1)
    # 头坏得没法按分块走：在开头 8KB 里找第一个同步头，找到就从那儿开始
    fh.seek(0)
    blob = fh.read(min(total, 8192))
    i = blob.find(_CD_SYNC)
    return i if i >= 0 else 0


def _xa_subheader_ok(sub):
    """subheader 像不像 XA 音频扇区的头。

    ⚠ **不能要求 8 字节两份副本完全相等**。标准 CD-XA 里 16..19 和 20..23
    是同一份 subheader 的两份拷贝，但 JoshW 的 PS1 ``.XA`` 里**channel 字节
    故意不同** —— 实测 Armored Core 的 BGM01.XA 全部 2101 个扇区都是
    ``01 00 64 01 | 01 02 64 01``。卡「完全相等」会把整包判成没有音频。

    所以只比 file / submode / coding 三个字节，再加上编码只能是 4bit
    （mono/stereo）—— 本工程的 ``_xa_decode_group`` 只实现了 4bit。

    ⚠ coding（sub[3]）**不能卡 ``<= 1``**。CD-XA 的 coding 字节低位是声道数
    （0=mono, 1=stereo），高位是采样率/保留位；真实包里有 ``0x09``（stereo +
    保留位置位）这类非常规值，`_xa_decode_group` 不看 coding、固定按
    4bit 立体声解，这里卡死会把整包误判成没有音频（实测踩过：
    test_psx_txtp.py 的 coding=0x09 样本返回空 groups → IndexError）。
    正确判据：**低 2 位 = 声道数（0/1），高位置位不影响** —— 解码器
    只实现 4bit，凡是声道数合法就当它是可解的 XA 音频。
    """
    if len(sub) < 8:
        return False
    return (sub[0] == sub[4] and sub[2] == sub[6]
            and sub[3] == sub[7] and (sub[3] & 0x03) <= 1)


def xa_sector_groups(bin_path, sec_size=SECTOR, start=None):
    """单遍扫描镜像，按 (file, channel) 收集 XA 音频扇区的**文件偏移**。

    只看扇区头，不搬音频数据；300MB 的盘约 0.3 秒。

    判定要求四条同时成立，缺一不可：
      1. 12 字节同步头 ``00 FF*10 00``
      2. 第 15 字节（mode）= 2（MODE2）
      3. subheader 的 submode 同时置了 Form2(0x20) 与 audio(0x04)
      4. subheader 两份副本在 file/submode/coding 上一致（见
         :func:`_xa_subheader_ok`）
    只看 submode 位是不够的 —— 静音音频轨的 ``F0 FF`` 填充会全部命中。

    ``start`` 留空时自动判断（裸流 0 / RIFF 包裹则跳过 data 头）。
    """
    groups = {}
    with open(bin_path, "rb") as fh:
        if start is None:
            start = _xa_scan_start(fh)
        base = start
        fh.seek(start)
        while True:
            block = fh.read(sec_size * 2048)
            if not block:
                break
            n = len(block) // sec_size
            for i in range(n):
                o = i * sec_size
                if block[o:o + 12] != _CD_SYNC:
                    continue
                if block[o + 15] != 2:
                    continue
                sub = block[o + 16:o + 24]
                if not _xa_subheader_ok(sub):
                    continue
                if (sub[2] & 0x20) and (sub[2] & 0x04):
                    groups.setdefault((sub[0], sub[1]), []).append(base + o)
            base += n * sec_size
    return groups


def probe_xa_audio(cue_or_bin):
    """XA 元信息（不读音频）：扇区数 / 通道数 / 可解时长（秒）。"""
    try:
        bp = _disc_data_bin(cue_or_bin)
    except RuntimeError:
        return {"sectors": 0, "groups": 0, "total_secs": 0.0}
    if not bp:
        return {"sectors": 0, "groups": 0, "total_secs": 0.0}
    groups = xa_sector_groups(bp)
    n = sum(len(v) for v in groups.values())
    return {"sectors": n, "groups": len(groups), "total_secs": n * _XA_SECTOR_SECS}


def _xa_decode_group(bin_path, offsets, max_frames=None):
    """按扇区顺序把一组 XA 解成两个 int16 列表（37800Hz 的 L/R）。

    遍历顺序必须是「sound group -> 28 帧窗口(4) -> 声道(2) -> 窗口内 28 点」，
    因为预测器历史 h1/h2 是**按声道**跨组、跨扇区连续递推的；顺序错了
    声音就糊（布局说明见上方注释）。

    内层是纯 Python 逐采样递推（ADPCM 有反馈，没法向量化），
    实测约 1.4M 采样/秒；15 分钟的 XA 约 35 秒。
    """
    left = array.array("h")
    right = array.array("h")
    h1 = [0, 0]                       # 每个声道各一份预测器历史
    h2 = [0, 0]
    k0, k1 = _XA_K0, _XA_K1
    stop = max_frames is not None
    with open(bin_path, "rb") as fh:
        for off in offsets:
            if stop and len(left) >= max_frames:
                break
            fh.seek(off)
            sec = fh.read(SECTOR)
            if len(sec) < SECTOR:
                break
            pay = sec[24:24 + 2304]
            for g in range(18):
                grp = pay[g * 128:(g + 1) * 128]
                for win in range(4):
                    for ch in (0, 1):
                        # 子帧号 = win*2 + ch；参数区 [4..11] 即 sf0..sf7
                        sp = grp[4 + win * 2 + ch]
                        sh = sp & 0x0F
                        if sh > 12:
                            sh = 9
                        f = sp >> 4
                        c0 = k0[f]
                        c1 = k1[f]
                        p1 = h1[ch]
                        p2 = h2[ch]
                        out = left if ch == 0 else right
                        nshift = 0 if ch == 0 else 4
                        base = 16 + win        # word[j] 的第 win 个字节
                        for j in range(0, 112, 4):
                            n = (grp[base + j] >> nshift) & 0x0F
                            if n >= 8:
                                n -= 16
                            s = (n << 12) >> sh
                            s += (c0 * p1 + c1 * p2 + 32) >> 6
                            p2 = p1
                            p1 = s             # 历史不钳位
                            if s < -32768:
                                s = -32768
                            elif s > 32767:
                                s = 32767
                            out.append(s)
                        h1[ch] = p1
                        h2[ch] = p2
    return left, right


def _xa_ratio(src_rate, dst_rate):
    """把采样率比化成整数 up/down（37800→44100 就是 7/6）。"""
    from fractions import Fraction
    f = Fraction(int(dst_rate), int(src_rate)).limit_denominator(1024)
    return f.numerator, f.denominator


def _xa_poly_filter(up, down, taps=16):
    """多相低通：长度 up*taps，通带截止到源信号奈奎斯特。

    零插值会把幅度压低 up 倍，所以归一化成 sum(h)==up。
    截止取 0.46/max(up,down)（略低于理论值）是为了给过渡带留余量 ——
    滤不干净的镜频会在抽取后折回通带，那正是「线性插值听起来发闷发毛」的来源。
    """
    N = up * taps
    n = _np.arange(N, dtype=_np.float64) - (N - 1) / 2.0
    fc = 0.46 / max(up, down)
    h = _np.sinc(2.0 * fc * n) * _np.hamming(N)
    return h * (up / h.sum())


def _xa_to_pcm(left, right, src_rate=_XA_RATE, dst_rate=44100):
    """37800Hz 的 L/R -> 44.1k 小端立体声 PCM 字节。

    v4.42 起换成 **多相 FIR 重采样**（7:6）。原先是最邻近+线性插值，
    线性插值等价于一个 sinc² 低通，高频被压掉、镜频又折回通带，
    听感是「闷 + 毛边」。分块是为了不把整条轨一次性展开成 float64
    （15 分钟要 300MB+）。
    """
    if not _HAVE_NUMPY:
        raise RuntimeError("XA 抽取需要 numpy（重采样）")
    n = len(left)
    if n < 2:
        return b""
    up, down = _xa_ratio(src_rate, dst_rate)
    if up == down:
        inter = _np.empty((n, 2), dtype="<i2")
        inter[:, 0] = _np.frombuffer(left, dtype="<i2")
        inter[:, 1] = _np.frombuffer(right, dtype="<i2")
        return inter.tobytes()

    taps = 16
    h = _xa_poly_filter(up, down, taps)
    pad = taps + up
    la = _np.concatenate((_np.zeros(pad), _np.frombuffer(left, dtype="<i2")
                          .astype(_np.float64), _np.zeros(pad)))
    ra = _np.concatenate((_np.zeros(pad), _np.frombuffer(right, dtype="<i2")
                          .astype(_np.float64), _np.zeros(pad)))
    n_out = n * up // down
    chunks = []
    step = 1 << 18
    pos = 0
    while pos < n_out:
        cnt = min(step, n_out - pos)
        t = _np.arange(pos, pos + cnt, dtype=_np.int64) * down
        q = t // up + pad
        ph = t - (t // up) * up
        acc_l = _np.zeros(cnt, dtype=_np.float64)
        acc_r = _np.zeros(cnt, dtype=_np.float64)
        for j in range(taps):
            g = h[ph + up * j]
            acc_l += g * la[q - j]
            acc_r += g * ra[q - j]
        _np.clip(acc_l, -32768.0, 32767.0, out=acc_l)
        _np.clip(acc_r, -32768.0, 32767.0, out=acc_r)
        inter = _np.empty((cnt, 2), dtype="<i2")
        inter[:, 0] = acc_l.astype(_np.int16)
        inter[:, 1] = acc_r.astype(_np.int16)
        chunks.append(inter.tobytes())
        pos += cnt
    return b"".join(chunks)


#: 超过这个字节数就把解出来的 PCM 落盘（`_SpoolPcm`），不占内存。
#: Silent Hill 那种纯数据盘 XA 有 63.8 分钟 ≈ 675MB，全放内存会顶到 1GB+。
_XA_SPOOL_BYTES = 128 << 20

# ---------------------------------------------------------------------------
# XA「音乐 / 音效语音」判定（v4.39）
#
# 一张 PS1 盘的 XA 里，BGM、音效、语音是**混在同一个池子里**的，每条
# (file, channel) 流只是「同一通道上的连续音频」。所以一张盘动辄两三百条流，
# 而用户要的 OST 通常只有几十条（DACLUA 实测 229 条 / 154 分钟，其中音乐
# 33 条 / 118 分钟）。
#
# XA 没有任何元数据能说明哪条是音乐，只能靠可测量特征推断。三条依据：
#   ① **时长**：BGM 一般几十秒到几分钟；音效/语音多数在 20 秒内。
#   ② **立体声宽度**：BGM 是立体声混音（左右相关常在 0.2~0.5）；
#      语音/音效多半是单声道复制到双声道（相关 0.75~1.0）。
#   ③ **≥60 秒的连续音频一律算音乐** —— 单声道 BGM 也真实存在（DACLUA 的
#      f20/ch06 就是 145 秒、相关 0.575），只靠 ② 会把它误杀。
#
# 标定样本：
#   DACLUA   ch00-05（30 条 BGM）：时长 103~510s，相关 0.03~0.73
#            ch06/07（199 条）：中位时长 9~11s，相关中位 0.75~0.77
#   Silent Hill  200 条：5 条 ≥60s，其余多为 5~20s 的音效/语音
# 阈值取 20s / 60s / 0.6 都落在两簇中间的空档上，不是卡着边界切的。
# ---------------------------------------------------------------------------
_XA_MUSIC_MIN_SECS = 20.0      # 短于这个一律当音效/语音
_XA_MUSIC_LONG_SECS = 60.0     # 长于这个一律当音乐（连续长音频基本是 BGM）
_XA_MUSIC_MAX_CORR = 0.6       # L/R 相关高于这个 = 近单声道（语音/音效）
_XA_MUSIC_PROBE_SECS = 4.0     # 判定只解前 4 秒，不必整条解一遍


def _xa_stream_feature(bin_path, offsets, probe_secs=_XA_MUSIC_PROBE_SECS):
    """解前几秒，返回 ``(rms, 左右声道相关系数)``，给音乐/音效判定用。

    相关系数用去均值后的 Pearson 系数：立体声混音左右差异大 -> 低相关；
    单声道复制到双声道 -> 接近 +1.0。整条解一遍太贵（几百条流），
    前 4 秒足够——BGM 的开头不会一直是纯静音。
    """
    n = max(1, int(probe_secs / _XA_SECTOR_SECS))
    left, right = _xa_decode_group(bin_path, offsets[:n])
    k = min(len(left), len(right))
    if k < 64:
        return 0.0, 1.0
    ml = sum(left[:k]) / k
    mr = sum(right[:k]) / k
    vl = [v - ml for v in left[:k]]
    vr = [v - mr for v in right[:k]]
    rms = (sum(v * v for v in vl) / k) ** 0.5 / 32768.0
    num = sum(a * b for a, b in zip(vl, vr))
    den = (sum(a * a for a in vl) * sum(b * b for b in vr)) ** 0.5
    return rms, (num / den if den else 1.0)


def xa_is_music(secs, corr):
    """按「时长 + 立体声宽度」判定一条 XA 流是不是音乐。规则见上方常量注释。"""
    if secs >= _XA_MUSIC_LONG_SECS:
        return True
    return secs >= _XA_MUSIC_MIN_SECS and corr < _XA_MUSIC_MAX_CORR


# ---------------------------------------------------------------------------
# XA 静音间隔切曲（v4.43）
#
# PS1 的 XA 流里**没有 EOR 曲界标记**（Xenogears 13542 扇区 / FF7 15856 扇区 /
# MGS 2899 扇区实测 EOR 全是 0），一首 BGM 接下一首只靠「中间留一段静音」。
# 所以整条流解出来是一条几十分钟的长 WAV，用户要按曲听只能自己剪。
#
# 这条路径给一个**可选**的切分：按 100ms 窗 RMS 找静音段，在静音中点下刀。
# 切点落在静音里 -> 不会在波形中间断开（爆音），也不必为每段单独重采样
# （那样每段边界都会缺 pad，出现起始瞬态）。
#
# 阈值标定（三张真实盘，100ms 窗 RMS 直方图）：
#   Sangokushi VI   静音簇 <0.001 占 6.8%，0.001~0.008 只有 41 窗（淡入淡出）
#   Rockman X4      静音簇 <0.001 占 0.4%，0.001~0.008 只有 36 窗
#   MGS Disc 1      静音簇 <0.001 占 1.4%，0.001~0.008 只有 22 窗
# 取 0.004（-48dBFS）落在那个稀疏带里：比静音簇高 8 倍，又低于淡出尾巴。
# ---------------------------------------------------------------------------
_XA_SPLIT_WIN = 0.10          # 判定窗长（秒）
_XA_SPLIT_RMS = 0.004         # 窗 RMS 低于满刻度的这个比例算静音（-48dBFS）
_XA_SPLIT_MIN_GAP = 1.5       # 连续静音至少这么长才当曲界（避免切碎）
_XA_SPLIT_MIN_TRACK = 5.0     # 切出来的段短于这个就并到相邻段
_XA_SPLIT_SILENT_RATIO = 0.95  # 整段 95% 以上是静音 -> 当填充，直接丢


def _pcm_quiet_flags(pcm, win_frames, n_win, thr):
    """每 ``win_frames`` 帧算一次 RMS，返回「是否静音」的布尔列表。

    numpy 在就整块向量化（24M 采样 ~30ms）；没有 numpy 时按步长 16 抽样，
    判静音不需要逐采样精确（实测静音簇比阈值低 8 倍，抽样不影响结论）。
    """
    if _HAVE_NUMPY:
        cnt = n_win * win_frames * 2
        a = _np.frombuffer(pcm, dtype="<i2", count=cnt).astype(_np.float64)
        a = a.reshape(n_win, win_frames * 2)
        r = _np.sqrt((a * a).mean(axis=1)) / 32768.0
        return [bool(v) for v in (r < thr)]
    a = _int16(pcm[:n_win * win_frames * 4], True)
    out = []
    step = 16
    for w in range(n_win):
        seg = a[w * win_frames * 2:(w + 1) * win_frames * 2:step]
        if not len(seg):
            out.append(True)
            continue
        s = 0
        for v in seg:
            s += v * v
        out.append((s / float(len(seg))) ** 0.5 / 32768.0 < thr)
    return out


def _pcm_silent_ratio(pcm, start, end, win_frames, thr):
    """[start, end) 这段里静音窗的占比（0~1）。"""
    n = (end - start) // win_frames
    if n <= 0:
        return 1.0
    flags = _pcm_quiet_flags(pcm[start * 4:end * 4], win_frames, n, thr)
    return sum(1 for f in flags if f) / float(n)


def pcm_split_points(pcm, win=_XA_SPLIT_WIN, thr=_XA_SPLIT_RMS,
                     min_gap=_XA_SPLIT_MIN_GAP,
                     min_track=_XA_SPLIT_MIN_TRACK, rate=44100):
    """按静音间隔把 16bit/立体声 PCM 切段，返回 ``[(start, end), ...]``（帧）。

    ``pcm`` 必须是**已经重采样到 44.1k 的小端 16bit 立体声**字节。
    切点取静音段的**中点**，所以段与段之间不会丢音频，也不会在波形中间断开。
    """
    frames = len(pcm) // 4
    if frames <= 0:
        return []
    win_f = max(1, int(win * rate))
    n_win = frames // win_f
    if n_win < 2:
        return [(0, frames)]
    quiet = _pcm_quiet_flags(pcm, win_f, n_win, thr)

    # 连续静音窗 -> 候选切点（静音段中点）。gap_w 至少 1 窗，避免除零
    gap_w = max(1, int(round(min_gap / win)))
    cuts = []
    i = 0
    while i < n_win:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < n_win and quiet[j]:
            j += 1
        if j - i >= gap_w:
            cuts.append(((i + j) // 2) * win_f)
        i = j

    bounds = [0] + cuts + [frames]
    segs = [(bounds[k], bounds[k + 1]) for k in range(len(bounds) - 1)]

    # 太短的段并到相邻段：片头静音会并进第一首，片尾残段并进最后一首
    min_f = int(min_track * rate)
    merged = []
    for s, e in segs:
        if merged and ((e - s) < min_f or (merged[-1][1] - merged[-1][0]) < min_f):
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))

    # 整段几乎全是静音（片头/片尾填充）-> 丢掉，别产出「空曲」
    out = [(s, e) for s, e in merged
           if _pcm_silent_ratio(pcm, s, e, win_f, thr) < _XA_SPLIT_SILENT_RATIO]
    return out or [(0, frames)]


def extract_xa_audio(cue_or_bin, max_secs=None, on_progress=None,
                     music_only=False, stats=None, split_silence=False):
    """把镜像数据轨里的 XA-ADPCM 解成 16bit/44.1k/立体声音轨（每通道一条）。

    给「CD-DA 轨是静音、甚至根本没有 AUDIO 轨」的 PS1 盘兜底 —— 见上方格式注释。
    一个 (file, channel) 就是一条独立音频流，按 (file, channel) 排序输出，
    这样写出来的文件序号也是稳定的。

    ``on_progress`` 可选，签名 ``fn(0.0~1.0)``；纯数据盘的 XA 可能有好几十分钟
    （Silent Hill 实测 63.8 分钟 / 200 条流），没有进度会让人以为卡死。

    ``music_only``（v4.39）：只导出音乐，按 ``xa_is_music`` 把音效/语音滤掉。
    判定要先把每条流解 4 秒（DACLUA 229 条约 49 秒），所以默认**关闭** ——
    不做静默丢数据。GUI 的「XA 只导音乐」勾上才传 True。

    ``stats`` 可选 dict，会被填上 ``total`` / ``kept`` / ``skipped``，
    供 GUI 在日志里说清「滤掉了多少条」。

    ``split_silence``（v4.43）：按静音间隔把每条流切成多段，一段一个文件。
    默认**关闭** —— 只有盘里确实用静音当曲界时才有意义，乱切会毁数据
    （Sangokushi VI 实测能切 4 段，但有的盘整条就是一首 12 分钟组曲）。
    开启时 ``stats`` 会多出 ``split_streams``（被切开的流数）与
    ``segments``（切出来的总段数）。

    总体积超过 ``_XA_SPOOL_BYTES`` 时，PCM 逐轨落盘成 ``_SpoolPcm``
    （与 chip_decode 的大音源同一套机制），由 ``cleanup_spc_temp`` 收尾。
    """
    bp = _disc_data_bin(cue_or_bin)
    if not bp:
        raise RuntimeError("未找到镜像数据轨, 无法抽取 XA 音频")
    groups = xa_sector_groups(bp)
    if not groups:
        raise RuntimeError("镜像里没有 XA-ADPCM 音频扇区")
    disc = os.path.splitext(os.path.basename(_disc_cue_path(cue_or_bin)))[0]
    max_frames = int(max_secs * _XA_RATE) if max_secs else None
    result = []
    multi = len(groups) > 1
    keys = sorted(groups)                       # (file, channel) 升序

    # ---- v4.39: 只导音乐时，先给每条流判一次，再决定解哪些 ----
    n_total = len(keys)
    n_skipped = 0
    if music_only:
        kept = []
        for i, key in enumerate(keys):
            secs = len(groups[key]) * _XA_SECTOR_SECS
            if secs >= _XA_MUSIC_LONG_SECS:
                kept.append(key)                # 长流不必再解，直接算音乐
            else:
                _, corr = _xa_stream_feature(bp, groups[key])
                if xa_is_music(secs, corr):
                    kept.append(key)
                else:
                    n_skipped += 1
            if on_progress:
                try:
                    on_progress(0.5 * (i + 1) / float(n_total))
                except Exception:               # noqa: BLE001
                    pass
        keys = kept
        if not keys:
            raise RuntimeError(
                "XA 里没有判定为音乐的流（都是音效/语音）；"
                "可以关掉「XA 只导音乐」再试一次")
    if stats is not None:
        stats["total"] = n_total
        stats["kept"] = len(keys)
        stats["skipped"] = n_skipped

    total_bytes = (sum(len(groups[k]) for k in keys) * _XA_PER_SECTOR
                   * 4 * 44100 // _XA_RATE)
    spool_dir = None
    if total_bytes > _XA_SPOOL_BYTES:
        spool_dir = tempfile.mkdtemp(prefix="xa_pcm_")

    def _emit(data, tname, secs):
        """落盘（必要时）并追加一条音轨。spool 的编号用 result 长度，保持稳定。"""
        pcm_out = data
        if spool_dir:
            path = os.path.join(spool_dir, f"{len(result):04d}.pcm")
            with open(path, "wb") as fh:
                fh.write(data)
            pcm_out = _SpoolPcm(path, spool_dir)
        result.append({
            "name": tname,
            "pcm": pcm_out,
            "little_endian": True,
            "xa_secs": secs,
        })

    n_split = 0
    try:
        for i, key in enumerate(keys):
            fno, ch = key
            left, right = _xa_decode_group(bp, groups[key], max_frames)
            if on_progress:
                try:
                    base = 0.5 if music_only else 0.0
                    on_progress(base + (1.0 - base) * (i + 1) / float(len(keys)))
                except Exception:               # noqa: BLE001 回调出错不影响解码
                    pass
            if len(left) < _XA_PER_SECTOR:
                continue
            name = (f"{disc} - XA{fno:02d}-{ch:02d}" if multi
                    else f"{disc} - XA")
            pcm = _xa_to_pcm(left, right)
            # v4.43: 按静音切曲。只在切出 2 段以上时才改名，
            # 切不动的流（整条一首）保持原名，避免用户看到「… XA 01」这种假分轨。
            segs = pcm_split_points(pcm) if split_silence else None
            if segs and len(segs) > 1:
                n_split += 1
                for k, (f0, f1) in enumerate(segs, 1):
                    _emit(pcm[f0 * 4:f1 * 4], f"{name} {k:02d}",
                          (f1 - f0) / float(44100))
            else:
                _emit(pcm, name, len(left) / float(_XA_RATE))
    except Exception:
        if spool_dir:
            shutil.rmtree(spool_dir, ignore_errors=True)
        raise
    if not result:
        raise RuntimeError("XA 扇区解不出有效音频")
    if stats is not None and split_silence:
        stats["split_streams"] = n_split
        stats["segments"] = len(result)
    return result


def tracks_peak(tracks):
    """所有音轨的最大绝对采样值（0.0~1.0）。用来判断 CD-DA 轨到底有没有内容。"""
    peak = 0
    for t in tracks:
        raw = t["pcm"]
        if hasattr(raw, "read_all"):
            raw = raw.read_all()
        a = array.array("h")
        a.frombytes(raw[:len(raw) // 2 * 2])
        if sys.byteorder != "little" and t["little_endian"]:
            a.byteswap()
        elif sys.byteorder == "little" and not t["little_endian"]:
            a.byteswap()
        if a:
            peak = max(peak, max(max(a), -min(a)))
    return peak / 32768.0


#: CD-DA 轨峰值低于这个值就当「没有内容」（-46dBFS）。真实音轨不会这么安静，
#: 而「静音轨」实测只有 17/32768 ≈ 0.0005，两者差两个数量级。
_CDDA_SILENT_PEAK = 0.005


def _try_xa(cue_paths, on_progress=None, music_only=False, stats=None,
            split_silence=False):
    """逐个 CUE 尝试抽 XA，任何一个失败都只是跳过，不打断整批。"""
    out = []
    for c in cue_paths:
        try:
            out += extract_xa_audio(c, on_progress=on_progress,
                                    music_only=music_only, stats=stats,
                                    split_silence=split_silence)
        except (RuntimeError, OSError):
            continue
    return out


def _cdda_or_xa(tracks, cue_paths, on_progress=None, music_only=False,
                stats=None, split_silence=False):
    """CD-DA 抽出来是静音时，改用数据轨里的 XA-ADPCM。

    判据用峰值：真实 CD-DA 音轨峰值通常在 0.5 以上，而「静音轨」只有
    0.0005 量级，中间空着两个数量级，0.005（-46dBFS）足以分开。这样既
    不会误伤正常 CD-DA 盘，又能救回「音乐全在 XA 里」的 PS1 盘。

    这里**不打印**：本函数也会被 GUI 调用，PyInstaller 窗口模式下
    ``sys.stdout`` 是 None，print 会直接抛异常。要提示由调用方按轨名判断
    （XA 轨的名字里带 `` - XA``）。
    """
    try:
        pk = tracks_peak(tracks)
    except Exception:                      # noqa: BLE001 读不了就按原样返回
        return tracks
    if pk >= _CDDA_SILENT_PEAK:
        return tracks
    xa = _try_xa(cue_paths, on_progress, music_only, stats, split_silence)
    return xa or tracks


def load_mp3(path):
    """MP3 -> 归一化音轨 (解码并转换到 16-bit/44.1k/立体声, miniaudio 内置 dr_mp3)."""
    try:
        import miniaudio
    except ImportError as e:
        raise RuntimeError(f"需要 miniaudio 库解码 MP3 (pip install miniaudio): {e}")
    # ⚠ miniaudio.decode_file 在 Windows 上按 **ANSI 路径**打开文件，
    #   路径里只要有一个非 ASCII 字符（如「工程备份」「静音.mp3」）就会报
    #   DecodeError('failed to decode file', -7)（-7 = MA_DOES_NOT_EXIST），
    #   文件明明存在也读不到。实测：同一份字节，纯 ASCII 路径 OK，中文路径必败。
    #   兜底改走「自己读字节 + 内存解码」，完全不碰文件系统 API，与路径无关。
    try:
        d = miniaudio.decode_file(path,
                                  output_format=miniaudio.SampleFormat.SIGNED16,
                                  nchannels=2, sample_rate=44100)
    except Exception:                       # noqa: BLE001 见上：非 ASCII 路径必抛
        with open(path, "rb") as fh:
            raw = fh.read()
        d = miniaudio.decode(raw,
                             output_format=miniaudio.SampleFormat.SIGNED16,
                             nchannels=2, sample_rate=44100)
    pcm = array.array("h", d.samples).tobytes()
    return [{
        "name": os.path.splitext(os.path.basename(path))[0],
        "pcm": pcm,
        "little_endian": True,
    }]


def load_stream(path):
    """流式音频（.adx / .mod）-> 归一化音轨（16bit/44.1k/立体声 小端 PCM）。

    这两类既不是 CD 音轨也不是芯片日志，解码器在 :mod:`stream_audio` /
    :mod:`mod_audio` 里。**惰性 import** 的理由与 chip_decode 那条一样：
    stream_audio 内部才反向依赖本模块，顶层 import 会成环。
    """
    import stream_audio
    pcm = stream_audio.decode_pcm(path)
    if not pcm:
        raise RuntimeError(f"{path}: 解出来是空音频")
    return [{
        "name": os.path.splitext(os.path.basename(path))[0],
        "pcm": pcm,
        "little_endian": True,
    }]


# --------------------------------------------------------------------------
# 模式 0 (源): SPC (SNES-SPC700) 解码
#   用 libgme (game-music-emu) 把 SPC 解码为 16-bit/44.1k/立体声 小端 PCM。
#   SPC 原生采样率 32kHz, libgme 在打开时重采样到目标采样率 (这里 44100)。
#   注意: 个别损坏/不受支持的 SPC 会让 libgme 的 gme_play 卡死,
#   因此解码放在独立子进程中进行并用超时保护, 卡死时跳过而非冻结程序。
#   需要同目录的 libgme.dll 及其依赖 (mingw64 构建):
#     libstdc++-6.dll, zlib1.dll, libgcc_s_seh-1.dll, libwinpthread-1.dll
# --------------------------------------------------------------------------

_SPC_TIMEOUT = 60  # 单个 SPC 解码超时基数(秒), 防止卡死冻结程序

#: 是否运行在「已打包」的程序里。
#:
#: ⚠ 不能只判 ``sys.frozen`` —— 那是 PyInstaller 设的。**Nuitka 不设它**，
#: 只注入一个模块级 ``__compiled__``。只判 frozen 的话，Nuitka 包里
#: ``_spc_worker_cmd`` 会走「脚本模式」分支拼出
#: ``[exe, <临时目录>/msu1_to_cd.py, --spc-decode, …]`` —— 那个 .py 根本不存在
#: （已编译），子进程直接 WinError 2，表现为「一转换就失败：系统找不到指定的文件」。
#: 2026-10-22 在 MSU1Converter-web.exe 上实测踩到。
_IS_FROZEN = bool(getattr(sys, "frozen", False)) or ("__compiled__" in globals())


def worker_launcher():
    """「把自己再调起来当 worker」用的可执行路径。

    ⚠⚠ 不能直接用 ``sys.executable``。实测（Nuitka 4.3.1 ``--onefile``）：

        sys.executable   = C:\\…\\Temp\\onefile_5044_…\\python.exe   ← **不存在**
        sys.argv[0]      = D:\\…\\MSU1Converter-web.exe            ← 真身
        os.path.isfile(sys.executable) == False

    onefile 的 bootloader 把 ``sys.executable`` 指到一个虚拟路径，直接拿去
    ``subprocess.run`` 就是 ``[WinError 2] 系统找不到指定的文件``。
    PyInstaller 相反（两者都是 exe），所以按「谁真在磁盘上就用谁」来选，
    两边都对。

    2026-10-22 在 MSU1Converter-web.exe 上踩到：加音源正常、一按转换就
    「转换失败：系统找不到指定的文件」。
    """
    for cand in (sys.argv[0], sys.executable):
        try:
            if cand and os.path.isfile(cand):
                return os.path.abspath(cand)
        except OSError:
            continue
    return sys.executable

# 结束判定: SPC 的 ID666 元数据并不可靠(实测大量文件 length=0/几百毫秒, 且
# song/game/artist 标签全为空), 因此改用"尾部静音"判定播放结束。
#   _SPC_SIL_PEAK  连续静音块的峰值阈值(16bit 满量程 32767)
#   _SPC_SIL_SECS  连续静音达到该秒数即认为曲子结束
#   _SPC_MIN_SECS  最短播放时长, 防止开头即静音被误判为结束
#   _SPC_MAX_SECS  最长播放时长, 循环曲/无终点曲的兜底上限
_SPC_SIL_PEAK = 256
_SPC_SIL_SECS = 2.0
_SPC_LEAD_SIL_SECS = 10.0   # 开头全静音的上限(超过则视为空曲)
_SPC_MIN_SECS = 2.0
_SPC_MAX_SECS = 600
_SPC_CHUNK = 4096  # 每次 gme_play 的帧数(必须 <= 65535)

# 多格式芯片音源支持的扩展名(转出 chip_formats, 单一真源)。
# chip_formats 是纯解析层(零 ctypes/零 DLL), 在这里做**惰性 import** 以免
# 把它的加载塞进核心模块的导入期, 同时保证两边常量永远一致。
def _chip_exts():
    try:
        import chip_formats
        return tuple(chip_formats.CHIP_EXTS), tuple(chip_formats.P0_CHIP_EXTS)
    except Exception:                                   # noqa: BLE001
        return ((".spc", ".nsf", ".nsfe", ".gbs", ".vgm", ".vgz", ".hes"),
                (".nsf", ".nsfe", ".gbs", ".vgm", ".vgz", ".hes"))


CHIP_EXTS, P0_CHIP_EXTS = _chip_exts()

_GME = None  # 进程内句柄 (仅用于自检/验证, 真正解码走子进程)


class _GmeInfo(ctypes.Structure):
    """gme.h的 ``struct gme_info_t`` —— **必须与上游逐字段对齐**。

    ⚠⚠ 这里曾经写错过，而且错得**毫无症状**（不崩、不报错、静默污染数据）：
      旧版把 ``track_count`` 放在最前面，后面是 ``i4..i15``。但上游结构
      **第一个字段就是 length，根本没有 track_count**，于是整个结构错位一个
      int —— 所有 const char* 也跟着偏移，实测全部读成 None、所有格式的
      length 恒为 415（与文件内容无关）。

    上游真实布局（gme.h）：
        int length, intro_length, loop_length, play_length, fade_length;
        int i5..i15;                      /* 11 个保留 int */
        const char *system, *game, *song, *author, *copyright, *comment, *dumper;
        const char *s7..s15;/* 9 个保留指针 */
    共 16 个 int + 16 个指针。

    ⚠ **保留字段宁可多留也不能少留**：少一个int，后面的字符串指针区整体前移，
      读出来就是垃圾指针（可能直接段错误）。多留只是多几个字节，安全。
    """

    _fields_ = [
        # ---- 5 个时间字段（毫秒；-1 = 未知）----
        ("length", ctypes.c_int),
        ("intro_length", ctypes.c_int),
        ("loop_length", ctypes.c_int),
        ("play_length", ctypes.c_int),
        ("fade_length", ctypes.c_int),
        # ---- 11 个保留 int（上游 i5..i15）----
        ("i5", ctypes.c_int), ("i6", ctypes.c_int), ("i7", ctypes.c_int),
        ("i8", ctypes.c_int), ("i9", ctypes.c_int), ("i10", ctypes.c_int),
        ("i11", ctypes.c_int), ("i12", ctypes.c_int), ("i13", ctypes.c_int),
        ("i14", ctypes.c_int), ("i15", ctypes.c_int),
        # ---- 7 个字符串（空串"" = 无）----
        ("system", ctypes.c_char_p),
        ("game", ctypes.c_char_p),
        ("song", ctypes.c_char_p),
        ("author", ctypes.c_char_p),
        ("copyright", ctypes.c_char_p),
        ("comment", ctypes.c_char_p),
        ("dumper", ctypes.c_char_p),
        # ---- 9 个保留指针（上游 s7..s15）----
        ("s7", ctypes.c_char_p), ("s8", ctypes.c_char_p),
        ("s9", ctypes.c_char_p), ("s10", ctypes.c_char_p),
        ("s11", ctypes.c_char_p), ("s12", ctypes.c_char_p),
        ("s13", ctypes.c_char_p), ("s14", ctypes.c_char_p),
        ("s15", ctypes.c_char_p),
    ]


def _gme_track_info(gme, emu, ti):
    """正确地读一轨的 ``gme_info_t``。返回 dict（读不到返回 None）。**绝不抛异常**。

    ⚠⚠ **两个必须做对的点，少一个都静默出错**：

    1. **调用约定是``gme_info_t**``（二级指针）**：
           gme_err_t gme_track_info( Music_Emu const*, gme_info_t** out, int track );
       DLL **自己 new 出结构体并把指针写回**，调用方不拥有那块内存。
       旧代码传的是单个指针（``byref(gi)``），DLL 于是把「结构体指针」写进
       调用方内存的前 8 字节，之后所有字段全是垃圾 —— 这就是那个恒为 415 的
       length 与一片 None 的字符串的来源。

    2. **必须调 gme_free_info 释放**，否则**每轨泄漏一次**。
       HES 是 256 轨、KSS 也是 256 轨，跑一个文件就漏 256 份 —— 长时间批量
       转换会持续涨内存。老DLL 若未导出该符号，用 hasattr 先探一次并降级
       （不释放只是慢，不会错）。

    返回 dict 而非裸结构体：把 ``bytes`` 字段在**释放之前**取出来，调用方就
    不会拿着已free 的指针去读字符串（use-after-free，比错位更难查）。
    """
    try:
        holder = ctypes.POINTER(_GmeInfo)()
        if gme.gme_track_info(emu, ctypes.byref(holder), ti):
            return None
        if not holder:
            return None
        try:
            info = holder.contents

            def _s(v):
                if not v:
                    return ""
                if isinstance(v, bytes):
                    return v.decode("utf-8", "replace").strip()
                return str(v).strip()

            return {
                "length": int(info.length),
                "intro_length": int(info.intro_length),
                "loop_length": int(info.loop_length),
                "play_length": int(info.play_length),
                "fade_length": int(info.fade_length),
                "system": _s(info.system),
                "game": _s(info.game),
                "song": _s(info.song),
                "author": _s(info.author),
                "copyright": _s(info.copyright),
                "comment": _s(info.comment),
                "dumper": _s(info.dumper),
                # ⚠ 上游 gme_info_t **没有 track_name 字段**（s7..s15 是保留
                #   指针）。旧结构体里那个 track_name 是凭空加的，位置也错。
                #   逐轨曲名请走 gme_track_info 之外的途径，或退回 song/文件名。
                #   这里显式给 ""，让调用方按既有的 1b->1d->2 优先级自然降级，
                #   而不是去读一个保留指针（那才是未定义行为）。
                "track_name": "",
            }
        finally:
            free = getattr(gme, "gme_free_info", None)
            if free is not None:
                try:
                    free(holder)
                except Exception:            # noqa: BLE001
                    pass
    except Exception:                        # noqa: BLE001
        return None


def _load_gme():
    """加载 libgme.dll 并配置 ctypes 签名。返回已加载的库对象。"""
    global _GME
    if _GME is not None:
        return _GME
    dll_name = "libgme.dll"
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [here]
    if getattr(sys, "frozen", False):
        candidates.append(os.path.dirname(os.path.abspath(sys.executable)))
        m = getattr(sys, "_MEIPASS", None)
        if m:
            candidates.append(m)
    found = None
    for d in candidates:
        if os.path.exists(os.path.join(d, dll_name)):
            found = d
            break
    if found is None:
        raise RuntimeError(
            "未找到 libgme.dll (SPC 解码所需)。请将 libgme.dll 及其依赖 "
            "(libstdc++-6.dll / zlib1.dll / libgcc_s_seh-1.dll / libwinpthread-1.dll) "
            "与本程序放在同一目录。")
    try:
        os.add_dll_directory(found)  # 让加载器能找到同目录的依赖 DLL
    except (AttributeError, OSError):
        pass
    lib = ctypes.CDLL(os.path.join(found, dll_name))
    lib.gme_open_file.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
    lib.gme_open_file.restype = ctypes.c_char_p
    lib.gme_track_count.argtypes = [ctypes.c_void_p]
    lib.gme_track_count.restype = ctypes.c_int
    lib.gme_start_track.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.gme_start_track.restype = ctypes.c_char_p
    lib.gme_play.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_short)]
    lib.gme_play.restype = ctypes.c_char_p  # 成功返回 NULL, 失败返回错误字符串
    lib.gme_delete.argtypes = [ctypes.c_void_p]
    lib.gme_delete.restype = None
    lib.gme_track_info.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    lib.gme_track_info.restype = ctypes.c_char_p
    # gme_free_info(**必须**显式声明)：DLL 用自己的 new 分配结构体，只能由它释放。
    # 漏掉它 = 每轨泄漏一份（HES/KSS 各256 轨，批量转换会持续涨内存）。
    # 老版本 libgme 可能没导出这个符号，故容错。
    try:
        lib.gme_free_info.argtypes = [ctypes.c_void_p]
        lib.gme_free_info.restype = None
    except AttributeError:
        pass
    # 逐声部分离所需: 查询/控制每个 SNES 通道的开关
    try:
        lib.gme_voice_count.argtypes = [ctypes.c_void_p]
        lib.gme_voice_count.restype = ctypes.c_int
        lib.gme_mute_voices.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.gme_mute_voices.restype = None
        lib.gme_mute_voice.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.gme_mute_voice.restype = None
    except AttributeError:
        pass  # 老版本 libgme 可能没有这些导出
    # 非 ASCII 路径兜底用（见 gme_open）：老版本 libgme 可能没导出，故容错
    try:
        lib.gme_open_data.argtypes = [ctypes.c_char_p, ctypes.c_long,
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
        lib.gme_open_data.restype = ctypes.c_char_p
    except AttributeError:
        pass
    _GME = lib
    return lib


def gme_open(gme, path, sample_rate):
    """打开音源文件 -> (err, emu, keepalive)。

    ⚠ **libgme 的 gme_open_file 是按 ANSI 码页解释路径的**，路径里只要有非
      ASCII 字符（中文目录名、中文曲名），文件明明存在也会返回
      `Couldn't open file`。实测同一份 sample.spc：
        `D:/_env_probe/ascii_run/...`            -> 成功
        `D:/.../MSU1Converter-工程备份-.../...`  -> 失败
      这不是路径长度/权限问题，纯粹是编码。

    兜底：整块读进内存后走 `gme_open_data`（libgme 官方 API），完全不碰
    文件系统编码，因此与路径无关。返回的第三个值必须由调用方**持有到用完
    为止**（libgme 打开阶段会读这块内存），所以不要写成 `gme_open(...)[:2]`。
    """
    abs_path = os.path.abspath(path)
    emu = ctypes.c_void_p()
    err = gme.gme_open_file(abs_path.encode("utf-8"), ctypes.byref(emu), sample_rate)
    if not err:
        return err, emu, None
    opener = getattr(gme, "gme_open_data", None)
    if opener is None:
        return err, emu, None                      # 老 DLL：维持原错误
    try:
        with open(abs_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return err, emu, None                      # 真读不到，保留原始错误
    emu2 = ctypes.c_void_p()
    err2 = opener(raw, len(raw), ctypes.byref(emu2), sample_rate)
    if err2:
        return err, emu, None                      # 仍失败：报原始的「打不开」
    return err2, emu2, raw


def safe_filename(name, fallback="track", limit=100):
    """把任意字符串转成 Windows 安全文件名 (去掉 \\/:*?"<>| 与控制字符)。

    SPC 的 ID666 曲名含 "Huh?!"、"Rat-a-Tat-Tat It's..." 这类字符, 其中
    ? * : " / < > | 在 Windows 上非法, 直接拼路径会抛 PermissionError。

    超长时**保留尾部**(如 " (Variant 2)"), 因为变体区分信息通常在末尾;
    否则 "People Without Hope (Variant 1)" 与 "(Variant 2)" 会被截成同名,
    输出时后者覆盖前者, 丢曲子。
    """
    s = re.sub(r'[\\/:*?"<>|]+', "_", name)
    s = "".join(ch for ch in s if ord(ch) >= 32)
    s = s.strip().rstrip(". ")
    if len(s) > limit:
        tail = s[-24:].lstrip()
        s = s[:limit - len(tail)] + tail
    return s.strip().rstrip(". ") or fallback


def dedupe_names(names, limit=100):
    """确保一批曲名互不相同: 同名时追加 _2 / _3 ... 后缀。返回新列表。"""
    seen = {}
    out = []
    for n in names:
        key = n.lower()
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 1:
            out.append(n)
        else:
            cand = f"{n}_{seen[key]}"
            while cand.lower() in seen:
                seen[key] += 1
                cand = f"{n}_{seen[key]}"
            seen[cand.lower()] = 1
            out.append(cand)
    return out


def _spc_track_name(path, libinfo, ti, ntrack, id666=None):
    """SPC 曲名: 优先 ID666 标签(libgme 常读不到, 故用自行解析的 id666 兜底)。

    返回的名字会用作输出文件名, 因此必须过 safe_filename。

    ⚠ `libinfo` 是 :func:`_gme_track_info` 返回的 **dict**，不再是 ctypes
      结构体 —— 那块内存已被 gme_free_info 释放，再按结构体读就是
      use-after-free（比字段错位更难查）。故用 ``.get()``，libinfo 为 None
      时整体跳过。
    """
    def dec(p):
        if not p:
            return None
        if isinstance(p, (bytes, bytearray)):
            try:
                return bytes(p).decode("utf-8", "replace")
            except Exception:                            # noqa: BLE001
                return None
        return str(p)
    base = os.path.splitext(os.path.basename(path))[0]
    # 文件名去掉前导序号(如 "204 People Without Hope (Variant 1)")得到主体
    fn_body = re.sub(r"^\d+\s+", "", base).strip()
    lib_song = ""
    if isinstance(libinfo, dict):
        lib_song = dec(libinfo.get("track_name")) or dec(libinfo.get("song")) or ""
    title = lib_song or id666
    # ID666 曲名字段只有 26 字节, 长曲名(尤其带 "(Variant N)" 后缀)会被截断,
    # 导致 "People Without Hope (Variant 1)" 与 "(Variant 2)" 截成同名互相覆盖。
    # 若文件名主体"包含" ID666 曲名且更长(说明 ID666 被截断), 用文件名主体兜底。
    if title and fn_body and len(fn_body) > len(title) and title.lower() in fn_body.lower():
        title = fn_body
    if title:
        return safe_filename(f"{ti+1:02d}_{title}" if ntrack > 1 else title,
                             f"{base}_{ti+1:02d}")
    return safe_filename(f"{base}_{ti+1:02d}" if ntrack > 1 else base, base)


def _id666_num(raw, lo, hi):
    """ID666 的定长十进制文本字段 -> 整数 (非数字/越界一律返回 0)。"""
    txt = raw.decode("latin-1", "replace").strip().strip("\x00").strip()
    if not txt.isdigit():
        return 0
    v = int(txt)
    return v if lo <= v <= hi else 0


def _spc_id666_title(path):
    """解析 SPC 的 ID666 文本区, 返回 (曲名, 游戏名, 时长秒, 淡出毫秒)。

    SPC v0.30 头部布局(固定偏移):
      0x00  33 字节 "SNES-SPC700 Sound File Data v0.30"
      0x21  26 个 0x1A + 1 个 0x1E
      0x2E  曲名    26 字节
      0x48  游戏名  32 字节
      0x68  Dumper  16 字节
      0x78  注释    32 字节
      0x98  日期    11 字节
      0xA9  播放秒数     3 位 ASCII 十进制  ("999" / 空白 = 未标注)
      0xAC  淡出毫秒数   5 位 ASCII 十进制
    libgme 对部分文件读不出这些标签, 故自行解析。

    ⚠ 0xA3 是日期字段的一部分, 老代码把它当秒数读, 所以 92 首全部读成 0,
      只能退回"解码到连续静音为止"——对循环曲就是白播两遍。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(0xB1)
    except OSError:
        return None, None, 0, 0
    if len(head) < 0xB1 or not head.startswith(b"SNES-SPC700 Sound File Data"):
        return None, None, 0, 0

    def field(off, size):
        b = head[off:off + size]
        return b.split(b"\x00")[0].decode("ascii", "replace").strip()

    song = field(0x2E, 26)
    game = field(0x48, 32)
    secs = _id666_num(head[0xA9:0xAC], 1, 3600)
    fade_ms = _id666_num(head[0xAC:0xB1], 0, 60000)
    return song or None, game or None, secs, fade_ms


def _spc_play_plan(secs, fade_ms, policy):
    """按策略算 (目标播放秒数, 淡出毫秒)。secs=0 表示无法确定, 返回 None 走静音检测。

    policy:
      "natural"   解码到连续静音为止 (无 ID666 时的兜底, 循环曲会白播两遍)
      "id666"     按 ID666 标注秒数播一遍 (推荐: 这就是抓轨时标注的单遍长度)
      "id666_x2"  ID666 时长 ×2 (想多听一轮循环时用)
    """
    if policy not in ("id666", "id666_x2") or secs <= 1:
        return None, 0
    play = float(secs) * (2.0 if policy == "id666_x2" else 1.0)
    play = min(play, float(_SPC_MAX_SECS))
    fade = int(fade_ms or 0)
    # 淡出不能超过总长的 40%, 否则整首都在衰减
    fade = max(0, min(fade, int(play * 1000 * 0.4)))
    return play, fade


def _apply_fade(pcm, fade_ms, sr=44100):
    """给 PCM(16bit/立体声/小端) 尾部施加升余弦淡出, 返回新字节。

    自己算而不用 libgme 的 gme_set_fade_msecs: 实测该接口只在很短的时间点上
    可靠, 起点一长(>60s)就完全不生效, 而 SPC 的 ID666 淡出常在 140s 之后。
    """
    if fade_ms <= 0:
        return pcm
    frames = len(pcm) // 4
    n = min(int(fade_ms * sr / 1000.0), frames)
    if n <= 1:
        return pcm
    if _HAVE_NUMPY:
        a = _np.frombuffer(pcm, dtype="<i2").reshape(-1, 2).astype(_np.float32)
        g = _np.ones(frames, dtype=_np.float32)
        t = _np.linspace(0.0, 1.0, n, dtype=_np.float32)
        g[frames - n:] = 0.5 * (1.0 + _np.cos(_np.pi * t))   # 1 -> 0
        return (a * g[:, None]).astype("<i2").tobytes()
    # 无 numpy 时的兜底: 以 16 帧为一段的阶梯增益(0.36ms 一级, 听不出台阶)
    import math
    out = array.array("h")
    out.frombytes(pcm)
    base = (frames - n) * 2
    step = 16
    for i in range(0, n, step):
        g = 0.5 * (1.0 + math.cos(math.pi * min(1.0, i / n)))
        j0 = base + i * 2
        j1 = base + min(n, i + step) * 2
        for k in range(j0, j1):
            out[k] = int(out[k] * g)
    return out.tobytes()


def _spc_decode_file(path, length_policy="natural", speed=1.0,
                     decode_cap=None):
    """真正的解码(在子进程中调用)。逐轨 yield (name, pcm_bytes), 不累积。

    每个 SPC 可能含多轨(gme_track_count); 全部解码为 16-bit/44.1k/立体声 小端 PCM。
    这是生成器, 调用方应边取边写盘。

    speed 是"播放设备时钟补偿": 某些播放器(如 Pico Plus 的菜单播放器)内部 I2S
    时钟偏低, 会把 44.1k 数据当 36.75k/32k 送出, 听感就是整体变慢、变低。
    此时把渲染采样率设为 44100/speed, 同样长的音乐只占 1/speed 的采样点,
    写盘时仍标 44100, 设备慢放 speed 倍后正好还原原始时长与音高。
    speed=1.0 表示不补偿(正常 44100 渲染)。
    """
    gme = _load_gme()
    try:
        sp = float(speed)
    except (TypeError, ValueError):
        sp = 1.0
    sp = max(0.5, min(2.0, sp))
    SR = int(round(44100 / sp))
    err, emu, _hold = gme_open(gme, path, SR)
    if err:
        raise RuntimeError("libgme 打开失败: " + err.decode("latin-1", "replace"))
    try:
        ntrack = gme.gme_track_count(emu)
        if ntrack <= 0:
            raise RuntimeError("libgme: 文件未包含可识别音轨")
        # libgme 常读不出 ID666 标签, 先自行解析一次作为兜底曲名/时长
        id666_song, _id666_game, id666_secs, id666_fade = _spc_id666_title(path)
        for ti in range(ntrack):
            e = gme.gme_start_track(emu, ti)
            if e:
                raise RuntimeError("start_track 失败: " + e.decode("latin-1", "replace"))
            libinfo = _gme_track_info(gme, emu, ti)
            # 时长仍走 ID666 策略（_spc_play_plan），**不**改用 libgme 的
            # length/play_length —— 那是本轮明确不碰的已验证行为（见任务要求）。
            play_secs, fade_ms = _spc_play_plan(id666_secs, id666_fade, length_policy)
            # 「已知时长」(id666 策略) 与「解到静音为止」(natural 策略) 两条路
            # 必须分开记 —— decode_cap 只对后者生效, 而且**只当上限用**:
            # stop_on_silence 仍为 True, 短曲照样按尾部静音提前收尾,
            # 只有整轨循环、永远等不到静音的那种才会被这个上限截住。
            # ⚠ 不能直接把 play_secs 当成「已知时长」传下去 —— 那会把
            #   stop_on_silence 关掉, 含长静音段的环境音会被当成整轨循环,
            #   白白解满上限(见 _spc_decode_track 的 docstring)。
            known_len = play_secs is not None
            if not known_len and decode_cap:
                play_secs = float(decode_cap)
            pcm = _spc_decode_track(gme, emu, SR, max_secs=play_secs,
                                    stop_on_silence=not known_len)
            if fade_ms:
                pcm = _apply_fade(bytes(pcm), fade_ms, SR)
            yield (_spc_track_name(path, libinfo, ti, ntrack, id666_song), pcm)
    finally:
        gme.gme_delete(emu)


def _spc_decode_track(gme, emu, sr, max_secs=None, stop_on_silence=True,
                      lead_sil_limit=_SPC_LEAD_SIL_SECS, stop_on_ended=False):
    """逐块解码, 以"尾部连续静音"判定曲子结束。返回 16-bit/44.1kHz/立体声 小端 PCM。

    静音判定有两个必须的保护:
      1. 必须"曾经发过声"(has_sound) 才允许因静音结束 —— 否则开头就休止的
         曲子会被判成 2 秒空曲。
      2. 从未发声时, 允许在开头静音累积到 lead_sil_limit 秒后放弃,
         避免空文件白跑满 max_frames。

    stop_on_silence=False 时只按 max_secs 截断 —— 已知确切时长(ID666)时必须这样,
    否则"地下下水道""海浪""监狱塔"这类含长静音段的环境音会在第一个 2 秒空隙处
    被误判为结束(Prison Tower 标注 52s 却只出 2.5s)。

    stop_on_ended=True 时额外用 **libgme 自己的 gme_track_ended** 判断收尾。
    这是 v4.4 为芯片格式加的：芯片音乐的轨长短差异极大（一个 NSF 包里既有 1.25s
    的音效、也有无限循环的 BGM），而**我们自己的「连续 2 秒静音」判定对这两类
    都会错** —— 对短音效太慢（拖静音尾巴）、对长 BGM 中途的乐句停顿又会误砍。
    libgme 的 ended 用约 1.25s 的静音前瞻，且是格式实现方的官方判定，比我们猜可靠。
    ⚠ 默认 False，**SPC 路径行为逐字节不变**。

    lead_sil_limit: 开头全静音的上限(秒)。**默认值 = _SPC_LEAD_SIL_SECS, 即本参数
    加入前的原值**, 因此 SPC 路径的产出**逐字节不变**(AC-14 零回归的前提, 设计 H2)。
    芯片音源在"已知确切时长"时必须传 0 —— VGM 开头可能静音超过 10s,
    用默认的 10s 会把曲子误砍成空(AC-5 会挂)。
    """
    chunk = _SPC_CHUNK
    buf = (ctypes.c_short * (chunk * 2))()
    sil_need = int(_SPC_SIL_SECS * sr)
    min_frames = int(_SPC_MIN_SECS * sr)
    lead_limit = int(max(0.0, float(lead_sil_limit)) * sr)
    max_frames = int((max_secs if max_secs else _SPC_MAX_SECS) * sr)
    pcm = bytearray()
    frames = 0   # 已解码帧数
    run = 0      # 当前连续静音帧数
    has_sound = False
    while frames < max_frames:
        # 关键: gme_play 的第 2 个参数是**样本数(short 个数)**, 不是帧数。
        # 立体声下 1 帧 = 2 个 short。早先误传帧数, 每次只填满缓冲区的
        # 一半(后半是零/上一块的残留), 结果整条音频被拉长一倍, 并每
        # 93ms 出现一次断点 —— 听感就是"速度很慢、节奏错乱"。
        n = min(chunk, max_frames - frames)
        e = gme.gme_play(emu, n * 2, buf)
        if e:
            raise RuntimeError("解码错误: " + e.decode("latin-1", "replace"))
        raw = ctypes.string_at(ctypes.addressof(buf),
                               n * 2 * ctypes.sizeof(ctypes.c_short))
        pcm += raw
        frames += n
        # 该块是否静音(峰值低于阈值)。注意 array.frombytes 是"追加",
        # 必须每轮新建数组, 否则越积越多导致峰值扫描退化成 O(N^2)。
        samples = array.array("h")
        samples.frombytes(raw)
        peak = max(map(abs, samples))
        if peak >= _SPC_SIL_PEAK:
            has_sound = True
            run = 0
        else:
            run += n
        if stop_on_silence and run >= sil_need and frames >= min_frames:
            # 已发过声 -> 尾部静音 = 曲子结束; 从未发声 -> 只在"开头长时间
            # 全静音"时才放弃。
            if has_sound or frames >= lead_limit:
                break
        elif not stop_on_silence and not has_sound and frames >= lead_limit:
            # 已知时长但整轨无声(空曲), 不必白跑到底
            break
        # v4.4：芯片格式用 libgme 官方的 ended 判定收尾（见 docstring）。
        # 要求 has_sound，避免把「开头就静音」的空轨误当结束；
        # gme_track_ended 缺失时静默降级（旧版 libgme 没有该接口）。
        if stop_on_ended and has_sound:
            try:
                if gme.gme_track_ended(emu):
                    break
            except AttributeError:
                pass
            except Exception:                            # noqa: BLE001
                pass
    return pcm


def spc_decode_voices(path, max_sec=180.0):
    """逐声部解码: 把 SPC 的每个 SNES 通道单独解码成独立 PCM。

    SPC (SNES S-SMP) 最多 8 个通道。用 gme_mute_voices 依次只保留单通道,
    得到 8 条互不干扰的音轨。这是"复音翻奏"的关键: 只有分通道检测音高,
    才能保留原曲的旋律/和声/贝斯/复调结构(混成单声道再用单音 YIN 会把
    8 个声部压成一条乱跳的音高线, 完全听不出原曲)。

    返回 [(voice_index, mono_float64_array), ...], 只含非静音通道。
    """
    if not _HAVE_NUMPY:
        raise RuntimeError("逐声部分离需要 numpy")
    gme = _load_gme()
    SR = 44100
    err, emu, _hold = gme_open(gme, path, SR)
    if err:
        raise RuntimeError("libgme 打开失败: " + err.decode("latin-1", "replace"))
    try:
        ntrack = gme.gme_track_count(emu)
        if ntrack <= 0:
            raise RuntimeError("libgme: 文件未包含可识别音轨")
        # 逐声部只处理第 0 轨(绝大多数 SPC 是单轨)
        gme.gme_start_track(emu, 0)
        try:
            nvoice = gme.gme_voice_count(emu)
        except AttributeError:
            nvoice = 8
        nvoice = max(1, min(8, int(nvoice)))
        max_frames = int(max_sec * SR)
        # 第一步: 先用"完整混音"解码一次, 确定整首曲子的长度。
        # 逐通道 solo 时**必须按这个长度解码, 不能各自做静音检测** ——
        # 伴奏/贝斯/和声通道常在开头休止好几秒, 若各自用静音判定, 会在
        # 开头就认定"曲子结束", 把整条音轨截成 2 秒空白后当空通道丢弃。
        # (这正是先前大量曲目只剩 1 个声部、听不出原曲的主因。)
        gme.gme_mute_voices(emu, 0)               # 0 = 全部取消静音
        gme.gme_start_track(emu, 0)
        full = _spc_decode_track(gme, emu, SR, max_secs=max_sec)
        song_frames = len(full) // 4              # 16-bit 立体声 -> 4 字节/帧
        del full
        if song_frames <= 0:
            return []
        # 多解 0.25s 收尾, 保证最后一个音不被切掉
        target = min(song_frames + int(0.25 * SR), max_frames)
        out = []
        for v in range(nvoice):
            # 只保留第 v 通道: 先全静音, 再单独放开第 v 路
            gme.gme_mute_voices(emu, -1)          # -1 = 全部静音
            gme.gme_mute_voice(emu, v, 0)         # 0 = 取消静音(单独放开 v)
            # 重新定位到开头再解码
            gme.gme_start_track(emu, 0)
            buf = (ctypes.c_short * (_SPC_CHUNK * 2))()
            frames = 0
            pcm = bytearray()
            # 固定解码到 target, 不用静音判定(见上方说明)
            while frames < target:
                # 同 _spc_decode_track: 参数是 short 样本数 = 帧数 × 2
                n = min(_SPC_CHUNK, target - frames)
                e = gme.gme_play(emu, n * 2, buf)
                if e:
                    break
                raw = ctypes.string_at(ctypes.addressof(buf),
                                       n * 2 * ctypes.sizeof(ctypes.c_short))
                pcm += raw
                frames += n
            if len(pcm) < SR * 4:               # 太短视为空通道
                continue
            arr = _np.frombuffer(bytes(pcm), dtype="<i2").reshape(-1, 2)
            mono = arr.mean(axis=1).astype(_np.float64)
            # 整轨几乎静音(峰值极低)的通道跳过
            if _np.max(_np.abs(mono)) < 500:
                continue
            out.append((v, mono[:song_frames]))
        return out
    finally:
        gme.gme_delete(emu)


def _spc_decode_and_write(out_path, in_paths, length_policy="natural", speed=1.0,
                          decode_cap=None):
    """子进程 worker: 解码若干 SPC 文件, 写出帧文件供主进程读取。

    帧格式: I 轨数, 每轨 [H 名长 + 名(UTF-8) + I pcm长度 + pcm字节]。
    解码结果**边解边写**, 不在内存里累积(几十首曲子可达数 GB)。
    """
    count_pos = 0
    ntrack = 0
    with open(out_path, "wb") as f:
        f.write(struct.pack("<I", 0))  # 占位, 稍后回填真实轨数
        for p in in_paths:
            for name, pcm in _spc_decode_file(p, length_policy, speed,
                                              decode_cap):
                nb = name.encode("utf-8")
                f.write(struct.pack("<H", len(nb)))
                f.write(nb)
                f.write(struct.pack("<I", len(pcm)))
                f.write(pcm)
                ntrack += 1
        f.seek(count_pos)
        f.write(struct.pack("<I", ntrack))


def _read_spc_frame(path):
    """读帧文件 -> 音轨列表。

    PCM 先落盘为临时文件, "pcm" 字段返回惰性读取器 _SpoolPcm, 使用时才真正
    读入内存。这样一次解码几十首曲子(每首数 MB~数十 MB)也不会打爆内存。
    """
    with open(path, "rb") as f:
        header = f.read(4)
        if len(header) < 4:
            raise RuntimeError("SPC 解码结果为空或已损坏")
        (ntrack,) = struct.unpack_from("<I", header, 0)
        tracks = []
        spool_dir = tempfile.mkdtemp(prefix="spc_pcm_")
        for i in range(ntrack):
            (nlen,) = struct.unpack("<H", f.read(2))
            name = f.read(nlen).decode("utf-8", "replace")
            (plen,) = struct.unpack("<I", f.read(4))
            pcm_path = os.path.join(spool_dir, f"{i:04d}.pcm")
            remaining = plen
            with open(pcm_path, "wb") as pf:
                while remaining > 0:
                    chunk = f.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise RuntimeError("SPC 解码结果被截断")
                    pf.write(chunk)
                    remaining -= len(chunk)
            tracks.append({"name": name, "pcm": _SpoolPcm(pcm_path, spool_dir),
                           "little_endian": True})
        return tracks


class _SpoolPcm:
    """磁盘暂存的 PCM: 行为像 bytes(支持 len/索引/切片/bytes()), 但按需读取。"""

    __slots__ = ("_path", "_size", "_dir")

    def __init__(self, path, spool_dir=None):
        self._path = path
        self._size = os.path.getsize(path)
        self._dir = spool_dir

    def _read_all(self):
        with open(self._path, "rb") as f:
            return f.read()

    def __len__(self):
        return self._size

    def __bytes__(self):
        return self._read_all()

    def __eq__(self, other):
        if isinstance(other, _SpoolPcm):
            return (self._size == other._size
                    and self._read_all() == other._read_all())
        if isinstance(other, (bytes, bytearray)):
            return self._read_all() == bytes(other)
        return NotImplemented

    def __hash__(self):
        return hash(self._read_all())

    def __repr__(self):
        return f"<SpoolPcm {self._size} bytes>"

    def __getitem__(self, key):
        data = self._read_all()
        if isinstance(key, slice):
            return data[key]
        return data[key]

    def __iter__(self):
        return iter(self._read_all())

    def __add__(self, other):
        return self._read_all() + bytes(other)

    def __radd__(self, other):
        return bytes(other) + self._read_all()

    def __bool__(self):
        return self._size > 0

    def read_all(self):
        return self._read_all()

    def cleanup(self):
        try:
            os.remove(self._path)
        except OSError:
            pass
        # 该批次的所有 PCM 都在同一个暂存目录里, 由最后一个被清理的实例删目录
        if self._dir:
            self._dir = None
            shutil.rmtree(os.path.dirname(self._path), ignore_errors=True)


def load_spc(path, length_policy="natural", speed=1.0):
    """单个 SPC 文件 -> 归一化音轨列表(可能多轨)。通过子进程解码以防卡死。"""
    return load_spc_paths([path], length_policy=length_policy, speed=speed)


def _spc_worker_cmd(out_path, in_args, length_policy="natural", speed=1.0,
                    decode_cap=None):
    base = ([worker_launcher()] if _IS_FROZEN
            else [sys.executable, os.path.abspath(__file__)])
    cmd = (base + ["--spc-decode", out_path, "--spc-length", length_policy,
                   "--spc-speed", repr(float(speed))])
    # 只有真给了上限才加这个参数 —— 参数是「顺序解析」的, 多写一个空值
    # 会把后面的输入文件当成上限值吃掉(见 maybe_run_spc_worker 的写法)。
    if decode_cap:
        cmd += ["--spc-cap", repr(float(decode_cap))]
    return cmd + in_args


def _spc_decode_batch(batch, length_policy="natural", speed=1.0,
                      decode_cap=None):
    """跑一个 worker 子进程解码一批 SPC, 返回音轨列表。失败抛 RuntimeError。"""
    out_path = None
    try:
        tf = tempfile.NamedTemporaryFile(delete=False, prefix="spcbin_",
                                         suffix=".spcbin")
        tf.close()
        out_path = tf.name
        cmd = _spc_worker_cmd(out_path, [os.path.abspath(p) for p in batch],
                              length_policy, speed, decode_cap)
        # 每批超时 = 单文件超时 × 批大小, 再留一倍余量
        timeout = _SPC_TIMEOUT * max(1, len(batch)) * 2
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"SPC 解码超时({timeout}s): 文件可能损坏或不受支持 -> "
                + ", ".join(os.path.basename(p) for p in batch))
        if res.returncode != 0:
            msg = res.stderr.decode("utf-8", "replace").strip()
            if "SPCDECODE_ERROR" in msg:  # worker 把错误写进 stderr
                msg = msg.split("SPCDECODE_ERROR", 1)[1].strip(": ")
            raise RuntimeError("SPC 解码失败: " + (msg or f"退出码 {res.returncode}"))
        return _read_spc_frame(out_path)
    finally:
        if out_path and os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass


def load_spc_paths(paths, batch_size=8, length_policy="natural", speed=1.0,
                   on_progress=None, decode_cap=None):
    """多个 SPC 文件 -> 归一化音轨列表。

    分批解码(默认每批 8 个), 每批结束立即把 PCM 落盘为临时文件并释放解码
    进程内的内存, 因此一次转换几十上百首曲子也不会耗尽内存。

    length_policy 见 _spc_play_plan: "natural" / "id666" / "id666_x2"。
    speed 见 _spc_decode_file: 设备时钟补偿倍数, 1.0 = 不补偿。
    on_progress(done, total, secs): 每批解码结束后回调, secs 为本批音频秒数,
    供上层进度条按"已解码多少秒"推进。
    decode_cap: 解码时长上限(秒), 见 load_spc_dir 的同名参数。
    """
    prog = on_progress or (lambda *_a: None)
    total = len(paths)
    tracks = []
    batch = []
    for p in paths:
        batch.append(p)
        if len(batch) >= batch_size:
            _flush_spc_batch(batch, length_policy, speed, tracks, total, prog,
                             decode_cap)
            batch = []
    if batch:
        _flush_spc_batch(batch, length_policy, speed, tracks, total, prog,
                         decode_cap)
    # 曲名去重: 同一目录里常有同名不同版本的曲子(如 Variant 1/2),
    # 名字重复会导致输出文件互相覆盖、悄悄丢曲子。
    for tr, new_name in zip(tracks, dedupe_names([t["name"] for t in tracks])):
        tr["name"] = new_name
    return tracks


def _flush_spc_batch(batch, length_policy, speed, tracks, total, prog,
                     decode_cap=None):
    """解码一批并追加到 tracks, 顺带汇报已解码多少秒 (不进内存, len() 即可)。"""
    got = _spc_decode_batch(batch, length_policy, speed, decode_cap)
    tracks.extend(got)
    secs = sum(len(t["pcm"]) for t in got) / (44100 * 2 * 2)
    prog(len(tracks), total, secs)


def cleanup_spc_temp(tracks):
    """删除 SPC 解码产生的磁盘暂存文件(转换完成后调用)。"""
    for tr in tracks or ():
        pcm = tr.get("pcm")
        if hasattr(pcm, "cleanup"):
            pcm.cleanup()


def load_spc_dir(input_path, length_policy="natural", speed=1.0,
                 on_progress=None, decode_cap=None):
    """含 .spc 的文件夹 -> 归一化音轨列表。

    on_progress(done, total, secs): 每批解码结束回调, 契约同 load_spc_paths。
    **以前这个参数根本不存在**, load_source 也就没法往下传, 于是几十首的
    SPC 包全程零进度上报 —— 界面表现就是「卡在转换中、一直 0%」
    (用户拿 Chrono Trigger 92 首的包实测报过, 解了十几分钟没动静)。

    decode_cap: 解码时长上限(秒), 0/None = 不设限(旧行为)。
      **它不改变成品长度** —— write_tracks 本来就会按「单曲最长」截断,
      这里只是提前停手, 免得为了留下前 150 秒硬解满 _SPC_MAX_SECS(600 秒)。
      整轨循环的包(Chrono Trigger 几乎每首都是)实测 18 分钟 → 4.7 分钟,
      且成品逐字节不变(已验证 md5 一致)。
    """
    files = [os.path.join(input_path, f) for f in os.listdir(input_path)
             if f.lower().endswith(".spc")]
    files.sort(key=natural_track_key)
    if not files:
        raise RuntimeError(f"{input_path}: 未发现 .spc 文件")
    return load_spc_paths(files, length_policy=length_policy, speed=speed,
                          on_progress=on_progress, decode_cap=decode_cap)


def load_source(path, on_progress=None, xa_music_only=False, xa_stats=None,
                xa_split=False, decode_cap=None):
    if os.path.isdir(path):
        # 优先 MSU-1 (*.pcm); 其次 SPC 文件夹; 再其次多格式芯片音源; 最后 MP3 文件夹
        if any(f.lower().endswith(".pcm") for f in os.listdir(path)):
            return load_msu1_dir(path)
        if any(f.lower().endswith(".spc") for f in os.listdir(path)):
            # ⚠ 这里以前是裸的 `load_spc_dir(path)` —— on_progress 没接下去,
            #   于是整个 SPC 包解码期间**零进度上报**。几十首的包要解十几分钟,
            #   界面表现就是「卡在转换中、一直 0%」(用户实测报过)。
            #   契约换算: load_spc_paths 的 prog 是 (已完成, 总数, 秒数),
            #   load_source 的 on_progress 收一个 0..1 的比例。
            cb = None
            if on_progress is not None:
                def cb(done, total, _secs, _f=on_progress):     # noqa: E306
                    try:
                        _f(min(0.999, float(done) / max(1.0, float(total or 1))))
                    except Exception:                           # noqa: BLE001
                        pass
            return load_spc_dir(path, on_progress=cb, decode_cap=decode_cap)
        # 芯片判定放在 .spc 之后 —— 纯 .spc 目录必须仍走 load_spc_dir,
        # 否则 CLI 行为改变, AC-14 零回归会挂(设计 H3)。
        if any(f.lower().endswith(CHIP_EXTS) for f in os.listdir(path)):
            return load_chip_dir(path, on_progress=on_progress)
        # v4.34: 光盘镜像文件夹（PS1 / Sega CD 的 cue+bin 整包，PCE-CD 的 cue+iso）。
        # 判据必须**同时**有 .cue 和镜像文件 —— 只有 .cue 的目录可能是描述
        # .ape 的播放列表（PCE-CD 另一种常见形态），那种要落回下面的
        # CD 音轨/MP3 分支（v4.30 的 PyAV 路径能直接读 .ape/.flac）。
        names = [f.lower() for f in os.listdir(path)]
        cues = sorted(f for f in os.listdir(path) if f.lower().endswith(".cue"))
        if cues and any(n.endswith(DISC_IMG_EXTS) for n in names):
            tracks = []
            cue_paths = [os.path.join(path, c) for c in cues]
            for c in cue_paths:
                try:
                    tracks += extract_disc_audio(c)
                except RuntimeError:
                    # 这个 CUE 里没有 AUDIO 轨（纯数据盘），跳过它继续看下一个
                    continue
            if tracks:
                return _cdda_or_xa(tracks, cue_paths, on_progress,
                                   xa_music_only, xa_stats, xa_split)
            # v4.37: **一条 AUDIO 轨都没有**的纯数据盘（Silent Hill 这种，
            # CUE 里只有 TRACK 01 MODE2/2352）。以前直接落到 MP3 分支返回空，
            # 用户看到的就是「转换不出东西」。音乐其实全在数据轨的 XA 里。
            xa = _try_xa(cue_paths, on_progress, xa_music_only, xa_stats, xa_split)
            if xa:
                return xa
        mp3s = sorted(
            (os.path.join(path, f) for f in os.listdir(path)
             if f.lower().endswith(".mp3")),
            key=natural_track_key)
        tracks = []
        for fp in mp3s:
            tracks += load_mp3(fp)
        return tracks
    if path.lower().endswith((".cue",) + DISC_IMG_EXTS):
        try:
            return _cdda_or_xa(extract_disc_audio(path), [path], on_progress,
                               xa_music_only, xa_stats, xa_split)
        except RuntimeError as e:
            # 纯数据盘：没有 AUDIO 轨 -> 改抽数据轨里的 XA（v4.37）
            xa = _try_xa([path], on_progress, xa_music_only, xa_stats, xa_split)
            if xa:
                return xa
            raise RuntimeError(
                f"{e}；数据轨里也没有 XA-ADPCM 音频（这张盘可能没有可抽取的音乐）")
    if path.lower().endswith(".mp3"):
        return load_mp3(path)
    if path.lower().endswith(".spc"):
        return load_spc(path)
    # ⚠ 用 CHIP_EXTS（全部）而不是 P0_CHIP_EXTS（只有 6 种）：
    #   P1 的 .kss/.gym/.sap/.ay 和 v5.4 的 .sgc 都不在 P0 里，用 P0 会让这些
    #   **能转**的单文件直接落到下面的 raise —— 而原来那句是 SystemExit，
    #   它继承 BaseException，ConvertJob 的 `except Exception` 抓不到，于是
    #   整个 run() 静默退出，界面表现是「一直转、永不结束」。
    #   （`convert_job.classify` 里同一个坑已经修过）
    if path.lower().endswith(CHIP_EXTS):
        # 惰性 import: chip_decode 单向依赖本模块, 在这里 import 会成环(设计 §1.1)
        from chip_decode import load_chip
        # ⚠ 这里必须把 on_progress 接下去。以前是裸的 `load_chip(path)`，
        #   于是「芯片音源解码」全程零进度上报 —— 别的格式解得快（几秒）没人
        #   察觉，WSR 一个包要解几十首、跑几分钟，界面表现就是
        #   **「卡在转换中，一直无进度」**（实测踩过）。
        #   契约换算：load_chip 的 prog 是 (已完成, 总数, 秒数)，
        #   load_source 的 on_progress 收一个 0..1 的比例。
        cb = None
        if on_progress is not None:
            def cb(done, total, _secs, _f=on_progress):      # noqa: E306
                try:
                    _f(min(0.999, float(done) / max(1.0, float(total or 1))))
                except Exception:                            # noqa: BLE001
                    pass
        return load_chip(path, on_progress=cb)
    # 流式音频（.adx / .mod）：放在最后，避免每次 load_source 都白 import 一次。
    # ⚠ 这条以前缺着 —— 界面上「添加音源」能加进来（web_ui 按扩展名认），
    #   但转换线程走到这里就抛 SystemExit，用户看到的是「一直转、永不结束」。
    import stream_audio
    if path.lower().endswith(stream_audio.ALL_EXTS):
        return load_stream(path)
    # ⚠ 必须是 RuntimeError 而不是 SystemExit：后者继承 BaseException，
    #   ConvertJob 的 `except Exception` 抓不到 → 整个 run() 静默退出，
    #   用户看到「一直转、永不结束」。main() 用的是 `except Exception`，
    #   所以改这里不会让 CLI 变成裸 traceback。
    raise RuntimeError("input 必须是 MSU-1 文件夹 / .cue+.bin/.iso 光盘镜像 / .mp3 / .spc / "
                       "芯片音源文件(.nsf/.nsfe/.gbs/.vgm/.vgz/.hes/.kss/.gym/.sap/.ay/.sgc) / "
                       "流式音频(.adx/.mod) / 含芯片音源的文件夹")


def load_chip_dir(input_path, on_progress=None):
    """含多格式芯片音源的文件夹 -> 归一化音轨列表(按名称自然排序)。"""
    # 同 load_source：用 CHIP_EXTS 而不是 P0_CHIP_EXTS（P1 与 SGC 也要收）
    files = [os.path.join(input_path, f) for f in os.listdir(input_path)
             if f.lower().endswith(CHIP_EXTS)]
    files.sort(key=natural_track_key)
    if not files:
        raise RuntimeError(f"{input_path}: 未发现支持的芯片音源文件")
    from chip_decode import load_chip_paths
    # 进度契约同 load_source 的芯片分支：(已完成, 总数, 秒数) -> 0..1
    cb = None
    if on_progress is not None:
        def cb(done, total, _secs, _f=on_progress):          # noqa: E306
            try:
                _f(min(0.999, float(done) / max(1.0, float(total or 1))))
            except Exception:                                # noqa: BLE001
                pass
    return load_chip_paths(files, on_progress=cb)


# --------------------------------------------------------------------------
# 模式 1: BIN + CUE
# --------------------------------------------------------------------------

def build_cue(bin_name, tracks, album, performer, use_cdtext):
    lines = []
    if use_cdtext:
        if album:
            lines.append(f'TITLE "{album}"')
        if performer:
            lines.append(f'PERFORMER "{performer}"')
    lines.append(f'FILE "{bin_name}" BINARY')
    for i, t in enumerate(tracks, 1):
        idx = f"{i:02d}"
        lines.append(f"  TRACK {idx} AUDIO")
        if use_cdtext:
            if t["title"]:
                lines.append(f'    TITLE "{t["title"]}"')
            if performer:
                lines.append(f'    PERFORMER "{performer}"')
        lines.append(f'    INDEX 00 {sector_to_mmssff(t["pre_sector"])}')
        lines.append(f'    INDEX 01 {sector_to_mmssff(t["start_sector"])}')
    return "\n".join(lines) + "\n"


def tracks_to_cd(tracks, out_dir, name, opts, on_progress=None, on_stage=None):
    """归一化音轨 -> BIN+CUE。opts: gap/album/performer/use_cdtext/verify/wav_dir/cd_wav

    on_progress(i, n, title, secs): 每写完一轨回调一次, 供界面显示进度。
    on_stage(text): 进入耗时阶段(回读校验)时回调, 界面据此更新状态文字。
    """
    prog = on_progress or (lambda *_a: None)
    stage = on_stage or (lambda _t: None)
    gap = int(round(max(0.0, float(opts.get("gap", 2.0))) * FRAME))
    os.makedirs(out_dir, exist_ok=True)
    bin_path = os.path.join(out_dir, name + ".bin")
    cue_path = os.path.join(out_dir, name + ".cue")

    infos = []
    sector = 0
    n_tracks = len(tracks)
    with open(bin_path, "wb") as out:
        for i, tr in enumerate(tracks, 1):
            raw_le = pcm_le(tr)
            nbytes = len(raw_le)
            pre_sector = sector
            if gap:
                out.write(b"\x00" * (gap * SECTOR))
                sector += gap
            start_sector = sector
            # 大端 CD-DA: 逐 2 字节交换后写出 (避免整份副本留在内存)
            block = 1 << 20
            for off in range(0, nbytes, block):
                seg = raw_le[off:off + block]
                out.write(seg if not tr["little_endian"] else _swap16(seg))
                del seg
            rem = nbytes % SECTOR
            if rem:
                out.write(b"\x00" * (SECTOR - rem))
            sector += (nbytes + SECTOR - 1) // SECTOR
            infos.append({
                "title": tr["name"],
                "pre_sector": pre_sector,
                "start_sector": start_sector,
                # 校验用: 只记录字节数, 不留整份音频副本
                "nbytes": nbytes,
                "track": tr,
            })
            prog(i, n_tracks, tr["name"], nbytes / (44100 * 2 * 2))

    cue = build_cue(name + ".bin", infos, opts.get("album", "") or name,
                    opts.get("performer", ""), opts.get("use_cdtext", True))
    with open(cue_path, "w", encoding="utf-8") as f:
        f.write(cue)

    wav_dir = opts.get("wav_dir")
    if not wav_dir and opts.get("cd_wav"):
        wav_dir = os.path.join(out_dir, "wav_preview")
    if wav_dir:
        os.makedirs(wav_dir, exist_ok=True)
        stage("导出试听 WAV…")
        for tr in tracks:
            raw_le = pcm_le(tr)
            wavp = os.path.join(wav_dir, tr["name"] + ".wav")
            with wave.open(wavp, "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(44100)
                w.writeframes(raw_le)

    if opts.get("verify"):
        stage(f"回读校验 {n_tracks} 轨…")
        ok, msg = verify_cd(bin_path, infos)
        if not ok:
            raise RuntimeError(f"BIN 校验失败: {msg}")
    return bin_path, cue_path, infos, sector


def verify_cd(bin_path, tracks):
    """校验 BIN: 扇区对齐 + 前导静音为零 + 逐轨音频与源 PCM 一致。

    逐轨流式比对, 不把整个 BIN 读进内存(几十首曲子可达数 GB)。
    """
    size = os.path.getsize(bin_path)
    if size % SECTOR != 0:
        return False, f"BIN 大小 {size} 不是 {SECTOR} 的整数倍"
    with open(bin_path, "rb") as fh:
        for t in tracks:
            pre = t["pre_sector"] * SECTOR
            start = t["start_sector"] * SECTOR
            gap_len = start - pre
            if gap_len:
                fh.seek(pre)
                if fh.read(gap_len) != b"\x00" * gap_len:
                    return False, f"轨 {t['title']} 前导静音区域非零"
            # 逐段重算期望的大端数据并比对, 避免整轨副本
            raw_le = pcm_le(t["track"])
            block = 1 << 20
            fh.seek(start)
            for off in range(0, t["nbytes"], block):
                seg = raw_le[off:off + block]
                want = seg if not t["track"]["little_endian"] else _swap16(seg)
                got = fh.read(len(want))
                if got != want:
                    return False, f"轨 {t['title']} 音频数据校验失败 (偏移 {off})"
            del raw_le
    return True, f"校验通过: {len(tracks)} 轨, BIN={size} 字节 ({size // SECTOR} 扇区)"


# --------------------------------------------------------------------------
# 模式 2: WAV (Pico-Plus / pico-infonesPlus 菜单播放)
#   要求: WAV / 16-bit(或24-bit) / 44.1kHz / 立体声 / .wav
#   设备无重采样器, 采样率必须恰好 44.1kHz
# --------------------------------------------------------------------------

def tracks_to_wav(tracks, out_dir, opts=None):
    """归一化音轨 -> 每轨一个 16-bit/44.1kHz/立体声 WAV。返回日志行列表。"""
    os.makedirs(out_dir, exist_ok=True)
    logs = []
    for i, tr in enumerate(tracks, 1):
        raw_le = pcm_le(tr)
        wavp = os.path.join(out_dir, f"{i:02d}_{tr['name']}.wav")
        with wave.open(wavp, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(raw_le)
        dur = len(raw_le) / 4 / 44100
        logs.append(f"   WAV {i:02d}/{len(tracks):02d}: {tr['name']}.wav "
                    f"({dur:.1f}s, 16bit/44.1kHz/立体声)")
    return logs


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def maybe_run_spc_worker():
    """若命令行含 --spc-decode / --chip-decode, 就以子进程 worker 身份执行解码并退出。

    load_spc_paths / chip_decode.load_chip_paths 会用子进程调用它执行真正危险的
    libgme 解码; 脚本与打包后的 exe(入口是 msu1_gui) 两个 main() 都必须先调用
    本函数分发。返回 True 表示已处理并退出。

    ⚠ --chip-decode 分支必须在最前(H11: 两种参数解析互相误吃)。
      以下 --spc-decode 分支整段未改, 保证 SPC 行为零回归。
    """
    if "--chip-decode" in sys.argv:
        from chip_decode import _run_chip_worker
        return _run_chip_worker()
    if "--spc-decode" not in sys.argv:
        return False
    try:
        i = sys.argv.index("--spc-decode")
        out_p = sys.argv[i + 1]
        rest = sys.argv[i + 2:]
        policy = "natural"
        if rest[:1] == ["--spc-length"] and len(rest) >= 2:
            policy = rest[1]
            rest = rest[2:]
        speed = 1.0
        if rest[:1] == ["--spc-speed"] and len(rest) >= 2:
            try:
                speed = float(rest[1])
            except ValueError:
                speed = 1.0
            rest = rest[2:]
        # 可选: 解码时长上限(见 load_spc_dir 的 decode_cap)。
        # 参数是**顺序解析**的 —— 不认识的前缀会原样落进 in_paths 当成输入
        # 文件, 所以旧版 exe 收到这个参数只会报「文件不存在」而不是静默出错。
        cap = None
        if rest[:1] == ["--spc-cap"] and len(rest) >= 2:
            try:
                cap = float(rest[1]) or None
            except ValueError:
                cap = None
            rest = rest[2:]
        in_paths = rest
        if not in_paths:
            raise RuntimeError("缺少输入 SPC 文件")
        _spc_decode_and_write(out_p, in_paths, policy, speed, cap)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("SPCDECODE_ERROR: " + str(e) + "\n")
        sys.stderr.flush()
        sys.exit(2)
    sys.exit(0)


def main():
    # 隐藏的 worker 模式: 由 load_spc_paths 以子进程调用, 真正执行 libgme 解码,
    # 把结果写成二进制帧到 out_path, 出错则写 SPCDECODE_ERROR 到 stderr 并以 2 退出。
    maybe_run_spc_worker()

    # 独立 SPC 自检: 用真实 SPC 文件验证整条解码链路 (需用户提供)
    if "--selftest-spc" in sys.argv:
        spc = sys.argv[sys.argv.index("--selftest-spc") + 1]
        try:
            tracks = load_spc(spc)
        except Exception as e:  # noqa: BLE001
            print("SPC SELFTEST FAIL:", e)
            sys.exit(1)
        total = sum(len(t["pcm"]) for t in tracks)
        print(f"SPC SELFTEST PASS: {len(tracks)} 轨, {total} 字节 PCM (16bit/44.1k/立体声)")
        for t in tracks:
            dur = len(t["pcm"]) / 4 / 44100
            print(f"   - {t['name']}: {dur:.1f}s  little_endian={t['little_endian']}")
        sys.exit(0)

    ap = argparse.ArgumentParser(
        description="游戏音乐 -> 音频 CD (BIN+CUE) / Pico-Plus WAV 转换器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input", help="MSU-1 文件夹 / .cue(+.bin/.iso) 光盘镜像 / .mp3 / .spc / 含 SPC 文件夹")
    # 模式1: CD
    ap.add_argument("-o", "--output-dir", default="output", help="BIN+CUE 输出目录")
    ap.add_argument("-n", "--name", default="", help="BIN/CUE 文件名(不含扩展名)")
    ap.add_argument("--album", default="", help="CUE 专辑名 (CD-TEXT)")
    ap.add_argument("--performer", default="", help="CUE 演奏者 (CD-TEXT)")
    ap.add_argument("--gap", type=float, default=2.0, help="轨前导静音秒数, 默认 2")
    ap.add_argument("--wav-dir", default="", help="同时导出 WAV 供电脑试听 (CD 模式)")
    ap.add_argument("--no-cdtext", action="store_true", help="关闭 CUE 中的 CD-TEXT")
    # 模式 2: WAV
    ap.add_argument("--wav-out", default="", help="WAV 输出目录 (16bit/44.1kHz/立体声, Pico-Plus)")
    # 通用
    ap.add_argument("--header", type=int, default=-1,
                    help="(MSU-1)跳过文件头字节数; -1=自动检测")
    ap.add_argument("--verify", action="store_true", help="写完后回读校验 BIN/CD")
    ap.add_argument("--xa-music-only", action="store_true",
                    help="纯数据盘改抽 XA 时只导音乐（按时长+立体声宽度滤掉音效/语音）")
    ap.add_argument("--xa-split", action="store_true",
                    help="XA 按静音间隔切曲（一段一个文件；默认关）")
    args = ap.parse_args()

    _xa_stats = {}
    tracks = load_source(args.input, xa_music_only=args.xa_music_only,
                         xa_stats=_xa_stats, xa_split=args.xa_split)
    if _xa_stats.get("skipped"):
        print("XA 只导音乐: %d 条流里滤掉 %d 条音效/语音，保留 %d 条"
              % (_xa_stats["total"], _xa_stats["skipped"], _xa_stats["kept"]))
    if _xa_stats.get("split_streams"):
        print("XA 按静音切曲: %d 条流切成 %d 段"
              % (_xa_stats["split_streams"], _xa_stats["segments"]))
    if not tracks:
        print("未找到任何音轨", file=sys.stderr)
        sys.exit(1)
    if len(tracks) > 99:
        print("警告: 音频 CD 最多 99 轨, 超出部分无法刻录", file=sys.stderr)

    name = args.name or os.path.splitext(os.path.basename(
        args.input.rstrip("\\/")))[0] or "msu1_audio"

    cd_opts = {
        "gap": args.gap, "album": args.album, "performer": args.performer,
        "use_cdtext": not args.no_cdtext, "verify": args.verify,
        "wav_dir": args.wav_dir or None, "cd_wav": False,
    }
    # 模式 1: BIN+CUE
    bin_path, cue_path, infos, total_sectors = tracks_to_cd(
        tracks, args.output_dir, name, cd_opts)
    dur_sec = total_sectors / FRAME
    print(f"✅ [CD] 已生成 {len(infos)} 轨")
    print(f"   总时长: {int(dur_sec // 60)}分{dur_sec % 60:.0f}秒  ({total_sectors} 扇区)")
    if total_sectors > 360000:
        print("⚠️  超过 80 分钟 CD 容量, 可能无法刻录到普通 CD-R", file=sys.stderr)
    print(f"   BIN: {bin_path}")
    print(f"   CUE: {cue_path}")

    # 模式 2: WAV
    if args.wav_out:
        print("✅ [WAV] 生成 16-bit/44.1kHz/立体声 WAV (Pico-Plus):")
        for line in tracks_to_wav(tracks, args.wav_out):
            print(line)

    if args.verify:
        ok, msg = verify_cd(bin_path, infos)
        print(("✅ " if ok else "❌ ") + msg)
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
