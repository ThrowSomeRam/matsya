// View-model: OwnerView payload in, exactly what the owner page renders out.

import { rs, shortDate } from "./format";
import type { FanDay, ObligationItem, ObligationsPayload, Owed, OwnerView, PlanAction, WhatIf } from "./types";

// ---------------------------------------------------------------- status

/** The four states and every cutoff they use. */
export const STATUS_RULES = {
  /** "More likely than not": the 0.5 line Track B uses for runway and for breaking points. */
  likely: 0.5,
  /** Fallback target: Track B's default decision.eps. */
  fallbackTarget: 0.05,
  /** Lead with 30 days: the engine beats a flat forecast there and loses at 90. */
  nearDay: 30,
  farDay: 90,
  states: {
    /** p30 >= likely, or runway within 30 days. */
    act_now: { label: "Act this week", serious: true },
    /** p30 > target, or p90 >= likely (runway within 90 days). */
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

// ---------------------------------------------------------------- chance as a percentage

/** How estimates are worded: percentages, as a range below `likely`. */
export const CHANCE_FORMAT = {
  /** Below this the estimate reads "under 2%": low risks are under-predicted. */
  small: 0.02,
  /** Above this it reads "over 95%". */
  high: 0.95,
  /** Below `likely`, the top of the range is the estimate times this. */
  widen: 1.5,
};

/** A chance in whole percent: to the nearest 5 from 10% up, the nearest 1 below. */
export function roundPct(p: number): number {
  const v = p * 100;
  return v >= 10 ? Math.round(v / 5) * 5 : Math.max(1, Math.round(v));
}

/** An estimate as a percentage: "under 2%", "20–30%", "about 70%", "over 95%". */
export function chanceEstimate(p: number): string {
  if (!finite(p) || p < 0 || p > 1) return "";
  const { small, high, widen } = CHANCE_FORMAT;
  if (p < small) return `under ${Math.round(small * 100)}%`;
  if (p > high) return `over ${Math.round(high * 100)}%`;
  const lo = roundPct(p);
  if (p >= STATUS_RULES.likely) return `about ${lo}%`;
  const hi = Math.min(roundPct(p * widen), Math.round(high * 100));
  return lo === hi ? `about ${lo}%` : `${lo}–${hi}%`;
}

// ---------------------------------------------------------------- payment slowdown as time

/** Scaling the weekly chance of paying by m stretches the average wait by 1/m; returns 1/m - 1. */
export function extraTimeToPay(m: number | null | undefined): number | null {
  if (!finite(m) || m <= 0 || m >= 1) return null;
  return 1 / m - 1;
}

/** Extra time as whole percent: to the nearest 5 from 10 up, the nearest 1 below. */
export function longerPct(extra: number): number {
  const x = extra * 100;
  return x >= 10 ? Math.round(x / 5) * 5 : Math.max(1, Math.round(x));
}

/** "about 15% longer". */
export function longerText(extra: number): string {
  return `about ${longerPct(extra)}% longer`;
}

// ---------------------------------------------------------------- plan

/** decision.stretch penalty (Rs 2,000): the whole cost of stretching a non-MSME supplier. */
export const STRETCH_PENALTY_PAISE = 2_000_00;

export const MSME_STRETCH_CATCH = "Paying late can trigger interest under MSMED and may cost you a tax deduction.";

/** DEMO HEURISTIC: infer from stretch cost; replace with an explicit payload field. */
export function isLikelyMsmeSupplierFromStretchCost(a: PlanAction): boolean {
  return a.kind === "supplier_stretch" && finite(a.cost?.paise) && a.cost.paise > STRETCH_PENALTY_PAISE;
}

/** "Supplier S1", or "Supplier S1 (micro)" once a class is known. */
export function supplierLabel(targetId: string | null | undefined, msmeClass?: string | null): string {
  const id = (targetId ?? "").split(":")[0].trim();
  if (!id) return "your supplier";
  return `Supplier ${id}` + (msmeClass ? ` (${msmeClass})` : "");
}

export interface Catch { text: string; serious: boolean }
/** What a task's action button does (automations.ts drafts the message). Facts only, from the payload. */
export type TaskAction =
  | { kind: "remind"; buyer: string }
  | { kind: "supplier"; supplier: string; days: number | null; msme: boolean }
  | { kind: "treds"; billPaise: number | null; feePaise: number | null; buyer: string | null }
  | { kind: "overdraft"; amountPaise: number | null; date: string | null };

export interface Task { title: string; cost: string | null; catches: Catch[]; action: TaskAction | null }

export interface PlanView {
  /** Before and after the whole plan, fresh-path checked: "35–50%" -> "under 2%". */
  compare: { now: string; after: string; nowLabel: string; afterLabel: string; caption: string } | null;
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

function task(a: PlanAction, owed: Owed[], buyers: string[]): Task {
  const cost = finite(a.cost?.paise) ? a.cost.text : null;
  // The buyer a task is about: a known buyer name inside the payload's own sentence (longest match first).
  const named = [...buyers].sort((x, y) => y.length - x.length).find((b) => a.description.includes(b)) ?? null;
  switch (a.kind) {
    case "supplier_stretch": {
      const d = stretchDays(a);
      const who = supplierLabel(a.target_id);
      const msme = isLikelyMsmeSupplierFromStretchCost(a);
      return {
        title: d !== null ? `Ask ${who} to wait ${d} more days for payment` : a.description,
        cost: cost && `Estimated cost: ${cost}`,
        catches: [msme
          ? { text: MSME_STRETCH_CATCH, serious: true }
          : { text: "They may say no, and asking can strain the relationship.", serious: false }],
        action: { kind: "supplier", supplier: who, days: d, msme },
      };
    }
    case "treds_discount":
      return {
        title: a.description, cost: cost && `Fee: about ${cost}`,
        catches: [{ text: "You get the money within days, but less than the full bill.", serious: false }],
        action: { kind: "treds", billPaise: finite(a.amount?.paise) ? a.amount.paise : null,
          feePaise: finite(a.cost?.paise) ? a.cost.paise : null, buyer: named },
      };
    case "overdraft":
      return {
        title: a.description, cost: cost && `Interest: about ${cost}`,
        catches: [{ text: "Pay it back as your customers' payments come in.", serious: false }],
        action: { kind: "overdraft", amountPaise: finite(a.amount?.paise) ? a.amount.paise : null, date: a.on_date ?? null },
      };
    case "chase": {
      const l = leverage(a, owed);
      return {
        title: a.description, cost: null,
        catches: [{ text: "Keep it friendly: they are a customer.", serious: false }, ...(l ? [l] : [])],
        action: named ? { kind: "remind", buyer: named } : null,
      };
    }
    default:
      return { title: a.description, cost: cost && `Estimated cost: ${cost}`, catches: [], action: null };
  }
}

/** Which steps reach the target. */
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
  const buyers = [...new Set([...owed.map((o) => o.buyer), ...(v.drivers ?? []).map((d) => d.buyer)].filter(Boolean))];
  const none: PlanView = { compare: null, tasks: [], optional: [], optionalLabel: null, totals: null, change: null, statusChange: null,
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
  const compare = finite(b) && finite(a)
    ? { now: chanceEstimate(b), after: chanceEstimate(a), nowLabel: "now",
        afterLabel: actions.length === 1 ? "with this step" : "with all steps",
        caption: "chance of a dip below your cushion within three months" }
    : null;
  return {
    compare,
    tasks: core.map((x) => task(x, owed, buyers)),
    optional: optional.map((x) => task(x, owed, buyers)),
    optionalLabel: optional.length ? `Optional: ${plural(optional.length, "more step")} for extra safety` : null,
    totals, change, statusChange, caveat: p.caveat ?? null, empty: null,
  };
}

// ---------------------------------------------------------------- who owes you

/** Never-paid chance at which a bill reads as unlikely, and as left out of the forecast. */
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
  /** Bar widths, 0-1: this customer's amount against the largest, and the overdue part of it. */
  bar: { stake: number; overdue: number };
  overduePaise: number;
  /** Overdue bills that carry MSMED late-payment interest (payload owed block), for reminders and notices. */
  owedBills: { principalPaise: number; daysLate: number; interestPaise: number }[];
  interestPaise: number;
  /** Short table cells; "—" when unknown. */
  cells: { outstanding: string; overdue: string; pays: string; slow: string | null; neverPaid: string; neverPaidDetail: string | null };
}

export interface OwesView {
  rows: BuyerRow[]; more: BuyerRow[]; moreLabel: string | null; empty: string | null;
  totalOverduePaise: number; overdueCustomers: number;
}

function habitText(days: number | null | undefined, slower: boolean): string | null {
  const tail = slower ? ", slower than similar customers" : "";
  if (!finite(days)) return slower ? "Pays slower than similar customers." : null;
  return (days <= 0 ? "Usually pays on time" : `Usually pays ${plural(Math.round(days), "day")} late`) + `${tail}.`;
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
      overdue: a.open !== null && overdue > 0 ? `${rs(Math.min(overdue, a.open))} overdue` : null,
      habit: habitText(a.usual, a.slower),
      slower: null,
      lost: [...(left.length ? [lostText(left, true)] : []), ...(partly.length ? [lostText(partly, false)] : [])],
      stakePaise: stake,
      bar: { stake: 0, overdue: stake > 0 ? Math.min(overdue, stake) / stake : 0 },
      overduePaise: Math.min(overdue, stake),
      owedBills: (v.owed?.items ?? []).filter((o) => o.buyer === buyer && finite(o.amount?.paise) && finite(o.interest?.paise))
        .map((o) => ({ principalPaise: o.amount.paise, daysLate: o.days_late, interestPaise: o.interest.paise }))
        .sort((x, y) => y.daysLate - x.daysLate),
      interestPaise: (v.owed?.items ?? []).filter((o) => o.buyer === buyer && finite(o.interest?.paise))
        .reduce((x, o) => x + o.interest.paise, 0),
      cells: {
        outstanding: rs(stake),
        overdue: overdue > 0 ? rs(Math.min(overdue, stake)) : "—",
        pays: finite(a.usual) ? (a.usual <= 0 ? "On time" : `${plural(Math.round(a.usual), "day")} late`) : "—",
        slow: a.slower ? "Slow payer" : null,
        neverPaid: left.length || partly.length ? rs([...left, ...partly].reduce((x, b) => x + b.paise, 0)) : "—",
        neverPaidDetail: left.length || partly.length
          ? [...(left.length ? [lostText(left, true)] : []), ...(partly.length ? [lostText(partly, false)] : [])].join(" ")
          : null,
      },
    });
  }
  rows.sort((x, y) => y.stakePaise - x.stakePaise || x.buyer.localeCompare(y.buyer));
  const max = rows[0]?.stakePaise ?? 0;
  for (const r of rows) r.bar.stake = max > 0 ? r.stakePaise / max : 0;
  return rows;
}

