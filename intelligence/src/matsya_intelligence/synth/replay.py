"""Replay one generator world day by day, as if data were arriving live.

Each step moves as_of forward one day and reveals the contract rows Track A would have
learned in that day (known_at in (previous as_of, new as_of]), including the re-stamp rows a
fetch writes: open-invoice labels with censored_at = the ledger-complete-through watermark,
and the bank-reported balance (synth.world.complete_through, docs/track-b-decisions.md).
The revealed rows are appended to the running tables, so every step is an ordinary as-of
read of an append-only history. Each step also emits ops.events rows (new_credit,
new_invoice) for what it revealed, so the real event path can replace this loop later.

Every step runs the normal pipeline (io.adapters -> run.run_tenant) and keeps its forecast.
score() then compares each day's fan chart with the balances that actually happened
(forecast error and interval coverage by horizon, against a flat "balance stays where it
is" baseline), and buyer_range() tracks one buyer's payment-time range as labels arrive.
Together they are the learning-curve benchmark.

    python -m matsya_intelligence.synth.replay --days 30 --paths demo --out DIR
"""

import argparse
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
from matsya_contracts.events import NewCredit, NewInvoice
from matsya_contracts.validate import schema, validate

from matsya_intelligence.calibrate import Calibrator
from matsya_intelligence.io import adapters
from matsya_intelligence.io.sink import write_outputs
from matsya_intelligence.run import run_tenant
from matsya_intelligence.synth.world import Truth, contract_tables_at

PATHS = {"demo": 2_000, "benchmark": 10_000}
AS_OF_TABLES = ("ledger.transactions", "ledger.balances", "ledger.invoices", "ledger.matches",
                "labels.invoice_outcomes")
SCORE_HORIZONS = (1, 7, 14, 30, 60, 90)
KEEP = ("forecast.runs", "forecast.balance_quantiles", "forecast.shortfall")


@dataclass
class StepResult:
    as_of: datetime
    events: pl.DataFrame  # ops.events revealed in this step
    forecast: dict[str, pl.DataFrame]  # KEEP tables of this step's run
    buyer: dict  # payment-time range of the tracked buyer
    calibration: dict[str, pl.DataFrame] | None = None  # scored (outcomes known this step), bands, state
    paths: np.ndarray | None = None  # (n_paths, len(SCORE_HORIZONS)) balances at the scored horizons
    engine: object = field(default=None, repr=False)  # EngineResult of this step; dropped once used


