#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""netease_cloud.py — 把转好的音频上传到网易云「音乐云盘」（核心层，无 tkinter）。

## 为什么需要这一层

用户要的是「转完 WAV 直接进云盘」，但**网易云云盘的对外接口只收 mp3 / flac**。
官方客户端能拖 WAV 进去，是因为它自己先转了一道码（源码级证据：
NeteaseCloudMusicApi 的 ``module/cloud.js`` 里格式校验写死 "MP3 or FLAC"）。

所以链路是：``WAV --(int16 直通)--> FLAC --(MD5 查重)--> NOS 上传 --> 发布``。

## ⚠⚠ 两个实测结论（别"优化"）

1. **FLAC 编码必须 int16 直通**。若先把 WAV 读成 float 再写 ``PCM_16``，
   libsndfile 会加 dither，两次独立写入的 PCM 就不一样了 —— 我第一版因此
   误判成「FLAC 有损」。正确做法：``sf.read(src, dtype="int16")`` 直接喂
   ``sf.write(..., subtype="PCM_16")``，实测逐样本相同。
2. **云盘接口有风控**。未登录打 ``/weapi/cloud/upload/check`` 会返回
   ``{"code":-460,"message":"检测到您的网络环境存在风险"}``。带有效 Cookie
   + 完整浏览器头才通。所以必须先扫码登录，且请求头不能省
   （``Origin`` / ``Accept-Language`` 都要有）。

## weapi 加密（与登录态无关的静态部分）

    params   = AES128-CBC( AES128-CBC(json, NONCE), secret )
    encSecKey= RSA( reverse(secret) )  —— 指数 010001，固定模数

``secret`` 用 16 位随机字母数字；**不要用 base64 取前 16 位**（会带 ``+`` ``/``
``=``，网易云那边会解不出来）。

## 命令行自检

    python netease_cloud.py login          # 打印二维码链接并轮询
    python netease_cloud.py status         # 只检查本地登录态
    python netease_cloud.py upload <路径>  # 上传单个文件或整个目录
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.cookiejar
import json
import os
import random
import socket
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

__all__ = [
    "NeteaseError",
    "NeteaseSession",
    "ERR_NETWORK",
    "ERR_TIMEOUT",
    "weapi_params",
    "wav_to_flac",
    "wav_to_audio",
    "bitrate_for",
    "md5_of",
    "clean_cookie_input",
    "cookie_path",
    "load_cookie",
    "save_cookie",
    "clear_cookie",
    "CONVERTIBLE_EXTS",
]

WEAPI_MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab"
    "17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbda92557c93"
    "870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cfe4875d3e8204"
    "7b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)
WEAPI_NONCE = "0CoJUm6Qyw8W8jud"
WEAPI_PUBKEY = "010001"
WEAPI_IV = "0102030405060708"

#: 云盘音频桶。硬编码在网易云客户端里，换桶会导致 nos 403。
NOS_BUCKET = "jd-musicrep-privatecloud-audio-public"
#: 云盘上传要求的 bitrate 占位值（999000 = 无损）。传别的值会被判成标准音质。
CLOUD_BITRATE = "999000"
#: MP3 上传用的 bitrate（v6.0.1）。
#:
#: 不能沿用 999000：那个值是「无损」的占位标记，服务端拿它做匹配时会把
#: 一个有损文件当成无损去查库，既查不到正确的匹配、也让云盘里显示的码率
#: 是假的。我们自己的 MP3 写入器是 **320 kbps CBR**（见 ``flac_out._write``
#: 里 ``compression_level=0.0`` + ``bitrate_mode="CONSTANT"``），所以填真值。
CLOUD_BITRATE_MP3 = "320000"


def bitrate_for(fmt: str) -> str:
    """按容器给 ``bitrate`` 字段。只认 flac / mp3，其余按无损处理。"""
    return CLOUD_BITRATE_MP3 if str(fmt or "").lower().lstrip(".") == "mp3" \
        else CLOUD_BITRATE

API_HOST = "https://music.163.com"
#: 可参与上传的源文件扩展名
CONVERTIBLE_EXTS = (".wav",)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 往对象存储传音频**不走系统代理**：NOS 是国内直连就能通的地址，而用户机器上
# 常见 Clash/V2Ray 之类的系统代理经常对这种大流量 PUT 直接 502（实测踩过：
# ``Tunnel connection failed: 502 Bad Gateway``）。weapi 那几个接口仍旧走
# 会话默认 opener（那里代理是必要的，毕竟要连 music.163.com）。
_NOS_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


#: 网络类失败的伪 code。服务端 code 都是正数/负数业务码，这两个不会撞。
#: ⚠ 只有这两个 code 值得重试 —— 其余（-460 风控、400 参数、403 签名）重试只会更糟。
ERR_NETWORK = -1
ERR_TIMEOUT = -2


class NeteaseError(RuntimeError):
    """接口返回了非 200，或网络/解密失败。``code`` 是接口的 code 字段。"""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = int(code or 0)

    @property
    def transient(self) -> bool:
        """值得重试吗？（网络超时 / 连接中断 / 404 未登记）"""
        return self.code in (ERR_NETWORK, ERR_TIMEOUT, 404)


# ---------------------------------------------------------------------------
# weapi 加密
# ---------------------------------------------------------------------------

def _aes_encrypt_base64(text: str, key: str) -> str:
    """AES-128-CBC（PKCS#7 补齐，固定 IV）→ base64。"""
    from Crypto.Cipher import AES

    raw = text.encode("utf-8")
    pad = 16 - len(raw) % 16
    raw += bytes([pad]) * pad
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, WEAPI_IV.encode("utf-8"))
    return base64.b64encode(cipher.encrypt(raw)).decode("ascii")


