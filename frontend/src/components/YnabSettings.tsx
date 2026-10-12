// Budget app connection (YNAB, or Actual when connected) and account mapping (Settings → Connections).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "../lib/api";
import { date, money } from "../lib/format";
import type { Account } from "../lib/types";
import { useToast } from "./Toasts";
import { Chip, Field, Panel, statusTone } from "./ui";

type Conn = { kind: string; config: Record<string, unknown>; has_secret: boolean; status: string; last_sync: string | null; error: string | null };
type YAcct = { id: string; name: string; type: string; on_budget: boolean; closed: boolean; balance: string; account_id: number | null; push_account_id: number | null; push_precision: number };

export function YnabSettings() {
  const qc = useQueryClient();
  const { toast } = useToast();
  const conns = useQuery({ queryKey: ["connections"], queryFn: () => api<Conn[]>("/api/connections") });
  const be = useQuery({ queryKey: ["budget-backend"], queryFn: () => api<{ kind: string | null; name: string | null }>("/api/cashflow/backend") });
  const isActual = be.data?.kind === "actual";
  const conn = conns.data?.find((c) => c.kind === (isActual ? "actual" : "ynab"));
  const label = isActual ? "Actual" : "YNAB";
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/api/accounts") });
  const yaccts = useQuery({ queryKey: ["ynab-accounts"], queryFn: () => api<YAcct[]>("/api/cashflow/ynab/accounts"), enabled: !!conn });
  const [token, setToken] = useState("");
  const [budgets, setBudgets] = useState<{ id: string; name: string }[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const test = async () => {
    setErr(null);
    try {
      setBudgets(await api("/api/cashflow/ynab/budgets", { body: { token } }));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Could not reach YNAB");
    }
  };
  const save = useMutation({
    mutationFn: (budget_id: string) => api("/api/connections/ynab", { method: "PUT", body: { config: { budget_id }, secret: token || null } }),
    onSuccess: async () => {
      await api("/api/cashflow/ynab/sync", { method: "POST" });
      toast("YNAB connected · first sync queued");
      setBudgets(null);
      setToken("");
      qc.invalidateQueries({ queryKey: ["connections"] });
      setTimeout(() => qc.invalidateQueries({ queryKey: ["ynab-accounts"] }), 6000);
    },
  });
  const map = async (y: YAcct, patch: Partial<YAcct>) => {
    try {
      await api(`/api/cashflow/ynab/accounts/${y.id}`, { method: "PUT", body: { account_id: y.account_id, push_account_id: y.push_account_id, push_precision: y.push_precision, ...patch } });
      qc.invalidateQueries({ queryKey: ["ynab-accounts"] });
      qc.invalidateQueries({ queryKey: ["networth"] });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed", { error: true });
    }
  };
  const sync = async () => {
    await api("/api/cashflow/ynab/sync", { method: "POST" });
    toast(`${label} sync queued`);
  };
  return (
    <Panel title={isActual ? "Actual Budget" : "YNAB"} meta={conn ? <><Chip tone={statusTone(conn.status)}>{conn.status === "new" ? "connected" : conn.status}</Chip>{conn.last_sync && <span>synced {date(conn.last_sync)}</span>}<button className="btn" onClick={sync}>Sync now</button></> : "not connected"}>
      {conn?.error && <p className="err">{conn.error}</p>}
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {!isActual && <Field label={conn?.has_secret ? "Replace personal access token" : "Personal access token"} id="ynab-token" hint="YNAB → Account settings → Developer settings → New token. Stored encrypted; used read-only unless you enable value push below.">
          <div style={{ display: "flex", gap: 8 }}>
            <input id="ynab-token" className="input mono" type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" />
            <button className="btn" onClick={test} disabled={token.length < 10}>Test</button>
          </div>
        </Field>}
        {err && <p className="err" style={{ margin: 0 }}>{err}</p>}
        {budgets && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {budgets.map((b) => (
              <button key={b.id} className="btn primary" onClick={() => save.mutate(b.id)}>Use budget “{b.name}”</button>
            ))}
          </div>
        )}
        {!!yaccts.data?.length && (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>{label} account</th>
                  <th className="r">Balance</th>
                  <th>Balance goes to</th>
                  <th className="hide-sm">Push value from</th>
                </tr>
              </thead>
              <tbody>
                {yaccts.data.filter((y) => !y.closed).map((y) => (
                  <tr key={y.id} style={{ cursor: "default" }}>
                    <td><div className="inst"><span>{y.name}</span><small>{y.type}{y.on_budget ? "" : " · tracking"}</small></div></td>
                    <td className="r num">{money(y.balance, "EUR", 2)}</td>
                    <td>
                      <select className="select" aria-label={`Account for ${y.name}`} value={y.account_id ?? ""} onChange={(e) => map(y, { account_id: e.target.value ? Number(e.target.value) : null })}>
                        <option value="">—</option>
                        {accounts.data?.filter((a) => ["bank", "savings", "credit_card", "loan", "manual_asset", "manual_liability"].includes(a.kind)).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                      </select>
                    </td>
                    <td className="hide-sm">
                      {y.on_budget ? <span style={{ color: "var(--muted)", fontSize: 12 }}>tracking accounts only</span> : (
                        <span style={{ display: "flex", gap: 6 }}>
                          <select className="select" aria-label={`Push into ${y.name}`} value={y.push_account_id ?? ""} onChange={(e) => map(y, { push_account_id: e.target.value ? Number(e.target.value) : null })}>
                            <option value="">off</option>
                            {accounts.data?.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                          </select>
                          <select className="select" aria-label="Rounding" value={y.push_precision} onChange={(e) => map(y, { push_precision: Number(e.target.value) })}>
                            {[1, 100, 1000, 10000].map((p) => <option key={p} value={p}>to {p} €</option>)}
                          </select>
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p style={{ margin: 0, fontSize: 12, color: "var(--muted)" }}>{isActual ? "Pushing writes that account's value (rounded) into your Actual budget on the LAN." : "Pushing sends that account's value (rounded) to YNAB's cloud."} It is off unless you choose an account.</p>
      </div>
    </Panel>
  );
}
