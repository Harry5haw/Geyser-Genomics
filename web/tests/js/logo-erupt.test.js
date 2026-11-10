// geyser.js's header-logo eruption, on a fake DOM: the static lockup until someone is there, then the animated lockup
// on the first hover/focus (without hover: the first touch or scroll); nothing at load or on a timer; once a session;
// never with reduced motion or without storage.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "samples", "static", "samples", "geyser.js"), "utf8");
const STATIC = "/static/samples/geyser-lockup.svg";
const ERUPT = "/static/samples/geyser-erupt-light.svg";

function load({ stored = null, storage = true, reduced = false, hoverNone = false } = {}) {
  const on = (bag) => ({
    addEventListener: (t, fn) => { (bag[t] = bag[t] || new Set()).add(fn); },
    removeEventListener: (t, fn) => bag[t] && bag[t].delete(fn),
  });
  const brandL = {};
  const winL = {};
  const brand = on(brandL);
  const img = { src: STATIC, dataset: { erupt: ERUPT }, parentNode: brand };
  const store = { "geyser-erupted": stored };
  const sessionStorage = storage
    ? { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } }
    : { getItem: () => { throw new Error("SecurityError"); }, setItem: () => { throw new Error("SecurityError"); } };
  const timers = [];
  const window = {
    ...on(winL),
    matchMedia: (q) => ({ matches: (q.includes("reduced-motion") && reduced) || (q.includes("hover: none") && hoverNone) }),
  };
  const document = {
    readyState: "complete",
    addEventListener() {},
    querySelectorAll: (q) => (q === ".brand img[data-erupt]" ? [img] : []),
  };
  const context = {
    document, window, sessionStorage, localStorage: sessionStorage, location: { href: "http://x/", origin: "http://x" },
    HTMLScriptElement: { supports: () => true },
    setTimeout: (fn, ms) => timers.push({ fn, ms }), Promise, Set, Map, Number, String, JSON, Math, Date, Array, Object,
  };
  vm.runInNewContext(src, context);
  const fire = (bag) => (t) => [...(bag[t] || [])].forEach((fn) => fn());
  const count = (bag) => Object.values(bag).reduce((n, s) => n + s.size, 0);
  return { img, store, fire: fire(brandL), fireWin: fire(winL), timers, brandL, winL, count };
}

test("the static lockup until the first hover, then the animated one, once", () => {
  const { img, store, fire, timers, brandL, count } = load();
  assert.strictEqual(img.src, STATIC); // nothing at load: page-load measurements see the static page
  assert.strictEqual(timers.length, 0);
  fire("pointerover");
  assert.strictEqual(img.src, ERUPT);
  assert.strictEqual(store["geyser-erupted"], "1");
  assert.strictEqual(count(brandL), 0); // once only: the listeners are gone
});

test("keyboard focus erupts it too", () => {
  const { img, fire } = load();
  fire("focusin");
  assert.strictEqual(img.src, ERUPT);
});

test("without hover: the first touch or scroll erupts it, never a timer", () => {
  for (const first of ["touchstart", "scroll"]) {
    const { img, fireWin, timers, winL, count } = load({ hoverNone: true });
    assert.strictEqual(img.src, STATIC);
    assert.strictEqual(timers.length, 0);
    fireWin(first);
    assert.strictEqual(img.src, ERUPT);
    assert.strictEqual(count(winL), 0);
  }
});

test("a screen with hover doesn't listen to scrolling", () => {
  const { img, fireWin, winL, count } = load();
  assert.strictEqual(count(winL), 0);
  fireWin("scroll");
  assert.strictEqual(img.src, STATIC);
});

test("still: already erupted this session, reduced motion, or no storage", () => {
  for (const opts of [{ stored: "1" }, { reduced: true }, { storage: false }]) {
    const { img, fire, fireWin, timers } = load({ ...opts, hoverNone: true });
    fire("pointerover");
    fireWin("touchstart");
    assert.strictEqual(img.src, STATIC, JSON.stringify(opts));
    assert.strictEqual(timers.length, 0);
  }
});
