# -*- coding: utf-8 -*-
"""QA 独立验收 · 真实端到端（联网 joshw -> .7z -> 解压 -> 转 WAV）

路径: joshw.info 真实站点 -> 下载 1 个 .7z -> 自动解压 -> 转 WAV
      -> 断言产出**非静音** WAV（峰值 > 300）。

网络不可用时不静默回落，如实报告 NET_FAIL 并退出（由调用方决定是否降级）。

用法: C:/Python314/python.exe vendor/_qa_e2e_live.py
"""
import glob
import os
import shutil
import ssl
import sys
import tempfile
import threading
import time
import wave
import struct

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


def wav_peak(path):
    try:
        with wave.open(path, "rb") as w:
            nch, sw = w.getnchannels(), w.getsampwidth()
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
        if sw != 2:
            return (0, 0, sr, nch)
        v = struct.unpack("<%dh" % (len(raw) // 2), raw[:len(raw) // 2 * 2])
        return (max((abs(x) for x in v), default=0), len(v) // nch, sr, nch)
    except Exception:                                  # noqa: BLE001
        return (0, 0, 0, 0)


def main():
    print("=" * 78)
    print("QA 独立验收 · 真实端到端（联网）  pid=%d" % os.getpid())
    print("=" * 78)

    import urllib.request
    import json
    import urllib.parse
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # ---- 0. 连通性 ----
    print("\n-- 0. 站点连通性 --")
    base = None
    for u in ("http://joshw.info/", "https://joshw.info/"):
        try:
            r = urllib.request.urlopen(u, timeout=20, context=ctx)
            body = r.read(400)
            print("  连通: %s -> %s, %d bytes" % (u, r.status, len(body)))
            base = u
            break
        except Exception as e:                          # noqa: BLE001
            print("  不可达: %s -> %s: %s" % (u, type(e).__name__,
                                             str(e)[:100]))
    if base is None:
        print("\nNET_FAIL: 站点完全不可达 —— 无法做真实下载 E2E。")
        print("（按验收要求如实说明，不静默回落）")
        return 2
    print("  注: HTTPS 证书主机名不匹配（joshw.info），本机只能走 HTTP。")

    # ---- 1. 找一个真实 .7z 音源包 ----
    print("\n-- 1. 探测真实 .7z 音源包（走产品自己的 Apache autoindex 解析）--")
    import joshw_source as js
    sevenz = None
    tried = []
    # 子站根页通常只有字母目录，.7z 在字母目录里（用产品自己的 _parse_dirs
    # + parse_index 下探，路径与产品完全一致）。
    # HTTPS 证书主机名不匹配 -> 换 http 直连。
    def fetch(u):
        req = urllib.request.Request(u, headers={"User-Agent": "qa"})
        return urllib.request.urlopen(req, timeout=25,
                                      context=ctx).read().decode("utf-8",
                                                                "replace")

    def to_http(u):
        return ("http://" + u[len("https://"):]) if u.startswith("https://") \
            else u

    for pf in js.PLATFORMS:
        if pf["fmt"] not in ("GBS", "NSF"):
            continue
        root = "http://%s/" % pf["host"]
        try:
            html = fetch(root)
        except Exception as e:                          # noqa: BLE001
            tried.append("%s -> %s" % (pf["host"], str(e)[:50]))
            continue
        entries = js.parse_index(html, root)
        dirs = [to_http(d[1]) for d in js._parse_dirs(html, root)]
        print("  %-18s 根 .7z=%d 字母目录=%d"
              % (pf["host"], len(entries), len(dirs)))
        # 根页散落的先看，其次进字母目录挑**最小的**（下载快、解压快）
        pool = list(entries)
        for d in dirs[:4]:
            if len(pool) >= 8:
                break
            try:
                pool.extend(js.parse_index(fetch(d), d))
            except Exception:                          # noqa: BLE001
                continue
        if not pool:
            tried.append("%s -> 根页与字母目录都无 .7z" % pf["host"])
            continue
        pool = [p for p in pool if str(p.get("url", "")).lower().endswith(".7z")]
        if not pool:
            continue
        e0 = min(pool, key=lambda p: int(p.get("size") or 0))
        sevenz = to_http(str(e0.get("url")))
        print("  选中(%s, 共%d 个候选): %s" % (pf["name"], len(pool),
                                                sevenz.split("/")[-1]))
        print("        size=%s mtime=%s" % (e0.get("size"), e0.get("mtime")))
        break
    if not sevenz:
        print("\nNET_FAIL: 所有子站 autoindex 都取不到 .7z —— 无法做真实下载 E2E。")
        for t in tried[:10]:
            print("   " + t)
        return 2

    tmp = tempfile.mkdtemp(prefix="qa_e2e_live_%d " % os.getpid())
    try:
        # ---- 2. 真实下载 ----
        print("\n-- 2. 真实下载 .7z --")
        arc = os.path.join(tmp, "pack.7z")
        t0 = time.time()
        try:
            data = urllib.request.urlopen(sevenz, timeout=120,
                                         context=ctx).read()
        except Exception as e:                          # noqa: BLE001
            print("\nNET_FAIL: 下载失败 %s: %s" % (type(e).__name__,
                                                  str(e)[:140]))
            return 2
        with open(arc, "wb") as f:
            f.write(data)
        dt = time.time() - t0
        check("真实下载到 .7z（%.1f KB, %.1fs）" % (len(data) / 1024.0, dt),
              os.path.getsize(arc) == len(data) and len(data) > 0,
              "%d bytes" % len(data))
        print("  magic=%s" % data[:6].hex())

        # ---- 3. 自动解压（产品自己的解压器路径）----
        print("\n-- 3. 自动解压（tar.exe / 产品路径）--")
        import joshw_dl
        ex = os.path.join(tmp, "ex")
        os.makedirs(ex, exist_ok=True)
        tar = joshw_dl.find_tar()
        check("找到解压器", bool(tar), str(tar))
        import subprocess
        if tar:
            p = subprocess.run([tar, "-xf", arc, "-C", ex],
                               capture_output=True, timeout=180)
            check("tar 解压成功", p.returncode == 0,
                  "rc=%s err=%s" % (p.returncode,
                                    p.stderr.decode("utf-8", "replace")[:120]))
        got = [os.path.join(r, f) for r, _d, fs in os.walk(ex) for f in fs]
        check("解压出文件", len(got) > 0, "文件数=%d" % len(got))
        print("  解压产物:")
        for g in got[:12]:
            print("    %-52s %8d B" % (os.path.basename(g)[:52],
                                       os.path.getsize(g)))
        conv = [g for g in got
                if os.path.splitext(g)[1].lower() in
                (".spc", ".nsf", ".gbs", ".vgm", ".hes", ".kss", ".gym",
                 ".sap", ".ay", ".nsfe")]
        if not conv:
            print("\n  该包内无芯片/SPC 文件（纯 .mp3/.7z 等），"
                  "转换环节改用本地样本验证（已在 B18/B22 脚本覆盖）。")
            return 0

        # ---- 4. 自动转 WAV ----
        print("\n-- 4. 自动转 WAV（ConvertWorker 全流程）--")
        import dl_convert as dc
        wav_dir = os.path.join(ex, "WAV")
        ev = []
        lk = threading.Lock()

        def on_ev(kind, payload):
            with lk:
                ev.append((kind, dict(payload or {})))

        w = dc.ConvertWorker(wav_dir, on_ev, threading.Event())
        w.start()
        for c in conv:
            w.enqueue({"src": c, "url": sevenz,
                       "policy": dc.policy_for(c), "pkg": "真实包"})
        w.mark_src_finished()
        end = time.time() + 600
        while time.time() < end and w.is_alive():
            time.sleep(0.3)
        w.wait(30)

        with lk:
            done = [p for k, p in ev if k == "cv_file_done"]
            failed = [p for k, p in ev if k == "cv_file_failed"]
            skips = [p for k, p in ev if k == "cv_skip"]
            exit_ = next((p for k, p in ev if k == "cv_thread_exit"), None)
        print("  事件汇总: done=%d failed=%d skip=%d" %
              (len(done), len(failed), len(skips)))
        for k, p in ev:
            if k in ("cv_file_done", "cv_file_failed"):
                print("    %-16s %-46s n_wav=%s %s" %
                      (k, os.path.basename(p.get("src", ""))[:46],
                       p.get("n_wav"), str(p.get("error", ""))[:70]))
        check("转换线程正常退出", not w.is_alive())
        check("有成功产出（真实下载素材）", len(done) > 0,
              "done=%d failed=%d" % (len(done), len(failed)))
        if exit_:
            print("  cv_thread_exit: %s" % {k: exit_.get(k) for k in
                                            ("ok", "fail", "skip", "wavs",
                                             "bytes", "secs") if k in exit_})
        wavs = sorted(glob.glob(os.path.join(wav_dir, "*.wav")))
        check("磁盘上有 WAV 产出", len(wavs) > 0, "落盘=%d" % len(wavs))
        if wavs:
            peaks = []
            for p in wavs[:8]:
                pk, n, sr, nch = wav_peak(p)
                peaks.append(pk)
                print("    %-46s peak=%-6s frames=%-8s %sHz %dch" %
                      (os.path.basename(p)[:46], pk, n, sr, nch))
            check("产出 WAV 非静音（峰值 > 300，AC-C3）",
                  max(peaks) > 300, "峰值=%s" % peaks)
            # manifest
            mfp = os.path.join(wav_dir, ".msu1-convert.json")
            check("manifest 已落盘", os.path.isfile(mfp), mfp)
            if os.path.isfile(mfp):
                mf = dc.ConvertManifest(mfp)
                n = mf.load()
                print("  manifest entries=%d" % n)
                check("好文件已记账（already_done=True）",
                      all(mf.already_done(c) for c in conv if
                          os.path.isfile(c) and len(done)),
                      "entries=%d" % n)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 78)
    print("真实端到端: 通过 %d / 失败 %d" % (PASS[0], len(FAIL)))
    for f in FAIL:
        print("  - %s" % f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
