"""analysis/hs91/pooled_rate.py: watch-point matching, the L/P/migration rule
and the censoring count.

Tiny synthetic rows in a temporary data/ tree: no network, no real data.
"""
import gzip
import importlib.util
import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import ROOT

_spec = importlib.util.spec_from_file_location(
    "hs91_pooled", ROOT / "analysis" / "hs91" / "pooled_rate.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

T0 = datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)


def h(x):
    return T0 + timedelta(hours=x)


def ca(i):
    return "0x" + f"{i:040x}"


def pool(i):
    return "0x" + f"{i:064x}"


def log_of(rows):
    """[(hours, liq)] -> the (times, rows) shape load_log() returns."""
    rs = [(h(t), liq, 1.0) for t, liq in rows]
    return [r[0] for r in rs], rs


# ------------------------------------------------------------------ matching

def test_entry_is_last_log_row_at_or_before_enrolment_within_30_min():
    lg = log_of([(-1, 10.0), (-0.4, 20.0), (0.1, 30.0)])
    assert P.entry_row(lg, h(0))[1] == 20.0
    assert P.entry_row(log_of([(-0.6, 10.0)]), h(0)) is None
    assert P.entry_row(log_of([(-0.5, 10.0)]), h(0))[1] == 10.0
    assert P.entry_row(None, h(0)) is None


def test_window_is_open_at_entry_closed_at_72h_and_deduplicated():
    rows = [(h(0), 1.0, 1.0), (h(0.01), 2.0, 1.0), (h(0.01), 9.0, 1.0),
            (h(72), 3.0, 1.0), (h(72.01), 4.0, 1.0)]
    assert [p[1] for p in P.window_points(rows, h(0))] == [2.0, 3.0]


def test_numeric_parsing():
    assert P.num("12.5") == 12.5 and P.num(3) == 3.0
    for bad in (None, "", "abc", "nan", "inf"):
        assert P.num(bad) is None


# ------------------------------------------------------------------ L / P / migration

ENTRY_LIQ, ENTRY_PRICE = 100_000.0, 1.0      # liq bar = 10,000


@pytest.mark.parametrize("res, price, want", [
    (9_999.0, 1.0, (True, False)),
    (10_000.0, 1.0, (False, False)),
    (50_000.0, 0.10, (False, True)),
    (50_000.0, 0.11, (False, False)),
    (500.0, 0.05, (True, True)),
    (50_000.0, None, (False, False)),
    (None, 0.01, (False, False)),      # unknown point: neither clause
])
def test_point_flags(res, price, want):
    assert P.point_flags((h(3), res, price), ENTRY_LIQ, ENTRY_PRICE, None) == want


def test_absolute_floor_applies_to_small_entries():
    # entry liq 5,000: 10% is 500, so the 1,000 floor is the bar
    assert P.point_flags((h(3), 999.0, 1.0), 5_000.0, 1.0, None) == (True, False)
    assert P.point_flags((h(3), 1_000.0, 1.0), 5_000.0, 1.0, None) == (False, False)


def test_migration_cancels_L_only_within_one_hour_and_at_the_bar():
    pt = (h(10), 100.0, 0.05)
    assert P.point_flags(pt, ENTRY_LIQ, ENTRY_PRICE, log_of([(10.9, 10_000.0)])) == (False, True)
    assert P.point_flags(pt, ENTRY_LIQ, ENTRY_PRICE, log_of([(11.1, 10_000.0)])) == (True, True)
    assert P.point_flags(pt, ENTRY_LIQ, ENTRY_PRICE, log_of([(10, 9_999.0)])) == (True, True)


def _pts(pattern):
    """'L' reserve dead, 'P' price dead, 'B' both, 'A' alive, '.' unknown."""
    m = {"L": (500.0, 1.0), "P": (50_000.0, 0.05), "B": (500.0, 0.05),
         "A": (50_000.0, 1.0), ".": (None, None)}
    return [(h(3 * (k + 1)), *m[c]) for k, c in enumerate(pattern)]


@pytest.mark.parametrize("pattern, want", [
    ("A" * 24, ("alive", None)),
    ("LL" + "A" * 22, ("dead", "L")),
    ("PP" + "A" * 22, ("dead", "P")),
    ("BB" + "A" * 22, ("dead", "both")),
    ("LLAPP" + "A" * 19, ("dead", "both")),
    ("LP" + "A" * 22, ("dead", "mixed")),
    ("LALALA" * 4, ("alive", None)),
    ("L.L" + "A" * 21, ("alive", None)),       # unknown row breaks the run
])
def test_classify_deaths(pattern, want):
    assert P.classify(_pts(pattern), ENTRY_LIQ, ENTRY_PRICE, None) == want


def test_classify_migration_makes_an_L_pair_alive():
    pts = _pts("LL" + "A" * 22)
    lg = log_of([(3, 50_000.0), (6, 50_000.0)])
    assert P.classify(pts, ENTRY_LIQ, ENTRY_PRICE, lg) == ("alive", None)


