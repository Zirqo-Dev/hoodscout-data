"""verify_stocks.py fails safe when its reference control fails.

The bytecode test compares a candidate against two known-good contracts. If
those two do not match each other (or either code is empty, e.g. an RPC that
returned nothing), the comparison proves nothing, so no candidate may be
given a match verdict and the run must exit non-zero.

No network: every external call is monkeypatched and all hex is synthetic.
"""
import pytest

import verify_stocks as V

GOOD = "aa" * 40          # synthetic "reference" runtime code
OTHER = "bb" * 40         # synthetic code that matches nothing
CA1 = "0x" + "1" * 40
CA2 = "0x" + "2" * 40

MATCH = "MATCH (name + code)"
NO_MATCH = "NO MATCH"
INCONCLUSIVE = "INCONCLUSIVE (reference control failed)"


@pytest.fixture
def run(monkeypatch, capsys):
    """Run main() for one ticker; returns (exit_code, stdout, depth_calls)."""
    def go(ref_codes, cands):
        by_addr = dict(zip(V.REFERENCE.values(), ref_codes))
        by_addr.update({ca: code for ca, (code, _) in cands.items()})
        depth_calls = []

        monkeypatch.setattr(V, "code_of", lambda a: by_addr[a])
        monkeypatch.setattr(V, "call", lambda to, sel: "0x")
        monkeypatch.setattr(V, "candidates",
                            lambda t: {ca: {"pools": 1, "reserve": 1.0,
                                            "created": [], "names": set()}
                                       for ca in cands})

        def describe(ca, refs):
            code, suffix_ok = cands[ca]
            return {"ca": ca, "name": "x", "symbol": "X", "decimals": 18,
                    "total_supply": 1.0, "code_len": len(code) // 2,
                    "code_sha256": "00",
                    "bytecode": {s: {"exact": code == r, "similarity_pct": 0.0,
                                     "len_delta": 0} for s, r in refs.items()},
                    "name_suffix_ok": suffix_ok}

        monkeypatch.setattr(V, "describe", describe)
        monkeypatch.setattr(V, "pool_depth",
                            lambda ca: depth_calls.append(ca) or {"count": 0})
        monkeypatch.setattr(V.time, "sleep", lambda s: None)
        monkeypatch.setattr(V.sys, "argv", ["verify_stocks.py", "XYZ"])
        code = V.main()
        return code, capsys.readouterr().out, depth_calls
    return go


def verdicts(out):
    return [l.split(":", 1)[1].strip() for l in out.splitlines()
            if "VERDICT" in l]


def test_control_ok_exact_candidate_matches(run):
    code, out, depth = run([GOOD, GOOD], {CA1: (GOOD, True)})
    assert verdicts(out) == [MATCH]
    assert depth == [CA1]
    assert code == 0
    assert "REFERENCE CONTROL FAILED" not in out


def test_control_mismatch_is_inconclusive_for_every_candidate(run):
    # Both candidates would be a MATCH and a NO MATCH respectively if the
    # control held; neither may get a match verdict now.
    code, out, depth = run([GOOD, OTHER],
                           {CA1: (GOOD, True), CA2: (OTHER, True)})
    assert verdicts(out) == [INCONCLUSIVE, INCONCLUSIVE]
    assert "REFERENCE CONTROL FAILED: bytecode test not usable" in out
    assert MATCH not in out and NO_MATCH not in out
    assert depth == []
    assert code == 2


@pytest.mark.parametrize("refs", [["", GOOD], [GOOD, ""], ["", ""]])
def test_empty_reference_code_exits_2(run, refs):
    # "" == "" would pass a bare equality check; emptiness must fail it.
    code, out, depth = run(refs, {CA1: (GOOD, True)})
    assert verdicts(out) == [INCONCLUSIVE]
    assert depth == []
    assert code == 2


def test_control_ok_suffix_ok_but_no_exact_code_is_no_match(run):
    code, out, depth = run([GOOD, GOOD], {CA1: (OTHER, True)})
    assert verdicts(out) == [NO_MATCH]
    assert depth == []
    assert code == 0
