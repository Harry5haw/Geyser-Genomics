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
/* The logo eruption (_site.css, _logo_burst.html): the first time this session, with motion allowed, the header
   logo starts as an empty geyser (logo-armed) and erupts on the first hover or focus (logo-erupt), or by itself
   on a screen without hover. Once erupted it stays full for the session. Without this script, or under reduced
   motion, or if storage throws, the logo is simply full. */
(function () {
  var r = document.documentElement, k = "geyser-erupted", m = window.matchMedia, s;
  try { s = sessionStorage.getItem(k); } catch (e) { s = "1"; }
  if (s || (m && m("(prefers-reduced-motion: reduce)").matches)) return;
  r.classList.add("logo-armed");
  var go = function (e) {
    if (e && !(e.target.closest && e.target.closest(".brand"))) return;
    r.classList.replace("logo-armed", "logo-erupt");
    try { sessionStorage.setItem(k, "1"); } catch (x) { s = "1"; }
    document.removeEventListener("pointerover", go);
    document.removeEventListener("focusin", go);
  };
  document.addEventListener("pointerover", go);
  document.addEventListener("focusin", go);
  if (m && m("(hover: none)").matches) setTimeout(go, 700);
})();
