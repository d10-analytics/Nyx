(() => {
  "use strict";

  const storageKey = "nyx-theme";
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
  const normalize = (value) => ["system", "light", "dark"].includes(value) ? value : "dark";
  let preference = "dark";
  let selector = null;
  try {
    preference = normalize(window.localStorage.getItem(storageKey));
  } catch (_) {
    // Theme selection remains usable when browser storage is unavailable.
  }

  function apply() {
    document.documentElement.dataset.theme = preference === "system"
      ? systemTheme.matches ? "dark" : "light"
      : preference;
    if (selector) selector.value = preference;
  }

  apply();
  systemTheme.addEventListener("change", () => {
    if (preference === "system") apply();
  });
  window.addEventListener("storage", (event) => {
    if (event.key !== storageKey && event.key !== null) return;
    preference = normalize(event.newValue);
    apply();
  });
  document.addEventListener("DOMContentLoaded", () => {
    selector = document.querySelector("#theme");
    selector.value = preference;
    selector.addEventListener("change", () => {
      preference = normalize(selector.value);
      apply();
      try {
        window.localStorage.setItem(storageKey, preference);
      } catch (_) {
        // Keep the chosen theme for this page even if it cannot be saved.
      }
    });
  });
})();
