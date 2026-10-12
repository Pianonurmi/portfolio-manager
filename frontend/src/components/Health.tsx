// Health: per person and measurement, the latest value, a trend with the target band, 30/90-day averages,
// a quick-add form and medications. Sensitive: the server keeps all of it out of the AI, digest and pushes.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api, ApiError } from "../lib/api";
import { date, decimal } from "../lib/format";
import type { HealthSeries, Medication, Person } from "../lib/types";
import { Legend, LineChart, useCssVar } from "./LineChart";
import { Chip, Empty, Field, Panel } from "./ui";

const KINDS: Record<string, string> = { weight: "Weight (kg)", resting_hr: "Resting heart rate (bpm)", blood_pressure: "Blood pressure (mmHg)", sleep: "Sleep (h)", steps: "Steps", custom: "Custom" };
const STATUS = { ok: ["ok", "In target"], above: ["warn", "Above target"], below: ["warn", "Below target"], no_target: [undefined, "No target"], no_data: [undefined, "No data"] } as const;
const n = (v: string | null | undefined) => (v == null ? NaN : Number(v));
const fmt = (s: HealthSeries, v: string | null | undefined, v2?: string | null) => (v == null ? "—" : s.kind === "blood_pressure" && v2 ? `${decimal(v, 0)}/${decimal(v2, 0)}` : decimal(v, s.kind === "steps" ? 0 : 1));

function SeriesCard({ s, person, onChange }: { s: HealthSeries; person: number; onChange: () => void }) {
  const c1 = useCssVar("--c1"), c2 = useCssVar("--c2"), muted = useCssVar("--muted");
  const [edit, setEdit] = useState(false);
  const [t, setT] = useState({ direction: s.target?.direction ?? "below", low: s.target?.low ?? "", high: s.target?.high ?? "", high2: s.target?.high2 ?? "" });
  const [tone, label] = STATUS[s.status];
  const x = s.points.map((p) => p.date);
  const line = [{ name: s.kind === "blood_pressure" ? "Systolic" : s.label, values: s.points.map((p) => n(p.value)), color: c1 }];
  if (s.kind === "blood_pressure") line.push({ name: "Diastolic", values: s.points.map((p) => n(p.value2)), color: c2 });
  if (s.target) {
    const add = (name: string, v: string | null) => v != null && line.push({ name, values: x.map(() => n(v)), color: muted, dashed: true } as (typeof line)[number]);
    if (s.target.direction !== "above") add(s.kind === "blood_pressure" ? "Systolic limit" : "Upper limit", s.target.high);
    if (s.target.direction !== "below") add("Lower limit", s.target.low);
    if (s.kind === "blood_pressure") add("Diastolic limit", s.target.high2);
  }
  const kind = s.kind === "custom" ? "custom" : s.kind;
  return (
    <Panel title={s.label + (s.unit ? ` (${s.unit})` : "")} meta={<Chip tone={tone}>{label}</Chip>}>
      <div style={{ display: "flex", gap: 20, flexWrap: "wrap", marginBottom: 6 }}>
        <div><div className="num" style={{ fontSize: 22 }}>{fmt(s, s.latest?.value, s.latest?.value2)}</div><small style={{ color: "var(--muted)" }}>{s.latest ? `latest ${date(s.latest.date)}` : "no readings"}</small></div>
        <div><div className="num" style={{ fontSize: 16 }}>{fmt(s, s.avg_30?.value, s.avg_30?.value2)}</div><small style={{ color: "var(--muted)" }}>30-day average</small></div>
        <div><div className="num" style={{ fontSize: 16 }}>{fmt(s, s.avg_90?.value, s.avg_90?.value2)}</div><small style={{ color: "var(--muted)" }}>90-day average</small></div>
      </div>
      {s.points.length > 1 && (<><LineChart x={x} series={line} height={150} format={(v) => decimal(v, s.kind === "steps" ? 0 : 1)} label={`${s.label} over the last year`} /><div style={{ marginTop: 6, fontSize: 12 }}><Legend items={line.map((l) => ({ name: l.name, color: l.color }))} /></div></>)}
      <div style={{ marginTop: 8 }}>
        <button className="btn" onClick={() => setEdit(!edit)}>{s.target ? "Change target" : "Set target"}</button>
        {s.target && <button className="btn" style={{ marginLeft: 6 }} onClick={async () => { await api(`/api/health/targets/${s.target!.id}`, { method: "DELETE" }); onChange(); }}>Remove target</button>}
      </div>
      {edit && (
        <form style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8 }} onSubmit={async (ev) => {
          ev.preventDefault();
          await api("/api/health/targets", { method: "PUT", body: { person_id: person, kind, name: kind === "custom" ? s.label : null, direction: t.direction, low: t.low || null, high: t.high || null, high2: t.high2 || null } });
          setEdit(false); onChange();
        }}>
          <select className="select" aria-label="Direction" value={t.direction} onChange={(ev) => setT({ ...t, direction: ev.target.value as typeof t.direction })}><option value="below">Stay below</option><option value="above">Stay above</option><option value="range">Stay within</option></select>
          {t.direction !== "below" && <input className="input mono" style={{ width: 90 }} aria-label="Lower limit" placeholder="min" value={t.low} onChange={(ev) => setT({ ...t, low: ev.target.value })} />}
          {t.direction !== "above" && <input className="input mono" style={{ width: 90 }} aria-label="Upper limit" placeholder={s.kind === "blood_pressure" ? "systolic max" : "max"} value={t.high} onChange={(ev) => setT({ ...t, high: ev.target.value })} />}
          {s.kind === "blood_pressure" && <input className="input mono" style={{ width: 100 }} aria-label="Diastolic limit" placeholder="diastolic max" value={t.high2} onChange={(ev) => setT({ ...t, high2: ev.target.value })} />}
          <button className="btn primary">Save</button>
        </form>
      )}
    </Panel>
  );
}

