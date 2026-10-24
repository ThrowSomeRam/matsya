"""Labelling functions: small rules that vote for a class or abstain.

Each function sees the whole transaction frame for a tenant (recurrence and
counterparty rules need history) and returns one vote per row: a class index
into CATEGORIES, or ABSTAIN. The rules encode general Indian banking
knowledge, not the generator's exact templates.
"""

import re
from collections.abc import Callable

import numpy as np
import polars as pl

from matsya_intelligence.categories import CATEGORIES

ABSTAIN = -1
IDX = {c: i for i, c in enumerate(CATEGORIES)}

GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
COMPANY_RE = re.compile(r"\b(PVT LTD|LTD|LIMITED|NIGAM|CORPORATION|LLP)\b")
NAME_SEGMENT_RE = re.compile(r"[A-Z][A-Z ]{4,}[A-Z]")

# Any explicit keyword; the structural rules (company, recurrence, counterparty)
# stay out of transactions that already carry one.
KEYWORDED = re.compile(
    r"CPIN|GSTN|ITNS|CHALLAN|OLTAS|TDS|EPFO|ESIC|\bEPF\b|\bESI\b|\bSAL\b|SALARY|ACH|NACH|\bEMI\b|CHG|CHRG|ATM|ATW"
    r"|\bSELF\b|OWN ACCOUNT|\bRENT\b|PROF|REFUND|REVERSAL|CASH WDL")


def _keyword(pattern: str, category: str, sign: int = 0) -> Callable[[pl.DataFrame], np.ndarray]:
    rx = re.compile(pattern)

    def lf(df: pl.DataFrame) -> np.ndarray:
        hit = np.array([bool(rx.search(n)) for n in df["narration_raw"]])
        if sign:
            hit &= np.sign(df["amount_paise"].to_numpy()) == sign
        return np.where(hit, IDX[category], ABSTAIN)

    lf.__name__ = f"kw_{category}"
    return lf


def rail(narration: str) -> str:
    n = narration
    if n.startswith(("UPI", "UPI/")):
        return "UPI"
    if "IMPS" in n[:9]:
        return "IMPS"
    if "RTGS" in n[:8]:
        return "RTGS"
    if n.startswith(("ACH", "NACH")):
        return "ACH"
    if n.startswith(("ATW", "CASH WDL")):
        return "ATM"
    if "CHG" in n or n.startswith("CHRG"):
        return "CHARGES"
    if "NEFT" in n[:8]:
        return "NEFT"
    return "OTHER"


def counterparty(narration: str) -> str | None:
    """Longest run of letters and spaces with at least two words; None if absent."""
    segments = [s.strip() for s in NAME_SEGMENT_RE.findall(narration) if " " in s.strip()]
    return max(segments, key=len) if segments else None


def lf_company_credit(df: pl.DataFrame) -> np.ndarray:
    amt = df["amount_paise"].to_numpy()
    hit = [a > 0 and rail(n) in ("NEFT", "RTGS") and bool(COMPANY_RE.search(n))
           for n, a in zip(df["narration_raw"], amt)]
    return np.where(hit, IDX["customer_receipt"], ABSTAIN)


def lf_company_debit(df: pl.DataFrame) -> np.ndarray:
    amt = df["amount_paise"].to_numpy()
    hit = [a < 0 and bool(COMPANY_RE.search(n)) and not KEYWORDED.search(n)
           for n, a in zip(df["narration_raw"], amt)]
    return np.where(hit, IDX["supplier_payment"], ABSTAIN)


def lf_gstin_debit(df: pl.DataFrame) -> np.ndarray:
    amt = df["amount_paise"].to_numpy()
    hit = [a < 0 and bool(GSTIN_RE.search(n)) for n, a in zip(df["narration_raw"], amt)]
    return np.where(hit, IDX["gst_payment"], ABSTAIN)


def lf_small_upi_credit(df: pl.DataFrame) -> np.ndarray:
    amt = df["amount_paise"].to_numpy()
    hit = [0 < a < 20_000_00 and rail(n) == "UPI" and "REFUND" not in n and not COMPANY_RE.search(n)
           for n, a in zip(df["narration_raw"], amt)]
    return np.where(hit, IDX["misc_income"], ABSTAIN)


def lf_small_upi_debit(df: pl.DataFrame) -> np.ndarray:
    amt = df["amount_paise"].to_numpy()
    hit = [-5_000_00 < a < 0 and rail(n) == "UPI" and not COMPANY_RE.search(n)
           for n, a in zip(df["narration_raw"], amt)]
    return np.where(hit, IDX["misc_expense"], ABSTAIN)


