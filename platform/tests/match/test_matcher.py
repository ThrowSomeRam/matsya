import random
from datetime import date, datetime, time, timedelta, timezone

import polars as pl

from matsya_contracts import ids
from matsya_contracts import tables as T
from matsya_contracts.validate import frame, validate
from matsya_platform.ingest.invoices import fiscal_year
from matsya_platform.match.buyers import gstin_checksum, gstin_valid
from matsya_platform.match.config import MatchConfig
from matsya_platform.match.engine import match

UTC = timezone.utc
TENANT = "3b2f7c1e-9a4d-4e8b-b6c5-1d2e3f4a5b6c"
SUPPLIER = "29AAFCS1234K1Z2"
AS_OF = datetime(2025, 8, 31, 18, 29, tzinfo=UTC)
THROUGH = date(2025, 8, 30)


def gstin(pan: str, state: str = "27") -> str:
    first14 = f"{state}{pan}1Z"
    return first14 + gstin_checksum(first14)


def buyer(name: str, pan: str, btype: str = "listed") -> T.Buyer:
    return T.Buyer(buyer_id=ids.buyer_id(pan), id_basis="pan", pan=pan, gstin=gstin(pan), name=name, type=btype,
                   sector="textiles", sector_source="exchange", treds_mandated=True if btype == "cpse" else None)


ACME = buyer("ACME TEXTILES LTD", "ABCCA1234F")
BETA = buyer("BETA FOODS PVT LTD", "BBCCB5678G", "unlisted")
CPSE = buyer("BHARAT HEAVY CORP LTD", "CDCCC9012H", "cpse")
BUYERS = frame(T.Buyer, [ACME, BETA, CPSE])


def invoice(no: str, b: T.Buyer, issue: date, gross_rs: float, gst_rs: float, terms: int = 30) -> T.Invoice:
    return T.Invoice(tenant_id=TENANT, invoice_id=ids.invoice_id(TENANT, SUPPLIER, no, fiscal_year(issue)), irn=None,
                     buyer_id=b.buyer_id, issue_date=issue, due_date=issue + timedelta(days=terms), terms_days=terms,
                     terms_source="document_terms", gross_paise=round(gross_rs * 100), gst_paise=round(gst_rs * 100),
                     expected_tds_paise=None, status="open",
                     known_at=datetime.combine(issue, time(12), tzinfo=UTC))


def credit(n: int, d: date, amount_rs: float, narration: str) -> T.Transaction:
    return T.Transaction(tenant_id=TENANT, txn_uid=ids.hash32("credit", str(n)), account_id="ACC", value_date=d,
                         known_at=datetime.combine(d + timedelta(days=1), time(1), tzinfo=UTC),
                         amount_paise=round(amount_rs * 100), direction="credit", narration_raw=narration, source="aa",
                         dq_score=1.0, dup_group_id=None)


def run(credits, invoices, config=MatchConfig()):
    r = match(frame(T.Transaction, credits), frame(T.Invoice, invoices), BUYERS, None, tenant_id=TENANT,
              supplier_gstin=SUPPLIER, as_of=AS_OF, complete_through=THROUGH, config=config)
    validate(r.matches, T.Match)
    validate(r.outcomes, T.InvoiceOutcome)
    return r


def outcome(r, inv: T.Invoice) -> dict:
    return r.outcomes.filter(pl.col("invoice_id") == inv.invoice_id).row(0, named=True)


D = date(2025, 5, 1)


def test_two_percent_tds_credit_matches_its_invoice():
    inv = invoice("INV-1", ACME, D, 118000, 18000)  # taxable 1,00,000; 2% TDS = 2,000
    r = run([credit(1, D + timedelta(days=20), 116000, "BY NEFT/HDFCN2512000000001/ACME TEXTILES LTD/PAYMENT")], [inv])
    (m,) = r.matches.iter_rows(named=True)
    assert (m["allocated_paise"], m["tds_inferred_paise"], m["tds_rate"], m["tds_basis"]) == \
        (11600000, 200000, 0.02, "taxable_value")
    o = outcome(r, inv)
    assert o["paid_date"] == D + timedelta(days=20) and o["settlement_kind"] == "buyer" and o["censored_at"] is None


