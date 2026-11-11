"""analysis/h0003-poolswitch/poolswitch.py: exit-row lookup, pool-switch
classification, and the refusals that keep it off H-0009 rows.

Tiny synthetic rows only: no network, no real data. poolswitch.py is an
analysis script, so it is loaded from its own path; label.py is the real one.
"""
import importlib.util
from datetime import datetime, timedelta, timezone

import pytest

import label
from conftest import ROOT

_spec = importlib.util.spec_from_file_location(
    "h0003_poolswitch", ROOT / "analysis" / "h0003-poolswitch" / "poolswitch.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

T0 = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
CA = "0x" + "a" * 40


def row(h, pool, price=1.0, liq=50_000.0, ca=CA):
    dt = T0 + timedelta(hours=h)
    return {"ts": dt.isoformat(), "dt": dt, "ca": ca, "tier": "EMERGING",
            "rms": 0, "price": price, "liq": liq, "txns": 300, "pool": pool,
            "fee": None}


def label_6h(rows):
    """label.outcome() on the first row, with the indexes load_log builds."""
    rows = sorted(rows, key=lambda r: r["dt"])
    by_ca, times = {CA: rows}, {CA: [r["dt"] for r in rows]}
    o = label.outcome(rows[0], "6h", by_ca, times, rows[-1]["dt"])
    return o, {(r["ts"], r["ca"]): r for r in rows}


def test_exit_from_other_pool_is_a_switch():
    o, entry_of = label_6h([row(0, "0xpoolA"), row(6, "0xpoolB", price=9.0)])
    assert o["status"] == "ok"
    assert P.exit_row(o, entry_of)["pool"] == "0xpoolB"
    assert P.classify(o, entry_of) == "switch"


def test_nearest_print_is_the_exit_row():
    # two prints in the window: the one nearer entry+6h is the exit row
    o, entry_of = label_6h([row(0, "0xA"), row(5.5, "0xB"), row(6.25, "0xA")])
    assert o["t_actual"] == row(6.25, "0xA")["ts"]
    assert P.classify(o, entry_of) == "same"


def test_gone_no_liq_uses_its_zero_liq_print():
    o, entry_of = label_6h([row(0, "0xA"), row(6, "0xB", liq=0.0)])
    assert o["status"] == "gone_no_liq"
    assert P.classify(o, entry_of) == "switch"


def test_gone_unseen_has_no_exit_row():
    o, entry_of = label_6h([row(0, "0xA"), row(10, "0xA")])
    assert o["status"] == "gone_unseen"
    assert P.classify(o, entry_of) == "no_exit"


def test_null_pool_is_unknown_not_switch():
    o, entry_of = label_6h([row(0, None), row(6, "0xB")])
    assert P.classify(o, entry_of) == "unknown"


def test_refuses_rows_at_or_after_h0009_watermark():
    wm = datetime.fromisoformat(P.H0009_WATERMARK)
    before = {"dt": wm - timedelta(seconds=1)}
    assert P.check_before_watermark([before]) == before["dt"]
    with pytest.raises(P.Refused):
        P.check_before_watermark([before, {"dt": wm}])
    with pytest.raises(P.Refused):
        P.check_before_watermark([])


def test_refuses_outside_the_pin(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)          # not a worktree at the pin
    assert P.main() == 2
    assert "REFUSED" in capsys.readouterr().err


def test_token_row_match():
    assert P.is_token_row({"ca": P.TOKEN, "ts": "2026-10-27T20:00:31+00:00"})
    assert not P.is_token_row({"ca": P.TOKEN, "ts": "2026-10-27T21:00:31+00:00"})
    assert not P.is_token_row({"ca": CA, "ts": "2026-10-27T20:00:31+00:00"})
