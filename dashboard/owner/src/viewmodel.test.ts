import { describe, expect, it } from "vitest";
import demoJson from "../../../eval/demo/owner_view.json";
import obJson from "../../../eval/demo/obligations.json";
import type { ObligationsPayload, OwnerView, PlanAction, WhatIf } from "./types";
import {
  BANNED, BIG_PAYMENTS, BUYERS_SHOWN, MSME_STRETCH_CATCH, STATUS_RULES, STRETCH_PENALTY_PAISE, buildOwnerScreen, buyerRows,
  dashboardAt, OBLIGATION_RULES, OBLIGATIONS_WINDOW_DAYS,
  chanceEstimate, extraTimeToPay, horizonCovered, isLikelyMsmeSupplierFromStretchCost, longerText, readyText,
  roundPct, rsRange, splitSteps, statusOf, supplierLabel,
} from "./viewmodel";

const demo = demoJson as unknown as OwnerView;
const ob = obJson as unknown as ObligationsPayload;
const clone = (): OwnerView => JSON.parse(JSON.stringify(demo));
const T = 0.05;
const L = STATUS_RULES.likely;

describe("statusOf", () => {
  it("needs both chances", () => {
    expect(statusOf({ p30: null, p90: 0.2, target: T })).toBeNull();
    expect(statusOf({ p30: 0.1, p90: undefined, target: T })).toBeNull();
    expect(statusOf({ p30: Number.NaN, p90: 0.2, target: T })).toBeNull();
  });

  it("ok when p90 is at or under the target", () => {
    expect(statusOf({ p30: 0, p90: 0, target: T })?.key).toBe("ok");
    expect(statusOf({ p30: 0.01, p90: T, target: T })?.key).toBe("ok");
  });

  it("watch just above the target at 90 days with 30 days within it", () => {
    expect(statusOf({ p30: T, p90: T + 1e-9, target: T })).toEqual({ key: "watch", reason: "far" });
    expect(statusOf({ p30: 0.01, p90: L - 1e-9, target: T })?.key).toBe("watch");
  });

  it("at_risk when p30 is just over the target", () => {
    expect(statusOf({ p30: T + 1e-9, p90: 0.2, target: T })).toEqual({ key: "at_risk", reason: "near" });
    expect(statusOf({ p30: L - 1e-9, p90: 0.9, target: T })).toEqual({ key: "at_risk", reason: "near" });
  });

  it("at_risk (far) when p90 is exactly on the likely line with 30 days within target", () => {
    expect(statusOf({ p30: 0.01, p90: L, target: T })).toEqual({ key: "at_risk", reason: "far" });
  });

  it("act_now when p30 is exactly on the likely line", () => {
    expect(statusOf({ p30: L, p90: L, target: T })).toEqual({ key: "act_now", reason: "near" });
  });

  it("runway day 30 is act_now, 31 and 90 are at_risk, null changes nothing", () => {
    expect(statusOf({ p30: 0.01, p90: 0.6, target: T, runwayDay: 30 })?.key).toBe("act_now");
    expect(statusOf({ p30: 0.01, p90: 0.6, target: T, runwayDay: 31 })).toEqual({ key: "at_risk", reason: "far" });
    expect(statusOf({ p30: 0.01, p90: 0.04, target: T, runwayDay: 90 })).toEqual({ key: "at_risk", reason: "far" });
    expect(statusOf({ p30: 0.01, p90: 0.04, target: T, runwayDay: null })?.key).toBe("ok");
  });

  it("missing or unusable target falls back to the documented default", () => {
    const f = STATUS_RULES.fallbackTarget;
    for (const target of [undefined, null, 0, -1, Number.NaN]) {
      expect(statusOf({ p30: f, p90: f, target })?.key).toBe("ok");
      expect(statusOf({ p30: f, p90: f + 1e-9, target })?.key).toBe("watch");
      expect(statusOf({ p30: f + 1e-9, p90: 0.2, target })?.key).toBe("at_risk");
    }
  });

  it("treats p90 as at least p30, so the states never contradict", () => {
    expect(statusOf({ p30: 0.2, p90: 0.01, target: T })?.key).toBe("at_risk");
    expect(statusOf({ p30: 0.04, p90: 0.01, target: T })?.key).toBe("ok");
  });

  it("never reports a serious state with no near or far risk behind it", () => {
    for (let a = 0; a <= 1; a += 0.05) {
      for (let b = a; b <= 1; b += 0.05) {
        const s = statusOf({ p30: a, p90: b, target: T, runwayDay: null })!;
        if (STATUS_RULES.states[s.key].serious) expect(a > T || b >= L).toBe(true);
        if (s.key === "act_now") expect(a).toBeGreaterThanOrEqual(L);
      }
    }
  });
});

