"""Obligations detector: statutory dates plus recurring fixed outflows."""

from matsya_intelligence.obligations.calendar import statutory_dates
from matsya_intelligence.obligations.detector import detect_obligations
from matsya_intelligence.obligations.recurrence import Recurrence, detect_recurrences

__all__ = ["Recurrence", "detect_obligations", "detect_recurrences", "statutory_dates"]
