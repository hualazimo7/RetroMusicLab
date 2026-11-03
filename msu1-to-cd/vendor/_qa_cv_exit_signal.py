# -*- coding: utf-8 -*-
"""QA 独立验收 · cv_thread_exit 收尾信号无条件发出（工程师 finally 兜底）

背景：工程师在 ``dl_convert.run()`` 的 finally 加了 stats() 兜底
（``dl_convert.py:935-941``），理由是**cv_thread_exit 是 GUI 唯一的收尾信号**。

这个风险等级比「错误处理没测」更高：
    若最外层 except 之后 cv_thread_exit 丢了-> _handle_cv 收不到
    -> _finish_download() 不调 -> 按钮锁死、进度条不动 -> **用户只能杀进程**。

工程师已在他的 [12] 组里测过。本脚本是**独立复核**（不信任他的断言），
并额外覆盖他可能没测到的角度：

  T1 正常路径: cv_thread_exit 恰好 1 次（基线）
  T2 stats() 抛异常 -> cv_thread_exit **仍发出**（兜底生效）
  T3 stats() 抛异常 -> 线程仍退出、不挂死
  T4 外层except（_estimate_bytes 抛）-> cv_thread_exit 仍发出
  T5 兜底 payload 结构完整（GUI 按固定键读，少键会让统计口径不一致）
  T6 收尾信号丢失的可观测后果（模拟 GUI：没收到就不调 _finish_download）

用法: C:/Python314/python.exe vendor/_qa_cv_exit_signal.py
"""
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


class Collector(object):
    def __init__(self):
        self.ev = []
        self._lk = threading.Lock()
        self._cv = threading.Condition(self._lk)

    def __call__(self, kind, payload):
        with self._cv:
            self.ev.append((kind, dict(payload or {})))
            self._cv.notify_all()

    def of(self, kind):
        with self._lk:
            return [p for k, p in self.ev if k == kind]

    def kinds(self):
        with self._lk:
            return set(k for k, _ in self.ev)

    def wait_for(self, kind, timeout=60.0):
        end = time.time() + timeout
        with self._cv:
            while True:
                for k, p in self.ev:
                    if k == kind:
                        return p
                left = end - time.time()
                if left <= 0:
                    return None
                self._cv.wait(min(left, 0.2))


def make_src(path, data=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data or (b"NESM\x1a" + b"\x00" * 0x200))
    return path


def ok_convert(paths, out_dir, policy, cancel=None, **kw):
    return {"ok": 1, "fail": 0, "files": [], "seconds": 0.1,
            "errors": [], "last_idx": 1}


