# Invoice ingest: adapters and the interface

**Neither new format is verified against a real file.** There is no real Tally export and no
verified GST e-invoice sample in this project. Both adapters, and the renderers that feed
them in `eval/platform/render/`, are built from the formats as understood. The round trip
passing proves only that our renderer and our reader agree with each other. It is not a
working integration, and it says nothing about whether a real Tally day book or a real
e-invoice reads correctly. Nothing in the repo pins either shape: Track B's generator has an
`irn` column that is a sha256 of the invoice number ("valid in shape, not a real IRN", per
`docs/renderer-review-for-track-a.md`), every current world has `irn` null, and the contract
only types `ledger.invoices.irn` as 64 hex characters.

## The interface

An adapter turns one file into `InvoiceLine` records and per-record `Reject`s
(`platform/src/matsya_platform/ingest/invoices.py`). Everything after that (invoice_id,
fiscal year, terms, buyer resolution, duplicates, contract validation) is the shared core.
Adapters are looked up by name in `ingest/registry.py`: `csv_generic`, `tally_daybook`,
`gst_einvoice`.

### What adding the second and third adapters showed was wrong

The interface had one implementation, so its assumptions were the CSV's. Four were wrong:

1. **`irn` was hard-coded null in the core.** The core wrote `irn=None` with the comment
   "no IRN in these sources", so an e-invoice's IRN had nowhere to go. `InvoiceLine` now
   carries `irn`, null for every format but the e-invoice.
2. **The supplier was only ever the caller's word.** `invoice_id` hashes the supplier GSTIN
   that the caller passes. An e-invoice states its seller, and a file from another supplier
   would have been given valid-looking ids under the wrong supplier. `InvoiceLine` now
   carries `seller_gstin` when the source states it, and the core rejects a mismatch as
   `wrong_supplier`.
3. **The total check assumed gross = taxable + GST.** An e-invoice has `RndOffAmt` and
   `OthChrg`, and a Tally voucher can carry a round-off ledger. A valid document with a
   round-off would either be rejected, or have to fold the round-off into taxable value,
   which is a silent error, since the matcher computes TDS on taxable value. `InvoiceLine` now
   carries `other_paise`, and the check is taxable + GST + other = gross.
   `ledger.invoices` still has no taxable-value field (F16).
4. **The reject reasons were the CSV's.** Added: `malformed` (a document that is not an
   object, a bad IRN), `unsupported_document` (credit and debit notes), `wrong_supplier`.

Unchanged, on purpose: `source_row` stays an int, now defined as the record's 1-based
position in the adapter's own unit (CSV data row, the day book line where the voucher
starts, the e-invoice document index). The shared amount and date parsing moved from the
CSV adapter to `ingest/fields.py`.

## Formats as understood

**Tally-style day book** (`ingest/tally_daybook.py`): CSV with title lines, then Date,
Particulars, Vch Type, Vch No., Debit Amount, Credit Amount.
- Dates are DD-MMM-YY. Amounts are rupees with Indian digit grouping, in separate debit and
  credit columns; a Dr or Cr suffix must agree with its column.
- A Sales voucher debits the party (the gross) and credits the sales ledger and the Output
  GST ledgers. Its bill-wise line `New Ref <bill>` carries the invoice reference, optionally
  followed by `Due on DD-MMM-YY` or `<n> Days`.
- The voucher number is Tally's own sequence and is not used. Other voucher types are
  skipped, not rejected.
- A day book shows no party GSTIN, so every Tally invoice resolves its buyer by name.
- Tally's native export is XML; a CSV day book is one of several layouts an owner might
  produce, and only this one is read.

**GST e-invoice JSON** (`ingest/gst_einvoice.py`): one document or a list. Each document has:
- `Irn`;
- `DocDtls` {Typ, No, Dt as DD/MM/YYYY};
- `SellerDtls`, `BuyerDtls` {Gstin, LglNm};
- `ItemList`, `ValDtls` (must agree with the ItemList sums);
- `PayDtls.CrDay`, absent when terms are unknown, which the importer records as `unknown`.

Amounts are JSON numbers, read as Decimal, never float. Only `Typ` INV is an invoice. The
renderer's IRN is sha256(supplier GSTIN | FY | INV | number), a stand-in of the right shape,
not one issued by the registration portal.

## Round trip (eval/platform)

Each invoice is assigned a format, seeded: e-invoice 5 percent (only for buyers with a GSTIN;
micro suppliers below the e-invoicing threshold have none), day book 45 percent, CSV 50
percent. One supplier keeping two book formats is not realistic; it is how one render
exercises all three adapters. `duplicate_row` applies to every format; `missing_gstin` only
to the CSV, since a day book never shows a GSTIN and a B2B e-invoice cannot omit it.

4 worlds x 3 seeds, invoices rebuilt exactly and nothing unexpectedly rejected:

| format | invoices | rebuilt exactly | rejects | buyer resolution | terms_source document / unknown |
|---|---|---|---|---|---|
| csv_generic | 2,598 | 2,598 | 0 | gstin 2,598 (with missing_gstin 5%: gstin 2,475, name 123) | 2,389 / 209 |
| tally_daybook | 2,358 | 2,358 | 0 | name 2,358 | 2,142 / 216 |
| gst_einvoice | 261 | 261 | 0 | gstin 261 | 239 / 22 |

With `duplicate_row` at 5 percent, all 190 duplicates were reported as identical and dropped
(CSV 91, day book 87, e-invoice 12).

The round trip found one real adapter bug before this table: a Dr suffix on the party's
debit amount was rejected as `bad_amount` (the suffix was compared with the first two
letters of "debit"). The hand-written fixture had the suffix only on the bill-wise line,
which is skipped.

## Limitations

- **Unverified formats**, as above. The first real Tally export and the first real
  e-invoice are the actual test.
- **Day book buyers resolve by name only.** Every world here has unique buyer names, so
  the ambiguous-name reject never fires in the loop; it is covered only by the CSV unit
  tests. A real supplier's ledger names will differ from the buyer registry far more than
  case does.
- **No overlap between formats.** A supplier above the threshold would have the same invoice
  in Tally and as an e-invoice. The second file's copy is skipped as already in the ledger,
  so if the day book arrives first the IRN is never recorded. Not modelled.
- **Rendered e-invoices never carry a round-off, and credit notes are never rendered.** Both
  paths are covered by unit tests only.
