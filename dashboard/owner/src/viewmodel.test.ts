import { describe, expect, it } from "vitest";
import demoJson from "../../../eval/demo/owner_view.json";
import type { OwnerView, PlanAction, WhatIf } from "./types";
import {
  BANNED, BUYERS_SHOWN, MSME_STRETCH_CATCH, STATUS_RULES, STRETCH_PENALTY_PAISE, buildOwnerScreen, buyerRows,
  chanceEstimate, extraTimeToPay, horizonCovered, isLikelyMsmeSupplierFromStretchCost, longerText, readyText,
  roundPct, splitSteps, statusOf, supplierLabel,
} from "./viewmodel";

const demo = demoJson as unknown as OwnerView;
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
