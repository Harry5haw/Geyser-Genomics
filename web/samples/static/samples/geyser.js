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

  // Status polling: an element with data-poll="<url>" is replaced by the server's fresh copy every
  // data-poll-seconds. The copy keeps polling only while it still has data-poll (a finished sample table
  // doesn't). A response with a Geyser-Refresh header means the run finished: reload to show the results.
  const poll = (el) => {
    const every = (Number(el.dataset.pollSeconds) || 3) * 1000;
    const tick = async () => {
      if (!document.hidden) {
        try {
          const r = await fetch(el.dataset.poll, { cache: "no-store" });
          if (r.headers.get("Geyser-Refresh")) return location.reload();
          if (r.ok) {
            const t = document.createElement("template");
            t.innerHTML = (await r.text()).trim();
            const next = t.content.firstElementChild;
            if (next) {
              el.replaceWith(next);
              if (next.dataset.poll) poll(next);
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

  // The DeepVariant card's motif (_motif_dv.html): a finished run plays it once on load (CSS); hovering the card, or
  // focusing inside it, plays it again: re-adding .done restarts its animations. Decorative; nothing under
  // prefers-reduced-motion (the CSS stops every animation there).
  for (const card of document.querySelectorAll(".dv-card")) {
    const art = card.querySelector(".mdv.done");
    if (!art) continue;
    let last = performance.now();
    const replay = () => {
      if (performance.now() - last < 3800) return; // not while it is still playing
      last = performance.now();
      art.classList.remove("done");
      void art.getBoundingClientRect();
      art.classList.add("done");
    };
    card.addEventListener("mouseenter", replay);
    card.addEventListener("focusin", replay);
  }

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
