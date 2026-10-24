# intelligence (Track B)

Components B1 to B10. Writes only `features`, `forecast`, `decisions`, `ref.obligations`, `ref.buyer_priors`, `ops.calibration_state`. Reads Track A tables through `contracts/` only.

```bash
cd intelligence && pip install -e '.[dev]' && pytest
```
