"""Chronos-2 zero-shot quantiles for short histories (flow forecaster fallback).

Optional: needs the `chronos` extra (chronos-forecasting, CPU torch) and the amazon/chronos-2
weights. Import errors propagate so the caller can fall back to the bootstrap.
"""

from functools import cache

import numpy as np

MODEL = "amazon/chronos-2"


@cache
def _pipeline():
    from chronos import BaseChronosPipeline

    return BaseChronosPipeline.from_pretrained(MODEL, device_map="cpu")


def chronos_quantiles(series: np.ndarray, horizon: int, quantiles: np.ndarray) -> np.ndarray:
    """(horizon, len(quantiles)) forecast quantiles of a daily series, sorted per day."""
    import torch

    torch.manual_seed(0)  # Chronos-2 quantile heads are deterministic; this guards any sampling path
    q, _ = _pipeline().predict_quantiles(torch.tensor(np.asarray(series, dtype=np.float32))[None, None, :],
                                         prediction_length=horizon, quantile_levels=[float(x) for x in quantiles])
    return np.sort(np.asarray(q[0], dtype=float).reshape(horizon, len(quantiles)), axis=1)
