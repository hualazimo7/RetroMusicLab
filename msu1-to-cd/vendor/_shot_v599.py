#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_shot_v599.py — 拍 v5.9.9 的交付图 + 真点两颗转换按钮。

要证明的事：

  v5.9.9 把下载页那两个「下载后自动转 WAV / FLAC」复选框换成了**两颗独立
  按钮**（在「上传云盘」旁边），并且修了「已转 WAV 就跳过 FLAC」的 bug。
  这两件事都只有真点一次才算数 —— 静态看按钮在那儿，点下去可能是灰的、
  可能报 404、可能后端收到了却没写对目录。

所以这个脚本会：

  1. 铺一个真实的音源包到临时保存目录（11 首，别再多：开发机有批量删除
     护栏，一次落盘超过 50 个临时文件会在收尾时被掐掉）；
  2. 起服务 + Edge，打开下载页；
  3. 勾选这一行 → 拍图（两颗按钮从灰变亮）；
  4. **真点**「转 WAV」→ 等日志出现输出目录 → 拍图；
  5. **真点**「转 FLAC」→ 等日志 → 拍图（证明已转 WAV 之后还能转 FLAC）；
  6. 核对两个目录的产物、状态列文案。

用法::

    py -3.14 vendor/_shot_v599.py
    py -3.14 vendor/_shot_v599.py --keep

⚠ 端口 9421（9412~9420 已被其它 _shot_* / _probe_* 占着）。
⚠ 全程落在 %TEMP%，prefs 用 MSU1_DL_PREFS 重定向，不碰用户配置。
⚠ 下载页一律用 ``document.getElementById`` —— 主界面那个 ``$`` 在 app.js
  顶层，CDP 能访问；而 download.js 里的 ``$`` 在 IIFE 内部，页面上没有
  全局 ``$``，用了会 ``ReferenceError`` 且后续断言静默失败。
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

