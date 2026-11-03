#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shell.py — 应用外壳：起本地服务，用系统 Edge 的 ``--app`` 模式开窗。

## 为什么是 Edge ``--app``

pywebview 在本机跑不起来（WebView2 控件导航永不启动，最小例子 + Python
3.13/3.14 双版本都复现）。``--app`` 是 Chromium 的「应用窗口」模式：

    无地址栏、无标签页、无工具栏 → 外观就是一个独立窗口

代价（如实记下来）：**任务栏和标题栏的图标是 Edge 的**，这个改不了。
页面给了 favicon 的话，窗口标题栏有可能用上，但不保证。

## 生命周期

    shell.py
      ├─ 起 server（多线程，端口固定 47913 兼作单实例锁）
      ├─ 拉 msedge --app --user-data-dir=<持久化目录>
      ├─ proc.wait()   ← 用户关窗口 = Edge 进程退出
      └─ 退出

Edge 用**独立的 user-data-dir**，所以不会复用用户已开的 Edge 窗口。但
**不能**因此认为「``proc.wait()`` 的返回 = 用户关了窗口」：Chromium 同一个
``--user-data-dir`` 只允许一个浏览器主进程，新起的 msedge 会把命令行**交接**
给已有实例、自己立刻以 0 退出（见 ``launch()`` 里的 HANDOFF_GRACE）。

## 跑法

    python -m web_ui.shell                 # 正常启动
    python -m web_ui.shell --port 0        # 随机端口（开发用）
    python -m web_ui.shell --browser       # 不用 --app，退回默认浏览器
