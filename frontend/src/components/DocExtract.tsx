// Documents read by the local AI (ING-09, AI-03, DOC-03): upload statements, screenshots, scans, CSV or Excel
// (or pick from Paperless); review what was read and how it was checked; then compare, book or record the value.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type DragEvent } from "react";
import { api, ApiError } from "../lib/api";
import { date, money, quantity } from "../lib/format";
import type { Account, Instrument } from "../lib/types";
import { useToast } from "./Toasts";
import { Chip, Field, Panel } from "./ui";

type FieldVal = { value: string; quote: string; found: boolean | null; type: string; edited?: boolean };
type Row = {
  name: string; isin: string | null; quantity: string | null; price: string | null; price_ccy: string | null; value: string | null; value_ccy: string | null;
  cost: string | null; account: string; source: string; read_from: string; in_text: boolean | null; status: "ok" | "check"; reasons: string[];
  fixed?: string; instrument_id: number | null; instrument: string | null; skip?: boolean; acquired?: string | null; edited?: boolean;
};
type Check = { account: string; what: string; computed: string; stated: string; ok: boolean };
type Extraction = {
  id: number; source: string; filename: string | null; paperless_id: number | null; title: string; kind: string; status: string;
  fields: Record<string, FieldVal>; account_id: number | null; error: string | null; model: string | null;
  data: { header?: Record<string, unknown>; rows?: Row[]; checks?: Check[]; notes?: string[]; factsheet?: Factsheet; options?: FsOptions }; progress: { done?: number; total?: number; step?: string };
};
type FsItem = { name: string; label: string | null; weight: number; found: boolean | null };
type Factsheet = {
  name: string | null; isins: string[]; share_class: string | null; as_of: string | null; ter: number | null; asset_classes: Record<string, number>;
  regions: FsItem[]; countries: FsItem[]; sectors: FsItem[]; cap_size: FsItem[]; bond: { modified_duration?: number; ytm?: number; credit_quality?: string };
  checks: Record<string, boolean | null>; instrument_id: number | null;
};
type FsOptions = { regions: string[]; sectors: string[]; countries: { code: string; name: string }[]; cap_size: string[] };
type Doc = { id: number; title: string; created: string | null; url: string };
type Gateway = { url: string; model: string; vision_model: string; app: string; health?: { status: string; queued: number }; policy?: { tier: string; max_tokens: number }; error?: string };

const KINDS: Record<string, string> = {
  holdings: "Holdings / portfolio statement", statement: "Account balance statement", loan_letter: "Loan letter",
  pension_record: "Pension record (työeläkeote)", factsheet: "Fund factsheet / KID", transactions: "Transaction list", other: "Other", unknown: "Detecting…",
};
const LABELS: Record<string, string> = {
  as_of: "Date", balance: "Balance €", account_hint: "Account as printed", institution: "Institution", outstanding_balance: "Outstanding €",
  reference_rate_name: "Reference rate", reference_rate: "Reference rate %", margin: "Margin %", next_reset: "Next rate reset",
  monthly_payment: "Monthly payment €", end_date: "Final payment", statement_year: "Record year", accrued_monthly: "Accrued pension €/month",
  annual_earnings: "Annual earnings €", target_age: "Target retirement age",
};

function useErr() {
  const { toast } = useToast();
  return (e: unknown) => toast(e instanceof ApiError ? e.message : "Failed", { error: true });
}

