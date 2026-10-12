// Optimise (Phase E): mean-variance optimum and efficient frontier of the asset classes or the holdings, with the
// moves from today's mix. Nothing is traded; the numbers are model output from assumptions or shrunk history.
import { useQuery } from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import type { Ctx } from "../lib/context";
import { decimal, money, pct, signedMoney } from "../lib/format";
import { Legend, useCssVar } from "./LineChart";
import { Empty, Panel, Skeleton } from "./ui";

const CLASSES = ["equity", "bond", "cash", "other"] as const;
type Universe = "classes" | "holdings";
type Objective = "max_sharpe" | "min_variance" | "target_return" | "target_vol" | "risk_parity";
type Assumptions = { mean: Record<string, number>; vol: Record<string, number>; corr: number[][] };
type Point = { return: number; volatility: number; sharpe: number | null };
type Asset = {
  key: string; name: string; fixed: boolean; value: string; current_weight: number; suggested_weight: number;
  suggested_value: string; move: string; expected_return: number; volatility: number; risk_share: number;
};
type Result = {
  total: string; assets: Asset[]; current: Point; optimum: Point; turnover: number; notes: string[];
  frontier: (Point & { weights: number[] })[];
  window: { start: string; end: string; months: number } | null;
};
type Defaults = Assumptions & { max_weight: Record<Universe, number> };

const OBJECTIVES: { id: Objective; label: string }[] = [
  { id: "max_sharpe", label: "Best return per risk (max Sharpe)" },
  { id: "min_variance", label: "Lowest risk (min variance)" },
  { id: "target_return", label: "Lowest risk for a target return" },
  { id: "target_vol", label: "Highest return for a target risk" },
  { id: "risk_parity", label: "Equal risk from each asset" },
];
const PERIODS = ["3Y", "5Y", "10Y", "MAX"];
const sharpe = (v: number | null) => (v == null ? "—" : decimal(v, 2));

