#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_unpack.py — joshw_dl 自动解压链路的离线验收测试。

覆盖三件事：

1. **定位解压器**：正常找到 / 找不到时的降级 / 指定路径不存在返回 None；
2. **真的能解**：用真实 ``.7z`` 解到临时目录，校验产出的文件名与扩展名
   （含空格、中文、方括号），并确认走的是**参数列表 + shell=False**；
3. **失败与幂等**：损坏包要报错且**保留原 .7z**；重复解压不产生
   ``name (2).nsf`` 堆积；"已存在就跳过"要连带看解压产物。

**完全离线**：测试自己用 bsdtar 造 .7z 夹具（bsdtar 既能读也能写 7z），
不碰网络。端到端联网验收在 ``test_unpack.py --e2e``，默认不跑。

用法：
    python vendor/test_unpack.py            # 全部离线用例
    python vendor/test_unpack.py --json     # 机器可读（CI 用）
    python vendor/test_unpack.py -v         # 打印每条断言细节
    python vendor/test_unpack.py --e2e      # 额外跑真实 joshw 联网验收
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from typing import Callable, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import joshw_dl  # noqa: E402
from joshw_dl import Downloader  # noqa: E402

# 真实 joshw 包名风格：空格 + 方括号 + 括号 + 中文，最容易把 shell 拆词打爆。
AWKWARD_STEM = "Antarctic Adventure [Kekkyoku Nankyoku Daibouken] (1985-04-22)(Konami)"
CJK_STEM = "中文 测试 [方括号] (1985)(厂商)"
SIMPLE_STEM = "Game Genie"

# vendor/samples 里的真实 NSF，用作夹具内容（保证产物是真货而非空文件）
SAMPLES_NSF = os.path.join(ROOT, "vendor", "samples", "nsf")
REAL_NSF = os.path.join(SAMPLES_NSF, "Game Genie (19xx)(Galoob).nsf")


# ---------------------------------------------------------------------------
# 迷你测试框架（不引 pytest：这套文件要能直接跟 exe 一起跑）
# ---------------------------------------------------------------------------
class Failure(AssertionError):
    """断言失败。"""


class Registry:
    """用例注册 + 结果收集。"""

    def __init__(self, verbose: bool = False) -> None:
        self.results: List[Dict[str, object]] = []
        self.verbose = verbose
        self._current: Optional[str] = None

    def case(self, name: str):
        """装饰器：注册一个用例。"""
        def deco(func: Callable[[], None]) -> Callable[[], None]:
            self.run(name, func)
            return func
        return deco


REG = Registry()


def run(name: str, func: Callable[[], None]) -> None:
    """跑一个用例并记录结果（不抛异常，保证跑完所有用例）。"""
    sys.stdout.write("  %-58s " % name)
    sys.stdout.flush()
    started = time.monotonic()
    record: Dict[str, object] = {"name": name, "ok": False,
                                 "error": "", "detail": "", "secs": 0.0}
    try:
        detail = func()
        record["ok"] = True
        record["detail"] = str(detail or "")
    except Failure as exc:
        record["error"] = str(exc)
    except Exception:                                    # noqa: BLE001
        record["error"] = "未预期异常:\n" + traceback.format_exc()
    record["secs"] = round(time.monotonic() - started, 3)
    REG.results.append(record)
    if record["ok"]:
        sys.stdout.write("PASS (%.2fs)\n" % record["secs"])
        if REG.verbose and record["detail"]:
            for line in str(record["detail"]).splitlines():
                sys.stdout.write("        | %s\n" % line)
    else:
        sys.stdout.write("FAIL (%.2fs)\n" % record["secs"])
        for line in str(record["error"]).splitlines():
            sys.stdout.write("        ! %s\n" % line)
    sys.stdout.flush()


def check(condition: bool, message: str) -> None:
    """断言为真。"""
    if not condition:
        raise Failure(message)


def check_eq(actual, expected, message: str) -> None:
    """断言相等。"""
    if actual != expected:
        raise Failure("%s\n          实际: %r\n          期望: %r"
                      % (message, actual, expected))


def log(message: str) -> None:
    """在用例内打印一行（加前缀，便于和断言输出区分）。"""
    sys.stdout.write("\r        > %s\n" % message)
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# 夹具：用 bsdtar 造真实 .7z
# ---------------------------------------------------------------------------
def _tar() -> Optional[str]:
    return joshw_dl.find_tar()


def _payload_bytes() -> bytes:
    """取一个真实 NSF 的字节作为夹具内容；没有就造一段非空内容。

    真实样本优先 —— 这样夹具产出的 .nsf 能被 chip_formats.probe() 正常识别，
    端到端验收才有意义。
    """
    if os.path.isfile(REAL_NSF):
        with open(REAL_NSF, "rb") as handle:
            return handle.read()
    return b"NESM\x1a" + b"\x00" * 240


def _is_real_nsf(path: str) -> bool:
    """产物是否真的是一个可被 chip_formats 认出的 NSF。"""
    import chip_formats

    info = chip_formats.probe(path)
    return bool(info.ok) and str(info.fmt or "").upper() == "NSF"


