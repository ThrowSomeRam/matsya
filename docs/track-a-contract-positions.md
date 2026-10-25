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
     - One `utr:` key per payload: a second row with the same UTR/RRN and amount in the
       same payload is a second transaction and takes its own `bal:` or `ord:` key.
       Merging would lose money, which cannot be recovered; a false duplicate is visible.
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
   - A break is dated by the IST posting date of the anchor that reveals it, not by any
     value date. Its uncertainty window runs from the earlier of the last consistent
     anchor's posting date and the value dates of the rows posted since, through the
     break; an accepted break marks every reconstructed date in that window
     `continuity_ok = false`.
   - Unverifiable payloads (no `currentBalance` on any row and no Summary) are stored, not
     refused: their rows get a lowered `dq_score` and the payload is reported as
     `unverified_payload` (an `ops.events` kind once F7 lands).
   - `validate()` on read raises and never drops or fixes rows.
6. **Balances.** `ledger.balances` adds `known_at` and `balance_source`
   (`bank_reported | reconstructed`).
   - Bank-reported: `known_at` = `fetched_at` of the payload that carried the balance.
   - Reconstructed: `known_at` = the fetch time of the build that produced the version,
     the moment the figure became known. Not the latest `known_at` of the rows behind it:
     a closing can change with no new row behind its date (an accepted gap resolved, an
     opening re-derived), and that correction must supersede the stale version. The old
     rule left stale closings in place, some still marked `continuity_ok = true`.
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
- Use link evidence strength in payment-timing fitting: a label settled only by an
  arithmetic-only link (no invoice reference) is weaker evidence and should move a buyer's
  estimate less than a referenced one (`docs/matcher-design.md`; label field pending F14).
- Send links for the 393(1) and 37(2)(g) sources.
- Confirm the hackathon deadline.

## F. Contract problems found while building A4 (proposed for v0.2)

1. **`ledger.balances` key has no `balance_source`.** A bank-reported row and a
   reconstructed row for the same account and date, learned from the same fetch, share
   `(tenant_id, account_id, date, known_at)`. A4 keeps the bank-reported row (Track B uses
   it as the opening balance) and drops that reconstructed version. Proposal: key
   `(tenant_id, account_id, date, balance_source, known_at)`, with as-of reads per source.
   A strict xfail test in `platform/tests/ledger/test_build.py` pins this.
2. **`ledger.transactions` cannot be rebuilt into A4's working state.** Detecting
   `txn_id_conflict` and re-running continuity need the bank txnId, the key path, the
   payload id, the posting timestamp and the bank's running balance. None of these are in
   the contract, so A4 keeps them in private state. Proposal: an A-only `ledger.txn_lineage`
   table (txn_uid, payload_id, key_path, bank_txn_id, posted_at, balance_after_paise), or
   nullable columns on `ledger.transactions`.
3. **The ordinal path can collapse real transactions.** If one fetch window ends mid-day
   and the next begins on the same day, identical rows on either side both get ordinal 0
   and the same `txn_uid`. Proposal: include transactionTimestamp in the `ord:` key when
   the payload carries it.
4. **No table for coverage.** A4 computes, per account, `complete_through`: the end of the
   unbroken run of fetched date ranges (ReBIT `Transactions.startDate..endDate`). It comes
   from fetch ranges only, so backdated rows never move it, and a gap between ranges stops it
   until a fetch fills the gap. It is the right `censored_at` for open labels. The contract has
   nowhere to publish it, so `build()` returns it as a plain frame. Proposal: an additive
   `ledger.coverage` table (tenant_id, account_id, complete_through, known_at), append-only,
   read as of like balances.
5. **No home for invoice ingest rejects.** `ops.dq_quarantine` has transaction-shaped
   columns (account_id, value_date, direction) and only two reasons (`zero_amount`,
   `sign_direction_mismatch`). Invoice lines rejected as `unresolved_buyer`,
   `ambiguous_buyer`, `total_mismatch`, `bad_amount`, `bad_date`, `missing_field` or
   `invalid_row` are returned by `ingest()` but cannot be written. Proposal: additive
   `QuarantineReason` members plus nullable `source_file`, `source_row` and `detail`
   columns, or a separate `ops.ingest_rejects` table.
