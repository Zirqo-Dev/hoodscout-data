"""label.py's fee model and net-return arithmetic.

The fee is known for only part of the sample, so every claim the evidence
layer makes has to survive the fee being wrong. These tests pin the two
properties that argument rests on: a net return moves by exactly twice a fee
error, and a row whose fee came from the pool name does not move at all.
"""
import pytest

import label

GROSS = 0.10
LIQ = 1_000_000.0          # deep enough that impact is small but not zero
IMPACT_500 = 2 * (2.0 * 500 / LIQ)      # both sides of the round trip
IMPACT_5K = 2 * (2.0 * 5000 / LIQ)


def test_fee_sweep_is_the_observed_range():
    """0, 0.3%, 1% and 1.9% -- the range seen on this chain. The fallback sits
    inside it, near the top."""
    assert label.FEE_SWEEP == (0.0, 0.003, 0.01, 0.019)
    assert label.FEE_FALLBACK == 0.01
    assert label.FEE_FALLBACK in label.FEE_SWEEP
    assert min(label.FEE_SWEEP) <= label.FEE_FALLBACK <= max(label.FEE_SWEEP)


@pytest.mark.parametrize("fee", label.FEE_SWEEP)
def test_nets_across_the_sweep(fee):
    out = label.nets(GROSS, LIQ, LIQ, fee)
    assert out["ret_net_500"] == pytest.approx(GROSS - 2 * fee - IMPACT_500,
                                               abs=1e-6)
    assert out["ret_net_5000"] == pytest.approx(GROSS - 2 * fee - IMPACT_5K,
                                                abs=1e-6)


def test_the_larger_notional_is_never_better():
    """Impact scales with notional, so 5k cannot beat 500 at the same fee."""
    for fee in label.FEE_SWEEP:
        out = label.nets(GROSS, LIQ, LIQ, fee)
        assert out["ret_net_5000"] <= out["ret_net_500"]


def test_a_fee_error_moves_net_by_exactly_twice_itself():
    """The claim the summary prints, and the reason a fee error largely
    cancels in a difference between two groups with similar unknown-fee
    shares. Checked across every pair in the sweep."""
    for a in label.FEE_SWEEP:
        for b in label.FEE_SWEEP:
            na = label.nets(GROSS, LIQ, LIQ, a)["ret_net_500"]
            nb = label.nets(GROSS, LIQ, LIQ, b)["ret_net_500"]
            assert na - nb == pytest.approx(-2 * (a - b), abs=2e-6)


def test_renet_moves_by_twice_the_fee_error_too():
    """renet() re-nets a stored outcome at another fee. It must obey the same
    2x rule as nets(), or a fee sweep over stored rows would not measure what
    a fee sweep over fresh ones does."""
    o = {"ret_gross": GROSS, "liq_in": LIQ, "liq_out": LIQ,
         "fee_source": "fallback"}
    for a in label.FEE_SWEEP:
        for b in label.FEE_SWEEP:
            assert label.renet(o, a) - label.renet(o, b) == \
                pytest.approx(-2 * (a - b), abs=1e-9)


def test_renet_agrees_with_nets_at_the_same_fee():
    """renet() reads ret_gross, which nets() rounded to 6dp, so the two agree
    to about 1e-6 and not exactly. The documented tolerance is 9.8e-7."""
    stored = label.nets(GROSS, LIQ, LIQ, label.FEE_FALLBACK)
    o = {"ret_gross": round(GROSS, 6), "liq_in": LIQ, "liq_out": LIQ}
    assert label.renet(o, label.FEE_FALLBACK) == \
        pytest.approx(stored["ret_net_500"], abs=1e-6)


def test_net_is_floored_at_minus_one():
    """A round trip cannot lose more than it put in."""
    out = label.nets(-0.99, 100.0, 100.0, 0.019)
    assert out["ret_net_500"] == -1.0
    assert out["ret_net_5000"] == -1.0


def test_a_floored_row_does_not_move_in_a_sweep():
    o = {"ret_gross": -0.999, "liq_in": 100.0, "liq_out": 100.0}
    assert label.renet(o, 0.0) == -1.0
    assert label.renet(o, 0.019) == -1.0


def test_impact_counts_both_sides_and_skips_missing_liquidity():
    """A side with no liquidity figure contributes no impact rather than
    dividing by zero."""
    one_side = label.nets(GROSS, LIQ, None, 0.0)["ret_net_500"]
    assert one_side == pytest.approx(GROSS - IMPACT_500 / 2, abs=1e-6)
    neither = label.nets(GROSS, 0.0, None, 0.0)["ret_net_500"]
    assert neither == pytest.approx(GROSS, abs=1e-9)


def test_net_at_leaves_a_known_fee_row_alone():
    """A fee read from the pool name is already right. A sweep must not move
    it, or the sweep would manufacture uncertainty the row does not have."""
    o = {"ret_gross": GROSS, "liq_in": LIQ, "liq_out": LIQ,
         "fee_source": "name", "ret_net_500": 0.0777}
    for fee in label.FEE_SWEEP:
        assert label.net_at(o, fee) == 0.0777


def test_net_at_re_nets_an_unknown_fee_row():
    o = {"ret_gross": GROSS, "liq_in": LIQ, "liq_out": LIQ,
         "fee_source": "fallback", "ret_net_500": 0.0777}
    assert label.net_at(o, 0.0) != 0.0777
    assert label.net_at(o, 0.0) == pytest.approx(GROSS - IMPACT_500, abs=1e-9)


def test_renet_on_a_row_with_no_gross_is_none():
    assert label.renet({"ret_gross": None}, 0.01) is None
    assert label.renet({}, 0.01) is None
