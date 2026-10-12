// Global context bar state lives in the URL, so every view is linkable (UX-08).
import { useCallback } from "react";
import { useSearchParams } from "react-router";

export const PERIODS = ["1M", "YTD", "1Y", "3Y", "5Y", "Max"] as const;
export type Period = (typeof PERIODS)[number];

export type Ctx = { scope: string; period: Period; ccy: "EUR" | "native" };

export function useCtx(): [Ctx, (patch: Partial<Ctx>) => void] {
  const [params, setParams] = useSearchParams();
  const period = (PERIODS as readonly string[]).includes(params.get("period") ?? "") ? (params.get("period") as Period) : "1Y";
  const ctx: Ctx = { scope: params.get("scope") ?? "household", period, ccy: params.get("ccy") === "native" ? "native" : "EUR" };
  const set = useCallback(
    (patch: Partial<Ctx>) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(patch)) {
            const dflt = k === "scope" ? "household" : k === "period" ? "1Y" : "EUR";
            if (v == null || v === dflt) next.delete(k);
            else next.set(k, String(v));
          }
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );
  return [ctx, set];
}

export function ctxQuery(ctx: Ctx): string {
  return `scope=${encodeURIComponent(ctx.scope)}&period=${ctx.period}`;
}
