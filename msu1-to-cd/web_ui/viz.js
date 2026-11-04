/* ═══════════════════════════════════════════════════════════
   viz.js — 试听频谱 / 波形（v5.9.6）
   主界面（app.js）和下载页（download.js）**共用这一份**。

   ## 为什么不各写一份

   theme.js 的注释里已经写过同样的结论：「复制一份的话两边迟早长得不一样」。
   这块面板有两个页面的 DOM id、两种播放器（主界面是单例 `<audio>`，
   下载页也是单例但多一个音量滑块），各写一份的结果就是改了一边忘了另一边。

   ## 三个坑，踩一个就静音

   ① `createMediaElementSource()` 对同一个元素只能调一次，第二次抛
      `InvalidStateError`。而且**首次调用之后** audio 的声音就只走
      AudioNode 图了 —— 必须把 analyser 接回 `destination`，并且这条
      连接**永远不断开**（面板关掉只是不画，不是不连）。
      忘接的症状：频谱很漂亮，**但没声音**，比不画还糟。
   ② AudioContext 必须在用户手势里创建/恢复，否则 Chrome 给个
      `suspended`，频谱永远是平的。所以是**懒建**：第一次真播放时才建。
   ③ 分析器数据只在播放时有意义。暂停时画平线，**不造假振幅** ——
      「看着有声音其实没有」比空白更让人困惑。

   ## 「炫彩」不写死颜色

   从当前主题的 `--accent` / `--accent-2` 取，换复古主题时频谱跟着变
   （写死彩虹色在 6 套复古主题上会很脏）。

   ## 用法

     const viz = VizKit.create({
       audio:  audioEl,          // 必填，单例 <audio>
       canvas: "#vizCanvas",     // 必填
       wrap:   "#vizWrap",       // 可选：关掉时整块隐藏
       empty:  "#vizEmpty",      // 可选：空态提示
       toggle: "#vizTog",        // 可选：开关按钮
       key:    "msu1.viz",       // 可选：localStorage 键（默认 msu1.viz）
       bars:   48,               // 可选：柱子数（默认 48）
     });
     viz.init();                 // 页面启动时调一次
     viz.ensure(); viz.start();  // 开始播放时（必须在用户手势里）
     viz.stop();                 // 暂停 / 结束
     viz.paintEmpty();           // 曲名变了之后刷新空态
   ═══════════════════════════════════════════════════════════ */