describe("chance as a percentage", () => {
  it("rounds to 5 from 10% up and to 1 below", () => {
    expect(roundPct(0.196)).toBe(20);
    expect(roundPct(0.337)).toBe(35);
    expect(roundPct(0.073)).toBe(7);
    expect(roundPct(0.001)).toBe(1);
  });

  it("widens low estimates upward into a range", () => {
    expect(chanceEstimate(0.2)).toBe("20–30%");
    expect(chanceEstimate(0.05)).toBe("5–8%");
    expect(chanceEstimate(0.4)).toBe("40–60%");
  });

  it("says under 2% at the bottom and over 95% at the top", () => {
    expect(chanceEstimate(0.001)).toBe("under 2%");
    expect(chanceEstimate(0)).toBe("under 2%");
    expect(chanceEstimate(0.02)).toBe("2–3%");
    expect(chanceEstimate(0.96)).toBe("over 95%");
  });

  it("gives one figure from the likely line up", () => {
    expect(chanceEstimate(0.5)).toBe("about 50%");
    expect(chanceEstimate(0.7)).toBe("about 70%");
  });

  it("rejects values that are not probabilities", () => {
    for (const p of [Number.NaN, -0.1, 1.2]) expect(chanceEstimate(p)).toBe("");
  });
});

describe("payment slowdown as time", () => {
  it("derives the extra time from the multiplier", () => {
    expect(extraTimeToPay(0.8671875)).toBeCloseTo(1 / 0.8671875 - 1, 12);
    expect(longerText(extraTimeToPay(0.8671875)!)).toBe("about 15% longer");
    expect(longerText(extraTimeToPay(0.5)!)).toBe("about 100% longer");
    expect(longerText(extraTimeToPay(0.95)!)).toBe("about 5% longer");
  });

  it("rejects multipliers that are not slowdowns", () => {
    for (const m of [0, 1, 1.2, -0.5, null, undefined, Number.NaN]) expect(extraTimeToPay(m)).toBeNull();
  });
});

const act = (o: Partial<PlanAction>): PlanAction =>
  ({ rank: 1, kind: "chase", target_id: null, description: "x", cost: { paise: 0, text: "Rs 0" }, ...o });

describe("plan rules", () => {
  it("flags an MSME supplier only above the penalty (demo heuristic)", () => {
    const s = (paise: number) => act({ kind: "supplier_stretch", cost: { paise, text: "" } });
    expect(isLikelyMsmeSupplierFromStretchCost(s(STRETCH_PENALTY_PAISE))).toBe(false);
    expect(isLikelyMsmeSupplierFromStretchCost(s(STRETCH_PENALTY_PAISE + 1))).toBe(true);
    expect(isLikelyMsmeSupplierFromStretchCost(act({ kind: "overdraft", cost: { paise: 10 ** 9, text: "" } }))).toBe(false);
  });

  it("labels suppliers from the payload id only", () => {
    expect(supplierLabel("S1:+30d")).toBe("Supplier S1");
    expect(supplierLabel("S2:+7d", "micro")).toBe("Supplier S2 (micro)");
    expect(supplierLabel(null)).toBe("your supplier");
  });

  it("splits at the first step that reaches the target", () => {
    const steps = [0.3, 0.04, 0.03].map((v, i) => act({ rank: i + 1, risk_after: { value: v, text: "" } }));
    const r = splitSteps(steps, T);
    expect(r.core.map((a) => a.rank)).toEqual([1, 2]);
    expect(r.optional.map((a) => a.rank)).toEqual([3]);
  });

  it("keeps every step when the target is never reached or figures are missing", () => {
    const never = [0.3, 0.2].map((v, i) => act({ rank: i + 1, risk_after: { value: v, text: "" } }));
    expect(splitSteps(never, T).optional).toEqual([]);
    expect(splitSteps([act({ rank: 1 }), act({ rank: 2 })], T).core).toHaveLength(2);
  });
});

