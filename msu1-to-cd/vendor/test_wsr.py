#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_wsr.py — WSR（WonderSwan Sound Rip）端到端自检。

覆盖这一轮最容易回归的四个点：

1. **WSR footer 解析** —— 头在**文件尾部** 32 字节，`0xFFFE5` = 最初の曲番号。
2. **曲号靠 AX 寄存器传** —— 这是整个格式的命门。WSR 规范写明
   `CS=0xFFFF, IP=0x0000, AX=曲号`，而普通模拟器复位时把 AX 清 0，所以
   ROM 启动桩什么都不放。`wsr2wav.exe` 在 `retro_load_game` 之后直接
   `v30mz_set_reg(NEC_AW, track)`。**谁把这一步删了，下面「有声」的断言就红。**
3. **曲目数是实测出来的** —— 各 rip 驱动布局全不同，没有便宜的来源。
4. **接线** —— `.wsr` 必须进 CHIP_EXTS / ALL_EXTS / is_convertible，且
   **不能**留在 SKIP_REASONS（否则「能转的说成转不了」）。

跑法（需要样本 + wsr2wav.exe；缺任一个就整段 skip，不判失败）：

    py -3.14 vendor/test_wsr.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_GREEN, _RED, _YEL, _RST = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    _GREEN = _RED = _YEL = _RST = ""

_pass = 0
_fail = 0
_skip = 0

SAMPLE = os.path.join(ROOT, "vendor", "samples", "wsr", "SWJ-BANC3C.wsr")


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  {_GREEN}PASS{_RST} {name}" + (f"  {detail}" if detail else ""))
    else:
        _fail += 1
        print(f"  {_RED}FAIL{_RST} {name}  {detail}")
    return bool(cond)


def skip(name, why):
    global _skip
    _skip += 1
    print(f"  {_YEL}SKIP{_RST} {name}  {why}")


