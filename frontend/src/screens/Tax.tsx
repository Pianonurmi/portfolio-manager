// Tax (TAX-01..08): yearly capital income estimate per person, sales with method, OST, harvesting.
import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { Harvest } from "../components/Harvest";
import { Empty, Panel, ScreenHead, Skeleton } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { useCtx } from "../lib/context";
import { date, money, pct, quantity } from "../lib/format";

type Sale = { account: string; instrument: string; sold: string; acquired: string; quantity: string; gross: string; proceeds: string; cost: string; deemed: string; method: string; gain: string; asset_class: string };
type Move = { id: number; date: string; account: string; asset: string; quantity: string; description: string | null };
type Crypto = {
  year: number; proceeds: string; gains: string; losses: string; rewards_total: string;
  by_asset: { asset: string; proceeds: string; cost: string; fees: string; gain: string }[];
  rewards: { date: string; asset: string; eur: string }[];
  unmatched_out: Move[]; unknown_cost_in: Move[]; warnings: string[];
};
type Year = {
  year: number; rules_version: string; sales: Sale[]; sale_proceeds: string; small_disposals_exempt: boolean; losses_deductible: boolean; gains: string; losses: string;
  loss_carry_in: string; loss_used: string; loss_carry_out: string; taxable_gains: string;
  dividends: { gross: string; interest: string; rewards: string; finnish_withholding: string; foreign_withholding: string };
  taxable_dividends: string; crypto_rewards: string; capital_income: string; tax: string; foreign_tax_credit: string; prepaid: string;
  foreign_tax_reclaimable: string; foreign_tax_by_country: { country: string; gross: string; withheld: string; treaty_rate: string; credit: string; reclaimable: string }[]; estimate_due: string;
};

