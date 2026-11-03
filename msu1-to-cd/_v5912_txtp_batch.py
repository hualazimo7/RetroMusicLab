#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.9.12 批量验证 —— **一次**把整个 PS4 包的 txtp 交给 convert()。

和 ``_v5912_txtp_all.py`` 的区别：那个是逐条调用（每条一个 ``used`` 集合），
同名会被互相覆盖，所以产物数会少于曲目数。真实流程（界面按「开始转换」）
是**一次批量调用**，``used`` 全程共享、重名自动加 `` (2)``。
这个脚本走的就是真实流程，用来拿「产物文件数」这个数字。
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
    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def remove(path):
        return None


import vgm_audio
vgm_audio.tempfile.mkstemp = _fake_mkstemp
vgm_audio.os = _OsShim()

import psx_txtp as pt

ROOT = (r"D:/MSU1Downloads/joshw/PlayStation 4/"
        r"Assassin's Creed IV - Freedom Cry (2014-02-18)"
        r"(Ubisoft Montreal)(Ubisoft)[PS4]")
OUT = r"D:/workbuddy/_t5912_batch"


def main():
    os.makedirs(OUT, exist_ok=True)
    items = pt.list_txtp(ROOT)
    print("txtp 总数 %d" % len(items), flush=True)
    t0 = time.time()
    st = pt.convert(items, OUT, on_log=lambda s: None)
    el = time.time() - t0
    wavs = [n for n in os.listdir(OUT) if n.lower().endswith(".wav")]
    res = {"items": len(items), "ok": st["ok"], "fail": st["fail"],
           "files": len(wavs), "seconds": round(st["seconds"], 1),
           "elapsed": round(el, 1),
           "errors": st["errors"][:5]}
    print("BATCH %s" % json.dumps(res, ensure_ascii=False), flush=True)
    json.dump(res, open(os.path.join(OUT, "_result.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
