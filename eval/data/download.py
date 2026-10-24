"""Download the foreign benchmark datasets into eval/data/raw/ and verify checksums.

    uv run --project intelligence --extra bench python eval/data/download.py [kaggle|berka|all]

Benchmarks only: nothing fitted on these sets may set a production parameter
(docs/track-b-decisions.md, SOURCES.md). Raw data is gitignored and never committed.

- kaggle: rajattomar132/payment-date-dataset version 2, public download endpoint (no
  credentials). The zip and the CSV inside it are checked against pinned sha256.
- berka: the "financial" database (PKDD'99) from the CTU relational repository's public
  MariaDB server (guest account published on the dataset page). Each table is exported
  ordered by its primary key to CSV; sha256 of each CSV is pinned below, so a changed
  upstream copy fails loudly instead of silently changing results.
"""

import csv
import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

RAW = Path(__file__).resolve().parent / "raw"

KAGGLE_URL = "https://www.kaggle.com/api/v1/datasets/download/rajattomar132/payment-date-dataset?datasetVersionNumber=2"
KAGGLE_SHA256 = {
    "payment-date-dataset-v2.zip": "df810331a2a6979394499b7a5ede79fbae870ad847d00103069bf2863315b5ac",
    "dataset.csv": "139482bdc3325a734474d08f48ceee71ab67c84b106f2a0c0fe9479d71be8431",
}

BERKA_DB = {"host": "relational.fel.cvut.cz", "port": 3306, "user": "guest", "password": "ctu-relational",
            "database": "financial"}
BERKA_TABLES = {  # table -> primary key order
    "account": "account_id", "client": "client_id", "disp": "disp_id", "card": "card_id",
    "district": "district_id", "loan": "loan_id", "order": "order_id", "trans": "trans_id",
}
BERKA_SHA256 = {  # first download 2026-10-25 (SOURCES.md)
    "account": "7b73fbf635edee867f48de2def090140631769e28898b822cd989504714ac95f",
    "client": "09f87f3c95cbcc77fbff92dbfe1361b63aa6a4800f569b8f329db9a8879b3916",
    "disp": "83f11ef399aaf45c9dca07e6dc506271f486575dc1a92413e2832cde4aa1f16b",
    "card": "8e5adf2e2a845abe78329ce93ee5ca25c3a68268a01a20eb66ae8fa48ef200b5",
    "district": "4b9ccdd2a37ada8a2edeffaf83f726883afd4f3731036ea9ff1619723c640718",
    "loan": "2ae8e45073ce5334112d0af74420fe9668a45d6430298e3f34bd725e122c4e96",
    "order": "de57bf457a103f0b6f6e23a54a2177c889ea0cac4c440bf534dd6c7950521426",
    "trans": "bc71313c967339139c54259d7e4b0c3f5e902b922c27b44d2ec55968d42f3281",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(path: Path, expected: str | None) -> str:
    got = sha256(path)
    if expected is not None and got != expected:
        raise SystemExit(f"checksum mismatch for {path.name}: {got} != {expected}")
    return got


def kaggle() -> None:
    out = RAW / "kaggle"
    out.mkdir(parents=True, exist_ok=True)
    z = out / "payment-date-dataset-v2.zip"
    if not z.exists():
        urllib.request.urlretrieve(KAGGLE_URL, z)
    check(z, KAGGLE_SHA256[z.name])
    with zipfile.ZipFile(z) as zf:
        zf.extract("dataset.csv", out)
    check(out / "dataset.csv", KAGGLE_SHA256["dataset.csv"])
    print("kaggle ok:", out / "dataset.csv")


def berka() -> None:
    import pymysql

    out = RAW / "berka"
    out.mkdir(parents=True, exist_ok=True)
    con = pymysql.connect(**BERKA_DB, charset="utf8mb4")
    try:
        for table, pk in BERKA_TABLES.items():
            path = out / f"{table}.csv"
            if not path.exists():
                with con.cursor(pymysql.cursors.SSCursor) as cur:
                    cur.execute(f"SELECT * FROM `{table}` ORDER BY `{pk}`")
                    cols = [d[0] for d in cur.description]
                    buf = io.StringIO()
                    w = csv.writer(buf, lineterminator="\n")
                    w.writerow(cols)
                    for row in cur:
                        w.writerow(["" if v is None else v for v in row])
                path.write_text(buf.getvalue(), encoding="utf-8")
            print(f"berka {table}: sha256 {check(path, BERKA_SHA256.get(table))}")
    finally:
        con.close()


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("kaggle", "all"):
        kaggle()
    if which in ("berka", "all"):
        berka()
