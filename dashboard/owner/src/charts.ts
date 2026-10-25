import * as Plot from "@observablehq/plot";
import { rs, rsTick, shortDate } from "./format";
import type { BalanceChart, ChartLabel, ChartPoint } from "./viewmodel";

// Shared look: no frame, no background, faint horizontal gridlines only, direct labels instead of legends.
// RISK is the one accent, kept for what is really at risk (the cushion line here).
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

/** The owner page's balance chart: middle estimate, the band half of the estimates fall in, the cushion.
 *  Days after the first 30 are faded (the view-model splits them into `far`). No 90% band. */
export function balanceChart(c: BalanceChart, width: number): SVGSVGElement | HTMLElement {
  const all = [...c.near, ...c.far];
  const x = (p: ChartPoint) => d(p.date);
  const lo = Math.min(...all.map((p) => p.lo), c.cushionPaise ?? Infinity);
  const hi = Math.max(...all.map((p) => p.hi));
  const marks: Plot.Markish[] = [
    Plot.gridY({ stroke: GRID, strokeOpacity: 1, ticks: 5 }),
    Plot.areaY(c.near, { x, y1: "lo", y2: "hi", fill: BAND50, curve: "monotone-x" }),
    Plot.lineY(c.near, { x, y: "median", stroke: INK, strokeWidth: 1.75, curve: "monotone-x" }),
  ];
  if (c.far.length) {
    marks.push(
      Plot.areaY(c.far, { x, y1: "lo", y2: "hi", fill: BAND50, fillOpacity: 0.4, curve: "monotone-x" }),
      Plot.lineY(c.far, { x, y: "median", stroke: INK, strokeOpacity: 0.35, strokeWidth: 1.75, curve: "monotone-x" }),
    );
  }
  if (c.cushionPaise !== null) {
    marks.push(Plot.ruleY([c.cushionPaise], { stroke: RISK, strokeWidth: 1 }));
    if (c.cushionLabel) {
      marks.push(Plot.text([c.cushionPaise], {
        x: d(all[0].date), y: (v: number) => v, text: () => c.cushionLabel, fill: RISK, textAnchor: "start", dy: -8, fontSize: 12,
      }));
    }
  }
  if (!narrow(width)) {
    const gap = (hi - lo) * 0.06;
    marks.push(Plot.text(spread(c.labels.map((l) => ({ ...l, y: l.paise })), gap), {
      x: (l: ChartLabel) => d(l.date), y: "y", text: "text", fill: MID, textAnchor: "start", dx: 8, fontSize: 12,
    }));
  }
  marks.push(Plot.tip(all, Plot.pointerX({ x, y: "median", ...tip, title: (p: ChartPoint) => p.tip })));
  return Plot.plot({
    width, height: narrow(width) ? 240 : 300, marginLeft: 64, marginRight: narrow(width) ? 8 : 120, marginTop: 16, marginBottom: 32, style,
    x: { type: "time", label: null, ticks: narrow(width) ? 3 : 6, tickFormat: (v: Date) => v.toLocaleDateString("en-IN", { day: "numeric", month: "short" }), tickSize: 0 },
    y: { label: null, tickFormat: rsTick, ticks: 5, tickSize: 0 },
    marks,
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
