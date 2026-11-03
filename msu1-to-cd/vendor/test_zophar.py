#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_zophar.py — Zophar's Domain 原始音源下载（第二家音源站）自检。

**全离线**：所有页面都用内嵌的 HTML 片段（结构照抄 2026-10-25 的真实页面），
网络访问一律打桩。所以这个文件在任何机器上都能跑，不受站点改版影响 ——
反过来说，**站点改版时这里不会红**，要靠 ``--live`` 那条人工抽查。

覆盖这几件容易错的事：

1. **分页** —— 一个机种 257 个游戏分 2 页，只抓第 1 页会少 57 个（静默少）。
2. **只认 ``(EMU)`` 包** —— 同一个游戏页有两个批量包，``(MP3)`` 那条是
   **有损录音**，拿它当音源等于把 59MB 的 MP3 转成 WAV 再叫「原声」。
   而且 ``(MP3)`` 那条 URL 带随机 token（``/ojossoys/``），``(EMU)`` 不带。
3. **条目 url 是「游戏页」而不是归档直链** —— 这是 zophar 与 joshw 的
   唯一实质差别。落盘名必须按**解析出来的** ``.zip`` 算，否则
   ``_file_name`` 会补 ``.7z``，界面所有行永远停在「等待…」。
4. **解析失败不拖累别人** —— 单个游戏没有原始音源时，其余照常下载。
5. **缓存 7 天** —— 过期/损坏要能自动失效，不能拿着坏 JSON 崩。

跑法：

    py -3.14 vendor/test_zophar.py          # 离线（默认，全绿才算过）
    py -3.14 vendor/test_zophar.py --live   # 额外跑一次真实联网抽查（可选）
"""

import json
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

_GREEN, _RED, _YEL, _RST = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    _GREEN = _RED = _YEL = _RST = ""

_pass = 0
_fail = 0
_skip = 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  {_GREEN}PASS{_RST} {name}" + (f"  {detail}" if detail else ""))
    else:
        _fail += 1
        print(f"  {_RED}FAIL{_RST} {name}  {detail}")
    return bool(cond)


def skip(name, why):
    global _skip
    _skip += 1
    print(f"  {_YEL}SKIP{_RST} {name}  {why}")


# ---------------------------------------------------------------------------
# 内嵌样本：结构照抄真实页面（行缩进/换行都保留，正则才不会「只在真页面里对」）
# ---------------------------------------------------------------------------

#: 机种页第 1 页。两个正常行 + 一个带 HTML 实体的行 + 一个无年份行。
PAGE1 = """<html><body>
<div id="gamelistpage">
<h2>Sega Game Gear Music</h2>
<p class="found_stats">Found 3 games with 30 tracks!</p>
<table id="gamelist">
<tr class="headerrow">
\t<td class="image">&nbsp;</td>
\t<td class="name">Name</td>
\t<td class="year">Year</td>
\t<td class="developer">Developer</td>
</tr>

\t<tr class="regularrow">
\t\t<td class="image">&nbsp;</td>
\t\t<td class="name"><a href="/music/sega-game-gear-sgc/aa-harimanada">Aa Harimanada</a></td>
\t\t<td class="year"><a href="/music/year/1993">1993</a></td>
\t\t<td class="developer"><a href="/music/developer/sega">Sega</a></td>
\t</tr>

\t<tr class="regularrow">
\t\t<td class="image">&nbsp;</td>
\t\t<td class="name"><a href="/music/sega-game-gear-sgc/adventures-of-batman-robin-the">Adventures of Batman &amp; Robin, The</a></td>
\t\t<td class="year"><a href="/music/year/1995">1995</a></td>
\t\t<td class="developer"><a href="/music/developer/foo">Foo</a></td>
\t</tr>

