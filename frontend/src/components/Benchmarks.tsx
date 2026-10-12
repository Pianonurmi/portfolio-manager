// Benchmarks: the portfolio's growth next to broad indices and mixes, with risk/return stats per benchmark.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import type { Ctx } from "../lib/context";
import { date, decimal, pct, signedPct } from "../lib/format";
import { Legend, LineChart, useCssVar } from "./LineChart";
import { useToast } from "./Toasts";
import { Chip, Empty, Panel, Skeleton } from "./ui";

const PERIODS = ["1Y", "3Y", "5Y", "10Y", "MAX"] as const;
type Period = (typeof PERIODS)[number];
const GROUPS = ["Global equity", "Regions", "Bonds", "Mixes"];
const MAX_SEL = 4;

type Stats = { total: number; cagr: number; volatility: number; max_drawdown: number; sharpe: number | null };
type Bench = Partial<Stats> & {
  key: string; name: string; fund: string | null; group: string; available: boolean; from?: string;
  excess_cagr?: number; beta?: number; correlation?: number; tracking_error?: number; index?: number[];
};
type Data = { period: string; months: string[]; portfolio: (Stats & { index: number[] }) | null; benchmarks: Bench[]; notes: string[] };

const num2 = (x: number | null | undefined) => (x == null ? "—" : decimal(x, 2));