const wi = (o: Partial<WhatIf>): WhatIf =>
  ({ preset: "x", name: "X", description: "", risk_90: { value: 0.5, text: "" }, change_90: 0, breaking_magnitude: 1,
    risk_by_day: new Array(90).fill(0), ...o });

describe("be ready for", () => {
  it("hides a what-if whose horizon was not computed", () => {
    expect(horizonCovered(wi({ preset: "reduced_sales" }))).toBe(false);
    expect(horizonCovered(wi({ preset: "reduced_sales", risk_by_day: new Array(180).fill(0) }))).toBe(true);
    expect(horizonCovered(wi({ preset: "unexpected_expense" }))).toBe(true);
    expect(horizonCovered(wi({ risk_by_day: undefined, fan: undefined }))).toBe(false);
  });

  it("words each breaking point in rupees, days or time to pay", () => {
    expect(readyText(wi({ breaking_amount: { paise: 5856537000, text: "Rs 5.9 lakh" } })))
      .toBe("an unexpected bill of Rs 5.9 lakh or more arrives");
    expect(readyText(wi({ breaking_days: 26, days_from: 30 }))).toBe("suppliers want paying within 26 days instead of 30");
    expect(readyText(wi({ preset: "delayed_customer_payments", breaking_magnitude: 0.8, breaking_share: { value: 0.2, text: "20%" } })))
      .toBe("your customers take about 25% longer to pay than they do now");
    expect(readyText(wi({ breaking_magnitude: null }))).toBeNull();
    expect(readyText(wi({ preset: "unknown", breaking_magnitude: 0.8, breaking_share: { value: 0.2, text: "20%" } }))).toBeNull();
  });
});

function strings(x: unknown, out: string[] = []): string[] {
  if (typeof x === "string") out.push(x);
  else if (Array.isArray(x)) x.forEach((y) => strings(y, out));
  else if (x && typeof x === "object") Object.values(x).forEach((y) => strings(y, out));
  return out;
}

