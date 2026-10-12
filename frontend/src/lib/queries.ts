// Data hooks for the analytics endpoints; keys include the context so views stay in sync with the context bar.
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { Ctx } from "./context";
import type { Figure } from "./types";

export type HoldingRow = {
  instrument_id: number;
  name: string;
  symbol: string | null;
  isin: string | null;
  type: string;
  currency: string;
  is_cash: boolean;
  quantity: string;
  avg_cost: string | null;
  price: string | null;
  price_currency: string | null;
  price_date: string | null;
  price_source: string | null;
  value: Figure;
  cost: Figure;
  unrealized: Figure;
  unrealized_pct: number | null;
  day_pct: number | null;
  weight: number;
};
export type HoldingsResp = {
  as_of: string;
  accounts: { account_id: number; name: string; kind: string; kind_label: string; value: Figure; holdings: HoldingRow[] }[];
  total: Figure;
  cost: Figure;
  unrealized: Figure;
  warnings: string[];
};
export type PeriodPerf = {
  start: string;
  end: string;
  days: number;
  annualised: boolean;
  twr: Figure | null;
  xirr: Figure | null;
  net_flows: Figure;
  gain: Figure;
  benchmark: number | null;
};
export type PerfResp = {
  periods: Record<string, PeriodPerf>;
  series: { date: string; value: string; contributions: string }[];
  benchmark: { instrument_id: number; name: string } | null;
};
export type IncomeResp = {
  dividends: Figure;
  withholding: Figure;
  interest: Figure;
  forecast_12m: Figure;
  yield_on_cost: number | null;
  forecast_yield: number | null;
  forecast_sources: { yahoo: number; ledger: number };
  by_instrument: { name: string; eur: string }[];
};
export type NwItem = { account_id: number; name: string; kind: string; kind_label: string; share: string; value: Figure; gross: string; method: string; as_of: string | null };
export type NwResp = {
  as_of: string;
  net_worth: Figure;
  assets: Figure;
  liabilities: Figure;
  liquid: Figure;
  illiquid: Figure;
  home_equity: Figure;
  groups: { id: string; label: string; total: Figure; items: NwItem[] }[];
  monthly_spending: string | null;
  emergency_months: number | null;
  liquid_months: number | null;
};
export type NwPoint = { date: string; assets: string; liabilities: string; net_worth: string; investments: string; property: string; cash: string };
export type ChangeResp = { start: string; end: string; contributions: Figure; market: Figure; debt_paydown: Figure; revaluation: Figure; total: Figure };
export type Attention = {
  severity: "info" | "warn" | "bad";
  title: string;
  detail: string;
  route?: string;
  ref?: string;
  chip?: string;
  action?: { label: string; path: string };
};
export type Kid = {
  person_id: number;
  entity_id: number;
  name: string;
  birth_date: string | null;
  value: Figure | null;
  contributions?: Figure;
  gain?: Figure;
  twr_1y?: Figure | null;
  twr_max?: Figure | null;
  since?: string;
  series?: { date: string; value: string }[];
};

const s = (ctx: Ctx) => encodeURIComponent(ctx.scope);

export const useHoldings = (ctx: Ctx) => useQuery({ queryKey: ["holdings", ctx.scope], queryFn: () => api<HoldingsResp>(`/api/portfolio/holdings?scope=${s(ctx)}`) });
export const usePerformance = (ctx: Ctx) =>
  useQuery({ queryKey: ["performance", ctx.scope, ctx.period], queryFn: () => api<PerfResp>(`/api/portfolio/performance?scope=${s(ctx)}&period=${ctx.period}`) });
export const useIncome = (ctx: Ctx) => useQuery({ queryKey: ["income", ctx.scope, ctx.period], queryFn: () => api<IncomeResp>(`/api/portfolio/income?scope=${s(ctx)}&period=${ctx.period}`) });
export const useNetworth = (ctx: Ctx) => useQuery({ queryKey: ["networth", ctx.scope], queryFn: () => api<NwResp>(`/api/networth?scope=${s(ctx)}`) });
export const useNwHistory = (ctx: Ctx) =>
  useQuery({ queryKey: ["nw-history", ctx.scope, ctx.period], queryFn: () => api<{ series: NwPoint[] }>(`/api/networth/history?scope=${s(ctx)}&period=${ctx.period}`) });
export const useNwChange = (ctx: Ctx) => useQuery({ queryKey: ["nw-change", ctx.scope, ctx.period], queryFn: () => api<ChangeResp>(`/api/networth/change?scope=${s(ctx)}&period=${ctx.period}`) });
export const useAttention = () => useQuery({ queryKey: ["attention"], queryFn: () => api<Attention[]>("/api/attention"), refetchInterval: 60_000 });
export const useKids = () => useQuery({ queryKey: ["kids"], queryFn: () => api<Kid[]>("/api/portfolio/kids") });

export const ANALYTICS_KEYS = ["holdings", "performance", "income", "networth", "nw-history", "nw-change", "attention", "kids", "freshness"];
