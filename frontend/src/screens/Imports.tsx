// Imports (ING-01..03): upload → review with keyboard → map → commit, with undo and reconciliation.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { Link, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from "react-router";
import { useInspector } from "../components/Inspector";
import { DocExtract } from "../components/DocExtract";
import { useToast } from "../components/Toasts";
import { Chip, Empty, Panel, ScreenHead, statusTone } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { date, dateTime, decimal, money, quantity } from "../lib/format";
import type { Account, Instrument } from "../lib/types";

type Batch = {
  id: number;
  source: string;
  adapter: string;
  filename: string;
  file_sha256: string;
  status: string;
  created_at: string;
  committed_at: string | null;
  counts: Record<string, number>;
  rows: number;
};
type Row = {
  id: number;
  row_no: number;
  status: string;
  message: string | null;
  date: string;
  raw_type: string;
  kind: string | null;
  security: string | null;
  isin: string | null;
  quantity: string | null;
  price: string | null;
  amount: string | null;
  currency: string | null;
  portfolio: string | null;
  account: string | null;
  instrument: string | null;
  instrument_key: string | null;
};
type Detail = Batch & {
  rows_detail: Row[];
  unknown_types: string[];
  unmapped_portfolios: string[];
  replaces_statements?: { account_id: number; account: string; entries: number }[];
  missing_instruments: { key: string; name: string | null; isin: string | null; currency: string | null }[];
  kinds: string[];
};
type Recon = { account_id: number; account: string; subject: string; reported: string; computed: string; ok: boolean; as_of: string };

const STATUS_LABEL: Record<string, string> = {
  new: "new",
  needs_mapping: "needs mapping",
  needs_review: "needs review",
  duplicate: "duplicate",
  ignored: "ignored",
  committed: "committed",
  error: "error",
};

export function Imports() {
  return (
    <Routes>
      <Route index element={<ImportList />} />
      <Route path=":id" element={<Review />} />
    </Routes>
  );
}

function ImportList() {
  const loc = useLocation();
  const batches = useQuery({ queryKey: ["imports"], queryFn: () => api<Batch[]>("/api/imports") });
  const recon = useQuery({ queryKey: ["recon"], queryFn: () => api<Recon[]>("/api/reconciliation") });
  return (
    <>
      <ScreenHead title="Imports" sub="Transaction exports for an exact ledger; statements, screenshots and scans read by the local AI" />
      <div style={{ marginBottom: 12 }}><DocExtract /></div>
      <div className="grid g-3">
        <div className="grid" style={{ alignContent: "start" }}>
          <Upload />
          <Panel title="Imports" bodyless>
            {batches.data?.length === 0 && <Empty title="No imports yet">Upload your first export above.</Empty>}
            <div className="list">
              {batches.data?.map((b) => (
                <Link key={b.id} to={{ pathname: `/imports/${b.id}`, search: loc.search }} className="li" style={{ color: "inherit", textDecoration: "none" }}>
                  <span className={`sev ${b.status === "committed" ? "" : "info"}`} />
                  <div className="t">
                    #{b.id} · {b.filename}
                    <small>
                      {dateTime(b.created_at)} · {b.rows} rows · {Object.entries(b.counts).map(([k, v]) => `${v} ${STATUS_LABEL[k] ?? k}`).join(" · ")}
                    </small>
                  </div>
                  <Chip tone={statusTone(b.status)}>{b.status}</Chip>
                </Link>
              ))}
            </div>
          </Panel>
        </div>
        <ReconPanel data={recon.data} />
      </div>
    </>
  );
}