describe("demo payload", () => {
  const screen = buildOwnerScreen(demo, "demo file");

  it("uses no banned words anywhere", () => {
    for (const s of strings(screen)) for (const b of BANNED) expect(s, s).not.toMatch(b);
  });

  it("leads with the 30-day estimate, not the 90-day figure", () => {
    expect(screen.status?.headline).toContain("30 days");
    expect(screen.status?.headline).toMatch(/\d+–\d+%\.$/);
    expect(screen.status?.estimateNote).toMatch(/estimate/);
    expect(screen.status?.range30).toMatch(/^Likely balance on /);
    expect(screen.status?.hero.value).toMatch(/^\d+–\d+%$/);
    expect(screen.status?.hero.caption).toContain("next 30 days");
    expect(screen.status?.tiles.map((x) => x.label)).toEqual(["In the bank today", expect.stringMatching(/^Likely balance on /)]);
  });

  it("shows no per-step risk figures and only fresh-path totals", () => {
    const all = strings(screen.plan).join("\n");
    for (const a of demo.plan.actions) if (a.risk_after) expect(all).not.toContain(`then ${a.risk_after.text}`);
    expect(screen.plan.change).toMatch(/now, .* with all of them/);
  });

  it("puts the MSME catch on the costly stretch and not on the penalty-only one", () => {
    const tasks = [...screen.plan.tasks, ...screen.plan.optional];
    const s1 = tasks.find((t) => t.title.includes("Supplier S1"))!;
    const s2 = tasks.find((t) => t.title.includes("Supplier S2"))!;
    expect(s1.catches.map((c) => c.text)).toContain(MSME_STRETCH_CATCH);
    expect(s2.catches.map((c) => c.text)).not.toContain(MSME_STRETCH_CATCH);
  });

  it("folds owed interest into the chase task", () => {
    const chase = [...screen.plan.tasks, ...screen.plan.optional].find((t) => t.title.startsWith("Call "))!;
    expect(chase.catches.some((c) => /late-payment interest/.test(c.text))).toBe(true);
  });

  it("has one row per buyer, sorted by rupees at stake", () => {
    const rows = buyerRows(demo);
    expect(new Set(rows.map((r) => r.buyer)).size).toBe(rows.length);
    for (let i = 1; i < rows.length; i++) expect(rows[i - 1].stakePaise).toBeGreaterThanOrEqual(rows[i].stakePaise);
    expect(screen.owes.rows.length).toBeLessThanOrEqual(BUYERS_SHOWN);
    expect(screen.owes.rows.length + screen.owes.more.length).toBe(rows.length);
    expect(rows.some((r) => r.lost.some((l) => l.includes("already left out of your forecast")))).toBe(true);
  });

  it("does not show the orders what-if, whose 180-day view was never computed", () => {
    expect(screen.ready?.items.join(" ")).not.toMatch(/orders/i);
    expect(screen.ready?.items.length).toBeLessThanOrEqual(3);
  });

  it("drops the chart's 90% band and fades after day 30", () => {
    expect(screen.chart?.near.at(-1)?.date).toBe(screen.chart?.far[0]?.date);
    expect(JSON.stringify(screen.chart)).not.toMatch(/lo90|hi90/);
  });
});

describe("formatting helpers", () => {
  it("shares the unit in a rupee range when it can", () => {
    expect(rsRange(277163175, 471606988)).toBe("Rs 27.7–47.2 lakh");
    expect(rsRange(4500000, 12000000)).toBe("Rs 45,000 to Rs 1.2 lakh");
    expect(rsRange(-66000000, 172000000)).toBe("Rs −6.6 to 17.2 lakh");
  });

  it("scales customer bars to the largest amount", () => {
    const rows = buyerRows(demo);
    expect(rows[0].bar.stake).toBe(1);
    for (const r of rows) {
      expect(r.bar.stake).toBeGreaterThan(0);
      expect(r.bar.overdue).toBeGreaterThanOrEqual(0);
      expect(r.bar.overdue).toBeLessThanOrEqual(1);
    }
  });
});

describe("dashboard pieces", () => {
  const screen = buildOwnerScreen(demo);

  it("builds the four KPI tiles, the risk one with its status badge", () => {
    expect(screen.kpis.map((k) => k.id)).toEqual(["risk", "bank", "likely", "overdue"]);
    const risk = screen.kpis[0];
    expect(risk.value).toBe(screen.status?.hero.value);
    expect(risk.badge?.label).toBe(screen.status?.label);
    expect(risk.serious).toBe(screen.status?.serious);
  });

  it("totals overdue across every customer, shown or not", () => {
    const rows = buyerRows(demo);
    const k = screen.kpis.find((x) => x.id === "overdue")!;
    expect(k.note).toBe(`across ${rows.filter((r) => r.overduePaise > 0).length} customers`);
    expect(screen.owes.totalOverduePaise).toBe(rows.reduce((s, r) => s + r.overduePaise, 0));
  });

  it("fills every table cell, with a dash when unknown", () => {
    for (const r of buyerRows(demo)) {
      for (const c of [r.cells.outstanding, r.cells.overdue, r.cells.pays, r.cells.neverPaid]) expect(c).toBeTruthy();
      expect(r.cells.neverPaid === "—").toBe(r.cells.neverPaidDetail === null);
    }
  });

  it("gives each breaking point a big value and a label", () => {
    expect(screen.ready?.cards.map((c) => c.value)).toEqual(["15% longer", "26 days", "Rs 5.9 lakh"]);
    expect(screen.ready?.cards.length).toBe(screen.ready?.items.length);
  });

  it("drops the risk tile without risk figures", () => {
    const v = clone();
    v.headline.risk = [];
    v.risk_by_day = [];
    expect(buildOwnerScreen(v).kpis.map((k) => k.id)).toEqual(["bank", "likely", "overdue"]);
  });
});

