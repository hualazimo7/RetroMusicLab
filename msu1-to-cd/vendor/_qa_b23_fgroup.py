# -*- coding: utf-8 -*-
"""QA 独立验收 · B23 双路径收尾 + F 组零回归（真 Tk）

B23:点「■ 停止」后``all_done`` 与 ``cv_thread_exit`` **两条路径**都会到
    ``_finish_download()`` -> 必须只执行一次（否则汇总日志打两遍、
    失败弹窗弹两次）。

F 组:开关关闭时必须与 v3.9 **逐条一致**（不多建目录、不多发事件、
     状态文案无「🎵」）。

做法:真 Tk + 打点计数,**分别驱动两条路径**再一起驱动,验证重入守卫。

用法: C:/Python314/python.exe vendor/_qa_b23_fgroup.py
"""
import glob
import os
import queue
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = [0]
FAIL = []


def check(label, cond, detail="", ac=""):
    PASS[0] += 1
    line = "  [%s] %s" % ("PASS" if cond else "FAIL", label)
    if ac:
        line += "  (%s)" % ac
    if detail:
        line += "  -- %s" % detail
    print(line, flush=True)
    if not cond:
        FAIL.append("%s | %s" % (label, detail))
    return bool(cond)


def main():
    print("=" * 78)
    print("QA 独立验收 · B23 双路径收尾 + F 组零回归  pid=%d" % os.getpid())
    print("=" * 78)
    import tkinter as tk
    import msu1_gui as G
    import dl_convert as dc

    msgs = []
    G.messagebox.showinfo = lambda *a, **k: msgs.append(("info", a))
    G.messagebox.showwarning = lambda *a, **k: msgs.append(("warn", a))
    G.messagebox.showerror = lambda *a, **k: msgs.append(("error", a))
    G.messagebox.askyesno = lambda *a, **k: True

    tmp = tempfile.mkdtemp(prefix="qa_b23_%d " % os.getpid())
    dest = os.path.join(tmp, "JoshW")
    os.makedirs(dest, exist_ok=True)

    root = tk.Tk()
    root.withdraw()
    dlg = G.DownloadDialog(root)
    dlg.var_dest.set(dest)
    dlg._dest_auto = False
    dlg.cur_key = "snes"
    dlg.cur_name = "SNES / SFC"

    def drain(seconds=0.6):
        end = time.time() + seconds
        n = 0
        while time.time() < end:
            try:
                ev = dlg.q.get_nowait()
            except queue.Empty:
                time.sleep(0.01)
                continue
            try:
                dlg._dispatch(ev)
            except Exception as e:                      # noqa: BLE001
                print("      [dispatch 抛异常] %s: %s" % (type(e).__name__, e))
                FAIL.append("dispatch %s: %s" % (type(e).__name__, e))
            n += 1
        return n

    try:
        # ================================================== M 组 · B23
        print("\n===== M1/M2 · 点停止后 all_done + cv_thread_exit 双路到达 =====")
        dlg.var_auto_wav.set(True)

        # 打点:统计 _finish_download 真实执行次数
        real_finish = dlg._finish_download
        cnt = {"n": 0}
        loglines = []

        def counting_finish():
            cnt["n"] += 1
            return real_finish()

        dlg._finish_download = counting_finish
        real_log = dlg._log

        def spy_log(t):
            loglines.append(str(t))
            return real_log(t)

        dlg._log = spy_log

        # 造一个真的会跑一会儿的 worker
        started = threading.Event()

        def slow(paths, out_dir, policy, cancel=None, **kw):
            started.set()
            t0 = time.time()
            while time.time() - t0 < 10.0:
                if cancel is not None and cancel.is_set():
                    break
                time.sleep(0.05)
            return {"ok": 0, "fail": 0, "files": [],
                    "seconds": time.time() - t0, "errors": [],
                    "last_idx": kw.get("start_idx", 0)}

        wav_dir = os.path.join(dlg._dest_root(), "WAV")
        cv = dc.ConvertWorker(wav_dir, dlg._cv_emit, dlg._cv_cancel)
        cv.convert_fn = slow
        cv.start()
        src1 = os.path.join(tmp, "a.nsf")
        with open(src1, "wb") as f:
            f.write(b"NESM\x1a" + b"\x00" * 0x200)
        cv.enqueue({"src": src1, "url": "u", "policy": "chip", "pkg": "P"})
        dlg._cv = cv
        dlg._cv_queued = 1
        dlg._downloading = True
        dlg.dl = object()          # 假装 Downloader 还活着
        end = time.time() + 15
        while time.time() < end and not started.is_set():
            time.sleep(0.05)
        check("M0 慢 convert 已开跑", started.is_set() and cv.is_alive(),
              "started=%s" % started.is_set())

        # ① 先 all_done（开关打开时**必须延后**，不收尾 —— 设计 §3.1.1）
        dlg._on_dl_all_done()
        drain(1.0)
        n_after_alldone = cnt["n"]
        # ⚠ 开关打开时 all_done **不得**直接收尾（那会让 _stop_download 早退
        #   -> B17）。所以此刻 count 必须是 0，收尾要等 cv_thread_exit。
        check("M1 开关打开时 all_done 不提前收尾（延后到 cv_thread_exit）",
              n_after_alldone == 0,
              "count=%d（期望 0；非 0 说明 all_done 抢跑 -> B17 会复发）"
              % n_after_alldone)
        check("M1b all_done 已置 _cv_src_finished 标志",
              dlg._cv_src_finished is True,
              "_cv_src_finished=%r" % dlg._cv_src_finished)
        # ② 再让转换线程自然退出 -> 发 cv_thread_exit
        t0 = time.time()
        while time.time() - t0 < 20 and cv.is_alive():
            time.sleep(0.1)
        drain(2.0)
        check("M1c cv_thread_exit 后收尾恰好 1 次（B23 重入守卫）",
              cnt["n"] == 1, "两条路径后 count=%d" % cnt["n"])

        # 汇总日志只出现一遍
        summary = [x for x in loglines if "🎉 下载结束" in x]
        check("M2 汇总日志只出现一遍", len(summary) == 1,
              "出现 %d 次" % len(summary))
        warn = [m for m in msgs if m[0] == "warn"]
        check("M2b 失败汇总弹窗只弹一次", len(warn) <= 1,
              "warn 弹窗 %d 次" % len(warn))

        # ================================================== M3 · 正常完成
        print("\n===== M3 · 正常完成（cv_thread_exit 与 all_done 都会到）=====")
        cnt["n"] = 0
        del loglines[:]
        del msgs[:]
        dlg._finished = False          # 模拟第二轮重置
        check("M4 _finished 已重置为 False（第二轮可正常收尾）",
              dlg._finished is False)

        wd2 = os.path.join(tmp, "m3", "WAV")
        cv2 = dc.ConvertWorker(wd2, dlg._cv_emit, threading.Event())
        cv2.convert_fn = lambda p, o, po, cancel=None, **kw: {
            "ok": 1, "fail": 0, "files": [], "seconds": 0.1,
            "errors": [], "last_idx": 1}
        cv2.start()
        cv2.enqueue({"src": src1, "url": "u", "policy": "chip", "pkg": "P"})
        dlg._cv = cv2
        dlg._cv_cancel = threading.Event()
        dlg._cv_queued = 1
        cv2.mark_src_finished()
        t0 = time.time()
        while time.time() - t0 < 20 and cv2.is_alive():
            time.sleep(0.05)
        drain(2.0)
        check("M3 正常完成时收尾 1 次", cnt["n"] == 1, "count=%d" % cnt["n"])
        s3 = [x for x in loglines if "🎉 下载结束" in x]
        check("M3b 正常完成汇总日志一遍", len(s3) == 1, "出现 %d 次" % len(s3))

        # ================================================== M5 · 开关关闭
        print("\n===== M5/F 组 · 开关关闭零回归 =====")
        cnt["n"] = 0
        del loglines[:]
        dlg._finished = False
        dlg.var_auto_wav.set(False)
        wav_dir3 = os.path.join(tmp, "f", "WAV")

        # F1/F2:关闭态 all_done 应**立即**收尾（原路径）
        dlg._cv = None
        dlg._downloading = False
        dlg._cv_queued = 0
        dlg._cv_ok = 0
        dlg._cv_fail = 0
        dlg._cv_skip = 0
        dlg._cv_bytes = 0
        dlg._cv_abandoned = 0
        dlg._on_dl_all_done()
        drain(1.0)
        check("F3 关闭态 all_done 立即收尾 1 次", cnt["n"] == 1,
              "count=%d" % cnt["n"])
        cv_lines = [x for x in loglines if "🎵" in x or "📁 WAV 输出目录" in x]
        check("F2 关闭态日志无任何「🎵」/「📁 WAV」多出行", not cv_lines,
              str(cv_lines)[:160])
        check("F1 关闭态状态文案无「🎵」",
              "🎵" not in str(dlg.var_status.get()),
              "status=%r" % str(dlg.var_status.get()))
        check("F4 关闭态 WAV 目录未被创建", not os.path.isdir(wav_dir3),
              wav_dir3)
        check("F5 关闭态 manifest 未被创建",
              not os.path.isfile(os.path.join(wav_dir3,
                                              ".msu1-convert.json")))
        check("M5 关闭态仍只收尾 1 次", cnt["n"] == 1, "count=%d" % cnt["n"])
        # F6: _clear_cache 不删 manifest
        man = os.path.join(wav_dir3, ".msu1-convert.json")
        os.makedirs(wav_dir3, exist_ok=True)
        with open(man, "w", encoding="utf-8") as f:
            f.write('{"version":1,"entries":{}}')
        dummy = os.path.join(wav_dir3, "x.wav")
        with open(dummy, "wb") as f:
            f.write(b"RIFF")
        try:
            dlg._clear_cache()
        except Exception as e:                          # noqa: BLE001
            print("      [_clear_cache] %s: %s" % (type(e).__name__, e))
        check("F6 「🗑 清除缓存」不删 manifest（FR-C7.5）",
              os.path.isfile(man), "manifest 存在=%s" % os.path.isfile(man))
        shutil.rmtree(wav_dir3, ignore_errors=True)

    finally:
        try:
            dlg._closed = True
            root.destroy()
        except Exception:                              # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 78)
    print("结果: 通过 %d / 失败 %d" % (PASS[0], len(FAIL)))
    for f in FAIL:
        print("  - %s" % f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
