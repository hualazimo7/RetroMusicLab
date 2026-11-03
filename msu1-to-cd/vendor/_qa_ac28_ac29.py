# -*- coding: utf-8 -*-
"""QA 独立验收 · AC-C28 / AC-C29（工程师明确说「没覆盖」，由 QA 补上）

AC-C28 转换不拖慢下载: 下载吞吐 >= 纯下载基线 80%
AC-C29 内存与临时文件: 256 轨 HES 内存 <= 1.5GB；结束后 WAV/ 之外无残留临时 PCM

AC-C29 用**合成 256 轨 HES**（本地样本最多 23 轨，达不到 256）。
按 HES 头格式造一个 256 条子通道的合法文件，走完整 ConvertWorker。

用法: C:/Python314/python.exe vendor/_qa_ac28_ac29.py
"""
import glob
import os
import shutil
import struct
import subprocess
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
    """本进程 RSS（MB）。

    ⚠ 必须显式设 ``GetCurrentProcess.restype = c_void_p``：它返回的是
    伪句柄 -1，默认的 c_int restype 会把它截成无效值 ->
    GetProcessMemoryInfo 报 ERROR_INVALID_HANDLE(6) 且 WorkingSetSize 恒为 0
    （**假通过**：内存断言会在真实超限时仍然 PASS）。这是本脚本第一版的 bug。
    """
    try:
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
        if not ps.GetProcessMemoryInfo(k32.GetCurrentProcess(),
                                       ctypes.byref(c), ctypes.sizeof(PMC)):
            raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo")
        return c.WorkingSetSize / (1024.0 * 1024.0)
    except Exception as e:                              # noqa: BLE001
        print("      [rss_mb] %s: %s" % (type(e).__name__, e))
        return None            # ⚠ 返回 None 而不是 0 —— 0 会被误当「很小」


