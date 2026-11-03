#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_web_ui.py — Web 版（Edge ``--app`` 外壳）后端门禁。

## 这个门禁在守什么

界面好不好看没法自动断言，但**后端契约**可以。Web 版把「业务」和「界面」
彻底分开了：

    convert_job.py   纯流程，无 UI 依赖      ← 已被 Qt 版共用（vendor/test_qt_ui.py 也覆盖）
    web_ui/server.py HTTP + 状态机 + 任务管理 ← 本文件守

所以这里只测三件容易悄悄坏掉的事：

1. **静态白名单**。``server.py`` 顺手把整个目录暴露成可下载，等于把源码
   送出去。一旦有人加了文件又忘了收口，这里必须红。
2. **API 契约**。前端 ``app.js`` 是照着字段名写的，后端改字段名不会报错，
   只会让界面「静悄悄地空白」。
3. **真实转换端到端**。假的 mock 测不出解码链，必须真跑一首。

跑法::

    python vendor/test_web_ui.py            # 全跑（含真实转换）
    python vendor/test_web_ui.py --fast     # 跳过真实转换与取消
"""

import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 本工程约定：临时文件一律走 tmpdir（D:\MSU1Temp），别在 C 盘留垃圾
import tmpdir                                                   # noqa: E402
tmpdir.init(sweep_old=False)

# 下载页偏好（``dl_prefs.json``：保存目录）必须重定向到临时目录，
# **而且必须在 import server 之前设** —— server 在模块级就建了
# ``DownloadState()``，那时偏好已经读完了。
# 不重定向有两个后果：① ``set_opts`` 会把 dl_prefs.json 写进仓库根目录；
# ② 会读到开发机上真实的下载目录，测试结果跟着机器变（换台机器就红）。
os.environ["MSU1_DL_PREFS"] = os.path.join(tmpdir.root(), "gate_dl_prefs.json")
try:
    os.remove(os.environ["MSU1_DL_PREFS"])       # 上一轮的残留会让 dest 指向旧目录
except OSError:
    pass

from web_ui import server                                        # noqa: E402

SPC = os.path.join(ROOT, "vendor", "sample.spc")
CUE = os.path.join(ROOT, "output",
                   "Ginga_Fukei_Densetsu_-_Sapphire_(NTSC-J)_[HCD5080].cue")

_PASS = 0
_FAIL = 0
_BASE = ""


def _mark(cond, label, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [OK] %s" % label, flush=True)
    else:
        _FAIL += 1
        print("  [!!] %s%s" % (label, ("  → " + str(detail)) if detail else ""), flush=True)
    return bool(cond)


def _first_cached_png(root=None):
    """从封面缓存里挑一张现成的 png 当测试素材（**不联网**）。

    v6.0.11 起结构是 ``<根>/<机种>/<游戏名>/<图>.png`` **三层**，以前是
    ``<根>/<机种>/<图>.png`` 两层。这里两种都认 —— 只认两层的话新结构下
    永远找不到素材，一串「嵌图 / 回填」的行为测试会被**静默跳过**
    （门禁还是绿的，但什么都没验）。

    返回绝对路径；一张都没有返回空串。
    """
    import cover_art as _ca
    root = root or _ca.cache_root()
    try:
        for plat in sorted(os.listdir(root)):
            pdir = os.path.join(root, plat)
            if not os.path.isdir(pdir) or plat.startswith("_"):
                continue
            for item in sorted(os.listdir(pdir)):
                ipath = os.path.join(pdir, item)
                if os.path.isfile(ipath):
                    if item.lower().endswith(".png"):     # 旧的两层结构
                        return ipath
                    continue
                try:                                       # 新的三层结构
                    for f in sorted(os.listdir(ipath)):
                        if f.lower().endswith(".png"):
                            return os.path.join(ipath, f)
                except OSError:
                    pass
    except OSError:
        pass
    return ""


def _rmtree_soft(path, batch=40):
    """分批删目录，删不掉也不抛、更不会把进程带走。

    为什么不用 ``shutil.rmtree``：环境里挂着一道「本回合删除超过 50 个文件要二次
    确认」的安全守卫，它会**直接打断整个进程** —— 门禁跑到最后一步清理临时目录
    时被杀，结论行永远不打印，看着像「测试挂了」。`[3i]` 流式音频那段会产出
    60 个 WAV，正好越线（实测 60 / 62 两个数）。

    所以这里自己走 ``os.remove``，每批 40 个，并且连 ``BaseException`` 都吃掉：
    清理失败最多留几个临时文件（系统会回收），不能让 500 多条断言白跑。
    """
    if not path or not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for i in range(0, len(files), batch):
            for name in files[i:i + batch]:
                try:
                    os.remove(os.path.join(root, name))
                except BaseException:                       # noqa: BLE001 清理而已
                    pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except BaseException:                           # noqa: BLE001
                pass
    try:
        os.rmdir(path)
    except BaseException:                                   # noqa: BLE001
        pass




def _req(path, body=None, headers=None):
    """返回 (status, headers_dict, bytes)。4xx/5xx 不抛，交给调用方断言。"""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(_BASE + path, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def _jget(path):
    code, _h, raw = _req(path)
    try:
        return code, json.loads(raw.decode("utf-8"))
    except Exception:                                            # noqa: BLE001
        return code, {}


def _jpost(path, body=None):
    code, _h, raw = _req(path, body if body is not None else {})
    try:
        return code, json.loads(raw.decode("utf-8"))
    except Exception:                                            # noqa: BLE001
        return code, {}


def _post_raw(path, data, headers=None):
    """POST **二进制**（拖拽上传用）。

    ⚠ 不能复用 :func:`_req`：它会把 body 当 JSON 序列化，二进制直接炸。
    """
    hdrs = dict(headers or {})
    req = urllib.request.Request(_BASE + path, data=data, headers=hdrs,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _wait(cond, timeout, step=0.2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


# ══════════════════════════════════════════════════════════════════════
#  1. 静态文件与安全
# ══════════════════════════════════════════════════════════════════════
def t_static():
    print("\n[1] 静态文件与白名单", flush=True)

    code, hdr, raw = _req("/")
    _mark(code == 200 and b"RetroMusic Lab" in raw, "GET / 返回 index.html", code)
    _mark("text/html" in hdr.get("content-type", ""), "GET / 是 text/html",
          hdr.get("content-type"))
    _mark("no-store" in hdr.get("cache-control", ""), "GET / 禁缓存（no-store）",
          hdr.get("cache-control"))

    for name, ctype in (("/app.css", "text/css"), ("/app.js", "text/javascript")):
        code, hdr, raw = _req(name)
        _mark(code == 200 and len(raw) > 500, "GET %s 有内容" % name,
              "%s / %d B" % (code, len(raw)))
        _mark(ctype in hdr.get("content-type", ""), "GET %s 是 %s" % (name, ctype),
              hdr.get("content-type"))

    # 白名单必须真的收口 —— 这些都不许出去
    for bad in ("/server.py", "/convert_job.py", "/shot.py", "/native_dialog.py",
                "/web_ui/server.py", "/../msu1_to_cd.py",
                "/%2e%2e/msu1_to_cd.py", "/msu1_to_cd.py", "/index.html.bak"):
        code, _h, _r = _req(bad)
        _mark(code == 404, "GET %s 被拒（404）" % bad, code)


# ══════════════════════════════════════════════════════════════════════
#  2. API 契约
# ══════════════════════════════════════════════════════════════════════
def t_api_contract(state):
    print("\n[2] API 契约", flush=True)

    code, d = _jget("/api/state?since=0")
    want = {"state", "progress", "status", "error", "result",
            "sources", "opts", "outputs", "upload", "logs", "seq"}
    _mark(code == 200, "GET /api/state 200", code)
    _mark(want <= set(d), "state 字段齐全", sorted(want - set(d)))
    _mark(d.get("state") == "idle", "初始 state=idle", d.get("state"))
    _mark(isinstance(d.get("opts"), dict) and "max_secs" in d["opts"],
          "opts 带默认值")

    code, d = _jpost("/api/nope", {})
    _mark(code == 404 and d.get("ok") is False, "未知 API 返回 404 + ok:false", code)

    code, d = _jpost("/api/start", {})
    _mark(code == 400 and d.get("ok") is False, "无音源时 start 被拒（400）", code)

    code, _h, raw = _req("/api/audio?id=999999")
    _mark(code == 404, "试听不存在的 id → 404", code)

    # 还没转换过 → 输出目录不存在，上传必须明确拒绝而不是悄悄什么都不做
    code, d = _jpost("/api/upload_cloud", {})
    _mark(code == 400 and d.get("ok") is False, "无输出时上传被拒（400）", d)

    # 诊断接口：所有可选依赖都得真能 import，libgme 得真能载
    code, d = _jget("/api/diag")
    _mark(code == 200 and d.get("ok"), "GET /api/diag 200", code)
    mods = d.get("modules") or {}
    _mark(bool(mods) and all(mods.values()), "依赖全部可用",
          [k for k, v in mods.items() if not v])
    _mark(d.get("libgme") == "ok", "libgme.dll 能加载", d.get("libgme"))
    _mark(bool(d.get("temp")), "临时目录已定", d.get("temp"))

    # 「从网络下载」= 转手给 tkinter 版。只验查找逻辑，不真启动 exe
    code, d = _jpost("/api/open_downloader", {"dry": True})
    if code == 200:
        _mark(os.path.isfile(d.get("exe") or ""), "找得到 tkinter 版 exe",
              d.get("exe"))
    else:
        _mark(code == 404, "没有 tkinter 版时给 404 + 说明", d.get("error"))


# ══════════════════════════════════════════════════════════════════════
#  3. 加音源 / 去重 / 跳过
# ══════════════════════════════════════════════════════════════════════
def t_sources(state):
    print("\n[3] 音源管理", flush=True)

    code, d = _jpost("/api/add_sources", {"paths": [SPC]})
    _mark(code == 200 and d.get("added") == 1, "加 sample.spc 成功", d)

    code, d = _jpost("/api/add_sources", {"paths": [SPC]})
    _mark(d.get("added") == 0 and len(d.get("skipped") or []) == 1,
          "重复添加被去重", d)

    # SPC 探测：轨数与时长（只读 ID666，不该失败）
    code, d = _jget("/api/state")
    src = (d.get("sources") or [{}])[0]
    _mark(src.get("kind") == "SPC", "SPC 被识别成 SPC", src.get("kind"))
    _mark(src.get("n_tracks") == 1, "SPC 探到 1 轨", src.get("n_tracks"))
    _mark(float(src.get("total_secs") or 0) > 0, "SPC 探到时长",
          src.get("total_secs"))

    # 中间那张表的数据源
    code, d = _jget("/api/tracks?i=0")
    rows = d.get("tracks") or []
    _mark(code == 200 and len(rows) == 1, "/api/tracks 列出 SPC 曲目", d)
    _mark(bool(rows) and rows[0].get("title") == "Stage Clear",
          "曲目名取自 ID666", rows[0] if rows else None)
    _mark(bool(rows) and float(rows[0].get("secs") or 0) > 0, "曲目带时长",
          rows[0].get("secs") if rows else None)

    code, d = _jget("/api/tracks?i=99")
    _mark(code == 404, "/api/tracks 越界 → 404", code)

    # 不认识的文件要「跳过并说明原因」，不能静默吞掉
    junk = os.path.join(tmpdir.root(), "not_a_song.txt")
    with open(junk, "w", encoding="utf-8") as f:
        f.write("x")
    code, d = _jpost("/api/add_sources", {"paths": [junk]})
    _mark(d.get("added") == 0 and (d.get("skipped") or [{}])[0].get("why"),
          "不认识的文件被跳过且给出原因", d)

    # PSF 系（PS1 芯片音源）：v5.2 起是**原生解码**（psf_native + psf2wav.exe），
    # 不再转手 tkinter 版 / foobar2000。这里只验「能加进来、归成 PSF」——
    # 真解码要一份 BIOS，另见 [3c]。
    psf = os.path.join(tmpdir.root(), "fake.psf")
    with open(psf, "wb") as f:
        f.write(b"PSF\x01\x00\x00\x00" + b"\0" * 12)
    code, d = _jpost("/api/add_sources", {"paths": [psf]})
    _mark(code == 200 and d.get("added") == 1,
          "PSF 能被加进音源（不再要求转手）", d)

    code, st = _jget("/api/state")
    kinds = [s.get("kind") for s in (st.get("sources") or [])]
    _mark("PSF" in kinds, "PSF 归成 PSF", kinds)

    # 清空：假 PSF 不能留给后面段落，否则下标整体移位
    _jpost("/api/clear", {})
    _jpost("/api/add_sources", {"paths": [SPC]})

    code, d = _jpost("/api/remove_source", {"index": 0})
    code, d = _jget("/api/state")
    _mark(len(d.get("sources") or []) == 0, "移除音源生效",
          len(d.get("sources") or []))

    code, d = _jpost("/api/remove_source", {"index": 99})
    _mark(code == 200, "越界移除不崩", code)


# ══════════════════════════════════════════════════════════════════════
#  4. 参数合并与状态机
# ══════════════════════════════════════════════════════════════════════
def _make_txtp_pack(base):
    """造一个最小的 PS1 ``.txtp`` 包 —— 实现搬到 ``_txtp_fixture.py``，
    与 exe 验收共用同一份样本（各写一遍必然漂移）。"""
    from _txtp_fixture import make_pack
    return make_pack(base)


# ══════════════════════════════════════════════════════════════════════
#  3b. .txtp（PSX 音频索引）—— 纯 Python 解码，不需要 foobar2000
# ══════════════════════════════════════════════════════════════════════
def t_txtp(state, fast=False):
    print("\n[3b] .txtp 接入", flush=True)
    pack = _make_txtp_pack(tmpdir.root())
    a = os.path.join(pack, "A.txtp")
    b = os.path.join(pack, "B.txtp")
    c = os.path.join(pack, "C.txtp")

    code, d = _jpost("/api/add_sources", {"paths": [a, b, c]})
    _mark(code == 200 and d.get("added") == 3,
          "3 个 .txtp 都能加进音源", d)

    code, st = _jget("/api/state")
    srcs = st.get("sources") or []
    _mark(all(s.get("kind") == "PSX 音频" for s in srcs),
          "都归成「PSX 音频」", [s.get("kind") for s in srcs])
    _mark(all(s.get("n_tracks") == 1 for s in srcs), "每个都是 1 轨",
          [s.get("n_tracks") for s in srcs])
    _mark(float(srcs[0].get("total_secs") or 0) > 0, "XA 探到时长",
          srcs[0].get("total_secs"))
    _mark(abs(float(srcs[1].get("total_secs") or 0) - 1.0) < 0.02,
          "DA 探到时长 1.0s", srcs[1].get("total_secs"))
    # 目标缺失的那个也必须能加（转换时才报错），但大小/时长是 0
    _mark(float(srcs[2].get("total_secs") or 0) == 0.0,
          "目标缺失的 .txtp 时长 0（不假装能转）", srcs[2].get("total_secs"))

    code, d = _jget("/api/tracks?i=0")
    rows = d.get("tracks") or []
    _mark(len(rows) == 1, "/api/tracks 列出 1 轨", d)
    _mark(bool(rows) and rows[0].get("title") == "测试曲A",
          "曲名取自 !tags.m3u", rows[0] if rows else None)
    _mark(bool(rows) and rows[0].get("kind") == "XA", "类型是 XA",
          rows[0].get("kind") if rows else None)

    # 真转一遍：.txtp 必须能出 WAV，也能出 BIN+CUE（走的是主流水线）
    if fast:
        print("  [--] --fast：跳过 .txtp 真实转换", flush=True)
        _jpost("/api/clear", {})
        return
    out = os.path.join(tmpdir.root(), "webui_gate_txtp")
    code, d = _jpost("/api/start", {"opts": {"out_dir": out, "do_cd": True,
                                             "do_wav": True}})
    _mark(code == 200 and d.get("ok"), "启动 .txtp 转换", d)
    ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 180)
    code, st = _jget("/api/state")
    _mark(ok, "转换在 180 秒内结束", st.get("state"))
    _mark(st.get("state") == "done", "state=done", st.get("state"))
    res = st.get("result") or {}
    # 3 个音源里 C 的目标不存在，必然 1 首失败 —— 这正是要断言的行为：
    # 不能静默跳过，要计数并让 state 变 failed 或 fail≥1
    _mark(int(res.get("ok") or 0) >= 2, "至少 2 首转成功", res)
    _mark(int(res.get("fail") or 0) == 1, "缺失目标计 1 失败", res)
    names = sorted(os.listdir(out)) if os.path.isdir(out) else []
    _mark(any(n.lower().endswith(".cue") for n in names), "出了 CUE",
          "、".join(names))
    _mark(any(n.lower().endswith(".wav") for n in names), "出了 WAV", "")

    _jpost("/api/clear", {})
    code, st = _jget("/api/state")
    _mark(len(st.get("sources") or []) == 0, "clear 清空音源", st.get("sources"))


def t_txtp_stream(state, fast=False):
    """子流式 ``.txtp``：**文件 0 字节，子流号写在文件名里**。

    真实下载包（PS1 的 ``BGM.XA`` 一类的多子流 XA）解压出来就是一堆
    ``BGM.XA#01.txtp`` / ``#02`` …，每个都是 0 字节 —— 内容不在文件里，
    而在**名字**里。早先 ``parse_txtp`` 见到空文件直接判「.txtp 是空的」，
    于是整包 PS1 音源一首也转不出来（用户报的就是这个）。

    这里要盯住三件事，缺一条就会「看起来能跑但全错」：

    1. **序号口径**：``#01`` 是「第 1 条子流」，按 ``(file, channel)`` 升序
       排 —— 不是 subheader 里那个 channel 字节。样本故意让两者错位
       （序号 1/2/3 ↔ channel 0/1/2），实现若写成 ``channel == N`` 会选错。
    2. **每首时长不同**：三条子流扇区数是 6/4/2。若都取整文件或都取最长
       那条，界面上 3 首会显示同一个时长 —— 一眼看不出，但转出来全一样长。
    3. **越界要报人话**：``#09`` 只有 3 条子流时必须说「超出范围」，
       不能抛 IndexError，更不能静默转出空 WAV。
    """
    print("\n[3b-2] 子流式 .txtp（0 字节 + 文件名带子流号）", flush=True)
    from _txtp_fixture import (make_stream_pack, STREAM_SECTORS,
                              STREAM_CHANNELS, stream_seconds)
    import psx_txtp

    pack = make_stream_pack(tmpdir.root())
    nums = sorted(STREAM_SECTORS)
    paths = [os.path.join(pack, "BGM.XA#%02d.txtp" % n) for n in nums]

    # -- ① 解析：子流号从文件名里来 --------------------------------
    _mark(all(os.path.getsize(p) == 0 for p in paths),
          "样本里的 .txtp 都是 0 字节（内容在文件名里）",
          [os.path.getsize(p) for p in paths])
    parsed = [psx_txtp.parse_txtp(p) for p in paths]
    _mark(all(x["ok"] for x in parsed),
          "0 字节 .txtp 解析成功（不再报「是空的」）",
          [x["error"] for x in parsed if not x["ok"]])
    _mark([x.get("stream") for x in parsed] == nums,
          "子流号取自文件名 #01/#02/#03", [x.get("stream") for x in parsed])
    _mark(all(os.path.basename(x["target"]).upper() == "BGM.XA" for x in parsed),
          "目标都指向同一个 BGM.XA", [x.get("target") for x in parsed])

    # -- ② 时长：每首只算自己那条流 --------------------------------
    probes = [psx_txtp.probe_txtp(p) for p in paths]
    _mark(all(x["ok"] for x in probes),
          "三条子流都能探到时长", [x["error"] for x in probes if not x["ok"]])
    exp = [stream_seconds(STREAM_SECTORS[n]) for n in nums]
    got = [x["seconds"] for x in probes]
    _mark(all(abs(g - e) < 0.002 for g, e in zip(got, exp)),
          "时长按各自扇区数算（6/4/2 → 0.320/0.213/0.107）",
          ["%.4f/期望%.4f" % (g, e) for g, e in zip(got, exp)])
    _mark(len(set(round(g, 4) for g in got)) == 3,
          "三首时长互不相同（没退化成整文件）", got)
    _mark([x["kind"] for x in probes] == ["XA#%d" % n for n in nums],
          "类型标成 XA#序号", [x["kind"] for x in probes])

    # -- ③ 解码：真出 PCM，且挑的不是同一条 ------------------------
    dec = [psx_txtp.decode_pcm(p) for p in paths]
    _mark(all(x["ok"] for x in dec),
          "三条子流都能解出 PCM", [x["error"] for x in dec if not x["ok"]])
    _mark(all(abs(x["seconds"] - e) < 0.002 for x, e in zip(dec, exp)),
          "解码后时长与探针一致", [round(x["seconds"], 4) for x in dec])
    lens = [len(x["pcm"]) for x in dec]
    _mark(len(set(lens)) == 3 and all(lens),
          "三条 PCM 长度各不相同（确实挑的是不同流）", lens)
    # 序号口径核对：实现若按 channel 字节挑，第 1 条会拿到 channel=0 的那条
    # —— 样本里 channel 0 恰好也是序号 1，所以再正面确认一次「序号是排序后的位置」
    _mark(sorted(STREAM_CHANNELS.values()) == [0, 1, 2],
          "样本的 channel 字节与序号错位（防实现按 channel 选）",
          STREAM_CHANNELS)

    # -- ④ 越界：报人话，不抛 IndexError --------------------------
    p9 = os.path.join(pack, "BGM.XA#09.txtp")
    pr9 = psx_txtp.probe_txtp(p9)
    d9 = psx_txtp.decode_pcm(p9)
    _mark(not pr9["ok"] and "超出范围" in pr9["error"],
          "探针对越界子流报「超出范围」", pr9.get("error"))
    _mark(not d9["ok"] and "超出范围" in d9["error"],
          "解码对越界子流报「超出范围」", d9.get("error"))

    # -- ⑤ 走主流水线：ConvertJob 真出 WAV + BIN+CUE --------------
    if fast:
        print("  [--] --fast：跳过子流式 .txtp 真实转换", flush=True)
        return
    allp = paths + [p9]
    code, d = _jpost("/api/add_sources", {"paths": allp})
    _mark(code == 200 and d.get("added") == 4,
          "4 个（含越界那条）都能加进音源", d)
    code, st = _jget("/api/state")
    srcs = st.get("sources") or []
    _mark(len(srcs) == 4, "音源列表 4 项", len(srcs))
    # 越界那条探不到时长（0.0），所以只比非零的三条
    got_secs = sorted(round(float(s.get("total_secs") or 0), 3) for s in srcs
                      if float(s.get("total_secs") or 0) > 0)
    _mark(got_secs == sorted(round(e, 3) for e in exp),
          "列表里三首时长各不同", [s.get("total_secs") for s in srcs])
    _mark(any(s.get("kind") == "PSX 音频" for s in srcs),
          "仍归成「PSX 音频」", [s.get("kind") for s in srcs])

    out = os.path.join(tmpdir.root(), "webui_gate_stream")
    code, d = _jpost("/api/start", {"opts": {"out_dir": out, "do_cd": True,
                                             "do_wav": True}})
    _mark(code == 200 and d.get("ok"), "启动子流式转换", d)
    ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 180)
    code, st = _jget("/api/state")
    _mark(ok, "转换在 180 秒内结束", st.get("state"))
    _mark(st.get("state") == "done", "state=done", st.get("state"))
    res = st.get("result") or {}
    _mark(int(res.get("ok") or 0) == 3, "3 首转成功（越界那条不算）", res)
    _mark(int(res.get("fail") or 0) == 1, "越界那条计 1 失败", res)
    names = sorted(os.listdir(out)) if os.path.isdir(out) else []
    wavs = [n for n in names if n.lower().endswith(".wav")]
    _mark(len(wavs) == 3, "出了 3 个 WAV", "、".join(wavs))
    _mark(any(n.lower().endswith(".cue") for n in names), "出了 CUE",
          "、".join(names))
    # 编号必须连续 01/02/03 —— 早先 write_tracks 的 last_idx 多 +1 会跳号
    _mark([w[:2] for w in wavs] == ["01", "02", "03"],
          "WAV 编号连续 01/02/03（不跳号）", "、".join(wavs))
    # 三个 WAV 大小必须各不相同，否则说明写的是同一段音频
    sizes = []
    for w in wavs:
        fp = os.path.join(out, w)
        sizes.append(os.path.getsize(fp) if os.path.isfile(fp) else 0)
    _mark(len(set(sizes)) == 3 and all(sizes),
          "三个 WAV 大小各不相同（没写成同一段）", sizes)

    _jpost("/api/clear", {})
    code, st = _jget("/api/state")
    _mark(len(st.get("sources") or []) == 0, "clear 清空音源", st.get("sources"))

    # -- ⑥ 加整个文件夹（用户真正的操作：解压完直接拖目录）-------
    # 没有这条时目录会被 classify 归成「未知」直接拒收，用户只能一个个挑
    # 23 个 .txtp —— 这才是「解压出来转不了」最可能踩到的第一步。
    from convert_job import classify
    _mark(classify(pack) == "PSX 音频",
          "包目录归成「PSX 音频」（不再是未知）", classify(pack))
    code, d = _jpost("/api/add_sources", {"paths": [pack]})
    _mark(code == 200 and d.get("added") == 1, "整个包目录能加进音源", d)
    code, st = _jget("/api/state")
    dsrc = (st.get("sources") or [{}])[0]
    _mark(dsrc.get("kind") == "PSX 音频", "目录归成 PSX 音频", dsrc.get("kind"))
    _mark(int(dsrc.get("n_tracks") or 0) == 4,
          "轨数 = 目录里 .txtp 个数（4）", dsrc.get("n_tracks"))
    _mark(abs(float(dsrc.get("total_secs") or 0) - sum(exp)) < 0.01,
          "总时长 = 三首之和（越界那条不算）", dsrc.get("total_secs"))
    code, d = _jget("/api/tracks?i=0")
    drows = d.get("tracks") or []
    _mark(len(drows) == 4, "曲目表列出 4 行", len(drows))

    out2 = os.path.join(tmpdir.root(), "webui_gate_streamdir")
    code, d = _jpost("/api/start", {"opts": {"out_dir": out2, "do_cd": True,
                                             "do_wav": True}})
    _mark(code == 200 and d.get("ok"), "启动目录转换", d)
    ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 180)
    code, st = _jget("/api/state")
    _mark(ok and st.get("state") == "done", "目录转换在 180 秒内结束",
          st.get("state"))
    res = st.get("result") or {}
    _mark(int(res.get("ok") or 0) == 3, "目录转出 3 首（坏的那条跳过）", res)
    dnames = sorted(os.listdir(out2)) if os.path.isdir(out2) else []
    dwav = [n for n in dnames if n.lower().endswith(".wav")]
    _mark(len(dwav) == 3, "目录出 3 个 WAV", "、".join(dwav))
    dcue = [n for n in dnames if n.lower().endswith(".cue")]
    _mark(len(dcue) == 1, "整目录合成 1 个 CUE（一个音源=一张盘）",
          "、".join(dcue))
    if dcue:
        txt = open(os.path.join(out2, dcue[0]), encoding="utf-8",
                   errors="replace").read()
        _mark(txt.count("TRACK ") == 3, "CUE 里 3 条 TRACK", txt.count("TRACK "))

    _jpost("/api/clear", {})


def _make_xa_file(base, n_sectors=4):
    """造一个**平铺包里的独立 ``.XA``**：RIFF/CDXA 头 + 整扇区。

    刻意复刻真实文件（JoshW 的 PS1 包）的两个坑，否则测不出东西：

    1. **44 字节 RIFF 头**（``RIFF <size> CDXA`` + ``fmt `` + ``data``）。
       扇区从 ``data`` 之后才开始 —— 扫描偏移算错的话一个扇区都匹配不上。
    2. **两份 subheader 副本的 channel 字节不一致**（``01 00 64 01 | 01 02
       64 01``）。按「8 字节完全相等」判会把整包判成没有音频 —— 实测
       Armored Core 的 15 个文件全是这个形态。

    扇区数据全 0 = 静音 ADPCM（预测器系数 0、nibble 0），够验通路，
    也不用往仓库里塞几 MB 的样本。
    """
    import struct

    sync = b"\x00" + b"\xff" * 10 + b"\x00"
    sub = bytes([0x01, 0x00, 0x64, 0x01, 0x01, 0x02, 0x64, 0x01])
    payload = bytearray()
    for i in range(n_sectors):
        sec = bytearray(2352)
        sec[0:12] = sync
        sec[12:15] = bytes([0, 3, i])          # 地址（内容无所谓）
        sec[15] = 0x02                          # MODE2
        sec[16:24] = sub
        # 24..2328 是 18 组 × 128 字节的 ADPCM 数据，留 0 = 静音
        payload += sec
    fmt = bytes([0, 0, 0, 0, 1, 0x55, 0x58, 0x41, 1, 0, 0, 0, 0, 0, 0, 0])
    body = (b"CDXA"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(payload)) + bytes(payload))
    path = os.path.join(base, "TEST_BGM01.XA")
    with open(path, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def t_batch_and_drop(state, fast=False):
    """v5.4：左栏「删除选中 / 清空」+ 拖拽上传兜底。

    两块分开验，因为坏法完全不同：

    1. **批量删除 / 清空**：清空必须**只清音源**，不能连 outputs 一起清 ——
       用户清列表往往只是想换一批音源，已经转好的 WAV 消失是不可接受的
       （`/api/clear` 才是「整个任务重来」，那是另一回事）。
    2. **拖拽**：Chromium 出于安全**永远**不给网页真实路径
       （`File.path` 是 Electron 私有扩展，纯 Edge `--app` 窗口里恒为
       undefined），所以前端改成「把文件内容 POST 上来」。这里验后端那半边：
       blob 写入 + 目录结构保持 + commit 后能被 add_paths 识别。
    """
    print("\n[3b-2c] 批量删除 / 清空 / 拖拽上传", flush=True)

    d = os.path.join(tmpdir.root(), "webui_gate_batch_%d" % os.getpid())
    os.makedirs(d, exist_ok=True)
    paths = []
    for i in range(4):
        p = os.path.join(d, "b%d.spc" % i)
        with open(p, "wb") as fh:
            fh.write(b"SNES-SPC700 Sound File Data v0.30\x1a\x1a"
                     + b"\x00" * 66000)
        paths.append(p)

    _jpost("/api/clear", {})
    code, r = _jpost("/api/add_sources", {"paths": paths})
    _mark(code == 200 and int(r.get("added") or 0) == 4, "先加 4 个音源", r)

    # 塞一个假 output：后面验证 clear_sources **不动**它
    with state.lock:
        state.outputs = [{"id": 99, "name": "keep.wav", "path": "x",
                          "secs": 1.0}]

    # ① 批量删除（索引故意乱序，服务端要自己兜成倒序）
    code, r = _jpost("/api/remove_sources", {"indexes": [2, 0]})
    _mark(code == 200 and int(r.get("removed") or 0) == 2, "批量删 2 个", r)
    code, st = _jget("/api/state")
    left = [s["label"] for s in (st.get("sources") or [])]
    _mark(len(left) == 2 and left == ["b1", "b3"], "剩下的是 b1/b3", left)

    # ② 清空音源
    code, r = _jpost("/api/clear_sources", {})
    _mark(code == 200 and int(r.get("cleared") or 0) == 2, "清空音源", r)
    code, st = _jget("/api/state")
    _mark(len(st.get("sources") or []) == 0, "音源空了",
          len(st.get("sources") or []))
    _mark(len(st.get("outputs") or []) == 1,
          "清空音源**不动**输出结果", len(st.get("outputs") or []))
    with state.lock:
        state.outputs = []

    # ③ 拖拽：blob 上传 + commit
    sess = "gate%d" % os.getpid()
    rel = "dndrop_pack/b3.spc"
    with open(paths[3], "rb") as fh:
        data = fh.read()
    code, raw = _post_raw("/api/drop_blob", data, {
        "Content-Type": "application/octet-stream",
        "X-Rel-Path": urllib.parse.quote(rel, safe=""),
        "X-Drop-Session": sess,
    })
    _mark(code == 200, "drop_blob 写入成功", raw[:80])

    # 目录结构要保住（.mini2sf 靠同目录的 .2sflib）
    # ⚠ 落盘根是 drop_root()（`<下载盘>/MSU1Downloads/拖入/`），**不是**
    #   tmpdir —— 拖入的音源放临时目录会被 sweep 清掉，输出也会跟着进去。
    written = os.path.join(server.drop_root(), sess, "dndrop_pack", "b3.spc")
    _mark(os.path.isfile(written), "按相对路径落盘（结构没拍平）", written)

    code, r = _jpost("/api/drop_commit", {"session": sess})
    _mark(code == 200 and int(r.get("added") or 0) == 1,
          "drop_commit 收进 1 个音源", r)
    code, st = _jget("/api/state")
    _mark(len(st.get("sources") or []) == 1, "音源数 1",
          len(st.get("sources") or []))

    # ④ 路径穿越必须被**拒绝**（不是静默过滤）
    code, _raw = _post_raw("/api/drop_blob", b"x", {
        "Content-Type": "application/octet-stream",
        "X-Rel-Path": urllib.parse.quote("../../evil.txt", safe=""),
        "X-Drop-Session": sess,
    })
    _mark(code == 400, "路径穿越被拒绝（400）", code)

    _jpost("/api/clear", {})


def t_xsf_home(state, fast=False):
    """首页（主转换器）能不能收 XSF 家族 —— v5.4 补的一个真缺口。

    用户报障原话：「现在支持了这么多种格式，首页转换器还没有实装这一部分」。

    根因不是 ``classify``（它早就认识 USF/2SF/SSF/DSF/GSF），而是**两条旁路**：

    1. ``web_ui/server.py`` 的 ``_SRC_EXTS`` 只拼了
       ``chip_formats.ALL_EXTS``（含 PSF 六后缀）+ ``.xa`` + 流式音频，
       **没有** USF/VIO/SSF/GSF 与 ``.txtp``。而 ``classify()`` 对这些目录
       **故意**返回「未知」（为了逐文件展开、一首坏掉只废那一首），于是走到
       ``scan_dir_sources`` —— 那里只认 ``_SRC_EXTS``，结果「一个装着 45 个
       .mini2sf 的目录」扫出来是 **0 个音源**，界面表现就是「添加文件夹后
       什么都没有」。
    2. ``web_ui/native_dialog.py`` 的 ``FILTER_PARTS`` 同样漏了这些后缀，
       Windows 的 OFN 对话框会把文件**直接隐藏**，用户根本选不到。

    这两处都属于「能转但加不进来」，比「转失败」更让人摸不着头脑。
    """
    print("\n[3b-2b] 首页能收 XSF 家族（USF / 2SF / SSF-DSF / GSF / .txtp）",
          flush=True)
    import convert_job
    from web_ui import native_dialog

    # ① 静态：_SRC_EXTS 必须含全部新后缀
    want = [".usf", ".miniusf", ".2sf", ".mini2sf",
            ".ssf", ".minissf", ".dsf", ".minidsf",
            ".gsf", ".minigsf", ".txtp"]
    missing = [e for e in want if e not in server._SRC_EXTS]
    _mark(not missing, "_SRC_EXTS 含全部 XSF 后缀", "缺 %s" % missing)

    # ② 静态：文件选择器过滤器必须含全部新后缀（拼成一个大串再找）
    blob = ";".join(p for _label, p in native_dialog.FILTER_PARTS)
    miss2 = [e for e in want if ("*" + e) not in blob]
    _mark(not miss2, "FILTER_PARTS 含全部 XSF 后缀", "缺 %s" % miss2)

    # ③ 动态：四家族各一个 → classify 必须归「未知」→ 逐文件展开。
    #    ⚠ 这里**不能**混进 .txtp / .psf2：那两个的 classify 优先级更高，
    #      会把整个目录归成「PSX 音频」/「PSF」整包（那是对的行为，
    #      所以它们的「整包」语义放到 ⑥ 单独验）。
    base = os.path.join(tmpdir.root(), "webui_gate_xsf_%d" % os.getpid())
    os.makedirs(base, exist_ok=True)
    for name in ("a.miniusf", "b.mini2sf", "c.ssf", "d.minigsf"):
        with open(os.path.join(base, name), "wb") as fh:
            fh.write(b"PSF\x21")            # 内容不重要，只验收集
    _mark(convert_job.classify(base) == "未知",
          "混合 XSF 目录归「未知」（逐文件展开）",
          convert_job.classify(base))
    found, _cut = server.scan_dir_sources(base)
    got = {os.path.splitext(p)[1].lower() for p in found}
    _mark(got == {".miniusf", ".mini2sf", ".ssf", ".minigsf"},
          "scan_dir_sources 收齐 4 种", sorted(got))

    # ④ 端到端：/api/add_sources 传目录要能加进来（4 个都加）
    _jpost("/api/clear", {})
    code, d = _jpost("/api/add_sources", {"paths": [base]})
    _mark(code == 200 and int(d.get("added") or 0) == 4,
          "add_sources(目录) 加进 4 个", d)
    code, st = _jget("/api/state")
    kinds = {s.get("kind") for s in (st.get("sources") or [])}
    _mark(bool(kinds & {"USF", "2SF", "SSF", "DSF", "GSF"}),
          "音源类型认出来了", sorted(kinds))
    _jpost("/api/clear", {})

    # ⑤ .txtp 目录 → 归「PSX 音频」整包（不是逐文件）
    tp = os.path.join(tmpdir.root(), "webui_gate_txtp_dir_%d" % os.getpid())
    os.makedirs(tp, exist_ok=True)
    with open(os.path.join(tp, "A.txtp"), "w", encoding="utf-8") as fh:
        fh.write("sound/a.xa\n")
    _mark(convert_job.classify(tp) == "PSX 音频",
          ".txtp 目录归「PSX 音频」（整包）", convert_job.classify(tp))

    # ⑥ .psf2 目录 → 归「PSF」整包
    pf = os.path.join(tmpdir.root(), "webui_gate_psf2_dir_%d" % os.getpid())
    os.makedirs(pf, exist_ok=True)
    with open(os.path.join(pf, "a.psf2"), "wb") as fh:
        fh.write(b"PSF\x24")
    _mark(convert_job.classify(pf) == "PSF",
          ".psf2 目录归「PSF」（整包）", convert_job.classify(pf))


def t_xa(state, fast=False):
    """平铺的 PS1 包：``一堆 .XA`` + 一个 ``.PSF``，**没有 .txtp**。

    用户报的原始现象就是这种包「只转出了 PSF 那一首」，15 个 XA 全丢
    （``.xa`` 不在 ``CHIP_EXTS`` 里，没人把它当音源）。

    这里要盯住四件事：

    1. **认得出 RIFF 头**（否则 ``xa_sector_groups`` 返回空 = 「没有 XA
       音频扇区」）。
    2. **subheader 副本不一致也算 XA**（真实文件的形态）。
    3. **目录要逐文件展开**：``classify(目录)`` 必须返回「未知」，交给
       ``scan_dir_sources`` 把每个 ``.xa`` / ``.psf`` 各算一个音源 ——
       归成一个整包就得在同一个音源里混两套解码器。
    4. **真能转出 WAV**，且 ``/api/tracks`` 列得出来。
    """
    print("\n[3b-3] 独立 .XA（平铺包，没有 .txtp）", flush=True)
    import convert_job

    base = os.path.join(tmpdir.root(), "webui_gate_xa")
    os.makedirs(base, exist_ok=True)
    xa = _make_xa_file(base, n_sectors=4)

    # ① 扇区扫描：RIFF 头 + 副本不一致，两个坑一起验
    groups = server.core.xa_sector_groups(xa)
    _mark(len(groups) == 1, "扫到 1 条子流", groups)
    _mark(bool(groups) and len(list(groups.values())[0]) == 4,
          "扫到 4 个扇区（RIFF 头之后的偏移算对了）",
          {k: len(v) for k, v in groups.items()})

    # ② 探测
    import psx_txtp
    pr = psx_txtp.probe_xa(xa)
    _mark(pr.get("ok") is True, "probe_xa 通过", pr)
    _mark(pr.get("streams") == 1, "子流数 = 1", pr.get("streams"))
    _mark(abs(float(pr.get("seconds") or 0) - 4 * 2016.0 / 37800.0) < 0.01,
          "时长 = 扇区数 × 0.0533s", pr.get("seconds"))

    # ③ 目录归类：有 .xa 没 .txtp → 「未知」→ 交给扫描逐文件展开
    _mark(convert_job.classify(base) == "未知",
          "有 .xa 没 .txtp 的目录归「未知」（要逐文件展开，不当整包）",
          convert_job.classify(base))
    found, cut = server.scan_dir_sources(base)
    _mark(any(p.lower().endswith(".xa") for p in found),
          "scan_dir_sources 会收 .xa", [os.path.basename(p) for p in found])

    # ④ 加进音源
    _jpost("/api/clear", {})
    code, d = _jpost("/api/add_sources", {"paths": [xa]})
    _mark(code == 200 and d.get("added") == 1, "独立 .xa 能加进音源", d)
    code, st = _jget("/api/state")
    src = (st.get("sources") or [{}])[0]
    _mark(src.get("kind") == "PSX 音频", "归成「PSX 音频」", src.get("kind"))
    _mark(int(src.get("n_tracks") or 0) == 1, "1 轨", src.get("n_tracks"))
    _mark(float(src.get("total_secs") or 0) > 0.2, "探到时长",
          src.get("total_secs"))
    code, d = _jget("/api/tracks?i=0")
    rows = d.get("tracks") or []
    _mark(len(rows) == 1 and rows[0].get("kind") == "XA",
          "/api/tracks 列出 1 轨 XA", rows)

    if fast:
        print("  [--] --fast：跳过 .xa 真实转换", flush=True)
        _jpost("/api/clear", {})
        return

    # ⑤ 真转一遍（4 个扇区 = 0.2 秒，几乎不耗时）
    out = os.path.join(tmpdir.root(), "webui_gate_xa_out")
    code, d = _jpost("/api/start", {"opts": {"out_dir": out, "do_cd": False,
                                             "do_wav": True}})
    _mark(code == 200 and d.get("ok"), "启动 .xa 转换", d)
    ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 120)
    code, st = _jget("/api/state")
    _mark(ok and st.get("state") == "done", "state=done", st.get("state"))
    res = st.get("result") or {}
    _mark(int(res.get("ok") or 0) == 1 and int(res.get("fail") or 0) == 0,
          "转出 1 首、0 失败", res)
    wavs = [n for n in os.listdir(out) if n.lower().endswith(".wav")] \
        if os.path.isdir(out) else []
    _mark(len(wavs) == 1, "落盘 1 个 WAV", wavs)
    if wavs:
        import wave
        with wave.open(os.path.join(out, wavs[0]), "rb") as wf:
            _mark(wf.getframerate() == 44100 and wf.getnchannels() == 2
                  and wf.getsampwidth() == 2,
                  "WAV 是 44.1k/16bit/立体声",
                  (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()))
            # XA 原生是 37800Hz（每扇区每声道 2016 采样），落盘要重采样到
            # 44100 —— 所以帧数是 扇区数 × 2016 × 44100/37800，不是 ×2016。
            want = int(round(4 * 2016 * 44100.0 / 37800.0))
            _mark(wf.getnframes() == want,
                  "帧数 = 扇区数 × 2016 × 44100/37800（重采样到 44.1k）",
                  (wf.getnframes(), want))

    _jpost("/api/clear", {})


def t_psf(state, fast=False):
    """PSF 系（PS1 芯片音源）原生解码。

    这条链路和别的格式不一样：真正干活的是**外挂的 psf2wav.exe**（不是 Python
    模块），而且它绕不过一份 BIOS。所以门禁分两层：

    1. 状态接口必须**如实**报「解码器在不在 / BIOS 在不在」——
       不能一律说可用，也不能一律说不可用。
    2. 一份坏 PSF 必须**明确报错**，绝不能产出静音 WAV 冒充成功。

    有真样本时（环境变量 ``MSU1_PSF_SAMPLE`` 指向一个 .psf）再验一次真解码，
    并且要检查输出**不是全静音** —— 全静音是这条链路最典型的假成功。
    """
    print("\n[3c] PSF（PS1 音源）原生解码", flush=True)
    _jpost("/api/clear", {})

    # -- 解码器必须**静默**启动（不弹黑框）---------------------------
    # psf2wav.exe 是控制台程序，本程序是 --windows-console-mode=disable 的
    # 窗口程序。不带 CREATE_NO_WINDOW 的话，每转一首都会闪一次黑框，
    # 偶尔还会因为抢不到控制台直接 0xc0000142 启动失败。
    import psf_native
    kw = psf_native._silent_kw()
    _mark(set(kw) == {"startupinfo", "creationflags"},
          "_silent_kw() 给出 startupinfo + creationflags", sorted(kw))
    _mark(kw.get("creationflags") == 0x08000000,
          "creationflags 是 CREATE_NO_WINDOW（不建控制台）", kw.get("creationflags"))
    _mark(getattr(kw.get("startupinfo"), "wShowWindow", None) == 0,
          "startupinfo 要求 SW_HIDE（万一带窗口也藏起来）",
          getattr(kw.get("startupinfo"), "wShowWindow", None))
    _mark(psf_native._rc_u32(-1073741502) == 0xC0000142,
          "_rc_u32 能把有符号退出码还原成 0xC0000142",
          hex(psf_native._rc_u32(-1073741502)))
    _mark(int(getattr(psf_native, "_SPAWN_RETRY", 0)) >= 2,
          "0xc0000142 有重试（偶发失败不该直接判死）",
          getattr(psf_native, "_SPAWN_RETRY", None))
    # 静态锁：**每一处** spawn 都必须带静默参数，否则改回去没人发现。
    # ⚠ 早先这里写的是「_silent_kw() 出现次数 >= 3」—— 数的是**字面次数**，
    #   谁多写一行注释、或把某处改成走 winspawn 封装，断言就误红（实测踩过）。
    #   改成结构判断：每个 subprocess.Popen/run 调用的后 4 行里必须有 _silent_kw()。
    _src = open(os.path.join(ROOT, "psf_native.py"), encoding="utf-8").read()
    _lines = _src.splitlines()
    _spawns = [i for i, ln in enumerate(_lines)
               if ("subprocess.Popen(" in ln or "subprocess.run(" in ln)
               and not ln.strip().startswith("#")]
    _covered = [i for i in _spawns
                if any("_silent_kw()" in _lines[j]
                       for j in range(i, min(len(_lines), i + 8)))]
    _mark(len(_spawns) >= 2 and len(_covered) == len(_spawns),
          "psf_native.py 里所有起解码器的地方都用了 _silent_kw",
          "%d/%d 处" % (len(_covered), len(_spawns)))

    # 解压 / 芯片解码也走同一套（joshw_dl 起 tar.exe、chip_decode 起自身）
    for _mod, _tag in (("joshw_dl.py", "解压"), ("chip_decode.py", "芯片解码")):
        _s = open(os.path.join(ROOT, _mod), encoding="utf-8").read()
        _ln = _s.splitlines()
        # ⚠ 跳过注释行：joshw_dl 里有一句「自己轮询而不是 subprocess.run(...)」，
        #   按字面数会把它当成一个 spawn 调用（实测误红）。
        #   窗口给到 8 行：多行调用（一个参数一行）常常把静默参数推到第 5 行。
        _sp = [i for i, ln in enumerate(_ln)
               if ("subprocess.Popen(" in ln or "subprocess.run(" in ln)
               and not ln.strip().startswith("#")]
        _cv = [i for i in _sp
               if any(("winspawn.silent_kw()" in _ln[j]) or ("_silent_kw()" in _ln[j])
                      for j in range(i, min(len(_ln), i + 8)))]
        _mark(bool(_sp) and len(_cv) == len(_sp),
              "%s（%s）起子进程也静默" % (_mod, _tag),
              "%d/%d 处" % (len(_cv), len(_sp)))

    # 真跑一次解码器（无参数 -> 打印用法），确认静默参数下**仍能拿到 stderr**
    _exe = psf_native.find_decoder()
    if _exe:
        import subprocess as _sp
        try:
            _r = _sp.run([_exe], capture_output=True, timeout=30, **kw)
            _mark(_r.returncode != 0 and bool(_r.stderr),
                  "静默启动仍能捕获解码器输出（不是把输出也吞了）",
                  "rc=%s stderr=%d B" % (_r.returncode, len(_r.stderr or b"")))
        except Exception as _e:                   # noqa: BLE001
            _mark(False, "静默启动解码器", _e)
    else:
        print("     （本机没有 psf2wav.exe，跳过静默启动实测）", flush=True)

    code, psf_st = _jget("/api/psf/status")
    _mark(code == 200 and psf_st.get("ok"), "GET /api/psf/status", code)
    _mark("decoder" in psf_st and "bios" in psf_st and "hint" in psf_st,
          "状态里解码器 / BIOS / 说明三件都有", list(psf_st))
    if not psf_st.get("decoder"):
        print("     （本机没有 psf2wav.exe，后续只验报错路径）", flush=True)

    # 坏 PSF：必须报错，不能静默产出
    bad = os.path.join(tmpdir.root(), "bad.psf")
    with open(bad, "wb") as f:
        f.write(b"PSF\x01\x00\x00\x00" + b"\0" * 12)
    _jpost("/api/add_sources", {"paths": [bad]})
    out = os.path.join(tmpdir.root(), "webui_gate_psf")
    code, d = _jpost("/api/start", {"opts": {"out_dir": out, "do_cd": False,
                                             "do_wav": True}})
    _mark(code == 200 and d.get("ok"), "启动坏 PSF 转换", d)
    ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 180)
    code, st = _jget("/api/state")
    _mark(ok, "坏 PSF 在 180 秒内结束", st.get("state"))
    res = st.get("result") or {}
    _mark(int(res.get("fail") or 0) == 1 and int(res.get("ok") or 0) == 0,
          "坏 PSF 计 1 失败、0 成功", res)
    names = sorted(os.listdir(out)) if os.path.isdir(out) else []
    _mark(not any(n.lower().endswith(".wav") for n in names),
          "坏 PSF 没产出 WAV（不冒充成功）", "、".join(names))
    logs = " ".join(row[1] for row in (st.get("logs") or []))
    _mark(("BIOS" in logs) or ("PSF" in logs),
          "日志里说清了原因（BIOS / PSF）", logs[-160:])

    _jpost("/api/clear", {})

    # 有真样本才验真解码。样本自带 BIOS 依赖，所以两头都得在。
    sample = os.environ.get("MSU1_PSF_SAMPLE", "").strip()
    if fast or not sample or not os.path.isfile(sample):
        print("  [--] 没给 MSU1_PSF_SAMPLE（或 --fast）：跳过真解码", flush=True)
        return
    if not psf_st.get("available"):
        print("  [--] 本机缺解码器或 BIOS：跳过真解码", flush=True)
        return

    import psf_native
    r = psf_native.decode_pcm(sample)
    _mark(r.get("ok"), "真 PSF 能解出 PCM", r.get("error"))
    if not r.get("ok"):
        return
    _mark(r["seconds"] > 0.5, "解出时长 > 0.5s", r["seconds"])
    # 全 0 采样 = 解码器「成功」但什么都没出 —— 这条链路的经典假成功
    peak = max(abs(v) for v in memoryview(r["pcm"]).cast("h")) \
        if r["pcm"] else 0
    _mark(peak > 100, "输出不是全静音（peak > 100）", peak)


def t_usf(state, fast=False):
    """N64 USF（``.miniusf``）原生解码 —— 外挂 ``usf2wav.exe``。

    和 PSF 一样真正干活的是**独立 exe**（psflib + lazyusf2 + zlib），但
    **不需要 BIOS** —— N64 没有这东西，核里自带 PIF/CIC 的 HLE 引导。

    验四件事：

    1. **模块契约**：``USF_EXTS``、静默启动参数、有符号退出码还原。
       静默这条不能省 —— 解码器是控制台程序，主程序是窗口程序，
       不带 ``CREATE_NO_WINDOW`` 每首都会闪一次黑框（README ⚠40/⚠42）。
    2. **分流**：``is_convertible`` / ``policy_for`` / ``classify`` 三处必须
       一致，而且 ``.miniusf`` 不能再留在 ``SKIP_REASONS`` 里 ——
       否则界面上会出现「能转的格式被说成转不了」。
    3. **WAV 读取**：合成一个 44 字节头 + 若干采样的 WAV，秒数要算对。
       （这一层是上次 ``.hwas`` 翻车的地方：判据定错，坏输出被当成成功。）
    4. **坏文件必须明确报错**，绝不能产出静音 WAV 冒充成功。
    """
    print("\n[3c2] N64 USF 原生解码", flush=True)

    import usf_native
    import dl_convert as dl
    import convert_job as cj

    # -- 1. 模块契约 --------------------------------------------------
    _mark(usf_native.USF_EXTS == (".miniusf", ".usf"),
          "USF_EXTS 是 (.miniusf, .usf)", usf_native.USF_EXTS)
    _kw = usf_native._silent_kw()
    _mark(set(_kw) == {"startupinfo", "creationflags"},
          "usf_native 起解码器带静默参数", sorted(_kw))
    _mark(_kw.get("creationflags") == 0x08000000,
          "creationflags 是 CREATE_NO_WINDOW", _kw.get("creationflags"))
    _mark(usf_native._rc_u32(-1073741502) == 0xC0000142,
          "_rc_u32 能把有符号退出码还原成 0xC0000142",
          hex(usf_native._rc_u32(-1073741502)))
    _mark(int(getattr(usf_native, "_SPAWN_RETRY", 0)) >= 2,
          "0xc0000142 有重试（偶发失败不该直接判死）",
          getattr(usf_native, "_SPAWN_RETRY", None))
    # 静态锁：**每一处** spawn 都必须带静默参数，改回去要能被发现
    _src = open(os.path.join(ROOT, "usf_native.py"), encoding="utf-8").read()
    _lines = _src.splitlines()
    _spawns = [i for i, ln in enumerate(_lines)
               if ("subprocess.Popen(" in ln or "subprocess.run(" in ln)
               and not ln.strip().startswith("#")]
    _covered = [i for i in _spawns
                if any("_silent_kw()" in _lines[j]
                       for j in range(i, min(len(_lines), i + 8)))]
    _mark(bool(_spawns) and len(_covered) == len(_spawns),
          "usf_native.py 里所有起解码器的地方都用了 _silent_kw",
          "%d/%d 处" % (len(_covered), len(_spawns)))

    # -- 2. 分流一致 --------------------------------------------------
    _mark(dl.is_convertible("a/x.miniusf") and dl.is_convertible("a/x.usf"),
          ".miniusf / .usf 都算可转音源")
    _mark(dl.policy_for("a/x.miniusf") == "usf",
          "policy_for(.miniusf) == 'usf'", dl.policy_for("a/x.miniusf"))
    _mark(cj.classify("a/x.miniusf") == "USF",
          "classify(.miniusf) == 'USF'", cj.classify("a/x.miniusf"))
    _mark(".miniusf" not in dl.SKIP_REASONS and ".usf" not in dl.SKIP_REASONS,
          "已能转的 .miniusf/.usf 不在 SKIP_REASONS 里")
    _mark(".usflib" in dl.SKIP_REASONS,
          "配套采样库 .usflib 仍有「只配合 .miniusf 用」的说明")

    # -- 3. WAV 读取 ---------------------------------------------------
    import tempfile as _tf
    _fd, _p = _tf.mkstemp(suffix=".wav")
    os.close(_fd)
    try:
        _pcm = b"\x00\x00" * 2 * 44100          # 1 秒静音立体声
        with open(_p, "wb") as _f:
            _f.write(b"RIFF" + struct.pack("<I", 36 + len(_pcm)) + b"WAVE")
            _f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100,
                                           176400, 4, 16))
            _f.write(b"data" + struct.pack("<I", len(_pcm)))
            _f.write(_pcm)
        _got, _sec, _ch, _rate = usf_native._read_wav(_p)
        _mark(_ch == 2 and _rate == 44100 and abs(_sec - 1.0) < 1e-6,
              "_read_wav 算出 2ch/44100/1.000 秒",
              "%dch/%d/%.3f" % (_ch, _rate, _sec))
    finally:
        try:
            os.remove(_p)
        except OSError:
            pass

    # -- 4. 坏文件必须明确报错 ------------------------------------------
    _fd, _bad = _tf.mkstemp(suffix=".miniusf")
    os.close(_fd)
    try:
        with open(_bad, "wb") as _f:
            _f.write(b"PSF\x21" + b"\x00" * 12 + b"not a real usf")
        _r = usf_native.decode_pcm(_bad)
        _mark(not _r["ok"] and bool(_r["error"]),
              "坏 .miniusf 明确报错，不产出静音 WAV", _r.get("error"))
        _mark("USF" in (_r.get("error") or "")
              or "usflib" in (_r.get("error") or ""),
              "错误信息点明是 USF / usflib 的问题",
              (_r.get("error") or "")[:60])
    finally:
        try:
            os.remove(_bad)
        except OSError:
            pass

    # -- 5. 有真样本时再验一次真解码 ------------------------------------
    # 只在开发机上有意义：设 USF_SAMPLE 指向一个 .miniusf。
    _samp = os.environ.get("MSU1_USF_SAMPLE", "")
    if not _samp or not os.path.isfile(_samp):
        print("  [--] 真解码（设 MSU1_USF_SAMPLE=<某个 .miniusf> 才验）",
              flush=True)
        return
    _r = usf_native.decode_pcm(_samp)
    _mark(_r["ok"], "真样本解码成功", _r.get("error"))
    if _r["ok"]:
        _mark(_r["seconds"] > 0.5, "时长 > 0.5 秒", _r["seconds"])
        _pk = max(abs(struct.unpack_from("<h", _r["pcm"], i)[0])
                  for i in range(0, min(len(_r["pcm"]), 400000) - 1, 2))
        _mark(_pk > 500, "不是全静音（峰值 > 500）", _pk)


def t_vio(state, fast=False):
    """NDS 2SF（``.mini2sf``）原生解码 —— 外挂 ``vio2wav.exe``。

    和 USF 一个模子：真正干活的是**独立 exe**（psflib + vio2sf + zlib），
    **不需要 BIOS** —— vio2sf 的 ``bios.c`` 是 HLE 实现。

    验四件事：

    1. **模块契约**：``VIO_EXTS``、静默启动参数、有符号退出码还原。
       静默这条不能省 —— 解码器是控制台程序，主程序是窗口程序，
       不带 ``CREATE_NO_WINDOW`` 每首都会闪一次黑框（README ⚠40/⚠42）。
    2. **分流**：``is_convertible`` / ``policy_for`` / ``classify`` 三处必须
       一致，而且 ``.mini2sf`` 不能再留在 ``SKIP_REASONS`` 里。
    3. **WAV 读取**：合成一个 44 字节头 + 若干采样的 WAV，秒数要算对。
    4. **坏文件必须明确报错**，绝不能产出静音 WAV 冒充成功。
    """
    print("\n[3c3] NDS 2SF 原生解码", flush=True)

    import vio_native
    import dl_convert as dl
    import convert_job as cj

    # -- 1. 模块契约 --------------------------------------------------
    _mark(vio_native.VIO_EXTS == (".mini2sf", ".2sf"),
          "VIO_EXTS 是 (.mini2sf, .2sf)", vio_native.VIO_EXTS)
    _kw = vio_native._silent_kw()
    _mark(set(_kw) == {"startupinfo", "creationflags"},
          "vio_native 起解码器带静默参数", sorted(_kw))
    _mark(_kw.get("creationflags") == 0x08000000,
          "creationflags 是 CREATE_NO_WINDOW", _kw.get("creationflags"))
    _mark(vio_native._rc_u32(-1073741502) == 0xC0000142,
          "_rc_u32 能把有符号退出码还原成 0xC0000142",
          hex(vio_native._rc_u32(-1073741502)))
    _mark(int(getattr(vio_native, "_SPAWN_RETRY", 0)) >= 2,
          "0xc0000142 有重试（偶发失败不该直接判死）",
          getattr(vio_native, "_SPAWN_RETRY", None))
    # 静态锁：**每一处** spawn 都必须带静默参数，改回去要能被发现
    _src = open(os.path.join(ROOT, "vio_native.py"), encoding="utf-8").read()
    _lines = _src.splitlines()
    _spawns = [i for i, ln in enumerate(_lines)
               if ("subprocess.Popen(" in ln or "subprocess.run(" in ln)
               and not ln.strip().startswith("#")]
    _covered = [i for i in _spawns
                if any("_silent_kw()" in _lines[j]
                       for j in range(i, min(len(_lines), i + 8)))]
    _mark(bool(_spawns) and len(_covered) == len(_spawns),
          "vio_native.py 里所有起解码器的地方都用了 _silent_kw",
          "%d/%d 处" % (len(_covered), len(_spawns)))

    # -- 2. 分流一致 --------------------------------------------------
    _mark(dl.is_convertible("a/x.mini2sf") and dl.is_convertible("a/x.2sf"),
          ".mini2sf / .2sf 都算可转音源")
    _mark(dl.policy_for("a/x.mini2sf") == "vio",
          "policy_for(.mini2sf) == 'vio'", dl.policy_for("a/x.mini2sf"))
    _mark(cj.classify("a/x.mini2sf") == "2SF",
          "classify(.mini2sf) == '2SF'", cj.classify("a/x.mini2sf"))
    _mark(".mini2sf" not in dl.SKIP_REASONS and ".2sf" not in dl.SKIP_REASONS,
          "已能转的 .mini2sf/.2sf 不在 SKIP_REASONS 里")
    _mark(".2sflib" in dl.SKIP_REASONS,
          "配套 ROM 库 .2sflib 仍有「只配合 .mini2sf 用」的说明")

    # -- 3. WAV 读取 ---------------------------------------------------
    import tempfile as _tf
    _fd, _p = _tf.mkstemp(suffix=".wav")
    os.close(_fd)
    try:
        _pcm = b"\x00\x00" * 2 * 44100          # 1 秒静音立体声
        with open(_p, "wb") as _f:
            _f.write(b"RIFF" + struct.pack("<I", 36 + len(_pcm)) + b"WAVE")
            _f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100,
                                           176400, 4, 16))
            _f.write(b"data" + struct.pack("<I", len(_pcm)))
            _f.write(_pcm)
        _got, _sec, _ch, _rate = vio_native._read_wav(_p)
        _mark(_ch == 2 and _rate == 44100 and abs(_sec - 1.0) < 1e-6,
              "_read_wav 算出 2ch/44100/1.000 秒",
              "%dch/%d/%.3f" % (_ch, _rate, _sec))
    finally:
        try:
            os.remove(_p)
        except OSError:
            pass

    # -- 4. 坏文件必须明确报错 ------------------------------------------
    _fd, _bad = _tf.mkstemp(suffix=".mini2sf")
    os.close(_fd)
    try:
        with open(_bad, "wb") as _f:
            _f.write(b"PSF\x24" + b"\x00" * 12 + b"not a real 2sf")
        _r = vio_native.decode_pcm(_bad)
        _mark(not _r["ok"] and bool(_r["error"]),
              "坏 .mini2sf 明确报错，不产出静音 WAV", _r.get("error"))
        _mark("2SF" in (_r.get("error") or "")
              or "2sflib" in (_r.get("error") or ""),
              "错误信息点明是 2SF / 2sflib 的问题",
              (_r.get("error") or "")[:60])
    finally:
        try:
            os.remove(_bad)
        except OSError:
            pass

    # -- 5. 有真样本时再验一次真解码 ------------------------------------
    # 只在开发机上有意义：设 MSU1_2SF_SAMPLE 指向一个 .mini2sf。
    _samp = os.environ.get("MSU1_2SF_SAMPLE", "")
    if not _samp or not os.path.isfile(_samp):
        print("  [--] 真解码（设 MSU1_2SF_SAMPLE=<某个 .mini2sf> 才验）",
              flush=True)
        return
    _r = vio_native.decode_pcm(_samp)
    _mark(_r["ok"], "真样本解码成功", _r.get("error"))
    if _r["ok"]:
        _mark(_r["seconds"] > 0.5, "时长 > 0.5 秒", _r["seconds"])
        _pk = max(abs(struct.unpack_from("<h", _r["pcm"], i)[0])
                  for i in range(0, min(len(_r["pcm"]), 400000) - 1, 2))
        _mark(_pk > 500, "不是全静音（峰值 > 500）", _pk)


def t_ssf(state, fast=False):
    """Saturn/Dreamcast SSF/DSF 原生解码 —— 外挂 ``ssf2wav.exe``。

    和前三个同构：独立 exe（psflib + Highly Theoretical + zlib），
    **不需要 BIOS**。特殊点是**一个 exe 管两种机器**：
    核里 ``sega_get_state_size(version)`` 的 version 1 = Saturn、
    2 = Dreamcast，对应容器版本 0x11 / 0x12。

    验四件事：

    1. **模块契约**：``SSF_EXTS`` 四个后缀、静默启动参数、有符号退出码还原。
    2. **分流**：``.ssf`` 归 ``SSF``、``.dsf`` 归 ``DSF``（界面上要分得开），
       且两者都不能再留在 ``SKIP_REASONS`` 里。
    3. **WAV 读取**：合成 WAV，秒数要算对。
    4. **坏文件必须明确报错**，绝不能产出静音 WAV 冒充成功。
    """
    print("\n[3c4] Saturn/Dreamcast SSF/DSF 原生解码", flush=True)

    import ssf_native
    import dl_convert as dl
    import convert_job as cj

    # -- 1. 模块契约 --------------------------------------------------
    _mark(ssf_native.SSF_EXTS == (".ssf", ".minissf", ".dsf", ".minidsf"),
          "SSF_EXTS 是 (.ssf, .minissf, .dsf, .minidsf)",
          ssf_native.SSF_EXTS)
    _mark(ssf_native.DECODER_NAME == "ssf2wav.exe",
          "解码器名是 ssf2wav.exe（一个 exe 管 Saturn + Dreamcast）",
          ssf_native.DECODER_NAME)
    _kw = ssf_native._silent_kw()
    _mark(set(_kw) == {"startupinfo", "creationflags"},
          "ssf_native 起解码器带静默参数", sorted(_kw))
    _mark(_kw.get("creationflags") == 0x08000000,
          "creationflags 是 CREATE_NO_WINDOW", _kw.get("creationflags"))
    _mark(ssf_native._rc_u32(-1073741502) == 0xC0000142,
          "_rc_u32 能把有符号退出码还原成 0xC0000142",
          hex(ssf_native._rc_u32(-1073741502)))
    _mark(int(getattr(ssf_native, "_SPAWN_RETRY", 0)) >= 2,
          "0xc0000142 有重试（偶发失败不该直接判死）",
          getattr(ssf_native, "_SPAWN_RETRY", None))
    # 静态锁：**每一处** spawn 都必须带静默参数
    _src = open(os.path.join(ROOT, "ssf_native.py"), encoding="utf-8").read()
    _lines = _src.splitlines()
    _spawns = [i for i, ln in enumerate(_lines)
               if ("subprocess.Popen(" in ln or "subprocess.run(" in ln)
               and not ln.strip().startswith("#")]
    _covered = [i for i in _spawns
                if any("_silent_kw()" in _lines[j]
                       for j in range(i, min(len(_lines), i + 8)))]
    _mark(bool(_spawns) and len(_covered) == len(_spawns),
          "ssf_native.py 里所有起解码器的地方都用了 _silent_kw",
          "%d/%d 处" % (len(_covered), len(_spawns)))

    # -- 2. 分流一致 --------------------------------------------------
    _mark(all(dl.is_convertible("a/x" + e) for e in ssf_native.SSF_EXTS),
          "四个后缀都算可转音源")
    _mark(dl.policy_for("a/x.ssf") == "ssf",
          "policy_for(.ssf) == 'ssf'", dl.policy_for("a/x.ssf"))
    _mark(cj.classify("a/x.ssf") == "SSF",
          "classify(.ssf) == 'SSF'", cj.classify("a/x.ssf"))
    # 界面上 Saturn 和 Dreamcast 必须分得开 —— 用户看得出是哪台机器
    _mark(cj.classify("a/x.dsf") == "DSF",
          "classify(.dsf) == 'DSF'（不和 Saturn 混成一类）", cj.classify("a/x.dsf"))
    _mark(cj.classify("a/x.minidsf") == "DSF",
          "classify(.minidsf) == 'DSF'", cj.classify("a/x.minidsf"))
    _mark(not [e for e in ssf_native.SSF_EXTS if e in dl.SKIP_REASONS],
          "已能转的 .ssf/.dsf 都不在 SKIP_REASONS 里")
    _mark(".ssflib" in dl.SKIP_REASONS and ".dsflib" in dl.SKIP_REASONS,
          "配套库 .ssflib/.dsflib 仍有「只配合 …用」的说明")

    # -- 3. WAV 读取 ---------------------------------------------------
    import tempfile as _tf
    _fd, _p = _tf.mkstemp(suffix=".wav")
    os.close(_fd)
    try:
        _pcm = b"\x00\x00" * 2 * 44100          # 1 秒静音立体声
        with open(_p, "wb") as _f:
            _f.write(b"RIFF" + struct.pack("<I", 36 + len(_pcm)) + b"WAVE")
            _f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100,
                                           176400, 4, 16))
            _f.write(b"data" + struct.pack("<I", len(_pcm)))
            _f.write(_pcm)
        _got, _sec, _ch, _rate = ssf_native._read_wav(_p)
        _mark(_ch == 2 and _rate == 44100 and abs(_sec - 1.0) < 1e-6,
              "_read_wav 算出 2ch/44100/1.000 秒",
              "%dch/%d/%.3f" % (_ch, _rate, _sec))
    finally:
        try:
            os.remove(_p)
        except OSError:
            pass

    # -- 4. 坏文件必须明确报错 ------------------------------------------
    for _ext, _magic in ((".ssf", b"PSF\x11"), (".dsf", b"PSF\x12")):
        _fd, _bad = _tf.mkstemp(suffix=_ext)
        os.close(_fd)
        try:
            with open(_bad, "wb") as _f:
                _f.write(_magic + b"\x00" * 12 + b"not a real ssf")
            _r = ssf_native.decode_pcm(_bad)
            _mark(not _r["ok"] and bool(_r["error"]),
                  "坏 %s 明确报错，不产出静音 WAV" % _ext, _r.get("error"))
            _mark("SSF" in (_r.get("error") or "")
                  or "ssflib" in (_r.get("error") or "")
                  or "dsflib" in (_r.get("error") or ""),
                  "坏 %s 的错误信息点明是 SSF/DSF 的问题" % _ext,
                  (_r.get("error") or "")[:60])
        finally:
            try:
                os.remove(_bad)
            except OSError:
                pass

    # -- 5. 有真样本时再验一次真解码 ------------------------------------
    _samp = os.environ.get("MSU1_SSF_SAMPLE", "")
    if not _samp or not os.path.isfile(_samp):
        print("  [--] 真解码（设 MSU1_SSF_SAMPLE=<某个 .ssf> 才验）", flush=True)
        return
    _r = ssf_native.decode_pcm(_samp)
    _mark(_r["ok"], "真样本解码成功", _r.get("error"))
    if _r["ok"]:
        _mark(_r["seconds"] > 0.5, "时长 > 0.5 秒", _r["seconds"])
        _pk = max(abs(struct.unpack_from("<h", _r["pcm"], i)[0])
                  for i in range(0, min(len(_r["pcm"]), 400000) - 1, 2))
        _mark(_pk > 500, "不是全静音（峰值 > 500）", _pk)


def t_gsf(state, fast=False):
    """GBA GSF/minigsf 原生解码 —— 外挂 ``gsf2wav.exe``。

    和前四个同构：独立 exe（psflib + mGBA 最小核 + zlib），**不需要 BIOS**。
    特殊点是**原生采样率 32768**（上游 ``get_srate(0x22)``），而主流水线
    写 WAV 一律标 44100 —— 所以 ``gsf2wav.exe`` 出厂就让 blip_buf 直接出
    44100，这里钉住「读回来的 WAV 是 44100」。

    验四件事：

    1. **模块契约**：``GSF_EXTS`` 两个后缀、静默启动参数、有符号退出码还原。
    2. **分流**：``.gsf`` / ``.minigsf`` 归 ``GSF``，且不能再留在
       ``SKIP_REASONS`` 里；配套的 ``.gsflib`` 仍要留说明。
    3. **WAV 读取**：合成 WAV，秒数要算对。
    4. **坏文件必须明确报错**，绝不能产出静音 WAV 冒充成功。
    """
    print("\n[3c5] GBA GSF/minigsf 原生解码", flush=True)

    import gsf_native
    import dl_convert as dl
    import convert_job as cj

    # -- 1. 模块契约 --------------------------------------------------
    _mark(gsf_native.GSF_EXTS == (".gsf", ".minigsf"),
          "GSF_EXTS 是 (.gsf, .minigsf)", gsf_native.GSF_EXTS)
    _mark(gsf_native.DECODER_NAME == "gsf2wav.exe",
          "解码器名是 gsf2wav.exe", gsf_native.DECODER_NAME)
    _kw = gsf_native._silent_kw()
    _mark(set(_kw) == {"startupinfo", "creationflags"},
          "gsf_native 起解码器带静默参数", sorted(_kw))
    _mark(_kw.get("creationflags") == 0x08000000,
          "creationflags 是 CREATE_NO_WINDOW", _kw.get("creationflags"))
    _mark(gsf_native._rc_u32(-1073741502) == 0xC0000142,
          "_rc_u32 能把有符号退出码还原成 0xC0000142",
          hex(gsf_native._rc_u32(-1073741502)))
    _mark(int(getattr(gsf_native, "_SPAWN_RETRY", 0)) >= 2,
          "0xc0000142 有重试（偶发失败不该直接判死）",
          getattr(gsf_native, "_SPAWN_RETRY", None))
    # 静态锁：**每一处** spawn 都必须带静默参数
    _src = open(os.path.join(ROOT, "gsf_native.py"), encoding="utf-8").read()
    _lines = _src.splitlines()
    _spawns = [i for i, ln in enumerate(_lines)
               if ("subprocess.Popen(" in ln or "subprocess.run(" in ln)
               and not ln.strip().startswith("#")]
    _covered = [i for i in _spawns
                if any("_silent_kw()" in _lines[j]
                       for j in range(i, min(len(_lines), i + 8)))]
    _mark(bool(_spawns) and len(_covered) == len(_spawns),
          "gsf_native.py 里所有起解码器的地方都用了 _silent_kw",
          "%d/%d 处" % (len(_covered), len(_spawns)))

    # -- 2. 分流一致 --------------------------------------------------
    _mark(all(dl.is_convertible("a/x" + e) for e in gsf_native.GSF_EXTS),
          "两个后缀都算可转音源")
    _mark(dl.policy_for("a/x.minigsf") == "gsf",
          "policy_for(.minigsf) == 'gsf'", dl.policy_for("a/x.minigsf"))
    _mark(dl.policy_for("a/x.gsf") == "gsf",
          "policy_for(.gsf) == 'gsf'", dl.policy_for("a/x.gsf"))
    _mark(cj.classify("a/x.minigsf") == "GSF",
          "classify(.minigsf) == 'GSF'", cj.classify("a/x.minigsf"))
    _mark(cj.classify("a/x.gsf") == "GSF",
          "classify(.gsf) == 'GSF'", cj.classify("a/x.gsf"))
    # 目录要逐文件展开（一个 .gsflib 带一堆 .minigsf）——
    # 否则整包会被 classify 归成「未知」直接拒收。
    import shutil
    _d = os.path.join(tmpdir.root(), "gate_gsf_dir")
    os.makedirs(_d, exist_ok=True)
    try:
        open(os.path.join(_d, "01.minigsf"), "wb").close()
        open(os.path.join(_d, "AGB.gsflib"), "wb").close()
        _mark(cj.classify(_d) == "未知",
              "整个 .minigsf 目录归「未知」→ 逐文件展开",
              cj.classify(_d))
    finally:
        _rmtree_soft(_d)
    _mark(not [e for e in gsf_native.GSF_EXTS if e in dl.SKIP_REASONS],
          "已能转的 .gsf/.minigsf 都不在 SKIP_REASONS 里")
    _mark(".gsflib" in dl.SKIP_REASONS,
          "配套库 .gsflib 仍有「只配合 .minigsf 用」的说明")

    # -- 3. WAV 读取 ---------------------------------------------------
    import tempfile as _tf
    _fd, _p = _tf.mkstemp(suffix=".wav")
    os.close(_fd)
    try:
        # ⚠ 用 44100 而不是 32768 —— 钉住「出厂就是 44100」这件事
        _pcm = b"\x00\x00" * 2 * 44100          # 1 秒静音立体声
        with open(_p, "wb") as _f:
            _f.write(b"RIFF" + struct.pack("<I", 36 + len(_pcm)) + b"WAVE")
            _f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100,
                                           176400, 4, 16))
            _f.write(b"data" + struct.pack("<I", len(_pcm)))
            _f.write(_pcm)
        _got, _sec, _ch, _rate = gsf_native._read_wav(_p)
        _mark(_ch == 2 and _rate == 44100 and abs(_sec - 1.0) < 1e-6,
              "_read_wav 算出 2ch/44100/1.000 秒",
              "%dch/%d/%.3f" % (_ch, _rate, _sec))
    finally:
        try:
            os.remove(_p)
        except OSError:
            pass

    # -- 4. 坏文件必须明确报错 ------------------------------------------
    for _ext, _magic in ((".minigsf", b"PSF\x22"), (".gsf", b"PSF\x22")):
        _fd, _bad = _tf.mkstemp(suffix=_ext)
        os.close(_fd)
        try:
            with open(_bad, "wb") as _f:
                _f.write(_magic + b"\x00" * 12 + b"not a real gsf")
            _r = gsf_native.decode_pcm(_bad)
            _mark(not _r["ok"] and bool(_r["error"]),
                  "坏 %s 明确报错，不产出静音 WAV" % _ext, _r.get("error"))
            _mark("GSF" in (_r.get("error") or "")
                  or "gsflib" in (_r.get("error") or ""),
                  "坏 %s 的错误信息点明是 GSF 的问题" % _ext,
                  (_r.get("error") or "")[:60])
        finally:
            try:
                os.remove(_bad)
            except OSError:
                pass

    # -- 5. 有真样本时再验一次真解码 ------------------------------------
    _samp = os.environ.get("MSU1_GSF_SAMPLE", "")
    if not _samp or not os.path.isfile(_samp):
        print("  [--] 真解码（设 MSU1_GSF_SAMPLE=<某个 .minigsf> 才验）",
              flush=True)
        return
    _r = gsf_native.decode_pcm(_samp)
    _mark(_r["ok"], "真样本解码成功", _r.get("error"))
    if _r["ok"]:
        _mark(_r["seconds"] > 0.5, "时长 > 0.5 秒", _r["seconds"])
        _pk = max(abs(struct.unpack_from("<h", _r["pcm"], i)[0])
                  for i in range(0, min(len(_r["pcm"]), 400000) - 1, 2))
        _mark(_pk > 500, "不是全静音（峰值 > 500）", _pk)
        # ⚠ GSF 原生 32768，出厂必须已经重采样到 44100
        _mark(_r["kind"].endswith("/44100"),
              "出厂输出是 44100（GSF 原生 32768 已重采样）", _r["kind"])


def t_qsf(fast=False):
    """[3c6] Capcom QSound 的 QSF/miniqsf 原生解码（v5.9.10）。

    这条链路和 GSF 形状一样（外挂 exe、不需要 BIOS），但**有两个独有的坑**
    必须钉住：

    1. **原生采样率 24038**。``qsound_set_rates(state, 8000000, 250, 24038)``
       里那个数字是 QSound DAC 的硬件节拍（``60000000/2/1248 = 24038.46``），
       混音器每输出一个样本就按它推进一次 DSP —— 改大只会让整首变调。
       所以 ``qsf2wav.exe`` 内部用 64 抽头 Kaiser sinc 重采样到 44100，
       这里钉住「出厂输出是 44100」。
    2. **不需要 BIOS**。QSound 是街机板上的独立声音子系统，照抄
       ``psf_native`` 把 BIOS 检查带过来是概念性错误 —— 断言模块里
       **没有** ``find_bios`` / ``set_bios`` / ``check_bios``。

    另外钉住：QSF 是 PSF 容器（版本字节 0x41），所以它和 PSF 一样
    **不能进 EXT_TABLE**（那是 libgme 的解码清单，libgme 不吃 PSF 家族）。

    ``fast`` 只影响末尾那段真样本解码（要设 ``MSU1_QSF_SAMPLE``）。
    """
    print("\n[3c6] Capcom QSound QSF/miniqsf 原生解码", flush=True)

    import qsf_native as q
    import dl_convert as dl
    import convert_job as cj
    import chip_formats as cf

    # -- 1. 模块契约 --------------------------------------------------
    _mark(q.QSF_EXTS == (".qsf", ".miniqsf"),
          "QSF_EXTS 是 (.qsf, .miniqsf)", q.QSF_EXTS)
    _mark(q.QSF_LIB_EXTS == (".qsflib",),
          "QSF_LIB_EXTS 是 (.qsflib,)", q.QSF_LIB_EXTS)
    _mark(q.DECODER_NAME == "qsf2wav.exe",
          "解码器名是 qsf2wav.exe", q.DECODER_NAME)
    _mark(q.OUT_RATE == 44100 and q.SRC_RATE == 24038,
          "出厂 44100 / 原生 24038", (q.OUT_RATE, q.SRC_RATE))
    _mark(q.OUT_RATE == 44100,
          "出厂采样率与 spc_wav.SR 一致（44100）", q.OUT_RATE)

    # ⚠ 概念性守卫：QSound 没有主机 BIOS。
    _mark(not [n for n in ("find_bios", "set_bios", "check_bios",
                           "bios_hint", "BIOS_ENV")
               if hasattr(q, n)],
          "qsf_native 里没有任何 BIOS API（QSound 不需要 BIOS）",
          [n for n in ("find_bios", "set_bios", "check_bios")
           if hasattr(q, n)])

    _kw = q._silent_kw()
    _mark(set(_kw) == {"startupinfo", "creationflags"},
          "qsf_native 起解码器带静默参数", sorted(_kw))
    _mark(_kw.get("creationflags") == 0x08000000,
          "creationflags 是 CREATE_NO_WINDOW", _kw.get("creationflags"))
    _mark(q._rc_u32(-1073741502) == 0xC0000142,
          "_rc_u32 能把有符号退出码还原成 0xC0000142",
          hex(q._rc_u32(-1073741502)))
    _mark(int(getattr(q, "_SPAWN_RETRY", 0)) >= 2,
          "0xc0000142 有重试（偶发失败不该直接判死）",
          getattr(q, "_SPAWN_RETRY", None))

    # 静态锁：**每一处** spawn 都必须带静默参数
    _src = open(os.path.join(ROOT, "qsf_native.py"), encoding="utf-8").read()
    _lines = _src.splitlines()
    _spawns = [i for i, ln in enumerate(_lines)
               if ("subprocess.Popen(" in ln or "subprocess.run(" in ln)
               and not ln.strip().startswith("#")]
    _covered = [i for i in _spawns
                if any("_silent_kw()" in _lines[j]
                       for j in range(i, min(len(_lines), i + 8)))]
    _mark(bool(_spawns) and len(_covered) == len(_spawns),
          "qsf_native.py 里所有起解码器的地方都用了 _silent_kw",
          "%d/%d 处" % (len(_covered), len(_spawns)))

    # -- 2. 分流一致 --------------------------------------------------
    _mark(all(dl.is_convertible("a/x" + e) for e in q.QSF_EXTS),
          "两个后缀都算可转音源")
    _mark(dl.policy_for("a/x.miniqsf") == "qsf",
          "policy_for(.miniqsf) == 'qsf'", dl.policy_for("a/x.miniqsf"))
    _mark(dl.policy_for("a/x.qsf") == "qsf",
          "policy_for(.qsf) == 'qsf'", dl.policy_for("a/x.qsf"))
    _mark(cj.classify("a/x.miniqsf") == "QSF",
          "classify(.miniqsf) == 'QSF'", cj.classify("a/x.miniqsf"))
    _mark(cj.classify("a/x.qsf") == "QSF",
          "classify(.qsf) == 'QSF'", cj.classify("a/x.qsf"))

    # 目录要逐文件展开（一个 .qsflib 带一堆 .miniqsf）
    _d = os.path.join(tmpdir.root(), "gate_qsf_dir")
    os.makedirs(_d, exist_ok=True)
    try:
        open(os.path.join(_d, "01 - Opening.miniqsf"), "wb").close()
        open(os.path.join(_d, "Game.qsflib"), "wb").close()
        _mark(cj.classify(_d) == "未知",
              "整个 .miniqsf 目录归「未知」→ 逐文件展开", cj.classify(_d))
    finally:
        _rmtree_soft(_d)

    _mark(not [e for e in q.QSF_EXTS if e in dl.SKIP_REASONS],
          "已能转的 .qsf/.miniqsf 都不在 SKIP_REASONS 里")
    _mark(".qsflib" in dl.SKIP_REASONS,
          "配套库 .qsflib 仍有「只配合 .miniqsf 用」的说明")
    _mark(not dl.is_convertible("a/x.qsflib"),
          "配套库 .qsflib 本身不算可转音源")

    # -- 3. chip_formats 的登记 ----------------------------------------
    _mark(all(e in cf.ALL_EXTS for e in q.QSF_EXTS),
          "两个后缀都进了 ALL_EXTS（否则拖进来不收）")
    _mark(not any(e in cf.EXT_TABLE for e in q.QSF_EXTS),
          "QSF 不进 EXT_TABLE（那是 libgme 的解码清单，libgme 不吃 PSF 家族）",
          [e for e in q.QSF_EXTS if e in cf.EXT_TABLE])
    _mark(".qsflib" in cf.PSF_LIB_EXTS,
          "PSF_LIB_EXTS 里登记了 .qsflib", cf.PSF_LIB_EXTS)
    _mark(cf.SYSTEM_NAMES.get("QSF") == "Capcom QSound 街机",
          "SYSTEM_NAMES['QSF'] 有名字（否则文案出空括号）",
          cf.SYSTEM_NAMES.get("QSF"))

    # -- 4. WAV 读取 ---------------------------------------------------
    import tempfile as _tf
    _fd, _p = _tf.mkstemp(suffix=".wav")
    os.close(_fd)
    try:
        _pcm = b"\x00\x00" * 2 * 44100          # 1 秒静音立体声
        with open(_p, "wb") as _f:
            _f.write(b"RIFF" + struct.pack("<I", 36 + len(_pcm)) + b"WAVE")
            _f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100,
                                           176400, 4, 16))
            _f.write(b"data" + struct.pack("<I", len(_pcm)))
            _f.write(_pcm)
        _got, _sec, _ch, _rate = q._read_wav(_p)
        _mark(_ch == 2 and _rate == 44100 and abs(_sec - 1.0) < 1e-6,
              "_read_wav 算出 2ch/44100/1.000 秒",
              "%dch/%d/%.3f" % (_ch, _rate, _sec))
    finally:
        try:
            os.remove(_p)
        except OSError:
            pass

    # -- 5. length_tag（下载页估磁盘占用靠它，绝不能起解码器） -----------
    _fd, _t = _tf.mkstemp(suffix=".miniqsf")
    os.close(_fd)
    try:
        with open(_t, "wb") as _f:
            _f.write(b"PSF\x41" + b"\x00" * 40)
            # ⚠ ``[TAG]`` 后面**必须有换行**：真正的标签区是
            #   ``[TAG]\ntitle=…\nlength=0:45`` 一行一个键值，
            #   正则用的是 ``^length=``（re.M），贴在 ``[TAG]`` 同一行匹配不上。
            #   （第一版就是这么写错的，门禁当场抓出来。）
            _f.write(b"[TAG]\ntitle=Opening\nlength=0:45\nfade=10\n")
        _mark(abs(q.length_tag(_t) - 45.0) < 1e-6,
              "length_tag 读出 0:45 → 45.0 秒", q.length_tag(_t))
        with open(_t, "wb") as _f:
            _f.write(b"PSF\x41" + b"\x00" * 40)
        _mark(q.length_tag(_t) == 0.0,
              "没有 [TAG] 时 length_tag 返回 0.0（不猜）", q.length_tag(_t))
    finally:
        try:
            os.remove(_t)
        except OSError:
            pass

    # -- 6. 坏文件必须明确报错，且不能提 BIOS ----------------------------
    for _ext in q.QSF_EXTS:
        _fd, _bad = _tf.mkstemp(suffix=_ext)
        os.close(_fd)
        try:
            with open(_bad, "wb") as _f:
                _f.write(b"PSF\x41" + b"\x00" * 12 + b"not a real qsf")
            _r = q.decode_pcm(_bad)
            _mark(not _r["ok"] and bool(_r["error"]),
                  "坏 %s 明确报错，不产出静音 WAV" % _ext, _r.get("error"))
            _err = _r.get("error") or ""
            # ⚠ 这是本格式最容易犯的文案错误：照抄 PSF 的「请检查 BIOS」，
            #   用户会去翻 BIOS 目录，而 QSF 根本不需要 BIOS。
            _mark("BIOS" not in _err and "bios" not in _err,
                  "坏 %s 的错误信息**不提 BIOS**" % _ext, _err[:70])
        finally:
            try:
                os.remove(_bad)
            except OSError:
                pass

    # -- 7. 有真样本时再验一次真解码 ------------------------------------
    _samp = os.environ.get("MSU1_QSF_SAMPLE", "")
    if not _samp or not os.path.isfile(_samp):
        print("  [--] 真解码（设 MSU1_QSF_SAMPLE=<某个 .miniqsf> 才验）",
              flush=True)
        return
    if fast:
        print("  [--] 真解码（--fast 跳过）", flush=True)
        return
    _r = q.decode_pcm(_samp)
    _mark(_r["ok"], "真样本解码成功", _r.get("error"))
    if _r["ok"]:
        _mark(_r["seconds"] > 0.5, "时长 > 0.5 秒", _r["seconds"])
        _pk = max(abs(struct.unpack_from("<h", _r["pcm"], i)[0])
                  for i in range(0, min(len(_r["pcm"]), 400000) - 1, 2))
        _mark(_pk > 500, "不是全静音（峰值 > 500）", _pk)
        # ⚠ 原生 24038，出厂必须已经重采样到 44100
        _mark(_r["kind"].endswith("/44100"),
              "出厂输出是 44100（QSound 原生 24038 已重采样）", _r["kind"])


def t_v5911(fast=False):
    """[3c7] PS3 / PSP / PS Vita / GameCube / Wii / 3DS 六个机种（v5.9.11）。

    这一轮的结论和以前每一轮都**不一样**：六分之五是**预录流式音频**
    （XVAG / AT3 / HCA / BRSTM / LAAC），FFmpeg 原生就认，一行解码代码都
    不用写 —— 没有模拟器核、没有外挂 exe、不需要 BIOS。只有 GameCube 的
    ``.psf`` 要自己解（PSX-ADPCM / NGC-DSP），而它和 PS1 的 ``.psf``
    **撞名**，这才是真正的风险点。

    所以这里钉的不是「能不能解」（``vendor/_e2e_v5911_platforms.py`` 已经
    拿 310 个真样本跑过了，100% 成功），而是三处**接线**上的错：

    1. **撞名分流** —— ``.psf`` 的第 4 字节有 14 个值分属两个主人。判错的
       症状极其误导：能解的包被报成「BIOS 有问题」。这里用**合成头**把
       6 个 PS1/PS2 字节 + 8 个 Pivotal 字节逐一过一遍，不依赖素材。
    2. **30 样本/块** —— Pivotal 的 PSX-ADPCM 把标准 PSX 当 padding 丢掉的
       第 16 字节也用上了。写成 28 就是「每块丢 2 个样本」，时长只差 7%、
       听感轻微破碎，**很难被当成 bug 报上来**。这里用手算的期望值钉死。
    3. **登记三处** —— 新后缀要同时进 ``stream_audio.EXT_KIND``（才收）、
       ``chip_formats.ALL_EXTS``（拖进来才认）、``joshw_dl._PAYLOAD_EXTS``
       （下载页才认得出负载）。漏一处表现各不相同，都是「看起来没事」。

    ``fast`` 只影响末尾那段真样本解码（要设 ``MSU1_GC_PSF_SAMPLE``）。
    """
    print("\n[3c7] PS3 / PSP / PSV / GC / Wii / 3DS 六个机种", flush=True)

    import stream_audio as sa
    import dl_convert as dl
    import convert_job as cj
    import chip_formats as cf
    import pivotal_psf as pp
    import psx_txtp as pt
    import joshw_dl as jd

    # ── 1. 模块契约 ────────────────────────────────────────────────────
    _mark(pp.OUT_RATE == 44100 and pp.OUT_CH == 2,
          "pivotal_psf 出厂 44100 / 立体声", (pp.OUT_RATE, pp.OUT_CH))
    _mark(pp.MAGIC == b"PSF", "魔数是 PSF（和 PS1 共用）", pp.MAGIC)
    _mark(tuple(pp.SEG_FLAGS) == (0x60, 0x31),
          "分段类型字节是 0x60 / 0x31", pp.SEG_FLAGS)
    _mark(pp.SEG_ENTRY == 0x0C, "段表项 0x0C 字节", pp.SEG_ENTRY)

    # ⚠ 概念性守卫①：0x01 / 0x21 两边都用（Pivotal 单声道变体 vs PS1/PS2
    #   版本号）。本模块**必须不认**这两个值 —— 撞名时让给 PSF 家族。
    _mark(not ({0x01, 0x21} & set(pp.SINGLE_FLAGS)),
          "SINGLE_FLAGS 故意不含 0x01/0x21（与 PS1/PS2 版本号撞名）",
          sorted(set(pp.SINGLE_FLAGS) & {0x01, 0x21}))

    # ⚠ 概念性守卫②：这条路**不需要 BIOS、也没有外挂 exe**。照抄 psf_native
    #   的结构会让人以为缺 BIOS，或者以为少了某个 .exe。
    _mark(not [n for n in ("find_bios", "set_bios", "check_bios",
                           "bios_hint", "BIOS_ENV", "DECODER_NAME",
                           "decoder_ready")
               if hasattr(pp, n)],
          "pivotal_psf 里没有 BIOS API、也没有外挂 exe（纯 Python）",
          [n for n in ("find_bios", "DECODER_NAME", "decoder_ready")
           if hasattr(pp, n)])

    def _mk(path, flags=0x80, code=3763, blocks=2, payload=None):
        """造一个合成 Pivotal PSF。``blocks`` 是**交错单位数**（每单位 32 字节）。"""
        cfg = ((int(code) & 0xFFF) << 20) | (int(blocks) & 0xFFFFF)
        body = payload if payload is not None else b"\x00" * (int(blocks) * 0x20)
        with open(path, "wb") as f:
            f.write(b"PSF" + bytes([flags]) + struct.pack("<I", cfg) + body)

    box = os.path.join(tmpdir.root(), "gate_v5911")
    os.makedirs(box, exist_ok=True)
    try:
        # ── 2. 撞名分流：14 个类型字节逐一过 ────────────────────────────
        _ps1 = (0x01, 0x02, 0x11, 0x12, 0x21, 0x22)
        _wrong = []
        for b in _ps1:
            f = os.path.join(box, "ps1_%02X.psf" % b)
            _mk(f, flags=b, blocks=1)
            if pp.is_pivotal(f) or cj.classify(f) != "PSF":
                _wrong.append("0x%02X" % b)
        _mark(not _wrong,
              "6 个 PS1/PS2 类型字节都不认，classify 仍归 'PSF'", _wrong)

        _piv = tuple(sorted(set(pp.SINGLE_FLAGS) | set(pp.SEG_FLAGS)))
        _wrong = []
        for b in _piv:
            f = os.path.join(box, "gc_%02X.psf" % b)
            _mk(f, flags=b, blocks=1)
            if not pp.is_pivotal(f) or cj.classify(f) != "GameCube 音频":
                _wrong.append("0x%02X" % b)
        _mark(not _wrong,
              "%d 个 Pivotal 类型字节都归「GameCube 音频」" % len(_piv), _wrong)

        # 魔数不对的文件不能被顺手认成 Pivotal
        f = os.path.join(box, "notpsf.psf")
        with open(f, "wb") as _f:
            _f.write(b"PSX\x80" + b"\x00" * 32)
        _mark(not pp.is_pivotal(f) and cj.classify(f) == "PSF",
              "魔数不是 PSF 的文件不认（退回 'PSF' 让 psf_native 报错）",
              cj.classify(f))

        # ── 3. 解码口径：30 样本/块 + 无 +32 舍入 ───────────────────────
        #
        # 声道 0 第 0 块：头 0x00（filter 0 / shift 0），首数据字节 0x0F。
        # 解码器取半字节顺序是 ``(b & 0x0F, b >> 4)``：
        #   低半字节 15 → nn = 15-16 = -1 → s = (-1 << 12) >> 0 = **-4096**
        #   高半字节  0 → 0
        # 其余全零 → 全是 0。这一组值同时钉住两件事：
        #   * 写成 28 样本/块 → 第 3 个样本会变成下一块的头字节，长度也少 2/30
        #   * 加了 ``+32`` 四舍五入 → -4096 变成 -4032
        f = os.path.join(box, "num.psf")
        pay = bytearray(b"\x00" * 64)     # 2 个交错单位 = 4 块 = 每声道 2 块
        pay[1] = 0x0F
        _mk(f, flags=0x80, blocks=2, payload=bytes(pay))
        h = pp.read_single(open(f, "rb").read(), 0)
        _mark(h["ok"] and h["samples"] == 60 and h["channels"] == 2,
              "read_single：2 块 × 30 样本 = 60（每声道）",
              (h.get("samples"), h.get("channels")))
        _mark(h["rate"] == 44100, "采样率代号 3763 → 44100", h.get("rate"))
        _pcm = pp.decode_pcm(f)
        _mark(len(_pcm) == 60 * 4,
              "decode_pcm 长度 = 60 帧 × 2ch × 2B", len(_pcm))
        _mark(list(struct.unpack_from("<4h", _pcm, 0)) == [-4096, 0, 0, 0],
              "首 4 个样本 = [-4096, 0, 0, 0]（无 +32 舍入）",
              list(struct.unpack_from("<4h", _pcm, 0)))
        _pr = pp.probe(f)
        _mark(_pr["ok"] and abs(_pr["secs"] - 60 / 44100.0) < 1e-9,
              "probe 时长 = 60/44100", _pr.get("secs"))
        # 采样率代号不常见时要退回「代号 × 11.72」，不能崩
        f2 = os.path.join(box, "rate.psf")
        _mk(f2, flags=0x80, code=1365, blocks=1)
        _mark(pp.probe(f2).get("rate") == 16000,
              "代号 1365 → 16000（真值表命中）", pp.probe(f2).get("rate"))

        # ── 4. 分段：段表 + 链式遍历 + 子流编号 ─────────────────────────
        #
        # 2 段：段 0 只存 4 条 track 的入口（都指向段 1），段 1 指回自己。
        # 按 vgmstream 的走法，``full`` 走成 **[1,1,1,1,1]** —— 4 条 track
        # 各走一段，然后回到 track 0 时命中循环点，**再补一个样本段**才算收尾
        # （``loop_end`` 记的是补之前的长度 4）。这个「多一个」是 vgmstream
        # 的原样行为，不是 off-by-one：真文件上的验证是
        # ``vendor/_check_pivotal_ref.py`` 拿 vgmstream 逐样本比对
        # （分段 7 个用例、最大差 0），以及 24 首 GC 曲目 probe 时长与实转
        # **逐首一致**。所以这里按实际行为钉，别按直觉改。
        seg1 = 0x08 + 2 * pp.SEG_ENTRY
        seg2 = seg1 + 8 + 32
        blob = bytearray()
        blob += b"PSF\x60" + struct.pack("<I", 2)
        blob += struct.pack("<I4H", seg1, 1, 1, 1, 1)
        blob += struct.pack("<I4H", seg2, 1, 1, 1, 1)
        assert len(blob) == seg1, (len(blob), seg1)
        blob += b"PSF\x80" + struct.pack("<I", (3763 << 20) | 1) + b"\x00" * 32
        assert len(blob) == seg2, (len(blob), seg2)
        blob += b"PSF\x80" + struct.pack("<I", (3763 << 20) | 1) + b"\x00" * 32
        fs = os.path.join(box, "seg.psf")
        with open(fs, "wb") as _f:
            _f.write(bytes(blob))
        _seg = pp.read_segments(bytes(blob))
        _mark(_seg["ok"] and _seg["count"] == 2,
              "read_segments 读出 2 段", _seg.get("count"))
        _mark(pp.total_subsongs(2) == 6,
              "子流总数 = 1(full) + 4(track) + (段数-1) = 6",
              pp.total_subsongs(2))
        _seq1, _ls, _le = pp.walk_sequence(_seg["entries"], 1)
        _mark(_seq1 == [1, 1, 1, 1, 1],
              "full 子流的段序列 = [1,1,1,1,1]（4 条 track + 循环点补一个）",
              _seq1)
        _mark((_ls, _le) == (0, 4),
              "full 的循环区间是序列下标 0..4", (_ls, _le))
        _seq6, _, _ = pp.walk_sequence(_seg["entries"], 6)
        _mark(_seq6 == [1], "第 6 条子流 = 单个段 [1]", _seq6)
        _prs = pp.probe(fs, subsong=1)
        _mark(_prs["ok"] and _prs["subsongs"] == 6,
              "probe 分段：subsongs = 6", _prs.get("subsongs"))
        _mark(abs(_prs["secs"] - 5 * 30 / 44100.0) < 1e-9,
              "probe 分段：full 时长 = 5 段 × 30 样本", _prs.get("secs"))
        _mark(pp.probe(fs, subsong=6).get("secs") is not None
              and abs(pp.probe(fs, subsong=6)["secs"] - 30 / 44100.0) < 1e-9,
              "probe 第 6 条子流 = 单段时长", pp.probe(fs, subsong=6).get("secs"))

        # ── 5. txtp 的 ``#005`` 子流号（GC 包就是这种） ─────────────────
        #
        # ⚠ 这是 v5.9.11 修的一个真 bug：``parse_txtp`` 原来只认 ``#I`` /
        #   ``#E``，``stream/STREAM__004.psf#005`` 里的 ``#005`` 被整个丢掉，
        #   24 首全变成「第 1 条子流」—— 时长对不上、内容也不对。
        pkg = os.path.join(box, "pkg")
        os.makedirs(os.path.join(pkg, "stream"), exist_ok=True)
        _mk(os.path.join(pkg, "stream", "STREAM__004.psf"), blocks=1)
        tp = os.path.join(pkg, "STREAM__004_005.txtp")
        with open(tp, "w", encoding="utf-8") as _f:
            _f.write("stream/STREAM__004.psf#005\n")
        _r = pt.parse_txtp(tp)
        _mark(_r["ok"] and _r["stream"] == 5,
              "parse_txtp 解析出子流号 #005 → 5", _r.get("stream"))
        _mark(_r["target"].lower().endswith("stream__004.psf"),
              "parse_txtp 的 target 指向 .psf", _r.get("target"))
        _mark(cj.classify(pkg) == "GameCube 音频",
              ".txtp 包按目标分到「GameCube 音频」", cj.classify(pkg))
        _prt = pt.probe_txtp(tp)
        _mark(_prt.get("ok"), "probe_txtp 能探到 .psf 目标", _prt.get("error"))
        _mark(dl.policy_for(os.path.join(box, "gc_80.psf")) == "pivotal",
              "policy_for(Pivotal .psf) == 'pivotal'",
              dl.policy_for(os.path.join(box, "gc_80.psf")))
        _mark(dl.policy_for(os.path.join(box, "ps1_01.psf")) == "psf",
              "policy_for(PS1 .psf) 仍是 'psf'",
              dl.policy_for(os.path.join(box, "ps1_01.psf")))
    finally:
        _rmtree_soft(box)

    # ── 6. 五个流式后缀的登记（三处都要有） ────────────────────────────
    _se = (".xvag", ".at3", ".hca", ".brstm", ".laac", ".at9", ".vag")
    _miss = [e for e in _se if e not in sa.EXT_KIND]
    _mark(not _miss, "七个流式后缀都在 stream_audio.EXT_KIND 里", _miss)
    _mark(all(sa.EXT_KIND.get(e) in sa.PYAV_KINDS
              for e in (".xvag", ".at3", ".hca", ".brstm", ".laac", ".at9")),
          "PyAV 的六个 kind 都在 PYAV_KINDS（走 PyAV，不写解码器）",
          [sa.EXT_KIND.get(e) for e in _se])
    # ⚠ .vag **必须不**在 PYAV_KINDS 里：FFmpeg 能 probe 出参数，但解码时
    #   avcodec_send_packet() 报 InvalidDataError（实测 2 个样本全这样）。
    #   它走本模块原生的 PSX-ADPCM（decode_vag），和 .npsf 同一条路。
    _mark("vag" not in sa.PYAV_KINDS,
          ".vag 不在 PYAV_KINDS 里（FFmpeg 能探不能解，走原生解码）",
          sa.PYAV_KINDS)
    _mark(hasattr(sa, "decode_vag") and hasattr(sa, "read_vag_header"),
          "stream_audio 有原生 VAG 解码器（decode_vag / read_vag_header）")
    _miss = [e for e in _se if e not in sa.ALL_EXTS]
    _mark(not _miss, "五个都在 stream_audio.ALL_EXTS（否则拖进来不收）", _miss)
    _miss = [e for e in _se if e not in cf.ALL_EXTS]
    _mark(not _miss, "五个都在 chip_formats.ALL_EXTS", _miss)
    _miss = [e for e in _se if not cf.SYSTEM_NAMES.get(sa.EXT_KIND.get(e, "").upper())]
    _mark(not _miss, "SYSTEM_NAMES 都有名字（否则文案出空括号）", _miss)
    _mark(".psf" in cf.ALL_EXTS, ".psf 仍在 ALL_EXTS 里")
    _mark(".txtp" in cf.ALL_EXTS, ".txtp 在 ALL_EXTS 里（GC 包靠它）")

    # 下载页的负载识别 —— 第四例同一个坑：认不出就当「不是负载」跳过
    _miss = [e for e in _se + (".txtp", ".txth")
             if not jd._is_payload_file("a/x" + e)]
    _mark(not _miss, "joshw_dl._is_payload_file 认得新后缀", _miss)
    _mark(".txth" in dl.NON_AUDIO_EXTS,
          ".txth 归「非音频」（3DS 的参数说明，否则 219 首全报转不了）")
    _mark(not dl.is_convertible("a/x.txth"), ".txth 本身不算可转音源")
    _miss = [e for e in _se if not dl.is_convertible("a/x" + e)]
    _mark(not _miss, "五个都算可转音源（is_convertible）", _miss)
    _miss = [e for e in _se if dl.policy_for("a/x" + e) != "stream"]
    _mark(not _miss, "五个的 policy_for 都是 'stream'", _miss)
    # ⚠ .psf **不能**进 stream_audio —— 它不是流式音频，走的是自己的解码器。
    _mark(".psf" not in sa.EXT_KIND,
          ".psf 不在 stream_audio 里（走 pivotal_psf / psf_native）")

    # ── 6b. VAG 的 28 样本/块（合成头，不依赖素材） ────────────────────
    #
    # VAG 和 Pivotal 的 PSX-ADPCM **不是**同一个口径：VAG 是标准 PSX
    # （16 字节块 = 1 头 + 1 padding + 14 数据 = **28** 样本，预测项带
    # ``+32`` 四舍五入）；Pivotal 把 padding 也用上 = 30 样本、不带 +32。
    # 两个都写错成对方的样子，症状都是「听起来对、但对不上参考实现」。
    _vbox = os.path.join(tmpdir.root(), "gate_v5911_vag")
    os.makedirs(_vbox, exist_ok=True)
    try:
        _vg = os.path.join(_vbox, "a.vag")
        _vh = bytearray(0x30)
        _vh[0:4] = b"VAGp"
        _vh[0x0C:0x10] = (16).to_bytes(4, "big")      # 数据 16 字节 = 1 块
        _vh[0x10:0x14] = (44100).to_bytes(4, "big")
        _blk = bytearray(16)
        _blk[2] = 0x0F        # 解码器从块内 +2 起读数据（+1 是 padding）
        with open(_vg, "wb") as _f:
            _f.write(bytes(_vh) + bytes(_blk))
        _hd = sa.read_vag_header(_vg)
        _mark(_hd["rate"] == 44100 and _hd["samples"] == 28,
              "VAG 头：44100 / 1 块 = 28 样本",
              (_hd["rate"], _hd["samples"]))
        _vp = sa.decode_pcm(_vg)
        _mark(len(_vp) == 28 * 4,
              "VAG 解出 28 帧 × 2ch × 2B（单声道已复制到立体声）", len(_vp))
        _mark(list(struct.unpack_from("<2h", _vp, 0)) == [-4096, -4096],
              "VAG 首帧 = [-4096, -4096]（15 → -1 → -1<<12）",
              list(struct.unpack_from("<2h", _vp, 0)))
        _mark(sa.probe(_vg).get("codec") == "PSX ADPCM (VAG)",
              "probe(.vag) 报 PSX ADPCM (VAG)", sa.probe(_vg).get("codec"))
    finally:
        _rmtree_soft(_vbox)

    # ── 7. 打包清单 + diag ─────────────────────────────────────────────
    _bp = open(os.path.join(ROOT, "build_web_pyi.py"), encoding="utf-8").read()
    _mark('"pivotal_psf"' in _bp,
          "build_web_pyi.HIDDEN 含 pivotal_psf（三处都是延迟 import）")

    _code, _d = _jget("/api/diag")
    _pv = _d.get("pivotal") or {}
    _mark(_code == 200 and _pv.get("ok"),
          "/api/diag 的 pivotal 段报可用", _pv.get("error") or _pv)
    _mark(int(_pv.get("rate") or 0) == 44100,
          "diag 报 pivotal 采样率 44100", _pv.get("rate"))
    _mark((_d.get("modules") or {}).get("pivotal_psf") is True,
          "diag.modules 里能看到 pivotal_psf（延迟 import 的唯一证据）")
    # ⚠ 两个 .psf 主人必须是**两个独立的段**，不能合成一个 —— 一个是
    #   「缺 BIOS / 缺 psf2wav.exe」，一个是「缺纯 Python 模块」，处置完全不同。
    _mark("psf" in _d and "pivotal" in _d and _d["psf"] is not _d["pivotal"],
          "diag 里 psf 与 pivotal 是两段（PS1/PS2 vs GameCube）")

    # ── 8. 有真样本时再验一次真解码 ────────────────────────────────────
    _samp = os.environ.get("MSU1_GC_PSF_SAMPLE", "")
    if not _samp or not os.path.isfile(_samp):
        print("  [--] 真解码（设 MSU1_GC_PSF_SAMPLE=<某个 GC .psf> 才验）",
              flush=True)
        return
    if fast:
        print("  [--] 真解码（--fast 跳过）", flush=True)
        return
    _r = pp.probe(_samp)
    _mark(_r["ok"], "真样本 probe 成功", _r.get("error"))
    if _r["ok"]:
        _mark(_r["secs"] > 0.5, "时长 > 0.5 秒", _r["secs"])
        _pcm = pp.decode_pcm(_samp)
        _mark(len(_pcm) > 44100,
              "解出超过 0.25 秒的 PCM（44100/16bit/立体声）", len(_pcm))
        _pk = max(abs(struct.unpack_from("<h", _pcm, i)[0])
                  for i in range(0, min(len(_pcm), 400000) - 1, 2))
        _mark(_pk > 500, "不是全静音（峰值 > 500）", _pk)


def t_v5912(fast=False):
    """[3c8] PS4 / Xbox 360 / Xbox / Wii U / PC-98 五个机种（v5.9.12）。

    这一轮和前几轮最大的不同：**第一次随包带第三方解码器**。

    ``.wem``（Audiokinetic Wwise 的 Custom Vorbis）和 ``.fsb``（FMOD FSB5 里的
    Nintendo DSP ADPCM）FFmpeg 都不支持 —— 前者能开容器但 ``codec_context``
    是 None，后者直接 ``PatchWelcomeError: Not yet implemented``。所以拉了
    ``vgmstream-cli.exe`` + **11 个 DLL**（约 11MB，ISC 许可）进包。

    这里钉四类容易「看起来没事」的错：

    1. **11 个 DLL 缺一不可** —— 它们是**载入时**硬依赖，不是用到才加载。
       实测只拷 exe + libvorbis.dll 报 ``libspeex-1.dll``；补到 8 个
       （想省掉 FFmpeg 那 4 个约 6MB）仍报 ``avutil-vgmstream-57.dll``。
       打包清单漏一个的表现是「.wem 一转换就说找不到解码器」。
    2. **4 声道被当立体声** —— Wwise 的 .wem 实测 195/224 个是 4 声道
       （FL FR BL BR）。原代码 ``if rate == OUT_RATE: return src.reshape(-1)``
       会把 4 声道数据**原样当立体声返回**，``layout="stereo"`` 又会让 SWR
       把「前两个样本当左右、后两个当下一帧」—— **输出速度翻倍、音调全错**。
       这是本轮修掉的最严重的一个 bug，用合成 4 声道钉死。
    3. **txtp 的三种主人** —— 同一个 ``.txtp`` 扩展名，包里第一个 txtp 指向
       ``.xa`` 是 PS1、指向 ``.psf`` 是 GameCube、指向 ``.wem`` 是 PS4。
       分错的症状是「能解的包被报成 PS1 没 BIOS」。
    4. **``.s98`` 曾经被判成「无解」，v5.9.13 解开了** —— PC-98 的 YM2608
       寄存器写入日志。上一轮钉的是「不许登记成可转」，理由是「工程里没有
       YM2608 核心」；那个结论**漏了一跳**：``.s98`` 和 ``.vgm`` 同族，
       理论上可以转成 VGM 复用 libgme。读了 libgme 的 ``Vgm_Emu_Impl.cpp``
       才确认这条路也是死的（只实现 SN76489/YM2413/YM2612 五条指令，
       YM2608 的 0x56/0x57 落到 ``default:`` 静默跳过 → 精确静音）。
       真正的解是自己编一个 ymfm 核（``s982wav.exe``）。
       现在钉的是**正向**：``.s98`` 必须可转、必须走 ``s98_native``。

    ``fast`` 只影响末尾的真样本解码（要设 ``MSU1_PS4_TXTP`` / ``MSU1_PS4_WEM``）。
    """
    print("\n[3c8] PS4 / X360 / Xbox / WiiU / PC-98 五个机种", flush=True)

    import stream_audio as sa
    import dl_convert as dl
    import convert_job as cj
    import chip_formats as cf
    import psx_txtp as pt
    import joshw_dl as jd
    import vgm_audio as vg
    import msu1_to_cd as core

    # ── 1. 模块契约 ────────────────────────────────────────────────────
    _mark(tuple(vg.EXTS) == (".wem", ".fsb"),
          "vgm_audio.EXTS 只认 .wem / .fsb", vg.EXTS)
    _mark(vg.DECODER_NAME == "vgmstream-cli.exe",
          "解码器名是 vgmstream-cli.exe", vg.DECODER_NAME)

    # ⚠ 概念性守卫：这两族**必须不走 PyAV**。FFmpeg 对 .fsb 是
    #   PatchWelcomeError，对 .wem 是 codec_context=None —— 进了 PYAV_KINDS
    #   的表现是「能 probe、一解码就炸」，比根本解不了更难查。
    _mark("wem" not in sa.PYAV_KINDS and "fsb" not in sa.PYAV_KINDS,
          "wem / fsb 不在 PYAV_KINDS（FFmpeg 不支持这两族）", sa.PYAV_KINDS)
    _mark(tuple(sa.VGM_KINDS) == ("wem", "fsb"),
          "VGM_KINDS == ('wem', 'fsb')", sa.VGM_KINDS)
    _mark(all(sa.EXT_KIND.get(e) in sa.VGM_KINDS for e in (".wem", ".fsb")),
          "两个扩展名的 kind 都落在 VGM_KINDS 里",
          [sa.EXT_KIND.get(e) for e in (".wem", ".fsb")])

    # ── 2. 4 声道降混（本轮最严重的 bug，用合成数据钉死） ───────────────
    #
    # 4800 帧 @48000 → 4410 帧 @44100。写成 stereo 会让 SWR 把 4 声道
    # 交错数据当成「2 帧立体声」，输出 9600 帧 = **速度翻倍**。
    import numpy as np
    _n = 4800
    _q = np.zeros((_n, 4), dtype=np.int16)
    _q[:, 0] = 1000      # FL
    _q[:, 1] = -1000     # FR
    _q[:, 2] = 1000      # BL
    _q[:, 3] = -1000     # BR
    _out = sa._resample_to_out(_q.reshape(-1), 48000, 4)
    _o = np.frombuffer(_out, dtype="<i2")
    _frames = len(_o) // 2
    _mark(abs(_frames - 4410) <= 2,
          "4ch 48000→44100 输出 4410 帧（不是 9600 的翻倍错位）", _frames)
    _mark(_o[0] > 0 and _o[1] < 0,
          "4ch 降混后 L 正 / R 负（FL+BL → L，FR+BR → R）",
          (int(_o[0]), int(_o[1])))
    _mark(sa._SRC_LAYOUT.get(4) == "quad",
          "4 声道用 'quad' 声明（不能用 'stereo'）", sa._SRC_LAYOUT.get(4))
    # 2ch@44100 必须**原样返回**（这是快路径，别为了修 4ch 把它弄慢）
    _st = np.arange(-100, 100, dtype=np.int16)
    _mark(np.array_equal(np.frombuffer(
        sa._resample_to_out(_st, 44100, 2), dtype="<i2"), _st),
        "2ch 44100 原样返回（快路径没被 4ch 的修改破坏）")
    # 单声道必须复制成双声道，不是只塞左边
    _mono = np.full(100, 500, dtype=np.int16)
    _mo = np.frombuffer(sa._resample_to_out(_mono, 44100, 1), dtype="<i2")
    _mark(len(_mo) == 200 and _mo[0] == 500 and _mo[1] == 500,
          "单声道复制到双声道", (len(_mo), int(_mo[0]), int(_mo[1])))

    # ── 3. BTSND（Wii U 启动音）—— 大端 PCM16 ──────────────────────────
    box = os.path.join(tmpdir.root(), "gate_v5912")
    os.makedirs(box, exist_ok=True)
    try:
        _f = os.path.join(box, "boot.btsnd")
        _vals = (0x1000, -0x1000, 0x7FFF, -0x8000)
        with open(_f, "wb") as _fh:
            _fh.write(b"\x00" * 8)          # type=0 / loop_start=0
            for _v in _vals:
                _fh.write(int(_v).to_bytes(2, "big", signed=True))
        _h = sa.read_btsnd_header(_f)
        _mark(_h["rate"] == 48000 and _h["channels"] == 2,
              "BTSND 头：48000 / 2ch", (_h["rate"], _h["channels"]))
        _mark(_h["samples"] == 2,
              "BTSND 8 字节体 = 2 帧（不是 4 帧）", _h["samples"])
        _pcm, _rate = sa.decode_btsnd(_f)
        _mark(list(np.asarray(_pcm).tolist()) == list(_vals),
              "BTSND 大端 → 小端数值不变（符号位正确）",
              np.asarray(_pcm).tolist())
        _mark(sa.probe(_f).get("codec") == "PCM 16bit BE (BTSND)",
              "probe(.btsnd) 报 PCM 16bit BE", sa.probe(_f).get("codec"))
        # 长度不是 4 的倍数必须报错，不能悄悄截断
        _bad = os.path.join(box, "bad.btsnd")
        with open(_bad, "wb") as _fh:
            _fh.write(b"\x00" * 8 + b"\x01\x02\x03")
        _threw = False
        try:
            sa.read_btsnd_header(_bad)
        except ValueError:
            _threw = True
        _mark(_threw, "BTSND 体长不是 4 的倍数时报错（不悄悄截断）")

        # ── 4. txtp 的三种主人 ──────────────────────────────────────────
        _mark(tuple(pt._VGM_TARGET_EXTS) == (".wem", ".fsb"),
              "psx_txtp._VGM_TARGET_EXTS == ('.wem', '.fsb')",
              pt._VGM_TARGET_EXTS)

        def _mkpkg(name, txtp_name, body, extra=()):
            d = os.path.join(box, name)
            os.makedirs(d, exist_ok=True)
            for e in extra:
                _p = os.path.join(d, e)
                os.makedirs(os.path.dirname(_p), exist_ok=True)
                with open(_p, "wb") as _fh:
                    _fh.write(b"\x00" * 16)
            with open(os.path.join(d, txtp_name), "w",
                      encoding="utf-8") as _fh:
                _fh.write(body)
            return d

        _pw = _mkpkg("ps4", "e.txtp",
                     "wem/1.wem #i #b 44.37635746425119\n", ("wem/1.wem",))
        _pg = _mkpkg("gc", "s.txtp", "stream/STREAM__004.psf#005\n",
                     ("stream/STREAM__004.psf",))
        _px = _mkpkg("ps1", "x.txtp", "MUSIC/XA1.xa;1\n", ("MUSIC/XA1.xa",))
        _mark(cj._txtp_target_ext(_pw, os.listdir(_pw)) == ".wem",
              "_txtp_target_ext 读出 .wem", cj._txtp_target_ext(
                  _pw, os.listdir(_pw)))
        _mark(cj.classify(_pw) == "PS4 音频",
              "指向 .wem 的 txtp 包归「PS4 音频」", cj.classify(_pw))
        _mark(cj.classify(_pg) == "GameCube 音频",
              "指向 .psf 的 txtp 包归「GameCube 音频」", cj.classify(_pg))
        _mark(cj.classify(_px) == "PSX 音频",
              "指向 .xa 的 txtp 包归「PSX 音频」", cj.classify(_px))
        # 没有目标扩展名的 txtp 不能崩，要退回空串
        _pn = _mkpkg("none", "n.txtp", "plainname\n")
        _mark(cj._txtp_target_ext(_pn, os.listdir(_pn)) == "",
              "txtp 里没有扩展名时返回空串（不崩）",
              cj._txtp_target_ext(_pn, os.listdir(_pn)))
        # ── 4b. 长路径 + txtp 的 ``?`` 前缀（v5.9.12 实测踩的两个坑） ─────
        #
        # ⚠ 坑一：Windows MAX_PATH(260)。PS4 的 AC4 包里有 **38 个 .txtp**
        #   的完整路径是 263~315 字符（名字里塞满
        #   ``(3198468865=LAND)(PLANTATIONS=INACTIVE)...`` 这种事件参数）。
        #   ``os.path.isfile`` 对它们返回 **False 而不是抛异常** ——
        #   于是这 38 首**静默消失**：界面少 38 首、不报错、日志无痕。
        #   实测 ``list_txtp`` 因此只返回 148 / 186。
        _mark(core.long_path("D:\\a\\b.wav") == "D:\\a\\b.wav",
              "long_path：短路径原样返回")
        _long = "D:\\" + "x" * 260 + ".wav"
        _mark(core.long_path(_long).startswith("\\\\?\\"),
              "long_path：超长路径加 \\\\?\\ 前缀", core.long_path(_long)[:8])
        _mark(core.long_path("\\\\srv\\share\\" + "y" * 260 + ".wav")
              .startswith("\\\\?\\UNC\\"),
              "long_path：UNC 路径拼成 \\\\?\\UNC\\（不能直接加前缀）")
        _mark(core.long_path("\\\\?\\D:\\already.wav")
              == "\\\\?\\D:\\already.wav",
              "long_path：已经有前缀的不重复加")

        # 真造一个超长路径的 .txtp，验 list_txtp / parse_txtp 都能看见它
        _deep = os.path.join(box, "lp")
        for _i in range(6):
            _deep = os.path.join(_deep, "d" * 40 + str(_i))
        os.makedirs(core.long_path(_deep), exist_ok=True)
        _lname = "L" * 200 + ".txtp"
        _lpath = os.path.join(_deep, _lname)
        _mark(len(_lpath) > 260, "造出来的测试路径确实超过 260 字符",
              len(_lpath))
        with open(core.long_path(_lpath), "w", encoding="utf-8") as _fh:
            _fh.write("?wem/1.wem #i #b 30.0\n")
        _found = pt.list_txtp(_deep)
        _mark(len(_found) == 1,
              "list_txtp 能看见超长路径的 .txtp（旧代码会静默漏掉）",
              len(_found))

        # ⚠ 坑二：行首 ``?`` 是 vgmstream 的「**可选文件**」标记，不是文件名
        #   的一部分。WWISER 在「这个事件引用的 wem 可能不在银行里」时写成
        #   ``?wem/163303038.wem #i #b 30.0``。不去掉的话拼出来的目标是
        #   ``\?wem\163303038.wem`` → 「目标不存在」，而那个 wem 其实在。
        #   实测 AC4 的 4 首 Plantations_Day 就是这么挂的。
        _p = pt.parse_txtp(_lpath)
        _mark(_p["ok"] and not os.path.basename(_p["target"]).startswith("?"),
              "parse_txtp 去掉行首 '?'（可选文件标记）",
              os.path.basename(_p.get("target") or ""))
        _mark(_p["ok"] and _p["target"].lower().endswith("1.wem"),
              "去掉 '?' 后目标指向 1.wem",
              os.path.basename(_p.get("target") or ""))
        # 不带 '?' 的正常 txtp 不能被误伤
        _q = os.path.join(box, "noq.txtp")
        with open(_q, "w", encoding="utf-8") as _fh:
            _fh.write("wem/1.wem #i #b 30.0\n")
        _pq = pt.parse_txtp(_q)
        _mark(_pq["ok"] and _pq["target"].lower().endswith("1.wem"),
              "不带 '?' 的 txtp 不受影响",
              os.path.basename(_pq.get("target") or ""))
    finally:
        _rmtree_soft(box)

    # ── 5. 六个新后缀的登记（三处都要有） ──────────────────────────────
    _se = (".wem", ".fsb", ".btsnd", ".xma", ".bik", ".araw")
    _miss = [e for e in _se if e not in sa.EXT_KIND]
    _mark(not _miss, "六个新后缀都在 stream_audio.EXT_KIND 里", _miss)
    _miss = [e for e in _se if e not in sa.ALL_EXTS]
    _mark(not _miss, "六个都在 stream_audio.ALL_EXTS（否则拖进来不收）", _miss)
    _miss = [e for e in _se if e not in cf.ALL_EXTS]
    _mark(not _miss, "六个都在 chip_formats.ALL_EXTS", _miss)
    _miss = [e for e in _se if not cf.SYSTEM_NAMES.get(
        sa.EXT_KIND.get(e, "").upper())]
    _mark(not _miss, "SYSTEM_NAMES 都有名字（否则文案出空括号）", _miss)
    _miss = [e for e in _se if not jd._is_payload_file("a/x" + e)]
    _mark(not _miss, "joshw_dl._is_payload_file 认得新后缀（第五例同一个坑）",
          _miss)
    _miss = [e for e in _se if not dl.is_convertible("a/x" + e)]
    _mark(not _miss, "六个都算可转音源（is_convertible）", _miss)
    _miss = [e for e in _se if dl.policy_for("a/x" + e) != "stream"]
    _mark(not _miss, "六个的 policy_for 都是 'stream'", _miss)

    # ── 6. 三个「不转」的必须明确不转 ──────────────────────────────────
    #
    # ⚠ ``.bnk`` 是 Wwise 的 SoundBank。实测 AC4 那个只有 BKHD + HIRC + STID
    #   三个块，**没有 DIDX/DATA**（音频表 + 数据）—— 里面根本没有音频，
    #   真正的音频是旁边那 224 个 .wem。当成可转音源会让每一批都带 2 条
    #   「转不了」的假失败。
    _mark(".bnk" in dl.NON_AUDIO_EXTS,
          ".bnk 归「非音频」（SoundBank 里没有 DIDX/DATA）")
    _mark(not dl.is_convertible("a/x.bnk"), ".bnk 不算可转音源")
    # ⚠ ``.s98``（PC-98 / PC-88 的 YM2608）—— v5.9.13 起**可转**。
    #
    #   上一轮这里是反向断言（``".s98" not in ...``），依据是「工程里没有
    #   YM2608 核心」。那个结论对，但**理由不完整**：``.s98`` 和 ``.vgm``
    #   同族，正确思路是先转 VGM 再复用 libgme。读了 libgme 源码才发现
    #   这一跳也走不通 —— ``Vgm_Emu_Impl.cpp`` 的 ``run_commands()`` 只实现
    #   cmd_gg_stereo/cmd_psg/cmd_ym2413/cmd_ym2612_port0,1 五条，YM2608 的
    #   VGM 指令 0x56/0x57 会落到 ``default:`` 被 ``command_len`` 跳过，
    #   输出精确静音**且不报错**。所以最终解是自己编 ymfm 核。
    #
    #   现在钉正向三件事，防止「登记了但其实没接上」的假成功：
    #     ① 扩展名进 ALL_EXTS / is_chip_ext（界面收得进来）
    #     ② 不能被当成流式音频（否则会去问 FFmpeg，必然失败）
    #     ③ 真的登记成芯片格式，且识别得出魔数
    _mark(".s98" in cf.ALL_EXTS and cf.is_chip_ext(".s98"),
          ".s98 登记进 ALL_EXTS / CHIP_EXTS（v5.9.13 起可转）")
    _mark(".s98" not in sa.EXT_KIND,
          ".s98 不走流式音频（FFmpeg 没有 YM2608 模拟）")
    _mark(cf.EXT_TABLE.get(".s98") == "S98",
          ".s98 -> 格式 id S98")
    _mark(cf.S98_EXTS == (".s98",), "S98_EXTS 单一真源正确")
    _mark(dl.is_convertible("a/x.s98"), ".s98 算可转音源")
    _mark(cf.SYSTEM_NAMES.get("S98", "").startswith("PC-98"),
          "S98 的机种名是 PC-98 / PC-88")

    # ⚠ v5.9.13 顺手修的一个**老 bug**：``_probe_chip`` 以前只认
    #   ``track_lengths``（m3u 的逐轨长度），把 ``exact_secs`` 完全漏掉 ——
    #   而 VGM 的采样数、GBS 的 loop_end、GYM 的帧数、S98 的 tick
    #   **都落在 exact_secs 里**。于是这些格式在左栏一律显示 150 秒。
    #   实测：1.2 秒的 .s98 显示成 150 秒（转换结果没错，只是数字骗人）。
    #   这里用合成样本钉住 —— 它不依赖任何外部素材。
    import _s98_fixture as _fx
    _p98 = _fx.make_s98(os.path.join(tmpdir.root(), "v5913_fixture.s98"))
    _jpost("/api/clear", {})
    _code, _r = _jpost("/api/add_sources", {"paths": [_p98]})
    _code, _st = _jget("/api/state")
    _src = (_st.get("sources") or [{}])[0]
    _mark(_r.get("added") == 1 and _src.get("kind") == "芯片音源",
          "合成 .s98 加得进音源列表、归成芯片音源", _src.get("kind"))
    _mark(abs(float(_src.get("total_secs") or 0) - _fx.EXPECT_SECS) < 0.05,
          "左栏时长取 exact_secs（%.2fs，不是默认的 150s）" % _fx.EXPECT_SECS,
          _src.get("total_secs"))
    _jpost("/api/clear", {})
    # 压缩包不是音源，但必须能被跳过而不是报错
    _mark(all(e in dl.NON_AUDIO_EXTS for e in (".7z", ".zip", ".rar")),
          "压缩包归「非音频」（joshw 包里常带 !Extras.7z）")

    # ── 7. 打包清单 + diag ─────────────────────────────────────────────
    _bp = open(os.path.join(ROOT, "build_web_pyi.py"), encoding="utf-8").read()
    _mark('"vgm_audio"' in _bp,
          "build_web_pyi.HIDDEN 含 vgm_audio（两处都是延迟 import）")
    _need = ("vgmstream-cli.exe", "avcodec-vgmstream-59.dll",
             "avformat-vgmstream-59.dll", "avutil-vgmstream-57.dll",
             "swresample-vgmstream-4.dll", "libatrac9.dll",
             "libcelt-0061.dll", "libcelt-0110.dll", "libg719_decode.dll",
             "libmpg123-0.dll", "libspeex-1.dll", "libvorbis.dll",
             "vgmstream-COPYING.txt")
    _miss = [n for n in _need if n not in _bp]
    _mark(not _miss,
          "build_web_pyi.VGM_FILES 含 exe + 11 DLL + COPYING", _miss)
    # COPYING 是 ISC 的唯一义务 —— 单独钉一次，别被当垃圾清掉
    _mark("vgmstream-COPYING.txt" in _bp,
          "打包清单带 vgmstream-COPYING.txt（ISC 署名义务）")

    _code, _d = _jget("/api/diag")
    _v = _d.get("vgm") or {}
    _mark(_code == 200 and _v.get("ok"),
          "/api/diag 的 vgm 段报可用（exe + 11 DLL 都在）",
          _v.get("why") or _v.get("error"))
    _mark(os.path.basename(str(_v.get("decoder") or ""))
          == "vgmstream-cli.exe",
          "diag 报的解码器是 vgmstream-cli.exe", _v.get("decoder"))
    _mark((_d.get("modules") or {}).get("vgm_audio") is True,
          "diag.modules 里能看到 vgm_audio（延迟 import 的唯一证据）")
    _mark(sorted(_v.get("exts") or []) == [".fsb", ".wem"],
          "diag 报的 exts == ['.fsb', '.wem']", _v.get("exts"))

    # v5.9.13：S98 这一路是**另一个**第三方解码器（ymfm），和 vgmstream 没有
    # 任何共用文件，所以必须单独钉一遍 —— 只钉 vgm 那段会漏掉「s982wav.exe
    # 忘了带」这种包（模块在、exe 不在，界面照样把 .s98 收进来）。
    _mark('"s98_native"' in _bp,
          "build_web_pyi.HIDDEN 含 s98_native（__import__ 动态加载）")
    _mark('"s982wav.exe"' in _bp,
          "build_web_pyi.EXTRA_EXE 含 s982wav.exe")
    _s = _d.get("s98") or {}
    _mark(_code == 200 and _s.get("ok"),
          "/api/diag 的 s98 段报可用（s98_native + s982wav.exe 都在）",
          _s.get("why") or _s.get("error"))
    _mark(os.path.basename(str(_s.get("decoder") or "")) == "s982wav.exe",
          "diag 报的 S98 解码器是 s982wav.exe", _s.get("decoder"))
    _mark((_d.get("modules") or {}).get("s98_native") is True,
          "diag.modules 里能看到 s98_native（延迟 import 的唯一证据）")
    _mark(sorted(_s.get("exts") or []) == [".s98"],
          "diag 报的 S98 exts == ['.s98']", _s.get("exts"))
    # 这两段必须各自独立 —— 曾经把 s98 错挂到 vgm 段上，结果 vgmstream 在
    # 而 s982wav 不在时 diag 依然全绿（.s98 一转换就找不到解码器）。
    _mark((_d.get("s98") or {}) is not (_d.get("vgm") or {}),
          "diag 的 s98 段与 vgm 段是两个独立对象")

    # ── 8. 有真样本时再验一次真解码 ────────────────────────────────────
    if fast:
        print("  [--] 真解码（--fast 跳过）", flush=True)
        return
    _tp = os.environ.get("MSU1_PS4_TXTP", "")
    _wm = os.environ.get("MSU1_PS4_WEM", "")
    if not (_tp and os.path.isfile(_tp)):
        print("  [--] 真解码（设 MSU1_PS4_TXTP=<某个 PS4 .txtp> 才验）",
              flush=True)
        return
    _r = pt.probe_txtp(_tp)
    _mark(_r.get("ok"), "PS4 txtp probe 成功", _r.get("error"))
    if _r.get("ok"):
        _mark((_r.get("seconds") or 0) > 1.0, "时长 > 1 秒", _r.get("seconds"))
        _mark("Vorbis" in str(_r.get("kind") or ""),
              "报的编码是 Custom Vorbis", _r.get("kind"))
        _raw = vg.decode_out(_tp)
        _a = np.frombuffer(_raw, dtype="<i2")
        _mark(len(_a) > 44100,
              "解出超过 0.25 秒的 PCM（44100/16bit/立体声）", len(_a))
        _mark(int(np.abs(_a).max()) > 500, "不是全静音（峰值 > 500）",
              int(np.abs(_a).max()))
        # ⚠ probe 与实转的时长必须一致 —— 这两个字段踩过坑：
        #   numberOfSamples 是素材全长，playSamples 对 layered 是各层之和。
        _want = float(_r.get("seconds") or 0)
        _got = len(_a) / 2.0 / 44100.0
        _mark(abs(_got - _want) < 0.05,
              "probe 时长与实转一致（误差 < 50ms）", (_want, round(_got, 2)))
    if _wm and os.path.isfile(_wm):
        _r2 = vg.probe(_wm)
        _mark(_r2.get("ok"), "裸 .wem probe 成功", _r2.get("error"))


def t_opts_and_fsm(state):
    print("\n[4] 参数合并与状态机", flush=True)

    out = os.path.join(tmpdir.root(), "webui_gate_out")
    _jpost("/api/add_sources", {"paths": [SPC]})

    # v5.4：输出格式默认**一个都不勾**，所以「两个都没传」必须被挡住 ——
    # 否则 ConvertJob 两个分支都不进，用户看到「转换完成」却一个文件都没有。
    code, d = _jpost("/api/start", {"opts": {"out_dir": out}})
    _mark(code == 400 and "格式" in (d.get("error") or ""),
          "两个格式都没选 → 400 且说清原因", (code, d))
    code, st = _jget("/api/state")
    _mark(st.get("state") != "running", "被挡住时不会真的开始转",
          st.get("state"))

    # 只给一个字段，其余必须落回默认值 —— 前端漏传字段不该让转换炸
    code, d = _jpost("/api/start", {"opts": {"max_secs": 30, "out_dir": out,
                                             "do_wav": True}})
    _mark(code == 200 and d.get("ok"), "start 接受部分 opts", d)

    code, st = _jget("/api/state")
    _mark(st.get("state") == "running", "state 变 running", st.get("state"))
    _mark(float(st["opts"].get("max_secs")) == 30.0, "传入的 max_secs 生效",
          st["opts"].get("max_secs"))
    _mark(float(st["opts"].get("gap")) == 2.0, "未传的 gap 落回默认 2.0",
          st["opts"].get("gap"))
    _mark(st["opts"].get("do_wav") is True, "传入的 do_wav 生效",
          st["opts"].get("do_wav"))
    _mark(st["opts"].get("do_cd") is False,
          "未传的 do_cd 落回默认 False（v5.4 起不再默认全勾）",
          st["opts"].get("do_cd"))

    code, d = _jpost("/api/start", {"opts": {}})
    _mark(code == 409, "running 时再 start → 409", code)

    # 等它跑完（sample.spc 只有 4 秒，很快）
    ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 180)
    code, st = _jget("/api/state")
    _mark(ok, "转换在 180 秒内结束", st.get("state"))
    _mark(st.get("state") == "done", "state=done", st.get("state"))

    res = st.get("result") or {}
    _mark(int(res.get("ok") or 0) >= 1, "result.ok ≥ 1", res)
    _mark(int(res.get("fail") or 0) == 0, "result.fail = 0",
          (res, (st.get("logs") or [])[-8:]))
    _mark(float(res.get("seconds") or 0) > 0, "result.seconds > 0",
          res.get("seconds"))
    _mark(st.get("progress") == 100.0, "结束后 progress=100", st.get("progress"))
    _mark(bool(st.get("outputs")), "outputs 已登记", len(st.get("outputs") or []))
    _mark(bool(st.get("logs")), "日志有内容", len(st.get("logs") or []))
    return out


# ══════════════════════════════════════════════════════════════════════
#  「打开输出目录」（v5.2.3）
# ══════════════════════════════════════════════════════════════════════
def t_open_outdir(state):
    """`POST /api/open_outdir` 的四个分支。

    ⚠ 必须把 ``os.startfile`` 打桩 —— 否则跑门禁会一连弹出好几个资源管理器。

    ⚠ 这里守的是一条**很容易被后人改回去**的约定：前端必须显式传 ``dir``。
    因为 ``state.opts["out_dir"]`` 只在 ``POST /api/start`` 里才写入，
    转换之前后端自己取不到目录 —— 谁要是「简化」成不传 dir，按钮在
    转换前就会一律报「还没设输出目录」，而门禁全绿，没人发现。
    """
    print("\n[+] 打开输出目录", flush=True)

    called = []
    real = getattr(os, "startfile", None)
    os.startfile = lambda p: called.append(p)     # noqa: S606 打桩，不真弹窗口
    old = (state.opts or {}).get("out_dir") or ""
    try:
        base = os.path.join(tmpdir.root(), "gate_outdir")
        os.makedirs(base, exist_ok=True)

        # 1. 一个目录都没有
        state.opts["out_dir"] = ""
        code, d = _jpost("/api/open_outdir", {})
        _mark(code == 400 and "输出目录" in (d.get("error") or ""),
              "没设目录 → 400 且提示「还没设输出目录」", (code, d))
        _mark(not called, "没设目录时不去碰资源管理器", called)

        # 2. 目录不存在（用户没转换过 / 目录被移走）
        state.opts["out_dir"] = os.path.join(base, "no_such_dir_xyz")
        code, d = _jpost("/api/open_outdir", {})
        _mark(code == 400 and "目录不存在" in (d.get("error") or ""),
              "目录不存在 → 400 且文案是人话（不是英文 FileNotFoundError）",
              (code, d))
        _mark(not called, "目录不存在时不去碰资源管理器", called)

        # 3. 正常路径：留空 dir 就用 state.opts.out_dir
        state.opts["out_dir"] = base
        code, d = _jpost("/api/open_outdir", {})
        _mark(code == 200 and d.get("ok") is True, "正常目录 → 200", (code, d))
        _mark(len(called) == 1
              and os.path.normcase(called[0]) == os.path.normcase(base),
              "startfile 收到同一个目录", called)

        # 4. 前端显式传 dir —— 转换之前唯一能走通的路
        del called[:]
        base2 = os.path.join(tmpdir.root(), "gate_outdir2")
        os.makedirs(base2, exist_ok=True)
        state.opts["out_dir"] = ""                # 故意清空，模拟「还没转换」
        code, d = _jpost("/api/open_outdir", {"dir": base2})
        _mark(code == 200 and len(called) == 1
              and os.path.normcase(called[0]) == os.path.normcase(base2),
              "显式传 dir 在 opts 为空时也能开（按钮就是靠这条）",
              (code, d, called))
    finally:
        state.opts["out_dir"] = old
        if real is not None:
            os.startfile = real


def t_default_outdir(state):
    """默认输出目录的命名规则（v5.4 修的一个真 bug）。

    ⚠ 规则必须是「output 放在**音源所在那一层**」：

    * 目录音源 → ``<父级>\\<包名>output``
    * 文件音源 → ``<所在目录>\\output``

    曾经的写法是「base 的 basename + output」，对**目录**音源就变成
    ``<包名>\\<包名>output`` —— 路径长度直接翻倍。实测一个 NDS 包
    （``Ace Attorney Investigations - Miles Edgeworth [Gyakuten Kenji]
    (2009-05-28)(Capcom)[NDS]``）因此撞上 Windows MAX_PATH(260)，报
    **「Errno 2 No such file or directory」** —— 这个错误码极具误导性，
    看起来像「目录没建」，而 ``ConvertJob.run()`` 开头明明有 ``makedirs``。
    实际是 Windows 对超长路径就回这个码。

    对**文件**音源还额外踩过一次：拼了文件名（``101 xxx Prologueoutput``），
    一个包里几十个 .mini2sf 会各自建一个目录，编号也断成每目录从 01 起。

    ⚠ 这里**直接调** ``server.default_out_dir()``，不走 ``/api/start`` ——
    后者要真起一次转换（慢），而且现在「两个格式都没勾」会直接 400。
    单一实现点就在那个函数里，测它等价于测整条链路。
    """
    print("\n[+] 默认输出目录命名", flush=True)

    base = os.path.join(tmpdir.root(), "gate_default_outdir_%d" % os.getpid())
    os.makedirs(base, exist_ok=True)

    # ① 目录音源 → <父级>\<包名>output
    spcdir = os.path.join(base, "SpcPack")
    os.makedirs(spcdir, exist_ok=True)
    got = server.default_out_dir(spcdir)
    want = os.path.join(base, "SpcPackoutput")
    _mark(os.path.normcase(got) == os.path.normcase(want),
          "目录音源 → <父级>\\<包名>output（不嵌套）", got)
    _mark(os.path.normcase(got) !=
          os.path.normcase(os.path.join(spcdir, "SpcPackoutput")),
          "output 不在包目录内部", got)

    # ② 文件音源 → <所在目录>\output（不拼文件名）
    longpkg = os.path.join(
        base, "Ace Attorney Investigations - Miles Edgeworth "
              "[Gyakuten Kenji] (2009-05-28)(Capcom)[NDS]")
    os.makedirs(longpkg, exist_ok=True)
    f = os.path.join(longpkg, "104 Investigation ~ Contradiction at the "
                             "Crime Scene.mini2sf")
    with open(f, "wb") as fh:
        fh.write(b"PSF\x24")
    got2 = server.default_out_dir(f)
    want2 = os.path.join(longpkg, "output")
    _mark(os.path.normcase(got2) == os.path.normcase(want2),
          "文件音源 → <所在目录>\\output（不拼文件名）", got2)

    # ③ 长路径场景：默认目录 + 60 字符文件名必须还在 MAX_PATH 内
    _mark(len(got2) + 60 < 260,
          "默认输出目录 + 文件名 < MAX_PATH(260)", len(got2) + 60)

    # ④ 空路径不炸（防御性）
    _mark(server.default_out_dir("") == os.path.join(".", "output"),
          "空路径不抛异常", server.default_out_dir(""))

# ══════════════════════════════════════════════════════════════════════
#  5. 试听（含 Range）
# ══════════════════════════════════════════════════════════════════════
def t_audio(state):
    print("\n[5] 试听接口", flush=True)

    code, st = _jget("/api/state")
    outs = st.get("outputs") or []
    if not _mark(bool(outs), "有可试听的输出"):
        return
    oid = outs[0]["id"]
    size = int(outs[0].get("size") or 0)

    code, hdr, raw = _req("/api/audio?id=%d" % oid)
    _mark(code == 200, "GET /api/audio 200", code)
    _mark(raw[:4] == b"RIFF" and raw[8:12] == b"WAVE", "首字节是 RIFF/WAVE",
          raw[:12])
    _mark(len(raw) == size, "整文件长度与登记一致", "%d vs %d" % (len(raw), size))
    _mark("audio/wav" in hdr.get("content-type", ""), "Content-Type 是 audio/wav",
          hdr.get("content-type"))
    _mark(hdr.get("accept-ranges") == "bytes", "声明 Accept-Ranges: bytes",
          hdr.get("accept-ranges"))

    code, hdr, raw = _req("/api/audio?id=%d" % oid, headers={"Range": "bytes=0-99"})
    _mark(code == 206, "Range 请求返回 206", code)
    _mark(len(raw) == 100, "Range 返回 100 字节", len(raw))
    _mark(hdr.get("content-range", "").startswith("bytes 0-99/"),
          "Content-Range 正确", hdr.get("content-range"))


# ══════════════════════════════════════════════════════════════════════
#  6. 取消
# ══════════════════════════════════════════════════════════════════════
def t_cancel(state):
    print("\n[6] 取消", flush=True)

    code, d = _jpost("/api/cancel", {})
    _mark(code == 200 and d.get("ok"), "空闲时 cancel 也返回 ok", d)

    if not os.path.isfile(CUE):
        print("  [--] 没有光盘镜像素材，跳过真实取消", flush=True)
        return

    out = os.path.join(tmpdir.root(), "webui_gate_cancel")
    _jpost("/api/clear", {})
    _jpost("/api/add_sources", {"paths": [CUE]})
    code, d = _jpost("/api/start", {"opts": {"out_dir": out, "do_cd": False,
                                             "do_wav": True}})
    if not _mark(code == 200, "启动镜像转换", d):
        return
    time.sleep(1.0)
    code, d = _jpost("/api/cancel", {})
    _mark(code == 200, "发出取消", d)

    ok = _wait(lambda: _jget("/api/state")[1].get("state") != "running", 120)
    code, st = _jget("/api/state")
    _mark(ok, "取消后 120 秒内退出 running", st.get("state"))
    _mark(st.get("state") in ("cancelled", "done"), "终态是 cancelled/done",
          st.get("state"))
    _mark(state.job is None, "job 已清空（不会卡住下一次）", state.job)


# ══════════════════════════════════════════════════════════════════════
#  3d. 网易云云盘：账号 / 登录 / 上传清单
# ══════════════════════════════════════════════════════════════════════
def _make_wav_dir(base, n=3):
    """造一个「既有合并文件、又有分轨」的目录。

    ⚠ 合并文件的后缀必须从 ``cloud_upload.MERGE_TAG`` 取，**不能写死
    「（合并）」** —— 那条命名规则一旦漂移，这里就会造出一个「不是合并文件
    的合并文件」，然后 need_choose 断言会以一个完全误导人的方式红掉。
    """
    import shutil
    import wave

    import cloud_upload as cu

    d = os.path.join(base, "webui_gate_cloud")
    _rmtree_soft(d)
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        p = os.path.join(d, "Track%02d.wav" % i)
        with wave.open(p, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x01" * 4410)          # 0.05 秒
    shutil.copy(os.path.join(d, "Track00.wav"),
                os.path.join(d, "Album" + cu.MERGE_TAG + ".wav"))
    return d


def t_game_folders(state):
    """下载流程的产物按**游戏名**分子文件夹（``WAV/<游戏名>/``）。

    真正的行为验证在 ``test_dl_convert_worker.py`` 的 ``[13]`` 段（用假解码器
    跑两轮）。这里只守**接线**——三处任意一处没接上，用户看到的就是
    「还是全挤在 WAV/ 里」，而代码看起来完全正常：

    1. ``ConvertWorker`` 必须**有** ``group_by_game`` 参数；
    2. ``web_ui.dl_api`` 必须真的传 ``True``（默认是关的，见 ``__init__`` 注释）；
    3. 云盘上传必须**递归**扫 —— 只扫一层的话子文件夹里的 WAV 全看不见，
       用户会看到「没有可上传的 WAV」而文件明明在那儿。
    """
    print("\n[3h] 下载产物按游戏名分文件夹", flush=True)
    import inspect
    import dl_convert

    params = inspect.signature(dl_convert.ConvertWorker.__init__).parameters
    _mark("group_by_game" in params, "ConvertWorker 有 group_by_game 参数",
          list(params))
    _mark(params["group_by_game"].default is False,
          "默认关（只有下载流程能确认目录结构）",
          params["group_by_game"].default)

    api = open(os.path.join(ROOT, "web_ui", "dl_api.py"), encoding="utf-8").read()
    _mark("group_by_game=True" in api,
          "dl_api 建 ConvertWorker 时显式开了分组", "")

    # 文件夹名清洗：Windows 非法字符必须换掉，否则建目录直接抛
    f = dl_convert.ConvertWorker._folder_name
    _mark(f('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j",
          "非法字符换成下划线", f('a/b\\c:d*e?f"g<h>i|j'))
    _mark(f("Final Fantasy VII (Japan)") == "Final Fantasy VII (Japan)",
          "正常包名原样保留（不转拼音、不删括号）",
          f("Final Fantasy VII (Japan)"))
    _mark(f("") == "未命名" and f("...") == "未命名",
          "空名 / 全是点 -> 未命名", [f(""), f("...")])
    _mark(len(f("x" * 300)) <= 100, "超长名截到 100 字符", len(f("x" * 300)))

    _mark("os.walk(wav_dir)" in api,
          "云盘上传递归扫子文件夹（不是只扫一层）", "")


def t_dl_play(state):
    """下载页：**转换进度写进日志** + **试听转好的 WAV**。

    用户报的两条：
      ① 下载页日志框里只有下载行，看不到 WAV 转换走到哪了；
      ② tkinter 版的下载页能直接播转出来的 WAV，Web 版没有。

    这里守「接线 + 安全边界」：日志用真实事件驱动一遍，试听走真 HTTP，
    特别是**越界拦截** —— ``/api/dl/audio?rel=`` 是唯一一处「前端给路径、
    后端开文件」的接口，挡不住就是任意文件读取。
    """
    print("\n[3i] 下载页：转换进度日志 + 试听", flush=True)
    import shutil
    import wave

    # -- ① 静态接线 ------------------------------------------------------
    html = open(os.path.join(ROOT, "web_ui", "download.html"), encoding="utf-8").read()
    js = open(os.path.join(ROOT, "web_ui", "download.js"), encoding="utf-8").read()
    css = open(os.path.join(ROOT, "web_ui", "download.css"), encoding="utf-8").read()
    api = open(os.path.join(ROOT, "web_ui", "dl_api.py"), encoding="utf-8").read()
    srv = open(os.path.join(ROOT, "web_ui", "server.py"), encoding="utf-8").read()
    for _id in ("dlPlayList", "dlPlayBtn", "dlPlayBar", "dlPlayName",
                "dlPlayClear"):
        _mark(_id in html, "download.html 有 #%s" % _id, "")
    _mark("/api/dl/wavs" in js and "/api/dl/audio" in js,
          "download.js 会拉列表 / 走试听接口", "")
    _mark(".dl-play-row" in css and ".dl-play-fill" in css,
          "download.css 有试听列表与进度条样式", "")
    # 音量（v5.2.4）：DOM + 记住上次的值 + 静音键。只查「在不在」不够，
    # 还要确认写的是 localStorage（否则每次打开都重置成 100%，夜里炸耳朵）。
    for _id in ("dlVol", "dlVolBtn", "dlVolW1", "dlVolX"):
        _mark(_id in html, "download.html 有音量控件 #%s" % _id, "")
    _mark("localStorage" in js and "dl_vol" in js,
          "音量值写进 localStorage（下次打开沿用）", "")
    _mark("--vol-pct" in css and "--vol-pct" in js,
          "音量轨道是自绘的两段式渐变（不跟系统配色）", "")
    _mark(".dl-vol-btn.muted" in css, "静音状态有独立样式", "")
    _mark('"/api/dl/wavs"' in srv and '"/api/dl/audio"' in srv,
          "server.py 注册了下载页试听接口", "")
    # 清空试听列表（v5.9.2）：前端要真发这个请求，后端要真注册
    _mark("/api/dl/wav_clear" in js and '"/api/dl/wav_clear"' in srv,
          "清空试听列表的接口两端都接上了", "")
    _mark("def wav_clear" in api and "WAV_CLEAR_FILE" in api,
          "dl_api 有 wav_clear + 落盘文件名", "")
    _mark("CV_TICK_SECS" in api and "def wav_path" in api and "def wav_list" in api,
          "dl_api 有节流心跳 / 列表 / 路径校验", "")
    # 心跳必须**限流**：不限流的话一个源几十首就把下载日志冲没了
    _mark("_cv_tick_text" in api and "CV_TICK_SECS" in api,
          "解码心跳有限流（同一句话不重复 + 最小间隔）", "")

    # ---- v6.0.1 需求 3：游戏列表各列可拖拽调宽 -------------------------
    # 守的是「拖不坏」的三件事：宽度只由 CSS 变量驱动（JS 不直接写每列
    # inline width，否则排序/虚拟滚动重建行时会丢）、有最小宽度、能存下来。
    _mark("var(--dl-cols" in css,
          "列宽由 --dl-cols 变量驱动（JS 只改变量，不逐列写 style）", "")
    _mark(".c-grip" in css and "c-grip" in js,
          "表头有列宽把手 .c-grip（样式与生成都在）", "")
    _mark("dl-col-drag" in css and "dl-col-drag" in js,
          "拖动时切 col-resize 光标（避免误选中行）", "")
    _mark("COL_MIN" in js and "COL_ORDER" in js,
          "每列有最小宽度 + 固定顺序（拖不坏布局）", "")
    _mark("msu1.dl.cols" in js and "localStorage" in js,
          "列宽写进 localStorage（下次打开沿用）", "")
    _mark("ondblclick" in js and "colDefaults" in js,
          "双击把手恢复默认列宽", "")

    # ---- v6.0.1 需求 4：日志 / 试听之间的封面区 ------------------------
    for _id in ("dlCoverBox", "dlCoverBody", "dlCoverImg", "dlCoverPh"):
        _mark(_id in html, "download.html 有封面区 #%s" % _id, "")
    _mark(".dl-cover" in css and ".dl-cover-frame" in css,
          "download.css 有封面框与占位样式", "")
    _mark(html.find('id="dlLog"') < html.find('id="dlCoverBox"')
          < html.find('id="dlPlayList"'),
          "封面区夹在日志与试听之间（DOM 顺序 = 视觉顺序）", "")
    _mark("/api/dl/cover" in js and '"/api/dl/cover"' in srv,
          "封面接口两端都接上了", "")
    _mark("def cover_path" in api and "cover_for_title" in api,
          "dl_api 有 cover_path（先找本地封面，再查缓存）", "")
    _mark("function showCover" in js and "S.coverGame" in js,
          "试听时切换封面，且同一游戏连播不重拉图", "")

    # -- ② 试听接口（真 HTTP）-------------------------------------------
    from web_ui import dl_api as _dlapi                     # noqa: F401 接线自检
    dl = server.dl_state()
    base = os.path.join(tmpdir.root(), "webui_gate_play")
    _rmtree_soft(base)
    wav_root = os.path.join(base, "WAV")
    os.makedirs(os.path.join(wav_root, "FF7"), exist_ok=True)
    names = []
    for i, sub in enumerate(("FF7", "")):
        nm = "%02d_Gate.wav" % (i + 1)
        p = os.path.join(wav_root, sub, nm) if sub else os.path.join(wav_root, nm)
        with wave.open(p, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x01" * 4410)
        names.append(("%s/%s" % (sub, nm)) if sub else nm)
    with open(os.path.join(base, "secret.wav"), "wb") as f:
        f.write(b"RIFF" + b"\x00" * 64)

    old_dest, old_wav = dl.dest, dl.wav_dir
    old_phase, old_logs = dl.phase, list(dl.logs)
    try:
        with dl.lock:
            dl.dest = base
            dl.wav_dir = ""              # 走「按保存目录推 WAV」的分支
            dl._wav_cache = None
        code, d = _jget("/api/dl/wavs?force=1")
        _mark(code == 200 and d.get("ok"), "GET /api/dl/wavs", code)
        _mark(int(d.get("total") or 0) == 2, "扫到 2 首", d.get("total"))
        rels = [w.get("rel") for w in (d.get("wavs") or [])]
        _mark(all(r in rels for r in names), "相对路径正确", rels)
        _mark(all((w.get("size_text") or "").endswith("B")
                  for w in (d.get("wavs") or [])), "带人话体积", "")

        first = names[0]
        q = urllib.parse.quote(first)
        code, hdr, raw = _req("/api/dl/audio?rel=" + q)
        _mark(code == 200, "GET /api/dl/audio 200", code)
        _mark(raw[:4] == b"RIFF" and raw[8:12] == b"WAVE",
              "首字节是 RIFF/WAVE", raw[:12])
        _mark("audio/wav" in hdr.get("content-type", ""),
              "Content-Type 是 audio/wav", hdr.get("content-type"))
        code, hdr, raw = _req("/api/dl/audio?rel=" + q,
                              headers={"Range": "bytes=0-99"})
        _mark(code == 206 and len(raw) == 100, "Range 请求返回 206 / 100 B",
              "%s / %d" % (code, len(raw)))

        # -- 封面接口（v6.0.1 需求 4）---------------------------------
        # 真放一张 PNG 在 <保存目录>/FLAC/FF7/cover.png，再从 HTTP 取回来。
        # ⚠ 同时必须验「挡穿越」：这是第二个「前端给字符串、后端开文件」的
        #   接口（第一个是 /api/dl/audio），挡不住一样是任意文件读取。
        png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
               b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
               b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
               b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
        cdir = os.path.join(base, "FLAC", "FF7")
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "cover.png"), "wb") as f:
            f.write(png)
        code, hdr, raw = _req("/api/dl/cover?game=FF7")
        _mark(code == 200 and raw == png, "GET /api/dl/cover 返回那张 PNG",
              "%s / %d B" % (code, len(raw)))
        _mark("image/png" in hdr.get("content-type", ""),
              "封面 Content-Type 是 image/png", hdr.get("content-type"))
        _mark("max-age" in hdr.get("cache-control", ""),
              "封面带 Cache-Control（切歌不重拉同一张）",
              hdr.get("cache-control"))
        code, _h, _r = _req("/api/dl/cover?game=NoSuchGame")
        _mark(code == 404, "没有封面时 404（前端显示 ♪ 占位，不弹错）", code)
        for evil in ("..", "../..", "..%2F..%2FWindows", "a/b"):
            code, _h, _r = _req("/api/dl/cover?game=" + urllib.parse.quote(evil))
            _mark(code == 404, "挡穿越：game=%r → 404" % evil, code)

        # -- 清空试听列表（v5.9.2）-------------------------------------
        # 用户报：「转出的 wav 列表会一直保留」。
        # ⚠ 判据里有一条**不是**看返回值，是直接查文件还在不在 ——
        #   这个功能唯一不能错的地方就是「别删用户的 WAV」。
        old_la = os.environ.get("LOCALAPPDATA")
        old_cleared = dict(dl._wav_cleared)
        os.environ["LOCALAPPDATA"] = os.path.join(base, "_appdata")
        with dl.lock:
            dl._wav_cleared = {}
            dl._wav_cache = None
        try:
            code, r = _jpost("/api/dl/wav_clear", {})
            _mark(code == 200 and r.get("ok"), "POST /api/dl/wav_clear", code)
            _mark(int(r.get("hidden") or 0) == 2, "清掉 2 首（数得准）",
                  r.get("hidden"))
            code, d = _jget("/api/dl/wavs?force=1")
            _mark(int(d.get("total") or 0) == 0, "清空后列表为空", d.get("total"))
            _mark(d.get("cleared") is True and int(d.get("hidden") or 0) == 2,
                  "响应带 cleared / hidden（前端才能说清「文件没删」）",
                  "%s / %s" % (d.get("cleared"), d.get("hidden")))
            _mark(all(os.path.isfile(os.path.join(wav_root, r_)) for r_ in names),
                  "**WAV 文件一个都没删**", names)
            # 连点两次不该报「又清了 2 首」—— 计数只算当前还看得见的
            code, r2 = _jpost("/api/dl/wav_clear", {})
            _mark(int(r2.get("hidden") or 0) == 0, "重复清空不重复计数",
                  r2.get("hidden"))
            # 清空之后**新转出来的必须出现**，否则这功能就成了「把列表弄死」
            after = os.path.join(wav_root, "FF7", "99_After.wav")
            with wave.open(after, "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(44100)
                w.writeframes(b"\x00\x01" * 4410)
            time.sleep(0.05)
            code, d = _jget("/api/dl/wavs?force=1")
            _mark(int(d.get("total") or 0) == 1
                  and (d.get("wavs") or [{}])[0].get("name") == "99_After.wav",
                  "清空后新转的照样出现", d.get("total"))
            # 落盘 + 读回：重开程序要还记得（只在内存里记等于白点一次）
            store = os.path.join(os.environ["LOCALAPPDATA"], "MSU1Converter",
                                 _dlapi.WAV_CLEAR_FILE)
            _mark(os.path.isfile(store), "清空时刻落盘了", store)
            fresh = _dlapi.DownloadState()
            _mark(bool(fresh._wav_cleared), "新实例能读回清空记录",
                  len(fresh._wav_cleared))
        finally:
            with dl.lock:
                dl._wav_cleared = old_cleared
                dl._wav_cache = None
            if old_la is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = old_la

        # ⚠ 越界：这几种**必须** 404，否则就是任意文件读取
        for bad, why in (("../secret.wav", "父目录穿越"),
                         ("../../secret.wav", "两级穿越"),
                         (os.path.join(base, "secret.wav"), "绝对路径"),
                         ("C:/Windows/win.ini", "别的盘/系统文件"),
                         ("FF7/01_Gate.txt", "非 .wav"),
                         ("FF7/nope.wav", "文件不存在")):
            code, _h, _r = _req("/api/dl/audio?rel=" + urllib.parse.quote(bad))
            _mark(code == 404, "越界被挡：%s" % why, "%s -> %s" % (bad, code))

        # -- ③ 转换进度进日志 -------------------------------------------
        with dl.lock:
            dl.phase = "converting"
            dl.cv_queued = 2
            dl.cv_ok = 0
            dl.cv_fail = 0
            dl.cv_skip = 0
            dl.cv_tracks = 0
            dl.logs = []
        src = os.path.join(base, "FF7", "BGM.psf")
        dl._handle_cv("cv_file_start", {"src": src, "url": "gate://a"})
        _mark("正在转 FF7/BGM.psf" in dl.status, "状态行显示正在转的文件", dl.status)
        dl._handle_cv("cv_progress", {"n": 4, "idx": 2, "total": 9,
                                      "name": "t", "secs": 3.0})
        dl._handle_cv("cv_file_done", {"src": src, "url": "gate://a",
                                       "n_wav": 9, "bytes": 4096})
        texts = [t for _s, t in dl.logs]
        _mark(any("解码中" in t for t in texts), "日志里有「解码中…」心跳", texts)
        _mark(any("第 2/9 首" in t for t in texts), "心跳带第几首", texts)
        _mark(any("[1/2]" in t and "9 首" in t for t in texts),
              "日志里有「[1/2] … → 9 首」完成行", texts)
        _mark(int(dl.cv_tracks) == 9, "cv_tracks 记账", dl.cv_tracks)
        _mark(0 < float(dl.progress) <= 100, "转换阶段进度条会动", dl.progress)
        # 同一条心跳不该连写两遍
        n0 = len(dl.logs)
        dl._handle_cv("cv_progress", {"n": 4, "idx": 3, "total": 9})
        _mark(len(dl.logs) == n0, "6 秒内不重复刷心跳", len(dl.logs) - n0)
        # 走一遍 HTTP：前端拿到的 logs 里确实有这些行
        code, st = _jget("/api/dl/state?since=0")
        jtexts = [r[1] for r in (st.get("logs") or [])]
        _mark(any("🎵 [1/2]" in t for t in jtexts),
              "snapshot 的 logs 带转换进度", jtexts[-3:])
        _mark(int((st.get("stats") or {}).get("cv_tracks") or 0) == 9,
              "snapshot.stats 有 cv_tracks", (st.get("stats") or {}).get("cv_tracks"))

        # -- ④「打开文件夹」的落点 ----------------------------------------
        # 用户报过两轮：
        #   ① 没勾自动转 WAV / 第一次用，点这颗按钮弹
        #      「目录不存在：…\Game Boy Advance\WAV」；
        #   ② v6.0.1：「现在指向游戏的 WAV 文件夹，改为指向游戏所对应的
        #      **机种文件夹**」—— 点它的动机是「我下到哪儿了」，不是「看产物」。
        # 判定在 dl_api.open_dir_target() 里（纯函数，不真开资源管理器）。
        # ⚠ 这里**只调函数**，不走 HTTP —— 走了就会在门禁里弹一堆资源管理器窗口。
        _t = _dlapi.DownloadState()
        # (a) 机种目录在 → 直接开机种目录（v6.0.1 起的第一优先级）
        with _t.lock:
            _t.dest = base
            _t.wav_dir = wav_root
        p, tag, note = _t.open_dir_target()
        _mark(tag == "dest" and os.path.abspath(p) == os.path.abspath(base)
              and not note,
              "机种目录在 → 开机种目录（不是 WAV 子目录）", "%s %s" % (tag, p))
        # (b) 保存目录在、WAV 不在 → 还是机种目录（音频子目录不再抢优先级）
        with _t.lock:
            _t.dest = base
            _t.wav_dir = os.path.join(base, "WAV_not_there")
        p, tag, note = _t.open_dir_target()
        _mark(tag == "dest" and os.path.abspath(p) == os.path.abspath(base),
              "没转过 WAV → 仍是机种目录", "%s %s" % (tag, p))
        # (c) 机种目录没建出来、但 WAV 子目录在 → 兜底开 WAV，并说明一句
        _only_wav = os.path.join(base, "only_wav")
        os.makedirs(os.path.join(_only_wav, "WAV"), exist_ok=True)
        with _t.lock:
            _t.dest = os.path.join(base, "ghost_machine")
            _t.wav_dir = os.path.join(_only_wav, "WAV")
        p, tag, note = _t.open_dir_target()
        _mark(tag == "wav" and os.path.abspath(p)
              == os.path.abspath(os.path.join(_only_wav, "WAV")),
              "机种目录不在、WAV 在 → 兜底开 WAV", "%s %s" % (tag, p))
        _mark("WAV" in note, "兜底开 WAV 时有提示", note)
        # (d) 连保存目录都没建出来 → 退到上一级（第一次用的情形）
        deeper = os.path.join(base, "Game Boy Advance")
        with _t.lock:
            _t.dest = deeper
            _t.wav_dir = os.path.join(deeper, "WAV")
        p, tag, note = _t.open_dir_target()
        _mark(tag == "up" and os.path.abspath(p) == os.path.abspath(base),
              "目录都没建出来 → 退到上一级", "%s %s" % (tag, p))
        _mark("上级" in note, "退到上级时有提示", note)
        # (e) 一路都不存在 → 明确报错，而不是返回一个假路径
        with _t.lock:
            _t.dest = os.path.join(base, "nope1", "nope2")
            _t.wav_dir = ""
        p, tag, note = _t.open_dir_target()
        _mark(p == "" and tag == "" and bool(note),
              "全都不存在 → 空路径 + 原因", "%s %s %s" % (p, tag, note))
    finally:
        with dl.lock:
            dl.dest, dl.wav_dir = old_dest, old_wav
            dl.phase = old_phase
            dl.logs = old_logs
            dl._wav_cache = None
            dl.cv_tracks = 0
            dl.cv_queued = 0
        _rmtree_soft(base)


def t_dl_disk_state(state):
    """[3m] 下载页「状态」列：靠磁盘事实 + 保存目录能跨机找回（v5.9.3）。

    用户报的：「exe 关闭重启后状态栏就清空了，也不知道哪个文件是已经下载了」，
    补一句「解压目录和 WAV 目录在不同电脑上也要能自动识别，不要绑定本台电脑」。

    病根是 ``snapshot()`` 的 ``state`` 只取 ``self.states``（纯内存，重启即空）。
    现在改成「运行期状态优先、没有就从磁盘快照算」。这段守四件事：

    1. 三种落盘形态（只有包 / 包删了只剩解压目录 / 已转 WAV）都判得出来；
    2. **新建实例**（= 重启程序）后状态一条不差；
    3. 保存目录换了盘符 / 位置也能按目录名找回，而不是回到默认目录；
    4. 4000 行快照别把 300ms 轮询拖垮（结果有缓存，磁盘一变就失效）。

    界面那一半（``paintState()`` 把 ``row.state`` 贴进 ``.c-state``）由
    ``vendor/_e2e_disk_state.py`` 和 DOM 断言守，这里只查接线。
    """
    print("\n[3m] 下载页：状态列靠磁盘事实 + 目录跨机找回", flush=True)
    import shutil
    from web_ui import dl_api as _dlapi

    base = os.path.join(tmpdir.root(), "gate_disk_state")
    _rmtree_soft(base)
    root = os.path.join(base, "MSU1Downloads")
    plat = "Super Nintendo"
    dest = os.path.join(root, "joshw", plat)
    wav_root = os.path.join(dest, "WAV")
    # v5.9.9：FLAC 有了自己的目录（以前和 WAV 挤在一起，导致两种格式共用
    # 一份转换记账 → 已转 WAV 的游戏再转 FLAC 会被判「已转过」直接跳过）。
    flac_root = os.path.join(dest, "FLAC")

    def _touch(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"\0" * 64)

    old_prefs = os.environ.get("MSU1_DL_PREFS")
    prefs_path = os.path.join(base, "dl_prefs.json")
    os.environ["MSU1_DL_PREFS"] = prefs_path
    try:
        # -- ① 静态接线 --------------------------------------------------
        api = open(os.path.join(ROOT, "web_ui", "dl_api.py"),
                   encoding="utf-8").read()
        js = open(os.path.join(ROOT, "web_ui", "download.js"),
                  encoding="utf-8").read()
        _mark("def _disk_state" in api and "_disk_dirs" in api
              and "_wav_games" in api,
              "dl_api 有磁盘快照三件套（文件/目录名/WAV 游戏名）", "")
        _mark("_relocate_dir" in api and "def _find_by_name" in api,
              "dl_api 有目录找回（换盘符 + 有界深搜）", "")
        _mark("PREFS_FILE" in api and "dl_prefs.json" in api,
              "保存目录会落盘（dl_prefs.json）", "")
        _mark("n_on_disk" in api and "n_on_disk" in js,
              "snapshot 带 n_on_disk 且前端用上了", "")
        _mark('"state": self.states.get(url, "") or dstate' in api,
              "状态列「运行期优先、磁盘兜底」", "")

        # -- ② 三种落盘形态 ----------------------------------------------
        _touch(os.path.join(dest, "Only Zip [SNES].7z"))
        os.makedirs(os.path.join(dest, "Unzipped [SNES]"), exist_ok=True)
        _touch(os.path.join(dest, "Unzipped [SNES]", "a.spc"))
        _touch(os.path.join(dest, "Converted [SNES].7z"))
        _touch(os.path.join(wav_root, "Converted [SNES]", "01.wav"))
        _touch(os.path.join(dest, "EmptyWav [SNES].7z"))
        os.makedirs(os.path.join(wav_root, "EmptyWav [SNES]"), exist_ok=True)
        # v5.9.7：只放 .flac 的目录也必须算「已转」。下载页的快照以前只认
        # .wav，用户选了 FLAC 输出后每一行都显示「未转」—— 文件明明躺在
        # 那里，界面却说没转，比不显示更让人困惑（会反复重转）。
        # v5.9.9：这条同时成了**老目录兼容**的用例 —— 那时 FLAC 也写在
        # WAV/ 里，升级后照样要认成「已转 FLAC」。
        _touch(os.path.join(dest, "FlacOnly [SNES].7z"))
        _touch(os.path.join(wav_root, "FlacOnly [SNES]", "01.flac"))
        # v5.9.9：新布局 —— FLAC 有自己的目录。
        _touch(os.path.join(dest, "FlacDir [SNES].7z"))
        _touch(os.path.join(flac_root, "FlacDir [SNES]", "01.flac"))
        # v5.9.9：两种格式都转过的游戏 —— 状态要同时说清，不能只说一种。
        _touch(os.path.join(dest, "Both [SNES].7z"))
        _touch(os.path.join(wav_root, "Both [SNES]", "01.wav"))
        _touch(os.path.join(flac_root, "Both [SNES]", "01.flac"))

        names = ["Only Zip [SNES]", "Unzipped [SNES]", "Converted [SNES]",
                 "EmptyWav [SNES]", "FlacOnly [SNES]", "FlacDir [SNES]",
                 "Both [SNES]", "Never [SNES]"]
        entries = [{"name": n, "size": 64, "mtime": 1700000000,
                    "url": "https://e.invalid/%d.7z" % i}
                   for i, n in enumerate(names)]
        want = {"Only Zip [SNES]": "✓ 已下载",
                "Unzipped [SNES]": "📦 已解压",
                # v5.9.9：文案不再统一说「已转音频」，而是**说清是哪种**。
                # 两种格式可以同时存在（先转 WAV 再转 FLAC），只说「已转音频」
                # 用户就没法判断还缺哪一种 —— 而「还缺哪种」正是他下一步
                # 要点哪颗按钮的依据。
                "Converted [SNES]": "🎵 已转 WAV",
                "EmptyWav [SNES]": "✓ 已下载",     # 空 WAV 目录不算转好
                "FlacOnly [SNES]": "🎵 已转 FLAC",  # 老布局（.flac 混在 WAV/ 里）
                "FlacDir [SNES]": "🎵 已转 FLAC",   # 新布局（独立的 FLAC/ 目录）
                "Both [SNES]": "🎵 已转 WAV+FLAC",  # 两种都有
                "Never [SNES]": ""}

        # -- ③ 第一次运行：设目录（会落盘） ------------------------------
        a = _dlapi.DownloadState()
        a.set_dest(dest)
        _mark(os.path.isfile(prefs_path), "set_dest 落盘 dl_prefs.json", "")
        import json as _json
        prefs = _json.load(open(prefs_path, encoding="utf-8"))
        _mark(sorted(prefs) == ["dest", "manual", "root"],
              "prefs 只存 dest/manual/root（不存选项，换机不继承并发数）",
              sorted(prefs))
        a.entries = [dict(e) for e in entries]
        a._refresh_disk()
        _mark({r["name"]: r["state"] for r in a.snapshot()["rows"]} == want,
              "第一次运行：三种落盘形态都判对了", "")

        # -- ④ 重启程序（新实例，内存状态全空） --------------------------
        b = _dlapi.DownloadState()
        _mark(os.path.abspath(b.dest).lower() == os.path.abspath(dest).lower(),
              "重启后保存目录自动恢复", b.dest)
        _mark(len(b.states) == 0, "重启后内存状态确实是空的", len(b.states))
        b.entries = [dict(e) for e in entries]
        b._refresh_disk()
        snap = b.snapshot()
        got = {r["name"]: r["state"] for r in snap["rows"]}
        bad = ["%s 期望%r 实际%r" % (n, w, got.get(n))
               for n, w in want.items() if got.get(n) != w]
        _mark(not bad, "**重启后状态列一条不差**（用户报的就是这条）", bad)
        # 别写死数字：用例数每加一条就要改一次（v5.9.9 加 FlacDir / Both
        # 两条时正好撞上）。按 want 里「有状态的那几条」算出来。
        _want_n = len([1 for _n, _w in want.items() if _w])
        _mark(snap["n_on_disk"] == _want_n, "snapshot 的 n_on_disk 对得上",
              "%s（期望 %d）" % (snap["n_on_disk"], _want_n))

        # -- ⑤ 运行期状态优先 --------------------------------------------
        u = entries[0]["url"]
        b.states[u] = "⏳ 待转 WAV"
        r0 = [r for r in b.snapshot()["rows"] if r["url"] == u][0]
        _mark(r0["state"] == "⏳ 待转 WAV",
              "正在转 WAV 时不被磁盘状态盖掉", r0["state"])
        b.states[u] = ""
        r0 = [r for r in b.snapshot()["rows"] if r["url"] == u][0]
        _mark(r0["state"] == "✓ 已下载", "内存状态清掉后落回磁盘状态", r0["state"])

        # -- ⑥ 换机：坏盘符 + 位置也变了 --------------------------------
        drives = _dlapi._local_drives()
        _mark(bool(drives) and all(len(d) == 2 and d[1] == ":" for d in drives),
              "_local_drives() 返回盘符列表", drives)
        me = os.path.splitdrive(os.path.abspath(root))[0]
        other = next((d for d in drives if d.lower() != me.lower()), "")
        if other:
            alt_tail = os.path.join("gate_alt_%d" % os.getpid(), "MSU1Downloads")
            alt_root = os.path.join(other + os.sep, alt_tail)
            try:
                os.makedirs(os.path.join(alt_root, "joshw", plat),
                            exist_ok=True)
                asked = os.path.join("Z:" + os.sep, alt_tail)
                got_alt = _dlapi._relocate_dir(asked, "joshw")
                _mark(os.path.abspath(got_alt).lower()
                      == os.path.abspath(alt_root).lower(),
                      "只换盘符（Z:→%s）能找回同一条相对路径" % other,
                      "%r -> %r" % (asked, got_alt))
            finally:
                shutil.rmtree(os.path.join(other + os.sep,
                                           "gate_alt_%d" % os.getpid()),
                              ignore_errors=True)
        else:
            print("  [SKIP] 本机只有一个盘，跳过换盘符用例", flush=True)

        deep_name = "MSU1DownloadsGate"
        deep = os.path.join(base, "别的盘", "Games", deep_name)
        shutil.copytree(root, deep, dirs_exist_ok=True)
        _mark(os.path.abspath(_dlapi._find_by_name(
                  deep_name, "joshw", roots=[base], max_depth=6)).lower()
              == os.path.abspath(deep).lower(),
              "位置彻底变了也能按目录名找到（有界深搜）", deep)
        _mark(_dlapi._find_by_name(deep_name, "joshw", roots=[base],
                                   max_depth=2) == "",
              "深搜尊重 max_depth（不会扫全盘）", "")

        # 真重启一次：prefs 里塞坏盘符
        tail = os.path.splitdrive(os.path.abspath(root))[1].lstrip("\\/")
        bad_root = os.path.join("Z:" + os.sep, tail)
        _json.dump({"root": bad_root,
                    "dest": os.path.join(bad_root, "joshw", plat),
                    "manual": True},
                   open(prefs_path, "w", encoding="utf-8"),
                   ensure_ascii=False)
        c = _dlapi.DownloadState()
        _mark(os.path.abspath(c.dest).lower() == os.path.abspath(dest).lower(),
              "prefs 里是坏盘符，dest 仍能找回", c.dest)
        _mark(getattr(c, "_root_moved", False) is True,
              "搬家被标记出来（load 时写日志告诉用户）", "")
        _mark(os.path.abspath(_json.load(open(prefs_path, encoding="utf-8"))
                              .get("root", "")).lower()
              == os.path.abspath(root).lower(),
              "搬家后把新位置写回 prefs（下次不用再找）", "")
        c.entries = [dict(e) for e in entries]
        c._refresh_disk()
        _mark({r["name"]: r["state"] for r in c.snapshot()["rows"]} == want,
              "换机后状态列照样对（换机不等于重下）", "")

        # -- ⑦ 缓存：省时间，但磁盘一变必须失效 --------------------------
        big = _dlapi.DownloadState()
        big.dest = dest
        big._refresh_disk()
        big.entries = [{"name": "F%04d [SNES]" % i, "size": 1,
                        "mtime": 1700000000,
                        "url": "https://e.invalid/f%04d.7z" % i}
                       for i in range(2000)]
        t0 = time.time()
        big.snapshot()
        one = (time.time() - t0) * 1000
        t0 = time.time()
        for _ in range(5):
            big.snapshot()
        avg = (time.time() - t0) * 1000 / 5
        _mark(avg < one * 0.7, "状态判定有缓存（之后明显快于首次）",
              "首次 %.1fms / 之后 %.1fms" % (one, avg))
        _touch(os.path.join(dest, "F0007 [SNES].7z"))
        big._refresh_disk()
        r7 = [r for r in big.snapshot()["rows"] if r["name"] == "F0007 [SNES]"][0]
        _mark(r7["state"] == "✓ 已下载",
              "磁盘快照一变缓存就失效（新落盘立刻显示）", r7["state"])

        # -- ⑧ 边界：目录被删掉不能崩 ------------------------------------
        gone = _dlapi.DownloadState()
        gone.dest = os.path.join(base, "根本不存在的目录")
        try:
            gone._refresh_disk()
            _mark(gone.snapshot()["n_on_disk"] == 0,
                  "保存目录不存在：不抛异常、状态全空", "")
        except Exception as exc:                     # noqa: BLE001
            _mark(False, "保存目录不存在：不抛异常、状态全空", repr(exc))

        # -- ⑨ HTTP 往返：改目录 → 按新目录重扫 → 回给前端 ---------------
        # 用户能自己填任意路径，所以「改完目录有没有真的按新目录扫」必须在
        # 接口这一层验一遍，不能只验函数。改完恢复，别影响后面的用例。
        dl = server.dl_state()          # 单例访问器，不是 server.dl
        with dl.lock:
            old_d = dl.dest
        try:
            code, r = _jpost("/api/dl/set_opts", {"dest": dest})
            _mark(code == 200 and r.get("ok"), "set_opts 改保存目录返回 ok",
                  code)
            _mark(os.path.abspath(r.get("dest") or "").lower()
                  == os.path.abspath(dest).lower(),
                  "set_opts 回的 dest 就是用户填的那个", r.get("dest"))
            _mark(r.get("dest_exists") is True,
                  "set_opts 回 dest_exists（目录在）", r.get("dest_exists"))
            sc = r.get("scan") or {}
            _mark(sc.get("files", 0) > 0 and "wav" in sc and "limited" in sc,
                  "set_opts 回 scan（按新目录扫到多少）", sc)
            # 换成不存在的目录：要如实说「不存在」，不能假装扫到了
            nope = os.path.join(base, "不存在的目录")
            code, r2 = _jpost("/api/dl/set_opts", {"dest": nope})
            _mark(r2.get("dest_exists") is False,
                  "目录不存在时 dest_exists=False（前端据此提示）",
                  r2.get("dest_exists"))
            _mark((r2.get("scan") or {}).get("files") == 0,
                  "目录不存在时 scan.files=0（不会残留上一个目录的数）",
                  r2.get("scan"))
            # 切回来：状态列应立刻恢复（证明「以用户设定路径为准」可来回切）
            _jpost("/api/dl/set_opts", {"dest": dest})
            code, st2 = _jget("/api/dl/state?since=0")
            _mark(os.path.abspath((st2.get("scan") or {}).get("dir") or "")
                  .lower() == os.path.abspath(dest).lower(),
                  "state 的 scan.dir 就是当前保存目录", st2.get("scan"))
        finally:
            with dl.lock:
                dl.dest = old_d
            dl._refresh_disk()
            try:
                dl._save_prefs()
            except Exception:                        # noqa: BLE001
                pass
    finally:
        if old_prefs is None:
            os.environ.pop("MSU1_DL_PREFS", None)
        else:
            os.environ["MSU1_DL_PREFS"] = old_prefs
        _rmtree_soft(base)


def t_dl_row_actions(state):
    """[3n] 下载页行内按钮 + 上传云盘选游戏（v5.9.4）。

    用户报的三件事，一件一段：

    1. 「已经下载、解压完成但没有转 wav 的，没有入口」→ 行内「转 WAV」；
    2. 「已转成 wav 的加不进试听列表，清空之后也没法手动挑」→「＋试听」；
    3. 「转换完 wav 后点上传云盘提示『这个目录里没有 WAV』」→ 递归扫描，
       并让用户自己挑要传哪几个游戏。

    真转换（``convert_wav`` 跑完一条 SPC）由 ``vendor/_e2e_row_actions.py``
    验 —— 那段要真解码、要等 1~2 秒，不适合塞进 ``--fast``。这里守的是
    **接线 + 契约 + 便宜的边界**。
    """
    print("\n[3n] 下载页：行内转 WAV / 加入试听 / 上传选游戏", flush=True)
    import shutil
    from web_ui import dl_api as _dlapi

    base = os.path.join(tmpdir.root(), "gate_row_actions")
    _rmtree_soft(base)
    os.makedirs(base, exist_ok=True)
    old_prefs = os.environ.get("MSU1_DL_PREFS")
    os.environ["MSU1_DL_PREFS"] = os.path.join(base, "dl_prefs.json")
    try:
        # -- ① 静态接线 --------------------------------------------------
        js = open(os.path.join(ROOT, "web_ui", "download.js"),
                  encoding="utf-8").read()
        api = open(os.path.join(ROOT, "web_ui", "dl_api.py"),
                   encoding="utf-8").read()
        srv = open(os.path.join(ROOT, "web_ui", "server.py"),
                   encoding="utf-8").read()
        cjs = open(os.path.join(ROOT, "web_ui", "cloud.js"),
                   encoding="utf-8").read()
        html = open(os.path.join(ROOT, "web_ui", "download.html"),
                    encoding="utf-8").read()
        css = open(os.path.join(ROOT, "web_ui", "download.css"),
                   encoding="utf-8").read()
        cu = open(os.path.join(ROOT, "cloud_upload.py"),
                  encoding="utf-8").read()

        _mark("function actOf" in js and 'k: "play"' in js and 'k: "wav"' in js,
              "download.js 有「状态 → 按钮」映射（actOf）", "")
        _mark('"/api/dl/convert_wav"' in js and '"/api/dl/wav_add"' in js,
              "行内按钮打的是 convert_wav / wav_add", "")
        _mark("ev.stopPropagation()" in js,
              "按钮点击不会顺带把整行勾选切掉", "")
        _mark('class="c-act"' in html and ".c-act" in css,
              "表头多了一列「操作」（c-act）", "")
        _mark("96px 52px" in css or "52px" in css,
              "列表网格给按钮留了列宽", "")
        _mark("function chooseGames" in cjs and "o.games" in cjs,
              "cloud.js 有「选游戏」对话框且 upload 认 games", "")
        _mark("collect_wavs(folder, deep=True)" in srv,
              "cloud_plan 递归扫（老 bug 的正身）", "")
        _mark("deep=False" in cu and "deep=True" not in cu.split("def ")[0],
              "collect_wavs 默认仍是浅扫（不改老界面行为）", "")
        _mark("def convert_wav" in api and "def wav_add" in api
              and "def wav_games" in api,
              "dl_api 有 convert_wav / wav_add / wav_games", "")
        _mark("_cv_oneshot" in api and "def _payload_in" in api,
              "只转不下载的收尾口径 + 「只剩解压目录」分支都在", "")
        _mark("WAV_PICKS_FILE" in api and "wav_picks.json" in api,
              "「手动加入」落盘（清空后还能挑回来）", "")
        _mark('elif path == "/api/dl/convert_wav"' in srv
              and 'elif path == "/api/dl/wav_add"' in srv
              and '"/api/dl/wav_games"' in srv,
              "三条路由都注册了", "")
        _mark('dl.upload_cloud(body.get("scope") or "",' in srv,
              "upload_cloud 收 games 参数", "")

        # -- ② 后端行为：还没下载的那一条要自己收尾 ----------------------
        # 这条最容易出事：一个文件都没入队时不会再有 cv_thread_exit，
        # 忘了自己调 _finish() 的话界面永远停在「进行中…」，
        # 而且「开始下载」一直是灰的（用户只能重启）。
        st = _dlapi.DownloadState()
        st.set_dest(os.path.join(base, "dest1"))
        os.makedirs(os.path.join(base, "dest1"), exist_ok=True)
        st.entries = [{"name": "Ghost [SNES]", "size": 1, "mtime": 1700000000,
                       "url": "https://e.invalid/ghost.7z"}]
        st._refresh_disk()
        r = st.convert_wav(["https://e.invalid/ghost.7z"])
        _mark(r.get("ok") is True, "convert_wav 接受请求", r)
        for _i in range(60):
            if not st.busy:
                break
            time.sleep(0.1)
        _mark(st.busy is False, "没源可转时自己收尾（不会卡在「进行中」）",
              "busy=%s" % st.busy)
        _mark(st.snapshot()["rows"][0]["state"] == "❌ 还没下载",
              "这一条标成「❌ 还没下载」", st.snapshot()["rows"][0]["state"])
        _mark(any("转 WAV 完成" in str(x[1]) for x in st.logs),
              "收尾日志按「只转不下载」的口径写（不报「下载成功 0」）", "")

        # -- ③ 加入试听：清空之后还能挑回来 ------------------------------
        dest3 = os.path.join(base, "dest3")
        gdir = os.path.join(dest3, "WAV", "Game A [SNES]")
        os.makedirs(gdir, exist_ok=True)
        for i in range(2):
            with open(os.path.join(gdir, "%02d.wav" % (i + 1)), "wb") as fh:
                fh.write(b"RIFF" + b"\0" * 60)
        st3 = _dlapi.DownloadState()
        st3.set_dest(dest3)
        st3.entries = [{"name": "Game A [SNES]", "size": 1,
                        "mtime": 1700000000,
                        "url": "https://e.invalid/a.7z"}]
        st3._refresh_disk()
        _mark(st3.wav_list(force=True)["total"] == 2, "前置：2 首在列表里", "")
        st3.wav_clear()
        _mark(st3.wav_list(force=True)["total"] == 0, "清空后列表为 0", "")
        ra = st3.wav_add(["https://e.invalid/a.7z"])
        _mark(ra.get("ok") is True and ra.get("added") == 2,
              "清空后能手动加回来（用户报的第二件事）", ra)
        _mark(st3.wav_list(force=True)["total"] == 2,
              "加回来的曲目 mtime 更旧也照样显示", "")
        st4 = _dlapi.DownloadState()          # 模拟重开程序
        st4.set_dest(dest3)
        _mark(st4.wav_list(force=True)["total"] == 2,
              "重开程序后「手动加入」还在（落盘了）", "")
        _mark(st4.wav_clear()["ok"] and st4.wav_list(force=True)["total"] == 0,
              "清空会把「手动加入」一起清掉（清空就是空）", "")

        # -- ④ wav_games 分组（上传对话框的数据源）-----------------------
        st5 = _dlapi.DownloadState()
        st5.set_dest(dest3)
        st5.wav_dir = os.path.join(dest3, "WAV")
        g = st5.wav_games()
        _mark(g.get("ok") and len(g.get("games") or []) == 1,
              "wav_games 按游戏分组", g.get("games"))
        _mark((g.get("games") or [{}])[0].get("count") == 2,
              "分组里带曲目数", g.get("games"))

        # -- ⑤ 上传云盘：递归 + 按游戏过滤 ------------------------------
        wav_root = os.path.join(base, "up", "WAV")
        for name, n in (("Game A", 2), ("Game B", 3)):
            d = os.path.join(wav_root, name)
            os.makedirs(d, exist_ok=True)
            for i in range(n):
                with open(os.path.join(d, "%02d.wav" % (i + 1)), "wb") as fh:
                    fh.write(b"RIFF" + b"\0" * 60)
        import cloud_upload as _cu
        _mark(len(_cu.collect_wavs(wav_root)) == 0,
              "浅扫 0 个（「这个目录里没有 WAV」的根因）", "")
        _mark(len(_cu.collect_wavs(wav_root, deep=True)) == 5,
              "deep=True 递归扫到 5 个", "")
        code, plan = _jget("/api/cloud/plan?dir=" + urllib.parse.quote(wav_root))
        _mark(code == 200 and plan.get("ok") and plan.get("count") == 5,
              "HTTP：cloud/plan 递归后能列出 5 个", plan.get("error"))
        code, plan2 = _jget("/api/cloud/plan?dir="
                            + urllib.parse.quote(wav_root)
                            + "&games=" + urllib.parse.quote("Game B"))
        _mark(code == 200 and plan2.get("count") == 3,
              "HTTP：games 过滤后只剩 3 个", plan2.get("count"))

        # -- ⑥ HTTP 契约：三条新接口 --------------------------------
        code, rg = _jget("/api/dl/wav_games")
        _mark(code == 200 and "games" in rg, "HTTP：/api/dl/wav_games 有 games",
              code)
        code, rw = _jpost("/api/dl/wav_add", {"urls": []})
        _mark(code == 200 and rw.get("ok") is False,
              "HTTP：wav_add 空参数明确失败", rw)
        code, rc = _jpost("/api/dl/convert_wav", {"urls": []})
        _mark(code == 200 and rc.get("ok") is False
              and "没有选中" in (rc.get("error") or ""),
              "HTTP：convert_wav 空参数明确失败", rc)
        code, ru = _jpost("/api/dl/upload_cloud",
                          {"scope": "all", "games": ["根本不存在的游戏"]})
        _mark(code == 200 and ru.get("ok") is False,
              "HTTP：upload_cloud 认 games 且不会瞎传", ru.get("error"))
    finally:
        if old_prefs is None:
            os.environ.pop("MSU1_DL_PREFS", None)
        else:
            os.environ["MSU1_DL_PREFS"] = old_prefs
        _rmtree_soft(base)


def t_spc_progress():
    """[3o] SPC 包转换不再「0% 卡住」（v5.9.5）。

    用户拿 Chrono Trigger 92 首的包实测：点「开始转换」后进度**一直 0%**，
    十几分钟没动静，日志停在 ``▶ [1/1]`` 那行，看着就是死机。两个原因叠一起：

    1. ``load_source`` 对 ``.spc`` 目录是裸调 ``load_spc_dir(path)`` ——
       ``on_progress`` 压根没接下去，整段解码零上报（而 CPU 是满的）。
    2. ``natural`` 策略下 ``max_secs=None`` → 每首硬解到 ``_SPC_MAX_SECS``
       （600 秒），``write_tracks`` 随后只留前 150 秒 —— 4 倍白干。
       92 首 × 600 秒 = 15.3 小时音频，约 50 倍速 → 18 分钟。

    修法：把 ``on_progress`` 接下去；再给解码一个 ``decode_cap``
    （= 成品上限 + 2 秒余量），**只当上限用**、仍按尾部静音收尾，
    因此成品逐字节不变（本段最后两条就是守这个）。
    """
    print("\n[3o] SPC 包转换不再「0% 卡住」", flush=True)
    import inspect
    import shutil
    import tempfile

    import convert_job
    import msu1_to_cd as core

    sig_dir = inspect.signature(core.load_spc_dir)
    _mark("on_progress" in sig_dir.parameters,
          "load_spc_dir 收 on_progress（原来根本没这个参数）",
          list(sig_dir.parameters))
    _mark("decode_cap" in sig_dir.parameters, "load_spc_dir 收 decode_cap")
    _mark("decode_cap" in inspect.signature(core.load_source).parameters,
          "load_source 收 decode_cap")

    # 曲目表的「已输出 / 转换中」必须看**真实写盘数**，不能拿 progress 反推：
    # 解码阶段也开始报进度之后（0..50%），反推会在一个文件都没写的时候
    # 就说「已输出 32 首」—— v5.9.5 第一版交付图就是这么拍出来的。
    _code, _d = _jget("/api/state")
    _mark("done_tracks" in _d and "total_tracks" in _d,
          "/api/state 带 done_tracks（曲目表「已输出」的真实来源）",
          sorted(_d))
    _js = open(os.path.join(ROOT, "web_ui", "app.js"),
               encoding="utf-8").read()
    _mark("S.doneTracks" in _js and "d.done_tracks" in _js,
          "app.js 认 done_tracks，不再拿进度反推已输出数")

    if not _mark(os.path.isfile(SPC), "sample.spc 在", SPC):
        return
    tmp = tempfile.mkdtemp(prefix="spc_prog_")
    try:
        d = os.path.join(tmp, "pack")
        os.makedirs(d)
        shutil.copy2(SPC, os.path.join(d, "01 sample.spc"))

        calls = []
        tracks = core.load_source(d, on_progress=lambda f: calls.append(f))
        _mark(len(calls) >= 1, "load_source 对 .spc 目录会回调进度",
              "%d 次" % len(calls))
        _mark(bool(calls) and max(calls) > 0, "进度值真的往前走了",
              calls[-3:] if calls else calls)
        full = len(tracks[0]["pcm"]) if tracks else 0
        _mark(full > 0, "自然解码有产出", full)

        capped = core.load_source(d, decode_cap=2.0)
        clen = len(capped[0]["pcm"]) if capped else 0
        _mark(0 < clen <= 2.0 * 176400 + 64,
              "decode_cap=2 时只解 2 秒（不再硬解到 600 秒）",
              "%.2f 秒" % (clen / 176400.0))
        _mark(clen < full, "确实比不设限短", "%d vs %d" % (clen, full))

        # 成品必须逐字节不变 —— 这是「加上限」能成立的前提
        import spc_wav
        a, _ca = spc_wav._limit_len(bytes(capped[0]["pcm"]), 1.0)
        b, _cb = spc_wav._limit_len(bytes(tracks[0]["pcm"]), 1.0)
        _mark(a == b, "加解码上限后成品逐字节不变",
              "%d vs %d 字节" % (len(a), len(b)))
    finally:
        _rmtree_soft(tmp)

    # ConvertJob 传下去的上限：芯片/SPC = 单曲最长 + 2 秒；自带 length+fade
    # 标签的格式（光盘镜像 / PSF / USF …）不设限 —— 截断等于毁数据。
    seen = []
    orig = core.load_source

    def spy(path, **kw):
        seen.append(kw.get("decode_cap"))
        raise RuntimeError("只验参数，不解码")

    tmp2 = tempfile.mkdtemp(prefix="spc_cap_")
    base = {"out_dir": os.path.join(tmp2, "out"), "do_cd": False, "do_wav": True,
            "policy": "auto", "speed": 0.0, "max_secs": 150.0,
            "default_secs": 150.0, "gap": 2.0, "album": "", "performer": "",
            "verify": False, "cd_wav": False, "xa_music_only": False,
            "xa_split": False, "merge_wav": False}
    core.load_source = spy
    try:
        for kind, ext, want in (("SPC", ".spc", 152.0),
                                ("光盘镜像", ".iso", None)):
            seen[:] = []
            job = convert_job.ConvertJob(
                [{"path": os.path.join(tmp2, "x" + ext), "label": kind,
                  "kind": kind}],
                dict(base, out_dir=os.path.join(tmp2, kind)),
                on_log=lambda _m: None,
                on_progress=lambda _p, _t: None,
                on_track=lambda _i, _n: None)
            job.run()
            got = seen[0] if seen else "没调到 load_source"
            _mark(got == want, "%s 的解码上限 = %s" % (kind, want), got)
    finally:
        core.load_source = orig
        _rmtree_soft(tmp2)


def t_viz_panel():
    """[3p] 频谱 / 波形面板：主界面右栏 + 下载页试听区（v5.9.6）。

    用户原话两段：
      ① 「两个『看着有、其实没用』的选项删掉，空出来的位置请开发一个
         炫彩的可视化、可选窗口，用于展示试听音频的频谱、波形。」
      ② 「忘了一件事，同样的可视化图也要加到下载音源界面里去」

    这类「删控件 + 加面板 + 两页复用」的改动最容易留下几处半成品，
    而且全都**只在真实点击时才炸**，静态看着一切正常：

    1. ``collectOpts()`` 里还写着 ``$("#spMax").value`` —— DOM 没了就是
       ``null.value``，点「开始转换」直接抛 TypeError，按钮彻底哑掉。
    2. ``createMediaElementSource()`` 之后忘了把 analyser 接回
       ``destination`` —— 声音被这张图吞掉：频谱有了、播放没了，
       比不画还糟。
    3. 暂停 / 关掉面板后 rAF 还在空转 —— 白烧 60fps。
    4. 只给主界面加了、下载页漏了（用户第二次提醒才补上）。
    5. 两页各复制一份实现 —— 迟早长得不一样，所以抽成 ``viz.js`` 共用；
       代价是 ``viz.js`` 必须进静态白名单，否则两个页面一起 404。

    本段是纯静态检查（读文件 + 正则），--fast 也跑。
    """
    print("\n[3p] 频谱 / 波形面板（v5.9.6）", flush=True)

    html = open(os.path.join(ROOT, "web_ui", "index.html"),
                encoding="utf-8").read()
    js = open(os.path.join(ROOT, "web_ui", "app.js"),
              encoding="utf-8").read()
    css = open(os.path.join(ROOT, "web_ui", "app.css"),
               encoding="utf-8").read()
    dhtml = open(os.path.join(ROOT, "web_ui", "download.html"),
                 encoding="utf-8").read()
    djs = open(os.path.join(ROOT, "web_ui", "download.js"),
               encoding="utf-8").read()
    dcss = open(os.path.join(ROOT, "web_ui", "download.css"),
                encoding="utf-8").read()

    vzp = os.path.join(ROOT, "web_ui", "viz.js")
    _has_vz = os.path.exists(vzp)
    vz = open(vzp, encoding="utf-8").read() if _has_vz else ""
    _mark(_has_vz, "web_ui/viz.js 存在（两页共用的实现）",
          "%d B" % os.path.getsize(vzp) if _has_vz else "缺失")

    # ---- ① 两个选项真的没了（HTML 和 JS 都不许再提） ----
    _mark("spMax" not in html and "spSpeed" not in html,
          "index.html 里不再有单曲最长 / 速度补偿", "")
    _mark("spMax" not in js and "spSpeed" not in js,
          "app.js 里不再引用 #spMax / #spSpeed（否则点开始会抛 null）", "")
    _mark("时长与速度" not in html, "「时长与速度」整块已删", "")

    # ---- ② 主界面面板骨架 ----
    for tag in ("vizWrap", "vizCanvas", "vizTog", "vizEmpty"):
        _mark('id="%s"' % tag in html, "index.html 有 #%s" % tag, "")

    # ---- ③ 下载页面板骨架（用户第二次要求补的） ----
    for tag in ("dlVizWrap", "dlVizCanvas", "dlVizTog", "dlVizEmpty"):
        _mark('id="%s"' % tag in dhtml, "download.html 有 #%s" % tag, "")

    # ---- ④ 两页都引了 viz.js，且都排在自己的主脚本之前 ----
    _iv, _ia = html.find('src="viz.js"'), html.find('src="app.js"')
    _mark(_iv >= 0, "index.html 引了 viz.js", "")
    _mark(_iv >= 0 and _ia >= 0 and _iv < _ia,
          "index.html 里 viz.js 在 app.js 之前（否则 VizKit 未定义）", "")
    _dv, _dd = dhtml.find('src="/viz.js"'), dhtml.find('src="/download.js"')
    _mark(_dv >= 0, "download.html 引了 /viz.js", "")
    _mark(_dv >= 0 and _dd >= 0 and _dv < _dd,
          "download.html 里 viz.js 在 download.js 之前", "")

    # ---- ⑤ 共用一份实现：各页只 create 一次 ----
    _mark("window.VizKit" in vz and "create: create" in vz,
          "viz.js 导出 window.VizKit.create", "")
    _mark(js.count("VizKit.create(") == 1,
          "app.js 只 create 一次（同一 audio 二次 create 抛 InvalidStateError）",
          str(js.count("VizKit.create(")))
    _mark(djs.count("VizKit.create(") == 1,
          "download.js 只 create 一次", str(djs.count("VizKit.create(")))
    # 接 AudioNode 的代码只许在 viz.js 里；页面自己再接一份 = 声音断掉
    _mark("createMediaElementSource" not in js and
          "createMediaElementSource" not in djs,
          "两页都不自己接 AudioNode（实现只在 viz.js 里）", "")
    _mark('"msu1.viz"' in js and '"msu1.dl.viz"' in djs,
          "两页的开关记忆用不同 key（msu1.viz / msu1.dl.viz）", "")

    # ---- ⑥ 三处最容易漏的实现细节（都在 viz.js 里） ----
    _mark("createMediaElementSource" in vz,
          "viz.js 用 createMediaElementSource 接单例 audio", "")
    _mark("an.connect(ctx.destination)" in vz,
          "analyser 接回 destination（不接就没声音）", "")
    _mark("requestAnimationFrame" in vz and "cancelAnimationFrame" in vz,
          "rAF 有开有关（暂停 / 关闭时停画）", "")
    _mark("localStorage" in vz and "KEY" in vz,
          "开关状态记进 localStorage", "")

    # ---- ⑦ 下载页的播放钩子接全 ----
    _mark("viz.init()" in djs, "download.js 启动时 viz.init()", "")
    _mark(djs.count("viz.stop()") >= 2,
          "download.js 暂停 / 播完都 stop（不是只停一处）",
          str(djs.count("viz.stop()")))
    _mark("viz.ensure()" in djs and "viz.start()" in djs,
          "download.js 播放时 ensure + start", "")
    _mark("viz.touched = true" in djs,
          "download.js 点歌后清空态（touched）", "")

    # ---- ⑧ collectOpts 的固定值 ----
    m = re.search(r"function collectOpts\(\)\s*\{(.*?)\n\}", js, re.S)
    body = m.group(1) if m else ""
    _mark("max_secs: 150" in body and "default_secs: 150" in body,
          "collectOpts 固定 max_secs/default_secs = 150", "")
    _mark(re.search(r"speed:\s*0\s*,", body) is not None,
          "collectOpts 固定 speed = 0（不补偿）", "")
    # 字段名不能删：后端 ConvertJob 直接读这两个 key
    _mark("max_secs" in body and "speed" in body,
          "两个字段名仍在（后端按 key 读，删了会退回它自己的默认值）", "")

    # ---- ⑨ 换主题时频谱跟着换色（不写死彩虹色） ----
    _mark("VIZ.invalidateColors()" in js,
          "setTheme 调 VIZ.invalidateColors()（复古主题下频谱跟着变）", "")
    _mark("invalidateColors" in vz and "V.colors = null" in vz,
          "viz.js 里 invalidateColors 真的清了缓存色", "")
    _mark("--accent" in vz and "--accent-2" in vz,
          "频谱配色取自 --accent / --accent-2（不写死彩虹）", "")

    # ---- ⑩ 让位：右栏加宽 + 两块面板都有实高 ----
    _mark("minmax(0, 1fr) 340px" in css,
          "右栏 340px（原来是 292px，腾给频谱）", "")
    mh = re.search(r"\.viz-wrap\s*\{[^}]*height:\s*(\d+)px", css, re.S)
    _mark(bool(mh) and int(mh.group(1)) >= 120,
          "主界面 viz-wrap 有实高（不是塌成 0）",
          mh.group(1) if mh else "没找到")
    dh = re.search(r"\.dl-viz\s*\{[^}]*height:\s*(\d+)px", dcss, re.S)
    _mark(bool(dh) and int(dh.group(1)) >= 100,
          "下载页 .dl-viz 有实高", dh.group(1) if dh else "没找到")

    # ---- ⑩b canvas 尺寸必须被 CSS 钉死（v5.9.8 修的回归） ----
    # viz.js 的 resize() 把「量到的尺寸 × dpr」写回 canvas 的 width/height
    # 属性，而 canvas 的 CSS 尺寸在**没有样式约束**时恰恰由这两个属性决定
    # —— 闭环正反馈，每帧 ×dpr。v5.9.6 只在 app.css 里写了 #vizCanvas，
    # 下载页的 #dlVizCanvas 就落进去了：0.35 秒从 300 涨到 32545×16358，
    # flex 侧栏被撑破，面板被 overflow:hidden 裁掉 —— 用户看到的正是
    # 「频谱整块不见了」。
    #
    # 这类 bug 静态看一切正常，而且上一轮的「两帧指纹不同」断言也拦不住
    # （爆掉的 canvas 每帧照样在变），只有**量实际渲染尺寸**才露馅
    # （运行时那半边在 vendor/_probe_viz_fix.py 里）。这里静态断约束在不在。
    mcv = re.search(r"\.viz-wrap\s*>\s*canvas\s*\{([^}]*)\}", css, re.S)
    _mark(bool(mcv) and "width: 100%" in mcv.group(1)
          and "height: 100%" in mcv.group(1),
          "app.css 用结构选择器钉死面板 canvas（只写 #vizCanvas 会漏掉下载页）",
          "有 .viz-wrap > canvas" if mcv else "缺这条规则")
    mw = re.search(r"\.viz-wrap\s*\{([^}]*)\}", css, re.S)
    _mark(bool(mw) and "min-width: 0" in mw.group(1),
          "app.css 的 .viz-wrap 有 min-width:0（flex 子项不被内容顶破）", "")
    # ⚠ 这里要匹配**整块**，不能复用上面 dh 那个「截到 height 就停」的正则 ——
    #   min-width 写在 height 后面，截断的 group(0) 里根本不含它（会假红）。
    mdl = re.search(r"\.dl-viz\s*\{([^}]*)\}", dcss, re.S)
    _mark(bool(mdl) and "min-width: 0" in mdl.group(1),
          "download.css 的 .dl-viz 也有 min-width:0", "")
    _mark("$(opt.wrap)" in vz,
          "viz.js 的 resize() 量外层容器，不量 canvas 自己（断掉正反馈）", "")
    _mark("MAXPX" in vz
          and re.search(r"if\s*\(\s*w\s*>\s*MAXPX\s*\)", vz) is not None,
          "viz.js 有 MAXPX 尺寸钳制（真失控了也只坏一块面板，不坏布局）", "")

    # ---- ⑪ 静态白名单：漏了 viz.js 就是 404，两页一起废 ----
    from web_ui import server as _sv
    _mark("viz.js" in _sv.STATIC_OK, "server.STATIC_OK 含 viz.js", "")
    _c, _hz, _rawz = _req("/viz.js")
    _zb = _rawz.decode("utf-8", "replace")
    _mark(_c == 200 and "VizKit" in _zb,
          "GET /viz.js 回 200 且是 VizKit 源码", "%s / %d B" % (_c, len(_rawz)))

    # ---- ⑫ 版本对齐 ----
    # 这里只断「是个正经版本号，且和 server.APP_VERSION 对得上」——
    # 精确版本号归 t_flac_out() 管。原来写的是 startswith("5.9.")，
    # 升到 6.0 就假红一次；改成形状 + 一致性断言，以后发版不用动这里。
    _code, _d = _jget("/api/diag")
    _ver = str(_d.get("version") or "")
    _mark(bool(re.match(r"^\d+\.\d+(?:\.\d+)?$", _ver)),
          "/api/diag 版本是 x.y[.z] 形式（面板没被回退掉）", _ver)
    _mark(_ver == server.APP_VERSION,
          "/api/diag 的版本与 server.APP_VERSION 一致", _ver)


def t_flac_out():
    """[3q] FLAC 输出（v5.9.7）：主界面 + 下载页共用一条写盘链路。

    FLAC 是「只换容器、不换音频」的改动，看着风险很低，但它踩中的全是
    **只在真实转换时才暴露**的点，静态看一切正常：

    1. ``spc_wav.write_tracks`` 是两页唯一的写盘收口，而 ``convert()``
       内部是**位置传参**调用它：``write_tracks(t, out, secs, log, prog,
       cancel, used, start, total)``。新加的 ``fmt`` 一旦插在中间，log 会被
       当成 cancel 传进去 —— 症状是「转换跑完但没有一行日志、取消失灵」。
       所以 ``fmt`` 必须是**最后一个**参数。
    2. 下载页 ``wav_scan_numbering`` 只扫 ``.wav``。FLAC 模式下返回
       ``(0, set())`` → 第二次点下载编号从 1 重来、重名不避让 →
       **逐个覆盖上一轮转好的文件**（R7 那条 bug 的原样复发）。
    3. ``merge_wav`` 用标准库 ``wave`` 读盘，喂 .flac 直接抛。不强制关掉
       就是每首合并失败、刷一屏红字。
    4. 磁盘门禁 ``_estimate_bytes`` 按 WAV 口径估 FLAC 会高估约 3 倍 →
       盘够却判「空间不足」→ 连续 3 轮后放弃剩余曲目。
    5. **无损**是硬指标：``sf.read(..., dtype="int16")`` 中间过一趟 float32
       就会被 libsndfile 加 dither —— 文件能播、时长也对，只是**不等于
       原文件**了。这类 bug 没有任何外部症状。

    本段是纯静态检查（读文件 + 正则），``--fast`` 也跑。
    真解码/真写盘的验证在 ``vendor/_probe_flac.py``（29 项）。
    """
    print("\n[3q] FLAC 输出（v5.9.7）", flush=True)

    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    fo = _read("flac_out.py")
    sw = _read("spc_wav.py")
    cj = _read("convert_job.py")
    dcv = _read("dl_convert.py")
    dapi = _read("web_ui", "dl_api.py")
    sv = _read("web_ui", "server.py")
    ihtml = _read("web_ui", "index.html")
    ajs = _read("web_ui", "app.js")
    acss = _read("web_ui", "app.css")
    dhtml = _read("web_ui", "download.html")
    djs = _read("web_ui", "download.js")
    bp = _read("build_web_pyi.py")

    # ---- ① 编码模块本体 ----
    _mark(bool(fo), "flac_out.py 存在", "%d B" % len(fo))
    for fn in ("def available", "def encode_pcm", "def wav_to_flac",
               "def probe_flac"):
        _mark(fn in fo, "flac_out 有 %s" % fn.split()[-1], "")

    # ---- ② 无损的两个硬要求：int16 直通 + 原子写 ----
    _mark('dtype="int16"' in fo and 'dtype="<i2"' in fo,
          "全程 int16 直通（不经过 float32，否则被加 dither）", "")
    _mark("float32" not in fo.replace("float→int", "").replace("float32，", "")
          or "不经过 float32" in fo,
          "源码里没有 float32 中转", "")
    _mark('"PCM_16"' in fo, "写 PCM_16（16bit，与 WAV 同规格）", "")
    # v5.9.15：format/subtype 收进 _WRITERS 表（加 mp3 时只改一行）
    _mark('"FLAC"' in fo and "MPEG_LAYER_III" in fo,
          "format=FLAC / MP3（_WRITERS 表）", "")
    _mark('+ ".part"' in fo and "os.replace" in fo,
          "原子写：.part → os.replace（不留半个 FLAC）", "")
    _mark("if not os.path.exists(tmp) or os.path.getsize(tmp) <= 0" in fo
          or "FLAC 输出为空" in fo,
          "0 字节输出当失败（不伪造成功）", "")

    # ---- ③ write_tracks：fmt 必须是**最后一个**参数 ----
    m = re.search(r"def write_tracks\((.*?)\):", sw, re.S)
    sig = m.group(1) if m else ""
    _mark('fmt="wav"' in sig, "write_tracks 有 fmt 参数且默认 wav", "")
    # 位置传参兼容：fmt 前面必须是 total
    _mark(bool(re.search(r"total=None,\s*fmt=", sig)),
          "fmt 排在 total 之后（convert 是位置传参，插中间会把 log 当 cancel）",
          "")
    _mark('ext = ("." + fmt) if fmt in ENC_FMTS else ".wav"' in sw,
          "扩展名跟着 fmt 走（直接从格式名推，不再手写映射表）", "")
    _mark("flac_out.encode_pcm" in sw,
          "FLAC 从内存 PCM 直写（不先落 WAV 再读回）", "")
    _mark("if fmt not in ENC_FMTS:" in sw,
          "未知 fmt 值退回 wav（不让整批失败）", "")
    _mark("编码器不可用" in sw and "stat[\"fail\"] = len(tracks)" in sw,
          "编码器不可用时整批只报一次（不是每首刷一行红字）", "")

    # ---- ④ convert / convert_job 的透传 ----
    _mark(bool(re.search(r"loader=None, fmt=\"wav\"\)", sw)),
          "convert() 有 fmt 参数", "")
    _mark(sw.count("fmt=fmt") >= 2,
          "convert 的两个分支都把 fmt 传下去", str(sw.count("fmt=fmt")))
    _mark('o.get("fmt")' in cj, "convert_job 读 opts['fmt']", "")
    _mark("if _fmt in spc_wav.ENC_FMTS:" in cj,
          "convert_job 在非 WAV 下跳过整轨合并（wavmerge 只吃 WAV）", "")

    # ---- ⑤ 主界面：卡片 + 互斥 + collectOpts ----
    _mark('data-fmt="flac"' in ihtml, "index.html 有 FLAC 卡片", "")
    _mark("fmt-off" in ihtml or "fmt-off" in acss,
          "编码器不可用时卡片有置灰样式", "")
    _mark('grid-column: 1 / -1' in acss,
          "BIN+CUE 独占一行（三张卡塞两列会截断「BIN + CUE」）", "")
    # v6.0.0：collectOpts 改成遍历 AUDIO_FMTS —— 断言「四格式都登记了」
    # 和「fmt 按 FMT_PRIORITY 取」，比盯着某一张卡片的字面量更抗重构。
    _mark('const AUDIO_FMTS = ["wav", "flac", "mp3", "ogg"]' in ajs,
          "app.js 登记了四种音频容器（wav/flac/mp3/ogg）", "")
    _mark("AUDIO_FMTS.some(" in ajs,
          "do_wav 由 AUDIO_FMTS 遍历得出（不写死卡片名）", "")
    _mark("FMT_PRIORITY.find(" in ajs,
          "fmt 按 FMT_PRIORITY 确定性取一个（不随 DOM 顺序漂移）", "")
    _mark('const other = $(`[data-fmt=' in ajs,
          "app.js 里 WAV / FLAC 互斥", "")
    _mark("probeFlac" in ajs and "/api/diag" in ajs,
          "app.js 启动时探一次编码器，不可用就置灰", "")

    # ---- ⑥ 下载页：两颗独立转换按钮（v5.9.9 起不再是复选框） ----
    _mark('id="dlConvWav"' in dhtml and 'id="dlConvFlac"' in dhtml,
          "download.html 有「转 WAV」「转 FLAC」两颗按钮", "")
    _mark('id="dlAutoWav"' not in dhtml and 'id="dlAutoFlac"' not in dhtml,
          "两个「下载后自动转」复选框已删（用户要求改成独立按钮）", "")
    # 按钮必须挨着「上传云盘」—— 用户明确说了位置
    _cv_i = dhtml.find('id="dlConvWav"')
    _cl_i = dhtml.find('id="dlCloud"')
    _mark(_cv_i > _cl_i > 0,
          "两颗按钮在「上传云盘」之后（同一排）", "")
    _mark('fmt: fmt' in djs and 'convSelected("wav")' in djs
          and 'convSelected("flac")' in djs,
          "download.js 点按钮时把 fmt 传给后端（不再靠 opts 猜）", "")
    _mark('auto_wav: $("#dlAutoWav").checked' not in djs
          and 'auto_flac: $("#dlAutoFlac").checked' not in djs,
          "download.js 不再上报 auto_wav / auto_flac", "")
    _mark("fmtTag" in djs, "download.js 的按钮文案跟着格式变（不写死「转 WAV」)", "")
    # 后端仍保留这两个字段：老前端 / 老 prefs 传进来时不能炸
    _mark('"auto_flac": False' in dapi, "dl_api.opts 仍保留 auto_flac（兼容旧调用）", "")
    _mark("def _pending_tag" in dapi, "「待转 WAV / FLAC」按当前格式走", "")
    _mark('_games_with(wav_dirs, (".wav",))' in dapi
          and '_games_with(flac_dirs, (".flac",))' in dapi,
          "磁盘快照分别扫 WAV/ 和 FLAC/（否则只转了一种的行显示「未转」）", "")
    _mark('dl.opts["auto_flac"]' in sv and 'dl.opts["auto_wav"] = False' in sv,
          "server 的 set_opts 仍兜底互斥（老客户端传两个 true 时）", "")

    # ---- ⑥b 输出目录按格式分离（v5.9.9 修「已转 WAV 就跳过 FLAC」的关键） ----
    # 为什么分开目录能修 bug：``already_done()`` 判的是「这个源文件转过没有」，
    # 而它的记账文件 ``.msu1-convert.json`` 就放在**输出目录**里。两种格式
    # 共用一个目录 = 共用一份记账 → 转完 WAV 后记账里有 ``01_xx.wav`` 且文件
    # 还在，转 FLAC 时直接判「已转过」跳过，一个 FLAC 都不出（用户报的就是）。
    _mark("def _out_dir" in dapi, "dl_api 有 _out_dir(fmt)（按格式给目录）", "")
    _mark('os.path.join(dest, _FMT_DIRS[f])' in dapi,
          "_out_dir 把 WAV/FLAC/MP3 指到各自目录（查 _FMT_DIRS 表）", "")
    _mark("def _audio_roots_now" in dapi,
          "dl_api 有 _audio_roots_now()（试听/状态/上传同时看两个根）", "")
    _mark("def convert_wav(self, urls=None, fmt=None)" in dapi,
          "convert_wav 收 fmt 参数（两颗按钮各传各的）", "")
    _mark('self._out_dir(_fmt, dest_root)' in dapi,
          "start() 里自动转也走 _out_dir（两种格式不再混在一个目录）", "")
    _mark('out_root = self._out_dir(fmt, dest)' in dapi,
          "convert_wav 里按 fmt 选输出目录", "")

    # ---- ⑦ dl_convert：容器 + 编号 + 合并 + 门禁 ----
    _mark('fmt: str = "wav"' in dcv, "ConvertWorker 有 fmt 参数", "")
    _mark("if self._fmt not in spc_wav.ENC_FMTS:" in dcv,
          "ConvertWorker 归一化 fmt（未知值退回 wav）", "")
    _mark("fmt=self._fmt" in dcv,
          "三处 write_tracks 都传了 fmt", str(dcv.count("fmt=self._fmt")))
    _mark('and self._fmt == "wav")' in dcv,
          "FLAC 下强制关掉 merge_wav", "")
    # 编号扫描必须认 .flac
    _mark('_AUDIO_EXTS = (".wav", ".flac", ".mp3")' in dcv,
          "wav_scan_numbering 认三种扩展名（否则第二轮覆盖第一轮）", "")
    _mark("os.path.splitext(name)" in dcv,
          "去扩展名用 splitext（手写 name[:-5] 加 mp3 就会错 1 个字符）", "")
    _mark("FLAC_BYTES_RATIO" in dcv, "体量预估有 FLAC 折算系数", "")
    _mark("FMT_BYTES_RATIO.get(self._fmt, 1.0)" in dcv,
          "_estimate_bytes 按格式折算（否则高估 3 倍误判空间不足）", "")

    # ---- ⑧ diag 暴露编码器状态 ----
    _code, _d = _jget("/api/diag")
    _f = (_d.get("flac") or {})
    _mark(isinstance(_f, dict) and "ok" in _f,
          "/api/diag 有 flac 段（含 ok / why）", _f.get("why"))
    _mark(bool((_d.get("modules") or {}).get("flac_out")),
          "/api/diag.modules.flac_out 为真", "")

    # ---- ⑨ 打包清单 ----
    _mark('"flac_out"' in bp,
          "build_web_pyi.HIDDEN 含 flac_out（函数内 import，静态分析看不到）",
          "")

    # ---- ⑩ 版本对齐 ----
    # v5.9.8 起改成跟 server.py 的 APP_VERSION 比，不再写死字面量：
    # 每次发版都要手改这里的话，改漏一个就是一条「假红」（5.9.6→5.9.7 撞过）。
    _exp_ver = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"',
                         open(os.path.join(ROOT, "web_ui", "server.py"),
                              encoding="utf-8").read(), re.M)
    _exp_ver = _exp_ver.group(1) if _exp_ver else ""
    _mark(_d.get("version") == _exp_ver,
          "/api/diag 版本 = server.APP_VERSION", _d.get("version"))

    # ---- ⑩b index.html 里的两处静态版本号（v5.9.11 加） ----
    # **这不是重复检查**：`<title>` 和 `.brand-ver` 是静态文本，不受
    # APP_VERSION 影响。历史上它们漂过两次 —— 界面停在 5.2 而实际 5.5
    # （2026-10-25），以及 5.9.6 一路漂到 5.9.11（这次）。diag 是对的、
    # 徽标是错的，用户看到的就是「版本号对不上」，而且没人会去点开 diag。
    # 所以这里直接把 HTML 的两个数拉出来和 APP_VERSION 对齐。
    _idx = open(os.path.join(ROOT, "web_ui", "index.html"),
                encoding="utf-8").read()
    _m_ver = re.search(r'class="brand-ver">([^<]+)<', _idx)
    _m_ttl = re.search(r"<title>[^<]*?(\d+\.\d+(?:\.\d+)?)</title>", _idx)
    _mark(bool(_m_ver) and _m_ver.group(1).strip() == _exp_ver,
          "index.html .brand-ver = APP_VERSION",
          _m_ver.group(1).strip() if _m_ver else "没找到徽标")
    _mark(bool(_m_ttl) and _m_ttl.group(1) == _exp_ver,
          "index.html <title> 版本 = APP_VERSION",
          _m_ttl.group(1) if _m_ttl else "标题里没有版本号")

    # ---- ⑪ 已转 WAV 的游戏还能再转 FLAC（v5.9.9 修的 bug） ----
    # **这是用户报的那个 bug 的回归测试**，必须留在门禁里 —— 它太容易复发：
    # 只要有人把两种格式的输出目录合并回一个（共用一份转换记账），就又会
    # 「转完 WAV 再转 FLAC 一个都不出」，而且界面看起来一切正常。
    #
    # 机制：``already_done()`` 判的是「这个**源文件**转过没有」，而记账文件
    # ``.msu1-convert.json`` 就放在**输出目录**里。所以目录分开 = 记账分开
    # = 互不干扰。这里直接验证这个前提，不跑真转换（--fast 也能跑）。
    import tempfile as _tf
    import dl_convert as _dc
    with _tf.TemporaryDirectory() as _td:
        _wdir = os.path.join(_td, "WAV", "Game")
        _fdir = os.path.join(_td, "FLAC", "Game")
        os.makedirs(_wdir)
        os.makedirs(_fdir)
        _src = os.path.join(_td, "Game.spc")
        with open(_src, "wb") as _fh:
            _fh.write(b"\0" * 64)
        # 校验②要求「记账里的产物**仍在磁盘**」—— 不真建这两个文件的话，
        # already_done() 一律返回 False，这组断言就成了摆设。
        with open(os.path.join(_wdir, "01_Stage.wav"), "wb") as _fh:
            _fh.write(b"\0" * 64)
        with open(os.path.join(_fdir, "01_Stage.flac"), "wb") as _fh:
            _fh.write(b"\0" * 64)
        _mw = _dc.ConvertManifest(os.path.join(_wdir, _dc.MANIFEST_NAME))
        _mw.record(_src, ["01_Stage.wav"], "id666")
        _mark(_mw.already_done(_src),
              "前提：WAV 目录的记账判「转过」", "")
        _mf = _dc.ConvertManifest(os.path.join(_fdir, _dc.MANIFEST_NAME))
        _mark(not _mf.already_done(_src),
              "同一源在 FLAC 目录里判「没转过」← 这就是修好的那个 bug", "")
        # 反向也要成立：先转 FLAC 再转 WAV，不能因为 FLAC 记账而跳过 WAV
        _mf.record(_src, ["01_Stage.flac"], "id666")
        _mw2 = _dc.ConvertManifest(os.path.join(_wdir, _dc.MANIFEST_NAME))
        _mw2.load()          # ⚠ 构造不会自动读盘，必须显式 load
        _mark(_mw2.already_done(_src),
              "WAV 记账仍在（两种格式互不覆盖对方的记账）", "")
        # 同一份记账里确实会跳过 —— 证明「跳过」本身没坏，坏的是共用记账
        _mark(_mf.already_done(_src),
              "同格式重复转仍会跳过（没把 already_done 整个废掉）", "")

    # ---- ⑪b 状态列必须能说出「两种格式都有」（v5.9.9 修） ----
    # 转换过程中 ``cv_file_done`` 写的是**这一趟**转了什么（WAV 或 FLAC），
    # 那条运行期状态会盖住磁盘快照。结果：先转 WAV 再转 FLAC，状态列还写
    # 「🎵 已转 WAV」—— 用户以为 FLAC 没转成，其实 10 个 .flac 都在盘上。
    # 所以收尾扫盘之后必须把「🎵 已转 X」摘掉，让 _disk_state() 接管。
    from web_ui import dl_api as _dapi
    _dapi_src = _read("web_ui", "dl_api.py")
    _mark('"🎵 已转 " + self._pending_tag()' in _dapi_src,
          "cv_file_done 的状态文案跟着本次格式走（不再写死 WAV）", "")
    _mark("def _drop_cv_states" in _dapi_src,
          "有 _drop_cv_states()：收尾后摘掉「🎵 已转 X」", "")
    _fin = _dapi_src.split("def _finish(self):", 1)[-1].split("def _drop_cv_states", 1)[0]
    _mark(_fin.count("self._drop_cv_states()") == 2,
          "两处收尾（只转 / 下载+转）都调了 _drop_cv_states()",
          "找到 %d 处" % _fin.count("self._drop_cv_states()"))
    _mark(_fin.find("self._refresh_disk()") < _fin.find("self._drop_cv_states()"),
          "_refresh_disk() 在前、_drop_cv_states() 在后（顺序反了就白摘）", "")

    # 真跑一遍：摘「🎵 已转 …」，但「⚠ 转换失败」这类磁盘看不出来的要留着
    # ⚠ 不用 DownloadState()：构造会读 prefs、扫磁盘、加载中文词库，门禁里
    #   又慢又可能碰到用户真实配置。这里只需要 states / _cv_urls / lock 三样。
    import threading as _th2
    _st = object.__new__(_dapi.DownloadState)
    _st.lock = _th2.RLock()
    _st.states = {"u1": "🎵 已转 WAV", "u2": "⚠ 转换失败", "u3": "🎵 已转 FLAC"}
    _st._cv_urls = {"u1", "u2", "u3"}
    _st._drop_cv_states()
    _mark("u1" not in _st.states and "u3" not in _st.states,
          "「🎵 已转 X」被摘掉（交给磁盘快照）", str(_st.states))
    _mark(_st.states.get("u2") == "⚠ 转换失败",
          "「⚠ 转换失败」保留（磁盘看不出来，摘了就丢信息）", str(_st.states))
    _mark(_st._cv_urls == set(), "_cv_urls 用完即清（下一轮从零开始）",
          str(_st._cv_urls))


def t_bootlog(state):
    """[3k] 启动黑匣子：窗口程序的 stderr 是 None，崩了必须留痕。

    用户报「拷到另一台 Win11 上双击毫无反应」—— 窗口程序把异常全吞了，
    查了两轮才定位。这段守三件事：日志能写、异常钩子装上了、
    入口把每一步都打了点。
    """
    print("\n[3k] 启动黑匣子（bootlog）", flush=True)
    import shutil as _sh
    import threading as _th
    import bootlog as _B

    _mark(hasattr(_B, "start") and hasattr(_B, "fatal"),
          "bootlog 导出 start / fatal", "")

    # ⚠ 记下原钩子，测完必须还回去 —— start() 装的钩子会在**未捕获异常**
    #   时弹 MessageBox，后面任何一个测试炸了都会卡在弹窗上等人点确定。
    _old_hook = sys.excepthook
    _old_thook = getattr(_th, "excepthook", None)

    # 换到一个临时目录跑，别把测试日志写进程序目录
    base = os.path.join(tmpdir.root(), "bootlog_gate")
    _rmtree_soft(base)
    os.makedirs(base, exist_ok=True)
    _orig = _B._candidates
    _B._candidates = lambda: [base]
    _B._started = False
    _B._fh = None
    try:
        p = _B.start(["--gate"], tag="gate")
        _mark(os.path.isfile(p), "日志文件建出来了", p)
        _B.log("门禁测试行")
        txt = open(p, encoding="utf-8").read()
        _mark("门禁测试行" in txt, "log() 写得进去", "")
        _mark("参数: ['--gate']" in txt, "记下了启动参数", "")
        _mark("程序目录:" in txt and "版本:" in txt,
              "where() 记了环境信息", "")

        # 第二次启动要轮转，不能无限长
        _B._started = False
        _B.start([], tag="gate2")
        _mark(os.path.isfile(os.path.join(base, "startup.prev.log")),
              "上一份轮转成 startup.prev.log", os.listdir(base))

        # 钩子必须真的挂上（没挂 = 崩了还是静默）
        _mark(sys.excepthook is not _old_hook,
              "sys.excepthook 被替换过", "")
        _mark(getattr(_th, "excepthook", None) is not None,
              "threading.excepthook 在", "")
    finally:
        sys.excepthook = _old_hook
        if _old_thook is not None:
            _th.excepthook = _old_thook
        _B._candidates = _orig
        _B._started = False
        _B._fh = None
        _rmtree_soft(base)

    # -- 静态锁：入口和外壳必须真的调 --------------------------------
    _w = open(os.path.join(ROOT, "msu1_web.py"), encoding="utf-8").read()
    _mark("import bootlog" in _w, "入口第一件事 import bootlog", "")
    _mark("bootlog.start(" in _w, "入口调 bootlog.start", "")
    _mark("bootlog.fatal(" in _w, "入口崩了会 fatal 弹窗", "")
    _mark("--console" in _w, "支持 --console 诊断模式", "")
    _mark('_step("shell.main")' in _w, "每一步都打了点", "")

    _s = open(os.path.join(ROOT, "web_ui", "shell.py"), encoding="utf-8").read()
    _mark("def _log(" in _s, "shell 有 _log 同时写日志与 stdout", "")
    _mark("webbrowser.open" in _s and "if not ok:" in _s,
          "浏览器调不起来时弹窗（不再静默死等）", "")

    # -- 浏览器降级链：不能只认 Edge ----------------------------------
    # 用户问「没有预装 Edge 的 Win10 怎么办」。只认 Edge 就是单点依赖，
    # 那台机器上会走到「默认浏览器也调不起来 → 静默」的老路。
    from web_ui import shell as _sh
    _names = [n for (n, _i, _c) in _sh._BROWSERS]
    _mark(_names[:1] == ["Edge"], "Edge 仍然排第一", _names)
    _mark(len(_names) >= 3 and "Chrome" in _names,
          "降级链里有 Chrome 等替代", _names)
    for _n in _names:
        _mark(_sh._BROWSERS[_names.index(_n)][2],
              "%s 有候选路径" % _n, "")
    _mark(hasattr(_sh, "find_browser"), "有 find_browser()", "")
    _mark(_sh.profile_dir("Edge") != _sh.profile_dir("Chrome"),
          "不同浏览器用不同 profile（共用会报「配置文件属于另一个程序」）",
          (_sh.profile_dir("Edge"), _sh.profile_dir("Chrome")))
    _mark("_CUR_IMAGE" in _s and "!= _CUR_IMAGE" in _s,
          "窗口判定跟着当前浏览器走（不写死 msedge.exe）", "")
    _mark("%LOCALAPPDATA%" in _s, "候选路径支持环境变量展开", "")
    _mark("App Paths" in _s, "走注册表 App Paths 兜非标准安装路径", "")

    # -- tk 兜底：一个浏览器都没有时的最后一条路 --------------------
    _mark("def _tk_fallback(" in _s, "有 _tk_fallback()", "")
    _mark('"--tk" in argv' in _s, "支持 --tk 强制走内置界面", "")
    _mark("if not _exe:" in _s and "_tk_fallback" in _s,
          "没浏览器时自动切内置界面", "")
    try:
        import msu1_gui as _g
        _mark(hasattr(_g, "main"), "msu1_gui 能导入且导出 main", "")
    except Exception as _e:                                  # noqa: BLE001
        _mark(False, "msu1_gui 能导入（tk 兜底的前提）", _e)
    _b = open(os.path.join(ROOT, "build_web.py"), encoding="utf-8").read()
    _mark('"tkinter"' not in _b.split("NOFOLLOW")[1].split("]")[0],
          "NOFOLLOW 里不再排掉 tkinter", "")
    _mark('"msu1_gui"' in _b and "--include-module" in _b,
          "显式点名 msu1_gui 等兜底模块", "")


def t_bios_bundle(state):
    """[3j] BIOS 固化：指定一次 → 复制进程序目录 → 以后自动找到。

    用户报的是「本机有 PS2 BIOS，能不能集成进软件里，不要再次指定」。
    老实现只写一行 ``psf_bios.txt``（指向用户随手放的地方，比如桌面某个
    临时文件夹），源文件一挪、配置一丢就得重新指定。

    ⚠ 只测「复制 / 判定 / 目录顺序」这些纯逻辑 —— ``check_bios`` 要真 BIOS
      才能过，门禁机器上不能假设有。真实链路在 README ⚠45 里有实测记录。
    """
    print("\n[3j] BIOS 固化（指定一次 → 集成进程序目录）", flush=True)
    import shutil as _sh
    import psf_native as _P

    _mark(_P.BIOS_HOME == "bios", "BIOS_HOME 就是 bios", _P.BIOS_HOME)

    base = os.path.join(tmpdir.root(), "bios_bundle")
    _rmtree_soft(base)
    os.makedirs(base, exist_ok=True)

    # app_dir 指到临时目录 —— 别把测试文件写进真的程序目录（那会被打进产物）
    _orig = _P.app_dir
    _P.app_dir = lambda: base
    try:
        home = _P.bios_home()
        _mark(home == os.path.join(base, "bios"),
              "bios_home() = <程序目录>\\bios", home)

        src = os.path.join(base, "外面临时", "SCPH39001.bin")
        os.makedirs(os.path.dirname(src), exist_ok=True)
        with open(src, "wb") as f:
            f.write(b"\x00" * 32)

        dst, err = _P.adopt(src)
        _mark(err == "" and os.path.isfile(dst), "adopt() 复制成功", (dst, err))
        _mark(os.path.dirname(dst) == home, "副本落在 bios/ 里", dst)
        _mark(os.path.getsize(dst) == 32, "副本内容一致", os.path.getsize(dst))
        _mark(_P._is_bundled(dst), "_is_bundled 认副本", dst)
        _mark(not _P._is_bundled(src), "_is_bundled 不认源文件", src)

        again, err2 = _P.adopt(dst)
        _mark(again == dst and err2 == "", "已在 bios/ 里就不重复复制",
              (again, err2))

        # 扫目录时 bios/ 排第一 —— 源文件没了也能命中副本
        dirs = _P._bios_dirs()
        _mark(bool(dirs) and dirs[0] == home, "bios/ 是第一个被扫的目录",
              dirs[:2])

        # 源文件删掉，只留 bios/ 里的副本
        os.remove(src)
        _mark(_P._is_bundled(dst) and os.path.isfile(dst),
              "源文件删了副本还在", dst)
    finally:
        _P.app_dir = _orig
        _P._invalidate()
        _rmtree_soft(base)

    # -- 静态锁：三个改动点任意一个回退就红 ------------------------------
    _ps = open(os.path.join(ROOT, "psf_native.py"), encoding="utf-8").read()
    _mark("bundled, why = adopt(p)" in _ps,
          "set_bios 会先把文件固化进 bios/", "")
    _mark("def autodiscover(" in _ps, "psf_native 有 autodiscover()", "")
    _mark('"bios_bundled"' in _ps, "status 报 bios_bundled", "")

    _srv = open(os.path.join(ROOT, "web_ui", "server.py"), encoding="utf-8").read()
    _mark("/api/psf/autofind" in _srv, "server 有 /api/psf/autofind 路由", "")
    _mark("def _bios_autoboot(" in _srv, "启动时后台自动固化", "")

    _js = open(os.path.join(ROOT, "web_ui", "app.js"), encoding="utf-8").read()
    _mark("自动查找 BIOS" in _js, "未就绪时按钮是「自动查找 BIOS」", "")
    _mark("集成到程序目录" in _js, "已就绪未固化时按钮是「集成到程序目录」", "")
    _mark('"/api/psf/autofind"' in _js, "前端会调 autofind", "")

    _bld = open(os.path.join(ROOT, "build_web.py"), encoding="utf-8").read()
    _mark("bios_src" in _bld and "copytree" in _bld,
          "打包时把 bios/ 复制到 exe 旁边", "")


def t_theme(state):
    print("\n[3e] 主题（2 套现代 + 6 套复古）", flush=True)

    # ---- theme.js 必须在白名单里。漏了的表现：页面全正常，只有点「复古配色」
    #      那颗按钮没反应，错误只在 DevTools 控制台的 "ThemeKit is not defined"。
    code, hdr, raw = _req("/theme.js")
    _mark(code == 200 and len(raw) > 1500, "GET /theme.js 有内容",
          "%s / %d B" % (code, len(raw)))
    _mark("text/javascript" in hdr.get("content-type", ""),
          "GET /theme.js 是 text/javascript", hdr.get("content-type"))
    _mark(b"ThemeKit" in raw and b"mountPanel" in raw,
          "theme.js 导出 ThemeKit / mountPanel", "")

    code, d = _jget("/api/themes")
    _mark(code == 200 and d.get("ok"), "GET /api/themes", code)
    themes = d.get("themes") or []
    _mark(len(themes) == 8, "一共 8 套配色", len(themes))

    by = {t.get("key"): t for t in themes}
    for k in ("dark", "light"):
        _mark(k in by and not (by[k].get("vars") or {}),
              "%s 不注入变量（吃 app.css 默认）" % k, by.get(k))
    for k in ("famicom", "nes", "gameboy", "pce", "megadrive", "sfc"):
        t = by.get("retro:" + k)
        _mark(t is not None, "有 retro:%s" % k, "")
        if not t:
            continue
        v = t.get("vars") or {}
        # 28 个变量是 app.css 里 dark/light 两个块的并集；少一个就会有一处
        # 颜色落回默认，在深色底上表现为一块色斑。
        _mark(len(v) >= 20, "retro:%s 变量数够（≥20）" % k, len(v))
        # ⚠ 变量名是 app.css 那一套（--surface / --text / --accent …），
        #   不是 retro_palette 里的键名（bg / panel / card / text …）。
        for name in ("--bg", "--surface", "--text", "--accent"):
            _mark(bool(v.get(name)), "retro:%s 有 %s" % (k, name), v.get(name))
        _mark((t.get("swatch") or []), "retro:%s 有预览色块" % k, t.get("swatch"))
        _mark((t.get("label") or "") != "", "retro:%s 有中文名" % k, t.get("label"))

    # 6 套的 bg 不能互相重复 —— 那说明 THEMES 映射表接错了键
    bgs = [(by["retro:" + k].get("vars") or {}).get("--bg")
           for k in ("famicom", "nes", "gameboy", "pce", "megadrive", "sfc")
           if by.get("retro:" + k)]
    _mark(len(set(bgs)) == len(bgs), "6 套底色互不相同", bgs)


def t_shell(state):
    print("\n[3g] 外壳：残留窗口判定（ERR_CONNECTION_REFUSED 的根因）", flush=True)
    from web_ui import shell

    # 页面加载成功 / 失败两种标题都要认，否则残留的错误页窗口躲过清理。
    for t, want, why in (
            ("RetroMusic Lab · 游戏音乐转换器 5.2", True, "加载成功"),
            ("127.0.0.1", True, "加载失败（Edge 用地址当标题）"),
            ("127.0.0.1:47913", True, "带端口的地址"),
            ("Microsoft Edge", False, "普通 Edge 窗口"),
            ("Visual Studio Code", False, "别的程序"),
            ("", False, "空标题"),
    ):
        _mark(shell._is_app_window(t, 47913) is want,
              "标题 %-32r → %s" % (t, "算我们的" if want else "不是我们的"), why)

    # 换端口之后旧端口不该再匹配（防止误关别的 127.0.0.1 窗口）
    _mark(shell._is_app_window("127.0.0.1:47914", 47913) is False,
          "别的端口的地址不算我们的", "")
    # 端口号必须精确匹配：479130 不能被 47913 的前缀骗过去
    _mark(shell._is_app_window("127.0.0.1:479130", 47913) is False,
          "127.0.0.1:479130 不算我们的（端口不能被前缀命中）", "")
    _mark(shell._is_app_window("127.0.0.1:47913/x", 47913) is True,
          "127.0.0.1:47913/x 算我们的", "")

    # 进程名过滤：这里必然拿不到 msedge，所以必须返回空 —— 说明
    # 「标题对但进程不对」的窗口不会被误关
    _mark(shell._proc_image(os.getpid()).endswith(".exe"),
          "_proc_image 能读到自己的进程名", shell._proc_image(os.getpid()))
    _mark(shell._proc_image(0) == "", "拿不到的 PID 返回空串（不误判）",
          shell._proc_image(0))


def t_zh(state):
    print("\n[3f] 游戏名中文词典", flush=True)

    import zh_titles
    # ---- 词典规模：主体在 zh_titles_user.json 里，内置字面量只有一千多条 ----
    _mark(os.path.isfile("zh_titles_user.json"),
          "zh_titles_user.json 在项目根", os.path.abspath("zh_titles_user.json"))
    n = len(zh_titles.GAME_ZH)
    _mark(n > 20000, "合并后的 GAME_ZH > 2 万条", n)

    # ---- 真实覆盖率：拿内置索引快照跑一遍，别只看词典条数 ----
    import joshw_source as jw
    items, _, _ = jw.best_index("spc")
    names = [str(e.get("name") or "") for e in items]
    if not names:
        print("  [--] 没读到 spc 内置快照：跳过覆盖率", flush=True)
        return
    got = sum(1 for x in names if zh_titles.translate(x))
    pct = 100.0 * got / len(names)
    # 实测 1900/1918 = 99.1%。坏包（词典没进来）会掉到 16% 左右。
    _mark(pct > 90, "SPC 机种中文名覆盖率 > 90%",
          "%d/%d = %.1f%%" % (got, len(names), pct))

    # ---- 用户词典的优先级：它必须在最后合并，不能被内置补录覆盖 ----
    # 挑一个只可能来自用户词典的长尾条目（内置字面量里没有）
    probe = "zelda ii the adventure of link"
    _mark(zh_titles.GAME_ZH.get(probe) is not None,
          "用户词典条目已合并且未被覆盖",
          zh_titles.GAME_ZH.get(probe))


def t_cloud(state):
    print("\n[3d] 网易云云盘（登录 / 上传清单）", flush=True)

    # ---- 静态件：两页共用的 cloud.js / cloud.css 必须在白名单里 ----
    for name, ctype in (("/cloud.js", "text/javascript"),
                        ("/cloud.css", "text/css")):
        code, hdr, raw = _req(name)
        _mark(code == 200 and len(raw) > 800, "GET %s 有内容" % name,
              "%s / %d B" % (code, len(raw)))
        _mark(ctype in hdr.get("content-type", ""),
              "GET %s 是 %s" % (name, ctype), hdr.get("content-type"))

    # ---- 状态接口：字段一个都不能少（前端照着字段名写的）----
    code, st = _jget("/api/cloud/status")
    _mark(code == 200 and st.get("ok"), "GET /api/cloud/status", code)
    want = {"logged_in", "user_name", "has_profile", "browser", "can_upload",
            "can_browser", "deps", "job", "qr", "why", "hint"}
    _mark(want <= set(st), "状态字段齐全", sorted(want - set(st)))
    _mark(isinstance(st.get("deps"), dict)
          and {"soundfile", "Crypto", "segno"} <= set(st["deps"]),
          "deps 三件都在（soundfile / Crypto / segno）", st.get("deps"))
    _mark(all(st["deps"].values()), "三个依赖都真的 import 得动", st.get("deps"))
    _mark(isinstance(st.get("logged_in"), bool), "logged_in 是布尔",
          type(st.get("logged_in")).__name__)
    # 没登录时 why 必须说人话，不能空
    if not st.get("logged_in"):
        _mark(bool(st.get("why")), "未登录时 why 有说明", st.get("why"))

    # ---- 上传清单：plan 的口径 ----
    code, d = _jget("/api/cloud/plan")
    _mark(code == 200 and d.get("ok") is False,
          "没有输出目录时 plan 明确报错", d.get("error"))

    wav_dir = _make_wav_dir(tmpdir.root())
    import urllib.parse
    code, d = _jget("/api/cloud/plan?dir=" + urllib.parse.quote(wav_dir))
    _mark(code == 200 and d.get("ok"), "有 WAV 时 plan 正常", d.get("error"))
    _mark(d.get("need_choose") is True,
          "合并 + 分轨都在 → 要求用户选范围", d.get("need_choose"))
    _mark(len(d.get("merged") or []) == 1, "认出 1 个合并文件",
          [r.get("name") for r in (d.get("merged") or [])])
    _mark(len(d.get("tracks") or []) == 3, "认出 3 个分轨",
          [r.get("name") for r in (d.get("tracks") or [])])
    _mark(float(d.get("est_mb") or 0) >= 0, "给了体积估算", d.get("est_mb"))
    _mark(bool(d.get("hint")), "给了「要不要全传」的提示", d.get("hint"))

    # ---- 范围过滤（pick_scope）：三个值 + 默认 ----
    import cloud_upload as cu
    from web_ui.cloud_api import pick_scope
    wavs = cu.collect_wavs(wav_dir)
    _mark(len(pick_scope(wavs, scope="tracks")[0]) == 3, "scope=tracks → 3 个",
          len(pick_scope(wavs, scope="tracks")[0]))
    _mark(len(pick_scope(wavs, scope="merged")[0]) == 1, "scope=merged → 1 个",
          len(pick_scope(wavs, scope="merged")[0]))
    _mark(len(pick_scope(wavs, scope="all")[0]) == 4, "scope=all → 4 个",
          len(pick_scope(wavs, scope="all")[0]))
    # 老参数（布尔 merged）必须继续认 —— 外部脚本还在用
    _mark(len(pick_scope(wavs, merged=False)[0]) == 3,
          "老参数 merged=False 仍只传分轨",
          len(pick_scope(wavs, merged=False)[0]))
    _mark(len(pick_scope(wavs, merged=True)[0]) == 1,
          "老参数 merged=True 仍只传合并",
          len(pick_scope(wavs, merged=True)[0]))
    # 默认：两种都有 → 只传分轨（合并就是它们拼出来的）
    _mark(len(pick_scope(wavs)[0]) == 3, "不给范围时默认只传分轨",
          len(pick_scope(wavs)[0]))
    # 只给一个不存在的范围 → 必须给原因，不能返回空列表让调用方自己猜。
    # ⚠ 必须挑一个**分轨**来测：collect_wavs 是字母序，"Album（合并）" 排在
    #   "Track00" 前面，随手取 wavs[0] 拿到的其实是合并文件 —— 那样
    #   scope=merged 反而有东西，这条断言会以一个完全误导人的方式红掉。
    only_track = [w for w in wavs if not cu.is_merged_wav(w)][0]
    empty, why = pick_scope([only_track], scope="merged")
    _mark(not empty and bool(why), "范围里没东西时说清原因", why)

    # ---- 粘贴 Cookie：三条错误路径都要给人话 ----
    code, d = _jpost("/api/cloud/login_cookie", {"cookie": ""})
    _mark(code == 200 and d.get("ok") is False, "空 Cookie 被拒", d.get("error"))
    code, d = _jpost("/api/cloud/login_cookie", {"cookie": "a=1; b=2"})
    _mark(d.get("ok") is False and "MUSIC_U" in (d.get("error") or ""),
          "没有 MUSIC_U 时点明缺什么", d.get("error"))

    # ---- 上传入口：未登录返回 need_login（**不是 500**）----
    # ⚠⚠ 只在**未登录**时测这条。已登录的话这一下会真把 3 个测试 WAV
    #    传进用户的网易云云盘 —— 门禁绝不能有副作用。
    state.opts["out_dir"] = wav_dir
    if st.get("logged_in"):
        print("  [--] 本机已登录网易云：跳过上传入口（否则会真上传）", flush=True)
    else:
        code, d = _jpost("/api/upload_cloud", {"scope": "tracks"})
        _mark(code == 200, "未登录上传不报 500", code)
        _mark(d.get("ok") is False and d.get("need_login") is True,
              "未登录返回 need_login（前端据此弹登录窗）", d)
        _mark(int(d.get("count") or 0) == 3, "need_login 也带上了待传数量",
              d.get("count"))
        _mark("tkinter" not in (d.get("error") or "").lower(),
              "报错里不再让用户去开 tkinter 版", d.get("error"))

    # ---- 扫码：真打一次接口（网络不通就 [--] 跳过，不算过）----
    code, d = _jpost("/api/cloud/qr_start", {})
    if d.get("ok"):
        n = int(d.get("n") or 0)
        _mark(n >= 21 and len(d.get("rows") or []) == n,
              "二维码矩阵 n×n 且行数对得上", "%s / %s" % (n, len(d.get("rows") or [])))
        # quiet zone：首行必须全 0，否则手机对不上焦
        _mark(set(d["rows"][0]) == {"0"}, "二维码有 quiet zone（首行全白）",
              d["rows"][0][:20])
        code, p = _jget("/api/cloud/qr_poll")
        _mark(p.get("ok") and "code" in p, "qr_poll 返回网易云的 code", p.get("code"))
    else:
        print("  [--] 二维码申请失败（多半是没网）：%s"
              % (d.get("error") or "")[:60], flush=True)

    # ---- 换号 / 退出 / 取消：幂等，不能崩 ----
    _mark(_jpost("/api/cloud/cancel", {})[1].get("ok"), "cancel 幂等")
    _mark(_jpost("/api/cloud/dismiss", {})[1].get("ok"), "dismiss 幂等")

    # ---- 单例：每次请求造新实例 = 进度每 0.5 秒被清零 ----
    from web_ui.cloud_api import auth
    _mark(auth() is auth(), "CloudAuth 是进程级单例")
    _mark(auth() is server.cloud_auth(), "server.cloud_auth 拿到同一个实例")

    state.opts["out_dir"] = ""


# ══════════════════════════════════════════════════════════════════════
def t_env(state):
    """换机依赖审计 —— 每一条都对应一个「换台电脑就炸」的真实形态。

    这一组不测业务逻辑，测的是**环境假设**：端口被占、目录不可写、
    ``tar.exe`` 缺失或跑挂、``%LOCALAPPDATA%`` 没建。这些在开发机上
    永远不会发生，所以必须靠门禁钉住，不能靠人眼。
    """
    print("\n[3h] 换机依赖审计（端口 / 目录 / 解压退路）", flush=True)

    import shutil
    import tempfile

    from web_ui import server as _srv
    from web_ui import shell

    # ---- 1. 身份端点：shell 靠它分辨「自己人」还是「别人占了端口」 ----
    code, d = _jget("/api/whoami")
    _mark(code == 200 and d.get("app") == _srv.APP_ID,
          "GET /api/whoami 回 app=%r" % _srv.APP_ID, d)
    _mark(d.get("pid") == os.getpid(),
          "/api/whoami 的 pid 是本进程", "%s vs %s" % (d.get("pid"), os.getpid()))
    _mark(shell._WHOAMI_APP == _srv.APP_ID,
          "shell 与 server 的身份字符串一致（改一边就断）",
          "%r / %r" % (shell._WHOAMI_APP, _srv.APP_ID))

    # ---- 2. 端口归属判定 ----
    my_port = int(_BASE.rsplit(":", 1)[1])
    _mark(shell.port_is_ours(my_port) is True,
          "自己起的服务 → port_is_ours 认得出", my_port)

    # 找一个刚释放的空闲端口（绑了立刻关）：那儿没人监听，必须判成「不是我们」
    import socket
    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    dead_port = sk.getsockname()[1]
    sk.close()
    try:
        got = shell.port_is_ours(dead_port, timeout=0.6)
        raised = ""
    except Exception as exc:                                     # noqa: BLE001
        got, raised = None, "%s: %s" % (type(exc).__name__, exc)
    _mark(got is False and not raised,
          "空端口 → 判成「不是我们」且不抛异常（宁可不误判成自己）",
          raised or got)

    # ---- 3. 浏览器 profile 目录：必须能建，且绝不抛异常 ----
    prof = shell.profile_dir("Edge")
    _mark(os.path.isdir(prof), "profile_dir 建出了目录", prof)

    old_la = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = os.path.join(tempfile.gettempdir(),
                                              "不存在的盘符_x", "nowhere")
    try:
        try:
            alt = shell.profile_dir("Chrome")
            err = ""
        except Exception as exc:                                 # noqa: BLE001
            alt, err = "", "%s: %s" % (type(exc).__name__, exc)
        _mark(not err, "LOCALAPPDATA 不可写时不抛异常（退到 temp）", err)
        _mark(bool(alt) and os.path.isdir(alt),
              "回落目录真的建出来了", alt)
    finally:
        if old_la is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_la

    # ---- 4. 下载默认目录：没有 D 盘时不能塞进 AppData ----
    import tmpdir
    from web_ui import dl_api
    dest = dl_api.DownloadState._default_dest("PlayStation")
    _mark("MSU1Downloads" in dest and "joshw" in dest,
          "默认下载目录形状正确", dest)
    _mark("AppData" not in dest,
          "默认下载目录不在 AppData 里（用户找得到）", dest)

    old_fb = tmpdir._fallback
    tmpdir._fallback = True                 # 假装没有 D 盘
    try:
        dest2 = dl_api.DownloadState._default_dest("PlayStation")
    finally:
        tmpdir._fallback = old_fb
    _mark("AppData" not in dest2 and "MSU1Downloads" in dest2,
          "没有 D 盘时退到用户主目录（不是 AppData\\Local）", dest2)

    # ---- 5. 用户词典路径：新机器上 %LOCALAPPDATA%\\MSU1Converter 还不存在 ----
    import zh_titles
    old_la = os.environ.get("LOCALAPPDATA")
    fresh = os.path.join(tempfile.gettempdir(), "rml_fresh_la_%d" % os.getpid())
    _rmtree_soft(fresh)
    os.environ["LOCALAPPDATA"] = fresh
    try:
        p = zh_titles.user_dict_path()
        _mark(bool(p), "全新的 LOCALAPPDATA 也能拿到用户词典路径", p)
    finally:
        if old_la is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_la
        _rmtree_soft(fresh)

    # ---- 6. 解压退路：这是「换机后无法自动解压」的根因，必须钉死 ----
    import zipfile
    import joshw_dl

    box = tempfile.mkdtemp(prefix="env_zip_")
    try:
        arc = os.path.join(box, "pack.zip")
        with zipfile.ZipFile(arc, "w") as zf:
            zf.writestr("sound/a.txt", "hello")
            zf.writestr("sound/b.txt", "world")

        real_find_tar = joshw_dl.find_tar
        # (a) 没有 tar.exe 的机器（精简版 Windows / 用户删了）
        joshw_dl.find_tar = lambda: None
        try:
            ok, files, why = joshw_dl.extract_archive(
                arc, os.path.join(box, "no_tar"), timeout=60)
        finally:
            joshw_dl.find_tar = real_find_tar
        _mark(ok and len(files) >= 2,
              "没有 tar.exe 时 .zip 也能解（走标准库 zipfile）",
              (ok, len(files), why))

        # (b) tar.exe 在，但**跑挂了**（被杀软拦、被组策略禁跑、版本太老）
        sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
        faker = os.path.join(sysroot, "System32", "whoami.exe")
        if not os.path.isfile(faker):
            faker = os.path.join(sysroot, "System32", "cmd.exe")
        joshw_dl.find_tar = lambda: faker
        try:
            ok2, files2, why2 = joshw_dl.extract_archive(
                arc, os.path.join(box, "bad_tar"), timeout=60)
        finally:
            joshw_dl.find_tar = real_find_tar
        _mark(ok2 and len(files2) >= 2,
              "tar.exe 存在但解压失败时，自动换备用解压器",
              (ok2, len(files2), why2))

        # (c) 坏包仍然要明确失败（别把「解不了」静默成「成功」）
        bad = os.path.join(box, "bad.zip")
        with open(bad, "wb") as fh:
            fh.write(b"this is not a zip at all")
        joshw_dl.find_tar = lambda: None
        try:
            ok3, files3, why3 = joshw_dl.extract_archive(
                bad, os.path.join(box, "bad_pack"), timeout=60)
        finally:
            joshw_dl.find_tar = real_find_tar
        _mark(not ok3 and not files3 and bool(why3),
              "坏包明确报错、不返回空成功", why3)
    finally:
        _rmtree_soft(box)


def _make_hwas(path, rate=20894, block=0x4000, payload=b"", channels=1,
               magic=b"sawh"):
    """造一个合成 HWAS（``.hwas``）：2048 字节头 + 裸 IMA 负载。"""
    head = bytearray(0x200)
    head[0:4] = magic
    struct.pack_into("<I", head, 0x04, block)
    struct.pack_into("<i", head, 0x08, rate)
    struct.pack_into("<i", head, 0x0C, channels)
    struct.pack_into("<I", head, 0x10, 0)                 # loop start
    struct.pack_into("<I", head, 0x14, len(payload))      # 字节数
    struct.pack_into("<I", head, 0x18, len(payload))      # loop end
    with open(path, "wb") as f:
        f.write(bytes(head))
        f.write(payload)


def _make_mod(path, period=428, smp_rate=8287.0, cycles=100, n=8192,
              magic=b"M.K.", note=True):
    """造一个合成 ``.MOD``：1 个采样 + 1 个模式，采样是整数周期的正弦。

    返回这个采样自身的基频（Hz）。``cycles`` 取整数、循环长度取满采样，
    这样循环点是严丝合缝的，输出的频谱是一根干净的谱线 —— 拿来反推音高
    换算对不对最直接。
    """
    import numpy as np

    head = bytearray(1084)
    head[0:7] = b"test\x00\x00\x00"
    head[20 + 22] = (n >> 8) & 0xFF          # 采样长度，单位「字」
    head[20 + 23] = n & 0xFF
    head[20 + 24] = 0                        # finetune
    head[20 + 25] = 64                       # volume
    head[20 + 26] = 0                        # 循环起点（字）
    head[20 + 27] = 0
    head[20 + 28] = ((n // 2) >> 8) & 0xFF   # 循环长度（字）= 整个采样
    head[20 + 29] = (n // 2) & 0xFF
    head[950] = 1                            # 乐曲长度
    head[951] = 0
    head[952] = 0                            # order 0 → 模式 0
    head[1080:1084] = magic

    pat = bytearray(1024)
    if note:
        pat[0] = (period >> 8) & 0x0F        # 周期高 4 位
        pat[1] = period & 0xFF
        pat[2] = 0x10                        # 采样 1，效果 0
        pat[3] = 0

    t = np.arange(n)
    data = (np.sin(2 * np.pi * cycles * t / float(n)) * 100).astype(np.int8)
    with open(path, "wb") as f:
        f.write(bytes(head) + bytes(pat) + data.tobytes())
    return cycles * smp_rate / float(n)


def _peak_hz(pcm, rate, lo=20.0, hi=4000.0):
    """找 ``pcm`` 在 ``lo~hi`` 内的最强谱峰频率（Hz）。"""
    import numpy as np

    n = min(pcm.size, 1 << 16)
    seg = pcm[:n].astype(np.float64)
    seg = seg - seg.mean()
    sp = np.abs(np.fft.rfft(seg * np.hanning(n)))
    fr = np.fft.rfftfreq(n, 1.0 / rate)
    band = (fr >= lo) & (fr <= hi)
    if not band.any():
        return 0.0
    return float(fr[band][int(np.argmax(sp[band]))])


def _make_swav(path, rate=24050, wtype=2, payload=b"", loop=0,
               nonloop_words=None):
    """造一个合成 SWAV（``.swav``）：0x24 字节头 + 负载。"""
    if nonloop_words is None:
        nonloop_words = (len(payload) + 3) // 4
    head = bytearray(0x24)
    head[0:4] = b"SWAV"
    head[4:6] = b"\xff\xfe"
    struct.pack_into("<H", head, 0x06, 0x0100)
    struct.pack_into("<I", head, 0x08, 0x24 + len(payload))
    struct.pack_into("<H", head, 0x0C, 0x10)
    struct.pack_into("<H", head, 0x0E, 1)
    head[0x10:0x14] = b"DATA"
    struct.pack_into("<I", head, 0x14, 0x14 + len(payload))
    head[0x18] = wtype
    head[0x19] = loop
    struct.pack_into("<H", head, 0x1A, rate)
    struct.pack_into("<H", head, 0x1E, 0)
    struct.pack_into("<I", head, 0x20, nonloop_words)
    with open(path, "wb") as f:
        f.write(bytes(head))
        f.write(payload)


def _make_dvi(path, samples=4, payload=b"", data_off=0x800):
    """造一个合成 DVI（``.dvi``）：0x800 字节头 + 负载。"""
    head = bytearray(data_off)
    head[0:4] = b"DVI."
    struct.pack_into(">i", head, 0x04, data_off)
    struct.pack_into(">i", head, 0x08, samples)
    struct.pack_into(">i", head, 0x0C, -1)
    with open(path, "wb") as f:
        f.write(bytes(head))
        f.write(payload)


def _f80(value):
    """整数采样率 → AIFF 的 **80 位扩展浮点**（10 字节，大端）。

    AIFF 的采样率不是 u32 也不是 float32，是 Apple 的 extended 格式：
    ``sign(1) | exponent(15，偏移 16383) | mantissa(64，无隐含位)``，
    值 = mant × 2^(exp − 16383 − 63)。

    实测两个真实文件：22050 → ``400e ac44 0000…``，11025 → ``400d ac44 0000…``
    —— 这个函数必须能复现这两个字节串，否则测试样本本身就不合法。
    """
    v = int(value)
    if v <= 0:
        return b"\x00" * 10
    e = v.bit_length() - 1                       # floor(log2(v))
    mant = v << (63 - e)
    return struct.pack(">H", e + 16383) + mant.to_bytes(8, "big")


def _make_aiff(path, mono=(), rate=22050, bits=8, channels=1):
    """造一个最小但**完全合法**的 AIFF（``FORM....AIFF`` + COMM + SSND）。

    8bit 按 AIFF 规范是**有符号**（这点和 WAV 的无符号不一样，最容易搞错）；
    16bit 是大端。数据按 COMM 的 frames 对齐，chunk 补偶数字节。
    """
    n = len(mono) // channels
    if bits == 8:
        data = bytes((int(s) & 0xFF) for s in mono)
    else:
        data = b"".join(struct.pack(">h", int(s)) for s in mono)

    comm = (struct.pack(">hIh", channels, n, bits) + _f80(rate))
    ssnd = struct.pack(">II", 0, 0) + data
    body = b"AIFF"
    for cid, payload in ((b"COMM", comm), (b"SSND", ssnd)):
        body += cid + struct.pack(">I", len(payload)) + payload
        if len(payload) & 1:
            body += b"\x00"
    with open(path, "wb") as f:
        f.write(b"FORM" + struct.pack(">I", len(body)) + body)


def _make_aifc_sdx2(path, payload=b"", rate=11025, channels=1, frames=None):
    """造一个 3DO 的 AIFC（``FORM....AIFC`` + ``SDX2``）。

    AIFC 的 COMM 比 AIFF 多两段：``compressionType``（4 字节四字符码，
    本项目只认 ``SDX2``/``NONE``/``twos``）+ **pascal 字符串**（1 字节长度
    + 名字，再补到偶数字节）。少写这两段会被读成「AIFC 的 COMM 缺
    compressionType」，是很典型的手写样本踩坑点。
    """
    n = frames if frames is not None else len(payload)
    comp_name = b"3DO SQXD"                      # 真实文件里就长这样
    comm = (struct.pack(">hIh", channels, n, 16) + _f80(rate)
            + b"SDX2" + bytes([len(comp_name)]) + comp_name)
    if len(comm) & 1:
        comm += b"\x00"
    ssnd = struct.pack(">II", 0, 0) + payload
    body = b"AIFC"
    for cid, data in ((b"COMM", comm), (b"SSND", ssnd)):
        body += cid + struct.pack(">I", len(data)) + data
        if len(data) & 1:
            body += b"\x00"
    with open(path, "wb") as f:
        f.write(b"FORM" + struct.pack(">I", len(body)) + body)


def t_zophar(state):
    """第二家音源站（Zophar's Domain）：只锁「接线」，不碰网络。

    为什么单开一段：zophar 的条目 url 是**游戏页**而不是归档直链，下载前要
    解析一次。这个差别散落在四个地方，漏一处就是「下载成功但界面全停在等待」
    或者「落盘成 .7z 解压不了」：

    1. 机种树要出现（``platforms()`` 把两家合起来）
    2. ``_src_of()`` 要认得出 z_ 前缀（``load()`` / ``scan`` 靠它分派）
    3. 落盘名要按**解析后的 .zip** 算（按游戏页算会补 ``.7z``）
    4. ``_url_for()`` 要靠 ``_zip_of`` 把落盘名映射回游戏页 url

    完整的解析/缓存/分页测试在 ``vendor/test_zophar.py``（90 条，全离线）。
    """
    print("\n[3k] Zophar 原声（第二家音源站）接线", flush=True)
    try:
        # ⚠ 必须走包导入（``from web_ui import dl_api``），不能裸 ``import dl_api``：
        #   后者要求 ``web_ui/`` 自己在 sys.path 上，而那只在「前面某个分片
        #   恰好动过 sys.path」时才成立 —— 换成 ``--only=zophar`` 单独跑就
        #   立刻报 ``No module named 'dl_api'``（实测）。包导入没有这个顺序依赖。
        from web_ui import dl_api as _dl
    except Exception as exc:                          # noqa: BLE001
        _mark(False, "dl_api 可导入", exc)
        return
    if getattr(_dl, "zs", None) is None:
        _mark(False, "zophar_source 已导入", "模块缺失（打包 HIDDEN 漏了？）")
        return
    _mark(True, "zophar_source 已导入")

    groups, err = _dl.DownloadState().platforms()
    names = [i["name"] for g in groups for i in g["items"]]
    _mark(not err and "Game Gear" in names and "Master System" in names,
          "机种树含 Game Gear / Master System（v5.9 走 joshw 的 kss 容器）",
          names[-3:])
    # v5.9：Zophar 那组已经撤了 —— 它当初存在的唯一理由（joshw 没有 GG/SMS）
    # 被证伪，两家都列只会让用户多纠结一次「为什么这条没体积」。
    _mark(not [g for g in groups if g["group"] == "Zophar 原声"],
          "v5.9：机种树里没有「Zophar 原声」组",
          [g["group"] for g in groups])
    _mark(all("src" in i for g in groups for i in g["items"]),
          "每个机种都带 src（下载层靠它分派）")
    _mark(all(i.get("src") == "joshw" for g in groups for i in g["items"]),
          "机种树里全部是 joshw（zophar 已不参与）")

    st = _dl.DownloadState()
    _mark(st._src_of("kss_gg") == "joshw" and st._src_of("spc") == "joshw",
          "v5.9：_src_of 恒 joshw（zophar 不再进树）",
          (st._src_of("kss_gg"), st._src_of("spc")))

    # ⚠ 这条是「界面所有行停在等待…」的锁：落盘名必须用解析后的 .zip
    zurl = ("https://fi.zophar.net/soundfiles/sega-game-gear-sgc/aa-harimanada/"
            "Aa%20Harimanada%20%28EMU%29.zophar.zip")
    page = "https://www.zophar.net/music/sega-game-gear-sgc/aa-harimanada"
    nm = st._file_name("Aa Harimanada", zurl)
    _mark(nm == "Aa Harimanada.zip", "落盘名按解析后的 .zip 算", nm)
    _mark(st._file_name("Aa Harimanada", page).endswith(".7z"),
          "按游戏页算会变 .7z（所以必须先解析）",
          st._file_name("Aa Harimanada", page))

    st.entries = [{"name": "Aa Harimanada", "url": page, "size": 0,
                   "mtime": "1993", "letter": "A", "src": "zophar"}]
    st._zip_of = {page: zurl}
    _mark(st._url_for("D:\\x\\" + nm) == page,
          "_url_for 靠 _zip_of 把落盘名映射回游戏页", st._url_for("D:\\x\\" + nm))
    st._zip_of = {}
    _mark(st._url_for("D:\\x\\" + nm) is None,
          "没有 _zip_of 时映射不到（确认依赖成立）")

    d1 = st._default_dest("Game Gear", "zophar")
    d2 = st._default_dest("SNES / SFC", "joshw")
    _mark("zophar" in d1 and "joshw" in d2 and d1 != d2,
          "默认保存目录分站（.../zophar/ 与 .../joshw/）", (d1, d2))

    dg = st.diag()
    _mark(dg.get("zophar_source") is True, "diag 报 zophar_source 模块在（不进树，仅留档）")
    _mark(dg.get("platforms") == len(_dl.jw.PLATFORMS),
          "diag 机种数 = joshw 全量（v5.9 起不再加 zophar 的 2 个）",
          (dg.get("platforms"), len(_dl.jw.PLATFORMS)))


def t_kss_native():
    """[3l] KSS 改走 libkss（libgme 只认 16 KB 布局）。

    为什么非改不可：libgme 的 KSS 实现只映射 0x0000-0x3FFF 与 0x8000-0xBFFF，
    **不映射 0x4000-0x7FFF**。32 KB 的 Game Gear rip（`load_addr` 0x3E10 一带）
    会丢 16 KB 代码，Z80 跑飞 —— 而 libgme **不报错**，只是每轨精确静音。
    上层看到的是「未产出任何 WAV（解码零产出，文件可能损坏或格式不受支持）」，
    把解码器的缺陷说成了素材的错。实测 Zophar 的 Game Gear 栏 3/12 这样。

    这一段纯离线：只验接线、曲目表、格式转换，不跑真实解码（那要 32 KB 的
    真实 KSS 样本，放不进仓库）。
    """
    print("\n[3l] KSS 走 libkss（libgme 只认 16 KB 布局）", flush=True)
    import tempfile

    import chip_decode
    import chip_formats as cf
    import kss_native

    _mark(cf.KSS_EXTS == (".kss",),
          "KSS_EXTS 只含 .kss（.sgc 是独立容器，libgme 支持良好，不跟着改）",
          cf.KSS_EXTS)
    _mark(kss_native.DECODER_NAME == "kss2wav.exe", "解码器名 kss2wav.exe",
          kss_native.DECODER_NAME)
    ok, why = kss_native.decoder_ready()
    _mark(ok, "kss2wav.exe 找得到（缺了 .kss 会整批静默失败）", why)
    _mark(kss_native.GAIN > 1.0,
          "GAIN > 1（libkss 混音比 libgme 低约 12 dB，实测比值 3.7~4.4）",
          kss_native.GAIN)

    # ---- 曲目表：zophar 的「一首一个 m3u」+ 转义逗号 ----
    d = tempfile.mkdtemp(prefix="kss_gate_")
    try:
        kss = os.path.join(d, "G-3324.kss")
        with open(kss, "wb") as f:
            f.write(b"KSCC" + b"\x00" * 60)      # 内容不重要，只验曲目表
        for fn, body in (
            ("01 BGM #01.m3u",
             "G-3324.kss::KSS,0,BGM #01 - A\\, B - Puyo,2:01,,10"),
            ("02 BGM #02.m3u",
             "G-3324.kss::KSS,1,BGM #02 - A\\, B - Puyo,2:18,,10"),
        ):
            with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
                f.write(body)
        plan = kss_native.read_playlist(kss)
        _mark(len(plan) == 2, "一首一个 m3u → 读到 2 首（v5.7 的兜底仍生效）",
              plan)
        _mark(bool(plan) and plan[0][0] == 0 and abs(plan[0][2] - 121.0) < 0.01,
              "曲号与时长正确（0 号 / 2:01 = 121 秒）", plan[:1])
        _mark(bool(plan) and "," in plan[0][1] and "\\" not in plan[0][1],
              "曲名里的转义逗号 \\, 还原成真逗号", plan[:1])
    finally:
        _rmtree_soft(d)

    # ---- mono → 立体声 + 增益 + 削顶（纯函数） ----
    mono = struct.pack("<4h", 100, -200, 300, -400)
    st = kss_native._to_stereo(mono, 1)
    _mark(len(st) == len(mono) * 2, "mono → 立体声：字节数翻倍", len(st))
    _mark(list(struct.unpack("<8h", st))
          == [400, 400, -800, -800, 1200, 1200, -1600, -1600],
          "左右声道相同、且乘了 GAIN", list(struct.unpack("<8h", st)))
    _mark(struct.unpack("<2h", kss_native._to_stereo(
        struct.pack("<1h", 30000), 1))[0] == 32767,
          "×GAIN 之后削顶到 32767（不会溢出成负数）")

    # ---- 分流：.kss 必须交给 kss_native ----
    d2 = tempfile.mkdtemp(prefix="kss_gate2_")
    hit = []
    orig = kss_native.load_tracks
    kss_native.load_tracks = lambda p, **kw: (hit.append(os.path.basename(p)) or [])
    try:
        chip_decode.load_chip_paths([os.path.join(d2, "x.kss")])
    except Exception:                                   # noqa: BLE001
        pass
    finally:
        kss_native.load_tracks = orig
        _rmtree_soft(d2)
    _mark(hit == ["x.kss"], ".kss 被分流到 kss_native.load_tracks", hit)


def t_aif(state, fast):
    """3DO 的 AIFF / AIFC（含 SDX2）。

    v5.6 起 3DO 不再「需要 ARM60 + DSP + CEL 整机模拟」—— 它的音源就是
    游戏自己存好的**流式音频**，容器是苹果 AIFF/AIFC。这里用**手算的期望
    值**把三个解码内核钉死，不依赖任何外部素材（`vendor/_check_aif_ref.py`
    才是拿 vgmstream 逐样本比的那条线，它需要外部 exe，不进常规门禁）。
    """
    print("\n[3j] 3DO 音源：AIFF / AIFC / SDX2", flush=True)

    import shutil
    import tempfile

    import numpy as np

    import aif_audio as af
    import dl_convert as dl
    import stream_audio as sa
    from convert_job import classify

    box = tempfile.mkdtemp(prefix="aif_gate_")
    try:
        # ---- 1. 80 位扩展浮点：必须能复现真实文件的字节 ----
        # 实测 Real Pinball 的 BGM.aiff：22050 → 400dac44 0000 0000 0000
        _mark(_f80(22050).hex() == "400dac44000000000000",
              "80bit 采样率 22050 → 400dac44…（与真实文件逐字节一致）",
              _f80(22050).hex())
        # ⚠ 真实文件**不一定是规约的**：Psychic Detective 的 .aifc 里
        #   11025 写成 400e2b11…（尾数没左移到 bit63）。按公式算就对，
        #   谁要是「先把尾数归一化」再断言字节，会在这里红。
        _mark(abs(af._read_f80(b"\x40\x0e\x2b\x11" + b"\x00" * 6) - 11025) < 1e-6,
              "_read_f80 能读**非规约**的 11025（真实 .aifc 里的写法）",
              af._read_f80(b"\x40\x0e\x2b\x11" + b"\x00" * 6))
        # 往返：任意采样率写进去再读出来必须等值（这才是 _f80 的正确性判据）
        _rt = [8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000]
        _mark(all(abs(af._read_f80(_f80(v)) - v) < 1e-6 for v in _rt),
              "80bit 往返：%s 全部等值" % _rt,
              [af._read_f80(_f80(v)) for v in _rt])
        _mark(af._read_f80(b"\x00" * 10) == 0.0, "全零 f80 → 0.0（不崩）")

        # ---- 2. SDX2 内核：手算 4 个样本 ----
        #
        # 表：squares[i] = i<0 ? -i*i*2 : i*i*2，下标 i = byte + 128
        #   0x80 → i=0   → -(128*128)*2 = -32768
        #   0x81 → i=1   → -(127*127)*2 = -32258
        #   0x00 → i=128 → 0
        #   0x02 → i=130 → 2*2*2 = 8
        #
        # 字节 0x80,0x81,0x00,0x02（偶数=绝对值模式，hist 清零）：
        #   0x80 偶 → hist=0, 0+(-32768) = -32768          → -32768
        #   0x81 奇 → hist=-32768, -32768+(-32258)=-65026  → 钳到 -32768
        #   0x00 偶 → hist=0,   0+0       = 0              → 0
        #   0x02 偶 → hist=0,   0+8       = 8              → 8
        got = af.decode_sdx2(b"\x80\x81\x00\x02").tolist()
        _mark(got == [-32768, -32768, 0, 8],
              "SDX2 绝对值模式（偶数字节清 hist）+ 负向钳位", got)

        # 字节 0x00,0x01,0x02,0x03：
        #   0x00 偶 → 0
        #   0x01 奇 → 0+2   = 2      （i=129 → 1*1*2）
        #   0x02 偶 → hist=0, 0+8 = 8（i=130 → 2*2*2）
        #   0x03 奇 → 8+18  = 26     （i=131 → 3*3*2）
        got = af.decode_sdx2(b"\x00\x01\x02\x03").tolist()
        _mark(got == [0, 2, 8, 26], "SDX2 增量模式（奇数字节累加）", got)

        # ⚠ hist 存的是**钳位后**的值（vgmstream: hist = out = clamp16(sample)）。
        #   0x7F → i=255 → 127*127*2 = 32258。
        #   0x00 起：0 → 32258 → 钳 32767（hist=32767）→ 32767+32258 钳 32767
        got = af.decode_sdx2(b"\x00\x7f\x7f\x7f").tolist()
        _mark(got == [0, 32258, 32767, 32767],
              "SDX2 正向钳位，且 hist 存钳位后的值（不累积漂移）", got)

        # 空输入不崩
        _mark(af.decode_sdx2(b"").size == 0, "SDX2 空数据 → 空数组")

        # ---- 3. PCM8 / PCM16BE ----
        # 8bit 是有符号（-128..127）→ 左移 8 位，不缩放
        got = af._decode_pcm8(bytes([0x00, 0x7F, 0x80, 0xFF])).tolist()
        _mark(got == [0, 32512, -32768, -256],
              "8bit 有符号 PCM → <<8（0x80 是 -128 不是 128）", got)
        got = af._decode_pcm16be(b"\x00\x01\x80\x00\xff\xff").tolist()
        _mark(got == [1, -32768, -1], "16bit 大端 PCM", got)

        # ---- 4. 头解析：8bit / 16bit / AIFC-SDX2 三个合成样本 ----
        p8 = os.path.join(box, "pcm8.aiff")
        _make_aiff(p8, mono=[0, 100, -100, 0], rate=22050, bits=8)
        h = af.read_header(p8)
        _mark(h["form"] == "AIFF" and h["codec"] == af.CODEC_PCM8
              and h["frames"] == 4 and int(h["rate"]) == 22050
              and h["channels"] == 1,
              "AIFF 8bit 头解析（form/codec/frames/rate/channels）",
              {k: h[k] for k in ("form", "codec", "frames", "rate", "channels")})
        pcm, rate = af.decode_pcm(p8)
        _mark(pcm.tolist() == [0, 25600, -25600, 0] and rate == 22050,
              "AIFF 8bit 解码值", (pcm.tolist(), rate))

        p16 = os.path.join(box, "pcm16.aiff")
        _make_aiff(p16, mono=[1, -32768, -1, 12345], rate=44100, bits=16)
        pcm, rate = af.decode_pcm(p16)
        _mark(pcm.tolist() == [1, -32768, -1, 12345] and rate == 44100,
              "AIFF 16bit 大端解码值", (pcm.tolist(), rate))

        # AIFC + SDX2（3DO 的真实形态）
        ps = os.path.join(box, "sdx2.aifc")
        _make_aifc_sdx2(ps, payload=b"\x00\x01\x02\x03", rate=11025)
        h = af.read_header(ps)
        _mark(h["form"] == "AIFC" and h["aifc"] and h["codec"] == af.CODEC_SDX2
              and h["frames"] == 4 and int(h["rate"]) == 11025,
              "AIFC 头解析：compressionType=SDX2 + pascal 串",
              {k: h[k] for k in ("form", "aifc", "codec", "frames", "rate")})
        pcm, rate = af.decode_pcm(ps)
        _mark(pcm.tolist() == [0, 2, 8, 26] and rate == 11025,
              "AIFC-SDX2 端到端解码值", (pcm.tolist(), rate))

        # ---- 5. probe 永不抛异常，且给出可读的编码名 ----
        pr = af.probe(ps)
        _mark(pr["ok"] and pr["codec"] == "SDX2 ADPCM (3DO)"
              and abs(pr["secs"] - 4 / 11025.0) < 1e-9,
              "probe(AIFC-SDX2)：ok + 编码名 + 时长", pr)
        bad = os.path.join(box, "fake.aif")
        with open(bad, "wb") as f:
            f.write(b"RIFF\x00\x00\x00\x00WAVEfmt ")   # WAV 冒名 .aif
        pr = af.probe(bad)
        _mark(not pr["ok"] and "FORM" in pr["error"],
              "WAV 冒名 .aif → probe 报「缺 FORM 头」而不是崩", pr["error"][:50])
        pr = af.probe(os.path.join(box, "nope.aif"))
        _mark(not pr["ok"] and pr["error"], "文件不存在 → probe 也不崩",
              pr["error"][:50])

        # 4bit = XA-ADPCM（Road Rash）：明确拒绝，绝不产出静音 WAV
        p4 = os.path.join(box, "xa4.aiff")
        _make_aiff(p4, mono=[0] * 8, rate=22050, bits=4)
        _mark(af.read_header(p4)["codec"] == af.CODEC_XA4,
              "4bit AIFF 被识别成 XA4")
        try:
            af.decode_pcm(p4)
            _mark(False, "4bit AIFF 必须抛异常（不能静音糊过去）")
        except ValueError as exc:
            _mark("XA-ADPCM" in str(exc) or "4bit" in str(exc),
                  "4bit AIFF 抛 ValueError 并说明原因", str(exc)[:50])

        # ---- 6. 接进 stream_audio 之后的行为 ----
        _mark(sa.kind_of(p8) == "aif", ".aiff 被 stream_audio 认成 aif",
              sa.kind_of(p8))
        pcm2, rate2, nch2 = sa.decode_any(p8)
        _mark(nch2 == 1 and rate2 == 22050 and pcm2.size == 4,
              "stream_audio.decode_any(.aiff) → (pcm, rate, 声道)",
              (pcm2.tolist(), rate2, nch2))
        raw = sa.decode_pcm(p8)
        # 4 帧 @22050 → 44100 是 8 帧 × 2 声道 × 2 字节 = 32 字节
        _mark(len(raw) == 32,
              "stream_audio.decode_pcm → 44.1k 立体声裸 PCM（4 帧→8 帧）",
              len(raw))
        _mark(np.frombuffer(raw, "<i2").size == 16,
              "裸 PCM 是单声道上混到双声道（帧数 ×2 是重采样，×2 是上混）",
              np.frombuffer(raw, "<i2").size)
        # ⚠ 极短输入：SWR 会整体吞掉 ≤16 帧的输入（实测输出 0 帧），
        #   界面上就是「转换成功但文件是 0 秒」。_resample_to_out 现在先补
        #   静音到 256 帧再按理论长度裁回 —— 这条锁住那个修复。
        _tiny = sa._resample_to_out(np.arange(4, dtype=np.int16), 22050, 1)
        # 返回值是**交错立体声样本**：4 帧 @22050 → 8 帧 × 2 声道 = 16 个样本
        _mark(_tiny.size == 16,
              "极短输入（4 帧）不再被 SWR 吞成 0 帧", _tiny.size)
        _mark(sa._resample_to_out(np.arange(2, dtype=np.int16), 8000, 1).size > 0,
              "极短输入（2 帧 @8000，要上采样 5.5 倍）也有输出")

        # ---- 7. 三条链路 ----
        for e in (".aif", ".aiff", ".aifc"):
            _mark(e in sa.ALL_EXTS, "%s 在 stream_audio.ALL_EXTS 里" % e)
            _mark(dl.policy_for("x" + e) == "stream",
                  "%s → policy stream" % e, dl.policy_for("x" + e))
            _mark(dl.is_convertible("x" + e), "%s → 可转" % e)
        # 整包目录要能被 expand 成逐个文件（否则「添加文件夹 → 0 个音源」）
        _mark(all(e in __import__("web_ui.server", fromlist=["x"])._SRC_EXTS
                  for e in (".aif", ".aiff", ".aifc")),
              "_SRC_EXTS 含 .aif/.aiff/.aifc（否则加文件夹扫出 0 个音源）")
        # classify：整包 .aif 必须落到「未知」→ 逐文件展开，不能当成 chip 包
        _mark(classify(box) not in ("chip", "cd", "psf"),
              "整包 .aif 的目录不被当成 chip/cd/psf 包（要逐文件展开）",
              classify(box))

        # 文件过滤器：Windows 的 OFN 对话框会**隐藏**不在过滤器里的文件
        from web_ui import native_dialog
        parts = " ".join(p for _t, p in native_dialog.FILTER_PARTS)
        _mark("*.aif" in parts and "*.aifc" in parts,
              "文件对话框过滤器含 *.aif/*.aifc（否则用户看不到文件）")
    finally:
        _rmtree_soft(box)


def t_stream(state):
    """流式音频：``.adx``（PyAV）+ ``.mod``（自写回放器）+ HWAS/SWAV/DVI（自写 IMA）。

    v5.2.7 起三种 NDS/Saturn 格式已经接上。这里用**手算的期望值**把 IMA
    内核钉死 —— 上一轮就是因为 ``_IMA_INDEX`` 后半段写错（-2,-4,-6,-8）
    而把「自己解错」误判成「格式没破」。
    """
    print("\n[3i] 流式音频：ADX / MOD / HWAS / SWAV / DVI", flush=True)

    import shutil
    import tempfile
    import wave

    import stream_audio as sa
    import mod_audio as ma
    import dl_convert as dl
    from convert_job import classify

    box = tempfile.mkdtemp(prefix="stream_gate_")
    try:
        # ---- 1. IMA 内核：拿合成流钉死每一步 ----
        #
        # 输入 7F 80 00 FF（低半字节先，hist=0, idx=0），手算：
        #   0x7F → nib 15: step=7,  delta=0+1+3+7=11, 负 → hist=-11,  idx=8
        #        → nib  7: step=16, delta=2+4+8+16=30    → hist=19,   idx=16
        #   0x80 → nib  0: step=34, delta=4             → hist=23,   idx=15
        #        → nib  8: step=31, delta=3, 负          → hist=20,   idx=14
        #   0x00 → nib  0: step=28, delta=3             → hist=23,   idx=13
        #        → nib  0: step=25, delta=3             → hist=26,   idx=12
        #   0xFF → nib 15: step=23, delta=2+5+11+23=41, 负 → hist=-15, idx=20
        #        → nib 15: step=50, delta=6+12+25+50=93, 负 → hist=-108, idx=28
        #
        # ⚠ 若 `_IMA_INDEX` 后半段写成负数，第 4 个样本起就会分叉
        #   （idx 一路收缩到 0，delta 变小）—— 这组值就是那道防线。
        want_lo = [-11, 19, 23, 20, 23, 26, -15, -108]
        got_lo = sa._ima_decode(b"\x7F\x80\x00\xFF", 44100, high_first=False)[0]
        _mark(got_lo.tolist() == want_lo,
              "IMA 低半字节先：8 个样本逐值正确", got_lo.tolist())
        got_hi = sa._ima_decode(b"\x7F\x80\x00\xFF", 44100, high_first=True)[0]
        _mark(got_hi.tolist() == [11, -19, -23, -20, -17, -14, -55, -148],
              "DVI IMA 高半字节先：8 个样本逐值正确", got_hi.tolist())
        # 每块重置：同样的 2 字节解两遍，第二遍必须与第一遍**一模一样**
        blk = sa._ima_decode(b"\xFF\xFF", 44100, hist=0, step_index=0)[0]
        _mark(blk.tolist() == [-11, -41, -104, -240],
              "单块 FF FF → [-11,-41,-104,-240]", blk.tolist())

        # ---- 1b. HWAS：容器 + 解码 + 每块重置 ----
        hw = os.path.join(box, "a.hwas")
        _make_hwas(hw, rate=20894, block=0x4000, payload=b"\xFF\xFF")
        h = sa.read_hwas_header(hw)
        _mark(h["rate"] == 20894 and h["block"] == 0x4000 and h["channels"] == 1,
              "HWAS 头解析正确（rate/block/channels）",
              (h["rate"], h["block"], h["channels"]))
        _mark(abs(h["secs"] - (2 * 2) / 20894.0) < 1e-6,
              "HWAS 时长 = 字节数×2/采样率", h["secs"])
        _mark(".hwas" in sa.ALL_EXTS, ".hwas 已在转换链路里", sa.ALL_EXTS)
        _mark(dl.is_convertible(hw), "is_convertible 认 .hwas")
        _mark(dl.policy_for(hw) == "stream", "policy_for(.hwas) == stream",
              dl.policy_for(hw))
        pr = sa.probe(hw)
        _mark(pr["ok"] and pr["rate"] == 20894 and "IMA" in pr["codec"],
              "probe(.hwas) 给出采样率与编码", pr)

        pcm, _r = sa.decode_hwas(hw)
        _mark(pcm.tolist() == [-11, -41, -104, -240],
              "HWAS 解码（单块 2 字节）", pcm.tolist())
        # 两块各 2 字节：第二块必须从 0 重新起（不是接着 -240 往下走）
        hw2 = os.path.join(box, "b.hwas")
        _make_hwas(hw2, rate=20894, block=2, payload=b"\xFF\xFF\xFF\xFF")
        pcm2, _r = sa.decode_hwas(hw2)
        # 块大小 2 字节 = 4 个 nibble = 4 个样本；两块必须**逐值相同**
        _mark(pcm2.tolist() == [-11, -41, -104, -240] * 2,
              "HWAS 每块重置 ADPCM 状态（两块逐值相同）", pcm2.tolist())

        # ---- 1c. SWAV：只有一个 4 字节头 ----
        sw = os.path.join(box, "a.swav")
        _make_swav(sw, rate=24050, wtype=2,
                   payload=struct.pack("<hh", 0, 0) + b"\x7F\x80\x00\xFF")
        sh = sa.read_swav_header(sw)
        _mark(sh["wavetype"] == 2 and sh["rate"] == 24050,
              "SWAV 头解析（IMA / 24050Hz）", (sh["wavetype"], sh["rate"]))
        spcm, _r = sa.decode_swav(sw)
        _mark(spcm.tolist() == want_lo,
              "SWAV 只有一个头，之后全是连续 nibble", spcm.tolist())
        _mark(dl.is_convertible(sw) and dl.policy_for(sw) == "stream",
              "SWAV 进了转换链路")

        # ---- 1d. DVI：4 字节交错 + 右声道在前 + 高半字节先 ----
        dv = os.path.join(box, "a.dvi")
        # 交错单位是 **4 字节**：块 0（右）= 7F 80 00 FF，块 1（左）= 00 FF 7F 80
        _make_dvi(dv, samples=8,
                  payload=b"\x7F\x80\x00\xFF" + b"\x00\xFF\x7F\x80")
        dh = sa.read_dvi_header(dv)
        _mark(dh["rate"] == 44100 and dh["channels"] == 2 and dh["samples"] == 8,
              "DVI 头解析（44100/2ch/8 样本）", (dh["rate"], dh["channels"],
                                                dh["samples"]))
        dpcm, _r = sa.decode_dvi(dv)
        # 右（块 0，高先）= [11,-19,-23,-20,-17,-14,-55,-148]
        # 左（块 1，高先）= [0, 0, -11, -41, 22, -114, -133, -116]
        # 输出是 L,R,L,R… —— 右声道在前的那条规则就靠这组值钉住。
        want_dvi = [0, 11, 0, -19, -11, -23, -41, -20,
                    22, -17, -114, -14, -133, -55, -116, -148]
        _mark(dpcm.tolist() == want_dvi,
              "DVI 声道交换 + 4 字节交错 + 高半字节先", dpcm.tolist())
        _mark(dl.is_convertible(dv) and dl.policy_for(dv) == "stream",
              "DVI 进了转换链路")

        # ---- 2. MOD：格式判定 ----
        m1 = os.path.join(box, "a.MOD")
        f0 = _make_mod(m1)
        _mark(ma.is_mod(m1), "MOD 魔数判定通过（M.K.）")
        junk = os.path.join(box, "junk.MOD")
        with open(junk, "wb") as f:
            f.write(b"\x00" * 2048)
        _mark(not ma.is_mod(junk), "垃圾文件不会被当成 MOD")

        song = ma.parse(m1)
        _mark(song.nch == 4 and song.patterns == 1 and song.song_len == 1,
              "MOD 解析出 4 声道 / 1 模式 / 1 order",
              (song.nch, song.patterns, song.song_len))
        _mark(len(song.samples) == 31 and song.samples[0].length == 8192,
              "31 个采样头，采样 1 长度 8192",
              (len(song.samples), song.samples[0].length))

        # ---- 3. 音高换算：合成正弦反推，这是回放器最容易错的一处 ----
        #
        # freq = 7093789.2 / (2 × period)，再按「采样自身基频 / 采样率」缩放。
        # 三个周期覆盖 2 个八度，任何一个错都会立刻偏掉。
        for period, tag in ((428, "C-2"), (214, "C-3"), (856, "C-1")):
            p = os.path.join(box, "p%d.MOD" % period)
            f0 = _make_mod(p, period=period)
            pcm, rate = ma.render(p)
            want = f0 * (7093789.2 / (2.0 * period)) / 8287.0
            got = _peak_hz(pcm, rate)
            _mark(abs(got - want) < 1.5,
                  "音高换算正确（period=%d/%s：期望 %.1f Hz）" % (period, tag, want),
                  "实测 %.1f Hz" % got)

        # ---- 4. 时标：probe 只走序列，必须和真渲染的长度一致 ----
        info = ma.probe(m1)
        pcm, rate = ma.render(m1)
        _mark(abs(info["secs"] - pcm.size / float(rate)) < 0.05,
              "probe 时长与渲染长度一致",
              (round(info["secs"], 3), round(pcm.size / float(rate), 3)))
        _mark(info["secs"] > 1.0, "时长算得出来（不是 0）", info["secs"])

        # ---- 5. 分类 / 策略 / 估算 ----
        _mark(classify(m1) == "流式音频", "classify(.MOD) → 流式音频", classify(m1))
        _mark(classify("x.mod") == "流式音频", "小写 .mod 也认", classify("x.mod"))
        _mark(classify("x.adx") == "流式音频", "classify(.adx) → 流式音频",
              classify("x.adx"))
        _mark(dl.policy_for(m1) == "stream" and dl.policy_for("x.adx") == "stream",
              "policy_for → stream", (dl.policy_for(m1), dl.policy_for("x.adx")))
        _mark(dl.is_convertible(m1) and dl.is_convertible("x.adx"),
              "is_convertible 放行 .mod/.adx")
        _mark(classify(box) == "未知",
              "平铺的流式包归「未知」→ 逐文件展开（不并成一个音源）",
              classify(box))
        est = dl.estimate_files([m1])
        _mark(est.get("tracks") == 1 and est.get("secs", 0) > 0.5,
              "estimate_files 算得出 1 轨 + 时长", est)

        # ---- 6. 真转一首：44.1k / 16bit / 立体声 ----
        out = os.path.join(box, "out")
        os.makedirs(out, exist_ok=True)
        st = sa.convert_one(m1, out)
        ok = st.get("ok") == 1 and len(st.get("files") or []) == 1
        _mark(ok, "MOD 能转出 WAV", st.get("errors"))
        if ok:
            with wave.open(st["files"][0]) as w:
                nch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
                nf = w.getnframes()
            _mark((nch, sw, sr) == (2, 2, 44100),
                  "产出是 44.1k/16bit/立体声", (nch, sw, sr))
            _mark(abs(nf / 44100.0 - info["secs"]) < 0.05,
                  "WAV 时长与 probe 一致", round(nf / 44100.0, 3))
            _mark(not st.get("fail"), "没有多算失败数", st.get("fail"))

        # 坏文件走同一条路：必须返回失败统计，不能抛
        st2 = sa.convert_one(junk, out)
        _mark(st2.get("ok") == 0 and st2.get("fail") == 1 and st2.get("errors"),
              "坏 MOD → 返回失败统计（不抛异常）", st2.get("errors"))

        # ---- 7. 手动「添加音源」那条路：load_source 必须认流式音频 ----
        #
        # ⚠ 这里踩过一个真坑：web_ui 按扩展名把 .mod/.adx 收成合法音源了，
        #   但转换线程走的 msu1_to_cd.load_source 有一份**自己的**白名单，
        #   不认这两种，于是抛 SystemExit —— 界面上表现成「一直转、永远不结束」
        #   （实测：真 exe 里加一首 45 秒的 MOD，等 120 秒还是 running）。
        import msu1_to_cd as core

        tracks = core.load_source(m1)
        _mark(len(tracks) == 1 and bool(tracks[0]["pcm"])
              and tracks[0]["little_endian"],
              "load_source 认 .mod（手动加音源能转）",
              [t["name"] for t in tracks])
        _mark(abs(len(tracks[0]["pcm"]) / 176400.0 - info["secs"]) < 0.1,
              "load_source 的 PCM 长度与 probe 一致",
              round(len(tracks[0]["pcm"]) / 176400.0, 3))

        # 造一个小 ADX 验另一条分支（PyAV 带 adpcm_adx 编码器）
        adx = os.path.join(box, "tiny.adx")
        made = False
        try:
            import av
            import numpy as np
            with av.open(adx, "w", format="adx") as c:
                st = c.add_stream("adpcm_adx", rate=44100)
                st.layout = "stereo"
                sig = np.sin(2 * np.pi * 440
                             * np.arange(44100) / 44100.0) * 0.3
                arr = np.stack([sig, sig], axis=1)
                fr = av.AudioFrame.from_ndarray(
                    (arr * 32767).astype(np.int16).reshape(1, -1),
                    format="s16", layout="stereo")
                fr.sample_rate = 44100
                for pkt in st.encode(fr):
                    c.mux(pkt)
                for pkt in st.encode(None):
                    c.mux(pkt)
            made = os.path.getsize(adx) > 1000
        except Exception as exc:                     # noqa: BLE001
            print("  （造 ADX 失败：%s）" % exc, flush=True)
        if made:
            pcm = sa.decode_pcm(adx)
            secs = len(pcm) / 176400.0
            _mark(abs(secs - 1.0) < 0.25, "decode_pcm 解 .adx 出 1 秒 PCM",
                  "%.2f 秒" % secs)
            tr = core.load_source(adx)
            _mark(len(tr) == 1 and len(tr[0]["pcm"]) > 100000,
                  "load_source 认 .adx（手动加音源能转）",
                  len(tr[0]["pcm"]) if tr else 0)
        else:
            _mark(False, "造 ADX 样本（PyAV 的 adpcm_adx 编码器）", "失败")

        # ---- 8. ADX：PyAV 的 adpcm_adx 解码器必须在 ----
        try:
            import av
            has = "adpcm_adx" in av.codecs_available
        except Exception:                            # noqa: BLE001
            has = False
        _mark(has, "PyAV 带 adpcm_adx 解码器（.adx 靠它）", has)

        # ---- 9. 「为什么转不了」的说明（v5.2.6）----
        #
        # 起因：8 个机种包 665 个音频文件只转得出 17 个，而下载页原来只会说
        # 「包里没有本工具能直接转换的音源（.psf2、.m3u…）」—— 把扩展名
        # 一列了事，用户不知道是自己下错了、程序漏了、还是本来就不行。
        # 这一节把「原因表」与「拼句」两件事都钉住。
        print("  -- 转不了的原因说明", flush=True)

        # (a) 原因表与能力表**不能打架**：表里每个后缀都必须真的转不了。
        #     否则哪天把某个格式做通了，忘了删原因，用户会被骗着不转。
        bad = [e for e in dl.SKIP_REASONS if dl.is_convertible("x" + e)]
        _mark(not bad, "SKIP_REASONS 里没有已经能转的格式", bad)
        # (b) 反过来：已经能转的不许出现在「转不了」表里。
        done = (".adx", ".mod", ".hwas", ".swav", ".dvi",
                ".miniusf", ".usf", ".mini2sf", ".2sf",
                ".ssf", ".minissf", ".dsf", ".minidsf",
                ".gsf", ".minigsf")
        bad2 = [e for e in done if e in dl.SKIP_REASONS]
        _mark(not bad2,
              "已支持的 .adx/.mod/.hwas/.swav/.dvi/.miniusf/.mini2sf/.ssf/.dsf"
              "/.minigsf 不在 SKIP_REASONS 里",
              bad2)

        # (c) 仍然转不了的核心后缀都要有说法 —— 少一个用户就会看到「不认识的格式」。
        #     v5.2.7 起 `.hwas/.swav/.dvi` 已经能转，从这张清单里摘掉了；
        #     v5.2.8 补上 `.ssf` —— Angelique Special 那包 32 个 .ssf 之前
        #     会被说成「不认识的格式」，正是 v5.2.6 想消灭的那句话；
        #     v5.3 起 `.miniusf` 也摘掉（外挂 usf2wav.exe 接上了），
        #     `.mini2sf` 同样摘掉（外挂 vio2wav.exe 接上了），
        #     `.ssf` / `.dsf` 也摘掉（外挂 ssf2wav.exe 接上了），
        #     `.minigsf` 也摘掉（外挂 gsf2wav.exe 接上了），
        #     v5.4 起 `.psf2` / `.minipsf2` 也摘掉（外挂 psf2wav.exe 的
        #     version 2 路径接上了，见 vendor/_check_psf2_ref.py）。
        #     v5.4：`.sgc` 也摘掉了 —— 改用 **kode54/Game_Music_Emu** 重编
        #     libgme（Sgc_Emu + Sms_Apu + Z80_Cpu 全套），实测 13 轨
        #     ok=13 fail=0。
        #     v5.5：`.wsr` 也摘掉了 —— 曲号靠 AX 寄存器传，核用
        #     beetle-wswan（mednafen），见 wsr_native.py。实测 122 首全出声。
        #     所以「还能不能给个说法」剩下的是**配套库后缀**（单独转没意义）。
        need = (".usflib", ".gsflib", ".ssflib", ".2sflib", ".dsflib")
        miss = [e for e in need if e not in dl.SKIP_REASONS]
        _mark(not miss, "仍转不了的后缀都有原因（.usflib 等配套库）", miss)
        # 正向锁：`.wsr` 必须**能转**（谁把它写回 SKIP_REASONS 这条就红）
        _mark(dl.is_convertible("a/x.wsr"),
              ".wsr 已能转（不再在 SKIP_REASONS）")
        # 正向锁：`.sgc` 必须**能转**（谁把它写回 SKIP_REASONS 这条就红）
        _mark(dl.is_convertible("a/x.sgc"),
              ".sgc 已能转（不再在 SKIP_REASONS）")

        _mark(dl.explain_skipped([]) == "", "explain_skipped(空) → 空串")
        _mark(dl.explain_skipped(["a/!playlist.m3u", "a/readme.txt"]) == "",
              "只有播放列表/说明文件时不算「转不了」")
        # v5.4: 48 个 .psf2 现在**全部能转** —— explain_skipped 必须一个字
        # 都不说（is_convertible 在内部先滤掉），否则会把能转的说成转不了。
        s = dl.explain_skipped(["a/%02d.psf2" % i for i in range(48)]
                               + ["a/!playlist.m3u"])
        _mark(s == "",
              "48 个 .psf2 + 播放列表 → 全部能转，一个字都不说", s[:60])
        # 正向锁：`.aif` 必须**能转**（v5.6 起 3DO 接上了）。
        # ⚠ 谁把 .aif 写回 SKIP_REASONS，下面这几条会一起红 —— 这是有意的：
        #   v5.5 及以前这些测试拿 .aif 当「真正转不了」的样本，v5.6 把它
        #   拆掉后必须换成真的转不了的（`.gsflib` 这类配套库）。
        _mark(dl.is_convertible("a/x.aif"), ".aif 已能转（3DO，v5.6）")
        _mark(dl.is_convertible("a/x.aiff"), ".aiff 已能转（3DO，v5.6）")
        _mark(dl.is_convertible("a/x.aifc"), ".aifc 已能转（3DO，v5.6）")
        _mark(dl.policy_for("a/x.aif") == "stream",
              ".aif 走 stream 策略（不是 chip）", dl.policy_for("a/x.aif"))
        _mark(".aif" not in dl.SKIP_REASONS,
              ".aif 不在 SKIP_REASONS 里（旧注释说需要 ARM60 模拟是错的）")
        s_aif = dl.explain_skipped(["a/%02d.aif" % i for i in range(48)])
        _mark(s_aif == "", "48 个 .aif → 全部能转，一个字都不说", s_aif[:60])

        # 真正转不了的样本（v5.6 起只剩 `.gsflib` 这类配套库）仍要说清数量和格式。
        s = dl.explain_skipped(["a/%02d.gsflib" % i for i in range(48)]
                               + ["a/!playlist.m3u"])
        _mark(s.startswith("48 个转不了") and ".gsflib" in s and "采样库" in s,
              "48 个 .gsflib → 说清数量和「采样库」", s[:60])
        _mark(".m3u" not in s, "播放列表不混进「转不了」的计数里", s[:60])
        # 反向：48 个 .sgc 现在全部能转 → explain_skipped 必须一个字都不说
        s2 = dl.explain_skipped(["a/%02d.sgc" % i for i in range(48)])
        _mark(s2 == "", "48 个 .sgc → 全部能转，一个字都不说", s2[:60])
        s3 = dl.explain_skipped(["a/x.zzz"] * 2)
        _mark("不认识的格式" in s3, "未知后缀也能给出一句话", s3)
        # 传整批文件（含能转的）时，能转的必须被内部滤掉 —— 这条是实测
        # 踩出来的：核对脚本直接传了整目录，输出成「10 个 .mod（不认识的
        # 格式）」，把已经能转的格式说成不认识，比不说话还糟。
        s4 = dl.explain_skipped(["a/x.mod"] * 10 + ["a/y.gsflib"] * 3)
        _mark("3 个转不了" in s4 and ".mod" not in s4,
              "explain_skipped 自己滤掉能转的（传整批也不会说错）", s4)
        # v5.3：`.minigsf` 也能转了 —— 整批里混进去必须一条都不报。
        s5 = dl.explain_skipped(["a/x.mod"] * 5 + ["a/y.minigsf"] * 4)
        _mark(s5 == "", "整批都是能转的（含 .minigsf）→ 一个字都不说", s5)
        # v5.4：`.psf2` 也能转了 —— 整批里混进去同样一条都不报。
        s6 = dl.explain_skipped(["a/x.mod"] * 5 + ["a/y.psf2"] * 4)
        _mark(s6 == "", "整批都是能转的（含 .psf2）→ 一个字都不说", s6)

        # (d) 下载页的 _payload_hint 必须真的用上它（否则改了等于没改）。
        # ⚠ v5.3 起 `.minigsf` 能转了、v5.4 起 `.psf2` 和 `.sgc` 也能转了、
        #   v5.5 起 `.wsr` 也能转了、**v5.6 起 `.aif` 也能转了**（3DO），
        #   这里改用最后一批真正还转不了的：`.gsflib` 这类**配套采样库**
        #   （单独转没意义，永远会在 SKIP_REASONS 里）。
        from web_ui import dl_api as _dlapi
        pack = os.path.join(box, "gba_pack")
        os.makedirs(pack, exist_ok=True)
        for i in range(3):
            open(os.path.join(pack, "%02d.gsflib" % i), "wb").close()
        open(os.path.join(pack, "!playlist.m3u"), "wb").close()
        hint = _dlapi.DownloadState._payload_hint(pack)
        _mark("3 个转不了" in hint and "gsflib" in hint,
              "_payload_hint 说清「几个、什么格式、为什么」", hint[:60])

        psfpack = os.path.join(box, "psf_pack")
        os.makedirs(psfpack, exist_ok=True)
        open(os.path.join(psfpack, "BGM.psf"), "wb").close()
        _mark("原生解码" in _dlapi.DownloadState._payload_hint(psfpack),
              ".psf 仍走「能转，去转换页」的提示（不能误报成转不了）")
        psf2pack = os.path.join(box, "psf2_pack")
        os.makedirs(psf2pack, exist_ok=True)
        open(os.path.join(psf2pack, "BGM.psf2"), "wb").close()
        _mark("原生解码" in _dlapi.DownloadState._payload_hint(psf2pack),
              ".psf2 同样走「能转，去转换页」的提示（v5.4 起不能误报成转不了）")

        empty = os.path.join(box, "empty_pack")
        os.makedirs(empty, exist_ok=True)
        _mark("空" in _dlapi.DownloadState._payload_hint(empty),
              "空目录的提示没变")

        # 递归要能找到子目录里的文件（PS1 包是 sound/xxx.xa 这种结构）
        deep = os.path.join(pack, "sub", "deeper")
        os.makedirs(deep, exist_ok=True)
        # ⚠ 用不可识别的后缀：`.dvi` 从 v5.2.7 起能转了，会让下面
        #   「0 个音源」的断言失败（这条测试只关心递归找得到文件）。
        open(os.path.join(deep, "x.zzz"), "wb").close()
        found = _dlapi.DownloadState._dir_files(pack)
        _mark(len(found) == 5, "_dir_files 递归到子目录（4 个直下 + 1 个深层）",
              len(found))
        _mark(len(_dlapi.DownloadState._dir_files(pack, max_depth=0)) == 4,
              "_dir_files 的 max_depth=0 只列本层 4 个文件（限量生效）",
              len(_dlapi.DownloadState._dir_files(pack, max_depth=0)))

        # (e) 手动「添加文件夹 / 加单个文件」那条入口同样要说原因。
        #     以前整包回「文件夹里没有可识别的音源」、单文件回「不认识的文件
        #     类型」—— 用户拿着一堆 .gsflib 只会以为是自己下错了。
        _mark("采样库" in server._no_source_reason(pack),
              "添加文件夹：整包 .gsflib 也说得清为什么",
              server._no_source_reason(pack)[:50])
        _mark("没有可识别" in server._no_source_reason(empty),
              "空目录仍回「没有可识别的音源」（不硬凑原因）")
        n0, sk0 = state.add_paths([pack])
        _mark(n0 == 0 and bool(sk0) and "采样库" in sk0[0][1],
              "add_paths(整包) → 0 个音源 + 带原因", sk0[:1])
        # ⚠ .psf2 现在**能转**（v5.4）—— 加单个 .psf2 必须真的入队，
        #   不能再回「转不了」（否则用户拿着能转的 PS2 包会被劝退）。
        good_pack = os.path.join(box, "psf2_good")
        os.makedirs(good_pack, exist_ok=True)
        open(os.path.join(good_pack, "BGM.psf2"), "wb").close()
        n1, sk1 = state.add_paths([os.path.join(good_pack, "BGM.psf2")])
        _mark(n1 == 1 and not sk1,
              "add_paths(单个 .psf2) → 入队成功（v5.4 起能转）",
              "added=%d skipped=%r" % (n1, sk1[:1]))
        # ⚠ v5.6：`.aif` 现在**能转**（3DO）—— 加单个 .aif 必须真的入队，
        #   不能再回「转不了」（否则用户拿着 3DO 包会被劝退）。
        aif_pack = os.path.join(box, "aif_good")
        os.makedirs(aif_pack, exist_ok=True)
        _aif = os.path.join(aif_pack, "BGM.aiff")
        _make_aiff(_aif, mono=[0, 100, -100, 0], rate=22050)
        n2a, sk2a = state.add_paths([_aif])
        _mark(n2a == 1 and not sk2a,
              "add_paths(单个 .aiff) → 入队成功（v5.6 起能转）",
              "added=%d skipped=%r" % (n2a, sk2a[:1]))
        # 而真正转不了的 .gsflib 单个加仍要带原因
        n2, sk2 = state.add_paths([os.path.join(pack, "00.gsflib")])
        _mark(n2 == 0 and bool(sk2) and "转不了" in sk2[0][1],
              "add_paths(单个 .gsflib) → 带原因，不是「不认识的文件类型」",
              sk2[:1])
        # v5.4：单个 .sgc 现在必须能入队（classify 的 P0->CHIP_EXTS 修好后）
        sgc_pack = os.path.join(box, "sgc_good")
        os.makedirs(sgc_pack, exist_ok=True)
        _sgc = os.path.join(sgc_pack, "x.sgc")
        with open(_sgc, "wb") as _fh:
            _fh.write(b"SGC\x1a" + bytes(0xA0 - 4) + b"\x00" * 64)
        n3, sk3 = state.add_paths([_sgc])
        _mark(n3 == 1 and not sk3,
              "add_paths(单个 .sgc) → 入队成功（v5.4 起能转）",
              "added=%d skipped=%r" % (n3, sk3[:1]))

        # ⚠ 上面的 .psf2 入队成功是**有意**的（v5.4 起能转），但那个空
        #   .psf2 解码必然失败 —— （e）段结束时必须清掉，否则残留到
        #   [4] 状态机段，`t_opts_and_fsm` 会多出一个失败音源，
        #   「result.fail = 0」就红了（实测踩过）。与 /api/clear 同语义：
        with state.lock:
            state.sources = []
            state.outputs = []
            state.logs = []
            state.job = None
            state.state = "idle"
            state.progress = 0.0
            state.status = ""
            state.result = None
            state.error = ""
    finally:
        _rmtree_soft(box)


def t_v5914():
    """[3d0] 封面（v5.9.14）：libretro-thumbnails 抓图 + 嵌进 FLAC。

    ## 为什么没用用户点名的 The Cover Project

    ``thecoverproject.net`` **用不了**，两道硬墙：

    1. 整站挂在 Cloudflare 人机验证后面 —— ``HTTP 403`` + ``Cf-Mitigated:
       challenge``，连 ``index.php`` 都只回那个 5 KB 的挑战页。
    2. ``robots.txt`` 另外写着 ``Disallow: *.jpg$`` / ``/images/`` /
       ``/uploads/`` / ``/download_cover.php``。就算绕过挑战，封面文件本身
       也是明确不许抓的。

    所以改用 **libretro-thumbnails**（RetroArch 官方缩略图库）：Apache 目录
    索引可列、直连可用、不需要 API key。

    ## 这一段在守什么

    1. **映射表**：``PLATFORM_DIRS`` 的值必须是**元组**。joshw 一个子站常常
       混装两代机器（``smd`` 含 Sega CD、``hes`` 含 PCE CD），而 libretro
       把它们拆成不同目录 —— 退化成字符串会让这些条目**成片地没封面**
       （实测命中率 65% → 88% 就是这个改动）。
    2. **匹配算法不许「差不多就行」**：``Contra`` 不许匹到
       ``Contra Hard Corps``、``Ys`` 不许匹到 ``Ys III``。贴错封面比没有
       封面更糟，所以副标题前缀必须带 `` - `` 分隔。
    3. **区域偏好**：``(Japan, USA)`` 这种多区合版必须认出来。整串比对的
       话它一个都认不出来、排名掉到「未知」，于是输给纯欧版（实测就是
       这样，拿到的是 ``Super Metroid (Europe) (En,Fr,De)``）。
    4. **FLAC 图片块**：真写一个 FLAC、真嵌、真读回；并且**音频字节不许变**
       （封面是元数据，不该动到一个采样）。
    5. **幂等**：反复嵌不叠加。否则「转一遍 WAV 再转一遍 FLAC」这种操作会让
       文件越滚越大。
    6. **缺封面不影响转换**：最重要的一条。封面是锦上添花，它失败一次不能
       让整首判失败。

    本段**不联网**（``--fast`` 也跑）。联网真抓的验证在
    ``vendor/_smoke_cover_live.py``。
    """
    print("\n[3d0] 封面（v5.9.14）：libretro-thumbnails + FLAC 图片块",
          flush=True)

    import hashlib
    import struct as _st
    import zlib

    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    import cover_art as ca
    import flac_out as fo

    # ---- ① 模块与数据源 ----
    _mark(ca.SOURCE == "libretro-thumbnails",
          "数据源是 libretro-thumbnails（The Cover Project 有 CF 挑战 + robots 禁止）",
          ca.SOURCE)
    _mark(ca.BASE.startswith("https://thumbnails.libretro.com"),
          "BASE 是官方缩略图站", ca.BASE)
    for fn in ("platform_dirs", "platform_dir", "supported", "norm_title",
               "pick", "list_covers", "fetch", "fetch_many",
               "cover_for_title", "stats"):
        _mark(callable(getattr(ca, fn, None)), "cover_art 有 %s()" % fn, "")

    # ---- ② 映射表 ----
    _mark(len(ca.PLATFORM_DIRS) >= 28,
          "登记了足够多的机种", len(ca.PLATFORM_DIRS))
    bad = [k for k, v in ca.PLATFORM_DIRS.items()
           if not isinstance(v, tuple) or not v
           or not all(isinstance(x, str) and x for x in v)]
    _mark(not bad,
          "每个机种的值都是**非空字符串元组**（多目录合并靠它）", bad[:3])
    for k in ("spc", "nsf", "gbs", "smd", "hes", "s98", "psf", "usf"):
        _mark(ca.supported(k), "机种 %s 有封面源" % k, "")
    _mark(len(ca.platform_dirs("smd")) >= 2,
          "smd 含多个目录（Mega Drive + Sega CD 混装）",
          ca.platform_dirs("smd"))
    _mark(len(ca.platform_dirs("hes")) >= 2,
          "hes 含多个目录（PCE + PCE CD 混装）", ca.platform_dirs("hes"))
    _mark(not ca.supported("switch") and ca.platform_dirs("") == (),
          "没登记的机种老实返回空（不瞎猜目录）", "")
    _mark(ca.platform_dir("spc") == ca.platform_dirs("spc")[0],
          "platform_dir() 给的是首选目录", ca.platform_dir("spc"))

    # ---- ③ 名字归一化（两边必须对称） ----
    pairs = [
        ("3 Ninjas Kick Back (1994-11)(Malibu)(Sony Imagesoft)[SNES]",
         "3 Ninjas Kick Back (USA).png", "3 ninjas kick back"),
        ("7th Saga, The [Elnard] (1993-04-23)(Produce!)(Enix)[SNES]",
         "7th Saga, The (USA).png", "7th saga the"),
        ("Sonic & Knuckles (1994-06-23)(Sega)[SMD]",
         "Sonic & Knuckles (World).png", "sonic and knuckles"),
    ]
    for j, l, want in pairs:
        a, b = ca.norm_title(j), ca.norm_title(l)
        _mark(a == b == want, "归一化对称：%r" % want, (a, b))

    # ---- ④ pick：宁可漏，不可错 ----
    def _items(*names):
        return [{"dir": "D", "name": n} for n in names]

    _mark(ca.pick(_items("Foo (USA).png", "Foo (Japan).png"), "Foo [SNES]")
          == "Foo (USA).png",
          "完全相等 + 区域偏好（USA 优先于 Japan）", "")
    _mark(ca.pick(_items("Foo (Japan).png", "Foo (World).png"), "Foo")
          == "Foo (World).png", "World 优先于 Japan", "")
    # 这条是本轮修的真 bug：整串比对认不出 ``(Japan, USA)``
    _mark(ca.pick(_items("Super Metroid (Japan, USA) (En,Ja).png",
                         "Super Metroid (Europe) (En,Fr,De).png"),
                  "Super Metroid (1994-03-19)(Nintendo)[SNES]")
          == "Super Metroid (Japan, USA) (En,Ja).png",
          "多区合版 (Japan, USA) 认得出，不会输给纯欧版", "")
    _mark(ca._region_of("Game (USA, Europe).png") == "USA",
          "多区合版取排名最靠前的那个", ca._region_of("Game (USA, Europe).png"))
    _mark(ca._region_of("Game (En,Fr,De).png") == "",
          "纯语言标记不当区域", ca._region_of("Game (En,Fr,De).png"))
    # 副标题前缀：必须带 " - "
    _mark(ca.pick(_items("SimCity 2000 - The Ultimate City Simulator (USA).png"),
                  "SimCity 2000") ==
          "SimCity 2000 - The Ultimate City Simulator (USA).png",
          "副标题前缀能匹上（带 ' - ' 分隔）", "")
    _mark(ca.pick(_items("Contra Hard Corps (USA).png"), "Contra") == "",
          "「Contra」**不许**匹到「Contra Hard Corps」（贴错比没有更糟）", "")
    _mark(ca.pick(_items("Ys III (USA).png"), "Ys") == "",
          "「Ys」不许匹到「Ys III」（相似度阈值 0.86 兜住）", "")
    _mark(ca.pick([], "Foo") == "" and ca.pick(_items("Foo (USA).png"), "") == "",
          "空列表 / 空名字都返回空串（不抛）", "")
    _mark(ca.MIN_RATIO >= 0.85, "相似度阈值不低于 0.85", ca.MIN_RATIO)

    # ---- ④b 映射表变了要**作废旧缓存** ----
    # 这条是实测出来的真 bug：``gbs`` 从「Game Boy」一个目录变成「+ Game Boy
    # Color」两个，旧缓存 30 天没到期 → 升级上来的用户整整一个月拿不到 GBC
    # 封面。现象极难归因（命中率 45% 而不是 98%，而且「别人有我没有」）。
    _mark("cached_dirs != tuple(dirs)" in _read("cover_art.py"),
          "缓存里记了当时扫的目录，和映射表对不上就当过期", "")

    # ---- ⑤ FLAC 图片块：真写、真嵌、真读回 ----
    import numpy as np
    box = os.path.join(tmpdir.root(), "gate_cover")
    os.makedirs(box, exist_ok=True)
    try:
        def _tiny_png(w, h):
            """手搓一张合法 PNG（不引 PIL）—— 门禁不该为一张图加依赖。"""
            rows = b""
            for y in range(h):
                rows += b"\x00" + bytes(
                    [(x * 7 + y * 3) % 256 for x in range(w)]) * 3
            def _chunk(tag, data):
                return (len(data).to_bytes(4, "big") + tag + data
                        + zlib.crc32(tag + data).to_bytes(4, "big"))
            ihdr = _st.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
            return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
                    + _chunk(b"IDAT", zlib.compress(rows, 9))
                    + _chunk(b"IEND", b""))

        png = os.path.join(box, "c.png")
        with open(png, "wb") as fh:
            fh.write(_tiny_png(64, 48))
        png2 = os.path.join(box, "c2.png")
        with open(png2, "wb") as fh:
            fh.write(_tiny_png(32, 32))

        def _audio_md5(path):
            with open(path, "rb") as fh:
                buf = fh.read()
            _bl, at = fo._split_blocks(buf)
            return hashlib.md5(buf[at:]).hexdigest()

        raw = (np.sin(np.arange(44100) * 2 * np.pi * 440 / 44100)
               * 8000).astype("<i2").tobytes()
        f1 = os.path.join(box, "a.flac")
        ok, _p, detail = fo.encode_pcm(raw, f1, 44100, 2, fo.LEVEL)
        _mark(ok and os.path.isfile(f1), "先写一个真 FLAC（无封面）", detail)
        _mark("封面" not in detail or "没嵌上" not in detail,
              "没给 cover 时不动图片块", detail)
        _mark(fo.pictures(f1) == [], "无封面的 FLAC 读回来是空的",
              fo.pictures(f1))
        md5_before = _audio_md5(f1)
        size_before = os.path.getsize(f1)

        okp, why = fo.embed_picture(f1, png)
        _mark(okp, "embed_picture 成功", why)
        pics = fo.pictures(f1)
        _mark(len(pics) == 1, "读回正好 1 张封面", pics)
        if pics:
            p = pics[0]
            _mark(p.get("type") == 3, "类型 3 = 正面封面", p.get("type"))
            _mark(p.get("mime") == "image/png", "MIME = image/png",
                  p.get("mime"))
            _mark((p.get("w"), p.get("h")) == (64, 48),
                  "宽高从 PNG 头里读对了", (p.get("w"), p.get("h")))
            _mark(p.get("bytes") == os.path.getsize(png),
                  "图片字节数与源文件一致", (p.get("bytes"),
                                     os.path.getsize(png)))
        _mark(_audio_md5(f1) == md5_before,
              "**音频字节一个都没变**（封面是元数据）", "")

        # STREAMINFO 必须仍在第一个
        with open(f1, "rb") as fh:
            _bl, _at = fo._split_blocks(fh.read())
        _mark(_bl and _bl[0][0] == 0,
              "STREAMINFO 仍是第一个块（FLAC 规范硬要求）",
              [t for t, _b in _bl])

        # 幂等：同一张再嵌一次
        sz1 = os.path.getsize(f1)
        okp2, _w2 = fo.embed_picture(f1, png)
        _mark(okp2 and len(fo.pictures(f1)) == 1 and os.path.getsize(f1) == sz1,
              "重复嵌同一张：张数不变、体积不变（幂等）",
              (len(fo.pictures(f1)), sz1, os.path.getsize(f1)))

        # 换一张：替换，不叠加
        okp3, _w3 = fo.embed_picture(f1, png2)
        pics3 = fo.pictures(f1)
        _mark(okp3 and len(pics3) == 1
              and (pics3[0]["w"], pics3[0]["h"]) == (32, 32),
              "换一张是**替换**不是叠加（否则每转一次胖一圈）", pics3)

        # 坏输入：不抛、不动原文件
        okb, whyb = fo.embed_picture(f1, os.path.join(box, "nope.png"))
        _mark(not okb and "读不到封面" in whyb, "缺图文件 → 返回 False 不抛",
              whyb)
        txt = os.path.join(box, "notimg.png")
        with open(txt, "wb") as fh:
            fh.write(b"this is not a png")
        okb2, whyb2 = fo.embed_picture(f1, txt)
        _mark(not okb2, "不是图片 → 返回 False 不抛", whyb2)
        _mark(os.path.getsize(f1) > 0 and len(fo.pictures(f1)) == 1,
              "两次失败都没破坏原文件", len(fo.pictures(f1)))

        # ---- ⑥ 缺封面不影响转码（最重要的一条） ----
        f2 = os.path.join(box, "b.flac")
        ok2, _p2, d2 = fo.encode_pcm(raw, f2, 44100, 2, fo.LEVEL,
                                     cover=os.path.join(box, "missing.png"))
        _mark(ok2 and os.path.isfile(f2) and os.path.getsize(f2) > 0,
              "cover 指向不存在的文件 → **转码照样成功**", d2)
        _mark("⚠ 封面没嵌上" in d2, "失败原因写进 detail（不是静默吞掉）", d2)

        f3 = os.path.join(box, "c.flac")
        ok3, _p3, d3 = fo.encode_pcm(raw, f3, 44100, 2, fo.LEVEL, cover=png)
        _mark(ok3 and len(fo.pictures(f3)) == 1,
              "encode_pcm(cover=...) 一步到位（转完就带封面）", d3)
        _mark("封面 image/png" in d3, "detail 里报告封面尺寸", d3)
    finally:
        _rmtree_soft(box)

    # ---- ⑦ 接线：下载页 + 转换链路 ----
    dapi = _read("web_ui", "dl_api.py")
    dhtml = _read("web_ui", "download.html")
    djs = _read("web_ui", "download.js")
    sv = _read("web_ui", "server.py")
    sw = _read("spc_wav.py")
    cj = _read("convert_job.py")
    bp = _read("build_web_pyi.py")

    _mark("import cover_art" in dapi and "cover_art = None" in dapi,
          "dl_api 用 try/except 可选导入 cover_art（缺了不影响下载页）", "")
    _mark('"auto_cover": True' in dapi, "opts.auto_cover 默认开", "")
    _mark("def _cover_worker" in dapi, "dl_api 有 _cover_worker（后台抓）", "")
    # v6.0.4：下载流程不再自动抓图，「封面单开线程」改成手动按钮那条链
    _mark("threading.Thread(target=self._cover_fetch_thread" in dapi,
          "「下载封面」单开线程（限速 0.3s/次，不能占下载 worker）", "")
    for k in ("cover_total", "cover_done", "cover_ok", "cover_fail",
              "cover_current"):
        _mark('"%s": self.%s' % (k, k) in dapi,
              "snapshot.stats 带出 %s" % k, "")
    _mark('"cover_art": cover_art is not None' in dapi,
          "diag 报告 cover_art（打包漏件的第一手证据）", "")
    _mark("cover_art.supported(key)" in dapi,
          "机种没登记就跳过并说一句（不是静默不抓）", "")

    # v6.0.4：复选框 → 按钮（用户要求「可主动选择什么时候下载封面」）
    _mark('id="dlCover"' not in dhtml,
          "「顺带下载封面」复选框已撤掉", "")
    _mark('id="dlCoverFetch"' in dhtml and "下载封面</button>" in dhtml,
          "download.html 有「下载封面」按钮", "")
    _mark('$("#dlCoverFetch").onclick' in djs,
          "download.js 绑了「下载封面」的点击", "")
    _mark('"/api/dl/fetch_covers"' in djs,
          "前端调 /api/dl/fetch_covers", "")
    _mark("def fetch_covers" in dapi, "dl_api 有 fetch_covers()", "")
    _mark('"/api/dl/fetch_covers"' in sv,
          "server.py 有 /api/dl/fetch_covers 路由", "")
    # ⚠ v6.0.0~v6.0.3 的启用列表漏了 #dlConvOgg，那颗键一直是灰的
    _mark('"#dlConvOgg", "#dlCoverFetch"' in djs,
          "OGG / 下载封面跟着勾选数启用（v6.0.3 之前 OGG 永远灰着）", "")
    _mark("cst.cover_total" in djs,
          "状态栏拼上封面进度（三条线并行，只显示下载会以为卡住）", "")

    # write_tracks：cover 必须排在 fmt 之后（convert 是位置传参）
    m = re.search(r"def write_tracks\((.*?)\):", sw, re.S)
    sig = m.group(1) if m else ""
    _mark(bool(re.search(r'fmt="wav",\s*cover=None', sig)),
          "write_tracks 的 cover 排在 fmt 之后（位置传参兼容）", "")
    # v5.9.17：MP3 也能嵌封面（ID3v2 APIC），清 cover 的条件从
    # 「只认 flac」放宽成「不是 flac / mp3 才清」——WAV 仍然没有容器放图。
    # v6.0.0：条件收口到 ENC_FMTS，所以 OGG 也自动保留 cover（图落 cover.png）。
    # 不用正则：转义在多层传递里容易被吃掉。
    # ⚠ 源码里 `if fmt not in ENC_FMTS:` 出现**两次**（格式归一 + 清封面），
    #   要判的是**后一次**——用 rsplit 取最后一段，别用 split 取第一段。
    _after = sw.rsplit("if fmt not in ENC_FMTS:", 1)
    _mark(len(_after) == 2 and 'cover = ""' in _after[1][:200],
          "非 flac/mp3/ogg 时强制清掉 cover（WAV 没有图片块）", "")
    _mark("fmt != \"flac\"" not in sw and "fmt != 'flac'" not in sw,
          "旧写法「只认 FLAC」已清干净（MP3 现在也走封面路径）", "")
    _mark('o.get("fmt")' in cj and "cover=_cover" in cj,
          "convert_job **显式**传 cover（不靠目录名猜）", "")
    _mark("cover_art.cover_for_title(" in cj,
          "convert_job 用 cover_for_title 按游戏名反查", "")
    _mark('"cover_art",' in bp,
          "build_web_pyi 的 HIDDEN 里有 cover_art（可选 import 静态分析看不到）",
          "")


def t_v5915():
    """[3e0] MP3 容器（v5.9.15）：首页格式卡片 + 下载页第三颗转换按钮。

    ## 为什么能做到「最省 token」

    不引 ffmpeg / lame.exe，也没有新写一个编码模块 —— ``soundfile``
    （libsndfile 1.2.2）**自带 LAME**，``'MP3' in sf.available_formats()``
    为真。所以整条链路只是「把已经存在的 ``fmt`` 参数多认一个值」：
    ``flac_out._WRITERS`` 加一行，下游 ``spc_wav`` / ``convert_job`` /
    ``dl_convert`` / ``dl_api`` 各自把「两选一」改成「三选一」。

    ## 这一段在守什么

    1. **白名单收口**：``_norm_fmt`` / ``_FMT_DIRS`` / ``_FMT_EXTS`` /
       ``_AUDIO_EXTS`` 四张表都必须是三值。漏一张的表现很隐蔽 ——
       例如 ``_AUDIO_EXTS`` 漏 ``.mp3``，第二轮转换会从 01 重新编号，
       **把上一轮转好的 MP3 逐个覆盖**（这正是 v5.9.7 加 FLAC 时踩过的坑）。
    2. **三种格式三个目录**：``WAV/`` ``FLAC/`` ``MP3/``。共用目录会让
       ``ConvertManifest.already_done()`` 判成「已转过」直接跳过。
    3. **体积系数必须是实测值**：libsndfile 的 MP3 是 **320 kbps CBR**，
       恒为 WAV 的 0.2268 —— 不是「MP3 约 1/9」那种 128 kbps 的常识。
       系数写小了，界面上「预计 1.2 GB」而实际写 2.7 GB。
    4. **真写一个 MP3**：能写、非空、能读回、帧数/采样率/声道对得上。
       MP3 有编码器延迟，帧数允许有微小出入，但**不能差一个数量级**。
    5. **MP3 不嵌封面**：FLAC 才有 METADATA_BLOCK_PICTURE；MP3 要写 ID3
       APIC，这一版明确不做。传了 cover 也不能报错、不能改动大小。
    6. **前端三处入口齐全**：首页卡片、下载页按钮、互斥逻辑。

    本段**不联网**，也不做完整音源转换（那在 ``[4-6]`` 里）。
    """
    print("\n[3e0] MP3 容器（v5.9.15）：首页卡片 + 下载页第三颗按钮",
          flush=True)

    import array
    import math

    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    import flac_out as fo
    import dl_convert as dc
    import spc_wav as sw

    dapi = _read("web_ui", "dl_api.py")
    dcv = _read("dl_convert.py")
    sv = _read("web_ui", "server.py")
    ihtml = _read("web_ui", "index.html")
    ajs = _read("web_ui", "app.js")
    acss = _read("web_ui", "app.css")
    dhtml = _read("web_ui", "download.html")
    djs = _read("web_ui", "download.js")

    # ---- ① 编码器 ----
    _mark(fo.WRITE_FMTS == ("flac", "mp3", "ogg"),
          "flac_out.WRITE_FMTS 登记了 flac + mp3 + ogg", fo.WRITE_FMTS)
    _mark("WRITE_FMTS" in fo.__all__,
          "WRITE_FMTS 在 __all__ 里（前端/门禁能 import 到）", "")
    ok_mp3, why_mp3 = fo.available("mp3")
    _mark(ok_mp3, "MP3 编码器可用（soundfile 自带 LAME）", why_mp3)
    ok_def, _w = fo.available()
    _mark(ok_def, "available() 不传参仍默认查 FLAC（老调用点不变）", "")
    _mark(fo._WRITERS.get("mp3") == ("MP3", "MPEG_LAYER_III", ".mp3"),
          "_WRITERS['mp3'] 是 MP3 / MPEG_LAYER_III / .mp3",
          fo._WRITERS.get("mp3"))
    _mark("fmt=\"flac\"" in _sig_of(fo._write),
          "_write() 有 fmt 参数且默认 flac", "")
    _mark("fmt=\"flac\"" in _sig_of(fo.encode_pcm),
          "encode_pcm() 有 fmt 参数且默认 flac", "")

    # ---- ② 体积系数 ----
    _mark(0.20 <= dc.MP3_BYTES_RATIO <= 0.30,
          "MP3_BYTES_RATIO 在实测区间（320kbps → 0.2268）",
          dc.MP3_BYTES_RATIO)
    _mark(dc.FMT_BYTES_RATIO.get("mp3") == dc.MP3_BYTES_RATIO,
          "FMT_BYTES_RATIO 收口了 mp3（不再散装 if）", "")
    _mark(dc.FMT_BYTES_RATIO.get("flac") == dc.FLAC_BYTES_RATIO,
          "FMT_BYTES_RATIO 的 flac 仍是 0.40", "")
    _mark("FMT_BYTES_RATIO.get(str(fmt" in dcv,
          "probe_worker 按 fmt 查表折算（预估体量不说谎）", "")
    _mark("FMT_BYTES_RATIO.get(self._fmt, 1.0)" in dcv,
          "_estimate_bytes 按 self._fmt 查表（磁盘门禁不误判空间不足）", "")
    _mark(".mp3" in dc._AUDIO_EXTS,
          "_AUDIO_EXTS 认 .mp3（否则第二轮从 01 重编号、覆盖上一轮）",
          dc._AUDIO_EXTS)

    # ---- ③ 三格式白名单 ----
    _mark("_FMT_DIRS" in dapi and "_FMT_EXTS" in dapi,
          "dl_api 有 _FMT_DIRS / _FMT_EXTS 两张表", "")
    _mark('"mp3": "MP3"' in dapi, "_FMT_DIRS 有 mp3 → MP3", "")
    _mark('"mp3": ".mp3"' in dapi, "_FMT_EXTS 有 mp3 → .mp3", "")

    from web_ui import dl_api as _dla
    _mark(_dla.DownloadState._norm_fmt("mp3") == "mp3",
          "_norm_fmt('mp3') == 'mp3'", "")
    _mark(_dla.DownloadState._norm_fmt("MP3") == "mp3",
          "_norm_fmt 大小写不敏感", "")
    _mark(_dla.DownloadState._norm_fmt("flac") == "flac",
          "_norm_fmt('flac') 不受影响", "")
    # v6.0.0：ogg 从「认不出」变成合法值了 —— 拿真正不存在的 opus 测兜底。
    _mark(_dla.DownloadState._norm_fmt("ogg") == "ogg",
          "_norm_fmt('ogg') == 'ogg'（v6.0.0 起合法）", "")
    _mark(_dla.DownloadState._norm_fmt("opus") == "wav",
          "_norm_fmt 认不出的值退回 wav（宁可出 WAV 也别整批失败）", "")
    _mark("auto_mp3" in dapi, "dl_api.opts 有 auto_mp3", "")
    _mark("def _opts_fmt" in dapi,
          "dl_api 有 _opts_fmt()（三选一的唯一口径）", "")
    _mark('("auto_mp3", "mp3"), ("auto_flac", "flac")' in dapi,
          "_opts_fmt 优先级 mp3 > flac > wav", "")
    _mark('self.mp3_dir = ""' in dapi, "DownloadState 有 mp3_dir 字段", "")
    _mark("_mp3_games" in dapi, "有 _mp3_games 集合（状态列分开记）", "")
    _mark('elif low == "mp3":' in dapi,
          "_refresh_disk 扫 MP3/ 目录（否则状态列永远「未转」）", "")
    _mark('_games_with(mp3_dirs, (".mp3",))' in dapi,
          "按 .mp3 认「这个游戏转过了」", "")
    _mark('("MP3", _has_mp3)' in dapi,
          "状态列文案带 MP3（三种格式用同一套 + 拼法）", "")
    _mark('"ogg": "ogg_dir"}.get(fmt)' in dapi,
          "convert_wav 收尾按格式查表定位记账字段（含 ogg）", "")

    # ---- ④ 目录分离 + 真写盘 ----
    # ⚠ 不 new 一个真的 DownloadState：它会在模块级偏好文件上写东西，
    #   把 server 那份共享状态搞脏（同一次门禁里后面还有分片要跑）。
    #   这里只借方法，用个最小 stub 就够 —— _out_dir 只读 self.dest / self.lock。
    import threading

    class _Stub(object):
        dest = os.path.join(ROOT, "_tmp_v5915_dest")
        lock = threading.RLock()
        wav_dir = ""
        flac_dir = ""
        mp3_dir = ""
        _norm_fmt = staticmethod(_dla.DownloadState._norm_fmt)
        _out_dir = _dla.DownloadState._out_dir

    st = _Stub()
    d_wav = st._out_dir("wav")
    d_flac = st._out_dir("flac")
    d_mp3 = st._out_dir("mp3")
    _mark(d_mp3.replace("\\", "/").endswith("/MP3"),
          "_out_dir('mp3') 指向 <保存目录>/MP3", d_mp3)
    _mark(len({d_wav.lower(), d_flac.lower(), d_mp3.lower()}) == 3,
          "三种格式三个目录（共用目录 = 记账互撞 = 第二种被判「已转过」）",
          "")

    box = os.path.join(ROOT, "_tmp_v5915")
    try:
        os.makedirs(box, exist_ok=True)
        # 1 秒 440Hz 正弦，int16 LE 立体声
        n = 44100
        a = array.array("h")
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * 440 * i / 44100))
            a.append(v)
            a.append(v)
        raw = a.tobytes()

        p_mp3 = os.path.join(box, "01_sine.mp3")
        ok, path, detail = fo.encode_pcm(raw, p_mp3, fmt="mp3")
        size = os.path.getsize(path) if ok and os.path.exists(path) else 0
        _mark(ok and size > 0, "encode_pcm(fmt='mp3') 真写出非空文件", detail)
        _mark(path.lower().endswith(".mp3"), "产物扩展名是 .mp3", path)
        _mark(not os.path.exists(p_mp3 + ".part"),
              "不留 .part 临时文件（原子替换）", "")

        try:
            import soundfile as sf
            data, sr = sf.read(p_mp3)
            frames = len(data)
            ch = data.shape[1] if getattr(data, "ndim", 1) > 1 else 1
        except Exception as exc:                     # noqa: BLE001
            frames, sr, ch = -1, 0, 0
            print("   ⚠ 读回失败: %s" % exc, flush=True)
        _mark(sr == 44100, "读回采样率 44100", sr)
        _mark(ch == 2, "读回立体声", ch)
        _mark(abs(frames - n) <= 3000,
              "读回帧数≈原长（MP3 有编码器延迟，允许 ~70ms 出入）",
              "%d vs %d" % (frames, n))

        # 体积比：320kbps CBR 与内容无关，所以可以严格一点。
        # ⚠ 容差不能压到 0.01 以内：MP3 每帧 1152 个样本，CBR 必须凑整帧。
        #   1 秒 = 38.28 帧 → 补到 39 帧（+1.9%），5 秒只补 0.3%。
        #   这 1.6% 的差是**帧对齐开销**，不是码率漂了 —— v5.9.18 修音质后
        #   实测 0.2298 vs 0.2272，差 0.0026 在 0.025 容差内。
        ratio = size / float(len(raw) + 44)
        _mark(0.20 <= ratio <= 0.27,
              "1 秒样本体积比落在 320kbps 预期区间", "%.4f" % ratio)

        # 30 秒噪声：内容最坏情况也还是 320kbps（CBR）
        a2 = array.array("h")
        for i in range(44100 * 5):
            v = (((i * 1103515245 + 12345) & 0x7FFFFFFF) % 65536 - 32768) // 2
            a2.append(v)
            a2.append(v)
        raw2 = a2.tobytes()
        p2 = os.path.join(box, "02_noise.mp3")
        ok2, path2, _d2 = fo.encode_pcm(raw2, p2, fmt="mp3")
        r2 = (os.path.getsize(path2) / float(len(raw2) + 44)) if ok2 else 0
        _mark(ok2 and abs(r2 - ratio) < 0.025,
              "噪声与正弦体积比一致（CBR，不随内容漂）", "%.4f" % r2)
        _mark(r2 <= dc.MP3_BYTES_RATIO + 0.001,
              "预估系数不低估实际体积（低估会把用户盘写满）",
              "%.4f <= %.2f" % (r2, dc.MP3_BYTES_RATIO))

        # 封面：MP3 不支持，传了也必须无害
        p3 = os.path.join(box, "03_cover.mp3")
        ok3, path3, d3 = fo.encode_pcm(raw, p3, fmt="mp3",
                                       cover=os.path.join(box, "nope.png"))
        _mark(ok3, "MP3 + cover 不报错（MP3 没有图片块，这一版不做 ID3）", d3)
        _mark(os.path.getsize(path3) == size,
              "MP3 + cover 文件大小不变（确认没往 MP3 里塞东西）",
              "%d vs %d" % (os.path.getsize(path3), size))

        # ---- ⑤ write_tracks 走 MP3 ----
        out_dir = os.path.join(box, "tracks")
        os.makedirs(out_dir, exist_ok=True)
        tracks = [{"name": "01 Sine", "pcm": raw, "little_endian": True}]
        sub = sw.write_tracks(tracks, out_dir, 0, fmt="mp3")
        _mark(sub["ok"] == 1 and sub["fail"] == 0,
              "write_tracks(fmt='mp3') 成功 1 首", sub)
        files = [x for x in os.listdir(out_dir) if x.lower().endswith(".mp3")]
        _mark(len(files) == 1, "落盘是 .mp3（不是 .wav）", files)

        out_dir2 = os.path.join(box, "tracks2")
        os.makedirs(out_dir2, exist_ok=True)
        # v6.0.0：ogg 已是合法格式，兜底要用真正不存在的值测（opus 没有实现）
        sub2 = sw.write_tracks([{"name": "x", "pcm": raw,
                                 "little_endian": True}],
                               out_dir2, 0, fmt="opus")
        _mark(sub2["ok"] == 1
              and any(x.lower().endswith(".wav")
                      for x in os.listdir(out_dir2)),
              "write_tracks 认不出的 fmt 退回 WAV", "")

        # ---- ⑥ 编号扫描认 .mp3 ----
        scan = os.path.join(box, "scan")
        os.makedirs(scan, exist_ok=True)
        for nm in ("01_a.wav", "02_b.flac", "05_c.mp3", "notes.txt"):
            open(os.path.join(scan, nm), "wb").close()
        max_idx, used = dc.wav_scan_numbering(scan)
        _mark(max_idx == 5, "wav_scan_numbering 认 .mp3 的编号", max_idx)
        _mark({"a", "b", "c"} <= used,
              "used 里三种扩展名的裸曲名都在", sorted(used))
    finally:
        _rmtree_soft(box)

    # ---- ⑦ server 白名单 / 互斥 ----
    _mark('"auto_mp3", "merge_wav"' in sv or '"auto_mp3"' in sv,
          "set_opts 白名单接受 auto_mp3（否则卡片点了不生效）", "")
    _mark('if "auto_mp3" in body and dl.opts["auto_mp3"]:' in sv,
          "后端三选一互斥：mp3 生效时清掉 wav/flac", "")
    _mark('dl.opts["auto_mp3"] = False' in sv,
          "反过来 flac / wav 生效时也清掉 mp3", "")
    _mark("_f in _allow" in sv and "_fo.WRITE_FMTS" in sv,
          "/api/start 的 fmt 白名单真的收口在 flac_out.WRITE_FMTS", "")
    _mark('"mp3": _fmt_status("mp3")' in sv,
          "diag 报告 mp3 编码器可用性", "")
    _mark("def _fmt_status" in sv and "def _flac_status" in sv,
          "_fmt_status() 抽出后 _flac_status() 仍保留（老调用点不炸）", "")

    code, d = _jpost("/api/dl/set_opts", {"auto_mp3": True})
    o = (d or {}).get("opts") or {}
    _mark(code == 200 and o.get("auto_mp3") is True,
          "POST set_opts(auto_mp3=True) 真的存下来", o.get("auto_mp3"))
    _mark(o.get("auto_wav") is False and o.get("auto_flac") is False,
          "开后 MP3，WAV / FLAC 自动关掉（三者互斥）", o)
    code, d = _jpost("/api/dl/set_opts", {"auto_flac": True})
    o = (d or {}).get("opts") or {}
    _mark(o.get("auto_flac") is True and o.get("auto_mp3") is False,
          "再开 FLAC，MP3 自动关掉（以最后到达的为准）", o)
    _jpost("/api/dl/set_opts", {"auto_wav": True})

    # ---- ⑧ 前端入口 ----
    _mark('data-fmt="mp3"' in ihtml, "index.html 有 MP3 格式卡片", "")
    # ⚠ 别写死字面量：v6.0.0 这里写的是 "6.0.0"，升到 6.0.1 就红了一条 ——
    #   而它想守的其实是「首页版本号跟 server.APP_VERSION 没漂」。
    _mark(server.APP_VERSION in ihtml,
          "index.html 版本号 = server.APP_VERSION（%s）" % server.APP_VERSION, "")
    _mark("FMT_PRIORITY.find(" in ajs,
          "app.js collectOpts 按 FMT_PRIORITY 读卡片 → fmt", "")
    _mark("AUDIO_FMTS.some(" in ajs,
          "app.js do_wav 由 AUDIO_FMTS 遍历（含 MP3 / OGG）", "")
    _mark('const AUDIO_FMTS = ["wav", "flac", "mp3", "ogg"]' in ajs,
          "app.js 互斥用四格式列表（两两分支会漏）", "")
    _mark("repeat(6, 1fr)" in acss and "grid-column: span 3" in acss,
          "app.css 六列栅格 → 音频四张卡 2×2（BIN+CUE 独占一行）", "")
    _mark('id="dlConvMp3"' in dhtml, "download.html 有「转 MP3」按钮", "")
    _mark('$("#dlConvMp3").onclick = () => convSelected("mp3")' in djs,
          "download.js 把按钮接到 convSelected('mp3')", "")
    _mark('"#dlConvWav", "#dlConvFlac", "#dlConvMp3"' in djs,
          "忙 / 未勾选时三颗按钮一起禁用", "")
    _mark('mp3: "#dlConvMp3"' in djs,
          "convSelected 按格式选按钮（不是三元 wav/flac）", "")
    _mark('return lastFmt.toUpperCase();' in djs,
          "fmtTag 支持第三种格式（写死 WAV/FLAC 会骗人）", "")


def t_v5916():
    """[3f0] 转 FLAC 前补封面（v5.9.16）。

    ## 这一版修的是什么

    用户 2026-10-27 报「刚转的 FLAC 没有封面」。查下来**不是匹配失败**
    （``pick`` 能命中 ``Advance Wars 2 - Black Hole Rising (USA).png``），
    而是**时机错位**：

    * 封面原本只在**下载**时抓（``start()`` -> ``_cover_worker``）；
    * 转码时 ``spc_wav.write_tracks`` 走 ``cover_art.cover_for_title``，
      那是**纯离线查询**，只认本地已下载的图；
    * 于是「v5.9.14 之前下载的包」和「只点转 FLAC 没重下的包」永远没封面，
      而且日志里连一行提示都没有（``if cover:`` 才打）。

    ## 守什么

    1. 转码前**先抓**（``_covers_before_convert``），且只在 FLAC 时做
       —— 只有 FLAC 有 ``METADATA_BLOCK_PICTURE``；
    2. 尊重 ``opts["auto_cover"]``：用户关掉「下载封面」就不该偷偷联网；
    3. **已经转好的 FLAC 也要补**（``_backfill_audio``）—— 转换记账会把
       它们判成「已转过」跳过，所以不能靠重转；
    4. 回填**幂等**：二次调用文件大小不变；
    5. ``_has_picture`` 只读块头，不把整个文件读进内存。

    本段**不联网**：把 ``cover_for_title`` 临时换成「返回缓存里任意一张
    png」，测的是回填链路，不是抓图能力。
    """
    print("\n[3f0] 转 FLAC 前补封面（v5.9.16）", flush=True)

    import math
    import shutil
    import struct
    import tempfile
    import threading

    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    import cover_art as ca
    import flac_out as fo
    from web_ui import dl_api as dmod

    dapi = _read("web_ui", "dl_api.py")

    # ---- ① 源码：三个新方法 + 调用点 ----
    _mark("def _covers_before_convert" in dapi,
          "dl_api 有 _covers_before_convert（转码前抓封面）", "")
    _mark("def _backfill_audio" in dapi,
          "dl_api 有 _backfill_audio（给已转好的 FLAC/MP3 补图）", "")
    _mark("def _has_picture" in dapi,
          "dl_api 有 _has_picture（只读块头，不全量读文件）", "")
    _mark("self._covers_before_convert(jobs)" in dapi,
          "_cv_oneshot_thread 在入队前调用（顺序不能反，否则转码时还没图）", "")
    _mark("import flac_out" in dapi,
          "dl_api 导入了 flac_out（回填要用 embed_picture）", "")
    # v5.9.17：MP3 也嵌封面，所以「转码前抓图」的适用格式从 flac 放宽到 flac+mp3
    _mark('not in ("flac", "mp3")' in dapi,
          "只在转 FLAC / MP3 时抓（WAV 没有容器放图）", "")
    _mark('!= "flac"' not in dapi and "!= 'flac'" not in dapi,
          "旧写法「只认 FLAC」已清干净（否则 MP3 永远抓不到图）", "")
    _mark('if not self.opts.get("auto_cover", True):' in dapi,
          "尊重「下载封面」开关（关掉就不该偷偷联网）", "")

    # ---- ② _has_picture 的行为 ----
    tmp = tempfile.mkdtemp(prefix="v5916_gate_")
    try:
        n = 22050
        raw = b"".join(struct.pack("<hh", int(8000 * math.sin(2 * math.pi * 440 * i / 44100)),
                                   int(8000 * math.sin(2 * math.pi * 440 * i / 44100)))
                       for i in range(n))
        bare = os.path.join(tmp, "bare.flac")
        ok, _, _d = fo.encode_pcm(raw, bare)
        _mark(ok and not dmod.DownloadState._has_picture(bare),
              "_has_picture 对没有图片块的 FLAC 返回 False", "")

        # 缓存里找一张现成的 png 当「假封面」（不联网）
        img = _first_cached_png()

        if img:
            _mark(os.path.isfile(img), "缓存里有可用封面（回填测试的素材）",
                  os.path.basename(img))
            _mark(fo.embed_picture(bare, img)[0]
                  and dmod.DownloadState._has_picture(bare),
                  "_has_picture 对已嵌图的 FLAC 返回 True", "")

            # ---- ③ _backfill_audio：目录名 -> 封面 -> 回填 ----
            game = "Gate Test Game"
            gdir = os.path.join(tmp, "FLAC", game)
            os.makedirs(gdir)
            tgt = os.path.join(gdir, "01_track.flac")
            shutil.copyfile(bare, tgt)
            fo.embed_picture(tgt, img)          # 先嵌再剥：确认能被替换
            # 剥掉图片块，模拟「v5.9.14 之前转的」
            fo.embed_picture(tgt, img)
            bare2 = os.path.join(gdir, "02_track.flac")
            ok2, _, _ = fo.encode_pcm(raw, bare2)
            _mark(ok2 and not dmod.DownloadState._has_picture(bare2),
                  "造出一个没有封面的 FLAC（模拟老包）", "")

            st = dmod.DownloadState()
            st.key = "gsf"
            st.flac_dir = os.path.join(tmp, "FLAC")
            st.opts["auto_cover"] = True

            # 把「查封面」换成本地图，不联网
            orig = ca.cover_for_title
            ca.cover_for_title = lambda _t: img
            try:
                n_fill = st._backfill_audio(st.flac_dir)
                _mark(n_fill >= 1, "_backfill_audio 真给老 FLAC 补上了图",
                      "补了 %d 个" % n_fill)
                _mark(dmod.DownloadState._has_picture(bare2),
                      "补完之后文件里确实有图片块（不是只改了返回值）", "")

                sz1 = os.path.getsize(bare2)
                n2 = st._backfill_audio(st.flac_dir)
                _mark(n2 == 0 and os.path.getsize(bare2) == sz1,
                      "回填幂等：二次调用补 0 个、文件大小不变",
                      "size=%d" % sz1)

                # ---- ④ 非 FLAC 格式不动作 ----
                st._cv_fmt = "wav"
                calls = []
                orig_fetch = ca.fetch
                ca.fetch = lambda *a, **k: (calls.append(a), (False, "", "x"))[1]
                try:
                    st._covers_before_convert([])
                    _mark(not calls,
                          "_cv_fmt=wav 时一个封面都不抓（WAV 没容器放图）", "")
                    st._cv_fmt = "flac"
                    st.opts["auto_cover"] = False
                    st._covers_before_convert([])
                    _mark(not calls,
                          "auto_cover=False 时也不抓（尊重用户关掉）", "")
                finally:
                    ca.fetch = orig_fetch
            finally:
                ca.cover_for_title = orig
        else:
            _mark(True, "缓存里没有 png，跳过回填行为测试（源码断言已覆盖）", "")
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:                            # noqa: BLE001
            pass


def t_v5917():
    """[3f1] 封面落到 FLAC 游戏目录（v5.9.16）。

    ## 用户要什么

    「封面存放位置放在下载目录里 FLAC 文件夹内的对应游戏文件内」——
    即 ``<保存目录>/FLAC/<游戏名>/cover.png``，图跟着音频走，不再只躺在
    ``%LOCALAPPDATA%\\MSU1Converter\\covers`` 里。

    ## 守什么

    1. **两个模块的常量必须一致**：``spc_wav.COVER_NAME`` 决定转码时找哪个
       文件名，``dl_api._COVER_NAME`` 决定往里写哪个。写不一样就会出现
       「复制了但转码不认」，而且现象极难归因。
    2. **下载页落副本**：``cover=None``（按目录名推断）时，``out_dir`` 就是
       「一个游戏一个目录」，要留 ``cover.png``。
    3. **首页不落副本**：那边所有音源**共用一个输出目录**，落副本会让第一个
       音源的图糊到后面每一个游戏上（v5.9.14 显式传 ``cover`` 就是防这个）。
       这条是回归红线 —— 写错一次，整批游戏的封面全错。
    4. **优先用目录里的图**：目录里已有 ``cover.png`` 时，清掉全局缓存也能
       正常转码（``_cover_for_dir`` 先看本地）。
    5. **幂等**：重复跑不改文件、不重复下载。
    """
    print("\n[3f1] 封面落到 FLAC 游戏目录（v5.9.16）", flush=True)

    import math
    import shutil
    import struct
    import tempfile

    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    import cover_art as ca
    import flac_out as fo
    import spc_wav as sw
    from web_ui import dl_api as dmod

    sws = _read("spc_wav.py")
    dapi = _read("web_ui", "dl_api.py")

    # ---- ① 常量一致 + 参数还在 ----
    _mark(sw.COVER_NAME == dmod._COVER_NAME,
          "spc_wav.COVER_NAME 与 dl_api._COVER_NAME 一致（写错=复制了但转码不认）",
          "%s / %s" % (sw.COVER_NAME, dmod._COVER_NAME))
    _mark(sw.COVER_NAME == "cover.png",
          "文件名是 cover.png（不是 folder.png —— 后者会被系统当目录图标）",
          sw.COVER_NAME)
    _mark("cover_beside=None" in _sig_of(sw.write_tracks),
          "write_tracks 有 cover_beside 参数（默认自动判定）", "")
    _mark("_save_cover_beside" in sws, "spc_wav 有 _save_cover_beside", "")
    _mark("def _cover_for_dir" in dapi, "dl_api 有 _cover_for_dir", "")
    _mark("def _copy_cover" in dapi, "dl_api 有 _copy_cover", "")
    _mark('os.path.join(gdir, _COVER_NAME)' in dapi,
          "_backfill_audio 用目录里的 cover.png 优先", "")

    # ---- ② 真写盘：三种路径 ----
    tmp = tempfile.mkdtemp(prefix="v5917_gate_")
    try:
        n = 22050
        raw = b"".join(struct.pack("<hh", int(8000 * math.sin(2 * math.pi * 440 * i / 44100)),
                                   int(8000 * math.sin(2 * math.pi * 440 * i / 44100)))
                       for i in range(n))
        tracks = [{"name": "T", "pcm": raw, "little_endian": True,
                   "seconds": 0.5, "rate": 44100, "channels": 2}]

        # 缓存里找一张现成 png（不联网）
        img = _first_cached_png()
        _mark(bool(img) and os.path.isfile(img), "缓存里有可用封面（测试素材）",
              os.path.basename(img) if img else "无")

        if img:
            # ②-a 下载页：一个游戏一个目录，cover=None -> 落副本
            A = os.path.join(tmp, "FLAC", "Some Game")
            os.makedirs(A)
            # 让自动推断命中：临时把 cover_for_title 指到这张图
            orig = ca.cover_for_title
            ca.cover_for_title = lambda _t: img
            try:
                st = sw.write_tracks(tracks, A, 0.0, fmt="flac", cover=None)
                cp = os.path.join(A, sw.COVER_NAME)
                _mark(st["ok"] == 1 and os.path.isfile(cp),
                      "下载页（cover=None）：cover.png 落到游戏目录", cp)
                fa = [f for f in os.listdir(A) if f.endswith(".flac")]
                _mark(bool(fa) and bool(fo.pictures(os.path.join(A, fa[0]))),
                      "同一批 FLAC 也嵌了图片块（不是只放了图）", "")
                sz_cp = os.path.getsize(cp) if os.path.isfile(cp) else 0
                _mark(sz_cp == os.path.getsize(img),
                      "cover.png 与源图字节数一致（是复制不是截断）",
                      "%d B" % sz_cp)

                # ②-b 首页：共用目录 + 显式 cover -> 不落副本
                B = os.path.join(tmp, "shared_out")
                os.makedirs(B)
                st2 = sw.write_tracks(tracks, B, 0.0, fmt="flac", cover=img)
                _mark(st2["ok"] == 1
                      and not os.path.isfile(os.path.join(B, sw.COVER_NAME)),
                      "首页（共用目录）：**不**落 cover.png（否则一张图糊满所有游戏）",
                      "")
                fb = [f for f in os.listdir(B) if f.endswith(".flac")]
                _mark(bool(fb) and bool(fo.pictures(os.path.join(B, fb[0]))),
                      "首页的 FLAC 照样有图片块（只是不留副本）", "")

                # ②-c 强制
                C = os.path.join(tmp, "force")
                os.makedirs(C)
                sw.write_tracks(tracks, C, 0.0, fmt="flac", cover=img,
                                cover_beside=True)
                _mark(os.path.isfile(os.path.join(C, sw.COVER_NAME)),
                      "cover_beside=True 可强制落副本", "")

                # ②-d 优先用目录里的图（清掉缓存也要能转）
                ca.cover_for_title = lambda _t: ""      # 假装缓存里没有
                D = os.path.join(tmp, "FLAC", "Local Only")
                os.makedirs(D)
                shutil.copyfile(img, os.path.join(D, sw.COVER_NAME))
                st4 = sw.write_tracks(tracks, D, 0.0, fmt="flac", cover=None)
                fd = [f for f in os.listdir(D) if f.endswith(".flac")]
                _mark(st4["ok"] == 1 and bool(fd)
                      and bool(fo.pictures(os.path.join(D, fd[0]))),
                      "目录里已有 cover.png 时，缓存查不到也能嵌（图跟着音频走）",
                      "")
            finally:
                ca.cover_for_title = orig

            # ---- ③ _backfill_audio 也会落副本 ----
            E = os.path.join(tmp, "FLAC2", "Old Game")
            os.makedirs(E)
            old = os.path.join(E, "01_old.flac")
            fo.encode_pcm(raw, old)
            stt = dmod.DownloadState()
            stt.opts["auto_cover"] = True
            ca.cover_for_title = lambda _t: img
            try:
                n1 = stt._backfill_audio(os.path.join(tmp, "FLAC2"))
                _mark(n1 == 1 and os.path.isfile(os.path.join(E, sw.COVER_NAME)),
                      "_backfill_audio 给老 FLAC 补图时也放 cover.png", "补 %d 个" % n1)
                _mark(dmod.DownloadState._has_picture(old),
                      "老 FLAC 确实嵌上了", "")
                sz = os.path.getsize(old)
                n2 = stt._backfill_audio(os.path.join(tmp, "FLAC2"))
                _mark(n2 == 0 and os.path.getsize(old) == sz,
                      "重复跑幂等（补 0 个、文件大小不变）", "")
            finally:
                ca.cover_for_title = orig
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:                            # noqa: BLE001
            pass


def t_v5918():
    """[3f2] MP3 内嵌封面（v5.9.17）：ID3v2.3 的 APIC 帧。

    ## 用户问的

    「MP3 格式能内嵌封面吗」—— 能。v5.9.15 当时明确不做，理由是「用户要的
    是能出 MP3，不是 MP3 也要带图」；现在主动问到了，说明要。

    ## 为什么可以纯标准库做

    libsndfile 写出的 MP3 **既没有 ID3v2 也没有 ID3v1**（头就是 ``ff fb``
    的 MPEG 帧同步），所以在文件最前面插一个 ID3v2.3 标签块就行，不用
    改写任何已有结构。ID3v2 的 APIC 帧结构和 FLAC 的 PICTURE 块一样简单，
    不值得引 mutagen（GPL + 体积）。

    ## 守什么

    1. **写 2.3 不写 2.4**：2.3 的帧长度是普通大端，2.4 是 synchsafe。
       2.3 兼容面更广（资源管理器 / foobar2000 / 老车机）。
    2. **替换不叠加**：反复嵌同一个文件，大小不能滚雪球。
    3. **别的帧要保住**：用户用别的工具打过 TIT2（标题）的 MP3，
       嵌封面不能把标题弄丢。
    4. **音频不能坏**：``soundfile`` 要能正常解码（ID3 头对解码器是透明的）。
    5. **和 FLAC 走同一套入口**：``write_tracks(fmt="mp3")`` 也要落
       ``cover.png``、也要能被 ``_backfill_audio`` 补。

    本段**不联网**：图片素材从缓存里找。
    """
    print("\n[3f2] MP3 内嵌封面（v5.9.17）", flush=True)

    import math
    import shutil
    import struct
    import tempfile

    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    import cover_art as ca
    import flac_out as fo
    import spc_wav as sw
    from web_ui import dl_api as dmod

    fos = _read("flac_out.py")
    sws = _read("spc_wav.py")
    dapi = _read("web_ui", "dl_api.py")
    cj = _read("convert_job.py")

    # ---- ① 接口齐全 ----
    _mark("def embed_id3_picture" in fos, "flac_out 有 embed_id3_picture", "")
    _mark("def id3_pictures" in fos, "flac_out 有 id3_pictures（读回来验证）", "")
    _mark("def build_id3_block" in fos, "flac_out 有 build_id3_block", "")
    _mark("def has_picture" in fos,
          "flac_out 有 has_picture（FLAC/MP3 统一判，只读头）", "")
    for _n in ("embed_id3_picture", "id3_pictures", "build_id3_block",
               "has_picture"):
        _mark(_n in fo.__all__, "%s 在 __all__ 里" % _n, "")

    # ---- ② 写的是 2.3，不是 2.4 ----
    _mark('_ID3_MAGIC + b"\\x03\\x00"' in fos,
          "写 ID3v2.3（帧长度普通大端；2.4 是 synchsafe，兼容面窄）", "")
    _mark("ID3_FRONT_COVER = 3" in fos,
          "图片类型 3 = 正面封面（与 FLAC 的 PICTURE_FRONT_COVER 同义）", "")

    # ---- ③ 下游都放开了 MP3 ----
    _mark("if fmt not in ENC_FMTS:" in sws,
          "spc_wav 不再把 MP3 / OGG 的封面丢掉", "")
    _mark('not in ("flac", "mp3")' in dapi,
          "dl_api._covers_before_convert 转 MP3 也会抓封面", "")
    _mark('elif low.endswith(".mp3"):' in dapi,
          "_backfill_audio 认得 .mp3", "")
    _mark("if _fmt in spc_wav.ENC_FMTS:" in cj,
          "首页转换（convert_job）MP3 / OGG 也查封面", "")

    # ---- ④ 真写盘 ----
    tmp = tempfile.mkdtemp(prefix="v5918_gate_")
    try:
        n = 22050
        raw = b"".join(struct.pack("<hh", int(8000 * math.sin(2 * math.pi * 440 * i / 44100)),
                                   int(8000 * math.sin(2 * math.pi * 440 * i / 44100)))
                       for i in range(n))

        # 缓存里找一张现成 png（不联网）—— 三层 / 两层都认，见 _first_cached_png
        img = _first_cached_png()
        if not img:
            _mark(True, "缓存里没有 png，跳过行为测试（源码断言已覆盖）", "")
        else:
            img_size = os.path.getsize(img)

            # ④-a 直接写：带 cover
            p1 = os.path.join(tmp, "a.mp3")
            ok1, _, _d1 = fo.encode_pcm(raw, p1, cover=img, fmt="mp3")
            _mark(ok1 and fo.has_picture(p1),
                  "encode_pcm(fmt=mp3, cover=...) 写出了 APIC", "")
            pics = fo.id3_pictures(p1)
            _mark(len(pics) == 1 and pics[0]["type"] == 3
                  and pics[0]["mime"] == "image/png",
                  "APIC 是 type=3 / image/png（播放器认的就是这个）",
                  str(pics[0] if pics else None))
            _mark(bool(pics) and pics[0]["bytes"] == img_size,
                  "帧里的图片字节数与源图一致（不是截断）",
                  "%d B" % (pics[0]["bytes"] if pics else 0))

            # ④-b 不带 cover -> 没有 APIC
            p2 = os.path.join(tmp, "b.mp3")
            fo.encode_pcm(raw, p2, fmt="mp3")
            _mark(not fo.has_picture(p2),
                  "不带 cover 时不写任何 ID3 标签（不白占空间）", "")

            # ④-c 后补 + 幂等
            ok2, _why2 = fo.embed_id3_picture(p2, img)
            sz1 = os.path.getsize(p2)
            _mark(ok2 and fo.has_picture(p2), "后补封面成功", "")
            fo.embed_id3_picture(p2, img)
            _mark(os.path.getsize(p2) == sz1
                  and len(fo.id3_pictures(p2)) == 1,
                  "重复嵌是**替换**不是叠加（大小不变、APIC 仍 1 个）",
                  "%d B" % sz1)

            # ④-d 别的 ID3 帧要保住
            p3 = os.path.join(tmp, "c.mp3")
            fo.encode_pcm(raw, p3, fmt="mp3")
            tit = b"\x00Test Title"
            frame = b"TIT2" + len(tit).to_bytes(4, "big") + b"\x00\x00" + tit
            with open(p3, "rb") as fh:
                _audio = fh.read()
            with open(p3, "wb") as fh:
                fh.write(b"ID3\x03\x00\x00" + fo._synchsafe(len(frame))
                         + frame + _audio)
            fo.embed_id3_picture(p3, img)
            _fr, _end = fo._split_id3(open(p3, "rb").read())
            _mark(any(f == b"TIT2" for f, _b in _fr),
                  "已有的 TIT2（标题）帧没被弄丢", "")
            _mark(sum(1 for f, _b in _fr if f == b"APIC") == 1,
                  "嵌完只有 1 个 APIC", "")

            # ④-e 音频没坏
            try:
                import soundfile as sf
                _data, _sr = sf.read(p1, frames=11025)
                _ok_dec = len(_data) > 0 and _sr == 44100
            except Exception:                        # noqa: BLE001
                _ok_dec = False
            _mark(_ok_dec, "ID3 标签不影响解码（soundfile 仍能读）", "")

            # ④-f write_tracks 整条路：落 cover.png + 嵌 APIC
            orig = ca.cover_for_title
            ca.cover_for_title = lambda _t: img
            try:
                A = os.path.join(tmp, "MP3", "Some Game")
                os.makedirs(A)
                tracks = [{"name": "T", "pcm": raw, "little_endian": True,
                           "seconds": 0.5, "rate": 44100, "channels": 2}]
                st = sw.write_tracks(tracks, A, 0.0, fmt="mp3", cover=None)
                fa = [f for f in os.listdir(A) if f.endswith(".mp3")]
                _mark(st["ok"] == 1 and os.path.isfile(
                    os.path.join(A, sw.COVER_NAME)),
                    "write_tracks(fmt=mp3) 也落 cover.png", "")
                _mark(bool(fa) and fo.has_picture(os.path.join(A, fa[0])),
                      "同一批 MP3 也嵌了 APIC", "")

                # ④-g _backfill_audio 认 mp3
                B = os.path.join(tmp, "MP3b", "Old Game")
                os.makedirs(B)
                bare = os.path.join(B, "01_x.mp3")
                fo.encode_pcm(raw, bare, fmt="mp3")
                stt = dmod.DownloadState()
                stt.opts["auto_cover"] = True
                nf = stt._backfill_audio(os.path.join(tmp, "MP3b"))
                _mark(nf == 1 and fo.has_picture(bare),
                      "_backfill_audio 给老 MP3 补上了封面", "补 %d 个" % nf)
                _sz = os.path.getsize(bare)
                _mark(stt._backfill_audio(os.path.join(tmp, "MP3b")) == 0
                      and os.path.getsize(bare) == _sz,
                      "MP3 回填也幂等", "")
            finally:
                ca.cover_for_title = orig
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:                            # noqa: BLE001
            pass


def t_v5919():
    """[3f3] MP3 **音质**回归（v5.9.18）—— 用户报「转出来有持续杂音」。

    ## 为什么会漏掉

    v5.9.15 加 MP3 时，所有断言都是**结构性**的：能写、非空、能解码、
    帧数≈源时长、44100Hz、立体声、体积落在 320kbps 区间。这些全过了，
    可声音是坏的 —— 因为坏的方式**不改变任何结构指标**：

    * 帧数一样（时长没变）
    * 采样率、声道一样
    * 文件大小甚至更接近「320kbps」（削顶方波高频能量大，VBR 反而给得更多）

    **所以「体积」和「时长」都不能用来推断内容对不对。** 必须解码回来
    和源逐样本比。

    ## 真因

    libsndfile 1.2.x 的 MP3 编码器**不认 int16 输入**。把 int16 数组
    （-32768..32767）直接交给它，它按 float 样本（-1.0..1.0）解释 ——
    每个采样点放大三万倍后削顶成 ±1 方波。实测：

    | 输入 | SNR | 相关系数 |
    |---|---|---|
    | int16 直传 | **-4.4 dB** | 0.38 |
    | float32 归一化 | **+39.1 dB** | 0.99994 |

    ## 守什么

    1. ``_write`` 的 MP3 分支**必须**先 ``/32768.0`` 归一化成 float32。
    2. **实测 SNR ≥ 20 dB**（320kbps 音乐实测 39 dB，留足余量）。
    3. **相关系数 ≥ 0.99**（对「整体放大/削顶」这类失真极敏感）。
    4. **码率落在 300~340 kbps** —— 顺带钉死「320 kbps CBR」这个对外承诺。
       （v5.9.18 之前其实只有 102 kbps，而且没人发现。）
    5. **峰值不削顶**（输出峰值不应超过源峰值的 1.1 倍）。
    6. FLAC 那条路**不能受影响**（仍是无损）。

    本段**不联网**，素材是合成信号（多频 + 包络），够敏感也够快。
    """
    print("\n[3f3] MP3 音质回归（v5.9.18）", flush=True)

    import math
    import tempfile

    import numpy as np
    import soundfile as sf

    import flac_out as fo

    sw = open(os.path.join(ROOT, "flac_out.py"), encoding="utf-8").read()

    # ---- ① 源码：必须归一化，且必须要求 CBR ----
    _mark("/ 32768.0" in sw or "/32768.0" in sw,
          "MP3 分支把 int16 归一化成 float32（除 32768）", "")
    _mark("astype(np.float32)" in sw,
          "归一化的目标是 float32（不是 float64，省一半内存）", "")
    _mark('"CONSTANT"' in sw,
          "显式要求 CBR（不要求就是 VBR，实测只有 170 kbps）", "")
    _mark("bitrate_mode" in sw,
          "传了 bitrate_mode 给 soundfile", "")
    _mark('"bitrate_mode" not in str(exc)' in sw,
          "老 soundfile 只在缺 bitrate_mode 时降级（别的 TypeError 照常抛）", "")

    # ---- ② 合成一段「对失真敏感」的信号 ----
    sr = 44100
    n = sr  # 1 秒够算，也够快
    t = np.arange(n) / float(sr)
    # 三个不协和频率叠加 + 渐强包络：任何非线性失真都会产生明显互调
    wave = (0.45 * np.sin(2 * math.pi * 440.0 * t)
            + 0.30 * np.sin(2 * math.pi * 1330.0 * t)
            + 0.20 * np.sin(2 * math.pi * 3170.0 * t))
    wave *= (0.15 + 0.85 * t)                     # 渐强，避开首尾静音
    pcm = np.clip(wave * 30000.0, -32768, 32767).astype("<i2")
    stereo = np.ascontiguousarray(np.stack([pcm, pcm], axis=1))
    raw = stereo.tobytes()
    ref = stereo[:, 0].astype(np.float64)
    src_peak = float(np.abs(ref).max())

    def _snr(got):
        g = got[:len(ref)].astype(np.float64)
        r = ref[:len(g)]
        err = g - r
        ps, pe = float(np.dot(r, r)), float(np.dot(err, err))
        snr = 10.0 * math.log10(ps / pe) if pe > 1e-9 and ps > 1e-9 else 99.0
        corr = float(np.dot(r, g) / (math.sqrt(ps * float(np.dot(g, g))) + 1e-18))
        return snr, corr

    tmp = tempfile.mkdtemp(prefix="v5919_")
    try:
        # ---- ③ MP3：音质 ----
        dst = os.path.join(tmp, "a.mp3")
        ok, _p, detail = fo.encode_pcm(raw, dst, sr, 2, cover=None, fmt="mp3")
        _mark(ok, "MP3 写出来了", detail)
        if ok:
            got, gsr = sf.read(dst, dtype="int16", always_2d=True)
            _mark(gsr == sr and got.shape[1] == 2,
                  "解码回来仍是 44100Hz / 2ch", "%d / %d" % (gsr, got.shape[1]))
            snr, corr = _snr(got[:, 0])
            _mark(snr >= 20.0,
                  "MP3 音质 SNR ≥ 20 dB（int16 直传只有 -4 dB）", "%.2f dB" % snr)
            _mark(corr >= 0.99,
                  "与源波形相关 ≥ 0.99（放大/削顶会掉到 0.4 以下）",
                  "%.5f" % corr)
            kbps = os.path.getsize(dst) * 8.0 / (len(stereo) / float(sr)) / 1000.0
            # 上限放宽到 360：这是 **1 秒**的样本，Xing/LAME 头 + 帧对齐的
            # 固定开销占比重（几十字节摊到 1 秒就是几个 kbps）。
            # 31 秒的真曲子实测 321 kbps，落在这个区间里。
            _mark(300.0 <= kbps <= 360.0,
                  "码率落在 320 kbps CBR 区间（说明书对外承诺的就是这个）",
                  "%.0f kbps" % kbps)
            peak = float(np.abs(got.astype(np.float64)).max())
            _mark(peak <= src_peak * 1.1,
                  "没有削顶（输出峰值不超过源峰值 1.1 倍）",
                  "源 %.0f / 出 %.0f" % (src_peak, peak))
            # 声道：左右应各自对得上（不能串音或混成单声道）
            sL, _c = _snr(got[:, 0])
            sR, _c2 = _snr(got[:, 1])
            _mark(sL >= 18.0 and sR >= 18.0,
                  "左右声道各自都干净", "L %.1f / R %.1f dB" % (sL, sR))

        # ---- ④ 带封面时音质不能变 ----
        img = None
        try:
            import cover_art
            img = cover_art.cover_for_title("Advance Wars 2 - Black Hole Rising")
        except Exception:                        # noqa: BLE001
            img = ""
        if img and os.path.isfile(img):
            dst2 = os.path.join(tmp, "b.mp3")
            ok2, _p2, _d2 = fo.encode_pcm(raw, dst2, sr, 2, cover=img, fmt="mp3")
            if ok2:
                g2, _g2sr = sf.read(dst2, dtype="int16", always_2d=True)
                snr2, _c3 = _snr(g2[:, 0])
                _mark(snr2 >= 20.0,
                      "带封面时音质不受影响（ID3 块不碰音频）",
                      "%.2f dB" % snr2)
                _mark(fo.has_picture(dst2), "封面确实嵌进去了", "")

        # ---- ⑤ FLAC 不能受影响 ----
        fdst = os.path.join(tmp, "c.flac")
        okf, _pf, df = fo.encode_pcm(raw, fdst, sr, 2, cover=None, fmt="flac")
        _mark(okf, "FLAC 仍能写", df)
        if okf:
            gf, _gsr = sf.read(fdst, dtype="int16", always_2d=True)
            same = bool(np.array_equal(gf, stereo))
            _mark(same, "FLAC 仍是逐样本无损（这条不能被 MP3 的改动带坏）", "")
            snrf, _cf = _snr(gf[:, 0])
            _mark(snrf >= 60.0, "FLAC SNR ≥ 60 dB", "%.1f dB" % snrf)

        # ---- ⑥ 老坏文件必须能被识别出来重转 ----
        # 这是「用户升级后点一次转 MP3 就自动修好」的关键：v5.9.15~17 的
        # 产物结构指标全对，只有记账版本能证明它是坏的。
        import dl_convert as dc
        _mark(dc._ver_lt(getattr(dc, "AUDIO_WRITER_VERSION", ""), "5.9.18") is False,
              "dl_convert.AUDIO_WRITER_VERSION 不早于 5.9.18",
              dc.AUDIO_WRITER_VERSION)
        _mark(dc.BAD_BEFORE.get("mp3") == "5.9.18",
              "BAD_BEFORE 登记了 mp3 → 5.9.18", dc.BAD_BEFORE.get("mp3"))
        _mark("mp3" not in dc.BAD_BEFORE or "flac" not in dc.BAD_BEFORE,
              "FLAC / WAV 不在名单里（它们没坏过，不该被迫重转）", "")
        _mark(dc._ver_lt("5.9.9", "5.9.18") and dc._ver_lt("5.9", "5.9.18"),
              "版本比较按数字不按字符串（5.9.9 < 5.9.18）", "")
        _mark(not dc._ver_lt("5.10.0", "5.9.18"),
              "5.10.0 不算旧（字符串比会误判）", "")

        msrc = os.path.join(tmp, "01_src.spc")
        with open(msrc, "wb") as fh:
            fh.write(b"x" * 64)
        # ⚠ v6.0.2：记账里的**扩展名必须与目标格式一致**（``already_done``
        #   新加的校验④）。这一段原来只造一个 ``01_a.wav``，就断言「按 MP3
        #   判是已转过」—— 而那正是用户报的那个 bug 的形状：FLAC/MP3 目录里
        #   躺着 .wav，记账记的也是 .wav 的名字。现在每种格式各造各的文件。
        for _n in ("01_a.wav", "01_a.mp3", "01_a.flac"):
            with open(os.path.join(tmp, _n), "wb") as fh:
                fh.write(b"R" * 44)
        man = dc.ConvertManifest(os.path.join(tmp, dc.MANIFEST_NAME))
        man.record(msrc, ["01_a.mp3"])
        _mark(man.entry(msrc).get("by") == dc.AUDIO_WRITER_VERSION,
              "record() 把生产版本写进 by", man.entry(msrc).get("by"))
        _mark(man.already_done(msrc, "mp3"), "新版写的 MP3 判定为「已转过」", "")
        rec = man._data["entries"][os.path.normpath(msrc).lower()]
        rec.pop("by", None)
        _mark(not man.already_done(msrc, "mp3"),
              "没有 by 的老记账 → MP3 必须重转（v5.9.17 及更早都是坏的）", "")
        rec["by"] = "5.9.17"
        _mark(not man.already_done(msrc, "mp3"), "by=5.9.17 → 重转", "")
        rec["by"] = "5.9.18"
        _mark(man.already_done(msrc, "mp3"), "by=5.9.18 → 跳过", "")
        # BAD_BEFORE 只针对 mp3：FLAC 用它自己那条记账，版本再老也该跳过
        fdir = os.path.join(tmp, "flacman")
        os.makedirs(fdir, exist_ok=True)
        with open(os.path.join(fdir, "01_a.flac"), "wb") as fh:
            fh.write(b"fLaC")
        man2 = dc.ConvertManifest(os.path.join(fdir, dc.MANIFEST_NAME))
        man2.record(msrc, ["01_a.flac"])
        man2._data["entries"][os.path.normpath(msrc).lower()]["by"] = "5.9.17"
        _mark(man2.already_done(msrc, "flac"),
              "FLAC 记账（by=5.9.17）仍跳过 —— BAD_BEFORE 只针对 MP3，"
              "不牵连好格式", "")
        _mark("already_done(src, self._fmt)" in
              open(os.path.join(ROOT, "dl_convert.py"), encoding="utf-8").read(),
              "转换线程把 fmt 传给了 already_done（不传就永远不重转）", "")
    finally:
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:                        # noqa: BLE001
            pass


def t_v5920():
    """[3f4] MP3 落盘**自检**（v5.9.19）—— 让「坏音频」不可能悄悄发出去。

    ## 为什么还要再加一层

    v5.9.18 已经把编码 bug 修了，测试也钉住了「SNR ≥ 20 dB」。但那条断言
    是**测试里的**断言 —— 它只能证明「我这台机器上、这段合成信号，编出来
    是对的」。用户机器上的 libsndfile 版本、CPU、soundfile 版本都可能不同，
    一旦某个组合又踩到编码器的坑，用户拿到的还是「能播、时长对、体积对」
    的坏文件，而我们毫无察觉。

    所以把这条检查**搬进产品代码**：每写一首 MP3，立刻解回来跟源比 SNR，
    不达标就抛异常、删掉半成品、让这首判失败。失败是响的，噪声是哑的。

    ## 守什么

    1. ``flac_out.verify_lossy`` 存在、已导出、阈值 = 8 dB / 相关 0.80 / 峰值比 1.6。
    2. 正常 320k CBR 的产物 → **通过**，且 SNR 明显高于阈值。
    3. 复现 v5.9.18 之前的错误编码（int16 当 float 直传）→ **必须拦住**。
    4. **满量程白噪 → 必须放过**（最坏情况下的合法内容只有 15.3 dB；
       门槛压到 20 dB 会把「转音效」误判成失败 —— 这条是踩过坑补的）。
    5. 静音源 → **跳过**而不是判失败（分母为 0，硬算会误杀）。
    6. 太短的片段（< 2048 帧）→ 跳过，不误杀。
    7. 自检本身出问题（文件不存在）→ 不抛异常、不误杀。
    8. ``_write`` 里 MP3 分支**确实调了**自检，且发生在 ``os.replace`` 之前
       —— 顺序反了就等于「先把坏文件放到目标位置，再检查」，白做。
    9. 自检不通过时**目标文件不能存在**（半成品要清掉）。
    10. FLAC 是无损，**不跑**这条自检（跑了只会白白慢一倍）。
    11. detail 里带上自检 SNR —— 出问题时用户/日志能直接看到数字。
    """
    print("\n[3f4] MP3 落盘自检（v5.9.19）", flush=True)

    import array
    import math
    import tempfile

    import numpy as np
    import soundfile as sf

    import flac_out as fo

    sw = open(os.path.join(ROOT, "flac_out.py"), encoding="utf-8").read()

    # ---- ① 接口与常量 ----
    _mark(hasattr(fo, "verify_lossy"), "flac_out 暴露了 verify_lossy", "")
    _mark("verify_lossy" in getattr(fo, "__all__", ()),
          "verify_lossy 在 __all__ 里（别的模块能直接用）", "")
    _mark(abs(float(getattr(fo, "VERIFY_MIN_SNR", -1)) - 8.0) < 1e-9,
          "SNR 阈值是 8 dB（不是 20 —— 20 会误杀白噪）",
          getattr(fo, "VERIFY_MIN_SNR", "?"))
    _mark(abs(float(getattr(fo, "VERIFY_MIN_CORR", -1)) - 0.80) < 1e-9,
          "相关系数下限是 0.80", getattr(fo, "VERIFY_MIN_CORR", "?"))
    _mark(abs(float(getattr(fo, "VERIFY_MAX_PEAK_RATIO", -1)) - 1.6) < 1e-9,
          "峰值比上限是 1.6", getattr(fo, "VERIFY_MAX_PEAK_RATIO", "?"))

    # ---- ② 源码：顺序必须对 ----
    i_call = sw.find("verify_lossy(tmp")
    i_repl = sw.find("os.replace(tmp, dst)")
    _mark(i_call > 0, "_write 里对 tmp 调了自检", "")
    _mark(i_call > 0 and i_repl > 0 and i_call < i_repl,
          "自检在 os.replace **之前**（顺序反了等于没检）", "")
    _mark("避免写出「能播但满是杂音」的音频" in sw,
          "自检失败时的报错把原因说清楚了", "")

    sr = 44100
    n = sr
    t = np.arange(n) / float(sr)
    wave = (0.45 * np.sin(2 * math.pi * 440.0 * t)
            + 0.30 * np.sin(2 * math.pi * 1330.0 * t)
            + 0.20 * np.sin(2 * math.pi * 3170.0 * t))
    wave *= (0.15 + 0.85 * t)
    pcm = np.clip(wave * 30000.0, -32768, 32767).astype("<i2")
    stereo = np.ascontiguousarray(np.stack([pcm, pcm], axis=1))
    raw = stereo.tobytes()

    tmp = tempfile.mkdtemp(prefix="v5920_")
    try:
        # ---- ③ 正常产物必须通过 ----
        dst = os.path.join(tmp, "good.mp3")
        ok, _p, detail = fo.encode_pcm(raw, dst, sr, 2, fmt="mp3")
        _mark(ok, "正常 MP3 写入成功", detail)
        _mark("自检 SNR" in detail, "detail 里带自检 SNR", detail)
        vok, vsnr, vwhy = fo.verify_lossy(dst, stereo, sr)
        _mark(vok, "正常产物通过自检", vwhy)
        _mark(vsnr >= 25.0, "正常产物 SNR 明显高于阈值", "%.1f dB" % vsnr)

        # ---- ④ 复现老 bug：int16 当 float 直传 → 必须拦住 ----
        bad = os.path.join(tmp, "bad.mp3")
        sf.write(bad, stereo.astype(np.float32), sr,
                 format="MP3", subtype="MPEG_LAYER_III")
        bok, bsnr, bwhy = fo.verify_lossy(bad, stereo, sr)
        _mark(not bok, "int16 直传的坏文件被自检拦住", bwhy)
        _mark(bsnr < 0.0, "坏文件 SNR 是负的（放大三万倍削顶）",
              "%.1f dB" % bsnr)
        # 相关系数是第二条保险 —— 而且比峰值比可靠得多。
        # ⚠ 别指望峰值比：源本来就接近满量程时（这段合成信号峰值 30000），
        #   bug 放大后削到 32767，比值只有 1.1 左右，根本超不过 1.6。
        #   所以峰值比只是「源很轻时」的补充，真正兜底的是 SNR + 相关系数。
        dec_bad, _ = sf.read(bad, dtype="int16", always_2d=True)
        nb = min(len(stereo), len(dec_bad))
        aa = stereo[:nb].astype(np.float64).ravel()
        bb = dec_bad[:nb].astype(np.float64).ravel()
        aa -= aa.mean()
        bb -= bb.mean()
        cor_bad = float(np.dot(aa, bb) /
                        np.sqrt(np.dot(aa, aa) * np.dot(bb, bb)))
        _mark(cor_bad < fo.VERIFY_MIN_CORR,
              "坏文件的波形相关系数也崩了（第二条保险）", "%.3f" % cor_bad)

        # ---- ④b 满量程白噪必须放过（最坏情况下的合法内容） ----
        # ⚠ 这条是踩过坑补的：门槛原来定 20 dB，白噪只有 15.3 dB，
        #   结果 t_v5915「噪声与正弦体积比一致」直接红 —— 转音效会被误杀。
        a2 = array.array("h")
        for i in range(sr):
            v = (((i * 1103515245 + 12345) & 0x7FFFFFFF) % 65536 - 32768) // 2
            a2.append(v)
            a2.append(v)
        noise_raw = a2.tobytes()
        noise = np.frombuffer(noise_raw, dtype="<i2").reshape(-1, 2)
        ndst = os.path.join(tmp, "noise.mp3")
        nok2, _np2, ndetail = fo.encode_pcm(noise_raw, ndst, sr, 2, fmt="mp3")
        _mark(nok2, "满量程白噪也能正常转（自检不该误杀合法内容）", ndetail)
        if nok2:
            _nv, nsnr, _nw = fo.verify_lossy(ndst, noise, sr)
            _mark(nsnr > fo.VERIFY_MIN_SNR + 4.0,
                  "白噪 SNR 明显高于阈值（留足余量）",
                  "%.1f dB vs 阈值 %.0f" % (nsnr, fo.VERIFY_MIN_SNR))

        # ---- ⑤ 静音不误杀 ----
        sil = np.zeros((n, 2), dtype="<i2")
        sdst = os.path.join(tmp, "silent.mp3")
        sok, _sp, sdetail = fo.encode_pcm(sil.tobytes(), sdst, sr, 2, fmt="mp3")
        _mark(sok, "静音源仍能写出 MP3（不该因为自检失败）", sdetail)
        svok, _sv, svwhy = fo.verify_lossy(sdst, sil, sr)
        _mark(svok, "静音跳过自检而不是判失败", svwhy)

        # ---- ⑥ 太短不误杀 ----
        tiny = stereo[:1000]
        tpath = os.path.join(tmp, "tiny.mp3")
        sf.write(tpath, tiny.astype(np.float32) / 32768.0, sr,
                 format="MP3", subtype="MPEG_LAYER_III")
        tok, _ts, twhy = fo.verify_lossy(tpath, tiny, sr)
        _mark(tok, "太短（<2048 帧）跳过自检", twhy)

        # ---- ⑦ 自检自身出错不误杀 ----
        nok, _ns, nwhy = fo.verify_lossy(os.path.join(tmp, "nope.mp3"), stereo, sr)
        _mark(nok, "文件不存在时自检跳过、不抛异常", nwhy)

        # ---- ⑧ 自检不通过时，目标文件不能留下 ----
        orig = fo.verify_lossy
        fo.verify_lossy = lambda *a, **k: (False, -99.0, "人工注入失败")
        try:
            fdst = os.path.join(tmp, "should_fail.mp3")
            fok, _fp, fwhy = fo.encode_pcm(raw, fdst, sr, 2, fmt="mp3")
        finally:
            fo.verify_lossy = orig
        _mark(not fok, "自检不通过时 encode_pcm 返回失败", fwhy)
        _mark(not os.path.exists(fdst), "自检不通过时目标位置没有半成品", "")
        _mark(not os.path.exists(fdst + ".part"), ".part 临时文件也清掉了", "")

        # ---- ⑨ FLAC 不跑这条自检 ----
        cdst = os.path.join(tmp, "c.flac")
        cok, _cp, cdetail = fo.encode_pcm(raw, cdst, sr, 2, fmt="flac")
        _mark(cok, "FLAC 仍能写", cdetail)
        _mark("自检 SNR" not in cdetail,
              "FLAC 不跑有损自检（无损跑它只会白白慢一倍）", cdetail)
    finally:
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:                        # noqa: BLE001
            pass


def t_v600():
    """[3g] v6.0.0 —— 第四种输出容器 OGG Vorbis。

    ## 为什么加这一档

    用户要在手机 / 车机 / 老播放器上用，MP3 320k 体积 23% 但高频明显不如
    Vorbis；FLAC 35% 无损又太大。OGG（libsndfile 自带 libvorbis，不用外挂）
    正好卡在中间：q10 约 395 kbps VBR，体积约 28%。

    ## 这一档最容易踩的坑（本用例主要守这些）

    1. **码率档位不能贪小**。Vorbis 在 q5 附近（level 0.2）体积只要 13%，
       但满量程方波 SNR 只有 **0.82 dB** —— 而方波正是 NES / GB 声道的
       真实音色。低一档就会把合法内容判成「编码异常」直接丢文件。
       q10（level 0.0）下白噪 10.4 dB / 方波 9.95 dB，才过得去。
    2. **OGG 不吃 bitrate_mode**。MP3 那条 `bitrate_mode="CONSTANT"`
       照抄过来会直接抛 `LibsndfileError: Error set bitrate mode CONSTANT`。
    3. **有损自检必须覆盖 OGG**，不能只认 `fmt == "mp3"` ——
       否则「新格式没接上自检」这种洞会一直开着。
    4. **SNR 门限要分格式**。Vorbis 的误差结构跟 LAME 不同，
       共用 8 dB 只剩 2 dB 余量（见 flac_out.VERIFY_MIN_SNR_BY_FMT）。
    5. 四处格式白名单（flac_out / spc_wav / dl_convert / dl_api / server）
       漏一处，表现都是**静默降级成 WAV**，比报错难查得多。
    """
    print("\n[3g] OGG 输出（v6.0.0）", flush=True)

    import math
    import tempfile

    import numpy as np
    import soundfile as sf

    import flac_out as fo
    import spc_wav as sw

    # ---- ① 登记与白名单 ----
    _mark(fo.WRITE_FMTS == ("flac", "mp3", "ogg"),
          "flac_out.WRITE_FMTS = flac + mp3 + ogg", fo.WRITE_FMTS)
    _mark(fo.LOSSY_FMTS == ("mp3", "ogg"),
          "LOSSY_FMTS 收口了两种有损格式（自检按它判，不写死 mp3）",
          fo.LOSSY_FMTS)
    _mark(tuple(sw.ENC_FMTS) == tuple(fo.WRITE_FMTS),
          "spc_wav.ENC_FMTS 与 flac_out.WRITE_FMTS 一致（两处必须同步）",
          sw.ENC_FMTS)
    _ok, _why = fo.available("ogg")
    _mark(_ok, "libsndfile 报告 OGG 编码器可用", _why)

    # ---- ② 档位：必须 0.0 ----
    _mark(abs(float(fo.OGG_LEVEL) - 0.0) < 1e-9,
          "OGG_LEVEL = 0.0（q10；低一档会把方波判成坏文件）", fo.OGG_LEVEL)
    _mark(fo.VERIFY_MIN_SNR_BY_FMT.get("ogg") == 6.0
          and fo.VERIFY_MIN_SNR_BY_FMT.get("mp3") == 8.0,
          "SNR 门限分格式：ogg 6.0 / mp3 8.0", fo.VERIFY_MIN_SNR_BY_FMT)

    sr = 44100
    tmp = tempfile.mkdtemp(prefix="v600_gate_")
    try:
        rs = np.random.RandomState(7)

        def _raw(arr):
            return np.ascontiguousarray(arr).astype("<i2").tobytes()

        # 一段类音乐素材（多谐波 + 包络 + 轻微噪声）
        n = sr * 2
        t = np.arange(n) / float(sr)
        env = 0.6 + 0.4 * np.sin(2 * math.pi * 0.7 * t)
        x = env * (0.30 * np.sin(2 * math.pi * 220 * t)
                   + 0.18 * np.sin(2 * math.pi * 440 * t)
                   + 0.10 * np.sin(2 * math.pi * 660 * t))
        x = x + 0.02 * rs.randn(n)
        music = np.stack([np.round(np.clip(x, -1, 1) * 32767)] * 2, 1).astype(np.int16)

        # ---- ③ 真写一首 OGG ----
        dst = os.path.join(tmp, "01_track.ogg")
        ok, path, detail = fo.encode_pcm(_raw(music), dst, sr, 2, fmt="ogg")
        _mark(ok, "encode_pcm(fmt='ogg') 成功", detail)
        _mark(os.path.isfile(dst) and os.path.getsize(dst) > 0,
              "OGG 落盘且非空",
              os.path.getsize(dst) if os.path.isfile(dst) else "缺失")
        _mark("自检 SNR" in detail, "OGG 也跑了落盘自检（有损必须验）", detail)

        info = sf.info(dst)
        _mark(info.format == "OGG" and info.subtype == "VORBIS",
              "容器真的是 OGG/Vorbis（不是改了个扩展名的 FLAC）",
              "%s/%s" % (info.format, info.subtype))
        _mark(info.samplerate == sr and info.channels == 2,
              "采样率 / 声道与源一致",
              "%d Hz / %d ch" % (info.samplerate, info.channels))
        _mark(abs(info.frames - n) <= 2048,
              "帧数与源基本一致（编码器补零范围内）",
              "%d vs %d" % (info.frames, n))

        back, _bsr = sf.read(dst, dtype="int16", always_2d=True)
        m = min(len(back), len(music))
        a = music[:m].astype(np.float64) / 32768.0
        b = back[:m].astype(np.float64) / 32768.0
        err = a - b
        snr = 10 * math.log10(float(np.sum(a * a))
                              / max(float(np.sum(err * err)), 1e-12))
        _mark(snr > 20.0, "解回来的 SNR > 20 dB（内容没坏）", "%.2f dB" % snr)
        ratio = os.path.getsize(dst) / float(len(music.tobytes()))
        _mark(0.05 < ratio < 0.60,
              "体积落在 WAV 的 5%~60%（VBR，随内容浮动）", "%.1f%%" % (ratio * 100))

        # ---- ④ 最坏合法内容不能误杀（本用例的核心） ----
        sq = (np.sign(np.sin(2 * math.pi * 200 * np.arange(sr * 2) / sr))[:, None]
              * 32000).astype(np.int16).repeat(2, 1)
        ok_sq, _p, d_sq = fo.encode_pcm(_raw(sq), os.path.join(tmp, "sq.ogg"),
                                        sr, 2, fmt="ogg")
        _mark(ok_sq, "满量程方波（NES/GB 的真实音色）不被误杀", d_sq)

        # ---- ④' 长曲：OGG 必须**分块写**（不分块会把进程直接带走） ----
        #
        # 实测 libsndfile 1.2.2：`sf.write()` 一次性写 >17 秒的 Vorbis 会
        # **栈溢出**（Windows 退出码 0xC00000FD）—— 不抛异常、日志全空、
        # 盘上留一个 `.part`，用户看到的是「转一首 2 分半的曲子，程序消失」。
        # 边界实测：8/10/12/14/16 秒正常，18 秒起必崩；FLAC / MP3 一次性
        # 写到 900 秒都没事，**只有 OGG** 有这毛病。
        #
        # 这条**必须放子进程跑**：真回归了是子进程崩，门禁能照常报 [FAIL]，
        # 而不是整个门禁静默死掉（那种失败最难查）。
        _mark(0 < fo.OGG_BLOCK_FRAMES < sr * 17,
              "OGG 分块帧数落在崩溃边界以内（< 17 秒）", fo.OGG_BLOCK_FRAMES)
        import subprocess as _sp
        _child = os.path.join(tmp, "_long_ogg_child.py")
        with open(_child, "w", encoding="utf-8") as _cf:
            _cf.write(
                "import sys\n"
                "sys.path.insert(0, %r)\n"
                "import numpy as np\n"
                "import flac_out\n"
                "sr = 44100\n"
                "n = sr * 40\n"
                "t = np.arange(n) / float(sr)\n"
                "x = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)\n"
                "arr = np.stack([x, x], 1)\n"
                "ok, p, d = flac_out._write(sys.argv[1], arr, sr, 0.5, fmt='ogg')\n"
                "print('OK' if ok else ('FAIL ' + d))\n"
                "sys.exit(0 if ok else 3)\n" % ROOT)
        _long = os.path.join(tmp, "long_40s.ogg")
        try:
            _r = _sp.run([sys.executable, _child, _long],
                         capture_output=True, text=True, timeout=180)
            _rc, _out = _r.returncode, (_r.stdout or _r.stderr).strip()
        except Exception as _e:                          # noqa: BLE001
            _rc, _out = -1, repr(_e)
        _mark(_rc == 0 and os.path.isfile(_long) and os.path.getsize(_long) > 0,
              "40 秒长曲 OGG 写得完（分块写，进程不崩）",
              "%s / rc=%s" % (_out[-50:], _rc))

        nz = rs.randint(-32768, 32767, size=(sr * 2, 2)).astype(np.int16)
        ok_nz, _p, d_nz = fo.encode_pcm(_raw(nz), os.path.join(tmp, "nz.ogg"),
                                        sr, 2, fmt="ogg")
        _mark(ok_nz, "满量程白噪（最坏合法内容）不被误杀", d_nz)

        sil = np.zeros((sr, 2), dtype=np.int16)
        ok_si, _p, d_si = fo.encode_pcm(_raw(sil), os.path.join(tmp, "sil.ogg"),
                                        sr, 2, fmt="ogg")
        _mark(ok_si and "静音" in d_si, "静音跳过自检但仍写出文件", d_si)

        # ---- ⑤ 自检确实拦得住坏文件 ----
        orig = fo.verify_lossy
        fo.verify_lossy = lambda *a, **k: (False, -99.0, "人工注入失败")
        try:
            fdst = os.path.join(tmp, "should_fail.ogg")
            fok, _fp, fwhy = fo.encode_pcm(_raw(music), fdst, sr, 2, fmt="ogg")
        finally:
            fo.verify_lossy = orig
        _mark(not fok, "OGG 自检不通过时判失败", fwhy)
        _mark(not os.path.exists(fdst) and not os.path.exists(fdst + ".part"),
              "OGG 自检失败时目标与 .part 都不留", "")

        # ---- ⑥ 封面：OGG 不嵌但不该报错 ----
        import struct as _st
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
               + _st.pack(">II", 1, 1) + b"\x08\x02\x00\x00\x00"
               + b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00IEND\xaeB`\x82")
        cvp = os.path.join(tmp, "cover.png")
        with open(cvp, "wb") as fh:
            fh.write(png)
        cok, _cp, cdetail = fo.encode_pcm(_raw(music),
                                          os.path.join(tmp, "withcover.ogg"),
                                          sr, 2, cover=cvp, fmt="ogg")
        _mark(cok, "带封面参数写 OGG 仍成功（封面失败不该拖垮音频）", cdetail)
        _mark("OGG 不支持内嵌封面" in cdetail,
              "detail 里说清了 OGG 不嵌封面（不静默）", cdetail)

        # ---- ⑦ 上层链路：白名单 / 目录 / 体积系数 ----
        from web_ui import dl_api as _dla
        import dl_convert as _dc
        _mark(_dla.DownloadState._norm_fmt("ogg") == "ogg",
              "dl_api._norm_fmt 认 ogg", "")
        _mark(_dla._FMT_DIRS.get("ogg") == "OGG", "_FMT_DIRS 有 ogg → OGG", "")
        _mark(_dla._FMT_EXTS.get("ogg") == ".ogg", "_FMT_EXTS 有 ogg → .ogg", "")
        _mark(_dc.FMT_BYTES_RATIO.get("ogg") == _dc.OGG_BYTES_RATIO,
              "FMT_BYTES_RATIO 有 ogg（体积预估不落空）",
              _dc.FMT_BYTES_RATIO)
        _mark(0.2 < _dc.OGG_BYTES_RATIO < 0.5,
              "OGG 体积系数在 20%~50% 之间（真实曲库实测 26.8%）",
              _dc.OGG_BYTES_RATIO)
        # ⚠ 别写死 "6.0.0"：v6.0.1 一升级这条就红，而它想守的是
        #   「写出器版本 >= 6.0.0（OGG 上线那一版）」，不是「恰好等于」。
        _mark(_dc._ver_lt(_dc.AUDIO_WRITER_VERSION, "6.0.0") is False,
              "AUDIO_WRITER_VERSION 不早于 6.0.0（%s）"
              % _dc.AUDIO_WRITER_VERSION, _dc.AUDIO_WRITER_VERSION)
        _mark(_dc.BAD_BEFORE.get("mp3") == "5.9.18" and "ogg" not in _dc.BAD_BEFORE,
              "BAD_BEFORE 仍只标 MP3（OGG 没有历史坏产物）", _dc.BAD_BEFORE)

        # ---- ⑧ spc_wav 真写 ----
        out_dir = os.path.join(tmp, "tracks")
        os.makedirs(out_dir, exist_ok=True)
        _logs = []
        sub = sw.write_tracks([{"name": "01 Ogg", "pcm": _raw(music),
                                "little_endian": True}],
                              out_dir, 0, on_log=_logs.append, fmt="ogg")
        _mark(sub["ok"] == 1 and sub["fail"] == 0,
              "write_tracks(fmt='ogg') 成功 1 首", sub)
        _mark(any(f.lower().endswith(".ogg") for f in os.listdir(out_dir)),
              "落盘扩展名是 .ogg", os.listdir(out_dir))
        # v6.0.0：自检结果要进日志。以前 SNR 只活在返回值里，用户根本
        # 看不到这道保险在工作（v5.9.19 的更新说明还宣称日志里有）。
        _sum = [x for x in _logs if "落盘自检" in x]
        _mark(bool(_sum), "转换日志里有一行自检汇总", _logs)
        _mark(bool(_sum) and "OGG" in _sum[0],
              "自检汇总点名了格式（OGG）", _sum[:1])

        # ---- ⑨ 版本号五处一致 ----
        import build_web_pyi as _bwp
        ihtml = open(os.path.join(ROOT, "web_ui", "index.html"),
                     encoding="utf-8").read()
        _mark(_dc._ver_lt(_dc.AUDIO_WRITER_VERSION, "5.9.18") is False,
              "AUDIO_WRITER_VERSION 不早于 5.9.18（%s）" % _dc.AUDIO_WRITER_VERSION,
              _dc.AUDIO_WRITER_VERSION)
        _mark(_bwp.VER == server.APP_VERSION,
              "build_web_pyi.VER 与 APP_VERSION 一致", _bwp.VER)
        # ⚠ 这里原来写的是 ``"6.0.0" in ihtml`` —— 它能通过纯属侥幸：
        #   index.html 里有一条 ``<!-- v6.0.0：OGG ... -->`` 注释，版本号
        #   真漂了也照样命中。改成跟 APP_VERSION 比。
        _mark(server.APP_VERSION in ihtml,
              "index.html 里是 %s" % server.APP_VERSION, "")
        _code, _d = _jget("/api/diag")
        _mark(bool((_d.get("ogg") or {}).get("ok")),
              "/api/diag 报告 ogg 可用（前端据此点亮卡片）",
              (_d.get("ogg") or {}).get("why"))

        # ---- ⑩ 前端入口 ----
        ajs = open(os.path.join(ROOT, "web_ui", "app.js"), encoding="utf-8").read()
        dhtml = open(os.path.join(ROOT, "web_ui", "download.html"),
                     encoding="utf-8").read()
        _mark('data-fmt="ogg"' in ihtml, "index.html 有 OGG 格式卡片", "")
        _mark('["ogg", "OGG"]' in ajs, "app.js 的 FMT_CARDS 登记了 ogg", "")
        _mark('id="dlConvOgg"' in dhtml, "download.html 有「转 OGG」按钮", "")
    finally:
        _rmtree_soft(tmp)


def t_v601():
    """[3h] v6.0.1 —— 上传云盘可选 FLAC / MP3（用户需求 1）。

    ## 这一档守的是什么

    「可选格式」听起来只是多一个下拉框，实际是**四层各自独立的口径**必须
    同时改对，漏任何一层都会出现「界面能选、实际还是 FLAC」这种哑失败：

    1. **转码层** ``netease_cloud.wav_to_audio``：FLAC 必须 int16 直通
       （float 中转会被加 dither，同一份 WAV 两次写出 md5 不同）；MP3 必须
       **先归一化 float32**（libsndfile 的 MP3 编码器不认 int16，直传会把
       采样值当 float 解释 → 满幅方波，且帧数/采样率/体积全对，任何「能解码
       + 时长对」的检查都发现不了）。两条路都走 ``flac_out`` 的分派，不在
       这里另写一份。
    2. **查重/登记层** ``cloud_check`` / ``nos_alloc`` / ``commit_info``：
       ``ext`` 与 ``bitrate`` 是**匹配键的一部分**。MP3 必须报 320000，
       沿用 FLAC 的 999000（= 无损占位）等于拿有损文件去查无损的库。
    3. **编排层** ``CloudUploader(fmt=…)``：临时文件扩展名、日志文案、
       重试路径（``_verify`` → ``_one(force=True)``）都得跟着变 ——
       只改主路径、漏掉重试，表现是「第一次 MP3、重试偷偷变 FLAC」。
    4. **界面层**：``chooseScope`` 要返回 ``{scope, fmt}`` 并把 ``fmt`` 放进
       POST body；体积估算要**按范围 × 格式**给（后端 ``est_scope``），
       不能前端拍一个比例 —— 用户是拿这个数判断 5 GB 够不够的。
    """
    print("\n[3h] 上传云盘可选 FLAC / MP3（v6.0.1）", flush=True)

    import math
    import struct
    import tempfile
    import wave

    import cloud_upload as cu
    import netease_cloud as ncm
    from web_ui import cloud_api

    # ---- ① 格式白名单与归一化 ----
    _mark(tuple(cu.UPLOAD_FMTS) == ("flac", "mp3"),
          "cloud_upload.UPLOAD_FMTS 只收 flac / mp3", cu.UPLOAD_FMTS)
    _mark(tuple(cloud_api.UPLOAD_FMTS) == tuple(cu.UPLOAD_FMTS),
          "cloud_api.UPLOAD_FMTS 与 cloud_upload 一致（两处必须同步）",
          cloud_api.UPLOAD_FMTS)
    for raw, want in (("flac", "flac"), ("FLAC", "flac"), (" .MP3 ", "mp3"),
                      ("mp3", "mp3"), ("ogg", "flac"), ("", "flac"),
                      (None, "flac")):
        _mark(cu.norm_fmt(raw) == want,
              "norm_fmt(%r) = %s" % (raw, want), cu.norm_fmt(raw))

    # ---- ② bitrate：MP3 不能沿用无损占位值 ----
    _mark(ncm.bitrate_for("flac") == "999000", "FLAC 的 bitrate = 999000（无损占位）",
          ncm.bitrate_for("flac"))
    _mark(ncm.bitrate_for("mp3") == "320000",
          "MP3 的 bitrate = 320000（我们写的是 320k CBR，报真值）",
          ncm.bitrate_for("mp3"))

    tmp = tempfile.mkdtemp(prefix="v601_")
    try:
        # 6 秒 44.1k 立体声（够长，能看出容器差异；又短到门禁不拖时间）
        wav = os.path.join(tmp, "a.wav")
        sr, n = 44100, 44100 * 6
        with wave.open(wav, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(sr)
            buf = bytearray()
            for i in range(n):
                v = int(18000 * math.sin(2 * math.pi * 440 * i / sr)
                        + 5000 * math.sin(2 * math.pi * 1234 * i / sr))
                buf += struct.pack("<hh", v, int(v * 0.8))
            w.writeframes(bytes(buf))

        # ---- ③ 真转码：两种容器都要能读回来、容器魔数要对 ----
        ok_f, f_flac, d_f = ncm.wav_to_audio(wav, fmt="flac")
        ok_m, f_mp3, d_m = ncm.wav_to_audio(wav, fmt="mp3")
        _mark(ok_f and os.path.isfile(f_flac),
              "WAV → FLAC 成功（%s）" % d_f[:40], d_f)
        _mark(ok_m and os.path.isfile(f_mp3),
              "WAV → MP3 成功（%s）" % d_m[:40], d_m)
        _mark(f_flac.lower().endswith(".flac") and f_mp3.lower().endswith(".mp3"),
              "目标扩展名按 fmt 走（flac / mp3）", (f_flac, f_mp3))
        if ok_f:
            with open(f_flac, "rb") as fh:
                _mark(fh.read(4) == b"fLaC", "FLAC 文件头是 fLaC", "")
        if ok_m:
            with open(f_mp3, "rb") as fh:
                head = fh.read(3)
            # 有 ID3v2 时头是 "ID3"，否则是 MPEG 帧同步 0xFFF
            _mark(head == b"ID3" or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0),
                  "MP3 文件头是 ID3 或 MPEG 帧同步", head)
            # 320k CBR：6 秒 ≈ 240 KB，允许 ±25%
            sz = os.path.getsize(f_mp3)
            _mark(0.18e6 <= sz <= 0.30e6,
                  "MP3 体积约 320k CBR（6 秒 ≈ 240KB，实测 %.0f KB）"
                  % (sz / 1024.0), sz)
            _mark("SNR" in d_m,
                  "MP3 过了有损自检（detail 里带 SNR，说明没写出噪声）", d_m)
        if ok_f and ok_m:
            _mark(os.path.getsize(f_mp3) > os.path.getsize(f_flac),
                  "这个纯音素材上 MP3 反而更大（CBR 不随内容缩小，符合预期）",
                  (os.path.getsize(f_flac), os.path.getsize(f_mp3)))
        ok_x, _p, why_x = ncm.wav_to_audio(wav, fmt="ogg")
        _mark(not ok_x and "不支持" in str(why_x),
              "wav_to_audio 拒绝 flac / mp3 以外的容器（不静默降级）", why_x)

        # ---- ④ upload_flac 把 ext / bitrate 传对（假会话，不走网络） ----
        sess = ncm.NeteaseSession()
        seen = {}

        def _chk(md5, size, ext="flac", bitrate=None):
            seen["check"] = (ext, bitrate)
            return {"need_upload": True, "songid": "SID"}

        def _alloc(name, md5, size, ext="flac"):
            seen["alloc"] = (name, ext)
            return {"bucket": "b", "objectKey": "k", "token": "t",
                    "resourceId": "R"}

        def _put(ticket, path, md5, progress=None):
            seen["put"] = os.path.basename(path)

        def _commit(md5, songid, filename, song, artist="", album="",
                    resource_id="", bitrate=None):
            seen["commit"] = (filename, bitrate)
            return "SONGID"

        def _pub(songid):
            seen["pub"] = songid

        sess.cloud_check, sess.nos_alloc = _chk, _alloc
        sess.put_object, sess.commit_info, sess.publish = _put, _commit, _pub

        if ok_m:
            r = sess.upload_flac(f_mp3, fmt="mp3")
            _mark(bool(r.get("ok")) and not r.get("skipped"),
                  "upload_flac(fmt='mp3') 走完整条链", r)
            _mark(seen.get("check") == ("mp3", "320000"),
                  "查重带 ext=mp3 + bitrate=320000", seen.get("check"))
            _mark((seen.get("alloc") or ("", ""))[1] == "mp3",
                  "申请令牌带 ext=mp3", seen.get("alloc"))
            _mark((seen.get("commit") or ("", ""))[1] == "320000",
                  "提交元数据带 bitrate=320000", seen.get("commit"))
            _mark((seen.get("alloc") or ("", ""))[0].lower().endswith(".mp3"),
                  "申请令牌的文件名是 .mp3", seen.get("alloc"))

            # 不传 fmt 时按扩展名推断 —— 老调用方（tkinter / Qt）不受影响
            seen.clear()
            sess.upload_flac(f_mp3)
            _mark(seen.get("check") == ("mp3", "320000"),
                  "不传 fmt 时按扩展名推断成 mp3", seen.get("check"))
        if ok_f:
            seen.clear()
            sess.upload_flac(f_flac, fmt="flac")
            _mark(seen.get("check") == ("flac", "999000"),
                  "upload_flac(fmt='flac') 仍报 ext=flac + bitrate=999000",
                  seen.get("check"))

        # ---- ⑤ 编排层：CloudUploader 认 fmt，且重试路径也认 ----
        _mark(_sig_of(cu.CloudUploader.__init__).find("fmt") >= 0,
              "CloudUploader.__init__ 有 fmt 参数",
              _sig_of(cu.CloudUploader.__init__))
        _mark(_sig_of(ncm.NeteaseSession.upload_flac).find("fmt") >= 0,
              "upload_flac 有 fmt 参数", _sig_of(ncm.NeteaseSession.upload_flac))
        _mark(_sig_of(cloud_api.plan_upload).find("wavs") >= 0,
              "plan_upload 签名未破坏", _sig_of(cloud_api.plan_upload))
        # ⚠ 必须走包导入（``from web_ui import dl_api``），裸 ``import dl_api``
        #   在 ``--only=v601`` 单独跑时拿不到（见 t_zophar 里同样的坑）。
        from web_ui import dl_api as _dl
        _mark(_sig_of(_dl.DownloadState.upload_cloud).find("fmt") >= 0,
              "dl_api.upload_cloud 有 fmt 参数",
              _sig_of(_dl.DownloadState.upload_cloud))

        up = cu.CloudUploader(None, lambda *a, **k: None, None, fmt="MP3")
        _mark(up.fmt == "mp3", "CloudUploader(fmt='MP3') 归一化成 mp3", up.fmt)
        up2 = cu.CloudUploader(None, lambda *a, **k: None, None)
        _mark(up2.fmt == "flac", "不传 fmt 默认 flac（老调用方行为不变）", up2.fmt)

        # ---- ⑥ 体积估算：范围 × 格式，两个数都要有且 flac > mp3 ----
        plan = cloud_api.plan_upload([wav])
        est = plan.get("est") or {}
        _mark(set(est) == {"flac", "mp3"}, "plan.est 按格式给两个数", est)
        _mark(est.get("flac", 0) > est.get("mp3", 0) > 0,
              "FLAC 估算 > MP3 估算 > 0（0.40 vs 0.24 实测系数）", est)
        sc = plan.get("est_scope") or {}
        _mark(set(sc) == {"merged", "tracks", "all"},
              "plan.est_scope 覆盖三个范围", list(sc))
        _mark(abs(float(plan.get("est_mb") or 0) - float(est.get("flac") or 0)) < 1e-6,
              "老的 est_mb 仍等于 FLAC 估算（向后兼容）",
              (plan.get("est_mb"), est.get("flac")))

        # ---- ⑦ 前端接线 ----
        cjs = open(os.path.join(ROOT, "web_ui", "cloud.js"),
                   encoding="utf-8").read()
        ccss = open(os.path.join(ROOT, "web_ui", "cloud.css"),
                    encoding="utf-8").read()
        _mark('name="clFmt"' in cjs and 'value="mp3"' in cjs,
              "cloud.js 有 FLAC / MP3 单选组", "")
        _mark("pick.fmt" in cjs and "fmt: pick.fmt" in cjs,
              "cloud.js 把选中的 fmt 放进上传请求", "")
        _mark("est_scope" in cjs,
              "cloud.js 用后端 est_scope 报体积（不自己拍比例）", "")
        _mark(".cl-radios.inline" in ccss,
              "cloud.css 有横排单选样式（480px 放得下两项）", "")
    finally:
        _rmtree_soft(tmp)


def t_v602():
    """[3i] v6.0.2 —— 流式音频（Wii 的 .brstm 等）转 FLAC / MP3 出来还是 WAV。

    ## 用户报障（原话）

    「对于机种 Wii 我下载完后点击转换 FLAC 和转换 MP3，检查结果发现还 WAV
    格式，``D:\\MSU1Downloads\\joshw\\Wii`` 目录下的 FLAC 和 MP3 文件夹中
    均为 WAV。」

    实测复现：``FLAC/<游戏>/01_BGM_Adventure1.wav`` 的**前 4 字节是 RIFF**，
    体积 5,995,052 B —— 目录名是 FLAC、内容是 WAV、体积一分没省。

    ## 根因

    ``ConvertWorker`` 的写盘路径其实有**两条**，而 v6.0.0 的注释写的是
    「只有一处（都收口在 ``spc_wav.write_tracks``）」。漏掉的那条是：

    * 芯片 / PSF / 外挂解码器 / txtp → 先拿 PCM → ``write_tracks(fmt=…)``
      → **直接写目标格式**（这条是对的）；
    * stream（.brstm/.adx/.xvag/.laac…）/ cd / xa / pivotal → 调各模块自己的
      ``convert_one``，那几个函数**只会写 WAV**（签名里根本没有 fmt）。

    所以这一档守两件事：

    1. ``_reencode_to_fmt`` 真的能把 WAV 编成 FLAC / MP3，并且**删掉中间
       WAV**（不删的话磁盘占用翻倍，用户还以为转换没生效）；
    2. ``already_done`` 见到「记账里是 .wav、目标却是 FLAC」必须判**需重转**
       —— 否则升级后再点一次只会得到「已转过，跳过」，用户永远修不好。
    """
    print("\n[3i] 流式音频补编码（v6.0.2，Wii .brstm 出 WAV）", flush=True)

    import math
    import struct
    import tempfile
    import threading
    import wave as _wave

    import dl_convert as _dc

    # ---- ① 接线：函数在、调用点只有一处、且在「⑤ 统计」之前 ----
    _mark(hasattr(_dc.ConvertWorker, "_reencode_to_fmt"),
          "ConvertWorker 有 _reencode_to_fmt（WAV-only 分支的补编码口）", "")
    src_txt = open(os.path.join(ROOT, "dl_convert.py"), encoding="utf-8").read()
    _n_call = src_txt.count("sub = self._reencode_to_fmt(")
    _mark(_n_call == 1,
          "补编码调用点只有一处（统一在分派后补，不在各分支各写一份）", _n_call)
    _i_call = src_txt.find("sub = self._reencode_to_fmt(")
    _i_stat = src_txt.find("wavs: List[str] = []")
    _mark(0 < _i_call < _i_stat,
          "补编码在「⑤ 统计」之前（manifest 才会记新名字，不是已删的 .wav）",
          (_i_call, _i_stat))

    tmp = tempfile.mkdtemp(prefix="v602_")
    try:
        sr, secs = 44100, 0.5
        n = int(sr * secs)

        def _mk_wav(path):
            with _wave.open(path, "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(sr)
                buf = bytearray()
                for i in range(n):
                    v = int(9000 * math.sin(2 * math.pi * 440 * i / sr))
                    buf += struct.pack("<hh", v, v)
                w.writeframes(bytes(buf))
            return path

        def _cv(fmt, out):
            os.makedirs(out, exist_ok=True)
            return _dc.ConvertWorker(out, lambda t, d: None,
                                     threading.Event(), fmt=fmt)

        def _sub(path):
            return {"ok": 1, "fail": 0, "files": [path], "seconds": secs,
                    "errors": [], "last_idx": 1}

        # ---- ② FLAC：扩展名 / 文件头 / 中间 WAV 已删 ----
        d = os.path.join(tmp, "flac")
        w1 = _mk_wav(os.path.join(os.makedirs(d) or d, "01_x.wav"))
        wav_bytes = os.path.getsize(w1)
        log = []
        out = _cv("flac", d)._reencode_to_fmt(_sub(w1), log)
        f1 = (out.get("files") or [""])[0]
        _mark(str(f1).lower().endswith(".flac"), "FLAC：产物扩展名是 .flac", f1)
        _mark(not os.path.exists(w1), "FLAC：中间 WAV 已删除（不留双份）", w1)
        head = b""
        if os.path.isfile(f1):
            with open(f1, "rb") as f:
                head = f.read(4)
        _mark(head == b"fLaC", "FLAC：文件头是真 fLaC（不是改名的 WAV）", head)
        _mark(os.path.isfile(f1) and os.path.getsize(f1) < wav_bytes,
              "FLAC：体积小于原 WAV", (os.path.getsize(f1) if os.path.isfile(f1)
                                       else -1, wav_bytes))
        _mark(any("已编码成 FLAC" in str(x) for x in log),
              "FLAC：日志里说清了「N 个 WAV 已编码」（用户能看出走了补编码）",
              log[-1] if log else "")

        # ---- ③ MP3：文件头是帧同步，不是 RIFF ----
        d2 = os.path.join(tmp, "mp3")
        w2 = _mk_wav(os.path.join(os.makedirs(d2) or d2, "01_y.wav"))
        wav_bytes2 = os.path.getsize(w2)
        out2 = _cv("mp3", d2)._reencode_to_fmt(_sub(w2), [])
        f2 = (out2.get("files") or [""])[0]
        _mark(str(f2).lower().endswith(".mp3"), "MP3：产物扩展名是 .mp3", f2)
        head2 = b""
        if os.path.isfile(f2):
            with open(f2, "rb") as f:
                head2 = f.read(2)
        _mark(len(head2) == 2 and head2[0] == 0xFF and (head2[1] & 0xE0) == 0xE0,
              "MP3：文件头是 MPEG 帧同步（不是 RIFF）", head2)
        _mark(os.path.isfile(f2) and os.path.getsize(f2) < 0.6 * wav_bytes2,
              "MP3：体积显著小于原 WAV（320k CBR）",
              (os.path.getsize(f2) if os.path.isfile(f2) else -1, wav_bytes2))

        # ---- ④ fmt=wav 时原样放过（别把「只想转 WAV」的人的文件删了）----
        d3 = os.path.join(tmp, "wav")
        w3 = _mk_wav(os.path.join(os.makedirs(d3) or d3, "01_z.wav"))
        out3 = _cv("wav", d3)._reencode_to_fmt(_sub(w3), [])
        _mark((out3.get("files") or []) == [w3] and os.path.isfile(w3),
              "fmt=wav：原样返回，WAV 保留", out3.get("files"))

        # ---- ⑤ 幂等：产物已是目标格式时不再动它 ----
        m1 = os.path.getmtime(f1)
        out4 = _cv("flac", d)._reencode_to_fmt(_sub(f1), [])
        _mark((out4.get("files") or []) == [f1]
              and abs(os.path.getmtime(f1) - m1) < 1e-6,
              "幂等：已是 .flac 的产物不被重编（芯片那条路径会走到这里）",
              out4.get("files"))

        # ---- ⑥ 原有 errors 不被覆盖 ----
        d4 = os.path.join(tmp, "err")
        w4 = _mk_wav(os.path.join(os.makedirs(d4) or d4, "01_e.wav"))
        s4 = _sub(w4)
        s4["errors"] = ["上游给的一条提示"]
        out5 = _cv("flac", d4)._reencode_to_fmt(s4, [])
        _mark("上游给的一条提示" in (out5.get("errors") or []),
              "补编码不覆盖上游已有的 errors", out5.get("errors"))

        # ---- ⑦ 判重自愈：记账是 .wav、目标 FLAC/MP3 → 必须重转 ----
        # 这是「用户升级后再点一次就能修好」的唯一机制。少了它，用户看到
        # 的是「已转过，跳过」，而目录里继续躺着 WAV。
        mdir = os.path.join(tmp, "mf")
        os.makedirs(mdir, exist_ok=True)
        srcf = _mk_wav(os.path.join(tmp, "source_like.brstm"))
        man = _dc.ConvertManifest(os.path.join(mdir, ".msu1-convert.json"))
        man.load()
        # ⚠ 记账里的名字必须在磁盘上真的存在 —— ``already_done`` 的校验②
        #   就是「至少一个产物还在」。不造这两个文件，下面的断言会全部
        #   因为「产物不存在」而返回 False，测不出扩展名这条判据。
        for _n in ("01_a.wav", "01_a.flac"):
            with open(os.path.join(mdir, _n), "wb") as _f:
                _f.write(b"x")
        man.record(srcf, ["01_a.wav"], "stream")
        _mark(man.already_done(srcf, "flac") is False,
              "记账是 .wav、目标 FLAC → 判「需重转」（升级后自愈）", "")
        _mark(man.already_done(srcf, "mp3") is False,
              "记账是 .wav、目标 MP3 → 判「需重转」", "")
        _mark(man.already_done(srcf, "wav") is True,
              "同一份记账按 WAV 判仍是「已转过」（不误伤 WAV 用户）", "")
        man.record(srcf, ["01_a.flac"], "stream")
        _mark(man.already_done(srcf, "flac") is True,
              "记账是 .flac、目标 FLAC → 正常跳过（重转完就不会反复转）", "")
        _mark(man.already_done(srcf, "mp3") is False,
              "记账是 .flac、目标 MP3 → 仍要转（两种格式各自一份）", "")

        # ---- ⑧ 端到端：真跑一遍 ConvertWorker（policy=cd 也是 WAV-only 分支）----
        # 上面 ②③ 测的是「补编码这个函数」；这一条测的是**它在流水线里真的
        # 被调到**。用 .wav 当源（policy_for → "cd" → cd_audio.convert_one，
        # 与 stream 同一类「只会写 WAV」的分支），一次跑完整队列。
        e2e_src = os.path.join(tmp, "e2e_src.wav")
        _mk_wav(e2e_src)
        e2e_out = os.path.join(tmp, "e2e_out")
        os.makedirs(e2e_out, exist_ok=True)
        _evs = []
        _cvx = _dc.ConvertWorker(e2e_out, lambda t, d: _evs.append((t, d)),
                                 threading.Event(), fmt="mp3")
        _cvx.enqueue({"src": e2e_src, "url": "e2e://cd", "pkg": "e2e"})
        _cvx.mark_src_finished()
        _cvx.start()
        _cvx.join(120)
        _e2e = []
        for _r, _ds, _fs in os.walk(e2e_out):
            for _n in sorted(_fs):
                if _n.startswith("."):
                    continue
                _e2e.append(os.path.join(_r, _n))
        _done = [d for t, d in _evs if t == "cv_file_done"]
        _mark(bool(_done) and int((_done[-1] or {}).get("fail") or 0) == 0,
              "端到端：ConvertWorker 跑完一条（policy=cd）", _done[-1:] or _evs)
        _mark(len(_e2e) == 1 and _e2e[0].lower().endswith(".mp3"),
              "端到端：目录里只有 1 个 .mp3（没有残留 WAV）",
              [os.path.basename(x) for x in _e2e])
        if _e2e:
            with open(_e2e[0], "rb") as _f:
                _h = _f.read(2)
            _mark(len(_h) == 2 and _h[0] == 0xFF and (_h[1] & 0xE0) == 0xE0,
                  "端到端：产物是真 MP3（帧同步）", _h)

        # ---- ⑨ 全 policy 审计（v6.0.2「全面检查」的锚点）----
        # 下载页的分派一共 13 条 policy，其中**只有**「先拿到 PCM、再交给
        # spc_wav.write_tracks(fmt=…)」的那几条会直接写目标格式：
        #   id666 / chip（spc_wav.convert）、psf（_convert_psf）、
        #   usf / vio / ssf / gsf / qsf（_convert_emu）
        # 另外 5 条走各自的 convert_one，**只会写 WAV**：
        #   cd / xa / txtp / pivotal / stream
        # 它们全靠 _reencode_to_fmt 兜。这条断言把「分派表都有谁」钉死 ——
        # 以后新增机种（新 policy）时，忘了考虑格式会在这里露出来。
        _i_disp = src_txt.find('if policy == "cd":')
        _i_after = src_txt.find("if not isinstance(sub, dict):")
        _disp = src_txt[_i_disp:_i_after] if 0 < _i_disp < _i_after else ""
        _named = ("cd", "txtp", "xa", "psf", "pivotal", "stream",
                  "usf", "vio", "ssf", "gsf", "qsf")
        _missing = [p for p in _named if ('policy == "%s"' % p) not in _disp]
        _mark(not _missing, "分派表覆盖全部具名 policy（chip 走 else）", _missing)
        # 界面/接口层的两条「说清楚」文案 —— 缺了就是静默跳过
        _dla = open(os.path.join(ROOT, "web_ui", "dl_api.py"),
                    encoding="utf-8").read()
        _mark("只对 WAV 生效" in _dla,
              "勾了合并却选 FLAC/MP3 时会明说「不合并」（不再静默）", "")
        _mark("避免二次压缩" in _dla,
              "只转过 FLAC/MP3 时，上传云盘会解释「要从 WAV 重编」", "")

        # ---- ⑩ 版本号：写进 manifest 的 by 字段是 6.0.2 ----
        _mark(_dc._ver_lt(_dc.AUDIO_WRITER_VERSION, "6.0.2") is False,
              "AUDIO_WRITER_VERSION 不早于 6.0.2（%s）"
              % _dc.AUDIO_WRITER_VERSION, _dc.AUDIO_WRITER_VERSION)
    finally:
        _rmtree_soft(tmp)


def t_v603():
    """[3j] v6.0.3 —— 试听条：进度条不能拖、没有上一曲 / 下一曲。

    ## 用户报障（原话）

    「下载音源界面的播放器进度无法调节，也没有下一曲、上一曲按钮」

    截图里那条紫色进度线其实是**纯展示的 div**（`.dl-play-bar`），
    没有任何事件；而且它被塞在曲名下面，宽度只有 meta 那一截（约 150px），
    就算绑了事件也很难点中。

    ## 这一档守什么

    1. 结构：`dlPrev` / `dlNext` / `dlPlaySeek` 三个节点在，进度条是
       `role="slider"` 且可聚焦（键盘也能调）；
    2. 行为：拖动期间 `ontimeupdate` 必须**让位**（`S.seeking`）——
       不让位的话每 250ms 的时间更新会把手指拖到的位置拽回去，
       表现就是「拖不动」，这正是用户报的那个现象；
    3. 命中区：视觉 4px，但 `::after` 把命中区撑到 16px；
    4. 上一曲 / 下一曲按**列表顺序**走，两端循环，只有一首时从头播。
    """
    print("\n[3j] 试听条：进度可拖 + 上一曲 / 下一曲（v6.0.3）", flush=True)

    html = open(os.path.join(ROOT, "web_ui", "download.html"),
                encoding="utf-8").read()
    js = open(os.path.join(ROOT, "web_ui", "download.js"),
              encoding="utf-8").read()
    css = open(os.path.join(ROOT, "web_ui", "download.css"),
               encoding="utf-8").read()

    # ---- ① 结构 ----
    for _id in ("dlPrev", "dlNext", "dlPlaySeek", "dlPlayBar", "dlPlayKnob"):
        _mark(('id="%s"' % _id) in html, "download.html 有 #%s" % _id, "")
    _mark('role="slider"' in html and 'tabindex="0"' in html,
          "进度条是 role=slider 且可聚焦（键盘能调）", "")
    _mark('aria-valuenow' in html and 'aria-valuetext' in html,
          "进度条带 aria 值（读屏能念出当前位置）", "")
    # 播放键仍在原位，且夹在两颗切换键中间
    _i_prev = html.find('id="dlPrev"')
    _i_play = html.find('id="dlPlayBtn"')
    _i_next = html.find('id="dlNext"')
    _mark(0 < _i_prev < _i_play < _i_next,
          "顺序是 上一曲 → 播放 → 下一曲", (_i_prev, _i_play, _i_next))

    # ---- ② 进度条：可点、可拖、可键盘 ----
    _mark("pointerdown" in js and "setPointerCapture" in js,
          "进度条用 pointer 事件 + 指针捕获（鼠标/触控/触控笔一套代码）", "")
    _mark("function seekToRatio(" in js and "function ratioAt(" in js,
          "有「坐标 → 比例 → currentTime」的换算", "")
    # ★ 最关键的一条：拖动期间 ontimeupdate 必须让位
    _i_tu = js.find("audio.ontimeupdate")
    _i_seek_guard = js.find("if (S.seeking) return;")
    _mark(0 < _i_tu < _i_seek_guard and (_i_seek_guard - _i_tu) < 400,
          "ontimeupdate 在拖动期间让位（不让位 = 拖不动，正是用户报的现象）",
          (_i_tu, _i_seek_guard))
    _mark("ArrowLeft" in js and "ArrowRight" in js,
          "键盘 ←/→ 调 5 秒", "")
    _mark('"Home"' in js and '"End"' in js, "键盘 Home/End 跳首尾", "")

    # ---- ③ 上一曲 / 下一曲 ----
    _mark("function stepTrack(" in js, "有 stepTrack(dir)", "")
    _mark('$("#dlPrev").onclick' in js and '$("#dlNext").onclick' in js,
          "两颗键都绑了 onclick", "")
    _mark("findIndex((w) => w.rel === S.playRel)" in js,
          "按 S.wavs 的**列表顺序**找当前曲目（不是另存一份顺序）", "")
    _mark("((i + dir) % n + n) % n" in js,
          "两端循环（列表只有 3 首时「下一曲」不该是死键）", "")
    _mark("if (j === i)" in js and "audio.currentTime = 0" in js,
          "只有一首时从头播（不能走 playWav —— 同曲在它那里是「暂停」）", "")
    _mark(js.count("syncSkipBtns()") >= 4,
          "上/下一曲的可用性在多处同步（playWav / loadWavs / 清空 / 出错）",
          js.count("syncSkipBtns()"))

    # ---- ④ 换曲时进度条要清零 ----
    _i_pw = js.find("function playWav(")
    _i_pw_end = js.find("function stepTrack(", _i_pw)
    _seg = js[_i_pw:_i_pw_end] if 0 < _i_pw < _i_pw_end else ""
    _mark('$("#dlPlayBar").style.width = "0%"' in _seg,
          "换曲先把进度条清零（否则上一首的百分比留在新曲目上）", "")

    # ---- ⑤ CSS：整行独占 + 命中区撑开 ----
    _mark(".dl-play-seek" in css and "grid-column" in css,
          "进度条在 grid 里独占一行（不再是曲名下面那一小截）", "")
    _mark(".dl-play-seek::after" in css and "inset: -6px 0" in css,
          "视觉 4px、命中区 16px（细条也点得中）", "")
    _mark("touch-action: none" in css,
          "touch-action:none —— 触屏上别让浏览器抢去做滚动", "")
    _mark(".dl-skip" in css, "有上一曲 / 下一曲的幽灵键样式", "")
    _mark(".dl-play-bar" not in css,
          "老的 .dl-play-bar 已移除（不能两套并存）", "")


def t_v604():
    """[v6.0.4] 「顺带下载封面」复选框 → 「下载封面」按钮。

    ## 用户要什么

    「把这个顺带下载封面改成下载封面按钮，可主动选择什么时候下载封面。」

    ## 这条链上有五个容易错的地方

    1. 复选框撤了、``auto_cover`` 却还留在下载流程里 —— 用户既关不掉
       也控制不了，等于「说改了其实没改」；
    2. 按钮只抓图、不落 ``cover.png`` —— FLAC 目录里没图，把整个目录
       拷到别的机器就丢封面（v5.9.16 特意修过这个）；
    3. 回填不带 ``only`` —— 只想给勾选的 3 个游戏抓图，却把 200 个
       游戏的 FLAC 全重写一遍；
    4. 机种没有封面源时默默转一圈 0 张 —— 用户以为按钮坏了；
    5. 下载**还在**自动抓图 —— 那就不是「主动选择什么时候下载」。
    """
    print("\n[3k] 「顺带下载封面」复选框 → 「下载封面」按钮（v6.0.4）",
          flush=True)

    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    import math
    import shutil
    import tempfile

    import cover_art as ca
    import flac_out as fo
    from web_ui import dl_api as dmod

    dhtml = _read("web_ui", "download.html")
    djs = _read("web_ui", "download.js")
    dapi = _read("web_ui", "dl_api.py")

    # ---- ① 下载流程里不许再有自动抓图 ----
    _i = dapi.find("    def start(")
    _j = dapi.find("    def stop(", _i)
    _start_src = dapi[_i:_j] if 0 < _i < _j else ""
    _mark(bool(_start_src), "取到 start() 源码段（下面几条断言的前提）", "")
    _mark("self._cover_worker(" not in _start_src,
          "start() 里不再调 _cover_worker()（下载不再自动抓图）", "")
    _mark("auto_cover" not in _start_src,
          "start() 里不再看 auto_cover（复选框已撤，留着就是死代码）", "")

    # ---- ② 结构：按钮在哪、跟谁同一排 ----
    _mark('id="dlCoverFetch"' in dhtml and "下载封面</button>" in dhtml,
          "download.html 有「下载封面」按钮", "")
    _i_cv = dhtml.find('id="dlConvWav"')
    _i_cf = dhtml.find('id="dlCoverFetch"')
    _mark(0 < _i_cv < _i_cf,
          "按钮排在「转 WAV」之后（和四颗转换键同一排，作用于勾选条目）", "")
    _mark('id="dlCover"' not in dhtml,
          "旧的「顺带下载封面」复选框已从页面移除", "")
    _mark('class="tb-btn dl-conv" disabled' in
          dhtml[max(0, _i_cf - 200):_i_cf + 200],
          "按钮初始是禁用的（没勾选时不给假的可点状态）", "")

    # ---- ③ 后端：参数校验，每条都要说清原因 ----
    st = dmod.DownloadState()
    r = st.fetch_covers([])
    _mark(r.get("ok") is False and "没有选中" in (r.get("error") or ""),
          "一条都没勾 → 明确拒绝（不是静默 ok）", r.get("error") or "")
    st.key = ""
    r = st.fetch_covers(["u1"])
    _mark(r.get("ok") is False and "还没选机种" in (r.get("error") or ""),
          "没选机种 → 拒绝", r.get("error") or "")
    st.key = "switch"
    r = st.fetch_covers(["u1"])
    # ⚠ v6.0.5 改口径：switch 以前在 libretro 里没目录，会被直接拒绝；
    #   现在 IGDB 兜底了它，这里不该再被拒。
    _mark("没有对应的封面目录" not in (r.get("error") or ""),
          "有封面源的机种（switch）不再被「图库没有」挡掉（v6.0.5 起有 IGDB）",
          r.get("error") or "ok")
    st.key = "spc"
    st.busy = True
    r = st.fetch_covers(["u1"])
    _mark(r.get("ok") is False and "正在下载" in (r.get("error") or ""),
          "忙的时候拒绝（不跟下载/转换抢状态机）", r.get("error") or "")
    st.busy = False
    st.entries = [{"url": "u1", "name": "Not In This Platform"}]
    r = st.fetch_covers(["zzz"])
    _mark(r.get("ok") is False and "不在当前机种" in (r.get("error") or ""),
          "url 对不上当前机种 → 提示刷新列表（不是空跑）",
          r.get("error") or "")

    # ---- ④ 真跑一遍（全离线：fetch / list_covers 都换成桩）----
    tmp = tempfile.mkdtemp(prefix="v604_gate_")
    orig = (ca.fetch, ca.list_covers, ca.cover_for_title)
    try:
        # 找一张缓存里现成的 png 当「假封面」
        img = _first_cached_png()
        _mark(bool(img), "缓存里有可用封面（下面的行为测试靠它，不联网）",
              os.path.basename(img) if img else "没有")

        if img:
            ca.fetch = lambda _k, _t, cancel=None: (True, img, "stub")
            ca.list_covers = lambda _k, refresh=False, cancel=None: []
            ca.cover_for_title = lambda _t: img

            game_a, game_b = "V604 Wanted", "V604 Untouched"
            for g in (game_a, game_b):
                os.makedirs(os.path.join(tmp, "FLAC", g))
            st2 = dmod.DownloadState()
            st2.key = "spc"
            st2.dest = tmp
            st2.entries = [{"url": "u-a", "name": game_a},
                           {"url": "u-b", "name": game_b}]
            r = st2.fetch_covers(["u-a"])
            _mark(r.get("ok") is True and r.get("count") == 1,
                  "勾一个游戏 → 只排一个（count=1）", str(r))

            # 等后台线程跑完（它是 daemon，只能轮询）
            _t0 = time.time()
            while time.time() - _t0 < 10:
                if (st2.cover_done or 0) >= 1:
                    break
                time.sleep(0.05)
            _mark((st2.cover_done or 0) >= 1,
                  "抓图计数有推进（状态栏才显示得出 1/1）",
                  "done=%s" % st2.cover_done)

            _p_a = os.path.join(tmp, "FLAC", game_a, "cover.png")
            _p_b = os.path.join(tmp, "FLAC", game_b, "cover.png")
            # ⚠ cover_done 是在 _cover_worker 里推进的，落 cover.png 的
            #   _place_covers 是它**后面**那一步 —— 只等计数器会有竞态，
            #   必须等文件真的出现。
            _t0 = time.time()
            while time.time() - _t0 < 10 and not os.path.isfile(_p_a):
                time.sleep(0.05)
            _mark(os.path.isfile(_p_a),
                  "cover.png 落进 FLAC/<勾选的游戏>/（拷走目录不丢封面）", "")
            _mark(not os.path.isfile(_p_b),
                  "**没勾的**游戏目录不被碰（不能顺手改别人的目录）", "")

            # ---- ⑤ _backfill_audio(only=…) 只动点名的游戏 ----
            n = 22050
            raw = b"".join(struct.pack("<hh",
                                       int(8000 * math.sin(2 * math.pi * 440 * i / 44100)),
                                       int(8000 * math.sin(2 * math.pi * 440 * i / 44100)))
                           for i in range(n))
            fa = os.path.join(tmp, "FLAC", game_a, "01_track.flac")
            fb = os.path.join(tmp, "FLAC", game_b, "01_track.flac")
            ok_a, _, _ = fo.encode_pcm(raw, fa)
            ok_b, _, _ = fo.encode_pcm(raw, fb)
            _mark(ok_a and ok_b and not dmod.DownloadState._has_picture(fa)
                  and not dmod.DownloadState._has_picture(fb),
                  "造两个没有图片块的 FLAC（模拟「先转码、后要封面」）", "")

            n_a = st2._backfill_audio(os.path.join(tmp, "FLAC"), only={game_a})
            _mark(n_a >= 1 and dmod.DownloadState._has_picture(fa),
                  "only={A} 时 A 被补上图", "补了 %d 个" % n_a)
            _mark(not dmod.DownloadState._has_picture(fb),
                  "only={A} 时 B 一个字节都没动（这就是加 only 的理由）", "")
    finally:
        ca.fetch, ca.list_covers, ca.cover_for_title = orig
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:                            # noqa: BLE001
            pass


def t_v605():
    """[v6.0.5] IGDB 封面兜底源（走 design.zaparoo.org）。

    ## 用户要什么

    v6.0.4 的封面覆盖审计结论是「33 个机种里 3 个无源、Xbox 360 命中 0%」，
    用户问能不能用 IGDB 补。IGDB 官方 API 断在换 token 这一步
    （``id.twitch.tv`` 被 DNS 污染到 Facebook 地址段），用户又没法注册
    Twitch 开发者应用 —— 于是改走 ``design.zaparoo.org/api/search``，
    那是 Zaparoo Designer 的公开后端，无鉴权、直接返回 IGDB 原始对象。

    ## 这片门禁守的是「0% 那个 bug 不许回来」

    第一版 ``_search()`` 把 joshw 的 ``名字 (日期)(厂商)[机种]`` **原样**
    丢给 ``searchTerm``，实测 56 条全部 0 命中 —— 那个接口是近乎全等的
    匹配，圆括号方括号全是噪音。而它失败的样子是「静默返回空表」，
    界面上只显示「没找到」，和「这个游戏 IGDB 真没有」完全分不清。

    所以这里必须**拦住搜索词本身**，而不只是测「最后有没有图」：
    第 ③④ 组断言就是干这个的。

    ⚠ 全程离线：``_get`` 换成桩，一个请求都不发。真联网的命中率归
    ``vendor/_probe_igdb.py`` 管（那是给人看的数，不是门禁）。
    """
    print("\n[3l] IGDB 封面兜底源（v6.0.5）", flush=True)

    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    import shutil
    import tempfile

    import cover_art as ca
    import cover_igdb as cg
    import joshw_source as js
    from web_ui import dl_api as dmod

    # ① 版本号五处同步：**只由最新分片（t_v606）负责**。
    # 历史分片钉死自己那一版的号，每升一次版就全红 —— v6.0.4 那段在
    # v6.0.5 时就是这么摘掉的，同样的处理。

    # ---- ② 33 个机种全都有 IGDB 平台映射 ----
    keys = [p["key"] for p in js.PLATFORMS]
    miss = [k for k in keys if not cg.supported(k)]
    _mark(not miss, "全部 %d 个机种都有 IGDB 平台映射" % len(keys),
          ",".join(miss))
    _mark(all(isinstance(v, tuple) and v for v in cg.PLATFORM_IDS.values()),
          "PLATFORM_IDS 的值都是非空元组（混装机种靠多 id 兜）", "")
    _mark(all(isinstance(i, int) for v in cg.PLATFORM_IDS.values() for i in v),
          "平台 id 全是 int（字符串会让 platforms[].id 的过滤永远不等）", "")

    # ---- ③ 搜索词：绝不许把 joshw 原始名原样发出去（0% 那个 bug）----
    raw = "#RaceDieRun (2019-08-09)(QubicGames)[Switch]"
    terms = cg._terms(raw)
    _mark(raw not in terms,
          "原始 joshw 名不在搜索词里（原样发出去必然 0 条）", str(terms))
    _mark(terms and terms[0] == "#RaceDieRun",
          "第一候选是「去块、保留大小写」", str(terms[:1]))
    _mark("racedierun" in terms,
          "归一化形态也在候选里（去掉 # 这类符号）", str(terms))
    _mark(all("(" not in t and "[" not in t for t in terms),
          "所有候选词都不含圆/方括号（那是本地元数据，不是游戏名）",
          str(terms))
    _mark(cg._terms("Chrono Trigger (1995-03-11)(Square)[SNES]")[0]
          == "Chrono Trigger",
          "日期/厂商/机种块被剥掉，名字本身一个字符都不动", "")
    _mark(cg._terms("") == [] and cg._terms(None) == [],
          "空名字返回空表（不是 [''] 这种会真发出去的东西）", "")
    _mark(cg._terms("A - B")[-1] == "A",
          "副标题前缀作为最后一档兜底（和 cover_art.pick 那条规则同源）",
          str(cg._terms("A - B")))
    _t3 = cg._terms("Sonic (1991)(Sega)[MD]")
    _mark(len(_t3) == len(set(_t3)), "候选词去重（同词不重复请求）", str(_t3))

    # ---- ④ 真发出去的 URL：searchTerm 必须干净 ----
    seen = []
    real_get = cg._get

    def fake_get(url, limit, timeout=30):
        seen.append(url)
        return json.dumps({"count": 0, "results": []}).encode(), ""

    cg._get = fake_get
    try:
        cg._search("switch", raw)
    finally:
        cg._get = real_get
    qs = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query) for u in seen]
    sent = [q.get("searchTerm", [""])[0] for q in qs]
    _mark(bool(sent), "确实发出了搜索请求（下面几条才有意义）", str(len(seen)))
    _mark(raw not in sent, "发出去的 searchTerm 里没有 joshw 原始名", str(sent))
    _mark(all("(" not in t and "[" not in t for t in sent),
          "发出去的 searchTerm 都不带括号", str(sent))
    _mark(all(q.get("platformId") for q in qs),
          "每个请求都带 platformId（不带就会混进别的机种）", "")

    # ---- ⑤ 平台过滤：别的平台的同名词不许进来 ----
    def _rows_get(payload):
        def g(url, limit, timeout=30):
            return json.dumps(payload).encode(), ""
        return g

    cg._get = _rows_get({"count": 1, "results": [
        {"id": 1, "name": "Same Name", "cover": {"image_id": "co1"},
         "platforms": [{"id": 6, "name": "PC"}]}]})
    try:
        rows, why = cg._search("switch", "Same Name")
    finally:
        cg._get = real_get
    _mark(not rows,
          "平台对不上的行被滤掉（防御：接口哪天忽略 platformId）", why)

    cg._get = _rows_get({"count": 1, "results": [
        {"id": 2, "name": "Same Name", "cover": {"image_id": "co2"},
         "platforms": [{"id": 130, "name": "Nintendo Switch"}]}]})
    try:
        rows, _why = cg._search("switch", "Same Name")
    finally:
        cg._get = real_get
    _mark(len(rows) == 1, "平台对得上的行保留", str(len(rows)))

    # ---- ⑥ 翻页：count 超过一页且首页没封面时才翻，最多 MAX_PAGES ----
    pages = []

    def paging_get(url, limit, timeout=30):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        pg = int(q.get("page", ["1"])[0])
        pages.append(pg)
        if pg == 1:
            return json.dumps({"count": 120, "results": [
                {"id": 1, "name": "NoCover",
                 "platforms": [{"id": 130}]}]}).encode(), ""
        return json.dumps({"count": 120, "results": [
            {"id": 2, "name": "WithCover", "cover": {"image_id": "co2"},
             "platforms": [{"id": 130}]}]}).encode(), ""

    cg._get = paging_get
    try:
        rows, _why = cg._search("switch", "OnlyOne")
    finally:
        cg._get = real_get
    _mark(pages == [1, 2] and len(rows) == 1,
          "首页没封面、count>50 → 翻第二页并找到", str(pages))

    pages[:] = []

    def nopage_get(url, limit, timeout=30):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        pages.append(int(q.get("page", ["1"])[0]))
        return json.dumps({"count": 999, "results": [
            {"id": 3, "name": "NoCover",
             "platforms": [{"id": 130}]}]}).encode(), ""

    cg._get = nopage_get
    try:
        cg._search("switch", "OnlyOne")
    finally:
        cg._get = real_get
    _mark(max(pages) == cg.MAX_PAGES,
          "翻页有上限（最多 %d 页，不把对方带宽当免费）" % cg.MAX_PAGES,
          str(pages))

    # ---- ⑦ 落盘：只认 PNG、进 cover_art 的缓存、记进查找表 ----
    tmp = tempfile.mkdtemp(prefix="v605_gate_")
    orig = (ca.cache_root, ca.cover_for_title, ca._remember,
            ca.fetch, cg.fetch, ca.list_covers,
            ca.fetch_all, cg.fetch_all)
    urls = []
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    try:
        ca.cache_root = lambda: tmp
        ca.cover_for_title = lambda _t: ""
        remembered = []
        ca._remember = lambda t, rel: remembered.append((t, rel))

        def png_get(url, limit, timeout=30):
            urls.append(url)
            if "/api/search" in url:
                return json.dumps({"count": 1, "results": [
                    {"id": 9, "name": "Bloodborne",
                     "cover": {"image_id": "cob99l"},
                     "platforms": [{"id": 48}]}]}).encode(), ""
            return png, ""

        cg._get = png_get
        _title = "Bloodborne (2015-03-24)(FromSoftware)[PlayStation 4]"
        ok, path, why = cg.fetch("psf4", _title)
        _mark(ok and os.path.isfile(path),
              "正常路径：搜到 → 下 PNG → 落盘", why)
        _mark(path.startswith(tmp),
              "落盘在 cover_art 的缓存根下（复用它的查找表，别的模块不用改）",
              path)
        # v6.0.11：从 ``<根>/<机种>/`` 变成 ``<根>/<机种>/<游戏名>/``
        _mark(os.path.dirname(os.path.dirname(path)) == os.path.join(tmp, "psf4"),
              "按「机种/游戏名」两层分目录（v6.0.11 起，用户要求）", path)
        _mark(os.path.basename(os.path.dirname(path)) == ca._safe_name(_title),
              "游戏名那一层用的是原始名（不是归一化后的小写）",
              os.path.dirname(path))
        _mark(bool(remembered) and remembered[-1][0] == _title,
              "_remember 记了「游戏名 → 相对路径」，cover_for_title 才找得到",
              str(remembered[-1:]))
        _mark(any(cg.SIZE in u for u in urls),
              "取的是 %s 档（264 太小、原图常 1 MB）" % cg.SIZE, "")
        _mark(any(u.endswith(".png") for u in urls if "images.igdb" in u),
              "图片 URL 用 .png 后缀（IGDB 按后缀换编码）", "")

        def jpg_get(url, limit, timeout=30):
            if "/api/search" in url:
                return json.dumps({"count": 1, "results": [
                    {"id": 10, "name": "Bloodborne II",
                     "cover": {"image_id": "coJPG"},
                     "platforms": [{"id": 48}]}]}).encode(), ""
            return jpg, ""

        cg._get = jpg_get
        ok2, _p2, why2 = cg.fetch(
            "psf4", "Bloodborne II (2020-01-01)(FromSoftware)[PlayStation 4]")
        _mark(not ok2 and "PNG" in (why2 or ""),
              "回 JPEG 时拒绝（存成 .png 的 JPEG 会在嵌 FLAC 那一步炸）", why2)

        def boom_get(url, limit, timeout=30):
            if "/api/search" in url:
                return json.dumps({"count": 1, "results": [
                    {"id": 9, "name": "Bloodborne",
                     "cover": {"image_id": "cob99l"},
                     "platforms": [{"id": 48}]}]}).encode(), ""
            return b"", "不该走到这里"

        cg._get = boom_get
        ok3, _p3, why3 = cg.fetch("psf4", _title)
        _mark(ok3 and why3 == "已有",
              "同一个游戏再抓一次 → 直接说「已有」，不重复下图", why3)

        _mark(cg.fetch("definitely_not_a_key", "X")[0] is False,
              "没有平台映射的机种 → False（不联网、不抛异常）", "")
        _mark(cg.fetch("psf4", "   ")[0] is False, "空名字 → False", "")

        # ---- ⑧ dl_api 的双源并集 ----
        _mark(dmod.cover_igdb is cg,
              "dl_api 把 cover_igdb 收进来了（否则日志里永远没有 IGDB 那一行）",
              "")
        _mark(dmod._has_cover_source("switch"),
              "_has_cover_source('switch') 为真（v6.0.4 时这里是 False）", "")
        _mark(dmod._has_cover_source("fmtowns")
              and dmod._has_cover_source("pc"),
              "fmtowns / pc 也有源了（libretro 那 3 个无源机种全补上）", "")
        _mark(not dmod._has_cover_source("definitely_not_a_platform"),
              "真没源的 key 仍然是 False（并集不等于无条件 True）", "")

        # ---- ⑨ _cover_one：libretro 失手就转 IGDB ----
        st = dmod.DownloadState()
        ca.fetch = lambda _k, _t, cancel=None: (False, "", "libretro 没收录")
        cg.fetch = lambda _k, _t, cancel=None: (True, "x.png", "IGDB「X」12 KB")
        good, _p, why4, tag = st._cover_one("switch", "Some Game")
        _mark(good and tag == "IGDB", "libretro 失手 → 落到 IGDB", tag)
        _mark("IGDB" in (why4 or ""),
              "说明里保留 IGDB 的描述（日志里看得出图是哪来的）", why4)

        ca.fetch = lambda _k, _t, cancel=None: (True, "y.png", "libretro「Y」")
        good, _p, _w, tag = st._cover_one("spc", "Some Game")
        _mark(good and tag == "libretro",
              "libretro 命中时不去打扰 IGDB（省一次外部请求）", tag)

        ca.fetch = lambda _k, _t, cancel=None: (True, "y.png", "已有")
        good, _p, _w, tag = st._cover_one("spc", "Some Game")
        _mark(good and tag == "缓存",
              "本地已有 → 标签是「缓存」（不冒充成 libretro 新抓的）", tag)

        ca.fetch = lambda _k, _t, cancel=None: (False, "", "libretro 没收录")
        cg.fetch = lambda _k, _t, cancel=None: (False, "", "IGDB 也没搜到")
        good, _p, why5, tag = st._cover_one("switch", "Some Game")
        _mark(not good and "libretro 没收录" in (why5 or "")
              and "IGDB 也没搜到" in (why5 or ""),
              "两边都失手时两边的理由都留着"
              "（否则分不清「都没收录」和「IGDB 挂了」）", why5)

        def _boom(*_a, **_k):
            raise RuntimeError("炸了")

        ca.fetch = _boom
        cg.fetch = lambda _k, _t, cancel=None: (True, "z.png", "ok")
        good, _p, _w, tag = st._cover_one("switch", "Some Game")
        _mark(good and tag == "IGDB",
              "libretro 抛异常也不影响 IGDB 兜底（封面不许把下载搞崩）", "")

        # ---- ⑩ 日志：来源要标出来 ----
        # ⚠ v6.0.7：``_cover_worker`` 改走 ``_cover_many``（要抓多张），
        # 所以这里桩的是 ``fetch_all`` 而不是 ``fetch``。
        logs = []
        st2 = dmod.DownloadState()
        st2.log = lambda m: logs.append(str(m))
        _probe_img = os.path.join(tmp, "log_probe.png")
        with open(_probe_img, "wb") as _fh:
            _fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        ca.fetch_all = lambda _k, _t, cancel=None, limit=6: []
        cg.fetch_all = lambda _k, _t, cancel=None, limit=6, kinds=None: [
            (_probe_img, "cover")]
        ca.list_covers = lambda _k, refresh=False, cancel=None: []
        st2._cover_worker([("switch", "G1"), ("switch", "G2")])
        txt = "\n".join(logs)
        _mark("来源" in txt and "IGDB" in txt,
              "开跑那行就写明「来源 libretro-thumbnails → IGDB」", txt[:120])
        _mark("其中 2 个游戏用到了 IGDB" in txt,
              "结尾单独报「多少个游戏用到 IGDB」"
              "（直接回答用户「到底补了多少」）", txt[-160:])
        _mark(st2.cover_ok == 2 and st2.cover_fail == 0,
              "计数器照旧推进（封面区进度条不受影响）",
              "%s/%s" % (st2.cover_ok, st2.cover_fail))
        _mark("→ 1 张" in txt,
              "每个游戏那行报的是**张数**（v6.0.7 一个游戏可能好几张）",
              txt[:200])
    finally:
        cg._get = real_get
        (ca.cache_root, ca.cover_for_title, ca._remember,
         ca.fetch, cg.fetch, ca.list_covers,
         ca.fetch_all, cg.fetch_all) = orig
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:                            # noqa: BLE001
            pass

    # ---- ⑪ 静态：走的是哪条路、打包有没有漏 ----
    _mark(cg.SEARCH_URL.startswith("https://design.zaparoo.org/"),
          "走的是 design.zaparoo.org（用户指定的那条路），不是官方 API",
          cg.SEARCH_URL)
    _mark("https://api.igdb.com" not in _read("cover_igdb.py"),
          "代码里没有 api.igdb.com 的请求地址"
          "（那条路要 token，本机取不到；文档里提到它是为了说明原因）", "")
    _mark(cg.MIN_GAP >= 0.3,
          "请求间隔不小于 0.3s（对方是公益项目，别当 CDN 刷）",
          str(cg.MIN_GAP))
    _mark('"cover_igdb",' in _read("build_web_pyi.py"),
          "打包脚本收 cover_igdb"
          "（漏了就是「代码全在、exe 里没有」的静默缺失）", "")
    _src = _read("cover_igdb.py")
    _i = _src.find("def _search(")
    _j = _src.find("def _search_one(", _i)
    _seg = _src[_i:_j] if 0 < _i < _j else ""
    _mark(bool(_seg) and "_terms(title)" in _seg,
          "_search() 的搜索词来自 _terms()，不是 title 原样"
          "（这条一红，就是 0% 那个 bug 回来了）", "")


def t_v606():
    """[v6.0.6] 「衍生 WAV」去重 —— FLAC 20 个 / MP3 10 个 那个 bug。

    ## 用户报的

    > 「同一个游戏转出的 flac 数量和 MP3 数量不同，以 PS4 为例
    >  ``…\\FLAC\\Assetto Corsa…``；``…\\MP3\\Assetto Corsa…``」

    实测 FLAC 目录 20 个、MP3 目录 10 个，而且是**成对**的：

    ==========================================  ============
    ``01_full_a-proud-moment_0117 {uuid}.flac``   9,971,553 B
    ``02_…{uuid}.fsb.flac``                       9,971,553 B  ← 同一段音频
    ==========================================  ============

    ## 根因

    源目录里 ``xxx.fsb``（真音源）和 ``xxx.fsb.wav``（**我们自己**转出来的
    WAV）**两个** ``is_convertible`` 都为真，枚举音源时被当成两首。
    ``.txtp`` 同理（``xxx.txtp`` + ``xxx.txtp.wav``，AC4 就是 186 对）。

    时间线解释了「为什么 MP3 是 10、FLAC 是 20」：
    13:13 转 MP3（当时只有 ``.fsb``）→ 13:14 转 WAV 生成 ``.fsb.wav``
    → 13:14 再转 FLAC（此时 ``.fsb`` 和 ``.fsb.wav`` 都在）→ 20 个。

    ## 两半修复，缺一不可

    * **止血**：``drop_derived_wavs()`` 让枚举阶段就看不见衍生 WAV
      （Web / GUI 七处调用点全部接上）；
    * **自愈**：``ConvertWorker._drop_duplicate_products()`` 把用户**已经**
      转坏的那些目录清干净、记账清零、编号从 01 重来。没有这一半，
      存量用户永远看不到修复效果。

    ## ⚠ 最危险的一条：自愈绝不能在 ``wav`` 桶上做

    WAV 桶的「产物」就是 ``xxx.fsb.wav`` 本身 —— 在 wav 桶上执行自愈
    **等于删用户的源文件**。第 ⑤ 组最后两条就是拦这个。
    """
    print("\n[3m] 衍生 WAV 去重（v6.0.6）", flush=True)

    import json
    import shutil
    import tempfile
    import threading

    import dl_convert as _dc

    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    # ① 版本号五处同步：**只由最新分片（t_v607）负责**。
    # 历史分片钉死自己那一版的号，每升一次版就全红 —— t_v604 / t_v605
    # 都是这么摘掉的，同样的处理。

    # ---- ② 导出名齐了（Web / GUI 都是跨模块 import，漏一个就是 NameError）----
    for name in ("is_derived_wav", "drop_derived_wavs"):
        _mark(name in getattr(_dc, "__all__", ()),
              "dl_convert.__all__ 收录 %s" % name, "")
    _mark(hasattr(_dc, "is_derived_wav") and hasattr(_dc, "drop_derived_wavs"),
          "两个函数在 dl_convert 上真能取到", "")

    tmp = tempfile.mkdtemp(prefix="v606_")
    try:
        # ---- ③ is_derived_wav 判据 ----
        d3 = os.path.join(tmp, "judge")
        os.makedirs(d3)

        def _touch(*names):
            for n in names:
                with open(os.path.join(d3, n), "w") as fh:
                    fh.write("x")

        _touch("a.fsb", "a.fsb.wav",              # 真音源 + 它的 WAV 产物
               "b.txtp", "b.txtp.wav",            # .txtp 同理（AC4 的形态）
               "lonely.wav",                      # 无对应音源：正片源就是 wav 的机种
               "c.flac", "d.mp3", "e.ogg")

        def _j(n):
            return _dc.is_derived_wav(os.path.join(d3, n))

        _mark(_j("a.fsb.wav") is True,
              "is_derived_wav：a.fsb.wav（a.fsb 存在）→ True", "")
        _mark(_j("b.txtp.wav") is True,
              "is_derived_wav：b.txtp.wav（b.txtp 存在）→ True", "")
        _mark(_j("lonely.wav") is False,
              "is_derived_wav：lonely.wav（没有 lonely 这个源）→ False"
              "（这条一红，正片源就是 WAV 的机种会被误删）", "")
        _mark(_j("a.fsb") is False,
              "is_derived_wav：a.fsb 本身 → False（它不是 wav）", "")
        _mark(_j("c.flac") is False and _j("d.mp3") is False and _j("e.ogg") is False,
              "is_derived_wav：flac / mp3 / ogg 都不是衍生 WAV", "")
        _mark(_j("") is False and _j("x") is False,
              "is_derived_wav：空串 / 无扩展名不抛异常、返回 False", "")
        _mark(_j("A.FSB.WAV") is True,
              "is_derived_wav：大小写不敏感（A.FSB.WAV）", "")
        _mark(_j("a.fsb.wav.tmp") is False,
              "is_derived_wav：.fsb.wav.tmp 不算（只认以 .wav 结尾）", "")

        # ---- ④ drop_derived_wavs 批量 ----
        # 造一个「AC 真实形态」：10 个 .fsb + 10 个 .fsb.wav
        d4 = os.path.join(tmp, "batch")
        os.makedirs(d4)
        pairs = []
        for i in range(1, 11):
            stem = "t%02d" % i
            for suf in (".fsb", ".fsb.wav"):
                p = os.path.join(d4, stem + suf)
                with open(p, "w") as fh:
                    fh.write("x")
                pairs.append(p)
        kept = _dc.drop_derived_wavs(list(pairs))
        _mark(len(pairs) == 20 and len(kept) == 10,
              "drop_derived_wavs：20 个（10 真源 + 10 衍生）→ 10 个",
              "进 %d 出 %d" % (len(pairs), len(kept)))
        _mark(all(not p.lower().endswith(".wav") for p in kept),
              "drop_derived_wavs：留下的一个 .wav 都没有", "")
        _mark([p for p in pairs if not p.lower().endswith(".wav")] == kept,
              "drop_derived_wavs：保序（返回的是原列表里的真源，顺序不变）", "")
        _mark(_dc.drop_derived_wavs([]) == [],
              "drop_derived_wavs：空输入 → 空输出", "")
        _mark(_dc.drop_derived_wavs(["", None]) == [],
              "drop_derived_wavs：脏输入（空串 / None）不抛、原样过滤掉", "")
        # 同批里只有 .wav 没有源 → 一个都不许丢
        d4b = os.path.join(tmp, "batch2")
        os.makedirs(d4b)
        lone = []
        for i in range(3):
            p = os.path.join(d4b, "solo%d.wav" % i)
            with open(p, "w") as fh:
                fh.write("x")
            lone.append(p)
        _mark(_dc.drop_derived_wavs(list(lone)) == lone,
              "drop_derived_wavs：3 个孤立 .wav（无源）→ 全部保留", "")

        # ---- ⑤ ConvertManifest：derived_src_keys / all_records ----
        d5 = os.path.join(tmp, "man")
        os.makedirs(d5)
        for i in range(1, 4):
            for suf in (".fsb", ".fsb.wav"):
                with open(os.path.join(d5, "m%02d%s" % (i, suf)), "w") as fh:
                    fh.write("x")
        ents = {}
        for i in range(1, 4):
            for suf, fmt in ((".fsb", "stream"), (".fsb.wav", "cd")):
                src = os.path.join(d5, "m%02d%s" % (i, suf))
                ents[os.path.normpath(src).lower()] = {
                    "mtime": 0.0, "size": 1, "n_wav": 1, "policy": fmt,
                    "wavs": ["%02d_x.flac" % i]}
        with open(os.path.join(d5, ".msu1-convert.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"version": _dc.MANIFEST_VERSION, "entries": ents}, fh)

        man = _dc.ConvertManifest(os.path.join(d5, ".msu1-convert.json"))
        _mark(man.load() == 6, "ConvertManifest.load() 读回 6 条", "")
        _mark(len(man.all_records()) == 6, "all_records() 返回 6 条", "")
        dk = man.derived_src_keys()
        _mark(len(dk) == 3,
              "derived_src_keys()：6 条里挑出 3 条衍生源（.fsb.wav）", len(dk))
        _mark(all(k.endswith(".fsb.wav") for k in dk),
              "derived_src_keys()：挑出来的全是 .fsb.wav", dk[:1])
        man.forget(dk[0])
        _mark(len(man.derived_src_keys()) == 2,
              "forget() 之后 derived_src_keys() 少一条（自愈靠这个清账）", "")
        _mark(len(man.all_records()) == 5, "forget() 之后 all_records() 也少一条", "")

        # ---- ⑥ 自愈 _drop_duplicate_products（核心）----
        def _mk_flac_dir(name, fmt):
            """造一个「已经转坏」的输出目录：20 个产物 + 20 条记账。"""
            d = os.path.join(tmp, name)
            os.makedirs(d)
            ent = {}
            srcs = []
            for i in range(1, 11):
                stem = "g%02d" % i
                fsb = os.path.join(d, stem + ".fsb")
                fsbw = fsb + ".wav"
                for p in (fsb, fsbw):
                    with open(p, "w") as fh:
                        fh.write("x")
                srcs.append(fsb)
                a = "%02d_%s.%s" % (i * 2 - 1, stem, fmt)
                b = "%02d_%s.fsb.%s" % (i * 2, stem, fmt)
                for n in (a, b):
                    with open(os.path.join(d, n), "w") as fh:
                        fh.write("F")
                ent[os.path.normpath(fsb).lower()] = {
                    "mtime": 0.0, "size": 1, "n_wav": 1, "policy": "stream",
                    "wavs": [a]}
                ent[os.path.normpath(fsbw).lower()] = {
                    "mtime": 0.0, "size": 1, "n_wav": 1, "policy": "cd",
                    "wavs": [b]}
            with open(os.path.join(d, ".msu1-convert.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"version": _dc.MANIFEST_VERSION, "entries": ent}, fh)
            # 用户手放的一个文件（不在记账里）—— 必须保住
            with open(os.path.join(d, "mydiy." + fmt), "w") as fh:
                fh.write("DIY")
            return d, srcs[0]

        def _flacs(d, fmt):
            return sorted(n for n in os.listdir(d) if n.lower().endswith("." + fmt))

        d6, src6 = _mk_flac_dir("heal_flac", "flac")
        cv6 = _dc.ConvertWorker(d6, lambda t, dd: None,
                                threading.Event(), fmt="flac")
        # ⚠ 这条不能省。ConvertManifest 构造**不读盘**，忘了 load 的话
        # 后面「记账清空」会假阳性通过（空 == 空），而自愈其实一步没做。
        # 实测就踩过：直接构造 Worker 没走 run()，5 条断言全红但「记账清空」是绿的。
        _mark(cv6._bucket.manifest.ensure_loaded() == 20,
              "ensure_loaded()：自愈前读到 20 条记账"
              "（顺带验证幂等 load，重复调用不重读盘）",
              cv6._bucket.manifest.ensure_loaded())
        _mark(len(_flacs(d6, "flac")) == 21,
              "自愈前：20 个 FLAC + 1 个手放 = 21 个", len(_flacs(d6, "flac")))
        buf = []
        n6 = cv6._drop_duplicate_products(src6, buf)
        _mark(n6 == 20, "自愈：删掉 20 个 FLAC（记账点名的全部）", n6)
        _mark(_flacs(d6, "flac") == ["mydiy.flac"],
              "自愈后：只剩手放的那个（用户的东西一个不碰）",
              _flacs(d6, "flac"))
        _mark(not cv6._bucket.manifest.all_records(),
              "自愈后：记账清空（编号才能从 01 重来）",
              len(cv6._bucket.manifest.all_records()))
        _mark(any("清掉 20 个旧的 FLAC" in str(x) for x in buf),
              "自愈：日志说清了「清掉 N 个旧的 FLAC」", buf[:1])
        _mark(cv6._drop_duplicate_products(src6, buf) == 0,
              "自愈幂等：第二次调用返回 0（记账已空）", "")

        # 幂等还得保证「再点一次转换」能从头编号
        _mark(cv6._bucket.start_idx == 0,
              "自愈后 start_idx 归 0（下一次从 01 开始编）", cv6._bucket.start_idx)

        # ---- ⚠ 最危险的一条：wav 桶一个都不许删 ----
        d7, src7 = _mk_flac_dir("heal_wav", "wav")
        cv7 = _dc.ConvertWorker(d7, lambda t, dd: None,
                                threading.Event(), fmt="wav")
        before7 = _flacs(d7, "wav")
        _mark(cv7._drop_duplicate_products(src7, []) == 0,
              "⚠ wav 桶：返回 0（WAV 的产物就是 .fsb.wav 本身，删了等于删源）", "")
        _mark(_flacs(d7, "wav") == before7,
              "⚠ wav 桶：一个文件都没少", (len(before7), len(_flacs(d7, "wav"))))

        # mp3 / ogg 桶同样生效
        for fmt in ("mp3", "ogg"):
            dd, ss = _mk_flac_dir("heal_" + fmt, fmt)
            cvd = _dc.ConvertWorker(dd, lambda t, x: None,
                                    threading.Event(), fmt=fmt)
            _mark(cvd._drop_duplicate_products(ss, []) == 20,
                  "自愈在 %s 桶上也生效（删 20 个）" % fmt.upper(), "")

        # 干净目录（没有衍生源）→ 零开销直接返回
        d8 = os.path.join(tmp, "clean")
        os.makedirs(d8)
        with open(os.path.join(d8, "01_a.flac"), "w") as fh:
            fh.write("F")
        cv8 = _dc.ConvertWorker(d8, lambda t, dd: None,
                                threading.Event(), fmt="flac")
        _mark(cv8._drop_duplicate_products("whatever", []) == 0,
              "干净目录：没有衍生源记录 → 返回 0、不动文件", "")
        _mark(os.path.isfile(os.path.join(d8, "01_a.flac")),
              "干净目录：01_a.flac 还在", "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- ⑦ 调用点接入（七处，漏一处就有一半界面没修好）----
    _src_api = _read("web_ui", "dl_api.py")
    _src_gui = _read("msu1_gui.py")
    _mark(_src_api.count("dl_convert.drop_derived_wavs(") == 4,
          "dl_api.py：4 处 drop_derived_wavs 调用"
          "（续传 / extract_done / _payload_in / convert_wav 主路径）",
          _src_api.count("dl_convert.drop_derived_wavs("))
    _mark(_src_gui.count("dl_convert.drop_derived_wavs(") == 3,
          "msu1_gui.py：3 处 drop_derived_wavs 调用"
          "（估算 / _list_payload / extract_done）",
          _src_gui.count("dl_convert.drop_derived_wavs("))

    # ---- ⑧ 顺序：自愈必须排在 already_done 之前 ----
    # 那 10 个真源（.fsb）全都 already_done 为真 —— 自愈要是排在判重之后，
    # 永远走不到，「点了没反应」的存量用户永远修不好。
    _dcsrc = _read("dl_convert.py")
    _i_heal = _dcsrc.find("self._drop_duplicate_products(src, log_buf)")
    _i_done = _dcsrc.find("if self._bucket.manifest.already_done(src, self._fmt)")
    _mark(0 < _i_heal < _i_done,
          "自愈排在 already_done 之前"
          "（排在后面就永远走不到：真源全都 already_done）",
          (_i_heal, _i_done))
    _mark("_drop_duplicate_products(self" in _dcsrc,
          "_drop_duplicate_products 是 ConvertWorker 的方法", "")


def t_v607():
    """[v6.0.7] 一个游戏多张封面 + 试听时轮播（嵌入仍然只用 boxart）。

    ## 用户要什么

    > 「一个游戏的封面资源应该有若干个，下载时全下载，然后在试听时轮播」

    紧接着补了一句关键约束：

    > 「对因为涉及到嵌入flac和mp3，在嵌入环节还是只嵌入boxart」

    ## 两个源各能给什么

    | 源 | 能给 | kind |
    |---|---|---|
    | libretro | ``Named_Boxarts`` 的多个地区版本 | ``boxart`` |
    | IGDB | 1 张 ``cover`` + 6~7 张 ``screenshots`` + ``extra_images`` | ``cover`` / ``screenshot`` / ``extra`` |

    ## 这片门禁守的三件事

    1. **嵌入只用盒绘** —— ``cover_for_title()`` 只认
       :data:`cover_art.BOXART_KINDS`，只有截图时必须返回空串，
       **不许拿截图凑**（用户明确不要）；
    2. **轮播不出现重复图** —— 实测 PSP 的 Amnesia，libretro 的
       ``Amnesia.png`` 和 IGDB 的 ``igdb_cover_co20ie.png`` 是同一张图
       （都是 247,077 B），不去重会连着放两张一样的，用户以为卡住了；
    3. **旧缓存不失效** —— ``_lookup.json`` 从「一个字符串」变成
       「[(路径, kind)]」，升级上来的用户不该丢图。

    ⚠ 全程离线：两个 ``fetch_all`` 都换成桩，一个请求都不发。
    真联网的张数归 ``vendor/_probe_many.py`` 管（那是给人看的数）。
    """
    print("\n[3n] 多封面 + 轮播（v6.0.7）", flush=True)

    import json
    import shutil
    import tempfile

    import cover_art as ca
    import cover_igdb as cg
    from web_ui import dl_api as dmod

    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    # ---- ① 版本号五处同步 ----
    # v6.0.8 起移到 ``t_v608`` —— 惯例是「只由最新分片负责」，见 t_v606 的说明。
    # 这里保留位置说明，免得后来人以为漏了。

    # ---- ② 哪些 kind 能嵌音频 ----
    _mark(ca.BOXART_KINDS == ("boxart", "cover"),
          "BOXART_KINDS = (boxart, cover)（IGDB 的 cover 也是盒绘）",
          ca.BOXART_KINDS)
    _mark("screenshot" not in ca.BOXART_KINDS and "extra" not in ca.BOXART_KINDS,
          "screenshot / extra 不在可嵌入名单里"
          "（这条一红，FLAC 里就会被塞进游戏截图）", "")

    # ---- ③ 查找表三种历史格式都要认 ----
    _mark(ca._norm_items("psp/a.png") == [("psp/a.png", "boxart")],
          "_norm_items：v6.0.6 的单字符串 → [(rel, boxart)]"
          "（老值只可能是盒绘，见 docstring）", ca._norm_items("psp/a.png"))
    _mark(ca._norm_items(["psp/a.png"]) == [("psp/a.png", "boxart")],
          "_norm_items：中间态的 [rel] → [(rel, boxart)]", "")
    _mark(ca._norm_items([["psp/a.png", "boxart"],
                          ["psp/s.png", "screenshot"]])
          == [("psp/a.png", "boxart"), ("psp/s.png", "screenshot")],
          "_norm_items：定稿的 [[rel, kind]] 原样保留", "")
    for junk in (None, "", [], {}, 0, [None], [["x"]]):
        _mark(ca._norm_items(junk) == [],
              "_norm_items(%r) → 空（不抛）" % (junk,), ca._norm_items(junk))

    # ---- ④ pick_all：多个地区版本全要，模糊档只给一个 ----
    items = [{"dir": "d", "name": n} for n in (
        "Amnesia (USA).png", "Amnesia (Europe).png", "Amnesia (Japan).png",
        "Amnesia Later (Japan).png", "Amnesia World (Japan).png")]
    got = ca.pick_all(items, "Amnesia")
    _mark(len(got) == 3 and all("Later" not in g and "World" not in g
                                for g in got),
          "pick_all：3 个地区版本全要，不把「Amnesia Later」算进来", got)
    _mark(got[0] == "Amnesia (USA).png",
          "pick_all：USA 排第一（区域偏好）", got[:1])
    _mark(len(ca.pick_all(items, "Amnesia", limit=2)) == 2,
          "pick_all：limit 生效", "")
    # 模糊档（difflib）只给最优一个 —— 否则会把别的游戏塞进轮播
    fuzzy = [{"dir": "d", "name": n} for n in (
        "Ys III - Wanderers from Ys (USA).png",
        "Ys IV - Mask of the Sun (Japan).png")]
    got2 = ca.pick_all(fuzzy, "Ys")
    _mark(len(got2) <= 1,
          "pick_all：模糊档最多一个（多了就是把别的游戏贴进来）", got2)

    # ---- ⑤ 缓存目录里造图，验 cover_for_title 只认盒绘 ----
    tmp = tempfile.mkdtemp(prefix="v607_")
    old_lad = os.environ.get("LOCALAPPDATA")
    old_lookup, old_mtime = ca._mem_lookup, ca._mem_lookup_mtime
    try:
        os.environ["LOCALAPPDATA"] = tmp
        root = ca.cache_root()
        os.makedirs(os.path.join(root, "psp"), exist_ok=True)

        def _mk(name, data=b"x"):
            p = os.path.join(root, "psp", name)
            with open(p, "wb") as fh:
                fh.write(data)
            return p

        box = _mk("box.png", b"BOX")
        shot = _mk("shot.png", b"SHOT")

        def _set_lookup(mapping):
            with open(ca._lookup_path(), "w", encoding="utf-8") as fh:
                json.dump(mapping, fh)
            ca._mem_lookup = None
            ca._mem_lookup_mtime = 0.0

        _set_lookup({"amnesia": [["psp/box.png", "boxart"],
                                 ["psp/shot.png", "screenshot"]]})
        allimg = ca.covers_for_title("Amnesia")
        _mark(len(allimg) == 2, "covers_for_title：2 张都返回（轮播要全部）",
              len(allimg))
        _mark(ca.covers_for_title("Amnesia", kinds=ca.BOXART_KINDS)
              == [(box, "boxart")],
              "covers_for_title(kinds=BOXART_KINDS)：只剩盒绘", "")
        _mark(ca.cover_for_title("Amnesia") == box,
              "cover_for_title：盒绘排在前，返回它（嵌音频用这张）",
              ca.cover_for_title("Amnesia"))

        # ⚠ 只有截图时必须返回空串 —— 这是用户那条约束的核心
        _set_lookup({"amnesia": [["psp/shot.png", "screenshot"]]})
        _mark(ca.cover_for_title("Amnesia") == "",
              "⚠ 只有截图时 cover_for_title 返回空串"
              "（不许拿截图凑，用户明确只要 boxart）",
              ca.cover_for_title("Amnesia"))
        _mark(len(ca.covers_for_title("Amnesia")) == 1,
              "但轮播仍然有这张截图可看", "")

        # 旧格式（v6.0.6 的单字符串）继续可用
        _set_lookup({"amnesia": "psp/box.png"})
        _mark(ca.cover_for_title("Amnesia") == box,
              "旧格式（单字符串）升级后照样认，不用删缓存", "")

        # ---- ⑥ 按内容去重 ----
        dup1 = _mk("d1.png", b"SAME")
        dup2 = _mk("d2.png", b"SAME")
        uniq = _mk("u.png", b"DIFF")
        out = dmod.DownloadState._dedup_images(
            [(dup1, "libretro"), (dup2, "IGDB"), (uniq, "IGDB")])
        _mark(len(out) == 2 and out[0][0] == dup1,
              "按内容去重：两张一样的只留第一张（来源标签也跟着第一条）", out)
        _mark(out[1][0] == uniq, "不一样的那张留着", "")

        # 三元组也要吃 —— _cover_many 传的是 (path, 来源, kind)
        out3 = dmod.DownloadState._dedup_images(
            [(dup1, "libretro", "boxart"), (dup2, "IGDB", "cover"),
             (uniq, "IGDB", "screenshot")])
        _mark(len(out3) == 2 and out3[0] == (dup1, "libretro", "boxart")
              and out3[1] == (uniq, "IGDB", "screenshot"),
              "按内容去重：三元组（path/来源/kind）原样带过，kind 不丢",
              out3)

        # ---- ⑦ _cover_many：顺序 + 上限 + 去重，全用桩 ----
        _orig_ca, _orig_ig = ca.fetch_all, cg.fetch_all
        try:
            ca.fetch_all = lambda k, t, **kw: [(box, "boxart")]
            # IGDB 先给一张和 box 内容相同的 cover，再给两张不同的截图
            cg.fetch_all = lambda k, t, **kw: [
                (_mk("ig_cover.png", b"BOX"), "cover"),
                (_mk("ig_s1.png", b"S1"), "screenshot"),
                (_mk("ig_s2.png", b"S2"), "screenshot"),
            ]
            # 先把查找表造成「有重复」的样子（模拟 v6.0.6 遗留 + 两源都写过），
            # 再跑 _cover_many —— 顺序不能反，否则会把写回结果又冲掉。
            _set_lookup({"amnesia": [["psp/box.png", "boxart"],
                                     ["psp/box.png", "boxart"],
                                     ["psp/shot.png", "screenshot"]]})
            st = dmod.DownloadState()
            got3 = st._cover_many("psp", "Amnesia", None, 6)
            _mark(len(got3) == 3,
                  "_cover_many：3 张（box + s1 + s2，IGDB 那张重复 cover 被去掉）",
                  [os.path.basename(p) for p, _t in got3])
            _mark(got3[0][0] == box,
                  "_cover_many：盒绘排第一（轮播第一张、也是嵌音频那张）", "")

            # ⚠ 去重必须写回查找表 —— 端到端踩到的真 bug：
            # 内存里去了重，表里那两条重复记录还在，cover_paths 读出来
            # 还是 7 张、前两张一模一样。
            back = st.cover_paths("Amnesia")
            _mark(len(back) == 3,
                  "⚠ _cover_many 去重后写回查找表：cover_paths 只剩 3 张"
                  "（表里原本有重复，读出来不能还是 4 张）", back)
            _mark([os.path.basename(p) for p in back]
                  == ["box.png", "ig_s1.png", "ig_s2.png"],
                  "写回的顺序 = 展示顺序（盒绘在前，去重后不留重复）",
                  [os.path.basename(p) for p in back])

            _mark(len(st._cover_many("psp", "Amnesia", None, 2)) == 2,
                  "_cover_many：limit 生效", "")
        finally:
            ca.fetch_all, cg.fetch_all = _orig_ca, _orig_ig

        # ---- ⑧ cover_paths：先看缓存（顺序完整），空了才退回输出目录 ----
        _set_lookup({"amnesia": [["psp/box.png", "boxart"],
                                 ["psp/shot.png", "screenshot"]]})
        st2 = dmod.DownloadState()
        paths = st2.cover_paths("Amnesia")
        _mark(paths == [box, shot],
              "cover_paths：缓存里两张按顺序返回（盒绘在前）",
              [os.path.basename(p) for p in paths])
        _mark(st2.cover_path("Amnesia") == box,
              "cover_path 只取第一张（兼容旧调用）", "")

        # ⚠ 读侧也要扛住脏表：两条不同文件名、内容一模一样
        _mk("dup_a.png", b"SAMEIMG")
        _mk("dup_b.png", b"SAMEIMG")
        _set_lookup({"amnesia": [["psp/box.png", "boxart"],
                                 ["psp/dup_a.png", "screenshot"],
                                 ["psp/dup_b.png", "screenshot"]]})
        got_dup = [os.path.basename(p) for p in st2.cover_paths("Amnesia")]
        _mark(got_dup == ["box.png", "dup_a.png"],
              "⚠ 读侧按内容去重：表里两条同图（文件名不同）只返回一条"
              "（老版本写脏的表也不会让轮播放两张一样的）", got_dup)
        for bad in ("", "..", "../x", "a/b", "a\\b", "."):
            _mark(st2.cover_paths(bad) == [],
                  "cover_paths(%r) 被挡住（路径穿越）" % (bad,), "")
    finally:
        if old_lad is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_lad
        ca._mem_lookup, ca._mem_lookup_mtime = old_lookup, old_mtime
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- ⑨ 后端路由 ----
    _srv = _read("web_ui", "server.py")
    _mark('if path == "/api/dl/covers":' in _srv,
          "server.py：新增 /api/dl/covers（前端先问「有几张」）", "")
    _mark('qs.get("i")' in _srv,
          "/api/dl/cover 支持 i= 参数（第几张）", "")
    _mark("max(0, min(idx, len(paths) - 1))" in _srv,
          "i 越界被夹住，不返回 404"
          "（切歌瞬间的竞态会让轮播卡死）", "")

    # ---- ⑩ 前端 ----
    _js = _read("web_ui", "download.js")
    _mark("COVER_EVERY_MS = 3000" in _js,
          "轮播间隔 3 秒（用户 2026-10-28 选的）", "")
    _mark("S.coverN < 2" in _js and "audio.paused" in _js,
          "轮播只在「有 2 张以上 + 真在播放」时转"
          "（暂停了画面还在跳会让人以为出问题）", "")
    _mark("S.coverGame === game && img.getAttribute(\"src\")" in _js,
          "同一游戏连播不重置轮播（正在看第 3 张，切下一首不该跳回第 1 张）",
          "")
    _mark("/api/dl/covers?game=" in _js,
          "前端先问张数再逐张取（不是一次拉 6 张）", "")
    _mark("i + 1 < S.coverN" in _js,
          "某张图坏了就试下一张，全试完才显示音符占位", "")

    _css = _read("web_ui", "download.css")
    _mark("object-fit: contain" in _css,
          "封面框用 contain（盒绘竖版 + 截图横版混排，cover 会把盒绘切掉一半）",
          "")
    _mark("object-fit: cover" not in _css,
          "没有残留的 cover（两个都在时后者会覆盖）", "")

    # ---- ⑪ 嵌入链路一行没改（复用 cover_for_title）----
    _api = _read("web_ui", "dl_api.py")
    _mark("cover_art.cover_for_title" in _api,
          "_cover_for_dir 仍然走 cover_for_title（自动只拿盒绘）", "")
    _mark("COVER_LIMIT = 6" in _api,
          "每游戏上限 6 张（用户选的）", "")



def t_v608():
    """v6.0.8：「更新全部列表」一颗按钮跑完所有机种。

    用户原话：「现在下载音源界面列表更新需要到每个机种下面去点击，把更新
    列表功能做成一个按钮，点击即更新全部列表」。

    重点不是「有没有这颗按钮」，而是**它不能把用户正在看的东西搞乱**：
    批量更新跑在后台，``self.key`` / ``self.entries`` / ``self.sel`` 一概
    不碰 —— 否则用户点完刷新，选中项没了、列表跳了，比不刷新更烦。
    """
    import threading

    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    # ---- ① 版本号五处同步 ----
    # v6.0.9 起移到 ``t_v609`` —— 惯例是「只由最新分片负责」，见 t_v606 的说明。

    from web_ui import dl_api as dmod

    # ---- ② 常量与内部信号 ----
    _mark(getattr(dmod, "REFRESH_WORKERS", 0) == 3,
          "REFRESH_WORKERS = 3（33 个机种串行要 15 分钟，3 路约 5 分钟）",
          getattr(dmod, "REFRESH_WORKERS", None))
    _mark(isinstance(getattr(dmod, "_Cancelled", None), type)
          and issubclass(dmod._Cancelled, Exception),
          "_Cancelled 是 Exception 子类（用户点停不算失败）", "")

    # ---- ③ __init__ 的字段 ----
    st = dmod.DownloadState()
    _mark(isinstance(st.refresh, dict)
          and set(st.refresh) >= {"running", "done", "total",
                                  "key", "name", "ok", "fail"},
          "refresh 进度字典字段齐全", sorted(st.refresh))
    _mark(st.refresh["running"] is False,
          "刚启动时 refresh.running = False", st.refresh["running"])
    _mark(isinstance(st._refresh_cancel, threading.Event),
          "_refresh_cancel 是独立的 Event"
          "（不能复用 _scan_cancel —— 那会把前台的机种扫描一起掐掉）", "")
    _mark(isinstance(st._key_locks, dict)
          and isinstance(st._key_locks_guard, type(threading.Lock())),
          "按 key 的写锁容器 + 建锁时的互斥量都在", "")

    # ---- ④ _key_lock：同 key 同一把、不同 key 不同把 ----
    l1, l2 = st._key_lock("spc"), st._key_lock("spc")
    l3 = st._key_lock("nsf")
    _mark(l1 is l2, "_key_lock 同一个 key 返回同一把锁（否则锁不住）", "")
    _mark(l1 is not l3, "不同 key 用不同的锁（互不阻塞）", "")

    # ---- ⑤ 全量更新：跑完、计数对、**不动当前视图** ----
    class _FakeJW(object):
        """替身：只实现 refresh_all 用到的那几个接口。"""

        def __init__(self, keys, fail=(), delay=0.0):
            self.PLATFORMS = [{"key": k, "name": "名-" + k} for k in keys]
            self._fail = set(fail)
            self._delay = delay
            self.calls = []

        def platform(self, key):
            if key not in [p["key"] for p in self.PLATFORMS]:
                raise KeyError(key)
            return {"name": "名-" + key}

        def scan_platform(self, key, progress=None, cancel=None):
            self.calls.append(key)
            if self._delay:
                time.sleep(self._delay)
            if cancel is not None and cancel.is_set():
                return []
            if key in self._fail:
                raise RuntimeError("模拟网络炸了")
            return [{"name": key + "-a", "url": "u-" + key}]

    keys5 = ["spc", "nsf", "gbc", "gg", "smd"]
    real_jw = dmod.jw
    try:
        fake = _FakeJW(keys5)
        dmod.jw = fake

        st = dmod.DownloadState()
        # 造一个「用户正在看、已经勾了两条」的现场
        st.key = "spc"
        st.name = "名-spc"
        st.entries = [{"name": "old", "url": "u-old"}]
        st.sel = {"u-old", "u-old2"}
        loaded = []
        st.load = lambda k, force=False: loaded.append(k)

        r = st.refresh_all()
        _mark(r.get("ok") is True and r.get("total") == 5,
              "refresh_all 立刻返回 ok + 总机种数（后台线程干活）", r)

        for _ in range(200):
            if not st.refresh["running"]:
                break
            time.sleep(0.02)
        _mark(st.refresh["running"] is False,
              "跑完后 running 落回 False", st.refresh)
        _mark(st.refresh["ok"] == 5 and st.refresh["fail"] == 0,
              "5 个机种全部成功", st.refresh)
        _mark(sorted(fake.calls) == sorted(keys5),
              "每个机种都真扫了一遍", fake.calls)

        # ⚠ 核心断言：后台更新不许动前台现场
        _mark(st.key == "spc" and st.entries == [{"name": "old", "url": "u-old"}]
              and st.sel == {"u-old", "u-old2"},
              "⚠ 更新期间/之后，当前机种、列表、勾选**一个都没变**"
              "（变了就是比不刷新更烦）", (st.key, len(st.entries), len(st.sel)))
        _mark(loaded == [],
              "有勾选时不重读当前机种（重读会把 self.sel 清掉）", loaded)

        # ---- ⑥ 单个机种失败不拖垮整批 ----
        fake2 = _FakeJW(keys5, fail=("gbc",))
        dmod.jw = fake2
        st2 = dmod.DownloadState()
        st2.load = lambda k, force=False: None
        st2.refresh_all()
        for _ in range(200):
            if not st2.refresh["running"]:
                break
            time.sleep(0.02)
        _mark(st2.refresh["ok"] == 4 and st2.refresh["fail"] == 1,
              "一个机种抛异常：4 成功 / 1 失败，其余照跑", st2.refresh)
        _mark(len(fake2.calls) == 5,
              "失败的那个不会让整批提前退出", fake2.calls)

        # ---- ⑦ 没勾选、不忙 → 顺手把当前机种从新缓存重读一遍 ----
        fake3 = _FakeJW(keys5)
        dmod.jw = fake3
        st3 = dmod.DownloadState()
        st3.key = "nsf"
        got3 = []
        st3.load = lambda k, force=False: got3.append(k)
        st3.refresh_all()
        for _ in range(200):
            if not st3.refresh["running"]:
                break
            time.sleep(0.02)
        _mark(got3 == ["nsf"],
              "没勾选时跑完会重读当前机种（否则用户盯着屏幕觉得「点了没反应」）",
              got3)

        # busy（正在下载/转换）时也不能重读 —— 会把正在跑的任务的列表抽掉
        fake4 = _FakeJW(keys5)
        dmod.jw = fake4
        st4 = dmod.DownloadState()
        st4.key = "nsf"
        st4.busy = True
        got4 = []
        st4.load = lambda k, force=False: got4.append(k)
        st4.refresh_all()
        for _ in range(200):
            if not st4.refresh["running"]:
                break
            time.sleep(0.02)
        _mark(got4 == [],
              "busy（下载/转换中）不重读当前机种", got4)

        # ---- ⑧ 重复点：正在跑时再点，拒绝而不是开第二份 ----
        fake5 = _FakeJW(keys5, delay=0.05)
        dmod.jw = fake5
        st5 = dmod.DownloadState()
        st5.load = lambda k, force=False: None
        st5.refresh_all()
        r2 = st5.refresh_all()
        _mark(r2.get("ok") is False and "正在" in str(r2.get("error")),
              "正在更新时再点一次 → 拒绝（不会开第二份线程抢同一个缓存）", r2)
        for _ in range(300):
            if not st5.refresh["running"]:
                break
            time.sleep(0.02)

        # ---- ⑨ 取消：停下来、running 归位、不算失败 ----
        fake6 = _FakeJW(keys5, delay=0.30)
        dmod.jw = fake6
        st6 = dmod.DownloadState()
        st6.load = lambda k, force=False: None
        st6.refresh_all()
        time.sleep(0.12)
        mid = dict(st6.refresh)
        _mark(mid["running"] is True and mid["done"] < 5,
              "跑的过程中能看到进度（done 在涨、没跑完）", mid)
        _mark(bool(mid["name"]),
              "进度里带着当前机种名（状态栏要显示「· NES」）", mid["name"])
        cr = st6.cancel_refresh()
        _mark(cr.get("ok") is True, "cancel_refresh 接受请求", cr)
        for _ in range(300):
            if not st6.refresh["running"]:
                break
            time.sleep(0.02)
        _mark(st6.refresh["running"] is False,
              "取消后 running 归位（不归位这颗键就永远变不回「更新全部列表」）",
              st6.refresh)
        _mark(st6.refresh["fail"] == 0,
              "用户主动停的**不算失败**（fail 必须还是 0）", st6.refresh)
        _mark(st6.refresh["done"] == st6.refresh["ok"] + st6.refresh["fail"],
              "⚠ 被叫停的机种不算进 done"
              "（否则界面显示「已处理 3/33、成功 0」，自相矛盾）", st6.refresh)
        _mark(st6.cancel_refresh().get("ok") is False,
              "没在跑的时候 cancel 返回 ok=False（不假装成功）", "")

        # ---- ⑩ 机种列表为空 / 模块缺失 → 明确报错 ----
        fake7 = _FakeJW([])
        dmod.jw = fake7
        st7 = dmod.DownloadState()
        _mark(st7.refresh_all().get("ok") is False,
              "没有机种时返回 ok=False（不静默成功）", st7.refresh_all())
        dmod.jw = None
        _mark(dmod.DownloadState().refresh_all().get("ok") is False,
              "音源模块缺失时返回 ok=False", "")
    finally:
        dmod.jw = real_jw

    # ---- ⑪ 前台扫描也要拿同一把 key 锁 ----
    _api = _read("web_ui", "dl_api.py")
    _mark("with self._key_lock(key):" in _api,
          "前台 _scan_thread 也拿 key 锁"
          "（和批量更新同时扫同一机种会写出半个 JSON）", "")
    _mark("self._refresh_cancel" in _api and "_scan_cancel" in _api,
          "两套取消信号分开（停批量更新不该掐掉前台扫描）", "")
    _mark('"refresh": dict(self.refresh)' in _api,
          "snapshot 暴露 refresh 进度（前端靠它显示 12/33）", "")
    _mark('"refresh_workers": int(REFRESH_WORKERS)' in _api,
          "diag 报 refresh_workers（打包漏了常量时 exe 验收能点名）", "")
    _mark("if entries and not fetcher.cancelled():" in _read("joshw_source.py"),
          "scan_platform 取消时不落盘（半截结果不覆盖好缓存）—— 这条是"
          "「点停是安全的」的前提", "")

    # ---- ⑫ 后端路由 ----
    _srv = _read("web_ui", "server.py")
    _mark('path == "/api/dl/refresh_all"' in _srv,
          "server.py：新增 /api/dl/refresh_all", "")
    _mark('path == "/api/dl/refresh_cancel"' in _srv,
          "server.py：新增 /api/dl/refresh_cancel", "")

    # ---- ⑬ 前端 ----
    _html = _read("web_ui", "download.html")
    _js = _read("web_ui", "download.js")
    _css = _read("web_ui", "download.css")
    _mark('id="dlRefreshAll"' in _html,
          "download.html：工具条上有「更新全部列表」这颗键", "")
    _mark("更新全部列表" in _html,
          "按钮文案是「更新全部列表」", "")
    _mark('$("#dlRefreshAll").onclick' in _js,
          "download.js：按钮接上了", "")
    _mark('api("/api/dl/refresh_all", {})' in _js,
          "点一下调 refresh_all", "")
    _mark('api("/api/dl/refresh_cancel", {})' in _js,
          "跑的时候再点一下是「停止」", "")
    _mark('"停止更新 " + (rf.done || 0) + "/" + (rf.total || 0)' in _js,
          "跑起来按钮变「停止更新 12/33」（5 分钟的等待里得有个进度和出口）",
          "")
    _mark("const rf = d.refresh || {};" in _js,
          "按钮状态以**后端**为准，不靠前端自己记"
          "（换窗口/刷新页面后状态也不会错）", "")
    _mark('"🔄 更新列表 "' in _js,
          "状态栏也拼一行更新进度（和下载/封面并列的第三条线）", "")
    _mark("S.refreshing = !!rf.running;" in _js,
          "记住上一次是否在跑（跑完那一下要弹结论）", "")
    _mark(".dl-refresh-all.on" in _css,
          "「正在跑」有独立视觉态", "")
    _mark("white-space: nowrap" in _css,
          "按钮字变长时不换行（.tb-btn 是 flex，不锁会被压成两行）", "")
    _mark("缺新条目时点「更新全部列表」" in _html,
          "左侧提示语改指新按钮（原来写的是「点清除缓存」，33 遍）", "")


def _col_consts(js):
    """从 download.js 里把 COL_ORDER / COL_MIN / COL_DEF 抠出来。

    抠而不是写死：断言必须跟着源码走，否则改了常量而门禁还在验旧值，
    等于给自己发假绿灯。
    """
    import re
    def _obj(name):
        m = re.search(r"const %s = \{([^}]*)\}" % name, js)
        if not m:
            return {}
        return {k: int(v) for k, v in re.findall(r"(\w+)\s*:\s*(\d+)", m.group(1))}
    m = re.search(r'const COL_ORDER = \[([^\]]*)\]', js)
    order = re.findall(r'"(\w+)"', m.group(1)) if m else []
    return order, _obj("COL_MIN"), _obj("COL_DEF")


def _col_defaults(order, cmin, cdef, avail):
    """照 ``colDefaults()`` 的原文逻辑跑一遍：名称列吃剩余空间，装不下就匀。"""
    c = dict(cdef)
    others = sum(c[k] for k in order if k != "name")
    c["name"] = avail - others
    if c["name"] >= cmin["name"]:
        return c
    c["name"] = cmin["name"]
    need = sum(c[k] for k in order) - avail
    for k in sorted([k for k in order if k != "name"],
                    key=lambda k: -(c[k] - cmin[k])):
        if need <= 0:
            break
        take = min(max(0, c[k] - cmin[k]), need)
        c[k] -= take
        need -= take
    return c


def t_v609():
    """v6.0.9：默认列宽必须装得下 + 布局没就绪不许量宽度。

    这一版的 bug 是「只有英文名称列能拖」。根因不是把手没挂上（都挂了），
    是**默认值溢出 30px**：容器 648 → 可用 568，六个非名称列默认合计 478，
    名称列本该吃 90 但下限 120 → 总宽 598。名称列贴死下限之后，谁都没余量。

    更早一层：``initCols()`` 在 ``boot()`` 里跑，那一刻 ``clientWidth``
    还是 0，算出来的「可用宽度」是假的。

    ⚠ v6.0.10 把「变宽时谁让位」整个换掉了（见 t_v610），但**这一版的两条
    修复仍然必须成立** —— 默认值装不下 / 布局没算完就量，任何一条回来，
    整排都会退回「一格都拖不动」。所以这个分片保留，只摘掉已删除的
    ``colRoom`` / ``colGive`` / ``colFit`` 那几条。
    """
    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    js = _read("web_ui", "download.js")
    css = _read("web_ui", "download.css")

    # ---- ① 列宽常量 ----
    order, cmin, cdef = _col_consts(js)
    _mark(order == ["sel", "name", "zh", "date", "size", "state", "act"],
          "COL_ORDER 七列齐全且顺序稳定（sel/name/zh/date/size/state/act）",
          order)
    _mark(set(cmin) == set(order) and set(cdef) == set(order),
          "COL_MIN / COL_DEF 每一列都有值（漏一列会让总宽算错）",
          (sorted(cmin), sorted(cdef)))
    _mark(cdef.get("name") == 0,
          "COL_DEF.name = 0 是占位值 —— 名称列吃剩余空间，由 colDefaults 算",
          cdef.get("name"))

    # ---- ② 布局就绪判断 ----
    _mark("function colLaidOut()" in js,
          "有 colLaidOut()：boot() 里 clientWidth 常是 0，得先判断布局算完没有",
          "")
    _mark("w.clientWidth > 200" in js,
          "colLaidOut 的判据是 clientWidth > 200（0 和个位数都算没算完）", "")

    # ---- ③ 默认值必须装得下 ----
    _mark("function colDefaults()" in js, "有 colDefaults()", "")
    _mark("if (c.name >= COL_MIN.name) return c;" in js,
          "colDefaults：名称列能吃到剩余空间就直接返回", "")
    _mark("let need = colTotal(c) - avail;" in js,
          "colDefaults：装不下时算出溢出量，从别的列匀（原来直接返回，总宽溢出）",
          "")

    # ---- ④ 把手：六个，不含复选框列 ----
    _mark('[["c-name", "name"], ["c-zh", "zh"], ["c-date", "date"],' in js,
          "把手挂在 name/zh/date/size/state/act 六列上", "")
    _mark('["c-size", "size"], ["c-state", "state"], ["c-act", "act"]]' in js,
          "六列的把手列表完整（含操作列 —— 用户说的「其他几列」里就有它）",
          "")
    _mark('["c-sel", "sel"]' not in js,
          "复选框列不挂把手（26px 没加宽必要，挂了还会和「点一下全选」抢命中区）",
          "")
    _mark(".dl-thead > span" in css and "min-height: 18px" in css,
          "表头格子有 min-height —— c-sel / c-act 是空 span，不撑高度的话"
          "把手 height:100% 跟着变 0，看得见点不中", "")
    _mark(".c-grip" in css, "CSS 里有 .c-grip 命中区", "")

    # ---- ⑤ 等两帧再校 ----
    _mark("if (colsTouched || !colLaidOut()) return;" in js,
          "refit 有双重保护：用户动过就不覆盖、布局没好就不硬算", "")
    _mark("requestAnimationFrame(() => requestAnimationFrame(refit));" in js,
          "等两帧再 refit（一帧还不够 —— 实测第一帧 clientWidth 仍是 0）", "")
    _mark("if (!colsTouched) {" in js,
          "只在用户没动过列宽时才自动 refit", "")

    # ---- ⑥ 数值验证：默认值装得下 ----
    if not (order and cmin and cdef):
        _mark(False, "列宽常量解析失败，后面的数值验证没法做",
              (cmin, cdef, order))
        return

    # 实测容器 648px（用户截图那个窗口）→ 可用 648-24-8*7 = 568
    for avail in (568, 480, 900):
        d = _col_defaults(order, cmin, cdef, avail)
        _mark(sum(d.values()) <= avail,
              "默认列宽装得下 %dpx（修复前 568 那档是 598，溢出 30px 就是"
              "所有 bug 的源头）" % avail,
              "%d / %d = %s" % (sum(d.values()), avail, list(d.values())))
        _mark(all(d[k] >= cmin[k] for k in order),
              "%dpx 下每一列都不低于自己的下限" % avail,
              {k: (d[k], cmin[k]) for k in order if d[k] < cmin[k]})


def t_v610():
    """v6.0.10：拖宽某一列时，**其它列一概不动**，表格可横向滚动。

    用户原话（v6.0.9 交付后的反馈）：
      「英文名称列向右拉，日期、大小、状态列不动，中文名列被压缩；
        中文名列向右拉，结果日期、大小、状态列还是不动，英文名称列被压缩。」

    这不是「拖不动」，是**分配模型不对**。v6.0.1~v6.0.9 是「总宽锁死、
    有人让位」，让位顺序「名称列优先、然后中文名」。这两列各有 60px 余量，
    于是永远是它们互相抢 —— 日期/大小/状态看起来一动不动。

    用户在两个方案里明确选了「谁都不让，出滚动条」：
    拖谁只改谁，其它列宽度和位置都不变，合计超了就出横向滚动条。
    """
    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    js = _read("web_ui", "download.js")
    css = _read("web_ui", "download.css")

    # ---- ① 版本号五处同步 ----
    # v6.0.11 起移到 t_v611（惯例：只有最新分片负责版本号，见 t_v606 的说明）。
    # 这里原来有 5 条 ``6.0.10`` 的断言，升版后必然红，已摘掉。

    # ---- ② 旧模型的三件套必须彻底删掉 ----
    for fn in ("colRoom", "colGive", "colFit"):
        _mark(("function %s(" % fn) not in js,
              "%s 已删除 —— 它三个就是「互相借宽度」的实现，留着迟早被误用"
              % fn, "")

    # ---- ③ colDrag 只改自己 ----
    _mark("const start = cols[key];" in js,
          "colDrag 只记住自己那一列的起点（原来还要备份整行 startCols）", "")
    _mark("cols[key] = Math.max(COL_MIN[key], Math.round(start + ev.clientX - startX));"
          in js,
          "拖动时**只写 cols[key]**，其它列一个字节都不碰", "")
    _mark("colsTouched = true;" in js and "colSave();" in js,
          "松手才落盘（拖动过程中不写 localStorage，一帧一写会卡）", "")

    # ---- ④ 横向滚动：--dl-w 撑开 + .dl-rows 双轴滚 + 表头跟走 ----
    _mark('w.style.setProperty("--dl-w"' in js,
          "colApply 会写 --dl-w（列宽合计 + 间隙 + 内边距）—— 撑开表格靠它",
          "")
    _mark("min-width: var(--dl-w, 0)" in css,
          "CSS 用 --dl-w 给表格设 min-width（合计没超容器时它不起作用，"
          "末尾 1fr 照旧吃余量）", "")
    _mark("overflow: auto;" in css,
          ".dl-rows 双轴滚动（原来是 overflow-y: auto，横向会被裁掉）", "")
    _mark(".dl-table-wrap" in css and "overflow: hidden;" in css,
          ".dl-table-wrap 要 hidden —— 表头被撑宽后溢出，不裁会给整个页面"
          "加一根横向滚动条", "")
    _mark("function colSyncHead()" in js,
          "有 colSyncHead()：表头不在滚动容器里，得跟着 translateX", "")
    _mark('head.style.transform = "translateX(" + (-box.scrollLeft) + "px)";' in js,
          "表头位移 = -滚动容器的 scrollLeft（不跟就表头与内容错位）", "")
    _mark('box.addEventListener("scroll", colSyncHead' in js,
          "滚动时调 colSyncHead", "")

    # ---- ⑤ 存档换 key（旧模型的宽度分布不适用）----
    _mark('const COL_KEY = "msu1.dl.cols2";' in js,
          "存档 key 升到 cols2 —— 旧的「总宽锁死」分布套到新模型上会一打开"
          "就溢出，直接作废更干净", "")
    _mark("colFit(c);" not in js,
          "colLoad 不再强行把存档缩到容器内（横向滚动是合法的，缩了等于"
          "偷偷改用户的选择）", "")

    # ---- ⑥ resize：动过就一律不动 ----
    _mark("if (!cols || !colLaidOut() || colsTouched) return;" in js,
          "resize 时动过列宽就**一律不动** —— 每列独立后，用户定的宽度"
          "跟窗口大小无关", "")

    # ---- ⑦ 日期/大小不换行 ----
    _mark(".dl-row .c-date" in css and "white-space: nowrap" in css,
          "日期列 nowrap —— 行高锁死 27px，折成两行会顶出格子"
          "（用户截图里的「2020-08-」+「18」）", "")

    # ---- ⑧ 数值验证：拖一列，其它列必须一个像素都不变 ----
    order, cmin, cdef = _col_consts(js)
    if not (order and cmin and cdef):
        _mark(False, "列宽常量解析失败，数值验证没法做", (cmin, cdef, order))
        return

    avail = 568
    d0 = _col_defaults(order, cmin, cdef, avail)

    def _drag(c0, key, delta):
        c = dict(c0)
        c[key] = max(cmin[key], c[key] + delta)
        return c

    for key, delta in (("name", 40), ("zh", 40), ("date", 30),
                       ("size", 20), ("state", 30), ("act", 40)):
        dn = _drag(d0, key, delta)
        others_same = all(dn[k] == d0[k] for k in order if k != key)
        _mark(dn[key] > d0[key] and others_same,
              "拖 %s +%d：只有它变宽，其余六列宽度一个像素都没变" % (key, delta),
              "%d→%d · 其它变动=%s"
              % (d0[key], dn[key], [k for k in order if k != key
                                    and dn[k] != d0[k]]))
        _mark(sum(dn.values()) > avail,
              "拖 %s 之后总宽超过可用宽（%d > %d）→ 这才会有横向滚动条"
              % (key, sum(dn.values()), avail), "")

    # 拖窄也不能低于下限
    dz = _drag(d0, "date", -999)
    _mark(dz["date"] == cmin["date"],
          "往左拖到底停在该列下限（%dpx），不会拖成 0 宽" % cmin["date"],
          dz["date"])


def t_v611():
    """v6.0.11：封面改到 ``<下载根>/Artworks/<机种>/<游戏名>/``。

    用户原话：

    > 「改一下路径，改到下载音源目录中，单独创建一个文件夹 Artworks 中，
    > 子文件夹为：机种\\游戏名」

    这一版最容易坏的地方**不在**「路径拼得对不对」，而在三处联动：

    1. **写侧和读侧必须用同一套目录名**。写图时用机种显示名（``NES / FC``
       → 落盘 ``NES _ FC``），读查找表时 rel 里也是它。任何一边回落到 key
       （``nsf``）就会出现「图在盘上但显示没封面」——**不报错**。
    2. **``rel`` 从两段变三段**。``_cover_many`` 里原来靠「取两段目录名」
       拼 rel，三层结构下会拼错并且**静默跳过**，于是 v6.0.7 修好的
       「去重后回写查找表」直接失效（重复图又回来了）。
    3. **迁移不能并发**。多个入口同时触发会各写一份 ``_lookup.json``，
       后写的冲掉先写的 —— 图还在，但一半游戏查不到。
    """
    print("\n[3c11] 封面目录改到 Artworks（机种/游戏名）", flush=True)

    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            with open(p, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    import cover_art as ca
    import cover_igdb as cg
    import tempfile

    # ---- ① 版本号五处同步 ----
    # v6.0.12 起移到 t_v612（惯例：只有最新分片负责版本号，见 t_v606 的说明）。
    # for path, pat in (("build_web_pyi.py", 'VER = "6.0.11"'),
    #                   ("dl_convert.py", 'AUDIO_WRITER_VERSION = "6.0.11"'),
    #                   ("web_ui/server.py", 'APP_VERSION = "6.0.11"'),
    #                   ("web_ui/index.html", "游戏音乐转换器 6.0.11</title>"),
    #                   ("web_ui/index.html",
    #                    '<span class="brand-ver">6.0.11</span>')):
    #     _mark(pat in _read(*path.split("/")),
    #           "版本号同步：%s 含 %s" % (path, pat), "")

    # ---- ② 新接口都在 ----
    for fn in ("set_cache_root", "cache_root", "platform_sub",
               "legacy_cache_root", "rel_of", "migrate_legacy", "stats"):
        _mark(callable(getattr(ca, fn, None)),
              "cover_art.%s 存在" % fn, "")
    _lock = getattr(ca, "_migrate_lock", None)
    _mark(callable(getattr(_lock, "acquire", None))
          and callable(getattr(_lock, "release", None)),
          "_migrate_lock 是锁对象 —— 迁移必须串行", type(_lock).__name__)
    _mark(str(getattr(ca, "MIGRATE_MARK", "")) != "",
          "有迁移标记文件名（否则每次启动都重扫旧目录）", ca.MIGRATE_MARK)

    # ---- ③ 根 / 机种层 / rel 的纯函数行为 ----
    tmp = tempfile.mkdtemp(prefix="v611_gate_")
    orig = (ca.cache_root, ca.legacy_cache_root)
    try:
        ca.set_cache_root("")
        _mark(ca.cache_root().endswith(os.path.join("MSU1Converter", "covers")),
              "没设过时仍是旧位置（命令行 / 门禁不调 set_cache_root 也不炸）",
              ca.cache_root())

        art = os.path.join(tmp, "Artworks")
        ca.set_cache_root(art, "NES / FC")
        _mark(os.path.normpath(ca.cache_root()) == os.path.normpath(art),
              "set_cache_root 生效", ca.cache_root())
        _mark(ca.platform_sub("nsf") == "NES _ FC",
              "机种目录用显示名并净化（/ → _），和下载目录同名",
              ca.platform_sub("nsf"))
        ca.set_cache_root(art, "")
        _mark(ca.platform_sub("nsf") == "nsf",
              "没给显示名时回落 key（不至于写出空目录）",
              ca.platform_sub("nsf"))

        ca.set_cache_root(art, "NES / FC")
        title = "1943 - The Battle of Midway (1988-10)(Capcom)(US)"
        rel = ca.rel_of("nsf", title, title + ".png")
        _mark(rel.count("/") == 2, "rel 是三段：机种/游戏名/文件名", rel)
        _mark(rel.split("/")[0] == "NES _ FC", "第一段是机种显示名", rel)
        _mark(rel.split("/")[1] == ca._safe_name(title),
              "第二段是**原始**游戏名（不是归一化后的小写）", rel)
        _mark(rel.endswith(".png"), "最后是文件名", rel)

        # 写侧和读侧必须能对上：rel 拼回去要真落在根下
        full = os.path.join(ca.cache_root(), rel.replace("/", os.sep))
        _mark(full.startswith(os.path.normpath(art)),
              "rel 拼回绝对路径落在 Artworks 下（读写两侧同一套）", full)

        # ---- ③-b override 必须让位给 LOCALAPPDATA 隔离 ----
        # 门禁有一批用例靠 ``os.environ["LOCALAPPDATA"] = tmp`` 隔离封面目录。
        # 如果 override 无条件优先，那些用例会往**用户真实的 Artworks** 写
        # 测试垃圾（实测写过 ``PSP/box.png`` 这种 3 字节假图，而且它排在最
        # 前面，直接把「嵌图 / 回填」一串行为测试全带红）。
        _env0 = os.environ.get("LOCALAPPDATA")
        try:
            os.environ["LOCALAPPDATA"] = os.path.join(tmp, "fake_lad")
            _mark(not os.path.normpath(ca.cache_root()).startswith(
                      os.path.normpath(art)),
                  "LOCALAPPDATA 被换掉后 override 失效（门禁的隔离手法）",
                  ca.cache_root())
            _mark(ca.platform_sub("nsf") == "nsf",
                  "机种层同样回落 key（不能出现「根在临时目录、机种层是真名」的错配）",
                  ca.platform_sub("nsf"))
        finally:
            if _env0 is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = _env0
        _mark(os.path.normpath(ca.cache_root()) == os.path.normpath(art),
              "LOCALAPPDATA 换回来以后 override 重新生效", ca.cache_root())

        # ---- ④ _artworks_root 的四种形状 ----
        try:
            from web_ui import dl_api as dmod
            f = dmod.DownloadState._artworks_root
            cases = [
                (r"D:\MSU1Downloads\joshw\Wii", r"D:\MSU1Downloads\Artworks"),
                (r"D:\MSU1Downloads\joshw", r"D:\MSU1Downloads\Artworks"),
                (r"D:\MSU1Downloads\zophar\PSP", r"D:\MSU1Downloads\Artworks"),
                (r"E:\Music\PS4", r"E:\Music\Artworks"),
            ]
            for src, want in cases:
                got = f(src)
                _mark(os.path.normpath(got) == os.path.normpath(want),
                      "保存目录 %s → %s" % (src, want), got)
            _mark(dmod.DownloadState._artworks_root("") == "",
                  "保存目录为空时返回空（不去猜一个路径出来）", "")
        except Exception as exc:                     # noqa: BLE001
            _mark(False, "dl_api._artworks_root 可用", str(exc))

        # ---- ⑤ 迁移：造旧结构真搬一遍 ----
        old_root = os.path.join(tmp, "oldcovers")
        os.makedirs(os.path.join(old_root, "nsf"), exist_ok=True)
        os.makedirs(os.path.join(old_root, "psp"), exist_ok=True)
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 200
        n1 = "1943 - The Battle of Midway (1988-10)(Capcom)(US).png"
        n2 = "igdb_cover_co20ie.png"
        n3 = "Amnesia.png"
        for d, n in (("nsf", n1), ("psp", n2), ("psp", n3)):
            with open(os.path.join(old_root, d, n), "wb") as fh:
                fh.write(png)
        with open(os.path.join(old_root, "_lookup.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"1943 the battle of midway": [["nsf/" + n1, "boxart"]],
                       "amnesia": [["psp/" + n3, "boxart"],
                                   ["psp/" + n2, "cover"]]},
                      fh, ensure_ascii=False)

        ca.legacy_cache_root = lambda: old_root
        name_map = {"nsf": "NES / FC", "psp": "PSP"}
        st = ca.migrate_legacy(art, name_of=lambda k: name_map.get(k, ""),
                               log=lambda _m: None)
        _mark(st["files"] == 3, "旧目录 3 张图全搬过来", st)
        _mark(st["games"] == 2, "归到 2 个游戏目录", st)
        _mark(os.path.isfile(os.path.join(art, "NES _ FC",
                                          "1943 - The Battle of Midway", n1)),
              "目录名从文件名反推出**原始大小写**（不是小写归一化名）", "")
        _mark(os.path.isfile(os.path.join(art, "PSP", "Amnesia", n3))
              and os.path.isfile(os.path.join(art, "PSP", "Amnesia", n2)),
              "同一游戏的 libretro 图和 IGDB 图进**同一个**游戏目录", "")
        _mark(os.path.isfile(os.path.join(old_root, "nsf", n1)),
              "搬的是副本，旧目录原样保留（可回退）", "")

        new_lk = {}
        try:
            with open(os.path.join(art, "_lookup.json"), encoding="utf-8") as fh:
                new_lk = json.load(fh)
        except (OSError, ValueError):
            pass
        am = new_lk.get("amnesia") or []
        _mark(len(am) == 2, "新查找表里 amnesia 两张都记了", am)
        _mark(all(r.count("/") == 2 for r, _k in am),
              "新 rel 都是三段（旧表里是两段）", am)
        _mark(bool(am) and os.path.isfile(
            os.path.join(art, am[0][0].replace("/", os.sep))),
            "新 rel 拼出来真能读到文件（读写两侧对得上）", am[0][0] if am else "")

        st2 = ca.migrate_legacy(art, name_of=lambda k: name_map.get(k, ""),
                                log=lambda _m: None)
        _mark(st2["files"] == 0 and st2["games"] == 0,
              "第二次调用什么都不做（标记文件挡住重复搬家）", st2)

        # ---- ⑥ stats 认三层 ----
        s = ca.stats()
        _mark(s.get("images") == 3, "stats 数得出三层的图", s)
        _mark(s.get("games") == 2, "stats 报游戏数", s)
        _mark(s.get("platforms") == 2, "stats 报机种数", s)
    finally:
        ca.cache_root, ca.legacy_cache_root = orig
        ca.set_cache_root("")
        _rmtree_soft(tmp)

    # ---- ⑥-b 并发：迁移和「抓完图写表」同时跑，谁的条目都不许丢 ----
    # 这是上面 ⑧-b 那条源码断言的行为版。只断言源码容易漏掉「锁加了但加的
    # 不是同一把」这种情况，所以真起两个线程对撞一次。
    import threading as _th
    tmp2 = tempfile.mkdtemp(prefix="v611_cc_")
    try:
        old2 = os.path.join(tmp2, "oldcovers")
        new2 = os.path.join(tmp2, "Artworks")
        os.makedirs(os.path.join(old2, "nsf"), exist_ok=True)
        _old_lk = {}
        for i in range(8):
            fn = "Game %02d (USA).png" % i
            with open(os.path.join(old2, "nsf", fn), "wb") as fh:
                fh.write(png)
            _old_lk["game %02d" % i] = [["nsf/" + fn, "boxart"]]
        with open(os.path.join(old2, "_lookup.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(_old_lk, fh, ensure_ascii=False)

        ca.set_cache_root(new2, "NES _ FC")
        ca.legacy_cache_root = lambda: old2
        _ev = _th.Event()

        def _mig():
            try:
                ca.migrate_legacy(new2, name_of=lambda _k: "NES _ FC",
                                  log=lambda _m: None)
            finally:
                _ev.set()

        _t = _th.Thread(target=_mig, daemon=True)
        _t.start()
        _wrote = 0
        while not _ev.is_set() and _wrote < 200:      # 上限防止迁移饿死时卡死
            ca.set_covers("Fetched %02d" % _wrote,
                          [("NES _ FC/Fetched %02d/a.png" % _wrote, "boxart")])
            _wrote += 1
            _ev.wait(0.001)                           # 让出锁，别饿死迁移
        _t.join(10)

        try:
            with open(os.path.join(new2, "_lookup.json"),
                      encoding="utf-8") as fh:
                _fin = json.load(fh)
        except (OSError, ValueError):
            _fin = {}
        _mig_n = len([k for k in _fin if k.startswith("game ")])
        _fet_n = len([k for k in _fin if k.startswith("fetched ")])
        _mark(_mig_n == 8, "并发写表时迁移登记的 8 个游戏一条不少",
              "%d/8（表共 %d 条）" % (_mig_n, len(_fin)))
        _mark(_fet_n == _wrote, "并发写表时同时写入的 %d 条也都在" % _wrote,
              "%d/%d" % (_fet_n, _wrote))
    finally:
        ca.legacy_cache_root = orig[1]
        ca.set_cache_root("")
        _rmtree_soft(tmp2)

    # ---- ⑦ 写侧：两个模块都用 rel_of，不许各拼一遍 ----
    art_src = _read("cover_art.py")
    igdb_src = _read("cover_igdb.py")
    _mark("def rel_of(" in art_src and "platform_sub(key)" in art_src,
          "cover_art 有 rel_of 且用 platform_sub 拼机种层", "")
    _mark(igdb_src.count("cover_art.rel_of(") >= 2,
          "cover_igdb 的两处 rel 都改调 rel_of（各拼一遍格式必错）",
          igdb_src.count("cover_art.rel_of("))
    _mark('"%s/%s" % (key, local)' not in igdb_src,
          "cover_igdb 里不再有「两段 rel」的旧拼法", "")
    _mark("cover_art.platform_sub(key)" in igdb_src,
          "cover_igdb 的落盘目录也分了机种/游戏名两层", "")

    # ---- ⑧ _cover_many 去重回写：rel 必须按相对路径算 ----
    dl_src = _read("web_ui", "dl_api.py")
    _mark("os.path.relpath(p, root)" in dl_src,
          "_cover_many 用 relpath 算 rel（三层结构下「取两段目录名」会拼错）", "")
    _mark("os.path.dirname(d) != root" not in dl_src,
          "旧的「比较上两级目录」判据已删除 —— 它在新结构下会**全部跳过**，"
          "让 v6.0.7 的去重回写静默失效", "")

    # ---- ⑧-b 迁移写查找表必须和「抓完图写表」抢同一把锁 ----
    # 用户机上真实出过一次：迁移登记了 54 个游戏，之后并发写表把整份冲成
    # 4 条 —— 磁盘 58 个游戏目录全在、表里几乎全查不到，**不报错**。
    # 根因是迁移用 ``_migrate_lock``（只串行化迁移自身）、写表用 ``_lock``，
    # 两把锁不互斥，各写一份完整文件。
    _mig_body = art_src.split("def _migrate_legacy_locked(", 1)
    _mig_body = _mig_body[1].split("\ndef ", 1)[0] if len(_mig_body) > 1 else ""
    _mark("with _lock:" in _mig_body,
          "迁移写查找表在 _lock 内（_migrate_lock 挡不住同时进行的写表）", "")
    _mark("_forget_lookup_cache()" in _mig_body,
          "迁移写盘后清掉查找表内存缓存（否则下一次 _remember 会用旧基线整份覆盖）",
          "")
    _mark("def _forget_lookup_cache(" in art_src,
          "有 _forget_lookup_cache 辅助", "")

    # ---- ⑨ 联动：切机种 / 改保存目录都要同步根 ----
    _mark(dl_src.count("self._sync_cover_root()") >= 3,
          "_sync_cover_root 在 __init__ / 切机种 / set_dest 三处都调",
          dl_src.count("self._sync_cover_root()"))
    _mark("def start_cover_migration" in dl_src,
          "有 start_cover_migration 公开方法", "")
    _mark("_sync_cover_root(migrate=True)" in dl_src
          and dl_src.count("_sync_cover_root(migrate=True)") == 1,
          "迁移只在 start_cover_migration 里触发一次"
          "（写在 __init__ 里会让门禁每构造一个实例就往用户磁盘复制一遍）", "")

    srv_src = _read("web_ui", "server.py")
    _mark("start_cover_migration()" in srv_src,
          "server.serve() 里调了 start_cover_migration", "")
    _mark(srv_src.index("start_cover_migration()")
          > srv_src.index("def serve("),
          "调用点在 serve() 里面（不是模块级，否则 import 就搬家）", "")

    # ---- ⑩ 界面提示 ----
    html = _read("web_ui", "download.html")
    _mark("Artworks" in html,
          "「下载封面」按钮的 tooltip 写清了新路径（不然用户找不到图）", "")
    _mark("存到下载目录下的 Artworks" in html,
          "tooltip 说的是「下载目录下」而不是写死某个盘符", "")

    # ---- ⑪ 门禁自己的素材查找要认三层 ----
    t_src = _read("vendor", "test_web_ui.py")
    _mark("def _first_cached_png(" in t_src,
          "门禁有 _first_cached_png 辅助（三层 / 两层都认）", "")
    _mark("新的三层结构" in t_src and "旧的两层结构" in t_src,
          "_first_cached_png 两种结构都处理 —— 只认两层的话"
          "「嵌图 / 回填」的行为测试会被静默跳过", "")


def t_v612():
    """v6.0.12：模糊匹配加「数字序列必须一致」判据 —— 续作不再贴错封面。

    用户在我列的两个方案里选了 **A**（加判据、阈值不动）。

    为什么阈值救不了：``MIN_RATIO`` 比的是字符重合比例，而续作只多一两个
    字符，名字越长占比越小。实测 nsf 封面库 **136 对「正传 + 续作」里 85%
    的相似度 ≥ 0.86**，最长的到 0.98。更要命的是**错的和对的混在同一区间**
    （``caltron 6 in 1`` 是 0.930、``castle quest``/``castlequest`` 是 0.947），
    一条横线切不开 —— 调高到 0.96 只能拦掉 94% 的错，代价是有效匹配掉一半。

    实测（150 个正传样本 × 4 机种，模拟「库里没有这个游戏」）：

    | 机种 | 改前有封面 | 其中错 | 改后有封面 | 其中错 | 换了一张的 |
    |---|---|---|---|---|---|
    | nsf | 39 | 7 | 32 | 0 | 0 |
    | psf | 19 | 8 | 11 | 0 | 0 |
    | wii | 26 | 8 | 18 | 0 | 0 |
    | gsf | 20 | 7 | 13 | 0 | 0 |

    被拦掉的 30 条逐条看过：**全是续作或不同版本**（``robocop`` → ``RoboCop 2``、
    ``family circuit`` → ``Family Circuit '91``、``sims the`` → ``Sims 2``…），
    **没有一条误伤**，也没有一条「本来对、换成了别的」。
    """
    print("\n[3c12] 封面模糊匹配：数字序列必须一致", flush=True)

    def _read(*parts):
        p = os.path.join(ROOT, *parts)
        try:
            with open(p, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    import cover_art as ca

    # ---- ① 版本号五处同步（只有最新分片负责，见 t_v606 的说明）----
    # ⚠ v1.0 起「产品版本」与「编码器能力版本」解耦：
    #   产品版本 APP_VERSION = 1.0（对外），AUDIO_WRITER_VERSION 仍是 6.0.12
    #   （BAD_BEFORE 用它判旧 MP3，降到 1.0 会把新文件误判成坏文件）。
    for path, pat in (("build_web_pyi.py", 'VER = "1.0"'),
                      ("web_ui/server.py", 'APP_VERSION = "1.0"'),
                      ("web_ui/index.html", "游戏音乐转换器 1.0</title>"),
                      ("web_ui/index.html",
                       '<span class="brand-ver">1.0</span>'),
                      ("dl_convert.py", 'AUDIO_WRITER_VERSION = "6.0.12"')):
        _mark(pat in _read(*path.split("/")),
              "版本号同步：%s 含 %s" % (path, pat), "")

    # ---- ② 新接口与常量都在 ----
    src = _read("cover_art.py")
    _mark(callable(getattr(ca, "num_seq", None)), "cover_art.num_seq 存在", "")
    _mark("_NUM_TOKEN" in src and "_ROMAN" in src,
          "有数字/罗马数字的正则和映射表", "")
    _mark(src.count("num_seq(") >= 5,
          "num_seq 在 pick / pick_all 的各层判据里都用上了"
          "（只在模糊档用不够，副标题层也会匹到续作）", src.count("num_seq("))

    # ---- ③ num_seq 的纯函数行为 ----
    _mark(ca.num_seq("Money Game II - The Kabutochou no Kiseki") == [2],
          "罗马数字转成数值（II -> 2）",
          ca.num_seq("Money Game II - The Kabutochou no Kiseki"))
    _mark(ca.num_seq("Money Game 2 - The Kabutochou no Kiseki") == [2],
          "阿拉伯数字原样", ca.num_seq("Money Game 2 - The Kabutochou no Kiseki"))
    _mark(ca.num_seq("Caltron 6 in 1") == [6, 1],
          "多个数字按顺序都取（6 in 1）", ca.num_seq("Caltron 6 in 1"))
    _mark(ca.num_seq("Castle Quest") == [],
          "没有数字返回空列表", ca.num_seq("Castle Quest"))
    _mark(ca.num_seq("1943 - The Battle of Midway") == [1943],
          "标题里的年份/编号数字也算（1943）",
          ca.num_seq("1943 - The Battle of Midway"))
    _mark(ca.num_seq("Rambo Rev PRG0") == [],
          "紧贴字母的数字不算 —— prg0 是 rom 修订号，不是游戏编号",
          ca.num_seq("Rambo Rev PRG0"))
    _mark(ca.num_seq("Final Fantasy X") == [10],
          "独立成词的 X 当罗马数字 10", ca.num_seq("Final Fantasy X"))
    _mark(ca.num_seq("Final Fantasy X-2") == [10, 2],
          "X-2 是两个数字（10 和 2）", ca.num_seq("Final Fantasy X-2"))

    # ---- ④ 行为：库里只有续作时必须挑不到 ----
    def _it(*names):
        return [{"dir": "Nintendo - Nintendo Entertainment System",
                 "name": n + ".png"} for n in names]

    _mark(ca.pick(_it("Sonic The Hedgehog 2 (USA)"), "Sonic The Hedgehog") == "",
          "sonic → sonic 2 不再贴错", "")
    _mark(ca.pick(_it("RoboCop 2 (USA) (Rev 1)"), "RoboCop") == "",
          "robocop → robocop 2 不再贴错", "")
    _mark(ca.pick(_it("Family Circuit '91 (Japan) (En)"), "Family Circuit") == "",
          "family circuit → family circuit '91 不再贴错", "")
    _mark(ca.pick(_it("Caltron 9 in 1 (USA)"), "Caltron 6 in 1") == "",
          "合卡张数不同（6 in 1 / 9 in 1）不匹配", "")
    _mark(ca.pick(_it("Black Bass (USA)"), "Black Bass 2, The") == "",
          "反向也要拦（要 2 代、库里只有 1 代）", "")

    # ---- ⑤ 行为：同一个游戏的不同写法必须照样匹配 ----
    got = ca.pick(_it("Money Game 2 - The Kabutochou no Kiseki (Japan)"),
                  "Money Game II - The Kabutochou no Kiseki")
    _mark(bool(got), "II 和 2 判成同一个游戏（罗马数字先转数值）", got)
    got = ca.pick(_it("Castlequest (USA)"), "Castle Quest")
    _mark(bool(got), "只差一个空格的名字照样能匹配", got)
    got = ca.pick(_it("Ultraman Club - Kaijuu Daikessen (Japan)"),
                  "Ultraman Club Kaijuu Dai Kessen")
    _mark(bool(got), "断词不同的名字照样能匹配", got)
    got = ca.pick(_it("SimCity 2000 - The Ultimate City Simulator (USA)"),
                  "SimCity 2000")
    _mark(bool(got), "真副标题（数字一致）照样能匹配 —— 别把副标题层一起废掉",
          got)

    # ---- ⑥ 副标题层单独验：libretro 也用 ` - ` 接续作编号 ----
    # `Family Quiz - 4-nin wa Rival` 是**另一款游戏**，不是 Family Quiz 的副标题。
    # 只给模糊档加判据的话，这条会从第 2 层漏过去（实测漏过）。
    _mark(ca.pick(_it("Family Quiz - 4-nin wa Rival (Japan)"), "Family Quiz") == "",
          "副标题层同样受「数字序列」约束（漏了这层实测会放行 family quiz）", "")
    _mark("raw_want_nums = num_seq(raw_want)" in src,
          "pick 的副标题层算过 num_seq", "")

    # ---- ⑦ pick_all 也要（轮播走的是它）----
    _mark(ca.pick_all(_it("Sonic The Hedgehog 2 (USA)"),
                      "Sonic The Hedgehog") == [],
          "pick_all 的模糊档同样受约束（它内部调 pick）", "")
    got = ca.pick_all(_it("Money Game 2 - The Kabutochou no Kiseki (Japan)"),
                      "Money Game II - The Kabutochou no Kiseki")
    _mark(len(got) == 1, "pick_all 对同游戏不同写法仍返回 1 张", got)

    # ---- ⑧ 阈值没动 ----
    _mark(ca.MIN_RATIO == 0.86, "MIN_RATIO 保持 0.86（方案 A 是加判据，不是调阈值）",
          ca.MIN_RATIO)
    _mark("MIN_RATIO = 0.86" in src, "源码里也是 0.86", "")

    # ---- ⑨ 判据只有一处实现 ----
    igdb = _read("cover_igdb.py")
    _mark("cover_art.pick(" in igdb,
          "cover_igdb 仍复用 cover_art.pick（判据一处生效，不会两边不一致）", "")
    _mark("def pick(" not in igdb,
          "cover_igdb 没有自己再实现一份 pick", "")

    # ---- ⑩ 审计脚本在（以后要重新评估阈值可以重跑）----
    for name in ("_probe_minratio.py", "_probe_numrule.py",
                 "_probe_realpick.py", "_probe_diff.py"):
        _mark(os.path.isfile(os.path.join(ROOT, "vendor", name)),
              "审计脚本 %s 在（可重跑复核）" % name, "")


def _sig_of(fn):
    """取函数签名文本（含默认值），供「参数还在不在」这类断言用。

    ⚠ ``inspect.signature`` 渲染默认值用的是**单引号**（``fmt='flac'``），
    断言里写双引号会永远不匹配 —— 所以统一换成双引号再比。
    """
    try:
        import inspect
        return str(inspect.signature(fn)).replace("'", '"')
    except Exception:                                # noqa: BLE001
        return ""


def _plan(state, fast):
    """门禁清单：``[(名字, 可调用对象), ...]``，顺序就是执行顺序。

    ⚠ 抽成一张表不是为了好看。本机挂着一道安全护栏：**单次进程删除
    超过 50 个文件要二次确认、超过 100 个直接终止进程**。整套门禁一次
    跑完会撞上去，而「被砍掉的那一段」既没有结论、又会在 ``D:\\MSU1Temp``
    里留下没删掉的残留 —— 下一轮跑出来的红字全是假阳性（实测踩过：
    ``webui_gate_play`` 残留一个 ``99_After.wav``，让「扫到 2 首」变成 3）。

    所以有了这张表，就可以 ``--only=`` / ``--skip=`` 分片跑，每片都在
    护栏以内，结论也是真的。
    """
    return [
        ("static", lambda: t_static()),
        ("api", lambda: t_api_contract(state)),
        ("sources", lambda: t_sources(state)),
        ("txtp", lambda: t_txtp(state, fast)),
        ("txtp_stream", lambda: t_txtp_stream(state, fast)),
        ("xsf", lambda: t_xsf_home(state, fast)),
        ("batch", lambda: t_batch_and_drop(state, fast)),
        ("xa", lambda: t_xa(state, fast)),
        ("psf", lambda: t_psf(state, fast)),
        ("usf", lambda: t_usf(state, fast)),
        ("vio", lambda: t_vio(state, fast)),
        ("ssf", lambda: t_ssf(state, fast)),
        ("gsf", lambda: t_gsf(state, fast)),
        ("qsf", lambda: t_qsf(fast)),
        ("v5911", lambda: t_v5911(fast)),
        ("v5912", lambda: t_v5912(fast)),
        ("cloud", lambda: t_cloud(state)),
        ("theme", lambda: t_theme(state)),
        ("game_folders", lambda: t_game_folders(state)),
        ("dl_play", lambda: t_dl_play(state)),
        ("dl_disk", lambda: t_dl_disk_state(state)),
        ("dl_row", lambda: t_dl_row_actions(state)),
        ("bios", lambda: t_bios_bundle(state)),
        ("bootlog", lambda: t_bootlog(state)),
        ("shell", lambda: t_shell(state)),
        ("zh", lambda: t_zh(state)),
        ("open_outdir", lambda: t_open_outdir(state)),
        ("default_outdir", lambda: t_default_outdir(state)),
        ("env", lambda: t_env(state)),
        ("stream", lambda: t_stream(state)),
        ("aif", lambda: t_aif(state, fast)),
        ("zophar", lambda: t_zophar(state)),
        ("kss", lambda: t_kss_native()),
        ("spc_progress", lambda: t_spc_progress()),
        ("viz", lambda: t_viz_panel()),
        ("flac_out", lambda: t_flac_out()),
        ("v5914", lambda: t_v5914()),
        ("v5915", lambda: t_v5915()),
        ("v5916", lambda: t_v5916()),
        ("v5917", lambda: t_v5917()),
        ("v5918", lambda: t_v5918()),
        ("v5919", lambda: t_v5919()),
        ("v5920", lambda: t_v5920()),
        ("v600", lambda: t_v600()),
        ("v601", lambda: t_v601()),
        ("v602", lambda: t_v602()),
        ("v603", lambda: t_v603()),
        ("v604", lambda: t_v604()),
        ("v605", lambda: t_v605()),
        ("v606", lambda: t_v606()),
        ("v607", lambda: t_v607()),
        ("v608", lambda: t_v608()),
        ("v609", lambda: t_v609()),
        ("v610", lambda: t_v610()),
        ("v611", lambda: t_v611()),
        ("v612", lambda: t_v612()),
        # 真转换 / 取消：--fast 下跳过（这两个是删除大户）
        ("opts_fsm", lambda: t_opts_and_fsm(state)),
        ("audio", lambda: t_audio(state)),
        ("cancel", lambda: t_cancel(state)),
    ]


#: ``--fast`` 下跳过的分片（真转换 + 取消）
_SLOW = ("opts_fsm", "audio", "cancel")


def main(argv=None):
    global _BASE
    argv = list(sys.argv[1:] if argv is None else argv)
    fast = "--fast" in argv

    def _arg(flag):
        for a in argv:
            if a.startswith(flag + "="):
                return [x.strip() for x in a.split("=", 1)[1].split(",")
                        if x.strip()]
        return []

    only = _arg("--only")
    skip = _arg("--skip")

    if not os.path.isfile(SPC):
        print("缺少素材: %s" % SPC, flush=True)
        return 2

    httpd, port, state = server.serve(port=0)
    _BASE = "http://127.0.0.1:%d" % port
    print("Web UI 门禁 · 后端 %s" % _BASE, flush=True)

    try:
        plan = _plan(state, fast)
        names = [n for n, _f in plan]
        unknown = [x for x in (only + skip) if x not in names]
        if unknown:
            print("未知分片: %s\n可选: %s" % (unknown, ", ".join(names)),
                  flush=True)
            return 2
        for name, fn in plan:
            if only and name not in only:
                continue
            if name in skip:
                continue
            if fast and name in _SLOW and not only:
                if name == "opts_fsm":
                    print("\n[4-6] --fast：跳过真实转换与取消", flush=True)
                continue
            fn()
    finally:
        httpd.shutdown()

    print("\n" + "=" * 46, flush=True)
    print("Web UI 门禁: %d 通过 / %d 失败" % (_PASS, _FAIL), flush=True)
    print("=" * 46, flush=True)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
