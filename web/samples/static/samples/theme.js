/* Geyser theme: light, dark or high contrast. Loaded synchronously in <head> (a static file, so the hosted
   site's CSP needs no new hash), it sets <html data-theme> before first paint, so a page never flashes the
   wrong theme. The default follows the system (prefers-contrast / forced-colors, then prefers-color-scheme);
   a choice made with the header buttons persists in localStorage. Storage can be blocked (private windows,
   strict settings): every access is wrapped, and the page then simply follows the system.
   Other scripts listen for "geyser:theme" (detail.theme) to recolour what CSS can't, e.g. the 3D figures. */
(function (global) {
  "use strict";
  var KEY = "geyser-theme";
  var THEMES = ["light", "dark", "contrast"];

  // The pure core (tested in web/tests/js/theme.test.js): the theme to show.
  function resolve(stored, prefs) {
    if (THEMES.indexOf(stored) >= 0) return stored;
    if (prefs.contrast) return "contrast";
    return prefs.dark ? "dark" : "light";
  }
  global.GeyserTheme = { KEY: KEY, THEMES: THEMES, resolve: resolve };

  var doc = global.document;
  if (!doc || !doc.documentElement) return; // not a browser (the tests)
  var root = doc.documentElement;
  var media = function (q) {
    return !!(global.matchMedia && global.matchMedia(q).matches);
  };
  var prefs = function () {
    return {
      contrast: media("(prefers-contrast: more)") || media("(forced-colors: active)"),
      dark: media("(prefers-color-scheme: dark)"),
    };
  };
  var stored = function () {
    try {
      return global.localStorage.getItem(KEY);
    } catch (e) {
      return null;
    }
  };
  var apply = function (theme) {
    root.setAttribute("data-theme", theme);
    var buttons = doc.querySelectorAll("[data-theme-choice]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute("aria-pressed", String(buttons[i].getAttribute("data-theme-choice") === theme));
    }
    try {
      doc.dispatchEvent(new CustomEvent("geyser:theme", { detail: { theme: theme } }));
    } catch (e) {
      /* very old browsers: no CustomEvent constructor; CSS still themes the page */
    }
  };

  apply(resolve(stored(), prefs()));
  doc.addEventListener("DOMContentLoaded", function () {
    apply(root.getAttribute("data-theme")); // the buttons exist now: mark the current one pressed
  });
  doc.addEventListener("click", function (event) {
    var button = event.target && event.target.closest && event.target.closest("[data-theme-choice]");
    if (!button) return;
    var theme = button.getAttribute("data-theme-choice");
    if (THEMES.indexOf(theme) < 0) return;
    try {
      global.localStorage.setItem(KEY, theme);
    } catch (e) {
      /* not saved: this page still switches */
    }
    apply(theme);
  });
  // With no saved choice, follow the system when it changes (e.g. the OS switches to dark at night).
  ["(prefers-color-scheme: dark)", "(prefers-contrast: more)"].forEach(function (q) {
    var list = global.matchMedia && global.matchMedia(q);
    if (list && list.addEventListener) {
      list.addEventListener("change", function () {
        if (THEMES.indexOf(stored()) < 0) apply(resolve(null, prefs()));
      });
    }
  });
})(this);
