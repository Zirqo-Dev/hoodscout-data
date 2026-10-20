#!/usr/bin/env python3
"""Notice when collection stalls, without depending on the thing that runs it.

Every collector in this repo is started by cron-job.org calling
workflow_dispatch. That is a single point of failure with no alarm on it: if
the job stops, or its token expires around 2027-01-16, `data/latest.json`
simply stops advancing and the only visible symptom is the dashboard's
freshness chips going amber. Nothing in the repository notices.

This script is what notices. It reads two timestamps off disk and reports
which feeds have gone quiet. It never writes to the data, never commits, and
never calls an API -- the whole point is that it can run when the collectors
cannot, so it depends on as little as possible.

check_staleness() is pure: timestamps in, problems out. The CLI is a thin
wrapper that finds the two timestamps and formats the result.
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

LATEST_PATH = "data/latest.json"
HOLDERS_GLOB = "data/holders/*/*.jsonl"

# `latest.json` is rewritten every 15 minutes, so an hour is four missed runs
# -- comfortably past the delivery noise measured in docs/TECH_NOTES.md and
# still inside the window where a human could notice and act.
LATEST_MAX_S = 60 * 60
# The holders collector runs 3-hourly. Five hours is one missed run plus
# slack, rather than two, because a 3-hourly feed that skips twice has lost
# most of a cross-section's worth of history.
HOLDERS_MAX_S = 5 * 60 * 60

# A timestamp this far ahead of now is a clock or data fault, not freshness.
# Without this a bad clock would read as permanently fresh, which is the one
# failure a dead-man switch must not have.
FUTURE_TOLERANCE_S = 5 * 60

FEEDS = (
    ("latest.json", LATEST_MAX_S),
    ("holders", HOLDERS_MAX_S),
)


def parse_ts(value):
    """-> an aware datetime, or None if it is missing or not a timestamp."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_max_age_minutes(raw):
    """-> an override for latest.json's threshold, in seconds, or None.

    Absent or empty means no override, which is what a scheduled run passes.
    Anything else must be a whole number of minutes >= 0; a bad value RAISES
    rather than being quietly dropped, because this input exists to prove the
    alert path works and an operator who typed it wrongly needs to be told,
    not handed a run that looks like an ordinary clean check.

    Zero is ALLOWED and is the intended forcing value: the staleness rule is
    "older than", so a threshold of zero makes latest.json stale at any
    positive age, whatever the data's real age. The one age it does not fire
    on is exactly zero -- a timestamp equal to `now` to the microsecond --
    which no written-then-read file ever is, but which is worth knowing
    rather than claiming the rule is absolute. Negative is rejected: there is
    no reading of it that means anything.
    """
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        minutes = int(raw)
    except ValueError:
        raise ValueError("max-age-minutes must be a whole number of minutes, "
                         "got %r" % raw)
    if minutes < 0:
        raise ValueError("max-age-minutes cannot be negative, got %d"
                         % minutes)
    return minutes * 60


