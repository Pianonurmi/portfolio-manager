// Settings → Connections: crypto sources, notification channels, alert rules.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "../lib/api";
import { dateTime } from "../lib/format";
import type { Account } from "../lib/types";
import { useToast } from "./Toasts";
import { Chip, Field, Panel, statusTone } from "./ui";

type Conn = { kind: string; config: Record<string, unknown>; has_secret: boolean; status: string; last_sync: string | null; error: string | null };
type FieldDef = { key: string; label: string; hint?: string; secret?: boolean; type?: "account" | "addresses" | "text" };

const DEFS: Record<string, { title: string; note: string; fields: FieldDef[] }> = {
  kraken: { title: "Kraken", note: "Create an API key with only “Query Funds” and “Query Ledger Entries”. Until you import the Ledgers export (Imports), balance changes are booked at market value; after it, new ledger entries are pulled twice a day with real costs and balances are only checked.",
    fields: [{ key: "api_key", label: "API key" }, { key: "secret", label: "Private key", secret: true }, { key: "account_id", label: "Account", type: "account" }] },
  binance: { title: "Binance", note: "Create a read-only API key (no trading, no withdrawals). For real costs and tax, import the Transaction history statement (Imports); after that the key only checks balances.",
    fields: [{ key: "api_key", label: "API key" }, { key: "secret", label: "Secret key", secret: true }, { key: "account_id", label: "Account", type: "account" }] },
  ledger: { title: "Ledger wallet (public addresses)", note: "Public addresses only — never a seed or private key. Lookups go to mempool.space (BTC) and a public Ethereum node; add those hosts to PM_OUTBOUND_ALLOWLIST to enable. The explorer sees which addresses you query.",
    fields: [{ key: "addresses", label: "Addresses, one per line as chain:address (btc:bc1… or eth:0x…)", type: "addresses" }, { key: "account_id", label: "Account", type: "account" }] },
  actual: { title: "Actual Budget (replaces YNAB when connected)", note: "Runs through actual-http-api next to your Actual server (github.com/jhonderson/actual-http-api). Add its host to PM_LAN_ALLOWLIST. Connecting Actual switches cash flow from YNAB to Actual: the next sync replaces the mirrored budget data, then map accounts again above. End-to-end encrypted budgets are not supported.",
    fields: [{ key: "url", label: "actual-http-api URL", hint: "e.g. http://actual.lan:5007" }, { key: "sync_id", label: "Budget sync id", hint: "Actual → Settings → Advanced → Sync ID" }, { key: "secret", label: "API key (API_KEY of actual-http-api)", secret: true }] },
  paperless: { title: "Paperless-ngx", note: "Documents stay in Paperless; this stores links only. Add the Paperless host to PM_LAN_ALLOWLIST in deploy/.env.",
    fields: [{ key: "url", label: "Paperless URL", hint: "e.g. http://paperless.lan:8000" }, { key: "secret", label: "API token", secret: true }] },
  llm: { title: "Local LLM", note: "Uses PM_LLM_URL from deploy/.env by default, as app “portfolio-manager”. Leave empty to keep the default, or point to another OpenAI-compatible server on your LAN; its host must be in PM_LAN_ALLOWLIST. Cloud endpoints are refused.",
    fields: [{ key: "url", label: "Base URL", hint: "default PM_LLM_URL, e.g. http://192.168.1.20:4000" }, { key: "model", label: "Text model", hint: "default local-general" }, { key: "vision_model", label: "Image model", hint: "default local-vision (scans, screenshots)" }, { key: "app_name", label: "App name at the gateway", hint: "default portfolio-manager" }, { key: "secret", label: "App key (lak_…, optional)", secret: true }] },
  gotify: { title: "Gotify", note: "Push notifications. Add the Gotify host to PM_LAN_ALLOWLIST in deploy/.env.",
    fields: [{ key: "url", label: "Server URL", hint: "e.g. http://gotify.lan" }, { key: "secret", label: "Application token", secret: true }] },
  smtp: { title: "Email (local mail server)", note: "Sent from the worker over a network limited to SMTP ports on private addresses.",
    fields: [{ key: "host", label: "SMTP host" }, { key: "port", label: "Port", hint: "25, 465 or 587" }, { key: "from", label: "From" }, { key: "to", label: "To" },
             { key: "username", label: "Username (optional)" }, { key: "secret", label: "Password (optional)", secret: true }] },
};

export function ConnectionCards() {
  const conns = useQuery({ queryKey: ["connections"], queryFn: () => api<Conn[]>("/api/connections") });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/api/accounts") });
  return (
    <>
      <div className="grid g-2">
        {Object.entries(DEFS).map(([kind, def]) => (
          <ConnCard key={kind} kind={kind} def={def} conn={conns.data?.find((c) => c.kind === kind)} accounts={accounts.data ?? []} />
        ))}
      </div>
      <AlertSettings />
    </>
  );
}

