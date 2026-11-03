#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zophar_source.py -- 按机种浏览 Zophar's Domain 音乐库的**原始音源**（核心层）。

与 :mod:`joshw_source` 平行：joshw 是「一台机种一个 Apache 目录，全是 .7z」，
zophar 是「一台机种一个游戏列表，每个游戏一个下载页」。两者的发现逻辑没法
共用，但**产出的条目契约完全一致**（见 :func:`parse_games`），所以下载层
（``joshw_dl.Downloader``）一行都不用改。

站点事实（2026-10-25 实测）：

* 机种页 ``https://www.zophar.net/music/<slug>`` 是分页的游戏表，每页 200 条，
  页脚写 ``Page 1 of N``，第 N 页用 ``?page=N``。
* 每个游戏页 ``/music/<slug>/<game>`` 的 ``<div id="mass_download">`` 里有
  **两种**批量包：

  - ``Download all files as MP3 (59 MB)``
    → ``…/<随机 token>/<名字> (MP3).zophar.zip``  ← **有损录音，不要**
  - ``Download original music files (12 KB)``
    → ``…/<名字> (EMU).zophar.zip``                ← **原始音源，无随机 token**

  本模块只取后者。随机 token 那条 URL 会变，原始音源这条**不带 token**，
  所以直接拼也行，但我们仍然从页面里解析，不硬编码。

* ``robots.txt`` 实测为空（无任何 Disallow），但仍按 :mod:`joshw_source` 的
  礼貌口径来：只在用户主动选机种时扫描，请求间隔 >= 0.25 秒，索引缓存 7 天。

为什么只收录 Game Gear / Master System
--------------------------------------
2026-10-25 对 zophar 全部 36 个机种页逐个抽样（每站 6 个游戏）确认：

===========================  =========  ==================
机种                          原始音源    本工具能否解码
===========================  =========  ==================
Game Gear (``.sgc``+m3u)      **有**      **能**（v5.4 起）
Master System (``.vgm``+m3u)  **有**      **能**（v5.0 起）
NES / SNES / MD / PS1 / …     有          能，但 joshw 已覆盖，不重复收录
PC-88 / PC-98 / X1 / X68000   有(``.s98``)  **不能**（.s98 未实现）
Commodore 64                  有(``.sid``)  **不能**（.sid 未实现）
Amiga / Arcade / Atari ST /   只有 MP3     —（无损录音，不能当音源）
Atari 8-Bit / MS-DOS / CD-i /
Sharp X1 / Windows / X68000 /
ZX Spectrum / MSX2 / FM Towns
===========================  =========  ==================

也就是说：**zophar 上「我们没有、而且拿得到原始音源、而且解得开」的机种，
就这两个。** 其余新机种要么只有 MP3 录音，要么是 ``.s98``/``.sid``。
这条结论写在模块里，免得下次又从头调研一遍。

