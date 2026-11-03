#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dl_api.py — 下载页（joshw.info 全机种镜像）的后端状态机。

## 这个模块是什么、不是什么

**是**：把 ``joshw_source`` / ``joshw_dl`` / ``dl_convert`` 的事件搬运成
「界面能直接渲染的一份状态」。tkinter 版这层长在 ``DownloadDialog`` 里
（3,082 行，其中一半是 Treeview 布局和列宽记忆）。

**不是**：不重写任何业务逻辑。索引怎么解析、7z 怎么解、芯片格式怎么解码，
一律调原模块。**改解码口径请改原模块，别改这里** —— 改了就是两份事实。

## 和 tkinter 版的一一对应

    DownloadDialog._load_platform   ->  load()
    DownloadDialog._scan_thread     ->  _scan_thread()
    DownloadDialog._start_download  ->  start()
    DownloadDialog._stop_download   ->  stop()
    DownloadDialog._handle_dl       ->  _on_dl_event()
    DownloadDialog._handle_cv       ->  _on_cv_event()
    DownloadDialog._toggle_zh       ->  translate()
    DownloadDialog.upload_cloud     ->  upload_cloud()

## 线程模型

和主界面（``server.AppState``）同一套：**工作线程只写状态、不碰 HTTP**，
前端 250ms 轮询 ``snapshot(since)`` 拉增量日志。没有 WebSocket、没有回调
进请求线程。

## 三个必须共用的对象（改这里前先读）

1. ``Downloader._cancel_event`` 与 ``ConvertWorker._cancel`` **必须是同一个
   Event**。「停止」一句话停掉下载 + 解压 + 转换三段，靠的就是它。
2. ``ConvertWorker`` 的 ``wav_dir`` 必须在 ``start()`` 时**冻结成绝对路径**
   —— 转换期间用户切机种会改保存目录，已入队的任务不能跟着漂。
