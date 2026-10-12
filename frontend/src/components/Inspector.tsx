// Right-side inspector: explains any figure down to its source rows (UX-01).
import { useQuery } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "../lib/api";
import type { ExplainNode } from "../lib/types";

type Open = { ref: string; title?: string } | null;
const InspCtx = createContext<{ open: (ref: string, title?: string) => void; close: () => void; current: Open }>({
  open: () => {},
  close: () => {},
  current: null,
});
export const useInspector = () => useContext(InspCtx);

export function InspectorProvider({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<Open>(null);
  const open = useCallback((ref: string, title?: string) => setCurrent({ ref, title }), []);
  const close = useCallback(() => setCurrent(null), []);
  return (
    <InspCtx.Provider value={{ open, close, current }}>
      {children}
      {current && <Inspector key={current.ref} target={current} onClose={close} />}
    </InspCtx.Provider>
  );
}

function Inspector({ target, onClose }: { target: NonNullable<Open>; onClose: () => void }) {
  const [tab, setTab] = useState<"explain" | "details">("explain");
  const ref = useRef<HTMLElement>(null);
  const q = useQuery({ queryKey: ["explain", target.ref], queryFn: () => api<ExplainNode>(`/api/explain/${target.ref}`) });
  useEffect(() => {
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const root = q.data;
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="inspector" role="dialog" aria-label="Inspector" tabIndex={-1} ref={ref}>
        <div className="insp-h">
          <div className="row">
            <h3>{root?.label ?? target.title ?? "Explain"}</h3>
            <button className="x" onClick={onClose} aria-label="Close">
              ×
            </button>
          </div>
          <div className="big">{root?.value ?? (q.isLoading ? <span className="skeleton">000 000 €</span> : "")}</div>
          {root?.formula && <div style={{ color: "var(--muted)", fontFamily: "var(--mono)", fontSize: 11 }}>{root.formula}</div>}
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={tab === "explain"} onClick={() => setTab("explain")}>
              Explain
            </button>
            <button role="tab" aria-selected={tab === "details"} onClick={() => setTab("details")}>
              Details
            </button>
          </div>
        </div>
        <div className="insp-b">
          {q.isError && <p className="err">{(q.error as Error).message}</p>}
          {tab === "explain" && root && (
            <>
              {root.children.length ? (
                <ul className="tree" role="tree">
                  {root.children.map((c, i) => (
                    <TreeNode key={i} node={c} depth={0} />
                  ))}
                </ul>
              ) : (
                <p style={{ color: "var(--muted)" }}>{root.note ?? "This value is entered directly."}</p>
              )}
              <p style={{ color: "var(--muted)", fontSize: 12, marginTop: 14 }}>
                Every value is derived from the ledger. Leaf rows point to the imported file and row.
              </p>
            </>
          )}
          {tab === "details" && (
            <dl className="kv">
              <dt>Reference</dt>
              <dd>{target.ref}</dd>
              <dt>Fetched</dt>
              <dd>{q.dataUpdatedAt ? new Date(q.dataUpdatedAt).toLocaleTimeString("fi-FI") : "—"}</dd>
            </dl>
          )}
        </div>
      </aside>
    </>
  );
}

function TreeNode({ node, depth }: { node: ExplainNode; depth: number }) {
  const lazy = !!node.ref && node.children.length === 0;
  const [open, setOpen] = useState(depth < 1 && !lazy);
  const sub = useQuery({
    queryKey: ["explain", node.ref],
    queryFn: () => api<ExplainNode>(`/api/explain/${node.ref}`),
    enabled: lazy && open,
  });
  const kids = lazy ? (sub.data?.children ?? []) : node.children;
  const expandable = lazy || node.children.length > 0;
  const toggle = () => expandable && setOpen((o) => !o);
  return (
    <li>
      <div
        className="node"
        role="treeitem"
        tabIndex={0}
        aria-expanded={expandable ? open : undefined}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle();
          }
        }}
      >
        <span className="caret">{expandable ? "▶" : ""}</span>
        <span className="lbl">
          <span>
            {node.label}
            {node.source && <span className={`src ${node.source}`}>{node.source}</span>}
            {node.stale && <span className="tag-stale">● stale</span>}
          </span>
          {(node.formula || node.note) && <small>{[node.formula, node.note].filter(Boolean).join(" · ")}</small>}
        </span>
        <span className="val">{node.value ?? ""}</span>
      </div>
      {open && expandable && (
        <ul>
          {lazy && sub.isLoading && (
            <li>
              <div className="node">
                <span />
                <span className="skeleton">loading</span>
              </div>
            </li>
          )}
          {kids.map((c, i) => (
            <TreeNode key={i} node={c} depth={depth + 1} />
          ))}
        </ul>
      )}
    </li>
  );
}
