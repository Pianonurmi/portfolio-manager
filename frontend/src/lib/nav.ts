export type Screen = { id: string; name: string; path: string; key: string; icon: string; phone?: boolean };

export const SCREENS: Screen[] = [
  { id: "home", name: "Home", path: "/", key: "h", icon: "home", phone: true },
  { id: "portfolio", name: "Portfolio", path: "/portfolio", key: "p", icon: "portfolio", phone: true },
  { id: "networth", name: "Net worth", path: "/net-worth", key: "n", icon: "networth", phone: true },
  { id: "cashflow", name: "Cash flow", path: "/cash-flow", key: "c", icon: "cashflow" },
  { id: "tax", name: "Tax", path: "/tax", key: "x", icon: "tax" },
  { id: "imports", name: "Imports", path: "/imports", key: "i", icon: "imports" },
  { id: "plan", name: "Planning", path: "/planning", key: "l", icon: "plan", phone: true },
  { id: "etfs", name: "ETF screener", path: "/etfs", key: "e", icon: "search" },
  { id: "kids", name: "Kids", path: "/kids", key: "k", icon: "kids" },
  { id: "household", name: "Household", path: "/household", key: "o", icon: "household" },
  { id: "life", name: "Life & tools", path: "/life", key: "f", icon: "kids" },
  { id: "settings", name: "Settings & jobs", path: "/settings", key: "s", icon: "settings" },
];

export function screenFor(pathname: string): Screen {
  return [...SCREENS].sort((a, b) => b.path.length - a.path.length).find((s) => (s.path === "/" ? pathname === "/" : pathname.startsWith(s.path))) ?? SCREENS[0]!;
}
