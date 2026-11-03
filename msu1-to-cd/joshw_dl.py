#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""joshw_dl.py -- 音源包的断点续传下载器（核心层）。

特性：

* 线程池并发（默认 2 个 worker），单文件超时 30 秒；
* 断点续传：目标旁保留 ``<dest>.part``，重跑时用 ``Range`` 续传，
  服务端回 206 则追加、回 200 则从头覆盖；完成后 ``os.replace`` 转正；
* 1 MB 分块，每块回调全局累计进度；
* 单文件失败重试 3 次（1s / 2s / 4s 退避），仍失败则发 ``file_done(ok=False)``
  并继续下一个；
* 取消时保留 ``.part``，且队列清空/取消后必定发一次 ``all_done``；
* **下载后自动解压**（默认开启）：joshw 托管的包除 SNES 的裸 ``.spc`` 外
  全是 ``.7z``，用 Windows 自带的 ``%SystemRoot%\\System32\\tar.exe``
  （bsdtar/libarchive）直接解，**不走 PATH 上的 GNU tar**（不支持 7z，且会把
  ``C:`` 当远程主机）。解压失败**保留原包**并在事件里说明原因，不丢数据。

事件回调 ``on_event(kind, payload)``：

======================  =====================================================
kind                    payload
======================  =====================================================
``file_start``          ``{"file": str, "size": int}``
``progress``            ``{"done": int, "total": int}``  全局累计字节
``file_done``           ``{"file": str, "ok": bool, "error": str}``
``extract_start``       ``{"file": str, "dest": str}``
``extract_done``        ``{"file": str, "dest": str, "files": [str],
                          "count": int, "archive_removed": bool}``
``extract_failed``      ``{"file": str, "error": str, "reason": str}``
``all_done``            ``{}``
======================  =====================================================
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from joshw_source import USER_AGENT, tls_context
import winspawn                       # 解压器不弹黑框，见 winspawn.py

__all__ = ["Downloader", "sanitize_filename", "build_dest",
           "extract_archive", "find_tar", "is_archive", "ARCHIVE_EXTS",
           "extract_dir_for", "extract_timeout_for", "payload_files",
           "has_payload", "unsafe_members"]

_CHUNK_SIZE = 1024 * 1024  # 1 MB
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF = (1.0, 2.0, 4.0)
_MAX_NAME_LENGTH = 120
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + ["com%d" % i for i in range(1, 10)]
    + ["lpt%d" % i for i in range(1, 10)]
)

# ---- 自动解压 --------------------------------------------------------------
# joshw 托管的归档格式。除 SNES 的 .spc 是裸文件外，其余机种全是 .7z。
ARCHIVE_EXTS = (".7z", ".zip")
# 解压超时的上下限。实际值由 extract_timeout_for() 按包大小算，
# 这里只是兜底的默认值（包大小读不出来时用）。
_EXTRACT_TIMEOUT = 120.0
_EXTRACT_TIMEOUT_MIN = 60.0
_EXTRACT_TIMEOUT_MAX = 600.0
# 收到取消信号后，先 terminate（友好），5 秒还不走就 kill（强杀）。
_KILL_GRACE = 5.0
# 轮询子进程的间隔。取 0.1s 是为了"取消"和"超时"的响应足够快，
# 又不至于空转烧 CPU。
_EXTRACT_POLL = 0.1

# joshw 各机种解包后真正的音源扩展名。SNES 的 .spc 与其余机种一样在 .7z 里，
# 这里列的是"解出来的产物"用得到的；判断"是否已就位"不依赖索引里的格式名。
# 用途只有一个：把随包带出的 .txt / .nfo / 封面等附带物排除掉。
_PAYLOAD_EXTS = frozenset([
    ".nsf", ".nsfe", ".spc",
    ".gbs", ".gbs7z",
    ".hes", ".kss", ".2sf", ".2sfm",
    ".psf", ".psf1", ".minipsf", ".minipsf1", ".minipsf2", ".ssf", ".dsf",
    ".spu", ".minispu",
    ".vgm", ".vgz",
    ".wii", ".cnk",
    ".3do", ".iso",
    ".qsf", ".mpt", ".mod", ".s3m", ".xm", ".it",
    # v5.9.10：QSF 包（joshw 的 ``hoot/qsf/`` 那 18 个 .7z）解出来是
    # 「1 个 .qsflib + N 个 .miniqsf」，**曲目是 .miniqsf**。
    # 少这两个后缀的症状很隐蔽：``has_payload()`` 恒为 False，于是每次
    # 进下载页都判定「内容没就位」，重复下载几 MB 再解一遍。
    ".miniqsf", ".qsflib",
    ".ssm", ".bs", ".gsf",
    ".lnx", ".sgx", ".ym", ".gym", ".sp",
    ".snes", ".bsx", ".nwc",
    ".mp3", ".ogg", ".wav", ".aiff", ".aif",
    # v5.9.11：PS3 / PSP / Wii / 3DS 的预录流 + GameCube 的 Pivotal 包。
    # ⚠ 这已经是**第四例**同一个坑（前三次是 .gsflib / .ksslib / .qsflib）：
    #   少一个后缀，``has_payload()`` 就恒为 False，于是每次进下载页都判定
    #   「内容没就位」，把几 MB 到几百 MB 的包重复下载再解一遍。
    #   实测这 6 个机种的包解出来分别是：
    #     PS3   → .XVAG / .AT3      PSP  → .hca
    #     Wii   → .brstm            3DS  → .laac（+ .txth 描述文件）
    #     GC    → .psf（Pivotal）+ .txtp（曲目索引）
    ".xvag", ".at3", ".hca", ".brstm", ".laac",
    #     PS Vita → .at9 / .vag（+ 66 个 .txtp 索引）—— v5.9.11 补
    ".at9", ".vag",
    # ⚠ ``.txtp`` 必须算负载：GameCube 的包里**音频全在 stream/ 子目录**，
    #   顶层只有 24 个 .txtp —— 不认 .txtp 的话整个包看起来是空的。
    ".txtp", ".txth",
    # v5.9.12：Xbox 360 / PS4 / Xbox / Wii U / PC-98。
    # 还是同一个坑（**第五例**），症状一模一样：``has_payload()`` 恒为 False，
    # 下载页每次都判定「内容没就位」，重复下载再解一遍。
    #   X360 → .xma          PS4 → .wem（+ .bnk 银行 / .bik 视频音轨）
    #   Xbox → .araw         WiiU → .fsb / .btsnd      PC-98 → .s98
    ".xma", ".wem", ".bnk", ".bik", ".araw", ".fsb", ".btsnd", ".s98",
])


