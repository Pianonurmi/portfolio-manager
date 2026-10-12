// Leverage (Nordnet securities credit): collateral value, credit used, tier and margin-call distance now, a
// "borrow to invest" what-if with shock tests, the tier conditions and per-holding collateral rates you can override.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { date, decimal, money, pct } from "../lib/format";
import { useToast } from "./Toasts";
import { Chip, Empty, Panel, Skeleton } from "./ui";

type Check = { what: string; value: number; limit: number; ok: boolean };
type Holding = { key: string; name: string; kind: "stock" | "etf" | "fund" | "other"; value: number; pawn: number; rate: number; collateral: number; listed: boolean; instrument_id?: number | null; id?: number | null };
type Report = {
  market_value: number; pledged_value: number; average_rate: number; largest_share: number; bonus: number; collateral_value: number; effective_rate: number;
  holdings: Holding[]; loan: number; equity: number; leverage: number | null; loan_to_collateral: number | null; headroom: number;
  tier: { tier: "kulta" | "hopea" | "base"; label: string; rate: number; checks: Record<"kulta" | "hopea", Check[]> };
  interest: { gross: number; after_tax: number; rate_after_tax: number };
  margin_call_drop: { all: number | null; largest: number | null };
  shocks: { fall: number; loan_to_collateral: number; margin_call: boolean }[];
};
type Data = {
  account: { id: number; name: string }; extra: number; deductible: number; collateral_as_of: string | null; manual: string[];
  terms: { rates: Record<string, number>; tiers: Record<string, Record<string, number>>; max_rate: number; source: string };
  notes: string[]; now: Report; after: Report | null;
};

function useDebounced<T>(v: T, ms = 400): T {
  const [d, setD] = useState(v);
  useEffect(() => { const t = setTimeout(() => setD(v), ms); return () => clearTimeout(t); }, [v, ms]);
  return d;
}

const lev = (x: number | null) => (x == null ? "—" : `×${decimal(x, 2)}`);
const drop = (x: number | null) => (x == null ? "no credit" : `−${pct(x, 0)}`);
const checkVal = (v: number, lim: number) => (Math.abs(v) <= 1.5 && Math.abs(lim) <= 1.5 ? pct(v, 1) : money(v));

