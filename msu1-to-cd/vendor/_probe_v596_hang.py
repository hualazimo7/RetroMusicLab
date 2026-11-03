#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_probe_v596_hang.py — 查「转换停在 100% 写 WAV N/N」到底卡在哪。

现象：WonderSwan 33 首 WAV 全部写完、磁盘上 33 个文件都在，
      但 ``/api/state`` 的 ``state`` 一直是 ``running``，``status``
      停在 ``写 WAV 33/33`` —— 也就是说 ConvertJob 在最后一次
      on_progress 之后就没再返回。

做法：把 faulthandler 挂上（每 15 秒把所有线程栈打到 stderr），
      跑一次真转换，看卡住时工作线程停在哪个函数。

    py -3.14 vendor/_probe_v596_hang.py
"""

import faulthandler
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

SRC = (r"D:\MSU1Downloads\joshw\WonderSwan"
       r"\Card Captor Sakura - Sakura to Fushigi na Clow Card "
       r"(1999-12-02)(SIMS)(Bandai)[WS]")


def main():
    for k in [k for k in os.environ if "proxy" in k.lower()]:
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"

    from web_ui import server

    httpd, port, _state = server.serve(port=0)

    def api(path, body=None):
        url = "http://127.0.0.1:%d%s" % (port, path)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")

    out = os.path.join(os.environ.get("TEMP") or r"C:\Windows\Temp",
                       "rml_probe_hang_out")
    api("/api/add_sources", {"paths": [SRC]})
    print("开始", flush=True)
    api("/api/start", {"opts": {"out_dir": out, "do_wav": True, "do_cd": False}})

    faulthandler.dump_traceback_later(40, repeat=True, exit=False)
    t0 = time.time()
    last = ""
    while time.time() - t0 < 75:
        time.sleep(1.0)
        st = api("/api/state")
        cur = "%s %.0f%% %s" % (st.get("state"), st.get("progress") or 0,
                                st.get("status") or "")
        if cur != last:
            print("%6.1fs  %s" % (time.time() - t0, cur), flush=True)
            last = cur
        if st.get("state") in ("done", "failed", "cancelled"):
            print("结束：%s 输出 %d 个"
                  % (st.get("state"), len(st.get("outputs") or [])), flush=True)
            break
        # 每隔 10 秒把进程内的真相打一遍：线程还活着吗？job 还在吗？
        if int(time.time() - t0) % 10 == 0:
            th = getattr(_state, "thread", None)
            print("      内部：state=%r thread=%r alive=%r job=%r"
                  % (_state.state, getattr(th, "name", None),
                     th.is_alive() if th else None,
                     type(getattr(_state, "job", None)).__name__), flush=True)
    else:
        print("!! 75 秒还没结束，state 仍为 %r" % last, flush=True)
        print("!! 服务器日志尾巴：", flush=True)
        for row in (_state.logs or [])[-14:]:
            print("   %s" % (row,), flush=True)
        print("!! outputs=%d  done_tracks=%r  total_tracks=%r"
              % (len(_state.outputs or []), _state.done_tracks,
                 _state.total_tracks), flush=True)

    faulthandler.cancel_dump_traceback_later()
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
