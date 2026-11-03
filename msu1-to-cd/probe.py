# -*- coding: utf-8 -*-
"""最小探针（probe.exe）—— 判定「这台机器能不能跑 Nuitka onefile 程序」。

为什么需要它
------------
主程序是 47 MB 的 onefile exe：先自解压 166 MB 到 ``%TEMP%\\onefile_*``，
再执行里面的真身。用户报「双击没反应、没有日志、没有窗口」时，两种可能
在现象上完全一样：

  A. 自解压阶段就被系统干掉（Smart App Control / 杀软 / TEMP 不可写）
  B. 自解压成功了，但 Python 代码里某处崩了

主程序体积大、依赖多，排查时噪声太大。这个探针只做一件事：**用同样的
Nuitka onefile 机制、同样的 ``--windows-console-mode=disable``，装一个
2 MB 的空壳**。它一启动就弹原生 MessageBox ——

  * 弹出框 → onefile 机制没问题，问题在主程序（回去看日志最后一行）
  * 不弹框 → 这台机器拦 onefile 自解压，任何 Nuitka onefile 都跑不起来，
             换代码没用，只能关 Smart App Control / 加杀软白名单 / 用
             standalone 目录版

探针本身不 import 任何第三方库，只用 ctypes + 标准库，所以打包产物极小、
构建快，也排除了「某个依赖坏了」的干扰。

用法
----
    python probe.py            # 源码态直接跑
    probe.exe                  # 打包后双击
    probe.exe --quiet          # 不弹框，只写日志 + 返回码
    probe.exe --no-log         # 只弹框，不写日志

返回码
------
    0  一切正常
    2  MessageBox 都弹不出来（理论上见不到，能跑就说明能弹）
    4  三处日志全写不进去
"""

import os
import sys
import time

APP = "MSU1Converter-probe"
VER = "1.0"


# --------------------------------------------------------------------------
# 日志：三处候选，和 bootlog.py 完全一致的顺序
#   <exe 目录>\\logs\\probe.log
#   %LOCALAPPDATA%\\RetroMusic Lab\\logs\\probe.log
#   %TEMP%\\RetroMusic Lab\\probe.log
# 三处全写不进去 = 这台机器的文件权限/磁盘有问题
# --------------------------------------------------------------------------
def _candidates():
    out = []
    try:
        out.append(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])),
                                "logs"))
    except Exception:
        pass
    la = os.environ.get("LOCALAPPDATA")
    if la:
        out.append(os.path.join(la, "RetroMusic Lab", "logs"))
    tp = os.environ.get("TEMP") or os.environ.get("TMP")
    if tp:
        out.append(os.path.join(tp, "RetroMusic Lab"))
    return out


def _open_log():
    """返回 (路径, 文件对象)；全失败返回 (None, None)。"""
    for d in _candidates():
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "probe.log")
            fh = open(p, "w", encoding="utf-8", errors="replace")
            return p, fh
        except Exception:
            continue
    return None, None


class _Log(object):
    def __init__(self, enabled=True):
        self.path = None
        self.fh = None
        if enabled:
            self.path, self.fh = _open_log()
        self.t0 = time.time()

    def __call__(self, msg):
        line = "[%7.3fs] %s" % (time.time() - self.t0, msg)
        if self.fh is not None:
            try:
                self.fh.write(line + "\n")
                self.fh.flush()
            except Exception:
                pass
        try:
            print(line, flush=True)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 环境信息
# --------------------------------------------------------------------------
def _windows_version():
    try:
        import platform
        return platform.platform()
    except Exception:
        return "?"


def _temp_info():
    tp = os.environ.get("TEMP") or os.environ.get("TMP") or "(未设置)"
    ok = "不可写"
    try:
        t = os.path.join(tp, "_probe_write_test.tmp")
        with open(t, "wb") as f:
            f.write(b"ok")
        os.remove(t)
        ok = "可写"
    except Exception as exc:
        ok = "不可写（%s）" % type(exc).__name__
    return "%s  →  %s" % (tp, ok)


def _onefile_dirs():
    """列出 %TEMP% 下的 onefile_* 目录 —— 自解压确实发生过的证据。"""
    tp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    if not tp or not os.path.isdir(tp):
        return "(TEMP 无效)"
    try:
        names = [n for n in os.listdir(tp) if n.startswith("onefile_")]
    except Exception as exc:
        return "(列不出来: %s)" % type(exc).__name__
    if not names:
        return "(没有 onefile_* 目录)"
    return "%d 个: %s" % (len(names), ", ".join(sorted(names)[:3]))


def _is_frozen():
    # ⚠ Nuitka 往**每个被编译模块的 globals()** 里注入 ``__compiled__``，
    #   它不是可 import 的模块 —— 早期写成 ``import __compiled__`` 会永远
    #   失败，把打包版误判成「源码直跑」（实测踩过）。
    if "__compiled__" in globals():
        return "是（Nuitka 编译%s）" % (
            " · onefile" if getattr(sys, "frozen", False) else "")
    if getattr(sys, "frozen", False):
        return "是（frozen）"
    return "否（源码直跑）"


