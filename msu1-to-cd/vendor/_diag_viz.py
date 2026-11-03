#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_diag_viz.py — 诊断下载页频谱面板为什么「整块不见」。

只读不写：起服务 → 开 Edge → 打开 download.html → 读 DOM / 计算样式 /
运行时探针 / window.__errs。不转歌、不落盘、不截图（要图另说）。

用法::

    py -3.14 vendor/_diag_viz.py
    py -3.14 vendor/_diag_viz.py --page index.html

⚠ 端口 9419（9412~9418 被 _shot_* / _v594 / _v595 / _v596 / _flac 占着）。
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

CDP_PORT = 9419
TMP = os.environ.get("TEMP") or r"C:\Windows\Temp"
PROFILE = os.path.join(TMP, "rml_diag_viz_profile")


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

    page = arg("--page", "download.html")

    import websocket                                             # noqa: E402
    from web_ui import server                                    # noqa: E402
    from web_ui.shot import call, find_edge                      # noqa: E402

    def evaluate(ws, mid, js, await_promise=False):
        r = call(ws, mid, "Runtime.evaluate",
                 {"expression": js, "returnByValue": True,
                  "awaitPromise": await_promise, "userGesture": True})
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return "EXC: " + json.dumps(res["exceptionDetails"],
                                        ensure_ascii=False)[:400]
        return res.get("result", {}).get("value")

    def targets():
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json" % CDP_PORT, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))

    edge = find_edge()
    if not edge:
        print("找不到 Edge", flush=True)
        return 1

    httpd, port, _state = server.serve(port=0)
    print("服务已起：127.0.0.1:%d" % port, flush=True)

    os.makedirs(PROFILE, exist_ok=True)
    url = "http://127.0.0.1:%d/%s?theme=dark" % (port, page)
    proc = subprocess.Popen(
        [edge, "--app=%s" % url, "--window-size=1360,900",
         "--window-position=40,20", "--user-data-dir=%s" % PROFILE,
         "--remote-debugging-port=%d" % CDP_PORT, "--no-first-run",
         "--no-default-browser-check",
         "--autoplay-policy=no-user-gesture-required",
         "--disable-features=msEdgeSidebarV2,msImplicitSignIn,msEdgeCollections"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Edge pid=%d" % proc.pid, flush=True)

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

    time.sleep(2.5)
    ws = None
    for _a in range(4):
        try:
            ws = websocket.create_connection(
                target["webSocketDebuggerUrl"], timeout=25, suppress_origin=True,
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

    def show(label, js):
        v = ev(js)
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        print("  %-26s %s" % (label, v), flush=True)
        return v

    try:
        # 等页面脚本把 boot() 跑完
        for _ in range(20):
            time.sleep(0.5)
            if ev("!!window.__errs"):
                break

        print("\n=== 1. DOM 是否存在 ===", flush=True)
        show("title", "document.title")
        show("css 已加载", "getComputedStyle(document.body).backgroundColor")
        show("#dlVizWrap", "!!document.getElementById('dlVizWrap')")
        show("#dlVizTog", "!!document.getElementById('dlVizTog')")
        show("#dlVizCanvas", "!!document.getElementById('dlVizCanvas')")
        show("#dlVizEmpty", "!!document.getElementById('dlVizEmpty')")
        show(".dl-viz-head", "!!document.querySelector('.dl-viz-head')")

        print("\n=== 2. 计算样式 / 几何 ===", flush=True)
        show("wrap.hidden", "document.getElementById('dlVizWrap')?.hidden")
        show("wrap.display", "(()=>{const e=document.getElementById('dlVizWrap');return e?getComputedStyle(e).display:'NO-EL'})()")
        show("wrap.rect", "(()=>{const e=document.getElementById('dlVizWrap');if(!e)return 'NO-EL';const r=e.getBoundingClientRect();return [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]})()")
        show("head.display", "(()=>{const e=document.querySelector('.dl-viz-head');return e?getComputedStyle(e).display:'NO-EL'})()")
        show("head.rect", "(()=>{const e=document.querySelector('.dl-viz-head');if(!e)return 'NO-EL';const r=e.getBoundingClientRect();return [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]})()")
        show("tog.display", "(()=>{const e=document.getElementById('dlVizTog');return e?getComputedStyle(e).display:'NO-EL'})()")
        show("tog.rect", "(()=>{const e=document.getElementById('dlVizTog');if(!e)return 'NO-EL';const r=e.getBoundingClientRect();return [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]})()")
        show("canvas.rect", "(()=>{const e=document.getElementById('dlVizCanvas');if(!e)return 'NO-EL';const r=e.getBoundingClientRect();return [Math.round(r.width),Math.round(r.height),e.width,e.height]})()")
        show("canvas cssW/H", "(()=>{const e=document.getElementById('dlVizCanvas');if(!e)return 'NO-EL';const s=getComputedStyle(e);return [s.display,s.width,s.height]})()")
        show("aside.rect", "(()=>{const e=document.querySelector('aside');if(!e)return 'NO-EL';const r=e.getBoundingClientRect();return [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]})()")
        show("aside.scrollH/clientH", "(()=>{const e=document.querySelector('aside');if(!e)return 'NO-EL';return [e.scrollHeight,e.clientHeight,getComputedStyle(e).overflow]})()")
        show("play.rect", "(()=>{const e=document.querySelector('.dl-play');if(!e)return 'NO-EL';const r=e.getBoundingClientRect();return [Math.round(r.y),Math.round(r.height)]})()")
        show("playList.rect", "(()=>{const e=document.getElementById('dlPlayList');if(!e)return 'NO-EL';const r=e.getBoundingClientRect();return [Math.round(r.y),Math.round(r.height)]})()")

        print("\n=== 3. 运行时探针 ===", flush=True)
        show("VizKit 存在", "typeof window.VizKit")
        show("__dlViz 存在", "!!window.__dlViz")
        show("viz.on", "window.__dlViz?.on")
        show("viz.raf", "!!window.__dlViz?.raf")
        show("viz.touched", "window.__dlViz?.touched")
        show("viz.ctx.state", "window.__dlViz?.ctx?.state || 'null(未建)'")
        show("localStorage msu1.dl.viz", "(()=>{try{return localStorage.getItem('msu1.dl.viz')}catch(e){return 'ERR'}})()")

        print("\n=== 4. JS 异常 ===", flush=True)
        show("__errs", "JSON.stringify(window.__errs||[])")

        print("\n=== 5. 谁把它藏了（遍历祖先） ===", flush=True)
        show("祖先链", "(()=>{let e=document.getElementById('dlVizWrap');if(!e)return 'NO-EL';const out=[];while(e&&e!==document.body){const s=getComputedStyle(e);out.push((e.id?('#'+e.id):e.tagName.toLowerCase())+(e.className&&typeof e.className==='string'?('.'+e.className.trim().split(/\\s+/).join('.')):'')+' [d='+s.display+' v='+s.visibility+' o='+s.opacity+' h='+s.height+' ov='+s.overflow+']');e=e.parentElement;}return out})()")

    finally:
        try:
            ws.close()
        except Exception:                                        # noqa: BLE001
            pass
        proc.terminate()
        httpd.shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())
