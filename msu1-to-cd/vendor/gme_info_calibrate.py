#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/gme_info_calibrate.py — 证明「旧写法错在哪、正确写法长什么样」。

## 这个脚本存在的理由
项目里曾经有一个**静默**bug：`gme_track_info` 被当成 `gme_info_t*` 调用，
而上游签名是 **`gme_info_t**`**（DLL 自己 new 并回填指针）。叠加 `_GmeInfo`
字段顺序也写错（多了不存在的 `track_count`），后果是：

  · 所有格式的 `length`恒为同一个值（与文件内容无关）
  · 所有字符串字段全是 None
  · **不崩、不报错**

这种bug 靠"读代码看看对不对"是抓不住的 —— 必须把两种调用**并排跑一遍**
让差异自己显形。本脚本就是那把尺子。

## 现在为什么还留着
因为 `core._GmeInfo` / `core._gme_track_info` 已修好，本脚本的"正确"一侧
**直接复用 core**，不再自带一份结构体（避免同一个结构体有两份定义 ——
那正是这个 bug 能长期潜伏的原因：一份对一份错，谁在用哪份看不出来）。

"错误"一侧则**故意保留**旧布局+ 旧调用约定，用来说明"如果改回去会怎样"，
并给vendor/test_gme_info.py 的 T1/T2/T3 断言提供对照依据。

跑法：
    python vendor/gme_info_calibrate.py
    python vendor/gme_info_calibrate.py --dir vendor/samples
"""

import argparse
import ctypes
import glob
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import msu1_to_cd as core          # noqa: E402


class OldBuggyGmeInfo(ctypes.Structure):
    """**故意保留的错误布局**，仅用于演示与对照，不要在别处使用。

    两处错：
      1. 多了上游根本不存在的 `track_count`，导致后面全体错位一个 int；
      2. 尾部凭空多出 `track_name` / `left` / `right` 三个字段。
    """

    _fields_ = [
        ("track_count", ctypes.c_int),
        ("length", ctypes.c_int),
        ("intro_length", ctypes.c_int),
        ("loop_length", ctypes.c_int),
        ("play_length", ctypes.c_int),
        ("i4", ctypes.c_int), ("i5", ctypes.c_int), ("i6", ctypes.c_int),
        ("i7", ctypes.c_int), ("i8", ctypes.c_int), ("i9", ctypes.c_int),
        ("i10", ctypes.c_int), ("i11", ctypes.c_int), ("i12", ctypes.c_int),
        ("i13", ctypes.c_int), ("i14", ctypes.c_int), ("i15", ctypes.c_int),
        ("system", ctypes.c_char_p),
        ("game", ctypes.c_char_p),
        ("song", ctypes.c_char_p),
        ("copyright", ctypes.c_char_p),
        ("author", ctypes.c_char_p),
        ("comment", ctypes.c_char_p),
        ("dumper", ctypes.c_char_p),
        ("track_name", ctypes.c_char_p),
        ("left", ctypes.c_char_p),
        ("right", ctypes.c_char_p),
    ]


def _s(v):
    if not v:
        return None
    return v.decode("latin-1", "replace").split("\x00")[0]


def vgm_truth_ms(path):
    """VGM 的**外部权威真值**：头部 u32@0x18 总采样数 / 44100 * 1000 -> ms。

    用它当尺子，而不是靠"看起来对不对"。
    """
    import gzip
    head = b""
    try:
        if path.lower().endswith(".vgz"):
            with gzip.open(path, "rb") as f:
                head = f.read(64)
        else:
            with open(path, "rb") as f:
                head = f.read(64)
    except OSError:
        return None
    if not head.startswith(b"Vgm "):
        return None
    (samples,) = struct.unpack_from("<I", head, 0x18)
    if not samples or samples == 0xFFFFFFFF:
        return None
    return round(samples / 44100.0 * 1000)


def call_old_buggy(gme, emu, ti):
    """旧写法：把 byref(struct) 当 gme_info_t** 传。演示它读出来是什么。"""
    gi = OldBuggyGmeInfo()
    if gme.gme_track_info(emu, ctypes.byref(gi), ti):
        return {"err": "track_info 失败"}
    return {
        "track_count": gi.track_count,          # <- 其实是 DLL 写的结构体指针
        "length": gi.length, "intro": gi.intro_length,
        "system": _s(gi.system), "game": _s(gi.game), "song": _s(gi.song),
        "author": _s(gi.author),
    }


def call_correct(gme, emu, ti):
    """正确写法：直接复用 core 修好的实现（gme_info_t** + gme_free_info）。"""
    return core._gme_track_info(gme, emu, ti)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(ROOT, "vendor", "samples"))
    args = ap.parse_args()

    gme = core._load_gme()

    groups = [
        ("VGM（有外部真值）", sorted(glob.glob(
            os.path.join(args.dir, "smd", "*.vgm*")))[:2]),
        ("NSF", sorted(glob.glob(
            os.path.join(args.dir, "nsf", "*.nsf")))[:2]),
        ("GBS", sorted(glob.glob(
            os.path.join(args.dir, "gbs", "*.gbs")))[:2]),
        ("P1/KSS", sorted(glob.glob(
            os.path.join(args.dir, "kss", "*.kss")))[:1]),
        ("P1/GYM", sorted(glob.glob(
            os.path.join(args.dir, "gym", "*.gym")))[:1]),
        ("P1/SAP", sorted(glob.glob(
            os.path.join(args.dir, "sap", "*.sap")))[:1]),
        ("P1/AY", sorted(glob.glob(
            os.path.join(args.dir, "ay", "*.ay")))[:1]),
    ]

    print("=" * 76)
    print("对照：旧写法（错）vs core._gme_track_info（对）")
    print("=" * 76)

    old_lengths = []
    new_lengths = []
    for label, files in groups:
        if not files:
            print(f"\n### {label}: 无样本")
            continue
        print(f"\n### {label}")
        for path in files:
            if not os.path.exists(path):
                continue
            truth = vgm_truth_ms(path)
            emu = ctypes.c_void_p()
            err = gme.gme_open_file(os.path.abspath(path).encode("utf-8"),
                                    ctypes.byref(emu), 44100)
            if err:
                print(f"  {os.path.basename(path)}: open 失败")
                continue
            try:
                ntrack = gme.gme_track_count(emu)
                print(f"\n  -- {os.path.basename(path)}gme_track_count={ntrack}")
                if truth:
                    print(f"     外部真值(VGM 采样数/44100) = {truth} ms")
                old = call_old_buggy(gme, emu, 0)
                new = call_correct(gme, emu, 0)
                print(f"     [旧·错] {old}")
                print(f"     [新·对] {new}")
                if isinstance(old.get("length"), int):
                    old_lengths.append(old["length"])
                if isinstance(new, dict) and isinstance(new.get("length"), int):
                    new_lengths.append(new["length"])
            finally:
                gme.gme_delete(emu)

    print("\n" + "=" * 76)
    print("结论")
    print("=" * 76)
    print(f"  旧写法 length 取值集合: {sorted(set(old_lengths))}")
    print(f"  正确写法 length 取值集合: {sorted(set(new_lengths))}")
    if len(set(old_lengths)) <= 1:
        print("  → 旧写法的 length 是**常数**（与文件无关），这正是 bug 的指纹。")
    if len(set(new_lengths)) > 1:
        print("  → 正确写法的 length 随文件变化，符合 gme.h 语义。")
    print("\n  回归门禁见 vendor/test_gme_info.py（T1 字符串 / T2 length非常数 /")
    print("  T3 无 track_count 字段 / T4 500 次调用内存不涨）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())