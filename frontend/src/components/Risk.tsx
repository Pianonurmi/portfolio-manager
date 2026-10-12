// Risk (Phase C): today's holdings weights over past monthly returns: risk tiles, drawdown, rolling return,
// correlation heatmap and each holding's share of portfolio risk.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import type { Ctx } from "../lib/context";
import { date, decimal, money, pct, signedPct } from "../lib/format";
import { LineChart, useCssVar } from "./LineChart";
import { Chip, Empty, Panel, Skeleton } from "./ui";

type Matrix = { labels: string[]; matrix: (number | null)[][]; extended?: boolean[] };
type Dated = { date: string; value: number };
type RiskData = {
  window: { start: string; end: string; months: number; requested_months: number; coverage: number } | null;
  excluded: { instrument_id: number; name: string; weight: number; first_price: string | null; reason: string }[];
  proxies: { instrument_id: number; name: string; proxy_id: number; proxy_name: string; correlation: number; overlap_months: number; from: string; to: string }[];
  holdings: { instrument_id: number; name: string; type: string; value: string; weight: number; volatility: number | null; risk_share: number | null }[];
  metrics: {
    cagr: number | null;
    volatility: number | null;
    sharpe: number | null;
    sortino: number | null;
    max_drawdown: { value: number; peak: string | null; trough: string | null; recovery: string | null; recovered: boolean };
    longest_drawdown_months: number;
    best_month: Dated;
    worst_month: Dated;
    best_year: { year: number; value: number } | null;
    worst_year: { year: number; value: number } | null;
    var_95: number;
    cvar_95: number;
    positive_months: number;
    benchmark?: { beta: number | null; correlation: number | null; alpha: number | null; tracking_error: number | null; up_capture: number | null; down_capture: number | null };
  } | null;
  benchmark: { name: string; available: boolean } | null;
  series: { dates: string[]; drawdown: number[]; rolling_dates: string[]; rolling_12m: number[] } | null;
  correlations: { holdings: Matrix; classes: Matrix } | null;
  notes: string[];
};

const PERIODS = ["1Y", "3Y", "5Y", "10Y", "Max"];
const monthYear = (d: string) => new Date(d).toLocaleDateString("fi-FI", { month: "short", year: "numeric" });
const proxyText = (p: RiskData["proxies"][number]) =>
  `${p.name}: ${monthYear(p.from)} – ${monthYear(p.to)} uses ${p.proxy_name} (ρ ${decimal(p.correlation, 2)}, ${p.overlap_months} months overlap)`;
const ratio = (v: number | null | undefined, digits = 2) => (v == null ? "—" : decimal(v, digits));

