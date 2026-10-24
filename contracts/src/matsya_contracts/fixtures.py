"""Deterministic fixtures: small, valid versions of every table. Same seed, same output.

Every enum member appears in its table where practical. IDs are consistent across tables
(matches point at real txns and invoices, outcomes at real invoices, and so on).

Usage: python -m matsya_contracts.fixtures OUT_DIR [--seed N]
Writes one Parquet file per table, named <namespace>.<table>.parquet.
"""

import argparse
import hashlib
import json
import random
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import polars as pl

from matsya_contracts import enums as E
from matsya_contracts import events as ev
from matsya_contracts import ids
from matsya_contracts import tables as T
from matsya_contracts.validate import frame

START = date(2026, 4, 1)
FY = "2026-27"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Sign of each category's synthetic flow: + credit, - debit.
CATEGORY_SIGN = {
    E.TxnCategory.CUSTOMER_RECEIPT: 1, E.TxnCategory.REFUND: 1, E.TxnCategory.MISC_INCOME: 1,
    E.TxnCategory.OWNER_TRANSFER: 1,
}


def _utc(d: date, hour: int = 6) -> datetime:
    return datetime.combine(d, time(hour), tzinfo=timezone.utc)


def _cycle(members, i):
    members = list(members)
    return members[i % len(members)]


class _Gen:
    def __init__(self, seed: int):
        self.r = random.Random(seed)
        self.rows: dict[str, list[T.Row]] = {t: [] for t in T.TABLES}

    def add(self, row: T.Row) -> T.Row:
        self.rows[row.table].append(row)
        return row

    def tenant(self) -> str:
        return str(uuid.UUID(int=self.r.getrandbits(128), version=4))

    def pan(self, kind: str = "C") -> str:
        r = self.r
        return "".join(r.choice(LETTERS) for _ in range(3)) + kind + r.choice(LETTERS) + \
            f"{r.randrange(10_000):04d}" + r.choice(LETTERS)

    def gstin(self, pan: str) -> str:
        return f"{self.r.randrange(1, 38):02d}{pan}1Z{self.r.choice(LETTERS)}"

    def sha(self, *parts) -> str:
        return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()

    def paise(self, lo_rupees: int, hi_rupees: int) -> int:
        return self.r.randrange(lo_rupees, hi_rupees) * 100


def _buyers(g: _Gen) -> list[T.Buyer]:
    out = []
    global_sources = [E.SectorSource.EXCHANGE, E.SectorSource.CIN_NIC]
    for i, sector in enumerate(E.Sector):
        pan = g.pan()
        btype = _cycle(E.BuyerType, i)
        unknown = sector is E.Sector.UNKNOWN
        out.append(g.add(T.Buyer(
            buyer_id=ids.buyer_id(pan), id_basis=E.BuyerIdBasis.PAN, pan=pan, gstin=g.gstin(pan) if i % 4 else None,
            name=f"{sector.value.replace('_', ' ').title()} Buyer {i} Ltd", type=btype, sector=sector,
            sector_source=E.SectorSource.UNKNOWN if unknown else _cycle(global_sources, i),
            treds_mandated=True if btype is E.BuyerType.CPSE else _cycle([True, False, None], i),
        )))
    return out


def _disclosures(g: _Gen, buyers: list[T.Buyer]) -> None:
    for i, b in enumerate(x for x in buyers if x.type is not E.BuyerType.UNLISTED):
        not_due = None if i % 3 == 1 else g.paise(0, 5_000_000)
        overdue = [g.paise(0, 2_000_000), g.paise(0, 500_000), g.paise(0, 100_000), g.paise(0, 50_000)]
        g.add(T.BuyerDisclosure(
            buyer_id=b.buyer_id, fiscal_year="2025-26", msme_not_due=not_due,
            msme_lt_1y=overdue[0], msme_1_2y=overdue[1], msme_2_3y=overdue[2], msme_gt_3y=overdue[3],
            msme_disputed=0 if i % 2 else g.paise(0, 10_000), interest_due_unpaid=g.paise(0, 20_000),
            msme_unbilled=g.paise(0, 300_000) if i % 3 == 2 else None,
            msme_total_outstanding=(not_due or 0) + sum(overdue) if i % 2 == 0 else None,
        ))


