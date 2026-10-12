// X-ray (Phase B): the portfolio's true exposures once every ETF and fund is looked through.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import type { Ctx } from "../lib/context";
import { date, money, num, pct } from "../lib/format";
import { useToast } from "./Toasts";
import { Chip, Empty, Field, Modal, Panel, Skeleton } from "./ui";

type Item = { key: string; label: string; value: string; weight: number };
type Source = { source: string; as_of: string | null; fields: string[] };
type Row = {
  instrument_id: number;
  name: string;
  type: string;
  account: string;
  value: string;
  weight: number;
  asset_classes: Record<string, number>;
  top_region: string | null;
  ter: number | null;
  ter_eur: string | null;
  sources: Source[];
  covered: boolean;
};
type XRayData = {
  total: string;
  dimensions: Record<string, Item[]>;
  ter: { fraction: number | null; eur_per_year: string; known_share: number };
  bond: { value: string; weight: number; modified_duration: number | null; ytm: number | null; credit_quality: string | null; duration_coverage: number };
  coverage: number;
  holdings: Row[];
};
type Profile = { data: Record<string, any>; sources: Record<string, { source: string; as_of: string | null }> };

const TABS = [
  ["asset_class", "Asset class"],
  ["region", "Region"],
  ["country", "Country"],
  ["sector", "Sector"],
  ["size", "Size"],
  ["style", "Style"],
  ["credit", "Credit"],
] as const;
const SOURCE_LABEL: Record<string, string> = { manual: "Manual", morningstar: "Morningstar", factsheet: "Factsheet", derived: "Derived" };

