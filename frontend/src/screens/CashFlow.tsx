// Cash flow from YNAB or Actual (CF-01..04): monthly income vs spending, savings rate, categories.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useLocation } from "react-router";
import { Figure } from "../components/Figure";
import { Legend } from "../components/LineChart";
import { Empty, Panel, ScreenHead, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { compact, money, num, pct } from "../lib/format";

type Summary = {
  months: { month: string; income: string; spending: string; saved: string; rate: number | null }[];
  avg_income: import("../lib/types").Figure;
  avg_spending: import("../lib/types").Figure;
  savings_rate: number | null;
  categories: { category: string; total: string; monthly_avg: string; by_month: Record<string, string> }[];
  connected: boolean;
  backend: "ynab" | "actual" | null;
};

export function CashFlow() {
  const loc = useLocation();
  const [months, setMonths] = useState(12);
  const s = useQuery({ queryKey: ["cashflow", months], queryFn: () => api<Summary>(`/api/cashflow/summary?months=${months}`) });
  if (s.isLoading) return <Skeleton w={240} />;
  if (!s.data?.connected || s.data.months.every((m) => num(m.income) === 0 && num(m.spending) === 0))
    return (
      <>
        <ScreenHead title="Cash flow" />
        <Panel bodyless>
          <Empty title="Connect your budget to see income and spending" action={<Link className="btn primary" to={{ pathname: "/settings/connections", search: loc.search }}>Connect YNAB or Actual</Link>}>
            Budgeting stays in YNAB (or Actual Budget). This reads your transactions to show savings rate and real spending averages, which also feed the planning studio.
          </Empty>
        </Panel>
      </>
    );
  const d = s.data;
  const max = Math.max(1, ...d.months.flatMap((m) => [num(m.income), num(m.spending)]));
  return (
    <>
      <ScreenHead
        title="Cash flow"
        sub={`From ${d.backend === "actual" ? "Actual Budget" : "YNAB"} · transfers between your own accounts excluded · current month is partial`}
        actions={
          <div className="seg" role="group" aria-label="Months">
            {[6, 12, 24].map((m) => (
              <button key={m} aria-pressed={months === m} onClick={() => setMonths(m)}>{m} mo</button>
            ))}
          </div>
        }
      />
      <section className="panel">
        <div className="kpis">
          <div className="kpi"><span className="label">Avg income</span><span className="v"><Figure f={d.avg_income} /></span></div>
          <div className="kpi"><span className="label">Avg spending</span><span className="v"><Figure f={d.avg_spending} /></span></div>
          <div className="kpi"><span className="label">Avg saved</span><span className="v">{money(num(d.avg_income.value) - num(d.avg_spending.value))}</span></div>
          <div className="kpi"><span className="label">Savings rate</span><span className="v mono">{d.savings_rate == null ? "—" : pct(d.savings_rate)}</span></div>
        </div>
        <div className="panel-b">
          <div style={{ marginBottom: 8, fontSize: 12, color: "var(--fg-2)" }}><Legend items={[{ name: "Income", color: "var(--c3)" }, { name: "Spending", color: "var(--c2)" }]} /></div>
          <div role="img" aria-label="Monthly income and spending" style={{ display: "grid", gridTemplateColumns: `repeat(${d.months.length}, minmax(0,1fr))`, gap: 6, alignItems: "end", height: 170 }}>
            {d.months.map((m) => (
              <div key={m.month} title={`${m.month}: income ${money(m.income)}, spending ${money(m.spending)}`} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 4, height: "100%", justifyContent: "flex-end" }}>
                <div style={{ display: "flex", gap: 2, alignItems: "flex-end", height: 140, width: "100%", justifyContent: "center" }}>
                  <span style={{ width: "40%", maxWidth: 14, height: `${(num(m.income) / max) * 100}%`, background: "var(--c3)", borderRadius: "3px 3px 0 0" }} />
                  <span style={{ width: "40%", maxWidth: 14, height: `${(num(m.spending) / max) * 100}%`, background: "var(--c2)", borderRadius: "3px 3px 0 0" }} />
                </div>
                <span className="num" style={{ fontSize: 10, color: "var(--muted)" }}>{m.month.slice(5)}/{m.month.slice(2, 4)}</span>
              </div>
            ))}
          </div>
        </div>
      </section>
      <Panel title="Spending by category" meta={`monthly average over ${months} months`} style={{ marginTop: 12 }} bodyless>
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>Category</th>
                <th className="r">Per month</th>
                <th className="r hide-sm">Total</th>
                <th className="hide-sm">Share</th>
              </tr>
            </thead>
            <tbody>
              {d.categories.map((c) => (
                <tr key={c.category} style={{ cursor: "default" }}>
                  <td>{c.category}</td>
                  <td className="r num">{money(c.monthly_avg)}</td>
                  <td className="r num hide-sm">{compact(num(c.total))}</td>
                  <td className="hide-sm" style={{ width: 160 }}>
                    <div className="meter"><span style={{ width: `${(num(c.monthly_avg) / Math.max(1, num(d.avg_spending.value))) * 100}%`, background: "var(--c1)" }} /></div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}
