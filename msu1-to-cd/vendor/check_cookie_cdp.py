#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_cookie_cdp.py — 浏览器登录（CDP 取 cookie）的本地门禁。

## 为什么必须单独有一份

这条链路**整个存在的理由**就是「别再用读 cookies.sqlite 那条死路」。
三个结论都是实测出来的，任何一个被"优化"回去都会让功能悄悄失效：

1. Chrome 127+ 的 cookie 是 ``v20``（App-Bound Encryption），DPAPI 解不开。
   实测本机 Chrome 155：key 能解出 32 字节，11 条 v20 cookie **一条都解不出**。
2. CDP ``Network.getAllCookies`` **能**拿到明文（浏览器自己解密）。
3. 网易云给的 ``MUSIC_U`` 可能是**会话期** cookie（无 expires）——
   不 ``pin`` 回浏览器，取完一关就没了，用户会以为「每次都要重新登录」。

## 分成两类

* **纯逻辑**（不启浏览器）：cookie 归并、过滤、去重、过期判定。这些是判据本身，
  写错了整条链路都会"看起来成功其实没用"。
* **集成**（真启无头 Chrome）：起停、取 cookie、pin 后跨会话仍在。
  找不到 Chrome/Edge 时 **SKIP**，不假 PASS。

## 用法

    python vendor/check_cookie_cdp.py

⚠ 集成项每次约 4~8 秒（要启两次浏览器）。这是必要成本：
   不能只测纯函数，因为「pin 有没有落盘」只有真重启浏览器才能证明。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import traceback

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

RESULTS = []


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label, detail))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", label,
                       ("  [" + detail + "]") if detail else ""))
    return bool(ok)


def skip(label, reason):
    RESULTS.append((None, label, reason))
    print("SKIP  %s  [%s]" % (label, reason))


# --------------------------------------------------------------------------
# 一、纯逻辑：cookie 归并
# --------------------------------------------------------------------------
def part_logic(nc):
    print("\n-- 1. cookie 归并（纯逻辑）--")
    f = nc.cookie_header_from_cdp

    check(f([]) == "", "1.1 空列表 → 空串")

    check(f([{"name": "NMTID", "value": "x", "domain": ".163.com",
              "path": "/"}]) == "",
          "1.2 没有 MUSIC_U → 空串（判据唯一）")

    h = f([{"name": "MUSIC_U", "value": "abc", "domain": ".music.163.com",
            "path": "/"}])
    check(h == "MUSIC_U=abc", "1.3 有 MUSIC_U → 原样输出", h)

    h = f([{"name": "MUSIC_U", "value": "abc", "domain": ".music.163.com", "path": "/"},
           {"name": "__csrf", "value": "zzz", "domain": ".music.163.com", "path": "/"}])
    check("MUSIC_U=abc" in h and "__csrf=zzz" in h, "1.4 多 cookie 用 '; ' 连接", h)

    # 非 163 域必须被挡掉，否则会把别的网站 cookie 一起发出去
    h = f([{"name": "MUSIC_U", "value": "abc", "domain": ".music.163.com", "path": "/"},
           {"name": "SECRET", "value": "leak", "domain": ".example.com", "path": "/"},
           {"name": "EVIL", "value": "leak", "domain": ".163.com.evil.net", "path": "/"}])
    check("SECRET" not in h and "EVIL" not in h,
          "1.5 只收 163.com 域（挡掉伪装域名）", h)

    # 大小写域名也要认
    h = f([{"name": "MUSIC_U", "value": "abc", "domain": ".MUSIC.163.COM", "path": "/"}])
    check(h == "MUSIC_U=abc", "1.6 域名大小写不敏感", h)

    # 过期判定：now 可控，别依赖真实时间
    now = 1_700_000_000
    h = f([{"name": "MUSIC_U", "value": "old", "domain": ".music.163.com",
            "path": "/", "expires": now - 10}], now=now)
    check(h == "", "1.7 已过期的 MUSIC_U 被丢弃", h)

    h = f([{"name": "MUSIC_U", "value": "good", "domain": ".music.163.com",
            "path": "/", "expires": now + 10}], now=now)
    check(h == "MUSIC_U=good", "1.8 未过期的保留", h)

    h = f([{"name": "MUSIC_U", "value": "sess", "domain": ".music.163.com",
            "path": "/", "expires": -1}], now=now)
    check(h == "MUSIC_U=sess", "1.9 expires=-1（会话期）不当作过期", h)

    # 同名多 path：长的优先（更具体，浏览器也是这个规则）
    h = f([{"name": "MUSIC_U", "value": "root", "domain": ".music.163.com", "path": "/"},
           {"name": "MUSIC_U", "value": "deep", "domain": ".music.163.com",
            "path": "/api/"}])
    check(h == "MUSIC_U=deep", "1.10 同名按 path 长者优先", h)

    h = f([{"name": "", "value": "x", "domain": ".163.com", "path": "/"},
           {"name": "MUSIC_U", "value": "ok", "domain": ".163.com", "path": "/"}])
    check(h == "MUSIC_U=ok", "1.11 空 name 被跳过", h)

    h = f([{"name": "MUSIC_U", "value": "", "domain": ".163.com", "path": "/"}])
    check(h == "MUSIC_U=", "1.12 空值仍保留键（交给服务端判定）", h)


