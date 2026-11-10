/* Geyser Genomics UI: the only script (no framework), loaded with defer on every page. */
(() => {
  "use strict";

  // Upload size check: catch an oversized file before the upload starts (the server checks again).
  document.addEventListener("change", (e) => {
    const input = e.target;
    if (!input.dataset || !input.dataset.maxBytes || !input.files || !input.files[0]) return;
    const tooBig = input.files[0].size > Number(input.dataset.maxBytes);
    input.setCustomValidity(tooBig ? "This file is larger than the upload limit." : "");
    input.setAttribute("aria-invalid", tooBig ? "true" : "false");
  });

  // The sample list's shortcuts (All / DeepVariant / AlphaGenome / Failed): shown only when this script runs; a
  // button sets data-filter on #samples (outside the polled table, so a refresh keeps it) and CSS hides the rest.
  const filters = document.querySelectorAll("#samples .filters")[0];
  if (filters) {
    filters.hidden = false;
    filters.addEventListener("click", (e) => {
      const b = e.target.closest("[data-filter-choice]");
      if (!b) return;
      filters.closest("#samples").dataset.filter = b.dataset.filterChoice;
      for (const o of filters.querySelectorAll("[data-filter-choice]")) o.setAttribute("aria-pressed", String(o === b));
    });
  }

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
  const tipAt = (bar, v) => {
    const g = bar.querySelector(".hp-tip");
    if (g) {
      g.setAttribute("x1", String(v * 1000));
      g.setAttribute("x2", String(v * 1000 + 0.5));
    }
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
  const progressBars = (now) => {
    const dt = last ? Math.min(0.25, (now - last) / 1000) : 0.016;
    last = now;
    for (const bar of document.querySelectorAll(".hp")) update(bar, dt);
  };
  const bars = document.querySelectorAll(".hp");
  if (bars.length) {
    for (const bar of bars) update(bar, 0); // settle what the server sent before the first paint
    for (const k of Object.keys(shown)) shown[k].settled = false; // from now on, a change is live
    if (still || typeof requestAnimationFrame !== "function") setInterval(() => progressBars(Date.now()), 1000);
    else {
      const frame = (t) => { progressBars(t); requestAnimationFrame(frame); };
      requestAnimationFrame(frame);
    }
  }

  // A poll's fresh copy replaces the element, except the progress bar: while the same run is in the same state, the
  // live bar node stays in the page (its flow animation and drawn length carry on) and takes the copy's data, labels
  // and text; the rest of the element is swapped around it. Returns the element now in the page.
  const swap = (el, next) => {
    const old = el.querySelector(".hp");
    const fresh = next.querySelector(".hp");
    const going = (s) => s === "queued" || s === "running"; // one live bar from queued to the end of the run
    const same = old && fresh && old.parentNode === el && fresh.parentNode === next
      && old.dataset.key === fresh.dataset.key
      && (old.dataset.state === fresh.dataset.state || (going(old.dataset.state) && going(fresh.dataset.state)));
    if (!same) {
      el.replaceWith(next);
      return next;
    }
    for (const a of [...old.attributes]) if (a.name.startsWith("data-") && !fresh.hasAttribute(a.name)) old.removeAttribute(a.name);
    for (const a of [...fresh.attributes]) old.setAttribute(a.name, a.value);
    old.removeAttribute("data-offset"); // recomputed from the copy's server clock
    for (const sel of [".hp-labels", ".hp-now"]) {
      const o = old.querySelector(sel);
      const f = fresh.querySelector(sel);
      if (o && f) o.replaceWith(f);
    }
    for (const a of [...next.attributes]) el.setAttribute(a.name, a.value);
    for (const child of [...el.childNodes]) if (child !== old) child.remove();
    let before = true;
    for (const child of [...next.childNodes]) {
      if (child === fresh) before = false;
      else if (before) el.insertBefore(child, old);
      else el.appendChild(child);
    }
    return el;
  };

  // Status polling: an element with data-poll="<url>" is refreshed from the server's copy every data-poll-seconds
  // (swap above). The copy keeps polling only while it still has data-poll (a finished sample table doesn't). A
  // Geyser-Refresh header means the run finished: its copy shows the end (the bar's finish or failure plays), then
  // the page reloads to show the results.
  const poll = (el) => {
    const every = (Number(el.dataset.pollSeconds) || 3) * 1000;
    const tick = async () => {
      if (!document.hidden) {
        try {
          const r = await fetch(el.dataset.poll, { cache: "no-store" });
          const refresh = r.headers.get("Geyser-Refresh");
          if (refresh && r.status === 204) return location.reload();
          if (r.ok) {
            const t = document.createElement("template");
            t.innerHTML = (await r.text()).trim();
            const next = t.content.firstElementChild;
            if (next) {
              const live = swap(el, next);
              for (const bar of live.querySelectorAll(".hp")) update(bar, 0); // no frame of the raw copy
              if (refresh) return setTimeout(() => location.reload(), 2200); // after the finish plays
              if (live.dataset.poll) poll(live);
              return;
            }
          }
        } catch (_) {
          // offline or the server restarting: try again next tick
        }
      }
      setTimeout(tick, every);
    };
    setTimeout(tick, every);
  };
  // 3D figures (/figures/): each shows its inline-SVG poster first. The 2D/3D buttons choose the view for the whole
  // page (remembered in localStorage): 3D by default where WebGL works, else 2D with the 3D button disabled. In 3D,
  // the renderer (surface.js, once) and the figure's grid load when the figure scrolls into view; on any failure
  // (offline, a lost context) the figure falls back to 2D the same way.
  const VIEW = "geyser.figures.view";
  const stored = () => { try { return localStorage.getItem(VIEW); } catch (_) { return null; } };
  const remember = (v) => { try { localStorage.setItem(VIEW, v); } catch (_) { /* private mode: not remembered */ } };
  const hasWebGL = () => {
    try { return !!document.createElement("canvas").getContext("webgl"); } catch (_) { return false; }
  };
  let renderer;
  const surface = (fig) => {
    if (fig.dataset.started) return;
    fig.dataset.started = "1";
    renderer = renderer || new Promise((ok, fail) => {
      const s = document.createElement("script");
      s.src = fig.dataset.surfaceJs;
      s.onload = () => ok(window.GeyserSurface);
      s.onerror = fail;
      document.head.append(s);
    });
    Promise.all([renderer, fetch(fig.dataset.surface).then((r) => (r.ok ? r.json() : Promise.reject(r.status)))])
      .then(([lib, data]) => lib.mount(fig, data))
      .catch(() => no3d(fig));
  };
  // flat = the 2D view (CSS: .fig.flat shows the poster, hides the canvas and its overlays).
  const show = (fig, view) => {
    const flat = view !== "3d" || fig.classList.contains("no3d");
    fig.classList.toggle("flat", flat);
    fig.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String((b.dataset.view === "3d") !== flat)));
    if (!flat && fig.dataset.seen) surface(fig);
  };
  const no3d = (fig) => {
    fig.classList.add("no3d");
    fig.querySelectorAll('[data-view="3d"]').forEach((b) => { b.disabled = true; });
    show(fig, "2d");
  };
  const figures = () => {
    const figs = document.querySelectorAll("[data-surface]");
    if (!figs.length) return;
    const webgl = hasWebGL();
    const view = webgl ? (stored() === "2d" ? "2d" : "3d") : "2d";
    const io = "IntersectionObserver" in window &&
      new IntersectionObserver((entries) => entries.forEach((e) => {
        if (!e.isIntersecting) return;
        io.unobserve(e.target);
        e.target.dataset.seen = "1";
        if (!e.target.classList.contains("flat")) surface(e.target);
      }), { rootMargin: "120px" });
    figs.forEach((fig) => {
      if (!io) fig.dataset.seen = "1"; // no IntersectionObserver: every figure counts as on screen
      if (!webgl) no3d(fig);
      else show(fig, view);
      if (io) io.observe(fig);
      fig.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => {
        remember(b.dataset.view);
        fig.dataset.seen = "1"; // the clicked figure is on screen
        figs.forEach((f) => show(f, b.dataset.view));
        // A 3D view hidden while flat redraws on the next frame (surface.js re-uploads on this event).
        if (b.dataset.view === "3d") document.dispatchEvent(new CustomEvent("geyser:theme", { detail: { theme: document.documentElement.dataset.theme } }));
      }));
    });
  };

  // The static site: an element with data-status-api="<web API>/status/<id>" polls the run's status (JSON) and
  // reloads the page once the run has ended, as its last step republishes the pages. Every 15 s at most (the
  // API is small and shared); at most 3 reloads per page per session, in case a cache serves the old copy.
  const ENDED = new Set(["SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"]);
  const watch = (el) => {
    const every = Math.max(15, Number(el.dataset.pollSeconds) || 0) * 1000;
    const tick = async () => {
      if (!document.hidden) {
        try {
          const r = await fetch(el.dataset.statusApi, { cache: "no-store" });
          if (r.status === 404) return; // no run of this sample: nothing to wait for
          const run = r.ok ? await r.json() : null;
          if (run && ENDED.has(run.status)) {
            // Only for a run that ended just now: one that stopped long ago was published already (a page that
            // still shows it running had no publish to wait for, so reloading would loop).
            const stopped = Date.parse(run.stopped || "");
            const recent = Number.isNaN(stopped) || Date.now() - stopped < 15 * 60 * 1000;
            const key = `geyser-reloads:${location.pathname}`, done = Number(sessionStorage.getItem(key) || 0);
            if (recent && done < 3) {
              sessionStorage.setItem(key, String(done + 1));
              setTimeout(() => location.reload(), 5000); // a moment for the CDN to drop its copy
            }
            return;
          }
        } catch (_) {
          // the API unreachable for a moment: try again next tick
        }
      }
      setTimeout(tick, every);
    };
    setTimeout(tick, every);
  };

  const start = () => {
    document.querySelectorAll("[data-poll]").forEach(poll);
    document.querySelectorAll("[data-status-api]").forEach(watch);
    figures();
  };
  // A page prerendered by the speculation rules doesn't poll or load 3D until it is shown.
  if (document.prerendering) document.addEventListener("prerenderingchange", start, { once: true });
  else start();

  // Browsers without Speculation Rules (Firefox, Safari): prefetch a same-site page on hover or touch,
  // so the click finds it in the cache. Downloads and the current page are skipped.
  if (!(HTMLScriptElement.supports && HTMLScriptElement.supports("speculationrules"))) {
    const seen = new Set([location.href]);
    const prefetch = (e) => {
      const a = e.target.closest && e.target.closest("a[href]");
      if (!a || a.origin !== location.origin || a.hasAttribute("download") || a.pathname.includes("/files/")) return;
      if (seen.has(a.href)) return;
      seen.add(a.href);
      const link = document.createElement("link");
      link.rel = "prefetch";
      link.href = a.href;
      document.head.append(link);
    };
    document.addEventListener("mouseover", prefetch, { passive: true });
    document.addEventListener("touchstart", prefetch, { passive: true });
  }
})();