\t<tr class="regularrow">
\t\t<td class="image">&nbsp;</td>
\t\t<td class="name"><a href="/music/sega-game-gear-sgc/no-year-game">No Year Game</a></td>
\t\t<td class="year">&nbsp;</td>
\t\t<td class="developer">&nbsp;</td>
\t</tr>
</table>
<p class="pagenav">Page 1 of 2</p>
</div>
</body></html>"""

#: 机种页第 2 页（只有一条，且与第 1 页重复一条 —— 验证跨页去重）。
PAGE2 = """<html><body>
<div id="gamelistpage">
<table id="gamelist">
<tr class="regularrow">
\t<td class="image">&nbsp;</td>
\t<td class="name"><a href="/music/sega-game-gear-sgc/aa-harimanada">Aa Harimanada</a></td>
\t<td class="year"><a href="/music/year/1993">1993</a></td>
</tr>
<tr class="regularrow_image">
\t<td class="image"><img src="/images/box.png"></td>
\t<td class="name"><a href="/music/sega-game-gear-sgc/zoop">Zoop</a></td>
\t<td class="year"><a href="/music/year/1995">1995</a></td>
</tr>
</table>
<p class="pagenav">Page 2 of 2</p>
</div>
</body></html>"""

#: 游戏页：**同时**有 MP3 包和原始音源包 —— 这正是必须挑对的那一页。
GAME_OK = """<html><body>
<div id="gamepage">
\t<div id="mass_download">          
\t\t\t<a href="https://fi.zophar.net/soundfiles/sega-game-gear-sgc/aa-harimanada/ojossoys/Aa%20Harimanada%20%28MP3%29.zophar.zip">
\t\t\t<p>Download all files as MP3 (59 MB)</p>
\t\t</a>
\t\t\t<a href="https://fi.zophar.net/soundfiles/sega-game-gear-sgc/aa-harimanada/Aa%20Harimanada%20%28EMU%29.zophar.zip">
\t\t\t<p>Download original music files (12 KB)</p>
\t\t</a>
\t 
\t</div>
\t<table id="tracklist">
\t\t<tr class="regularrow trackrow">
\t\t\t<td class="number">1.</td>
\t\t\t<td class="name">BGM #01</td>
\t\t\t<td class="download"><a href="https://fi.zophar.net/soundfiles/sega-game-gear-sgc/aa-harimanada/01_BGM%20%2301.mp3">mp3</a></td>
\t\t</tr>
\t</table>
</div>
</body></html>"""

#: 游戏页：只有 MP3 录音（Amiga / Arcade / X68000 那些机种都是这样）。
GAME_MP3_ONLY = """<html><body>
<div id="mass_download">          
\t<a href="https://fi.zophar.net/soundfiles/amiga/1st-division-manager/xmxazldr/1st%20Division%20Manager%20%28Amiga%29.zophar.zip">
\t<p>Download all files as MP3 (7 MB)</p>
\t</a>
</div>
</body></html>"""

#: 游戏页：什么都没有（下架 / 空页面）。
GAME_EMPTY = """<html><body><div id="mass_download"></div></body></html>"""


# ---------------------------------------------------------------------------
# 桩：把所有网络访问换成本地页面
# ---------------------------------------------------------------------------

class _FakeSite:
    """把 URL → 页面内容 的映射做成一个假站点，并记录被请求过哪些 URL。"""

    def __init__(self, pages, fail=()):
        self.pages = dict(pages)
        self.fail = set(fail)
        self.hits = []

    def __call__(self, url, timeout=None):
        self.hits.append(url)
        if url in self.fail:
            raise OSError("模拟网络错误: %s" % url)
        if url not in self.pages:
            raise OSError("模拟 404: %s" % url)
        return self.pages[url]


def _patch(site):
    """把 zophar_source 的 HTTP 出口换成桩，返回还原函数。"""
    import zophar_source as zs
    original = zs.http_get_text
    zs.http_get_text = site
    return lambda: setattr(zs, "http_get_text", original)


# ---------------------------------------------------------------------------
# 1. 常量 / 机种表
# ---------------------------------------------------------------------------

def t_platforms():
    import zophar_source as zs

    print("=== 1. 机种表与常量")
    keys = [p["key"] for p in zs.PLATFORMS]
    check("只收录 Game Gear / Master System（其余新机种没有可解的原始音源）",
          keys == ["z_gg", "z_sms"], keys)
    check("每个机种都带 src=zophar（下载层靠它分派）",
          all(p.get("src") == zs.PROVIDER for p in zs.PLATFORMS))
    check("PROVIDER 常量是 zophar", zs.PROVIDER == "zophar", zs.PROVIDER)
    check("is_key 认 z_gg / 不认 spc",
          zs.is_key("z_gg") and not zs.is_key("spc"))
    check("platform('z_sms') 的 slug 是 sega-master-system-vgm",
          zs.platform("z_sms")["slug"] == "sega-master-system-vgm")
    try:
        zs.platform("nope")
        check("未知 key 抛 KeyError", False)
    except KeyError:
        check("未知 key 抛 KeyError", True)

    check("system_url 第 1 页不带 ?page",
          zs.system_url(zs.platform("z_gg")) ==
          "https://www.zophar.net/music/sega-game-gear-sgc")
    check("system_url 第 2 页带 ?page=2",
          zs.system_url(zs.platform("z_gg"), 2) ==
          "https://www.zophar.net/music/sega-game-gear-sgc?page=2")
    check("game_url 拼得对",
          zs.game_url(zs.platform("z_gg"), "zoop") ==
          "https://www.zophar.net/music/sega-game-gear-sgc/zoop")

    check("human_size(0) == '0 B'", zs.human_size(0) == "0 B")
    check("human_size(1536) == '1.5 KB'", zs.human_size(1536) == "1.5 KB")
    check("human_size(None) 不炸", zs.human_size(None) == "0 B")


# ---------------------------------------------------------------------------
# 2. 页面解析
# ---------------------------------------------------------------------------

def t_parse():
    import zophar_source as zs
    pf = zs.platform("z_gg")

    print("=== 2. 机种页解析（分页 / 实体 / 年份 / 去重）")
    check("page_count 从 'Page 1 of 2' 读到 2", zs.page_count(PAGE1) == 2,
          zs.page_count(PAGE1))
    check("page_count 读不到就按 1 页算", zs.page_count("<html></html>") == 1)
    check("found_count 读到 3", zs.found_count(PAGE1) == 3,
          zs.found_count(PAGE1))

    items = zs.parse_games(PAGE1, pf)
    check("第 1 页解析出 3 条", len(items) == 3, len(items))
    check("名字原样保留", items[0]["name"] == "Aa Harimanada", items[0]["name"])
    check("HTML 实体被还原（&amp; → &）",
          items[1]["name"] == "Adventures of Batman & Robin, The",
          items[1]["name"])
    check("url 是**游戏页**绝对地址（不是归档）",
          items[0]["url"] ==
          "https://www.zophar.net/music/sega-game-gear-sgc/aa-harimanada",
          items[0]["url"])
    check("年份进 mtime", items[0]["mtime"] == "1993", items[0]["mtime"])
    check("没有年份时 mtime 是空串", items[2]["mtime"] == "", items[2]["mtime"])
    check("letter 是名字首字母大写", items[0]["letter"] == "A", items[0]["letter"])
    check("条目带 src=zophar", all(i["src"] == "zophar" for i in items))
    check("列表页拿不到体积 → size 恒为 0",
          all(i["size"] == 0 for i in items))
    check("契约字段齐（name/url/size/mtime/letter）",
          all(set(i) >= {"name", "url", "size", "mtime", "letter"} for i in items))

    items2 = zs.parse_games(PAGE2, pf)
    check("第 2 页解析出 2 条", len(items2) == 2, len(items2))
    check("regularrow_image 那种行也能解析（Zoop）",
          items2[1]["name"] == "Zoop", items2[1]["name"])

    # 表格外的链接不能混进来
    noise = ('<html><body><div><a href="/music/sega-game-gear-sgc/fake">Fake</a>'
             '</div>' + PAGE1 + '</body></html>')
    check("只认 <table id=gamelist> 里的行（表格外的链接不算）",
          len(zs.parse_games(noise, pf)) == 3, len(zs.parse_games(noise, pf)))
    check("空页面返回空列表（不抛）", zs.parse_games("", pf) == [])


def t_parse_downloads():
    import zophar_source as zs

    print("=== 3. 游戏页解析（必须挑 (EMU)，不能挑 (MP3)）")
    items = zs.parse_downloads(GAME_OK)
    kinds = {i["kind"] for i in items}
    check("两个批量包都识别出来", len(items) == 2, len(items))
    check("分得出 mp3 与 emu", kinds == {"mp3", "emu"}, kinds)
    emu = [i for i in items if i["kind"] == "emu"][0]
    mp3 = [i for i in items if i["kind"] == "mp3"][0]
    check("(EMU) 那条没有随机 token（/aa-harimanada/Aa%20...）",
          "/ojossoys/" not in emu["url"] and "/aa-harimanada/" in emu["url"],
          emu["url"][-70:])
    check("(MP3) 那条带随机 token（这条 URL 会变，不能存）",
          "/ojossoys/" in mp3["url"], mp3["url"][-70:])
    check("体积从括号里解析：12 KB → 12288", emu["size"] == 12 * 1024, emu["size"])
    check("体积从括号里解析：59 MB → 59 MiB",
          mp3["size"] == 59 * 1024 * 1024, mp3["size"])
    check("tracklist 里的单曲 mp3 不算批量包（只解析 #mass_download）",
          all(not i["url"].lower().endswith(".mp3") for i in items)
          and len(items) == 2, [i["url"][-30:] for i in items])

    only_mp3 = zs.parse_downloads(GAME_MP3_ONLY)
    check("只有 MP3 的页面 → 只有 1 条且 kind=mp3",
          len(only_mp3) == 1 and only_mp3[0]["kind"] == "mp3", only_mp3)
    check("空页面 → 空列表", zs.parse_downloads(GAME_EMPTY) == [])


# ---------------------------------------------------------------------------
# 4. resolve / resolve_many
# ---------------------------------------------------------------------------

def t_resolve():
    import zophar_source as zs

    print("=== 4. 游戏页 → 原始音源直链")
    game = "https://www.zophar.net/music/sega-game-gear-sgc/aa-harimanada"
    site = _FakeSite({game: GAME_OK})
    restore = _patch(site)
    try:
        r = zs.resolve(game)
        check("resolve 成功", r.get("ok") is True, r)
        check("拿到的是 (EMU) 那条", "%28EMU%29" in r["url"], r["url"][-60:])
        check("不是 (MP3) 那条", "%28MP3%29" not in r["url"])
        check("体积跟着过来（12288）", r["size"] == 12288, r["size"])

        # 只有 MP3 的游戏：必须明确拒绝，不能退而求其次拿 MP3
        g2 = "https://www.zophar.net/music/amiga/1st-division-manager"
        site.pages[g2] = GAME_MP3_ONLY
        try:
            zs.resolve(g2)
            check("只有 MP3 时 resolve 抛错（绝不拿 MP3 顶替）", False)
        except RuntimeError as exc:
            check("只有 MP3 时 resolve 抛错（绝不拿 MP3 顶替）",
                  "MP3" in str(exc), str(exc))

        g3 = "https://www.zophar.net/music/amiga/empty"
        site.pages[g3] = GAME_EMPTY
        try:
            zs.resolve(g3)
            check("空游戏页 resolve 抛错", False)
        except RuntimeError as exc:
            check("空游戏页 resolve 抛错", True, str(exc))

        try:
            zs.resolve("https://www.zophar.net/music/amiga/gone")
            check("404 页面 resolve 抛错", False)
        except RuntimeError as exc:
            check("404 页面 resolve 抛错", True, str(exc)[:60])

        # 批量：一个成功、一个只有 MP3、一个 404 —— 互不影响
        pages = [game, g2, "https://www.zophar.net/music/amiga/gone"]
        got = zs.resolve_many(pages)
        check("resolve_many 三个都返回了（不因个别失败中断）",
              len(got) == 3, len(got))
        check("成功的 ok=True", got[game].get("ok") is True)
        check("只有 MP3 的 ok=False 且带原因",
              got[g2].get("ok") is False and "MP3" in str(got[g2].get("error")),
              got[g2].get("error"))
        check("404 的 ok=False",
              got["https://www.zophar.net/music/amiga/gone"].get("ok") is False)

        # 取消：置位后立刻返回，不再抓页面
        before = len(site.hits)
        ev = threading.Event()
        ev.set()
        got2 = zs.resolve_many([game, g2], cancel=ev)
        check("已取消时不发请求", len(site.hits) == before, len(site.hits))
        check("已取消时结果标 ok=False",
              all(v.get("ok") is False for v in got2.values()), got2)
    finally:
        restore()


# ---------------------------------------------------------------------------
# 5. scan_platform（分页必须走完）
# ---------------------------------------------------------------------------

def t_scan():
    import zophar_source as zs

    print("=== 5. 扫描（分页走完 + 去重 + 写缓存）")
    p1 = "https://www.zophar.net/music/sega-game-gear-sgc"
    p2 = p1 + "?page=2"
    site = _FakeSite({p1: PAGE1, p2: PAGE2})
    restore = _patch(site)
    tmp = tempfile.mkdtemp(prefix="zop_cache_")
    old_dir = zs.cache_dir
    zs.cache_dir = lambda: tmp
    try:
        seen = []
        items = zs.scan_platform("z_gg",
                                 progress=lambda d, t, m: seen.append((d, t, m)))
        check("两页都抓了", len(site.hits) == 2, site.hits)
        check("共 4 条（3 + 2 - 1 条跨页重复）", len(items) == 4, len(items))
        urls = [i["url"] for i in items]
        check("跨页重复的 url 去掉了", len(urls) == len(set(urls)), urls)
        check("按名字排序（Aa < Adventures < No Year < Zoop）",
              [i["name"][:3] for i in items] == ["Aa ", "Adv", "No ", "Zoo"],
              [i["name"] for i in items])
        check("进度回调被调用过", bool(seen), seen[-1:])
        check("最后一页的 total 是 2", seen[-1][1] == 2, seen[-1])

        # 缓存落地 + 可读回
        check("缓存文件写出来了", os.path.isfile(zs.cache_file("z_gg")))
        back = zs.load_cache("z_gg")
        check("缓存能读回同样 4 条", back is not None and len(back[0]) == 4,
              len(back[0]) if back else None)
        check("缓存里 src 字段还在",
              all(i.get("src") == "zophar" for i in back[0]))

        # best_index：新鲜缓存走 cache
        ents, stamp, src = zs.best_index("z_gg")
        check("best_index 走 cache", src == "cache" and len(ents) == 4, src)

        # 第 2 页抓不到时，第 1 页的结果仍然要返回（不整段失败）
        site2 = _FakeSite({p1: PAGE1}, fail=[p2])
        restore2 = _patch(site2)
        try:
            partial = zs.scan_platform("z_gg")
            check("第 2 页失败时返回第 1 页的 3 条（不整段失败）",
                  len(partial) == 3, len(partial))
        finally:
            restore2()

        # 第 1 页就抓不到 → 必须抛，不能假装「这个机种是空的」
        site3 = _FakeSite({}, fail=[p1])
        restore3 = _patch(site3)
        try:
            zs.scan_platform("z_gg")
            check("首页失败要抛 RuntimeError（不能假装空机种）", False)
        except RuntimeError as exc:
            check("首页失败要抛 RuntimeError（不能假装空机种）", True,
                  str(exc)[:60])
        finally:
            restore3()
    finally:
        restore()
        zs.cache_dir = old_dir


# ---------------------------------------------------------------------------
# 6. 缓存健壮性
# ---------------------------------------------------------------------------

def t_cache():
    import zophar_source as zs

    print("=== 6. 缓存（7 天 TTL / 坏文件 / 清理）")
    tmp = tempfile.mkdtemp(prefix="zop_cache2_")
    old_dir = zs.cache_dir
    zs.cache_dir = lambda: tmp
    try:
        good = [{"name": "X", "url": "https://a/1", "size": 0,
                 "mtime": "1999", "letter": "X", "src": "zophar"}]
        zs.save_cache("z_gg", good)
        check("写入后能读回", zs.load_cache("z_gg") is not None)

        path = zs.cache_file("z_gg")
        payload = json.load(open(path, encoding="utf-8"))
        payload["ts"] = time.time() - 8 * 86400          # 8 天前
        json.dump(payload, open(path, "w", encoding="utf-8"))
        check("超过 7 天自动失效", zs.load_cache("z_gg") is None)
        check("ttl_days=0 时忽略时间", zs.load_cache("z_gg", 0) is not None)

        open(path, "w", encoding="utf-8").write("{ 这不是 JSON")
        check("坏 JSON 返回 None（不抛）", zs.load_cache("z_gg") is None)

        json.dump({"ts": time.time(), "entries": [{"name": "只有名字"}]},
                  open(path, "w", encoding="utf-8"))
        check("条目缺字段整份作废", zs.load_cache("z_gg") is None)

        zs.save_cache("z_gg", good)
        zs.clear_cache("z_gg")
        check("clear_cache(key) 删掉该机种", zs.load_cache("z_gg") is None)
        zs.save_cache("z_gg", good)
        zs.save_cache("z_sms", good)
        zs.clear_cache(None)
        check("clear_cache(None) 全清",
              zs.load_cache("z_gg") is None and zs.load_cache("z_sms") is None)

        try:
            zs.cache_file("../etc/passwd")
            check("非法 key 抛 ValueError（防路径穿越）", False)
        except ValueError:
            check("非法 key 抛 ValueError（防路径穿越）", True)

        check("best_index 无缓存时 source=none",
              zs.best_index("z_gg")[2] == "none")
    finally:
        zs.cache_dir = old_dir


# ---------------------------------------------------------------------------
# 7. 与下载层的接线（这是最容易「看起来对、其实断线」的一段）
# ---------------------------------------------------------------------------

def t_wiring():
    import dl_api as A

    print("=== 7. 与下载页接线")
    if A.zs is None:
        skip("下载页接线", "zophar_source 没导进来")
        return

    st = A.DownloadState()
    groups, err = st.platforms()
    check("机种树能取到", not err and bool(groups), err)
    names = [i["name"] for g in groups for i in g["items"]]
    check("Game Gear 出现在机种树里", "Game Gear" in names, names[-4:])
    check("Master System 出现在机种树里", "Master System" in names)
    zg = [g for g in groups if g["group"] == "Zophar 原声"]
    check("单独成组「Zophar 原声」（不混进复古主机）", len(zg) == 1,
          [g["group"] for g in groups])
    check("该组正好 2 个机种", len(zg[0]["items"]) == 2 if zg else False)
    check("每项都带 src（前端/分派都要用）",
          all("src" in i for g in groups for i in g["items"]))

    check("_src_of(z_gg) == zophar", st._src_of("z_gg") == "zophar")
    check("_src_of(spc) == joshw", st._src_of("spc") == "joshw")
    check("_src_of(乱填) 落到 joshw（不炸）", st._src_of("乱填") == "joshw")

    # ⚠ 这一段是「界面所有行永远停在等待…」那个 bug 的锁
    zurl = ("https://fi.zophar.net/soundfiles/sega-game-gear-sgc/aa-harimanada/"
            "Aa%20Harimanada%20%28EMU%29.zophar.zip")
    name = st._file_name("Aa Harimanada", zurl)
    check("落盘名按解析出来的 .zip 算（不是 .7z）",
          name == "Aa Harimanada.zip", name)
    check("用游戏页 URL 算会得到 .7z（所以必须用解析后的 URL）",
          st._file_name("Aa Harimanada",
                        "https://www.zophar.net/music/sega-game-gear-sgc/aa-harimanada")
          .endswith(".7z"),
          st._file_name("Aa Harimanada",
                        "https://www.zophar.net/music/sega-game-gear-sgc/aa-harimanada"))

    # _url_for 必须能把落盘名映射回**游戏页** url（状态回写靠它）
    page = "https://www.zophar.net/music/sega-game-gear-sgc/aa-harimanada"
    st.entries = [{"name": "Aa Harimanada", "url": page, "size": 0,
                   "mtime": "1993", "letter": "A", "src": "zophar"}]
    st._zip_of = {page: zurl}
    check("_url_for 用 _zip_of 把 <游戏>.zip 映射回游戏页 url",
          st._url_for(os.path.join("D:\\x", name)) == page,
          st._url_for(os.path.join("D:\\x", name)))
    st._zip_of = {}
    check("没有 _zip_of 时映射不到（说明确实依赖它）",
          st._url_for(os.path.join("D:\\x", name)) is None)

    # 默认保存目录要分站
    d1 = st._default_dest("Game Gear", "zophar")
    d2 = st._default_dest("SNES / SFC", "joshw")
    check("zophar 的默认目录落在 .../zophar/ 下",
          os.sep + "zophar" + os.sep in d1, d1)
    check("joshw 的默认目录仍落在 .../joshw/ 下",
          os.sep + "joshw" + os.sep in d2, d2)
    check("两家目录不重合", d1 != d2)

    d = st.diag()
    check("diag 报出 zophar_source 可用", d.get("zophar_source") is True)
    check("diag 的机种数把两家加起来（30 + 2 = 32）",
          d.get("platforms") == 32, d.get("platforms"))


# ---------------------------------------------------------------------------
# 8. 可选：真实联网抽查（--live）
# ---------------------------------------------------------------------------

def t_live():
    import zophar_source as zs

    print("=== 8. 真实联网抽查（--live）")
    try:
        items = zs.scan_platform("z_gg")
    except BaseException as exc:                     # noqa: BLE001
        skip("真实扫描 Game Gear", "网络不可用: %s" % str(exc)[:60])
        return
    check("真实扫描拿到 >= 200 个游戏", len(items) >= 200, len(items))
    check("条目 url 都是游戏页",
          all("/music/sega-game-gear-sgc/" in i["url"] for i in items[:5]))
    sample = [i for i in items if i["name"] == "Zoop"] or items[:1]
    r = zs.resolve(sample[0]["url"])
    check("真实解析拿到 (EMU) 直链",
          r.get("ok") and "%28EMU%29" in r["url"], r.get("url", "")[-60:])
    check("真实体积 > 0", int(r.get("size") or 0) > 0, r.get("size"))


def main(argv):
    t_platforms()
    t_parse()
    t_parse_downloads()
    t_resolve()
    t_scan()
    t_cache()
    t_wiring()
    if "--live" in argv:
        t_live()
    else:
        skip("真实联网抽查", "加 --live 才跑（需要网络）")

    print()
    print("结果: %s%d 通过%s / %s%d 失败%s / %d 跳过"
          % (_GREEN, _pass, _RST, _RED if _fail else _GREEN, _fail, _RST, _skip))
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
