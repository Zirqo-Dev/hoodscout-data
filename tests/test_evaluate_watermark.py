"""evaluate.py and a hypothesis that has not started.

Registering H-0004 and H-0005 with a null log_watermark -- which is what
"waiting for its first row" looks like -- took the evaluator down for every
hypothesis, including the three that could be evaluated:

    if o["h"] == h["horizon"] and o["ts"] > since
    TypeError: '>' not supported between instances of 'str' and 'NoneType'

Fixed in 646ee0a. These are the checks that fix was verified against,
carried into the suite so the next null-watermark registration cannot
reintroduce it.
"""
import pytest

import evaluate as E

LABEL_ROW = {
    "h": "6h", "ts": "2026-10-19T00:00:00+00:00", "status": "ok",
    "ret_net_500": 1.0, "ca": "0xaa", "fee_source": "name",
    "tier": "BEST", "rms": 0, "dt": None, "_base": 0.0, "_peers": [],
}


@pytest.fixture
def unstarted(fixture):
    return fixture("hypothesis_unstarted.json")


@pytest.fixture
def started(fixture):
    return fixture("hypothesis_h0001.json")


def test_null_watermark_does_not_raise(unstarted):
    assert unstarted["log_watermark"] is None
    E.evaluate(unstarted, [LABEL_ROW], None)      # the exact crash


def test_null_watermark_is_not_started(unstarted):
    r = E.evaluate(unstarted, [LABEL_ROW], None)
    assert r["status"] == "not started"
    assert r["no_watermark"] is True


def test_not_started_carries_no_gates(unstarted):
    """render() and the console summary both key off the absent gates, so an
    unstarted hypothesis must not grow an empty gate table."""
    r = E.evaluate(unstarted, [LABEL_ROW], None)
    assert "gates" not in r
    assert r["rows"] == 0


def test_a_missing_watermark_key_behaves_like_an_explicit_null(unstarted):
    h = {k: v for k, v in unstarted.items() if k != "log_watermark"}
    assert E.evaluate(h, [LABEL_ROW], None)["status"] == "not started"


def test_since_does_not_start_an_unstarted_hypothesis(unstarted):
    """--since is an exercise override, not a licence to invent a start date
    for a hypothesis that has never had one."""
    r = E.evaluate(unstarted, [LABEL_ROW], "2026-01-01T00:00:00+00:00")
    assert r["status"] == "not started"
    assert r["no_watermark"] is True


def test_a_started_hypothesis_is_still_evaluated(started):
    """The regression that matters: the short-circuit must not swallow a
    hypothesis that has a watermark."""
    r = E.evaluate(started, [LABEL_ROW], started["log_watermark"])
    assert r.get("no_watermark") is None
    assert r["status"] != "not started"
    assert "gates" in r


def meta_for(h, n_unstarted=1):
    return {
        "run_at": "2026-10-20T00:00:00+00:00", "evaluator": "e" * 40,
        "labeler": "l" * 40, "rows": 1, "snaps": 1, "span": "a .. b",
        "pile": "scored", "n_hyp": 2, "n_open": 1,
        "n_unstarted": n_unstarted, "mismatch": None, "since_override": None,
        "by_id": {h["id"]: h},
    }


def test_render_never_slices_a_null_watermark(unstarted):
    """The second latent crash on the same null: the registered-clauses block
    slices h["log_watermark"][:19]. The not-started branch returns first."""
    out = E.render([{"id": unstarted["id"], "status": "not started",
                     "rows": 0, "no_watermark": True}], meta_for(unstarted))
    assert "registered clauses" not in out


def test_render_says_why_it_is_not_started(unstarted):
    out = E.render([{"id": unstarted["id"], "status": "not started",
                     "rows": 0, "no_watermark": True}], meta_for(unstarted))
    assert "**Not started.**" in out
    assert "`log_watermark` is null" in out
    assert "`--since` does not override this" in out


def test_render_does_not_call_it_a_missing_rule(unstarted):
    """It is a different thing from "no rule implemented", and the report has
    to tell a reader which."""
    out = E.render([{"id": unstarted["id"], "status": "not started",
                     "rows": 0, "no_watermark": True}], meta_for(unstarted))
    assert "No rule implemented" not in out


def test_render_counts_the_unstarted_in_the_header(unstarted):
    out = E.render([{"id": unstarted["id"], "status": "not started",
                     "rows": 0, "no_watermark": True}],
                   meta_for(unstarted, n_unstarted=2))
    assert "2 not started" in out