"""

import os
import subprocess
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web_ui import server                                         # noqa: E402

#: 启动日志（窗口程序 stderr 是 None，只能靠这个留痕，见 bootlog.py）
try:
    import bootlog                                                # noqa: E402
except Exception:                                                 # noqa: BLE001
    bootlog = None


def _log(msg):
    """同时写启动日志和 stdout。窗口程序下只有前者看得见。"""
    if bootlog:
        bootlog.log(msg)
    print("[外壳] %s" % msg, flush=True)


#: 固定端口 —— 兼作单实例锁（第二个实例绑不上就说明已经有一个在跑）
FIXED_PORT = 47913

#: ``GET /api/whoami`` 回的 ``app`` 字段值（见 ``web_ui/server.py`` 的 ``APP_ID``）
_WHOAMI_APP = "RetroMusic Lab"


def port_is_ours(port, timeout=1.2):
    """``port`` 上蹲着的是不是**我们自己**（另一个实例）。

    为什么要问这一句：``FIXED_PORT`` 绑不上时，老逻辑一律当成「已经有一个
    在跑」→ 弹框让用户去任务管理器结束 ``MSU1Converter-web.exe``。可端口也
    可能被**别的程序**占着（杀软、别的开发工具、用户自己起过的服务），那台
    机器上根本没有第二个我们的进程 —— 用户照着提示找不到、程序永远起不来。

    判据是 ``/api/whoami`` 回的 JSON。任何异常（连不上、不是 HTTP、返回的不是
    我们的 JSON）都算「不是我们」，因为**宁可多开一个实例，也不能永久拒绝启动**。

    ⚠ 必须绕开系统代理：``urllib`` 默认吃 ``HTTP_PROXY``，公司网 / 开了全局
    代理的机器上，请求会被送到代理服务器，127.0.0.1 直接超时 → 误判成「不是我
    们」→ 多开一个实例，两个实例抢同一个 Edge profile。
    """
    try:
        import urllib.request
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))          # 空 dict = 不走代理
        url = "http://127.0.0.1:%d/api/whoami" % int(port)
        with opener.open(url, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
        import json
        return json.loads(body).get("app") == _WHOAMI_APP
    except Exception:                                 # noqa: BLE001
        return False

#: 能当「应用外壳」的浏览器，按优先级排。
#:
#: 为什么不止 Edge：Win10 有被精简 / 手动卸载 Edge 的机器（也有企业镜像
#: 直接去掉的）。只认 Edge 就是**单点依赖** —— 那台机器上 ``find_edge()``
#: 返回空，退回系统默认浏览器，而默认浏览器同样可能不存在，最后表现就是
#: 「双击没反应，任务管理器里却有个进程」。
#:
#: 这里列的四个都是 Chromium 系，全都支持 ``--app=`` / ``--user-data-dir=``，
#: 外观和 Edge 一模一样（无地址栏、无标签页），所以降级是**无损**的。
_BROWSERS = (
    ("Edge", "msedge.exe", (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
        r"D:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"D:\Program Files\Microsoft\Edge\Application\msedge.exe",
    )),
    ("Chrome", "chrome.exe", (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    )),
    ("Brave", "brave.exe", (
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
    )),
    ("Vivaldi", "vivaldi.exe", (
        r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe",
        r"C:\Program Files\Vivaldi\Application\vivaldi.exe",
    )),
)

#: 当前实际用哪个浏览器的进程名 —— :func:`_app_windows` 靠它筛
#: 「哪些窗口是我们的」。换成 Chrome 之后还按 ``msedge.exe`` 筛，就永远
#: 找不到自己的窗口（残留清理和「等窗口关闭」都会失效）。
_CUR_IMAGE = "msedge.exe"


# ── Win32 签名声明 ──────────────────────────────────────────────────────
#
# ⚠ 和 ``native_dialog.py`` 里同一类坑：不声明签名时，ctypes 一律把参数和
#   返回值按 **32 位 c_int** 处理，而 HWND / HANDLE 在 64 位下都是指针。
#
#   这里和对话框那个不一样的地方在于**它不会崩**：HWND 通常小于 2^24，
#   截断了也照样能用，所以平时完全看不出来。真正要命的是
#   ``OpenProcess`` 返回的句柄一旦被截断，``QueryFullProcessImageNameW``
#   就失败 → :func:`_proc_image_name` 返回空串 → 「这个窗口是不是我们的」
#   判断失效：残留窗口清不掉、「等窗口关闭」永远超时、第二实例守卫失灵。
#
#   这种「不崩但行为悄悄错掉」的故障比访问违例难查得多，所以老实声明。
if os.name == "nt":
    import ctypes
    import ctypes.wintypes as _wt

    _u32 = ctypes.windll.user32
    _k32 = ctypes.windll.kernel32

    # 收 HWND 的
    _u32.IsWindowVisible.argtypes = [_wt.HWND]
    _u32.IsWindowVisible.restype = _wt.BOOL
    _u32.GetWindowTextLengthW.argtypes = [_wt.HWND]
    _u32.GetWindowTextLengthW.restype = ctypes.c_int
    _u32.GetWindowTextW.argtypes = [_wt.HWND, _wt.LPWSTR, ctypes.c_int]
    _u32.GetWindowTextW.restype = ctypes.c_int
    _u32.GetWindowThreadProcessId.argtypes = [_wt.HWND,
                                              ctypes.POINTER(_wt.DWORD)]
    _u32.GetWindowThreadProcessId.restype = _wt.DWORD
    _u32.PostMessageW.argtypes = [_wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM]
    _u32.PostMessageW.restype = _wt.BOOL
    _u32.SetForegroundWindow.argtypes = [_wt.HWND]
    _u32.SetForegroundWindow.restype = _wt.BOOL
    _u32.ShowWindow.argtypes = [_wt.HWND, ctypes.c_int]
    _u32.ShowWindow.restype = _wt.BOOL
    _u32.BringWindowToTop.argtypes = [_wt.HWND]
    _u32.BringWindowToTop.restype = _wt.BOOL
    _u32.SwitchToThisWindow.argtypes = [_wt.HWND, _wt.BOOL]
    _u32.SwitchToThisWindow.restype = None
    # 回调参数用 c_void_p 接 CFUNCTYPE 实例是可以的（实测 648 个窗口正常枚举）
    _u32.EnumWindows.argtypes = [ctypes.c_void_p, _wt.LPARAM]
    _u32.EnumWindows.restype = _wt.BOOL

    # 收/返 HANDLE 的
    _k32.OpenProcess.argtypes = [_wt.DWORD, _wt.BOOL, _wt.DWORD]
    _k32.OpenProcess.restype = _wt.HANDLE
    _k32.QueryFullProcessImageNameW.argtypes = [
        _wt.HANDLE, _wt.DWORD, _wt.LPWSTR, ctypes.POINTER(_wt.DWORD)]
    _k32.QueryFullProcessImageNameW.restype = _wt.BOOL
    _k32.TerminateProcess.argtypes = [_wt.HANDLE, _wt.UINT]
    _k32.TerminateProcess.restype = _wt.BOOL
    _k32.CloseHandle.argtypes = [_wt.HANDLE]
    _k32.CloseHandle.restype = _wt.BOOL


def _expand(p):
    """展开 ``%LOCALAPPDATA%`` 这类环境变量；展开不了返回空串。"""
    try:
        return os.path.expandvars(p) if p else ""
    except Exception:                                # noqa: BLE001
        return ""


def _app_paths(exe_name):
    """从注册表 ``App Paths`` 查浏览器真实路径。

    装在非标准位置（自定义安装目录、别的盘）时，硬编码路径全落空，
    注册表这一条还能兜住。
    """
    try:
        import winreg
        key = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\"
               + exe_name)
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, key) as k:
                    p = winreg.QueryValue(k, None)
            except OSError:
                continue
            if p and os.path.isfile(p):
                return p
    except Exception:                                # noqa: BLE001
        pass
    return ""


def find_browser():
    """找一个能当外壳的 Chromium 浏览器。返回 ``(exe, 名字, 进程名)``。

    顺序：Edge → Chrome → Brave → Vivaldi。每个先查注册表 App Paths，
    再查硬编码路径。都找不到返回 ``("", "", "")``，调用方退回默认浏览器。
    """
    for name, image, cands in _BROWSERS:
        p = _app_paths(image)
        if p:
            return p, name, image
        for c in cands:
            c = _expand(c)
            if c and os.path.isfile(c):
                return c, name, image
    return "", "", ""


def find_edge():
    """只找 Edge（保留旧名字，外部脚本和门禁在用）。

    ⚠ 程序实际用的是 :func:`find_browser` —— 别在新代码里调这个，
      否则又变回「只认 Edge」。
    """
    p, name, _image = find_browser()
    return p if name == "Edge" else ""

#: 让窗口更像应用、少点 Edge 自己的东西
EDGE_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    # ⚠ 这两条是**必须**的，不是可选优化。profile 目录是持久化的，一旦里面
    #   留下过普通标签页会话，Edge 下次启动会先恢复那个窗口（Edge 默认尺寸、
    #   没有应用外观、还会抢在 --app 窗口前面）。用户看到的就是一个
    #   「127.0.0.1 拒绝连接」的普通浏览器窗口，根本不知道真正的应用窗口在后面。
    #   见 clear_restore_data()。
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    "--disable-features=msEdgeSidebarV2,msImplicitSignin,msEdgeIdentity,"
                        "msEdgeCollections,msEdgeWorkspaces",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-extensions",
    "--disable-component-update",
]

#: Edge 用来「下次启动恢复上次窗口」的文件/目录（相对 profile 根）。
#: 每次启动前清掉 —— 不清就会把上次的普通窗口一起恢复出来。
_RESTORE_PATHS = [
    ("Default", "Sessions"),
    ("Default", "EdgeSessions"),
    ("Default", "Last Session"),
    ("Default", "Last Tabs"),
    ("Default", "Current Session"),
    ("Default", "Current Tabs"),
]


def clear_restore_data(prof=None):
    """清掉 Edge 的「恢复上次会话」数据，返回删掉的条目数。

    为什么必须清：profile 目录是**持久化**的（这样冷启动快、也不跟用户
    自己的 Edge 抢）。但持久化也意味着 ``Sessions/Tabs_*`` 会一直留着 ——
    Edge 下次启动就会把那个**普通窗口**一起恢复出来。

    实测（2026-10-22）：profile 里躺着 2 个 ``Apps_*``（--app 窗口会话）和
    2 个 ``Tabs_*``（普通标签页会话）。双击 exe 时 Edge 先弹出那个普通窗口，
    尺寸是 Edge 默认的 1080×620，不是我们的 1280×840 —— 用户看到它连不上
    ``127.0.0.1``，就以为程序坏了。

    删不掉不算错误（Edge 可能正占着文件），跳过继续。
    """
    prof = prof or profile_dir()
    gone = 0
    for parts in _RESTORE_PATHS:
        p = os.path.join(prof, *parts)
        try:
            if os.path.isdir(p):
                for n in os.listdir(p):
                    try:
                        os.remove(os.path.join(p, n))
                        gone += 1
                    except Exception:            # noqa: BLE001 占用/权限都跳过
                        pass
            elif os.path.isfile(p):
                os.remove(p)
                gone += 1
        except Exception:                        # noqa: BLE001 见上
            pass
    return gone


def focus_window(title_part="RetroMusic Lab", timeout=8.0):
    """把已经开出来的应用窗口拉到前台。找到返回 True。

    为什么要自己拉：Edge 是**另一个进程**，前台权限在它手上。而且如果
    profile 里恢复出了别的窗口，我们的 --app 窗口会被压在后面。用户看到
    的就不是应用界面。

    用 ``SwitchToThisWindow`` 兜底 —— 单靠 ``SetForegroundWindow`` 在
    「本进程不是前台」时会被 Windows 拒绝（返回 0）。
    """
    try:
        import ctypes
        import ctypes.wintypes as wt
    except Exception:                            # noqa: BLE001
        return False
    u = ctypes.windll.user32
    SW_SHOWNORMAL = 1
    deadline = time.time() + max(0.5, float(timeout))
    while time.time() < deadline:
        hits = []
        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        def _cb(h, _l):
            if not u.IsWindowVisible(h):
                return True
            n = u.GetWindowTextLengthW(h)
            if not n:
                return True
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, b, n + 1)
            if title_part in b.value:
                hits.append(h)
            return True

        u.EnumWindows(CB(_cb), 0)
        if hits:
            h = hits[0]
            try:
                u.ShowWindow(h, SW_SHOWNORMAL)
                u.BringWindowToTop(h)
                if not u.SetForegroundWindow(h):
                    u.SwitchToThisWindow(h, True)
            except Exception:                    # noqa: BLE001
                pass
            return True
        time.sleep(0.25)
    return False


def message_box(text, title="RetroMusic Lab", flags=0x40):
    """弹一个原生消息框。打包成 GUI 程序后 print 是没人看得见的。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), flags)
        return True
    except Exception:                            # noqa: BLE001
        return False


