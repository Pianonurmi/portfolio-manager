// Household (LIFE-01): subscriptions found in the budget, vehicles, and the home maintenance log.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useToast } from "../components/Toasts";
import { Chip, Empty, Field, Panel, ScreenHead } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { date, money } from "../lib/format";
import type { Account } from "../lib/types";

type Sub = { id: number; name: string; amount: string; period: string; yearly: string; next_due: string | null; status: string; source: string; previous_amount: string | null; last_seen: string | null };
type Subs = { items: Sub[]; yearly_total: string; monthly_total: string; candidates: number };
type Log = { id: number; date: string; kind: string; odometer: number | null; cost: string | null; note: string | null };
type Vehicle = {
  id: number; name: string; account_id: number | null; registration: string | null; model: string | null; year: number | null;
  purchase_date: string | null; purchase_price: string | null; depreciation_rate: string; inspection_due: string | null; insurance_renewal: string | null; tax_due: string | null;
  cost_categories: Record<string, string>; sold_on: string | null; notes: string | null; log: Log[];
  costs: { running_costs_12m: string; source: string; km_per_year: number | null; cost_per_km: string | null; total_cost_per_km: string | null; estimated_value: string | null; odometer: number | null };
};
type Item = { id: number; account_id: number | null; title: string; kind: string; done_on: string | null; cost: string | null; adds_to_cost: boolean; interval_months: number | null; next_due: string | null; contractor: string | null; warranty_until: string | null };
type HomeResp = { items: Item[]; due: Item[]; spent_12m: string; improvements_by_property: Record<string, string> };

const PERIOD: Record<string, string> = { weekly: "week", monthly: "month", quarterly: "quarter", yearly: "year" };
const today = () => new Date().toISOString().slice(0, 10);
const soon = (d: string | null, days = 30) => !!d && (new Date(d).getTime() - Date.now()) / 86400000 <= days;

function useErr() {
  const { toast } = useToast();
  return (e: unknown) => toast(e instanceof ApiError ? e.message : "Failed", { error: true });
}

export function Household() {
  return (
    <>
      <ScreenHead title="Household" sub="Subscriptions, vehicles and the home: what it costs and what is due" />
      <Subscriptions />
      <div className="grid g-2" style={{ marginTop: 12 }}>
        <Vehicles />
        <HomeLog />
      </div>
    </>
  );
}

