// Kids (KIDS-01/02): each child's own portfolio, contributions, return, growth projection.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import { Link, useLocation } from "react-router";
import { Figure } from "../components/Figure";
import { LineChart, useCssVar } from "../components/LineChart";
import { Empty, Panel, ScreenHead, Skeleton } from "../components/ui";
import { local } from "../lib/store";
import { date, money, num } from "../lib/format";
import { useKids, type Kid } from "../lib/queries";

export function Kids() {
  const kids = useKids();
  const loc = useLocation();
  if (kids.isLoading) return <Skeleton w={240} />;
  if (!kids.data?.length)
    return (
      <>
        <ScreenHead title="Kids" />
        <Panel bodyless>
          <Empty title="No children added" action={<Link className="btn primary" to={{ pathname: "/settings/people", search: "?new=1" + (loc.search ? "&" + loc.search.slice(1) : "") }}>Add a child</Link>}>
            Add each child as a person (role “Child”) and give their Nordnet account them as owner.
          </Empty>
        </Panel>
      </>
    );
  return (
    <>
      <ScreenHead title="Kids" sub="Each child's own accounts · gains and dividends are taxed to the child" />
      <div className="kid-grid">
        {kids.data.map((k) => (
          <KidCard key={k.person_id} k={k} />
        ))}
      </div>
    </>
  );
}

function KidCard({ k }: { k: Kid }) {
  const c1 = useCssVar("--c1");
  const [monthly, setMonthly] = useState(() => local.get(`kid.${k.person_id}.monthly`) ?? "50");
  const [ret, setRet] = useState(() => local.get(`kid.${k.person_id}.return`) ?? "5");
  const born = k.birth_date ? new Date(k.birth_date) : null;
  const age = born ? (Date.now() - born.getTime()) / (365.25 * 86400e3) : null;
  const v = num(k.value?.value ?? 0);
  const proj = (at: number) => {
    if (age == null) return null;
    const years = Math.max(0, at - age);
    const r = num(ret) / 100 / 12;
    const m = num(monthly) || 0;
    const n = Math.round(years * 12);
    let w = v;
    for (let i = 0; i < n; i++) w = w * (1 + r) + m;
    return w;
  };
  const series = k.series ?? [];
  return (
    <section className="panel">
      <div className="panel-h">
        <h2>{k.name}</h2>
        <span className="meta">{age != null ? `age ${Math.floor(age)}` : "add date of birth"}</span>
      </div>
      {!k.value ? (
        <Empty title="No account yet">Set {k.name} as the owner of their Nordnet account in Settings → Accounts.</Empty>
      ) : (
        <>
          <div className="kpis" style={{ gridTemplateColumns: "repeat(4,minmax(0,1fr))" }}>
            <div className="kpi"><span className="label">Value</span><span className="v"><Figure f={k.value} /></span></div>
            <div className="kpi"><span className="label">Put in</span><span className="v"><Figure f={k.contributions} /></span></div>
            <div className="kpi"><span className="label">Gain</span><span className="v"><Figure f={k.gain} delta /></span></div>
            <div className="kpi"><span className="label">Return p.a.</span><span className="v"><Figure f={k.twr_max} kind="pct" delta /></span></div>
          </div>
          <div className="panel-b">
            <LineChart label={`${k.name} portfolio value`} x={series.map((r) => r.date)} series={[{ name: "Value", values: series.map((r) => num(r.value)), color: c1, area: true }]} height={140} />
          </div>
          <div className="panel-b" style={{ borderTop: "1px solid var(--line)" }}>
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "end" }}>
              <div className="field">
                <label htmlFor={`m${k.person_id}`}>Monthly saving €</label>
                <input id={`m${k.person_id}`} className="input mono" style={{ width: 100 }} value={monthly} onChange={(e) => (setMonthly(e.target.value), local.set(`kid.${k.person_id}.monthly`, e.target.value))} />
              </div>
              <div className="field">
                <label htmlFor={`r${k.person_id}`}>Return % p.a.</label>
                <input id={`r${k.person_id}`} className="input mono" style={{ width: 80 }} value={ret} onChange={(e) => (setRet(e.target.value), local.set(`kid.${k.person_id}.return`, e.target.value))} />
              </div>
            </div>
            <dl className="kv" style={{ marginTop: 10 }}>
              {[18, 25, 30].map((a) => {
                const p = proj(a);
                return (
                  <div key={a} style={{ display: "contents" }}>
                    <dt>At {a}{born ? ` (${born.getFullYear() + a})` : ""}</dt>
                    <dd>{p == null ? "—" : `${money(p)} est.`}</dd>
                  </div>
                );
              })}
            </dl>
            <p style={{ fontSize: 11, color: "var(--muted)", margin: "8px 0 0" }}>Nominal projection, before tax.</p>
          </div>
          <div className="panel-b" style={{ borderTop: "1px solid var(--line)" }}>
            <Gifts entityId={k.entity_id} />
          </div>
        </>
      )}
    </section>
  );
}


function Gifts({ entityId }: { entityId: number }) {
  const qc = useQueryClient();
  const g = useQuery({ queryKey: ["gifts", entityId], queryFn: () => api<{ windows: Record<string, { donor: string; total: string; limit: string; left: string; tax_if_now: string; window_frees: string }[]> }>(`/api/tax/gifts?recipient=${entityId}`) });
  const [f, setF] = useState({ donor: "", amount: "", date: new Date().toISOString().slice(0, 10) });
  const add = async () => {
    await api("/api/tax/gifts", { body: { donor: f.donor, recipient_entity_id: entityId, date: f.date, amount: f.amount.replace(",", ".") } });
    setF({ ...f, amount: "" });
    qc.invalidateQueries({ queryKey: ["gifts", entityId] });
  };
  const w = g.data?.windows[String(entityId)] ?? [];
  return (
    <>
      <span className="label">Gift-tax window · last 3 years per donor</span>
      {w.map((x) => (
        <div key={x.donor} style={{ marginTop: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}><span>{x.donor}</span><span className="num">{money(x.total)} / {money(x.limit)}</span></div>
          <div className="meter"><span style={{ width: `${Math.min(100, (Number(x.total) / Number(x.limit)) * 100)}%`, background: Number(x.left) <= 0 ? "var(--warn)" : "var(--c1)" }} /></div>
          <div style={{ fontSize: 11, color: "var(--muted)" }}>{Number(x.left) > 0 ? `${money(x.left)} left tax-free` : `gift tax if declared now ≈ ${money(x.tax_if_now)}`} · oldest gift leaves the window {date(x.window_frees)}</div>
        </div>
      ))}
      <form onSubmit={(e) => (e.preventDefault(), add())} style={{ display: "flex", gap: 6, marginTop: 10, flexWrap: "wrap" }}>
        <input className="input" style={{ width: 140 }} placeholder="Donor" value={f.donor} onChange={(e) => setF({ ...f, donor: e.target.value })} required aria-label="Donor" />
        <input className="input mono" style={{ width: 100 }} placeholder="€" value={f.amount} onChange={(e) => setF({ ...f, amount: e.target.value })} required aria-label="Amount" />
        <input className="input" type="date" style={{ width: 150 }} value={f.date} onChange={(e) => setF({ ...f, date: e.target.value })} aria-label="Date" />
        <button className="btn">Record gift</button>
      </form>
    </>
  );
}
