#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bootlog.py — 启动日志 + 崩溃兜底（窗口程序的「黑匣子」）。

## 为什么要有这个模块

打包出来的 exe 是 ``--windows-console-mode=disable`` 的**窗口程序**：没有
控制台，``sys.stderr`` 是 ``None``，任何 ``print`` 和未捕获异常都**看不见**。
于是「双击没反应」成了最难查的一类故障 —— 进程可能启动到第 3 步就崩了，
用户只看到桌面闪一下，我们拿不到一行线索。

这个模块在 **import 阶段**就把日志文件打开，之后每一步都落一行；
再挂上 ``sys.excepthook`` / ``threading.excepthook`` / ``faulthandler``，
崩溃时既写日志又**弹一个原生消息框**，用户能直接截图。

## 日志落点（按顺序取第一个可写的）

1. ``<exe 所在目录>\\logs\\startup.log``          绿色版：日志跟着程序走
2. ``%LOCALAPPDATA%\\RetroMusic Lab\\logs\\startup.log``   装在 Program Files 时
3. ``%TEMP%\\RetroMusic Lab\\startup.log``                 最后兜底

⚠ 每次启动把上一份轮转成 ``startup.prev.log``（只留一份），不无限长。
⚠ 这个模块**只依赖标准库**，且不能 import 本工程任何东西 —— 它要在
  「别的什么都还没加载」的时候就能用。
