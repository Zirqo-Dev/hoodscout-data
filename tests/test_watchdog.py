"""watchdog.py's staleness rules.

Collection is started by cron-job.org calling workflow_dispatch, which makes
that account a single point of failure with no alarm on it. This is the
alarm, so the thresholds are worth pinning exactly: an off-by-one at the
boundary either cries wolf every hour or stays silent through a real stall.

check_staleness is pure, so `now` is placed either side of each threshold
rather than waited for.
"""
from datetime import datetime, timedelta, timezone

import pytest

import watchdog as W

NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


def ago(**kw):
    return (NOW - timedelta(**kw)).isoformat()


def check(latest=None, holders=None, now=NOW):
    return W.check_staleness(latest, holders, now)


def feeds(problems):
    return {p["feed"]: p for p in problems}


# ------------------------------------------------------------------ fresh

def test_both_fresh_is_no_problems():
    assert check(latest=ago(minutes=5), holders=ago(hours=2)) == []


def test_fresh_to_the_second():
    assert check(latest=NOW.isoformat(), holders=NOW.isoformat()) == []


# -------------------------------------------------------- the thresholds

def test_latest_just_under_the_hour_is_fine():
    assert check(latest=ago(minutes=59, seconds=59),
                 holders=ago(hours=1)) == []


def test_latest_just_over_the_hour_is_stale():
    p = check(latest=ago(minutes=60, seconds=1), holders=ago(hours=1))
    assert len(p) == 1
    assert p[0]["feed"] == "latest.json"
    assert p[0]["kind"] == "stale"
    assert p[0]["limit_s"] == 3600


def test_latest_exactly_at_the_hour_is_not_yet_stale():
    """The rule is "older than", so the boundary itself passes. Pinned
    because an inclusive comparison would fire on a feed that is exactly on
    time, every hour, forever."""
    assert check(latest=ago(seconds=3600), holders=ago(hours=1)) == []


def test_holders_just_under_five_hours_is_fine():
    assert check(latest=ago(minutes=5),
                 holders=ago(hours=4, minutes=59, seconds=59)) == []


def test_holders_just_over_five_hours_is_stale():
    p = check(latest=ago(minutes=5), holders=ago(hours=5, seconds=1))
    assert len(p) == 1
    assert p[0]["feed"] == "holders"
    assert p[0]["kind"] == "stale"
    assert p[0]["limit_s"] == 5 * 3600


def test_holders_exactly_at_five_hours_is_not_yet_stale():
    assert check(latest=ago(minutes=5), holders=ago(seconds=5 * 3600)) == []


def test_the_two_feeds_have_different_thresholds():
    """A 3-hourly feed must not be judged by a 15-minute feed's clock. Two
    hours old is stale for latest.json and fine for holders."""
    p = check(latest=ago(hours=2), holders=ago(hours=2))
    assert list(feeds(p)) == ["latest.json"]


# --------------------------------------------------- one stale, not the other

def test_holders_stale_while_collect_is_fresh():
    p = check(latest=ago(minutes=10), holders=ago(hours=7))
    assert len(p) == 1
    assert p[0]["feed"] == "holders"
    assert "7h" in p[0]["message"]


def test_collect_stale_while_holders_is_fresh():
    p = check(latest=ago(hours=3), holders=ago(hours=1))
    assert len(p) == 1
    assert p[0]["feed"] == "latest.json"


def test_both_stale_reports_both():
    p = check(latest=ago(hours=9), holders=ago(hours=9))
    assert len(p) == 2
    assert set(feeds(p)) == {"latest.json", "holders"}
    assert all(x["kind"] == "stale" for x in p)


# ------------------------------------------------------- missing and garbage

def test_a_missing_timestamp_is_a_problem_not_a_crash():
    p = check(latest=None, holders=ago(hours=1))
    assert len(p) == 1
    assert p[0]["kind"] == "missing"
    assert p[0]["age_s"] is None
    assert "no timestamp" in p[0]["message"]


def test_both_missing():
    p = check(latest=None, holders=None)
    assert [x["kind"] for x in p] == ["missing", "missing"]


@pytest.mark.parametrize("junk", [
    "not a date", "", "   ", "2026-13-45T99:99:99Z", "1760000000",
    "2026/10/20 12:00", 12345, [], {},
])
def test_a_garbage_timestamp_is_a_problem_not_a_crash(junk):
    p = check(latest=junk, holders=ago(hours=1))
    assert len(p) == 1
    assert p[0]["kind"] in ("missing", "unreadable")
    assert p[0]["age_s"] is None