export function Optimize({ ctx }: { ctx: Ctx }) {
  const [universe, setUniverse] = useState<Universe>("classes");
  const [objective, setObjective] = useState<Objective>("max_sharpe");
  const [target, setTarget] = useState(6);
  const [period, setPeriod] = useState("5Y");
  const [rf, setRf] = useState<number | null>(null); // null: cash return for classes, 2 % for holdings (server side)
  const [maxW, setMaxW] = useState<number | null>(null);
  const [custom, setCustom] = useState<Assumptions | null>(null);
  const [cands, setCands] = useState(false);
  const def = useQuery({ queryKey: ["optimize-defaults"], queryFn: () => api<Defaults>("/api/portfolio/optimize/defaults"), staleTime: Infinity });
  const a = custom ?? def.data ?? null;
  const cap = maxW ?? (def.data ? def.data.max_weight[universe] * 100 : 100);
  const needsTarget = objective === "target_return" || objective === "target_vol";
  const body = {
    scope: ctx.scope, universe, objective, period, rf: rf == null ? null : rf / 100, max_weight: cap / 100,
    target: needsTarget ? target / 100 : null,
    assumptions: universe === "classes" ? a : null,
    candidates: universe === "holdings" && cands,
  };
  const q = useQuery({
    enabled: def.isSuccess,
    queryKey: ["optimize", body],
    queryFn: () => api<Result>("/api/portfolio/optimize", { body }),
    retry: false,
  });
  const d = q.data;
  return (
    <Panel
      title="Optimise"
      style={{ marginTop: 12 }}
      meta={
        <div className="seg" role="group" aria-label="Universe">
          <button aria-pressed={universe === "classes"} onClick={() => { setUniverse("classes"); setMaxW(null); }}>Asset classes</button>
          <button aria-pressed={universe === "holdings"} onClick={() => { setUniverse("holdings"); setMaxW(null); }}>Holdings</button>
        </div>
      }
      bodyless
    >
      <div style={{ padding: 14, display: "flex", gap: 14, flexWrap: "wrap", alignItems: "flex-end" }}>
        <label className="field">
          <span style={{ fontSize: 12, color: "var(--fg-2)" }}>Goal</span>
          <select className="select" value={objective} onChange={(e) => setObjective(e.target.value as Objective)}>
            {OBJECTIVES.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
          </select>
        </label>
        {needsTarget && <Num label={objective === "target_return" ? "Target return %" : "Target volatility %"} value={target} step={0.5} onChange={setTarget} />}
        <Num label="Risk-free rate %" value={rf ?? Math.round((universe === "classes" && a ? a.mean.cash ?? 0.025 : 0.02) * 1e4) / 100} step={0.25} onChange={setRf} />
        <Num label="Max weight per asset %" value={Math.round(cap)} step={5} min={1} max={100} onChange={setMaxW} />
        {universe === "holdings" && (
          <div className="seg" role="group" aria-label="History window">
            {PERIODS.map((p) => <button key={p} aria-pressed={period === p} onClick={() => setPeriod(p)}>{p === "MAX" ? "Max" : p}</button>)}
          </div>
        )}
        {universe === "holdings" && (
          <label style={{ fontSize: 12, color: "var(--fg-2)", display: "flex", gap: 6, alignItems: "center" }} title="Funds marked as candidates in the ETF screener join at 0 %">
            <input type="checkbox" checked={cands} onChange={(e) => setCands(e.target.checked)} /> Include screener candidates
          </label>
        )}
      </div>
      {universe === "classes" && a && (
        <details style={{ padding: "0 14px 10px" }}>
          <summary style={{ cursor: "pointer", fontSize: 12 }}>Edit assumptions (nominal, per year)</summary>
          <table style={{ marginTop: 6 }}>
            <thead><tr><th>Class</th><th className="r">Return %</th><th className="r">Volatility %</th>{CLASSES.map((k) => <th key={k} className="r">ρ {k}</th>)}</tr></thead>
            <tbody>
              {CLASSES.map((k, i) => (
                <tr key={k} style={{ cursor: "default" }}>
                  <td>{k}</td>
                  {(["mean", "vol"] as const).map((m) => (
                    <td key={m} className="r">
                      <input aria-label={`${k} ${m}`} className="input" type="number" step={0.5} style={{ width: 70 }} value={Math.round(a[m][k]! * 1e4) / 100}
                        onChange={(e) => setCustom({ ...a, [m]: { ...a[m], [k]: Number(e.target.value) / 100 } })} />
                    </td>
                  ))}
                  {CLASSES.map((c2, j) => (
                    <td key={c2} className="r">
                      {j === i ? "1" : (
                        <input aria-label={`correlation ${k} ${c2}`} className="input" type="number" step={0.1} min={-1} max={1} style={{ width: 56 }} value={a.corr[i]![j]}
                          onChange={(e) => { const v = Number(e.target.value); const corr = a.corr.map((r) => [...r]); corr[i]![j] = v; corr[j]![i] = v; setCustom({ ...a, corr }); }} />
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {custom && <button className="btn" style={{ marginTop: 6 }} onClick={() => setCustom(null)}>Reset to defaults</button>}
        </details>
      )}
      {q.isLoading && <p style={{ padding: 14 }}><Skeleton w={240} /></p>}
      {q.isError && <Empty title="No result">{(q.error as Error).message}</Empty>}
      {d && (
        <>
          <div className="tiles">
            <Tile label="Today" p={d.current} />
            <Tile label="Suggested mix" p={d.optimum} />
            <div className="tile">
              <span className="label">Turnover</span>
              <span className="v">{pct(d.turnover, 1)}</span>
              <span className="d" style={{ color: "var(--muted)" }}>of {money(d.total, "EUR", 0)} would change hands</span>
            </div>
          </div>
          <div style={{ padding: "6px 14px 0" }}>
            <Frontier d={d} />
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Asset</th>
                  <th className="r">Today</th>
                  <th className="r">Suggested</th>
                  <th className="r">Buy / sell</th>
                  <th className="r hide-sm">Exp. return</th>
                  <th className="r hide-sm">Volatility</th>
                  <th className="r hide-sm">Risk share</th>
                </tr>
              </thead>
              <tbody>
                {d.assets.map((x) => (
                  <tr key={x.key} style={{ cursor: "default" }}>
                    <td title={x.fixed ? "Kept at today's weight" : undefined}>{x.name}{x.fixed ? " (fixed)" : ""}</td>
                    <td className="r num">{pct(x.current_weight)}</td>
                    <td className="r num">{pct(x.suggested_weight)}</td>
                    <td className="r num">{Math.abs(Number(x.move)) < 1 ? "—" : signedMoney(x.move, "EUR", 0)}</td>
                    <td className="r num hide-sm">{pct(x.expected_return)}</td>
                    <td className="r num hide-sm">{pct(x.volatility)}</td>
                    <td className="r num hide-sm">{pct(x.risk_share, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ padding: "8px 14px 12px", display: "flex", flexDirection: "column", gap: 2 }}>
            {universe === "classes" && (
              <small style={{ color: "var(--muted)" }}>To act on a class-level change with taxes and new money in mind, set these as targets and use the rebalancing suggestion below.</small>
            )}
            {d.notes.map((n) => <small key={n} style={{ color: "var(--muted)" }}>{n}</small>)}
          </div>
        </>
      )}
    </Panel>
  );
}

function Num({ label, value, onChange, step, min, max }: { label: string; value: number; onChange: (v: number) => void; step: number; min?: number; max?: number }) {
  return (
    <label className="field" style={{ width: 130 }}>
      <span style={{ fontSize: 12, color: "var(--fg-2)" }}>{label}</span>
      <input className="input" type="number" value={value} step={step} min={min} max={max} onChange={(e) => e.target.value !== "" && onChange(Number(e.target.value))} />
    </label>
  );
}

function Tile({ label, p }: { label: string; p: Point }) {
  return (
    <div className="tile">
      <span className="label">{label}</span>
      <span className="v">{pct(p.return)}</span>
      <span className="d" style={{ color: "var(--muted)" }}>volatility {pct(p.volatility)} · Sharpe {sharpe(p.sharpe)}</span>
    </div>
  );
}

type Dot = { x: number; y: number; name: string; kind: "frontier" | "asset" | "current" | "optimum"; sub: string };

/** Risk (x) against return (y): the frontier as a line, assets as small dots, today and the suggestion as rings. */
function Frontier({ d }: { d: Result }) {
  const c1 = useCssVar("--c1");
  const c2 = useCssVar("--c2");
  const wrap = useRef<HTMLDivElement>(null);
  const [tip, setTip] = useState<Dot | null>(null);
  const W = 640, H = 260, m = { t: 12, r: 16, b: 34, l: 48 };
  const dots = useMemo<Dot[]>(() => [
    ...d.assets.map((a): Dot => ({ x: a.volatility, y: a.expected_return, name: a.name, kind: "asset", sub: `weight ${pct(a.current_weight)}` })),
    { x: d.current.volatility, y: d.current.return, name: "Today", kind: "current", sub: `Sharpe ${sharpe(d.current.sharpe)}` },
    { x: d.optimum.volatility, y: d.optimum.return, name: "Suggested", kind: "optimum", sub: `Sharpe ${sharpe(d.optimum.sharpe)}` },
  ], [d]);
  const all = [...dots, ...d.frontier.map((p) => ({ x: p.volatility, y: p.return }))];
  // round tick steps (1, 2, 2.5 or 5 × 10^k) and axes that end on a whole step
  const step = (span: number) => { const raw = span / 4 || 0.01; const p = 10 ** Math.floor(Math.log10(raw)); return [1, 2, 2.5, 5, 10].map((k) => k * p).find((s) => s >= raw)!; };
  const xs = step(Math.max(...all.map((p) => p.x)) * 1.05 || 0.1);
  const x1 = Math.ceil((Math.max(...all.map((p) => p.x)) * 1.05 || 0.1) / xs) * xs;
  const ys = all.map((p) => p.y);
  const yRaw0 = Math.min(0, ...ys), yRaw1 = Math.max(...ys) * 1.05 || 0.1;
  const yst = step(yRaw1 - yRaw0);
  const y0 = Math.floor(yRaw0 / yst) * yst, y1 = Math.ceil(yRaw1 / yst) * yst;
  const X = (v: number) => m.l + ((W - m.l - m.r) * v) / x1;
  const Y = (v: number) => m.t + (H - m.t - m.b) * (1 - (v - y0) / (y1 - y0 || 1));
  const ticks = (hi: number, lo = 0, st = hi / 4) => Array.from({ length: Math.round((hi - lo) / st) + 1 }, (_, i) => lo + i * st);
  return (
    <div className="chart" ref={wrap} role="img" aria-label="Efficient frontier: expected return against volatility">
      <div className="chart-legend">
        <Legend items={[{ name: "Efficient frontier", color: c1 }]} />
        <span><i style={{ width: 8, height: 8, borderRadius: 4, background: "var(--muted)" }} />Assets</span>
        <span><i style={{ width: 10, height: 10, borderRadius: 5, border: `2px solid ${c2}`, background: "transparent" }} />Today</span>
        <span><i style={{ width: 10, height: 10, borderRadius: 5, background: c1, border: "2px solid var(--surface)", boxShadow: `0 0 0 1px ${c1}` }} />Suggested</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} height={H} onPointerLeave={() => setTip(null)}>
        {ticks(y1, y0, yst).map((t) => (
          <g key={`y${t}`}>
            <line x1={m.l} x2={W - m.r} y1={Y(t)} y2={Y(t)} stroke="var(--line)" strokeWidth={1} />
            <text x={m.l - 6} y={Y(t) + 3.5} textAnchor="end">{pct(t, 0)}</text>
          </g>
        ))}
        {ticks(x1, 0, xs).map((t) => <text key={`x${t}`} x={X(t)} y={H - 16} textAnchor="middle">{pct(t, 0)}</text>)}
        <text x={(m.l + W - m.r) / 2} y={H - 3} textAnchor="middle">Volatility (per year)</text>
        <text transform={`translate(11 ${(m.t + H - m.b) / 2}) rotate(-90)`} textAnchor="middle">Expected return</text>
        <polyline points={d.frontier.map((p) => `${X(p.volatility)},${Y(p.return)}`).join(" ")} fill="none" stroke={c1} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        {dots.map((p, i) => {
          const r = p.kind === "asset" ? 4 : 6;
          return (
            <g key={i} onPointerEnter={() => setTip(p)} onPointerMove={() => setTip(p)}>
              <circle cx={X(p.x)} cy={Y(p.y)} r={12} fill="transparent" />
              {p.kind === "asset" && <circle cx={X(p.x)} cy={Y(p.y)} r={r} fill="var(--muted)" stroke="var(--surface)" strokeWidth={2} />}
              {p.kind === "current" && <circle cx={X(p.x)} cy={Y(p.y)} r={r} fill="var(--surface)" stroke={c2} strokeWidth={2} />}
              {p.kind === "optimum" && <circle cx={X(p.x)} cy={Y(p.y)} r={r} fill={c1} stroke="var(--surface)" strokeWidth={2} />}
            </g>
          );
        })}
      </svg>
      {tip && (
        <div className="tip" style={{ left: Math.min(Math.max(X(tip.x) * ((wrap.current?.clientWidth ?? W) / W) - 75, 0), (wrap.current?.clientWidth ?? W) - 190), top: 40 }}>
          <div className="h">{tip.name}</div>
          <div className="r"><span>Return</span><span className="num">{pct(tip.y)}</span></div>
          <div className="r"><span>Volatility</span><span className="num">{pct(tip.x)}</span></div>
          <div className="r"><span>{tip.sub}</span><span /></div>
        </div>
      )}
    </div>
  );
}
