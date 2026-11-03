#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cover_art.py — 给下载下来的音源包配一张游戏封面。

## 数据源为什么不是 The Cover Project

用户最初点的是 `thecoverproject.net`。**它用不了**，原因是硬的：

* 整站挂在 **Cloudflare 人机验证**后面 —— 不是限速，是 `HTTP 403` +
  `Cf-Mitigated: challenge`，连 `index.php` 都拿不到，返回的是
  "Just a moment..." 那个 5 KB 的挑战页。程序无法通过（也不应该去绕）。
* 它的 `robots.txt` 另外还写着 ``Disallow: *.jpg$`` / ``/images/`` /
  ``/uploads/`` / ``/download_cover.php`` —— 就算没有挑战，封面文件本身
  也是明确不许抓的。

所以改用 **libretro-thumbnails**：RetroArch 的官方缩略图库，
Apache 目录索引可列、直连可用、不需要 API key，覆盖的机种和本工具
几乎一一对应，命名规范是 ``<游戏名> (<区域>).png``。

## 怎么匹配

joshw 的条目名是 **No-Intro 风格**，元信息全在括号里：

    3 Ninjas Kick Back (1994-11)(Malibu)(Sony Imagesoft)[SNES]
    7th Saga, The [Elnard] (1993-04-23)(Produce!)(Enix)[SNES]

libretro 的文件名是：

    3 Ninjas Kick Back (USA).png
    7th Saga, The (USA).png