function owesView(v: OwnerView): OwesView {
  const all = buyerRows(v);
  if (!all.length) {
    return { rows: [], more: [], moreLabel: null, empty: "No customer owes you money right now.", totalOverduePaise: 0, overdueCustomers: 0 };
  }
  const rows = all.slice(0, BUYERS_SHOWN), more = all.slice(BUYERS_SHOWN);
  const moreLabel = more.length
    ? `${plural(more.length, "more customer")} with ${rs(more.reduce((s, r) => s + r.stakePaise, 0))} outstanding`
    : null;
  const late = all.filter((r) => r.overduePaise > 0);
  return { rows, more, moreLabel, empty: null, totalOverduePaise: late.reduce((x, r) => x + r.overduePaise, 0),
    overdueCustomers: late.length };
}

// ---------------------------------------------------------------- balance chart

export interface ChartPoint { date: string; median: number; lo: number; hi: number; tip: string }
export interface ChartLabel { date: string; paise: number; text: string }

/** A sharp fall in the typical balance from one day to the next marks a big payment going out. */
export const BIG_PAYMENTS = { share: 0.2, max: 4, text: "Big payments out" };

function bigPayments(pts: ChartPoint[], today: number | null): ChartLabel[] {
  const base = today ?? pts[0]?.median;
  if (!finite(base) || base <= 0) return [];
  const falls: ChartLabel[] = [];
  for (let i = 1; i < pts.length; i++) {
    if (pts[i - 1].median - pts[i].median >= BIG_PAYMENTS.share * base) {
      falls.push({ date: pts[i].date, paise: pts[i - 1].median, text: BIG_PAYMENTS.text });
    }
  }
  return falls.sort((a, b) => b.paise - a.paise).slice(0, BIG_PAYMENTS.max).sort((a, b) => a.date.localeCompare(b.date));
}
export interface BalanceChart {
  near: ChartPoint[];
  far: ChartPoint[];
  /** The baseline's middle estimate, behind a what-if; null on the baseline itself. */
  reference: ChartPoint[] | null;
  cushionPaise: number | null;
  cushionLabel: string | null;
  labels: ChartLabel[];
  /** Sharp falls in the typical balance (BIG_PAYMENTS). */
  events: ChartLabel[];
  legend: { kind: "line" | "band" | "rule" | "ref" | "event"; text: string }[];
  caption: string;
}

