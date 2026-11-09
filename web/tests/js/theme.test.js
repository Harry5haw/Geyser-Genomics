// node --test web/tests/js/*.test.js (Node 20+, no dependencies): the theme switch (static/samples/theme.js).
// The file is a browser script; in a vm context without a document only its pure core runs.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = path.join(__dirname, "..", "..", "samples", "static", "samples", "theme.js");
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(SOURCE, "utf8"), context);
const T = context.GeyserTheme;

test("a saved choice wins over the system", () => {
  assert.equal(T.resolve("dark", { contrast: true, dark: false }), "dark");
  assert.equal(T.resolve("contrast", { contrast: false, dark: true }), "contrast");
});

test("without a saved choice the system decides: contrast first, then dark, else light", () => {
  assert.equal(T.resolve(null, { contrast: true, dark: true }), "contrast");
  assert.equal(T.resolve(null, { contrast: false, dark: true }), "dark");
  assert.equal(T.resolve(null, { contrast: false, dark: false }), "light");
});

test("an unknown saved value (an old version, a typo) is ignored", () => {
  assert.equal(T.resolve("sepia", { contrast: false, dark: true }), "dark");
  assert.deepEqual([...T.THEMES], ["light", "dark", "retro", "contrast"]);
  assert.equal(T.resolve(null, { contrast: false, dark: false }), "light"); // retro only by hand
  assert.equal(T.resolve("retro", { contrast: true, dark: true }), "retro");
});

// The browser part: a tiny fake DOM, storage that throws (a private window), a system in dark mode.
test("in a page it sets data-theme before first paint, marks the pressed button and survives blocked storage", () => {
  const attrs = {};
  const events = [];
  const buttons = ["light", "dark", "retro", "contrast"].map((choice) => {
    const b = { a: { "data-theme-choice": choice } };
    b.getAttribute = (k) => b.a[k];
    b.setAttribute = (k, v) => (b.a[k] = v);
    b.closest = () => b;
    return b;
  });
  const listeners = {};
  const document = {
    documentElement: { setAttribute: (k, v) => (attrs[k] = v), getAttribute: (k) => attrs[k] },
    querySelectorAll: () => buttons,
    addEventListener: (type, fn) => (listeners[type] = fn),
    dispatchEvent: (e) => events.push(e),
  };
  const page = vm.createContext({
    document,
    CustomEvent: class {
      constructor(type, init) {
        this.type = type;
        this.detail = init.detail;
      }
    },
    localStorage: {
      getItem() {
        throw new Error("SecurityError");
      },
      setItem() {
        throw new Error("SecurityError");
      },
    },
    matchMedia: (q) => ({ matches: q === "(prefers-color-scheme: dark)", addEventListener() {} }),
  });
  vm.runInContext(fs.readFileSync(SOURCE, "utf8"), page);
  assert.equal(attrs["data-theme"], "dark"); // set while the script ran, before any paint
  assert.equal(buttons[1].a["aria-pressed"], "true");
  assert.equal(events.at(-1).type, "geyser:theme");
  listeners.click({ target: buttons[3] }); // choose high contrast: not saved (storage throws), still applied
  assert.equal(attrs["data-theme"], "contrast");
  assert.deepEqual(
    buttons.map((b) => b.a["aria-pressed"]),
    ["false", "false", "false", "true"],
  );
  assert.equal(events.at(-1).detail.theme, "contrast");
});
