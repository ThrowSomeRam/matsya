// View-model: OwnerView payload in, exactly what the owner page renders out. Pure, no DOM, no LLM.
// Every number and every sentence on the page is made here; owner.ts and charts.ts only lay it out.
//
// The page answers three questions: am I OK (status), who is the problem (who owes you), what do I do
// this week (to-dos). Figures from the payload are estimates from the simulation engine; the copy says so,
// avoids model words, and hides an element when the payload lacks what it needs instead of guessing.

import { rs, shortDate } from "./format";
import type { FanDay, Owed, OwnerView, PlanAction, WhatIf } from "./types";

// ---------------------------------------------------------------- status

/**
 * The four states and every cutoff they use. States are checked in the order of `states`; the first that
 * matches wins. p30 and p90 are the chances of dipping below the cushion at some point within 30 and 90
 * days (the payload's headline risk); p90 is taken as at least p30, since a dip within 30 days is also a
 * dip within 90.
 */
export const STATUS_RULES = {
  /** "More likely than not": the 0.5 line Track B uses for runway and for breaking points. */
  likely: 0.5,
  /** The owner's target when the payload carries none: Track B's default target, decision.eps 0.05 in
   *  intelligence/config/track_b_constants.yaml. */
  fallbackTarget: 0.05,
  /** The status leads with 30 days: the engine beats a flat forecast up to 30 days and loses to it at 90
   *  (docs/results-summary.md, learning-curve replay). 90 days is used only to tell watch from ok. */
  nearDay: 30,
  farDay: 90,
  states: {
    /** A dip within 30 days is more likely than not: p30 >= likely, or the payload's runway day (first day
     *  the chance reaches 0.5) is within 30 days. */
    act_now: { label: "Act this week", serious: true },
    /** Above the owner's target within 30 days (p30 > target), or a dip within 90 days is more likely than
     *  not (p90 >= likely, or a runway day within 90 days). */
    at_risk: { label: "Needs attention", serious: true },
    /** Within target for 30 days but not for 90 (p30 <= target < p90). */
    watch: { label: "Keep an eye on it", serious: false },
    /** Within target even over 90 days (p90 <= target). */
    ok: { label: "On track", serious: false },
  },
} as const;

export type StatusKey = keyof typeof STATUS_RULES.states;

export interface StatusInput { p30?: number | null; p90?: number | null; target?: number | null; runwayDay?: number | null }

/** Why the state was chosen: the next 30 days, or the months after. */
export interface StatusResult { key: StatusKey; reason: "near" | "far" }

const finite = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x);

export function statusOf(s: StatusInput): StatusResult | null {
  if (!finite(s.p30) || !finite(s.p90)) return null;
  const { likely, nearDay, farDay, fallbackTarget } = STATUS_RULES;
  const p30 = s.p30;
  const p90 = Math.max(s.p90, p30);
  const target = finite(s.target) && s.target > 0 ? s.target : fallbackTarget;
  const runway = finite(s.runwayDay) ? s.runwayDay : null;
  if (p30 >= likely || (runway !== null && runway <= nearDay)) return { key: "act_now", reason: "near" };
  if (p30 > target) return { key: "at_risk", reason: "near" };
  if (p90 >= likely || (runway !== null && runway <= farDay)) return { key: "at_risk", reason: "far" };
  if (p90 > target) return { key: "watch", reason: "far" };
  return { key: "ok", reason: "far" };
}

// ---------------------------------------------------------------- chance words

/** How estimates are worded. */
export const CHANCE_WORDS = {
  /** Below this the estimate is just "small": the engine under-predicts low risks (calibration guard,
   *  docs/results-summary.md), so a ratio like "1 in 500" would claim precision it does not have. */
  small: 0.02,
  /** Below `likely`, the upper end of the stated range is the estimate times this. On 100 generator pasts
   *  the true chance in the 0.05-0.20 bin was about 1.7 times the estimate (0.214 vs 0.126); 1.5 widens
   *  toward that without assuming the full gap everywhere. */
  widen: 1.5,
  denominators: [2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50],
  fractions: [[1, 2], [3, 5], [2, 3], [3, 4], [4, 5], [9, 10], [19, 20]] as [number, number][],
};

