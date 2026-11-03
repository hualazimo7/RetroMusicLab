#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_shot_v595.py — 拍 v5.9.5 的交付图（「进度真的在动」）。

要证明的事只有一件，而且**只在界面上看得见**：

  用户报的「转换卡住」= 进度条一直 0%、日志停在 ``▶ [1/1]``。
  修完之后同一个包（Chrono Trigger 92 首 SPC）应该看到
  「解码芯片音源… xx%」在往上走。日志证明不了「界面上看着不卡」，
  所以必须拍两张不同时刻的图 —— 一张 0% 的图说明不了任何事。

用法::

    python vendor/_shot_v595.py --src "C:\\Users\\<你>\\Desktop\\Chrono Trigger (EMU).zophar"

⚠ 这个脚本**会真的开始转换**（然后取消），所以：

  * 输出目录强制落到临时目录，绝不写用户自己的 output；
  * 拍到第二张就 ``/api/cancel``，不会真写满 1.9 GB；
  * 结束后清掉临时输出。

⚠ 端口不能复用 9412 / 9413 / 9414 / 9415（那些被别的截图脚本占着），
  否则两个脚本同时跑会抢到同一个浏览器实例，拍出来是对方的页面。
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

CDP_PORT = 9416
PROFILE = os.path.join(os.environ.get("TEMP") or r"C:\Windows\Temp",
                       "rml_v595_shot_profile")

DEFAULT_SRC = r"C:\Users\xqh\Desktop\Chrono Trigger (EMU).zophar"


def arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main():
    for k in [k for k in os.environ if "proxy" in k.lower()]:
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"

    src = os.path.abspath(arg("--src", DEFAULT_SRC))
    out = os.path.abspath(arg("--out", os.path.join(ROOT, "..", "deliverables",
                                                    "v5.9.5-web")))
    if not os.path.isdir(src):
        print("音源目录不存在：%s" % src, flush=True)
        return 1

    import websocket                                             # noqa: E402
    from web_ui import server                                    # noqa: E402
    from web_ui.shot import call, evaluate, find_edge             # noqa: E402

    def targets():
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json" % CDP_PORT, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))

    edge = find_edge()
    if not edge:
        print("找不到 Edge，无法拍图", flush=True)
        return 1

    httpd, port, _state = server.serve(port=0)

    def api(path, body=None):
        url = "http://127.0.0.1:%d%s" % (port, path)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")

    # 输出目录强制落临时区 —— 这个脚本只是拍图，不该往用户目录写东西
    tmp_out = os.path.join(os.environ.get("TEMP") or r"C:\Windows\Temp",
                           "rml_v595_shot_out")
    shutil.rmtree(tmp_out, ignore_errors=True)

    r = api("/api/add_sources", {"paths": [src]})
    print("加音源：added=%s" % r.get("added"), flush=True)
    st = api("/api/state")
    n_tr = len(st.get("tracks") or [])
    print("曲目数：%d" % n_tr, flush=True)

    os.makedirs(PROFILE, exist_ok=True)
    url = "http://127.0.0.1:%d/index.html?theme=dark" % port
    proc = subprocess.Popen(
        [edge, "--app=%s" % url, "--window-size=1280,860",
         "--window-position=40,20", "--user-data-dir=%s" % PROFILE,
         "--remote-debugging-port=%d" % CDP_PORT, "--no-first-run",
         "--no-default-browser-check",
         "--disable-features=msEdgeSidebarV2,msImplicitSignIn,msEdgeCollections"],
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

    time.sleep(2.0)
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
        mid[0] += 1
        r = call(ws, mid[0], "Page.captureScreenshot", {"format": "png"})
        data = r.get("result", {}).get("data")
        if not data:
            print("  截图失败：%s" % name, flush=True)
            return ""
        os.makedirs(out, exist_ok=True)
        p = os.path.join(out, name)
        with open(p, "wb") as f:
            f.write(base64.b64decode(data))
        print("  截图 %s（%d B）" % (p, os.path.getsize(p)), flush=True)
        return p

    shots = []
    try:
        # 等列表渲染出来（92 行）
        t0 = time.time()
        n_row = 0
        while time.time() - t0 < 40:
            time.sleep(0.8)
            n_row = ev("document.querySelectorAll('.tr-row, .track-row, tbody tr')"
                       ".length")
            if isinstance(n_row, int) and n_row >= 10:
                break
        print("列表行数：%s" % n_row, flush=True)

        # 只改显示用的路径文字，真正的 out_dir 走 /api/start 的 body
        ev("(()=>{const el=document.querySelector('#pathText');"
           "if(el) el.textContent=%s;})()" % json.dumps(tmp_out))

        print("开始转换…", flush=True)
        api("/api/start", {"opts": {"out_dir": tmp_out, "do_wav": True,
                                    "do_cd": False, "max_secs": 150.0}})

        # 展开日志面板（用户截图里就是展开状态）。
        # ⚠ 别去点 #progStrip —— 它的 onclick 是**属性**不是 addEventListener，
        #   而且只在 progStrip 自身 hidden 时才需要点；这里直接改 hidden 最稳。
        time.sleep(1.5)
        ev("(()=>{const p=document.querySelector('#logPanel');"
           "if(p) p.hidden=false;})()")

        # 两张图分别落在「解码阶段」和「写盘阶段」：
        #   a → 解码中（曲目表应只有 01 是「转换中」，其余「等待」）
        #   b → 写盘中（前 N 首「已输出」，第 N+1 首「转换中」）
        want = [(12.0, "转换中-进度条在动-a.png"),
                (58.0, "转换中-进度条在动-b.png")]
        for floor, name in want:
            t0 = time.time()
            while time.time() - t0 < 420:
                st = api("/api/state")
                if (st.get("progress") or 0) >= floor:
                    break
                if st.get("state") in ("done", "failed", "cancelled"):
                    break
                time.sleep(1.0)
            st = api("/api/state")
            print("  此刻 %.1f%% / %s" % (st.get("progress") or 0, st.get("status")),
                  flush=True)
            txt = ev("(()=>{const e=document.querySelector('#progText');"
                     "return e?e.textContent:'';})()")
            bar = ev("(()=>{const e=document.querySelector('#progBar');"
                     "return e?e.style.width:'';})()")
            print("  界面上：进度文字=%r 条宽=%r" % (txt, bar), flush=True)
            cnt = ev("(()=>({ok:document.querySelectorAll('.stat.ok').length,"
                     "wait:document.querySelectorAll('.stat.wait').length,"
                     "skip:document.querySelectorAll('.stat.skip').length}))()")
            print("  曲目表：已输出=%s 转换中=%s 等待=%s"
                  % ((cnt or {}).get("ok"), (cnt or {}).get("wait"),
                     (cnt or {}).get("skip")), flush=True)
            p = shot(name)
            if p:
                shots.append(p)
    finally:
        try:
            api("/api/cancel", {})
        except Exception:                                        # noqa: BLE001
            pass
        time.sleep(1.5)
        try:
            ws.close()
        except Exception:                                        # noqa: BLE001
            pass
        proc.terminate()
        httpd.shutdown()
        shutil.rmtree(tmp_out, ignore_errors=True)

    print("\n拍到 %d 张" % len(shots), flush=True)
    return 0 if len(shots) >= 2 else 1


if __name__ == "__main__":
    sys.exit(main())
