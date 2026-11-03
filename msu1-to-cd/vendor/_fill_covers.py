"""给**已经下载好的** joshw 库补封面，并回填到已有的 FLAC。

为什么需要这个脚本
------------------
v5.9.14 的封面只在**下载时**抓（``dl_api.start`` -> ``_cover_worker``），
而转码时的 ``cover_for_title`` 是**纯离线查询**，只认本地已经下载过的图。
于是这两种情况必然没有封面：

* 包是 v5.9.14 之前下载的（那时还没有封面功能）；
* 当时关掉了「下载封面」，或者转码时压根没走下载流程（直接点「转 FLAC」）。

2026-10-27 用户报的就是这个：``Advance Wars 2`` 的包 10/23 下的，
今天只点了「转 FLAC」，47 个 .flac 一个图片块都没有。

用法
----
::

    python vendor/_fill_covers.py --root=D:/MSU1Downloads/joshw --dry
    python vendor/_fill_covers.py --root=D:/MSU1Downloads/joshw
    python vendor/_fill_covers.py --root=D:/MSU1Downloads/joshw --only=gsf,nsf

``--dry`` 只报告「能命中多少」，不下载、不写盘。建议先干跑一遍。

⚠ 每换一个机种，第一次要联网拉该机种的封面清单（几 MB，libretro 公益
  镜像，内部限速 ``MIN_GAP``）。已缓存的机种（``_idx/*.json``）不重复拉。
"""

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, os.path.join(_ROOT, "web_ui")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cover_art                                    # noqa: E402
import flac_out                                     # noqa: E402
import joshw_source                                 # noqa: E402
import spc_wav                                      # noqa: E402
from web_ui.dl_api import _safe_name                # noqa: E402

#: 音频产物目录名 —— 它们和「游戏包目录」同级，扫包时要跳过。
_OUT_TAGS = ("WAV", "FLAC", "MP3")


def _key_map():
    """joshw 落盘目录名 -> 机种 key。

    落盘目录是 ``_safe_name(PLATFORMS[i]["name"])``（斜杠换成 ``_``）。
    另外补一条**首段**索引：老版本的目录是 ``MSX``，而现在的名字是
    ``MSX / MSX2`` -> ``MSX _ MSX2``，精确比对会漏掉历史目录。
    """
    exact, head = {}, {}
    for item in joshw_source.PLATFORMS:
        name = _safe_name(item["name"])
        exact[name] = item["key"]
        head.setdefault(name.split(" _ ")[0].strip(), item["key"])
    return exact, head


def _games(root, plat_dir):
    """某机种下的游戏包目录名（跳过 WAV/FLAC/MP3 和隐藏项）。"""
    out = []
    try:
        for n in sorted(os.listdir(os.path.join(root, plat_dir))):
            if n.upper() in _OUT_TAGS or n.startswith("."):
                continue
            if os.path.isdir(os.path.join(root, plat_dir, n)):
                out.append(n)
    except OSError:
        pass
    return out


def _audio(root, plat_dir, tag, game):
    """某个游戏在 ``WAV`` / ``FLAC`` / ``MP3`` 目录下的音频文件。"""
    d = os.path.join(root, plat_dir, tag, game)
    if not os.path.isdir(d):
        return []
    exts = {"FLAC": (".flac",), "MP3": (".mp3",)}.get(tag, (".wav",))
    return [os.path.join(d, f) for f in sorted(os.listdir(d))
            if f.lower().endswith(exts)]


def _flacs(root, plat_dir, game):
    """这个游戏**能嵌封面**的产物：FLAC + MP3（WAV 没容器放图）。"""
    return (_audio(root, plat_dir, "FLAC", game)
            + _audio(root, plat_dir, "MP3", game))


def _has_pic(path):
    try:
        return bool(flac_out.has_picture(path))
    except Exception:                               # noqa: BLE001
        return False