export function Risk({ ctx }: { ctx: Ctx }) {
  const [period, setPeriod] = useState("5Y");
  const [proxies, setProxies] = useState(true);
  const [view, setView] = useState<"holdings" | "classes">("holdings");
  const c2 = useCssVar("--c2");
  const c1 = useCssVar("--c1");
  const q = useQuery({
    queryKey: ["risk", ctx.scope, period, proxies],
    queryFn: () => api<RiskData>(`/api/portfolio/risk?scope=${encodeURIComponent(ctx.scope)}&period=${period}&proxies=${proxies}`),
  });
  const d = q.data;
  const m = d?.metrics;
  const w = d?.window;
  const dd = m?.max_drawdown;
  return (
    <Panel
      title="Risk"
      style={{ marginTop: 12 }}
      meta={
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "var(--fg-2)" }}>
            <input type="checkbox" checked={proxies} onChange={(e) => setProxies(e.target.checked)} />
            Extend short histories with similar holdings
          </label>
          <div className="seg" role="group" aria-label="Risk period">
            {PERIODS.map((p) => (
              <button key={p} aria-pressed={period === p} onClick={() => setPeriod(p)}>{p}</button>
            ))}
          </div>
        </div>
      }
      bodyless
    >
      {q.isLoading && <p style={{ padding: 14 }}><Skeleton w={240} /></p>}
      {q.isError && <p className="err" style={{ padding: 14 }}>Could not load risk figures.</p>}
      {d && !m && <Empty title="Not enough price history">{d.notes.join(" ")}</Empty>}
      {d && m && w && d.series && d.correlations && (
        <>
          <p style={{ padding: "10px 14px 0", margin: 0, fontSize: 12, color: "var(--fg-2)" }}>
            {monthYear(w.start)} – {monthYear(w.end)} · {pct(w.coverage, 0)} of value
            {w.months < w.requested_months && ` · shorter than the requested ${period}`}
            {d.proxies.length > 0 && (
              <>
                {"; "}
                <span title={d.proxies.map(proxyText).join("\n")} style={{ textDecoration: "underline dotted", cursor: "help" }}>
                  {d.proxies.length} holding{d.proxies.length > 1 ? "s" : ""} extended with a proxy
                </span>
              </>
            )}
            {d.excluded.length > 0 && (
              <>
                {"; "}
                <span title={d.excluded.map((e) => `${e.name}: ${e.reason}${e.first_price ? `, first price ${date(e.first_price)}` : ""}`).join("\n")} style={{ textDecoration: "underline dotted", cursor: "help" }}>
                  {d.excluded.length} holding{d.excluded.length > 1 ? "s" : ""} excluded
                </span>
              </>
            )}
          </p>
          <div className="tiles" style={{ marginTop: 10 }}>
            <Tile label="CAGR" v={m.cagr == null ? "—" : signedPct(m.cagr)} sub="monthly rebalanced, price return" />
            <Tile label="Volatility" v={m.volatility == null ? "—" : pct(m.volatility)} sub="annualised" />
            <Tile label="Sharpe" v={ratio(m.sharpe)} sub={`Sortino ${ratio(m.sortino)}`} />
            <Tile
              label="Max drawdown"
              v={pct(dd!.value)}
              sub={dd!.peak && dd!.trough ? `${monthYear(dd!.peak)} → ${monthYear(dd!.trough)} · ${dd!.recovered ? `recovered ${monthYear(dd!.recovery!)}` : "not recovered"}` : "no drawdown"}
            />
            <Tile label="Monthly VaR 95 %" v={pct(m.var_95)} sub={`CVaR ${pct(m.cvar_95)} · ${pct(m.positive_months, 0)} up months`} />
            {d.benchmark && m.benchmark ? (
              <Tile label={`Beta vs ${d.benchmark.name}`} v={ratio(m.benchmark.beta)} sub={`correlation ${ratio(m.benchmark.correlation)} · alpha ${m.benchmark.alpha == null ? "—" : signedPct(m.benchmark.alpha)}`} />
            ) : (
              <Tile label="Beta" v="—" sub={d.benchmark ? "benchmark has no history here" : "set a benchmark in Returns"} />
            )}
          </div>
          <div className="grid g-2" style={{ padding: 14 }}>
            <div>
              <div className="label" style={{ marginBottom: 6 }}>Drawdown · longest {m.longest_drawdown_months} months</div>
              <LineChart label="Drawdown from the previous peak" x={d.series.dates} series={[{ name: "Drawdown", values: d.series.drawdown, color: c2, area: true }]} format={(v) => pct(v, 0)} yMax={0} height={170} />
            </div>
            <div>
              <div className="label" style={{ marginBottom: 6 }}>Rolling 12-month return</div>
              {d.series.rolling_dates.length ? (
                <LineChart label="Rolling 12-month return" x={d.series.rolling_dates} series={[{ name: "12 months", values: d.series.rolling_12m, color: c1 }]} format={(v) => pct(v, 0)} height={170} />
              ) : (
                <p style={{ color: "var(--muted)", fontSize: 12 }}>Needs at least 12 months of history.</p>
              )}
            </div>
          </div>
          <div style={{ padding: "0 14px 14px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <span className="label">Correlation of monthly returns</span>
              <div className="seg" role="group" aria-label="Correlation view">
                <button aria-pressed={view === "holdings"} onClick={() => setView("holdings")}>Holdings</button>
                <button aria-pressed={view === "classes"} onClick={() => setView("classes")}>Asset classes</button>
              </div>
            </div>
            <Heatmap m={d.correlations[view]} />
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Holding</th>
                  <th className="r">Weight</th>
                  <th className="r hide-sm">Volatility</th>
                  <th style={{ width: "34%" }}>Share of portfolio risk</th>
                </tr>
              </thead>
              <tbody>
                {[...d.holdings].sort((a, b) => (b.risk_share ?? 0) - (a.risk_share ?? 0)).map((h) => (
                  <tr key={h.instrument_id} style={{ cursor: "default" }}>
                    <td title={money(h.value, "EUR", 0)}>{h.name}</td>
                    <td className="r num">{pct(h.weight)}</td>
                    <td className="r num hide-sm">{h.type === "cash" ? "—" : h.volatility == null ? "—" : pct(h.volatility)}</td>
                    <td>
                      <span className="xbar" style={{ gridTemplateColumns: "minmax(0,1fr) 48px" }}>
                        <span className="tr" aria-hidden="true"><span className="fl" style={{ width: `${Math.max(0, (h.risk_share ?? 0) * 100)}%` }} /></span>
                        <span className="num">{h.risk_share == null ? "—" : pct(h.risk_share)}</span>
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ padding: "8px 14px 12px", display: "flex", flexDirection: "column", gap: 2 }}>
            {d.excluded.length > 0 && (
              <small style={{ color: "var(--muted)" }}>
                Excluded: {d.excluded.map((e) => `${e.name} (${e.first_price ? `prices from ${date(e.first_price)}` : "no prices"})`).join(", ")} <Chip>{pct(d.excluded.reduce((s, e) => s + e.weight, 0), 1)} of value</Chip>
              </small>
            )}
            {d.proxies.length > 0 && (
              <small style={{ color: "var(--muted)" }}>
                Extended: {d.proxies.map((p) => `${p.name} ${monthYear(p.from)} – ${monthYear(p.to)} via ${p.proxy_name}`).join(", ")}
              </small>
            )}
            {d.notes.map((n) => (
              <small key={n} style={{ color: "var(--muted)" }}>{n}</small>
            ))}
          </div>
        </>
      )}
    </Panel>
  );
}

function Tile({ label, v, sub }: { label: string; v: string; sub?: string }) {
  return (
    <div className="tile">
      <span className="label">{label}</span>
      <span className="v">{v}</span>
      {sub && <span className="d" style={{ color: "var(--muted)" }}>{sub}</span>}
    </div>
  );
}

const short = (s: string) => (s.length > 14 ? s.slice(0, 13) + "…" : s);

/** Diverging scale: cool blue for +1, warm orange for −1, the surface colour at 0. */
function cell(v: number | null): string {
  if (v == null) return "var(--surface-2)";
  const side = v >= 0 ? "var(--c1)" : "var(--c2)";
  return `color-mix(in srgb, ${side} ${Math.round(Math.abs(v) * 70)}%, var(--surface-2))`;
}

function Heatmap({ m }: { m: Matrix }) {
  const n = m.labels.length;
  if (n < 2 || !m.matrix.length) return <p style={{ color: "var(--muted)", fontSize: 12 }}>Needs at least two series with price history.</p>;
  return (
    <div style={{ overflowX: "auto" }}>
      <div role="table" aria-label="Correlation matrix" style={{ display: "grid", gridTemplateColumns: `minmax(90px,130px) repeat(${n}, minmax(34px,1fr))`, gap: 2, fontSize: 11, minWidth: 130 + n * 36 }}>
        <span />
        {m.labels.map((l) => (
          <span key={l} title={m.extended?.[m.labels.indexOf(l)] ? `${l} (partly proxy-extended)` : l} style={{ color: "var(--fg-2)", writingMode: "vertical-rl", transform: "rotate(180deg)", height: 92, overflow: "hidden", whiteSpace: "nowrap", justifySelf: "center" }}>{short(l)}{m.extended?.[m.labels.indexOf(l)] ? " †" : ""}</span>
        ))}
        {m.labels.map((row, i) => (
          <div key={row} style={{ display: "contents" }}>
            <span title={m.extended?.[i] ? `${row} (partly proxy-extended)` : row} style={{ color: "var(--fg-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", alignSelf: "center" }}>{short(row)}{m.extended?.[i] ? " †" : ""}</span>
            {m.matrix[i]!.map((v, j) => (
              <span
                key={j}
                className="num"
                title={`${row} × ${m.labels[j]}: ${v == null ? "no variance" : decimal(v, 2)}`}
                style={{ background: cell(v), color: "var(--fg)", textAlign: "center", padding: "6px 0", borderRadius: 3 }}
              >
                {n <= 13 ? (v == null ? "·" : decimal(v, 2)) : ""}
              </span>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
