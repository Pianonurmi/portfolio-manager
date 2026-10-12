import { useState, type FormEvent } from "react";
import { api, ApiError } from "../lib/api";

export function Auth({ setup, onDone }: { setup: boolean; onDone: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [needTotp, setNeedTotp] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (setup) await api("/api/auth/setup", { body: { username, password } });
      else await api("/api/auth/login", { body: { username, password, totp: totp || null } });
      onDone();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Could not reach the server";
      if (msg.includes("authenticator")) setNeedTotp(true);
      setError(msg);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="auth">
      <form className="panel" onSubmit={submit}>
        <div className="panel-h">
          <span className="brand-mark" style={{ width: 18, height: 18, borderRadius: 5, background: "linear-gradient(135deg,var(--c1),var(--c3))" }} />
          <h1>{setup ? "Set up Portfolio Manager" : "Sign in"}</h1>
        </div>
        <div className="panel-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {setup && <p style={{ margin: 0, color: "var(--fg-2)" }}>Create the admin account. Only this account can sign in.</p>}
          <div className="field">
            <label htmlFor="u">Username</label>
            <input id="u" className="input" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required minLength={2} autoFocus />
          </div>
          <div className="field">
            <label htmlFor="p">Password</label>
            <input id="p" className="input" type="password" autoComplete={setup ? "new-password" : "current-password"} value={password} onChange={(e) => setPassword(e.target.value)} required minLength={setup ? 12 : 1} />
            {setup && <span className="hint">At least 12 characters. A passphrase works well.</span>}
          </div>
          {needTotp && (
            <div className="field">
              <label htmlFor="t">Authenticator code</label>
              <input id="t" className="input mono" inputMode="numeric" autoComplete="one-time-code" value={totp} onChange={(e) => setTotp(e.target.value)} autoFocus />
            </div>
          )}
          {error && <p className="err" role="alert" style={{ margin: 0 }}>{error}</p>}
          <button className="btn primary" disabled={busy} style={{ justifyContent: "center", padding: "8px 10px" }}>
            {setup ? "Create account" : "Sign in"}
          </button>
        </div>
      </form>
    </div>
  );
}
