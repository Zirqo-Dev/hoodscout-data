"""analysis/hs92/power.py: entry, outcome, coverage, cost and the power draw.

Synthetic candles and rows only: no network, no repo data. power.py is an
analysis script, not a pipeline script, so it is loaded from its own path.
"""
import importlib.util
import random
from datetime import datetime, timedelta, timezone

import pytest

from conftest import ROOT

_spec = importlib.util.spec_from_file_location(
    "hs92_power", ROOT / "analysis" / "hs92" / "power.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

FS = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)      # first_seen, pre-watermark
ENTRY = FS + timedelta(hours=2)                              # hour-aligned entry
END = FS + timedelta(days=7)
H = timedelta(hours=1)


def token(enrolled=ENTRY, first_seen=FS):
    return {"ca": "0x" + "a" * 40, "main_pool": "0x" + "b" * 40,
            "first_seen": first_seen.isoformat(), "enrolled_at": enrolled.isoformat()}


def flat(start=FS - 2 * H, stop=END + H, price=1.0):
    """Hourly candles at a flat price, opens in [start, stop)."""
    out, t = [], start
    while t < stop:
        out.append((t, price, price, price, price))
        t += H
    return out


def put(candles, at, high=None, low=None):
    """Candles with the one opening at `at` given a new high and/or low."""
    out = []
    for c in candles:
        if c[0] == at:
            c = (c[0], c[1], high if high is not None else c[2],
                 low if low is not None else c[3], c[4])
        out.append(c)
    return out


def run(candles, e=None, entry=ENTRY, price=1.0):
    return P.outcome(e or token(), entry, price, candles)


# ------------------------------------------------------------------ entry

def row(dt, **kw):
    return {"ts": dt.isoformat(), "dt": dt, "ca": "0x" + "a" * 40, **kw}


def test_entry_is_last_row_inside_30_min_and_none_outside():
    t = ENTRY
    rows = [row(t - timedelta(minutes=45), price=1), row(t - timedelta(minutes=20), price=2),
            row(t - timedelta(minutes=5), price=3), row(t + timedelta(minutes=1), price=4)]
    assert P.entry_row(rows, t)["price"] == 3
    assert P.entry_row(rows[:1], t) is None
    assert P.entry_row([row(t - timedelta(minutes=30), price=5)], t)["price"] == 5


def test_load_log_dedups_on_ts_ca_first_kept(tmp_path):
    d = tmp_path / "2026-10-20"
    d.mkdir()
    a, b = "0x" + "a" * 40, "0x" + "c" * 40
    ts = ENTRY.isoformat()
    (d / "000000.jsonl").write_text(
        f'{{"ts":"{ts}","ca":"{a}","price":1}}\n{{"ts":"{ts}","ca":"{b}","price":9}}\n')
    (d / "000001.jsonl").write_text(f'{{"ts":"{ts}","ca":"{a}","price":2}}\n')
    out = P.load_log({a}, str(tmp_path))
    assert [r["price"] for r in out[a]] == [1] and b not in out


# ------------------------------------------------------------------ outcome

def test_timeout_on_flat_covered_window_exits_at_last_close_before_end():
    c = flat()
    c = [x if x[0] != END - H else (x[0], 1, 1, 1, 1.3) for x in c]
    out, exit_price, info = run(c)
    assert out == "TIMEOUT" and exit_price == 1.3
    assert info["covered"] and info["missing_hours"] == 0
    assert info["window_hours"] == int((END - ENTRY) / H)


def test_hit_before_stop():
    c = put(put(flat(), ENTRY + 5 * H, high=2.0), ENTRY + 9 * H, low=0.4)
    assert run(c)[0] == "HIT"


def test_stop_before_hit():
    c = put(put(flat(), ENTRY + 5 * H, low=0.5), ENTRY + 9 * H, high=3.0)
    assert run(c)[0] == "STOP"


def test_one_candle_touching_both_is_stop():
    c = put(flat(), ENTRY + 5 * H, high=2.5, low=0.4)
    assert run(c)[0] == "STOP"


def test_just_inside_levels_is_not_a_touch():
    c = put(flat(), ENTRY + 5 * H, high=1.999, low=0.501)
    assert run(c)[0] == "TIMEOUT"


def test_candle_containing_entry_and_before_are_ignored():
    entry = ENTRY + timedelta(minutes=20)
    c = put(put(flat(), ENTRY, high=5.0), ENTRY - H, low=0.1)
    assert run(c, entry=entry)[0] == "TIMEOUT"
    assert run(put(flat(), ENTRY + H, high=5.0), entry=entry)[0] == "HIT"


