#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_netease.py — 网易云云盘上传链路的本地门禁。

## 为什么要有这份门禁

云盘上传这条链路**有一半是没法自动验证的**（要真账号扫码、要真往云盘写东西）。
所以这里只锁死「不需要账号就能证明是对的那部分」，把不确定性压到最小：

1. **weapi 加密**：``params`` 必须是合法 base64、``encSecKey`` 必须是 256 位
   十六进制；同输入两次加密必须不同（``secret`` 是随机的）。
2. **曲名清洗**：网易云明确要求 ``song`` 字段不含 ``.`` 和 ``/``，否则提交必失败。
3. **WAV → FLAC 无损**：这是整个功能的地基。**逐样本**比对，不是比文件大小。
   ⚠ 这条最容易骗过测试者：如果读成 float32 再写，libsndfile 会加 dither，
   两次写入结果不同；只看「能播」是发现不了的。
4. **Cookie 往返**：存盘再读回，``MUSIC_U`` 不能丢。
5. **网络连通性**（可跳过）：能拿到二维码 unikey，说明加密 + 请求头 + 接口
   路径三样都对。断网时 SKIP，不算失败。

## 用法

    python vendor/check_netease.py

⚠ 需要 ``soundfile`` / ``pycryptodome``。缺了会明确 SKIP 而不是假 PASS。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import traceback
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

RESULTS = []


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label, detail))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", label,
                       ("  [" + detail + "]") if detail else ""))
    return bool(ok)


def skip(label, reason):
    RESULTS.append((None, label, reason))
    print("SKIP  %s  [%s]" % (label, reason))