describe("simulation explorer", () => {
  const sim = buildOwnerScreen(demo).simulation!;

  it("offers the baseline and only the what-ifs whose horizon was computed", () => {
    expect(sim.scenarios[0].id).toBe("base");
    const ids = sim.scenarios.map((x) => x.id);
    expect(ids).not.toContain("reduced_sales");
    expect(ids.length).toBe(1 + demo.what_ifs.filter((w) => w.preset !== "reduced_sales").length);
  });

  it("reads out every day, with the plan only on the baseline", () => {
    for (const sc of sim.scenarios) {
      expect(sc.days).toHaveLength(demo.fan.days.length);
      expect(sc.days.every((d) => d.middle && d.range && d.chance)).toBe(true);
      expect(sc.days.some((d) => d.planChance !== null)).toBe(sc.id === "base");
      expect(sc.chart.reference === null).toBe(sc.id === "base");
    }
    expect(sim.scenarios[0].days[29].chance).toBe(chanceEstimate(demo.risk_by_day[29].before));
  });

  it("describes what-ifs without model words", () => {
    const buyers = sim.scenarios.find((x) => x.id === "delayed_customer_payments")!;
    expect(buyers.description).toBe("Customers take twice as long to pay as they do now.");
    for (const sc of sim.scenarios) for (const b of BANNED) expect(sc.description).not.toMatch(b);
  });

  it("marks only sharp falls of the typical balance, and lists them in the legend", () => {
    const c = sim.scenarios[0].chart;
    const pts = [...c.near, ...c.far.slice(1)];
    const today = demo.headline.balance_today.paise;
    expect(c.events.length).toBeGreaterThan(0);
    expect(c.events.length).toBeLessThanOrEqual(BIG_PAYMENTS.max);
    for (const e of c.events) {
      const i = pts.findIndex((p) => p.date === e.date);
      expect(pts[i - 1].median - pts[i].median).toBeGreaterThanOrEqual(BIG_PAYMENTS.share * today);
    }
    expect(c.legend.some((l) => l.kind === "event")).toBe(true);
  });

  it("labels the day bar with the payload's own path count", () => {
    expect(sim.labels.barTitle).toContain(demo.meta.n_paths!.toLocaleString("en-IN"));
  });

  it("keeps the day bar inside its scale", () => {
    for (const d of sim.scenarios[0].days) {
      const b = d.bar;
      expect(0 <= b.lo90 && b.lo90 <= b.lo50 && b.lo50 <= b.median && b.median <= b.hi50 && b.hi50 <= b.hi90 && b.hi90 <= 1).toBe(true);
    }
  });

  it("starts the slider at day 30", () => {
    expect(sim.defaultDay).toBe(30);
  });
});

