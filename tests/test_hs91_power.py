"""analysis/hs91/power.py: grid matching and the DEAD_72H death rule.

Tiny synthetic rows only: no network, no real data. power.py is an analysis
script, not a pipeline script, so it is loaded from its own path.
"""
import importlib.util
import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import ROOT

_spec = importlib.util.spec_from_file_location(
    "hs91_power", ROOT / "analysis" / "hs91" / "power.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

T0 = datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)
CA = "0x" + "a" * 40


def row(h, liq=100_000.0, price=1.0, ca=CA):
    dt = T0 + timedelta(hours=h)
    return {"ca": ca, "ts": dt.isoformat(), "dt": dt, "liq": liq, "price": price}


def grid(rows, n=24, entry_h=0):
    rows = sorted(rows, key=lambda r: r["dt"])
    return P.grid_prints(rows, [r["dt"] for r in rows], T0 + timedelta(hours=entry_h), n)


# ------------------------------------------------------------------ grid

def test_grid_picks_nearest_print_within_45_min():
    pts = grid([row(2.5, liq=1), row(3.25, liq=2), row(3.6, liq=3)], n=1)
    assert pts[0]["liq"] == 2


def test_grid_window_edges_are_inclusive_and_beyond_is_unknown():
    pts = grid([row(3.75, liq=1), row(5.24, liq=2)], n=2)
    assert pts[0]["liq"] == 1           # exactly +45 min: in
    assert pts[1] is None               # 6 h point: 5.24 h is 46 min early


def test_grid_tie_goes_to_earlier_print():
    pts = grid([row(2.5, liq=1), row(3.5, liq=2)], n=1)
    assert pts[0]["liq"] == 1


def test_grid_starts_one_step_after_entry():
    pts = grid([row(0, liq=1), row(3, liq=2)], n=1)
    assert pts[0]["liq"] == 2
    assert len(grid([], n=24)) == 24


# ------------------------------------------------------------------ death rule

@pytest.mark.parametrize("entry_liq, liq, dead", [
    (100_000, 9_999, True),       # below 10% of entry
    (100_000, 10_000, False),     # exactly 10%: not below
    (5_000, 999, True),           # below the 1000 absolute floor
    (5_000, 1_000, False),        # 10% of 5k is 500, floor 1000: not below
    (100_000, None, True),        # missing liq counts as 0
])
def test_point_dead_liquidity_leg(entry_liq, liq, dead):
    assert P.point_dead({"liq": liq, "price": 1.0}, entry_liq, 1.0) is dead


@pytest.mark.parametrize("price, dead", [(0.10, True), (0.1001, False), (None, False)])
def test_point_dead_price_leg(price, dead):
    assert P.point_dead({"liq": 100_000, "price": price}, 100_000, 1.0) is dead


def _pts(pattern):
    """'D' dead print, 'A' alive print, '.' unknown."""
    m = {"D": {"liq": 0.0, "price": 1.0}, "A": {"liq": 100_000.0, "price": 1.0}, ".": None}
    return [m[c] for c in pattern]


@pytest.mark.parametrize("pattern, want", [
    ("DD" + "A" * 22, "dead"),
    ("A" * 22 + "DD", "dead"),
    ("DADADA" * 4, "alive"),               # never two in a row
    ("D.D" + "A" * 21, "alive"),           # unknown breaks the run
    ("DD" + "." * 22, "censored"),         # 2 known < 12: censored even if dead
    ("A" * 12 + "." * 12, "alive"),        # exactly 12 known: not censored
    ("A" * 11 + "." * 13, "censored"),
])
def test_classify(pattern, want):
    assert P.classify(_pts(pattern), 100_000.0, 1.0, 12) == want


# ------------------------------------------------------------------ entry and file

def test_entry_is_fourth_distinct_snapshot_and_horizons(tmp_path):
    rows = [row(h, liq=100_000.0) for h in (0, 0, 0.5, 1, 1.5)]   # dup ts at 0
    rows[4]["liq"] = 60_000.0                                     # 4th distinct ts
    entry_h = 1.5
    for k in range(1, 25):
        dead = k in (10, 11)
        rows.append(row(entry_h + 3 * k + 0.1, liq=1.0 if dead else 60_000.0))
    rows.append(row(entry_h + 80))           # history runs past the 72 h grid
    path = tmp_path / "h.jsonl"
    path.write_text("".join(json.dumps({k: v for k, v in r.items() if k != "dt"}) + "\n"
                            for r in rows))
    (rec,) = P.tokens(P.load_rows(path))
    assert rec["liq"] == 60_000.0 and rec["lag_h"] == pytest.approx(1.5)
    assert rec[24] == "alive" and rec[72] == "dead" and rec["known72"] == 24
    assert P.arm(rec, 50_000) == "hi" and P.arm(rec, 100_000) == "lo"


def test_too_few_snapshots_and_pending(tmp_path):
    rows = [row(h) for h in (0, 1, 2)] + [row(h, ca="0x" + "b" * 40) for h in (0, 1, 2, 3, 10)]
    path = tmp_path / "h.jsonl"
    path.write_text("".join(json.dumps({k: v for k, v in r.items() if k != "dt"}) + "\n"
                            for r in rows))
    (rec,) = P.tokens(P.load_rows(path))
    assert rec[24] == "pending" and rec[72] == "pending"


def test_load_rows_refuses_post_watermark(tmp_path):
    path = tmp_path / "h.jsonl"
    path.write_text(json.dumps({"ca": CA, "ts": "2026-10-18T13:30:25+00:00"}) + "\n")
    with pytest.raises(AssertionError):
        P.load_rows(path)


def test_simulation_is_seeded_and_null_shares_one_pool():
    import random
    a = P.simulate([1, 0, 0, 0], [0, 0], 50, 20, random.Random(1))
    b = P.simulate([1, 0, 0, 0], [0, 0], 50, 20, random.Random(1))
    assert a == b
    assert all(g >= 0 for g in a[1])        # the low arm has no deaths to draw