def human(seconds):
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    h, m = divmod(seconds // 60, 60)
    if h:
        return "%s%dh %dm" % (sign, h, m)
    return "%s%dm" % (sign, m)


def check_staleness(latest_generated_at, latest_holders_ts, now,
                    latest_max_s=None):
    """-> a list of problems, each a dict; empty means everything is fresh.

    Pure. The two timestamps are whatever was found on disk -- a string, None
    if the file was missing, or garbage if it was unreadable -- and `now` is
    passed in rather than read, so a test can place itself either side of a
    threshold exactly.

    `latest_max_s` overrides the threshold for latest.json ONLY, and only
    when a manual run asks for it. The holders threshold is never
    overridable: the input exists to prove the alert path end to end, and one
    feed is enough to do that.

    A problem is one of three kinds: `missing` (no timestamp at all),
    `unreadable` (there was a value and it is not a timestamp) and `stale`
    (older than the feed's threshold). A timestamp in the future is reported
    as `ahead`, because a clock fault that reads as fresh would hide a stall
    for as long as the fault lasts.
    """
    limits = dict(FEEDS)
    if latest_max_s is not None:
        limits["latest.json"] = latest_max_s
    out = []
    for (feed, _default), value in zip(FEEDS,
                                       (latest_generated_at,
                                        latest_holders_ts)):
        limit = limits[feed]
        if value is None:
            out.append({"feed": feed, "kind": "missing", "age_s": None,
                        "limit_s": limit, "value": None,
                        "message": "%s: no timestamp found" % feed})
            continue
        dt = parse_ts(value)
        if dt is None:
            out.append({"feed": feed, "kind": "unreadable", "age_s": None,
                        "limit_s": limit, "value": str(value)[:64],
                        "message": "%s: timestamp is not readable (%r)"
                                   % (feed, str(value)[:64])})
            continue
        age = (now - dt).total_seconds()
        if age < -FUTURE_TOLERANCE_S:
            out.append({"feed": feed, "kind": "ahead", "age_s": age,
                        "limit_s": limit, "value": value,
                        "message": "%s: timestamp is %s in the future (%s)"
                                   % (feed, human(-age), value)})
        elif age > limit:
            out.append({"feed": feed, "kind": "stale", "age_s": age,
                        "limit_s": limit, "value": value,
                        "message": "%s: %s old, over the %s limit (%s)"
                                   % (feed, human(age), human(limit), value)})
    return out


# ------------------------------------------------------------ reading disk

def read_latest(path=LATEST_PATH):
    """-> generated_at, or None if the file is missing or not readable JSON.

    A missing file is a problem for check_staleness to report, not an
    exception: a watchdog that crashes is a watchdog that says nothing.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    return d.get("generated_at") if isinstance(d, dict) else None


def newest_holders_ts(pattern=HOLDERS_GLOB):
    """-> the ts of the newest holders snapshot, or None.

    The path sorts chronologically (YYYY-MM-DD/HHMMSS.jsonl), so the last one
    is the newest. The row's own ts is used rather than the filename: it is
    what every other consumer of this log reads.
    """
    paths = sorted(glob.glob(pattern))
    if not paths:
        return None
    try:
        with open(paths[-1], encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    row = json.loads(line)
                    return row.get("ts") if isinstance(row, dict) else None
    except (OSError, ValueError):
        return None
    return None


def render(problems, now, latest_max_s=None):
    """The issue body. One line per problem, plus when it was checked."""
    lines = ["Collection has gone quiet. Checked at %s."
             % now.isoformat(timespec="seconds"), ""]
    for p in problems:
        lines.append("- %s" % p["message"])
    if latest_max_s is not None:
        # Said loudly, so a forced test alert is never mistaken for a real
        # stall by whoever reads the issue.
        lines += ["",
                  "**THIS IS A TEST RUN.** latest.json's threshold was "
                  "overridden to %s by a manual dispatch, so this alert says "
                  "nothing about whether collection is actually stale."
                  % human(latest_max_s)]
    lines += ["",
              "Thresholds: latest.json %s, holders %s."
              % (human(latest_max_s if latest_max_s is not None
                       else LATEST_MAX_S), human(HOLDERS_MAX_S)),
              "",
              "Collection is started by cron-job.org calling "
              "workflow_dispatch. Check that job first, then the token "
              "expiry. This check runs on GitHub's own schedule and is "
              "deliberately independent of it."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", metavar="PATH", default=None,
                    help="write the issue body here IF there are problems, "
                         "and remove any stale copy if there are none")
    ap.add_argument("--now", metavar="TS", default=None,
                    help="override the clock, for a dry run")
    ap.add_argument("--max-age-minutes", metavar="N", default=None,
                    help="override latest.json's staleness threshold, in "
                         "minutes, for a manual run that is proving the "
                         "alert path works. Empty means no override. 0 "
                         "forces the alert.")
    a = ap.parse_args(argv)

    now = parse_ts(a.now) if a.now else datetime.now(timezone.utc)
    if now is None:
        print("--now is not a readable timestamp: %r" % a.now)
        return 2
    try:
        latest_max_s = parse_max_age_minutes(a.max_age_minutes)
    except ValueError as e:
        print("bad --max-age-minutes: %s" % e)
        return 2

    latest = read_latest()
    holders = newest_holders_ts()
    problems = check_staleness(latest, holders, now, latest_max_s)

    print("checked at %s" % now.isoformat(timespec="seconds"))
    print("  latest.json generated_at : %s" % (latest or "MISSING"))
    print("  holders newest ts        : %s" % (holders or "MISSING"))
    if latest_max_s is not None:
        print("  OVERRIDE: latest.json threshold set to %s (test run)"
              % human(latest_max_s))
    if not problems:
        print("no problems: both feeds are inside their thresholds")
    else:
        print("%d problem(s):" % len(problems))
        for p in problems:
            print("  %s" % p["message"])

    if a.out:
        if problems:
            with open(a.out, "w", encoding="utf-8") as fh:
                fh.write(render(problems, now, latest_max_s))
            print("wrote %s" % a.out)
        elif os.path.exists(a.out):
            os.remove(a.out)
    # Always 0. The workflow decides what to do from the presence of --out,
    # so a non-zero exit would only turn a real alert into a red run that
    # nobody can tell apart from the script itself being broken.
    return 0


if __name__ == "__main__":
    sys.exit(main())
