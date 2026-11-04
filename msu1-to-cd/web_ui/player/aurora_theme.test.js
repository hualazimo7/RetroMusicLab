/* aurora_theme.test.js —— 极光外观参数单测
 *
 * 跑法：node msu1-to-cd/web_ui/player/aurora_theme.test.js
 * 不依赖浏览器与测试框架：失败即抛，退出码非 0。
 */
"use strict";

const T = require("./aurora_theme.js");

let pass = 0;
function ok(cond, msg) {
  if (!cond) throw new Error("断言失败：" + msg);
  pass++;
}
function eq(a, b, msg) {
  ok(a === b, `${msg}（期望 ${b}，实际 ${a}）`);
}
function near(a, b, eps, msg) {
  ok(Math.abs(a - b) <= eps, `${msg}（期望 ${b}，实际 ${a}）`);
}

/* ── 1. 频谱灯光：四套主题 ─────────────────────────── */
eq(T.LIGHT_THEMES.retro.bands.length, 4, "复古四色是 4 段");
eq(T.LIGHT_THEMES.cyber.bands.length, 4, "赛博朋克是 4 段");
eq(T.LIGHT_THEMES.gold.bands.length, 1, "鎏金是单色（实测全 16 行同色）");
eq(T.LIGHT_THEMES.silver.bands.length, 1, "冷银是单色");
eq(T.lightColor("retro", 0), "#ff344f", "复古第 1 段红");
eq(T.lightColor("retro", 3), "#399bff", "复古第 4 段蓝");
eq(T.lightColor("gold", 0), T.lightColor("gold", 15), "单色主题任意 band 同色");
eq(T.lightColor("cyber", 1), "#f2e51b", "赛博第 2 段亮黄");
eq(T.lightColor("不存在", 0), "#ff344f", "未知主题回落到默认，不返回 undefined");

/* ── 2. 周界与锚点 ──────────────────────────────────── */
const sq0 = T.perimeter(100, 100, 0);
near(sq0.total, 400, 1e-9, "直角方框周长 = 4×边长");
const circ = T.perimeter(100, 100, 50);
near(circ.total, Math.PI * 100, 1e-6, "半径 50 的圆角矩形退化成圆，周长 = πd");

const fr = T.anchorFractions(360, 200, 12);
ok(fr.topMid > 0.1 && fr.topMid < 0.2, "顶中约在周界 15%（360×200 的矩形）");
ok(fr.topMid > 0 && fr.topMid < fr.topRight, "顶中在右上角之前");
ok(fr.topRight < fr.rightLow && fr.rightLow < fr.bottomMid, "顺时针递增");
ok(fr.bottomMid < fr.bottomLeft && fr.bottomLeft < fr.leftMid, "底中 → 左下 → 左中");
ok(fr.leftMid < 1 && fr.topLeft < 1, "所有锚点都在 0..1 内");
ok(fr.topLeft > 0.95, "左上角落在环的接缝附近（>0.95）");

/* 2b. 周界取点：四条直边的中点必须落在正确坐标上 */
{
  const W = 360, H = 200, R = 10;
  const P = T.perimeter(W, H, R);
  const fe = (d) => d / P.total;
  const topMid = T.pointAt(fe(P.straightX * 0.5), W, H, R);
  near(topMid.x, R + P.straightX * 0.5, 1e-6, "取点：上边中点的 x");
  near(topMid.y, 0, 1e-6, "取点：上边中点的 y");
  const rightMid = T.pointAt(fe(P.straightX + P.arc + P.straightY * 0.5), W, H, R);
  near(rightMid.x, W, 1e-6, "取点：右边中点的 x");
  near(rightMid.y, R + P.straightY * 0.5, 1e-6, "取点：右边中点的 y");
  const bottomMid = T.pointAt(fe(P.straightX + P.arc + P.straightY + P.arc + P.straightX * 0.5), W, H, R);
  near(bottomMid.x, W - R - P.straightX * 0.5, 1e-6, "取点：底边中点（自右向左）x");
  near(bottomMid.y, H, 1e-6, "取点：底边中点的 y");
  const leftMid = T.pointAt(fe(P.straightX + P.arc + P.straightY + P.arc + P.straightX + P.arc + P.straightY * 0.5), W, H, R);
  near(leftMid.x, 0, 1e-6, "取点：左边中点的 x");
  near(leftMid.y, H - R - P.straightY * 0.5, 1e-6, "取点：左边中点（自下向上）y");
  const p0 = T.pointAt(0, W, H, R);
  const p1 = T.pointAt(1 - 1e-9, W, H, R);
  ok(Math.abs(p0.x - p1.x) < 0.02 && Math.abs(p0.y - p1.y) < 0.02, "取点：0 与 1 闭合");
  /* 所有采样点都落在盒子内 */
  let inside = true;
  for (let i = 0; i < 100; i++) {
    const q = T.pointAt(i / 100, W, H, R);
    if (q.x < -0.01 || q.x > W + 0.01 || q.y < -0.01 || q.y > H + 0.01) inside = false;
  }
  ok(inside, "取点：任意分数都落在盒子范围内");
}

/* ── 3. 氛围灯颜色 ──────────────────────────────────── */
eq(T.ringColor("off", 0.3, fr), null, "关闭 = 没有环（实测整圈消失）");
eq(T.ringColor("warm", 0.3, fr), "#a98849", "暖金是实测单色");
eq(T.ringColor("ice", 0.3, fr), "#4b85b0", "冰蓝是实测单色");
eq(T.ringColor("violet", 0.3, fr), "#7f5ab3", "紫霓是实测单色");

