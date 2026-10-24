"""The categoriser cascade: owner override > confident rules > classifier > abstain (LLM).

fit():   labelling functions -> label model (EM, no gold labels) -> training
         labels (+ owner corrections, which replace weak labels) -> classifier.
predict(): per transaction, in order of precedence:
    1. owner override for (tenant_id, txn_uid)           source "owner", confidence 1
    2. label model, if a function voted and posterior >= rule_threshold  source "rule"
    3. classifier, if its top probability >= model_threshold      source "model"
    4. otherwise ABSTAIN with needs_llm; an LLM labeller, if given, may fill it  source "llm"

Rules come before the classifier because the classifier is trained on the
rules' output: where the rules are confident it adds nothing, and on unseen
narration formats it can be confidently wrong. Its job is the rows the rules
do not cover.
"""

from dataclasses import dataclass, field

import numpy as np
import polars as pl

from matsya_intelligence.categories import CATEGORIES
from matsya_intelligence.categorise.classifier import NarrationClassifier
from matsya_intelligence.categorise.label_model import LabelModel
from matsya_intelligence.categorise.lfs import ABSTAIN, apply_lfs
from matsya_intelligence.categorise.llm import LLMLabeller, strip_pii
from matsya_intelligence.io.types import TXN_CATEGORY

ABSTAIN_LABEL = "abstain"

OVERRIDE_SCHEMA = {"tenant_id": pl.String, "txn_uid": pl.String, "category": pl.String}


def empty_overrides() -> pl.DataFrame:
    return pl.DataFrame(schema=OVERRIDE_SCHEMA)


def add_override(overrides: pl.DataFrame, tenant_id: str, txn_uid: str, category: str) -> pl.DataFrame:
    """Latest correction wins for a (tenant, txn)."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}")
    new = pl.DataFrame([{"tenant_id": tenant_id, "txn_uid": txn_uid, "category": category}], schema=OVERRIDE_SCHEMA)
    return pl.concat([overrides.filter(~((pl.col("tenant_id") == tenant_id) & (pl.col("txn_uid") == txn_uid))), new])


@dataclass
class Categoriser:
    model_threshold: float = 0.6
    rule_threshold: float = 0.5  # with one vote, the posterior equals that function's accuracy
    min_label_confidence: float = 0.5  # weak labels below this are not used for training
    owner_weight: float = 5.0  # sample weight of an owner correction vs a weak label (<= 1)
    label_model: LabelModel | None = field(default=None, repr=False)
    classifier: NarrationClassifier | None = field(default=None, repr=False)

    def training_set(self, txns: pl.DataFrame, overrides: pl.DataFrame | None = None,
                     votes: np.ndarray | None = None) -> pl.DataFrame:
        """txn_uid, tenant_id, category, weight, origin ("label_model" | "owner")."""
        votes = apply_lfs(txns) if votes is None else votes
        q = self.label_model.predict_proba(votes)
        keep = ((votes != ABSTAIN).any(axis=1)) & (q.max(axis=1) >= self.min_label_confidence)
        weak = pl.DataFrame({
            "tenant_id": txns["tenant_id"],
            "txn_uid": txns["txn_uid"],
            "category": [CATEGORIES[k] for k in q.argmax(axis=1)],
            "weight": q.max(axis=1),
            "origin": "label_model",
        }).filter(pl.Series(keep))
        if overrides is None or overrides.height == 0:
            return weak
        own = overrides.join(txns.select("tenant_id", "txn_uid"), on=["tenant_id", "txn_uid"]).with_columns(
            pl.lit(self.owner_weight).alias("weight"), pl.lit("owner").alias("origin"))
        weak = weak.join(own, on=["tenant_id", "txn_uid"], how="anti")
        return pl.concat([weak, own.select(weak.columns)])

    def fit(self, txns: pl.DataFrame, overrides: pl.DataFrame | None = None) -> "Categoriser":
        votes = apply_lfs(txns)
        self.label_model = LabelModel(len(CATEGORIES)).fit(votes)
        train = self.training_set(txns, overrides, votes)
        data = train.join(txns, on=["tenant_id", "txn_uid"], how="left")
        y = np.array([CATEGORIES.index(c) for c in data["category"]])
        self.classifier = NarrationClassifier().fit(data, y, data["weight"].to_numpy())
        return self

    def predict(self, txns: pl.DataFrame, overrides: pl.DataFrame | None = None,
                llm: LLMLabeller | None = None) -> pl.DataFrame:
        if self.classifier is None:
            raise RuntimeError("Categoriser not fitted")
        votes = apply_lfs(txns)
        q = self.label_model.predict_proba(votes)
        p = self.classifier.predict_proba(txns)
        m_cat, m_conf = p.argmax(axis=1), p.max(axis=1)
        r_cat, r_conf = q.argmax(axis=1), q.max(axis=1)
        has_vote = (votes != ABSTAIN).any(axis=1)

        use_rule = has_vote & (r_conf >= self.rule_threshold)
        use_model = ~use_rule & (m_conf >= self.model_threshold)
        category = np.where(use_rule, r_cat, np.where(use_model, m_cat, -1))
        conf = np.where(use_rule, r_conf, m_conf)
        labels = [CATEGORIES[k] if k >= 0 else ABSTAIN_LABEL for k in category]
        source = np.where(use_rule, "rule", "model").tolist()
        needs_llm = (category < 0).tolist()

        if llm is not None and any(needs_llm):
            idx = [i for i, need in enumerate(needs_llm) if need]
            answers = llm([strip_pii(txns["narration_raw"][i]) for i in idx], CATEGORIES)
            for i, a in zip(idx, answers):
                if a in CATEGORIES:
                    labels[i], source[i], needs_llm[i] = a, "llm", False

        out = pl.DataFrame({
            "tenant_id": txns["tenant_id"],
            "txn_uid": txns["txn_uid"],
            "category": labels,
            "confidence": conf.astype(float),
            "source": source,
            "needs_llm": needs_llm,
        }, schema=TXN_CATEGORY)

        if overrides is not None and overrides.height:
            out = out.join(overrides.rename({"category": "owner_cat"}), on=["tenant_id", "txn_uid"], how="left")
            owned = pl.col("owner_cat").is_not_null()
            out = out.with_columns(
                pl.when(owned).then(pl.col("owner_cat")).otherwise(pl.col("category")).alias("category"),
                pl.when(owned).then(1.0).otherwise(pl.col("confidence")).alias("confidence"),
                pl.when(owned).then(pl.lit("owner")).otherwise(pl.col("source")).alias("source"),
                pl.when(owned).then(False).otherwise(pl.col("needs_llm")).alias("needs_llm"),
            ).drop("owner_cat")
        return out
