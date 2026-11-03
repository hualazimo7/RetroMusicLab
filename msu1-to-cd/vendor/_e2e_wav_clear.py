#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_e2e_wav_clear.py — 「清空试听列表」的界面级验证（v5.9.2）。

后端逻辑在 ``test_web_ui.py`` 的 [3i] 段已经守住了（只隐藏不删文件、
重复点不重复计数、新转的照样出现、落盘能读回）。这里补的是**界面这一半**：

1. 按钮真的在标题栏里、点得动（``#dlPlayClear`` 有绑定）；
2. 点完列表真的空了，而且空态文案是「列表已清空」**不是**「还没转过」——
   这两句混了，用户会以为文件被删了；
3. 标题栏三样东西（计数 / 清空 / 刷）不会挤成两行或把「刷」挤出去；
4. WAV 文件一个都没少。

跑法::

    python vendor/_e2e_wav_clear.py
    python vendor/_e2e_wav_clear.py --shot-dir D:/MSU1Temp/wavclear_shots
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

CDP_PORT = 9414
PROFILE = os.path.join(os.environ.get("TEMP") or r"C:\Windows\Temp",
                       "rml_wavclear_profile")

PASS = [0]
FAIL = []


def check(name, ok, detail=""):
    if ok:
        PASS[0] += 1
        print("  [OK]   %s%s" % (name, ("  — " + str(detail)) if detail else ""),
              flush=True)
    else:
        FAIL.append(name)
        print("  [FAIL] %s  — %s" % (name, detail), flush=True)


def arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def mk_wav(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x01" * 4410)


def main():
    for k in [k for k in os.environ if "proxy" in k.lower()]:
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"

    import websocket                                             # noqa: E402
    from web_ui import server                                    # noqa: E402
    from web_ui.shot import call, evaluate, find_edge            # noqa: E402

    def targets():
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json" % CDP_PORT, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))

    edge = find_edge()
    if not edge:
        print("找不到 Edge，跳过（这不是失败）", flush=True)
        return 0

    shot_dir = arg("--shot-dir", "")
    os.makedirs(PROFILE, exist_ok=True)

    # 造一个「转过 WAV」的目录：跨两个游戏文件夹，列表才有分组的样子
    base = os.path.join(os.environ.get("TEMP") or ".", "rml_wavclear_wavs")
    shutil.rmtree(base, ignore_errors=True)
    wav_root = os.path.join(base, "WAV")
    for game, n in (("Castlevania", 3), ("ACME Animation", 2)):
        for i in range(n):
            mk_wav(os.path.join(wav_root, game, "0%d_Track.wav" % (i + 1)))
    total = 5
    before = sorted(os.path.relpath(os.path.join(dp, n), wav_root).replace("\\", "/")
                    for dp, _dn, fs in os.walk(wav_root) for n in fs
                    if n.lower().endswith(".wav"))

    httpd, port, _state = server.serve(port=0)
    from web_ui.server import dl_state                           # noqa: E402
    dl = dl_state()
    with dl.lock:
        dl.dest = base
        dl.wav_dir = wav_root
        dl._wav_cache = None
        dl._wav_cleared = {}          # 每次从「没清过」开始，结果可复现
    print("预置 %d 个 WAV → %s" % (total, wav_root), flush=True)

    url = "http://127.0.0.1:%d/download.html?theme=dark" % port
    proc = subprocess.Popen(
        [edge, "--app=%s" % url, "--window-size=1280,840",
         "--window-position=40,30", "--user-data-dir=%s" % PROFILE,
         "--remote-debugging-port=%d" % CDP_PORT, "--no-first-run",
         "--no-default-browser-check",
         "--disable-features=msEdgeSidebarV2,msImplicitSignin,msEdgeCollections"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Edge 已拉起 pid=%d" % proc.pid, flush=True)

    target = None
    for i in range(25):
        time.sleep(1.0)
        try:
            for t in targets():
                if t.get("type") == "page" and "127.0.0.1" in (t.get("url") or ""):
                    target = t
                    break
        except Exception:                                        # noqa: BLE001
            continue
        if target:
            print("第 %d 秒：页面就绪" % (i + 1), flush=True)
            break
    if not target:
        print("25 秒内没拿到页面 target", flush=True)
        proc.terminate()
        httpd.shutdown()
        return 1

    time.sleep(1.5)
    ws = None
    for _a in range(4):
        try:
            ws = websocket.create_connection(
                target["webSocketDebuggerUrl"], timeout=20, suppress_origin=True,
                http_proxy_host=None, http_no_proxy=["127.0.0.1"])
            call(ws, 1, "Runtime.evaluate", {"expression": "1", "returnByValue": True})
            break
        except Exception:                                        # noqa: BLE001
            time.sleep(1.0)
    if not ws:
        print("WS 连不上", flush=True)
        proc.terminate()
        httpd.shutdown()
        return 1

    mid = [100]

    def ev(js, wait=False):
        mid[0] += 1
        return evaluate(ws, mid[0], js, await_promise=wait)

    def shot(name):
        if not shot_dir:
            return
        mid[0] += 1
        r = call(ws, mid[0], "Page.captureScreenshot", {"format": "png"})
        data = r.get("result", {}).get("data")
        if not data:
            return
        os.makedirs(shot_dir, exist_ok=True)
        p = os.path.join(shot_dir, name)
        with open(p, "wb") as f:
            f.write(base64.b64decode(data))
        print("  截图 %s（%d B）" % (p, os.path.getsize(p)), flush=True)

    try:
        t0 = time.time()
        n = 0
        while time.time() - t0 < 40:
            time.sleep(0.8)
            n = ev("document.querySelectorAll('.dl-play-row').length")
            if isinstance(n, int) and n > 0:
                break
        check("试听列表已渲染", isinstance(n, int) and n == total,
              "%s 行（应为 %d）" % (n, total))
        check("计数条是 5 首",
              ev("document.querySelector('#dlPlayCount').textContent") == "%d 首" % total,
              ev("document.querySelector('#dlPlayCount').textContent"))

        # 按钮在不在、点得动、三样东西排在一行（不换行、不被挤出去）
        box = ev("(()=>{const h=document.querySelector('.dl-play .dl-col-head');"
                 "const b=document.querySelector('#dlPlayClear');"
                 "const r=document.querySelector('#dlPlayRefresh');"
                 "if(!h||!b||!r)return null;"
                 "const hb=h.getBoundingClientRect(),bb=b.getBoundingClientRect(),"
                 "rb=r.getBoundingClientRect();"
                 "return {h:Math.round(hb.height),bw:Math.round(bb.width),"
                 "bl:Math.round(bb.left),br:Math.round(bb.right),"
                 "rl:Math.round(rb.left),rr:Math.round(rb.right),"
                 "hr:Math.round(hb.right),gap:Math.round(rb.left-bb.right),"
                 "sameLine:Math.abs(bb.top-rb.top)<2}})()")
        check("标题栏有「清空」按钮", isinstance(box, dict), box)
        if isinstance(box, dict):
            check("清空 / 刷 在同一行", box.get("sameLine") is True, box)
            check("两颗按钮不重叠、间距正常",
                  0 <= box.get("gap", -1) <= 20, "gap=%s" % box.get("gap"))
            check("「刷」没被挤出标题栏",
                  box.get("rr", 9999) <= box.get("hr", 0) + 1,
                  "%s <= %s" % (box.get("rr"), box.get("hr")))
            check("标题栏没被撑高（34px）", box.get("h") == 34, box.get("h"))
        shot("wavclear-before.png")

        # 真点一下（走 onclick 绑定，不是直接调接口）
        ev("(()=>{document.querySelector('#dlPlayClear').click();return 1})()")
        time.sleep(1.6)
        n2 = ev("document.querySelectorAll('.dl-play-row').length")
        check("点完列表空了", n2 == 0, "%s 行" % n2)
        empty = ev("(document.querySelector('#dlPlayList .dl-empty')||{}).textContent")
        check("空态说的是「列表已清空」", "已清空" in str(empty), empty)
        check("计数条回到 0 首",
              ev("document.querySelector('#dlPlayCount').textContent") == "0 首",
              ev("document.querySelector('#dlPlayCount').textContent"))
        shot("wavclear-after.png")

        # ⚠ 最关键的一条：文件一个都不能少
        after = sorted(os.path.relpath(os.path.join(dp, n_), wav_root).replace("\\", "/")
                       for dp, _dn, fs in os.walk(wav_root) for n_ in fs
                       if n_.lower().endswith(".wav"))
        check("**WAV 文件一个都没删**", after == before,
              "%d 个（前 %d 个）" % (len(after), len(before)))

        # 再点一次：不该报「又清了 5 首」
        ev("(()=>{document.querySelector('#dlPlayClear').click();return 1})()")
        time.sleep(1.2)
        toast = ev("(document.querySelector('#dlToast')||{}).textContent")
        check("空列表再点，说的是「已经是空的」", "已经是空的" in str(toast), toast)

        errs = ev("JSON.stringify(window.__errs || [])")
        check("页面无 JS 异常", errs in ("[]", "null", None), errs)
    finally:
        ws.close()
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:                                        # noqa: BLE001
            proc.kill()
        httpd.shutdown()

    print("\n%d 通过 / %d 失败" % (PASS[0], len(FAIL)), flush=True)
    if FAIL:
        print("失败项：", "；".join(FAIL), flush=True)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
