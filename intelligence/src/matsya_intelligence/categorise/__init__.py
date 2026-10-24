"""Categoriser: label every transaction with one of CATEGORIES.

Labelling functions -> label model -> char n-gram classifier, with an abstain
path for an LLM and per-tenant owner overrides that win over everything.
"""

from matsya_intelligence.categorise.pipeline import ABSTAIN_LABEL, Categoriser, add_override, empty_overrides

__all__ = ["ABSTAIN_LABEL", "Categoriser", "add_override", "empty_overrides"]
