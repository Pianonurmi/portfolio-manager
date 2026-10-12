// The only way to render a number (UX-01, UX-05): fi-FI format, gain/loss sign, stale/estimated badges, explainable.
import { arrow, money, num, pct, quantity, signedMoney, signedPct } from "../lib/format";
import type { Figure as F } from "../lib/types";
import { useInspector } from "./Inspector";

type Props = {
  f: F | null | undefined;
  kind?: "money" | "pct" | "qty";
  delta?: boolean;
  digits?: number;
  title?: string;
};

export function Figure({ f, kind = "money", delta = false, digits = 0, title }: Props) {
  const insp = useInspector();
  if (!f) return <span style={{ color: "var(--muted)" }}>—</span>;
  const n = num(f.value);
  let text: string;
  if (kind === "pct") text = delta ? `${arrow(n)} ${signedPct(n, digits || 1)}` : pct(n, digits || 1);
  else if (kind === "qty") text = quantity(n);
  else text = delta ? `${arrow(n)} ${signedMoney(n, f.currency ?? "EUR", digits)}` : money(n, f.currency ?? "EUR", digits);
  const cls = ["fig", delta && n > 0 ? "gain" : "", delta && n < 0 ? "loss" : "", f.estimated ? "est" : ""].filter(Boolean).join(" ");
  const body = (
    <>
      {text}
    </>
  );
  return (
    <>
      {f.ref ? (
        <button className={cls} onClick={(e) => (e.stopPropagation(), insp.open(f.ref!, title))} title={title ?? "Explain this number"}>
          {body}
        </button>
      ) : (
        <span className={cls}>{body}</span>
      )}
      {f.estimated && <span className="tag-est">est.</span>}
      {f.stale && <span className="tag-stale">● {f.stale}</span>}
    </>
  );
}
