// Allocation advisor: the class mix with the most expected return inside your loss limits, projected with your
// monthly saving, and a glide path that takes risk off towards the date the money is needed. Saving it sets the
// allocation targets that rebalancing, the new-money plan and (optionally) the Monte Carlo follow.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import type { Ctx } from "../lib/context";
import { date, money, pct } from "../lib/format";
import { Legend, LineChart, useCssVar } from "./LineChart";
import { useToast } from "./Toasts";
import { Chip, Empty, Panel, Skeleton } from "./ui";

const CLASSES = ["equity", "bond", "cash", "other"] as const;
type Cls = (typeof CLASSES)[number];
const NAME: Record<Cls, string> = { equity: "Shares", bond: "Bonds", cash: "Cash", other: "Other" };
const LABEL: Record<Cls, string> = { equity: "Equity", bond: "Fixed income", cash: "Cash", other: "Other" };

type Metrics = { return: number; volatility: number; median_return: number; var95: number; cvar95: number; p_loss: number };
type Point = Metrics & { mix: Record<Cls, number>; ok: boolean; leverage?: number; p_margin_call?: number };
type Proj = { years: number; invested: number; p10: number; p50: number; p90: number; p_below_invested: number; drawdown_p50: number; drawdown_p10: number; leverage?: number; margin_call?: number | null };
type GlidePt = { date: string; years_left: number; mix: Record<Cls, number>; return: number; volatility: number };
type Result = {
  points: Point[]; recommended: number; found: boolean; current: Point | null; glide: GlidePt[];
  recommended_targets: Record<string, number>; glide_targets: { date: string; mix: Record<string, number> }[];
  projection: { recommended: Proj; glide: Proj; current: Proj | null }; notes: string[];
  inputs: { start_value: number; monthly: number; mean: Record<Cls, number>; vol: Record<Cls, number> };
};
type Saved = { horizon_years: number; max_loss: number; max_shortfall: number; monthly: number | null };
type Borrow = { leverage: boolean; max_leverage: number; max_margin_call: number };
type Defaults = {
  saved: Saved | null; mean: Record<Cls, number>; vol: Record<Cls, number>;
  targets: { targets: Record<string, string>; band: string; glide?: { date: string; mix: Record<string, number> }[]; source?: string };
};

function useDebounced<T>(v: T, ms = 400): T {
  const [d, setD] = useState(v);
  useEffect(() => { const t = setTimeout(() => setD(v), ms); return () => clearTimeout(t); }, [v, ms]);
  return d;
}

const mixText = (m: Partial<Record<Cls, number>>) =>
  CLASSES.filter((c) => (m[c] ?? 0) >= 0.005).map((c) => `${NAME[c]} ${pct(m[c] ?? 0, 0)}`).join(" · ");

