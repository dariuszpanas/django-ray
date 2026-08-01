"use strict";

{
  const supportedThemes = new Set(["auto", "dark", "light"]);

  function storedValue(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function unfoldTheme() {
    const storedTheme = storedValue("adminTheme");
    if (storedTheme === null) return null;

    try {
      const parsedTheme = JSON.parse(storedTheme);
      return supportedThemes.has(parsedTheme) ? parsedTheme : null;
    } catch {
      return null;
    }
  }

  const djangoTheme = storedValue("theme");
  const selectedTheme = supportedThemes.has(djangoTheme)
    ? djangoTheme
    : (unfoldTheme() ?? "auto");
  document.documentElement.dataset.theme = selectedTheme;
}
