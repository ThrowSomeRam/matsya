import "./style.css";
import { bell, openAction, play } from "./actions";
import { ACTION_LABELS, msmedNoticeSpec, obligationReminderEvents, reminderSpec, remindAllSpec, taskSpec } from "./automations";
import { balanceChart } from "./charts";
import { loadObligations, loadOwnerView } from "./data";
import { el } from "./format";
import { buildOwnerScreen, dashboardAt, type BuyerRow, type ObligationRow, type Kpi, type OwnerScreen, type Simulation, type StatusKey, type Task } from "./viewmodel";

// Layout only: every figure and sentence comes from the view-model (viewmodel.ts).

const app = document.getElementById("app")!;
document.body.classList.add("dash-page");
let redraw: (() => void) | null = null;

// Status icons (stroke): the badge never relies on colour alone.
const ALERT = '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>';
const ICONS: Record<StatusKey, string> = {
  ok: '<circle cx="12" cy="12" r="10"/><path d="m8.5 12.5 2.5 2.5 4.5-5"/>',
  watch: '<circle cx="12" cy="12" r="10"/><path d="M12 7v5l3 2"/>',
  at_risk: ALERT,
  act_now: ALERT,
};

function icon(key: StatusKey): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("icon");
  svg.innerHTML = ICONS[key];
  return svg;
}

const serious = <T extends HTMLElement>(node: T, on: boolean): T => {
  node.dataset.serious = String(on);
  return node;
};

function card(cls: string, title: string | null, ...children: (Node | null)[]): HTMLElement {
  return el("section", { cls: `card ${cls}` }, title ? el("h2", { cls: "card-title" }, title) : null, ...children);
}

function header(s: OwnerScreen): HTMLElement {
  const f = s.footer;
  return el("header", { cls: "appbar" },
    el("div", { cls: "brand" }, el("span", { cls: "logo" }, s.sections.app), el("span", { cls: "page" }, s.sections.page)),
    el("div", { cls: "meta" },
      ...[f.freshness, f.asOf].filter((x): x is string => !!x).map((x) => el("span", {}, x)),
      el("a", { href: f.evidenceHref }, f.evidenceText)),
    bell());
}

function actionButton(label: string, onClick: () => void, primary = false): HTMLButtonElement {
  const b = el("button", { type: "button", cls: primary ? "btn small primary" : "btn small" }, label);
  b.addEventListener("click", onClick);
  return b;
}

function kpi(k: Kpi): HTMLElement {
  return serious(el("div", { cls: `card kpi kpi-${k.id}` },
    el("p", { cls: "kpi-label" }, k.label),
    el("p", { cls: "kpi-value" }, k.value),
    k.note ? el("p", { cls: "kpi-note" }, k.note) : null,
    k.tag ? el("p", { cls: "kpi-tag" }, k.tag) : null,
    k.later ? el("p", { cls: "kpi-later" }, k.later) : null,
    k.badge ? serious(el("p", { cls: "badge" }, icon(k.badge.key), el("span", {}, k.badge.label)), k.serious) : null,
    k.withPlan ? el("p", { cls: "with-plan" }, el("span", { cls: "with-plan-arrow" }, "→"), icon(k.withPlan.key), el("span", {}, k.withPlan.text)) : null),
  k.serious);
}

