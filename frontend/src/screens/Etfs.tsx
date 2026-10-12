// ETF screener: browse the UCITS ETFs a Finnish broker reaches, compare them, and mark candidates for the planner.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Candidates, Compare, EtfDetail, dash, sizeEur, years, type Etf } from "../components/EtfDetail";
import { useToast } from "../components/Toasts";
import { Chip, Empty, Panel, ScreenHead, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { date, decimal, pct } from "../lib/format";

type Named = { name: string; count: number };
type Result = { total: number; rows: Etf[]; updated_at: string | null; categories: Named[]; providers: Named[]; nordnet_total: number };
type Filters = {
  q: string; category: string; provider: string; max_ter: string; min_size: string; equity_min: string; equity_max: string;
  distribution: string; hedged: string; min_years: string; broker: string;
};
// the list starts with what Nordnet sells: the one broker whose ETF list can be checked
const EMPTY: Filters = { q: "", category: "", provider: "", max_ter: "", min_size: "", equity_min: "", equity_max: "", distribution: "", hedged: "", min_years: "", broker: "nordnet" };
const PAGE = 50;
const MAX_COMPARE = 4;
const ASC = ["name", "ter", "vol_3y"]; // sorted ascending unless asked otherwise (as the API does)
const COLS: { key: string; label: string; sort?: string; r?: boolean }[] = [
  { key: "name", label: "ETF", sort: "name" }, { key: "ter", label: "TER", sort: "ter", r: true },
  { key: "size", label: "Size", sort: "fund_size_eur", r: true }, { key: "y1", label: "1y", sort: "y1", r: true },
  { key: "y3", label: "3y", sort: "y3", r: true }, { key: "y5", label: "5y", sort: "y5", r: true },
  { key: "vol", label: "Vol 3y", sort: "vol_3y", r: true }, { key: "sharpe", label: "Sharpe 3y", sort: "sharpe_3y", r: true },
  { key: "dd", label: "Max DD 3y", sort: "max_dd_3y", r: true }, { key: "eq", label: "Equity", r: true },
  { key: "nn", label: "Nordnet owners", sort: "owners", r: true }, { key: "dist", label: "Dist." }, { key: "dom", label: "Dom." }, { key: "inc", label: "Age", sort: "inception", r: true },
];

export function Etfs() {
  const qc = useQueryClient();
  const { toast } = useToast();
  const [f, setF] = useState<Filters>(EMPTY);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState("fund_size_eur");
  const [desc, setDesc] = useState<boolean | null>(null);
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const [picked, setPicked] = useState<Etf[]>([]);
  const [comparing, setComparing] = useState(false);
  useEffect(() => {
    const t = window.setTimeout(() => { setF((x) => ({ ...x, q: search })); setOffset(0); }, 250);
    return () => window.clearTimeout(t);
  }, [search]);
  const params = new URLSearchParams({ sort, limit: String(PAGE), offset: String(offset) });
  for (const [k, v] of Object.entries(f)) if (v !== "") params.set(k, v);
  if (desc !== null) params.set("desc", String(desc));
  const q = useQuery({ queryKey: ["etfs", params.toString()], queryFn: () => api<Result>(`/api/etfs?${params}`), placeholderData: (prev) => prev });
  const refresh = useMutation({
    mutationFn: () => api<{ run_id: number }>("/api/etfs/refresh", { method: "POST" }),
    onSuccess: () => {
      toast("Refresh queued; reload in a minute or two");
      window.setTimeout(() => void qc.invalidateQueries({ queryKey: ["etfs"] }), 60_000);
    },
  });
  const d = q.data;
  const set = (k: keyof Filters) => (v: string) => { setF((x) => ({ ...x, [k]: v })); setOffset(0); };
  const sortBy = (s: string) => {
    if (s === sort) setDesc((x) => !(x ?? !ASC.includes(s)));
    else { setSort(s); setDesc(null); }
    setOffset(0);
  };
  const isPicked = (e: Etf) => picked.some((p) => p.isin === e.isin);
  const toggle = (e: Etf) => setPicked((p) => (p.some((x) => x.isin === e.isin) ? p.filter((x) => x.isin !== e.isin) : p.length < MAX_COMPARE ? [...p, e] : p));
  const dirDesc = desc ?? !ASC.includes(sort);
  const filtered = (Object.keys(f) as (keyof Filters)[]).some((k) => f[k] !== EMPTY[k]);
  return (
    <>
      <ScreenHead
        title="ETF screener"
        sub={`Updated ${d?.updated_at ? date(d.updated_at) : "never"}`}
        actions={<button className="btn" onClick={() => refresh.mutate()} disabled={refresh.isPending}>Refresh list</button>}
      />
      <p style={{ margin: "-6px 0 14px", fontSize: 12, color: "var(--fg-2)", maxWidth: 820 }}>
        European-listed UCITS ETFs from Morningstar. "Nordnet" shows only what Nordnet Finland sells (its own ETF list, mostly on Xetra). Nordea publishes no
        list; its 1 % fee cap covers all ETFs, so check there before buying. ETFs cannot be held in an osakesäästötili.
      </p>
      <Candidates />
      {comparing && picked.length >= 2 && <Compare items={picked} onClose={() => setComparing(false)} />}
      <Panel
        title="ETFs"
        meta={
          <>
            {picked.length > 0 && <span>{picked.length} selected · </span>}
            <button className="btn" disabled={picked.length < 2} onClick={() => setComparing(true)}>Compare{picked.length >= 2 ? ` (${picked.length})` : ""}</button>
            {picked.length > 0 && <button className="btn" style={{ marginLeft: 6 }} onClick={() => { setPicked([]); setComparing(false); }}>Clear</button>}
          </>
        }
        bodyless
      >
        <div style={{ padding: 14, display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
          <Txt label="Search" value={search} onChange={setSearch} placeholder="Name, ISIN or ticker" w={200} />
          <label className="field">
            <span style={{ fontSize: 12, color: "var(--fg-2)" }}>Available at</span>
            <select className="select" value={f.broker} onChange={(e) => set("broker")(e.target.value)}>
              <option value="nordnet">Nordnet</option><option value="nordnet_monthly">Nordnet, monthly saving</option><option value="">All listed ETFs</option>
            </select>
          </label>
          <Sel label="Category" value={f.category} onChange={set("category")} options={d?.categories} />
          <Sel label="Provider" value={f.provider} onChange={set("provider")} options={d?.providers} />
          <Txt label="Max TER %" value={f.max_ter} onChange={set("max_ter")} num />
          <Txt label="Min size M€" value={f.min_size} onChange={set("min_size")} num />
          <Txt label="Equity from %" value={f.equity_min} onChange={set("equity_min")} num />
          <Txt label="Equity to %" value={f.equity_max} onChange={set("equity_max")} num />
          <Txt label="Min years" value={f.min_years} onChange={set("min_years")} num />
          <label className="field">
            <span style={{ fontSize: 12, color: "var(--fg-2)" }}>Distribution</span>
            <select className="select" value={f.distribution} onChange={(e) => set("distribution")(e.target.value)}>
              <option value="">Any</option><option value="acc">Accumulating</option><option value="dist">Distributing</option>
            </select>
          </label>
          <label className="field">
            <span style={{ fontSize: 12, color: "var(--fg-2)" }}>Hedged</span>
            <select className="select" value={f.hedged} onChange={(e) => set("hedged")(e.target.value)}>
              <option value="">Any</option><option value="true">Hedged</option><option value="false">Not hedged</option>
            </select>
          </label>
          {filtered && <button className="btn" onClick={() => { setF(EMPTY); setSearch(""); setOffset(0); }}>Reset</button>}
        </div>
        {q.isLoading && <p style={{ padding: 14 }}><Skeleton w={240} /></p>}
        {d && f.broker && d.nordnet_total === 0 && (
          <p style={{ margin: "0 14px 10px", fontSize: 12, color: "var(--warn)" }}>Nordnet's ETF list has not been loaded yet: use “Refresh list”, or choose “All listed ETFs”.</p>
        )}
        {d && d.total === 0 && (
          <Empty title={filtered ? "No ETF matches" : "The ETF list is empty"}>
            {filtered ? "Loosen the filters." : "Use “Refresh list” to load it from Morningstar."}
          </Empty>
        )}
        {d && d.rows.length > 0 && (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th />
                  {COLS.map((c) => (
                    <th key={c.key} className={c.r ? "r" : ""} aria-sort={c.sort === sort ? (dirDesc ? "descending" : "ascending") : undefined}>
                      {c.sort ? (
                        <button className="x" style={{ font: "inherit", textTransform: "inherit", letterSpacing: "inherit", color: c.sort === sort ? "var(--fg)" : "inherit", padding: 0 }} onClick={() => sortBy(c.sort!)}>
                          {c.label}{c.sort === sort ? (dirDesc ? " ▼" : " ▲") : ""}
                        </button>
                      ) : c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {d.rows.map((e) => (
                  <tr key={e.isin} aria-selected={open === e.isin} onClick={() => setOpen(e.isin)}>
                    <td onClick={(ev) => ev.stopPropagation()}>
                      <input type="checkbox" aria-label={`Compare ${e.name}`} checked={isPicked(e)} disabled={!isPicked(e) && picked.length >= MAX_COMPARE} onChange={() => toggle(e)} />
                    </td>
                    <td style={{ maxWidth: 340 }}>
                      <div className="inst" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                        {e.name}
                        <small>
                          {e.provider}{" "}
                          {e.listings.slice(0, 3).map((l) => <span key={`${l.exchange}${l.secid}`} className="mono" style={{ marginRight: 6 }}>{l.ticker}·{l.exchange.slice(1)}</span>)}
                          {e.brokers?.nordnet?.monthly_saving && <Chip>monthly saving</Chip>} {e.candidate_instrument_id && <Chip tone="info">candidate</Chip>} {e.held && <Chip tone="ok">held</Chip>}
                        </small>
                      </div>
                    </td>
                    <td className="r num">{dash(e.ter, (n) => pct(n, 2))}</td>
                    <td className="r num">{sizeEur(e.fund_size_eur)}</td>
                    <td className="r num">{dash(e.returns.y1, (n) => pct(n, 1))}</td>
                    <td className="r num">{dash(e.returns.y3, (n) => pct(n, 1))}</td>
                    <td className="r num">{dash(e.returns.y5, (n) => pct(n, 1))}</td>
                    <td className="r num">{dash(e.vol_3y, (n) => pct(n, 1))}</td>
                    <td className="r num">{dash(e.sharpe_3y, (n) => decimal(n, 2))}</td>
                    <td className="r num">{dash(e.max_dd_3y, (n) => pct(n, 1))}</td>
                    <td className="r num">{dash(e.equity_share, (n) => pct(n, 0))}</td>
                    <td className="r num">
                      {e.brokers?.nordnet ? <>{dash(e.brokers.nordnet.owners, (n) => n.toLocaleString("fi-FI"))}</> : "—"}
                    </td>
                    <td>{e.distribution === "acc" ? "Acc" : e.distribution === "dist" ? "Dist" : "—"}{e.hedged ? " · H" : ""}</td>
                    <td>{e.domicile ?? "—"}</td>
                    <td className="r num">{years(e.inception)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {d && d.total > 0 && (
          <div style={{ display: "flex", gap: 8, alignItems: "center", padding: "10px 14px", fontSize: 12, color: "var(--fg-2)" }}>
            <span className="num">{offset + 1}–{Math.min(offset + PAGE, d.total)} of {d.total}</span>
            <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
            <button className="btn" disabled={offset + PAGE >= d.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
          </div>
        )}
      </Panel>
      {open && <EtfDetail isin={open} onClose={() => setOpen(null)} />}
    </>
  );
}

function Txt({ label, value, onChange, placeholder, num, w }: { label: string; value: string; onChange: (v: string) => void; placeholder?: string; num?: boolean; w?: number }) {
  return (
    <label className="field" style={{ width: w ?? 96 }}>
      <span style={{ fontSize: 12, color: "var(--fg-2)" }}>{label}</span>
      <input className="input" type={num ? "number" : "search"} min={num ? 0 : undefined} step={num ? "any" : undefined} value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

function Sel({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: Named[] | undefined }) {
  return (
    <label className="field" style={{ maxWidth: 200 }}>
      <span style={{ fontSize: 12, color: "var(--fg-2)" }}>{label}</span>
      <select className="select" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">All</option>
        {value && !options?.some((o) => o.name === value) && <option value={value}>{value}</option>}
        {options?.map((o) => <option key={o.name} value={o.name}>{o.name} ({o.count})</option>)}
      </select>
    </label>
  );
}
