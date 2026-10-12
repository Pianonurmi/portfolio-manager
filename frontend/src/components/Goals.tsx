// Goals (money targets with pace) and habits (check-ins with streaks).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "../lib/api";
import { date, decimal, money, pct } from "../lib/format";
import type { Account, Goal, GoalKind, Habit, Person } from "../lib/types";
import { Chip, Empty, Field, Panel } from "./ui";

const KINDS: Record<GoalKind, string> = { savings: "Savings", net_worth: "Net worth", portfolio: "Investment portfolio", debt_payoff: "Pay off a debt", custom: "Custom" };
const STATUS = { on_track: ["ok", "On track"], behind: ["warn", "Behind"], reached: ["ok", "Reached"] } as const;

function GoalCard({ g, onChange }: { g: Goal; onChange: () => void }) {
  const [amt, setAmt] = useState("");
  const tone = g.status === "behind" ? "var(--warn)" : g.status === "reached" ? "var(--gain)" : "var(--accent)";
  const decreasing = g.kind === "debt_payoff";
  const [t, label] = STATUS[g.status];
  return (
    <div style={{ padding: "10px 0", borderBottom: "1px solid var(--line)" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
        <strong>{g.title}</strong>
        <Chip tone={t}>{label}</Chip>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>{KINDS[g.kind]}{g.target_date ? ` · by ${date(g.target_date)}` : ""}</span>
        <button className="x" style={{ marginLeft: "auto" }} aria-label={`Delete ${g.title}`} onClick={async () => { await api(`/api/goals/${g.id}`, { method: "DELETE" }); onChange(); }}>×</button>
      </div>
      <div className="meter" role="img" aria-label={`${g.title}: ${g.pct} % done`}><span style={{ width: `${g.pct}%`, background: tone }} /></div>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginTop: 6, fontSize: 12, color: "var(--fg-2)" }}>
        <span><span className="num">{money(g.current)}</span> {decreasing ? "owed" : ""} of <span className="num">{money(g.target_amount)}</span> · {decimal(g.pct, 0)} %</span>
        {g.required_monthly != null && g.status !== "reached" && <span>needs <span className="num">{money(g.required_monthly)}</span>/month</span>}
        {g.actual_monthly != null && <span>pace <span className="num">{money(g.actual_monthly)}</span>/month</span>}
        {g.projected_date && g.status !== "reached" && <span>at this pace {date(g.projected_date)}</span>}
        {g.reached_on && <span>reached {date(g.reached_on)}</span>}
      </div>
      {g.manual && (
        <form style={{ display: "flex", gap: 6, marginTop: 6 }} onSubmit={async (ev) => { ev.preventDefault(); await api(`/api/goals/${g.id}/entries`, { body: { amount: amt } }); setAmt(""); onChange(); }}>
          <input className="input mono" style={{ maxWidth: 140 }} placeholder="Add € (− to take out)" aria-label={`Amount for ${g.title}`} value={amt} onChange={(ev) => setAmt(ev.target.value)} required />
          <button className="btn">Add</button>
          {g.entries.slice(0, 3).map((e) => <span key={e.id} className="num" style={{ fontSize: 11, color: "var(--muted)", alignSelf: "center" }}>{date(e.date)} {money(e.amount)}</span>)}
        </form>
      )}
    </div>
  );
}

