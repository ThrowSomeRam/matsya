# matsya-contracts 0.1.0

The only code shared by Track A (platform) and Track B (intelligence). Each table's schema is
a pydantic row model in `tables.py`; its Polars schema is derived from that model. Both
tracks install this package editable and never import each other's code.

Changes come by PR approved by both owners. Within a phase they are additive (new nullable
columns, new tables, new enum members). Breaking changes wait for a gate and bump `VERSION`.

## Conventions

- **Money:** integer paise, `int64`, never floats. Any column ending `_paise` is typed
  `Paise`, and so are the quantile columns of `forecast.balance_quantiles` and
  `features.flow_quantiles`.
- **Signed amounts:** `ledger.transactions.amount_paise` is + credit, - debit, never zero.
  `direction` is redundant and must agree. A4 quarantines zero-amount rows and sign/direction
  mismatches to `ops.dq_quarantine`, with a `dq_alert` event. Only a balance-continuity break
  fails a batch. `features.obligations.amount_paise` is a positive outflow magnitude.
- **Time:** dates are ISO dates. Timestamps are tz-aware UTC; IST is applied only at display.
  Ledger-derived rows carry `value_date` (when it happened) and `known_at` (when we learned it).
- **As-of tables:** `ledger.balances`, `ledger.invoices`, `ledger.matches` and
  `labels.invoice_outcomes` are append-only, with `known_at` in the key. An as-of read takes,
  per key without `known_at`, the latest row with `known_at <= as_of`. `ledger.parsed`
  inherits its transaction's `known_at`.
- **Tenancy:** every row has `tenant_id` except the global `ref.*` tables (`ref.buyers`,
  `ref.buyer_disclosures`). There is no sentinel tenant.
- **IDs** (`ids.py`) are opaque and never parsed:
  - `tenant_id`: UUIDv4.
  - `txn_uid`: sha256(tenant | account | k)[:32]. k is `txn:<txnId>|<value_date>|<amount>`;
    else `utr:<UTR/RRN>|<amount>`; else `bal:<running balance>|<value_date>|<amount>`; else
    `ord:<value_date>|<amount>|<narration>|<ordinal>`. Amounts are signed paise. Build k with
    the `ids.txn_key_*` functions.
  - `invoice_id`: sha256(tenant | supplier GSTIN | normalised invoice no | FY)[:32]. It never
    depends on IRN.
  - `buyer_id`: sha256(PAN)[:32] when `id_basis` is `pan` (global). With no PAN,
    sha256("noPAN:" + tenant_id + "|" + normalised name or VPA)[:32] and `id_basis` is
    `local`. Local buyers never pool across tenants.
- **Validation:** fixtures and CI validate every row (`validate(df, table)`). Real writes use
  `validate(df, table, sample=WRITE_SAMPLE)`: columns, dtypes, nulls and keys on the whole
  frame, row rules on a sample.
- **Censoring:** in `labels.invoice_outcomes`, `censored_at` is set iff `paid_date` is null or
  `settlement_kind` is `treds_discount`. If `censored_at` is set, the timing label is censored
  regardless of `paid_date` (`tables.timing_state`).
- **Sector:** `ref.buyers.sector_source` is `exchange`, `cin_nic` or `unknown`. The owner's
  sector lives in `ref.tenant_buyers`. `tables.effective_sector` applies
  exchange > cin_nic > owner > unknown.
- **Enums** (`enums.py`) are additive only. Members are never renamed or removed, and
  consumers map unknown values to a fallback.
- **Events:** `ops.events.kind` is an `EventKind`. Each kind has one payload model in
  `events.PAYLOADS`, and `payload_json` must parse as that model. Track B reads only listed kinds.

## One writer per namespace

| Namespace | Writer | Tables |
|---|---|---|
| `ops` | A | consents, events, dq_quarantine, owner_inputs |
| `raw` | A | aa_payloads |
| `ledger` | A | transactions, balances, parsed, invoices, matches |
| `labels` | A | invoice_outcomes, txn_corrections |
| `ref` | A | buyers (global), tenant_buyers, buyer_disclosures (global) |
| `features` | B | txn_category, obligations, buyer_priors, invoice_timing, flow_quantiles, sales_process |
| `forecast` | B | runs, balance_quantiles, shortfall, scenarios, calibration_state, shock_state |
| `decisions` | B | plans, drivers |

`ledger.purchase_invoices` is deferred: optional, fixture-only until G2.

## Use

```python
from matsya_contracts.validate import validate, empty, schema
from matsya_contracts.fixtures import generate

tables = generate(seed=0)              # dict[table name, polars.DataFrame]
validate(tables["ledger.balances"], "ledger.balances")
```

```
python -m matsya_contracts.fixtures OUT_DIR --seed 0   # one Parquet file per table
pytest
```

`validate()` raises `ContractError` and never drops or fixes rows.