function point(date: string, median: number, lo: number, hi: number): ChartPoint {
  const tip = lo === hi ? `${shortDate(date)}\nToday ${rs(median)}`
    : `${shortDate(date)}\nTypical balance ${rs(median)}\nLikely range ${rs(lo)} to ${rs(hi)}`;
  return { date, median, lo, hi, tip };
}

function usableDays(fan: FanDay[] | undefined): FanDay[] {
  return (fan ?? []).filter((d) =>
    finite(d.median_paise) && finite(d.lo50_paise) && finite(d.hi50_paise) && typeof d.date === "string");
}

/** A chart from one set of daily ranges (the baseline's or a what-if's). */
function chartFrom(v: OwnerView, days: FanDay[], reference: ChartPoint[] | null = null): BalanceChart | null {
  if (days.length < 2) return null;
  const pts = days.map((d) => point(d.date, d.median_paise, d.lo50_paise, d.hi50_paise));
  const today = v.headline?.balance_today?.paise;
  if (finite(today) && v.headline.balance_date) pts.unshift(point(v.headline.balance_date, today, today, today));
  const cut = days.findIndex((d) => d.day >= STATUS_RULES.nearDay);
  const split = cut < 0 ? pts.length : pts.findIndex((p) => p.date === days[cut].date) + 1;
  const near = pts.slice(0, split), far = split < pts.length ? pts.slice(split - 1) : [];
  const t = v.fan?.threshold;
  const end = pts[pts.length - 1];
  const events = bigPayments(pts, finite(today) ? today : null);
  return {
    near, far, reference, events,
    cushionPaise: finite(t?.paise) ? t.paise : null,
    cushionLabel: finite(t?.paise) ? (t.paise > 0 ? `Your cushion, ${t.text}` : "Zero balance") : null,
    labels: [
      { date: end.date, paise: end.median, text: "Middle estimate" },
      { date: end.date, paise: (end.median + end.hi) / 2, text: "Half of estimates" },
    ],
    legend: [
      { kind: "line", text: "Typical outcome" },
      { kind: "band", text: "Likely range (half of runs)" },
      ...(reference ? [{ kind: "ref" as const, text: "As things are" }] : []),
      ...(events.length ? [{ kind: "event" as const, text: BIG_PAYMENTS.text }] : []),
      ...(finite(t?.paise) ? [{ kind: "rule" as const, text: t.paise > 0 ? `Your cushion, ${t.text}` : "Zero balance" }] : []),
    ],
    caption: far.length
      ? `After ${STATUS_RULES.nearDay} days the runs spread out and the estimate is less reliable, so that part is faded.`
      : "",
  };
}

function chartView(v: OwnerView): BalanceChart | null {
  return chartFrom(v, usableDays(v.fan?.days));
}

// ---------------------------------------------------------------- simulation explorer

/** One day of a simulated future, as the slider reads it out. */
export interface SimDay {
  day: number;
  date: string;
  dateLabel: string;
  middle: string;
  range: string;
  chance: string;
  /** The raw chance behind `chance` (null when unknown). */
  chanceValue: number | null;
  planChance: string | null;
  /** Percentile positions 0-1 on a scale fixed per scenario. */
  bar: { lo90: number; lo50: number; median: number; hi50: number; hi90: number; cushion: number | null };
  wide: string;
  wideLo: string;
  wideHi: string;
}

export interface SimScenario {
  id: string; name: string; description: string; chart: BalanceChart; days: SimDay[];
  /** The status this scenario's own chances give (same rules as the headline). */
  status: { key: StatusKey; label: string; serious: boolean } | null;
}

export interface Simulation {
  scenarios: SimScenario[];
  defaultDay: number;
  labels: { day: string; play: string; pause: string; scenarios: string; barTitle: string; barCushion: string };
  /** "below your Rs 10 lakh cushion" */
  cushionPhrase: string;
}