@dataclass
class Replay:
    truth: Truth
    n_paths: int = PATHS["demo"]
    buyer_id: str | None = None  # tracked buyer; default the one with most invoices
    out_dir: Path | None = None  # if set, every step's output tables are written here
    start: datetime | None = None  # first as_of; default the world's as_of (earlier is fine: truth has the past)
    calibrator: Calibrator | None = None  # calibration state carried from step to step
    keep_paths: bool = False  # keep each step's simulated balances at SCORE_HORIZONS (offline benchmarks)
    as_of: datetime = field(init=False)
    tables: dict[str, pl.DataFrame] = field(init=False)
    steps: list[StepResult] = field(init=False, default_factory=list)

    def __post_init__(self):
        self.as_of = self.start or self.truth.config.as_of
        self.tables = contract_tables_at(self.truth, self.as_of)
        self.tenant_id = self.truth.config.tenant_id
        if self.buyer_id is None:
            counts = self.truth.invoices.group_by("buyer_id").len().sort(["len", "buyer_id"], descending=[True, False])
            self.buyer_id = counts["buyer_id"][0]

    @property
    def last_day(self) -> datetime:
        c = self.truth.config
        return datetime.combine(c.as_of_date + timedelta(days=c.future_days), c.as_of.timetz())

    def reveal(self, cut: datetime) -> pl.DataFrame:
        """Append the rows learned in (self.as_of, cut]; return the ops.events for them."""
        if cut <= self.as_of or cut > self.last_day:
            raise ValueError(f"cut {cut} must be after {self.as_of} and by {self.last_day}")
        full = contract_tables_at(self.truth, cut)
        prev = self.as_of
        seen_inv = set(self.tables["ledger.invoices"]["invoice_id"])
        new = {t: full[t].filter((pl.col("known_at") > prev) & (pl.col("known_at") <= cut)) for t in AS_OF_TABLES}
        for t in AS_OF_TABLES:
            self.tables[t] = pl.concat([self.tables[t], new[t]])
        for t in full:  # reference tables are not versioned: take the current copy
            if t not in AS_OF_TABLES:
                self.tables[t] = full[t]
        self.as_of = cut
        return self._events(new["ledger.transactions"], new["ledger.invoices"].filter(
            ~pl.col("invoice_id").is_in(list(seen_inv))).unique("invoice_id", keep="first", maintain_order=True))

    def _events(self, txns: pl.DataFrame, invoices: pl.DataFrame) -> pl.DataFrame:
        rows = []
        for r in txns.filter(pl.col("amount_paise") > 0).iter_rows(named=True):
            p = NewCredit(txn_uid=r["txn_uid"], account_id=r["account_id"], value_date=r["value_date"],
                          amount_paise=r["amount_paise"])
            rows.append(("new_credit", r["txn_uid"], p.model_dump_json(), r["known_at"]))
        for r in invoices.iter_rows(named=True):
            p = NewInvoice(invoice_id=r["invoice_id"], buyer_id=r["buyer_id"], issue_date=r["issue_date"],
                           gross_paise=r["gross_paise"])
            rows.append(("new_invoice", r["invoice_id"], p.model_dump_json(), r["known_at"]))
        ev = pl.DataFrame(
            [(self.tenant_id, hashlib.sha256(f"{k}|{i}".encode()).hexdigest()[:32], k, j, at) for k, i, j, at in rows],
            schema=schema("ops.events"), orient="row").sort("at", "event_id")
        return validate(ev, "ops.events")

    def run_step(self) -> StepResult:
        """Forecast at the current as_of with the normal pipeline (no calibration side effects)."""
        out, inp, eng = run_tenant(self.tables, self.tenant_id, self.as_of, "replay", self.n_paths, return_inputs=True)
        if self.out_dir is not None:
            write_outputs(out, Path(self.out_dir) / "runs" / self.as_of.date().isoformat())
        cols = [h for h in SCORE_HORIZONS if h < eng.balance.shape[1]]
        return StepResult(as_of=self.as_of, events=pl.DataFrame(schema=schema("ops.events")),
                          forecast={k: out[k] for k in KEEP}, buyer=self._buyer_range(inp),
                          paths=eng.balance[:, cols].copy() if self.keep_paths else None, engine=eng)

    def known_balances(self) -> dict:
        """Closing balance per date known at the current as_of (as-of read, summed over accounts)."""
        b = adapters.as_of_read(self.tables["ledger.balances"], ["tenant_id", "account_id", "date"], self.as_of)
        b = b.filter(pl.col("date") <= self.as_of.date()).group_by("date").agg(pl.col("closing_paise").sum())
        return dict(zip(b["date"], b["closing_paise"]))

    def step(self) -> StepResult:
        """Advance one day and reveal; score the calibration bands whose outcome is now known;
        run with the current calibration state; issue this run's bands; keep the result."""
        events = self.reveal(self.as_of + timedelta(days=1))
        day = self.as_of.date()
        scored = self.calibrator.update(day, self.known_balances()) if self.calibrator else None
        res = self.run_step()
        res.events = events
        if self.calibrator is not None:
            bands = self.calibrator.issue(day, res.engine.balance)
            state = self.calibrator.state_table(self.tenant_id, day)
            res.calibration = {"scored": scored, "bands": bands, "forecast.calibration_state": state}
            if self.out_dir is not None:
                write_outputs({"forecast.calibration_state": state}, Path(self.out_dir) / "runs" / day.isoformat())
        res.engine = None
        self.steps.append(res)
        return res

    def run(self, days: int, on_step=None) -> list[StepResult]:
        for _ in range(days):
            res = self.step()
            if on_step is not None:
                on_step(res)
        if self.out_dir is not None:
            d = Path(self.out_dir)
            pl.concat([s.events for s in self.steps]).write_parquet(d / "ops.events.parquet")
            score(self).write_parquet(d / "scores.parquet")
            buyer_range(self).write_parquet(d / "buyer_range.parquet")
        return self.steps

    def _buyer_range(self, inp) -> dict:
        """Weeks to pay once due, 1/p, from the tracked buyer's posterior draws (alive invoices)."""
        i = inp.buyer_ids.index(self.buyer_id)
        w = 1 / np.asarray(inp.timing.p[i])
        lab = self.tables["labels.invoice_outcomes"]
        n_paid = lab.filter((pl.col("buyer_id") == self.buyer_id) & pl.col("paid_date").is_not_null())[
            "invoice_id"].n_unique()
        return {"as_of": self.as_of.date(), "buyer_id": self.buyer_id, "n_paid_labels": n_paid,
                "weeks_q05": float(np.quantile(w, 0.05)), "weeks_q50": float(np.quantile(w, 0.5)),
                "weeks_q95": float(np.quantile(w, 0.95))}


