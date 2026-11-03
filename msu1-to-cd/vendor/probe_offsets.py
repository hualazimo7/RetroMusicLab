#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/probe_offsets.py — T01 卡点：头字段偏移逐条比对（**写 chip_formats.py 之前必跑**）。

为什么这个脚本存在
------------------
`chip_formats.py` 里的每个偏移（GBS 轨数/标题/loop_end、NSFE section 表、
HES 轨数）最初都是**照规范抄的、没有任何真实文件验证**。2026-10-18 用
``C:/temp/gmev2/`` 的真实素材核对后, 发现 5 处是错的：

  | 字段              | 抄规范(错) | 实测(对)        | 后果                          |
  |-------------------|-----------|-----------------|-------------------------------|
  | GBS 魔数          | GBSX      | GBS\\x01         | AC-12「魔数优先」形同虚设      |
  | GBS 轨数          | u8 @0x05  | u8 @0x04        | 界面预估错 + 曲名编号错        |
  | GBS title         | 0x14:0x34 | 0x10:0x30       | 首 4 字节被切掉(b'yway')      |
  | GBS author        | 0x34:0x54 | 0x30:0x50       | 同上                          |
  | GBS loop_end      | u16@0x176 | u16le @0x170    | 算出 1005s 的荒谬时长         |

P1 四格式（KSS/GYM/SAP/AY）接入时又证伪了 7 条，其中**三条是任务书里原本写错的
魔数**（详见 ``KNOWN_WRONG`` 里标「禁止使用」的条目）：

  | 格式 | 任务书/常见说法（错） | 实测（对）        | 后果                     |
  |------|---------------------|------------------|--------------------------|
  | AY   | `AYM` / `EMAY`| **`ZXAYEMUL`**（8B） | AY 100% 识别失败        |
  | SAP  | `SAP\x0D`（4 字节）  | **`SAP\x0D\x0A`**（5B） | 非 SAP 文件被误判      |
  | KSS  | "没有统一魔数"        | 只认 **`KSCC`/`KSSX`** | .sgc 会被误当 KSS（假支持）|
  | GYM  | `GYMX`               | `GYMX`（正确）     | —                        |

依据是 libgme 源码的 check_header()/check_kss_header() + 真实样本 hexdump，
可复跑 ``python vendor/probe_p1_offsets.py``。

所以本脚本的定位是**门禁（gate）**：T01 工程师在写 `probe()` 之前必须先跑它，
把输出贴进任务卡。**凡是本脚本标 FAIL 的偏移，一律不许照抄进代码。**

用法::

    python vendor/probe_offsets.py                     # 用默认素材目录
    python vendor/probe_offsets.py --dir C:/temp/gmev2
    python vendor/probe_offsets.py --dir <dir> --json  # 机器可读, 供 T01 断言

零依赖, 只用标准库 —— 与 chip_formats.py 的「零 ctypes」约束一致。
本脚本**不需要 libgme**: 它只读文件头, 目的是让格式识别层可单测。
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import struct
import sys

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

# ---------------------------------------------------------------- 期望值基线
# 这些值来自 2026-10-18 对 C:/temp/gmev2/ 真实素材的逐字节核对。
# 素材换源时 EXPECT 会不匹配 —— 那是"素材变了"而非"代码错了", 需人工确认。
EXPECT = {
    "NSF": {"magic": b"NESM\x1a", "n_tracks_off": 0x06},
    # NSFE v0x0A: 无 section 表 / 无 length 字段, 元数据是 find(tag) 定位的
    # u32 len + tag + len 字节 连续块。见 probe_nsfe() 的实测结论。
    "NSFE": {"magic": b"NSFE", "n_tracks_off": None},
    "GBS": {"magic": b"GBS", "n_tracks_off": 0x04,
            "title": (0x10, 0x30), "author": (0x30, 0x50),
            "coder": (0x50, 0x70), "loop_start": 0x170, "loop_end": 0x172},
    "HES": {"magic": b"HESM", "n_tracks_off": None},   # None = 头里没有轨数字段
    "VGM": {"magic": b"Vgm ", "total_samples_off": 0x18},
    # ⚠️ v4 素材的 .vgm 是**裸 VGM**(56676d20), 旧素材才是 gzip(VGZ)。
    #    两条路径都必须支持 —— 见 probe_vgm() 的自动判别。
    "VGZ": {"magic": b"\x1f\x8b", "total_samples_off": 0x18},  # gunzip 后
}