/** What a what-if changes, in owner words and at the magnitude the payload ran. */
export function scenarioDescription(w: WhatIf): string | null {
  if (finite(w.amount?.paise)) {
    return `One unexpected bill of ${w.amount.text}${w.on_date ? ` on ${shortDate(w.on_date)}` : ""}.`;
  }
  if (finite(w.days) && finite(w.days_from)) return `Suppliers want paying within ${plural(w.days, "day")} instead of ${w.days_from}.`;
  const rate = PRESET_HINTS[w.preset]?.rate;
  if (rate === "buyers" && finite(w.share?.value)) {
    const extra = extraTimeToPay(w.share.value);
    if (extra === null) return null;
    return longerPct(extra) === 100 ? "Customers take twice as long to pay as they do now."
      : `Customers take ${longerText(extra)} to pay than they do now.`;
  }
  if (rate === "orders" && w.share) return `Orders fall by ${w.share.text}.`;
  if (finite(w.days)) return w.description || null;
  return null;
}

function dayLabel(date: string, day: number): string {
  return `${shortDate(date)}, ${day === 1 ? "tomorrow" : `${day} days from now`}`;
}

function simDays(days: FanDay[], risk: (number | null | undefined)[], plan: (number | null | undefined)[] | null,
  cushion: number | null): SimDay[] {
  const wideOk = days.every((d) => finite(d.lo90_paise) && finite(d.hi90_paise));
  const lo = Math.min(...days.map((d) => (wideOk ? d.lo90_paise : d.lo50_paise)), cushion ?? Infinity);
  const hi = Math.max(...days.map((d) => (wideOk ? d.hi90_paise : d.hi50_paise)), cushion ?? -Infinity);
  const at = (x: number) => (hi > lo ? (x - lo) / (hi - lo) : 0.5);
  return days.map((d, i) => {
    const p = risk[i];
    const a = plan?.[i];
    const l90 = wideOk ? d.lo90_paise : d.lo50_paise, h90 = wideOk ? d.hi90_paise : d.hi50_paise;
    return {
      day: d.day, date: d.date, dateLabel: dayLabel(d.date, d.day),
      middle: rs(d.median_paise),
      range: rsRange(d.lo50_paise, d.hi50_paise),
      chance: finite(p) ? chanceEstimate(p) : "—",
      chanceValue: finite(p) ? p : null,
      planChance: finite(a) ? `${chanceEstimate(a)} with the plan` : null,
      bar: { lo90: at(l90), lo50: at(d.lo50_paise), median: at(d.median_paise), hi50: at(d.hi50_paise), hi90: at(h90),
        cushion: cushion === null ? null : at(cushion) },
      wide: wideOk ? `90% of runs land between ${rs(l90)} and ${rs(h90)}; the typical balance is ${rs(d.median_paise)}.`
        : `The typical balance is ${rs(d.median_paise)}.`,
      wideLo: rs(l90), wideHi: rs(h90),
    };
  });
}

function scenarioStatus(risk: (number | null | undefined)[], target: number): SimScenario["status"] {
  const first = risk.findIndex((p) => finite(p) && p >= STATUS_RULES.likely);
  const s = statusOf({ p30: risk[STATUS_RULES.nearDay - 1], p90: risk[STATUS_RULES.farDay - 1], target,
    runwayDay: first < 0 ? null : first + 1 });
  return s && { key: s.key, label: STATUS_RULES.states[s.key].label, serious: STATUS_RULES.states[s.key].serious };
}

function simulationView(v: OwnerView, target: number): Simulation | null {
  const baseDays = usableDays(v.fan?.days);
  const base = chartFrom(v, baseDays);
  if (!base) return null;
  const r = v.risk_by_day ?? [];
  const scenarios: SimScenario[] = [{
    id: "base", name: "As things are", description: "No changes: your customers, suppliers and bills as they are now.",
    chart: base, days: simDays(baseDays, r.map((x) => x?.before), r.map((x) => x?.after), base.cushionPaise),
    status: scenarioStatus(r.map((x) => x?.before), target),
  }];
  const ref = [...base.near, ...base.far.slice(1)];
  for (const w of v.what_ifs ?? []) {
    const days = usableDays(w.fan);
    const description = scenarioDescription(w);
    if (!horizonCovered(w) || days.length !== baseDays.length || !description) continue;
    const chart = chartFrom(v, days, ref);
    if (!chart) continue;
    scenarios.push({ id: w.preset, name: w.name, description, chart,
      days: simDays(days, w.risk_by_day ?? [], null, base.cushionPaise), status: scenarioStatus(w.risk_by_day ?? [], target) });
  }
  const n = finite(v.meta?.n_paths) && v.meta.n_paths > 0 ? Math.round(v.meta.n_paths).toLocaleString("en-IN") : null;
  const t = v.fan?.threshold;
  return {
    scenarios,
    defaultDay: Math.min(STATUS_RULES.nearDay, baseDays.length),
    labels: {
      day: "Move along the next 90 days", play: "Play", pause: "Pause", scenarios: "What if",
      barTitle: `Where ${n ? `${n} runs` : "the runs"} land on this day`, barCushion: "cushion",
    },
    cushionPhrase: finite(t?.paise) ? (t.paise > 0 ? `below your ${t.text} cushion` : "below zero") : "below your cushion",
  };
}

/** The parts of the dashboard that follow the simulation controls. */
export interface DashboardAt {
  kpis: Kpi[];
  /** Notes for panels computed for the baseline only, shown under a what-if. */
  planNote: string | null;
  readyNote: string | null;
  obligationsNote: string | null;
}

