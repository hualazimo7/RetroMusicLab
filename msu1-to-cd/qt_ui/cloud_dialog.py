#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qt_ui.cloud_dialog — 「上传到网易云云盘」窗（Qt 版）。

## 与 tkinter 版的关系

上传逻辑**不重写**：``cloud_upload.CloudUploader`` 一个 UI 对象都不碰，
这里只是给它套一层 Qt 外壳（线程 + 信号 + 日志区）。

登录态**也不重写**：``netease_cloud.NeteaseSession`` / ``netease_cookie``
已经把所有网络细节封装好了。三档降级顺序与 tkinter 版一致：

    ① 本地存的 cookie → refresh_profile 真拉一次账号信息
    ② 专属 profile 里有登录态 → quick_cookie()（静默，0.01~3s）
    ③ 都没有 → 开浏览器让用户登录，或直接粘贴 Cookie

## 没有搬过来的东西

tkinter 版登录窗的第一页是**扫码登录**（把 qr_new/qr_check 返回的矩阵画成
二维码）。Qt 版没做这一页 —— 扫码本质是「手机上有网易云 App，电脑上没有
浏览器」的场景，而 ②③ 两档已经覆盖了绝大多数人。要补的话，在
``_login_browser`` 旁边加一个 ``_login_qr`` 即可，接口现成。

## 线程纪律

三个 QThread，一个都不碰控件：

