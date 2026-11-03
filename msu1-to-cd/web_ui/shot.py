#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shot.py — 起真实后端 + Edge ``--app``，用 CDP 截图 / 驱动交互。

这是开发期的眼睛：没有控制台可看时，只能靠 CDP 把页面状态读出来、
把画面拍下来。

## 为什么必须走 CDP

    PrintWindow           → 对 WebView2/Chromium 的 GPU 合成层只给黑图
    CopyFromScreen        → 本机窗口管理器抢焦点，截到的是别的窗口
    Page.captureScreenshot → 唯一可靠

## 三个已知坑（踩过，别再踩）

1. **系统 HTTP 代理会劫持 CDP 的 WebSocket**。``ws://127.0.0.1`` 被转成
   CONNECT → 502。连之前必须清空所有 ``*proxy*`` 环境变量 + ``NO_PROXY=*``
   + ``http_no_proxy=["127.0.0.1"]``。
2. **页面还在导航时连 WS 会被渲染进程掐断**。要等 target URL 里出现
   ``index.html`` 再连。
3. **页面就绪 ≠ JS 跑完**。连上后先 ``Runtime.evaluate`` 读 ``window.__errs``，
   有错直接打出来，别对着空白页猜。

## 跑法

    python web_ui/shot.py -o D:\\tmp\\a.png
    python web_ui/shot.py --theme light -o D:\\tmp\\light.png
    python web_ui/shot.py --add <音源路径> --add <另一个> -o D:\\tmp\\b.png
    python web_ui/shot.py --interact -o D:\\tmp\\c.png

    # 下载页（v5.1）
    python web_ui/shot.py --page download -o D:\\tmp\\dl.png
    python web_ui/shot.py --page download --interact -o D:\\tmp\\dl.png

    # 6 套复古配色（v5.2）
    python web_ui/shot.py --retro panel -o D:\\tmp\\pop.png      # 只开面板
    python web_ui/shot.py --retro famicom -o D:\\tmp\\fc.png     # 套上那套

``--page`` 缺省是主界面（``/``）；``download`` 走 ``/download.html``。
两个页面的 DOM 完全不同，所以探针和交互都是**按页分派**的。
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web_ui import server                                         # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

CDP_PORT = 9412
PROFILE = os.path.join(os.environ.get("TEMP") or r"C:\Windows\Temp", "rml_shot_profile")


def arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def arg_all(name):
    out = []
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            out.append(sys.argv[i + 1])
    return out


def find_edge():
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