两边**砍到第一个 ``(``** 就对齐了（joshw 多出来的 ``[...]`` 也一并去掉）。
实测这一条规则能覆盖绝大多数条目，剩下的靠 ``difflib`` 相似度兜底，
低于阈值就**放弃**——宁可没有封面，也不要贴错一张。

## 命中率（实测，每个机种随机抽 40 条 joshw 条目；``vendor/_audit_cover_hit.py``）

| 机种 | 封面库条目 | 命中 |
|---|---|---|
| GBA（gsf） | 5961 | **98%** |
| Saturn（ssf） | 2294 | **98%** |
| SNES（spc） | 3689 | **92%** |
| NES（nsf） | 13835 | **92%** |
| 3DO | 653 | **92%** |
| N64（usf） | 1115 | **90%** |
| PS1（psf） | 9301 | **88%** |
| GB + GBC（gbs） | 3171 | **85%** |
| WS + WSC（wsr） | 538 | **85%** |
| MD + Sega CD（smd） | 2961 | **82%** |
| PCE + PCE CD（hes） | 1430 | **82%** |
| DC（dsf） | 1332 | **82%** |
| NDS + DSi（2sf） | 7348 | **78%** |
| PC-98（s98） | 1370 | **28%** |

合计 **84%**。没命中的绝大多数是**库里真没有**（日版独占、原型、欧版独占的
小品），少数是命名差异（``Pokemon Gold`` vs libretro 的
``Pokemon - Gold Version``）。``s98`` 只有 28% 是意料之中：PC-98 那一代大量
是同人作品，而且游戏名重复度高（一堆 ``Rance`` / ``Ys`` 变体），相似度放宽会
大面积贴错。**宁可漏，不可错** —— 贴错封面比没有封面更糟，所以相似度阈值定在
0.86，且副标题前缀必须带 `` - ``。

## 缓存

    %LOCALAPPDATA%/MSU1Converter/covers/
        _idx/<key>.json     某机种的全部封面文件名（30 天有效）
        _lookup.json        归一化游戏名 -> 封面相对路径（转换时用）
        <key>/<文件名>.png  封面本体

机种列表一次约 1 MB（SNES 3689 条 / 975 KB），所以**按机种缓存 30 天**，
不是每次下载都去抓。封面本体按需下载，一次一张。

⚠ 所有对外函数**都不抛异常**：封面是锦上添花，绝不能因为它把下载或
转换搞失败。
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

#: 数据源标识（日志/界面用）。
SOURCE = "libretro-thumbnails"
BASE = "https://thumbnails.libretro.com"

#: 机种列表缓存有效期。站点半年才更新一批，30 天足够。
INDEX_TTL = 30 * 86400
#: 两次请求之间的最小间隔（秒）。列表一次、图片一次，都是重请求。
MIN_GAP = 0.3
#: 单次列表请求的硬上限。最大的机种（PS1）约 1.2 MB，8 MB 是安全阀。
MAX_LIST_BYTES = 8 * 1024 * 1024
#: 单张封面上限。实测最大 1.5 MB，4 MB 足够，防止被一个坏 URL 拖死。
MAX_IMG_BYTES = 4 * 1024 * 1024

#: joshw 机种 key -> libretro 缩略图库目录名（**可以有多个**）。
#:
#: 多个目录不是凑数：joshw 的一个子站常常混装两代机器，而 libretro 把它们
#: 拆成不同目录 —— 不合并的话这些条目会**成片地没有封面**：
#:
#:   ``smd``  joshw 把 Mega Drive 和 Sega CD 放一起，条目标 ``[SMD]`` / ``[SCD]``
#:   ``hes``  joshw 把 HuCard 和 PC Engine CD 放一起，标 ``[PCE]`` / ``[TCD]``
#:   ``nsf``  Famicom Disk System 的曲子在另一个目录
#:
#: 只有**确实存在对应目录**的才登记（127 个目录里挑出来的）。没登记的
#: 机种（Switch / FM Towns / PC）不是漏了 —— libretro 那边没有这些目录，
#: 查了也是 404，不如直接说「没有」。
PLATFORM_DIRS = {
    # ---- 复古主机 ----
    "spc": ("Nintendo - Super Nintendo Entertainment System",),
    "nsf": ("Nintendo - Nintendo Entertainment System",
            "Nintendo - Family Computer Disk System"),
    "gbs": ("Nintendo - Game Boy",
            # ``gbs`` 子站同时收 GB 和 GBC（条目标 ``[GB]`` / ``[GBC]``），
            # libretro 却是两个目录。不并的话 GBC 独占游戏**一张都抓不到**
            # —— 实测 e2e 抓的第一条 ``TOCA ... [GBC]`` 就是这样失败的。
            "Nintendo - Game Boy Color"),
    "gsf": ("Nintendo - Game Boy Advance",),
    "smd": ("Sega - Mega Drive - Genesis",
            "Sega - Mega-CD - Sega CD"),
    "hes": ("NEC - PC Engine - TurboGrafx 16",
            "NEC - PC Engine CD - TurboGrafx-CD",
            "NEC - PC Engine SuperGrafx"),
    "usf": ("Nintendo - Nintendo 64",),
    "ssf": ("Sega - Saturn",),
    "dsf": ("Sega - Dreamcast",),
    "2sf": ("Nintendo - Nintendo DS",
            # DSi 独占（DSiWare）在另一个目录，条目不多但确实是分开的
            "Nintendo - Nintendo DSi"),
    "wsr": ("Bandai - WonderSwan",
            "Bandai - WonderSwan Color"),
    "kss_gg": ("Sega - Game Gear",),
    "kss_msx": ("Microsoft - MSX", "Microsoft - MSX2"),
    "kss_sms": ("Sega - Master System - Mark III",),
    "3do": ("The 3DO Company - 3DO",),
    # ---- 现代主机 ----
    "psf": ("Sony - PlayStation",),
    "psf2": ("Sony - PlayStation 2",),
    "psf3": ("Sony - PlayStation 3",
             "Sony - PlayStation 3 (Downloadable)"),
    "psf4": ("Sony - PlayStation 4",),
    "psp": ("Sony - PlayStation Portable",),
    "vita": ("Sony - PlayStation Vita",),
    "gcn": ("Nintendo - GameCube",),
    "wii": ("Nintendo - Wii",),
    "wiiu": ("Nintendo - Wii U",),
    "xbox": ("Microsoft - Xbox",),
    "x360": ("Microsoft - Xbox 360",),
    "3sf": ("Nintendo - Nintendo 3DS",
            "Nintendo - Nintendo 3DS (Digital)"),
    # ---- 计算机与街机 ----
    "s98": ("NEC - PC-98", "NEC - PC-88", "NEC - PC-8001 - PC-8801"),
    "hoot_arcade": ("MAME", "FBNeo - Arcade Games"),
    "hoot_qsf": ("MAME", "FBNeo - Arcade Games"),
}

#: 封面子目录的优先顺序。``Named_Boxarts`` 是盒绘（用户要的就是这个）；
#: 没有盒绘时退到标题画面/游戏截图，总比没有强。
KINDS = ("Named_Boxarts", "Named_Titles", "Named_Snaps")

#: 区域偏好。同一个游戏常常有 USA / Europe / Japan / World 好几张，
#: 优先给英文区（大多数用户的期望），日本独占游戏自然只剩 Japan。
REGION_ORDER = ("World", "USA", "Europe", "Japan", "Asia", "Korea", "Brazil",
                "Australia", "Canada", "France", "Germany", "Spain", "Italy",
                "China", "Taiwan")

#: 相似度低于这个值就放弃。0.86 是实测出来的：再低会把
#: ``Ys`` 匹到 ``Ys III``、``Contra`` 匹到 ``Contra Hard Corps``。
#:
#: ⚠ **它拦不住续作，别指望调它能解决问题**。相似度衡量的是字符重合比例，
#: 而续作只多一两个字符 —— 名字越长占比越小。实测 nsf 封面库 136 对
#: 「正传 + 续作」里 85% 的相似度 ≥ 0.86（最长的到 0.98）。
#: 真正管用的是 :func:`num_seq` 那条「数字序列必须一致」，两者配合使用。
#: 单独把阈值提到 0.96 能消掉 94% 的错配，代价是有效匹配掉一半（实测）。
MIN_RATIO = 0.86

#: 文件名里 Windows 不允许的字符。
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_REGION_RE = re.compile(r"\(([^()]+)\)\s*$")

_lock = threading.Lock()
_last_req = 0.0
#: 机种列表的内存缓存：key -> [文件名, ...]，避免同一批下载反复读盘。
_mem_index = {}
#: 归一化游戏名 -> 封面相对路径。转换时高频查询，进程内缓存一份。
_mem_lookup = None
_mem_lookup_mtime = 0.0


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

#: 外部设定的封面根目录（v6.0.11）。空 = 用旧位置 ``%LOCALAPPDATA%``。
#: 由 :func:`set_cache_root` 写入，下载页在切机种 / 改保存目录时调用。
_root_override = None

#: 设 override 那一刻的 ``LOCALAPPDATA``。见 :func:`cache_root` 的说明 ——
#: 环境被换过（门禁把 ``LOCALAPPDATA`` 指到临时目录做隔离）时 override 失效。
_root_env = None

#: 外部设定的**当前机种目录名**（``NES / FC`` 这种，已做文件名净化）。
#: 空 = 回落到 ``key``（``nsf``）。见 :func:`platform_sub`。
_plat_override = ""


def set_cache_root(root, platform=""):
    """把封面根目录指到别处（v6.0.11）。

    用户要求「封面放进下载音源目录里，单独一个 ``Artworks`` 文件夹，
    子目录是 ``机种\\游戏名``」。所以根目录不再是固定的
    ``%LOCALAPPDATA%\\MSU1Converter\\covers``，而是跟着保存目录走::

        <保存根>\\Artworks\\<机种>\\<游戏名>\\<图>.png

    ``root`` 传 ``""`` / ``None`` 表示**恢复默认**（旧位置）。非 GUI 场景
    （命令行转换、门禁里的离线测试）根本不会调它，行为与以前一致。

    ⚠ 换根必须把查找表的内存缓存清掉 —— ``_mem_lookup`` 是「按 mtime
    判断要不要重读」的，新位置的 ``_lookup.json`` 恰好和旧的 mtime 相同
    就会读到上一份数据（跨盘复制 mtime 不变很常见）。

    Args:
        root: 新的根目录绝对路径，或空串表示恢复默认。
        platform: 当前机种的目录名（已净化）。空 = 用 key。
    """
    global _root_override, _plat_override, _root_env
    global _mem_lookup, _mem_lookup_mtime
    new_root = os.path.normpath(str(root)) if root else None
    new_plat = str(platform or "").strip()
    if new_root == _root_override and new_plat == _plat_override:
        return                                  # 没变就别白清缓存
    _root_override = new_root
    _plat_override = new_plat
    _root_env = os.environ.get("LOCALAPPDATA") or ""
    _mem_lookup = None
    _mem_lookup_mtime = None


def _forget_lookup_cache():
    """把查找表的内存缓存清掉，逼下一次 :func:`_load_lookup` 重新读盘。

    什么时候必须调：**绕过 :func:`_save_lookup` 直接改写了 ``_lookup.json``
    之后**。``_load_lookup`` 是靠 mtime 判断「文件变没变」的，但内存里那份
    还可能被后续的 ``_remember`` / ``set_covers`` 当成基线整份写回去 ——
    那就把磁盘上的新内容冲掉了。迁移（:func:`migrate_legacy`）就是这种情形。
    """
    global _mem_lookup, _mem_lookup_mtime
    _mem_lookup = None
    _mem_lookup_mtime = None


def cache_root():
    """封面根目录。设过 :func:`set_cache_root` 就用它，否则用旧位置。

    ⚠ **override 只在 ``LOCALAPPDATA`` 没变时生效。** 门禁有一批用例靠
    ``os.environ["LOCALAPPDATA"] = tmp`` 把封面目录隔离到临时目录；
    如果 override 无条件优先，那些用例会往**用户真实的 Artworks 目录**
    写测试垃圾（实测写过 ``PSP/box.png`` 这种 3 字节的假图，而它排在最
    前面，直接让「嵌图 / 回填」的一串行为测试全红）。
    """
    if _root_override and (os.environ.get("LOCALAPPDATA") or "") == _root_env:
        return _root_override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "MSU1Converter", "covers")


