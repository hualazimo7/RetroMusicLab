/* player_core.js —— 播放器内核（唯一状态源）
 *
 * 设计约束见 docs/deck/双风格-播放器改造方案-2026-11-04.md §4
 *   1. 皮肤不得碰 audio / viz 的节点图（不 connect、不 createMediaElementSource）
 *   2. 皮肤不得写 localStorage
 *   3. 皮肤不得发网络请求
 *   4. 皮肤不得直接查询业务 DOM
 *   5. 皮肤不得假设容器尺寸
 *
 * 内核只做四件事：持有 audio、维护 state、转发事件、管偏好。
 * 什么时候切歌、上一首是谁、封面轮播到第几张 —— 全在这里，皮肤不知道。
 */
(function (global) {
  "use strict";

  var SKIN_KEY = "rml-player-skin";
  var VOL_KEY = "rml-player-volume";
  var LAST_PREFIX = "rml-player-last:";   /* 按页面隔离：两页的曲目 id 互不相认 */
  var DEFAULT_VOL = 0.8;   /* 不用 1.0：夜里戴耳机会炸（download.js 的老教训） */

  function safeGet(k, d) {
    try { var v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; }
  }
  function safeSet(k, v) {
    try { localStorage.setItem(k, v); } catch (e) { /* 忽略：隐私模式 */ }
  }

  var Core = {
    audio: null,
    state: null,
    source: null,
    _skins: {},
    _skin: null,
    _host: null,
    _listeners: { state: [], frame: [], track: [] }
  };

  /* ── 事件 ───────────────────────────────────────── */
  Core.on = function (name, cb) {
    if (!this._listeners[name]) this._listeners[name] = [];
    this._listeners[name].push(cb);
    return cb;
  };
  Core.off = function (name, cb) {
    var a = this._listeners[name];
    if (!a) return;
    var i = a.indexOf(cb);
    if (i >= 0) a.splice(i, 1);
  };
  Core._emit = function (name, arg) {
    /* state 必须转发给当前皮肤：皮肤不是事件订阅者，它只有 onState 一个入口。
       少了这一步，先挂载的皮肤会永远停在初始画面（P0′ 实际踩过）。 */
    if (name === "state" && this._skin && this._skin.onState) {
      try { this._skin.onState(arg); } catch (e) { console.error("[core] skin.onState:", e); }
    }
    var a = this._listeners[name] || [];
    for (var i = 0; i < a.length; i++) {
      try { a[i](arg); } catch (e) { console.error("[core] listener error:", name, e); }
    }
  };

  /* ── 一次性迁移：下载页老键 "dl_vol"（0–100）→ rml-player-volume（0–1） */
  Core._legacyVol = function () {
    var old = safeGet("dl_vol", null);
    if (old === null || old === "") return DEFAULT_VOL;
    var n = parseInt(old, 10);
    return isFinite(n) ? n / 100 : DEFAULT_VOL;
  };

  Core._lastKey = function () {
    return LAST_PREFIX + ((this.source && this.source.kind) || "default");
  };

  /* ── 初始化 ─────────────────────────────────────── */
  Core.init = function (opts) {
    this.source = opts.source;
    this.audio = new Audio();
    this.audio.preload = "metadata";
    this.audio.volume = clamp01(parseFloat(safeGet(VOL_KEY, this._legacyVol())), DEFAULT_VOL);

    this.state = {
      skin: safeGet(SKIN_KEY, "deck"),
      playing: false,
      id: null, name: "", sub: "",
      dur: 0, pos: 0,
      volume: this.audio.volume,
      muted: false,
      live: false,
      status: "ok",            /* ok | missing */
      caps: this.source.caps || { prevNext: true, covers: false },
      canPrev: false, canNext: false,
      cover: null,
      vizMode: safeGet("rml-player-viz", "dots") === "bars" ? "bars" : "dots",
      /* 四组外观偏好：只存字符串，空串表示「用 aurora_theme 的默认值」 */
      appearance: {
        lightTheme: safeGet(LOOK_KEYS.lightTheme, ""),
        ambient: safeGet(LOOK_KEYS.ambient, ""),
        ambientDyn: safeGet(LOOK_KEYS.ambientDyn, ""),
        material: safeGet(LOOK_KEYS.material, "")
      },
      empty: !this._list().length
    };

    var self = this;
    this.audio.addEventListener("play", function () { self.state.playing = true; self._emit("state", self.state); });
    this.audio.addEventListener("pause", function () { self.state.playing = false; self._emit("state", self.state); });
    this.audio.addEventListener("timeupdate", function () {
      self.state.pos = self.audio.currentTime || 0;
      if (!self.state.dur) {
        var d = self.audio.duration;
        if (isFinite(d) && d > 0) self.state.dur = d;
      }
      self._emit("state", self.state);
    });
    this.audio.addEventListener("loadedmetadata", function () {
      var d = self.audio.duration;
      self.state.dur = isFinite(d) && d > 0 ? d : 0;
      self._emit("state", self.state);
    });
    this.audio.addEventListener("ended", function () { self.next(true); });
    this.audio.addEventListener("error", function () { self._fail(); });

    /* 恢复上次曲目：会话型恢复失败就静默清空，不打扰 */
    this._restore();
    return this;
  };

  /* ── 列表与索引 ─────────────────────────────────── */
  Core._list = function () {
    try { return this.source && this.source.list ? (this.source.list() || []) : []; }
    catch (e) { console.error("[core] list() failed:", e); return []; }
  };
  Core._indexOf = function (id) {
    var l = this._list();
    for (var i = 0; i < l.length; i++) {
      if (this.source.keyOf(l[i]) === id) return i;
    }
    return -1;
  };
  Core._syncBounds = function () {
    var l = this._list();
    this.state.empty = l.length === 0;
    if (!this.state.id || !this.state.caps.prevNext) {
      this.state.canPrev = false;
      this.state.canNext = false;
      return;
    }
    var i = this._indexOf(this.state.id);
    this.state.canPrev = i > 0;
    this.state.canNext = i >= 0 && i < l.length - 1;
  };

  /* ── 播放控制 ───────────────────────────────────── */
  Core.load = function (id, autoplay) {
    var l = this._list();
    var it = null;
    for (var i = 0; i < l.length; i++) if (this.source.keyOf(l[i]) === id) { it = l[i]; break; }
    if (!it) { this._fail(); return; }

    this.state.id = id;
    this.state.status = "ok";
    this.state.dur = 0;
    this.state.pos = 0;
    this.state.name = this.source.titleOf ? this.source.titleOf(it) : String(id);
    this.state.sub = this.source.subOf ? this.source.subOf(it) : "";
    this._syncBounds();
    safeSet(this._lastKey(), id);

    var url = this.source.urlOf(it);
    if (this.audio.getAttribute("src") !== url) this.audio.src = url;
    this._emit("track", it);
    this.setCoverFor(it);          /* 封面只在这里换：同游戏连播不重拉（§5.3.3） */
    this._emit("state", this.state);

    if (autoplay !== false) this.play();
  };

  Core.play = function () {
    if (!this.audio.src) {
      var l = this._list();
      if (l.length) this.load(this.source.keyOf(l[0]), true);
      return;
    }
    var p = this.audio.play();
    if (p && p.catch) p.catch(function () { /* 自动播放被拦：保持暂停态即可 */ });
  };
  Core.pause = function () { this.audio.pause(); };
  Core.toggle = function () { this.audio.paused ? this.play() : this.pause(); };

  Core.seek = function (sec) {
    var d = this.state.dur || this.audio.duration;
    if (!isFinite(d) || d <= 0) return;
    this.audio.currentTime = Math.max(0, Math.min(d, sec));
    this.state.pos = this.audio.currentTime;
    this._emit("state", this.state);
  };

  Core.next = function (auto) {
    var l = this._list();
    var i = this._indexOf(this.state.id);
    if (i < 0 || i >= l.length - 1) {
      if (auto) { this.audio.pause(); }
      return;
    }
    this.load(this.source.keyOf(l[i + 1]), true);
  };
  Core.prev = function () {
    var l = this._list();
    var i = this._indexOf(this.state.id);
    if (i <= 0) return;
    this.load(this.source.keyOf(l[i - 1]), true);
  };

  Core.setVolume = function (v) {
    v = clamp01(v, DEFAULT_VOL);
    this.audio.volume = v;
    this.state.volume = v;
    this.state.muted = v === 0;
    safeSet(VOL_KEY, String(v));
    this._emit("state", this.state);
  };
  Core.toggleMute = function () {
    if (this.state.volume > 0) {
      this._prevVol = this.state.volume;
      this.setVolume(0);
    } else {
      this.setVolume(this._prevVol || DEFAULT_VOL);
    }
  };

  Core.clear = function () {
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.state.id = null;
    this.state.name = "";
    this.state.sub = "";
    this.state.pos = 0;
    this.state.dur = 0;
    this.state.status = "ok";
    this._syncBounds();
    this._emit("state", this.state);
  };

  Core._fail = function () {
    this.state.status = "missing";
    this.state.playing = false;
    this._emit("state", this.state);
  };

  /* ── 封面（定时器只有一个，在这里）───────────────── */
  Core._coverTimer = 0;
  Core.setCoverFor = function (item) {
    var api = this.source.coverApi;

    function stop() {
      clearInterval(this._coverTimer);
      this.state.cover = null;
      this._emit("state", this.state);
    }

    if (!this.state.caps.covers || !api || !item) { stop.call(this); return; }

    var game = this.source.coverKeyOf ? this.source.coverKeyOf(item) : "";
    if (!game) { stop.call(this); return; }

    /* ⚠ 同游戏连播：**必须在 clearInterval 之前**返回。
       先清后判会让「同一游戏切下一首」把轮播定时器杀掉 —— 之后封面就再也不转了
       （P0′ 实测：3 秒过去了 idx 还是 0）。 */
    if (this.state.cover && this.state.cover.game === game) return;

    clearInterval(this._coverTimer);

    var self = this;
    fetch(api.count(game)).then(function (r) { return r.json(); }).then(function (j) {
      var n = (j && j.n) | 0;
      if (self.state.cover && self.state.cover.game === game) return;   /* 等待期间已切到同游戏 */
      if (n <= 0) { self.state.cover = null; self._emit("state", self.state); return; }
      self.state.cover = { game: game, n: n, idx: 0, url: api.url(game, 0) };
      self._emit("state", self.state);
      if (n > 1) {
        self._coverTimer = setInterval(function () {
          if (!self.state.cover || self.audio.paused) return;     /* 暂停不轮播 */
          var c = self.state.cover;
          c.idx = (c.idx + 1) % c.n;
          c.url = api.url(c.game, c.idx);
          self._emit("state", self.state);
        }, 3000);
      }
    }).catch(function () { self.state.cover = null; self._emit("state", self.state); });
  };

  /* ── 频谱（分析节点由页面侧 VizKit 建，这里只借用）─────────
   * createMediaElementSource() 对同一个 <audio> 只能调一次，第二次抛
   * InvalidStateError。两页都是 VizKit 先建（首次播放的用户手势里），
   * 所以内核**只接收、不创建**：多建一次不仅没用，还会把声音搞没。
   * 皮肤问内核要数据，不直接碰 AnalyserNode（§4 不变量 1）。
   */
  Core.analyser = null;
  Core._actx = null;
  Core._freq = null;

  Core.attachAnalyser = function (an, ctx) {
    this.analyser = an || null;
    this._actx = ctx || null;
    this._freq = this.analyser ? new Uint8Array(this.analyser.frequencyBinCount) : null;
  };

  /* 按**频率**分 cols 段（几何级数），每段取 bin 最大值 → 0..1 数组
   *
   * ⚠ 不要沿用 viz.js 的 `pow(t,1.85) * len * 0.62`：那个 0.62 是为「48 根柱」
   *   调出来的频带上限系数，段数换成 26 之后顶上 38% 的频带永远取不到，
   *   而且看不出来（§5.3.1 记过这个坑）。按 Hz 分段就没有系数可忘。
   */
  Core.spectrum = function (cols, loHz, hiHz) {
    var an = this.analyser;
    if (!an || !this._freq) return null;
    var n = an.frequencyBinCount;
    if (this._freq.length !== n) this._freq = new Uint8Array(n);
    an.getByteFrequencyData(this._freq);

    var nyq = (this._actx ? this._actx.sampleRate : 44100) / 2;
    var binHz = nyq / n;
    var lo = loHz || 60, hi = hiHz || 16000;
    var ratio = hi / lo;
    var out = new Float32Array(cols);
    for (var i = 0; i < cols; i++) {
      var b0 = Math.floor(lo * Math.pow(ratio, i / cols) / binHz);
      var b1 = Math.ceil(lo * Math.pow(ratio, (i + 1) / cols) / binHz) - 1;
      if (b0 < 0) b0 = 0;
      if (b1 >= n) b1 = n - 1;
      if (b1 < b0) b1 = b0;
      var m = 0;
      for (var b = b0; b <= b1; b++) if (this._freq[b] > m) m = this._freq[b];
      out[i] = m / 255;                 /* 硬阈值：不加 pow(v,0.72) 抬低幅，
                                           否则安静段落会整排亮（§5.3.1） */
    }
    return out;
  };

  /* 点阵 / 柱状。皮肤不能写 localStorage，偏好只能放内核（§4 不变量 2）。 */
  Core.setVizMode = function (mode) {
    mode = mode === "bars" ? "bars" : "dots";
    if (this.state.vizMode === mode) return;
    this.state.vizMode = mode;
    safeSet("rml-player-viz", mode);
    this._emit("state", this.state);
  };

  /* 时域 RMS（0..1）：氛围环的「律动」用它。
     ⚠ 与 VizKit 各读一次 getByteTimeDomainData 是安全的 —— 读的是同一份
       快照，不会互相消耗。 */
  Core.rms = function () {
    var an = this.analyser;
    if (!an) return 0;
    var n = an.fftSize;
    if (!this._wave || this._wave.length !== n) this._wave = new Uint8Array(n);
    an.getByteTimeDomainData(this._wave);
    var sum = 0;
    for (var i = 0; i < n; i++) {
      var v = (this._wave[i] - 128) / 128;
      sum += v * v;
    }
    return Math.sqrt(sum / n);
  };

  /* ── 外观偏好（四组，见 aurora_theme.js）──────────────
   * 内核只存**字符串**、不做校验：合法值表在 aurora_theme.js 里，
   * 皮肤渲染前用 AuroraTheme.resolve() 兜底。这样两边不各存一份清单。
   */
  var LOOK_KEYS = {
    lightTheme: "rml-player-light",
    ambient: "rml-player-ambient",
    ambientDyn: "rml-player-ambient-dyn",
    material: "rml-player-material"
  };

  Core.setAppearance = function (patch) {
    if (!patch) return;
    var s = this.state.appearance, changed = false;
    for (var k in LOOK_KEYS) {
      if (!Object.prototype.hasOwnProperty.call(LOOK_KEYS, k)) continue;
      if (patch[k] == null) continue;
      var v = String(patch[k]);
      if (s[k] === v) continue;
      s[k] = v;
      safeSet(LOOK_KEYS[k], v);
      changed = true;
    }
    if (changed) this._emit("state", this.state);
  };

  /* ── 皮肤 ───────────────────────────────────────── */
  Core.registerSkin = function (id, skin) { this._skins[id] = skin; };

  Core.setSkin = function (id) {
    if (!this._skins[id] || (this._skin && this._skin.id === id)) return;
    if (this._skin && this._skin.unmount) {
      try { this._skin.unmount(); } catch (e) { console.error("[core] unmount:", e); }
    }
    this._skin = this._skins[id];
    this.state.skin = id;
    safeSet(SKIN_KEY, id);
    this._host.innerHTML = "";
    /* ⚠ 不能直接赋值 className：宿主上还有页面自己的类（.dock-player 之类），
       覆盖掉会连带丢掉 flex 尺寸，播放器宽度塌成内容宽。 */
    this._host.classList.add("pv");
    this._host.classList.remove("pv--deck", "pv--aurora");
    this._host.classList.add("pv--" + id);
    this._skin.mount(this._host, this);
    this._emit("state", this.state);   /* 同时完成皮肤的首次绘制 */
  };

  Core.mount = function (host) {
    this._host = host;
    host.classList.add("pv");
    var id = this._skins[this.state.skin] ? this.state.skin : Object.keys(this._skins)[0];
    if (!id) { host.textContent = "（没有可用的皮肤）"; return; }
    this._skin = null;
    this.setSkin(id);

    /* 容器尺寸变化 → 通知皮肤（形态切换也可以纯 CSS 做，这里只是给个钩子） */
    var self = this;
    if (global.ResizeObserver) {
      this._ro = new ResizeObserver(function () {
        if (self._skin && self._skin.resize) {
          self._skin.resize(host.clientWidth, host.clientHeight);
        }
      });
      this._ro.observe(host);
    }
  };

  /* ── 恢复上次曲目 ───────────────────────────────── */
  Core._restore = function () {
    var last = safeGet(this._lastKey(), "");
    if (!last) return;
    var l = this._list();
    var hit = false;
    for (var i = 0; i < l.length; i++) if (this.source.keyOf(l[i]) === last) { hit = true; break; }
    if (!hit) { safeSet(this._lastKey(), ""); return; }
    if (this.source.resume === "persistent" || this.state.id === null) {
      this.load(last, false);   /* 不自动播：浏览器也不允许无交互自动播 */
    }
  };

  function clamp01(v, d) {
    v = parseFloat(v);
    if (!isFinite(v)) return d;
    return Math.max(0, Math.min(1, v));
  }

  Core._clamp01 = clamp01;
  global.PlayerCore = Core;
})(window);
