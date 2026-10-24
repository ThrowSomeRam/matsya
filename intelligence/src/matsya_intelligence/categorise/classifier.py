"""Character n-gram (2-5) TF-IDF + logistic regression over narration text.

Digit runs are collapsed to '#' (UTRs, RRNs and amounts are noise to an
n-gram model), and the text is prefixed with direction and an amount-size
token so the model can use them.
"""

import math
import re

import numpy as np
import polars as pl
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

from matsya_intelligence.categories import CATEGORIES

_DIGITS = re.compile(r"\d+")


def features(df: pl.DataFrame) -> list[str]:
    out = []
    for n, a in zip(df["narration_raw"], df["amount_paise"]):
        size = int(math.log10(abs(a) / 100 + 1))  # rupee order of magnitude
        out.append(f"{'CR' if a > 0 else 'DR'} AMT{size} {_DIGITS.sub('#', n.upper())}")
    return out


class NarrationClassifier:
    def __init__(self, C: float = 10.0):
        self.model = make_pipeline(
            TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2, sublinear_tf=True),
            LogisticRegression(C=C, max_iter=2000),
        )

    def fit(self, df: pl.DataFrame, labels: np.ndarray, weights: np.ndarray | None = None) -> "NarrationClassifier":
        self.model.fit(features(df), labels, logisticregression__sample_weight=weights)
        return self

    def predict_proba(self, df: pl.DataFrame) -> np.ndarray:
        """(n, len(CATEGORIES)) probabilities; classes never seen in training get 0."""
        proba = self.model.predict_proba(features(df))
        full = np.zeros((df.height, len(CATEGORIES)))
        full[:, self.model.classes_] = proba
        return full