export function DocExtract() {
  const qc = useQueryClient();
  const err = useErr();
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [kind, setKind] = useState("");
  const [busy, setBusy] = useState(false);
  const [q, setQ] = useState("");
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const list = useQuery({
    queryKey: ["extractions"],
    queryFn: () => api<Extraction[]>("/api/ai/extractions?status=queued,processing,review,error"),
    refetchInterval: (query) => ((query.state.data ?? []).some((x) => x.status === "queued" || x.status === "processing") ? 3000 : false),
  });
  const gw = useQuery({ queryKey: ["ai-gateway"], queryFn: () => api<Gateway>("/api/ai/gateway"), retry: false, staleTime: 60_000 });
  const send = async (files: FileList | File[]) => {
    if (!files.length) return;
    setBusy(true);
    const form = new FormData();
    for (const f of Array.from(files)) form.append("files", f);
    if (kind) form.append("kind", kind);
    try {
      const out = await api<{ filename: string; status: string; error?: string }[]>("/api/ai/attachments", { form });
      for (const o of out) if (o.status === "error") err(new ApiError(422, `${o.filename}: ${o.error}`));
      qc.invalidateQueries({ queryKey: ["extractions"] });
    } catch (e) {
      err(e);
    } finally {
      setBusy(false);
    }
  };
  const onDrop = (e: DragEvent) => (e.preventDefault(), setOver(false), send(e.dataTransfer.files));
  const search = async () => {
    try { setDocs(await api<Doc[]>(`/api/documents/search?q=${encodeURIComponent(q)}`)); } catch (e) { err(e); }
  };
  const fromPaperless = async (d: Doc) => {
    try { await api("/api/ai/extract", { body: { paperless_id: d.id, kind: kind && kind !== "holdings" ? kind : null } }); setDocs(null); qc.invalidateQueries({ queryKey: ["extractions"] }); } catch (e) { err(e); }
  };
  const g = gw.data;
  return (
    <Panel title="Documents & statements (local AI)" meta={g ? (g.health?.status === "ready" ? `${g.model} · ${g.policy?.tier ?? "?"} tier · ${g.health.queued} queued` : g.error ? "AI unreachable" : "") : gw.isError ? "AI not connected" : ""}>
      <div
        onDragOver={(e) => (e.preventDefault(), setOver(true))}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
        style={{ border: "1px dashed var(--line)", borderColor: over ? "var(--accent)" : "var(--line)", background: over ? "var(--accent-wash)" : undefined, borderRadius: 8, padding: 12, display: "flex", flexDirection: "column", gap: 8 }}
      >
        <span style={{ fontSize: 12, color: "var(--fg-2)" }}>
          Drop portfolio and fund statements, pension or insurance savings reports, broker screenshots, scans, CSV or Excel files. They are read on your
          local AI (nothing leaves your network), checked against the page and the statement totals, and wait here for you to confirm.
        </span>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <button className="btn primary" disabled={busy} onClick={() => input.current?.click()}>{busy ? "Uploading…" : "Choose files"}</button>
          <input ref={input} type="file" multiple hidden accept=".pdf,.png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff,.csv,.tsv,.txt,.xlsx,.xlsm,application/pdf,image/*" onChange={(e) => { if (e.target.files) send(Array.from(e.target.files)); e.target.value = ""; }} />
          <select className="select" value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Document type">
            <option value="">Detect the type</option>
            {Object.entries(KINDS).filter(([k]) => !["other", "unknown", "transactions"].includes(k)).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
      </div>
      <details style={{ marginTop: 8 }}>
        <summary className="add-summary">Or read a document from Paperless</summary>
        <form onSubmit={(e) => (e.preventDefault(), search())} style={{ display: "flex", gap: 8, marginTop: 6 }}>
          <input className="input" placeholder="koronmuutos, tiliote, työeläkeote…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search Paperless" />
          <button className="btn" disabled={q.length < 2}>Search</button>
        </form>
        {docs && (
          <div className="list" style={{ marginTop: 6 }}>
            {docs.length === 0 && <span style={{ color: "var(--muted)" }}>No documents found</span>}
            {docs.map((d) => (
              <div key={d.id} className="li" style={{ cursor: "default", padding: "4px 0" }}>
                <div className="t">{d.title}<small>{d.created ?? ""}</small></div>
                <button className="btn" onClick={() => fromPaperless(d)}>Read values</button>
              </div>
            ))}
          </div>
        )}
      </details>
      {list.data?.map((x) => <Item key={x.id} x={x} />)}
    </Panel>
  );
}

function Item({ x }: { x: Extraction }) {
  const qc = useQueryClient();
  const err = useErr();
  const done = () => ["extractions", "networth", "valuations", "meta", "holdings", "recon", "positions"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
  const reject = async () => { try { await api(`/api/ai/extractions/${x.id}/reject`, { method: "POST" }); done(); } catch (e) { err(e); } };
  const retry = async (kind?: string) => { try { await api(`/api/ai/extractions/${x.id}/retry${kind ? `?kind=${kind}` : ""}`, { method: "POST" }); done(); } catch (e) { err(e); } };
  const p = x.progress ?? {};
  return (
    <div style={{ borderTop: "1px solid var(--line)", marginTop: 10, paddingTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <b style={{ overflowWrap: "anywhere" }}>{x.title}</b>
        <Chip tone={x.status === "error" ? "bad" : x.status === "review" ? "info" : "warn"}>{x.status === "review" ? KINDS[x.kind] ?? x.kind : x.status}</Chip>
        {x.source === "upload" && <a href={`/api/ai/extractions/${x.id}/file`} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>original</a>}
        {x.model && <span style={{ fontSize: 11, color: "var(--muted)" }}>{x.model}</span>}
      </div>
      {(x.status === "queued" || x.status === "processing") && (
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <div className="meter" style={{ flex: 1 }}><span style={{ width: `${p.total ? ((p.done ?? 0) / p.total) * 100 : 5}%`, background: "var(--accent)" }} /></div>
          <span style={{ fontSize: 12, color: "var(--fg-2)" }}>{x.status === "queued" ? "waiting for the GPU queue" : `${p.step ?? "reading"} ${p.total ? `${p.done ?? 0}/${p.total}` : ""}`}</span>
        </div>
      )}
      {x.error && <p className="err" style={{ margin: 0 }}>{x.error}</p>}
      {x.status === "error" && x.source === "upload" && (
        <div className="form-actions" style={{ justifyContent: "flex-start" }}>
          <span style={{ fontSize: 12, color: "var(--fg-2)" }}>Read again as</span>
          {["holdings", "statement", "loan_letter", "pension_record", "factsheet"].map((k) => <button key={k} className="btn" onClick={() => retry(k)}>{KINDS[k]}</button>)}
          <button className="btn" onClick={reject}>Discard</button>
        </div>
      )}
      {x.status === "error" && x.source !== "upload" && <div className="form-actions"><button className="btn" onClick={reject}>Discard</button></div>}
      {x.status === "review" && (x.kind === "holdings" ? <HoldingsReview x={x} onDone={done} onReject={reject} /> : x.kind === "factsheet" ? <FactsheetReview x={x} onDone={done} onReject={reject} /> : <FieldsReview x={x} onDone={done} onReject={reject} />)}
      {x.status === "review" && x.source === "upload" && (
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: 12, color: "var(--fg-2)" }}>
          <span>Wrong type? Read again as</span>
          {["holdings", "statement", "loan_letter", "pension_record", "factsheet"].filter((k) => k !== x.kind).map((k) => <button key={k} className="btn" style={{ fontSize: 12 }} onClick={() => retry(k)}>{KINDS[k]}</button>)}
        </div>
      )}
    </div>
  );
}

function AccountPick({ value, onChange, kinds }: { value: number | null; onChange: (v: number | null) => void; kinds?: string[] }) {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/api/accounts") });
  return (
    <select className="select input" value={value ?? ""} onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}>
      <option value="">Choose…</option>
      {accounts.data?.filter((a) => !kinds || kinds.includes(a.kind)).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
    </select>
  );
}

function HoldingsReview({ x, onDone, onReject }: { x: Extraction; onDone: () => void; onReject: () => void }) {
  const err = useErr();
  const { toast } = useToast();
  const [rows, setRows] = useState<Row[]>(x.data.rows ?? []);
  const [acct, setAcct] = useState<number | null>(x.account_id);
  const [asOf, setAsOf] = useState<string>(String(x.data.header?.as_of ?? ""));
  const h = x.data.header ?? {};
  const [newAcct, setNewAcct] = useState({ name: `${String(h.institution ?? "Nordnet")} ${String(h.account ?? "")}`.trim(), kind: "aot" });
  const flagged = rows.filter((r) => r.status === "check" && !r.skip && !r.edited).length;
  const set = (i: number, patch: Partial<Row>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const go = async (mode: "compare" | "book" | "value" | "cash") => {
    try {
      const r = await api<{ undo_id?: number; applied: Record<string, unknown> }>(`/api/ai/extractions/${x.id}/accept`, { body: { mode, rows, account_id: acct, as_of: asOf || null, new_account: acct ? null : newAcct } });
      const a = r.applied;
      toast(mode === "compare" ? `Compared: ${a.differences} difference(s), see Imports → Reconciliation` : mode === "book" ? `Booked ${a.transactions} change(s)${(a.skipped_flagged as string[] | undefined)?.length ? `; ${(a.skipped_flagged as string[]).length} flagged row(s) left out` : ""}` : mode === "cash" ? `Cash set to ${money(String(a.cash), "EUR", 2)}` : `Recorded ${money(String(a.value))}`, { undoId: r.undo_id, onUndo: onDone });
      onDone();
    } catch (e) { err(e); }
  };
  const cash = Object.entries((h.cash ?? {}) as Record<string, { amount: string; ccy: string }>);
  return (
    <>
      <div style={{ fontSize: 12, color: "var(--fg-2)" }}>
        {String(h.institution ?? "")}{h.account ? ` · ${String(h.account)}` : ""} · {rows.length} holdings
        {cash.length > 0 && ` · cash ${cash.map(([, c]) => money(c.amount, c.ccy, 2)).join(", ")}`}
      </div>
      {(x.data.checks ?? []).map((c, i) => (
        <div key={i} style={{ fontSize: 12, color: c.ok ? "var(--gain)" : "var(--loss)" }}>{c.ok ? "✓" : "✗"} {c.what}: {money(c.computed, "EUR", 2)} vs {money(c.stated, "EUR", 2)}{c.account ? ` (${c.account})` : ""}</div>
      ))}
      {(x.data.notes ?? []).map((n, i) => <div key={i} className="err" style={{ fontSize: 12 }}>{n}</div>)}
      <div className="tbl-wrap" style={{ maxHeight: 360, overflowY: "auto" }}>
        <table>
          <thead><tr><th /><th>Holding</th><th className="r">Quantity</th><th className="r hide-sm">Price</th><th className="r">Value</th><th className="r hide-sm">Purchase value</th></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} style={{ cursor: "default", opacity: r.skip ? 0.45 : 1 }}>
                <td><input type="checkbox" checked={!r.skip} onChange={(e) => set(i, { skip: !e.target.checked })} aria-label={`Include ${r.name}`} /></td>
                <td>
                  <div className="inst"><span>{r.name}</span>
                    <small>
                      {r.acquired ? `bought ${date(r.acquired)} · ` : ""}{r.isin ?? "no ISIN"} · {r.instrument ? `matches ${r.instrument}` : "new instrument"} · {r.read_from === "image" ? "read from image" : r.in_text ? "numbers found in the text" : "check"}
                      {r.fixed ? ` · ${r.fixed}` : ""}
                    </small>
                    {r.status === "check" && <small style={{ color: "var(--warn)" }}>{r.reasons.join("; ")}{r.edited ? " · corrected by hand: will be booked" : " · not booked unless corrected"}</small>}
                  </div>
                </td>
                <td className="r"><input className="input mono" style={{ width: 110, textAlign: "right" }} value={r.quantity ?? ""} onChange={(e) => set(i, { quantity: e.target.value.replace(",", ".") || null, edited: true })} aria-label={`Quantity of ${r.name}`} /></td>
                <td className="r num hide-sm">{r.price ? `${quantity(r.price)} ${r.price_ccy ?? ""}` : "—"}</td>
                <td className="r num">{r.value ? money(r.value, r.value_ccy ?? "EUR", 2) : "—"}</td>
                <td className="r hide-sm"><input className="input mono" style={{ width: 110, textAlign: "right" }} value={r.cost ?? ""} onChange={(e) => set(i, { cost: e.target.value.replace(",", ".") || null, edited: true })} aria-label={`Purchase value of ${r.name}`} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="form-grid">
        <Field label="Account" id={`ha-${x.id}`} hint={acct ? "Suggested from the account number on the statement" : "None chosen: a new account is created"}><AccountPick value={acct} onChange={setAcct} /></Field>
        {!acct && (
          <>
            <Field label="New account name" id={`hn-${x.id}`}><input id={`hn-${x.id}`} className="input" value={newAcct.name} onChange={(e) => setNewAcct({ ...newAcct, name: e.target.value })} /></Field>
            <Field label="Type" id={`hk-${x.id}`}>
              <select id={`hk-${x.id}`} className="select input" value={newAcct.kind} onChange={(e) => setNewAcct({ ...newAcct, kind: e.target.value })}>
                <option value="aot">Arvo-osuustili (taxable)</option>
                <option value="ost">Osakesäästötili</option>
                <option value="kapitalisaatio">Kapitalisaatiosopimus</option>
                <option value="ps">PS-sopimus</option>
                <option value="manual_asset">Other</option>
              </select>
            </Field>
          </>
        )}
        <Field label="Values as of" id={`hd-${x.id}`}><input id={`hd-${x.id}`} className="input" type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} /></Field>
      </div>
      {flagged > 0 && <p style={{ margin: 0, fontSize: 12, color: "var(--warn)" }}>{flagged} row(s) need a look: compare with the original before booking.</p>}
      <div className="form-actions" style={{ flexWrap: "wrap" }}>
        <button className="btn" onClick={onReject}>Discard</button>
        <button className="btn" onClick={() => go("compare")} title="Check the ledger against the statement; changes nothing">Compare with ledger</button>
        {cash.length > 0 && <button className="btn" onClick={() => go("cash")} title="Set the account's cash to the statement's cash balance; holdings untouched">Update cash only</button>}
        <button className="btn" onClick={() => go("value")} title="For pension, insurance or other accounts valued as a whole">Record total value</button>
        <button className="btn primary" onClick={() => go("book")} title="Bring the ledger to these quantities (opening balances or corrections)">Book holdings</button>
      </div>
      <p style={{ margin: 0, fontSize: 11, color: "var(--muted)" }}>
        {h.lots ? "Each lot is booked on its own purchase date at its own purchase value, so tax lots are exact." : "Booking uses the statement date as the purchase date. For exact tax lots, use the broker's lot export (Nordnet: Omistukset → Ostoerittäin) or transaction history; then use “Compare with ledger” to check it."}
        {asOf ? ` Statement date ${date(asOf)}.` : ""}
      </p>
    </>
  );
}

function FieldsReview({ x, onDone, onReject }: { x: Extraction; onDone: () => void; onReject: () => void }) {
  const err = useErr();
  const { toast } = useToast();
  const [vals, setVals] = useState<Record<string, string>>({});
  const [acct, setAcct] = useState<number | null>(x.account_id);
  const needsAccount = x.kind !== "pension_record";
  const accept = async () => {
    try {
      const r = await api<{ undo_id?: number }>(`/api/ai/extractions/${x.id}/accept`, { body: { values: vals, account_id: acct } });
      toast("Values saved", { undoId: r.undo_id, onUndo: onDone });
      onDone();
    } catch (e) { err(e); }
  };
  return (
    <>
      <div className="form-grid">
        {Object.entries(x.fields).map(([k, f]) => (
          <Field key={k} id={`x${x.id}-${k}`} label={`${LABELS[k] ?? k}${f.found ? " ✓" : f.found === null ? "" : " · check"}`} hint={f.quote ? `“${f.quote}”` : f.found === null ? "read from an image" : "no quote given"}>
            <input id={`x${x.id}-${k}`} className="input mono" value={vals[k] ?? f.value} onChange={(e) => setVals({ ...vals, [k]: e.target.value })} style={f.found === false ? { borderColor: "var(--warn)" } : undefined} />
          </Field>
        ))}
        {needsAccount && <Field label="Goes to account" id={`x${x.id}-acct`}><AccountPick value={acct} onChange={setAcct} /></Field>}
      </div>
      <div className="form-actions">
        <button className="btn" onClick={onReject}>Discard</button>
        <button className="btn primary" onClick={accept} disabled={needsAccount && !acct}>Confirm and save</button>
      </div>
    </>
  );
}

const FS_LISTS = [["regions", "Regions"], ["countries", "Countries"], ["sectors", "Sectors"], ["cap_size", "Company size"]] as const;
const pctText = (f: number | null | undefined) => (f === null || f === undefined ? "" : String(Math.round(f * 1e6) / 1e4));
const toFrac = (t: string): number | null => { const n = Number(t.replace(",", ".")); return t.trim() === "" || Number.isNaN(n) ? null : n / 100; };

function FactsheetReview({ x, onDone, onReject }: { x: Extraction; onDone: () => void; onReject: () => void }) {
  const err = useErr();
  const { toast } = useToast();
  const src = x.data.factsheet as Factsheet;
  const opts = x.data.options as FsOptions;
  const insts = useQuery({ queryKey: ["instruments", ""], queryFn: () => api<Instrument[]>("/api/instruments") });
  const [fs, setFs] = useState<Factsheet>(src);
  const [inst, setInst] = useState<number | null>(src.instrument_id);
  const [asOf, setAsOf] = useState(src.as_of ?? "");
  const mark = (key: string) => (fs.checks[key] === false ? "Check: this number is not in the text" : undefined);
  const setList = (k: (typeof FS_LISTS)[number][0], i: number, patch: Partial<FsItem>) => setFs({ ...fs, [k]: fs[k].map((it, j) => (j === i ? { ...it, ...patch, found: patch.weight !== undefined ? true : it.found } : it)) });
  const labels = (k: (typeof FS_LISTS)[number][0]): [string, string][] => k === "countries" ? opts.countries.map((c) => [c.code, c.name]) : (opts[k] as string[]).map((v) => [v, v]);
  const save = async () => {
    try {
      const r = await api<{ applied: { instrument: string; fields: string[] } }>(`/api/ai/extractions/${x.id}/accept`, { body: { instrument_id: inst, factsheet: fs, as_of: asOf || null } });
      toast(`Saved as the fund profile of ${r.applied.instrument}: ${r.applied.fields.join(", ")}`);
      onDone();
    } catch (e) { err(e); }
  };
  const ac = fs.asset_classes;
  return (
    <>
      <div style={{ fontSize: 12, color: "var(--fg-2)" }}>
        {fs.name ?? "Fund"}{fs.share_class ? ` · ${fs.share_class}` : ""}{fs.isins.length ? ` · ${fs.isins.join(", ")}` : ""}
      </div>
      <div className="form-grid">
        <Field label="Ongoing charge (TER) %" id={`fs-ter-${x.id}`} hint={mark("ter")}>
          <input id={`fs-ter-${x.id}`} className="input mono" value={pctText(fs.ter)} onChange={(e) => setFs({ ...fs, ter: toFrac(e.target.value), checks: { ...fs.checks, ter: true } })} />
        </Field>
        {(["equity", "bond", "cash", "other"] as const).map((k) => (
          <Field key={k} label={`${k.charAt(0).toUpperCase()}${k.slice(1)} %`} id={`fs-${k}-${x.id}`} hint={mark(`asset_classes.${k}`)}>
            <input id={`fs-${k}-${x.id}`} className="input mono" value={pctText(ac[k])} onChange={(e) => { const { [k]: _drop, ...rest } = ac; const f = toFrac(e.target.value); setFs({ ...fs, asset_classes: f === null ? rest : { ...rest, [k]: f }, checks: { ...fs.checks, [`asset_classes.${k}`]: true } }); }} />
          </Field>
        ))}
        <Field label="Duration (years)" id={`fs-dur-${x.id}`} hint={mark("bond.modified_duration")}>
          <input id={`fs-dur-${x.id}`} className="input mono" value={fs.bond.modified_duration ?? ""} onChange={(e) => { const n = Number(e.target.value.replace(",", ".")); const { modified_duration: _d, ...rest } = fs.bond; setFs({ ...fs, bond: e.target.value.trim() === "" || Number.isNaN(n) ? rest : { ...rest, modified_duration: n }, checks: { ...fs.checks, "bond.modified_duration": true } }); }} />
        </Field>
        <Field label="Yield to maturity %" id={`fs-ytm-${x.id}`} hint={mark("bond.ytm")}>
          <input id={`fs-ytm-${x.id}`} className="input mono" value={pctText(fs.bond.ytm)} onChange={(e) => { const f = toFrac(e.target.value); const { ytm: _y, ...rest } = fs.bond; setFs({ ...fs, bond: f === null ? rest : { ...rest, ytm: f }, checks: { ...fs.checks, "bond.ytm": true } }); }} />
        </Field>
      </div>
      {FS_LISTS.map(([k, title]) => fs[k].length > 0 && (
        <div key={k} className="tbl-wrap" style={{ maxHeight: 240, overflowY: "auto" }}>
          <table>
            <thead><tr><th>{title} as printed</th><th>Used as</th><th className="r">%</th></tr></thead>
            <tbody>
              {fs[k].map((it, i) => (
                <tr key={i} style={{ cursor: "default" }}>
                  <td>{it.name}{it.found === false && <small style={{ color: "var(--warn)" }}> · check: number not in the text</small>}</td>
                  <td>
                    <select className="select input" aria-label={`${title}: ${it.name}`} value={it.label ?? ""} onChange={(e) => setList(k, i, { label: e.target.value || null })}>
                      <option value="">Not recognised: left out</option>
                      {labels(k).map(([v, n]) => <option key={v} value={v}>{n}</option>)}
                    </select>
                  </td>
                  <td className="r"><input className="input mono" style={{ width: 80, textAlign: "right" }} aria-label={`% of ${it.name}`} value={pctText(it.weight)} onChange={(e) => setList(k, i, { weight: toFrac(e.target.value) ?? 0 })} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      <div className="form-grid">
        <Field label="Fund" id={`fs-inst-${x.id}`} hint={src.instrument_id ? "Matched by ISIN" : "No held instrument has this ISIN: choose the fund"}>
          <select id={`fs-inst-${x.id}`} className="select input" value={inst ?? ""} onChange={(e) => setInst(e.target.value ? Number(e.target.value) : null)}>
            <option value="">Choose…</option>
            {insts.data?.map((i) => <option key={i.id} value={i.id}>{i.name}{i.isin ? ` · ${i.isin}` : ""}</option>)}
          </select>
        </Field>
        <Field label="Figures as of" id={`fs-date-${x.id}`}><input id={`fs-date-${x.id}`} className="input" type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} /></Field>
      </div>
      <div className="form-actions">
        <button className="btn" onClick={onReject}>Discard</button>
        <button className="btn primary" onClick={save} disabled={!inst} title="Used by the X-ray wherever Morningstar has no data for this fund">Save as fund profile</button>
      </div>
      <p style={{ margin: 0, fontSize: 11, color: "var(--muted)" }}>Morningstar data and figures you set by hand take precedence per field; a saved factsheet replaces the earlier one for this fund.</p>
    </>
  );
}
