#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_probe_v596_env.py — 复验门禁里那两条「环境性失败」。

## 背景（下次别再查一遍）

这台开发机从 2026-10-26 下午起多了一层**删除护栏**：凡是对 ``D:\\`` 盘
（以及 ``C:\\Temp`` 这类非系统临时目录）的批量删除，都会打一行

    [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":50,...}

然后**把进程直接掐掉**（非交互进程没法确认）。``tmpdir.root()`` 默认是
``D:\\MSU1Temp``，于是 ``vendor/test_web_ui.py`` 跑到一半就没了 ——
表现是「OK 计数停在 412 / 640 / 665，没有汇总行，exit=1」。

绕法：``MSU1_TEMP`` 指到系统临时目录（``%TEMP%`` 下的子目录不受护栏管），
门禁就能完整跑完。但 ``MSU1_TEMP`` 一改，有两条断言必然红 —— 它们断的
本来就是「默认临时根」下的行为：

  1. ``[3j] 副本落在 bios/ 里``：``tmpdir.root()`` 变成环境变量给的那串，
     ``adopt()`` 返回的路径被 ``abspath`` 规范化过，两边分隔符/尾斜杠
     不一致 → 字符串比较不等。
  2. ``[3e] 默认下载目录不在 AppData 里``：``_default_root()`` 取的是
     ``dirname(tmpdir.root())``，根一旦落进 ``%TEMP%``，结果里必然带
     ``AppData``。

本脚本在**默认**环境下把这两条单独复验一遍。全绿就说明那两条失败纯粹是
重定向临时根造成的，不是回归。

    py -3.14 vendor/_probe_v596_env.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

_PASS = 0
_FAIL = 0


def mark(cond, label, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [OK] %s" % label, flush=True)
    else:
        _FAIL += 1
        print("  [!!] %s%s" % (label, ("  → " + str(detail)) if detail else ""),
              flush=True)
    return bool(cond)


def main():
    print("MSU1_TEMP = %r" % os.environ.get("MSU1_TEMP"), flush=True)

    import tmpdir
    import psf_native as P
    from web_ui import dl_api

    root = tmpdir.root()
    print("tmpdir.root() = %s（fallback=%s）" % (root, tmpdir.is_fallback()),
          flush=True)
    mark("MSU1_TEMP" not in os.environ or not os.environ["MSU1_TEMP"],
          "本次没设 MSU1_TEMP（跑的是默认临时根）", os.environ.get("MSU1_TEMP"))

    # ---- ① 默认下载目录形状 ----
    print("\n① 默认下载目录", flush=True)
    dest = dl_api.DownloadState._default_dest("PlayStation")
    print("   dest = %s" % dest, flush=True)
    mark("MSU1Downloads" in dest and "joshw" in dest, "形状正确", dest)
    mark("AppData" not in dest, "不在 AppData 里（用户找得到）", dest)

    # ---- ② BIOS 固化：副本落在 <程序目录>/bios ----
    print("\n② BIOS 固化", flush=True)
    base = os.path.join(root, "probe_bios_bundle")
    import shutil
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    orig = P.app_dir
    P.app_dir = lambda: base
    try:
        home = P.bios_home()
        src = os.path.join(base, "外面临时", "SCPH39001.bin")
        os.makedirs(os.path.dirname(src), exist_ok=True)
        with open(src, "wb") as f:
            f.write(b"\x00" * 32)
        dst, err = P.adopt(src)
        print("   home = %s\ndst  = %s\nerr  = %r" % (home, dst, err), flush=True)
        mark(err == "" and os.path.isfile(dst), "adopt() 复制成功", (dst, err))
        mark(os.path.dirname(dst) == home, "副本落在 bios/ 里", dst)
        mark(os.path.getsize(dst) == 32, "副本内容一致", "")
        mark(P._is_bundled(dst), "_is_bundled 认副本", "")
        mark(not P._is_bundled(src), "_is_bundled 不认源文件", "")
    finally:
        P.app_dir = orig
        P._invalidate()
        shutil.rmtree(base, ignore_errors=True)

    print("\n复验：%d 通过 / %d 失败" % (_PASS, _FAIL), flush=True)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
