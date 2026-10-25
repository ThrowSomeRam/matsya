"""Stress sweep: where does the pipeline degrade, and where does it break?

Each fault runs alone across a range of intensities, then two combinations: "combined"
(every fault at the same level) and "no_anchors" (drop_all_ids + drop_balances +
drop_summary). Every level runs on several fault seeds and on several worlds (generator
seeds, densities). Every run renders the world, runs A4 and the invoice ingest, and is
judged against truth (score.py). Per level the report shows the spread across worlds and
seeds.

Two thresholds per fault:
  not exact at  the lowest level where any run's rebuild is no longer exact;
  silent at     the lowest level where any run is silently wrong (something is wrong and
                nothing flags it). This one matters more.
Levels that are wrong but loud (a payload refused, a row flagged) are the correct way to
fail and are reported as such, not as test failures.

partial_mode only changes what happens to partial windows, so the accept_gap sweep runs
the faults that include them (partial_window, combined); the rest would repeat batch_fails.

loop: "open" renders every payload from the fault plan alone, so A2 never reacts to a
fetch A4 refused unexpectedly and later fetches keep failing; loud levels there are partly
a harness artifact. "closed" runs A4 while rendering and re-fetches from coverage after
any refusal, as A2 would. The two are reported separately.

Runs execute in parallel processes; results are sorted, so output is deterministic.
Writes <out_dir>/stress_sweep.csv (one row per run) and stress_thresholds.csv.
"""

import multiprocessing
import os
import shutil
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path

import polars as pl

from render.cli import render
from render.faults import COMBOS, FAULTS, FaultConfig
from render.score import score
from render.windows import WindowConfig
from render.world import load

VERDICT_ORDER = ("exact", "correct", "loud", "SILENT", "ERROR")
# Matcher variants, each adding one change to the one before, so every change is measured
# against the same render. "balanced" is the change-3 advance-shaped checks used as the
# settle condition; "strict" never lets an arithmetic-only link settle.
MATCHERS = {
    "default": {},
    "before": {"contradiction_check": False, "category_gate": False, "strictness": "permissive", "basis_evidence": False},
    "+contradiction": {"contradiction_check": True, "category_gate": False, "strictness": "permissive",
                       "basis_evidence": False},
    "+category_gate": {"contradiction_check": True, "category_gate": True, "strictness": "permissive",
                       "basis_evidence": False},
    "balanced": {"contradiction_check": True, "category_gate": True, "strictness": "balanced", "basis_evidence": False},
    "strict": {"contradiction_check": True, "category_gate": True, "strictness": "strict", "basis_evidence": False},
    "current": {"category_gate": False, "basis_evidence": False},
    "basis": {"category_gate": False, "basis_evidence": True},
    "basis+gate": {"category_gate": True, "basis_evidence": True},
}
PARTIAL_FAULTS = ("partial_window", "combined")
_WORLDS: dict = {}


def _world(path: str):
    if path not in _WORLDS:
        _WORLDS[path] = load(path)
    return _WORLDS[path]


def run_one(world_dir: str, work: str, wcfg: WindowConfig, fcfg: FaultConfig, closed_loop: bool = False,
            matchers: tuple[str, ...] = ("default",)) -> dict[str, dict]:
    """Render once, score once per matcher variant."""
    tag = abs(hash((world_dir, closed_loop, tuple(sorted(asdict(fcfg).items())))))
    out = Path(work) / f"run-{os.getpid()}-{tag:x}"
    try:
        render(world_dir, out, wcfg, fcfg, world=_world(world_dir), closed_loop=closed_loop)
        return {mv: _row(score(out, MATCHERS[mv])) for mv in matchers}
    except Exception as e:  # a crash in render or scoring is reported, not hidden
        return {mv: {"verdict": "ERROR", "error": f"{type(e).__name__}: {e}"[:300]} for mv in matchers}
    finally:
        shutil.rmtree(out, ignore_errors=True)


def _row(rep: dict) -> dict:
    s, inv = rep["stress"], rep["tables"].get("ledger.invoices") or {}
    nm = rep["named_mismatches"]
    return {
        "verdict": s["verdict"], "pass": rep["pass"], "error": None,
        "transactions_exact_share": round(s["transactions_exact_share"], 4),
        "rupees_reconciled_share": round(s["rupees_reconciled_share"], 4),
        "invoices_exact_share": None if s["invoices_exact_share"] is None else round(s["invoices_exact_share"], 4),
        "continuity_failures": s["continuity_failures"], "quarantined_rows": s["quarantined_rows"],
        "unverified_payloads": s["unverified_payloads"],
        "invoice_rejects": sum((inv.get("rejects") or {}).values()),
        "silent_total": s["silent_total"], "loud_total": s["loud_total"],
        "extra_rows_silent": s["extra_rows_silent"], "missing_rows_silent": s["missing_rows_silent"],
        "balances_silent": s["balances_silent"], "invoices_silent": s["invoices_silent"],
        "wrong_facts": s["wrong_facts"], "extra_rows_flagged": s["extra_rows_flagged"],
        "missing_rows_loud": s["missing_rows_loud"], "balances_flagged": s["balances_flagged"],
        "identity_collisions": nm.get("identity_collisions", 0), "undetectable_partials": nm.get("undetectable_partials", 0),
        **_matching_cols(rep.get("matching")),
        **{f"key_{p}": n for p, n in s["key_paths"].items()}, "ledger_rows": s["ledger_rows"],
        "truth_rows": s["truth_rows"], "failed_checks": ",".join(s["failed_checks"]),
    }


