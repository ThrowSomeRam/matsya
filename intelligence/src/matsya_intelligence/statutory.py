"""Legal and statutory numbers, read from the constants file (the single source).

Code that needs one of these calls the function here; none of them is written as a literal
anywhere else.
"""

from matsya_intelligence.synth.world import load_constants


def gst_rate() -> float:
    """Standard GST rate applied to a tenant's sales and purchases (one rate per tenant)."""
    return float(load_constants()["gst"]["standard_rate"]["value"])


def s15_days() -> tuple[int, int]:
    """MSMED Act s.15: (cap on agreed terms in writing, days when there is no agreement)."""
    s15 = load_constants()["msmed_act"]["section_15"]
    return int(s15["max_payment_days_with_agreement"]), int(s15["payment_days_without_agreement"])


def s15_deadline(terms_days: int, in_writing: bool) -> int:
    """Days from acceptance to the s.15 deadline: written terms capped at the maximum, else the default."""
    cap, no_agreement = s15_days()
    return min(terms_days, cap) if in_writing else no_agreement


def msmed_rate_multiplier() -> int:
    """MSMED Act s.16: interest at this multiple of the RBI bank rate."""
    return int(load_constants()["msmed_act"]["section_16"]["rate_multiplier"])


def msmed_classes() -> tuple[str, ...]:
    """Enterprise classes covered by MSMED s.15/16 and s.43B(h)."""
    return tuple(load_constants()["msmed_act"]["applies_to"])


def qrmp_day(state_group: int) -> int:
    """GSTR-3B day of the month after the quarter for QRMP filers, by state group."""
    return int(load_constants()["statutory_calendar"]["quarterly"]["gstr3b_qrmp"][f"state_group_{state_group}"])


def runway_p_short() -> float:
    """Runway = first day on which P(shortfall by that day) reaches this."""
    return float(load_constants()["simulation"]["runway_p_short"]["value"])

