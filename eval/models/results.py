"""The eval results table: one long-format Parquet, every row with its n and baseline.

eval/results/results.parquet holds the latest value per (benchmark, dataset, model, metric,
horizon). Each benchmark also writes a markdown summary next to it. Rows record the git
commit and date of the run, so a number can be traced back to the code that made it.
"""

import subprocess
from datetime import date
from pathlib import Path

import polars as pl

RESULTS = Path(__file__).resolve().parents[1] / "results"
SCHEMA = {
    "benchmark": pl.String, "dataset": pl.String, "component": pl.String, "model": pl.String,
    "metric": pl.String, "horizon": pl.String, "value": pl.Float64, "ci_low": pl.Float64,
    "ci_high": pl.Float64, "n": pl.Int64, "baseline": pl.String, "note": pl.String,
    "run_date": pl.Date, "git_sha": pl.String,
}
KEY = ["benchmark", "dataset", "model", "metric", "horizon"]


def _sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=RESULTS).stdout.strip()
    except OSError:
        return ""


def row(benchmark, dataset, component, model, metric, value, n, baseline, horizon="", ci=(None, None), note=""):
    return {"benchmark": benchmark, "dataset": dataset, "component": component, "model": model, "metric": metric,
            "horizon": str(horizon), "value": None if value is None else float(value),
            "ci_low": None if ci[0] is None else float(ci[0]), "ci_high": None if ci[1] is None else float(ci[1]),
            "n": int(n), "baseline": baseline, "note": note}


def write(rows: list[dict], markdown: str, name: str) -> pl.DataFrame:
    RESULTS.mkdir(parents=True, exist_ok=True)
    new = pl.DataFrame([{**r, "run_date": date.today(), "git_sha": _sha()} for r in rows], schema=SCHEMA)
    path = RESULTS / "results.parquet"
    if path.exists():
        old = pl.read_parquet(path)
        new = pl.concat([old.join(new.select(KEY), on=KEY, how="anti"), new])
    new = new.sort(KEY)
    new.write_parquet(path)
    (RESULTS / f"{name}.md").write_text(markdown)
    return new


def md_table(df: pl.DataFrame, floats: int = 3) -> str:
    cols = df.columns
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in df.iter_rows():
        out.append("| " + " | ".join(
            "" if v is None else (f"{v:.{floats}f}" if isinstance(v, float) else str(v)) for v in r) + " |")
    return "\n".join(out)