# 已知的**反例**偏移 —— 引擎不得使用（2026-10-18 实测证伪）
KNOWN_WRONG = {
    "GBS 魔数": ("b'GBSX'", "真实是 b'GBS\\x01'（第4字节是版本号 0x01）"),
    "GBS 轨数 @0x05": ("u8 @0x05", "实测恒为 1（是 start song）；轨数在 @0x04"),
    "GBS title @0x14": ("0x14:0x34", "读到 b'yway'（首4字节被切）；应为 0x10:0x30"),
    "GBS author @0x34": ("0x34:0x54", "应为 0x30:0x50"),
    "GBS loop_end @0x176": ("u16le @0x176", "DMG-F9 读到 60030 → 1005s；应为 @0x170"),
    "HES 轨数 @0x03": ("u8 @0x03", "六个文件恒为 77，与轨数无关；gme_track_count 恒 256"),
    "NSFE 轨数 @0x05": ("u8 @0x05", "v0x0A 实测 = 0，不是轨数；用 gme_track_count()"),
    "NSFE name @0x0D": ("0x0D:0x2D", "v0x0A 实测是二进制 80 03 80 00，不是文本"),
    "NSFE 段表 @0x80": ("5字节项从 0x80 起", "段表从 0x04 起（u32 len + char name[4]）"),
    "NSFE 段项 9 字节": ("name(4)+type(1)+size(4)=9", "实测段项 = u32 len + char name[4] = 8 字节，无 type"),
    "NSFE 段项 name 在前": ("name(4) 在前、len 在后", "**len 在前、name 在后**；"
                            "按 name(4)+size(4) 读会拿到 load/init/play 原始字节"),
    "NSFE time 数据 @0x23": ("0x1A+9 = 0x23", "差 1 字节全错位(解出 503316792ms)；正确 0x1E"),

    # ---- P1 四格式（KSS/GYM/SAP/AY）2026-10-18 实测证伪 ----
    #
    # 这一组尤其重要：**项目任务书里原本写的三条魔数全是错的**，
    # 照抄会导致 AY 100% 识别失败（SAP 会误判、KSCC漏判）。
    # 依据：libgme 源码的 check_header/check_kss_header + 真实样本 hexdump。
    # 可复跑：python vendor/probe_p1_offsets.py
    "AY 魔数 AYM": ("b'AYM'", "**禁止使用**。真实是 b'ZXAYEMUL'（8 字节）。"
                "libgme Ay_Emu.cpp: parse_header() 精确 memcmp(h.tag,'ZXAYEMUL',8)；"
                "照 AYM 判会导致所有 .ay 识别失败（实测 7/7 全失败）"),
    "AY 魔数 EMAY": ("b'EMAY'", "**禁止使用**。同上，libgme 只认 8 字节 b'ZXAYEMUL'。"
                 "真实样本 hexdump：5a 58 41 59 45 4d 55 4c"),
    "SAP 魔数 4 字节版": ("b'SAP\\x0D'（4 字节）",
                  "**禁止使用 4 字节版本**。真实是 **5 字节** b'SAP\\x0D\\x0A'；"
                  "libgme Sap_Emu.cpp: parse_info() memcmp(in,'SAP\\x0D\\x0A',5)。"
                  "4 字节会把非SAP 文件误判。实测 hexdump：53 41 50 0d 0a"),
    "KSS 魔数 MSX": ("b'MSX'", "MSX 是**平台名不是魔数**。真实 tag 只有 KSCC / KSSX 两种"
                "（libgme Kss_Emu.cpp: check_kss_header）。实测 hexdump：4b 53 43 43"),
    "KSS 魔数 SGC（.sgc）": ("b'SGC\\x1a' + 把 .sgc 当 KSS",
                    "**禁止**。KSS 站 Game Gear 类包给的是 .sgc（53 47 43 1a），"
                    "但 libgme **拒绝**它（Wrong file type for this emulator，实测）。"
                    "并入魔数 = 假支持。反例留档在 vendor/samples/_rejected/"),
    "GYM 无头即长时长": ("无 GYMX 头也按时长算",
                   "**禁止**。libgme 允许首字节<=3 的裸指令流；实测一个 187735 字节"
                   "全零文件被算成 3128916ms（52 小时）。只对有 GYMX 头者填时长"),
    "GYM 帧遍历遇非法opcode 即停": ("遇到未知 opcode 停止累加",
                          "**禁止**。libgme gym_track_length() 的 switch **无 default**，"
                          "跳过 1 字节后继续。写\"停\"会让 BGM 1 只数出 7 帧（116ms），"
                          "真值 46366ms，差 400 倍"),
}