def test_credit_explicable_under_both_bases_still_matches_once():
    inv = invoice("INV-2", ACME, D, 100000, 0)  # no GST: 2% of taxable == 2% of gross
    a = run([credit(1, D + timedelta(days=10), 98000, "BY NEFT/X/ACME TEXTILES LTD/PAYMENT")], [inv])
    b = run([credit(1, D + timedelta(days=10), 98000, "BY NEFT/X/ACME TEXTILES LTD/PAYMENT")], [inv])
    (m,) = a.matches.iter_rows(named=True)
    assert m["tds_inferred_paise"] == 200000 and m["tds_basis"] == "taxable_value"
    assert a.matches.equals(b.matches) and not a.review


def test_part_payment_reaches_paid_only_on_the_second_credit():
    inv = invoice("INV-3", ACME, D, 118000, 18000)  # net after 2% TDS = 1,16,000
    c1 = credit(1, D + timedelta(days=15), 50000, "BY NEFT/X/ACME TEXTILES LTD/INV 3")
    c2 = credit(2, D + timedelta(days=40), 66000, "BY NEFT/Y/ACME TEXTILES LTD/PAYMENT")
    half = run([c1], [inv])
    o = outcome(half, inv)
    assert o["paid_date"] is None and o["first_paid_date"] == c1.value_date and o["censored_at"] == THROUGH
    assert 0 < o["paid_fraction"] < 1
    full = run([c1, c2], [inv])
    o = outcome(full, inv)
    assert (o["first_paid_date"], o["paid_date"], o["paid_fraction"]) == (c1.value_date, c2.value_date, 1.0)
    assert full.matches["tds_inferred_paise"].sum() == 200000


def test_one_credit_settles_two_invoices():
    a = invoice("INV-4", BETA, D, 50000, 0)
    b = invoice("INV-5", BETA, D + timedelta(days=3), 30000, 0)
    c = invoice("INV-6", BETA, D + timedelta(days=5), 44444, 0)
    r = run([credit(1, D + timedelta(days=30), 80000, "BY NEFT/X/BETA FOODS PVT LTD/PAYMENT")], [a, b, c])
    assert set(r.matches["invoice_id"]) == {a.invoice_id, b.invoice_id}
    assert r.links["stage"].unique().to_list() == ["bundle"]
    assert outcome(r, a)["paid_date"] == outcome(r, b)["paid_date"] == D + timedelta(days=30)
    assert outcome(r, c)["paid_date"] is None


def test_zero_percent_credit_at_exact_gross():
    inv = invoice("INV-7", BETA, D, 25000, 0)
    r = run([credit(1, D + timedelta(days=9), 25000, "BY NEFT/X/BETA FOODS PVT LTD/PAYMENT")], [inv])
    (m,) = r.matches.iter_rows(named=True)
    assert m["tds_rate"] == 0.0 and m["tds_inferred_paise"] == 0
    assert r.links["stage"].to_list() == ["exact"]


def test_two_same_value_invoices_go_to_review_not_guessed():
    a = invoice("INV-8", ACME, D, 59000, 9000)
    b = invoice("INV-9", ACME, D + timedelta(days=2), 59000, 9000)
    r = run([credit(1, D + timedelta(days=20), 58000, "BY NEFT/X/ACME TEXTILES LTD/PAYMENT")], [a, b])
    assert r.matches.height == 0
    (item,) = r.review
    assert "ambiguous" in item.reason and len(item.candidates) >= 2
    assert {item.candidates[0].invoice_ids[0], item.candidates[1].invoice_ids[0]} == {a.invoice_id, b.invoice_id}
    assert item.candidates[0].unexplained_paise == item.candidates[1].unexplained_paise == 0
    # Neither invoice gets a training label: not paid, not censored-open.
    assert set(r.outcomes["invoice_id"]).isdisjoint({a.invoice_id, b.invoice_id})
    assert r.excluded_invoices == {a.invoice_id, b.invoice_id}


