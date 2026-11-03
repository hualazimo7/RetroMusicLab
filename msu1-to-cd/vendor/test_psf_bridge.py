# -*- coding: utf-8 -*-
"""v4.40 门禁：PSF（PS1/PS2 芯片音源）→ WAV 的半自动桥接。

全离线：用临时目录伪造 foobar2000 便携版的目录结构
（``portable_mode_enabled`` + ``profile/converter presets``），
不需要真的装 foobar2000，也不会去动用户真实的 profile。

覆盖三块：
  ① 格式登记 —— PSF 家族六种扩展名都认得，文案不再是空括号
  ② psf_bridge —— 找 profile / 写预设（不覆盖）/ 收集曲目 / 指引文案
  ③ GUI 侧 —— ``_psf_pack_dir`` 对文件夹、单文件、非 PSF 的判定

跑法：``python vendor/test_psf_bridge.py``
"""

import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

import chip_formats as cf
import psf_bridge as pb

_n_pass = 0
_n_fail = 0


def ok(cond, label, extra=""):
    global _n_pass, _n_fail
    if cond:
        _n_pass += 1
        print("  PASS %s  %s" % (label, extra))
    else:
        _n_fail += 1
        print("  FAIL %s  %s" % (label, extra))


def _mk_foobar(root, portable=True):
    """伪造一个 foobar2000 目录；返回 exe 路径。"""
    os.makedirs(root, exist_ok=True)
    exe = os.path.join(root, "foobar2000.exe")
    with open(exe, "wb") as fh:
        fh.write(b"MZ")                      # 内容无所谓，只要是个文件
    if portable:
        with open(os.path.join(root, "portable_mode_enabled"), "w") as fh:
            fh.write("")
        os.makedirs(os.path.join(root, "profile"), exist_ok=True)
    return exe


def _mk_psf_pack(folder, n=3, lib=True, sub=False):
    os.makedirs(folder, exist_ok=True)
    for i in range(n):
        with open(os.path.join(folder, "TRACK_%04d.minipsf" % i), "wb") as fh:
            fh.write(b"PSF\x01")
    if lib:
        with open(os.path.join(folder, "GAME.psflib"), "wb") as fh:
            fh.write(b"PSF\x01")
    if sub:
        d = os.path.join(folder, "sub")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "NESTED.minipsf"), "wb") as fh:
            fh.write(b"PSF\x01")


print("=" * 62)
print("① 格式登记")
print("=" * 62)

PSF_ALL = (".psf", ".psf1", ".minipsf", ".minipsf1", ".psf2", ".minipsf2")
# v5.2/v5.4: PSF 家族已**原生支持**（psf_native → psf2wav.exe），
# 从 P2_EXTS 移到了 PSF_EXTS —— 断言目标相应翻转。
missing = [e for e in PSF_ALL if e not in cf.PSF_EXTS]
ok(not missing, "PSF 家族六种扩展名都在 PSF_EXTS", "缺 %s" % missing if missing else "")
gone = [e for e in PSF_ALL if e in cf.P2_EXTS]
ok(not gone, "PSF 家族已不在 P2_EXTS（原生支持了，不再是「暂不支持」）",
   "残留 %s" % gone if gone else "")
ok(cf.SYSTEM_NAMES.get("PSF") == "PlayStation",
   "SYSTEM_NAMES 有 PSF → PlayStation", cf.SYSTEM_NAMES.get("PSF"))
ok(cf.SYSTEM_NAMES.get("PSF2") == "PlayStation 2",
   "SYSTEM_NAMES 有 PSF2 → PlayStation 2", cf.SYSTEM_NAMES.get("PSF2"))
ok(set(cf.PSF_EXTS) == set(pb.PSF_TRACK_EXTS),
   "chip_formats.PSF_EXTS 与 psf_bridge 一致（防两边漂移）")

