# -*- coding: utf-8 -*-
"""QA 独立验收 · AC-C29 内存（用**真实**样本，不合成）

背景：HES 的 ``n_tracks`` 由 chip_formats._probe_hes **恒定取 256**
（解码库一律当256 轨，多出来的空轨靠「首帧峰值 0」跳过）。
所以「256 轨」是 HES 的**设计口径**，不是文件里真有多少轨。

我第一版试图合成一个「256 条有效子通道」的 HES，结果 libgme 解不出任何轨
（落盘 0）—— 量的是「解码失败」的内存，属于**假验证**，已废弃。

本脚本改为：**用真实样本跑完整链路**，记录
  - 实际产出轨数（= 有效轨）
  - 内存峰值
  - 按「有效轨数 -> 峰值内存」拟合，外推 256 轨的内存上界
外推只作参考，结论里明确标注它是外推而非实测。

用法: C:/Python314/python.exe vendor/_qa_ac29_real.py
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


def rss_mb():
    """本进程 RSS（MB）。restype 必须显式设 c_void_p，否则恒返回 0。"""
    import ctypes
    import ctypes.wintypes as wt

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ps = ctypes.WinDLL("psapi", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.GetCurrentProcess.argtypes = []
    ps.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(PMC), wt.DWORD]
    ps.GetProcessMemoryInfo.restype = wt.BOOL
    c = PMC()
    c.cb = ctypes.sizeof(PMC)
    if not ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c),
                                   ctypes.sizeof(PMC)):
        return None
    return c.WorkingSetSize / (1024.0 * 1024.0)


def main():
    print("=" * 78)
    print("QA 独立验收 · AC-C29 内存（真实样本）  pid=%d" % os.getpid())
    print("=" * 78)
    import dl_convert as dc
    import chip_formats as cf

    tmp = tempfile.mkdtemp(prefix="qa_ac29_%d " % os.getpid())
    rows = []
    try:
        base = rss_mb()
        check("RSS 读取有效（>0，否则内存断言是假通过）",
              base is not None and base > 0, "起始 RSS = %s MB" % base)
        print("  起始 RSS = %.0f MB\n" % base)

        # 取所有真实芯片样本，按体积降序（内存压力最大的先跑）
        cands = []
        for sub in sorted(os.listdir(os.path.join(ROOT, "vendor", "samples"))):
            if sub.startswith("_"):
                continue
            sd = os.path.join(ROOT, "vendor", "samples", sub)
            if not os.path.isdir(sd):
                continue
            for f in sorted(os.listdir(sd)):
                p = os.path.join(sd, f)
                if not os.path.isfile(p):
                    continue
                if os.path.splitext(f)[1].lower() in (
                        ".hes", ".nsf", ".gbs", ".vgm", ".kss", ".gym",
                        ".sap", ".ay", ".nsfe", ".spc"):
                    cands.append(p)
        cands.sort(key=lambda p: -os.path.getsize(p))
        print("  候选样本 %d 个，按体积降序取前 6" % len(cands))

        for src in cands[:6]:
            peak = {"mb": 0.0}
            stop = threading.Event()
            ev = []
            lk = threading.Lock()
            wd = os.path.join(tmp, os.path.basename(src)[:24], "WAV")
            reads = {"n": 0, "bad": 0}

            def watch():
                while not stop.is_set():
                    v = rss_mb()
                    reads["n"] += 1
                    if v is None:
                        # ⚠ **不能静默跳过**。若读失败而 peak 停在 0，
                        #   下游 `peak <= 1536` 会**假通过**（0 也<= 1536）。
                        #   这里计数，由「采样有效率」断言兜住。
                        reads["bad"] += 1
                    else:
                        peak["mb"] = max(peak["mb"], v)
                    time.sleep(0.1)

            def on_ev(k, p):
                lk.acquire()
                ev.append((k, p))
                lk.release()

            w = dc.ConvertWorker(wd, on_ev, threading.Event())
            w.CHIP_SECS = 3.0        # 压时长，突出内存
            th = threading.Thread(target=watch, daemon=True)
            th.start()
            t0 = time.time()
            w.start()
            w.enqueue({"src": src, "url": "real", "policy": dc.policy_for(src),
                       "pkg": "R"})
            w.mark_src_finished()
            end = time.time() + 400
            while time.time() < end and w.is_alive():
                time.sleep(0.15)
            w.wait(30)
            stop.set()
            th.join(3)
            dt = time.time() - t0

            n = len(glob.glob(os.path.join(wd, "*.wav")))
            with lk:
                failed = [p for k, p in ev if k == "cv_file_failed"]
            name = os.path.basename(src)
            rows.append((name, n, peak["mb"], dt))
            print("  %-44s 轨=%-4d 峰值=%6.0f MB  %5.1fs %s"
                  % (name[:44], n, peak["mb"], dt,
                     ("FAIL:" + str(failed[0].get("error"))[:40]) if failed
                     else ""))

            # 无临时残留
            junk = [f for _r, _ds, fs in os.walk(os.path.dirname(wd))
                    for f in fs if f.lower().endswith(
                        (".pcm", ".tmp", ".raw", ".part"))]
            check("AC-C29 %s 无临时 PCM 残留" % name[:28], not junk,
                  "残留=%s" % junk[:3])
            # ★ 采样有效率：读失败会被watch 计数，这里断言「几乎全成功」。
            #   若 bad 占比高，peak 就是「没量到」而非「量到很小」——
            #   那是另一种形式的假通过，必须挡住。
            check("AC-C29 %s 内存采样有效（读失败率 <10%%）" % name[:28],
                  reads["n"] > 0 and reads["bad"] <= reads["n"] * 0.1,
                  "采样 %d 次, 失败 %d 次" % (reads["n"], reads["bad"]))

        print()
        ok_rows = [r for r in rows if r[1] > 0]
        check("至少 3 个真实样本成功解码（内存数据有意义）",
              len(ok_rows) >= 3,
              "成功 %d / %d" % (len(ok_rows), len(rows)))

        if ok_rows:
            mx = max(ok_rows, key=lambda r: r[2])
            print("\n  峰值最高: %s -> %.0f MB（%d 轨）"
                  % (mx[0], mx[2], mx[1]))
            # ⚠ 必须同时要求 peak > 0：peak=0 只可能是「没量到」，
            #   0 <= 1536 会假通过。
            check("AC-C29 实测内存峰值 <= 1.5 GB（且非零，防假通过）",
                  0 < mx[2] <= 1536.0,
                  "最高 %.0f MB, 门槛 1536 MB" % mx[2], "AC-C29")

            #按「每轨内存」外推 256 轨（**仅供参考，非实测**）
            # ⚠⚠ 公式必须是 **(peak - base) / tracks**，不是 peak / tracks。
            #   peak 里含~37MB 的**解释器 + DLL 基线**，那是与轨数无关的固定开销。
            #   写成 peak/tracks 会把基线摊到每一轨上 -> 每轨被高估几十倍
            #   （第一版正因此报出 696MB，已修正为 132MB）。
            delta = mx[2] - base
            per = max(delta, 0.0) / max(mx[1], 1)
            ext = base + per * 256
            print("  外推(参考): (峰值 %.0f - 基线 %.0f) / %d 轨 = %.3f MB/轨"
                  % (mx[2], base, mx[1], per))
            print("外推 256 轨≈ %.0f MB（门槛 1536 MB，余量 %.1f倍）"
                  % (ext, 1536.0 / ext if ext else 0))
            print("  ⚠ 此为**线性外推、非实测**。隐含假设「每轨内存线性」，"
                  "若解码器有非线性策略（整块预读 / run 切分随轨数变化）会低估。")
            print("  ⚠ 本机无法构造 256 有效轨样本：libgme 对 HES **恒按 256 轨处理**"
                  "（空轨靠首帧峰值 0 识别），本地最多 %d 有效轨，"
                  "合成文件解不出音轨。实测上限 = %d 有效轨。" % (mx[1], mx[1]))
            # 保守侧：即使每轨成本再高一个数量级，也仍在门槛内
            check("AC-C29 外推保守侧（每轨成本 x10）仍 <= 1.5 GB",
                  base + per * 10 * 256 <= 1536.0,
                  "保守外推 = %.0f MB（门槛 1536 MB）"
                  % (base + per * 10 * 256), "AC-C29/非实测")
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
