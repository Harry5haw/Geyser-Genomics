// surface.js's geometry (build): the 3D terrains are coloured by value through the heat ramp the grid JSON carries.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "samples", "static", "samples", "surface.js"), "utf8");
const window = {};
vm.runInNewContext(src, { window });
const { build } = window.GeyserSurface;

const RAMP = ["#000000", "#ffffff"]; // a two-stop ramp: the colour is the value's level on the scale
const grid = (values, z = {}) => ({
  x: { label: "x", unit: "u", values: [0, 1, 2], ticks: [] },
  rows: { names: values.map((_, i) => `r${i}`) },
  z: { label: "z", unit: "u", max: 4, clip: null, base: 0, ...z },
  values,
  ramp: RAMP,
});

// The front wall's vertices carry the ramp colour times a fixed 0.55 shade, so colour / 0.55 is the level.
const wallLevels = (g) => {
  const out = [];
  for (let s = 0; s < g.col.length / 36; s++) {
    const wall = s * 36 + 18; // the 7th vertex of the segment: the wall's first one
    out.push(+(g.col[wall] / 0.55).toFixed(3));
  }
  return out;
};

test("each segment starts with the colour of its value's level on the scale", () => {
  const g = build(grid([[0, 2, 4]]));
  assert.deepStrictEqual(wallLevels(g), [0, 0.5]);
});

test("the ramp comes from the grid JSON, interpolated between its stops", () => {
  const g = build({ ...grid([[1, 3, 4]]), ramp: ["#000000", "#ff0000", "#ffffff"] });
  const wall = 18; // level 0.25: halfway between black and red
  assert.deepStrictEqual([g.col[wall], g.col[wall + 1], g.col[wall + 2]].map((c) => +(c / 0.55).toFixed(3)), [0.5, 0, 0]);
});

test("values above the cap take the top colour; the baseline takes the bottom", () => {
  const g = build(grid([[32, 99, 40]], { base: 32, clip: 40, max: 99 }));
  assert.deepStrictEqual(wallLevels(g), [0, 1]);
});

test("a grid JSON without a ramp (cached from before) still builds, coloured by the ramp's two ends", () => {
  const { ramp, ...old } = grid([[0, 4, 4]]);
  const g = build(old);
  assert.ok(Math.abs(g.col[18] / 0.55 - 0x4f / 255) < 1e-3); // level 0: the low end's red channel
});
