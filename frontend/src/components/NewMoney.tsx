// New-money plan: reach the target mix by buying only (nothing sold, no gains realised). Funds count by what they
// hold, so an 80/20 fund adds both shares and bonds. Shows this month's buys, the path to target and how markets
// and other fund choices change it.
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { api } from "../lib/api";
import type { Ctx } from "../lib/context";
import { money, pct } from "../lib/format";
import { Legend, LineChart, useCssVar } from "./LineChart";
import { Chip, Empty, Panel, Skeleton } from "./ui";

type Fund = {
  key: string; name: string; instrument_id: number | null; value: number; split: Record<string, number>;
  ter: number | null; held: boolean; buyable: boolean; account: string | null; type: string;
};
type Buy = { key: string; name: string; amount: number; fee: number; account: string | null };
type Scenario = { key: string; label: string; months_to_band: number | null; months_to_target: number | null; drift_12m: number; end_value: number; fees: number };
type Variant = { name: string; funds: string[]; months_to_band: number | null; months_to_target: number | null; drift_12m: number; best_gap: number; ter_new_money: number | null; fees: number; first_month: Buy[] };
type Plan = {
  total: number; classes: string[]; targets: Record<string, number>; band: number; now: Record<string, number>;
  drift_now: number; best_mix: Record<string, number>; best_gap: number; first_month: Buy[];
  after_first_month: Record<string, number>; totals_12m: Buy[]; spent: number;
  schedule: { month: string; money: number; weights: Record<string, number>; targets: Record<string, number>; max_drift: number; value: number; fees: number; waiting: number }[];
  months_to_band: number | null; months_to_target: number | null; needed_monthly: number | null;
  ter_new_money: number | null; scenarios: Scenario[]; variants: Variant[]; notes: string[];
  fees: number; fees_12m: number; fee_share: number; months_waited: number; first_month_waiting: number;
  loan: { now: number; end: number; interest_after_tax: number; rate: number } | null;
  sell_alternative: { sold: number; taxable_gain: number; tax_estimate: number; rate: number };
};
type Fees = Record<string, [number, number]>;
type Saved = {
  monthly: number; lump: number; months: number; growth: number; min_trade: number; prefer: string; funds: string[] | null;
  region_targets: Record<string, number> | null; fees: Fees | null; max_fee_share: number; follow_glide: boolean; repay_first?: boolean;
};
type Defaults = {
  targets: Record<string, number>; band: number; funds: Fund[]; equity_regions: Record<string, number>; saved: Saved | null;
  fees: Fees; glide: { date: string; mix: Record<string, number> }[] | null;
};

const HORIZONS = [12, 24, 36, 60, 120];
const COLORS = ["--c1", "--c2", "--c3", "--c4", "--c5"];
const r1 = (x: number) => Math.round(x * 1000) / 10;

function useDebounced<T>(v: T, ms = 400): T {
  const [d, setD] = useState(v);
  useEffect(() => { const t = setTimeout(() => setD(v), ms); return () => clearTimeout(t); }, [v, ms]);
  return d;
}

const months = (n: number | null, horizon: number) => (n == null ? `not within ${horizon} mo` : n === 0 ? "this month" : `${n + 1} mo`);
const splitText = (s: Record<string, number>) =>
  Object.entries(s).filter(([, x]) => x >= 0.005).sort((a, b) => b[1] - a[1]).map(([k, x]) => `${k === "Fixed income" ? "Bonds" : k} ${Math.round(x * 100)}`).join(" / ");