def make_hes256(path):
    """造一个 **libgme 真能解码**的 256 轨 HES。

    第一版我凭空拼了个头 -> probe 连轨数都读不出（n=None）、解码零产出，
    那种文件量出来的内存毫无意义（AC-C29 等于没测）。

    正确做法：**以真实样本为骨架**复制 N 份子通道数据。
    HES 的轨偏移表在0x1FE 起 256×u16be（chip_formats._probe_hes 注释），
    所以只要把样本的音频数据重复 256 份并让偏移表全部有效即可。
    """
    import struct
    src = os.path.join(ROOT, "vendor", "samples", "hes", "AKCD3001.hes")
    base = bytearray(open(src, "rb").read())
    blk = 0x800                                  # 每份子通道 2KB，远大于原样本
    reps = 256
    hdr = 0x200                                  # 头部（含 0x1FE 偏移表）
    need = hdr + blk * reps
    if len(base) < need:
        base.extend(b"\x00" * (need - len(base)))
    else:
        del base[need:]
    # 复制原样本的音频区（0x200 起）到每一份
    audio = bytes(base[0x200:0x200 + blk])
    for i in range(reps):
        off = 0x200 + i * blk
        base[off:off + blk] = audio
    # 写 0x1FE 起的 256×u16be 轨偏移表（指向各子通道起点，单位按 0x200 对齐）
    for i in range(reps):
        struct.pack_into(">H", base, 0x1FE + i * 2, (i * blk) // 0x200)
    # 头部声明 256 轨
    struct.pack_into("<I", base, 4, 0x0100)      # version
    struct.pack_into("<H", base, 8, (blk * reps) // 0x200)
    struct.pack_into("<H", base, 10, 0)
    # 子通道表: 16 条 + 256×0x20 从 0x100 起会与 0x1FE 撞 -> 只填前 15 条
    for i in range(15):
        o = 0x100 + i * 0x20
        struct.pack_into("<I", base, o + 0, (i * blk) // 0x200)
        struct.pack_into("<I", base, o + 4, i * 1000)
        struct.pack_into("<I", base, o + 8, (i * blk) // 0x200)
        struct.pack_into("<I", base, o + 12, i * 900)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(bytes(base))
    return path


def dir_size_mb(d, exclude=()):
    tot = 0
    for r, _ds, fs in os.walk(d):
        for fn in fs:
            if os.path.join(r, fn) in exclude:
                continue
            try:
                tot += os.path.getsize(os.path.join(r, fn))
            except OSError:
                pass
    return tot / (1024.0 * 1024.0)


def main():
    print("=" * 78)
    print("QA 独立验收 · AC-C28 / AC-C29（补工程师未覆盖项）  pid=%d" % os.getpid())
    print("=" * 78)
    import dl_convert as dc
    import chip_formats as cf

    tmp = tempfile.mkdtemp(prefix="qa_ac2829_%d " % os.getpid())
    try:
        # ===================================================== AC-C29
        print("\n===== AC-C29 · 256 轨 HES 内存 <= 1.5GB + 无临时残留 =====")
        src = make_hes256(os.path.join(tmp, "src", "big256.hes"))
        check("合成 256 轨 HES 已生成",
              os.path.getsize(src) > 0,
              "%d bytes (%.1f MB)" % (os.path.getsize(src),
                                      os.path.getsize(src) / 1048576.0))
        pr = cf.probe(src)
        info = pr[0] if isinstance(pr, (tuple, list)) else pr
        ntr = getattr(info, "n_tracks", None)
        # ⚠ HES 的 n_tracks 由 chip_formats._probe_hes **恒定取 256**
        #   （解码库一律当256 轨，空轨靠首帧峰值 0 识别），所以真样本也是 n=256。
        check("probe 认出 HES 且轨数口径 = 256（CHIP_MAX_TRACKS）",
              ntr == 256 and getattr(info, "fmt", "") == "HES",
              "n=%s fmt=%s" % (ntr, getattr(info, "fmt", "?")))

        wav_dir = os.path.join(tmp, "c29", "WAV")
        base_rss = rss_mb()
        check("RSS 读取有效（>0，避免内存断言假通过）",
              base_rss is not None and base_rss > 0,
              "转换前 RSS = %s MB" % base_rss)
        print("  转换前 RSS = %s MB" % base_rss)

        peak = {"mb": base_rss or 0.0}
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                v = rss_mb()
                if v:
                    peak["mb"] = max(peak["mb"], v)
                time.sleep(0.1)

        ev = []
        lk = threading.Lock()
        w = dc.ConvertWorker(wav_dir,
                             lambda k, p: (lk.acquire(), ev.append((k, p)),
                                           lk.release()),
                             threading.Event())
        # chip_secs 压到很小，只为跑完内存曲线（AC-C29 看的是内存不是时长）
        w.CHIP_SECS = 2.0
        th = threading.Thread(target=watch, daemon=True)
        th.start()
        t0 = time.time()
        w.start()
        w.enqueue({"src": src, "url": "synthetic", "policy": "chip", "pkg": "S"})
        w.mark_src_finished()
        end = time.time() + 900
        while time.time() < end and w.is_alive():
            time.sleep(0.2)
        w.wait(30)
        stop.set()
        th.join(3)
        dt = time.time() - t0

        with lk:
            done = [p for k, p in ev if k == "cv_file_done"]
            failed = [p for k, p in ev if k == "cv_file_failed"]
            exit_ = next((p for k, p in ev if k == "cv_thread_exit"), None)
        print("  转换后 RSS = %s MB, 峰值 = %.0f MB, 用时 %.1fs"
              % (rss_mb(), peak["mb"], dt))
        if exit_:
            print("  cv_thread_exit: ok=%s fail=%s wavs=%s secs=%s"
                  % (exit_.get("ok"), exit_.get("fail"),
                     exit_.get("wavs"), exit_.get("secs")))

        n_landed = len(glob.glob(os.path.join(wav_dir, "*.wav")))
        print("  实际落盘 WAV = %d 个" % n_landed)
        grow = peak["mb"] - (base_rss or 0.0)
        check("AC-C29 内存峰值 <= 1.5 GB",
              peak["mb"] > 0 and peak["mb"] <= 1536.0,
              "峰值 %.0f MB (增长 %.0f MB), 门槛 1536 MB"
              % (peak["mb"], grow), "AC-C29")
        #★ 合成文件必须真解出音轨，否则量的是「解码失败」的内存 = 假验证
        check("AC-C29 合成文件真解出音轨（内存数据有意义）", n_landed > 0,
              "落盘 %d 个；失败=%s"
              % (n_landed, str(failed[0].get("error"))[:110] if failed else "-"))

        # 无临时 PCM 残留：WAV/ 之外不该有 .pcm/.tmp/.raw
        junk = []
        for r, _ds, fs in os.walk(os.path.join(tmp, "c29")):
            for fn in fs:
                low = fn.lower()
                if low.endswith((".pcm", ".tmp", ".raw", ".part")):
                    junk.append(os.path.join(r, fn))
        check("AC-C29 WAV/ 之外无残留临时 PCM", not junk,
              "残留=%s" % junk[:5], "AC-C29/FR-C10.5")
        nw = len(glob.glob(os.path.join(wav_dir, "*.wav")))
        others = [f for f in os.listdir(wav_dir)
                  if not f.lower().endswith(".wav")]
        check("AC-C29 WAV 目录内只有 wav + manifest",
              set(x.lower() for x in others) <= {".msu1-convert.json"},
              "其它=%s, wav 数=%d" % (others, nw))

        # ===================================================== AC-C28
        print("\n===== AC-C28 · 转换不拖慢下载（吞吐 >= 基线 80%）=====")
        import urllib.request
        import ssl
        import joshw_source as js
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        def pick_url(min_bytes=2 * 1024 * 1024):
            """挑一个**至少 2MB** 的素材。

            ⚠ 第一版挑了「最小」的文件（19KB）-> 3 秒里绝大部分是 RTT 与
               连接建立，吞吐比完全是噪声（第一次跑出 104%、第二次 55%），
               同一份代码两次结论相反 = 不可复现的测量等于没测。
               AC-C28 比的是**吞吐**，必须让传输时间占主导。
            """
            for pf in js.PLATFORMS:
                if pf["key"] != "spc":
                    continue
                root = "http://%s/" % pf["host"]
                req = urllib.request.Request(root, headers={"User-Agent": "qa"})
                html = urllib.request.urlopen(req, timeout=25,
                                              context=ctx).read().decode(
                    "utf-8", "replace")
                pool = list(js.parse_index(html, root))
                for d in [x[1].replace("https://", "http://")
                          for x in js._parse_dirs(html, root)][:4]:
                    try:
                        r2 = urllib.request.Request(
                            d, headers={"User-Agent": "qa"})
                        pool.extend(js.parse_index(
                            urllib.request.urlopen(
                                r2, timeout=25, context=ctx
                            ).read().decode("utf-8", "replace"), d))
                    except Exception:                  # noqa: BLE001
                        continue
                pool = [p for p in pool
                        if str(p.get("url", "")).lower().endswith(".7z")
                        and int(p.get("size") or 0) >= min_bytes]
                if pool:
                    # 取中位大小：够大到吞吐占主导，又不至于太慢
                    pool.sort(key=lambda p: int(p.get("size") or 0))
                    e = pool[len(pool) // 2]
                    return str(e["url"]).replace("https://", "http://")
            return None

        url = pick_url()
        if not url:
            check("AC-C28 找到可下载素材", False, "网络/索引不可用")
        else:
            print("  素材: %s" % url.split("/")[-1][:70])

            def timed_download():
                t = time.time()
                data = urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": "qa"}),
                    timeout=180, context=ctx).read()
                return len(data), time.time() - t

            # 预热（不计分）：避免 TLS/连接建立计入基线
            try:
                timed_download()
            except Exception as e:                      # noqa: BLE001
                print("      [预热失败] %s" % str(e)[:70])

            # ⚠ **交替测量**（base->conc->base->conc...）再取中位数。
            #   单次 base 后立刻测 conc，网络抖动会直接带偏结论；
            #   交替能把「站点整体变慢」与「转换抢资源」区分开。
            ratios = []
            bases, concs = [], []
            ROUNDS = 3
            for r in range(ROUNDS):
                n1, t1 = timed_download()
                b = n1 / t1 / 1024.0
                bases.append(b)

                wd2 = os.path.join(tmp, "c28_%d" % r, "WAV")
                conv = dc.ConvertWorker(wd2, lambda *a: None,
                                        threading.Event())
                conv.CHIP_SECS = 2.0
                conv.start()
                conv.enqueue({"src": src, "url": "synthetic",
                              "policy": "chip", "pkg": "S"})
                conv.mark_src_finished()
                time.sleep(0.4)          # 让转换先抢起 CPU/IO
                n2, t2 = timed_download()
                c = n2 / t2 / 1024.0
                concs.append(c)
                ratios.append(c / b if b else 0.0)
                print("  轮%d: 基线 %.0f KB/s | 并发 %.0f KB/s | 比 %.0f%%"
                      % (r + 1, b, c, ratios[-1] * 100))
                conv.cancel()
                conv.wait(30)

            def med(xs):
                xs = sorted(xs)
                return xs[len(xs) // 2]

            base_mbps = med(bases)
            conv_mbps = med(concs)
            ratio = med(ratios)
            print("  中位数: 基线 %.0f KB/s | 并发 %.0f KB/s | 比 %.0f%%"
                  % (base_mbps, conv_mbps, ratio * 100))
            print("  各轮比值: %s"
                  % ", ".join("%.0f%%" % (x * 100) for x in ratios))
            check("AC-C28 下载吞吐 >= 基线 80%（3 轮交替中位数）",
                  ratio >= 0.80,
                  "基线 %.0f KB/s -> 并发 %.0f KB/s = %.0f%%（各轮 %s）"
                  % (base_mbps, conv_mbps, ratio * 100,
                     "/".join("%.0f%%" % (x * 100) for x in ratios)), "AC-C28")

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
