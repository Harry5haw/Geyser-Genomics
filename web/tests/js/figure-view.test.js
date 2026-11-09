// geyser.js's 2D/3D buttons on /figures/, on a fake DOM: the default view (3D where WebGL works, else 2D with the
// 3D button disabled), the page-wide switch with aria-pressed, the remembered choice, and private mode (no storage).
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "samples", "static", "samples", "geyser.js"), "utf8");

const classes = () => {
  const set = new Set();
  return {
    add: (c) => set.add(c),
    contains: (c) => set.has(c),
    toggle: (c, on) => (on ? set.add(c) : set.delete(c)),
  };
};
const button = (view) => {
  const attrs = { "aria-pressed": view === "3d" ? "true" : "false" };
  const listeners = [];
  return {
    dataset: { view }, disabled: false, attrs,
    setAttribute: (k, v) => { attrs[k] = v; },
    addEventListener: (type, fn) => listeners.push(fn),
    click: () => listeners.forEach((fn) => fn()),
  };
};
const figure = () => {
  const buttons = [button("2d"), button("3d")];
  return {
    dataset: { surface: "/figures/x.json", surfaceJs: "surface.js" },
    classList: classes(),
    buttons,
    querySelectorAll: (q) => (q === "[data-view]" ? buttons : q === '[data-view="3d"]' ? [buttons[1]] : []),
    querySelector: () => null,
  };
};

function load({ webgl = true, stored = null, storage = true } = {}) {
  const figs = [figure(), figure()];
  const events = [];
  const store = { "geyser.figures.view": stored };
  const localStorage = storage
    ? { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } }
    : { getItem: () => { throw new Error("SecurityError"); }, setItem: () => { throw new Error("SecurityError"); } };
  const document = {
    documentElement: { dataset: { theme: "light" } },
    head: { append() {} },
    addEventListener() {},
    dispatchEvent: (e) => events.push(e),
    querySelectorAll: (q) => (q === "[data-surface]" ? figs : []),
    createElement: (tag) => (tag === "canvas" ? { getContext: () => (webgl ? {} : null) } : { dataset: {} }),
  };
  const context = {
    document, localStorage, window: {}, location: { href: "http://x/figures/", origin: "http://x" },
    HTMLScriptElement: { supports: () => true }, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    setTimeout, fetch: () => new Promise(() => {}), Promise, Set, Map, Number, String, JSON, Math, Date, Array, Object,
    // No IntersectionObserver: every figure counts as seen at once (the fallback path).
  };
  vm.runInNewContext(src, context);
  return { figs, store, events };
}

const pressed = (fig) => fig.buttons.map((b) => b.attrs["aria-pressed"]);

test("3D by default where WebGL works", () => {
  const { figs } = load();
  for (const fig of figs) {
    assert.ok(!fig.classList.contains("flat"));
    assert.deepStrictEqual(pressed(fig), ["false", "true"]);
    assert.strictEqual(fig.dataset.started, "1"); // the renderer starts (seen, in 3D)
  }
});

test("without WebGL: 2D, the 3D button disabled, nothing loaded", () => {
  const { figs } = load({ webgl: false });
  for (const fig of figs) {
    assert.ok(fig.classList.contains("flat") && fig.classList.contains("no3d"));
    assert.deepStrictEqual(pressed(fig), ["true", "false"]);
    assert.strictEqual(fig.buttons[1].disabled, true);
    assert.strictEqual(fig.dataset.started, undefined);
  }
});

test("a remembered 2D choice is restored, and the renderer isn't loaded", () => {
  const { figs } = load({ stored: "2d" });
  assert.ok(figs.every((f) => f.classList.contains("flat") && !f.dataset.started));
});

test("one button switches every figure, is remembered, and 3D redraws", () => {
  const { figs, store, events } = load();
  figs[0].buttons[0].click(); // 2D
  assert.ok(figs.every((f) => f.classList.contains("flat")));
  assert.deepStrictEqual(figs.map(pressed), [["true", "false"], ["true", "false"]]);
  assert.strictEqual(store["geyser.figures.view"], "2d");
  figs[1].buttons[1].click(); // back to 3D
  assert.ok(figs.every((f) => !f.classList.contains("flat")));
  assert.strictEqual(store["geyser.figures.view"], "3d");
  assert.strictEqual(events.at(-1).type, "geyser:theme");
});

test("private mode (storage throws): still works, 3D by default, nothing remembered", () => {
  const { figs } = load({ storage: false });
  assert.ok(!figs[0].classList.contains("flat"));
  figs[0].buttons[0].click();
  assert.ok(figs[0].classList.contains("flat"));
});
