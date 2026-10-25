import * as Plot from "@observablehq/plot";
import { pct, rs, rsTick, shortDate } from "./format";
import type { Driver, FanDay } from "./types";

// Shared look: no frame, no background, faint horizontal gridlines only, direct labels instead of legends.
const INK = "#111111";
const MID = "#6B6B6B";
const RISK = "#B42318";
const PLAN = "#0F5257";
const GRID = "#F2F2F2";
const BAND90 = "#EDEDED";
const BAND50 = "#D4D4D4";

const style = { background: "transparent", color: MID, fontFamily: "inherit", fontSize: "12px", overflow: "visible" };
const tip = { fill: "white", stroke: "white", fontSize: 13, textPadding: 8 } as const;

const d = (iso: string) => new Date(iso + "T00:00:00");

function narrow(width: number) {
  return width < 520;
}

/** Push end labels apart so none overlap: at least `gap` (in data units) between neighbours. */
function spread<T extends { y: number }>(labels: T[], gap: number): T[] {
  const s = [...labels].sort((a, b) => a.y - b.y).map((l) => ({ ...l }));
  for (let i = 1; i < s.length; i++) if (s[i].y - s[i - 1].y < gap) s[i].y = s[i - 1].y + gap;
  return s;
}

export interface Overlay {
  name: string;
  fan: FanDay[];
  risk: number[];
}

/** Chart 1: the 90-day balance outlook. 90% band lighter, 50% band darker, the middle line, the cushion. */
export function fanChart(days: FanDay[], thresholdPaise: number, width: number, overlay?: Overlay): SVGSVGElement | HTMLElement {
  const last = days[days.length - 1];
  const right = narrow(width) ? 8 : 120;
  const span = Math.max(...days.map((r) => r.hi90_paise)) - Math.min(...days.map((r) => r.lo90_paise), thresholdPaise);
  const labels = spread([
    { y: last.median_paise, text: "Middle outcome", color: INK },
    { y: (last.hi50_paise + last.hi90_paise) / 2, text: "9 in 10 futures", color: MID },
    { y: (last.median_paise + last.hi50_paise) / 2, text: "Half of futures", color: MID },
  ], span * 0.06);
  const marks: Plot.Markish[] = [
    Plot.gridY({ stroke: GRID, strokeOpacity: 1, ticks: 5 }),
    Plot.areaY(days, { x: (r: FanDay) => d(r.date), y1: "lo90_paise", y2: "hi90_paise", fill: BAND90, curve: "monotone-x" }),
    Plot.areaY(days, { x: (r: FanDay) => d(r.date), y1: "lo50_paise", y2: "hi50_paise", fill: BAND50, curve: "monotone-x" }),
    Plot.lineY(days, { x: (r: FanDay) => d(r.date), y: "median_paise", stroke: INK, strokeWidth: 1.75, curve: "monotone-x" }),
    Plot.ruleY([thresholdPaise], { stroke: RISK, strokeWidth: 1 }),
    Plot.text([thresholdPaise], {
      x: d(days[0].date), y: (v: number) => v, text: () => `Your cushion, ${rs(thresholdPaise)}`, fill: RISK,
      textAnchor: "start", dy: 12, fontSize: 12,
    }),
  ];
  if (!narrow(width)) {
    marks.push(Plot.text(labels, { x: d(last.date), y: "y", text: "text", fill: "color", textAnchor: "start", dx: 8, fontSize: 12 }));
  }
  if (overlay) {
    const o = overlay.fan;
    const ol = o[o.length - 1];
    marks.push(
      Plot.lineY(o, { x: (r: FanDay) => d(r.date), y: "lo90_paise", stroke: RISK, strokeOpacity: 0.55, strokeWidth: 1, strokeDasharray: "2,3", curve: "monotone-x" }),
      Plot.lineY(o, { x: (r: FanDay) => d(r.date), y: "hi90_paise", stroke: RISK, strokeOpacity: 0.55, strokeWidth: 1, strokeDasharray: "2,3", curve: "monotone-x" }),
      Plot.lineY(o, { x: (r: FanDay) => d(r.date), y: "median_paise", stroke: RISK, strokeWidth: 1.75, strokeDasharray: "6,4", curve: "monotone-x" }),
    );
    if (!narrow(width)) {
      marks.push(Plot.text([ol], { x: d(ol.date), y: "median_paise", text: () => overlay.name, fill: RISK, textAnchor: "start", dx: 8, dy: 14, fontSize: 12 }));
    }
  }
  marks.push(
    Plot.tip(days, Plot.pointerX({
      x: (r: FanDay) => d(r.date), y: "median_paise", ...tip,
      title: (r: FanDay) => `${shortDate(r.date)}\nMiddle outcome ${rs(r.median_paise)}\nHalf of futures ${rs(r.lo50_paise)} to ${rs(r.hi50_paise)}\n9 in 10 futures ${rs(r.lo90_paise)} to ${rs(r.hi90_paise)}`,
    })),
  );
  return Plot.plot({
    width, height: narrow(width) ? 260 : 340, marginLeft: 64, marginRight: right, marginTop: 16, marginBottom: 32, style,
    x: { type: "time", label: null, ticks: narrow(width) ? 3 : 6, tickFormat: (v: Date) => v.toLocaleDateString("en-IN", { day: "numeric", month: "short" }), tickSize: 0 },
    y: { label: null, tickFormat: rsTick, ticks: 5, tickSize: 0 },
    marks,
  });
}

