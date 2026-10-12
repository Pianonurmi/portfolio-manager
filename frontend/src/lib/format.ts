// fi-FI formatting for every number and date in the UI (NFR-13).

const nf = new Map<string, Intl.NumberFormat>();
function fmt(opts: Intl.NumberFormatOptions): Intl.NumberFormat {
  const k = JSON.stringify(opts);
  let f = nf.get(k);
  if (!f) {
    f = new Intl.NumberFormat("fi-FI", opts);
    nf.set(k, f);
  }
  return f;
}

export const num = (v: number | string | null | undefined): number => (v == null || v === "" ? NaN : typeof v === "number" ? v : Number(v));

const minus = (s: string) => s.replace(/-/g, "−");

export function money(v: number | string, currency = "EUR", digits = 0): string {
  const n = num(v);
  if (!Number.isFinite(n)) return "—";
  return minus(fmt({ style: "currency", currency: currency.length === 3 ? currency : "EUR", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n));
}

export function decimal(v: number | string, digits = 2, maxDigits = digits): string {
  const n = num(v);
  if (!Number.isFinite(n)) return "—";
  return minus(fmt({ minimumFractionDigits: digits, maximumFractionDigits: maxDigits }).format(n));
}

export function quantity(v: number | string): string {
  const n = num(v);
  if (!Number.isFinite(n)) return "—";
  const whole = Number.isInteger(n);
  return minus(fmt({ minimumFractionDigits: whole ? 0 : Math.abs(n) < 1 ? 3 : 2, maximumFractionDigits: whole ? 0 : 8 }).format(n));
}

export function pct(v: number | string, digits = 1): string {
  const n = num(v);
  if (!Number.isFinite(n)) return "—";
  return minus(fmt({ style: "percent", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n));
}

export const sign = (n: number) => (n > 0 ? "+" : n < 0 ? "−" : "");
export const arrow = (n: number) => (n > 0 ? "▲" : n < 0 ? "▼" : "");

export function signedMoney(v: number | string, currency = "EUR", digits = 0): string {
  const n = num(v);
  if (!Number.isFinite(n)) return "—";
  return sign(n) + money(Math.abs(n), currency, digits);
}

export function signedPct(v: number | string, digits = 1): string {
  const n = num(v);
  if (!Number.isFinite(n)) return "—";
  return sign(n) + pct(Math.abs(n), digits);
}

export function compact(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e6) return decimal(v / 1e6, 1) + " M€";
  if (a >= 1e3) return decimal(v / 1e3, 0) + " k€";
  return decimal(v, 0) + " €";
}

export function date(v: string | Date | null | undefined): string {
  if (!v) return "—";
  const d = typeof v === "string" ? new Date(v.length === 10 ? v + "T00:00:00" : v) : v;
  return d.toLocaleDateString("fi-FI", { day: "numeric", month: "numeric", year: "numeric" });
}

export function dateTime(v: string | null | undefined): string {
  if (!v) return "—";
  const d = new Date(v);
  return `${d.toLocaleDateString("fi-FI", { day: "numeric", month: "numeric" })} ${d.toLocaleTimeString("fi-FI", { hour: "2-digit", minute: "2-digit" })}`;
}

export function ago(v: string | null | undefined): string {
  if (!v) return "never";
  const s = (Date.now() - new Date(v).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400 * 1.5) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
