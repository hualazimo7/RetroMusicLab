/* deck_physics.js —— 磁带机皮肤的走带物理（纯函数，无 DOM、无状态）
 *
 * 参数来源：Re:Play 0.7.6 的 cassette_motion 实现（见
 * docs/deck/replay-逆向分析-2026-11-04.md §5.2–§5.3）。原文摘录：
 *
 *   h = [sqrt(1 - 0.72*p), sqrt(0.28 + 0.72*p)]      // 两个卷盘的半径比
 *   v = playing ? 1.5 : ff ? 10 : rew ? -10 : 0      // 线速度
 *   ω = v / h                                        // 半径越小转得越快
 *   k = 1 - exp(-dt * 9)                             // 一阶平滑
 *   angle += ω * dt
 *
 * 为什么是平方根而不是线性：磁带长度 ∝ 半径²，恒带速下半径随剩余长度开方。
 * 线性插值在 p>0.7 之后肉眼可见偏快（`实现路径.md` §6 的旧写法已被此取代）。
 *
 * 这个文件单独存在是为了可单测：node 直接 require 就能跑，不需要浏览器。
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (root) root.DeckPhysics = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : null, function () {
  "use strict";

  var TAU2 = Math.PI * 2;

  /* 常数集中在这里，测试与皮肤共用一份，避免两边各写一个 0.72 */
  var CFG = {
    REMAIN: 0.72,      /* 半径平方的变化幅度 */
    BASE: 0.28,        /* p=1 时小盘剩余的面积比例 → 半径 sqrt(0.28)≈0.529 */
    SPEED: { playing: 1.5, ff: 10, rew: -10, paused: 0 },
    SMOOTH_RATE: 9,    /* 一阶平滑速率（1/s），等价 τ≈111ms */
    DT_MAX: 0.08       /* 单帧步长上限：切标签页回来时 dt 可能是几秒，必须夹住 */
  };

  function clamp01(v) {
    v = Number(v);
    if (!isFinite(v)) return 0;
    return v < 0 ? 0 : (v > 1 ? 1 : v);
  }

  /* 进度 → 两个卷盘的半径比 [左, 右]
     p=0   → [1.000, 0.529]  左满右空
     p=0.5 → [0.800, 0.800]  两边一样（画面最平衡）
     p=1   → [0.529, 1.000]  左空右满
     恒等式：hL² + hR² = 1.28（右侧 0.28 是小盘保底面积，不是 0）——
     这条可直接作为单测断言：带总面积在播放过程中守恒。 */
  function reelRadius(p) {
    p = clamp01(p);
    return [
      Math.sqrt(1 - CFG.REMAIN * p),
      Math.sqrt(CFG.BASE + CFG.REMAIN * p)
    ];
  }

  /* 一阶平滑系数：dt 秒内逼近目标的比例 */
  function smoothing(dt) {
    dt = Number(dt);
    if (!isFinite(dt) || dt <= 0) return 0;
    if (dt > CFG.DT_MAX) dt = CFG.DT_MAX;
    return 1 - Math.exp(-CFG.SMOOTH_RATE * dt);
  }

  /* 夹住单帧步长（秒） */
  function step(dt) {
    dt = Number(dt);
    if (!isFinite(dt) || dt <= 0) return 0;
    return dt > CFG.DT_MAX ? CFG.DT_MAX : dt;
  }

  /* 目标角速度：线速度 / 半径。半径比越小 → 角速度越大 → 空盘转得快。 */
  function targetOmega(radius, speed) {
    var r = Number(radius);
    if (!isFinite(r) || r <= 0) return 0;
    return (Number(speed) || 0) / r;
  }

  /* 一帧演进：给定当前角速度数组、半径数组、线速度与 dt，返回新的角度与角速度。
     纯函数（不改入参），便于单测「加速/减速都在逼近目标」。 */
  function advance(state, radius, speed, dt) {
    dt = step(dt);
    var k = smoothing(dt);
    var om = [0, 0], ang = [0, 0];
    for (var i = 0; i < 2; i++) {
      var target = targetOmega(radius[i], speed);
      om[i] = state.omega[i] + (target - state.omega[i]) * k;
      ang[i] = (state.angle[i] + om[i] * dt) % TAU2;
      if (ang[i] < 0) ang[i] += TAU2;
    }
    return { omega: om, angle: ang };
  }

  return {
    CFG: CFG,
    SPEED: CFG.SPEED,      /* 顶层别名：皮肤写 DP.SPEED.playing 更顺 */
    TAU: TAU2,
    clamp01: clamp01,
    reelRadius: reelRadius,
    smoothing: smoothing,
    step: step,
    targetOmega: targetOmega,
    advance: advance
  };
});