# --------------------------------------------------------------------------
# 二、路径与浏览器探测
# --------------------------------------------------------------------------
def part_probe(nc):
    print("\n-- 2. 路径与探测 --")
    d = nc.profile_dir()
    check(os.path.basename(d) == "browser" and "MSU1Converter" in d,
          "2.1 profile 目录在 MSU1Converter 下", d)

    name, exe = nc.find_browser()
    if exe:
        check(os.path.isfile(exe), "2.2 找到浏览器且 exe 存在", "%s: %s" % (name, exe))
    else:
        skip("2.2 找到浏览器", "本机没装 Chrome/Edge")
    return name, exe


# --------------------------------------------------------------------------
# 三、集成：真启无头浏览器
# --------------------------------------------------------------------------
def part_integration(nc, has_browser):
    print("\n-- 3. 集成（真启无头浏览器）--")
    if not has_browser:
        for label in ("3.1 启动并连上调试端口", "3.2 未登录时取到空串",
                      "3.3 种 cookie 后能取到", "3.4 pin 后跨会话仍在",
                      "3.5 close 能干净退出"):
            skip(label, "无浏览器")
        return

    tmp = tempfile.mkdtemp(prefix="ckcdp_")
    bl = nc.BrowserLogin(silent=True, profile=tmp)
    ok_start = False
    try:
        t0 = time.time()
        ok_start = bl.start()
        dt = time.time() - t0
        check(ok_start, "3.1 启动并连上调试端口",
              "%s %.1fs port=%d %s" % (bl.name, dt, bl.port, bl.error or ""))
        if not ok_start:
            return

        check(bl.fetch_cookie_header() == "", "3.2 未登录时取到空串")

        # 故意不给 expires —— 模拟网易云下发会话期 cookie
        ws = bl._connect()
        bl._mid += 1
        rsp = ws.call(bl._mid, "Network.setCookie",
                      {"name": "MUSIC_U", "value": "gate_value_42",
                       "domain": ".music.163.com", "path": "/"})
        check(bool((rsp.get("result") or {}).get("success")),
              "3.2b 能向浏览器种 cookie（测试前置）")

        h = bl.wait_cookie(timeout=10, interval=0.4)
        check("MUSIC_U=gate_value_42" in h, "3.3 种 cookie 后能取到", h[:60])
    finally:
        proc = bl.proc
        bl.close()
        ended = proc is not None and proc.poll() is not None
        check(ended, "3.5 close 能干净退出")

    # 3.4 必须换一个新实例 —— 同一个 BrowserLogin 复用的是内存里的连接，
    # 证明不了「cookie 落盘了」。
    bl2 = nc.BrowserLogin(silent=True, profile=tmp)
    try:
        if bl2.start():
            h2 = bl2.wait_cookie(timeout=8, interval=0.4)
            check("MUSIC_U=gate_value_42" in h2,
                  "3.4 pin 后跨会话仍在（= 只需登录一次）", h2[:60])
        else:
            check(False, "3.4 pin 后跨会话仍在（= 只需登录一次）",
                  "第二次启动失败：%s" % bl2.error)
    finally:
        bl2.close()
        time.sleep(0.3)
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 四、换号：清 cookie（v4.26）
# --------------------------------------------------------------------------
def part_switch(nc, has_browser):
    """验证「清登录态」真的清掉了，而不是返回 True 就算完。

    ⚠ 全程用**临时 profile**。默认 profile 里是用户真实的登录态，
    在这个门禁里清掉它等于把用户的账号退了 —— 绝不能碰。
    """
    print("\n-- 4. 换号（清 cookie，临时 profile）--")
    if not has_browser:
        for label in ("4.1 种下旧号 cookie", "4.2 clear_browser_cookies 返回成功",
                      "4.3 清完旧号取不到", "4.4 清完能种新号"):
            skip(label, "无浏览器")
        return

    tmp = tempfile.mkdtemp(prefix="cksw_")
    try:
        # ① 前置：种一个带 expires 的「旧号」cookie（模拟已登录）
        bl = nc.BrowserLogin(silent=True, url="about:blank", profile=tmp)
        planted = False
        try:
            if bl.start():
                ws = bl._connect()
                bl._mid += 1
                rsp = ws.call(bl._mid, "Network.setCookie",
                              {"name": "MUSIC_U", "value": "old_account_42",
                               "domain": ".music.163.com", "path": "/",
                               "expires": time.time() + 86400, "secure": True})
                planted = bool((rsp.get("result") or {}).get("success"))
        finally:
            bl.close()
        check(planted, "4.1 种下旧号 cookie（测试前置）")

        # ② 清
        ok, msg = nc.clear_browser_cookies(profile=tmp)
        check(ok, "4.2 clear_browser_cookies 返回成功", msg)

        # ③ 验证真的没了 —— 只看返回值会被「假成功」骗过
        bl2 = nc.BrowserLogin(silent=True, url="about:blank", profile=tmp)
        try:
            if bl2.start():
                h2 = ""
                try:
                    h2 = bl2.fetch_cookie_header()
                except Exception as exc:              # noqa: BLE001
                    h2 = "（读失败：%s）" % str(exc)[:40]
                check("MUSIC_U" not in h2, "4.3 清完旧号取不到", h2[:60] or "空")
            else:
                check(False, "4.3 清完旧号取不到", "第二次启动失败：%s" % bl2.error)
        finally:
            bl2.close()

        # ④ 清完还能种新号（别把 profile 弄坏了）
        bl3 = nc.BrowserLogin(silent=True, url="about:blank", profile=tmp)
        fresh = False
        try:
            if bl3.start():
                ws = bl3._connect()
                bl3._mid += 1
                rsp = ws.call(bl3._mid, "Network.setCookie",
                              {"name": "MUSIC_U", "value": "new_account_7",
                               "domain": ".music.163.com", "path": "/",
                               "expires": time.time() + 86400, "secure": True})
                fresh = bool((rsp.get("result") or {}).get("success"))
                h3 = bl3.fetch_cookie_header()
                fresh = fresh and "MUSIC_U=new_account_7" in h3
        finally:
            bl3.close()
        check(fresh, "4.4 清完能种新号")
    finally:
        time.sleep(0.3)
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    try:
        import netease_cookie as nc
    except Exception:
        print("FAIL  无法导入 netease_cookie")
        traceback.print_exc()
        return 2

    part_logic(nc)
    name, exe = part_probe(nc)
    part_integration(nc, bool(exe))
    part_switch(nc, bool(exe))

    failed = [r for r in RESULTS if r[0] is False]
    skipped = [r for r in RESULTS if r[0] is None]
    passed = [r for r in RESULTS if r[0] is True]
    print("-" * 66)
    print("结果: %d 通过 / %d 失败 / %d 跳过" % (len(passed), len(failed), len(skipped)))
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
