# eval/models

Model benchmarks for Track B. Every result ships with a baseline and an n.

## Real-data benchmarks (foreign datasets, benchmarks only)

Fetch the data first (`eval/data/SOURCES.md` has sources, licences and the owner's decision):

```bash
cd intelligence && uv sync --extra dev --extra bench
uv run python ../eval/data/download.py all
uv run python ../eval/models/kaggle_checks.py all     # payment timing, sales counts, synthetic vs real
uv run python ../eval/models/berka_checks.py          # outside check: 682 loans
```

`loaders.py` maps both datasets to contract tables (Berka per account at any as_of, for the
flow forecaster and calibration benchmarks later). Results go to `eval/results/results.parquet`
(one row per benchmark, model, metric and horizon, each with n, baseline, git sha) and a
markdown file per check.
