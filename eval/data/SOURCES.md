# Benchmark datasets

Foreign datasets used **only to benchmark methods**. Nothing fitted on them may set a
parameter used on Indian businesses (handbook: open data only; CLAUDE.md). Raw files live in
`eval/data/raw/` (gitignored, never committed or redistributed). Fetch with
`uv run --project intelligence --extra bench python eval/data/download.py`; every file is
checked against a pinned sha256 in that script.

## Kaggle invoice payment dataset

- **Source:** https://www.kaggle.com/datasets/rajattomar132/payment-date-dataset, version 2
  (last updated 2022-05-17), `dataset.csv`, 50,000 rows, 19 columns. Downloaded 2026-10-25
  from the public endpoint `api/v1/datasets/download/rajattomar132/payment-date-dataset`.
- **Licence:** the uploader labels it **CC0: Public Domain** (Kaggle metadata API). A second
  copy of what appears to be the same data (`pradumn203/payment-date-prediction-for-invoices-dataset`)
  is labelled CC BY-NC 4.0. Neither states who produced the data; the schema (SAP-style
  `business_code`, `cust_number`, `doc_id`, `clear_date`, payment-term codes) and real customer
  names suggest an accounts-receivable extract from a company or a hackathon. An uploader's
  CC0 label cannot grant rights they do not hold.
- **Decision (project owner, 2026-10-25):** use it as planned, as an internal benchmark only,
  under the stricter reading (non-commercial, not redistributed, results cited with this
  caveat). Recheck before any public release of results.
- **Data facts that limit what it can test** (measured in `eval/models/kaggle_checks.py`):
  - 1,161 exact duplicate rows (dropped).
  - The 9,681 `isOpen = 1` rows are every invoice posted after 2020-02-27, with `clear_date`
    removed: a hidden test split, not unpaid invoices. They are not used as censored labels.
  - The labelled rows are **right-truncated, not censored**: an invoice appears only if it
    cleared within its `buisness_year` (2019 postings all cleared by 2019-12-31; Jan-Feb 2020
    postings by the 2020-05-22 extract). There are no unpaid invoices, so never-paid
    calibration cannot be measured here.
  - Amounts are USD and CAD; stored as integer cents in the `_paise` columns.

## Berka (PKDD'99 financial dataset)

- **Source:** CTU Prague Relational Learning Repository,
  https://relational.fel.cvut.cz/dataset/Financial, public MariaDB server
  (`relational.fel.cvut.cz:3306`, database `financial`, guest account published on that page).
  Tables `account, client, disp, card, district, loan, order, trans` exported 2026-10-25,
  ordered by primary key; 4,500 accounts, 1,056,320 transactions, 682 loans
  (A 203, B 31, C 403, D 45: good A/C 606, bad B/D 76).
- **Licence:** **none stated** on the dataset page or the repository. Anonymised data from a
  Czech bank released for the PKDD'99 Discovery Challenge; widely used for research and
  teaching. Cite: P. Berka, "Guide to the Financial Data Set", PKDD 2000 Discovery
  Challenge, and Motl & Schulte, "The CTU Prague Relational Learning Repository" (2015).
- **Decision (project owner, 2026-10-25):** use as an internal research benchmark, not
  redistributed.
- Amounts are in CZK, stored as integer hellers (x100) in the `_paise` columns.
- **Substitute copy (2026-10-25, when the CTU MariaDB server was unreachable over this
  environment's HTTPS-only network):** Kaggle `marceloventura/the-berka-dataset` (labelled
  CC0; the original PKDD'99 files with `;` separators and YYMMDD dates), converted to the export
  format above (`trans.csv`, `loan.csv` only: what the loaders read). `loan.csv` converts to
  exactly the pinned sha256. `trans.csv` has the same 1,056,320 rows but not the pinned bytes;
  against the committed outside-check features (682 loan accounts): loan statuses identical,
  transaction counts differ by up to 4 per account, balances by up to 0.5 CZK (90-day mean by up
  to 6.6 CZK). Results on this copy are compared only with runs on the same copy.
