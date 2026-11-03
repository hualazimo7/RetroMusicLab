#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/e2e_dl.py — 下载页的**联网**端到端验证（手动跑，不属于门禁）。

## 为什么单独放一个

`test_web_ui.py` / `check_web_exe.py` 都**不联网**（除了拉 joshw 索引的那一步），
所以「真的下一个包 → 解压 → 转 WAV」这条链没被它们覆盖。而这条链恰恰
暴露过最隐蔽的一个 bug：

    落盘文件名少了 .7z  →  is_archive() 不认 → 不解压 → is_convertible() 不认
    → 不转 WAV  →  表现是「下载成功、什么都没产出」

不联网就跑不了，所以不能进 CI 式的门禁，只能手动跑。

## 跑法

    C:/Python314/python.exe vendor/e2e_dl.py
    C:/Python314/python.exe vendor/e2e_dl.py --key snes --dest D:\\tmp\\dl
    C:/Python314/python.exe vendor/e2e_dl.py --index 0        # 按体积排序取第 N 小

## 判据（脚本自己会打印）

    1. 落盘文件名带扩展名（.7z / .zip / .rar）
    2. ex_pkgs >= 1（真解压了）
    3. cv_ok >= 1 且 WAV 目录里有 .wav，文件头是 RIFF

退出码：0 = 全部满足，2 = 下载/解压成功但没产出 WAV，1 = 起不来。
"""

import argparse
import json
import os
import shutil
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))       # 让 `web_ui` 能 import
from web_ui import server                       # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="spc", help="机种 key（默认 spc）")
    ap.add_argument("--dest", default=r"D:\MSU1Temp\dl_e2e")
    ap.add_argument("--index", type=int, default=0,
                    help="按体积升序取第 N 小的条目（默认 0 = 最小）")
    ap.add_argument("--keep", action="store_true", help="不清空目标目录")
    a = ap.parse_args(argv)

    dest = a.dest
    if not a.keep and os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)

    httpd, port, _st = server.serve(port=0)
    B = "http://127.0.0.1:%d" % port

    def get(p):
        with urllib.request.urlopen(B + p, timeout=30) as r:
            return json.loads(r.read())

    def post(p, b=None):
        data = json.dumps(b or {}).encode()
        req = urllib.request.Request(B + p, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())

    print("load %s ..." % a.key, flush=True)
    post("/api/dl/load", {"key": a.key})
    s = {}
    for _ in range(160):
        s = get("/api/dl/state?since=0")
        if not s["scanning"]:
            break
        time.sleep(0.5)
    print("条目 %d" % s.get("count", 0), flush=True)
    if not s.get("rows"):
        print("没有条目，退出", flush=True)
        httpd.shutdown()
        return 1

    rows = sorted(s["rows"], key=lambda r: r["size"])
    if a.index >= len(rows):
        print("--index 越界（只有 %d 条）" % len(rows), flush=True)
        httpd.shutdown()
        return 1
    pick = rows[a.index]
    print("选中: %s  %s" % (pick["name"], pick["size_text"]), flush=True)

    post("/api/dl/select", {"urls": [pick["url"]], "mode": "set"})
    d = post("/api/dl/set_opts", {"dest": dest, "workers": 2,
                                  "auto_extract": True, "auto_wav": True})
    print("opts:", d["opts"], d["dest"], flush=True)

    d = post("/api/dl/start", {})
    print("start ->", d, flush=True)
    if not d.get("ok"):
        httpd.shutdown()
        return 1

    t0 = time.time()
    last = 0
    while time.time() - t0 < 300:
        s = get("/api/dl/state?since=%d" % last)
        last = s["seq"]
        for row in s["logs"]:
            print("   ", row[1], flush=True)
        if not s["busy"]:
            break
        time.sleep(0.6)

    s = get("/api/dl/state?since=%d" % last)
    for row in s["logs"]:
        print("   ", row[1], flush=True)
    print("\nphase=%s status=%s" % (s["phase"], s["status"]), flush=True)
    print("stats=%s" % s["stats"], flush=True)

    files = []
    for root, _d, names in os.walk(dest):
        for n in names:
            files.append(os.path.join(root, n))
    print("\n落盘 %d 个文件：" % len(files), flush=True)
    for f in sorted(files)[:14]:
        print("   %8d  %s" % (os.path.getsize(f),
                              os.path.relpath(f, dest)), flush=True)

    # ---- 三条判据 ----
    st = s["stats"]
    arc = [f for f in files
           if os.path.splitext(f)[1].lower() in (".7z", ".zip", ".rar")]
    wavs = [f for f in files if f.lower().endswith(".wav")]
    ok_name = bool(arc)
    ok_ex = int(st.get("ex_pkgs") or 0) >= 1
    ok_wav = int(st.get("cv_ok") or 0) >= 1 and bool(wavs)
    print("\n判据：", flush=True)
    print("  落盘名带扩展名 : %s" % ("OK" if ok_name else "FAIL"), flush=True)
    print("  真解压过       : %s (%s 个包 / %s 个文件)"
          % ("OK" if ok_ex else "FAIL", st.get("ex_pkgs"), st.get("ex_files")),
          flush=True)
    print("  转出 WAV       : %s (%s 首)" % ("OK" if ok_wav else "FAIL",
                                            st.get("cv_ok")), flush=True)
    for w in wavs[:3]:
        with open(w, "rb") as fh:
            head = fh.read(4)
        print("    %s  头=%r" % (os.path.basename(w), head), flush=True)

    httpd.shutdown()
    if ok_name and ok_ex and ok_wav:
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
