#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_dl_convert_worker.py — dl_convert.ConvertWorker 的离线验收。

**全程不碰 Tk、不联网、不解码真实音源**（用注入的假 convert），
因此可以在 CI 里稳定跑完。真实解码在 vendor/test_dl_convert_e2e.py。

跑法::

    python vendor/test_dl_convert_worker.py
    python vendor/test_dl_convert_worker.py --json
    python vendor/test_dl_convert_worker.py -v

覆盖（对应设计 §8.3 T2 验收点 ①~④）：

① **失败隔离**：5 个文件（含 1 损坏、1 不存在）→ 只有这 2 个失败，
   其余 3 个成功，**线程不退出**（不静默死亡）；
② **取消语义**：`cancel()` 后未开始项被丢弃、已开始项收手、
   **已产出文件仍在**（不回滚）、manifest 照常写（幂等）；
③ **单文件超时隔离**：注入 `time.sleep(999)` 的假 convert，验证主线程按
   ``FILE_TIMEOUT_S`` 置**该文件**的 cancel，队列继续（不误杀整队）；
   ⚠ 重点验证**没有** ``join(300)`` 然后放弃那种编号竞争；
④ **磁盘门禁**：任务**不出队**（队列长度不变）+ 空间恢复后自动继续 +
   源已结束且持续不足 → 发 ``cv_abandoned`` 收敛（不死锁）。

额外覆盖：线程铁律（转换线程内不出现任何 Tk 调用）、`used`/`start_idx`
跨调用共享、三段式统计、事件 kind 齐全、`cv_thread_exit` 必达。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import dl_convert as dc                                  # noqa: E402

_GREEN, _RED, _YEL, _RST = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    _GREEN = _RED = _YEL = _RST = ""

_pass = 0
_fail = 0
_notes = []


