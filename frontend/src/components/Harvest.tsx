// Tax-loss harvesting plan: holdings at a loss that can be sold before year end to offset capital income.
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../lib/api";
import { date, money } from "../lib/format";
import { Chip, Empty, Panel, Skeleton } from "./ui";

type Candidate = {
  instrument: string; account: string; loss: number; value: number; lots: number; since: string; years_held: number;
  deducted_now: number; carried_forward: number; tax_saved_now: number; tax_saved_later: number; trading_cost: number; net_now: number;
  resets_deemed_cost_clock: boolean;
};
type Plan = {
  year: number; rate: number; capital_income_so_far: number; gains_so_far: number; losses_so_far: number; carry_in: number;
  small_disposals_exempt: boolean; tax_saved_now: number; candidates: Candidate[]; notes: string[];
};

export function Harvest({ scope }: { scope: string }) {
  const q = useQuery({ queryKey: ["harvest-plan", scope], queryFn: () => api<Plan>(`/api/tax/harvest-plan?scope=${scope}`), retry: false });
  const title = q.data ? `Harvest losses before 31.12.${q.data.year}` : "Harvest losses before year end";
  if (q.isLoading) return <Panel title={title} style={{ marginTop: 12 }}><Skeleton w={240} /></Panel>;
  if (q.isError || !q.data) return <Panel title={title} style={{ marginTop: 12 }}><Empty title="No harvest plan">{(q.error as ApiError | null)?.message ?? "Not available."}</Empty></Panel>;
  const d = q.data;
  const muted = { color: "var(--muted)" };
  return (
    <Panel title={title} style={{ marginTop: 12 }} meta="taxable accounts">
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div className="tiles" style={{ borderRadius: 8, border: "1px solid var(--line)" }}>
          <div className="tile"><span className="label">Capital income so far</span><span className="v">{money(d.capital_income_so_far)}</span>
            <span className="d" style={muted}>gains {money(d.gains_so_far)} · losses {money(d.losses_so_far)}</span></div>
          <div className="tile"><span className="label">Losses carried in</span><span className="v">{money(d.carry_in)}</span>
            <span className="d" style={muted}>from earlier years</span></div>
          <div className="tile"><span className="label">Tax saved now</span><span className="v">{money(d.tax_saved_now)}</span>
            <span className="d" style={muted}>if all are harvested</span></div>
        </div>
        {d.candidates.length === 0 ? <Empty title="No holdings stand at a loss">Nothing to harvest right now.</Empty> : (
          <div className="tbl-wrap"><table>
            <thead><tr><th>Holding</th><th className="r hide-sm">Value</th><th className="r">Loss</th><th className="r hide-sm">Deducted this year</th><th className="r hide-sm">Carried forward</th><th className="r">Tax saved now</th><th className="r hide-sm">Trading cost</th><th className="r">Net now</th></tr></thead>
            <tbody>{d.candidates.map((c, i) => (
              <tr key={`${c.account}${c.instrument}${i}`} style={{ cursor: "default" }}>
                <td><div className="inst"><span>{c.instrument}{c.resets_deemed_cost_clock && <> <Chip tone="warn">10-year clock</Chip></>}</span>
                  <small>{c.account} · {c.lots} lot{c.lots === 1 ? "" : "s"} since {date(c.since)}</small>
                  <small style={muted}>sold at a loss, bought back at once</small></div></td>
                <td className="r num hide-sm">{money(c.value)}</td>
                <td className="r num" style={{ color: "var(--loss)" }}>{money(-Math.abs(c.loss))}</td>
                <td className="r num hide-sm">{money(c.deducted_now)}</td>
                <td className="r num hide-sm">{money(c.carried_forward)}</td>
                <td className="r num">{money(c.tax_saved_now)}</td>
                <td className="r num hide-sm">{money(c.trading_cost)}</td>
                <td className="r num" style={{ color: c.net_now >= 0 ? "var(--gain)" : "var(--loss)" }}>{money(c.net_now)}</td>
              </tr>))}</tbody>
          </table></div>
        )}
        {d.notes.length > 0 && <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>{d.notes.map((t) => <small key={t} style={muted}>{t}</small>)}</div>}
      </div>
    </Panel>
  );
}