function simulationCard(s: OwnerScreen, sim: Simulation, onChange: (scenarioId: string, day: number) => void): HTMLElement {
  let scen = 0, day = sim.defaultDay, timer = 0;
  const holder = el("div", { cls: "chart" });
  const legend = el("ul", { cls: "legend" });
  const desc = el("p", { cls: "scenario-desc" });
  const caption = el("p", { cls: "card-note" });
  const dateLabel = el("p", { cls: "sim-date" });
  const seg = (cls: string) => el("span", { cls });
  const d90 = seg("d90"), d50 = seg("d50"), dmed = seg("dmed"), dcush = seg("dcush");
  const wide = el("p", { cls: "dist-note" });
  const slider = el("input", { type: "range", min: "1", value: String(day), cls: "slider" });
  slider.setAttribute("aria-label", sim.labels.day);
  const play = el("button", { type: "button", cls: "play" }, sim.labels.play);
  const chips = sim.scenarios.map((sc, i) => {
    const b = el("button", { type: "button", cls: "chip" }, sc.name);
    b.addEventListener("click", () => { scen = i; update(true); });
    return b;
  });

  const update = (full = false) => {
    const sc = sim.scenarios[scen];
    slider.max = String(sc.days.length);
    const d = sc.days[Math.min(day, sc.days.length) - 1];
    const marker = [...sc.chart.near, ...sc.chart.far].find((p) => p.date === d.date);
    holder.replaceChildren(balanceChart(sc.chart, Math.max(280, Math.floor(holder.clientWidth)), marker));
    dateLabel.textContent = d.dateLabel;
    onChange(sc.id, d.day);
    const pct = (x: number) => `${(x * 100).toFixed(2)}%`;
    d90.style.left = pct(d.bar.lo90); d90.style.width = pct(d.bar.hi90 - d.bar.lo90);
    d50.style.left = pct(d.bar.lo50); d50.style.width = pct(d.bar.hi50 - d.bar.lo50);
    dmed.style.left = pct(d.bar.median);
    dcush.hidden = d.bar.cushion === null;
    if (d.bar.cushion !== null) dcush.style.left = pct(d.bar.cushion);
    wide.textContent = d.wide;
    if (full) {
      chips.forEach((b, i) => b.setAttribute("aria-pressed", String(i === scen)));
      desc.textContent = sc.description;
      caption.textContent = sc.chart.caption;
      legend.replaceChildren(...sc.chart.legend.map((l) => el("li", {}, el("span", { cls: `key key-${l.kind}` }), l.text)));
    }
  };
  const stop = () => { clearInterval(timer); timer = 0; play.textContent = sim.labels.play; };
  slider.addEventListener("input", () => { stop(); day = Number(slider.value); update(); });
  play.addEventListener("click", () => {
    if (timer) return stop();
    const last = sim.scenarios[scen].days.length;
    if (day >= last) day = 1;
    play.textContent = sim.labels.pause;
    timer = window.setInterval(() => {
      day = Math.min(day + 1, last);
      slider.value = String(day);
      update();
      if (day >= last) stop();
    }, 90);
  });
  redraw = () => update();
  requestAnimationFrame(() => update(true));

  dcush.append(el("span", { cls: "dcush-label" }, sim.labels.barCushion));
  return card("chart-card", null,
    el("div", { cls: "card-head" }, el("h2", { cls: "card-title" }, s.sections.outlook), legend),
    el("div", { cls: "chips", role: "group" }, el("span", { cls: "chips-label" }, sim.labels.scenarios), ...chips),
    desc,
    holder,
    el("div", { cls: "scrub" }, play, slider, dateLabel),
    el("div", { cls: "dist" },
      el("p", { cls: "stat-label" }, sim.labels.barTitle),
      el("div", { cls: "dist-track" }, d90, d50, dmed, dcush),
      wide),
    caption);
}

let screenRows: BuyerRow[] = [];

function taskItem(t: Task, n: number | null): HTMLElement {
  const spec = t.action ? taskSpec(t.action, screenRows) : null;
  return el("li", { cls: "task" },
    n === null ? el("span", { cls: "step-dot" }) : el("span", { cls: "step-num" }, String(n)),
    el("div", {},
      el("p", { cls: "task-title" }, t.title),
      t.cost ? el("p", { cls: "task-meta" }, t.cost) : null,
      ...t.catches.map((c) => serious(el("p", { cls: "catch" }, c.text), c.serious)),
      spec ? el("div", { cls: "task-actions" }, actionButton(ACTION_LABELS.doThis, () => openAction(spec), n !== null)) : null));
}

const planNote = el("p", { cls: "scenario-note" });
const obligationsNote = el("p", { cls: "scenario-note" });
const readyNote = el("p", { cls: "scenario-note" });

