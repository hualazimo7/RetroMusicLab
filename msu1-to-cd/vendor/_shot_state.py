#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_shot_state.py — 给截图造一份「下过一部分」的保存目录（v5.9.3）。

为什么需要它：v5.9.3 的核心卖点是「状态列按磁盘事实算」，空目录拍出来的
截图全是空白 —— 正好是这个版本要修的样子，拿它当交付图等于没证明。

这个脚本按**真实机种的真实条目名**造出四种完成度：

    前 6 条   → 压缩包在盘上            → ✓ 已下载
    再 4 条   → 包已删、解压目录在       → 📦 已解压
    再 3 条   → 还转出了 WAV             → 🎵 已转 WAV
    其余      → 什么都不造               → （空白）

落盘名走 ``DownloadState._file_name()``、WAV 子目录名走
``dl_api._wav_folder_name()`` —— **必须用同一套规则**，否则截图上的状态
和真实下载时对不上，那是骗自己。

跑法::

    python vendor/_shot_state.py --key spc
    → 打印 DEST / PREFS 两行，供 MSU1_DL_PREFS 与 shot.py 使用
"""

import os
import shutil
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

import tmpdir                                                    # noqa: E402
tmpdir.init(sweep_old=False)


def arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def mk_wav(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x01" * 22050)


def main():
    key = arg("--key", "spc")
    # 样本目录名。截图会把它显示在「保存目录」里，所以默认给个中性名字，
    # 别让交付图上出现 ``shot_state_104435`` 这种一看就是临时目录的东西。
    tag = arg("--dir", "演示样例")
    base = os.path.join(tmpdir.root(), tag)
    shutil.rmtree(base, ignore_errors=True)
    root = os.path.join(base, "MSU1Downloads")
    os.makedirs(root, exist_ok=True)
    prefs = os.path.join(base, "dl_prefs.json")
    # ⚠ 必须在 import / 创建 DownloadState 之前设，否则读的是真机偏好
    os.environ["MSU1_DL_PREFS"] = prefs

    from web_ui import dl_api

    st = dl_api.DownloadState()
    st.load(key)
    t0 = time.time()
    while not st.entries and time.time() - t0 < 25:
        time.sleep(0.2)
    n = len(st.entries)
    print("机种 %s：%d 条" % (key, n), flush=True)
    if not n:
        print("❌ 没拿到条目，无法造样本", flush=True)
        return 2

    # ⚠ 必须把 dest 拽回临时目录。``load()`` 会按 ``_default_root()`` 算出一个
    #   机种目录 —— 本机就是 ``D:\MSU1Downloads\joshw\SNES _ SFC``，那是**用户
    #   真实的下载目录**。第一版直接往那儿写，往真目录里塞了 16 个假文件
    #   （靠 4096 字节特征才捞回来）。造样本只能造在 tmpdir 里。
    dest = os.path.join(root, "joshw", dl_api._safe_name(st.name))
    os.makedirs(dest, exist_ok=True)
    st.set_dest(dest)
    guard = os.path.abspath(dest).lower()
    assert guard.startswith(os.path.abspath(base).lower()), \
        "样本目录跑出临时目录了，拒绝写入：%s" % dest
    print("样本目录: %s" % dest, flush=True)

    rows = st.entries
    plan = [(0, 6, "zip"), (6, 10, "dir"), (10, 13, "wav")]
    made = {"zip": 0, "dir": 0, "wav": 0}
    for lo, hi, kind in plan:
        for e in rows[lo:hi]:
            arch = dl_api.DownloadState._file_name(e["name"], e["url"])
            stem = os.path.splitext(arch)[0]
            if kind == "zip":
                p = os.path.join(dest, arch)
                with open(p, "wb") as fh:
                    fh.write(b"\0" * 4096)
            elif kind == "dir":
                d = os.path.join(dest, stem)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "track 01.spc"), "wb") as fh:
                    fh.write(b"\0" * 64)
            else:
                # 包留着 + 真的转出 WAV（两样都造，最完整的那种）
                with open(os.path.join(dest, arch), "wb") as fh:
                    fh.write(b"\0" * 4096)
                g = dl_api._wav_folder_name(stem)
                mk_wav(os.path.join(dest, "WAV", g, "01 - Title.wav"))
                mk_wav(os.path.join(dest, "WAV", g, "02 - Stage.wav"))
            made[kind] += 1

    st._refresh_disk()
    snap = st.snapshot()
    got = {}
    for r in snap["rows"][:16]:
        got[r["state"] or "（空白）"] = got.get(r["state"] or "（空白）", 0) + 1
    print("造好：包 %d / 解压目录 %d / WAV %d" % (made["zip"], made["dir"],
                                                  made["wav"]), flush=True)
    print("状态分布（前 16 条）：%s" % got, flush=True)
    print("n_on_disk=%s" % snap["n_on_disk"], flush=True)
    print("DEST=%s" % dest, flush=True)
    print("PREFS=%s" % prefs, flush=True)
    print("KEY=%s" % key, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