def is_archive(path: str) -> bool:
    """目标是不是"需要再解一层"的归档包。"""
    return str(path or "").lower().endswith(ARCHIVE_EXTS)


def extract_timeout_for(size_bytes: int) -> float:
    """按包大小算解压超时：``max(60, min(600, 30 + MB*2))`` 秒。

    joshw 的包从几百 KB 到几十 MB 都有。给 60 秒下限是为了让"慢机器 +
    大包"不至于被误杀；30 秒余量覆盖 bsdtar 的建目录与 CRC 校验开销；
    每 MB 2 秒对应约 0.5 MB/s 的极端慢速磁盘也绰绰有余。
    """
    try:
        size = max(0, int(size_bytes))
    except (TypeError, ValueError):
        size = 0
    megabytes = size / (1024.0 * 1024.0)
    return max(_EXTRACT_TIMEOUT_MIN,
               min(_EXTRACT_TIMEOUT_MAX, 30.0 + megabytes * 2.0))


def extract_dir_for(archive: str, extract_dir: Optional[str] = None) -> str:
    """归档 -> 它会被解到的目标目录。

    与 :func:`extract_archive` / :meth:`Downloader._maybe_extract` 用的是
    **同一套规则**，这样调用方（例如 GUI 的"已存在就跳过"判断）可以在
    下载之前就准确算出产物落在哪，不用把包先下下来试。

    Args:
        archive: 归档文件路径，只取 basename 与所在目录。
        extract_dir: 解压根目录；``None`` 时用归档所在目录。
    """
    archive = os.path.abspath(str(archive or ""))
    root = os.path.abspath(str(extract_dir)) if extract_dir else (
        os.path.dirname(archive) or os.curdir)
    stem = os.path.splitext(os.path.basename(archive))[0]
    return os.path.join(root, stem)


def payload_files(archive: str, extract_dir: Optional[str] = None) -> List[str]:
    """列出某个归档**已经解出来的**产物文件（没解过返回空表）。

    这是给"已存在就跳过"用的查询接口：只看 ``.7z`` 本体会漏判 ——
    用户手动删掉包、只留产物目录（或上次是"解压后删包"模式）时，
    本体不在但内容其实已经齐了，不该重复下载几 MB 再解一遍。
    """
    if not is_archive(archive):
        return []
    target = extract_dir_for(archive, extract_dir)
    if not os.path.isdir(target):
        return []
    return sorted(_snapshot_dir(target))


def has_payload(archive: str, extract_dir: Optional[str] = None) -> bool:
    """这个归档是否**已经解压出可用的音频文件**。

    只认真正的音频/芯片扩展名：解压器可能顺带带出 ``.txt``、``.nfo``、
    ``.sfx`` 之类的说明文件，那些不算"内容已就位"。
    """
    for path in payload_files(archive, extract_dir):
        if _is_payload_file(path):
            return True
    return False


def _is_payload_file(path: str) -> bool:
    """产物是否算"可用的音源文件"（而非说明/封面等附带物）。"""
    ext = os.path.splitext(str(path or ""))[1].lower()
    return ext in _PAYLOAD_EXTS


def find_tar() -> Optional[str]:
    """定位可解 7z 的 tar.exe，**只认 Windows 自带的那一份**。

    为什么必须用全路径而不是 ``tar``：

    * PATH 上的 ``tar`` 通常是 Git 带的 GNU tar 1.35，**不支持 7z**；
    * 而且它把 ``C:\\...`` 里的盘符当成"远程主机名"，直接报
      ``Cannot connect to C: resolve failed``；
    * ``%SystemRoot%\\System32\\tar.exe`` 是 bsdtar(libarchive)，原生支持 7z，
      且能正确处理 Windows 盘符路径。

    返回 ``None`` 表示这台机器上没有可用的解压器（调用方据此降级为
    "请手动解压"，并把原因写进日志，不静默失败）。
    """
    if not sys.platform.startswith("win"):
        # 非 Windows：7z/p7zip 与 bsdtar 都可能可用，但本项目只在 Windows 上分发，
        # 这里保持保守 —— 找不到就返回 None，由调用方降级。
        for name in ("bsdtar", "7z", "7za"):
            found = shutil.which(name)
            if found:
                return found
        return None
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    for candidate in (os.path.join(system_root, "System32", "tar.exe"),
                      os.path.join(system_root, "Sysnative", "tar.exe")):
        if os.path.isfile(candidate):
            return candidate
    # 极少数精简版 Windows 可能把 tar 放在 System32 之外，但仍是 bsdtar 而非
    # GNU tar —— 用 windir 反查一次，找不到就明确返回 None。
    alt = shutil.which("tar.exe", path=os.path.join(system_root, "System32"))
    return alt


