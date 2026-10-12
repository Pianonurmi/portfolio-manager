import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { Link, Route, Routes, useLocation, useSearchParams } from "react-router";
import { useToast } from "../components/Toasts";
import { ConnectionCards } from "../components/ConnectionCards";
import { YnabSettings } from "../components/YnabSettings";
import { Chip, Empty, Field, Modal, Panel, ScreenHead, statusTone } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { date, dateTime, quantity } from "../lib/format";
import type { Account, DuplicateGroup, DuplicateRow, EntityRow, Institution, Instrument, Job, MergeResult, Person, PriceCoverage } from "../lib/types";

const TABS = [
  ["people", "People"],
  ["accounts", "Accounts"],
  ["instruments", "Instruments"],
  ["jobs", "Jobs"],
  ["connections", "Connections"],
  ["rules", "Rules"],
  ["security", "Security"],
] as const;

export function Settings() {
  const loc = useLocation();
  const active = loc.pathname.split("/")[2] ?? "people";
  return (
    <>
      <ScreenHead title="Settings & jobs" sub="People, accounts, data sources and system status" />
      <nav className="subnav">
        {TABS.map(([id, label]) => (
          <Link key={id} to={{ pathname: `/settings/${id}`, search: loc.search }} aria-current={active === id ? "page" : undefined}>
            {label}
          </Link>
        ))}
      </nav>
      <Routes>
        <Route index element={<People />} />
        <Route path="people" element={<People />} />
        <Route path="accounts" element={<Accounts />} />
        <Route path="instruments" element={<Instruments />} />
        <Route path="jobs" element={<Jobs />} />
        <Route path="connections" element={<Connections />} />
        <Route path="rules" element={<Rules />} />
        <Route path="security" element={<Security />} />
      </Routes>
    </>
  );
}

function useNewParam(): [boolean, () => void] {
  const [params, setParams] = useSearchParams();
  const isNew = params.get("new") === "1";
  return [isNew, () => setParams((p) => (p.delete("new"), p), { replace: true })];
}

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : "Something went wrong";
}

// ---- people ----------------------------------------------------------------------------------