function QuickAdd({ person, onDone }: { person: number; onDone: () => void }) {
  const [f, setF] = useState({ kind: "weight", value: "", value2: "", name: "", unit: "", date: "", note: "" });
  const [err, setErr] = useState<string | null>(null);
  const set = (k: keyof typeof f) => (ev: { target: { value: string } }) => setF({ ...f, [k]: ev.target.value });
  return (
    <form className="form-grid" onSubmit={async (ev) => {
      ev.preventDefault();
      try {
        await api("/api/health/metrics", { body: { person_id: person, kind: f.kind, value: f.value, value2: f.kind === "blood_pressure" ? f.value2 : null, name: f.kind === "custom" ? f.name : null, unit: f.kind === "custom" ? f.unit || null : null, date: f.date || null, note: f.note || null } });
        setF({ ...f, value: "", value2: "", note: "" }); setErr(null); onDone();
      } catch (x) { setErr(x instanceof ApiError ? x.message : "Failed"); }
    }}>
      <Field label="Measurement" id="h-k"><select id="h-k" className="select input" value={f.kind} onChange={set("kind")}>{Object.entries(KINDS).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></Field>
      {f.kind === "custom" && <Field label="Name" id="h-n"><input id="h-n" className="input" value={f.name} onChange={set("name")} required /></Field>}
      {f.kind === "custom" && <Field label="Unit" id="h-u"><input id="h-u" className="input" value={f.unit} onChange={set("unit")} /></Field>}
      <Field label={f.kind === "blood_pressure" ? "Systolic" : "Value"} id="h-v"><input id="h-v" className="input mono" inputMode="decimal" value={f.value} onChange={set("value")} required /></Field>
      {f.kind === "blood_pressure" && <Field label="Diastolic" id="h-v2"><input id="h-v2" className="input mono" inputMode="decimal" value={f.value2} onChange={set("value2")} required /></Field>}
      <Field label="Date (today if empty)" id="h-d"><input id="h-d" className="input" type="date" value={f.date} onChange={set("date")} /></Field>
      <Field label="Note" id="h-no"><input id="h-no" className="input" value={f.note} onChange={set("note")} /></Field>
      <div className="form-actions" style={{ gridColumn: "1 / -1" }}><button className="btn primary">Add reading</button>{err && <span style={{ color: "var(--loss)", marginLeft: 8 }}>{err}</span>}</div>
    </form>
  );
}

function Meds({ person, onChange }: { person: number; onChange: () => void }) {
  const meds = useQuery({ queryKey: ["medications", person], queryFn: () => api<Medication[]>(`/api/health/medications?person_id=${person}`) });
  const [m, setM] = useState({ name: "", dose: "", schedule: "", refill_date: "" });
  const refresh = () => { meds.refetch(); onChange(); };
  return (
    <Panel title="Medications" meta="refill dates appear in the calendar">
      {meds.data?.map((x) => (
        <div key={x.id} className="li" style={{ cursor: "default", padding: "6px 0", opacity: x.active ? 1 : 0.55 }}>
          <div className="t">{x.name}{x.dose ? ` · ${x.dose}` : ""}<small>{x.schedule ?? ""}{x.refill_date ? ` · refill ${date(x.refill_date)}` : ""}{x.active ? "" : " · stopped"}</small></div>
          <button className="btn" onClick={async () => { await api(`/api/health/medications/${x.id}`, { method: "PUT", body: { ...x, active: !x.active } }); refresh(); }}>{x.active ? "Stop" : "Resume"}</button>
          <button className="x" aria-label={`Delete ${x.name}`} onClick={async () => { await api(`/api/health/medications/${x.id}`, { method: "DELETE" }); refresh(); }}>×</button>
        </div>
      ))}
      <form style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8 }} onSubmit={async (ev) => { ev.preventDefault(); await api("/api/health/medications", { body: { person_id: person, name: m.name, dose: m.dose || null, schedule: m.schedule || null, refill_date: m.refill_date || null } }); setM({ name: "", dose: "", schedule: "", refill_date: "" }); refresh(); }}>
        <input className="input" placeholder="Name" aria-label="Medication name" value={m.name} onChange={(ev) => setM({ ...m, name: ev.target.value })} required />
        <input className="input" style={{ width: 90 }} placeholder="Dose" aria-label="Dose" value={m.dose} onChange={(ev) => setM({ ...m, dose: ev.target.value })} />
        <input className="input" placeholder="When" aria-label="Schedule" value={m.schedule} onChange={(ev) => setM({ ...m, schedule: ev.target.value })} />
        <input className="input" type="date" aria-label="Refill date" value={m.refill_date} onChange={(ev) => setM({ ...m, refill_date: ev.target.value })} />
        <button className="btn">Add</button>
      </form>
    </Panel>
  );
}

