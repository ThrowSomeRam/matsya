# Track A: state of the work (2026-10-25)

A handover for someone who has never seen this project. It says what Track A has built, how
well it is known to work, what it does not do, and what is open. Every number names where it
came from. Longer treatments live in `docs/matcher-design.md` (the matcher),
`docs/invoice-ingest.md` (invoice adapters) and `docs/track-a-contract-positions.md`
(contract positions, gaps F1 to F16, storage decisions); this file points at them rather than
repeating them.

State of the repository: the persistence wiring, export, encrypted table store and fetch path
(sections 2.7 to 2.10) are committed in `c69a7cc` on `main`. The object-store change (MinIO
backend, DuckDB reading encrypted Parquet in place, store history and reproducible exports;
section 2.7) came after it.

## 1. What this project is

**Problem.** Hackathon problem statement F-03: a cash-flow engine for Indian micro and small
enterprises that sell on credit to larger buyers. It should tell the owner, with honest odds,
when the bank balance is likely to go below zero, why, and the cheapest set of actions that
keeps it above zero. Bank data arrives with consent through India's Account Aggregator (AA)
network; invoices come as GST e-invoices or accounting exports (Tally, CSV). Source:
`docs/Handbook.pdf`, "What we are building".

**Two tracks, split at the data contracts.** Source: Handbook, "Two-person development tracks".
- **Track A, the platform plane** (this document): everything that makes data trustworthy and
  the system operable. Handbook components A1 to A10: consent, AA fetch, raw store and keys,
  ledger builder, narration parser, invoice ingest and matcher, buyer disclosures, privacy
  gate, orchestration, drafting.
- **Track B, the intelligence plane**: categoriser, payment-timing model, priors, Monte Carlo
  engine, calibration, scenarios, decision engine (`intelligence/`). Track A does not edit it.

**The seam.** The tracks share only `contracts/` (`matsya_contracts`): one pydantic row model
per table, Polars schemas derived from them, ID functions and enums. Neither imports the
other's code (Handbook, "Data contracts"; `contracts/README.md`). Track A writes the
`ops.*`, `raw.*`, `ledger.*`, `labels.*` and `ref.*` tables; Track B writes `features.*`,
`forecast.*` and `decisions.*` (`writer` attribute on each model in
`contracts/src/matsya_contracts/tables.py`). Track B reads Track A's tables from a directory
of flat files named `<namespace>.<table>.parquet` (`intelligence/src/matsya_intelligence/io/source.py`,
`read_contract_dir`); Track A's export writes exactly that (section 2.9).

## 2. What is built, component by component

Test counts are collected tests (`pytest --collect-only`), as of this writing. Totals:
platform 201 (the store tests run once per backend: locally 161 pass, 39 MinIO cases skip and
1 strict xfail pins contract gap F1; with MinIO configured 199 pass, 1 local-only skip, the
xfail), eval 34, contracts 101.
"Wired" means used by a running path: the eval loop (`python -m render.cli
loop|score|sweep`, section 5), and the product pipeline (section 2.11), which runs every
component below on a schedule.

