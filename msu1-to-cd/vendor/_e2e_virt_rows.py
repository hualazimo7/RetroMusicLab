#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_e2e_virt_rows.py — 下载页「能翻到全部条目」的端到端验证（v5.9.1）。

## 为什么要专门一个脚本

v5.9.1 之前列表最多渲染 800 行，底部写「还有 N 条未显示 —— 用搜索缩小
范围」。Switch 有 3512 条，等于八成条目根本翻不到。改成虚拟滚动后，
「看起来对了」和「真的对了」是两件事：

  * 假高度算错   → 滚动条只到一半，最后几百条永远够不着；
  * 窗口下标错位 → 滚到中间显示的是别的行，甚至整段重复；
  * 没有回收     → DOM 里还是几千个节点，白改。

所以这里不看截图，只量三样：**假高度、末行身份、DOM 节点数**。

跑法::

    python vendor/_e2e_virt_rows.py                 # 默认 switch（3512 条）
    python vendor/_e2e_virt_rows.py --key hoot_arcade

⚠ 依赖 Edge（和 web_ui/shot.py 同一套 CDP 手段），CI 上没有会直接跳过。
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

ROW_H = 27                      # 与 download.js 的 ROW_H / CSS 的 .dl-row 一致
CDP_PORT = 9413
PROFILE = os.path.join(os.environ.get("TEMP") or r"C:\Windows\Temp",
                       "rml_virt_profile")

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


