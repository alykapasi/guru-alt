import { useCallback, useEffect, useState } from "react";

export type Theme = "guru-light" | "guru-dark";

const STORAGE_KEY = "guru-theme";

function systemTheme(): Theme {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "guru-dark" : "guru-light";
}

function initialTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY);
  return stored === "guru-light" || stored === "guru-dark" ? stored : systemTheme();
}

/** Reads/writes the DaisyUI `data-theme` attribute on `<html>`, persisted across reloads. */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(STORAGE_KEY, theme);
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((t) => (t === "guru-light" ? "guru-dark" : "guru-light"));
  }, []);

  return { theme, toggle };
}
