#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""设计规范门禁：证明「文档 / 令牌 / 代码」三者没有漂移。

设计规范最常见的死法不是写错，而是**写完就开始烂**：代码调了 2px 内边距、
换了主题色，文档还停在三个月前，新人照着抄就抄错。本脚本把这件事变成可执行的判定：

  1. 令牌 ↔ 代码       tokens.json 的 6 套 palette / 派生色与 ``THEMES`` 逐键相等
  2. 主题完整性        每套 14 色位 + onAccent 齐全；浅色；bg < panel < card 明度阶梯
  3. 对比度            25 组 × 6 套全过，且与代码 ``_contrast_checks()`` **同源同数**
  4. 组件实测尺寸       tokens 里登记的 padding / 行高 / 厚度在 retro_theme.py 里真实存在
  5. 规范页同步         index.html 内嵌令牌与 tokens.json 完全一致（没忘重生成）
  6. 文档完整性         README.md 关键章节还在

用法（必须系统 Python 3.14，托管 3.13 没有 tkinter）::

    cd design-system
    /c/Python314/python.exe check_design_system.py

返回值：0 = 全过；1 = 有问题（每条 ✘ 都写了怎么修）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "msu1-to-cd"
sys.path.insert(0, str(APP))

import retro_theme as rt  # noqa: E402

TOKENS = HERE / "tokens.json"
INDEX = HERE / "index.html"
README = HERE / "README.md"
THEME_SRC = APP / "retro_theme.py"
GUI_SRC = APP / "msu1_gui.py"

PALETTE_KEYS = ("bg", "panel", "card", "card2", "line", "text", "muted",
                "dim", "cyan", "magenta", "amber", "green", "red", "sel")

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    mark = "✔" if ok else "✘"
    line = "  %s %s" % (mark, name)
    if detail and not ok:
        line += "\n      → " + detail.replace("\n", "\n        ")
    print(line)
    return ok


def head(title: str) -> None:
    print("\n[%s]" % title)


# ---------------------------------------------------------------------------
# 颜色工具
# ---------------------------------------------------------------------------
def _rgb(color: str) -> tuple[int, int, int]:
    h = color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lin(c: int) -> float:
    v = c / 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    r, g, b = _rgb(color)
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def lstar(color: str) -> float:
    y = luminance(color)
    return 116 * (y ** (1 / 3)) - 16 if y > 0.008856 else 903.3 * y


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def resolve(theme: dict, role: str, tokens: dict) -> str | None:
    """角色名 → 色值。先按语义映射转代码键，再查 palette / derived。"""
    key = tokens["color"]["semantics"].get(role, role)
    return (theme["palette"].get(key)
            or theme["derived"].get(key)
            or theme["palette"].get(role)
            or theme["derived"].get(role))


