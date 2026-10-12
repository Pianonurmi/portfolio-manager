// Life & tools: local AI Q&A, stress tests, costs, insurance, estate package, timeline (Phases 12–15).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router";
import { CalendarView } from "../components/CalendarView";
import { DownloadPdfButton, ScheduledPackage } from "../components/EstatePackage";
import { Goals } from "../components/Goals";
import { Health } from "../components/Health";
import { Field, Panel, ScreenHead } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { date, money, pct } from "../lib/format";

const TABS = [["overview", "Overview"], ["goals", "Goals & habits"], ["calendar", "Calendar"], ["health", "Health"]] as const;

export function Life() {
  const [params, setParams] = useSearchParams();
  const tab = TABS.find(([k]) => k === params.get("tab"))?.[0] ?? "overview";
  return (
    <>
      <ScreenHead title="Life & tools" sub="Ask your data, goals, calendar, health and the estate package" />
      <div className="tabs" role="tablist" style={{ marginBottom: 12 }}>
        {TABS.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => setParams(k === "overview" ? {} : { tab: k })}>{label}</button>
        ))}
      </div>
      {tab === "overview" && <Overview />}
      {tab === "goals" && <Goals />}
      {tab === "calendar" && <CalendarView />}
      {tab === "health" && <Health />}
    </>
  );
}