function People() {
  const qc = useQueryClient();
  const people = useQuery({ queryKey: ["persons"], queryFn: () => api<Person[]>("/api/persons") });
  const [isNew, clearNew] = useNewParam();
  const [edit, setEdit] = useState<Person | "new" | null>(null);
  useEffect(() => {
    if (isNew) setEdit("new");
  }, [isNew]);
  const close = () => (setEdit(null), clearNew());
  return (
    <Panel title="People" meta={<button className="btn" onClick={() => setEdit("new")}>Add person</button>} bodyless>
      {people.data?.length === 0 && (
        <Empty title="Add the people in your household" action={<button className="btn primary" onClick={() => setEdit("new")}>Add yourself first</button>}>
          Every account and asset is owned by one or more people. Add yourself, your spouse (for shared assets) and the kids.
        </Empty>
      )}
      {!!people.data?.length && (
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Role</th>
                <th className="hide-sm">Born</th>
                <th>Tracked</th>
              </tr>
            </thead>
            <tbody>
              {people.data.map((p) => (
                <tr key={p.id} onClick={() => setEdit(p)}>
                  <td>
                    {p.name}
                    {p.is_self && <span style={{ color: "var(--muted)" }}> · you</span>}
                  </td>
                  <td>{p.role === "child" ? "Child" : "Adult"}</td>
                  <td className="num hide-sm">{date(p.birth_date)}</td>
                  <td>{p.tracking === "full" ? <Chip tone="ok">all finances</Chip> : <Chip>shared assets only</Chip>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {edit && <PersonForm person={edit === "new" ? null : edit} first={!people.data?.length} onClose={close} onSaved={() => (qc.invalidateQueries({ queryKey: ["persons"] }), qc.invalidateQueries({ queryKey: ["entities"] }), close())} />}
    </Panel>
  );
}

function PersonForm({ person, first, onClose, onSaved }: { person: Person | null; first: boolean; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({
    name: person?.name ?? "",
    role: person?.role ?? "adult",
    birth_date: person?.birth_date ?? "",
    tracking: person?.tracking ?? "full",
    is_self: person?.is_self ?? first,
  });
  const { toast } = useToast();
  const m = useMutation({
    mutationFn: () =>
      api(person ? `/api/persons/${person.id}` : "/api/persons", { method: person ? "PUT" : "POST", body: { ...f, birth_date: f.birth_date || null } }),
    onSuccess: () => (toast(person ? "Saved" : `Added ${f.name}`), onSaved()),
  });
  const submit = (e: FormEvent) => (e.preventDefault(), m.mutate());
  return (
    <Modal title={person ? `Edit ${person.name}` : "Add person"} onClose={onClose}>
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div className="form-grid">
          <Field label="Name" id="p-name">
            <input id="p-name" className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required />
          </Field>
          <Field label="Role" id="p-role">
            <select id="p-role" className="select input" value={f.role} onChange={(e) => setF({ ...f, role: e.target.value as "adult" | "child" })}>
              <option value="adult">Adult</option>
              <option value="child">Child</option>
            </select>
          </Field>
          <Field label="Date of birth" id="p-born" hint="Used for retirement age, kids' projections and pension">
            <input id="p-born" className="input" type="date" value={f.birth_date} onChange={(e) => setF({ ...f, birth_date: e.target.value })} />
          </Field>
          <Field label="What to track" id="p-track">
            <select id="p-track" className="select input" value={f.tracking} onChange={(e) => setF({ ...f, tracking: e.target.value as "full" | "shared" })}>
              <option value="full">All of their finances</option>
              <option value="shared">Only assets shared with others</option>
            </select>
          </Field>
        </div>
        <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13 }}>
          <input type="checkbox" checked={f.is_self} onChange={(e) => setF({ ...f, is_self: e.target.checked })} /> This is me
        </label>
        {m.error && <p className="err">{errMsg(m.error)}</p>}
        <div className="form-actions">
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={m.isPending}>{person ? "Save" : "Add person"}</button>
        </div>
      </form>
    </Modal>
  );
}

// ---- accounts --------------------------------------------------------------------------------

function Accounts() {
  const qc = useQueryClient();
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/api/accounts") });
  const [isNew, clearNew] = useNewParam();
  const [edit, setEdit] = useState<Account | "new" | null>(null);
  useEffect(() => {
    if (isNew) setEdit("new");
  }, [isNew]);
  const close = () => (setEdit(null), clearNew());
  return (
    <Panel title="Accounts" meta={<button className="btn" onClick={() => setEdit("new")}>Add account</button>} bodyless>
      {accounts.data?.length === 0 && (
        <Empty title="No accounts yet" action={<button className="btn primary" onClick={() => setEdit("new")}>Add an account</button>}>
          Add brokerage accounts (Nordnet AOT, OST), bank and savings accounts, loans and credit cards. Imports attach to these.
        </Empty>
      )}
      {!!accounts.data?.length && (
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>Account</th>
                <th className="hide-sm">Type</th>
                <th className="hide-sm">Institution</th>
                <th>Owners</th>
                <th className="hide-sm">Opened</th>
              </tr>
            </thead>
            <tbody>
              {accounts.data.map((a) => (
                <tr key={a.id} onClick={() => setEdit(a)}>
                  <td>
                    <div className="inst">
                      <span>{a.name}</span>
                      <small>
                        {a.currency}
                        {a.external_ref ? ` · ${a.external_ref}` : ""}
                        {a.closed_on ? ` · closed ${date(a.closed_on)}` : ""}
                      </small>
                    </div>
                  </td>
                  <td className="hide-sm">{a.kind_label}</td>
                  <td className="hide-sm">{a.institution ?? "—"}</td>
                  <td>{a.owners.length ? a.owners.map((o) => `${o.name} ${Math.round(Number(o.share) * 100)} %`).join(" · ") : <Chip tone="warn">no owner</Chip>}</td>
                  <td className="num hide-sm">{date(a.opened_on)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {edit && <AccountForm account={edit === "new" ? null : edit} onClose={close} onSaved={() => (qc.invalidateQueries({ queryKey: ["accounts"] }), close())} />}
    </Panel>
  );
}

function AccountForm({ account, onClose, onSaved }: { account: Account | null; onClose: () => void; onSaved: () => void }) {
  const kinds = useQuery({ queryKey: ["account-kinds"], queryFn: () => api<Record<string, string>>("/api/account-kinds") });
  const insts = useQuery({ queryKey: ["institutions"], queryFn: () => api<Institution[]>("/api/institutions") });
  const ents = useQuery({ queryKey: ["entities"], queryFn: () => api<EntityRow[]>("/api/entities") });
  const qc = useQueryClient();
  const { toast } = useToast();
  const [f, setF] = useState({
    name: account?.name ?? "",
    kind: account?.kind ?? "aot",
    institution_id: account?.institution_id ?? null,
    currency: account?.currency ?? "EUR",
    opened_on: account?.opened_on ?? "",
    closed_on: account?.closed_on ?? "",
    external_ref: account?.external_ref ?? "",
    notes: account?.notes ?? "",
  });
  const [owners, setOwners] = useState<{ entity_id: number; pct: string }[]>(
    account?.owners.map((o) => ({ entity_id: o.entity_id, pct: String(Math.round(Number(o.share) * 10000) / 100) })) ?? [],
  );
  const [ownersFrom, setOwnersFrom] = useState("");
  const [newInst, setNewInst] = useState("");
  useEffect(() => {
    if (!account && owners.length === 0 && ents.data?.length) {
      const me = ents.data.find((e) => e.is_self) ?? ents.data[0];
      if (me) setOwners([{ entity_id: me.id, pct: "100" }]);
    }
  }, [ents.data, account, owners.length]);
  const total = owners.reduce((s, o) => s + Number(o.pct || 0), 0);
  const m = useMutation({
    mutationFn: async () => {
      let institution_id = f.institution_id;
      if (newInst.trim()) {
        const created = await api<Institution>("/api/institutions", { body: { name: newInst.trim(), kind: "bank", country: "FI" } });
        institution_id = created.id;
        qc.invalidateQueries({ queryKey: ["institutions"] });
      }
      const body = {
        ...f,
        institution_id,
        opened_on: f.opened_on || null,
        closed_on: f.closed_on || null,
        external_ref: f.external_ref || null,
        notes: f.notes || null,
        owners: owners.filter((o) => Number(o.pct) > 0).map((o) => ({ entity_id: o.entity_id, share: (Number(o.pct) / 100).toFixed(6) })),
        owners_from: ownersFrom || null,
      };
      return api(account ? `/api/accounts/${account.id}` : "/api/accounts", { method: account ? "PUT" : "POST", body });
    },
    onSuccess: () => (toast(account ? "Account saved" : `Added ${f.name}`), onSaved()),
  });
  const submit = (e: FormEvent) => (e.preventDefault(), m.mutate());
  return (
    <Modal title={account ? `Edit ${account.name}` : "Add account"} onClose={onClose}>
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div className="form-grid">
          <Field label="Name" id="a-name">
            <input id="a-name" className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Nordnet AOT" required />
          </Field>
          <Field label="Type" id="a-kind">
            <select id="a-kind" className="select input" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>
              {Object.entries(kinds.data ?? {}).map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </select>
          </Field>
          <Field label="Institution" id="a-inst">
            <select id="a-inst" className="select input" value={f.institution_id ?? ""} onChange={(e) => setF({ ...f, institution_id: e.target.value ? Number(e.target.value) : null })}>
              <option value="">—</option>
              {insts.data?.map((i) => (
                <option key={i.id} value={i.id}>{i.name}</option>
              ))}
            </select>
          </Field>
          <Field label="…or add institution" id="a-newinst">
            <input id="a-newinst" className="input" value={newInst} onChange={(e) => setNewInst(e.target.value)} placeholder="Nordnet" />
          </Field>
          <Field label="Currency" id="a-ccy">
            <input id="a-ccy" className="input mono" value={f.currency} onChange={(e) => setF({ ...f, currency: e.target.value.toUpperCase() })} maxLength={4} />
          </Field>
          <Field label="Account number / reference" id="a-ref" hint="Used to match imports to this account">
            <input id="a-ref" className="input mono" value={f.external_ref} onChange={(e) => setF({ ...f, external_ref: e.target.value })} />
          </Field>
          <Field label="Opened" id="a-open">
            <input id="a-open" className="input" type="date" value={f.opened_on} onChange={(e) => setF({ ...f, opened_on: e.target.value })} />
          </Field>
          <Field label="Closed" id="a-close">
            <input id="a-close" className="input" type="date" value={f.closed_on} onChange={(e) => setF({ ...f, closed_on: e.target.value })} />
          </Field>
        </div>
        <div>
          <span className="label">Owners</span>
          {owners.map((o, i) => (
            <div key={i} style={{ display: "flex", gap: 8, marginTop: 6, alignItems: "center" }}>
              <select className="select input" aria-label="Owner" value={o.entity_id} onChange={(e) => setOwners(owners.map((x, j) => (j === i ? { ...x, entity_id: Number(e.target.value) } : x)))}>
                {ents.data?.map((e) => (
                  <option key={e.id} value={e.id}>{e.name}</option>
                ))}
              </select>
              <input className="input mono" aria-label="Share %" style={{ width: 90, textAlign: "right" }} value={o.pct} onChange={(e) => setOwners(owners.map((x, j) => (j === i ? { ...x, pct: e.target.value } : x)))} />
              <span style={{ color: "var(--muted)" }}>%</span>
              <button type="button" className="x" aria-label="Remove owner" onClick={() => setOwners(owners.filter((_, j) => j !== i))}>×</button>
            </div>
          ))}
          <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center", flexWrap: "wrap" }}>
            <button type="button" className="btn" onClick={() => ents.data?.[0] && setOwners([...owners, { entity_id: ents.data[0].id, pct: "" }])}>Add owner</button>
            <span className="num" style={{ color: Math.abs(total - 100) < 0.001 || owners.length === 0 ? "var(--muted)" : "var(--loss)" }}>Total {total.toLocaleString("fi-FI")} %</span>
            {account && (
              <span className="field" style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
                <label htmlFor="a-from">Shares valid from</label>
                <input id="a-from" className="input" type="date" style={{ width: 160 }} value={ownersFrom} onChange={(e) => setOwnersFrom(e.target.value)} />
              </span>
            )}
          </div>
        </div>
        <Field label="Notes" id="a-notes">
          <textarea id="a-notes" className="textarea" rows={2} value={f.notes} onChange={(e) => setF({ ...f, notes: e.target.value })} />
        </Field>
        {m.error && <p className="err">{errMsg(m.error)}</p>}
        <div className="form-actions">
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={m.isPending}>{account ? "Save" : "Add account"}</button>
        </div>
      </form>
    </Modal>
  );
}

// ---- instruments -----------------------------------------------------------------------------

function Instruments() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const list = useQuery({ queryKey: ["instruments", q], queryFn: () => api<Instrument[]>(`/api/instruments${q ? `?q=${encodeURIComponent(q)}` : ""}`) });
  const [edit, setEdit] = useState<Instrument | "new" | null>(null);
  return (
    <>
    <DuplicatesPanel />
    <PriceCoveragePanel onEdit={async (id) => setEdit((await api<Instrument[]>("/api/instruments")).find((i) => i.id === id) ?? null)} />
    <Panel
      title="Instruments"
      meta={
        <>
          <input className="input" style={{ width: 200 }} placeholder="Filter" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filter instruments" />
          <button className="btn" onClick={() => setEdit("new")}>Add instrument</button>
        </>
      }
      bodyless
    >
      {list.data?.length === 0 && !q && <Empty title="No instruments yet">Instruments are created automatically when you import transactions. You can also add them by hand.</Empty>}
      {!!list.data?.length && (
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th className="hide-sm">ISIN</th>
                <th className="hide-sm">Symbol</th>
                <th className="hide-sm">Price source symbol</th>
                <th>Ccy</th>
              </tr>
            </thead>
            <tbody>
              {list.data.map((i) => (
                <tr key={i.id} onClick={() => setEdit(i)}>
                  <td>{i.name}</td>
                  <td>{i.type}</td>
                  <td className="num hide-sm">{i.isin ?? "—"}</td>
                  <td className="num hide-sm">{i.symbol ?? "—"}</td>
                  <td className="num hide-sm">{i.price_symbol ?? <Chip tone="warn">none</Chip>}</td>
                  <td className="num">{i.currency}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {edit && <InstrumentForm inst={edit === "new" ? null : edit} onClose={() => setEdit(null)} onSaved={() => (qc.invalidateQueries({ queryKey: ["instruments"] }), qc.invalidateQueries({ queryKey: ["price-coverage"] }), setEdit(null))} />}
    </Panel>
    </>
  );
}

function DuplicatesPanel() {
  const qc = useQueryClient();
  const { toast } = useToast();
  const dups = useQuery({ queryKey: ["instrument-duplicates"], queryFn: () => api<DuplicateGroup[]>("/api/instruments/duplicates") });
  const [pending, setPending] = useState<{ keep: DuplicateRow; drop: DuplicateRow } | null>(null);
  const merge = useMutation({
    mutationFn: (p: { keep: DuplicateRow; drop: DuplicateRow }) => api<MergeResult>(`/api/instruments/${p.keep.id}/merge`, { body: { drop_id: p.drop.id } }),
    onSuccess: (r) => {
      toast(`Merged: ${Object.entries(r.moved).map(([t, n]) => `${n} ${t.replace("_", " ")}`).join(", ") || "nothing to move"}`);
      setPending(null);
      qc.invalidateQueries(); // holdings, X-ray, risk, price coverage and instrument lists all change shape
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Merge failed"),
  });
  const groups = dups.data ?? [];
  if (!groups.length) return null;
  return (
    <Panel title="Possible duplicates" meta={<Chip tone="warn">{groups.length}</Chip>} bodyless>
      {groups.map((g) => {
        const keep = g.instruments.find((i) => i.id === g.keep_id)!;
        return (
          <div key={g.keep_id} style={{ padding: "10px 16px", borderTop: "1px solid var(--line)" }}>
            <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>{g.reasons.join(" · ")}</div>
            <div className="tbl-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Type</th>
                    <th className="hide-sm">ISIN</th>
                    <th>Ccy</th>
                    <th>Held</th>
                    <th className="hide-sm">Postings</th>
                    <th className="hide-sm">Price source</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {g.instruments.map((i) => (
                    <tr key={i.id}>
                      <td>{i.name} <span className="num" style={{ color: "var(--muted)" }}>#{i.id}</span>{i.id === g.keep_id && <> <Chip tone="ok">keep</Chip></>}</td>
                      <td>{i.type}</td>
                      <td className="num hide-sm">{i.isin ?? "—"}</td>
                      <td className="num">{i.currency}</td>
                      <td className="num">{quantity(i.quantity)}</td>
                      <td className="num hide-sm">{i.postings}</td>
                      <td className="hide-sm">{i.price_source ?? "—"}</td>
                      <td>
                        {i.id !== g.keep_id && (
                          <button className="btn" disabled={!i.mergeable} title={i.mergeable ? undefined : "Different currency"} onClick={() => setPending({ keep, drop: i })}>
                            Merge into #{keep.id}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}
      {pending && (
        <Modal title={`Merge ${pending.drop.name} into ${pending.keep.name}`} onClose={() => setPending(null)}>
          <p>These move from <b>#{pending.drop.id}</b> to <b>#{pending.keep.id}</b>, and #{pending.drop.id} is then deleted:</p>
          <ul style={{ margin: "8px 0 8px 18px" }}>
            <li>{pending.drop.postings} ledger postings ({quantity(pending.drop.quantity)} units held)</li>
            <li>prices, dividends, profiles, import rows and name mappings (the kept instrument's own win where both have one)</li>
            {pending.drop.isin && !pending.keep.isin && <li>the ISIN {pending.drop.isin}</li>}
          </ul>
          <p style={{ color: "var(--muted)", fontSize: 12 }}>This cannot be undone here. A database backup is taken nightly.</p>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 12 }}>
            <button className="btn" onClick={() => setPending(null)}>Cancel</button>
            <button className="btn primary" onClick={() => merge.mutate(pending)} disabled={merge.isPending}>Merge</button>
          </div>
        </Modal>
      )}
    </Panel>
  );
}

function PriceCoveragePanel({ onEdit }: { onEdit: (id: number) => void }) {
  const { toast } = useToast();
  const cov = useQuery({ queryKey: ["price-coverage"], queryFn: () => api<PriceCoverage[]>("/api/portfolio/price-coverage") });
  const fetchNow = useMutation({
    mutationFn: () => api("/api/jobs/prices.backfill/run", { method: "POST" }),
    onSuccess: () => toast("Price fetch queued; refresh in a minute"),
  });
  const rows = cov.data ?? [];
  const count = (s: PriceCoverage["status"]) => rows.filter((r) => r.status === s).length;
  const problems = rows.filter((r) => r.status !== "ok");
  return (
    <Panel
      title="Price coverage"
      meta={
        <>
          {!!rows.length && (
            <>
              <Chip tone="ok">{count("ok")} ok</Chip> <Chip tone={count("no_feed") ? "warn" : undefined}>{count("no_feed")} no feed</Chip>{" "}
              <Chip tone={count("stale") ? "warn" : undefined}>{count("stale")} stale</Chip>{" "}
              <Chip tone={count("missing") ? "bad" : undefined}>{count("missing")} missing</Chip>
            </>
          )}
          <button className="btn" onClick={() => fetchNow.mutate()} disabled={fetchNow.isPending}>Fetch prices now</button>
        </>
      }
      bodyless
    >
      {cov.data && !problems.length && <Empty title="Every holding has a recent price">{rows.length} holdings priced.</Empty>}
      {!!problems.length && (
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th className="hide-sm">Source / symbol</th>
                <th>Last price</th>
                <th>Reason</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {problems.map((r) => (
                <tr key={r.id}>
                  <td>{r.name}</td>
                  <td>{r.type}</td>
                  <td className="num hide-sm">{r.price_source ?? "—"}{r.symbol ? ` · ${r.symbol}` : ""}</td>
                  <td className="num">{r.last_date ? <Chip tone="warn">{date(r.last_date)}</Chip> : <Chip tone="bad">none</Chip>}</td>
                  <td>{r.reason ?? "—"}</td>
                  <td><button className="btn" onClick={() => onEdit(r.id)}>Edit</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function InstrumentForm({ inst, onClose, onSaved }: { inst: Instrument | null; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({
    type: inst?.type ?? "stock",
    name: inst?.name ?? "",
    isin: inst?.isin ?? "",
    symbol: inst?.symbol ?? "",
    mic: inst?.mic ?? "",
    currency: inst?.currency ?? "EUR",
    price_symbol: inst?.price_symbol ?? "",
    asset_class: inst?.asset_class ?? "equity",
  });
  const m = useMutation({
    mutationFn: () => {
      const body = Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v === "" ? null : v]));
      return api(inst ? `/api/instruments/${inst.id}` : "/api/instruments", { method: inst ? "PUT" : "POST", body: { ...body, currency: f.currency } });
    },
    onSuccess: onSaved,
  });
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return (
    <Modal title={inst ? inst.name : "Add instrument"} onClose={onClose}>
      <form onSubmit={(e) => (e.preventDefault(), m.mutate())} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div className="form-grid">
          <Field label="Name" id="i-name"><input id="i-name" className="input" value={f.name} onChange={set("name")} required /></Field>
          <Field label="Type" id="i-type">
            <select id="i-type" className="select input" value={f.type} onChange={set("type")}>
              {["stock", "etf", "fund", "bond", "crypto", "other"].map((t) => <option key={t}>{t}</option>)}
            </select>
          </Field>
          <Field label="Asset class" id="i-cls">
            <select id="i-cls" className="select input" value={f.asset_class} onChange={set("asset_class")}>
              {["equity", "fixed_income", "mixed", "cash", "crypto", "real_estate", "other"].map((t) => <option key={t}>{t}</option>)}
            </select>
          </Field>
          <Field label="ISIN" id="i-isin"><input id="i-isin" className="input mono" value={f.isin} onChange={set("isin")} /></Field>
          <Field label="Symbol" id="i-sym"><input id="i-sym" className="input mono" value={f.symbol} onChange={set("symbol")} /></Field>
          <Field label="Exchange (MIC)" id="i-mic"><input id="i-mic" className="input mono" value={f.mic} onChange={set("mic")} placeholder="XHEL" /></Field>
          <Field label="Currency" id="i-ccy"><input id="i-ccy" className="input mono" value={f.currency} onChange={set("currency")} /></Field>
          <Field label="Price source symbol" id="i-ps" hint="e.g. SAMPO.HE, IWDA.AS, BTC-EUR">
            <input id="i-ps" className="input mono" value={f.price_symbol} onChange={set("price_symbol")} />
          </Field>
        </div>
        {m.error && <p className="err">{errMsg(m.error)}</p>}
        <div className="form-actions">
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={m.isPending}>Save</button>
        </div>
      </form>
    </Modal>
  );
}

// ---- jobs, connections, rules, security ----------------------------------------------------

type WorkerState = { api_version: string; version: string | null; started_at: string | null; heartbeat_at: string | null; alive: boolean; current: boolean };

function Jobs() {
  const qc = useQueryClient();
  const { toast } = useToast();
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => api<Job[]>("/api/jobs"), refetchInterval: 5000 });
  const worker = useQuery({ queryKey: ["worker"], queryFn: () => api<WorkerState>("/api/worker"), refetchInterval: 30_000 });
  const w = worker.data;
  const [log, setLog] = useState<number | null>(null);
  const run = async (j: Job) => {
    await api(`/api/jobs/${j.name}/run`, { method: "POST" });
    toast(`${j.title} queued`);
    qc.invalidateQueries({ queryKey: ["jobs"] });
  };
  return (
    <Panel title="Jobs" meta="Scheduled in Europe/Helsinki time" bodyless>
      {w && (
        <p className={w.alive && w.current ? "sub" : "err"} style={{ padding: "8px 12px", margin: 0 }}>
          {!w.version ? "The worker has not started yet." : !w.alive
            ? `Worker is not running: last heartbeat ${dateTime(w.heartbeat_at)}. Scheduled jobs are not running.`
            : !w.current
              ? `Worker runs ${w.version}, the app ${w.api_version}: redeploy with docker compose up -d --build.`
              : `Worker ${w.version} running since ${dateTime(w.started_at)} · heartbeat ${dateTime(w.heartbeat_at)}`}
        </p>
      )}
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th>Job</th>
              <th>Status</th>
              <th className="hide-sm">Last run</th>
              <th className="hide-sm">Schedule</th>
              <th className="hide-sm">Result</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {jobs.data?.map((j) => (
              <tr key={j.name} onClick={() => j.last_run_id && setLog(j.last_run_id)}>
                <td>{j.title}</td>
                <td>{j.last_status ? <Chip tone={statusTone(j.last_status)}>{j.last_status}</Chip> : <Chip>never run</Chip>}</td>
                <td className="num hide-sm">{dateTime(j.last_finished ?? j.last_started)}</td>
                <td className="num hide-sm">{j.cron ?? "manual"}</td>
                <td className="hide-sm" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis" }}>{j.last_summary ?? ""}</td>
                <td className="r">
                  <button className="btn" onClick={(e) => (e.stopPropagation(), run(j))}>Run now</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {log && <RunLog id={log} onClose={() => setLog(null)} />}
    </Panel>
  );
}

function RunLog({ id, onClose }: { id: number; onClose: () => void }) {
  const r = useQuery({ queryKey: ["run", id], queryFn: () => api<{ job: string; status: string; summary: string | null; log: string | null }>(`/api/jobs/runs/${id}`) });
  return (
    <Modal title={`Run #${id}`} onClose={onClose}>
      {r.data && (
        <>
          <p style={{ marginTop: 0 }}>
            <Chip tone={statusTone(r.data.status)}>{r.data.status}</Chip> {r.data.summary}
          </p>
          <pre className="mono" style={{ fontSize: 11, whiteSpace: "pre-wrap", maxHeight: 360, overflow: "auto", background: "var(--bg)", padding: 10, borderRadius: 6 }}>{r.data.log || "No log output"}</pre>
        </>
      )}
    </Modal>
  );
}

function Connections() {
  return (
    <div className="grid">
      <YnabSettings />
      <ConnectionCards />
      <OutboundPanel />
    </div>
  );
}

function OutboundPanel() {
  const out = useQuery({ queryKey: ["outbound"], queryFn: () => api<{ host: string; allowed: boolean; last_call: string | null; calls: number; blocked?: number }[]>("/api/outbound") });
  return (
    <Panel title="Outbound connections" meta="Every other host is blocked and logged" bodyless>
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th>Host</th>
              <th>Status</th>
              <th className="r">Calls</th>
              <th>Last call</th>
            </tr>
          </thead>
          <tbody>
            {out.data?.map((h) => (
              <tr key={h.host}>
                <td className="num">{h.host}</td>
                <td>{h.allowed ? <Chip tone="ok">allowed</Chip> : <Chip tone="bad">blocked</Chip>}</td>
                <td className="r num">{h.calls ?? 0}</td>
                <td className="num">{dateTime(h.last_call)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function DataExport() {
  return (
    <Panel title="Your data">
      <p style={{ margin: "0 0 10px", color: "var(--fg-2)" }}>
        Download everything you have entered or imported as JSON, plus the original import files. Nothing is locked in.
      </p>
      <a className="btn" href="/api/export" download>Export all data (.zip)</a>
    </Panel>
  );
}

function Rules() {
  const rules = useQuery({ queryKey: ["rules"], queryFn: () => api<{ domain: string; year: number; version: string; loaded_at: string }[]>("/api/rules") });
  return (
    <Panel title="Rule tables" meta="Tax parameters used for estimates · review every January" bodyless>
      {rules.data?.length === 0 && <Empty title="No rule tables loaded">Run the “Load rule tables” job.</Empty>}
      <div className="tbl-wrap">
        <table>
          <tbody>
            {rules.data?.map((r) => (
              <tr key={`${r.domain}${r.year}${r.version}`}>
                <td>{r.domain.toUpperCase()} {r.year}</td>
                <td className="num">version {r.version}</td>
                <td className="num">{dateTime(r.loaded_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function Security() {
  const { toast } = useToast();
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<{ username: string; totp_enabled: boolean }>("/api/auth/me") });
  const [pw, setPw] = useState({ current: "", next: "" });
  const [totp, setTotp] = useState<{ secret: string; uri: string } | null>(null);
  const [code, setCode] = useState("");
  const qc = useQueryClient();
  const changePw = useMutation({ mutationFn: () => api("/api/auth/password", { body: { current: pw.current, new: pw.next } }), onSuccess: () => (toast("Password changed"), setPw({ current: "", next: "" })) });
  const enable = useMutation({
    mutationFn: () => api("/api/auth/totp/enable", { body: { secret: totp?.secret, code } }),
    onSuccess: () => (toast("Two-factor sign-in is on"), setTotp(null), qc.invalidateQueries({ queryKey: ["me"] })),
  });
  return (
    <div className="grid g-2">
      <DataExport />
      <Panel title="Password">
        <form onSubmit={(e) => (e.preventDefault(), changePw.mutate())} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <Field label="Current password" id="s-cur"><input id="s-cur" className="input" type="password" autoComplete="current-password" value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} required /></Field>
          <Field label="New password" id="s-new" hint="At least 12 characters"><input id="s-new" className="input" type="password" autoComplete="new-password" minLength={12} value={pw.next} onChange={(e) => setPw({ ...pw, next: e.target.value })} required /></Field>
          {changePw.error && <p className="err">{errMsg(changePw.error)}</p>}
          <div className="form-actions"><button className="btn primary" disabled={changePw.isPending}>Change password</button></div>
        </form>
      </Panel>
      <Panel title="Two-factor sign-in" meta={me.data?.totp_enabled ? <Chip tone="ok">on</Chip> : <Chip>off</Chip>}>
        {me.data?.totp_enabled ? (
          <p style={{ margin: 0, color: "var(--fg-2)" }}>Sign-in asks for a code from your authenticator app.</p>
        ) : totp ? (
          <form onSubmit={(e) => (e.preventDefault(), enable.mutate())} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <p style={{ margin: 0, color: "var(--fg-2)" }}>Add this key to your authenticator app (manual entry), then enter the 6-digit code it shows.</p>
            <code className="mono" style={{ userSelect: "all", overflowWrap: "anywhere", background: "var(--bg)", padding: 8, borderRadius: 6 }}>{totp.secret}</code>
            <Field label="Code" id="s-code"><input id="s-code" className="input mono" inputMode="numeric" value={code} onChange={(e) => setCode(e.target.value)} required /></Field>
            {enable.error && <p className="err">{errMsg(enable.error)}</p>}
            <div className="form-actions"><button className="btn primary" disabled={enable.isPending}>Turn on</button></div>
          </form>
        ) : (
          <button className="btn" onClick={async () => setTotp(await api("/api/auth/totp/setup", { method: "POST" }))}>Set up authenticator app</button>
        )}
      </Panel>
    </div>
  );
}
