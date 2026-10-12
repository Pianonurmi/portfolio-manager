// Tax-aware withdrawal order and "where should new money go" (PLAN-09), under the planning studio.
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import { money, pct } from "../lib/format";
import { Field, Panel } from "./ui";

type Run = { strategy: string; label: string; total_tax: number; depleted_age: number | null; end_wealth: number; end_after_tax: number };
type Cmp = { buckets: Record<string, number>; runs: Run[]; best: string; assumptions: string[] };
type Opt = { destination: string; value: number; invested: number; note: string };

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function TaxStrategy({ params: live }: { params: Record<string, unknown> }) {
  const params = useDebounced(live, 400); // sliders move fast; this is a heavier calculation
  const key = JSON.stringify(params);
  const w = useQuery({
    queryKey: ["withdrawals", key],
    queryFn: () => api<Cmp>("/api/plan/withdrawals", { body: { params } }),
    retry: false,
    placeholderData: (prev) => prev,
  });
  const [nm, setNm] = useState({ monthly: "500", years: "15", dividend_yield: "3", loan_rate: "" });
  const money_ = useQuery({
    queryKey: ["new-money", nm, params.real_return],
    queryFn: () =>
      api<{ ost_room: number; options: Opt[] }>("/api/plan/new-money", {
        body: {
          monthly: Number(nm.monthly) || 0,
          years: Number(nm.years) || 1,
          real_return: Number(params.real_return ?? 0.045),
          dividend_yield: (Number(nm.dividend_yield.replace(",", ".")) || 0) / 100,
          loan_rate: nm.loan_rate ? Number(nm.loan_rate.replace(",", ".")) / 100 : null,
        },
      }),
    enabled: Number(nm.monthly) > 0,
    retry: false,
  });
  const b = w.data?.buckets;
  return (
    <div className="grid g-2" style={{ marginTop: 12 }}>
      <Panel title="Withdrawal order" meta="Finnish capital income tax · deterministic">
        {w.isError && <p className="err" style={{ margin: 0 }}>{(w.error as ApiError).message}</p>}
        {b && (
          <p style={{ margin: "0 0 8px", fontSize: 12, color: "var(--fg-2)" }}>
            Starting from cash {money(b.cash ?? 0)}, book-entry {money(b.taxable ?? 0)} (cost {money(b.taxable_cost ?? 0)}), OST and wrappers {money(b.wrapper ?? 0)} (deposits {money(b.wrapper_deposits ?? 0)}).
          </p>
        )}
        {w.data && (
          <div className="tbl-wrap"><table>
            <thead><tr><th>Draw from</th><th className="r">Tax paid</th><th className="r hide-sm">Lasts to</th><th className="r">After tax at {String(params.horizon_age ?? 95)}</th></tr></thead>
            <tbody>{w.data.runs.map((r) => (
              <tr key={r.strategy} style={{ cursor: "default", fontWeight: r.strategy === w.data!.best ? 600 : undefined }}>
                <td>{r.label}{r.strategy === w.data!.best ? " ✓" : ""}</td>
                <td className="r num">{money(r.total_tax)}</td>
                <td className="r num hide-sm">{r.depleted_age ? `age ${r.depleted_age}` : "the end"}</td>
                <td className="r num">{money(r.end_after_tax)}</td>
              </tr>))}</tbody>
          </table></div>
        )}
        {w.data && <p style={{ margin: "8px 0 0", fontSize: 11, color: "var(--muted)" }}>Assumes {w.data.assumptions.join("; ")}.</p>}
      </Panel>
      <Panel title="Where should new money go?" meta={money_.data ? `OST room ${money(money_.data.ost_room)}` : ""}>
        <div className="form-grid">
          <Field label="€ / month" id="nm-m"><input id="nm-m" className="input mono" value={nm.monthly} onChange={(e) => setNm({ ...nm, monthly: e.target.value })} /></Field>
          <Field label="Years" id="nm-y"><input id="nm-y" className="input mono" value={nm.years} onChange={(e) => setNm({ ...nm, years: e.target.value })} /></Field>
          <Field label="Dividend yield %" id="nm-d"><input id="nm-d" className="input mono" value={nm.dividend_yield} onChange={(e) => setNm({ ...nm, dividend_yield: e.target.value })} /></Field>
          <Field label="Mortgage rate % (optional)" id="nm-l"><input id="nm-l" className="input mono" value={nm.loan_rate} onChange={(e) => setNm({ ...nm, loan_rate: e.target.value })} /></Field>
        </div>
        {money_.isError && <p className="err" style={{ margin: 0 }}>{(money_.error as ApiError).message}</p>}
        {money_.data && (
          <div className="list" style={{ marginTop: 8 }}>
            {money_.data.options.map((o, i) => (
              <div key={o.destination} className="li" style={{ cursor: "default", padding: "4px 0" }}>
                <div className="t">{i === 0 ? <b>{o.destination}</b> : o.destination}<small>{o.note}</small></div>
                <span className="num">{money(o.value)}<small style={{ display: "block", color: "var(--muted)", textAlign: "right" }}>{pct(o.value / Math.max(1, o.invested) - 1)}</small></span>
              </div>
            ))}
          </div>
        )}
        <p style={{ margin: "8px 0 0", fontSize: 11, color: "var(--muted)" }}>After-tax value in today's euros if everything is sold at the end, at the scenario's real return. OST can hold listed shares only, not funds or ETFs.</p>
      </Panel>
    </div>
  );
}

export function ExplainPlan({ params, result }: { params: Record<string, unknown>; result: Record<string, unknown> | null }) {
  const [text, setText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const go = async () => {
    setBusy(true);
    try {
      const r = await api<{ explanation: string }>("/api/ai/explain-plan", { body: { params, result, language: navigator.language.startsWith("fi") ? "Finnish" : "English" } });
      setText(r.explanation);
    } catch (e) {
      setText(e instanceof ApiError ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel title="In plain words" meta={<button className="btn" onClick={go} disabled={busy || !result}>{busy ? "Thinking…" : "Explain (local AI)"}</button>} style={{ marginTop: 12 }}>
      {text ? <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{text}</p> : <span style={{ color: "var(--muted)", fontSize: 12 }}>A short explanation of this scenario from your local model, using only the numbers above.</span>}
    </Panel>
  );
}
