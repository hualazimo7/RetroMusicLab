#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cover_igdb.py — 用 IGDB 补 libretro-thumbnails 抓不到的封面。

## 为什么需要第二个源

`cover_art.py` 只用 libretro-thumbnails。实测（`vendor/_audit_cover_hit.py`）
33 个机种里 **30 个有源、3 个没有**（Switch / FM Towns / PC），而且
Xbox 360 命中率 **0%**（整个库只有 12 条）、PS4 5%、Vita 2%、PS3 10%。
这些不是匹配算法的问题 —— 是那个库压根没收录这几代。

IGDB 这几代收录完整，正好补上。

## 为什么走 design.zaparoo.org 而不是 IGDB 官方 API

IGDB 官方 API 要两个头：``Client-ID`` + ``Authorization: Bearer <token>``，
token 必须拿 client_id/client_secret 去 ``id.twitch.tv/oauth2/token`` 换。
本机实测（2026-10-28）：

| 端点 | 结果 |
|---|---|
| ``images.igdb.com``（图 CDN） | 200，真 PNG |
| ``api.igdb.com/v4/*`` | 401（可达，只缺鉴权） |
| ``id.twitch.tv/oauth2/token`` | **不可达**：DNS 被污染到 Facebook 地址段 |

所以官方那条路是断在「换 token」这一步，而用户又没法注册 Twitch 开发者应用。

``design.zaparoo.org`` 是 Zaparoo Designer（做 NFC 卡片标签的开源项目）的
**公开后端**，``/api/search`` 不需要任何鉴权，返回的就是 IGDB 的原始对象
（``cover.url`` 直接指向 ``images.igdb.com``）。两个可达的端点它都替我们
用上了 —— 这就是「曲线救国」的具体含义。

⚠ 这是**别人的服务**，不是官方 API：
  * 请求间隔必须留够（:data:`MIN_GAP`），别当 CDN 刷；
  * 它哪天改接口/加限流，本模块会**安静失败**（返回 False + 原因），
    绝不能让封面把下载或转码搞崩；
  * 所以它是 **libretro 之后的兜底**，不是替代。

## 匹配口径

和 `cover_art` **完全共用** ``norm_title`` / ``pick`` —— 两边命中率才可比，
也不会出现「同一个游戏两个源挑出不同版本」这种怪事。

## 缓存

直接写进 `cover_art` 的缓存目录（``%LOCALAPPDATA%/MSU1Converter/covers/``），
并复用它的 ``_remember()`` 查找表。好处是：`cover_for_title()`、
`_cover_for_dir()`、`_backfill_audio()`、封面区接口**一行都不用改**，
IGDB 抓来的图对它们就是「本地已有的封面」。
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import cover_art

#: 数据源标识（日志里显示用）。
SOURCE = "IGDB"
#: Zaparoo Designer 的公开后端。**不是**官方 API，见模块开头。
SEARCH_URL = "https://design.zaparoo.org/api/search"
#: 图片 CDN（官方，可达）。
IMG_BASE = "https://images.igdb.com/igdb/image/upload"
#: 取哪一档尺寸。``t_cover_big`` 是 264×374，``_2x`` 是 528×748 ——
#: 盒绘嵌进 FLAC 当专辑图，264 太小（手机播放器上糊），原图又常有 1 MB。
SIZE = "t_cover_big_2x"
#: 单张图上上限。
MAX_IMG = 4 * 1024 * 1024
#: 两次请求的最小间隔（秒）。对方是公益项目，别刷。
MIN_GAP = 0.35
#: 接口一页固定 50 条（实测）。``count`` 是总数，超过一页才需要翻。
PAGE_SIZE = 50
#: 最多翻几页。再多就是「搜到一堆不相干的」，翻下去纯浪费对方带宽。
MAX_PAGES = 2

#: joshw 机种 key -> IGDB 平台 id（**可以多个**）。
#:
#: ⚠ 这些 id 是 2026-10-28 从接口返回的 ``platforms[].id`` 里**实测收集**的，
#:   不是照文档抄的：官方文档的 id 表和线上数据会漂。改之前先跑一遍
#:   `vendor/_probe_igdb.py` 看有没有静默全错。
#:
#: 多个 id 的原因和 `cover_art.PLATFORM_DIRS` 一样：joshw 一个子站常混装
#: 两代机器（``smd`` 混 MD 和 Sega CD，``hes`` 混 PCE 和 PCE CD）。
PLATFORM_IDS = {
    # ---- 复古主机 ----
    "spc": (19, 58),            # SNES / Super Famicom
    "nsf": (18, 51, 99),        # NES / Famicom Disk System / Famicom
    "gbs": (33, 22),            # Game Boy / Game Boy Color
    "gsf": (24,),               # Game Boy Advance
    "smd": (29, 78),            # Mega Drive·Genesis / Sega CD
    "hes": (86, 150),           # PC Engine / PC Engine CD
    "usf": (4,),                # Nintendo 64
    "ssf": (32,),               # Saturn
    "dsf": (23,),               # Dreamcast
    "2sf": (20, 159),           # Nintendo DS / DSi
    "wsr": (57, 123),           # WonderSwan / WonderSwan Color
    "kss_gg": (35,),            # Game Gear
    "kss_msx": (27, 53),        # MSX / MSX2
    "kss_sms": (64,),           # Master System / Mark III
    "3do": (50,),               # 3DO
    # ---- 现代主机 ----
    "psf": (7,),                # PlayStation
    "psf2": (8,),               # PlayStation 2
    "psf3": (9,),               # PlayStation 3
    "psf4": (48,),              # PlayStation 4
    "psp": (38,),               # PSP
    "vita": (46,),              # PS Vita
    "gcn": (21,),               # GameCube
    "wii": (5,),                # Wii
    "wiiu": (41,),              # Wii U
    "xbox": (11,),              # Xbox
    "x360": (12,),              # Xbox 360
    "3sf": (37, 137),           # 3DS / New 3DS
    "switch": (130,),           # Nintendo Switch
    # ---- 计算机与街机 ----
    "s98": (149, 125),          # PC-9800 / PC-8800
    "fmtowns": (118,),          # FM Towns
    "pc": (6, 13),              # Windows / DOS
    "hoot_arcade": (52,),       # Arcade
    "hoot_qsf": (52,),          # Arcade
}

_lock = threading.Lock()
_last_req = 0.0


def supported(key):
    """这个机种能不能走 IGDB。"""
    return bool(PLATFORM_IDS.get(str(key or "").strip().lower()))


def _throttle():
    """全局节流：两次请求至少隔 :data:`MIN_GAP` 秒。"""
    global _last_req
    with _lock:
        wait = MIN_GAP - (time.time() - _last_req)
        if wait > 0:
            time.sleep(wait)
        _last_req = time.time()


def _get(url, limit, timeout=30):
    """取一个 URL，返回 ``(bytes, 错误说明)``。永不抛异常。"""
    _throttle()
    req = urllib.request.Request(url, headers={
        "User-Agent": "MSU1Converter/cover_igdb (+design.zaparoo.org)",
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as exc:
        return b"", "HTTP %s" % exc.code
    except Exception as exc:                        # noqa: BLE001
        return b"", "%s: %s" % (type(exc).__name__, exc)
    if len(data) > limit:
        return b"", "响应超过 %d 字节" % limit
    return data, ""


def _strip_blocks(title):
    """去掉 ``(2019-08-09)(QubicGames)[Switch]`` 这类块，保留原始大小写和空格。

    joshw 的条目名长这样::

        #RaceDieRun (2019-08-09)(QubicGames)[Switch]
        Chrono Trigger (1995-03-11)(Square)[SNES]

    圆括号里是日期和发行商，方括号里是机种 —— 全是本地元数据，对搜索是噪音。
    """
    t = re.sub(r"\s*\([^()]*\)\s*", " ", str(title or ""))
    t = re.sub(r"\s*\[[^\[\]]*\]\s*", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _terms(title):
    """把一个 joshw 条目名拆成若干候选搜索词（去重、保序）。

    ## 为什么要拆

    接口（``/api/search``）对 ``searchTerm`` 是**近乎全等**的匹配，
    不是模糊检索。2026-10-28 实测同一个游戏：

    | searchTerm | 结果 |
    |---|---|
    | ``Chrono Trigger (1995-03-11)(Square)[SNES]`` | **0 条** |
    | ``Chrono Trigger`` | 1 条（``co3plw``） |
    | ``chrono trigger`` | 1 条（``co3plw``） |

    所以拿原始名直接搜是 **100% 失败** —— 这就是 ``_probe_igdb.py``
    第一次跑出「0/56 = 0%」的原因，不是网络也不是平台映射。

    ## 三个候选的顺序

    1. **去块保留大小写**（``Chrono Trigger``）—— 最自然，命中率最高；
    2. **归一化**（``cover_art.norm_title``：小写、去标点）—— 对
       ``#RaceDieRun`` → ``racedierun`` 这类带符号的名字更有效；
    3. **副标题前缀**（``Final Fantasy VII - ...`` → ``Final Fantasy VII``）
       —— 和 ``cover_art.pick`` 里那条「副标题 `` - `` 前缀」规则同源，
       只在全名搜不到时才用。

    挑错的兜底不在这儿 —— 在 :func:`search` 里，它把结果交给
    ``cover_art.pick`` 用同一套阈值复核，宁可漏不可错。
    """
    raw = str(title or "").strip()
    out = []

    def add(t):
        t = re.sub(r"\s+", " ", str(t or "")).strip()
        if t and t not in out:
            out.append(t)

    add(_strip_blocks(raw))
    add(cover_art.norm_title(raw))
    if out:
        head = out[0].split(" - ")[0].strip()
        if head:
            add(head)
    return out


def _search(key, title):
    """搜一次，返回 ``(rows, 错误说明)``。``rows`` 是 IGDB 的 game 对象列表。"""
    ids = PLATFORM_IDS.get(str(key or "").strip().lower())
    if not ids:
        return [], "机种 %s 没有 IGDB 平台映射" % (key or "?")
    terms = _terms(title)
    if not terms:
        return [], "没有游戏名"
    # ⚠ 一次只带一个 platformId：接口只认一个。多个平台（smd / hes / 2sf…）
    #   由 :func:`fetch` 依次试，第一个命中就停 —— 比一次拉全平台再过滤准。
    out, why = [], ""
    for term in terms:
        for pid in ids:
            rows, err = _search_one(term, pid)
            if err:
                why = err
                continue
            out.extend(rows)
            if out:
                return out, ""
    return [], why or "没有带封面的结果（%d 个搜索词都没命中）" % len(terms)


def _as_int(v):
    """尽量转 int，转不动给 0。``platforms[].id`` 偶尔是空/字符串。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _search_one(term, pid):
    """单个 (搜索词, 平台) 的检索，需要时翻页。返回 ``(rows, 错误说明)``。"""
    out, why = [], ""
    for page in range(1, MAX_PAGES + 1):
        q = urllib.parse.urlencode({"searchTerm": term, "page": page,
                                    "platformId": pid})
        raw, err = _get("%s?%s" % (SEARCH_URL, q), 2 * 1024 * 1024)
        if not raw:
            return out, (err or "空响应")
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return out, "返回的不是 JSON（接口可能改了）"
        rows = data.get("results") or []
        for r in rows:
            if not (r.get("cover") or {}).get("image_id"):
                continue
            # 带 platformId 时接口确实会筛（实测返回行都含该平台），这一条
            # 只是防御：它哪天忽略了这个参数，也不至于把别的机种的封面存进来。
            if not any(_as_int(p.get("id")) == pid
                       for p in (r.get("platforms") or ())):
                continue
            out.append(r)
        if out or not rows:
            break
        try:
            total = int(data.get("count") or 0)
        except (TypeError, ValueError):
            total = 0
        if total <= page * PAGE_SIZE:
            break
    return out, why


def search(key, title):
    """给某个游戏挑一条 IGDB 记录。返回 ``(row, 错误说明)``。

    挑法**完全复用** `cover_art.pick` —— 两边同一套归一化与相似度阈值，
    命中率才可比，也不会出现「同一个游戏两个源挑出不同版本」。
    """
    rows, why = _search(key, title)
    if not rows:
        return None, why
    items = [{"dir": str(r.get("id") or ""), "name": str(r.get("name") or "")}
             for r in rows]
    name = cover_art.pick(items, title)
    if not name:
        return None, "搜到 %d 条，但名字都不够像（宁可漏不可错）" % len(rows)
    for r in rows:
        if str(r.get("name") or "") == name:
            return r, ""
    return None, "内部匹配错位"


def _save_png(dest_dir, local, data):
    """把 PNG 字节落到 ``dest_dir/local``。返回绝对路径或空串。"""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ""
    if not cover_art._ensure_dir(dest_dir):
        return ""
    dest = os.path.join(dest_dir, local)
    try:
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except OSError:
        return ""
    return dest


def _get_png(image_id, size):
    """按 image_id 下一张 PNG。返回 ``(bytes, 错误)``。"""
    url = "%s/%s/%s.png" % (IMG_BASE, size, urllib.parse.quote(str(image_id)))
    data, err = _get(url, MAX_IMG, timeout=45)
    if not data:
        return b"", (err or "空响应")
    # 只认 PNG：IGDB 对不存在的尺寸也会回图（自动降级成 jpg），
    # 光看状态码会存一个后缀是 .png 的 JPEG 进去，后面嵌 FLAC 才炸。
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return b"", "返回的不是 PNG"
    return data, ""


def fetch(key, title, *, cancel=None):
    """给某个游戏抓**一张盒绘**。返回 ``(ok, 本地路径, 说明)``。

    落盘位置、文件名规则、查找表都复用 `cover_art` —— 这样
    ``cover_for_title()`` / ``_cover_for_dir()`` / ``_backfill_audio()``
    和封面区接口都不用知道有 IGDB 这回事。

    v6.0.7 说明：**没有**改成 ``fetch_all(limit=1)`` 的包装。两者关注点
    不同 —— 这个函数要能说清「为什么没拿到」（JPEG 不是 PNG / 搜不到 /
    已取消），而 ``fetch_all`` 是批量抓、失败一张就跳过。混在一起之后
    失败理由全变成「没搜到」，日志里就分不清了。
    """
    key = str(key or "").strip().lower()
    if not supported(key):
        return False, "", "机种 %s 没有 IGDB 平台映射" % (key or "?")
    title = str(title or "").strip()
    if not title:
        return False, "", "没有游戏名"

    # 先查本地（同一个游戏重复下载 / 上次已经抓过）。只认盒绘 ——
    # 用户明确要求「嵌入 flac 和 mp3 只嵌入 boxart」。
    for p, _kind in cover_art.covers_for_title(
            title, kinds=cover_art.BOXART_KINDS):
        return True, p, "已有"

    if cancel is not None and cancel.is_set():
        return False, "", "已取消"

    row, why = search(key, title)
    if not row:
        return False, "", why or "没搜到"

    image_id = str(row["cover"]["image_id"])
    local = "igdb_cover_%s.png" % image_id
    # v6.0.11：和 cover_art 一样按「机种\游戏名」分层，见 cover_art.rel_of
    dest_dir = os.path.join(cover_art.cache_root(),
                            cover_art.platform_sub(key),
                            cover_art._safe_name(title))
    dest = os.path.join(dest_dir, local)
    rel = cover_art.rel_of(key, title, local)

    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        cover_art._remember(title, [(rel, "cover")])
        return True, dest, "已有"

    if cancel is not None and cancel.is_set():
        return False, "", "已取消"

    data, err = _get_png(image_id, SIZE)
    if not data:
        # 「返回的不是 PNG」原样透出：那是**内容**问题，不是网络问题，
        # 日志里要能一眼分清（门禁 t_v605 有断言盯着）。
        return False, "", (err if err == "返回的不是 PNG"
                           else "下载失败：%s" % (err or "空响应"))
    p = _save_png(dest_dir, local, data)
    if not p:
        return False, "", "写盘失败：%s" % dest_dir

    cover_art._remember(title, [(rel, "cover")])
    return True, p, "IGDB「%s」%d KB" % (str(row.get("name") or title),
                                        len(data) // 1024)


#: IGDB 的图分三类。``cover`` 是竖版盒绘（能嵌音频），另外两类是截图。
#: 尺寸也分开：盒绘用 ``t_cover_big_2x``（528×748），截图用
#: ``t_screenshot_big_2x``（更宽，硬套盒绘尺寸会被裁）。
SHOT_SIZE = "t_screenshot_big_2x"


def fetch_all(key, title, *, cancel=None, limit=6, kinds=None):
    """给某个游戏抓 IGDB 的图，返回 ``[(本地路径, kind), ...]``。

    v6.0.7：用户要求「一个游戏的封面资源应该有若干个，下载时全下载」。
    IGDB 一个游戏能给 1 张 ``cover``（盒绘）+ 6~7 张 ``screenshots``
    （游戏截图）+ 若干 ``extra_images``。截图的尺寸明显更大，轮播时有变化。

    Args:
        limit: 最多下几张（含已经存在的）。
        kinds: 只要这些类（``("cover",)`` / ``("screenshot",)`` /
            ``None`` = 全要）。``fetch()`` 传 ``("cover",)`` 是因为
            只有盒绘能嵌音频。

    Returns:
        ``[(绝对路径, kind), ...]``，kind ∈ ``cover`` / ``screenshot`` /
        ``extra``。``cover`` 在 :data:`cover_art.BOXART_KINDS` 里，
        另外两个不在 —— 所以它们永远不会被嵌进 FLAC / MP3。
    """
    key = str(key or "").strip().lower()
    if not supported(key):
        return []
    title = str(title or "").strip()
    if not title:
        return []
    lim = max(1, int(limit or 6))
    want = set(kinds) if kinds else {"cover", "screenshot", "extra"}

    dest_dir = os.path.join(cover_art.cache_root(),
                            cover_art.platform_sub(key),
                            cover_art._safe_name(title))
    out = []

    # 旧版（v6.0.5 / v6.0.6）落的 ``{游戏名}.png`` —— 当时只有这一张，
    # 是 cover。认它，免得老用户升级后「图全没了」。
    legacy = os.path.join(dest_dir, cover_art._safe_name(title) + ".png")
    if "cover" in want and os.path.isfile(legacy):
        out.append((legacy, "cover"))

    if cancel is not None and cancel.is_set():
        return out[:lim]
    row, _why = search(key, title)
    if not row:
        return out[:lim]

    # ---- cover ----
    if "cover" in want and len(out) < lim:
        image_id = str((row.get("cover") or {}).get("image_id") or "")
        if image_id:
            local = "igdb_cover_%s.png" % image_id
            dest = os.path.join(dest_dir, local)
            if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                if dest not in [p for p, _k in out]:
                    out.append((dest, "cover"))
            elif cancel is None or not cancel.is_set():
                data, _err = _get_png(image_id, SIZE)
                p = _save_png(dest_dir, local, data) if data else ""
                if p:
                    out.append((p, "cover"))

    # ---- screenshots / extra_images ----
    for field, kind in (("screenshots", "screenshot"),
                        ("extra_images", "extra")):
        if kind not in want:
            continue
        seq = row.get(field)
        # ⚠ 实测：``extra_images`` 有时是**整数**（数量）而不是数组 ——
        # 直接 for 会 TypeError: 'int' object is not iterable。
        # ``screenshots`` 目前都是数组，但同一个接口的字段，一起防。
        if not isinstance(seq, (list, tuple)):
            continue
        for item in seq:
            if len(out) >= lim:
                break
            if cancel is not None and cancel.is_set():
                break
            if not isinstance(item, dict):
                continue
            image_id = str(item.get("image_id") or "")
            if not image_id:
                continue
            prefix = "igdb_shot_" if kind == "screenshot" else "igdb_extra_"
            local = "%s%s.png" % (prefix, image_id)
            dest = os.path.join(dest_dir, local)
            if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                if dest not in [p for p, _k in out]:
                    out.append((dest, kind))
                continue
            size = SHOT_SIZE if kind == "screenshot" else SIZE
            data, _err = _get_png(image_id, size)
            p = _save_png(dest_dir, local, data) if data else ""
            if p:
                out.append((p, kind))

    # 只把**这次新落的**写进查找表（老图早就记过了，重复写没意义）
    fresh = []
    known = {p for p, _k in cover_art.covers_for_title(title)}
    for p, k in out:
        if p in known:
            continue
        rel = cover_art.rel_of(key, title, os.path.basename(p))
        fresh.append((rel, k))
    if fresh:
        cover_art._remember(title, fresh)
    return out[:lim]