### 2.1 A4 ledger builder
- **Does:** parses ReBIT AA deposit payloads into one deduplicated transaction list per
  account, with bank-reported and reconstructed daily balances, quarantine of bad rows, and
  coverage (the date through which each account's fetched ranges are unbroken). Money is
  integer paise via `Decimal`. A balance-continuity break that is not accepted fails the
  batch; everything else is flagged, not dropped.
- **Files:** `platform/src/matsya_platform/ledger/` (`build.py` entry `build(payloads,
  state)`, `rebit.py`, `ids.py`, `dedup.py`, `continuity.py`, `balances.py`, `quarantine.py`).
  `build()` is pure: no I/O.
- **Wired:** yes, every delivery in the loop goes through `build()`.
- **Tests:** 46 (`tests/ledger/test_build.py` 26 including the F1 xfail, `test_rebit.py` 16,
  `test_ids.py` 4).
- **Thin spots:** the handbook's "score each source's data quality" is only a few fixed caps
  (`dq_score` 0.5 inside an accepted gap or for an unverifiable payload, lowered for
  unresolved duplicate suspects), not a per-source score. The handbook test "Berka with
  injected duplicates" is not done by Track A; Berka is used only in Track B's benchmarks
  (`eval/models/`).

### 2.2 A5 narration parser: not built
There is no narration parser component and nothing writes `ledger.parsed`. Narration is read
only inside the matcher: buyer GSTIN (with checksum), PAN, names and TReDS platform names in
`match/buyers.py`, invoice references in `match/references.py`. The contract's `Gstin` type
comment says the checksum "is checked by the narration parser (A5)"; in fact the matcher
checks it. The handbook's per-rail grammar (UPI, NEFT, RTGS, IMPS, card, ATM, charges) and
golden files per bank do not exist.

### 2.3 A6 invoice ingest and its three adapters
- **Does:** turns an invoice file into `ledger.invoices` rows. The adapter parses the format
  into format-neutral lines; a shared core computes `invoice_id`, fiscal year and terms,
  resolves the buyer (GSTIN through PAN, else exact then fuzzy name), checks the seller,
  drops duplicates and validates against the contract. Unknown terms give
  `terms_source = unknown` and a due date of issue + 45 days (`UNKNOWN_TERMS_DAYS`, marked
  `# VERIFY:` as the MSMED Act s.15 maximum).
- **Files:** `platform/src/matsya_platform/ingest/`: `invoices.py` (core and interface),
  `fields.py` (rupee and date parsing), `registry.py`, and the adapters `invoices_csv.py`
  (generic owner CSV), `tally_daybook.py`, `gst_einvoice.py`.
- **Wired:** yes, all three, in the loop (each rendered invoice file is ingested through
  `registry.adapter(name)`).
- **Tests:** 45 (`tests/ingest/`: CSV 27, Tally 12, e-invoice 6), plus loop tests in
  `eval/platform/tests/test_render.py`.
- **Unverified:** the Tally day book layout and the e-invoice JSON shape are built from the
  formats as understood. Neither has been checked against a real file; nothing in the repo
  pins either shape. The round trip passing proves only that our renderer and our reader
  agree (`docs/invoice-ingest.md`). Zoho, named in the handbook, is not built.

### 2.4 A6 matcher
- **Does:** links bank credits to invoices (`ledger.matches`) and writes payment-timing labels
  (`labels.invoice_outcomes`), allowing for TDS, part-payments, bundles and TReDS. Section 4.
- **Files:** `platform/src/matsya_platform/match/` (`engine.py` entry `match(...)`,
  `config.py`, `buyers.py`, `references.py`, `review.py`). `match()` is pure.
- **Wired:** yes, run once per scored loop, at the cut.
- **Tests:** 34 (`tests/match/test_matcher.py`).

### 2.5 A3 raw store and keys
- **Does:** stores every original payload byte encrypted per tenant. Each tenant has a random
  256-bit data key, stored only wrapped (RFC 3394 AES key wrap) under a master key read from
  the environment variable `MATSYA_MASTER_KEY` (`.env.example`); unset or malformed, the
  store refuses to start. Payloads are AES-256-GCM with tenant and payload id as associated
  data. Destroying a tenant key overwrites and deletes its wrapped key file. A platform key
  (`keys.PLATFORM`) covers data that must outlive a tenant (section 2.7).
- **Files:** `platform/src/matsya_platform/store/keys.py`, `store/raw.py`. Uses the
  `cryptography` library, not home-made crypto. Payload blobs live in the object store
  (section 2.7); keys stay on local disk and never go to the bucket.
- **Wired:** yes, the loop fetches every delivery into it and builds from the decrypted bytes
  (uncommitted).
- **Tests:** in `tests/store/test_store.py` (30 tests in total for 2.5 to 2.9): 5 on keys, 5 on
  erasure and object integrity.

### 2.6 A1 consent and A2 fetch
- **Consent does:** a state machine over the contract's states (pending, active, paused,
  revoked, expired) with fixed transitions; every change rewrites `ops.consents` and appends a
  `consent_changed` event; a terminal state triggers erasure; expiry is checked on transition
  and by `expire_due(now)`. A change during a fetch makes `commit_fetch` discard the
  in-flight payload (a generation counter, held in memory, so this holds only within one
  process). File: `platform/src/matsya_platform/consent/service.py`.
- **Fetch does:** a runner (`fetch/runner.py`) with retries, doubling backoff, a
  `fetch_failed` event per failed attempt, and idempotency by payload sha256. The adapter
  (`fetch/adapter.py`, `LocalFolderAdapter`) reads a local folder of rendered payloads.
  **There is no AA sandbox access**; this is a stub, and a real AA client would replace that
  one adapter class.
- **Not built from the handbook's A1/A2:** reading AA consent notifications, scheduling fetches
  from the consent's frequency, Temporal workflows, routing around unhealthy banks.
- **Wired:** yes, uncommitted: each loop run opens an active consent and fetches each delivery
  through the runner. A loop path revokes mid-run (tested, not part of normal scoring).
- **Tests:** 9 consent and 4 fetch tests in `tests/store/test_store.py`; 3 loop tests in
  `eval/platform/tests/test_render.py`.

### 2.7 Table store: encrypted per tenant, with history, on local disk or MinIO
- **Does:** writes and reads every table `build()`, ingest and the matcher produce. Layout
  `tables/<schema>/<name>/<tenant or _shared>/<part|snap>-<seq>-<commit ns>.<k>.parquet` in
  an object store: a local folder by default, or MinIO when `MATSYA_S3_ENDPOINT` is set
  (`store/objects.py`, `.env.example`, `infra/compose.platform.yml`). Each part is Parquet
  with modular encryption (AES-GCM, encrypted footer), written by DuckDB under the tenant key
  (`k = t`) or the platform key (`k = p`: `ref.buyers`, `ref.buyer_disclosures`,
  `ops.consents`, lifecycle events).
- **DuckDB reads in place.** Frames cross to DuckDB as Arrow C streams (no pyarrow); reads and
  `query()` hand DuckDB the part URLs and the key in memory, so no plaintext file is written
  anywhere and a query reads only what it needs, over S3 too. A key destroy bumps
  `KeyStore.epoch` and the store reopens its DuckDB connection (tested).
- **History.** A replace keeps what it supersedes; `read(..., as_of=t)` and
  `query(..., as_of=t)` return what the store held at t. History is deleted only by a tenant's
  erasure and by revoking one consent of a tenant with another live one; both leave a purge
  marker, and reads as of earlier times then raise. The export is reproducible through it
  (section 2.9).
- **Guards:** the S3 backend refuses a bucket with versioning or object lock. Contract tables
  are validated on every row on write and cast back to the contract schema on read. All stores
  in a process must share one `KeyStore`, enforced.
