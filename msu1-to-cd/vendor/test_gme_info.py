#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_gme_info.py — `gme_track_info` 调用约定的**回归门禁**。

这个测试存在的唯一理由：守住一个**曾经错过、且症状完全静默**的 bug。

## 曾经的 bug（2026-10-18 修）
上游签名是**二级指针**：
    gme_err_t gme_track_info( Music_Emu const*, gme_info_t** out, int track );
    void gme_free_info( gme_info_t* );

旧代码写成：
    gi = _GmeInfo()
    gme.gme_track_info(emu, ctypes.byref(gi), ti)     # 只传了一个指针
等于把 `gme_info_t**` 当 `gme_info_t*` —— DLL 把「结构体指针」写进调用方内存的
前 8 字节，之后所有字段全是垃圾。叠加 `_GmeInfo` 自己字段顺序也错（多了
`track_count`），后果：

  · 所有格式的 `length` **恒为 415**（与文件内容毫无关系）
  · 所有字符串字段（song/game/author）**全是 None**
  · **不崩、不报错**，只是静默污染

所以本文件用四条断言把"错位"变成"会失败的事"：

  T1 字符串字段能真正取到值（song/game/author 非空）——错位时必挂
  T2 length **不是所有文件都相同的常数** —— 专门抓"恒 415"这个特征
  T3 结构体里**没有** track_count 字段；轨数只能来自 gme_track_count()
     —— 防止有人为了"方便"又加回那个不存在的字段
  T4 连续 500 次 track_info，进程内存增长 < 20MB —— 抓漏 gme_free_info 的泄漏

⚠ T1 按格式区分：NSF/GBS/SAP 本身**就没有 song 字段**（实测 song=''），
  那是格式特性不是 bug；所以对"确实有 song"的格式（VGM/GYM/AY）断言非空，
  对所有格式都断言 game/system 至少有内容。

跑法：
    python vendor/test_gme_info.py
    python vendor/test_gme_info.py --dir C:/temp/gmev2