3. 入队只用 ``dl_convert.policy_for(src)`` 定策略，**不要自己判扩展名**。
"""

import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import msu1_to_cd as core

# ── 可选依赖：缺一个就少一块功能，但不该让整个页面打不开 ──────────────
try:
    import joshw_source as jw
except Exception:                                    # noqa: BLE001
    jw = None
try:
    import joshw_dl
except Exception:                                    # noqa: BLE001
    joshw_dl = None
# zophar 是**第二家**音源站（joshw 之外的补充，见 zophar_source 模块头）。
# 它的条目 url 是「游戏页」而不是归档直链，下载前要 resolve 一次 —— 这个差别
# 只体现在 start() 里，其余（扫描/缓存/机种树）与 joshw 完全同形。
try:
    import zophar_source as zs
except Exception:                                    # noqa: BLE001
    zs = None
try:
    import dl_convert
except Exception:                                    # noqa: BLE001
    dl_convert = None
try:
    import zh_titles
except Exception:                                    # noqa: BLE001
    zh_titles = None
try:
    import zh_online
except Exception:                                    # noqa: BLE001
    zh_online = None
# v5.9.14：封面（libretro-thumbnails）。缺了就少一个功能，不影响下载本身。
try:
    import cover_art
except Exception:                                    # noqa: BLE001
    cover_art = None
# v6.0.5：封面兜底源（IGDB，走 design.zaparoo.org 的公开后端）。
# 和 ``cover_art`` 分开 try：libretro 能用而 IGDB 不能时，覆盖率只是低一点，
# 不该整块功能消失。
try:
    import cover_igdb
except Exception:                                    # noqa: BLE001
    cover_igdb = None
# v5.9.16：转 FLAC 前给**已经转好**的文件补图片块（``embed_picture``）。
# 和 ``cover_art`` 分开 try：只缺 soundfile 时封面缓存照样能用，
# 只是没法回填已有文件。
try:
    import flac_out
except Exception:                                    # noqa: BLE001
    flac_out = None
try:
    import psf_native
except Exception:                                    # noqa: BLE001
    psf_native = None


def _has_cover_source(key):
    """这个机种能不能抓封面 —— 两个源任意一个支持就算。

    v6.0.5 之前这里等同于 ``cover_art.supported(key)``，所以 Switch /
    FM Towns / PC 三个机种点「下载封面」会被直接拒绝。IGDB 接上之后
    它们都有源了，判断必须改成并集，否则新源白接。
    """
    if cover_art is not None and cover_art.supported(key):
        return True
    if cover_igdb is not None and cover_igdb.supported(key):
        return True
    return False


LOG_MAX = 2500
#: 机种分组顺序（与 tkinter 版 DownloadDialog.GROUP_ORDER 一致）
#: v5.9 去掉了末位 ``Zophar 原声`` —— 它的存在前提（joshw 没有 GG/SMS）
#: 已被证伪，两家都列只会让用户多纠结一次「为什么这条没体积」。
GROUP_ORDER = ("复古主机", "现代主机", "计算机与街机")
#: 单次最多下发多少条。**这是安全阀，不是显示上限** —— 显示上限已经
#: 取消（v5.9.1 改成前端虚拟滚动，多少条都能翻到底）。
#: 取 12000 的依据：实测最大的机种是 Switch 的 4638 条，其次街机 VGM
#: 1450 条；留一倍半余量，同时把单次快照的 JSON 压在 ~3MB 以内 ——
#: 轮询是 300ms 一次，再往上加就要开始吃 CPU 了。
#: ⚠ tkinter 版的 MAX_ROWS 仍是 4000（Treeview 塞多了是真的卡），
#:   两边不同口径是故意的，别去「对齐」。
MAX_ROWS = 12000

#: 「解码中…」心跳的最小间隔（秒）。
#: ⚠ ``cv_progress`` 是**每轨**一发：一个源几十首就是几十行，全写进日志会把
#: 下载日志淹掉（tkinter 版因此把 ``cv_progress`` 整个丢掉了，见
#: ``msu1_gui.py`` 里 "cv_progress / cv_stage 等明细 kind: **不进界面**"）。
#: Web 版按用户要求要**看得见**转换进度，所以改成限流：同一句话不重复、
#: 且最快 6 秒一行 —— 既在动，又不刷屏。
CV_TICK_SECS = 6.0
#: 试听列表最多返回多少首（前端一次渲染几千行会卡）
WAV_LIST_MAX = 400
#: 试听列表的扫描缓存时间（秒）。每转完一个源文件都要重扫一次目录，
#: 但前端 300ms 轮询一次 —— 不缓存的话就是每秒扫三遍全目录。
WAV_SCAN_TTL = 1.5
#: 试听列表「清空」的记录文件（落在 ``%LOCALAPPDATA%\MSU1Converter``）。
#: 内容是 ``{WAV 根目录(小写): 清空时刻}`` —— 为什么是记时刻而不是删文件，
#: 见 ``DownloadState.wav_clear()`` 的说明。
WAV_CLEAR_FILE = "wav_cleared.json"
#: 「手动加入试听」的记录文件（同目录）。内容是 ``[WAV 根目录(小写), [rel, ...]]``。
#:
#: 为什么需要它：试听列表原先只有「刚转出来的」一种来源（按 mtime > 清空时刻），
#: 于是**清空之后就没法再听了** —— 用户要的就是「自己挑哪几个游戏加进来」。
#: 手动加入的曲目不受「清空时刻」约束，但清空时会被一起清掉（清空 = 真的空）。
WAV_PICKS_FILE = "wav_picks.json"

#: 文件夹名清洗（兜底用，正常走 ``dl_convert.ConvertWorker._folder_name``）
_FOLDER_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

#: 每种音频格式落到保存目录下的子目录名（v5.9.15 起三种）。
#:
#: **必须一个格式一个目录**：``ConvertManifest.already_done()`` 判的是
#: 「这个源文件转过没有」，而记账文件 ``.msu1-convert.json`` 就放在输出目录里。
#: 共用目录 → 先转 WAV 再转 MP3 会被判成「已转过」直接跳过（v5.9.9 用户报的
#: 就是这个）。目录分开 = 记账分开 = 互不干扰。
_FMT_DIRS = {"wav": "WAV", "flac": "FLAC", "mp3": "MP3", "ogg": "OGG"}

#: 走 flac_out 编码的容器（v6.0.0 起四种：wav 直写，其余三种过编码器）。
_FMT_AUDIO = ("flac", "mp3", "ogg")

#: 每种格式的产物扩展名（``_audio_roots_now()`` 返回的 tag → 后缀）。
#: v5.9.15 前是三元表达式 ``".flac" if tag == "flac" else ".wav"`` ——
#: 加了 mp3 之后那种写法会静默把 MP3 当成 WAV 去找，所以收成表。
_FMT_EXTS = {"wav": ".wav", "flac": ".flac", "mp3": ".mp3", "ogg": ".ogg"}

#: 封面在音频目录里的文件名（v5.9.16）。
#:
#: ⚠ **必须和** ``spc_wav.COVER_NAME`` **一致** —— 转码时那边先找这个文件，
#:   这里负责把它放进去。两边写不一样就会出现「复制了但转码不认」，
#:   门禁 ``t_v5917`` 有断言盯着。这里不 import spc_wav 是因为它会连带
#:   拉进 msu1_to_cd（解码管线），而本模块在下载页只需要常量。
_COVER_NAME = "cover.png"

#: 每个游戏最多存几张封面图（v6.0.7）。
#: 用户 2026-10-28 拍板「最多 6 张」：IGDB 一个游戏就有 1 张盒绘 + 6~7 张
#: 截图 + 若干 extra_images，libretro 还能再补几个地区版本的盒绘。全收会到
#: 20+ 张、十几 MB —— 轮播根本看不完，纯占盘。
#: **只影响「下几张」，不影响「嵌哪张」**：嵌入永远只用盒绘
#: （``cover_art.BOXART_KINDS``），截图不会进音频文件。
COVER_LIMIT = 6

#: 「更新全部列表」同时扫几个机种（v6.0.8）。
#:
#: 一个机种要翻 27 个字母页，实测首次约 30 秒。33 个机种串着跑就是 15 分钟，
#: 用户会以为卡死。开 3 路并发大约 5 分钟 —— 每个机种仍然是**独立的一次完整
#: 扫描**（各自翻各自的 27 页），不是把同一机种的页面拆开抢跑，所以对站点的
#: 压力只是 3 倍于单个用户，可以接受。再高就没必要了：这是手动触发的维护
#: 动作，不是高频路径。
REFRESH_WORKERS = 3


class _Cancelled(Exception):
    """内部信号：用户在「更新全部列表」途中点了停。

    只用来让工作线程**提前退出**，不往外抛、不打日志 —— 用户主动停的
    不算失败，不该出现在「N 个失败」里。
    """

#: 下载页偏好（保存根目录）。**落在 exe 旁边** —— 换机器时整个文件夹拷过去
#: 就跟着走；exe 目录不可写（装在 Program Files）时退到
#: ``%LOCALAPPDATA%\MSU1Converter``。
#: ⚠ 里面存的是绝对路径，但用的时候一律「先验存在、再按目录名重找」，
#:   换台电脑盘符从 D: 变成 E: 也认得出来 —— 见 :func:`_relocate_dir`。
PREFS_FILE = "dl_prefs.json"

#: 「状态」列扫描保存目录时向下几层。用户可能把保存目录填成根
#: （``D:\MSU1Downloads``）、站（``…\joshw``）或机种（``…\joshw\Super Nintendo``），
#: 三层刚好覆盖全部三种填法，再多就是给「填成 D:\」的用户白扫。
DISK_SCAN_DEPTH = 3
#: 扫描的硬上限。用户真把保存目录填成 ``D:\`` 时，没有上限就是几分钟 + 卡死。
DISK_SCAN_MAX_NODES = 60000
#: 扫描时长上限（秒）。超了就用已扫到的部分 —— 宁可少认几条，不能卡住界面。
DISK_SCAN_DEADLINE = 2.5


def _app_dir():
    """程序目录：打包后是 exe 所在目录，源码跑时是项目根。

    ⚠ 不能只看 ``sys.executable`` —— PyInstaller/Nuitka onefile 下它指向
    临时解包目录，写进去一退出就没了（``bootlog._exe_dir`` 踩过同一个坑）。
    ⚠ 也不能无条件用 ``sys.argv[0]`` —— 源码跑 ``python -m web_ui.shot`` 时
    它是 ``web_ui/shot.py``，会把配置写到 ``web_ui/`` 里去。
    """
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        try:
            return os.path.dirname(os.path.abspath(sys.argv[0]))
        except (OSError, ValueError, IndexError):
            pass
    # 源码：本文件在 <根>/web_ui/dl_api.py，往上两级就是根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _prefs_paths():
    """``dl_prefs.json`` 的候选落点，按优先级：环境变量 → exe 旁边 → LOCALAPPDATA。

    ``MSU1_DL_PREFS`` 是给「不想往程序目录写东西」的场景留的（门禁、绿色版），
    值可以是文件路径，也可以是目录。
    """
    out = []
    env = os.environ.get("MSU1_DL_PREFS") or ""
    if env:
        out.append(env if env.lower().endswith(".json")
                   else os.path.join(env, PREFS_FILE))
    d = _app_dir()
    if d:
        out.append(os.path.join(d, PREFS_FILE))
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    if base:
        out.append(os.path.join(base, "MSU1Converter", PREFS_FILE))
    return out


def _prefs_path():
    """读的时候用：取第一个**已经存在**的；都不存在就返回首选路径。"""
    cands = _prefs_paths()
    for p in cands:
        if os.path.isfile(p):
            return p
    return cands[0] if cands else ""


def _local_drives():
    """本机存在的盘符（``["C:", "D:"]``）。

    用 ``GetLogicalDrives()`` 的位掩码，**不逐个 isdir 探测** —— 断开的
    网络映射盘上 ``os.path.isdir("Z:\\\\")`` 能卡好几秒，界面会假死。
    """
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()      # noqa: E1101
    except Exception:                                # noqa: BLE001
        return []
    return ["%s:" % chr(ord("A") + i) for i in range(26) if mask & (1 << i)]


#: 深度搜索时直接跳过的目录名（小写）。这些里面不可能有用户的下载目录，
#: 进去只是白扫几万个目录 —— ``Windows`` 一层就够卡半秒。
_RELOC_SKIP = {
    "windows", "$recycle.bin", "system volume information", "recovery",
    "program files", "program files (x86)", "programdata", "perflogs",
    "msocache", "config.msi", "$windows.~bt", "$windows.~ws",
    "appdata", "node_modules", ".git", ".svn", "__pycache__",
    "winsxs", "driverstore", "installer", "assembly",
}


def _find_by_name(name, must_have="", roots=None, max_depth=3, deadline=0.9):
    """在各盘里**按目录名**做有界广度搜索，返回第一个命中。

    「有界」是三重的，缺一条就会卡界面：深度 ``max_depth``、总时长
    ``deadline``、跳过 ``_RELOC_SKIP`` 里的系统目录。

    ⚠ 只在「精确路径 + 换盘符」都失败之后才调 —— 正常情况（目录没挪窝）
    一次都不会走到这里。走到的场景就是换电脑且目录位置也变了，多花
    半秒找回来，比「状态列全空白、用户以为没下过」划算得多。

    ``roots`` 是搜索起点（默认各盘根）。测试可以直接塞临时目录进来，
    不必真的在盘根下造东西。
    """
    if roots is None:
        roots = [d + os.sep for d in _local_drives()]
    t0 = time.time()
    frontier = [r for r in roots if r and os.path.isdir(r)]
    for _ in range(max(1, int(max_depth))):
        nxt = []
        for d in frontier:
            if time.time() - t0 > deadline:
                return ""
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if not e.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        low = e.name.lower()
                        if low in _RELOC_SKIP or low.startswith("$"):
                            continue
                        if low == name.lower():
                            if not must_have or os.path.isdir(
                                    os.path.join(e.path, must_have)):
                                return os.path.abspath(e.path)
                        nxt.append(e.path)
            except OSError:
                continue
        if not nxt:
            return ""
        frontier = nxt
    return ""


def _relocate_dir(path, must_have=""):
    """``path`` 在本机找不到时，**按目录名**重新找一遍。

    按「换机的可能性从高到低」四级试探，前一级命中就不往下走：

    1. **原样**就在 → 直接用（99% 的情况，零成本）；
    2. **只换了盘符** —— ``D:\\Games\\MSU1Downloads`` →
       ``E:\\Games\\MSU1Downloads``。把路径里的盘符挨个换成各盘试一遍。
       这是换电脑最常见的形态（新机器只有一块盘，原来那块叫 D: 现在叫 E:），
       而且**保留后面的整条路径**，比只认 basename 精确得多；
    3. **挪到 exe 旁边 / 上一级 / 某个盘根下** —— 绿色版整体拷走就长这样；
    4. **有界深度搜索**（见 :func:`_find_by_name`）—— 位置彻底变了。

    ``must_have`` 非空时，候选目录里必须有这个子目录才算「就是它」
    （``joshw`` 用来区分「同名但不相干」的目录）；实在一个都没有，
    退而求其次返回第一个同名目录 —— 目录名对上通常就是它。

    返回绝对路径；一个都找不到返回 ``""``。
    """
    path = str(path or "").strip()
    if path and os.path.isdir(path):
        return os.path.abspath(path)
    name = os.path.basename(path.rstrip("\\/"))
    if not name:
        return ""
    drives = _local_drives()

    def ok(c):
        return os.path.isdir(c) and (
            not must_have or os.path.isdir(os.path.join(c, must_have)))

    # -- 2. 换盘符（保留后面的整条相对路径）--------------------------------
    tail = os.path.splitdrive(path)[1]
    if tail and tail not in ("\\", "/"):
        tail = tail.lstrip("\\/")
        for drv in drives:
            c = os.path.join(drv + os.sep, tail)
            if ok(c):
                return os.path.abspath(c)

    # -- 3. exe 旁边 / 上一级 / 各盘根 --------------------------------------
    app = _app_dir()
    cands = [os.path.join(app, name),
             os.path.join(os.path.dirname(app) or app, name)]
    for drv in drives:
        cands.append(os.path.join(drv + os.sep, name))
    weak = ""
    seen = set()
    for c in cands:
        key = os.path.abspath(c).lower()
        if key in seen:
            continue
        seen.add(key)
        if not os.path.isdir(c):
            continue
        if must_have and not os.path.isdir(os.path.join(c, must_have)):
            if not weak:
                weak = c
            continue
        return os.path.abspath(c)
    if weak:
        return weak

    # -- 4. 有界深度搜索 ----------------------------------------------------
    return _find_by_name(name, must_have, roots=[d + os.sep for d in drives])


def _wav_folder_name(stem):
    """包名 → ``WAV/`` 下的子目录名。

    **必须与 ``dl_convert.ConvertWorker._folder_name`` 同一套规则** ——
    下载页要拿它去 ``WAV/<目录>/`` 里判断「这个游戏转没转过」，自己再写一遍
    清洗规则，迟早有一天两边对不上（比如这边忘了截断到 100 字符），
    表现就是「明明转好了，状态列还写着已解压」。
    """
    if dl_convert is not None:
        try:
            return dl_convert.ConvertWorker._folder_name(stem)
        except Exception:                            # noqa: BLE001
            pass
    s = _FOLDER_BAD.sub("_", str(stem or ""))
    s = s.strip().strip(".").strip()
    return s[:100].rstrip() or "未命名"


def _human(n):
    """字节数 → 人能读的短串。与 ``convert_job.fmt_size`` 同口径。

    ``0`` 一律显示成 ``—``（未知），不显示 ``0 B``：
    joshw 的列表页偶尔解析不出体积，**zophar 的列表页则根本没有体积**
    （要进游戏页才看得到）。显示 ``0 B`` 会让人以为「这个包是空的」。
    """
    n = float(n or 0)
    if n <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else \
                ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


def _fmt_day(stamp):
    """日期 → ``YYYY-MM-DD``（列表里的「日期」列）。

    ⚠ ``joshw_source`` 给的 ``mtime`` **本来就是字符串**（页面上的日期文本，
    已经 ``re.sub`` 压过空白），不是时间戳。只有缓存/内置索引的 stamp 才是
    float。两种都得接得住 —— 早先只当 float 处理，日期列全是空。
    """
    if isinstance(stamp, str):
        return stamp.strip()[:10]
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(stamp or 0)))
    except (ValueError, OSError, TypeError):
        return ""


def _safe_name(name):
    """条目名/机种名 → 合法的文件/目录名。

    ⚠ 机种名里**带斜杠**（``"SNES / SFC"``、``"PC Engine / TurboGrafx"``），
    直接拿去 ``os.path.join`` 会变成多级路径，``makedirs`` 直接报
    ``[WinError 3] 系统找不到指定的路径`` —— 实测踩过。
    """
    s = str(name or "unnamed").strip() or "unnamed"
    for ch in ('\\', '/', ':', '*', '?', '"', '<', '>', '|'):
        s = s.replace(ch, "_")
    return s.strip() or "unnamed"


class DownloadState:
    """下载页的全部状态。所有读写都在 ``self.lock`` 里。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.logs = []              # [[seq, text], ...]
        self._seq = 0

        # -- 当前机种 / 条目 ------------------------------------------------
        self.key = ""
        self.name = ""
        self.src = "joshw"          # "joshw" / "zophar"（决定条目 url 语义）
        self._zip_of = {}           # zophar：游戏页 url -> 原始音源直链
        self._zip_size = {}         # zophar：游戏页 url -> 原始包字节数
        self._resolving = False     # 正在解析 zophar 直链（防并发重复开始）
        self.entries = []           # 原始条目（jw / zs 给的 dict）
        self.states = {}            # url -> 状态文字（"✅ 完成" 之类）
        self.sel = set()            # 已选 url
        self.scanning = False
        self._scan_tok = 0
        self._scan_cancel = threading.Event()
        # -- 「更新全部列表」（v6.0.8）--------------------------------------
        # 用户报：「列表更新需要到每个机种下面去点击」。改成一个按钮跑完全部。
        # 它**只写各机种自己的缓存文件**，不碰 ``self.key`` / ``self.entries``
        # / ``self.sel`` —— 后台跑的时候用户照样能浏览、能下载，不会被
        # 「刷新」把选中项清掉。
        self.refresh = {"running": False, "done": 0, "total": 0,
                        "key": "", "name": "", "ok": 0, "fail": 0}
        self._refresh_cancel = threading.Event()
        # 同一个机种的缓存文件只能有一个线程在写：前台「清除缓存 + 重扫」和
        # 后台批量更新可能同时盯上同一个 key，两边都 save_cache 会写出半个
        # JSON。每个 key 一把锁，按需创建。
        self._key_locks = {}
        self._key_locks_guard = threading.Lock()
        self.source = ""            # cache / bundled / scan
        self.stamp = 0.0

        # -- 下载 / 转换 ----------------------------------------------------
        self.dl = None
        self.cv = None
        self.cancel = threading.Event()
        self.busy = False           # 下载+解压+转换任一段还在跑
        self.dl_total = 0           # 计划下载的字节数
        self.dl_shown = 0.0
        self.dl_expect = 0          # 计划下载的文件数
        self.dl_done = 0            # 已结束（成功+失败）的文件数
        self.dl_ok = 0
        self.dl_fail = 0
        self.dl_skip = 0
        self.ex_pkgs = 0            # 解压成功的包数
        self.ex_files = 0           # 解出来的文件数
        self.ex_fail = 0
        self.cv_queued = 0
        self.cv_ok = 0
        self.cv_fail = 0
        self.cv_skip = 0
        self.cv_bytes = 0
        self.cv_tracks = 0          # 已产出的 WAV 首数（一个源可能出多首）
        self.cv_current = ""
        #: v5.9.14：封面抓取进度（和下载并行，互不影响）
        self.cover_total = 0
        self.cover_done = 0
        self.cover_ok = 0
        self.cover_fail = 0
        self.cover_current = ""
        self._cv_tick = 0.0         # 上次「解码中…」心跳的时间戳
        self._cv_tick_text = ""     # 上次心跳的那句话（重复的不再写）
        self._wav_cache = None      # (时间戳, 列表) —— 试听列表扫描缓存
        # 「清空试听列表」的时刻，按 WAV 根目录分开记（切机种各算各的）。
        # 落盘在 %LOCALAPPDATA%\MSU1Converter\wav_cleared.json —— 只在内存里
        # 记的话，重开程序列表又全长回来，用户等于白点一次。
        self._wav_cleared = self._load_wav_cleared()
        # 「手动加入试听」的曲目（rel 小写）。与上面那份「清空时刻」是两个
        # 方向的东西：清空时刻负责「别让旧的自动冒出来」，这份负责
        # 「用户点名要听的，就算早于清空时刻也要出现」。
        self._wav_picks = self._load_wav_picks()
        # 「只转 WAV、不下载」的临时任务（列表里那颗粒按钮）。收尾日志要按
        # 这个口径写 —— 不然会报「下载成功 0 / 失败 0」，看着像失败了。
        self._cv_oneshot = False
        #: v5.9.9：本次转换的容器（``"wav"`` / ``"flac"``）。
        #: 「⏳ 待转 X」这类文案要跟着它走 —— 手动点「转 FLAC」时如果还写
        #: 「待转 WAV」，用户会以为按钮点错了。
        self._cv_fmt = "wav"
        #: 本轮转换涉及到的条目 url。收尾时要拿它把「🎵 已转 WAV」这类
        #: **单次转换**的说法清掉 —— 磁盘快照同时看得见 WAV/ 和 FLAC/，
        #: 交给它接管，状态列才会显示「🎵 已转 WAV+FLAC」。
        self._cv_urls = set()
        #: 最近一次转换的输出根目录（**WAV**）。只为兼容旧代码保留 ——
        #: 判断「产物在哪」请用 :meth:`_out_dir` / :meth:`_audio_roots_now`，
        #: 别再假定「所有音频都在 wav_dir 里」。
        self.wav_dir = ""
        #: v5.9.9：FLAC 的输出根目录（``<保存目录>/FLAC``）。
        #: 和 WAV **物理分开**，各自一份 ``.msu1-convert.json`` ——
        #: 这不是整理癖，是修 bug 的关键：``already_done()`` 判的是
        #: 「这个源文件转过没有」，它只认**自己那个目录**里的记账。
        #: 两种格式共用一份记账时，先转 WAV 再转 FLAC 会被判成「已转过」
        #: 直接跳过（用户报的就是这个）。目录分开 = 记账分开 = 互不干扰。
        self.flac_dir = ""
        #: v5.9.15：MP3 的输出根目录（``<保存目录>/MP3``）。理由同上 ——
        #: 三种格式三份记账，谁也不挡谁。
        self.mp3_dir = ""
        #: v6.0.0：OGG 的输出根目录（``<保存目录>/OGG``）。理由同上。
        self.ogg_dir = ""
        self.phase = "idle"         # idle | downloading | converting | done
        self.status = "就绪 — 请在左侧选择机种"
        self.progress = 0.0

        # -- 中文名 ---------------------------------------------------------
        self.zh = {}                # 英文名 -> 中文名（本机种内）
        self.zh_running = False
        self._zh_cancel = threading.Event()
        self._zh_logged = set()

        # -- 目录 / 选项 ----------------------------------------------------
        # 保存目录**不能写死本机路径**（v5.9.3）：先读上次的选择，找不到
        # 就按目录名在本机重新找（换电脑盘符变了也认得出来），最后才用默认。
        self._prefs = self._load_prefs()
        self._root_path = self._resolve_root()
        self._dest_manual = False
        self.dest = self._default_dest("", root=self._root_path)
        saved_dest = str(self._prefs.get("dest") or "")
        if self._prefs.get("manual") and saved_dest:
            d = _relocate_dir(saved_dest)
            if d:
                self.dest = d
                self._dest_manual = True
        # v6.0.11：封面根跟着保存目录走（``<下载根>/Artworks``）。
        # ⚠ 这里**只挪根、不搬家** —— 搬家（把 %LOCALAPPDATA% 的旧图复制
        # 过来）由 :meth:`start_cover_migration` 在**服务真起来**时触发。
        # 早先写在 __init__ 里，结果门禁每构造一个 DownloadState 就往用户
        # 磁盘上复制几十 MB，测试跑一次脏一次。
        self._cover_migrated = False
        self._sync_cover_root()
        self.opts = {
            "workers": 2,
            "auto_extract": True,
            "del_archive": False,
            "auto_wav": False,
            # v5.9.7：下载后自动转 FLAC。与 auto_wav **互斥**（前端保证），
            # 后端也各自独立判断 —— 两者同时为真时会各写一份同样的音频，
            # 属于用户明确要求的重复产物，不是 bug，但前端不会让它发生。
            "auto_flac": False,
            # v5.9.15：下载后自动转 MP3。和 auto_wav / auto_flac **三者互斥**
            # （前端保证，后端 :meth:`start` 里还有一道兜底）。有损格式，
            # 体积约 WAV 的 1/9 —— 给要往播放器/手机里塞的用户用。
            "auto_mp3": False,
            "merge_wav": False,
            #: v5.9.14：下载时顺手抓一张封面（libretro-thumbnails）。
            #: 默认开 —— 它和下载并行、失败只写日志，代价极小；
            #: 关掉之后转 FLAC 不会嵌封面（除非缓存里已经有）。
            "auto_cover": True,
        }
        self._disk = {}             # 落盘文件名 -> 字节（判「✓ 已下载」）
        # v5.9.3：光记文件不够。勾了「解压后删除原压缩包」的话，包没了、
        # 解压目录还在 —— 目录名没记下来，「已下载」就永远判不出来。
        self._disk_dirs = set()     # 保存目录下**一层子目录**名（小写）
        self._wav_games = set()     # WAV/ 下真的有 .wav 的子目录名（小写）
        #: v5.9.9：``FLAC/`` 下真的有 .flac 的子目录名（小写）。
        #: 和 ``_wav_games`` **分开记**：状态列要说清「转了哪种」，
        #: 合成一个集合就只能显示「已转音频」，用户没法判断还缺哪种。
        self._flac_games = set()
        #: v5.9.15：``MP3/`` 下真的有 .mp3 的子目录名（小写）。同理分开记。
        self._mp3_games = set()
        #: v6.0.0：``OGG/`` 下真的有 .ogg 的子目录名（小写）。同理分开记。
        self._ogg_games = set()
        self._disk_root = None
        self._disk_limited = False  # 上次扫描撞上 DEPTH/NODES/DEADLINE 上限了吗
        # 「这条音源在磁盘上到哪一步」的结果缓存。``snapshot()`` 每 300ms
        # 被调一次，4000 行光算落盘文件名（urlparse + 正则清洗）就要 34ms，
        # 一直烧着一个核的十分之一。名字只跟 (name, url) 有关，磁盘快照变了
        # 才需要重算 —— 所以在 ``_refresh_disk()`` 里整体清掉。
        self._dstate_cache = {}

        # -- 云上传（与主界面各自一套状态，共用 CloudUploader）-------------
        self.cloud = {"running": False, "progress": 0, "ok": 0,
                      "skip": 0, "fail": 0, "error": "",
                      "user": "", "count": 0}
        self._cloud_cancel = threading.Event()

        # 目录搬过家（换电脑 / 盘符变了）→ 把**新**位置写回偏好，下次直接命中
        # 第 1 级「原样就在」，不必再花时间找。只在真的搬家时写，
        # 正常启动一个字节都不落盘。
        if getattr(self, "_root_moved", False):
            try:
                self._save_prefs()
            except Exception:                        # noqa: BLE001
                pass

    # -- 日志 ---------------------------------------------------------------
    def log(self, msg):
        with self.lock:
            self._seq += 1
            self.logs.append([self._seq, str(msg)])
            if len(self.logs) > LOG_MAX:
                del self.logs[:len(self.logs) - LOG_MAX]

    def logs_since(self, since):
        with self.lock:
            return [r for r in self.logs if r[0] > since], self._seq

    def _status(self, text):
        with self.lock:
            self.status = str(text)

    # -- 目录 ---------------------------------------------------------------
    @staticmethod
    def _default_root():
        """本机默认的下载根目录 ``<临时盘根>/MSU1Downloads``。

        优先跟临时目录放同一个盘（本机是 ``D:\\MSU1Downloads``）—— 一张 PS1
        合集能到几个 GB，默认往 C 盘用户目录里塞不合适。

        ⚠ **没有 D 盘的机器**（笔记本只有一个 C 分区很常见）上 ``tmpdir`` 会
        回落到系统 temp，此时 ``dirname`` 得到的是
        ``C:\\Users\\xxx\\AppData\\Local`` —— 往那儿下载用户根本找不到自己的
        文件。这种情况改用用户主目录下的 ``MSU1Downloads``，看得见、也好找。
        """
        try:
            import tmpdir
            if not tmpdir.is_fallback():
                return os.path.join(os.path.dirname(tmpdir.root()) or "D:\\",
                                    "MSU1Downloads")
        except Exception:                            # noqa: BLE001
            pass
        return os.path.join(os.path.expanduser("~"), "MSU1Downloads")

    def _resolve_root(self):
        """自动模式下的下载根目录 —— **跟着本机走，不绑死上次那台电脑**。

        1. 上次用过的根目录**在本机真的存在** → 就用它；
        2. 不存在（换了电脑 / 盘符从 D: 变成 E: / 整个文件夹被拷走）→
           按**目录名**在本机重新找一遍（见 :func:`_relocate_dir`）；
        3. 都找不到 → 用本机默认（``_default_root()``）。

        ⚠ 这里绝不能只读保存的绝对路径就完事：换台电脑 ``D:\\MSU1Downloads``
        多半不存在，那样「状态」列会一条都算不出来 —— 用户看到的就是
        「明明下过，重启后全空白」。
        """
        saved = str(self._prefs.get("root") or "")
        self._root_moved = False
        if saved:
            d = _relocate_dir(saved, "joshw")
            if d:
                # 换机器 / 盘符变了：记住了「原来在哪」和「现在在哪」，
                # 由 load() 写一行日志告诉用户，别让目录悄悄搬家。
                self._root_moved = (os.path.abspath(d).lower()
                                    != os.path.abspath(saved).lower())
                self._root_saved = saved
                return d
        return self._default_root()

    @staticmethod
    def _artworks_root(dest):
        """封面根目录：``<下载根>/Artworks``（v6.0.11）。

        用户要求「封面改到下载音源目录里，单独一个 Artworks 文件夹，
        子目录是 ``机种\\游戏名``」。所以要从**保存目录**倒推下载根：

        * ``D:\\MSU1Downloads\\joshw\\Wii``  → ``D:\\MSU1Downloads\\Artworks``
        * ``D:\\MSU1Downloads\\joshw``       → ``D:\\MSU1Downloads\\Artworks``
        * ``E:\\Music\\PS4``（用户自定义）  → ``E:\\Music\\Artworks``

        最后一种是把 dest 的**父目录**当根。``joshw`` / ``zophar`` 这一层
        是站点划分，封面不分站点（同一游戏换个站下还是同一张图），
        所以统一提到它们的上一层。

        ⚠ 保持 ``@staticmethod``：门禁拿它当纯函数验目录形状。
        ⚠ 判空**必须在 abspath 之前** —— ``os.path.abspath("")`` 返回的是
        当前工作目录（不是空串），会凭空造出一个「<cwd>/../Artworks」。
        """
        s = str(dest or "").strip()
        if not s:
            return ""
        p = os.path.abspath(s)
        if not p:
            return ""
        base = os.path.basename(p).lower()
        if base in ("joshw", "zophar"):
            return os.path.join(os.path.dirname(p), "Artworks")
        parent = os.path.dirname(p)
        if os.path.basename(parent).lower() in ("joshw", "zophar"):
            return os.path.join(os.path.dirname(parent), "Artworks")
        return os.path.join(parent, "Artworks") if parent else ""

    @staticmethod
    def _plat_name_of(key):
        """机种 key -> 目录名（``nsf`` -> ``NES / FC``）。

        封面目录要和 ``<下载根>/joshw/<机种>`` 用同一套名字，用户翻目录
        才不会一半英文一半缩写。查不到就返回空，由 ``platform_sub``
        回落到 key。
        """
        k = str(key or "").strip().lower()
        if not k or jw is None:
            return ""
        for p in getattr(jw, "PLATFORMS", []) or []:
            if str(p.get("key") or "").lower() == k:
                return str(p.get("name") or "")
        return ""

    def _sync_cover_root(self, migrate=False):
        """把封面的根目录 / 机种层同步到当前保存目录（v6.0.11）。

        封面模块（``cover_art``）是**全局单例**，它不知道下载页当前在哪
        个机种上。切机种 / 改保存目录时必须推一把，否则新抓的图会落到
        上一个机种的目录里，或者落到旧的 ``%LOCALAPPDATA%`` 去。

        Args:
            migrate: 是否顺手把旧位置的图搬过来。**只有服务真启动时**才该
                     传 True（见 :meth:`start_cover_migration`）——
                     门禁里构造 DownloadState 也会走到这里，搬一次就把
                     用户的磁盘写一遍。
        """
        if cover_art is None:
            return
        root = self._artworks_root(self.dest)
        plat = self._plat_name_of(self.key) or ""
        try:
            cover_art.set_cache_root(root, plat)
        except Exception:                             # noqa: BLE001
            return
        if migrate and root and not self._cover_migrated:
            self._cover_migrated = True
            threading.Thread(target=self._cover_migrate_thread,
                             args=(root,), daemon=True).start()

    def start_cover_migration(self):
        """服务启动时调一次：把旧位置的封面搬进 ``Artworks``（v6.0.11）。

        用户要求「自动搬到新位置」。放在这里而不是 ``__init__`` 里，
        是为了**只在真服务里搬** —— 门禁/脚本构造 DownloadState 不该动
        用户磁盘。搬完打标记文件，之后每次启动都是一次 ``os.path.isfile``。
        """
        self._sync_cover_root(migrate=True)

    def _cover_migrate_thread(self, root):
        """后台把旧位置的封面搬进 Artworks（v6.0.11，只做一次）。

        **绝不抛异常**：封面是锦上添花，搬家失败不该影响下载页能不能用。
        """
        if cover_art is None:
            return
        try:
            st = cover_art.migrate_legacy(root, name_of=self._plat_name_of,
                                          log=self.log)
            if not (st.get("files") or st.get("games")):
                return
        except Exception as exc:                      # noqa: BLE001
            self.log("⚠ 旧封面搬家失败（不影响新图）：%s" % exc)

    @staticmethod
    def _default_dest(platform_name, src="joshw", root=""):
        """某个机种的保存目录：``<根>/<站>/<机种名>``。

        第二家站（zophar）单独一层目录，不和 joshw 的包混在一起 ——
        两家的包名规则不同（zophar 的原始包叫 ``<游戏>.zip``），混放会撞名。

        ⚠ 保持 ``@staticmethod``：门禁拿 ``DownloadState._default_dest("PlayStation")``
        当**纯函数**验「默认目录形状对不对」（不依赖任何实例状态，也不受开发机
        上 dl_prefs.json 影响）。根目录要非默认时用 ``root=`` 显式传进来，
        别改成从 ``self`` 取 —— 改了这条断言就变成「测开发机的配置」。
        """
        base = root or DownloadState._default_root()
        site = "zophar" if src == "zophar" else "joshw"
        return os.path.join(base, site, _safe_name(platform_name)) \
            if platform_name else os.path.join(base, site)

    # -- 偏好落盘（保存目录）------------------------------------------------
    def _load_prefs(self):
        """读 ``dl_prefs.json``。坏文件 / 读不到一律当「没设置过」。"""
        p = _prefs_path()
        if not p or not os.path.isfile(p):
            return {}
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_prefs(self):
        """把保存目录写回 ``dl_prefs.json``（失败就算了，不影响本次使用）。

        只存 ``root`` / ``dest`` / ``manual`` 三项 —— **不存选项**。
        选项（并发数、是否自动转 WAV）跟机器性能有关，换台电脑沿用上一台的
        选择反而容易出意外；而且门禁会调 ``set_opts``，存进去会污染仓库目录。
        """
        with self.lock:
            data = {"root": self._root_path or "",
                    "dest": self.dest or "",
                    "manual": bool(self._dest_manual)}
        for p in _prefs_paths():
            try:
                tmp = p + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=1)
                os.replace(tmp, p)
                return True
            except OSError:
                continue
        return False

    def _refresh_disk(self):
        """扫一遍**用户设定的保存目录**，记住「这条音源现在处于哪一步」。

        三样东西，**全部是磁盘事实**，不是内存里的运行状态 —— 关掉程序再打开
        照样算得出来。用户报的就是「重启后状态栏全空了，不知道哪个已经下过」：

        * ``_disk``      文件名（小写）-> 字节
        * ``_disk_dirs`` 目录名（小写），解压产物 ``<包名>/`` 就在这里
        * ``_wav_games`` ``WAV/`` 下**真的有 .wav** 的子目录名（按游戏分的那层）

        ## 以「用户设定的路径」为准，不认任何推导出来的路径

        保存目录可以随手填，用户填哪一层就得按哪一层扫。同一份数据可能被填成::

            D:\\MSU1Downloads                        ← 根（离包 2 层）
            D:\\MSU1Downloads\\joshw                  ← 站（离包 1 层）
            D:\\MSU1Downloads\\joshw\\Super Nintendo   ← 机种（包就在这层）
            E:\\我的音乐\\游戏原声\\任何名字             ← 完全自定义

        所以这里**不能只 scandir 一层**：填成根目录时包在下面两层，一层扫过去
        一条都看不见，用户看到的就是「明明下过，状态列全空」。改成向下
        ``DEPTH`` 层（默认 3）的有界遍历，三种填法都覆盖。

        ⚠ 有界是必须的：万一用户把保存目录填成 ``D:\\``，无界递归会扫几分钟
        并把界面卡死。``DEPTH`` + ``MAX_NODES`` + ``DEADLINE`` 三重上限，
        超了就用已经扫到的部分（宁可少认几条，也不能卡住）。
        ⚠ ``_wav_games`` 必须确认目录里**真有 .wav**：转换失败的包也会留下
        一个空目录，只看目录存在会把它报成「已转 WAV」。
        """
        root = self.dest or ""
        if not root or not os.path.isdir(root):
            with self.lock:
                self._disk = {}
                self._disk_dirs = set()
                self._wav_games = set()
                self._flac_games = set()
                self._mp3_games = set()
                self._ogg_games = set()
                self._disk_root = root
                self._dstate_cache = {}
            return

        found = {}
        dirs = set()
        wav_dirs = []                  # 找到的 WAV 根（可能有多个：多机种并存）
        # v5.9.9：FLAC 根也要单独收集 —— 产物从「都堆在 WAV/」改成了
        # 「WAV/ 和 FLAC/ 各一份」，只认 wav 目录的话，只转了 FLAC 的游戏
        # 状态列会一直显示「未转」。
        flac_dirs = []
        #: v5.9.15：MP3 根同理（``<保存目录>/MP3``）。
        mp3_dirs = []
        #: v6.0.0：OGG 根同理（``<保存目录>/OGG``）。
        ogg_dirs = []
        limited = False
        t0 = time.time()
        nodes = 0
        frontier = [(root, 0)]
        while frontier:
            if time.time() - t0 > DISK_SCAN_DEADLINE:
                limited = True
                break
            nxt = []
            for d, depth in frontier:
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            nodes += 1
                            if nodes > DISK_SCAN_MAX_NODES:
                                limited = True
                                break
                            try:
                                if e.is_dir():
                                    low = e.name.lower()
                                    dirs.add(low)
                                    if low == "wav":
                                        wav_dirs.append(e.path)
                                    elif low == "flac":
                                        flac_dirs.append(e.path)
                                    elif low == "mp3":
                                        mp3_dirs.append(e.path)
                                    elif low == "ogg":
                                        ogg_dirs.append(e.path)
                                    elif depth < DISK_SCAN_DEPTH:
                                        nxt.append((e.path, depth + 1))
                                elif e.is_file():
                                    found[e.name.lower()] = e.stat().st_size
                            except OSError:
                                continue
                except OSError:
                    continue
                if limited:
                    break
            frontier = nxt

        # 音频目录里的「按游戏分的子目录」—— 必须有成品才算，找到第一首就停。
        # v5.9.7：**.flac 也要认**（原来只认 .wav，选了 FLAC 后每行都显示未转）。
        # v5.9.9：WAV 和 FLAC 分别收进两个集合 —— 状态列要说清「转了哪种」，
        # 合成一个就只能显示「已转音频」，用户没法判断还缺哪种。
        # v5.9.15：再加 MP3，三个集合。
        def _games_with(root_list, exts):
            out = set()
            for wr in root_list:
                try:
                    for g in os.scandir(wr):
                        if not g.is_dir():
                            continue
                        try:
                            for s in os.scandir(g.path):
                                if (s.is_file()
                                        and s.name.lower().endswith(exts)):
                                    out.add(g.name.lower())
                                    break
                        except OSError:
                            continue
                except OSError:
                    continue
            return out

        wav = _games_with(wav_dirs, (".wav",))
        flac = _games_with(flac_dirs, (".flac",))
        mp3 = _games_with(mp3_dirs, (".mp3",))
        ogg = _games_with(ogg_dirs, (".ogg",))
        # 兼容老版本（≤v5.9.8）：那时 FLAC 也写在 WAV/ 里，目录里两种扩展名
        # 混着。这种目录里的 .flac 照样要认成「已转 FLAC」，否则老用户升级后
        # 会看到自己转过的东西忽然变成「未转」。
        flac |= _games_with(wav_dirs, (".flac",))
        with self.lock:
            self._disk = found
            self._disk_dirs = dirs
            self._wav_games = wav
            self._flac_games = flac
            self._mp3_games = mp3
            self._ogg_games = ogg
            self._disk_root = root
            self._disk_limited = limited
            self._dstate_cache = {}

    def _pending_tag(self, fmt=None):
        """「待转」状态里那个格式词（``WAV`` / ``FLAC`` / ``MP3``）。

        v5.9.9：优先用 ``fmt`` 参数，其次用本次转换的 ``self._cv_fmt``
        （手动点「转 FLAC」时设的），最后才退回 ``opts["auto_flac"]``。
        写死 WAV 的话，用户点「转 FLAC」后列表里一路显示「⏳ 待转 WAV」——
        而产物是 .flac。这种文案说谎最伤信任：用户会怀疑按钮根本没生效，
        然后去翻日志。

        v5.9.15：退回 opts 的那一支改成三选一（mp3 > flac > wav），
        和 :meth:`start` 里 ``_fmt`` 的推导顺序保持一致。
        """
        if fmt is None:
            fmt = getattr(self, "_cv_fmt", None)
        if fmt is None:
            fmt = self._opts_fmt()
        return self._norm_fmt(fmt).upper()

    def _opts_fmt(self):
        """``opts`` 里三个自动转开关推导出的格式 → ``"wav"`` / ``"flac"`` / ``"mp3"``。

        优先级 mp3 > flac > wav 只是为了「**确定性地**挑一个」——正常路径下
        三者互斥（前端保证 + :meth:`start` 兜底），同时为真的情况不该出现，
        真出现了也不能让结果随字典顺序漂移。
        """
        for k, f in (("auto_mp3", "mp3"), ("auto_flac", "flac")):
            if self.opts.get(k):
                return f
        return "wav"

    def _disk_state(self, name, url):
        """这条音源**在磁盘上**到哪一步了 → 界面「状态」列的文字。

        五级，从后往前判（越靠后越完成）::

            ""                 还没下载（空白）
            ✓ 已下载           压缩包在盘上
            📦 已解压           包已删（或还在），解压目录 ``<包名>/`` 在
            🎵 已转 WAV         ``WAV/<游戏名>/`` 里真有 .wav
            🎵 已转 FLAC        ``FLAC/<游戏名>/`` 里真有 .flac
            🎵 已转 MP3         ``MP3/<游戏名>/`` 里真有 .mp3（v5.9.15）
            🎵 已转 WAV+FLAC    多个目录都有，用 ``+`` 拼起来

        判据全来自 ``_refresh_disk()`` 的磁盘快照，所以**重启程序照样显示**。
        运行期的临时状态（``⏳ 待转 WAV`` / ``📦 解压中…`` / ``❌`` 之类）由
        ``snapshot()`` 优先采用，只有它没有的时候才落到这里。

        ⚠ v5.9.9 起**不再统一说「已转音频」**。两种格式可以同时存在
        （先转 WAV 再转 FLAC），只说「已转音频」用户就没法判断还缺哪一种 ——
        而「还缺哪种」正是他下一步要点哪颗按钮的依据。
        """
        # 结果按 (条目名, 真实直链) 缓存 —— 见 ``self._dstate_cache`` 的说明。
        # 缓存里存的是"这一轮磁盘快照下的答案"，快照一变 _refresh_disk 就清空。
        key = (name, self._zip_of.get(url) or url)
        hit = self._dstate_cache.get(key)
        if hit is not None:
            return hit
        arch = self._file_name(name, key[1])
        stem = os.path.splitext(arch)[0]
        # 磁盘快照的键统一小写（Windows 文件名本来就大小写不敏感），
        # 这样用户目录里是 "Game C [SNES].7Z" 也认得出。
        a_low = arch.lower()
        s_low = stem.lower()
        out = ""
        _g = _wav_folder_name(stem).lower()
        _has_wav = _g in self._wav_games
        _has_flac = _g in self._flac_games
        _has_mp3 = _g in self._mp3_games
        _has_ogg = _g in self._ogg_games
        # v5.9.15：三种格式用同一套拼法 —— 单个就是「已转 FLAC」，
        # 多个就是「已转 WAV+FLAC」「已转 WAV+FLAC+MP3」。文案格式和
        # v5.9.9 的「WAV+FLAC」完全一致，老断言不受影响。
        _tags = [t for t, on in (("WAV", _has_wav), ("FLAC", _has_flac),
                                 ("MP3", _has_mp3), ("OGG", _has_ogg)) if on]
        if _tags:
            out = "🎵 已转 " + "+".join(_tags)
        elif s_low in self._disk_dirs:
            out = "📦 已解压"
        elif a_low in self._disk or s_low in self._disk:
            out = "✓ 已下载"
        self._dstate_cache[key] = out
        return out

    # -- 机种 ---------------------------------------------------------------
    def platforms(self):
        """返回 ``[{group, items:[{key,name,fmt}]}]``（按 GROUP_ORDER 排）。

        v5.9 起**只列 joshw**。Zophar 的「原声」组已移除 —— 原因是它的
        存在前提没了：当初接 Zophar 是为了补 joshw 没有的 Game Gear /
        Master System，而实测 `kss.joshw.info` 根下本来就有这两个机种目录
        （v5.9 已拆成 ``kss_gg`` / ``kss_sms`` 两条独立机种）。
        两家都列，用户会看到「同一个游戏两个来源」，还得多解释一遍
        「为什么 Zophar 那条没体积」。

        ``zophar_source`` 模块本身没删（它的解析/分类逻辑还有测试价值，
        见 ``vendor/test_zophar.py``），只是不再进机种树 —— 所以下面
        所有 ``src == "zophar"`` 分支在界面上都不会被走到。
        """
        if jw is None:
            return [], "音源模块未就绪（缺少 joshw_source）"
        try:
            plats = list(jw.PLATFORMS)
        except Exception as exc:                     # noqa: BLE001
            return [], "读取机种列表失败: %s" % exc
        groups = {}
        for p in plats:
            if not isinstance(p, dict) or not p.get("key"):
                continue
            groups.setdefault(p.get("group") or "其他", []).append({
                "key": str(p.get("key")),
                "name": p.get("name") or str(p.get("key")),
                "fmt": p.get("fmt") or "—",
                "src": "joshw",
            })
        order = [g for g in GROUP_ORDER if g in groups]
        order += sorted(g for g in groups if g not in GROUP_ORDER)
        return [{"group": g, "items": groups[g]} for g in order], ""

    @staticmethod
    def _src_of(key):
        """机种 key 属于哪家。

        v5.9 起机种树里只有 joshw，恒返回 ``"joshw"``；保留函数是因为
        ``start()`` / ``open_dir()`` 等十几处按它分派，删掉改动面太大，
        而且 zophar 的解析分支留着做回归（`test_zophar.py` 直接测模块）。
        """
        return "joshw"

    def load(self, key, force=False):
        """切换机种：本地缓存 → 内置索引 → 后台联网扫描。"""
        key = str(key or "").strip()
        if not key:
            return {"ok": False, "error": "缺少机种 key"}
        src = self._src_of(key)
        mod = zs if src == "zophar" else jw
        if mod is None:
            return {"ok": False, "error": "音源模块未就绪"}
        try:
            meta = mod.platform(key) or {}
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}

        with self.lock:
            self._scan_tok += 1
            tok = self._scan_tok
            try:
                self._scan_cancel.set()
            except Exception:                        # noqa: BLE001
                pass
            self._scan_cancel = threading.Event()
            cancel = self._scan_cancel
            self.key = key
            self.src = src
            self.name = str(meta.get("name") or key)
            self.entries = []
            self.states = {}
            self.sel = set()
            self.zh = {}
            self.source = ""
            self.stamp = 0.0
            self.scanning = False
            self._zip_of = {}       # zophar：游戏页 url -> 原始音源直链
            self._zip_size = {}
            if not self._dest_auto_off():
                self.dest = self._default_dest(self.name, src, root=self._root_path)
        # v6.0.11：切机种会改保存目录，封面根和机种层要跟着挪 ——
        # 不挪的话新抓的图会落进上一个机种的目录（图还在，但翻目录时找不着）。
        self._sync_cover_root()
        # 目录被「搬家」过就说一句 —— 换台电脑盘符变了会自动找回，
        # 但用户得知道找的是哪个目录，不然会以为「怎么又空空的」。
        if getattr(self, "_root_moved", False):
            self._root_moved = False
            self.log("📁 下载根目录已自动找回：%s → %s"
                     % (getattr(self, "_root_saved", ""), self._root_path))
        self._refresh_disk()

        # ---- 1) 缓存 / 2) 内置快照 ----
        items, stamp, source = None, 0.0, "none"
        if not force:
            try:
                items, stamp, source = mod.best_index(key)
            except Exception as exc:                 # noqa: BLE001
                self.log("⚠ 读取索引失败（改为重新扫描）: %s" % exc)
                items, source = None, "none"
        if source == "cache" and items:
            self._apply_entries(items, stamp, "cache")
            self.log("📂 %s — 本地缓存（%d 条）" % (self.name, len(items)))
            return {"ok": True, "source": "cache", "count": len(items),
                    "scanning": False}
        if source == "bundled" and items:
            self._apply_entries(items, stamp, "bundled")
            day = _fmt_day(stamp)
            self.log("ℹ 使用内置索引（快照日期 %s），需要最新列表点「清除缓存」重扫"
                     % day)
            return {"ok": True, "source": "bundled", "count": len(items),
                    "scanning": False}

        # ---- 3) 联网扫描 ----
        with self.lock:
            self.scanning = True
            self.phase = "scanning"
            self.progress = 0.0
        if src == "zophar":
            # zophar 一个机种只有 2~3 页，几秒就完；joshw 是 27 个字母目录
            self._status("扫描中…（%s）" % self.name)
            self.log("▶ 扫描 %s — zophar.net/music/%s（分页游戏列表，几秒）"
                     % (self.name, meta.get("slug") or key))
        else:
            self._status("扫描中 0/27 页…（%s）" % self.name)
            self.log("▶ 扫描 %s — %s，首次约 30 秒"
                     % (key, meta.get("host") or key))
        threading.Thread(target=self._scan_thread, args=(key, tok, cancel),
                         daemon=True).start()
        return {"ok": True, "source": "scan", "count": 0, "scanning": True}

    def _dest_auto_off(self):
        """用户是否手工改过保存目录（改过就不再随机种自动变）。"""
        return bool(getattr(self, "_dest_manual", False))

    def set_dest(self, path):
        """用户手选了保存目录 → 记住它。

        ⚠ 必须落盘（``dl_prefs.json``）：只在内存里记的话，关掉程序再打开
        又回到默认目录，「状态」列按默认目录去算 —— 用户看到的就是
        「明明下过，重启后全空白」。而且换台电脑时这个文件跟着 exe 走，
        里面的绝对路径找不到会按目录名重新找（见 :func:`_relocate_dir`）。
        """
        path = str(path or "").strip()
        if not path:
            return False
        with self.lock:
            self.dest = path
            self._dest_manual = True
            # ``<根>/joshw/<机种>`` 这种形状就把根也记住 —— 换机时按
            # 「MSU1Downloads」找比按「WonderSwan」这种机种目录找可靠得多。
            # 用户也可能直接填到 ``<根>/joshw`` 这一层（截图里就是这样），
            # 那 ``<根>`` 就是它的上一级，两种都要认。
            p = os.path.abspath(path)
            if os.path.basename(p).lower() in ("joshw", "zophar"):
                self._root_path = os.path.dirname(p)
            else:
                up = os.path.dirname(p)
                if os.path.basename(up).lower() in ("joshw", "zophar"):
                    self._root_path = os.path.dirname(up)
            self._disk_root = None          # 目录变了，磁盘快照必须重扫
            self._disk_limited = False
        # v6.0.11：封面根也跟着这个目录走（``<新根>/Artworks``）
        self._sync_cover_root()
        self._refresh_disk()
        self._save_prefs()
        return True

    def _apply_entries(self, items, stamp, source):
        with self.lock:
            self.entries = [dict(e) for e in (items or [])
                            if isinstance(e, dict) and e.get("url")]
            self.stamp = float(stamp or 0)
            self.source = source
            self.scanning = False
            self.phase = "idle"
            self.progress = 0.0
            n = len(self.entries)
        self._refresh_disk()
        self._load_zh_cache()
        # ⚠ 这里**不要**写「已选 N 条」。早先硬编码了「已选 0 条」，
        #   而 select() 不会重新生成 status → 用户全选 1918 条后，
        #   顶部 #dlCount 写「已选 1918 条」、底部状态条还写「已选 0 条」。
        #   「已选多少」的唯一事实源是 #dlCount（来自 snapshot 的 sel），
        #   状态条只负责「当前在干什么」。
        self._status("%s — 共 %d 条" % (self.name, n))

    def _scan_thread(self, key, tok, cancel):
        """工作线程：只跑网络扫描，只往状态里写，不碰 HTTP。"""
        src = self._src_of(key)
        mod = zs if src == "zophar" else jw
        if mod is None:                              # 切换途中模块没了
            with self.lock:
                self.scanning = False
                self.phase = "idle"
            self._status("音源模块未就绪")
            return

        def progress(done, total, msg=""):
            if cancel.is_set() or tok != self._scan_tok:
                return
            pct = 100.0 * (done or 0) / max(1, int(total or 1))
            with self.lock:
                self.progress = max(0.0, min(100.0, pct))
            self._status("扫描中 %d/%d 页…（%s）%s"
                         % (done or 0, total or 1, self.name, msg or ""))

        try:
            # 真实签名是 scan_platform(key, progress=, cancel=) —— 位置传参也行，
            # 但关键字更抗改。缓存由它内部 save_cache 落盘，这里不用管。
            # ⚠ 按 key 加锁：「更新全部列表」可能正在扫同一个机种，
            #   两边同时 save_cache 会写出半个 JSON。
            with self._key_lock(key):
                items = mod.scan_platform(key, progress=progress, cancel=cancel)
        except Exception as exc:                     # noqa: BLE001
            if tok == self._scan_tok and not cancel.is_set():
                with self.lock:
                    self.scanning = False
                    self.phase = "idle"
                self._status("扫描失败")
                self.log("❌ 扫描失败: %s" % exc)
            return
        if cancel.is_set() or tok != self._scan_tok:
            return
        # scan_platform 内部已 save_cache，缓存文件的 mtime 就是这次的快照时间
        stamp = time.time()
        try:
            stamp = os.path.getmtime(mod.cache_file(key))
        except Exception:                            # noqa: BLE001
            pass
        self._apply_entries(items or [], stamp, "scan")
        self.log("✅ 扫描完成：%d 条" % len(items or []))

    # -- 选择 ---------------------------------------------------------------
    def select(self, urls=None, mode="set"):
        """``mode``: set / add / toggle / all / none / invert。"""
        with self.lock:
            valid = {e["url"] for e in self.entries}
            if mode == "all":
                self.sel = set(valid)
            elif mode == "none":
                self.sel = set()
            elif mode == "invert":
                self.sel = valid - self.sel
            elif mode == "add":
                self.sel |= {u for u in (urls or []) if u in valid}
            elif mode == "toggle":
                for u in (urls or []):
                    if u not in valid:
                        continue
                    if u in self.sel:
                        self.sel.discard(u)
                    else:
                        self.sel.add(u)
            else:
                self.sel = {u for u in (urls or []) if u in valid}
            return len(self.sel)

    # -- 下载 ---------------------------------------------------------------
    def _resolve_zophar(self, chosen, resolve_fail):
        """把选中的游戏页解析成原始音源归档直链，写进 ``_zip_of`` / ``_zip_size``。

        zophar 的列表页只给「游戏」，真实归档在游戏页的 ``#mass_download`` 里，
        所以下载前必须逐页解析一次。三点约束：

        * **不能持锁解析** —— 前端每 300ms 轮询 ``/api/dl/state``，持锁 6 秒
          等于界面卡死。这里只在读写字段时短暂拿锁。
        * 单个游戏解析失败**不影响**其它条目：失败原因逐条进日志，
          对应行标 ``❌``，其余照常下载。
        * 用 ``self._scan_cancel`` 作为取消信号 —— 「停止」按钮在下载阶段
          才生效，解析阶段属于同一次操作，用户按停止应该立刻停。
        """
        if zs is None:
            self.log("⚠ Zophar 模块未就绪，无法解析直链")
            return
        pages = [str(e.get("url")) for e in chosen if e.get("url")]
        if not pages:
            return
        with self.lock:
            self._resolving = True
            self.phase = "resolving"
            self.progress = 0.0
            cancel = self._scan_cancel
        try:
            self._status("解析原始音源直链 0/%d…" % len(pages))
            self.log("🔎 解析 %d 个游戏的原始音源直链（每个要读一次游戏页，"
                     "约 0.6 秒/个）" % len(pages))

            def on_progress(done, total, msg):
                pct = 100.0 * (done or 0) / max(1, int(total or 1))
                with self.lock:
                    self.progress = max(0.0, min(100.0, pct))
                self._status("解析原始音源直链 %d/%d… %s"
                             % (done or 0, total or 1, msg or ""))

            got = zs.resolve_many(pages, progress=on_progress, cancel=cancel)
            with self.lock:
                for page, res in (got or {}).items():
                    if res.get("ok"):
                        self._zip_of[page] = str(res["url"])
                        self._zip_size[page] = int(res.get("size") or 0)
                    else:
                        resolve_fail.append((page, str(res.get("error") or "失败")))
            for page, reason in resolve_fail:
                name = self._name_of(page)
                self.log("❌ %s — %s" % (name, reason))
            self.log("🔎 解析完成：%d 个有原始音源%s"
                     % (len(self._zip_of),
                        "，%d 个没有（已跳过）" % len(resolve_fail)
                        if resolve_fail else ""))
        finally:
            with self.lock:
                self._resolving = False
                self.progress = 0.0
                if not self.busy:
                    self.phase = "downloading"

    def _name_of(self, url):
        """条目 url → 显示名（找不到就回退成 url 尾巴）。"""
        with self.lock:
            for e in self.entries:
                if e.get("url") == url:
                    return str(e.get("name") or url)
        return str(url or "").rstrip("/").rsplit("/", 1)[-1]

    def start(self, opts=None):
        """开始下载（含自动解压 / 自动转 WAV）。返回 ``{ok, ...}``。"""
        if joshw_dl is None:
            return {"ok": False, "error": "下载模块未就绪（缺少 joshw_dl）"}
        with self.lock:
            if self.busy or self.scanning or self._resolving:
                return {"ok": False, "error": "正在忙，先停止或等它跑完"}
            key = self.key
            src = self.src
            chosen = [e for e in self.entries if e["url"] in self.sel]
            if not key:
                return {"ok": False, "error": "请先选择机种"}
            if not chosen:
                return {"ok": False, "error": "请先选中要下载的条目"}
            merged = dict(self.opts)
            for k, v in (opts or {}).items():
                if k in merged:
                    merged[k] = v
            # v5.9.7：兜底互斥。两个都为真时以 auto_flac 为准 —— 这与
            # ConvertWorker 的 fmt 选择、以及下面 want_wav 的判断**同一口径**，
            # 保证「界面文案 / 实际产物 / opts 记录」三者不会互相打脸。
            # v5.9.15：三选一，优先级 mp3 > flac > wav（与 _opts_fmt 一致）。
            if merged.get("auto_mp3"):
                merged["auto_flac"] = False
                merged["auto_wav"] = False
                merged["merge_wav"] = False
            if merged.get("auto_flac"):
                merged["auto_wav"] = False
                merged["merge_wav"] = False
            if "dest" in (opts or {}) and opts["dest"]:
                self.set_dest(opts["dest"])
            self.opts = merged
            dest_root = self.dest

        try:
            os.makedirs(dest_root, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": "无法创建目录 %s：%s"
                                          % (dest_root, exc)}

        want_wav = bool(dl_convert is not None
                        and (merged["auto_wav"] or merged.get("auto_flac")
                             or merged.get("auto_mp3")))

        # ---- zophar：条目是「游戏页」，下载前先把原始音源直链解析出来 ----
        # joshw 的条目 url 本身就是归档直链，不走这一步。
        # 每个游戏要读一次游戏页（约 0.6 秒），并发 2 路 + 全局节流；
        # 失败的不拖累其它条目，单独在日志里说清原因。
        resolve_fail = []
        if src == "zophar":
            self._resolve_zophar(chosen, resolve_fail)

        todo, resume, skipped = [], [], 0
        for e in chosen:
            url = e.get("url")
            if src == "zophar":
                real = self._zip_of.get(url)
                if not real:
                    # 解析阶段已经记过日志了，这里只把行状态标红
                    with self.lock:
                        self.states[url] = "❌ 没有原始音源"
                    continue
                url = real
            target = os.path.join(
                dest_root, self._file_name(e.get("name"), url))
            size = int(e.get("size") or 0)
            if src == "zophar":
                size = int((self._zip_size or {}).get(e.get("url")) or 0)
            try:
                have_archive = (size > 0 and os.path.isfile(target)
                                and abs(os.path.getsize(target) - size) <= max(
                                    4096, size // 100))
                have_payload = bool(joshw_dl) and joshw_dl.has_payload(
                    target, dest_root)
            except OSError:
                have_archive = have_payload = False
            if have_archive or have_payload:
                skipped += 1
                with self.lock:
                    self.states[e["url"]] = "⏭ 已存在 → 待转" if want_wav \
                        else "⏭ 已存在"
                self.log("⏭ 已存在，跳过: %s" % os.path.basename(target))
                if want_wav:
                    resume.append((e["url"], target))
                continue
            todo.append((url, target, size))
            with self.lock:
                self.states[e["url"]] = "等待…"

        if not todo and not resume:
            if resolve_fail:
                self._status("选中的 %d 个游戏都没有原始音源" % len(resolve_fail))
                self.log("❌ 选中的 %d 个游戏都没有原始音源（只有 MP3 录音），"
                         "换几个再试" % len(resolve_fail))
                return {"ok": False,
                        "error": "%d 个游戏都没有原始音源" % len(resolve_fail)}
            self._status("全部 %d 个已存在，无需下载" % skipped)
            self.log("✅ 没有需要下载的文件")
            return {"ok": True, "todo": 0, "skipped": skipped}

        with self.lock:
            self.busy = True
            self.phase = "downloading"
            self.cancel = threading.Event()
            self.dl_total = max(1, sum(s for _u, _t, s in todo)) if todo else 1
            self.dl_shown = 0.0
            self.dl_done = 0
            self.dl_ok = 0
            self.dl_fail = 0
            self.dl_skip = skipped
            self.dl_expect = len(todo)
            self.ex_pkgs = 0
            self.ex_files = 0
            self.ex_fail = 0
            self.cv_queued = 0
            self.cv_ok = 0
            self.cv_fail = 0
            self.cv_skip = 0
            self.cv_bytes = 0
            self.cv_tracks = 0
            self.cv_current = ""
            self.cover_total = 0
            self.cover_done = 0
            self.cover_ok = 0
            self.cover_fail = 0
            self.cover_current = ""
            self._cv_tick = 0.0
            self._cv_tick_text = ""
            self._cv_urls = set()
            self._wav_cache = None
            self.progress = 0.0
            self.wav_dir = os.path.join(dest_root, "WAV")
            # v5.9.9：FLAC 独立目录。两个都提前算好，状态列 / 试听随时能用。
            self.flac_dir = os.path.join(dest_root, "FLAC")
            # v5.9.15：MP3 同样独立一个。
            self.mp3_dir = os.path.join(dest_root, "MP3")
            # v6.0.0：OGG 同样独立一个（四份记账，互不干扰）。
            self.ogg_dir = os.path.join(dest_root, "OGG")
            self._finished = False
            self._pkg_logged = set()

        # 转换 worker 必须在下载器建好之后建 —— 两者共用同一个 cancel Event
        self.cv = None
        try:
            self.dl = joshw_dl.Downloader(
                workers=int(merged["workers"]),
                on_event=self._on_dl_event,
                auto_extract=bool(merged["auto_extract"]),
                delete_archive=bool(merged["del_archive"]),
                extract_dir=dest_root)
            for url, target, size in todo:
                self.dl.add(url, target, size)
            shared = getattr(self.dl, "_cancel_event", None)
            if isinstance(shared, threading.Event):
                with self.lock:
                    self.cancel = shared
        except Exception as exc:                     # noqa: BLE001
            with self.lock:
                self.busy = False
                self.phase = "idle"
                self.dl = None
            self.log("❌ 创建下载任务失败: %s" % exc)
            return {"ok": False, "error": "无法创建下载任务: %s" % exc}

        # ---- v6.0.4：下载流程**不再**自动抓封面 ----
        # v5.9.14 起这里是「和下载并行跑一段 _cover_worker」，开关是上面那个
        # 「顺带下载封面」复选框。用户 2026-10-28 要求把复选框改成按钮
        # （「可主动选择什么时候下载封面」），所以这段整块删掉 ——
        # 抓图只剩两个入口：底部「下载封面」按钮，和转 FLAC/MP3 前的补图。
        #
        # ⚠ 别因为「少了一段功能」又把这里加回来：默认自动联网是用户明确
        #   不想要的。要恢复也得先问。

        # v5.9.15：格式在这里先算好 —— 下面日志文案 / except 分支都要用它，
        # 放在 try 里一旦前面那几行炸了就会 NameError。
        _fmt = self._opts_fmt()
        if want_wav:
            try:
                # v5.9.9：输出目录**跟着格式走**（WAV → <dest>/WAV，
                # FLAC → <dest>/FLAC）。以前两种格式都写进 wav_dir，
                # 既让产物混在一个目录里，又让两种格式共用一份转换记账 ——
                # 后者的后果是「转完 WAV 再转 FLAC 会被判成已转过而跳过」。
                _out = self._out_dir(_fmt, dest_root)
                self.cv = dl_convert.ConvertWorker(
                    _out, self._on_cv_event, self.cancel,
                    merge_wav=bool(merged["merge_wav"]),
                    # v5.2-web-game: 每个游戏一个子文件夹。
                    # 只有下载流程能确认「<下载根>/<包名>/」这个结构，
                    # 所以只在这里开（ConvertWorker 默认是关的）。
                    group_by_game=True,
                    fmt=_fmt)
                self.cv.start()
                self.log("🎵 下载后自动转 %s 已开启：输出 %s"
                         % (_fmt.upper(), _out))
                self.log("📁 每个游戏一个子文件夹（按包名），编号从 01 起")
            except Exception as exc:                 # noqa: BLE001
                self.cv = None
                self.log("⚠ 转 %s 启动失败，本次只下载不解码: %s"
                         % (_fmt.upper(), exc))

        self.log("⬇ 开始下载 %d 个文件（并发 %s）→ %s"
                 % (len(todo), merged["workers"], dest_root)
                 + ("，跳过已存在 %d 个" % skipped if skipped else ""))
        if merged["auto_extract"]:
            self.log("📦 下载后自动解压" +
                     ("，并删除原 .7z 包" if merged["del_archive"]
                      else "（原包保留）"))
        self._status("下载中 0/%d · 0 B / %s"
                     % (len(todo), _human(self.dl_total)))

        if todo:
            self.dl.start()
        else:
            # 全部已存在但开了自动转码：没有下载，直接进转换
            self.log("✅ 无需下载（%d 个已存在），开始转换已有文件" % skipped)
            self._enqueue_resume(resume)
            if self.cv is not None:
                self.cv.mark_src_finished()
                with self.lock:
                    self.phase = "converting"
                    self.progress = 0.0
                    queued = self.cv_queued
                self.log("🎵 开始转 %s：已入队 %d 个源文件"
                         % (_fmt.upper(), queued))
        return {"ok": True, "todo": len(todo), "skipped": skipped}

    @staticmethod
    def _file_name(name, url=""):
        """条目名 → 落盘文件名。**必须与 ``joshw_dl.Downloader.add()`` 同一套规则**，
        否则「已存在则跳过」判断的路径和真实落盘路径对不上，每次都会重下。

        三个要点（每一条都踩过）：
        * 条目 ``name`` **不带扩展名**（``"...[SNES]"``），扩展名在 URL 里。
          不补 ``.7z`` 的话：``is_archive`` 不认 → 不解压；``is_convertible``
          不认 → 不转 WAV。实测现象是「下载成功、什么都没产出」。
        * 清洗交给 ``joshw_dl.sanitize_filename``（Downloader 内部用的就是它）。
        * 机种名里带斜杠，``_safe_name`` 兜一道。
        """
        raw = str(name or "download")
        ext = ""
        if url:
            try:
                ext = os.path.splitext(
                    urllib.parse.urlparse(str(url)).path)[1]
            except Exception:                        # noqa: BLE001
                ext = ""
        if not ext:
            ext = ".7z"
        if not raw.lower().endswith(ext.lower()):
            raw += ext
        if joshw_dl is not None:
            try:
                return joshw_dl.sanitize_filename(raw)
            except Exception:                        # noqa: BLE001
                pass                                 # 退化成下面这份实现
        return _safe_name(raw)

    def _enqueue_resume(self, resume):
        """把「已下载但未转过」的条目排进转换队列（只转，不重下）。

        ⚠ 按目录去重：``joshw_dl.payload_files`` 每次都会 scandir 整个产物
        目录，上百个「已存在」条目逐个调用就是 O(N²) 的目录扫描。
        """
        if self.cv is None or not resume:
            return
        by_dir = {}
        for url, target in resume:
            try:
                if joshw_dl is not None and joshw_dl.is_archive(target):
                    d = joshw_dl.extract_dir_for(target, self.dest)
                    by_dir.setdefault(d, (url, []))[1].extend(
                        joshw_dl.payload_files(target, self.dest))
                else:
                    by_dir.setdefault(os.path.dirname(target),
                                      (url, []))[1].append(target)
            except Exception:                        # noqa: BLE001
                continue
        for _d, (url, files) in by_dir.items():
            # v6.0.6：去掉「音源转出来的 WAV」（``xxx.fsb`` + ``xxx.fsb.wav``），
            # 否则同一首会被当成两首各转一遍。见 dl_convert.is_derived_wav。
            for f in dl_convert.drop_derived_wavs(
                    [x for x in files if dl_convert.is_convertible(x)]):
                self._enqueue(f, url, pkg=os.path.basename(str(f)))

    def _enqueue(self, src, url, pkg=""):
        """唯一入队路径。``policy`` 交给 ``dl_convert.policy_for`` 定。"""
        cv = self.cv
        if cv is None or not src:
            return
        try:
            cv.enqueue({"src": src, "url": url, "pkg": pkg,
                        "policy": dl_convert.policy_for(src), "n_hint": 0})
        except Exception:                            # noqa: BLE001
            return
        with self.lock:
            self.cv_queued += 1
            self.states[url] = "⏳ 待转 " + self._pending_tag()
            if url:
                self._cv_urls.add(url)

    def stop(self):
        """「停止」= 下载 + 解压 + 转 WAV 三段联动。"""
        with self.lock:
            dl, cv, cancel = self.dl, self.cv, self.cancel
            was_busy = self.busy
        self.log("⛔ 正在停止…")
        try:
            cancel.set()
        except Exception:                            # noqa: BLE001
            pass
        try:
            if dl is not None:
                dl.cancel()
        except Exception:                            # noqa: BLE001
            pass
        try:
            if cv is not None:
                cv.cancel()
        except Exception:                            # noqa: BLE001
            pass
        with self.lock:
            if not was_busy:
                self.phase = "idle"
            self._status("已停止")
        return {"ok": True}

    # -- 封面 ---------------------------------------------------------------
    def _cover_worker(self, items, cancel=None):
        """后台抓封面（``[(机种 key, 条目名), ...]``）。

        ⚠ 这个线程**绝不能**动 ``phase`` / ``busy`` / ``progress`` ——
        那是下载和转换的状态机。封面插一脚的后果是「下载早就完了，
        界面还在转」或者反过来「界面说完了，其实还在下」。
        它只写 ``cover_*`` 这一组计数器和日志。

        ⚠ 也**绝不**能让异常逃出去：封面是锦上添花，它失败一次不能让
        整批下载判失败。所以每个条目单独 try，外层再兜一道。

        v6.0.5 起是**双源**：先 libretro-thumbnails，没找到再问 IGDB
        （``cover_igdb``）。后者不是官方 API，见那个模块开头 —— 它挂了
        只是覆盖率掉回去，不影响这里。
        """
        if cover_art is None and cover_igdb is None:
            return
        total = len(items)
        if not total:
            return
        with self.lock:
            # 调用方已经设过一次（为了让界面在起线程前就看到总数），
            # 这里再设一遍是幂等的 —— 好处是这个方法单独调用时计数器也是对的。
            self.cover_total = total
        srcs = []
        if cover_art is not None:
            srcs.append("libretro-thumbnails")
        if cover_igdb is not None:
            srcs.append("IGDB")
        self.log("🖼 封面：%d 个游戏，来源 %s" % (total, " → ".join(srcs)))
        # 索引先单独拉一遍：第一次要下 1 MB 左右的目录（SNES 3689 条），
        # 不单独拉的话这段耗时会算在第一个游戏头上，看着像「它特别慢」。
        # ⚠ 只在 libretro 有该机种目录时才预热 —— Switch / FM Towns / PC
        #   在它那儿压根没有目录，预热纯属白跑一趟。
        if cover_art is not None:
            try:
                cover_art.list_covers(items[0][0], cancel=cancel)
            except Exception:                        # noqa: BLE001
                pass
        ok = fail = igdb_ok = 0
        n_img = 0
        for key, title in items:
            if cancel is not None and cancel.is_set():
                self.log("🖼 封面：已停止（成功 %d / 未找到 %d / 共 %d）"
                         % (ok, fail, total))
                break
            with self.lock:
                self.cover_current = title
            got = self._cover_many(key, title, cancel)
            if got:
                ok += 1
                n_img += len(got)
                # 每个游戏的图可能来自两个源，按标签分开数 —— 日志里要能
                # 看出「这个游戏是 libretro 有、还是全靠 IGDB 兜的」。
                by = {}
                for _p, tag in got:
                    by[tag] = by.get(tag, 0) + 1
                if by.get("IGDB"):
                    igdb_ok += 1
                self.log("🖼 %s → %d 张（%s）"
                         % (title, len(got),
                            " · ".join("%s %d" % (k, v)
                                       for k, v in sorted(by.items()))))
            else:
                fail += 1
                self.log("🖼 %s ✗ 两个源都没找到" % title)
            with self.lock:
                self.cover_done = ok + fail
                self.cover_ok = ok
                self.cover_fail = fail
                self.cover_current = ""
        if fail:
            self.log("🖼 封面完成：%d 个游戏 / %d 张图，%d 个没找到（不影响音源）"
                     % (ok, n_img, fail))
        else:
            self.log("🖼 封面完成：%d 个游戏 / %d 张图" % (ok, n_img))
        if igdb_ok:
            self.log("🖼 其中 %d 个游戏用到了 IGDB（libretro 没收录的）"
                     % igdb_ok)

    def _cover_many(self, key, title, cancel=None, limit=COVER_LIMIT):
        """给一个游戏抓**一组**图：libretro 的盒绘 + IGDB 的盒绘/截图。

        v6.0.7：用户要求「一个游戏的封面资源应该有若干个，下载时全下载，
        试听时轮播」。两个源都要问 —— libretro 先给盒绘（常有好几个地区
        版本），不够 ``limit`` 张再由 IGDB 补 ``cover`` + ``screenshots``。

        ⚠ 顺序就是**展示顺序**，也是**嵌音频取哪张**的依据：盒绘永远排在
        前面（``cover_art.BOXART_KINDS``），截图排在后面。用户明确要求
        「嵌入 flac 和 mp3 只嵌入 boxart」。

        Returns:
            ``[(绝对路径, 来源标签), ...]``，标签 ∈ ``libretro`` / ``IGDB``
            / ``缓存``。空列表 = 一张都没拿到。
        """
        # 多要两张：下面按内容去重，重复的去掉之后还要凑够 limit。
        # 实测 PSP 的 Amnesia：libretro 的 ``Amnesia.png`` 和 IGDB 的
        # ``igdb_cover_co20ie.png`` **是同一张图**（都是 247,077 B）——
        # libretro 的图源本来就是 IGDB。不去重的话轮播会连着放两张一样的，
        # 用户以为卡住了。
        need = limit + 2
        raw = []                                 # [(path, 来源, kind)]
        if cover_art is not None:
            try:
                got = cover_art.fetch_all(key, title, cancel=cancel, limit=need)
            except Exception as exc:                 # noqa: BLE001
                got = []
                self.log("⚠ 封面（libretro）%s：%s" % (title, exc))
            for p, kind in got:
                raw.append((p, "libretro", kind))
        if (len(raw) < need and cover_igdb is not None
                and not (cancel is not None and cancel.is_set())):
            try:
                got = cover_igdb.fetch_all(key, title, cancel=cancel,
                                           limit=need - len(raw))
            except Exception as exc:                 # noqa: BLE001
                got = []
                self.log("⚠ 封面（IGDB）%s：%s" % (title, exc))
            for p, kind in got:
                raw.append((p, "IGDB", kind))
        raw = self._dedup_images(raw)
        out = raw[:limit]

        # ⚠ **去重后必须写回查找表**。只去重内存里的列表不够：
        # ``cover_paths()`` 读的是查找表，表里那两条重复记录还在的话，
        # 界面上就会连着显示两张一模一样的图（实测踩到：7 张里前两张
        # 都是 247,077 B 的同一张）。
        if cover_art is not None:
            try:
                root = cover_art.cache_root()
                rels = []
                for p, _src, kind in out:
                    # v6.0.11：目录变成 ``<根>/<机种>/<游戏名>/<图>`` 三层，
                    # 再按「取两段目录名」拼 rel 就拼不出来了 —— 直接算相对
                    # 路径，层数怎么变都对。
                    try:
                        rel = os.path.relpath(p, root).replace("\\", "/")
                    except ValueError:           # 不同盘符
                        continue
                    if rel.startswith(".."):
                        continue                 # 不在封面根下，跳过
                    rels.append((rel, kind))
                if rels:
                    cover_art.set_covers(title, rels)
                else:
                    # 空 rels 多半是「图的路径不在封面根下」—— 说明写侧和
                    # 读侧的根对不上。这种错**不会报错**，只是表里没记录，
                    # 表现是「图在盘上但试听没封面」，所以必须留一句日志。
                    self.log("⚠ 封面路径不在封面根下，没记进查找表：%s"
                             "（根=%s，共 %d 张）" % (title, root, len(out)))
            except Exception as exc:                 # noqa: BLE001
                self.log("⚠ 封面查找表写回失败：%s" % exc)

        # 老图打「缓存」标签：日志里要能区分「这次真下了」和「本来就有」，
        # 否则重复点按钮看不出是「秒过」还是「真快」。
        tagged = []
        for p, src, _kind in out:
            try:
                old = cover_art._is_old(p) if cover_art is not None else False
            except Exception:                        # noqa: BLE001
                old = False
            tagged.append((p, "缓存" if old else src))
        return tagged

    @staticmethod
    def _dedup_images(items):
        """按**文件内容**去重，保留第一次出现的那条。

        每项只要求第 0 位是路径，其余字段原样带过去（``_cover_many`` 传的
        是 ``(path, 来源, kind)`` 三元组，门禁里传二元组，都能吃）。

        为什么不用文件名：libretro 和 IGDB 给同一个游戏用的名字完全不同
        （``Amnesia.png`` vs ``igdb_cover_co20ie.png``），但图一模一样。
        为什么不只用大小：不同的截图偶尔同尺寸，会误删。

        读全文件算 md5 —— 单张 200 KB ~ 1 MB，6 张也就几 MB，
        在后台线程里跑，用户感觉不到。
        """
        seen = set()
        out = []
        for it in items:
            try:
                p = it[0]
            except (TypeError, IndexError):
                continue
            try:
                with open(p, "rb") as fh:
                    h = hashlib.md5(fh.read()).hexdigest()
            except OSError:
                continue
            if h in seen:
                continue
            seen.add(h)
            out.append(it)
        return out

    def _cover_one(self, key, title, cancel=None):
        """给一个游戏抓**一张盒绘**。返回 ``(ok, path, 说明, 标签)``。

        只用于「转码前补图」那种真的只需要一张的场景（图要嵌进音频，
        而音频的图片字段只有一个）。批量下载封面走 :meth:`_cover_many`。

        ⚠ v6.0.7 刻意**不**改成 ``_cover_many(limit=1)``：那样「为什么没
        拿到」的理由会全变成「两个源都没找到」，日志里就分不清
        「IGDB 返回的是 JPEG」和「这个游戏真没收录」了。
        """
        why = ""
        if cover_art is not None:
            try:
                good, path, why = cover_art.fetch(key, title, cancel=cancel)
            except Exception as exc:                 # noqa: BLE001
                good, path = False, ""
                why = "%s: %s" % (type(exc).__name__, exc)
            if good:
                return True, path, why, ("缓存" if why == "已有" else "libretro")
        if cancel is not None and cancel.is_set():
            return False, "", "已取消", ""
        if cover_igdb is not None:
            try:
                good, path, why2 = cover_igdb.fetch(key, title, cancel=cancel)
            except Exception as exc:                 # noqa: BLE001
                good, path, why2 = False, "", "%s: %s" % (
                    type(exc).__name__, exc)
            if good:
                return True, path, why2, "IGDB"
            # 两边的理由都留着：不然「两边都没收录」和「IGDB 挂了」分不清。
            why = "%s ／ IGDB：%s" % (why, why2) if why else why2
        return False, "", why or "没找到", ""

    # -- 下载器事件 ---------------------------------------------------------
    def _url_for(self, path):
        """落盘路径 → 条目 url（状态要写回正确的行）。

        ⚠ zophar 的条目 url 是**游戏页**（没有扩展名），真实落盘名是
        ``<游戏名>.zip`` —— 必须用解析出来的归档直链去算名字，
        否则 ``_file_name`` 会补成 ``.7z``，永远匹配不上，界面所有行
        都停在「等待…」（下载其实成功了，只是状态回写不到行上）。
        """
        if not path:
            return None
        base = os.path.basename(str(path))
        with self.lock:
            for e in self.entries:
                page = e.get("url")
                real = self._zip_of.get(page) or page
                if self._file_name(e.get("name"), real) == base:
                    return page
        return None

    def _set_state(self, url, text):
        if not url:
            return
        with self.lock:
            self.states[url] = text

    def _on_dl_event(self, kind, payload):
        """下载线程回调：只更新状态 + 写日志，绝不阻塞。"""
        payload = payload if isinstance(payload, dict) else {}
        try:
            self._handle_dl(kind, payload)
        except Exception as exc:                     # noqa: BLE001
            self.log("⚠ 处理下载事件 %s 出错: %s" % (kind, exc))

    def _handle_dl(self, kind, payload):             # noqa: C901 事件分发
        if kind == "file_start":
            self._set_state(self._url_for(payload.get("file")), "下载中…")
            return

        if kind == "progress":
            try:
                self.dl_shown = max(self.dl_shown,
                                    float(payload.get("done") or 0))
            except (TypeError, ValueError):
                pass
            self._paint_progress()
            return

        if kind == "file_done":
            url = self._url_for(payload.get("file"))
            name = os.path.basename(str(payload.get("file") or "?"))
            with self.lock:
                self.dl_done += 1
                self.dl_shown = max(self.dl_shown,
                                    float(payload.get("done") or 0))
                if payload.get("ok"):
                    self.dl_ok += 1
                else:
                    self.dl_fail += 1
            if payload.get("ok"):
                self._set_state(url, "✅ 完成")
                size = int(payload.get("size") or 0)
                self.log("✅ %s  %s" % (name, _human(size)))
                # SPC 是裸文件、不经解压 —— 必须在这里入队，
                # 否则只接 extract_done 的话 SPC 永远转不出来。
                if self.cv is not None and name.lower().endswith(".spc"):
                    self._enqueue(str(payload.get("file") or ""), url, pkg=name)
            else:
                self._set_state(url, "❌ 失败")
                reason = str(payload.get("error") or "下载失败")
                self.log("❌ %s: %s" % (name, reason))
                if "404" in reason:
                    self.log("ℹ 该条目可能已从站点移除，点「清除缓存」重扫")
            self._paint_progress(force=True)
            return

        if kind == "extract_start":
            name = os.path.basename(str(payload.get("file") or "?"))
            self._set_state(self._url_for(payload.get("file")), "📦 解压中…")
            self._status("解压中 %s …" % name)
            self.log("📦 正在解压 %s" % name)
            return

        if kind == "extract_done":
            name = os.path.basename(str(payload.get("file") or "?"))
            count = int(payload.get("count") or 0)
            files = payload.get("files") or []
            with self.lock:
                self.ex_pkgs += 1
                self.ex_files += count
            self._set_state(self._url_for(payload.get("file")),
                            "📦 已解压" +
                            ("(删包)" if payload.get("archive_removed") else ""))
            self.log("✅ 已解压 %s → %s（%d 个文件）"
                     % (name, os.path.basename(str(payload.get("dest") or "")),
                        count))
            self._refresh_disk()
            if self.cv is not None:
                # v6.0.6：同 _enqueue_resume —— 去掉「音源转出来的 WAV」。
                srcs = dl_convert.drop_derived_wavs(
                    [str(f) for f in files if dl_convert.is_convertible(f)])
                if srcs:
                    url = self._url_for(payload.get("file"))
                    for f in srcs:
                        self._enqueue(f, url, pkg=name)
                    # 可转的入队了，但包里可能还躺着转不了的 —— GBA 包就是
                    # 「10 个 .mod + 121 个 .minigsf」。不说这一句，用户会
                    # 以为剩下那 121 首是程序漏掉了（v5.2.6 之前就是这样）。
                    skipped = dl_convert.explain_skipped(
                        [str(f) for f in files
                         if not dl_convert.is_convertible(f)])
                    if skipped:
                        self.log("ℹ %s：已入队 %d 个，%s"
                                 % (name, len(srcs), skipped))
                else:
                    self.log("⏭ %s：%s"
                             % (name, self._payload_hint(payload.get("dest"))))
                return

        if kind == "extract_failed":
            name = os.path.basename(str(payload.get("file") or "?"))
            with self.lock:
                self.ex_fail += 1
            self._set_state(self._url_for(payload.get("file")), "⚠ 解压失败")
            self.log("⚠ %s 解压失败: %s"
                     % (name, payload.get("error") or "未知原因"))
            return

        if kind == "all_done":
            self.log("📥 下载与解压全部结束")
            if self.cv is not None:
                self.cv.mark_src_finished()
                with self.lock:
                    self.phase = "converting"
                    queued = self.cv_queued
                    self.progress = 0.0
                self._status("转换中…")
                # 转换阶段开头给一行「要转多少个」—— 否则进度条从 100% 掉回 0
                # 会让人以为出错了。
                self.log("🎵 开始转 WAV：已入队 %d 个源文件" % queued)
            else:
                self._finish()
            return

    @staticmethod
    def _dir_files(dest, max_depth=3, cap=8000):
        """列出解压目录下的所有文件 —— 委托 :func:`dl_convert.walk_files`。

        保留这个名字是因为门禁与老代码都按它调；实现只有一份，别在这里
        再写一遍 ``os.walk``（两处不一致时最难查）。
        """
        return dl_convert.walk_files(dest, max_depth=max_depth, cap=cap)

    @staticmethod
    def _payload_hint(dest):
        """解压产物里没有可转换的源文件时，说清楚为什么。

        v5.2.6 起不再「把扩展名一列了事」—— 旧文案是
        「包里没有本工具能直接转换的音源（.minigsf、.m3u…）」，对用户等于
        什么都没说：他不知道是自己下错了、还是程序漏了、还是本来就不行。

        现在改为调 :func:`dl_convert.explain_skipped`，按格式给出**原因**
        （内存快照要主机模拟器核心 / 容器已解但负载编码没破 / libgme 没编
        该机种），见 README ⚠58 的 8 机种实测表。
        """
        d = str(dest or "")
        files = DownloadState._dir_files(d)
        if not files:
            return "解压目录是空的"
        exts = {os.path.splitext(f)[1].lower() for f in files}
        # 这三种是「能转，但要走别的入口」—— 不是「转不了」，必须单独说，
        # 否则用户会以为 .psf 也得装模拟器。
        # ⚠ v5.4: PS2 的 .psf2/.minipsf2 也原生支持了（psf2wav.exe 的
        #   version 2 路径）—— 必须一并放行，否则整包 .psf2 会被下面的
        #   explain_skipped 兜底成「包内没有可转换的音源」，把能转的说成转不了
        #   （test_web_ui.py [9c] 钉住这条）。
        if exts & {".psf", ".minipsf", ".psf1", ".minipsf1",
                  ".psf2", ".minipsf2"}:
            return "包内是 PSF，可直接加进转换页（Web 版原生解码）"
        if exts & {".txtp"}:
            return "包内是 .txtp 索引，可直接拖进转换页"
        hint = dl_convert.explain_skipped(files)
        if hint:
            return hint
        return "包内没有可转换的音源（%s）" \
            % "、".join(sorted(e for e in exts if e)[:5])

    def _paint_progress(self, force=False):
        with self.lock:
            total = max(1.0, float(self.dl_total))
            pct = 100.0 * float(self.dl_shown) / total
            self.progress = max(0.0, min(100.0, pct))
            text = ("下载中 %d/%d 个 · %s / %s"
                    % (self.dl_done, max(1, self.dl_expect),
                       _human(int(self.dl_shown)), _human(self.dl_total)))
        self._status(text)

    # -- 转换进度（日志 + 进度条）--------------------------------------------
    def _short_src(self, src):
        """源文件 → 日志里好认的短名。

        下载流程下源文件在 ``<下载根>/<游戏名>/BGM.psf``，只写 basename 的话
        一个机种几十个 ``BGM.psf`` 全一样，用户分不出是哪首 —— 所以带上
        「游戏名/文件名」。不在下载根里的（比如转换页拖进来的）退回 basename。
        """
        s = str(src or "")
        if not s:
            return "?"
        try:
            root = os.path.dirname(os.path.abspath(self._wav_dir_now()))
            rel = os.path.relpath(os.path.abspath(s), root)
            parts = [p for p in rel.replace("\\", "/").split("/")
                     if p not in ("", ".", "..")]
            if len(parts) >= 2:
                return "/".join(parts[-2:])
        except (ValueError, OSError):
            pass
        return os.path.basename(s) or "?"

    def _cv_heartbeat(self, payload):
        """「解码中…」心跳 —— **限流**写日志。

        为什么不是每条 ``cv_progress`` 都写：一个大源几十首，每首一发，
        全写进日志区会把下载日志冲没（这正是 tkinter 版干脆丢掉它的原因）。
        这里改成：同一句话不重复 + 最快 ``CV_TICK_SECS`` 秒一行。

        ⚠ ``idx`` / ``total`` 的语义**随解码路径不同**（见
        ``dl_convert.ConvertWorker._on_progress`` 的说明）：芯片路径是
        ``(累计音轨数, run 序号, 秒数)``，SPC 路径是 ``(序号, 总数, 曲名, 秒)``。
        所以只在「看着像第几首/共几首」时才把数字写出来，宁可少写，
        也不写出「第 12/3 首」这种自相矛盾的行。
        """
        with self.lock:
            cur = self.cv_current
            last = self._cv_tick
        idx, total = payload.get("idx"), payload.get("total")
        tip = ""
        if isinstance(idx, int) and isinstance(total, int) and 0 < idx <= total:
            tip = "（第 %d/%d 首）" % (idx, total)
        text = "⏳ 解码中 %s%s" % (self._short_src(cur), tip)
        now = time.time()
        with self.lock:
            if text == self._cv_tick_text or now - last < CV_TICK_SECS:
                return
            self._cv_tick = now
            self._cv_tick_text = text
        self.log(text)

    def _paint_cv_progress(self):
        """转换阶段的进度条 + 状态行。

        下载阶段进度条按**字节**算（``_paint_progress``）；转换阶段没有字节可
        算，改成按「已处理源文件 / 已入队源文件」—— 否则下载一结束进度条就
        卡在 100% 不动，用户以为卡死了。
        """
        with self.lock:
            if self.phase != "converting":
                return
            done = self.cv_ok + self.cv_fail + self.cv_skip
            queued = max(1, self.cv_queued)
            self.progress = min(100.0, 100.0 * done / queued)
            text = ("转换中 %d/%d 个源 · 已出 %d 首 · %s"
                    % (done, queued, self.cv_tracks, _human(self.cv_bytes)))
            cur = self.cv_current
        if cur:
            # 正在转哪个文件 —— 状态行是**一行**，比刷日志更合适
            text += " · 正在转 %s" % self._short_src(cur)
        self._status(text)

    # -- 转换器事件 ---------------------------------------------------------
    def _on_cv_event(self, kind, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        try:
            self._handle_cv(kind, payload)
        except Exception as exc:                     # noqa: BLE001
            self.log("⚠ 处理转换事件 %s 出错: %s" % (kind, exc))

    def _handle_cv(self, kind, payload):             # noqa: C901 事件分发
        if kind == "cv_log":
            text = payload.get("text") or payload.get("msg") or ""
            if text:
                self.log(text)
            return
        if kind == "cv_file_start":
            src = str(payload.get("src") or "")
            with self.lock:
                self.cv_current = src
                # 新文件允许立刻打一次「解码中」心跳（同一个文件的后续
                # cv_progress 才受限流约束）
                self._cv_tick = 0.0
                self._cv_tick_text = ""
            self._paint_cv_progress()
            return
        if kind == "cv_skip":
            with self.lock:
                self.cv_skip += 1
                n = self.cv_skip
            # ⚠ 逐条记会刷屏：重跑一次就有几百条「已转过」。
            #   每 10 条报一次总数，用户照样知道在往前走。
            if n % 10 == 0:
                self.log("⏭ 已跳过 %d 个（之前转过，不重转）" % n)
            self._paint_cv_progress()
            return
        if kind == "cv_file_done":
            nbytes = int(payload.get("bytes") or 0)
            n_wav = int(payload.get("n_wav") or 0)
            with self.lock:
                self.cv_ok += 1
                self.cv_bytes += nbytes
                self.cv_tracks += n_wav
                self.cv_current = ""
                done = self.cv_ok
                queued = self.cv_queued
                tracks = self.cv_tracks
            url = payload.get("url")
            if url:
                # ⚠ 别写死 "🎵 已转 WAV"：点「转 FLAC」时这一行会说谎，用户
                #   会以为按钮没生效。跟着本次格式走（v5.9.9）。
                self._set_state(url, "🎵 已转 " + self._pending_tag())
            # 用户要的就是这一行：**每转完一个源文件就在日志框里留一行**。
            self.log("🎵 [%d/%d] %s → %d 首 · %s（累计 %d 首）"
                     % (done, max(queued, done), self._short_src(payload.get("src")),
                        n_wav, _human(nbytes), tracks))
            self._paint_cv_progress()
            return
        if kind == "cv_file_failed":
            with self.lock:
                self.cv_fail += 1
                self.cv_current = ""
            url = payload.get("url")
            if url:
                self._set_state(url, "⚠ 转换失败")
            self.log("❌ %s 转换失败：%s"
                     % (self._short_src(payload.get("src")),
                        payload.get("error") or "未知原因"))
            self._paint_cv_progress()
            return
        if kind == "cv_progress":
            cur = payload.get("current") or payload.get("file") or ""
            if cur:
                with self.lock:
                    self.cv_current = str(cur)
            self._cv_heartbeat(payload)
            return
        if kind == "cv_merged":
            self.log("🔗 已合并：%s" % (payload.get("file") or ""))
            return
        if kind == "cv_abandoned":
            self.log("⚠ 磁盘空间不足，放弃剩余 %s 个文件"
                     % (payload.get("count") or "?"))
            return
        if kind == "cv_paused":
            self._status("磁盘空间不足，已暂停…")
            return
        if kind == "cv_error":
            self.log("❌ 转换出错: %s" % (payload.get("error") or ""))
            return
        if kind == "cv_thread_exit":
            self._finish()
            return

    def _finish(self):
        """收尾（下载 + 转换都结束了）。可重入。"""
        with self.lock:
            if getattr(self, "_finished", False):
                return
            self._finished = True
            self.busy = False
            self.phase = "done"
            self.progress = 100.0
            ok, fail, skip = self.dl_ok, self.dl_fail, self.cv_skip
            wav_ok, wav_fail = self.cv_ok, self.cv_fail
            tracks = self.cv_tracks
            pkgs, files = self.ex_pkgs, self.ex_files
            oneshot = bool(getattr(self, "_cv_oneshot", False))
            self._cv_oneshot = False
        if oneshot:
            # 只转（不下载）的临时任务：没有下载、没有解压统计，按「转了 N 首」说。
            # 复用上面那套「下载成功 0 / 失败 0」的文案会让用户以为失败了。
            _tag = self._pending_tag()
            _dir = self._out_dir(self._cv_fmt)
            self._status("转 %s 完成：%d 首（失败 %d）" % (_tag, tracks, wav_fail))
            self.log("🏁 转 %s 完成 — 成功 %d 首 / 失败 %d 首"
                     % (_tag, tracks, wav_fail))
            if tracks:
                self.log("📁 %s 输出目录：%s" % (_tag, _dir))
            self._refresh_disk()
            self._drop_cv_states()
            return
        _tag = self._pending_tag()
        _dir = self._out_dir(self._cv_fmt)
        self._status("完成：下载 %d 个，解压 %d 个包 / %d 个文件，转 %s %d 首"
                     % (ok, pkgs, files, _tag, tracks))
        self.log("🏁 全部完成 — 下载成功 %d / 失败 %d / 跳过 %d；"
                 "解压 %d 个包（%d 个文件）；转 %s 成功 %d / 失败 %d"
                 % (ok, fail, skip, pkgs, files, _tag, wav_ok, wav_fail))
        if tracks:
            self.log("🎧 共产出 %d 首 %s，右侧「试听」可直接播放" % (tracks, _tag))
            self.log("📁 %s 输出目录：%s" % (_tag, _dir))
        self._refresh_disk()
        self._drop_cv_states()

    def _drop_cv_states(self):
        """收尾后把本轮转换写的「🎵 已转 X」从运行期状态里摘掉。

        为什么：``cv_file_done`` 写的是**这一趟转了什么**（WAV 或 FLAC），
        而用户真正想知道的是「这个游戏现在手上有什么」。后者只有磁盘快照
        答得上来 —— ``_disk_state()`` 同时看 ``WAV/`` 和 ``FLAC/``，能说出
        「🎵 已转 WAV+FLAC」。留着运行期那条，状态列就永远停在先转的那一种，
        用户会以为第二种没转成（v5.9.9 实测踩到）。

        只摘「🎵 已转 …」这一种：``⚠ 转换失败`` / ``❌ 还没下载`` 这类是
        磁盘看不出来的信息，摘掉就丢了。
        """
        with self.lock:
            for u in list(self._cv_urls):
                if str(self.states.get(u) or "").startswith("🎵 已转"):
                    self.states.pop(u, None)
            self._cv_urls = set()

    # -- 打开文件夹 ---------------------------------------------------------
    def open_dir_target(self):
        """「打开文件夹」该开哪个目录 → ``(路径, 种类, 提示)``。

        **v6.0.1 起优先打开「机种文件夹」（= 保存目录本身）**，不再直接跳到
        ``WAV``。用户的原话：「打开文件夹现在指向游戏的 WAV 文件夹，改为指向
        游戏所对应的机种文件夹」—— 点这颗按钮的动机多半是「我下到哪儿了、
        压缩包在不在、这个机种还差哪几个游戏」，而不是「看产物」。产物在
        ``<机种>/WAV/<游戏名>/`` 下面一层，从机种目录点进去一样找得到；
        反过来从 ``WAV`` 退回机种目录却要多点一次「向上」。

        落点按优先级：

        1. ``保存目录`` 本身 —— 就是**机种文件夹**（``<根>/<站>/<机种名>``，
           见 :meth:`machine_dir`）。正常情况都走这一条；
        1b. ``<保存目录>/WAV`` / ``/FLAC`` / ``/MP3`` / ``/OGG`` —— 只有在
           保存目录**不存在**、但音频子目录还在这种反常情况下兜底；
        2. 保存目录的**上一级**（``...\\joshw``）—— 第一次用、目录还没建出来
           时也能打开，让用户看见别的机种都下到哪儿了。

        ⚠ 早先写的是 ``dl.wav_dir or dl.dest``：``wav_dir`` 在 ``start()`` 里
        就冻成 ``<保存目录>/WAV`` 了，**哪怕这个目录从来没被创建**。于是
        「没勾自动转 WAV」或「第一次用还没下载」时点这颗按钮就是
        「目录不存在：…\\Game Boy Advance\\WAV」—— 用户报的就是这个。

        找不到任何可用目录时返回 ``("", "", 原因)``。
        """
        with self.lock:
            wav = str(self.wav_dir or "")
            flac = str(self.flac_dir or "")
            mp3 = str(self.mp3_dir or "")
            ogg = str(self.ogg_dir or "")
            dest = str(self.dest or "")
        if not wav and dest:
            wav = os.path.join(dest, "WAV")
        if not flac and dest:
            flac = os.path.join(dest, "FLAC")
        if not mp3 and dest:
            mp3 = os.path.join(dest, "MP3")
        if not ogg and dest:
            ogg = os.path.join(dest, "OGG")
        cands = []
        # ① 机种目录优先（v6.0.1 按用户要求改）
        if dest:
            cands.append((dest, "dest"))
        if wav:
            cands.append((wav, "wav"))
        if flac:
            cands.append((flac, "flac"))
        if mp3:
            cands.append((mp3, "mp3"))
        if ogg:
            cands.append((ogg, "ogg"))
        if dest:
            up = os.path.dirname(os.path.abspath(dest))
            # 只往上退一级：退太多会退到盘根（D:\），那对用户没意义
            if up and os.path.dirname(up) != up:
                cands.append((up, "up"))
        seen = set()
        for path, tag in cands:
            try:
                key = os.path.abspath(path).lower()
            except (ValueError, OSError):
                continue
            if key in seen:
                continue
            seen.add(key)
            if os.path.isdir(path):
                note = ""
                if tag == "wav":
                    note = "机种目录还没建出来，已打开 WAV 目录"
                elif tag in ("flac", "mp3", "ogg"):
                    note = "机种目录还没建出来，已打开 %s 目录" % tag.upper()
                elif tag == "up":
                    note = "保存目录还没建出来，已打开上级目录（开始下载后自动创建）"
                return path, tag, note
        return "", "", "保存目录还没建出来：%s" % (dest or "（未设置）")

    # -- 试听（下载页播放转好的 WAV）----------------------------------------
    @staticmethod
    def _norm_fmt(fmt):
        """``"flac"`` / ``"mp3"`` / ``"ogg"`` / ``"wav"``，其余一律当 wav
        （大小写不敏感）。

        v5.9.15：加 mp3。白名单必须**收口在这里** —— 下游
        ``ConvertWorker`` / ``spc_wav.write_tracks`` / ``flac_out.encode_pcm``
        都按这个值走，多一个没登记的字符串会在写盘那一步才炸。
        """
        f = str(fmt or "").strip().lower()
        return f if f in _FMT_AUDIO else "wav"

    def _out_dir(self, fmt, dest=None):
        """某种格式的输出根目录（绝对路径）。三种格式 **各一个目录**。

        ``<保存目录>/WAV``、``/FLAC``、``/MP3``，里面再按游戏名分子文件夹。

        为什么必须分开（v5.9.9）：
        ``dl_convert.ConvertManifest.already_done()`` 判的是「这个**源文件**
        转过没有」，而记账文件 ``.msu1-convert.json`` 放在**输出目录**里。
        两种格式共用一个目录时，先转 WAV 记下 ``01_xx.wav``，再转 FLAC 时
        ``already_done()`` 发现产物还在 → 直接跳过，一个 FLAC 都不出
        （用户报的正是这个）。分开目录 = 各记各的 = 互不干扰。
        """
        f = self._norm_fmt(fmt)
        if dest is None:
            with self.lock:
                dest = str(self.dest or "")
        if not dest:
            return ""
        return os.path.abspath(os.path.join(dest, _FMT_DIRS[f]))

    def _audio_roots_now(self):
        """当前**实际存在**的音频根目录列表 ``[(绝对路径, "wav"|"flac"|"mp3"), ...]``。

        试听列表 / 状态快照 / 清空 / 上传云盘都要同时看三个目录 ——
        只看 WAV 的话，选了 FLAC/MP3 的用户会觉得「转了但一首都没有」。
        目录不存在就不返回（试听列表因此能自然区分「没转过」和「转过 WAV」）。
        """
        with self.lock:
            dest = str(self.dest or "")
            wav = str(self.wav_dir or "")
            flac = str(self.flac_dir or "")
            mp3 = str(self.mp3_dir or "")
            ogg = str(self.ogg_dir or "")
        roots = []
        wav = wav or (os.path.join(dest, "WAV") if dest else "")
        flac = flac or (os.path.join(dest, "FLAC") if dest else "")
        mp3 = mp3 or (os.path.join(dest, "MP3") if dest else "")
        ogg = ogg or (os.path.join(dest, "OGG") if dest else "")
        for path, tag in ((wav, "wav"), (flac, "flac"), (mp3, "mp3"),
                          (ogg, "ogg")):
            if not path:
                continue
            try:
                p = os.path.abspath(path)
            except (OSError, ValueError):
                continue
            if os.path.isdir(p):
                roots.append((p, tag))
        return roots

    def _wav_dir_now(self):
        """当前 **WAV** 根目录（绝对路径）。

        ``self.wav_dir`` 只在 ``start()`` 里冻结 —— 程序刚起来、还没下载过时
        它是空的。这时按「保存目录 + WAV」推一个，让用户能试听上一次的产出
        （tkinter 版就是这样：下载页随时能放已转好的 WAV）。

        ⚠ 这是**单数**版本，只代表 WAV。要看两种格式请用
        :meth:`_audio_roots_now`。
        """
        with self.lock:
            d = str(self.wav_dir or "")
            dest = str(self.dest or "")
        if d:
            return os.path.abspath(d)
        return os.path.abspath(os.path.join(dest, "WAV")) if dest else ""

    # -- 试听列表的「清空」--------------------------------------------------
    @staticmethod
    def _state_dir():
        """用户状态目录 ``%LOCALAPPDATA%\\MSU1Converter``（拿不到返回 ""）。

        和 tkinter 版的 ``ui_theme.json`` / ``joshw_cache`` 同一个地方 ——
        程序目录可能装在 Program Files（不可写），用户状态不能往那儿放。
        """
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        if not base:
            return ""
        d = os.path.join(base, "MSU1Converter")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return ""
        return d

    def _load_wav_cleared(self):
        """读「清空时刻」记录 → ``{根目录小写: 时间戳}``。

        文件不存在 / 坏掉 / 权限不足，一律返回空 dict —— 试听列表的记忆
        不值得为它弹一个错误框（读不出来最多是「清空没记住」）。
        """
        d = self._state_dir()
        if not d:
            return {}
        try:
            with open(os.path.join(d, WAV_CLEAR_FILE), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out = {}
        for k, v in data.items():
            try:
                out[str(k).lower()] = float(v)
            except (TypeError, ValueError):
                continue
        return out

    def _save_wav_cleared(self):
        """把「清空时刻」写回磁盘（失败就算了，不影响本次清空）。"""
        d = self._state_dir()
        if not d:
            return
        with self.lock:
            data = dict(self._wav_cleared)
        tmp = os.path.join(d, WAV_CLEAR_FILE + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, os.path.join(d, WAV_CLEAR_FILE))
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    # -- 试听列表的「手动加入」----------------------------------------------
    def _load_wav_picks(self):
        """读「用户点名要听」的曲目 → ``{根目录小写: {rel 小写, ...}}``。

        坏文件 / 没权限一律当空 —— 这份记录丢了最多是「要重新点一次加入」，
        不值得为它弹错误。
        """
        d = self._state_dir()
        if not d:
            return {}
        try:
            with open(os.path.join(d, WAV_PICKS_FILE), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out = {}
        for root, rels in data.items():
            if not isinstance(rels, (list, tuple)):
                continue
            got = set()
            for r in rels:
                r = str(r or "").replace("\\", "/").strip()
                if r:
                    got.add(r.lower())
            if got:
                out[str(root).lower()] = got
        return out

    def _save_wav_picks(self):
        """把「手动加入」写回磁盘（失败就算了，不影响本次）。"""
        d = self._state_dir()
        if not d:
            return
        with self.lock:
            data = {k: sorted(v) for k, v in self._wav_picks.items() if v}
        tmp = os.path.join(d, WAV_PICKS_FILE + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, os.path.join(d, WAV_PICKS_FILE))
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def wav_clear(self):
        """清空试听列表 —— **只隐藏，不删文件**。

        为什么不是删文件：这张列表是「刚转出来的 WAV」的入口，而 WAV 本身就是
        用户要拿去用的产物（上传云盘、拖进播放器、压成 MSU-1 包）。点一下
        「清空」就把几百 MB 删掉是不可逆的，而且「清空列表」这句话也没有
        「删文件」的意思。真要删，资源管理器就在旁边（「打开文件夹」）。

        做法：按 WAV 根目录记一个**时刻**，列表只显示 mtime 晚于它的文件。
        于是三件事同时成立：

        * 清空后新转的照样出现（用户要的就是这个）；
        * 同一首重新转一遍会回来（mtime 变了）—— 比记「隐藏名单」合理，
          名单会把重转的也一起藏掉，看着像坏了；
        * 换机种各算各的（保存目录不同 = 根目录不同）。
        """
        roots = self._audio_roots_now()
        if not roots:
            return {"ok": False, "error": "还没有音频目录", "hidden": 0}
        # 两个根合成一个签名当 key —— 与 wav_list() 同一口径，
        # 「清空」一次把 WAV 和 FLAC 都清掉（用户说的清空就是都别显示）。
        key = "|".join(sorted(os.path.abspath(r).lower() for r, _t in roots))
        with self.lock:
            prev = float(self._wav_cleared.get(key, 0.0))
            picked = self._wav_picks.get(key) or set()
        # 先数一下「这次真的清掉几首」，好让前端说清结果（不是「操作成功」
        # 这种废话）。只数**当前还看得见**的 —— 已经清过一次的不能再算进去，
        # 否则连点两次会报出「清空 300 首」，其实第二次一首也没清。
        hidden = 0
        for root, tag in roots:
            ext = _FMT_EXTS.get(tag, ".wav")
            for dirpath, dirnames, files in os.walk(root):
                dirnames.sort()
                for n in files:
                    if not n.lower().endswith(ext):
                        continue
                    rel = os.path.relpath(os.path.join(dirpath, n), root)
                    rel = rel.replace("\\", "/").lower()
                    if rel not in picked:
                        try:
                            if os.stat(os.path.join(dirpath, n)).st_mtime <= prev:
                                continue
                        except OSError:
                            continue
                    hidden += 1
        with self.lock:
            self._wav_cleared[key] = time.time()
            # 手动加入的也一起清掉 —— 「清空」就是空。用户再点「加入试听」
            # 就回来了，不需要为此保留一份「清不掉的东西」。
            self._wav_picks.pop(key, None)
            self._wav_cache = None            # 缓存必须失效，否则前端拿到的还是旧列表
        self._save_wav_cleared()
        self._save_wav_picks()
        return {"ok": True, "dir": roots[0][0], "hidden": hidden, "error": ""}

    def wav_list(self, force=False):
        """列出**两个音频根**下转好的文件（递归，新的在前，**已清空的不列**）。

        返回 ``{"ok", "dir", "dirs", "total", "wavs": [...], "cleared",
        "hidden"}``；每项::

            {"rel": "FF7/01_Stage Clear.wav", "name": "...", "game": "FF7",
             "fmt": "wav", "size": 123456, "size_text": "120.6 KB",
             "mtime": 1.7e9}

        ``rel`` 是相对**该格式根目录**的路径（WAV 相对 ``WAV/``、FLAC 相对
        ``FLAC/``），前端拿它去 ``/api/dl/audio?rel=`` —— **不传绝对路径**，
        免得到处开洞。后端按扩展名认根（见 :meth:`wav_path`）。

        ⚠ v5.9.9：以前只扫 ``WAV/`` 且只认 ``.wav``。现在两种格式分目录存，
        只扫一个根的话，选了 FLAC 的用户会觉得「转了但一首都没有」。
        """
        roots = self._audio_roots_now()
        if not roots:
            with self.lock:
                dest = str(self.dest or "")
            return {"ok": True, "dir": "", "dirs": {}, "total": 0, "wavs": [],
                    "cleared": False, "hidden": 0,
                    "error": "" if dest else "还没有保存目录"}
        now = time.time()
        # 缓存 / 清空时刻 / 手动加入都用**两个根合起来的签名**做 key ——
        # 切换机种会同时改两个根，只比一个根的话另一半会串台。
        sig = "|".join(sorted(os.path.abspath(r).lower() for r, _t in roots))
        with self.lock:
            cache = self._wav_cache
            cut = float(self._wav_cleared.get(sig, 0.0))
            picks = set(self._wav_picks.get(sig) or ())
        # ⚠ 缓存必须**连目录一起比**：切机种会改 dest，根目录跟着变，
        #   只比时间的话新机种会拿到上一个机种的列表（实测：列表还在、
        #   点播放却 404）。
        # ⚠ 清空时刻 / 手动加入变了都要失效缓存 —— 见 wav_clear()/wav_add()。
        if not force and cache and cache[1] == sig and now - cache[0] < WAV_SCAN_TTL:
            return cache[2]
        items = []
        hidden = 0
        for root, tag in roots:
            ext = _FMT_EXTS.get(tag, ".wav")
            for dirpath, dirnames, files in os.walk(root):
                dirnames.sort()
                for n in files:
                    if not n.lower().endswith(ext):
                        continue
                    full = os.path.join(dirpath, n)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    rel = os.path.relpath(full, root).replace("\\", "/")
                    # 两道放行：比清空时刻新（自动），或者用户点名要听（手动）。
                    # 手动那条不看 mtime —— 清空之后再加进来的曲目，文件本身
                    # 当然是旧的，按 mtime 判会一首都不显示。
                    if cut and st.st_mtime <= cut and rel.lower() not in picks:
                        hidden += 1
                        continue
                    items.append({
                        "rel": rel,
                        "name": n,
                        "game": (os.path.basename(dirpath)
                                 if dirpath != root else ""),
                        "fmt": tag,
                        "size": st.st_size,
                        "size_text": _human(st.st_size),
                        "mtime": st.st_mtime,
                    })
        # 新的在前：刚转完的那首就在最上面，不用翻
        items.sort(key=lambda e: (-e["mtime"], e["rel"]))
        out = {"ok": True, "dir": roots[0][0], "total": len(items),
               "dirs": {tag: root for root, tag in roots},
               "wavs": items[:WAV_LIST_MAX], "error": "",
               "cleared": bool(cut), "hidden": hidden,
               "picked": len(picks)}
        with self.lock:
            self._wav_cache = (now, sig, out)
        return out

    def wav_path(self, rel):
        """``rel`` → 绝对路径；**越界 / 非音频 / 不存在一律 None**。

        ⚠ 这是唯一一处「前端给路径、后端开文件」的地方，必须挡住
        ``../../Windows/System32/...`` 这种穿越。用 ``commonpath`` 比
        字符串 ``startswith`` 可靠：``WAV_backup`` 不会被误判成 ``WAV`` 里。

        v5.9.9：音频现在有两个根（``WAV/`` 和 ``FLAC/``），``rel`` 是**相对
        各自根**的路径。靠扩展名认根 —— ``.wav`` 找 WAV 根、``.flac`` 找
        FLAC 根。这样前端不用给 rel 加格式前缀，两边的路径规则完全一致。
        v5.9.15：再加 ``.mp3`` → MP3 根。
        """
        rel = str(rel or "").replace("\\", "/").lstrip("/")
        if not rel:
            return None
        low = rel.lower()
        want = ""
        for _tag, _ext in _FMT_EXTS.items():
            if low.endswith(_ext):
                want = _tag
                break
        if not want:
            return None
        for root, tag in self._audio_roots_now():
            if tag != want:
                continue
            root_abs = os.path.abspath(root)
            try:
                full = os.path.abspath(os.path.join(root_abs, rel))
                if os.path.commonpath([root_abs.lower(), full.lower()]) \
                        != root_abs.lower():
                    continue
            except (ValueError, OSError):
                continue
            if os.path.isfile(full):
                return full
        return None

    # -- 封面（v6.0.1：试听时显示对应游戏的封面）----------------------------
    def cover_paths(self, game):
        """某个游戏的**全部**封面图绝对路径；没有就返回 ``[]``。

        v6.0.7：用户要求「一个游戏的封面资源应该有若干个…试听时轮播」。

        查找顺序（都只看**本地已有**的文件，绝不联网）：

        1. 封面缓存（``%LOCALAPPDATA%\\MSU1Converter\\covers\\``，按游戏名
           归一化后查）—— **顺序就是展示顺序**，盒绘在前、截图在后；
        2. 缓存空了才退回 ``<保存目录>/<FLAC|MP3|OGG|WAV>/<游戏名>/cover.png``
           —— 用户手删过缓存、但游戏目录里还留着图的情况。

        ⚠ 反过来了：v6.0.6 是「先看输出目录再看缓存」，现在**先看缓存**。
        因为输出目录只有一张 ``cover.png``，先取它会让轮播第一张和第二张
        是同一张图（缓存里那张就是它的源）。缓存为空时才用它兜底。

        ⚠ ``game`` 是**前端给的字符串**（来自磁盘目录名），必须挡住
        ``..`` / 路径分隔符：这里虽然是只读，但 ``..\\..\\Windows\\x`` 拼进
        去就能读任意目录下的 ``cover.png``。判据取「归一化后不等于原名」，
        比黑名单可靠。
        """
        g = str(game or "").strip()
        if not g or g in (".", ".."):
            return []
        if os.path.basename(g) != g or "/" in g or "\\" in g:
            return []
        out = []
        if cover_art is not None:
            try:
                out = [p for p, _kind in cover_art.covers_for_title(g)]
            except Exception:                    # noqa: BLE001
                out = []
        if out:
            return out
        with self.lock:
            dest = str(self.dest or "")
        if dest:
            for _tag in ("flac", "mp3", "ogg", "wav"):
                p = os.path.join(dest, _FMT_DIRS[_tag], g, _COVER_NAME)
                if os.path.isfile(p):
                    return [p]
        return []

    def cover_path(self, game):
        """某个游戏的第一张封面（兼容旧调用，只取一张）。"""
        got = self.cover_paths(game)
        return got[0] if got else ""

    # -- 单条「转 WAV」/「加入试听」（v5.9.4）--------------------------------
    def _entry_of(self, url):
        """url → 条目 dict（找不到 None）。"""
        with self.lock:
            for e in self.entries:
                if e.get("url") == url:
                    return dict(e)
        return None

    def _archive_of(self, url):
        """url → 该条目**应该**落盘的压缩包绝对路径（不保证存在）。

        和 ``start()`` 里 ``target = os.path.join(dest_root, self._file_name(...))``
        必须同一套规则，否则「转 WAV」会去解一个不存在的文件。
        zophar 的条目 url 是游戏页，真实直链在 ``_zip_of`` 里。
        """
        e = self._entry_of(url)
        if not e:
            return ""
        real = self._zip_of.get(url) or url
        with self.lock:
            dest = str(self.dest or "")
        if not dest:
            return ""
        return os.path.join(dest, self._file_name(e.get("name"), real))

    def _wav_folder_of(self, url):
        """url → ``WAV/<游戏名>`` 的绝对路径（**不保证存在**）。

        游戏名 = 包名去扩展名后按 ``ConvertWorker._folder_name`` 清洗 ——
        与转换器写盘时用的规则是同一套（见 ``_wav_folder_name``）。

        ⚠ v5.9.9：只给 **WAV** 那一个。要看两种格式请用
        :meth:`_audio_folders_of`。
        """
        arch = self._archive_of(url)
        if not arch:
            return ""
        stem = os.path.splitext(os.path.basename(arch))[0]
        root = self._wav_dir_now()
        if not root:
            return ""
        return os.path.join(root, _wav_folder_name(stem))

    def _audio_folders_of(self, url):
        """url → ``[(绝对目录, 根目录, "wav"|"flac"), ...]``（**不保证存在**）。

        v5.9.9：同一个游戏在两种格式下各有一个子文件夹，名字相同、
        父目录不同。试听/加入列表要两个都看。
        """
        arch = self._archive_of(url)
        if not arch:
            return []
        stem = os.path.splitext(os.path.basename(arch))[0]
        name = _wav_folder_name(stem)
        out = []
        for root, tag in self._audio_roots_now():
            out.append((os.path.join(root, name), root, tag))
        return out

    def wav_add(self, urls=None):
        """把某几个游戏**已经转好的**音频加入试听列表（用户点名要听）。

        为什么需要这个入口：试听列表原先只有「mtime 比清空时刻新」一种来源，
        于是清空之后列表就永远空了 —— 用户没法把自己想听的那几个游戏挑回来。

        做法是记下这些曲目的 ``rel``（落盘在 ``wav_picks.json``），
        ``wav_list()`` 对它们**不看 mtime**，直接放行。于是：

        * 清空 → 空列表 → 点「加入试听」→ 又回来了；
        * 重开程序照样在（不像内存标记那样一退就没）；
        * 清空会把这份记录一起清掉（「清空」就该是空，不是「清掉一半」）。

        v5.9.9：两种格式的子文件夹都找一遍。同一个游戏只转了 FLAC 时
        也算「转好了」—— 以前只找 ``WAV/<游戏>``，那种情况下会误报
        「这些游戏还没转出 WAV」。
        """
        roots = self._audio_roots_now()
        if not roots:
            return {"ok": False, "error": "还没有音频目录", "added": 0}
        key = "|".join(sorted(os.path.abspath(r).lower() for r, _t in roots))
        added, games, missing = 0, [], []
        fresh = set()
        for u in (urls or []):
            n = 0
            for folder, root, tag in self._audio_folders_of(u):
                if not folder or not os.path.isdir(folder):
                    continue
                ext = _FMT_EXTS.get(tag, ".wav")
                for dirpath, dirnames, files in os.walk(folder):
                    dirnames.sort()
                    for fn in files:
                        if not fn.lower().endswith(ext):
                            continue
                        rel = os.path.relpath(os.path.join(dirpath, fn), root)
                        fresh.add(rel.replace("\\", "/").lower())
                        n += 1
            if n:
                added += n
                games.append(self._name_of(u))
            else:
                missing.append(self._name_of(u))
        if fresh:
            with self.lock:
                cur = self._wav_picks.setdefault(key, set())
                before = len(cur)
                cur |= fresh
                real_added = len(cur) - before
                self._wav_cache = None
            self._save_wav_picks()
        else:
            real_added = 0
        if not fresh:
            return {"ok": False, "added": 0, "games": games,
                    "error": "这些游戏还没转出音频" if missing else "没有选中任何游戏",
                    "missing": missing}
        self.log("🎧 已加入试听：%s（%d 首）"
                 % ("、".join(games[:3]) + ("…" if len(games) > 3 else ""), added))
        return {"ok": True, "added": added, "new": real_added,
                "games": games, "missing": missing, "error": ""}

    def wav_games(self):
        """两个音频根下**已经转好**的游戏清单（上传云盘时让用户挑）。

        返回 ``{"ok", "dir", "dirs", "games": [{game, count, size,
        size_text, fmts, fmt_text}], "total", "bytes", "bytes_text"}``，
        按游戏名排序。

        v5.9.9：同时扫 ``WAV/`` 和 ``FLAC/``。同一个游戏两种格式都有时
        **合成一条**（``count`` 是文件总数、``size`` 是两者之和），
        ``fmts`` 列出 ``["flac", "wav"]``、``fmt_text`` 给界面直接显示
        ``WAV+FLAC``。合成一条的理由：上传云盘是按「游戏」挑的，同一个游戏
        拆成两行会让用户以为要传两遍。

        ⚠ **不能走 ``wav_list()``**：那张表受「清空试听列表」的时刻约束，
        而「清空试听」只是不想听了，文件还在、照样该能上传。走 wav_list
        的话用户清一次列表，上传对话框就会说「还没有转好的 WAV」——
        正是这次要修的那个 bug 的另一种形态（实测踩到）。
        """
        roots = self._audio_roots_now()
        if not roots:
            with self.lock:
                dest = str(self.dest or "")
            return {"ok": True, "dir": "", "dirs": {}, "games": [], "total": 0,
                    "bytes": 0, "bytes_text": "0 B",
                    "error": "" if dest else "还没有保存目录"}
        agg = {}
        for root, tag in roots:
            ext = _FMT_EXTS.get(tag, ".wav")
            for dirpath, dirnames, files in os.walk(root):
                dirnames.sort()
                rel_dir = os.path.relpath(dirpath, root)
                game = "" if rel_dir == "." else rel_dir.replace("\\", "/")
                for n in sorted(files):
                    if not n.lower().endswith(ext):
                        continue
                    try:
                        nbytes = os.path.getsize(os.path.join(dirpath, n))
                    except OSError:
                        continue
                    cur = agg.setdefault(
                        game, {"game": game, "count": 0, "size": 0, "fmts": []})
                    cur["count"] += 1
                    cur["size"] += nbytes
                    if tag not in cur["fmts"]:
                        cur["fmts"].append(tag)
        games = sorted(agg.values(), key=lambda x: x["game"].lower())
        for g in games:
            g["size_text"] = _human(g["size"])
            g["fmts"].sort()
            g["fmt_text"] = "+".join(f.upper() for f in g["fmts"])
        total = sum(g["size"] for g in games)
        return {"ok": True, "dir": roots[0][0],
                "dirs": {tag: root for root, tag in roots}, "games": games,
                "total": sum(g["count"] for g in games),
                "bytes": total, "bytes_text": _human(total),
                "error": "" if games else "还没有转好的音频"}

    def convert_wav(self, urls=None, fmt=None):
        """把「已下载 / 已解压但还没转**这个格式**」的条目单独排进转换队列。

        用户的原话是「对已经下载、解压完成但没有转 wav 的，增加额外的转 wav
        按键」—— 因为「下载后自动转 WAV」是个**下载前**的选项，勾晚了就再也
        没有入口，只能把整个机种重下一遍（几百 MB）才触发转换。

        v5.9.9 起格式由 ``fmt`` 显式指定（底部那两颗「转 WAV」/「转 FLAC」
        按钮各传各的），不再从 ``opts["auto_flac"]`` 猜。同一个游戏可以先转
        WAV 再转 FLAC —— 因为两种格式的产物落在**不同目录**，各自一份转换
        记账，``already_done()`` 只看自己那份，不会互相判「已转过」。

        不下载、不解压（除非包还在、产物不在）、不改选项，只做转换：

        1. 包已解压 → 直接拿产物文件入队；
        2. 包在但没解压 → 先解压再入队（复用 ``joshw_dl.extract_archive``）；
        3. 两者都没有 → 这一条报「还没下载」，其余照转。

        转换 worker 是**新建的**（``self.cv`` 可能为 None：程序刚重启），
        跑完自己退出，收尾日志按「只转不下载」的口径写。

        Args:
            urls: 要转换的条目 url 列表。
            fmt: ``"wav"`` / ``"flac"`` / ``"mp3"`` / ``"ogg"``；``None`` 时按 ``opts`` 里
                三个自动转开关推一个（老调用方和门禁仍按这个走）。
        """
        if dl_convert is None:
            return {"ok": False, "error": "转换模块未就绪（缺少 dl_convert）"}
        urls = [u for u in (urls or []) if u]
        if not urls:
            return {"ok": False, "error": "没有选中任何游戏"}
        with self.lock:
            if self.busy:
                return {"ok": False, "error": "正在下载/转换中，等它跑完再试"}
            dest = str(self.dest or "")
            # v5.9.9：格式由调用方说了算。没传才退回选项（兼容旧调用）。
            if fmt is None:
                fmt = self._opts_fmt()
            fmt = self._norm_fmt(fmt)
            merge = bool(self.opts.get("merge_wav")) and fmt == "wav"
            self._cv_fmt = fmt
            self.busy = True
            self.phase = "converting"
            self.progress = 0.0
            self._finished = False
            self.cv_queued = 0
            self.cv_ok = 0
            self.cv_fail = 0
            self.cv_skip = 0
            self.cv_bytes = 0
            self.cv_tracks = 0
            self.cv_current = ""
            self._cv_urls = set()
            self.cancel = threading.Event()
            cancel = self.cancel
            out_root = self._out_dir(fmt, dest)
            # 记账用的三个字段都指向本次的输出根 —— 收尾/状态列靠它们。
            # v6.0.0：改成查表 —— 以前是 if/elif/else，加第四种格式时
            # 很容易漏一支，表现是「转了 OGG，状态列却指着 WAV 目录」。
            _attr = {"flac": "flac_dir", "mp3": "mp3_dir",
                     "ogg": "ogg_dir"}.get(fmt)
            if _attr:
                setattr(self, _attr, out_root)
            else:
                self.wav_dir = out_root
            self._cv_oneshot = True
        if not dest:
            with self.lock:
                self.busy = False
                self.phase = "idle"
                self._cv_oneshot = False
            return {"ok": False, "error": "还没设置保存目录"}
        # v6.0.2：勾了「多轨合并成单文件」却选了 FLAC / MP3 时，合并是**不做**的
        # （``merge_wav`` 只对 WAV 生效）。以前这里一声不响 —— 用户拿到 31 个
        # 分轨、一个整轨都没有，只会以为功能坏了。说一句比默默跳过强。
        if merge is False and self.opts.get("merge_wav") and fmt != "wav":
            self.log("ℹ 「多轨合并成单文件」只对 WAV 生效，本次转 %s 不合并"
                     % fmt.upper())
        jobs = [(u, self._archive_of(u)) for u in urls]
        _tag = fmt.upper()
        for u, _a in jobs:
            self._set_state(u, "⏳ 待转 " + _tag)
        try:
            self.cv = dl_convert.ConvertWorker(
                out_root, self._on_cv_event, cancel,
                merge_wav=merge, group_by_game=True, fmt=fmt)
            self.cv.start()
        except Exception as exc:                     # noqa: BLE001
            with self.lock:
                self.busy = False
                self.phase = "idle"
                self._cv_oneshot = False
                self.cv = None
            return {"ok": False, "error": "启动转换失败：%s" % exc}
        threading.Thread(target=self._cv_oneshot_thread,
                         args=(jobs, dest), daemon=True).start()
        return {"ok": True, "count": len(jobs), "fmt": fmt, "dir": out_root}

    def fetch_covers(self, urls=None):
        """手动「下载封面」：给**勾选的条目**抓封面，不下载音源、不转码。

        v6.0.4：原来是「顺带下载封面」复选框（默认开，下载时自动抓）。用户
        要求改成按钮 ——「可主动选择什么时候下载封面」。所以口径变成：

        * 下载流程**不再**自动抓图（``start()`` 里那段已删）；
        * 联网抓图只剩两个入口：这颗按钮，以及「转 FLAC / MP3 前补图」
          （后者是必要的 —— 图要嵌进文件，不抓就永远没封面，见 v5.9.16）；
        * 抓回来的图进封面缓存，并复制一份 ``cover.png`` 到**已存在**的
          ``<格式>/<游戏>/`` 目录；已经转好的 FLAC / MP3 由
          ``_backfill_audio`` 补图（绕开转换记账，否则「先转码后要封面」
          的用户再点一次「转 FLAC」也补不上）。

        v6.0.5：抓图变成**双源**。以前 Switch / FM Towns / PC 三个机种在
        libretro-thumbnails 里没有目录，点这颗按钮会被直接拒绝；现在它们
        由 IGDB 兜底（见 ``cover_igdb``），判断也从「libretro 支不支持」
        改成 ``_has_cover_source`` 的并集。

        Args:
            urls: 要抓封面的条目 url 列表（前端传当前勾选的）。

        Returns:
            ``{"ok": True, "count": n}`` 或 ``{"ok": False, "error": "..."}``。
        """
        if cover_art is None:
            return {"ok": False, "error": "封面模块未就绪（缺少 cover_art）"}
        urls = [u for u in (urls or []) if u]
        if not urls:
            return {"ok": False, "error": "没有选中任何游戏"}
        with self.lock:
            if self.busy:
                return {"ok": False, "error": "正在下载/转换中，等它跑完再试"}
            key = str(self.key or "")
            name_of = {e.get("url"): str(e.get("name") or "")
                       for e in self.entries}
        if not key:
            return {"ok": False, "error": "还没选机种"}
        if not _has_cover_source(key):
            return {"ok": False,
                    "error": "%s 在两个封面源（libretro-thumbnails / IGDB）里"
                             "都没有对应平台，抓不到图（音源本身不受影响）"
                             % (self.name or key)}
        names = []
        for u in urls:
            n = name_of.get(u, "")
            if n and n not in names:
                names.append(n)
        if not names:
            return {"ok": False,
                    "error": "勾选的游戏不在当前机种的列表里，先刷新一下列表"}

        cancel = threading.Event()
        with self.lock:
            self.cover_total = len(names)
            self.cover_done = 0
            self.cover_ok = 0
            self.cover_fail = 0
            self.cover_current = ""
        threading.Thread(target=self._cover_fetch_thread,
                         args=(key, names, cancel), daemon=True).start()
        return {"ok": True, "count": len(names), "platform": key}

    def _cover_fetch_thread(self, key, names, cancel):
        """后台线程：抓图 → 落 ``cover.png`` 到各格式目录 → 给已有音频补图。

        和 ``_cover_worker`` 一样**绝不抛异常**（封面是锦上添花），也绝不动
        ``phase`` / ``busy`` —— 那是下载和转换的状态机。
        """
        try:
            self._cover_worker([(key, n) for n in names], cancel)
            self._place_covers(names)
        except Exception as exc:                     # noqa: BLE001
            self.log("⚠ 封面处理失败（不影响音源和转码）：%s" % exc)

    def _place_covers(self, names):
        """把缓存里的封面落一份到 ``<格式>/<游戏>/cover.png`` 并回填音频。

        只动**参数里点名的游戏**（用户勾选的那些）—— 不顺手处理整个目录，
        否则「只想给 3 个游戏抓图」会变成给 200 个游戏改文件。
        """
        if cover_art is None:
            return
        want = set(names)
        with self.lock:
            dest = str(self.dest or "")
        if not dest:
            return
        placed = 0
        for fmt in ("flac", "mp3", "ogg", "wav"):
            root = os.path.join(dest, _FMT_DIRS[fmt])
            if not os.path.isdir(root):
                continue
            for g in sorted(os.listdir(root)):
                if g not in want:
                    continue
                gdir = os.path.join(root, g)
                if not os.path.isdir(gdir):
                    continue
                had = os.path.isfile(os.path.join(gdir, _COVER_NAME))
                try:
                    if self._cover_for_dir(gdir, g) and not had:
                        placed += 1
                except Exception:                    # noqa: BLE001
                    continue
        if placed:
            self.log("🖼 已把封面放进 %d 个游戏目录（cover.png）" % placed)
        for fmt in ("flac", "mp3"):
            root = os.path.join(dest, _FMT_DIRS[fmt])
            n = self._backfill_audio(root, only=want)
            if n:
                self.log("🖼 已给 %d 个此前转好的 %s 补上封面"
                         % (n, fmt.upper()))

    @staticmethod
    def _convertible(path):
        """这个文件能不能转 WAV。``dl_convert`` 没打进来时一律 False。"""
        if dl_convert is None or not path:
            return False
        try:
            return bool(dl_convert.is_convertible(path))
        except Exception:                            # noqa: BLE001
            return False

    def _payload_in(self, extract_dir):
        """解压目录里所有**可转**的文件（递归）。目录不在返回空表。

        ⚠ 这条分支是 v5.9.4 补的：勾了「解压后删除原压缩包」的用户，
        包已经删了、只剩 ``<保存目录>/<包名>/``，只按包找会说「还没下载」——
        而文件其实都在，用户看到的是一颗「转 WAV」按钮点了没反应。

        v6.0.6：返回前过一遍 :func:`dl_convert.drop_derived_wavs` ——
        这个目录里通常同时躺着 ``xxx.fsb`` 和 ``xxx.fsb.wav``（前者是音源、
        后者是我们自己转出来的），两个都 ``is_convertible`` 为真。
        不去重就是「同一首转两遍」。
        """
        if not extract_dir or not os.path.isdir(extract_dir):
            return []
        out = []
        for dirpath, dirnames, files in os.walk(extract_dir):
            dirnames.sort()
            for n in sorted(files):
                p = os.path.join(dirpath, n)
                try:
                    if dl_convert.is_convertible(p):
                        out.append(p)
                except Exception:                    # noqa: BLE001
                    continue
        return dl_convert.drop_derived_wavs(out)

    @staticmethod
    def _has_picture(path):
        """这个音频文件里有没有封面。**不把整个文件读进内存**。

        v5.9.17 起交给 ``flac_out.has_picture`` 统一判 —— 它同时认 FLAC 的
        ``METADATA_BLOCK_PICTURE`` 和 MP3 的 ID3v2 ``APIC``，而且只读块头
        和标签区。这里保留一层薄封装是为了「``flac_out`` 没打进来」时
        仍能降级成 False，而不是抛异常把整批转换搞挂。
        """
        if flac_out is None:
            return False
        try:
            return bool(flac_out.has_picture(path))
        except Exception:                           # noqa: BLE001
            return False

    @staticmethod
    def _copy_cover(src, dst):
        """复制封面到 ``dst``（先写 ``.part`` 再 replace，别留半个文件）。"""
        tmp = dst + ".part"
        try:
            with open(src, "rb") as fh:
                data = fh.read()
            if not data:
                return False
            with open(tmp, "wb") as out:
                out.write(data)
            os.replace(tmp, dst)
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    def _cover_for_dir(self, gdir, game=""):
        """这个游戏目录该用哪张封面，**并保证目录里有 cover.png**。

        v5.9.16：用户要求「封面放在下载目录里 FLAC 文件夹内的对应游戏
        文件夹」。所以这里做两件事：

        1. 目录里已经有 ``cover.png`` → 直接用它（拷走目录也不丢封面）；
        2. 没有 → 从全局缓存查一张，**复制进来**再用。

        返回本地图片路径；都没有返回 ``""``。
        """
        local = os.path.join(gdir, _COVER_NAME)
        try:
            if os.path.isfile(local) and os.path.getsize(local) > 0:
                return local
        except OSError:
            pass
        if cover_art is None:
            return ""
        try:
            img = cover_art.cover_for_title(game or os.path.basename(gdir))
        except Exception:                           # noqa: BLE001
            img = ""
        if not img:
            return ""
        # 落副本失败（只读盘 / 权限）就退回缓存里的路径 —— 图还是要嵌的
        if self._copy_cover(img, local):
            return local
        return img

    def _backfill_audio(self, root, only=None):
        """给 ``root`` 下**已经存在**的 FLAC / MP3 补封面。返回补了几个。

        为什么需要单独回填：``ConvertWorker`` 有转换记账
        （``.msu1-convert.json``），已经转过的曲目会被判「已转过」跳过 ——
        所以「先转了 FLAC、事后才想到要封面」的用户，重新点一次「转 FLAC」
        也补不上图。这里绕开记账，直接改已有的文件。

        v5.9.17 起 MP3 也补（ID3v2 APIC）。

        幂等：已经有图片的文件跳过（``embed_picture`` / ``embed_id3_picture``
        本身也是替换不叠加，但先判一下能省掉几十 MB 的读写）。

        Args:
            only: 只处理这些游戏（目录名）的集合；``None`` = 整个 root。
                v6.0.4 加 —— 手动「下载封面」只该动用户勾选的那几个，
                不能顺手把 200 个游戏的 FLAC 全重写一遍。
        """
        if cover_art is None or flac_out is None:
            return 0
        if not root or not os.path.isdir(root):
            return 0
        n = 0
        for game in sorted(os.listdir(root)):
            if only is not None and game not in only:
                continue
            gdir = os.path.join(root, game)
            if not os.path.isdir(gdir):
                continue
            # ⚠ 口径必须和 ``spc_wav.write_tracks`` 完全一致：它也是
            #   「先看目录里的 cover.png，再退回按目录名查缓存」。
            img = self._cover_for_dir(gdir, game)
            if not img:
                continue
            for f in sorted(os.listdir(gdir)):
                low = f.lower()
                if low.endswith(".flac"):
                    embed = flac_out.embed_picture
                elif low.endswith(".mp3"):
                    embed = flac_out.embed_id3_picture
                else:
                    continue                        # WAV 没容器放图
                p = os.path.join(gdir, f)
                try:
                    if self._has_picture(p):
                        continue
                    ok, _why = embed(p, img)
                    if ok:
                        n += 1
                except Exception:                   # noqa: BLE001
                    continue
        return n

    def _covers_before_convert(self, jobs):
        """转码前：抓封面 + 给已存在的音频补图。**永不抛异常**。

        只在转 **FLAC / MP3** 时做 —— 这两种容器都能带封面
        （FLAC 走 ``METADATA_BLOCK_PICTURE``，MP3 走 ID3v2 的 ``APIC``），
        WAV 根本没有容器放图，白抓一次没意义。

        尊重 ``opts["auto_cover"]``：用户明确关掉「下载封面」就不该在
        转码时偷偷联网 —— 那个开关的意义就是「我这台机器不想连图站」。
        """
        try:
            if cover_art is None or flac_out is None:
                return
            fmt = self._norm_fmt(getattr(self, "_cv_fmt", ""))
            # OGG 的 Vorbis comment 里塞不进图片（libsndfile 不提供接口），
            # 所以不抓 —— 抓了也没地方放，白联一次网。
            if fmt not in ("flac", "mp3"):
                return
            if not self.opts.get("auto_cover", True):
                return
            key = str(self.key or "")
            if not key or not _has_cover_source(key):
                return

            # 本次转的是哪种格式，就看哪个根（四个目录分开记账）
            root = str((self.mp3_dir if fmt == "mp3" else self.flac_dir) or "")
            tag = fmt.upper()
            with self.lock:
                names = {e["url"]: str(e.get("name") or "")
                         for e in self.entries}
            # ① 即将转的 + ② 已经转好的（目录名就是游戏名）
            titles = []
            for u, _a in jobs or ():
                t = names.get(u, "")
                if t and t not in titles:
                    titles.append(t)
            if root and os.path.isdir(root):
                for g in sorted(os.listdir(root)):
                    if os.path.isdir(os.path.join(root, g)) and g not in titles:
                        titles.append(g)
            if not titles:
                return

            self.log("🖼 转 %s 前确认封面（%d 个游戏）" % (tag, len(titles)))
            self._cover_worker([(key, t) for t in titles], self.cancel)

            n = self._backfill_audio(root)
            if n:
                self.log("🖼 已给 %d 个此前转好的 %s 补上封面" % (n, tag))
        except Exception as exc:                    # noqa: BLE001
            # 封面是锦上添花 —— 它失败一次不能让整批转换判失败
            self.log("⚠ 封面处理失败（不影响转码）：%s" % exc)

    def _cv_oneshot_thread(self, jobs, dest):
        """单条转换的后台线程：必要时先解压，再统一入队。

        源文件的找法按「包 → 解压目录」两级退让：

        1. 包在且没解压 → 先解压（``extract_archive`` 会自己拼
           ``<dest>/<包名去扩展名>``，所以 ``dest_dir`` 传**保存目录**，
           传包名目录会变成两层同名目录）；
        2. 包在且已解压 → 直接用产物；
        3. 包没了但解压目录在（勾了「解压后删除原压缩包」）→ 用解压目录里的；
        4. 都没有 → 这一条报「还没下载」，其余照转。
        """
        # v5.9.16：**先补封面，再入队转码**。
        #
        # 为什么必须在这里：转码时 ``spc_wav.write_tracks`` 靠
        # ``cover_art.cover_for_title`` 找图，而那是**纯离线查询** ——
        # 只认本地已经下载过的图。封面原本只在「下载」时抓
        # （``start()`` -> ``_cover_worker``），于是这两类包转 FLAC 永远
        # 没有封面，而且**日志里连一行提示都没有**（``if cover:`` 才打）：
        #
        #   * 包是 v5.9.14 之前下载的（那时还没有封面功能）；
        #   * 用户只点「转 FLAC」没重下（封面只在下载时抓）。
        #
        # 用户 2026-10-27 报的就是后者：``Advance Wars 2`` 的包 10/23 下的，
        # 47 个 .flac 一个图片块都没有。
        #
        # 放在这里而不是 ``convert_wav()`` 里：那边是 HTTP 请求线程，
        # 同步抓 30 个封面要 10 秒，前端会等到超时。这里本来就是后台线程，
        # 且此刻转换队列还空着（入队是本方法后半段干的），抓完再入队就能
        # 保证 ``write_tracks`` 一定拿到图。
        self._covers_before_convert(jobs)

        cv = self.cv
        for url, arch in jobs:
            if self.cancel.is_set():
                break
            stem = os.path.splitext(os.path.basename(arch))[0] if arch else ""
            extract_dir = os.path.join(dest, stem) if stem else ""
            files = []
            if arch and os.path.isfile(arch):
                if joshw_dl is not None and joshw_dl.is_archive(arch):
                    try:
                        files = joshw_dl.payload_files(arch, dest)
                    except Exception:                # noqa: BLE001
                        files = []
                    if not files:
                        self.log("📦 先解压：%s" % os.path.basename(arch))
                        self._set_state(url, "📦 解压中…")
                        try:
                            ok, files, why = joshw_dl.extract_archive(
                                arch, dest_dir=dest, cancel=self.cancel)
                        except Exception as exc:     # noqa: BLE001
                            ok, files, why = False, [], str(exc)
                        if not ok:
                            self._set_state(url, "❌ 解压失败")
                            self.log("❌ 解压失败 %s：%s"
                                     % (os.path.basename(arch), why))
                            continue
                        with self.lock:
                            self.ex_pkgs += 1
                            self.ex_files += len(files)
                else:
                    files = [arch]
            # 先滤成「真的能转的」：解压产物里常带 .txt/.nfo，只按「列表非空」
            # 判会让这一条停在「⏳ 待转 WAV」不动（一个也没入队）。
            # v6.0.6：再去掉「音源转出来的 WAV」—— 这是用户报的
            # 「FLAC 数量和 MP3 数量不同」的根因（``xxx.fsb`` 和
            # ``xxx.fsb.wav`` 各转一遍，10 首变 20 个 FLAC）。
            files = dl_convert.drop_derived_wavs(
                [f for f in files if self._convertible(f)])
            if not files:
                # 包没了但解压目录还在（「解压后删除原压缩包」）—— 这是
                # 「📦 已解压」最常见的形态，必须能转。
                files = self._payload_in(extract_dir)
                if files:
                    self.log("📂 用已解压的目录：%s（%d 个源文件）"
                             % (stem, len(files)))
            if not files:
                self._set_state(url, "❌ 还没下载")
                self.log("❌ %s 还没下载（先下载再转）" % self._name_of(url))
                continue
            for f in files:
                self._enqueue(f, url, pkg=os.path.basename(str(f)))
            self._set_state(url, "⏳ 待转 " + self._pending_tag())
        # ⚠ 不要在这里写 ``self.cv_queued = queued`` —— ``_enqueue()`` 已经
        #   每入队一个就 +1 了，再覆盖一次会把「解压时顺手入队的」算丢。
        with self.lock:
            queued = self.cv_queued
        self.log("🎵 开始转 %s：%d 个游戏，已入队 %d 个源文件"
                 % (self._pending_tag(), len(jobs), queued))
        if cv is not None:
            cv.mark_src_finished()
        if not queued:
            # 一个都没入队 → 不会再有 cv_thread_exit，得自己收尾，
            # 否则界面永远停在「进行中…」且「开始下载」一直是灰的。
            self._finish()

    # -- 中文名 -------------------------------------------------------------
    def _load_zh_cache(self):
        """本机种已有的中文名（``zh_titles`` 的离线词库 + 用户词典）。"""
        if zh_titles is None:
            return
        out = {}
        with self.lock:
            names = [str(e.get("name") or "") for e in self.entries]
        for n in names:
            try:
                z = zh_titles.translate(n)
            except Exception:                        # noqa: BLE001
                continue
            if z and z != n:
                out[n] = z
        with self.lock:
            self.zh = out

    def translate(self):
        """翻译未收录的条目：先离线词库，再联网兜底。"""
        if self.zh_running:
            return {"ok": False, "error": "正在翻译中"}
        if zh_titles is None and zh_online is None:
            return {"ok": False, "error": "翻译模块未就绪"}
        with self.lock:
            self.zh_running = True
            self._zh_cancel = threading.Event()
            names = [str(e.get("name") or "") for e in self.entries]
        threading.Thread(target=self._zh_thread, args=(names,),
                         daemon=True).start()
        return {"ok": True}

    def _zh_thread(self, names):
        cancel = self._zh_cancel
        got = {}
        try:
            self.log("🌐 开始翻译中文名（%d 条）…" % len(names))
            if zh_titles is not None:
                for n in names:
                    if cancel.is_set():
                        return
                    try:
                        z = zh_titles.translate(n)
                    except Exception:                # noqa: BLE001
                        continue
                    if z and z != n:
                        got[n] = z
            miss = [n for n in names if n not in got]
            if miss and zh_online is not None and not cancel.is_set():
                self.log("ℹ 离线词库未收录 %d 条，改用在线翻译…" % len(miss))
                try:
                    pairs = zh_online.translate_titles(miss)
                except Exception as exc:             # noqa: BLE001
                    pairs = {}
                    self.log("⚠ 在线翻译失败: %s" % exc)
                if isinstance(pairs, dict):
                    for k, v in pairs.items():
                        if v:
                            got[str(k)] = str(v)
                elif isinstance(pairs, (list, tuple)):
                    for k, v in zip(miss, pairs):
                        if v:
                            got[str(k)] = str(v)
            with self.lock:
                self.zh = got
                self.zh_running = False
            self.log("✅ 翻译完成：%d 条有中文名" % len(got))
        except Exception as exc:                     # noqa: BLE001
            with self.lock:
                self.zh_running = False
            self.log("❌ 翻译失败: %s" % exc)

    # -- 缓存 ---------------------------------------------------------------
    def _key_lock(self, key):
        """取某个机种的缓存写锁（每个 key 一把，按需创建）。

        为什么需要：``scan_platform`` 扫完会 ``save_cache`` 写
        ``<cache>/<key>.json``。前台「清除缓存 + 重扫」和后台「更新全部列表」
        可能同时盯上同一个 key，两边都写 → 半个 JSON，下次读直接报错。
        """
        with self._key_locks_guard:
            lk = self._key_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._key_locks[key] = lk
            return lk

    def refresh_all(self):
        """把所有机种的列表**一次性**重新扫一遍（v6.0.8）。

        用户报：「列表更新需要到每个机种下面去点击」。原来只能选中机种 →
        点「清除缓存」→ 重扫，33 个机种就得点 33 遍。现在一颗按钮跑完。

        和 ``clear_cache(key)`` + ``load(key, force=True)`` 的关键区别：

        - **不动当前视图**。``self.key`` / ``self.entries`` / ``self.sel``
          一概不碰 —— 用户点完还能继续看当前机种、继续下载；
        - 只写各机种自己的缓存文件，切到哪个机种就是最新列表；
        - 后台线程跑，进度写在 ``self.refresh`` 里，前端轮询
          ``/api/dl/state`` 的 ``refresh`` 字段显示「12/33 · NES」。

        跑完如果当前机种也在这次范围内、而且**没有勾选任何条目**、
        也不在下载/转换中，就顺手把它从新缓存重读一遍 —— 否则用户盯着
        屏幕会觉得「点了没反应」。有勾选时不动，免得把选择清掉。
        """
        if jw is None:
            return {"ok": False, "error": "音源模块未就绪"}
        with self.lock:
            if self.refresh.get("running"):
                return {"ok": False, "error": "正在更新中，等它跑完"}
        try:
            keys = [str(p.get("key")) for p in jw.PLATFORMS if p.get("key")]
        except Exception as exc:                     # noqa: BLE001
            return {"ok": False, "error": "读取机种列表失败: %s" % exc}
        if not keys:
            return {"ok": False, "error": "没有可更新的机种"}

        cancel = threading.Event()
        with self.lock:
            self._refresh_cancel = cancel
            self.refresh = {"running": True, "done": 0, "total": len(keys),
                            "key": "", "name": "", "ok": 0, "fail": 0}
        self.log("🔄 开始更新全部列表：%d 个机种，%d 路并发…"
                 % (len(keys), min(REFRESH_WORKERS, len(keys))))
        threading.Thread(target=self._refresh_worker, args=(keys, cancel),
                         daemon=True).start()
        return {"ok": True, "total": len(keys)}

    def cancel_refresh(self):
        """停掉「更新全部列表」。已扫完的机种保留（缓存是完整的才落盘）。"""
        with self.lock:
            running = bool(self.refresh.get("running"))
            cancel = self._refresh_cancel
        if not running:
            return {"ok": False, "error": "没有在更新"}
        cancel.set()
        self.log("⏹ 正在停止更新全部列表…")
        return {"ok": True}

    def _refresh_worker(self, keys, cancel):
        """后台线程：按 key 池化扫描，**只写缓存**，不碰界面状态。

        ``scan_platform`` 在取消时会**跳过 save_cache**（见 joshw_source），
        所以半截的扫描结果不会覆盖掉好缓存 —— 中途点停是安全的。
        """
        n = len(keys)
        cursor = {"i": 0, "done": 0, "ok": 0, "fail": 0}
        guard = threading.Lock()

        def take():
            with guard:
                if cursor["i"] >= n or cancel.is_set():
                    return None, 0
                i = cursor["i"]
                cursor["i"] = i + 1
                return keys[i], i

        def work():
            while True:
                key, i = take()
                if key is None:
                    return
                try:
                    name = str((jw.platform(key) or {}).get("name") or key)
                except Exception:                    # noqa: BLE001
                    name = key
                with self.lock:
                    self.refresh["key"] = key
                    self.refresh["name"] = name
                try:
                    with self._key_lock(key):
                        if cancel.is_set():
                            raise _Cancelled()
                        items = jw.scan_platform(key, progress=None,
                                                 cancel=cancel)
                    if cancel.is_set():
                        # 扫到一半被叫停：scan_platform 不会 save_cache，
                        # 这个机种等于没更新，**不能算进 done**（否则界面
                        # 会显示「已处理 3/33、成功 0」，自相矛盾）。
                        raise _Cancelled()
                except _Cancelled:
                    return                           # 用户点了停，不再领新活
                except Exception as exc:             # noqa: BLE001
                    with guard:
                        cursor["fail"] += 1
                    self.log("⚠ [%d/%d] %s 失败：%s" % (i + 1, n, name, exc))
                else:
                    with guard:
                        cursor["ok"] += 1
                    self.log("🔄 [%d/%d] %s ✓ %d 条"
                             % (i + 1, n, name, len(items or [])))
                with guard:
                    cursor["done"] += 1
                    done, ok, fail = cursor["done"], cursor["ok"], cursor["fail"]
                with self.lock:
                    self.refresh.update({"done": done, "ok": ok, "fail": fail})

        threads = [threading.Thread(target=work, daemon=True)
                   for _ in range(max(1, min(REFRESH_WORKERS, n)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stopped = cancel.is_set()
        with self.lock:
            self.refresh["running"] = False
            self.refresh["key"] = ""
            self.refresh["name"] = ""
            ok, fail = self.refresh["ok"], self.refresh["fail"]
            cur = self.key
            # 只有「没勾选、不在下载/转换」时才顺手刷新当前视图 ——
            # 有勾选时重读会把 self.sel 清掉，那是更糟的意外。
            reload_cur = (not stopped) and bool(cur) and cur in keys \
                and not self.sel and not self.busy
        if stopped:
            self.log("⏹ 已停止更新全部列表（完成 %d/%d）" % (ok + fail, n))
        else:
            self.log("✅ 全部列表已更新：%d 个成功 / %d 个失败" % (ok, fail))
        if reload_cur:
            try:
                self.load(cur)                       # 读刚写好的缓存，不联网
            except Exception as exc:                 # noqa: BLE001
                self.log("⚠ 刷新当前机种显示失败：%s" % exc)

    def clear_cache(self, key=None):
        target = key or self.key or None
        # 缓存目录按站分开，清谁的要看清谁 —— 用错模块等于什么都没清
        mod = zs if (target and self._src_of(target) == "zophar") else jw
        if mod is None:
            return {"ok": False, "error": "音源模块未就绪"}
        try:
            mod.clear_cache(target)
        except Exception as exc:                     # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if self.src == "zophar" or (target and self._src_of(target) == "zophar"):
            with self.lock:
                self._zip_of = {}
                self._zip_size = {}
        self.log("🗑 已清除缓存%s" % ("（%s）" % target if target else ""))
        return {"ok": True}

    # -- 云上传 -------------------------------------------------------------
    def upload_cloud(self, scope="", games=None, fmt="flac"):
        """把 WAV 输出目录里的文件传到网易云音乐云盘。

        ``scope``：``merged`` / ``tracks`` / ``all``（口径见
        ``cloud_api.pick_scope``）。空串 = 老行为（两种都有就只传分轨）。
        ``games``：只传这几个游戏子目录（``WAV/<游戏名>/``）。空/None = 全传。
        ``fmt``：上传容器，``"flac"``（默认，无损）或 ``"mp3"``（v6.0.1，
        320 kbps CBR，约 1/4 体积）。不认识的值按 flac 处理 —— 这是**故意**
        的：宁可传无损，也别因为一个拼错的参数把整批拒绝掉。

        登录态走 ``cloud_api.auth().ensure()``；**未登录返回
        ``need_login: True``**，前端据此弹登录窗、登完自动回来接着传 ——
        不再让用户去开 tkinter 版。
        """
        if self.cloud.get("running"):
            return {"ok": False, "error": "正在上传中"}
        # ⚠ 必须用 ``_wav_dir_now()``，不能读 ``self.wav_dir``：后者只在
        #   ``start()`` 里冻结，程序重启后是空的 → 退回 ``<dest>/WAV`` 时
        #   dest 若已被用户改过，扫的就是另一个目录（「这个目录里没有 WAV」
        #   的成因之一）。
        wav_dir = self._wav_dir_now()
        if not wav_dir or not os.path.isdir(wav_dir):
            return {"ok": False,
                    "error": "没有可上传的 WAV（%s 不存在），先转 WAV"
                             % (wav_dir or "（保存目录未设置）")}
        # v5.2-web-game: 产物现在按游戏分子文件夹（WAV/<游戏名>/xx.wav），
        # 只扫一层会把它们全漏掉 —— 用户看到「没有可上传的 WAV」而文件明明在。
        want = {str(g).lower() for g in (games or ()) if g}
        wavs = []
        for root, _dirs, files in os.walk(wav_dir):
            rel_game = os.path.relpath(root, wav_dir)
            rel_game = "" if rel_game == "." else rel_game.replace("\\", "/")
            if want and rel_game.split("/")[0].lower() not in want:
                continue
            for n in files:
                if n.lower().endswith(".wav"):
                    wavs.append(os.path.join(root, n))
        wavs.sort()
        if not wavs:
            # v6.0.2：只转过 FLAC / MP3 的用户会走到这里。上传的**源头必须是
            # WAV**（FLAC→FLAC 是重编码，MP3→MP3 是有损再压一遍，都不该做），
            # 所以不能拿 FLAC/MP3 顶替 —— 但得把这句话说出来，否则用户看着
            # 一目录的 FLAC 却被告知「没有 WAV」。
            _hint = ""
            try:
                for _tag in ("FLAC", "MP3", "OGG"):
                    if os.path.isdir(os.path.join(dest, _tag)):
                        _hint = ("；%s/ 里的是转好的成品，上传要从 WAV 重新编码"
                                 "（避免二次压缩），请先转一次 WAV" % _tag)
                        break
            except Exception:                        # noqa: BLE001
                _hint = ""
            return {"ok": False,
                    "error": (("选中的游戏里没有 WAV" if want
                               else "输出目录里没有 WAV") + _hint)}
        try:
            import cloud_upload
            from web_ui import cloud_api
        except Exception as exc:                     # noqa: BLE001
            return {"ok": False, "error": "上传模块未就绪: %s" % exc}

        wavs, why = cloud_api.pick_scope(wavs, scope=scope)
        if not wavs:
            return {"ok": False,
                    "error": why or "这个范围里没有可上传的文件"}

        # 登录态**同步**确认（静默恢复最多约 1 秒）。未登录就把话交回前端，
        # 别起线程 —— 起了线程前端只能拿到「已开始」，然后干看着日志报未登录。
        got = cloud_api.auth().ensure(log=self.log)
        if not got.get("ok"):
            return {"ok": False,
                    "need_login": bool(got.get("need_login")),
                    "error": got.get("error") or "云盘未登录",
                    "files": len(wavs)}
        sess = got["session"]
        user = sess.user_name or ""
        fmt = cloud_upload.norm_fmt(fmt)

        with self.lock:
            self.cloud = {"running": True, "progress": 0, "ok": 0,
                          "skip": 0, "fail": 0, "error": "",
                          "user": user, "count": len(wavs), "fmt": fmt}
            self._cloud_cancel = threading.Event()
            cancel = self._cloud_cancel
        threading.Thread(target=self._cloud_thread,
                         args=(cloud_upload, wavs, cancel, sess, fmt),
                         daemon=True).start()
        return {"ok": True, "files": len(wavs), "user": user, "fmt": fmt}

    def _cloud_thread(self, cloud_upload, wavs, cancel, sess, fmt="flac"):
        """后台线程：真上传。

        ⚠⚠ ``CloudUploader.emit(kind, payload)`` 的 payload **不是 dict**：

        ==========  ======================================
        ``"log"``   字符串（一行日志）
        ``"prog"``  int（0-100 的百分比）
        ``"done"``  元组 ``(ok, skip, fail)``
        ==========  ======================================

        这里原来一律按 dict 处理（``payload.get("text")``），结果日志全是
        空行、进度永远 0、成功数永远 0 —— 三样一起废，而界面看起来「在跑」。
        这是「下载页上传云盘像是成功了、其实什么都没显示」的根因。
        """
        def emit(kind, payload=None):
            if kind == "log":
                self.log(str(payload or ""))
            elif kind == "prog":
                with self.lock:
                    self.cloud["progress"] = int(payload or 0)
            elif kind == "done":
                try:
                    ok, skip, fail = payload
                except (TypeError, ValueError):
                    ok = skip = fail = 0
                with self.lock:
                    self.cloud.update(ok=int(ok), skip=int(skip), fail=int(fail))

        try:
            self.log("☁ 开始上传 %d 个文件（%s / %s）…"
                     % (len(wavs), sess.user_name or "未知账号",
                        fmt.upper()))
            cloud_upload.CloudUploader(sess, emit, cancel,
                                       fmt=fmt).run(wavs)
            with self.lock:
                self.cloud["running"] = False
                self.cloud["progress"] = 100
            self.log("☁ 上传结束：成功 %d / 跳过 %d / 失败 %d"
                     % (self.cloud["ok"], self.cloud["skip"],
                        self.cloud["fail"]))
        except Exception as exc:                     # noqa: BLE001
            with self.lock:
                self.cloud["running"] = False
                self.cloud["error"] = str(exc)
            self.log("❌ 上传失败: %s" % exc)

    def cancel_cloud(self):
        try:
            self._cloud_cancel.set()
        except Exception:                            # noqa: BLE001
            pass
        return {"ok": True}

    # -- 快照 ---------------------------------------------------------------
    def snapshot(self, since=0):
        logs, seq = self.logs_since(since)
        with self.lock:
            rows = []
            n_on_disk = 0
            for e in self.entries[:MAX_ROWS]:
                url = e["url"]
                name = str(e.get("name") or "")
                size = int(e.get("size") or 0)
                # ⚠ 这里必须和 start() 用**同一个** URL 算落盘名：
                #   zophar 的条目 url 是游戏页，真实落盘是 <游戏>.zip。
                #   用 page url 算会得到 .7z，「已在磁盘」的勾永远不亮。
                # v5.9.3：状态不再只靠内存里的 self.states —— 那个一重启就空，
                #   用户看到的就是「整列空白，不知道哪个下过了」。现在
                #   运行期临时状态优先（⏳ 待转 WAV 这类），没有就从磁盘算。
                dstate = self._disk_state(name, url)
                if dstate:
                    n_on_disk += 1
                rows.append({
                    "url": url,
                    "name": name,
                    "zh": self.zh.get(name, ""),
                    "mtime": _fmt_day(e.get("mtime") or 0),
                    "size": size,
                    "size_text": _human(size),
                    "state": self.states.get(url, "") or dstate,
                    "sel": url in self.sel,
                    "on_disk": bool(dstate),
                })
            return {
                "key": self.key,
                "name": self.name,
                "source": self.source,
                "stamp": self.stamp,
                "scanning": self.scanning,
                #: 「更新全部列表」的进度（v6.0.8）。前端拿它在按钮上显示
                #: 「更新中 12/33」并把按钮置灰 —— 不然 5 分钟的等待里
                #: 用户会以为按钮没生效、反复点。
                "refresh": dict(self.refresh),
                "phase": self.phase,
                "busy": self.busy,
                "status": self.status,
                "progress": round(self.progress, 1),
                "dest": self.dest,
                #: 上传云盘要用的目录 —— 前端拿它去 GET /api/cloud/plan?dir=
                "wav_dir": self.wav_dir or "",
                "opts": dict(self.opts),
                "count": len(self.entries),
                "sel": len(self.sel),
                #: 盘上已经有东西的条数（已下载 / 已解压 / 已转 WAV 都算）——
                #: 前端拿它在「共 N 条，已选 M 条」后面补一句「已下载 K 条」，
                #: 用户一眼就知道还有多少没下。
                "n_on_disk": n_on_disk,
                #: 状态列是**按哪个目录**扫出来的 + 扫到了多少东西。
                #: 前端放在计数条的 title 里：用户把保存目录改到别处后，
                #: 状态列没变化时能自己确认「扫的是不是我以为的那个目录」。
                "scan": {
                    "dir": self._disk_root or self.dest or "",
                    "files": len(self._disk),
                    "dirs": len(self._disk_dirs),
                    "wav": len(self._wav_games),
                    "limited": bool(self._disk_limited),
                },
                "truncated": len(self.entries) > MAX_ROWS,
                "rows": rows,
                "zh_running": self.zh_running,
                "cloud": dict(self.cloud),
                "stats": {
                    "dl_total": self.dl_total,
                    "dl_shown": int(self.dl_shown),
                    "dl_expect": self.dl_expect,
                    "dl_done": self.dl_done,
                    "dl_ok": self.dl_ok,
                    "dl_fail": self.dl_fail,
                    "dl_skip": self.dl_skip,
                    "ex_pkgs": self.ex_pkgs,
                    "ex_files": self.ex_files,
                    "ex_fail": self.ex_fail,
                    "cv_queued": self.cv_queued,
                    "cv_ok": self.cv_ok,
                    "cv_fail": self.cv_fail,
                    "cv_skip": self.cv_skip,
                    "cv_tracks": self.cv_tracks,
                    "cv_current": self.cv_current,
                    "wav_dir": self.wav_dir,
                    #: v5.9.15：另外两种格式的输出根 + 本次转换的格式。
                    #: 前端「转 MP3」那颗按钮点完要知道产物落在哪。
                    "flac_dir": self.flac_dir,
                    "mp3_dir": self.mp3_dir,
                    "ogg_dir": self.ogg_dir,
                    "cv_fmt": self._cv_fmt,
                    #: v5.9.14 封面。和 cv_* 分开一组 —— 封面和转码是两条
                    #: 互不相干的线（封面可能还在跑，转码早完了），混在一起
                    #: 前端没法判断「整体进度」。
                    "cover_total": self.cover_total,
                    "cover_done": self.cover_done,
                    "cover_ok": self.cover_ok,
                    "cover_fail": self.cover_fail,
                    "cover_current": self.cover_current,
                },
                "logs": logs,
                "seq": seq,
            }

    # -- 能力探测 -----------------------------------------------------------
    def diag(self):
        """下载页依赖是否齐（打包漏件的第一手证据）。"""
        def has(mod):
            try:
                __import__(mod)
                return True
            except Exception:                        # noqa: BLE001
                return False
        return {
            "joshw_source": jw is not None,
            # ⚠ v5.9 起 zophar 不再进机种树（见 platforms()），这一项只表示
            #   「模块打进去了没」—— 别再拿它当「下载页有 Zophar 组」的证据。
            "zophar_source": zs is not None,
            "joshw_dl": joshw_dl is not None,
            "dl_convert": dl_convert is not None,
            "zh_titles": zh_titles is not None,
            "zh_online": zh_online is not None,
            # ⚠ 只查「模块在不在」是不够的：中文名的主体在 zh_titles_user.json
            #   里（约 2.7 万条），那个 JSON 不是 Python 模块，Nuitka 不会自动
            #   带上。模块在、词典空 → 界面上中文名几乎全是「—」，而所有
            #   「模块在不在」的检查都是绿的。所以这里直接把**词条数**报出来。
            "zh_dict": len(zh_titles.GAME_ZH) if zh_titles is not None else 0,
            # PSF 系：模块在不在 + 那个**不是 Python 模块**的 psf2wav.exe 在不在。
            # 只查前者会漏掉最典型的一种坏包（.py 打进去了、.exe 忘了带）。
            "psf_native": psf_native is not None,
            "psf_decoder": bool(psf_native.decoder_ready()[0]) if psf_native else False,
            "psf_bios": bool(psf_native.find_bios()[0]) if psf_native else False,
            "py7zr": has("py7zr"),
            "Crypto": has("Crypto"),
            "cloud_upload": has("cloud_upload"),
            "netease_cloud": has("netease_cloud"),
            "tar": bool(joshw_dl.find_tar()) if joshw_dl is not None else False,
            "platforms": len(jw.PLATFORMS) if jw is not None else 0,
            # v5.9.14 封面：模块在不在 + 登记了多少机种 + 缓存里已经有多少张。
            # 纯 Python（标准库 urllib），没有外部 exe 依赖，所以「模块在」
            # 就等于「功能在」；仍然把张数报出来，方便确认缓存目录对不对。
            "cover_art": cover_art is not None,
            # v6.0.5 兜底源。单独报，因为它是**另一个** try/except 可选 import：
            # 只漏它的话上面那条照样绿，而 IGDB 那一整层静默消失。
            "cover_igdb": cover_igdb is not None,
            "cover_igdb_platforms": (len(cover_igdb.PLATFORM_IDS)
                                     if cover_igdb is not None else 0),
            "covers": (cover_art.stats() if cover_art is not None else {}),
            # v6.0.6 衍生 WAV 去重。两个函数在 exe 里取不到的表现是
            # 「FLAC 数量还是 MP3 的两倍」，而界面一切正常 —— 只能靠这条点名。
            "derived_wav": (hasattr(dl_convert, "is_derived_wav")
                            and hasattr(dl_convert, "drop_derived_wavs")),
            # 转换器版本。打包漏了某个改动时，这个数字对不上是最快的线索。
            "audio_writer_version": str(getattr(
                dl_convert, "AUDIO_WRITER_VERSION", "") or ""),
            # v6.0.7 多封面。0 表示常量没打进去（轮播会退化成只有一张）。
            "cover_limit": int(COVER_LIMIT),
            # v6.0.8「更新全部列表」的并发数。0 = 常量没打进去，
            # 那颗按钮点下去会立刻报「没有可更新的机种」或者串行跑 15 分钟。
            "refresh_workers": int(REFRESH_WORKERS),
        }
