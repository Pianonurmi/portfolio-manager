// ETF screener side panels: candidates list, one ETF's detail (with look-through profile) and the side-by-side compare.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import { date, decimal, money, pct } from "../lib/format";
import { useToast } from "./Toasts";
import { Chip, Empty, Panel, Skeleton } from "./ui";

export type Nordnet = { symbol: string | null; exchange: string | null; currency: string | null; monthly_saving: boolean; owners: number | null };
export type Listing = { exchange: string; ticker: string | null; currency: string | null; secid: string | null };
export type Etf = {
  isin: string; name: string; secid: string | null; listings: Listing[]; provider: string | null; category: string | null;
  global_category: string | null; ter: number | null; fund_size_eur: number | null; inception: string | null;
  domicile: string | null; distribution: "acc" | "dist" | null; hedged: boolean; base_currency: string | null;
  equity_share: number | null; duration: number | null; credit_quality: number | null; srri: number | null;
  star_rating: number | null; sustainability: number | null; benchmark: string | null; style_box: string | null;
  returns: { ytd?: number; y1?: number; y3?: number; y5?: number; y10?: number }; vol_3y: number | null;
  sharpe_3y: number | null; max_dd_3y: number | null; yield_12m: number | null; close_price: number | null;
  close_currency: string | null; instrument_id: number | null; candidate_instrument_id: number | null; held: boolean;
  brokers: { nordnet?: Nordnet };
};
type Detail = Etf & { profile: { data: Profile; sources: Record<string, { source: string; as_of: string | null }> } | null };
type Profile = {
  asset_classes?: Record<string, number>; regions?: Record<string, number>; ter?: number;
  bond?: { modified_duration?: number; credit_quality?: string };
};
export type Candidate = {
  id: number; name: string; isin: string | null; currency: string; ter: number | null;
  asset_classes: Record<string, number> | null; last_price: string | null; price_problem: string | null;
  profile_source: string | null; profile_as_of: string | null;
};

export const dash = (v: number | null | undefined, f: (n: number) => string) => (v == null ? "—" : f(v));
export const sizeEur = (v: number | null) => dash(v, (n) => (n >= 1e9 ? `${decimal(n / 1e9, 1)} Md€` : `${decimal(n / 1e6, 0)} M€`));
export const years = (d: string | null) => (d ? decimal((Date.now() - new Date(d).getTime()) / 3.156e10, 1) : "—");
const EXCHANGE: Record<string, string> = { XETR: "Xetra", XSTO: "Stockholm", XCSE: "Copenhagen", XOSL: "Oslo", XHEL: "Helsinki" };
export const nordnetText = (n: Nordnet | undefined) =>
  n
    ? [`${n.symbol ?? "?"} on ${EXCHANGE[n.exchange ?? ""] ?? n.exchange ?? "?"} (${n.currency ?? "?"})`, n.monthly_saving ? "monthly saving" : null, n.owners != null ? `${n.owners.toLocaleString("fi-FI")} owners` : null]
        .filter(Boolean)
        .join(" · ")
    : "Not in Nordnet's ETF list";
/** The listing Nordnet trades, so a candidate is priced from the line you would actually buy. */
const nordnetListing = (e: Etf) => {
  const n = e.brokers?.nordnet;
  return n ? e.listings.find((l) => l.exchange === n.exchange && l.ticker === n.symbol) ?? e.listings.find((l) => l.exchange === n.exchange) : undefined;
};
const DIST = { acc: "Accumulating", dist: "Distributing" } as const;
const CLASS_LABEL: Record<string, string> = { equity: "Equity", bond: "Bonds", cash: "Cash", other: "Other", crypto: "Crypto" };

function Split({ ac }: { ac: Record<string, number> | null | undefined }) {
  const parts = Object.entries(ac ?? {}).filter(([, w]) => w >= 0.005).sort((a, b) => b[1] - a[1]);
  if (!parts.length) return <span style={{ color: "var(--muted)" }}>—</span>;
  return <span className="num">{parts.map(([k, w]) => `${CLASS_LABEL[k] ?? k} ${pct(w, 0)}`).join(" · ")}</span>;
}

