// App shell: nav rail (bottom tab bar on phone), global context bar, palette, keyboard map.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router";
import { api } from "../lib/api";
import { PERIODS, useCtx } from "../lib/context";
import { ago } from "../lib/format";
import { VERSION } from "../lib/changelog";
import { SCREENS, screenFor } from "../lib/nav";
import { toggleTheme } from "../lib/store";
import type { EntityRow, Job } from "../lib/types";
import { Icon } from "./Icon";
import { Palette, Shortcuts } from "./Palette";

type Freshness = { prices_as_of: string | null; stale_count: number; last_import: string | null; unreconciled: number };

export function Shell({ username, open = false }: { username: string; open?: boolean }) {
  const loc = useLocation();
  const nav = useNavigate();
  const qc = useQueryClient();
  const [ctx, setCtx] = useCtx();
  const [overlay, setOverlay] = useState<null | "palette" | "keys" | "more">(null);
  const [online, setOnline] = useState(navigator.onLine);
  const current = screenFor(loc.pathname);
  const entities = useQuery({ queryKey: ["entities"], queryFn: () => api<EntityRow[]>("/api/entities") });
  const fresh = useQuery({
    queryKey: ["freshness"],
    queryFn: () => api<Freshness>("/api/freshness").catch(() => null),
    refetchInterval: 60_000,
  });

  useEffect(() => {
    const on = () => setOnline(true);
    const off = () => setOnline(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, []);

  // keyboard map (ui-spec §3)
  const gPending = useRef(false);
  useEffect(() => {
    let timer: number | undefined;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName) || t.isContentEditable;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOverlay((o) => (o === "palette" ? null : "palette"));
        return;
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "Escape") return setOverlay(null);
      if (overlay) return;
      if (gPending.current) {
        gPending.current = false;
        window.clearTimeout(timer);
        const s = SCREENS.find((x) => x.key === e.key);
        if (s) {
          e.preventDefault();
          nav({ pathname: s.path, search: loc.search });
        }
        return;
      }
      if (e.key === "g") {
        gPending.current = true;
        timer = window.setTimeout(() => (gPending.current = false), 900);
      } else if (e.key === "/") {
        e.preventDefault();
        setOverlay("palette");
      } else if (e.key === "?") setOverlay("keys");
      else if (e.key === "t") toggleTheme();
      else if (e.key === "[" || e.key === "]") {
        const i = PERIODS.indexOf(ctx.period) + (e.key === "]" ? 1 : -1);
        const p = PERIODS[Math.max(0, Math.min(PERIODS.length - 1, i))];
        if (p) setCtx({ period: p });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [overlay, nav, loc.search, ctx.period, setCtx]);

  const tracked = (entities.data ?? []).filter((e) => e.tracking !== "shared");
  const f = fresh.data;
  const freshState = !f || !f.prices_as_of ? "warn" : f.stale_count > 0 || f.unreconciled > 0 ? "warn" : "";
  const logout = async () => {
    await api("/api/auth/logout", { method: "POST" });
    qc.clear();
    window.location.href = "/";
  };
  const link = (path: string) => ({ pathname: path, search: loc.search });

  return (
    <div className="app">
      <nav className="rail" aria-label="Main">
        <div className="brand">
          <span className="brand-mark" />
          Portfolio Manager
        </div>
        {SCREENS.map((s) => (
          <Link key={s.id} to={link(s.path)} className="nav-btn" aria-current={current.id === s.id && loc.pathname !== "/whats-new" ? "page" : undefined}>
            <Icon name={s.icon} />
            <span>{s.name}</span>
            <span className="kbd">g {s.key}</span>
          </Link>
        ))}
        <div className="rail-foot">
          <Link to={link("/whats-new")} className="nav-btn" aria-current={loc.pathname === "/whats-new" ? "page" : undefined}>
            <Icon name="release" />
            <span>What's new</span>
            <span className="kbd">v{VERSION}</span>
          </Link>
          <button className="nav-btn" onClick={() => setOverlay("palette")}>
            <Icon name="search" />
            <span>Search & actions</span>
            <span className="kbd">⌘K</span>
          </button>
          <button className="nav-btn" onClick={toggleTheme}>
            <Icon name="theme" />
            <span>Theme</span>
            <span className="kbd">t</span>
          </button>
          <button className="nav-btn" onClick={() => setOverlay("keys")}>
            <Icon name="keys" />
            <span>Shortcuts</span>
            <span className="kbd">?</span>
          </button>
          {!open && (
            <button className="nav-btn" onClick={logout} title={`Signed in as ${username}`}>
              <Icon name="logout" />
              <span>Sign out</span>
            </button>
          )}
        </div>
      </nav>

      <div className="main">
        {!online && <div className="offline">Offline · showing the last data loaded on this device</div>}
        <header className="ctx">
          <select className="select" aria-label="Whose finances" value={ctx.scope} onChange={(e) => setCtx({ scope: e.target.value })}>
            <option value="household">Household</option>
            {tracked.map((e) => (
              <option key={e.id} value={`entity:${e.id}`}>
                {e.name}
              </option>
            ))}
          </select>
          <div className="seg" role="group" aria-label="Period">
            {PERIODS.map((p) => (
              <button key={p} aria-pressed={ctx.period === p} onClick={() => setCtx({ period: p })}>
                {p}
              </button>
            ))}
          </div>
          <div className="seg hide-sm" role="group" aria-label="Currency">
            <button aria-pressed={ctx.ccy === "EUR"} onClick={() => setCtx({ ccy: "EUR" })}>
              EUR
            </button>
            <button aria-pressed={ctx.ccy === "native"} onClick={() => setCtx({ ccy: "native" })}>
              Native
            </button>
          </div>
          <span className="spacer" />
          <Link to={link("/settings/jobs")} className="fresh hide-sm" style={{ textDecoration: "none" }} title="Data freshness">
            <span className={`dot ${freshState}`} />
            {f?.prices_as_of ? `Prices ${new Date(f.prices_as_of).toLocaleDateString("fi-FI")}` : "No prices yet"}
            {f && f.stale_count > 0 ? ` · ${f.stale_count} stale` : ""}
            {f?.last_import ? ` · import ${ago(f.last_import)}` : ""}
          </Link>
          <button className="palette-btn" onClick={() => setOverlay("palette")} aria-label="Search and actions">
            <Icon name="search" size={14} />
            <span className="lbl">Search, jump, run…</span>
            <span className="kbd">⌘K</span>
          </button>
        </header>
        <main className="screen" id="screen" tabIndex={-1}>
          <Outlet />
        </main>
      </div>

      <nav className="tabbar" aria-label="Main">
        {SCREENS.filter((s) => s.phone).map((s) => (
          <Link key={s.id} to={link(s.path)} aria-current={current.id === s.id ? "page" : undefined} className="tab">
            <Icon name={s.icon} />
            <span>{s.name}</span>
          </Link>
        ))}
        <button onClick={() => setOverlay("more")} aria-current={!current.phone ? "page" : undefined}>
          <Icon name="more" />
          <span>More</span>
        </button>
      </nav>

      {overlay === "palette" && <Palette onClose={() => setOverlay(null)} onShortcuts={() => setOverlay("keys")} />}
      {overlay === "keys" && <Shortcuts onClose={() => setOverlay(null)} />}
      {overlay === "more" && (
        <div className="pal-wrap" onMouseDown={(e) => e.target === e.currentTarget && setOverlay(null)}>
          <div className="pal" role="dialog" aria-label="More">
            <div className="pal-list">
              {SCREENS.filter((s) => !s.phone).map((s) => (
                <Link key={s.id} to={link(s.path)} className="pal-item" onClick={() => setOverlay(null)} style={{ color: "inherit", textDecoration: "none" }}>
                  <Icon name={s.icon} size={16} />
                  <span>{s.name}</span>
                </Link>
              ))}
              <button className="pal-item" style={{ width: "100%", border: 0, background: "none" }} onClick={() => (setOverlay(null), setOverlay("palette"))}>
                <Icon name="search" size={16} />
                <span>Search & actions</span>
              </button>
              <button className="pal-item" style={{ width: "100%", border: 0, background: "none" }} onClick={() => (setOverlay(null), toggleTheme())}>
                <Icon name="theme" size={16} />
                <span>Toggle theme</span>
              </button>
              {!open && (
                <button className="pal-item" style={{ width: "100%", border: 0, background: "none" }} onClick={logout}>
                  <Icon name="logout" size={16} />
                  <span>Sign out</span>
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export function useJobs() {
  return useQuery({ queryKey: ["jobs"], queryFn: () => api<Job[]>("/api/jobs"), refetchInterval: 15_000 });
}