# ---------------------------------------------------------------------------
# 1. 令牌 ↔ 代码
# ---------------------------------------------------------------------------
def check_tokens_match_code(tokens: dict) -> None:
    head("1. 令牌 ↔ 代码（retro_theme.py 是真源）")
    color = tokens["color"]
    check(list(color["order"]) == list(rt.THEME_ORDER),
          "主题顺序与 THEME_ORDER 一致",
          "tokens=%s\n代码=%s" % (color["order"], list(rt.THEME_ORDER)))
    check(color["default"] == rt.DEFAULT_THEME,
          "默认主题一致（%s）" % rt.DEFAULT_THEME,
          "tokens=%s" % color["default"])

    keys_tok = set(color["themes"])
    keys_code = set(rt.THEMES)
    check(keys_tok == keys_code, "主题集合一致（%d 套）" % len(keys_code),
          "仅 tokens 有：%s\n仅代码有：%s" % (keys_tok - keys_code, keys_code - keys_tok))

    bad_palette, bad_label, bad_derived = [], [], []
    for key in rt.THEME_ORDER:
        if key not in color["themes"]:
            continue
        th, code = color["themes"][key], rt.THEMES[key]
        if th["palette"] != code["palette"]:
            diff = {k: (th["palette"].get(k), code["palette"].get(k))
                    for k in set(th["palette"]) | set(code["palette"])
                    if th["palette"].get(k) != code["palette"].get(k)}
            bad_palette.append("%s: %s" % (key, diff))
        if th["label"] != code["label"]:
            bad_label.append("%s: %s != %s" % (key, th["label"], code["label"]))
        # 派生色必须能由强调色现算出来（切主题后重算的规则不能被写死）
        saved = rt._current_theme
        rt.set_current(key)
        expect = {
            "onAccent": rt.THEMES[key]["on_accent"],
            "onSel": rt.PALETTE["text"],
            "accentHover": rt.mix_color(rt.PALETTE["cyan"], "#ffffff", 0.10),
            "accentPress": rt._shade(rt.PALETTE["cyan"], 0.82),
        }
        rt.set_current(saved)
        for name, val in expect.items():
            if th["derived"].get(name, "").upper() != val.upper():
                bad_derived.append("%s.%s: %s != %s" % (key, name, th["derived"].get(name), val))
    check(not bad_palette, "6 套 palette 逐键相等（84 色值）",
          "\n".join(bad_palette) + "\n修法：跑 python build_tokens.py")
    check(not bad_label, "6 套 label 一致", "\n".join(bad_label))
    check(not bad_derived, "派生色与现算值一致（onAccent / onSel / hover / press）",
          "\n".join(bad_derived) + "\n修法：跑 python build_tokens.py")


# ---------------------------------------------------------------------------
# 2. 主题完整性
# ---------------------------------------------------------------------------
def check_theme_shape(tokens: dict) -> None:
    head("2. 主题完整性 / 浅色 / 明度阶梯")
    missing, not_light, ladder = [], [], []
    for key in tokens["color"]["order"]:
        th = tokens["color"]["themes"][key]
        gap = [k for k in PALETTE_KEYS if k not in th["palette"]]
        if gap:
            missing.append("%s 缺 %s" % (key, ", ".join(gap)))
        if not th["derived"].get("onAccent"):
            missing.append("%s 缺 onAccent" % key)
        p = th["palette"]
        if not missing:
            lb, lp, lc = lstar(p["bg"]), lstar(p["panel"]), lstar(p["card"])
            if lb < 78:
                not_light.append("%s bg L*=%.1f（<78，越界成深色皮肤）" % (key, lb))
            if not (lb < lp < lc):
                ladder.append("%s bg %.1f / panel %.1f / card %.1f 未单调递增"
                              % (key, lb, lp, lc))
    check(not missing, "每套主题 14 色位 + onAccent 齐全", "\n".join(missing))
    check(not not_light, "全部为浅色皮肤（bg L* ≥ 78）", "\n".join(not_light))
    check(not ladder, "三级表面明度阶梯 bg < panel < card", "\n".join(ladder))


