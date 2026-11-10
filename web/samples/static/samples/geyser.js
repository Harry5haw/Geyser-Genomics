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
    filters.dataset.ready = "1"; // its space was kept (no layout shift); now it can be seen
    filters.addEventListener("click", (e) => {
      const b = e.target.closest("[data-filter-choice]");
      if (!b) return;
      filters.closest("#samples").dataset.filter = b.dataset.filterChoice;
      for (const o of filters.querySelectorAll("[data-filter-choice]")) o.setAttribute("aria-pressed", String(o === b));
    });
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
              if (window.GeyserProgress) window.GeyserProgress(live); // the bar: no frame of the raw copy
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
  // The header logo's eruption (scripts/build_logo_burst.py). The page shows the static lockup (no JS, reduced
  // motion, or already erupted this session). Otherwise, once the page has loaded (so the static lockup stays the
  // first paint and the LCP image): the empty geyser, then on the first hover or focus (on a screen without hover,
  // the first touch or scroll anywhere) the animated lockup, which plays once in the <img> (SMIL) and holds the full
  // logo. Never on a timer: an animation nobody started would be paint work inside every page-load measurement.
  // data-erupt = "<empty URL> <animated URL>": hashed static names, which a script can't work out.
  const logos = document.querySelectorAll(".brand img[data-erupt]");
  const ERUPTED = "geyser-erupted";
  const media = (q) => Boolean(window.matchMedia && window.matchMedia(q).matches);
  let erupted;
  try { erupted = sessionStorage.getItem(ERUPTED); } catch (_) { erupted = "1"; /* no storage: keep it still */ }
  if (logos.length && !erupted && !media("(prefers-reduced-motion: reduce)")) {
    const brand = logos[0].parentNode;
    const use = (k) => logos.forEach((img) => { img.src = img.dataset.erupt.split(" ")[k]; });
    let done = false;
    const erupt = () => {
      if (done) return;
      done = true;
      brand.removeEventListener("pointerover", erupt);
      brand.removeEventListener("focusin", erupt);
      ["touchstart", "scroll"].forEach((t) => window.removeEventListener && window.removeEventListener(t, erupt));
      try { sessionStorage.setItem(ERUPTED, "1"); } catch (_) { /* private mode: it may erupt again */ }
      use(1);
    };
    const rest = () => {
      if (done) return;
      use(0);
      if (media("(hover: none)") && window.addEventListener) {
        ["touchstart", "scroll"].forEach((t) => window.addEventListener(t, erupt, { passive: true }));
      }
    };
    // Rest only once the page has loaded AND the browser has recorded its largest paint (on a fast machine that
    // comes after load): swapping earlier would make the empty geyser, fetched by this script, the LCP image.
    const lcp = typeof PerformanceObserver !== "undefined" &&
      (PerformanceObserver.supportedEntryTypes || []).includes("largest-contentful-paint");
    let waits = lcp ? 2 : 1;
    const ready = () => {
      if (--waits) return;
      if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => requestAnimationFrame(rest));
      else rest();
    };
    if (lcp) {
      const seen = new PerformanceObserver((list) => {
        if (list.getEntries().length) { seen.disconnect(); ready(); }
      });
      seen.observe({ type: "largest-contentful-paint", buffered: true });
    }
    if (document.readyState === "complete") ready();
    else if (window.addEventListener) window.addEventListener("load", ready, { once: true });
    brand.addEventListener("pointerover", erupt);
    brand.addEventListener("focusin", erupt);
  }
})();
