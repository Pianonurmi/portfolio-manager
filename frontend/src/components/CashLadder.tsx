// Cash and deposit rates: market references next to the household's own bank offers, and an interest-ladder
// planner (term deposits of 3-24 months plus a liquid savings account) with the month-by-month result.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { date, money, pct } from "../lib/format";
import { useToast } from "./Toasts";
import { Chip, Empty, Panel, Skeleton } from "./ui";

type Product = { rate: number; source: "yours" | "market"; label: string; as_of: string | null; market_rate: number | null; bank: string | null };
type Rates = { products: Record<string, Product>; series: { key: string; label: string; date: string; value: number }[]; updated_at: string | null };
type Rung = { product: string; months: number; amount: number; rate: number; start: string; maturity: string; interest_gross: number; interest_net: number };
type Month = { month: number; date: string; maturing: number; interest_gross: number; interest_tax: number; interest_net: number; savings: number; deposits: number; total: number };
type Cmp = { label: string; net_interest: number; final_value: number; effective_net_yield: number };
type Ladder = {
  inputs: { tax_rate: number; horizon: number };
  rungs: Rung[]; months: Month[];
  totals: { gross_interest: number; tax: number; net_interest: number; accrued_interest_net: number; final_value: number; effective_net_yield: number };
  available: { now: number; next: { date: string; amount: number }[] };
  comparison: Cmp[];
};

const PRODUCTS = ["savings", "3m", "6m", "12m", "24m"];
const NAMES: Record<string, string> = { savings: "Savings account", "3m": "3 months", "6m": "6 months", "12m": "12 months", "24m": "24 months" };
const TENORS = [3, 6, 12, 24];
const ROLLOVERS = [
  { v: "longest", t: "Re-deposit at the longest term (classic ladder)" },
  { v: "same", t: "Renew at the same term" },
  { v: "savings", t: "Move to the savings account" },
];
const pctText = (f: number) => String(Math.round(f * 100000) / 1000);

function useDebounced<T>(v: T, ms = 400): T {
  const [d, setD] = useState(v);
  useEffect(() => { const t = setTimeout(() => setD(v), ms); return () => clearTimeout(t); }, [v, ms]);
  return d;
}

export function CashLadder() {
  return (
    <Panel title="Cash & interest ladder" style={{ marginTop: 12 }} bodyless meta={<span>savings account and term deposits</span>}>
      <RatesTable />
      <Builder />
    </Panel>
  );
}