function todoCard(s: OwnerScreen): HTMLElement {
  const p = s.plan, c = p.compare;
  return card("todo-card", s.sections.todo,
    planNote,
    c ? el("div", { cls: "compare" },
      el("div", {}, el("p", { cls: "compare-value" }, c.now), el("p", { cls: "compare-label" }, c.nowLabel)),
      el("span", { cls: "compare-arrow" }, "→"),
      el("div", {}, el("p", { cls: "compare-value" }, c.after), el("p", { cls: "compare-label" }, c.afterLabel))) : null,
    c ? el("p", { cls: "card-note" }, c.caption) : null,
    p.empty ? el("p", { cls: "card-note" }, p.empty) : null,
    p.tasks.length ? el("ol", { cls: "tasks" }, ...p.tasks.map((t, i) => taskItem(t, i + 1))) : null,
    p.optional.length && p.optionalLabel
      ? el("details", { cls: "more" }, el("summary", {}, p.optionalLabel), el("ol", { cls: "tasks" }, ...p.optional.map((t) => taskItem(t, null))))
      : null,
    p.totals || p.statusChange
      ? el("div", { cls: "card-foot" }, ...[p.totals, p.statusChange].filter((x): x is string => !!x).map((x) => el("p", {}, x)))
      : null,
    p.caveat ? el("p", { cls: "card-note" }, p.caveat) : null);
}

let asOf: string | null = null;

function buyerRow(r: BuyerRow, h: OwnerScreen["sections"]["table"]): HTMLTableRowElement {
  const bar = el("div", { cls: "bar" }, el("div", { cls: "bar-overdue" }));
  bar.style.width = `${(r.bar.stake * 100).toFixed(1)}%`;
  (bar.firstElementChild as HTMLElement).style.width = `${(r.bar.overdue * 100).toFixed(1)}%`;
  const td = (label: string, cls: string, ...c: (Node | string | null)[]) => {
    const cell = el("td", { cls }, ...c);
    cell.dataset.label = label;
    return cell;
  };
  const never = el("span", {}, r.cells.neverPaid);
  if (r.cells.neverPaidDetail) {
    never.title = r.cells.neverPaidDetail;
    never.classList.add("has-detail");
  }
  return el("tr", {},
    td(h.customer, "customer", el("span", { cls: "name" }, r.buyer),
      el("div", { cls: "row-actions" }, actionButton(ACTION_LABELS.remind, () => openAction(reminderSpec(r))),
        ...(() => { const n = msmedNoticeSpec(r, asOf); return n ? [actionButton(ACTION_LABELS.notice, () => openAction(n))] : []; })())),
    td(h.outstanding, "num outstanding", el("span", {}, r.cells.outstanding), el("div", { cls: "bar-wrap" }, bar)),
    td(h.overdue, "num", el("span", {}, r.cells.overdue)),
    td(h.pays, "pays", el("span", {}, r.cells.pays, r.cells.slow ? el("span", { cls: "tag" }, r.cells.slow) : null)),
    td(h.neverPaid, "num never", never));
}

function tableCard(s: OwnerScreen): HTMLElement {
  const o = s.owes, h = s.sections.table;
  if (o.empty) return card("table-card", s.sections.owes, el("p", { cls: "card-note" }, o.empty));
  const table = (rows: BuyerRow[]) => el("table", { cls: "buyers" },
    el("thead", {}, el("tr", {}, ...[h.customer, h.outstanding, h.overdue, h.pays, h.neverPaid].map((x, i) =>
      el("th", { cls: i === 0 || i === 3 ? "" : "num" }, x)))),
    el("tbody", {}, ...rows.map((r) => buyerRow(r, h))));
  const all = remindAllSpec([...o.rows, ...o.more]);
  return card("table-card", null,
    el("div", { cls: "card-head" }, el("h2", { cls: "card-title" }, s.sections.owes),
      all ? actionButton(ACTION_LABELS.remindAll, () => openAction(all), true) : null),
    el("p", { cls: "card-sub" }, s.sections.owesBarKey),
    table(o.rows),
    o.more.length && o.moreLabel ? el("details", { cls: "more" }, el("summary", {}, o.moreLabel), table(o.more)) : null,
    el("p", { cls: "card-note" }, s.sections.owesNeverNote));
}