window.VizKit = (function () {
  "use strict";

  /* 把 #8B5CF6 / rgb(139,92,246) 一律解成 [r,g,b]；解不出返回 null */
  function rgb(c) {
    const s = String(c || "").trim();
    if (s[0] === "#") {
      let h = s.slice(1);
      if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
      const n = parseInt(h.slice(0, 6), 16);
      if (h.length >= 6 && !isNaN(n)) {
        return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
      }
    }
    const m = s.match(/(\d+)[,\s]+(\d+)[,\s]+(\d+)/);
    return m ? [+m[1], +m[2], +m[3]] : null;
  }

  function mix(c1, c2, t) {
    const a = rgb(c1), b = rgb(c2);
    if (!a || !b) return c1;
    const k = Math.max(0, Math.min(1, t));
    return "rgb(" + Math.round(a[0] + (b[0] - a[0]) * k) + ","
                  + Math.round(a[1] + (b[1] - a[1]) * k) + ","
                  + Math.round(a[2] + (b[2] - a[2]) * k) + ")";
  }

  function themeColors() {
    const cs = getComputedStyle(document.documentElement);
    const pick = (n, d) => (cs.getPropertyValue(n) || "").trim() || d;
    return {
      a: pick("--accent", "#8B5CF6"),      /* 主色：高频那端 */
      b: pick("--accent-2", "#22D3EE"),    /* 副色：低频那端 */
      dim: pick("--border-2", "rgba(255,255,255,.14)"),
    };
  }

  /* 圆角柱：只圆上边（底边贴齐，圆了会露出缝隙） */
  function bar(g, x, y, w, h, r) {
    r = Math.min(r, w / 2, h);
    g.beginPath();
    g.moveTo(x, y + h);
    g.lineTo(x, y + r);
    g.quadraticCurveTo(x, y, x + r, y);
    g.lineTo(x + w - r, y);
    g.quadraticCurveTo(x + w, y, x + w, y + r);
    g.lineTo(x + w, y + h);
    g.closePath();
    g.fill();
  }

  function create(o) {
    const opt = o || {};
    const audio = opt.audio;
    const KEY = opt.key || "msu1.viz";
    const NBARS = Math.max(16, Math.min(96, opt.bars || 48));

    const V = {
      ctx: null, src: null, an: null,
      freq: null, wave: null,
      peaks: null,       /* 峰值保持（每根柱子一条「慢慢落下的小横线」） */
      raf: 0,
      on: true,
      touched: false,    /* 播放过一次 → 空态文字不再回来 */
      colors: null,
      tc: 0,             /* 上次取主题色的时间（每 500ms 刷一次，够跟手） */
      w: 0, h: 0, dpr: 1,
    };

    const $ = (s) => (typeof s === "string" ? document.querySelector(s) : s);

    /* 尺寸跟着 CSS 走，DPR 上限 2 —— 再高对这种线条图没收益，只烧 GPU。

       ⚠ 量的是**外层容器**（opt.wrap）的盒子，不是 canvas 自己的 rect。
       原因：canvas 的 CSS 尺寸在没被样式钉死时由它自己的 width/height 属性
       决定，而下面几行又拿量到的尺寸去写这两个属性 —— 一个闭环正反馈，
       每帧 ×dpr。v5.9.6 下载页的 canvas 就因为漏了 `width:100%` 的样式，
       0.35 秒内从 300 涨到 32545×16358，把 flex 侧栏撑爆、面板整块看不见。
       读容器就与 canvas 自身的属性彻底无关，链条直接断掉；
       MAXPX 是最后一道保险 —— 真失控了也只坏一块面板，不坏布局。 */
    const MAXPX = 4096;   /* 2 倍屏下正常画布也就 800~900，4096 够宽松了 */

    function resize() {
      const cv = $(opt.canvas);
      if (!cv) return false;
      const box = $(opt.wrap);
      let w, h;
      if (box) {
        w = box.clientWidth;             /* 内容盒，不含 border / 滚动条 */
        h = box.clientHeight;
      } else {
        const r = cv.getBoundingClientRect();
        w = Math.round(r.width);
        h = Math.round(r.height);
      }
      if (!w || !h) return false;        /* 面板被隐藏时是 0 */
      if (w > MAXPX) w = MAXPX;
      if (h > MAXPX) h = MAXPX;
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      if (V.w === w && V.h === h && V.dpr === dpr) return true;
      V.w = w; V.h = h; V.dpr = dpr;
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
      cv.getContext("2d").setTransform(dpr, 0, 0, dpr, 0, 0);
      return true;
    }

    function ensure() {
      if (!audio) return false;
      if (V.ctx) {
        if (V.ctx.state === "suspended") V.ctx.resume().catch(() => {});
        return true;
      }
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return false;
      try {
        const ctx = new Ctx();
        const src = ctx.createMediaElementSource(audio);
        const an = ctx.createAnalyser();
        an.fftSize = 2048;               /* 1024 个 bin，44.1k 下每 bin ≈ 21.5Hz */
        an.smoothingTimeConstant = 0.78;  /* 太跟手会闪，太滑又糊 */
        an.minDecibels = -88;
        an.maxDecibels = -18;
        src.connect(an);
        an.connect(ctx.destination);     /* ← 不接这一步，声音就没了 */
        V.ctx = ctx; V.src = src; V.an = an;
        V.freq = new Uint8Array(an.frequencyBinCount);
        V.wave = new Uint8Array(an.fftSize);
        /* 把分析节点交给播放器内核借用：皮肤要频谱，但
           createMediaElementSource 对同一个 audio 只能调一次 ——
           谁先建谁持有，这里建完把引用递过去，内核**不再重复建**。 */
        if (window.PlayerCore && window.PlayerCore.attachAnalyser) {
          window.PlayerCore.attachAnalyser(an, ctx);
        }
        return true;
      } catch (e) {
        V.ctx = V.src = V.an = null;
        return false;
      }
    }

    function draw() {
      const cv = $(opt.canvas);
      if (!cv || !resize()) return;
      const g = cv.getContext("2d");
      const W = V.w, H = V.h;
      g.clearRect(0, 0, W, H);

      const t0 = performance.now();
      if (!V.colors || t0 - V.tc > 500) { V.colors = themeColors(); V.tc = t0; }
      const C = V.colors;

      /* readyState >= 2 = HAVE_CURRENT_DATA，还没解出数据时取到的是全 0 */
      const live = !!(V.an && audio && !audio.paused && audio.readyState >= 2);
      if (live) {
        V.an.getByteFrequencyData(V.freq);
        V.an.getByteTimeDomainData(V.wave);
      }

      /* ---- ① 频谱柱：底对齐，对数取样（线性取样低频会挤成一坨） ---- */
      const gap = 2;
      const bw = (W - gap * (NBARS + 1)) / NBARS;
      const top = H * 0.12;              /* 柱子的天花板，留白给波形 */
      const room = H - top - 2;
      if (!V.peaks || V.peaks.length !== NBARS) V.peaks = new Array(NBARS).fill(0);

      /* 竖着的渐变只造一次、所有柱子共用 —— 每根单独造 gradient 是 N 倍开销，
         而这层「底暗顶亮」正是让它看着像霓虹而不是色块的关键。 */
      const vg = g.createLinearGradient(0, H, 0, top);
      vg.addColorStop(0, mix(C.a, "#000000", 0.55));
      vg.addColorStop(0.45, C.a);
      vg.addColorStop(1, C.b);

      for (let i = 0; i < NBARS; i++) {
        const t = i / (NBARS - 1);
        let v = 0;
        if (live && V.freq) {
          const bin = Math.min(V.freq.length - 1,
            Math.floor(Math.pow(t, 1.85) * V.freq.length * 0.62));
          v = V.freq[bin] / 255;
          /* 0.72 次方：低幅段抬一点，安静段落不至于整排贴地；
             高幅段基本不变，所以形状仍是真实的频谱包络，不是假振幅。 */
          v = Math.pow(v, 0.72);
        }
        const h = Math.max(1.5, v * room);
        const x = gap + i * (bw + gap);
        const y = H - h;
        g.globalAlpha = live ? 0.34 + v * 0.58 : 0.10;
        g.fillStyle = vg;
        bar(g, x, y, bw, h, 1.6);

        /* 柱顶那 2px 单独上「横向色相」：竖渐变负责霓虹质感，
           横向的青→紫负责「炫彩」。两层叠起来比任何单层都好看，
           而每帧只多 N 次 fillRect。 */
        if (live && h > 3) {
          g.globalAlpha = 0.9;
          g.fillStyle = mix(C.b, C.a, t);
          g.fillRect(x, y, bw, 2);
        }

        /* 峰值保持：掉得比柱子慢，看着有「惯性」，不是死板的等高线 */
        if (live) {
          V.peaks[i] = Math.max(h, V.peaks[i] * 0.94 - 0.4);
          if (V.peaks[i] > 3) {
            g.globalAlpha = 0.62;
            g.fillStyle = C.b;
            g.fillRect(x, H - V.peaks[i], bw, 1.4);
          }
        } else {
          V.peaks[i] = 0;
        }
      }

      /* ---- ② 波形：一条亮线压在频谱上，带同色辉光 ---- */
      if (live && V.wave) {
        const mid = H * 0.46, amp = H * 0.32;
        g.globalAlpha = 1;
        g.lineWidth = 1.6;
        g.lineJoin = "round";
        g.lineCap = "round";
        g.strokeStyle = C.b;
        g.shadowColor = C.a;
        g.shadowBlur = 9;
        g.beginPath();
        const n = V.wave.length;
        for (let i = 0; i < n; i += 2) {  /* 隔点取，视觉上没区别，省一半 */
          const x = (i / (n - 1)) * W;
          const y = mid + ((V.wave[i] - 128) / 128) * amp;
          if (i === 0) g.moveTo(x, y); else g.lineTo(x, y);
        }
        g.stroke();
        g.shadowBlur = 0;
      } else {
        /* 暂停 / 未播放：一条平线，不伪造振幅 */
        g.globalAlpha = 0.5;
        g.strokeStyle = C.dim;
        g.lineWidth = 1;
        g.beginPath();
        g.moveTo(0, H * 0.46);
        g.lineTo(W, H * 0.46);
        g.stroke();
      }
      g.globalAlpha = 1;
    }

    function loop() {
      if (!V.on) { V.raf = 0; return; }
      draw();
      V.raf = requestAnimationFrame(loop);
    }

    function start() {
      if (!V.on || V.raf) return;
      loop();
    }

    function stop() {
      if (V.raf) { cancelAnimationFrame(V.raf); V.raf = 0; }
      draw();            /* 补最后一帧，别停在半截柱子上 */
    }

    function paintEmpty() {
      const e = $(opt.empty);
      if (e) e.hidden = V.touched;
    }

    function setOn(on, save) {
      V.on = !!on;
      const b = $(opt.toggle), w = $(opt.wrap);
      if (b) {
        b.classList.toggle("on", V.on);
        b.title = V.on ? "隐藏可视化" : "显示可视化";
      }
      if (w) w.hidden = !V.on;
      if (V.on) start();
      else if (V.raf) { cancelAnimationFrame(V.raf); V.raf = 0; }
      if (save) {
        try { localStorage.setItem(KEY, V.on ? "1" : "0"); } catch (e) {}
      }
    }

    function init() {
      let on = true;
      try { on = localStorage.getItem(KEY) !== "0"; } catch (e) {}
      const b = $(opt.toggle);
      if (b) b.onclick = () => setOn(!V.on, true);
      setOn(on, false);
      paintEmpty();
    }

    return {
      get on() { return V.on; },
      get raf() { return !!V.raf; },
      get touched() { return V.touched; },
      get ctx() { return V.ctx; },
      set touched(v) { V.touched = !!v; },
      ensure: ensure,
      start: start,
      stop: stop,
      draw: draw,
      paintEmpty: paintEmpty,
      setOn: setOn,
      init: init,
      invalidateColors: function () { V.colors = null; },
    };
  }

  return { create: create };
})();