export function Leverage() {
  const qc = useQueryClient();
  const { toast } = useToast();
  const [extra, setExtra] = useState(0);
  const [into, setInto] = useState("");
  const dx = useDebounced(extra);
  const qs = new URLSearchParams();
  if (dx > 0) { qs.set("extra", String(dx)); if (into) qs.set("into", into); }
  const res = useQuery({ queryKey: ["leverage", dx, into], queryFn: () => api<Data>(`/api/portfolio/leverage${qs.size ? `?${qs}` : ""}`), retry: false, placeholderData: (p) => p });
  const refresh = useMutation({
    mutationFn: () => api<{ run_id: number }>("/api/portfolio/leverage/refresh", { method: "POST" }),
    onSuccess: () => { toast("Fetching Nordnet rates…"); setTimeout(() => void qc.invalidateQueries({ queryKey: ["leverage"] }), 15000); },
    onError: (e) => toast(e instanceof Error ? e.message : "Could not refresh"),
  });
  const setRate = async (h: Holding, rate: number | null) => {
    const id = Number(h.key); // the key is the instrument id
    if (!Number.isFinite(id)) return;
    try {
      await api(`/api/portfolio/leverage/collateral/${id}`, { method: "PUT", body: { rate } });
      void qc.invalidateQueries({ queryKey: ["leverage"] });
    } catch (e) {
      toast(e instanceof Error ? e.message : "Could not save");
    }
  };

  if (res.isLoading) return <Panel title="Leverage (Nordnet credit)" style={{ marginTop: 12 }}><Skeleton w={240} /></Panel>;
  if (res.isError || !res.data) return <Panel title="Leverage (Nordnet credit)" style={{ marginTop: 12 }}><Empty title="No credit view">{(res.error as Error | null)?.message ?? "Not available."}</Empty></Panel>;
  const d = res.data, n = d.now, a = d.after;
  const shown = a ?? n;
  const meta = `${d.account.name}${d.collateral_as_of ? ` · rates as of ${date(d.collateral_as_of)}` : ""}`;
  const warn = { color: "var(--warn)" };

  return (
    <Panel title="Leverage (Nordnet credit)" style={{ marginTop: 12 }} meta={meta}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div className="tiles" style={{ borderRadius: 8, border: "1px solid var(--line)" }}>
          <div className="tile"><span className="label">Collateral value</span><span className="v">{money(n.collateral_value)}</span>
            <span className="d" style={{ color: "var(--muted)" }}>{pct(n.effective_rate, 1)} of {money(n.market_value)}{n.bonus > 0.001 && ` · +${decimal(n.bonus * 100, 1)} pp diversification bonus`}</span></div>
          <div className="tile"><span className="label">Credit used</span><span className="v">{money(n.loan)}</span>
            <span className="d" style={{ color: "var(--muted)" }}>leverage {lev(n.leverage)} · loan-to-collateral {n.loan_to_collateral == null ? "—" : pct(n.loan_to_collateral, 0)}</span></div>
          <div className="tile"><span className="label">Tier</span><span className="v">{n.tier.label} · {pct(n.tier.rate, 2)}</span>
            <span className="d" style={{ color: "var(--muted)" }}>{pct(n.interest.rate_after_tax, 2)} after tax</span></div>
          <div className="tile"><span className="label">Margin call at</span><span className="v">{drop(n.margin_call_drop.all)}</span>
            <span className="d" style={{ color: "var(--muted)" }}>{n.margin_call_drop.all == null ? "nothing borrowed" : `all holdings; largest alone ${drop(n.margin_call_drop.largest)}`}</span></div>
        </div>

        <div>
          <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>Borrow to invest</div>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
            <label className="field" style={{ width: 130 }}>
              <span style={{ fontSize: 12, color: "var(--fg-2)" }}>Borrow €</span>
              <input className="input mono" type="number" min={0} step={1000} value={extra} onChange={(e) => e.target.value !== "" && setExtra(Math.max(0, Number(e.target.value)))} />
            </label>
            <label className="field" style={{ minWidth: 0, maxWidth: "100%" }}>
              <span style={{ fontSize: 12, color: "var(--fg-2)" }}>Buy</span>
              <select className="select" value={into} onChange={(e) => setInto(e.target.value)}>
                <option value="">Spread over my holdings</option>
                {n.holdings.map((h) => <option key={h.key} value={h.key}>{h.name}</option>)}
              </select>
            </label>
          </div>
          {a && (
            <div className="tbl-wrap" style={{ marginTop: 8 }}><table>
              <thead><tr><th /><th className="r">Now</th><th className="r">After</th></tr></thead>
              <tbody>
                <tr style={{ cursor: "default" }}><td>Market value</td><td className="r num">{money(n.market_value)}</td><td className="r num">{money(a.market_value)}</td></tr>
                <tr style={{ cursor: "default" }}><td>Credit</td><td className="r num">{money(n.loan)}</td><td className="r num">{money(a.loan)}</td></tr>
                <tr style={{ cursor: "default" }}><td>Leverage</td><td className="r num">{lev(n.leverage)}</td><td className="r num">{lev(a.leverage)}</td></tr>
                <tr style={{ cursor: "default" }}><td>Loan-to-collateral</td><td className="r num">{n.loan_to_collateral == null ? "—" : pct(n.loan_to_collateral, 0)}</td><td className="r num">{a.loan_to_collateral == null ? "—" : pct(a.loan_to_collateral, 0)}</td></tr>
                <tr style={{ cursor: "default" }}><td>Tier and rate</td><td className="r num">{n.tier.label} {pct(n.tier.rate, 2)}</td><td className="r num">{a.tier.label} {pct(a.tier.rate, 2)}</td></tr>
                <tr style={{ cursor: "default" }}><td>Interest a year, after tax</td><td className="r num">{money(n.interest.after_tax)}</td><td className="r num">{money(a.interest.after_tax)}</td></tr>
                <tr style={{ cursor: "default" }}><td>Margin call at</td><td className="r num">{drop(n.margin_call_drop.all)}</td><td className="r num" style={a.margin_call_drop.all != null && a.margin_call_drop.all < 0.25 ? warn : undefined}>{drop(a.margin_call_drop.all)}</td></tr>
              </tbody>
            </table></div>
          )}
          <div className="tbl-wrap" style={{ marginTop: 8 }}><table>
            <caption style={{ textAlign: "left", fontSize: 12, color: "var(--fg-2)", paddingBottom: 4 }}>If markets fall{a ? " (after borrowing)" : ""}</caption>
            <thead><tr><th>Fall</th><th className="r">Loan-to-collateral</th><th className="r">Margin call</th></tr></thead>
            <tbody>{shown.shocks.map((s) => (
              <tr key={s.fall} style={{ cursor: "default" }}>
                <td className="num">−{pct(s.fall, 0)}</td><td className="r num">{pct(s.loan_to_collateral, 0)}</td>
                <td className="r" style={s.margin_call ? warn : undefined}>{s.margin_call ? "Yes" : "No"}</td>
              </tr>))}</tbody>
          </table></div>
        </div>

        <div>
          <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>Tier conditions (all three must hold; Nordnet checks daily)</div>
          <div className="tbl-wrap"><table>
            <thead><tr><th>Condition</th><th className="r">Value</th><th className="r">Limit</th><th className="r" /></tr></thead>
            <tbody>{(["kulta", "hopea"] as const).flatMap((t) => [
              <tr key={t} style={{ cursor: "default" }}><td colSpan={4} style={{ fontWeight: 600 }}>{t === "kulta" ? "Kulta" : "Hopea"} · {pct(d.terms.rates[t] ?? 0, 2)}{n.tier.tier === t && <> <Chip tone="ok">current</Chip></>}</td></tr>,
              ...n.tier.checks[t].map((c, i) => (
                <tr key={`${t}${i}`} style={{ cursor: "default" }}>
                  <td>{c.what}</td><td className="r num">{checkVal(c.value, c.limit)}</td><td className="r num">{checkVal(c.limit, c.limit)}</td>
                  <td className="r" style={c.ok ? { color: "var(--gain, inherit)" } : warn} aria-label={c.ok ? "met" : "not met"}>{c.ok ? "✓" : "✗"}</td>
                </tr>)),
            ])}</tbody>
          </table></div>
        </div>

        <details>
          <summary style={{ cursor: "pointer", fontSize: 12 }}>Collateral by holding ({n.holdings.length})</summary>
          <div className="tbl-wrap" style={{ marginTop: 6 }}><table>
            <thead><tr><th>Holding</th><th className="r hide-sm">Value</th><th className="r hide-sm">Nordnet rate</th><th className="r">With bonus</th><th className="r hide-sm">Collateral</th><th className="r">Your rate %</th></tr></thead>
            <tbody>{n.holdings.map((h) => <HoldingRow key={h.key} h={h} manual={d.manual.includes(h.key)} onSave={(r) => void setRate(h, r)} />)}</tbody>
          </table></div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 6, flexWrap: "wrap" }}>
            <button className="btn" disabled={refresh.isPending} onClick={() => refresh.mutate()}>Refresh Nordnet rates</button>
            <small style={{ color: "var(--muted)" }}>Leave a rate empty to use Nordnet's published one.</small>
          </div>
        </details>

        <HomeEquity borrow={dx} />

        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {d.notes.map((t) => <small key={t} style={{ color: "var(--muted)" }}>{t}</small>)}
          <small style={{ color: "var(--muted)" }}>Interest is shown after the {pct(d.deductible, 0)} deduction. {d.terms.source}</small>
        </div>
      </div>
    </Panel>
  );
}