# ══════════════════════════════════════════════════════════════════════
#  CDP
# ══════════════════════════════════════════════════════════════════════
def cdp_targets():
    with urllib.request.urlopen("http://127.0.0.1:%d/json" % CDP_PORT, timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


def call(ws, mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == mid:
            return m


def evaluate(ws, mid, js, await_promise=False):
    r = call(ws, mid, "Runtime.evaluate",
             {"expression": js, "returnByValue": True, "awaitPromise": await_promise})
    res = r.get("result", {})
    if "exceptionDetails" in res:
        return "EXC: " + json.dumps(res["exceptionDetails"], ensure_ascii=False)[:200]
    return res.get("result", {}).get("value")


def shot_now(ws, mid, path):
    """中途再拍一张（比如「转换中」的样子）。"""
    call(ws, mid + 500, "Page.enable")
    shot = call(ws, mid, "Page.captureScreenshot", {"format": "png"})
    data = shot.get("result", {}).get("data")
    if not data:
        return False
    with open(path, "wb") as f:
        f.write(base64.b64decode(data))
    print("  截图: %s (%d B)" % (path, os.path.getsize(path)), flush=True)
    return True


# ══════════════════════════════════════════════════════════════════════
def main():
    # 坑 1：清掉代理，否则 CDP 的 WS 会被转成 CONNECT 然后 502
    for k in [k for k in os.environ if "proxy" in k.lower()]:
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"

    import websocket                                             # noqa: E402

    edge = find_edge()
    if not edge:
        print("找不到 Edge，无法截图", flush=True)
        return 2

    theme = arg("--theme", "dark")
    page = arg("--page", "")
    cloud = arg("--cloud", "")          # login / qr / cookie / scope（见下）
    cloud_dir = arg("--cloud-dir", "")
    # 6 套复古配色：--retro famicom 直接套上；--retro panel 只把弹出面板打开
    # （拍面板本身长什么样）。两者都不改 --theme，深/浅底由 --theme 决定。
    retro = arg("--retro", "")
    out = arg("-o", os.path.join(os.environ.get("TEMP") or ".", "rml_shot.png"))
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    os.makedirs(PROFILE, exist_ok=True)

    httpd, port, state = server.serve(port=0)
    base = "http://127.0.0.1:%d" % port

    # 云盘截图要一个「有 WAV」的目录，否则 plan 直接报「没有可上传的 WAV」
    if cloud_dir:
        state.opts["out_dir"] = cloud_dir
        # 下载页的 WAV 不在「输出目录」，而在 dl_state.wav_dir —— 只设
        # state.opts 的话下载页那颗按钮照样会报「没有可上传的 WAV」
        try:
            from web_ui.server import dl_state
            dl_state().wav_dir = cloud_dir
        except Exception:                    # noqa: BLE001 截图而已，失败不致命
            pass

    # 下载页截图要一个**已经选好机种**的列表，否则拍到的是「这个机种还没有条目」。
    # 预置 = 直接调 DownloadState.load()，走缓存/内置快照，不联网（除非缓存过期）。
    dl_key = arg("--dl-key", "")
    # v6.0.1：封面区要 `<保存目录>/FLAC/<游戏>/cover.png` 才拍得出图，
    # 而那个目录只有 ``dl.dest`` 知道。**只在内存里改，不落盘** —— 截图脚本
    # 不该把用户的保存目录偏好改掉。
    dl_dest = arg("--dl-dest", "")
    if dl_dest:
        try:
            from web_ui.server import dl_state
            st0 = dl_state()
            st0.dest = dl_dest
            # ⚠ 光赋值没用：``load(key)`` 里有一句「用户没手改过保存目录就按
            #   机种重算」，点一下机种就把 dest 冲掉了（实测：拍出来的封面区
            #   一直显示 ♪ 占位，因为查的是 ``<默认目录>/FLAC/<游戏>/cover.png``）。
            #   置这个标记 = 冒充「用户手选过」，只改内存，不落盘 ——
            #   截图脚本不该动用户的 dl_prefs.json。
            st0._dest_manual = True
            print("  预置保存目录 → %s" % dl_dest, flush=True)
        except Exception as exc:                     # noqa: BLE001
            print("  预置保存目录失败（不影响截图）: %s" % exc, flush=True)
    if dl_key:
        try:
            from web_ui.server import dl_state
            st = dl_state()
            print("  预置下载页机种 %s → %s" % (dl_key, st.load(dl_key)), flush=True)
            if arg("--dl-select", ""):
                # 顺手勾上前 N 条，拍出来才有「已选」的样子
                n = int(arg("--dl-select", "0") or 0)
                urls = [e["url"] for e in st.entries[:max(0, n)]]
                print("  勾选 %d 条 → %s" % (len(urls), st.select(urls, "set")),
                      flush=True)
        except Exception as exc:                     # noqa: BLE001 截图而已
            print("  预置下载页失败（不影响截图）: %s" % exc, flush=True)

    # 拍「真实下载 → 解压 → 转 FLAC + 抓封面」的日志（v5.9.14）。
    #
    # 为什么要在这里**同步干完再起 Edge**：页面加载后第一轮 poll 会把整段
    # 日志一次性渲染出来（``logs_since(0)``），截图自然就是「完成态」。
    # 反过来在页面里点按钮、再等，就得处理一堆异步和滚动位置，得不偿失。
    #
    # 需要配合 ``--dl-key`` 指定机种。挑**体积最小**的那几个 —— 太小的
    # 是空壳（实测 gbs 里有 340 B 的包，解出来一个文件都转不了），
    # 所以设了 20 KB 下限。
    dl_try = arg("--dl-try", "")
    if dl_try and dl_key:
        try:
            from web_ui.server import dl_state
            st = dl_state()
            n = max(1, int(dl_try))
            pool = sorted([e for e in st.entries if e.get("url")],
                          key=lambda e: e.get("size") or 0)
            pool = [e for e in pool if (e.get("size") or 0) >= 20000] or pool
            urls = [e["url"] for e in pool[:n]]
            # 直接赋 ``dest`` 而不是 ``set_dest()``：截图不该把保存目录
            # 写进 ``dl_prefs.json``（那会让用户下次打开跳到临时目录）。
            st.dest = os.path.join(os.environ.get("TEMP") or ".", "rml_shot_cover")
            st.opts.update({"auto_extract": True, "auto_flac": True,
                            "auto_wav": False, "merge_wav": False,
                            "auto_cover": True, "workers": 2})
            st.select(urls, "set")
            print("  真实下载 %d 个 → %s" % (len(urls), st.dest), flush=True)
            print("  start → %s" % (st.start(),), flush=True)
            t0 = time.time()
            while time.time() - t0 < 300:
                time.sleep(2)
                # ⚠ ``cv_skip`` 也要算「跑完了」：重复跑同一张截图时包和
                #   FLAC 都已经在盘上，转换会全部跳过、``cv_ok`` 恒为 0，
                #   只看 ``cv_ok`` 就会空等满 300 秒（实测踩过）。
                if (not st.busy and st.phase in ("done", "idle")
                        and (st.cv_ok >= 1 or st.cv_skip >= 1)):
                    break
                if st.phase == "error":
                    break
            print("  结束：dl_ok=%d cv_ok=%d cover_ok=%d cover_fail=%d（%.0f 秒）"
                  % (st.dl_ok, st.cv_ok, st.cover_ok, st.cover_fail,
                     time.time() - t0), flush=True)
        except Exception as exc:                     # noqa: BLE001 截图而已
            print("  --dl-try 失败（不影响截图）: %s" % exc, flush=True)

    # 预置音源（截图要拍真实数据，不能是空壳）
    seeds = arg_all("--add")
    if seeds:
        added, skipped = state.add_paths(seeds)
        for p, why in skipped:
            print("  跳过 %s（%s）" % (os.path.basename(p), why), flush=True)
        print("预置音源 %d 个" % added, flush=True)

    # page 为空 → 主界面 "/"；"download" → "/download.html"
    path = "/" if not page else "/" + (
        page if page.endswith(".html") else page + ".html")
    url = "%s%s?theme=%s" % (base, path, theme)
    print("server: %s" % url, flush=True)

    args = [
        edge,
        "--app=%s" % url,
        "--window-size=1280,840",
        "--window-position=40,30",
        "--user-data-dir=%s" % PROFILE,
        "--remote-debugging-port=%d" % CDP_PORT,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=msEdgeSidebarV2,msImplicitSignin,msEdgeCollections",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Edge --app 已拉起, pid=%d" % proc.pid, flush=True)

    # 坑 2：等导航完成再连 WS
    #
    # ⚠ 这个变量**必须**叫 target，不能叫 page —— 上面那个 ``page`` 是
    #   「拍哪个页面」的**开关**（空 = 主界面 / "download" = 下载页），
    #   下面好几处 ``if page:`` 都靠它分派。早先这里复用了同名变量装
    #   CDP target 字典，于是 page 恒为真：状态探针、云盘按钮、
    #   ``--interact``、``--convert`` 全部走进「下载页」分支 ——
    #   主界面截图看着正常，其实探测和交互全都没跑。
    target = None
    for i in range(25):
        time.sleep(1.0)
        try:
            for t in cdp_targets():
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

    time.sleep(1.2)
    ws = None
    for a in range(4):
        try:
            ws = websocket.create_connection(
                target["webSocketDebuggerUrl"], timeout=20, suppress_origin=True,
                http_proxy_host=None, http_no_proxy=["127.0.0.1"])
            call(ws, 1, "Runtime.evaluate", {"expression": "1", "returnByValue": True})
            print("WebSocket 连上（第 %d 次）" % (a + 1), flush=True)
            break
        except Exception as exc:                                 # noqa: BLE001
            print("WS 第 %d 次失败: %s" % (a + 1, exc), flush=True)
            time.sleep(2)
            ws = None
    if ws is None:
        proc.terminate()
        httpd.shutdown()
        return 1

    # 坑 3：先读错误探针，有错立刻打出来
    time.sleep(1.0)
    if page:
        # 下载页：机种列表 / 条目行 / 日志
        expr = ("JSON.stringify({ready:document.readyState,title:document.title,"
                "errs:window.__errs||[],conn:(document.querySelector('#dlConn')||{}).textContent,"
                "grps:document.querySelectorAll('.dl-grp').length,"
                "plats:document.querySelectorAll('.dl-plat-item').length,"
                "rows:document.querySelectorAll('.dl-row').length,"
                "empty:document.querySelectorAll('.dl-empty').length,"
                "logs:document.querySelectorAll('#dlLog > div').length,"
                "playRows:document.querySelectorAll('.dl-play-row').length,"
                "playCount:(document.querySelector('#dlPlayCount')||{}).textContent,"
                "playName:(document.querySelector('#dlPlayName')||{}).textContent,"
                "count:(document.querySelector('#dlCount')||{}).textContent,"
                "status:(document.querySelector('#dlStatus')||{}).textContent,"
                "bg:getComputedStyle(document.body).backgroundColor,"
                "vp:innerWidth+'x'+innerHeight})")
    else:
        expr = ("JSON.stringify({ready:document.readyState,title:document.title,"
                "errs:window.__errs||[],conn:(document.querySelector('#conn')||{}).textContent,"
                "srcs:document.querySelectorAll('.src').length,"
                "rows:document.querySelectorAll('.tr').length,"
                "bars:document.querySelectorAll('#wave i').length,"
                "empty:document.querySelectorAll('.empty').length,"
                "bg:getComputedStyle(document.body).backgroundColor,"
                "vp:innerWidth+'x'+innerHeight})")
    val = evaluate(ws, 2, expr)
    print("--- 页面状态 ---", flush=True)
    if isinstance(val, str):
        for k, v in json.loads(val).items():
            print("  %-7s %s" % (k, v), flush=True)
    else:
        print("  读不到:", val, flush=True)

    # ── 云盘界面（登录窗 / 上传范围选择）────────────────────────────
    # 这几个弹窗都是 JS 现造的，不能靠 --interact 的固定脚本点到，
    # 所以单独一个开关：--cloud login|qr|cookie|scope
    if cloud:
        print("--- 云盘界面 ---", flush=True)
        if cloud in ("login", "qr", "cookie"):
            # 不 await：openLogin() 返回的 Promise 要等用户登录才 resolve
            evaluate(ws, 60, "CloudUI.openLogin(); 'opened'")
            time.sleep(1.8)
            if cloud in ("qr", "cookie"):
                evaluate(ws, 61,
                         "(()=>{const b=[...document.querySelectorAll('[data-cl-tab]')]"
                         ".find(x=>x.dataset.clTab==='%s');if(b)b.click();return !!b})()"
                         % cloud)
                time.sleep(3.0 if cloud == "qr" else 0.7)
        elif cloud == "scope":
            # 走真流程：plan → 弹「合并 / 分轨 / 全传」。
            # ⚠ 下载页要点**真按钮**，别直接调 CloudUI.upload ——
            #   那样就绕过了 download.js 里「把 wav_dir 递过去」那段，
            #   等于没验到两页的差异。
            if page:
                evaluate(ws, 62,
                         "(()=>{const b=document.querySelector('#dlCloud');"
                         "if(!b)return 'no btn';b.click();return 'clicked'})()")
            else:
                evaluate(ws, 62,
                         "CloudUI.upload({endpoint:'/api/upload_cloud'}); 'go'")
            time.sleep(2.4)
        print("  弹窗数:", evaluate(
            ws, 63, "document.querySelectorAll('.cl-mask').length"), flush=True)
        print("  标题:", evaluate(
            ws, 64,
            "(()=>{const t=document.querySelector('.cl-title');return t?t.textContent:'-'})()"),
            flush=True)
        print("  账号行:", evaluate(
            ws, 65,
            "(()=>{const a=document.querySelector('.cl-acct-name');return a?a.textContent:'-'})()"),
            flush=True)

    if "--interact" in sys.argv:
        print("--- 交互 ---", flush=True)

        def step(label, js, mid, wait=False):
            """wait=True 时把 js 当 Promise 等它 resolve（读异步结果必须用）。"""
            print("  %-14s %s" % (label, evaluate(ws, mid, js, await_promise=wait)),
                  flush=True)

        if page:
            # ── 下载页 ────────────────────────────────────────
            step("机种分组", "document.querySelectorAll('.dl-grp').length + ' 组 / ' "
                             "+ document.querySelectorAll('.dl-plat-item').length + ' 个机种'", 10)
            step("初始空态", "document.querySelectorAll('.dl-empty').length", 11)
            step("点首个机种",
                 # 直接读 item.textContent 会把机种名和扩展名标签粘一起
                 # （"SNES / SFCSPC"），分开读
                 "(()=>{const a=document.querySelectorAll('.dl-plat-item');"
                 "if(!a.length)return 'no plat';a[0].click();"
                 "const nm=(a[0].querySelector('.dl-plat-name')||{}).textContent;"
                 "const fm=(a[0].querySelector('.dl-plat-fmt')||{}).textContent;"
                 "return nm+' / '+fm})()", 12)

            # 扫描是异步的（首次要联网），轮询到不扫为止
            t0 = time.time()
            n_rows = 0
            while time.time() - t0 < 90:
                time.sleep(1.0)
                n_rows = evaluate(ws, 13, "document.querySelectorAll('.dl-row').length")
                busy = evaluate(ws, 14, "document.querySelector('#dlStart').disabled")
                if isinstance(n_rows, int) and n_rows > 0 and not busy:
                    print("  第 %d 秒：列表就绪 %d 行" % (int(time.time() - t0), n_rows),
                          flush=True)
                    break
            step("行数", "document.querySelectorAll('.dl-row').length", 15)
            step("计数条", "document.querySelector('#dlCount').textContent", 16)
            step("首行文本",
                 "(()=>{const r=document.querySelector('.dl-row');"
                 "return r?r.textContent.replace(/\\s+/g,' ').slice(0,70):'none'})()", 17)
            step("中文名生效",
                 # ⚠ 必须限定到 .dl-row —— 表头 .dl-thead 里也有一个 .c-zh
                 #   （"中文名"三个字），不限定就会数成 801 条、样例是"中文名"
                 "(()=>{const z=[...document.querySelectorAll('.dl-row .c-zh')]"
                 ".map(e=>e.textContent).filter(t=>t&&t!=='—');"
                 "return z.length+' 条 / 例: '+(z[0]||'-')})()", 18)
            step("搜索过滤",
                 # ⚠ oninput 有 180ms 防抖，点完立刻读只会读到旧行数。
                 #   必须等两轮（防抖 400ms + poll 回来 400ms）再读。
                 "new Promise(r=>{const b=document.querySelector('#dlSearch');"
                 "b.value='mario';b.dispatchEvent(new Event('input'));"
                 "setTimeout(()=>{const n=document.querySelectorAll('.dl-row').length;"
                 "b.value='';b.dispatchEvent(new Event('input'));"
                 "setTimeout(()=>r(n+' → '+document.querySelectorAll('.dl-row').length),450)},450)})",
                 19, True)
            step("全选",
                 # sel() 是 async：click 后立刻读 #dlCount 拿到的是旧值
                 "new Promise(r=>{document.querySelector('#dlAll').click();"
                 "setTimeout(()=>r(document.querySelector('#dlCount').textContent),700)})",
                 20, True)
            step("反选",
                 "new Promise(r=>{document.querySelector('#dlInvert').click();"
                 "setTimeout(()=>r(document.querySelector('#dlCount').textContent),700)})",
                 21, True)
            step("清空选择",
                 "new Promise(r=>{document.querySelector('#dlNone').click();"
                 "setTimeout(()=>r(document.querySelector('#dlCount').textContent),700)})",
                 22, True)
            # v6.0.5：可选先切机种。用来验 IGDB 兜底那一路 —— 必须挑一个
            # libretro **没有目录**的机种（switch / pc / fmtowns），否则
            # 抓到的图全是 libretro 的，日志里永远看不到 IGDB 那一行。
            _dlm = arg("--dl-machine", "")
            if _dlm:
                step("切机种 " + _dlm,
                     "new Promise(r=>{"
                     "const e=document.querySelector('[data-key=\"%s\"]');"
                     "if(!e)return r('没有这个机种');"
                     "e.click();"
                     # 列表要重新拉一遍 joshw 索引，给足时间再读行数
                     "setTimeout(()=>r(document.querySelectorAll('.dl-row')"
                     ".length+' 行'),6000)})" % _dlm, 319, True)
            # v6.0.4：手动「下载封面」（原「顺带下载封面」复选框）。
            # 勾 2 条 → 点按钮 → 等日志里出现「封面完成」。**真联网**：
            # 图站限速 0.3s/次，2 个游戏约 1~3 秒。
            # ⚠ 行里没有 input[type=checkbox]：勾选是整行 click（toggleRow），
            #   去点 checkbox 会一个都选不上，然后「按钮仍禁用」的假象。
            step("勾 2 条",
                 "new Promise(r=>{"
                 "const rows=[...document.querySelectorAll('.dl-row')].slice(0,2);"
                 "rows.forEach(x=>x.click());"
                 "setTimeout(()=>r(document.querySelector('#dlCount').textContent),900)})",
                 320, True)
            step("下载封面键可点",
                 "(()=>{const b=document.querySelector('#dlCoverFetch');"
                 "return b?(b.disabled?'仍禁用':'可点'):'没有这颗键'})()", 321)
            step("点「下载封面」",
                 "new Promise(r=>{const b=document.querySelector('#dlCoverFetch');"
                 "if(!b)return r('no btn');b.click();"
                 "setTimeout(()=>r('已点击'),600)})", 322, True)
            _t1 = time.time()
            while time.time() - _t1 < 25:
                time.sleep(1.0)
                _lg = evaluate(ws, 323, "document.querySelector('#dlLog').textContent")
                if isinstance(_lg, str) and "封面完成" in _lg:
                    print("  第 %d 秒：封面抓完" % int(time.time() - _t1), flush=True)
                    break
            step("封面日志",
                 "(()=>{const t=document.querySelector('#dlLog').textContent||'';"
                 "const m=t.split('\\n').filter(s=>s.indexOf('\\ud83d\\uddbc')>=0).slice(-3);"
                 "return m.length?m.join(' | ').slice(0,170):'（没有封面日志行）'})()", 324)
            step("状态栏封面计数",
                 "(()=>{const s=document.querySelector('#dlStatus').textContent||'';"
                 "return s.slice(-36)})()", 325)
            # 试听（v5.2-web-play）：点第一首，等 1.6 秒看进度有没有真的往前走。
            # ⚠ **必须发真鼠标事件**：``element.click()`` 是合成事件，Chromium
            #   的自动播放策略不认它是「用户手势」，``audio.play()`` 直接被拒
            #   （表现：paused=true / duration=0 / error=null —— 看着像接口坏了，
            #   其实是策略拦的）。``Input.dispatchMouseEvent`` 才是可信手势。
            _box = evaluate(ws, 92,
                            "(()=>{const a=document.querySelector('.dl-play-row');"
                            "if(!a)return null;const r=a.getBoundingClientRect();"
                            "return {x:Math.round(r.left+r.width/2),"
                            "y:Math.round(r.top+r.height/2)}})()")
            if isinstance(_box, dict) and _box.get("x"):
                for _t in ("mousePressed", "mouseReleased"):
                    call(ws, 93, "Input.dispatchMouseEvent",
                         {"type": _t, "x": _box["x"], "y": _box["y"],
                          "button": "left", "clickCount": 1})
                time.sleep(1.6)
                print("  点第一首试听      真鼠标点击 (%d,%d)" % (_box["x"], _box["y"]),
                      flush=True)
            else:
                print("  点第一首试听      没有可试听的曲目", flush=True)
            step("试听状态",
                 "(()=>{const A=window.__dlAudio||{};"
                 "return (document.querySelector('.dl-play-row.on')?'已选中 ':'没选中 ')+"
                 "'bar='+document.querySelector('#dlPlayBar').style.width+"
                 "' time='+document.querySelector('#dlPlayTime').textContent+"
                 "' paused='+A.paused+' t='+(A.currentTime||0).toFixed(2)+"
                 "' dur='+(A.duration||0).toFixed(2)+"
                 "' err='+(A.error?A.error.code:'-')})()", 94)
            # v6.0.3：进度条可点 —— 在 60% 处发**真鼠标事件**，看 currentTime
            # 有没有跳过去。用合成 click() 也行，但真事件才证明「点得中」
            # （命中区靠 CSS 的 ::after 撑开，合成事件会绕过这层）。
            _sk = evaluate(ws, 95,
                           "(()=>{const e=document.querySelector('#dlPlaySeek');"
                           "if(!e)return null;const r=e.getBoundingClientRect();"
                           "return {x:Math.round(r.left+r.width*0.6),"
                           "y:Math.round(r.top+r.height/2)}})()")
            if isinstance(_sk, dict) and _sk.get("x"):
                for _t in ("mousePressed", "mouseReleased"):
                    call(ws, 96, "Input.dispatchMouseEvent",
                         {"type": _t, "x": _sk["x"], "y": _sk["y"],
                          "button": "left", "clickCount": 1})
                time.sleep(0.6)
                step("点进度条 60%",
                     "(()=>{const A=window.__dlAudio||{};"
                     "const d=A.duration||0;"
                     "return 't='+(A.currentTime||0).toFixed(2)"
                     "+' dur='+d.toFixed(2)"
                     "+' pct='+(d?(100*A.currentTime/d).toFixed(0):'-')+'%'})()", 97)
            else:
                print("  点进度条 60%    没有进度条", flush=True)
            # 上一曲 / 下一曲：点完读曲名，两首的列表里应该换成另一首
            step("点下一曲",
                 "new Promise(r=>{document.querySelector('#dlNext').click();"
                 "setTimeout(()=>r(document.querySelector('#dlPlayName').textContent),900)})",
                 98, True)
            step("点上一曲",
                 "new Promise(r=>{document.querySelector('#dlPrev').click();"
                 "setTimeout(()=>r(document.querySelector('#dlPlayName').textContent),900)})",
                 99, True)
            step("再点一次暂停",
                 "new Promise(r=>{const a=document.querySelector('.dl-play-row');"
                 "if(a)a.click();"
                 "setTimeout(()=>r('btn='+document.querySelector('#dlPlayBtn').textContent),500)})",
                 91, True)
            # 主题按钮是**切换**（不是「切到浅色」），所以标签写成「切一次 / 再切回」，
            # 否则 --theme light 起手时输出会反着写，看着像 bug
            step("主题切换一次",
                 "(()=>{document.querySelector('#dlTheme').click();"
                 "return document.documentElement.dataset.theme})()", 23)
            time.sleep(0.4)
            step("切换后背景", "getComputedStyle(document.body).backgroundColor", 24)
            step("再切回",
                 "(()=>{document.querySelector('#dlTheme').click();"
                 "return document.documentElement.dataset.theme})()", 25)
            time.sleep(0.4)
            step("切回后背景", "getComputedStyle(document.body).backgroundColor", 26)
            step("状态条", "document.querySelector('#dlStatus').textContent", 27)
            step("连接状态", "document.querySelector('#dlConn').textContent", 28)
            step("日志行数", "document.querySelectorAll('#dlLog > div').length", 29)
            # 截图前恢复「选中态」—— 交付截图里空选择看不出勾选效果。
            # 这一步只影响截图观感，不影响上面任何断言。
            step("截图前全选",
                 "new Promise(r=>{document.querySelector('#dlAll').click();"
                 "setTimeout(()=>r(document.querySelector('#dlCount').textContent),700)})",
                 30, True)
            # ⚠ 等一轮 poll 把底部 #dlStatus 也刷新到「已选 N」，
            #   否则截图里上面写「已选 1918」、下面还写「已选 0」，自相矛盾。
            time.sleep(1.2)
        else:
            # ── 主界面 ────────────────────────────────────────
            step("初始标题", "document.querySelector('#stageName').textContent", 10)
            step("点第 2 个音源",
                 "(()=>{const s=document.querySelectorAll('.src')[1];if(!s)return 'no src';"
                 "s.click();return document.querySelector('#stageName').textContent})()", 11)
            # 曲目清单是异步拉的（/api/tracks），点完立刻读只会读到「读取曲目…」
            time.sleep(0.9)
            step("该源曲目",
                 "document.querySelector('#stageName').textContent + ' / ' "
                 "+ document.querySelectorAll('.tr').length + ' 行'", 110)
            step("表头", "document.querySelector('#tableHead').textContent.replace(/\\s+/g,' ')", 111)
            step("搜索过滤",
                 "(()=>{const b=document.querySelector('#searchBox');b.value='a';"
                 "b.dispatchEvent(new Event('input'));const n=document.querySelectorAll('.tr').length;"
                 "b.value='';b.dispatchEvent(new Event('input'));return n+' → '+"
                 "document.querySelectorAll('.tr').length})()", 13)
            step("切浅色",
                 "(()=>{document.querySelector('[data-theme-btn=light]').click();"
                 "return document.documentElement.dataset.theme})()", 14)
            step("切回深色",
                 "(()=>{document.querySelector('[data-theme-btn=dark]').click();"
                 "return document.documentElement.dataset.theme})()", 15)
            step("格式卡数", "document.querySelectorAll('.fmt-card.on').length", 16)
            step("进度条隐藏",
                 "(()=>{const p=document.querySelector('#progStrip');"
                 "return p.hidden+' / '+getComputedStyle(p).display})()", 17)
            step("日志面板隐藏",
                 "(()=>{const p=document.querySelector('#logPanel');"
                 "return p.hidden+' / '+getComputedStyle(p).display})()", 18)
            step("连接状态", "document.querySelector('#conn').textContent", 19)

    # 真跑一次转换，拍「转换中 / 完成」两个状态（**只对主界面有意义**）
    if "--convert" in sys.argv and not page:
        print("--- 真跑转换 ---", flush=True)
        outdir = arg("--outdir", "")
        if outdir:
            # 直接写 S.outDir，跳过原生目录对话框（截图脚本不该弹窗）
            js_out = json.dumps(os.path.abspath(outdir))
            print("  预设输出目录:", evaluate(
                ws, 29, "(()=>{S.outDir=%s;paintOutDir();"
                        "return document.querySelector('#pathText').textContent})()" % js_out),
                flush=True)
        print("  点开始转换:", evaluate(
            ws, 30, "(()=>{const b=document.querySelector('#btnGo');"
                    "if(b.disabled)return 'btn disabled';b.click();return 'clicked'})()"),
            flush=True)

        saw_running = False
        for i in range(150):
            time.sleep(1.0)
            st = evaluate(ws, 31, "S.state + '|' + S.progress.toFixed(0) + '|' "
                                  "+ document.querySelector('#progText').textContent")
            if isinstance(st, str) and st.startswith("running"):
                if not saw_running:
                    saw_running = True
                    print("  第 %d 秒 running: %s" % (i + 1, st), flush=True)
                    shot_now(ws, 40, out.replace(".png", "_running.png"))
            elif isinstance(st, str) and (st.startswith("done") or st.startswith("failed")
                                          or st.startswith("cancelled")):
                print("  第 %d 秒终态: %s" % (i + 1, st), flush=True)
                break

        # 完成态：展开日志 + 切到输出结果
        print("  展开日志:", evaluate(
            ws, 32, "(()=>{const p=document.querySelector('#logPanel');"
                    "p.hidden=false;return document.querySelectorAll('.log-line').length})()"),
            flush=True)
        time.sleep(0.8)
        print("  输出行数:", evaluate(
            ws, 33, "document.querySelectorAll('.tr[data-oid]').length"), flush=True)
        print("  切输出视图:", evaluate(
            ws, 34, "(()=>{if(!S.tab.match('outputs')){S.tab='outputs';"
                    "renderStage();renderTable();}return S.tab})()"), flush=True)
        time.sleep(0.5)
        print("  试听第一首:", evaluate(
            ws, 35, "(()=>{const r=document.querySelector('.tr[data-oid]');"
                    "if(!r)return 'no row';r.click();return S.playingId})()"), flush=True)
        time.sleep(2.5)
        print("  波形柱数 / 已点亮:", evaluate(
            ws, 36, "document.querySelectorAll('#wave i').length + ' / ' "
                    "+ document.querySelectorAll('#wave i.on').length"), flush=True)
        print("  底部播放条:", evaluate(
            ws, 37, "document.querySelector('#nowName').textContent + ' | ' "
                    "+ document.querySelector('#nowMeta').textContent"), flush=True)

    # ── 6 套复古配色 ────────────────────────────────────────────
    # 面板是 `#themeSeg` 里那颗调色板按钮弹出的浮层，条目是 `.theme-item`，
    # key 形如 `retro:famicom`。`panel` 只开不选，用来拍面板本身。
    if retro:
        print("--- 复古主题: %s ---" % retro, flush=True)
        print("  打开配色面板:", evaluate(
            ws, 60, "(()=>{const b=document.querySelector('[data-theme-btn=retro]');"
                    "if(!b)return 'no retro btn';b.click();"
                    "const p=document.querySelector('#themePop');"
                    "return p ? (p.hidden ? 'still hidden' : 'open, '"
                    "+ p.querySelectorAll('.theme-item').length + ' items')"
                    ": 'no pop'})()"), flush=True)
        time.sleep(0.4)
        if retro != "panel":
            key = retro if retro.startswith("retro:") else "retro:" + retro
            print("  选 %s:" % key, evaluate(
                ws, 61, "(()=>{const b=document.querySelector('.theme-item"
                        "[data-theme-key=\"%s\"]');if(!b)return 'no such item';"
                        "b.click();return document.documentElement.dataset.theme"
                        " + ' / ' + getComputedStyle(document.body).backgroundColor})()"
                        % key), flush=True)
            time.sleep(0.5)
            print("  注入的变量数:", evaluate(
                ws, 62, "(()=>{const s=document.documentElement.style;"
                        "let n=0;for(let i=0;i<s.length;i++)if(s[i].startsWith('--'))n++;"
                        "return n})()"), flush=True)
        # 选了具体一套时面板自己会关（app.js 的 click 处理里 close()）；
        # `panel` 模式则故意留着，截图上要能看到那 6 个色块。

    # 落快门前给渲染一点时间。`boot()` 里音源清单和曲目都是异步拉的
    # （`/api/state` + `/api/tracks`），页面 ready 不代表表格已经填好 ——
    # 不等的话会出现「左栏有音源、中间表格空着」的半截图。
    time.sleep(1.0)

    call(ws, 3, "Page.enable")
    shot = call(ws, 4, "Page.captureScreenshot", {"format": "png"})
    data = shot.get("result", {}).get("data")
    if data:
        with open(out, "wb") as f:
            f.write(base64.b64decode(data))
        print("截图: %s (%d B)" % (out, os.path.getsize(out)), flush=True)

    ws.close()
    proc.terminate()
    try:
        proc.wait(5)
    except Exception:                                            # noqa: BLE001
        proc.kill()
    httpd.shutdown()
    return 0 if data else 1


if __name__ == "__main__":
    sys.exit(main())