def platform_sub(key):
    """某个机种在封面根下的子目录名。

    设过机种名就用它（``NES / FC`` → 落盘是 ``NES _ FC``，和下载目录同名），
    没设过回落到 ``key``（``nsf``）。**两条路径必须一致** —— 写图时用哪个、
    读查找表里的 rel 时就用哪个，混了就会「图在盘上但找不到」。

    ⚠ 和 :func:`cache_root` 一样，override 只在 ``LOCALAPPDATA`` 没变时生效，
    否则门禁的隔离用例会出现「根在临时目录、机种层却是真机种名」的错配。
    """
    plat = ""
    if _plat_override and (os.environ.get("LOCALAPPDATA") or "") == _root_env:
        plat = _plat_override
    if not plat:
        plat = str(key or "").strip().lower()
    return _safe_name(plat) if plat else ""


def legacy_cache_root():
    """v6.0.11 之前的固定位置（迁移时读它）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "MSU1Converter", "covers")


def rel_of(key, title, filename):
    """封面文件在封面根下的相对路径 —— 查找表里存的就是这个。

    v6.0.11 起是 ``<机种>/<游戏名>/<文件名>``。**写图的地方必须调它**，
    本模块和 :mod:`cover_igdb` 各写一遍的话，格式一错读侧
    :func:`covers_for_title` 就找不到盘上的图 —— 而且**不报错**，
    表现只是「没封面」。
    """
    return "%s/%s/%s" % (platform_sub(key), _safe_name(title),
                         _safe_name(filename))


#: 迁移完成的标记文件名（放在新根下）。有了它就不再重复扫旧目录。
MIGRATE_MARK = ".migrated-v6011"

#: 迁移串行化。多个入口（服务启动、切机种、改保存目录）可能同时触发，
#: 不加锁的话两个线程会各写一份 ``_lookup.json``，后写的把先写的冲掉
#: —— 图还在盘上但查找表少了一半，表现是「有的游戏突然没封面了」。
_migrate_lock = threading.Lock()


def migrate_legacy(new_root, name_of=None, log=None):
    """把 v6.0.11 之前存在 ``%LOCALAPPDATA%`` 的封面搬进新的 Artworks 结构。

    用户要求「自动搬到新位置」。做法是**复制**不是移动 —— 旧目录原样留着，
    新根在没插上的移动硬盘、或者用户反悔时图还在。

    目录名怎么定（唯一需要动脑的地方）：旧查找表只有**归一化后**的游戏名
    （``3 ninjas kick back``，全小写、非 ASCII 字符已被抹掉），直接拿它当
    目录名会把中文游戏名变成空串。所以从**文件名**反推 —— libretro 的文件名
    就是「游戏名 (区域).png」，:func:`_raw_title` 去掉括号后正好是原始大小写
    的标题。同一游戏若只有 IGDB 的图（``igdb_cover_xxx.png``），文件名推不
    出来，退回用归一化名。

    Args:
        new_root: 新的封面根（``<下载根>/Artworks``）。
        name_of: ``key -> 机种目录名`` 的回调（dl_api 传 PLATFORMS 的显示名）。
                 不传就用 key 本身。
        log: 单参数日志回调，可为 None。

    Returns:
        ``{"files": 复制张数, "games": 游戏数, "skipped": 跳过张数}``。
        已经迁过（标记文件在）或没有旧目录时返回全 0。
    """
    stats = {"files": 0, "games": 0, "skipped": 0}
    with _migrate_lock:                               # 并发入口串行化
        return _migrate_legacy_locked(new_root, name_of, log, stats)


def _migrate_legacy_locked(new_root, name_of, log, stats):
    """:func:`migrate_legacy` 的主体（调用方已持 :data:`_migrate_lock`）。"""
    old_root = legacy_cache_root()
    new_root = os.path.normpath(str(new_root or ""))
    if not new_root or not old_root:
        return stats
    if os.path.normpath(old_root) == new_root:
        return stats                                  # 同一个地方，没什么可搬
    if not os.path.isdir(old_root):
        return stats
    mark = os.path.join(new_root, MIGRATE_MARK)
    if os.path.isfile(mark):
        return stats                                  # 迁过了

    def _say(msg):
        if log is not None:
            try:
                log(msg)
            except Exception:                         # noqa: BLE001
                pass

    # 旧查找表：{归一化名: [(旧 rel, kind), ...]}，旧 rel 形如 "nsf/xxx.png"
    try:
        with open(os.path.join(old_root, "_lookup.json"), encoding="utf-8") as fh:
            blob = json.load(fh)
        old_lookup = ({k: _norm_items(v) for k, v in blob.items()}
                      if isinstance(blob, dict) else {})
    except (OSError, ValueError):
        old_lookup = {}

    # 旧 rel -> 归一化名（反查用：磁盘上有张图，它属于哪个游戏）
    rel_owner = {}
    for norm, items in old_lookup.items():
        for rel, _kind in items:
            rel_owner.setdefault(rel.replace("\\", "/"), norm)

    def _plat(key):
        if name_of is not None:
            try:
                got = name_of(key)
            except Exception:                         # noqa: BLE001
                got = ""
            if got:
                return _safe_name(got)
        return key

    def _game_dir(key, norm, filenames):
        """该游戏在新结构里的目录名：优先从非 IGDB 的文件名反推原始大小写。"""
        for fn in filenames:
            base = os.path.basename(fn)
            if base.startswith("igdb_"):
                continue
            raw = _raw_title(base)
            if raw:
                return _safe_name(raw)
        return _safe_name(norm) or "game"

    # 按 (机种 key, 归一化名) 分组，逐组复制
    groups = {}                                       # (key, norm) -> [文件名]
    for key_dir in sorted(os.listdir(old_root)):
        src_dir = os.path.join(old_root, key_dir)
        if not os.path.isdir(src_dir) or key_dir.startswith("_"):
            continue
        try:
            names = sorted(os.listdir(src_dir))
        except OSError:
            continue
        for fn in names:
            if not fn.lower().endswith((".png", ".jpg", ".jpeg")):
                continue
            if not os.path.isfile(os.path.join(src_dir, fn)):
                continue
            norm = rel_owner.get("%s/%s" % (key_dir, fn)) or norm_title(fn)
            if not norm:
                continue
            groups.setdefault((key_dir, norm), []).append(fn)

    # ``_idx`` 是「libretro 有哪些图」的目录清单缓存，和封面图无关但重拉很慢
    # （每个机种一次 HTTP）。顺手搬过去，不搬的话升级后第一次抓图会变慢。
    try:
        src_idx = os.path.join(old_root, "_idx")
        dst_idx = os.path.join(new_root, "_idx")
        if os.path.isdir(src_idx) and not os.path.isdir(dst_idx):
            shutil.copytree(src_idx, dst_idx)
    except OSError:
        pass

    new_lookup_path = os.path.join(new_root, "_lookup.json")
    rows_by_norm = {}                                 # 归一化名 -> [[rel, kind], ...]

    for (key, norm), files in groups.items():
        plat = _plat(key)
        game = _game_dir(key, norm, files)
        dst_dir = os.path.join(new_root, plat, game)
        if not _ensure_dir(dst_dir):
            continue
        rows = []
        for fn in files:
            src = os.path.join(old_root, key, fn)
            dst = os.path.join(dst_dir, fn)
            try:
                if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
                    stats["skipped"] += 1          # 已经在了，不重复覆盖
                else:
                    shutil.copy2(src, dst)
                    stats["files"] += 1
            except OSError:
                continue
            kind = ""
            for rel, k in old_lookup.get(norm) or ():
                if os.path.basename(rel.replace("\\", "/")) == fn:
                    kind = k
                    break
            rows.append(["%s/%s/%s" % (plat, game, fn), kind or "boxart"])
        if not rows:
            continue
        stats["games"] += 1
        rows_by_norm.setdefault(norm, []).extend(rows)

    if rows_by_norm:
        # ⚠ 这里必须和 ``_remember`` / ``set_covers`` 抢**同一把锁**（``_lock``）。
        #   ``_migrate_lock`` 只串行化迁移自身，挡不住「与此同时正在抓图、抓完
        #   写表」那条线程；两边各写一份完整文件，后写的把先写的整份冲掉。
        #   实测丢过一次：磁盘上 58 个游戏目录、查找表只剩 4 条，表现是
        #   「图都在但没封面」，且不报错。
        #   也不能拿函数开头读到的旧表当基线 —— 那份可能早就过期了，
        #   所以进锁之后重新读一次磁盘。
        with _lock:
            mapping = dict(_load_lookup())
            for norm, rows in rows_by_norm.items():
                old_rows = list(mapping.get(norm) or [])
                seen = {r for r, _k in old_rows}
                # 合并而不是覆盖：新根里可能已经有用户新抓的图
                mapping[norm] = old_rows + [r for r in rows if r[0] not in seen]
            try:
                os.makedirs(new_root, exist_ok=True)
                tmp = new_lookup_path + ".part"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(mapping, fh, ensure_ascii=False, sort_keys=True)
                os.replace(tmp, new_lookup_path)
                _forget_lookup_cache()                # 下次读盘拿最新的
            except OSError:
                pass
        _say("🖼 已把 %d 张旧封面搬到 %s（%d 个游戏，旧目录保留）"
             % (stats["files"], new_root, stats["games"]))

    # 无论有没有搬成，都打标记 —— 否则每次启动都重扫一遍旧目录
    try:
        os.makedirs(new_root, exist_ok=True)
        with open(mark, "w", encoding="utf-8") as fh:
            fh.write("%d files / %d games\n" % (stats["files"], stats["games"]))
    except OSError:
        pass
    return stats


def _idx_dir():
    return os.path.join(cache_root(), "_idx")


def _lookup_path():
    return os.path.join(cache_root(), "_lookup.json")


def _ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except OSError:
        return False


def platform_dirs(key):
    """机种 key -> 候选目录元组；没登记返回空元组。"""
    return PLATFORM_DIRS.get(str(key or "").strip().lower(), ())


def platform_dir(key):
    """机种 key -> **首选**目录名（界面显示用）；没登记返回空串。"""
    dirs = platform_dirs(key)
    return dirs[0] if dirs else ""


def supported(key):
    """这个机种有没有封面源。"""
    return bool(platform_dirs(key))


# ---------------------------------------------------------------------------
# 名字归一化与匹配
# ---------------------------------------------------------------------------

def norm_title(name):
    """游戏名归一化，用于跨库比对。

    规则（两边都用同一套，所以「砍到第一个括号」是对称的）::

        3 Ninjas Kick Back (1994-11)(Malibu)(Sony Imagesoft)[SNES]  ->  3 ninjas kick back
        3 Ninjas Kick Back (USA).png                                ->  3 ninjas kick back
        7th Saga, The [Elnard] (1993-04-23)(...)[SNES]              ->  7th saga the
        Sonic & Knuckles (World).png                                ->  sonic and knuckles
    """
    s = str(name or "")
    s = os.path.splitext(s)[0]
    # 去掉方括号里的别名/机种标记：[Elnard] [SNES] [T-En by ...]
    s = re.sub(r"\[[^\]]*\]", " ", s)
    # 砍到第一个左括号：后面的日期/厂商/区域一律不要
    s = s.split("(")[0]
    s = s.replace("&", " and ")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _region_of(filename):
    """从 libretro 文件名里取区域，取不到返回空串。

    ⚠ 必须**按逗号拆开**再认：libretro 的合法写法里有一堆多区合版 ——
    ``(Japan, USA)``、``(USA, Europe)``、``(World)``。整串比对的话
    ``Japan, USA`` 一个都认不出来，排名掉到「未知」，于是
    ``Super Metroid (Japan, USA)`` 会输给 ``Super Metroid (Europe) (En,Fr,De)``
    —— 实测就是这样，用户拿到一张纯欧版盒绘。
    拆开逐个认，取**排名最靠前**的那个（World > USA > …）。
    """
    stem = os.path.splitext(str(filename or ""))[0]
    hits = re.findall(r"\(([^()]+)\)", stem)
    best, best_rank = "", len(REGION_ORDER)
    for h in hits:
        for part in re.split(r"[,/]", h):
            p = part.strip()
            for r in REGION_ORDER:
                rank = REGION_ORDER.index(r)
                if rank < best_rank and p.lower() == r.lower():
                    best, best_rank = r, rank
    return best


def _region_rank(filename):
    r = _region_of(filename)
    if r in REGION_ORDER:
        return REGION_ORDER.index(r)
    return len(REGION_ORDER)


def _raw_title(name):
    """去掉括号元信息、保留原始大小写的标题部分（只用于判副标题）。"""
    s = str(name or "")
    s = os.path.splitext(s)[0]
    s = re.sub(r"\[[^\]]*\]", " ", s)
    return " ".join(s.split("(")[0].split())


#: 名字里的数字 / 罗马数字。**必须独立成词**才算 —— ``prg0`` 里那个 0 是
#: rom 修订号不是游戏编号，``1st`` / ``3d`` 也不是。
_NUM_TOKEN = re.compile(r"\b(\d+|xii|xi|ix|viii|vii|vi|iv|iii|ii|v|x)\b")

#: 罗马数字 -> 数值。比的是「第几代」，不是字面写法。
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6,
          "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}


def num_seq(name):
    """名字里的数字序列，罗马数字先转成数值。

    ``money game ii`` → ``[2]``、``money game 2`` → ``[2]`` —— 同一个游戏的
    两种写法，必须判成相同；``caltron 6 in 1`` → ``[6, 1]`` 和
    ``caltron 9 in 1`` → ``[9, 1]`` 是不同游戏，必须判成不同。

    为什么需要它（而不是只靠 :data:`MIN_RATIO`）：名字越长，「多一个 2」对
    字符相似度的影响越小。实测 nsf 封面库里 136 对「正传 + 续作」有 **85%
    的相似度 ≥ 0.86**，最长的那批到 0.98 —— 靠阈值永远拦不住，而它们恰恰
    是最常见的错配（``sonic the hedgehog`` → ``sonic the hedgehog 2``）。

    Args:
        name: 已归一化或未归一化的名字都行（内部自己 lower）。

    Returns:
        整数列表。没有数字就是空列表。
    """
    out = []
    for m in _NUM_TOKEN.finditer(str(name or "").lower()):
        tok = m.group(1)
        out.append(int(tok) if tok.isdigit() else _ROMAN.get(tok, 0))
    return out


def pick(items, title):
    """从列表里挑一张最合适的。挑不到返回 ``""``。

    ``items`` 是 :func:`list_covers` 的返回值（``[{"dir","name"}]``）。
    判据顺序：

    1. 归一化后**完全相等**；
    2. **副标题前缀** —— ``SimCity 2000`` ↔ ``SimCity 2000 - The Ultimate
       City Simulator``。这里必须要求分隔符是 `` - ``（libretro 用 `` - ``
       接副标题），否则 ``Contra`` 会匹到 ``Contra Hard Corps`` —— 那是
       两个不同的游戏，贴错封面比没有封面更糟；
    3. ``difflib`` 相似度 ≥ :data:`MIN_RATIO`，**并且数字序列一致**
       （见 :func:`num_seq`）—— 光靠阈值拦不住续作，见那里的说明。

    同等条件下按区域偏好（World > USA > Europe > Japan > 其它）。
    """
    want = norm_title(title)
    if not want or not items:
        return ""
    files = [it["name"] for it in items]
    # 归一化一次，别在循环里重复做（PS1 有 8000 条）
    keyed = [(f, norm_title(f)) for f in files]
    exact = [f for f, n in keyed if n == want]
    if exact:
        return sorted(exact, key=lambda f: (_region_rank(f), len(f), f))[0]

    # 副标题前缀：两个方向都试（joshw 带副标题 / libretro 带副标题）
    raw_want = _raw_title(title).lower()
    raw_want_nums = num_seq(raw_want)
    prefixed = []
    if len(raw_want) >= 4:
        for f in files:
            raw_f = _raw_title(f).lower()
            if not raw_f:
                continue
            if (raw_f.startswith(raw_want + " - ")
                    or raw_want.startswith(raw_f + " - ")):
                # 同一条「数字序列」判据。libretro 也用 `` - `` 接续作编号，
                # 于是 ``Family Quiz`` 会匹到 ``Family Quiz - 4-nin wa Rival``
                # —— 那是两款游戏，不是副标题。数字不同就否掉。
                if num_seq(raw_f) != raw_want_nums:
                    continue
                prefixed.append(f)
    if prefixed:
        return sorted(prefixed, key=lambda f: (_region_rank(f), len(f), f))[0]

    from difflib import SequenceMatcher
    want_nums = num_seq(want)
    best, best_score = "", 0.0
    for f, n in keyed:
        if not n:
            continue
        # 先用长度差粗筛：差一半以上的没必要算相似度
        if abs(len(n) - len(want)) > max(6, len(want) // 2):
            continue
        # 数字序列不一致 → 不是同一个游戏：续作（``sonic`` / ``sonic 2``）、
        # 不同卷（``family trainer`` / ``family trainer 5``）、合卡张数不同
        # （``caltron 6 in 1`` / ``caltron 9 in 1``）。
        # ⚠ 这条比阈值管用得多，别删。名字越长「多一个 2」对相似度的影响越小，
        #   实测 85% 的正传/续作对相似度都 ≥0.86，最长那批到 0.98 —— 调阈值
        #   到能拦住它们的地步，等于把整个兜底层关掉（实测有效匹配会掉一半）。
        #   反过来，错的和对的混在 0.87~0.98 同一区间（``caltron 6 in 1`` 是
        #   0.930、``castle quest``/``castlequest`` 是 0.947），一条横线切不开。
        if num_seq(n) != want_nums:
            continue
        score = SequenceMatcher(None, want, n).ratio()
        if score < MIN_RATIO:
            continue
        # 同分时让区域偏好和「更像」一起决定
        score -= _region_rank(f) * 0.001
        if score > best_score:
            best, best_score = f, score
    return best


def pick_all(items, title, limit=6):
    """从列表里挑**所有**匹配的，按展示优先级排序。挑不到返回 ``[]``。

    v6.0.7 加的：用户要求「一个游戏的封面资源应该有若干个，下载时全下载，
    试听时轮播」。同一个游戏在 libretro 里常常有好几张（``(USA)`` /
    ``(Europe)`` / ``(Japan)`` / ``(World)`` 各一张），全都要。

    ## 和 :func:`pick` 的唯一区别：模糊档只给一个

    前两档（**归一化全等** / **副标题前缀**）是「确定就是这个游戏」，返回
    全部 —— 多个地区版本正是我们想要的。

    第三档 ``difflib`` 是「猜的」。``pick`` 只取最高分那一个，猜错也就错
    一张；这里要是把 ≥0.86 的全返回，就会把 ``Ys III`` 也塞给 ``Ys``、
    把 ``Contra Hard Corps`` 塞给 ``Contra`` —— **轮播时用户会看到别的
    游戏的图**，比没有图糟得多。所以模糊档**只返回最优的一个**，
    与 :func:`pick` 完全一致。

    Returns:
        文件名列表（不含目录）。可能为空。
    """
    want = norm_title(title)
    if not want or not items:
        return []
    lim = max(1, int(limit or 6))
    files = [it["name"] for it in items]
    keyed = [(f, norm_title(f)) for f in files]

    def _order(fs):
        return sorted(fs, key=lambda f: (_region_rank(f), len(f), f))

    exact = [f for f, n in keyed if n == want]
    if exact:
        return _order(exact)[:lim]

    raw_want = _raw_title(title).lower()
    raw_want_nums = num_seq(raw_want)
    prefixed = []
    if len(raw_want) >= 4:
        for f in files:
            raw_f = _raw_title(f).lower()
            if not raw_f:
                continue
            if (raw_f.startswith(raw_want + " - ")
                    or raw_want.startswith(raw_f + " - ")):
                if num_seq(raw_f) != raw_want_nums:      # 理由见 pick()
                    continue
                prefixed.append(f)
    if prefixed:
        return _order(prefixed)[:lim]

    # 模糊档：只给最优的一个（理由见 docstring）
    best = pick(items, title)
    return [best] if best else []


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------

def _throttle():
    """全局限速。站点是公益镜像，别把它当 CDN 用。"""
    global _last_req
    with _lock:
        gap = time.time() - _last_req
        if gap < MIN_GAP:
            time.sleep(MIN_GAP - gap)
        _last_req = time.time()


def _get(url, limit, timeout=45):
    """取一个 URL，返回 ``(bytes, 错误说明)``。永不抛异常。"""
    _throttle()
    req = urllib.request.Request(url, headers={
        # 老实报自己是谁。抓的是公开镜像，不伪装浏览器。
        "User-Agent": "MSU1Converter/cover_art (+libretro-thumbnails)",
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(limit + 1)
            if len(data) > limit:
                return b"", "响应超过 %d 字节，放弃" % limit
            return data, ""
    except urllib.error.HTTPError as exc:
        return b"", "HTTP %d" % exc.code
    except urllib.error.URLError as exc:
        return b"", "连不上：%s" % (exc.reason,)
    except Exception as exc:                       # noqa: BLE001
        return b"", "%s: %s" % (type(exc).__name__, exc)


def _parse_listing(html):
    """从 Apache 目录索引里抠出所有 .png 文件名。"""
    text = html.decode("utf-8", "replace")
    out = []
    for m in re.finditer(r'href="([^"]+\.png)"', text):
        name = urllib.parse.unquote(m.group(1))
        if "/" in name or not name:
            continue                             # 跳过 ../ 之类
        out.append(name)
    return out


def list_covers(key, *, refresh=False, cancel=None):
    """某机种的全部封面。返回 ``[{"dir": 目录, "name": 文件名}, ...]``。

    一个机种可能对应多个 libretro 目录（见 :data:`PLATFORM_DIRS`），
    这里**合并**成一个列表。列表按机种缓存 30 天；``refresh=True`` 强制重抓。
    失败时返回 ``[]``（或过期缓存）。
    """
    key = str(key or "").strip().lower()
    dirs = platform_dirs(key)
    if not dirs:
        return []
    if not refresh and key in _mem_index:
        return _mem_index[key]

    path = os.path.join(_idx_dir(), "%s.json" % key)
    cached, cached_ts, cached_dirs = [], 0.0, ()
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        if isinstance(blob, dict) and isinstance(blob.get("items"), list):
            for pair in blob["items"]:
                if (isinstance(pair, list) and len(pair) == 2
                        and isinstance(pair[0], str) and isinstance(pair[1], str)):
                    cached.append({"dir": pair[0], "name": pair[1]})
            cached_ts = float(blob.get("ts") or 0.0)
            if isinstance(blob.get("dirs"), list):
                cached_dirs = tuple(blob["dirs"])
    except (OSError, ValueError, TypeError):
        pass

    # ⚠ 缓存里记着「当时扫的是哪几个目录」。和现在的映射表对不上就**当过期**。
    #
    #   这不是洁癖：v5.9.14 里 ``gbs`` 从「Game Boy」一个目录变成
    #   「Game Boy + Game Boy Color」两个，而旧缓存的 30 天还没到期 ——
    #   只比时间戳的话，升级上来的用户整整一个月都拿不到 GBC 的封面，
    #   而且现象极难归因（「为什么别人有我没有」）。实测就是这么发现的：
    #   命中率 45% 而不是预期的 60%+，因为 GBC 那 1332 条压根没进列表。
    if cached and cached_dirs and cached_dirs != tuple(dirs):
        cached, cached_ts = [], 0.0

    fresh = cached and (time.time() - cached_ts) < INDEX_TTL
    if fresh and not refresh:
        _mem_index[key] = cached
        return cached

    if cancel is not None and cancel.is_set():
        return cached

    items = []
    for d in dirs:
        if cancel is not None and cancel.is_set():
            break
        # 先试盒绘；空的话依次退到标题画面、截图
        for kind in KINDS:
            url = "%s/%s/%s/" % (BASE, urllib.parse.quote(d, safe=""), kind)
            data, _why = _get(url, MAX_LIST_BYTES)
            if not data:
                continue
            names = _parse_listing(data)
            if names:
                items.extend({"dir": d, "name": n} for n in names)
                break

    if not items:
        # 抓不到就用旧缓存（哪怕过期）—— 比没有强，而且不会因为一次
        # 网络抖动就让「已下载的封面全失效」
        _mem_index[key] = cached
        return cached

    _ensure_dir(_idx_dir())
    try:
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "key": key, "dirs": list(dirs),
                       "count": len(items),
                       "items": [[x["dir"], x["name"]] for x in items]}, fh,
                      ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass
    _mem_index[key] = items
    return items


# ---------------------------------------------------------------------------
# 查找表（转换时按游戏名反查）
# ---------------------------------------------------------------------------

#: 哪些 kind 算「盒绘」—— **只有这些能嵌进 FLAC / MP3**。
#: 用户 2026-10-28 明确要求：「因为涉及到嵌入 flac 和 mp3，在嵌入环节还是
#: 只嵌入 boxart」。轮播可以看到截图，但音频文件里的图必须是盒绘。
BOXART_KINDS = ("boxart", "cover")


def _norm_items(val):
    """查找表里的一条记录 -> ``[(相对路径, kind), ...]``。

    三种历史形态都要吃：

    * **v6.0.6 及以前**：单个字符串 ``"psp/xx.png"`` —— 那时候每个游戏只存
      一张，且要么是 libretro 的 ``Named_Boxarts``、要么是 IGDB 的 cover，
      **都是盒绘**，所以补 ``"boxart"``；
    * **v6.0.7 中间态**：``["psp/a.png", "psp/b.png"]`` —— 只有路径没 kind，
      同样按盒绘处理（那一版只收 boxart）；
    * **v6.0.7 定稿**：``[["psp/a.png", "boxart"], ["psp/s.png", "screenshot"]]``。

    这样升级上来的用户不用删缓存，旧图继续当盒绘用。
    """
    if isinstance(val, str):
        return [(val, "boxart")] if val else []
    if not isinstance(val, (list, tuple)):
        return []
    out = []
    for it in val:
        if isinstance(it, str):
            if it:
                out.append((it, "boxart"))
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            rel, kind = it[0], it[1]
            if isinstance(rel, str) and rel:
                out.append((rel, str(kind or "boxart")))
    return out


def _load_lookup():
    """读 ``_lookup.json``。文件变了才重读，转换时每首都会调。

    返回的映射**值一律是 ``[(rel, kind), ...]``**（见 :func:`_norm_items`），
    调用方不用再判类型。
    """
    global _mem_lookup, _mem_lookup_mtime
    path = _lookup_path()
    try:
        mt = os.path.getmtime(path)
    except OSError:
        _mem_lookup = _mem_lookup or {}
        return _mem_lookup
    if _mem_lookup is not None and mt == _mem_lookup_mtime:
        return _mem_lookup
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        _mem_lookup = ({k: _norm_items(v) for k, v in blob.items()}
                       if isinstance(blob, dict) else {})
    except (OSError, ValueError):
        _mem_lookup = _mem_lookup or {}
    _mem_lookup_mtime = mt
    return _mem_lookup


def _save_lookup(mapping):
    global _mem_lookup, _mem_lookup_mtime
    path = _lookup_path()
    _ensure_dir(cache_root())
    try:
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
        _mem_lookup = mapping
        _mem_lookup_mtime = os.path.getmtime(path)
    except OSError:
        pass


def _remember(title, items):
    """把「游戏名 -> 封面」写进查找表（幂等）。

    ``items`` 可以是单个字符串、``[rel, ...]``、或 ``[(rel, kind), ...]``。
    **追加而非覆盖** —— 同一个游戏先由 libretro 存了盒绘、再由 IGDB 补几张
    截图，两批都要留住。已经在表里的相对路径不重复加（kind 也不覆盖，
    先写进来的更可信）。
    """
    key = norm_title(title)
    if not key:
        return
    new = _norm_items(items)
    if not new:
        return
    with _lock:
        mapping = dict(_load_lookup())
        old = list(mapping.get(key) or [])
        seen = {r for r, _k in old}
        merged = old + [(r, k) for r, k in new if r not in seen]
        if merged == old:
            return
        mapping[key] = [[r, k] for r, k in merged]
        _save_lookup(mapping)


def set_covers(title, items):
    """用给定列表**替换**某个游戏的查找表条目（去重后回写用，v6.0.7）。

    为什么需要它：``_cover_many`` 会按文件内容去重（libretro 和 IGDB 常常
    给同一张图），但去重只发生在内存里 —— 查找表里那两条重复记录还在，
    于是 ``cover_paths()`` 读出来还是 7 张、前两张一模一样。

    把去重后的结果写回表里，读取侧就永远是干净的。**不删磁盘上的图**
    （可能被别处引用，而且删文件是不可逆操作），只是不再列出它。
    """
    key = norm_title(title)
    if not key:
        return
    items = _norm_items(items)
    with _lock:
        mapping = dict(_load_lookup())
        if not items:
            mapping.pop(key, None)
        else:
            mapping[key] = [[r, k] for r, k in items]
        _save_lookup(mapping)


def _dedup_by_size(cand):
    """``[(路径, kind, 字节数), ...]`` → 去掉**内容相同**的重复项，保留第一条。

    为什么要这一步：libretro 的图源本来就是 IGDB，同一个游戏两边会各存一份
    （``Amnesia.png`` 和 ``igdb_cover_co20ie.png`` 是同一张 247,077 B 的图）。
    写入侧（``dl_api._cover_many``）已经按内容去重并回写查找表了，但**读侧
    不能只依赖表是干净的** —— 表可能是老版本写的，或者被手工改过。这一层
    兜住之后，「轮播 N 张一定互不相同」就成了接口的硬保证。

    成本：先比**大小**（``os.stat``，本来就为了判存在做过一次），只有大小
    相同的才读文件算 md5。实测 6 张里通常只有 1 组同大小，多读 200 KB。
    """
    counts = {}
    for _p, _k, size in cand:
        counts[size] = counts.get(size, 0) + 1
    seen = set()
    out = []
    for p, kind, size in cand:
        if counts.get(size, 0) > 1:
            try:
                with open(p, "rb") as fh:
                    h = hashlib.md5(fh.read()).hexdigest()
            except OSError:
                out.append((p, kind))
                continue
            if h in seen:
                continue                       # 同一张图，前面已经收过
            seen.add(h)
        out.append((p, kind))
    return out


def covers_for_title(title, kinds=None):
    """按游戏名找**本地已有**的封面，返回 ``[(路径, kind), ...]``。

    纯离线查询，不会联网。顺序就是展示顺序：盒绘在前、截图在后
    （写入顺序由 :func:`fetch_all` 决定）。

    Args:
        kinds: 只要这些 kind（``None`` = 全部）。嵌音频时传
            :data:`BOXART_KINDS`。
    """
    key = norm_title(title)
    if not key:
        return []
    want = set(kinds) if kinds else None
    cand = []
    for rel, kind in _load_lookup().get(key) or ():
        if want is not None and kind not in want:
            continue
        p = os.path.join(cache_root(), str(rel).replace("/", os.sep))
        try:
            size = os.stat(p).st_size
        except OSError:
            continue                           # 表里有、磁盘上没有：跳过
        cand.append((p, kind, size))
    return _dedup_by_size(cand)


def cover_for_title(title):
    """按游戏名找**本地已有的盒绘**。没有返回空串。

    纯离线查询，转换时调用，不会联网。

    ⚠ **只返回盒绘，且只有一张** —— 这是给「嵌进 FLAC / MP3」用的。
    用户 2026-10-28 明确要求：「涉及到嵌入 flac 和 mp3，在嵌入环节还是
    只嵌入 boxart」。轮播要全部图请用 :func:`covers_for_title`。

    ⚠ **没有盒绘就返回空串，不拿截图凑**。以前会退到「随便哪一张」，
    但那样用户会看到 FLAC 里嵌着一张游戏截图 —— 正是他明确不要的。
    """
    got = covers_for_title(title, kinds=BOXART_KINDS)
    return got[0][0] if got else ""


# ---------------------------------------------------------------------------
# 抓一张
# ---------------------------------------------------------------------------

def _safe_name(name):
    """libretro 的文件名 -> Windows 能落盘的文件名。"""
    s = _BAD_CHARS.sub("_", str(name or "")).strip(" .")
    return s or "cover.png"


def fetch(key, title, *, cancel=None):
    """给某个游戏抓一张封面。返回 ``(ok, 本地路径, 说明)``。

    ``title`` 用 joshw 的条目名即可（归一化会去掉日期/厂商/机种标记）。
    已经抓过就直接返回本地路径，不重复下载。

    ⚠ v6.0.7 起这个函数只是 :func:`fetch_all` 的「取第一张盒绘」包装 ——
    两条路径必须共用一套判据，否则「转码时嵌的图」和「试听时显示的图」
    可能是两张不同的（用户会看到 FLAC 里的封面和界面上不一样）。
    """
    got = fetch_all(key, title, cancel=cancel, limit=1)
    if not got:
        # 失败原因要能说清：分「机种没源」「列表拿不到」「列表里没有」
        key = str(key or "").strip().lower()
        if not platform_dirs(key):
            return False, "", "机种 %s 没有封面源" % (key or "?")
        if not str(title or "").strip():
            return False, "", "没有游戏名"
        items = list_covers(key, cancel=cancel)
        if not items:
            return False, "", "拿不到 %s 的封面列表" % platform_dirs(key)[0]
        return False, "", "列表里没有匹配「%s」的封面（共 %d 条）" % (
            norm_title(title), len(items))
    path = got[0][0]
    return True, path, ("已有" if _is_old(path) else os.path.basename(path))


def _is_old(path):
    """给日志用：这张图是不是这次之前就有的（判据是 mtime 早于 10 秒前）。"""
    try:
        return (time.time() - os.path.getmtime(path)) > 10
    except OSError:
        return False


def fetch_all(key, title, *, cancel=None, limit=6):
    """给某个游戏抓**全部盒绘**（最多 ``limit`` 张）。返回 ``[(路径, kind)]``。

    v6.0.7：用户要求「一个游戏的封面资源应该有若干个，下载时全下载」。
    同一个游戏在 libretro 里常有多个地区版本（``(USA)`` / ``(Europe)`` /
    ``(Japan)``），由 :func:`pick_all` 一次全挑出来。

    kind 恒为 ``"boxart"`` —— 这个模块只碰 ``Named_Boxarts`` 那一层。
    标题画面 / 游戏截图由 :mod:`cover_igdb` 的 ``screenshots`` 补，
    kind 是 ``"screenshot"``，**不会**被嵌进音频（见 :data:`BOXART_KINDS`）。

    * **幂等**：本地已经够 ``limit`` 张就直接返回，不重复下载；
    * **失败不影响已成功的**：某张下不下来只跳过它，不推翻整批；
    * **顺序即展示顺序**：调用方拿到的列表可以直接拿去轮播。

    Returns:
        ``[(本地绝对路径, "boxart"), ...]``，可能为空或少于 ``limit``。
    """
    key = str(key or "").strip().lower()
    title = str(title or "").strip()
    lim = max(1, int(limit or 6))
    dirs = platform_dirs(key)
    if not dirs or not title:
        return []

    have = covers_for_title(title, kinds=BOXART_KINDS)
    if len(have) >= lim:
        return have[:lim]

    items = list_covers(key, cancel=cancel)
    if not items:
        return have[:lim]
    names = pick_all(items, title, lim)
    if not names:
        return have[:lim]

    # 文件名 -> 它在哪个 libretro 目录（混装机种一个机种对应多个目录）
    dir_of = {}
    for it in items:
        dir_of.setdefault(it["name"], it["dir"])

    # v6.0.11：目录结构从 ``<根>/<key>/<图>.png`` 变成
    # ``<根>/<机种>/<游戏名>/<图>.png`` —— 用户要求按「机种\游戏名」分层，
    # 这样在资源管理器里翻图时一眼知道是哪台机器上的哪个游戏。
    # ``rel`` 跟着变成三段，查找表里存的就是它，读侧不用再拼。
    plat = platform_sub(key)
    game = _safe_name(title)
    dest_dir = os.path.join(cache_root(), plat, game)
    if not _ensure_dir(dest_dir):
        return have[:lim]

    out = list(have)
    have_paths = {p for p, _k in have}
    fresh = []
    for name in names:
        if len(out) >= lim:
            break
        if cancel is not None and cancel.is_set():
            break
        local = _safe_name(name)
        dest = os.path.join(dest_dir, local)
        rel = rel_of(key, title, local)
        if dest in have_paths:
            continue
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            out.append((dest, "boxart"))
            have_paths.add(dest)
            continue

        pdir = dir_of.get(name) or dirs[0]
        data = b""
        for kind in KINDS:
            url = "%s/%s/%s/%s" % (BASE, urllib.parse.quote(pdir, safe=""),
                                   kind, urllib.parse.quote(name))
            data, _why = _get(url, MAX_IMG_BYTES)
            if data:
                break
        if not data:
            continue
        # 只认 PNG。站点偶尔对 404 也回 200 的 HTML，光看状态码会存坏文件。
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            continue
        try:
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, dest)
        except OSError:
            continue
        out.append((dest, "boxart"))
        have_paths.add(dest)
        fresh.append((rel, "boxart"))

    if fresh:
        _remember(title, fresh)
    return out[:lim]


# ---------------------------------------------------------------------------
# 给下载流程用的批量入口
# ---------------------------------------------------------------------------

def fetch_many(items, *, cancel=None, on_log=None):
    """一串 ``(机种 key, 条目名)`` -> ``{条目名: 本地路径}``。

    失败的条目**不进结果**，只在 ``on_log`` 里说一句 —— 封面是锦上添花，
    不能让它影响音源下载的成功率。
    """
    log = on_log or (lambda _t: None)
    out = {}
    for key, title in items:
        if cancel is not None and cancel.is_set():
            break
        ok, path, why = fetch(key, title, cancel=cancel)
        if ok:
            out[title] = path
            log("🖼 封面 %s：%s" % (title, os.path.basename(path)))
        else:
            log("🖼 封面 %s：%s" % (title, why))
    return out


def stats():
    """诊断用：缓存里有多少张封面、多少个机种、多少个游戏。

    v6.0.11 起结构是 ``<根>/<机种>/<游戏名>/<图>`` 三层，以前是两层
    （``<根>/<机种>/<图>``）。这里**两种都数** —— 迁移只复制不删除，
    旧位置还在的话 ``stats()`` 也可能被指过去看。
    """
    root = cache_root()
    n_img, n_plat, n_game = 0, 0, 0
    try:
        for d in sorted(os.listdir(root)):
            sub = os.path.join(root, d)
            if not os.path.isdir(sub) or d.startswith("_"):
                continue
            n_plat += 1
            try:
                names = os.listdir(sub)
            except OSError:
                continue
            for g in names:
                gd = os.path.join(sub, g)
                if not os.path.isdir(gd):
                    if g.lower().endswith(".png"):    # 旧的两层结构
                        n_img += 1
                    continue
                n_game += 1
                try:
                    n_img += sum(1 for f in os.listdir(gd)
                                 if f.lower().endswith(".png"))
                except OSError:
                    pass
    except OSError:
        pass
    return {"root": root, "images": n_img, "platforms": n_plat,
            "games": n_game, "sources": len(PLATFORM_DIRS)}
