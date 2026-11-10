// geyser.js's header-logo eruption, on a fake DOM: the static lockup until the page has loaded, then the empty
// geyser, then the animated lockup on the first hover/focus (without hover: the first touch or scroll); once a
// session; never on a timer, with reduced motion or without storage.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "samples", "static", "samples", "geyser.js"), "utf8");
const STATIC = "/static/samples/geyser-lockup.svg";
const EMPTY = "/static/samples/geyser-empty-light.svg";
const ERUPT = "/static/samples/geyser-erupt-light.svg";

function load({ stored = null, storage = true, reduced = false, hoverNone = false, readyState = "loading", lcpApi = false } = {}) {
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
    addEventListener: (t, fn) => { (winListeners[t] = winListeners[t] || new Set()).add(fn); },
    removeEventListener: (t, fn) => winListeners[t] && winListeners[t].delete(fn),
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
  const observers = [];
  const frames = [];
  if (lcpApi) {
    context.PerformanceObserver = class {
      constructor(cb) { this.cb = cb; this.on = true; observers.push(this); }
      observe(opts) { this.opts = opts; }
      disconnect() { this.on = false; }
      static get supportedEntryTypes() { return ["largest-contentful-paint", "paint"]; }
    };
    context.requestAnimationFrame = (fn) => frames.push(fn);
  }
  vm.runInNewContext(src, context);
  const lcp = () => observers.filter((o) => o.on).forEach((o) => o.cb({ getEntries: () => [{}] }));
  const frame = () => frames.splice(0).forEach((fn) => fn());
  const fire = (t) => [...(listeners[t] || [])].forEach((fn) => fn());
  const fireWin = (t) => [...(winListeners[t] || [])].forEach((fn) => fn());
  return { img, store, fire, fireWin, loaded: () => fireWin("load"), timers, winListeners, lcp, frame, observers };
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

test("without hover: the first touch or scroll erupts it, never a timer", () => {
  for (const first of ["touchstart", "scroll"]) {
    const { img, loaded, fireWin, timers, winListeners } = load({ hoverNone: true });
    assert.ok(!winListeners[first]); // not before load
    loaded();
    assert.strictEqual(img.src, EMPTY);
    assert.strictEqual(timers.length, 0); // nothing animates unless someone is there
    fireWin(first);
    assert.strictEqual(img.src, ERUPT);
    assert.strictEqual(winListeners.touchstart.size + winListeners.scroll.size, 0); // once only
  }
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

test("where the browser reports LCP, it rests only after load and the largest paint, a frame later", () => {
  const { img, loaded, lcp, frame, observers } = load({ lcpApi: true });
  assert.strictEqual(JSON.stringify(observers[0].opts), JSON.stringify({ type: "largest-contentful-paint", buffered: true }));
  loaded();
  frame(); frame();
  assert.strictEqual(img.src, STATIC); // loaded, but the largest paint isn't in yet (a fast machine): wait
  lcp();
  assert.strictEqual(img.src, STATIC); // not in the same frame
  frame(); frame();
  assert.strictEqual(img.src, EMPTY);
  assert.ok(!observers[0].on); // one entry is enough
});

test("the largest paint before load also waits for load", () => {
  const { img, loaded, lcp, frame } = load({ lcpApi: true });
  lcp();
  frame(); frame();
  assert.strictEqual(img.src, STATIC);
  loaded();
  frame(); frame();
  assert.strictEqual(img.src, EMPTY);
});