- **Weak spot:** a part has no associated data, so one moved between two tables of the same
  tenant would decrypt (moved across tenants it fails, and reads check `tenant_id`).
- **Wired:** yes: the loop persists as it runs and scores what it reads back. The loop uses
  the local backend unless `MATSYA_S3_ENDPOINT` is set. Row lineage stays in memory (F2).
- **Tests:** in `tests/store/test_store.py`, every test once per backend (MinIO cases need
  `MATSYA_TEST_S3_*`), including as-of reads after appends and replaces, `ref.buyers`
  history, the revoke purge, an erased tenant gone from history while the other tenant's
  history reads back unchanged, and an export rebuilt identically at its recorded time.
  Write-up, and the Iceberg trial that was reverted: positions doc, end of section H.

### 2.8 Erasure
A terminal consent deletes that consent's raw objects. When no other consent of the tenant is
live, it also destroys the tenant key and removes the tenant's table parts, keeping only the
platform-key consent record and lifecycle events, and writes `erasure_completed`. The proof
(`test_erasure_proof_covers_raw_and_derived_tables`): after destroying tenant A's key, a
fresh process tries every stored object of A, raw and derived, under a regenerated A key, B's
key and the platform key, with real AES-GCM decrypts; all fail; B reads back unchanged; no
plaintext Parquet exists under the store. A soft delete (a flag, or a key left on disk) would
fail it.

### 2.9 Export for Track B
`store/export.py` writes every Track A table, empty ones included, as
`<namespace>.<table>.parquet`, validated. The loop writes it to `<out>/contract/`. Checked
against a line-for-line copy of Track B's `read_contract_dir`, not the function itself (its
module needs `yaml`, absent from the platform environment). The export is plaintext and
outside the erasure. It is a read of the store as of one time (`as_of`, default now),
recorded with a sha256 per file in `_export.json`; re-exporting at that time writes the same
files, or raises if an erasure or revoke purged history in between. A forecast is
reproducible once Track B records `store_as_of` (section 9). Tested by 2 tests in
`tests/store/test_store.py`.

### 2.10 The eval renderer and scoring loop
- **Does:** renders a Track B generator world into the files Track A would receive (ReBIT AA
  JSON payloads per fetch window; invoice files in three formats), injects named faults,
  writes the expected tables and a fault log, then runs the whole Track A pipeline and scores
  it. Section 5.
- **Files:** `eval/platform/render/` (`cli.py`, `world.py`, `windows.py`, `aa_rebit.py`,
  `csv_generic.py`, `tally_daybook.py`, `gst_einvoice.py`, `faults.py`, `truth.py`,
  `score.py`, `sweep.py`).
- **Tests:** 28 (`eval/platform/tests/test_render.py` 27, `test_sweep.py` 1). The suite takes
  about 43 s locally and 87 s with the store on MinIO (21 s before the object store, 14 s
  before encryption and the fetch path). Most of it is the sweep test's 108 runs.
- **Needs** `MATSYA_MASTER_KEY` set for `score`, `loop` and `sweep`; the tests set a random one
  per test (`eval/platform/tests/conftest.py`).
- The `score.py` module docstring still says invoices are ingested "with the generic CSV
  adapter"; it uses all three now.

### 2.11 The pipeline: every component as one scheduled run
- **Does:** `python -m matsya_platform.pipeline -c pipeline.toml run` (also installed as
  `matsya-pipeline`) runs, per tenant under an active consent: consent expiry, A2 fetch into
  the encrypted raw store, A4 build, reference tables, A6 invoice ingest from an inbox (one
  folder per adapter), the A6 matcher, persistence; then one export for Track B. Config is
  one TOML file (`pipeline/config.py` docstring). Files: `platform/src/matsya_platform/pipeline/`
  (`run.py`, `config.py`, `__main__.py`).
- **Automation:** `run` is what cron calls; `run --every N` stays up and repeats;
  `replay --start --end --step-days` drives a simulated clock (a year of weekly runs on the
  `loop` world takes 48 s and ends at the same 394 links and 12 review items as one run).
  A lock file under the store root refuses an overlapping run (exit 2). `consent
  grant|pause|resume|revoke` stands in for the AA consent flow; `status` prints consents,
  payloads applied or failed, and the last run.
- **Incremental and idempotent.** The builder's state is rebuilt each run by replaying the
  applied payloads from the raw store (`aux.build_log` holds their order), because it cannot
  be rebuilt from the ledger tables (F2); then only new payloads are built. `aux.ingest_log`
  keeps each invoice file's sha256. The matcher appends only match rows and labels that
  differ from their latest stored version. A repeat run at the same time writes nothing but
  the export (tested). A payload whose balances break continuity is logged once as failed
  and never applied or retried: accepting a gap needs the owner (`ops.owner_inputs`, not
  built).
- **Consent rules:** no active consent, nothing runs for the tenant, invoices included, so an
  erased tenant's inbox cannot bring its data back (tested).
- **Tests:** 6 in `eval/platform/tests/test_pipeline.py`: weekly runs end exactly where one
  run over everything ends (transactions, balances, invoices, matches, labels, on a tiny world
  with buyer payments added), a repeat run writes nothing, a broken payload is logged once and
  never applied, nothing runs or returns after a revoke, an overlapping run is refused, and the
  command line runs a replay.
