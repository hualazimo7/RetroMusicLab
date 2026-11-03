#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_shot_flac.py — 拍 v5.9.7 的交付图（FLAC 输出选项）。

要证明的事：

  v5.9.7 给主界面和下载页都加了 FLAC 输出。**一张静态截图证明不了
  「互斥真的生效」** —— 卡片画在那里、样式好看，但点 FLAC 时 WAV 是否
  真的被取消，只有真点一次才知道。所以这个脚本会：

    1. 打开主界面，拍右栏三张卡片（BIN+CUE 独占一行，WAV / FLAC 并排）；
    2. **真的点一下 FLAC 卡片**，读回两张卡的 class，确认 WAV 掉了；
    3. 再拍一张，肉眼能看出选中态换到了 FLAC；
    4. 打开下载页，拍「下载后自动转 WAV / FLAC」两个复选框；
    5. 真的勾上 FLAC，确认 WAV 被取消、多轨合并被禁用。

用法::

    py -3.14 vendor/_shot_flac.py
    py -3.14 vendor/_shot_flac.py --out <其它目录>

⚠ 端口 9418：9412~9417 已经被 _shot_state / _v594 / _v595 / _v596 占着。
⚠ 用**独立**的 --user-data-dir：复用同一个 profile 时，Edge 的会话恢复
   会把窗口带到上次的页面，和脚本刚连上的 target 打架（v5.9.6 踩过：
   拍主界面拍成了下载页）。
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

