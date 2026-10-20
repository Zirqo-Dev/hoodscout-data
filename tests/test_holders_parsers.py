"""holders.py response parsers, against fixtures shaped like the real API.

The shapes here are the ones the live runs settled, plus the variants the
parsers were written to tolerate because Blockscout spells the same field
differently across versions. parse_tx_sender's `from` key was the last
inference in the collector; the 2026-10-19 18:10 and 21:10 holders runs
confirmed it by resolving 70 and then 82 creator wallets. Pinned here so a
parser rewrite cannot quietly un-confirm it.
"""
import holders


# ------------------------------------------------------------ parse_holders

def test_parse_holders_reference_shape(fixture):
    rows = holders.parse_holders(fixture("blockscout_holders.json"))
    assert len(rows) == 3
    first = rows[0]
    assert first["address"] == "0xaaaa000000000000000000000000000000000001"
    assert first["balance"] == "500000000000000000000000"
    assert first["is_contract"] is False
    assert first["label"] is None


def test_parse_holders_lowercases_the_address(fixture):
    """The fixture sends holder 1 checksummed. Addresses are compared as
    strings downstream -- against burn lists and exclusion hints -- so a
    checksummed one that survives would silently miss every match."""
    payload = fixture("blockscout_holders.json")
    assert any(c.isupper() for c in payload["items"][0]["address_hash"]["hash"])
    rows = holders.parse_holders(payload)
    assert rows[0]["address"] == rows[0]["address"].lower()


def test_parse_holders_keeps_balance_as_a_string(fixture):
    """Base-unit balances run past 2**53. A float is not a container for one,
    and the registered metric divides raw integers."""
    rows = holders.parse_holders(fixture("blockscout_holders.json"))
    assert all(isinstance(r["balance"], str) for r in rows)
    assert int(rows[0]["balance"]) == 500000000000000000000000


def test_parse_holders_is_contract_does_not_catch_burn(fixture):
    """The finding that sent H-0004 to H-0005: a burn address reports
    is_contract false and holds a top-10 balance, so exclusion cannot be
    delegated to that flag."""
    rows = holders.parse_holders(fixture("blockscout_holders.json"))
    burn = next(r for r in rows if r["address"].endswith("dead"))
    assert burn["is_contract"] is False
    assert int(burn["balance"]) > 0


def test_parse_holders_carries_the_pool_label(fixture):
    rows = holders.parse_holders(fixture("blockscout_holders.json"))
    pool = next(r for r in rows if r["is_contract"])
    assert pool["label"] == "Pons V2 Pool"


def test_parse_holders_tolerates_the_other_spellings(fixture):
    """address vs address_hash, a bare string instead of an object, value vs
    balance vs amount, and a tag list in place of a name."""
    rows = holders.parse_holders(fixture("blockscout_holders_variants.json"))
    by_addr = {r["address"]: r for r in rows}
    assert "0xbbbb000000000000000000000000000000000002" in by_addr
    assert "0xcccc000000000000000000000000000000000003" in by_addr
    assert by_addr["0xbbbb000000000000000000000000000000000002"]["balance"] == "42"
    assert by_addr["0xcccc000000000000000000000000000000000003"]["balance"] == "7"
    tagged = by_addr["0xdddd000000000000000000000000000000000004"]
    assert tagged["label"] == "Burn"
    assert tagged["balance"] is None


def test_parse_holders_drops_rows_with_no_address(fixture):
    """An empty hash, a missing address and a non-object are dropped rather
    than stored as a holder with no identity."""
    rows = holders.parse_holders(fixture("blockscout_holders_variants.json"))
    assert len(rows) == 3
    assert all(r["address"] for r in rows)


def test_parse_holders_honours_the_limit(fixture):
    rows = holders.parse_holders(fixture("blockscout_holders.json"), limit=2)
    assert len(rows) == 2


def test_parse_holders_on_junk():
    for junk in (None, [], "", 0, {"items": "nope"}, {}):
        assert holders.parse_holders(junk) == []


# --------------------------------------------------------- parse_token_meta

def test_parse_token_meta(fixture):
    meta = holders.parse_token_meta(fixture("blockscout_token.json"))
    assert meta["total_supply"] == "1000000000000000000000000"
    assert meta["decimals"] == 18
    assert meta["symbol"] == "HOOD"


def test_parse_token_meta_coerces_string_decimals(fixture):
    """The live response sends decimals as a string."""
    payload = fixture("blockscout_token.json")
    assert isinstance(payload["decimals"], str)
    assert holders.parse_token_meta(payload)["decimals"] == 18


def test_parse_token_meta_supply_stays_a_string(fixture):
    meta = holders.parse_token_meta(fixture("blockscout_token.json"))
    assert isinstance(meta["total_supply"], str)


def test_parse_token_meta_bad_decimals_is_none():
    meta = holders.parse_token_meta({"total_supply": "1", "decimals": "many"})
    assert meta["decimals"] is None
    assert meta["total_supply"] == "1"


def test_parse_token_meta_on_junk():
    empty = {"total_supply": None, "decimals": None, "symbol": None}
    for junk in (None, [], "", 0, {}):
        assert holders.parse_token_meta(junk) == empty


# ----------------------------------------------------------- parse_deployer

def test_parse_deployer(fixture):
    d = holders.parse_deployer(fixture("blockscout_address.json"))
    assert d["deployer"] == "0xfac7000000000000000000000000000000000001"
    assert d["creation_tx"].startswith("0x7eec1e8d")


def test_parse_deployer_accepts_a_nested_creator():
    d = holders.parse_deployer({"creator_address_hash": {"hash": "0xABC"},
                                "creation_transaction_hash": "0xdef"})
    assert d["deployer"] == "0xabc"
    assert d["creation_tx"] == "0xdef"


def test_parse_deployer_on_junk():
    empty = {"deployer": None, "creation_tx": None}
    for junk in (None, [], "", 0, {}):
        assert holders.parse_deployer(junk) == empty


# ---------------------------------------------------------- parse_tx_sender

def test_parse_tx_sender(fixture):
    """The whole point of resolving the creation tx: creator_address_hash is
    usually a factory, and the sender is the wallet behind it."""
    sender = holders.parse_tx_sender(fixture("blockscout_tx.json"))
    assert sender == "0xe0a1000000000000000000000000000000000001"


def test_parse_tx_sender_differs_from_the_deployer(fixture):
    deployer = holders.parse_deployer(fixture("blockscout_address.json"))
    sender = holders.parse_tx_sender(fixture("blockscout_tx.json"))
    assert sender != deployer["deployer"]


def test_parse_tx_sender_accepts_a_bare_string():
    assert holders.parse_tx_sender({"from": "0xABC"}) == "0xabc"


def test_parse_tx_sender_alternate_spellings():
    for key in ("from_address_hash", "from_address", "sender"):
        assert holders.parse_tx_sender({key: "0xAbC"}) == "0xabc"


def test_parse_tx_sender_on_junk():
    for junk in (None, [], "", 0, {}, {"from": None}, {"from": {}},
                 {"from": {"hash": ""}}, {"to": {"hash": "0xabc"}}):
        assert holders.parse_tx_sender(junk) is None