- **Limits:**
  - The AA source is still the folder stub (`LocalFolderAdapter`, fetching up to `now`).
  - The replay feeds the year's invoice files on day one, so invoices are known before their
    issue date. The matcher now leaves out invoices issued after its `as_of`
    (`match/engine.py`); before this they produced labels censored after they were known,
    which the label validator rejects. A realistic replay would drop files in over time.
  - Replaying every applied payload makes each run O(payloads held).
  - The lock is per machine, so with MinIO and two machines it does not hold.
  - `ledger.matches` cannot retract a link: if a later run stops making a link, the earlier
    row stays the latest version Track B reads (a contract gap, not yet in the F list).
  - Without Track B's categoriser (`txn_category` in the config) the category gate is off;
    on the `loop` world that is 12 review items against 5 with the oracle categories.

## 3. The data model

Source: `contracts/README.md`, `contracts/src/matsya_contracts/` and positions doc sections A
and B.

- **Writers.** One writer per namespace (above). Track A writes `ops`, `raw`, `ledger`,
  `labels`, `ref`.
- **Money:** integer paise (`int64`), never floats, everywhere a column ends in `_paise`.
- **Signed amounts:** `ledger.transactions.amount_paise` is + credit, - debit, never zero;
  `direction` is redundant and must agree. Zero amounts and sign/direction mismatches are
  quarantined to `ops.dq_quarantine` with a `dq_alert` event, not written.
- **Two times.** `value_date` is when a transaction happened; `known_at` is when we learned it
  (the AA fetch time; every row of one fetch shares one `known_at`). Timestamps are UTC; IST
  applies only at display and in date derivation. An as-of date means the end of that IST day
  (18:29:59.999999 UTC; `ledger/build.py`, `as_of_cutoff`).
- **Append-only as-of tables:** `ledger.balances`, `ledger.invoices`, `ledger.matches` and
  `labels.invoice_outcomes` carry `known_at` in their key; an as-of read takes, per key, the
  latest row with `known_at <= as_of`. A correction is a new row, never an update.
  `ledger.balances` also carries `balance_source` (`bank_reported | reconstructed`); a
  reconstructed version's `known_at` is the fetch time of the build that produced it (positions
  doc A6; F6 records that the contract comment still states the old rule).
- **Content-hashed IDs**, opaque, never parsed (`contracts/src/matsya_contracts/ids.py`):
  `txn_uid` = sha256(tenant | account | k)[:32], with k the first available of:
  - `txn:<bank txnId>|<value_date>|<amount>`: banks reuse txnIds, so the id alone would merge
    two real transactions and lose money; date and amount separate them.
  - `utr:<UTR or RRN>|<amount>`: one UTR can cover a payment and its bank charge, hence the
    amount. **This path has no date**, contrary to a common summary of the scheme; a UTR is
    unique enough across dates. One `utr:` key per payload; a second row with the same UTR
    and amount takes the next path.
  - `bal:<running balance>|<value_date>|<amount>`: a running balance repeats across dates,
    hence date and amount.
  - `ord:<value_date>|<amount>|<normalised narration>|<ordinal>`: last resort; two identical
    same-day credits must not collapse, but an ordinal shifts when a later fetch covers a
    different window (F3 records a case where it collapses real rows).

  `invoice_id` = sha256(tenant | supplier GSTIN | normalised invoice no | fiscal year), never
  IRN. `buyer_id` = sha256(PAN) when the PAN is known (global), else a tenant-scoped hash of
  the name (`id_basis = local`).
- **Labels: `censored_at` beats `paid_date`.** `paid_date` is the first date allocated plus
  inferred TDS reaches gross minus tolerance; `censored_at` is set exactly when `paid_date` is
  null or the settlement is a TReDS discount, and when set the label is censored regardless
  of `paid_date` (validator in `tables.InvoiceOutcome`). A TReDS discount is cash to the
  supplier but not a buyer payment, so treating it as paid would teach the timing model a
  fast buyer that is not fast.

## 4. The matcher

Full design: `docs/matcher-design.md`. Summary from `match/engine.py` and `match/config.py`.

**Cascade, in order** (each stage removes what it resolves):
0. Buyer resolution from the narration: checksum-valid GSTIN, PAN inside a GSTIN, exact
   name, unique close name; TReDS platform credits name the platform. A credit with no buyer,
   no platform rail and no invoice reference is not a receipt and is ignored. Candidate
   window: 120 days after issue. If `features.txn_category` is available and labels the
   credit confidently (>= 0.7) as anything but a customer receipt, the credit is skipped
   (the category gate).
1. Reference match: an invoice number in the narration fixes the invoice; arithmetic must
   still explain the rupees (gross, a TDS hypothesis, a TReDS discount or a part-payment).
2. Exact amount, accepted only if exactly one open invoice fits within Rs 2.
3. TDS-explained: each rate in {0, 0.1, 1, 2, 10} percent (marked `# VERIFY:`) on each base
   in {taxable value, gross}.
4. Part-payments: accepted with a reference; without one they go to review.
5. Bundles: up to 4 invoices among the buyer's 15 nearest open ones, brute-forced; accepted
   only if exactly one subset fits under one TDS hypothesis.
6. TReDS: a short platform credit before due is a financier discount; full net on a platform
   rail is the buyer at any date; short after due is unknown.
7. EM-style habit learning: each buyer's usual (rate, basis) from accepted fits, re-match with
   a penalty off-habit, up to 3 passes; order-independent (tested).