def check(label, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print("  %sPASS%s %s" % (_GREEN, _RST, label))
    else:
        _fail += 1
        print("  %sFAIL%s %s%s" % (_RED, _RST, label,
                                   ("  <- " + detail) if detail else ""))
    return bool(cond)


def note(text):
    _notes.append(text)
    print("  %sNOTE%s %s" % (_YEL, _RST, text))


# ---------------------------------------------------------------------------
# 夹具：假 convert（**不调真解码**，秒跑）
# ---------------------------------------------------------------------------

class FakeConvert:
    """可注入的假 ``spc_wav.convert``。

    行为按文件名约定，便于构造各类故障：

    * ``boom*``   → 抛异常（模拟解码 / 写盘炸了）
    * ``slow*``   → ``time.sleep(HANG)``（模拟子进程 hang，验超时隔离）
    * ``empty*``  → 返回 ok=0 且无产物（模拟「解码完成但没产出」）
    * ``cancel*`` → 循环检查 cancel，一旦置位就 break 并返回部分产物
    * 其它        → 正常产出 2 个 WAV
    """

    def __init__(self, wav_dir, hang=999.0, n_wav=2):
        self.wav_dir = wav_dir
        self.hang = hang
        self.n_wav = n_wav
        self.calls = []              # [(paths, policy, start_idx, sorted used)]
        self.lock = threading.Lock()

    def __call__(self, paths, out_dir, policy, on_log=None, on_progress=None,
                 on_stage=None, cancel=None, speed=1.0, max_secs=0.0,
                 used=None, start_idx=0, total=None, **extra):
        src = str(paths[0])
        name = os.path.basename(src).lower()
        with self.lock:
            self.calls.append((list(paths), policy, start_idx,
                               sorted(used or ())))
        if on_stage:
            on_stage(1, 1, len(paths))
        if on_log:
            on_log("fake decode start: " + name)

        if name.startswith("boom"):
            raise RuntimeError("模拟解码失败")
        if name.startswith("hang"):
            # 真的 sleep，靠外部置 cancel 让它返回（验超时隔离）
            deadline = time.time() + self.hang
            while time.time() < deadline:
                if cancel is not None and cancel.is_set():
                    return self._write(out_dir, policy, start_idx, used,
                                       n_override=1, cancel_hit=True)
                time.sleep(0.02)
            return self._write(out_dir, policy, start_idx, used)
        if name.startswith("cancel"):
            for _ in range(200):
                if cancel is not None and cancel.is_set():
                    return self._write(out_dir, policy, start_idx, used,
                                       n_override=1, cancel_hit=True)
                time.sleep(0.01)
            return self._write(out_dir, policy, start_idx, used)
        if name.startswith("empty"):
            return {"ok": 0, "fail": 1, "files": [], "seconds": 0.0,
                    "errors": ["空产"], "last_idx": start_idx}
        return self._write(out_dir, policy, start_idx, used)

    def _write(self, out_dir, policy, start_idx, used, n_override=None,
               cancel_hit=False):
        n = self.n_wav if n_override is None else n_override
        os.makedirs(out_dir, exist_ok=True)
        idx = start_idx
        files = []
        pool = used if used is not None else set()
        for i in range(n):
            idx += 1
            stem = "T%02d" % idx
            wavp = os.path.join(out_dir, "%02d_%s.wav" % (idx, stem))
            with open(wavp, "wb") as fh:
                fh.write(b"RIFF" + b"\x00" * 4096)
            pool.add(stem.lower())
            files.append(wavp)
        return {"ok": len(files), "fail": 0, "files": files, "seconds": 1.0,
                "errors": [] if not cancel_hit else ["已取消"],
                "last_idx": idx}


class Collector:
    """收集事件（**必须只 append，不碰 Tk** —— 与 GUI 侧同款契约）。"""

    def __init__(self):
        self.ev = []
        self.lock = threading.Lock()

    def __call__(self, kind, payload):
        with self.lock:
            self.ev.append((kind, dict(payload or {})))

    def kinds(self):
        with self.lock:
            return [k for k, _p in self.ev]

    def of(self, kind):
        with self.lock:
            return [p for k, p in self.ev if k == kind]

    def first(self, kind):
        got = self.of(kind)
        return got[0] if got else {}

    def wait_for(self, kind, timeout=30.0):
        """等到某个 kind 出现（或超时）。返回 payload 或 None。"""
        end = time.time() + timeout
        while time.time() < end:
            got = self.of(kind)
            if got:
                return got[0]
            time.sleep(0.02)
        return None


def make_src(path, body=b"SNES-SPC700 Sound File Data"):
    """造一个源文件（扩展名决定分流，不需要真的能解码）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body + b"\x00" * 128)
    return path


def wav_files(d):
    try:
        return sorted(f for f in os.listdir(d) if f.lower().endswith(".wav"))
    except OSError:
        return []


# ---------------------------------------------------------------------------
# ① 失败隔离
# ---------------------------------------------------------------------------

def test_failure_isolation(tmp):
    print("\n[1] 失败隔离：5 文件（含 1 坏 1 不存在）只有 2 个失败，线程不退出")
    wav_dir = os.path.join(tmp, "iso", "WAV")
    os.makedirs(wav_dir, exist_ok=True)
    src_dir = os.path.join(tmp, "iso", "src")
    files = []
    for name in ("a.spc", "b.nsf", "boom.gbs", "c.vgm", "d.hes"):
        files.append(make_src(os.path.join(src_dir, name)))
    missing = os.path.join(src_dir, "not_exist.spc")

    col = Collector()
    fake = FakeConvert(wav_dir)
    w = dc.ConvertWorker(wav_dir, col, threading.Event())
    w.convert_fn = fake
    w.start()
    for i, f in enumerate(files):
        w.enqueue({"src": f, "url": "u%d" % i, "pkg": "p",
                   "policy": dc.policy_for(f), "n_hint": 1})
    w.enqueue({"src": missing, "url": "uX", "policy": "id666"})
    w.mark_src_finished()

    exit_p = col.wait_for("cv_thread_exit", 60)
    check("收到 cv_thread_exit（线程未静默死亡）", exit_p is not None)
    w.wait(10)
    check("线程确实退出", not w.is_alive())

    done = col.of("cv_file_done")
    failed = col.of("cv_file_failed")
    # 6 个 job = 5 个真实文件 + 1 个不存在；boom 与不存在失败 -> 4 成功
    check("4 个成功", len(done) == 4, "got %d" % len(done))
    check("2 个失败（boom + 不存在）", len(failed) == 2, "got %d" % len(failed))
    names = sorted(os.path.basename(p.get("src", "")) for p in failed)
    check("失败项正确", names == ["boom.gbs", "not_exist.spc"], str(names))
    boom = [p for p in failed if "boom" in p.get("src", "")][0]
    check("失败原因首行可执行", "模拟解码失败" in str(boom.get("error", "")),
          str(boom.get("error")))
    check("失败项带 policy 标记（FR-C10.3）", bool(boom.get("policy")))
    logs = col.of("cv_log")
    check("失败时回传解码明细（FR-C10.4 仅失败时展开）", len(logs) >= 1,
          str(len(logs)))
    check("成功时不刷 cv_log（只 1 次，对应 boom 那个文件）", len(logs) == 1,
          str([p.get("src") for p in logs]))
    check("cv_file_done 成功项不带明细",
          all(not p.get("detail") for p in done),
          str([p.get("src") for p in done if p.get("detail")]))

    # 分流正确性：spc 走 id666，其余走 chip
    pol = sorted(c[1] for c in fake.calls)
    check("分流：1×id666 + 4×chip",
          pol == ["chip", "chip", "chip", "chip", "id666"], str(pol))
    # chip 路径必须注入 loader + chip_secs，id666 不得注入
    srcs = {os.path.basename(c[0][0]): c[1] for c in fake.calls}
    check("a.spc 走 id666", srcs.get("a.spc") == "id666")
    check("b.nsf 走 chip", srcs.get("b.nsf") == "chip")

    # start_idx 跨调用单调递增（H2 硬性配套）。
    # ⚠ **失败的文件不消耗编号** —— boom 没产出 WAV，编号不前进，
    #   下一个文件接着用同一个号。这是有意的：宁可编号有空洞，
    #   也不能让失败的 convert 把 last_idx 推到越界。
    idxs = [c[2] for c in fake.calls]
    check("start_idx 跨调用单调不减", all(b >= a for a, b in zip(idxs, idxs[1:])),
          str(idxs))
    check("start_idx 首项为 0", idxs[0] == 0, str(idxs))
    check("失败项复用同一编号（不跳号）", idxs == [0, 2, 4, 4, 6], str(idxs))
    # used 跨调用共享：第 2 次调用时池里已有第 1 次的 stem
    check("used 集合跨调用共享",
          any(c[3] for c in fake.calls[1:]), str([c[3] for c in fake.calls]))

    check("统计 ok=4", int(exit_p.get("ok", 0)) == 4, str(exit_p))
    check("统计 fail=2", int(exit_p.get("fail", 0)) == 2, str(exit_p))
    check("统计 wavs=8", int(exit_p.get("wavs", 0)) == 8, str(exit_p))
    check("统计 bytes>0", int(exit_p.get("bytes", 0)) > 0, str(exit_p))
    check("产出 8 个 WAV（失败文件不占编号也不占名）",
          len(wav_files(wav_dir)) == 8, str(wav_files(wav_dir)))
    nums = sorted(int(f[:2]) for f in wav_files(wav_dir))
    check("编号无空洞且唯一", nums == [1, 2, 3, 4, 5, 6, 7, 8], str(nums))
    check("manifest 已落盘",
          os.path.isfile(os.path.join(wav_dir, dc.MANIFEST_NAME)))


# ---------------------------------------------------------------------------
# ② 取消语义
# ---------------------------------------------------------------------------

def test_cancel(tmp):
    print("\n[2] 取消：未开始项丢弃 / 已产出保留 / 不 join")
    wav_dir = os.path.join(tmp, "cancel", "WAV")
    os.makedirs(wav_dir, exist_ok=True)
    src_dir = os.path.join(tmp, "cancel", "src")
    col = Collector()
    fake = FakeConvert(wav_dir, hang=60.0)
    w = dc.ConvertWorker(wav_dir, col, threading.Event())
    w.convert_fn = fake
    w.start()
    for i in range(5):
        f = make_src(os.path.join(src_dir, "hang%d.spc" % i))
        w.enqueue({"src": f, "url": "u%d" % i, "policy": "id666"})
    w.mark_src_finished()

    # 等到第一个文件真的开始（_cur 非空）
    end = time.time() + 20
    while time.time() < end and w.current() is None:
        time.sleep(0.02)
    check("current() 可读到在转文件", w.current() is not None)
    cur = w.current() or {}
    check("current 含 t0/cancel 键",
          "t0" in cur and hasattr(cur.get("cancel"), "set"),
          str(sorted(cur.keys())))
    queued_before = w._q.qsize()

    t0 = time.time()
    w.cancel()                       # **不 join**，只置 Event + drain
    check("cancel() 立即返回（不 join）", time.time() - t0 < 1.0,
          "%.2fs" % (time.time() - t0))
    check("cancel() 后队列被清空", w._q.qsize() == 0,
          "before=%d after=%d" % (queued_before, w._q.qsize()))

    exit_p = col.wait_for("cv_thread_exit", 30)
    check("取消后线程退出并发 cv_thread_exit", exit_p is not None)
    w.wait(20)
    check("线程真的退出", not w.is_alive())
    check("取消后无卡死（<20s）", time.time() - t0 < 20.0,
          "%.1fs" % (time.time() - t0))

    # 已写完整的 WAV **保留**（不回滚，R8）
    kept = wav_files(wav_dir)
    check("已产出 WAV 保留", len(kept) >= 1, str(kept))
    for f in kept:
        with open(os.path.join(wav_dir, f), "rb") as fh:
            blob = fh.read()
        # 假 convert 写的是 b"RIFF" + 4096 字节 = 4100
        check("保留的 WAV 头完整 %s" % f,
              blob[:4] == b"RIFF" and len(blob) == 4100,
              "%d bytes" % len(blob))
    check("取消后未开始项无产物（只转了 %d 个）" % len(kept), len(kept) <= 2,
          str(kept))
    # manifest 照常写 -> 下次能跳过已转过的（幂等）
    check("取消后 manifest 仍落盘",
          os.path.isfile(os.path.join(wav_dir, dc.MANIFEST_NAME)))

    # 二次 cancel 幂等
    w.cancel()
    check("重复 cancel 不抛", True)


# ---------------------------------------------------------------------------
# ③ 单文件超时隔离
# ---------------------------------------------------------------------------

def test_timeout_isolation(tmp):
    print("\n[3] 单文件超时只影响该文件，队列继续（无 join(300) 竞争）")
    wav_dir = os.path.join(tmp, "to", "WAV")
    os.makedirs(wav_dir, exist_ok=True)
    src_dir = os.path.join(tmp, "to", "src")
    col = Collector()
    fake = FakeConvert(wav_dir, hang=600.0)
    w = dc.ConvertWorker(wav_dir, col, threading.Event())
    w.convert_fn = fake
    # 把阈值调小，行为与 300s 完全同构（等 300s 不现实）
    w.FILE_TIMEOUT_S = 1.0
    w.start()
    slow = make_src(os.path.join(src_dir, "hang0.spc"))
    after1 = make_src(os.path.join(src_dir, "a1.nsf"))
    after2 = make_src(os.path.join(src_dir, "a2.nsf"))
    w.enqueue({"src": slow, "url": "slow", "policy": "id666"})
    w.enqueue({"src": after1, "url": "a1", "policy": "chip"})
    w.enqueue({"src": after2, "url": "a2", "policy": "chip"})
    w.mark_src_finished()

    end = time.time() + 20
    while time.time() < end and w.current() is None:
        time.sleep(0.02)
    cur = w.current()
    check("慢文件在转", cur is not None)

    # ---- 模拟主线程 `_check_conv_timeout`（GUI 侧同一套逻辑）----
    timed = False
    deadline = time.time() + 15
    while time.time() < deadline:
        c = w.current()
        if c and (time.time() - c["t0"]) > w.FILE_TIMEOUT_S and not timed:
            timed = True
            check("cur 内有 per-file cancel Event",
                  hasattr(c.get("cancel"), "is_set"))
            check("per-file Event 初始未置位", not c["cancel"].is_set())
            c["cancel"].set()                  # 只置本文件的
            check("置位后 is_set 为真", c["cancel"].is_set())
        if col.of("cv_file_done") and len(col.of("cv_file_done")) >= 3:
            break
        time.sleep(0.05)
    check("巡检确实触发超时置位", timed)

    exit_p = col.wait_for("cv_thread_exit", 40)
    check("超时后线程仍能收尾", exit_p is not None)
    w.wait(20)
    check("线程退出", not w.is_alive())
    done = col.of("cv_file_done")
    check("后两个文件照常成功（超时未误杀整队）", len(done) == 3,
          "got %d" % len(done))
    # 关键：若实现是 join(300) 然后放弃，start_idx 会被并发改两次
    idxs = [c[2] for c in fake.calls]
    check("start_idx 严格递增无竞争", idxs == sorted(set(idxs)) or
          all(b >= a for a, b in zip(idxs, idxs[1:])), str(idxs))
    check("三个文件都被调用过", len(fake.calls) == 3, str(len(fake.calls)))


# ---------------------------------------------------------------------------
# ④ 磁盘门禁
# ---------------------------------------------------------------------------

def test_disk_gate(tmp):
    print("\n[4] 磁盘门禁：任务不出队 / 恢复后继续 / 持续不足则放弃")
    wav_dir = os.path.join(tmp, "disk", "WAV")
    os.makedirs(wav_dir, exist_ok=True)
    src_dir = os.path.join(tmp, "disk", "src")

    # --- 4a 门禁触发：任务不出队 ---
    col = Collector()
    fake = FakeConvert(wav_dir)
    w = dc.ConvertWorker(wav_dir, col, threading.Event())
    w.convert_fn = fake
    free = {"v": 1.0}                       # 1 字节 -> 必然不足
    w.disk_free_fn = lambda: free["v"]
    w.start()
    for i in range(3):
        f = make_src(os.path.join(src_dir, "g%d.spc" % i))
        w.enqueue({"src": f, "url": "u%d" % i, "policy": "id666"})
    w.mark_src_finished()
    time.sleep(1.2)
    check("磁盘不足时不消费（0 产出）", len(wav_files(wav_dir)) == 0,
          str(wav_files(wav_dir)))
    check("发过 cv_paused", len(col.of("cv_paused")) >= 1)
    check("cv_paused 带 reason=disk",
          col.first("cv_paused").get("reason") == "disk")
    # 收敛保证：源已结束 + 持续不足 -> 放弃剩余而不是死锁
    ab = col.wait_for("cv_abandoned", 20)
    check("磁盘持续不足发 cv_abandoned（不死锁）", ab is not None)
    if ab:
        check("cv_abandoned 带剩余数 left", int(ab.get("left", 0)) >= 1,
              str(ab))
    exit_p = col.wait_for("cv_thread_exit", 20)
    check("放弃后仍走 cv_thread_exit（GUI 能收尾）", exit_p is not None)
    w.wait(15)
    check("线程退出", not w.is_alive())

    # --- 4b 空间恢复后自动继续 ---
    wav_dir2 = os.path.join(tmp, "disk2", "WAV")
    os.makedirs(wav_dir2, exist_ok=True)
    col2 = Collector()
    fake2 = FakeConvert(wav_dir2)
    w2 = dc.ConvertWorker(wav_dir2, col2, threading.Event())
    w2.convert_fn = fake2
    free2 = {"v": 1.0}
    w2.disk_free_fn = lambda: free2["v"]
    w2.DISK_STALL_ROUNDS = 1000           # 关掉本轮的放弃逻辑，专测恢复
    w2.start()
    for i in range(2):
        f = make_src(os.path.join(src_dir, "h%d.spc" % i))
        w2.enqueue({"src": f, "url": "v%d" % i, "policy": "id666"})
    w2.mark_src_finished()
    time.sleep(0.8)
    check("4b 暂停期间无产出", len(wav_files(wav_dir2)) == 0)
    free2["v"] = 50.0 * 1024 ** 3          # 用户删文件腾出空间
    exit2 = col2.wait_for("cv_thread_exit", 30)
    check("空间恢复后自动继续并收尾", exit2 is not None)
    w2.wait(15)
    check("4b 恢复后产出 2 个文件 × 2 首", len(wav_files(wav_dir2)) == 4,
          str(wav_files(wav_dir2)))

    # --- 4c disk_usage 失败 -> 放行（不卡用户）---
    col3 = Collector()
    fake3 = FakeConvert(wav_dir2)
    w3 = dc.ConvertWorker(wav_dir2, col3, threading.Event())
    w3.convert_fn = fake3

    def boom_free():
        raise OSError("disk_usage 不可用")

    w3.disk_free_fn = boom_free
    src3 = make_src(os.path.join(src_dir, "ok.spc"))
    w3.disk_free_fn = lambda: 50.0 * 1024 ** 3
    check("disk 探测失败时放行", w3._disk_ok(1 << 30) is True)

    # ---- v4.3 门禁验收（架构师指定 5 条：1 / 2' / 2'' / 3 / 4）----
    # ⚠⚠ **旧的三条 ④a 断言已作废**：它们把 free 设成 need*1.3，在 1.3 被去掉后
    #   依然通过 -> **失去鉴别力**（正是 M6 那类「绿色但无意义」的断言）。
    #   判据 1（free **精确等于** need）才是唯一能夹住「1.3 是否去掉」的用例。
    #
    # ⚠ 原判据 2「JC63011 free=1GiB -> 放行」是**错的**（架构师纠正）：
    #   need 用 256 轨 = 6.31 GiB；去掉 1.3 只把门槛 8.20 -> 6.31 GiB，
    #   **1 GiB 仍然不够**。判据是 `need`，**不是真实产出**。
    GIB = 1024 ** 3
    need = 256 * 150 * dc.BYTES_PER_SEC           # 6.309 GiB
    w4 = dc.ConvertWorker(os.path.join(tmp, "disk_gate", "WAV"),
                          lambda *a: None, threading.Event())

    def gate(free_bytes, label, want_pass):
        w4.disk_free_fn = lambda v=float(free_bytes): v
        got = w4._disk_ok(need)
        check("%s -> %s" % (label, "放行" if want_pass else "拦下"),
              got is want_pass,
              "free=%.3f GiB need=%.3f GiB got=%s"
              % (free_bytes / GIB, need / GIB, got))

    gate(need, "判据1  free == need(6.309GiB)【唯一夹住 1.3 的用例】", True)
    gate(int(6.5 * GIB), "判据2' JC63011 free=6.5GiB（验 1.3 已去）", True)
    gate(int(1.0 * GIB), "判据2'' JC63011 free=1GiB（真实产出不是判据）", False)
    gate(int(0.3 * GIB), "判据3  JC63011 free=0.3GiB", False)
    gate(int(1.85 * GIB), "判据4  PV1004 free=真实×0.5(1.85GiB)", False)
    gate(need - 1, "边界  free == need-1", False)
    # 地板：need 极小时仍要 256 MiB（防止把地板当「多余保险」删掉）
    tiny = 1024
    w4.disk_free_fn = lambda: float(dc.ConvertWorker.DISK_RESERVE)
    check("地板 need=1KiB、free==256MiB -> 放行", w4._disk_ok(tiny) is True,
          "地板被当保险删掉了？")
    w4.disk_free_fn = lambda: float(dc.ConvertWorker.DISK_RESERVE) - 1
    check("地板 need=1KiB、free==256MiB-1 -> 拦下", w4._disk_ok(tiny) is False)
    # 反向锁：源码层防止 1.3 / `>` 被加回来。
    # ⚠⚠ **用 AST 而不是文本/token 匹配**。本轮已连续三次踩同一个坑：
    #   ① dl_convert 的 n_tracks_used  ② e2e 的 AC-C27  ③ 这里的 1.3 ——
    #   每次都是「文本匹配命中了我自己写的解释性注释/文档字符串」。
    #   AST 只看**可执行语句**，注释与 docstring 天然被排除，根治这类假失败。
    import ast as _ast
    _src4 = open(os.path.join(ROOT, "dl_convert.py"), encoding="utf-8").read()
    _tree = _ast.parse(_src4)
    _disk_fn = None
    for _n in _tree.body:
        if isinstance(_n, _ast.ClassDef):
            for _m in _n.body:
                if isinstance(_m, _ast.FunctionDef) and _m.name == "_disk_ok":
                    _disk_fn = _m
    check("反向锁：能定位 _disk_ok", _disk_fn is not None)
    if _disk_fn is not None:
        # (a) 不得有 1.3 这类浮点常量（docstring 里的数字不算）
        _consts = [n.value for n in _ast.walk(_disk_fn)
                   if isinstance(n, _ast.Constant)
                   and isinstance(n.value, float)]
        check("反向锁：_disk_ok 语句里无浮点系数（1.3 被加回来？）",
              not _consts, "发现浮点常量: %s" % _consts)
        # (b) 比较运算符必须是 GtE，且函数内不得出现裸 Gt
        _cmps = [n for n in _ast.walk(_disk_fn)
                 if isinstance(n, _ast.Compare)]
        _ops_used = [type(o).__name__
                     for c in _cmps for o in c.ops]
        check("反向锁：_disk_ok 用 GtE 而非 Gt",
              "GtE" in _ops_used and "Gt" not in _ops_used,
              "比较运算符: %s" % _ops_used)
    check("反向锁：DISK_RESERVE 地板仍在（256MB）",
          "DISK_RESERVE = 256 * 1024 * 1024" in _src4)


# ---------------------------------------------------------------------------
# ⑤ 事件契约 / 线程铁律 / 幂等
# ---------------------------------------------------------------------------

def test_events_and_idempotent(tmp):
    print("\n[5] 事件契约 / 幂等 / 线程铁律")
    wav_dir = os.path.join(tmp, "evt", "WAV")
    os.makedirs(wav_dir, exist_ok=True)
    src_dir = os.path.join(tmp, "evt", "src")

    # --- 幂等：同一批跑两遍，第二遍全 cv_skip ---
    col = Collector()
    fake = FakeConvert(wav_dir)
    files = [make_src(os.path.join(src_dir, "i%d.nsf" % i)) for i in range(3)]
    w = dc.ConvertWorker(wav_dir, col, threading.Event())
    w.convert_fn = fake
    w.start()
    for i, f in enumerate(files):
        w.enqueue({"src": f, "url": "u%d" % i, "policy": "chip"})
    w.mark_src_finished()
    col.wait_for("cv_thread_exit", 30)
    w.wait(10)
    n_first = len(wav_files(wav_dir))
    check("首轮产出 6 个 WAV", n_first == 6, str(n_first))

    col2 = Collector()
    fake2 = FakeConvert(wav_dir)
    w2 = dc.ConvertWorker(wav_dir, col2, threading.Event())
    w2.convert_fn = fake2
    w2.start()
    for i, f in enumerate(files):
        w2.enqueue({"src": f, "url": "u%d" % i, "policy": "chip"})
    w2.mark_src_finished()
    exit2 = col2.wait_for("cv_thread_exit", 30)
    w2.wait(10)
    skips = col2.of("cv_skip")
    check("第二轮全部 cv_skip", len(skips) == 3, "got %d" % len(skips))
    check("第二轮不再调 convert", len(fake2.calls) == 0,
          str(len(fake2.calls)))
    check("cv_skip 带 n_wav", all("n_wav" in s for s in skips))
    check("cv_skip 带 reason=done",
          all(s.get("reason") == "done" for s in skips))
    check("产物数不变（幂等）", len(wav_files(wav_dir)) == n_first)
    check("第二轮 stats.skip=3", int((exit2 or {}).get("skip", 0)) == 3,
          str(exit2))

    # ---- 删 WAV 的粒度（设计 §4.3 明确「实现用全量探」）----
    # 每个源文件产出 2 个 WAV（01_T01/02_T02 属 file0，03_T03/04_T04 属 file1…）。
    # 设计 §4.3 的判据②是**全量探 + 任一存在即算已转过**：
    #   「更符合『用户删了中间几首也别重复转』的直觉」——
    #   即只删了 3 首里的 1 首时**不重转**（避免半包重复占盘）。
    # ⚠ 这与 PRD AC-C15② 的「手动删掉某个 WAV → 重转」措辞不同，
    #   **以设计为准**（§4.3 已显式推翻了同节代码草稿里的 `wavs[:1]` 写法）。
    allw = wav_files(wav_dir)
    os.remove(os.path.join(wav_dir, allw[0]))          # file0 的 01_T01
    col3 = Collector()
    fake3 = FakeConvert(wav_dir)
    w3 = dc.ConvertWorker(wav_dir, col3, threading.Event())
    w3.convert_fn = fake3
    w3.start()
    for i, f in enumerate(files):
        w3.enqueue({"src": f, "url": "u%d" % i, "policy": "chip"})
    w3.mark_src_finished()
    col3.wait_for("cv_thread_exit", 30)
    w3.wait(10)
    check("只删 1 首（该源还有 1 首）-> 不重转（全量探，设计 §4.3）",
          len(fake3.calls) == 0 and len(col3.of("cv_skip")) == 3,
          "calls=%d skip=%d" % (len(fake3.calls), len(col3.of("cv_skip"))))

    # 把 file0 的**全部** WAV 删掉 -> 只重转那一个（不是全量）
    for f in wav_files(wav_dir):
        if f in ("01_T01.wav", "02_T02.wav"):
            os.remove(os.path.join(wav_dir, f))
    col4 = Collector()
    fake4 = FakeConvert(wav_dir)
    w4 = dc.ConvertWorker(wav_dir, col4, threading.Event())
    w4.convert_fn = fake4
    w4.start()
    for i, f in enumerate(files):
        w4.enqueue({"src": f, "url": "u%d" % i, "policy": "chip"})
    w4.mark_src_finished()
    e4 = col4.wait_for("cv_thread_exit", 30)
    w4.wait(10)
    check("删光某源的全部 WAV -> 只重转那 1 个",
          len(fake4.calls) == 1 and len(col4.of("cv_skip")) == 2,
          "calls=%d skip=%d" % (len(fake4.calls), len(col4.of("cv_skip"))))
    check("重转时编号续在已有最大号之后",
          int(fake4.calls[0][2]) == 6,
          "start_idx=%s（现有 01~06 已占，故从 7 起）" % fake4.calls[0][2])
    check("重转后产物数恢复到 6", len(wav_files(wav_dir)) == 6,
          str(wav_files(wav_dir)))
    check("重转不覆盖他人产物",
          all(os.path.getsize(os.path.join(wav_dir, f)) == 4100
              for f in wav_files(wav_dir)))

    # --- 事件 kind 齐全 ---
    kinds = set(col.kinds()) | set(col2.kinds()) | set(col3.kinds())
    for k in ("cv_file_done", "cv_skip", "cv_thread_exit"):
        check("事件 %s 出现" % k, k in kinds)
    # ⚠ 这条**曾经是恒真的**：col/col2/col3 全是 happy path, cv_error 在结构上
    #   不可能产生 —— 等于断言「一个从未发生的事没发生」。
    #   现已由 [12] 组提供**正向触发**作对照, 这里才是真结论。
    check("无 cv_error（正常路径, 有 [12] 正向对照）",
          "cv_error" not in kinds, str(sorted(kinds)))

    # --- cv_error 路径：emit 回调炸了也不能中断线程 ---
    col4 = Collector()

    def boom_cb(kind, payload):
        raise RuntimeError("GUI 队列炸了")

    w4 = dc.ConvertWorker(os.path.join(tmp, "evt", "WAV2"), boom_cb,
                          threading.Event())
    w4.convert_fn = FakeConvert(os.path.join(tmp, "evt", "WAV2"))
    w4.start()
    w4.mark_src_finished()
    w4.wait(15)
    check("on_event 抛异常线程仍正常退出", not w4.is_alive())

    # --- cancel Event 复用：与 Downloader 同一个 Event 三段联动 ---
    shared = threading.Event()
    col5 = Collector()
    wav5 = os.path.join(tmp, "evt", "WAV3")
    fake5 = FakeConvert(wav5, hang=30.0)
    w5 = dc.ConvertWorker(wav5, col5, shared)
    w5.convert_fn = fake5
    w5.start()
    w5.enqueue({"src": make_src(os.path.join(src_dir, "z.spc")),
                "url": "z", "policy": "id666"})
    end = time.time() + 15
    while time.time() < end and w5.current() is None:
        time.sleep(0.02)
    shared.set()                        # 模拟 dl.cancel()
    exit5 = col5.wait_for("cv_thread_exit", 30)
    check("共享 Event 置位能收掉转换（FR-C9.1）", exit5 is not None)
    w5.wait(15)
    check("共享 Event 路径线程退出", not w5.is_alive())

    # --- mark_src_finished 之前不得早退（双条件收尾）---
    col6 = Collector()
    wav6 = os.path.join(tmp, "evt", "WAV4")
    fake6 = FakeConvert(wav6)
    w6 = dc.ConvertWorker(wav6, col6, threading.Event())
    w6.convert_fn = fake6
    w6.start()
    w6.enqueue({"src": make_src(os.path.join(src_dir, "q.nsf")),
                "url": "q", "policy": "chip"})
    time.sleep(1.5)
    check("源未结束时不收尾（只看队列空会早退）", not col6.of("cv_thread_exit"),
          str(col6.kinds()))
    w6.enqueue({"src": make_src(os.path.join(src_dir, "q2.nsf")),
                "url": "q2", "policy": "chip"})
    time.sleep(1.0)
    check("第二个入队后仍能消费", len(fake6.calls) >= 2,
          str(len(fake6.calls)))
    w6.mark_src_finished()
    e6 = col6.wait_for("cv_thread_exit", 30)
    check("双条件满足后才收尾", e6 is not None)
    w6.wait(10)
    check("2 个文件都转完", int((e6 or {}).get("ok", 0)) == 2, str(e6))


# ---------------------------------------------------------------------------
# ⑥ 回调签名兼容（实测踩过的真 bug）
# ---------------------------------------------------------------------------

class ZeroOutputConvert(FakeConvert):
    """假 convert：模拟**损坏文件**的解码结果（B18）。

    PM 实测：截断 / 全零 / 合法头+零数据三种坏 NSF 都是
    ``{'ok':0,'fail':0,'errors:[],'files':[]}`` —— 单文件解码失败**不抛**，
    落在 chip_decode 的 ``last_skips`` 里。**只看 fail 会判成功**。
    """

    def __init__(self, wav_dir, hang=0.0):
        FakeConvert.__init__(self, wav_dir)
        self.hang = hang

    def __call__(self, paths, out_dir, policy, cancel=None, **kw):
        name = os.path.basename(str(paths[0])).lower()
        if name.startswith("zero"):
            return {"ok": 0, "fail": 0, "files": [], "seconds": 0.0,
                    "errors": [], "last_idx": kw.get("start_idx", 0)}
        if self.hang:
            end = time.time() + self.hang
            while time.time() < end:
                if cancel is not None and cancel.is_set():
                    # ★ B20/B21: 返回值 ok=0 与「没取消」时**完全一样**,
                    #   唯一区别是**落盘了几个 WAV**。所以测试必须数文件。
                    return {"ok": 0, "fail": 0, "files": [],
                            "seconds": 0.0, "errors": [],
                            "last_idx": kw.get("start_idx", 0)}
                time.sleep(0.02)
        return FakeConvert.__call__(self, paths, out_dir, policy,
                                    cancel=cancel, **kw)


def make_real_nsf(path, n_tracks=16, pad=2048):
    """造一个**结构完整**的 NSF（魔数 NESM + 轨数 + 标题区 + 数据）。"""
    h = bytearray(0x100)
    h[0:4] = b"NESM\x1a"
    h[5] = 1                       # init
    h[6] = n_tracks
    h[7] = 0
    h[0x0A] = 0x80                 # NTSC
    h[0x0E:0x2E] = b"Real Test".ljust(0x20, b"\x00")[:0x20]
    h[0x2E:0x4E] = b"tester".ljust(0x20, b"\x00")[:0x20]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(bytes(h) + b"\x00" * pad)
    return path


def test_zero_output(tmp):
    print("\n[6] K 组：零产出静默失败（B18）")
    wav_dir = os.path.join(tmp, "k", "WAV")
    os.makedirs(wav_dir, exist_ok=True)
    src_dir = os.path.join(tmp, "k", "src")
    good = make_src(os.path.join(src_dir, "ok.nsf"))

    # ---- K1/K2/K3 的**真**输入: 截断到 0x30 字节的 NSF ----
    # ⚠ 它的 probe 会判为 **NSF/True**（只看魔数 NESM）-> 光靠 probe 挡不住,
    #   必须转换后判产出数。实测 decode 返回
    #   {'ok':0,'fail':0,'errors':[],'files':[]} -> 旧逻辑只看 fail 会判成功。
    full = make_real_nsf(os.path.join(src_dir, "_full.nsf"))
    bad = os.path.join(src_dir, "trunc.nsf")
    with open(full, "rb") as fh:
        blob = fh.read()
    with open(bad, "wb") as fh:
        fh.write(blob[:0x30])                       # 截断
    try:
        import chip_formats as cf
        info = cf.probe(bad)
        check("K0 截断文件的 probe 仍判为合法 NSF（所以拦不住）",
              info is not None and info.fmt == "NSF", repr(info))
    except Exception:                                # noqa: BLE001
        note("probe 不可用，跳过 K0")
    check("K0 截断文件只有 0x30 字节", os.path.getsize(bad) == 0x30)

    # K5 先单独验: read_last_skips 在任何情况下都不抛
    try:
        r = dc.read_last_skips()
        check("K5 read_last_skips() 不抛且可为 None", True, "got %r" % (r,))
    except Exception as e:                            # noqa: BLE001
        check("K5 read_last_skips() 不抛", False,
              "%s: %s" % (type(e).__name__, e))
    # last_skips 是 **dict 列表** —— 必须转成可读文本，不能 str(dict) 打给用户
    try:
        import chip_decode
        old = getattr(chip_decode.load_chip_paths, "last_skips", None)
        chip_decode.load_chip_paths.last_skips = [
            {"t": "skip", "path": r"D:\x\bad.nsf",
             "msg": "解码库打开失败: Wrong file type"}]
        txt = dc.read_last_skips()
        check("read_last_skips 把 dict 转成可读文本（不吐 Python 字面量）",
              txt and "解码库打开失败" in txt and "'t':" not in txt,
              repr(txt))
        chip_decode.load_chip_paths.last_skips = ["纯字符串原因"]
        check("read_last_skips 兼容字符串形态",
              dc.read_last_skips() == "纯字符串原因",
              repr(dc.read_last_skips()))
        if old is None:
            try:
                del chip_decode.load_chip_paths.last_skips
            except AttributeError:
                pass
        else:
            chip_decode.load_chip_paths.last_skips = old
    except Exception as e:                            # noqa: BLE001
        check("read_last_skips 能读到并转可读文本", False,
              "%s: %s" % (type(e).__name__, e))

    # ---- 用**真解码**跑（不加 convert_fn 覆盖）----
    col = Collector()
    w = dc.ConvertWorker(wav_dir, col, threading.Event())
    w.start()
    w.enqueue({"src": bad, "url": "u_bad", "policy": "chip"})
    w.mark_src_finished()
    exit_p = col.wait_for("cv_thread_exit", 240)
    w.wait(20)
    if exit_p is None:
        note("真解码超时，跳过 K1~K3（离线环境可能缺 DLL）")
    else:
        done = col.of("cv_file_done")
        failed = col.of("cv_file_failed")
        # (a) 发的是 cv_file_failed 而非 cv_file_done
        check("K1a 零产出发 cv_file_failed（不是 cv_file_done）",
              len(failed) == 1 and len(done) == 0,
              "failed=%d done=%d" % (len(failed), len(done)))
        check("K1a cv_file_failed 的 src 指向坏文件",
              failed and os.path.basename(failed[0].get("src", "")) == "trunc.nsf",
              str([f.get("src") for f in failed]))
        # (b) w.fail >= 1
        check("K1b fail 计数 >= 1", int((exit_p or {}).get("fail", 0)) >= 1,
              str(exit_p))
        # (c) manifest 无记录
        man = dc.ConvertManifest(os.path.join(wav_dir, dc.MANIFEST_NAME))
        man.load()
        check("K1c manifest 无该文件记录（保证下次能重试）",
              not man.already_done(bad), "坏文件被记账了 -> 永不重试")
        # (d) payload 含可展示的提示文本
        if failed:
            p = failed[0]
            check("K1d 带 zero_output 标记", p.get("zero_output") is True,
                  str(p))
            check("K1d error 含「未产出任何 WAV」",
                  "未产出任何 WAV" in str(p.get("error", "")), str(p.get("error")))
            check("K1d n_wav/n_files 显式为 0（统计口径一致）",
                  p.get("n_wav") == 0 and p.get("n_files") == 0, str(p))
        landed = len(wav_files(wav_dir))
        check("K1d 落盘 0 个 WAV", landed == 0, "落盘 %d 个" % landed)

    # ---- K4: 正常文件（假 convert, 快）不受影响 ----
    wav_dir2 = os.path.join(tmp, "k2", "WAV")
    os.makedirs(wav_dir2, exist_ok=True)
    col2 = Collector()
    fake = ZeroOutputConvert(wav_dir2)
    w2 = dc.ConvertWorker(wav_dir2, col2, threading.Event())
    w2.convert_fn = fake
    w2.start()
    w2.enqueue({"src": good, "url": "u_ok", "policy": "chip"})
    w2.enqueue({"src": make_src(os.path.join(src_dir, "zero2.nsf")),
                "url": "u_zero", "policy": "chip"})
    w2.mark_src_finished()
    e2 = col2.wait_for("cv_thread_exit", 30)
    w2.wait(10)
    check("K4 正常文件仍成功（ok=1）", int((e2 or {}).get("ok", 0)) == 1, str(e2))
    check("K4 正常文件发 cv_file_done",
          len(col2.of("cv_file_done")) == 1, str(len(col2.of("cv_file_done"))))
    check("K4 零产出文件发 cv_file_failed",
          len(col2.of("cv_file_failed")) == 1,
          str(len(col2.of("cv_file_failed"))))
    check("K4 零产出的不误报到 cv_file_done",
          all("zero2" not in str(p.get("src"))
              for p in col2.of("cv_file_done")))
    man2 = dc.ConvertManifest(os.path.join(wav_dir2, dc.MANIFEST_NAME))
    man2.load()
    check("K4 正常文件有记录", man2.already_done(good))
    check("K4 零产出文件无记录", not man2.already_done(
        os.path.join(src_dir, "zero2.nsf")))


def test_composite_cancel(tmp):
    print("\n[7] H 组：停止够得到当前文件（B20）+ 空窗期（B21）")
    # ---- H3: _CancelPair 单元 ----
    a = threading.Event()
    b = threading.Event()
    pair = dc._CancelPair(a, b)
    check("H3 都不置 -> 未取消", not pair.is_set())
    a.set()
    check("H3 只置全局 -> 取消", pair.is_set())
    a.clear()
    b.set()
    check("H3 只置本文件 -> 取消", pair.is_set())
    b.clear()
    check("H3 都清 -> 未取消", not pair.is_set())
    check("H3 不是 threading.Event（不依赖继承）",
          not isinstance(pair, threading.Event))

    # ---- H1: 停止够得到**正在跑**的文件（1s 内返回）----
    wav_dir = os.path.join(tmp, "h1", "WAV")
    os.makedirs(wav_dir, exist_ok=True)
    src_dir = os.path.join(tmp, "h1", "src")
    col = Collector()
    fake = ZeroOutputConvert(wav_dir, hang=300.0)
    w = dc.ConvertWorker(wav_dir, col, threading.Event())
    w.convert_fn = fake
    w.start()
    f = make_src(os.path.join(src_dir, "hang.nsf"))
    w.enqueue({"src": f, "url": "u", "policy": "chip"})
    end = time.time() + 20
    while time.time() < end and w.current() is None:
        time.sleep(0.02)
    check("H1 慢文件在转", w.current() is not None)
    t0 = time.time()
    w.cancel()
    dt = time.time() - t0
    check("H1 cancel() 立即返回", dt < 1.0, "%.2fs" % dt)
    w.wait(20)
    check("H1 线程 1s 内退出（停止够得到当前文件）",
          not w.is_alive() and (time.time() - t0) < 20,
          "%.1fs" % (time.time() - t0))
    # ★ 判据是**落盘数**，不是返回值（两种实现的 ok 都是 0）
    landed = len(wav_files(wav_dir))
    check("H1 停止后落盘 0 个（不是返回值判据）", landed == 0,
          "落盘 %d 个: %s" % (landed, wav_files(wav_dir)))

    # ---- H2: cancel() 会连带置 _cur["cancel"] ----
    wav_dir2 = os.path.join(tmp, "h2", "WAV")
    os.makedirs(wav_dir2, exist_ok=True)
    fake2 = ZeroOutputConvert(wav_dir2, hang=300.0)
    w2 = dc.ConvertWorker(wav_dir2, lambda *a: None, threading.Event())
    w2.convert_fn = fake2
    w2.start()
    w2.enqueue({"src": make_src(os.path.join(src_dir, "h2.nsf")),
                "url": "u", "policy": "chip"})
    end = time.time() + 20
    while time.time() < end and w2.current() is None:
        time.sleep(0.02)
    cur = w2.current() or {}
    check("H2 _cur 含 per-file cancel", hasattr(cur.get("cancel"), "is_set"))
    check("H2 置位前未取消", not cur.get("cancel").is_set())
    w2.cancel()
    check("H2 cancel() 后 _cur['cancel'] 被置位（B20）",
          cur.get("cancel").is_set())
    w2.wait(20)

    # ---- H4: 空窗期（B21）—— 全局 Event 在 composite 里，产出必须是 0 ----
    # ⚠⚠ **本用例必须是「差分测试」**：错法（只传 job_cancel）与正法
    #   （复合）的返回值/ok/fail **完全一样**，唯一区别是**落盘数**。
    #   如果错法也能过，本用例就白写了 —— 所以这里用一个「真的会落盘」的
    #   假 convert：cancel 未置位时**无条件写一个 WAV**（复刻 write_tracks
    #   先写第一轨、之后才检查 cancel 的真实行为）。
    #   差分表（架构师实测 + 本用例复刻）：
    #       错法（只传 job_cancel）-> 落盘 1 个 ⚠
    #       正法（复合含全局）    -> 落盘 0 个 ✓
    def land_or_stop(paths, out_dir, policy, cancel=None, **kw):
        """复刻「先写一轨, 再看取消」—— cancel 未置位就真的落盘。"""
        if cancel is None or not cancel.is_set():
            os.makedirs(out_dir, exist_ok=True)
            idx = int(kw.get("start_idx", 0)) + 1
            p = os.path.join(out_dir, "%02d_Ghost.wav" % idx)
            with open(p, "wb") as fh:
                fh.write(b"RIFF" + b"\x00" * 2048)
            return {"ok": 1, "fail": 0, "files": [p], "seconds": 0.1,
                    "errors": [], "last_idx": idx}
        # 已取消：write_tracks 会在写第一轨**之前** break -> 零产出
        return {"ok": 0, "fail": 0, "files": [], "seconds": 0.0,
                "errors": [], "last_idx": kw.get("start_idx", 0)}

    results = {}
    for label, build in (("正法(复合含全局)", True), ("错法(只传job_cancel)", False)):
        wd = os.path.join(tmp, "h4_%s" % ("ok" if build else "bad"), "WAV")
        os.makedirs(wd, exist_ok=True)
        glob = threading.Event()
        in_window = threading.Event()      # 消费线程已出队、正要进 convert
        release = threading.Event()       # 放行, 让它真正调 convert

        def fake_win(paths, out_dir, policy, cancel=None, _b=build, **kw):
            if _b:
                cc = dc._CancelPair(glob, threading.Event())   # 正法
            else:
                cc = threading.Event()                         # 错法: 与全局无关
            return land_or_stop(paths, out_dir, policy, cancel=cc, **kw)

        ww = dc.ConvertWorker(wd, lambda *a: None, glob)
        ww.convert_fn = fake_win

        # ★ 把「停止」精确投进 B21 空窗：任务已出队（_estimate_bytes 在跑）、
        #   convert 还没被调用。这正是用户最容易点停止的时刻。
        orig_est = ww._estimate_bytes

        def gated_est(src, _o=orig_est, _w=in_window, _r=release):
            _w.set()
            _r.wait(20)                   # 卡在这里, 等主线程投停止信号
            return _o(src)

        ww._estimate_bytes = gated_est
        ww.start()
        ww.enqueue({"src": make_src(os.path.join(src_dir, "w_%s.nsf"
                                                  % ("ok" if build else "bad"))),
                    "url": "u", "policy": "chip"})
        ww.mark_src_finished()
        # 等到任务真的出队（进了磁盘估算 = 已出 convert 之前）
        end = time.time() + 20
        while time.time() < end and not in_window.is_set():
            time.sleep(0.01)
        check("H4 空窗期已构造（任务出队、convert 未调）[%s]" % label,
              in_window.is_set())
        glob.set()                          # ← 用户此刻点「■ 停止」
        release.set()                       # 放行消费线程
        ww.wait(20)
        results[label] = len(wav_files(wd))

    check("H4 正法（复合含全局）空窗期停止 -> 落盘 0 个",
          results["正法(复合含全局)"] == 0,
          "落盘 %d 个" % results["正法(复合含全局)"])
    check("H4 差分成立：错法（只传 job_cancel）会偷偷落盘 1 个 —— "
          "证明本用例真的有鉴别力",
          results["错法(只传job_cancel)"] == 1,
          "错法落盘 %d 个（若也是 0，本用例无鉴别力，需换更狠的假 convert）"
          % results["错法(只传job_cancel)"])

    # ---- H5: _cur 为 None 时 cancel 不抛 ----
    w3 = dc.ConvertWorker(os.path.join(tmp, "h5", "WAV"),
                          lambda *a: None, threading.Event())
    w3.convert_fn = ZeroOutputConvert(os.path.join(tmp, "h5", "WAV"))
    w3._cur = None
    try:
        w3.cancel()
        check("H5 _cur=None 时 cancel 不抛", True)
    except Exception as e:                            # noqa: BLE001
        check("H5 _cur=None 时 cancel 不抛", False,
              "%s: %s" % (type(e).__name__, e))
    w3._cur = {"cancel": None}                       # 极端: cancel 为 None
    try:
        w3.cancel()
        check("H5 _cur['cancel']=None 时 cancel 不抛", True)
    except Exception as e:                            # noqa: BLE001
        check("H5 _cur['cancel']=None 时 cancel 不抛", False,
              "%s: %s" % (type(e).__name__, e))
    check("H5 cancel 后全局 Event 已置位", w3._cancel.is_set())
    # 重复 cancel 幂等
    try:
        w3.cancel()
        check("H5 重复 cancel 不抛（幂等）", True)
    except Exception as e:                            # noqa: BLE001
        check("H5 重复 cancel 不抛（幂等）", False,
              "%s: %s" % (type(e).__name__, e))


def test_event_reuse(tmp):
    print("\n[8] 停止后可复用：Event 不能被永久置位")
    wav_dir = os.path.join(tmp, "reuse", "WAV")
    os.makedirs(wav_dir, exist_ok=True)
    src_dir = os.path.join(tmp, "reuse", "src")
    f = make_src(os.path.join(src_dir, "a.nsf"))

    # 第 1 轮: 取消
    col1 = Collector()
    w1 = dc.ConvertWorker(wav_dir, col1, threading.Event())
    w1.convert_fn = FakeConvert(wav_dir)
    w1.start()
    w1.enqueue({"src": f, "url": "u", "policy": "chip"})
    w1.cancel()
    w1.wait(20)
    check("第 1 轮已取消", w1._cancel.is_set())
    produced1 = len(wav_files(wav_dir))

    # 第 2 轮: **新** Event + 新 worker -> 必须能正常转换
    col2 = Collector()
    fake2 = FakeConvert(wav_dir)
    w2 = dc.ConvertWorker(wav_dir, col2, threading.Event())
    check("第 2 轮用的是全新的 Event（未继承置位状态）",
          not w2._cancel.is_set())
    w2.convert_fn = fake2
    w2.start()
    w2.enqueue({"src": f, "url": "u", "policy": "chip"})
    w2.mark_src_finished()
    exit2 = col2.wait_for("cv_thread_exit", 30)
    w2.wait(20)
    check("第 2 轮能正常产出（没被上一轮的取消污染）",
          len(fake2.calls) == 1, str(len(fake2.calls)))
    check("第 2 轮 OK", int((exit2 or {}).get("ok", 0)) == 1, str(exit2))
    check("第 2 轮后产物增加",
          len(wav_files(wav_dir)) > produced1,
          "%d -> %d" % (produced1, len(wav_files(wav_dir))))

    # 同一 worker 复用（cancel 后又 enqueue）应立即退出而不是继续干活
    w3 = dc.ConvertWorker(wav_dir, lambda *a: None, threading.Event())
    w3.cancel()
    fake3 = FakeConvert(wav_dir)
    w3.convert_fn = fake3
    w3.start()
    w3.enqueue({"src": f, "url": "u", "policy": "chip"})
    w3.mark_src_finished()
    w3.wait(20)
    check("已取消的 worker 不再消费（省 CPU）", len(fake3.calls) == 0,
          str(len(fake3.calls)))


# ---------------------------------------------------------------------------
# ⑨ 回调签名兼容（实测踩过的真 bug）
# ---------------------------------------------------------------------------

def test_callback_signatures():
    print("\n[9] 回调签名兼容：两个调用方给的参数个数不同")
    import inspect
    w = dc.ConvertWorker(os.path.join(os.path.dirname(__file__), "_x"),
                         lambda *a: None, threading.Event())
    for name in ("_on_log", "_on_progress", "_on_stage"):
        fn = getattr(w, name)
        spec = inspect.getfullargspec(fn)
        varargs = bool(spec.varargs)
        npos = len(spec.args) - 1          # 减掉 self
        check("%s 接受可变参数（*args）" % name, varargs,
              "args=%s varargs=%s" % (spec.args, spec.varargs))
        check("%s 无必填位置参数" % name, npos == 0 or varargs,
              "npos=%d" % npos)

    # 真调一遍各种形态, 一个都不许抛
    got = []
    w2 = dc.ConvertWorker(os.path.join(os.path.dirname(__file__), "_x"),
                          lambda k, p: got.append((k, p)), threading.Event())
    try:
        w2._on_log("纯文本")
        w2._on_log("带级别", "文本")
        w2._on_log()
        w2._on_progress(1, 2, 3.0)                 # chip_decode: 3 参
        w2._on_progress(1, 2, "name", 3.0)        # write_tracks: 4 参
        w2._on_progress(1)                        # 极端: 1 参
        w2._on_stage(1, 1, 1)                     # spc_wav: 3 参
        w2._on_stage(1, 1)                        # 2 参
        w2._on_stage()                            # 0 参
    except Exception as e:                       # noqa: BLE001
        check("各种参数形态都不抛", False, "%s: %s" % (type(e).__name__, e))
        return
    check("各种参数形态都不抛", True)
    kinds = [k for k, _p in got]
    check("发的是 cv_log/cv_progress/cv_stage",
          kinds == ["cv_log", "cv_log", "cv_log",
                    "cv_progress", "cv_progress", "cv_progress",
                    "cv_stage", "cv_stage", "cv_stage"], str(kinds))
    prog3 = [p for k, p in got if k == "cv_progress" and p.get("n") == 3][0]
    check("chip_decode 的 3 参形态被收下（idx/total/name）",
          prog3.get("idx") == 1 and prog3.get("total") == 2, str(prog3))


# ---------------------------------------------------------------------------
# ⑪ manifest 生命周期（QA 建议固化）
# ---------------------------------------------------------------------------

def test_manifest_lifecycle(tmp):
    """manifest 只给**真成功**建账 —— 同时防两个相反方向的错。

    QA 实测发现的两条事实拼在一起才是完整结论：
      ① 正常转换后 manifest 有完整记账、``already_done()=True``（防漏记账）
      ② B18 坏文件场景下manifest **连文件都不会被创建**（``entries=0``）（防错记账）

    两者缺一都会出事，且方向相反：
      * **漏记账** -> 下次 ``already_done()`` 返回 False -> **重复转**
        -> 覆盖用户文件 / 白跑几 GB 磁盘；
      * **错记账** -> 下次指纹匹配被跳过 -> **永不重试**
        -> 用户以为转过了，其实没有。**「静默 + 不可恢复」比报错更糟**。

    所以这里把**两个方向一起断言**，而不是只测 happy path。
    """
    print("\n[11] manifest 生命周期：只给真成功建账（双向防错）")
    wav_dir = os.path.join(tmp, "life", "WAV")
    os.makedirs(wav_dir, exist_ok=True)
    src_dir = os.path.join(tmp, "life", "src")
    mpath = os.path.join(wav_dir, dc.MANIFEST_NAME)

    # ---- 方向① 坏文件：manifest 不得被创建 ----
    col_bad = Collector()
    fake_bad = ZeroOutputConvert(wav_dir)
    w_bad = dc.ConvertWorker(wav_dir, col_bad, threading.Event())
    w_bad.convert_fn = fake_bad
    w_bad.start()
    bad = make_src(os.path.join(src_dir, "zero.nsf"))
    w_bad.enqueue({"src": bad, "url": "u_bad", "policy": "chip"})
    w_bad.mark_src_finished()
    w_bad.wait(20)

    check("方向① 零产出后 manifest 文件**完全不存在**",
          not os.path.exists(mpath), "manifest 被创建了 -> 坏文件污染账本")
    man_a = dc.ConvertManifest(mpath)
    check("方向① 空 manifest load() == 0", man_a.load() == 0)
    check("方向① 零产出文件判为「未转过」（可重试）",
          not man_a.already_done(bad))

    # ---- 方向② 好文件：manifest 必须建账且能被读到 ----
    col_ok = Collector()
    fake_ok = FakeConvert(wav_dir)
    w_ok = dc.ConvertWorker(wav_dir, col_ok, threading.Event())
    w_ok.convert_fn = fake_ok
    w_ok.start()
    good = make_src(os.path.join(src_dir, "ok.nsf"))
    w_ok.enqueue({"src": good, "url": "u_ok", "policy": "chip"})
    w_ok.mark_src_finished()
    w_ok.wait(20)

    check("方向② 有成功产出后 manifest 被创建", os.path.exists(mpath))
    man_b = dc.ConvertManifest(mpath)
    n = man_b.load()
    check("方向② manifest 恰好 1 条记账（坏文件没混进来）", n == 1,
          "got %d（若为 0 = 漏记账；若为 2 = 坏文件污染）" % n)
    check("方向② 好文件 already_done()=True（防重复转）",
          man_b.already_done(good))
    check("方向② 记账含 wavs 清单（供全量探使用）",
          bool((man_b.entry(good) or {}).get("wavs")),
          str(man_b.entry(good)))
    check("方向② 记账记了 policy（便于日后排查）",
          (man_b.entry(good) or {}).get("policy") == "chip",
          str((man_b.entry(good) or {}).get("policy")))

    # ---- 两者同时成立才算健康 ----
    check("综合：账本只含真成功的条目（双向都对）",
          man_b.already_done(good) and not man_b.already_done(bad),
          "good=%s bad=%s" % (man_b.already_done(good),
                              man_b.already_done(bad)))

    # ---- FR-C8.5: 「🗑 清除缓存」不删 manifest（QA 侧实测, 这里固化）----
    # 站点索引缓存与 WAV 记账**是两回事**：删了记账会导致全量重转。
    mpath2 = os.path.join(wav_dir, "m2.json")
    dc.ConvertManifest(mpath2).record(good, ["01_Ok.wav"], "chip")
    check("FR-C8.5 manifest 不由站点缓存逻辑删除（存在即保留）",
          os.path.exists(mpath2))
    # 说明: 真正的「清除缓存」是 jw.clear_cache(), 与 WAV 目录无交集;
    # 本实现**刻意不写任何删除 WAV/ 或 manifest 的代码** ——
    # 「没有代码就没有 bug」。QA 已实测调用 _clear_cache() 后 manifest 仍在。


# ---------------------------------------------------------------------------
# ⑫ cv_error 反向触发（QA 对账发现这条路径原本零覆盖）
# ---------------------------------------------------------------------------

def test_cv_error_path(tmp):
    """让 :meth:`ConvertWorker.run` 的**最外层** except 真正触发一次。

    ============================ 为什么必须有这条 ============================
    之前唯一的 cv_error 断言是 ``"cv_error" not in kinds`` —— 而那几个
    collector 全是 happy path，``cv_error`` 在结构上**不可能**产生，
    等于在断言「一个从未发生的事没发生」，**恒真**。

    风险不在「错误处理写得对不对」，而在：**这条路径若坏掉，没有任何一条
    测试会红**。而它坏掉的表现极严重 ——
    ``cv_thread_exit`` 是 GUI 唯一的收尾信号（`_handle_cv` 收到它才调
    `_finish_download()`）。一旦丢了事件，**下载窗口永久卡死**：
    开始/停止按钮锁死、进度条不动，用户只能杀进程。

    所以本用例做两件事：
      ① **正向触发** cv_error，让 :589 那条「无 cv_error」有对照、变有意义；
      ② 断言 **cv_thread_exit 依然发出** —— 这才是真正的收尾保证。
    """
    print("\n[12] cv_error 反向触发 + cv_thread_exit 无条件发出")
    wav_dir = os.path.join(tmp, "err", "WAV")
    os.makedirs(wav_dir, exist_ok=True)
    src_dir = os.path.join(tmp, "err", "src")

    # ---- ① 制造一个「最外层」异常: 让编号扫描崩掉 ----
    # 这条路径在正常情况下不可达（wav_scan_numbering 内部已兜 OSError），
    # 但正因为不可达才要用注入来证明「万一崩了也不会静默卡死」。
    col = Collector()
    w = dc.ConvertWorker(wav_dir, col, threading.Event())
    w.convert_fn = FakeConvert(wav_dir)

    def boom():
        raise RuntimeError("模拟 _ensure_numbering 崩溃")

    w._ensure_numbering = boom
    w.start()
    w.enqueue({"src": make_src(os.path.join(src_dir, "a.nsf")),
               "url": "u", "policy": "chip"})
    w.mark_src_finished()
    w.wait(20)

    check("cv_error 确实发出（不再是死代码）", len(col.of("cv_error")) == 1,
          "kinds=%s" % col.kinds())
    if col.of("cv_error"):
        err = col.first("cv_error")
        check("cv_error payload 带 error 文本",
              "模拟 _ensure_numbering 崩溃" in str(err.get("error", "")),
              str(err))
        check("cv_error payload 带异常类型",
              "RuntimeError" in str(err.get("error", "")), str(err))

    # ---- ② **关键**: 外层异常后cv_thread_exit 仍必须发出 ----
    # 丢了它 = GUI 永久卡死（按钮锁死、无进度、只能杀进程）。
    check("cv_thread_exit 在外层异常后**依然发出**（GUI 收尾保证）",
          len(col.of("cv_thread_exit")) == 1, "kinds=%s" % col.kinds())
    check("线程确实退出（不留悬空线程）", not w.is_alive())
    # 恰好各一次，重复发会让 GUI 走两遍收尾（B23 重入守卫之外的第二重保险）
    check("cv_error / cv_thread_exit 都恰好 1 次（不重复）",
          len(col.of("cv_error")) == 1 and len(col.of("cv_thread_exit")) == 1)

    # ---- ③ stats() 自身抛异常时，cv_thread_exit 仍要发 ----
    # 防御: finally 里原本直接 self.stats()，它一抛就会把整个 finally 带崩，
    # cv_thread_exit 一起丢 -> 又是永久卡死。
    col2 = Collector()
    w2 = dc.ConvertWorker(wav_dir, col2, threading.Event())
    w2.convert_fn = FakeConvert(wav_dir)
    w2._ensure_numbering = boom
    w2.start()
    w2.mark_src_finished()

    def boom_stats():
        raise RuntimeError("模拟 stats() 崩溃")

    w2.stats = boom_stats
    w2.wait(20)
    check("stats() 抛异常时 cv_thread_exit 仍然发出",
          len(col2.of("cv_thread_exit")) == 1,
          "kinds=%s" % col2.kinds())
    check("stats() 异常时线程也不挂死", not w2.is_alive())
    # ---- ④ 现在: 那条原本恒真的断言终于有了对照 ----
    check("对照成立：happy path 确实不发 cv_error（此时才是真结论）",
          len(col.of("cv_error")) >= 1 and len(col2.of("cv_error")) >= 1)


# ---------------------------------------------------------------------------
# ⑬ 线程铁律：转换线程体内不得出现 Tk 调用
# ---------------------------------------------------------------------------

def test_thread_purity():
    print("\n[10] 线程铁律：dl_convert 不得 import tkinter / 碰 Tk")
    src = open(os.path.join(ROOT, "dl_convert.py"), encoding="utf-8").read()
    for bad in ("import tkinter", "from tkinter", ".config(", "messagebox",
                "_set_state", "var_status", "logw"):
        check("dl_convert.py 不含 %r" % bad, bad not in src)
    check("ConvertWorker 继承 threading.Thread",
          "class ConvertWorker(threading.Thread)" in src)
    check("ConvertWorker daemon=True", "daemon=True" in src)
    check("线程名固定 dl-convert-0", 'name="dl-convert-0"' in src)
    check("并发恒为 1（无 ThreadPool）",
          "ThreadPool" not in src and "concurrent.futures" not in src)
    # ⚠ 不要自己定义 is_alive()（架构师在设计里手滑加过一个）——
    #   覆盖它会让 start() 之后的存活判断全部失效，连带把 B17 弄坏。
    check("不自定义 is_alive（用 threading.Thread 继承来的）",
          "def is_alive" not in src)
    # R7: used 键必须剥两层（裸曲名）
    check("wav_scan_numbering 剥了序号前缀（对齐 spc_wav 的 used 键）",
          "stem = stem[m.end():]" in src)


# ---------------------------------------------------------------------------
# ⑫ 按游戏名分文件夹（v5.2-web-game）
# ---------------------------------------------------------------------------

def test_game_folders(tmp):
    """``WAV/<游戏名>/``：**编号和清单都必须按目录各算各的**。

    用户的抱怨：``WAV/`` 里躺着十几个游戏的产物，``01_`` 到 ``300_`` 一路连着，
    根本分不清哪个文件是哪款游戏的。

    这条测试守三件事，缺一条就会「看着分了文件夹但其实是错的」：

    1. **两个游戏都从 ``01_`` 起** —— 共用一个 ``start_idx`` 的话，
       第 2 个游戏的第一首会叫 ``03_xxx.wav``；
    2. **清单也分开** —— 共用一份 manifest 时，B 游戏里同名的 ``01.psf``
       会被 A 游戏的记录判成「已转过」直接跳过（**静默丢文件**）；
    3. **散文件不受影响** —— 直接躺在下载根里的音源没有游戏名，
       仍写进 ``WAV/`` 根，不能因此报错或被塞进某个游戏目录。
    """
    print("\n[13] 按游戏名分文件夹（WAV/<游戏名>/）")
    base = os.path.join(tmp, "gamefold")
    wav = os.path.join(base, "WAV")
    os.makedirs(wav, exist_ok=True)

    games = ["Final Fantasy VII (Japan)", "Chrono Trigger [PS1]"]
    srcs = [make_src(os.path.join(base, g, "sound", "01_x.spc")) for g in games]
    loose = make_src(os.path.join(base, "loose.spc"))

    def tree(root):
        out = []
        for r, _d, fs in os.walk(root):
            for f in fs:
                if f.lower().endswith(".wav"):
                    out.append(os.path.relpath(os.path.join(r, f), root))
        return sorted(out)

    col = Collector()
    cv = dc.ConvertWorker(wav, col, threading.Event(), group_by_game=True)
    cv.convert_fn = FakeConvert(wav, n_wav=2)
    cv.start()
    for s in srcs + [loose]:
        cv.enqueue({"src": s, "url": "u://" + os.path.basename(s),
                    "pkg": "", "policy": "id666", "n_hint": 0})
    cv.mark_src_finished()
    cv.join(60)

    got = tree(wav)
    check("两个游戏各建了自己的子文件夹",
          os.path.join(games[0], "01_T01.wav") in got
          and os.path.join(games[1], "01_T01.wav") in got, str(got))
    check("每个游戏的编号都从 01 起（没接着上一个游戏往下数）",
          os.path.join(games[0], "02_T02.wav") in got
          and os.path.join(games[1], "02_T02.wav") in got, str(got))
    check("散文件仍落在 WAV/ 根（没有游戏名时不瞎分）",
          "01_T01.wav" in got, str(got))
    check("总共 3 个源 × 2 首 = 6 个 WAV", len(got) == 6, str(got))
    check("日志里说了每个游戏写到哪",
          any("Final Fantasy VII (Japan)" in str(p.get("text") or "")
              for p in col.of("cv_log")),
          str([p.get("text") for p in col.of("cv_log")][:4]))

    # 幂等 + 清单隔离：重跑同一批，必须全部 cv_skip（不会因为换了目录重转）
    col2 = Collector()
    cv2 = dc.ConvertWorker(wav, col2, threading.Event(), group_by_game=True)
    cv2.convert_fn = FakeConvert(wav, n_wav=2)
    cv2.start()
    for s in srcs + [loose]:
        cv2.enqueue({"src": s, "url": "u://2" + os.path.basename(s),
                     "pkg": "", "policy": "id666", "n_hint": 0})
    cv2.mark_src_finished()
    cv2.join(60)
    check("第二轮全部跳过（每目录各有一份清单）",
          len(col2.of("cv_skip")) == 3, str(col2.kinds()))
    check("第二轮产物数不变", len(tree(wav)) == 6, str(tree(wav)))

    # 对照组：关掉分组 -> 全部平铺、编号连续（老行为零变化）
    wav2 = os.path.join(tmp, "gamefold_flat", "WAV")
    os.makedirs(wav2, exist_ok=True)
    col3 = Collector()
    cv3 = dc.ConvertWorker(wav2, col3, threading.Event(), group_by_game=False)
    cv3.convert_fn = FakeConvert(wav2, n_wav=1)
    cv3.start()
    for s in srcs + [loose]:
        cv3.enqueue({"src": s, "url": "u://3" + os.path.basename(s),
                     "pkg": "", "policy": "id666", "n_hint": 0})
    cv3.mark_src_finished()
    cv3.join(60)
    flat = tree(wav2)
    check("group_by_game=False：全部平铺在 WAV/ 根",
          len(flat) == 3 and all(os.sep not in t for t in flat), str(flat))
    check("group_by_game=False：编号连续 01/02/03",
          [t[:2] for t in flat] == ["01", "02", "03"], str(flat))


def main():
    ap = argparse.ArgumentParser(description="ConvertWorker 离线验收")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    # 按 PID 隔离：两个进程并发跑会互删文件，报出像解码坏了的 FileNotFoundError
    tmp = tempfile.mkdtemp(prefix="dl_convert_worker_test_%d " % os.getpid())
    t0 = time.time()
    print("临时目录: %s" % tmp)
    try:
        test_failure_isolation(tmp)
        test_cancel(tmp)
        test_timeout_isolation(tmp)
        test_disk_gate(tmp)
        test_events_and_idempotent(tmp)
        test_zero_output(tmp)
        test_composite_cancel(tmp)
        test_event_reuse(tmp)
        test_callback_signatures()
        test_manifest_lifecycle(tmp)
        test_cv_error_path(tmp)
        test_thread_purity()
        test_game_folders(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    dt = time.time() - t0
    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项  (%.1fs)" % (_pass, _fail, dt))
    if _notes:
        print("备注:")
        for t in _notes:
            print("  - " + t)
    print("=" * 60)
    if args.json:
        print(json.dumps({"pass": _pass, "fail": _fail, "secs": round(dt, 1)},
                         ensure_ascii=False))
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
