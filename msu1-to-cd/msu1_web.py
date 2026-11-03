#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""msu1_web.py — Web 版（v5.1-web）的程序入口，也是 Nuitka 的编译入口。

界面不再是 Qt，而是「本地 HTTP 后端 + 系统 Edge 的 ``--app`` 窗口」：

    msu1_web.py
      └─ web_ui/shell.py
           ├─ web_ui/server.py   起本地服务（ThreadingTCPServer，禁缓存）
           ├─ 拉起 msedge --app  无地址栏/标签页 → 看起来就是个独立窗口
           └─ proc.wait()        窗口关闭即退出

## 为什么单独一个入口文件

Nuitka 需要一个**顶层**脚本做根。让根直接指 ``web_ui/shell.py`` 会把
``sys.path[0]`` 指到 ``web_ui/``，于是 ``import msu1_to_cd`` 找不到；
指到这里就没有任何路径问题。

## 跑法

    python msu1_web.py                 # 正常启动
    python msu1_web.py --port 0        # 随机端口（开发用）
    python msu1_web.py --browser       # 用默认浏览器，不强制 --app
    python msu1_web.py --console       # 诊断：开一个控制台，把过程全打出来

## ⚠ 「双击没反应」怎么查（v5.2.0.6）

打包出来是 ``--windows-console-mode=disable`` 的**窗口程序**，stderr 是
``None`` —— 任何一步失败都表现为「桌面闪一下，什么都没发生」，用户和开发者
都拿不到线索（实测：拷到另一台 Win11 上双击毫无反应，查了两轮才定位）。

所以入口第一件事就是装 :mod:`bootlog`：

* 日志写到 ``<exe 目录>\\logs\\startup.log``（不可写时退 ``%LOCALAPPDATA%``）
* 未捕获异常 / 子线程异常 / 段错误 → 写日志 + **弹原生消息框**
* ``--console`` 时临时接一个控制台，逐步 print 出来

每一步都用 ``_step()`` 打点，日志里能直接看出卡在哪。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ⚠ 第一个 import，而且包一层 try：之后任何一步崩了都能落盘 + 弹窗。
#   连黑匣子都装不上时只能硬撑（这时说明打包本身出了问题）。
try:
    import bootlog
except Exception:                                # noqa: BLE001
    bootlog = None


def _step(msg):
    """给启动过程打点。日志里按顺序看，最后一行就是「走到哪」。"""
    if bootlog:
        bootlog.log("step: " + str(msg))
    print("[启动] %s" % msg, flush=True)


def _ensure_stdout(name="web_worker.log"):
    """窗口版 exe 没有控制台时 ``sys.stdout`` 是 None。

    解 SPC / 芯片音源时本程序会被**自己**再调起来当 worker
    （``--spc-decode`` / ``--chip-decode``），worker 要把错误写 stderr 让父进程
    读得到。stdout 为 None 时任何 print 都会抛 AttributeError，所以先兜到文件。
    """
    if sys.stdout is None or sys.stderr is None:
        try:
            # ⚠ 用 argv[0] 而不是 sys.executable：Nuitka onefile 的
            # sys.executable 指向临时目录里的虚拟 python.exe，日志写那儿就丢了。
            here = (os.path.dirname(os.path.abspath(sys.argv[0]))
                    if getattr(sys, "frozen", False) or "__compiled__" in globals()
                    else os.path.dirname(os.path.abspath(__file__)))
            f = open(os.path.join(here, name), "w", encoding="utf-8")
        except OSError:
            return
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # -- 黑匣子：尽早装 ---------------------------------------------------
    if bootlog:
        if "--console" in argv:
            bootlog.attach_console()             # 先接控制台，后面的 print 才有地方去
        bootlog.start(argv, tag="MSU1Converter-web")

    try:
        return _run(argv)
    except SystemExit:
        raise
    except BaseException as exc:                 # noqa: BLE001
        # 走到这儿说明启动阶段炸了。窗口程序原本会**静默退出**，
        # 用户只看到「双击没反应」。现在弹窗 + 落盘。
        if bootlog:
            bootlog.fatal(exc)
        else:
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None, "启动失败：\n\n%s" % exc, "RetroMusic Lab", 0x10)
            except Exception:                    # noqa: BLE001
                pass
        return 3
    finally:
        if bootlog:
            bootlog.log("退出")
            bootlog.pause_if_allocated()


def _run(argv):
    # ⚠⚠ 必须先做「子进程 worker」分发，再干别的。
    # load_spc_paths / chip_decode.load_chip_paths 会用「本 exe + --spc-decode /
    # --chip-decode」把**自己**再调起来（libgme 解坏内存不能污染主进程）。
    # 不在这里分发的话，子进程会被当成正常启动 → 起服务 + 拉一个 Edge 窗口 →
    # 父进程永远等不到结果文件，界面就卡在「读取音源…」不动。
    # Qt 版在 msu1_qt.main() 开头做同一件事，漏了同样会卡。
    _step("import msu1_to_cd")
    import msu1_to_cd as core
    if "--spc-decode" in argv or "--chip-decode" in argv:
        _step("worker 模式：%s" % ("--spc-decode" if "--spc-decode" in argv
                                  else "--chip-decode"))
        _ensure_stdout()
        core.maybe_run_spc_worker()
        return 0

    _step("tmpdir.init")
    import tmpdir
    root = tmpdir.init()                         # 临时文件统一落 D 盘（不可写则退系统 temp）
    _step("临时目录 = %s" % root)

    _step("import web_ui.shell")
    from web_ui import shell

    _step("shell.main")
    rc = shell.main(argv)
    _step("shell.main 返回 %r" % (rc,))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
