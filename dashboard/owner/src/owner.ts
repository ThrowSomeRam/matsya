import "./style.css";
import { balanceChart } from "./charts";
import { loadOwnerView } from "./data";
import { el } from "./format";
import { buildOwnerScreen, type BalanceChart, type BuyerRow, type OwnerScreen, type Task } from "./viewmodel";

// Layout only: every figure and sentence comes from the view-model (viewmodel.ts).

const app = document.getElementById("app")!;
let redraw: (() => void) | null = null;

function width(node: HTMLElement): number {
  return Math.max(280, Math.floor(node.clientWidth || app.clientWidth - 48));
}

function status(s: OwnerScreen): HTMLElement | null {
  const st = s.status;
  if (!st) return null;
  const label = el("p", { cls: "status-label" }, st.label);
  label.dataset.serious = String(st.serious);
  return el("section", { id: "status" },
    el("h1", {}, s.title),
    label,
    el("p", { cls: "headline-text" }, st.headline),
    st.balance ? el("p", { cls: "balance nums" }, st.balance) : null,
    st.range30 ? el("p", { cls: "nums" }, st.range30) : null,
    el("p", { cls: "note" }, st.estimateNote));
}

function taskItem(t: Task): HTMLElement {
  return el("li", {},
    el("p", { cls: "task-title" }, t.title),
    t.cost ? el("p", { cls: "muted small nums" }, t.cost) : null,
    ...t.catches.map((c) => {
      const p = el("p", { cls: "catch small" }, c.text);
      p.dataset.serious = String(c.serious);
      return p;
    }));
}

function todo(s: OwnerScreen): HTMLElement {
  const p = s.plan;
  return el("section", { id: "todo" },
    el("h2", {}, s.sections.todo),
    p.empty ? el("p", { cls: "muted" }, p.empty) : null,
    p.tasks.length ? el("ol", { cls: "plain tasks" }, ...p.tasks.map(taskItem)) : null,
    p.optional.length && p.optionalLabel
      ? el("details", { cls: "optional" }, el("summary", {}, p.optionalLabel), el("ol", { cls: "plain tasks" }, ...p.optional.map(taskItem)))
      : null,
    p.totals ? el("p", { cls: "totals" }, p.totals) : null,
    p.change ? el("p", {}, p.change) : null,
    p.statusChange ? el("p", { cls: "muted" }, p.statusChange) : null,
    p.caveat ? el("p", { cls: "note" }, p.caveat) : null);
}

function buyerItem(r: BuyerRow): HTMLElement {
  return el("li", {},
    el("div", { cls: "row" }, el("span", { cls: "name" }, r.buyer), el("span", { cls: "right nums" }, r.amount)),
    r.overdue ? el("p", { cls: "muted small nums" }, r.overdue) : null,
    r.habit ? el("p", { cls: "muted small" }, r.habit) : null,
    r.slower ? el("p", { cls: "muted small" }, r.slower) : null,
    ...r.lost.map((l) => el("p", { cls: "small nums" }, l)));
}

function owes(s: OwnerScreen): HTMLElement {
  const o = s.owes;
  return el("section", { id: "owes" },
    el("h2", {}, s.sections.owes),
    o.empty ? el("p", { cls: "muted" }, o.empty) : el("p", { cls: "muted" }, s.sections.owesNote),
    o.rows.length ? el("ul", { cls: "plain" }, ...o.rows.map(buyerItem)) : null,
    o.more.length && o.moreLabel
      ? el("details", { cls: "optional" }, el("summary", {}, o.moreLabel), el("ul", { cls: "plain" }, ...o.more.map(buyerItem)))
      : null);
}

function outlook(s: OwnerScreen, chart: BalanceChart): HTMLElement {
  const holder = el("div", { cls: "chart" });
  redraw = () => holder.replaceChildren(balanceChart(chart, width(holder)));
  requestAnimationFrame(redraw);
  return el("section", { id: "outlook" }, el("h2", {}, s.sections.outlook), holder, el("p", { cls: "note" }, chart.caption));
}

function ready(s: OwnerScreen): HTMLElement | null {
  const r = s.ready;
  if (!r) return null;
  return el("section", { id: "ready" },
    el("h2", {}, s.sections.ready),
    el("p", {}, r.lead),
    el("ul", { cls: "bullets" }, ...r.items.map((i) => el("li", {}, i))));
}

function footer(s: OwnerScreen): HTMLElement {
  const f = s.footer;
  return el("footer", { cls: "foot" },
    ...[f.freshness, f.asOf, f.source].filter((x): x is string => !!x).map((x) => el("p", {}, x)),
    el("p", {}, el("a", { href: f.evidenceHref }, f.evidenceText)));
}

function render(s: OwnerScreen) {
  app.replaceChildren(...[
    status(s) ?? el("h1", {}, s.title),
    todo(s),
    owes(s),
    s.chart ? outlook(s, s.chart) : null,
    ready(s),
    footer(s),
  ].filter((x): x is HTMLElement => x !== null));
}

let resizeTimer = 0;
addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = window.setTimeout(() => redraw?.(), 120);
});

loadOwnerView().then(({ data, source }) => render(buildOwnerScreen(data, source)))
  .catch((e: Error) => app.replaceChildren(el("p", {}, `Could not load your cash outlook. ${e.message}`)));