/** KPI tiles for a scenario and day, from the same runs as the chart; plan and breaking points are baseline-only. */
export function dashboardAt(screen: OwnerScreen, scenarioId: string, day: number): DashboardAt {
  const sim = screen.simulation;
  const sc = sim?.scenarios.find((x) => x.id === scenarioId);
  if (!sim || !sc || !sc.days.length) return { kpis: screen.kpis, planNote: null, readyNote: null, obligationsNote: null };
  const d = sc.days[Math.min(Math.max(Math.round(day), 1), sc.days.length) - 1];
  const tag = sc.id === "base" ? null : `If: ${sc.name.toLowerCase()}`;
  // A chosen day that reads "under 2%" carries no serious badge or red: it would contradict the figure. The
  // scenario's verdict is about the whole window, so the tile says where the chance rises to instead.
  const low = d.chanceValue !== null && d.chanceValue < CHANCE_FORMAT.small;
  const ahead = [STATUS_RULES.nearDay, STATUS_RULES.farDay].map((n) => sc.days[Math.min(n, sc.days.length) - 1])
    .find((x) => x.day > d.day && x.chanceValue !== null && x.chanceValue >= CHANCE_FORMAT.small);
  const quiet = low && !!sc.status?.serious;
  const kpis = screen.kpis.map((k): Kpi => {
    if (k.id === "risk") {
      return { ...k, label: `Chance of a dip · by ${shortDate(d.date)}`, value: d.chance, note: sim.cushionPhrase,
        serious: quiet ? false : sc.status?.serious ?? k.serious,
        badge: quiet ? null : sc.status ? { key: sc.status.key, label: sc.status.label } : k.badge, tag,
        withPlan: quiet || sc.id !== "base" ? null : k.withPlan,
        later: low && ahead ? `Rises to ${ahead.chance} by ${shortDate(ahead.date)}` : null };
    }
    if (k.id === "likely") return { ...k, label: `Likely balance · ${shortDate(d.date)}`, value: d.range, note: `typical ${d.middle}`, tag };
    return k;
  });
  if (!kpis.some((k) => k.id === "risk") && sc.status) {
    kpis.unshift({ id: "risk", label: `Chance of a dip · by ${shortDate(d.date)}`, value: d.chance, note: sim.cushionPhrase,
      serious: sc.status.serious, badge: { key: sc.status.key, label: sc.status.label }, tag });
  }
  const other = sc.id === "base" ? null : `Worked out for things as they are; not yet re-checked for "${sc.name}".`;
  return { kpis, planNote: other, readyNote: other,
    obligationsNote: sc.id === "base" ? null : `How tight each day is, is worked out for things as they are; not yet for "${sc.name}".` };
}

// ---------------------------------------------------------------- be ready for

/** What the payload does not say about a preset, keyed by its name. */
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

export interface ReadyItem { value: string; label: string; text: string }

/** The breaking point in owner terms, or null when it has none or its kind is unknown. */
export function readyItem(w: WhatIf): ReadyItem | null {
  if (!finite(w.breaking_magnitude)) return null;
  if (finite(w.breaking_amount?.paise)) {
    const v = w.breaking_amount.text;
    return { value: v, label: "one unexpected bill this big or more", text: `an unexpected bill of ${v} or more arrives` };
  }
  if (finite(w.breaking_days) && finite(w.days_from)) {
    return { value: plural(w.breaking_days, "day"), label: `suppliers want paying within this, not ${w.days_from}`,
      text: `suppliers want paying within ${plural(w.breaking_days, "day")} instead of ${w.days_from}` };
  }
  if (finite(w.breaking_days)) {
    return { value: plural(w.breaking_days, "day"), label: `${w.name}, by this much or more`,
      text: `${w.name} by ${plural(w.breaking_days, "day")} or more` };
  }
  const rate = PRESET_HINTS[w.preset]?.rate;
  if (rate === "buyers") {
    const extra = extraTimeToPay(w.breaking_magnitude);
    if (extra === null) return null;
    return { value: `${longerPct(extra)}% longer`, label: "customers take this much longer to pay",
      text: `your customers take ${longerText(extra)} to pay than they do now` };
  }
  if (rate === "orders" && w.breaking_share) {
    return { value: `${w.breaking_share.text} fewer`, label: "orders fall this much",
      text: `orders fall by ${w.breaking_share.text} or more` };
  }
  return null;
}

export function readyText(w: WhatIf): string | null {
  return readyItem(w)?.text ?? null;
}

export interface ReadyView { lead: string; items: string[]; cards: ReadyItem[] }

function readyView(v: OwnerView): ReadyView | null {
  const items = (v.what_ifs ?? [])
    .filter(horizonCovered)
    .map((w) => ({ w, item: readyItem(w) }))
    .filter((x): x is { w: WhatIf; item: ReadyItem } => x.item !== null)
    .sort((a, b) => (b.w.risk_90?.value ?? 0) - (a.w.risk_90?.value ?? 0))
    .slice(0, READY_SHOWN)
    .map((x) => x.item);
  if (!items.length) return null;
  return { lead: "Any one of these would make a dip below your cushion within three months more likely than not.",
    items: items.map((i) => i.text), cards: items };
}

// ---------------------------------------------------------------- obligations