export function Tax() {
  const [ctx] = useCtx();
  const [year, setYear] = useState(new Date().getFullYear());
  const personal = ctx.scope.startsWith("entity:");
  const y = useQuery({ queryKey: ["tax", ctx.scope, year], enabled: personal, queryFn: () => api<Year>(`/api/tax/year?scope=${ctx.scope}&year=${year}`) });
  const ost = useQuery({ queryKey: ["ost", ctx.scope], enabled: personal, queryFn: () => api<Record<string, string | number>[]>(`/api/tax/ost?scope=${ctx.scope}`) });
  const cr = useQuery({ queryKey: ["tax-crypto", ctx.scope, year], enabled: personal, queryFn: () => api<Crypto>(`/api/tax/crypto?scope=${ctx.scope}&year=${year}`) });
  const hv = useQuery({ queryKey: ["harvest", ctx.scope], enabled: personal, queryFn: () => api<Record<string, string>[]>(`/api/tax/harvest?scope=${ctx.scope}`) });
  if (!personal)
    return (<><ScreenHead title="Tax" /><Panel bodyless><Empty title="Choose a person">Taxes are personal. Pick yourself or a child in the context bar at the top.</Empty></Panel></>);
  const d = y.data;
  return (
    <>
      <ScreenHead title="Tax" sub={d ? `Estimate for checking Nordnet's report and Vero's pre-filled return · rules ${d.rules_version}` : ""}
        actions={<div className="seg">{[0, 1, 2, 3].map((k) => { const v = new Date().getFullYear() - k; return <button key={v} aria-pressed={year === v} onClick={() => setYear(v)}>{v}</button>; })}</div>} />
      {y.isError && <p className="err">{(y.error as ApiError).message}</p>}
      {!d ? <Skeleton w={300} /> : (
        <>
          <section className="panel">
            <div className="kpis">
              <div className="kpi"><span className="label">Capital income</span><span className="v mono">{money(d.capital_income)}</span></div>
              <div className="kpi"><span className="label">Tax (30 / 34 %)</span><span className="v mono">{money(d.tax)}</span></div>
              <div className="kpi"><span className="label">Withheld + credit</span><span className="v mono">{money(Number(d.prepaid) + Number(d.foreign_tax_credit))}</span></div>
              <div className="kpi"><span className="label">Estimated to pay</span><span className="v mono">{money(d.estimate_due)}</span></div>
            </div>
            <div className="panel-b">
              <dl className="kv">
                <dt>Sales (gross proceeds)</dt><dd>{money(d.sale_proceeds, "EUR", 2)}{d.small_disposals_exempt ? ` · under the 1 000 € limit: gains tax-free${d.losses_deductible ? ", losses still deductible (costs over 1 000 €)" : ", losses not deductible"}` : ""}</dd>
                <dt>Gains / losses</dt><dd>{money(d.gains, "EUR", 2)} / {money(d.losses, "EUR", 2)}</dd>
                <dt>Losses carried in · used · carried forward</dt><dd>{money(d.loss_carry_in)} · {money(d.loss_used)} · {money(d.loss_carry_out)}</dd>
                <dt>Dividends gross → taxable (85 %)</dt><dd>{money(d.dividends.gross, "EUR", 2)} → {money(d.taxable_dividends, "EUR", 2)}</dd>
                {Number(d.crypto_rewards) > 0 && <><dt>Crypto staking and other rewards</dt><dd>{money(d.crypto_rewards, "EUR", 2)}</dd></>}
                <dt>Finnish withholding (prepayment)</dt><dd>{money(d.dividends.finnish_withholding, "EUR", 2)}</dd>
                <dt>Foreign withholding → credited</dt><dd>{money(d.dividends.foreign_withholding, "EUR", 2)} → {money(d.foreign_tax_credit, "EUR", 2)}</dd>
                {d.foreign_tax_by_country.map((c) => (
                  <Fragment key={c.country}><dt>· {c.country} (treaty {Math.round(Number(c.treaty_rate) * 100)} %)</dt><dd>{money(c.withheld, "EUR", 2)} withheld on {money(c.gross, "EUR", 2)} · credit {money(c.credit, "EUR", 2)}{Number(c.reclaimable) > 0 ? ` · reclaim ${money(c.reclaimable, "EUR", 2)} from ${c.country}` : ""}</dd></Fragment>
                ))}
              </dl>
            </div>
          </section>
          <Panel title={`Sales ${d.year}`} meta="FIFO per account · the better of actual cost and hankintameno-olettama per sale" style={{ marginTop: 12 }} bodyless>
            {d.sales.length === 0 ? <Empty title="No taxable sales">Sales inside the osakesäästötili are not taxed until you withdraw.</Empty> : (
              <div className="tbl-wrap"><table>
                <thead><tr><th>Sold</th><th>Instrument</th><th className="r hide-sm">Qty</th><th className="r">Price</th><th className="r hide-sm">Cost</th><th className="hide-sm">Method</th><th className="r">Gain</th></tr></thead>
                <tbody>{d.sales.map((s, i) => (
                  <tr key={i} style={{ cursor: "default" }}>
                    <td className="num">{date(s.sold)}</td>
                    <td><div className="inst"><span>{s.instrument}</span><small>{s.account} · bought {date(s.acquired)}</small></div></td>
                    <td className="r num hide-sm">{quantity(s.quantity)}</td>
                    <td className="r num">{money(s.gross, "EUR", 2)}</td>
                    <td className="r num hide-sm">{money(s.method === "actual cost" ? s.cost : s.deemed, "EUR", 2)}</td>
                    <td className="hide-sm">{s.method}</td>
                    <td className="r num" style={{ color: Number(s.gain) >= 0 ? "var(--gain)" : "var(--loss)" }}>{money(s.gain, "EUR", 2)}</td>
                  </tr>))}</tbody>
              </table></div>
            )}
          </Panel>
        </>
      )}
      {cr.data && (cr.data.by_asset.length > 0 || cr.data.rewards.length > 0 || cr.data.unmatched_out.length > 0 || cr.data.unknown_cost_in.length > 0) && (
        <CryptoPanel c={cr.data} scope={ctx.scope} />
      )}
      <Harvest scope={ctx.scope} />
      <div className="grid g-2" style={{ marginTop: 12 }}>
        <Panel title="Osakesäästötili">
          {!ost.data?.length ? <span style={{ color: "var(--muted)" }}>No OST in this view</span> : ost.data.map((o, i) => (
            <dl key={i} className="kv">
              <dt>{String(o.account)} value</dt><dd>{money(String(o.value))}</dd>
              <dt>Deposits (tax-free part)</dt><dd>{money(String(o.deposits))}</dd>
              <dt>Gain share of withdrawals</dt><dd>{pct(Number(o.gain_share))}</dd>
              <dt>Tax on 1 000 € withdrawn</dt><dd>~{money(String(o.tax_per_1000_withdrawn))}</dd>
              <dt>Deposit room left</dt><dd>{money(String(o.cap_left))}</dd>
            </dl>
          ))}
        </Panel>
        <Panel title="Tax-loss harvesting" meta="taxable accounts · before 31.12.">
          {!hv.data?.length ? <span style={{ color: "var(--muted)" }}>No lots with unrealised losses</span> : (
            <div className="list">{hv.data.slice(0, 8).map((h, i) => (
              <div key={i} className="li" style={{ cursor: "default", padding: "6px 0" }}><div className="t">{h.instrument}<small>{h.account} · bought {date(h.acquired)} · {quantity(h.quantity ?? "0")}</small></div><span className="num" style={{ color: "var(--loss)" }}>{money(h.unrealised ?? "0")}</span></div>
            ))}</div>
          )}
        </Panel>
      </div>
    </>
  );
}