def main():
    import chip_formats as cf

    print("=== 注册表 / 识别（不依赖样本与解码器）")
    check(".wsr 在 EXT_TABLE 且映射到 WSR", cf.EXT_TABLE.get(".wsr") == "WSR")
    check(".wsr 进 CHIP_EXTS", ".wsr" in cf.CHIP_EXTS)
    check(".wsr 进 ALL_EXTS（界面收集靠它）", ".wsr" in cf.ALL_EXTS)
    check("SYSTEM_NAMES 有 WSR", "WSR" in cf.SYSTEM_NAMES)
    check("WSR_EXTS 常量存在", getattr(cf, "WSR_EXTS", ()) == (".wsr",))
    check("is_chip_ext('.wsr')", cf.is_chip_ext(".wsr"))
    # 魔数在文件尾部，identify 必须靠扩展名而不是头部字节
    check("identify 对 .wsr 直接给 WSR（不信头部魔数）",
          cf.identify("x.wsr", b"garbage-head") == ("WSR", False))

    print("=== 「转不了」清单不得包含 .wsr")
    import dl_convert as dl
    check(".wsr 不在 SKIP_REASONS", ".wsr" not in dl.SKIP_REASONS)
    check("is_convertible('.wsr')", dl.is_convertible("a/x.wsr"))
    check("explain_skipped(整批 .wsr) → 一个字都不说",
          dl.explain_skipped(["a/%02d.wsr" % i for i in range(40)]) == "")

    print("=== 解码器")
    try:
        import wsr_native as wn
    except Exception as e:                              # noqa: BLE001
        skip("wsr_native 可用", str(e))
        return summary()
    ok, why = wn.decoder_ready()
    if not ok:
        skip("wsr2wav.exe 可用", why)
        return summary()
    check("wsr2wav.exe 找得到", True, os.path.basename(why))

    if not os.path.isfile(SAMPLE):
        skip("真实样本", "缺 %s" % SAMPLE)
        return summary()

    print("=== footer 解析")
    foot = wn.read_footer(SAMPLE)
    check("read_footer ok", foot["ok"], foot.get("error", ""))
    check("size = 196608", foot["size"] == 196608, foot["size"])
    check("first_song = 1", foot["first_song"] == 1, foot["first_song"])
    check("ROM 容量字节 = 0x04（16Mbit）", foot["rom_cap"] == 0x04, foot["rom_cap"])
    check("非 WSR 文件 ok=False", wn.read_footer(__file__)["ok"] is False)

    print("=== 曲目实测（扫描）")
    hits = wn.scan_tracks(SAMPLE, use_cache=False)
    check("扫出 122 首", len(hits) == 122, len(hits))
    check("曲号升序且从 1 开始", hits and hits[0] == 1 and hits == sorted(hits),
          hits[:5])
    # 空曲（全通道指向 00 00 00 00 ff ff）不该被判成有声
    check("29~33 号空曲没被算进来", not any(29 <= t <= 33 for t in hits))

    print("=== AX 注入 = 真的出声")
    # 这一条是整件事的核心：AX 不注入的话解码器输出会全是 0。
    r = wn.decode_track(SAMPLE, 60, secs=2.0)
    check("decode_track ok", r["ok"], r.get("error", ""))
    check("时长 2.00s", abs(r["seconds"] - 2.0) < 0.05, r["seconds"])
    peak = 0
    pcm = r["pcm"]
    for i in range(0, min(len(pcm), 200000), 2):
        v = int.from_bytes(pcm[i:i + 2], "little", signed=True)
        if abs(v) > peak:
            peak = abs(v)
    check("PCM 不是静音（峰值 > 1000）", peak > 1000, "peak=%d" % peak)
    # 反向锁：0 号曲在这个 rip 里是**空曲**，本来就该静音
    r0 = wn.decode_track(SAMPLE, 0, secs=1.0)
    check("0 号空曲确实静音（说明不是随便给个值就有声）",
          r0["ok"] and not any(r0["pcm"][:4000]), "len=%d" % len(r0["pcm"]))

    print("=== probe 接线")
    info = cf.probe(SAMPLE)
    check("probe fmt=WSR", info.fmt == "WSR", info.fmt)
    check("probe ok", info.ok, info.reason)
    check("probe n_tracks=122", info.n_tracks == 122, info.n_tracks)
    check("probe system 是 WonderSwan", "WonderSwan" in (info.system or ""),
          info.system)

    print("=== m3u 曲目表优先（权威，且不再扫描）")
    # 仓库里这个样本**故意不带 m3u**，好锁住「没有 m3u 时回落到扫描」这条。
    # 下面现造一个带 m3u 的临时包，验另一条路。
    import shutil
    import tempfile
    tmpd = tempfile.mkdtemp(prefix="wsr_m3u_")
    try:
        stem = os.path.splitext(os.path.basename(SAMPLE))[0]
        wsr = os.path.join(tmpd, stem + ".wsr")
        shutil.copy2(SAMPLE, wsr)
        with open(os.path.join(tmpd, stem + ".m3u"), "w",
                  encoding="utf-8") as f:
            f.write("# @TITLE       Test Pack\n")
            f.write("# @ARTIST      Nobody\n\n")
            f.write("%s.wsr::WSR,0,Opening,0:30,,10\n" % stem)
            f.write("%s.wsr::WSR,5,Battle,1:00,,10\n" % stem)
        pl = wn.read_playlist(wsr)
        check("read_playlist 读到 2 条", len(pl) == 2, pl)
        check("曲号/曲名/时长都解析对",
              pl == [(0, "Opening", 30.0), (5, "Battle", 60.0)], pl)
        info2 = cf.probe(wsr)
        check("probe 用 m3u：2 轨（不是扫描出的 122）",
              info2.n_tracks == 2, info2.n_tracks)
        check("probe 带回曲名（按曲号索引）",
              (info2.track_titles or {}).get(5) == "Battle",
              info2.track_titles)
        tracks2 = wn.load_tracks(wsr, secs=150.0)
        check("load_tracks 只解 m3u 里的 2 首", len(tracks2) == 2, len(tracks2))
        got = sum(len(t["pcm"]) for t in tracks2) / 176400.0
        check("时长按 m3u 走（30+60=90s），不是 2×150s",
              abs(got - 90.0) < 0.5, "%.1fs" % got)
        check("曲名进了文件名", "Opening" in tracks2[0]["name"],
              tracks2[0]["name"])
        import msu1_to_cd as core2
        core2.cleanup_spc_temp(tracks2)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)

    return summary()


def summary():
    print()
    print("=" * 60)
    print("通过 %d 项，失败 %d 项，跳过 %d 项" % (_pass, _fail, _skip))
    print("=" * 60)
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
