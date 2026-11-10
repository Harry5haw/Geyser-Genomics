// geyser.js's header-logo eruption, on a fake DOM: the static lockup until the page has loaded, then the empty
// geyser, then the animated lockup on the first hover/focus (or 0.7 s after load without hover); once a session;
// never with reduced motion or without storage.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "samples", "static", "samples", "geyser.js"), "utf8");
const STATIC = "/static/samples/geyser-lockup.svg";
const EMPTY = "/static/samples/geyser-empty-light.svg";
const ERUPT = "/static/samples/geyser-erupt-light.svg";

function load({ stored = null, storage = true, reduced = false, hoverNone = false, readyState = "loading" } = {}) {
  const listeners = {};
  const brand = {
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || new Set()).add(fn); },
    removeEventListener: (t, fn) => listeners[t] && listeners[t].delete(fn),
  };
  const img = { src: STATIC, dataset: { erupt: `${EMPTY} ${ERUPT}` }, parentNode: brand };
  const store = { "geyser-erupted": stored };
  const sessionStorage = storage
    ? { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } }
    : { getItem: () => { throw new Error("SecurityError"); }, setItem: () => { throw new Error("SecurityError"); } };
  const winListeners = {};
  const timers = [];
  const window = {
    matchMedia: (q) => ({ matches: (q.includes("reduced-motion") && reduced) || (q.includes("hover: none") && hoverNone) }),
    addEventListener: (t, fn) => { winListeners[t] = fn; },
  };
  const document = {
    readyState,
    addEventListener() {},
    querySelectorAll: (q) => (q === ".brand img[data-erupt]" ? [img] : []),
  };
  const context = {
    document, window, sessionStorage, localStorage: sessionStorage, location: { href: "http://x/", origin: "http://x" },
    HTMLScriptElement: { supports: () => true },
    setTimeout: (fn, ms) => timers.push({ fn, ms }), Promise, Set, Map, Number, String, JSON, Math, Date, Array, Object,
  };
  vm.runInNewContext(src, context);
  const fire = (t) => [...(listeners[t] || [])].forEach((fn) => fn());
  return { img, store, fire, loaded: () => winListeners.load && winListeners.load(), timers };
}

test("the static lockup until load, then the empty geyser, then the eruption on the first hover", () => {
  const { img, store, fire, loaded } = load();
  assert.strictEqual(img.src, STATIC); // the first paint (the LCP image) is untouched
  loaded();
  assert.strictEqual(img.src, EMPTY);
  fire("pointerover");
  assert.strictEqual(img.src, ERUPT);
  assert.strictEqual(store["geyser-erupted"], "1");
  img.src = "changed";
  fire("pointerover"); // once only: the listeners are gone
  fire("focusin");
  assert.strictEqual(img.src, "changed");
});

test("keyboard focus erupts it too", () => {
  const { img, fire, loaded } = load();
  loaded();
  fire("focusin");
  assert.strictEqual(img.src, ERUPT);
});

test("a page that has already loaded rests at once", () => {
  const { img } = load({ readyState: "complete" });
  assert.strictEqual(img.src, EMPTY);
});

test("a hover before load erupts, and load doesn't empty it again", () => {
  const { img, fire, loaded } = load();
  fire("pointerover");
  assert.strictEqual(img.src, ERUPT);
  loaded();
  assert.strictEqual(img.src, ERUPT);
});

test("without hover it erupts 0.7 s after load", () => {
  const { img, loaded, timers } = load({ hoverNone: true });
  assert.strictEqual(timers.length, 0); // not before load
  loaded();
  assert.strictEqual(img.src, EMPTY);
  assert.deepStrictEqual(timers.map((t) => t.ms), [700]);
  timers[0].fn();
  assert.strictEqual(img.src, ERUPT);
});

test("still: already erupted this session, reduced motion, or no storage", () => {
  for (const opts of [{ stored: "1" }, { reduced: true }, { storage: false }]) {
    const { img, fire, loaded, timers } = load({ ...opts, hoverNone: true });
    loaded && loaded();
    fire("pointerover");
    assert.strictEqual(img.src, STATIC, JSON.stringify(opts));
    assert.strictEqual(timers.length, 0);
  }
});