export function XRay({ ctx }: { ctx: Ctx }) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const [tab, setTab] = useState<(typeof TABS)[number][0]>("asset_class");
  const [edit, setEdit] = useState<Row | null>(null);
  const q = useQuery({ queryKey: ["xray", ctx.scope], queryFn: () => api<XRayData>(`/api/portfolio/xray?scope=${encodeURIComponent(ctx.scope)}`) });
  const refresh = useMutation({
    mutationFn: () => api("/api/jobs/instruments.profile/run", { method: "POST" }),
    onSuccess: () => toast("Profile refresh queued; reload the X-ray in a minute"),
  });
  const d = q.data;
  const eq = pctOf(d, "Equity");
  const bonds = pctOf(d, "Fixed income");
  return (
    <Panel
      title="X-ray"
      style={{ marginTop: 12 }}
      meta={
        <button className="btn" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
          Refresh from Morningstar
        </button>
      }
      bodyless
    >
      {q.isLoading && <p style={{ padding: 14 }}><Skeleton w={240} /></p>}
      {d && d.holdings.length === 0 && <Empty title="Nothing to look through yet">Holdings appear here after your first import.</Empty>}
      {d && d.holdings.length > 0 && (
        <>
          <div className="tiles">
            <Tile label="Equity" v={pct(eq)} />
            <Tile label="Bonds" v={pct(bonds)} />
            <Tile label="Cash + other" v={pct(Math.max(0, 1 - eq - bonds))} />
            <Tile label="Fund costs (TER)" v={d.ter.fraction == null ? "—" : pct(d.ter.fraction, 2)} sub={`${money(d.ter.eur_per_year, "EUR", 0)} / year`} />
            <Tile
              label="Bond sleeve"
              v={d.bond.modified_duration == null ? "—" : `${d.bond.modified_duration.toLocaleString("fi-FI", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} y`}
              sub={d.bond.weight > 0 ? `modified duration · YTM ${d.bond.ytm == null ? "—" : pct(d.bond.ytm, 1)} · ${d.bond.credit_quality ?? "no rating"}` : "no bonds"}
            />
            <Tile label="Data coverage" v={pct(d.coverage, 0)} sub="of value classified" />
          </div>
          <div style={{ padding: "10px 14px 0" }}>
            <div className="seg" role="group" aria-label="Dimension">
              {TABS.map(([k, l]) => (
                <button key={k} aria-pressed={tab === k} onClick={() => setTab(k)}>
                  {l}
                </button>
              ))}
            </div>
          </div>
          <div style={{ padding: 14 }}>
            <Bars items={d.dimensions[tab] ?? []} top={tab === "country" ? 10 : undefined} />
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Holding</th>
                  <th className="r">Value</th>
                  <th className="hide-sm">Split</th>
                  <th className="hide-sm">Top region</th>
                  <th className="r">TER</th>
                  <th>Sources</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {d.holdings.map((r) => (
                  <tr key={`${r.account}-${r.instrument_id}`} style={{ cursor: "default" }}>
                    <td>
                      <div className="inst">
                        <span>{r.name}</span>
                        <small>{r.account}</small>
                      </div>
                    </td>
                    <td className="r num">{money(r.value, "EUR", 0)}</td>
                    <td className="hide-sm">{Object.entries(r.asset_classes).filter(([, w]) => w >= 0.005).map(([k, w]) => `${k} ${pct(w, 0)}`).join(" · ")}</td>
                    <td className="hide-sm">{r.top_region ?? "—"}</td>
                    <td className="r num">{r.ter == null ? "—" : pct(r.ter, 2)}</td>
                    <td>
                      <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
                        {r.sources.length === 0 && <Chip tone="warn">No data</Chip>}
                        {r.sources.map((s) => (
                          <Chip key={s.source} tone={s.source === "manual" ? "info" : s.source === "morningstar" ? "ok" : undefined}>
                            {SOURCE_LABEL[s.source] ?? s.source}
                            {s.as_of ? ` ${date(s.as_of)}` : ""}
                          </Chip>
                        ))}
                      </span>
                    </td>
                    <td className="r">
                      <button className="btn" onClick={() => setEdit(r)}>Edit</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {edit && (
        <ProfileEditor
          row={edit}
          onClose={() => setEdit(null)}
          onSaved={() => {
            setEdit(null);
            qc.invalidateQueries({ queryKey: ["xray"] });
            qc.invalidateQueries({ queryKey: ["allocation"] });
            qc.invalidateQueries({ queryKey: ["rebalance"] });
          }}
        />
      )}
    </Panel>
  );
}

function pctOf(d: XRayData | undefined, key: string): number {
  return d?.dimensions.asset_class?.find((i) => i.key === key)?.weight ?? 0;
}

function Tile({ label, v, sub }: { label: string; v: string; sub?: string }) {
  return (
    <div className="tile">
      <span className="label">{label}</span>
      <span className="v">{v}</span>
      {sub && <span className="d" style={{ color: "var(--muted)" }}>{sub}</span>}
    </div>
  );
}

function Bars({ items, top }: { items: Item[]; top?: number }) {
  let shown = items;
  if (top && items.length > top) {
    const special = (i: Item) => i.key === "Unknown" || i.key === "Not applicable";
    const main = items.filter((i) => !special(i));
    const head = main.slice(0, top);
    const tail = main.slice(top);
    const other = tail.reduce((s, i) => s + num(i.value), 0);
    shown = [...head, ...(tail.length ? [{ key: "_other", label: `Other (${tail.length})`, value: String(other), weight: tail.reduce((s, i) => s + i.weight, 0) }] : []), ...items.filter(special)];
  }
  if (!shown.length) return <p style={{ color: "var(--muted)", fontSize: 12 }}>No data.</p>;
  const max = Math.max(...shown.map((i) => i.weight), 0.0001);
  return (
    <div className="xbars" role="list">
      {shown.map((i) => (
        <div key={i.key} className={`xbar${i.key === "Unknown" || i.key === "Not applicable" ? " dim" : ""}`} role="listitem" title={`${i.label}: ${pct(i.weight, 2)} · ${money(i.value, "EUR", 0)}`}>
          <span className="nm">{i.label}</span>
          <span className="tr" aria-hidden="true"><span className="fl" style={{ width: `${(i.weight / max) * 100}%` }} /></span>
          <span className="num">{pct(i.weight)}</span>
          <span className="num eur" style={{ color: "var(--muted)" }}>{money(i.value, "EUR", 0)}</span>
        </div>
      ))}
    </div>
  );
}

const CLASSES = [["equity", "Equity"], ["bond", "Bonds"], ["cash", "Cash"], ["other", "Other"]] as const;

function ProfileEditor({ row, onClose, onSaved }: { row: Row; onClose: () => void; onSaved: () => void }) {
  const p = useQuery({ queryKey: ["profile", row.instrument_id], queryFn: () => api<Profile>(`/api/instruments/${row.instrument_id}/profile`) });
  const [f, setF] = useState<{ ter: string; cls: Record<string, string>; dur: string } | null>(null);
  const cur = p.data?.data;
  // seed the form once the effective profile has loaded
  const init = cur
    ? {
        ter: cur.ter == null ? "" : String(+(cur.ter * 100).toFixed(4)),
        cls: Object.fromEntries(CLASSES.map(([k]) => [k, cur.asset_classes?.[k] ? String(+(cur.asset_classes[k] * 100).toFixed(2)) : ""])) as Record<string, string>,
        dur: cur.bond?.modified_duration == null ? "" : String(cur.bond.modified_duration),
      }
    : null;
  const form =
    f ??
    (cur
      ? {
          ter: cur.ter == null ? "" : String(+(cur.ter * 100).toFixed(4)),
          cls: Object.fromEntries(CLASSES.map(([k]) => [k, cur.asset_classes?.[k] ? String(+(cur.asset_classes[k] * 100).toFixed(2)) : ""])),
          dur: cur.bond?.modified_duration == null ? "" : String(cur.bond.modified_duration),
        }
      : null);
  const m = useMutation({
    mutationFn: () => {
      if (!form || !cur || !init) throw new Error("not loaded");
      // only what was touched becomes a manual override; the rest stays with its source
      const data: Record<string, unknown> = {};
      const pc = (s: string) => num(s.replace(",", ".")) / 100;
      if (form.ter !== init.ter) data.ter = form.ter.trim() === "" ? null : pc(form.ter);
      if (CLASSES.some(([k]) => (form.cls[k] ?? "") !== (init.cls[k] ?? ""))) {
        const split = Object.fromEntries(CLASSES.map(([k]) => [k, form.cls[k] ?? ""]).filter(([, v]) => v !== "").map(([k, v]) => [k, pc(v as string)]));
        data.asset_classes = Object.keys(split).length ? split : null;
      }
      if (form.dur !== init.dur) {
        const { modified_duration: _drop, ...rest } = cur.bond ?? {};
        data.bond = form.dur.trim() === "" ? (Object.keys(rest).length ? rest : null) : { ...rest, modified_duration: num(form.dur.replace(",", ".")) };
      }
      return api(`/api/instruments/${row.instrument_id}/profile`, { method: "PUT", body: { data } });
    },
    onSuccess: onSaved,
  });
  const sum = form ? CLASSES.reduce((s, [k]) => s + (num((form.cls[k] ?? "").replace(",", ".")) || 0), 0) : 0;
  const set = (patch: Partial<NonNullable<typeof form>>) => form && setF({ ...form, ...patch });
  return (
    <Modal title={`Edit profile · ${row.name}`} onClose={onClose}>
      {!form ? (
        <Skeleton w={200} />
      ) : (
        <form onSubmit={(e) => (e.preventDefault(), m.mutate())} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p style={{ fontSize: 12, color: "var(--muted)", margin: 0 }}>
            Your values win over Morningstar. Clear a field to hand it back.
          </p>
          <div className="form-grid">
            <Field label="TER (% a year)" id="p-ter" hint={p.data?.sources.ter ? `now from ${SOURCE_LABEL[p.data.sources.ter.source]}` : undefined}>
              <input id="p-ter" className="input mono" inputMode="decimal" value={form.ter} onChange={(e) => set({ ter: e.target.value })} />
            </Field>
            <Field label="Modified duration (years)" id="p-dur">
              <input id="p-dur" className="input mono" inputMode="decimal" value={form.dur} onChange={(e) => set({ dur: e.target.value })} />
            </Field>
            {CLASSES.map(([k, l]) => (
              <Field key={k} label={`${l} (%)`} id={`p-${k}`}>
                <input id={`p-${k}`} className="input mono" inputMode="decimal" value={form.cls[k] ?? ""} onChange={(e) => set({ cls: { ...form.cls, [k]: e.target.value } })} />
              </Field>
            ))}
          </div>
          {sum > 100.01 && <p className="err">Asset classes add up to {sum.toFixed(1)} %, at most 100.</p>}
          {m.error && <p className="err">{(m.error as Error).message}</p>}
          <div className="form-actions">
            <button type="button" className="btn" onClick={onClose}>Cancel</button>
            <button className="btn primary" disabled={m.isPending || sum > 100.01}>Save</button>
          </div>
        </form>
      )}
    </Modal>
  );
}
