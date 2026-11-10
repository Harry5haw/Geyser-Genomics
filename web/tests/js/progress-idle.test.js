// node --test web/tests/js/*.test.js: progress.js does no per-frame work on a page whose run is over.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = path.join(__dirname, "..", "..", "samples", "static", "samples", "progress.js");

const bar = (state) => {
  const attrs = {};
  const el = {
    dataset: { key: "S1", state, value: state === "succeeded" ? "1" : "0.3" },
    classList: { add() {}, remove() {}, contains: () => false },
    setAttribute: (k, v) => (attrs[k] = v),
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  return el;
};

function load(state) {
  const frames = [];
  const bars = [bar(state)];
  const context = {
    document: { querySelectorAll: (q) => (q === ".hp" ? bars : []) },
    window: {},
    requestAnimationFrame: (f) => frames.push(f),
    setInterval: () => 0,
    clearInterval() {},
    matchMedia: () => ({ matches: false }),
    Date, Math, Number, String, Object, Array,
  };
  context.window = context;
  vm.runInNewContext(fs.readFileSync(SOURCE, "utf8"), context);
  return { frames };
}

test("a finished run's bar schedules no frames", () => {
  assert.equal(load("succeeded").frames.length, 0);
  assert.equal(load("failed").frames.length, 0);
});

test("a running bar keeps the loop going", () => {
  assert.equal(load("running").frames.length, 1);
});