def _matching_cols(mt: dict | None) -> dict:
    if not mt:
        return {}
    a, c = mt["a6"], mt["a6"].get("coincidental") or {}
    fate = c.get("real_payment_fate") or {}
    cov = a.get("review_coverage") or {}
    causes = a.get("review_causes") or {}
    return {
            "contradiction_events": a["contradiction_events"]["value"],
            "gated_by_category": a["credits_gated_by_category"]["value"],
            "review_items": cov.get("items"), "cover_top5": cov.get("top_5"), "cover_top10": cov.get("top_10"),
            "cover_top25": cov.get("top_25"),
            "review_causes": ";".join(f"{k}={v}" for k, v in causes.items()),
            "truth_paid_no_date": a["paid_no_date_open_label"]["value"] + a["paid_no_date_no_label"]["value"],"link_precision": round(a["precision"]["value"], 4), "links": a["precision"]["n"],
            "link_recall": round(a["recall"]["value"], 4), "review_share": round(a["review_share"]["value"], 4),
            "credits_considered": a["review_share"]["n"], "unexplained_rupees": round(a["unexplained_rupees"]["value"], 2),
            "wrong_links": c.get("wrong_links"), "wrong_from_coincidental": c.get("wrong_from_fault"),
            "injected": c.get("injected", 0), "injected_linked": c.get("injected_linked", 0),
            "injected_in_review": c.get("injected_in_review", 0),
            "injected_by_basis": (c.get("injected_review_causes") or {}).get("basis evidence", 0),
            "injected_by_contradiction": (c.get("injected_review_causes") or {}).get("settled-invoice contradiction", 0),
            "injected_zero_tds": c.get("injected_zero_tds", 0), "real_flagged_by_basis": c.get("real_flagged_by_basis", 0),
            "real_payment_review": fate.get("review", 0), "real_payment_other_invoice": fate.get("linked_to_another_invoice", 0),
            "real_payment_unmatched": fate.get("unmatched", 0), "paid_err_mean": round(a["paid_err_full_mean"]["value"], 3),
            "paid_truth": a["paid_truth"]["n"]}


def _task(args):
    world_dir, work, wcfg, fcfg, closed, matchers = args
    return run_one(world_dir, work, wcfg, fcfg, closed, matchers)


def sweep(worlds: list[str], levels: list[int], seeds: list[int], faults: list[str],
          modes: tuple[str, ...] = ("batch_fails",), wcfg: WindowConfig | None = None, work: Path | None = None,
          workers: int | None = None, loops: tuple[str, ...] = ("open",), combos: bool = True,
          matchers: tuple[str, ...] = ("default",), fault_extra: dict | None = None) -> pl.DataFrame:
    wcfg = wcfg or WindowConfig()
    work = work or Path.home() / ".cache" / "matsya-sweep-work"
    work.mkdir(parents=True, exist_ok=True)
    plan, keys = [], {}
    for loop in loops:
        for mode in modes:
            pool = [*faults, *COMBOS] if combos else list(faults)
            names = pool if mode == "batch_fails" else [f for f in pool if f in PARTIAL_FAULTS]
            for world in worlds:
                for fault in names:
                    for level in levels:
                        for seed in seeds:
                            fcfg = FaultConfig.at(level / 100, only=None if fault == "combined" else fault, seed=seed,
                                                  partial_mode=mode, **(fault_extra or {}))
                            key = (loop, str(world), tuple(sorted(asdict(fcfg).items())))
                            keys.setdefault(key, (str(world), str(work), wcfg, fcfg, loop == "closed", matchers))
                            plan.append({"loop": loop, "mode": mode, "world": Path(world).name, "fault": fault,
                                         "level_pct": level, "seed": seed, "_key": key})
    unique = list(keys)
    # spawn, not fork: forking a process that has started Polars' thread pool can deadlock.
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers or min(12, os.cpu_count() or 1), mp_context=ctx) as ex:
        results = dict(zip(unique, ex.map(_task, [keys[k] for k in unique], chunksize=2)))
    rows = [{**{k: v for k, v in p.items() if k != "_key"}, "matcher": mv, **res}
            for p in plan for mv, res in results[p["_key"]].items()]
    return pl.DataFrame(rows, infer_schema_length=None).sort("matcher", "loop", "mode", "fault", "level_pct", "world",
                                                                 "seed", maintain_order=True)


def _spread(values) -> str:
    v = [x for x in values if x is not None]
    if not v:
        return "-"
    lo, hi = min(v), max(v)
    fmt = (lambda x: f"{x:.3f}") if isinstance(lo, float) else str
    return fmt(lo) if lo == hi else f"{fmt(lo)}-{fmt(hi)}"