def _cpu_name():
    """CPU 型号 —— 排「指令集不兼容」时最关键的一条信息。

    拿到型号就能查它到底支持到哪一代指令集（SSE2 / AVX / AVX2 / AVX-512）。
    ``0xC000001D``（STATUS_ILLEGAL_INSTRUCTION）基本都是 CPU 缺指令，
    而缺哪条要看型号，所以这一行必须打出来。

    ⚠ 走注册表而不是 WMI：WMI 要起 COM + WmiPrvSE，几秒起步，还可能被
      安全策略挡住；注册表读字符串是纯 winreg，几毫秒且永不失败。
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        try:
            val, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            return str(val).strip()
        finally:
            winreg.CloseKey(key)
    except Exception:
        pass
    try:
        import platform
        return platform.processor() or "(取不到)"
    except Exception:
        return "(取不到)"


def _emulated():
    """是否跑在「x64 模拟层」上 —— ARM64 Windows 的 Prism 会这样。

    ARM64 机器上跑 x64 进程时，``PROCESSOR_ARCHITECTURE`` 也是 ``AMD64``，
    只看它会被骗。真正的判据是 ``PROCESSOR_ARCHITEW6432``：在 ARM64 上
    cmd 是 ARM64 原生进程、我们的 exe 是模拟的 x64，两者不一致。
    """
    import platform
    real = os.environ.get("PROCESSOR_ARCHITEW6432") or ""
    native = os.environ.get("PROCESSOR_ARCHITECTURE") or ""
    m = platform.machine()
    if real and native and real != native:
        return "是（%s 上模拟 %s）" % (real, native)
    if m.lower() in ("arm64", "aarch64"):
        return "是（ARM64 原生）"
    return "否"


def _proc_age():
    """进程从被创建到此刻的总耗时（秒）—— **包含 onefile 自解压**。

    这是判断「双击后半天没反应」的关键数字：主程序要解 166 MB，
    慢盘 / 杀软逐个扫文件时可能要几十秒，期间没有任何界面和日志。
    在真机上跑探针，看这个值就知道是不是「慢」而不是「死」。
    """
    try:
        import ctypes
        import time
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        # ⚠ 必须声明 restype —— ctypes 默认按 c_int 处理返回值，64 位下
        #   句柄会被截断成负数，GetProcessTimes 直接失败（实测踩过）。
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ]
        k32.GetProcessTimes.restype = wintypes.BOOL
        h = k32.GetCurrentProcess()
        c = wintypes.FILETIME()
        e = wintypes.FILETIME()
        k = wintypes.FILETIME()
        u = wintypes.FILETIME()
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                   ctypes.byref(k), ctypes.byref(u)):
            return None
        created = (c.dwHighDateTime << 32) | c.dwLowDateTime
        if created <= 0:
            return None
        # FILETIME 是 1601-01-01 起的 100ns 计数
        unix = (created - 116444736000000000) / 10000000.0
        age = time.time() - unix
        return age if 0 <= age < 3600 else None
    except Exception:
        return None


def _report(log):
    lines = []
    lines.append("探针版本 : %s %s" % (APP, VER))
    lines.append("运行形态 : %s" % _is_frozen())
    lines.append("程序路径 : %s" % os.path.abspath(sys.argv[0]))
    lines.append("Python   : %s" % sys.version.split()[0])
    lines.append("系统     : %s" % _windows_version())
    lines.append("CPU      : %s" % _cpu_name())
    lines.append("模拟层   : %s" % _emulated())
    age = _proc_age()
    lines.append("启动耗时 : %s" % (
        "%.2f 秒（含自解压）" % age if age is not None else "测不出来"))
    lines.append("TEMP     : %s" % _temp_info())
    lines.append("解包目录 : %s" % _onefile_dirs())
    lines.append("日志位置 : %s" % (log.path or "三处全写不进去！"))
    return lines


# --------------------------------------------------------------------------
# 原生 MessageBox（不依赖 tkinter / 任何 GUI 库）
# --------------------------------------------------------------------------
def _msgbox(text, title="探针结果", flags=0x40):
    """0x40 = MB_ICONINFORMATION | MB_OK"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
        return True
    except Exception:
        return False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    quiet = "--quiet" in argv
    want_log = "--no-log" not in argv
    log = _Log(enabled=want_log)

    log("=" * 60)
    log("探针启动")
    log("命令行: %r" % (argv,))

    lines = _report(log)
    for ln in lines:
        log(ln)

    if not want_log:
        # 用户主动要求不写日志，不能算失败
        log("结论: 按 --no-log 要求跳过日志")
        verdict = "已跳过日志（--no-log）"
        rc = 0
    elif log.path:
        log("结论: 自解压成功、Python 代码已执行 —— onefile 机制在本机可用")
        verdict = "onefile 机制在本机可用"
        rc = 0
    else:
        log("结论: 三处日志全写不进去 —— 磁盘/权限问题")
        verdict = "三处日志全写不进去（磁盘/权限）"
        rc = 4

    log("探针正常结束")

    if not quiet:
        body = "\n".join(lines) + "\n\n结论：" + verdict + \
               "\n\n看到这个框，就说明：onefile 自解压 + Python 启动 都是好的。"
        _msgbox(body, "探针 OK · %s %s" % (APP, VER))

    if log.fh is not None:
        try:
            log.fh.close()
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