def test_an_unreadable_timestamp_says_what_it_saw():
    p = check(latest="tomorrow probably", holders=ago(hours=1))
    assert p[0]["kind"] == "unreadable"
    assert "tomorrow probably" in p[0]["message"]


# ---------------------------------------------------------- a clock fault

def test_a_future_timestamp_does_not_read_as_fresh():
    """The one failure a dead-man switch must not have. A clock or data fault
    that puts the timestamp ahead of now would otherwise look permanently
    fresh and hide a stall for as long as the fault lasted."""
    ahead = (NOW + timedelta(hours=3)).isoformat()
    p = check(latest=ahead, holders=ago(hours=1))
    assert len(p) == 1
    assert p[0]["kind"] == "ahead"
    assert "future" in p[0]["message"]


def test_small_clock_skew_is_tolerated():
    """Runners and the collector do not share a clock. A couple of minutes
    ahead is skew, not a fault."""
    ahead = (NOW + timedelta(minutes=2)).isoformat()
    assert check(latest=ahead, holders=ago(hours=1)) == []


# -------------------------------------------------------------- formatting

def test_naive_timestamps_are_read_as_utc():
    """Everything this repo writes is tz-aware, but a naive string must not
    raise -- comparing naive to aware is a TypeError."""
    naive = NOW.replace(tzinfo=None).isoformat()
    assert check(latest=naive, holders=ago(hours=1)) == []


def test_human_ages():
    assert W.human(0) == "0m"
    assert W.human(59) == "0m"
    assert W.human(60) == "1m"
    assert W.human(3600) == "1h 0m"
    assert W.human(3660) == "1h 1m"
    assert W.human(None) == "unknown"
    assert W.human(-3600) == "-1h 0m"


def test_the_body_names_every_problem_and_the_remedy():
    p = check(latest=ago(hours=9), holders=ago(hours=9))
    body = W.render(p, NOW)
    assert body.count("\n- ") == 2
    for x in p:
        assert x["message"] in body
    assert "cron-job.org" in body, "the body has to say where to look first"
    assert "Thresholds" in body


# ---------------------------------------------------------- reading disk

def test_read_latest_on_a_missing_file(tmp_path):
    assert W.read_latest(str(tmp_path / "nope.json")) is None


def test_read_latest_on_garbage(tmp_path):
    p = tmp_path / "latest.json"
    p.write_text("{not json")
    assert W.read_latest(str(p)) is None


def test_read_latest_on_a_json_list(tmp_path):
    """Valid JSON, wrong shape. Must be None rather than an AttributeError."""
    p = tmp_path / "latest.json"
    p.write_text("[1, 2, 3]")
    assert W.read_latest(str(p)) is None


def test_read_latest_picks_up_generated_at(tmp_path):
    p = tmp_path / "latest.json"
    p.write_text('{"generated_at": "2026-10-20T12:00:00+00:00"}')
    assert W.read_latest(str(p)) == "2026-10-20T12:00:00+00:00"


def test_newest_holders_ts_with_no_snapshots(tmp_path):
    assert W.newest_holders_ts(str(tmp_path / "*" / "*.jsonl")) is None


def test_newest_holders_ts_takes_the_last_path(tmp_path):
    """The path sorts chronologically, so the newest snapshot is the last."""
    for day, hhmmss, ts in (
        ("2026-10-19", "153414", "2026-10-19T15:34:14+00:00"),
        ("2026-10-20", "101021", "2026-10-20T10:10:21+00:00"),
    ):
        d = tmp_path / day
        d.mkdir(exist_ok=True)
        (d / (hhmmss + ".jsonl")).write_text('{"ts": "%s", "ca": "0xaa"}\n' % ts)
    got = W.newest_holders_ts(str(tmp_path / "*" / "*.jsonl"))
    assert got == "2026-10-20T10:10:21+00:00"


