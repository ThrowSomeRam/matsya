# Track A contract positions (contracts v0.1)

Track A's position on Track B's contracts v0.1 proposal. Sections A and B are what Track A
will build to. Section D lists facts nobody may code against until they are verified.

## A. Required in contracts v0.1 before the contracts PR opens

1. **As-of labels and invoices.** `ledger.invoices`, `ledger.matches` and
   `labels.invoice_outcomes` carry `known_at` and are append-only, like `ledger.balances`.
   An as-of read takes, per key, the latest row with `known_at <= X`. An invoice status
   change is a new row. `ledger.parsed` has no `known_at` of its own: a parsed row inherits
   its transaction's `known_at`. This replaces the timing model's `label_lag_days = 3` cut
   (`timing/labels.py`).
2. **IDs.** All IDs are opaque strings and are never parsed.
   - `tenant_id` = UUIDv4.
   - `txn_uid` = sha256(tenant | account | k), truncated to 32 hex chars. k, in order of
     preference:
     - `txn:<bank txnId>|<value_date>|<signed amount_paise>`
     - `utr:<UTR or RRN>|<signed amount_paise>`
     - `bal:<post-transaction running balance>|<value_date>|<signed amount_paise>`
     - `ord:<value_date>|<signed amount_paise>|<normalised narration>|<ordinal>`, last resort
   - Reasons:
     - Banks reuse txnIds, so a bare txnId hash collides two real transactions into one
       row and loses money from the ledger. The same txnId arriving with different fields
       raises a `txn_id_conflict` event.
     - One UTR can cover a payment and its bank charge, hence the amount.
     - A running balance repeats across dates, hence the date and amount.
     - Two identical same-day UPI credits must not collapse into one row, and an ordinal
       alone shifts when a later fetch covers a different window.
   - `invoice_id` = sha256(tenant | supplier GSTIN | normalised invoice no | FY), truncated
     the same way. It never depends on IRN.
3. **Buyers.** `ref.buyers` holds public facts and has no `tenant_id`. It has a nullable
   `pan` and an `id_basis` enum (`pan | local`).
   - `pan`: `buyer_id` = sha256(PAN)[:32], global.
   - `local` (no PAN): `buyer_id` = sha256("noPAN:" + tenant_id + "|" + normalised name or
     VPA)[:32]. Local buyers are tenant-scoped and do not pool across tenants.
   - `gstin` is the single primary GSTIN. A national buyer has one GSTIN per state;
     `ref.buyer_gstins` is deferred to after G2.
   - `ref.tenant_buyers` holds `tenant_id`, `buyer_id`, owner sector, `written_agreement`,
     `treds_onboarded`.
   - Convention exception: every row has `tenant_id` except global `ref.*` tables. There is
     no sentinel tenant.
4. **Namespaces.** `ref.obligations` and `ref.buyer_priors` move to `features.*`.
   `ops.calibration_state` and `ops.shock_state` move to `forecast.*`. `ops.events` stays
   Track A, with a typed `kind` enum and one pydantic payload model per kind. Track B reads
   only listed kinds.
5. **Signed amounts.** `ledger.transactions.amount_paise` is signed (+ credit, - debit);
   `direction` stays as a redundant check.
   - Zero-amount rows and sign/direction mismatches are quarantined with a dq flag and an
     alert, not written to `ledger.transactions`.
   - Only a balance-continuity break fails the batch.
   - `validate()` on read raises and never drops or fixes rows.
6. **Balances.** `ledger.balances` adds `known_at` and `balance_source`
   (`bank_reported | reconstructed`).
   - Bank-reported: `known_at` = `fetched_at` of the payload that carried the balance.
   - Reconstructed: `known_at` = the latest `known_at` among the transactions behind it.
   - Append-only, key `(tenant_id, account_id, date, known_at)`. An as-of read takes the
     latest row with `known_at <= as_of`.
7. **Categories.** `features.txn_category.category` is a closed enum: the 15 existing
   classes (customer_receipt, supplier_payment, salary, rent, emi, professional_fee,
   gst_payment, tds_payment, pf_esi, bank_charges, refund, owner_transfer, cash_withdrawal,
   misc_income, misc_expense) plus `abstain`. Additive only: values are never renamed or
   removed, and consumers map values they do not know to a fallback.
