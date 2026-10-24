"""Score the categoriser against synthetic truth, with template hold-out.

Train worlds use narration variant 0 and four bank codes. Two test sets:
  in_template: new worlds (other seeds), same variant and banks
  held_out:    new worlds with variant 1 and the other four banks
Baselines:
  rules_only:      labelling functions + majority vote (abstain on no votes)
  classifier_only: the same n-gram model trained on gold labels of the train
                   worlds (as if a hand-labelled set existed), no rules, no abstain
  weak_classifier: the pipeline's own classifier (trained on label-model
                   output) used alone, to show what it adds beyond the rules
Abstains count as wrong in macro-F1; the abstain share is reported separately.

Caveat: the labelling functions were written by someone who has seen both
template families, so held_out tests the classifiers' generalisation, not the
rules'. The real test for the rules is hand-labelled narrations from real banks.
"""

from dataclasses import replace

import numpy as np
import polars as pl
from sklearn.metrics import f1_score

from matsya_intelligence.categories import CATEGORIES
from matsya_intelligence.categorise.classifier import NarrationClassifier
from matsya_intelligence.categorise.lfs import ABSTAIN, apply_lfs, majority_vote
from matsya_intelligence.categorise.pipeline import ABSTAIN_LABEL, Categoriser
from matsya_intelligence.io.types import TXN
from matsya_intelligence.synth import WorldConfig, generate_world
from matsya_intelligence.synth.world import synth_tenant_id

TRAIN_STYLE = {"narration_variants": (0,), "narration_banks": ("HDFC", "ICIC", "SBIN", "UTIB")}
HELD_OUT_STYLE = {"narration_variants": (1,), "narration_banks": ("KKBK", "PUNB", "BARB", "CNRB")}


def build(style: dict, seeds, base: WorldConfig | None = None) -> tuple[pl.DataFrame, list[str]]:
    """Transactions (Txn columns) from several worlds, one tenant each, and their true categories."""
    base = base or WorldConfig()
    frames = []
    for s in seeds:
        cfg = replace(base, tenant_id=synth_tenant_id(f"t{s}"), **style)
        frames.append(generate_world(cfg, past_seed=s, future_seed=0)[1].transactions)
    all_txns = pl.concat(frames)
    return all_txns.select(list(TXN)), all_txns["category"].to_list()


def score(pred: list[str], truth: list[str]) -> dict:
    labels = sorted(set(truth))
    per_class = f1_score(truth, pred, labels=labels, average=None, zero_division=0)
    return {
        "macro_f1": float(np.mean(per_class)),
        "per_class": dict(zip(labels, map(float, per_class))),
        "abstain_share": float(np.mean([p == ABSTAIN_LABEL for p in pred])),
        "n": len(truth),
    }


def run(train_seeds=range(100, 103), test_seeds=range(200, 202), base: WorldConfig | None = None) -> dict:
    train, train_y = build(TRAIN_STYLE, train_seeds, base)
    tests = {
        "in_template": build(TRAIN_STYLE, test_seeds, base),
        "held_out": build(HELD_OUT_STYLE, test_seeds, base),
    }
    cat = Categoriser().fit(train)
    gold_clf = NarrationClassifier().fit(train, np.array([CATEGORIES.index(c) for c in train_y]))

    results = {"n_train": train.height}
    for name, (txns, y) in tests.items():
        rules = majority_vote(apply_lfs(txns))
        results[name] = {
            "pipeline": score(cat.predict(txns)["category"].to_list(), y),
            "rules_only": score([CATEGORIES[k] if k != ABSTAIN else ABSTAIN_LABEL for k in rules], y),
            "classifier_only": score([CATEGORIES[k] for k in gold_clf.predict_proba(txns).argmax(axis=1)], y),
            "weak_classifier": score([CATEGORIES[k] for k in cat.classifier.predict_proba(txns).argmax(axis=1)], y),
        }
    return results
