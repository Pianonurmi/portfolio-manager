// Property tools in the account sheet: Statistics Finland price index (RE-03) and housing costs from YNAB (RE-07).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "../lib/api";
import { date, decimal, money, pct } from "../lib/format";
import { useToast } from "./Toasts";
import { Field } from "./ui";

type Meta = Record<string, string | boolean | Record<string, string> | null | undefined>;
type IndexResp = {
  configured: boolean;
  label?: string;
  unit?: "index" | "eur_m2";
  fetched_at?: string;
  points?: { period: string; value: string; trades: number | null }[];
  estimate?: {
    latest_period: string;
    latest_value: string;
    value: string | null;
    change?: number;
    market_m2_value?: string;
    missing?: string;
    base: { date: string; value: string; source: string; period: string; index: string } | null;
  };
};
type CostsResp = {
  mapping: { category_id: string; category: string; type: string; type_label: string }[];
  rent_category: string | null;
  types: Record<string, string>;
  last12: { by_type: Record<string, string>; running: string; total: string; monthly_running: string; per_m2_month: string | null; rent: string; net_yield: number | null };
};
type Cat = { id: string; group: string; name: string };

export function PropertyIndex({ accountId, meta, onDone }: { accountId: number; meta: Meta; onDone: () => void }) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const [m, setM] = useState<Meta>({
    index_source: (meta.index_source as string) ?? "",
    index_region: (meta.index_region as string) ?? "",
    index_house_type: (meta.index_house_type as string) ?? "3",
    index_rooms: (meta.index_rooms as string) ?? "00",
    postal_house_type: (meta.postal_house_type as string) ?? "2",
    index_auto: Boolean(meta.index_auto),
  });
  const regions = useQuery({ queryKey: ["statfin-regions"], enabled: m.index_source === "region", staleTime: 86_400_000, queryFn: () => api<{ code: string; label: string }[]>("/api/property/index/regions") });
  const idx = useQuery({ queryKey: ["prop-index", accountId], queryFn: () => api<IndexResp>(`/api/property/${accountId}/index`), retry: false });
  const save = async () => {
    try {
      await api(`/api/accounts/${accountId}/meta`, { method: "PUT", body: { meta: m } });
      await api<IndexResp>(`/api/property/${accountId}/index?refresh=true`);
      qc.invalidateQueries({ queryKey: ["prop-index", accountId] });
      qc.invalidateQueries({ queryKey: ["meta", accountId] });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed", { error: true });
    }
  };
  const addValuation = async () => {
    try {
      const r = await api<{ value: string; undo_id: number }>(`/api/property/${accountId}/index/valuation`, { method: "POST" });
      toast(`Index valuation ${money(r.value)} added`, { undoId: r.undo_id, onUndo: onDone });
      onDone();
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed", { error: true });
    }
  };
  const e = idx.data?.estimate;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <span className="label">Price index (Statistics Finland)</span>
      <form onSubmit={(ev) => (ev.preventDefault(), save())} className="form-grid">
        <Field label="Source" id="pi-src" hint="Region index is quality-adjusted; postal-code €/m² is smoothed over four quarters">
          <select id="pi-src" className="select input" value={String(m.index_source)} onChange={(ev) => setM({ ...m, index_source: ev.target.value })}>
            <option value="">None</option>
            <option value="region">Region index</option>
            <option value="postal">Postal code €/m² (uses the postal code above)</option>
          </select>
        </Field>
        {m.index_source === "region" && (
          <>
            <Field label="Area" id="pi-reg">
              <select id="pi-reg" className="select input" value={String(m.index_region)} onChange={(ev) => setM({ ...m, index_region: ev.target.value })}>
                <option value="">{regions.isLoading ? "Loading…" : "Choose…"}</option>
                {regions.data?.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}
              </select>
            </Field>
            <Field label="House type" id="pi-ht">
              <select id="pi-ht" className="select input" value={String(m.index_house_type)} onChange={(ev) => setM({ ...m, index_house_type: ev.target.value })}>
                <option value="3">Block of flats</option>
                <option value="1">Terraced house</option>
                <option value="0">All</option>
              </select>
            </Field>
            {m.index_house_type === "3" && (
              <Field label="Rooms" id="pi-rooms">
                <select id="pi-rooms" className="select input" value={String(m.index_rooms)} onChange={(ev) => setM({ ...m, index_rooms: ev.target.value })}>
                  <option value="00">All</option>
                  <option value="01">Studio</option>
                  <option value="02">Two rooms</option>
                  <option value="03">Three or more</option>
                </select>
              </Field>
            )}
          </>
        )}
        {m.index_source === "postal" && (
          <Field label="Type" id="pi-pht">
            <select id="pi-pht" className="select input" value={String(m.postal_house_type)} onChange={(ev) => setM({ ...m, postal_house_type: ev.target.value })}>
              <option value="1">Block of flats, studio</option>
              <option value="2">Block of flats, two rooms</option>
              <option value="3">Block of flats, three rooms+</option>
              <option value="5">Terraced house</option>
            </select>
          </Field>
        )}
        <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12 }}>
          <input type="checkbox" checked={Boolean(m.index_auto)} onChange={(ev) => setM({ ...m, index_auto: ev.target.checked })} />
          Update the valuation automatically each quarter
        </label>
        <div className="form-actions" style={{ gridColumn: "1 / -1" }}><button className="btn">Save and load index</button></div>
      </form>
      {idx.isError && <p className="err" style={{ margin: 0 }}>{(idx.error as ApiError).message}</p>}
      {idx.data?.configured && e && (
        <dl className="kv">
          <dt>Series</dt><dd>{idx.data.label}</dd>
          <dt>Latest ({e.latest_period})</dt><dd>{idx.data.unit === "eur_m2" ? `${money(e.latest_value)}/m²` : decimal(e.latest_value, 1)}</dd>
          {e.market_m2_value && (<><dt>€/m² × floor area</dt><dd>{money(e.market_m2_value)}</dd></>)}
          {e.base && e.value ? (
            <>
              <dt>From {e.base.source} {date(e.base.date)}</dt><dd>{money(e.base.value)} → <b>{money(e.value)}</b> ({pct(e.change ?? 0)})</dd>
            </>
          ) : (
            <><dt>Estimate</dt><dd style={{ color: "var(--muted)" }}>Needs {e.missing}</dd></>
          )}
        </dl>
      )}
      {e?.value && <div className="form-actions"><button className="btn primary" onClick={addValuation}>Add {money(e.value)} as valuation</button></div>}
    </div>
  );
}

