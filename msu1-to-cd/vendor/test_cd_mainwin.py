#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_cd_mainwin.py — 主窗口支持 CD 音轨（v4.31）的回归锁。

为什么单独一条：主窗的 CD 支持横跨三处 —— **类型判定**（`_kind_of_file` /
`_kind_of_dir`）、**元信息合成**（`_with_cd_meta` / `_chip_secs_of`）、
**转换分派**（`_do_chip` 顺带 / `_do_cd_audio`）。任一处漏改的症状都是
「拖进去没反应」「转了但数量不对」「CD 被单曲上限砍断」，而这三处
`test_cd_audio.py` 都不覆盖（那个只测解码器本身）。

顺带锁住两条 v4.31 的**决策**（不是实现细节）：
  * 崩溃计数**不得**再关闭拖放（历史上这条让拖放神秘消失且不自愈）；
  * CD 音轨**不得**吃「单曲最长」钳制。

不需要 .ape 素材：WAV 与 APE 在 `cd_audio` 里走同一条 `av.open` →
`AudioResampler` 路径，这里验的是我们自己的那层逻辑。

跑法：python vendor/test_cd_mainwin.py
"""

import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


def make_wav(path, secs=3.0, rate=44100):
    """写一段已知长度、已知格式的 PCM（只用 stdlib，少一个失败变量）。"""
    import array
    import math
    n = int(secs * rate)
    pcm = array.array("h")
    for i in range(n):
        v = int(11000 * math.sin(2 * math.pi * 440 * i / rate))
        pcm.append(v)
        pcm.append(v)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return path


def wav_info(path):
    with wave.open(path, "rb") as w:
        return (w.getnchannels(), w.getsampwidth() * 8, w.getframerate(),
                w.getnframes() / float(w.getframerate()))


def main():
    import tkinter as tk
    import msu1_gui as g

    print("=" * 62)
    print("主窗 CD 音轨支持验收（PyAV 可用 = %s）" % _av_ok())
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="cdmain_")
    pure = os.path.join(tmp, "pure_cd")
    mixed = os.path.join(tmp, "mixed")
    chip = os.path.join(tmp, "chip_only")
    for d in (pure, mixed, chip):
        os.makedirs(d, exist_ok=True)

    # 纯 CD 目录：两首都比下面的 max_secs 长，用来证明「不被砍断」
    make_wav(os.path.join(pure, "A Track.wav"), 3.0)
    make_wav(os.path.join(pure, "B Track.wav"), 4.0)
    # 混合包：1 个 HES + 1 首 CD（PCE-CD 的结构）
    hes_dir = os.path.join(ROOT, "vendor", "samples", "hes")
    for n in ("AKCD3001.hes", "AKCD3001.m3u"):
        shutil.copy(os.path.join(hes_dir, n), mixed)
    make_wav(os.path.join(mixed, "PACK_20 Theme.wav"), 3.0)
    for n in ("AKCD3001.hes", "AKCD3001.m3u"):
        shutil.copy(os.path.join(hes_dir, n), chip)

    root = tk.Tk()
    app = g.App(root)
    root.update()
    try:
        # ---- 1. 类型判定 --------------------------------------------------
        print("\n[1] 类型判定")
        check("单文件 .ape → CD", g.App._kind_of_file("x.ape")[:2] == ("CD", "◎"))
        check("单文件 .flac → CD", g.App._kind_of_file("x.flac")[:2] == ("CD", "◎"))
        check("单文件 .wav → CD", g.App._kind_of_file("x.wav")[:2] == ("CD", "◎"))
        check(".hes 仍走 CHIP",
              g.App._kind_of_file(os.path.join(mixed, "AKCD3001.hes"))[0] == "CHIP")
        check("纯 CD 目录 → CD", g.App._kind_of_dir(pure) == "CD")
        check("混合目录 → CHIP（内部顺带转 CD）",
              g.App._kind_of_dir(mixed) == "CHIP")
        check("纯芯片目录 → CHIP", g.App._kind_of_dir(chip) == "CHIP")

        # ---- 2. 收集与元信息 ----------------------------------------------
        print("\n[2] 收集与元信息")
        fs = g._cd_files(mixed)
        check("_cd_files 只挑 CD 扩展名（不误收 .hes/.m3u）",
              len(fs) == 1 and fs[0].lower().endswith(".wav"),
              "got %s" % [os.path.basename(x) for x in fs])
        meta = g._cd_meta(pure)
        check("_cd_meta 首数 = 2", meta is not None and meta.n_tracks == 2)
        check("_cd_meta 总时长 ≈ 7.0s",
              meta is not None and abs(meta.total_secs - 7.0) < 0.05,
              "got %.2fs" % (meta.total_secs if meta else -1))
        check("_cd_meta fmt = CD-DA", meta is not None and meta.fmt == g.CD_FMT)

        # ---- 3. 混合包的首数不能只数芯片 ----------------------------------
        print("\n[3] 混合包元信息合成")
        app.add_paths([mixed])
        root.update()
        s = app.sources[0]
        hes_n = int(g.Chip.probe(os.path.join(mixed, "AKCD3001.hes")).n_tracks)
        check("混合包 kind = CHIP", s["kind"] == "CHIP", "got %s" % s["kind"])
        check("首数 = HES 轨数 + CD 首数（不是只数 HES）",
              s["n_tracks"] == hes_n + 1,
              "got %s (HES=%s)" % (s["n_tracks"], hes_n))
        check("cd_tracks = 1", s["cd_tracks"] == 1, "got %s" % s["cd_tracks"])
        check("cd_secs ≈ 3.0", abs(s["cd_secs"] - 3.0) < 0.05,
              "got %.2f" % s["cd_secs"])
        check("计划文案分开写 HES 与 CD-DA",
              "CD-DA" in app.var_plan.get(),
              app.var_plan.get()[:70])
        check("格式列只放格式名（不带轨数，避免三位数被 74px 列宽裁掉）",
              "×" not in app._fmt_cell(s), app._fmt_cell(s))

        # ---- 4. CD 不吃「单曲最长」----------------------------------------
        print("\n[4] 单曲上限只钳芯片部分")
        app.var_max.set("30")
        entry = {"n_tracks": 7, "cd_tracks": 1, "est_secs": 150.0,
                 "cd_secs": 78.0, "total_secs": 978.0}
        got = app._chip_secs_of(entry)
        check("6 首芯片×30s + 78s CD = 258s（CD 不被砍到 30s）",
              abs(got - (30 * 6 + 78.0)) < 0.01, "got %.2f" % got)
        app.var_max.set("0")

        # ---- 5. _merge_subs（合并只做一次，避免同名覆盖）------------------
        print("\n[5] 统计字典合并")
        a = {"ok": 2, "fail": 0, "files": ["a1", "a2"], "seconds": 10.0,
             "errors": [], "last_idx": 2}
        b = {"ok": 1, "fail": 0, "files": ["b1"], "seconds": 5.0,
             "errors": [], "last_idx": 3}
        m = g._merge_subs(a, b)
        check("ok/files/seconds 累加", m["ok"] == 3 and len(m["files"]) == 3
              and abs(m["seconds"] - 15.0) < 1e-6)
        check("last_idx 取最大（编号不回退）", m["last_idx"] == 3)
        check("None 被跳过", g._merge_subs(a, None)["ok"] == 2)

        # ---- 6. 端到端：纯 CD 目录 -> WAV（原长，不截断）------------------
        print("\n[6] 端到端转换")
        app.clear_sources()
        app.add_paths([pure])
        root.update()
        out = tempfile.mkdtemp(prefix="cdout_")
        opts = {"do_cd": False, "do_wav": True, "out_dir": out,
                "album": "", "performer": "", "gap": 2, "verify": False,
                "cd_wav": False, "policy": "id666", "policy_label": "ID666",
                "speed": 1.0, "max_secs": 1.0,   # 故意设 1 秒
                "chip_secs": 150.0, "total_tracks": 2, "est_secs": 7.0,
                "cur_kind": "", "merge_wav": False}
        q = queue.Queue()
        w = g.ConvertWorker(list(app.sources), opts, q, threading.Event())
        w.start()
        w.join(120)
        files = sorted(n for n in os.listdir(out) if n.lower().endswith(".wav"))
        check("产出 2 个 WAV", len(files) == 2, "got %s" % files)
        sizes = [wav_info(os.path.join(out, f))[3] for f in files]
        check("两首都按原长输出（3.0s / 4.0s，**未**被 1s 上限砍断）",
              len(sizes) == 2 and abs(sizes[0] - 3.0) < 0.1
              and abs(sizes[1] - 4.0) < 0.1,
              "got %s" % ["%.2f" % x for x in sizes])
        fmts = {wav_info(os.path.join(out, f))[:3] for f in files}
        check("格式统一 2ch/16bit/44100Hz", fmts == {(2, 16, 44100)}, str(fmts))
        check("文件名带连续编号", files[0].startswith("01_")
              and files[1].startswith("02_"), str(files))

        # ---- 7. 拖放不再被崩溃计数关闭（决策锁）--------------------------
        print("\n[7] 拖放策略")
        src = open(os.path.join(ROOT, "msu1_gui.py"), encoding="utf-8").read()
        check("源码里不存在『异常退出 -> want_drop = False』",
              "want_drop = False" not in src)
        check("保留 --no-drop 手动开关", '"--no-drop" not in argv' in src)
        g.release_session_lock()
        check("正常关闭后锁文件被删",
              not os.path.exists(g._session_lock_path()))
        with open(g._session_lock_path(), "w", encoding="utf-8") as f:
            f.write("999999 5 %d" % int(time.time()))
        prev, streak = g.take_session_lock()
        check("死 pid 判定为异常退出", prev is True)
        check("连续次数累加", streak == 6, "got %s" % streak)
        g.release_session_lock()
    finally:
        try:
            root.destroy()
        except Exception:                                # noqa: BLE001
            pass
        g.release_session_lock()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 62)
    print("结果: %d 通过 / %d 失败" % (_pass, _fail))
    return 0 if _fail == 0 else 1


def _av_ok():
    try:
        import cd_audio
        return cd_audio.available()
    except Exception:                                    # noqa: BLE001
        return False


if __name__ == "__main__":
    sys.exit(main())
