// The 3D geometry built from the real coverage grid (fixtures/coverage-grid.json, kept equal to figures.py's output
// by web/tests/test_web_figures.py) uses the rainbow: its wall colours fall in >= 5 of Turbo's bands. Without the
// knots (a grid from before, coloured on the linear height scale) the same data reaches fewer: the "mostly green".
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "samples", "static", "samples", "surface.js"), "utf8");
const grid = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "coverage-grid.json"), "utf8"));
const window = {};
const document = { documentElement: { dataset: { theme: "light" } } };
vm.runInNewContext(src, { window, document });
const { build } = window.GeyserSurface;

const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
const turbo = grid.ramps.turbo.map(rgb);

// The band of each segment's wall colour: the nearest Turbo stop (walls are the ramp colour x 0.55).
function bands(g) {
  const seen = new Set();
  for (let s = 0; s < g.col.length / 36; s++) {
    const c = [0, 1, 2].map((k) => g.col[s * 36 + 18 + k] / 0.55);
    let best = 0, bestD = Infinity;
    turbo.forEach((t, k) => {
      const d = (t[0] - c[0]) ** 2 + (t[1] - c[1]) ** 2 + (t[2] - c[2]) ** 2;
      if (d < bestD) { bestD = d; best = k; }
    });
    seen.add(best);
  }
  return seen.size;
}

test("the 3D coverage terrain spans at least 5 Turbo bands (rank colouring)", () => {
  assert.ok(Array.isArray(grid.z.colour) && grid.z.colour.length === 5, "the grid carries the knots");
  const rank = bands(build(grid));
  const { colour, ...linearZ } = grid.z;
  const linear = bands(build({ ...grid, z: linearZ }));
  assert.ok(rank >= 5, `rank: ${rank} bands`);
  assert.ok(rank > linear, `rank ${rank} vs linear ${linear}`);
});