def test_newest_holders_ts_ignores_the_meta_sidecar(tmp_path):
    """The sidecar is .meta.json and must not be picked up as a snapshot --
    it has no ts field and sorts after the .jsonl it describes."""
    d = tmp_path / "2026-10-20"
    d.mkdir()
    (d / "101021.jsonl").write_text('{"ts": "2026-10-20T10:10:21+00:00"}\n')
    (d / "101021.meta.json").write_text('{"coverage": 1.0}')
    assert W.newest_holders_ts(str(tmp_path / "*" / "*.jsonl")) \
        == "2026-10-20T10:10:21+00:00"


def test_newest_holders_ts_on_an_empty_file(tmp_path):
    d = tmp_path / "2026-10-20"
    d.mkdir()
    (d / "101021.jsonl").write_text("")
    assert W.newest_holders_ts(str(tmp_path / "*" / "*.jsonl")) is None


# ------------------------------------------------------------------- the CLI

def test_the_cli_reports_clean_data(capsys, monkeypatch):
    monkeypatch.setattr(W, "read_latest", lambda *a, **k: NOW.isoformat())
    monkeypatch.setattr(W, "newest_holders_ts", lambda *a, **k: NOW.isoformat())
    assert W.main(["--now", NOW.isoformat()]) == 0
    assert "no problems" in capsys.readouterr().out


def test_the_cli_writes_the_body_only_when_there_is_a_problem(tmp_path,
                                                              monkeypatch):
    out = tmp_path / "STALE.txt"
    out.write_text("left over from a previous run")
    monkeypatch.setattr(W, "read_latest", lambda *a, **k: NOW.isoformat())
    monkeypatch.setattr(W, "newest_holders_ts", lambda *a, **k: NOW.isoformat())
    assert W.main(["--now", NOW.isoformat(), "--out", str(out)]) == 0
    assert not out.exists(), "a stale body must be cleared, or the workflow " \
                             "would alert on last hour's problem"

    monkeypatch.setattr(W, "read_latest", lambda *a, **k: ago(hours=9))
    assert W.main(["--now", NOW.isoformat(), "--out", str(out)]) == 0
    assert out.exists()
    assert "latest.json" in out.read_text()


def test_the_cli_always_exits_zero_even_when_alerting(monkeypatch):
    """A non-zero exit would make a real alert indistinguishable from the
    script itself being broken."""
    monkeypatch.setattr(W, "read_latest", lambda *a, **k: None)
    monkeypatch.setattr(W, "newest_holders_ts", lambda *a, **k: None)
    assert W.main(["--now", NOW.isoformat()]) == 0


def test_a_bad_now_override_is_rejected(capsys):
    assert W.main(["--now", "yesterday"]) == 2
    assert "not a readable timestamp" in capsys.readouterr().out


# ------------------------------------------- the manual threshold override

def test_no_override_by_default():
    """What a scheduled run passes: nothing, or the empty string the
    workflow_dispatch default supplies."""
    assert W.parse_max_age_minutes(None) is None
    assert W.parse_max_age_minutes("") is None
    assert W.parse_max_age_minutes("   ") is None


def test_an_override_is_read_as_minutes():
    assert W.parse_max_age_minutes("30") == 30 * 60
    assert W.parse_max_age_minutes(" 90 ") == 90 * 60
    assert W.parse_max_age_minutes(5) == 5 * 60


def test_zero_is_allowed_and_forces_the_alert():
    """The intended forcing value. The rule is "older than", so a threshold
    of zero fires at any positive age -- which is every real timestamp, since
    a file is always read after it was written."""
    assert W.parse_max_age_minutes("0") == 0
    one_second_old = ago(seconds=1)
    assert check(latest=one_second_old, holders=ago(hours=1)) == [], \
        "fresh under the real threshold"
    p = W.check_staleness(one_second_old, ago(hours=1), NOW, latest_max_s=0)
    assert len(p) == 1
    assert p[0]["feed"] == "latest.json"
    assert p[0]["kind"] == "stale"


def test_zero_does_not_fire_on_an_age_of_exactly_zero():
    """The one edge the forcing value has, pinned rather than papered over:
    "older than zero" excludes zero itself. No file written and then read
    back has an age of exactly zero, so this never happens in practice, but
    the rule is strict at every threshold and that is worth being honest
    about rather than special-casing one of them."""
    assert W.check_staleness(NOW.isoformat(), ago(hours=1), NOW,
                             latest_max_s=0) == []


@pytest.mark.parametrize("bad", ["abc", "30m", "1.5", "", " x ", "1e3",
                                 "0x10", "--5", "nan", "inf"])