export function Candidates() {
  const qc = useQueryClient();
  const { toast } = useToast();
  const q = useQuery({ queryKey: ["etf-candidates"], queryFn: () => api<Candidate[]>("/api/etfs/candidates"), refetchInterval: (s) => (s.state.data?.some((c) => !c.last_price || !c.profile_source) ? 10_000 : false) });
  const remove = useMutation({
    mutationFn: (isin: string) => api(`/api/etfs/${isin}/candidate`, { method: "DELETE" }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["etf-candidates"] });
      void qc.invalidateQueries({ queryKey: ["etfs"] });
      void qc.invalidateQueries({ queryKey: ["etf"] });
      toast("Candidate removed");
    },
  });
  const rows = q.data ?? [];
  return (
    <Panel title="Candidates" meta={`${rows.length} · used by the new-money plan and the optimiser`} style={{ marginBottom: 12 }} bodyless>
      {q.isLoading && <p style={{ padding: 14 }}><Skeleton w={200} /></p>}
      {q.data && rows.length === 0 && <Empty title="No candidates yet">Open an ETF below and add it as a candidate to test it against your portfolio.</Empty>}
      {rows.length > 0 && (
        <div className="tbl-wrap">
          <table>
            <thead><tr><th>ETF</th><th>Asset split</th><th className="r">TER</th><th>Prices</th><th>Profile</th><th /></tr></thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.id} style={{ cursor: "default" }}>
                  <td><div className="inst">{c.name}<small>{c.isin} · {c.currency}</small></div></td>
                  <td><Split ac={c.asset_classes} /></td>
                  <td className="r num">{dash(c.ter, (n) => pct(n, 2))}</td>
                  <td>{c.last_price ? <Chip tone="ok">to {date(c.last_price)}</Chip> : c.price_problem ? <Chip tone="bad">{c.price_problem}</Chip> : <Chip tone="info">fetching</Chip>}</td>
                  <td>{c.profile_source ? <Chip tone="ok">Morningstar {date(c.profile_as_of)}</Chip> : <Chip tone="info">fetching</Chip>}</td>
                  <td className="r">{c.isin && <button className="btn" disabled={remove.isPending} onClick={() => remove.mutate(c.isin!)}>Remove</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function Kv({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "5px 0", borderBottom: "1px solid var(--line)", fontSize: 12 }}>
      <span style={{ color: "var(--fg-2)" }}>{k}</span>
      <span className="num" style={{ textAlign: "right" }}>{children}</span>
    </div>
  );
}

export function EtfDetail({ isin, onClose }: { isin: string; onClose: () => void }) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const [secid, setSecid] = useState("");
  const q = useQuery({ queryKey: ["etf", isin], queryFn: () => api<Detail>(`/api/etfs/${isin}`) });
  const done = (msg: string) => {
    void qc.invalidateQueries({ queryKey: ["etf", isin] });
    void qc.invalidateQueries({ queryKey: ["etfs"] });
    void qc.invalidateQueries({ queryKey: ["etf-candidates"] });
    toast(msg);
  };
  const add = useMutation({
    mutationFn: () => api(`/api/etfs/${isin}/candidate`, { body: { listing_secid: secid || (q.data && nordnetListing(q.data)?.secid) || null } }),
    onSuccess: () => done("Candidate added; fetching its profile and prices"),
  });
  const remove = useMutation({ mutationFn: () => api(`/api/etfs/${isin}/candidate`, { method: "DELETE" }), onSuccess: () => done("Candidate removed") });
  const d = q.data;
  const p = d?.profile?.data;
  const regions = Object.entries(p?.regions ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 6);
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="inspector" role="dialog" aria-label="ETF detail">
        <div className="insp-h">
          <div className="row">
            <h3>{d?.name ?? isin}</h3>
            <button className="x" onClick={onClose} aria-label="Close">×</button>
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", paddingBottom: 10 }}>
            <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>{isin}</span>
            {d?.held && <Chip tone="ok">held</Chip>}
            {d?.candidate_instrument_id && <Chip tone="info">candidate</Chip>}
          </div>
        </div>
        <div className="insp-b">
          {q.isLoading && <Skeleton w={200} />}
          {d && (
            <>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
                {d.candidate_instrument_id ? (
                  <button className="btn" disabled={remove.isPending} onClick={() => remove.mutate()}>Remove candidate</button>
                ) : (
                  <>
                    {d.listings.length > 1 && (
                      <select className="select" aria-label="Listing" value={secid} onChange={(e) => setSecid(e.target.value)}>
                        <option value="">{nordnetListing(d) ? "Nordnet's listing" : "Preferred listing"}</option>
                        {d.listings.map((l) => <option key={l.secid ?? l.exchange} value={l.secid ?? ""}>{l.exchange} {l.ticker} ({l.currency})</option>)}
                      </select>
                    )}
                    <button className="btn primary" disabled={add.isPending} onClick={() => add.mutate()}>Add as candidate</button>
                  </>
                )}
              </div>
              <Kv k="Nordnet">{nordnetText(d.brokers?.nordnet)}</Kv>
              <Kv k="Nordea">No public list; Nordea's 1 % fee cap covers all ETFs. Check availability in Nordea's netbank.</Kv>
              <Kv k="Provider">{d.provider ?? "—"}</Kv>
              <Kv k="Category">{d.category ?? "—"}</Kv>
              <Kv k="Benchmark">{d.benchmark ?? "—"}</Kv>
              <Kv k="TER">{dash(d.ter, (n) => pct(n, 2))}</Kv>
              <Kv k="Fund size">{sizeEur(d.fund_size_eur)}</Kv>
              <Kv k="Inception">{date(d.inception)} ({years(d.inception)} y)</Kv>
              <Kv k="Domicile">{d.domicile ?? "—"}</Kv>
              <Kv k="Distribution">{d.distribution ? DIST[d.distribution] : "—"}{d.yield_12m ? ` · yield ${pct(d.yield_12m, 2)}` : ""}</Kv>
              <Kv k="Currency hedged">{d.hedged ? "Yes" : "No"}</Kv>
              <Kv k="Equity share">{dash(d.equity_share, (n) => pct(n, 0))}</Kv>
              <Kv k="Style box">{d.style_box ?? "—"}</Kv>
              <Kv k="Return YTD / 1y / 3y / 5y / 10y">{(["ytd", "y1", "y3", "y5", "y10"] as const).map((k) => dash(d.returns[k], (n) => pct(n, 1))).join(" / ")}</Kv>
              <Kv k="Volatility 3y">{dash(d.vol_3y, (n) => pct(n, 1))}</Kv>
              <Kv k="Sharpe 3y">{dash(d.sharpe_3y, (n) => decimal(n, 2))}</Kv>
              <Kv k="Max drawdown 3y">{dash(d.max_dd_3y, (n) => pct(n, 1))}</Kv>
              <Kv k="Risk indicator (SRRI)">{d.srri ?? "—"}</Kv>
              <Kv k="Morningstar rating / sustainability">{d.star_rating ?? "—"} / {d.sustainability ?? "—"}</Kv>
              <Kv k="Last price">{d.close_price != null ? money(d.close_price, d.close_currency ?? "EUR", 2) : "—"}</Kv>
              <h4 style={{ fontSize: 12, margin: "16px 0 4px" }}>Listings</h4>
              <table>
                <thead><tr><th>Exchange</th><th>Ticker</th><th>Currency</th></tr></thead>
                <tbody>
                  {d.listings.map((l) => (
                    <tr key={`${l.exchange}${l.secid}`} style={{ cursor: "default" }}>
                      <td>{l.exchange}</td><td className="mono">{l.ticker ?? "—"}</td><td>{l.currency ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {p?.asset_classes && (
                <>
                  <h4 style={{ fontSize: 12, margin: "16px 0 4px" }}>Look-through</h4>
                  <Kv k="Asset classes"><Split ac={p.asset_classes} /></Kv>
                  {p.bond?.modified_duration != null && <Kv k="Duration">{decimal(p.bond.modified_duration, 1)} y</Kv>}
                  {p.ter != null && <Kv k="TER (profile)">{pct(p.ter, 2)}</Kv>}
                  {regions.map(([k, w]) => <Kv key={k} k={k}>{pct(w, 1)}</Kv>)}
                </>
              )}
              {!p?.asset_classes && d.candidate_instrument_id && <p style={{ fontSize: 12, color: "var(--muted)" }}>The look-through profile is being fetched.</p>}
            </>
          )}
        </div>
      </aside>
    </>
  );
}

const ROWS: [string, (e: Etf) => string][] = [
  ["Provider", (e) => e.provider ?? "—"],
  ["Nordnet", (e) => nordnetText(e.brokers?.nordnet)],
  ["TER", (e) => dash(e.ter, (n) => pct(n, 2))],
  ["Fund size", (e) => sizeEur(e.fund_size_eur)],
  ["Age (years)", (e) => years(e.inception)],
  ["Return 1y", (e) => dash(e.returns.y1, (n) => pct(n, 1))],
  ["Return 3y p.a.", (e) => dash(e.returns.y3, (n) => pct(n, 1))],
  ["Return 5y p.a.", (e) => dash(e.returns.y5, (n) => pct(n, 1))],
  ["Volatility 3y", (e) => dash(e.vol_3y, (n) => pct(n, 1))],
  ["Sharpe 3y", (e) => dash(e.sharpe_3y, (n) => decimal(n, 2))],
  ["Max drawdown 3y", (e) => dash(e.max_dd_3y, (n) => pct(n, 1))],
  ["Equity share", (e) => dash(e.equity_share, (n) => pct(n, 0))],
  ["Distribution", (e) => (e.distribution ? DIST[e.distribution] : "—")],
  ["Hedged", (e) => (e.hedged ? "Yes" : "No")],
  ["Domicile", (e) => e.domicile ?? "—"],
  ["Category", (e) => e.category ?? "—"],
  ["Benchmark", (e) => e.benchmark ?? "—"],
  ["Listings", (e) => e.listings.map((l) => `${l.exchange} ${l.ticker ?? ""}`).join(", ")],
];

export function Compare({ items, onClose }: { items: Etf[]; onClose: () => void }) {
  return (
    <Panel title="Compare" meta={<button className="btn" onClick={onClose}>Close</button>} style={{ marginBottom: 12 }} bodyless>
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr><th />{items.map((e) => <th key={e.isin} style={{ whiteSpace: "normal", minWidth: 150 }}>{e.name}</th>)}</tr>
          </thead>
          <tbody>
            {ROWS.map(([label, f]) => (
              <tr key={label} style={{ cursor: "default" }}>
                <td style={{ color: "var(--fg-2)" }}>{label}</td>
                {items.map((e) => <td key={e.isin} className="num">{f(e)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