/** Chart 2: chance of running short by each date, doing nothing and with the plan (and a what-if). */
export function riskChart(dates: string[], before: number[], after: (number | null)[], width: number, overlay?: Overlay): SVGSVGElement | HTMLElement {
  const rows = dates.map((dt, i) => ({ date: d(dt), iso: dt, before: before[i], after: after[i], what: overlay?.risk[i] }));
  const last = rows[rows.length - 1];
  const ends = [
    { y: last.before, text: `Doing nothing ${pct(last.before)}`, color: RISK },
    ...(last.after != null ? [{ y: last.after, text: `With the plan ${pct(last.after)}`, color: PLAN }] : []),
    ...(overlay && last.what != null ? [{ y: last.what, text: `${overlay.name} ${pct(last.what)}`, color: MID }] : []),
  ];
  const marks: Plot.Markish[] = [
    Plot.gridY({ stroke: GRID, strokeOpacity: 1, ticks: 5 }),
    Plot.lineY(rows, { x: "date", y: "before", stroke: RISK, strokeWidth: 2 }),
  ];
  if (last.after != null) marks.push(Plot.lineY(rows, { x: "date", y: "after", stroke: PLAN, strokeWidth: 2, strokeDasharray: "7,4" }));
  if (overlay) marks.push(Plot.lineY(rows, { x: "date", y: "what", stroke: MID, strokeWidth: 1.75, strokeDasharray: "2,3" }));
  marks.push(
    Plot.text(spread(ends, 0.07), { x: last.date, y: "y", text: "text", fill: "color", textAnchor: "start", dx: 8, fontSize: 12 }),
    Plot.tip(rows, Plot.pointerX({
      x: "date", y: "before", ...tip,
      title: (r: typeof rows[number]) => `By ${shortDate(r.iso)}\nDoing nothing ${pct(r.before)}` +
        (r.after != null ? `\nWith the plan ${pct(r.after)}` : "") + (overlay && r.what != null ? `\n${overlay.name} ${pct(r.what)}` : ""),
    })),
  );
  return Plot.plot({
    width, height: narrow(width) ? 240 : 280, marginLeft: 48, marginRight: narrow(width) ? 128 : 170, marginTop: 16, marginBottom: 32, style,
    x: { type: "time", label: null, ticks: narrow(width) ? 3 : 6, tickFormat: (v: Date) => v.toLocaleDateString("en-IN", { day: "numeric", month: "short" }), tickSize: 0 },
    y: { label: null, domain: [0, 1], tickFormat: (v: number) => `${Math.round(v * 100)}%`, ticks: 5, tickSize: 0 },
    marks,
  });
}