def _tenant(g: _Gen, t: int, buyers: list[T.Buyer]) -> None:
    tenant = g.tenant()
    account = f"ACC{t:02d}"
    supplier_gstin = g.gstin(g.pan("F"))

    # ops.consents: every status across tenants.
    for i, status in enumerate(E.ConsentStatus):
        if t == 0 or i % 2 == 0:
            g.add(T.Consent(
                tenant_id=tenant, consent_id=f"CON-{t}-{i}", fip_id=f"FIP{i % 3}", purpose="cash-flow forecast",
                range_from=START - timedelta(days=365), range_to=START + timedelta(days=90), frequency="daily",
                status=status, expires_at=_utc(START + timedelta(days=365)),
            ))

    payloads = []
    for p in range(2):
        payload_id = f"PL-{t}-{p}"
        payloads.append(payload_id)
        g.add(T.AaPayload(
            tenant_id=tenant, payload_id=payload_id, consent_id=f"CON-{t}-0", fip_id="FIP0",
            fetched_at=_utc(START + timedelta(days=30 * (p + 1))), object_uri=f"s3://raw/{tenant}/{payload_id}.json.enc",
            sha256=g.sha(tenant, payload_id),
        ))

    # Tenant's buyers: a slice of the global list plus one local (no-PAN) buyer.
    local_name = f"Sharma Traders {t}"
    local = g.add(T.Buyer(
        buyer_id=ids.local_buyer_id(tenant, local_name), id_basis=E.BuyerIdBasis.LOCAL, pan=None, gstin=None,
        name=local_name, type=E.BuyerType.UNLISTED, sector=E.Sector.UNKNOWN, sector_source=E.SectorSource.UNKNOWN,
        treds_mandated=None,
    ))
    mine = buyers[t * 5:t * 5 + 5] + [local]
    for i, b in enumerate(mine):
        g.add(T.TenantBuyer(
            tenant_id=tenant, buyer_id=b.buyer_id, display_name=b.name.split(" Ltd")[0] if i % 2 else None,
            owner_sector=None if i % 3 == 0 else _cycle(E.Sector, i + 6 * t), written_agreement=_cycle([True, False, None], i),
            treds_onboarded=_cycle([None, True, False], i),
        ))

    # Invoices: every terms_source and status.
    invoices = []
    for i in range(8):
        b = mine[i % len(mine)]
        issue = START + timedelta(days=3 * i)
        terms_source = _cycle(E.TermsSource, i)
        terms = None if terms_source is E.TermsSource.UNKNOWN else _cycle([15, 30, 45], i)
        gross = g.paise(20_000, 500_000)
        gst = gross * 18 // 118
        inv = g.add(T.Invoice(
            tenant_id=tenant, invoice_id=ids.invoice_id(tenant, supplier_gstin, f"INV/{t}/{i:03d}", FY),
            irn=g.sha("irn", tenant, i) if i % 3 == 0 else None, buyer_id=b.buyer_id, issue_date=issue,
            due_date=issue + timedelta(days=terms or 15), terms_days=terms, terms_source=terms_source,
            gross_paise=gross, gst_paise=gst if i % 4 else None,
            expected_tds_paise=(gross - gst) // 100 if i % 2 else None, status=_cycle(E.InvoiceStatus, i),
            known_at=_utc(issue + timedelta(days=1)),
        ))
        invoices.append(inv)

    # Transactions: one per category, plus one receipt per paid or part-paid invoice.
    txns: list[tuple[T.Transaction, E.TxnCategory]] = []

    def _txn_key(n: int, d: date, amount: int, narration: str) -> str:
        # Every key path, so fixtures exercise all four.
        path = n % 4
        if path == 0:
            return ids.txn_key_bank_id(f"BANKTXN{t}{n:06d}", d, amount)
        if path == 1:
            return ids.txn_key_utr(f"UTR{t}{n:010d}", amount)
        if path == 2:
            return ids.txn_key_balance(1_000_000 + n, d, amount)
        return ids.txn_key_ordinal(d, amount, narration, 0)

    def txn(d: date, amount: int, category: E.TxnCategory, narration: str) -> T.Transaction:
        n = len(txns)
        row = g.add(T.Transaction(
            tenant_id=tenant, txn_uid=ids.txn_uid(tenant, account, _txn_key(n, d, amount, narration)), account_id=account,
            value_date=d, known_at=_utc(d + timedelta(days=1)), amount_paise=amount,
            direction=E.Direction.CREDIT if amount > 0 else E.Direction.DEBIT, narration_raw=narration,
            source=_cycle(E.TxnSource, n), dq_score=1.0 if n % 5 else 0.8,
            dup_group_id=f"DUP-{t}-{n}" if n % 7 == 3 else None,
        ))
        txns.append((row, category))
        return row

    for i, cat in enumerate(E.CATEGORIES):
        amount = CATEGORY_SIGN.get(cat, -1) * g.paise(500, 200_000)
        txn(START + timedelta(days=i), amount, cat, f"{cat.value.upper()} REF{i}")

    receipts = {}
    for i, inv in enumerate(invoices):
        if inv.status is E.InvoiceStatus.OPEN:
            continue
        net = inv.gross_paise - (inv.expected_tds_paise or 0)
        amount = net if inv.status is E.InvoiceStatus.PAID else net // 2
        receipts[inv.invoice_id] = txn(inv.issue_date + timedelta(days=10 + i), amount,
                                       E.TxnCategory.CUSTOMER_RECEIPT, f"NEFT CR {i}")

    # An abstained txn for the categoriser table.
    abstained = txn(START + timedelta(days=40), -g.paise(100, 5_000), E.TxnCategory.ABSTAIN, "UPI/XYZ/unclear")

    # ledger.parsed: every rail.
    for i, (row, _) in enumerate(txns):
        rail = _cycle(E.Rail, i)
        g.add(T.Parsed(
            tenant_id=tenant, txn_uid=row.txn_uid, rail=rail,
            counterparty_vpa=f"party{i}@okbank" if rail is E.Rail.UPI else None,
            counterparty_ifsc=f"HDFC0{i:06d}" if rail in (E.Rail.NEFT, E.Rail.RTGS, E.Rail.IMPS) else None,
            utr=f"UTR{t}{i:010d}" if rail is not E.Rail.UNKNOWN else None, reference=f"REF{i}" if i % 2 else None,
            gstin=_cycle(mine, i).gstin if i % 3 == 0 else None,
        ))

    # ledger.balances: reconstructed daily, a bank-reported anchor, and a late-txn version.
    opening = g.paise(100_000, 1_000_000)
    by_date: dict[date, list[T.Transaction]] = {}
    for row, _ in txns:
        by_date.setdefault(row.value_date, []).append(row)
    running = opening
    for d in sorted(by_date):
        running += sum(x.amount_paise for x in by_date[d])
        known = max(x.known_at for x in txns_up_to(txns, d))
        g.add(T.Balance(tenant_id=tenant, account_id=account, date=d, closing_paise=running, continuity_ok=True,
                        balance_source=E.BalanceSource.RECONSTRUCTED, known_at=known))
    last = max(by_date)
    g.add(T.Balance(tenant_id=tenant, account_id=account, date=last, closing_paise=running, continuity_ok=None,
                    balance_source=E.BalanceSource.BANK_REPORTED, known_at=_utc(START + timedelta(days=60))))
    g.add(T.Balance(tenant_id=tenant, account_id=account, date=last, closing_paise=running - 10_000,
                    continuity_ok=False, balance_source=E.BalanceSource.RECONSTRUCTED,
                    known_at=_utc(START + timedelta(days=61))))

    # ledger.matches and labels.invoice_outcomes: every payer_kind, settlement_kind, tds basis.
    as_of = START + timedelta(days=60)
    settled = list(E.SettlementKind)
    for i, inv in enumerate(invoices):
        k = i // 2
        if inv.invoice_id in receipts:
            rec = receipts[inv.invoice_id]
            tds_rate = _cycle([0.0, 0.001, 0.01, 0.02, 0.10, None], i)
            g.add(T.Match(
                tenant_id=tenant, txn_uid=rec.txn_uid, invoice_id=inv.invoice_id, allocated_paise=rec.amount_paise,
                tds_inferred_paise=inv.expected_tds_paise or 0, confidence=0.9, method=_cycle(E.MatchMethod, i),
                payer_kind=_cycle(E.PayerKind, k + t), tds_rate=tds_rate,
                tds_basis=None if tds_rate is None else _cycle(E.TdsBasis, i), known_at=rec.known_at,
            ))
        if inv.status is E.InvoiceStatus.PAID:
            rec = receipts[inv.invoice_id]
            kind = _cycle(settled, k + 2 * t)
            treds = kind is E.SettlementKind.TREDS_DISCOUNT
            g.add(T.InvoiceOutcome(
                tenant_id=tenant, invoice_id=inv.invoice_id, buyer_id=inv.buyer_id, issue_date=inv.issue_date,
                first_paid_date=rec.value_date, paid_date=rec.value_date, paid_fraction=1.0, settlement_kind=kind,
                censored_at=rec.value_date if treds else None, known_at=rec.known_at,
            ))
        else:
            first = receipts.get(inv.invoice_id)
            g.add(T.InvoiceOutcome(
                tenant_id=tenant, invoice_id=inv.invoice_id, buyer_id=inv.buyer_id, issue_date=inv.issue_date,
                first_paid_date=first.value_date if first else None, paid_date=None,
                paid_fraction=0.5 if first else 0.0, settlement_kind=None, censored_at=as_of,
                known_at=_utc(as_of, 23),
            ))
    # A later version of one open invoice and its label: the as-of read must pick it only after known_at.
    open_inv = next(i for i in invoices if i.status is E.InvoiceStatus.OPEN)
    paid_on = as_of + timedelta(days=5)
    g.add(open_inv.model_copy(update={"status": E.InvoiceStatus.PAID, "known_at": _utc(paid_on + timedelta(days=1))}))
    g.add(T.InvoiceOutcome(
        tenant_id=tenant, invoice_id=open_inv.invoice_id, buyer_id=open_inv.buyer_id, issue_date=open_inv.issue_date,
        first_paid_date=paid_on, paid_date=paid_on, paid_fraction=1.0, settlement_kind=E.SettlementKind.BUYER,
        censored_at=None, known_at=_utc(paid_on + timedelta(days=1)),
    ))

    # features.txn_category: every category and source.
    for i, (row, cat) in enumerate(txns):
        abstain = cat is E.TxnCategory.ABSTAIN
        g.add(T.TxnCategoryRow(
            tenant_id=tenant, txn_uid=row.txn_uid, category=cat, confidence=0.3 if abstain else 0.9,
            source=E.CategorySource.MODEL if abstain else _cycle(E.CategorySource, i), needs_llm=abstain,
        ))

    # ops.owner_inputs and labels.txn_corrections.
    inputs = {}
    for i, kind in enumerate(E.OwnerInputKind):
        subject = abstained.txn_uid if kind is E.OwnerInputKind.TXN_CATEGORY else mine[i].buyer_id
        value = {
            E.OwnerInputKind.BUYER_SECTOR: {"sector": E.Sector.TEXTILES.value},
            E.OwnerInputKind.WRITTEN_AGREEMENT: {"written_agreement": True, "terms_days": 45},
            E.OwnerInputKind.TREDS_ONBOARDED: {"treds_onboarded": False},
            E.OwnerInputKind.TXN_CATEGORY: {"category": E.TxnCategory.MISC_EXPENSE.value},
        }[kind]
        inputs[kind] = g.add(T.OwnerInput(
            tenant_id=tenant, input_id=f"OI-{t}-{i}", kind=kind, subject_id=subject,
            value_json=json.dumps(value, sort_keys=True), source_file="owner_inputs.yaml",
            entered_at=_utc(START + timedelta(days=45)),
        ))
    correction = g.add(T.TxnCorrection(
        tenant_id=tenant, correction_id=f"CORR-{t}-0", txn_uid=abstained.txn_uid,
        category=E.TxnCategory.MISC_EXPENSE, input_id=inputs[E.OwnerInputKind.TXN_CATEGORY].input_id,
        corrected_at=_utc(START + timedelta(days=45)),
    ))

    # ops.dq_quarantine: every reason.
    quarantined = []
    for i, reason in enumerate(E.QuarantineReason):
        zero = reason is E.QuarantineReason.ZERO_AMOUNT
        quarantined.append(g.add(T.DqQuarantine(
            tenant_id=tenant, quarantine_id=f"Q-{t}-{i}", payload_id=payloads[0], account_id=account, reason=reason,
            value_date=START + timedelta(days=i), amount_paise=0 if zero else -g.paise(100, 1_000),
            direction=E.Direction.DEBIT if zero else E.Direction.CREDIT, narration_raw=f"BAD ROW {i}",
            known_at=_utc(START + timedelta(days=30)),
        )))

    # ops.events: every kind with a valid payload.
    receipt = next(iter(receipts.values()))
    payload_by_kind = {
        E.EventKind.CONSENT_CHANGED: ev.ConsentChanged(consent_id=f"CON-{t}-0", status=E.ConsentStatus.ACTIVE,
                                                       previous=E.ConsentStatus.PENDING),
        E.EventKind.PAYLOAD_FETCHED: ev.PayloadFetched(payload_id=payloads[0], consent_id=f"CON-{t}-0", fip_id="FIP0",
                                                       sha256=g.sha(tenant, payloads[0])),
        E.EventKind.FETCH_FAILED: ev.FetchFailed(consent_id=f"CON-{t}-0", fip_id="FIP1", attempt=3, error="timeout"),
        E.EventKind.NEW_CREDIT: ev.NewCredit(txn_uid=receipt.txn_uid, account_id=account,
                                             value_date=receipt.value_date, amount_paise=receipt.amount_paise),
        E.EventKind.NEW_INVOICE: ev.NewInvoice(invoice_id=invoices[0].invoice_id, buyer_id=invoices[0].buyer_id,
                                               issue_date=invoices[0].issue_date, gross_paise=invoices[0].gross_paise),
        E.EventKind.CORRECTION: ev.Correction(correction_id=correction.correction_id, txn_uid=correction.txn_uid,
                                              category=correction.category),
        E.EventKind.OVERRIDE: ev.Override(items=[ev.OverrideItem(kind=E.OverrideKind.ONE_OFF, on=as_of,
                                                                 amount_paise=-g.paise(1_000, 50_000))]),
        E.EventKind.DQ_ALERT: ev.DqAlert(quarantine_id=quarantined[0].quarantine_id, reason=quarantined[0].reason,
                                         payload_id=payloads[0], account_id=account),
        E.EventKind.ACCEPT_GAP: ev.AcceptGap(account_id=account, from_date=last, to_date=last, gap_paise=10_000,
                                             accepted_by="operator", note="bank statement gap confirmed"),
        E.EventKind.TXN_ID_CONFLICT: ev.TxnIdConflict(
            account_id=account, bank_txn_id=f"BANKTXN{t}000000", existing_txn_uid=txns[0][0].txn_uid,
            payload_id=payloads[1], existing_value_date=txns[0][0].value_date,
            new_value_date=txns[0][0].value_date + timedelta(days=30), existing_amount_paise=txns[0][0].amount_paise,
            new_amount_paise=txns[0][0].amount_paise - 100),
        E.EventKind.ERASURE_COMPLETED: ev.ErasureCompleted(consent_id=f"CON-{t}-0", keys_destroyed=1),
    }
    for i, (kind, payload) in enumerate(payload_by_kind.items()):
        g.add(T.Event(tenant_id=tenant, event_id=f"EV-{t}-{i:02d}", kind=kind, payload_json=payload.model_dump_json(),
                      at=_utc(START + timedelta(days=i), 12)))

    # features.obligations: every kind; GST's amount belongs to the sales forecaster.
    for i, kind in enumerate(E.ObligationKind):
        gst = kind is E.ObligationKind.GST
        g.add(T.Obligation(
            tenant_id=tenant, obligation_id=f"{kind.value}-{t}-{i}", kind=kind,
            due_date=as_of + timedelta(days=i + 1), amount_paise=None if gst else g.paise(1_000, 100_000),
            confidence=1.0 if gst else 0.8,
            amount_owner=E.AmountOwner.SALES_FORECASTER if gst else E.AmountOwner.OBLIGATIONS_DETECTOR,
        ))

    for b in mine:
        g.add(T.BuyerPrior(tenant_id=tenant, buyer_id=b.buyer_id, alpha=2.0, beta=6.0, cure_alpha=1.0, cure_beta=19.0,
                           source="type_default", as_of=as_of))

    for inv in invoices:
        if inv.status is E.InvoiceStatus.PAID:
            continue
        p_never = 0.05
        weekly = [0.3, 0.25, 0.15, 0.1]
        for w, p in enumerate(weekly, start=1):
            g.add(T.InvoiceTiming(tenant_id=tenant, invoice_id=inv.invoice_id, week=w, p_paid=p, p_never=p_never,
                                  p_beyond=round(1 - p_never - sum(weekly), 10), past_cutoff=False))

    for i, cat in enumerate([E.TxnCategory.REFUND, E.TxnCategory.MISC_INCOME, E.TxnCategory.MISC_EXPENSE,
                             E.TxnCategory.BANK_CHARGES]):
        for d in range(3):
            base = CATEGORY_SIGN.get(cat, -1) * g.paise(100, 10_000)
            qs = sorted([base, base + 2_000, base + 5_000, base + 9_000, base + 15_000])
            g.add(T.FlowQuantile(tenant_id=tenant, category=cat, date=as_of + timedelta(days=d + 1),
                                 q05=qs[0], q25=qs[1], q50=qs[2], q75=qs[3], q95=qs[4]))

    shares = [g.r.random() for _ in mine]
    for name in E.SalesParam:
        if name is E.SalesParam.BUYER_SHARE:
            for b, s in zip(mine, shares):
                g.add(T.SalesProcess(tenant_id=tenant, as_of=as_of, name=name, key=b.buyer_id, value=s / sum(shares)))
        else:
            g.add(T.SalesProcess(tenant_id=tenant, as_of=as_of, name=name, key=None, value=round(g.r.uniform(1, 20), 3)))

    # forecast.* and decisions.*: one baseline run and one scenario run.
    scenario_id = f"SC-{t}-tariff"
    g.add(T.Scenario(tenant_id=tenant, scenario_id=scenario_id, name="tariff shock",
                     params_json=json.dumps({"hazard_multiplier": 0.7}), breaking_magnitude=1.8 if t == 0 else None))
    for r_i, scen in enumerate([None, scenario_id]):
        run_id = f"RUN-{t}-{r_i}"
        overrides = [] if r_i == 0 else [ev.OverrideItem(kind=E.OverrideKind.ONE_OFF, on=as_of + timedelta(days=5),
                                                          amount_paise=-g.paise(1_000, 50_000))]
        overrides_json = ev.OVERRIDE_LIST.dump_json(overrides).decode()
        seed = int.from_bytes(hashlib.sha256(f"{run_id}:".encode()).digest()[:8], "little")
        g.add(T.Run(
            tenant_id=tenant, run_id=run_id, as_of=as_of, input_snapshot_ids=[f"snap-{t}-{r_i}-ledger"],
            model_versions_json=json.dumps({"timing": "0.1"}), seed=seed,
            seed_streams_json=json.dumps({"timing": seed // 3}), scenario_id=scen, threshold_paise=0,
            overrides_json=overrides_json, overrides_hash=hashlib.sha256(overrides_json.encode()).hexdigest(),
            n_paths=10_000, horizon_days=90, runway_days=None if r_i == 0 else 40,
        ))
        for d in range(1, 6):
            mid = running + g.paise(-50_000, 50_000)
            g.add(T.BalanceQuantile(tenant_id=tenant, run_id=run_id, date=as_of + timedelta(days=d),
                                    q05=mid - 4_000_000, q25=mid - 1_000_000, q50=mid, q75=mid + 1_000_000,
                                    q95=mid + 4_000_000))
            p = min(1.0, 0.05 * d * (r_i + 1))
            cond = g.paise(1_000, 100_000)
            g.add(T.Shortfall(tenant_id=tenant, run_id=run_id, date=as_of + timedelta(days=d), day=d, p_short=p,
                              expected_shortfall_cond_paise=cond, expected_shortfall_uncond_paise=round(p * cond)))
        for rank, action in enumerate(E.ActionKind, start=1):
            g.add(T.Plan(tenant_id=tenant, run_id=run_id, rank=rank, action=action,
                         target_id=invoices[rank % len(invoices)].invoice_id if action is not E.ActionKind.OVERDRAFT
                         else None, cost_paise=g.paise(0, 20_000), p_short_after=0.05))
        for kind in E.DriverKind:
            driver_id = {E.DriverKind.INVOICE: invoices[0].invoice_id, E.DriverKind.BUYER: mine[0].buyer_id,
                         E.DriverKind.SECTOR_SHOCK: mine[0].sector.value}[kind]
            g.add(T.Driver(tenant_id=tenant, run_id=run_id, driver_kind=kind, driver_id=driver_id,
                           delta_p_short=round(g.r.uniform(-0.1, 0.3), 4)))

    for h in (7, 30, 90):
        g.add(T.CalibrationState(tenant_id=tenant, horizon=h, as_of=as_of, alpha_t=0.1, miss_rate_28d=0.12))
    for sector in sorted({b.sector for b in mine}):
        for w in range(3):
            g.add(T.ShockState(tenant_id=tenant, sector=sector, week_start=as_of - timedelta(days=7 * (3 - w)),
                               z=round(g.r.gauss(0, 1), 4), z_sd=0.5))


def txns_up_to(txns, d: date):
    return [row for row, _ in txns if row.value_date <= d]


def generate(seed: int = 0) -> dict[str, pl.DataFrame]:
    g = _Gen(seed)
    buyers = _buyers(g)
    _disclosures(g, buyers)
    for t in range(2):
        _tenant(g, t, buyers)
    return {table: frame(T.TABLES[table], rows) for table, rows in g.rows.items()}


def write(out_dir: str | Path, seed: int = 0) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for table, df in generate(seed).items():
        p = out / f"{table}.parquet"
        df.write_parquet(p)
        paths.append(p)
    return paths


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out_dir")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    for p in write(a.out_dir, a.seed):
        print(p)


if __name__ == "__main__":
    main()
