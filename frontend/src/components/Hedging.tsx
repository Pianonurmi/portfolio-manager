// Hedging without selling: simulates protective puts, collars, short mini futures, bear certificates and partial
// sales against an index (beta-weighted, with basis risk), compares them on the same paths after tax, and lists
// the Nordnet products that could do it. Strategy rows are editable; the simulation reruns 400 ms after a change.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import type { Ctx } from "../lib/context";
import { date, decimal, money, pct } from "../lib/format";
import { Legend, LineChart, useCssVar } from "./LineChart";
import { useToast } from "./Toasts";
import { Chip, Empty, Panel, Skeleton } from "./ui";

type Kind = "none" | "put" | "collar" | "short" | "bear" | "sell";
const KIND_LABEL: Record<Kind, string> = { none: "No hedge", put: "Protective put", collar: "Zero-cost collar", short: "Short mini future", bear: "Bear certificate", sell: "Sell part" };
type Strategy = { kind: Kind; ratio: number; tenor: number; strike: number; option_fee_pct: number; option_fee_min: number; clearing_pct: number; mini_cost: number; mini_leverage: number; bear_factor: number; bear_cost: number | null; label: string | null };
type Row = Strategy & { id: number };
type Fit = { key: string; name: string; currency: string; months: number; beta: number; r2: number; resid_vol: number; index_vol: number };
type Info = {
  fits: Fit[]; best: string | null; tax: { value: number; tax_free_share: number; gain_share: number }; rate: number;
  products: number; products_updated: string | null; defaults: Strategy[];
};
type Out = {
  key: string; kind: Kind; label: string; available: boolean; ratio: number; tenor: number; strike: number | null; cap: number | null; premium: number | null; upfront: number; tax_now: number;
  mean: number; p5: number; p10: number; p50: number; p90: number; loss_10: number; loss_20: number; drawdown_p50: number; drawdown_p10: number;
  hedge_tax_mean: number; knockouts: number; capital: number | null; cost: number; scenario: number[];
};
type Product = { isin: string; name: string; kind: string; index: string; financing_level?: number | null; barrier?: number | null; issuer?: string | null; leverage: number; distance_to_stop?: number | null };
type Sim = {
  months: number; paths: number; index: Fit | null;
  market: { value: number; beta: number; resid_vol: number; index_mean: number; index_vol: number; portfolio_mean: number; rate: number; dividend_yield: number; skew: number; gain_share: number; tax_free_share: number; gain_tax: number; losses_usable: boolean };
  strategies: Out[]; scenarios: { fall: number; values: Record<string, number> }[];
  products: { short: Product[]; bear: Product[] }; notes: string[];
};

const MAX_ROWS = 8;
const COLORS = ["--c1", "--c2", "--c3", "--c4", "--c5"] as const;
const signed = (v: number, d = 1) => (v > 0 ? "+" : v < 0 ? "−" : "") + pct(Math.abs(v), d);

function useDebounced<T>(v: T, ms = 400): T {
  const [d, setD] = useState(v);
  useEffect(() => { const t = setTimeout(() => setD(v), ms); return () => clearTimeout(t); }, [v, ms]);
  return d;
}

