"""HS-130: NFT snapshot slots, the slot guard, and the two field fixes.

Synthetic only. Every test runs with the working directory moved to
tmp_path, so nft.py's relative data/ paths land there; get/post/mint_key,
time.sleep and utcnow are patched, so nothing reaches the network and
nothing reads the clock. The real data/ is never opened: the autouse
guard below fails the test if any of its NFT files changed.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from conftest import ROOT

import nft

UTC = timezone.utc
H13, H21 = "17 13 * * *", "17 21 * * *"

# Synthetic collections. "offchain" answers but is not on the robinhood
# chain, so measure() returns no row without raising.
SYNTH_WATCHLIST = {"alpha": "Alpha", "beta": "Beta", "offchain": "Offchain"}

# Keys the files carried before HS-130; new keys may be added, none removed.
OLD_LATEST_ROW = {"slug", "name", "chains", "image_url", "total_supply",
                  "floor_price", "floor_symbol", "volume_total", "volume_24h",
                  "sales_24h", "num_owners", "listed_pct", "ts",
                  "floor_chg_pct", "flag_thin_floor_move"}
OLD_HISTORY_ROW = {"ts", "slug", "floor_price", "volume_24h", "sales_24h",
                   "num_owners"}
OLD_LATEST_TOP = {"generated_at", "chain", "collections"}
OLD_DISCOVER_TOP = {"generated_at", "chain", "order_by", "social_fields",
                    "collections"}
OLD_DISCOVER_ROW = {"slug", "name", "image_url", "created_date", "age_days",
                    "total_supply", "num_owners", "owner_supply_ratio",
                    "floor_price", "floor_symbol", "volume_total", "volume_24h",
                    "sales_24h", "safelist_status", "opensea_url",
                    "social_presence_score", "social_fields_checked",
                    "social_fields_filled", "flag_low_owner_ratio", "flag_new",
                    "flag_dressed_but_thin", "floor_chg_pct",
                    "flag_thin_floor_move"}
SLOT_KEYS = {"slot", "slot_day"}
LEDGER_KEYS = {"slot", "slot_day", "trigger", "schedule", "started_at",
               "observed_at", "status", "tracked_rows", "discover"}


def t(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


class FakeAPI:
    """Stands in for OpenSea. Floors, listed counts and failures are set per
    test; discovery lists `discovered` in order."""

    def __init__(self):
        self.floors = {"alpha": 1.0, "beta": 2.0, "offchain": 3.0, "newco": 0.5}
        self.listed = {"alpha": 10, "beta": 0}
        self.supply = {"alpha": 100, "beta": 200, "offchain": 50, "newco": 1000}
        self.fail = set()          # slugs whose calls raise
        self.fail_listing = False  # discovery's collection list raises
        self.discovered = ["alpha", "newco"]
        self.calls = []

    def get(self, path, key):
        self.calls.append(path)
        if path.startswith("/collections?"):
            if self.fail_listing:
                raise RuntimeError("synthetic listing failure")
            return {"collections": [{"collection": s} for s in self.discovered]}
        slug = path.split("/")[2].split("?")[0]
        if slug in self.fail:
            raise RuntimeError(f"synthetic failure for {slug}")
        if path.endswith("/stats"):
            total = {"floor_price": self.floors[slug], "floor_price_symbol": "ETH",
                     "volume": 100.0, "num_owners": 40}
            if slug in self.listed:
                total["num_listed"] = self.listed[slug]
            return {"total": total,
                    "intervals": [{"interval": "one_day", "volume": 1.5, "sales": 2}]}
        chain = "ethereum" if slug == "offchain" else "robinhood"
        return {"name": slug.title(), "contracts": [{"chain": chain}],
                "total_supply": self.supply[slug], "created_date": "2026-01-01",
                "image_url": "", "opensea_url": f"https://example.invalid/{slug}"}


@pytest.fixture(autouse=True)
def real_data_untouched():
    names = ["nft.jsonl", "nft_latest.json", "nft_discover.json", "nft_slots.jsonl"]
    paths = [ROOT / "data" / n for n in names]
    before = [(p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
              for p in paths]
    yield
    after = [(p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
             for p in paths]
    assert before == after, "a test touched the real data/"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = FakeAPI()
    clock = {"now": t("2026-11-20T13:20:00")}
    mints = []

    def fake_mint():
        mints.append(1)
        return "synthetic-key"

    def no_post(*a, **k):
        raise AssertionError("post() must not be called")

    monkeypatch.setattr(nft, "get", api.get)
    monkeypatch.setattr(nft, "post", no_post)
    monkeypatch.setattr(nft, "mint_key", fake_mint)
    monkeypatch.setattr(nft.time, "sleep", lambda s: None)
    monkeypatch.setattr(nft, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(nft, "WATCHLIST", dict(SYNTH_WATCHLIST))
    for k in ("NFT_TRIGGER", "NFT_SCHEDULE", "NFT_SLOT", "OPENSEA_API_KEY",
              "DISCOVER_ORDER"):
        monkeypatch.delenv(k, raising=False)

    class Env:
        pass

    e = Env()
    e.api, e.clock, e.mints, e.root = api, clock, mints, tmp_path

    def run(now, trigger="schedule", schedule="", slot=""):
        clock["now"] = t(now)
        monkeypatch.setenv("NFT_TRIGGER", trigger)
        monkeypatch.setenv("NFT_SCHEDULE", schedule)
        monkeypatch.setenv("NFT_SLOT", slot)
        return nft.run_slotted()

    e.run = run
    e.ledger = lambda: read_jsonl(tmp_path / "data" / "nft_slots.jsonl")
    e.hist = lambda: read_jsonl(tmp_path / "data" / "nft.jsonl")
    e.latest = lambda: json.load(open(tmp_path / "data" / "nft_latest.json"))
    e.discover = lambda: json.load(open(tmp_path / "data" / "nft_discover.json"))
    return e


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def seed_ledger(e, *slots):
    os.makedirs(e.root / "data", exist_ok=True)
    with open(e.root / "data" / "nft_slots.jsonl", "w") as f:
        for s in slots:
            f.write(json.dumps({"slot": s, "slot_day": s[:10], "status": "ok"}) + "\n")


# --- resolve_slot ------------------------------------------------------------

@pytest.mark.parametrize("now,schedule,want", [
    ("2026-11-20T13:20:00", H13, "2026-11-20T13:17Z"),
    ("2026-11-20T21:04:00", H13, "2026-11-20T13:17Z"),   # late backup
    ("2026-11-20T13:16:00", H13, "2026-11-19T13:17Z"),
    ("2026-11-20T21:30:00", H21, "2026-11-20T21:17Z"),
    ("2026-11-21T01:00:00", H21, "2026-11-20T21:17Z"),   # UTC rollover
    ("2026-11-21T13:00:00", H21, "2026-11-20T21:17Z"),
    ("2026-11-20T13:17:00", "", "2026-11-20T13:17Z"),    # dispatch: latest of two
    ("2026-11-20T13:16:59", "", "2026-11-19T21:17Z"),
    ("2026-11-20T22:00:00", "", "2026-11-20T21:17Z"),
    ("2026-11-20T22:00:00", "17 13,21 * * *", "2026-11-20T21:17Z"),  # unknown string
])
def test_resolve_slot_from_schedule(now, schedule, want):
    assert nft.slot_str(nft.resolve_slot(t(now), schedule, "")) == want


def test_resolve_slot_explicit_wins():
    got = nft.resolve_slot(t("2026-11-20T22:00:00"), H21, "2026-11-19T13:17Z")
    assert nft.slot_str(got) == "2026-11-19T13:17Z"


@pytest.mark.parametrize("bad", [
    "2026-11-20T14:17Z", "2026-11-20T13:18Z", "2026-11-20 13:17Z",
    "2026-11-20T13:17", "2026-11-20T13:17:00Z", "garbage", " x ",
    "2026-02-30T13:17Z",            # passes the regex, impossible date
    "2026-13-01T13:17Z",
    "2026-11-20T21:17Z",            # in the future at 13:20Z
])
def test_resolve_slot_rejects(bad):
    with pytest.raises(nft.SlotError):
        nft.resolve_slot(t("2026-11-20T13:20:00"), "", bad)


# --- runs ---------------------------------------------------------------------

def test_first_run_ok_and_fields(env):
    assert env.run("2026-11-20T13:20:00", schedule=H13) == 0
    (line,) = env.ledger()
    assert set(line) == LEDGER_KEYS
    assert line["slot"] == "2026-11-20T13:17Z" and line["slot_day"] == "2026-11-20"
    assert line["status"] == "ok"            # offchain is a gap, not a failure
    assert line["tracked_rows"] == 2 and line["discover"] == "ok"
    assert line["trigger"] == "schedule" and line["schedule"] == H13
    assert line["started_at"].startswith("2026-11-20T13:20:00")


def test_duplicate_dispatch_is_skipped(env):
    assert env.run("2026-11-20T13:25:00", trigger="workflow_dispatch") == 0
    files = {n: (env.root / "data" / n).read_bytes()
             for n in ("nft.jsonl", "nft_latest.json", "nft_discover.json")}
    mints = len(env.mints)
    assert env.run("2026-11-20T13:40:00", trigger="workflow_dispatch") == 0
    last = env.ledger()[-1]
    assert last["status"] == "skipped_duplicate"
    assert last["slot"] == "2026-11-20T13:17Z"
    assert len(env.mints) == mints                  # no key minted for a duplicate
    for n, b in files.items():
        assert (env.root / "data" / n).read_bytes() == b


def test_partial_counts_duplicate_too(env):
    env.api.fail = {"beta"}
    assert env.run("2026-11-20T13:20:00", schedule=H13) == 0
    assert env.ledger()[-1]["status"] == "partial"
    env.api.fail = set()
    assert env.run("2026-11-20T14:00:00", schedule=H13) == 0
    assert env.ledger()[-1]["status"] == "skipped_duplicate"


def test_late_backup_keeps_its_slot(env):
    assert env.run("2026-11-20T21:04:00", schedule=H13) == 0
    line = env.ledger()[-1]
    assert line["slot"] == "2026-11-20T13:17Z" and line["slot_day"] == "2026-11-20"
    for r in env.hist():
        assert r["ts"].startswith("2026-11-20T21:04")
        assert r["slot"] == "2026-11-20T13:17Z" and r["slot_day"] == "2026-11-20"
    assert env.latest()["slot"] == "2026-11-20T13:17Z"
    assert env.discover()["slot"] == "2026-11-20T13:17Z"


def test_utc_rollover_belongs_to_previous_day(env):
    assert env.run("2026-11-21T01:00:00", schedule=H21) == 0
    line = env.ledger()[-1]
    assert line["slot"] == "2026-11-20T21:17Z" and line["slot_day"] == "2026-11-20"
    for r in env.hist():
        assert r["ts"].startswith("2026-11-21T01:00")
        assert r["slot_day"] == "2026-11-20"
    assert env.latest()["slot_day"] == "2026-11-20"
    assert env.latest()["generated_at"].startswith("2026-11-21")


def test_explicit_slot_valid(env):
    assert env.run("2026-11-20T22:00:00", trigger="workflow_dispatch",
                   slot="2026-11-20T13:17Z") == 0
    line = env.ledger()[-1]
    assert line["slot"] == "2026-11-20T13:17Z" and line["trigger"] == "workflow_dispatch"
    assert env.hist()[0]["ts"].startswith("2026-11-20T22:00")


@pytest.mark.parametrize("bad", ["2026-11-20T14:17Z", "2026-02-30T13:17Z",
                                 "2026-11-21T13:17Z", "nonsense"])
def test_explicit_slot_invalid_writes_nothing(env, bad):
    assert env.run("2026-11-20T22:00:00", trigger="workflow_dispatch", slot=bad) == 2
    assert not (env.root / "data").exists()
    assert env.mints == [] and env.api.calls == []


def test_missed_slots_logged(env):
    seed_ledger(env, "2026-11-19T13:17Z")
    assert env.run("2026-11-20T21:20:00", schedule=H21) == 0
    got = [(l["slot"], l["status"]) for l in env.ledger()[1:]]
    assert got == [("2026-11-19T21:17Z", "missed"),
                   ("2026-11-20T13:17Z", "missed"),
                   ("2026-11-20T21:17Z", "ok")]
    missed = env.ledger()[1]
    assert set(missed) == LEDGER_KEYS
    assert missed["trigger"] is None and missed["started_at"] is None
    assert missed["tracked_rows"] is None and missed["discover"] is None
    assert missed["observed_at"].startswith("2026-11-20T21:20")


def test_missed_window_is_48h(env):
    seed_ledger(env, "2026-11-10T13:17Z")
    assert env.run("2026-11-20T21:20:00", schedule=H21) == 0
    missed = [l["slot"] for l in env.ledger() if l["status"] == "missed"]
    assert missed == ["2026-11-19T13:17Z", "2026-11-19T21:17Z", "2026-11-20T13:17Z"]


def test_no_missed_before_first_ledger_line(env):
    assert env.run("2026-11-20T21:20:00", schedule=H21) == 0    # empty ledger
    assert [l["status"] for l in env.ledger()] == ["ok"]
    seed_ledger(env, "2026-11-20T13:17Z")
    assert env.run("2026-11-21T13:20:00", schedule=H13) == 0
    assert [(l["slot"], l["status"]) for l in env.ledger()] == [
        ("2026-11-20T13:17Z", "ok"),
        ("2026-11-20T21:17Z", "missed"),
        ("2026-11-21T13:17Z", "ok")]


def test_missed_then_late_run_appends_ok(env):
    seed_ledger(env, "2026-11-20T13:17Z")
    assert env.run("2026-11-21T13:20:00", schedule=H13) == 0     # marks 11-20 21:17 missed
    assert env.run("2026-11-21T13:40:00", trigger="workflow_dispatch",
                   slot="2026-11-20T21:17Z") == 0
    rows = [(l["slot"], l["status"]) for l in env.ledger()
            if l["slot"] == "2026-11-20T21:17Z"]
    assert rows == [("2026-11-20T21:17Z", "missed"), ("2026-11-20T21:17Z", "ok")]
    # a slot that already has a line is never marked missed again
    assert env.run("2026-11-21T21:20:00", schedule=H21) == 0
    assert sum(1 for l in env.ledger() if l["status"] == "missed") == 1


# --- floor comparison --------------------------------------------------------

def test_missing_history_keeps_floor_chg_null(env):
    assert env.run("2026-11-20T13:20:00", schedule=H13) == 0
    assert all(r["floor_chg_pct"] is None for r in env.latest()["collections"])
    # alpha is tracked AND discovered: with no prior row it must stay null,
    # not 0.0 from comparing with the row this run just wrote
    assert all(r["floor_chg_pct"] is None for r in env.discover()["collections"])


def test_discovery_compares_with_previous_run(env):
    assert env.run("2026-11-20T13:20:00", schedule=H13) == 0
    env.api.floors["alpha"] = 1.1
    assert env.run("2026-11-20T21:20:00", schedule=H21) == 0
    disc = {r["slug"]: r for r in env.discover()["collections"]}
    assert disc["alpha"]["floor_chg_pct"] == 10.0     # same-run compare gives 0.0
    assert disc["newco"]["floor_chg_pct"] is None
    tracked = {r["slug"]: r for r in env.latest()["collections"]}
    assert tracked["alpha"]["floor_chg_pct"] == 10.0


# --- listed_pct ----------------------------------------------------------------

@pytest.mark.parametrize("listed,supply,want", [
    (0, 100, 0.0), (25, 100, 25.0), (1, 3, 33.33), (None, 100, None),
    ("absent", 100, None), (10, 0, None), (10, None, None), (True, 100, None),
    ("5", 100, None), (10, -5, None),
])
def test_listed_pct(env, listed, supply, want):
    env.api.supply["alpha"] = supply
    if listed == "absent":
        env.api.listed.pop("alpha", None)
    else:
        env.api.listed["alpha"] = listed
    got = nft.measure("alpha", "synthetic-key")["listed_pct"]
    assert got == want and (want is None or isinstance(got, float))


# --- failures ------------------------------------------------------------------

def test_failed_collection(env):
    os.makedirs(env.root / "data")
    seeded = '{"ts": "2026-11-19T21:30:00+00:00", "slug": "alpha", "floor_price": 1.0}\n'
    (env.root / "data" / "nft.jsonl").write_text(seeded)
    env.api.fail = {"alpha", "beta", "offchain"}
    assert env.run("2026-11-20T13:20:00", schedule=H13) != 0
    line = env.ledger()[-1]
    assert line["status"] == "failed" and line["tracked_rows"] == 0
    assert (env.root / "data" / "nft.jsonl").read_text() == seeded
    assert not (env.root / "data" / "nft_latest.json").exists()
    # a failed slot is not a duplicate: the next run for it collects
    env.api.fail = set()
    assert env.run("2026-11-20T14:00:00", schedule=H13) == 0
    assert env.ledger()[-1]["status"] == "ok"


def test_failed_key_mint_is_recorded(env, monkeypatch):
    def boom():
        raise RuntimeError("synthetic 429")
    monkeypatch.setattr(nft, "mint_key", boom)
    assert env.run("2026-11-20T13:20:00", schedule=H13) == 1
    line = env.ledger()[-1]
    assert line["status"] == "failed" and line["tracked_rows"] == 0
    assert not (env.root / "data" / "nft.jsonl").exists()


def test_discovery_failure_keeps_exit_code(env):
    env.api.fail_listing = True
    assert env.run("2026-11-20T13:20:00", schedule=H13) == 1
    line = env.ledger()[-1]
    assert line["status"] == "ok" and line["discover"] == "failed"
    assert len(env.hist()) == 2
    assert not (env.root / "data" / "nft_discover.json").exists()


# --- layout --------------------------------------------------------------------

def test_existing_keys_unchanged(env):
    assert env.run("2026-11-20T13:20:00", schedule=H13) == 0
    latest, disc = env.latest(), env.discover()
    assert set(latest) == OLD_LATEST_TOP | SLOT_KEYS
    assert set(disc) == OLD_DISCOVER_TOP | SLOT_KEYS
    for r in latest["collections"]:
        assert set(r) == OLD_LATEST_ROW
    for r in disc["collections"]:
        assert set(r) == OLD_DISCOVER_ROW
    for r in env.hist():
        assert set(r) == OLD_HISTORY_ROW | SLOT_KEYS


def test_history_lines_never_rewritten(env):
    os.makedirs(env.root / "data")
    old = ('{"ts": "2026-11-19T21:30:00+00:00", "slug": "alpha", "floor_price": 1.0, '
           '"volume_24h": 0.0, "sales_24h": 0, "num_owners": 1}')
    (env.root / "data" / "nft.jsonl").write_text(old + "\n")
    assert env.run("2026-11-20T13:20:00", schedule=H13) == 0
    lines = (env.root / "data" / "nft.jsonl").read_text().splitlines()
    assert lines[0] == old and "slot" not in json.loads(lines[0])
    assert len(lines) == 3


def test_import_is_side_effect_free(tmp_path):
    out = subprocess.run(
        [sys.executable, "-B", "-c", "import nft; print(len(nft.WATCHLIST))"],
        cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "scripts")})
    assert out.returncode == 0, out.stderr
    assert list(tmp_path.iterdir()) == []


# --- nft.yml -------------------------------------------------------------------

def test_workflow_static():
    text = (ROOT / ".github" / "workflows" / "nft.yml").read_text()
    on_block = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    triggers = re.findall(r"^  (\w+):", on_block, re.M)
    assert triggers == ["schedule", "workflow_dispatch"]
    crons = re.findall(r"^\s*-\s*cron:\s*\"([^\"]+)\"", text, re.M)
    assert crons == [H13, H21]
    assert re.search(r"^      slot:\n(?:        .*\n)*?        default: \"\"\n",
                     on_block, re.M)
    assert re.search(r"^concurrency:\n  group: nft\n  cancel-in-progress: false\n",
                     text, re.M)
    assert re.search(r"^\s+ref: main\s*$", text, re.M)
    assert re.findall(r"secrets\.(\w+)", text) == ["OPENSEA_API_KEY"]
    for k, v in [("NFT_TRIGGER", "github.event_name"),
                 ("NFT_SCHEDULE", "github.event.schedule"),
                 ("NFT_SLOT", "inputs.slot")]:
        assert re.search(rf"^\s+{k}: \$\{{\{{ {re.escape(v)} \}}\}}\s*$", text, re.M)
    # inputs reach run: blocks through env only
    for block in re.findall(r"run: \|\n((?:          .*\n|\s*\n)+)", text):
        assert "${{" not in block
    for mode in ("--probe", "--discover", "--verify", "--with-discover"):
        assert f"python scripts/nft.py {mode}" in text
    steps = re.findall(r"^      - (?:name: (.+)|uses: )", text, re.M)
    assert steps[-1] == "Commit"
    commit = text.split("- name: Commit", 1)[1]
    assert re.search(r"^\s+if: always\(\)\s*$", commit, re.M)
    for s in ("git add data", "for i in 1 2 3 4 5; do", "git push && exit 0",
              "git pull --rebase --autostash", "exit 1"):
        assert s in commit
