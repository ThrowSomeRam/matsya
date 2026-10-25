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

const style = { background: "transparent", color: MID, fontFamily: "inherit", fontSize: "14px", overflow: "visible" };
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

/** The owner page's balance chart: middle estimate, the band half of the estimates fall in, the cushion. */
export function balanceChart(c: BalanceChart, width: number, marker?: ChartPoint): SVGSVGElement | HTMLElement {
  const all = [...c.near, ...c.far];
  const x = (p: ChartPoint) => d(p.date);
  const marks: Plot.Markish[] = [
    Plot.gridY({ stroke: GRID, strokeOpacity: 1, ticks: 4 }),
    Plot.areaY(c.near, { x, y1: "lo", y2: "hi", fill: BAND50, curve: "monotone-x" }),
    Plot.lineY(c.near, { x, y: "median", stroke: INK, strokeWidth: 2, curve: "monotone-x" }),
  ];
  if (c.far.length) {
    marks.push(
      Plot.areaY(c.far, { x, y1: "lo", y2: "hi", fill: BAND50, fillOpacity: 0.4, curve: "monotone-x" }),
      Plot.lineY(c.far, { x, y: "median", stroke: INK, strokeOpacity: 0.35, strokeWidth: 1.75, curve: "monotone-x" }),
    );
  }
  if (c.reference) {
    marks.push(Plot.lineY(c.reference, { x, y: "median", stroke: MID, strokeWidth: 1.25, strokeDasharray: "4,4", curve: "monotone-x" }));
  }
  if (c.cushionPaise !== null) marks.push(Plot.ruleY([c.cushionPaise], { stroke: RISK, strokeWidth: 1.5 }));
  if (c.events.length) {
    marks.push(Plot.dot(c.events, { x: (l: ChartLabel) => d(l.date), y: "paise", dy: -10, r: 5, symbol: "triangle", rotate: 180, fill: MID }));
  }
  if (marker) {
    marks.push(
      Plot.ruleX([d(marker.date)], { stroke: INK, strokeOpacity: 0.25, strokeWidth: 1 }),
      Plot.ruleX([marker], { x, y1: "lo", y2: "hi", stroke: INK, strokeWidth: 3, strokeLinecap: "round" }),
      Plot.dot([marker], { x, y: "median", r: 6, fill: INK, stroke: "white", strokeWidth: 2 }),
    );
  }
  marks.push(Plot.tip(all, Plot.pointerX({ x, y: "median", ...tip, title: (p: ChartPoint) => p.tip })));
  return Plot.plot({
    width, height: narrow(width) ? 240 : 300, marginLeft: 64, marginRight: 12, marginTop: 16, marginBottom: 32, style,
    x: { type: "time", label: null, ticks: narrow(width) ? 3 : 4, tickFormat: (v: Date) => v.toLocaleDateString("en-IN", { day: "numeric", month: "short" }), tickSize: 0 },
    y: { label: null, tickFormat: rsTick, ticks: 4, tickSize: 0 },
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
