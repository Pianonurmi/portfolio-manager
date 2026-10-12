// Home (UX-03, NW-01, PORT-03): net worth headline, tiles, history, needs attention, allocation.
// Widgets can be shown/hidden and reordered; the layout is saved on the server (dashboards API).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate } from "react-router";
import { useState } from "react";
import { Figure } from "../components/Figure";
import { useInspector } from "../components/Inspector";
import { Legend, LineChart, useCssVar } from "../components/LineChart";
import { Chip, Panel, ScreenHead, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { useCtx } from "../lib/context";
import { date, num, pct } from "../lib/format";
import { ANALYTICS_KEYS, useAttention, useHoldings, useNetworth, useNwHistory, usePerformance } from "../lib/queries";
import { useToast } from "../components/Toasts";
import type { Account } from "../lib/types";
import { Onboarding } from "./Home";

const WIDGETS = [
  { id: "summary", title: "Net worth and key figures" },
  { id: "history", title: "Net worth history" },
  { id: "attention", title: "Needs attention" },
  { id: "allocation", title: "Allocation" },
] as const;
type WidgetId = (typeof WIDGETS)[number]["id"];
type Layout = { widgets: { id: WidgetId; visible: boolean }[] };
type Dash = { id: number; name: string; is_home: boolean; layout: Layout; version: number };
const DEFAULT: Layout = { widgets: WIDGETS.map((w) => ({ id: w.id, visible: true })) };

export function Dashboard() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/api/accounts") });
  const dash = useQuery({ queryKey: ["dashboards"], queryFn: () => api<Dash[]>("/api/dashboards") });
  const [editing, setEditing] = useState(false);
  const qc = useQueryClient();
  const home = dash.data?.find((d) => d.is_home);
  const layout: Layout = home?.layout?.widgets ? home.layout : DEFAULT;
  const save = useMutation({
    mutationFn: (l: Layout) => api(`/api/dashboards/${home?.id ?? 0}`, { method: "PUT", body: { name: "Home", is_home: true, layout: l } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dashboards"] }),
  });
  if (accounts.isLoading) return <Skeleton w={240} />;
  if (!accounts.data?.length) return <Onboarding />;
  const move = (i: number, dir: -1 | 1) => {
    const ws = [...layout.widgets];
    const j = i + dir;
    if (j < 0 || j >= ws.length) return;
    [ws[i], ws[j]] = [ws[j]!, ws[i]!];
    save.mutate({ widgets: ws });
  };
  const toggle = (i: number) => save.mutate({ widgets: layout.widgets.map((w, k) => (k === i ? { ...w, visible: !w.visible } : w)) });
  return (
    <>
      <ScreenHead
        title="Home"
        actions={<button className="btn" onClick={() => setEditing((e) => !e)} aria-pressed={editing}>{editing ? "Done" : "Edit layout"}</button>}
      />
      {editing && (
        <Panel title="Layout" meta="Show, hide and reorder widgets · saved for every device" bodyless>
          <div className="list">
            {layout.widgets.map((w, i) => (
              <div key={w.id} className="li" style={{ cursor: "default" }}>
                <input type="checkbox" checked={w.visible} onChange={() => toggle(i)} aria-label={`Show ${w.id}`} />
                <div className="t">{WIDGETS.find((x) => x.id === w.id)?.title}</div>
                <button className="btn" onClick={() => move(i, -1)} disabled={i === 0} aria-label="Move up">↑</button>
                <button className="btn" onClick={() => move(i, 1)} disabled={i === layout.widgets.length - 1} aria-label="Move down">↓</button>
              </div>
            ))}
          </div>
        </Panel>
      )}
      <div className="grid" style={{ marginTop: editing ? 12 : 0 }}>
        {layout.widgets.filter((w) => w.visible).map((w) => (
          <Widget key={w.id} id={w.id} />
        ))}
      </div>
    </>
  );
}

function Widget({ id }: { id: WidgetId }) {
  if (id === "summary") return <Summary />;
  if (id === "history") return <History />;
  if (id === "attention") return <AttentionList />;
  return <Allocation />;
}

function Summary() {
  const [ctx] = useCtx();
  const nw = useNetworth(ctx);
  const perf = usePerformance(ctx);
  const hist = useNwHistory(ctx);
  const d = nw.data;
  const s = hist.data?.series ?? [];
  const first = s[0];
  const delta = d && first ? num(d.net_worth.value) - num(first.net_worth) : null;
  const g = (id: string) => d?.groups.find((x) => x.id === id)?.total;
  const p = perf.data?.periods[ctx.period];
  return (
    <section className="panel hero-panel">
      <div className="hero">
        <span className="label">Net worth</span>
        <span className="value">{d ? <Figure f={d.net_worth} /> : <Skeleton w={220} />}</span>
        <span>
          {delta != null && (
            <>
              <Figure f={{ value: String(delta), currency: "EUR", ref: null }} delta /> <span style={{ color: "var(--muted)" }}>·</span>{" "}
              {first && num(first.net_worth) !== 0 && <Figure f={{ value: String(delta / Math.abs(num(first.net_worth))), currency: null, ref: null }} kind="pct" delta />}{" "}
              <span style={{ color: "var(--muted)" }}>{ctx.period}</span>
            </>
          )}
        </span>
      </div>
      <div className="tiles">
        <div className="tile"><span className="label">Investments</span><span className="v"><Figure f={g("investments")} /></span></div>
        <div className="tile"><span className="label">Cash</span><span className="v"><Figure f={g("cash")} /></span></div>
        <div className="tile"><span className="label">Home equity</span><span className="v"><Figure f={d?.home_equity} /></span></div>
        <div className="tile"><span className="label">Debt</span><span className="v"><Figure f={d?.liabilities} /></span></div>
        <div className="tile">
          <span className="label">Return {ctx.period} (TWR)</span>
          <span className="v"><Figure f={p?.twr} kind="pct" delta /></span>
          {p?.benchmark != null && <span className="d" style={{ color: "var(--muted)" }}>Benchmark {pct(p.benchmark)}</span>}
        </div>
      </div>
    </section>
  );
}

function History() {
  const [ctx] = useCtx();
  const hist = useNwHistory(ctx);
  const c1 = useCssVar("--c1");
  const c3 = useCssVar("--c3");
  const c2 = useCssVar("--c2");
  const s = hist.data?.series ?? [];
  const hasDebt = s.some((r) => num(r.liabilities) > 0);
  return (
    <Panel title="Net worth history" meta={<Legend items={[{ name: "Net worth", color: c1 }, ...(hasDebt ? [{ name: "Assets", color: c3 }, { name: "Liabilities", color: c2 }] : [])]} />}>
      <LineChart
        label="Net worth history"
        x={s.map((r) => r.date)}
        series={[
          { name: "Net worth", values: s.map((r) => num(r.net_worth)), color: c1, area: true },
          ...(hasDebt
            ? [
                { name: "Assets", values: s.map((r) => num(r.assets)), color: c3 },
                { name: "Liabilities", values: s.map((r) => num(r.liabilities)), color: c2 },
              ]
            : []),
        ]}
        height={210}
      />
    </Panel>
  );
}

function AttentionList() {
  const att = useAttention();
  const qc = useQueryClient();
  const { toast } = useToast();
  const runAction = async (path: string, label: string) => {
    try {
      const r = await api<{ undo_id?: number }>(path, { method: "POST" });
      for (const k of ANALYTICS_KEYS) qc.invalidateQueries({ queryKey: [k] });
      toast(`${label}: done`, { undoId: r?.undo_id, onUndo: () => ANALYTICS_KEYS.forEach((k) => qc.invalidateQueries({ queryKey: [k] })) });
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed", { error: true });
    }
  };
  const nav = useNavigate();
  const loc = useLocation();
  const insp = useInspector();
  const items = att.data ?? [];
  return (
    <Panel title="Needs attention" meta={`${items.length} item${items.length === 1 ? "" : "s"}`} bodyless>
      {items.length === 0 ? (
        <div className="empty" style={{ padding: "14px" }}><p>All clear: imports reviewed, prices fresh, accounts reconciled.</p></div>
      ) : (
        <div className="list">
          {items.map((a, i) => (
            <div key={i} className="li" role="button" tabIndex={0}
              onClick={() => (a.ref ? insp.open(a.ref, a.title) : a.route && nav({ pathname: a.route, search: loc.search }))}
              onKeyDown={(e) => e.key === "Enter" && (a.ref ? insp.open(a.ref, a.title) : a.route && nav({ pathname: a.route, search: loc.search }))}>
              <span className={`sev ${a.severity}`} />
              <div className="t">{a.title}<small>{a.detail}</small></div>
              {a.action ? (
                <button className="btn primary" onClick={(e) => (e.stopPropagation(), runAction(a.action!.path, a.action!.label))}>{a.action.label}</button>
              ) : (
                a.chip && <Chip tone={a.severity === "bad" ? "bad" : a.severity === "warn" ? "warn" : "info"}>{a.chip}</Chip>
              )}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function Allocation() {
  const [ctx] = useCtx();
  const h = useHoldings(ctx);
  const nw = useNetworth(ctx);
  const loc = useLocation();
  const cls = new Map<string, number>();
  for (const a of h.data?.accounts ?? [])
    for (const r of a.holdings) {
      const k = r.is_cash ? "Cash" : r.type === "etf" || r.type === "fund" ? "Funds & ETFs" : r.type === "crypto" ? "Crypto" : "Stocks";
      cls.set(k, (cls.get(k) ?? 0) + num(r.value.value));
    }
  const cash = num(nw.data?.groups.find((g) => g.id === "cash")?.total.value ?? 0);
  if (cash) cls.set("Cash", (cls.get("Cash") ?? 0) + cash);
  const eq = num(nw.data?.home_equity.value ?? 0);
  if (eq > 0) cls.set("Home equity", eq);
  const order = ["Funds & ETFs", "Stocks", "Cash", "Crypto", "Home equity"].filter((k) => (cls.get(k) ?? 0) > 0);
  const total = order.reduce((s, k) => s + (cls.get(k) ?? 0), 0);
  const colors = ["--c1", "--c2", "--c3", "--c4", "--c5"];
  return (
    <Panel title="Allocation" meta={<Link to={{ pathname: "/portfolio", search: loc.search }} style={{ color: "var(--muted)" }}>Portfolio →</Link>}>
      {total <= 0 ? (
        <span style={{ color: "var(--muted)" }}>No assets yet</span>
      ) : (
        <>
          <div className="strip" role="img" aria-label="Allocation by asset class">
            {order.map((k, i) => (
              <span key={k} style={{ width: `${((cls.get(k) ?? 0) / total) * 100}%`, background: `var(${colors[i]})` }} title={k} />
            ))}
          </div>
          <div className="legend">
            {order.map((k, i) => (
              <div className="lg" key={k}>
                <i style={{ background: `var(${colors[i]})` }} />
                {k}
                <span className="num">{pct((cls.get(k) ?? 0) / total)}</span>
              </div>
            ))}
          </div>
          <p style={{ margin: "10px 0 0", fontSize: 11, color: "var(--muted)" }}>As of {h.data ? date(h.data.as_of) : "—"} · home at equity (value minus mortgage and housing-company loan share)</p>
        </>
      )}
    </Panel>
  );
}
