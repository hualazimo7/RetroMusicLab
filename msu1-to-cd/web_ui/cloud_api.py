#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cloud_api.py — 网易云云盘：账号 / 登录 / 上传清单（**零 UI**）。

tkinter 版把这一整套长在 ``msu1_gui.py`` 里（``NeteaseLoginDialog`` 三页 +
``SwitchAccountDialog`` + ``ensure_cloud_session`` + ``plan_cloud_upload``），
Web 版不能 import 它（会把 tkinter 整个拖进打包），所以这里按**同一个口径**
重写成一层无界面的状态机。

## 为什么是「状态机 + 轮询」而不是同步接口

浏览器登录要等用户在里面扫码/输密码，**最长 4 分钟**。HTTP 请求扛不住这么长
的同步等待，所以 ``start_browser()`` 立刻返回，工作线程在后台跑，前端每
0.5 秒 ``GET /api/cloud/status`` 看 ``job`` 走到哪一步。

## 三条登录路径（顺序即推荐度）

1. **浏览器登录** —— 开一个专属 profile 的真浏览器，用户在里面正常登录，
   我们用 DevTools Protocol 把浏览器**已经持有**的 cookie 取走。走的是官方
   网页流程，不触发 8821 风控。**登录一次，之后长期复用。**
2. **粘贴 Cookie** —— 用户自己从 F12 请求头里复制。浏览器登录不可用时兜底。
3. **扫码** —— 申请 unikey → 前端画二维码 → 轮询 ``qr_check``。
   ⚠ 2024 年后网易云给网页扫码加了 **8821 行为验证码**，手机上点确认会被拒，
   所以这一页要明说「可能被风控」，撞上了自动切到第 2 页。

## 静默恢复（「只需登录一次」的关键）

