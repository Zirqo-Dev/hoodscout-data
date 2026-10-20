"""Shared paths and loaders for the test suite.

Nothing here touches the network, and nothing reads the live pipeline's own
data: every input is a small JSON file under tests/fixtures/. The two
hypothesis fixtures are synthetic and say so in their own statement field --
`data/hypotheses.jsonl` is append-only and a test must never depend on what
it happens to contain today.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

# The pipeline scripts are not a package and are run as files, so the tests
# import them the same way they are invoked.
sys.path.insert(0, str(ROOT / "scripts"))


def load_fixture(name):
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def fixture():
    return load_fixture


def index_rows(rows):
    """Mirror the (by_ca, times) indexes label.load_log() builds.

    Rebuilt here rather than imported so a test can hand outcome() three
    rows instead of a log directory. Kept in step with load_log() by shape:
    rows sorted per ca, and a parallel list of datetimes for the bisect.
    """
    by_ca, times = {}, {}
    for r in rows:
        by_ca.setdefault(r["ca"], []).append(r)
    for ca, v in by_ca.items():
        v.sort(key=lambda r: r["dt"])
        times[ca] = [r["dt"] for r in v]
    return by_ca, times


@pytest.fixture
def label_log():
    """The label_rows.json fixture, as (rows, by_ca, times, t0).

    offset_h is resolved against t0 so the fixture reads as "six hours after
    entry" rather than as a wall-clock timestamp nobody can check by eye.
    """
    raw = load_fixture("label_rows.json")
    t0 = datetime.fromisoformat(raw["t0"])
    rows = []
    for r in raw["rows"]:
        r = dict(r)
        dt = t0 + timedelta(hours=r.pop("offset_h"))
        r["dt"] = dt
        r["ts"] = dt.isoformat()
        rows.append(r)
    by_ca, times = index_rows(rows)
    return rows, by_ca, times, t0
