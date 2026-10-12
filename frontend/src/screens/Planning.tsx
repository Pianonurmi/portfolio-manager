// Planning studio (PLAN-01..08, PEN-01..03, UX-04): live what-if on the server engine, scenarios, compare.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { CashLadder } from "../components/CashLadder";
import { Legend, LineChart, useCssVar } from "../components/LineChart";
import { MonteCarlo } from "../components/MonteCarlo";
import { ExplainPlan, TaxStrategy } from "../components/TaxStrategy";
import { useToast } from "../components/Toasts";
import { Field, Modal, Panel, ScreenHead, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { compact, money, pct } from "../lib/format";

type Params = {
  age_now: number; start_wealth: number; monthly_savings: number; retire_age: number; real_return: number; volatility: number;
  monthly_spending: number; pension_monthly: number; pension_age: number; withdrawal_tax: number; horizon_age: number;
  events: { age?: number; amount?: number; label?: string; from_age?: number; to_age?: number; savings_delta?: number }[];
  inheritances: { age: number; amount_after_tax: number; probability: number }[];
};
type Result = { ages: number[]; p10: number[]; p25: number[]; p50: number[]; p75: number[]; p90: number[]; success: number; deterministic: number[]; earliest_retirement: number | null; ms: number; seed: number; paths: number };
type Base = { params: Params; pension: Record<string, number | null>; sources: Record<string, string> };

const CONTROLS: [keyof Params, string, number, number, number, (v: number) => string][] = [
  ["monthly_savings", "Monthly savings €", 0, 6000, 50, (v) => money(v)],
  ["retire_age", "Retirement age", 40, 70, 1, (v) => String(v)],
  ["real_return", "Real return p.a.", 0, 0.08, 0.001, (v) => pct(v)],
  ["volatility", "Volatility", 0.03, 0.25, 0.005, (v) => pct(v)],
  ["monthly_spending", "Spending in retirement €/mo", 1000, 8000, 50, (v) => money(v)],
  ["pension_monthly", "Työeläke after tax €/mo", 0, 5000, 50, (v) => money(v)],
  ["withdrawal_tax", "Tax on withdrawals", 0, 0.3, 0.01, (v) => pct(v)],
];

export function Planning() {
  const base = useQuery({ queryKey: ["plan-base"], queryFn: () => api<Base>("/api/plan/base") });
  const scen = useQuery({ queryKey: ["scenarios"], queryFn: () => api<{ id: number; name: string; params: Params; is_base: boolean }[]>("/api/plan/scenarios") });
  const [p, setP] = useState<Params | null>(null);
  const [baseline, setBaseline] = useState<Params | null>(null);
  const [res, setRes] = useState<Result | null>(null);
  const [bres, setBres] = useState<Result | null>(null);
  const [modal, setModal] = useState<null | "events" | "pension">(null);
  const qc = useQueryClient();
  const { toast } = useToast();
  const c1 = useCssVar("--c1");
  const muted = useCssVar("--muted");
  const timer = useRef<number | undefined>(undefined);
  const seq = useRef(0);

  useEffect(() => {
    if (!base.data || p) return;
    const saved = scen.data?.find((s) => s.is_base)?.params;
    const start = { ...base.data.params, ...(saved ?? {}), age_now: base.data.params.age_now, start_wealth: base.data.params.start_wealth };
    setP(start);
    setBaseline(start);
  }, [base.data, scen.data, p]);
  useEffect(() => {
    if (!p) return;
    window.clearTimeout(timer.current);
    const n = ++seq.current;
    timer.current = window.setTimeout(async () => {
      const r = await api<Result>("/api/plan/simulate", { body: { params: p, paths: 1500 } });
      if (n === seq.current) setRes(r);
    }, 120);
  }, [p]);
  useEffect(() => {
    if (baseline) api<Result>("/api/plan/simulate", { body: { params: baseline, paths: 1500, earliest: false } }).then(setBres);
  }, [baseline]);

  if (base.isLoading || !p) return <Skeleton w={240} />;
  const set = (k: keyof Params, v: number) => setP({ ...p, [k]: v });
  const at = (r: Result | null, arr: keyof Result, age: number) => (r ? (r[arr] as number[])[r.ages.indexOf(age)] ?? 0 : 0);
  const yMax = res ? Math.max(...res.p75, ...(bres?.p50 ?? [0])) * 1.3 : undefined;
  const save = async (asBase: boolean) => {
    const name = asBase ? "Baseline" : `Scenario ${new Date().toLocaleDateString("fi-FI")} · retire ${p.retire_age}`;
    await api("/api/plan/scenarios", { body: { name, params: p, is_base: asBase } });
    if (asBase) setBaseline(p);
    qc.invalidateQueries({ queryKey: ["scenarios"] });
    toast(`Saved “${name}”`);
  };
  const rows: [string, (r: Result | null) => number, "pct" | "eur"][] = [
    ["Probability of success (to 95)", (r) => r?.success ?? 0, "pct"],
    [`Median at ${p.retire_age}`, (r) => at(r, "p50", p.retire_age), "eur"],
    ["Median at 65", (r) => at(r, "p50", 65), "eur"],
    ["Median at 90", (r) => at(r, "p50", 90), "eur"],
    ["Pessimistic (p10) at 90", (r) => at(r, "p10", 90), "eur"],
  ];
  return (
    <>
      <ScreenHead title="Planning studio" sub={`Real euros · starts from today's investments and cash (${money(p.start_wealth)}) · ${base.data?.sources.spending}`}
        actions={<><button className="btn" onClick={() => setModal("pension")}>Työeläke</button><button className="btn" onClick={() => setModal("events")}>Life events ({p.events.length + p.inheritances.length})</button>
          <button className="btn" onClick={() => baseline && setP(baseline)}>Reset</button><button className="btn" onClick={() => save(false)}>Save scenario</button><button className="btn primary" onClick={() => save(true)}>Set as baseline</button></>} />
      <div className="plan">
        <Panel title="Scenarios" bodyless>
          <div className="scn">
            {(scen.data ?? []).map((s) => (
              <button key={s.id} onClick={() => setP({ ...s.params, age_now: p.age_now, start_wealth: p.start_wealth })}>
                <span>{s.name}{s.is_base ? " · baseline" : ""}</span><small>retire {s.params.retire_age} · spend {compact(s.params.monthly_spending)}</small>
              </button>
            ))}
            {!scen.data?.length && <span style={{ padding: 10, color: "var(--muted)", fontSize: 12 }}>Save a scenario to compare later.</span>}
          </div>
        </Panel>
        <section className="panel">
          <div className="kpis">
            <div className="kpi"><span className="label">Success</span><span className="v mono">{res ? pct(res.success) : "…"}</span>
              <div className="meter"><span style={{ width: `${(res?.success ?? 0) * 100}%`, background: (res?.success ?? 0) >= 0.85 ? "var(--gain)" : (res?.success ?? 0) >= 0.7 ? "var(--warn)" : "var(--loss)" }} /></div></div>
            <div className="kpi"><span className="label">Median at 65</span><span className="v mono">{compact(at(res, "p50", 65))}</span></div>
            <div className="kpi"><span className="label">Earliest at 85 %</span><span className="v mono">{res?.earliest_retirement ? `age ${res.earliest_retirement}` : "—"}</span></div>
            <div className="kpi"><span className="label">Run</span><span className="v mono" style={{ fontSize: 13 }}>{res ? `${res.ms} ms · ${res.paths} paths · seed ${res.seed}` : ""}</span></div>
          </div>
          <div className="panel-b">
            <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 6 }}><Legend items={[{ name: "Median", color: c1 }, { name: "Baseline median", color: muted }]} /></div>
            {res && <LineChart label="Projected investable wealth by age" x={res.ages} xFormat={(a) => `age ${a}`} height={260} yMax={yMax}
              bands={[{ lo: res.p10, hi: res.p90, color: c1, opacity: 0.12, label: "p10–p90" }, { lo: res.p25, hi: res.p75, color: c1, opacity: 0.2, label: "p25–p75" }]}
              series={[{ name: "Median", values: res.p50, color: c1 }, ...(bres ? [{ name: "Baseline median", values: bres.p50, color: muted, dashed: true }] : [])]}
              marker={{ index: Math.max(0, p.retire_age - p.age_now), label: `retire ${p.retire_age}` }} />}
          </div>
          <div className="tbl-wrap" style={{ borderTop: "1px solid var(--line)" }}>
            <table><thead><tr><th>Compare</th><th className="r">Baseline</th><th className="r">This scenario</th><th className="r">Δ</th></tr></thead>
              <tbody>{rows.map(([n, f, k]) => {
                const a = f(bres), b = f(res), d = b - a;
                const fmt = (v: number) => (k === "pct" ? pct(v) : money(v));
                return (<tr key={n} style={{ cursor: "default" }}><td>{n}</td><td className="r num">{fmt(a)}</td><td className="r num">{fmt(b)}</td>
                  <td className="r num" style={{ color: Math.abs(d) < 1e-9 ? "var(--muted)" : d > 0 ? "var(--gain)" : "var(--loss)" }}>{Math.abs(d) < 1e-9 ? "—" : k === "pct" ? `${d > 0 ? "▲ +" : "▼ −"}${(Math.abs(d) * 100).toFixed(1).replace(".", ",")} pp` : `${d > 0 ? "▲ +" : "▼ −"}${money(Math.abs(d))}`}</td></tr>);
              })}</tbody></table>
          </div>
        </section>
        <Panel title="Inputs" meta="baseline under each" bodyless>
          {CONTROLS.map(([k, label, min, max, step, fmt]) => (
            <div className="ctl" key={k}>
              <div className="ctl-top"><label htmlFor={`in-${k}`}>{label}</label>
                <input type="number" id={`in-${k}`} min={min} max={max} step={step} value={p[k] as number} onChange={(e) => set(k, Number(e.target.value))} /></div>
              <input type="range" min={min} max={max} step={step} value={p[k] as number} onChange={(e) => set(k, Number(e.target.value))} aria-label={label} />
              <span className="base">baseline {baseline ? fmt(baseline[k] as number) : ""}</span>
            </div>
          ))}
        </Panel>
      </div>
      <MonteCarlo />
      <ExplainPlan params={p} result={res} />
      <TaxStrategy params={p} />
      <CashLadder />
      {modal === "events" && <EventsModal p={p} onChange={setP} onClose={() => setModal(null)} />}
      {modal === "pension" && <PensionModal initial={base.data!.pension} onClose={() => (setModal(null), qc.invalidateQueries({ queryKey: ["plan-base"] }))} onUse={(m) => set("pension_monthly", m)} />}
    </>
  );
}

