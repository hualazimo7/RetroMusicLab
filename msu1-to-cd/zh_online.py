"""用在线翻译服务把 joshw 的英文档案名批量翻成中文。

主用谷歌翻译（免密钥端点），**谷歌连不上时自动切备用源**：
谷歌 → 谷歌备用域名 → MyMemory 公共接口，并带重试退避。
(实测: 有道免密钥端点 404、Bing 网页版 ttranslatev3 401，都不可用。)

只用来补 `zh_titles.py` 里没有的条目：翻完写进 `zh_titles_user.json`，
下次打开就能直接用，不用再联网翻一遍。

批量时把多条标题用换行拼在一次请求里发（省请求）；返回行数对不上就退回逐条翻，
宁可慢也不把两条标题串到一行。
"""

import json
import random
import re
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional

__all__ = ["TranslateError", "translate_titles", "probe", "providers",
           "current_provider"]

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_GOOGLE = "https://translate.googleapis.com/translate_a/single"
_GOOGLE_B = "https://translate.google.com/translate_a/single"

# 旧名字保留，别的模块可能还在引用
ENDPOINT = _GOOGLE


class TranslateError(RuntimeError):
    """联网翻译失败（网络不通 / 被限流 / 返回格式不对）。"""


def _http_text(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _http_json(url: str, timeout: float) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise TranslateError("返回内容不是 JSON（可能被限流）") from exc


# --------------------------------------------------------------------------
# 各家服务：统一暴露 fn(list[str], timeout) -> list[str|None]
# --------------------------------------------------------------------------
def _google_one(text: str, base: str, timeout: float) -> str:
    params = urllib.parse.urlencode(
        {"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text})
    data = _http_json(base + "?" + params, timeout)
    if not isinstance(data, list) or not data or not isinstance(data[0], list):
        raise TranslateError("返回结构异常")
    parts = [seg[0] for seg in data[0]
             if isinstance(seg, list) and seg and isinstance(seg[0], str)]
    out = "".join(parts).strip()
    if not out:
        raise TranslateError("翻译结果为空")
    return out


def _multi_line(one_shot, texts: List[str], timeout: float,
                fallback_one) -> List[Optional[str]]:
    """整批拼一次请求；返回行数对不上就退回逐条，宁可慢也别串行。"""
    merged = one_shot("\n".join(texts), timeout)
    lines = [ln.strip() for ln in merged.split("\n")]
    if len(lines) == len(texts):
        return [ln or None for ln in lines]
    out: List[Optional[str]] = []
    for t in texts:
        try:
            out.append(fallback_one(t))
        except Exception:                            # noqa: BLE001
            out.append(None)
    return out


def _google_batch(base: str):
    def run(texts: List[str], timeout: float) -> List[Optional[str]]:
        return _multi_line(lambda s, to: _google_one(s, base, to), texts,
                           timeout, lambda t: _google_one(t, base, timeout))
    return run


def _youdao_batch(texts: List[str], timeout: float) -> List[Optional[str]]:
    # 有道对多行文本的处理不稳，一律逐条
    out: List[Optional[str]] = []
    for t in texts:
        try:
            out.append(_youdao_one(t, timeout))
        except Exception:                            # noqa: BLE001
            out.append(None)
    return out


#: (显示名, 批量函数, 是否逐条较慢)
def _mymemory_batch(texts: List[str], timeout: float) -> List[Optional[str]]:
    """MyMemory 公共接口(免密钥)。质量一般, 只当最后一档; 原文照抄的结果会被丢弃。"""
    out: List[Optional[str]] = []
    for t in texts:
        url = ("https://api.mymemory.translated.net/get?q=%s&langpair=en|zh-CN"
               % urllib.parse.quote(t))
        try:
            data = _http_json(url, timeout)
            txt = str(data["responseData"]["translatedText"]).strip()   # type: ignore[index]
            out.append(txt if txt and txt != t else None)
        except Exception:                            # noqa: BLE001
            out.append(None)
    return out


# 备源说明(2026-10-18 实测)：有道免密钥端点 404、cn.bing.com 网页版 ttranslatev3 返回 401
# (即使带 cookie + token 也一样), 所以国内能免密钥用的只剩 MyMemory; 谷歌有两个域名可用。
providers = [
    ("谷歌", _google_batch(_GOOGLE), False),
    ("谷歌备用域", _google_batch(_GOOGLE_B), False),
    ("MyMemory", _mymemory_batch, True),
]

_last_ok = {"name": providers[0][0]}


def current_provider() -> str:
    return _last_ok["name"]


def probe(timeout: float = 10.0) -> str:
    """按 谷歌 → 微软 Bing → MyMemory 探测，返回第一个能用的服务。"""
    errs = []
    for name, fn, _slow in providers:
        try:
            got = fn(["Super Mario Bros."], timeout)
            if got and got[0]:
                _last_ok["name"] = name
                return "%s -> %s" % (name, got[0])
        except Exception as exc:                     # noqa: BLE001
            errs.append("%s: %s" % (name, type(exc).__name__))
    raise TranslateError("所有翻译服务都连不上：" + "; ".join(errs))


def _call_with_fallback(texts: List[str], timeout: float,
                        retries: int = 2) -> List[Optional[str]]:
    """按顺序试各服务, 某个挂了换下一个; 返回与 texts 等长的结果。

    单个服务先原地重试 ``retries`` 次再换源 —— 实测遇到的 502 多半是代理/隧道抖动,
    原地退避一下就成了, 没必要立刻降级到质量更差的备用源。
    """
    order = sorted(providers, key=lambda p: 0 if p[0] == _last_ok["name"] else 1)
    errs = []
    for name, fn, _slow in order:
        for attempt in range(retries + 1):
            try:
                got = fn(texts, timeout)
                if any(got):
                    _last_ok["name"] = name
                    return got
                errs.append("%s: 返回全空" % name)
                break
            except Exception as exc:                 # noqa: BLE001
                errs.append("%s: %s" % (name, type(exc).__name__))
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
    raise TranslateError("；".join(errs) or "翻译服务全部失败")


def translate_titles(titles: List[str],
                     on_progress: Optional[Callable[[int, int], None]] = None,
                     cancel=None,
                     batch: int = 12,
                     pause: float = 0.35,
                     sink: Optional[Dict[str, str]] = None,
                     jitter: bool = True) -> Dict[str, str]:
    """把一批标题翻成中文，返回 ``{原文: 译文}``。

    ``on_progress(done, total)`` 在工作线程里被调用。
    ``cancel`` 是 ``threading.Event``，置位后立刻停止并返回已翻到的部分。
    ``sink`` 由调用方持有：中途抛异常时已经翻好的部分也不会丢。
    """
    items = [t.strip() for t in dict.fromkeys(titles or []) if t and t.strip()]
    total = len(items)
    out: Dict[str, str] = sink if sink is not None else {}
    if not total:
        return out

    i = 0
    while i < total:
        if cancel is not None and cancel.is_set():
            break
        chunk = items[i:i + batch]
        got = _call_with_fallback(chunk, 15.0)
        for src, dst in zip(chunk, got):
            if dst and dst != src:
                out[src] = dst
        i += len(chunk)
        if on_progress is not None:
            on_progress(len(out), total)
        time.sleep(pause + (random.uniform(0, 0.25) if jitter else 0.0))
    return out