def _rsa_encrypt_hex(text: str) -> str:
    """``pow(reverse(text), e, n)`` → 256 位十六进制。

    Python 的大整数运算原生支持，**不需要任何 RSA 库**；只有 AES 才要
    pycryptodome（自己写 AES 属于不该做的事）。
    """
    reversed_hex = text[::-1].encode("utf-8").hex()
    value = pow(int(reversed_hex, 16), int(WEAPI_PUBKEY, 16), int(WEAPI_MODULUS, 16))
    return format(value, "x").zfill(256)


def weapi_params(payload: Dict) -> Dict[str, str]:
    """把业务参数包成 ``{"params": ..., "encSecKey": ...}``。

    ``secret`` 必须是 **16 位字母数字**；用 base64 截前 16 位会引入
    ``+ / =``，服务端解不出来（表现为 code=-460 或参数错误）。
    """
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    secret = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    params = _aes_encrypt_base64(_aes_encrypt_base64(text, WEAPI_NONCE), secret)
    return {"params": params, "encSecKey": _rsa_encrypt_hex(secret)}


# ---------------------------------------------------------------------------
# 本地工具
# ---------------------------------------------------------------------------

def md5_of(path: str, chunk: int = 1 << 20) -> str:
    """文件 MD5（云盘查重用它，所以必须与文件内容严格对应）。"""
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_cookie_input(raw: str) -> str:
    """把用户从浏览器里粘贴来的各种格式统一成 ``a=1; b=2``。

    支持的粘贴形态（实测用户会粘成什么样子都要接住）：

    ==========================================  ============================
    形态                                         示例
    ==========================================  ============================
    Request Headers 里的 Cookie 值               ``MUSIC_U=xxx; __csrf=yyy``
    带 ``Cookie:`` 前缀                          ``Cookie: MUSIC_U=xxx; ...``
    多行 ``Name: Value``（每条 header 一行）      ``MUSIC_U: xxx\\n__csrf: yyy``
    Netscape cookies.txt 制表符格式             ``.163.com\\tTRUE\\t/\\t...``
    JSON（Cookie-Editor 导出）                   ``[{"name":"MUSIC_U",...}]``
    每行一个 ``a=1``（换行而不是分号）             ``MUSIC_U=xxx\\n__csrf=yyy``
    ==========================================  ============================

    返回时已去掉 ``Cookie:`` 前缀与引号，重复的键保留**第一个**。
    解析不出来返回空串（调用方据此提示）。
    """
    text = str(raw or "").strip()
    if not text:
        return ""

    pairs: List[Tuple[str, str]] = []

    # ---- JSON（Cookie-Editor / EditThisCookie 的导出格式）----
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("name"):
                    pairs.append((str(item["name"]), str(item.get("value", ""))))
        elif isinstance(data, dict):
            rows = data.get("cookies")
            if isinstance(rows, list):
                for item in rows:
                    if isinstance(item, dict) and item.get("name"):
                        pairs.append((str(item["name"]), str(item.get("value", ""))))
            else:                                # {"MUSIC_U": "...", ...}
                for name, value in data.items():
                    if isinstance(value, (str, int, float)):
                        pairs.append((str(name), str(value)))
        if pairs:
            return _join_cookie_pairs(pairs)

    # ---- Netscape cookies.txt（7 列，制表符分隔）----
    if "\t" in text:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) >= 7 and cols[5]:
                pairs.append((cols[5].strip(), cols[6].strip()))
        if pairs:
            return _join_cookie_pairs(pairs)

    # ---- 文本形态：先去 Cookie: 前缀，再按 ; 或换行切 ----
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:"):].strip()
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip().strip('"').strip("'")
        if not line:
            continue
        # 多行 "Name: Value" 形态（但别把 "a=1:2" 误判，所以先看有没有 =）
        if "=" not in line and ":" in line:
            name, _, value = line.partition(":")
            pairs.append((name.strip(), value.strip()))
            continue
        for part in line.split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                pairs.append((name.strip(), value.strip()))
    return _join_cookie_pairs(pairs)


def _join_cookie_pairs(pairs: List[Tuple[str, str]]) -> str:
    """拼回 header，顺手丢掉空名/重复名（同名保留第一个）。"""
    seen = set()
    out = []
    for name, value in pairs:
        if not name or name in seen:
            continue
        seen.add(name)
        out.append("%s=%s" % (name, value))
    return "; ".join(out)