function Overview() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stress, setStress] = useState<Record<string, { success: number; p50_at_90: number }> | null>(null);
  const pol = useQuery({ queryKey: ["policies"], queryFn: () => api<{ policies: Record<string, string | null>[]; adequacy: Record<string, string | boolean> }>("/api/policies") });
  const est = useQuery({ queryKey: ["estate"], queryFn: () => api<{ id: number; section: string; title: string; body: string }[]>("/api/estate") });
  const tl = useQuery({ queryKey: ["timeline"], queryFn: () => api<{ date: string; kind: string; title: string }[]>("/api/timeline?limit=30") });
  const costs = useQuery({ queryKey: ["costs"], queryFn: () => api<{ trading_fees: string; fund_costs_estimate: string; note: string }>("/api/portfolio/costs") });
  const [p, setP] = useState({ kind: "life", insurer: "", coverage: "", premium_yearly: "", renewal_date: "", beneficiaries: "" });
  const [e, setE] = useState({ section: "instruction", title: "", body: "" });
  const askAi = async () => {
    setBusy(true);
    try { setAnswer((await api<{ answer: string }>("/api/ai/ask", { body: { question: q } })).answer); }
    catch (x) { setAnswer(x instanceof ApiError ? x.message : "Failed"); }
    finally { setBusy(false); }
  };
  const runStress = async () => {
    const b = await api<{ params: Record<string, unknown> }>("/api/plan/base");
    setStress(await api("/api/plan/stress", { body: { params: b.params } }));
  };
  return (
    <>
      <div className="grid g-2">
        <Panel title="Ask (local AI)" meta="answers only from your data · never acts">
          <form onSubmit={(ev) => (ev.preventDefault(), askAi())} style={{ display: "flex", gap: 8 }}>
            <input className="input" placeholder="How much did my investments gain this year?" value={q} onChange={(ev) => setQ(ev.target.value)} aria-label="Question" />
            <button className="btn primary" disabled={busy || q.length < 3}>{busy ? "Thinking…" : "Ask"}</button>
          </form>
          {answer && <p style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>{answer}</p>}
        </Panel>
        <Panel title="Stress test" meta={<button className="btn" onClick={runStress}>Run on baseline</button>}>
          {stress ? (
            <dl className="kv">{Object.entries(stress).map(([k, v]) => (<div key={k} style={{ display: "contents" }}><dt>{k === "base" ? "Normal markets" : k === "crash_at_retirement" ? "−35 % in the first retirement year" : "Lost decade after retiring"}</dt><dd>{pct(v.success)} success · median at 90 {money(v.p50_at_90)}</dd></div>))}</dl>
          ) : <span style={{ color: "var(--muted)" }}>Uses your Planning baseline.</span>}
          {costs.data && <p style={{ margin: "10px 0 0", fontSize: 12, color: "var(--fg-2)" }}>Costs this year: trading fees {money(costs.data.trading_fees, "EUR", 2)} · fund running costs ≈ {money(costs.data.fund_costs_estimate)}</p>}
        </Panel>
        <Panel title="Insurance" meta={pol.data ? (pol.data.adequacy.covers_debt ? "life/disability cover ≥ debt" : "cover below household debt") : ""}>
          {pol.data?.policies.map((x) => (<div key={String(x.id)} className="li" style={{ cursor: "default", padding: "6px 0" }}><div className="t">{x.insurer} · {x.kind}<small>{x.coverage ? money(x.coverage) : ""} {x.renewal_date ? `· renews ${date(x.renewal_date)}` : ""}</small></div>
            <button className="x" aria-label="Delete" onClick={async () => (await api(`/api/policies/${x.id}`, { method: "DELETE" }), qc.invalidateQueries({ queryKey: ["policies"] }))}>×</button></div>))}
          <form onSubmit={async (ev) => { ev.preventDefault(); await api("/api/policies", { body: Object.fromEntries(Object.entries(p).map(([k, v]) => [k, v || null])) }); setP({ ...p, insurer: "", coverage: "" }); qc.invalidateQueries({ queryKey: ["policies"] }); }} className="form-grid" style={{ marginTop: 8 }}>
            <Field label="Type" id="po-k"><select id="po-k" className="select input" value={p.kind} onChange={(ev) => setP({ ...p, kind: ev.target.value })}>{["life", "disability", "home", "accident", "travel", "car", "other"].map((k) => <option key={k}>{k}</option>)}</select></Field>
            <Field label="Insurer" id="po-i"><input id="po-i" className="input" value={p.insurer} onChange={(ev) => setP({ ...p, insurer: ev.target.value })} required /></Field>
            <Field label="Cover €" id="po-c"><input id="po-c" className="input mono" value={p.coverage} onChange={(ev) => setP({ ...p, coverage: ev.target.value })} /></Field>
            <Field label="Renewal" id="po-r"><input id="po-r" className="input" type="date" value={p.renewal_date} onChange={(ev) => setP({ ...p, renewal_date: ev.target.value })} /></Field>
            <div className="form-actions" style={{ gridColumn: "1 / -1" }}><button className="btn">Add policy</button></div>
          </form>
        </Panel>
        <Panel title="If something happens to me" meta={<span style={{ display: "flex", gap: 6 }}><a className="btn" href="/api/estate/package" download>Printable HTML</a><DownloadPdfButton /></span>}>
          {est.data?.map((x) => (<div key={x.id} className="li" style={{ cursor: "default", padding: "6px 0" }}><div className="t">{x.section}: {x.title}<small style={{ whiteSpace: "pre-wrap" }}>{x.body}</small></div>
            <button className="x" aria-label="Delete" onClick={async () => (await api(`/api/estate/${x.id}`, { method: "DELETE" }), qc.invalidateQueries({ queryKey: ["estate"] }))}>×</button></div>))}
          <form onSubmit={async (ev) => { ev.preventDefault(); try { await api("/api/estate", { body: e }); setE({ ...e, title: "", body: "" }); qc.invalidateQueries({ queryKey: ["estate"] }); } catch (x) { alert(x instanceof ApiError ? x.message : "Failed"); } }} style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 8 }}>
            <div style={{ display: "flex", gap: 6 }}>
              <select className="select" aria-label="Section" value={e.section} onChange={(ev) => setE({ ...e, section: ev.target.value })}>{["contact", "instruction", "wish", "location"].map((k) => <option key={k}>{k}</option>)}</select>
              <input className="input" placeholder="Title" value={e.title} onChange={(ev) => setE({ ...e, title: ev.target.value })} required aria-label="Title" />
            </div>
            <textarea className="textarea" rows={2} placeholder="Say where things are, never the passwords themselves" value={e.body} onChange={(ev) => setE({ ...e, body: ev.target.value })} required aria-label="Text" />
            <div className="form-actions"><button className="btn">Add</button></div>
          </form>
          <ScheduledPackage />
        </Panel>
      </div>
      <Panel title="Timeline" style={{ marginTop: 12 }} bodyless>
        <div className="list">{tl.data?.map((t, i) => (<div key={i} className="li" style={{ cursor: "default" }}><span className="when" style={{ minWidth: 86 }}>{date(t.date)}</span><div className="t">{t.title}<small>{t.kind}</small></div></div>))}</div>
      </Panel>
    </>
  );
}
