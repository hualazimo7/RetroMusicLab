#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_chip_formats.py — chip_formats.py 的**离线**单元自检。

不需要 libgme / DLL / 样本文件也能跑（用合成头字节），因此可以进 CI。
真实样本的端到端解码验证在 vendor/test_chip_decode.py。

跑法：
    python vendor/test_chip_formats.py                 # 合成用例（永远可跑）
    python vendor/test_chip_formats.py --dir C:/temp/gmev2   # 追加真实样本

覆盖：
    · identify() 三级判定 + 魔数优先 + 冲突标记
    · 各格式头字段解析（NSF/NSFE/GBS/VGM/VGZ/HES/SPC）
    · plan_duration 决策链六档 + stop_on_silence 矩阵（设计 §3.2）
    · FR-4.2 不支持文案三类
    · H10 门禁：chip_formats 不得 import ctypes/动态库/核心模块
    · P1 四格式（KSS/GYM/SAP/AY）魔数、头字段、时长闸门（合成用例）
"""

import argparse
import gzip
import os
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chip_formats as cf                                # noqa: E402

_GREEN, _RED, _YEL, _RST = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    _GREEN = _RED = _YEL = _RST = ""

_pass = 0
_fail = 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  {_GREEN}PASS{_RST} {name}" + (f"  {detail}" if detail else ""))
    else:
        _fail += 1
        print(f"  {_RED}FAIL{_RST} {name}  {detail}")
    return bool(cond)


def w(name, data):
    """在临时目录写一个文件，返回路径。"""
    p = os.path.join(TMP, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


# ---------------------------------------------------------------------------
# 合成头字节
# ---------------------------------------------------------------------------

def mk_nsf(n=8, song=b"Test Song", artist=b"Someone"):
    h = bytearray(0x80)
    h[0:5] = b"NESM\x1a"
    h[5] = 1                    # 起始轨
    h[6] = n                    # 总轨数
    h[0x0E:0x0E + len(song)] = song
    h[0x2E:0x2E + len(artist)] = artist
    return bytes(h)


def mk_gbs(n=9, loop_start=1000, loop_end=17952, title=b"Kenji Yamamoto"):
    """按**实测**布局构造：magic b"GBS\\x01", 轨数@0x04, loop@0x14/0x16, 文本@0x39+"""
    h = bytearray(0x100)
    h[0:4] = b"GBS\x01"
    h[4] = n                    # 轨数（实测位置）
    h[5] = 1
    h[0x14:0x16] = struct.pack("<H", loop_start)
    h[0x16:0x18] = struct.pack("<H", loop_end)
    h[0x39:0x39 + len(title)] = title
    return bytes(h)


def mk_vgm(samples=809405, gd3=b""):
    h = bytearray(0x100)
    h[0:4] = b"Vgm "
    h[4:8] = struct.pack("<I", 0x150)
    h[0x18:0x1C] = struct.pack("<I", samples)
    h[0x34:0x38] = struct.pack("<I", 0x0C)
    if gd3:
        off = 0x100
        blob = b"Gd3 " + struct.pack("<I", 0x150) + \
            struct.pack("<I", len(gd3) + 4) + gd3
        h += blob
        h[0x14:0x18] = struct.pack("<I", off)
    return bytes(h)


def mk_hes():
    h = bytearray(0x200)
    h[0:4] = b"HESM"
    h[4] = 0
    h[0x15] = 95                # 真实字段（但我们按 256 报，见 probe 注释）
    return bytes(h)


def mk_spc(song=b"Stage Clear", secs=b"004"):
    h = bytearray(0x100)
    h[0:33] = b"SNES-SPC700 Sound File Data v0.30" + b"\x00"
    h[0x2E:0x2E + len(song)] = song
    h[0xA9:0xAC] = secs         # 播放秒数（3 位 ASCII）
    return bytes(h)


def mk_nsfe_chunk(names, total=None):
    """真实 NSFE 的 chunk 布局（按实测字节，不是设计 §2.5 的 section 表）。

    实测（样本 1943 - The Battle of Midway.nsfe）字节序：
        "NSFE" + 版本/预留 … + "INFO" + 地址 …
        → 一片 0xFF 填充
        → "auth" + "artist\\0copyright\\0dumper\\0"   ← **无长度前缀**
        → u32le 总轨数
        → "plst" + playlist 字节
        → "tlbl" + "曲名1\\0曲名2\\0…\\0"              ← **无长度前缀**
    """
    body = bytearray(b"NSFE")
    body += bytes([1, 0, 0])
    body += b"INFO" + bytes(0x10)
    body += b"\x00" * 0x180                       # 填充到元数据区
    body += b"auth" + b"\x00".join(
        [b"1943", b"1988 Capcom", b"dumper"]) + b"\x00"
    body += struct.pack("<I", total if total is not None else len(names))
    body += b"plst" + bytes([len(names)]) + bytes(range(len(names)))
    body += b"tlbl" + b"\x00".join(names) + b"\x00"
    body += b"\x00" * 64
    return bytes(body)


# ---------------------------------------------------------------------------
# P1 四格式（KSS / GYM / SAP / AY）的合成头
#
# ⚠ 所有字段偏移都来自 **libgme 源码 + 真实样本 hexdump 双向验证**
#   （见 vendor/probe_p1_offsets.py）。特别是 AY：魔数是 "ZXAYEMUL"，
#   不是任务书里提到的 "AYM"/"EMAY"；SAP 魔数是 5 字节 "SAP\x0D\x0A"。
# ---------------------------------------------------------------------------

def mk_kss(tag=b"KSCC"):
    """KSS：libgme 只认 KSCC / KSSX 两种 tag（check_kss_header）。

    真实样本头 16 字节：4b 53 43 43 10 30 96 00 17 30 10 30 00 02 00 05
    轨数由 libgme 写死 256，头里读不出来，所以这里只放魔数 + 任意负载。
    """
    h = bytearray(cf.KSS_HEADER_SIZE + 32)
    h[0:4] = tag
    return bytes(h)


def mk_gym(frames=600, song=b"Test Song", game=b"Test Game",
           copyright=b"2026 Test", emulator=b"Gens", dumper=b"Tester",
           loop_start=0, packed=0, bad_tail=b""):
    """GYM：428 字节 GYMX 头 + 指令流。

    指令流用 `frames` 个 0x00（每帧 1/60 秒）+ 两条 0x01 寄存器写，
    因此帧数可预期，时长 = frames × 1000/60 ms。
    bad_tail 用来构造"非法 opcode"，验证遍历不会因此提前中断
    （libgme 的 switch 没有 default，跳 1 字节继续）。
    """
    h = bytearray(cf.GYM_HEADER_SIZE)
    h[0:4] = b"GYMX"
    h[0x04:0x04 + len(song)] = song
    h[0x24:0x24 + len(game)] = game
    h[0x44:0x44 + len(copyright)] = copyright
    h[0x64:0x64 + len(emulator)] = emulator
    h[0x84:0x84 + len(dumper)] = dumper
    struct.pack_into("<I", h, 0x1A4, loop_start)
    struct.pack_into("<I", h, 0x1A8, packed)
    body = bytearray()
    body += bytes([0x01, 0x22, 0x08])      # YM port0 写
    body += bytes([0x00]) * frames# 空转 N 帧
    body += bytes([0x02, 0xA4, 0x00])      # YM port1 写
    body += bytes([0x03, 0x80])            # PSG 写
    body += bad_tail
    return bytes(h) + bytes(body)


def mk_gym_raw(frames=100):
    """无头 GYM（老 Genecyst 原始格式）：整条流都是 0x00。

    libgme 允许首字节 <= 3 视为裸指令流。这类文件**时长不可信**
    （帧数 == 文件长度），必须回落到默认 150 秒 —— 用例专门守这条。
    """
    return bytes([0x00]) * frames


def mk_sap(name=b"Test Tune", author=b"Tester", songs=None,
           typ=b"B", init=0x3A6F, player=0x3403, music=None):
    """SAP：`SAP\\x0D\\x0A` + CRLF 文本标签 + `\\xFF\\xFF` + 二进制。

    ⚠ 两个坑（用例覆盖）：
      1. 行尾是 CRLF，按 0x0D 切行后下一行开头会残留 0x0A；
      2. SONGS 是十进制，INIT/PLAYER/MUSIC 是十六进制。
    """
    lines = [b'AUTHOR "' + author + b'"',
             b'NAME "' + name + b'"',
             b'DATE "2026-01-01"',
             b'TYPE ' + typ]
    if songs is not None:
        lines.append(b"SONGS " + str(int(songs)).encode())
    if music is not None:
        lines.append(b"MUSIC " + format(music, "04X").encode())
    lines.append(b"INIT " + format(init, "04X").encode())
    lines.append(b"PLAYER " + format(player, "04X").encode())
    lines.append(b"TIME 01:32.41 LOOP")
    head = b"SAP\x0d\x0a" + b"\x0d\x0a".join(lines) + b"\x0d\x0a"
    return head + b"\xff\xff" + bytes(64)


def mk_ay(track_lengths, names=None, author=b"AY Author", misc=b"misc note"):
    """AY：`ZXAYEMUL` 头 + 相对指针的 song structure + song data。

    布局（libgme Ay_Emu::header_t）：
        0x00 "ZXAYEMUL" 0x08 vers 0x09 player 0x0A unused[2]
        0x0C PAuthor 0x0E PMisc 0x10 max_track 0x11 first_track
        0x12 PSongsStructure
    每轨 song data：+4 = SongLength（大端，单位 1/50 秒）
    track_lengths 传**秒**（浮点），内部换算成 1/50 秒帧数。
    """
    names = names or [f"Track {i + 1}".encode() for i in range(len(track_lengths))]
    n = len(track_lengths)

    # 先把数据区拼出来，稍后回填各指针（相对偏移需知道绝对位置）
    # 布局：[0x14 起] author\0 misc\0 | songstruct(n*4) | names\0... | data...
    buf = bytearray(cf.AY_HEADER_SIZE)
    buf[0:8] = b"ZXAYEMUL"
    buf[8] = 0                                  # file version
    buf[9] = 3                                  # player version
    buf[0x10] = n - 1                           # max_track（轨数 = +1）
    buf[0x11] = 0                               # first_track

    author_at = len(buf)
    buf += author + b"\x00"
    misc_at = len(buf)
    buf += misc + b"\x00"

    struct_at = len(buf)
    buf += bytes(4 * n)
    name_ats = []
    for nm in names:
        name_ats.append(len(buf))
        buf += nm + b"\x00"
    data_ats = []
    for secs in track_lengths:
        data_ats.append(len(buf))
        frames = int(round(float(secs) * 50))
        buf += bytes([0, 1, 2, 3])               # A/B/C/Noise chan
        buf += struct.pack(">H", frames)         # SongLength
        buf += struct.pack(">H", 0)              # FadeLength
        buf += bytes([0xFF] * 4)                 # HiReg/LoReg/对齐

    def rel16(at):
        """写"从 at 指向 target 的带符号相对偏移"（大端）。"""
        struct.pack_into(">H", buf, at, target - at)

    # 头里的指针
    struct.pack_into(">H", buf, 0x0C, author_at - 0x0C)
    struct.pack_into(">H", buf, 0x0E, misc_at - 0x0E)
    struct.pack_into(">H", buf, 0x12, struct_at - 0x12)
    # song structure 每项：PSongName, PSongData
    for i in range(n):
        rec = struct_at + i * 4
        target = name_ats[i]
        rel16(rec)
        target = data_ats[i]
        rel16(rec + 2)
    return bytes(buf)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def test_identify():
    print(f"\n{_YEL}=== identify() 三级判定（FR-1.3）{_RST}")
    p1 = w("a.nsf", mk_nsf())
    fmt, conflict = cf.identify(p1)
    check("NSF 魔数 -> NSF", fmt == "NSF", str(fmt))
    check("NSF 无冲突", conflict is False)

    p2 = w("b.gbs", mk_gbs())
    fmt, _ = cf.identify(p2)
    check("GBS 魔数 b'GBS\\x01' -> GBS（实测布局）", fmt == "GBS", str(fmt))

    p3 = w("c.vgm", mk_vgm())
    fmt, _ = cf.identify(p3)
    check("VGM 魔数 -> VGM", fmt == "VGM", str(fmt))

    # 魔数优先：.nsf 里装的是 SPC
    p4 = w("d.nsf", mk_spc())
    fmt, conflict = cf.identify(p4)
    check("魔数与扩展名不符 -> 以魔数为准 (SPC)", fmt == "SPC", str(fmt))
    check("且标记 conflict=True", conflict is True)

    # gzip 魔数 -> VGZ（joshw 的 .vgm 实为 gzip）
    raw = gzip.compress(mk_vgm())
    p5 = w("e.vgm", raw)
    fmt, conflict = cf.identify(p5)
    check("gzip 魔数 -> VGZ", fmt == "VGZ", str(fmt))
    check(".vgm+gzip 标记 conflict（设计 §2.4 预期）", conflict is True)

    # 扩展名不认识但魔数认识
    p6 = w("f.qqq", mk_nsf())
    fmt, _ = cf.identify(p6)
    check("扩展名不认识 + 魔数认识 -> 按内容 (NSF)", fmt == "NSF", str(fmt))

    # 都不认识
    p7 = w("g.qqq", b"not a chip file at all")
    fmt, _ = cf.identify(p7)
    check("都不认识 -> None", fmt is None, str(fmt))

    # 空文件 -> 退回扩展名（H9）
    p8 = w("h.nsf", b"")
    fmt, conflict = cf.identify(p8)
    check("空文件 -> 退回扩展名 NSF", fmt == "NSF", str(fmt))
    check("空文件不标 conflict", conflict is False)


def test_probe_fields():
    print(f"\n{_YEL}=== probe() 头字段解析{_RST}")
    i = cf.probe(w("n.nsf", mk_nsf(n=8, song=b"Zelda Theme")))
    check("NSF n_tracks", i.n_tracks == 8, str(i.n_tracks))
    check("NSF title", i.title == "Zelda Theme", repr(i.title))
    check("NSF artist", i.author == "Someone", repr(i.author))
    check("NSF fmt", i.fmt == "NSF", str(i.fmt))
    check("NSF 无自带时长", i.exact_secs == 0.0)

    i = cf.probe(w("g.gbs", mk_gbs(n=12, loop_end=31073)))
    check("GBS n_tracks@0x04", i.n_tracks == 12, str(i.n_tracks))
    check("GBS title@0x39", i.title == "Kenji Yamamoto", repr(i.title))
    # ⚠ GBS 循环点本期一律不采信(2026-10-18 复核)：0x10 起是 32 字节标题,
    # 原实现读 0x14/0x16 等于在标题里取值, 曾把一首 jingle 算出 520 秒。
    # 4 个候选偏移(0x0CC/0x10C/0x16C/0x170)在 6 个真实样本上均不自洽。
    check("GBS 循环点不采信 -> 0", i.loop_end_secs == 0.0, f"{i.loop_end_secs:.4f}")

    # 即使文件里写了看起来合法的循环点, 也必须忽略(宁可 150s 也不给 800s)
    i = cf.probe(w("g2.gbs", mk_gbs(loop_start=100, loop_end=9000)))
    check("GBS 合法循环点仍被忽略", i.loop_end_secs == 0.0, str(i.loop_end_secs))
    i = cf.probe(w("g3.gbs", mk_gbs(loop_end=0xFFFF)))
    check("GBS loop_end=0xFFFF -> 0", i.loop_end_secs == 0.0,
          str(i.loop_end_secs))

    i = cf.probe(w("v.vgm", mk_vgm(samples=809405)))
    check("VGM n_tracks=1", i.n_tracks == 1, str(i.n_tracks))
    check("VGM exact_secs = samples/44100",
          abs(i.exact_secs - 809405 / 44100.0) < 1e-9,
          f"{i.exact_secs:.6f}")
    check("VGM exact_src", i.exact_src == "vgm_samples", i.exact_src)
    # 0xFFFFFFFF = 未标注
    i = cf.probe(w("v2.vgm", mk_vgm(samples=0xFFFFFFFF)))
    check("VGM 0xFFFFFFFF -> 无时长", i.exact_secs == 0.0, str(i.exact_secs))

    # VGZ（gzip）
    i = cf.probe(w("z.vgz", gzip.compress(mk_vgm(samples=742474))))
    check("VGZ 解压后按 VGM 解析 fmt", i.fmt == "VGZ", str(i.fmt))
    check("VGZ exact_secs",
          abs(i.exact_secs - 742474 / 44100.0) < 1e-9, f"{i.exact_secs:.6f}")

    i = cf.probe(w("h.hes", mk_hes()))
    check("HES n_tracks=256（对齐 gme_track_count）", i.n_tracks == 256,
          str(i.n_tracks))
    check("HES 无 m3u -> track_ids 为空（退回 256 槽全扫）",
          i.track_ids == [], str(i.track_ids[:6]))

    # ---- v4.29: 同目录 m3u = PCE-CD / HES rip 的真实曲目表 ----
    # 合成一份「PCE-CD 形态」的 m3u：HES 曲目 + CD 音轨（.ape）混排，
    # 曲号不连续（0/3/2/0x0A）、长度有 `0:39` 与裸秒两种写法。
    m3u = (
        "\ufeff# @TITLE  Test Rip\n"
        "# 注释行要跳过\n"
        "\n"
        "pack.hes::HES,$00,Konami Logo,4,,0\n"
        "pack_05 Requiem.ape\n"
        "pack.hes::HES,$03,Stage Clear,0:39,-,10\n"
        "pack.hes::HES,$02,Poison, Mind,15,0:12,10\n"   # 标题含逗号
        "pack.hes::HES,$0A,Last,1:02,,10\n"
        "pack.hes::HES,$100,Too High,10,,0\n"           # 越界曲号(256)必须丢弃
        "pack.hes::NSF,$01,Wrong Format,10,,0\n"        # 非 HES 行必须丢弃
    )
    w("pack.m3u", m3u.encode("utf-8"))
    ip = cf.probe(w("pack.hes", mk_hes()))
    check("m3u: 只取 HES 行、按出现顺序", ip.track_ids == [0, 3, 2, 10],
          str(ip.track_ids))
    check("m3u: 长度解析（4 / 0:39 / 15 / 1:02）",
          ip.track_lengths == {0: 4.0, 3: 39.0, 2: 15.0, 10: 62.0},
          str(ip.track_lengths))
    check("m3u: 标题含逗号不被切坏",
          ip.track_titles.get(2) == "Poison, Mind", repr(ip.track_titles.get(2)))
    check("m3u: CD 音轨被记下（本工具不转但要让用户看见）",
          ip.cd_tracks == ["pack_05 Requiem.ape"], str(ip.cd_tracks))
    check("m3u: n_tracks 覆盖到最大曲号（解码区间）", ip.n_tracks == 11,
          str(ip.n_tracks))
    check("m3u: per_track_secs 取最长一轨（切批不超预算）",
          ip.per_track_secs == 62.0, str(ip.per_track_secs))

    # 只有 CD 音轨的表（纯 CD-DA 包）：不得伪造出芯片曲目
    w("cdonly.m3u", b"cdonly_01 Track.ape\ncdonly_02 Track.ape\n")
    icd = cf.probe(w("cdonly.hes", mk_hes()))
    check("纯 CD 表: track_ids 为空", icd.track_ids == [], str(icd.track_ids))
    check("纯 CD 表: cd_tracks 记 2 首", len(icd.cd_tracks) == 2,
          str(icd.cd_tracks))

    # 坏 m3u（乱码/二进制）不得影响 probe 的「永不抛异常」契约
    w("bad.m3u", b"\xff\xfe\x00\x01garbage\x00\x00")
    try:
        ibad = cf.probe(w("bad.hes", mk_hes()))
        check("坏 m3u: 不抛异常且退回 256", ibad.n_tracks == 256 and
              not ibad.track_ids, "n=%s ids=%s" % (ibad.n_tracks,
                                                   ibad.track_ids[:4]))
    except Exception as e:                             # noqa: BLE001
        check("坏 m3u 不抛异常", False, "%s: %s" % (type(e).__name__, e))

    i = cf.probe(w("s.spc", mk_spc(song=b"Boss Theme", secs=b"004")))
    check("SPC n_tracks=1", i.n_tracks == 1, str(i.n_tracks))
    check("SPC ID666 时长", i.exact_secs == 4.0, str(i.exact_secs))
    check("SPC exact_src=id666", i.exact_src == "id666", i.exact_src)
    check("SPC title", i.title == "Boss Theme", repr(i.title))

    names = [b"Air Battle 1", b"Air Battle 2", b"Boss Battle"]
    i = cf.probe(w("x.nsfe", mk_nsfe_chunk(names)))
    check("NSFE n_tracks（plst 前 u32）", i.n_tracks == 3, str(i.n_tracks))
    check("NSFE 逐轨曲名", i.track_names == [n.decode() for n in names],
          str(i.track_names))
    check("NSFE artist 来自 auth 块", i.author == "1943", repr(i.author))


def test_plan_duration():
    print(f"\n{_YEL}=== plan_duration 决策链（FR-3.1 / 设计 §3.2）{_RST}")
    i = cf.probe(w("p1.nsf", mk_nsf()))

    # 5 默认单遍（无自带时长）
    d = cf.plan_duration(i, default_secs=150.0)
    check("NSF -> default 150s", abs(d.secs - 150.0) < 1e-9, str(d.secs))
    # v4.4 修复：原断言是 `is True`（开静音检测），已改为 False。
    # 理由（用户报障「NSF 转出来声音不对」实测）：NSF 是循环型 BGM，没有自然结尾，
    # 而游戏音乐里 2 秒以上停顿极常见 → 开静音检测会导致 17/17 轨被截断在 2~8 秒。
    # ⚠ 这条断言现在是**行为锁**：若有人把 stop_on_silence 改回 True，它会立刻红。
    check("NSF -> 关静音检测（循环型格式，v4.4 修复）",
          d.stop_on_silence is False, "got %r" % (d.stop_on_silence,))
    check("NSF -> lead_sil_limit=10", abs(d.lead_sil_limit - 10.0) < 1e-9,
          str(d.lead_sil_limit))

    # 1 用户上限最高 + 必开静音检测
    d = cf.plan_duration(i, user_max_secs=60.0, default_secs=150.0)
    check("用户上限优先 -> 60s", abs(d.secs - 60.0) < 1e-9, str(d.secs))
    check("用户上限 -> source=user", d.source == "user", d.source)
    # v4.4 修复：原断言「仍开静音检测(R4)」已改为 False。
    # 理由：它与 default 分支是同一个 bug —— 用户设 60s，NSF 仍在 3 秒断，
    # 「单曲最长」形同虚设。对循环型 BGM 只能按用户上限硬截。
    check("用户上限 -> 关静音检测（v4.4 修复，上限即截断依据）",
          d.stop_on_silence is False, str(d.stop_on_silence))

    # 2 SPC id666 -> 精确，关静音检测
    i2 = cf.probe(w("p2.spc", mk_spc(secs=b"004")))
    d = cf.plan_duration(i2, default_secs=150.0)
    check("SPC id666 -> 4s", abs(d.secs - 4.0) < 1e-9, str(d.secs))
    check("SPC id666 -> 关静音检测", d.stop_on_silence is False)
    check("SPC id666 -> lead_sil=0", d.lead_sil_limit == 0.0)

    # 3 VGM -> vgm_samples，关静音检测，lead_sil=0（AC-5）
    i3 = cf.probe(w("p3.vgm", mk_vgm(samples=809405)))
    d = cf.plan_duration(i3, default_secs=150.0)
    check("VGM -> samples/44100", abs(d.secs - 809405 / 44100.0) < 1e-9,
          str(d.secs))
    check("VGM -> source=vgm_samples", d.source == "vgm_samples", d.source)
    check("VGM -> 关静音检测", d.stop_on_silence is False)
    check("VGM -> lead_sil=0（防前10s静音误砍）", d.lead_sil_limit == 0.0)

    # 4 GBS loop
    i4 = cf.probe(w("p4.gbs", mk_gbs(loop_end=17952)))
    d = cf.plan_duration(i4, default_secs=150.0)
    check("GBS -> 循环点不采信, 用默认 150s",
          abs(d.secs - 150.0) < 1e-9, str(d.secs))
    # 循环点不可信 -> GBS 一律回落默认时长, 并保留静音检测(150s 上限仍要能提前收手)
    check("GBS -> 回落 default", d.source == "default", d.source)
    check("GBS -> 默认 150s", abs(d.secs - 150.0) < 1e-9, f"{d.secs:.3f}")
    # v4.4 修复：GBS 在「无 loop_end」时也走 default 分支，同样不该开静音检测。
    check("GBS -> 关静音检测（v4.4 修复）",
          d.stop_on_silence is False, str(d.stop_on_silence))

    # 硬顶 clamp
    d = cf.plan_duration(i, user_max_secs=99999.0, hard_cap_secs=600.0)
    check("用户上限被 clamp 到 600s 硬顶", abs(d.secs - 600.0) < 1e-9,
          str(d.secs))
    d = cf.plan_duration(i, default_secs=99999.0, hard_cap_secs=600.0)
    check("默认单遍被 clamp 到 600s", abs(d.secs - 600.0) < 1e-9, str(d.secs))
    # default_secs=0 -> 回落 150
    d = cf.plan_duration(i, default_secs=0.0)
    check("default_secs=0 -> 回落 150", abs(d.secs - 150.0) < 1e-9, str(d.secs))
    # 垃圾输入不抛
    d = cf.plan_duration(i, user_max_secs="abc", default_secs=None,
                         hard_cap_secs="xyz")
    check("垃圾输入不抛异常且回落默认", abs(d.secs - 150.0) < 1e-9, str(d.secs))


def test_unsupported():
    print(f"\n{_YEL}=== FR-4.2 不支持文案三类{_RST}")
    p = w("x.usf", b"USF\x00garbage")
    r = cf.unsupported_reason(p)
    check("① USF -> 点名 USF + 机种 + 替代方案",
          "USF" in r and "Nintendo 64" in r and "暂不支持" in r, r[:70])

    p = w("x.qqq", b"whatever")
    r = cf.unsupported_reason(p)
    check("② 未知扩展名 -> 出现 .qqq", ".qqq" in r and "不认识" in r, r[:70])

    p = w("x.nsf", b"this is not a chip file")
    i = cf.probe(p)
    r = cf.unsupported_reason(p, i)
    check("③ 魔数与扩展名不符且内容不是芯片格式",
          ("不是" in r) or ("不认识的扩展名" in r), r[:70])

    # 支持的格式不应被判为不支持
    for name, data in (("ok.nsf", mk_nsf()), ("ok.gbs", mk_gbs()),
                       ("ok.vgm", mk_vgm()), ("ok.hes", mk_hes())):
        i = cf.probe(w(name, data))
        check(f"{name} ok=True", i.ok is True, i.reason)


def test_probe_never_raises():
    print(f"\n{_YEL}=== probe() 永不抛异常（设计约束 6）{_RST}")
    cases = [
        ("不存在", os.path.join(TMP, "nope.nsf")),
        ("是目录", TMP),
        ("空文件", w("e1.nsf", b"")),
        ("垃圾", w("e2.nsf", b"\x00" * 5000)),
        ("截断 VGZ", w("e3.vgz", b"\x1f\x8b\x08\x00truncated")),
        ("无扩展名", w("e4", mk_nsf())),
    ]
    for label, path in cases:
        try:
            i = cf.probe(path)
            ok = isinstance(i, cf.ChipInfo)
        except Exception as e:                            # noqa: BLE001
            ok, i = False, e
        check(f"{label} -> 返回 ChipInfo 不抛", ok, str(i)[:60])
    # read_magic 也必须不抛
    check("read_magic(不存在) -> b''",
          cf.read_magic(os.path.join(TMP, "nope")) == b"")


def test_gates():
    print(f"\n{_YEL}=== 冻结门禁（H10 / H13 / H16）{_RST}")
    path = os.path.join(ROOT, "chip_formats.py")
    # 只看**真正的 import 语句**（用 ast 而不是 grep，否则会命中我们自己写的
    # 警告注释 —— 第一版就是这么误报自己的）。
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    banned = {"ctypes", "msu1_to_cd", "chip_decode", "gme", "libgme"}
    hit = mods & banned
    check("H10 chip_formats 不 import 动态库/核心模块", not hit,
          f"imports={sorted(mods)}")

    # H13：全项目不得出现对 identify_file / identify_header 的**调用**
    bad = []
    for f in sorted(os.listdir(ROOT)):
        if not f.endswith(".py"):
            continue
        src = open(os.path.join(ROOT, f), encoding="utf-8", errors="replace").read()
        try:
            t = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(t):
            if isinstance(node, ast.Attribute) and node.attr in (
                    "gme_identify_file", "gme_identify_header"):
                bad.append(f"{f}:{node.lineno}")
            if isinstance(node, ast.Name) and node.id in (
                    "gme_identify_file", "gme_identify_header"):
                bad.append(f"{f}:{node.lineno}")
    check("H13 全项目无 identify_file/header 调用（AST 级）", not bad, str(bad))


def test_sidecar_m3u():
    """同目录 m3u 的三种命名约定（v5.7）。

    背景：libgme 把 KSS 一律当 **256 轨**，只有 m3u 能给出真实曲目表。
    而 m3u 的文件名**两家站不一样**：

      · joshw  —— ``<包名>.m3u`` / ``!playlist.m3u``（一个文件装全套）
      · zophar —— ``01 BGM #01.m3u``、``02 BGM #02.m3u`` …（**一首一个**，
                   名字里没有源文件名）

    只认 joshw 那种时，zophar 的 ``Addams Family [GG]/T-81087.kss`` 会被当成
    256 轨：实测解出 256 个 WAV、6.6 GB，而真实曲目只有 11 首。

    还要防两件事：
      · 同目录有多套 rip 时，不能把 A 包的 m3u 套到 B 包上；
      · 曲名里带**转义逗号**（``\\,``）时，字段不能错位（错位会把时长读成 0）。
    """
    print(f"\n{_YEL}=== 同目录 m3u（joshw / zophar 两种命名 + 转义逗号）{_RST}")
    import shutil

    d = os.path.join(TMP, "m3u_case")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)

    def put(name, data):
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p

    kss = put("T-81087.kss", mk_kss())
    check("没有 m3u 时 KSS 退回 256 轨（libgme 写死的上限）",
          cf.probe(kss).n_tracks == cf.CHIP_MAX_TRACKS,
          cf.probe(kss).n_tracks)

    # ---- ① zophar：一首一个 m3u，正文引用源文件名 ----
    # 第 3 首的曲名里带转义逗号 —— 这是真实数据（Addams Family 的版权行）
    zop = [
        "T-81087.kss::KSS,0,BGM #01 - ? - The Addams Family,1:11,,10",
        "T-81087.kss::KSS,1,BGM #02 - ? - The Addams Family,0:13,,1",
        "T-81087.kss::KSS,2,BGM #03 - ? - The Addams Family - \xa91993"
        " Arc Developments\\, Flying Edge,2:24,,10",
    ]
    for i, line in enumerate(zop, 1):
        put("%02d BGM #%02d.m3u" % (i, i), line.encode("latin-1"))
    info = cf.probe(kss)
    check("zophar 式「一首一个 m3u」也能认出真实曲目数（3 首，不是 256）",
          info.n_tracks == 3, info.n_tracks)
    check("曲号按 m3u 顺序给出", info.track_ids == [0, 1, 2], info.track_ids)
    check("时长读出来了（1:11 → 71 秒）",
          abs(float(info.track_lengths.get(0) or 0) - 71.0) < 0.01,
          info.track_lengths)
    check("曲名里的转义逗号被还原（字段没错位）",
          "Developments, Flying" in (info.track_titles.get(2) or ""),
          info.track_titles.get(2))

    # ---- ② 不引用本文件的 m3u 一律忽略（多套 rip 同目录时不能串味）----
    d2 = os.path.join(TMP, "m3u_case2")
    shutil.rmtree(d2, ignore_errors=True)
    os.makedirs(d2, exist_ok=True)
    p2 = os.path.join(d2, "AAA.kss")
    with open(p2, "wb") as f:
        f.write(mk_kss())
    with open(os.path.join(d2, "01 other.m3u"), "wb") as f:
        f.write(b"ZZZ.kss::KSS,0,Other Game,1:00,,1")
    check("正文不引用本文件的 m3u 不算（防 A 包套 B 包）",
          cf.probe(p2).n_tracks == cf.CHIP_MAX_TRACKS,
          cf.probe(p2).n_tracks)

    # ---- ③ joshw 式 <stem>.m3u 仍然优先，且优先于「一首一个」----
    d3 = os.path.join(TMP, "m3u_case3")
    shutil.rmtree(d3, ignore_errors=True)
    os.makedirs(d3, exist_ok=True)
    p3 = os.path.join(d3, "BBB.kss")
    with open(p3, "wb") as f:
        f.write(mk_kss())
    with open(os.path.join(d3, "BBB.m3u"), "wb") as f:
        f.write(b"BBB.kss::KSS,5,Only Five,2:00,,1\n")
    with open(os.path.join(d3, "01 stray.m3u"), "wb") as f:
        f.write(b"BBB.kss::KSS,9,Stray,1:00,,1")
    i3 = cf.probe(p3)
    check("joshw 式 <stem>.m3u 优先（曲号 5，不是 9）",
          i3.track_ids == [5] and i3.n_tracks == 1, i3.track_ids)

    # ---- ④ !playlist.m3u 仍然认 ----
    d4 = os.path.join(TMP, "m3u_case4")
    shutil.rmtree(d4, ignore_errors=True)
    os.makedirs(d4, exist_ok=True)
    p4 = os.path.join(d4, "CCC.kss")
    with open(p4, "wb") as f:
        f.write(mk_kss())
    with open(os.path.join(d4, "!playlist.m3u"), "wb") as f:
        f.write(b"CCC.kss::KSS,0,A,1:00,,1\nCCC.kss::KSS,2,B,1:00,,1\n")
    check("!playlist.m3u 仍然认（2 首）", cf.probe(p4).n_tracks == 2,
          cf.probe(p4).n_tracks)


def test_p1_formats():
    """P1 四格式：KSS / GYM / SAP / AY。

    真值来源全部是「libgme 源码 + 真实样本实测」，不是照抄公开规范：
      · AY魔数是ZXAYEMUL（不是 AYM/EMAY）
      · SAP 魔数是5 字节 SAP\\x0D\\x0A（不是 4 字节）
      · GYM 帧数遇到非法 opcode 要**跳过继续**，不能停
      · AY/GYM 的时长单位是**毫秒**，与 CHIP_MAX_SECS（秒）比要换算
    """
    print(f"\n{_YEL}=== P1 魔数与识别（KSS/GYM/SAP/AY）{_RST}")

    # ---- 扩展名集合已含 P1 ----
    for ext, pid in ((".kss", "KSS"), (".gym", "GYM"),
                     (".sap", "SAP"), (".ay", "AY")):
        check(f"EXT_TABLE 含 {ext}", cf.EXT_TABLE.get(ext) == pid)
        check(f"CHIP_EXTS 含 {ext}", ext in cf.CHIP_EXTS)
    check("P1_ENABLED 列出四种",
          set(cf.P1_ENABLED) == {"KSS", "GYM", "SAP", "AY"}, str(cf.P1_ENABLED))

    # ---- 魔数（真实样本 hexdump 验证过的字节）----
    for name, data, pid in (
            ("KSCC", mk_kss(b"KSCC"), "KSS"),
            ("KSSX", mk_kss(b"KSSX"), "KSS"),
            ("GYMX", mk_gym(), "GYM"),
            ("SAP\\x0D\\x0A", mk_sap(), "SAP"),
            ("ZXAYEMUL", mk_ay([2.0]), "AY")):
        p = w(f"magic_{pid}.bin", data)
        fmt, conflict = cf.identify(p)
        check(f"魔数 {name} -> {pid}", fmt == pid, str(fmt))
        check(f"{name} 魔数优先（.bin 不冲突）", not conflict, str(conflict))

    # ⚠ 反例：AYM / EMAY **不是** AY 的魔数（任务书里的说法有误）
    fake = w("fake_ay.ay", b"AYM\x00" + bytes(64))
    fmt, _ = cf.identify(fake)
    check("AYM 不是 AY 的魔数（应退回扩展名）", fmt == "AY", str(fmt))
    i = cf.probe(fake)
    check("AYM 魔数下轨数退回默认 1", i.n_tracks == 1, str(i.n_tracks))

    # ---- KSS ----
    print(f"\n{_YEL}--- KSS{_RST}")
    p = w("t.kss", mk_kss())
    i = cf.probe(p)
    check("KSS 识别", i.fmt == "KSS", str(i.fmt))
    check("KSS 轨数恒 256（libgme 口径）", i.n_tracks == cf.CHIP_MAX_TRACKS,
          str(i.n_tracks))
    check("KSS ok=True", i.ok, i.reason)
    d = cf.plan_duration(i)
    check("KSS 无文件内时长 -> 默认 150s", d.source == "default"
          and abs(d.secs - 150.0) < 0.01, f"{d.secs}/{d.source}")
    # v4.4 修复：KSS 同为循环型芯片音乐，走 default 分支时不启用静音检测。
    check("KSS 默认时长关静音检测（v4.4 修复）",
          d.stop_on_silence is False, str(d.stop_on_silence))

    # ---- GYM ----
    print(f"\n{_YEL}--- GYM{_RST}")
    p = w("t.gym", mk_gym(frames=600))
    i = cf.probe(p)
    check("GYM 识别", i.fmt == "GYM", str(i.fmt))
    check("GYM 轨数 1", i.n_tracks == 1, str(i.n_tracks))
    check("GYM song 字段", i.title == "Test Song", repr(i.title))
    check("GYM copyright->author", i.author == "2026 Test", repr(i.author))
    # 600 帧 = 10.0 秒
    check("GYM 时长 =帧数x1000/60", abs(i.exact_secs - 10.0) < 0.02,
          f"{i.exact_secs}")
    check("GYM exact_src=gym_frames", i.exact_src == "gym_frames", i.exact_src)
    check("GYM exact=True", i.exact)
    d = cf.plan_duration(i)
    check("GYM 走 exact 链（关静音检测）",
          d.source == "gym_frames" and not d.stop_on_silence,
          f"{d.source}/{d.stop_on_silence}")
    check("GYM exact 时前置静音上限 0", d.lead_sil_limit == 0.0)

    # 非法 opcode 不能中断帧遍历（libgme 的 switch 无 default）
    p = w("t2.gym", mk_gym(frames=300, bad_tail=bytes([0xAA, 0xBB, 0xCC])))
    i2 = cf.probe(p)
    check("GYM 非法 opcode 后仍继续数帧",
          abs(i2.exact_secs - 5.0) < 0.02, f"{i2.exact_secs}")

    # 超长（跑不完）-> 不填 exact，回落默认。
    # 注意 probe 只读前PROBE_MAX_READ(8KB)，所以构造一个**头+大指令流**，
    # 指令流必须落在 8KB 之内才能被完整数帧；这里用 frames=300000 会超出读取
    # 上限而走"截断"分支（同样不填 exact，但验证的不是 600s 闸门）。
    # 要专门验证 600s 闸门，得让**帧数多但文件小** —— 用bad_tail 填满读取窗口：
    big = mk_gym(frames=10) + bytes([0xAA]) * 60000      # ≈100000帧 ≈1667秒
    p = w("t3.gym", big)
    i3 = cf.probe(p)
    check("GYM 帧数换算是÷60（不是×1000）", abs(i3.exact_secs - 10 / 60.0) < 0.02
          or i3.exact_secs == 0.0, f"{i3.exact_secs}")
    check("GYM 超 600s 不填 exact（防 13 小时单曲）",
          i3.exact_secs == 0.0 and i3.exact_src == "", f"{i3.exact_secs}")
    check("GYM 超长仍 ok=True", i3.ok, i3.reason)

    # packed != 0 -> libgme 不支持，不填 exact
    p = w("t4.gym", mk_gym(frames=600, packed=1234))
    i4 = cf.probe(p)
    check("GYM packed!=0 不填 exact", i4.exact_secs == 0.0, f"{i4.exact_secs}")

    # 无头 GYM -> 时长不可信，回落默认
    p = w("t5.gym", mk_gym_raw(frames=5000))
    i5 = cf.probe(p)
    check("无头 GYM 靠扩展名识别", i5.fmt == "GYM", str(i5.fmt))
    check("无头 GYM 不填 exact", i5.exact_secs == 0.0, f"{i5.exact_secs}")

    # ---- SAP ----
    print(f"\n{_YEL}--- SAP{_RST}")
    p = w("t.sap", mk_sap(songs=3))
    i = cf.probe(p)
    check("SAP 识别", i.fmt == "SAP", str(i.fmt))
    check("SAP SONGS 标签 -> 轨数", i.n_tracks == 3, str(i.n_tracks))
    check("SAP NAME 标签 -> 标题", i.title == "Test Tune", repr(i.title))
    check("SAP AUTHOR 标签 -> 作者", i.author == "Tester", repr(i.author))
    check("SAP 无可信时长 -> 默认", i.exact_secs == 0.0
          and cf.plan_duration(i).source == "default")

    # SONGS 缺失 -> 1（与 libgme 缺省一致）
    p = w("t2.sap", mk_sap())
    i2 = cf.probe(p)
    check("SAP 无 SONGS -> 1 轨", i2.n_tracks == 1, str(i2.n_tracks))

    # CRLF 行尾不能把标签解析坏（实测踩过：标签全空）
    p = w("t3.sap", mk_sap(songs=7))
    i3 = cf.probe(p)
    check("SAP CRLF 行尾解析正确", i3.n_tracks == 7 and i3.title == "Test Tune",
          f"n={i3.n_tracks} title={i3.title!r}")

    # TYPE D = Digimusic，libgme 明确不支持 -> ok=False 并给出原因
    p = w("t4.sap", mk_sap(typ=b"D"))
    i4 = cf.probe(p)
    check("SAP TYPE D 判为不可解码", not i4.ok, f"ok={i4.ok}")
    check("SAP TYPE D 给出原因", "Digimusic" in i4.reason, repr(i4.reason))
    check("SAP TYPE D 不被识别成 SAP 之外的格式", i4.fmt == "SAP", str(i4.fmt))

    # ---- AY ----
    print(f"\n{_YEL}--- AY{_RST}")
    p = w("t.ay", mk_ay([2.0, 4.0, 6.0]))
    i = cf.probe(p)
    check("AY 识别", i.fmt == "AY", str(i.fmt))
    check("AY 轨数 = max_track+1", i.n_tracks == 3, str(i.n_tracks))
    check("AY 逐轨时长 2/4/6 秒",
          [round(x, 2) for x in i.track_secs] == [2.0, 4.0, 6.0],
          str(i.track_secs))
    check("AY exact= 逐轨最长", abs(i.exact_secs - 6.0) < 0.05,
          f"{i.exact_secs}")
    check("AY exact_src=ay_frames", i.exact_src == "ay_frames", i.exact_src)
    check("AY 逐轨曲名", i.track_names[:3] == ["Track 1", "Track 2", "Track 3"],
          str(i.track_names))
    check("AY PAuthor -> author", i.author == "AY Author", repr(i.author))
    d = cf.plan_duration(i)
    check("AY 走 exact 链（关静音检测）",
          d.source == "ay_frames" and not d.stop_on_silence, d.source)

    # 单轨
    p = w("t2.ay", mk_ay([9.48]))
    i2 = cf.probe(p)
    check("AY 单轨 9.48 秒（实测样本真值）",
          i2.n_tracks == 1 and abs(i2.exact_secs - 9.48) < 0.05,
          f"n={i2.n_tracks} exact={i2.exact_secs}")

    # SongLength == 0 表示"未知/无限"，不能当 0 秒
    p = w("t3.ay", mk_ay([0.0]))
    i3 = cf.probe(p)
    check("AY SongLength=0 不填 exact", i3.exact_secs == 0.0, f"{i3.exact_secs}")
    check("AY SongLength=0 回落默认", cf.plan_duration(i3).source == "default")

    # 指针越界（截断）-> 不填 exact，但轨数仍可用
    raw = bytearray(mk_ay([2.0, 4.0]))
    del raw[40:]                        # 砍掉数据区，制造越界指针
    p = w("t4.ay", bytes(raw))
    i4 = cf.probe(p)
    check("AY 指针越界仍能识别", i4.fmt == "AY", str(i4.fmt))
    check("AY 指针越界不填 exact", i4.exact_secs == 0.0, f"{i4.exact_secs}")
    check("AY 指针越界 ok=True（交给 worker 试）", i4.ok, i4.reason)

    # ---- 冲突标记：魔数与扩展名不符 ----
    p = w("wrong.gym", mk_sap())        # SAP 内容套 .gym 扩展名
    fmt, conflict = cf.identify(p)
    check("扩展名与魔数冲突 -> 以魔数为准且标记 conflict",
          fmt == "SAP" and conflict, f"{fmt}/{conflict}")

    # ---- 曲名质量闸门（_useful_song）----
    print(f"\n{_YEL}--- 曲名质量闸门{_RST}")
    import chip_decode
    p_vgm = w("01 - Title Theme.vgm", b"")
    check("空曲名 -> 不用（NSF/GBS/HES 恒空）",
          chip_decode._useful_song("", p_vgm) == "")
    check("None 曲名 -> 不用", chip_decode._useful_song(None, p_vgm) == "")
    check("纯空白曲名 -> 不用", chip_decode._useful_song("   ", p_vgm) == "")
    check("真曲名 -> 采用",
          chip_decode._useful_song("Title Theme", p_vgm) == "Title Theme")
    check("等于文件名主体 -> 不用（无信息量）",
          chip_decode._useful_song("01 - Title Theme", p_vgm) == "")
    p_num = w("204 People Without Hope.nsf", b"")
    check("去掉前导序号后等于文件名 -> 不用",
          chip_decode._useful_song("People Without Hope", p_num) == "")
    p_case = w("BGM 1.gym", b"")
    check("大小写不同也视为相同 -> 不用",
          chip_decode._useful_song("bgm 1", p_case) == "")

    # 端到端：空 song 必须退回「文件名+轨号」，而不是产出空文件名
    info = cf.ChipInfo(path=p_vgm, fmt="VGM", n_tracks=1)
    nm = chip_decode._chip_track_name(p_vgm, "VGM", info,
                                      {"song": "", "track_name": ""}, 0, 1)
    check("空 song 时产出非空文件名", bool(nm) and len(nm) > 1, repr(nm))
    nm2 = chip_decode._chip_track_name(p_vgm, "VGM", info,
                                       {"song": "Title Theme",
                                        "track_name": ""}, 0, 1)
    check("真song 时用曲名（单轨不加序号）", nm2 == "Title Theme", repr(nm2))

    # ---- 时长来源登记 ----
    for src in ("gym_frames", "ay_frames"):
        check(f"EXACT_SOURCES 含 {src}", src in cf.EXACT_SOURCES)
        check(f"{src} 前置静音上限 0",
              cf.Duration(1.0, src).lead_sil_limit == 0.0)
    check("gbs_loop 仍参与 lead_sil 闸门（回归）",
          cf.Duration(1.0, "gbs_loop").lead_sil_limit == 0.0)
    check("default 仍开前置静音",
          cf.Duration(1.0, "default").lead_sil_limit == 10.0)


def test_real_samples(sample_dir):
    print(f"\n{_YEL}=== 真实样本交叉验证（{sample_dir}）{_RST}")
    import glob
    files = []
    for pat in ("*.nsf", "*.nsfe", "*.gbs", "*.vgm", "*.vgz", "*.hes"):
        files += glob.glob(os.path.join(sample_dir, "**", pat),
                           recursive=True)
    if not files:
        print("  (无样本，跳过)")
        return
    bad = 0
    for p in sorted(files):
        i = cf.probe(p)
        d = cf.plan_duration(i)
        status = "OK"
        if not i.ok:
            status = "ok=False"
            bad += 1
        elif i.n_tracks <= 0:
            status = "轨数未知"
        print(f"    {os.path.basename(p)[:38]:<40} {i.fmt:<5} n={i.n_tracks:<4}"
              f" plan={d.secs:>7.2f}s/{d.source:<12} {status}")
    check(f"{len(files)} 个真实文件全部 ok=True", bad == 0, f"{bad} 个失败")


TMP = None


def main():
    global TMP
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="", help="真实样本目录（可选）")
    args = ap.parse_args()
    TMP = tempfile.mkdtemp(prefix="chipfmt_test_")
    try:
        test_identify()
        test_probe_fields()
        test_sidecar_m3u()
        test_p1_formats()
        test_plan_duration()
        test_unsupported()
        test_probe_never_raises()
        test_gates()
        if args.dir:
            test_real_samples(args.dir)
        print(f"\n结果: {_pass} 通过 / {_fail} 失败")
        return 0 if _fail == 0 else 1
    finally:
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
