#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_smoke_cloud.py — 云盘上传 UI 冒烟（真建窗口，不真上传）。

验证这些事：
1. 「☁ 上传云盘」按钮存在且**转换前禁用**
2. 转换完成后按钮放开（模拟 done 里的那一行）
3. 扫码登录窗口能真的申请到 unikey 并画出二维码矩阵
4. 二维码是纯黑白（不跟主题走）—— 这是刻意为之，写死在这里防止被"美化"
5. 登录窗口是**三个**标签页，且默认停在「浏览器登录」（v4.21 新增，最省事的那页）
6. 「粘贴 Cookie」页可输入、可提交、空输入被拦住
7. 浏览器登录页的控件就位（不真启浏览器，那是 check_cookie_cdp.py 的事）

⚠ 不真启浏览器：这里跑在 Tk 主线程里，启浏览器要几秒，会把冒烟拖成"卡住"。
   启停与取 cookie 的真实行为由 `vendor/check_cookie_cdp.py` 的集成项覆盖。
"""

import os
import shutil
import sys
import tempfile
import time
import tkinter as tk

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJ, "msu1-to-cd"))

import msu1_gui as g          # noqa: E402
import netease_cloud as ncm   # noqa: E402
import cloud_upload as cu_mod  # noqa: E402  （别名避开 main() 里的 cu 实例）

RESULTS = []


def check(ok, label, detail=""):
    RESULTS.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", label,
                       ("  [" + detail + "]") if detail else ""))
    return ok


def main():
    root = tk.Tk()
    app = g.App(root)
    root.update()
    check(hasattr(app, "btn_cloud"), "1.1 主窗有 btn_cloud")
    check(str(app.btn_cloud["state"]) == "disabled",
          "1.2 转换前按钮是禁用态", str(app.btn_cloud["state"]))

    # 模拟 done 分支那行
    app.btn_cloud.config(state="normal")
    check(str(app.btn_cloud["state"]) == "normal", "1.3 done 后可放开")

    sess = ncm.NeteaseSession()
    dlg = g.NeteaseLoginDialog(root, sess)
    deadline = time.time() + 12
    while time.time() < deadline and not dlg._key:
        root.update()
        time.sleep(0.05)
    check(bool(dlg._key), "2.1 扫码窗口拿到 unikey", dlg._key[:12] if dlg._key else "")
    items = dlg.canvas.find_all()
    check(len(items) > 20, "2.2 二维码矩阵已绘制", "%d 个方块" % len(items))
    fills = {dlg.canvas.itemcget(i, "fill") for i in items}
    check(fills == {"#000000"}, "2.3 二维码是纯黑块（不跟主题）", str(fills))
    check(dlg.canvas["bg"] == "#ffffff", "2.4 二维码底色是纯白", str(dlg.canvas["bg"]))
    # 词表里不该出现主题强调色
    check(g.theme.PALETTE["cyan"].lower() not in {"#000000", "#ffffff"},
          "2.5 强调色确实没有混进二维码")

    # ---- 3. 三标签页 & 默认页 --------------------------------------------
    check(dlg.nb.index("end") == 3, "3.1 登录窗有三个标签页",
          "end=%s" % dlg.nb.index("end"))
    check("浏览器登录" in dlg.nb.tab(0, "text"), "3.2 第一页是浏览器登录",
          dlg.nb.tab(0, "text").strip())
    check("Cookie" in dlg.nb.tab(1, "text"), "3.3 第二页是粘贴 Cookie",
          dlg.nb.tab(1, "text").strip())
    check("扫码" in dlg.nb.tab(2, "text"), "3.4 第三页是扫码",
          dlg.nb.tab(2, "text").strip())
    check(dlg.nb.index(dlg.nb.select()) == 0,
          "3.5 默认停在浏览器登录（最省事的一页）")

    # ---- 4. 浏览器登录页控件 ---------------------------------------------
    check(hasattr(dlg, "btn_br"), "4.1 有 btn_br 按钮")
    check(not dlg.btn_br.instate(["disabled"]), "4.2 btn_br 初始可用")
    check(dlg.var_br.get() == "尚未开始", "4.3 状态初始文案", dlg.var_br.get())
    if not hasattr(g, "nck") or g.nck is None:
        check(False, "4.4 netease_cookie 模块已加载")
    else:
        name, exe = g.nck.find_browser()
        check(bool(exe) or True, "4.4 netease_cookie 已加载",
              "%s" % (name or "本机无 Chrome/Edge"))

    # ---- 5. 粘贴 Cookie 页 ------------------------------------------------
    dlg.nb.select(1)
    root.update()
    check(dlg.nb.index("current") == 1, "5.1 能切到 Cookie 页")
    check(isinstance(dlg.txt_cookie, tk.Text), "5.2 Cookie 输入框是 tk.Text")
    dlg.txt_cookie.insert("1.0", "MUSIC_U=probe_only; __csrf=x")
    check("MUSIC_U" in dlg.txt_cookie.get("1.0", "end"), "5.3 能输入 Cookie")
    dlg.txt_cookie.delete("1.0", "end")
    dlg._start_cookie_login()          # 空输入，不该发网络
    check(dlg.var_ck.get().startswith("❌"), "5.4 空输入被拦住不发请求",
          dlg.var_ck.get()[:28])

    dlg._close()
    root.update()

    # ---- 6. 下载窗也有上传云盘（v4.23）-----------------------------------
    # 只验「按钮就位 + 事件翻译 + 上传源收集 + 执行器可取消」，
    # **不点真上传**（会弹确认框并开始传文件）。
    dd = g.DownloadDialog(root)
    dd.withdraw()                       # 不闪窗；控件已构建完毕
    root.update()
    check(hasattr(dd, "btn_cloud"), "6.1 下载窗有 btn_cloud")
    check(not dd.btn_cloud.instate(["disabled"]),
          "6.2 下载窗按钮初始可用（有历史 WAV 时可直接传）")

    # 上传源：默认取当前机种 WAV 目录；空目录时返回空列表而不是报错
    wavs0, folder0 = dd._cloud_wav_files()
    check(isinstance(wavs0, list) and os.path.basename(folder0) == "WAV",
          "6.3 上传源目录是 <保存目录>/<机种>/WAV", folder0)

    tmpdir = tempfile.mkdtemp(prefix="cloud_smoke_")
    for _n in ("02_b.wav", "01_a.WAV", "notes.txt"):
        with open(os.path.join(tmpdir, _n), "wb") as _f:
            _f.write(b"")
    dd._wav_dir = lambda: tmpdir          # 实例级替换，不动真实目录
    wavs1, folder1 = dd._cloud_wav_files()
    check(len(wavs1) == 2, "6.4 只收 .wav（.WAV 也认，txt 不算）",
          "%d 个" % len(wavs1))
    check([os.path.basename(p) for p in wavs1] == ["01_a.WAV", "02_b.wav"],
          "6.5 收集结果按名排序",
          str([os.path.basename(p) for p in wavs1]))
    check(g.collect_wavs(tmpdir, os.path.join(tmpdir, "不存在")) == wavs1,
          "6.6 collect_wavs 跳过不存在的目录且不重复")

    # 事件翻译：log / prog / done 三条都要走通（done 会弹窗，先拦掉）
    _real_info = g.messagebox.showinfo
    g.messagebox.showinfo = lambda *a, **k: None
    try:
        dd._cloud_busy = True
        dd._sync_buttons()
        check(dd.btn_start.instate(["disabled"]),
              "6.7 上传中「开始下载」被禁（不写到一半就传）")
        check(dd.btn_cloud.instate(["disabled"]), "6.8 上传中上传按钮自己置灰")

        dd._dispatch(("cloud", "log", "☁ smoke-log"))
        check("☁ smoke-log" in dd.logw.get("1.0", "end"), "6.9 日志事件落到日志列")
        dd._dispatch(("cloud", "prog", 40))
        check(dd.var_status.get() == "☁ 上传中 40%", "6.10 进度事件写状态行",
              dd.var_status.get())
        dd._dispatch(("cloud", "done", (1, 0, 0)))
        check(dd._cloud_busy is False, "6.11 done 复位 busy")
        check(not dd.btn_cloud.instate(["disabled"]), "6.12 done 放开按钮")
        check(dd.var_status.get().startswith("☁ 上传完成"), "6.13 done 写汇总状态",
              dd.var_status.get())
    finally:
        g.messagebox.showinfo = _real_info

    # 执行器：cancel 已置位时一首都不该传（不碰网络），但仍要发 done
    events = []
    cu = g.CloudUploader(None, lambda k, p=None: events.append((k, p)))
    cu.cancel = __import__("threading").Event()
    cu.cancel.set()
    cu.run([os.path.join(tmpdir, "01_a.WAV")])
    check(("done", (0, 0, 0)) in events, "6.14 已取消时直接收尾发 done",
          str(events))
    check(any(k == "log" and "取消" in str(p) for k, p in events),
          "6.15 取消会留一条日志")

    # ---- 7. 合并文件 vs 分轨（v4.25）-------------------------------------
    # 合并文件就是分轨拼出来的。collect_wavs 只看扩展名，会把两者一起收进
    # 上传列表 → 同一张专辑传两遍、云盘空间翻倍。这里验辨识与规划的逻辑。
    m1 = os.path.join(tmpdir, "Pack（合并）.wav")
    m2 = os.path.join(tmpdir, "Pack（合并）.WAV")
    t1, t2 = wavs1[0], wavs1[1]          # 6.4 建的 01_a.WAV / 02_b.wav
    with open(m1, "wb") as _f:
        _f.write(b"RIFF" + b"\0" * 96)
    with open(m2, "wb") as _f:
        _f.write(b"RIFF" + b"\0" * 96)
    check(g.is_merged_wav(m1) and not g.is_merged_wav(t1),
          "7.1 认得出合并文件，不误判分轨")
    check(g.is_merged_wav(m2), "7.2 大写 .WAV 也认")
    mm, tt = g.split_merged([m1, t1, t2])
    check(mm == [m1] and tt == [t1, t2], "7.3 分组正确",
          "%s / %s" % ([os.path.basename(x) for x in mm],
                       [os.path.basename(x) for x in tt]))
    # v5.0-qt 起标记常量只在两处：wavmerge 定义、cloud_upload 引用。
    # 这里断言 cloud_upload 真的取到了 wavmerge 的值，而不是兜底常量。
    check(g.wavmerge is not None and cu_mod.MERGE_TAG == g.wavmerge.MERGE_TAG,
          "7.4 后缀标记与 wavmerge 同源（改一处不会漂移）", cu_mod.MERGE_TAG)
    _mt = g.wavmerge.merge_target(tmpdir, "Some Pack")
    check(g.is_merged_wav(_mt),
          "7.5 merge_target 的产物能被认出来（命名闭环）",
          os.path.basename(_mt))

    # 只有一种形态时**静默**返回（不弹窗，老行为完全不变）
    check(g.plan_cloud_upload(root, [t1, t2]) == [t1, t2],
          "7.6 只有分轨时不弹窗，原样返回")
    check(g.plan_cloud_upload(root, [m1]) == [m1],
          "7.7 只有合并时不弹窗，原样返回")
    check(g.plan_cloud_upload(root, []) == [], "7.8 空列表安全")

    # 两种并存要弹窗 —— 用替身验转发（真弹窗是模态的，会卡住门禁）
    _real_ask = g._ask_cloud_scope
    try:
        g._ask_cloud_scope = lambda _p, _m, _t: None
        check(g.plan_cloud_upload(root, [m1, t1]) is None,
              "7.9 弹窗取消 → 返回 None（中止上传）")
        g._ask_cloud_scope = lambda _p, _m, _t: _t + _m
        check(g.plan_cloud_upload(root, [m1, t1]) == [t1, m1],
              "7.10 弹窗的选择原样透传")
        _got = {}
        g._ask_cloud_scope = lambda _p, _m, _t: _got.update(m=_m, t=_t) or _m
        g.plan_cloud_upload(root, [m1, m2, t1, t2])
        check(_got.get("m") == [m1, m2] and _got.get("t") == [t1, t2],
              "7.11 传给弹窗的分组正确", str(_got))
    finally:
        g._ask_cloud_scope = _real_ask

    # ---- 8. 账号切换（v4.26）---------------------------------------------
    # ⚠ 全程**不碰真实登录态**：昵称用替身喂，对话框建好后立刻把
    # `_closed` 置位 —— `__init__` 里排了 `after(80, _begin)`，
    # 不拦的话它真会去清 cookie、开浏览器。
    print("\n-- 8. 账号切换 --")
    check(hasattr(app, "btn_cloud_acct") and app.btn_cloud_acct.cget("text") == "▾",
          "8.1 主窗「☁ 上传云盘」旁有账号菜单按钮")
    check(hasattr(dd, "btn_cloud_acct") and dd.btn_cloud_acct.cget("text") == "▾",
          "8.2 下载窗也有账号菜单按钮")
    check(not app.btn_cloud_acct.instate(["disabled"]),
          "8.3 账号按钮不受「上传云盘」禁用影响（没 WAV 也能换号）")

    if g.ncm is None:
        check(False, "8.4 cloud_account_label 用替身验三种情况", "ncm 未导入")
    else:
        _real_load = g.ncm.load_cookie
        try:
            g.ncm.load_cookie = lambda: {"user_name": "测试号A"}
            check(g.cloud_account_label() == "测试号A", "8.4 读到保存的昵称",
                  g.cloud_account_label())
            g.ncm.load_cookie = lambda: {"cookie": "x"}
            check(g.cloud_account_label() == "",
                  "8.5 老存档没昵称时返回空串（不显示「None」）")
            g.ncm.load_cookie = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            check(g.cloud_account_label() == "",
                  "8.6 读文件异常也不抛（菜单必须能打开）")
        finally:
            g.ncm.load_cookie = _real_load

    sd = g.SwitchAccountDialog(root, "switch")
    sd._closed = True                    # 拦下 after(80) 里的 _begin
    check(sd.top.title() == "切换网易云账号", "8.7 换号窗标题", sd.top.title())
    sd._finish(True, "测试号B", "")
    check(sd.ok and sd.user == "测试号B", "8.8 成功后 ok/user 正确", sd.user)
    check("已切换为" in sd.var_status.get(), "8.9 成功有明确提示",
          sd.var_status.get())
    check(sd.btn_main.cget("text") == "完成", "8.10 成功后按钮变「完成」")

    sd2 = g.SwitchAccountDialog(root, "logout")
    sd2._closed = True
    check(sd2.top.title() == "退出网易云登录", "8.11 退出登录窗标题",
          sd2.top.title())
    sd2._finish(False, "", "启动浏览器失败")
    check((not sd2.ok) and "❌" in sd2.var_status.get(),
          "8.12 失败时 ok=False 且状态里带 ❌", sd2.var_status.get())

    for _sd in (sd, sd2):
        try:
            _sd.top.destroy()
        except tk.TclError:
            pass

    # ---- 9. CloudUploader 的「核对」（v4.27）-------------------------------
    # 现场事故：日志「✅ 已上传」，云盘里却没有。原因是跳过看的是
    # upload/check 的 needUpload（云盘列表才是唯一判据），而 publish 201
    # 又被当成失败。这里用假会话把三条行为锁死，不碰网络。
    class _FakeSess:
        def __init__(self, cloud=(), lie=False, fix_on_retry=False):
            self.cloud = set(cloud)
            self.lie = lie
            self.fix_on_retry = fix_on_retry
            self.calls = []
            self.q = 0

        def cloud_md5s(self):
            self.q += 1
            return set(self.cloud)

        def upload_flac(self, path, **kw):
            name = os.path.basename(path)
            md5 = name[:-5].lower() if name.lower().endswith(".flac") else name.lower()
            self.calls.append((name, bool(kw.get("force"))))
            known = kw.get("known_md5")
            if known is not None and md5 in known and not kw.get("force"):
                return {"ok": True, "skipped": True, "songid": 0, "md5": md5,
                        "in_cloud": True}
            if self.lie and not (self.fix_on_retry and kw.get("force")):
                return {"ok": True, "skipped": False, "songid": 1, "md5": md5}
            self.cloud.add(md5)
            return {"ok": True, "skipped": False, "songid": 1, "md5": md5}

    _real_flac = g.ncm.wav_to_flac

    def _fake_flac(src, *a, **k):
        dst = src + ".flac"
        with open(dst, "wb") as fh:
            fh.write(b"fLaC")
        return True, dst, "fake"

    g.ncm.wav_to_flac = _fake_flac
    try:
        def _run(sess, files):
            ev = []
            cu = g.CloudUploader(sess, lambda k, p=None: ev.append((k, p)))
            cu.run(files)
            logs = [p for k, p in ev if k == "log"]
            done = [p for k, p in ev if k == "done"]
            return ev, logs, done

        a, b = wavs1[0], wavs1[1]
        # 9.1 批次开始读云盘清单（真跳过的唯一依据）
        s9 = _FakeSess()
        _ev, logs, done = _run(s9, [a])
        check(any("云盘现有" in str(x) for x in logs),
              "9.1 开传前先拉云盘清单", " / ".join(str(x) for x in logs[:3]))

        # 9.2 清单里已有 → 跳过，且不进入上传
        s9b = _FakeSess(cloud={os.path.basename(a).lower()})
        ev9b, logs9b, done9b = _run(s9b, [a])
        check(s9b.calls and s9b.calls[0][1] is False,
              "9.2 命中清单才算跳过（仍交给 upload_flac 判）",
              str(s9b.calls))
        check(done9b and done9b[0] == (0, 1, 0), "9.3 跳过计入「跳过」而非成功",
              str(done9b))

        # 9.4 上传后被声称成功 → 核对能发现「云盘里没有」，并重试
        s9c = _FakeSess(lie=True)
        ev9c, logs9c, done9c = _run(s9c, [a])
        check(any("核对" in str(x) for x in logs9c),
              "9.4 传完会核对云盘列表", " / ".join(str(x) for x in logs9c))
        check(any("重试" in str(x) for x in logs9c), "9.5 缺失的会重试一次")
        check(any(f is True for _n, f in s9c.calls), "9.6 重试用 force=True")
        check(done9c and done9c[0][0] == 0 and done9c[0][2] == 1,
              "9.7 最终没进云盘的一首从成功数里扣掉、计失败", str(done9c))

        # 9.8 重试后进了云盘 → 仍算成功
        s9d = _FakeSess(lie=True, fix_on_retry=True)
        _ev9d, logs9d, done9d = _run(s9d, [a])
        check(done9d and done9d[0] == (1, 0, 0),
              "9.8 重试后核对通过仍算成功", str(done9d))

        # 9.9 正常路径：核对通过会明说
        s9e = _FakeSess()
        _ev9e, logs9e, done9e = _run(s9e, [a, b])
        check(any("核对通过" in str(x) for x in logs9e),
              "9.9 核对通过有明确日志", " / ".join(str(x) for x in logs9e))
        check(done9e and done9e[0] == (2, 0, 0), "9.10 两首都算成功", str(done9e))

        # 9.11 取消时不发请求（连清单都不拉）
        s9f = _FakeSess()
        cu9 = g.CloudUploader(s9f, lambda k, p=None: None)
        cu9.cancel = __import__("threading").Event()
        cu9.cancel.set()
        cu9.run([a])
        check(s9f.q == 0 and not s9f.calls, "9.11 已取消时一个请求都不发")
    finally:
        g.ncm.wav_to_flac = _real_flac

    dd._closed = True                     # 先置位，免得 _shutdown 里的对话框
    dd._shutdown()
    shutil.rmtree(tmpdir, ignore_errors=True)
    root.update()
    root.destroy()

    bad = RESULTS.count(False)
    print("-" * 56)
    print("结果: %d 通过 / %d 失败" % (RESULTS.count(True), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