export function NewMoney({ ctx }: { ctx: Ctx }) {
  const def = useQuery({ queryKey: ["new-money-defaults", ctx.scope], queryFn: () => api<Defaults>(`/api/portfolio/new-money?scope=${encodeURIComponent(ctx.scope)}`) });
  const d = def.data;
  const [form, setForm] = useState<Saved | null>(null);
  const [targets, setTargets] = useState<Record<string, number> | null>(null); // percents
  const [regions, setRegions] = useState<Record<string, number> | null>(null); // percents of equity, null = off
  const [variants, setVariants] = useState<{ name: string; funds: string[] }[]>([]);

  useEffect(() => {
    if (!d || form) return;
    const s = d.saved;
    setForm({
      monthly: s?.monthly ?? 500, lump: s?.lump ?? 0, months: s?.months ?? 36, growth: s?.growth ?? 0,
      min_trade: s?.min_trade ?? 20, prefer: s?.prefer ?? "held",
      funds: s?.funds?.filter((k) => d.funds.some((f) => f.key === k)) ?? null,
      fees: s?.fees ?? d.fees, max_fee_share: s?.max_fee_share ?? 0.01, follow_glide: s?.follow_glide ?? true, repay_first: s?.repay_first ?? false,
    } as Saved);
    setTargets(Object.fromEntries(Object.entries(d.targets).map(([k, v]) => [k, r1(v)])));
    if (s?.region_targets) setRegions(Object.fromEntries(Object.entries(s.region_targets).map(([k, v]) => [k, r1(v)])));
  }, [d, form]);

  const gliding = !!d?.glide?.length && !!form?.follow_glide;
  const chosen = useMemo(() => new Set(form?.funds ?? d?.funds.filter((f) => f.buyable).map((f) => f.key) ?? []), [form, d]);
  const body = useMemo(() => form && targets && {
    scope: ctx.scope, monthly: form.monthly, lump: form.lump, months: form.months, growth: form.growth / 100,
    min_trade: form.min_trade, prefer: form.prefer, funds: [...chosen],
    fees: form.fees, max_fee_share: form.max_fee_share, follow_glide: form.follow_glide, repay_first: !!form.repay_first,
    targets: gliding ? null : Object.fromEntries(Object.entries(targets).map(([k, v]) => [k, v / 100])),
    region_targets: regions ? Object.fromEntries(Object.entries(regions).filter(([, v]) => v > 0).map(([k, v]) => [k, v / 100])) : null,
    variants,
  }, [ctx.scope, form, targets, regions, chosen, variants, gliding]);
  const q = useDebounced(body);
  const res = useQuery({ enabled: !!q, queryKey: ["new-money", q], queryFn: () => api<Plan>("/api/portfolio/new-money", { body: q }), retry: false });
  const p = res.data;

  if (def.isLoading || !form || !targets || !d) return <Panel title="New-money plan" style={{ marginTop: 12 }}><Skeleton w={240} /></Panel>;
  const set = (patch: Partial<Saved>) => setForm({ ...form, ...patch });
  const toggle = (k: string) => {
    const s = new Set(chosen);
    if (s.has(k)) s.delete(k); else s.add(k);
    set({ funds: [...s] });
  };
  // funds and cash; a single share is picked only when it was chosen before
  const choosable = d.funds.filter((f) => f.type === "etf" || f.type === "fund" || f.type === "cash" || chosen.has(f.key));
  const tSum = Object.values(targets).reduce((a, b) => a + b, 0);

  return (
    <Panel title="New-money plan" style={{ marginTop: 12 }} meta={<span>buy only · nothing is sold</span>} bodyless>
      <div className="mc-layout" style={{ padding: 14 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
            <Num label="Monthly €" value={form.monthly} step={50} onChange={(v) => set({ monthly: Math.max(0, v) })} />
            <Num label="Lump sum now €" value={form.lump} step={500} onChange={(v) => set({ lump: Math.max(0, v) })} />
            <Num label="Yearly raise %" value={form.growth} step={1} onChange={(v) => set({ growth: v })} />
            <Num label="Smallest buy €" value={form.min_trade} step={10} onChange={(v) => set({ min_trade: Math.max(0, v) })} />
            <label className="field">
              <span style={{ fontSize: 12, color: "var(--fg-2)" }}>Horizon</span>
              <select className="select" value={form.months} onChange={(e) => set({ months: Number(e.target.value) })}>
                {HORIZONS.map((h) => <option key={h} value={h}>{h / 12} {h === 12 ? "year" : "years"}</option>)}
              </select>
            </label>
          </div>

          {!!d.glide?.length && (
            <label style={{ fontSize: 12, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center" }}>
              <input type="checkbox" checked={form.follow_glide} onChange={(e) => set({ follow_glide: e.target.checked })} />
              Follow my glide path (targets move month by month, from the allocation advisor)
            </label>
          )}
          {p?.loan && (
            <label style={{ fontSize: 12, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center" }}>
              <input type="checkbox" checked={!!form.repay_first} onChange={(e) => set({ repay_first: e.target.checked })} />
              Pay the credit down before buying
            </label>
          )}
          {!gliding && <div>
            <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>Target mix % (look-through){Math.abs(tSum - 100) > 0.05 && <span style={{ color: "var(--warn)" }}> · adds up to {tSum.toFixed(1)} %, scaled to 100</span>}</div>
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
              {Object.entries(targets).map(([k, v]) => (
                <Num key={k} label={k} value={v} step={5} width={100} onChange={(x) => setTargets({ ...targets, [k]: Math.max(0, x) })} />
              ))}
            </div>
            <small style={{ color: "var(--muted)" }}>Starts from your saved targets (Rebalancing below); changes here are only for this plan.</small>
          </div>}

          <details>
            <summary style={{ cursor: "pointer", fontSize: 12 }}>Costs: {form.fees?.etf?.[0] ?? 0} € per ETF buy, {form.fees?.fund?.[0] ?? 0} € per fund buy, at most {pct(form.max_fee_share, 1)} of a buy</summary>
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 6 }}>
              <Num label="ETF buy fee €" value={form.fees?.etf?.[0] ?? 0} step={0.5} width={110} onChange={(v) => set({ fees: { ...(form.fees ?? {}), etf: [Math.max(0, v), form.fees?.etf?.[1] ?? 0] } })} />
              <Num label="Fund buy fee €" value={form.fees?.fund?.[0] ?? 0} step={0.5} width={110} onChange={(v) => set({ fees: { ...(form.fees ?? {}), fund: [Math.max(0, v), form.fees?.fund?.[1] ?? 0] } })} />
              <Num label="Max fee per buy %" value={Math.round(form.max_fee_share * 1000) / 10} step={0.25} width={130} onChange={(v) => set({ max_fee_share: Math.min(100, Math.max(0.05, v)) / 100 })} />
              <small style={{ color: "var(--muted)", flexBasis: "100%" }}>
                Nordnet: ETF monthly saving costs 2.50 € per buy; funds have no trading fee. An ETF is bought only when the fee is at most this share of the buy;
                smaller amounts go to a fee-free fund doing the same job, or wait in cash for a later month.
              </small>
            </div>
          </details>

          <details open={!!regions}>
            <summary style={{ cursor: "pointer", fontSize: 12 }} onClick={(e) => { e.preventDefault(); setRegions(regions ? null : Object.fromEntries(Object.entries(d.equity_regions).slice(0, 6).map(([k, v]) => [k, r1(v)]))); }}>
              {regions ? "Region targets for the equity part (on)" : "Also steer the equity regions…"}
            </summary>
            {regions && (
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 6 }}>
                {Object.entries(regions).map(([k, v]) => <Num key={k} label={`${k} %`} value={v} step={5} width={130} onChange={(x) => setRegions({ ...regions, [k]: Math.max(0, x) })} />)}
                <small style={{ color: "var(--muted)", flexBasis: "100%" }}>Prefilled with today's split; regions left out are not steered. Asset classes take priority.</small>
              </div>
            )}
          </details>

          <div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span style={{ fontSize: 12, color: "var(--fg-2)" }}>Funds to buy</span>
              <div className="seg" role="group" aria-label="Split between similar funds">
                <button aria-pressed={form.prefer === "held"} onClick={() => set({ prefer: "held" })} title="Funds doing the same job share money in proportion to what you own">Like now</button>
                <button aria-pressed={form.prefer === "equal"} onClick={() => set({ prefer: "equal" })}>Equally</button>
              </div>
            </div>
            <div className="tbl-wrap" style={{ marginTop: 4 }}>
              <table>
                <tbody>
                  {choosable.map((f) => (
                    <tr key={f.key} onClick={() => toggle(f.key)}>
                      <td style={{ width: 28 }}><input type="checkbox" aria-label={`Buy ${f.name}`} checked={chosen.has(f.key)} onChange={() => toggle(f.key)} onClick={(e) => e.stopPropagation()} /></td>
                      <td>
                        <div className="inst">
                          {f.name}{!f.held && <> <Chip tone="info">candidate</Chip></>}
                          <small>{splitText(f.split)}{f.ter != null && ` · TER ${pct(f.ter, 2)}`}{f.value ? ` · ${money(f.value)}` : ""}{(form.fees?.[f.type]?.[0] ?? 0) > 0 && ` · ${form.fees![f.type]![0]} €/buy`}</small>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ display: "flex", gap: 8, marginTop: 6, flexWrap: "wrap", alignItems: "center" }}>
              <button className="btn" disabled={variants.length >= 3 || !chosen.size} onClick={() => setVariants([...variants, { name: `Set ${String.fromCharCode(65 + variants.length)}`, funds: [...chosen] }])}>Keep this selection for comparison</button>
              {variants.map((v, i) => <button key={v.name} className="btn" onClick={() => setVariants(variants.filter((_, j) => j !== i))} title="Remove">{v.name} ×</button>)}
              <small style={{ color: "var(--muted)" }}>Find more funds, such as 80/20 mixed ETFs, in the <Link to="/etfs">ETF screener</Link> and mark them as candidates.</small>
            </div>
          </div>
        </div>

        <div className="mc-results">
          {res.isLoading && <Skeleton w={240} />}
          {res.isError && <Empty title="No plan">{(res.error as Error).message}</Empty>}
          {p && <Result p={p} horizon={form.months} />}
        </div>
      </div>
    </Panel>
  );
}

function Result({ p, horizon }: { p: Plan; horizon: number }) {
  const colors = [useCssVar(COLORS[0]!), useCssVar(COLORS[1]!), useCssVar(COLORS[2]!), useCssVar(COLORS[3]!), useCssVar(COLORS[4]!)];
  const shown = p.classes.filter((c) => (p.targets[c] ?? 0) > 0 || p.schedule.some((s) => (s.weights[c] ?? 0) > 0.005));
  const sa = p.sell_alternative;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="tiles" style={{ borderRadius: 8, border: "1px solid var(--line)" }}>
        <div className="tile"><span className="label">Largest drift now</span><span className="v">{pct(p.drift_now)}</span><span className="d" style={{ color: "var(--muted)" }}>band ±{pct(p.band, 0)}</span></div>
        <div className="tile"><span className="label">Inside the band</span><span className="v">{months(p.months_to_band, horizon)}</span><span className="d" style={{ color: "var(--muted)" }}>on target (±1 %) {months(p.months_to_target, horizon)}</span></div>
        <div className="tile"><span className="label">For the band in 12 mo</span><span className="v">{p.needed_monthly == null ? "—" : `${money(p.needed_monthly)}/mo`}</span><span className="d" style={{ color: "var(--muted)" }}>{p.needed_monthly == null ? "not reachable with these funds" : "at expected returns"}</span></div>
        <div className="tile"><span className="label">Selling instead</span><span className="v">{sa.sold > 0 ? `~${money(sa.tax_estimate)} tax` : "no sales needed"}</span><span className="d" style={{ color: "var(--muted)" }}>{sa.sold > 0 ? `${money(sa.sold)} sold, ${money(sa.taxable_gain)} gain` : "already inside the band"}</span></div>
      </div>

      {p.loan && (
        <small style={{ color: "var(--fg-2)" }}>
          Credit: now {money(p.loan.now)} → end {money(p.loan.end)}, interest after tax {money(p.loan.interest_after_tax)} ({pct(p.loan.rate, 2)})
        </small>
      )}

      <div>
        <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>This month: buy</div>
        {p.first_month.length ? (
          <div className="tbl-wrap"><table>
            <thead><tr><th>Fund</th><th className="hide-sm">Account</th><th className="r">Amount</th></tr></thead>
            <tbody>{p.first_month.map((b) => <tr key={b.key} style={{ cursor: "default" }}><td>{b.name}</td><td className="hide-sm" style={{ color: "var(--fg-2)" }}>{b.account ?? "—"}</td><td className="r num">{money(b.amount)}</td></tr>)}</tbody>
          </table></div>
        ) : <small style={{ color: "var(--muted)" }}>Nothing to buy: enter a monthly amount or a lump sum.</small>}
        <small style={{ color: "var(--muted)" }}>
          After it: {p.classes.filter((c) => (p.after_first_month[c] ?? 0) > 0.0005).map((c) => `${c} ${pct(p.after_first_month[c] ?? 0)}`).join(" · ")}
          {p.ter_new_money != null && ` · new money's TER ${pct(p.ter_new_money, 2)}`}
        </small>
      </div>

      <div>
        <LineChart
          label="Asset class weights month by month, with targets dashed"
          x={p.schedule.map((s) => s.month)}
          xFormat={(v) => String(v)}
          format={(v) => pct(v, 0)}
          height={200}
          series={shown.flatMap((c, i) => [
            { name: c, values: p.schedule.map((s) => s.weights[c] ?? 0), color: colors[i % 5]! },
            { name: `${c} target`, values: p.schedule.map(() => p.targets[c] ?? 0), color: colors[i % 5]!, dashed: true },
          ])}
        />
        <Legend items={shown.map((c, i) => ({ name: `${c} (target ${pct(p.targets[c] ?? 0, 0)})`, color: colors[i % 5]! }))} />
      </div>

      <div>
        <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>If markets move (same plan)</div>
        <div className="tbl-wrap"><table>
          <thead><tr><th>Scenario</th><th className="r">Inside band</th><th className="r">Drift at 12 mo</th><th className="r hide-sm">Value at end</th></tr></thead>
          <tbody>{p.scenarios.map((s) => (
            <tr key={s.key} style={{ cursor: "default" }}>
              <td>{s.label}</td><td className="r num">{months(s.months_to_band, horizon)}</td>
              <td className="r num">{pct(s.drift_12m)}</td><td className="r num hide-sm">{money(s.end_value)}</td>
            </tr>
          ))}</tbody>
        </table></div>
      </div>

      {p.variants.length > 0 && (
        <div>
          <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>Fund sets compared</div>
          <div className="tbl-wrap"><table>
            <thead><tr><th>Set</th><th className="r">Inside band</th><th className="r">Closest mix gap</th><th className="r hide-sm">TER</th><th className="hide-sm">First buys</th></tr></thead>
            <tbody>{p.variants.map((v) => (
              <tr key={v.name} style={{ cursor: "default" }}>
                <td>{v.name} <small style={{ color: "var(--muted)" }}>({v.funds.length} funds)</small></td>
                <td className="r num">{months(v.months_to_band, horizon)}</td>
                <td className="r num" style={{ color: v.best_gap > p.band ? "var(--warn)" : undefined }}>{pct(v.best_gap)}</td>
                <td className="r num hide-sm">{v.ter_new_money == null ? "—" : pct(v.ter_new_money, 2)}</td>
                <td className="hide-sm" style={{ fontSize: 12 }}>{v.first_month.map((b) => `${b.name} ${money(b.amount)}`).join(", ")}</td>
              </tr>
            ))}</tbody>
          </table></div>
        </div>
      )}

      {p.totals_12m.length > 0 && (
        <small style={{ color: "var(--fg-2)" }}>First 12 months in total: {p.totals_12m.map((b) => `${b.name} ${money(b.amount)}`).join(" · ")}</small>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {p.notes.map((n) => <small key={n} style={{ color: "var(--muted)" }}>{n}</small>)}
        {sa.sold > 0 && <small style={{ color: "var(--muted)" }}>The selling estimate takes gains at {pct(sa.rate, 0)} before any deemed acquisition cost (hankintameno-olettama), which may lower it.</small>}
      </div>
    </div>
  );
}

function Num({ label, value, onChange, step, width = 120 }: { label: string; value: number; onChange: (v: number) => void; step: number; width?: number }) {
  return (
    <label className="field" style={{ width }}>
      <span style={{ fontSize: 12, color: "var(--fg-2)" }}>{label}</span>
      <input className="input mono" type="number" value={value} step={step} onChange={(e) => e.target.value !== "" && onChange(Number(e.target.value))} />
    </label>
  );
}
