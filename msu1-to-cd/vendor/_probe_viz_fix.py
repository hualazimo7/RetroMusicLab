#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_probe_viz_fix.py — 验证下载页频谱面板「整块不见」已修好。

要证明的事（对应 v5.9.7 的回归）：

  v5.9.6 给下载页加的频谱面板，CSS 里只钉了主界面的 ``#vizCanvas``，
  下载页的 ``#dlVizCanvas`` 没有尺寸约束。viz.js 的 resize() 把量到的
  尺寸 × dpr 写回 canvas 的 width/height 属性，而这两个属性在无 CSS 约束
  时又反过来决定 CSS 尺寸 —— 正反馈，每帧 ×dpr，0.35 秒涨到 32545×16358，
  把 flex 侧栏撑破，面板被 overflow 裁掉，界面上就是「整块不见了」。

  所以这里断言的**不是**「元素存在」，而是「canvas 的实际渲染尺寸合理」：
  上一轮就是因为只验了 canvas 指纹两帧不同（爆掉的 canvas 每帧也在变），
  才让这个 bug 溜过去。

用法::

    py -3.14 vendor/_probe_viz_fix.py
    py -3.14 vendor/_probe_viz_fix.py --keep     # 留着临时目录看现场

⚠ 端口 9420（9412~9419 已被其它 _shot_* / _diag_viz 占着）。
⚠ 全程落在 %TEMP%，prefs 用 MSU1_DL_PREFS 重定向，不碰用户配置。
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

CDP_PORT = 9420
TMP = os.environ.get("TEMP") or r"C:\Windows\Temp"
BASE = os.path.join(TMP, "rml_vizfix")
PROFILE = os.path.join(BASE, "profile")
DEST = os.path.join(BASE, "dest")

SRC_WAVS = [
    (r"D:\MSU1Downloads\joshw\Mega Drive _ Sega CD\WAV"
     r"\Wu Kong Wai Zhuan (1996)(TW Ming)[SMD]\04_Chapter Screen.wav",
     "Wu Kong Wai Zhuan (1996)(TW Ming)[SMD]"),
    (r"D:\MSU1Downloads\joshw\Mega Drive _ Sega CD\WAV"
     r"\Wu Kong Wai Zhuan (1996)(TW Ming)[SMD]\128_Battle Won.wav",
     "Wu Kong Wai Zhuan (1996)(TW Ming)[SMD]"),
]

# 断言阈值：正常 2 倍屏、336 宽的栏位，canvas 属性尺寸上限约 700×260。
# 超过 2000 就一定是那个正反馈又回来了。
MAX_CANVAS = 2000

ok_n = [0]
bad = []


