#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_shot_v596.py — 拍 v5.9.6 的交付图（频谱 / 波形面板，两页都要）。

要证明的事：

  v5.9.6 把「单曲最长」「速度补偿」两块删了，腾出的位置换成一块
  实时频谱 + 波形。用户后来补了一句：「同样的可视化图也要加到下载
  音源界面里去」—— 所以下载页的试听区也得拍。**只有界面能证明它真的
  在动** —— 一张静止的图和「画了个渐变背景」分不出来。所以这个脚本会：

    1. 真转一个包（MSX 11 首，约 20 秒，别再多：开发机有批量删除护栏，
       一次落盘超过 50 个临时文件会在收尾时被掐掉）；
    2. 真播第一首；
    3. 隔 1 秒拍两张，比对 canvas 的像素是否**不一样**（在动 vs 死图）；
    4. 关掉开关再拍一张，证明「可选」不是嘴上说说；
    5. 把两个转好的 WAV 复制进 WAV 根目录，切到 download.html，
       真播一首，再拍两张（下载页也有同一块面板）。

用法::

    py -3.14 vendor/_shot_v596.py
    py -3.14 vendor/_shot_v596.py --src "<其它包>" --keep

⚠ 这个脚本会真写盘（11 首 ≈ 60 MB），全部落在 %TEMP%，拍完就删。
⚠ 端口 9417：9412~9416 已经被 _shot_state / _v594 / _v595 占着。
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

CDP_PORT = 9417
TMP = os.environ.get("TEMP") or r"C:\Windows\Temp"
PROFILE = os.path.join(TMP, "rml_v596_shot_profile")