def main():
    print("=" * 78)
    print("QA 独立验收 · cv_thread_exit 收尾信号  pid=%d" % os.getpid())
    print("=" * 78)
    tmp = tempfile.mkdtemp(prefix="qa_cvexit_%d " % os.getpid())
    try:
        # ---------------------------------------------------------- T1
        print("\n===== T1 · 正常路径基线 =====")
        col = Collector()
        wd = os.path.join(tmp, "t1", "WAV")
        w = dc.ConvertWorker(wd, col, threading.Event())
        w.convert_fn = ok_convert
        w.start()
        w.enqueue({"src": make_src(os.path.join(tmp, "src", "a.nsf")),
                   "url": "u", "policy": "chip", "pkg": "P"})
        w.mark_src_finished()
        exit_p = col.wait_for("cv_thread_exit", 60)
        w.wait(20)
        check("T1 正常路径发 cv_thread_exit", exit_p is not None,
              "kinds=%s" % sorted(col.kinds()))
        check("T1b cv_thread_exit 恰好 1 次",
              len(col.of("cv_thread_exit")) == 1,
              "%d 次" % len(col.of("cv_thread_exit")))
        check("T1c 线程退出", not w.is_alive())

        # ---------------------------------------------------------- T2/T3
        print("\n===== T2/T3 · stats() 抛异常 -> 收尾信号仍发出（工程师的兜底）=====")
        col2 = Collector()
        wd2 = os.path.join(tmp, "t2", "WAV")
        w2 = dc.ConvertWorker(wd2, col2, threading.Event())
        w2.convert_fn = ok_convert

        def boom_stats():
            raise RuntimeError("注入：stats() 崩溃")

        w2.stats = boom_stats          # 故障注入
        w2.start()
        w2.enqueue({"src": make_src(os.path.join(tmp, "src", "b.nsf")),
                    "url": "u", "policy": "chip", "pkg": "P"})
        w2.mark_src_finished()
        exit2 = col2.wait_for("cv_thread_exit", 60)
        w2.wait(20)
        check("T2 stats() 崩溃时 cv_thread_exit **仍发出**",
              exit2 is not None,
              "kinds=%s（若缺 cv_thread_exit -> GUI 会永久卡死）"
              % sorted(col2.kinds()))
        check("T2b 兜底路径下线程仍退出、不挂死",
              not w2.is_alive(), "alive=%s" % w2.is_alive())
        check("T2c 兜底 payload 含 GUI 要读的固定键",
              exit2 is not None and all(
                  k in exit2 for k in ("ok", "fail", "skip", "wavs", "bytes")),
              "keys=%s" % (sorted(exit2.keys()) if exit2 else None))

        # ---------------------------------------------------------- T4
        print("\n===== T4 · 单文件级异常（第三重兜底）-> 收尾信号仍发出 =====")
        # ⚠ 我第一版把这里写成「注入 _estimate_bytes 抛 -> 应记 cv_error」，
        #   实测 cv_error=0。查源码后确认**是我的断言错了**：
        #   `run()` 对每个 job 有**单文件兜底**（dl_convert.py:916-923），
        #   异常被就地转成 `cv_file_failed`。这是**正确的设计**
        #   （一个文件坏不该拖垮整批），所以**根本走不到最外层 except**。
        #   真正能命中 cv_error 的是**队列层/清单层**异常，见 T4b。
        col3 = Collector()
        wd3 = os.path.join(tmp, "t4", "WAV")
        w3 = dc.ConvertWorker(wd3, col3, threading.Event())
        w3.convert_fn = ok_convert

        def boom_est(src):
            raise ValueError("注入：_estimate_bytes 抛")

        w3._estimate_bytes = boom_est
        w3.start()
        w3.enqueue({"src": make_src(os.path.join(tmp, "src", "c.nsf")),
                    "url": "u", "policy": "chip", "pkg": "P"})
        w3.mark_src_finished()
        exit3 = col3.wait_for("cv_thread_exit", 60)
        w3.wait(20)
        check("T4 单文件级异常后 cv_thread_exit 仍发出",
              exit3 is not None, "kinds=%s" % sorted(col3.kinds()))
        check("T4b 单文件级异常记为 cv_file_failed（**不是** cv_error）",
              len(col3.of("cv_file_failed")) >= 1 and
              len(col3.of("cv_error")) == 0,
              "cv_file_failed=%d cv_error=%d（单文件兜底本就该转 file_failed）"
              % (len(col3.of("cv_file_failed")), len(col3.of("cv_error"))))
        check("T4c 线程退出", not w3.is_alive(), "alive=%s" % w3.is_alive())

        # ---------------------------------------------------------- T4d
        print("\n===== T4d · 队列层异常 -> 命中最外层 except（cv_error 真路径）=====")
        col4 = Collector()
        wd4 = os.path.join(tmp, "t4d", "WAV")
        w4 = dc.ConvertWorker(wd4, col4, threading.Event())
        w4.convert_fn = ok_convert
        # _ensure_numbering 在 try 内、循环外 —— 这里抛才会命中最外层 except
        w4._ensure_numbering = lambda: (_ for _ in ()).throw(
            RuntimeError("注入：清单层崩溃"))
        w4.start()
        w4.enqueue({"src": make_src(os.path.join(tmp, "src", "d.nsf")),
                    "url": "u", "policy": "chip", "pkg": "P"})
        w4.mark_src_finished()
        exit4 = col4.wait_for("cv_thread_exit", 60)
        w4.wait(20)
        check("T4d 队列层异常记为 cv_error（不再是死代码）",
              len(col4.of("cv_error")) >= 1,
              "cv_error=%d（0 说明这条路径仍无覆盖）"
              % len(col4.of("cv_error")))
        check("T4e 队列层异常后 cv_thread_exit **仍发出**（GUI 收尾保证）",
              exit4 is not None,
              "kinds=%s（缺 cv_thread_exit =永久卡死）" % sorted(col4.kinds()))
        check("T4f 线程退出、不挂死", not w4.is_alive(),
              "alive=%s" % w4.is_alive())
        if col4.of("cv_error"):
            err = str(col4.of("cv_error")[0].get("error") or "")
            check("T4g cv_error payload 带异常类型与文本",
                  "RuntimeError" in err and "清单层崩溃" in err, err[:120])
            check("T4h cv_error payload 带 'error' 键（GUI 按此键读）",
                  "error" in col4.of("cv_error")[0])

        # ---------------------------------------------------------- T5
        print("\n===== T5 · 收尾信号丢失的可观测后果（模拟 GUI）=====")
        # GUI 逻辑：只有收到 cv_thread_exit 才收尾。丢�� = 永久卡死。
        def simulate_gui(collector, label):
            got = collector.wait_for("cv_thread_exit", 0.5)
            return got is not None

        for colx, label, name in ((col, "正常路径", "t1"),
                                  (col4, "队列层异常", "t4d"),
                                  (col2, "stats()崩溃", "t2"),
                                  (col3, "外层异常", "t4")):
            finished = simulate_gui(colx, label)
            check("T5 %s 下 GUI 能收到收尾信号（不会卡死）" % label,
                  finished,
                  "收到=%s（False 即模拟永久卡死）" % finished)

        # 反向对照：如果信号真丢了，GUI 确实收不到 —— 证明 T5 有鉴别力
        empty = Collector()
        got_empty = simulate_gui(empty, "空 collector")
        check("T5b 反向对照：空 collector 确实收不到（证明 T5 有鉴别力）",
              got_empty is False,
              "收到=%s（若为 True 说明 T5 是永真断言）" % got_empty)
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