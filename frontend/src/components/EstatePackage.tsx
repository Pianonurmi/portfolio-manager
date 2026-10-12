// Encrypted emergency package (EST-02): download now as a password-protected PDF, and a monthly refreshed copy on disk.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "../lib/api";
import { date } from "../lib/format";
import { Field, Modal } from "./ui";

type PackageSettings = { enabled: boolean; path_dir: string | null; effective_dir: string; keep: number; has_passphrase: boolean; last: { file: string; at: string; dir: string } | null };

export const MIN_PASSWORD = 10;

/** POST the password, never put it in a URL; the PDF comes back as a download. */
async function downloadPdf(password: string): Promise<void> {
  const res = await fetch("/api/estate/package.pdf", {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-PM-Request": "1" },
    body: JSON.stringify({ password }),
  });
  if (!res.ok) {
    const d = await res.json().catch(() => null);
    throw new ApiError(res.status, typeof d?.detail === "string" ? d.detail : `Request failed (${res.status})`);
  }
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: "estate-package.pdf" });
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function DownloadPdfButton() {
  const [open, setOpen] = useState(false);
  const [pw, setPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const close = () => { setOpen(false); setPw(""); setErr(null); };
  const go = async () => {
    try { await downloadPdf(pw); close(); } catch (e) { setErr(e instanceof ApiError ? e.message : "Failed"); }
  };
  return (
    <>
      <button className="btn" onClick={() => setOpen(true)}>Download now (encrypted PDF)</button>
      {open && (
        <Modal title="Encrypted emergency package" onClose={close}>
          <form onSubmit={(e) => (e.preventDefault(), go())} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <Field label={`Password to open the PDF (at least ${MIN_PASSWORD} characters)`} id="pkg-pw" hint="Used for this file only; it is not stored. Keep it where your family can find it.">
              <input id="pkg-pw" className="input" type="password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} />
            </Field>
            {err && <p className="err" style={{ margin: 0 }}>{err}</p>}
            <div className="form-actions">
              <button type="button" className="btn" onClick={close}>Cancel</button>
              <button className="btn primary" disabled={pw.length < MIN_PASSWORD}>Download</button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}

export function ScheduledPackage() {
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["estate-package"], queryFn: () => api<PackageSettings>("/api/estate/package-settings") });
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [dir, setDir] = useState<string | null>(null);
  const [keep, setKeep] = useState<number | null>(null);
  const [pass, setPass] = useState("");
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  if (!s.data) return null;
  const d = s.data;
  const save = async () => {
    try {
      await api("/api/estate/package-settings", { method: "PUT", body: { enabled: enabled ?? d.enabled, path_dir: dir ?? d.path_dir, keep: keep ?? d.keep, passphrase: pass || null } });
      setPass(""); setEnabled(null); setDir(null); setKeep(null); setMsg({ text: "Saved" });
      qc.invalidateQueries({ queryKey: ["estate-package"] });
    } catch (e) { setMsg({ text: e instanceof ApiError ? e.message : "Failed", bad: true }); }
  };
  return (
    <details style={{ marginTop: 10 }}>
      <summary className="add-summary">Monthly encrypted copy on disk{d.enabled ? " (on)" : ""}</summary>
      <div className="form-grid" style={{ marginTop: 6 }}>
        <label style={{ display: "flex", gap: 6, alignItems: "center", gridColumn: "1 / -1" }}>
          <input type="checkbox" checked={enabled ?? d.enabled} onChange={(e) => setEnabled(e.target.checked)} /> Write a fresh encrypted PDF on the 1st of each month
        </label>
        <Field label={d.has_passphrase ? "Replace passphrase" : "Passphrase (to open the PDFs)"} id="pkg-pass" hint={d.has_passphrase ? "A passphrase is stored encrypted; leave empty to keep it. It is never shown." : `At least ${MIN_PASSWORD} characters; stored encrypted with the server key`}>
          <input id="pkg-pass" className="input" type="password" autoComplete="new-password" value={pass} onChange={(e) => setPass(e.target.value)} />
        </Field>
        <Field label="Folder" id="pkg-dir" hint={`Default ${d.effective_dir}`}>
          <input id="pkg-dir" className="input mono" placeholder={d.effective_dir} value={dir ?? d.path_dir ?? ""} onChange={(e) => setDir(e.target.value)} />
        </Field>
        <Field label="Keep the latest" id="pkg-keep">
          <input id="pkg-keep" className="input mono" type="number" min={1} max={24} value={keep ?? d.keep} onChange={(e) => setKeep(Number(e.target.value))} />
        </Field>
        <div className="form-actions" style={{ gridColumn: "1 / -1" }}><button className="btn" onClick={save} disabled={pass !== "" && pass.length < MIN_PASSWORD}>Save</button></div>
      </div>
      {msg && <p className={msg.bad ? "err" : undefined} style={{ margin: "4px 0 0", fontSize: 12 }}>{msg.text}</p>}
      <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--fg-2)" }}>
        {d.last ? `Last written: ${d.last.file} (${date(d.last.at.slice(0, 10))}) in ${d.last.dir}` : "Not written yet."}
      </p>
    </details>
  );
}