/** How each kind of obligation can bend, and what to say about it. */
export const OBLIGATION_RULES: Record<string, { label: string; noun: string; flex: "law" | "keep" | "ask"; tip: string }> = {
  gst: { label: "GST", noun: "GST", flex: "law", tip: "Set by law. Paying late adds interest and late fees, so keep this one on time." },
  tds: { label: "TDS deposit", noun: "TDS deposit", flex: "law", tip: "Set by law. Paying late adds interest, so keep this one on time." },
  pf: { label: "PF", noun: "PF", flex: "law", tip: "Set by law. Paying late adds interest and damages, so keep this one on time." },
  esi: { label: "ESI", noun: "ESI", flex: "law", tip: "Set by law. Paying late adds interest, so keep this one on time." },
  salary: { label: "Salaries", noun: "salaries", flex: "keep", tip: "Best kept on time: your staff depend on it." },
  rent: { label: "Rent", noun: "rent", flex: "ask", tip: "You can ask your landlord to move the date; ask before it is due." },
  emi: { label: "Loan EMI", noun: "loan EMI", flex: "ask", tip: "Ask your bank before the date; a missed EMI hurts your credit record." },
  professional_fee: { label: "Professional fees", noun: "professional fees", flex: "ask", tip: "Usually easy to move by a few days if you ask." },
};
export const FLEX_LABELS = { law: "Fixed by law", keep: "Keep on time", ask: "Can ask to move" } as const;

/** Rows in the table by default; the rest go behind "show all". */
export const OBLIGATIONS_WINDOW_DAYS = 30;
/** Supplier payment terms the MSMED Act allows at most (s.15, with a written agreement; verified). */
export const MSMED_MAX_DAYS = 45;

export interface ObligationRow {
  date: string;
  dateLabel: string;
  label: string;
  amount: string;
  flex: "law" | "keep" | "ask";
  flexLabel: string;
  tip: string;
  tight: boolean;
  dayChance: string;
}

export interface Suggestion { title: string; detail: string; serious: boolean }

export interface ObligationsView {
  summary: string;
  lawShare: string | null;
  suppliers: string | null;
  rows: ObligationRow[];
  later: ObligationRow[];
  laterLabel: string | null;
  columns: { date: string; payment: string; amount: string; flex: string; day: string };
  dayNote: string;
  suggestions: Suggestion[];
}

/** The amount alone, for sentences: the middle figure, without "usually" or a range. */
function obligationAmountShort(o: ObligationItem): string {
  return finite(o.amount_paise) ? rs(o.amount_paise) : "";
}

function obligationAmount(o: ObligationItem): string {
  if (!finite(o.amount_paise)) return "—";
  if (finite(o.amount_lo_paise) && finite(o.amount_hi_paise) && o.amount_lo_paise !== o.amount_hi_paise) {
    return `about ${rs(o.amount_paise)} (${rsRange(o.amount_lo_paise, o.amount_hi_paise)})`;
  }
  return (o.amount_owner === "obligations_detector" && o.confidence < 0.95 ? "usually " : "") + rs(o.amount_paise);
}

/** The obligations payload belongs to this owner view only when tenant, run and as-of all match. */
export function obligationsMatch(v: OwnerView, ob: ObligationsPayload | null | undefined): ob is ObligationsPayload {
  return !!ob && ob.schema_version === "obligations-v1" && ob.tenant_id === v.meta?.tenant_id && ob.run_id === v.meta?.run_id
    && ob.as_of === v.meta?.as_of && Array.isArray(ob.items);
}

