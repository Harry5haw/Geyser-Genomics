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
  const start = () => document.querySelectorAll("[data-poll]").forEach(poll);
  // A page prerendered by the speculation rules doesn't poll until it is shown.
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