CDP_PORT = 9418
TMP = os.environ.get("TEMP") or r"C:\Windows\Temp"
#: 每次都用新 profile：见文件头第 2 条坑。
PROFILE = os.path.join(TMP, "rml_v597_shot_profile")


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

    out = os.path.abspath(arg("--out", os.path.join(
        ROOT, "..", "deliverables", "v5.9.7-web")))

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
                                        ensure_ascii=False)[:300]
        return res.get("result", {}).get("value")

    def targets():
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json" % CDP_PORT, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))

    edge = find_edge()
    if not edge:
        print("找不到 Edge，无法拍图", flush=True)
        return 1

    httpd, port, _state = server.serve(port=0)
    print("服务端口 %d" % port, flush=True)

    shutil.rmtree(PROFILE, ignore_errors=True)
    os.makedirs(PROFILE, exist_ok=True)
    url = "http://127.0.0.1:%d/index.html?theme=dark" % port
    proc = subprocess.Popen(
        [edge, "--app=%s" % url, "--window-size=1360,900",
         "--window-position=40,20", "--user-data-dir=%s" % PROFILE,
         "--remote-debugging-port=%d" % CDP_PORT, "--no-first-run",
         "--no-default-browser-check",
         "--autoplay-policy=no-user-gesture-required",
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
                target["webSocketDebuggerUrl"], timeout=25, suppress_origin=True,
                http_proxy_host=None, http_no_proxy=["127.0.0.1"])
            call(ws, 1, "Runtime.evaluate", {"expression": "1",
                                             "returnByValue": True})
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

    def shot(name, clip=None, scale=1):
        mid[0] += 1
        p = {"format": "png"}
        if clip:
            p["clip"] = dict(clip, scale=scale)
            p["captureBeyondViewport"] = True
        r = call(ws, mid[0], "Page.captureScreenshot", p)
        data = r.get("result", {}).get("data")
        if not data:
            print("  截图失败：%s" % name, flush=True)
            return ""
        os.makedirs(out, exist_ok=True)
        path = os.path.join(out, name)
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        print("  截图 %s（%d B）" % (path, os.path.getsize(path)), flush=True)
        return path

    def rect_of(sel):
        js = ("(()=>{const e=document.querySelector('%s');"
              "if(!e) return null;const r=e.getBoundingClientRect();"
              "return JSON.stringify({x:Math.round(r.x),y:Math.round(r.y),"
              "width:Math.round(r.width),height:Math.round(r.height)});})()"
              % sel)
        v = ev(js)
        if not v or str(v).startswith("EXC"):
            return None
        try:
            return json.loads(v)
        except (TypeError, ValueError):
            return None

    def nav(to):
        """同一个 target 换页 —— CDP 连接不用重建。"""
        ev("location.href='%s'" % to)
        time.sleep(2.5)

    shots = []
    try:
        # ---- ① 主界面：三张卡片 ----
        time.sleep(1.5)
        print("\n[1] 主界面右栏", flush=True)
        print("  卡片数：%s" % ev("document.querySelectorAll('#fmtGrid .fmt-card').length"),
              flush=True)
        print("  卡片顺序：%s" % ev(
            "JSON.stringify([...document.querySelectorAll('#fmtGrid .fmt-card')]"
            ".map(c=>c.dataset.fmt))"), flush=True)
        r = rect_of(".inspector")
        if r:
            # 只拍「输出格式」那一块 + 上下一圈，别把频谱面板也切一半
            clip = {"x": r["x"], "y": r["y"], "width": r["width"],
                    "height": min(r["height"], 320)}
            shots.append(shot("主界面-FLAC选项.png", clip, 2))

        # ---- ② 真的点一下 FLAC，验证互斥 ----
        print("\n[2] 点 FLAC，验互斥", flush=True)
        before = ev("JSON.stringify({wav:$('[data-fmt=\"wav\"]')"
                    ".classList.contains('on'),"
                    "flac:$('[data-fmt=\"flac\"]').classList.contains('on')})")
        print("  点之前：%s" % before, flush=True)
        ev("$('[data-fmt=\"flac\"]').click()")
        time.sleep(0.6)
        after = ev("JSON.stringify({wav:$('[data-fmt=\"wav\"]')"
                   ".classList.contains('on'),"
                   "flac:$('[data-fmt=\"flac\"]').classList.contains('on')})")
        print("  点之后：%s" % after, flush=True)
        try:
            a = json.loads(after)
            if a.get("flac") and not a.get("wav"):
                print("  ✓ 互斥生效（WAV 自动取消）", flush=True)
            else:
                print("  ✗ 互斥没生效！", flush=True)
        except (TypeError, ValueError):
            pass
        # 顺便验 collectOpts 真的报 flac
        print("  collectOpts.fmt = %s"
              % ev("(typeof collectOpts==='function'? collectOpts().fmt : 'n/a')"),
              flush=True)
        if r:
            clip = {"x": r["x"], "y": r["y"], "width": r["width"],
                    "height": min(r["height"], 320)}
            shots.append(shot("主界面-FLAC已选.png", clip, 2))

        # ---- ③ 下载页：两个复选框 ----
        print("\n[3] 下载页复选框", flush=True)
        nav("http://127.0.0.1:%d/download.html?theme=dark" % port)
        print("  当前页：%s" % ev("location.pathname"), flush=True)
        # ⚠ 这里**不能**用 $ —— 主界面的 $ 是 app.js 顶层的全局函数，
        #   而 download.js 里那个 $ 是 IIFE 内部的，页面上根本没有全局 $。
        #   用它会得到 ReferenceError，而且后面每一条断言都会静默失败。
        # 下载页要联网拉机种列表，得等控件真的渲染出来再拍。
        for _w in range(20):
            if ev("!!document.getElementById('dlAutoFlac')"):
                break
            time.sleep(0.5)
        print("  控件：%s" % ev(
            "JSON.stringify({wav:!!document.getElementById('dlAutoWav'),"
            "flac:!!document.getElementById('dlAutoFlac'),"
            "merge:!!document.getElementById('dlMerge')})"), flush=True)
        r2 = rect_of(".dl-tool-row.dl-wrap")
        if r2:
            clip = {"x": r2["x"], "y": r2["y"], "width": r2["width"],
                    "height": r2["height"]}
            shots.append(shot("下载页-FLAC选项.png", clip, 2))

        # ---- ④ 勾上 FLAC，验互斥 + 合并被禁用 ----
        print("\n[4] 勾 FLAC", flush=True)
        ev("(()=>{const e=document.getElementById('dlAutoWav');"
           "e.checked=true;e.dispatchEvent(new Event('change',{bubbles:true}));})()")
        time.sleep(0.8)
        print("  先勾 WAV：%s" % ev(
            "JSON.stringify({wav:document.getElementById('dlAutoWav').checked,"
            "flac:document.getElementById('dlAutoFlac').checked,"
            "mergeOff:document.getElementById('dlMerge').disabled})"), flush=True)
        ev("(()=>{const e=document.getElementById('dlAutoFlac');"
           "e.checked=true;e.dispatchEvent(new Event('change',{bubbles:true}));})()")
        time.sleep(0.8)
        st2 = ev("JSON.stringify({wav:document.getElementById('dlAutoWav').checked,"
                 "flac:document.getElementById('dlAutoFlac').checked,"
                 "mergeOff:document.getElementById('dlMerge').disabled})")
        print("  再勾 FLAC：%s" % st2, flush=True)
        try:
            b = json.loads(st2)
            if b.get("flac") and not b.get("wav"):
                print("  ✓ 互斥生效（WAV 自动取消）", flush=True)
            else:
                print("  ✗ 互斥没生效！", flush=True)
            if b.get("mergeOff"):
                print("  ✓ 多轨合并已禁用（FLAC 下不支持）", flush=True)
            else:
                print("  ✗ 多轨合并没被禁用", flush=True)
        except (TypeError, ValueError):
            pass
        # 后端真的收到了吗
        print("  后端 opts：%s" % ev(
            "fetch('/api/dl/state').then(r=>r.json())"
            ".then(d=>JSON.stringify(d.opts))", wait=True), flush=True)
        if r2:
            clip = {"x": r2["x"], "y": r2["y"], "width": r2["width"],
                    "height": r2["height"]}
            shots.append(shot("下载页-FLAC已选.png", clip, 2))

        print("\nJS 错误：%s" % ev("JSON.stringify(window.__errs||[])"), flush=True)
    finally:
        try:
            ws.close()
        except Exception:                                        # noqa: BLE001
            pass
        proc.terminate()
        httpd.shutdown()
        shutil.rmtree(PROFILE, ignore_errors=True)

    print("\n拍到 %d 张" % len(shots), flush=True)
    return 0 if len(shots) >= 4 else 1


if __name__ == "__main__":
    sys.exit(main())