export function obligationsView(v: OwnerView, ob: ObligationsPayload | null | undefined, target: number,
  owes: OwesView): ObligationsView | null {
  if (!obligationsMatch(v, ob)) return null;
  const items = ob.items.filter((o) => OBLIGATION_RULES[o.kind] && finite(o.day) && o.day >= 1).sort((a, b) => a.day - b.day);
  if (!items.length) return null;
  const toRow = (o: ObligationItem): ObligationRow => {
    const rule = OBLIGATION_RULES[o.kind];
    const p = o.p_below_cushion_that_day;
    return {
      date: o.due_date, dateLabel: shortDate(o.due_date), label: rule.label, amount: obligationAmount(o),
      flex: rule.flex, flexLabel: FLEX_LABELS[rule.flex], tip: rule.tip,
      tight: finite(p) && p > target, dayChance: finite(p) ? chanceEstimate(p) : "—",
    };
  };
  const soon = items.filter((o) => o.day <= OBLIGATIONS_WINDOW_DAYS);
  const later = items.filter((o) => o.day > OBLIGATIONS_WINDOW_DAYS);
  const sum = (xs: ObligationItem[]) => xs.reduce((a, o) => a + (finite(o.amount_paise) ? o.amount_paise : 0), 0);
  const lawSoon = soon.filter((o) => OBLIGATION_RULES[o.kind].flex === "law");
  const sp = ob.supplier_payments_30d;

  const t = v.fan?.threshold;
  const cushion = finite(t?.paise) && t.paise > 0 ? `your ${t.text} cushion` : "your cushion";
  const suggestions: Suggestion[] = [];
  const tight = items.filter((o) => finite(o.p_below_cushion_that_day) && o.p_below_cushion_that_day > target);
  const tightest = [...tight].sort((a, b) => b.p_below_cushion_that_day - a.p_below_cushion_that_day)[0];
  const topCustomer = owes.rows.filter((r) => r.overduePaise > 0).sort((a, b) => b.overduePaise - a.overduePaise)[0];
  if (tightest) {
    const r = OBLIGATION_RULES[tightest.kind];
    suggestions.push({
      title: `Your tightest day is ${shortDate(tightest.due_date)}: ${r.noun} of ${obligationAmountShort(tightest)}`,
      detail: `${chanceEstimate(tightest.p_below_cushion_that_day)} of runs end that day below ${cushion}.` +
        (topCustomer ? ` Collecting overdue money before then helps most; ${topCustomer.buyer} owes the most (${rs(topCustomer.overduePaise)} overdue).` : ""),
      serious: true,
    });
  }
  const law = tight.filter((o) => OBLIGATION_RULES[o.kind].flex === "law" && finite(o.amount_paise))
    .sort((a, b) => (b.amount_paise ?? 0) - (a.amount_paise ?? 0) || a.day - b.day);
  for (const o of law.slice(0, 1)) {
    suggestions.push({
      title: `Keep ${obligationAmountShort(o)} aside for ${OBLIGATION_RULES[o.kind].noun} by ${shortDate(o.due_date)}`,
      detail: `${OBLIGATION_RULES[o.kind].tip} ${chanceEstimate(o.p_below_cushion_that_day)} of runs end that day below ${cushion}.`,
      serious: false,
    });
  }
  // Per kind, its tightest due date; then the two biggest amounts.
  const byKind = new Map<string, ObligationItem>();
  for (const o of tight.filter((x) => OBLIGATION_RULES[x.kind].flex === "ask" && finite(x.amount_paise))) {
    const cur = byKind.get(o.kind);
    if (!cur || o.p_below_cushion_that_day > cur.p_below_cushion_that_day) byKind.set(o.kind, o);
  }
  const ask = [...byKind.values()].sort((a, b) => (b.amount_paise ?? 0) - (a.amount_paise ?? 0)).slice(0, 2);
  for (const o of ask) {
    suggestions.push({
      title: `You could ask to move the ${OBLIGATION_RULES[o.kind].noun} of ${obligationAmountShort(o)} due ${shortDate(o.due_date)}`,
      detail: `${chanceEstimate(o.p_below_cushion_that_day)} of runs end that day below ${cushion}. ${OBLIGATION_RULES[o.kind].tip} ` +
        "The forecast has not tested this move.",
      serious: false,
    });
  }
  if (sp && finite(sp.median_paise)) {
    suggestions.push({
      title: `Supplier payments: about ${rsRange(sp.lo_paise, sp.hi_paise)} in the next ${sp.days} days`,
      detail: `If you need more time, registered micro and small suppliers must be paid within ${MSMED_MAX_DAYS} days under the ` +
        "MSMED Act. Paying them later costs interest and can cost you this year's tax deduction.",
      serious: false,
    });
  }
  const owedInterest = v.owed?.total_interest;
  if (finite(owedInterest?.paise) && owedInterest.paise > 0) {
    suggestions.push({
      title: `Customers owe you ${owedInterest.text} in late-payment interest`,
      detail: "The MSMED Act gives you this on bills paid late. It is useful in collection calls; you can also claim it on the " +
        "MSME Samadhaan portal, though claiming can strain the relationship.",
      serious: false,
    });
  }

  return {
    summary: `${rs(sum(soon))} due in the next ${OBLIGATIONS_WINDOW_DAYS} days across ${plural(soon.length, "payment")}`,
    lawShare: lawSoon.length ? `${rs(sum(lawSoon))} of it is fixed by law (${[...new Set(lawSoon.map((o) => OBLIGATION_RULES[o.kind].label))].join(", ")}).` : null,
    suppliers: sp && finite(sp.median_paise) ? `Plus about ${rsRange(sp.lo_paise, sp.hi_paise)} to suppliers for stock.` : null,
    rows: soon.map(toRow),
    later: later.map(toRow),
    laterLabel: later.length ? `${plural(later.length, "more payment")} in the following 60 days (${rs(sum(later))})` : null,
    columns: { date: "Due", payment: "Payment", amount: "Amount", flex: "Can it move?", day: "Day ends below cushion" },
    dayNote: "Day ends below cushion: the share of runs whose balance is below your cushion at the end of that day.",
    suggestions,
  };
}

// ---------------------------------------------------------------- the page

export interface Tile { label: string; value: string; note: string | null }

export interface StatusView {
  key: StatusKey;
  label: string;
  serious: boolean;
  /** The one big figure and what it means. */
  hero: { value: string; caption: string };
  /** One calm line on what to do about it. */
  advice: string;
  tiles: Tile[];
  /** The whole status as one sentence (for screen readers). */
  headline: string;
  balance: string | null;
  range30: string | null;
  estimateNote: string;
}

export const SECTIONS = {
  app: "Matsya",
  page: "Cash outlook",
  evidence: "How good are these estimates?",
  todo: "This week",
  obligations: "Your obligations",
  obligationsDo: "What you can do",
  table: { customer: "Customer", outstanding: "Outstanding", overdue: "Overdue", pays: "Usually pays", neverPaid: "May never be paid" },
  owesNeverNote: "May never be paid: bills your forecast already leaves out, or counts on only in part. Hover a figure for details.",
  owes: "Who owes you",
  owesNote: "Customers with money outstanding, biggest amounts first.",
  owesBarKey: "Bar length is the amount outstanding; the darker part is overdue.",
  outlook: "Your balance over the next 90 days",
  ready: "Be ready for",
};

export interface Kpi {
  id: string;
  label: string;
  value: string;
  note: string | null;
  serious: boolean;
  badge: { key: StatusKey; label: string } | null;
  /** The status the plan would give, when it is better than the current one (fresh-path checked). */
  withPlan?: { key: StatusKey; label: string; text: string } | null;
  /** Which what-if the figure is under, when one is on. */
  tag?: string | null;
  /** Where the chance goes later, shown when the chosen day is still low. */
  later?: string | null;
}

