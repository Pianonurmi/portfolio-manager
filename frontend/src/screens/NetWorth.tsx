// Net worth (NW-01..04, RE-01..06): balance sheet, change decomposition, liquidity, history, account details.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useLocation } from "react-router";
import { DocsPanel } from "../components/DocsPanel";
import { Figure } from "../components/Figure";
import { HousingCosts, PropertyIndex } from "../components/PropertyTools";
import { Legend, LineChart, useCssVar } from "../components/LineChart";
import { useToast } from "../components/Toasts";
import { Empty, Field, Modal, Panel, ScreenHead, Skeleton } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { useCtx } from "../lib/context";
import { date, decimal, money, num, pct, signedMoney } from "../lib/format";
import { ANALYTICS_KEYS, useNetworth, useNwChange, useNwHistory, type NwItem } from "../lib/queries";

const VALUED_KINDS = new Set(["property", "housing_loan", "mortgage", "loan", "credit_card", "savings", "bank", "vehicle", "manual_asset", "manual_liability", "ps", "kapitalisaatio"]);

export function NetWorth() {
  const [ctx] = useCtx();
  const loc = useLocation();
  const nw = useNetworth(ctx);
  const hist = useNwHistory(ctx);
  const ch = useNwChange(ctx);
  const c1 = useCssVar("--c1");
  const c3 = useCssVar("--c3");
  const c4 = useCssVar("--c4");
  const [open, setOpen] = useState<NwItem | null>(null);
  const [spendEdit, setSpendEdit] = useState(false);
  const d = nw.data;
  if (d && d.groups.length === 0)
    return (
      <>
        <ScreenHead title="Net worth" />
        <Panel bodyless>
          <Empty title="Nothing on the balance sheet yet" action={<Link className="btn primary" to={{ pathname: "/settings/accounts", search: loc.search + (loc.search ? "&" : "?") + "new=1" }}>Add an account</Link>}>
            Add your home, mortgage, housing-company loan share, savings and credit cards, then give each a value.
          </Empty>
        </Panel>
      </>
    );
  const parts: [string, keyof NonNullable<typeof ch.data>][] = [
    ["Contributions", "contributions"],
    ["Market returns", "market"],
    ["Debt paydown", "debt_paydown"],
    ["Revaluation", "revaluation"],
  ];
  const maxAbs = Math.max(1, ...parts.map(([, k]) => Math.abs(num((ch.data?.[k] as { value: string } | undefined)?.value ?? 0))));
  const series = hist.data?.series ?? [];
  const showProperty = series.some((r) => num(r.property) !== 0);
  return (
    <>
      <ScreenHead title="Net worth" sub={d ? `Joint items at ownership share · as of ${date(d.as_of)}` : ""} />
      <div className="grid g-2">
        <Panel title="Balance sheet" meta={d ? <Figure f={d.net_worth} /> : <Skeleton />} bodyless>
          <div className="tbl-wrap">
            <table>
              <tbody>
                {d?.groups.map((g) => (
                  <GroupBlock key={g.id} g={g} onOpen={setOpen} />
                ))}
                {d && (
                  <tr className="total">
                    <td>Net worth</td>
                    <td className="r"><Figure f={d.net_worth} /></td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Panel>
        <div className="grid" style={{ alignContent: "start" }}>
          <Panel title={`What changed · ${ctx.period}`} meta={ch.data ? <Figure f={ch.data.total} delta /> : null}>
            <div className="wf">
              {parts.map(([label, k]) => {
                const v = num((ch.data?.[k] as { value: string } | undefined)?.value ?? 0);
                const w = (Math.abs(v) / maxAbs) * 50;
                return (
                  <div className="wf-row" key={k}>
                    <span>{label}</span>
                    <div className="wf-track">
                      <span className="wf-bar" style={{ left: `${v >= 0 ? 50 : 50 - w}%`, width: `${w}%`, background: v >= 0 ? "var(--c1)" : "var(--c2)" }} />
                      <span style={{ position: "absolute", left: "50%", top: -2, bottom: -2, width: 1, background: "var(--line)" }} />
                    </div>
                    <span className="num" style={{ textAlign: "right" }}>{ch.data ? <Figure f={ch.data[k] as never} delta /> : ""}</span>
                  </div>
                );
              })}
            </div>
          </Panel>
          <Panel title="Liquidity" meta={<button className="btn" onClick={() => setSpendEdit(true)}>Monthly spending</button>}>
            <dl className="kv">
              <dt>Liquid (investments + cash)</dt>
              <dd><Figure f={d?.liquid} /></dd>
              <dt>Illiquid (property, other)</dt>
              <dd><Figure f={d?.illiquid} /></dd>
              <dt>Home equity</dt>
              <dd><Figure f={d?.home_equity} /></dd>
              <dt>Emergency fund</dt>
              <dd>{d?.emergency_months == null ? "set monthly spending" : `${decimal(d.emergency_months, 1)} months`}</dd>
              <dt>Liquid coverage</dt>
              <dd>{d?.liquid_months == null ? "—" : `${decimal(d.liquid_months, 0)} months`}</dd>
            </dl>
            {d?.emergency_months != null && (
              <>
                <div className="meter" aria-label="Emergency fund, target 6 months">
                  <span style={{ width: `${Math.min(100, (d.emergency_months / 6) * 100)}%`, background: "var(--c3)" }} />
                </div>
                <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 4 }}>Target 6 months · spending {money(d.monthly_spending ?? 0)}/month (from YNAB once connected)</div>
              </>
            )}
          </Panel>
        </div>
      </div>
      <Panel
        title="History"
        style={{ marginTop: 12 }}
        meta={<Legend items={[{ name: "Net worth", color: c1 }, { name: "Investments", color: c3 }, ...(showProperty ? [{ name: "Property (share)", color: c4 }] : [])]} />}
      >
        <LineChart
          label="Net worth history"
          x={series.map((r) => r.date)}
          series={[
            { name: "Net worth", values: series.map((r) => num(r.net_worth)), color: c1, area: true },
            { name: "Investments", values: series.map((r) => num(r.investments)), color: c3 },
            ...(showProperty ? [{ name: "Property (share)", values: series.map((r) => num(r.property)), color: c4 }] : []),
          ]}
          height={230}
        />
      </Panel>
      {open && <AccountSheet item={open} onClose={() => setOpen(null)} />}
      {spendEdit && <SpendingForm current={d?.monthly_spending ?? ""} onClose={() => setSpendEdit(false)} />}
    </>
  );
}

