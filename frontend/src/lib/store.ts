// Per-device conveniences only (theme). Never financial data.
export const local = {
  get(key: string): string | null {
    try {
      return localStorage.getItem("pm." + key);
    } catch {
      return null;
    }
  },
  set(key: string, value: string): void {
    try {
      localStorage.setItem("pm." + key, value);
    } catch {
      /* private mode */
    }
  },
};

export function toggleTheme(): void {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : !matchMedia("(prefers-color-scheme: light)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  local.set("theme", root.dataset.theme);
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "#f4f5f7" : "#0c0e12");
  window.dispatchEvent(new Event("pm:theme"));
}
