#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_e2e_row_actions.py — v5.9.4「行内按钮 + 上传选游戏」验证。

## 为什么要有这个脚本

v5.9.4 修的是用户报的三件事，三件都**只能在真磁盘上验**：

1. 「已经下载、解压完成但没有转 wav 的，没有入口」
   → 新增行内「转 WAV」。要验的是：**包被删了（勾了「解压后删除原压缩包」）
     只剩解压目录时，也照样转得出来** —— 这是最容易漏的一条。
2. 「已转成 wav 的，加不进试听列表；清空之后也没法手动挑」
   → 新增「＋试听」。要验的是：清空之后再加，那些文件的 mtime **早于**
     清空时刻，也必须在列表里。
3. 「转换完 wav 后点上传云盘提示『这个目录里没有 WAV』」
   → 根因是 ``cloud_upload.collect_wavs`` 只扫一层，而产物在
     ``WAV/<游戏名>/`` 里。要验的是：递归扫得到，且能按游戏过滤。

不依赖浏览器、不联网（云盘登录态用桩替换），跑完约 10 秒。

## 用法

    py -3.14 vendor/_e2e_row_actions.py

退出码 0 = 全过。
"""
import io
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

# 临时文件一律走项目约定的 tmpdir，别在 C 盘留垃圾
try:
    import tmpdir                                                  # noqa: E402
    tmpdir.init(sweep_old=False)
    BASE = tmpdir.root()
except Exception:                                                  # noqa: BLE001
    BASE = tempfile.gettempdir()

SANDBOX = tempfile.mkdtemp(prefix="v594_", dir=BASE)
os.environ["MSU1_DL_PREFS"] = os.path.join(SANDBOX, "prefs.json")
os.environ["LOCALAPPDATA"] = os.path.join(SANDBOX, "localappdata")

import dl_api                                                      # noqa: E402
from web_ui import cloud_api, server                               # noqa: E402

OK = 0
BAD = 0
FAILED = []


def check(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print("  [OK] %s" % name, flush=True)
    else:
        BAD += 1
        FAILED.append(name)
        print("  [!!] %s   %s" % (name, extra), flush=True)


def touch(path, data=b"\0" * 16):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def wav(path, nbytes=64):
    return touch(path, b"RIFF" + b"\0" * (nbytes - 4))


def wait_idle(st, timeout=40.0):
    """等下载/转换跑完（``busy`` 落下）。返回耗时秒数，超时返回 -1。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not st.busy:
            return time.time() - t0
        time.sleep(0.15)
    return -1


def make_state(sub, entries, dest_name="Super Nintendo"):
    """造一个「新进程」的 DownloadState，保存目录指向独立子目录。"""
    st = dl_api.DownloadState()
    dest = os.path.join(SANDBOX, sub, dest_name)
    os.makedirs(dest, exist_ok=True)
    st.set_dest(dest)
    st.entries = [dict(e) for e in entries]
    st._refresh_disk()
    return st, dest


def url_of(stem):
    """假直链。用 zlib.crc32 而不是 hash()：hash 每次进程启动都变，
    重跑时对不上「上一次留下的状态」。"""
    import zlib
    return "https://example.invalid/%d.7z" % (zlib.crc32(stem.encode()) % 100000)