@pytest.mark.parametrize("pattern, reason", [
    ("", "no watch rows in window"),
    ("A" * 11, "fewer than 12 watch rows in window"),
    ("A" * 11 + ".", "12+ rows but fewer than 12 with a numeric reserve"),
])
def test_censoring_reasons(pattern, reason):
    assert P.classify(_pts(pattern), ENTRY_LIQ, ENTRY_PRICE, None) == ("censored", reason)


def test_exactly_12_known_is_not_censored_even_if_dead():
    assert P.classify(_pts("LL" + "A" * 10), ENTRY_LIQ, ENTRY_PRICE, None) == ("dead", "L")


# ------------------------------------------------------------------ end to end

@pytest.fixture
def tree(tmp_path, monkeypatch):
    fu, lg = tmp_path / "followup", tmp_path / "log"
    monkeypatch.setattr(P, "FOLLOWUP", str(fu))
    monkeypatch.setattr(P, "LOG_DIR", str(lg))
    return fu, lg


def write_tree(fu, lg, enrolled, log_rows, watch_runs):
    """watch_runs: [(hours, [(ca, pool, reserve, price)])]."""
    fu.mkdir()
    (fu / "enrolled.json").write_text(json.dumps(enrolled))
    (lg / "d").mkdir(parents=True)
    (lg / "d" / "x.jsonl").write_text("".join(json.dumps(r) + "\n" for r in log_rows))
    wd = fu / "watch" / "d"
    wd.mkdir(parents=True)
    for k, (t, rows) in enumerate(watch_runs):
        ts = h(t).isoformat()
        (wd / f"{k:03d}.meta.json").write_text(json.dumps({"run_ts": ts}))
        with gzip.open(wd / f"{k:03d}.jsonl.gz", "wt") as f:
            for c, p, res, price in rows:
                f.write(json.dumps({"ts": ts, "ca": c, "pool": p, "attributes": {
                    "reserve_in_usd": res, "base_token_price_usd": price}}) + "\n")


def enr(i, first_h=-10, at_h=0):
    return {"ca": ca(i), "main_pool": pool(i), "first_seen": h(first_h).isoformat(),
            "enrolled_at": h(at_h).isoformat()}


def logrow(i, t, liq, price=1.0):
    return {"ts": h(t).isoformat(), "ca": ca(i), "liq": liq, "price": price, "tier": "x"}


def test_run_counts_end_to_end(tree):
    fu, lg = tree
    enrolled = [
        enr(1),                        # dead (L pair at runs 1-2)
        enr(2),                        # alive
        enr(3),                        # censored: reserve not numeric
        enr(4),                        # no entry row
        enr(5),                        # entry liq 0
        enr(6, first_h=-100),          # enrolled 100 h after first seen
        enr(7, at_h=10),               # not mature by the last run
        enr(8),                        # watch rows on another pool only: censored
        enr(9, at_h=200),              # enrolled after this run: not counted
    ]
    logs = [logrow(i, -0.2, 60_000.0 if i != 2 else 20_000.0) for i in (1, 2, 3, 6, 8)]
    logs.append(logrow(7, 9.8, 60_000.0))
    logs += [logrow(5, -0.2, 0.0), logrow(4, -2, 60_000.0), logrow(99, -0.2, 1e6)]
    runs = []
    for k in range(1, 25):
        rows = [(ca(1), pool(1), "100" if k in (1, 2) else "60000", "1"),
                (ca(2), pool(2), "20000", "1"),
                (ca(3), pool(3), "n/a", "1"),
                (ca(7), pool(7), "60000", "1"),
                (ca(8), pool(99), "60000", "1")]
        runs.append((3 * k, rows))
    write_tree(fu, lg, enrolled, logs, runs)
    c, reasons, clauses, arms, shape, latest = P.run(NOW)
    assert latest == h(72)
    assert c == {"enrolled": 8, "no_entry": 1, "liq": 1, "lag": 1, "immature": 1,
                 "eligible": 4, "censored": 2, "evaluable": 2, "dead": 1}
    assert reasons == {"12+ rows but fewer than 12 with a numeric reserve": 1,
                       "no watch rows in window": 1}
    assert clauses == {"L": 1, "P": 0, "both": 0, "mixed": 0}
    assert arms == {"hi": 3, "lo": 1}
    text = P.report(NOW, c, reasons, clauses, arms, shape, latest)
    assert ca(1) not in text and pool(1) not in text     # no per-token rows


# ------------------------------------------------------------------ stats

def test_wilson_and_sizing():
    lo, hi = P.wilson(3, 26)
    assert 0.03 < lo < 0.05 and 0.27 < hi < 0.31
    assert P.wilson(0, 0) == (None, None)
    assert P.n_per_arm(0.5, -0.10) > P.n_per_arm(0.5, -0.20)
    assert P.n_per_arm(0.02, -0.10) is None