8. **Validation.** Fixtures and CI run full pydantic validation on every row. Real writes
   check columns, dtypes, nulls and key uniqueness on the whole frame, plus the row rules on
   a configurable sample.

## B. Agreed changes

- **`ledger.invoices`:** `terms_source` enum
  `written_agreement | document_terms | default_15d | unknown`. `written_agreement` comes
  only from owner input. `irn` is nullable.
- **`ledger.matches`:** `payer_kind` enum `buyer | treds_financier | other_entity | unknown`;
  nullable `tds_rate` and `tds_basis`. The TDS candidate rate set is configurable, includes
  0%, and is tested against both GST-exclusive and GST-inclusive bases.
- **`ref.buyers`:** nullable `treds_mandated` (from A7). `treds_onboarded` is nullable,
  owner-only, and lives in `ref.tenant_buyers`. Closed sector enum plus `unknown`.
  `sector_source` precedence: `exchange > cin_nic > owner > unknown`.
- **`ref.buyer_disclosures`:** nullable `msme_not_due`, `msme_unbilled`,
  `msme_total_outstanding`. Null means the column is absent from the report; 0 means
  reported as zero.
- **`labels.invoice_outcomes`:** add `first_paid_date` and `settlement_kind`.
  - `paid_date` = the first date where allocated plus inferred TDS reaches gross minus a
    config tolerance.
  - `first_paid_date` = the date of the first allocation.
  - `paid_fraction` = min(settled / gross, 1).
  - `censored_at` is set iff `paid_date` is null or `settlement_kind` says censor.
- **TReDS-financier credits:** the ledger status is settled, because the cash is real. The
  timing label is censored at the discount date, not excluded. Limitation: the supplier's
  choice to discount is mildly informative about expected slowness, so censoring here is
  not fully non-informative.
- **`labels.txn_corrections`:** new table. Track A writes it from owner input; the
  categoriser reads it.
- **Owner input:** a YAML/CSV loader only. It writes `ops.owner_inputs` and projects into
  `ref.tenant_buyers` and `ledger.invoices.terms_source`.
- **`features.buyer_priors.source`:** a free string until B3 exists. Closing it to an enum
  later is a breaking change and needs a version bump.

Contracts v0.1 implementing sections A and B is on branch `contracts-v0.1`.

## C. Deferred

- `ledger.purchase_invoices`: optional, fixture-only until G2.
- `payer_kind` detection beyond a TReDS platform-name list.
- `treds_onboarded` population and any onboarding beyond the loader: G3.
- NIC-to-sector mapping: G2. The sector enum itself goes in v0.1.
- ITA 2025 section wording in drafts: P3.
- A6 build order: generic CSV, then Tally, then Zoho, then e-invoice.

## D. Unverified

Each of these must carry a `# VERIFY:` comment wherever it appears in code, and must not
appear in the demo until verified.

- CPSE settlement via TReDS since June 2026. Status should be `verify`, not `verified`,
  until a PIB release or gazette notification is found.
- Rs 250 cr TReDS onboarding threshold (S.O. 4845(E), 7 Nov 2024). Pull the gazette.
- Income-tax Act 2025 sections 393(1) (TDS) and 37(2)(g) (old 43B(h)).
- NIC code taken from CIN characters 2 to 6.
- Udyam registration date vs supply date in MSMED claim logic.
- Current RBI bank rate.
- TDS base: taxable value vs GST-inclusive amount.

## E. Requests to Track B

- Publish synth worlds as Parquet in the contract schema, with truth tables. Track A renders
  them to ReBIT AA JSON and Tally/CSV in `eval/platform/`, with injected duplicates and
  missing IDs.
- Review the definitions Track A wrote. In Track B's namespaces: the `forecast.shock_state`
  columns and `forecast.calibration_state.as_of`. In Track A's: `ops.owner_inputs`,
  `ops.dq_quarantine` and the 11 `ops.events` payload models.
- Set the CPSE/TReDS constant to `verify`.
- Send links for the 393(1) and 37(2)(g) sources.
- Confirm the hackathon deadline.
