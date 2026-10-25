import "./style.css";
import { driversChart, fanChart, riskChart, type Overlay } from "./charts";
import { loadOwnerView } from "./data";
import { el } from "./format";
import type { OwnerView, WhatIf } from "./types";

const app = document.getElementById("app")!;
let view: OwnerView;
let selected: string | null = null;
let source = "";

function width(node: HTMLElement): number {
  return Math.max(280, Math.floor(node.clientWidth || app.clientWidth - 48));
}

function headline(v: OwnerView): HTMLElement {
  const h = v.headline;
  return el("section", { id: "headline" },
    el("h1", {}, "Your cash outlook"),
    el("p", { cls: "balance nums" }, `${h.balance_today.text} in the bank today`),
    el("div", { cls: "risks" },
      ...h.risk.map((r) => el("div", {},
        el("div", { cls: "risk-num nums" }, r.chance.text),
        el("div", { cls: "muted small" }, `chance of running short within ${r.day} days`)))),
    el("p", { cls: "runway" },
      h.runway_day == null ? "Runway: beyond 90 days, on current odds." : `Runway: ${h.runway_text}, when running short becomes more likely than not.`),
    el("p", { cls: "headline-text" }, h.drivers_sentence));
}

function overlay(): Overlay | undefined {
  const w = view.what_ifs.find((x) => x.preset === selected);
  return w ? { name: w.name, fan: w.fan ?? [], risk: w.risk_by_day ?? [] } : undefined;
}

function chartSection(id: string, title: string, sub: string | null, draw: (w: number) => Node, note?: string): HTMLElement {
  const holder = el("div", { cls: "chart" });
  const s = el("section", { id }, el("h2", {}, title), sub ? el("p", { cls: "muted" }, sub) : null, holder,
    note ? el("p", { cls: "note" }, note) : null);
  requestAnimationFrame(() => holder.replaceChildren(draw(width(holder))));
  (s as HTMLElement & { redraw?: () => void }).redraw = () => holder.replaceChildren(draw(width(holder)));
  return s;
}

function plan(v: OwnerView): HTMLElement {
  const p = v.plan;
  return el("section", { id: "plan" },
    el("h2", {}, "The plan"),
    el("p", { cls: "muted" }, p.summary),
    el("ol", { cls: "plain" }, ...p.actions.map((a) => el("li", {},
      el("div", { cls: "row" },
        el("span", {}, el("span", { cls: "step-num" }, `${a.rank}.`), a.description),
        el("span", { cls: "right nums" }, `${a.cost.text}, then ${a.risk_after?.text} chance`))))),
    el("p", { cls: "totals" }, `Total cost ${p.total_cost.text}. Chance of running short within 90 days: ${p.risk_before.text} now, ${p.risk_after.text} with the plan.`),
    p.caveat ? el("p", { cls: "caveat" }, p.caveat) : null);
}

function whatIfs(v: OwnerView): HTMLElement {
  const list = el("ul", { cls: "plain" });
  const render = () => list.replaceChildren(...v.what_ifs.map((w: WhatIf) => {
    const on = w.preset === selected;
    const b = el("button", { cls: "whatif", type: "button" },
      el("div", { cls: "row" },
        el("span", { cls: "name" }, w.name),
        el("span", { cls: "right nums" }, `${w.risk_90.text} chance within 90 days`)),
      el("p", { cls: "small" }, w.description),
      el("p", { cls: "muted small" }, `${w.change_text} ${w.runway_text}`));
    b.setAttribute("aria-pressed", String(on));
    b.addEventListener("click", () => {
      selected = on ? null : w.preset;
      render();
      redrawCharts();
    });
    return el("li", {}, b, on ? el("p", { cls: "breaking" }, w.breaking_point) : null);
  }));
  render();
  return el("section", { id: "what-ifs" },
    el("h2", {}, "What if"),
    el("p", { cls: "muted" }, "Pick one to see it on the first two charts. Pick it again to go back."),
    list);
}

function owed(v: OwnerView): HTMLElement {
  const o = v.owed;
  return el("section", { id: "owed" },
    el("h2", {}, "Money you're owed"),
    o.items.length === 0
      ? el("p", { cls: "muted" }, "No buyer owes you late-payment interest today.")
      : el("ul", { cls: "plain" }, ...o.items.map((i) => el("li", {},
        el("div", { cls: "row" },
          el("span", {}, `${i.buyer}, ${i.amount.text}`),
          el("span", { cls: "right nums" }, `${i.days_late} days late, interest ${i.interest.text}`))))),
    o.items.length ? el("p", { cls: "totals" }, `Interest owed in total: ${o.total_interest.text}.`) : null,
    el("p", { cls: "note" }, o.note));
}

let sections: (HTMLElement & { redraw?: () => void })[] = [];

function redrawCharts() {
  for (const s of sections) s.redraw?.();
}

function render(v: OwnerView) {
  const dates = v.risk_by_day.map((r) => r.date);
  const fan = chartSection("outlook", "Balance outlook", "Your bank balance over the next 90 days, across the futures we simulate.",
    (w) => fanChart(v.fan.days, v.fan.threshold?.paise ?? 0, w, overlay()), v.fan.note);
  const risk = chartSection("risk", "Chance of running short, by date", "Doing nothing, and with the plan below.",
    (w) => riskChart(dates, v.risk_by_day.map((r) => r.before), v.risk_by_day.map((r) => r.after), w, overlay()));
  const drivers = chartSection("drivers", "What's driving the risk",
    "How much the chance of running short within 90 days would drop if each one were paid on time.",
    (w) => driversChart(v.drivers, w));
  sections = [fan, risk, drivers];
  app.replaceChildren(headline(v), fan, risk, drivers, plan(v), whatIfs(v), owed(v),
    el("p", { cls: "foot" }, `${v.meta.data_freshness} Figures as of ${v.meta.as_of}. Data from the ${source}. `,
      el("a", { href: "/evidence/" }, "How this forecast compares")));
}

let resizeTimer = 0;
addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = window.setTimeout(redrawCharts, 120);
});

loadOwnerView().then(({ data, source: s }) => {
  view = data;
  source = s === "api" ? "live service" : "saved demo file";
  render(view);
}).catch((e: Error) => app.replaceChildren(el("p", {}, `Could not load your cash outlook. ${e.message}`)));