def test_treds_discount_settles_short_of_gross_and_censors_at_discount_date():
    inv = invoice("INV-10", CPSE, D, 118000, 18000, terms=45)  # 10% TDS on taxable = 10,000; net 1,08,000
    disc = D + timedelta(days=4)
    r = run([credit(1, disc, 106380, "BY NEFT/ICICN2512100000009/RXIL TREDS/PAYMENT")], [inv])  # 1.5% fee
    (m,) = r.matches.iter_rows(named=True)
    assert m["payer_kind"] == "treds_financier" and m["allocated_paise"] == 10638000
    o = outcome(r, inv)
    assert o["settlement_kind"] == "treds_discount" and o["paid_date"] == disc and o["censored_at"] == disc
    assert o["paid_fraction"] == 1.0


def test_platform_pass_through_at_full_net_counts_as_buyer():
    inv = invoice("INV-11", CPSE, D, 118000, 18000, terms=30)
    r = run([credit(1, D + timedelta(days=35), 108000, "BY NEFT/X/INVOICEMART/PAYMENT")], [inv])
    (m,) = r.matches.iter_rows(named=True)
    assert m["payer_kind"] == "buyer"
    o = outcome(r, inv)
    assert o["settlement_kind"] == "buyer" and o["censored_at"] is None


def test_unmatched_invoice_stays_open_and_censored():
    inv = invoice("INV-12", ACME, D, 40000, 0)
    r = run([], [inv])
    o = outcome(r, inv)
    assert o["paid_date"] is None and o["first_paid_date"] is None and o["censored_at"] == THROUGH
    assert o["settlement_kind"] is None and o["paid_fraction"] == 0.0


def test_reference_identifies_the_invoice_among_equal_amounts():
    a = invoice("INV-00013", ACME, D, 59000, 9000)
    b = invoice("INV-00014", ACME, D + timedelta(days=2), 59000, 9000)
    r = run([credit(1, D + timedelta(days=20), 58000, "BY NEFT/X/ACME TEXTILES LTD/INV 00014")], [a, b])
    assert r.matches["invoice_id"].to_list() == [b.invoice_id] and r.links["stage"].to_list() == ["reference"]


def test_buyer_tds_habit_breaks_a_tie_on_the_second_pass():
    # First credit is unambiguous and teaches ACME's habit (2% on taxable value). The second
    # fits invoice x at 2% and invoice y at 10%: a tie without the habit.
    learn = invoice("INV-20", ACME, D, 118000, 18000)
    x = invoice("INV-21", ACME, D + timedelta(days=1), 118000, 18000)  # 2%: 1,16,000
    y = invoice("INV-22", ACME, D + timedelta(days=2), 128888.89, 0)  # 10% of gross 1,28,888.89: 1,16,000
    cs = [credit(1, D + timedelta(days=10), 116000, "BY NEFT/A/ACME TEXTILES LTD/INV 20"),
          credit(2, D + timedelta(days=20), 116000, "BY NEFT/B/ACME TEXTILES LTD/PAYMENT")]
    r = run(cs, [learn, x, y])
    assert set(r.matches["invoice_id"]) >= {learn.invoice_id, x.invoice_id}


def test_same_input_any_order_gives_identical_output():
    invs = [invoice(f"INV-{k}", [ACME, BETA, CPSE][k % 3], D + timedelta(days=k), 10000 + 1000 * k, 1000 * (k % 2))
            for k in range(30, 42)]
    cs = []
    for k, inv in enumerate(invs):
        name = {ACME.buyer_id: ACME.name, BETA.buyer_id: BETA.name, CPSE.buyer_id: CPSE.name}[inv.buyer_id]
        taxable = inv.gross_paise - inv.gst_paise
        cs.append(credit(k, inv.issue_date + timedelta(days=12), (inv.gross_paise - round(0.02 * taxable)) / 100,
                         f"BY NEFT/U{k}/{name}/PAYMENT"))
    base = run(cs, invs)
    rng = random.Random(7)
    for _ in range(3):
        c2, i2 = cs[:], invs[:]
        rng.shuffle(c2)
        rng.shuffle(i2)
        other = run(c2, i2)
        assert other.matches.equals(base.matches) and other.outcomes.equals(base.outcomes)
        assert [x.txn_uid for x in other.review] == [x.txn_uid for x in base.review]