8. Confidence by margin to the runner-up; small margins go to review.
9. Outputs: matches, one label version per invoice, review set, events, per-invoice evidence
   strength.

**TDS basis as evidence, and why it works.** TDS must be computed on the taxable value
excluding GST when the invoice shows GST separately. An advance is paid before any invoice
exists, so there is no separately shown GST and the deduction falls on the whole amount. So a
credit that fits an invoice only on the gross basis, from a buyer whose learned habit is
taxable value, is advance-shaped and goes to review ("basis evidence"). It is on by default
(`basis_evidence = True`), with its own 5-paise tolerance (`basis_tolerance_paise`) because
the Rs 2 amount tolerance would swallow the gap between the bases on small invoices. The rule
was confirmed by the project owner; it was not checked against the statute here. No signal
exists at 0 percent TDS or when rate x GST is within 5 paise; such links are labelled
`arithmetic_no_basis_signal`, the weakest evidence.

**What goes to review, and why.** Part-payments without a reference, ambiguous fits (a
runner-up within Rs 2), bundles that are not unique, basis-evidence advances, a credit
displaced by a later referenced payment ("settled-invoice contradiction"), and under the
`balanced` or `strict` settings, advance-shaped or all arithmetic-only links. Invoices in a
review item get no label rather than a guess, because a wrong link teaches a wrong paid date
while an unmatched credit only leaves an invoice censored. The review set is ranked by
at-risk rupees x (1 + days the paid date would move / 30) / sqrt(1 + the buyer's paid
invoices) (`match/review.py`).

**Measured now.** Source: a grid run while writing this document with the current
(uncommitted) code and default config (`permissive`, category gate on, basis evidence on),
open loop, `batch_fails` partial mode, only the `coincidental_credit` fault, advances taxed on
gross; 4 worlds x 3 fault seeds = 12 runs per level; output saved at
`~/.cache/matsya-state-grid.parquet` (local, not in the repo; the run was a one-off script
calling `render` and `score`, and the baseline figures exist only in each run's `score.json`
under `matching.baseline`). Caveats that belong to every number below:
- The worlds are Track B generator worlds on this machine only (`~/.cache/matsya-world-loop`,
  `-s1`, `-s2`, `-dense0`; generator seeds 0, 1, 2 and a denser seed-0 world). `dense0` shares
  its buyers and truth invoices with `loop` and gives identical matcher numbers, so for the
  matcher there are 3 distinct invoice books, not 4.
- At 0 percent injection the fault seed changes nothing, so the 12 runs are 4 distinct results.
- The categoriser gate uses the true category with confidence 0.9 as a stand-in for Track B's
  categoriser (an oracle), so gate effects are an upper bound.
- The generator's buyers deduct TDS at the rates and bases we model, and put the invoice
  number in about 70 percent of payment narrations; these are favourable conditions.

At 0 percent injection, per distinct world (gold links = truth payments of ingested invoices
whose credit is in the ledger):

| world | links made | precision | recall (n gold) | review share | baseline links | baseline recall |
|---|---|---|---|---|---|---|
| loop | 394 | 1.000 | 0.987 (399) | 0.013 | 47 | 0.118 |
| s1 | 324 | 1.000 | 0.991 (327) | 0.009 | 0 | 0.000 |
| s2 | 344 | 1.000 | 0.948 (363) | 0.052 | 57 | 0.157 |

The baseline is "exact gross": a credit from a resolved buyer links to the one open invoice
whose gross equals the credit exactly (`score._baseline`). It is weak by design (it ignores
TDS); every link it makes is correct, and on s1 it makes none. TDS rate and basis are right on
every accepted link (accuracy 1.000), payer kind on at least 0.988, no false paid labels.
Paid-date error on `loop` (n = 343 invoices paid in truth by the cut): matcher mean 1.55 days,
median 0, p95 0, max 324; baseline mean 150, median 140. Review share stays under the
5-percent target at 0 percent injection (0.9 to 5.2 percent), and the top 10 review items
cover on average 93 percent of the at-risk rupees.

Coincidental credits (advances at an exact invoice net; section 6 for before and after).

## 5. The test loop

Sources: `eval/platform/render/*.py` docstrings, `docs/renderer-review-for-track-a.md`.

1. **Track B's generator** (`intelligence/.../synth`, exported by
   `synth.export.write_contract_parquet`) produces a world: every contract table Track A would
   hold at the cut, plus generator truth (`truth/*.parquet`: transactions with the synthetic
   bank txnId and posting order, payments with their invoice links, buyers with their true TDS
   rate, invoices with invoice numbers). Track A never edits it.
2. **The renderer** turns that world into what Track A would receive: ReBIT-shaped AA payloads,
   one per fetch window (weekly cadence, 3-day overlap), and invoice files (about 50 percent
   CSV, 45 percent Tally day book, 5 percent e-invoice; one supplier using three formats is not
   realistic, it is how one render covers all adapters). It adds no facts, only format.
3. **Faults** (`faults.py`), each with a rate and its own seeded stream: duplicate payloads,
   dropped ids, balances or summaries, identical rows, split-day windows, partial windows
   (`batch_fails` or `accept_gap`), duplicate invoice rows, missing GSTINs, and coincidental
   credits. Every fault is logged with its expected pipeline behaviour (`fault_log.json`).
4. **Expected tables** (`truth.py`) are what our rules should produce from what was delivered,
   not the generator's tables; known differences are reported as named mismatches.