function HoldingRow({ h, manual, onSave }: { h: Holding; manual: boolean; onSave: (r: number | null) => void }) {
  const shown = Math.round(h.pawn * 1000) / 10;
  const [v, setV] = useState(manual ? String(shown) : "");
  useEffect(() => { setV(manual ? String(shown) : ""); }, [manual, shown]);
  const commit = () => {
    if (v.trim() === "") { if (manual) onSave(null); return; }
    const x = Number(v);
    if (!Number.isFinite(x) || x < 0 || x > 90) { setV(manual ? String(shown) : ""); return; }
    if (!manual || x !== shown) onSave(x / 100);
  };
  return (
    <tr style={{ cursor: "default" }}>
      <td><div className="inst">{h.name}{manual && <> <Chip tone="info">yours</Chip></>}{!h.listed && <> <Chip tone="warn">not listed</Chip></>}<small>{h.kind}</small></div></td>
      <td className="r num hide-sm">{money(h.value)}</td>
      <td className="r num hide-sm">{manual ? "—" : pct(h.pawn, 0)}</td>
      <td className="r num">{pct(h.rate, 1)}</td>
      <td className="r num hide-sm">{money(h.collateral)}</td>
      <td className="r"><input aria-label={`Own collateral rate for ${h.name}`} className="input mono" type="number" min={0} max={90} step={5} style={{ width: 64 }} placeholder={String(shown)} value={v}
        onChange={(e) => setV(e.target.value)} onBlur={commit} onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()} /></td>
    </tr>
  );
}