def test_gstin_checksum_validation():
    good = gstin("ABCCA1234F")
    bad = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert gstin_valid(good) and not gstin_valid(bad) and not gstin_valid("27ABCCA1234F")


def test_part_payment_without_reference_goes_to_review_even_with_one_candidate():
    inv = invoice("INV-30", ACME, D, 118000, 18000)
    r = run([credit(1, D + timedelta(days=15), 50000, "BY NEFT/X/ACME TEXTILES LTD/PAYMENT")], [inv])
    assert r.matches.height == 0
    (item,) = r.review
    assert item.reason.startswith("part-payment without reference") and inv.invoice_id in r.excluded_invoices


def test_bundle_with_two_fitting_subsets_goes_to_review():
    a = invoice("INV-31", BETA, D, 50000, 0)
    b = invoice("INV-32", BETA, D + timedelta(days=1), 30000, 0)
    c = invoice("INV-33", BETA, D + timedelta(days=2), 30000, 0)
    r = run([credit(1, D + timedelta(days=20), 80000, "BY NEFT/X/BETA FOODS PVT LTD/PAYMENT")], [a, b, c])
    assert r.matches.height == 0
    (item,) = r.review
    assert item.reason.startswith("ambiguous bundle: 2 invoice subsets")


def test_platform_full_net_before_due_date_is_the_buyer():
    inv = invoice("INV-34", CPSE, D, 118000, 18000, terms=45)
    r = run([credit(1, D + timedelta(days=10), 108000, "BY NEFT/X/RXIL TREDS/PAYMENT")], [inv])
    (m,) = r.matches.iter_rows(named=True)
    assert m["payer_kind"] == "buyer"
    o = outcome(r, inv)
    assert o["settlement_kind"] == "buyer" and o["censored_at"] is None


def run_at(credits, invoices, as_of, config=MatchConfig(), categories=None):
    r = match(frame(T.Transaction, credits), frame(T.Invoice, invoices), BUYERS, None, tenant_id=TENANT,
              supplier_gstin=SUPPLIER, as_of=as_of, complete_through=as_of.date() - timedelta(days=1), config=config,
              txn_category=categories)
    validate(r.matches, T.Match)
    validate(r.outcomes, T.InvoiceOutcome)
    return r


def end_of(d: date) -> datetime:
    return datetime.combine(d, time(18, 29), tzinfo=UTC)


def test_referenced_payment_displaces_an_earlier_arithmetic_link():
    inv = invoice("INV-40", ACME, D, 118000, 18000)  # 2% TDS: net 1,16,000
    advance = credit(1, D + timedelta(days=5), 116000, "BY NEFT/A/ACME TEXTILES LTD/PAYMENT")
    real = credit(2, D + timedelta(days=30), 116000, "BY NEFT/B/ACME TEXTILES LTD/INV 40")
    before = run_at([advance], [inv], end_of(D + timedelta(days=10)))
    assert before.matches["txn_uid"].to_list() == [advance.txn_uid] and not before.events
    after = run_at([advance, real], [inv], end_of(D + timedelta(days=40)))
    assert after.matches["txn_uid"].to_list() == [real.txn_uid]
    (item,) = after.review
    assert item.txn_uid == advance.txn_uid and item.reason.startswith("settled-invoice contradiction")
    (ev,) = after.events
    assert ev["kind"] == "settled_invoice_contradiction"
    assert (ev["displaced_txn_uid"], ev["invoice_id"], ev["referenced_txn_uid"]) == \
        (advance.txn_uid, inv.invoice_id, real.txn_uid)


def test_contradiction_check_off_keeps_the_old_reason():
    inv = invoice("INV-41", ACME, D, 118000, 18000)
    cs = [credit(1, D + timedelta(days=5), 116000, "BY NEFT/A/ACME TEXTILES LTD/PAYMENT"),
          credit(2, D + timedelta(days=30), 116000, "BY NEFT/B/ACME TEXTILES LTD/INV 41")]
    r = run_at(cs, [inv], end_of(D + timedelta(days=40)), MatchConfig(contradiction_check=False))
    assert not r.events and not r.review[0].reason.startswith("settled-invoice")