/** A chance as "1 in 5" or "2 in 3" (no hedge word); "" when not a probability. */
export function oneIn(p: number): string {
  if (!finite(p) || p < 0 || p > 1) return "";
  if (p >= 0.975) return "almost certain";
  if (p >= 0.5) {
    const [a, b] = CHANCE_WORDS.fractions.reduce((x, y) => (Math.abs(y[0] / y[1] - p) < Math.abs(x[0] / x[1] - p) ? y : x));
    return `${a} in ${b}`;
  }
  const lp = Math.log(Math.max(p, 1e-9));
  const n = CHANCE_WORDS.denominators.reduce((x, y) => (Math.abs(Math.log(1 / y) - lp) < Math.abs(Math.log(1 / x) - lp) ? y : x));
  return `1 in ${n}`;
}

/** An estimate as words: "small", "about 1 in 5", "roughly 1 in 5 to 1 in 3", "about 2 in 3". Below
 *  `likely` the range is widened upward (see CHANCE_WORDS.widen). */
export function chanceEstimate(p: number): string {
  if (!finite(p) || p < 0 || p > 1) return "";
  if (p < CHANCE_WORDS.small) return "small";
  const lo = oneIn(p);
  if (p >= STATUS_RULES.likely) return lo === "almost certain" ? lo : `about ${lo}`;
  const hi = oneIn(Math.min(p * CHANCE_WORDS.widen, 0.99));
  return lo === hi ? `about ${lo}` : `roughly ${lo} to ${hi}`;
}

// ---------------------------------------------------------------- payment slowdown as time

/** A what-if that scales every buyer's weekly chance of paying by m (0 < m < 1) stretches the average wait
 *  by 1/m: a weekly chance h gives a geometric wait with mean 1/h weeks, and m h gives 1/(m h). Exact for a
 *  constant weekly chance, close for the per-week chances the engine uses. Returns the extra share of time
 *  (0.153 for m = 0.867), or null when m is not a slowdown. */
export function extraTimeToPay(m: number | null | undefined): number | null {
  if (!finite(m) || m <= 0 || m >= 1) return null;
  return 1 / m - 1;
}

/** "about 15% longer": to the nearest 5% from 10% up, the nearest 1% below. */
export function longerText(extra: number): string {
  const x = extra * 100;
  const r = x >= 10 ? Math.round(x / 5) * 5 : Math.max(1, Math.round(x));
  return `about ${r}% longer`;
}

// ---------------------------------------------------------------- plan

/** decision.stretch penalty in intelligence/config/track_b_constants.yaml: Rs 2,000, the whole cost of
 *  stretching a supplier the MSMED Act does not cover. */
export const STRETCH_PENALTY_PAISE = 2_000_00;

export const MSME_STRETCH_CATCH = "Paying late can trigger interest under MSMED and may cost you a tax deduction.";

/**
 * DEMO HEURISTIC: infer from stretch cost; replace with an explicit payload field.
 * The decision engine prices a stretch as the penalty, plus MSMED interest and the 43B(h) deduction delay
 * only when the supplier is micro or small. So a stretch costing more than the penalty means an MSME
 * supplier. The payload does not say so directly (docs/owner-dashboard-payload-gaps.md).
 */
export function isLikelyMsmeSupplierFromStretchCost(a: PlanAction): boolean {
  return a.kind === "supplier_stretch" && finite(a.cost?.paise) && a.cost.paise > STRETCH_PENALTY_PAISE;
}

/** "Supplier S1", or "Supplier S1 (micro)" once a class is known. Never invents a name: the payload's
 *  stretch target is "<supplier id>:+<days>d". */
export function supplierLabel(targetId: string | null | undefined, msmeClass?: string | null): string {
  const id = (targetId ?? "").split(":")[0].trim();
  if (!id) return "your supplier";
  return `Supplier ${id}` + (msmeClass ? ` (${msmeClass})` : "");
}

