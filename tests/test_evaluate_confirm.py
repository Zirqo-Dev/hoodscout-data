"""scripts/evaluate_confirm.py runs evaluate.py's confirmed rule unchanged.

The decision tests use the seeded synthetic labels in tests/synth_labels.py and
never read data/. The registry tests at the bottom are the one approved
exception to conftest.py's rule: they read data/hypotheses.jsonl, whose lines
are append-only, to pin H-0001..H-0009 byte for byte, to check that H-0009
copies H-0003's frozen fields, and that H-0010 changes only the listed fields
of H-0008, the line it supersedes.
"""
import argparse
import contextlib
import hashlib
import io
import json
import re
from datetime import datetime, timezone

import pytest

import evaluate as E
import evaluate_confirm as C
import label
import synth_labels as S
from conftest import ROOT

SCEN = S.scenarios()
STATUSES = ("pass", "fail", "inconclusive", "waiting", "expired")
H3_SCEN = ["h3_pass", "h3_fail", "h3_inconclusive_censoring"]
READ_ON = "2031-03-03"


def confirming(h3, **over):
    """A synthetic confirmation line for a synthetic H-0003 line."""
    c = dict(h3)
    c.update({"id": "H-0009", "confirms": "H-0003", "read_on": READ_ON,
              "status": "waiting"})
    c.update(over)
    return c


def _strip(r):
    r = S.roundtrip(r)
    r.pop("id")
    r.pop("confirms", None)
    return r


# ---------------------------------------------------------------- decision

@pytest.mark.parametrize("name", H3_SCEN)
def test_decision_equals_evaluate_for_h0003(name):
    h3, labels = SCEN[name]
    c = confirming(h3)
    r = C.evaluate_confirm(c, [h3, c], labels)
    assert r["id"] == "H-0009" and r["confirms"] == "H-0003"
    ref = E.evaluate(h3, labels, h3["log_watermark"])
    got = _strip(r)
    for k, v in _strip(ref).items():
        assert got[k] == v, k
    full = E.evaluate_full(h3, labels, h3["log_watermark"], None)
    assert got == _strip(full)