export function Benchmarks({ ctx }: { ctx: Ctx }) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const [period, setPeriod] = useState<Period>("5Y");
  const [sel, setSel] = useState<string[]>(["ACWI_IMI", "WORLD", "SP500", "MIX_60_40"]);
  const colors = [useCssVar("--c1"), useCssVar("--c2"), useCssVar("--c3"), useCssVar("--c4"), useCssVar("--c5")];
  const res = useQuery({
    queryKey: ["benchmarks", ctx.scope, period],
    queryFn: () => api<Data>(`/api/portfolio/benchmarks?scope=${encodeURIComponent(ctx.scope)}&period=${period}`),
    retry: false,
    placeholderData: (p) => p,
  });
  const refresh = useMutation({
    mutationFn: () => api<{ run_id: number }>("/api/portfolio/benchmarks/refresh", { method: "POST" }),
    onSuccess: () => { toast("Fetching benchmark prices…"); setTimeout(() => void qc.invalidateQueries({ queryKey: ["benchmarks"] }), 30000); },
    onError: (e) => toast(e instanceof Error ? e.message : "Could not refresh"),
  });
  const T = "Benchmarks";
  if (res.isLoading) return <Panel title={T} style={{ marginTop: 12 }}><Skeleton w={240} /></Panel>;
  if (res.isError || !res.data) return <Panel title={T} style={{ marginTop: 12 }}><Empty title="No benchmarks">{(res.error as Error | null)?.message ?? "Not available."}</Empty></Panel>;
  const d = res.data;
  if (!d.portfolio) return <Panel title={T} style={{ marginTop: 12 }}><Empty title="No benchmark comparison">{d.notes[0] ?? "Not enough history yet."}</Empty></Panel>;
  const p = d.portfolio;

  const toggle = (k: string) => setSel((s) => (s.includes(k) ? s.filter((x) => x !== k) : s.length >= MAX_SEL ? s : [...s, k]));
  // LineChart cannot draw gaps, so a benchmark that starts late follows the portfolio until its first month.
  const align = (b: Bench): number[] => {
    const idx = b.index ?? [];
    const off = Math.max(0, d.months.length - idx.length);
    const start = idx[0] ?? 1;
    const base = off > 0 ? (p.index[off] ?? 1) / start : 1;
    return d.months.map((_, i) => (i < off ? (p.index[i] ?? NaN) : (idx[i - off] ?? NaN) * (off > 0 ? base : 1)));
  };
  const chosen = d.benchmarks.filter((b) => b.available && b.index && sel.includes(b.key));
  const series = [
    { name: "Your portfolio", values: p.index, color: colors[0] ?? "" },
    ...chosen.map((b, i) => ({ name: b.name, values: align(b), color: colors[(i + 1) % colors.length] ?? "", dashed: i + 1 >= 5 })),
  ];
  const warn = { color: "var(--warn)" };
  const muted = { color: "var(--muted)" };

  return (
    <Panel
      title={T}
      style={{ marginTop: 12 }}
      meta={`${date(d.months[0])} – ${date(d.months[d.months.length - 1])} · month-end, growth of 1`}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", justifyContent: "space-between" }}>
          <span className="seg" role="group" aria-label="Period">
            {PERIODS.map((x) => (
              <button key={x} type="button" aria-pressed={period === x} className={period === x ? "on" : undefined} onClick={() => setPeriod(x)}>{x === "MAX" ? "Max" : x}</button>
            ))}
          </span>
          <button className="btn" disabled={refresh.isPending} onClick={() => refresh.mutate()}>{refresh.isPending ? "Refreshing…" : "Refresh"}</button>
        </div>
        <Legend items={series.map((s) => ({ name: s.name, color: s.color }))} />
        <LineChart label="Growth of 1: portfolio against benchmarks" x={d.months} series={series} format={(v) => decimal(v, 2)} height={240} />
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>Show</th><th>Name</th><th className="hide-sm">Since</th><th className="r">CAGR</th><th className="r">Your excess</th>
                <th className="r">Volatility</th><th className="r">Max drawdown</th><th className="r">Sharpe</th>
                <th className="r hide-sm">Beta</th><th className="r hide-sm">Correlation</th><th className="r hide-sm">Tracking error</th>
              </tr>
            </thead>
            <tbody>
              <tr style={{ cursor: "default", fontWeight: 600 }}>
                <td></td><td>Your portfolio</td><td className="hide-sm">{date(d.months[0])}</td>
                <td className="r num">{pct(p.cagr, 1)}</td><td className="r num">—</td>
                <td className="r num">{pct(p.volatility, 1)}</td><td className="r num">{pct(p.max_drawdown, 1)}</td><td className="r num">{num2(p.sharpe)}</td>
                <td className="r num hide-sm">—</td><td className="r num hide-sm">—</td><td className="r num hide-sm">—</td>
              </tr>
              {GROUPS.concat(d.benchmarks.map((b) => b.group).filter((g) => !GROUPS.includes(g))).filter((g, i, a) => a.indexOf(g) === i).map((g) => {
                const rows = d.benchmarks.filter((b) => b.group === g);
                if (!rows.length) return null;
                return [
                  <tr key={"g" + g} style={{ cursor: "default" }}><td colSpan={11} style={{ ...muted, fontSize: 12, textTransform: "uppercase", letterSpacing: ".04em" }}>{g}</td></tr>,
                  ...rows.map((b) => {
                    const on = sel.includes(b.key);
                    return (
                      <tr key={b.key} style={{ cursor: "default" }}>
                        <td><input type="checkbox" aria-label={`Show ${b.name} in chart`} checked={on} disabled={!b.available || (!on && sel.length >= MAX_SEL)} onChange={() => toggle(b.key)} /></td>
                        <td><div className="inst">{b.name}{b.fund && <small>{b.fund}</small>}</div></td>
                        {b.available ? (
                          <>
                            <td className="hide-sm">{b.from ? date(b.from) : "—"}</td>
                            <td className="r num">{b.cagr == null ? "—" : pct(b.cagr, 1)}</td>
                            <td className="r num" style={b.excess_cagr == null ? undefined : b.excess_cagr > 0 ? { color: "var(--ok)" } : b.excess_cagr < 0 ? warn : undefined}>{b.excess_cagr == null ? "—" : signedPct(b.excess_cagr, 1)}</td>
                            <td className="r num">{b.volatility == null ? "—" : pct(b.volatility, 1)}</td>
                            <td className="r num">{b.max_drawdown == null ? "—" : pct(b.max_drawdown, 1)}</td>
                            <td className="r num">{num2(b.sharpe)}</td>
                            <td className="r num hide-sm">{num2(b.beta)}</td>
                            <td className="r num hide-sm">{num2(b.correlation)}</td>
                            <td className="r num hide-sm">{b.tracking_error == null ? "—" : pct(b.tracking_error, 1)}</td>
                          </>
                        ) : (
                          <td colSpan={9} style={muted}>no price history yet</td>
                        )}
                      </tr>
                    );
                  }),
                ];
              })}
            </tbody>
          </table>
        </div>
        {d.notes.length > 0 && <div style={{ ...muted, fontSize: 12 }}>{d.notes.map((n, i) => <p key={i} style={{ margin: "2px 0" }}>{n}</p>)}</div>}
        {d.benchmarks.some((b) => b.available && b.from && b.from > d.months[0]!) && <Chip>Benchmarks with shorter history follow your portfolio until their first month</Chip>}
      </div>
    </Panel>
  );
}