def _embed(path, img):
    """按容器选写法：FLAC 走 METADATA_BLOCK_PICTURE，MP3 走 ID3 APIC。"""
    if path.lower().endswith(".mp3"):
        return flac_out.embed_id3_picture(path, img)
    return flac_out.embed_picture(path, img)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"D:/MSU1Downloads/joshw")
    ap.add_argument("--only", default="",
                    help="只处理这些机种 key，逗号分隔（如 gsf,nsf）")
    ap.add_argument("--dry", action="store_true", help="只报告，不下载不写盘")
    ap.add_argument("--limit", type=int, default=0, help="每个机种最多几个（调试）")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print("库根不存在：%s" % root)
        return 2
    only = {x.strip().lower() for x in args.only.split(",") if x.strip()}

    exact, head = _key_map()
    stat = {"game": 0, "hit": 0, "miss": 0, "skip": 0, "png": 0,
            "flac": 0, "flac_ok": 0, "flac_bad": 0, "no_src": 0}
    t0 = time.time()

    for plat_dir in sorted(os.listdir(root)):
        if not os.path.isdir(os.path.join(root, plat_dir)) \
                or plat_dir.upper() in _OUT_TAGS:
            continue
        key = exact.get(plat_dir) or head.get(plat_dir) or ""
        if not key:
            print("[skip] %-22s 不是机种目录" % plat_dir)
            continue
        if only and key not in only:
            continue
        if not cover_art.supported(key):
            print("[skip] %-22s key=%s 没有封面源" % (plat_dir, key))
            stat["no_src"] += 1
            continue

        games = _games(root, plat_dir)
        if args.limit:
            games = games[:args.limit]
        print("\n== %s (key=%s) %d 个包 ==" % (plat_dir, key, len(games)))

        for g in games:
            stat["game"] += 1
            flacs = _flacs(root, plat_dir, g)
            # 已经有封面就不用再抓（除非要回填那些漏的）
            if args.dry:
                # 干跑：只查清单能不能匹上，不下载
                items = cover_art.list_covers(key)
                name = cover_art.pick(items, g)
                if name:
                    stat["hit"] += 1
                    mark = "命中 " + name
                else:
                    stat["miss"] += 1
                    mark = "未命中"
                n_pic = sum(1 for p in flacs if _has_pic(p))
                print("  %-52s %s | FLAC %d 个（%d 带图）"
                      % (g[:52], mark, len(flacs), n_pic))
                continue

            ok, path, why = cover_art.fetch(key, g)
            if ok:
                stat["hit"] += 1
            else:
                stat["miss"] += 1
                print("  ✗ %-50s %s" % (g[:50], why))
                continue
            # v5.9.16：图也放一份到 ``FLAC/<游戏名>/cover.png``（用户要的
            # 可见位置，拷走目录也跟着走）。目录不存在（还没转 FLAC）就跳过。
            # v5.9.17：``MP3/<游戏名>/`` 同样落一份 —— 两种格式共用同一张图，
            # 用户打开哪个目录都看得见。目录不存在就跳过（不是错误）。
            img = path
            for tag in ("FLAC", "MP3"):
                gdir = os.path.join(root, plat_dir, tag, g)
                if not os.path.isdir(gdir):
                    continue
                got = spc_wav._save_cover_beside(path, gdir) or path
                if tag == "FLAC":
                    img = got                       # 后续嵌入用这份本地副本
                if os.path.isfile(os.path.join(gdir, spc_wav.COVER_NAME)):
                    stat["png"] += 1
            if not flacs:
                print("  · %-50s 有封面，无 FLAC / MP3 产物" % g[:50])
                continue
            n_flac = n_mp3 = 0
            for p in flacs:
                stat["flac"] += 1
                good, msg = _embed(p, img)
                if good:
                    stat["flac_ok"] += 1
                    if p.lower().endswith(".mp3"):
                        n_mp3 += 1
                    else:
                        n_flac += 1
                else:
                    stat["flac_bad"] += 1
                    print("  ✗ %s: %s" % (os.path.basename(p), msg))
            parts = []
            if n_flac:
                parts.append("FLAC %d" % n_flac)
            if n_mp3:
                parts.append("MP3 %d" % n_mp3)
            print("  ✓ %-50s 回填 %s" % (g[:50], " + ".join(parts) or "0"))

    dur = time.time() - t0
    print("\n%s" % ("== 干跑结果 ==" if args.dry else "== 完成 =="))
    print("  包 %d：命中 %d / 未命中 %d / 无机种源 %d"
          % (stat["game"], stat["hit"], stat["miss"], stat["no_src"]))
    if not args.dry:
        print("  FLAC / MP3 回填 %d 个：成功 %d / 失败 %d"
              % (stat["flac"], stat["flac_ok"], stat["flac_bad"]))
        print("  落到 FLAC / MP3 目录的 cover.png：%d 个" % stat["png"])
    print("  耗时 %.1f 秒 | 缓存共 %d 张图"
          % (dur, cover_art.stats()["images"]))
    return 0 if not stat["flac_bad"] else 1


if __name__ == "__main__":
    sys.exit(main())
