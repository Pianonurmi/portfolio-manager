// Allocation by dimension, targets with drift band, tax-aware rebalancing suggestions (PORT-05/06).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import type { Ctx } from "../lib/context";
import { money, num, pct, signedPct } from "../lib/format";
import { Panel } from "./ui";

type Alloc = { items: { key: string; value: string; weight: number }[]; total: string };
type Reb = { rows: { class: string; value: string; weight: number; target: number; drift: number }[]; trades: Record<string, string>[]; band: string };
const COLORS = ["--c1", "--c2", "--c3", "--c4", "--c5"];

export function Allocation({ ctx }: { ctx: Ctx }) {
  const [dim, setDim] = useState("asset_class");
  const [cash, setCash] = useState("0");
  const qc = useQueryClient();
  const al = useQuery({ queryKey: ["allocation", ctx.scope, dim], queryFn: () => api<Alloc>(`/api/portfolio/allocation?scope=${encodeURIComponent(ctx.scope)}&dim=${dim}`) });
  const reb = useQuery({ queryKey: ["rebalance", ctx.scope, cash], queryFn: () => api<Reb>(`/api/portfolio/rebalance?scope=${encodeURIComponent(ctx.scope)}&new_money=${num(cash) || 0}`) });
  const tg = useQuery({ queryKey: ["targets"], queryFn: () => api<{ targets: Record<string, string>; band: string }>("/api/portfolio/targets") });
  const [edit, setEdit] = useState<Record<string, string> | null>(null);
  const saveTargets = async () => {
    if (!edit) return;
    const targets = Object.fromEntries(Object.entries(edit).filter(([, v]) => v !== "").map(([k, v]) => [k, String(num(v.replace(",", ".")) / 100)]));
    await api("/api/portfolio/targets", { method: "PUT", body: { targets, band: tg.data?.band ?? "0.05" } });
    setEdit(null);
    qc.invalidateQueries({ queryKey: ["targets"] });
    qc.invalidateQueries({ queryKey: ["rebalance"] });
  };
  const items = al.data?.items ?? [];
  return (
    <div className="grid g-2" style={{ marginTop: 12 }}>
      <Panel title="Allocation" meta={
        <div className="seg">{([["asset_class", "Class"], ["region", "Region"], ["currency", "Currency"], ["instrument", "Instrument"]] as const).map(([k, l]) => <button key={k} aria-pressed={dim === k} onClick={() => setDim(k)}>{l}</button>)}</div>}>
        <div className="strip" role="img" aria-label="Allocation">
          {items.slice(0, 5).map((i, n) => <span key={i.key} style={{ width: `${i.weight * 100}%`, background: `var(${COLORS[n]})` }} />)}
          {items.length > 5 && <span style={{ width: `${items.slice(5).reduce((s, i) => s + i.weight, 0) * 100}%`, background: "var(--muted)" }} />}
        </div>
        <div className="legend">
          {items.slice(0, 5).map((i, n) => <div key={i.key} className="lg"><i style={{ background: `var(${COLORS[n]})` }} />{i.key}<span className="num">{pct(i.weight)}</span></div>)}
          {items.length > 5 && <div className="lg"><i style={{ background: "var(--muted)" }} />Other ({items.length - 5})<span className="num">{pct(items.slice(5).reduce((s, i) => s + i.weight, 0))}</span></div>}
        </div>
      </Panel>
      <Panel title="Rebalancing" meta={<span>band ±{pct(num(tg.data?.band ?? 0.05), 0)}</span>}>
        <div className="tbl-wrap">
          <table>
            <thead><tr><th>Class</th><th className="r">Now</th><th className="r">Target</th><th className="r">Drift</th></tr></thead>
            <tbody>
              {reb.data?.rows.map((r) => (
                <tr key={r.class} style={{ cursor: "default" }}>
                  <td>{r.class}</td>
                  <td className="r num">{pct(r.weight)}</td>
                  <td className="r">{edit ? <input className="input mono" style={{ width: 64, textAlign: "right" }} value={edit[r.class] ?? ""} onChange={(e) => setEdit({ ...edit, [r.class]: e.target.value })} /> : <span className="num">{pct(r.target)}</span>}</td>
                  <td className="r num" style={{ color: Math.abs(r.drift) > num(reb.data!.band) ? "var(--warn)" : "var(--muted)" }}>{signedPct(r.drift)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8, flexWrap: "wrap" }}>
          {edit ? <><button className="btn primary" onClick={saveTargets}>Save targets (%)</button><button className="btn" onClick={() => setEdit(null)}>Cancel</button></>
            : <button className="btn" onClick={() => setEdit(Object.fromEntries((reb.data?.rows ?? []).map((r) => [r.class, String(Math.round(r.target * 1000) / 10)])))}>Edit targets</button>}
          <label style={{ fontSize: 12, color: "var(--fg-2)" }}>New money € <input className="input mono" style={{ width: 90, display: "inline-block" }} value={cash} onChange={(e) => setCash(e.target.value)} /></label>
        </div>
        {!!reb.data?.trades.length && (
          <div className="list" style={{ marginTop: 8 }}>
            {reb.data.trades.map((t, i) => (
              <div key={i} className="li" style={{ cursor: "default", padding: "6px 0" }}>
                <span className={`sev ${t.action === "sell" ? "warn" : "info"}`} />
                <div className="t">{t.action === "buy" ? "Buy" : "Sell"} {money(t.amount ?? "0")} of {t.instrument ?? t.class}{t.account ? ` in ${t.account}` : ""}
                  <small>{t.where}{t.taxable_gain_estimate && num(t.taxable_gain_estimate) > 0 ? ` · ~${money(t.taxable_gain_estimate)} gain realised` : ""}</small></div>
              </div>
            ))}
          </div>
        )}
        {reb.data && !reb.data.trades.length && <p style={{ margin: "8px 0 0", fontSize: 12, color: "var(--muted)" }}>Within the band. Nothing to do.</p>}
      </Panel>
    </div>
  );
}
