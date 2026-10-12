// Contributions & withdrawals Monte Carlo (Phase D): one set of simulated paths, five withdrawal strategies compared.
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "../lib/api";
import { compact, date as fmtDate, money, pct } from "../lib/format";
import { Legend, LineChart, useCssVar } from "./LineChart";
import { Panel, Skeleton } from "./ui";

const CLASSES = ["equity", "bond", "cash", "other"] as const;
type Cls = (typeof CLASSES)[number];
type Strategy = "fixed_real" | "four_percent" | "percent_of_portfolio" | "guardrails" | "vpw";
const STRATEGY_LABEL: Record<Strategy, string> = {
  fixed_real: "Fixed real amount",
  four_percent: "4 % rule",
  percent_of_portfolio: "Percent of portfolio",
  guardrails: "Guardrails (Guyton–Klinger)",
  vpw: "Variable (VPW)",
};
const DEFAULT_RATE: Partial<Record<Strategy, number>> = { four_percent: 0.04, percent_of_portfolio: 0.04, guardrails: 0.05 };

type Defaults = {
  start_value: number; mix: Record<Cls, number>; fees: number; start_age: number; pension_monthly: number; pension_age: number; inflation: number;
  history: { months: number; from: string | null; to: string | null };
  assumptions: { mean: Record<Cls, number>; vol: Record<Cls, number>; corr: number[][] };
  leverage: { tier_rates: Record<string, number>; tier_limits: Record<string, number>; diversified: boolean; collateral_rate: number; deductible: number; gain_tax: number; gain_share: number } | null;
};
type Policy = "monthly" | "yearly" | "never";
const POLICY_LABEL: Record<Policy, string> = { yearly: "Back to the ratio every year", monthly: "Every month", never: "Borrow once, let it drift" };
const SWEEP = [1, 1.25, 1.5, 2];
type Lev = { ratio: number; policy: Policy; collateral_rate: number | null; sweep: boolean };
type LevResult = { ratio: number; policy: Policy; margin_call: number; first_call_median_year: number | null; ruin: number; interest_per_year: number; forced_tax_per_year: number };
type LevRow = { ratio: number; success: number; at_withdrawal_real_p50: number; at_withdrawal_real_p50_before_tax: number | null; at_withdrawal_real_p10: number; ending_real_p50: number; ending_real_p10: number; safe_withdrawal_rate: number | null; margin_call: number; ruin: number; interest_per_year: number };
type HedgeKind = "none" | "put" | "collar" | "short";
const HEDGE_LABEL: Record<HedgeKind, string> = { none: "None", put: "Protective put", collar: "Zero-cost collar", short: "Short index" };
type Hedge = { kind: HedgeKind; ratio: number; tenor: number; strike: number };
type HedgeResult = { kind: string; ratio: number; premium: number; cap: number | null; cost_per_year: number; payoff_per_year: number };
type Form = {
  start_value: number; start_age: number; horizon_age: number; mix: Record<Cls, number>; fees: number; inflation: number;
  return_model: "assumptions" | "historical"; assumptions: Defaults["assumptions"];
  contributions: { monthly: number; yearly_increase: number | null; until_age: number | null; lumps: { age: number; amount: number }[] };
  withdrawal: { start_age: number; strategy: Strategy; monthly: number; rate: number | null; floor: number; pension_monthly: number; pension_age: number; tax: number };
  target: { age: number; value_real: number } | null;
  use_saved_glide: boolean;
  lev: Lev;
  hedge: Hedge;
};
type Pcts = Record<"p10" | "p25" | "p50" | "p75" | "p90", number[]>;
type Row = { strategy: Strategy; selected: boolean; rate: number | null; success: number; median_yearly_withdrawal: number; first_year_median: number; p10_worst_year: number; median_ending_real: number };
type Result = {
  ages: number[]; withdrawal_start_age: number; real: Pcts; nominal: Pcts; success: number; median_depletion_age: number | null;
  value_at_withdrawal_start: { real_median: number; nominal_median: number };
  first_year: { median: number; p10: number }; phase_average: { median: number; p10: number }; worst_year_p10: number; total_real_median: number;
  ending_real: { p10: number; p50: number; p90: number };
  target: { age: number; value_real: number; probability: number } | null;
  compare: Row[]; safe_withdrawal_rate: number | null; perpetual_withdrawal_rate: number | null; success_target: number;
  history: { months: number; from: string; to: string; cagr: number; warning?: string } | null;
  hedge: HedgeResult | null;
  leverage: LevResult | null; leverage_sweep: LevRow[] | null;
  inputs: { expected_real_return: number }; seed: number; paths: number; ms: number;
};