6. **Reconstructed `known_at` is defined the old way in the contract.** `contracts/tables.py`
   (`Balance.known_at` comment) says reconstructed `known_at` is "the latest known_at among
   the transactions behind it". A4 now stamps the build's fetch time instead (section A6).
   The stress sweep showed the old rule makes a corrected closing unversionable, leaving
   silently wrong balances. Proposal: change the comment and the README's balances rule;
   no schema change.
7. **No `ops.events` kind for unverified payloads.** `EventKind` is closed and has no
   `unverified_payload`, so A4 returns these payloads as a plain `unverified` frame from
   `build()` and cannot write the event. Proposal: additive `EventKind.UNVERIFIED_PAYLOAD`
   with a payload model (payload_id, account_id, rows).
8. **`ledger.invoices` has no invoice number.** Only the hashed `invoice_id` is stored, so
   the matcher resolves a narration reference by hashing its spelling variants for the
   plausible fiscal years (`match/references.py`). Proposal: an additive nullable
   `invoice_no` (normalised) on `ledger.invoices`.
9. **`MatchMethod` has no member per cascade stage.** Exact, TDS-explained, TReDS and
   part-payment matches are all written as `amount_window`, and bundles as `ip_assignment`;
   the real stage lives only in the matcher's own output. Proposal: additive members
   `exact`, `tds`, `treds`, `partial`, `bundle`.
