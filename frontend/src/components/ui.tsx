import { useEffect, useRef, type ReactNode } from "react";

export function Panel({ title, meta, children, style, bodyless }: { title?: ReactNode; meta?: ReactNode; children: ReactNode; style?: React.CSSProperties; bodyless?: boolean }) {
  return (
    <section className="panel" style={style}>
      {title !== undefined && (
        <div className="panel-h">
          <h2>{title}</h2>
          {meta !== undefined && <span className="meta">{meta}</span>}
        </div>
      )}
      {bodyless ? children : <div className="panel-b">{children}</div>}
    </section>
  );
}

export function ScreenHead({ title, sub, actions }: { title: string; sub?: ReactNode; actions?: ReactNode }) {
  useEffect(() => {
    document.title = `${title} · Portfolio Manager`;
  }, [title]);
  return (
    <div className="screen-head">
      <h1>{title}</h1>
      {sub && <span className="sub">{sub}</span>}
      {actions && <div className="actions">{actions}</div>}
    </div>
  );
}

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function Chip({ tone, children }: { tone?: "ok" | "warn" | "bad" | "info"; children: ReactNode }) {
  return <span className={`chip ${tone ?? ""}`}>{children}</span>;
}

export function statusTone(s: string | null | undefined): "ok" | "warn" | "bad" | "info" | undefined {
  if (s === "ok" || s === "committed") return "ok";
  if (s === "partial" || s === "needs_mapping" || s === "staged") return "warn";
  if (s === "failed" || s === "error") return "bad";
  if (s === "queued" || s === "running" || s === "new") return "info";
  return undefined;
}

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("input,select,textarea,button")?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      prev?.focus();
    };
  }, [onClose]);
  return (
    <div className="modal-wrap" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label={title} ref={ref}>
        <div className="panel-h">
          <h2>{title}</h2>
          <button className="x" onClick={onClose} style={{ marginLeft: "auto" }} aria-label="Close">
            ×
          </button>
        </div>
        <div className="panel-b">{children}</div>
      </div>
    </div>
  );
}

export function Field({ label, hint, children, id }: { label: string; hint?: string; children: ReactNode; id: string }) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children}
      {hint && <span className="hint">{hint}</span>}
    </div>
  );
}

export function Skeleton({ w = 80 }: { w?: number }) {
  return (
    <span className="skeleton" style={{ display: "inline-block", width: w }}>
      &nbsp;
    </span>
  );
}