function RatesTable() {
  const qc = useQueryClient();
  const { toast } = useToast();
  const q = useQuery({ queryKey: ["cash-rates"], queryFn: () => api<Rates>("/api/cash/rates") });
  const [own, setOwn] = useState<Record<string, { rate: string; bank: string }>>({});
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!q.data) return;
    setOwn(Object.fromEntries(PRODUCTS.map((p) => {
      const x = q.data.products[p]!;
      return [p, x.source === "yours" ? { rate: pctText(x.rate), bank: x.bank ?? "" } : { rate: "", bank: "" }];
    })));
    setLoaded(true);
  }, [q.data]);

  const save = useMutation({
    mutationFn: () => api<Rates>("/api/cash/rates", {
      method: "PUT",
      body: { products: Object.fromEntries(PRODUCTS.map((p) => {
        const o = own[p] ?? { rate: "", bank: "" };
        const n = o.rate.trim() === "" ? NaN : Number(o.rate.replace(",", "."));
        return [p, { rate: Number.isFinite(n) ? n / 100 : null, bank: o.bank.trim() || null }];
      })) },
    }),
    onSuccess: (d) => { qc.setQueryData(["cash-rates"], d); qc.invalidateQueries({ queryKey: ["cash-ladder"] }); toast("Rates saved"); },
    onError: (e: Error) => toast(e.message, { error: true }),
  });
  const refresh = useMutation({
    mutationFn: () => api<{ run_id: number }>("/api/cash/rates/refresh", { method: "POST", body: {} }),
    onSuccess: () => {
      toast("Fetching market rates…");
      for (const ms of [4000, 12000]) setTimeout(() => { qc.invalidateQueries({ queryKey: ["cash-rates"] }); qc.invalidateQueries({ queryKey: ["cash-ladder"] }); }, ms);
    },
    onError: (e: Error) => toast(e.message, { error: true }),
  });

  if (q.isLoading || !loaded) return <div style={{ padding: 14 }}><Skeleton w={240} /></div>;
  if (q.isError || !q.data) return <div style={{ padding: 14 }}><Empty title="Rates unavailable">{(q.error as Error)?.message}</Empty></div>;
  const r = q.data;
  const set = (p: string, patch: Partial<{ rate: string; bank: string }>) => setOwn({ ...own, [p]: { ...(own[p] ?? { rate: "", bank: "" }), ...patch } });
  const noMarket = !r.updated_at;

  return (
    <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 8, borderBottom: "1px solid var(--line)" }}>
      <div className="tbl-wrap">
        <table>
          <thead><tr><th>Product</th><th className="r">Market</th><th className="hide-sm">Reference</th><th className="r">Your rate %</th><th>Bank</th><th className="r">Used</th></tr></thead>
          <tbody>
            {PRODUCTS.map((p) => {
              const x = r.products[p]!;
              return (
                <tr key={p} style={{ cursor: "default" }}>
                  <td>{NAMES[p]}</td>
                  <td className="r num">{x.market_rate == null ? "—" : pct(x.market_rate, 2)}</td>
                  <td className="hide-sm" style={{ color: "var(--fg-2)", fontSize: 12 }}>{x.label}{x.as_of ? ` · ${date(x.as_of)}` : ""}</td>
                  <td className="r"><input className="input mono" style={{ width: 80, textAlign: "right" }} inputMode="decimal" placeholder="—" aria-label={`${NAMES[p]} your rate %`} value={own[p]?.rate ?? ""} onChange={(e) => set(p, { rate: e.target.value })} /></td>
                  <td><input className="input" style={{ width: 120 }} placeholder="bank" aria-label={`${NAMES[p]} bank`} value={own[p]?.bank ?? ""} onChange={(e) => set(p, { bank: e.target.value })} /></td>
                  <td className="r num">{pct(x.rate, 2)} <Chip tone={x.source === "yours" ? "ok" : undefined}>{x.source}</Chip></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <button className="btn primary" disabled={save.isPending} onClick={() => save.mutate()}>Save my rates</button>
        <button className="btn" disabled={refresh.isPending} onClick={() => refresh.mutate()}>Refresh market rates</button>
        <small style={{ color: "var(--muted)" }}>
          {noMarket ? "No market rates fetched yet." : `Market data from the ECB, fetched ${date(r.updated_at)}.`} Leave your rate empty to use the market default. Market figures are Finnish bank averages; your bank's offer is usually better, enter it.
        </small>
      </div>
    </div>
  );
}

function Builder() {
  const [amount, setAmount] = useState(50000);
  const [liquid, setLiquid] = useState(5000);
  const [horizon, setHorizon] = useState(24);
  const [rollover, setRollover] = useState("longest");
  const [on, setOn] = useState<Record<number, boolean>>({ 3: true, 6: true, 12: true, 24: true });
  const [share, setShare] = useState<Record<number, number>>({ 3: 25, 6: 25, 12: 25, 24: 25 });

  const body = useMemo(() => ({
    amount, liquid, horizon, rollover,
    rungs: TENORS.filter((t) => on[t]).map((t) => ({ tenor: t, share: share[t] ?? 0 })),
  }), [amount, liquid, horizon, rollover, on, share]);
  const dq = useDebounced(body);
  const res = useQuery({ queryKey: ["cash-ladder", dq], queryFn: () => api<Ladder>("/api/cash/ladder", { body: dq }), retry: false, placeholderData: (p) => p });
  const shareSum = TENORS.filter((t) => on[t]).reduce((a, t) => a + (share[t] ?? 0), 0);

  return (
    <div className="mc-layout" style={{ padding: 14 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
          <Num label="Total cash €" value={amount} step={1000} onChange={(v) => setAmount(Math.max(0, v))} />
          <Num label="Keep liquid €" value={liquid} step={500} onChange={(v) => setLiquid(Math.max(0, v))} />
          <Num label="Horizon (months)" value={horizon} step={6} onChange={(v) => setHorizon(Math.min(120, Math.max(1, Math.round(v))))} />
        </div>
        <label className="field">
          <span style={{ fontSize: 12, color: "var(--fg-2)" }}>When a deposit matures</span>
          <select className="select" value={rollover} onChange={(e) => setRollover(e.target.value)}>
            {ROLLOVERS.map((r) => <option key={r.v} value={r.v}>{r.t}</option>)}
          </select>
        </label>
        <div>
          <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>
            Terms and their share of the deposited money{shareSum > 0 && Math.abs(shareSum - 100) > 0.05 && <span style={{ color: "var(--warn)" }}> · adds up to {shareSum} %, scaled to 100</span>}
          </div>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            {TENORS.map((t) => (
              <div key={t} style={{ display: "flex", flexDirection: "column", gap: 4, width: 100 }}>
                <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13 }}>
                  <input type="checkbox" checked={!!on[t]} onChange={(e) => setOn({ ...on, [t]: e.target.checked })} /> {t} mo
                </label>
                <input className="input mono" type="number" min={0} step={5} disabled={!on[t]} aria-label={`${t} month share %`} value={share[t] ?? 0} onChange={(e) => e.target.value !== "" && setShare({ ...share, [t]: Math.max(0, Number(e.target.value)) })} />
              </div>
            ))}
          </div>
        </div>
        <small style={{ color: "var(--muted)" }}>
          Deposits are covered by the Finnish deposit guarantee up to 100 000 € per bank and person; spread larger sums over several banks. Interest is taxed 30 % at source (capital income); amounts shown are after tax unless marked gross. Simple interest per term, rates held constant.
        </small>
      </div>
      <div className="mc-results">
        {res.isLoading && <Skeleton w={240} />}
        {res.isError && <Empty title="No ladder">{(res.error as Error).message}</Empty>}
        {res.data && <Result d={res.data} />}
      </div>
    </div>
  );
}

function Result({ d }: { d: Ladder }) {
  const t = d.totals;
  const next = d.available.next[0];
  const max = Math.max(1, ...d.months.map((m) => m.maturing));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="tiles" style={{ borderRadius: 8, border: "1px solid var(--line)" }}>
        <div className="tile"><span className="label">Net interest, {d.inputs.horizon} mo</span><span className="v">{money(t.net_interest, "EUR", 2)}</span><span className="d" style={{ color: "var(--muted)" }}>{money(t.gross_interest, "EUR", 2)} gross, {money(t.tax, "EUR", 2)} tax</span></div>
        <div className="tile"><span className="label">Net yield p.a.</span><span className="v">{pct(t.effective_net_yield, 2)}</span><span className="d" style={{ color: "var(--muted)" }}>compounded, after tax</span></div>
        <div className="tile"><span className="label">Next maturity</span><span className="v">{next ? date(next.date) : "—"}</span><span className="d" style={{ color: "var(--muted)" }}>{next ? money(next.amount, "EUR", 2) : "nothing within the horizon"}</span></div>
        <div className="tile"><span className="label">Available now</span><span className="v">{money(d.available.now, "EUR", 2)}</span><span className="d" style={{ color: "var(--muted)" }}>in the savings account</span></div>
      </div>

      {d.rungs.length > 0 && (
        <div className="tbl-wrap"><table>
          <thead><tr><th>Deposit</th><th className="r">Amount</th><th className="r">Rate</th><th className="hide-sm">Starts</th><th>Matures</th><th className="r hide-sm">Net interest</th></tr></thead>
          <tbody>{d.rungs.map((r) => (
            <tr key={r.product} style={{ cursor: "default" }}>
              <td>{r.months} mo</td><td className="r num">{money(r.amount, "EUR", 2)}</td><td className="r num">{pct(r.rate, 2)}</td>
              <td className="hide-sm">{date(r.start)}</td><td>{date(r.maturity)}</td><td className="r num hide-sm">{money(r.interest_net, "EUR", 2)}</td>
            </tr>
          ))}</tbody>
        </table></div>
      )}

      <div>
        <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>Money becoming available per month (principal + net interest)</div>
        <div role="img" aria-label="Money maturing per month" style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 70 }}>
          {d.months.map((m) => (
            <div key={m.month} title={`${date(m.date)}: ${money(m.maturing, "EUR", 2)}`} style={{ flex: "1 1 0", minWidth: 0, height: `${Math.max(m.maturing > 0 ? 4 : 1, (m.maturing / max) * 100)}%`, background: m.maturing > 0 ? "var(--c1)" : "var(--line)", borderRadius: "2px 2px 0 0" }} />
          ))}
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--muted)" }}><span>{date(d.months[0]?.date)}</span><span>{date(d.months[d.months.length - 1]?.date)}</span></div>
      </div>

      <details>
        <summary style={{ cursor: "pointer", fontSize: 12 }}>Month by month</summary>
        <div className="tbl-wrap" style={{ maxHeight: 320, overflowY: "auto" }}><table>
          <thead><tr><th>Month</th><th className="r">Maturing</th><th className="r hide-sm">Interest gross</th><th className="r">Interest net</th><th className="r hide-sm">Savings</th><th className="r">Total</th></tr></thead>
          <tbody>{d.months.map((m) => (
            <tr key={m.month} style={{ cursor: "default" }}>
              <td>{date(m.date)}</td><td className="r num">{m.maturing ? money(m.maturing, "EUR", 2) : "—"}</td>
              <td className="r num hide-sm">{m.interest_gross ? money(m.interest_gross, "EUR", 2) : "—"}</td><td className="r num">{m.interest_net ? money(m.interest_net, "EUR", 2) : "—"}</td>
              <td className="r num hide-sm">{money(m.savings, "EUR", 2)}</td><td className="r num">{money(m.total, "EUR", 2)}</td>
            </tr>
          ))}</tbody>
        </table></div>
        {t.accrued_interest_net > 0 && <small style={{ color: "var(--muted)" }}>Totals include {money(t.accrued_interest_net, "EUR", 2)} net interest accrued on deposits still open at the end.</small>}
      </details>

      <div>
        <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>Compared with the same money elsewhere</div>
        <div className="tbl-wrap"><table>
          <thead><tr><th>Option</th><th className="r">Net interest</th><th className="r">Net yield p.a.</th><th className="r hide-sm">Value at end</th></tr></thead>
          <tbody>{d.comparison.map((c, i) => (
            <tr key={c.label} style={{ cursor: "default", fontWeight: i === 0 ? 600 : undefined }}>
              <td>{c.label}</td><td className="r num">{money(c.net_interest, "EUR", 2)}</td><td className="r num">{pct(c.effective_net_yield, 2)}</td><td className="r num hide-sm">{money(c.final_value, "EUR", 2)}</td>
            </tr>
          ))}</tbody>
        </table></div>
      </div>
    </div>
  );
}

function Num({ label, value, onChange, step }: { label: string; value: number; onChange: (v: number) => void; step: number }) {
  return (
    <label className="field" style={{ width: 130 }}>
      <span style={{ fontSize: 12, color: "var(--fg-2)" }}>{label}</span>
      <input className="input mono" type="number" value={value} step={step} onChange={(e) => e.target.value !== "" && onChange(Number(e.target.value))} />
    </label>
  );
}
