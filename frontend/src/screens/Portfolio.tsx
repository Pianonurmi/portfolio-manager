// Portfolio (PORT-01..04): returns, holdings grouped by account, value vs contributions, income.
import { Fragment, useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router";
import { Figure } from "../components/Figure";
import { Allocation } from "../components/Rebalance";
import { Benchmarks } from "../components/Benchmarks";
import { XRay } from "../components/XRay";
import { Factors } from "../components/Factors";
import { Advisor } from "../components/Advisor";
import { Hedging } from "../components/Hedging";
import { Leverage } from "../components/Leverage";
import { NewMoney } from "../components/NewMoney";
import { Optimize } from "../components/Optimize";
import { Risk } from "../components/Risk";
import { useInspector } from "../components/Inspector";
import { Legend, LineChart, useCssVar } from "../components/LineChart";
import { Empty, Panel, ScreenHead, Skeleton } from "../components/ui";
import { useCtx } from "../lib/context";
import { arrow, date, decimal, num, pct, quantity, signedPct } from "../lib/format";
import { useHoldings, useIncome, usePerformance, type HoldingRow } from "../lib/queries";

const PERIODS = ["1M", "YTD", "1Y", "3Y", "5Y", "Max"];

export function Portfolio() {
  const [ctx] = useCtx();
  const loc = useLocation();
  const h = useHoldings(ctx);
  const perf = usePerformance(ctx);
  const inc = useIncome(ctx);
  const insp = useInspector();
  const c1 = useCssVar("--c1");
  const c3 = useCssVar("--c3");
  const [group, setGroup] = useState<"account" | "class" | "currency">("account");
  const [sel, setSel] = useState(0);

  const rows = useMemo(() => {
    const out: { key: string; label: string; value?: HoldingRow["value"]; rows: (HoldingRow & { account: string })[] }[] = [];
    if (!h.data) return out;
    if (group === "account") {
      for (const a of h.data.accounts) out.push({ key: `a${a.account_id}`, label: `${a.name} · ${a.kind_label}`, value: a.value, rows: a.holdings.map((r) => ({ ...r, account: a.name })) });
    } else {
      const map = new Map<string, (HoldingRow & { account: string })[]>();
      for (const a of h.data.accounts)
        for (const r of a.holdings) {
          const k = group === "class" ? (r.is_cash ? "Cash" : r.type === "etf" || r.type === "fund" ? "Funds & ETFs" : r.type === "crypto" ? "Crypto" : "Stocks") : r.currency;
          map.set(k, [...(map.get(k) ?? []), { ...r, account: a.name }]);
        }
      for (const [k, list] of map) out.push({ key: k, label: k, rows: list.sort((x, y) => num(y.value.value) - num(x.value.value)) });
    }
    return out;
  }, [h.data, group]);
  const flat = rows.flatMap((g) => g.rows);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName) || e.metaKey || e.ctrlKey || document.querySelector(".inspector,.pal-wrap,.modal-wrap")) return;
      if (e.key === "j" || e.key === "ArrowDown") (e.preventDefault(), setSel((s) => Math.min(flat.length - 1, s + 1)));
      else if (e.key === "k" || e.key === "ArrowUp") (e.preventDefault(), setSel((s) => Math.max(0, s - 1)));
      else if (e.key === "Enter" && flat[sel] && t.tagName !== "BUTTON") insp.open(flat[sel]!.value.ref!, flat[sel]!.name);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [flat, sel, insp]);
  useEffect(() => {
    document.querySelector(`#holdings tr[data-i="${sel}"]`)?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  if (h.data && h.data.accounts.length === 0)
    return (
      <>
        <ScreenHead title="Portfolio" />
        <Panel bodyless>
          <Empty title="No holdings yet" action={<Link className="btn primary" to={{ pathname: "/imports", search: loc.search }}>Import from Nordnet</Link>}>
            Holdings, returns and income appear after your first import.
          </Empty>
        </Panel>
      </>
    );
  const p = perf.data?.periods ?? {};
  const series = perf.data?.series ?? [];
  let idx = -1;
  return (
    <>
      <ScreenHead
        title="Portfolio"
        sub={h.data ? `${flat.filter((r) => !r.is_cash).length} holdings · as of ${date(h.data.as_of)}` : ""}
        actions={
          <div className="seg" role="group" aria-label="Group by">
            {(["account", "class", "currency"] as const).map((g) => (
              <button key={g} aria-pressed={group === g} onClick={() => setGroup(g)}>
                {g === "account" ? "By account" : g === "class" ? "By asset class" : "By currency"}
              </button>
            ))}
          </div>
        }
      />
      <Panel title="Returns" meta={`TWR and XIRR · p.a. above 1 year${perf.data?.benchmark ? ` · benchmark ${perf.data.benchmark.name}` : ""}`} bodyless>
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th />
                {PERIODS.map((x) => (
                  <th key={x} className="r">{x}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr style={{ cursor: "default" }}>
                <td>Time-weighted</td>
                {PERIODS.map((x) => (
                  <td key={x} className="r">{perf.isLoading ? <Skeleton w={50} /> : <Figure f={p[x]?.twr} kind="pct" delta />}</td>
                ))}
              </tr>
              <tr style={{ cursor: "default" }}>
                <td>Money-weighted (XIRR)</td>
                {PERIODS.map((x) => (
                  <td key={x} className="r">{perf.isLoading ? <Skeleton w={50} /> : <Figure f={p[x]?.xirr} kind="pct" delta />}</td>
                ))}
              </tr>
              <tr style={{ cursor: "default" }}>
                <td>Gain</td>
                {PERIODS.map((x) => (
                  <td key={x} className="r"><Figure f={p[x]?.gain} delta /></td>
                ))}
              </tr>
              {perf.data?.benchmark && (
                <tr style={{ cursor: "default" }}>
                  <td style={{ color: "var(--muted)" }}>Benchmark</td>
                  {PERIODS.map((x) => (
                    <td key={x} className="r num" style={{ color: "var(--muted)" }}>{p[x]?.benchmark == null ? "—" : signedPct(p[x]!.benchmark!)}</td>
                  ))}
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel
        title="Holdings"
        style={{ marginTop: 12 }}
        meta={
          <>
            <span className="hide-sm"><span className="kbd">j</span><span className="kbd">k</span> move · <span className="kbd">↵</span> explain</span>
            {h.data && <Figure f={h.data.total} />}
          </>
        }
        bodyless
      >
        <div className="tbl-wrap">
          <table id="holdings">
            <thead>
              <tr>
                <th>Instrument</th>
                <th className="r hide-sm">Qty</th>
                <th className="r hide-sm">Avg cost</th>
                <th className="r hide-sm">Price</th>
                <th className="r">Value</th>
                <th className="r">Unrealised</th>
                <th className="r hide-sm">%</th>
                <th className="r hide-sm">Day</th>
                <th className="r hide-sm">Weight</th>
              </tr>
            </thead>
            <tbody>
              {h.isLoading && (
                <tr><td colSpan={9}><Skeleton w={300} /></td></tr>
              )}
              {rows.map((g) => (
                <Fragment key={g.key}>{groupRows(g)}</Fragment>
              ))}
              {h.data && (
                <tr className="total">
                  <td>Total</td>
                  <td className="hide-sm" />
                  <td className="hide-sm" />
                  <td className="hide-sm" />
                  <td className="r"><Figure f={h.data.total} /></td>
                  <td className="r"><Figure f={h.data.unrealized} delta /></td>
                  <td className="r num hide-sm">{num(h.data.cost.value) ? signedPct(num(h.data.unrealized.value) / num(h.data.cost.value)) : ""}</td>
                  <td className="hide-sm" />
                  <td className="r num hide-sm">100,0 %</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {h.data?.warnings.length ? <p className="err" style={{ padding: "8px 14px" }}>{h.data.warnings.join(" · ")}</p> : null}
      </Panel>

      <Benchmarks ctx={ctx} />
      <XRay ctx={ctx} />
      <Risk ctx={ctx} />
      <Optimize ctx={ctx} />
      <Factors ctx={ctx} />
      <Advisor ctx={ctx} />
      <Leverage />
      <Hedging ctx={ctx} />
      <Allocation ctx={ctx} />
      <NewMoney ctx={ctx} />
      <div className="grid g-2" style={{ marginTop: 12 }}>
        <Panel title="Value vs. net contributions" meta={<Legend items={[{ name: "Value", color: c1 }, { name: "Net contributions", color: c3 }]} />}>
          <LineChart
            label="Portfolio value and net contributions"
            x={series.map((r) => r.date)}
            series={[
              { name: "Value", values: series.map((r) => num(r.value)), color: c1, area: true },
              { name: "Net contributions", values: series.map((r) => num(r.contributions)), color: c3 },
            ]}
            height={200}
          />
        </Panel>
        <Panel title="Income" meta={ctx.period}>
          <dl className="kv">
            <dt>Dividends received (gross)</dt>
            <dd><Figure f={inc.data?.dividends} digits={2} /></dd>
            <dt>Tax withheld</dt>
            <dd><Figure f={inc.data?.withholding} digits={2} /></dd>
            <dt>Interest</dt>
            <dd><Figure f={inc.data?.interest} digits={2} /></dd>
            <dt>Forecast next 12 months</dt>
            <dd><Figure f={inc.data?.forecast_12m} /></dd>
            <dt>Forecast yield on value</dt>
            <dd>{inc.data?.forecast_yield == null ? "—" : pct(inc.data.forecast_yield, 2)}</dd>
            <dt>Yield on cost</dt>
            <dd>{inc.data?.yield_on_cost == null ? "—" : pct(inc.data.yield_on_cost, 2)}</dd>
          </dl>
          {inc.data && (
            <div style={{ marginTop: 6, fontSize: 12, color: "var(--muted)" }}>
              Forecast = units held × the last 12 months of distributions per unit: Yahoo for {inc.data.forecast_sources.yahoo} holdings, your own bookings for {inc.data.forecast_sources.ledger}.
            </div>
          )}
          {!!inc.data?.by_instrument.length && (
            <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 4 }}>
              {inc.data.by_instrument.slice(0, 6).map((r) => (
                <div key={r.name} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "var(--fg-2)" }}>
                  <span>{r.name}</span>
                  <span className="num">{decimal(r.eur, 2)} €</span>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </>
  );

  function groupRows(g: (typeof rows)[number]) {
    return (
      <>
        <tr className="group">
          <td colSpan={9}>
            <b>{g.label}</b>
            {g.value && <> · <Figure f={g.value} /></>}
          </td>
        </tr>
        {g.rows.map((r) => {
          idx += 1;
          const i = idx;
          const day = r.day_pct;
          return (
            <tr key={`${g.key}-${r.account}-${r.instrument_id}`} data-i={i} aria-selected={i === sel} onClick={() => (setSel(i), insp.open(r.value.ref!, r.name))}>
              <td>
                <div className="inst">
                  <span>{r.name}</span>
                  <small>
                    {[r.symbol, r.currency, group !== "account" ? r.account : null].filter(Boolean).join(" · ")}
                  </small>
                </div>
              </td>
              <td className="r num hide-sm">{r.is_cash ? "" : quantity(r.quantity)}</td>
              <td className="r num hide-sm">{r.avg_cost ? decimal(r.avg_cost, 2) : ""}</td>
              <td className="r num hide-sm">
                {r.price ? `${decimal(r.price, 2, 4)}${r.price_currency && r.price_currency !== "EUR" ? " " + r.price_currency : ""}` : ""}
              </td>
              <td className="r"><Figure f={r.value} /></td>
              <td className="r">{r.is_cash ? "" : <Figure f={r.unrealized} delta />}</td>
              <td className="r num hide-sm" style={{ color: (r.unrealized_pct ?? 0) >= 0 ? "var(--gain)" : "var(--loss)" }}>
                {r.unrealized_pct == null ? "" : signedPct(r.unrealized_pct)}
              </td>
              <td className="r num hide-sm" style={{ color: day == null ? "var(--muted)" : day > 0 ? "var(--gain)" : day < 0 ? "var(--loss)" : "var(--muted)" }}>
                {day == null ? "—" : `${arrow(day)} ${signedPct(day)}`}
              </td>
              <td className="r num hide-sm">{pct(r.weight)}</td>
            </tr>
          );
        })}
      </>
    );
  }
}