export function HousingCosts({ accountId }: { accountId: number }) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const costs = useQuery({ queryKey: ["prop-costs", accountId], queryFn: () => api<CostsResp>(`/api/property/${accountId}/costs`) });
  const cats = useQuery({ queryKey: ["budget-categories"], queryFn: () => api<Cat[]>("/api/cashflow/categories") });
  const [add, setAdd] = useState({ category: "", type: "vastike" });
  const d = costs.data;
  const put = async (mapping: Record<string, string>, rent: string | null) => {
    try {
      await api(`/api/property/${accountId}/costs`, { method: "PUT", body: { cost_categories: mapping, rent_category: rent } });
      qc.invalidateQueries({ queryKey: ["prop-costs", accountId] });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed", { error: true });
    }
  };
  if (!d) return null;
  const mapping = Object.fromEntries(d.mapping.map((x) => [x.category_id, x.type]));
  if (cats.data && cats.data.length === 0)
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <span className="label">Housing costs</span>
        <p style={{ margin: 0, fontSize: 12, color: "var(--muted)" }}>Connect YNAB (Settings → Connections) to link vastike, upkeep and utilities categories to this home.</p>
      </div>
    );
  const l = d.last12;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <span className="label">Housing costs (YNAB, last 12 full months)</span>
      {d.mapping.length > 0 && (
        <dl className="kv">
          {Object.entries(l.by_type).map(([t, v]) => (<div key={t} style={{ display: "contents" }}><dt>{d.types[t] ?? t}</dt><dd>{money(v)}</dd></div>))}
          <dt>Running costs / month</dt><dd>{money(l.monthly_running)}{l.per_m2_month ? ` · ${money(l.per_m2_month, "EUR", 2)}/m²` : ""}</dd>
          {d.rent_category && (<><dt>Rent received</dt><dd>{money(l.rent)}{l.net_yield != null ? ` · net yield ${pct(l.net_yield)}` : ""}</dd></>)}
        </dl>
      )}
      <div className="list">
        {d.mapping.map((x) => (
          <div key={x.category_id} className="li" style={{ cursor: "default", padding: "4px 0" }}>
            <div className="t">{x.category}<small>{x.type_label}</small></div>
            <button className="x" aria-label={`Unlink ${x.category}`} onClick={() => { const n = { ...mapping }; delete n[x.category_id]; put(n, d.rent_category); }}>×</button>
          </div>
        ))}
      </div>
      <form className="form-grid" onSubmit={(ev) => { ev.preventDefault(); if (add.category) put({ ...mapping, [add.category]: add.type }, d.rent_category); }}>
        <Field label="YNAB category" id="hc-cat">
          <select id="hc-cat" className="select input" value={add.category} onChange={(ev) => setAdd({ ...add, category: ev.target.value })}>
            <option value="">Choose…</option>
            {cats.data?.filter((c) => !(c.id in mapping)).map((c) => <option key={c.id} value={c.id}>{c.group} · {c.name}</option>)}
          </select>
        </Field>
        <Field label="Is" id="hc-type">
          <select id="hc-type" className="select input" value={add.type} onChange={(ev) => setAdd({ ...add, type: ev.target.value })}>
            {Object.entries(d.types).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </Field>
        <Field label="Rent income category (if rented out)" id="hc-rent">
          <select id="hc-rent" className="select input" value={d.rent_category ?? ""} onChange={(ev) => put(mapping, ev.target.value || null)}>
            <option value="">Not rented</option>
            {cats.data?.map((c) => <option key={c.id} value={c.id}>{c.group} · {c.name}</option>)}
          </select>
        </Field>
        <div className="form-actions" style={{ gridColumn: "1 / -1" }}><button className="btn" disabled={!add.category}>Link category</button></div>
      </form>
    </div>
  );
}