def find_edge():
    """先查 App Paths 注册表，再退回常见安装路径。"""
    try:
        import winreg
        key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, key) as k:
                    p = winreg.QueryValue(k, None)
                if p and os.path.isfile(p):
                    return p
            except OSError:
                continue
    except Exception:                                # noqa: BLE001
        pass
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


def profile_dir(browser="Edge"):
    """每个浏览器用**各自的** profile 目录。

    ⚠ 不能共用同一个目录：Chrome 打开 Edge 的 profile 会直接报
      「配置文件属于另一个程序」然后退出 —— 表现又是「双击没反应」。

    建不出来（``%LOCALAPPDATA%`` 被组策略重定向到不可写的位置、漫游配置
    损坏、磁盘满）就退到系统 temp —— profile 丢了最多是「主题变回默认」，
    比整个程序起不来好得多。**这里不能抛异常**：调用点都在启动主链路上。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    sub = "%s_profile" % str(browser or "Edge").lower()
    d = os.path.join(base, "MSU1Converter", sub)
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        pass
    import tempfile
    alt = os.path.join(tempfile.gettempdir(), "MSU1Converter", sub)
    try:
        os.makedirs(alt, exist_ok=True)
    except OSError:
        pass
    return alt


# ══════════════════════════════════════════════════════════════════════
#  应用窗口探测 / 残留清理
# ══════════════════════════════════════════════════════════════════════
#: 「Edge 秒退」判定窗口：进程在这么多秒内以 0 退出，按「交接」处理
HANDOFF_GRACE = 4.0
#: 交接之后等窗口出现的最长时间
HANDOFF_FIRST_WAIT = 20.0
#: 窗口消失后还要守多久（秒）才认定用户真的关了
HANDOFF_IDLE = 6.0


def _iter_windows():
    """枚举可见窗口，产出 ``(hwnd, title, pid)``。拿不到就产出空。"""
    try:
        import ctypes
        import ctypes.wintypes as wt
    except Exception:                                # noqa: BLE001
        return
    u = ctypes.windll.user32
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    found = []

    def _cb(h, _l):
        if not u.IsWindowVisible(h):
            return True
        n = u.GetWindowTextLengthW(h)
        if not n:
            return True
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, b, n + 1)
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        found.append((h, b.value, int(pid.value)))
        return True

    try:
        u.EnumWindows(CB(_cb), 0)
    except Exception:                                # noqa: BLE001
        return
    for item in found:
        yield item


def _is_app_window(title, port):
    """这个标题是不是我们的应用窗口。

    页面**加载成功**时标题是 ``RetroMusic Lab · 游戏音乐转换器``；
    加载**失败**（正是要清理的那种）时 Edge 用的是地址 —— ``127.0.0.1``。
    两种都要认，否则残留的错误页窗口会一直躲过检查。

    ⚠ 带端口的匹配要**看端口后面那个字符**：直接 ``in`` 的话
      ``127.0.0.1:479130`` 会被 ``127.0.0.1:47913`` 命中。
    """
    t = title or ""
    if "RetroMusic Lab" in t:
        return True
    if t.strip() == "127.0.0.1":
        return True
    host = "127.0.0.1:%d" % port
    i = t.find(host)
    while i >= 0:
        nxt = t[i + len(host):i + len(host) + 1]
        if nxt == "" or nxt in "/?# ":
            return True
        i = t.find(host, i + 1)
    return False


def _proc_image(pid):
    """进程主模块文件名（小写）。拿不到返回空串。"""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k = ctypes.windll.kernel32
        hp = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not hp:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if not k.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value).lower()
        finally:
            k.CloseHandle(hp)
    except Exception:                                # noqa: BLE001
        return ""


def _app_windows(port):
    """属于**本程序**的应用窗口。

    ⚠ 必须再筛一道进程名：标题叫 ``127.0.0.1`` 的窗口未必是我们的浏览器
      （万一别的程序也这么叫），而这里会**关掉**匹配到的窗口。
      进程名取 :data:`_CUR_IMAGE` —— 降级到 Chrome 之后还写死
      ``msedge.exe``，就永远认不出自己的窗口。
    """
    out = []
    for (h, t, p) in _iter_windows():
        if not _is_app_window(t, port):
            continue
        if _proc_image(p) != _CUR_IMAGE:
            continue
        out.append((h, t, p))
    return out


def close_stale_windows(port, wait=2.5):
    """关掉**上一次遗留**的应用窗口，返回关掉的个数。

    为什么必须关：Edge 用独立的 ``--user-data-dir``，但 exe 被强杀 / 崩溃时
    Edge 进程会活下来。下次启动时新的 ``msedge.exe`` 发现那个 profile 已被
    占用，就把请求**交接**给老实例然后自己以 0 退出 —— 而我们的
    ``proc.wait()`` 会立刻返回，``finally`` 里把服务停掉，老实例这才把窗口
    打开，用户看到的就是 ``ERR_CONNECTION_REFUSED``。

    端口能绑上 = 我们是唯一实例，此刻任何指向本端口的窗口都是残留，关掉安全。
    """
    wins = _app_windows(port)
    if not wins:
        return 0
    try:
        import ctypes
        WM_CLOSE = 0x0010
        u = ctypes.windll.user32
        for (h, _t, _p) in wins:
            try:
                u.PostMessageW(h, WM_CLOSE, 0, 0)
            except Exception:                        # noqa: BLE001
                pass
    except Exception:                                # noqa: BLE001
        return 0
    deadline = time.time() + max(0.5, wait)
    while time.time() < deadline:
        if not _app_windows(port):
            return len(wins)
        time.sleep(0.25)
    # 关不掉就强杀（只杀我们自己 profile 的那个进程）
    try:
        import ctypes
        PROCESS_TERMINATE = 0x0001
        k = ctypes.windll.kernel32
        for (_h, _t, pid) in _app_windows(port):
            if not pid:
                continue
            hp = k.OpenProcess(PROCESS_TERMINATE, False, pid)
            if hp:
                k.TerminateProcess(hp, 0)
                k.CloseHandle(hp)
    except Exception:                                # noqa: BLE001
        pass
    time.sleep(0.5)
    return len(wins)


def wait_until_window_closed(port, first=HANDOFF_FIRST_WAIT, idle=HANDOFF_IDLE):
    """守到应用窗口消失为止（交接模式下用）。

    先等窗口出现（最多 ``first`` 秒）；一直不出现就直接返回 —— 别把服务
    无限期挂在后台。出现之后，连续 ``idle`` 秒看不到窗口才算用户关了。
    """
    deadline = time.time() + max(1.0, first)
    seen = False
    while time.time() < deadline:
        if _app_windows(port):
            seen = True
            break
        time.sleep(0.3)
    if not seen:
        return
    last = time.time()
    while True:
        if _app_windows(port):
            last = time.time()
        elif time.time() - last > idle:
            return
        time.sleep(0.5)


def launch(url, port, argv=()):
    """起服务 + 开窗。返回进程退出码。"""
    global _CUR_IMAGE

    # Edge → Chrome → Brave → Vivaldi，都是 Chromium 系，``--app`` 外观一致。
    exe, bname, image = ("", "", "") if "--browser" in argv else find_browser()
    if exe:
        _CUR_IMAGE = image                          # 窗口判定跟着换
    _log("外壳浏览器 = %s%s" % (bname or "（没找到，用系统默认）",
                                (" · " + exe) if exe else ""))

    if not exe:
        # ⚠ 这里以前是「webbrowser.open 然后死等」——如果那台机器 Edge 被
        #   卸载/被策略禁用、又没有默认浏览器，open() 会**静默失败**：
        #   服务起着、进程活着，但屏幕上一个窗口都没有。用户看到的就是
        #   「双击没反应」，任务管理器里却明明有进程（实测最容易误判的一种）。
        print("[外壳] 没找到 Chromium 系浏览器，改用默认浏览器：%s" % url,
              flush=True)
        _log("退回系统默认浏览器")
        ok = False
        try:
            ok = bool(webbrowser.open(url))
        except Exception as exc:                 # noqa: BLE001
            _log("webbrowser.open 抛异常：%s" % exc)
        if not ok:
            _log("默认浏览器也调不起来，弹窗告诉用户手开")
            message_box(
                "没能打开界面窗口。\n\n"
                "这台机器上找不到 Edge / Chrome / Brave / Vivaldi，\n"
                "也没能调起系统默认浏览器。\n\n"
                "请手动用任意浏览器打开这个地址（服务已经在运行）：\n"
                "%s\n\n"
                "想恢复「独立窗口」的外观，装一个 Edge 或 Chrome 即可，"
                "程序会自动用上它。\n\n"
                "关掉这个对话框后程序继续运行；用完请到任务管理器结束 "
                "MSU1Converter-web.exe。" % url,
                flags=0x30)
        print("[外壳] 按 Ctrl+C 结束服务", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    prof = profile_dir(bname)
    _log("profile = %s" % prof)

    # ⚠ 顺序不能反：**先关残留窗口，再清恢复记录**。
    #   残留的 Edge 正占着 ``Sessions/Apps_*``，先清的话删除会失败
    #   （safe-delete 会报 "Some operations were aborted"），下次启动照样
    #   把旧窗口恢复出来。
    #
    #   为什么要关残留：上一次 exe 被强杀 / 崩溃时 Edge 会活下来。不关的话，
    #   下面新起的 msedge 会「交接」给那个老实例然后立刻退出，我们的服务
    #   跟着被停掉，窗口就显示 ERR_CONNECTION_REFUSED。端口能绑上 = 我们是
    #   唯一实例，此刻指向本端口的窗口必然是残留。
    stale = close_stale_windows(port)
    if stale:
        print("[外壳] 关掉 %d 个上次遗留的窗口" % stale, flush=True)

    gone = clear_restore_data(prof)
    if gone:
        print("[外壳] 清掉 %d 项上次的窗口恢复记录" % gone, flush=True)

    # ⚠ 不开 --remote-debugging-port：生产用不到 CDP，开着等于把浏览器
    #   交给本机任意进程控制。要截图的工具（web_ui/shot.py）自己传端口。
    #
    # ⚠ EDGE_FLAGS 里那几条 ``--disable-features=msEdge*`` 是 Edge 专有的：
    #   Chrome / Brave 不认识，但 Chromium 对未知 feature 名字是**忽略**而不是
    #   报错，所以同一条命令行可以给四个浏览器通用，不用分支。
    args = [exe, "--app=%s" % url,
            "--window-size=1280,840",
            "--user-data-dir=%s" % prof] + EDGE_FLAGS
    print("[外壳] %s" % os.path.basename(exe), flush=True)
    print("[外壳] %s" % url, flush=True)
    _log("拉起 %s --app=%s" % (os.path.basename(exe), url))

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _log("%s pid = %d" % (bname, proc.pid))
    # 浏览器是**另一个进程**，前台权限在它那边；如果 profile 里又冒出一个窗口，
    # 我们的 --app 窗口会被压到后面。起个线程等它出来就拉到前台。
    threading.Thread(target=focus_window, kwargs={"timeout": 12.0},
                     daemon=True).start()
    try:
        # ⚠⚠ ``proc.wait()`` 的返回时机**不一定**是「用户关了窗口」。
        #   Chromium 用同一个 ``--user-data-dir`` 时只允许一个浏览器主进程：
        #   新起的 msedge 会把命令行**交接**给已有实例，自己立刻以 0 退出。
        #   早先直接 ``proc.wait()``，于是 exe 在 1~2 秒内就退出，
        #   ``finally`` 把服务停掉 —— 而老实例这时才刚把窗口打开，
        #   用户看到的就是「127.0.0.1 拒绝连接」，关掉 exe 再开又正常
        #   （第二次没有老实例可交接，走的是正常路径）。
        #   判据：几秒内以 0 退出 = 交接，继续守着窗口，别关服务。
        fast = _wait_exit(proc, HANDOFF_GRACE)
        if fast and proc.returncode == 0:
            print("[外壳] Edge 已交接给已有实例，继续提供服务", flush=True)
            wait_until_window_closed(port)
        else:
            proc.wait()
    except KeyboardInterrupt:
        print("\n[外壳] 收到中断，关闭窗口…", flush=True)
        try:
            proc.terminate()
            proc.wait(5)
        except Exception:                            # noqa: BLE001
            proc.kill()
    return proc.returncode or 0


def _wait_exit(proc, grace):
    """在 ``grace`` 秒内轮询进程是否退出；退出返回 True。"""
    deadline = time.time() + max(0.1, grace)
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.1)
    return False


def _tk_fallback(argv, reason):
    """最后一条路：起**内置 tkinter 界面**。

    为什么这条路能「保证任何一台 Win10/11 都能跑」：tkinter 是 Python
    标准库，已经编译进 exe，**不依赖浏览器 / WebView2 / .NET / VC++ 运行库**。
    外观比 Web 版朴素（v4.44 那套界面），但功能是完整的。

    什么时候走到这儿：
      * 本机一个 Chromium 系浏览器都没有（精简版 Win10、企业镜像）
      * 用户显式给了 ``--tk``

    ⚠ ``msu1_gui.main()`` 读的是 ``sys.argv`` 而不是参数，调用前必须换掉，
      否则 ``--tk`` 会被它当成未知参数。
    """
    _log("切到内置 tkinter 界面：%s" % reason)
    try:
        import msu1_gui
    except Exception as exc:                     # noqa: BLE001
        _log("内置界面起不来：%s" % exc)
        message_box(
            "这台机器上既找不到 Edge / Chrome 等浏览器，\n"
            "内置的备用界面也没能启动。\n\n"
            "原因：%s\n\n日志：\n%s"
            % (exc, bootlog.path() if bootlog else "(无)"), flags=0x10)
        return 3

    old = sys.argv
    sys.argv = [old[0] if old else "MSU1Converter-web.exe"] + [
        a for a in argv if a not in ("--tk",)]
    try:
        msu1_gui.main()
        return 0
    except SystemExit as exc:                    # tk 版内部会 sys.exit(...)
        code = exc.code
        return int(code) if isinstance(code, int) else 0
    finally:
        sys.argv = old


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--port" in argv:
        want = int(argv[argv.index("--port") + 1])
    else:
        want = FIXED_PORT

    # ---- 先决定用哪套界面 ------------------------------------------------
    # ⚠ 必须在起 HTTP 服务**之前**判断：没有浏览器的话，服务起了也没人连，
    #   白占端口；而且后面再切 tk 还得把服务停掉。
    if "--tk" in argv:
        return _tk_fallback(argv, "指定了 --tk")
    if "--browser" not in argv:
        _exe, _bname, _img = find_browser()
        if not _exe:
            return _tk_fallback(
                argv, "本机没有 Edge / Chrome / Brave / Vivaldi")

    _log("准备起服务，端口 %d" % want)
    try:
        httpd, port, state = server.serve(port=want)
        _log("服务已起：http://127.0.0.1:%d/" % port)
    except OSError as exc:
        _log("端口 %d 绑不上：%s" % (want, exc))
        if want != FIXED_PORT:
            httpd, port, state = server.serve(port=0)
        elif not port_is_ours(want):
            # 端口被**别的程序**占着（不是我们的第二个实例）。这时候弹「已经有
            # 一个在运行」是纯粹的误导 —— 用户按提示去任务管理器根本找不到。
            # 直接换一个空闲端口起，用户毫无感知。
            _log("端口 %d 被别的程序占用，改用随机端口" % want)
            httpd, port, state = server.serve(port=0)
        else:
            # 已经有一个在跑 —— 把它的窗口拉到前台，**不要静默退出**。
            # 打包成 GUI 程序后 print 没人看得见，用户双击第二次时如果
            # 什么反应都没有，只会以为程序坏了（实测踩过）。
            if focus_window(timeout=3.0):
                _log("已有一个实例在跑，把它的窗口拉到前台")
                return 0
            message_box(
                "已经有一个 RetroMusic Lab 在运行（端口 %d 被占用），\n"
                "但没有找到它的窗口。\n\n"
                "请打开任务管理器，结束 MSU1Converter-web.exe 后再启动。" % want)
            return 2

    url = "http://127.0.0.1:%d/" % port
    state.log("RetroMusic Lab 已启动")
    try:
        return launch(url, port, argv)
    finally:
        _log("关服务")
        httpd.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
