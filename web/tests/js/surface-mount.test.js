// surface.js's mount() end to end on a fake DOM, once per theme: it must start the 3D view (a ReferenceError in
// mount, e.g. a helper shadowed by a later `let`, made every figure fall back to its poster), upload the theme's
// colours, and recolour on "geyser:theme" without throwing.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "samples", "static", "samples", "surface.js"), "utf8");

const el = (extra = {}) => {
  const node = {
    dataset: {}, style: {}, hidden: true, childNodes: [], listeners: {}, clientWidth: 640, clientHeight: 360,
    classList: { names: new Set(), add(n) { this.names.add(n); } },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    replaceChildren(...kids) { this.childNodes = kids; },
    append() {}, focus() {}, getBoundingClientRect: () => ({ left: 0, top: 0, width: 640, height: 360 }),
    querySelector: () => null,
    ...extra,
  };
  return node;
};

function page(theme) {
  const frames = { light: "#d2cbbe", dark: "#4a4a4a", retro: "#c0c0c0", contrast: "#ffffff" };
  const docListeners = {};
  const loads = [];
  const document = {
    documentElement: { dataset: { theme } },
    head: el(),
    activeElement: null,
    createElement: () => el(),
    addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); },
    dispatch(type) { for (const fn of docListeners[type] || []) fn({ type }); },
  };
  const window = {
    devicePixelRatio: 1,
    GeyserGL: () => ({ load: (geo) => loads.push(geo), draw: () => {} }), // the main-thread renderer (no Worker here)
  };
  const context = {
    window, document, setTimeout, Math, Float32Array, Promise, Map, Set, Array, JSON, Number, String, Object,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    ResizeObserver: class { observe() {} },
    getComputedStyle: () => ({ getPropertyValue: (name) => (name === "--frame" ? frames[theme] || "" : "") }),
  };
  vm.runInNewContext(src, context);
  const canvas = el();
  const parts = { canvas, ".labels": el(), ".readout": el(), ".marker": el() };
  const stage = el({ querySelector: (q) => parts[q] || null });
  const fig = el({ dataset: { surfaceGl: "surface-gl.js" }, querySelector: (q) => (q === ".stage" ? stage : null) });
  return { surface: window.GeyserSurface, document, fig, canvas, loads };
}

const data = {
  title: "t",
  x: { label: "x", unit: "u", values: [0, 1, 2], ticks: [] },
  rows: { label: "r", names: ["a", "b"] },
  z: { label: "z", unit: "u", min: 0, max: 4, clip: null, base: 0 },
  values: [[0, 2, 4], [4, 2, 0]],
  ramp: ["#00ff00", "#00ff00"],
  ramps: { turbo: ["#ff0000", "#ff0000"], "turbo-dark": ["#ff00ff", "#ff00ff"], cividis: ["#0000ff", "#0000ff"] },
};

for (const theme of ["light", "dark", "retro", "contrast"]) {
  test(`mount() starts the 3D view in the ${theme} theme and recolours on geyser:theme`, async () => {
    const { surface, document, fig, canvas, loads } = page(theme);
    await surface.mount(fig, data); // throws (rejects) on any error in mount
    assert.strictEqual(canvas.hidden, false);
    assert.ok(fig.classList.names.has("live"));
    assert.strictEqual(loads.length, 1);
    const wall = (geo) => [geo.col[18], geo.col[19], geo.col[20]].map((c) => +(c / 0.55).toFixed(3));
    const expect = { contrast: [0, 0, 1], dark: [1, 0, 1] }[theme] || [1, 0, 0];
    assert.deepStrictEqual(wall(loads[0]), expect);
    // hairlines take the theme's --frame
    const want = [1, 3, 5].map((i) => +(parseInt({ light: "#d2cbbe", dark: "#4a4a4a", retro: "#c0c0c0", contrast: "#ffffff" }[theme].slice(i, i + 2), 16) / 255).toFixed(3));
    assert.deepStrictEqual(Array.from(loads[0].lineCol.slice(0, 3)).map((c) => +c.toFixed(3)), want);
    // switch theme: new colour buffers, no throw
    document.documentElement.dataset.theme = theme === "contrast" ? "light" : "contrast";
    document.dispatch("geyser:theme");
    assert.strictEqual(loads.length, 2);
    assert.deepStrictEqual(wall(loads[1]), theme === "contrast" ? [1, 0, 0] : [0, 0, 1]);
  });
}