export function Advisor({ ctx }: { ctx: Ctx }) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const def = useQuery({ queryKey: ["advisor-defaults"], queryFn: () => api<Defaults>("/api/portfolio/advisor") });
  const [f, setF] = useState<Saved | null>(null);
  const [mean, setMean] = useState<Record<Cls, number> | null>(null);
  const [vol, setVol] = useState<Record<Cls, number> | null>(null);
  const [borrow, setBorrow] = useState<Borrow>({ leverage: false, max_leverage: 2, max_margin_call: 0.02 });
  const [pick, setPick] = useState<number | null>(null); // a different frontier point than the recommendation
  const [glide, setGlide] = useState<GlidePt[] | null>(null); // edited glide path
  useEffect(() => {
    if (!def.data || f) return;
    const s = def.data.saved;
    setF({ horizon_years: s?.horizon_years ?? 15, max_loss: s?.max_loss ?? 0.2, max_shortfall: s?.max_shortfall ?? 0.1, monthly: s?.monthly ?? null });
    setMean(def.data.mean);
    setVol(def.data.vol);
  }, [def.data, f]);
  const body = useMemo(() => f && mean && vol && { scope: ctx.scope, ...f, mean, vol, ...borrow, collateral_rate: null }, [ctx.scope, f, mean, vol, borrow]);
  const q = useDebounced(body);
  const res = useQuery({ enabled: !!q, queryKey: ["advisor", q], queryFn: () => api<Result>("/api/portfolio/advisor", { body: q }), retry: false });
  const r = res.data;
  useEffect(() => { setPick(null); setGlide(null); }, [r]);
  const colors = { equity: useCssVar("--c1"), bond: useCssVar("--c2"), cash: useCssVar("--c3"), other: useCssVar("--c4") };

  if (!f || !mean || !vol || !def.data) return <Panel title="Allocation advisor" style={{ marginTop: 12 }}><Skeleton w={240} /></Panel>;
  const set = (patch: Partial<Saved>) => setF({ ...f, ...patch });
  const chosen = r ? r.points[pick ?? r.recommended]! : null;
  const path = glide ?? r?.glide ?? [];
  const saved = def.data.targets;

  const save = async (withGlide: boolean) => {
    if (!r || !chosen) return;
    const toLabels = (m: Record<Cls, number>) => Object.fromEntries(CLASSES.filter((c) => m[c] > 0).map((c) => [LABEL[c], m[c]]));
    // a levered pick: the mix sums to its exposure (e.g. 1.5), the targets are that mix divided by its total
    const k = chosen.leverage ?? 1;
    const norm = (m: Record<string, number>) => { const t = Object.values(m).reduce((a, b) => a + b, 0) || 1; return Object.fromEntries(Object.entries(m).map(([c, v]) => [c, v / t])); };
    const g = withGlide ? path.map((p, i) => ({ date: p.date, mix: norm(toLabels(i === 0 && pick != null ? chosen.mix : p.mix)) })) : null;
    try {
      await api("/api/portfolio/targets", { method: "PUT", body: { targets: norm(toLabels(chosen.mix)), ...(k > 1 ? { leverage: k } : {}), band: saved.band ?? "0.05", glide: g, source: "advisor" } });
      toast(withGlide ? "Targets and glide path saved" : "Targets saved");
      for (const k of ["targets", "rebalance", "advisor-defaults", "new-money", "new-money-defaults"]) void qc.invalidateQueries({ queryKey: [k] });
    } catch (e) {
      toast(e instanceof Error ? e.message : "Could not save");
    }
  };
  const editGlide = (i: number, c: Cls, v: number) => {
    const next = path.map((p, j) => (j === i ? { ...p, mix: { ...p.mix, [c]: Math.max(0, v) / 100 } } : p));
    setGlide(next);
  };

  return (
    <Panel title="Allocation advisor" style={{ marginTop: 12 }} meta={saved.source === "advisor" ? <Chip tone="ok">targets from the advisor{saved.glide ? " · glide path on" : ""}</Chip> : <span>targets are set by hand</span>} bodyless>
      <div className="mc-layout" style={{ padding: 14 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
            <Num label="Years until the money is needed" value={f.horizon_years} step={1} width={190} onChange={(v) => set({ horizon_years: Math.min(60, Math.max(0.5, v)) })} />
            <Num label="Bad-year loss I accept %" value={Math.round(f.max_loss * 100)} step={5} width={160} onChange={(v) => set({ max_loss: Math.min(95, Math.max(1, v)) / 100 })} />
            <Num label="Chance of being below cost %" value={Math.round(f.max_shortfall * 100)} step={5} width={190} onChange={(v) => set({ max_shortfall: Math.min(49, Math.max(1, v)) / 100 })} />
            <Num label="Saving per month €" value={f.monthly ?? r?.inputs.monthly ?? 0} step={100} width={140} onChange={(v) => set({ monthly: Math.max(0, v) })} />
          </div>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
            <label style={{ fontSize: 12, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center" }}>
              <input type="checkbox" checked={borrow.leverage} onChange={(e) => { setBorrow({ ...borrow, leverage: e.target.checked }); setPick(null); }} />
              Allow borrowing (Nordnet credit)
            </label>
            {borrow.leverage && <>
              <Num label="Max leverage ×" value={borrow.max_leverage} step={0.25} width={120} onChange={(v) => setBorrow({ ...borrow, max_leverage: Math.min(10, Math.max(1, v)) })} />
              <Num label="Margin call within a year at most %" value={Math.round(borrow.max_margin_call * 1000) / 10} step={0.5} width={220} onChange={(v) => setBorrow({ ...borrow, max_margin_call: Math.min(100, Math.max(0, v)) / 100 })} />
            </>}
          </div>
          <small style={{ color: "var(--muted)" }}>
            Bad year: the 1-in-20 one-year loss. Below cost: the chance that money invested now is worth less than it cost when you need it. For retirement,
            use roughly the middle of the years you will draw on the money.
          </small>
          <details>
            <summary style={{ cursor: "pointer", fontSize: 12 }}>Assumptions (per year; cash is your savings rate when set)</summary>
            <table style={{ marginTop: 6 }}>
              <thead><tr><th>Class</th><th className="r">Return %</th><th className="r">Volatility %</th></tr></thead>
              <tbody>{CLASSES.map((c) => (
                <tr key={c} style={{ cursor: "default" }}>
                  <td>{NAME[c]}</td>
                  <td className="r"><input aria-label={`${c} return`} className="input" type="number" step={0.25} style={{ width: 70 }} value={Math.round(mean[c] * 1e4) / 100} onChange={(e) => setMean({ ...mean, [c]: Number(e.target.value) / 100 })} /></td>
                  <td className="r"><input aria-label={`${c} volatility`} className="input" type="number" step={0.5} style={{ width: 70 }} value={Math.round(vol[c] * 1e4) / 100} onChange={(e) => setVol({ ...vol, [c]: Number(e.target.value) / 100 })} /></td>
                </tr>))}</tbody>
            </table>
            <button className="btn" style={{ marginTop: 6 }} onClick={() => { setMean(def.data!.mean); setVol(def.data!.vol); }}>Reset</button>
          </details>

          {r && (
            <div>
              <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>Efficient mixes (click one to use it instead)</div>
              <div className="tbl-wrap"><table>
                <thead><tr><th>Mix</th><th className="r">Return</th><th className="r">Bad year</th><th className="r">Below cost</th>{borrow.leverage && <th className="r" title="Chance of a margin call within a year">Margin call 1y</th>}</tr></thead>
                <tbody>{r.points.filter((_, i) => i % 4 === 0 || i === r.recommended || i === pick || i === r.points.length - 1).map((p) => {
                  const i = r.points.indexOf(p);
                  const on = i === (pick ?? r.recommended);
                  return (
                    <tr key={i} aria-selected={on} onClick={() => setPick(i === r.recommended ? null : i)} style={{ opacity: p.ok ? 1 : 0.55 }}>
                      <td style={{ fontSize: 12 }}>{mixText(p.mix)}{(p.leverage ?? 1) > 1 && <> <Chip tone="info">×{p.leverage}</Chip></>}{i === r.recommended && <> <Chip tone="ok">advised</Chip></>}</td>
                      <td className="r num" title={`volatility ${pct(p.volatility)}`}>{pct(p.return)}</td>
                      <td className="r num" style={{ color: p.var95 < -f.max_loss ? "var(--warn)" : undefined }}>{pct(p.var95)}</td>
                      <td className="r num" style={{ color: p.p_loss > f.max_shortfall ? "var(--warn)" : undefined }}>{pct(p.p_loss)}</td>
                      {borrow.leverage && <td className="r num" style={{ color: (p.p_margin_call ?? 0) > borrow.max_margin_call ? "var(--warn)" : undefined }}>{(p.leverage ?? 1) > 1 ? pct(p.p_margin_call ?? 0) : "—"}</td>}
                    </tr>
                  );
                })}</tbody>
              </table></div>
              <small style={{ color: "var(--muted)" }}>Faded rows break one of your limits.</small>
            </div>
          )}
        </div>

        <div className="mc-results" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {res.isLoading && <Skeleton w={240} />}
          {res.isError && <Empty title="No advice">{(res.error as Error).message}</Empty>}
          {r && chosen && (
            <>
              <div className="tiles" style={{ borderRadius: 8, border: "1px solid var(--line)" }}>
                <div className="tile"><span className="label">{pick == null ? "Advised mix" : "Your pick"}</span><span className="v" style={{ fontSize: 14 }}>{mixText(chosen.mix)}</span><span className="d" style={{ color: "var(--muted)" }}>today: {r.current ? mixText(r.current.mix) : "—"}</span></div>
                <div className="tile"><span className="label">Expected return</span><span className="v">{pct(chosen.return)}</span><span className="d" style={{ color: "var(--muted)" }}>volatility {pct(chosen.volatility)}</span></div>
                <div className="tile"><span className="label">1-in-20 bad year</span><span className="v">{pct(chosen.var95)}</span><span className="d" style={{ color: "var(--muted)" }}>average of those {pct(chosen.cvar95)}</span></div>
                <div className="tile"><span className="label">Below cost at the date</span><span className="v">{pct(chosen.p_loss)}</span><span className="d" style={{ color: "var(--muted)" }}>money invested now</span></div>
              </div>

              <div>
                <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>In {f.horizon_years} years with {money(r.inputs.monthly)}/month ({money(r.projection.recommended.invested)} put in)</div>
                <div className="tbl-wrap"><table>
                  <thead><tr><th /><th className="r">Median</th><th className="r">Bad (P10)</th><th className="r">Below what was put in</th><th className="r hide-sm">Deepest fall, median / bad</th>{borrow.leverage && <th className="r hide-sm" title="Chance of a margin call during the period">Margin call</th>}</tr></thead>
                  <tbody>
                    {([["Today's mix", r.projection.current], ["Advised mix", r.projection.recommended], ["Glide path", r.projection.glide]] as const).map(([n, p]) => p && (
                      <tr key={n} style={{ cursor: "default" }}>
                        <td>{n}</td><td className="r num">{money(p.p50)}</td><td className="r num">{money(p.p10)}</td>
                        <td className="r num">{pct(p.p_below_invested)}</td><td className="r num hide-sm">{pct(p.drawdown_p50, 0)} / {pct(p.drawdown_p10, 0)}</td>
                        {borrow.leverage && <td className="r num hide-sm">{p.margin_call == null ? "—" : pct(p.margin_call)}</td>}
                      </tr>
                    ))}
                  </tbody>
                </table></div>
              </div>

              <div>
                <div style={{ fontSize: 12, color: "var(--fg-2)", marginBottom: 4 }}>Glide path {glide && <Chip tone="info">edited</Chip>}</div>
                {path.length > 1 && (
                  <>
                    <LineChart label="Glide path: class weights by year" x={path.map((g) => g.date)} xFormat={(v) => String(v).slice(0, 4)} format={(v) => pct(v, 0)} height={160} yMax={1}
                      series={CLASSES.filter((c) => path.some((g) => g.mix[c] > 0.005)).map((c) => ({ name: NAME[c], values: path.map((g) => g.mix[c]), color: colors[c] }))} />
                    <Legend items={CLASSES.filter((c) => path.some((g) => g.mix[c] > 0.005)).map((c) => ({ name: NAME[c], color: colors[c] }))} />
                  </>
                )}
                <details>
                  <summary style={{ cursor: "pointer", fontSize: 12 }}>Edit the glide path ({path.length} points)</summary>
                  <div className="tbl-wrap"><table>
                    <thead><tr><th>From</th>{CLASSES.map((c) => <th key={c} className="r">{NAME[c]} %</th>)}</tr></thead>
                    <tbody>{path.map((g, i) => (
                      <tr key={g.date} style={{ cursor: "default" }}>
                        <td>{date(g.date)}</td>
                        {CLASSES.map((c) => <td key={c} className="r"><input aria-label={`${c} ${g.date}`} className="input mono" type="number" step={5} style={{ width: 64 }} value={Math.round(g.mix[c] * 100)} onChange={(e) => editGlide(i, c, Number(e.target.value))} /></td>)}
                      </tr>))}</tbody>
                  </table></div>
                  {glide && <button className="btn" style={{ marginTop: 6 }} onClick={() => setGlide(null)}>Back to the advised path</button>}
                  <small style={{ color: "var(--muted)", display: "block" }}>Between points the mix moves in a straight line; each row is scaled to 100 %.</small>
                </details>
              </div>

              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <button className="btn primary" onClick={() => void save(true)}>Use as targets with the glide path</button>
                <button className="btn" onClick={() => void save(false)}>Use as fixed targets</button>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                {r.notes.map((n) => <small key={n} style={{ color: "var(--muted)" }}>{n}</small>)}
              </div>
            </>
          )}
        </div>
      </div>
    </Panel>
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
