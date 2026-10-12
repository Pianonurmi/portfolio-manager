// Calendar: month grid + agenda of own events and dates the other modules know (renewals, due dates, goals).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api, ApiError } from "../lib/api";
import { date } from "../lib/format";
import type { CalCategory, CalEvent, CalOccurrence, Person } from "../lib/types";
import { Field, Modal, Panel } from "./ui";

// Colour is never the only cue: every entry also names its source in text.
const CAT: Record<CalCategory, { label: string; color: string }> = {
  finance: { label: "Finance", color: "var(--c1)" },
  health: { label: "Health", color: "var(--c2)" },
  home: { label: "Home", color: "var(--c3)" },
  family: { label: "Family", color: "var(--c4)" },
  other: { label: "Other", color: "var(--c5)" },
};
const SOURCES: Record<string, string> = { calendar: "My events", insurance: "Insurance", vehicles: "Vehicles", home: "Home", subscriptions: "Subscriptions", loans: "Loans", goals: "Goals", health: "Health" };
const iso = (d: Date) => d.toLocaleDateString("sv-SE");
const addDays = (d: Date, n: number) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };

type Form = { id?: number; title: string; start_date: string; all_day: boolean; start_time: string; end_date: string; end_time: string; recurrence: string; category: string; person_id: string; reminder: string; notes: string };
const blank = (day: string, category = "other"): Form => ({ title: "", start_date: day, all_day: true, start_time: "09:00", end_date: "", end_time: "", recurrence: "none", category, person_id: "", reminder: "", notes: "" });