def _has(mod):
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def make_wav(path, seconds=8, sr=44100):
    """造一个与项目输出同规格的 WAV：44.1k / 16bit / stereo / 方波。

    用 8 秒而不是 3 分钟：门禁要快，而「是否无损」跟时长无关。
    """
    import numpy as np
    total = sr * seconds
    idx = np.arange(total, dtype=np.int64)
    level = np.where((idx * 220 // sr) % 2 == 0, 12000, -12000).astype(np.int16)
    stereo = np.empty((total, 2), dtype=np.int16)
    stereo[:, 0] = level
    stereo[:, 1] = (level * 0.6).astype(np.int16)
    with wave.open(path, "wb") as fh:
        fh.setnchannels(2)
        fh.setsampwidth(2)
        fh.setframerate(sr)
        fh.writeframes(stereo.tobytes())
    return stereo


def main():
    print("=" * 66)
    print("网易云云盘上传链路 本地门禁")
    print("=" * 66)

    try:
        import netease_cloud as nc
    except Exception as exc:
        print("导入 netease_cloud 失败：%s" % exc)
        traceback.print_exc()
        return 2

    # ---- 1. weapi 加密 -----------------------------------------------------
    import base64
    p1 = nc.weapi_params({"type": 1})
    p2 = nc.weapi_params({"type": 1})
    ok_b64 = True
    try:
        base64.b64decode(p1["params"], validate=True)
    except Exception:
        ok_b64 = False
    check(ok_b64 and len(p1["params"]) > 0,
          "1.1 params 是合法 base64", "len=%d" % len(p1["params"]))
    check(len(p1["encSecKey"]) == 256 and all(c in "0123456789abcdef"
                                             for c in p1["encSecKey"]),
          "1.2 encSecKey 是 256 位小写十六进制", "len=%d" % len(p1["encSecKey"]))
    check(p1["params"] != p2["params"],
          "1.3 同一输入两次加密结果不同（secret 随机）")
    check("params" in p1 and "encSecKey" in p1 and len(p1) == 2,
          "1.4 返回体只有 params / encSecKey 两个键")

    # ---- 2. 曲名清洗 -------------------------------------------------------
    cases = [
        ("01_Stage Clear.v2", "01_Stage Clear v2"),
        ("a/b\\c", "a b c"),
        ("  ", "Untitled"),
        ("", "Untitled"),
        ("Castlevania.3", "Castlevania 3"),
    ]
    bad = [(src, nc._clean_song_name(src), want) for src, want in cases
           if nc._clean_song_name(src) != want]
    check(not bad, "2.1 曲名清洗（去掉 . 和 / 、空值兜底）", str(bad) if bad else "")
    long_name = "x" * 300
    check(len(nc._clean_song_name(long_name)) <= 120,
          "2.2 超长曲名被截断到 120 字以内")

    # ---- 3. WAV → FLAC 无损 -----------------------------------------------
    if not _has("soundfile"):
        skip("3.x WAV→FLAC", "未安装 soundfile")
    else:
        import numpy as np
        import soundfile as sf
        work = tempfile.mkdtemp(prefix="ncm_gate_", dir=PROJ)
        try:
            wav = os.path.join(work, "gate.wav")
            stereo = make_wav(wav)
            ok, flac, detail = nc.wav_to_flac(wav)
            check(ok, "3.1 wav_to_flac 执行成功", detail)
            if ok:
                check(os.path.getsize(flac) < os.path.getsize(wav),
                      "3.2 FLAC 比 WAV 小",
                      "%.1f%%" % (100.0 * os.path.getsize(flac) / os.path.getsize(wav)))
                back, sr_back = sf.read(flac, dtype="int16")
                check(np.array_equal(back, stereo),
                      "3.3 **逐样本无损**（不是比大小，是比 PCM）",
                      "%s vs %s" % (back.shape, stereo.shape))
                check(sr_back == 44100, "3.4 采样率保持 44100", str(sr_back))
                check(not os.path.exists(flac + ".part"),
                      "3.5 成功路径不残留 .part 临时文件")

            # 幂等：同一文件再转一次，内容应完全一致（说明没有 dither 引入偏差）
            ok2, flac2, _ = nc.wav_to_flac(wav, os.path.join(work, "gate2.flac"))
            same = ok2 and nc.md5_of(flac) == nc.md5_of(flac2)
            check(same, "3.6 两次编码结果 MD5 相同（证明无 dither 扰动）")

            # 坏输入必须干净失败，不能留下垃圾
            broken = os.path.join(work, "broken.wav")
            with open(broken, "wb") as fh:
                fh.write(b"NOTAWAV" * 100)
            ok3, _, err3 = nc.wav_to_flac(broken)
            check((not ok3) and bool(err3), "3.7 坏 WAV 返回失败并给出原因", err3)
        finally:
            import shutil
            shutil.rmtree(work, ignore_errors=True)

    # ---- 4. Cookie 往返 ----------------------------------------------------
    sess = nc.NeteaseSession()
    sess.set_cookie_header("MUSIC_U=abc123; __csrf=xyz")
    check(sess.is_logged_in(), "4.1 set_cookie_header 后能识别登录态")
    dump = sess.dumps()
    sess2 = nc.NeteaseSession()
    sess2.loads(dump)
    check(sess2.cookie_header() == sess.cookie_header(),
          "4.2 dumps/loads 往返后 Cookie 一致")
    check(sess2.is_logged_in(), "4.3 往返后仍识别为已登录")
    check(not nc.NeteaseSession().is_logged_in(), "4.4 空会话判定为未登录")

    # ---- 4b. Cookie 粘贴解析（8821 风控下这是唯一的登录取径）---------------
    # 用户从浏览器里能粘出至少五六种形态，全都得接住 —— 接不住就是"明明粘对了
    # 却说没登录"。这里一条条钉死。
    ck = nc.clean_cookie_input
    check(ck("MUSIC_U=a; __csrf=b") == "MUSIC_U=a; __csrf=b",
          "4.5 标准头值原样保留")
    check(ck("Cookie: MUSIC_U=a; __csrf=b") == "MUSIC_U=a; __csrf=b",
          "4.6 去掉 Cookie: 前缀")
    check("MUSIC_U=a" in ck("MUSIC_U: a\n__csrf: b"),
          "4.7 多行 Name: Value（header 列表）")
    check(ck("MUSIC_U=a\n__csrf=b") == "MUSIC_U=a; __csrf=b",
          "4.8 换行分隔而非分号")
    check("MUSIC_U=a" in ck('[{"name":"MUSIC_U","value":"a"}]'),
          "4.9 JSON 数组（Cookie-Editor 导出）")
    check("MUSIC_U=a" in ck(".163.com\tTRUE\t/\tFALSE\t0\tMUSIC_U\ta"),
          "4.10 Netscape cookies.txt")
    check(ck("MUSIC_U=first; MUSIC_U=second") == "MUSIC_U=first",
          "4.11 重复键保留第一个")
    check(ck('"MUSIC_U=a; __csrf=b"') == "MUSIC_U=a; __csrf=b",
          "4.12 整串被引号包住")
    check(ck("") == "", "4.13 空输入返回空串")

    # ⚠ set_cookie_header 是追加不是替换，不清空就会新旧 MUSIC_U 混在一起
    s3 = nc.NeteaseSession()
    s3.set_cookie_header("MUSIC_U=old; __csrf=x")
    s3.clear_cookies()
    check((not s3.is_logged_in()) and s3.cookie_header() == "",
          "4.14 clear_cookies 真的清干净")

    # 这两类错误必须在**不发网络请求**时就被拦住（省得白等 20 秒超时）
    okb, msgb = nc.NeteaseSession().login_with_cookie("__csrf=x; NMTID=y")
    check((not okb) and "MUSIC_U" in msgb, "4.15 缺 MUSIC_U 立即拒绝",
          msgb.replace("\n", " ")[:34])
    okc, msgc = nc.NeteaseSession().login_with_cookie("   ")
    check((not okc) and bool(msgc), "4.16 空白输入立即拒绝",
          msgc.replace("\n", " ")[:30])

    # ---- 5. 网络：拿二维码 key（可跳过） ---------------------------------
    try:
        key, content = nc.NeteaseSession().qr_new()
    except Exception as exc:
        skip("5.1 申请二维码 unikey", "网络不可用：%s" % str(exc)[:60])
    else:
        check(bool(key) and len(key) > 10, "5.1 申请二维码 unikey", key)
        check(content.startswith("https://music.163.com/login?codekey="),
              "5.2 二维码内容格式正确")

    # ---- 6. 未登录时的错误处理 --------------------------------------------
    try:
        nc.NeteaseSession().cloud_check("0" * 32, 1024, "flac")
    except nc.NeteaseError as exc:
        check(True, "6.1 未登录调云盘接口被明确拒绝", "code=%s" % exc.code)
    except Exception as exc:
        check(False, "6.1 未登录调云盘接口被明确拒绝", "抛了非预期异常 %r" % exc)
    else:
        skip("6.1 未登录调云盘接口", "服务端没有拒绝（环境可能已带登录态）")

    # ---- 7. 上传细节（离线，本地起 HTTP 服务器）----------------------------
    # 这一节是 v4.22 补的。当时真实上传 13 首全失败，三个错都出在这儿：
    #   ① songId 是 128 位十六进制串，被 int() 吃掉；
    #   ② 上传地址用了 outerUrl（那是**下载**签名地址）→ 403 MismatchSignature；
    #   ③ nosdn.127.net 这个域名在新环境下不解析。
    hex_id = "16C1488E6D61FB309EF0D1ABAB1CC13F" * 4          # 128 位
    s7 = nc.NeteaseSession()
    s7._post = lambda path, payload, **kw: {"code": 200, "needUpload": True,
                                            "songId": hex_id}
    r7 = s7.cloud_check("0" * 32, 1024, "flac")
    check(r7["songid"] == hex_id, "7.1 songId 十六进制串原样返回（不再 int()）",
          str(r7["songid"])[:16] + "…")

    s7b = nc.NeteaseSession()
    s7b._post = lambda path, payload, **kw: {"code": 200, "needUpload": False,
                                            "songId": hex_id}
    check(s7b.cloud_check("0" * 32, 1024, "flac")["need_upload"] is False,
          "7.2 needUpload 原样透出（⚠ 只用于省重传，不等于云盘里有）")

    # commit_info：resourceId 必须带上；songId 回十六进制串也不能崩
    seen = {}

    def _fake_post(path, payload, **kw):
        seen["path"] = path
        seen["payload"] = dict(payload)
        seen["kw"] = dict(kw)
        return {"code": 200, "songId": hex_id}

    s7c = nc.NeteaseSession()
    s7c._post = _fake_post
    back = s7c.commit_info("a" * 32, hex_id, "x.flac", "X", "Y", "Z",
                           resource_id="82740038504")
    check(seen["payload"].get("resourceId") == "82740038504",
          "7.3 提交元数据带 resourceId")
    check(seen["payload"].get("songid") == hex_id,
          "7.4 songid 原样提交（不转数字）")
    check(back == hex_id, "7.5 返回的 songId 原样透传")

    # nos_url：主机、编码、version 三件事，外加「绝不能用 outerUrl」
    key7 = "obj/woDDmMOBw6PClWzCnMK-/83725516839/76cc/d9a9/x.flac"
    u7 = nc.NeteaseSession.nos_url({"bucket": "bk", "objectKey": key7,
                                    "outerUrl": "https://evil.invalid/sig?x=1"})
    check(u7.startswith("https://bk.nos-jd.163yun.com/"), "7.6 主机是 nos-jd.163yun.com",
          u7[:44])
    check("%2F" in u7 and "/83725516839" not in u7.split("?")[0],
          "7.7 objectKey 的 / 编成 %2F")
    check(u7.endswith("?offset=0&complete=true&version=1.0"), "7.8 查询串带 version=1.0")
    check("evil.invalid" not in u7, "7.9 outerUrl 是下载地址，不参与上传")
    check("nosdn.127.net" not in u7, "7.10 不再用已不解析的 nosdn.127.net")

    # put_object 真发一遍（打到本地服务器）
    got = {}

    class _Put(BaseHTTPRequestHandler):
        def do_PUT(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)
            got.update({"method": "PUT", "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "sha": hashlib.sha256(body).hexdigest(), "len": n})
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    try:
        import numpy as np
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Put)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        tmp7 = tempfile.mkdtemp(prefix="put_")
        f7 = os.path.join(tmp7, "payload.flac")
        blob = np.random.default_rng(3).integers(0, 256, 700 * 1024,
                                                 dtype="uint8").tobytes()
        with open(f7, "wb") as fh:
            fh.write(blob)
        md5_7 = hashlib.md5(blob).hexdigest()
        prog = []
        # 把目标主机换成本地端口：只换主机，路径拼接逻辑原样保留
        real = nc.NeteaseSession.nos_url
        nc.NeteaseSession.nos_url = staticmethod(
            lambda t: "http://127.0.0.1:%d%s" % (port, real(t).split(".com", 1)[1]))
        try:
            nc.NeteaseSession().put_object(
                {"bucket": "bk", "objectKey": key7, "token": "TOK"}, f7, md5_7,
                progress=lambda a, b: prog.append((a, b)))
        finally:
            nc.NeteaseSession.nos_url = staticmethod(real)
            srv.shutdown()

        check(got.get("method") == "PUT", "7.11 用的是 PUT")
        check(got.get("path", "").startswith("/obj%2F"), "7.12 请求路径是编码后的 key",
              got.get("path", "")[:40])
        check(got.get("headers", {}).get("x-nos-token") == "TOK", "7.13 带 x-nos-token")
        check(got.get("headers", {}).get("content-md5") == md5_7, "7.14 带 Content-MD5")
        check("transfer-encoding" not in got.get("headers", {}),
              "7.15 不是 chunked（靠显式 Content-Length）")
        check(got.get("sha") == hashlib.sha256(blob).hexdigest(),
              "7.16 收到的字节与源文件完全一致", "%d B" % got.get("len", 0))
        check(len(prog) == 3 and prog[-1] == (len(blob), len(blob)),
              "7.17 分块进度回调到终点", "%d 次" % len(prog))
    except Exception as exc:                              # noqa: BLE001
        check(False, "7.11~7.17 put_object 本地回环", repr(exc)[:70])

    # upload_flac 四步顺序：缺一步都会「提交成功但云盘里没有」
    s7d = nc.NeteaseSession()
    order = []
    s7d.cloud_check = lambda *a, **k: (order.append("check") or
                                       {"need_upload": True, "songid": hex_id})
    s7d.nos_alloc = lambda *a, **k: (order.append("token") or
                                     {"bucket": "bk", "objectKey": key7,
                                      "token": "T", "resourceId": "42"})
    s7d.put_object = lambda *a, **k: order.append("put")
    s7d.commit_info = lambda *a, **k: (order.append("commit") or "999")
    s7d.publish = lambda *a, **k: order.append("publish")
    steps = []
    tmpf = os.path.join(tempfile.mkdtemp(prefix="ord_"), "x.flac")
    with open(tmpf, "wb") as fh:
        fh.write(b"FLAC" * 100)
    r7d = s7d.upload_flac(tmpf, on_step=steps.append)
    check(order == ["check", "token", "put", "commit", "publish"],
          "7.18 四步顺序正确", " → ".join(order))
    check(r7d.get("songid") == "999" and not r7d.get("skipped"),
          "7.19 返回发布后的 songid")
    check(len(steps) >= 4 and steps[0].startswith("查重"),
          "7.20 阶段回调有文案（失败时能定位到步）", " / ".join(steps[:5]))

    # ---- 8. v4.27：三个「日志与事实相反」的坑 ------------------------------
    # 全部来自 2026-10-19 的真实上传现场：
    #   ① 97.8MB 合并 FLAC，needUpload=false 但云盘列表 count=0
    #      → 旧代码「跳过」= 假成功，用户永远看不到歌；
    #   ② publish 正常返回 201，旧代码只认 200 → 每首都报「发布失败」；
    #   ③ 92.6MB 那首在「提交元数据」读超时 20s
    #      → 裸 TimeoutError 冒到 UI，只有一句英文。
    md5_8 = nc.md5_of(tmpf)

    def _mk_sess(fake_post):
        s = nc.NeteaseSession()
        s._post = fake_post
        return s

    # 8.1~8.3 publish 的成功码
    def _pub(code):
        return _mk_sess(lambda path, payload, **kw: {"code": code})

    try:
        _pub(201).publish("1")
        check(True, "8.1 publish 返回 201 视为成功（新建时就是 201）")
    except Exception as exc:                            # noqa: BLE001
        check(False, "8.1 publish 返回 201 视为成功", repr(exc)[:70])
    try:
        _pub(200).publish("1")
        check(True, "8.2 publish 返回 200 视为成功")
    except Exception as exc:                            # noqa: BLE001
        check(False, "8.2 publish 返回 200 视为成功", repr(exc)[:70])
    try:
        _pub(403).publish("1")
        check(False, "8.3 publish 其它 code 仍然报错")
    except nc.NeteaseError as exc:
        check(exc.code == 403, "8.3 publish 其它 code 仍然报错", "code=%s" % exc.code)

    # 8.4~8.7 超时必须是 ERR_TIMEOUT，且只对网络类重试
    class _Boom:
        def open(self, *a, **kw):
            raise TimeoutError("The read operation timed out")

    s8 = nc.NeteaseSession()
    s8._opener = _Boom()
    try:
        s8._post("/x", {})
        check(False, "8.4 读超时转成 NeteaseError")
    except nc.NeteaseError as exc:
        check(exc.code == nc.ERR_TIMEOUT and "超时" in str(exc),
              "8.4 读超时转成 NeteaseError(ERR_TIMEOUT)", str(exc)[:40])
    except Exception as exc:                            # noqa: BLE001
        check(False, "8.4 读超时转成 NeteaseError",
              "抛了裸 %r —— UI 上只会看到英文" % exc)

    s8b = nc.NeteaseSession()
    calls = {"n": 0}

    def _flaky(path, payload, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise nc.NeteaseError("请求超时", nc.ERR_TIMEOUT)
        return {"code": 200, "ok": 1}

    s8b._post = _flaky
    try:
        s8b._post_retrying("/x", {}, tries=3, backoff=(0, 0, 0))
        check(calls["n"] == 3, "8.5 网络类错误会自动重试到成功", "%d 次" % calls["n"])
    except Exception as exc:                            # noqa: BLE001
        check(False, "8.5 网络类错误会自动重试到成功", repr(exc)[:70])

    calls2 = {"n": 0}

    def _biz(path, payload, **kw):
        calls2["n"] += 1
        raise nc.NeteaseError("业务失败", 400)

    s8c = nc.NeteaseSession()
    s8c._post = _biz
    try:
        s8c._post_retrying("/x", {}, tries=3, backoff=(0, 0, 0))
        check(False, "8.6 业务码不重试")
    except nc.NeteaseError:
        check(calls2["n"] == 1, "8.6 业务码不重试（只打一次）", "%d 次" % calls2["n"])
    check(nc.NeteaseSession().commit_timeout >= 60,
          "8.7 提交元数据超时 ≥60s", "%.0fs" % nc.NeteaseSession().commit_timeout)

    # 8.8 needUpload=false 且云盘里没有 → 跳过重传，但必须 commit + publish
    order8 = []

    def _mk_flow(need):
        s = nc.NeteaseSession()
        s.cloud_check = lambda *a, **k: (order8.append("check") or
                                         {"need_upload": need, "songid": hex_id})
        s.nos_alloc = lambda *a, **k: (order8.append("token") or
                                       {"bucket": "bk", "objectKey": key7,
                                        "token": "T", "resourceId": "42"})
        s.put_object = lambda *a, **k: order8.append("put")
        s.commit_info = lambda *a, **k: (order8.append("commit") or "888")
        s.publish = lambda *a, **k: order8.append("publish")
        return s

    r8 = _mk_flow(False).upload_flac(tmpf)
    check(order8 == ["check", "token", "commit", "publish"],
          "8.8 needUpload=false 仍会登记 + 发布（只是不重传）",
          " → ".join(order8))
    check(not r8.get("skipped"), "8.9 不会再把它当成「已存在」跳过")

    # 8.10 免传路径 commit 被拒（400）→ 自动改为完整上传后重试
    order8b = []

    def _s8d():
        s = nc.NeteaseSession()
        st = {"n": 0}

        def _commit(*a, **k):
            st["n"] += 1
            order8b.append("commit")
            if st["n"] == 1:
                raise nc.NeteaseError("uploadStatus: 6", 400)
            return "777"
        s.cloud_check = lambda *a, **k: (order8b.append("check") or
                                         {"need_upload": False, "songid": hex_id})
        s.nos_alloc = lambda *a, **k: (order8b.append("token") or
                                       {"bucket": "bk", "objectKey": key7,
                                        "token": "T", "resourceId": "42"})
        s.put_object = lambda *a, **k: order8b.append("put")
        s.commit_info = _commit
        s.publish = lambda *a, **k: order8b.append("publish")
        return s

    r8b = _s8d().upload_flac(tmpf)
    check(order8b == ["check", "token", "commit", "put", "commit", "publish"],
          "8.10 免传被拒后自动补传并重试", " → ".join(order8b))
    check(r8b.get("songid") == "777", "8.11 补传后拿到正常 songid")

    # 8.12 命中云盘清单 → 真跳过，一个请求都不发
    s8e = nc.NeteaseSession()
    s8e._post = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该发请求"))
    r8e = s8e.upload_flac(tmpf, known_md5={md5_8})
    check(r8e.get("skipped") and r8e.get("in_cloud"),
          "8.12 命中云盘清单才算真跳过（零请求）")

    # 8.13 commit 超时 → 自动重试
    order8c = []

    def _s8f():
        s = nc.NeteaseSession()
        st = {"n": 0}

        def _commit(*a, **k):
            st["n"] += 1
            order8c.append("commit")
            if st["n"] < 3:
                raise nc.NeteaseError("请求超时（60 秒无响应）", nc.ERR_TIMEOUT)
            return "666"
        s.cloud_check = lambda *a, **k: {"need_upload": True, "songid": hex_id}
        s.nos_alloc = lambda *a, **k: {"bucket": "bk", "objectKey": key7,
                                       "token": "T", "resourceId": "42"}
        s.put_object = lambda *a, **k: None
        s.commit_info = _commit
        s.publish = lambda *a, **k: order8c.append("publish")
        return s

    r8f = _s8f().upload_flac(tmpf)
    check(order8c.count("commit") == 3 and "publish" in order8c,
          "8.13 提交超时自动重试后仍然发布", " → ".join(order8c))

    # 8.14 云盘列表分页 + privateCloud 外壳 + md5 集合
    pages = {
        "0": {"code": 200, "count": 4, "data": [
            {"privateCloud": {"id": 1, "songId": 11, "md5": "aa"}},
            {"privateCloud": {"id": 2, "songId": 12, "md5": "bb"}},
        ]},
        "2": {"code": 200, "count": 4, "data": [
            {"privateCloud": {"id": 3, "songId": 13, "md5": "cc"}},
            {"privateCloud": {"id": 4, "songId": 14, "md5": "dd"}},
        ]},
    }

    def _paged(path, payload, **kw):
        return pages[str(payload.get("offset"))]

    s8g = nc.NeteaseSession()
    s8g._post = _paged
    got_md5 = s8g.cloud_md5s(limit=2)
    check(got_md5 == {"aa", "bb", "cc", "dd"},
          "8.14 云盘列表翻页 + 剥 privateCloud 外壳", ",".join(sorted(got_md5)))

    # 8.15 删除用 songId（pcId 会 404，实测过）
    seen_del = {}

    def _del(path, payload, **kw):
        seen_del["payload"] = dict(payload)
        return {"code": 200, "succIds": [1], "failIds": []}

    s8h = nc.NeteaseSession()
    s8h._post = _del
    s8h.delete_songs([11, 12])
    check(json.loads(seen_del["payload"].get("songIds") or "[]") == ["11", "12"],
          "8.15 删除传 songIds 的 JSON 数组",
          seen_del["payload"].get("songIds"))

    failed = [r for r in RESULTS if r[0] is False]
    skipped = [r for r in RESULTS if r[0] is None]
    passed = [r for r in RESULTS if r[0] is True]
    print("-" * 66)
    print("结果: %d 通过 / %d 失败 / %d 跳过" % (len(passed), len(failed), len(skipped)))
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