function Subscriptions() {
  const qc = useQueryClient();
  const err = useErr();
  const { toast } = useToast();
  const s = useQuery({ queryKey: ["subscriptions"], queryFn: () => api<Subs>("/api/subscriptions") });
  const [f, setF] = useState({ name: "", amount: "", period: "monthly", next_due: "" });
  const refresh = () => (qc.invalidateQueries({ queryKey: ["subscriptions"] }), qc.invalidateQueries({ queryKey: ["attention"] }));
  const patch = async (id: number, body: Record<string, unknown>) => { try { await api(`/api/subscriptions/${id}`, { method: "PATCH", body }); refresh(); } catch (e) { err(e); } };
  const detect = async () => { try { const r = await api<{ added: number }>("/api/subscriptions/detect", { method: "POST" }); refresh(); toast(r.added ? `${r.added} recurring payments found` : "No new recurring payments found"); } catch (e) { err(e); } };
  const add = async (e: FormEvent) => {
    e.preventDefault();
    try { await api("/api/subscriptions", { body: { ...f, amount: f.amount.replace(",", "."), next_due: f.next_due || null } }); setF({ ...f, name: "", amount: "" }); refresh(); } catch (x) { err(x); }
  };
  const d = s.data;
  const candidates = d?.items.filter((x) => x.status === "candidate") ?? [];
  const active = d?.items.filter((x) => x.status !== "candidate") ?? [];
  return (
    <Panel title="Subscriptions" meta={<>{d && <span>{money(d.monthly_total)}/month · {money(d.yearly_total)}/year</span>}<button className="btn" onClick={detect}>Find in budget</button></>}>
      {candidates.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <span className="label">Found in your budget: confirm or ignore</span>
          <div className="list">{candidates.map((x) => (
            <div key={x.id} className="li" style={{ cursor: "default", padding: "4px 0" }}>
              <div className="t">{x.name}<small>{money(x.amount, "EUR", 2)} a {PERIOD[x.period]} · last {x.last_seen ? date(x.last_seen) : "—"}</small></div>
              <span style={{ display: "flex", gap: 6 }}>
                <button className="btn primary" onClick={() => patch(x.id, { status: "active" })}>Track</button>
                <button className="btn" onClick={() => patch(x.id, { status: "ignored" })}>Not a subscription</button>
              </span>
            </div>))}</div>
        </div>
      )}
      {active.length === 0 && candidates.length === 0 ? (
        <Empty title="No subscriptions yet">“Find in budget” looks for payments to the same payee at a steady interval in your YNAB or Actual data. You can also add them by hand.</Empty>
      ) : (
        <div className="tbl-wrap"><table>
          <thead><tr><th>Subscription</th><th className="r">Amount</th><th className="r hide-sm">Per year</th><th className="hide-sm">Next</th><th /></tr></thead>
          <tbody>{active.map((x) => (
            <tr key={x.id} style={{ cursor: "default", opacity: x.status === "cancelled" ? 0.55 : 1 }}>
              <td><div className="inst"><span>{x.name}</span><small>{x.status === "cancelled" ? "cancelled" : x.source}{x.previous_amount && Number(x.amount) > Number(x.previous_amount) ? ` · was ${money(x.previous_amount, "EUR", 2)}` : ""}</small></div></td>
              <td className="r num">{money(x.amount, "EUR", 2)}<small style={{ color: "var(--muted)" }}> /{PERIOD[x.period]}</small></td>
              <td className="r num hide-sm">{money(x.yearly)}</td>
              <td className="hide-sm">{x.next_due ? <span style={{ color: soon(x.next_due, 14) ? "var(--warn)" : undefined }}>{date(x.next_due)}</span> : "—"}</td>
              <td className="r">{x.status === "active"
                ? <button className="btn" onClick={() => patch(x.id, { status: "cancelled" })}>Mark cancelled</button>
                : <button className="btn" onClick={() => patch(x.id, { status: "active" })}>Restore</button>}</td>
            </tr>))}</tbody>
        </table></div>
      )}
      <details style={{ marginTop: 10 }}><summary className="add-summary">Add a subscription by hand</summary>
      <form onSubmit={add} className="form-grid" style={{ marginTop: 8 }}>
        <Field label="Name" id="sb-n"><input id="sb-n" className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required /></Field>
        <Field label="Amount €" id="sb-a"><input id="sb-a" className="input mono" value={f.amount} onChange={(e) => setF({ ...f, amount: e.target.value })} required /></Field>
        <Field label="Every" id="sb-p"><select id="sb-p" className="select input" value={f.period} onChange={(e) => setF({ ...f, period: e.target.value })}>{Object.entries(PERIOD).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></Field>
        <Field label="Next payment" id="sb-d"><input id="sb-d" className="input" type="date" value={f.next_due} onChange={(e) => setF({ ...f, next_due: e.target.value })} /></Field>
        <div className="form-actions" style={{ gridColumn: "1 / -1" }}><button className="btn">Add subscription</button></div>
      </form></details>
    </Panel>
  );
}