``ensure()`` 先看存盘的 cookie 还能不能拉到账号信息；不能就试着从内置浏览器
profile 里静默取一次（headless，约 1 秒）。两条都失败才让前端弹登录窗 ——
用户第二次点上传时看到的应该是直接开始传，而不是又弹一个登录框。
"""

from __future__ import annotations

import os
import threading
import time

__all__ = [
    "CloudAuth", "auth", "deps", "qr_matrix", "plan_upload", "pick_scope",
    "UPLOAD_FMTS",
    "LOGIN_URL",
]

LOGIN_URL = "https://music.163.com/#/login"

#: 浏览器登录最多等多久（秒）。用户要扫码或输密码，4 分钟够用。
BROWSER_TIMEOUT = 300.0

#: 扫码轮询的最长寿命（秒）—— 二维码本身 5 分钟过期，留点余量。
QR_TIMEOUT = 300.0


# ---------------------------------------------------------------------------
# 依赖探测
# ---------------------------------------------------------------------------
def deps() -> dict:
    """云盘功能要的三个第三方包，缺哪个就只说哪个。

    * ``soundfile``  → WAV 转 FLAC（libsndfile）
    * ``Crypto``     → weapi 的 AES / RSA 参数加密（pycryptodome）
    * ``segno``      → 二维码矩阵（纯 Python，不用 PIL）

    前两个缺了**根本传不了**；segno 缺了只影响「扫码」这一页，
    浏览器登录和粘贴 Cookie 照常可用 —— 所以分两档报。
    """
    out = {}
    for name, mod in (("soundfile", "soundfile"),
                      ("Crypto", "Crypto"),
                      ("segno", "segno")):
        try:
            __import__(mod)
            out[name] = True
        except Exception:                        # noqa: BLE001
            out[name] = False
    return out


def _ncm():
    """惰性拿 ``netease_cloud``（打包时是 include-module，一定在）。"""
    import netease_cloud
    return netease_cloud


def _nck():
    """惰性拿 ``netease_cookie``；不可用返回 None（只影响浏览器登录）。"""
    try:
        import netease_cookie
        return netease_cookie
    except Exception:                            # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 二维码矩阵
# ---------------------------------------------------------------------------
def qr_matrix(text: str):
    """文本 → ``(边长, 行字符串列表)``，四周含 4 格 quiet zone。

    ⚠ 用 **segno** 而不是 ``qrcode``：后者一 import 就会把 PIL 拉进来，
    实测让 exe 多出 6.3MB（还连带 lxml 3MB）—— 而我们只要矩阵、不画图片。
    segno 是纯 Python 零依赖，出矩阵只要 ``qr.matrix``。

    quiet zone 不能省：四周不留白，手机扫码器对不上焦。

    返回行字符串（``"0101…"``）而不是 ``list[list[int]]``：同样的信息量，
    JSON 体积小一半，前端画的时候按下标取字符即可。
    """
    import segno
    qr = segno.make(text, error="m")
    body = [[1 if v else 0 for v in row] for row in qr.matrix]
    n = len(body)
    size = n + 8                                 # 四周各 4 格
    rows = []
    for y in range(size):
        if y < 4 or y >= n + 4:
            rows.append("0" * size)
            continue
        line = body[y - 4]
        rows.append("0000" + "".join("1" if v else "0" for v in line) + "0000")
    return size, rows


# ---------------------------------------------------------------------------
# 上传清单规划（纯函数，与界面无关）
# ---------------------------------------------------------------------------
def plan_upload(wavs):
    """把一批 WAV 分成「合并 / 分轨」，并判断两者是否内容重复。

    为什么需要这一层：v4.24 加了「多轨合并」后，``collect_wavs`` 只看扩展名，
    会把 ``X（合并）.wav`` 和它的 44 个分轨**一起**收进上传列表。合并文件就是
    那些分轨拼出来的，全传等于把同一张专辑传两遍，云盘空间直接翻倍。

    返回的 ``near`` 是「两种内容基本重复」的判据：字节数相差不超过 25%
    （合并多出的那部分是曲间静音 0.5s × N）。差得远说明只有部分源做了合并，
    那种情况下不能说「重复」，得让用户自己决定。
    """
    import cloud_upload as cu

    merged, tracks = cu.split_merged(list(wavs or ()))
    m_bytes, t_bytes = cu.sum_size(merged), cu.sum_size(tracks)
    near = (m_bytes > 0 and t_bytes > 0
            and abs(m_bytes - t_bytes) <= max(m_bytes, t_bytes) * 0.25)

    def rows(lst):
        return [{"path": p, "name": os.path.basename(p),
                 "size": _size(p)} for p in lst]

    need_choose = bool(merged) and bool(tracks)
    if need_choose:
        hint = ("两种内容基本重复（合并文件就是这些分轨拼起来的），"
                "全传等于占双倍空间。" if near else
                "合并文件与分轨的体量对不上（可能只有部分源做了合并），"
                "请按需要选。")
    else:
        hint = ""
    # 体积估算按「容器」给（v6.0.1）：用户要能在 FLAC / MP3 之间选，选中哪个
    # 就得报哪个的数。系数统一从 ``dl_convert.FMT_BYTES_RATIO`` 取 —— 那里
    # 是下载页预估磁盘占用用的同一张表，两处各写一份必然会漂移（FLAC 0.40 /
    # MP3 0.24 都是实测值，见该文件注释）。
    est = _est_mb(m_bytes + t_bytes)
    # 按范围分别估（合并 / 分轨 / 全传）。为什么不由前端按比例算：
    # 前端算出来的数会和「真的选中后传上去的大小」对不上，而用户是拿这个数
    # 判断「我的 5 GB 够不够」的 —— 估算错一次就没人再信这个提示了。
    est_scope = {"merged": _est_mb(m_bytes),
                 "tracks": _est_mb(t_bytes),
                 "all": est}
    return {
        "ok": True,
        "merged": rows(merged),
        "tracks": rows(tracks),
        "m_bytes": m_bytes,
        "t_bytes": t_bytes,
        "m_text": _human(m_bytes),
        "t_text": _human(t_bytes),
        "near": near,
        "need_choose": need_choose,
        "hint": hint,
        # 全传时的体积估算（``est`` 是按格式的字典，``est_mb`` 是老的 FLAC 值，
        # 保留给还没更新的调用方）。
        "est": est,
        "est_scope": est_scope,
        "est_mb": est.get("flac", 0.0),
    }


#: 上传可选容器。与 ``cloud_upload.UPLOAD_FMTS`` 必须一致。
UPLOAD_FMTS = ("flac", "mp3")


def _est_mb(nbytes):
    """``{"flac": x, "mp3": y}``，单位 MB。系数取自 ``dl_convert``。"""
    try:
        from dl_convert import FMT_BYTES_RATIO
    except Exception:                            # noqa: BLE001
        FMT_BYTES_RATIO = {"flac": 0.40, "mp3": 0.24}
    out = {}
    for f in UPLOAD_FMTS:
        r = float(FMT_BYTES_RATIO.get(f) or 0.40)
        out[f] = round(max(0, nbytes) / 1e6 * r, 1)
    return out


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def pick_scope(wavs, scope="", merged=None):
    """按「范围」挑出要传的 WAV，返回 ``(列表, 空时的原因)``。

    ``scope`` 取 ``merged`` / ``tracks`` / ``all``；``merged`` 是**老参数**
    （布尔），外部脚本还在用，继续认。

    **两个都没给**时按老行为走：两种都有就只传分轨（合并文件就是它们拼出来
    的，全传等于传两遍），只有一种就全传 —— 这样「目录里只有合并文件」时
    不会挑出空列表然后报一句莫名其妙的「这个范围里没有可上传的文件」。
    """
    import cloud_upload as cu

    m, t = cu.split_merged(list(wavs or ()))
    s = str(scope or "").strip().lower()
    if s not in ("merged", "tracks", "all"):
        if merged is not None:
            s = "merged" if merged else "tracks"
        else:
            s = "tracks" if (m and t) else "all"
    if s == "merged":
        return list(m), "目录里没有合并文件，请改选「只传分轨」或「全都传」"
    if s == "tracks":
        return list(t), "目录里没有分轨，请改选「只传合并文件」或「全都传」"
    return list(m) + list(t), ""


def _human(n):
    if n >= 1e9:
        return "%.2f GB" % (n / 1e9)
    if n >= 1e6:
        return "%.1f MB" % (n / 1e6)
    return "%.1f KB" % (n / 1e3)


# ---------------------------------------------------------------------------
# 登录状态机
# ---------------------------------------------------------------------------
class CloudAuth:
    """网易云账号的唯一入口。一个进程一个实例（``server.cloud_auth()``）。

    ⚠ 所有字段都在 ``self.lock`` 里读写。工作线程只改状态、不碰 UI ——
    Web 版没有 UI 可碰，但「只改状态、由前端来读」这条线不能破。
    """

    def __init__(self):
        self.lock = threading.RLock()
        #: 进行中的后台任务（浏览器登录 / 换号 / 退出）。None = 空闲
        self._job = None
        self._stop = threading.Event()
        self._bl = None                 # 正在跑的 BrowserLogin
        #: 扫码状态
        self._qr = {"key": "", "n": 0, "rows": [], "msg": "",
                    "code": 0, "at": 0.0, "busy": False}
        self._qr_busy = False

    # -- 小工具 ------------------------------------------------------------
    def _session(self, save_data=None):
        """按存盘 cookie 造一个 session（**不联网**）。"""
        ncm = _ncm()
        sess = ncm.NeteaseSession()
        data = save_data if save_data is not None else ncm.load_cookie()
        sess.loads(data or {})
        return sess

    def _save(self, sess):
        try:
            _ncm().save_cookie(sess.dumps())
        except Exception:                        # noqa: BLE001 存不下不影响本次
            pass

    def _set_job(self, **kw):
        with self.lock:
            if self._job is None:
                self._job = {}
            self._job.update(kw)

    # -- 状态 --------------------------------------------------------------
    def status(self) -> dict:
        """给前端轮询的一份完整快照。**绝不联网**（前端 0.5 秒拉一次）。

        登录态只从 ``netease.json`` 里的 ``MUSIC_U`` 判断 —— 那是「上次确实
        登录成功过」的证据。**是否还有效**要等 ``ensure()`` 真拉一次账号信息，
        两者不是一回事，别在这里加网络请求。
        """
        d = deps()
        nck = _nck()
        browser, exe = ("", "")
        has_profile = False
        if nck is not None:
            try:
                browser, exe = nck.find_browser()
                browser = browser or ""
                exe = exe or ""
                has_profile = bool(nck.has_profile())
            except Exception:                    # noqa: BLE001
                pass

        logged_in, name, uid = False, "", 0
        try:
            ncm = _ncm()
            data = ncm.load_cookie() or {}
            name = str(data.get("user_name") or "")
            uid = int(data.get("user_id") or 0)
            sess = self._session(data)
            logged_in = bool(sess.is_logged_in())
        except Exception:                        # noqa: BLE001
            pass

        can_upload = d["soundfile"] and d["Crypto"]
        can_browser = bool(exe) and nck is not None
        with self.lock:
            job = dict(self._job) if self._job else None
            qr = {k: v for k, v in self._qr.items() if k != "rows"}
            qr["has_qr"] = bool(self._qr.get("rows"))

        why = ""
        if not can_upload:
            miss = [k for k in ("soundfile", "Crypto") if not d[k]]
            why = "缺少依赖：%s（pip install soundfile pycryptodome）" % "、".join(miss)
        elif not logged_in:
            why = "还没登录网易云云盘"
        return {
            "ok": True,
            "logged_in": logged_in,
            "user_name": name,
            "user_id": uid,
            "has_profile": has_profile,
            "browser": browser,
            "browser_exe": exe,
            "can_browser": can_browser,
            "can_upload": can_upload,
            "deps": d,
            "job": job,
            "qr": qr,
            "why": why,
            "hint": ("浏览器登录最省事：会开一个独立窗口，在里面正常登录一次，"
                     "之后每次上传都自动复用。"),
        }

    # -- 上传前确保登录 ----------------------------------------------------
    def ensure(self, log=None) -> dict:
        """拿到一个**真能拉到账号信息**的 session。

        返回 ``{"ok": True, "session": sess}`` 或
        ``{"ok": False, "need_login": True, "error": "..."}``。

        ⚠ 只看 jar 里有没有 ``MUSIC_U`` 是不够的 —— 存下来的 Cookie 可能早
        过期，那样会一路走到上传才炸（v4.20 踩过）。所以这里真拉一次
        ``refresh_profile``。

        ⚠ 静默恢复那一步会起一个 headless 浏览器（约 1 秒）。只在
        ``has_profile()`` 为真时才试 —— profile 都没建过，里面必然没有登录态，
        起了也是白等。
        """
        def say(msg):
            if log is not None:
                try:
                    log(msg)
                except Exception:                # noqa: BLE001
                    pass

        try:
            ncm = _ncm()
        except Exception as exc:                 # noqa: BLE001
            return {"ok": False, "need_login": False,
                    "error": "云盘模块不可用：%s" % exc}

        d = deps()
        if not (d["soundfile"] and d["Crypto"]):
            miss = [k for k in ("soundfile", "Crypto") if not d[k]]
            return {"ok": False, "need_login": False,
                    "error": "云盘上传需要 %s。\n\npip install soundfile pycryptodome"
                             % "、".join(miss)}

        sess = self._session()
        authed = False
        if sess.is_logged_in():
            try:
                authed = bool(sess.refresh_profile())
            except Exception:                    # noqa: BLE001
                authed = False
        if authed:
            say("☁ 已登录：%s" % (sess.user_name or "（未取到昵称）"))
            return {"ok": True, "session": sess}

        nck = _nck()
        if nck is not None and nck.has_profile():
            say("☁ 正在恢复登录态…")
            header = ""
            try:
                header = nck.quick_cookie() or ""
            except Exception:                    # noqa: BLE001
                header = ""
            if header:
                try:
                    ok, _msg = sess.login_with_cookie(header)
                except Exception:                # noqa: BLE001
                    ok = False
                if ok:
                    self._save(sess)
                    say("☁ 已登录：%s" % (sess.user_name or "（未取到昵称）"))
                    return {"ok": True, "session": sess}

        say("☁ 需要登录网易云")
        return {"ok": False, "need_login": True,
                "error": "还没登录网易云云盘，请先登录一次"}

    # -- 浏览器登录 --------------------------------------------------------
    def start_browser(self) -> dict:
        """开一个**可见**的浏览器让用户登录。立刻返回，进度靠 ``status()``。"""
        nck = _nck()
        if nck is None:
            return {"ok": False, "error": "netease_cookie 模块不可用"}
        name, exe = nck.find_browser()
        if not exe:
            return {"ok": False,
                    "error": "没找到 Chrome / Edge，请改用「粘贴 Cookie」"}
        with self.lock:
            if self._job and self._job.get("running"):
                return {"ok": False, "error": "已经有一个登录流程在跑"}
            self._stop = threading.Event()
            self._job = {"kind": "browser", "running": True, "state": "starting",
                         "msg": "正在启动 %s…" % (name or "浏览器"),
                         "error": "", "user": "", "browser": name or "",
                         "started": time.time()}
        threading.Thread(target=self._browser_worker, daemon=True).start()
        return {"ok": True, "browser": name or ""}

    def _browser_worker(self):
        """后台：开浏览器 → 等登录 → 取 cookie → 验证 → 存盘 → 关浏览器。"""
        nck = _nck()
        bl = None
        try:
            bl = nck.BrowserLogin(silent=False)
            with self.lock:
                self._bl = bl
            if not bl.start():
                self._fail(bl.error or "浏览器启动失败")
                return
            if self._stop.is_set():
                self._cancel_job()
                return
            self._set_job(state="waiting",
                          msg="浏览器已打开，请在其中完成登录…")
            try:
                header = bl.wait_cookie(timeout=BROWSER_TIMEOUT, interval=1.2,
                                        stop=lambda: self._stop.is_set())
            except RuntimeError as exc:
                if self._stop.is_set():
                    self._cancel_job()
                else:
                    self._fail(str(exc)[:120])
                return
            if not header:
                if self._stop.is_set():
                    self._cancel_job()
                else:
                    self._fail("等了 %d 分钟还没登录，已放弃"
                               % int(BROWSER_TIMEOUT // 60))
                return
            self._set_job(state="verifying", msg="已取到登录凭据，正在向网易云核验…")
            self._finish_login(header)
        except Exception as exc:                 # noqa: BLE001
            self._fail("出错：%s" % str(exc)[:140])
        finally:
            if bl is not None:
                try:
                    bl.close()
                except Exception:                # noqa: BLE001
                    pass
            with self.lock:
                self._bl = None

    def _finish_login(self, header: str):
        """拿到 cookie 后的统一收尾：验证 → 存盘 → 置成功。"""
        ncm = _ncm()
        sess = ncm.NeteaseSession()
        try:
            ok, msg = sess.login_with_cookie(header)
        except Exception as exc:                 # noqa: BLE001
            self._fail("验证出错：%s" % str(exc)[:120])
            return
        if not ok:
            self._fail(msg or "登录凭据无效")
            return
        self._save(sess)
        with self.lock:
            if self._job is not None:
                self._job.update(running=False, state="done", error="",
                                 msg="登录成功", user=sess.user_name or "（未取到昵称）")
        self._clear_qr()

    def _fail(self, msg: str):
        with self.lock:
            if self._job is not None:
                self._job.update(running=False, state="failed", error=str(msg),
                                 msg=str(msg))

    def _cancel_job(self):
        with self.lock:
            if self._job is not None:
                self._job.update(running=False, state="cancelled",
                                 msg="已取消", error="")

    # -- 取消 / 收尾 -------------------------------------------------------
    def cancel(self) -> dict:
        """取消正在跑的登录任务（浏览器窗口会跟着关掉）。"""
        self._stop.set()
        bl = self._bl
        if bl is not None:
            # close() 里有 proc.wait(4)，不能放请求线程 —— 否则接口卡 4 秒
            threading.Thread(target=bl.close, daemon=True).start()
        with self.lock:
            if self._job is not None and self._job.get("running"):
                self._job.update(msg="正在取消…（浏览器窗口会跟着关掉）")
        return {"ok": True}

    def dismiss(self) -> dict:
        """前端关掉登录窗时调一次，把「已完成/已失败」的任务清掉。"""
        with self.lock:
            if self._job is not None and not self._job.get("running"):
                self._job = None
        return {"ok": True}

    # -- 粘贴 Cookie -------------------------------------------------------
    def login_cookie(self, raw: str) -> dict:
        """用粘贴的 Cookie 登录。**同步**，一次网络往返（约 1-3 秒）。"""
        ncm = _ncm()
        text = str(raw or "").strip()
        if not text:
            return {"ok": False, "error": "输入框是空的，请先粘贴 Cookie"}
        sess = self._session()
        try:
            ok, msg = sess.login_with_cookie(text)
        except Exception as exc:                 # noqa: BLE001
            return {"ok": False, "error": "验证出错：%s" % str(exc)[:140]}
        if not ok:
            return {"ok": False, "error": msg or "验证失败"}
        self._save(sess)
        self._clear_qr()
        return {"ok": True, "user_name": sess.user_name or "（未取到昵称）"}

    # -- 扫码 --------------------------------------------------------------
    def qr_start(self) -> dict:
        """申请一个二维码。同步（一次网络，约 0.3 秒），返回矩阵给前端画。"""
        if not deps()["segno"]:
            return {"ok": False,
                    "error": "缺少 segno（pip install segno），"
                             "请改用「浏览器登录」或「粘贴 Cookie」"}
        ncm = _ncm()
        sess = self._session()
        try:
            key, content = sess.qr_new()
        except Exception as exc:                 # noqa: BLE001
            return {"ok": False, "error": "申请二维码失败：%s" % str(exc)[:120]}
        try:
            n, rows = qr_matrix(content)
        except Exception as exc:                 # noqa: BLE001
            return {"ok": False, "error": "生成二维码失败：%s" % str(exc)[:120]}
        with self.lock:
            self._qr = {"key": key, "n": n, "rows": rows,
                        "msg": "等待手机扫码…", "code": 0,
                        "at": time.time(), "busy": False}
        return {"ok": True, "n": n, "rows": rows, "msg": "等待手机扫码…"}

    def qr_poll(self) -> dict:
        """轮询一次扫码状态。返回 ``{code, msg, done, user_name, switch_to}``。

        ``code`` 是网易云的约定：800 过期 / 801 等待扫码 / 802 待确认 /
        803 成功 / 8821 风控。``switch_to="cookie"`` 是给前端的指令 ——
        撞上风控就别让用户傻等，直接把他送到「粘贴 Cookie」那一页。
        """
        with self.lock:
            key = self._qr.get("key") or ""
            started = float(self._qr.get("at") or 0)
            if self._qr_busy:
                return {"ok": True, "code": 0, "msg": "…", "busy": True}
            self._qr_busy = True
        if not key:
            with self.lock:
                self._qr_busy = False
            return {"ok": False, "error": "还没有二维码，请先申请"}
        if time.time() - started > QR_TIMEOUT:
            with self.lock:
                self._qr_busy = False
                self._qr.update(msg="二维码已过期，请重新申请", code=800)
            return {"ok": True, "code": 800, "msg": "二维码已过期，请重新申请",
                    "expired": True}
        try:
            sess = self._session()
            code, msg = sess.qr_check(key)
        except Exception as exc:                 # noqa: BLE001
            with self.lock:
                self._qr_busy = False
                self._qr.update(msg="网络错误，重试中…（%s）" % str(exc)[:40], code=0)
            return {"ok": True, "code": 0,
                    "msg": "网络错误，重试中…（%s）" % str(exc)[:40]}
        out = {"ok": True, "code": int(code), "msg": msg, "done": False}
        if code == 803:
            self._save(sess)
            out.update(done=True, user_name=sess.user_name or "（未取到昵称）")
        elif code == 8821:
            out["switch_to"] = "cookie"
        with self.lock:
            self._qr_busy = False
            self._qr.update(code=int(code), msg=msg,
                            key="" if code in (800, 803) else key)
        return out

    def _clear_qr(self):
        with self.lock:
            self._qr = {"key": "", "n": 0, "rows": [], "msg": "",
                        "code": 0, "at": 0.0, "busy": False}

    # -- 换号 / 退出 -------------------------------------------------------
    def start_switch(self) -> dict:
        """换号：**先清干净再登录**，否则浏览器带着旧号 cookie 一起来，
        静默恢复立刻命中，用户看到还是原来那个号（「怎么点都换不了」）。

        清旧号的存档是**同步**做的（删文件，毫秒级），浏览器那一段进后台。
        """
        nck = _nck()
        ncm = _ncm()
        if nck is None:
            return {"ok": False, "error": "netease_cookie 模块不可用"}
        with self.lock:
            if self._job and self._job.get("running"):
                return {"ok": False, "error": "已经有一个登录流程在跑"}
            self._stop = threading.Event()
            self._job = {"kind": "switch", "running": True, "state": "starting",
                         "msg": "正在清空旧的登录态…", "error": "", "user": "",
                         "started": time.time()}
        try:
            ncm.clear_cookie()
        except Exception:                        # noqa: BLE001
            pass
        threading.Thread(target=self._switch_worker, daemon=True).start()
        return {"ok": True}

    def _switch_worker(self):
        nck = _nck()
        ncm = _ncm()
        try:
            res = nck.switch_account(
                on_ready=lambda: self._set_job(
                    state="waiting",
                    msg="浏览器已打开。请在里面登录要使用的账号。\n"
                        "登录成功后本窗口会自动关闭（最多等 4 分钟）。"),
                stop=lambda: self._stop.is_set())
            if not res.get("ok"):
                if self._stop.is_set():
                    self._cancel_job()
                else:
                    self._fail(res.get("error") or "未取到登录态")
                return
            sess = ncm.NeteaseSession()
            sess.set_cookie_header(res.get("header", ""))
            try:
                sess.refresh_profile()           # 取昵称，失败不影响上传
            except Exception:                    # noqa: BLE001
                pass
            self._save(sess)
            with self.lock:
                if self._job is not None:
                    self._job.update(
                        running=False, state="done", error="", msg="已切换账号",
                        user=sess.user_name or "（未取到昵称）")
            self._clear_qr()
        except Exception as exc:                 # noqa: BLE001
            self._fail(str(exc)[:160])

    def logout(self) -> dict:
        """退出登录：**本机**的登录态清掉，云端文件一个都不动。

        两处都要清（``netease.json`` + 内置浏览器的 cookie），只清文件的话
        下次静默恢复又变回原账号。
        """
        ncm = _ncm()
        nck = _nck()
        file_ok = True
        try:
            file_ok = bool(ncm.clear_cookie())
        except Exception:                        # noqa: BLE001
            file_ok = False
        err = ""
        if nck is not None:
            try:
                ok, why = nck.clear_browser_cookies()
                if not ok:
                    err = why or "清空浏览器 cookie 失败"
            except Exception as exc:             # noqa: BLE001
                err = str(exc)[:120]
        self._clear_qr()
        with self.lock:
            self._job = None
        if not file_ok:
            err = err or "删除登录文件失败"
        return {"ok": not err, "error": err}


# ---------------------------------------------------------------------------
# 进程级单例
# ---------------------------------------------------------------------------
_AUTH = None
_AUTH_LOCK = threading.Lock()


def auth():
    """进程内**唯一**的 :class:`CloudAuth`。

    ⚠ 必须是单例：里面存着「浏览器登录任务」的线程与进度。每个请求造一个
    新实例，等于每 0.5 秒把进度清零一次 —— 前端永远停在「正在启动」。
    """
    global _AUTH
    if _AUTH is not None:
        return _AUTH
    with _AUTH_LOCK:
        if _AUTH is None:
            _AUTH = CloudAuth()
    return _AUTH


# ---------------------------------------------------------------------------
# 命令行自检（排障用）
# ---------------------------------------------------------------------------
def _main(argv=None):
    import json
    import sys

    # ⚠ 只有「直接 python web_ui/cloud_api.py」这一种跑法需要补 path ——
    # 那时 sys.path[0] 是 web_ui/，找不到父目录的 netease_cloud。
    # 正常跑法 ``python -m web_ui.server`` 下父目录本来就在 path 上，
    # server.py 已经插过一次，这里不会重复也不会有副作用。
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)

    argv = list(sys.argv[1:] if argv is None else argv)
    a = auth()
    if not argv or argv[0] == "--status":
        print(json.dumps(a.status(), ensure_ascii=False, indent=2))
        return 0
    if argv[0] == "--qr":
        d = a.qr_start()
        if not d.get("ok"):
            print("ERR %s" % d.get("error"))
            return 1
        print("二维码 %d×%d：%s" % (d["n"], d["n"], d["msg"]))
        for row in d["rows"]:
            print("".join("██" if c == "1" else "  " for c in row))
        return 0
    if argv[0] == "--logout":
        print(json.dumps(a.logout(), ensure_ascii=False))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