function Num({ label, value, onChange, step = 1, pctVal, min, max, hint }: { label: string; value: number | null; onChange: (v: number) => void; step?: number; pctVal?: boolean; min?: number; max?: number; hint?: string }) {
  const id = "mc-" + label.replace(/\W+/g, "-").toLowerCase();
  const shown = value === null ? "" : pctVal ? Math.round(value * 1e4) / 100 : value;
  return (
    <div className="field">
      <label htmlFor={id}>{label}{pctVal ? " %" : ""}</label>
      <input className="input" type="number" id={id} step={step} min={min} max={max} value={shown}
        onChange={(e) => e.target.value !== "" && onChange(pctVal ? Number(e.target.value) / 100 : Number(e.target.value))} />
      {hint && <span className="hint">{hint}</span>}
    </div>
  );
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <fieldset style={{ border: "1px solid var(--line)", borderRadius: 8, padding: "8px 12px 12px", margin: 0, minWidth: 0 }}>
      <legend style={{ fontSize: 12, color: "var(--fg-2)", padding: "0 6px" }}>{title}</legend>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(130px, 1fr))", gap: 10 }}>{children}</div>
    </fieldset>
  );
}

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="kpi"><span className="label">{label}</span><span className="v mono">{value}</span>{sub && <span style={{ fontSize: 11, color: "var(--muted)" }}>{sub}</span>}</div>
  );
}