function EventsModal({ p, onChange, onClose }: { p: Params; onChange: (p: Params) => void; onClose: () => void }) {
  const [lump, setLump] = useState({ age: p.age_now + 5, amount: -150000, label: "Bigger home" });
  const [inh, setInh] = useState({ age: p.age_now + 20, gross: 150000, probability: 0.8 });
  const addInheritance = async () => {
    const r = await api<{ net: string; tax: string }>("/api/plan/inheritance-after-tax", { body: { amount: String(inh.gross) } });
    onChange({ ...p, inheritances: [...p.inheritances, { age: inh.age, amount_after_tax: Number(r.net), probability: inh.probability }] });
  };
  return (
    <Modal title="Life events and inheritances" onClose={onClose}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {p.events.map((e, i) => (
          <div key={i} className="li" style={{ padding: "4px 0", cursor: "default" }}><div className="t">{e.label ?? "Event"}<small>age {e.age} · {money(e.amount ?? 0)}</small></div>
            <button className="x" onClick={() => onChange({ ...p, events: p.events.filter((_, j) => j !== i) })} aria-label="Remove">×</button></div>
        ))}
        {p.inheritances.map((e, i) => (
          <div key={`i${i}`} className="li" style={{ padding: "4px 0", cursor: "default" }}><div className="t">Inheritance<small>age {e.age} · {money(e.amount_after_tax)} after tax · {pct(e.probability, 0)} likely</small></div>
            <button className="x" onClick={() => onChange({ ...p, inheritances: p.inheritances.filter((_, j) => j !== i) })} aria-label="Remove">×</button></div>
        ))}
        <span className="label">Add a one-off (negative = spend, positive = receive)</span>
        <div className="form-grid">
          <Field label="What" id="ev-l"><input id="ev-l" className="input" value={lump.label} onChange={(e) => setLump({ ...lump, label: e.target.value })} /></Field>
          <Field label="At age" id="ev-a"><input id="ev-a" className="input mono" type="number" value={lump.age} onChange={(e) => setLump({ ...lump, age: Number(e.target.value) })} /></Field>
          <Field label="Amount €" id="ev-m"><input id="ev-m" className="input mono" type="number" value={lump.amount} onChange={(e) => setLump({ ...lump, amount: Number(e.target.value) })} /></Field>
        </div>
        <div className="form-actions"><button className="btn" onClick={() => onChange({ ...p, events: [...p.events, lump] })}>Add event</button></div>
        <span className="label">Add an inheritance (gross; inheritance tax class I is deducted)</span>
        <div className="form-grid">
          <Field label="At my age" id="in-a"><input id="in-a" className="input mono" type="number" value={inh.age} onChange={(e) => setInh({ ...inh, age: Number(e.target.value) })} /></Field>
          <Field label="Gross €" id="in-g"><input id="in-g" className="input mono" type="number" value={inh.gross} onChange={(e) => setInh({ ...inh, gross: Number(e.target.value) })} /></Field>
          <Field label="Probability 0–1" id="in-p"><input id="in-p" className="input mono" type="number" step="0.05" value={inh.probability} onChange={(e) => setInh({ ...inh, probability: Number(e.target.value) })} /></Field>
        </div>
        <div className="form-actions"><button className="btn primary" onClick={addInheritance}>Add inheritance</button></div>
      </div>
    </Modal>
  );
}

