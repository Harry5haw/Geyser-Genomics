/* Inlined in <head> by base.html (inline_js): sets <html data-theme> before the first paint, with no request.
   The same choice as theme.js's resolve(): a saved theme, else high contrast if the system asks for more contrast
   or forces colours, else the system's dark or light. Retro is only ever a saved choice. Storage can throw. No //
   comments here: the inliner joins the lines. theme.js (deferred) wires the buttons and follows system changes. */
(function () {
  var t = null, m = window.matchMedia;
  try { t = localStorage.getItem("geyser-theme"); } catch (e) { t = null; }
  if (["light", "dark", "retro", "contrast"].indexOf(t) < 0) {
    var q = function (s) { return !!(m && m(s).matches); };
    t = q("(prefers-contrast: more)") || q("(forced-colors: active)") ? "contrast" : q("(prefers-color-scheme: dark)") ? "dark" : "light";
  }
  document.documentElement.setAttribute("data-theme", t);
})();
