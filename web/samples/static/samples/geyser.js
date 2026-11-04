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
  // 3D figures (/figures/): each shows its inline-SVG poster first. The renderer (surface.js, once) and the
  // figure's grid load only when it scrolls into view or "Rotate in 3D" is pressed; on any failure (no WebGL,
  // offline) the poster simply stays.
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
      .catch(() => fig.classList.add("no3d"));
  };
  const figures = () => {
    const io = "IntersectionObserver" in window &&
      new IntersectionObserver((entries) => entries.forEach((e) => {
        if (e.isIntersecting) { io.unobserve(e.target); surface(e.target); }
      }), { rootMargin: "120px" });
    document.querySelectorAll("[data-surface]").forEach((fig) => {
      if (io) io.observe(fig);
      const button = fig.querySelector("[data-surface-start]");
      if (button) button.addEventListener("click", () => surface(fig));
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
            const key = `geyser-reloads:${location.pathname}`, done = Number(sessionStorage.getItem(key) || 0);
            if (done < 3) {
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
