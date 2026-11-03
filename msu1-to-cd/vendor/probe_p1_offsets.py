#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/probe_p1_offsets.py — P1 四格式（KSS/GYM/SAP/AY）头部字段的**自洽性验证**。

为什么要有这个脚本（上一版翻车的原因）：
  偏移量不能靠"读几个字节看起来像"来确认。GBS 那一版把循环点读在标题字符串里，
  把一首 jingle 算成 520 秒；NSFE 反复错了三轮。所以本脚本对每个候选字段
  都要求**整条结构自洽**，且与 libgme 的 gme_track_count / track_info.length 对得上，
  才认为验证通过。

验证项：
  GYM  428B 头 -> 帧流遍历 -> 帧数 -> 时长（帧数 × 50/3 ms），与 libgme length 比对
  AY   0x14B 头 -> num_of_songs(=max_track+1) -> 逐轨 song_length(1/50s)，与 libgme 比对
  SAP  文本头 -> SONGS/INIT/PLAYER/TYPE 标签 -> track_count，与 libgme 比对
  KSS  KSCC/KSSX -> 轨数（libgme 恒 256）、有无时长字段

用法:
    python vendor/probe_p1_offsets.py
    python vendor/probe_p1_offsets.py --dir vendor/samples
"""

import argparse
import glob
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gme_probe                     # noqa: E402

GYM_HEADER_SIZE = 428
AY_HEADER_SIZE = 0x14


def hexdump(b, n=32):
    return b[:n].hex(" ")


def ascii_dump(b, n=48):
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b[:n])


# ---------------------------------------------------------------------------
# GYM
# ---------------------------------------------------------------------------

def gym_frames(data, start):
    """遍历 GYM 指令流，返回 (帧数, 结束 offset, 非法 opcode 或 None)。

    指令（公开规范 + libgme Gym_Emu.cpp:gym_track_length 逐行一致）：
      0x00            空转一帧
      0x01 rr vv      YM port0 写     （3 字节）
      0x02 rr vv      YM port1 写     （3 字节）
      0x03 vv         PSG 写          （2 字节）
    ⚠ 未知 opcode：libgme 的 switch **没有 default 分支**，`*p++` 已经前进 1 字节，
      什么都不做就继续循环。所以这里必须"跳过 1 字节继续"，**不能停**——
      实测头一次写成"遇到未知 opcode 就停"，BGM 1 只数出 7 帧（116ms）就断了，
      与 libgme 报的时长完全不符。
    """
    p = start
    n = len(data)
    frames = 0
    bad = None
    while p < n:
        op = data[p]
        p += 1
        if op == 0:
            frames += 1
        elif op in (1, 2):
            if p + 2 > n:
                bad = op
                break
            p += 2
        elif op == 3:
            if p + 1 > n:
                bad = op
                break
            p += 1
        else:
            # 与 libgme 一致：跳过这 1 字节，继续扫（首次记录下来便于排查）
            if bad is None:
                bad = op
    return frames, p, bad


def probe_gym(path, gme, verbose=True):
    data = open(path, "rb").read()
    info = {"fmt": "GYM"}
    if data[:4] == b"GYMX":
        start = GYM_HEADER_SIZE
        info["has_header"] = True
        if len(data) < GYM_HEADER_SIZE + 1:
            return {"fmt": "GYM", "err": "有 GYMX 头但文件短于 429 字节"}
        # packed 字段必须为 0，否则 libgme 报"Packed GYM file not supported"
        packed = struct.unpack_from("<I", data, 4 + 32 * 5 + 256 + 4)[0]
        loop_start = struct.unpack_from("<I", data, 4 + 32 * 5 + 256)[0]
        info["packed"] = packed
        info["loop_start"] = loop_start
        info["song"] = data[4:4 + 32].split(b"\x00")[0].decode("latin-1")
        info["game"] = data[36:68].split(b"\x00")[0].decode("latin-1")
        info["copyright"] = data[68:100].split(b"\x00")[0].decode("latin-1")
        info["emulator"] = data[100:132].split(b"\x00")[0].decode("latin-1")
        info["dumper"] = data[132:164].split(b"\x00")[0].decode("latin-1")
        info["comment"] = data[164:420].split(b"\x00")[0].decode("latin-1")
    else:
        # 无头 GYM（老 Genecyst 原始格式）：libgme 允许首字节 <= 3
        start = 0
        info["has_header"] = False
        if not data or data[0] > 3:
            return {"fmt": "GYM", "err": f"既无 GYMX 头，首字节 0x{data[0]:02x} > 3"}

    frames, endp, badop = gym_frames(data, start)
    info["frames"] = frames
    info["bad_opcode"] = badop
    info["data_bytes"] = len(data) - start
    info["ms"] = frames * 1000 // 60       # libgme: length = frames * 50 / 3

    r = gme_probe.probe_one(gme, path)
    info["gme_ok"] = r["ok"]
    info["gme_err"] = r.get("err", "")
    if r["ok"]:
        info["gme_ntrack"] = r["ntrack"]
        t0 = r["tracks"][0] if r["tracks"] else {}
        info["gme_len_ms"] = t0.get("length", -1)
        info["gme_intro_ms"] = t0.get("intro_length", -1)
        info["gme_song"] = t0.get("song", "")
        # 自洽判据：libgme 报的 length 必须等于我们算的帧数时长
        info["len_match"] = abs(info["gme_len_ms"] - info["ms"]) <= 20
    return info


# ---------------------------------------------------------------------------
# AY
# ---------------------------------------------------------------------------

def ay_get_be16(data, off):
    if off < 0 or off + 2 > len(data):
        return None
    return struct.unpack_from(">H", data, off)[0]


def ay_rel_ptr(data, pos, size):
    """AY 的指针是**相对当前记录位置的带符号偏移**（大端）。

    返回 (绝对 offset 或 None, 越界原因)。
    """
    rel = ay_get_be16(data, pos)
    if rel is None:
        return None, "read_fail"
    if rel >= 0x8000:
        rel -= 0x10000
    tgt = pos + rel
    if tgt < 0 or tgt + size > len(data):
        return None, f"out_of_range(pos={pos},rel={rel})"
    return tgt, ""


def probe_ay(path, gme):
    data = open(path, "rb").read()
    info = {"fmt": "AY"}
    if data[:8] != b"ZXAYEMUL":
        return {"fmt": "AY", "err": f"tag 不是 ZXAYEMUL: {hexdump(data, 8)}"}
    if len(data) < AY_HEADER_SIZE:
        return {"fmt": "AY", "err": "短于 0x14"}

    info["vers"] = data[8]
    info["player"] = data[9]
    max_track = data[0x10]           # libgme: track_count = max_track + 1
    first_song = data[0x11]
    info["max_track"] = max_track
    info["n_tracks"] = max_track + 1
    info["first_song"] = first_song

    # PAuthor@0x0C / PMisc@0x0E / PSongsStructure@0x12 都是相对指针
    for nm, pos in (("author", 0x0C), ("misc", 0x0E)):
        tgt, why = ay_rel_ptr(data, pos, 1)
        info[nm] = ""
        if tgt is not None:
            end = data.find(b"\x00", tgt)
            if end > tgt:
                info[nm] = data[tgt:end].decode("latin-1")
        else:
            info[nm + "_err"] = why

    songs_off, why = ay_rel_ptr(data, 0x12, (max_track + 1) * 4)
    info["songs_ptr_err"] = why
    lengths, names = [], []
    if songs_off is not None:
        for i in range(max_track + 1):
            rec = songs_off + i * 4
            nm_off, e1 = ay_rel_ptr(data, rec, 1)
            dt_off, e2 = ay_rel_ptr(data, rec + 2, 6)
            nm = ""
            if nm_off is not None:
                end = data.find(b"\x00", nm_off)
                if end > nm_off:
                    nm = data[nm_off:end].decode("latin-1")
            names.append(nm)
            # song data +4 = SongLength (1/50 s)
            if dt_off is not None:
                ln = ay_get_be16(data, dt_off + 4)
                lengths.append(None if ln is None else ln * 20)
            else:
                lengths.append(None)
    info["track_names"] = names
    info["track_ms"] = lengths

    r = gme_probe.probe_one(gme, path)
    info["gme_ok"] = r["ok"]
    info["gme_err"] = r.get("err", "")
    if r["ok"]:
        info["gme_ntrack"] = r["ntrack"]
        info["ntrack_match"] = (r["ntrack"] == info["n_tracks"])
        info["gme_len_ms"] = [t.get("length", -1) for t in r["tracks"]]
        # 自洽判据：逐轨 SongLength(1/50s -> ms) 必须与 libgme 的 length 一致
        if lengths and all(x is not None for x in lengths):
            pairs = [(mine, theirs) for mine, theirs in zip(lengths, info["gme_len_ms"])
                     if mine > 0 and theirs > 0]
            info["len_match"] = bool(pairs) and all(
                abs(a - b) <= 20 for a, b in pairs)
            info["len_pairs"] = pairs[:6]
    return info


# ---------------------------------------------------------------------------
# SAP
# ---------------------------------------------------------------------------

def probe_sap(path, gme):
    data = open(path, "rb").read()
    info = {"fmt": "SAP"}
    if data[:5] != b"SAP\x0d\x0a":
        return {"fmt": "SAP", "err": f"magic 不是 SAP\\x0D\\x0A: {hexdump(data, 5)}"}

    # 文本头逐行解析，遇到 0xFF 0xFF 结束
    # ⚠ SAP 的行尾是 **CR LF**（0x0D 0x0A）。libgme 只按 0x0D 找行尾，所以它读到
    #   的下一行开头会带一个 0x0A。这里同样按 0x0D 切行，但**必须把行首的 0x0A
    #   去掉**，否则标签会变成 "\nNAME" 而匹配不上（实测踩过：所有标签解析成空）。
    pos = 5
    end = len(data) - 5
    tags = {}
    order = []
    while pos < end and not (data[pos] == 0xFF and data[pos + 1:pos + 2] == b"\xFF"):
        line_end = data.find(b"\x0d", pos, end)
        if line_end < 0:
            line_end = end
        raw = data[pos:line_end]
        # 去掉紧跟行尾的 LF
        if raw[:1] == b"\x0a":
            raw = raw[1:]
        line = raw.decode("latin-1")
        order.append(line)
        if " " in line:
            k, v = line.split(" ", 1)
            tags[k.strip().upper()] = v.strip()
        pos = line_end + 1
    info["tags"] = tags
    # 只留可打印的文本行用于展示；二进制部分（0xFF 0xFF 之后）不打印，
    # 否则 dump 出来是几 KB 乱码，把真正的结论淹掉（实测踩过）。
    info["header_lines"] = [ln for ln in order
                            if ln and all(32 <= ord(c) < 127 for c in ln)][:16]
    info["binary_offset"] = pos

    def _hex(tagname):
        v = tags.get(tagname, "")
        v = v.split(";")[0].strip()
        try:
            return int(v, 16)
        except ValueError:
            return None

    info["init"] = _hex("INIT")
    info["player"] = _hex("PLAYER")
    info["music"] = _hex("MUSIC")
    stype = tags.get("TYPE", "").strip()
    info["type"] = stype
    songs = tags.get("SONGS", "").strip()
    try:
        info["n_tracks"] = int(songs)
    except ValueError:
        info["n_tracks"] = 1

    r = gme_probe.probe_one(gme, path)
    info["gme_ok"] = r["ok"]
    info["gme_err"] = r.get("err", "")
    if r["ok"]:
        info["gme_ntrack"] = r["ntrack"]
        info["ntrack_match"] = (r["ntrack"] == info["n_tracks"])
        info["gme_len_ms"] = r["tracks"][0].get("length", -1) if r["tracks"] else -1
    return info


# ---------------------------------------------------------------------------
# KSS
# ---------------------------------------------------------------------------

def probe_kss(path, gme):
    data = open(path, "rb").read()
    info = {"fmt": "KSS"}
    tag = data[:4]
    info["tag"] = tag.decode("latin-1")
    if tag not in (b"KSCC", b"KSSX"):
        info["kss_accepted_by_libgme"] = False
        info["note"] = (f"libgme Kss_Emu::check_kss_header 只认 KSCC/KSSX，"
                        f"此文件 {tag!r} 会被判 Wrong file type")
    else:
        info["kss_accepted_by_libgme"] = True

    r = gme_probe.probe_one(gme, path)
    info["gme_ok"] = r["ok"]
    info["gme_err"] = r.get("err", "")
    if r["ok"]:
        info["gme_ntrack"] = r["ntrack"]
        info["gme_len_ms"] = r["tracks"][0].get("length", -1) if r["tracks"] else -1
    return info


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def dump(v, indent="      "):
    if isinstance(v, dict):
        for k, val in v.items():
            if isinstance(val, (dict, list)):
                print(f"{indent}{k}:")
                dump(val, indent + "  ")
            else:
                print(f"{indent}{k} = {val!r}")
    elif isinstance(v, list):
        for i, val in enumerate(v[:12]):
            print(f"{indent}[{i}] {val!r}")
        if len(v) > 12:
            print(f"{indent}... 共 {len(v)} 项")
    else:
        print(f"{indent}{v!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(ROOT, "vendor", "samples"))
    ap.add_argument("--full", action="store_true", help="打印全部字段")
    args = ap.parse_args()

    import msu1_to_cd as core
    gme = core._load_gme()

    pats = [("kss", "*.kss", probe_kss), ("gym", "*.gym", probe_gym),
            ("gym", "*.GYM", probe_gym), ("sap", "*.sap", probe_sap),
            ("ay", "*.ay", probe_ay)]
    for sub, pat, fn in pats:
        files = sorted(glob.glob(os.path.join(args.dir, sub, pat)))
        print(f"\n{'=' * 70}\n[{sub.upper()}] {len(files)} 个样本")
        for f in files:
            head = open(f, "rb").read(48)
            print(f"\n  -- {os.path.basename(f)}  ({os.path.getsize(f)} B)")
            print(f"     hex  : {hexdump(head)}")
            print(f"     ascii: {ascii_dump(head)}")
            try:
                info = fn(f, gme)
            except Exception as e:                       # noqa: BLE001
                print(f"     !! 解析异常 {type(e).__name__}: {e}")
                continue
            if args.full or "err" in info:
                dump(info)
            else:
                dump(info)


if __name__ == "__main__":
    sys.exit(main())