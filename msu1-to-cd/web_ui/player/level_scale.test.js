/* level_scale.test.js —— 点阵分级与配色单测（验收第 3 / 5 条）
 *
 * 跑法：node msu1-to-cd/web_ui/player/level_scale.test.js
 * 不依赖浏览器与测试框架：失败即抛，退出码非 0。
 */
"use strict";

const L = require("./level_scale.js");

let pass = 0;
function ok(cond, msg) {
  if (!cond) throw new Error("断言失败：" + msg);
  pass++;
}
function eq(a, b, msg) {
  ok(a === b, `${msg}（期望 ${b}，实际 ${a}）`);
}

/* 1. 16 行的换色行必须是 3 / 6 / 11 —— 这是从截图逐行采样出来的实测，
      不是按四等分推的。写成 0.25/0.5/0.75 这里就会挂。 */
const rows16 = [];
for (let r = 0; r < 16; r++) rows16.push(L.bandOf(r, 16));
eq(rows16.slice(0, 3).join(""), "000", "16 行：第 0–2 行是第 1 段（红）");
eq(rows16.slice(3, 6).join(""), "111", "16 行：第 3–5 行是第 2 段（黄）");
eq(rows16.slice(6, 11).join(""), "22222", "16 行：第 6–10 行是第 3 段（绿）");
eq(rows16.slice(11).join(""), "33333", "16 行：第 11–15 行是第 4 段（蓝）");

/* 2. 分界不是四等分：0.25 处（第 4 行中心）必须已经换色 */
ok(L.bandOf(3, 16) === 1, "第 3 行已进入第 2 段 —— 分界在 0.20，不是 0.25");
ok(L.bandOf(11, 16) === 3, "第 11 行已进入第 4 段 —— 分界在 0.70，不是 0.75");

/* 3. 段覆盖行数：3 / 3 / 5 / 5 */
const seg = L.bandsOf(16);
eq(seg.length, 4, "16 行切成 4 段");
eq(seg.map(s => s.count).join(","), "3,3,5,5", "各段行数 3/3/5/5");
eq(seg[0].from, 0, "第 1 段从底行开始");
eq(seg[3].to, 15, "第 4 段到顶行结束");

/* 4. 结构恒定性：无论多少行，最底一行永远是第 1 段、最顶一行永远是第 4 段
      （验收第 5 条：三种 --dot-pitch 下结构不变 —— 行数不随 pitch 变，
       但这条保证即使将来改行数，配色顺序也不会翻） */
for (const rows of [8, 12, 16, 20, 24, 32]) {
  eq(L.bandOf(0, rows), 0, `${rows} 行：底行是第 1 段`);
  eq(L.bandOf(rows - 1, rows), 3, `${rows} 行：顶行是第 4 段`);
  const b = L.bandsOf(rows);
  eq(b.length, 4, `${rows} 行：仍然是 4 段`);
}

/* 5. 电平 → 点亮行数 */
eq(L.litRows(0, 16), 0, "电平 0 不亮");
eq(L.litRows(1, 16), 16, "电平 1 全亮");
eq(L.litRows(0.5, 16), 8, "电平 0.5 亮 8 行");
eq(L.litRows(0.99, 16), 16, "电平 0.99 也点亮顶行（不能留一行空缺）");
eq(L.litRows(-3, 16), 0, "负电平夹到 0");
eq(L.litRows(9, 16), 16, "超界电平夹到全亮");

/* 6. 柱状模式的连续高度 */
eq(L.barRows(0.25, 16), 4, "柱高 0.25 → 4 行");
ok(Math.abs(L.barRows(1, 16) - 16) < 1e-9, "柱高 1 → 16 行");

/* 7. 配色表来自实测，顺序必须「底→顶」= 红→黄→绿→蓝 */
eq(L.palette("retro").join(","), "#ff344f,#ffc928,#38ed6c,#399bff",
  "复古四色顺序：红黄绿蓝");
eq(L.palette("cyber").join(","), "#ff2d78,#f2e51b,#22dcff,#b24fff",
  "赛博朋克顺序：品红/亮黄/青/紫");
eq(L.palette("nope").length, 4, "未知配色名回落到默认，不返回 undefined");
eq(L.UNLIT, "#23252a", "熄灭亡点用实测色，不是纯黑");

/* 8. 非法输入不产生 NaN */
ok(!isNaN(L.bandOf(NaN, 16)), "非法行号不产生 NaN");
ok(!isNaN(L.bandOf(0, 0)), "行数为 0 时不产生 NaN");
ok(!isNaN(L.litRows("abc", 16)), "非法电平不产生 NaN");

/* 9. 单调性：电平越高，点亮行数不减 */
let last = -1;
for (let i = 0; i <= 100; i++) {
  const n = L.litRows(i / 100, 16);
  ok(n >= last, `电平 ${i / 100} 时点亮行数单调不减`);
  last = n;
}

/* 10. 网格常量与 CSS 同源：CSS 写的是 calc(26 * var(--dot-pitch))，
        JS 写的是 GRID.cols。两处各改一个数就会出现「canvas 画 26 列、
        屏只有 24 列宽」这种只有截图才能发现的错位，所以在测试里对齐。
        用 indexOf 而不是正则，避免转义噪音。 */
const fs = require("fs");
const path = require("path");
const css = fs.readFileSync(path.join(__dirname, "skin_aurora.css"), "utf8");
const wantW = "width: calc(" + L.GRID.cols + " * var(--dot-pitch))";
const wantH = "height: calc(" + L.GRID.rows + " * var(--dot-pitch))";
ok(css.indexOf(wantW) >= 0, "skin_aurora.css 的宽度 = " + wantW);
ok(css.indexOf(wantH) >= 0, "skin_aurora.css 的高度 = " + wantH);

console.log(`level_scale: ${pass} 条断言全部通过`);