# ---------------------------------------------------------------------------
# 7z 解压后端 2：py7zr（跨平台，Android 走这条）
# ---------------------------------------------------------------------------
# 桌面版优先用 bsdtar：它跟 joshw 的实际包打了几年交道，行为最稳。
# 但有两类环境根本没有 bsdtar：
#
#   * **Android**（``sys.platform == "linux"``）—— 没有 System32，也没有可
#     执行的 ``tar``，且分区存储不允许把可执行二进制解到数据目录去跑；
#   * 精简版 Windows / 用户误删 ``tar.exe``。
#
# 这时退到 py7zr。它是纯 Python 壳 + ``pyppmd`` / ``pybcj`` / ``inflate64``
# 三个 C 扩展（不是纯 Python 解码），实测解 20 MB 包 **1.05s**，
# 同机 bsdtar 是 1.20s —— 所谓「py7zr 慢几倍」的说法在带 C 扩展的 1.x 上
# 不成立，手机上完全够用。
#
# ⚠ 桌面版**不需要**装 py7zr：没有它就自动维持原来的 bsdtar 路径，
#   导入失败也只是让降级分支不可用，不影响任何现有行为。


class _ExtractAbort(Exception):
    """在 py7zr 的进度回调里抛出，用来中断解压。

    py7zr 没有原生取消接口，但回调抛异常能立刻打断它的解压循环。
    代价是目标目录会留下半截文件 —— **原包始终保留**，重解一次即可。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _import_py7zr():
    """延迟导入 py7zr，不可用时返回 ``None``（桌面版不强依赖它）。"""
    try:
        import py7zr
    except Exception:
        return None
    return py7zr


def unsafe_members(names) -> List[str]:
    """从条目名里挑出**非法路径**（绝对路径 / 盘符 / ``..`` 上跳）。

    7z 允许条目名里塞 ``../../etc/passwd``，libarchive 会拒绝，py7zr 新版也
    有防护，但这里**再显式拦一道** —— 解压目标在用户可写的音乐目录里，
    被穿出去一次就是往系统盘写文件，代价太高，值得自己校验。
    """
    bad = []
    for raw in names or ():
        name = str(raw)
        norm = name.replace("\\", "/")
        if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
            bad.append(name)
            continue
        if ".." in norm.split("/"):
            bad.append(name)
    return bad


def _install_quiet_excepthook() -> None:
    """让 py7zr reporter 线程里的 :class:`_ExtractAbort` 安静退出。

    py7zr 在自己的 reporter 线程里调用回调，且**外面没有 try/except**，
    我们靠抛异常打断它 —— 于是 ``threading.excepthook`` 会打一串
    ``Exception in thread reporter`` 的 traceback。但那个中断原因**已经
    通过 ``aborted`` 列表传回主线程**了，再打一遍纯属噪音（打包成 exe 后
    还会被 crash.log 收进去，看起来像真崩溃）。

    这里只过滤这一个异常类型，其它异常原样交给原 hook —— 不会掩盖真问题。
    幂等，反复调用只装一次。
    """
    prev = threading.excepthook
    if getattr(prev, "_msu1_quiet_extract", False):
        return

    def _hook(args):
        if args.exc_type is _ExtractAbort:
            return
        prev(args)

    _hook._msu1_quiet_extract = True
    threading.excepthook = _hook


def _make_progress_callback(py7zr_mod, cancel, deadline, aborted):
    """构造 py7zr 的 ``ExtractCallback``：每个回调点检查取消与超时。

    ⚠⚠ **py7zr 的回调跑在它自己的 reporter 线程里**，不是调用
    ``extractall()`` 的那个线程。第一版直接在这里 ``raise``，结果异常只把
    reporter 线程打死（控制台还打一串 traceback），主线程的 ``except``
    完全捕不到，``extractall()`` 照样跑完 —— 表现为「超时/取消测试不生效」。

    所以中断原因必须写进 ``aborted``（``list.append`` 是线程安全的），
    由主线程在 join 之后读；抛异常只负责**让 reporter 线程停下来**。

    ``ExtractCallback`` 的六个抽象方法**必须全部实现**，少一个在实例化时
    就报 ``TypeError``（不是运行时才报），所以一个都不能省。
    """
    callbacks = py7zr_mod.callbacks

    class _Progress(callbacks.ExtractCallback):
        def _check(self) -> None:
            if cancel is not None and cancel.is_set():
                aborted.append("已取消解压（已保留原包）")
                raise _ExtractAbort("已取消解压（已保留原包）")
            if deadline is not None and time.monotonic() >= deadline:
                aborted.append("解压超时，已保留原包")
                raise _ExtractAbort("解压超时，已保留原包")

        def report_start_preparation(self) -> None:
            self._check()

        def report_start(self, processing_file_path, processing_bytes) -> None:
            self._check()

        def report_update(self, decompressed_bytes) -> None:
            self._check()

        def report_end(self, processing_file_path, wrote_bytes) -> None:
            self._check()

        def report_warning(self, message) -> None:
            """警告不中断（例如时间戳/权限恢复失败），继续解。"""

        def report_postprocess(self) -> None:
            self._check()

    return _Progress()


def _extract_zip_stdlib(archive: str,
                        target: str,
                        cancel: Optional[threading.Event],
                        timeout: float) -> Tuple[bool, List[str], str]:
    """用**标准库** ``zipfile`` 解 ``.zip``，契约与其它两个分支完全一致。

    ## 为什么非有不可（这是真 bug，不是补强）

    ``ARCHIVE_EXTS`` 是 ``(".7z", ".zip")`` —— joshw 确实有机种用 ``.zip``。
    而没有 ``tar.exe`` 的机器上，:func:`extract_archive` 会直接进
    ``_extract_with_py7zr``，但 **py7zr 只认 7z**：``.zip`` 会被它判成
    「不是有效的 7z 文件（可能下载不完整）」。用户看到的就是
    **「下载完了，但解压失败」** —— 和 tkinter 版当年换机报的那个障
    一模一样。

    ``zipfile`` 是标准库，零外部依赖、零体积成本，所以这条退路必须留着。

    ⚠ 不能图省事用 ``shutil.unpack_archive``：它按扩展名猜格式，
      在 Windows 上同样会走到 ``tarfile``，而 ``tarfile`` 解 zip 是靠
      ``tarfile`` 的 zip 兼容层 —— 那条路对「有目录项 / 有符号链接」的包
      行为不稳定，直接用 ``zipfile`` 更可控。
    """
    import zipfile

    before = _snapshot_key(target)
    deadline = (time.monotonic() + timeout) if timeout else None
    try:
        zf = zipfile.ZipFile(archive, "r")
    except zipfile.BadZipFile as exc:
        return (False, [],
                "不是有效的 zip 文件（可能下载不完整）：%s。已保留原包。" % exc)
    except OSError as exc:
        return False, [], "打不开归档：%s" % exc

    with zf:
        names = zf.namelist()
        bad = unsafe_members(names)
        if bad:
            return (False, [],
                    "归档内含非法路径（试图写到解压目录之外）：%s。"
                    "已拒绝解压并保留原包。" % bad[0])
        # 逐条解而不是 extractall()：只有逐条才能在中途响应取消/超时，
        # 一个 500MB 的 zip 一进去就出不来，用户点「停止」也没反应。
        for n in names:
            if cancel is not None and cancel.is_set():
                return False, [], "已取消解压（已保留原包）"
            if deadline is not None and time.monotonic() >= deadline:
                return (False, [],
                        "解压超时（超过 %.0f 秒），已保留原包" % timeout)
            try:
                zf.extract(n, target)
            except Exception as exc:                    # noqa: BLE001
                return (False, [],
                        "解压失败（zipfile）：%s。已保留原包，请手动解压。" % exc)

    after = _snapshot_key(target)
    created = sorted(after[key] for key in after.keys() - before.keys())
    if not created:
        created = sorted(after.values())
    if cancel is not None and cancel.is_set():
        return False, [], "已取消"
    return True, created, ""


def _extract_with_py7zr(archive: str,
                        target: str,
                        cancel: Optional[threading.Event],
                        timeout: float) -> Tuple[bool, List[str], str]:
    """没有 ``tar.exe`` 时的退路：``.zip`` 走标准库，``.7z`` 走 py7zr。

    返回 ``(ok, 解出的文件列表, 出错原因)``；失败时列表为空、原包保留。

    实现要点：``extractall()`` 放在 worker 线程里跑，主线程只做「轮询
    join + 读共享状态」。理由见 :func:`_make_progress_callback` —— 直接在
    主线程调 ``extractall()`` 的话，回调里的中断信号传不出来。
    """
    # .zip 先分派走 —— py7zr 只认 7z，拿 zip 进去必然报「不是有效的 7z」。
    if os.path.splitext(str(archive))[1].lower() == ".zip":
        return _extract_zip_stdlib(archive, target, cancel, timeout)

    py7zr = _import_py7zr()
    if py7zr is None:
        return (False, [],
                "本机找不到能解 7z 的工具：既没有 bsdtar/tar.exe，"
                "也没装 py7zr。已保留原包，请手动解压。")

    deadline = (time.monotonic() + timeout) if timeout else None
    before = _snapshot_key(target)
    _install_quiet_excepthook()
    aborted: List[str] = []       # 回调线程写、主线程读
    failed: List[str] = []        # 非中断类错误（坏包 / 加密 / 非法路径）
    bad_path: List[str] = []

    def _worker() -> None:
        try:
            with py7zr.SevenZipFile(archive, mode="r") as zf:
                bad = unsafe_members(zf.getnames())
                if bad:
                    bad_path.append(bad[0])
                    return
                zf.extractall(
                    path=target,
                    callback=_make_progress_callback(
                        py7zr, cancel, deadline, aborted),
                )
        except _ExtractAbort:
            pass                       # 预期中断，原因已写进 aborted
        except py7zr.exceptions.PasswordRequired:
            failed.append("归档已加密，需要密码。已保留原包。")
        except py7zr.exceptions.Bad7zFile as exc:
            failed.append("不是有效的 7z 文件（可能下载不完整）：%s" % exc)
        except Exception as exc:       # noqa: BLE001 - 解压失败不抛给调用方
            failed.append("解压失败（py7zr）：%s。已保留原包，请手动解压。" % exc)

    worker = threading.Thread(target=_worker, name="py7zr-extract", daemon=True)
    worker.start()
    # 轮询而不是一把 join(timeout)：取消信号可能比超时先到，得早点看见。
    while worker.is_alive():
        worker.join(_EXTRACT_POLL)
        if cancel is not None and cancel.is_set() and not aborted:
            # 回调可能卡在没有检查点的长块里；这里补一次，然后等它收尾。
            aborted.append("已取消解压（已保留原包）")
        if deadline is not None and time.monotonic() >= deadline + _KILL_GRACE:
            break
    if worker.is_alive():
        # reporter 线程可能在解一个巨大的单块，给它最后一点时间退出；
        # 无论退没退干净，调用方都必须拿到明确的失败结论。
        worker.join(_KILL_GRACE)

    if bad_path:
        return (False, [],
                "归档内含非法路径（试图写到解压目录之外）：%s。"
                "已拒绝解压并保留原包。" % bad_path[0])
    if aborted:
        return False, [], aborted[0]
    if failed:
        return False, [], failed[0]
    if worker.is_alive():
        return False, [], "解压超时，已保留原包"

    after = _snapshot_key(target)
    created = sorted(after[key] for key in after.keys() - before.keys())
    if not created:
        created = sorted(after.values())
    if cancel is not None and cancel.is_set():
        return False, [], "已取消"
    return True, created, ""


def extract_archive(archive: str,
                    dest_dir: Optional[str] = None,
                    cancel: Optional[threading.Event] = None,
                    timeout: Optional[float] = None) -> Tuple[bool, List[str], str]:
    """把一个 ``.7z`` / ``.zip`` 解到 ``dest_dir``（默认与包同目录的同名子目录）。

    整个过程**不阻塞调用方的事件循环** —— 它跑在下载 worker 线程里，
    GUI 主线程只通过 :meth:`Downloader.extract_stats` 读计数。

    Args:
        archive: 归档文件绝对路径。
        dest_dir: 解压目标目录；``None`` 时用 ``<包所在目录>/<包名去扩展名>``。
            joshw 的包内部通常自带一层机种目录，所以这里**总是新建子目录**，
            避免几十个包的文件互相覆盖。
        cancel: 取消信号。为 None 时不可取消。
        timeout: 子进程超时秒数。``None`` 时按包大小自动算
            （见 :func:`extract_timeout_for`）。

    Returns:
        ``(ok, 解出的文件列表, 出错原因)``。``ok`` 为 False 时 ``files`` 为空，
        原因文本已经能直接展示给用户。**任何失败都不删原包。**
    """
    archive = os.path.abspath(str(archive or ""))
    if not os.path.isfile(archive):
        return False, [], "归档不存在或已被移动"

    if cancel is not None and cancel.is_set():
        return False, [], "已取消"

    base = os.path.dirname(archive)
    stem = os.path.splitext(os.path.basename(archive))[0]
    target = os.path.abspath(dest_dir) if dest_dir else os.path.join(base, stem)

    tar_exe = find_tar()

    if timeout is None:
        try:
            timeout = extract_timeout_for(os.path.getsize(archive))
        except OSError:
            timeout = _EXTRACT_TIMEOUT
    try:
        timeout = max(0.05, float(timeout))
    except (TypeError, ValueError):
        timeout = _EXTRACT_TIMEOUT

    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        return False, [], "无法创建解压目录 %s：%s" % (target, exc)

    # 没有 bsdtar（Android / 精简版 Windows / 用户删了 tar.exe）→ 退到 py7zr。
    # 放在 makedirs 之后：py7zr 分支自己也要对已存在的目录做快照。
    if not tar_exe:
        return _extract_with_py7zr(archive, target, cancel, timeout)

    # 先记下目标目录里已有的文件, 之后只把"新出现的"算作本次解出的成果。
    # 直接用目录 diff 而不是解析 tar 的输出: tar -xv 的输出格式随版本变,
    # 目录 diff 是语言无关且 100% 可靠的。
    # diff 的键走 normcase（大小写不敏感），但返回给调用方的是**原始大小写**。
    before = _snapshot_key(target)
    cmd = [tar_exe, "-xf", archive, "-C", target]
    try:
        # ⚠ **必须带 winspawn.silent_kw()**：tar.exe（bsdtar）是控制台程序，
        #   本程序是窗口程序 —— 不加的话**每解压一个包就闪一次黑框**，
        #   一晚上挂 20 个包就是 20 次闪屏。用户报的「解压环节弹黑框」就是它。
        proc = subprocess.Popen(                            # noqa: S603 - shell=False, 参数全为绝对路径
            cmd,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **winspawn.silent_kw(),
        )
    except FileNotFoundError:
        # find_tar() 找到了、Popen 时却没了 —— 中间被杀软删掉，或路径是
        # 「App Paths」那种转发壳。同样换备用解压器，别直接判死。
        ok, files, why = _extract_with_py7zr(archive, target, cancel, timeout)
        if ok:
            return True, files, ""
        return False, [], "解压器已消失：%s（备用解压器也不行：%s）" % (tar_exe, why)
    except OSError as exc:
        return False, [], "无法启动解压器：%s" % exc

    # 自己轮询而不是 subprocess.run(timeout=)，因为要同时支持"取消"和"超时"，
    # 并且取消时要 terminate -> 5s -> kill 的升级流程。
    deadline = time.monotonic() + timeout
    cancelled = False
    timed_out = False
    # 因为 stderr=STDOUT，tar 的报错被并进了 stdout，所以真正要的是
    # communicate() 返回值的**第一个**元素。之前误取了第二个（stderr，
    # 此时恒为空），导致用户看到的失败原因永远是空��。
    output = b""
    while True:
        try:
            output, _stderr = proc.communicate(timeout=_EXTRACT_POLL)
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break

    if cancelled or timed_out:
        proc.terminate()
        try:
            proc.communicate(timeout=_KILL_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=_KILL_GRACE)
            except subprocess.TimeoutExpired:            # 极端情况：kill 后仍不收
                pass
        if cancelled:
            return False, [], "已取消解压（已保留原包）"
        return (False, [],
                "解压超时（超过 %.0f 秒），已保留原包" % timeout)

    if proc.returncode != 0:
        text = _decode(output)
        detail = text.strip().splitlines()[-1] if text.strip() else ""
        # ⚠ tar.exe **存在但跑挂了**也要试退路。换机失败的另一半原因就在这儿：
        #   精简镜像里的 tar.exe 可能被组策略禁跑、被杀软拦下、或版本老到不认
        #   7z。只看「tar.exe 在不在」判断可用性是不够的 —— 得看它**真的解出来
        #   没有**。这里失败不立刻放弃，换 py7zr / zipfile 再试一次；两个都不行
        #   才把 bsdtar 的原始报错交给用户（那个信息量最大）。
        ok, files, why = _extract_with_py7zr(archive, target, cancel, timeout)
        if ok:
            return True, files, ""
        return (False, [],
                "解压失败（退出码 %d）%s。已保留原包，请手动解压。"
                % (proc.returncode,
                   ("：" + detail) if detail else
                   ("；备用解压器也不行：%s" % why)))

    after = _snapshot_key(target)
    # 保留原始大小写输出；只有"哪些是新的"这一步用小写键比较。
    created = sorted(after[key] for key in after.keys() - before.keys())
    if not created:
        # 退出码 0 但没有新文件: 多数是包里就是这些文件（重解同一个包）。
        created = sorted(after.values())
    if cancel is not None and cancel.is_set():
        return False, [], "已取消"
    return True, created, ""


def _decode(raw) -> str:
    """子进程输出字节 -> 文本, 永不抛异常。"""
    if not raw:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _snapshot_dir(root: str) -> set:
    """目录内所有文件的**原始大小写**路径集合（递归）。不存在时返回空集。

    刻意**不做** :func:`os.path.normcase` —— 那个函数在 Windows 上会把路径
    整体转小写，而本函数的返回值要直接作为 ``extract_done`` 事件里的
    ``files`` 交给界面显示。返回小写路径会让
    ``Antarctic Adventure [...].nsf`` 显示成全小写。大小写不敏感的比较
    交给 :func:`_snapshot_key` 在 diff 时单独做。
    """
    found = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            found.add(os.path.join(dirpath, name))
    return found


def _snapshot_key(root: str) -> Dict[str, str]:
    """``{比较用的小写键: 原始路径}``，用于大小写不敏感的新旧文件 diff。"""
    return {_compare_key(path): path for path in _snapshot_dir(root)}


def _compare_key(path: str) -> str:
    """diff 用的比较键：绝对化 + 归一化分隔符 + 小写（仅 Windows）。"""
    text = os.path.normpath(os.path.abspath(str(path)))
    return os.path.normcase(text)


def sanitize_filename(name: str, max_length: int = _MAX_NAME_LENGTH) -> str:
    """把归档名清洗成 Windows 合法文件名。

    规则：去掉 ``< > : " / \\ | ? *`` 与控制字符、去掉结尾的点和空格、
    规避 ``CON``/``NUL`` 等保留设备名、总长截断到 ``max_length``
    且尽量保留扩展名（``.7z``）。

    >>> sanitize_filename('S.O.S. - Sink or Swim [SNES].7z')
    'S.O.S. - Sink or Swim [SNES].7z'
    """
    text = str(name or "")
    text = text.replace("\\", "/")
    text = text.rsplit("/", 1)[-1]  # 只留 basename
    text = _ILLEGAL_RE.sub("_", text).strip()
    text = text.rstrip(". ")
    if not text:
        text = "unnamed"
    limit = max(8, int(max_length))
    if len(text) > limit:
        stem, ext = os.path.splitext(text)
        if ext and len(ext) < limit - 1:
            keep = limit - len(ext)
            text = stem[:keep].rstrip(". ") + ext
        else:
            text = text[:limit].rstrip(". ")
    if text.split(".", 1)[0].strip().lower() in _RESERVED_NAMES:
        text = "_" + text
    return text


def build_dest(directory: str, name: str) -> str:
    """把目录与归档名拼成完整的目标文件路径（自动清洗文件名）。"""
    return os.path.join(directory, sanitize_filename(name))


class Downloader:
    """多线程断点续传下载器。

    Args:
        workers: 并发 worker 数，至少 1。
        on_event: ``on_event(kind, payload)`` 事件回调，可为 ``None``。
        timeout: 单次 HTTP 请求超时秒数。
        auto_extract: 下载完成后是否自动解压归档（默认 True）。
        delete_archive: 解压成功后是否删除原 ``.7z``（默认 False —— 保留原包，
            万一解出来的文件不对还能重新解）。仅在 ``auto_extract`` 为 True
            且解压成功时才会删。
        extract_dir: 解压目标根目录。``None`` 时每个包解到
            ``<保存目录>/<包名去扩展名>/``。joshw 的包内部通常自带一层
            机种目录，因此**不**平铺到保存目录根，避免同名互相覆盖。
    """

    def __init__(self, workers: int = 2,
                 on_event: Optional[Callable[[str, Dict[str, object]], None]] = None,
                 timeout: float = 30.0,
                 auto_extract: bool = True,
                 delete_archive: bool = False,
                 extract_dir: Optional[str] = None) -> None:
        self._workers = max(1, int(workers))
        self._on_event = on_event
        self._timeout = float(timeout) if timeout and timeout > 0 else 30.0
        self._auto_extract = bool(auto_extract)
        self._delete_archive = bool(delete_archive)
        self._extract_dir = str(extract_dir) if extract_dir else None
        self._lock = threading.Lock()
        self._queue: "queue.Queue[Optional[Dict[str, object]]]" = queue.Queue()
        self._cancel_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self._supervisor: Optional[threading.Thread] = None
        self._running = False
        self._all_done_sent = False
        self._total_expected = 0
        self._done_bytes = 0
        self._pending = 0  # 未完成（排队 + 进行中）任务数
        self._failed = 0
        self._finished = 0
        self._extracted = 0        # 解压成功的包数
        self._extracted_files = 0  # 解出来的文件总数
        self._extract_failed = 0   # 解压失败的包数

    # -- 事件 ---------------------------------------------------------------
    def _emit(self, kind: str, payload: Dict[str, object]) -> None:
        """触发事件回调；回调异常一律吞掉，避免影响下载线程。"""
        callback = self._on_event
        if callback is None:
            return
        try:
            callback(kind, dict(payload))
        except Exception:  # noqa: BLE001 - GUI 回调异常不应中断下载
            pass

    # -- 队列 ---------------------------------------------------------------
    def add(self, url: str, dest: str, size: int = 0) -> None:
        """加入一个下载任务。

        Args:
            url: 归档的绝对 URL。
            dest: 目标文件的**完整路径**（父目录自动创建）；文件名会被
                :func:`sanitize_filename` 清洗，真实落盘路径通过
                ``file_start`` 事件的 ``file`` 字段给出。
            size: 预期字节数，未知填 0（仅影响进度条总量）。
        """
        directory = os.path.dirname(dest) or os.curdir
        target = os.path.join(directory, sanitize_filename(os.path.basename(dest)))
        try:
            expected = int(size)
        except (TypeError, ValueError):
            expected = 0
        expected = max(0, expected)
        task = {"url": str(url), "dest": target, "size": expected}
        with self._lock:
            self._pending += 1
            self._total_expected += expected
        self._queue.put(task)

    def start(self) -> None:
        """启动 worker 线程（非阻塞，用 :meth:`wait` 等完成）。"""
        with self._lock:
            if self._running:
                return
            if self._pending <= 0 and self._queue.empty():
                # 没有任务也要给调用方一个确定的收尾事件
                self._running = False
            else:
                self._running = True
        self._cancel_event.clear()
        self._all_done_sent = False
        self._threads = []
        for index in range(self._workers):
            thread = threading.Thread(
                target=self._worker_loop,
                name="joshw-dl-%d" % index,
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        self._supervisor = threading.Thread(
            target=self._supervise, name="joshw-dl-supervisor", daemon=True
        )
        self._supervisor.start()

    def _supervise(self) -> None:
        """等所有 worker 退出后收尾：确保 ``all_done`` 恰好发一次。"""
        for thread in list(self._threads):
            thread.join()
        with self._lock:
            self._running = False
            send = not self._all_done_sent
            self._all_done_sent = True
        if send:
            self._emit("all_done", {})

    def wait(self, timeout: Optional[float] = None) -> bool:
        """等待全部完成；超时返回 ``False``。"""
        supervisor = self._supervisor
        if supervisor is None:
            return True
        supervisor.join(timeout)
        return not supervisor.is_alive()

    def cancel(self) -> None:
        """请求取消：不再取新任务，当前 1 MB 块写完后停，``.part`` 保留。"""
        self._cancel_event.set()
        dropped: List[Dict[str, object]] = []
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                break
            if task is not None:
                dropped.append(task)
            self._queue.task_done()
        for task in dropped:
            with self._lock:
                self._pending = max(0, self._pending - 1)
            self._emit("file_done",
                       {"file": str(task.get("dest", "")), "ok": False, "error": "已取消"})

    def is_running(self) -> bool:
        """是否仍在运行。"""
        with self._lock:
            return self._running

    def total_bytes(self) -> int:
        """所有已加入任务的预期总字节数。"""
        with self._lock:
            return self._total_expected

    def done_bytes(self) -> int:
        """当前全局累计已下载字节数。"""
        with self._lock:
            return self._done_bytes

    def pending_count(self) -> int:
        """尚未完成（排队中 + 下载中）的任务数。"""
        with self._lock:
            return self._pending

    def failed_count(self) -> int:
        """已失败（含取消）的任务数。"""
        with self._lock:
            return self._failed

    def extract_stats(self) -> Tuple[int, int, int]:
        """``(解压成功的包数, 解出的文件总数, 解压失败的包数)``。"""
        with self._lock:
            return self._extracted, self._extracted_files, self._extract_failed

    # -- worker -------------------------------------------------------------
    def _worker_loop(self) -> None:
        while True:
            if self._cancel_event.is_set():
                return
            try:
                task = self._queue.get(timeout=0.1)
            except queue.Empty:
                with self._lock:
                    if self._pending <= 0:
                        return  # 队列空且无在途任务 -> 退出
                continue
            if task is None:
                self._queue.task_done()
                return
            try:
                self._run_task(task)
            finally:
                self._queue.task_done()

    def _run_task(self, task: Dict[str, object]) -> None:
        url = str(task.get("url", ""))
        dest = str(task.get("dest", ""))
        size = int(task.get("size", 0) or 0)
        self._emit("file_start", {"file": dest, "size": size})

        if self._cancel_event.is_set():
            with self._lock:
                self._pending = max(0, self._pending - 1)
                self._failed += 1
            self._emit("file_done", {"file": dest, "ok": False, "error": "已取消"})
            return

        ok, error = self._download_with_retry(url, dest, size)
        if not ok:
            with self._lock:
                self._pending = max(0, self._pending - 1)
                self._failed += 1
            self._emit("file_done", {"file": dest, "ok": False, "error": error})
            return

        self._emit("file_done", {"file": dest, "ok": True, "error": ""})

        # 下载成功后才解压; 解压失败不影响"下载成功"这个事实, 但要单独
        # 发事件说明 —— 用户要看到"包下来了, 但没解开", 而不是被静默吞掉。
        #
        # 注意计数顺序: **解压必须占用 _pending**, 也就是在这里才 -= 1。
        # 若先 -= 1 再解压, _worker_loop 看到 _pending<=0 会立刻退出,
        # _supervise 随即发 all_done, 界面显示"完成"时 tar 其实还在跑,
        # 用户点"打开目录"会看到半成品。解压段用 try/finally 保证一定归还计数。
        try:
            self._maybe_extract(dest)
        finally:
            with self._lock:
                self._pending = max(0, self._pending - 1)
                self._finished += 1

    def _maybe_extract(self, dest: str) -> None:
        """下载完成后按需解压。非归档 / 已取消 / 关闭解压开关时直接返回。"""
        if not self._auto_extract or not is_archive(dest):
            return
        if self._cancel_event.is_set():
            return
        out_dir = extract_dir_for(dest, self._extract_dir)
        self._emit("extract_start", {"file": dest, "dest": out_dir})
        ok, files, error = extract_archive(dest, out_dir, self._cancel_event)
        if ok:
            removed = False
            if self._delete_archive:
                try:
                    os.remove(dest)
                    removed = True
                except OSError:
                    removed = False
            with self._lock:
                self._extracted += 1
                self._extracted_files += len(files)
            self._emit("extract_done", {
                "file": dest, "dest": out_dir, "files": files,
                "count": len(files), "archive_removed": removed,
            })
            return
        with self._lock:
            self._extract_failed += 1
        self._emit("extract_failed", {
            "file": dest, "error": error,
            "reason": "已保留原包 %s，可手动解压后拖进主窗口"
                      % os.path.basename(dest),
        })

    def _download_with_retry(self, url: str, dest: str, size: int) -> Tuple[bool, str]:
        """下载单个文件，失败按 1/2/4 秒退避重试 3 次。"""
        part = dest + ".part"
        last_error = ""
        for attempt in range(_MAX_ATTEMPTS):
            if self._cancel_event.is_set():
                return False, "已取消"
            try:
                directory = os.path.dirname(dest)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                self._stream_to_part(url, part)
                os.replace(part, dest)
                return True, ""
            except urllib.error.HTTPError as exc:
                last_error = "HTTP %s %s" % (exc.code, exc.reason)
                if exc.code == 416:
                    # Range 超出文件长度：丢弃 .part 从头再来
                    self._remove_quietly(part)
                if exc.code in (401, 403, 404, 410):
                    return False, last_error  # 确定性失败，重试无意义
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last_error = "%s: %s" % (type(exc).__name__, exc)
            if attempt + 1 < _MAX_ATTEMPTS and not self._cancel_event.is_set():
                time.sleep(_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)])
        if self._cancel_event.is_set():
            return False, "已取消"
        return False, last_error or "未知错误"

    @staticmethod
    def _remove_quietly(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def _stream_to_part(self, url: str, part: str) -> None:
        """把 url 流式写入 ``part``（支持续传），每 1 MB 上报一次进度。"""
        existing = 0
        if os.path.exists(part):
            try:
                existing = os.path.getsize(part)
            except OSError:
                existing = 0
        headers = {"User-Agent": USER_AGENT}
        if existing > 0:
            headers["Range"] = "bytes=%d-" % existing

        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(
            request, timeout=self._timeout, context=tls_context(host)
        ) as response:
            status = getattr(response, "status", None) or response.getcode()
            if existing > 0 and status == 206:
                mode = "ab"
                offset = existing
            else:
                mode = "wb"
                offset = 0
            with open(part, mode) as handle:
                while True:
                    if self._cancel_event.is_set():
                        handle.flush()
                        raise OSError("已取消")
                    chunk = response.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    self._bump_progress(len(chunk))
                handle.flush()

    def _bump_progress(self, amount: int) -> None:
        with self._lock:
            self._done_bytes += int(amount)
            done = self._done_bytes
            total = self._total_expected
        self._emit("progress", {"done": done, "total": total})