def score(replay: Replay) -> pl.DataFrame:
    """Per horizon, over every step whose target date is inside the world: median error and
    absolute error of q50 (Rs), coverage of the 50% and 90% bands, and the flat baseline
    (balance at as_of carried forward). n = number of (step, date) pairs."""
    truth = replay.truth.balances.select("date", pl.col("closing_paise").alias("actual"))
    rows = []
    for s in replay.steps:
        bq = s.forecast["forecast.balance_quantiles"].join(truth, on="date")
        start = truth.filter(pl.col("date") == s.as_of.date())["actual"][0]
        rows.append(bq.with_columns(((pl.col("date") - pl.lit(s.as_of.date())).dt.total_days()).alias("h"),
                                    pl.lit(start).alias("flat")))
    df = pl.concat(rows).filter(pl.col("h").is_in(SCORE_HORIZONS))
    return df.group_by("h").agg(
        pl.len().alias("n"),
        ((pl.col("q50") - pl.col("actual")) / 100).mean().alias("mean_error_rs"),
        ((pl.col("q50") - pl.col("actual")).abs() / 100).mean().alias("mae_rs"),
        ((pl.col("flat") - pl.col("actual")).abs() / 100).mean().alias("baseline_flat_mae_rs"),
        pl.col("actual").is_between(pl.col("q25"), pl.col("q75")).mean().alias("coverage_50"),
        pl.col("actual").is_between(pl.col("q05"), pl.col("q95")).mean().alias("coverage_90"),
    ).sort("h")


def buyer_range(replay: Replay) -> pl.DataFrame:
    """The tracked buyer's payment-time range per step, with the planted truth (1/p)."""
    p = replay.truth.buyers.filter(pl.col("buyer_id") == replay.buyer_id)["p"][0]
    return pl.DataFrame([s.buyer for s in replay.steps]).with_columns(pl.lit(1 / p).alias("true_weeks"))


def main() -> None:
    from matsya_intelligence.synth import WorldConfig, generate_world

    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--paths", choices=sorted(PATHS), default="demo")
    ap.add_argument("--preset", default="borderline")
    ap.add_argument("--past-seed", type=int, default=0)
    ap.add_argument("--future-seed", type=int, default=0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    _, truth = generate_world(WorldConfig.preset(a.preset), a.past_seed, a.future_seed)
    r = Replay(truth, n_paths=PATHS[a.paths], out_dir=Path(a.out) if a.out else None)

    def show(s: StepResult) -> None:
        sf = s.forecast["forecast.shortfall"]
        print(f"{s.as_of.date()}  events {s.events.height:3d}  p_short(90) {sf['p_short'][-1]:.3f}  "
              f"runway {s.forecast['forecast.runs']['runway_days'][0]}  "
              f"buyer weeks {s.buyer['weeks_q05']:.1f}-{s.buyer['weeks_q95']:.1f}")

    r.run(a.days, on_step=show)
    with pl.Config(tbl_rows=-1, tbl_cols=-1):
        print(score(r))
    print(buyer_range(r))


if __name__ == "__main__":
    main()
