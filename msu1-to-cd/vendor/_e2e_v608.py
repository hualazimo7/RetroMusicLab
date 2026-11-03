# -*- coding: utf-8 -*-
"""v6.0.8 端到端：起**真 exe**，走 HTTP 验「更新全部列表」。

门禁只到「函数返回什么」这一层。这个脚本再往下走一步：真进程、真 HTTP、
真后台线程。验五件事：

1. ``/api/dl/state`` 里带 ``refresh`` 字段（前端靠它显示进度）；
2. ``POST /api/dl/refresh_all`` 立刻返回 ok + 总机种数（33）；
3. 随后 ``refresh.running`` 为真、``total`` 对得上 —— 说明**真的在跑**；
4. ``POST /api/dl/refresh_cancel`` 能停下来，``running`` 归位；
5. 用户主动停的**不算失败**（``fail`` 必须还是 0）。

⚠ 只跑几秒就取消。``joshw_source.scan_platform`` 在取消时**不落盘**，
所以不会把任何机种的缓存写成半截。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(BASE, "build", "MSU1Converter-web-pyi",
                   "MSU1Converter-web.exe")


def _get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.status, r.read()


def _post(url, payload=None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def main():
    if not os.path.isfile(EXE):
        print("跳过：exe 不存在")
        return 1
    log = os.path.join(os.path.dirname(EXE), "logs", "startup.log")
    try:
        os.remove(log)
    except OSError:
        pass

    p = subprocess.Popen([EXE], cwd=os.path.dirname(EXE),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port = 0
    for _ in range(40):
        time.sleep(0.5)
        try:
            txt = open(log, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        m = re.search(r"http://127\.0\.0\.1:(\d+)/", txt)
        if m:
            port = int(m.group(1))
            break
    if not port:
        print("!! 起不来（没读到端口）")
        p.terminate()
        return 1
    base = "http://127.0.0.1:%d" % port
    print("服务端口 %d" % port)

    bad = 0
    try:
        code, raw = _get(base + "/api/dl/state")
        st = json.loads(raw.decode())
        rf = st.get("refresh")
        print("/api/dl/state → refresh = %r" % (rf,))
        if not isinstance(rf, dict) or "running" not in rf:
            print("!! state 里没有 refresh 字段")
            bad += 1
        elif rf.get("running"):
            print("!! 刚启动就在跑？")
            bad += 1

        code, d = _post(base + "/api/dl/refresh_all")
        print("refresh_all → %s %r" % (code, d))
        total = int(d.get("total") or 0)
        if code != 200 or not d.get("ok") or total != 33:
            print("!! refresh_all 没按预期返回（期望 ok + total=33）")
            bad += 1

        time.sleep(1.5)
        code, raw = _get(base + "/api/dl/state")
        st = json.loads(raw.decode())
        rf = st.get("refresh") or {}
        print("跑起来之后 → running=%s done=%s/%s key=%s"
              % (rf.get("running"), rf.get("done"), rf.get("total"),
                 rf.get("key")))
        if not rf.get("running"):
            print("!! 没在跑（后台线程没起来？）")
            bad += 1
        if int(rf.get("total") or 0) != 33:
            print("!! total 不对")
            bad += 1

        # 再点一次应该被拒绝（不会开第二份）
        code, d2 = _post(base + "/api/dl/refresh_all")
        print("再点一次 → %r" % (d2,))
        if d2.get("ok") is not False:
            print("!! 重复点击没被拒绝")
            bad += 1

        code, d3 = _post(base + "/api/dl/refresh_cancel")
        print("refresh_cancel → %r" % (d3,))
        if not d3.get("ok"):
            print("!! 取消失败")
            bad += 1

        stopped = False
        for _ in range(60):
            time.sleep(0.5)
            code, raw = _get(base + "/api/dl/state")
            rf = (json.loads(raw.decode()).get("refresh") or {})
            if not rf.get("running"):
                stopped = True
                break
        print("停之后 → running=%s ok=%s fail=%s done=%s/%s"
              % (rf.get("running"), rf.get("ok"), rf.get("fail"),
                 rf.get("done"), rf.get("total")))
        if not stopped:
            print("!! 取消后还在跑")
            bad += 1
        if int(rf.get("fail") or 0) != 0:
            print("!! 用户主动停被算成了失败")
            bad += 1

        # 当前机种/列表没被搅乱（这次没选机种，key 应为空）
        code, raw = _get(base + "/api/dl/state")
        st = json.loads(raw.decode())
        if st.get("key"):
            print("!! 批量更新把当前机种改掉了：%r" % st.get("key"))
            bad += 1
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:                          # noqa: BLE001
            p.kill()

    print("\n结论：%s" % ("全部通过" if not bad else "%d 项失败" % bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
