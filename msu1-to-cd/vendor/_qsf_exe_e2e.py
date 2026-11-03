# -*- coding: utf-8 -*-
"""用**打包后的 exe** 真转一首 QSF —— 验 onefile 里能不能找到 qsf2wav.exe。

为什么单写一个脚本、而不塞进 ``check_web_exe.py``：
``check_web_exe`` 要跑满 169 条、开窗口、验单实例，跑一次 30 秒；
这里只关心一件事 —— **打包态下 ``find_decoder()`` 能不能在
``%TEMP%\\_MEIxxxxx`` 里找到 qsf2wav.exe**。

这是 QSF 独有的风险点：``qsf_native._search_dirs()`` 在打包态走
``sys._MEIPASS``，而 ``qsf2wav.exe`` 是靠 ``build_web_pyi.EXTRA_EXE``
才带出去的。源码树里永远正常，只有 exe 会犯。

用法::

    py -3.14 vendor/_qsf_exe_e2e.py
    py -3.14 vendor/_qsf_exe_e2e.py --exe "../deliverables/v5.9.10-web/MSU1Converter-web.exe"
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULT_EXE = os.path.join(
    ROOT, "build", "MSU1Converter-web-pyi", "MSU1Converter-web.exe")
DEFAULT_SRC = (
    r"D:\MSU1Downloads\joshw\QSound 街机（Hoot）"
    r"\Mega Man 2 - The Power Fighters [Rockman 2 - The Power Fighters] "
    r"(QSF)(1996)(Capcom)\01 - Opening.miniqsf")

BASE = "http://127.0.0.1:47913"

_n_ok = 0
_n_bad = 0


def _mark(cond, label, detail=""):
    global _n_ok, _n_bad
    if cond:
        _n_ok += 1
        print("  [OK] %s%s" % (label, ("  → " + str(detail)) if detail else ""))
    else:
        _n_bad += 1
        print("  [!!] %s%s" % (label, ("  → " + str(detail)) if detail else ""))


def _req(path, body=None, timeout=60):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:                                    # noqa: BLE001
        return 0, b""


def _jget(path, timeout=60):
    code, raw = _req(path, None, timeout)
    try:
        return code, json.loads(raw.decode("utf-8"))
    except Exception:                                    # noqa: BLE001
        return code, {}


def _jpost(path, body=None, timeout=60):
    code, raw = _req(path, body if body is not None else {}, timeout)
    try:
        return code, json.loads(raw.decode("utf-8"))
    except Exception:                                    # noqa: BLE001
        return code, {}


def _wait(cond, timeout, step=0.3):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=DEFAULT_EXE)
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--keep", action="store_true", help="跑完不关 exe")
    a = ap.parse_args(argv)

    exe = os.path.abspath(a.exe)
    src = os.path.abspath(a.src)
    if not os.path.isfile(exe):
        print("找不到 exe: %s" % exe)
        return 2
    if not os.path.isfile(src):
        print("找不到样本: %s" % src)
        return 2

    out = tempfile.mkdtemp(prefix="qsf_exe_")
    print("exe : %s" % exe)
    print("样本: %s" % src)
    print("输出: %s\n" % out)

    env = dict(os.environ)
    unpack = os.path.join(ROOT, "_pyi_unpack_tmp")
    try:
        os.makedirs(unpack, exist_ok=True)
        env["TEMP"] = unpack
        env["TMP"] = unpack
    except OSError:
        pass

    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=env)
    try:
        ok = _wait(lambda: _jget("/api/state", timeout=3)[0] == 200, 180)
        _mark(ok, "后端在 180 秒内起来")
        if not ok:
            return 1

        # ---- ① diag：打包态真的找得到 qsf2wav.exe ----
        code, d = _jget("/api/diag")
        q = (d.get("qsf") or {})
        _mark(bool(q.get("decoder")), "打包态 diag：qsf2wav.exe 在包里",
              q.get("decoder_path") or q)
        _mark(os.path.basename(str(q.get("decoder_path") or "")) == "qsf2wav.exe",
              "解码器路径确实指向 qsf2wav.exe", q.get("decoder_path"))
        # onefile 会解到 _MEIxxxx，路径里必须有它 —— 否则说明它找的是源码树
        _mark("_MEI" in str(q.get("decoder_path") or ""),
              "解码器是从 onefile 解包目录里找到的",
              q.get("decoder_path"))

        # ---- ② 加音源：要归成 QSF ----
        _jpost("/api/clear", {})
        code, d2 = _jpost("/api/add_sources", {"paths": [src]})
        _mark(code == 200 and d2.get("added") == 1, "加 .miniqsf 音源", d2)
        code, st = _jget("/api/state")
        srcs = st.get("sources") or []
        _mark(bool(srcs) and srcs[0].get("kind") == "QSF",
              "归成 QSF（不是「未知」）", srcs[:1])

        # ---- ③ 真转 WAV ----
        code, d3 = _jpost("/api/start", {"opts": {
            "out_dir": out, "do_wav": True, "fmt": "wav"}})
        _mark(code == 200 and d3.get("ok"), "start 接受转换请求", d3)

        ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 300)
        code, st = _jget("/api/state")
        _mark(ok, "转换在 300 秒内结束", st.get("state"))
        _mark(st.get("state") == "done", "state=done", st.get("state"))

        res = st.get("result") or {}
        _mark(int(res.get("ok") or 0) >= 1, "result.ok ≥ 1", res)
        _mark(int(res.get("fail") or 0) == 0, "result.fail = 0",
              (res, (st.get("logs") or [])[-6:]))

        wavs = [f for f in os.listdir(out) if f.lower().endswith(".wav")]
        _mark(bool(wavs), "产出 WAV", wavs)
        if wavs:
            p = os.path.join(out, wavs[0])
            n = os.path.getsize(p)
            # 45 秒 × 176400 B/s + 44 字节头
            _mark(n == 7938044, "大小 = 45 秒 × 176400 + 44（44100/16/2）",
                  "%d 字节" % n)
            with open(p, "rb") as f:
                head = f.read(44)
            _mark(head[:4] == b"RIFF" and head[8:12] == b"WAVE",
                  "是合法 WAV")
            rate = int.from_bytes(head[24:28], "little")
            ch = int.from_bytes(head[22:24], "little")
            _mark((ch, rate) == (2, 44100), "2ch / 44100", "%dch/%d" % (ch, rate))
        print()
    finally:
        if a.keep:
            print("--keep：exe 留着不关（pid=%d）" % proc.pid)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(out, ignore_errors=True)

    print("=" * 46)
    print("QSF 打包态端到端: %d 通过 / %d 失败" % (_n_ok, _n_bad))
    print("=" * 46)
    return 0 if _n_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