def make_7z(directory: str, stem: str,
            entries: Optional[List[Tuple[str, bytes]]] = None) -> str:
    """在 ``directory`` 下造一个名为 ``<stem>.7z`` 的真实 7z 包。

    Args:
        directory: 存放目录。
        stem: 包名（不含 .7z），可以带空格/中文/方括号。
        entries: ``[(包内相对路径, 内容)]``；默认放一个 ``<stem>.nsf``。

    Returns:
        造出来的 ``.7z`` 绝对路径。
    """
    tar_exe = _tar()
    if not tar_exe:
        raise Failure("找不到 bsdtar，无法造 .7z 夹具")
    entries = entries or [("%s.nsf" % stem, _payload_bytes())]
    stage = tempfile.mkdtemp(prefix="7zstage_", dir=directory)
    try:
        for rel, blob in entries:
            path = os.path.join(stage, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(blob)
        pack = os.path.join(directory, stem + ".7z")
        # -a 让 bsdtar 依据扩展名选 7z 容器（bsdtar 原生支持写 7z）
        proc = subprocess.run([tar_exe, "-a", "-c", "-f", pack, "."],
                              cwd=stage, shell=False,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=120, check=False)
        if proc.returncode != 0 or not os.path.isfile(pack):
            raise Failure("造 7z 夹具失败: %s"
                          % joshw_dl._decode(proc.stdout).strip())
        return pack
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def truncate(path: str, keep: int = 64) -> None:
    """把文件截断成前 ``keep`` 字节 —— 制造"损坏的包"。"""
    with open(path, "rb") as handle:
        blob = handle.read(keep)
    with open(path, "wb") as handle:
        handle.write(blob)


# ===========================================================================
# 1. 定位解压器（find_tar）
# ===========================================================================
def t_find_tar_found():
    """1.1 正常情况：能找到 System32 的 bsdtar。"""
    tar = joshw_dl.find_tar()
    check(tar is not None, "find_tar() 返回 None，本机没有可用解压器")
    check(os.path.isfile(tar), "find_tar() 返回的路径不存在: %s" % tar)
    check(os.path.basename(tar).lower() == "tar.exe",
          "期望 tar.exe，实际 %s" % os.path.basename(tar))
    # 必须是全路径，不能是 PATH 上的裸 "tar" —— 后者是 GNU tar，不支持 7z
    check(os.path.dirname(tar), "返回的是裸命令名而非全路径，会命中 GNU tar")
    # 验证它真的是 bsdtar 而不是 GNU tar
    try:
        proc = subprocess.run([tar, "--version"], shell=False,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=30, check=False)
        banner = joshw_dl._decode(proc.stdout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Failure("无法执行 %s --version: %s" % (tar, exc))
    check("bsdtar" in banner.lower(),
          "不是 bsdtar（不支持 7z）:\n%s" % banner.strip())
    return "%s\n%s" % (tar, banner.strip().splitlines()[0])


def t_find_tar_missing_degrades():
    """1.2 降级：找不到解压器时下载仍可用，解压失败可见（陷阱③）。

    这里用「把 SystemRoot 指歪」来制造"没有解压器"，而不是直接改
    ``joshw_dl.find_tar`` —— 因为 :func:`find_tar` 自己也必须被验到。
    但**不能**在 SystemRoot 被改坏的状态下去建 socket：本机 HTTP 伺服
    依赖 Winsock，SystemRoot 歪了会直接 WinError 10106 起不来。
    所以顺序是：先把伺服和 URL 备好，只在真正要发请求的那一刻把
    SystemRoot 改掉，且下载结束立刻还原。
    """
    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, SIMPLE_STEM)

        # 伺服在**正常环境**下起好
        with _LocalHttp(tmp) as http:
            import urllib.parse
            url = "%s/%s" % (http.base_url,
                             urllib.parse.quote("%s.7z" % SIMPLE_STEM))
            bogus = os.path.join(tempfile.gettempdir(), "_no_such_winroot")

            # --- find_tar 本身：路径不存在 -> None ---
            saved = dict(os.environ)
            try:
                os.environ["SystemRoot"] = bogus
                os.environ["WINDIR"] = bogus
                check(joshw_dl.find_tar() is None,
                      "SystemRoot 指歪时 find_tar 应返回 None，实际 %r"
                      % (joshw_dl.find_tar(),))
                # 降级文案要能直接展示给用户
                ok, files, err = joshw_dl.extract_archive(pack)
                check(ok is False, "没有解压器时不应报成功")
                check(files == [], "失败时 files 应为空，实际 %r" % (files,))
                check("解压" in err, "文案没说明是解压问题: %r" % err)
                check("手动解压" in err, "文案没提示手动解压: %r" % err)
                check(os.path.isfile(pack), "降级路径竟然删了原包")
                log("降级文案: %s" % err)
            finally:
                os.environ.clear()
                os.environ.update(saved)

            # --- 下载链路：只把 find_tar 打桩成 None，其余照常 ---
            # （不靠改 SystemRoot，避免连带把 socket 也弄坏）
            real_find_tar = joshw_dl.find_tar
            joshw_dl.find_tar = lambda: None
            try:
                events: List[Tuple[str, Dict[str, object]]] = []
                dl = Downloader(workers=1, timeout=10, auto_extract=True,
                                on_event=lambda k, p: events.append((k, p)))
                dest = os.path.join(tmp, "dl", SIMPLE_STEM + ".7z")
                dl.add(url, dest, os.path.getsize(pack))
                dl.start()
                check(dl.wait(30), "无解压器时下载没收尾")
            finally:
                joshw_dl.find_tar = real_find_tar

        kinds = [k for k, _p in events]
        ok_evts = [p for k, p in events if k == "file_done"]
        check(ok_evts, "无解压器时连下载都没发生: %r" % kinds)
        check(ok_evts[0]["ok"] is True,
              "❌ 下载应仍然成功（解压器缺失不该影响下载）: %s"
              % ok_evts[0].get("error"))
        check(os.path.isfile(dest), "❌ 包没落盘")
        check_eq(os.path.getsize(dest), os.path.getsize(pack), "落盘大小不对")

        # 解压失败必须作为独立事件上报，而不是静默吞掉
        check("extract_failed" in kinds,
              "❌ 没有 extract_failed 事件 —— 降级必须可见，不能静默（%r）"
              % kinds)
        err_evt = [p for k, p in events if k == "extract_failed"][0]
        check("手动解压" in str(err_evt.get("error")),
              "extract_failed 文案应提示手动解压: %r" % err_evt)
        check(str(err_evt.get("reason") or ""),
              "extract_failed 应带 reason 说明原包已保留")
        check_eq(dl.extract_stats()[2], 1, "extract_stats 应记 1 个解压失败")
        log("事件序列: %s" % " -> ".join(kinds))
    return "无解压器：下载成功 + 解压失败可见且提示手动解压"


def t_find_tar_explicit_missing():
    """1.3 指定路径不存在时返回 None（不静默、不崩）。"""
    saved = dict(os.environ)
    try:
        # 显式给一个不存在的 SystemRoot
        bogus = os.path.join(tempfile.gettempdir(), "_nope_winroot")
        os.environ["SystemRoot"] = bogus
        os.environ["WINDIR"] = bogus
        result = joshw_dl.find_tar()
        check(result is None,
              "指定路径 %s 不存在时应返回 None，实际 %r" % (bogus, result))
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return "不存在路径 -> None"


# ===========================================================================
# 2. 解压真实 .7z
# ===========================================================================
def t_extract_awkward_name():
    """2.1 含空格/中文/方括号的包名能正确解开，文件名与扩展名都对。"""
    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, AWKWARD_STEM)
        check(os.path.isfile(pack), "夹具没造出来")

        ok, files, err = joshw_dl.extract_archive(pack)
        check(ok, "解压失败: %s" % err)
        check(len(files) == 1, "应解出 1 个文件，实际 %d: %r" % (len(files), files))

        produced = files[0]
        base = os.path.basename(produced)
        check_eq(base, "%s.nsf" % AWKWARD_STEM,
                 "产物文件名不对（空格/方括号被吃掉或被拆词了）")
        check(base.lower().endswith(".nsf"),
              "产物扩展名应为 .nsf，实际 %r" % base)
        check(os.path.isfile(produced), "报告的文件不存在: %s" % produced)

        # 内容必须是完整的（不是 0 字节 / 不是被截断），且必须还是可识别的 NSF
        with open(produced, "rb") as handle:
            blob = handle.read()
        check_eq(len(blob), len(_payload_bytes()), "产物大小与源文件不符")
        check_eq(blob, _payload_bytes(), "产物字节与源文件不一致")
        check(_is_real_nsf(produced),
              "产物不是可识别的 NSF（大小写/方括号可能损坏了文件名或内容）")

        # 解压目标目录：<包目录>/<包名去扩展名>
        want_dir = os.path.join(tmp, AWKWARD_STEM)
        check(os.path.isdir(want_dir), "没建出解压目录 %s" % want_dir)
        log("产物: %s (%d 字节)" % (base, len(blob)))
    return "文件名/扩展名/内容/目录 全部正确"


