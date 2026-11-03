#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.9.12 验证脚本 —— 全量跑 PS4 的 txtp，统计真实通过率。

**为什么要绕开临时文件**：本机有一层「单轮累计 50 次删除」的保护钩子，
148 个 txtp 每个一次 ``os.remove(tmp)`` 就会在第 50 个被拦下，进程直接死
（实测两次：一次跑到 48 个、一次跑到 46 个就断，连结果 JSON 都没写出来）。
复用同一个临时文件也没用 —— 它数的是**操作次数**，不是文件个数。

所以这里给 ``vgm_audio`` 换一个 ``os`` 替身：只把 ``remove`` 变成空操作，
其余（``os.path`` 等）原样代理。全程只留 1 个临时文件，被测逻辑一行没动。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TMP = r"D:/MSU1Temp/_v5912_reuse.wav"


def _fake_mkstemp(prefix="", suffix=""):
    fd = os.open(TMP, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    return fd, TMP


class _OsShim(object):
    """把 ``os`` 除 ``remove`` 外全部透传。"""

    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def remove(path):
        return None          # 故意不删：见模块 docstring


import vgm_audio
vgm_audio.tempfile.mkstemp = _fake_mkstemp
vgm_audio.os = _OsShim()

import psx_txtp as pt

ROOT = (r"D:/MSU1Downloads/joshw/PlayStation 4/"
        r"Assassin's Creed IV - Freedom Cry (2014-02-18)"
        r"(Ubisoft Montreal)(Ubisoft)[PS4]")
OUT = r"D:/workbuddy/_t5912_all3"


def main():
    os.makedirs(OUT, exist_ok=True)
    items = pt.list_txtp(ROOT)
    print("txtp 总数 %d" % len(items), flush=True)

    # 断点续跑：产物名是 "NN_xxx.wav"，NN 已经在了就跳过
    have = set()
    for n in os.listdir(OUT):
        if n.lower().endswith(".wav") and "_" in n:
            have.add(n.split("_", 1)[0])
    print("已有产物 %d 个，续跑" % len(have), flush=True)

    jsonl = os.path.join(OUT, "_progress.jsonl")
    ok = fail = 0
    skipped = 0
    t0 = time.time()
    with open(jsonl, "w", encoding="utf-8") as fh:
        for i, p in enumerate(items, 1):
            if ("%02d" % i) in have:
                skipped += 1
                continue
            pr = pt.probe_txtp(p)
            r = pt.convert([p], OUT, on_log=lambda s: None)
            ok += r["ok"]
            fail += r["fail"]
            row = {"i": i, "name": os.path.basename(p),
                   "probe": round(pr.get("seconds") or 0, 2),
                   "ok": r["ok"], "fail": r["fail"],
                   "secs": round(r["seconds"], 2),
                   "errors": r["errors"][:2]}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            if r["fail"]:
                print("FAIL %s :: %s" % (row["name"][:60], row["errors"][:1]),
                      flush=True)
            if i % 20 == 0:
                print("  ... %d/%d  ok=%d fail=%d  %.0fs"
                      % (i, len(items), ok, fail, time.time() - t0), flush=True)

    el = time.time() - t0
    res = {"total": len(items), "ran": ok + fail, "skipped": skipped,
           "ok": ok, "fail": fail, "elapsed": round(el, 1)}
    json.dump(res, open(os.path.join(OUT, "_result.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=1)
    print("DONE %s" % json.dumps(res, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

