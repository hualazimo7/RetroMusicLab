# -*- coding: utf-8 -*-
"""QA 独立验收 · B20 / B21 独立差分测试（停止信号够不够得到）

设计铁律（验收要求第 6 条）:判断「停止是否生效」**只能用落盘 WAV 数**
（``len(glob(out_dir/*.wav))``），**不能**断言 ok/fail/errors ——
因为实测两种 composite 的返回值完全一样（都是 ok=0）。

做法与实现者的 H4 同源但**独立实现**: 造一个「先写一轨再查 cancel」的假
convert（复刻 write_tracks 的真实行为），分别喂两种 cancel:

    正法 _CancelPair(全局 Event, 本文件 Event)   -> 期望落盘 0
    错法 裸本文件 Event（与全局无关）            -> 期望落盘 1（B20 症状）

若错法也落盘 0，则本用例无鉴别力，必须换更狠的假 convert。

B20 专项: 停止置位后，**1s 内**当前 convert 必须返回（cancel 够得到）
B21 专项: 停止落在「已出队、还没进 convert」的空窗期 -> 落盘必须 0

用法: C:/Python314/python.exe vendor/_qa_b20_b21_diff.py
"""
import glob
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dl_convert as dc  # noqa: E402

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


def n_wavs(d):
    return len(glob.glob(os.path.join(d, "*.wav")))


def make_src(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"NESM" + b"\x1a" + b"\x00" * 0x200)
    return path


def land_or_stop(paths, out_dir, policy, cancel=None, **kw):
    """复刻 write_tracks: 先落第一轨, 之后才检查 cancel。

    ⚠ 关键: cancel 未置位就**真的写一个 WAV**。这样「停不掉」会体现为
    落盘 1 个, 而返回值两种实现都是 ok=0 —— 只能靠落盘数分辨。
    """
    if cancel is None or not cancel.is_set():
        os.makedirs(out_dir, exist_ok=True)
        idx = int(kw.get("start_idx", 0) or 0) + 1
        p = os.path.join(out_dir, "%02d_Ghost.wav" % idx)
        with open(p, "wb") as fh:
            fh.write(b"RIFF" + b"\x00" * 2048)
        return {"ok": 1, "fail": 0, "files": [p], "seconds": 0.1,
                "errors": [], "last_idx": idx}
    return {"ok": 0, "fail": 0, "files": [], "seconds": 0.0,
            "errors": [], "last_idx": kw.get("start_idx", 0)}


