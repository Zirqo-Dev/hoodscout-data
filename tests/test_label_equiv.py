"""scripts/label_equiv.py: the blind labeler equivalence check.

Synthetic log only, written to a temp directory; nothing here reads data/.
The rows are dated 2031 so they all sit after the real watermark the script
filters on.
"""
import json
import random
import re
from datetime import datetime, timedelta, timezone

import pytest

import label
import label_equiv

T0 = datetime(2031, 1, 6, tzinfo=timezone.utc)
LINE_OK = (re.compile(r"^(registered_blob|current_blob): [0-9a-f]{12}$"),
           re.compile(r"^[a-z_]+: \d+$"))


@pytest.fixture
def synth_log(tmp_path, monkeypatch):
    """Eight tokens every 15 minutes for 10h: half with a fee, half falling
    back, one that loses its liquidity, one that stops appearing."""
    rng = random.Random(7)
    day = tmp_path / "2031-01-06"
    day.mkdir()
    lines = []
    for step in range(41):
        ts = (T0 + timedelta(minutes=15 * step)).isoformat()
        for i in range(8):
            if i == 7 and step > 8:
                continue                                  # gone_unseen
            liq = 0.0 if i == 6 and step > 12 else 50000.0 * (1 + i)
            lines.append({"ts": ts, "ca": f"0x{i:040x}", "tier": "BEST",
                          "price": 1.0 + rng.uniform(-0.2, 0.2), "liq": liq,
                          "txns": 500, "fee": 0.003 if i % 2 else None})
    (day / "000000.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in lines))
    monkeypatch.setattr(label, "LOG_DIR", str(tmp_path))


def source():
    with open(label.__file__, "rb") as fh:
        return fh.read()


def parse(out):
    return dict(line.split(": ", 1) for line in out.splitlines())


def assert_blind(out):
    """Tripwire: hashes and integer counts, nothing that could be a number
    anyone reads as a result."""
    assert out.strip()
    for line in out.splitlines():
        assert any(p.match(line) for p in LINE_OK), line
    assert "%" not in out and "." not in out


def test_same_module_is_equivalent(synth_log, capsys):
    src = source()
    assert label_equiv.run(src, src, label) == 0
    out = capsys.readouterr().out
    assert_blind(out)
    got = parse(out)
    assert int(got["rows_compared"]) > 0
    assert int(got["pending_skipped"]) > 0
    assert int(got["baseline_compared"]) == int(got["rows_compared"])
    for k in ("outcome_diffs_status", "outcome_diffs_returns",
              "baseline_diffs", "verdict"):
        assert got[k] == "0"


def test_altered_fallback_fee_is_caught(synth_log, capsys):
    src = source()
    assert src.count(b"\nFEE_FALLBACK = 0.01\n") == 1
    altered = src.replace(b"\nFEE_FALLBACK = 0.01\n", b"\nFEE_FALLBACK = 0.02\n")
    assert label_equiv.run(altered, src, label) == 1
    out = capsys.readouterr().out
    assert_blind(out)
    got = parse(out)
    assert int(got["outcome_diffs_returns"]) > 0
    assert got["verdict"] == "1"
    assert got["registered_blob"] != got["current_blob"]


def test_differing_log_helpers_stop_the_check(synth_log, capsys):
    """The data is built with the current module only, so the check refuses
    to run when the two versions would not have loaded the same rows."""
    src = source()
    assert src.count(b"\nMIN_SCORE_TXNS = 200") == 1
    altered = src.replace(b"\nMIN_SCORE_TXNS = 200", b"\nMIN_SCORE_TXNS = 100")
    assert label_equiv.run(altered, src, label) == 2
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "MIN_SCORE_TXNS" in cap.err
