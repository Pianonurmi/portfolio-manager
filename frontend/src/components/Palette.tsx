// Command palette (UX-02): navigation, live search across the data, and actions.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Command } from "cmdk";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../lib/api";
import { SCREENS } from "../lib/nav";
import { toggleTheme } from "../lib/store";
import type { SearchHit } from "../lib/types";
import { useInspector } from "./Inspector";
import { useToast } from "./Toasts";

export function Palette({ onClose, onShortcuts }: { onClose: () => void; onShortcuts: () => void }) {
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const nav = useNavigate();
  const insp = useInspector();
  const { toast } = useToast();
  const qc = useQueryClient();
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 120);
    return () => clearTimeout(t);
  }, [q]);
  const hits = useQuery({
    queryKey: ["search", debounced],
    queryFn: () => api<SearchHit[]>(`/api/search?q=${encodeURIComponent(debounced)}`),
    enabled: debounced.length >= 2,
    staleTime: 10_000,
  });
  const run = (fn: () => void) => {
    onClose();
    fn();
  };
  const job = (name: string, label: string) =>
    run(async () => {
      await api(`/api/jobs/${name}/run`, { method: "POST" });
      toast(`${label} queued`);
      qc.invalidateQueries({ queryKey: ["jobs"] });
    });
  const groups = new Map<string, SearchHit[]>();
  for (const h of hits.data ?? []) groups.set(h.kind, [...(groups.get(h.kind) ?? []), h]);
  return (
    <div className="pal-wrap" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <Command label="Command palette" loop onKeyDown={(e) => e.key === "Escape" && onClose()}>
        <Command.Input autoFocus value={q} onValueChange={setQ} placeholder="Search accounts, instruments, people, actions…" />
        <Command.List>
          <Command.Empty>{hits.isFetching ? "Searching…" : "No matches"}</Command.Empty>
          {[...groups.entries()].map(([kind, list]) => (
            <Command.Group key={kind} heading={kind[0]!.toUpperCase() + kind.slice(1) + "s"}>
              {list.map((h) => (
                <Command.Item
                  key={kind + h.id}
                  value={`${kind} ${h.title} ${h.subtitle ?? ""} ${h.id}`}
                  onSelect={() => run(() => (nav(h.route), h.ref && insp.open(h.ref, h.title)))}
                >
                  <span>{h.title}</span>
                  <small>{h.subtitle}</small>
                </Command.Item>
              ))}
            </Command.Group>
          ))}
          <Command.Group heading="Go to">
            {SCREENS.map((s) => (
              <Command.Item key={s.id} value={`go ${s.name}`} onSelect={() => run(() => nav(s.path))}>
                <span>{s.name}</span>
                <small>g {s.key}</small>
              </Command.Item>
            ))}
          </Command.Group>
          <Command.Group heading="Actions">
            <Command.Item value="import file upload nordnet" onSelect={() => run(() => nav("/imports?new=1"))}>
              <span>Import a file…</span>
              <small>upload</small>
            </Command.Item>
            <Command.Item value="add account" onSelect={() => run(() => nav("/settings/accounts?new=1"))}>
              <span>Add account</span>
            </Command.Item>
            <Command.Item value="add person" onSelect={() => run(() => nav("/settings/people?new=1"))}>
              <span>Add person</span>
            </Command.Item>
            <Command.Item value="fetch prices fx" onSelect={() => job("prices.daily", "Price fetch")}>
              <span>Fetch prices now</span>
              <small>job</small>
            </Command.Item>
            <Command.Item value="recompute snapshots" onSelect={() => job("ledger.recompute", "Recompute")}>
              <span>Recompute everything</span>
              <small>job</small>
            </Command.Item>
            <Command.Item value="toggle theme dark light" onSelect={() => run(toggleTheme)}>
              <span>Toggle theme</span>
              <small>t</small>
            </Command.Item>
            <Command.Item value="keyboard shortcuts help" onSelect={() => run(onShortcuts)}>
              <span>Keyboard shortcuts</span>
              <small>?</small>
            </Command.Item>
          </Command.Group>
        </Command.List>
      </Command>
    </div>
  );
}

export function Shortcuts({ onClose }: { onClose: () => void }) {
  const K: [string, string][] = [
    ["⌘K  /", "Command palette"],
    ["g h · g p · g n", "Home · Portfolio · Net worth"],
    ["g i · g l · g k · g s", "Imports · Planning · Kids · Settings"],
    ["j / k", "Move row selection"],
    ["↵", "Open the selected row"],
    ["[ / ]", "Previous / next period"],
    ["t", "Toggle theme"],
    ["esc", "Close"],
  ];
  return (
    <div className="pal-wrap" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="pal" role="dialog" aria-label="Keyboard shortcuts">
        <div className="panel-h">
          <h2>Keyboard shortcuts</h2>
          <button className="x" onClick={onClose} style={{ marginLeft: "auto" }} aria-label="Close">
            ×
          </button>
        </div>
        <div className="panel-b">
          <dl className="kv">
            {K.map(([k, v]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt>
                  <span className="kbd">{k}</span>
                </dt>
                <dd style={{ textAlign: "left", fontFamily: "var(--ui)" }}>{v}</dd>
              </div>
            ))}
          </dl>
        </div>
      </div>
    </div>
  );
}