def t_extract_cjk_name():
    """2.2 全中文包名也能正确解开。"""
    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, CJK_STEM)
        ok, files, err = joshw_dl.extract_archive(pack)
        check(ok, "中文包名解压失败: %s" % err)
        check_eq(len(files), 1, "应解出 1 个文件")
        base = os.path.basename(files[0])
        check_eq(base, "%s.nsf" % CJK_STEM, "中文产物文件名不对")
        check(os.path.isfile(files[0]), "中文产物不存在")
        log("产物: %s" % base)
    return "中文名 OK"


def t_extract_uses_argv_not_shell():
    """2.3 走的是**参数列表 + shell=False**，不是拼 shell 字符串（陷阱）。"""
    # 直接验证：把 extract_archive 的 Popen 拦下来，看传的是什么
    captured: Dict[str, object] = {}
    real_popen = subprocess.Popen

    def spy_popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        captured["shell"] = kwargs.get("shell")
        return real_popen(cmd, *args, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, AWKWARD_STEM)
        joshw_dl.subprocess.Popen = spy_popen
        try:
            ok, _files, err = joshw_dl.extract_archive(pack)
        finally:
            joshw_dl.subprocess.Popen = real_popen

        check(ok, "解压失败: %s" % err)
        cmd = captured.get("cmd")
        check(isinstance(cmd, list),
              "cmd 必须是参数列表(list)，实际类型 %s —— 拼字符串会被拆词"
              % type(cmd).__name__)
        check(captured.get("shell") is False,
              "shell 必须是 False，实际 %r" % (captured.get("shell"),))
        # 归档路径必须是一个**完整**的参数，不能被空格切开
        check(pack in cmd, "cmd 里应原样含归档路径 %r，实际 %r" % (pack, cmd))
        check_eq(len(cmd), 5,
                 "cmd 应为 [tar,-xf,archive,-C,target] 五项，实际 %r" % (cmd,))
        check_eq(cmd[0], _tar(), "cmd[0] 必须是 bsdtar 全路径")
        check_eq(cmd[1], "-xf", "缺少 -xf")

    # 反证：如果真拼成 shell 字符串，GNU tar / cmd 一定会拆词失败
    parts = [_tar(), "-xf", os.path.join(tempfile.gettempdir(),
                                         "%s.7z" % AWKWARD_STEM)]
    shell_str = " ".join(parts)
    check(" " in shell_str, "拼出来的字符串确实含空格（会被 shell 拆词）")
    check(AWKWARD_STEM in shell_str, "字符串里确实有那个难缠的包名")
    log("argv 长度=%d, shell=%r" % (len(cmd), captured.get("shell")))
    return "argv list + shell=False 已确认；拼字符串必然拆词"


def t_extract_to_explicit_dir():
    """2.4 指定 dest_dir 时产物落在那，且目录规则可被 GUI 预先算出。"""
    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, AWKWARD_STEM)
        out = os.path.join(tmp, "_custom_out")
        ok, files, err = joshw_dl.extract_archive(pack, out)
        check(ok, "解压失败: %s" % err)
        check(os.path.isdir(out), "没建出自定义目录")
        for path in files:
            check(os.path.dirname(os.path.normcase(path))
                  == os.path.normcase(out),
                  "产物不在指定目录: %s" % path)

        # extract_dir_for 与实际落盘一致（GUI 靠它做跳过判断）
        predicted = joshw_dl.extract_dir_for(pack)
        check_eq(os.path.normcase(predicted),
                 os.path.normcase(os.path.join(tmp, AWKWARD_STEM)),
                 "extract_dir_for 与默认解压目录不一致")
    return "dest_dir 与 extract_dir_for 一致"


def t_extract_makes_subdirs():
    """2.5 包内自带子目录时，目录结构被完整还原。"""
    entries = [("NSF/main.nsf", _payload_bytes()),
               ("NSF/sub/second [x] (1990).nsf", _payload_bytes())]
    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, SIMPLE_STEM, entries)
        ok, files, err = joshw_dl.extract_archive(pack)
        check(ok, "解压失败: %s" % err)
        check(len(files) == 2, "应解出 2 个文件，实际 %d: %r" % (len(files), files))
        names = sorted(os.path.basename(f) for f in files)
        check_eq(names, ["main.nsf", "second [x] (1990).nsf"], "文件名不对")
        check(any(os.sep + "sub" + os.sep in f for f in files),
              "嵌套子目录没还原: %r" % files)
    return "子目录结构完整"


