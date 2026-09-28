#!/usr/bin/env python3
"""The once-an-hour gate shared by the workflow and cloud_hourly.py (stdlib only,
so it can run before any dependency is installed).

Several independent things start this workflow (cron-job.org, GitHub's own
schedule, a launchd job on the Mac). Any of them may fire, late, twice or not at
all -- the gate makes that harmless: a real cycle (=an ERP login) runs only if
  * the last SUCCESSFUL cycle was >= MIN_SUCCESS_GAP minutes ago, AND
  * the last ATTEMPT (success or failure) was >= MIN_ATTEMPT_GAP minutes ago
    (so a failed login is retried, but not hammered -- the ERP rate-limits
    rapid repeated logins).
A manual run with input force=true (env FORCE_RUN=true) always runs.
State lives in last_run.json, committed back to the repo by the workflow.
"""
import datetime, json, os
from pathlib import Path

FILE = Path(__file__).resolve().parent / "last_run.json"
LOCK_FILE = Path(__file__).resolve().parent / "local_lock.json"
MIN_SUCCESS_GAP = 50
MIN_ATTEMPT_GAP = 25


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _read():
    try:
        return json.loads(FILE.read_text())
    except Exception:
        return {}


def _ts(state, key):
    try:
        return datetime.datetime.fromisoformat(state[key])
    except Exception:
        return None


def _write(**upd):
    st = _read(); st.update({k: v.isoformat() for k, v in upd.items()})
    FILE.write_text(json.dumps(st))


def last_success():
    return _ts(_read(), "last_success")


def locked():
    """True while the user has flagged that they're logging into ERP manually
    themselves (see pause_bot.sh) -- the ERP is effectively single-session, so
    the cloud login and a manual one within the same couple of minutes can
    make one of them grab/consume the other's OTP email. This is a plain
    timed pause, not tied to force=true, so even a manual test run respects it."""
    try:
        until = datetime.datetime.fromisoformat(json.loads(LOCK_FILE.read_text())["until"])
    except Exception:
        return False
    return _now() < until


def run_is_due():
    if locked():
        return False
    if os.environ.get("FORCE_RUN", "").strip().lower() == "true":
        return True
    st = _read(); now = _now()
    ls, la = _ts(st, "last_success"), _ts(st, "last_attempt")
    if ls and (now - ls).total_seconds() / 60 < MIN_SUCCESS_GAP:
        return False
    if la and (now - la).total_seconds() / 60 < MIN_ATTEMPT_GAP:
        return False
    return True


def record_attempt():
    _write(last_attempt=_now())


def record_success():
    n = _now(); _write(last_success=n, last_attempt=n)


if __name__ == "__main__":
    due = run_is_due()
    print(f"should_run={due}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"should_run={'true' if due else 'false'}\n")
