# Renderer plan: review from Track B

Comments on the renderer plan (`eval/platform/render/`). Track B is not editing that
folder; these are for Track A to take or leave.

The plan is sound. The things to keep: fail loudly on input that doesn't validate, keep
renderers pure, log every fault with its expected outcome, require a real reference sample
before each renderer, and stop after the first end-to-end comparison.

## What Track B now provides

The C1 blocker is gone. `synth.export.write_contract_parquet(world, out_dir)` writes:

- `out_dir/<namespace>.<table>.parquet`: every contract table Track A would hold at as_of
  (all rows validated against contracts v0.1);
- `out_dir/truth/*.parquet`: generator truth. `truth/transactions.parquet` covers the whole
  world (past and future) with two columns added for renderers:
  - `bank_txn_id`: the synthetic bank txnId behind `txn_uid`, so
    `txn_uid = ids.txn_uid(tenant, account, ids.txn_key_bank_id(bank_txn_id, value_date, amount_paise))`;
  - `day_seq`: 0-based posting order within `value_date`. Running balances in
    `(value_date, day_seq)` order end each day at the daily closing in `truth/balances.parquet`
    (tested).

`synth.world.contract_tables_at(truth, cut)` gives the contract tables at any time inside the
world, and `synth.replay` steps through them a day at a time.

## Must design in, or the comparison will fail

1. **Cut fetch windows by `known_at`, not `value_date`.** Late reporting is the AA
   behaviour that matters most downstream. A payment value-dated Monday and reported
   Wednesday must arrive in Wednesday's payload. Overlap windows by at least the largest
   reporting lag (about 3 days on default worlds) or late rows fall between windows. This
   is what exercises the ledger-complete-through watermark (proposal in
   `docs/track-b-decisions.md`: L = largest `known_at date - value_date` seen per account,
   ledger complete through fetch date minus L, open labels re-stamped with that date at
   each fetch).
2. **`known_at` changes in the round trip.** The platform sets `known_at` from the
   `fetched_at` of the payload that carried the row, not from the generator's value. The
   expected tables must either leave `known_at` out of the comparison or recompute it as the
   delivery time of the first window holding the row. The versioned tables
   (`ledger.balances`, `ledger.invoices`, `ledger.matches`, `labels.invoice_outcomes`)
   depend on window policy and delivery order the same way.
3. **Faults that change the key path change the expected `txn_uid`.** With
   `drop_txn_id` the platform falls back to the UTR, balance or ordinal key, so the truth
   writer must recompute that row's `txn_uid` with the matching `ids.txn_key_*` function
   instead of copying the original.
4. **`currentBalance` per row** comes from `day_seq`; the contract only holds daily closings.

## TReDS case

CPSE buyers settle through TReDS platforms, so credits arrive from a platform name, not the
buyer. Two different things look alike:

- **pass-through settlement:** the buyer's own payment routed via the platform. Should
  match with `payer_kind = buyer`, `settlement_kind = buyer`;
- **financier discount:** the financier pays early, net of a fee. Should match with
  `payer_kind = treds_financier`, `settlement_kind = treds_discount`, `censored_at` = the
  discount date.

The generator already produces both (`treds_discount_prob > 0` turns discounts on; CPSE
receipts otherwise use platform narrations). Please render both and assert the matcher tells
them apart (open question 6). Also assert that a discount net of the fee still settles the
invoice (open question 7): with a 1.5% fee, allocated + TDS never reaches gross, so a
tolerance-based `paid_date` would never fire.

## Smaller points

- **`sign_error` can't occur in ReBIT as described.** ReBIT amounts are unsigned with a
  separate `type`, so there is no sign to disagree with. Put this fault in signed-amount
  sources (statement CSV). The ReBIT equivalent is a wrong `type`, which only balance
  continuity can catch.
- **ReBIT `amount` is a decimal rupee string**, like `currentBalance`. Mark the schema
  version with `# VERIFY` until the reference sample is checked in.
- **Tally: the real risk is parsing, not rounding.** Paise to rupees at two decimals is
  exact. Test Indian digit grouping (`1,23,456.00`), `Dr`/`Cr` suffixes and float parsing.
- **Set `has_irn` false for micro-supplier worlds.** The generator defaults it to true
  (above the Rs 5 crore e-invoicing threshold). Its `irn` is a hash of the invoice number:
  valid in shape, not a real IRN.
- **Unknown terms:** for invoices with `terms_source = unknown` (`terms_unknown_share > 0`),
  render no `CrDay` / credit-days field, so the invoice importer has a real reason to emit
  `unknown`.
- **`partial_window`: pick one expected outcome now**, either `batch_fails` or `accept_gap`
  followed by recovery. "Continuity break / accept_gap" can't be asserted.
