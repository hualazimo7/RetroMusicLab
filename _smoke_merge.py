#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_smoke_merge.py — 多轨合并端到端门禁（v4.24，需 libgme + 真实 NSF 样本）。

验两件事，都是「只有真跑才看得见」的：
1. **主窗** ConvertWorker 勾上合并后：17 个分轨 + 1 个合并文件，
   合并帧数 = 分轨之和 + 间隔×（n-1），重转不产生「（合并） 2.wav」；
2. **下载窗** ConvertWorker 同理，并且第二次对同一源走 `cv_skip` 时
   **不再重复合并**（否则每点一次下载就把几百 MB 重拷一遍）。

样本用 `vendor/samples/nsf/10-Yard Fight`（17 轨，1.5s/轨，两秒跑完）。
纯离线（不联网、不建窗口）。
"""

import os
import shutil
import sys
import tempfile
import threading
import wave

PROJ = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(PROJ, "msu1-to-cd")
sys.path.insert(0, CODE)

import msu1_gui as g          # noqa: E402
import dl_convert as dc       # noqa: E402
import wavmerge as wm         # noqa: E402

NSF = os.path.join(CODE, "vendor", "samples", "nsf",
                   "10-Yard Fight (1985-08-30)(Irem).nsf")
R = []


def check(ok, label, detail=""):
    R.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", label,
                       ("  [" + str(detail) + "]") if detail else ""))


def main_win(out):
    """主窗 ConvertWorker + merge_wav=True"""
    info = g.Chip.probe(NSF)
    n = max(1, int(getattr(info, "n_tracks", 0) or 0))
    src = {"path": NSF, "label": "10-Yard Fight", "kind": "CHIP",
           "fmt": "NSF", "n_tracks": n, "est_secs": 1.5, "exact": False}
    opts = g._default_opts(out, merge_wav=True)
    opts["chip_secs"] = 1.5
    q = __import__("queue").Queue()
    w = g.ConvertWorker([src], opts, q, threading.Event())
    w.start()
    w.join(300)
    logs = []
    while not q.empty():
        m = q.get_nowait()
        if m[0] == "log":
            logs.append(m[1])
        elif m[0] == "error":
            print("  ERR", m[1])
    wavs = sorted(x for x in os.listdir(out) if x.lower().endswith(".wav"))
    merged = [x for x in wavs if "（合并）" in x]
    tracks = [x for x in wavs if "（合并）" not in x]
    print("  主窗: %d 个 WAV（分轨 %d + 合并 %d）" % (len(wavs), len(tracks), len(merged)))
    check(len(tracks) >= 2, "主窗 多轨分轨已产出", len(tracks))
    check(len(merged) == 1, "主窗 合并文件正好 1 个", merged)
    check(any("🔗 已合并" in x for x in logs), "主窗 日志有合并行",
          [x for x in logs if "🔗" in x][:1])
    if merged:
        mp = os.path.join(out, merged[0])
        frames = 0
        for t in tracks:
            with wave.open(os.path.join(out, t), "rb") as wf:
                frames += wf.getnframes()
        with wave.open(mp, "rb") as wf:
            got = wf.getnframes()
        gap = int(round(44100 * 0.5))
        want = frames + gap * (len(tracks) - 1)
        check(got == want, "主窗 合并帧数 = 分轨之和 + 间隔",
              "%d vs %d" % (got, want))
        check(os.path.getsize(mp) == 44 + want * 4,
              "主窗 合并文件大小自洽", os.path.getsize(mp))
    # 再跑一次：合并文件名不变（覆盖），不该出现「（合并） 2.wav」
    w2 = g.ConvertWorker([src], opts, q, threading.Event())
    w2.start()
    w2.join(300)
    wavs2 = [x for x in os.listdir(out) if "（合并）" in x]
    check(len(wavs2) == 1, "主窗 重转不产生（合并） 2.wav", wavs2)


def dl_win(out):
    """下载窗 ConvertWorker + merge_wav=True"""
    os.makedirs(out, exist_ok=True)
    evs = []
    cw = dc.ConvertWorker(out, lambda k, p: evs.append((k, p)),
                          threading.Event(), merge_wav=True)
    cw.CHIP_SECS = 1.5
    cw.enqueue({"src": NSF, "url": "u1", "pkg": "pkg"})
    cw.mark_src_finished()
    cw.start()
    cw.wait(300)
    wavs = sorted(x for x in os.listdir(out) if x.lower().endswith(".wav"))
    merged = [x for x in wavs if "（合并）" in x]
    tracks = [x for x in wavs if "（合并）" not in x]
    print("  下载窗: %d 个 WAV（分轨 %d + 合并 %d）" % (len(wavs), len(tracks), len(merged)))
    check(len(tracks) >= 2, "下载窗 多轨分轨已产出", len(tracks))
    check(len(merged) == 1, "下载窗 合并文件正好 1 个", merged)
    check(os.path.basename(merged[0]).startswith("10-Yard Fight")
          if merged else False, "下载窗 合并文件名取源文件名",
          merged[:1])
    check(any(k == "cv_merged" and not p.get("error") for k, p in evs),
          "下载窗 发了 cv_merged 成功事件",
          [p for k, p in evs if k == "cv_merged"][:1])
    st = cw.stats()
    check(st.get("merged") == 1, "下载窗 统计里有 merged=1", st)

    # 再跑一次：源文件指纹一致 -> already_done 跳过，但合并文件应仍在
    evs2 = []
    cw2 = dc.ConvertWorker(out, lambda k, p: evs2.append((k, p)),
                           threading.Event(), merge_wav=True)
    cw2.enqueue({"src": NSF, "url": "u1", "pkg": "pkg"})
    cw2.mark_src_finished()
    cw2.start()
    cw2.wait(300)
    kinds = [k for k, _p in evs2]
    check("cv_skip" in kinds, "下载窗 第二次走 skip", kinds[:4])
    check("cv_merged" not in kinds,
          "下载窗 已合过就不再重拷（第二次不发 cv_merged）", kinds[:4])
    check(len([x for x in os.listdir(out) if "（合并）" in x]) == 1,
          "下载窗 第二次不生成本（合并） 2.wav",
          [x for x in os.listdir(out) if "（合并）" in x])


def ui():
    """复选框就位 + 与「输出 WAV」的联动（真建窗口，不真转）。"""
    import tkinter as tk
    root = tk.Tk()
    app = g.App(root)
    root.update()
    check(app.cb_merge.instate(["!disabled"]),
          "UI 主窗 合并复选框默认可用（WAV 默认勾选）")
    app.var_wav.set(False)
    root.update()
    check(app.cb_merge.instate(["disabled"]),
          "UI 主窗 取消 WAV 后合并置灰")
    check(app.var_merge.get() is False, "UI 主窗 取消 WAV 不擅自改勾选")
    app.var_wav.set(True)
    root.update()
    check(app.cb_merge.instate(["!disabled"]), "UI 主窗 重新勾 WAV 后恢复")

    dd = g.DownloadDialog(root)
    dd.withdraw()
    root.update()
    check(dd.var_merge_cb.instate(["disabled"]),
          "UI 下载窗 未开「自动转 WAV」时合并置灰")
    dd.var_auto_wav.set(True)
    root.update()
    check(dd.var_merge_cb.instate(["!disabled"]),
          "UI 下载窗 开了「自动转 WAV」后合并可用")
    dd._closed = True
    dd._shutdown()
    root.update()
    root.destroy()


def main():
    base = tempfile.mkdtemp(prefix="merge_e2e_")
    try:
        ui()
        o1 = os.path.join(base, "main")
        os.makedirs(o1, exist_ok=True)
        print("== 主窗 ==")
        main_win(o1)
        print("== 下载窗 ==")
        dl_win(os.path.join(base, "dl"))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    bad = R.count(False)
    print("-" * 60)
    print("结果: %d 通过 / %d 失败" % (R.count(True), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