/** Chart 3: what's driving the risk. One bar per driver, its plain label above it, the amount it adds at the end. */
export function driversChart(drivers: Driver[], width: number): SVGSVGElement | HTMLElement {
  const rows = drivers.filter((r) => r.contribution.value > 0).map((r, i) => ({
    key: String(i).padStart(2, "0"), label: r.label, v: r.contribution.value * 100, text: `${Math.round(r.contribution.value * 100)} points`, reason: r.reason,
  }));
  const max = Math.max(...rows.map((r) => r.v), 1);
  return Plot.plot({
    width, height: rows.length * 52 + 8, marginLeft: 0, marginRight: 72, marginTop: 22, marginBottom: 0, style,
    x: { axis: null, domain: [0, max] },
    y: { axis: null, domain: rows.map((r) => r.key), padding: 0.62 },
    marks: [
      Plot.barX(rows, { x: "v", y: "key", fill: RISK }),
      Plot.text(rows, { x: 0, y: "key", text: "label", textAnchor: "start", dy: -16, fill: INK, fontSize: 13 }),
      Plot.text(rows, { x: "v", y: "key", text: "text", textAnchor: "start", dx: 6, fill: MID, fontSize: 12 }),
      Plot.tip(rows, Plot.pointerY({ x: "v", y: "key", ...tip, title: (r: typeof rows[number]) => `${r.label}\n${r.reason}` })),
    ],
  });
}

export interface EvidenceLine { q05: number[]; q50: number[]; q95: number[] }

/** Chart 4: naive vs ours vs truth, middle outcome over 90 days, truth's 9-in-10 range behind. */
export function evidenceChart(dates: string[], truth: EvidenceLine, naive: EvidenceLine, ours: EvidenceLine, threshold: number, width: number): SVGSVGElement | HTMLElement {
  const rows = dates.map((dt, i) => ({
    date: d(dt), iso: dt, t: truth.q50[i], tlo: truth.q05[i], thi: truth.q95[i], n: naive.q50[i], o: ours.q50[i],
  }));
  const last = rows[rows.length - 1];
  const ends = [
    { y: last.t, text: "What happened (truth)", color: INK },
    { y: last.o, text: "Our forecast", color: PLAN },
    { y: last.n, text: "Naive forecast", color: RISK },
  ];
  const all = rows.flatMap((r) => [r.tlo, r.thi, r.n, r.o]);
  const gap = (Math.max(...all) - Math.min(...all)) * 0.07;
  return Plot.plot({
    width, height: narrow(width) ? 280 : 360, marginLeft: 64, marginRight: narrow(width) ? 120 : 160, marginTop: 16, marginBottom: 32, style,
    x: { type: "time", label: null, ticks: narrow(width) ? 3 : 6, tickFormat: (v: Date) => v.toLocaleDateString("en-IN", { day: "numeric", month: "short" }), tickSize: 0 },
    y: { label: null, tickFormat: rsTick, ticks: 5, tickSize: 0 },
    marks: [
      Plot.gridY({ stroke: GRID, strokeOpacity: 1, ticks: 5 }),
      Plot.areaY(rows, { x: "date", y1: "tlo", y2: "thi", fill: BAND90, curve: "monotone-x" }),
      Plot.ruleY([threshold], { stroke: MID, strokeWidth: 1, strokeDasharray: "1,3" }),
      Plot.lineY(rows, { x: "date", y: "t", stroke: INK, strokeWidth: 2, curve: "monotone-x" }),
      Plot.lineY(rows, { x: "date", y: "o", stroke: PLAN, strokeWidth: 2, strokeDasharray: "7,4", curve: "monotone-x" }),
      Plot.lineY(rows, { x: "date", y: "n", stroke: RISK, strokeWidth: 2, strokeDasharray: "2,3", curve: "monotone-x" }),
      Plot.text(spread(ends, gap), { x: last.date, y: "y", text: "text", fill: "color", textAnchor: "start", dx: 8, fontSize: 12 }),
      Plot.tip(rows, Plot.pointerX({
        x: "date", y: "t", ...tip,
        title: (r: typeof rows[number]) => `${shortDate(r.iso)}\nWhat happened, middle ${rs(r.t)}\nOur forecast ${rs(r.o)}\nNaive forecast ${rs(r.n)}`,
      })),
    ],
  });
}