def summarise(runs: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    order = {f: i for i, f in enumerate([*FAULTS, *COMBOS])}
    levels = []
    for (loop, mode, fault, level), g in runs.group_by("loop", "mode", "fault", "level_pct", maintain_order=True):
        counts = {v: g.filter(pl.col("verdict") == v).height for v in VERDICT_ORDER}
        status = ("SILENT" if counts["SILENT"] else "ERROR" if counts["ERROR"] else
                  "loud (correct)" if counts["loud"] else "exact" if counts["exact"] == g.height else "correct")
        col = lambda c: g[c].to_list() if c in g.columns else []  # noqa: E731
        if loop == "open" and status == "loud (correct)" and g["continuity_failures"].max() > 0:
            status = "loud (artifact?)"  # open loop cannot re-fetch after an unexpected refusal
        levels.append({
            "loop": loop, "mode": mode, "fault": fault, "level_pct": level, "status": status,
            "verdicts": " ".join(f"{v}:{n}" for v, n in counts.items() if n),
            "txn_exact": _spread(col("transactions_exact_share")), "rupees_rec": _spread(col("rupees_reconciled_share")),
            "inv_exact": _spread(col("invoices_exact_share")), "cont_fail": _spread(col("continuity_failures")),
            "quarant": _spread(col("quarantined_rows")), "unverif": _spread(col("unverified_payloads")),
            "silent": _spread(col("silent_total")),
            "loud": _spread(col("loud_total")),
            "paths_txn/utr/bal/ord": "/".join(_spread(col(f"key_{p}")) for p in ("txn", "utr", "bal", "ord")),
        })
    levels_df = pl.DataFrame(levels).with_columns(
        pl.col("fault").replace_strict(order, return_dtype=pl.Int32).alias("_o")).sort("loop", "mode", "_o", "level_pct").drop("_o")
    thresholds = []
    for (loop, mode, fault), g in runs.group_by("loop", "mode", "fault", maintain_order=True):
        per_run = []
        for _, s in g.sort("level_pct").group_by("world", "seed", maintain_order=True):
            lv, v = s["level_pct"].to_list(), s["verdict"].to_list()
            per_run.append((next((l for l, x in zip(lv, v) if x != "exact"), None),
                            next((l for l, x in zip(lv, v) if x == "SILENT"), None)))

        def stat(i):
            vals = [t[i] for t in per_run]
            hit = [x for x in vals if x is not None]
            if not hit:
                return "never"
            note = "" if len(hit) == len(vals) else f" ({len(vals) - len(hit)}/{len(vals)} runs never)"
            return f"{min(hit)}/{statistics.median(hit):g}/{max(hit)}{note}"

        thresholds.append({"loop": loop, "mode": mode, "fault": fault, "not_exact_at_pct min/med/max": stat(0),
                           "SILENT_at_pct min/med/max": stat(1)})
    thr = pl.DataFrame(thresholds).with_columns(
        pl.col("fault").replace_strict(order, return_dtype=pl.Int32).alias("_o")).sort("loop", "mode", "_o").drop("_o")
    return levels_df, thr


def main_sweep(a) -> None:
    levels = [int(x) for x in a.levels.split(",")]
    seeds = [int(x) for x in a.seeds.split(",")]
    faults = list(FAULTS) if a.faults == "all" else a.faults.split(",")
    worlds = a.world_dir.split(",")
    modes = tuple(a.modes.split(","))
    loops = tuple(a.loops.split(","))
    runs = sweep(worlds, levels, seeds, faults, modes, loops=loops, combos=not a.no_combos,
                 matchers=tuple(a.matchers.split(",")),
                 fault_extra=dict(kv.split("=", 1) for kv in a.fault_extra) if a.fault_extra else None)
    levels_df, thresholds = summarise(runs)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    runs.write_csv(out / "stress_sweep.csv")
    thresholds.write_csv(out / "stress_thresholds.csv")
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=260, fmt_str_lengths=60, tbl_hide_dataframe_shape=True,
                   tbl_hide_column_data_types=True):
        print(f"stress sweep: worlds {[Path(w).name for w in worlds]}, levels {levels} %, seeds {seeds}, "
              f"modes {modes}, loops {loops}")
        print("status: exact = every run exact; correct = ledger right, flags or timing differ; "
              "loud (correct) = wrong but flagged or refused; SILENT = wrong and unflagged;\n"
              "        loud (artifact?) = open loop with refusals: A2 cannot re-fetch there, so some of that "
              "loudness is the harness, not the pipeline (compare the closed-loop row)")
        print(levels_df)
        print("\nthresholds (percent, over every world x seed):")
        print(thresholds)
    errors = runs.filter(pl.col("verdict") == "ERROR")
    if errors.height:
        print(f"\n{errors.height} run(s) errored:")
        for r in errors.select("mode", "world", "fault", "level_pct", "seed", "error").unique().iter_rows():
            print(f"  {r}")
    print(f"\nwrote {out / 'stress_sweep.csv'} and {out / 'stress_thresholds.csv'}")