# ===========================================================================
# 3. 失败处理
# ===========================================================================
def t_extract_corrupt_keeps_archive():
    """3.1 损坏包：报错可识别，且**不删原 .7z**。"""
    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, SIMPLE_STEM)
        truncate(pack, keep=48)          # 只留 48 字节 -> 必然解不开
        before_size = os.path.getsize(pack)

        ok, files, err = joshw_dl.extract_archive(pack)
        check(ok is False, "损坏包竟然报成功")
        check(files == [], "失败时 files 应为空，实际 %r" % (files,))
        check(err, "失败时必须给出原因")
        check("解压失败" in err or "退出码" in err,
              "错误文案不可识别: %r" % err)
        check("保留原包" in err, "错误文案应说明保留了原包: %r" % err)
        # 文案里必须带上解压器**真正的**报错，而不是 Python 对象 repr。
        # 踩过的坑：曾经把 proc.stdout（BufferedReader）直接解码，
        # 用户看到的是 "<_io.BufferedReader name=3>"。
        check("BufferedReader" not in err and "_io." not in err,
              "❌ 错误文案里混进了 Python 对象 repr: %r" % err)
        # 截断包时 bsdtar 会说 "tar.exe: Seek error" 之类；只要不是我们
        # 自己编的字，就说明确实把子进程的输出带出来了。
        check("tar.exe:" in err or "bsdtar" in err,
              "错误文案没带上解压器自身的报错: %r" % err)

        check(os.path.isfile(pack), "❌ 损坏包被删了 —— 必须保留原包")
        check_eq(os.path.getsize(pack), before_size, "原包大小被改了")
        log("错误文案: %s" % err)
    return "报错可识别，原包保留"


def t_extract_missing_archive():
    """3.2 归档不存在：干净报错，不抛异常。"""
    with tempfile.TemporaryDirectory() as tmp:
        ghost = os.path.join(tmp, "not-there.7z")
        ok, files, err = joshw_dl.extract_archive(ghost)
        check(ok is False, "不存在的包应报失败")
        check("不存在" in err, "文案应说明不存在: %r" % err)
    return "缺失归档 -> 干净报错"


def t_extract_timeout_keeps_archive():
    """3.3 超时：报错且保留原包（用极短 timeout 模拟卡住）。"""
    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, SIMPLE_STEM)
        ok, files, err = joshw_dl.extract_archive(pack, timeout=0.001)
        check(ok is False, "超时场景不应报成功")
        check(files == [], "超时时 files 应为空")
        check("超时" in err or "取消" in err,
              "文案应说明超时/取消: %r" % err)
        check(os.path.isfile(pack), "❌ 超时后原包被删了")
    return "超时 -> 保留原包"


def t_extract_cancel_stops_promptly():
    """3.4 取消：terminate 后能较快返回，且保留原包。"""
    with tempfile.TemporaryDirectory() as tmp:
        # 多塞点内容，让解压稍微有点工作量
        entries = [("d%d/big.nsf" % i, _payload_bytes() * 200)
                   for i in range(60)]
        pack = make_7z(tmp, SIMPLE_STEM, entries)
        cancel = threading.Event()
        threading.Timer(0.05, cancel.set).start()   # 极快就取消

        started = time.monotonic()
        ok, files, err = joshw_dl.extract_archive(pack, cancel=cancel)
        elapsed = time.monotonic() - started

        check(ok is False, "取消后不应报成功")
        check("取消" in err, "文案应说明是取消: %r" % err)
        check(elapsed < 30, "取消后应较快返回，实际 %.1fs" % elapsed)
        check(os.path.isfile(pack), "❌ 取消后原包被删了")
        log("取消耗时 %.2fs，文案: %s" % (elapsed, err))
    return "取消 %.2fs 返回，原包保留" % elapsed


def t_extract_precancelled():
    """3.5 解压前就已取消：立刻返回，不启动子进程。"""
    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, SIMPLE_STEM)
        cancel = threading.Event()
        cancel.set()
        started = time.monotonic()
        ok, _files, err = joshw_dl.extract_archive(pack, cancel=cancel)
        elapsed = time.monotonic() - started
        check(ok is False, "预取消应报失败")
        check("取消" in err, "文案应说明取消: %r" % err)
        check(elapsed < 3, "预取消应立刻返回，实际 %.2fs" % elapsed)
    return "预取消 %.3fs 返回" % elapsed


# ===========================================================================
# 4. 幂等 / 已存在就跳过
# ===========================================================================
def t_extract_twice_no_dupes():
    """4.1 同一目录解两次：文件数不变，不产生 "name (2).nsf"。"""
    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, SIMPLE_STEM,
                       entries=[("a.nsf", _payload_bytes()),
                                ("b.nsf", _payload_bytes()),
                                ("c [1] (1990).nsf", _payload_bytes())])
        out = os.path.join(tmp, "out")

        ok1, files1, err1 = joshw_dl.extract_archive(pack, out)
        check(ok1, "第一次解压失败: %s" % err1)
        count1 = _count_files(out)
        names1 = _names(out)

        ok2, files2, err2 = joshw_dl.extract_archive(pack, out)
        check(ok2, "第二次解压失败: %s" % err2)
        count2 = _count_files(out)
        names2 = _names(out)

        check_eq(count1, 3, "第一次应解出 3 个文件")
        check_eq(count2, count1,
                 "解两次后文件数变了：%d -> %d（新增 %r）"
                 % (count1, count2, sorted(set(names2) - set(names1))))

        dupes = [n for n in names2 if "(2)" in n or "(1)" in n
                 and not n.startswith("c ")]
        check(not dupes, "出现了重复堆积文件: %r" % dupes)
        check_eq(sorted(names2), sorted(names1), "两次解出的文件名不一致")
        # 第二次没有新文件 -> 走 created 回退分支，files 应仍等于目录内容
        check_eq(len(files2), count2,
                 "重解时 files 应回退为目录全量，实际 %d != %d"
                 % (len(files2), count2))
        log("两次都是 %d 个文件: %s" % (count2, names2))
    return "幂等，无 (2) 堆积"