export interface OwnerScreen {
  title: string;
  /** ISO as-of date of the figures (for drafts). */
  asOfDate: string | null;
  kpis: Kpi[];
  sections: typeof SECTIONS;
  status: StatusView | null;
  plan: PlanView;
  owes: OwesView;
  chart: BalanceChart | null;
  simulation: Simulation | null;
  obligations: ObligationsView | null;
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

/** "Rs 27.7–47.2 lakh" when both ends share a unit, else "Rs 45,000 to Rs 1.2 lakh". */
export function rsRange(lo: number, hi: number): string {
  const unit = (p: number) => /^Rs ([\d.]+) (lakh|crore)$/.exec(rs(Math.abs(p)));
  const a = unit(lo), b = unit(hi);
  if (a && b && a[2] === b[2]) {
    const n = (m: RegExpExecArray, p: number) => `${p < 0 ? "−" : ""}${m[1]}`;
    return `Rs ${n(a, lo)}${lo < 0 || hi < 0 ? " to " : "–"}${n(b, hi)} ${a[2]}`;
  }
  return `${rs(lo)} to ${rs(hi)}`;
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
  const near = s.reason === "near";
  const hero = {
    value: chanceEstimate(near ? p30 : Math.max(p90, p30)),
    caption: `chance your balance dips ${below} ${near ? "in the next 30 days" : "within the next three months"}`,
  };
  const advice = {
    act_now: "A dip is more likely than not. Start on the to-dos below this week.",
    at_risk: near ? "Worth acting this week. The to-dos below show how."
      : "The next 30 days look manageable, but it is worth acting early. The to-dos below show how.",
    watch: "The next 30 days look fine. The risk builds after that.",
    ok: "Nothing urgent right now.",
  }[s.key];
  const d30 = v.fan?.days?.find((d) => d.day === STATUS_RULES.nearDay);
  const range30 = d30 && finite(d30.lo50_paise) && finite(d30.hi50_paise)
    ? `Likely balance on ${shortDate(d30.date)}: ${rs(d30.lo50_paise)} to ${rs(d30.hi50_paise)} (the middle half of our estimates).`
    : null;
  const bt = v.headline?.balance_today;
  const tiles: Tile[] = [];
  if (finite(bt?.paise)) tiles.push({ label: "In the bank today", value: bt.text, note: null });
  if (d30 && finite(d30.lo50_paise) && finite(d30.hi50_paise)) {
    tiles.push({ label: `Likely balance on ${shortDate(d30.date)}`, value: rsRange(d30.lo50_paise, d30.hi50_paise),
      note: "the middle half of our estimates" });
  }
  return {
    key: s.key, label: STATUS_RULES.states[s.key].label, serious: STATUS_RULES.states[s.key].serious,
    hero, advice, tiles, headline,
    balance: finite(bt?.paise) ? `${bt.text} in the bank today` : null,
    range30,
    estimateNote: "These are estimates from your bank and invoice records, not guarantees.",
  };
}

/** The status after the plan (fresh-path chances); null unless it improves on now. */
export function statusWithPlan(v: OwnerView, s: StatusResult | null, target: number): Kpi["withPlan"] {
  const r = v.risk_by_day ?? [];
  const after = statusOf({ p30: r[STATUS_RULES.nearDay - 1]?.after, p90: r[STATUS_RULES.farDay - 1]?.after, target });
  const order: StatusKey[] = ["ok", "watch", "at_risk", "act_now"];
  if (!s || !after || order.indexOf(after.key) >= order.indexOf(s.key)) return null;
  const label = STATUS_RULES.states[after.key].label;
  return { key: after.key, label, text: `${label} with the plan` };
}

function kpis(v: OwnerView, st: StatusView | null, s: StatusResult | null, owes: OwesView, target: number): Kpi[] {
  const out: Kpi[] = [];
  const t = v.fan?.threshold;
  if (st && s) {
    out.push({
      withPlan: statusWithPlan(v, s, target),
      id: "risk", label: s.reason === "near" ? "Risk · next 30 days" : "Risk · next 3 months", value: st.hero.value,
      note: finite(t?.paise) ? (t.paise > 0 ? `of dipping below your ${t.text} cushion` : "of dipping below zero") : "of dipping below your cushion",
      serious: st.serious, badge: { key: st.key, label: st.label },
    });
  }
  const bt = v.headline?.balance_today;
  if (finite(bt?.paise)) {
    out.push({ id: "bank", label: "In the bank today", value: bt.text,
      note: v.headline.balance_date ? `as of ${shortDate(v.headline.balance_date)}` : null, serious: false, badge: null });
  }
  const d30 = v.fan?.days?.find((d) => d.day === STATUS_RULES.nearDay);
  if (d30 && finite(d30.lo50_paise) && finite(d30.hi50_paise)) {
    out.push({ id: "likely", label: `Likely balance · ${shortDate(d30.date)}`, value: rsRange(d30.lo50_paise, d30.hi50_paise),
      note: "middle half of our estimates", serious: false, badge: null });
  }
  if (owes.rows.length) {
    out.push({ id: "overdue", label: "Overdue from customers", value: rs(owes.totalOverduePaise),
      note: `across ${plural(owes.overdueCustomers, "customer")}`, serious: false, badge: null });
  }
  return out;
}

export function buildOwnerScreen(v: OwnerView, source: "api" | "demo file" = "api", ob: ObligationsPayload | null = null): OwnerScreen {
  const t = v.plan?.target?.value;
  const target = finite(t) && t > 0 ? t : STATUS_RULES.fallbackTarget;
  const s = statusOf({
    p30: riskAt(v, STATUS_RULES.nearDay), p90: riskAt(v, STATUS_RULES.farDay), target: v.plan?.target?.value,
    runwayDay: v.headline?.runway_day,
  });
  const status = statusView(v, s);
  const owes = owesView(v);
  return {
    title: "Your cash outlook",
    asOfDate: v.meta?.as_of ?? null,
    kpis: kpis(v, status, s, owes, target),
    sections: SECTIONS,
    status,
    plan: planView(v, target, s),
    owes,
    chart: chartView(v),
    simulation: simulationView(v, target),
    obligations: obligationsView(v, ob, target, owes),
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

