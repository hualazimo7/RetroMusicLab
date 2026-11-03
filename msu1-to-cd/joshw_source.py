#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""joshw_source.py -- 按机种浏览 joshw.info 音源归档索引（核心层）。

本模块只负责「发现」：给一个机种 key，返回该机种下全部 .7z 音源包的名字、
绝对下载地址、大小与修改时间。真正的下载在 :mod:`joshw_dl`。

数据源事实（2026-10-18 实测）：

* 全机种库不在 apex ``https://joshw.info/``（那里只有 PS5 的 12 个 .7z），
  而是按机种子域名分布，例如 ``spc.joshw.info``、``psf.joshw.info``。
* 每个子站根目录是 Apache autoindex 页面，通常包含 ``0-9/ a/ b/ ... z/``
  字母目录，但也有例外：

  - ``kss.joshw.info`` 根下是 ``Game Gear/``、``MSX/``、``Master System/``
    这类「容器目录」，容器里才是字母目录（需要多下探一层）；
  - ``ncd.joshw.info`` 根下没有目录，直接 97 个 .7z；
  - ``gsf`` / ``hes`` / ``2sf`` / ``hoot`` 根下既有目录也有散落的 .7z。

  因此字母目录集合**一律从 HTML 实时解析**，绝不硬编码。

* 各站 ``robots.txt`` 为 ``Disallow: /``，所以本模块只在用户主动选择机种
  时才发起扫描（绝不启动即扫描、绝不遍历全机种），请求间隔 >= 0.2 秒，
  并把索引缓存 7 天。

