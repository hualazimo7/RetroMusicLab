#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_cd_audio.py — CD-DA 音轨解码（cd_audio）与分流接线验证。

不需要 .ape 素材：用 ``soundfile`` 现做 FLAC/WAV 样本（PyAV 的 APE 解码路径
与 FLAC 共用同一套 ``av.open`` → ``AudioResampler`` 流程，这里验证的是
**我们自己的那层**：格式统一、命名、编号、避让、截断、取消）。

跑法：python vendor/test_cd_audio.py
"""

import os
import shutil
import sys
import tempfile
import threading
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cd_audio                                            # noqa: E402
import dl_convert                                          # noqa: E402

_GREEN, _RED, _RST = "\033[32m", "\033[31m", "\033[0m"
if not sys.stdout.isatty():
    _GREEN = _RED = _RST = ""

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


def make_tone(path, secs=2.5, rate=44100, channels=2, subtype="PCM_16"):
    """写一个已知长度的正弦样本（FLAC/WAV 都行）。"""
    import array
    import math
    import soundfile as sf
    n = int(secs * rate)
    data = array.array("h")
    for i in range(n):
        v = int(12000 * math.sin(2 * math.pi * 440 * i / rate))
        for _c in range(channels):
            data.append(v)
    import numpy as np
    arr = np.frombuffer(data.tobytes(), dtype="<i2").reshape(-1, channels)
    sf.write(path, arr, rate, subtype=subtype)
    return path


def wav_info(path):
    with wave.open(path, "rb") as w:
        return (w.getnchannels(), w.getsampwidth() * 8, w.getframerate(),
                w.getnframes() / float(w.getframerate()))


def main():
    print("=" * 62)
    print("cd_audio 验收（PyAV 可用性 = %s）" % cd_audio.available())
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="cdtest_")
    try:
        # ---------------- 1) 扩展名与分流 ----------------
        print("\n[1] 扩展名判定 / policy 分流")
        for ext in (".ape", ".flac", ".wav", ".ogg", ".m4a"):
            check("is_cd_audio(%s)" % ext, cd_audio.is_cd_audio("a" + ext))
        for ext in (".mp3", ".hes", ".nsf", ".cue", ".bin", ".7z", ""):
            check("is_cd_audio 拒绝 %s" % (ext or "(空)"),
                  not cd_audio.is_cd_audio("a" + ext))
        check("policy_for(.ape) == cd", dl_convert.policy_for("x.ape") == "cd")
        check("policy_for(.flac) == cd", dl_convert.policy_for("x.flac") == "cd")
        check("policy_for(.hes) == chip", dl_convert.policy_for("x.hes") == "chip")
        check("policy_for(.spc) == id666", dl_convert.policy_for("x.spc") == "id666")
        check("is_convertible(.ape)", dl_convert.is_convertible("x.ape"))
        check("is_convertible 仍拒绝 .cue", not dl_convert.is_convertible("x.cue"))
        check("is_convertible 仍拒绝 .bin", not dl_convert.is_convertible("x.bin"))
        check("is_convertible 仍拒绝 .mp3", not dl_convert.is_convertible("x.mp3"))

        if not cd_audio.available():
            print("\n  ⚠ PyAV 不可用 —— 后面的解码用例跳过")
            return finish()

        # ---------------- 2) 标题剥离 ----------------
        print("\n[2] 曲名剥离")
        cases = {
            "KMCD3005_04 Overture.ape": "Overture",
            "KMCD4007_05 Tokimeki [Opening Demo].ape": "Tokimeki [Opening Demo]",
            "04 - Stage 1.ape": "Stage 1",
            "Overture.ape": "Overture",
        }
        for src, want in cases.items():
            got = cd_audio._title_of(os.path.join(tmp, src))
            check("标题 %s -> %s" % (src, want), got == want, "got %r" % got)

        # ---------------- 3) probe ----------------
        print("\n[3] probe（时长/采样率/编码）")
        flac = make_tone(os.path.join(tmp, "PACK_01 Song.flac"), 2.5)
        p = cd_audio.probe(flac)
        check("probe ok", p["ok"], p.get("error", ""))
        check("probe 时长 ≈2.5s", abs(p["secs"] - 2.5) < 0.05, "%.3f" % p["secs"])
        check("probe rate=44100", p["rate"] == 44100, str(p["rate"]))
        check("probe codec=flac", p["codec"] == "flac", p["codec"])
        check("probe 标题 = Song", p["title"] == "Song", p["title"])
        pbad = cd_audio.probe(os.path.join(tmp, "nope.ape"))
        check("probe 不存在的文件不抛异常", pbad["ok"] is False and pbad["error"])

        # ---------------- 4) 转出格式 ----------------
        print("\n[4] 转 WAV：格式 / 时长 / 命名")
        out = os.path.join(tmp, "out")
        os.makedirs(out, exist_ok=True)
        used = set()
        r = cd_audio.convert_one(flac, out, used=used, start_idx=0)
        check("convert ok", r["ok"] == 1, str(r["errors"]))
        check("产出 1 个文件", len(r["files"]) == 1)
        name = os.path.basename(r["files"][0])
        check("命名 01_Song.wav", name == "01_Song.wav", name)
        nch, bits, rate, secs = wav_info(r["files"][0])
        check("格式 2ch/16bit/44100", (nch, bits, rate) == (2, 16, 44100),
              "%dch/%dbit/%dHz" % (nch, bits, rate))
        check("时长 ≈2.5s", abs(secs - 2.5) < 0.02, "%.3f" % secs)
        check("last_idx == 1", r["last_idx"] == 1, str(r["last_idx"]))

        # ---------------- 5) 编号续编 + 同名避让 ----------------
        print("\n[5] 连续编号 / 同名避让（跨文件共享 used）")
        r2 = cd_audio.convert_one(flac, out, used=used, start_idx=r["last_idx"])
        check("第二个是 02_Song (2).wav",
              os.path.basename(r2["files"][0]) == "02_Song (2).wav",
              os.path.basename(r2["files"][0]))
        check("last_idx 累加到 2", r2["last_idx"] == 2, str(r2["last_idx"]))
        check("used 集合已登记两个不同键", len(used) == 2, str(sorted(used)))

        # ---------------- 6) 单声道上混 ----------------
        print("\n[6] 单声道 -> 立体声")
        mono = make_tone(os.path.join(tmp, "PACK_02 Mono.flac"), 1.0, channels=1)
        r3 = cd_audio.convert_one(mono, out, used=set(), start_idx=0)
        check("单声道也能转", r3["ok"] == 1, str(r3["errors"]))
        check("输出是 2ch", wav_info(r3["files"][0])[0] == 2)

        # ---------------- 7) 截断 ----------------
        print("\n[7] max_secs 截断")
        r4 = cd_audio.convert_one(flac, out, used=set(), start_idx=0, max_secs=1.0)
        check("截断后 ok", r4["ok"] == 1, str(r4["errors"]))
        d = wav_info(r4["files"][0])[3]
        check("截断到 1.0s", abs(d - 1.0) < 0.05, "%.3f" % d)

        # ---------------- 8) 取消 ----------------
        print("\n[8] 取消")
        ev = threading.Event()
        ev.set()
        r5 = cd_audio.convert_one(flac, out, used=set(), start_idx=0, cancel=ev)
        check("取消不产出文件", r5["files"] == [], str(r5["files"]))
        check("取消不算失败", r5["fail"] == 0, str(r5["fail"]))
        left = [n for n in os.listdir(out) if n.endswith(".part")]
        check("取消后不留 .part 半成品", not left, str(left))

        # ---------------- 9) 估算口径 ----------------
        print("\n[9] estimate_files 计入 CD 音轨")
        est = dl_convert.estimate_files([flac], 150.0)
        check("估算 tracks=1", est["tracks"] == 1.0, str(est["tracks"]))
        check("估算秒数用真实时长（非 150）", abs(est["secs"] - 2.5) < 0.1,
              "%.2f" % est["secs"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return finish()


def finish():
    print("\n" + "=" * 62)
    print("结果: %d 通过 / %d 失败" % (_pass, _fail))
    print("=" * 62)
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