export function MonteCarlo() {
  const defs = useQuery({ queryKey: ["mc-defaults"], queryFn: () => api<Defaults>("/api/plan/montecarlo/defaults") });
  const targets = useQuery({ queryKey: ["targets"], queryFn: () => api<{ glide?: { date: string; mix: Record<string, number> }[] }>("/api/portfolio/targets") });
  const [f, setF] = useState<Form | null>(null);
  const [res, setRes] = useState<Result | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<"real" | "nominal">("real");
  const timer = useRef<number | undefined>(undefined);
  const seq = useRef(0);
  const c1 = useCssVar("--c1");

  useEffect(() => {
    const d = defs.data;
    if (!d || f) return;
    const wStart = Math.max(d.start_age + 1, Math.min(d.pension_age, 67));
    setF({
      start_value: d.start_value, start_age: d.start_age, horizon_age: 95, mix: d.mix, fees: d.fees, inflation: d.inflation,
      return_model: "assumptions", assumptions: d.assumptions,
      contributions: { monthly: 1000, yearly_increase: null, until_age: null, lumps: [] },
      withdrawal: { start_age: wStart, strategy: "four_percent", monthly: 2500, rate: null, floor: 0, pension_monthly: d.pension_monthly, pension_age: d.pension_age, tax: 0 },
      target: null,
      use_saved_glide: false,
      lev: { ratio: 1, policy: "yearly", collateral_rate: d.leverage?.collateral_rate ?? null, sweep: false },
      hedge: { kind: "none", ratio: 0.5, tenor: 3, strike: 0.9 },
    });
  }, [defs.data, f]);

  useEffect(() => {
    if (!f) return;
    window.clearTimeout(timer.current);
    const n = ++seq.current;
    setBusy(true);
    timer.current = window.setTimeout(async () => {
      try {
        const { lev, hedge, ...rest } = f;
        const on = lev.ratio > 1 || lev.sweep;
        const body = {
          ...rest, paths: 2000,
          ...(on ? { leverage: { ratio: Math.max(1, lev.ratio), policy: lev.policy, rate: null, collateral_rate: lev.collateral_rate } } : {}),
          ...(lev.sweep ? { leverage_sweep: SWEEP } : {}),
          hedge: hedge.kind === "none" ? null : { kind: hedge.kind, ratio: hedge.ratio, tenor: hedge.tenor, strike: hedge.kind === "short" ? 1 : hedge.strike, skew: 0.03, fee: 0.002, short_cost: 0.015 },
        };
        const r = await api<Result>("/api/plan/montecarlo", { body });
        if (n === seq.current) { setRes(r); setErr(null); }
      } catch (e) {
        if (n === seq.current) setErr(e instanceof Error ? e.message : "Simulation failed");
      } finally {
        if (n === seq.current) setBusy(false);
      }
    }, 400);
    return () => window.clearTimeout(timer.current);
  }, [f]);

  if (defs.isLoading || !f) return <Panel title="Contributions & withdrawals (Monte Carlo)"><Skeleton w={240} /></Panel>;
  const set = (patch: Partial<Form>) => setF({ ...f, ...patch });
  const w = f.withdrawal, ct = f.contributions, a = f.assumptions;
  const setW = (patch: Partial<Form["withdrawal"]>) => set({ withdrawal: { ...w, ...patch } });
  const setC = (patch: Partial<Form["contributions"]>) => set({ contributions: { ...ct, ...patch } });
  const mixSum = CLASSES.reduce((s, k) => s + f.mix[k], 0) || 1;
  const hasGlide = !!targets.data?.glide?.length;
  const hist = defs.data!.history;
  const histOk = hist.months >= 36;
  const lv = f.lev;
  const setL = (patch: Partial<Lev>) => set({ lev: { ...lv, ...patch } });
  const hg = f.hedge;
  const setH = (patch: Partial<Hedge>) => set({ hedge: { ...hg, ...patch } });
  const lumpSet = (i: number, patch: Partial<{ age: number; amount: number }>) => setC({ lumps: ct.lumps.map((l, j) => (j === i ? { ...l, ...patch } : l)) });
  const p = res ? res[view] : null;
  const money0 = (v: number) => money(v);

  return (
    <Panel title="Contributions & withdrawals (Monte Carlo)" meta={busy ? "running…" : res ? `${res.paths} paths · ${res.ms} ms · seed ${res.seed}` : ""}>
      <div className="mc-layout">
      <div className="mc-inputs">
        <Group title="Start">
          <Num label="Portfolio value €" value={f.start_value} step={1000} min={0} onChange={(v) => set({ start_value: v })} />
          <Num label="Age" value={f.start_age} min={0} max={100} onChange={(v) => set({ start_age: Math.round(v) })} />
          <Num label="Horizon age" value={f.horizon_age} min={1} max={120} onChange={(v) => set({ horizon_age: Math.round(v) })} />
        </Group>
        <Group title="Mix">
          {CLASSES.map((k) => <Num key={k} label={k[0]!.toUpperCase() + k.slice(1)} pctVal value={f.mix[k] / mixSum} step={1} min={0} max={100} onChange={(v) => set({ mix: { ...f.mix, [k]: v } })} />)}
          <div className="field"><label>&nbsp;</label><button className="btn" onClick={() => set({ mix: defs.data!.mix, start_value: defs.data!.start_value, fees: defs.data!.fees })}>From my portfolio</button></div>
          {hasGlide && (
            <label style={{ gridColumn: "1 / -1", fontSize: 12, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center" }}>
              <input type="checkbox" checked={f.use_saved_glide} onChange={(e) => set({ use_saved_glide: e.target.checked, return_model: e.target.checked ? "assumptions" : f.return_model })} />
              Follow my glide path (from the allocation targets) instead of a fixed mix
            </label>
          )}
        </Group>
        <Group title="Return model">
          <div className="field" style={{ gridColumn: "1 / -1" }}><label>Model</label>
            <div className="seg" role="group" aria-label="Return model">
              <button aria-pressed={f.return_model === "assumptions"} onClick={() => set({ return_model: "assumptions" })}>Assumptions</button>
              <button aria-pressed={f.return_model === "historical"} disabled={!histOk} title={histOk ? "" : "Needs 36 months of price history"} onClick={() => set({ return_model: "historical" })}>Historical</button>
            </div></div>
          <div style={{ gridColumn: "1 / -1", fontSize: 12, color: "var(--fg-2)" }}>
            {f.return_model === "historical"
              ? histOk ? `Resamples ${hist.months} months of your own portfolio (${fmtDate(hist.from)} – ${fmtDate(hist.to)}) in 12-month blocks, today's weights. Mix and fees above do not apply.` : "Not enough history."
              : <details><summary style={{ cursor: "pointer" }}>Edit assumptions (nominal, per year)</summary>
                <table style={{ marginTop: 6 }}><thead><tr><th>Class</th><th className="r">Return %</th><th className="r">Volatility %</th>{CLASSES.map((k) => <th key={k} className="r">ρ {k}</th>)}</tr></thead>
                  <tbody>{CLASSES.map((k, i) => (
                    <tr key={k} style={{ cursor: "default" }}><td>{k}</td>
                      {(["mean", "vol"] as const).map((m) => <td key={m} className="r"><input aria-label={`${k} ${m}`} className="input" type="number" step={0.5} style={{ width: 70 }} value={Math.round(a[m][k] * 1e4) / 100}
                        onChange={(e) => set({ assumptions: { ...a, [m]: { ...a[m], [k]: Number(e.target.value) / 100 } } })} /></td>)}
                      {CLASSES.map((c2, j) => <td key={c2} className="r">{j === i ? "1" : <input aria-label={`correlation ${k} ${c2}`} className="input" type="number" step={0.1} min={-1} max={1} style={{ width: 56 }} value={a.corr[i]![j]}
                        onChange={(e) => { const v = Number(e.target.value); const corr = a.corr.map((r) => [...r]); corr[i]![j] = v; corr[j]![i] = v; set({ assumptions: { ...a, corr } }); }} />}</td>)}
                    </tr>))}</tbody></table></details>}
          </div>
        </Group>
        <Group title="Fees & inflation">
          <Num label="Fees p.a." pctVal step={0.05} value={f.fees} min={0} max={10} onChange={(v) => set({ fees: v })} hint="from your funds' TER" />
          <Num label="Inflation p.a." pctVal step={0.1} value={f.inflation} min={-5} max={20} onChange={(v) => set({ inflation: v })} />
        </Group>
        <Group title="Contributions">
          <Num label="Per month €" value={ct.monthly} step={50} min={0} onChange={(v) => setC({ monthly: v })} />
          <Num label="Yearly increase" pctVal step={0.1} value={ct.yearly_increase ?? f.inflation} onChange={(v) => setC({ yearly_increase: v })} hint="default: inflation" />
          <Num label="Until age" value={ct.until_age ?? w.start_age} min={0} max={120} onChange={(v) => setC({ until_age: v })} hint="default: withdrawal start" />
          <div style={{ gridColumn: "1 / -1" }}>
            {ct.lumps.map((l, i) => (
              <div key={i} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4 }}>
                <input aria-label="Lump sum age" className="input" type="number" style={{ width: 70 }} value={l.age} onChange={(e) => lumpSet(i, { age: Number(e.target.value) })} />
                <input aria-label="Lump sum amount" className="input" type="number" style={{ width: 110 }} step={1000} value={l.amount} onChange={(e) => lumpSet(i, { amount: Number(e.target.value) })} />
                <span style={{ fontSize: 11, color: "var(--muted)" }}>age · € (negative = out, today's euros)</span>
                <button className="btn" aria-label="Remove lump sum" onClick={() => setC({ lumps: ct.lumps.filter((_, j) => j !== i) })}>×</button>
              </div>))}
            <button className="btn" onClick={() => setC({ lumps: [...ct.lumps, { age: f.start_age + 5, amount: 10000 }] })}>Add lump sum</button>
          </div>
        </Group>
        <Group title="Withdrawals">
          <Num label="Start age" value={w.start_age} min={0} max={120} onChange={(v) => setW({ start_age: Math.round(v) })} />
          <div className="field"><label htmlFor="mc-strategy">Strategy</label>
            <select id="mc-strategy" className="select" value={w.strategy} onChange={(e) => setW({ strategy: e.target.value as Strategy, rate: null })}>
              {(Object.keys(STRATEGY_LABEL) as Strategy[]).map((s) => <option key={s} value={s}>{STRATEGY_LABEL[s]}</option>)}</select></div>
          {w.strategy === "fixed_real" && <Num label="Per month € (today's)" value={w.monthly} step={100} min={0} onChange={(v) => setW({ monthly: v })} />}
          {w.strategy in DEFAULT_RATE && <Num label="Initial rate p.a." pctVal step={0.1} min={0.1} max={50} value={w.rate ?? DEFAULT_RATE[w.strategy]!} onChange={(v) => setW({ rate: v })}
            hint={w.strategy === "percent_of_portfolio" ? "of the value each January" : w.strategy === "guardrails" ? "±20 % guardrails, 10 % steps" : "then indexed to inflation"} />}
          <Num label="Floor €/mo (today's)" value={w.floor} step={100} min={0} onChange={(v) => setW({ floor: v })} />
          <Num label="Pension €/mo" value={w.pension_monthly} step={50} min={0} onChange={(v) => setW({ pension_monthly: v })} />
          <Num label="Pension from age" value={w.pension_age} min={0} max={120} onChange={(v) => setW({ pension_age: Math.round(v) })} />
          <Num label="Tax on draws" pctVal step={1} value={w.tax} min={0} max={60} onChange={(v) => setW({ tax: v })} hint="see the tax-aware withdrawal order below" />
        </Group>
        {defs.data!.leverage && (
          <Group title="Leverage">
            <Num label="Loan ratio ×" value={lv.ratio} step={0.05} min={1} max={10} hint="1 = no borrowing" onChange={(v) => setL({ ratio: Math.min(10, Math.max(1, v)) })} />
            <Num label="Collateral rate" pctVal step={1} min={5} max={90} value={lv.collateral_rate ?? defs.data!.leverage.collateral_rate} hint="of the portfolio" onChange={(v) => setL({ collateral_rate: Math.min(0.9, Math.max(0.05, v)) })} />
            <div className="field" style={{ gridColumn: "1 / -1" }}><label htmlFor="mc-policy">Re-lever policy</label>
              <select id="mc-policy" className="select" value={lv.policy} onChange={(e) => setL({ policy: e.target.value as Policy })}>
                {(Object.keys(POLICY_LABEL) as Policy[]).map((k) => <option key={k} value={k}>{POLICY_LABEL[k]}</option>)}</select></div>
            <label style={{ gridColumn: "1 / -1", fontSize: 12, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center" }}>
              <input type="checkbox" checked={lv.sweep} onChange={(e) => setL({ sweep: e.target.checked })} />
              Compare {SWEEP.map((x) => `${x}×`).join(", ")}
            </label>
          </Group>
        )}
        <Group title="Hedge">
          <div className="field" style={{ gridColumn: "1 / -1" }}><label htmlFor="mc-hedge">Hedge</label>
            <select id="mc-hedge" className="select" value={hg.kind} onChange={(e) => setH({ kind: e.target.value as HedgeKind })}>
              {(Object.keys(HEDGE_LABEL) as HedgeKind[]).map((k) => <option key={k} value={k}>{HEDGE_LABEL[k]}</option>)}</select></div>
          {hg.kind !== "none" && <>
            <Num label="Ratio" pctVal step={5} min={0} max={200} value={hg.ratio} hint="of the portfolio" onChange={(v) => setH({ ratio: Math.min(2, Math.max(0, v)) })} />
            <Num label="Period (months)" step={1} min={1} max={24} value={hg.tenor} onChange={(v) => setH({ tenor: Math.min(24, Math.max(1, Math.round(v))) })} />
            {hg.kind !== "short" && <Num label="Strike" pctVal step={1} min={30} max={120} value={hg.strike} hint="of today's level" onChange={(v) => setH({ strike: Math.min(1.2, Math.max(0.3, v)) })} />}
          </>}
        </Group>
        <Group title="Target (optional)">
          <div className="field"><label htmlFor="mc-target-on">Goal</label>
            <label style={{ fontSize: 12 }}><input id="mc-target-on" type="checkbox" checked={!!f.target} onChange={(e) => set({ target: e.target.checked ? { age: w.start_age, value_real: 1_000_000 } : null })} /> reach a value</label></div>
          {f.target && <><Num label="At age" value={f.target.age} min={f.start_age} max={f.horizon_age} onChange={(v) => set({ target: { ...f.target!, age: Math.round(v) } })} />
            <Num label="Value € (today's)" value={f.target.value_real} step={50000} min={1} onChange={(v) => set({ target: { ...f.target!, value_real: v } })} /></>}
        </Group>
      </div>
      <div className="mc-results">
      {err && <div role="alert" style={{ color: "var(--loss)", margin: "10px 0", fontSize: 13 }}>{err}</div>}
      {res && p && (
        <div style={{ marginTop: 14, opacity: busy ? 0.6 : 1 }}>
          {res.history?.warning && <p className="err" role="note" style={{ margin: "0 0 10px", fontSize: 13 }}>{res.history.warning}</p>}
          <div className="kpis" style={{ border: "1px solid var(--line)", borderRadius: 8 }}>
            <Tile label="Success" value={pct(res.success)} sub={res.median_depletion_age ? `median runs out at ${res.median_depletion_age}` : `to age ${f.horizon_age}`} />
            <Tile label={`Median at ${res.withdrawal_start_age}`} value={compact(res.value_at_withdrawal_start.real_median)} sub="today's euros" />
            <Tile label="First-year withdrawal" value={money0(res.first_year.median)} sub={`p10 ${money0(res.first_year.p10)} · today's euros`} />
            <Tile label="Median ending value" value={compact(res.ending_real.p50)} sub={`p10 ${compact(res.ending_real.p10)} · p90 ${compact(res.ending_real.p90)}`} />
            <Tile label={`Safe rate (${pct(res.success_target, 0)} success)`} value={res.safe_withdrawal_rate == null ? "—" : pct(res.safe_withdrawal_rate, 2)} sub="of value at start, indexed" />
            <Tile label="Perpetual rate" value={res.perpetual_withdrawal_rate == null ? "—" : pct(res.perpetual_withdrawal_rate, 2)} sub="keeps median real value" />
            {res.target && <Tile label={`Reach ${compact(res.target.value_real)} at ${res.target.age}`} value={pct(res.target.probability)} sub="probability" />}
          </div>
          {res.hedge && (
            <div className="kpis" style={{ border: "1px solid var(--line)", borderRadius: 8, marginTop: 10 }}>
              <Tile label={`Hedge cost a year (${HEDGE_LABEL[res.hedge.kind as HedgeKind] ?? res.hedge.kind})`} value={pct(res.hedge.cost_per_year, 2)} sub="of the portfolio" />
              <Tile label="Average payoff a year" value={pct(res.hedge.payoff_per_year, 2)} sub="of the portfolio, after tax" />
            </div>
          )}
          {res.hedge && (
            <p style={{ fontSize: 12, color: "var(--muted)", marginTop: 6 }}>Rolled on the portfolio itself (no basis risk); premiums at Black–Scholes with the mix's volatility plus skew; the hedge's result is taxed (losses credited).</p>
          )}
          {res.leverage && (
            <div className="kpis" style={{ border: "1px solid var(--line)", borderRadius: 8, marginTop: 10 }}>
              <Tile label={`Margin call (${res.leverage.ratio}×)`} value={pct(res.leverage.margin_call, 1)} sub={res.leverage.first_call_median_year == null ? "chance during the run" : `median first call in year ${res.leverage.first_call_median_year}`} />
              <Tile label="Wiped out" value={pct(res.leverage.ruin, 1)} sub="chance of ruin" />
              <Tile label="Interest a year" value={pct(res.leverage.interest_per_year, 2)} sub="of own money, after tax" />
              <Tile label="Tax on forced sales" value={pct(res.leverage.forced_tax_per_year, 2)} sub="of own money, a year" />
            </div>
          )}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", margin: "12px 0 6px", fontSize: 12, color: "var(--fg-2)" }}>
            <Legend items={[{ name: "Median", color: c1 }]} />
            <span style={{ display: "inline-flex", gap: 10, alignItems: "center" }}>bands: p25–p75 and p10–p90
              <span className="seg" role="group" aria-label="Real or nominal">
                <button aria-pressed={view === "real"} onClick={() => setView("real")}>Real</button>
                <button aria-pressed={view === "nominal"} onClick={() => setView("nominal")}>Nominal</button></span></span>
          </div>
          <LineChart label={`Portfolio value by age, ${view === "real" ? "today's euros" : "nominal euros"}`} x={res.ages} xFormat={(v) => `age ${v}`} height={280}
            bands={[{ lo: p.p10, hi: p.p90, color: c1, opacity: 0.12, label: "p10–p90" }, { lo: p.p25, hi: p.p75, color: c1, opacity: 0.2, label: "p25–p75" }]}
            series={[{ name: "Median", values: p.p50, color: c1 }]}
            marker={{ index: Math.max(0, res.withdrawal_start_age - f.start_age), label: `withdraw from ${res.withdrawal_start_age}` }} />
          <div className="tbl-wrap" style={{ marginTop: 10 }}>
            <table><thead><tr><th title="Every strategy runs on the same simulated paths">Strategy</th><th className="r">Success</th><th className="r" title="Median of the yearly withdrawals, today's euros">€/year</th><th className="r">1st year</th><th className="r" title="10th percentile of each path's worst year">Worst yr</th><th className="r" title="Median value left at the horizon, today's euros">Ending</th></tr></thead>
              <tbody>{res.compare.map((r) => (
                <tr key={r.strategy} aria-selected={r.selected} style={{ cursor: "default", fontWeight: r.selected ? 600 : 400 }}>
                  <td>{STRATEGY_LABEL[r.strategy]}{r.rate ? ` · ${pct(r.rate, 1)}` : ""}</td><td className="r num">{pct(r.success)}</td>
                  <td className="r num">{money0(r.median_yearly_withdrawal)}</td><td className="r num">{money0(r.first_year_median)}</td>
                  <td className="r num">{money0(r.p10_worst_year)}</td><td className="r num">{money0(r.median_ending_real)}</td></tr>))}</tbody></table>
          </div>
          {res.leverage_sweep && res.leverage_sweep.length > 0 && (
            <div className="tbl-wrap" style={{ marginTop: 10 }}>
              <table><caption style={{ textAlign: "left", fontSize: 12, color: "var(--fg-2)", paddingBottom: 4 }}>Leverage compared (same paths)</caption>
                <thead><tr><th>Ratio</th><th className="r">Success</th><th className="r" title="Median / P10 at withdrawal start, today's euros, after the tax on repaying the loan">At start, median / P10</th><th className="r hide-sm" title="Median before the tax that repaying (and margin calls) realised; an unlevered portfolio pays that tax later, as it withdraws">Median before repay tax</th><th className="r hide-sm" title="Median / P10 at the horizon, today's euros">Ending, median / P10</th><th className="r hide-sm">Safe rate</th><th className="r">Margin call</th><th className="r hide-sm">Ruin</th><th className="r hide-sm" title="Of own money, after tax">Interest/yr</th></tr></thead>
                <tbody>{res.leverage_sweep.map((r) => (
                  <tr key={r.ratio} style={{ cursor: "default" }}>
                    <td className="num">×{r.ratio}</td><td className="r num">{pct(r.success)}</td>
                    <td className="r num">{compact(r.at_withdrawal_real_p50)} / {compact(r.at_withdrawal_real_p10)}</td>
                    <td className="r num hide-sm">{r.at_withdrawal_real_p50_before_tax == null ? "—" : compact(r.at_withdrawal_real_p50_before_tax)}</td>
                    <td className="r num hide-sm">{compact(r.ending_real_p50)} / {compact(r.ending_real_p10)}</td>
                    <td className="r num hide-sm">{r.safe_withdrawal_rate == null ? "—" : pct(r.safe_withdrawal_rate, 2)}</td>
                    <td className="r num" style={r.margin_call > 0.05 ? { color: "var(--warn)" } : undefined}>{pct(r.margin_call, 1)}</td>
                    <td className="r num hide-sm">{pct(r.ruin, 1)}</td><td className="r num hide-sm">{pct(r.interest_per_year, 2)}</td></tr>))}</tbody></table>
            </div>
          )}
          {(res.leverage || res.leverage_sweep) && (
            <p style={{ fontSize: 12, color: "var(--muted)", marginTop: 10 }}>
              Contributions and withdrawals keep the leverage of the moment. Interest is counted after the {pct(defs.data!.leverage?.deductible ?? 0.3, 0)} deduction.
              A margin call sells holdings and realises taxable gains{defs.data!.leverage ? ` (gain share from your lots: ${pct(defs.data!.leverage.gain_share, 0)})` : ""}.
            </p>
          )}
          <p style={{ fontSize: 12, color: "var(--muted)", marginTop: 10 }}>
            Returns are simulated, not predicted: the outcomes show how wide the range could be, not what will happen. Euros are in today's money unless you switch to nominal.
            {res.history
              ? ` Historical mode reuses only ${res.history.months} months of your own portfolio (${fmtDate(res.history.from)} – ${fmtDate(res.history.to)}, ${pct(res.history.cagr, 1)} a year), net of the funds' own costs, so it may miss crashes that history has not shown yet.`
              : ` Expected real return of this mix: ${pct(res.inputs.expected_real_return, 1)} a year. Variable (VPW) is not counted as failing if it spends down in its last year.`}
            {" "}Withdrawals are net spending; the pension covers part of them and the tax drag is added on top of what is drawn.
          </p>
        </div>
      )}
      </div>
      </div>
    </Panel>
  );
}