function Vehicles() {
  const qc = useQueryClient();
  const err = useErr();
  const { toast } = useToast();
  const v = useQuery({ queryKey: ["vehicles"], queryFn: () => api<Vehicle[]>("/api/vehicles") });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/api/accounts") });
  const [f, setF] = useState({ name: "", registration: "", purchase_date: "", purchase_price: "", inspection_due: "", account_id: "" });
  const refresh = () => (qc.invalidateQueries({ queryKey: ["vehicles"] }), qc.invalidateQueries({ queryKey: ["attention"] }));
  const add = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await api("/api/vehicles", { body: { ...Object.fromEntries(Object.entries(f).map(([k, x]) => [k, x || null])), name: f.name, account_id: f.account_id ? Number(f.account_id) : null } });
      setF({ name: "", registration: "", purchase_date: "", purchase_price: "", inspection_due: "", account_id: "" });
      refresh();
    } catch (x) { err(x); }
  };
  const value = async (id: number) => {
    try { const r = await api<{ value: string; undo_id: number }>(`/api/vehicles/${id}/valuation`, { method: "POST" }); toast(`Valuation ${money(r.value)} added`, { undoId: r.undo_id }); qc.invalidateQueries({ queryKey: ["networth"] }); } catch (x) { err(x); }
  };
  return (
    <Panel title="Vehicles">
      {v.data?.length === 0 && <span style={{ color: "var(--muted)" }}>No vehicles yet.</span>}
      {v.data?.map((x) => (
        <div key={x.id} style={{ borderBottom: "1px solid var(--line)", paddingBottom: 10, marginBottom: 10 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <b>{x.name}</b>{x.registration && <Chip>{x.registration}</Chip>}
            {(["inspection_due", "insurance_renewal", "tax_due"] as const).map((k) => x[k] && (
              <Chip key={k} tone={soon(x[k], 0) ? "bad" : soon(x[k]) ? "warn" : undefined}>{{ inspection_due: "katsastus", insurance_renewal: "insurance", tax_due: "vehicle tax" }[k]} {date(x[k]!)}</Chip>
            ))}
          </div>
          <dl className="kv" style={{ marginTop: 6 }}>
            {x.costs.estimated_value && (<><dt>Estimated value</dt><dd>{money(x.costs.estimated_value)}</dd></>)}
            <dt>Running costs, 12 months</dt><dd>{money(x.costs.running_costs_12m)} <small style={{ color: "var(--muted)" }}>({x.costs.source})</small></dd>
            {x.costs.km_per_year && (<><dt>Driving</dt><dd>{x.costs.km_per_year.toLocaleString("fi-FI")} km/year</dd></>)}
            {x.costs.cost_per_km && (<><dt>Cost per km</dt><dd>{money(x.costs.cost_per_km, "EUR", 2)}{x.costs.total_cost_per_km ? ` · ${money(x.costs.total_cost_per_km, "EUR", 2)} with value loss` : ""}</dd></>)}
          </dl>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 6 }}>
            {x.costs.estimated_value && x.account_id && <button className="btn" onClick={() => value(x.id)}>Book value to net worth</button>}
          </div>
          <VehicleLogForm v={x} onDone={refresh} />
        </div>
      ))}
      <details open={v.data?.length === 0}><summary className="add-summary">Add a vehicle</summary>
      <form onSubmit={add} className="form-grid" style={{ marginTop: 8 }}>
        <Field label="Vehicle" id="vh-n"><input id="vh-n" className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required /></Field>
        <Field label="Registration" id="vh-r"><input id="vh-r" className="input mono" value={f.registration} onChange={(e) => setF({ ...f, registration: e.target.value })} /></Field>
        <Field label="Bought" id="vh-d"><input id="vh-d" className="input" type="date" value={f.purchase_date} onChange={(e) => setF({ ...f, purchase_date: e.target.value })} /></Field>
        <Field label="Price €" id="vh-p"><input id="vh-p" className="input mono" value={f.purchase_price} onChange={(e) => setF({ ...f, purchase_price: e.target.value })} /></Field>
        <Field label="Next katsastus" id="vh-i"><input id="vh-i" className="input" type="date" value={f.inspection_due} onChange={(e) => setF({ ...f, inspection_due: e.target.value })} /></Field>
        <Field label="Net-worth account" id="vh-a" hint="An account of type Vehicle">
          <select id="vh-a" className="select input" value={f.account_id} onChange={(e) => setF({ ...f, account_id: e.target.value })}>
            <option value="">None</option>
            {accounts.data?.filter((a) => a.kind === "vehicle").map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </Field>
        <div className="form-actions" style={{ gridColumn: "1 / -1" }}><button className="btn">Add vehicle</button></div>
      </form></details>
    </Panel>
  );
}

function VehicleLogForm({ v, onDone }: { v: Vehicle; onDone: () => void }) {
  const err = useErr();
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ date: today(), kind: "service", odometer: "", cost: "", note: "", next_inspection: "" });
  if (!open) return <button className="btn" onClick={() => setOpen(true)}>Log service, fuel or odometer…</button>;
  const save = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await api(`/api/vehicles/${v.id}/log`, { body: { ...f, odometer: f.odometer ? Number(f.odometer) : null, cost: f.cost ? f.cost.replace(",", ".") : null, note: f.note || null, next_inspection: f.next_inspection || null } });
      setOpen(false);
      onDone();
    } catch (x) { err(x); }
  };
  return (
    <form onSubmit={save} className="form-grid">
      <Field label="Date" id={`vl-d${v.id}`}><input id={`vl-d${v.id}`} className="input" type="date" value={f.date} onChange={(e) => setF({ ...f, date: e.target.value })} /></Field>
      <Field label="What" id={`vl-k${v.id}`}><select id={`vl-k${v.id}`} className="select input" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>{["service", "repair", "inspection", "tyres", "fuel", "other"].map((k) => <option key={k}>{k}</option>)}</select></Field>
      <Field label="Odometer km" id={`vl-o${v.id}`}><input id={`vl-o${v.id}`} className="input mono" value={f.odometer} onChange={(e) => setF({ ...f, odometer: e.target.value })} /></Field>
      <Field label="Cost €" id={`vl-c${v.id}`}><input id={`vl-c${v.id}`} className="input mono" value={f.cost} onChange={(e) => setF({ ...f, cost: e.target.value })} /></Field>
      {f.kind === "inspection" && <Field label="Next katsastus" id={`vl-n${v.id}`}><input id={`vl-n${v.id}`} className="input" type="date" value={f.next_inspection} onChange={(e) => setF({ ...f, next_inspection: e.target.value })} /></Field>}
      <div className="form-actions" style={{ gridColumn: "1 / -1" }}><button type="button" className="btn" onClick={() => setOpen(false)}>Cancel</button><button className="btn primary">Save</button></div>
    </form>
  );
}