type HomeCmp = {
  amount: number; euribor_12m: number; home_from_net_worth: { home_value: number | null; mortgage: number | null }; cheaper: "mortgage" | "nordnet";
  nordnet: { rate: number; rate_after_tax: number; interest_after_tax: number; repayment: number | null; margin_call_drop: number | null; risk: string };
  mortgage: { rate: number; rate_after_tax: number; interest_after_tax: number; monthly_payment: number; years: number; room: number | null; fits: boolean; loan_to_value_after: number | null; risk: string };
  notes: string[];
};

// Borrow against the home instead? Compares a mortgage top-up with the Nordnet credit for the same amount.
function HomeEquity({ borrow }: { borrow: number }) {
  const [open, setOpen] = useState(false);
  const [own, setOwn] = useState(50000);
  const [margin, setMargin] = useState("0.8");
  const [years, setYears] = useState("20");
  const [ltv, setLtv] = useState("75");
  const [home, setHome] = useState("");
  const [mortgage, setMortgage] = useState("");
  const amount = borrow > 0 ? borrow : own;
  const key = useDebounced({ amount, margin, years, ltv, home, mortgage });
  const m = Number(key.margin), y = Number(key.years), l = Number(key.ltv);
  const optional = (v: string) => (v.trim() === "" || !Number.isFinite(Number(v)) ? null : Number(v));
  const valid = key.amount > 0 && Number.isFinite(m) && m >= 0 && Number.isInteger(y) && y > 0 && Number.isFinite(l) && l > 0 && l <= 100;
  const res = useQuery({
    queryKey: ["home-equity", key], enabled: open && valid, retry: false, placeholderData: (p) => p,
    queryFn: () => api<HomeCmp>("/api/portfolio/financing/home-equity", { method: "POST", body: {
      amount: key.amount, mortgage_margin: m / 100, years: y, bank_ltv: l / 100, home_value: optional(key.home), mortgage_now: optional(key.mortgage) } }),
  });
  const r = res.data;
  const muted = { color: "var(--muted)" };
  const hv = r?.home_from_net_worth;
  const noHome = r != null && (hv?.home_value == null || hv.home_value <= 0);
  const cheap = (k: "mortgage" | "nordnet") => r?.cheaper === k && <> <Chip tone="ok">cheaper</Chip></>;
  const row = (label: string, a: React.ReactNode, b: React.ReactNode) => <tr style={{ cursor: "default" }}><td>{label}</td><td className="r num">{a}</td><td className="r num">{b}</td></tr>;
  return (
    <details open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary style={{ cursor: "pointer", fontSize: 12 }}>Borrow against the home instead?</summary>
      <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 6 }}>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
          {borrow > 0 ? <small style={{ ...muted, alignSelf: "center" }}>Comparing {money(borrow)} from the Borrow € above</small> : (
            <label className="field" style={{ width: 130 }}><span style={{ fontSize: 12, color: "var(--fg-2)" }}>Amount €</span>
              <input className="input mono" type="number" min={0} step={1000} value={own} onChange={(e) => e.target.value !== "" && setOwn(Math.max(0, Number(e.target.value)))} /></label>
          )}
          <label className="field" style={{ width: 110 }}><span style={{ fontSize: 12, color: "var(--fg-2)" }}>Mortgage margin %</span>
            <input className="input mono" type="number" min={0} step={0.05} value={margin} onChange={(e) => setMargin(e.target.value)} /></label>
          <label className="field" style={{ width: 90 }}><span style={{ fontSize: 12, color: "var(--fg-2)" }}>Years</span>
            <input className="input mono" type="number" min={1} step={1} value={years} onChange={(e) => setYears(e.target.value)} /></label>
          <label className="field" style={{ width: 110 }}><span style={{ fontSize: 12, color: "var(--fg-2)" }}>Bank LTV limit %</span>
            <input className="input mono" type="number" min={1} max={100} step={5} value={ltv} onChange={(e) => setLtv(e.target.value)} /></label>
          <label className="field" style={{ width: 130 }}><span style={{ fontSize: 12, color: "var(--fg-2)" }}>Home value €</span>
            <input className="input mono" type="number" min={0} step={10000} placeholder={hv?.home_value ? String(Math.round(hv.home_value)) : "optional"} value={home} onChange={(e) => setHome(e.target.value)} /></label>
          <label className="field" style={{ width: 130 }}><span style={{ fontSize: 12, color: "var(--fg-2)" }}>Mortgage now €</span>
            <input className="input mono" type="number" min={0} step={10000} placeholder={hv?.mortgage != null ? String(Math.round(hv.mortgage)) : "optional"} value={mortgage} onChange={(e) => setMortgage(e.target.value)} /></label>
        </div>
        {!valid && <small style={muted}>Enter an amount, margin, years and a loan-to-value limit.</small>}
        {noHome && !home && <small style={muted}>No home in your net worth: enter your home's value.</small>}
        {open && valid && res.isLoading && <Skeleton w={240} />}
        {res.isError && <Empty title="No comparison">{(res.error as Error).message}</Empty>}
        {r && valid && (
          <>
            <div className="tbl-wrap"><table>
              <caption style={{ textAlign: "left", fontSize: 12, color: "var(--fg-2)", paddingBottom: 4 }}>{money(r.amount)} · 12-month Euribor {pct(r.euribor_12m, 2)}</caption>
              <thead><tr><th /><th className="r">Nordnet credit{cheap("nordnet")}</th><th className="r">Mortgage top-up{cheap("mortgage")}</th></tr></thead>
              <tbody>
                {row("Rate", pct(r.nordnet.rate, 2), pct(r.mortgage.rate, 2))}
                {row("Rate after tax", pct(r.nordnet.rate_after_tax, 2), pct(r.mortgage.rate_after_tax, 2))}
                {row("Interest a year, after tax", money(r.nordnet.interest_after_tax), money(r.mortgage.interest_after_tax))}
                {row("Monthly payment", "—", `${money(r.mortgage.monthly_payment)} · ${r.mortgage.years} y`)}
                {row("Margin call at", drop(r.nordnet.margin_call_drop), "—")}
                {row("Room under the bank's limit", "—", <span style={r.mortgage.fits ? undefined : { color: "var(--warn)" }}>{r.mortgage.room == null ? "—" : money(r.mortgage.room)}{!r.mortgage.fits && " · does not fit"}</span>)}
                {r.mortgage.loan_to_value_after != null && row("Loan-to-value after", "—", pct(r.mortgage.loan_to_value_after, 0))}
              </tbody>
            </table></div>
            <small style={muted}>Nordnet: {r.nordnet.risk}</small>
            <small style={muted}>Mortgage: {r.mortgage.risk}</small>
            {r.notes.map((t) => <small key={t} style={muted}>{t}</small>)}
          </>
        )}
      </div>
    </details>
  );
}
