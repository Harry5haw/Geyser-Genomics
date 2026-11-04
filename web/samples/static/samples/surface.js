/* Geyser 3D terrains: a small hand-rolled WebGL renderer for the /figures/ grids (web plan W1b). No library.
   Loaded by geyser.js only when a figure scrolls into view (or "Rotate in 3D" is pressed); draws only when
   something changes, so an idle chart costs nothing. Each row is a category (a sample, a read file), so it is
   drawn as its own ribbon and nothing is interpolated between rows. */
(() => {
  "use strict";

  // The sequential ramp (dataviz reference, one hue): dark = low, receding into the page; light = high.
  const RAMP = ["#0d366b", "#104281", "#184f95", "#1c5cab", "#256abf", "#2a78d6", "#3987e5", "#5598e7",
    "#6da7ec", "#86b6ef", "#9ec5f4", "#b7d3f6", "#cde2fb"].map((h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255));
  const ramp = (t) => {
    t = Math.min(1, Math.max(0, t)) * (RAMP.length - 1);
    const i = Math.min(RAMP.length - 2, Math.floor(t)), f = t - i;
    return [0, 1, 2].map((k) => RAMP[i][k] + (RAMP[i + 1][k] - RAMP[i][k]) * f);
  };
  const LINE = [0.18, 0.54, 0.41]; // the table frame colour (#2f8a68): hairlines one step off the surface
  const H = 0.75, D = 0.9; // terrain height and half depth, in world units (x spans -1..1)

  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const norm = (a) => { const l = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / l, a[1] / l, a[2] / l]; };
  const mul = (a, b) => { // column-major 4x4
    const o = new Array(16);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
      let s = 0;
      for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
      o[c * 4 + r] = s;
    }
    return o;
  };
  const perspective = (fov, aspect, n, f) => {
    const t = 1 / Math.tan(fov / 2);
    return [t / aspect, 0, 0, 0, 0, t, 0, 0, 0, 0, (f + n) / (n - f), -1, 0, 0, (2 * f * n) / (n - f), 0];
  };
  const lookAt = (eye, at) => {
    const z = norm(sub(eye, at)), x = norm(cross([0, 1, 0], z)), y = cross(z, x);
    return [x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0, -dot(x, eye), -dot(y, eye), -dot(z, eye), 1];
  };
  const fmt = (v) => (v === 0 ? "0" : Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2));

  function build(data) {
    const xs = data.x.values, rows = data.rows.names, vals = data.values, n = rows.length;
    const x0 = xs[0], x1 = xs[xs.length - 1] === x0 ? x0 + 1 : xs[xs.length - 1];
    const top = data.z.clip || data.z.max || 1, base = data.z.base || 0, span = top - base || 1;
    const wx = (v) => -1 + ((v - x0) / (x1 - x0)) * 2;
    const level = (v) => Math.max(0, Math.min(v, top) - base) / span; // 0..1: height and colour alike
    const height = (v) => level(v) * H;
    const pitch = (2 * D) / n, half = pitch * 0.34;
    const zc = (i) => -D + (i + 0.5) * pitch; // row 0 at the back
    const L = norm([0.35, 1, 0.55]), m = xs.length;
    // Straight into typed arrays (12 vertices per segment: the top quad and the front wall), no per-vertex objects.
    let segments = 0;
    for (const line of vals) for (let j = 0; j + 1 < m; j++) if (line[j] != null && line[j + 1] != null) segments++;
    const pos = new Float32Array(segments * 36), col = new Float32Array(segments * 36), points = [];
    let o = 0;
    const put = (x, y, z, c, k) => {
      pos[o] = x; pos[o + 1] = y; pos[o + 2] = z;
      col[o] = c[0] * k; col[o + 1] = c[1] * k; col[o + 2] = c[2] * k;
      o += 3;
    };
    const X = xs.map(wx);
    for (let i = 0; i < n; i++) {
      const z = zc(i), line = vals[i], zf = z + half, zb = z - half;
      const Y = line.map((v) => (v == null ? null : height(v))), C = line.map((v) => (v == null ? null : ramp(level(v))));
      const shade = (j) => { // light from the slope along x, baked into the colour
        const a = Y[Math.max(0, j - 1)], b = Y[Math.min(m - 1, j + 1)];
        if (a == null || b == null) return 0.85;
        const dx = X[Math.min(m - 1, j + 1)] - X[Math.max(0, j - 1)] || 1e-6, len = Math.hypot(b - a, dx);
        return 0.55 + 0.45 * Math.max(0, (-(b - a) * L[0] + dx * L[1]) / len);
      };
      for (let j = 0; j < m; j++) {
        if (Y[j] != null) points.push({ row: i, col: j, p: [X[j], Y[j], z] });
        if (j + 1 >= m || Y[j] == null || Y[j + 1] == null) continue;
        const xa = X[j], xb = X[j + 1], ya = Y[j], yb = Y[j + 1], ca = C[j], cb = C[j + 1], sa = shade(j), sb = shade(j + 1);
        put(xa, ya, zf, ca, sa); put(xa, ya, zb, ca, sa); put(xb, yb, zf, cb, sb); // top
        put(xa, ya, zb, ca, sa); put(xb, yb, zb, cb, sb); put(xb, yb, zf, cb, sb);
        put(xa, 0, zf, ca, 0.55); put(xa, ya, zf, ca, 0.55); put(xb, 0, zf, cb, 0.55); // front wall
        put(xa, ya, zf, ca, 0.55); put(xb, yb, zf, cb, 0.55); put(xb, 0, zf, cb, 0.55);
      }
    }
    const lines = [], l = (a, b) => lines.push(...a, ...b);
    l([-1, 0, -D], [1, 0, -D]); l([1, 0, -D], [1, 0, D]); l([1, 0, D], [-1, 0, D]); l([-1, 0, D], [-1, 0, -D]);
    l([-1, 0, -D], [-1, H, -D]);
    for (let k = 1; k < 4; k++) l([-1 + k / 2, 0, -D], [-1 + k / 2, 0, D]);
    for (let k = 1; k <= 2; k++) l([-1, (H * k) / 2, -D], [1, (H * k) / 2, -D]);
    const labels = [];
    const ticks = data.x.ticks && data.x.ticks.length ? data.x.ticks // named positions (batch sizes on a log2 axis)
      : [0, 1, 2, 3, 4].map((k) => [x0 + ((x1 - x0) * k) / 4, `${+(x0 + ((x1 - x0) * k) / 4).toPrecision(3)}`]);
    for (const [v, text] of ticks) labels.push({ p: [wx(v), 0, D + 0.14], text });
    labels.push({ p: [0, 0, D + 0.32], text: `${data.x.label} (${data.x.unit})`, cls: "axis" });
    for (let k = 0; k <= 2; k++) labels.push({ p: [-1.06, (H * k) / 2, -D], text: fmt(base + (span * k) / 2), cls: "z" });
    labels.push({ p: [-1, H + 0.12, -D], text: `${data.z.label} (${data.z.unit})`, cls: "axis" });
    rows.forEach((name, i) => labels.push({ p: [1.04, 0, zc(i)], text: name, cls: "row" }));
    return { pos, col, lines: new Float32Array(lines), labels, points, top, xs, rows, vals };
  }

  // Yield to the browser between the setup steps, so no single task blocks input (TBT ~0 on slow phones).
  const breathe = () => new Promise((ok) => setTimeout(ok, 0));

  // The GL half (surface-gl.js): in a Worker on an OffscreenCanvas where the browser can (context creation and
  // drawing off the main thread), else the same file as a plain script drawing here.
  async function renderer(fig, canvas, geometry) {
    const url = fig.dataset.surfaceGl;
    if (canvas.transferControlToOffscreen && window.Worker) {
      const worker = new Worker(url);
      const offscreen = canvas.transferControlToOffscreen();
      const transfer = [offscreen, geometry.pos.buffer, geometry.col.buffer, geometry.lines.buffer, geometry.lineCol.buffer];
      const ready = new Promise((ok, fail) => {
        worker.onmessage = (e) => (e.data.type === "ready" ? ok() : fail(new Error(e.data.reason)));
        worker.onerror = () => fail(new Error("renderer worker failed"));
      });
      worker.postMessage({ type: "init", canvas: offscreen, geometry }, transfer);
      await ready;
      return { draw: (view) => worker.postMessage({ type: "draw", view }) };
    }
    if (!window.GeyserGL) {
      await new Promise((ok, fail) => {
        const s = document.createElement("script");
        s.src = url;
        s.onload = ok;
        s.onerror = fail;
        document.head.append(s);
      });
    }
    const gl = window.GeyserGL(canvas);
    gl.load(geometry);
    return gl;
  }

  async function mount(fig, data) {
    const stage = fig.querySelector(".stage"), canvas = stage.querySelector("canvas");
    const g = build(data);
    const lineCol = new Float32Array(g.lines.length);
    for (let k = 0; k < lineCol.length; k += 3) lineCol.set(LINE, k);
    await breathe();
    const gl = await renderer(fig, canvas, { pos: g.pos, col: g.col, lines: g.lines, lineCol });
    const overlay = stage.querySelector(".labels"), readout = stage.querySelector(".readout"), marker = stage.querySelector(".marker");
    overlay.replaceChildren(...g.labels.map((lb) => {
      const s = document.createElement("span");
      s.textContent = lb.text; // labels come from CSV headers: text, never HTML
      if (lb.cls) s.className = lb.cls;
      return s;
    }));
    const HOME = { yaw: -0.55, pitch: 0.5, dist: 3.3 };
    const view = { ...HOME };
    let matrix = null, frame = 0, cursor = null, W = 1, Hpx = 1; // the stage's size, read once per draw
    const at = [0, H * 0.2, 0.12]; // a little below and in front of the centre: the front floor edge and its ticks stay in the box

    const project = (p) => {
      const m = matrix, x = p[0], y = p[1], z = p[2];
      const cx = m[0] * x + m[4] * y + m[8] * z + m[12], cy = m[1] * x + m[5] * y + m[9] * z + m[13];
      const cw = m[3] * x + m[7] * y + m[11] * z + m[15];
      return [((cx / cw + 1) / 2) * W, ((1 - cy / cw) / 2) * Hpx, cw]; // no layout read here: labels are written in between
    };
    const draw = () => {
      frame = 0;
      const w = (W = stage.clientWidth), h = (Hpx = stage.clientHeight), dpr = Math.min(2, window.devicePixelRatio || 1);
      const eye = [at[0] + view.dist * Math.cos(view.pitch) * Math.sin(view.yaw), at[1] + view.dist * Math.sin(view.pitch),
        at[2] + view.dist * Math.cos(view.pitch) * Math.cos(view.yaw)];
      matrix = mul(perspective(0.75, w / h || 1, 0.1, 50), lookAt(eye, at));
      gl.draw({ matrix, width: Math.round(w * dpr), height: Math.round(h * dpr) });
      let lastRowY = -1e9; // row labels closer than 16 px to the last one shown are skipped (the readout names every row)
      overlay.childNodes.forEach((s, k) => {
        const [x, y, depth] = project(g.labels[k].p);
        s.style.transform = `translate(${x.toFixed(1)}px,${y.toFixed(1)}px)`;
        let visible = depth > 0;
        if (visible && g.labels[k].cls === "row") {
          visible = Math.abs(y - lastRowY) >= 16;
          if (visible) lastRowY = y;
        }
        s.style.visibility = visible ? "" : "hidden";
      });
      show(cursor);
    };
    const redraw = () => { if (!frame) frame = requestAnimationFrame(draw); };

    const show = (pt) => {
      cursor = pt;
      if (!pt || !matrix) {
        readout.hidden = marker.hidden = true;
        return;
      }
      const v = g.vals[pt.row][pt.col], [x, y] = project(pt.p);
      marker.hidden = readout.hidden = false;
      marker.style.transform = `translate(${x.toFixed(1)}px,${y.toFixed(1)}px)`;
      const strong = document.createElement("strong");
      strong.textContent = `${fmt(v)} ${data.z.unit}` + (v > g.top ? ` (drawn at the ${fmt(g.top)} cap)` : "");
      const small = document.createElement("span");
      const tick = (data.x.ticks || []).find((t) => t[0] === g.xs[pt.col]); // a named position reads as its name
      small.textContent = `${g.rows[pt.row]} · ${data.x.label} ${tick ? tick[1] : `${g.xs[pt.col]} ${data.x.unit}`}`;
      readout.replaceChildren(strong, small);
    };
    const nearest = (mx, my) => {
      const r = g.points.length < 100 ? 64 : 24; // the reader only has to be closest; sparse grids get a wider reach
      let best = null, bestD = r * r;
      for (const pt of g.points) {
        const [x, y] = project(pt.p), d = (x - mx) ** 2 + (y - my) ** 2;
        if (d < bestD) { bestD = d; best = pt; }
      }
      return best;
    };

    // Pointer: drag rotates, two pointers pinch-zoom, hover reads values. Wheel zooms only once the chart has
    // focus (a click or Tab), so scrolling the page past it is never captured.
    const ptrs = new Map();
    let pinch = 0;
    canvas.addEventListener("pointerdown", (e) => {
      canvas.focus({ preventScroll: true });
      canvas.setPointerCapture(e.pointerId);
      ptrs.set(e.pointerId, [e.clientX, e.clientY]);
      if (ptrs.size === 2) { const [a, b] = [...ptrs.values()]; pinch = Math.hypot(a[0] - b[0], a[1] - b[1]); }
    });
    canvas.addEventListener("pointermove", (e) => {
      const prev = ptrs.get(e.pointerId);
      if (!prev) {
        const r = canvas.getBoundingClientRect();
        show(nearest(e.clientX - r.left, e.clientY - r.top));
        return;
      }
      ptrs.set(e.pointerId, [e.clientX, e.clientY]);
      if (ptrs.size === 2) {
        const [a, b] = [...ptrs.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]);
        if (pinch) zoom(pinch / d);
        pinch = d;
      } else {
        view.yaw -= (e.clientX - prev[0]) * 0.008;
        view.pitch = Math.min(1.45, Math.max(0.05, view.pitch + (e.clientY - prev[1]) * 0.006));
        redraw();
      }
    });
    const release = (e) => { ptrs.delete(e.pointerId); pinch = 0; };
    canvas.addEventListener("pointerup", release);
    canvas.addEventListener("pointercancel", release);
    canvas.addEventListener("pointerleave", () => { if (!ptrs.size) show(null); });
    const zoom = (f) => { view.dist = Math.min(7, Math.max(1.6, view.dist * f)); redraw(); };
    canvas.addEventListener("wheel", (e) => {
      if (document.activeElement !== canvas) return;
      e.preventDefault();
      zoom(Math.exp(e.deltaY * 0.0012));
    }, { passive: false });
    const reset = () => { Object.assign(view, HOME); redraw(); };
    canvas.addEventListener("dblclick", reset);

    // Keyboard: arrows rotate, +/- zoom, 0 resets; Shift+arrows walk the readout over the cells (the same values
    // hover shows, without a pointer).
    canvas.addEventListener("keydown", (e) => {
      const k = e.key;
      if (e.shiftKey && k.startsWith("Arrow")) {
        const c = cursor || g.points[0];
        const row = Math.min(g.rows.length - 1, Math.max(0, c.row + (k === "ArrowDown" ? 1 : k === "ArrowUp" ? -1 : 0)));
        const colStep = k === "ArrowRight" ? 1 : k === "ArrowLeft" ? -1 : 0;
        const inRow = g.points.filter((p) => p.row === row);
        if (!inRow.length) return;
        const target = Math.min(g.xs.length - 1, Math.max(0, c.col + colStep));
        show(inRow.reduce((a, p) => (Math.abs(p.col - target) < Math.abs(a.col - target) ? p : a)));
      } else if (k === "ArrowLeft" || k === "ArrowRight") { view.yaw += k === "ArrowLeft" ? 0.12 : -0.12; redraw(); }
      else if (k === "ArrowUp" || k === "ArrowDown") { view.pitch = Math.min(1.45, Math.max(0.05, view.pitch + (k === "ArrowUp" ? 0.08 : -0.08))); redraw(); }
      else if (k === "+" || k === "=") zoom(0.88);
      else if (k === "-" || k === "_") zoom(1.14);
      else if (k === "0" || k === "Home") reset();
      else if (k === "Escape") show(null);
      else return;
      e.preventDefault();
    });

    await breathe();
    new ResizeObserver(redraw).observe(stage);
    canvas.hidden = false;
    fig.classList.add("live"); // CSS hides the poster: the canvas takes its place in the same box, nothing shifts
    const button = fig.querySelector("[data-surface-start]"); // the same button, relabelled: no layout change
    if (button) {
      button.textContent = "Reset view";
      button.addEventListener("click", reset);
    }
    draw();
  }

  window.GeyserSurface = { mount, build };
})();