def cookie_path() -> str:
    """登录态落盘位置，与 ``ui_theme.json`` 同一个目录。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "MSU1Converter")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(folder, "netease.json")


def load_cookie() -> Dict:
    """读取上次保存的登录态；读不到返回空 dict（不抛异常）。"""
    path = cookie_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cookie(data: Dict) -> bool:
    """原子写入登录态（tmp + ``os.replace``，与项目其它配置同一套路）。"""
    path = cookie_path()
    if not path:
        return False
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def clear_cookie() -> bool:
    """删掉保存的登录态；下次上传会重新要求登录。

    ⚠ 只删这个文件**不够**：真正让浏览器一开就是旧号的是 profile 里的
    cookie（见 ``netease_cookie.clear_browser_cookies``）。换号必须两处都清，
    否则删了文件下一轮又被静默恢复成原来的账号。
    """
    path = cookie_path()
    if not path or not os.path.isfile(path):
        return True
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def wav_to_flac(src: str,
                dst: Optional[str] = None,
                compression_level: float = 0.8) -> Tuple[bool, str, str]:
    """WAV → FLAC（无损）。

    ⚠⚠ **必须 int16 直通**：``sf.read(..., dtype="int16")`` 读出来的就是原始
    PCM，再原样写回 ``PCM_16``。若中间经过 float32，libsndfile 会加 dither，
    PCM 值会被扰动（实测两次独立写入结果不同）。

    Args:
        src: 源 WAV 路径。
        dst: 目标 FLAC；``None`` 时把扩展名换成 ``.flac``。
        compression_level: 0.0~1.0。0.8 在体积/速度间比较平衡
            （实测 3 分钟方波 31.8MB → 10.9MB，耗时 0.72s）。

    Returns:
        ``(ok, 目标路径, 错误原因)``。失败时目标文件不会残留。
    """
    dst = dst or (os.path.splitext(src)[0] + ".flac")
    tmp = dst + ".part"
    try:
        import soundfile as sf
    except Exception as exc:                       # pragma: no cover
        return False, dst, ("缺少 soundfile，无法转 FLAC：%s。"
                            "安装：python -m pip install soundfile" % exc)

    data = sr = None
    try:
        data, sr = sf.read(src, dtype="int16", always_2d=True)
    except Exception as exc:
        return False, dst, "读取 WAV 失败：%s" % exc

    try:
        sf.write(tmp, data, sr, format="FLAC", subtype="PCM_16",
                 compression_level=compression_level)
        sampled = data.shape[0]
        del data
        if os.path.getsize(tmp) <= 0:
            raise OSError("FLAC 输出为空")
        os.replace(tmp, dst)
        return True, dst, "%d 帧 / %d Hz" % (sampled, sr)
    except Exception as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, dst, "写 FLAC 失败：%s" % exc


def wav_to_audio(src: str, dst: Optional[str] = None, fmt: str = "flac",
                 compression_level: float = 0.8) -> Tuple[bool, str, str]:
    """WAV → 上传用的容器（``flac`` 或 ``mp3``，v6.0.1）。

    为什么不把 MP3 也塞进 :func:`wav_to_flac`：两条路的**数值口径不一样**。

    * FLAC 是**整数无损**编码，必须 int16 直通（float 中转会被 libsndfile
      加 dither，同一份 WAV 两次写出 md5 不同）。
    * MP3 是**浮点有损**编码，libsndfile 的 MP3 写入器**不认 int16 输入** ——
      直接把 int16 交进去，它会把 -32768..32767 当成 -1.0..1.0 解释，削顶成
      满幅方波（实测 SNR -4.4 dB，而正确路径 +21 dB）。所以必须先归一化成
      float32 再交给编码器。

    这两件事都在 ``flac_out._write`` 里分派好了（它按 ``fmt`` 走不同分支），
    所以 MP3 这条路**转调它**而不是在这里另写一份 ``sf.write`` —— 另写一份
    就等于把上面那个坑再挖一遍。

    Args:
        src: 源 WAV。
        dst: 目标；``None`` 时把扩展名换成 ``.fmt``。
        fmt: ``"flac"``（默认）/ ``"mp3"``。
        compression_level: 仅 FLAC 用，含义见 :func:`wav_to_flac`。

    Returns:
        ``(ok, 目标路径, 详情/错误原因)``。失败时目标文件不会残留。
    """
    fmt = str(fmt or "flac").lower().lstrip(".")
    if fmt == "flac":
        return wav_to_flac(src, dst, compression_level)
    if fmt != "mp3":
        return (False, dst or src,
                "不支持的上传格式：%s（只支持 flac / mp3）" % fmt)

    dst = dst or (os.path.splitext(src)[0] + ".mp3")
    try:
        import soundfile as sf
    except Exception as exc:                       # pragma: no cover
        return False, dst, ("缺少 soundfile，无法转 MP3：%s。"
                            "安装：python -m pip install soundfile" % exc)
    try:
        import flac_out
    except Exception as exc:                       # pragma: no cover
        return False, dst, "缺少 flac_out，无法转 MP3：%s" % exc

    try:
        data, sr = sf.read(src, dtype="int16", always_2d=True)
    except Exception as exc:
        return False, dst, "读取 WAV 失败：%s" % exc
    if data is None or data.shape[0] <= 0:
        return False, dst, "源 WAV 没有音频帧"

    # ``encode_pcm`` 收的是交织的 int16 字节串，内部按 fmt 决定要不要归一化。
    try:
        raw = data.tobytes()
        channels = int(data.shape[1]) if data.ndim > 1 else 1
        del data
        ok, path, detail = flac_out.encode_pcm(
            raw, dst, sr=sr, channels=channels, fmt="mp3")
    except Exception as exc:                       # noqa: BLE001
        return False, dst, "写 MP3 失败：%s" % exc
    if not ok:
        return False, dst, detail or "写 MP3 失败"
    # detail 已经是「N 帧 / sr / B」，原样透出（前面再拼一遍就重复了）。
    return True, path, detail


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

class NeteaseSession:
    """一个网易云登录会话。

    用 ``http.cookiejar`` 自动管理 ``MUSIC_U`` / ``__csrf``，所以调用方
    只需要关心业务方法。登录态可 ``dumps()`` / ``loads()`` 存盘。
    """

    def __init__(self, cookie_header: str = "", timeout: float = 20.0) -> None:
        self.timeout = float(timeout)
        #: 「提交元数据」给的时间比普通接口宽得多：一首 90MB+ 的 FLAC，
        #: 服务端要登记对象、算匹配，20 秒实测会超时（真踩过）。
        self.commit_timeout = max(60.0, self.timeout * 3)
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))
        self.user_name = ""
        self.user_id = 0
        if cookie_header:
            self.set_cookie_header(cookie_header)

    # ---- cookie 读写 ------------------------------------------------------
    def clear_cookies(self) -> None:
        """清空 jar。

        ⚠ ``set_cookie_header()`` 是**追加**，不是替换 —— 粘新 Cookie 之前
        必须先清，否则旧的 ``MUSIC_U`` 会和新的混在一起，服务端只认先到的那个，
        表现为「明明粘对了却还是登录成上一个账号」。
        """
        self.user_name = ""
        self.user_id = 0
        try:
            self._jar.clear()
        except Exception:                            # noqa: BLE001
            pass

    def set_cookie_header(self, header: str) -> None:
        """把浏览器里复制来的 ``a=1; b=2`` 灌进 jar。"""
        for part in str(header or "").split(";"):
            if "=" not in part:
                continue
            name, _, value = part.strip().partition("=")
            if not name:
                continue
            self._jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=name, value=value, port=None, port_specified=False,
                domain=".music.163.com", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True, secure=False, expires=None,
                discard=False, comment=None, comment_url=None, rest={},
            ))

    def cookie_header(self) -> str:
        """导出 ``a=1; b=2`` 形式，用于存盘/复用。"""
        return "; ".join("%s=%s" % (c.name, c.value) for c in self._jar)

    def dumps(self) -> Dict:
        return {"cookie": self.cookie_header(),
                "user_name": self.user_name, "user_id": self.user_id,
                "saved_at": int(time.time())}

    def loads(self, data: Dict) -> None:
        if not data:
            return
        self.set_cookie_header(data.get("cookie", ""))
        self.user_name = data.get("user_name", "") or ""
        self.user_id = int(data.get("user_id") or 0)

    def is_logged_in(self) -> bool:
        return any(c.name == "MUSIC_U" for c in self._jar)

    def login_with_cookie(self, raw: str) -> Tuple[bool, str]:
        """用浏览器里复制的 Cookie 登录（扫码被风控时的保底方案）。

        为什么需要它：网易云从 2024 年起对网页扫码登录加了 **8821 行为验证码**
        风控，第三方客户端扫码经常在「手机点确认」那一步被拒（提示「请切换其他
        登陆方式或升级新版本再试」）。Cookie 登录走的是浏览器已建立的会话，
        不触发这套风控，是目前唯一稳的路子。

        Args:
            raw: 用户粘贴的原文，格式随意（见 :func:`clean_cookie_input`）。

        Returns:
            ``(成功?, 说明)``。成功时 ``self.user_name`` 已填好昵称。
        """
        header = clean_cookie_input(raw)
        if not header:
            return False, "没解析出任何 Cookie，请确认复制的是完整的那一串"

        names = {part.partition("=")[0].strip() for part in header.split(";")}
        if "MUSIC_U" not in names:
            return False, ("这段 Cookie 里没有 MUSIC_U。\n"
                           "多半是复制了根域（163.com）的 Cookie，"
                           "请刷新 music.163.com 后重新从请求头里复制。")

        backup = self.cookie_header()
        self.clear_cookies()
        self.set_cookie_header(header)
        try:
            profile = self.refresh_profile()
        except NeteaseError as exc:
            self.clear_cookies()
            if backup:
                self.set_cookie_header(backup)
            return False, "验证失败：%s" % exc

        if not profile:
            # 清干净再退回旧态，别把无效 Cookie 留在 jar 里
            self.clear_cookies()
            if backup:
                self.set_cookie_header(backup)
            return False, "Cookie 已失效（网易云没返回账号信息），请重新登录网页版后再复制"

        return True, self.user_name or ("用户 %s" % self.user_id)

    # ---- HTTP ------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": _UA,
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def _post(self, path: str, payload: Dict,
              timeout: Optional[float] = None) -> Dict:
        """打一个 weapi 接口，返回解析后的 JSON。

        ⚠ 不要省略 ``Origin`` / ``Accept-Language``：实测缺这两个头时
        云盘接口会直接吃 ``-460 网络环境风险``。

        ⚠⚠ **读超时必须是 NeteaseError，不能是裸 TimeoutError**。
        以前没接住：92.6MB 的 FLAC 在「提交元数据」那步服务端慢一点，
        ``resp.read()`` 抛 ``TimeoutError('The read operation timed out')``，
        一路穿到 UI 只显示这句英文，用户完全看不出是哪一步、能不能重试。
        现在统一成 ``ERR_TIMEOUT``，上层据此决定重试。
        """
        tmo = self.timeout if timeout is None else float(timeout)
        body = urllib.parse.urlencode(weapi_params(payload)).encode("ascii")
        req = urllib.request.Request(API_HOST + path, data=body,
                                     headers=self._headers(), method="POST")
        try:
            with self._opener.open(req, timeout=tmo) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise NeteaseError("HTTP %s" % exc.code, exc.code) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise NeteaseError("请求超时（%g 秒无响应）：%s" % (tmo, path),
                                   ERR_TIMEOUT) from exc
            raise NeteaseError("网络不可达：%s" % reason, ERR_NETWORK) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise NeteaseError("请求超时（%g 秒无响应）：%s" % (tmo, path),
                               ERR_TIMEOUT) from exc
        except (ConnectionResetError, ConnectionAbortedError,
                http.client.RemoteDisconnected,
                http.client.IncompleteRead) as exc:
            raise NeteaseError("连接中断：%s" % exc, ERR_NETWORK) from exc
        text = raw.decode("utf-8", "replace")
        if not text.strip():
            raise NeteaseError("接口返回空响应：%s" % path, ERR_NETWORK)
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise NeteaseError("接口返回非 JSON：%s" % text[:120]) from exc
        code = int(data.get("code") or 0)
        if code == -460:
            raise NeteaseError(
                "网易云风控拦截（-460）：请确认已登录，并降低上传频率", code)
        return data

    def _post_retrying(self, path: str, payload: Dict,
                       timeout: Optional[float] = None,
                       tries: int = 3,
                       backoff: Tuple[float, ...] = (1.5, 3.0, 6.0)) -> Dict:
        """网络类错误自动重试（**只重试网络**，业务码立刻抛）。"""
        last: Optional[NeteaseError] = None
        for i in range(max(1, tries)):
            try:
                return self._post(path, payload, timeout=timeout)
            except NeteaseError as exc:
                last = exc
                if not exc.transient or i == tries - 1:
                    raise
                time.sleep(backoff[min(i, len(backoff) - 1)])
        raise last if last else NeteaseError("未知失败")

    # ---- 登录 ------------------------------------------------------------
    def qr_new(self) -> Tuple[str, str]:
        """申请一个扫码登录的 key。

        Returns:
            ``(unikey, 二维码内容)``。二维码内容可直接交给前端渲染，
            手机网易云 App 扫它即可。
        """
        data = self._post("/weapi/login/qrcode/unikey", {"type": 1})
        key = str(data.get("unikey") or "")
        if not key:
            raise NeteaseError("申请二维码失败：%s" % data)
        return key, "https://music.163.com/login?codekey=%s" % key

    def qr_check(self, unikey: str) -> Tuple[int, str]:
        """轮询扫码状态。

        Returns:
            ``(code, 说明)``，code 含义（网易云约定）：

            ======  ==========================================
            800     二维码过期，需要重新申请
            801     等待扫码
            802     已扫码，等待手机端确认
            803     授权成功，Cookie 已进 jar
            ======  ==========================================
        """
        data = self._post("/weapi/login/qrcode/client/login",
                          {"key": unikey, "type": 1})
        code = int(data.get("code") or 0)
        message = {
            800: "二维码已过期",
            801: "等待手机扫码",
            802: "已扫码，请在手机上确认",
            803: "登录成功",
            # ⚠ 8821 = 需要行为验证码。手机上会显示「请切换其他登陆方式或
            # 升级新版本再试」。这是网易云 2024 年后对网页扫码加的风控，
            # 不是我们加密写错了 —— 撞上就只能切 Cookie 登录。
            8821: "风控拦截（8821）：扫码登录被拒绝，请切到「粘贴 Cookie」",
        }.get(code, str(data.get("message") or "未知状态"))
        if code == 803:
            self.refresh_profile()
        return code, message

    def refresh_profile(self) -> Dict:
        """登录成功后拉一次账号信息（顺便验证 Cookie 是否真的有效）。"""
        data = self._post("/weapi/w/nuser/account/get", {})
        profile = data.get("profile") or {}
        if profile:
            self.user_name = str(profile.get("nickname") or "")
            self.user_id = int(profile.get("userId") or 0)
        return profile

    # ---- 云盘 ------------------------------------------------------------
    def cloud_check(self, md5: str, size: int, ext: str = "flac",
                    bitrate: Optional[str] = None) -> Dict:
        """云盘查重。返回 ``{"need_upload": bool, "songid": str}``。

        ``ext`` / ``bitrate`` 必须和**要传的那个文件**一致（v6.0.1 起支持
        MP3）：查重是拿「md5 + 大小 + 扩展名 + 码率」一起做匹配的，扩展名填错
        就等于问错了问题 —— 要么漏掉真正存在的重复项，要么把 FLAC 的结论
        套到 MP3 上。

        ⚠⚠ ``need_upload=False`` 只表示「NOS 内容库里已有同一份数据」，
        **不代表「你的云盘列表里有这首歌」**。实测证据（2026-10-19）：
        一个 97.8MB 的合并 FLAC，``needUpload=false``，可云盘列表
        ``count=0``、该 md5 命中 0 条 —— 它是上一次 PUT 成功但提交元数据
        超时留下的孤儿。照旧逻辑「跳过」= 假成功，用户永远看不到歌。
        所以这个字段只能用来**省掉重传**，不能用来省掉登记。
        """
        data = self._post_retrying("/weapi/cloud/upload/check", {
            "songid": "0",
            "version": 1,
            "length": str(int(size)),
            "ext": ext.lstrip("."),
            "md5": md5,
            "bitrate": str(bitrate or bitrate_for(ext)),
        }, tries=3)
        code = int(data.get("code") or 0)
        if code != 200:
            raise NeteaseError("查重失败：%s" % (data.get("message") or data), code)
        # ⚠⚠ **songId 不是数字**：实测（2026-10）返回 128 位十六进制串，
        #   形如 ``16C1488EE6D61FB3…02073``。曾经在这里写 ``int(song_id)``，
        #   结果每首歌都在上传第一步炸掉，报
        #   ``invalid literal for int() with base 10``。
        #   它是不透明标识（不是主键，也不是数组下标），**原样透传**给
        #   ``commit_info`` / ``publish`` 就行，任何数值化都是错的。
        #   ⚠ 即便 needUpload=true 也会返回这个串，**别传 "0"** ——
        #   实测 commit 带 "0" 直接 400。
        song_id = data.get("songId") or data.get("songid") or 0
        return {"need_upload": bool(data.get("needUpload", True)),
                "songid": song_id}

    def cloud_songs(self, limit: int = 500, max_pages: int = 40) -> List[Dict]:
        """列出「我的音乐云盘」全部条目（自动翻页）。

        返回的每条已剥掉 ``privateCloud`` 外壳，字段是
        ``id``(pcId) / ``songId`` / ``song`` / ``md5`` / ``fileName`` / ``fileSize``。

        ⚠ 云盘列表才是「用户能不能看到」的唯一判据；``upload/check`` 不是。
        ⚠ ``limit`` 实测 500 可用，返回条数小于 limit 就是最后一页。
        """
        out: List[Dict] = []
        seen_pc = set()
        offset = 0
        for _ in range(max(1, max_pages)):
            data = self._post_retrying("/weapi/v1/cloud/get", {
                "limit": str(limit), "offset": str(offset)}, tries=3)
            rows = data.get("data") or []
            if not rows:
                break
            for row in rows:
                pc = row.get("privateCloud") or row
                key = pc.get("id")
                if key in seen_pc:           # 翻页有重叠时去重
                    continue
                seen_pc.add(key)
                out.append(pc)
            offset += len(rows)
            total = int(data.get("count") or 0)
            if len(rows) < limit or (total and offset >= total):
                break
        return out

    def cloud_md5s(self, **kw) -> set:
        """云盘里所有文件的 md5 集合（用来做真跳过判断）。"""
        return {str(s.get("md5") or "") for s in self.cloud_songs(**kw)
                if s.get("md5")}

    def cloud_has(self, md5: str) -> bool:
        """这首歌真的在云盘列表里吗？"""
        return str(md5).lower() in {m.lower() for m in self.cloud_md5s()}

    def delete_songs(self, song_ids: List) -> Dict:
        """从云盘删除条目。⚠ 参数是 **songId**，不是 pcId（pcId 会 404）。"""
        ids = [str(x) for x in song_ids if x]
        if not ids:
            return {"succIds": [], "failIds": [], "code": 200}
        return self._post_retrying("/weapi/cloud/del",
                                   {"songIds": json.dumps(ids)}, tries=2)

    def nos_alloc(self, filename: str, md5: str, size: int,
                  ext: str = "flac") -> Dict:
        """申请 NOS 上传令牌。返回 ``{bucket, objectKey, token}``。"""
        data = self._post_retrying("/weapi/nos/token/alloc", {
            "bucket": NOS_BUCKET,
            "ext": ext.lstrip("."),
            "filename": filename,
            "local": "false",
            "nos_product": 3,
            "type": "audio",
            "md5": md5,
        }, tries=3)
        result = data.get("result") or {}
        object_key = str(result.get("objectKey") or "")
        token = str(result.get("token") or "")
        if not object_key or not token:
            raise NeteaseError("申请上传令牌失败：%s"
                               % (data.get("message") or data),
                               int(data.get("code") or 0))
        return {"bucket": str(result.get("bucket") or NOS_BUCKET),
                "objectKey": object_key,
                "token": token,
                "docId": str(result.get("docId") or ""),
                # 提交元数据要带上它，否则服务端认不出这个对象是本次上传的。
                "resourceId": str(result.get("resourceId") or ""),
                # 服务端连签名带地址一起给了，直接用最保险：它内部的 objectKey
                # 已经编码成 ``obj%2F...``，我们不必自己猜编码规则。
                "outerUrl": str(result.get("outerUrl") or "")}

    @staticmethod
    def nos_url(ticket: Dict) -> str:
        """算出 PUT 目标地址。

        这里有**三个**实测出来的坑，一次踩全：

        1. ⚠⚠ **``outerUrl`` 不能拿来 PUT**。令牌响应里的 ``outerUrl`` 是一条
           带 ``Signature`` 的**下载**地址；拿它 PUT 会吃
           ``403 MismatchSignature``。上传地址必须自己拼。
        2. ⚠ **主机是 ``{bucket}.nos-jd.163yun.com``，不是 ``nosdn.127.net``**：
           后者在本机实测根本不解析（``getaddrinfo failed``），前端那条老写法
           早就过期了。
        3. ⚠ ``objectKey`` 里的 ``/`` 要编成 ``%2F``（``outerUrl`` 就是这个形态）。
           令牌里登记的是一个**整体 key**，保留斜杠会被当成多级路径。
        """
        key = urllib.parse.quote(str(ticket["objectKey"]), safe="")
        return "https://%s.nos-jd.163yun.com/%s?offset=0&complete=true&version=1.0" % (
            ticket["bucket"], key)

    def put_object(self, ticket: Dict, path: str, md5: str,
                   progress: Optional[Callable[[int, int], None]] = None) -> None:
        """把文件二进制 PUT 到网易云对象存储。

        ``progress(已传, 总计)`` 每 256KB 回调一次；UI 侧只往 queue 里塞，
        不要在里面碰 Tk。
        """
        total = os.path.getsize(path)
        url = self.nos_url(ticket)
        headers = {
            "User-Agent": _UA,
            "x-nos-token": ticket["token"],
            "Content-Type": "audio/flac",
            "Content-MD5": md5,
            "Content-Length": str(total),
        }
        # 用「可迭代体」分块发送，避免把几十 MB 一次性读进内存。
        # ⚠ 生成器只能消费一次，所以每次尝试都要**新建一份**（下面会重试）。
        def _make_body():
            state = {"sent": 0}

            def gen():
                with open(path, "rb") as fh:
                    for block in iter(lambda: fh.read(256 * 1024), b""):
                        state["sent"] += len(block)
                        if progress:
                            try:
                                progress(state["sent"], total)
                            except Exception:             # noqa: BLE001
                                pass      # 进度回调失败不能影响上传
                        yield block

            return gen()

        # 直连优先（见 _NOS_OPENER 的注释）；连不上再退回系统代理 —— 有些
        # 环境反过来（必须走代理才能出网）。HTTP 层面的错误不重试，那是
        # 服务端明确拒绝，换条链路也没用。
        last_exc = None
        for open_ in (_NOS_OPENER.open, urllib.request.urlopen):
            req = urllib.request.Request(url, data=_make_body(),
                                         headers=headers, method="PUT")
            try:
                with open_(req, timeout=max(self.timeout, 120)) as resp:
                    if resp.status not in (200, 201, 204):
                        raise NeteaseError("NOS 上传返回 HTTP %s" % resp.status,
                                           resp.status)
                return
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                raise NeteaseError("NOS 上传失败 HTTP %s：%s" % (exc.code, detail),
                                   exc.code) from exc
            except urllib.error.URLError as exc:
                last_exc = exc
        raise NeteaseError("连不上对象存储：%s" % last_exc) from last_exc

    def commit_info(self, md5: str, songid, filename: str,
                    song: str, artist: str = "", album: str = "",
                    resource_id: str = "",
                    timeout: Optional[float] = None,
                    bitrate: Optional[str] = None) -> str:
        """提交云盘元数据，返回云盘 songId（**字符串**，可能是十六进制串）。

        ⚠ ``songid`` 必须用 ``cloud_check`` 返回的那个串（128 位十六进制），
        即便 ``needUpload=true`` 也有值。实测传 ``"0"`` 会直接
        ``{"code": 400, "songId": "0"}``。

        ⚠ 超时默认走 ``self.commit_timeout``（≥60s）。返回值里没有
        ``code`` 时按成功处理（服务端部分版本只给 ``songId``）。
        """
        payload = {
            "md5": md5,
            "songid": str(songid),
            "filename": filename,
            "song": song,
            "album": album or ".",
            "artist": artist or ".",
            "bitrate": str(bitrate or CLOUD_BITRATE),
        }
        # ``resourceId`` 来自令牌接口，是「这次上传占的那个坑」的编号；
        # 不传的话服务端只回 ``uploadStatus: 6`` + 400，实测必失败。
        if resource_id:
            payload["resourceId"] = str(resource_id)
        data = self._post_retrying("/weapi/upload/cloud/info/v2", payload,
                                   timeout=(self.commit_timeout if timeout is None
                                            else timeout),
                                   tries=1)      # 重试策略在 upload_flac 里
        code = int(data.get("code") or 0)
        if code not in (200, 201):
            raise NeteaseError("提交元数据失败：%s"
                               % (data.get("message") or data.get("msg") or data),
                               code)
        # ⚠ 同样不能 int()：有的账号/版本这里回的是十六进制串。
        return data.get("songId") or data.get("id") or songid

    def publish(self, songid) -> None:
        """把云盘资源发布出来（不做这步，App 里的云盘看不到）。

        ⚠⚠ **成功码是 200 或 201**，实测两种都会出现：新建时 201，
        重复发布时 200。早期版本只认 200，于是**每一首上传的最后一律
        报「发布失败」**，而文件其实已经进云盘了 —— 日志与事实相反。
        """
        data = self._post_retrying("/weapi/cloud/pub/v2",
                                   {"songid": str(songid)}, tries=3)
        code = int(data.get("code") or 0)
        if code not in (200, 201):
            raise NeteaseError("发布失败：%s"
                               % (data.get("message") or data.get("msg") or data),
                               code)

    def upload_flac(self, path: str, song: Optional[str] = None,
                    artist: str = "RetroMusic Lab", album: str = "Game Music",
                    progress: Optional[Callable[[int, int], None]] = None,
                    on_step: Optional[Callable[[str], None]] = None,
                    known_md5: Optional[set] = None,
                    force: bool = False,
                    fmt: Optional[str] = None) -> Dict:
        """完整上传一个音频文件到云盘。返回 ``{ok, skipped, songid, md5}``。

        ``fmt`` 是**文件容器的权威声明**（``"flac"`` / ``"mp3"``，v6.0.1）。
        不给就按扩展名猜 —— 猜错的后果不是「传不上去」，而是查重/登记用错
        口径（见 :meth:`cloud_check`），所以调用方知道自己转了什么就显式传。
        方法名保留 ``upload_flac`` 是为了不破坏既有调用方（tkinter / Qt /
        web 三处），实际已经与容器无关。

        ``known_md5`` 是**云盘列表**里的 md5 集合（``cloud_md5s()``）。
        只有命中它才算真跳过；命中时还会把该 md5 记进去，避免同一批里重复传。

        ``force=True`` 无视 ``known_md5``（核对阶段发现缺文件时用它重试）。

        ## ⚠⚠ 两条实测出来的铁律（2026-10-19）

        1. **``needUpload=false`` 不等于「云盘里有」。** 它只说明 NOS 内容库
           有这份数据（可能是上次 PUT 成功但提交超时留下的孤儿）。照旧跳过，
           用户就会遇到「日志说跳过/完成，网盘里啥也没有」。正确做法：
           命中 ``known_md5`` 才跳过；否则**跳过重传但必须登记 + 发布**。
        2. **``publish`` 返回 201 也是成功。** 只认 200 会让每一首都报
           「发布失败」，而文件其实已经在云盘里。

        ``on_step(文案)`` 是**诊断用的阶段回调**：几步里哪一步炸的，日志上直接
        看得出来。没有它的时候只能看到一句异常，根本分不清是查重、上传还是发布
        出了问题（第一次真实上传就吃了这个亏）。
        """
        def _step(msg):
            if on_step:
                try:
                    on_step(msg)
                except Exception:                     # noqa: BLE001
                    pass

        md5 = md5_of(path)
        size = os.path.getsize(path)
        name = os.path.basename(path)
        song = _clean_song_name(song or os.path.splitext(name)[0])
        # 容器口径只认一处：``fmt`` 参数优先，没给才按扩展名推。
        ext = str(fmt or os.path.splitext(name)[1]).lower().lstrip(".") or "flac"
        br = bitrate_for(ext)

        # 真跳过：只有「云盘列表里已经有这个 md5」才算。
        if known_md5 is not None and md5 in known_md5 and not force:
            _step("云盘里已有同一文件（按 MD5 核对），跳过")
            return {"ok": True, "skipped": True, "songid": 0, "md5": md5,
                    "in_cloud": True}

        _step("查重…")
        chk = self.cloud_check(md5, size, ext, bitrate=br)
        _step("申请上传令牌…")
        ticket = self.nos_alloc(name, md5, size, ext)

        uploaded = False
        if chk["need_upload"]:
            _step("上传中（%.1f MB）…" % (size / 1e6))
            self.put_object(ticket, path, md5, progress)
            uploaded = True
        else:
            # 内容库有、自己的云盘没有 —— 省掉重传，但**不能省登记**。
            # 实测：alloc（不 PUT）→ commit → publish 能把 97.8MB 的歌挂上去。
            _step("云端内容库已有同一份数据，跳过重传（仍需登记到云盘）…")

        # 提交元数据：404（服务端还没登记对象）/ 网络超时都值得重试。
        # ⚠ 重试可能造出两条记录（实测 commit 每次给不同 songId），所以
        #   批次末尾还有一次按 md5 的核对 + 清重（见 CloudUploader.run）。
        attempts = 3
        song_id = None
        for i in range(1, attempts + 1):
            try:
                _step("提交元数据…" if i == 1 else "提交元数据（第 %d 次）…" % i)
                song_id = self.commit_info(
                    md5, chk["songid"], name, song, artist, album,
                    resource_id=ticket.get("resourceId", ""),
                    bitrate=br)
                break
            except NeteaseError as exc:
                # 没重传就登记被拒（uploadStatus: 6 之类）→ 老老实实传一份
                if not uploaded and exc.code == 400:
                    _step("登记被拒（%s），改为完整上传后重试…" % str(exc)[:60])
                    self.put_object(ticket, path, md5, progress)
                    uploaded = True
                    continue
                if exc.transient and i < attempts:
                    wait = 1.5 * i
                    _step("提交失败（%s），%.1f 秒后重试…" % (str(exc)[:80], wait))
                    time.sleep(wait)
                    continue
                raise

        _step("发布…")
        self.publish(song_id)
        return {"ok": True, "skipped": False, "songid": song_id, "md5": md5}


def _clean_song_name(text: str) -> str:
    """清洗曲名：网易云明确要求 ``song`` 字段不能含 ``.`` 和 ``/``。

    ⚠ 只剥**音频扩展名**，不能对任何 ``.xxx`` 都 splitext ——
    ``01_Stage Clear.v2`` 这种名字里的 ``.v2`` 是曲名的一部分，
    一刀切会把名字截短。
    """
    text = str(text or "")
    stem, ext = os.path.splitext(text)
    if ext.lower() in (".flac", ".wav", ".mp3", ".m4a", ".ogg", ".ape", ".wv"):
        text = stem
    cleaned = text.replace(".", " ").replace("/", " ").replace("\\", " ")
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned[:120] or "Untitled"


# ---------------------------------------------------------------------------
# CLI（用于在没有 GUI 的情况下验证整条链路）
# ---------------------------------------------------------------------------

def _cli_login(session: NeteaseSession) -> int:
    key, content = session.qr_new()
    print("二维码内容（用网易云 App 扫码）：")
    print("  " + content)
    print("（GUI 里会把这个内容渲染成二维码图片）")
    print("等待扫码…")
    for _ in range(120):
        code, message = session.qr_check(key)
        if code == 803:
            state = session.dumps()
            save_cookie(state)
            print("登录成功：%s（userId=%s）" % (session.user_name, session.user_id))
            print("已保存到 %s" % cookie_path())
            return 0
        if code == 800:
            print("二维码过期，请重新运行")
            return 2
        if code in (801, 802):
            time.sleep(2)
            continue
        print("异常状态 %s：%s" % (code, message))
        time.sleep(2)
    print("超时未完成登录")
    return 3


def _cli_status() -> int:
    data = load_cookie()
    sess = NeteaseSession()
    sess.loads(data)
    if not sess.is_logged_in():
        print("未登录（没有保存的 MUSIC_U）")
        return 1
    try:
        profile = sess.refresh_profile()
    except NeteaseError as exc:
        print("登录态可能已失效：%s" % exc)
        return 1
    print("已登录：%s（userId=%s）" % (sess.user_name, sess.user_id))
    print("Cookie 文件：%s" % cookie_path())
    return 0 if profile else 1


def _cli_upload(target: str) -> int:
    sess = NeteaseSession()
    sess.loads(load_cookie())
    if not sess.is_logged_in():
        print("还没登录，先跑：python netease_cloud.py login")
        return 1

    files = []
    if os.path.isdir(target):
        for name in sorted(os.listdir(target)):
            if name.lower().endswith((".wav", ".flac")):
                files.append(os.path.join(target, name))
    elif os.path.isfile(target):
        files.append(target)
    if not files:
        print("没找到可上传的 wav/flac：%s" % target)
        return 1

    ok = fail = skip = 0
    try:
        known = sess.cloud_md5s()
        print("云盘现有 %d 首（用于跳过判断）" % len(known))
    except NeteaseError as exc:
        print("⚠ 读取云盘列表失败（%s），本次不做跳过判断" % exc)
        known = None
    for path in files:
        print("→ %s" % os.path.basename(path))
        tmp_flac = ""
        try:
            src = path
            if path.lower().endswith(".wav"):
                done, tmp_flac, detail = wav_to_flac(path)
                if not done:
                    print("   转 FLAC 失败：%s" % detail)
                    fail += 1
                    continue
                src = tmp_flac
            result = sess.upload_flac(src, known_md5=known)
            if result.get("skipped"):
                print("   云盘里已有同一文件，跳过（songid=%s）" % result.get("songid"))
                skip += 1
            else:
                print("   上传成功（songid=%s）" % result.get("songid"))
                ok += 1
                if known is not None and result.get("md5"):
                    known.add(result["md5"])
        except NeteaseError as exc:
            print("   失败：%s" % exc)
            fail += 1
        finally:
            if tmp_flac and os.path.exists(tmp_flac):
                try:
                    os.remove(tmp_flac)        # 临时 FLAC 用完就删
                except OSError:
                    pass
        time.sleep(1.2)                        # 限速，避免触发风控

    print("-" * 50)
    print("成功 %d / 跳过 %d / 失败 %d" % (ok, skip, fail))
    save_cookie(sess.dumps())
    return 0 if fail == 0 else 1


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = args[0]
    if cmd == "login":
        return _cli_login(NeteaseSession())
    if cmd == "status":
        return _cli_status()
    if cmd == "upload":
        if len(args) < 2:
            print("用法：python netease_cloud.py upload <文件或目录>")
            return 1
        return _cli_upload(args[1])
    print("未知命令：%s" % cmd)
    return 1


if __name__ == "__main__":
    sys.exit(main())