# ---------------------------------------------------------------------------
# 3. 对比度（含与代码清单同源校验）
# ---------------------------------------------------------------------------
def check_contrast(tokens: dict) -> None:
    head("3. 无障碍对比度（WCAG 2.1 AA）")
    pairs = tokens["contrast"]["pairs"]
    check(len(pairs) == 25, "自检清单 25 组", "实际 %d 组" % len(pairs))

    fails, drift = [], []
    total = len(pairs) * len(tokens["color"]["order"])
    passed = 0
    for key in tokens["color"]["order"]:
        th = tokens["color"]["themes"][key]
        code_set, tok_set = [], []
        saved = rt._current_theme
        rt.set_current(key)
        for label, fg, bg, mn in rt._contrast_checks():
            code_set.append((fg.upper(), bg.upper(), float(mn)))
        rt.set_current(saved)
        for p in pairs:
            fg, bg = resolve(th, p["fg"], tokens), resolve(th, p["bg"], tokens)
            if fg is None or bg is None:
                drift.append("%s: 角色 %s/%s 解析不到颜色" % (key, p["fg"], p["bg"]))
                continue
            tok_set.append((fg.upper(), bg.upper(), float(p["min"])))
            ratio = contrast(fg, bg)
            if ratio + 1e-9 >= p["min"]:
                passed += 1
            else:
                fails.append("%s · %s = %.2f（要求 %.1f）" % (key, p["label"], ratio, p["min"]))
        if sorted(code_set) != sorted(tok_set):
            only_code = [x for x in code_set if x not in tok_set]
            only_tok = [x for x in tok_set if x not in code_set]
            drift.append("%s 清单不一致；仅代码：%s；仅规范：%s" % (key, only_code, only_tok))
    check(passed == total, "6 套 × 25 组全部通过（%d/%d）" % (passed, total),
          "\n".join(fails))
    check(not drift, "规范清单与代码 _contrast_checks() 同源同数",
          "\n".join(drift) + "\n修法：以 retro_theme.py 为准，同步 build_tokens.CONTRAST_PAIRS")


# ---------------------------------------------------------------------------
# 4. 组件实测尺寸
# ---------------------------------------------------------------------------
def check_component_metrics(tokens: dict) -> None:
    head("4. 组件实测尺寸（规范里的数字必须在代码里真实存在）")
    src = THEME_SRC.read_text(encoding="utf-8")
    gui = GUI_SRC.read_text(encoding="utf-8")
    missing = []

    for name, pad in tokens["padding"].items():
        if name in ("section.gap",):
            continue
        pat = r"padding=\(%d, %d\)" % (pad["x"], pad["y"])
        if not re.search(pat, src):
            missing.append("%s padding (%d, %d) 在 retro_theme.py 里找不到"
                           % (name, pad["x"], pad["y"]))

    size_re = {
        "row.height": r"rowheight=%s",
        "progress.thickness": r"thickness=%s",
        "slider.size": r"sliderlength=%s",
        "scrollbar.width": r"\bwidth=%s",
        "combobox.arrow": r"arrowsize=%s",
        "tree.indent": r"indent=%s",
    }
    for name, pat in size_re.items():
        val = tokens["size"][name]["value"]
        if not re.search(pat % val, src):
            missing.append("%s = %s 在 retro_theme.py 里找不到" % (name, val))

    # 窗口尺寸写在 GUI 里（"1260×1020" → 1260 / 1020 两个数字都要出现）
    for name in ("window.main", "window.download", "window.dialog"):
        raw = tokens["size"][name]["value"]
        nums = re.findall(r"\d+", raw)
        for n in nums:
            if n not in gui:
                missing.append("%s 的 %s 在 msu1_gui.py 里找不到" % (name, n))
    check(not missing, "padding / 行高 / 厚度 / 窗口尺寸与代码一致",
          "\n".join(missing) + "\n修法：改代码后同步 build_tokens.py 里的 PADDINGS / SIZES")

    # 皮肤铁律：install() 里按钮必须 relief="solid"（clam 下 flat 不画边框）
    check('relief="solid"' in src, "按钮/Labelframe 使用 relief=\"solid\"（clam 的 flat 不画边框）",
          "retro_theme.install() 里找不到 relief=\"solid\"")
    # 主题必须是进程级唯一入口
    check("ttk.Style(root)" in src and 'theme_use("clam")' in src,
          "样式统一走 install() + clam（不允许界面代码自建 ttk.Style）")