def test_label_evidence_marks_referenced_and_arithmetic_only():
    a = invoice("INV-42", ACME, D, 59000, 9000)
    b = invoice("INV-43", BETA, D, 25000, 0)
    r = run([credit(1, D + timedelta(days=20), 58000, "BY NEFT/X/ACME TEXTILES LTD/INV 42"),
             credit(2, D + timedelta(days=20), 25000, "BY NEFT/Y/BETA FOODS PVT LTD/PAYMENT")], [a, b])
    assert r.label_evidence == {a.invoice_id: "referenced", b.invoice_id: "arithmetic_no_basis_signal"}  # 0% TDS
    methods = dict(r.matches.select("invoice_id", "method").iter_rows())
    assert methods == {a.invoice_id: "reference", b.invoice_id: "amount_window"}


def test_balanced_sends_advance_shaped_fits_to_review_and_settles_the_rest():
    early = invoice("INV-44", BETA, D, 25000.37, 0, terms=45)
    near = invoice("INV-45", ACME, D, 118000.50, 18000, terms=30)  # 2% of 1,00,000.50 = 2,000.01
    cs = [credit(1, D + timedelta(days=3), 25000.37, "BY NEFT/X/BETA FOODS PVT LTD/PAYMENT"),  # 42 days before due
          credit(2, D + timedelta(days=28), 116000.49, "BY NEFT/Y/ACME TEXTILES LTD/PAYMENT")]  # 2 days before due
    r = run(cs, [early, near], MatchConfig(strictness="balanced"))
    assert r.matches["invoice_id"].to_list() == [near.invoice_id]
    (item,) = r.review
    assert item.reason.startswith("advance-shaped") and "before the due date" in item.reason


def test_balanced_flags_round_figures():
    inv = invoice("INV-46", BETA, D, 50000, 0, terms=30)
    r = run([credit(1, D + timedelta(days=29), 50000, "BY NEFT/X/BETA FOODS PVT LTD/PAYMENT")], [inv],
            MatchConfig(strictness="balanced"))
    assert r.matches.height == 0 and "round-figure" in r.review[0].reason


def test_strict_never_settles_on_arithmetic_alone():
    a = invoice("INV-47", ACME, D, 59000, 9000)
    b = invoice("INV-48", BETA, D, 25000, 0)
    r = run([credit(1, D + timedelta(days=20), 58000, "BY NEFT/X/ACME TEXTILES LTD/INV 47"),
             credit(2, D + timedelta(days=29), 25000, "BY NEFT/Y/BETA FOODS PVT LTD/PAYMENT")], [a, b],
            MatchConfig(strictness="strict"))
    assert r.matches["invoice_id"].to_list() == [a.invoice_id]
    assert r.review[0].reason.startswith("strict")


def _category(c: T.Transaction, cat: str, conf: float) -> T.TxnCategoryRow:
    return T.TxnCategoryRow(tenant_id=TENANT, txn_uid=c.txn_uid, category=cat, confidence=conf, source="model",
                            needs_llm=cat == "abstain")


def test_category_gate_removes_confident_non_receipts_and_falls_back_otherwise():
    inv = invoice("INV-49", BETA, D, 25000, 0)
    refund = credit(1, D + timedelta(days=20), 25000, "BY NEFT/X/BETA FOODS PVT LTD/PAYMENT")
    for cat, conf, matched in (("refund", 0.9, False), ("refund", 0.5, True), ("abstain", 0.3, True),
                               ("customer_receipt", 0.95, True)):
        cats = frame(T.TxnCategoryRow, [_category(refund, cat, conf)])
        r = run_at([refund], [inv], AS_OF, categories=cats)
        assert (r.matches.height == 1) is matched, (cat, conf)
        assert r.stats["credits_gated_by_category"] == (0 if matched else 1)