def test_candle_closing_after_window_end_is_ignored():
    c = put(flat(), END, high=9.0)          # opens at the end: outside
    assert run(c)[0] == "TIMEOUT"
    assert run(put(flat(), END - H, high=9.0))[0] == "HIT"   # closes at the end: inside


def test_censored_when_candles_stop_short_and_no_level():
    c = flat(stop=END - 3 * H)
    out, _, info = run(c)
    assert out == "CENSORED" and not info["covered"] and info["missing_hours"] == 3


def test_hit_inside_partial_coverage_still_counts():
    c = put(flat(stop=END - 30 * H), ENTRY + 4 * H, high=2.0)
    assert run(c)[0] == "HIT"


def test_missing_hours_inside_covered_window_are_counted_not_censored():
    c = [x for x in flat() if x[0] not in (ENTRY + 3 * H, ENTRY + 4 * H)]
    out, _, info = run(c)
    assert out == "TIMEOUT" and info["missing_hours"] == 2


def test_full_200_candle_file_starting_after_entry_is_not_covered():
    c = flat(start=END + H - P.CANDLE_LIMIT * H, stop=END + H)
    assert len(c) == P.CANDLE_LIMIT
    assert run(c, entry=c[0][0] - 2 * H)[0] == "CENSORED"
    assert run(c, entry=c[0][0])[0] == "TIMEOUT"


def test_outcome_refuses_post_watermark_enrolment():
    late = P.WATERMARK + timedelta(minutes=1)
    with pytest.raises(AssertionError):
        P.outcome(token(enrolled=late, first_seen=late - H), late, 1.0, flat())


# ------------------------------------------------------------------ checks and cost

def test_entry_ratio_uses_last_close_at_or_before_entry_only():
    c = [(ENTRY - 2 * H, 1, 1, 1, 0.5), (ENTRY - H, 1, 1, 1, 0.8), (ENTRY, 1, 1, 1, 100.0)]
    assert P.entry_ratio(1.0, c, ENTRY) == pytest.approx(1.25)
    assert P.entry_ratio(1.0, c[2:], ENTRY) is None


def test_cost_is_label_nets_with_liq_out_equal_liq_in():
    assert P.cost({"liq": 100_000.0, "fee": 0.003}) == pytest.approx(2 * 0.003 + 4 * 500 / 100_000)
    assert P.cost({"liq": 100_000.0, "fee": None}) == pytest.approx(
        2 * P.label.FEE_FALLBACK + 4 * 500 / 100_000)
    assert P.cost({"liq": 1000.0, "fee": 0.01}) == 1.0     # floored at -100% net
    assert P.cost({"liq": 0, "fee": 0.01}) is None


# ------------------------------------------------------------------ power

def test_shifted_raises_evaluable_hit_share_and_keeps_censored():
    p = {"HIT": 0.2, "STOP": 0.4, "TIMEOUT": 0.2, "CENSORED": 0.2}
    q, capped = P.shifted(p, 10)
    assert not capped
    assert q["HIT"] / 0.8 == pytest.approx(0.25 + 0.10)
    assert q["CENSORED"] == 0.2 and sum(q.values()) == pytest.approx(1.0)
    assert q["STOP"] / q["TIMEOUT"] == pytest.approx(2.0)
    assert P.shifted(p, 0)[0] == pytest.approx(p)


def test_pass_rate_is_seeded_and_extremes_behave():
    p = {"HIT": 0.3, "STOP": 0.4, "TIMEOUT": 0.2, "CENSORED": 0.1}
    r1 = P.pass_rate(p, p, 0.2, 40, 160, 500, random.Random(1))
    r2 = P.pass_rate(p, p, 0.2, 40, 160, 500, random.Random(1))
    assert r1 == r2 and 0 < r1 < 1
    assert P.pass_rate(p, p, 1.01, 40, 160, 200, random.Random(2)) == 0.0
    sure = {"HIT": 1.0, "STOP": 0.0, "TIMEOUT": 0.0, "CENSORED": 0.0}
    none = {"HIT": 0.0, "STOP": 1.0, "TIMEOUT": 0.0, "CENSORED": 0.0}
    assert P.pass_rate(sure, none, 0.5, 40, 160, 200, random.Random(3)) == 1.0