function obligationsCard(s: OwnerScreen): HTMLElement | null {
  const o = s.obligations;
  if (!o) return null;
  const c = o.columns;
  const row = (r: ObligationRow) => {
    const td = (label: string, cls: string, ...k: (Node | string | null)[]) => {
      const cell = el("td", { cls }, ...k);
      cell.dataset.label = label;
      return cell;
    };
    const flex = el("span", { cls: `flex flex-${r.flex}` }, r.flexLabel);
    flex.title = r.tip;
    const tr = el("tr", {},
      td(c.date, "date", r.dateLabel),
      td(c.payment, "payment", el("span", { cls: "name" }, r.label)),
      td(c.amount, "num", r.amount),
      td(c.flex, "flex-cell", flex,
        r.flex === "law" ? actionButton(ACTION_LABELS.remindMe, () => play(obligationReminderEvents(r, asOf))) : null),
      td(c.day, "num day", serious(el("span", {}, r.dayChance), r.tight)));
    return tr;
  };
  const table = (rows: ObligationRow[]) => el("table", { cls: "buyers obligations" },
    el("thead", {}, el("tr", {}, ...[c.date, c.payment, c.amount, c.flex, c.day].map((x, i) =>
      el("th", { cls: i === 2 || i === 4 ? "num" : "" }, x)))),
    el("tbody", {}, ...rows.map(row)));
  return card("obligations-card", null,
    el("div", { cls: "card-head" }, el("h2", { cls: "card-title" }, s.sections.obligations)),
    obligationsNote,
    el("p", { cls: "ob-summary" }, o.summary),
    ...[o.lawShare, o.suppliers].filter((x): x is string => !!x).map((x) => el("p", { cls: "card-sub" }, x)),
    table(o.rows),
    o.later.length && o.laterLabel ? el("details", { cls: "more" }, el("summary", {}, o.laterLabel), table(o.later)) : null,
    el("p", { cls: "card-note" }, o.dayNote));
}

function suggestionsCard(s: OwnerScreen): HTMLElement | null {
  const o = s.obligations;
  if (!o || !o.suggestions.length) return null;
  return card("suggest-card", s.sections.obligationsDo,
    el("ul", { cls: "suggest" }, ...o.suggestions.map((x) => serious(el("li", {},
      el("p", { cls: "suggest-title" }, x.title), el("p", { cls: "suggest-detail" }, x.detail)), x.serious))));
}

function readyCard(s: OwnerScreen): HTMLElement | null {
  const r = s.ready;
  if (!r) return null;
  return card("ready-card", s.sections.ready,
    readyNote,
    el("p", { cls: "card-sub" }, r.lead),
    el("ul", { cls: "ready" }, ...r.cards.map((c) => el("li", {},
      el("p", { cls: "ready-value" }, c.value), el("p", { cls: "ready-label" }, c.label)))));
}

function render(s: OwnerScreen) {
  const f = s.footer;
  screenRows = [...s.owes.rows, ...s.owes.more];
  asOf = s.asOfDate;
  const kpiRow = el("div", { cls: "kpis" }, ...s.kpis.map(kpi));
  let last = "";
  const follow = (scenarioId: string, day: number) => {
    const key = `${scenarioId}:${day}`;
    if (key === last) return;
    last = key;
    const at = dashboardAt(s, scenarioId, day);
    kpiRow.replaceChildren(...at.kpis.map(kpi));
    planNote.textContent = at.planNote ?? "";
    planNote.hidden = !at.planNote;
    readyNote.textContent = at.readyNote ?? "";
    readyNote.hidden = !at.readyNote;
    obligationsNote.textContent = at.obligationsNote ?? "";
    obligationsNote.hidden = !at.obligationsNote;
  };
  planNote.hidden = readyNote.hidden = obligationsNote.hidden = true;
  const obCard = obligationsCard(s), sugCard = suggestionsCard(s);
  app.replaceChildren(
    header(s),
    el("div", { cls: "dash" },
      kpiRow,
      el("div", { cls: "row" }, s.simulation ? simulationCard(s, s.simulation, follow) : el("div", {}), todoCard(s)),
      obCard ? el("div", { cls: "row" }, obCard, sugCard ?? el("div", {})) : null,
      el("div", { cls: "row" }, tableCard(s), readyCard(s) ?? el("div", {})),
      el("footer", { cls: "foot" },
        ...[s.status?.estimateNote ?? null, f.source].filter((x): x is string => !!x).map((x) => el("p", {}, x)))));
}

let resizeTimer = 0;
addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = window.setTimeout(() => redraw?.(), 120);
});

Promise.all([loadOwnerView(), loadObligations()]).then(([{ data, source }, ob]) => render(buildOwnerScreen(data, source, ob)))
  .catch((e: Error) => app.replaceChildren(el("p", {}, `Could not load your cash outlook. ${e.message}`)));
