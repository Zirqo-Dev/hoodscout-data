"""label.py's exit statuses, which decide what counts as knowledge.

The distinction that matters is between a token that was last seen with its
liquidity gone -- a real loss -- and one that simply stopped printing, which
is censored and must never be scored. Getting that backwards would invent
total losses out of tokens nobody could have exited, and every eff_n and
median downstream reads against it.
"""
from datetime import timedelta

import pytest

import label

H = "6h"
HOURS, TOL, _STRIDE = label.HORIZONS[H]


def entry_for(rows, ca):
    return min((r for r in rows if r["ca"] == ca), key=lambda r: r["dt"])


def outcome_for(label_log, ca, latest_offset_h=48.0):
    rows, by_ca, times, t0 = label_log
    latest = t0 + timedelta(hours=latest_offset_h)
    return label.outcome(entry_for(rows, ca), H, by_ca, times, latest)


def test_horizon_window():
    """The fixture's offsets are chosen against these, so pin them."""
    assert (HOURS, TOL) == (6.0, 0.75)


def test_ok_when_a_deep_print_lands_in_the_window(label_log):
    o = outcome_for(label_log, "0x0a")
    assert o["status"] == "ok"
    assert o["ret_gross"] == pytest.approx(0.1, abs=1e-6)
    assert o["ret_net_500"] is not None
    assert o["dt_h"] == 6.0
    assert o["enterable"] is True


def test_a_known_fee_is_recorded_as_such(label_log):
    o = outcome_for(label_log, "0x0a")
    assert o["fee"] == 0.003
    assert o["fee_source"] == "name"
    assert o["fee_used"] == 0.003


def test_an_unknown_fee_falls_back_and_says_so(label_log):
    o = outcome_for(label_log, "0x0b")
    assert o["fee"] is None
    assert o["fee_source"] == "fallback"
    assert o["fee_used"] == label.FEE_FALLBACK


def test_thin_when_either_side_is_shallow(label_log):
    """Still a number, but flagged: the larger notional is not exitable."""
    o = outcome_for(label_log, "0x0b")
    assert o["status"] == "thin"
    assert min(o["liq_in"], o["liq_out"]) < label.THIN_LIQ
    assert o["ret_net_500"] is not None


def test_gone_no_liq_is_a_loss(label_log):
    """Last seen with the liquidity gone. That is an exit at zero, and the
    only case where -100% is asserted rather than withheld."""
    o = outcome_for(label_log, "0x0c")
    assert o["status"] == "gone_no_liq"
    assert o["ret_gross"] == -1.0
    assert o["ret_net_500"] == -1.0
    assert o["liq_out"] == 0.0


def test_gone_unseen_is_censored_not_a_loss(label_log):
    """It stopped printing. That is an absence of evidence, so every return
    is None and the row contributes to cens% instead of to a median."""
    o = outcome_for(label_log, "0x0d")
    assert o["status"] == "gone_unseen"
    assert o["ret_gross"] is None
    assert o["ret_net_500"] is None
    assert o["ret_net_5000"] is None
    assert o["t_actual"] is None


def test_no_liquidity_at_entry_withholds_the_number(label_log):
    """Not a position anyone could have opened. The exit is still recorded --
    a deep print did land in the window -- and only the return is withheld.

    The status is "thin" rather than "ok" because the thin test is
    min(liq_in, liq_out) < THIN_LIQ and liq_in is zero here, so a
    non-enterable row trips it by construction. Harmless, since enterable
    already withholds the number, but asserted as it is rather than as it
    reads.
    """
    o = outcome_for(label_log, "0x0e")
    assert o["enterable"] is False
    assert o["status"] == "thin"
    assert o["ret_gross"] is None
    assert o["ret_net_500"] is None
    assert o["liq_out"] == 100000.0


def test_pending_is_none_rather_than_materialised(label_log):
    """Before the window opens there is no outcome at all, not a row with
    nulls that something downstream might count."""
    assert outcome_for(label_log, "0x0a", latest_offset_h=1.0) is None


def test_the_window_opens_at_horizon_minus_tolerance(label_log):
    just_before = HOURS - TOL - 0.01
    just_after = HOURS - TOL + 0.01
    assert outcome_for(label_log, "0x0a", latest_offset_h=just_before) is None
    assert outcome_for(label_log, "0x0a", latest_offset_h=just_after) is not None


def test_every_status_is_one_of_the_four(label_log):
    rows, by_ca, times, t0 = label_log
    latest = t0 + timedelta(hours=48)
    seen = set()
    for ca in sorted({r["ca"] for r in rows}):
        o = label.outcome(entry_for(rows, ca), H, by_ca, times, latest)
        if o:
            seen.add(o["status"])
    assert seen <= {"ok", "thin", "gone_no_liq", "gone_unseen"}
    assert seen == {"ok", "thin", "gone_no_liq", "gone_unseen"}


def test_thinning_keeps_one_snapshot_per_stride():
    """Snapshots, not rows: thinning changes how often a token is represented,
    never which tokens are."""
    ts = [f"2026-10-01T{h:02d}:00:00+00:00" for h in range(12)]
    assert label.thin_snapshots(set(ts), 0.0) == set(ts)
    kept = label.thin_snapshots(set(ts), 6.0)
    assert len(kept) == 2
    assert min(ts) in kept
