// Factors (Phase E): Fama-French regression of the portfolio and its largest holdings. Loadings come with t-statistics;
// the wording is chosen by the backend from fixed thresholds.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import type { Ctx } from "../lib/context";
import { decimal, pct, signedPct } from "../lib/format";
import { useCssVar } from "./LineChart";
import { Empty, Panel, Skeleton } from "./ui";

const FACTORS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "WML"] as const;
const LABEL: Record<string, string> = { "Mkt-RF": "Market", SMB: "Size", HML: "Value", RMW: "Profitability", CMA: "Investment", WML: "Momentum" };
type Fit = {
  alpha: number | null; alpha_t: number | null; r2: number | null; months: number;
  loadings: Record<string, number | null> | null; t: Record<string, number | null> | null;
};
type Data = {
  status: "ok" | "insufficient"; explanation: string | null;
  data: { source: string; last_month: string } | null;
  window: { start: string; end: string; months: number } | null;
  portfolio: Fit | null;
  interpretation: { factor: string; text: string }[];
  holdings: (Fit & { instrument_id: number; name: string; weight: number })[];
  notes: string[];
};
const PERIODS = ["3Y", "5Y", "10Y", "Max"];
const sig = (t: number | null | undefined) => t != null && Math.abs(t) >= 2;
const num = (v: number | null | undefined, d = 2) => (v == null ? "—" : decimal(v, d));

export function Factors({ ctx }: { ctx: Ctx }) {
  const [period, setPeriod] = useState("Max");
  const q = useQuery({
    queryKey: ["factors", ctx.scope, period],
    queryFn: () => api<Data>(`/api/portfolio/factors?scope=${encodeURIComponent(ctx.scope)}&period=${period.toUpperCase()}`),
    retry: false,
  });
  const d = q.data;
  const p = d?.portfolio;
  return (
    <Panel
      title="Factors"
      style={{ marginTop: 12 }}
      meta={
        <div className="seg" role="group" aria-label="Factor window">
          {PERIODS.map((x) => <button key={x} aria-pressed={period === x} onClick={() => setPeriod(x)}>{x}</button>)}
        </div>
      }
      bodyless
    >
      {q.isLoading && <p style={{ padding: 14 }}><Skeleton w={240} /></p>}
      {q.isError && <Empty title="Factor data unavailable">{(q.error as Error).message}</Empty>}
      {d && d.status === "insufficient" && <Empty title="Not enough overlap">{d.explanation}</Empty>}
      {d && p && p.loadings && p.t && (
        <>
          <p style={{ padding: "10px 14px 0", margin: 0, fontSize: 12, color: "var(--fg-2)" }}>
            {d.window?.start} – {d.window?.end} · {p.months} months · R² {pct(p.r2 ?? 0, 0)} · alpha {p.alpha == null ? "—" : signedPct(p.alpha)} a year (t {num(p.alpha_t, 1)})
          </p>
          <div className="grid g-2" style={{ padding: 14 }}>
            <Bars loadings={p.loadings} t={p.t} />
            <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 4, fontSize: 13 }}>
              {d.interpretation.map((i) => <li key={i.factor}>{i.text}</li>)}
            </ul>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Holding</th>
                  <th className="r hide-sm">Weight</th>
                  {FACTORS.map((f) => <th key={f} className="r" title={LABEL[f]}>{f === "Mkt-RF" ? "Mkt" : f}</th>)}
                  <th className="r hide-sm">R²</th>
                </tr>
              </thead>
              <tbody>
                {d.holdings.map((h) => (
                  <tr key={h.instrument_id} style={{ cursor: "default" }}>
                    <td>{h.name}</td>
                    <td className="r num hide-sm">{pct(h.weight)}</td>
                    {FACTORS.map((f) => (
                      <td key={f} className="r num" title={h.t ? `t ${num(h.t[f], 1)}` : undefined} style={{ fontWeight: sig(h.t?.[f]) ? 600 : 400, color: sig(h.t?.[f]) ? "var(--fg)" : "var(--muted)" }}>
                        {num(h.loadings?.[f])}
                      </td>
                    ))}
                    <td className="r num hide-sm">{h.r2 == null ? "—" : pct(h.r2, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ padding: "8px 14px 12px", display: "flex", flexDirection: "column", gap: 2 }}>
            <small style={{ color: "var(--muted)" }}>Bold loadings are statistically clear (|t| ≥ 2); hover a figure for its t-statistic. Holdings with too little history show a dash.</small>
            {d.data && <small style={{ color: "var(--muted)" }}>Factor data runs to {d.data.last_month}.</small>}
            {d.notes.map((n) => <small key={n} style={{ color: "var(--muted)" }}>{n}</small>)}
          </div>
        </>
      )}
    </Panel>
  );
}

/** Horizontal bars from zero for each factor; a solid bar is statistically clear, a washed one is not. */
function Bars({ loadings, t }: { loadings: Record<string, number | null>; t: Record<string, number | null> }) {
  const c1 = useCssVar("--c1");
  const max = Math.max(0.5, ...FACTORS.map((f) => Math.abs(loadings[f] ?? 0))) * 1.1;
  return (
    <div role="img" aria-label="Factor loadings" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {FACTORS.map((f) => {
        const v = loadings[f] ?? 0;
        return (
          <div key={f} style={{ display: "grid", gridTemplateColumns: "92px minmax(0,1fr) 92px", gap: 8, alignItems: "center", fontSize: 12 }} title={`${LABEL[f]} (${f}): ${num(v)} · t ${num(t[f], 1)}`}>
            <span style={{ color: "var(--fg-2)" }}>{LABEL[f]}</span>
            <span style={{ position: "relative", height: 10 }} aria-hidden="true">
              <span style={{ position: "absolute", left: "50%", top: -3, bottom: -3, width: 1, background: "var(--line)" }} />
              <span style={{ position: "absolute", top: 0, bottom: 0, left: v >= 0 ? "50%" : `${50 - (Math.abs(v) / max) * 50}%`, width: `${(Math.abs(v) / max) * 50}%`, minWidth: 2, background: c1, opacity: sig(t[f]) ? 1 : 0.4, borderRadius: v >= 0 ? "0 4px 4px 0" : "4px 0 0 4px" }} />
            </span>
            <span className="num">{num(v)} <span style={{ color: "var(--muted)" }}>(t {num(t[f], 1)})</span></span>
          </div>
        );
      })}
    </div>
  );
}