def test_review_reports_ties_and_ranks_by_risk():
    a = invoice("INV-50", ACME, D, 59000, 9000)
    b = invoice("INV-51", ACME, D + timedelta(days=2), 59000, 9000)
    small = invoice("INV-52", BETA, D, 10000, 0)
    cs = [credit(1, D + timedelta(days=20), 58000, "BY NEFT/X/ACME TEXTILES LTD/PAYMENT"),
          credit(2, D + timedelta(days=21), 4000, "BY NEFT/Y/BETA FOODS PVT LTD/PAYMENT")]  # part-payment: review
    r = run(cs, [a, b, small])
    top, second = r.review_ranked
    assert top["txn_uid"] == cs[0].txn_uid and top["risk"] > second["risk"]
    assert top["indistinguishable"] and [c["rank"] for c in top["candidates"][:2]] == [1, 1]
    assert {c["invoice_ids"][0] for c in top["candidates"][:2]} == {a.invoice_id, b.invoice_id}
    assert all(k in top["candidates"][0] for k in ("tds_rate", "tds_basis", "days_vs_due", "habit_match",
                                                   "unexplained_paise"))
    assert r.review_coverage["top_5"] == 1.0 and r.review_coverage["items"] == 2


def test_review_groups_items_sharing_a_buyer_and_cause():
    invs = [invoice(f"INV-6{k}", BETA, D + timedelta(days=k), 50000 + 1000 * k, 0) for k in range(3)]
    cs = [credit(k, D + timedelta(days=20 + k), 1000 + k, "BY NEFT/X/BETA FOODS PVT LTD/PAYMENT") for k in range(3)]
    r = run(cs, invs)
    (g,) = r.review_groups
    assert g["one_decision"] and len(g["items"]) == 3 and g["cause"] == "part-payment without reference"


BASIS = MatchConfig(basis_evidence=True)


def _habit_and_target():
    """ACME pays two invoices at 2% on taxable value (its habit), then a third invoice is open."""
    paid1 = invoice("INV-70", ACME, D, 118000, 18000)
    paid2 = invoice("INV-71", ACME, D + timedelta(days=1), 59000, 9000)
    target = invoice("INV-72", ACME, D + timedelta(days=2), 236000.10, 36000)
    habit = [credit(1, D + timedelta(days=25), 116000, "BY NEFT/A/ACME TEXTILES LTD/INV 70"),
             credit(2, D + timedelta(days=26), 58000, "BY NEFT/B/ACME TEXTILES LTD/INV 71")]
    return paid1, paid2, target, habit


def test_basis_evidence_catches_a_gross_basis_advance():
    paid1, paid2, target, habit = _habit_and_target()
    gross_net = target.gross_paise - round(0.02 * target.gross_paise)  # 2% on gross: how an advance is taxed
    adv = credit(3, D + timedelta(days=5), gross_net / 100, "BY NEFT/C/ACME TEXTILES LTD/PAYMENT")
    off = run(habit + [adv], [paid1, paid2, target], MatchConfig(basis_evidence=False))
    assert target.invoice_id in set(off.matches["invoice_id"])  # without the flag it settles the invoice
    on = run(habit + [adv], [paid1, paid2, target], BASIS)
    assert target.invoice_id not in set(on.matches["invoice_id"])
    (item,) = [i for i in on.review if i.txn_uid == adv.txn_uid]
    assert item.reason.startswith("basis evidence")


def test_basis_evidence_leaves_a_taxable_basis_payment_alone():
    paid1, paid2, target, habit = _habit_and_target()
    net = target.gross_paise - round(0.02 * (target.gross_paise - target.gst_paise))
    pay = credit(3, D + timedelta(days=28), net / 100, "BY NEFT/C/ACME TEXTILES LTD/PAYMENT")
    r = run(habit + [pay], [paid1, paid2, target], BASIS)
    assert target.invoice_id in set(r.matches["invoice_id"]) and not r.review