function PensionModal({ initial, onClose, onUse }: { initial: Record<string, number | null>; onClose: () => void; onUse: (m: number) => void }) {
  const [f, setF] = useState<Record<string, string>>(Object.fromEntries(Object.entries(initial).map(([k, v]) => [k, v == null ? "" : String(v)])));
  const curve = useQuery({ queryKey: ["pension-curve", f], queryFn: () => api<{ age: number; monthly_after_tax: number }[]>("/api/plan/pension/curve") });
  const save = async () => {
    await api("/api/plan/pension", { method: "PUT", body: Object.fromEntries(Object.entries(f).map(([k, v]) => [k, k === "statement_year" && !v ? null : Number(v.replace(",", "."))])) });
    curve.refetch();
  };
  const fld = (k: string, label: string, hint?: string) => (
    <Field label={label} id={`pe-${k}`} hint={hint}><input id={`pe-${k}`} className="input mono" value={f[k] ?? ""} onChange={(e) => setF({ ...f, [k]: e.target.value })} /></Field>
  );
  return (
    <Modal title="Työeläke" onClose={onClose}>
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <p style={{ margin: 0, fontSize: 12, color: "var(--muted)" }}>From your Työeläke.fi record (Työeläkeote). Figures are estimates; the retirement age for your cohort and the life-expectancy coefficient are on the same page.</p>
        <div className="form-grid">
          {fld("accrued_monthly", "Accrued pension €/month", "“Karttunut eläke” on the statement")}
          {fld("statement_year", "Statement year")}
          {fld("annual_earnings", "Annual earnings €")}
          {fld("accrual_rate", "Accrual rate", "1,5 % of earnings per year = 0.015")}
          {fld("target_age", "Target retirement age (cohort)")}
          {fld("life_coef", "Life-expectancy coefficient", "e.g. 0.94")}
          {fld("tax_rate", "Tax rate on pension", "e.g. 0.25")}
        </div>
        <div className="form-actions"><button className="btn primary" onClick={save}>Save and recalculate</button></div>
        {curve.data && (
          <div className="tbl-wrap"><table><thead><tr><th>Retire at</th><th className="r">€/month after tax</th><th /></tr></thead><tbody>
            {curve.data.map((r) => (<tr key={r.age} style={{ cursor: "default" }}><td className="num">{r.age}</td><td className="r num">{money(r.monthly_after_tax)}</td>
              <td className="r"><button className="btn" onClick={() => (onUse(r.monthly_after_tax), onClose())}>Use</button></td></tr>))}
          </tbody></table></div>
        )}
      </div>
    </Modal>
  );
}
