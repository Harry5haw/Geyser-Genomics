/* Geyser theme: light, dark, retro or high contrast (low vision); retro is only ever chosen by hand.
   The inline boot in <head> (templates/samples/_theme_boot.js) sets <html data-theme> before the first paint;
   this deferred file wires the header buttons (aria-pressed), saves a choice in localStorage (wrapped: storage can
   be blocked), follows the system while nothing is saved, and dispatches "geyser:theme" (detail.theme) for what
   CSS can't recolour, e.g. the 3D figures. resolve() here and the boot script must agree (theme.test.js). */
(function (global) {
  "use strict";
  var KEY = "geyser-theme";
  var THEMES = ["light", "dark", "retro", "contrast"]; // retro is never chosen automatically

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

  apply(root.getAttribute("data-theme") || resolve(stored(), prefs())); // the boot's choice, buttons now marked
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
