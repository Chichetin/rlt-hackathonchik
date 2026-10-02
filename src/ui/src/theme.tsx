import { useState } from "react";

export type Theme = "dark" | "light";
const KEY = "mitrandir-theme";

export function readTheme(): Theme {
  try {
    return window.localStorage.getItem(KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function applyTheme(theme: Theme, persist = false) {
  document.documentElement.setAttribute("data-theme", theme);
  if (!persist) return;
  try {
    window.localStorage.setItem(KEY, theme);
  } catch {
    /* storage is unavailable: the choice lasts until reload */
  }
}

export function initTheme() {
  applyTheme(readTheme());
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => (document.documentElement.getAttribute("data-theme") === "light" ? "light" : readTheme()));
  const next: Theme = theme === "light" ? "dark" : "light";
  return (
    <button
      type="button"
      className="theme-toggle"
      aria-label={next === "light" ? "Включить светлую тему" : "Включить тёмную тему"}
      aria-pressed={theme === "light"}
      onClick={() => { applyTheme(next, true); setTheme(next); }}
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true">
        {theme === "light" ? <path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5Z" /> : <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5" /></>}
      </svg>
      <span>{theme === "light" ? "Тёмная" : "Светлая"}</span>
    </button>
  );
}