def test_a_non_numeric_override_is_rejected(bad):
    """Rejected, not ignored: this input exists to prove the alert works, so
    an operator who mistyped it has to be told rather than handed a run that
    looks like an ordinary clean check. The empty string is the one value
    here that is legitimately "no override"."""
    if bad.strip() == "":
        assert W.parse_max_age_minutes(bad) is None
        return
    with pytest.raises(ValueError) as e:
        W.parse_max_age_minutes(bad)
    assert "whole number" in str(e.value)


@pytest.mark.parametrize("bad", ["-1", "-60", -5])
def test_a_negative_override_is_rejected(bad):
    with pytest.raises(ValueError) as e:
        W.parse_max_age_minutes(bad)
    assert "negative" in str(e.value)


def test_the_override_moves_only_the_latest_threshold():
    """The holders feed is never overridable. One feed is enough to prove the
    path, and leaving holders alone means a test run cannot mask a real
    holders stall."""
    p = W.check_staleness(ago(minutes=5), ago(hours=4), NOW, latest_max_s=60)
    assert [x["feed"] for x in p] == ["latest.json"]
    # holders at 4h is inside its own 5h threshold and stays there
    assert all(x["feed"] != "holders" for x in p)


def test_the_override_can_also_widen():
    """A larger threshold suppresses a real staleness, which is why it is
    manual-only and marked as a test run in the body."""
    assert W.check_staleness(ago(hours=3), ago(hours=1), NOW,
                             latest_max_s=4 * 3600) == []


def test_an_overridden_body_says_it_is_a_test():
    """Otherwise a forced alert reads as a real stall to whoever opens the
    issue."""
    p = W.check_staleness(NOW.isoformat(), ago(hours=1), NOW, latest_max_s=0)
    body = W.render(p, NOW, latest_max_s=0)
    assert "THIS IS A TEST RUN" in body
    assert "overridden to 0m" in body
    assert "says nothing about whether collection is actually stale" in body


def test_a_normal_body_does_not_say_test():
    p = check(latest=ago(hours=9), holders=ago(hours=9))
    assert "TEST RUN" not in W.render(p, NOW)


def test_the_body_quotes_the_overridden_threshold_not_the_default():
    p = W.check_staleness(ago(minutes=20), ago(hours=1), NOW,
                          latest_max_s=10 * 60)
    body = W.render(p, NOW, latest_max_s=10 * 60)
    assert "Thresholds: latest.json 10m, holders 5h 0m." in body


# ------------------------------------------------------ the flag end to end

def test_the_cli_accepts_an_empty_override(capsys, monkeypatch):
    """Exactly what a scheduled run supplies: --max-age-minutes "" """
    monkeypatch.setattr(W, "read_latest", lambda *a, **k: NOW.isoformat())
    monkeypatch.setattr(W, "newest_holders_ts", lambda *a, **k: NOW.isoformat())
    assert W.main(["--now", NOW.isoformat(), "--max-age-minutes", ""]) == 0
    out = capsys.readouterr().out
    assert "no problems" in out
    assert "OVERRIDE" not in out


def test_the_cli_forces_an_alert_with_zero(tmp_path, capsys, monkeypatch):
    out_file = tmp_path / "STALE.txt"
    monkeypatch.setattr(W, "read_latest", lambda *a, **k: ago(seconds=1))
    monkeypatch.setattr(W, "newest_holders_ts", lambda *a, **k: NOW.isoformat())
    assert W.main(["--now", NOW.isoformat(), "--max-age-minutes", "0",
                   "--out", str(out_file)]) == 0
    assert "OVERRIDE" in capsys.readouterr().out
    assert out_file.exists()
    assert "THIS IS A TEST RUN" in out_file.read_text()


def test_the_cli_rejects_a_bad_override_and_writes_nothing(tmp_path, capsys,
                                                           monkeypatch):
    out_file = tmp_path / "STALE.txt"
    monkeypatch.setattr(W, "read_latest", lambda *a, **k: ago(hours=9))
    monkeypatch.setattr(W, "newest_holders_ts", lambda *a, **k: NOW.isoformat())
    assert W.main(["--now", NOW.isoformat(), "--max-age-minutes", "soon",
                   "--out", str(out_file)]) == 2
    assert "bad --max-age-minutes" in capsys.readouterr().out
    assert not out_file.exists(), "a rejected run must not alert either way"