5. **The loop** (`score.py`): an active consent; every delivery fetched into the encrypted raw
   store and built; invoices ingested; the matcher run; everything persisted, read back and
   compared with expected tables, the fault log and generator truth; the export written.
6. **The truth map.** `row_map.parquet` maps every rendered row (payload id, row index) to its
   generator transaction. Through it, each ledger row is traced to the truth row it came from,
   so the scoring can say a row is missing, extra or wrong, and whether anything flagged it,
   independently of the expected tables: "silent" means wrong and unflagged, the failure that
   matters; "loud" means wrong but flagged (refused payload, unverified flag, reject).
7. **The sweep** (`sweep.py`) repeats this across fault levels, seeds, worlds, partial modes
   and open or closed loop (closed: fetch re-requests after a failure).

**What it proves:** that our own pipeline, on data in our understanding of the formats,
rebuilds what our rules say it should, says so when it cannot, and where it starts to be
silently wrong under each fault. **What it does not prove:** anything about real AA payloads,
real Tally or e-invoice files, real narrations, or real buyer behaviour. The generator, the
renderer and the readers all share our assumptions; agreement between them is not validation.

## 6. Measured results we can defend

- **Ledger stress sweep** (positions doc section G; 4 worlds x fault seeds 0 to 2, levels 0 to
  100 percent, both partial modes, open and closed loop). Lowest level at which any run turns
  silently wrong, min/median/max over world x seed: `batch_fails` combined 50/75/75 (3 of 12
  runs never); `accept_gap` partial_window never; `accept_gap` combined 50/50/75 (5 of 12
  never). Every single fault alone stays exact at every level. Measured before encryption,
  the fetch path and the duplicate-payload change; not re-run since. The remaining silent cases
  at 50 to 75 percent combined are undiagnosed.
- **Matcher at 0 percent injection:** section 4 table.
- **Coincidental credits, before and after the basis signal.** Before (variant "current" with
  advances taxed on gross, category gate off, basis evidence off; sweep output
  `~/.cache/matsya-sweep-basis-bcd`, 12 runs per level): at 5 percent injection 286 advances,
  114 settled an invoice wrongly, precision 0.960 to 0.988, recall 0.926 to 0.982; at 25
  percent 1,249 advances, 463 wrong, precision 0.873 to 0.914, recall 0.873 to 0.930. After
  (current defaults, the grid in section 4): at 5 percent 10 wrong, 104 caught by basis
  evidence, precision 0.991 to 1.000, recall 0.948 to 0.991, review share 5.9 to 11.8 percent;
  at 25 percent 46 wrong, 419 caught, precision 0.977 to 1.000, recall 0.937 to 0.991, review
  share 21 to 26 percent. Of the 46, all carry 0 percent TDS (checked directly in an earlier
  run with the gate off; the count is the same with it on), and all are labelled
  `arithmetic_no_basis_signal`. With no advances the signal flagged no real payment. The
  injection rate is a stress parameter, not an observed frequency: nobody has measured how
  often a real MSME receives such credits.
- **Invoice ingest** (`docs/invoice-ingest.md`; 12 runs, the same 3 distinct invoice books):
  every invoice rebuilt exactly in all three formats (CSV 2,598, Tally 2,358, e-invoice 261),
  no unexpected rejects, 190 duplicate rows all reported identical and dropped. Proves only
  that our renderers and readers agree.
- **Storage cost** (one dense0 run, 5 percent all faults; measured in the encryption task):
  DuckDB aggregate query 37.8 ms encrypted against 44.8 ms plaintext, join 45.4 against 49.9,
  full table read 23.3 against 29.6 (the encrypted store consolidates a table into one file
  per query, which outweighs decryption). A whole scored run takes 4.81 s, against 2.65 s with
  no store and about 2.9 s with the plaintext store; most of the increase is the fetch path
  (per-delivery consent checks, one encrypted file per event, 326 files per run). These
  figures predate in-place reads. After them (local backend, `python -m render.cli score`
  wall clock including startup, on a dense0 render at default rates): 9.6 s against 8.1 s on
  `c69a7cc`; on the test suite's small world, `score()` 0.65 s against 0.50 s. With history
  added: 8.9 s and 0.65 s (no measurable cost). The Iceberg trial took 19.8 s and 1.35 s. The
  query timings above were not re-measured.

## 7. Known limitations

From `docs/matcher-design.md` "Limitations", `docs/invoice-ingest.md` "Limitations" and
positions doc section H, plus what writing this document found.

- **Mislinks nothing contradicts.** When neither an advance nor the real payment carries a
  reference, nothing contradicts the link; it shows only as unexplained cash in aggregate.
  Review-set size and unexplained rupees, the only signals for it, are computed in scoring
  only (F13).
- **The 0 percent TDS blind spot.** About 10 to 12 percent of injected advances carry 0
  percent TDS; the basis cannot tell them from payments. Under the default `permissive` they
  settle the invoice, labelled weaker evidence; they reach review only under `balanced` or
  `strict`.
