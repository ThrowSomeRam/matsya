"""Combine labelling-function votes, weighting each function by its estimated accuracy.

One-coin Dawid-Skene, fitted by EM without any gold labels. Each function j
has an accuracy a_j: when it votes, it votes the true class with probability
a_j, otherwise one of the other K-1 classes uniformly. Abstains carry no
information. With class prior pi_k:

    E step: q_ik  propto  pi_k * prod_{j voted} (a_j if v_ij = k else (1 - a_j) / (K - 1))
    M step: a_j = (sum_{i voted} q_{i, v_ij} + s a0) / (#votes_j + s)

Started from majority vote. Rows with no votes get the prior.

Two guards against a known collapse. A function that is the only voter for a
rare class has no agreement evidence: its single vote multiplies that class by
a_j against (1 - a_j)/(K - 1) for the rest, which a skewed prior easily
outweighs (one rule for "rent" at a_j = 0.7 loses to a 0.6 prior on supplier
payments). The posterior then moves away, the M step lowers a_j, and it
spirals to the floor. So the class prior is uniform by default (pass
class_balance to override, as Snorkel does), and each a_j has a Beta prior
(mean a0, strength s) that holds it near a0 when evidence is thin.
"""

import numpy as np

from matsya_intelligence.categorise.lfs import ABSTAIN


class LabelModel:
    def __init__(self, n_classes: int, n_iter: int = 50, acc_prior_mean: float = 0.7,
                 acc_prior_strength: float = 10.0, acc_bounds: tuple[float, float] = (0.05, 0.99),
                 class_balance: np.ndarray | None = None):
        self.k = n_classes
        self.n_iter = n_iter
        self.a0, self.s = acc_prior_mean, acc_prior_strength
        self.acc_bounds = acc_bounds
        self.accuracy: np.ndarray | None = None
        self.prior = np.full(n_classes, 1.0 / n_classes) if class_balance is None else np.asarray(class_balance)

    def _posterior(self, votes: np.ndarray) -> np.ndarray:
        log_q = np.tile(np.log(self.prior), (votes.shape[0], 1))
        for j, a in enumerate(self.accuracy):
            v = votes[:, j]
            voted = v != ABSTAIN
            wrong = np.log((1 - a) / (self.k - 1))
            log_q[voted] += wrong
            log_q[np.flatnonzero(voted), v[voted]] += np.log(a) - wrong
        log_q -= log_q.max(axis=1, keepdims=True)
        q = np.exp(log_q)
        return q / q.sum(axis=1, keepdims=True)

    def fit(self, votes: np.ndarray) -> "LabelModel":
        n, m = votes.shape
        q = np.full((n, self.k), 1.0 / self.k)
        for i, row in enumerate(votes):
            v = row[row != ABSTAIN]
            if v.size:
                counts = np.bincount(v, minlength=self.k).astype(float)
                q[i] = counts / counts.sum()
        for _ in range(self.n_iter):
            acc = np.empty(m)
            for j in range(m):
                v = votes[:, j]
                voted = np.flatnonzero(v != ABSTAIN)
                acc[j] = (q[voted, v[voted]].sum() + self.s * self.a0) / (voted.size + self.s)
            self.accuracy = np.clip(acc, *self.acc_bounds)
            q = self._posterior(votes)
        return self

    def predict_proba(self, votes: np.ndarray) -> np.ndarray:
        if self.accuracy is None:
            raise RuntimeError("LabelModel not fitted")
        return self._posterior(votes)
