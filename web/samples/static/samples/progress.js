/* Geyser Genomics: the run's progress helix (samples/_progress.html). Loaded with defer after geyser.js, only on
   the pages that show it (a sample, the debug preview), so every other page stays lighter. */
(() => {
  "use strict";

  // The progress helix (samples/_progress.html, samples/progress.py). The drawn length is kept here per sample (data-key)
  // across polls, so it never jumps: it eases towards the server's truth (about a second to catch up) and never moves
  // back. Between polls the running stage keeps growing from its start and expected time (share(): linear, then easing
  // towards 97% of its span: progress.py's share()). A live finish spins and turns green; a live failure shudders,
  // snaps at the break and turns red from it: each once, on the transition only. Reduced motion: no flow, spin or
  // shudder; the length steps once a second and the final colour fades in.
  const say = (s) => (s < 90 ? `about ${Math.max(5, Math.round(s / 5) * 5)} s left`
    : s < 3600 ? `about ${Math.round(s / 60)} min left` : `about ${(s / 3600).toFixed(1)} h left`);
  const share = (x) => (x <= 0.8 ? Math.max(0.04, x) : 0.8 + 0.17 * (1 - Math.exp(-(x - 0.8) / 0.17)));
  const still = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const shown = {}; // sample -> {v: drawn length 0..1, state}
  let last = 0;
  const tipAt = (bar, v) => { // the drawn length: a clip (cheaper to paint than a mask)
    const r = bar.querySelector(".hp-clip");
    if (r) r.setAttribute("width", String(v * 1000));
  };
  const target = (bar) => {
    const d = bar.dataset;
    if (!d.stageStart) return Number(d.value) || 0;
    if (!d.offset) d.offset = String(Date.now() / 1000 - Number(d.now));
    const elapsed = Math.max(0, Date.now() / 1000 - Number(d.offset) - Number(d.stageStart));
    const expected = Number(d.expected) || 60;
    const text = `${d.lead}, ${say(Math.max(0, expected - elapsed) + Number(d.later || 0))}`;
    const now = bar.querySelector(".hp-now");
    if (now && now.textContent !== text) {
      now.textContent = text;
      bar.setAttribute("aria-valuetext", text);
    }
    return Number(d.from) + share(elapsed / expected) * (Number(d.to) - Number(d.from));
  };
  const place = (bar) => {
    const ol = bar.querySelector(".hp-labels");
    if (!ol || ol.classList.contains("placed")) return;
    for (const li of ol.querySelectorAll("li")) li.style.left = `${Number(li.dataset.mid) * 100}%`; // CSSOM: CSP-safe
    ol.classList.add("placed");
  };
  const spread = (bar, from) => { // red grows outward from the break, over 0.7 s
    const g = bar.querySelector(".hp-red");
    if (!g) return;
    const t0 = Date.now();
    const step = () => {
      const k = Math.min(1, (Date.now() - t0) / 700);
      const x = from * (1 - (1 - (1 - k) ** 3)); // ease-out from the break to 0
      g.setAttribute("x1", String(x - 30));
      g.setAttribute("x2", String(x));
      if (k < 1) requestAnimationFrame(step);
    };
    step();
  };
  const update = (bar, dt) => {
    const d = bar.dataset;
    const key = d.key || "_";
    const mem = shown[key] || (shown[key] = { v: Number(d.value) || 0, state: d.state, settled: true });
    place(bar);
    if (d.state === "running" || d.state === "queued") {
      if (mem.state !== "running" && mem.state !== "queued") mem.v = Number(d.value) || 0; // a new run
      const goal = target(bar);
      // catch up over ~1 s, but never faster than a quarter of the bar a second (no leap after an early stage end)
      if (goal > mem.v) mem.v += still ? goal - mem.v : Math.min((goal - mem.v) * Math.min(1, dt / 0.35), 0.25 * dt);
      tipAt(bar, mem.v);
      bar.setAttribute("aria-valuenow", String(Math.round(mem.v * 100)));
      mem.state = d.state;
      return;
    }
    // succeeded or failed
    if (bar.dataset.played) return;
    bar.dataset.played = "1";
    const live = mem.state === "running" && !mem.settled;
    const end = d.state === "failed" ? Number(d.fail) || 0 : 1;
    mem.state = d.state;
    if (!live) return; // opened on a finished run: the server's settled state
    if (still) {
      bar.classList.add("fading");
      return;
    }
    // the tip races to its end first (0.3 s), then the spin or the snap
    const from = mem.v;
    const t0 = Date.now();
    bar.classList.add("racing");
    tipAt(bar, from); // this frame still shows where the bar was, not the server's settled end
    if (d.state === "failed") {
      const g = bar.querySelector(".hp-red");
      if (g) g.setAttribute("x1", String(end * 1000)), g.setAttribute("x2", String(end * 1000 + 0.5));
    }
    const race = () => {
      const k = Math.min(1, (Date.now() - t0) / 300);
      tipAt(bar, from + (end - from) * (1 - (1 - k) ** 2));
      if (k < 1) return requestAnimationFrame(race);
      bar.classList.remove("racing");
      if (d.state === "succeeded") bar.classList.add("spinning");
      else {
        bar.classList.add("snapping");
        spread(bar, end * 1000);
      }
    };
    requestAnimationFrame(race);
  };
  // The loop runs only while a bar is queued or running (a finished run's bar is static; its finish or failure
  // animates on its own): no per-frame work on a finished page. A poll that brings a live bar restarts it.
  const live = () => [...document.querySelectorAll(".hp")].some((b) => b.dataset.state === "running" || b.dataset.state === "queued");
  let looping = false;
  const progressBars = (now) => {
    const dt = last ? Math.min(0.25, (now - last) / 1000) : 0.016;
    last = now;
    for (const bar of document.querySelectorAll(".hp")) update(bar, dt);
  };
  const run = () => {
    if (looping || !live()) return;
    looping = true;
    last = 0;
    if (still || typeof requestAnimationFrame !== "function") {
      const timer = setInterval(() => {
        progressBars(Date.now());
        if (!live()) { clearInterval(timer); looping = false; }
      }, 1000);
      return;
    }
    const frame = (t) => {
      progressBars(t);
      if (live()) requestAnimationFrame(frame);
      else looping = false;
    };
    requestAnimationFrame(frame);
  };
  window.GeyserProgress = (root) => { // geyser.js, after a poll's swap
    for (const bar of root.querySelectorAll(".hp")) update(bar, 0);
    run();
  };
  const bars = document.querySelectorAll(".hp");
  if (bars.length) {
    for (const bar of bars) update(bar, 0); // settle what the server sent before the first paint
    for (const k of Object.keys(shown)) shown[k].settled = false; // from now on, a change is live
    run();
  }
})();