function GroupBlock({ g, onOpen }: { g: { id: string; label: string; total: import("../lib/types").Figure; items: NwItem[] }; onOpen: (i: NwItem) => void }) {
  return (
    <>
      <tr className="group">
        <td><b>{g.label}</b></td>
        <td className="r"><Figure f={g.total} /></td>
      </tr>
      {g.items.map((i) => (
        <tr key={i.account_id} onClick={() => VALUED_KINDS.has(i.kind) && onOpen(i)} style={{ cursor: VALUED_KINDS.has(i.kind) ? "pointer" : "default" }}>
          <td>
            <div className="inst">
              <span>
                {i.name}
                {num(i.share) !== 1 && <span style={{ color: "var(--muted)" }}> · {pct(num(i.share), 0)}</span>}
              </span>
              <small>
                {i.kind_label}
                {i.method !== "ledger" && i.as_of ? ` · ${i.method === "estimate" ? "estimated from statement" : i.method} ${date(i.as_of)}` : ""}
                {i.method === "none" ? " · add a valuation" : ""}
              </small>
            </div>
          </td>
          <td className="r"><Figure f={i.value} /></td>
        </tr>
      ))}
    </>
  );
}

function SpendingForm({ current, onClose }: { current: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [v, setV] = useState(current);
  const m = useMutation({
    mutationFn: () => api("/api/networth/settings", { method: "PUT", body: { monthly_spending: v || null } }),
    onSuccess: () => (qc.invalidateQueries({ queryKey: ["networth"] }), onClose()),
  });
  return (
    <Modal title="Monthly spending" onClose={onClose}>
      <form onSubmit={(e) => (e.preventDefault(), m.mutate())} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <Field label="Average household spending per month (€)" id="sp" hint="Used for emergency-fund months and planning. Replaced by YNAB averages once connected.">
          <input id="sp" className="input mono" inputMode="decimal" value={v} onChange={(e) => setV(e.target.value.replace(",", "."))} />
        </Field>
        <div className="form-actions">
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary">Save</button>
        </div>
      </form>
    </Modal>
  );
}

type Valuation = { id: number; date: string; value: string; method: string; note: string | null; document_ref: string | null };
type Schedule = {
  ready: boolean;
  missing?: string[];
  rate?: string;
  kind?: string;
  end_date?: string;
  next_payment?: { date: string; payment: string; interest: string; principal: string } | null;
  interest_next_12m?: string;
  next_reset?: string | null;
  rows?: { date: string; payment: string; interest: string; principal: string; balance: string }[];
};

function AccountSheet({ item, onClose }: { item: NwItem; onClose: () => void }) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const vals = useQuery({ queryKey: ["valuations", item.account_id], queryFn: () => api<Valuation[]>(`/api/valuations?account_id=${item.account_id}`) });
  const meta = useQuery({ queryKey: ["meta", item.account_id], queryFn: () => api<Record<string, string>>(`/api/accounts/${item.account_id}/meta`) });
  const isLoan = ["mortgage", "loan", "housing_loan"].includes(item.kind);
  const sched = useQuery({ queryKey: ["schedule", item.account_id], queryFn: () => api<Schedule>(`/api/loans/${item.account_id}/schedule`), enabled: isLoan });
  const today = new Date().toISOString().slice(0, 10);
  const [f, setF] = useState({ date: today, value: "", method: isLoan || item.kind === "savings" ? "statement" : "manual", note: "" });
  const refresh = () => {
    for (const k of [...ANALYTICS_KEYS, "valuations", "schedule", "meta"]) qc.invalidateQueries({ queryKey: [k] });
  };
  const add = useMutation({
    mutationFn: () => api<{ undo_id: number }>("/api/valuations", { body: { account_id: item.account_id, ...f, value: f.value.replace(/\s/g, "").replace(",", "."), note: f.note || null } }),
    onSuccess: (r) => (refresh(), toast("Valuation added", { undoId: r.undo_id, onUndo: refresh }), setF({ ...f, value: "", note: "" })),
    onError: (e) => toast(e instanceof ApiError ? e.message : "Failed", { error: true }),
  });
  const del = async (id: number) => {
    const r = await api<{ undo_id: number }>(`/api/valuations/${id}`, { method: "DELETE" });
    refresh();
    toast("Valuation deleted", { undoId: r.undo_id, onUndo: refresh });
  };
  const [m, setM] = useState<Record<string, string> | null>(null);
  const mm = m ?? meta.data ?? {};
  const saveMeta = useMutation({
    mutationFn: () => api(`/api/accounts/${item.account_id}/meta`, { method: "PUT", body: { meta: mm } }),
    onSuccess: () => (refresh(), toast("Saved"), setM(null)),
  });
  const metaField = (k: string, label: string, hint?: string, type = "text") => (
    <Field label={label} id={`m-${k}`} hint={hint}>
      <input id={`m-${k}`} className="input mono" type={type} value={mm[k] ?? ""} onChange={(e) => setM({ ...mm, [k]: e.target.value })} />
    </Field>
  );
  const submit = (e: FormEvent) => (e.preventDefault(), add.mutate());
  return (
    <Modal title={item.name} onClose={onClose}>
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <form onSubmit={submit} className="form-grid" style={{ alignItems: "end" }}>
          <Field label={isLoan ? "Outstanding balance (100 %)" : "Value (100 %)"} id="v-val">
            <input id="v-val" className="input mono" inputMode="decimal" value={f.value} onChange={(e) => setF({ ...f, value: e.target.value })} required />
          </Field>
          <Field label="Date" id="v-date">
            <input id="v-date" className="input" type="date" value={f.date} onChange={(e) => setF({ ...f, date: e.target.value })} />
          </Field>
          <Field label="Source" id="v-m">
            <select id="v-m" className="select input" value={f.method} onChange={(e) => setF({ ...f, method: e.target.value })}>
              <option value="statement">Statement</option>
              <option value="manual">Own estimate</option>
              <option value="index">Price index</option>
              <option value="purchase">Purchase price</option>
            </select>
          </Field>
          <Field label="Note" id="v-note">
            <input id="v-note" className="input" value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} placeholder="e.g. isännöitsijätodistus 2026" />
          </Field>
          <div className="form-actions" style={{ gridColumn: "1 / -1" }}>
            <button className="btn primary" disabled={add.isPending}>Add valuation</button>
          </div>
        </form>
        <div className="tbl-wrap" style={{ maxHeight: 200, overflowY: "auto" }}>
          <table>
            <tbody>
              {vals.data?.map((v) => (
                <tr key={v.id} style={{ cursor: "default" }}>
                  <td className="num">{date(v.date)}</td>
                  <td>{v.method}{v.note ? ` · ${v.note}` : ""}</td>
                  <td className="r num">{money(v.value)}</td>
                  <td className="r"><button className="x" aria-label="Delete valuation" onClick={() => del(v.id)}>×</button></td>
                </tr>
              ))}
              {vals.data?.length === 0 && (
                <tr><td style={{ color: "var(--muted)" }}>No valuations yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
        {item.kind === "property" && (
          <form onSubmit={(e) => (e.preventDefault(), saveMeta.mutate())} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <span className="label">Property details</span>
            <div className="form-grid">
              {metaField("purchase_price", "Purchase price (100 %)")}
              {metaField("purchase_date", "Purchase date", undefined, "date")}
              {metaField("transfer_tax", "Transfer tax paid", "Varainsiirtovero, part of acquisition cost")}
              {metaField("postal_code", "Postal code", "For the postal-code price series (Statistics Finland)")}
              {metaField("area_m2", "Floor area m²")}
              {metaField("housing_company", "Housing company")}
            </div>
            <div className="form-actions"><button className="btn">Save details</button></div>
          </form>
        )}
        {isLoan && (
          <form onSubmit={(e) => (e.preventDefault(), saveMeta.mutate())} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <span className="label">Loan terms</span>
            <div className="form-grid">
              {metaField("reference_rate", "Reference rate (e.g. 0.0225)", "12 kk Euribor at the last reset")}
              {metaField("margin", "Margin (e.g. 0.0055)")}
              <Field label="Repayment" id="m-pt">
                <select id="m-pt" className="select input" value={mm.payment_type ?? "annuity"} onChange={(e) => setM({ ...mm, payment_type: e.target.value })}>
                  <option value="annuity">Annuity (annuiteetti)</option>
                  <option value="equal_principal">Equal principal (tasalyhennys)</option>
                  <option value="fixed">Fixed payment (kiinteä tasaerä)</option>
                </select>
              </Field>
              {mm.payment_type === "fixed" && metaField("fixed_payment", "Monthly payment €")}
              {metaField("end_date", "Final payment date", undefined, "date")}
              {metaField("next_reset", "Next rate reset", undefined, "date")}
            </div>
            <div className="form-actions"><button className="btn">Save terms</button></div>
          </form>
        )}
        {isLoan && sched.data && (
          sched.data.ready ? (
            <dl className="kv">
              <dt>Rate now</dt>
              <dd>{pct(num(sched.data.rate ?? 0), 2)}</dd>
              <dt>Next payment</dt>
              <dd>{sched.data.next_payment ? `${date(sched.data.next_payment.date)} · ${money(sched.data.next_payment.payment, "EUR", 2)} (interest ${money(sched.data.next_payment.interest, "EUR", 2)})` : "—"}</dd>
              <dt>Interest, next 12 months</dt>
              <dd>{money(sched.data.interest_next_12m ?? 0)}</dd>
              <dt>Paid off</dt>
              <dd>{sched.data.rows?.length ? date(sched.data.rows[sched.data.rows.length - 1]!.date) : "—"}</dd>
              <dt>Payments left</dt>
              <dd>{sched.data.rows?.length ?? 0}</dd>
            </dl>
          ) : (
            <p style={{ margin: 0, color: "var(--muted)", fontSize: 12 }}>Add {sched.data.missing?.join(" and ")} to see the repayment schedule and an up-to-date balance estimate.</p>
          )
        )}
        {isLoan && sched.data?.ready && <LoanScenario accountId={item.account_id} />}
        <DocsPanel subjectType="account" subjectId={item.account_id} />
        {item.kind === "property" && meta.data && <PropertyIndex accountId={item.account_id} meta={meta.data} onDone={refresh} />}
        {item.kind === "property" && <HousingCosts accountId={item.account_id} />}
        {item.kind === "property" && (
          <details>
            <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--fg-2)" }}>Enter index values by hand</summary>
            <IndexEstimate accountId={item.account_id} onDone={refresh} />
          </details>
        )}
        <p style={{ margin: 0, fontSize: 12, color: "var(--muted)" }}>Enter 100 % values; your share ({pct(num(item.share), 0)}) is applied automatically. Current value in this view: {signedMoney(item.value.value)}.</p>
      </div>
    </Modal>
  );
}