公开 API 见 ``__all__``。
"""

from __future__ import annotations

import gzip
import html
import json
import os
import posixpath
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "PLATFORMS",
    "USER_AGENT",
    "platform",
    "base_url",
    "human_size",
    "parse_index",
    "cache_dir",
    "cache_file",
    "load_cache",
    "save_cache",
    "clear_cache",
    "bundled_dir",
    "load_bundled",
    "best_index",
    "scan_platform",
    "tls_context",
]

# Apache 索引页里的 UA；joshw.info 会拒绝明显脚本化的 UA。
USER_AGENT = "MSU1Converter/3.4 (personal archive tool)"

_REQUEST_TIMEOUT = 30.0
_HTTP_RETRIES = 3
_RETRY_BACKOFF = (0.6, 1.2, 2.4)
_MIN_REQUEST_INTERVAL = 0.2  # 秒，全局请求间隔下限（尊重 robots.txt Disallow: /）
_SCAN_WORKERS = 2
_MAX_DIR_DEPTH = 2  # 根 -> 容器目录(kss/hoot) -> 字母目录
_CACHE_TTL_DAYS = 7.0
_CACHE_SUBDIR = os.path.join("MSU1Converter", "joshw_cache")
_BUNDLED_SUBDIR = "index"  # 打包进 exe 的索引快照目录（index/<key>.json.gz）

# ---------------------------------------------------------------------------
# 机种表：顺序即界面顺序，group 必填
# ---------------------------------------------------------------------------

_GROUP_RETRO = "复古主机"
_GROUP_MODERN = "现代主机"
_GROUP_COMPUTER = "计算机与街机"


def _pf(key: str, name: str, fmt: str, host: str, group: str,
        path: str = "") -> Dict[str, str]:
    """构造一条机种描述字典（内部辅助）。

    ``path`` 是主机内的子目录（v5.9 新增）。绝大多数站是「一站一机种」，
    根目录就是机种根（``path=""``）；但 ``kss`` 与 ``hoot`` 是**容器站** ——
    根下直接摆着机种目录（``kss/Game Gear/``、``hoot/X68k/``），
    一个机种只对应其中一个子目录，所以必须能把 base 指到子目录上。

    拆开的机种 key 一律加站名前缀（``kss_gg`` / ``hoot_x68k``），
    避免与独立站撞名（``hoot`` 下也有 ``smd`` / ``pc`` / ``msx`` / ``fmtowns``）。
    """
    return {"key": key, "name": name, "fmt": fmt, "host": host,
            "group": group, "path": path}


PLATFORMS: List[Dict[str, str]] = [
    # ---- 复古主机 ----
    _pf("spc", "SNES / SFC", "SPC", "spc.joshw.info", _GROUP_RETRO),
    _pf("nsf", "NES / FC", "NSF", "nsf.joshw.info", _GROUP_RETRO),
    _pf("gbs", "Game Boy", "GBS", "gbs.joshw.info", _GROUP_RETRO),
    _pf("gsf", "Game Boy Advance", "GSF", "gsf.joshw.info", _GROUP_RETRO),
    _pf("smd", "Mega Drive / Sega CD", "VGM", "smd.joshw.info", _GROUP_RETRO),
    _pf("hes", "PC Engine / TurboGrafx", "HES", "hes.joshw.info", _GROUP_RETRO),
    _pf("usf", "Nintendo 64", "USF", "usf.joshw.info", _GROUP_RETRO),
    _pf("ssf", "Saturn", "SSF", "ssf.joshw.info", _GROUP_RETRO),
    _pf("dsf", "Dreamcast", "DSF", "dsf.joshw.info", _GROUP_RETRO),
    _pf("2sf", "Nintendo DS", "2SF", "2sf.joshw.info", _GROUP_RETRO),
    _pf("wsr", "WonderSwan", "WSR", "wsr.joshw.info", _GROUP_RETRO),
    # kss.joshw.info 是容器站：根下直接是 Game Gear / MSX / Master System
    # 三个机种目录（v5.9 拆开，原来挤在一条「MSX」里，用户根本看不到 GG/SMS）。
    _pf("kss_gg", "Game Gear", "KSS", "kss.joshw.info", _GROUP_RETRO,
        path="Game Gear"),
    _pf("kss_msx", "MSX / MSX2", "KSS", "kss.joshw.info", _GROUP_RETRO,
        path="MSX"),
    _pf("kss_sms", "Master System", "KSS", "kss.joshw.info", _GROUP_RETRO,
        path="Master System"),
    _pf("3do", "3DO", "AIF", "3do.joshw.info", _GROUP_RETRO),
    # ---- 现代主机 ----
    _pf("psf", "PlayStation", "PSF", "psf.joshw.info", _GROUP_MODERN),
    _pf("psf2", "PlayStation 2", "PSF2", "psf2.joshw.info", _GROUP_MODERN),
    _pf("psf3", "PlayStation 3", "PSF3", "psf3.joshw.info", _GROUP_MODERN),
    _pf("psf4", "PlayStation 4", "PSF4", "psf4.joshw.info", _GROUP_MODERN),
    # PS5 已于 v5.9 移除：它没有独立子站，原来指向 apex ``joshw.info``，
    # 而 apex 现在 https 连不上、http 只回一个 102 B 的 🤔 占位页 ——
    # 留着就是一个点下去必然失败的机种。
    _pf("psp", "PSP", "PSP", "psp.joshw.info", _GROUP_MODERN),
    _pf("vita", "PS Vita", "PSV", "vita.joshw.info", _GROUP_MODERN),
    _pf("gcn", "GameCube", "GCM", "gcn.joshw.info", _GROUP_MODERN),
    _pf("wii", "Wii", "BRSTM", "wii.joshw.info", _GROUP_MODERN),
    _pf("wiiu", "Wii U", "BFSTM", "wiiu.joshw.info", _GROUP_MODERN),
    _pf("xbox", "Xbox", "XWB", "xbox.joshw.info", _GROUP_MODERN),
    _pf("x360", "Xbox 360", "XMA", "x360.joshw.info", _GROUP_MODERN),
    _pf("3sf", "Nintendo 3DS", "3SF", "3sf.joshw.info", _GROUP_MODERN),
    # switch 是 v5.9 补上的：完整站、27 个字母目录、文件名带 [Switch]。
    # ⚠ 只做「下载 + 解压」，**不做 WAV 转换**（包内是 Switch 的音频容器，
    # 我们没有对应解码器，chip_support("Switch") 返回 "no"）。
    _pf("switch", "Nintendo Switch", "Switch", "switch.joshw.info",
        _GROUP_MODERN),
    # ---- 计算机与街机 ----
    _pf("s98", "PC-98", "S98", "s98.joshw.info", _GROUP_COMPUTER),
    # hoot.joshw.info 是容器站，根下 19 个目录 —— 但**只有 2 个是音源归档**：
    #   ``!MDScene_Arcade_VGM/``  街机 VGM，字母目录下是 ``.7z``
    #   ``qsf/``                  Capcom QSound，18 个 ``.7z``
    # 其余 17 个（``X68k`` ``fm7`` ``pc98`` ``spectrum`` ``roms`` …）装的是
    # **Hoot 播放器用的 .zip ROM 包**（``albatrss_fm7.zip`` / ``180_ZX.zip``
    # 这种），不是音源 —— 我们也没有 Hoot 模拟器，下载了转不了。
    # 所以 v5.9 只把这两个真音源拆成独立机种；原来它们混在一条「街机 Hoot」
    # 里，条目和 ROM 混排，用户根本分不清哪条能转。
    _pf("hoot_arcade", "街机 VGM（Hoot）", "VGM", "hoot.joshw.info",
        _GROUP_COMPUTER, path="!MDScene_Arcade_VGM"),
    _pf("hoot_qsf", "QSound 街机（Hoot）", "QSF", "hoot.joshw.info",
        _GROUP_COMPUTER, path="qsf"),
    _pf("fmtowns", "FM Towns", "FM Towns", "fmtowns.joshw.info", _GROUP_COMPUTER),
    _pf("pc", "PC", "PC", "pc.joshw.info", _GROUP_COMPUTER),
]

_BY_KEY: Dict[str, Dict[str, str]] = {item["key"]: item for item in PLATFORMS}


def chip_support(fmt: str) -> str:
    """机种的主格式 -> 本工具的支持状态，供下载窗口显示徽标。

    返回值：
        ``"chip"``  多格式芯片音源解码管线支持（P0 六种 + SPC）
        ``"spc"``   SPC 走既有稳定路径
        ``"no"``    本期不支持（真实硬件音频日志等，需外部模拟器）

    说明：P0 的六种里 NSF/NSFE/GBS/VGM/VGZ/HES 共用同一条解码管线，
    这里按 :data:`chip_formats.P0_ENABLED` 判定，新增格式只需改那一处常量。
    """
    import chip_formats as _cf
    f = (fmt or "").upper()
    if f == "SPC":
        return "spc"
    if f in _cf.P0_ENABLED or f == "VGZ":
        return "chip"
    return "no"

# apex 主机的证书对 ``joshw.info`` 主机名不匹配（实测本机 198.18.x.x 解析），
# 需要放宽主机名校验，但证书链仍然校验。
_RELAXED_HOSTNAME_HOSTS = frozenset({"joshw.info"})


def platform(key: str) -> Dict[str, str]:
    """按 key 返回机种描述，不存在抛 :class:`KeyError`。"""
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError("未知机种: %r" % (key,))


def base_url(pf: Dict[str, str]) -> str:
    """返回机种根页面 URL（以 ``/`` 结尾）。

    ``path`` 为空时是主机根（``https://spc.joshw.info/``）；
    非空时指向容器站里的机种子目录（``https://kss.joshw.info/Game%20Gear/``）。
    路径按段做百分号编码 —— 目录名里有空格（``Game Gear``）和叹号
    （``!MDScene_Arcade_VGM``），不编码会被 Apache 直接 400。
    """
    host = pf["host"]
    path = str(pf.get("path") or "").strip("/")
    if not path:
        return "https://%s/" % host
    quoted = "/".join(urllib.parse.quote(seg, safe="") for seg in path.split("/"))
    return "https://%s/%s/" % (host, quoted)


# ---------------------------------------------------------------------------
# 尺寸格式化
# ---------------------------------------------------------------------------

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(n: int) -> str:
    """把字节数格式化为 1024 进制、1 位小数的可读字符串。

    >>> human_size(0)
    '0 B'
    >>> human_size(1536)
    '1.5 KB'
    """
    try:
        value = float(int(n))
    except (TypeError, ValueError):
        return "0 B"
    if value <= 0:
        return "0 B"
    index = 0
    while value >= 1024.0 and index < len(_UNITS) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return "%d B" % int(value)
    return "%.1f %s" % (value, _UNITS[index])


_SIZE_UNITS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}

# 一行形如：
# <img src="/icons/unknown.gif" alt="[   ]"> <a href="xxx.7z">xxx.7z</a>
#    2013-07-07 03:46   46K  <span class='description'>7-Zip Archive</span>
_ROW_RE = re.compile(
    r'<a\s+href="(?P<href>[^"]*)"[^>]*>(?P<label>[^<]*)</a>\s*'
    r"(?P<mtime>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s+"
    r"(?P<size>-|\d+(?:\.\d+)?[kKmMgGtT]?)"
)

# 目录行：<img src="/icons/folder.gif" alt="[DIR]"> <a href="s/">s/</a> ...
_DIR_RE = re.compile(r'<a\s+href="(?P<href>[^"?#]+/)"[^>]*>(?P<label>[^<]*)</a>', re.S)


def _parse_size_token(token: str) -> int:
    """把 Apache 的大小列（``-`` / ``46K`` / ``1.4G`` / ``123``）转成字节数。"""
    token = token.strip()
    if not token or token == "-":
        return 0
    suffix = token[-1].lower()
    if suffix in _SIZE_UNITS:
        try:
            return int(round(float(token[:-1]) * _SIZE_UNITS[suffix]))
        except ValueError:
            return 0
    try:
        return int(token)
    except ValueError:
        return 0


def _letter_of(page_url: str) -> str:
    """从页面 URL 推断归属字母（根页面返回空串）。

    普通子站：``/s/`` -> ``"s"``。像 kss 那样多一层容器目录的站，
    返回 ``"game gear/a"``，保证不同容器下的同名字母目录不会被混淆。
    """
    path = urllib.parse.urlsplit(page_url).path or "/"
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return ""
    return "/".join(urllib.parse.unquote(seg) for seg in segments).lower()


def _clean_label(label: str) -> str:
    """把 ``<a>`` 的显示文本整理成纯名字（去 HTML 实体、去首尾空白）。"""
    text = re.sub(r"<[^>]+>", "", label or "")
    return html.unescape(text).strip()


def parse_index(html_text: str, page_url: str) -> List[Dict[str, object]]:
    """解析一个 Apache autoindex 页面里的 .7z 归档条目。

    Args:
        html_text: 页面源码（已解码的 str）。
        page_url: 该页面的绝对 URL，用于拼接绝对下载地址并推断字母目录。

    Returns:
        条目列表，每项 ``{"name","url","size","mtime","letter"}``：

        * ``name`` -- 去掉 ``.7z`` 后缀的显示名；
        * ``url``  -- 绝对 URL，**保留原始百分号编码**；
        * ``size`` -- 字节数，解析失败为 0；
        * ``mtime``-- ``YYYY-MM-DD HH:MM``；
        * ``letter`` -- 所在字母目录；根页面下的散落文件为空串。

        目录行、``Parent Directory``、``?C=N;O=D`` 排序链接一律跳过。
    """
    if not html_text:
        return []
    letter = _letter_of(page_url)
    entries: List[Dict[str, object]] = []
    for match in _ROW_RE.finditer(html_text):
        href = match.group("href") or ""
        if not href or href.startswith("?") or href.startswith("#"):
            continue  # ?C=N;O=D 之类的排序链接
        if href.endswith("/"):
            continue  # 目录行
        if not href.lower().endswith(".7z"):
            continue  # robots.txt / .dll / .zip 等非归档文件
        name = _clean_label(match.group("label"))
        if not name:
            name = urllib.parse.unquote(posixpath.basename(href))
        if name.lower().endswith(".7z"):
            name = name[:-3].rstrip()
        if name.lower() in ("parent directory", ".."):
            continue
        url = urllib.parse.urljoin(page_url, href)
        entries.append(
            {
                "name": name,
                "url": url,
                "size": _parse_size_token(match.group("size")),
                "mtime": re.sub(r"\s+", " ", match.group("mtime")).strip(),
                "letter": letter,
            }
        )
    return entries


def _parse_dirs(html_text: str, page_url: str) -> List[Tuple[str, str]]:
    """解析页面里的子目录（返回 ``(显示名, 绝对URL)`` 列表，已去 Parent Directory）。"""
    if not html_text:
        return []
    found: List[Tuple[str, str]] = []
    seen: set = set()
    for match in _DIR_RE.finditer(html_text):
        href = match.group("href") or ""
        if not href or href.startswith("?"):
            continue
        url = urllib.parse.urljoin(page_url, href)
        if url in seen:
            continue
        seen.add(url)
        name = _clean_label(match.group("label")) or urllib.parse.unquote(href.rstrip("/"))
        name = name.rstrip("/").strip()
        if name.lower() in ("parent directory", ".."):
            continue
        found.append((name, url))
    return found


# ---------------------------------------------------------------------------
# TLS / HTTP
# ---------------------------------------------------------------------------

_CONTEXT_CACHE: Dict[str, ssl.SSLContext] = {}
_CONTEXT_LOCK = threading.Lock()


def tls_context(host: str) -> ssl.SSLContext:
    """返回访问 ``host`` 使用的 SSL 上下文（按主机缓存）。

    apex ``joshw.info`` 的证书对主机名不匹配，但证书链本身有效，
    因此对它只放宽主机名校验（``check_hostname=False``），仍保持
    ``CERT_REQUIRED``。其余主机使用 Python 默认（严格）校验。
    """
    host = (host or "").lower()
    with _CONTEXT_LOCK:
        cached = _CONTEXT_CACHE.get(host)
        if cached is not None:
            return cached
        if host in _RELAXED_HOSTNAME_HOSTS:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_REQUIRED
        else:
            ctx = ssl.create_default_context()
        _CONTEXT_CACHE[host] = ctx
        return ctx


def _relaxed_context() -> ssl.SSLContext:
    """证书链有效但主机名不匹配时使用的兜底上下文。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _decode_body(raw: bytes, content_encoding: str) -> str:
    """按需解压 gzip 并解码为 str（非法字节用替换符，绝不抛错）。"""
    if "gzip" in (content_encoding or "").lower() and raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return raw.decode("utf-8", "replace")