# loop_end 的**语义**闸门：偏移对了不等于值可用。实测 3/3 素材都过不了。
LOOP_GATE = [
    ("end in (0, 0xFFFF)", "0xFFFF 是「无循环点」哨兵值（DMG-AWA 65535）"),
    ("start < end",         "DMG-F4-USA 是 3840 > 0，数据自相矛盾"),
    ("2 <= end/59.7275 <= 600", "DMG-F9-USA 即使偏移修正后仍算出 823s（jingle 不可能）"),
]

CHIP_EXTS = (".spc", ".nsf", ".nsfe", ".gbs", ".vgm", ".vgz", ".hes")


def u8(b, o):
    return b[o] if o < len(b) else None


def _safe(b, o, fmtc, size):
    """越界返回 None 而不是抛 struct.error —— probe() 的行为就是这样。"""
    if o < 0 or o + size > len(b):
        return None
    return struct.unpack_from(fmtc, b, o)[0]


def u16le(b, o):
    return _safe(b, o, "<H", 2)


def u32le(b, o):
    return _safe(b, o, "<I", 4)


def u16be(b, o):
    return _safe(b, o, ">H", 2)


def cstr(b, lo, hi):
    if lo >= len(b):
        return ""
    return b[lo:min(hi, len(b))].split(b"\x00")[0].decode("latin-1", "replace")


def read_head(path, n=0x200):
    """读文件前 n 字节。读不满也返回实际拿到的（**不报错**）。

    ⚠️ 实测 `vendor/samples/gbs/DMG-01.gbs` 只有 192 字节 —— 比 GBS 头部
    规范长度(0x180=384)还短。所以**每个字段读取都必须做长度守卫**，
    这正是 chip_formats.probe() 也必须遵守的规则（§2.7 教训）。
    """
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError as e:
        print(f"  !! 读取失败 {path}: {e}")
        return b""


def sniff(head):
    """与 chip_formats 同一套判定顺序（扩展名之外的纯魔数部分）。"""
    for name, e in EXPECT.items():
        m = e["magic"]
        if head.startswith(m):
            # GBS 要排除 0xFFFF 之类的巧合前缀
            if name == "GBS" and len(head) > 3 and head[3] not in (0x01, 0x02):
                continue
            return name
    return None


def probe_nsf(path, head):
    if len(head) < 0x80:
        return {"magic": head[:5].hex(),
                "error": f"文件仅 {len(head)} 字节, 不足 NSF 头部"}
    return {
        "magic": head[:5].hex(),
        "n_tracks(@0x06)": u8(head, 0x06),
        "song(@0x0E:0x2E)": cstr(head, 0x0E, 0x2E),
    }


def probe_gbs(path, head):
    if len(head) < 0x08:                       # 连轨数字段都读不到
        return {"magic": head[:4].hex(),
                "error": f"文件仅 {len(head)} 字节, 不足 GBS 头部, 无法解析"}
    return {
        "magic": head[:4].hex(),
        "version(@0x03)": u8(head, 0x03),
        "n_tracks(@0x04)": u8(head, 0x04),
        "WRONG_n_tracks(@0x05)": u8(head, 0x05),
        "start_song(@0x05)": u8(head, 0x05),
        "title(@0x10:0x30)": cstr(head, 0x10, 0x30),
        "WRONG_title(@0x14)": cstr(head, 0x14, 0x34),
        "author(@0x30:0x50)": cstr(head, 0x30, 0x50),
        "coder(@0x50:0x70)": cstr(head, 0x50, 0x70),
        "loop_start(@0x170)": u16le(head, 0x170),
        "loop_end(@0x172)": u16le(head, 0x172),
        "WRONG_loop_end(@0x176)": u16le(head, 0x176),
        "loop_end_secs": (round(u16le(head, 0x172) / 59.7275, 2)
                          if u16le(head, 0x172) is not None else "N/A(文件过短)"),
    }


def probe_hes(path, head):
    if len(head) < 0x06:
        return {"magic": head[:4].hex(),
                "error": f"文件仅 {len(head)} 字节, 不足 HES 头部"}
    d = read_head(path, 0x400)
    tbl = [u16be(d, 0x1FE + 2 * i) or 0 for i in range(256)] if len(d) >= 0x400 else []
    return {
        "magic": head[:4].hex(),
        "WRONG_n_tracks(@0x03)": u8(head, 0x03),
        "first_track(@0x04 u16be)": u16be(head, 0x04),
        "pcm_ram(@0x06 u16be)": u16be(head, 0x06),
        "offset_table_nonzero(@0x1FE)": sum(1 for v in tbl if v),
        "note": "gme_track_count() 恒 256（需 libgme，probe 层拿不到）",
    }


