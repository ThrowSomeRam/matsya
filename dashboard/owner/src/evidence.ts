import "./style.css";
import { evidenceChart } from "./charts";
import { loadEvidence } from "./data";
import { el, pct, rs } from "./format";
import type { Evidence } from "./types";

const app = document.getElementById("app")!;

function sentences(e: Evidence): string[] {
  const a = e.across_worlds;
  const lakh = (r: number) => rs(r * 100);
  return [
    `On this business, a naive forecast that counts every bill as paid on its due date puts the chance of running ` +
      `short within 90 days at ${pct(e.p_short_90.naive)}; ours says ${pct(e.p_short_90.ours)}, and the truth was ${pct(e.p_short_90.truth)}.`,
    `It counts ${a.demo_dead_bills} bills worth ${rs(a.demo_dead_paise)} as certain cash that our model marks as likely never paid.`,
    `Across ${a.n_worlds} test businesses, the 90-day balance is off by ${lakh(a.median_err_90_rs.ours)} on average with ours ` +
      `and ${lakh(a.median_err_90_rs.naive)} with the naive forecast, and ours flags ${a.caught.ours} of the ${a.truth_shortfalls} ` +
      `real shortfalls against ${a.caught.naive} for the naive one.`,
  ];
}

function render(e: Evidence) {
  const holder = el("div", { cls: "chart" });
  const draw = () => holder.replaceChildren(evidenceChart(e.dates, e.series.truth, e.series.naive, e.series.ours,
    e.threshold_paise, Math.max(280, holder.clientWidth)));
  app.replaceChildren(el("section", {},
    el("h1", {}, "How the forecast compares"),
    el("h2", { cls: "after-h1" }, "Naive forecast, our forecast and what happened, next 90 days"),
    el("p", { cls: "muted" }, "Middle outcome of each. The shaded range is where 9 in 10 of the true futures landed."),
    holder,
    ...sentences(e).map((s) => el("p", { cls: "evidence-text" }, s)),
    el("p", { cls: "foot" }, el("a", { href: "/" }, "Back to your cash outlook"))));
  requestAnimationFrame(draw);
  addEventListener("resize", () => requestAnimationFrame(draw));
}

loadEvidence().then(({ data }) => render(data))
  .catch((err: Error) => app.replaceChildren(el("p", {}, `Could not load the comparison. ${err.message}`)));
