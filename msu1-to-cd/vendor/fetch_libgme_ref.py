#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/fetch_libgme_ref.py — 按需抓取 libgme 源码到 vendor/_ref/（不入库）。

为什么需要它：
  P1 四种格式的头部偏移与魔数，本项目一律以 **libgme 的解析代码** 为准，
  而不是照抄公开规范 —— 公开规范有错的地方实测已遇到两处：
    · AY 的魔数，公开说法多为 "AYM"/"EMAY"，而 libgme 只认 `ZXAYEMUL`（8 字节）
    · SAP 的魔数，公开说法常写 4 字节 `SAP\\x0D`，libgme 实际比 5 字节
      `SAP\\x0D\\x0A`（少一个字节就会把别的文件误判成 SAP）

  所以"字段偏移的依据"必须能被别人复核。这个脚本把依据现场抓下来，
  复核者跑一次就能对照 `vendor/probe_p1_offsets.py` 的结论。

⚠ **不要把 libgme 源码拷进仓库**：它是 LGPL-2.1（而 MAME YM2612 部分是 GPLv2+），
   与本项目分发状态不兼容。脚本默认抓到 vendor/_ref/ 并提示该目录应被忽略。

用法:
    python vendor/fetch_libgme_ref.py            # 抓默认的几个文件
    python vendor/fetch_libgme_ref.py --out /tmp/ref
"""

import argparse
import os
import sys
import time
import urllib.request

BASE = "https://raw.githubusercontent.com/libgme/game-music-emu/master/gme/"

#: (文件名, 用途) —— 只取跟P1 四种 + 结构体布局有关的
WANT = [
    ("gme.h", "C 接口：struct gme_info_t 真实布局 / gme_track_info 签名"),
    ("Gym_Emu.h", "GYM 428 字节头布局 header_t"),
    ("Gym_Emu.cpp", "GYM 帧遍历 gym_track_length() / packed 检查"),
    ("Ay_Emu.h", "AY 0x14 字节头布局 header_t"),
    ("Ay_Emu.cpp", "AY ZXAYEMUL 校验 / 相对指针 get_data() / 逐轨 SongLength"),
    ("Sap_Emu.h", "SAP info_t 布局"),
    ("Sap_Emu.cpp", "SAP SAP\\x0D\\x0A 校验 / SONGS,INIT,PLAYER,TYPE 标签解析"),
    ("Kss_Emu.h", "KSS 头布局"),
    ("Kss_Emu.cpp", "KSS check_kss_header()：只认 KSCC / KSSX"),
]

UA = "MSU1Converter-dev/1.0 (fetch libgme reference source for offset audit)"
DELAY = 0.3


def log(m):
    print(m, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_ref"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    ok = 0
    for name, why in WANT:
        dest = os.path.join(args.out, name)
        try:
            req = urllib.request.Request(BASE + name, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
            log(f"  OK   {name:16s} {len(data):>7,} B   ({why})")
            ok += 1
        except Exception as e:                # noqa: BLE001
            log(f"  FAIL {name:16s} {type(e).__name__}: {str(e)[:60]}")
        time.sleep(DELAY)

    log(f"\n抓到 {ok}/{len(WANT)} 个文件 -> {args.out}")
    log("用途: 对照 vendor/probe_p1_offsets.py 里各格式的偏移与魔数结论。")
    log("⚠ libgme 是 LGPL-2.1（内含 MAME YM2612 部分为 GPLv2+），"
        "**不要把本目录纳入版本库或随产品分发**。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())