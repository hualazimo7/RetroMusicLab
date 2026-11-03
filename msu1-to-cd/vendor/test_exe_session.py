#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 exe 的会话锁与拖放降级策略。

  A. 正常关闭 -> .running 被清掉, 下次拖放照常挂载
  B. 强杀(模拟卡退) -> .running 残留, 下次启动自动关闭拖放并提示
  C. 残留下正常关闭 -> 锁清掉, 第三次启动恢复拖放

读 dist/crash.log 里的 "拖放挂载:" 行判断实际状态。
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(BASE, "dist", "MSU1Converter.exe")
LOG = os.path.join(BASE, "dist", "crash.log")
LOCK = os.path.join(BASE, "dist", ".running")

u32 = ctypes.windll.user32
u32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                               wintypes.LPARAM), wintypes.LPARAM]
u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM]


def find_main(timeout=12.0):
    """找到转换器主窗口句柄 (标题含"转换器")。"""
    end = time.time() + timeout
    found = []

    def cb(hwnd, _lp):
        buf = ctypes.create_unicode_buffer(300)
        u32.GetWindowTextW(hwnd, buf, 300)
        if "转换器" in buf.value and u32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    cb_ref = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb)
    while time.time() < end:
        found.clear()
        u32.EnumWindows(cb_ref, 0)
        if found:
            return found[0]
        time.sleep(0.3)
    return 0


def start():
    if os.path.exists(LOG):
        os.remove(LOG)
    p = subprocess.Popen([EXE], cwd=os.path.dirname(EXE),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    hwnd = find_main()
    time.sleep(1.5)                     # 等 App 初始化、写日志
    return p, hwnd


def log_lines():
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            return [x.strip() for x in f if x.strip()]
    except OSError:
        return []


def drop_state():
    for line in log_lines():
        if "拖放挂载" in line:
            return line
    return "(无记录)"


def kill(p):
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                   capture_output=True)
    time.sleep(1.5)


def close_normally(p, hwnd):
    """发 WM_CLOSE, 走 App 的 WM_DELETE_WINDOW -> 清锁。"""
    if hwnd:
        u32.PostMessageW(hwnd, 0x0010, 0, 0)
    try:
        p.wait(timeout=8)
    except subprocess.TimeoutExpired:
        kill(p)
    time.sleep(0.8)


def main():
    if not os.path.exists(EXE):
        print("FAIL 缺少 dist/MSU1Converter.exe")
        return 1
    fails = []

    # --- A. 干净启动 + 正常关闭 ---
    if os.path.exists(LOCK):
        os.remove(LOCK)
    p, hwnd = start()
    print(f"[A] 启动 pid={p.pid} hwnd={hwnd}")
    print(f"    {drop_state()}")
    if "拖放挂载: True" not in drop_state():
        fails.append("A: 干净启动时拖放未挂载")
    close_normally(p, hwnd)
    print(f"    关闭后 .running 存在: {os.path.exists(LOCK)}")
    if os.path.exists(LOCK):
        fails.append("A: 正常关闭后锁未清除")

    # --- B. 强杀后重启: 应自动关闭拖放 ---
    p, hwnd = start()
    print(f"[B] 再次启动 pid={p.pid}, 拖放: {drop_state()}")
    if "拖放挂载: True" not in drop_state():
        fails.append("B: 干净状态下的再次启动拖放未挂载")
    kill(p)
    print(f"    强杀后 .running 残留: {os.path.exists(LOCK)}")
    if not os.path.exists(LOCK):
        fails.append("B: 强杀后锁竟然没了, 降级策略会失效")

    p, hwnd = start()
    st = drop_state()
    print(f"[B2] 强杀后重启, 拖放: {st}")
    if "拖放挂载: False" not in st:
        fails.append(f"B2: 上次异常退出后未降级 (实际 {st})")
    if hwnd == 0:
        fails.append("B2: 降级后面板没起来")

    # --- C. 降级状态下正常关闭 -> 清锁 -> 恢复正常 ---
    close_normally(p, hwnd)
    print(f"[C] 降级状态下正常关闭, .running 存在: {os.path.exists(LOCK)}")
    if os.path.exists(LOCK):
        fails.append("C: 降级状态下正常关闭未清锁")

    p, hwnd = start()
    st = drop_state()
    print(f"[C2] 再启动, 拖放: {st}")
    if "拖放挂载: True" not in st:
        fails.append(f"C2: 恢复后拖放仍未挂载 (实际 {st})")
    close_normally(p, hwnd)

    # --- D. --no-drop 强制关闭拖放 ---
    if os.path.exists(LOG):
        os.remove(LOG)
    p = subprocess.Popen([EXE, "--no-drop"], cwd=os.path.dirname(EXE),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    hwnd = find_main()
    time.sleep(1.5)
    st = drop_state()
    print(f"[D] --no-drop 启动, 拖放: {st}")
    if "拖放挂载: False" not in st:
        fails.append("D: --no-drop 未关闭拖放")
    close_normally(p, hwnd)

    if os.path.exists(LOCK):
        os.remove(LOCK)
    print()
    if fails:
        for f in fails:
            print("FAIL", f)
        return 1
    print("SESSION TEST PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
