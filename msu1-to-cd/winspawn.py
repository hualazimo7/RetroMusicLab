#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""winspawn.py — 起子进程时**不弹窗**的统一入口。

## 为什么需要这个文件

本程序（Web 版 exe）是用 ``--windows-console-mode=disable`` 打的**窗口**程序，
它自己**没有控制台**。而它要调的外部程序里有一堆是**控制台**程序：

| 子进程 | 用途 | 不带静默参数的后果 |
|---|---|---|
| ``psf2wav.exe`` | PSF 解码 | 每转一首闪一次黑框 |
| ``tar.exe``（bsdtar） | 解压 .7z | **每解压一个包闪一次黑框** |
| 自身 exe（``--chip-decode``） | 芯片音源解码 | 打包成窗口程序后同样会闪 |

从没有控制台的进程里起控制台程序，Windows 每次都要临时分配一个 ``conhost``
并显示出来 —— 用户看到的就是「黑框一闪」。偶尔 ``conhost`` 抢不到资源时，
子进程还会在 ``DllMain`` 阶段挂掉，退出码是 ``0xC0000142``
（``STATUS_DLL_INIT_FAILED``）。

## 两道保险缺一不可

* ``creationflags=CREATE_NO_WINDOW`` —— **别创建**控制台；
* ``startupinfo`` + ``SW_HIDE`` —— 万一系统还是给了窗口，藏起来。

只上其中一道时，某些 Windows 版本仍会闪一下。实测对照（``pythonw`` 起
``psf2wav.exe``，轮询可见的 ``ConsoleWindowClass`` 窗口数）：:

    不带参数    peak=1 after=1      ← 黑框出现，而且留下来了
    带参数      peak=0 after=0      ← 全程没有

⚠ ``CREATE_NO_WINDOW`` **不影响** ``stdout`` / ``stderr`` 管道 —— 静默不等于
把输出也吞了，调用方照样能读到子进程的报错（已实测）。
"""

import os
import subprocess

__all__ = ["silent_kw", "rc_u32", "DLL_INIT_FAILED", "CREATE_NO_WINDOW",
           "spawn_hidden"]

#: Windows 的 ``STATUS_DLL_INIT_FAILED``（0xc0000142）
DLL_INIT_FAILED = 0xC0000142
#: ``subprocess.CREATE_NO_WINDOW``。手动写死而不用常量：3.7 以前没有这个名字，
#: 而这里要保证任何 Python 版本下都能用。
CREATE_NO_WINDOW = 0x08000000


def silent_kw():
    """``subprocess.Popen`` / ``run`` 的「绝不弹窗」关键字参数。

    非 Windows 返回空字典 —— 别在别的平台上塞 Windows 专有参数。

    用法::

        subprocess.run(cmd, capture_output=True, **silent_kw())
        subprocess.Popen(cmd, stdout=subprocess.PIPE, **silent_kw())
    """
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": si, "creationflags": CREATE_NO_WINDOW}


def rc_u32(rc):
    """退出码转成**无符号** 32 位。

    Python 的 ``returncode`` 是**有符号**的：``0xC0000142`` 到手是
    ``-1073741502``，直接和 ``0xC0000142`` 比**永远不等**。
    """
    try:
        return int(rc) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


def spawn_hidden(cmd, **kw):
    """``subprocess.Popen`` 的静默封装（参数原样透传）。"""
    kw.update(silent_kw())
    return subprocess.Popen(cmd, **kw)