def main():
    print("=" * 62, flush=True)
    print("v5.9.4 行内按钮 / 加入试听 / 上传选游戏", flush=True)
    print("沙箱：%s" % SANDBOX, flush=True)
    print("=" * 62, flush=True)

    # ── [1] 状态串 → 该给哪颗粒按钮 ───────────────────────────────────
    # 前端 actOf() 认的是后端给的状态串。这里验「后端给得出这几种串」，
    # 否则前端那颗按钮永远不出现（用户看到的还是「没有入口」）。
    print("\n[1] 四种状态串都能从 snapshot 里拿到", flush=True)
    stems = ["A Downloaded [SNES]", "B Extracted [SNES]",
             "C Converted [SNES]", "D Nothing [SNES]"]
    ents = [{"name": s, "url": url_of(s), "size": 1, "mtime": 1700000000}
            for s in stems]
    st, dest = make_state("t1", ents)
    touch(os.path.join(dest, dl_api._wav_folder_name(stems[0]) + ".7z"),
          b"7z\xbc\xaf\x27\x1c" + b"\0" * 60)
    os.makedirs(os.path.join(dest, stems[1]), exist_ok=True)
    touch(os.path.join(dest, stems[1], "01.vgm"))
    wav(os.path.join(dest, "WAV", dl_api._wav_folder_name(stems[2]),
                     "01 - t.wav"))
    st._refresh_disk()
    got = {r["name"]: r["state"] for r in st.snapshot()["rows"]}
    check("已下载 → ✓ 已下载", got.get(stems[0]) == "✓ 已下载", got.get(stems[0]))
    check("已解压 → 📦 已解压", got.get(stems[1]) == "📦 已解压", got.get(stems[1]))
    check("已转 WAV → 🎵 已转 WAV", got.get(stems[2]) == "🎵 已转 WAV",
          got.get(stems[2]))
    check("没下过 → 空（不给按钮）", got.get(stems[3]) == "", repr(got.get(stems[3])))

    # ── [2] 单条转 WAV：只剩解压目录（包已被「解压后删除」清掉）────────
    print("\n[2] 转 WAV：包没了、只剩解压目录", flush=True)
    stem = "Only Dir (1993-01-01)(Test)[SNES]"
    u = url_of(stem)
    st, dest = make_state("t2", [{"name": stem, "url": u, "size": 1,
                                  "mtime": 1700000000}])
    src = os.path.join(dest, stem, "01 - Title.spc")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    shutil.copy(os.path.join(HERE, "sample.spc"), src)
    st._refresh_disk()
    check("前置：状态是 📦 已解压",
          st.snapshot()["rows"][0]["state"] == "📦 已解压")
    r = st.convert_wav([u])
    check("convert_wav 接受请求", r.get("ok") and r.get("count") == 1, r)
    took = wait_idle(st)
    check("转换跑完且没卡在「进行中」", took >= 0, "超时 40 秒")
    check("转了 1 首（不是「还没下载」）", st.cv_tracks == 1,
          "ok=%s fail=%s tracks=%s" % (st.cv_ok, st.cv_fail, st.cv_tracks))
    st._refresh_disk()
    check("转完状态变 🎵 已转 WAV",
          st.snapshot()["rows"][0]["state"] == "🎵 已转 WAV",
          st.snapshot()["rows"][0]["state"])
    produced = os.path.join(dest, "WAV", dl_api._wav_folder_name(stem))
    n_wav = len([n for n in os.listdir(produced)
                 if n.lower().endswith(".wav")]) if os.path.isdir(produced) else 0
    check("WAV 落在 WAV/<游戏名>/ 下", n_wav == 1, "%s 里有 %d 个" % (produced, n_wav))
    check("收尾日志按「只转不下载」的口径写",
          any("转 WAV 完成" in str(r[1]) for r in st.logs),
          "日志里没找到「转 WAV 完成」")

    # ── [3] 单条转 WAV：包在、没解压 → 先解压再转 ──────────────────────
    print("\n[3] 转 WAV：包在但没解压（先解压）", flush=True)
    stem3 = "Zip Only (1994-02-02)(Test)[SNES]"
    u3 = url_of(stem3)
    st3, dest3 = make_state("t3", [{"name": stem3, "url": u3, "size": 1,
                                    "mtime": 1700000000}])
    # bsdtar 认 zip，所以「造一个 zip 当 .7z」就能走真实的解压分支
    import zipfile
    arch = os.path.join(dest3, dl_api._wav_folder_name(stem3) + ".7z")
    with zipfile.ZipFile(arch, "w") as z:
        z.write(os.path.join(HERE, "sample.spc"), "02 - Stage.spc")
    st3._refresh_disk()
    check("前置：状态是 ✓ 已下载",
          st3.snapshot()["rows"][0]["state"] == "✓ 已下载",
          st3.snapshot()["rows"][0]["state"])
    r = st3.convert_wav([u3])
    check("convert_wav 接受请求", r.get("ok") is True, r)
    took = wait_idle(st3, timeout=60)
    check("解压 + 转换跑完", took >= 0, "超时 60 秒")
    check("解压了 1 个包", st3.ex_pkgs == 1, "ex_pkgs=%s" % st3.ex_pkgs)
    check("转了 1 首", st3.cv_tracks == 1,
          "ok=%s fail=%s" % (st3.cv_ok, st3.cv_fail))
    st3._refresh_disk()
    check("转完状态变 🎵 已转 WAV",
          st3.snapshot()["rows"][0]["state"] == "🎵 已转 WAV",
          st3.snapshot()["rows"][0]["state"])

    # ── [4] 单条转 WAV：什么都没有 → 明确报错，不能留「进行中」──────────
    print("\n[4] 转 WAV：源文件根本不在", flush=True)
    stem4 = "Ghost (1995-03-03)(Test)[SNES]"
    u4 = url_of(stem4)
    st4, _d4 = make_state("t4", [{"name": stem4, "url": u4, "size": 1,
                                  "mtime": 1700000000}])
    r = st4.convert_wav([u4])
    check("返回 ok（请求本身合法）", r.get("ok") is True, r)
    took = wait_idle(st4, timeout=15)
    check("没有卡在「进行中」（会自己收尾）", took >= 0, "busy 一直为 True")
    check("状态标成 ❌ 还没下载",
          st4.snapshot()["rows"][0]["state"] == "❌ 还没下载",
          st4.snapshot()["rows"][0]["state"])
    check("busy 已复位（「开始下载」不会一直是灰的）", st4.busy is False)

    # ── [5] 加入试听：清空之后还能挑回来 ────────────────────────────────
    print("\n[5] 加入试听：清空 → 再加回来", flush=True)
    stem5 = "Playable (1996-04-04)(Test)[SNES]"
    u5 = url_of(stem5)
    st5, dest5 = make_state("t5", [{"name": stem5, "url": u5, "size": 1,
                                    "mtime": 1700000000}])
    gdir = os.path.join(dest5, "WAV", dl_api._wav_folder_name(stem5))
    for i in range(3):
        wav(os.path.join(gdir, "%02d - Track.wav" % (i + 1)))
    st5._refresh_disk()
    check("前置：3 首在列表里", st5.wav_list(force=True)["total"] == 3,
          st5.wav_list(force=True)["total"])
    c = st5.wav_clear()
    check("清空后列表为 0", st5.wav_list(force=True)["total"] == 0, c)
    r = st5.wav_add([u5])
    check("wav_add 成功", r.get("ok") is True, r)
    check("加回 3 首", r.get("added") == 3, r)
    lst = st5.wav_list(force=True)
    check("列表又是 3 首（mtime 早于清空时刻也放行）", lst["total"] == 3, lst["total"])
    check("清单里带 game 名（上传时要分组）",
          all(w.get("game") for w in lst["wavs"]), lst["wavs"][:1])
    check("重复加入不会翻倍", st5.wav_add([u5]).get("new") == 0,
          st5.wav_add([u5]))

    # ── [6] 「手动加入」要能跨进程（重开程序还在）───────────────────────
    print("\n[6] 重开程序：手动加入的记录还在", flush=True)
    st6 = dl_api.DownloadState()          # 全新实例 = 模拟重启
    st6.set_dest(dest5)
    st6.entries = [{"name": stem5, "url": u5, "size": 1, "mtime": 1700000000}]
    lst6 = st6.wav_list(force=True)
    check("重启后列表还是 3 首", lst6["total"] == 3, lst6["total"])
    check("picked 计数 > 0（确实来自记录文件）", lst6.get("picked", 0) == 3,
          lst6.get("picked"))
    check("记录文件确实落盘",
          os.path.isfile(os.path.join(os.environ["LOCALAPPDATA"],
                                      "MSU1Converter",
                                      dl_api.WAV_PICKS_FILE)))
    check("清空会把「手动加入」一起清掉（清空就是空）",
          st6.wav_clear()["ok"] and st6.wav_list(force=True)["total"] == 0)

    # ── [7] 加入试听：还没转的游戏要明确拒绝 ───────────────────────────
    print("\n[7] 加入试听：还没转的游戏", flush=True)
    stem7 = "No Wav (1997-05-05)(Test)[SNES]"
    u7 = url_of(stem7)
    st7, dest7 = make_state("t7", [{"name": stem7, "url": u7, "size": 1,
                                    "mtime": 1700000000}])
    os.makedirs(os.path.join(dest7, stem7), exist_ok=True)
    # 先验「WAV 目录根本还没建」这条边界：说「还没有 WAV 目录」比
    # 「这个游戏还没转」更准确 —— 用户一个都没转过。
    r0 = st7.wav_add([u7])
    check("WAV 目录都没有 → 说「还没有 WAV 目录」",
          r0.get("ok") is False and "还没有 WAV" in (r0.get("error") or ""), r0)
    os.makedirs(os.path.join(dest7, "WAV"), exist_ok=True)
    r = st7.wav_add([u7])
    check("返回 ok=False 并说明原因", r.get("ok") is False, r)
    check("错误文案是「还没转出 WAV」", "还没转出" in (r.get("error") or ""), r)
    check("missing 里带上了游戏名", stem7 in (r.get("missing") or []),
          r.get("missing"))

    # ── [8] 上传云盘：递归扫到 WAV/<游戏>/ 里的文件（老 bug 的正身）──────
    print("\n[8] 上传云盘：递归扫 WAV/<游戏>/", flush=True)
    up_root = os.path.join(SANDBOX, "t8", "WAV")
    for g, n in (("Game One", 2), ("Game Two", 3)):
        for i in range(n):
            wav(os.path.join(up_root, g, "%02d - t.wav" % (i + 1)))
    import cloud_upload as cu
    shallow = cu.collect_wavs(up_root)
    check("只扫一层 → 0 个（这就是「这个目录里没有 WAV」的根因）",
          len(shallow) == 0, len(shallow))
    deep = cu.collect_wavs(up_root, deep=True)
    check("deep=True → 5 个", len(deep) == 5, len(deep))
    check("默认仍是浅扫（不悄悄改老界面的行为）",
          len(cu.collect_wavs(up_root)) == 0)

    plan = server._cloud_plan(up_root)
    check("_cloud_plan 递归后能列出清单", plan.get("ok") is True, plan.get("error"))
    check("清单数 = 5", plan.get("count") == 5, plan.get("count"))
    plan2 = server._cloud_plan(up_root, ["Game Two"])
    check("按游戏过滤后 = 3", plan2.get("count") == 3, plan2.get("count"))
    plan3 = server._cloud_plan(up_root, ["No Such Game"])
    check("选了个不存在的游戏 → 明确报错",
          plan3.get("ok") is False and "选中的游戏" in (plan3.get("error") or ""),
          plan3)

    # ── [9] 上传云盘：只传选中的游戏 ──────────────────────────────────
    print("\n[9] 上传云盘：只传选中的游戏", flush=True)
    # 把登录态换成桩 —— 真去连网易云会让这条门禁变成「看网络脸色」，
    # 而且开发机上可能真有 cookie，一跑就真传上去了。
    class _StubAuth:
        def ensure(self, log=None):
            return {"ok": False, "need_login": True, "error": "门禁用桩"}

    _orig_auth = cloud_api.auth
    cloud_api.auth = lambda: _StubAuth()
    try:
        st9 = dl_api.DownloadState()
        st9.set_dest(os.path.join(SANDBOX, "t9"))
        os.makedirs(os.path.join(SANDBOX, "t9"), exist_ok=True)
        st9.wav_dir = up_root              # 直接指向上面那份 5 首的产物
        r = st9.upload_cloud(games=["Game Two"])
        check("只收集选中游戏的 3 个文件", r.get("files") == 3, r)
        check("未登录时返回 need_login（前端据此弹登录窗）",
              r.get("need_login") is True, r)
        r2 = st9.upload_cloud(games=["No Such Game"])
        check("选了不存在的游戏 → ok=False 且说清",
              r2.get("ok") is False and "选中的游戏" in (r2.get("error") or ""),
              r2)
        r3 = st9.upload_cloud(games=None)
        check("不传 games = 全传（5 个）", r3.get("files") == 5, r3)
    finally:
        cloud_api.auth = _orig_auth

    # ── [10] wav_games：分组 / 计数 / 体积 ────────────────────────────
    print("\n[10] wav_games 分组", flush=True)
    st10 = dl_api.DownloadState()
    st10.set_dest(os.path.join(SANDBOX, "t10"))
    os.makedirs(os.path.join(SANDBOX, "t10"), exist_ok=True)
    st10.wav_dir = up_root
    g = st10.wav_games()
    check("列出 2 个游戏", len(g.get("games") or []) == 2,
          [x["game"] for x in g.get("games") or []])
    cnt = {x["game"]: x["count"] for x in g.get("games") or []}
    check("每个游戏的曲目数对得上",
          cnt.get("Game One") == 2 and cnt.get("Game Two") == 3, cnt)
    check("总数 5", g.get("total") == 5, g.get("total"))
    check("有体积字段（对话框里要显示）",
          all(x.get("size_text") for x in g.get("games") or []), g.get("games"))
    check("按游戏名排序",
          [x["game"] for x in g.get("games") or []] == sorted(
              [x["game"] for x in g.get("games") or []], key=str.lower))
    # ⚠ 这条是实测踩到的：wav_games 第一版走了 wav_list，于是「清空试听列表」
    #   会把上传对话框也变空 —— 正是这次要修的那个 bug 的另一种形态。
    st10.wav_clear()
    g2 = st10.wav_games()
    check("清空试听列表不影响上传清单（清空只是不想听了，文件还在）",
          len(g2.get("games") or []) == 2 and g2.get("total") == 5, g2)

    # ── 收尾 ──────────────────────────────────────────────────────────
    print("\n" + "=" * 62, flush=True)
    print("通过 %d / 失败 %d" % (OK, BAD), flush=True)
    if FAILED:
        print("失败项：", flush=True)
        for f in FAILED:
            print("  - %s" % f, flush=True)
    print("=" * 62, flush=True)
    return 1 if BAD else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    sys.exit(code)