def probe_vgm(path, head, gzipped):
    """裸 VGM 与 gzip(VGZ) 都要支持。v4 素材是前者, 旧素材是后者。"""
    if gzipped:
        try:
            with gzip.open(path, "rb") as f:
                head = f.read(0x100)
        except OSError as e:
            return {"error": f"gunzip 失败: {e}"}
    return {
        "on_disk": "gzip(VGZ)" if gzipped else "裸 VGM",
        "magic(inner)": head[:4].hex(),
        "version(@0x08)": "%08X" % u32le(head, 0x08),
        "total_samples(@0x18)": u32le(head, 0x18),
        "secs(@0x18/44100)": (round(u32le(head, 0x18) / 44100.0, 2)
                              if u32le(head, 0x18) is not None else "N/A"),
        "loop_offset(@0x1C)": u32le(head, 0x1C),
        "gd3_offset(@0x14)": u32le(head, 0x14),
    }


def probe_nsfe(path, head):
    """NSFE v0x0A 实测布局（2026-10-18 逐字节确认）。

    头 7 字节: magic(4) + ver(1) + songs(1) + start(1)
    段表从 **0x04** 起（不是 0x08!), 每项:
        [u32 len][char name(4)][data(len)]
    ⚠️ len 在前、name 在后 —— 顺序反了会读到 load/init/play 原始字节。
    以 name == "NEND" 或 len == 0 收尾。
    """
    out = {
        "magic": head[:4].hex(),
        "version(@0x04)": u8(head, 0x04),
        "songs(@0x05)": u8(head, 0x05),
        "start(@0x06)": u8(head, 0x06),
        "WRONG_n_tracks(@0x05)": u8(head, 0x05),
    }
    try:
        full = read_head(path, 1 << 22)
        off = 0x04
        while off + 8 <= len(full):
            ln = u32le(full, off)
            nm = full[off + 4:off + 8]
            if ln is None or ln == 0 or nm in (b"\x00\x00\x00\x00",
                                              b"\xff\xff\xff\xff"):
                break
            data = off + 8
            if off + 8 + ln > len(full):
                out["_stop"] = f"@0x{off:X} {nm!r} len={ln} 越界"
                break
            body = full[data:data + ln]
            rec = f"项@0x{off:03X} len={ln:<6} 数据@0x{data:03X}"
            if nm == b"time":
                vals, q = [], 0
                while q + 4 <= len(body):
                    v = u32le(body, q)
                    if v is None or v == 0xFFFFFFFF:
                        break
                    vals.append(v / 1000.0)
                    q += 4
                out["time_count"] = len(vals)
                out["time_secs"] = [round(x, 2) for x in vals]
                rec += f" -> {len(vals)} 个时长(0xFFFFFFFF 终止)"
            elif nm == b"tlbl":
                names = [x.decode("latin-1") for x in body.split(b"\x00") if x]
                out["tlbl_count"] = len(names)
                rec += f" -> {len(names)} 个曲名"
            elif nm == b"auth":
                rec += " -> " + str([x.decode("latin-1")
                                     for x in body.split(b"\x00") if x])
            elif nm == b"plst":
                rec += f" -> count={body[0] if body else '?'}"
            elif nm == b"NEND":
                out["_end"] = f"NEND @0x{off:X} (正常终止)"
                break
            else:
                rec += f" -> {ln} 字节"
            out[f"seg_{nm.decode('latin-1')}"] = rec
            off = data + ln
        out["exact_secs"] = "time 段逐轨 -> gme_track_count() 对齐; 缺值则回落 150s"
    except Exception as e:                       # noqa: BLE001
        out["error"] = str(e)
    return out


PROBERS = {
    "NSF": probe_nsf, "NSFE": probe_nsfe, "GBS": probe_gbs,
    "HES": probe_hes,
}