CDP_PORT = 9421
TMP = os.environ.get("TEMP") or r"C:\Windows\Temp"
BASE = os.path.join(TMP, "rml_v599_shot")
PROFILE = os.path.join(BASE, "profile")
DEST = os.path.join(BASE, "dest")
GAME = "Aerial Assault [GG]"
SRC = (r"D:\MSU1Downloads\joshw\MSX"
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

    keep = "--keep" in sys.argv
    out = os.path.abspath(arg("--out", os.path.join(
        ROOT, "..", "deliverables", "v5.9.9-web")))

    os.makedirs(BASE, exist_ok=True)
    os.environ["MSU1_DL_PREFS"] = os.path.join(BASE, "dl_prefs.json")

    import websocket                                             # noqa: E402
    from web_ui import dl_api, server                            # noqa: E402
    from web_ui.shot import call, find_edge                      # noqa: E402

    if not os.path.isdir(SRC):
        print("素材不存在：%s" % SRC, flush=True)
        return 1

    # ── 铺素材：模拟「已下载并解压好」 ──────────────────────
    game_dir = os.path.join(DEST, GAME)
    os.makedirs(game_dir, exist_ok=True)
    copied = []
    for n in sorted(os.listdir(SRC)):
        s = os.path.join(SRC, n)
        if os.path.isfile(s):
            d = os.path.join(game_dir, n)
            shutil.copy2(s, d)
            copied.append(d)
    print("铺了 %d 个源文件" % len(copied), flush=True)

    # ── 注入一个只有一条的下载状态（不联网扫平台） ──────────
    url = "https://x.invalid/%s.7z" % GAME
    st = dl_api.DownloadState()
    st.set_dest(DEST)
    st.key = "msx"
    st.entries = [{"name": GAME, "size": 1, "mtime": 1700000000, "url": url}]
    st.sel = {url}
    server._DL = st                    # 让 dl_state() 用我们这份

    httpd, port, _state = server.serve(port=0)
    print("服务已起：127.0.0.1:%d" % port, flush=True)

    edge = find_edge()
    if not edge:
        print("找不到 Edge", flush=True)
        httpd.shutdown()
        return 1

    os.makedirs(PROFILE, exist_ok=True)
    page = "http://127.0.0.1:%d/download.html?theme=dark" % port
    proc = subprocess.Popen(
        [edge, "--app=%s" % page, "--window-size=1360,900",
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
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json" % CDP_PORT, timeout=3) as r:
                for t in json.loads(r.read().decode("utf-8")):
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
        r = call(ws, mid[0], "Runtime.evaluate",
                 {"expression": js, "returnByValue": True,
                  "awaitPromise": wait, "userGesture": True})
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return "EXC: " + json.dumps(res["exceptionDetails"],
                                        ensure_ascii=False)[:300]
        return res.get("result", {}).get("value")

    def shot(name, clip=None):
        """clip 收两种写法：{"x","y","width","height"} 字典，或 [x, y, w, h] 列表。

        CDP 的 clip 只认字典，列表是页面上 getBoundingClientRect 算出来顺手
        传下来的，所以这里统一转一道 —— 之前直接 dict(clip) 会抛
        TypeError: object is not iterable。
        """
        mid[0] += 1
        p = {"format": "png"}
        if clip:
            if isinstance(clip, dict):
                p["clip"] = dict(clip, scale=2)
            else:
                x, y, w, h = clip[:4]
                p["clip"] = {"x": float(x), "y": float(y),
                             "width": float(w), "height": float(h), "scale": 2}
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

    shots = []
    try:
        # 等列表渲染出这一行
        t0 = time.time()
        n_row = 0
        while time.time() - t0 < 30:
            time.sleep(0.7)
            n_row = ev("document.querySelectorAll('#dlRows .dl-row').length")
            if isinstance(n_row, int) and n_row >= 1:
                break
        print("\n列表行数：%s" % n_row, flush=True)
        if not n_row:
            print("列表没渲染出来，放弃", flush=True)
            return 1

        print("\n=== 1. 两颗按钮存在、且勾选后可用 ===", flush=True)
        info = ev("""(()=>{
          const w=document.getElementById('dlConvWav');
          const f=document.getElementById('dlConvFlac');
          const old=document.getElementById('dlAutoWav')||document.getElementById('dlAutoFlac');
          const sel=document.querySelectorAll('#dlRows .dl-row.on').length;
          return {hasWav:!!w,hasFlac:!!f,wTxt:w&&w.textContent.trim(),
                  fTxt:f&&f.textContent.trim(),
                  wOff:w&&w.disabled,fOff:f&&f.disabled,
                  oldGone:!old,selRows:sel,
                  afterCloud:!!(w&&document.getElementById('dlCloud')&&
                    (document.getElementById('dlCloud').compareDocumentPosition(w)
                     & Node.DOCUMENT_POSITION_FOLLOWING))};
        })()""")
        print("  %s" % json.dumps(info, ensure_ascii=False), flush=True)

        # 勾选这一行。下载页行内**没有复选框**，选择状态就是行上的 .on 类
        # （点击整行触发 toggleRow），所以只能点行本身；已经 .on 就别点，
        # 点了反而取消选择。
        ev("""(()=>{const r=document.querySelector('#dlRows .dl-row:not(.on)');
               if(r) r.click();})()""")
        time.sleep(1.2)

        info2 = ev("""(()=>{
          const w=document.getElementById('dlConvWav');
          const f=document.getElementById('dlConvFlac');
          return {wOff:w.disabled,fOff:f.disabled,
                  selRows:document.querySelectorAll('#dlRows .dl-row.on').length};
        })()""")
        print("  勾选后：%s" % json.dumps(info2, ensure_ascii=False), flush=True)

        # 截工具条那一块。页面上一共有 4 个 .dl-tool-row，按钮在**最后一个**
        # 里，按下标取容易取错，直接由按钮反查父节点。
        bar = ev("""(()=>{const b=document.getElementById('dlConvWav');
                       if(!b) return null;
                       const r=b.parentElement.getBoundingClientRect();
                       return [Math.round(r.x)-8,Math.round(r.y)-8,
                               Math.round(r.width)+16,Math.round(r.height)+16];})()""")
        shots.append(shot("下载页-两个转换按钮.png",
                          bar if isinstance(bar, list) else None))

        print("\n=== 2. 真点「转 WAV」 ===", flush=True)
        ev("document.getElementById('dlConvWav').click()")
        t0 = time.time()
        log_txt = ""
        while time.time() - t0 < 120:
            time.sleep(1.5)
            log_txt = ev("document.getElementById('dlLog').textContent || ''")
            if isinstance(log_txt, str) and ("转 WAV 完成" in log_txt
                                             or "转 WAV 输出目录" in log_txt):
                break
        tail = (log_txt or "")[-300:].replace("\n", " | ")
        print("  日志尾部：%s" % tail, flush=True)
        wav_dir = os.path.join(DEST, "WAV", GAME)
        n_wav = len([n for n in os.listdir(wav_dir) if n.lower().endswith(".wav")]) \
            if os.path.isdir(wav_dir) else 0
        print("  WAV/ 下 %d 个 .wav" % n_wav, flush=True)
        shots.append(shot("下载页-转WAV日志.png"))

        print("\n=== 3. 真点「转 FLAC」（已转 WAV 之后） ===", flush=True)
        ev("document.getElementById('dlConvFlac').click()")
        t0 = time.time()
        log_txt2 = ""
        while time.time() - t0 < 180:
            time.sleep(1.5)
            log_txt2 = ev("document.getElementById('dlLog').textContent || ''")
            if isinstance(log_txt2, str) and "转 FLAC 完成" in log_txt2:
                break
        tail2 = (log_txt2 or "")[-400:].replace("\n", " | ")
        print("  日志尾部：%s" % tail2, flush=True)
        flac_dir = os.path.join(DEST, "FLAC", GAME)
        n_flac = len([n for n in os.listdir(flac_dir) if n.lower().endswith(".flac")]) \
            if os.path.isdir(flac_dir) else 0
        print("  FLAC/ 下 %d 个 .flac" % n_flac, flush=True)

        print("\n=== 4. 状态列 / 目录结构 ===", flush=True)
        # ⚠ 别看到「转 FLAC 完成」就立刻读状态列 —— ``_finish()`` 是**先写日志、
        #   后跑 _refresh_disk() 扫盘**，扫盘没走完时 _wav_games/_flac_games 还是
        #   上一轮的快照，状态列会停在「🎵 已转 WAV」。这里等它收敛。
        rowst = ""
        t0 = time.time()
        while time.time() - t0 < 20:
            rowst = ev("""(()=>{const r=document.querySelector('#dlRows .dl-row');
                           const c=r&&r.querySelector('.c-state');
                           return c?c.textContent.trim():'';})()""")
            if isinstance(rowst, str) and "FLAC" in rowst:
                break
            time.sleep(0.8)
        print("  状态列：%s" % rowst, flush=True)
        # 状态列收敛之后再拍全页图 —— 这张图要同时说明「FLAC 日志跑完了」
        # 和「状态列认两种格式」，拍早了状态列还是旧的。
        shots.append(shot("下载页-转FLAC日志.png"))
        errs = ev("JSON.stringify(window.__errs||[])")
        print("  JS 异常：%s" % errs, flush=True)

        print("\n--- 结论 ---", flush=True)
        print("  WAV 文件 %d 个 / FLAC 文件 %d 个" % (n_wav, n_flac), flush=True)
        print("  两颗按钮存在：%s" % (info.get("hasWav") and info.get("hasFlac")
                                    if isinstance(info, dict) else "?"), flush=True)
        print("  旧复选框已删：%s" % (info.get("oldGone")
                                    if isinstance(info, dict) else "?"), flush=True)
        print("  按钮在「上传云盘」之后：%s" % (info.get("afterCloud")
                                            if isinstance(info, dict) else "?"), flush=True)
        print("  状态列：%s" % rowst, flush=True)

    finally:
        try:
            ws.close()
        except Exception:                                        # noqa: BLE001
            pass
        proc.terminate()
        httpd.shutdown()
        if not keep:
            for p in copied:
                try:
                    os.remove(p)
                except OSError:
                    pass
            for sub in ("WAV", "FLAC"):
                root = os.path.join(DEST, sub)
                g = os.path.join(root, GAME)
                try:
                    for n in os.listdir(g):
                        try:
                            os.remove(os.path.join(g, n))
                        except OSError:
                            pass
                    os.rmdir(g)
                    os.rmdir(root)
                except OSError:
                    pass
            try:
                os.rmdir(game_dir)
            except OSError:
                pass
            try:
                os.remove(os.path.join(BASE, "dl_prefs.json"))
            except OSError:
                pass

    if shots:
        print("\n截图：", flush=True)
        for s in shots:
            if s:
                print("  %s" % s, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
