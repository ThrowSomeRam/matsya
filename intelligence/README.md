# intelligence (Track B)

Writes only `features.*`, `forecast.*` and `decisions.*` (contracts v0.1). Reads Track A tables through
`matsya_contracts` only: `io/adapters.py` turns as-of reads of contract tables into each component's inputs,
and `io/sink.py` validates every output against its contract before writing.

```bash
cd intelligence && uv sync --extra dev && uv run pytest -m "not slow"
uv run python -m matsya_intelligence.run --out data/outputs   # every output table, one Parquet per table
```

Inputs come from `config/data_source.yaml`: `DATA_SOURCE: synth` (a generator world) or `contract`
(Parquet named `<namespace>.<table>.parquet`); `MATSYA_DATA_SOURCE` overrides it. A generator world is
published for Track A with `synth.export.write_contract_parquet(world, out_dir)` (truth tables in
`out_dir/truth/`).

Live-ish demo and learning-curve benchmark: `uv run python -m matsya_intelligence.synth.replay --days 30
--paths demo --out data/replay` steps as_of forward one day at a time through a generator world, reveals what
arrived, reruns the pipeline and scores each day's forecast against what happened.
