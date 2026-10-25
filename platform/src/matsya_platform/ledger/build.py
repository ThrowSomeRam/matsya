"""A4 ledger builder. Pure: (payloads, current ledger state) -> (txns, balances, quarantine,
events) plus coverage and the next state. No I/O; the Iceberg writer comes later.

Order: parse -> quarantine -> key -> exact dedup -> fuzzy dedup -> continuity -> balances
-> coverage. An unaccepted continuity break raises ContinuityError and nothing is produced.

An as_of date means the end of that day in IST (18:29:59.999999 UTC); as_of reads take
rows with known_at at or before that instant.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone

import polars as pl

from matsya_contracts import tables as T
from matsya_contracts.events import AcceptGap
from matsya_contracts.validate import WRITE_SAMPLE, frame, validate
from matsya_platform.ledger import balances, continuity, dedup
from matsya_platform.ledger.continuity import ContinuityError
from matsya_platform.ledger.dedup import LedgerRow
from matsya_platform.ledger.ids import assign
from matsya_platform.ledger.quarantine import screen
from matsya_platform.ledger.rebit import IST, FetchRange, Payload, SummaryBalance, parse

GAP_DQ = 0.5  # dq_score cap for transactions inside an accepted gap
UNVERIFIED_DQ = 0.5  # dq_score cap for transactions from a payload nothing can verify
UNVERIFIED_SCHEMA = {"tenant_id": pl.String, "payload_id": pl.String, "account_id": pl.String, "rows": pl.Int32,
                     "known_at": pl.Datetime("us", "UTC")}
COVERAGE_SCHEMA = {"tenant_id": pl.String, "account_id": pl.String, "complete_through": pl.Date,
                   "known_at": pl.Datetime("us", "UTC")}


def as_of_cutoff(as_of: date | datetime) -> datetime:
    """A date means the end of that IST day; a datetime is used as given."""
    if isinstance(as_of, datetime):
        return as_of
    return datetime.combine(as_of, time(23, 59, 59, 999999), tzinfo=IST).astimezone(timezone.utc)


@dataclass(frozen=True)
class LedgerState:
    rows: tuple[LedgerRow, ...] = ()
    balances: tuple[T.Balance, ...] = ()
    summaries: tuple[SummaryBalance, ...] = ()
    ranges: tuple[FetchRange, ...] = ()
    accepted_gaps: tuple[AcceptGap, ...] = ()  # from accept_gap events

    def transactions_frame(self) -> pl.DataFrame:
        return frame(T.Transaction, [r.txn for r in self.rows])

    def balances_frame(self) -> pl.DataFrame:
        return frame(T.Balance, list(self.balances))

    def coverage_frame(self) -> pl.DataFrame:
        return _coverage_frame(continuity.coverage(self.ranges))

    def as_of(self, as_of: date | datetime) -> dict[str, pl.DataFrame]:
        """What was known at as_of: transactions, and the latest balance version per account,
        date and source."""
        cut = as_of_cutoff(as_of)
        txns = self.transactions_frame().filter(pl.col("known_at") <= cut)
        bal = (self.balances_frame().filter(pl.col("known_at") <= cut).sort("known_at")
               .group_by("tenant_id", "account_id", "date", "balance_source", maintain_order=True).last()
               .select(list(T.Balance.model_fields)).sort("account_id", "date", "balance_source"))
        cov = _coverage_frame(continuity.coverage(tuple(r for r in self.ranges if r.known_at <= cut)))
        return {"transactions": txns, "balances": bal, "coverage": cov}


@dataclass(frozen=True)
class BuildResult:
    txns: pl.DataFrame  # new ledger.transactions rows
    balances: pl.DataFrame  # new ledger.balances rows (append-only)
    quarantine: pl.DataFrame  # new ops.dq_quarantine rows
    events: pl.DataFrame  # new ops.events rows
    coverage: pl.DataFrame  # complete_through per account after this build (no contract table yet)
    # Payloads with no currentBalance on any row and no Summary: stored, flagged with a
    # lowered dq_score, and listed here. The ops.events kind unverified_payload does not
    # exist in the contract yet (positions doc F7), so this is a plain frame for now.
    unverified: pl.DataFrame
    state: LedgerState


def _unique_summaries(summaries) -> tuple[SummaryBalance, ...]:
    seen, out = set(), []
    for s in sorted(summaries, key=lambda s: (s.known_at, s.payload_id)):
        key = (s.account_id, s.at, s.balance_paise, s.known_at)  # every fetch keeps its own
        if key not in seen:
            seen.add(key)
            out.append(s)
    return tuple(out)


def build(payloads: list[Payload], state: LedgerState = LedgerState()) -> BuildResult:
    raw, new_summaries, new_ranges = [], [], []
    for p in payloads:
        rows, summaries, ranges = parse(p)
        raw += rows
        new_summaries += summaries
        new_ranges += ranges
    clean, quarantined, events = screen(raw)
    anchored = {(r.payload_id, r.account_id) for r in raw if r.balance_after_paise is not None} | \
        {(s.payload_id, s.account_id) for s in new_summaries}
    counts: dict[tuple, int] = {}
    for r in raw:
        counts[(r.payload_id, r.account_id)] = counts.get((r.payload_id, r.account_id), 0) + 1
    unverified = {k: n for k, n in counts.items() if k not in anchored}
    summaries = _unique_summaries(state.summaries + tuple(new_summaries))

    fresh, conflicts = dedup.exact(assign(clean), state.rows)
    fresh = dedup.resolve(fresh, state.rows, summaries, state.accepted_gaps)

    walks = continuity.walk(state.rows + tuple(fresh), summaries, state.accepted_gaps)
    breaks = [b for w in walks.values() for b in w.breaks]
    unaccepted = [b for b in breaks if not b.accepted]
    if unaccepted:
        raise ContinuityError(unaccepted)
    windows = [(b.account_id, b.accepted_by) for b in breaks]
    fresh = [_cap_unverified(_cap_in_gap(r, windows), unverified) for r in fresh]
    stamp = max((p.fetched_at for p in payloads), default=max((r.known_at for r in state.rows), default=None))

    rows = state.rows + tuple(fresh)
    # Bank-reported first: it wins a key collision (contract F1).
    candidates = balances.bank_reported(summaries) + (balances.reconstructed(rows, walks, stamp) if stamp else [])
    new_balances = balances.new_versions(candidates, state.balances)

    ranges = state.ranges + tuple(r for r in new_ranges if r not in state.ranges)
    next_state = LedgerState(rows=rows, balances=state.balances + tuple(new_balances), summaries=summaries,
                             ranges=ranges, accepted_gaps=state.accepted_gaps)
    return BuildResult(
        txns=frame(T.Transaction, [r.txn for r in fresh]),
        balances=frame(T.Balance, new_balances),
        quarantine=frame(T.DqQuarantine, quarantined),
        events=frame(T.Event, events + conflicts),
        coverage=next_state.coverage_frame(),
        unverified=pl.DataFrame(
            [{"tenant_id": next((p.tenant_id for p in payloads if p.payload_id == pid), None), "payload_id": pid,
              "account_id": acc, "rows": n, "known_at": next(p.fetched_at for p in payloads if p.payload_id == pid)}
             for (pid, acc), n in sorted(unverified.items())], schema=UNVERIFIED_SCHEMA),
        state=next_state,
    )


def _coverage_frame(cov) -> pl.DataFrame:
    return pl.DataFrame([{"tenant_id": c.tenant_id, "account_id": c.account_id, "complete_through": c.complete_through,
                          "known_at": c.known_at} for c in cov], schema=COVERAGE_SCHEMA)


def _cap_in_gap(r: LedgerRow, windows: list[tuple[str, AcceptGap]]) -> LedgerRow:
    """Rows posted inside an accepted gap's window (IST posting dates) get a lowered dq_score."""
    posted = r.posted_at.astimezone(IST).date()
    for account_id, g in windows:
        if r.account_id == account_id and g.from_date <= posted <= g.to_date and r.txn.dq_score > GAP_DQ:
            return replace(r, txn=r.txn.model_copy(update={"dq_score": GAP_DQ}))
    return r


def _cap_unverified(r: LedgerRow, unverified: dict) -> LedgerRow:
    if (r.payload_id, r.account_id) in unverified and r.txn.dq_score > UNVERIFIED_DQ:
        return replace(r, txn=r.txn.model_copy(update={"dq_score": UNVERIFIED_DQ}))
    return r


def validate_outputs(result: BuildResult, sample: int | None = WRITE_SAMPLE) -> BuildResult:
    """Real writes use the default sample; tests pass sample=None for full validation."""
    validate(result.txns, T.Transaction, sample=sample)
    validate(result.balances, T.Balance, sample=sample)
    validate(result.quarantine, T.DqQuarantine, sample=sample)
    validate(result.events, T.Event, sample=sample)
    return result