/* 彩虹：整圈正好一个 360° 循环，零点在顶边左端（左上圆角之后）3° */
near(T.hueOfHex(T.ringColor("rainbow", 0, fr)), 3, 1, "彩虹零点 = 顶边左端 3°");
near(T.hueOfHex(T.ringColor("rainbow", 0.5, fr)), 3 + 180, 2, "走半圈正好 +180°");
near(T.hueOfHex(T.ringColor("rainbow", 0.99, fr)), 3 + 356.4, 2, "走回零点前约 360°");

/* 赛博朋克：锚点必须落在实测色相上 */
near(T.hueOfHex(T.ringColor("cyber", fr.topLeft, fr)), 338, 2, "赛博：左上 338°");
near(T.hueOfHex(T.ringColor("cyber", fr.topMid, fr)), 311, 2, "赛博：顶中 311°");
near(T.hueOfHex(T.ringColor("cyber", fr.topRight, fr)), 272, 2, "赛博：右上 272°");
near(T.hueOfHex(T.ringColor("cyber", fr.bottomMid, fr)), 191, 2, "赛博：底中 191°（最亮处）");

/* 赛博的色相范围窄：不应出现绿色系（90–170°），这是它和彩虹最直观的区别 */
let sawGreen = false;
for (let i = 0; i < 200; i++) {
  const h = T.hueOfHex(T.ringColor("cyber", i / 200, fr));
  if (h > 100 && h < 170) sawGreen = true;
}
ok(!sawGreen, "赛博朋克环不含绿色段（实测 190°–339° 的窄弧）");

/* 彩虹反过来一定覆盖绿色 */
let sawGreen2 = false;
for (let i = 0; i < 200; i++) {
  const h = T.hueOfHex(T.ringColor("rainbow", i / 200, fr));
  if (h > 100 && h < 170) sawGreen2 = true;
}
ok(sawGreen2, "彩虹覆盖全部色相，含绿色段");

/* ── 4. 四档动态 ────────────────────────────────────── */
eq(T.ringAlpha("static", { t: 1.23 }), 1, "静态恒为 1");

near(T.ringAlpha("breathe", { t: 0 }), 0.64, 1e-6, "呼吸谷值 0.64");
near(T.ringAlpha("breathe", { t: 1.8 }), 1, 1e-6, "呼吸峰值 1.0（半周期 1.8s）");
near(T.ringAlpha("breathe", { t: 3.6 }), 0.64, 1e-6, "呼吸回到谷值（周期 3.6s）");

const beatLo = T.ringAlpha("beat", { rms: 0 });
const beatHi = T.ringAlpha("beat", { rms: 1 });
near(beatLo, 0.64, 1e-6, "律动：静音时 0.64");
near(beatHi, 1, 1e-6, "律动：满幅时 1.0");
ok(T.ringAlpha("beat", { rms: 0.3 }) > beatLo, "律动随 rms 增大而变亮");
ok(T.ringAlpha("beat", { rms: 0.3 }) < 1, "律动中间值落在区间内（不是哑的）");

/* 环绕：2 秒一圈的亮斑 */
near(T.ringAlpha("orbit", { t: 0, frac: 0 }), 1, 1e-6, "环绕：亮斑在 0 处最亮");
near(T.ringAlpha("orbit", { t: 0, frac: 0.5 }), 0.28, 0.02, "环绕：对侧几乎全暗");
near(T.ringAlpha("orbit", { t: 2.0, frac: 0 }), 1, 1e-6, "环绕：2 秒回到原位");
const orbitMid = T.ringAlpha("orbit", { t: 1.0, frac: 0.5 });
near(orbitMid, 1, 1e-6, "环绕：1 秒后亮斑转到对侧");

/* ── 5. resolve 兜底 ───────────────────────────────── */
const d = T.resolve();
eq(d.lightTheme, "retro", "默认频谱灯光：复古四色");
eq(d.ambient, "warm", "默认氛围灯：暖金");
eq(d.ambientDyn, "breathe", "默认动态：呼吸");
eq(d.material, "glass", "默认材质：亮面玻璃");
const bad = T.resolve({ lightTheme: "nope", ambient: "nope", ambientDyn: "nope", material: "nope" });
eq(bad.lightTheme, "retro", "未知主题回落默认");
eq(bad.ambient, "warm", "未知氛围回落默认");
eq(bad.material, "glass", "未知材质回落默认");

/* ── 6. 色相工具 ───────────────────────────────────── */
near(T.hueOfHex("#ff0000"), 0, 1e-6, "红 = 0°");
near(T.hueOfHex("#00ff00"), 120, 1e-6, "绿 = 120°");
near(T.hueOfHex("#0000ff"), 240, 1e-6, "蓝 = 240°");
eq(T.hueOfHex("#zzz"), null, "非法 hex 返回 null");
ok(/^#[0-9a-f]{6}$/.test(T.hslToHex(200)), "hslToHex 产出 #rrggbb");
near(T.hueOfHex(T.hslToHex(200)), 200, 2, "hslToHex → hueOfHex 往返一致");
near(T.hueOfHex(T.hslToHex(-40)), 320, 2, "负色相归一化到 320°");

console.log(`aurora_theme: ${pass} 条断言全部通过`);