function EventModal({ f, setF, onClose, onSaved }: { f: Form; setF: (f: Form) => void; onClose: () => void; onSaved: () => void }) {
  const persons = useQuery({ queryKey: ["persons"], queryFn: () => api<Person[]>("/api/persons") });
  const [err, setErr] = useState<string | null>(null);
  const set = (k: keyof Form) => (ev: { target: { value: string } }) => setF({ ...f, [k]: ev.target.value });
  const save = async (ev: React.FormEvent) => {
    ev.preventDefault();
    const body = { title: f.title, start_date: f.start_date, all_day: f.all_day, start_time: f.all_day ? null : f.start_time, end_date: f.end_date || null, end_time: f.all_day || !f.end_time ? null : f.end_time, recurrence: f.recurrence, category: f.category, person_id: f.person_id ? Number(f.person_id) : null, notes: f.notes || null, reminder_days_before: f.reminder === "" ? null : Number(f.reminder) };
    try { await (f.id ? api(`/api/calendar/events/${f.id}`, { method: "PUT", body }) : api("/api/calendar/events", { body })); onSaved(); } catch (x) { setErr(x instanceof ApiError ? x.message : "Failed"); }
  };
  return (
    <Modal title={f.id ? "Edit event" : "New event"} onClose={onClose}>
      <form className="form-grid" onSubmit={save}>
        <Field label="Title" id="ev-t"><input id="ev-t" className="input" value={f.title} onChange={set("title")} required autoFocus /></Field>
        <Field label="Date" id="ev-d"><input id="ev-d" className="input" type="date" value={f.start_date} onChange={set("start_date")} required /></Field>
        <Field label="All day" id="ev-a"><input id="ev-a" type="checkbox" checked={f.all_day} onChange={(ev) => setF({ ...f, all_day: ev.target.checked })} /></Field>
        {!f.all_day && <Field label="Start time" id="ev-st"><input id="ev-st" className="input" type="time" value={f.start_time} onChange={set("start_time")} required /></Field>}
        {!f.all_day && <Field label="End time" id="ev-et"><input id="ev-et" className="input" type="time" value={f.end_time} onChange={set("end_time")} /></Field>}
        <Field label="Last day (optional)" id="ev-ed"><input id="ev-ed" className="input" type="date" value={f.end_date} onChange={set("end_date")} /></Field>
        <Field label="Repeats" id="ev-r" hint="Changes apply to the whole series"><select id="ev-r" className="select input" value={f.recurrence} onChange={set("recurrence")}>{["none", "weekly", "monthly", "yearly"].map((r) => <option key={r} value={r}>{r === "none" ? "Does not repeat" : r}</option>)}</select></Field>
        <Field label="Category" id="ev-c"><select id="ev-c" className="select input" value={f.category} onChange={set("category")}>{Object.entries(CAT).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}</select></Field>
        <Field label="Person" id="ev-p"><select id="ev-p" className="select input" value={f.person_id} onChange={set("person_id")}><option value="">Everyone</option>{(persons.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
        <Field label="Remind me (days before)" id="ev-rm"><input id="ev-rm" className="input mono" type="number" min={0} max={365} value={f.reminder} onChange={set("reminder")} /></Field>
        <Field label="Notes" id="ev-n"><input id="ev-n" className="input" value={f.notes} onChange={set("notes")} /></Field>
        <div className="form-actions" style={{ gridColumn: "1 / -1", display: "flex", gap: 8, alignItems: "center" }}>
          <button className="btn primary">Save</button>
          {f.id && <button type="button" className="btn" onClick={async () => { await api(`/api/calendar/events/${f.id}`, { method: "DELETE" }); onSaved(); }}>Delete</button>}
          {err && <span style={{ color: "var(--loss)" }}>{err}</span>}
        </div>
      </form>
    </Modal>
  );
}

export function CalendarView() {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [month, setMonth] = useState(() => { const d = new Date(); return new Date(d.getFullYear(), d.getMonth(), 1); });
  const [category, setCategory] = useState("");
  const [source, setSource] = useState("");
  const [form, setForm] = useState<Form | null>(() => (params.get("new") ? blank(iso(new Date()), params.get("new") ?? "other") : null));
  const [picked, setPicked] = useState<string | null>(null);
  const gridStart = addDays(month, -((month.getDay() + 6) % 7)); // Monday
  const gridEnd = addDays(gridStart, 41);
  const q = `category=${category}&source=${source}`.replace(/(category|source)=(&|$)/g, "");
  const month$ = useQuery({ queryKey: ["calendar", iso(gridStart), category, source], queryFn: () => api<CalOccurrence[]>(`/api/calendar?from=${iso(gridStart)}&to=${iso(gridEnd)}${q ? "&" + q : ""}`) });
  const agenda = useQuery({ queryKey: ["calendar-agenda", category, source], queryFn: () => api<CalOccurrence[]>(`/api/calendar?from=${iso(new Date())}&to=${iso(addDays(new Date(), 60))}${q ? "&" + q : ""}`) });
  const events = useQuery({ queryKey: ["calendar-events"], queryFn: () => api<CalEvent[]>("/api/calendar/events") });
  const byDay = new Map<string, CalOccurrence[]>();
  for (const o of month$.data ?? []) {
    const last = o.end_date ?? o.date;
    for (let d = new Date(o.date + "T00:00:00"); iso(d) <= last; d = addDays(d, 1)) byDay.set(iso(d), [...(byDay.get(iso(d)) ?? []), o]);
  }
  const refresh = () => { setForm(null); params.delete("new"); setParams(params, { replace: true }); qc.invalidateQueries({ queryKey: ["calendar"] }); qc.invalidateQueries({ queryKey: ["calendar-agenda"] }); qc.invalidateQueries({ queryKey: ["calendar-events"] }); qc.invalidateQueries({ queryKey: ["attention"] }); };
  const edit = (o: CalOccurrence) => {
    const e = events.data?.find((x) => x.id === o.event_id);
    if (e) setForm({ id: e.id, title: e.title, start_date: e.start_date, all_day: e.all_day, start_time: e.start_time?.slice(0, 5) ?? "09:00", end_date: e.end_date ?? "", end_time: e.end_time?.slice(0, 5) ?? "", recurrence: e.recurrence, category: e.category, person_id: e.person_id ? String(e.person_id) : "", reminder: e.reminder_days_before == null ? "" : String(e.reminder_days_before), notes: e.notes ?? "" });
  };
  const entry = (o: CalOccurrence, i: number) => (
    <div key={i} className="li" style={{ cursor: "default", padding: "6px 0" }}>
      <i aria-hidden style={{ width: 8, height: 8, borderRadius: 4, background: CAT[o.category].color, flex: "none" }} />
      <span className="when" style={{ minWidth: 86 }}>{date(o.date)}{o.time ? ` ${o.time}` : ""}</span>
      <div className="t">{o.title}<small>{SOURCES[o.source] ?? o.source} · {CAT[o.category].label}{o.recurring ? " · repeats" : ""}</small></div>
      {o.event_id ? <button className="btn" onClick={() => edit(o)}>Edit</button> : <Link className="btn" to={o.route}>Open</Link>}
    </div>
  );
  const dayItems = picked ? byDay.get(picked) ?? [] : [];
  const title = month.toLocaleDateString("fi-FI", { month: "long", year: "numeric" });
  const todayIso = iso(new Date());
  return (
    <>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
        <div className="seg" role="group" aria-label="Month">
          <button onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))} aria-label="Previous month">‹</button>
          <button aria-pressed onClick={() => setMonth(new Date(new Date().getFullYear(), new Date().getMonth(), 1))}>{title}</button>
          <button onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))} aria-label="Next month">›</button>
        </div>
        <select className="select" aria-label="Category" value={category} onChange={(ev) => setCategory(ev.target.value)}><option value="">All categories</option>{Object.entries(CAT).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}</select>
        <select className="select" aria-label="Source" value={source} onChange={(ev) => setSource(ev.target.value)}><option value="">All sources</option>{Object.entries(SOURCES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <span style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <a className="btn" href="/api/calendar.ics?include=all" download>Download .ics</a>
          <button className="btn primary" onClick={() => setForm(blank(picked ?? todayIso))}>New event</button>
        </span>
      </div>
      <div className="grid g-2" style={{ gridTemplateColumns: "minmax(0,3fr) minmax(0,2fr)" }}>
        <Panel title={title} meta={Object.entries(CAT).map(([k, v]) => <span key={k} style={{ marginLeft: 10 }}><i aria-hidden style={{ display: "inline-block", width: 8, height: 8, borderRadius: 4, background: v.color, marginRight: 4 }} />{v.label}</span>)}>
          <div role="grid" aria-label={title} style={{ display: "grid", gridTemplateColumns: "repeat(7,minmax(0,1fr))", gap: 1, background: "var(--line)", border: "1px solid var(--line)" }}>
            {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => <div key={d} style={{ background: "var(--surface-2)", padding: "3px 6px", fontSize: 11, color: "var(--muted)" }}>{d}</div>)}
            {Array.from({ length: 42 }, (_, i) => {
              const d = addDays(gridStart, i), k = iso(d), items = byDay.get(k) ?? [];
              return (
                <button key={k} role="gridcell" aria-label={`${date(k)}, ${items.length} items`} onClick={() => setPicked(k)} onDoubleClick={() => setForm(blank(k))}
                  style={{ background: k === picked ? "var(--accent-wash)" : "var(--surface)", border: 0, minHeight: 64, padding: 3, textAlign: "left", cursor: "pointer", opacity: d.getMonth() === month.getMonth() ? 1 : 0.45, outline: k === todayIso ? "1px solid var(--accent)" : undefined, overflow: "hidden" }}>
                  <div className="num" style={{ fontSize: 11, color: k === todayIso ? "var(--accent)" : "var(--fg-2)" }}>{d.getDate()}</div>
                  {items.slice(0, 2).map((o, j) => <div key={j} style={{ fontSize: 10, lineHeight: 1.25, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", borderLeft: `3px solid ${CAT[o.category].color}`, paddingLeft: 3, marginTop: 1 }}>{o.title}</div>)}
                  {items.length > 2 && <div style={{ fontSize: 10, color: "var(--muted)" }}>+{items.length - 2} more</div>}
                </button>
              );
            })}
          </div>
          {picked && <div style={{ marginTop: 10 }}><strong>{date(picked)}</strong>{dayItems.length ? dayItems.map(entry) : <p style={{ color: "var(--muted)", margin: "4px 0" }}>Nothing planned. Double-click a day to add an event.</p>}</div>}
        </Panel>
        <Panel title="Next 60 days" bodyless>
          <div className="list" style={{ padding: "0 14px", maxHeight: 520, overflowY: "auto" }}>{agenda.data?.length ? agenda.data.map(entry) : <p style={{ color: "var(--muted)" }}>Nothing coming up.</p>}</div>
        </Panel>
      </div>
      {form && <EventModal f={form} setF={setForm} onClose={() => setForm(null)} onSaved={refresh} />}
    </>
  );
}