function CryptoPanel({ c, scope }: { c: Crypto; scope: string }) {
  const issues = c.unmatched_out.length + c.unknown_cost_in.length;
  return (
    <Panel title={`Crypto ${c.year}`} style={{ marginTop: 12 }}
      meta={<a className="btn" href={`/api/tax/crypto.csv?scope=${scope}&year=${c.year}`} download>CSV for OmaVero</a>}>
      <p style={{ margin: "0 0 8px", fontSize: 12, color: "var(--fg-2)" }}>
        Every swap, sale and fee paid in crypto is a disposal. Proceeds {money(c.proceeds, "EUR", 2)} · gains {money(c.gains, "EUR", 2)} · losses {money(c.losses, "EUR", 2)} · rewards {money(c.rewards_total, "EUR", 2)} (capital income when received).
      </p>
      {c.by_asset.length > 0 && (
        <div className="tbl-wrap"><table>
          <thead><tr><th>Asset</th><th className="r">Proceeds</th><th className="r hide-sm">Cost</th><th className="r hide-sm">Fees</th><th className="r">Gain</th></tr></thead>
          <tbody>{c.by_asset.map((a) => (
            <tr key={a.asset} style={{ cursor: "default" }}>
              <td>{a.asset}</td>
              <td className="r num">{money(a.proceeds, "EUR", 2)}</td>
              <td className="r num hide-sm">{money(a.cost, "EUR", 2)}</td>
              <td className="r num hide-sm">{money(a.fees, "EUR", 2)}</td>
              <td className="r num" style={{ color: Number(a.gain) >= 0 ? "var(--gain)" : "var(--loss)" }}>{money(a.gain, "EUR", 2)}</td>
            </tr>))}</tbody>
        </table></div>
      )}
      {issues > 0 && (
        <div style={{ marginTop: 10 }}>
          <p className="err" style={{ margin: "0 0 4px" }}>
            {issues} transfer{issues === 1 ? "" : "s"} without a counterpart. Import the other wallet or exchange so the cost basis can follow the coins; until then deposits count at 0 € cost.
          </p>
          <div className="list">
            {[...c.unmatched_out.map((m) => ({ ...m, dir: "out" })), ...c.unknown_cost_in.map((m) => ({ ...m, dir: "in" }))].slice(0, 10).map((m) => (
              <div key={`${m.dir}${m.id}`} className="li" style={{ cursor: "default", padding: "4px 0" }}>
                <span className="when" style={{ minWidth: 86 }}>{date(m.date)}</span>
                <div className="t">{m.dir === "out" ? "Sent" : "Received"} {quantity(m.quantity)} {m.asset}<small>{m.account}</small></div>
              </div>
            ))}
          </div>
        </div>
      )}
    </Panel>
  );
}
