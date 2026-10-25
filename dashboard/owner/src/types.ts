// The OwnerView payload (intelligence/src/matsya_intelligence/present/owner_view.py). Read-only here.
// Fields the view-model can live without are optional: a payload that lacks one hides that element.

export interface Money { paise: number; text: string }
export interface Pct { value: number; text: string }
export interface RiskAt { day: number; date: string; chance: Pct }

export interface FanDay {
  day: number; date: string; median_paise: number; lo50_paise: number; hi50_paise: number;
  lo90_paise: number; hi90_paise: number; calibrated: boolean;
}

export interface Driver {
  rank: number; kind: "invoice" | "buyer"; driver_id: string; buyer: string; label: string; amount: Money | null;
  contribution: Pct; contribution_es?: Money; days_overdue?: number | null; days_to_due?: number | null;
  buyer_usual_days_late?: number | null; chance_never_paid?: Pct | null; reason: string;
}

export interface PlanAction {
  rank: number; kind: string; target_id: string | null; description: string; cost: Money; risk_after?: Pct;
  amount?: Money | null; days?: number | null; on_date?: string | null;
}

export interface WhatIf {
  preset: string; name: string; description: string; risk_90: Pct; change_90: number; change_text?: string;
  runway_lost_days?: number; runway_text?: string; breaking_point?: string; breaking_magnitude: number | null;
  share?: Pct | null; amount?: Money | null; days?: number | null; days_from?: number | null; on_date?: string | null;
  breaking_share?: Pct | null; breaking_amount?: Money | null; breaking_days?: number | null;
  fan?: FanDay[]; risk_by_day?: number[];
}

export interface Owed { invoice_id: string; buyer: string; amount: Money; days_late: number; interest: Money }

export interface OwnerView {
  headline: {
    balance_today: Money; balance_date: string; risk: RiskAt[]; runway_day: number | null; runway_date?: string | null;
    runway_text?: string; drivers_sentence?: string;
  };
  fan: { days: FanDay[]; threshold?: Money; calibrated_through_day?: number; note?: string };
  risk_by_day: { day: number; date: string; before: number; after: number | null }[];
  drivers: Driver[];
  plan: {
    method?: string; actions: PlanAction[]; n_steps?: number; total_cost: Money; risk_before: Pct; risk_after: Pct;
    target?: Pct | null; target_met: boolean; summary?: string; caveat: string | null;
  };
  what_ifs: WhatIf[];
  owed: { items: Owed[]; total_interest: Money; note?: string };
  meta: {
    schema_version: string; tenant_id: string; run_id: string; as_of: string; data_through: string;
    data_lag_days: number; data_freshness?: string; model_versions?: Record<string, string>; plan_method?: string;
    plan_run_id?: string; n_paths?: number;
  };
}

export interface Series { q05: number[]; q25: number[]; q50: number[]; q75: number[]; q95: number[]; p_short: number[] }

export interface Evidence {
  as_of: string; threshold_paise: number; opening_paise: number; days: number[]; dates: string[];
  series: { truth: Series; naive: Series; ours: Series };
  p_short_90: { truth: number; naive: number; ours: number };
  dead_bills: { buyer: string; amount_paise: number; age_days: number; days_overdue: number; p_never: number; truly_never_paid: boolean }[];
  across_worlds: {
    n_worlds: number; n_futures: number; median_err_90_rs: { naive: number; ours: number };
    median_err_30_rs: { naive: number; ours: number }; p_short_90_err: { naive: number; ours: number };
    truth_shortfalls: number; caught: { naive: number; ours: number }; false_alarms: { naive: number; ours: number };
    demo_dead_bills: number; demo_dead_paise: number;
  };
}

// Obligations payload (dashboard/owner/precompute/obligations.py, schema obligations-v1): the obligations
// detector's next 90 days, with how tight each due date is in the owner view's own runs.
export interface ObligationItem {
  kind: string; due_date: string; day: number; confidence: number; amount_owner: string;
  amount_paise: number | null; amount_lo_paise: number | null; amount_hi_paise: number | null;
  p_below_cushion_that_day: number; median_balance_that_day_paise: number;
}

export interface ObligationsPayload {
  schema_version: string; tenant_id: string; run_id: string; as_of: string; n_paths: number; cushion_paise: number;
  items: ObligationItem[];
  supplier_payments_30d: { days: number; median_paise: number; lo_paise: number; hi_paise: number } | null;
}