function ConnCard({ kind, def, conn, accounts }: { kind: string; def: (typeof DEFS)[string]; conn?: Conn; accounts: Account[] }) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const init = () => {
    const c = conn?.config ?? {};
    const v: Record<string, string> = {};
    for (const f of def.fields) {
      if (f.secret) v[f.key] = "";
      else if (f.type === "addresses") v[f.key] = ((c.addresses as { chain: string; address: string }[] | undefined) ?? []).map((a) => `${a.chain}:${a.address}`).join("\n");
      else v[f.key] = c[f.key] == null ? "" : String(c[f.key]);
    }
    return v;
  };
  const [v, setV] = useState<Record<string, string> | null>(null);
  const vals = v ?? init();
  const save = async () => {
    const config: Record<string, unknown> = {};
    let secret: string | null = null;
    for (const f of def.fields) {
      const x = vals[f.key] ?? "";
      if (f.secret) secret = x || null;
      else if (f.type === "addresses")
        config.addresses = x.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => ({ chain: l.split(":")[0], address: l.slice(l.indexOf(":") + 1) }));
      else if (f.type === "account") config[f.key] = x ? Number(x) : null;
      else config[f.key] = x || null;
    }
    try {
      await api(`/api/connections/${kind}`, { method: "PUT", body: { config, secret } });
      toast(`${def.title} saved`);
      setV(null);
      qc.invalidateQueries({ queryKey: ["connections"] });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed", { error: true });
    }
  };
  return (
    <Panel title={def.title} meta={conn ? <><Chip tone={statusTone(conn.status)}>{conn.status === "new" ? "saved" : conn.status}</Chip>{conn.last_sync && <span>{dateTime(conn.last_sync)}</span>}</> : "not set up"}>
      <form onSubmit={(e) => (e.preventDefault(), save())} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <p style={{ margin: 0, fontSize: 12, color: "var(--muted)" }}>{def.note}</p>
        {conn?.error && <p className="err" style={{ margin: 0 }}>{conn.error}</p>}
        {def.fields.map((f) => (
          <Field key={f.key} label={f.secret && conn?.has_secret ? `${f.label} (stored; leave empty to keep)` : f.label} id={`${kind}-${f.key}`} hint={f.hint}>
            {f.type === "account" ? (
              <select id={`${kind}-${f.key}`} className="select input" value={vals[f.key]} onChange={(e) => setV({ ...vals, [f.key]: e.target.value })}>
                <option value="">—</option>
                {accounts.filter((a) => a.kind.startsWith("crypto")).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            ) : f.type === "addresses" ? (
              <textarea id={`${kind}-${f.key}`} className="textarea mono" rows={3} value={vals[f.key]} onChange={(e) => setV({ ...vals, [f.key]: e.target.value })} />
            ) : (
              <input id={`${kind}-${f.key}`} className="input mono" type={f.secret ? "password" : "text"} autoComplete="off" value={vals[f.key]} onChange={(e) => setV({ ...vals, [f.key]: e.target.value })} />
            )}
          </Field>
        ))}
        <div className="form-actions">
          {kind in { kraken: 1, binance: 1, ledger: 1 } && conn && <button type="button" className="btn" onClick={async () => (await api("/api/crypto/sync", { method: "POST" }), toast("Crypto sync queued"))}>Sync now</button>}
          <button className="btn primary">Save</button>
        </div>
      </form>
    </Panel>
  );
}

function AlertSettings() {
  const { toast } = useToast();
  const s = useQuery({ queryKey: ["alert-settings"], queryFn: () => api<{ attention: boolean; dividends: boolean; large_move_pct: number }>("/api/alerts/settings") });
  const log = useQuery({ queryKey: ["alert-log"], queryFn: () => api<{ title: string; channels: string; sent_at: string; error: string | null }[]>("/api/alerts/log") });
  const qc = useQueryClient();
  const put = async (patch: Partial<{ attention: boolean; dividends: boolean; large_move_pct: number }>) => {
    await api("/api/alerts/settings", { method: "PUT", body: { ...s.data, ...patch } });
    qc.invalidateQueries({ queryKey: ["alert-settings"] });
  };
  const test = async () => {
    const r = await api<Record<string, string>>("/api/alerts/test", { method: "POST" });
    toast(Object.entries(r).map(([k, v]) => `${k}: ${v}`).join(" · "));
  };
  return (
    <Panel title="Alerts" style={{ marginTop: 12 }} meta={<button className="btn" onClick={test}>Send test</button>}>
      {s.data && (
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "center", fontSize: 13 }}>
          <label><input type="checkbox" checked={s.data.attention} onChange={(e) => put({ attention: e.target.checked })} /> Needs-attention items</label>
          <label><input type="checkbox" checked={s.data.dividends} onChange={(e) => put({ dividends: e.target.checked })} /> Dividends received</label>
          <label>Large daily move ≥ <input className="input mono" style={{ width: 60, display: "inline-block" }} defaultValue={s.data.large_move_pct} onBlur={(e) => put({ large_move_pct: Number(e.target.value.replace(",", ".")) || 3 })} /> %</label>
          <span style={{ color: "var(--muted)" }}>Failed jobs always alert · weekly digest on Mondays 07:00</span>
        </div>
      )}
      {!!log.data?.length && (
        <div className="list" style={{ marginTop: 10 }}>
          {log.data.slice(0, 8).map((a, i) => (
            <div key={i} className="li" style={{ cursor: "default", padding: "6px 0" }}>
              <div className="t">{a.title}<small>{dateTime(a.sent_at)} · {a.channels || "no channel"}{a.error ? ` · ${a.error}` : ""}</small></div>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