- **Bundles go to review** unless exactly one subset fits; the basis read does not apply to
  them. No accepted link and no review cause in the runs checked for this document came from
  the bundle stage (the last grid run's stages: reference 291, tds 93, exact 12; the basis
  sweep's review causes are part-payments and unexplained credits), so the bundle path is
  exercised by unit tests only.
- **Unverified formats.** The Tally and e-invoice adapters have never seen a real file; Zoho is
  not built; Tally's native export is XML and only one CSV day book layout is read.
- **Day book buyers resolve by name only**, and every world has unique buyer names, so the
  ambiguous-name reject never fires in the loop.
- **Oracle categoriser.** The category gate's measured effect uses true categories; Track B's
  categoriser will do worse.
- **Small n.** Matcher evidence is 3 distinct invoice books from one generator.
- **Erasure scope.** A revoke while another consent of the tenant is live deletes that
  consent's raw payloads but not the ledger rows built from them (rows carry no payload
  lineage; needs a contract change). The export for Track B is plaintext and outside erasure.
  Tenant key files must never be backed up (a backup plus the master key would undo an
  erasure). Buyers with no PAN (`id_basis = local`) are tenant-derived but sit in the shared
  `ref.buyers` under the platform key, so they survive the tenant's erasure.
- **Single-process assumptions.** The mid-fetch discard and the key cache hold within one
  process.
- **Label stamping cadence.** The matcher runs once at the cut, writing one label version per
  invoice. Track B's watermark rule expects every fetch to re-stamp open labels with
  `censored_at` = a lag-based watermark; Track A stamps coverage (`complete_through`) instead.
  The difference is reported as the named mismatch `watermark_vs_coverage` (`truth.py`); it is
  not resolved with Track B.
- **TDS rates and the TReDS fee share** are unverified constants (`# VERIFY:` in `config.py`).

## 8. Decisions and their reasoning

- **Reproducible forecasts without Iceberg** (positions doc, end of section H): the store keeps
  history and exports can be rebuilt at their recorded time. Iceberg was built, worked, and
  was reverted: about twice the run time (23 ms per commit), semi-internal pyiceberg API, and
  nothing the history does not give. Kept as a scaling item for many writers or large tables.
- **MinIO: added, for scaling** (positions doc, end of section H): one machine's disk does not
  take more than one writer. Crypto-shredding already covers residue, as long as the bucket
  keeps no old versions (enforced) and keys never go to it. DuckDB reads the encrypted parts
  from it in place. Supersedes the "No MinIO" part of the next item.
- **No MinIO, Iceberg, Trino, Redpanda or Temporal** (positions doc section H). They are the
  scaling story; none changes a number a viewer sees, and each adds a service. Storage is local
  files: Parquet queried in process with DuckDB, with per-tenant envelope encryption over both
  raw payloads and derived tables.
  - **What changed when derived tables were encrypted.** An Iceberg trial (pyiceberg 0.12.0, in
    a scratch environment) showed that deleting rows and expiring snapshots leaves the deleted
    rows readable from the data file and from old metadata, and pyiceberg has no orphan-file
    removal. That was first read as "Iceberg cannot erase". With table files encrypted per
    tenant, old snapshots and orphan files of an erased tenant are noise, so that objection is
    gone; it is why the handbook pairs crypto-shredding with Iceberg. Iceberg stays cut on cost:
    pyarrow (163 MB), a catalog to run, and expiry plus orphan cleanup still needed for deletes
    that are not key-based (one consent of a still-active tenant).
  - **MinIO:** same bytes behind an S3 API; encryption is ours either way. **Trino:** a
    container for SQL DuckDB already runs; the privacy gate can be DuckDB views.
