#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：查网易云「音乐云盘」里实际有哪些条目。

用途：用户反馈「日志显示上传完成，网盘里没有」，需要区分
① 根本没提交成功 ② 提交了但处于未发布/审核态 ③ 查错了账号。

只调读取接口，不上传、不删除。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import netease_cloud as ncm


def post(sess, path, payload, timeout=30.0):
    body = urllib.parse.urlencode(ncm.weapi_params(payload)).encode("ascii")
    req = urllib.request.Request(ncm.API_HOST + path, data=body,
                                 headers=sess._headers(), method="POST")
    with sess._opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", "replace"))


def main() -> int:
    data = ncm.load_cookie()
    if not data.get("cookie"):
        print("没有本地登录态")
        return 2
    sess = ncm.NeteaseSession(timeout=25.0)
    sess.loads(data)
    try:
        sess.refresh_profile()
    except ncm.NeteaseError as exc:
        print("刷新登录态失败：%s" % exc)
        return 2
    print("账号：%s（userId=%s）" % (sess.user_name, sess.user_id))

    for path, payload in (
        ("/weapi/v1/cloud/get", {"limit": "100", "offset": "0"}),
        ("/weapi/cloud/get", {"limit": "100", "offset": "0"}),
    ):
        try:
            d = post(sess, path, payload)
        except Exception as exc:                       # noqa: BLE001
            print("\n%s -> 异常 %r" % (path, exc))
            continue
        songs = d.get("data") or []
        print("\n%s -> code=%s count=%s 本页=%d"
              % (path, d.get("code"), d.get("count"), len(songs)))
        for s in songs:
            print("   %s | %s | album=%s | %s B | br=%s | simple=%s"
                  % (s.get("songId"), s.get("songName") or s.get("name"),
                     s.get("album"), s.get("fileSize"), s.get("bitrate"),
                     s.get("simpleSong") is not None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