def _count_files(root: str) -> int:
    return sum(len(files) for _r, _d, files in os.walk(root))


def _names(root: str) -> List[str]:
    out: List[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            out.append(os.path.relpath(os.path.join(dirpath, name), root))
    return out


def t_skip_check_sees_payload():
    """4.2 「已存在就跳过」要连带看解压产物（陷阱②）。"""
    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, SIMPLE_STEM)
        # payload_files/has_payload 约定产物在 <extract_dir>/<包名去扩展名>/，
        # 与 _maybe_extract 落盘位置一致，所以这里也用同一套规则算 dest。
        root = os.path.join(tmp, "out")
        out = joshw_dl.extract_dir_for(pack, root)

        # a) 只有 .7z，没有产物 -> 没到手
        check(not joshw_dl.has_payload(pack, root),
              "没解过就不该算已就位")
        check_eq(joshw_dl.payload_files(pack, root), [], "没解过时产物应为空")

        # b) 解过 -> 产物在
        ok, _files, err = joshw_dl.extract_archive(pack, out)
        check(ok, "解压失败: %s" % err)
        payload = joshw_dl.payload_files(pack, root)
        check(payload, "解完应有产物（实际产物目录 %s）" % out)
        check(joshw_dl.has_payload(pack, root),
              "解完应算已就位（这正是 GUI 只看 .7z 会漏判的场景）")

        # c) 用户删了 .7z、只留产物目录 -> 仍算已就位（关键场景）
        os.remove(pack)
        check(not os.path.isfile(pack), "测试前提错：包应已删除")
        check(joshw_dl.has_payload(pack, root),
              "❌ 删了 .7z 只留产物时，应仍然算已就位，否则会误重下")
        log("删包留产物 -> has_payload=%s" % joshw_dl.has_payload(pack, root))

        # d) 空目录不算就位（否则删空文件夹就永久跳过）
        empty_root = os.path.join(tmp, "emptycase")
        os.makedirs(joshw_dl.extract_dir_for(pack, empty_root), exist_ok=True)
        check(not joshw_dl.has_payload(pack, empty_root),
              "空目录不该算已就位")

        # e) 只有说明文件（.txt）不算就位
        make_7z(tmp, "notes_only", entries=[("readme.txt", b"hello")])
        notes = os.path.join(tmp, "notes_only.7z")
        check(not joshw_dl.has_payload(notes),
              "只有 readme.txt 时不该算已就位")
    return "本体/产物/空目录/说明文件 四种情形均正确"


def t_skip_check_non_archive():
    """4.3 非归档文件（裸 .spc）不参与这套判断。"""
    with tempfile.TemporaryDirectory() as tmp:
        spc = os.path.join(tmp, "game.spc")
        with open(spc, "wb") as handle:
            handle.write(b"SNES-SPC700")
        check(joshw_dl.payload_files(spc) == [],
              "裸 .spc 不该有产物表")
        check(not joshw_dl.has_payload(spc), "裸 .spc 不该被判已就位")
    return "裸文件不误判"


def t_extract_dir_for_rules():
    """4.4 extract_dir_for 与 _maybe_extract 用同一套规则。"""
    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, AWKWARD_STEM)
        # 无 extract_dir -> 包所在目录
        check_eq(os.path.normcase(joshw_dl.extract_dir_for(pack)),
                 os.path.normcase(os.path.join(tmp, AWKWARD_STEM)),
                 "默认解压目录规则不对")
        # 有 extract_dir -> 解压根目录/包名
        check_eq(
            os.path.normcase(joshw_dl.extract_dir_for(pack, os.path.join(tmp, "R"))),
            os.path.normcase(os.path.join(tmp, "R", AWKWARD_STEM)),
            "指定解压根目录时的规则不对")

        # 实际落盘与预测一致（端到端对一次）
        root = os.path.join(tmp, "R")
        predicted = joshw_dl.extract_dir_for(pack, root)
        ok, files, err = joshw_dl.extract_archive(pack, predicted)
        check(ok, "解压失败: %s" % err)
        # 用 os.path.commonpath 判"在目录下"，避免直接 startswith 撞上
        # 目录名恰好是另一个目录前缀的坑
        for path in files:
            check_eq(os.path.commonpath([os.path.abspath(path),
                                         os.path.abspath(predicted)]),
                     os.path.abspath(predicted),
                     "落盘位置与预测不符: %s" % path)
        # 且预测目录就是 <root>/<包名去扩展名>
        check_eq(os.path.normcase(predicted),
                 os.path.normcase(os.path.join(root, AWKWARD_STEM)),
                 "指定解压根目录时的规则不对")
    return "规则一致"


# ===========================================================================
# 5. 线程 / 计数（陷阱①：解压必须占用 _pending）
# ===========================================================================
def t_pending_held_during_extract():
    """5.1 解压期间 _pending 仍占用计数（陷阱①：不能先 -1 再解压）。"""
    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, SIMPLE_STEM,
                       entries=[("d%d/x.nsf" % i, _payload_bytes() * 400)
                                for i in range(80)])

        # 直接检查 Downloader._run_task 的契约：把一次"下载"替换成瞬间成功，
        # 然后在 _maybe_extract 执行期间反复采样 pending_count()。
        dl = Downloader(workers=1, auto_extract=True)
        dl._pending = 1                       # 模拟 add() 排了一个任务

        samples: List[int] = []
        stop = threading.Event()

        original = dl._maybe_extract

        def spy(dest):
            # 在"解压中"这个时间窗里采样计数
            end = time.monotonic() + 0.3
            while time.monotonic() < end:
                samples.append(dl.pending_count())
                time.sleep(0.005)
            return original(dest)

        dl._maybe_extract = spy
        task = {"url": "http://127.0.0.1:1/x.7z", "dest": pack, "size": 0}

        # 让 _download_with_retry 立刻成功（不联网）
        dl._download_with_retry = lambda url, dest, size: (True, "")

        def drive():
            dl._run_task(task)

        th = threading.Thread(target=drive, daemon=True)
        th.start()
        th.join(30)
        stop.set()

        check(samples, "没采到任何样本")
        check(1 in samples,
              "❌ 解压期间没观察到 pending==1（采样到 %r）—— "
              "说明解压没占用计数，all_done 会在 tar 跑完前就发"
              % sorted(set(samples))[:8])
        # 整段解压期间都必须保持 1，不能中途掉到 0
        check(0 not in samples,
              "❌ 解压期间 pending 掉到 0（采样到 %r）—— "
              "此时 _worker_loop 会退出并触发 all_done"
              % sorted(set(samples))[:8])
        check_eq(dl.pending_count(), 0, "任务结束后 pending 应归零")
        log("解压期间采样 %d 次，全部为 1" % len(samples))
    return "解压全程 pending 恒为 1，结束后归零"


