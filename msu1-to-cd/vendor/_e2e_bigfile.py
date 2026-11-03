#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4.27 真实大文件复测：把当时提交超时的 92.6MB 那首重传一遍。

不碰 GUI，走的就是 CloudUploader 用的同一套核心接口：
cloud_md5s() → upload_flac(known_md5=…) → cloud_has() 核对。
"""
from __future__ import annotations

import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import netease_cloud as ncm


def main() -> int:
    pat = os.path.join(os.path.expanduser("~"), "Music", "JoshW", "NES-FC", "WAV",
                       "*1943*合并*.wav")
    hits = glob.glob(pat)
    if not hits:
        print("找不到文件")
        return 2
    wav = hits[0]
    print("源：%s（%.1f MB）" % (os.path.basename(wav),
                              os.path.getsize(wav) / 1e6))
    sess = ncm.NeteaseSession(timeout=20.0)
    sess.loads(ncm.load_cookie())
    sess.refresh_profile()
    print("账号：%s" % sess.user_name)

    t0 = time.time()
    ok, flac, detail = ncm.wav_to_flac(wav)
    if not ok:
        print("转 FLAC 失败：%s" % detail)
        return 2
    md5 = ncm.md5_of(flac)
    print("FLAC %.1f MB | md5=%s | 转码 %.1fs"
          % (os.path.getsize(flac) / 1e6, md5, time.time() - t0))

    known = sess.cloud_md5s()
    print("云盘现有 %d 首" % len(known))
    steps = []
    last = [0.0]

    def _prog(sent, tot):
        if sent - last[0] > 20 * 1024 * 1024:
            last[0] = sent
            print("   已传 %.0f%%" % (100.0 * sent / tot))

    t1 = time.time()
    try:
        r = sess.upload_flac(flac, progress=_prog,
                             on_step=lambda m: (steps.append(m),
                                                print("   · " + m)),
                             known_md5=known)
    except ncm.NeteaseError as exc:
        print("❌ 失败 code=%s：%s" % (exc.code, exc))
        return 1
    print("结果：%s（%.1fs）" % (r, time.time() - t1))
    print("核对 cloud_has =", sess.cloud_has(md5))
    os.remove(flac)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