describe("dashboard follows the simulation", () => {
  const screen = buildOwnerScreen(demo);
  const byId = (k: { id: string }[], id: string) => k.find((x) => x.id === id) as ReturnType<typeof dashboardAt>["kpis"][number];

  it("matches the static tiles on the baseline at day 30", () => {
    const at = dashboardAt(screen, "base", 30);
    expect(byId(at.kpis, "risk").value).toBe(byId(screen.kpis, "risk").value);
    expect(byId(at.kpis, "likely").value).toBe(byId(screen.kpis, "likely").value);
    expect(byId(at.kpis, "risk").badge?.label).toBe(screen.status?.label);
    expect(at.planNote).toBeNull();
  });

  it("reads the chance and balance for the chosen day from the runs", () => {
    const at = dashboardAt(screen, "base", 60);
    expect(byId(at.kpis, "risk").value).toBe(chanceEstimate(demo.risk_by_day[59].before));
    expect(byId(at.kpis, "risk").label).toContain(screen.simulation!.scenarios[0].days[59].dateLabel.split(",")[0]);
  });

  it("switches every simulated tile to the what-if, keeps today's facts, and flags the plan", () => {
    const w = demo.what_ifs.find((x) => x.preset === "delayed_customer_payments")!;
    const at = dashboardAt(screen, w.preset, 30);
    expect(byId(at.kpis, "risk").value).toBe(chanceEstimate(w.risk_by_day![29]));
    expect(byId(at.kpis, "risk").tag).toContain("buyers pay slower");
    expect(byId(at.kpis, "bank")).toEqual(byId(screen.kpis, "bank"));
    expect(byId(at.kpis, "overdue")).toEqual(byId(screen.kpis, "overdue"));
    expect(at.planNote).toContain("Buyers pay slower");
    expect(at.readyNote).toBeTruthy();
  });

  it("gives each what-if its own status from its own chances", () => {
    const sc = screen.simulation!.scenarios.find((x) => x.id === "delayed_customer_payments")!;
    const w = demo.what_ifs.find((x) => x.preset === "delayed_customer_payments")!;
    expect(sc.status?.key).toBe(statusOf({ p30: w.risk_by_day![29], p90: w.risk_by_day![89], target: 0.05,
      runwayDay: w.risk_by_day!.findIndex((p) => p >= 0.5) + 1 || null })?.key);
  });

  it("clamps the day and ignores unknown scenarios", () => {
    expect(byId(dashboardAt(screen, "base", 999).kpis, "risk").value).toBe(chanceEstimate(demo.risk_by_day[89].before));
    expect(dashboardAt(screen, "nope", 10).kpis).toBe(screen.kpis);
  });
});

describe("obligations", () => {
  const screen = buildOwnerScreen(demo, "api", ob);
  const o = screen.obligations!;

  it("belongs to this owner view only when tenant, run and as-of match", () => {
    expect(o).not.toBeNull();
    expect(buildOwnerScreen(demo, "api", { ...ob, run_id: "other" }).obligations).toBeNull();
    expect(buildOwnerScreen(demo, "api", { ...ob, tenant_id: "other" }).obligations).toBeNull();
    expect(buildOwnerScreen(demo, "api", null).obligations).toBeNull();
  });

  it("lists the next 30 days and keeps the rest behind show-all", () => {
    expect(o.rows.length + o.later.length).toBe(ob.items.length);
    expect(o.rows.every((r) => r.date <= "2026-04-30")).toBe(true);
    const sum = ob.items.filter((i) => i.day <= OBLIGATIONS_WINDOW_DAYS).reduce((a, i) => a + (i.amount_paise ?? 0), 0);
    expect(o.summary.startsWith(`Rs ${(sum / 1e7).toFixed(1).replace(/\.0$/, "")} lakh`)).toBe(true);
  });

  it("never suggests moving a statutory payment", () => {
    for (const r of [...o.rows, ...o.later]) if (["GST", "TDS deposit", "PF", "ESI"].includes(r.label)) expect(r.flex).toBe("law");
    for (const x of o.suggestions) expect(x.title).not.toMatch(/move the (GST|TDS|PF|ESI)/);
  });

  it("marks a day tight only above the owner's target", () => {
    for (const i of ob.items) {
      const r = [...o.rows, ...o.later].find((x) => x.date === i.due_date && x.label === OBLIGATION_RULES[i.kind].label)!;
      expect(r.tight).toBe(i.p_below_cushion_that_day > 0.05);
    }
  });

  it("leads with the tightest day and puts aside the biggest tight legal payment", () => {
    const tightest = [...ob.items].sort((a, b) => b.p_below_cushion_that_day - a.p_below_cushion_that_day)[0];
    expect(o.suggestions[0].title).toContain(OBLIGATION_RULES[tightest.kind].noun);
    expect(o.suggestions.find((x) => x.title.startsWith("Keep "))?.title).toContain("GST");
  });

  it("flags tightness as baseline-only under a what-if", () => {
    expect(dashboardAt(screen, "base", 30).obligationsNote).toBeNull();
    expect(dashboardAt(screen, "unexpected_expense", 30).obligationsNote).toContain("An unexpected bill");
  });

  it("uses no banned words", () => {
    for (const x of strings(o)) for (const b of BANNED) expect(x, x).not.toMatch(b);
  });
});