def t_all_done_after_extract():
    """5.2 真下载流程：all_done 必须晚于最后一个 extract_done。"""
    local_root = tempfile.mkdtemp(prefix="e2e_dl_")
    try:
        events: List[Tuple[str, Dict[str, object]]] = []
        # 造一个 7z，用 file:// 不行（Downloader 走 http），所以这里
        # 用一个本机临时 HTTP 服务器提供真实 .7z，完整跑一遍下载+解压链路。
        pack = make_7z(local_root, AWKWARD_STEM)
        with _LocalHttp(local_root) as http:
            url = "%s/%s.7z" % (http.base_url,
                                AWKWARD_STEM.replace(" ", "%20"))
            # 路径有空格/中文/方括号 -> 需要 quote
            import urllib.parse
            url = "%s/%s" % (http.base_url, urllib.parse.quote(
                "%s.7z" % AWKWARD_STEM))

            dest = os.path.join(local_root, "dl", AWKWARD_STEM + ".7z")
            dl = Downloader(workers=1, auto_extract=True, timeout=15,
                            on_event=lambda k, p: events.append((k, p)))
            dl.add(url, dest, os.path.getsize(pack))
            dl.start()
            finished = dl.wait(60)
            check(finished, "下载+解压未在 60s 内收尾")

        kinds = [k for k, _p in events]
        check("file_done" in kinds, "没有 file_done 事件: %r" % kinds)
        check("extract_start" in kinds, "没有触发解压: %r" % kinds)
        check("extract_done" in kinds,
              "解压没成功，事件序列: %r" % kinds)
        check("all_done" in kinds, "没有 all_done: %r" % kinds)

        # 顺序断言：all_done 必须在 extract_done 之后
        check(kinds.index("all_done") > kinds.index("extract_done"),
              "❌ all_done 早于 extract_done —— 界面会显示完成但文件还在解压")

        # 解出来的文件真实存在
        ex = [p for k, p in events if k == "extract_done"]
        check_eq(len(ex), 1, "应只有 1 个 extract_done")
        files = list(ex[0]["files"])
        check(files, "extract_done 没报文件")
        for path in files:
            check(os.path.isfile(path), "解压产物不存在: %s" % path)
        check(os.path.basename(files[0]) == "%s.nsf" % AWKWARD_STEM,
              "产物名不对: %s" % os.path.basename(files[0]))

        stats = dl.extract_stats()
        check_eq(stats[0], 1, "extract_stats 解压成功数不对")
        check_eq(stats[1], len(files), "extract_stats 文件数不对")
        check_eq(stats[2], 0, "不应有解压失败")
        log("事件序列: %s" % " -> ".join(kinds))
        log("产物: %s" % os.path.basename(files[0]))
        return "all_done 序号 %d > extract_done 序号 %d" % (
            kinds.index("all_done"), kinds.index("extract_done"))
    finally:
        shutil.rmtree(local_root, ignore_errors=True)


class _LocalHttp:
    """最小 HTTP 服务器，用来把本地 .7z 喂给 Downloader（离线跑真实链路）。"""

    def __init__(self, root: str) -> None:
        import http.server
        import threading as _th
        handler = _make_handler(root)
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = _th.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.port = self._server.server_address[1]
        self.base_url = "http://127.0.0.1:%d" % self.port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        return False


def _make_handler(root: str):
    import http.server
    import urllib.parse

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                       # noqa: N802
            rel = urllib.parse.unquote(self.path.lstrip("/"))
            path = os.path.join(root, *rel.split("/"))
            if not os.path.isfile(path):
                self.send_error(404)
                return
            with open(path, "rb") as handle:
                blob = handle.read()
            self.send_response(200)
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(blob)

        def log_message(self, *args):
            pass

    return Handler


# ===========================================================================
# 6. 非阻塞（解压不卡界面线程）
# ===========================================================================
def t_extract_runs_off_thread():
    """6.1 解压期间调用方线程仍可自由推进（不阻塞界面线程）。"""
    with tempfile.TemporaryDirectory() as tmp:
        pack = make_7z(tmp, SIMPLE_STEM,
                       entries=[("d%d/x.nsf" % i, _payload_bytes() * 500)
                                for i in range(120)])
        ticks: List[int] = []
        stop = threading.Event()

        def ticker():
            while not stop.is_set():
                ticks.append(1)
                time.sleep(0.005)

        th = threading.Thread(target=ticker, daemon=True)
        th.start()
        try:
            ok, _files, err = joshw_dl.extract_archive(pack)
        finally:
            stop.set()
            th.join(2)

        check(ok, "解压失败: %s" % err)
        check(len(ticks) > 5,
              "解压期间调用方线程几乎没推进（ticks=%d），疑似阻塞" % len(ticks))
        log("解压期间主线程推进 %d 次" % len(ticks))
    return "解压期间主线程保持响应"


