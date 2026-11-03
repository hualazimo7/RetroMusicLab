#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/snapshot_tracknames.py — 记录/对比「曲名 + PCM 哈希」基线（AC-14 零回归）。

用途：修`gme_track_info` 调用约定（`_GmeInfo` 字段错位 + `gme_info_t**`）
**必然**会改变曲名来源 ——修好后 `info.song` / `info.track_name` 才第一次真正
拿到值，于是 `_spc_track_name()` 的产物文件名会变。这大概率是**改进**
（不再退化成"文件名+轨号"），但必须用数据说话，不能口头断言"应该没影响"。

所以本脚本做两件事：
  · `--save <dir>`：跑一遍，把「产出文件名 + 每个 WAV 的 PCM SHA256」写JSON
  · `--compare <a> <b>`：对比两次快照，**PCM 必须逐字节一致**；文件名变化
    逐条列出「改前 -> 改后」，交给人判断能否接受。

为什么 PCM 用 SHA256 而不是听感/长度：
  时长策略与曲名是**两条独立路径**（时长走 chip_formats.probe / ID666，
  曲名走 track_info）。若 PCM 哈希变了，说明改动溢出到了解码路径，
  那就是真回归，必须查。哈希一致 = 音频bit-exact 没被碰。

覆盖文件：
  · vendor/sample.spc      —— 老功能主路径（AC-14 零回归）
  · 一个真实 NSF   —— 多轨 + 会走 chip_decode 曲名逻辑
用法：
    python vendor/snapshot_tracknames.py --save before.json
    # ...改代码...
    python vendor/snapshot_tracknames.py --save after.json
    python vendor/snapshot_tracknames.py --compare before.json after.json