def main():
    print("=" * 78)
    print("QA 独立验收 · B20/B21 停止信号差分测试  pid=%d" % os.getpid())
    print("=" * 78)
    tmp = tempfile.mkdtemp(prefix="qa_b20b21_%d " % os.getpid())
    try:
        # ============================================ B21 · 空窗期差分
        print("\n===== B21 · 空窗期（已出队、未进 convert）=====")
        results = {}
        for label, correct in (("正法(复合含全局)", True),
                               ("错法(只传job_cancel)", False)):
            wd = os.path.join(tmp, "b21_%s" % ("ok" if correct else "bad"),
                              "WAV")
            os.makedirs(wd, exist_ok=True)
            glob_ev = threading.Event()
            in_window = threading.Event()
            release = threading.Event()

            def fake(paths, out_dir, policy, cancel=None, _c=correct, **kw):
                cc = (dc._CancelPair(glob_ev, threading.Event()) if _c
                      else threading.Event())
                return land_or_stop(paths, out_dir, policy, cancel=cc, **kw)

            w = dc.ConvertWorker(wd, lambda *a: None, glob_ev)
            w.convert_fn = fake
            orig_est = w._estimate_bytes

            def gated(src, _o=orig_est, _w=in_window, _r=release):
                _w.set()            # 此刻：任务已出队、convert 还没被调
                _r.wait(20)         # 卡住等主线程投停止信号
                return _o(src)

            w._estimate_bytes = gated
            w.start()
            w.enqueue({"src": make_src(os.path.join(tmp, "src",
                                                    "w%s.nsf" % correct)),
                       "url": "u", "policy": "chip"})
            w.mark_src_finished()
            end = time.time() + 20
            while time.time() < end and not in_window.is_set():
                time.sleep(0.01)
            check("B21 空窗期已构造（任务出队/convert 未调）[%s]" % label,
                  in_window.is_set())
            glob_ev.set()           # ← 用户此刻点「■ 停止」
            release.set()
            w.wait(20)
            results[label] = n_wavs(wd)
            print("      %-24s 落盘 %d 个" % (label, results[label]))

        check("B21 正法（复合含全局）空窗期停止 -> 落盘 0 个",
              results["正法(复合含全局)"] == 0,
              "落盘 %d 个" % results["正法(复合含全局)"])
        check("B21 差分成立：错法落盘 1 个（证明用例有鉴别力）",
              results["错法(只传job_cancel)"] == 1,
              "错法落盘 %d 个（若也是 0，本用例无鉴别力）"
              % results["错法(只传job_cancel)"])

        # ============================================ B20 · 够得到当前文件
        print("\n===== B20 · 停止够得到「正在转」的文件 =====")
        wd = os.path.join(tmp, "b20", "WAV")
        os.makedirs(wd, exist_ok=True)
        entered = threading.Event()

        def slow(paths, out_dir, policy, cancel=None, **kw):
            """慢 convert: 每 50ms 查一次 cancel, 最多 300s（复刻超时上限）。"""
            entered.set()
            t0 = time.time()
            while time.time() - t0 < 300.0:
                if cancel is not None and cancel.is_set():
                    break
                time.sleep(0.05)
            return {"ok": 0, "fail": 0, "files": [],
                    "seconds": time.time() - t0, "errors": [],
                    "last_idx": kw.get("start_idx", 0)}

        w = dc.ConvertWorker(wd, lambda *a: None, threading.Event())
        w.convert_fn = slow
        w.start()
        w.enqueue({"src": make_src(os.path.join(tmp, "src", "hang.nsf")),
                   "url": "u", "policy": "chip"})
        w.mark_src_finished()
        end = time.time() + 20
        while time.time() < end and not entered.is_set():
            time.sleep(0.02)
        check("B20 慢文件已在转", entered.is_set() and w.is_alive(),
              "entered=%s alive=%s" % (entered.is_set(), w.is_alive()))
        cur = w.current() or {}
        check("B20 _cur 已挂上且含 per-file cancel",
              bool(cur) and hasattr(cur.get("cancel"), "is_set"),
              "cur keys=%s" % sorted(cur.keys()))
        check("B20 置位前 per-file cancel 未取消",
              not cur.get("cancel").is_set())
        t0 = time.time()
        w.cancel()
        dt = time.time() - t0
        check("B20 cancel() 立即返回（<1s，不 join）", dt < 1.0, "%.3fs" % dt)
        check("B20 cancel() 连带置位 _cur['cancel']（三段联动）",
              cur.get("cancel").is_set(),
              "is_set=%s" % cur.get("cancel").is_set())
        t1 = time.time()
        while time.time() - t1 < 10 and w.is_alive():
            time.sleep(0.05)
        quit_dt = time.time() - t0
        check("B20 线程 ≤10s 内退出（当前文件真被中止）",
              not w.is_alive(), "用时 %.2fs" % quit_dt)
        check("B20 停止后落盘 0 个（判据是落盘数不是返回值）",
              n_wavs(wd) == 0, "落盘 %d 个" % n_wavs(wd))

        # ============================================ _CancelPair 单元
        print("\n===== H3 · _CancelPair 单元 =====")
        a, b = threading.Event(), threading.Event()
        pair = dc._CancelPair(a, b)
        check("H3 都不置 -> 未取消", not pair.is_set())
        a.set()
        check("H3 只置全局 -> 取消", pair.is_set())
        a.clear()
        check("H3 全局清掉 -> 未取消", not pair.is_set())
        b.set()
        check("H3 只置本文件 -> 取消（空窗期防线）", pair.is_set())
        b.clear()
        check("H3 都清 -> 未取消", not pair.is_set())
        check("H3 不是 threading.Event 继承（鸭子类型即可）",
              not isinstance(pair, threading.Event),
              "type=%s" % type(pair).__mro__[1].__name__)

        # ============================================ _cur=None 不抛
        print("\n===== H5 · _cur 为 None 时 cancel 不抛 =====")
        w3 = dc.ConvertWorker(os.path.join(tmp, "h5", "WAV"),
                              lambda *a: None, threading.Event())
        w3.convert_fn = land_or_stop
        w3._cur = None
        try:
            w3.cancel()
            check("H5 _cur=None 时 cancel 不抛", True)
        except Exception as e:                          # noqa: BLE001
            check("H5 _cur=None 时 cancel 不抛", False,
                  "%s: %s" % (type(e).__name__, e))
        check("H5 _cur=None 时全局 cancel 仍被置位",
              w3._cancel.is_set(), "_cancel=%s" % w3._cancel.is_set())

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 78)
    print("结果: 通过 %d / 失败 %d" % (PASS[0], len(FAIL)))
    for f in FAIL:
        print("  - %s" % f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