# ---------------------------------------------------------------------------
# 5. 规范页同步
# ---------------------------------------------------------------------------
def check_html_sync(tokens: dict) -> None:
    head("5. 规范页同步（index.html）")
    if not INDEX.exists():
        check(False, "index.html 存在", "缺少 %s" % INDEX)
        return
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r'<script id="design-tokens" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        check(False, "找到内嵌令牌块", "index.html 里没有 <script id=\"design-tokens\">")
        return
    try:
        embedded = json.loads(m.group(1))
    except ValueError as exc:
        check(False, "内嵌令牌是合法 JSON", str(exc))
        return
    same = embedded == tokens
    check(same, "内嵌令牌与 tokens.json 完全一致",
          "两者不一致（多半是改了代码没重生成）\n修法：python build_tokens.py")

    # 页面不允许出现未登记的十六进制色值：模板里只有 :root 兜底块允许
    style = re.search(r"<style>(.*?)</style>", html, re.S)
    if style:
        css = style.group(1)
        root = re.search(r":root\{(.*?)\}", css, re.S)
        allowed = set(re.findall(r"#[0-9a-fA-F]{6}", root.group(1))) if root else set()
        allowed |= {c.upper() for th in tokens["color"]["themes"].values()
                    for c in list(th["palette"].values()) + list(th["derived"].values())}
        allowed |= {c.upper() for c in allowed}
        stray = [c for c in re.findall(r"#[0-9a-fA-F]{6}", css)
                 if c.upper() not in {a.upper() for a in allowed}]
        check(not stray, "样式表里没有未登记的色值",
              "出现 %s\n修法：色值只走 CSS 变量 / tokens.json" % sorted(set(stray)))
    check("design-tokens" in html and "JSON.parse" in html,
          "规范页仍从内嵌令牌取色（不是硬编码）")


# ---------------------------------------------------------------------------
# 6. 文档完整性
# ---------------------------------------------------------------------------
def check_readme(tokens: dict) -> None:
    head("6. 文档完整性（README.md）")
    if not README.exists():
        check(False, "README.md 存在", "缺少 %s" % README)
        return
    text = README.read_text(encoding="utf-8")
    required = [
        ("## 1. 设计原则", "设计原则"),
        ("## 3. 色彩系统", "色彩系统"),
        ("## 4. 字体", "字体"),
        ("## 5. 间距、尺寸、形状", "间距与尺寸"),
        ("## 6. 组件规范", "组件规范"),
        ("## 7. 状态矩阵", "状态矩阵"),
        ("## 9. 无障碍", "无障碍"),
        ("## 10. 新增一套主题", "主题扩展流程"),
        ("## 12. 验收门禁", "验收门禁"),
    ]
    missing = [name for anchor, name in required if anchor not in text]
    check(not missing, "9 个核心章节齐全", "缺：%s" % "、".join(missing))

    # 文档里引用的主题数量 / 对比度组数不能过期
    n_theme = len(tokens["color"]["order"])
    check(("%d 套" % n_theme) in text, "文档中的主题数量与代码一致（%d 套）" % n_theme,
          "README 里没写「%d 套」 —— 加主题后忘改文字" % n_theme)
    check("25 组" in text, "文档中的对比度组数与门禁一致（25 组）",
          "README 里没写「25 组」")


def main() -> int:
    if not TOKENS.exists():
        print("缺少 tokens.json —— 先跑 python build_tokens.py")
        return 1
    tokens = json.loads(TOKENS.read_text(encoding="utf-8"))
    print("RetroMusic Lab 设计规范门禁 · tokens v%s · 生成于 %s"
          % (tokens["meta"]["version"], tokens["meta"]["generated"]))

    check_tokens_match_code(tokens)
    check_theme_shape(tokens)
    check_contrast(tokens)
    check_component_metrics(tokens)
    check_html_sync(tokens)
    check_readme(tokens)

    ok = sum(1 for r in RESULTS if r[0])
    total = len(RESULTS)
    print("\n" + "=" * 62)
    if ok == total:
        print("PASS  %d/%d —— 文档 / 令牌 / 代码 三方一致" % (ok, total))
        return 0
    print("FAIL  %d/%d —— 有 %d 项不一致，按上面的 → 修" % (ok, total, total - ok))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
