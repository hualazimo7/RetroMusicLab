# -*- coding: utf-8 -*-
"""check_nonascii_path.py —— 「非 ASCII 路径」门禁（v4.15 新增）

为什么需要这个门禁
------------------
Windows 上两个解码库都是**按 ANSI 码页**打开文件的：

  · libgme   `gme_open_file`   -> 失败返回 "Couldn't open file"
  · miniaudio `decode_file`    -> DecodeError(-7)（-7 = MA_DOES_NOT_EXIST）

路径里只要有一个非 ASCII 字符（中文目录名 / 中文曲名），**文件明明存在也读
不到**。这不是权限、不是长度，纯编码问题。实测同一份字节：

    D:/_env_probe/ascii_run/...          -> 成功
    D:/.../MSU1Converter-工程备份-.../... -> 失败

后果：程序放在中文目录（备份目录常带「工程备份」「游戏音乐」）时，
`--selftest` 会报 `WAV 数量 5 != 预期 7`（MP3 + SPC 两路全挂）。

本门禁做的事
------------
1. 在**临时目录里造一个中文名子目录**，把样本复制进去；
2. 解码同一份样本的「ASCII 路径」与「中文路径」，要求 PCM 逐字节相同；
3. 覆盖两条真实受影响的路径：SPC（libgme）/ MP3（miniaudio）。

修法（已落地）
--------------
  · `msu1_to_cd.gme_open()`  —— gme_open_file 失败 -> 读字节走 gme_open_data
  · `msu1_to_cd.load_mp3()`  —— decode_file 失败 -> 读字节走 miniaudio.decode

用法：
    python vendor/check_nonascii_path.py
退出码 0 = 全绿。
"""
import hashlib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import msu1_to_cd as core                                   # noqa: E402

SAMPLE_SPC = os.path.join(HERE, "sample.spc")
FAILED = []


def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (("  " + detail) if detail else ""))
    if not ok:
        FAILED.append(name)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def pcm_spc(path):
    """SPC -> 拼接的 PCM 字节（policy=id666，与主程序一致）。"""
    return b"".join(t[1] for t in core._spc_decode_file(path, "id666", 1.0))


def make_mp3(path):
    """与 msu1_gui._make_mp3_fixture 同款的合成静音 MP3（20 帧）。"""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    with open(path, "wb") as f:
        f.write(frame * 20)


def main():
    work = tempfile.mkdtemp(prefix="nonascii_")
    ascii_dir = os.path.join(work, "ascii")
    zh_dir = os.path.join(work, "工程备份 中文")
    os.makedirs(ascii_dir)
    os.makedirs(zh_dir)

    # ---------- 1. 造样本 ----------
    a_spc = os.path.join(ascii_dir, "sample.spc")
    z_spc = os.path.join(zh_dir, "sample.spc")
    shutil.copy(SAMPLE_SPC, a_spc)
    shutil.copy(SAMPLE_SPC, z_spc)
    a_mp3 = os.path.join(ascii_dir, "silence.mp3")
    z_mp3 = os.path.join(zh_dir, "静音.mp3")            # 连文件名也带中文
    make_mp3(a_mp3)
    make_mp3(z_mp3)

    # ---------- 2. SPC / libgme ----------
    print("[1] SPC 解码（libgme）")
    try:
        pa, pz = pcm_spc(a_spc), pcm_spc(z_spc)
        check("非 ASCII 路径可打开", len(pz) > 0, "%d 字节" % len(pz))
        check("PCM 与 ASCII 路径逐字节一致", sha(pa) == sha(pz), sha(pz)[:16])
        check("PCM 长度正确（非空且为 4 的倍数）",
              len(pz) > 0 and len(pz) % 4 == 0, str(len(pz)))
    except Exception as e:                                  # noqa: BLE001
        check("SPC 解码不抛异常", False, "%s: %s" % (type(e).__name__, e))

    # ---------- 3. MP3 / miniaudio ----------
    print("[2] MP3 解码（miniaudio）")
    try:
        ma = core.load_mp3(a_mp3)[0]["pcm"]
        mz = core.load_mp3(z_mp3)[0]["pcm"]
        check("非 ASCII 路径可解码", len(mz) > 0, "%d 字节" % len(mz))
        check("PCM 与 ASCII 路径逐字节一致", sha(ma) == sha(mz), sha(mz)[:16])
    except Exception as e:                                  # noqa: BLE001
        check("MP3 解码不抛异常", False, "%s: %s" % (type(e).__name__, e))

    # ---------- 4. 反向保护：真不存在的文件仍要报错 ----------
    # ⚠ gme_open 的契约是**返回 err**（不抛异常），所以这里必须看返回值，
    #   不能像调用方那样只等异常（`_decode_one` 才负责把 err 翻成 raise）。
    print("[3] 反向保护（不能把真错误吞掉）")
    ghost = os.path.join(zh_dir, "不存在.spc")
    try:
        err, _emu, _keep = core.gme_open(core._load_gme(), ghost, 44100)
        check("不存在的文件仍返回错误", bool(err),
              (err or b"").decode("latin-1", "replace")[:48])
    except OSError as e:
        check("不存在的文件仍返回错误", True, "OSError: %s" % e)

    shutil.rmtree(work, ignore_errors=True)
    print()
    if FAILED:
        print("结果: 失败 %d 项 —— %s" % (len(FAILED), "；".join(FAILED)))
        return 1
    print("结果: 全部通过（非 ASCII 路径可正常解码）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