10. **`ledger.matches` has no TReDS discount fee.** Settled = allocated + TDS + fee, but the
    fee cannot be written, so a reader cannot recompute that a discounted invoice is
    settled. Proposal: additive nullable `discount_fee_paise` (agreed earlier as the answer
    to Track B's question 7, not yet in the contract).
11. **Review-set invoices have no legal label.** The contract forbids `settlement_kind` on an
    open label and has no way to mark a label as excluded from training, so invoices in a
    review item get no new `labels.invoice_outcomes` version and are listed in the review
    set instead. Proposal: allow `settlement_kind = unknown` while open, or add a nullable
    `review` flag.

Matcher design, decisions and rejected approaches: `docs/matcher-design.md`.

## G. Stress sweep after the A4 balance-versioning fix (2026-10-25)

Sweep: 4 worlds (Track B generator seeds 0, 1, 2 and a denser seed-0 world), fault seeds
0 to 2, levels 0 to 100 percent, partial_mode batch_fails and accept_gap, open and closed
loop. Before = the sweep that exposed the bug (open loop only). After = the same grid once
these landed: breaks dated by posting date with an uncertainty window marked
`continuity_ok = false` (section A5), reconstructed versions stamped with the build's
fetch time (section A6), unverified payloads flagged, and rows sharing a UTR and amount in
one payload kept as two transactions. Thresholds are the lowest level at which any run
turns SILENT, as min/median/max over world x seed.

| mode | fault | SILENT before | SILENT after (open and closed loop alike) |
|---|---|---|---|
| batch_fails | combined | 50/50/100 | 50/75/75 (3 of 12 runs never) |
| accept_gap | partial_window | 5/37.5/75 | never |
| accept_gap | combined | 5/50/75 | 50/50/75 (5 of 12 runs never) |

- Combined at 100 percent went from SILENT in all 12 runs to loud in all 12: with no
  identifiers and no anchors, the payloads are now flagged unverified, so the rows lost to
  identity collisions are flagged rather than silently missing.
- Every single fault on its own stays exact at every level in both loops; no_anchors stays
  exact to 75 percent and correct (rows right, no reconstructed balances) at 100.
- Open loop and closed loop give the same thresholds. Closed loop changes completeness, not
  silence: at combined 75 percent, mean transactions-exact share is 1.0 closed against
  0.077 open. The open-loop loud levels are therefore largely a harness artifact (A2 cannot
  re-fetch there).
- The remaining SILENT cases at 50 to 75 percent combined are undiagnosed. What is known
  only: they are mostly reconstructed balances inconsistent with the rows held while marked
  `continuity_ok = true`, plus about one silently missing row per run. Nothing beyond that
  is claimed.
12. **No `ops.events` kind for a settled-invoice contradiction.** When a referenced payment
    displaces an earlier arithmetic-only link, A6 records `settled_invoice_contradiction`
    (displaced txn_uid, invoice_id, referenced txn_uid, both value dates) in its own output;
    `EventKind` is closed, so it cannot be written. Proposal: additive
    `EventKind.SETTLED_INVOICE_CONTRADICTION` with that payload.
13. **No home for matcher quality metrics.** Review-set size and unexplained credit rupees
    are computed only in scoring, yet they are the only signal for mislinks no referenced
    payment contradicts. Proposal, not built: an A-written `ops.match_runs` table, one row per
    tenant per matcher run: tenant_id, run_at, credits_considered, credits_matched,
    review_items, review_share, at_risk_paise, top10_coverage, unexplained_credit_paise,
    contradiction_events, credits_gated_by_category, known_at. A9 alerts on a jump in
    review_share or unexplained_credit_paise against the tenant's own trailing level.
14. **Labels cannot carry link evidence strength.** `labels.invoice_outcomes` has no field
    saying whether a paid label rests on a referenced link or on arithmetic alone;
    `ledger.matches.method` carries it per link, but the label does not. Proposal: additive
    nullable `evidence` (`referenced | arithmetic_only | arithmetic_no_basis_signal |
    mixed`) on `labels.invoice_outcomes`. `arithmetic_no_basis_signal` is the weakest: an
    arithmetic-only link whose TDS basis cannot tell an advance from a payment (0 percent
    TDS, or the two bases within the basis tolerance). It still settles the invoice.
15. **`erasure_completed` cannot say what was erased.** Its payload is `consent_id` and
    `keys_destroyed` only. When a consent ends while another of the tenant's consents is
    live, the key survives (`keys_destroyed = 0`) and only that consent's payloads go, and
    the event cannot record how many. Proposal: additive `payloads_erased` and
    `derived_rows_erased` counts.
16. **`ledger.invoices` has no taxable value.** Only `gross_paise` and `gst_paise` are
    stored, so a reader takes taxable value as gross minus GST. An e-invoice round-off
    (`RndOffAmt`) or other charge (`OthChrg`), or a Tally round-off ledger, makes that wrong
    by the charge, and the matcher computes TDS on taxable value. Ingest now reads
    `other_paise` but cannot write it (`docs/invoice-ingest.md`). Proposal: additive nullable
    `taxable_paise` on `ledger.invoices`.

## H. Persistence, raw store, consent and fetch (2026-10-25)

**Decision: no Iceberg, MinIO, Trino, Redpanda or Temporal.** The handbook's full stack is
the scaling story: table format, object store, query engine, event bus, workflow engine. None
of it changes a single number in the output, nobody looking at a forecast or a review set
can see it, and each adds a service to run, configure and debug. What the output does depend
on is narrower: tables written and read back exactly, raw payloads encrypted per tenant so
erasure is real, a consent state machine, and a fetch path that a real AA client can drop
into. That is what is built. Do not re-add the stack without a requirement the output can
show, such as data volume one process cannot hold or more than one writer.

- **Tables: Parquet on disk plus DuckDB, not Postgres.** The Compose base
  (`infra/compose.base.yml`) does not exist; `infra/` holds only Track B's
  `compose.intelligence.yml`, so there is no Postgres container to reuse. Parquet keeps the
  Polars frames as they are (no type mapping), and DuckDB queries them in process. Layout
  `tables/<schema>/<name>/<partition>/part-<seq>.<k>.enc`; partitioning by tenant makes
  erasure a directory removal. Every part is encrypted (see "Derived tables are encrypted"
  below). Contract tables are validated on every write, on every row; outputs
  with no contract table go to `aux.*` with the producing `run_id`
  (`platform/src/matsya_platform/store/`). `build()` and `match()` stay pure:
  `store/persist.py` takes their results and writes them.
- **A3 raw store.** Each payload is AES-256-GCM under its tenant's 256-bit data key, with
  tenant and payload id as associated data. Tenant keys are stored only wrapped (RFC 3394
  AES key wrap) under a master key read from `MATSYA_MASTER_KEY` (see `.env.example`); unset
  or malformed, the store refuses to start, with no default. Destroying a tenant key
  overwrites and deletes its wrapped file, which makes every object of that tenant
  unreadable, copies included. Home-made crypto (an HMAC keystream) was rejected: the erasure
  claim is only as credible as the cipher behind it, so it uses the `cryptography`
  library's audited primitives.
- **A1 consent.** States pending, active, paused, revoked, expired; transitions in
  `consent/service.py`. A terminal state triggers erasure. When no other consent of the tenant
  is live, the key is destroyed and the tenant's derived rows are removed; `ops.consents`
  and `ops.events` are kept as the record. A change during a fetch makes the in-flight
  payloads be discarded, not stored.
- **A2 fetch is a stub.** There is no AA sandbox. `fetch/adapter.py` reads a local folder of
  rendered payloads; a real AA client replaces that one adapter. Retries with backoff, and
  idempotency by payload sha256, are in the runner.

- **The loop goes through consent, fetch and the raw store.** Each run opens an active
  consent; each rendered delivery is fetched through A2 (`LocalFolderAdapter`, one delivery
  at a time) into the encrypted raw store, and the ledger is built from the payload read back
  and decrypted from there. A path revokes the consent during a fetch: that payload is
  discarded, nothing after it is fetched, and the erasure runs.
- **The `duplicate_payload` fault changed.** A repeat delivery is byte-identical to its
  original, so the raw store's payload-hash check drops it at fetch: it is never stored and
  never built. Its expected outcome was "built, no new rows, one new bank_reported balance
  version (the repeat's fetch stamp)"; it is now "deduplicated, nothing built, no new
  bank_reported version", and the expected balances no longer carry that version. For loss
  accounting a dropped repeat inherits its original's flags (it is the same bytes).
- **Persistence is on the real path.** The eval loop persists every build as it runs, the
  ingested invoices, the reference tables and the matcher's output, then scores what it reads
  back from the store, not in-memory frames. On a real world the stored transactions,
  balances and coverage equal the in-memory ones exactly. Row lineage (which payload and row
  each ledger row came from) is scoring-only and stays in memory: it is no contract table.
- **Export for Track B.** `store/export.py` writes every Track A table, empty ones included,
  as `<namespace>.<table>.parquet` in one directory, the layout Track B's `contract` source
  reads; the loop writes it to `<out>/contract/`. Checked against a line-for-line copy of
  `read_contract_dir`, not Track B's function itself (its module needs `yaml`, which the
  platform environment does not have).

**Derived tables are encrypted under the tenant key (2026-10-25).** Until this change the
table store wrote plain Parquet, so destroying a tenant key made the raw payloads noise and
left every ledger row, invoice, match and label readable: the erasure claim held for half the
data. Now every table part is AES-256-GCM under the partition's tenant key, with table and
partition as associated data, so key destruction covers raw payloads and derived tables
alike. Two exceptions use the platform key (`keys.PLATFORM`, wrapped under the master key,
never destroyed by a tenant's erasure):
- `ref.buyers` and `ref.buyer_disclosures`, which have no tenant. They are public reference
  facts, but encrypting them costs nothing and keeps the rule "no plaintext Parquet under the
  store" without exceptions. A `local` (no PAN) buyer row is tenant-derived and survives the
  tenant's erasure this way; see the open question in the task report.
- The consent record (`ops.consents`) and lifecycle events (`consent_changed`,
  `fetch_failed`, `erasure_completed`) in `ops.events`: the proof that consent existed and
  was ended must outlive the erasure. Every other event is under the tenant key.

Reads decrypt to memory. DuckDB cannot read encrypted Parquet and, without pyarrow, cannot
be handed in-memory frames, so `query()` decrypts only the tables the SQL names into a
temporary directory in `/dev/shm` (RAM-backed), runs the query and removes the directory.
The erasure proof destroys a tenant's key, then from a fresh process (nothing cached) tries
every stored object of that tenant, raw and derived, with a regenerated key, the other
tenant's key and the platform key: every one fails a real AES-GCM decrypt; the other tenant
reads back unchanged; and no plaintext Parquet is left anywhere under the store. All three
stores in a process must share one `KeyStore` (enforced), because a second instance's key
cache would keep decrypting a destroyed tenant.

The export for Track B (`<out>/contract/`) is plaintext by necessity, since Track B's reader
opens plain Parquet. It is a hand-off outside the store: erasure does not reach it, and
whoever holds that directory must re-export or delete after an erasure.

**Trino is cut.** DuckDB reads Iceberg directly, the handbook's own fallback if Trino is cut,
and the A8 privacy gate can be DuckDB views. Trino would add a container, a catalog
configuration and a second copy of the S3 credentials, and change nothing in the output.

**Iceberg (Step A), corrected.** pyiceberg 0.12.0 with pyarrow 26.0.0 (163 MB on its own; 429
MB for the whole environment) and SQLAlchemy was tried in a scratch environment and not added.
Observed: after a delete, time travel to an older snapshot returns the deleted rows;
`expire_snapshots` works but deletes no files; the data file and every earlier
`metadata.json` stay on disk and read back the deleted rows; there is no orphan-file removal.

The earlier conclusion, that this makes Iceberg's snapshot-erasure test impossible to pass,
was wrong in its framing. That problem exists only because the derived tables were
plaintext. With per-tenant encryption of table files, old snapshots, expired metadata and
orphan files of an erased tenant are all ciphertext under a destroyed key, so they are noise
and the problem goes away. That is why the handbook pairs crypto-shredding with Iceberg
rather than treating them as alternatives. (Iceberg's own data files would need encrypting
the same way, which pyiceberg does not do for us; Parquet modular encryption or our own
envelope would be needed.)

Iceberg stays cut on cost, not on correctness: the pyarrow weight; a catalog to run and keep
consistent; and expiry plus orphan cleanup are still needed for deletes that are not
key-based, such as erasing one consent's data while the tenant stays active, where the
tenant key must survive. pyiceberg 0.12.0 provides expiry only, so that cleanup would be
ours to write.

**Final storage decision.** Local files: Parquet, queried in process with DuckDB, with
per-tenant envelope encryption (AES-256-GCM data keys, RFC 3394 wrapped under a master key
from the environment) covering both raw payloads and derived tables, so destroying a tenant
key is erasure of everything the store holds for that tenant. Not taken:
- **MinIO.** Same bytes behind an S3 API. Encryption is ours either way, so it adds nothing
  to erasure, and nothing a viewer sees changes. Worth it only for a cloud deployment or more
  than one writer machine.
- **Iceberg.** Cost, as above: pyarrow weight, a catalog, and non-key-based deletes still
  need expiry and orphan cleanup we would write ourselves. Time travel is the one real
  capability it adds, and the demo does not replay old snapshots.
- **Trino.** A container and a configuration for SQL DuckDB already runs in process; the
  privacy gate can be DuckDB views.

Limitations:
- A revoke while another consent of the tenant is live deletes that consent's raw payloads
  but not the ledger rows built from them: rows carry no payload lineage. Fixing it needs a
  contract change.
- Tenant key files must never be included in backups: a backup of the wrapped key plus the
  master key would undo an erasure.
- The mid-fetch discard holds only within a single process: the consent generation that
  detects a change during a fetch is held in memory.