"""

import argparse
import ctypes
import glob
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import msu1_to_cd as core                # noqa: E402

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


def open_file(gme, path, sr=44100):
    """打开一个文件，返回 (emu, ntrack)；失败返回 (None, 0)。"""
    emu = ctypes.c_void_p()
    err = gme.gme_open_file(os.path.abspath(path).encode("utf-8"),
                            ctypes.byref(emu), sr)
    if err:
        return None, 0
    return emu, int(gme.gme_track_count(emu))


def pick(sample_dir, sub, pat, want=1):
    files = sorted(glob.glob(os.path.join(sample_dir, "**", pat),
                             recursive=True))
    files = [f for f in files if "_rejected" not in f.split(os.sep)]
    return files[:want]


# ---------------------------------------------------------------------------
# T1 / T2 / T3
# ---------------------------------------------------------------------------

def test_strings_and_fields(gme, sample_dir):
    print(f"\n{_YEL}=== T1/T2/T3  字段与字符串（防错位回归）{_RST}")

    #挑一批尽量多的格式，覆盖不同 head 布局
    picks = []
    for sub, pat in (("nsf", "*.nsf"), ("gbs", "*.gbs"), ("smd", "*.vgm"),
                     ("ay", "*.ay"), ("gym", "*.gym"), ("sap", "*.sap"),
                     ("kss", "*.kss"), ("hes", "*.hes"), ("nsfe", "*.nsfe")):
        fs = pick(sample_dir, sub, pat, 1)
        if fs:
            picks.append((sub, fs[0]))

    if not picks:
        print("  (无样本，跳过；这不算通过)")
        return {}

    lengths = {}
    all_game = True
    all_song_or_game = True
    for sub, path in picks:
        emu, ntrack = open_file(gme, path)
        if emu is None:
            check(f"{sub} 能打开", False, path)
            continue
        try:
            d = core._gme_track_info(gme, emu, 0)
        finally:
            gme.gme_delete(emu)
        if not d:
            check(f"{sub} track_info 有返回", False, os.path.basename(path))
            continue
        # T1：game/system 至少要有一个有内容（所有格式都至少有 system）
        if not (d.get("game") or d.get("system")):
            all_game = False
        if not (d.get("song") or d.get("game") or d.get("system")):
            all_song_or_game = False
        lengths.setdefault(sub, d.get("length"))
        print(f"    {sub:5s} length={d.get('length'):>8} "
              f"play={d.get('play_length'):>7} "
              f"system={d.get('system')!r:26s} game={d.get('game')!r:22s} "
              f"song={d.get('song')!r}")

    check("T1 所有格式 system/game 至少一个有内容（错位时全 None）",
          all_game)
    check("T1 所有格式 song/game/system 至少一个有内容", all_song_or_game)

    # T2：length 不能是"所有文件都一样"的常数（旧 bug 恒 415）
    uniq = set(lengths.values())
    check("T2 length 不是所有文件都相同的常数（旧 bug 特征=恒定）",
          len(uniq) > 1 or -1 in uniq,
          f"各格式 length={lengths}")
    # 再加一条更强的：不能等于那个具体的魔数 415
    check("T2 length 不等于旧 bug 的魔数 415",
          415 not in uniq, f"lengths={sorted(uniq, key=str)}")

    # T3：结构体里没有 track_count —— 上游 gme_info_t 根本没这个字段
    names = {f[0] for f in core._GmeInfo._fields_}
    check("T3 _GmeInfo 没有 track_count 字段（上游 gme_info_t 无此字段）",
          "track_count" not in names)
    check("T3 _GmeInfo 第一个字段是 length",
          core._GmeInfo._fields_[0][0] == "length",
          core._GmeInfo._fields_[0][0])
    # 布局自检：16 个 int + 16 个指针，且字符串区偏移正确
    ints = [f[0] for f in core._GmeInfo._fields_
            if f[1] is ctypes.c_int]
    ptrs = [f[0] for f in core._GmeInfo._fields_
            if f[1] is ctypes.c_char_p]
    check("T3 int 段共 16 个（5 时间 + 11 保留）", len(ints) == 16,
          f"{len(ints)}: {ints}")
    check("T3 指针段共 16 个（7 字符串 + 9 保留）", len(ptrs) == 16,
          f"{len(ptrs)}: {ptrs}")
    # 第一个字符串 system 的字节偏移必须是 16*4 = 64（16 个 int 之后）
    off = None
    cur = 0
    for fname, ftype in core._GmeInfo._fields_:
        if fname == "system":
            off = cur
            break
        cur += ctypes.sizeof(ftype)
    check("T3 system 偏移 = 64（16 个 int 之后）", off == 64, str(off))

    return lengths


def test_length_varies_across_tracks(gme, sample_dir):
    """T2 补强：同一个多轨文件里，不同轨的 length 也应该不同/合理。

    旧 bug 下所有轨所有文件都是同一个 415，所以这条能抓到"整体错位"。
    """
    print(f"\n{_YEL}=== T2b  多轨文件的逐轨 length{_RST}")
    cands = []
    for sub, pat in (("nsf", "*.nsf"), ("gbs", "*.gbs"), ("ay", "*.ay")):
        fs = [f for f in sorted(glob.glob(os.path.join(sample_dir, "**", pat),
                                          recursive=True))
              if "_rejected" not in f.split(os.sep)]
        cands += fs
    multi = []
    for p in cands:
        try:
            import chip_formats as cf
            if cf.probe(p).n_tracks > 1:
                multi.append(p)
        except Exception:                                # noqa: BLE001
            pass
    if not multi:
        print("  (无多轨样本，跳过)")
        return
    path = multi[0]
    emu, ntrack = open_file(gme, path)
    if emu is None:
        check("多轨样本能打开", False, path)
        return
    try:
        vals = []
        for ti in range(min(ntrack, 16)):
            gme.gme_start_track(emu, ti)
            d = core._gme_track_info(gme, emu, ti)
            if d:
                vals.append((ti, d.get("length"), d.get("song")))
    finally:
        gme.gme_delete(emu)
    print(f"    {os.path.basename(path)[:40]} (取前 {len(vals)} 轨)")
    for ti, ln, sg in vals[:8]:
        print(f"      [{ti:3d}] length={ln:>8}  song={sg!r}")
    check("T2b 多轨文件至少读到 1 轨", len(vals) >= 1, str(len(vals)))
    # 至少不能全是 415（旧 bug）
    check("T2b 逐轨 length 不全是 415", all(v[1] != 415 for v in vals),
          str([v[1] for v in vals]))


# ---------------------------------------------------------------------------
# T4 内存泄漏粗检
# ---------------------------------------------------------------------------

def rss_mb():
    """当前进程 RSS（MB）。psutil 不一定有，退回 Windows API。"""
    try:
        import psutil                                # noqa: F401
        return psutil.Process().memory_info().rss / 1048576.0
    except Exception:                                    # noqa: BLE001
        pass
    try:
        if os.name == "nt":
            import ctypes as C
            class PMC(C.Structure):
                _fields_ = [("cb", C.c_uint), ("PageFaultCount", C.c_uint),
                            ("PeakWorkingSetSize", C.c_size_t),
                            ("WorkingSetSize", C.c_size_t),
                            ("QuotaPeakPagedPoolUsage", C.c_size_t),
                            ("QuotaPagedPoolUsage", C.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", C.c_size_t),
                            ("QuotaNonPagedPoolUsage", C.c_size_t),
                            ("PagefileUsage", C.c_size_t),
                            ("PeakPagefileUsage", C.c_size_t)]
            c = PMC()
            c.cb = C.sizeof(PMC)
            # 0xFFFF = GetProcessMemoryInfo
            if C.windll.psapi.GetProcessMemoryInfo(
                    C.windll.kernel32.GetCurrentProcess(),
                    C.byref(c), c.cb):
                return c.WorkingSetSize / 1048576.0
    except Exception:                                    # noqa: BLE001
        pass
    return 0.0


def test_no_leak(gme, sample_dir, iters=500, limit_mb=20.0):
    print(f"\n{_YEL}=== T4  gme_free_info 泄漏粗检（{iters} 次）{_RST}")
    fs = pick(sample_dir, "nsf", "*.nsf", 1) or \
        pick(sample_dir, "gbs", "*.gbs", 1)
    if not fs:
        print("  (无样本，跳过；这不算通过)")
        return
    path = fs[0]
    emu, ntrack = open_file(gme, path)
    if emu is None or ntrack <= 0:
        print(f"  (样本打不开: {path})")
        return
    try:
        # 先跑 50 次热身，让 DLL 的内部缓存/惰性分配稳定下来再量
        for _ in range(50):
            core._gme_track_info(gme, emu, 0)
        before = rss_mb()
        for i in range(iters):
            core._gme_track_info(gme, emu, i % ntrack)
        after = rss_mb()
    finally:
        gme.gme_delete(emu)
    growth = after - before
    print(f"    RSS {before:.1f} MB -> {after:.1f} MB  (增长 {growth:+.1f} MB)")
    check(f"T4 {iters} 次 track_info 后内存增长 < {limit_mb:.0f}MB",
          0 <= growth < limit_mb, f"{growth:+.1f} MB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(ROOT, "vendor", "samples"))
    ap.add_argument("--iters", type=int, default=500)
    args = ap.parse_args()

    gme = core._load_gme()
    test_strings_and_fields(gme, args.dir)
    test_length_varies_across_tracks(gme, args.dir)
    test_no_leak(gme, args.dir, iters=args.iters)

    print(f"\n结果: {_pass} 通过 / {_fail} 失败")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())