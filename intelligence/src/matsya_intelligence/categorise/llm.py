"""Interface for the zero-shot LLM on abstained transactions. No model is called yet.

Contract for an implementation:
- Input narrations must already have PII stripped (strip_pii below is the
  minimum: VPAs, account-like digit runs, person names are the caller's job).
- Return one category from CATEGORIES per narration, or None to stay abstained.
- The LLM only picks a label. It never produces or changes an amount.
"""

import re
from collections.abc import Callable, Sequence

LLMLabeller = Callable[[Sequence[str], Sequence[str]], list[str | None]]

_VPA = re.compile(r"[A-Za-z0-9._]+@[A-Za-z]+")
_LONG_DIGITS = re.compile(r"\d{6,}")


def strip_pii(narration: str) -> str:
    return _LONG_DIGITS.sub("#", _VPA.sub("<VPA>", narration))


def llm_label(narrations: Sequence[str], categories: Sequence[str]) -> list[str | None]:
    """Stub: abstains on everything until a local model is wired in (served by Track A)."""
    return [None] * len(narrations)
