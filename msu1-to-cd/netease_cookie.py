# -*- coding: utf-8 -*-
"""netease_cookie.py — 让**真浏览器**把网易云登录态交出来（零 tkinter、纯标准库）。

## 为什么不用「读 cookies.sqlite」

Chrome 127 起 Cookie 值变成 ``v20`` 前缀（App-Bound Encryption）：密钥由
``elevation_service.exe`` 用系统级密钥保护，普通进程用 DPAPI 解不开。
实测本机 Chrome 155，``Local State`` 里的 key 能 DPAPI 解开（32 字节），
但 11 条 ``v20`` cookie **一条都解不出**。所以「脚本读浏览器 cookie」这条路
在新版 Chrome / Edge 上已经不通，不要再回头试。

## 这条路的原理

浏览器自己知道明文。通过 **Chrome DevTools Protocol** 的
``Network.getAllCookies`` 让它在本地把 cookie 交出来 —— 同一台机器、同一个
用户，不需要提权，也不碰加密数据库。

## 为什么能绕开 8821

8821 是服务端对「非官方客户端调用登录接口」的风控（详见 README ⚠10）。
这里让用户在**真正的浏览器窗口**里完成 web 端登录，走的是官方网页流程，
不经过我们的 API 扫码；登录完成后我们只是把浏览器**已经持有**的 cookie 取走。

## 一次登录，长期复用

``--user-data-dir`` 指向 ``%LOCALAPPDATA%\\MSU1Converter\\browser``，
登录态落在这个专属 profile 里。下次再取时浏览器一起来就带着 cookie，
用户几乎无感 —— 这就是「自动化」的全部含义。

⚠ 不要改成复用用户自己的 Chrome profile：Chrome 136+ 禁止对默认
user-data-dir 开调试端口，且 profile 被占用时连不上。专属 profile 才是正解。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

__all__ = [
    "find_browser",
    "profile_dir",
    "has_profile",
    "cookie_header_from_cdp",
    "BrowserLogin",
    "clear_browser_cookies",
    "switch_account",
    "LOGIN_URL",
]

LOGIN_URL = "https://music.163.com/#/login"

# 浏览器候选：按「用户装得多、兼容性好」排序。Chrome 第一。
_BROWSERS: List[Tuple[str, List[str]]] = [
    ("Chrome", [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                r"%LocalAppData%\Google\Chrome\Application\chrome.exe"]),
    ("Edge", [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"]),
    ("Brave", [r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
               r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"]),
]

_APP_DIR_NAME = "MSU1Converter"


# --------------------------------------------------------------------------
# 路径与浏览器探测
# --------------------------------------------------------------------------
def profile_dir() -> str:
    """专属浏览器 profile 目录（持久保存登录态）。"""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, _APP_DIR_NAME, "browser")


def has_profile() -> bool:
    """是否已有可用 profile —— 有就说明用户至少登录过一次。

    只判目录里有没有 ``Local State``（Chrome 建 profile 时必写），
    不去看 Cookies 库（那个有锁，而且看了也解不开）。
    """
    d = profile_dir()
    return os.path.isfile(os.path.join(d, "Local State"))


def find_browser() -> Tuple[Optional[str], Optional[str]]:
    """返回 ``(名字, exe 绝对路径)``；都找不到返回 ``(None, None)``。"""
    for name, paths in _BROWSERS:
        for raw in paths:
            p = os.path.expandvars(raw)
            if os.path.isfile(p):
                return name, p
    return None, None


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


# --------------------------------------------------------------------------
# 最小 WebSocket 客户端（只够 CDP 用，避免引入 websocket-client）
# --------------------------------------------------------------------------
class _WS:
    """CDP 只需要：文本帧、单帧、能收大 payload。

    ⚠ 服务端 → 客户端的帧**不掩码**，客户端 → 服务端**必须掩码**。
    写反了握手成功但服务端直接断连，表现是「连上了然后立刻超时」。
    """

    def __init__(self, url: str, timeout: float = 8.0):
        rest = url[5:] if url.startswith("ws://") else url
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手时连接被关闭")
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0]
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手失败：%r" % head[:120])
        self._buf = buf.split(b"\r\n\r\n", 1)[1]

    # ---- 发送 -------------------------------------------------------------
    def send_text(self, text: str) -> None:
        data = text.encode("utf-8")
        n = len(data)
        header = bytearray([0x81])                    # FIN + text
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(
            b ^ mask[i % 4] for i, b in enumerate(data)))

    # ---- 接收 -------------------------------------------------------------
    def _need(self, k: int) -> None:
        while len(self._buf) < k:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket 连接已关闭")
            self._buf += chunk

    def _frame(self) -> Tuple[bool, int, bytes]:
        self._need(2)
        b0, b1 = self._buf[0], self._buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        n = b1 & 0x7F
        off = 2
        if n == 126:
            self._need(4)
            n = struct.unpack(">H", self._buf[2:4])[0]
            off = 4
        elif n == 127:
            self._need(10)
            n = struct.unpack(">Q", self._buf[2:10])[0]
            off = 10
        self._need(off + n)
        payload = self._buf[off:off + n]
        self._buf = self._buf[off + n:]
        return fin, opcode, payload

    def recv_text(self) -> str:
        """收一条完整文本消息（自动拼接 continuation 帧）。"""
        chunks: List[bytes] = []
        while True:
            fin, opcode, payload = self._frame()
            if opcode == 8:                            # close
                raise RuntimeError("WebSocket 对端关闭")
            if opcode == 9:                            # ping，忽略
                continue
            if opcode == 10:                           # pong，忽略
                continue
            if opcode in (1, 2):
                chunks = [payload]
            elif opcode == 0:
                chunks.append(payload)
            else:
                continue
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def call(self, mid: int, method: str,
             params: Optional[dict] = None, timeout: float = 8.0) -> dict:
        """发一条 CDP 命令并等它的回包（跳过事件）。"""
        self.sock.settimeout(timeout)
        self.send_text(json.dumps({"id": mid, "method": method,
                                   "params": params or {}}))
        while True:
            msg = json.loads(self.recv_text())
            if msg.get("id") == mid:
                return msg

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:                                # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# cookie 归并
# --------------------------------------------------------------------------
def cookie_header_from_cdp(cookies: List[dict], now: Optional[float] = None) -> str:
    """CDP 的 cookie 列表 → ``a=1; b=2`` 请求头；**没有 MUSIC_U 就返回空串**。

    只收 ``163.com``（含子域），同名按 path 长者优先。
    空串是「还没登录」的唯一判据 —— 判据必须和「登录成功」同源，
    别在别处再用「cookie 条数 > 0」之类的近似条件。
    """
    now = time.time() if now is None else now
    best: Dict[str, Tuple[int, str]] = {}
    for c in cookies:
        dom = (c.get("domain") or "").lstrip(".").lower()
        if not (dom == "163.com" or dom.endswith(".163.com")):
            continue
        name = c.get("name") or ""
        if not name:
            continue
        exp = c.get("expires", -1)
        if isinstance(exp, (int, float)) and exp > 0 and exp < now:
            continue                                   # 已过期
        path = c.get("path") or "/"
        prev = best.get(name)
        if prev is None or len(path) > prev[0]:
            best[name] = (len(path), c.get("value") or "")
    if "MUSIC_U" not in best:
        return ""
    return "; ".join("%s=%s" % (k, v[1]) for k, v in best.items())


# --------------------------------------------------------------------------
# 浏览器会话
# --------------------------------------------------------------------------
class BrowserLogin:
    """管理一个专属 profile 的浏览器实例，并从中取 cookie。

    典型用法::

        bl = BrowserLogin(silent=True)
        bl.start()
        try:
            header = bl.wait_cookie(timeout=20)
        finally:
            bl.close()
    """

    def __init__(self, silent: bool = False, url: str = LOGIN_URL,
                 profile: Optional[str] = None):
        self.silent = silent          # True = 窗口挪到屏幕外，不打扰用户
        self.url = url
        self.profile = profile or profile_dir()
        self.proc: Optional[subprocess.Popen] = None
        self.port = 0
        self.name = ""
        self.exe = ""
        self._ws: Optional[_WS] = None
        self._mid = 0
        self._tmp_profile: Optional[str] = None   # 仅测试用（自建一次性 profile）

    # ---- 生命周期 ---------------------------------------------------------
    def start(self, timeout: float = 25.0) -> bool:
        """启动浏览器并等到调试端口就绪。失败返回 False，原因写 ``self.error``。"""
        self.error = ""
        self.name, self.exe = find_browser()
        if not self.exe:
            self.error = "没找到 Chrome / Edge"
            return False

        try:
            os.makedirs(self.profile, exist_ok=True)
        except OSError as exc:
            # 目录建不出来就明说，别把 OSError 抛到 HTTP 处理器里变成 500 ——
            # 用户看到的是「云盘页一直转圈」，日志里只有一行 traceback。
            self.error = "建不了浏览器 profile 目录 %s：%s" % (self.profile, exc)
            return False
        # 上一次被强杀会留下 SingletonLock，Chrome 见到就当「已有实例」转给它，
        # 结果调试端口永远不开 —— 表现为等 25s 超时。
        for junk in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = os.path.join(self.profile, junk)
            if os.path.exists(p) or os.path.islink(p):
                try:
                    if self._lock_is_stale(p):
                        os.remove(p)
                except OSError:
                    pass

        self.port = _free_port()
        args = [self.exe,
                "--remote-debugging-port=%d" % self.port,
                "--user-data-dir=%s" % self.profile,
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--disable-features=Translate",
                "--no-service-autorun"]
        if self.silent:
            # ⚠ 别用 --window-position 挪到屏幕外：Chrome 会把窗口钳回可见区域，
            # 结果是「后台取个 cookie，用户看到一个浏览器闪一下」。headless 才干净。
            args += ["--headless=new", "--disable-gpu"]
            args.append(self.url or "https://music.163.com/")
        else:
            args.append(self.url)

        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creation if self.silent else 0)
        except OSError as exc:
            self.error = "启动浏览器失败：%s" % exc
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None and not self._http_ok():
                self.error = "浏览器启动后立刻退出（profile 可能被占用）"
                return False
            if self._http_ok():
                return True
            time.sleep(0.25)
        self.error = "等待浏览器调试端口超时"
        return False

    @staticmethod
    def _lock_is_stale(path: str) -> bool:
        """SingletonLock 是符号链接，指向 ``hostname-pid``；pid 活着就别删。"""
        try:
            target = os.readlink(path)
        except OSError:
            return True                                   # 普通文件 → 当垃圾
        pid = target.rsplit("-", 1)[-1]
        if not pid.isdigit():
            return True
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %s" % pid, "/NH"],
                                 capture_output=True, text=True, timeout=6,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return pid not in (out.stdout or "")
        except Exception:                                 # noqa: BLE001
            return True

    def _http_ok(self) -> bool:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/version" % self.port, timeout=1.5) as r:
                json.loads(r.read().decode("utf-8", "replace"))
            return True
        except Exception:                                 # noqa: BLE001
            return False

    def _page_url(self) -> str:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json/list" % self.port, timeout=3) as r:
            items = json.loads(r.read().decode("utf-8", "replace"))
        pages = [t for t in items if t.get("type") == "page"
                 and t.get("webSocketDebuggerUrl")]
        if not pages:
            raise RuntimeError("浏览器里没有可用的页面")
        # 优先挑网易云那个标签，避免取到 about:blank
        for t in pages:
            if "163.com" in (t.get("url") or ""):
                return t["webSocketDebuggerUrl"]
        return pages[0]["webSocketDebuggerUrl"]

    def _connect(self) -> _WS:
        if self._ws is None:
            self._ws = _WS(self._page_url())
        return self._ws

    # ---- 取 cookie --------------------------------------------------------
    def fetch_cookie_header(self) -> str:
        """取一次；没登录返回空串。连不上会抛 RuntimeError。"""
        ws = self._connect()
        self._mid += 1
        rsp = ws.call(self._mid, "Network.getAllCookies", timeout=10)
        cookies = (rsp.get("result") or {}).get("cookies") or []
        return cookie_header_from_cdp(cookies)

    def pin_cookie_header(self, header: str, days: int = 180) -> int:
        """把 cookie 以**持久 expires** 写回浏览器，返回写入条数。

        ⚠⚠ 这一步不能省。网易云 web 登录给的 ``MUSIC_U`` 很可能是
        **会话期 cookie**（没有 expires，只活在内存里）—— 我们取完就关浏览器，
        下次再开就什么都没了，表现是「明明登录过，每次都要重新登」。
        写回一份带 180 天过期时间的同名 cookie，Chrome 才会把它落盘。
        """
        if not header:
            return 0
        exp = time.time() + days * 86400
        n = 0
        for pair in header.split(";"):
            name, _, value = pair.strip().partition("=")
            name = name.strip()
            if not name or not value:
                continue
            if self._ws is None:
                self._connect()
            self._mid += 1
            try:
                rsp = self._ws.call(self._mid, "Network.setCookie", {
                    "name": name, "value": value.strip(),
                    "domain": ".music.163.com", "path": "/",
                    "expires": exp,
                    "httpOnly": name in ("MUSIC_U", "MUSIC_A"),
                    "secure": True,
                }, timeout=5)
                if (rsp.get("result") or {}).get("success"):
                    n += 1
            except Exception:                            # noqa: BLE001
                self._ws = None
                self._connect()
        return n

    def wait_cookie(self, timeout: float = 180.0, interval: float = 1.5,
                    stop=None) -> str:
        """轮询直到拿到 MUSIC_U 或超时。``stop()`` 返回 True 表示外部要求中止。

        拿到后会**自动固化**（见 :meth:`pin_cookie_header`），调用方不用管。
        """
        deadline = time.time() + timeout
        last_err = ""
        while time.time() < deadline:
            if stop and stop():
                return ""
            if self.proc is not None and self.proc.poll() is not None:
                # 浏览器被用户关掉：这不是错误，用户就是想取消
                raise RuntimeError("浏览器窗口已关闭")
            try:
                header = self.fetch_cookie_header()
                if header:
                    self.pin_cookie_header(header)
                    return header
                last_err = ""
            except Exception as exc:                      # noqa: BLE001
                last_err = str(exc)
                self._ws = None                           # 下一轮重连
            time.sleep(interval)
        if last_err:
            raise RuntimeError("等待登录超时（%s）" % last_err[:60])
        return ""

    # ---- 收尾 -------------------------------------------------------------
    def close(self) -> None:
        """先用 CDP 让浏览器自己退（干净），再兜底 terminate。"""
        if self._ws is not None:
            try:
                self._mid += 1
                self._ws.call(self._mid, "Browser.close", timeout=3)
            except Exception:                            # noqa: BLE001
                pass
            self._ws.close()
            self._ws = None
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    self.proc.wait(4)
            except Exception:                            # noqa: BLE001
                pass
            if self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(3)
                except Exception:                        # noqa: BLE001
                    try:
                        self.proc.kill()
                    except Exception:                    # noqa: BLE001
                        pass
            self.proc = None
        if self._tmp_profile:
            shutil.rmtree(self._tmp_profile, ignore_errors=True)
            self._tmp_profile = None


# --------------------------------------------------------------------------
# 便捷函数：一次性静默取 cookie（供上传入口调用）
# --------------------------------------------------------------------------
def quick_cookie(timeout: float = 3.0, start_timeout: float = 6.0) -> str:
    """起浏览器 → 取一次 cookie → 关掉。没登录返回空串。

    只应在 ``has_profile()`` 为真时调用：profile 都没建过，里面必然没有登录态，
    起了也是白等。

    ⚠ 超时别调大。**会阻塞调用线程**，而它在 Tk 主线程里跑 ——
    profile 有登录态时实测 0.01s 就能拿到（浏览器起来那一刻 cookie 就在），
    所以 3 秒足够；调成 20 秒只会让「点了没反应」变成「卡死 20 秒」。
    """
    bl = BrowserLogin(silent=True)
    try:
        if not bl.start(timeout=start_timeout):
            return ""
        try:
            return bl.wait_cookie(timeout=timeout, interval=0.4)
        except RuntimeError:
            return ""
    finally:
        bl.close()


# --------------------------------------------------------------------------
# 换号：清登录态 + 引导登录另一个账号
# --------------------------------------------------------------------------
def clear_browser_cookies(timeout: float = 20.0,
                          profile: Optional[str] = None) -> Tuple[bool, str]:
    """清空专属 profile 里的**全部** cookie（含网易云的 ``MUSIC_U``）。

    为什么不能直接删 profile 里的 ``Cookies`` 文件：Chrome 运行时会独占它
    （``WinError 32``），而且内存里的 cookie 会回写。走 CDP 的
    ``Network.clearBrowserCookies`` 是唯一稳的路径 —— 它清的是浏览器的
    cookie 存储，不是某个页面的 ``document.cookie``。

    Args:
        profile: 仅测试用。**门禁必须传一个临时 profile**，否则会把用户
            真实的登录态清掉。

    返回 ``(成功?, 说明)``。**不抛异常**：换号失败要让用户看到原因，
    不是崩一个 traceback。
    """
    bl = BrowserLogin(silent=True, url="about:blank", profile=profile)
    if not bl.start(timeout=timeout):
        return False, bl.error or "启动浏览器失败"
    try:
        ws = bl._connect()
        bl._mid += 1
        ws.call(bl._mid, "Network.clearBrowserCookies", {}, timeout=10)
        return True, ""
    except Exception as exc:                          # noqa: BLE001
        return False, str(exc)[:120]
    finally:
        bl.close()


def switch_account(on_ready=None, timeout: float = 240.0,
                   start_timeout: float = 25.0, stop=None,
                   profile: Optional[str] = None) -> Dict[str, str]:
    """清空登录态，然后让用户在**可见**浏览器里登录另一个账号。

    顺序不能颠倒：**先清 cookie 再加载页面**。反过来的话，浏览器带着旧号的
    cookie 一打开就是已登录状态，用户根本没机会换 —— 这是「怎么换都还是原来
    那个号」的根因。

    清完还要 ``Page.navigate`` 重载一次：网易云是 SPA，cookie 没了但页面
    DOM 还是旧的登录态，不重载用户会对着「已登录」的页面发愣。

    Args:
        on_ready: 浏览器已就绪（清完 cookie、已跳登录页）时回调一次，
            调用方用来把提示从「正在准备」切到「请去浏览器登录」。
        timeout: 等待用户登录的秒数。用户要扫码/输密码，给 4 分钟。
        stop: 返回 True 表示外部要求中止（用户点了取消）。
        profile: 仅测试用；``None`` 走默认的专属 profile。

    Returns:
        ``{"ok": "1" 或 "", "header": cookie 串, "error": 失败原因}``。
    """
    out = {"ok": "", "header": "", "error": ""}
    bl = BrowserLogin(silent=False, url=LOGIN_URL, profile=profile)
    if not bl.start(timeout=start_timeout):
        out["error"] = bl.error or "启动浏览器失败"
        return out
    try:
        ws = bl._connect()
        bl._mid += 1
        ws.call(bl._mid, "Network.clearBrowserCookies", {}, timeout=10)
        # ⚠ 必须先跳到 about:blank 再回登录页。LOGIN_URL 带 `#/login` 哈希，
        # 若当前页已经是 music.163.com，`Page.navigate` 到同源同路径只改 hash，
        # **不会重新加载** —— 于是页面还停在「已登录」的样子，用户以为没生效。
        for url in ("about:blank", LOGIN_URL):
            bl._mid += 1
            try:
                ws.call(bl._mid, "Page.navigate", {"url": url}, timeout=8)
            except Exception:                         # noqa: BLE001
                pass                    # 跳转失败也能用：用户自己点一下登录
            time.sleep(0.4)
        if on_ready is not None:
            try:
                on_ready()
            except Exception:                         # noqa: BLE001
                pass
        try:
            header = bl.wait_cookie(timeout=timeout, interval=1.0, stop=stop)
        except RuntimeError:
            # 用户登录成功后顺手把窗口关了 —— wait_cookie 会把它当异常抛。
            # 这时 cookie 可能已经落盘了，补一次静默读取再下结论。
            header = ""
        if not header:
            if stop is not None and stop():
                out["error"] = "已取消"
            else:
                # 兜底：窗口被关掉时 cookie 往往已经写进 profile 了
                header = quick_cookie(timeout=4.0) or ""
                if not header:
                    out["error"] = "等待登录超时（浏览器窗口已关闭或还没登录完）"
        if header:
            out["ok"] = "1"
            out["header"] = header
        return out
    except Exception as exc:                          # noqa: BLE001
        out["error"] = str(exc)[:160]
        return out
    finally:
        bl.close()