- **Review rather than guessing.** A wrong link teaches the timing model a wrong paid date; an
  unmatched credit only leaves an invoice censored. Escalating ambiguous and non-unique cases
  trades recall for precision on purpose (`docs/matcher-design.md`, "Escalate rather than
  guess").
- **No LLM in the matching decision.** Handbook ground rule: no model decides a rupee figure.
  Extraction might propose candidates in future; arithmetic judges (`matcher-design.md`,
  "Rejected").
- **Brute force, not an integer program.** The handbook names OR-Tools; at under about 15 open
  invoices per buyer, exhaustive search within a 4-invoice cap is exact and instant.
- **The default `permissive` with the category gate: not a recorded decision.** `config.py`
  calls `strictness = "permissive"` a "placeholder default pending a decision", and
  `matcher-design.md` says the same. What is recorded: in the earlier sweep `balanced` halved
  wrong links at 5 percent injection but cost recall at 0 to 5 percent (it also catches early
  real payments), and `strict` reached precision 1.0 at recall 0.71 to 0.73 with a 27 to 55
  percent review share. The category gate is on because it removes non-receipt credits from
  the review set (at 0 percent injection the review set fell from 339 items to 96 across the
  runs of that sweep), measured with the oracle categoriser.
- **Basis evidence routes to review, not a cost penalty**: an advance usually fits exactly one
  invoice, so there is no runner-up for a penalty to lose to.
- **Bundles stay out of the basis read** (ambiguous across invoices; bundles already go to
  review).

## 9. Open contract gaps and asks for Track B

Full text: positions doc section F (items 12 to 16 sit after section G in that file).

1. `ledger.balances` key lacks `balance_source`; same-day bank and reconstructed rows collide.
2. `ledger.transactions` cannot be rebuilt into A4's working state (no lineage columns).
3. The ordinal key path can collapse real rows across a mid-day window boundary.
4. No table for coverage (`complete_through`).
5. No home for invoice ingest rejects.
6. The contract comment still defines reconstructed `known_at` the old way.
7. No `ops.events` kind for unverified payloads.
8. `ledger.invoices` has no invoice number (references are resolved by hashing spellings).
9. `MatchMethod` has no member per cascade stage.
10. `ledger.matches` has no TReDS discount fee.
11. Review-set invoices have no legal label (`settlement_kind = unknown` while open, or a flag).
12. No `ops.events` kind for a settled-invoice contradiction.
13. No home for matcher quality metrics (`ops.match_runs` proposed, not built).
14. Labels cannot carry link evidence strength (`evidence` field proposed).
15. `erasure_completed` cannot say what was erased.
16. `ledger.invoices` has no taxable value (round-off and other charges distort the TDS base).

Asks to Track B (positions doc section E, plus the end of section H): record the export's
`store_as_of` in `forecast.runs.input_snapshot_ids` so a forecast's inputs can be rebuilt;
review Track A's definitions in their namespaces
(`forecast.shock_state`, `forecast.calibration_state.as_of`) and ours (`ops.owner_inputs`,
`ops.dq_quarantine`, the `ops.events` payload models); set the CPSE/TReDS constant to
`verify`; use link evidence strength when fitting payment timing; send source links for
Income-tax Act 2025 s.393(1) and s.37(2)(g); confirm the hackathon deadline. Not yet agreed
with Track B: the `censored_at` definition (coverage against their watermark) and re-stamping
open labels at every fetch (their questions 1 and 2 in `docs/track-b-decisions.md`).

## 10. What is not built

Against the handbook's A1 to A10 and phases P0 to P5.

| item | state | decided skip or pending |
|---|---|---|
| A1 consent: AA notifications, fetch scheduling | not built | pending |
| A2 AA sandbox access, Temporal, bank-health routing | not built; folder stub only | Temporal skipped (section H); sandbox pending, no access |
| A3 Vault or KMS | master key from the environment instead | decided (minimal local KMS) |
| A5 narration parser, `ledger.parsed` | not built | pending |
| A6 Zoho adapter | not built | pending |
| A7 buyer disclosures pipeline (`ref.buyers`, `ref.buyer_disclosures` from annual reports) | not built | pending; the handbook allows moving it to Track B |
| A8 privacy gate | not built | Trino skipped; DuckDB views proposed, not built |
| A9 orchestration and observability (Dagster, Redpanda, OpenTelemetry) | partial: the pipeline (section 2.11) runs every component on a schedule (cron, `--every`), with a lock and a JSON run report; no Dagster, metrics or tracing | Redpanda skipped (section H); Dagster and OpenTelemetry not decided |
| A10 drafting service | not built | pending; the handbook allows moving it to Track B |
| owner input loader (`ops.owner_inputs`), `labels.txn_corrections` writer | not built | pending (agreed in positions doc B) |
| `ops.match_runs` metrics and alerting | not built | pending (F13) |
| Compose base and `compose.platform.yml` | `compose.platform.yml` built (MinIO and bucket init); no base file | Track B's file still points at the missing base |
| Track A CI | not built (only `.github/workflows/track-b.yml`) | pending |
| P4 chaos suite, scale test, naive-stack comparison | partial: the stress sweep covers AA fault injection; no scale test or naive-stack run | pending |
| owner view, dashboard (P5) | built: owner-view API (`platform/src/matsya_platform/api`, serves precomputed payloads, no auth) and owner dashboard (`dashboard/owner`, logic in `src/viewmodel.ts`); payloads come from Track B's demo precompute (`eval/models/owner_view_demo.py`), not from the pipeline | pending: auth and tenant isolation, payloads fed by the pipeline; payload asks in `docs/owner-dashboard-payload-gaps.md` |
| Berka loaded as a Track A tenant (P1) | not built | pending |

## 11. Next steps and unanswered questions

Immediate:
1. Commit the uncommitted work (persistence wiring, export, encrypted table store, fetch path
   in the loop), once reviewed.
2. Re-run the ledger stress sweep on the current loop, since the duplicate-payload behaviour
   and the storage path changed after section G was measured.
3. Agree `censored_at` and label re-stamping with Track B (section 9).

Unanswered questions from the most recent tasks:
- Buyers with no PAN in the shared `ref.buyers`: move them to the tenant partition or delete
  them on erasure? Possibly a contract change.
- Should an erasure re-export or delete the plaintext `<out>/contract/` hand-off, or does Track
  B own deleting its copy?
- Batch events per fetch to cut the 326 files and most of the added run time?
- Invoice adapters: vary Tally buyer names so fuzzy and reject paths run; one format per world
  instead of a mix; whether a later file may enrich an existing invoice (a Tally invoice later
  arriving as an e-invoice keeps no IRN today); whether to obtain a real, anonymised Tally
  export before going further.
- The strictness default (section 8) is still formally a placeholder.

## Contradictions found while writing this

- `docs/track-a-contract-positions.md` section D still lists "TDS base: taxable value vs
  GST-inclusive amount" as unverified and barred from the demo; the basis rule has since been
  confirmed by the project owner and the matcher relies on it.
- The same file says contracts v0.1 "is on branch `contracts-v0.1`"; no such branch exists
  (only `main`).
- F12 to F16 are printed after section G instead of in section F.
- The contract's `Gstin` comment says the checksum is checked by the narration parser (A5),
  which does not exist; the matcher checks it.
- `score.py`'s docstring says invoices go through the generic CSV adapter only.
- The UTR key path has no date (summaries that say all four include date and amount are wrong).
- The "4 worlds" in sweep descriptions are 3 distinct invoice books for the matcher (`dense0`
  shares `loop`'s invoices).
