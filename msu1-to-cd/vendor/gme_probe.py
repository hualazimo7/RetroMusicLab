#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/gme_probe.py — 直接问 libgme 一个文件的真实元信息（验证用）。

用途：本项目的验收标准要求「probe() 的轨数 == gme_track_count()」。这个脚本
是**唯一的权威口径来源** —— 它只调 gme_open_file / gme_track_count /
gme_track_info，不做任何识别推断。

⚠ 绝不调用 gme_identify_file / gme_identify_header：本机 libgme.dll 上必段错误
   （项目 AST 门禁测试守着这条）。格式识别一律走 chip_formats 的魔数嗅探。

用法:
    python vendor/gme_probe.py <文件...>
    python vendor/gme_probe.py --secs 1.0 <文件>     # 顺带测首帧峰值
"""

import argparse
import ctypes
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import msu1_to_cd as core          # noqa: E402  复用既有 _load_gme


def track_info(gme, emu, ti):
    """读一轨的 gme_info_t。返回 dict 或 None。

    ⚠ **不再自带结构体**：直接复用 :func:`msu1_to_cd._gme_track_info`。
       之前这里自己抄了一份 `_GmeInfo`（字段顺序还不是上游那样），属于
       "同一个结构体在项目里有两份定义" —— 那正是这个 bug 能长期潜伏的原因：
       一份对一份错，谁在用哪份根本看不出来，两边还会各自漂移。
       现在只有 core 一份定义，改对了就一起对。

       core._gme_track_info 内部已处理：`gme_info_t**` 调用约定 + gme_free_info
       释放 + 在 free 之前把字符串取出来（避免 use-after-free）。
    """
    return core._gme_track_info(gme, emu, ti)


#: 兼容旧名：本模块曾导出 GmeInfo，改为直接引用 core 的唯一定义。
GmeInfo = core._GmeInfo


def probe_one(gme, path, secs=0.0, sr=44100):
    """返回 dict：ok/err/ntrack/(每轨信息)/peak。"""
    emu = ctypes.c_void_p()
    err = gme.gme_open_file(os.path.abspath(path).encode("utf-8"),
                            ctypes.byref(emu), sr)
    if err:
        return {"ok": False, "err": err.decode("latin-1", "replace")}
    out = {"ok": True, "err": "", "ntrack": 0, "tracks": [], "peak": 0}
    try:
        out["ntrack"] = int(gme.gme_track_count(emu))
        for ti in range(out["ntrack"]):
            rec = {"i": ti}
            try:
                d = track_info(gme, emu, ti)
                if d:
                    rec.update(d)
            except Exception as e:                       # noqa: BLE001
                rec["err"] = f"{type(e).__name__}"
            out["tracks"].append(rec)

        if secs > 0:
            out["peak"] = first_peak(gme, emu, sr, secs)
    finally:
        gme.gme_delete(emu)
    return out


def _dec(v):
    if not v:
        return ""
    if isinstance(v, str):
        return v.strip()
    try:
        return bytes(v).decode("utf-8", "replace").strip()
    except (TypeError, AttributeError):
        return str(v)


def first_peak(gme, emu, sr, secs):
    """解 secs 秒测峰值（0 = 静音/失败）。"""
    n = int(sr * max(0.05, min(secs, 5.0)))
    n = max(1, min(n, 65535))
    gme.gme_start_track(emu, 0)
    buf = (ctypes.c_short * (n * 2))()
    if gme.gme_play(emu, n * 2, buf):
        return 0
    raw = ctypes.string_at(ctypes.addressof(buf), n * 2 * 2)
    import array
    a = array.array("h")
    a.frombytes(raw)
    return max(max(a), -min(a)) if a else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--secs", type=float, default=0.0,
                    help=">0 时额外测首轨峰值")
    ap.add_argument("--max-tracks", type=int, default=8,
                    help="每文件最多打印几条轨信息")
    args = ap.parse_args()

    gme = core._load_gme()
    rc = 0
    for p in args.paths:
        name = os.path.basename(p)
        try:
            sz = os.path.getsize(p)
        except OSError:
            sz = -1
        with open(p, "rb") as f:
            head = f.read(32)
        print(f"\n=== {name}  ({sz} B)")
        print(f"    magic: {head[:16].hex(' ')}")
        print(f"    ascii: {''.join(chr(c) if 32 <= c < 127 else '.' for c in head[:16])}")
        r = probe_one(gme, p, args.secs)
        if not r["ok"]:
            print(f"    !! gme_open_file 失败: {r['err']}")
            rc = 1
            continue
        print(f"    gme_track_count = {r['ntrack']}")
        if args.secs > 0:
            print(f"    首轨 {args.secs}s 峰值 = {r['peak']}")
        for rec in r["tracks"][:args.max_tracks]:
            print(f"      [{rec['i']:3d}] name={rec.get('track_name','')!r} "
                  f"song={rec.get('song','')!r} author={rec.get('author','')!r}")
            print(f"            len={rec.get('length',-1)}ms "
                  f"intro={rec.get('intro_length',-1)} "
                  f"loop={rec.get('loop_length',-1)} "
                  f"play={rec.get('play_length',-1)}")
        if len(r["tracks"]) > args.max_tracks:
            print(f"      ... 共 {len(r['tracks'])} 轨")
    return rc


if __name__ == "__main__":
    sys.exit(main())