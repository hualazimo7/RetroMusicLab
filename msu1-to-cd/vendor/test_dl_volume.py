# -*- coding: utf-8 -*-
"""vendor/test_dl_volume.py — 下载页播放栏音量的**行为**门禁（真浏览器 + CDP）。

为什么单独一个文件、而不是塞进 `test_web_ui.py`：那边只验后端契约和静态
接线（DOM 里有没有那三个 id），验不了「拖滑块之后 `audio.volume` 到底变没变」。
音量这种东西全靠 JS 行为，静态检查再全也挡不住「滑块在、但拖了没反应」。

跑法::

    python vendor/test_dl_volume.py

需要本机有 Edge。会把窗口拉起来、跑完自己关掉。
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)
for k in [k for k in os.environ if "proxy" in k.lower()]:
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"

import websocket                                              # noqa: E402

from web_ui import server, shot                               # noqa: E402

OK, BAD = 0, 0


def check(name, cond, detail=""):
    global OK, BAD
    if cond:
        OK += 1
        print("  [OK] %s" % name, flush=True)
    else:
        BAD += 1
        print("  [!!] %s  %s" % (name, detail), flush=True)


edge = shot.find_edge()
if not edge:
    print("找不到 Edge")
    raise SystemExit(2)

httpd, port, state = server.serve(port=0)
url = "http://127.0.0.1:%d/download.html?theme=dark" % port
os.makedirs(shot.PROFILE, exist_ok=True)
proc = subprocess.Popen(
    [edge, "--app=%s" % url, "--window-size=1280,840", "--window-position=40,30",
     "--user-data-dir=%s" % shot.PROFILE,
     "--remote-debugging-port=%d" % shot.CDP_PORT,
     "--remote-allow-origins=*",
     "--no-first-run", "--no-default-browser-check",
     "--disable-features=msEdgeSidebarV2,msImplicitSignin,msEdgeCollections"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    target = None
    for _ in range(25):
        time.sleep(1.0)
        try:
            for t in shot.cdp_targets():
                if t.get("type") == "page" and "127.0.0.1" in (t.get("url") or ""):
                    target = t
                    break
        except Exception:                                     # noqa: BLE001
            continue
        if target:
            break
    if not target:
        print("连不上 CDP")
        raise SystemExit(3)

    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=20)
    MID = [0]

    def ev(js, await_promise=False):
        MID[0] += 1
        return shot.evaluate(ws, MID[0], js, await_promise)

    shot.call(ws, 900, "Runtime.enable")
    time.sleep(1.5)

    print("== 控件存在 ==")
    check("#dlVol 滑块在", ev("!!document.querySelector('#dlVol')") is True)
    check("#dlVolBtn 静音键在", ev("!!document.querySelector('#dlVolBtn')") is True)
    check("喇叭图标三条路径齐（喇叭体 + 两道声波 + 叉）",
          ev("document.querySelectorAll('#dlVolBtn svg path').length") == 4,
          ev("document.querySelectorAll('#dlVolBtn svg path').length"))
    check("滑块 min/max = 0/100",
          ev("[document.querySelector('#dlVol').min, document.querySelector('#dlVol').max].join(',')")
          == "0,100")

    print("== 默认值 ==")
    # 先清掉上次留下的值，验证「第一次打开是 80%」
    ev("try{localStorage.removeItem('dl_vol')}catch(e){};location.reload()")
    time.sleep(3.0)
    vol = ev("window.__dlAudio ? window.__dlAudio.volume : -1")
    check("首次打开音量 = 0.8（不是 1.0，别一上来最大声）",
          abs(float(vol) - 0.8) < 1e-6, vol)
    check("滑块同步显示 80", ev("document.querySelector('#dlVol').value") == "80",
          ev("document.querySelector('#dlVol').value"))

    print("== 拖滑块 ==")
    v = ev("""(()=>{const el=document.querySelector('#dlVol');
               el.value='35';
               el.dispatchEvent(new Event('input',{bubbles:true}));
               return window.__dlAudio.volume})()""")
    check("滑块拖到 35 → volume=0.35", abs(float(v) - 0.35) < 1e-6, v)
    check("localStorage 记住 35", ev("localStorage.getItem('dl_vol')") == "35",
          ev("localStorage.getItem('dl_vol')"))
    check("非静音时喇叭叉是隐藏的",
          ev("getComputedStyle(document.querySelector('#dlVolX')).display") == "none",
          ev("getComputedStyle(document.querySelector('#dlVolX')).display"))

    print("== 静音 / 取消静音 ==")
    v = ev("""(()=>{document.querySelector('#dlVolBtn').click();
               return window.__dlAudio.volume})()""")
    check("点静音 → volume=0", abs(float(v)) < 1e-9, v)
    check("静音时按钮带 .muted", ev("document.querySelector('#dlVolBtn').classList.contains('muted')") is True)
    check("静音时喇叭上划叉显示出来",
          ev("getComputedStyle(document.querySelector('#dlVolX')).display") != "none",
          ev("getComputedStyle(document.querySelector('#dlVolX')).display"))
    check("静音时两道声波隐藏",
          ev("getComputedStyle(document.querySelector('#dlVolW1')).display") == "none")
    check("静音时滑块归 0", ev("document.querySelector('#dlVol').value") == "0",
          ev("document.querySelector('#dlVol').value"))
    v = ev("""(()=>{document.querySelector('#dlVolBtn').click();
               return window.__dlAudio.volume})()""")
    check("再点一下恢复 0.35（不是恢复成 1.0）",
          abs(float(v) - 0.35) < 1e-6, v)
    check("恢复后 .muted 去掉",
          ev("document.querySelector('#dlVolBtn').classList.contains('muted')") is False)

    print("== 滚轮微调 ==")
    v = ev("""(()=>{const b=document.querySelector('#dlVolBtn');
               b.dispatchEvent(new WheelEvent('wheel',{deltaY:-100,bubbles:true,cancelable:true}));
               return window.__dlAudio.volume})()""")
    check("向上滚 +5% → 0.40", abs(float(v) - 0.40) < 1e-6, v)

    print("== 刷新后记住 ==")
    ev("location.reload()")
    time.sleep(3.0)
    vol = ev("window.__dlAudio ? window.__dlAudio.volume : -1")
    check("刷新后音量仍是 0.40", abs(float(vol) - 0.40) < 1e-6, vol)

    print("== 页面报错 ==")
    errs = ev("JSON.stringify(window.__errs||[]);")
    check("没有 JS 报错", errs in ("[]", None), errs)

    shot.shot_now(ws, 7000, r"D:\MSU1Temp\dl_volume.png")
    ws.close()
finally:
    try:
        proc.terminate()
    except Exception:                                         # noqa: BLE001
        pass
    httpd.shutdown()

print("\n结果：%d 通过 / %d 失败" % (OK, BAD))
raise SystemExit(1 if BAD else 0)