DEFAULT_SRC = (r"D:\MSU1Downloads\joshw\MSX"
               r"\Aerial Assault (1992-06-05)(SIMS)(Sega)[GG]")


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
                                                    "v5.9.6-web")))
    keep = "--keep" in sys.argv
    if not os.path.isdir(src):
        print("音源目录不存在：%s" % src, flush=True)
        return 1

    import websocket                                             # noqa: E402
    from web_ui import server                                    # noqa: E402
    from web_ui.shot import call, find_edge                      # noqa: E402

    def evaluate(ws, mid, js, await_promise=False):
        """和 shot.evaluate 一样，但带 userGesture —— 不带的话
        Chrome 会把 audio.play() 和 AudioContext 一起挂起，
        拍出来频谱永远是平的，等于什么都没验证。"""
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

    def api(path, body=None):
        url = "http://127.0.0.1:%d%s" % (port, path)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode() or "{}")

    tmp_out = os.path.join(TMP, "rml_v596_shot_out")
    shutil.rmtree(tmp_out, ignore_errors=True)

    r = api("/api/add_sources", {"paths": [src]})
    print("加音源：added=%s" % r.get("added"), flush=True)
    st = api("/api/state")
    print("曲目数：%d" % len(st.get("tracks") or []), flush=True)

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

    def canvas_sig(sel="#vizCanvas"):
        """canvas 的像素指纹 —— 两次不同就说明真的在重绘（不是一张死图）"""
        return ev("(()=>{const c=document.querySelector('%s');"
                  "if(!c) return 'no-canvas';"
                  "const d=c.getContext('2d')"
                  ".getImageData(0,0,c.width,c.height).data;"
                  "let h=0; for(let i=0;i<d.length;i+=97){h=(h*31+d[i])|0;}"
                  "return h+'/'+c.width+'x'+c.height;})()" % sel)

    def nav(url):
        """同一个 target 换页 —— 比开新 tab 简单，且 CDP 连接不用重建。"""
        mid[0] += 1
        call(ws, mid[0], "Page.navigate", {"url": url})

    shots = []
    demo = ""          # 下载页试听用的临时素材目录，finally 里清掉
    demo_files = []    # 只删自己复制进去的文件 —— 目录可能本来就有同名游戏
    try:
        # 等列表渲染
        t0 = time.time()
        n_row = 0
        while time.time() - t0 < 40:
            time.sleep(0.8)
            n_row = ev("document.querySelectorAll('#trackBody .tr').length")
            if isinstance(n_row, int) and n_row >= 5:
                break
        print("列表行数：%s" % n_row, flush=True)

        # ① 空态：还没播过任何东西
        print("JS 错误：%s" % (ev("JSON.stringify(window.__errs||[])"),), flush=True)
        print("可视化开关：%s" % ev("VIZ.on"), flush=True)
        print("面板尺寸：%s" % ev("JSON.stringify(document.querySelector"
                                  "('#vizCanvas').getBoundingClientRect())"),
              flush=True)
        shots.append(shot("频谱-空态.png"))

        # ② 真转一个包
        print("开始转换…", flush=True)
        api("/api/start", {"opts": {"out_dir": tmp_out, "do_wav": True,
                                    "do_cd": False}})
        t0 = time.time()
        last = ""
        while time.time() - t0 < 420:
            time.sleep(1.0)
            st = api("/api/state")
            cur = "%s %.0f%% %s" % (st.get("state"), st.get("progress") or 0,
                                    st.get("status") or "")
            if cur != last:
                print("  %s | %s" % (
                    cur, ev("location.pathname + ' S=' + typeof S +"
                            " ' VIZ=' + typeof VIZ")), flush=True)
                last = cur
            if st.get("state") in ("done", "failed", "cancelled"):
                break
        print("  转换后页面：%s" % ev("location.pathname + ' S=' + typeof S +"
                                    " ' VIZ=' + typeof VIZ +"
                                    " ' rows=' + document.querySelectorAll"
                                    "('#trackBody .tr').length"), flush=True)
        st = api("/api/state")
        n_out = len(st.get("outputs") or [])
        print("转换结束：%s / 输出 %d 个 / 错误 %r"
              % (st.get("state"), n_out, st.get("error")), flush=True)
        if n_out < 1:
            print("没有输出，拍不了播放中的图", flush=True)
            return 1

        # ③ 切到「输出」页签再播 —— 中间那张表列出刚转好的 WAV，
        #    比停在「曲目清单」更能说明「这东西真能用」。
        ev("(()=>{S.tab='outputs'; renderTable();})()")
        time.sleep(0.4)

        # ③ 真播第一首
        print("播放：%s" % ev("(()=>{const o=S.outputs[0];"
                              "playOutput(o.id); return o.name;})()"), flush=True)
        time.sleep(2.5)

        info = ev("(()=>({paused:audio.paused, t:+audio.currentTime.toFixed(2),"
                  "dur:+(audio.duration||0).toFixed(1),"
                  "ctx:VIZ.ctx?VIZ.ctx.state:'none',"
                  "an:!!VIZ.an, raf:!!VIZ.raf, on:VIZ.on,"
                  "touched:VIZ.touched,"
                  "empty:document.querySelector('#vizEmpty').hidden}))()")
        print("播放状态：%s" % json.dumps(info, ensure_ascii=False), flush=True)
        print("canvas 指纹 #1：%s" % canvas_sig(), flush=True)
        time.sleep(1.1)
        sig2 = canvas_sig()
        print("canvas 指纹 #2：%s" % sig2, flush=True)

        # ④ 满窗 + 特写
        shots.append(shot("主界面-频谱在动.png"))
        rect = ev("(()=>{const r=document.querySelector('#vizWrap')"
                  ".getBoundingClientRect();"
                  "return {x:r.x,y:r.y,width:r.width,height:r.height};})()")
        if isinstance(rect, dict) and rect.get("width"):
            pad = 6
            shots.append(shot("频谱特写.png", clip={
                "x": max(0, rect["x"] - pad), "y": max(0, rect["y"] - pad),
                "width": rect["width"] + pad * 2,
                "height": rect["height"] + pad * 2,
            }, scale=2))
        else:
            print("拿不到 #vizWrap 的 rect：%r" % (rect,), flush=True)

        # ⑤ 关掉开关 —— 证明「可选」
        ev("document.querySelector('#vizTog').click()")
        time.sleep(0.8)
        off = ev("(()=>({on:VIZ.on, hidden:document.querySelector"
                 "('#vizWrap').hidden, raf:!!VIZ.raf}))()")
        print("关掉后：%s" % json.dumps(off, ensure_ascii=False), flush=True)
        shots.append(shot("频谱-可关闭.png"))
        # 再打开，验证持久化前的内存态正常
        ev("document.querySelector('#vizTog').click()")
        time.sleep(0.5)
        print("再打开：%s" % ev("JSON.stringify({on:VIZ.on,raf:!!VIZ.raf})"),
              flush=True)
        print("JS 错误（主界面）：%s"
              % (ev("JSON.stringify(window.__errs||[])"),), flush=True)

        # ⑥ 下载页试听区 —— 用户第二次要求：「同样的可视化图也要加到
        #    下载音源界面里去」。这块必须真播才拍得出东西，所以先把刚转
        #    好的两个 WAV 复制进 WAV 根目录，让试听列表能扫到。
        print("\n[下载页] 准备试听素材…", flush=True)
        wav_root = ""
        try:
            wav_root = server.dl_state()._wav_dir_now()
        except Exception as e:                                   # noqa: BLE001
            print("  拿不到 WAV 根目录：%r" % (e,), flush=True)
        print("  WAV 根目录：%s" % (wav_root or "（空）"), flush=True)

        demo = os.path.join(wav_root, "Aerial Assault") if wav_root else ""
        got = []
        if demo:
            src_wavs = []
            for dp, _dn, fs in os.walk(tmp_out):
                for n in sorted(fs):
                    if n.lower().endswith(".wav"):
                        src_wavs.append(os.path.join(dp, n))
            src_wavs = src_wavs[:2]
            if src_wavs:
                os.makedirs(demo, exist_ok=True)
                for p in src_wavs:
                    dst = os.path.join(demo, os.path.basename(p))
                    shutil.copy2(p, dst)
                    demo_files.append(dst)
                    got.append(os.path.basename(p))
        print("  复制了 %d 个 WAV 到 %s：%s"
              % (len(got), demo or "（跳过）", got), flush=True)

        if got:
            nav("http://127.0.0.1:%d/download.html?theme=dark" % port)
            time.sleep(3.0)
            n_play = 0
            for _ in range(30):
                time.sleep(0.8)
                n_play = ev("document.querySelectorAll"
                            "('#dlPlayList .dl-play-row').length")
                if isinstance(n_play, int) and n_play >= 1:
                    break
            print("  试听行数：%s" % n_play, flush=True)
            print("  空态隐藏：%s"
                  % ev("document.querySelector('#dlVizEmpty').hidden"),
                  flush=True)
            shots.append(shot("下载页-频谱空态.png"))

            print("  点第一首：%s"
                  % ev("(()=>{const r=document.querySelector"
                       "('#dlPlayList .dl-play-row');"
                       "if(r){r.click(); return r.dataset.rel;} return null;})()"),
                  flush=True)
            time.sleep(3.5)     # 让进度条 / 时长走起来，别拍成 0:00
            dinfo = ev("(()=>{const a=window.__dlAudio, v=window.__dlViz;"
                       "return {paused:a?a.paused:null,"
                       "t:a?+(a.currentTime||0).toFixed(2):null,"
                       "ctx:v&&v.ctx?v.ctx.state:'none',"
                       "raf:!!(v&&v.raf), on:v?v.on:null,"
                       "touched:v?v.touched:null,"
                       "empty:document.querySelector('#dlVizEmpty').hidden};})()")
            print("  下载页播放状态：%s"
                  % json.dumps(dinfo, ensure_ascii=False), flush=True)
            print("  下载页 canvas 指纹 #1：%s" % canvas_sig("#dlVizCanvas"),
                  flush=True)
            time.sleep(1.1)
            print("  下载页 canvas 指纹 #2：%s" % canvas_sig("#dlVizCanvas"),
                  flush=True)

            shots.append(shot("下载页-频谱在动.png"))
            drect = ev("(()=>{const r=document.querySelector('#dlVizWrap')"
                       ".getBoundingClientRect();"
                       "return {x:r.x,y:r.y,width:r.width,height:r.height};})()")
            if isinstance(drect, dict) and drect.get("width"):
                pad = 6
                shots.append(shot("下载页-频谱特写.png", clip={
                    "x": max(0, drect["x"] - pad), "y": max(0, drect["y"] - pad),
                    "width": drect["width"] + pad * 2,
                    "height": drect["height"] + pad * 2,
                }, scale=2))
            print("  下载页 JS 错误：%s"
                  % (ev("JSON.stringify(window.__errs||[])"),), flush=True)
    finally:
        try:
            api("/api/cancel", {})
        except Exception:                                        # noqa: BLE001
            pass
        time.sleep(1.0)
        try:
            ws.close()
        except Exception:                                        # noqa: BLE001
            pass
        proc.terminate()
        httpd.shutdown()
        # ⚠ 只删自己复制的那几个文件，再尝试删空目录 —— 绝不能 rmtree：
        #   "Aerial Assault" 可能本来就是用户转过 WAV 的游戏目录。
        for p in demo_files:
            try:
                os.remove(p)
            except OSError:
                pass
        if demo:
            try:
                os.rmdir(demo)
            except OSError:
                pass
        if not keep:
            shutil.rmtree(tmp_out, ignore_errors=True)

    print("\n拍到 %d 张" % len(shots), flush=True)
    return 0 if len(shots) >= 6 else 1


if __name__ == "__main__":
    sys.exit(main())