_tmp = tempfile.mkdtemp(prefix="psfbr_")
try:
    f = os.path.join(_tmp, "GAME_0001.minipsf")
    with open(f, "wb") as fh:
        fh.write(b"PSF\x01")
    msg = cf.unsupported_reason(f)
    # v5.2/v5.4: PSF 已**原生支持**（psf_native → psf2wav.exe），
    # 再说什么「需要 foobar2000」就是误导 —— 断言翻转为「点名原生支持、
    # 说明是解码失败」，并且绝不能把能转的格式说成「不认识的扩展名」。
    ok("PSF" in msg and "已原生支持" in msg,
       "PSF 文案点名原生支持（不再指向 foobar2000）", msg[:60])
    ok("foobar2000" not in msg,
       "PSF 文案不再要求用户装 foobar2000", msg[:60])
    ok("不认识" not in msg,
       "PSF 文案不说「不认识的扩展名」（已原生支持）", msg[:60])
    ok("（）" not in msg and "()" not in msg,
       "不再出现空括号（SYSTEM_NAMES 补全前的老 bug）")

    print()
    print("=" * 62)
    print("② psf_bridge")
    print("=" * 62)

    fb = _mk_foobar(os.path.join(_tmp, "foobar2000"))
    prof = pb.profile_dir(fb)
    ok(prof == os.path.join(os.path.dirname(fb), "profile"),
       "便携版 profile 认到 portable_mode_enabled", prof)

    pdir = pb.preset_dir(fb)
    ok(pdir == os.path.join(prof, "converter presets"),
       "预设目录 = <profile>/converter presets")

    out = os.path.join(_tmp, "OUT")
    r1 = pb.ensure_wav_preset(fb, out)
    ok(r1["ok"] and r1["created"], "首次写入预设成功", r1["path"])
    body = open(r1["path"], encoding="utf-8").read()
    ok(pb.WAV_ENCODER_ID in body, "预设里是 WAV 编码器 GUID")
    ok("%filename%" in body, "文件名用 %filename%（PSF 常没有 title）")
    ok(out.replace("\\", "/") in body or out in body,
       "输出目录写进了预设", out)

    r2 = pb.ensure_wav_preset(fb, os.path.join(_tmp, "OTHER"))
    ok(r2["ok"] and not r2["created"], "再次调用不重写（created=False）")
    ok(open(r1["path"], encoding="utf-8").read() == body,
       "重复调用不改内容（不会把输出目录改掉）")

    # 用户自己改过的同名预设：必须原样保留
    with open(r1["path"], "w", encoding="utf-8") as fh:
        fh.write("encodingID=SENTINEL\n")
    r3 = pb.ensure_wav_preset(fb, out)
    ok(r3["ok"] and not r3["created"], "同名预设已存在时不覆盖")
    ok(open(r1["path"], encoding="utf-8").read() == "encodingID=SENTINEL\n",
       "用户改过的预设内容原样保留")

    r4 = pb.ensure_wav_preset("", out)
    ok(not r4["ok"] and r4["error"], "没有 foobar2000 时给出明确错误",
       r4["error"])

    # 非便携版（没有 portable_mode_enabled）：profile 只能靠 %APPDATA%
    fb2 = _mk_foobar(os.path.join(_tmp, "installed"), portable=False)
    _old_appdata = os.environ.get("APPDATA")
    fake_appdata = os.path.join(_tmp, "appdata")
    os.makedirs(os.path.join(fake_appdata, "foobar2000-v2"), exist_ok=True)
    os.environ["APPDATA"] = fake_appdata
    try:
        got = pb.profile_dir(fb2)
        ok(got == os.path.join(fake_appdata, "foobar2000-v2"),
           "安装版走 %APPDATA%\\foobar2000-v2", got)
    finally:
        if _old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = _old_appdata

    pack = os.path.join(_tmp, "pack")
    _mk_psf_pack(pack, n=4, lib=True, sub=True)
    tr = pb.collect_tracks(pack)
    ok(len(tr) == 4, "收集到 4 首曲目", [os.path.basename(x) for x in tr])
    ok(all(x.lower().endswith(".minipsf") for x in tr), "不把 .psflib 当曲目")
    ok(pb.count_tracks(pack) == 4, "count_tracks 与 collect_tracks 一致")
    ok(not any("sub" in x for x in tr), "只扫一层（子目录的曲目不算）")

    g = pb.guidance(fb, pack, out, n_tracks=4)
    ok("Ctrl+A" in g and "Convert" in g, "指引含「全选 + Convert」两步")
    ok(str(out) in g, "指引里写明输出目录")
    ok("4 首" in g, "指引里写明曲目数")
    ok("Output format" in g, "指引带兜底：输出不是 WAV 时怎么改预设")
    g2 = pb.guidance("", pack, out)
    ok("没有找到 foobar2000" in g2, "没装 foobar2000 时指引改成安装提示")

    print()
    print("=" * 62)
    print("③ GUI 侧判定")
    print("=" * 62)

    import msu1_gui as g_ui
    ok(g_ui.psf_bridge is not None, "GUI 已导入 psf_bridge")
    ok(g_ui._psf_pack_dir(pack) == pack, "文件夹 → 判为 PSF 包")
    ok(g_ui._psf_pack_dir(os.path.join(pack, "TRACK_0000.minipsf")) == pack,
       "单个 .minipsf → 判为其所在包")
    ok(g_ui._psf_pack_dir(os.path.join(_tmp, "OUT")) == "",
       "非 PSF 文件夹 → 不判为包")
    ok(g_ui._psf_pack_dir("") == "", "空路径 → 不判为包")
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

print()
print("=" * 62)
print("结果: %d 通过 / %d 失败" % (_n_pass, _n_fail))
print("=" * 62)
sys.exit(1 if _n_fail else 0)
