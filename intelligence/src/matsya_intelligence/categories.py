"""The transaction classes. Shared by the synthetic generator and the categoriser."""

CATEGORIES: tuple[str, ...] = (
    "customer_receipt",
    "supplier_payment",
    "salary",
    "rent",
    "emi",
    "professional_fee",
    "gst_payment",
    "tds_payment",
    "pf_esi",
    "bank_charges",
    "refund",
    "owner_transfer",
    "cash_withdrawal",
    "misc_income",
    "misc_expense",
)