function LoanScenario({ accountId }: { accountId: number }) {
  const [delta, setDelta] = useState("0");
  const [extra, setExtra] = useState("0");
  const r = useQuery({
    queryKey: ["loan-sim", accountId, delta, extra],
    queryFn: () => api<{ base: { months: number; paid_off: string; total_interest: string }; scenario: { months: number; paid_off: string; total_interest: string; first_payment: string }; interest_saved: string }>(
      `/api/loans/${accountId}/simulate`, { body: { rate_delta: String(num(delta.replace(",", ".")) / 100), extra_monthly: extra.replace(",", ".") || "0" } }),
  });
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <span className="label">What if</span>
      <div className="form-grid">
        <Field label="Rate change (percentage points)" id="ls-d"><input id="ls-d" className="input mono" value={delta} onChange={(e) => setDelta(e.target.value)} /></Field>
        <Field label="Extra repayment € / month" id="ls-x"><input id="ls-x" className="input mono" value={extra} onChange={(e) => setExtra(e.target.value)} /></Field>
      </div>
      {r.data && (
        <dl className="kv">
          <dt>Paid off</dt><dd>{date(r.data.scenario.paid_off)} (now {date(r.data.base.paid_off)})</dd>
          <dt>Total interest</dt><dd>{money(r.data.scenario.total_interest)} (now {money(r.data.base.total_interest)})</dd>
          <dt>Monthly payment</dt><dd>{money(r.data.scenario.first_payment, "EUR", 2)}</dd>
          <dt>Interest saved</dt><dd>{signedMoney(r.data.interest_saved)}</dd>
        </dl>
      )}
    </div>
  );
}

