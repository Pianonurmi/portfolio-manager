import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { api } from "../lib/api";

type Toast = { id: number; msg: string; undoId?: number; onUndo?: () => void; error?: boolean };
type Ctx = { toast: (msg: string, opts?: { undoId?: number; onUndo?: () => void; error?: boolean }) => void };

const ToastCtx = createContext<Ctx>({ toast: () => {} });
export const useToast = () => useContext(ToastCtx);

let seq = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const dismiss = (id: number) => setItems((xs) => xs.filter((x) => x.id !== id));
  const toast = useCallback<Ctx["toast"]>((msg, opts = {}) => {
    const id = ++seq;
    setItems((xs) => [...xs.slice(-3), { id, msg, ...opts }]);
    setTimeout(() => dismiss(id), opts.undoId || opts.onUndo ? 10000 : 5000);
  }, []);
  const undo = async (t: Toast) => {
    dismiss(t.id);
    if (t.undoId) await api(`/api/undo/${t.undoId}`, { method: "POST" });
    t.onUndo?.();
  };
  return (
    <ToastCtx.Provider value={{ toast }}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className="toast" style={t.error ? { background: "var(--loss)", color: "#fff" } : undefined}>
            <span>{t.msg}</span>
            {(t.undoId || t.onUndo) && <button onClick={() => undo(t)}>Undo</button>}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