describe("status with the plan", () => {
  const screen = buildOwnerScreen(demo);
  const risk = () => screen.kpis.find((k) => k.id === "risk")!;

  it("shows the way out under the badge when the plan improves the state", () => {
    expect(risk().badge?.label).toBe("Needs attention");
    expect(risk().withPlan).toEqual({ key: "ok", label: "On track", text: "On track with the plan" });
  });

  it("hides it when the plan does not improve the state, and under a what-if", () => {
    const v = clone();
    v.risk_by_day = v.risk_by_day.map((r) => ({ ...r, after: r.before }));
    expect(buildOwnerScreen(v).kpis.find((k) => k.id === "risk")!.withPlan).toBeNull();
    expect(dashboardAt(screen, "unexpected_expense", 30).kpis.find((k) => k.id === "risk")!.withPlan).toBeNull();
    expect(dashboardAt(screen, "base", 45).kpis.find((k) => k.id === "risk")!.withPlan).toEqual(risk().withPlan);
  });
});

describe("low days carry no alarm", () => {
  const screen = buildOwnerScreen(demo);
  const risk = (scenario: string, day: number) => dashboardAt(screen, scenario, day).kpis.find((k) => k.id === "risk")!;

  it("drops the serious badge and red when the chosen day reads under 2%, and says where it rises to", () => {
    for (const day of [1, 2, 3, 4]) {
      const k = risk("base", day);
      expect(k.value).toBe("under 2%");
      expect(k.badge).toBeNull();
      expect(k.serious).toBe(false);
      expect(k.withPlan).toBeNull();
      expect(k.later).toBe(`Rises to ${chanceEstimate(demo.risk_by_day[29].before)} by 30 Apr`);
    }
  });

  it("keeps the badge from the first day at 2% or more", () => {
    const k = risk("base", 5);
    expect(demo.risk_by_day[4].before).toBeGreaterThanOrEqual(0.02);
    expect(k.badge?.label).toBe("Needs attention");
    expect(k.serious).toBe(true);
    expect(k.later).toBeNull();
  });

  it("applies under a what-if too, using that what-if's own chances", () => {
    const w = demo.what_ifs.find((x) => x.preset === "revised_payment_schedule")!;
    expect(w.risk_by_day![1]).toBeLessThan(0.02);
    expect(risk(w.preset, 2).badge).toBeNull();
    expect(risk(w.preset, 2).later).toContain(chanceEstimate(w.risk_by_day![29]));
  });
});

describe("degrades without fields", () => {
  it("hides the status without the risk figures", () => {
    const v = clone();
    v.headline.risk = [];
    v.risk_by_day = [];
    expect(buildOwnerScreen(v).status).toBeNull();
  });

  it("hides the chart without a fan and the ready section without breaking points", () => {
    const v = clone();
    v.fan.days = [];
    v.what_ifs = v.what_ifs.map((w) => ({ ...w, breaking_magnitude: null }));
    const s = buildOwnerScreen(v);
    expect(s.chart).toBeNull();
    expect(s.ready).toBeNull();
  });

  it("says there is nothing to do for an empty plan", () => {
    const v = clone();
    v.plan.actions = [];
    expect(buildOwnerScreen(v).plan.empty).toBeTruthy();
  });

  it("words the cushion without a threshold", () => {
    const v = clone();
    delete v.fan.threshold;
    const s = buildOwnerScreen(v);
    expect(s.status?.headline).toContain("your cushion");
    expect(s.chart?.cushionPaise).toBeNull();
  });
});