function IndexEstimate({ accountId, onDone }: { accountId: number; onDone: () => void }) {
  const [f, setF] = useState({ base_date: "", base_value: "", index_base: "", index_now: "" });
  const { toast } = useToast();
  const go = async () => {
    try {
      const r = await api<{ value: string; undo_id: number }>("/api/valuations/index-estimate", { body: { account_id: accountId, ...f } });
      toast(`Index estimate ${money(r.value)} added`, { undoId: r.undo_id, onUndo: onDone });
      onDone();
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed", { error: true });
    }
  };
  return (
    <form onSubmit={(e) => (e.preventDefault(), go())} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <span className="label">Revalue with a price index</span>
      <p style={{ margin: 0, fontSize: 12, color: "var(--muted)" }}>For any other index: enter its value at the base date and now.</p>
      <div className="form-grid">
        <Field label="Base date" id="ix-d"><input id="ix-d" className="input" type="date" value={f.base_date} onChange={(e) => setF({ ...f, base_date: e.target.value })} required /></Field>
        <Field label="Value then (100 %)" id="ix-v"><input id="ix-v" className="input mono" value={f.base_value} onChange={(e) => setF({ ...f, base_value: e.target.value })} required /></Field>
        <Field label="Index then" id="ix-b"><input id="ix-b" className="input mono" value={f.index_base} onChange={(e) => setF({ ...f, index_base: e.target.value })} required /></Field>
        <Field label="Index now" id="ix-n"><input id="ix-n" className="input mono" value={f.index_now} onChange={(e) => setF({ ...f, index_now: e.target.value })} required /></Field>
      </div>
      <div className="form-actions"><button className="btn">Add index estimate</button></div>
    </form>
  );
}