"""

import os
import sys
import threading
import time
import traceback

__all__ = ["start", "log", "path", "fatal", "attach_console", "where",
           "pause_if_allocated"]

#: 日志文件名
LOG_NAME = "startup.log"

_lock = threading.Lock()
_path = ""                 # 当前日志文件的完整路径
_fh = None                 # 文本句柄（一直开着，别关）
_ftr = None                # faulthandler 用的**二进制**句柄（也得一直开着）
_started = False
_in_fatal = False          # 防止 fatal 里再抛异常导致无限递归
_allocated = False         # 控制台是我们自己新开的（退出前要停一下）


# ---------------------------------------------------------------------------
# 找落点
# ---------------------------------------------------------------------------

def _exe_dir():
    """exe 所在目录。

    ⚠ Nuitka onefile 下 ``sys.executable`` 指向**临时解包目录**里的
    ``python.exe``，写那儿一退出就没了。必须用 ``sys.argv[0]``。
    """
    cands = []
    if sys.argv:
        cands.append(sys.argv[0])
    exe = getattr(sys, "executable", "")
    if exe:
        cands.append(exe)
    for p in cands:
        try:
            d = os.path.dirname(os.path.abspath(p))
        except (OSError, ValueError):
            continue
        if d and os.path.isdir(d):
            return d
    return os.getcwd()


def _candidates():
    out = [os.path.join(_exe_dir(), "logs")]
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    if base:
        out.append(os.path.join(base, "RetroMusic Lab", "logs"))
    import tempfile
    try:
        out.append(os.path.join(tempfile.gettempdir(), "RetroMusic Lab"))
    except Exception:                            # noqa: BLE001
        pass
    return out


def _open_log():
    """挑一个能写的目录，轮转旧日志，返回 ``(路径, 文本句柄)``。"""
    for d in _candidates():
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, LOG_NAME)
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                try:
                    prev = os.path.join(d, "startup.prev.log")
                    if os.path.isfile(prev):
                        os.remove(prev)
                    os.replace(p, prev)          # 只留上一份，不无限长
                except OSError:
                    pass
            fh = open(p, "w", encoding="utf-8", buffering=1)
            return p, fh
        except OSError:
            continue
    return "", None


# ---------------------------------------------------------------------------
# 写日志
# ---------------------------------------------------------------------------

def log(msg):
    """写一行（带时间戳与进程号）。任何情况下都不抛异常。"""
    global _fh
    if _fh is None:
        return
    try:
        ts = time.strftime("%H:%M:%S")
        with _lock:
            _fh.write("[%s][%d] %s\n" % (ts, os.getpid(), msg))
    except Exception:                            # noqa: BLE001
        pass


def path():
    """当前日志文件路径（给界面/弹窗显示用）。"""
    return _path


def where():
    """一行说明：日志在哪、exe 在哪、Python 在哪。"""
    return ("日志: %s\n程序目录: %s\n可执行: %s\n版本: %s %s"
            % (_path or "(写不进去)", _exe_dir(),
               getattr(sys, "executable", ""),
               sys.version.split()[0],
               "打包" if ("__compiled__" in globals()
                          or getattr(sys, "frozen", False)) else "源码"))


# ---------------------------------------------------------------------------
# 崩溃兜底
# ---------------------------------------------------------------------------

def _message_box(title, text):
    """原生消息框。**不 import shell.py** —— 那会绕一圈依赖，崩溃时未必可用。"""
    try:
        import ctypes
        # MB_OK | MB_ICONERROR | MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title),
                                         0x10 | 0x10000)
        return True
    except Exception:                            # noqa: BLE001
        return False


def fatal(exc, title="RetroMusic Lab 启动失败"):
    """记下异常、弹出消息框。给用户一条能复述/截图的线索。"""
    global _in_fatal
    if _in_fatal:
        return
    _in_fatal = True
    try:
        if isinstance(exc, BaseException):
            tb = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))
        else:
            tb = str(exc)
        log("!! %s\n%s" % (title, tb))
        _message_box(
            title,
            "%s\n\n%s\n\n"
            "请把上面这段文字（或日志文件）发给开发者。\n\n"
            "日志：\n%s" % (title, tb.strip(), _path or "(写不进去)"))
    except Exception:                            # noqa: BLE001
        pass
    finally:
        _in_fatal = False


def _install_hooks():
    """装三类钩子：主线程异常 / 子线程异常 / 段错误。"""
    def _hook(exc_type, exc, tb):
        try:
            log("!! 未捕获异常\n" + "".join(
                traceback.format_exception(exc_type, exc, tb)))
        except Exception:                        # noqa: BLE001
            pass
        _message_box("RetroMusic Lab 出错了",
                     "%s: %s\n\n日志：\n%s"
                     % (exc_type.__name__, exc, _path or "(写不进去)"))

    sys.excepthook = _hook

    def _thook(args):
        try:
            log("!! 线程 %s 异常\n" % getattr(args.thread, "name", "?")
                + "".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:                        # noqa: BLE001
            pass

    try:
        threading.excepthook = _thook
    except Exception:                            # noqa: BLE001
        pass

    # faulthandler：段错误 / 栈溢出这类 C 层崩溃，Python 钩子抓不到。
    # ⚠ 必须给它一个**二进制**句柄，且句柄要一直开着（关了就写不进去）。
    global _ftr
    try:
        import faulthandler
        ftr = open(_path, "ab", buffering=0)
        faulthandler.enable(file=ftr, all_threads=True)
        _ftr = ftr                                # 故意留个引用，别被 GC
    except Exception:                            # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 诊断控制台
# ---------------------------------------------------------------------------

def attach_console():
    """给窗口程序临时分配一个控制台，让 print / 回溯看得见。

    两种情形：

    * 从 ``cmd`` 里跑（父进程已有控制台）→ ``AttachConsole`` 挂上去
    * 双击 / 从资源管理器跑（没有控制台）→ ``AllocConsole`` 新开一个

    返回 ``True`` 表示接上了。用户跑 ``MSU1Converter-web.exe --console``
    就能看到全部输出，这是排查「双击没反应」最快的一条路。

    ⚠ 新开的控制台会跟着进程一起消失（程序一退出窗口就关），所以
    :func:`pause_if_allocated` 要在退出前停一下，否则用户什么都看不到。
    """
    global _allocated
    if os.name != "nt":
        return False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        ATTACH_PARENT_PROCESS = 0xFFFFFFFF
        ok = bool(k.AttachConsole(ATTACH_PARENT_PROCESS))
        _allocated = False
        if not ok:
            ok = bool(k.AllocConsole())
            _allocated = ok                      # 新开的：退出前要停一下
        if not ok:
            return False
        k.SetConsoleTitleW("RetroMusic Lab 诊断控制台")
        for name in ("stdout", "stderr"):
            try:
                f = open("CONOUT$", "w", encoding="utf-8", buffering=1)
                setattr(sys, name, f)
            except OSError:
                pass
        try:
            sys.stdin = open("CONIN$", "r", encoding="utf-8")
        except OSError:
            pass
        return True
    except Exception:                            # noqa: BLE001
        return False


def pause_if_allocated():
    """``--console`` 且控制台是**我们新开**的时候，退出前等一次回车。

    否则用户双击一个 ``--console`` 的快捷方式，控制台一闪就没了 ——
    等于白跑一趟。
    """
    if not _allocated:
        return
    try:
        sys.stderr.write("\n按回车键关闭这个窗口…\n")
        sys.stderr.flush()
        sys.stdin.readline()
    except Exception:                            # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def start(argv=None, tag=""):
    """最早调用一次：轮转日志、写首行、装钩子。

    之后再崩，日志里至少能看到「走到哪一步」。
    """
    global _path, _fh, _ftr, _started
    if _started:
        return _path
    _started = True

    # ⚠ 重入（进程里第二次 start）时要先关掉旧句柄：Windows 上文件还被
    #   占着的时候 ``os.replace`` 会失败，轮转就成了空操作，日志越写越大。
    #   faulthandler 那个**二进制**句柄也要关 —— 漏了它同样锁着文件
    #   （门禁里实测过：只关 _fh 还是轮转失败）。
    if _ftr is not None:
        try:
            import faulthandler
            faulthandler.disable()
            _ftr.close()
        except Exception:                        # noqa: BLE001
            pass
        _ftr = None
    if _fh is not None:
        try:
            _fh.close()
        except Exception:                        # noqa: BLE001
            pass
        _fh = None

    _path, _fh = _open_log()
    log("=" * 60)
    log("启动 %s" % (tag or ""))
    log(where())
    if argv is not None:
        log("参数: %s" % (list(argv),))
    log("工作目录: %s" % os.getcwd())
    _install_hooks()
    return _path