def test_zero_percent_credit_gives_no_basis_signal():
    paid1, paid2, target, habit = _habit_and_target()
    pay = credit(3, D + timedelta(days=28), target.gross_paise / 100, "BY NEFT/C/ACME TEXTILES LTD/PAYMENT")
    r = run(habit + [pay], [paid1, paid2, target], BASIS)
    assert target.invoice_id in set(r.matches["invoice_id"]) and not r.review


def test_buyer_with_a_gross_basis_habit_is_not_flagged():
    invs = [invoice(f"INV-8{k}", BETA, D + timedelta(days=k), 59000 + 1000 * k, 9000 + 100 * k) for k in range(4)]
    cs = [credit(k, inv.issue_date + timedelta(days=20), (inv.gross_paise - round(0.02 * inv.gross_paise)) / 100,
                 "BY NEFT/X/BETA FOODS PVT LTD/PAYMENT") for k, inv in enumerate(invs)]
    r = run(cs, invs, BASIS)
    assert set(r.matches["invoice_id"]) == {i.invoice_id for i in invs} and not r.review
    assert set(r.matches["tds_basis"]) == {"gross"}


def test_zero_tds_review_item_says_the_basis_cannot_decide():
    inv = invoice("INV-90", BETA, D, 25000.37, 0)
    r = run([credit(1, D + timedelta(days=29), 25000.37, "BY NEFT/X/BETA FOODS PVT LTD/PAYMENT")], [inv],
            MatchConfig(strictness="strict"))
    (item,) = r.review
    assert "no TDS basis signal: at 0% TDS" in item.reason
    assert r.review_ranked[0]["candidates"][0]["basis_signal"] == "none_zero_tds"


def _gross_advance(no: str, gross: float, gst: float, rate: float = 0.001):
    inv = invoice(no, ACME, D, gross, gst)
    return inv, credit(1, D + timedelta(days=29), (inv.gross_paise - round(rate * inv.gross_paise)) / 100,
                       "BY NEFT/X/ACME TEXTILES LTD/PAYMENT")


def test_basis_gap_under_the_amount_tolerance_is_still_read():
    # 0.1% of GST Rs 1,705.62 is Rs 1.70: inside the Rs 2 amount tolerance, far outside the
    # 5 paise basis tolerance. The sweep's 3 uncaught advances were exactly this invoice.
    inv, adv = _gross_advance("INV-91", 11181.26, 1705.62)
    r = run([adv], [inv], MatchConfig(strictness="strict"))
    assert r.review_ranked[0]["candidates"][0]["basis_signal"] == "gross_only"
    paid1, paid2, _, habit = _habit_and_target()
    caught = run(habit + [adv], [paid1, paid2, inv], BASIS)
    assert inv.invoice_id not in set(caught.matches["invoice_id"])
    assert any(i.txn_uid == adv.txn_uid and i.reason.startswith("basis evidence") for i in caught.review)


def test_bases_within_the_basis_tolerance_give_no_signal():
    inv, adv = _gross_advance("INV-92", 1000.40, 40.00)  # 0.1% of Rs 40 GST = 4 paise
    r = run([adv], [inv], MatchConfig(strictness="strict"))
    (item,) = r.review
    assert "within the basis tolerance" in item.reason
    assert r.review_ranked[0]["candidates"][0]["basis_signal"] == "none_within_tolerance"


def test_label_marks_a_link_with_no_basis_signal_as_weaker():
    zero = invoice("INV-93", BETA, D, 25000.37, 0)
    taxed = invoice("INV-94", ACME, D, 118000.50, 18000)
    r = run([credit(1, D + timedelta(days=29), 25000.37, "BY NEFT/X/BETA FOODS PVT LTD/PAYMENT"),
             credit(2, D + timedelta(days=29), 116000.49, "BY NEFT/Y/ACME TEXTILES LTD/PAYMENT")], [zero, taxed])
    assert r.label_evidence == {zero.invoice_id: "arithmetic_no_basis_signal", taxed.invoice_id: "arithmetic_only"}
    assert {zero.invoice_id, taxed.invoice_id} <= set(r.matches["invoice_id"])  # weaker, still settled


def test_basis_evidence_is_on_by_default():
    assert MatchConfig().basis_evidence is True