公开 API 见 ``__all__``。
"""

from __future__ import annotations

import gzip
import html
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

__all__ = [
    "PROVIDER",
    "PLATFORMS",
    "USER_AGENT",
    "platform",
    "is_key",
    "system_url",
    "game_url",
    "parse_games",
    "page_count",
    "parse_downloads",
    "resolve",
    "scan_platform",
    "cache_dir",
    "cache_file",
    "load_cache",
    "save_cache",
    "clear_cache",
    "best_index",
    "human_size",
]

#: 下载层用它区分「条目 url 是归档直链」还是「条目 url 是游戏页，需先解析」。
PROVIDER = "zophar"

SITE = "https://www.zophar.net"
FILES_HOST = "fi.zophar.net"

#: 与 joshw_source 同一个 UA —— 实测 zophar 不挑 UA，但保持一致便于抓包对照。
USER_AGENT = "MSU1Converter/3.4 (personal archive tool)"

_GROUP = "Zophar 原声"

_REQUEST_TIMEOUT = 30.0
_HTTP_RETRIES = 3
_RETRY_BACKOFF = (0.6, 1.2, 2.4)
_MIN_REQUEST_INTERVAL = 0.25   # 秒，全局请求间隔下限
_SCAN_WORKERS = 2
_CACHE_TTL_DAYS = 7.0
_CACHE_SUBDIR = os.path.join("MSU1Converter", "zophar_cache")
_MAX_NAME_LENGTH = 120


def _pf(key: str, name: str, fmt: str, slug: str) -> Dict[str, str]:
    """构造一条机种记录。``slug`` 就是 URL 里的机种段。"""
    return {
        "key": key,
        "name": name,
        "fmt": fmt,
        "slug": slug,
        "group": _GROUP,
        "src": PROVIDER,
    }


PLATFORMS: List[Dict[str, str]] = [
    _pf("z_gg", "Game Gear", "SGC", "sega-game-gear-sgc"),
    _pf("z_sms", "Master System", "VGM", "sega-master-system-vgm"),
]

_BY_KEY: Dict[str, Dict[str, str]] = {item["key"]: item for item in PLATFORMS}

_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def platform(key: str) -> Dict[str, str]:
    """按 key 返回机种描述，不存在抛 :class:`KeyError`。"""
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError("未知机种: %r" % (key,))


def is_key(key: str) -> bool:
    """这个 key 是不是 zophar 的机种（下载层用它做 provider 分派）。"""
    return str(key or "") in _BY_KEY


def system_url(pf: Dict[str, str], page: int = 1) -> str:
    """机种页 URL；``page`` > 1 时带 ``?page=N``。"""
    url = "%s/music/%s" % (SITE, pf["slug"])
    if int(page or 1) > 1:
        url += "?page=%d" % int(page)
    return url


def game_url(pf: Dict[str, str], slug: str) -> str:
    """游戏页 URL。"""
    return "%s/music/%s/%s" % (SITE, pf["slug"], str(slug).strip("/"))


# ---------------------------------------------------------------------------
# 尺寸
# ---------------------------------------------------------------------------

_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_size(n) -> str:
    """字节数 → ``1.5 MB``（1024 进制，1 位小数）。"""
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


_SIZE_UNITS = {"b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
_SIZE_RE = re.compile(r"\(([\d.]+)\s*([BKMGT]?)B?\)", re.I)


def _parse_size_text(text: str) -> int:
    """``Download original music files (12 KB)`` → ``12288``。解析不了返回 0。"""
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    try:
        value = float(m.group(1))
    except ValueError:
        return 0
    unit = (m.group(2) or "b").lower()
    return int(round(value * _SIZE_UNITS.get(unit, 1)))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_CONTEXT: Optional[ssl.SSLContext] = None
_CONTEXT_LOCK = threading.Lock()


def _context() -> ssl.SSLContext:
    global _CONTEXT
    with _CONTEXT_LOCK:
        if _CONTEXT is None:
            _CONTEXT = ssl.create_default_context()
        return _CONTEXT


def _decode_body(raw: bytes, encoding: str) -> str:
    if "gzip" in (encoding or "").lower() and raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return raw.decode("utf-8", "replace")


def http_get_text(url: str, timeout: float = _REQUEST_TIMEOUT) -> str:
    """抓一个页面，带重试。

    Raises:
        urllib.error.HTTPError: 4xx/5xx（不重试，确定性失败）。
        OSError: 重试耗尽。
    """
    last: Optional[BaseException] = None
    for attempt in range(_HTTP_RETRIES):
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT,
                          "Accept-Language": "en-US,en;q=0.9"})
        try:
            with urllib.request.urlopen(request, timeout=timeout,
                                        context=_context()) as response:
                return _decode_body(
                    response.read(), response.headers.get("Content-Encoding", ""))
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
            last = exc
        if attempt + 1 < _HTTP_RETRIES:
            time.sleep(_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)])
    raise OSError("抓取失败: %s (%s)" % (url, last))


class _Fetcher:
    """带全局节流、可取消的抓取器（与 joshw_source 同口径）。"""

    def __init__(self, cancel: Optional[threading.Event] = None,
                 min_interval: float = _MIN_REQUEST_INTERVAL) -> None:
        self._cancel = cancel
        self._min_interval = max(0.0, float(min_interval))
        self._last = 0.0
        self._lock = threading.Lock()

    def cancelled(self) -> bool:
        return bool(self._cancel is not None and self._cancel.is_set())

    def _throttle(self) -> None:
        with self._lock:
            while True:
                wait = self._min_interval - (time.monotonic() - self._last)
                if wait <= 0:
                    break
                if self.cancelled():
                    return
                time.sleep(min(wait, 0.05))
            self._last = time.monotonic()

    def fetch(self, url: str) -> str:
        self._throttle()
        return http_get_text(url)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

#: 只认 ``<table id="gamelist">`` 里的行 —— 页面别处也有 ``/music/<slug>/…``
#: 链接（面包屑、推荐位），不加范围会把「随便一个游戏」当成列表项。
_TABLE_RE = re.compile(
    r'<table[^>]+id="gamelist"[^>]*>(?P<body>.*?)</table>', re.S | re.I)

_ROW_RE = re.compile(
    r'<td\s+class="name"\s*>\s*<a\s+href="(?P<href>[^"]+)"[^>]*>(?P<label>.*?)</a>',
    re.S | re.I)

_YEAR_RE = re.compile(r'<td\s+class="year"\s*>(?P<cell>.*?)</td>', re.S | re.I)

_PAGE_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)

_FOUND_RE = re.compile(r"Found\s+([\d,]+)\s+games", re.I)

#: 下载页 ``#mass_download`` 里的两种包。``(?P<label>.*?)`` 用来抓括号里的体积。
_MASS_RE = re.compile(r'<div[^>]+id="mass_download"[^>]*>(?P<body>.*?)</div>', re.S | re.I)

_ANCHOR_RE = re.compile(
    r'<a\s+href="(?P<href>[^"]+)"[^>]*>\s*(?:<p[^>]*>)?(?P<label>.*?)(?:</p>)?\s*</a>',
    re.S | re.I)


def _clean(text: str) -> str:
    """去标签 + 还原实体 + 压空白。"""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text or ""))).strip()


def page_count(html_text: str) -> int:
    """从 ``Page 1 of N`` 取总页数；取不到按 1 页算。"""
    m = _PAGE_RE.search(html_text or "")
    if not m:
        return 1
    try:
        return max(1, int(m.group(2)))
    except ValueError:
        return 1


def found_count(html_text: str) -> int:
    """``Found 257 games with 19206 tracks!`` → ``257``；取不到返回 0。"""
    m = _FOUND_RE.search(html_text or "")
    if not m:
        return 0
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return 0


def parse_games(html_text: str, pf: Dict[str, str]) -> List[Dict[str, object]]:
    """解析一页游戏表。

    Returns:
        条目列表，每项 **与 joshw_source.parse_index 同契约**：
        ``{"name","url","size","mtime","letter"}``，外加 ``"src":"zophar"``。

        * ``name``  -- 游戏名（如 ``Addams Family, The``）
        * ``url``   -- **游戏页**绝对 URL（不是归档直链，下载前要 :func:`resolve`）
        * ``size``  -- 恒为 0（zophar 列表页不给原始包体积，要进游戏页才知道）
        * ``mtime`` -- 年份（``1993``），列表页只有这个；没有就是空串
        * ``letter``-- 名字首字母大写（UI 分组用）
    """
    m = _TABLE_RE.search(html_text or "")
    if not m:
        return []
    body = m.group("body")

    # 行级切分：先按 <tr 切开，保证 name 和 year 落在同一行里
    rows = re.split(r"<tr\b", body, flags=re.I)
    out: List[Dict[str, object]] = []
    seen = set()
    for row in rows:
        nm = _ROW_RE.search(row)
        if not nm:
            continue
        href = (nm.group("href") or "").strip()
        if not href or href.startswith(("?", "#")):
            continue
        name = _clean(nm.group("label"))
        if not name:
            continue
        url = urllib.parse.urljoin(SITE + "/", href)
        if url in seen:
            continue
        seen.add(url)
        year = ""
        ym = _YEAR_RE.search(row)
        if ym:
            year = _clean(ym.group("cell"))[:4]
            if not year.isdigit():
                year = ""
        out.append({
            "name": name,
            "url": url,
            "size": 0,
            "mtime": year,
            "letter": name[:1].upper(),
            "src": PROVIDER,
        })
    return out


def parse_downloads(html_text: str) -> List[Dict[str, object]]:
    """解析游戏页 ``#mass_download`` 里的批量包。

    Returns:
        ``[{"url","label","size","kind"}]``，``kind`` 为：

        * ``"emu"``  -- 原始音源（``(EMU).zophar.zip``）—— **我们要的**
        * ``"mp3"``  -- MP3 录音（``(MP3).zophar.zip``）—— 有损，不用
        * ``"other"``-- 其它
    """
    m = _MASS_RE.search(html_text or "")
    body = m.group("body") if m else (html_text or "")
    out: List[Dict[str, object]] = []
    seen = set()
    for am in _ANCHOR_RE.finditer(body):
        href = (am.group("href") or "").strip()
        if not href.startswith("http"):
            continue
        if FILES_HOST not in href:
            continue
        label = _clean(am.group("label"))
        plain = urllib.parse.unquote(href).lower()
        if "(emu)" in plain:
            kind = "emu"
        elif "(mp3)" in plain or label.lower().startswith("download all files as mp3"):
            kind = "mp3"
        else:
            kind = "other"
        if href in seen:
            continue
        seen.add(href)
        out.append({
            "url": href,
            "label": label,
            "size": _parse_size_text(label),
            "kind": kind,
        })
    return out


def resolve(game_page: str,
            cancel: Optional[threading.Event] = None,
            fetcher: Optional[_Fetcher] = None) -> Dict[str, object]:
    """游戏页 → 原始音源归档直链。

    Args:
        game_page: 游戏页绝对 URL（条目里的 ``url``）。
        cancel: 置位后尽快返回（抛 :class:`RuntimeError` 交给调用方跳过）。
        fetcher: 复用调用方的节流器（批量解析时**必须**传同一个，否则
            并发下节流失效）。

    Returns:
        ``{"ok":True,"url","size","label"}``

    Raises:
        RuntimeError: 页面抓不到，或这个游戏**没有**原始音源（只有 MP3 录音）。
    """
    page = str(game_page or "").strip()
    if not page:
        raise RuntimeError("缺少游戏页 URL")
    if cancel is not None and cancel.is_set():
        raise RuntimeError("已取消")
    getter = fetcher.fetch if fetcher is not None else http_get_text
    try:
        text = getter(page)
    except BaseException as exc:                     # noqa: BLE001
        raise RuntimeError("抓游戏页失败: %s" % exc) from exc

    items = parse_downloads(text)
    emu = [it for it in items if it["kind"] == "emu"]
    if not emu:
        kinds = {it["kind"] for it in items}
        if "mp3" in kinds:
            raise RuntimeError("这个游戏只有 MP3 录音，没有原始音源")
        raise RuntimeError("这个游戏没有可下载的音源包")
    best = emu[0]
    return {"ok": True, "url": str(best["url"]),
            "size": int(best["size"] or 0), "label": str(best["label"] or "")}


def resolve_many(pages: List[str],
                 progress: Optional[Callable[[int, int, str], None]] = None,
                 cancel: Optional[threading.Event] = None,
                 ) -> Dict[str, Dict[str, object]]:
    """批量解析。返回 ``{game_page: resolve() 结果或 {"ok":False,"error":...}}``。

    并发 2 路、共用同一个节流器 —— 单页失败不影响其它页。
    """
    pages = [p for p in (pages or []) if p]
    if not pages:
        return {}
    fetcher = _Fetcher(cancel=cancel)
    results: Dict[str, Dict[str, object]] = {}
    lock = threading.Lock()
    done = [0]
    total = len(pages)

    def work(page: str) -> None:
        if cancel is not None and cancel.is_set():
            res: Dict[str, object] = {"ok": False, "error": "已取消"}
        else:
            try:
                res = resolve(page, cancel=cancel, fetcher=fetcher)
            except BaseException as exc:             # noqa: BLE001
                res = {"ok": False, "error": str(exc)}
        with lock:
            results[page] = res
            done[0] += 1
            if progress is not None:
                try:
                    progress(done[0], total,
                             str(res.get("error") or res.get("label") or ""))
                except Exception:                    # noqa: BLE001
                    pass

    workers = max(1, min(_SCAN_WORKERS, total))
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="zophar-res") as pool:
        list(pool.map(work, pages))
    return results


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------

def _report(progress: Optional[Callable[[int, int, str], None]],
            done: int, total: int, message: str) -> None:
    if progress is None:
        return
    try:
        progress(int(done), int(total), message)
    except Exception:                                # noqa: BLE001
        pass


def scan_platform(key: str,
                  progress: Optional[Callable[[int, int, str], None]] = None,
                  cancel: Optional[threading.Event] = None,
                  ) -> List[Dict[str, object]]:
    """扫描一个机种的全部游戏（走完所有分页）。

    Args:
        key: 机种 key（见 :data:`PLATFORMS`）。
        progress: ``progress(done, total, msg)``，``total`` 是页数。
        cancel: 置位后返回已抓到的部分结果。

    Returns:
        条目列表（按名字忽略大小写排序，``url`` 去重）。**成功就写缓存**。

    Raises:
        KeyError: 未知机种。
        RuntimeError: 第一页都抓不到。
    """
    pf = platform(key)                                # 未知 key -> KeyError
    fetcher = _Fetcher(cancel=cancel)
    name = str(pf["name"])

    _report(progress, 0, 1, "正在读取 %s 索引…" % name)
    if fetcher.cancelled():
        return []
    try:
        first = fetcher.fetch(system_url(pf, 1))
    except BaseException as exc:                      # noqa: BLE001
        raise RuntimeError("无法访问 %s 索引: %s" % (system_url(pf, 1), exc)) from exc

    total_pages = page_count(first)
    entries = parse_games(first, pf)
    _report(progress, 1, total_pages,
            "%s：第 1/%d 页 -> %d 个游戏" % (name, total_pages, len(entries)))

    for page in range(2, total_pages + 1):
        if fetcher.cancelled():
            break
        try:
            text = fetcher.fetch(system_url(pf, page))
        except BaseException as exc:                  # noqa: BLE001
            _report(progress, page, total_pages,
                    "%s：跳过第 %d 页（%s）" % (name, page, exc))
            continue
        got = parse_games(text, pf)
        entries.extend(got)
        _report(progress, page, total_pages,
                "%s：第 %d/%d 页 -> %d 个游戏" % (name, page, total_pages, len(got)))

    # 去重（分页边界上偶有重复）+ 排序
    seen = set()
    uniq: List[Dict[str, object]] = []
    for item in entries:
        url = str(item.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        uniq.append(item)
    uniq.sort(key=lambda e: (str(e.get("name", "")).lower(), str(e.get("url", ""))))

    if uniq and not fetcher.cancelled():
        save_cache(key, uniq)
    _report(progress, total_pages, total_pages,
            "%s：共 %d 个游戏" % (name, len(uniq)))
    return uniq


# ---------------------------------------------------------------------------
# 缓存（7 天）
# ---------------------------------------------------------------------------

def cache_dir() -> str:
    """缓存目录：``%LOCALAPPDATA%/MSU1Converter/zophar_cache``。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, _CACHE_SUBDIR)


