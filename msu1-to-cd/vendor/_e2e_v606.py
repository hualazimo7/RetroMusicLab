# -*- coding: utf-8 -*-
"""v6.0.6 端到端：拿**真实 .fsb 音源**跑完整 ConvertWorker，数产物。

前面的门禁和探针都只到「函数返回什么」这一层。这个脚本再往下走一步：
真的解码、真的编码、真的落盘，然后数 FLAC 有几个。

判据很硬 —— 如果「衍生 WAV 过滤」失效，那个假 ``.fsb.wav`` 会被当成
第二首送去解码，vgmstream 立刻报错，这一条就红了。

⚠ 只在临时目录里操作，不碰用户的下载目录。
"""
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dl_convert as dc                                        # noqa: E402

SRC_DIR = (r"D:\MSU1Downloads\joshw\PlayStation 4"
           r"\Assetto Corsa (2016-08-25)(Kunos Simulazioni)(505 Games)[PS4]")
PICK = ["intro2 {4ddb357c-560d-4d7e-be6e-564e28ec4d17}.fsb",
        "116_full_a-proud-moment_0117 {6db25559-424d-4b34-a371-f857c936a165}.fsb"]


def main():
    if not os.path.isdir(SRC_DIR):
        print("跳过：源目录不存在")
        return 0
    tmp = tempfile.mkdtemp(prefix="e2e606_")
    src = os.path.join(tmp, "src")
    out = os.path.join(tmp, "FLAC")
    os.makedirs(src)
    os.makedirs(out)

    got = []
    for name in PICK:
        p = os.path.join(SRC_DIR, name)
        if not os.path.isfile(p):
            print("跳过（缺文件）：%s" % name)
            continue
        shutil.copy2(p, os.path.join(src, name))
        # 造一个「衍生 WAV」：内容故意是垃圾 —— 万一过滤失效，解码立刻炸
        with open(os.path.join(src, name + ".wav"), "wb") as fh:
            fh.write(b"NOT A REAL WAV" * 100)
        got.append(os.path.join(src, name))

    if not got:
        print("没有可用的样本")
        return 1

    print("样本 %d 个真 .fsb + %d 个假 .fsb.wav" % (len(got), len(got)))

    # 枚举层：应该只剩真源
    listed = dc.drop_derived_wavs(
        [os.path.join(src, n) for n in os.listdir(src)
         if dc.is_convertible(os.path.join(src, n))])
    print("枚举：目录里 %d 个可转 → 过滤后 %d 个"
          % (len(os.listdir(src)), len(listed)))

    evs = []
    cv = dc.ConvertWorker(out, lambda t, d: evs.append((t, d)),
                          threading.Event(), fmt="flac")
    cv._bucket.manifest.ensure_loaded()
    for p in listed:
        cv._q.put({"src": p, "url": "", "pkg": ""})
    cv._src_finished = True
    cv.start()
    cv.join(timeout=600)

    flacs = sorted(n for n in os.listdir(out) if n.lower().endswith(".flac"))
    print("\n产物 %d 个：" % len(flacs))
    for n in flacs:
        print("  %s  %d B" % (n, os.path.getsize(os.path.join(out, n))))

    ok = len(flacs) == len(got)
    print("\n期望 %d 个，实际 %d 个  →  %s"
          % (len(got), len(flacs), "通过" if ok else "失败"))

    # 文件头必须是真 fLaC
    for n in flacs:
        with open(os.path.join(out, n), "rb") as fh:
            head = fh.read(4)
        print("  %s 头=%r %s" % (n[:24], head, "OK" if head == b"fLaC" else "!!"))

    errs = [d for t, d in evs if t == "cv_file_failed"]
    if errs:
        print("\n失败事件 %d 条：" % len(errs))
        for e in errs[:3]:
            print("  %s" % e)

    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
