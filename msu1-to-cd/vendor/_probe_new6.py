# -*- coding: utf-8 -*-
"""六个新机种的格式探针 —— 第一步：查有没有现成方案。

结论要回答三个问题：
  ① 每个文件到底是什么格式（靠头部魔数，不靠扩展名）
  ② PyAV（FFmpeg）能不能**直接**打开并解码（= 零开发量）
  ③ 不能的话，缺的是什么（demuxer / 解码器 / 密钥）

用法：py -3.14 vendor/_probe_new6.py
"""

import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

JOSHW = r"D:\MSU1Downloads\joshw"

#: 每个机种取一个代表文件（+ 可选第二格式）
SAMPLES = [
    ("PS3",  "PlayStation 3/God of War - Ascension (Demo)(2013)(SCE Santa Monica)(SCE)[PS3]/MENU_LOOP.XVAG"),
    ("PS3",  "PlayStation 3/God of War - Ascension (Demo)(2013)(SCE Santa Monica)(SCE)[PS3]/SND0.AT3"),
    ("PSP",  "PSP/Amnesia (2011-08-11)(Design Factory, Otomate)(Idea Factory)[PSP]/000.hca"),
    ("Vita", "PS Vita/Adventures of Mana [Seiken Densetsu - Final Fantasy Gaiden] (2016-02-04)(Square Enix)[Vita]/!playlist (8bit).m3u"),
    ("GC",   "GameCube/Conflict - Desert Storm [Conflict Delta - Wangan Sensou 1991] (2003-04-17)(Pivotal)(SCi)[GC]/stream/STREAM__000.psf"),
    ("GC",   "GameCube/Conflict - Desert Storm [Conflict Delta - Wangan Sensou 1991] (2003-04-17)(Pivotal)(SCi)[GC]/STREAM__000.txtp"),
    ("Wii",  "Wii/Active Life - Explorer [Family Trainer - Treasure Adventure] (2010-10-20)(Namco Bandai)[Wii]/BGM_Adventure1.brstm"),
    ("3DS",  "Nintendo 3DS/Pokemon X + Y [Pocket Monsters X] (2013-10-12)(Game Freak)(Nintendo)[3DS]/bgm_xy_ajito.laac"),
    ("3DS",  "Nintendo 3DS/Pokemon X + Y [Pocket Monsters X] (2013-10-12)(Game Freak)(Nintendo)[3DS]/bgm_xy_ajito.laac.txth"),
]


def head(path, n=64):
    with open(path, "rb") as f:
        return f.read(n)


def hexdump(b, indent="    "):
    for i in range(0, len(b), 16):
        chunk = b[i:i + 16]
        hx = " ".join("%02x" % c for c in chunk)
        tx = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
        print("%s%04x: %-47s  %s" % (indent, i, hx, tx))


def try_av(path):
    """PyAV 直接打开 —— 返回 (ok, 说明)。"""
    try:
        import av
    except Exception as exc:                             # noqa: BLE001
        return False, "PyAV 没装：%s" % exc
    try:
        c = av.open(path)
    except Exception as exc:                             # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, str(exc)[:110])
    try:
        info = []
        for st in c.streams:
            if st.type != "audio":
                continue
            cc = getattr(st, "codec_context", None)
            info.append("%s / %s / %dHz / %sch" % (
                st.codec_context.name,
                getattr(cc, "name", "?"),
                getattr(cc, "sample_rate", 0) or 0,
                getattr(cc, "channels", 0) or 0))
        if not info:
            return False, "打开了但没有音频流（demuxer 认了容器、没认出编码）"
        # 真解一帧
        got = 0
        for pkt in c.demux():
            for fr in pkt.decode():
                got += fr.samples
                break
            if got:
                break
        if not got:
            return False, "有音频流但**一帧都解不出来**（%s）" % info[0]
        return True, "%s（解出 %d 样本）" % ("；".join(info), got)
    finally:
        try:
            c.close()
        except Exception:                                # noqa: BLE001
            pass


def main():
    print("=" * 72)
    print("六个新机种 · 格式探针（第一步：查有无现成方案）")
    print("=" * 72)

    for plat, rel in SAMPLES:
        p = os.path.join(JOSHW, rel)
        print("\n--- [%s] %s" % (plat, os.path.basename(rel)))
        if not os.path.isfile(p):
            print("    !! 文件不存在：%s" % p)
            continue
        print("    大小 %d B" % os.path.getsize(p))
        b = head(p)
        hexdump(b)
        # 文本类文件（txtp / txth / m3u）直接打出来看
        if os.path.splitext(p)[1].lower() in (".txtp", ".txth", ".m3u", ".txt"):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    txt = f.read(600)
                print("    ---- 文本内容 ----")
                for ln in txt.splitlines()[:14]:
                    print("    | %s" % ln)
            except OSError as exc:
                print("    (读不出文本：%s)" % exc)
            continue
        ok, why = try_av(p)
        print("    PyAV: %s %s" % ("✅" if ok else "❌", why))

    print("\n" + "=" * 72)


if __name__ == "__main__":
    sys.exit(main())