# ===========================================================================
# 汇总
# ===========================================================================
CASES: List[Tuple[str, str, Callable[[], object]]] = [
    ("find_tar/正常找到 bsdtar", "定位解压器", t_find_tar_found),
    ("find_tar/找不到时降级且下载仍可用", "降级", t_find_tar_missing_degrades),
    ("find_tar/指定路径不存在返回 None", "降级", t_find_tar_explicit_missing),
    ("extract/空格中文方括号包名", "真实解压", t_extract_awkward_name),
    ("extract/全中文包名", "真实解压", t_extract_cjk_name),
    ("extract/argv + shell=False", "真实解压", t_extract_uses_argv_not_shell),
    ("extract/指定 dest_dir", "真实解压", t_extract_to_explicit_dir),
    ("extract/包内子目录还原", "真实解压", t_extract_makes_subdirs),
    ("extract/损坏包报错且保留原包", "失败处理", t_extract_corrupt_keeps_archive),
    ("extract/归档不存在", "失败处理", t_extract_missing_archive),
    ("extract/超时保留原包", "失败处理", t_extract_timeout_keeps_archive),
    ("extract/取消可 promptly 返回", "失败处理", t_extract_cancel_stops_promptly),
    ("extract/预取消立即返回", "失败处理", t_extract_precancelled),
    ("幂等/解两次文件数不变", "幂等与跳过", t_extract_twice_no_dupes),
    ("跳过判断/连带看解压产物", "幂等与跳过", t_skip_check_sees_payload),
    ("跳过判断/裸 spc 不误判", "幂等与跳过", t_skip_check_non_archive),
    ("解压目录规则/与落盘一致", "幂等与跳过", t_extract_dir_for_rules),
    ("陷阱①/解压占用 _pending", "计数与线程", t_pending_held_during_extract),
    ("陷阱①/all_done 晚于 extract_done", "计数与线程", t_all_done_after_extract),
    ("非阻塞/解压不卡调用方线程", "计数与线程", t_extract_runs_off_thread),
]


def run_all(verbose: bool) -> int:
    """跑全部离线用例，返回失败数。"""
    REG.verbose = verbose
    if not _tar():
        print("!! 找不到 bsdtar，无法跑解压用例。请确认 "
              "%%SystemRoot%%\\System32\\tar.exe 存在。")
        return 1
    print("解压器: %s" % _tar())
    print("样本源: %s" % (REAL_NSF if os.path.isfile(REAL_NSF) else "(无，用合成内容)"))
    print("\n运行 %d 个用例:\n" % len(CASES))
    group = ""
    for name, grp, func in CASES:
        if grp != group:
            group = grp
            print("[%s]" % group)
        run(name, func)
    passed = sum(1 for r in REG.results if r["ok"])
    failed = len(REG.results) - passed
    print("\n" + "=" * 72)
    print("结果: %d 通过 / %d 失败 / 共 %d" % (passed, failed, len(REG.results)))
    if failed:
        print("\n失败用例:")
        for record in REG.results:
            if not record["ok"]:
                print("  ✗ %s" % record["name"])
                first = str(record["error"]).splitlines()[0]
                print("      %s" % first)
    print("=" * 72)
    return failed


def run_e2e() -> int:
    """真实 joshw 联网端到端：下载 .7z -> 解压 -> 识别 -> 转 WAV。"""
    print("\n" + "=" * 72)
    print("端到端联网验收（joshw 真实站点）")
    print("=" * 72)
    e2e_main()
    return 0