* ``LoginThread``   —— 拿 session（可能开浏览器，最久 5 分钟）
* ``UploadThread``  —— CloudUploader.run()
* 信号统一走 ``log`` / ``prog`` / ``done`` / ``failed``
"""

import os
import threading

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

import cloud_upload as cu

try:
    import netease_cloud as ncm
except Exception:                                    # noqa: BLE001
    ncm = None

try:
    import netease_cookie as nck
except Exception:                                    # noqa: BLE001
    nck = None

_MISSING_MSG = ("缺少 netease_cloud 依赖（需要 requests）。\n"
                "请用打包好的 exe，或在源码目录 pip install requests。")


def account_label():
    """当前保存的账号昵称 —— **只从本地文件读，不联网**（菜单/窗口要秒开）。"""
    if ncm is None:
        return ""
    try:
        return str((ncm.load_cookie() or {}).get("user_name") or "")
    except Exception:                                # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# 登录线程
# ---------------------------------------------------------------------------
class LoginThread(QThread):
    """静默恢复登录态。不弹任何窗，失败就把原因交回去。"""

    ok = Signal(object)
    failed = Signal(str)

    def run(self):
        if ncm is None:
            self.failed.emit("缺少 netease_cloud 依赖")
            return
        try:
            sess = ncm.NeteaseSession()
            sess.loads(ncm.load_cookie())
            if sess.is_logged_in():
                try:
                    if sess.refresh_profile():
                        self.ok.emit(sess)
                        return
                except Exception:                    # noqa: BLE001
                    pass
            if nck is not None and nck.has_profile():
                header = nck.quick_cookie()
                if header:
                    good, msg = sess.login_with_cookie(header)
                    if good:
                        try:
                            ncm.save_cookie(sess.dumps())
                        except Exception:            # noqa: BLE001
                            pass
                        self.ok.emit(sess)
                        return
                    self.failed.emit("浏览器登录态已失效（%s）" % msg)
                    return
            self.failed.emit("尚未登录")
        except Exception as e:                        # noqa: BLE001
            self.failed.emit(str(e))


class BrowserLoginThread(QThread):
    """开浏览器 → 等用户登录 → 取 cookie → 核验。最久等 5 分钟。"""

    ok = Signal(object)
    status = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        if nck is None or ncm is None:
            self.failed.emit("登录模块不可用")
            return
        name, exe = nck.find_browser()
        if not exe:
            self.failed.emit("没找到 Chrome / Edge，请改用「粘贴 Cookie」")
            return
        self.status.emit("正在启动 %s…" % name)
        bl = nck.BrowserLogin(silent=False)
        try:
            if not bl.start():
                self.failed.emit(bl.error or "浏览器启动失败")
                return
            self.status.emit("浏览器已打开，请在其中完成登录…")
            try:
                header = bl.wait_cookie(timeout=300, interval=1.2,
                                        stop=lambda: self._stop)
            except RuntimeError as e:
                if not self._stop:
                    self.failed.emit(str(e)[:100])
                return
            if not header:
                if not self._stop:
                    self.failed.emit("等了 5 分钟还没登录，已放弃")
                return
            self.status.emit("已取到登录凭据，正在核验…")
            sess = ncm.NeteaseSession()
            good, msg = sess.login_with_cookie(header)
            if not good:
                self.failed.emit(msg)
                return
            try:
                ncm.save_cookie(sess.dumps())
            except Exception:                        # noqa: BLE001
                pass
            self.ok.emit(sess)
        except Exception as e:                        # noqa: BLE001
            self.failed.emit("出错：%s" % str(e)[:100])
        finally:
            bl.close()


class CookieLoginThread(QThread):
    """核验用户粘贴的 Cookie 串。"""

    ok = Signal(object)
    failed = Signal(str)

    def __init__(self, raw, parent=None):
        super().__init__(parent)
        self.raw = raw

    def run(self):
        try:
            sess = ncm.NeteaseSession()
            good, msg = sess.login_with_cookie(self.raw)
            if not good:
                self.failed.emit(msg)
                return
            try:
                ncm.save_cookie(sess.dumps())
            except Exception:                        # noqa: BLE001
                pass
            self.ok.emit(sess)
        except Exception as e:                        # noqa: BLE001
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# 上传线程
# ---------------------------------------------------------------------------
class UploadThread(QThread):
    log = Signal(str)
    prog = Signal(int)
    done = Signal(int, int, int)
    failed = Signal(str)

    def __init__(self, sess, wavs, parent=None):
        super().__init__(parent)
        self.sess = sess
        self.wavs = list(wavs)
        self.cancel = threading.Event()

    def run(self):
        def emit(kind, payload=None):
            if kind == "log":
                self.log.emit(str(payload))
            elif kind == "prog":
                self.prog.emit(int(payload or 0))
            elif kind == "done":
                ok, skip, fail = payload
                self.done.emit(int(ok), int(skip), int(fail))

        try:
            cu.CloudUploader(self.sess, emit, self.cancel).run(self.wavs)
        except Exception as e:                        # noqa: BLE001
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------
class CloudUploadDialog(QDialog):
    """选目录 → 选范围 → 登录 → 上传。"""

    def __init__(self, out_dir="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("上传到网易云云盘")
        self.setMinimumSize(760, 560)
        self.sess = None
        self.wavs = []
        self._login = None
        self._browser = None
        self._cookie = None
        self._up = None
        self.out_dir = out_dir
        #: 合并文件与分轨是否**同时**存在（决定要不要让用户选范围）。
        #: ⚠ 不能用 ``_rb_row.isVisible()`` 判 —— 窗口还没 show 时它恒为 False，
        #: 上传范围就会静默退回「全都传」（门禁抓到过）。
        self._has_both = False
        self._build()
        self._collect()
        self._auto_login()

    # ------------------------------------------------------------------ 构建
    def _build(self):
        col = QVBoxLayout(self)
        col.setContentsMargins(20, 18, 20, 16)
        col.setSpacing(10)

        head = QHBoxLayout()
        self.lb_acct = QLabel("账号：读取中…")
        self.lb_acct.setObjectName("h2")
        head.addWidget(self.lb_acct, 1)
        self.b_browser = QPushButton("浏览器登录…")
        self.b_cookie = QPushButton("粘贴 Cookie…")
        for b in (self.b_browser, self.b_cookie):
            head.addWidget(b)
        col.addLayout(head)

        self.lb_scope = QLabel("")
        self.lb_scope.setObjectName("muted")
        self.lb_scope.setWordWrap(True)
        col.addWidget(self.lb_scope)

        self.rb_merged = QRadioButton("只传合并文件 — 整张连着听")
        self.rb_tracks = QRadioButton("只传分轨 — 云盘里能单曲播放")
        self.rb_both = QRadioButton("全都传 — 内容会重复")
        self._rb_row = QWidget()
        rr = QHBoxLayout(self._rb_row)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.setSpacing(18)
        for b in (self.rb_merged, self.rb_tracks, self.rb_both):
            rr.addWidget(b)
        rr.addStretch(1)
        col.addWidget(self._rb_row)
        self._rb_row.setVisible(False)

        self.te = QPlainTextEdit()
        self.te.setObjectName("log")
        self.te.setReadOnly(True)
        col.addWidget(self.te, 1)

        self.pb = QProgressBar()
        self.pb.setRange(0, 100)
        self.pb.setValue(0)
        self.pb.setTextVisible(False)
        self.pb.setFixedHeight(8)
        col.addWidget(self.pb)

        row = QHBoxLayout()
        self.lb_state = QLabel("准备中…")
        self.lb_state.setObjectName("status")
        row.addWidget(self.lb_state, 1)
        self.b_go = QPushButton("开始上传")
        self.b_go.setObjectName("primary")
        self.b_go.setEnabled(False)
        self.b_close = QPushButton("关闭")
        row.addWidget(self.b_go)
        row.addWidget(self.b_close)
        col.addLayout(row)

        self.b_go.clicked.connect(self._start_upload)
        self.b_close.clicked.connect(self.reject)
        self.b_browser.clicked.connect(self._login_browser)
        self.b_cookie.clicked.connect(self._login_cookie)
        self.rb_merged.toggled.connect(lambda _v: self._refresh_scope())

    # ------------------------------------------------------------------ 清单
    def _collect(self):
        if not self.out_dir or not os.path.isdir(self.out_dir):
            self.lb_scope.setText("⚠ 输出目录还不存在，先转换一次再来上传")
            return
        all_wavs = cu.collect_wavs(self.out_dir)
        if not all_wavs:
            self.lb_scope.setText("⚠ 输出目录里没有 WAV：%s" % self.out_dir)
            return
        merged, tracks = cu.split_merged(all_wavs)
        self.wavs = all_wavs
        if merged and tracks:
            # 两种形态并存才让用户选 —— 只传一种的话不打扰
            self.lb_scope.setText(
                "这个目录里既有合并文件，也有分轨：\n"
                "· 合并文件 %d 个，共 %s（每个源一个整轨）\n"
                "· 分轨　　 %d 个，共 %s（每首一个文件）\n"
                "两种内容基本重复（合并文件就是这些分轨拼起来的），"
                "全传等于占双倍空间。"
                % (len(merged), cu.fmt_bytes(cu.sum_size(merged)),
                   len(tracks), cu.fmt_bytes(cu.sum_size(tracks))))
            self.rb_merged.setText("只传合并文件（%d 个）— 整张连着听" % len(merged))
            self.rb_tracks.setText("只传分轨（%d 个）— 云盘里能单曲播放" % len(tracks))
            self.rb_both.setText("全都传（%d 个）— 内容会重复"
                                 % (len(merged) + len(tracks)))
            self.rb_merged.setChecked(True)          # 合并是用户自己勾出来的显式意图
            self._rb_row.setVisible(True)
            self._has_both = True
            self._merged, self._tracks = merged, tracks
        else:
            self._merged, self._tracks = merged, tracks
            self._has_both = False
            self.lb_scope.setText("待上传 %d 个 WAV，共 %s"
                                  % (len(all_wavs), cu.fmt_bytes(
                                      cu.sum_size(all_wavs))))

    def _picked(self):
        if not self._has_both:
            return list(self.wavs)
        if self.rb_tracks.isChecked():
            return list(self._tracks)
        if self.rb_both.isChecked():
            return list(self._merged) + list(self._tracks)
        return list(self._merged)

    def _refresh_scope(self):
        picked = self._picked()
        self.lb_state.setText("待上传 %d 个，共 %s（预计占云盘 %s）"
                              % (len(picked), cu.fmt_bytes(cu.sum_size(picked)),
                                 cu.fmt_bytes(cu.sum_size(picked) * 0.4)))

    # ------------------------------------------------------------------ 登录
    def _auto_login(self):
        if ncm is None:
            self.lb_acct.setText("账号：❌ 缺少依赖")
            self.log(_MISSING_MSG)
            return
        name = account_label()
        self.lb_acct.setText("账号：%s" % (name or "（读取中…）"))
        self._login = LoginThread(self)
        self._login.ok.connect(self._on_login)
        self._login.failed.connect(self._on_login_fail)
        self._login.start()

    def _on_login(self, sess):
        self.sess = sess
        self.lb_acct.setText("账号：%s" % (sess.user_name or "（已登录）"))
        self.log("☁ 已登录：%s" % (sess.user_name or "（未取到昵称）"))
        self.b_go.setEnabled(bool(self._picked()))
        self._refresh_scope()

    def _on_login_fail(self, msg):
        self.lb_acct.setText("账号：未登录")
        self.log("☁ %s —— 请用「浏览器登录」或「粘贴 Cookie」" % msg)
        self.lb_state.setText("未登录，无法上传")

    def _login_browser(self):
        if self._browser is not None and self._browser.isRunning():
            return
        self.b_browser.setEnabled(False)
        self.lb_state.setText("正在打开浏览器…")
        self._browser = BrowserLoginThread(self)
        self._browser.status.connect(self.lb_state.setText)
        self._browser.ok.connect(self._on_login)
        self._browser.failed.connect(self._on_browser_fail)
        self._browser.start()

    def _on_browser_fail(self, msg):
        self.b_browser.setEnabled(True)
        self.lb_state.setText("登录失败")
        self.log("❌ 浏览器登录失败：%s" % msg)

    def _login_cookie(self):
        from PySide6.QtWidgets import QInputDialog
        raw, ok = QInputDialog.getMultiLineText(
            self, "粘贴 Cookie",
            "在浏览器里登录 music.163.com 后，从开发者工具复制完整 Cookie 串：", "")
        if not ok or not raw.strip():
            return
        self.lb_state.setText("正在核验…")
        self._cookie = CookieLoginThread(raw.strip(), self)
        self._cookie.ok.connect(self._on_login)
        self._cookie.failed.connect(
            lambda m: (self.lb_state.setText("核验失败"),
                       self.log("❌ Cookie 核验失败：%s" % m)))
        self._cookie.start()

    # ------------------------------------------------------------------ 上传
    def _start_upload(self):
        if self.sess is None:
            QMessageBox.warning(self, "还没登录", "请先登录网易云账号。")
            return
        wavs = self._picked()
        if not wavs:
            QMessageBox.warning(self, "没有可上传的文件", "当前选择是空的。")
            return
        need_mb = cu.sum_size(wavs) / 1e6 * 0.4
        ok = QMessageBox.question(
            self, "确认上传",
            "将把 %d 个 WAV 转成 FLAC 上传到你的网易云云盘。\n\n"
            "预计占用约 %.0f MB。\n"
            "免费额度通常 5 GB，超出需要会员扩容。\n\n"
            "注意：平台已有版权的曲目可能被拒绝或匹配成平台版本。\n\n继续？"
            % (len(wavs), need_mb))
        if ok != QMessageBox.Yes:
            return
        self.b_go.setEnabled(False)
        self.pb.setValue(0)
        self.lb_state.setText("上传中…")
        self._up = UploadThread(self.sess, wavs, self)
        self._up.log.connect(self.log)
        self._up.prog.connect(self.pb.setValue)
        self._up.done.connect(self._on_up_done)
        self._up.failed.connect(self._on_up_fail)
        self._up.start()

    def _on_up_done(self, ok, skip, fail):
        self.b_go.setEnabled(True)
        self.pb.setValue(100)
        self.lb_state.setText("完成 · 成功 %d / 跳过 %d / 失败 %d"
                              % (ok, skip, fail))
        self.log("=" * 50)
        self.log("☁ 上传结束：成功 %d，跳过 %d，失败 %d" % (ok, skip, fail))

    def _on_up_fail(self, msg):
        self.b_go.setEnabled(True)
        self.lb_state.setText("上传失败")
        self.log("❌ 上传失败：%s" % msg)

    def log(self, text):
        self.te.appendPlainText(str(text))

    # ------------------------------------------------------------------ 收尾
    def closeEvent(self, ev):
        if self._up is not None and self._up.isRunning():
            self._up.cancel.set()
            self._up.wait(2500)
        if self._browser is not None and self._browser.isRunning():
            self._browser.stop()
            self._browser.wait(2500)
        ev.accept()