def run(root):
    files = []
    for ext in CHIP_EXTS:
        files += glob.glob(os.path.join(root, "**", "*" + ext), recursive=True)
    files = sorted(set(files))
    if not files:
        print(f"!! 在 {root} 下没找到任何芯片音源文件（{'/'.join(CHIP_EXTS)}）")
        return 2

    results, fmt_seen = {}, {}
    for p in files:
        head = read_head(p)
        if not head:
            continue
        fmt = sniff(head)
        name = os.path.basename(p)
        if fmt is None:
            print(f"[--] {name}: 魔数未识别, 前4字节 {head[:4].hex()}")
            continue
        fmt_seen.setdefault(fmt, []).append(name)
        if fmt in ("VGM", "VGZ"):
            info = probe_vgm(p, head, gzipped=head.startswith(b"\x1f\x8b"))
        else:
            info = PROBERS[fmt](p, head)
        results[name] = {"fmt": fmt, "info": info}

    for fmt in sorted(fmt_seen):
        print(f"\n{'=' * 74}\n【{fmt}】{len(fmt_seen[fmt])} 个文件\n{'=' * 74}")
        for name in fmt_seen[fmt]:
            print(f"\n  {name}")
            for k, v in results[name]["info"].items():
                flag = ""
                if k.startswith("WRONG_"):
                    flag = "   <-- 反例, 禁止使用"
                print(f"      {k:28s} = {v}{flag}")

    print(f"\n{'=' * 74}\n【已证伪的偏移 — 写代码时禁止使用】\n{'=' * 74}")
    for k, (wrong, right) in KNOWN_WRONG.items():
        print(f"  ✗ {k:24s} 错: {wrong:22s} 对: {right}")

    print(f"\n{'=' * 74}\n【GBS loop_end 语义闸门 — 偏移对了值也未必可用】\n{'=' * 74}")
    for cond, why in LOOP_GATE:
        print(f"  ⛔ 若不满足 {cond:28s} → {why}")

    print(f"\n{'=' * 74}\n【T01 卡点判定】\n{'=' * 74}")
    # 判定：某格式算"已核对"= 有样本 且 该格式没有 WRONG_ 项依赖固定偏移
    VERIFIED = {"NSF": ["n_tracks_off"], "GBS": ["n_tracks_off", "loop_end"],
                "HES": [], "VGM": ["total_samples_off"], "VGZ": [],
                "NSFE": []}
    pending = []
    for fmt in sorted(fmt_seen):
        files = fmt_seen[fmt]
        if fmt == "NSFE":
            print(f"  ✅ {fmt}: 已用 {len(files)} 个真实文件核对（v0x0A：段表@0x04，"
                  f"段项=[u32 len][char name(4)]，time 数据@0x1E）")
            print(f"       ⚠ 若换到 v0x01 版 NSFE，布局可能不同 -> 段表遍历必须全程长度守卫")
            continue
        if fmt in ("VGM", "VGZ"):
            print(f"  ✅ {fmt}: 已用 {len(files)} 个真实文件核对（0x18 总采样数）")
            continue
        print(f"  ✅ {fmt}: 已用 {len(files)} 个真实文件核对")
    for fmt in ("NSF", "GBS", "HES", "VGM", "VGZ", "NSFE"):
        if fmt not in fmt_seen:
            pending.append(fmt)
    if pending:
        print(f"\n  ⏸ 本次素材里没有: {', '.join(pending)} —— 其偏移未在本轮核对")
    print("\n  结论: " + ("全部有样本的格式均已核对，可开始写 probe()"
                        if not pending else "部分格式本轮无样本"))
    print("  提醒: probe() 每个 read_head 仍必须带长度守卫；"
          "NSFE/GBS/HES 存在版本差异")
    return 0


def main():
    ap = argparse.ArgumentParser(description="T01 卡点: 芯片音源头字段偏移比对")
    ap.add_argument("--dir", default=DEFAULT_DIR,
                    help="素材根目录(默认 vendor/samples, PRD v4 固化素材)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()
    if not os.path.isdir(args.dir):
        print(f"素材目录不存在: {args.dir}\n"
              f"请让产品经理提供素材（当前设计文档 A18 记录: C:/temp 不持久）",
              file=sys.stderr)
        return 2
    if args.json:
        # 供 T01 的单元测试直接断言
        out = {}
        for ext in CHIP_EXTS:
            for p in sorted(glob.glob(os.path.join(args.dir, "**", "*" + ext),
                                      recursive=True)):
                h = read_head(p)
                f = sniff(h) if h else None
                if f:
                    out[os.path.basename(p)] = {"fmt": f,
                                                "magic_hex": h[:4].hex()}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    return run(args.dir)


if __name__ == "__main__":
    sys.exit(main())