@pytest.mark.parametrize("name", H3_SCEN)
def test_uses_its_own_watermark(name):
    """A later watermark on the confirmation line drops earlier rows; the
    result equals evaluate() at THAT watermark, not at H-0003's."""
    h3, labels = SCEN[name]
    ts = sorted(o["ts"] for o in labels)
    mid = ts[len(ts) // 2]
    c = confirming(h3, log_watermark=mid)
    got = _strip(C.evaluate_confirm(c, [h3, c], labels))
    ref = _strip(E.evaluate_full(h3, labels, mid, None))
    assert got == ref
    assert got["rows"] < E.evaluate(h3, labels, h3["log_watermark"])["rows"]


def test_own_stop_by_is_used():
    h3, labels = SCEN["h3_pass"]
    few = labels[:20]                       # gates not met
    c = confirming(h3, stop_by="2000-01-01")
    assert C.evaluate_confirm(c, [h3, c], few)["status"] == "expired"
    assert E.evaluate(h3, few, h3["log_watermark"])["status"] == "waiting"


@pytest.mark.parametrize("field", C.FROZEN)
def test_a_changed_frozen_field_is_refused(field):
    h3, labels = SCEN["h3_pass"]
    c = confirming(h3)
    c[field] = "changed" if isinstance(c.get(field), str) else 1
    with pytest.raises(ValueError, match=field):
        C.as_confirmed(c, [h3, c])


def test_unknown_target_is_refused():
    h3, _ = SCEN["h3_pass"]
    c = confirming(h3, confirms="H-0042")
    with pytest.raises(ValueError, match="not registered"):
        C.as_confirmed(c, [h3, c])


# ------------------------------------------------------ main, blind, clock

def _run(monkeypatch, tmp_path, name="h3_pass", now=None, **args):
    h3, labels = SCEN[name]
    c = confirming(h3)
    reg = tmp_path / "hypotheses.jsonl"
    reg.write_text("".join(json.dumps(h) + "\n" for h in (h3, c)))
    monkeypatch.setattr(E, "REGISTRY", str(reg))
    ts = labels[0]["ts"]
    calls = []

    def load():
        calls.append(1)
        return ([{"ts": ts, "dt": datetime.fromisoformat(ts)}], {}, {}, {}, 1)
    monkeypatch.setattr(label, "load_log", load)
    monkeypatch.setattr(E, "prepare_labels", lambda *a: (labels, {ts}))
    a = argparse.Namespace(id=None, report=None, blind=False, unblind=False)
    vars(a).update(args)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = C.main(a, now=(lambda: now) if now else None)
    return rc, out.getvalue(), err.getvalue(), calls


AT_READ = datetime(2031, 3, 3, 7, 0, 0, tzinfo=timezone.utc)
BEFORE = datetime(2031, 3, 3, 6, 59, 59, tzinfo=timezone.utc)


@pytest.mark.parametrize("name", H3_SCEN)
def test_blind_output_has_no_metric_status_or_figure(monkeypatch, tmp_path,
                                                     name):
    for flag in ({}, {"blind": True}):
        rc, out, _, _ = _run(monkeypatch, tmp_path, name, **flag)
        assert rc == 0
        body = "\n".join(l for l in out.splitlines() if l.startswith("  "))
        assert "H-0009  rows" in body and "eff_n " in body
        assert "tokens " in body and "all-tier" in body
        for word in STATUSES + ("metric", "pp", "%", "[", "superseded"):
            assert word not in body, (word, body)
        # the header names the wrapper, which contains "pp"; no figure may
        # carry a unit anywhere in stdout
        assert "%" not in out
        assert not re.search(r"\d\s*pp", out), out


def test_blind_needs_no_clock(monkeypatch, tmp_path):
    rc, _, _, _ = _run(monkeypatch, tmp_path, now=BEFORE)
    assert rc == 0


@pytest.mark.parametrize("args", [{"unblind": True},
                                  {"unblind": True, "report": ""}])
def test_unblind_before_read_on_is_refused(monkeypatch, tmp_path, args):
    rc, out, err, calls = _run(monkeypatch, tmp_path, now=BEFORE, **args)
    assert rc == C.EXIT_BEFORE_READ
    assert "refused" in err and "2031-03-03T07:00:00+00:00" in err
    assert calls == [] and out == ""        # nothing loaded, nothing printed


def test_unblind_at_read_on_is_allowed(monkeypatch, tmp_path):
    rc, out, _, calls = _run(monkeypatch, tmp_path, now=AT_READ,
                             unblind=True)
    assert rc == 0 and calls == [1]
    assert re.search(r"H-0009\s+pass\s", out)


def test_report_without_unblind_is_refused(monkeypatch, tmp_path):
    rc, out, err, calls = _run(monkeypatch, tmp_path, now=AT_READ, report="")
    assert rc == C.EXIT_REPORT_NEEDS_UNBLIND
    assert "--unblind" in err and calls == []


def test_report_after_read_on_writes(monkeypatch, tmp_path):
    path = tmp_path / "rep.md"
    rc, out, _, _ = _run(monkeypatch, tmp_path, now=AT_READ, unblind=True,
                         report=str(path))
    assert rc == 0
    text = path.read_text()
    assert "## H-0009 — `pass`" in text and "H-0009 confirms H-0003" in text


def test_read_at():
    assert C.read_at({"read_on": "2026-12-07"}) == datetime(
        2026, 12, 7, 7, 0, tzinfo=timezone.utc)


def test_cli_blind_and_unblind_are_exclusive():
    import subprocess, sys
    p = subprocess.run([sys.executable, str(ROOT / "scripts" /
                                            "evaluate_confirm.py"),
                        "--blind", "--unblind"], cwd=ROOT,
                       capture_output=True, text=True)
    assert p.returncode != 0 and "not allowed" in p.stderr


# ---------------------------------------------------------------- registry

REG = ROOT / "data" / "hypotheses.jsonl"
# sha256 of data/hypotheses.jsonl as it stood before H-0009 was appended:
# lines 1-8 and their newlines, at main e70b0110
LINES_1_8_SHA256 = ("f4427fbf08f497562e8be88c6a2bc23f260a54c6e41d969374da91a6"
                    "f77fe1ff")
COPIED = ("uses", "population", "horizon", "metric", "baseline", "pass_if",
          "fail_if", "inconclusive_if", "min_eff_n", "min_n_tokens",
          "max_censored_pct")


def _lines():
    return REG.read_bytes().splitlines(keepends=True)


def test_lines_1_to_8_are_byte_identical():
    assert hashlib.sha256(b"".join(_lines()[:8])).hexdigest() == \
        LINES_1_8_SHA256


def test_h0009_line():
    raw = _lines()[8]
    h = json.loads(raw)
    h3 = json.loads(_lines()[2])
    assert h["id"] == "H-0009" and h3["id"] == "H-0003"
    assert raw == (json.dumps(h, separators=(",", ":")) + "\n").encode()
    order = list(h3)
    i = order.index("stop_by") + 1
    assert list(h) == order[:i] + ["confirms", "read_on"] + order[i:]
    for k in COPIED:
        assert h[k] == h3[k], k
    assert tuple(C.FROZEN) == COPIED
    assert h["confirms"] == "H-0003" and h["read_on"] == "2026-12-07"
    assert h["stop_by"] == "2027-02-15" and h["status"] == "waiting"
    assert h["log_watermark"] < h["registered"]
    assert re.fullmatch(r"[0-9a-f]{40}", h["labeler_version"])
    C.as_confirmed(h, [json.loads(l) for l in _lines()])   # does not raise


# sha256 of lines 1-9 and their newlines, as they stood before H-0010 was
# appended (main 1025669d)
LINES_1_9_SHA256 = ("c2e2469f04683520f57ba0ef9cd320f9ef9832b90154cd8c4b1e431c"
                    "19b77bb4")
H10_CHANGED = ("registered", "log_watermark", "labeler_version", "id",
               "supersedes", "statement", "population", "notes")
H10_PARA = (
    "SUPERSEDES H-0008 before anything was evaluated (no evaluator exists, no "
    "outcome seen). Clause (b) was unreachable with a 30-min entry window: "
    "95/142 = 66.9% on 2026-11-10 (counts only, by an external review and by "
    "Claude Code, independently; the pre-registration file had shown 77/153). "
    "Enrolment runs every 3 h, so the window is set to 180 min to match that "
    "cycle, not fitted to the 120-min minimum. The population start stays at "
    "H-0008's watermark because nobody has seen an H-0008 outcome; this is "
    "disclosed as a predictor-side change after registration. Trade-off: the "
    "entry row can be up to 3 h older than enrolment. Coverage counts at "
    "30/60/90/120/180 min (66.9/70.4/75.4/82.4/100%) were seen before this "
    "line; no death rate, gap or watch outcome was.")


def _h8_name(h8):
    """The name line 8 uses, read from line 8 itself so this file never
    carries it."""
    return re.search(r"at (\w+)'s holding horizon", h8["statement"]).group(1)


def test_lines_1_to_9_are_byte_identical():
    assert hashlib.sha256(b"".join(_lines()[:9])).hexdigest() == \
        LINES_1_9_SHA256


def test_h0010_line():
    raw = _lines()[9]
    h = json.loads(raw)
    h8 = json.loads(_lines()[7])
    assert h["id"] == "H-0010" and h8["id"] == "H-0008"
    assert raw == (json.dumps(h, separators=(",", ":")) + "\n").encode()
    order = list(h8)
    i = order.index("id") + 1
    assert list(h) == order[:i] + ["supersedes"] + order[i:]
    h7 = json.loads(_lines()[6])
    assert list(h7).index("supersedes") == list(h7).index("id") + 1
    for k in h8:
        if k not in H10_CHANGED:
            assert h[k] == h8[k], k
    assert h["supersedes"] == "H-0008"
    assert h["log_watermark"] < h["registered"]
    assert h["registered"] > h8["registered"]
    assert h["labeler_version"] == "3ba1b3524df9d1412ad530e5c35768a9ee92c0ef"
    name = _h8_name(h8)
    assert h["statement"] == h8["statement"].replace(
        f"at {name}'s holding horizon of days",
        "at the owner's holding horizon of days")
    assert h["population"] == h8["population"].replace(
        "with enrolled_at > log_watermark and",
        "with enrolled_at > 2026-10-28T17:30:26.797920+00:00 (H-0008's "
        "watermark, kept on purpose; see notes) and").replace(
        "enrolled_at - 30 min <= ts <= enrolled_at",
        "enrolled_at - 180 min <= ts <= enrolled_at")
    assert h8["log_watermark"] in h["population"]
    assert h["notes"] == H10_PARA + " " + h8["notes"].replace(name,
                                                              "the owner")


def test_h0010_has_no_owner_name():
    name = _h8_name(json.loads(_lines()[7]))
    assert name.lower() not in _lines()[9].decode().lower()


def test_h0010_supersedes_h0008_in_the_registry():
    hyps = [json.loads(l) for l in _lines()]
    assert E.superseded_map(hyps).get("H-0008") == "H-0010"