function ReconPanel({ data }: { data: Recon[] | undefined }) {
  return (
    <Panel title="Reconciliation" meta="Ledger vs. the source's own running totals" bodyless>
      {!data?.length ? (
        <Empty title="Nothing to reconcile yet">After a commit, every account is checked row by row against the quantities and balances the source reports.</Empty>
      ) : (
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>Account</th>
                <th>Item</th>
                <th className="r">Ledger</th>
                <th className="r">Reported</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.map((r, i) => (
                <tr key={i}>
                  <td>{r.account}</td>
                  <td>{r.subject}</td>
                  <td className="r num">{decimal(r.computed, 0, 8)}</td>
                  <td className="r num">{decimal(r.reported, 0, 8)}</td>
                  <td>{r.ok ? <Chip tone="ok">match</Chip> : <Chip tone="bad">differs</Chip>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function Upload() {
  const nav = useNavigate();
  const loc = useLocation();
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (params.get("new") === "1") input.current?.click();
  }, [params]);
  const send = async (file: File) => {
    setBusy(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const b = await api<Batch>("/api/imports", { form });
      qc.invalidateQueries({ queryKey: ["imports"] });
      nav({ pathname: `/imports/${b.id}`, search: loc.search.replace(/[?&]new=1/, "") });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Upload failed");
    } finally {
      setBusy(false);
    }
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    const f = e.dataTransfer.files[0];
    if (f) send(f);
  };
  return (
    <section
      className="panel"
      onDragOver={(e) => (e.preventDefault(), setOver(true))}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
      style={{ borderStyle: "dashed", borderColor: over ? "var(--accent)" : "var(--line)", background: over ? "var(--accent-wash)" : undefined }}
    >
      <div className="empty" style={{ padding: "20px 16px" }}>
        <h3>{busy ? "Reading the file…" : "Drop an export here"}</h3>
        <p>
          Nordnet: <b>Omat sivut → Tapahtumat ja laskelmat</b>, all accounts and the full date range, then <b>Vie CSV</b>.
          Kraken: <b>History → Export → Ledgers</b>. Binance: <b>Orders → Transaction history → Generate all statements</b>.
          Ledger Live: <b>Settings → Accounts → Export operations</b>. Anything else: a CSV with{" "}
          <span className="mono">date,type,asset,amount,quote_asset,quote_amount,fee_asset,fee,id,note</span>.
          Re-importing the same or an overlapping file is safe: rows already in the ledger are skipped.
        </p>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button className="btn primary" onClick={() => input.current?.click()} disabled={busy}>
            Choose file
          </button>
          <input ref={input} type="file" accept=".csv,.txt,text/csv" hidden onChange={(e) => e.target.files?.[0] && send(e.target.files[0])} />
        </div>
        {error && <p className="err" role="alert">{error}</p>}
      </div>
    </section>
  );
}

function Review() {
  const { id } = useParams();
  const loc = useLocation();
  const nav = useNavigate();
  const qc = useQueryClient();
  const insp = useInspector();
  const { toast } = useToast();
  const [filter, setFilter] = useState<string>("attention");
  const [sel, setSel] = useState(0);
  const key = ["import", id];
  const d = useQuery({ queryKey: key, queryFn: () => api<Detail>(`/api/imports/${id}`) });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/api/accounts") });
  const instruments = useQuery({ queryKey: ["instruments", ""], queryFn: () => api<Instrument[]>("/api/instruments") });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: key });
    qc.invalidateQueries({ queryKey: ["imports"] });
  };
  const act = useMutation({
    mutationFn: ({ path, body, method }: { path: string; body?: unknown; method?: string }) =>
      api(`/api/imports/${id}${path}`, { body, method: method ?? "POST" }),
    onSuccess: refresh,
    onError: (e) => toast(e instanceof ApiError ? e.message : "Failed", { error: true }),
  });
  const commit = useMutation({
    mutationFn: () => api<{ committed: number; errors: number; undo_id: number | null; reconciliation: Record<string, { ok: boolean; row_mismatches?: number }>; warnings?: string[]; transfers_matched?: number }>(`/api/imports/${id}/commit`, { method: "POST" }),
    onSuccess: (r) => {
      refresh();
      qc.invalidateQueries({ queryKey: ["recon"] });
      qc.invalidateQueries({ queryKey: ["freshness"] });
      const recon = Object.values(r.reconciliation);
      const ok = recon.every((x) => x.ok);
      const extra = [r.transfers_matched ? `${r.transfers_matched} transfers matched` : "", ...(r.warnings ?? [])].filter(Boolean).join(" · ");
      toast(`Committed ${r.committed} rows${r.errors ? `, ${r.errors} errors` : ""} · reconciliation ${recon.length === 0 ? "not available" : ok ? "matches the source" : "has differences"}${extra ? ` · ${extra}` : ""}`, {
        undoId: r.undo_id ?? undefined,
        onUndo: () => (refresh(), qc.invalidateQueries({ queryKey: ["recon"] })),
      });
      setFilter("all");
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Commit failed", { error: true }),
  });

  const rows = useMemo(() => {
    const all = d.data?.rows_detail ?? [];
    if (filter === "all") return all;
    if (filter === "attention") return all.filter((r) => ["needs_mapping", "needs_review", "error"].includes(r.status));
    return all.filter((r) => r.status === filter);
  }, [d.data, filter]);
  const attention = (d.data?.rows_detail ?? []).filter((r) => ["needs_mapping", "needs_review", "error"].includes(r.status)).length;
  useEffect(() => {
    if (d.data && attention === 0 && filter === "attention") setFilter("all");
  }, [d.data, attention, filter]);
  useEffect(() => setSel(0), [filter]);

  // keyboard: j/k move, x ignore/unignore, c commit, Enter = source row
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName) || e.metaKey || e.ctrlKey || document.querySelector(".inspector,.pal-wrap,.modal-wrap")) return;
      const r = rows[sel];
      if (e.key === "j" || e.key === "ArrowDown") (e.preventDefault(), setSel((s) => Math.min(rows.length - 1, s + 1)));
      else if (e.key === "k" || e.key === "ArrowUp") (e.preventDefault(), setSel((s) => Math.max(0, s - 1)));
      else if (e.key === "x" && r && r.status !== "committed") act.mutate({ path: `/rows/${r.id}`, body: { ignore: r.status !== "ignored" } });
      else if (e.key === "c" && (d.data?.counts.new ?? 0) > 0) commit.mutate();
      else if (e.key === "Enter" && r) insp.open(`raw:${r.id}`, `Row ${r.row_no}`);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [rows, sel, act, commit, d.data, insp]);
  useEffect(() => {
    document.querySelector(`#imp tr[data-i="${sel}"]`)?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  if (d.isError) return <Empty title="Import not found" action={<Link className="btn" to="/imports">Back to imports</Link>} />;
  const b = d.data;
  const counts = b?.counts ?? {};
  const tabs: [string, string, number][] = [
    ["attention", "Needs attention", attention],
    ["new", "New", counts.new ?? 0],
    ["duplicate", "Duplicates", counts.duplicate ?? 0],
    ["ignored", "Ignored", counts.ignored ?? 0],
    ["committed", "Committed", counts.committed ?? 0],
    ["all", "All", b?.rows ?? 0],
  ];
  return (
    <>
      <ScreenHead
        title={`Import #${id}`}
        sub={b ? `${b.filename} · ${b.adapter} · ${b.rows} rows · sha256 ${b.file_sha256.slice(0, 10)}…` : ""}
        actions={
          <>
            <Link className="btn" to={{ pathname: "/imports", search: loc.search }}>All imports</Link>
            {b && b.status !== "committed" && (counts.committed ?? 0) === 0 && (
              <button className="btn danger" onClick={() => act.mutate({ path: "", method: "DELETE" }, { onSuccess: () => nav("/imports") })}>Discard</button>
            )}
            <button className="btn primary" disabled={!counts.new || commit.isPending} onClick={() => commit.mutate()}>
              Commit {counts.new ?? 0} new rows <span className="kbd" style={{ background: "transparent", color: "inherit", borderColor: "currentColor" }}>c</span>
            </button>
          </>
        }
      />
      {b && b.status !== "committed" && (b.replaces_statements ?? []).length > 0 && (
        <p className="err" style={{ margin: "0 0 12px" }}>
          {(b.replaces_statements ?? []).map((x) => `${x.account} has ${x.entries} entries booked from statements`).join("; ")}.
          Committing replaces them with this history, so no holding is counted twice; undoing the commit brings them back.
        </p>
      )}
      {b && (b.unmapped_portfolios.length > 0 || b.unknown_types.length > 0 || b.missing_instruments.length > 0) && (
        <div className="grid g-2" style={{ marginBottom: 12 }}>
          {b.unmapped_portfolios.length > 0 && (
            <Panel title="Which account is this?" meta={b.source === "nordnet" ? `${b.unmapped_portfolios.length} Nordnet portfolio(s)` : `${b.source} rows`}>
              {b.unmapped_portfolios.map((pf) => (
                <div key={pf} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
                  <span className="num" style={{ minWidth: 110 }}>{pf || "(no portfolio column)"}</span>
                  <select className="select input" aria-label={`Account for ${pf}`} defaultValue="" onChange={(e) => e.target.value && act.mutate({ path: "/map-account", body: { portfolio: pf, account_id: Number(e.target.value) } })}>
                    <option value="">Choose account…</option>
                    {accounts.data?.filter((a) => ["aot", "ost", "crypto_exchange", "crypto_wallet", "kapitalisaatio"].includes(a.kind)).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                  </select>
                </div>
              ))}
              <p style={{ margin: "6px 0 0", fontSize: 12, color: "var(--muted)" }}>
                Missing an account? <Link to="/settings/accounts?new=1">Add it</Link> with the portfolio number as its reference.
              </p>
            </Panel>
          )}
          {b.missing_instruments.length > 0 && (
            <Panel title="New instruments" meta={<button className="btn primary" onClick={() => act.mutate({ path: "/create-instruments" })}>Create all {b.missing_instruments.length}</button>}>
              <div className="tbl-wrap" style={{ maxHeight: 220, overflowY: "auto" }}>
                <table>
                  <tbody>
                    {b.missing_instruments.map((m) => (
                      <tr key={m.key} style={{ cursor: "default" }}>
                        <td><div className="inst"><span>{m.name}</span><small>{m.isin} · {m.currency}</small></div></td>
                        <td className="r">
                          <select className="select" aria-label={`Map ${m.name}`} defaultValue="" onChange={(e) => e.target.value && act.mutate({ path: "/map-instrument", body: { key: m.key, instrument_id: Number(e.target.value) } })}>
                            <option value="">or map to existing…</option>
                            {instruments.data?.filter((i) => i.type !== "cash").map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
                          </select>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          )}
          {b.unknown_types.length > 0 && (
            <Panel title="Unknown transaction types" meta="Your choice is remembered for future imports">
              {b.unknown_types.map((t) => (
                <div key={t} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
                  <span className="num" style={{ minWidth: 160 }}>{t}</span>
                  <select className="select input" aria-label={`Meaning of ${t}`} defaultValue="" onChange={(e) => e.target.value && act.mutate({ path: "/map-type", body: { raw_type: t, kind: e.target.value } })}>
                    <option value="">Means…</option>
                    {b.kinds.map((k) => <option key={k} value={k}>{k === "ignore" ? "nothing (ignore these rows)" : k.replace("_", " ")}</option>)}
                  </select>
                </div>
              ))}
            </Panel>
          )}
        </div>
      )}
      <section className="panel">
        <div className="panel-h">
          <div className="seg" role="tablist">
            {tabs.map(([k, label, n]) => (
              <button key={k} aria-pressed={filter === k} onClick={() => setFilter(k)}>
                {label} <span className="num" style={{ color: "var(--muted)" }}>{n}</span>
              </button>
            ))}
          </div>
          <span className="meta hide-sm">
            <span className="kbd">j</span><span className="kbd">k</span> move · <span className="kbd">x</span> ignore · <span className="kbd">↵</span> source row · <span className="kbd">c</span> commit
          </span>
        </div>
        {rows.length === 0 ? (
          <Empty title={filter === "attention" ? "Nothing needs your attention" : "No rows"}>{filter === "attention" ? "Every row is mapped. Commit to add them to the ledger." : null}</Empty>
        ) : (
          <div className="tbl-wrap" style={{ maxHeight: "calc(100vh - 320px)", overflowY: "auto" }}>
            <table id="imp">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Type</th>
                  <th>Instrument</th>
                  <th className="r hide-sm">Qty</th>
                  <th className="r hide-sm">Price</th>
                  <th className="r">Amount</th>
                  <th className="hide-sm">Account</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={r.id} data-i={i} aria-selected={i === sel} onClick={() => (setSel(i), insp.open(`raw:${r.id}`, `Row ${r.row_no}`))}>
                    <td className="num">{date(r.date)}</td>
                    <td>
                      <div className="inst">
                        <span>{r.kind ? r.kind.replace("_", " ") : "?"}</span>
                        <small>{r.raw_type}</small>
                      </div>
                    </td>
                    <td>
                      <div className="inst">
                        <span>{r.instrument ?? r.security ?? "—"}</span>
                        {r.isin && <small>{r.isin}</small>}
                      </div>
                    </td>
                    <td className="r num hide-sm">{r.quantity ? quantity(r.quantity) : ""}</td>
                    <td className="r num hide-sm">{r.price ? decimal(r.price, 2, 4) : ""}</td>
                    <td className="r num">{r.amount ? money(r.amount, r.currency ?? "EUR", 2) : ""}</td>
                    <td className="hide-sm">{r.account ?? <span style={{ color: "var(--muted)" }}>{r.portfolio}</span>}</td>
                    <td title={r.message ?? ""}>
                      <Chip tone={statusTone(r.status === "needs_review" ? "partial" : r.status)}>{STATUS_LABEL[r.status] ?? r.status}</Chip>
                      {r.message && r.status !== "duplicate" && <div style={{ fontSize: 11, color: "var(--muted)", maxWidth: 280, whiteSpace: "normal" }}>{r.message}</div>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