"""

import argparse
import glob
import hashlib
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import spc_wav                                     # noqa: E402


def pick_inputs():
    """选要快照的输入文件。返回 [(标签, 路径)]。"""
    out = []
    spc = os.path.join(ROOT, "vendor", "sample.spc")
    if os.path.exists(spc):
        out.append(("sample.spc", spc))
    # 真实 NSF：优先挑一个多轨的（曲名逻辑分支更多）
    cands = sorted(glob.glob(os.path.join(ROOT, "vendor", "samples", "nsf",
                                          "*.nsf")))
    multi = []
    for p in cands:
        try:
            import chip_formats as cf
            if cf.probe(p).n_tracks > 1:
                multi.append(p)
        except Exception:                                # noqa: BLE001
            pass
    pick = (multi or cands)
    if pick:
        out.append((os.path.basename(pick[0]), pick[0]))
    return out


def sha256_pcm(wav_path):
    """只对 **PCM 数据**（跳过 44 字节 WAV 头）取 SHA256。

    跳过文件头：文件名变了头也不变，但万一头里的元数据块有差异会干扰
    "音频是否逐字节一致"这个判断。音频本体才是我们要守的东西。
    """
    import wave
    with wave.open(wav_path, "rb") as h:
        raw = h.readframes(h.getnframes())
    return hashlib.sha256(raw).hexdigest()


def snapshot(secs=20.0):
    """跑一遍全部输入，返回可JSON 序列化的快照。"""
    items = pick_inputs()
    if not items:
        raise SystemExit("找不到任何输入文件（vendor/sample.spc 与 samples/nsf/*.nsf）")
    out_root = tempfile.mkdtemp(prefix="snap_trk_")
    snap = {"inputs": {}, "outputs": {}}
    try:
        for label, path in items:
            ext = os.path.splitext(path)[1].lower().lstrip(".")
            mode = "chip" if ext in ("nsf", "nsfe", "gbs", "vgm", "vgz",
                                      "hes", "kss", "gym", "sap", "ay") else "spc"
            out_dir = os.path.join(out_root, f"{ext}_{label}")
            shutil.rmtree(out_dir, ignore_errors=True)
            stat = spc_wav.convert([path], out_dir, mode, chip_secs=secs)
            files = [os.path.basename(f) for f in stat["files"]]
            snap["inputs"][label] = {
                "path": os.path.relpath(path, ROOT).replace("\\", "/"),
                "mode": mode,
                "n_wav": len(files),
                "errors": [str(e) for e in stat.get("errors", [])][:3],
            }
            for f in stat["files"]:
                if not os.path.exists(f):
                    continue
                snap["outputs"][os.path.basename(f)] = {
                    "pcm_sha256": sha256_pcm(f),
                    "bytes": os.path.getsize(f),
                }
            snap["inputs"][label]["files"] = sorted(files)
    finally:
        shutil.rmtree(out_root, ignore_errors=True)
    return snap


def compare(a_path, b_path):
    a = json.load(open(a_path, encoding="utf-8"))
    b = json.load(open(b_path, encoding="utf-8"))
    print("=" * 74)
    print(f"改前: {a_path}")
    print(f"改后: {b_path}")
    print("=" * 74)

    # ---- 1. PCM 必须逐字节一致 ----
    common = sorted(set(a["outputs"]) & set(b["outputs"]))
    same = [n for n in common
            if a["outputs"][n]["pcm_sha256"] == b["outputs"][n]["pcm_sha256"]]
    diff = [n for n in common if n not in same]
    only_a = sorted(set(a["outputs"]) - set(b["outputs"]))
    only_b = sorted(set(b["outputs"]) - set(a["outputs"]))

    print(f"\n[1] PCM 逐字节一致性（共 {len(common)} 个同名文件）")
    print(f"    一致: {len(same)}")
    if diff:
        print(f"    !! 不一致 {len(diff)} 个 —— 这是真回归，必须查：")
        for n in diff:
            print(f"       {n}")
            print(f"         改前 {a['outputs'][n]['pcm_sha256'][:16]} "
                  f"({a['outputs'][n]['bytes']} B)")
            print(f"         改后 {b['outputs'][n]['pcm_sha256'][:16]} "
                  f"({b['outputs'][n]['bytes']} B)")
    if only_a:
        print(f"    仅改前存在: {only_a}")
    if only_b:
        print(f"    仅改后存在: {only_b}")

    pcm_ok = not diff and not only_a and not only_b

    # ---- 2. 文件名变化（预期会变，逐条列出）----
    print(f"\n[2] 产出文件名变化（改前 -> 改后）")
    for label in sorted(set(a["inputs"]) | set(b["inputs"])):
        ia = a["inputs"].get(label, {})
        ib = b["inputs"].get(label, {})
        fa = ia.get("files", [])
        fb = ib.get("files", [])
        if fa == fb:
            print(f"    {label}: 无变化（{len(fa)} 个）")
            for f in fa[:6]:
                print(f"       = {f}")
            continue
        print(f"    {label}: 有变化")
        for x in fa:
            mark = "=" if x in fb else "-"
            print(f"       {mark} 改前: {x}")
        for x in fb:
            mark = "=" if x in fa else "+"
            print(f"       {mark} 改后: {x}")
        # 按位置对照，方便看"同一首曲子换了名字"
        if len(fa) == len(fb):
            print("       —— 逐条对照 ——")
            for x, y in zip(fa, fb):
                print(f"          {x if x == y else x + '  ->  ' + y}")

    print(f"\n[3] 结论")
    print(f"    PCM 逐字节一致: {'是' if pcm_ok else '否 —— 有回归！'}")
    print(f"    文件名变化: {'有（见上，需人工判断是否可接受）' if any(
        a['inputs'][k].get('files') != b['inputs'].get(k, {}).get('files')
        for k in set(a['inputs']) | set(b['inputs'])) else '无'}")
    return 0 if pcm_ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", metavar="JSON", help="跑一遍并保存快照")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"),
                    help="对比两份快照")
    ap.add_argument("--secs", type=float, default=20.0)
    args = ap.parse_args()

    if args.compare:
        return compare(args.compare[0], args.compare[1])
    if args.save:
        snap = snapshot(args.secs)
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2, sort_keys=True)
        total = sum(v["n_wav"] for v in snap["inputs"].values())
        print(f"已保存 {args.save}：{len(snap['inputs'])} 个输入 / "
              f"{total} 个 WAV")
        for label, v in snap["inputs"].items():
            print(f"  {label:44s} {v['mode']:5s} {v['n_wav']:3d} 个 WAV "
                  f"{'ERR' if v['errors'] else ''}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())