export interface Catch { text: string; serious: boolean }
export interface Task { title: string; cost: string | null; catches: Catch[] }

export interface PlanView {
  tasks: Task[];
  optional: Task[];
  optionalLabel: string | null;
  totals: string | null;
  change: string | null;
  statusChange: string | null;
  caveat: string | null;
  empty: string | null;
}

function stretchDays(a: PlanAction): number | null {
  if (finite(a.days)) return a.days;
  const m = /:\+(\d+)d$/.exec(a.target_id ?? "");
  return m ? Number(m[1]) : null;
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

function leverage(a: PlanAction, owed: Owed[]): Catch | null {
  // The chase's buyer: owed rows whose buyer name the payload's own chase sentence names.
  const mine = owed.filter((o) => o.buyer && a.description.includes(o.buyer) && finite(o.interest?.paise));
  const interest = mine.reduce((s, o) => s + o.interest.paise, 0);
  if (!mine.length || interest <= 0) return null;
  return {
    text: `They owe you ${rs(interest)} in late-payment interest under the MSMED Act on ${plural(mine.length, "overdue bill")}. ` +
      "Worth mentioning on the call; claiming it is your choice.",
    serious: false,
  };
}

function task(a: PlanAction, owed: Owed[]): Task {
  const cost = finite(a.cost?.paise) ? a.cost.text : null;
  switch (a.kind) {
    case "supplier_stretch": {
      const d = stretchDays(a);
      const who = supplierLabel(a.target_id);
      return {
        title: d !== null ? `Ask ${who} to wait ${d} more days for payment` : a.description,
        cost: cost && `Estimated cost: ${cost}`,
        catches: [isLikelyMsmeSupplierFromStretchCost(a)
          ? { text: MSME_STRETCH_CATCH, serious: true }
          : { text: "They may say no, and asking can strain the relationship.", serious: false }],
      };
    }
    case "treds_discount":
      return {
        title: a.description, cost: cost && `Fee: about ${cost}`,
        catches: [{ text: "You get the money within days, but less than the full bill.", serious: false }],
      };
    case "overdraft":
      return {
        title: a.description, cost: cost && `Interest: about ${cost}`,
        catches: [{ text: "Pay it back as your customers' payments come in.", serious: false }],
      };
    case "chase": {
      const l = leverage(a, owed);
      return {
        title: a.description, cost: null,
        catches: [{ text: "Keep it friendly: they are a customer.", serious: false }, ...(l ? [l] : [])],
      };
    }
    default:
      return { title: a.description, cost: cost && `Estimated cost: ${cost}`, catches: [] };
  }
}

/**
 * Which steps reach the target. Each step's risk_after is the chance after it and every step before it,
 * added up step by step on the decision engine's fresh paths (an additive approximation; only the last step
 * is a full run). They are used here only to group steps and are never shown: the steps up to the first
 * one at or under the target are the to-dos, the rest change nothing the status shows and are optional.
 * With no usable figures, or a target never reached, every step is a to-do.
 */
export function splitSteps(actions: PlanAction[], target: number): { core: PlanAction[]; optional: PlanAction[] } {
  const steps = [...actions].sort((a, b) => a.rank - b.rank);
  if (!steps.every((a) => finite(a.risk_after?.value))) return { core: steps, optional: [] };
  const k = steps.findIndex((a) => (a.risk_after as { value: number }).value <= target);
  if (k < 0) return { core: steps, optional: [] };
  return { core: steps.slice(0, k + 1), optional: steps.slice(k + 1) };
}

function planView(v: OwnerView, target: number, before: StatusResult | null): PlanView {
  const p = v.plan;
  const actions = p?.actions ?? [];
  const owed = v.owed?.items ?? [];
  const none: PlanView = { tasks: [], optional: [], optionalLabel: null, totals: null, change: null, statusChange: null,
    caveat: p?.caveat ?? null, empty: null };
  if (!actions.length) return { ...none, empty: "Nothing you need to do this week." };
  const { core, optional } = splitSteps(actions, target);
  const totals = finite(p.total_cost?.paise)
    ? `${actions.length === 1 ? "This step costs" : `All ${actions.length} steps together cost`} about ${p.total_cost.text}.`
    : null;
  const b = p.risk_before?.value, a = p.risk_after?.value;
  const change = finite(b) && finite(a)
    ? `Estimated chance of dipping below your cushion within three months: ${chanceEstimate(b)} now, ${chanceEstimate(a)} with ${actions.length === 1 ? "it" : "all of them"}.`
    : null;
  const r = v.risk_by_day ?? [];
  const after = statusOf({ p30: r[STATUS_RULES.nearDay - 1]?.after, p90: r[STATUS_RULES.farDay - 1]?.after, target });
  const statusChange = before && after && after.key !== before.key
    ? `That would take you from "${STATUS_RULES.states[before.key].label}" to "${STATUS_RULES.states[after.key].label}".`
    : null;
  return {
    tasks: core.map((x) => task(x, owed)),
    optional: optional.map((x) => task(x, owed)),
    optionalLabel: optional.length ? `Optional: ${plural(optional.length, "more step")} for extra safety` : null,
    totals, change, statusChange, caveat: p.caveat ?? null, empty: null,
  };
}

// ---------------------------------------------------------------- who owes you

/** A bill's chance of never being paid: from this level it reads as unlikely to be paid (Track B's
 *  LIKELY_DEAD); from `leftOut` the forecast counts on almost none of it. */
export const BILL_DOUBT = { unlikely: 0.5, leftOut: 0.9 };
export const BUYERS_SHOWN = 7;

export interface BuyerRow {
  buyer: string;
  amount: string;
  overdue: string | null;
  habit: string | null;
  slower: string | null;
  lost: string[];
  stakePaise: number;
}

export interface OwesView { rows: BuyerRow[]; more: BuyerRow[]; moreLabel: string | null; empty: string | null }

function habitText(days: number | null | undefined): string | null {
  if (!finite(days)) return null;
  return days <= 0 ? "Usually pays on time." : `Usually pays ${plural(Math.round(days), "day")} after the due date.`;
}

function lostText(bills: { paise: number }[], left: boolean): string {
  const what = `${plural(bills.length, "bill")} worth ${rs(bills.reduce((s, x) => s + x.paise, 0))}`;
  const one = bills.length === 1;
  return left
    ? `${what} ${one ? "is" : "are"} unlikely ever to be paid: already left out of your forecast.`
    : `${what} may never be paid: your forecast counts on only part of ${one ? "it" : "them"}.`;
}

export function buyerRows(v: OwnerView): BuyerRow[] {
  interface Acc { open: number | null; usual: number | null; slower: boolean; bills: Map<string, { paise: number; overdue: boolean; never: number | null }> }
  const by = new Map<string, Acc>();
  const get = (name: string): Acc => {
    let a = by.get(name);
    if (!a) by.set(name, (a = { open: null, usual: null, slower: false, bills: new Map() }));
    return a;
  };
  for (const o of v.owed?.items ?? []) {
    if (!o.buyer || !finite(o.amount?.paise)) continue;
    get(o.buyer).bills.set(o.invoice_id, { paise: o.amount.paise, overdue: o.days_late > 0, never: null });
  }
  for (const d of v.drivers ?? []) {
    if (!d.buyer) continue;
    const a = get(d.buyer);
    if (finite(d.buyer_usual_days_late)) a.usual = d.buyer_usual_days_late;
    if (d.kind === "buyer") {
      a.slower = true;
      if (finite(d.amount?.paise)) a.open = d.amount.paise;
    } else if (d.kind === "invoice" && finite(d.amount?.paise)) {
      const prev = a.bills.get(d.driver_id);
      a.bills.set(d.driver_id, {
        paise: prev?.paise ?? d.amount.paise,
        overdue: (prev?.overdue ?? false) || (d.days_overdue ?? 0) > 0,
        never: finite(d.chance_never_paid?.value) ? d.chance_never_paid.value : null,
      });
    }
  }
  const rows: BuyerRow[] = [];
  for (const [buyer, a] of by) {
    const bills = [...a.bills.values()];
    const overdue = bills.filter((b) => b.overdue).reduce((s, b) => s + b.paise, 0);
    const known = bills.reduce((s, b) => s + b.paise, 0);
    const stake = a.open ?? known;
    if (stake <= 0) continue;
    const left = bills.filter((b) => b.never !== null && b.never >= BILL_DOUBT.leftOut);
    const partly = bills.filter((b) => b.never !== null && b.never >= BILL_DOUBT.unlikely && b.never < BILL_DOUBT.leftOut);
    rows.push({
      buyer,
      amount: a.open !== null ? `${rs(a.open)} open` : overdue > 0 ? `${rs(overdue)} overdue` : `${rs(known)} open`,
      overdue: a.open !== null && overdue > 0 ? `${rs(Math.min(overdue, a.open))} of it overdue` : null,
      habit: habitText(a.usual),
      slower: a.slower ? "Pays more slowly than similar customers." : null,
      lost: [...(left.length ? [lostText(left, true)] : []), ...(partly.length ? [lostText(partly, false)] : [])],
      stakePaise: stake,
    });
  }
  return rows.sort((x, y) => y.stakePaise - x.stakePaise || x.buyer.localeCompare(y.buyer));
}

function owesView(v: OwnerView): OwesView {
  const all = buyerRows(v);
  if (!all.length) return { rows: [], more: [], moreLabel: null, empty: "No customer owes you money right now." };
  const rows = all.slice(0, BUYERS_SHOWN), more = all.slice(BUYERS_SHOWN);
  const moreLabel = more.length
    ? `${plural(more.length, "more customer")} with ${rs(more.reduce((s, r) => s + r.stakePaise, 0))} outstanding`
    : null;
  return { rows, more, moreLabel, empty: null };
}

// ---------------------------------------------------------------- balance chart

export interface ChartPoint { date: string; median: number; lo: number; hi: number; tip: string }
export interface ChartLabel { date: string; paise: number; text: string }
export interface BalanceChart {
  near: ChartPoint[];
  far: ChartPoint[];
  cushionPaise: number | null;
  cushionLabel: string | null;
  labels: ChartLabel[];
  caption: string;
}

function point(date: string, median: number, lo: number, hi: number): ChartPoint {
  const tip = lo === hi ? `${shortDate(date)}\nToday ${rs(median)}`
    : `${shortDate(date)}\nMiddle estimate ${rs(median)}\nHalf of estimates ${rs(lo)} to ${rs(hi)}`;
  return { date, median, lo, hi, tip };
}

function chartView(v: OwnerView): BalanceChart | null {
  const days = (v.fan?.days ?? []).filter((d: FanDay) =>
    finite(d.median_paise) && finite(d.lo50_paise) && finite(d.hi50_paise) && typeof d.date === "string");
  if (days.length < 2) return null;
  const pts = days.map((d) => point(d.date, d.median_paise, d.lo50_paise, d.hi50_paise));
  const today = v.headline?.balance_today?.paise;
  if (finite(today) && v.headline.balance_date) pts.unshift(point(v.headline.balance_date, today, today, today));
  const cut = days.findIndex((d) => d.day >= STATUS_RULES.nearDay);
  const split = cut < 0 ? pts.length : pts.findIndex((p) => p.date === days[cut].date) + 1;
  const near = pts.slice(0, split), far = split < pts.length ? pts.slice(split - 1) : [];
  const t = v.fan?.threshold;
  const end = pts[pts.length - 1];
  return {
    near, far,
    cushionPaise: finite(t?.paise) ? t.paise : null,
    cushionLabel: finite(t?.paise) ? (t.paise > 0 ? `Your cushion, ${t.text}` : "Zero balance") : null,
    labels: [
      { date: end.date, paise: end.median, text: "Middle estimate" },
      { date: end.date, paise: (end.median + end.hi) / 2, text: "Half of estimates" },
    ],
    caption: far.length
      ? `The line is our middle estimate and half of our estimates fall in the shaded band. After ${STATUS_RULES.nearDay} days the estimate is less reliable, so it is faded.`
      : "The line is our middle estimate and half of our estimates fall in the shaded band.",
  };
}

// ---------------------------------------------------------------- be ready for

/**
 * What the payload does not say about a preset, keyed by its name. Mirrors
 * intelligence/config/scenario_presets.yaml: which kind of rate change it is (the payload carries the
 * figures but not the transform type) and view_days (the horizons it needs; default 90). A preset that needs
 * a longer horizon than the payload computed is not shown. Both belong in the payload
 * (docs/owner-dashboard-payload-gaps.md).
 */
export const PRESET_HINTS: Record<string, { rate?: "buyers" | "orders"; viewDays?: number[] }> = {
  delayed_customer_payments: { rate: "buyers" },
  reduced_sales: { rate: "orders", viewDays: [90, 180] },
};
export const DEFAULT_VIEW_DAYS = [90];
export const READY_SHOWN = 3;

export function computedHorizon(w: WhatIf): number | null {
  const n = w.risk_by_day?.length ?? w.fan?.length;
  return finite(n) && n > 0 ? n : null;
}

export function horizonCovered(w: WhatIf): boolean {
  const h = computedHorizon(w);
  const need = Math.max(...(PRESET_HINTS[w.preset]?.viewDays ?? DEFAULT_VIEW_DAYS));
  return h !== null && need <= h;
}

/** The breaking point in owner terms, or null when it has none or its kind cannot be told. */
export function readyText(w: WhatIf): string | null {
  if (!finite(w.breaking_magnitude)) return null;
  if (finite(w.breaking_amount?.paise)) return `an unexpected bill of ${w.breaking_amount.text} or more arrives`;
  if (finite(w.breaking_days) && finite(w.days_from)) {
    return `suppliers want paying within ${plural(w.breaking_days, "day")} instead of ${w.days_from}`;
  }
  if (finite(w.breaking_days)) return `${w.name} by ${plural(w.breaking_days, "day")} or more`;
  const rate = PRESET_HINTS[w.preset]?.rate;
  if (rate === "buyers") {
    const extra = extraTimeToPay(w.breaking_magnitude);
    return extra === null ? null : `your customers take ${longerText(extra)} to pay than they do now`;
  }
  if (rate === "orders" && w.breaking_share) return `orders fall by ${w.breaking_share.text} or more`;
  return null;
}

export interface ReadyView { lead: string; items: string[] }

function readyView(v: OwnerView): ReadyView | null {
  const items = (v.what_ifs ?? [])
    .filter(horizonCovered)
    .map((w) => ({ w, text: readyText(w) }))
    .filter((x): x is { w: WhatIf; text: string } => x.text !== null)
    .sort((a, b) => (b.w.risk_90?.value ?? 0) - (a.w.risk_90?.value ?? 0))
    .slice(0, READY_SHOWN)
    .map((x) => x.text);
  if (!items.length) return null;
  return { lead: "A dip below your cushion within three months would become more likely than not if:", items };
}

// ---------------------------------------------------------------- the page

export interface StatusView {
  key: StatusKey;
  label: string;
  serious: boolean;
  headline: string;
  balance: string | null;
  range30: string | null;
  estimateNote: string;
}

export const SECTIONS = {
  todo: "This week's to-dos",
  owes: "Who owes you",
  owesNote: "Customers with money outstanding, biggest amounts first.",
  outlook: "Your balance over the next 90 days",
  ready: "Be ready for",
};

export interface OwnerScreen {
  title: string;
  sections: typeof SECTIONS;
  status: StatusView | null;
  plan: PlanView;
  owes: OwesView;
  chart: BalanceChart | null;
  ready: ReadyView | null;
  footer: { freshness: string | null; asOf: string | null; source: string | null; evidenceText: string; evidenceHref: string };
}

function riskAt(v: OwnerView, day: number): number | null {
  const h = v.headline?.risk?.find((r) => r.day === day)?.chance?.value;
  if (finite(h)) return h;
  const r = v.risk_by_day?.[day - 1];
  return r && r.day === day && finite(r.before) ? r.before : null;
}

function longDate(iso: string): string {
  return new Date(iso + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

function statusView(v: OwnerView, s: StatusResult | null): StatusView | null {
  if (!s) return null;
  const p30 = riskAt(v, STATUS_RULES.nearDay) as number, p90 = riskAt(v, STATUS_RULES.farDay) as number;
  const t = v.fan?.threshold;
  const below = finite(t?.paise) ? (t.paise > 0 ? `below your ${t.text} cushion` : "below zero") : "below your cushion";
  const above = finite(t?.paise) ? (t.paise > 0 ? `above your ${t.text} cushion` : "above zero") : "above your cushion";
  const headline = {
    act_now: `A dip ${below} in the next 30 days is more likely than not. Estimated chance: ${chanceEstimate(p30)}.`,
    at_risk: s.reason === "near"
      ? `Estimated chance of a dip ${below} in the next 30 days: ${chanceEstimate(p30)}.`
      : `The next 30 days look manageable, but a dip ${below} within three months is more likely than not.`,
    watch: `The next 30 days look fine (estimated chance of a dip ${below}: ${chanceEstimate(p30)}). ` +
      "The risk builds after that, so it is worth acting early.",
    ok: `Your balance is likely to stay ${above} for the next three months. Estimated chance of a dip: ${chanceEstimate(Math.max(p90, p30))}.`,
  }[s.key];
  const d30 = v.fan?.days?.find((d) => d.day === STATUS_RULES.nearDay);
  const range30 = d30 && finite(d30.lo50_paise) && finite(d30.hi50_paise)
    ? `Likely balance on ${shortDate(d30.date)}: ${rs(d30.lo50_paise)} to ${rs(d30.hi50_paise)} (the middle half of our estimates).`
    : null;
  const bt = v.headline?.balance_today;
  return {
    key: s.key, label: STATUS_RULES.states[s.key].label, serious: STATUS_RULES.states[s.key].serious, headline,
    balance: finite(bt?.paise) ? `${bt.text} in the bank today` : null,
    range30,
    estimateNote: "These are estimates from your bank and invoice records, not guarantees.",
  };
}

export function buildOwnerScreen(v: OwnerView, source: "api" | "demo file" = "api"): OwnerScreen {
  const t = v.plan?.target?.value;
  const target = finite(t) && t > 0 ? t : STATUS_RULES.fallbackTarget;
  const s = statusOf({
    p30: riskAt(v, STATUS_RULES.nearDay), p90: riskAt(v, STATUS_RULES.farDay), target: v.plan?.target?.value,
    runwayDay: v.headline?.runway_day,
  });
  return {
    title: "Your cash outlook",
    sections: SECTIONS,
    status: statusView(v, s),
    plan: planView(v, target, s),
    owes: owesView(v),
    chart: chartView(v),
    ready: readyView(v),
    footer: {
      freshness: v.meta?.data_freshness ?? null,
      asOf: v.meta?.as_of ? `All figures are estimates as of ${longDate(v.meta.as_of)}.` : null,
      source: source === "demo file" ? "Showing a saved demo copy." : null,
      evidenceText: "How good are these estimates?",
      evidenceHref: "/evidence/",
    },
  };
}

// Words the page must never show (model jargon, or claims the payload cannot back). Tests check every string.
export const BANNED = [/hazard/i, /quantile/i, /cvar/i, /p_short/i, /runway/i, /\bbins?\b/i, /\bpoints?\b/i,
  /checked against what actually happens/i, /calibrat/i];

