// Shapes returned by the API. Decimals arrive as strings to stay exact.

export type Dec = string;

export type Figure = { value: Dec; currency: string | null; ref: string | null; stale?: string | null; estimated?: boolean };

export type ExplainNode = {
  label: string;
  value?: string | null;
  formula?: string | null;
  source?: "ledger" | "price" | "fx" | "import" | "manual" | "rule" | "derived" | null;
  ref?: string | null;
  stale?: boolean;
  note?: string | null;
  children: ExplainNode[];
};

export type AuthStatus = { setup_required: boolean; signed_in: boolean; open?: boolean; user: { id: number; username: string; totp_enabled: boolean } | null };

export type Person = { id: number; entity_id: number; name: string; role: "adult" | "child"; birth_date: string | null; tracking: "full" | "shared"; is_self: boolean };

export type EntityRow = { id: number; kind: string; name: string; person_id: number | null; role: string | null; tracking: string | null; is_self: boolean };

export type Owner = { entity_id: number; name: string; share: Dec; valid_from: string };

export type Account = {
  id: number;
  name: string;
  kind: string;
  kind_label: string;
  institution_id: number | null;
  institution: string | null;
  currency: string;
  opened_on: string | null;
  closed_on: string | null;
  external_ref: string | null;
  notes: string | null;
  owners: Owner[];
};

export type Institution = { id: number; name: string; kind: string; country: string | null };

export type Instrument = {
  id: number;
  type: string;
  name: string;
  isin: string | null;
  symbol: string | null;
  mic: string | null;
  currency: string;
  price_symbol: string | null;
  asset_class: string | null;
};

export type DuplicateRow = {
  id: number;
  name: string;
  isin: string | null;
  type: string;
  currency: string;
  quantity: string;
  postings: number;
  price_source: string | null;
  created_from: string | null;
  mergeable: boolean;
};
export type DuplicateGroup = { keep_id: number; reasons: string[]; instruments: DuplicateRow[] };
export type MergeResult = { keep_id: number; dropped_id: number; moved: Record<string, number> };

export type PriceCoverage = {
  id: number;
  name: string;
  isin: string | null;
  type: string;
  currency: string;
  price_source: string | null;
  symbol: string | null;
  last_date: string | null;
  last_close: string | null;
  last_kind: string | null;
  status: "ok" | "stale" | "missing" | "no_feed";
  reason: string | null;
};

export type Job = {
  name: string;
  title: string;
  cron: string | null;
  last_status: string | null;
  last_started: string | null;
  last_finished: string | null;
  last_summary: string | null;
  last_run_id: number | null;
};

export type SearchHit = { kind: string; id: string; title: string; subtitle: string | null; route: string; ref: string | null };

// ---- Life-OS: goals, habits, calendar, health ----------------------------------------------------------------

export type GoalKind = "savings" | "net_worth" | "portfolio" | "debt_payoff" | "custom";
export type Goal = {
  id: number;
  title: string;
  kind: GoalKind;
  entity_id: number | null;
  account_id: number | null;
  target_amount: Dec;
  target_date: string | null;
  start_date: string;
  start_amount: Dec;
  reached_on: string | null;
  notes: string | null;
  current: Dec;
  pct: Dec;
  status: "on_track" | "behind" | "reached";
  required_monthly: Dec | null;
  actual_monthly: Dec | null;
  projected_date: string | null;
  expected_now: Dec | null;
  manual: boolean;
  entries: { id: number; date: string; amount: Dec; note: string | null }[];
};

export type Habit = {
  id: number;
  title: string;
  cadence: "daily" | "weekly";
  times_per_week: number;
  person_id: number | null;
  active: boolean;
  current_streak: number;
  best_streak: number;
  rate_30d: Dec;
  done_today: boolean;
  done_this_week: number;
  warning: "broken" | "at_risk" | null;
  recent: string[];
};

export type CalCategory = "finance" | "health" | "home" | "family" | "other";
export type CalOccurrence = {
  date: string;
  end_date: string | null;
  time: string | null;
  end_time: string | null;
  all_day: boolean;
  title: string;
  source: string;
  category: CalCategory;
  route: string;
  event_id: number | null;
  recurring: boolean;
  person_id: number | null;
  notes: string | null;
  private: boolean;
};
export type CalEvent = {
  id: number;
  title: string;
  start_date: string;
  start_time: string | null;
  end_date: string | null;
  end_time: string | null;
  all_day: boolean;
  recurrence: "none" | "weekly" | "monthly" | "yearly";
  category: CalCategory;
  person_id: number | null;
  notes: string | null;
  reminder_days_before: number | null;
};

export type HealthTarget = { id: number; direction: "below" | "above" | "range"; low: Dec | null; high: Dec | null; high2: Dec | null };
export type HealthSeries = {
  series: string;
  kind: string;
  label: string;
  unit: string;
  target: HealthTarget | null;
  latest: { date: string; value: Dec; value2: Dec | null; note: string | null } | null;
  avg_30: { value: Dec; value2: Dec | null } | null;
  avg_90: { value: Dec; value2: Dec | null } | null;
  count: number;
  status: "ok" | "above" | "below" | "no_target" | "no_data";
  points: { date: string; value: Dec; value2: Dec | null }[];
};
export type Medication = { id: number; person_id: number; name: string; dose: string | null; schedule: string | null; active: boolean; refill_date: string | null; notes: string | null };