def e2e_main() -> None:
    """跑真实链路。单独函数便于 --e2e 复用。"""
    import chip_decode
    import chip_formats
    import spc_wav

    work = tempfile.mkdtemp(prefix="e2e_real_")
    try:
        # 1) 从 joshw 找一个真实的 NSF .7z（返回 (显示名, url, size)）
        picked = _pick_live_pack()
        if picked is None:
            print("!! 没找到可下载的真实 .7z（站点结构可能变了）")
            return
        name, url, size = picked
        print("\n[1] 目标包: %s" % name)
        print("    URL: %s" % url)
        print("    索引大小: %s" % chip_formats.human_size(size))

        events: List[Tuple[str, Dict[str, object]]] = []
        dest = os.path.join(work, name + ".7z")
        dl = Downloader(workers=1, auto_extract=True, timeout=30,
                        on_event=lambda k, p: events.append((k, p)))
        dl.add(url, dest, size)
        print("\n[2] 开始下载 + 自动解压 ...")
        started = time.monotonic()
        dl.start()
        finished = dl.wait(180)
        elapsed = time.monotonic() - started
        check(finished, "180s 内没收尾")

        for kind, payload in events:
            if kind == "file_done":
                print("    file_done ok=%s %s"
                      % (payload.get("ok"), payload.get("error") or ""))
            elif kind == "extract_start":
                print("    extract_start -> %s"
                      % os.path.basename(str(payload.get("dest"))))
            elif kind == "extract_done":
                print("    extract_done %d 个文件, 删包=%s"
                      % (int(payload.get("count") or 0),
                         payload.get("archive_removed")))
                for f in list(payload.get("files") or [])[:3]:
                    print("       · %s (%s)"
                          % (os.path.basename(str(f)),
                             chip_formats.human_size(os.path.getsize(str(f)))))
            elif kind == "extract_failed":
                print("    extract_failed: %s" % payload.get("error"))
        print("    耗时 %.1fs, 解压统计=%s" % (elapsed, dl.extract_stats()))

        # 3) 校验：.7z 在 + 产物在
        check(os.path.isfile(dest), "!! 下载后 .7z 不存在")
        # Apache autoindex 的 Size 列是**四舍五入**过的（115.0K 实为 115280），
        # 所以不能用严格相等 —— 与 GUI 的 _size_matches 同一套容差规则。
        actual_size = os.path.getsize(dest)
        delta = abs(actual_size - size)
        check(delta <= max(2048, size * 0.03),
              "落盘大小与索引差太多: 实际 %d, 索引 %d, 差 %d"
              % (actual_size, size, delta))
        print("    落盘 %d 字节（索引 %d，autoindex 取整，差 %d）"
              % (actual_size, size, delta))

        ex = [p for k, p in events if k == "extract_done"]
        check(ex, "!! 没有 extract_done —— 解压没成功")
        produced = [f for f in ex[0]["files"]
                    if os.path.splitext(str(f))[1].lower() in (".nsf", ".nsfe")]
        check(produced, "!! 没解出 .nsf/.nsfe: %r" % (ex[0]["files"],))

        nsf = produced[0]
        print("\n[3] 解压产物: %s" % os.path.basename(nsf))

        # 4) 识别（等价于主窗口拖入文件后的 probe）
        info = chip_formats.probe(nsf)
        # 时长要走 GUI 真正用的 plan_duration 决策链：很多 NSF 不带
        # playlist 时长（exact_secs=0），此时由 default_secs + 静音检测
        # 决定，直接看 info.exact_secs 会误判成"时长为 0"。
        plan = chip_formats.plan_duration(info, default_secs=20.0,
                                          hard_cap_secs=600)
        print("\n[4] chip_formats.probe():")
        print("    fmt        = %s" % info.fmt)
        print("    system     = %s" % info.system)
        print("    title      = %s" % info.title)
        print("    n_tracks   = %s" % info.n_tracks)
        print("    exact_secs = %s (来源 %s)" % (info.exact_secs,
                                               info.exact_src or "-"))
        print("    总时长计划 = %.1fs (来源 %s, 静音检测=%s)"
              % (plan.secs, plan.source, plan.stop_on_silence))
        check(info.ok, "probe 判定不可用: %s" % info.reason)
        check(str(info.fmt or "").upper().startswith("NSF"),
              "!! 没识别成 NSF，实际 fmt=%r" % info.fmt)
        check(int(info.n_tracks or 0) > 0, "!! 轨数为 0")
        check(float(plan.secs or 0.0) > 0, "!! 计划时长为 0")
        print("    -> 识别为 %s，%d 轨，计划时长 %.1fs"
              % (info.fmt, info.n_tracks, plan.secs))

        # 5) 转 WAV —— 走主窗口同一条管线：
        #    spc_wav.convert(files, out_dir, "chip", loader=chip_decode.load_chip_paths)
        # 这个包有 210 轨，全转会非常慢；验收只需要证明"能转出非静音音频"，
        # 所以只取前 3 轨（path\first\count 协议，GUI 的 _plan_runs 同款）。
        wav_dir = os.path.join(work, "wav")
        os.makedirs(wav_dir, exist_ok=True)
        item = "%s\x1f0\x1f3" % nsf
        print("\n[5] spc_wav.convert(policy='chip', loader=load_chip_paths, "
              "前 3 轨) ...")
        logs: List[str] = []
        sub = spc_wav.convert(
            [item], wav_dir, "chip",
            on_log=logs.append,
            chip_secs=20.0,
            loader=chip_decode.load_chip_paths,
        )
        for line in logs[-12:]:
            print("    | %s" % line)
        produced_wavs = sorted(
            os.path.join(wav_dir, f) for f in os.listdir(wav_dir)
            if f.lower().endswith(".wav"))
        check(produced_wavs, "!! 没产出 WAV（out_dir=%s）" % wav_dir)

        out_wav = produced_wavs[0]
        wav_size = os.path.getsize(out_wav)
        peak = _wav_peak(out_wav)
        print("    WAV: %s (%s), 峰值 %d"
              % (os.path.basename(out_wav), chip_formats.human_size(wav_size),
                 peak))
        check(wav_size > 44, "!! WAV 是空的")
        check(peak > 300, "!! 峰值 %d <= 300，疑似静音" % peak)
        print("\n✅ 端到端通过：下载 %d 字节 -> 解压 %d 个文件 -> "
              "识别 %s(%d 轨) -> WAV 峰值 %d"
              % (actual_size, len(ex[0]["files"]), info.fmt, info.n_tracks,
                 peak))
        del sub
    finally:
        keep = os.environ.get("E2E_KEEP", "")
        if keep:
            print("\n（E2E_KEEP 已设置，产物保留在 %s）" % work)
        else:
            shutil.rmtree(work, ignore_errors=True)


def _pick_live_pack():
    """从 nsf.joshw.info 抓一个真实的 .7z 条目。

    返回 ``(显示名, url, size)``。挑一个 100KB~8MB 的包：太小撑不起
    "解压出真文件"的验证，太大又慢。joshw_source.scan_platform 走的是
    Apache autoindex 的分目录（0-9/ a/ b/ …），所以必须用它而不能自己
    正则首页。
    """
    import urllib.parse

    import joshw_source

    entries = joshw_source.scan_platform("nsf", progress=None)
    fallback = None
    for entry in entries:
        url = str(entry.get("url") or "")
        name = str(entry.get("name") or "")
        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if not url.lower().endswith(".7z") or not name:
            continue
        stem = os.path.splitext(urllib.parse.unquote(name))[0]
        if 100 * 1024 <= size <= 8 * 1024 * 1024:
            return stem, url, size
        if fallback is None and 0 < size < 30 * 1024 * 1024:
            fallback = (stem, url, size)
    return fallback


def _wav_peak(path: str) -> int:
    """读 WAV 算峰值（判断是不是静音）。"""
    import wave

    with wave.open(path, "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        width = wf.getsampwidth()
        peak = 0
        step = max(1, len(frames) // 400000) * width
        for offset in range(0, len(frames) - width + 1, step):
            value = int.from_bytes(frames[offset:offset + width],
                                   "little", signed=True)
            peak = max(peak, abs(value))
        return peak


def main() -> int:
    parser = argparse.ArgumentParser(description="joshw_dl 解压链路验收")
    parser.add_argument("--json", action="store_true", help="输出 JSON（CI 用）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="打印每条用例的细节")
    parser.add_argument("--e2e", action="store_true",
                        help="额外跑真实 joshw 联网端到端验收")
    args = parser.parse_args()

    if args.e2e and not args.json:
        failed = run_all(args.verbose)
        if failed:
            print("\n离线用例有失败，仍继续跑端到端 ...")
        run_e2e()
        return 1 if failed else 0

    if args.e2e and args.json:
        run_e2e()

    failed = run_all(args.verbose)
    if args.json:
        print(json.dumps({
            "passed": sum(1 for r in REG.results if r["ok"]),
            "failed": failed,
            "total": len(REG.results),
            "tar": _tar(),
            "results": REG.results,
        }, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