def cache_file(key: str) -> str:
    """某机种的缓存文件路径（key 非法抛 ValueError）。"""
    if not _KEY_RE.match(key or ""):
        raise ValueError("非法机种 key: %r" % (key,))
    return os.path.join(cache_dir(), "%s.json" % key)


def _valid_entry(entry: object) -> bool:
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


def load_cache(key: str,
               ttl_days: float = _CACHE_TTL_DAYS) -> Optional[Tuple[List[Dict[str, object]], float]]:
    """读缓存，返回 ``(entries, ts)``；缺失/过期/损坏返回 ``None``。"""
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
        age = (time.time() - float(stamp)) / 86400.0
        if age > float(ttl_days) or age < -1.0:
            return None
    if not all(_valid_entry(it) for it in entries):
        return None
    return [dict(it) for it in entries], float(stamp)


def save_cache(key: str, entries: List[Dict[str, object]]) -> None:
    """原子写缓存（失败静默）。"""
    try:
        path = cache_file(key)
    except ValueError:
        return
    payload = {"ts": time.time(), "key": key, "src": PROVIDER,
               "entries": list(entries)}
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def clear_cache(key: Optional[str] = None) -> None:
    """清某机种缓存；``key`` 为 ``None`` 清全部。"""
    directory = cache_dir()
    if key is None:
        try:
            names = os.listdir(directory)
        except OSError:
            return
        targets = [os.path.join(directory, n) for n in names
                   if n.endswith((".json", ".tmp"))]
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


def best_index(key: str, ttl_days: float = _CACHE_TTL_DAYS):
    """``(entries, ts, source)``，``source`` ∈ ``cache`` / ``none``。

    zophar 不打包内置快照（体积大且更新频繁），所以没有 ``bundled`` 分支。
    """
    payload = load_cache(key, ttl_days)
    if payload is not None and payload[0]:
        return payload[0], payload[1], "cache"
    return None, 0.0, "none"