def lf_recurring_fixed_debit(df: pl.DataFrame) -> np.ndarray:
    """Same amount debited in 3+ distinct months on nearly the same day, with no
    statutory, salary, loan or charge keyword, and large: most likely rent."""
    votes = np.full(df.height, ABSTAIN)
    d = df.with_row_index("i").filter(pl.col("amount_paise") <= -50_000_00)
    stats = d.group_by("tenant_id", "amount_paise").agg(
        pl.col("value_date").dt.strftime("%Y-%m").n_unique().alias("months"),
        (pl.col("value_date").dt.day().max() - pl.col("value_date").dt.day().min()).alias("day_spread"),
        pl.col("i"),
    ).filter((pl.col("months") >= 3) & (pl.col("day_spread") <= 3))
    narr = df["narration_raw"]
    for rows in stats["i"]:
        for i in rows:
            if not KEYWORDED.search(narr[i]):
                votes[i] = IDX["rent"]
    return votes


def lf_repeat_counterparty_debit(df: pl.DataFrame) -> np.ndarray:
    """A counterparty paid 3+ times, varying amounts of Rs 10k or more, looks like a supplier."""
    votes = np.full(df.height, ABSTAIN)
    cps = [counterparty(n) for n in df["narration_raw"]]
    d = df.with_row_index("i").with_columns(pl.Series("cp", cps, dtype=pl.String)).filter(
        (pl.col("amount_paise") <= -10_000_00) & pl.col("cp").is_not_null())
    stats = d.group_by("tenant_id", "cp").agg(
        pl.len().alias("n"), pl.col("amount_paise").n_unique().alias("amounts"), pl.col("i"))
    narr = df["narration_raw"]
    for rows in stats.filter((pl.col("n") >= 3) & (pl.col("amounts") >= 3))["i"]:
        for i in rows:
            if not KEYWORDED.search(narr[i]):
                votes[i] = IDX["supplier_payment"]
    return votes


LABELLING_FUNCTIONS: list[Callable[[pl.DataFrame], np.ndarray]] = [
    _keyword(r"CPIN|GSTN", "gst_payment"),
    _keyword(r"ITNS ?281|CHALLAN 281|OLTAS|\bTDS\b", "tds_payment"),
    _keyword(r"EPFO|ESIC|\bEPF\b|\bESI\b|PROVIDENT", "pf_esi"),
    _keyword(r"\bSAL\b|SALARY|PAYROLL", "salary", sign=-1),
    _keyword(r"^ACH|^NACH|\bEMI\b|LOAN", "emi", sign=-1),
    _keyword(r"CHGS|CHRG|CHARGES|\bAMC\b|ANNUAL FEE", "bank_charges", sign=-1),
    _keyword(r"^ATW|\bATM\b|CASH WDL|CASH WITHDRAWAL", "cash_withdrawal", sign=-1),
    _keyword(r"\bSELF\b|OWN ACCOUNT|OWN A/C", "owner_transfer"),
    _keyword(r"\bRENT\b", "rent", sign=-1),
    _keyword(r"PROF|PROFESSIONAL|CONSULT|RETAINER", "professional_fee", sign=-1),
    _keyword(r"REFUND|REVERSAL|^REV", "refund", sign=1),
    _keyword(r"TREDS|M1XCHANGE|INVOICEMART|RXIL", "customer_receipt", sign=1),
    _keyword(r"\bINV ?\d|INVOICE", "customer_receipt", sign=1),
    _keyword(r"\bBILL ?\d|\bPO ?\d", "supplier_payment", sign=-1),
    lf_company_credit,
    lf_company_debit,
    lf_gstin_debit,
    lf_small_upi_credit,
    lf_small_upi_debit,
    lf_recurring_fixed_debit,
    lf_repeat_counterparty_debit,
]
LF_NAMES = [f.__name__ for f in LABELLING_FUNCTIONS]


def apply_lfs(df: pl.DataFrame) -> np.ndarray:
    """(n_txns, n_lfs) int votes; ABSTAIN where a function does not fire."""
    df = df.with_columns(pl.col("narration_raw").str.to_uppercase())
    return np.stack([f(df) for f in LABELLING_FUNCTIONS], axis=1).astype(np.int64)


def majority_vote(votes: np.ndarray, n_classes: int = len(CATEGORIES)) -> np.ndarray:
    """Baseline: most common non-abstain vote; ABSTAIN on no votes. Ties go to the lower index."""
    out = np.full(votes.shape[0], ABSTAIN)
    for i, row in enumerate(votes):
        v = row[row != ABSTAIN]
        if v.size:
            out[i] = int(np.bincount(v, minlength=n_classes).argmax())
    return out
