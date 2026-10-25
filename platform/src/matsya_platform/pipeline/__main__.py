"""Command line for the Track A pipeline.

    python -m matsya_platform.pipeline -c pipeline.toml run               # one run, now
    python -m matsya_platform.pipeline -c pipeline.toml run --every 900   # every 15 minutes
    python -m matsya_platform.pipeline -c pipeline.toml replay --start 2025-04-01 --end 2026-04-01 --step-days 7
    python -m matsya_platform.pipeline -c pipeline.toml consent grant <tenant_id>
    python -m matsya_platform.pipeline -c pipeline.toml consent revoke <tenant_id> [<consent_id>]
    python -m matsya_platform.pipeline -c pipeline.toml status

run is what cron or a scheduler calls (`*/15 * * * * ... run`); overlapping runs are refused
by the store lock. --every keeps one process running instead. replay drives a simulated clock
through a period, one run per step, to show a year of operation in one command. --now sets
the clock for any command. Exit status: 0 when every tenant ran clean, 1 when one failed, 2
when another run held the lock.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

from matsya_contracts.enums import ConsentStatus as S
from matsya_platform.pipeline import config as config_mod
from matsya_platform.pipeline.run import RunInProgress, grant, run, set_status, status


def _time(v: str) -> datetime:
    t = datetime.fromisoformat(v)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _summary(rep) -> str:
    parts = []
    for t in rep.tenants:
        m = t.matched or {}
        parts.append(f"{t.tenant_id[:8]}: " + (f"ERROR {t.error}" if t.error else
                     f"fetched {t.fetched}, built {t.built}, failed {len(t.failed)}, invoices {t.invoices}, "
                     f"links {m.get('links', '-')}, new labels {m.get('new_labels', '-')}, review {m.get('review', '-')}"
                     + ("" if t.consent else ", no active consent")))
    return f"{rep.now} {rep.run_id}: " + "; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m matsya_platform.pipeline", description=__doc__.split("\n")[0])
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("--now", type=_time, help="the clock (ISO time, UTC if no offset); default: now")
    ap.add_argument("--json", action="store_true", help="print the full run report as JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--every", type=float, help="repeat every N seconds until interrupted")
    rp = sub.add_parser("replay")
    rp.add_argument("--start", type=_time, required=True)
    rp.add_argument("--end", type=_time, required=True)
    rp.add_argument("--step-days", type=float, default=7)
    c = sub.add_parser("consent")
    c.add_argument("action", choices=["grant", "pause", "resume", "revoke"])
    c.add_argument("tenant_id")
    c.add_argument("consent_id", nargs="?")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    cfg = config_mod.load(a.config)

    def one(now):
        rep = run(cfg, now)
        print(rep.to_json() if a.json else _summary(rep), flush=True)
        return rep.ok

    try:
        if a.cmd == "run":
            ok = one(a.now)
            while a.every:
                time.sleep(a.every)
                ok = one(None) and ok
            return 0 if ok else 1
        if a.cmd == "replay":
            ok, t = True, a.start
            while t <= a.end:
                ok = one(t) and ok
                t += timedelta(days=a.step_days)
            return 0 if ok else 1
        if a.cmd == "consent":
            if a.action == "grant":
                print(grant(cfg, a.tenant_id, a.now))
                return 0
            cid = a.consent_id or cfg.tenant(a.tenant_id).consent.consent_id
            to = {"pause": S.PAUSED, "resume": S.ACTIVE, "revoke": S.REVOKED}[a.action]
            print(set_status(cfg, a.tenant_id, cid, to, a.now))
            return 0
        print(json.dumps(status(cfg), indent=1, default=str))
        return 0
    except RunInProgress as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
