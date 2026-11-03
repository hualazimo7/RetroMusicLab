#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_shot_v594.py — 拍 v5.9.4 的两张交付图。

要证明的两件事都**只在界面上看得见**，光有日志不行：

1. 列表最后一列多了「操作」——`转 WAV` / `＋试听` 两颗行内按钮。
   用户报的原话是「没有入口」，所以交付图必须让人一眼看到入口在哪。
2. 上传云盘多了「选择要上传的游戏」——以前是全量上传，挑不了。

⚠ 这两张图都要求「磁盘上真的有一份下过一部分的目录」，否则列表全是空白，
拍出来正好是这个版本要修的样子。所以先跑::

    python vendor/_shot_state.py --key spc

再把打印出来的 PREFS 传进来::

    python vendor/_shot_v594.py --prefs D:/MSU1Temp/演示样例/dl_prefs.json \
        --out ../deliverables/v5.9.4-web
"""

import base64
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

# ⚠ 端口不能复用 9412（shot.py）/ 9413（_e2e_virt_rows）/ 9414（_e2e_wav_clear）,
#   否则两个脚本同时跑会抢到同一个浏览器实例，测出来的是对方的页面。
CDP_PORT = 9415
PROFILE = os.path.join(os.environ.get("TEMP") or r"C:\Windows\Temp",
                       "rml_v594_shot_profile")


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

    prefs = arg("--prefs", "")
    out = os.path.abspath(arg("--out", os.path.join(ROOT, "..", "deliverables",
                                                    "v5.9.4-web")))
    if prefs:
        os.environ["MSU1_DL_PREFS"] = prefs

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
    from web_ui.server import dl_state                           # noqa: E402
    dl = dl_state()
    # ⚠ 必须**服务端**先 load 一个机种。浏览器打开下载页时不会自己挑机种，
    #   不预置的话列表就是「在左侧选择一个机种」，一颗行内按钮都拍不到
    #   （第一版就是这么拍出一张空图的）。
    key = arg("--key", "spc")
    try:
        dl.load(key)
        t0 = time.time()
        while not dl.entries and time.time() - t0 < 30:
            time.sleep(0.3)
        print("机种 %s：%d 条" % (key, len(dl.entries)), flush=True)
    except Exception as exc:                                     # noqa: BLE001
        print("预置机种失败（不影响后面拍对话框）: %s" % exc, flush=True)
    print("保存目录：%s" % dl.dest, flush=True)
    print("WAV 目录：%s" % dl._wav_dir_now(), flush=True)
    games = dl.wav_games()
    print("已转好的游戏：%s" % [g["game"][:24] for g in games.get("games") or []],
          flush=True)

    os.makedirs(PROFILE, exist_ok=True)
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

    try:
        # 等列表渲染出带按钮的行
        n_btn = 0
        t0 = time.time()
        while time.time() - t0 < 40:
            time.sleep(0.8)
            n_btn = ev("document.querySelectorAll('.dl-row .dl-act').length")
            if isinstance(n_btn, int) and n_btn >= 2:
                break
        print("可见的行内按钮：%s 个" % n_btn, flush=True)
        labels = ev("Array.from(document.querySelectorAll('.dl-row .dl-act'))"
                    ".map(b=>b.textContent).join(' / ')")
        print("按钮文案：%s" % labels, flush=True)
        # 把列表滚到「已解压 / 已转 WAV」那一段，两种按钮同屏
        ev("(()=>{const b=document.querySelector('#dlRows');"
           "if(b) b.scrollTop = %d;})()" % (27 * 4))
        time.sleep(0.8)
        shot("下载页-行内按钮.png")

        # 打开「选择要上传的游戏」
        gjs = json.dumps(games.get("games") or [], ensure_ascii=False)
        ev("window.__shotUpload = CloudUI.upload({"
           "endpoint: '/api/dl/upload_cloud',"
           "dir: %s, games: %s});"
           % (json.dumps(games.get("dir") or "", ensure_ascii=False), gjs))
        time.sleep(2.0)
        n_chk = ev("document.querySelectorAll('.cl-game input').length")
        print("对话框里的游戏数：%s" % n_chk, flush=True)
        # ⚠ 截图专用：把遮罩/对话框的入场动画全部关掉再拍。
        #   ``cl-fade`` / ``cl-pop`` 会给这两层各提一个合成层，CDP 截图
        #   拿到的是**动画起始帧**（opacity 还没到 1）—— 拍出来整个对话框
        #   半透明、底下的表格透出来，看着像样式坏了，其实界面是好的。
        ev("(()=>{const m=document.querySelector('.cl-mask');"
           "if(m){m.style.animation='none';m.style.opacity='1';"
           "m.style.backdropFilter='none';m.style.background='rgba(0,0,0,.72)';}"
           "document.querySelectorAll('.cl-dlg').forEach(d=>{"
           "d.style.animation='none';d.style.opacity='1';"
           "d.style.transform='none';});})()")
        time.sleep(0.5)
        shot("上传云盘-选游戏.png")
    finally:
        try:
            proc.terminate()
        except Exception:                                        # noqa: BLE001
            pass
        httpd.shutdown()
        try:
            ws.close()
        except Exception:                                        # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
