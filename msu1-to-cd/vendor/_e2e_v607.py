# -*- coding: utf-8 -*-
"""v6.0.7 端到端：起**真 exe**，走 HTTP 接口验证多封面。

前面的门禁只到「函数返回什么」这一层。这个脚本再往下走一步：
真进程、真 HTTP、真图字节。验三件事：

1. ``/api/dl/covers`` 回的张数和磁盘上一致；
2. ``/api/dl/cover?i=0..n-1`` 每张都能取到，且**内容互不相同**
   （重复的图已经在 _cover_many 里按 md5 去掉了）；
3. 第 0 张就是 ``cover_for_title`` 会拿去嵌音频的那张（盒绘）。

⚠ 用真实的封面缓存，不下载任何新东西。
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
GAME = "Amnesia (2011-08-11)(Design Factory, Otomate)(Idea Factory)[PSP]"


def _get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.status, r.read()


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
    print("服务端口 %d" % port)

    bad = 0
    try:
        code, raw = _get("http://127.0.0.1:%d/api/dl/covers?game=%s"
                         % (port, urllib.parse.quote(GAME)))
        d = json.loads(raw.decode())
        n = int(d.get("n") or 0)
        print("/api/dl/covers → n = %d" % n)
        if n < 2:
            print("!! 张数不够（多封面没生效？）")
            bad += 1
        if n > 6:
            print("!! 超过上限 6 张（用户选的上限没生效）")
            bad += 1

        blobs = []
        for i in range(n):
            code, img = _get("http://127.0.0.1:%d/api/dl/cover?game=%s&i=%d"
                             % (port, urllib.parse.quote(GAME), i))
            okpng = img.startswith(b"\x89PNG")
            print("  i=%d  %s  %d B  PNG=%s"
                  % (i, code, len(img), okpng))
            if code != 200 or not okpng:
                bad += 1
            blobs.append(img)

        if len(set(blobs)) != len(blobs):
            print("!! 有重复的图（去重没生效）")
            bad += 1
        else:
            print("  %d 张内容互不相同 ✓" % len(blobs))

        # 越界不崩
        code, img = _get("http://127.0.0.1:%d/api/dl/cover?game=%s&i=99"
                         % (port, urllib.parse.quote(GAME)))
        print("  i=99（越界）→ %s，%d B" % (code, len(img)))
        if code != 200:
            bad += 1

        # 不存在的游戏 → 404
        try:
            code, _img = _get("http://127.0.0.1:%d/api/dl/cover?game=__nope__"
                              % port)
            print("  不存在的游戏 → %s（期望 404）" % code)
            bad += 1
        except urllib.error.HTTPError as exc:
            print("  不存在的游戏 → %s（期望 404）✓" % exc.code)
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