def chk(label, cond, detail=""):
    if cond:
        ok_n[0] += 1
        print("  ✅ %s%s" % (label, ("  " + detail) if detail else ""), flush=True)
    else:
        bad.append(label)
        print("  ❌ %s%s" % (label, ("  " + detail) if detail else ""), flush=True)


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

    keep = "--keep" in sys.argv
    out = os.path.abspath(arg("--out", os.path.join(
        ROOT, "..", "deliverables", "v5.9.8-web")))

    # prefs 重定向到临时目录 —— 绝不碰用户真实的 dl_prefs.json
    os.makedirs(BASE, exist_ok=True)
    os.environ["MSU1_DL_PREFS"] = os.path.join(BASE, "dl_prefs.json")

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

    # ── 铺素材 ──────────────────────────────────────────────
    copied = []
    for src, game in SRC_WAVS:
        if not os.path.isfile(src):
            print("素材缺失：%s" % src, flush=True)
            continue
        d = os.path.join(DEST, "WAV", game)
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, os.path.basename(src))
        shutil.copy2(src, dst)
        copied.append(dst)
    if not copied:
        print("一个素材都没铺上，放弃", flush=True)
        return 1
    print("铺了 %d 个 WAV 到 %s" % (len(copied), DEST), flush=True)

    httpd, port, _state = server.serve(port=0)
    print("服务已起：127.0.0.1:%d" % port, flush=True)

    def api(path, body=None):
        url = "http://127.0.0.1:%d%s" % (port, path)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")

    r = api("/api/dl/set_opts", {"dest": DEST})
    print("设 dest：%s" % r.get("ok"), flush=True)
    w = api("/api/dl/wavs?force=1")
    print("扫到试听曲目：%d 首" % (w.get("total") or 0), flush=True)

    edge = find_edge()
    if not edge:
        print("找不到 Edge", flush=True)
        httpd.shutdown()
        return 1

    os.makedirs(PROFILE, exist_ok=True)
    url = "http://127.0.0.1:%d/download.html?theme=dark" % port
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

    def canvas_sig(sel="#dlVizCanvas"):
        return ev("(()=>{const c=document.querySelector('%s');"
                  "if(!c) return 'no-canvas';"
                  "const d=c.getContext('2d')"
                  ".getImageData(0,0,c.width,c.height).data;"
                  "let h=0; for(let i=0;i<d.length;i+=97){h=(h*31+d[i])|0;}"
                  "return h+'/'+c.width+'x'+c.height;})()" % sel)

    shots = []
    try:
        # 等试听列表渲染出第一行
        t0 = time.time()
        n_row = 0
        while time.time() - t0 < 40:
            time.sleep(0.8)
            n_row = ev("document.querySelectorAll('#dlPlayList .dl-play-row').length")
            if isinstance(n_row, int) and n_row >= 1:
                break
        print("\n试听列表行数：%s" % n_row, flush=True)

        print("\n=== 1. 面板渲染（不播放时） ===", flush=True)
        geo = ev("(()=>{const w=document.getElementById('dlVizWrap');"
                 "const c=document.getElementById('dlVizCanvas');"
                 "const h=document.querySelector('.dl-viz-head');"
                 "if(!w||!c) return null;"
                 "const rw=w.getBoundingClientRect(), rc=c.getBoundingClientRect();"
                 "const rh=h?h.getBoundingClientRect():null;"
                 "return {wrap:[Math.round(rw.x),Math.round(rw.y),"
                 "Math.round(rw.width),Math.round(rw.height)],"
                 "head:rh?[Math.round(rh.x),Math.round(rh.y),"
                 "Math.round(rh.width),Math.round(rh.height)]:null,"
                 "cssW:Math.round(rc.width),cssH:Math.round(rc.height),"
                 "attrW:c.width,attrH:c.height,"
                 "disp:getComputedStyle(c).display,"
                 "emptyHidden:document.getElementById('dlVizEmpty').hidden};})()")
        print("  %s" % json.dumps(geo, ensure_ascii=False), flush=True)

        chk("面板存在且有高度", isinstance(geo, dict) and geo["wrap"][3] > 60,
            "wrap 高 %s" % (geo["wrap"][3] if isinstance(geo, dict) else "?"))
        chk("canvas 的 CSS 尺寸合理（不是 0，也不是被撑爆）",
            isinstance(geo, dict) and 100 < geo["cssW"] < 900
            and 60 < geo["cssH"] < 400,
            "%dx%d" % (geo["cssW"], geo["cssH"]) if isinstance(geo, dict) else "")
        chk("canvas 属性尺寸未被正反馈撑爆",
            isinstance(geo, dict) and geo["attrW"] < MAX_CANVAS
            and geo["attrH"] < MAX_CANVAS,
            "attr %dx%d（上限 %d）" % (geo["attrW"], geo["attrH"], MAX_CANVAS)
            if isinstance(geo, dict) else "")
        chk("canvas 的 CSS 尺寸 ≈ 容器内容盒",
            isinstance(geo, dict)
            and abs(geo["cssW"] - (geo["wrap"][2] - 2)) <= 2
            and abs(geo["cssH"] - (geo["wrap"][3] - 2)) <= 2,
            "canvas %dx%d vs wrap %dx%d" % (
                geo["cssW"], geo["cssH"], geo["wrap"][2], geo["wrap"][3])
            if isinstance(geo, dict) else "")
        chk("canvas display=block（CSS 生效）",
            isinstance(geo, dict) and geo["disp"] == "block",
            geo["disp"] if isinstance(geo, dict) else "")
        chk("空态提示可见（还没播放）",
            isinstance(geo, dict) and geo["emptyHidden"] is False)

        # 截第一张：静态面板
        shots.append(shot("下载页-频谱面板-修复后.png"))

        print("\n=== 2. 真播放一首 ===", flush=True)
        ev("document.querySelectorAll('#dlPlayList .dl-play-row')[0].click()")
        t0 = time.time()
        playing = None
        while time.time() - t0 < 20:
            time.sleep(0.7)
            playing = ev("(()=>{const a=window.__dlAudio,v=window.__dlViz;"
                         "return {paused:a.paused,t:+a.currentTime.toFixed(2),"
                         "dur:+(a.duration||0).toFixed(2),"
                         "raf:!!v.raf,on:v.on,touched:v.touched,"
                         "ctx:v.ctx?v.ctx.state:'null'};})()")
            if isinstance(playing, dict) and not playing["paused"] and playing["t"] > 0.4:
                break
        print("  %s" % json.dumps(playing, ensure_ascii=False), flush=True)

        chk("音频在播（没被暂停）",
            isinstance(playing, dict) and not playing["paused"],
            "t=%s / dur=%s" % (playing.get("t"), playing.get("dur"))
            if isinstance(playing, dict) else "")
        chk("AudioContext 已建立并 running",
            isinstance(playing, dict) and playing["ctx"] == "running",
            str(playing.get("ctx")) if isinstance(playing, dict) else "")
        chk("rAF 循环在跑", isinstance(playing, dict) and playing["raf"])
        chk("空态提示已收起", isinstance(playing, dict) and playing["touched"])

        sig1 = canvas_sig()
        time.sleep(1.0)
        sig2 = canvas_sig()
        print("  指纹 #1：%s" % sig1, flush=True)
        print("  指纹 #2：%s" % sig2, flush=True)
        chk("canvas 在真重绘（两帧像素不同）",
            isinstance(sig1, str) and isinstance(sig2, str)
            and "no-canvas" not in sig1 and sig1 != sig2)

        # 播放中再量一次尺寸 —— 正反馈只在 draw 时发生，必须这时量
        geo2 = ev("(()=>{const c=document.getElementById('dlVizCanvas');"
                  "const w=document.getElementById('dlVizWrap');"
                  "const rc=c.getBoundingClientRect(),rw=w.getBoundingClientRect();"
                  "return {cssW:Math.round(rc.width),cssH:Math.round(rc.height),"
                  "attrW:c.width,attrH:c.height,"
                  "wrapW:Math.round(rw.width),wrapH:Math.round(rw.height)};})()")
        print("  播放中：%s" % json.dumps(geo2, ensure_ascii=False), flush=True)
        chk("播放中 canvas 尺寸依然稳定（没被撑爆）",
            isinstance(geo2, dict) and geo2["attrW"] < MAX_CANVAS
            and geo2["attrH"] < MAX_CANVAS
            and abs(geo2["cssW"] - geo2["wrapW"]) <= 3,
            "attr %dx%d / css %dx%d / wrap %dx%d" % (
                geo2["attrW"], geo2["attrH"], geo2["cssW"], geo2["cssH"],
                geo2["wrapW"], geo2["wrapH"]) if isinstance(geo2, dict) else "")

        # 播放中截第二张：能看到频谱
        shots.append(shot("下载页-频谱播放中-修复后.png"))

        print("\n=== 3. 侧栏没被撑破 ===", flush=True)
        lay = ev("(()=>{const a=document.querySelector('aside.dl-right');"
                 "const m=document.querySelector('.dl-main');"
                 "const w=document.getElementById('dlVizWrap');"
                 "if(!a||!m||!w) return null;"
                 "const ra=a.getBoundingClientRect(),rw=w.getBoundingClientRect();"
                 "return {aside:[Math.round(ra.x),Math.round(ra.width)],"
                 "wrap:[Math.round(rw.x),Math.round(rw.width)],"
                 "docW:document.documentElement.scrollWidth,"
                 "winW:window.innerWidth};})()")
        print("  %s" % json.dumps(lay, ensure_ascii=False), flush=True)
        chk("面板宽度 ≤ 侧栏宽度（没溢出到列外）",
            isinstance(lay, dict) and lay["wrap"][1] <= lay["aside"][1] + 2,
            "wrap %s / aside %s" % (lay["wrap"], lay["aside"])
            if isinstance(lay, dict) else "")
        chk("整页没有横向滚动条（布局没被撑宽）",
            isinstance(lay, dict) and lay["docW"] <= lay["winW"] + 2,
            "docW=%s winW=%s" % (lay.get("docW"), lay.get("winW"))
            if isinstance(lay, dict) else "")

        print("\n=== 4. 主界面也验一遍（同一份 viz.js，别修一个坏一个） ===", flush=True)
        mid[0] += 1
        call(ws, mid[0], "Page.navigate",
             {"url": "http://127.0.0.1:%d/index.html?theme=dark" % port})
        time.sleep(4.0)
        g1 = ev("(()=>{const c=document.getElementById('vizCanvas');"
                "const w=document.getElementById('vizWrap');"
                "if(!c||!w) return null;"
                "const rc=c.getBoundingClientRect(),rw=w.getBoundingClientRect();"
                "return {cssW:Math.round(rc.width),cssH:Math.round(rc.height),"
                "attrW:c.width,attrH:c.height,"
                "wrapW:Math.round(rw.width),wrapH:Math.round(rw.height),"
                "disp:getComputedStyle(c).display};})()")
        print("  %s" % json.dumps(g1, ensure_ascii=False), flush=True)
        chk("主界面 canvas 尺寸正常",
            isinstance(g1, dict) and 100 < g1["cssW"] < 900
            and g1["attrW"] < MAX_CANVAS
            and abs(g1["cssW"] - g1["wrapW"]) <= 3,
            "%dx%d（属性 %dx%d）" % (g1["cssW"], g1["cssH"],
                                    g1["attrW"], g1["attrH"])
            if isinstance(g1, dict) else "")

        errs = ev("JSON.stringify(window.__errs||[])")
        chk("两页都没有 JS 异常", errs == "[]", str(errs))

    finally:
        try:
            ws.close()
        except Exception:                                        # noqa: BLE001
            pass
        proc.terminate()
        httpd.shutdown()
        # 只删自己铺进去的那几个文件，不批量删目录
        for p in copied:
            try:
                os.remove(p)
            except OSError:
                pass
        if not keep:
            for sub in ("WAV",):
                d = os.path.join(DEST, sub)
                try:
                    for g in os.listdir(d):
                        try:
                            os.rmdir(os.path.join(d, g))
                        except OSError:
                            pass
                    os.rmdir(d)
                except OSError:
                    pass
            for p in (os.path.join(BASE, "dl_prefs.json"),):
                try:
                    os.remove(p)
                except OSError:
                    pass

    print("\n" + "=" * 56, flush=True)
    print("结果：%d 项通过，%d 项失败" % (ok_n[0], len(bad)), flush=True)
    for b in bad:
        print("  失败：%s" % b, flush=True)
    if shots:
        print("截图：", flush=True)
        for s in shots:
            print("  %s" % s, flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
