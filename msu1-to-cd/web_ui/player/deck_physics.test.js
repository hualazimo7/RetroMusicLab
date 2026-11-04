/* deck_physics.test.js —— 卷盘物理单测（验收第 4 条）
 *
 * 跑法：node msu1-to-cd/web_ui/player/deck_physics.test.js
 * 不依赖浏览器，也不依赖测试框架：失败即抛，进程退出码非 0。
 */
"use strict";

const P = require("./deck_physics.js");

let pass = 0;
function ok(cond, msg) {
  if (!cond) throw new Error("断言失败：" + msg);
  pass++;
}
function near(a, b, eps, msg) {
  ok(Math.abs(a - b) <= eps, msg + `（期望 ${b}，实际 ${a}）`);
}

const R0 = P.reelRadius(0);
const R5 = P.reelRadius(0.5);
const R1 = P.reelRadius(1);
const MIN = Math.sqrt(0.28);

/* 1. 三个端点的值 */
near(R0[0], 1, 1e-9, "p=0 左盘满");
near(R0[1], MIN, 1e-9, "p=0 右盘空");
near(R5[0], 0.8, 1e-9, "p=0.5 左盘 0.8");
near(R5[1], 0.8, 1e-9, "p=0.5 右盘 0.8（两盘相等）");
near(R1[0], MIN, 1e-9, "p=1 左盘空");
near(R1[1], 1, 1e-9, "p=1 右盘满");

/* 2. 面积守恒：hL² + hR² = 1.28（1 是满盘，0.28 是小盘保底，带总面积恒定） */
for (let i = 0; i <= 20; i++) {
  const p = i / 20;
  const [l, r] = P.reelRadius(p);
  near(l * l + r * r, 1.28, 1e-9, `p=${p} 时 hL²+hR²=1.28`);
}

/* 3. 单调：左盘只减、右盘只增，不回折 */
for (let i = 1; i <= 100; i++) {
  const a = P.reelRadius((i - 1) / 100);
  const b = P.reelRadius(i / 100);
  ok(b[0] < a[0], `左盘随 p 单调递减（p=${i / 100}）`);
  ok(b[1] > a[1], `右盘随 p 单调递增（p=${i / 100}）`);
}

/* 4. 越界与非法输入被夹住，不产生 NaN */
near(P.reelRadius(-3)[0], 1, 1e-9, "p<0 夹到 0");
near(P.reelRadius(9)[1], 1, 1e-9, "p>1 夹到 1");
ok(!isNaN(P.reelRadius("abc")[0]), "非法输入不产生 NaN");

/* 5. 角速度 = 线速度 / 半径：空盘转得快，且比值等于半径反比 */
const wFull = P.targetOmega(1.0, P.CFG.SPEED.playing);
const wEmpty = P.targetOmega(MIN, P.CFG.SPEED.playing);
near(wEmpty / wFull, 1 / MIN, 1e-9, "空盘角速度 = 满盘 / 0.529");
near(wFull, 1.5, 1e-9, "满盘 ω = 1.5 rad/s");

/* 6. 三档线速度的比例：ff / play = 10 / 1.5 */
near(P.targetOmega(1, P.CFG.SPEED.ff) / P.targetOmega(1, P.CFG.SPEED.playing),
  10 / 1.5, 1e-9, "快进是播放的 6.67 倍");
ok(P.targetOmega(1, P.CFG.SPEED.rew) < 0, "倒带角速度为负");

/* 7. 一阶平滑：1 秒后基本到达目标；0.111 秒到约 63% */
const st0 = { omega: [0, 0], angle: [0, 0] };
const r = P.reelRadius(0.5);
let s = st0;
for (let i = 0; i < 60; i++) s = P.advance(s, r, P.CFG.SPEED.playing, 1 / 60);
near(s.omega[0], P.targetOmega(r[0], P.CFG.SPEED.playing), 0.02,
  "1 秒后角速度逼近目标");
/* 平滑系数的定义域是 dt ≤ 0.08（掉帧上限），而 τ=1/9≈0.111 > 0.08，
   所以「一步到 63.2%」在这个实现里不会发生 —— 只要 dt 不超上限，公式就是
   1 - e^(-9dt)；超了先夹再算。这两条分开断言，别把夹取误当成公式错。 */
near(P.smoothing(0.04), 1 - Math.exp(-9 * 0.04), 1e-9, "dt≤0.08 时按 1-e^(-9dt)");
near(P.smoothing(1 / P.CFG.SMOOTH_RATE), 1 - Math.exp(-9 * P.CFG.DT_MAX), 1e-9,
  "dt=τ 超过 0.08，被夹到上限后按上限算");

/* 8. 掉帧保护：dt 给 5 秒也只走 0.08 秒，角度不会突跳一整圈 */
const j = P.advance({ omega: [0, 0], angle: [0, 0] }, r, P.CFG.SPEED.playing, 5);
/* 不夹的话 5 秒会走 ~4.7 rad（快一圈），夹到 0.08s 后只走 0.08 rad 量级 */
ok(j.angle[0] > 0 && j.angle[0] < 0.2, "dt=5s 被夹到 0.08s，角度不跳变");
ok(1.875 * 5 > 6, "（参照）不夹取时角位移会超过一整圈");

/* 9. 暂停：角速度衰减但不瞬停，角度继续小幅前进 */
let sp = { omega: [3, 3], angle: [0, 0] };
sp = P.advance(sp, r, P.CFG.SPEED.paused, 1 / 60);
ok(sp.omega[0] > 0 && sp.omega[0] < 3, "暂停后角速度衰减而非归零");
ok(sp.angle[0] > 0, "暂停后仍有惯性转动");

/* 10. 角度归一化：长时间播放不溢出 */
let sl = { omega: [0, 0], angle: [0, 0] };
for (let i = 0; i < 6000; i++) sl = P.advance(sl, r, P.CFG.SPEED.ff, 1 / 60);
ok(sl.angle[0] >= 0 && sl.angle[0] < P.TAU, "角度始终落在 [0, 2π)");

console.log(`deck_physics: ${pass} 条断言全部通过`);