def main():
    # 坑：系统代理会把 CDP 的 WS 转成 CONNECT 然后 502（见 shot.py 头注释）
    for k in [k for k in os.environ if "proxy" in k.lower()]:
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"

    import websocket                                             # noqa: E402
    from web_ui import server                                    # noqa: E402
    from web_ui.shot import call, evaluate, find_edge            # noqa: E402

    # ⚠ 不能直接用 shot.cdp_targets()：它读的是 shot 模块里的 CDP_PORT(9412)，
    #   本脚本为了不和开发期的截图窗口抢端口用的是 9413 —— 复用就会一直
    #   查 9412、然后报「25 秒内没拿到页面 target」（踩过）。
    def targets():
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json" % CDP_PORT, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))

    key = arg("--key", "switch")
    edge = find_edge()
    if not edge:
        print("找不到 Edge，跳过（这不是失败）", flush=True)
        return 0

    os.makedirs(PROFILE, exist_ok=True)
    httpd, port, _state = server.serve(port=0)
    base = "http://127.0.0.1:%d" % port

    # 预置机种：走缓存/内置快照，必要时联网扫（慢但只发生一次）
    from web_ui.server import dl_state                          # noqa: E402
    st = dl_state()
    t0 = time.time()
    print("预置机种 %s …" % key, flush=True)
    st.load(key)
    while st.scanning and time.time() - t0 < 300:
        time.sleep(1.0)
    entries = list(st.entries)
    total = len(entries)
    print("  后端条目数 %d（%.1f 秒）" % (total, time.time() - t0), flush=True)
    if total < 1000:
        print("  ⚠ 这个机种只有 %d 条，测不出「大列表」——换 --key switch" % total,
              flush=True)

    url = "%s/download.html?theme=dark" % base
    args = [
        edge, "--app=%s" % url, "--window-size=1280,840", "--window-position=40,30",
        "--user-data-dir=%s" % PROFILE,
        "--remote-debugging-port=%d" % CDP_PORT,
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=msEdgeSidebarV2,msImplicitSignin,msEdgeCollections",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    for a in range(4):
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

    shot_dir = arg("--shot-dir", "")

    def shot(name):
        """存一张证据图（可选）。CDP 截图是唯一可靠的手段，见 shot.py 头注释。"""
        if not shot_dir:
            return
        mid[0] += 1
        r = call(ws, mid[0], "Page.captureScreenshot", {"format": "png"})
        data = r.get("result", {}).get("data")
        if not data:
            return
        p = os.path.join(shot_dir, name)
        os.makedirs(shot_dir, exist_ok=True)
        with open(p, "wb") as f:
            f.write(base64.b64decode(data))
        print("  截图 %s（%d B）" % (p, os.path.getsize(p)), flush=True)

    try:
        # 等列表真的渲染出来（扫描可能还在跑，轮询到有行为止）
        t0 = time.time()
        n = 0
        while time.time() - t0 < 180:
            time.sleep(1.0)
            n = ev("document.querySelectorAll('.dl-row').length")
            if isinstance(n, int) and n > 0:
                break
        check("列表已渲染", isinstance(n, int) and n > 0, "首屏 %s 行" % n)

        errs = ev("JSON.stringify(window.__errs || [])")
        check("页面无 JS 异常", errs in ("[]", "null", None), errs)
        shot("virt-top.png")

        # 表头与行的列位置必须对得上。虚拟滚动把行挪进了 .dl-vs-win
        # （绝对定位），列宽是 grid 自己算的 —— 一旦哪天 padding 或
        # box-sizing 被动过，表头会跟行错开，而界面「看着只是有点歪」，
        # 不会报错。允许 10px 误差：.dl-rows 要扣掉 9px 滚动条。
        align = ev(
            "(()=>{const q=(s)=>document.querySelector(s);"
            "const h=q('.dl-thead .c-name'),r=q('.dl-row .c-name');"
            "if(!h||!r)return null;"
            "const a=h.getBoundingClientRect(),b=r.getBoundingClientRect();"
            "return [Math.round(a.left),Math.round(b.left)]})()")
        check("表头与行左边界对齐（≤10px）",
              isinstance(align, list) and abs(align[0] - align[1]) <= 10,
              "%s vs %s" % (align[0], align[1]) if isinstance(align, list) else align)

        # ── 1. 假高度 = 总条数 × 27 ──────────────────────────
        info = ev(
            "(()=>{const s=document.querySelector('.dl-vs');"
            "const b=document.querySelector('#dlRows');"
            "return [s?parseInt(s.style.height,10):-1, b.scrollHeight,"
            "b.clientHeight, document.querySelectorAll('.dl-row').length]})()")
        check("假高度 = 条数 × 27", isinstance(info, list) and info[0] == total * ROW_H,
              "%s vs %d" % (info[0] if isinstance(info, list) else "?", total * ROW_H))
        check("可滚动高度 ≥ 假高度",
              isinstance(info, list) and info[1] >= total * ROW_H,
              "%s vs %d" % (info[1] if isinstance(info, list) else "?", total * ROW_H))
        check("DOM 节点数不随条数增长",
              isinstance(info, list) and info[3] < 200,
              "%s 个 .dl-row（总 %d 条）" % (info[3] if isinstance(info, list) else "?",
                                            total))

        # ── 2. 滚到底 = 最后一条 ─────────────────────────────
        ev("(()=>{const b=document.querySelector('#dlRows');"
           "b.scrollTop=b.scrollHeight;return b.scrollTop})()")
        time.sleep(0.45)
        last = ev(
            "(()=>{const rs=[...document.querySelectorAll('.dl-row')];"
            "return rs.length?[rs[rs.length-1].dataset.url, rs.length]:['','0']})()")
        check("滚到底能看到最后一条",
              isinstance(last, list) and last[0] == entries[-1]["url"],
              "%s… vs %s…" % (str(last[0])[-46:] if isinstance(last, list) else "?",
                              entries[-1]["url"][-46:]))
        check("滚到底 DOM 仍然只有一屏",
              isinstance(last, list) and 0 < int(last[1]) < 200,
              "%s 行" % (last[1] if isinstance(last, list) else "?"))
        shot("virt-bottom.png")

        # ── 3. 滚到中间 = 中间那一条 ─────────────────────────
        mid_top = ev("(()=>{const b=document.querySelector('#dlRows');"
                     "const t=Math.floor(b.scrollHeight/2/27)*27;"
                     "b.scrollTop=t;return t})()")
        time.sleep(0.45)
        got = ev("(()=>{const rs=[...document.querySelectorAll('.dl-row')];"
                 "return rs.length?[rs[0].dataset.url, rs.length]:['','0']})()")
        # 窗口首行 = 首屏可见行 − OVERSCAN(16)，所以后端对应下标就是它
        idx = max(0, int(mid_top or 0) // ROW_H - 16)
        check("滚到中间对得上号",
              isinstance(got, list) and got[0] == entries[idx]["url"],
              "%s… vs 期望第 %d 条 %s…" %
              (str(got[0])[-46:] if isinstance(got, list) else "?", idx,
               entries[idx]["url"][-46:]))

        # ── 4. 搜索后仍然正确（过滤走下标，最容易串）─────────
        # 词要选得**命中几十到上千条**：只命中 1 条测不出窗口计算，
        # 命中全部又退化成没过滤。候选里挑第一个落在区间内的。
        def hits_of(word):
            return sum(1 for e in entries
                       if word in (e["name"] + " " + str(e.get("zh") or "")).lower())

        q = ""
        for cand in ("mario", "the", "dragon", "a"):
            if 20 <= hits_of(cand) <= 2000:
                q = cand
                break
        if not q:
            q = (entries[len(entries) // 2]["name"][:3] or "a").lower()
        hit = hits_of(q)
        ev("(()=>{const b=document.querySelector('#dlSearch');"
           "b.value=%s;b.dispatchEvent(new Event('input'));return 1})()"
           % json.dumps(q, ensure_ascii=False))
        time.sleep(1.4)
        sres = ev(
            "(()=>{const s=document.querySelector('.dl-vs');"
            "const rs=[...document.querySelectorAll('.dl-row')];"
            "return [s?parseInt(s.style.height,10)/27:-1, rs.length,"
            "rs.every(r=>r.textContent.toLowerCase().indexOf(%s)>=0)]})()"
            % json.dumps(q))
        check("搜索「%s」后假高度 = 命中数" % q,
              isinstance(sres, list) and int(sres[0]) == hit,
              "%s vs %d" % (sres[0] if isinstance(sres, list) else "?", hit))
        check("搜索后渲染的行都真的命中",
              isinstance(sres, list) and sres[2] is True,
              sres[2] if isinstance(sres, list) else "?")
        check("搜索后 DOM 仍然只有一屏",
              isinstance(sres, list) and 0 < int(sres[1]) < 200,
              "%s 行（命中 %d）" % (sres[1] if isinstance(sres, list) else "?", hit))

        # ── 5. 清空搜索能回到全量 ────────────────────────────
        ev("(()=>{document.querySelector('#dlSearchClear').click();return 1})()")
        time.sleep(1.4)
        back = ev("(()=>{const s=document.querySelector('.dl-vs');"
                  "return s?parseInt(s.style.height,10)/27:-1})()")
        check("清空搜索回到全量", back == total, "%s vs %d" % (back, total))
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
