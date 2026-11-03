#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端: 下载 .7z -> 自动解压 -> 主窗口识别 -> 转出非静音 WAV。

不联网也能跑主体: 默认用**本地 HTTP 服务**伺服一个真实的 7z 包(用 Windows
自带的 tar.exe 现打一个), 因此走的是与线上完全相同的下载器代码路径
(断点续传/重试/事件/解压), 只是把 joshw 的 URL换成本地地址。

用法:

    py -3 vendor/_smoke_dl.py [--no-tar]     # --no-tar 模拟"找不到解压器"

打印真实日志片段, 供交付核对。
"""

import http.server
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import joshw_dl                     # noqa: E402

SRC_NS = r"C:\temp\gmev2\nsf"
TAR = r"C:\Windows\System32\tar.exe"
FAILS = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def serve(directory):
    """把目录挂成一个只读 HTTP 服务(多线程, 供下载器的并发 worker 用)。

    注意: ``SimpleHTTPRequestHandler.__init__`` 会把 ``directory`` 设成
    ``os.getcwd()``, 直接写成类属性是**无效**的 —— 必须在 __init__ 里显式传。
    """
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=directory, **kw)

        def log_message(self, *a, **k):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def make_package(workdir, nsf_path):
    """用 Windows 自带 tar 打一个真 7z(内容 = 一个 NSF)。"""
    pkg_dir = os.path.join(workdir, "pkg")
    os.makedirs(pkg_dir, exist_ok=True)
    shutil.copy2(nsf_path, os.path.join(pkg_dir, os.path.basename(nsf_path)))
    out = os.path.join(workdir, "Test NSF Pack.7z")
    if os.path.exists(out):
        os.remove(out)
    r = subprocess.run([TAR, "-a", "-cf", out, os.path.basename(nsf_path)],
                       cwd=pkg_dir, capture_output=True, shell=False)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace"))
    return out


def main():
    if not os.path.isdir(SRC_NS):
        print(f"找不到样本目录 {SRC_NS}")
        return 2
    nsf = next((os.path.join(SRC_NS, f) for f in sorted(os.listdir(SRC_NS))
                if f.lower().endswith(".nsf")), None)
    if not nsf:
        print("样本目录里没有 .nsf")
        return 2

    work = tempfile.mkdtemp(prefix="msu1_dl_")
    pkg = make_package(work, nsf)
    print(f"样本 NSF : {os.path.basename(nsf)} ({os.path.getsize(nsf)} B)")
    print(f"打包 7z  : {os.path.basename(pkg)} ({os.path.getsize(pkg)} B)")

    httpd, port = serve(work)
    dest = os.path.join(work, "dl")
    events = []
    # 包名里有空格, URL 必须百分号编码, 否则 http.client 直接拒收
    url = f"http://127.0.0.1:{port}/" + urllib.parse.quote(os.path.basename(pkg))

    def on_event(kind, payload):
        events.append((kind, payload))

    print("\n=== 1. 下载 + 自动解压 ===")
    dl = joshw_dl.Downloader(workers=1, on_event=on_event,
                             auto_extract=True, delete_archive=False,
                             extract_dir=dest)
    dl.add(url, os.path.join(dest, os.path.basename(pkg)),
           os.path.getsize(pkg))
    dl.start()
    check(dl.wait(120), "下载线程在 120s 内收尾")

    kinds = [k for k, _p in events]
    check("file_done" in kinds and
          dict(events)["file_done"].get("ok") is not None, "file_done 已发出")
    check("extract_done" in kinds, "extract_done 已发出")

    ex = next((p for k, p in events if k == "extract_done"), None)
    if ex is None:
        fail = next((p for k, p in events if k == "extract_failed"), None)
        print("  解压失败:", fail)
        check(False, "解压成功")
        return 1
    files = ex["files"]
    print(f"  解压目录: {ex['dest']}")
    for f in files:
        print(f"    · {os.path.basename(f)}  ({os.path.getsize(f)} B)")
    check(len(files) >= 1, f"解出 {len(files)} 个文件")
    check(any(f.lower().endswith(".nsf") for f in files), "解出了 .nsf")
    check(os.path.isfile(pkg), "原 .7z 被保留(默认不删)")
    check(ex["archive_removed"] is False, "archive_removed == False")

    print("\n=== 2. 主窗口识别 + 转 WAV ===")
    import tkinter as tk
    import msu1_gui as gui
    import chip_formats as cf
    gui.messagebox.showinfo = lambda *a, **k: None
    gui.messagebox.showwarning = lambda *a, **k: None
    gui.messagebox.askyesno = lambda *a, **k: True

    nsf_out = next(f for f in files if f.lower().endswith(".nsf"))
    root = tk.Tk()
    app = gui.App(root)
    app.drop_enabled = False
    root.update()
    app.add_paths([nsf_out])
    check(len(app.sources) == 1, "解出的 .nsf 已加入列表")
    e = app.sources[0]
    print(f"  识别: kind={e['kind']} fmt={e['fmt']} "
          f"n_tracks={e['n_tracks']} est={e['est_secs']:.0f}s")
    check(e["kind"] == "CHIP", f"kind == CHIP(得到 {e['kind']})")
    check(e["fmt"] == "NSF", f"fmt == NSF(得到 {e['fmt']})")
    check(e["n_tracks"] >= 1, f"轨数被解析 = {e['n_tracks']}")
    v = app.src_tree.item(e["iid"], "values")
    print(f"  表格行: 名称={v[0]!r} 格式={v[1]!r} 曲目={v[2]!r} "
          f"大小={v[3]!r} 时长={v[4]!r}")

    out_dir = os.path.join(work, "out")
    os.makedirs(out_dir, exist_ok=True)
    app.var_out.set(out_dir)
    app.var_wav.set(True)
    app.var_cd.set(False)
    app.var_chip_secs.set("30")           # 30s: NSF 是默认单遍, 别跑太久
    app.start()

    deadline = time.time() + 180
    while app.worker is not None and app.worker.is_alive() \
            and time.time() < deadline:
        root.update()
        time.sleep(0.2)
    for _i in range(60):                   # 把队列里的 done 消息消化完
        root.update()
        time.sleep(0.1)
        if app.btn_start["state"] != "disabled":
            break

    logs = []
    try:
        text = app.logw.get("1.0", "end")
        logs = [ln for ln in text.splitlines() if ln.strip()]
    except tk.TclError:
        pass
    print("\n  ---- 主窗口日志(尾部 22 行) ----")
    for ln in logs[-22:]:
        print("  | " + ln)

    wavs = sorted(f for f in os.listdir(out_dir) if f.lower().endswith(".wav"))
    check(len(wavs) >= 1, f"产出 {len(wavs)} 个 WAV")
    if wavs:
        import wave
        from array import array
        for w in wavs[:3]:
            p = os.path.join(out_dir, w)
            with wave.open(p, "rb") as f:
                ch, sw, sr, n = (f.getnchannels(), f.getsampwidth(),
                                 f.getframerate(), f.getnframes())
                raw = f.readframes(n)
            a = array("h")
            a.frombytes(raw)
            peak = max(max(a), -min(a)) if a else 0
            dur = n / sr
            print(f"  WAV {w}: {ch}ch/{sw * 8}bit/{sr}Hz "
                  f"{dur:.1f}s peak={peak}")
            check((ch, sw, sr) == (2, 2, 44100), f"{w} 规格 2ch/16bit/44.1k")
            check(peak > 300, f"{w} 非静音(peak={peak} > 300)")
            check(dur > 1.0, f"{w} 时长 > 1s(实际 {dur:.1f}s)")
    app._on_close()
    httpd.shutdown()

    print("\n=== 3. 降级路径: 找不到 tar.exe ===")
    real_root = os.environ.get("SystemRoot")
    os.environ["SystemRoot"] = r"C:\__no_such_windows__"
    check(joshw_dl.find_tar() is None, "SystemRoot 指空时 find_tar() 返回 None")
    work2 = tempfile.mkdtemp(prefix="msu1_dl_nf_")
    dest2 = os.path.join(work2, "dl")
    pkg2 = os.path.join(work2, "Fallback.7z")
    shutil.copy2(pkg, pkg2)
    ok, got, err = joshw_dl.extract_archive(pkg2, os.path.join(dest2, "Fallback"))
    print(f"  解压返回 ok={ok} err={err}")
    check(ok is False, "解压失败并返回 False")
    check("保留原包" in err or "手动解压" in err, "错误文案说明了要手动解压")
    check(os.path.isfile(pkg2), "原 .7z 未被删除(不丢数据)")
    if real_root:
        os.environ["SystemRoot"] = real_root
    else:
        os.environ.pop("SystemRoot", None)

    # 超时路径要在 tar **可用**的前提下测, 否则只会走到"找不到解压器"分支
    print("  -- 解压超时(timeout=0.001s) --")
    work2b = tempfile.mkdtemp(prefix="msu1_dl_to_")
    pkg2b = os.path.join(work2b, "Timeout.7z")
    shutil.copy2(pkg, pkg2b)
    ok2, _, err2 = joshw_dl.extract_archive(pkg2b, os.path.join(work2b, "out"),
                                            timeout=0.001)
    print(f"  超时返回 ok={ok2} err={err2}")
    check(ok2 is False and "超时" in err2, "超时文案说明了超时")
    check("保留原包" in err2, "超时时保留原包")
    check(os.path.isfile(pkg2b), "超时后 .7z 仍在")

    # 损坏的包: 必须失败而不是静默成功
    work2c = tempfile.mkdtemp(prefix="msu1_dl_bad_")
    pkg2c = os.path.join(work2c, "Broken.7z")
    with open(pkg2c, "wb") as f:
        f.write(b"this is definitely not a 7z archive")
    ok2b, _, err2b = joshw_dl.extract_archive(pkg2c,
                                              os.path.join(work2c, "out"))
    print(f"  损坏包 ok={ok2b} err={err2b}")
    check(ok2b is False and "保留原包" in err2b, "损坏包失败且保留")
    check(os.path.isfile(pkg2c), "损坏的 .7z 未被删除")
    for d in (work2, work2b, work2c):
        shutil.rmtree(d, ignore_errors=True)

    print("\n=== 4. 删除原 .7z 开关(delete_archive=True) ===")
    work3 = tempfile.mkdtemp(prefix="msu1_dl_del_")
    serve_dir = os.path.join(work3, "src")
    os.makedirs(serve_dir, exist_ok=True)
    pkg3 = os.path.join(serve_dir, "DeleteMe.7z")
    shutil.copy2(pkg, pkg3)
    httpd3, port3 = serve(serve_dir)
    ev3 = []
    dl3 = joshw_dl.Downloader(workers=1,
                              on_event=lambda k, p: ev3.append((k, p)),
                              auto_extract=True, delete_archive=True,
                              extract_dir=os.path.join(work3, "dl"))
    target3 = os.path.join(work3, "dl", "DeleteMe.7z")
    dl3.add(f"http://127.0.0.1:{port3}/DeleteMe.7z", target3,
            os.path.getsize(pkg3))
    dl3.start()
    check(dl3.wait(60), "delete_archive 模式下载收尾")
    ex3 = next((p for k, p in ev3 if k == "extract_done"), None)
    check(ex3 is not None, "delete_archive 模式解压成功")
    if ex3:
        check(ex3["archive_removed"] is True, "archive_removed == True")
        check(not os.path.isfile(target3), "原 .7z 已按开关删除")
        check(len(ex3["files"]) >= 1,
              f"解出的文件仍在: {[os.path.basename(x) for x in ex3['files']]}")
    httpd3.shutdown()

    for d in (work, work2, work3):
        shutil.rmtree(d, ignore_errors=True)

    print()
    if FAILS:
        print(f"DL_E2E FAIL ({len(FAILS)}):")
        for f in FAILS:
            print("   -", f)
        return 1
    print("DL_E2E PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