function HomeLog() {
  const qc = useQueryClient();
  const err = useErr();
  const h = useQuery({ queryKey: ["home"], queryFn: () => api<HomeResp>("/api/home") });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/api/accounts") });
  const props = accounts.data?.filter((a) => a.kind === "property") ?? [];
  const [f, setF] = useState({ title: "", kind: "repair", done_on: today(), cost: "", interval_months: "", adds_to_cost: false, account_id: "" });
  const refresh = () => (qc.invalidateQueries({ queryKey: ["home"] }), qc.invalidateQueries({ queryKey: ["attention"] }));
  const add = async (e: FormEvent) => {
    e.preventDefault();
    const account = f.account_id || (props.length === 1 ? String(props[0]!.id) : "");
    try {
      await api("/api/home", { body: { ...f, account_id: account ? Number(account) : null, cost: f.cost ? f.cost.replace(",", ".") : null, interval_months: f.interval_months ? Number(f.interval_months) : null, done_on: f.done_on || null } });
      setF({ ...f, title: "", cost: "", interval_months: "", adds_to_cost: false });
      refresh();
    } catch (x) { err(x); }
  };
  const done = async (id: number) => { try { await api(`/api/home/${id}/done`, { body: { done_on: today() } }); refresh(); } catch (x) { err(x); } };
  const d = h.data;
  const names = Object.fromEntries(props.map((a) => [String(a.id), a.name]));
  return (
    <Panel title="Home maintenance" meta={d ? `spent ${money(d.spent_12m)} in 12 months` : ""}>
      {d && d.due.length > 0 && (
        <div className="list" style={{ marginBottom: 8 }}>
          {d.due.map((x) => (
            <div key={x.id} className="li" style={{ cursor: "default", padding: "4px 0" }}>
              <div className="t">{x.title}<small style={{ color: soon(x.next_due, 0) ? "var(--loss)" : "var(--warn)" }}>due {date(x.next_due!)}</small></div>
              <button className="btn" onClick={() => done(x.id)}>Done today</button>
            </div>
          ))}
        </div>
      )}
      {d && Object.keys(d.improvements_by_property).length > 0 && (
        <p style={{ margin: "0 0 8px", fontSize: 12, color: "var(--fg-2)" }}>
          Improvements that add to the acquisition cost when selling: {Object.entries(d.improvements_by_property).map(([k, x]) => `${names[k] ?? "home"} ${money(x)}`).join(" · ")}
        </p>
      )}
      <div className="list">
        {d?.items.filter((x) => x.done_on).slice(0, 12).map((x) => (
          <div key={x.id} className="li" style={{ cursor: "default", padding: "4px 0" }}>
            <span className="when" style={{ minWidth: 86 }}>{date(x.done_on!)}</span>
            <div className="t">{x.title}<small>{x.kind}{x.contractor ? ` · ${x.contractor}` : ""}{x.adds_to_cost ? " · improvement" : ""}</small></div>
            <span className="num">{x.cost ? money(x.cost) : ""}</span>
          </div>
        ))}
      </div>
      <details style={{ marginTop: 10 }} open={d?.items.length === 0}><summary className="add-summary">Log work or add a recurring task</summary>
      <form onSubmit={add} className="form-grid" style={{ marginTop: 8 }}>
        <Field label="What" id="hm-t"><input id="hm-t" className="input" value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} required placeholder="e.g. Ilmanvaihdon suodattimet" /></Field>
        <Field label="Kind" id="hm-k"><select id="hm-k" className="select input" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>{["repair", "renovation", "service", "inspection", "purchase", "task"].map((k) => <option key={k}>{k}</option>)}</select></Field>
        <Field label="Done on" id="hm-d"><input id="hm-d" className="input" type="date" value={f.done_on} onChange={(e) => setF({ ...f, done_on: e.target.value })} /></Field>
        <Field label="Cost €" id="hm-c"><input id="hm-c" className="input mono" value={f.cost} onChange={(e) => setF({ ...f, cost: e.target.value })} /></Field>
        <Field label="Repeat every (months)" id="hm-i"><input id="hm-i" className="input mono" value={f.interval_months} onChange={(e) => setF({ ...f, interval_months: e.target.value })} /></Field>
        {props.length > 1 && (
          <Field label="Home" id="hm-a"><select id="hm-a" className="select input" value={f.account_id} onChange={(e) => setF({ ...f, account_id: e.target.value })}><option value="">—</option>{props.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</select></Field>
        )}
        <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12, gridColumn: "1 / -1" }}>
          <input type="checkbox" checked={f.adds_to_cost} onChange={(e) => setF({ ...f, adds_to_cost: e.target.checked })} />
          Improvement (perusparannus) paid by me: counts toward the acquisition cost when the home is sold
        </label>
        <div className="form-actions" style={{ gridColumn: "1 / -1" }}><button className="btn">Add to log</button></div>
      </form></details>
    </Panel>
  );
}