export function Health() {
  const qc = useQueryClient();
  const persons = useQuery({ queryKey: ["persons"], queryFn: () => api<Person[]>("/api/persons") });
  const [pid, setPid] = useState<number | null>(null);
  const person = pid ?? persons.data?.find((p) => p.is_self)?.id ?? persons.data?.[0]?.id ?? null;
  const overview = useQuery({ queryKey: ["health", person], enabled: person != null, queryFn: () => api<{ series: HealthSeries[] }>(`/api/health/overview?person_id=${person}`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["health"] }); qc.invalidateQueries({ queryKey: ["attention"] }); qc.invalidateQueries({ queryKey: ["calendar"] }); };
  if (persons.data && !persons.data.length) return <Empty title="Add a person first">Health data is kept per person (Settings → People).</Empty>;
  return (
    <>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
        <select className="select" aria-label="Person" value={person ?? ""} onChange={(ev) => setPid(Number(ev.target.value))}>{persons.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>Private: never sent to the AI, the weekly digest or notifications.</span>
        <Link className="btn" style={{ marginLeft: "auto" }} to="/life?tab=calendar&new=health">Add an appointment</Link>
      </div>
      {person != null && (
        <div className="grid g-2">
          <Panel title="Add a reading"><QuickAdd person={person} onDone={refresh} /></Panel>
          <Meds person={person} onChange={refresh} />
          {overview.data?.series.map((s) => <SeriesCard key={s.series} s={s} person={person} onChange={refresh} />)}
        </div>
      )}
    </>
  );
}