function GoalForm({ onDone }: { onDone: () => void }) {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/api/accounts") });
  const persons = useQuery({ queryKey: ["persons"], queryFn: () => api<Person[]>("/api/persons") });
  const [f, setF] = useState({ title: "", kind: "savings" as GoalKind, target_amount: "", target_date: "", start_amount: "0", entity_id: "", account_id: "" });
  const [err, setErr] = useState<string | null>(null);
  const set = (k: keyof typeof f) => (ev: { target: { value: string } }) => setF({ ...f, [k]: ev.target.value });
  const liabilities = (accounts.data ?? []).filter((a) => ["loan", "mortgage", "housing_loan", "credit_card", "manual_liability"].includes(a.kind) && !a.closed_on);
  return (
    <form className="form-grid" onSubmit={async (ev) => {
      ev.preventDefault();
      try {
        await api("/api/goals", { body: { title: f.title, kind: f.kind, target_amount: f.target_amount || "0", target_date: f.target_date || null, start_amount: f.kind === "savings" || f.kind === "custom" || f.kind === "debt_payoff" ? f.start_amount || "0" : "0", entity_id: f.entity_id ? Number(f.entity_id) : null, account_id: f.kind === "debt_payoff" && f.account_id ? Number(f.account_id) : null } });
        setF({ ...f, title: "", target_amount: "" });
        setErr(null);
        onDone();
      } catch (x) { setErr(x instanceof ApiError ? x.message : "Failed"); }
    }}>
      <Field label="Goal" id="g-t"><input id="g-t" className="input" value={f.title} onChange={set("title")} required placeholder="Emergency fund" /></Field>
      <Field label="Type" id="g-k"><select id="g-k" className="select input" value={f.kind} onChange={set("kind")}>{Object.entries(KINDS).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></Field>
      <Field label={f.kind === "debt_payoff" ? "Target owed €" : "Target €"} id="g-a"><input id="g-a" className="input mono" value={f.target_amount} onChange={set("target_amount")} required={f.kind !== "debt_payoff"} /></Field>
      <Field label="Target date" id="g-d"><input id="g-d" className="input" type="date" value={f.target_date} onChange={set("target_date")} /></Field>
      {(f.kind === "savings" || f.kind === "custom") && <Field label="Already saved €" id="g-s"><input id="g-s" className="input mono" value={f.start_amount} onChange={set("start_amount")} /></Field>}
      {f.kind === "debt_payoff" && <Field label="Debt account" id="g-ac"><select id="g-ac" className="select input" value={f.account_id} onChange={set("account_id")} required><option value="">Choose…</option>{liabilities.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</select></Field>}
      {(f.kind === "net_worth" || f.kind === "portfolio") && <Field label="Whose" id="g-e"><select id="g-e" className="select input" value={f.entity_id} onChange={set("entity_id")}><option value="">Household</option>{(persons.data ?? []).map((p) => <option key={p.id} value={p.entity_id}>{p.name}</option>)}</select></Field>}
      <div className="form-actions" style={{ gridColumn: "1 / -1" }}><button className="btn primary">Add goal</button>{err && <span style={{ color: "var(--loss)", marginLeft: 8 }}>{err}</span>}</div>
    </form>
  );
}

function HabitRow({ h, onChange }: { h: Habit; onChange: () => void }) {
  const today = new Date();
  const days = Array.from({ length: 28 }, (_, i) => { const d = new Date(today); d.setDate(d.getDate() - (27 - i)); return d.toLocaleDateString("sv-SE"); });
  const done = new Set(h.recent);
  const warn = h.warning === "broken" ? ["bad", "Streak broken"] : h.warning === "at_risk" ? ["warn", "Streak at risk"] : null;
  const unit = h.cadence === "weekly" ? "wk" : "d";
  return (
    <div className="li" style={{ cursor: "default", flexWrap: "wrap" }}>
      <div className="t">
        {h.title}
        <small>
          {h.cadence === "weekly" ? `${h.times_per_week}× a week (${h.done_this_week} this week)` : "daily"} · streak <span className="num">{h.current_streak}{unit}</span> · best <span className="num">{h.best_streak}{unit}</span> · 30 days <span className="num">{pct(h.rate_30d, 0)}</span>
        </small>
      </div>
      {warn && <Chip tone={warn[0] as "bad" | "warn"}>{warn[1]}</Chip>}
      <span role="img" aria-label={`Last 28 days: ${h.recent.length} check-ins`} style={{ display: "inline-flex", gap: 2 }}>
        {days.map((d) => <i key={d} title={d} style={{ width: 6, height: 14, borderRadius: 2, background: done.has(d) ? "var(--accent)" : "var(--surface-2)" }} />)}
      </span>
      <button className="btn" aria-pressed={h.done_today} onClick={async () => { await api(h.done_today ? `/api/habits/${h.id}/checkins/${days[27]}` : `/api/habits/${h.id}/checkins`, h.done_today ? { method: "DELETE" } : { body: {} }); onChange(); }}>{h.done_today ? "Done today ✓" : "Check in"}</button>
      <button className="x" aria-label={`Delete ${h.title}`} onClick={async () => { await api(`/api/habits/${h.id}`, { method: "DELETE" }); onChange(); }}>×</button>
    </div>
  );
}

export function Goals() {
  const qc = useQueryClient();
  const goals = useQuery({ queryKey: ["goals"], queryFn: () => api<Goal[]>("/api/goals") });
  const habits = useQuery({ queryKey: ["habits"], queryFn: () => api<Habit[]>("/api/habits") });
  const [h, setH] = useState({ title: "", cadence: "daily", times_per_week: "3" });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["goals"] }); qc.invalidateQueries({ queryKey: ["habits"] }); qc.invalidateQueries({ queryKey: ["attention"] }); };
  return (
    <div className="grid g-2">
      <Panel title="Goals" meta="net worth and portfolio goals read your accounts live">
        {goals.data?.length ? goals.data.map((g) => <GoalCard key={g.id} g={g} onChange={refresh} />) : <Empty title="No goals yet">Set a target and a date; the pace needed is worked out for you.</Empty>}
        <div style={{ marginTop: 12 }}><GoalForm onDone={refresh} /></div>
      </Panel>
      <Panel title="Habits" meta="streaks and the last 30 days" bodyless>
        <div className="list">{habits.data?.map((x) => <HabitRow key={x.id} h={x} onChange={refresh} />)}</div>
        <form style={{ display: "flex", gap: 6, padding: 12, flexWrap: "wrap" }} onSubmit={async (ev) => { ev.preventDefault(); await api("/api/habits", { body: { title: h.title, cadence: h.cadence, times_per_week: Number(h.times_per_week) } }); setH({ ...h, title: "" }); refresh(); }}>
          <input className="input" placeholder="New habit" aria-label="Habit" value={h.title} onChange={(ev) => setH({ ...h, title: ev.target.value })} required />
          <select className="select" aria-label="Cadence" value={h.cadence} onChange={(ev) => setH({ ...h, cadence: ev.target.value })}><option value="daily">Daily</option><option value="weekly">Weekly</option></select>
          {h.cadence === "weekly" && <input className="input mono" style={{ width: 60 }} type="number" min={1} max={7} aria-label="Times per week" value={h.times_per_week} onChange={(ev) => setH({ ...h, times_per_week: ev.target.value })} />}
          <button className="btn">Add habit</button>
        </form>
      </Panel>
    </div>
  );
}