export function Hedging({ ctx }: { ctx: Ctx }) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const info = useQuery({ queryKey: ["hedging", ctx.scope], queryFn: () => api<Info>(`/api/hedging?scope=${encodeURIComponent(ctx.scope)}`), retry: false });
  const [index, setIndex] = useState<string>("");
  const [months, setMonths] = useState(12);
  const [skew, setSkew] = useState(0.03);
  const [losses, setLosses] = useState(true);
  const [rows, setRows] = useState<Row[] | null>(null);
  const [view, setView] = useState<"chart" | "table">("chart");
  const nextId = useRef(1);
  const colors = [useCssVar("--c1"), useCssVar("--c2"), useCssVar("--c3"), useCssVar("--c4"), useCssVar("--c5")];

  const withIds = (s: Strategy[]): Row[] => s.map((x) => ({ ...x, id: nextId.current++ }));
  useEffect(() => { if (info.data && !rows) setRows(withIds(info.data.defaults.slice(0, MAX_ROWS))); }, [info.data, rows]);

  const body = useDebounced(useMemo(() => rows && ({
    scope: ctx.scope, index: index || null, months, skew, losses_usable: losses, index_vol: null, rate: null, dividend_yield: null,
    strategies: rows.map(({ id: _id, label, ...s }) => ({ ...s, label: label?.trim() ? label.trim() : null })),
  }), [rows, ctx.scope, index, months, skew, losses]));
  const sim = useQuery({
    queryKey: ["hedging-sim", body], enabled: !!body && !!info.data, retry: false, placeholderData: (p) => p,
    queryFn: () => api<Sim>("/api/hedging/simulate", { body }),
  });
  const refresh = useMutation({
    mutationFn: () => api<{ run_id: number }>("/api/hedging/refresh", { method: "POST" }),
    onSuccess: () => { toast("Fetching index levels and Nordnet products…"); setTimeout(() => { void qc.invalidateQueries({ queryKey: ["hedging"] }); void qc.invalidateQueries({ queryKey: ["hedging-sim"] }); }, 20000); },
    onError: (e) => toast(e instanceof Error ? e.message : "Could not refresh"),
  });

  const T = "Hedging without selling";
  if (info.isLoading) return <Panel title={T} style={{ marginTop: 12 }}><Skeleton w={240} /></Panel>;
  if (info.isError || !info.data) return <Panel title={T} style={{ marginTop: 12 }}><Empty title="No hedging view">{(info.error as Error | null)?.message ?? "Not available."}</Empty></Panel>;
  const d = info.data, r = sim.data ?? null;
  const fit = r?.index ?? null;
  const meta = fit ? `vs ${fit.name} · β ${decimal(fit.beta, 2)} · R² ${Math.round(fit.r2 * 100)} %` : "no index data yet";
  const patch = (id: number, p: Partial<Strategy>) => setRows((rs) => rs && rs.map((x) => (x.id === id ? { ...x, ...p } : x)));
  const warn = { color: "var(--warn)" };
  const muted = { color: "var(--muted)" };
  const bestP5 = r ? Math.max(...r.strategies.map((s) => s.p5)) : 0;
  const refreshBtn = (
    <button className="btn" disabled={refresh.isPending} onClick={() => refresh.mutate()}>Refresh index data &amp; products</button>
  );
  const sourceNote = <small style={muted}>{d.products} products{d.products_updated ? ` · ${date(d.products_updated)}` : ""}</small>;
  const chart = r && r.scenarios.length > 0 ? r.strategies.map((s, i) => ({ name: s.label, values: r.scenarios.map((sc) => sc.values[s.key] ?? NaN), color: colors[i % colors.length] ?? "", dashed: i >= COLORS.length })) : [];

  return (
    <Panel title={T} style={{ marginTop: 12 }} meta={meta}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
          <label className="field" style={{ minWidth: 0, maxWidth: "100%" }}>
            <span style={{ fontSize: 12, color: "var(--fg-2)" }}>Index</span>
            <select className="select" value={index} onChange={(e) => setIndex(e.target.value)}>
              <option value="">Best fit{d.best ? ` (${d.fits.find((f) => f.key === d.best)?.name ?? d.best})` : ""}</option>
              {d.fits.map((f) => <option key={f.key} value={f.key}>{f.name} (β {decimal(f.beta, 2)}, R² {Math.round(f.r2 * 100)} %)</option>)}
            </select>
          </label>
          <label className="field">
            <span style={{ fontSize: 12, color: "var(--fg-2)" }}>Horizon</span>
            <select className="select" value={months} onChange={(e) => setMonths(Number(e.target.value))}>
              {[3, 6, 12, 24].map((m) => <option key={m} value={m}>{m} months</option>)}
            </select>
          </label>
          <label className="field" style={{ width: 110 }}>
            <span style={{ fontSize: 12, color: "var(--fg-2)" }}>Vol skew %</span>
            <input className="input mono" type="number" min={0} max={20} step={0.5} value={Math.round(skew * 1e4) / 100}
              onChange={(e) => e.target.value !== "" && setSkew(Math.min(0.2, Math.max(0, Number(e.target.value) / 100)))} />
          </label>
          <label style={{ fontSize: 12, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center", paddingBottom: 6 }}>
            <input type="checkbox" checked={losses} onChange={(e) => setLosses(e.target.checked)} />
            Losses offset other capital income
          </label>
          <span style={{ display: "inline-flex", gap: 8, alignItems: "center", paddingBottom: 2 }}>{refreshBtn}{sourceNote}</span>
        </div>

        {!fit && r && (
          <small style={muted}>No index history yet, so β = 1 is assumed until index data is fetched. {refreshBtn}</small>
        )}

        <div>
          <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>Strategies (share of the β-weighted exposure)</div>
          <div className="tbl-wrap"><table>
            <thead><tr><th>Kind</th><th>Label</th><th className="r">Ratio %</th><th className="r">Months</th><th className="r">Strike %</th><th className="r">Bear ×</th><th /></tr></thead>
            <tbody>{(rows ?? []).map((x, i) => {
              const hasT = x.kind === "put" || x.kind === "collar" || x.kind === "bear";
              const hasK = x.kind === "put" || x.kind === "collar";
              const num = (v: number, step: number, min: number, max: number, aria: string, w: number, on: (n: number) => void) => (
                <input aria-label={aria} className="input mono" type="number" min={min} max={max} step={step} style={{ width: w }} value={v}
                  onChange={(e) => e.target.value !== "" && on(Math.min(max, Math.max(min, Number(e.target.value))))} />);
              return (
                <tr key={x.id} style={{ cursor: "default" }}>
                  <td><select aria-label="Kind" className="select" value={x.kind} onChange={(e) => patch(x.id, { kind: e.target.value as Kind })}>
                    {(Object.keys(KIND_LABEL) as Kind[]).map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}</select></td>
                  <td><input aria-label="Label" className="input" style={{ width: "100%", minWidth: 110 }} placeholder={r?.strategies[i]?.label ?? ""} value={x.label ?? ""} onChange={(e) => patch(x.id, { label: e.target.value })} /></td>
                  <td className="r">{x.kind === "none" ? "—" : num(Math.round(x.ratio * 100), 5, 0, 200, "Ratio %", 64, (n) => patch(x.id, { ratio: n / 100 }))}</td>
                  <td className="r">{hasT ? num(x.tenor, 1, 1, 24, "Tenor months", 56, (n) => patch(x.id, { tenor: Math.round(n) })) : "—"}</td>
                  <td className="r">{hasK ? num(Math.round(x.strike * 100), 1, 30, 120, "Strike %", 60, (n) => patch(x.id, { strike: n / 100 })) : "—"}</td>
                  <td className="r">{x.kind === "bear" ? num(x.bear_factor, 1, 1, 5, "Bear factor", 52, (n) => patch(x.id, { bear_factor: n })) : "—"}</td>
                  <td className="r"><button className="btn" aria-label="Remove strategy" onClick={() => setRows((rs) => rs && rs.filter((y) => y.id !== x.id))}>×</button></td>
                </tr>);
            })}</tbody>
          </table></div>
          <div style={{ display: "flex", gap: 8, marginTop: 6, flexWrap: "wrap" }}>
            <button className="btn" disabled={!rows || rows.length >= MAX_ROWS}
              onClick={() => setRows((rs) => rs && [...rs, ...withIds([{ ...(d.defaults.find((s) => s.kind === "put") ?? d.defaults[0]!), label: null }])])}>Add strategy</button>
            <button className="btn" onClick={() => setRows(withIds(d.defaults.slice(0, MAX_ROWS)))}>Reset to the standard set</button>
          </div>
        </div>

        {sim.isError && <div role="alert" style={{ color: "var(--loss)", fontSize: 13 }}>{(sim.error as Error).message}</div>}
        {!r && sim.isLoading && <Skeleton w={240} />}

        {r && (
          <div style={{ opacity: sim.isFetching ? 0.6 : 1, display: "flex", flexDirection: "column", gap: 12 }}>
            <div className="tbl-wrap"><table>
              <caption style={{ textAlign: "left", fontSize: 12, color: "var(--fg-2)", paddingBottom: 4 }}>
                Change in wealth over {r.months} months, after tax · {r.paths} paths · portfolio {money(r.market.value)}
              </caption>
              <thead><tr>
                <th>Strategy</th>
                <th className="r" title="Mean change in wealth compared with no hedge">Cost</th>
                <th className="r">Median</th>
                <th className="r" title="5th percentile: the bad case">Bad case</th>
                <th className="r hide-sm">Lose &gt; 20 %</th>
                <th className="r hide-sm" title="10th percentile of the deepest fall on the way">Deepest fall</th>
                <th className="r hide-sm" title="Premium paid at the start (for a sale: the tax now)">Upfront</th>
                <th className="r hide-sm">Tax now</th>
              </tr></thead>
              <tbody>{r.strategies.map((s) => {
                const best = s.kind !== "none" && s.p5 === bestP5;
                return (
                  <tr key={s.key} style={{ cursor: "default", fontWeight: best ? 600 : 400 }}>
                    <td><div className="inst">{s.label}{s.available === false && <> <Chip>not at Nordnet</Chip></>}{best && <> <Chip tone="ok">best bad case</Chip></>}
                      {s.knockouts > 0.05 && <small>stopped out {decimal(s.knockouts, 2)}× on average</small>}</div></td>
                    <td className="r num" style={s.cost < -0.03 ? warn : undefined} title={s.cost < -0.03 ? "Costs more than 3 % of wealth" : undefined}>{s.kind === "none" ? "—" : signed(s.cost)}</td>
                    <td className="r num">{signed(s.p50)}</td>
                    <td className="r num">{signed(s.p5)}</td>
                    <td className="r num hide-sm">{pct(s.loss_20, 1)}</td>
                    <td className="r num hide-sm">{signed(s.drawdown_p10)}</td>
                    <td className="r num hide-sm">{s.upfront ? money(s.upfront) : "—"}</td>
                    <td className="r num hide-sm">{s.tax_now ? money(s.tax_now) : "—"}</td>
                  </tr>);
              })}</tbody>
            </table></div>

            <div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 6, fontSize: 12, color: "var(--fg-2)", marginBottom: 6 }}>
                <Legend items={r.strategies.map((s, i) => ({ name: s.label, color: colors[i % colors.length] ?? "" }))} />
                <span className="seg" role="group" aria-label="Payoff view">
                  <button aria-pressed={view === "chart"} onClick={() => setView("chart")}>Chart</button>
                  <button aria-pressed={view === "table"} onClick={() => setView("table")}>Table</button></span>
              </div>
              {view === "chart" ? (
                <LineChart label="Change in wealth by index move" x={r.scenarios.map((s) => s.fall)} xFormat={(v) => signed(Number(v), 0)} format={(v) => signed(v, 0)} height={240} series={chart} />
              ) : (
                <div className="tbl-wrap"><table>
                  <thead><tr><th>Index</th>{r.strategies.map((s) => <th key={s.key} className="r" title={s.label}>{s.label.length > 14 ? s.label.slice(0, 13) + "…" : s.label}</th>)}</tr></thead>
                  <tbody>{r.scenarios.map((sc) => (
                    <tr key={sc.fall} style={{ cursor: "default" }}><td className="num">{signed(sc.fall, 0)}</td>
                      {r.strategies.map((s) => <td key={s.key} className="r num">{signed(sc.values[s.key] ?? NaN)}</td>)}</tr>))}</tbody>
                </table></div>
              )}
              <small style={muted}>if the index moves evenly over one hedge period</small>
            </div>

            <div>
              <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>Nordnet products to do it with</div>
              <div className="grid g-2">
                <ProductList title="Mini shorts" items={r.products.short} detail={(p) => `${decimal(p.leverage, 1)}× · stop ${p.distance_to_stop == null ? "—" : pct(p.distance_to_stop, 0)} away`} />
                <ProductList title="Bear certificates" items={r.products.bear} detail={(p) => `×${decimal(p.leverage, 0)}`} />
              </div>
              <small style={muted}>They trade in the AOT, not in an osakesäästötili. Nordnet Markets brokerage is 0 € over 100 €.</small>
            </div>

            <div className="tiles" style={{ borderRadius: 8, border: "1px solid var(--line)" }}>
              <div className="tile"><span className="label">In an OST</span><span className="v">{pct(d.tax.tax_free_share, 1)}</span>
                <span className="d" style={muted}>sold tax-free first</span></div>
              <div className="tile"><span className="label">Unrealised gain</span><span className="v">{pct(d.tax.gain_share, 0)}</span>
                <span className="d" style={muted}>of {money(d.tax.value)}; selling everything would cost {money(r.market.gain_tax)}</span></div>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {r.notes.map((t) => <small key={t} style={muted}>{t}</small>)}
            </div>
          </div>
        )}
      </div>
    </Panel>
  );
}

function ProductList({ title, items, detail }: { title: string; items: Product[]; detail: (p: Product) => string }) {
  return (
    <div className="tbl-wrap"><table>
      <thead><tr><th>{title}</th><th className="r">Detail</th></tr></thead>
      <tbody>{items.length === 0 ? <tr style={{ cursor: "default" }}><td colSpan={2} style={{ color: "var(--muted)" }}>None found</td></tr> : items.map((p) => (
        <tr key={p.isin} style={{ cursor: "default" }}>
          <td><div className="inst">{p.name}<small className="mono">{p.isin}{p.issuer ? ` · ${p.issuer}` : ""}</small></div></td>
          <td className="r num">{detail(p)}</td>
        </tr>))}</tbody>
    </table></div>
  );
}