def http_get_text(url: str, timeout: float = _REQUEST_TIMEOUT) -> str:
    """抓取一个文本页面，带重试与主机名兜底。

    Raises:
        urllib.error.HTTPError: 4xx/5xx（不重试）。
        OSError: 重试耗尽后的网络错误。
    """
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    last_error: Optional[BaseException] = None
    relaxed = False
    for attempt in range(_HTTP_RETRIES):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        context = _relaxed_context() if relaxed else tls_context(host)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                raw = response.read()
                return _decode_body(raw, response.headers.get("Content-Encoding", ""))
        except urllib.error.HTTPError:
            raise  # 404/403 属于确定性失败，交给调用方跳过
        except ssl.SSLCertVerificationError as exc:
            last_error = exc
            if not relaxed:
                relaxed = True  # 证书链有效、仅主机名不匹配：放宽后立即重试
                continue
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
            last_error = exc
        if attempt + 1 < _HTTP_RETRIES:
            time.sleep(_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)])
    raise OSError("抓取失败: %s (%s)" % (url, last_error))


class _Fetcher:
    """带全局节流、可取消的页面抓取器。"""

    def __init__(self, cancel: Optional[threading.Event] = None,
                 min_interval: float = _MIN_REQUEST_INTERVAL) -> None:
        self._cancel = cancel
        self._min_interval = max(0.0, float(min_interval))
        self._last_time = 0.0
        self._lock = threading.Lock()

    def cancelled(self) -> bool:
        return bool(self._cancel is not None and self._cancel.is_set())

    def _throttle(self) -> None:
        """保证相邻两次请求起点间隔 >= min_interval（可被取消打断）。"""
        with self._lock:
            while True:
                now = time.monotonic()
                wait = self._min_interval - (now - self._last_time)
                if wait <= 0:
                    break
                if self.cancelled():
                    return
                time.sleep(min(wait, 0.05))
            self._last_time = time.monotonic()

    def fetch(self, url: str) -> str:
        self._throttle()
        return http_get_text(url)

    def fetch_many(self, urls: Iterable[str]) -> List[Tuple[Optional[str], Optional[str]]]:
        """并发抓取若干页面，返回与输入同序的 ``(html, error)`` 列表。"""
        url_list = list(urls)
        if not url_list:
            return []
        results: List[Tuple[Optional[str], Optional[str]]] = [
            (None, "cancelled") for _ in url_list
        ]

        def worker(index: int, url: str) -> None:
            if self.cancelled():
                results[index] = (None, "cancelled")
                return
            try:
                results[index] = (self.fetch(url), None)
            except BaseException as exc:  # noqa: BLE001 - 单页失败不应杀掉扫描
                results[index] = (None, "%s: %s" % (type(exc).__name__, exc))

        workers = max(1, min(_SCAN_WORKERS, len(url_list)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="joshw-scan") as pool:
            futures = [pool.submit(worker, i, u) for i, u in enumerate(url_list)]
            for future in futures:
                future.result()
        return results


# ---------------------------------------------------------------------------
# 索引缓存（7 天）
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def cache_dir() -> str:
    """索引缓存目录：``%LOCALAPPDATA%/MSU1Converter/joshw_cache``。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, _CACHE_SUBDIR)


def cache_file(key: str) -> str:
    """返回某机种的缓存文件路径（key 非法时抛 ValueError）。"""
    if not _KEY_RE.match(key or ""):
        raise ValueError("非法机种 key: %r" % (key,))
    return os.path.join(cache_dir(), "%s.json" % key)


def _valid_entry(entry: object) -> bool:
    """校验缓存里的单个条目结构是否完整。"""
    if not isinstance(entry, dict):
        return False
    if not isinstance(entry.get("name"), str) or not entry.get("name"):
        return False
    if not isinstance(entry.get("url"), str) or not entry.get("url"):
        return False
    if not isinstance(entry.get("size"), int):
        return False
    if not isinstance(entry.get("mtime"), str):
        return False
    if not isinstance(entry.get("letter"), str):
        return False
    return True


def _load_cache_payload(key: str,
                        ttl_days: float = _CACHE_TTL_DAYS) -> Optional[Tuple[List[Dict[str, object]], float]]:
    """读取缓存并连同时间戳一起返回 ``(entries, ts)``；不可用时返回 ``None``。

    :func:`load_cache` 与 :func:`best_index` 共用这段逻辑，保证「是否算新鲜」
    的判定完全一致。
    """
    try:
        path = cache_file(key)
    except ValueError:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    stamp = payload.get("ts")
    entries = payload.get("entries")
    if not isinstance(stamp, (int, float)) or not isinstance(entries, list):
        return None
    if float(ttl_days) > 0:
        age_days = (time.time() - float(stamp)) / 86400.0
        if age_days > float(ttl_days) or age_days < -1.0:
            return None  # 过期（或时钟回拨异常）
    if not all(_valid_entry(item) for item in entries):
        return None
    return [dict(item) for item in entries], float(stamp)


def load_cache(key: str, ttl_days: float = _CACHE_TTL_DAYS) -> Optional[List[Dict[str, object]]]:
    """读取缓存；缺失 / 过期 / 损坏一律返回 ``None``。"""
    payload = _load_cache_payload(key, ttl_days)
    if payload is None:
        return None
    return payload[0]


def save_cache(key: str, entries: List[Dict[str, object]]) -> None:
    """原子写入缓存（写临时文件后 ``os.replace``，失败静默）。"""
    try:
        path = cache_file(key)
    except ValueError:
        return
    payload = {"ts": time.time(), "key": key, "entries": list(entries)}
    tmp_path = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp_path, path)
    except OSError:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def clear_cache(key: Optional[str] = None) -> None:
    """清除某机种缓存；``key`` 为 ``None`` 时清空全部缓存。"""
    directory = cache_dir()
    if key is None:
        try:
            names = os.listdir(directory)
        except OSError:
            return
        targets = [os.path.join(directory, name) for name in names
                   if name.endswith((".json", ".json.tmp", ".tmp"))]
    else:
        try:
            targets = [cache_file(key)]
        except ValueError:
            return
    for path in targets:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 内置索引快照（打包进 exe，首次选机种时无需联网）
# ---------------------------------------------------------------------------

# 解析结果的小缓存（key -> 记录）：最多留 4 个机种，约几 MB，避免来回切机种时
# 反复解压几 MB 的 JSON。键里带上 (路径, mtime, 大小)，文件被替换后自动失效。
_BUNDLED_MEMO: Dict[str, Dict[str, object]] = {}
_BUNDLED_LOCK = threading.Lock()
_BUNDLED_MEMO_MAX = 4


def bundled_dir() -> str:
    """返回内置索引快照目录（``index/<key>.json.gz``）。

    三种打包形态下 ``index/`` 落的位置不一样，这里按「先找存在的」处理：

    =========================  ============================================
    形态                        位置
    =========================  ============================================
    源码态                      ``<本文件目录>/index``
    PyInstaller onefile         ``sys._MEIPASS/index``
    **Nuitka onefile**          ``<本文件目录>/index``（解包临时目录）
    Nuitka ``--standalone``     ``<exe 同级>/index``
    =========================  ============================================

    ⚠ 曾经只认 ``sys._MEIPASS``，那是 PyInstaller 的私有属性，**Nuitka 没有**。
    换 Nuitka 之后 ``bundled_dir()`` 会一路走回源码分支，打包后读不到快照 →
    下载页第一次打开就联网全量扫描（30 个机种、每个几百到上千条），
    表现为「点了机种转半天，甚至直接超时」。

    目录不存在时也照常返回首选路径，由调用方按「没有快照」处理。
    """
    cands = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(os.path.join(str(meipass), _BUNDLED_SUBDIR))

    # Nuitka onefile：数据文件随 exe 解包到临时目录，__file__ 指那儿
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              _BUNDLED_SUBDIR))

    # Nuitka --standalone / PyInstaller onedir：数据就在 exe 同级
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        cands.append(os.path.join(
            os.path.dirname(os.path.abspath(sys.executable)), _BUNDLED_SUBDIR))

    for d in cands:
        if os.path.isdir(d):
            return d
    return cands[-1]


def _bundled_file(key: str) -> Optional[str]:
    """返回某机种快照文件的绝对路径；``key`` 非法时返回 ``None``。"""
    if not _KEY_RE.match(key or ""):
        return None
    return os.path.join(bundled_dir(), "%s.json.gz" % key)


def load_bundled(key: str) -> Optional[Tuple[List[Dict[str, object]], float]]:
    """读取内置快照 ``index/<key>.json.gz``。

    Args:
        key: 机种 key（见 :data:`PLATFORMS`）。

    Returns:
        ``(entries, ts)``；文件缺失、gzip 损坏、JSON 非法或条目字段不合契约
        时返回 ``None``。``entries`` 是全新的字典副本，调用方可随意修改。
    """
    path = _bundled_file(key)
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    stamp_key = (float(stat.st_mtime), int(stat.st_size))
    with _BUNDLED_LOCK:
        memo = _BUNDLED_MEMO.get(key)
        if memo is not None and memo.get("file") == path and memo.get("stamp") == stamp_key:
            cached_entries = memo.get("entries")
            if isinstance(cached_entries, list):
                return ([dict(item) for item in cached_entries],
                        float(memo.get("ts") or 0.0))

    try:
        with gzip.open(path, "rb") as handle:
            payload = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, EOFError):
        return None
    if not isinstance(payload, dict):
        return None
    raw_entries = payload.get("entries")
    raw_ts = payload.get("ts")
    if not isinstance(raw_entries, list):
        return None
    if not isinstance(raw_ts, (int, float)):
        return None
    if not all(_valid_entry(item) for item in raw_entries):
        return None

    entries: List[Dict[str, object]] = [dict(item) for item in raw_entries]
    ts = float(raw_ts)
    with _BUNDLED_LOCK:
        if len(_BUNDLED_MEMO) >= _BUNDLED_MEMO_MAX and key not in _BUNDLED_MEMO:
            # 简单淘汰：丢掉最先插入的一项（dict 保序）
            _BUNDLED_MEMO.pop(next(iter(_BUNDLED_MEMO)), None)
        _BUNDLED_MEMO[key] = {
            "file": path,
            "stamp": stamp_key,
            "entries": entries,
            "ts": ts,
        }
    return [dict(item) for item in entries], ts


def best_index(key: str,
               ttl_days: float = _CACHE_TTL_DAYS) -> Tuple[Optional[List[Dict[str, object]]],
                                                           float, str]:
    """挑选当前可用的索引来源。

    Args:
        key: 机种 key。
        ttl_days: 本地缓存的新鲜度阈值（天）；``<= 0`` 表示不看时间。

    Returns:
        ``(entries, ts, source)``，``source`` 取值为：

        * ``"cache"``   -- 本地缓存新鲜，``ts`` 是缓存写入时间；
        * ``"bundled"`` -- 本地缓存不可用，退化到内置快照（**即使快照比 TTL 旧
          也照旧返回**：先让用户看到列表，需要最新数据时再手动点「清除缓存」重扫）；
        * ``"none"``    -- 两者都没有，``entries`` 为 ``None``、``ts`` 为 ``0.0``。
    """
    payload = _load_cache_payload(key, ttl_days)
    if payload is not None and payload[0]:
        return payload[0], payload[1], "cache"
    bundled = load_bundled(key)
    if bundled is not None and bundled[0]:
        return bundled[0], bundled[1], "bundled"
    return None, 0.0, "none"


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------

def _report(progress: Optional[Callable[[int, int, str], None]],
            done: int, total: int, message: str) -> None:
    """安全地调用进度回调（回调异常不影响扫描）。"""
    if progress is None:
        return
    try:
        progress(int(done), int(total), message)
    except Exception:  # noqa: BLE001 - GUI 回调异常不应中断扫描
        pass


def _sort_key(entry: Dict[str, object]) -> Tuple[str, str]:
    return (str(entry.get("name", "")).lower(), str(entry.get("url", "")))


def scan_platform(key: str,
                  progress: Optional[Callable[[int, int, str], None]] = None,
                  cancel: Optional[threading.Event] = None) -> List[Dict[str, object]]:
    """扫描一个机种的全部 .7z 音源条目。

    Args:
        key: 机种 key（见 :data:`PLATFORMS`）。
        progress: 回调 ``progress(done, total, msg)``；``total`` 是**已发现**
            页面数，根页面解析前传 0。
        cancel: ``threading.Event``，置位后尽快返回已抓到的部分结果。

    Returns:
        条目列表，按 ``name`` 忽略大小写排序，``url`` 去重。

    Raises:
        KeyError: 未知机种。
        RuntimeError: 根页面抓不到，或所有子页面都失败且没有任何条目。
    """
    pf = platform(key)  # 未知 key -> KeyError
    root = base_url(pf)
    name = str(pf["name"])
    fetcher = _Fetcher(cancel=cancel)

    _report(progress, 0, 0, "正在读取 %s 索引…" % name)
    if fetcher.cancelled():
        return []

    try:
        root_html = fetcher.fetch(root)
    except BaseException as exc:  # noqa: BLE001
        raise RuntimeError("无法访问 %s 根索引: %s" % (root, exc)) from exc

    entries: List[Dict[str, object]] = []
    seen_urls: set = set()

    def absorb(items: Iterable[Dict[str, object]]) -> None:
        for item in items:
            url = str(item.get("url", ""))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            entries.append(item)

    root_entries = parse_index(root_html, root)
    absorb(root_entries)
    level: List[Tuple[str, str]] = _parse_dirs(root_html, root)
    total_pages = 1 + len(level)
    done_pages = 1
    _report(progress, done_pages, total_pages,
            "%s：根目录含 %d 个条目、%d 个子目录" % (name, len(root_entries), len(level)))

    failures = 0
    attempts = 1
    depth = 0
    while level and depth < _MAX_DIR_DEPTH:
        depth += 1
        if fetcher.cancelled():
            break
        page_urls = [url for _name, url in level]
        results = fetcher.fetch_many(page_urls)
        next_level: List[Tuple[str, str]] = []
        for (dir_name, page_url), (page_html, error) in zip(level, results):
            attempts += 1
            done_pages += 1
            if page_html is None:
                failures += 1
                _report(progress, done_pages, total_pages,
                        "%s：跳过 %s（%s）" % (name, dir_name, error or "失败"))
                continue
            page_entries = parse_index(page_html, page_url)
            absorb(page_entries)
            if not page_entries:
                # 页面没有归档 —— 可能是 kss/hoot 那种容器目录，再下探一层
                next_level.extend(_parse_dirs(page_html, page_url))
            _report(progress, done_pages, total_pages,
                    "%s：%s -> %d 个条目" % (name, dir_name, len(page_entries)))
        level = next_level
        if level:
            total_pages += len(level)
            _report(progress, done_pages, total_pages, "%s：继续下探 %d 个目录" % (name, len(level)))

    if fetcher.cancelled():
        _report(progress, done_pages, total_pages, "%s：已取消，返回已抓取的 %d 个条目" % (name, len(entries)))
    elif not entries and failures and failures >= attempts - 1:
        raise RuntimeError("扫描 %s 失败：%d 个页面全部无法访问" % (name, failures))

    entries.sort(key=_sort_key)
    if entries and not fetcher.cancelled():
        save_cache(key, entries)
    _report(progress, done_pages, total_pages, "%s：共 %d 个音源包" % (name, len(entries)))
    return entries
