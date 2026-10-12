// Linked Paperless documents, tags and notes for any record (DOC-01/02, CORE-08).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "../lib/api";
import { date } from "../lib/format";
import { useToast } from "./Toasts";

type Link = { id: number; paperless_id: number; title: string; created_on: string | null; url: string | null };
type Hit = { id: number; title: string; created: string | null; url: string };

export function DocsPanel({ subjectType, subjectId }: { subjectType: string; subjectId: number }) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const p = `subject_type=${subjectType}&subject_id=${subjectId}`;
  const links = useQuery({ queryKey: ["doclinks", subjectType, subjectId], queryFn: () => api<Link[]>(`/api/documents/links?${p}`) });
  const tags = useQuery({ queryKey: ["tags", subjectType, subjectId], queryFn: () => api<string[]>(`/api/documents/tags?${p}`) });
  const notes = useQuery({ queryKey: ["notes", subjectType, subjectId], queryFn: () => api<{ id: number; text: string; created_at: string }[]>(`/api/documents/notes?${p}`) });
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [tag, setTag] = useState("");
  const [note, setNote] = useState("");
  const inv = () => ["doclinks", "tags", "notes"].forEach((k) => qc.invalidateQueries({ queryKey: [k, subjectType, subjectId] }));
  const find = async () => {
    try {
      setHits(await api<Hit[]>(`/api/documents/search?q=${encodeURIComponent(q)}`));
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Search failed", { error: true });
    }
  };
  const link = async (h: Hit) => {
    await api("/api/documents/links", { body: { subject_type: subjectType, subject_id: subjectId, paperless_id: h.id, title: h.title.slice(0, 255), created_on: h.created } });
    setHits(null);
    setQ("");
    inv();
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <span className="label">Documents, tags and notes</span>
      {links.data?.map((l) => (
        <div key={l.id} className="doc">
          <span className="thumb" />
          <div style={{ flex: 1, minWidth: 0 }}>
            {l.url ? <a href={l.url} target="_blank" rel="noreferrer">{l.title}</a> : l.title}
            <small style={{ display: "block", color: "var(--muted)" }}>Paperless #{l.paperless_id}{l.created_on ? ` · ${date(l.created_on)}` : ""}</small>
          </div>
          <button className="x" aria-label="Unlink" onClick={async () => (await api(`/api/documents/links/${l.id}`, { method: "DELETE" }), inv())}>×</button>
        </div>
      ))}
      <form onSubmit={(e) => (e.preventDefault(), find())} style={{ display: "flex", gap: 6 }}>
        <input className="input" placeholder="Search Paperless…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search Paperless" />
        <button className="btn" disabled={q.length < 2}>Find</button>
      </form>
      {hits?.map((h) => (
        <button key={h.id} className="pal-item" style={{ border: 0, background: "none", width: "100%" }} onClick={() => link(h)}>
          <span>{h.title}</span><small>{h.created ? date(h.created) : ""} · link</small>
        </button>
      ))}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        {tags.data?.map((t) => (
          <span key={t} className="chip">{t}<button className="x" style={{ fontSize: 12, padding: 0 }} aria-label={`Remove ${t}`} onClick={async () => (await api(`/api/documents/tags?${p}&name=${encodeURIComponent(t)}`, { method: "DELETE" }), inv())}>×</button></span>
        ))}
        <form onSubmit={async (e) => { e.preventDefault(); if (!tag) return; await api("/api/documents/tags", { body: { subject_type: subjectType, subject_id: subjectId, name: tag } }); setTag(""); inv(); }}>
          <input className="input" style={{ width: 120 }} placeholder="+ tag" value={tag} onChange={(e) => setTag(e.target.value)} aria-label="Add tag" />
        </form>
      </div>
      {notes.data?.map((n) => (
        <div key={n.id} style={{ fontSize: 12, borderLeft: "2px solid var(--line)", paddingLeft: 8, display: "flex", gap: 6 }}>
          <span style={{ flex: 1, whiteSpace: "pre-wrap" }}>{n.text}<small style={{ display: "block", color: "var(--muted)" }}>{date(n.created_at)}</small></span>
          <button className="x" aria-label="Delete note" onClick={async () => (await api(`/api/documents/notes/${n.id}`, { method: "DELETE" }), inv())}>×</button>
        </div>
      ))}
      <form onSubmit={async (e) => { e.preventDefault(); if (!note) return; await api("/api/documents/notes", { body: { subject_type: subjectType, subject_id: subjectId, text: note } }); setNote(""); inv(); }} style={{ display: "flex", gap: 6 }}>
        <input className="input" placeholder="Add a note" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Add note" />
        <button className="btn">Add</button>
      </form>
    </div>
  );
}
