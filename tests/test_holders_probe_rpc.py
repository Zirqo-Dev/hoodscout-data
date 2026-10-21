"""holders.py --probe-rpc: the HS-73 eth_call diagnostic.

No network: the module's urlopen is replaced. These pin the parsing of an ok
result, a revert and an error, and that probe mode writes nothing and never
reaches the collector.
"""
import io
import json
import sys
import urllib.error

import pytest

import holders

WORD = "0x" + "0" * 24 + "d0601ce157db5bdc3162bbac2a2c8af5320d9eec"


class Resp:
    def __init__(self, payload, status=200, headers=None):
        self._b = json.dumps(payload).encode()
        self.status = status
        self.headers = headers or {"x-credits-remaining": "99980"}

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_parse_ok():
    assert holders.parse_eth_call({"jsonrpc": "2.0", "id": 1,
                                   "result": WORD}) == (
        "ok", "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec")


@pytest.mark.parametrize("payload", [
    {"jsonrpc": "2.0", "id": 1,
     "error": {"code": 3, "message": "execution reverted"}},
    {"jsonrpc": "2.0", "id": 1, "result": "0x"},
    {"status": "0", "message": "NOTOK", "result": "execution reverted"},
])
def test_parse_revert(payload):
    assert holders.parse_eth_call(payload)[0] == "revert"


@pytest.mark.parametrize("payload", [
    None, "not json", {},
    {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601,
                                          "message": "method not found"}},
    {"jsonrpc": "2.0", "id": 1, "result": "0x1234"},
    {"status": "0", "message": "NOTOK", "result": "Invalid API key"},
])
def test_parse_error(payload):
    assert holders.parse_eth_call(payload)[0] == "error"


def test_http_error_is_reported_not_raised(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "x", {}, io.BytesIO(b""))
    monkeypatch.setattr(holders, "_urlopen", boom)
    status, payload, _ = holders.rpc_request(
        "proapi_secret", holders.RPC_ROUTES[0], "0x" + "1" * 40, "0x0dfe1681")
    assert status == 503 and holders.parse_eth_call(payload)[0] == "error"


def test_key_in_header_only(monkeypatch):
    seen = []

    def fake(req, timeout=None):
        seen.append(req)
        return Resp({"jsonrpc": "2.0", "id": 1, "result": WORD})
    monkeypatch.setattr(holders, "_urlopen", fake)
    for route in holders.RPC_ROUTES:
        holders.rpc_request("proapi_secret", route, "0x" + "1" * 40,
                            "0x0dfe1681")
    for req in seen:
        assert "proapi_secret" not in req.full_url
        assert req.get_header("Authorization") == "proapi_secret"


def test_probe_mode_writes_nothing_and_never_collects(monkeypatch, tmp_path,
                                                      capsys):
    monkeypatch.chdir(tmp_path)
    for name in ("do_collect", "do_probe", "do_dry_run"):
        monkeypatch.setattr(holders, name, lambda *a, **k: pytest.fail(name))
    monkeypatch.setattr(holders.time, "sleep", lambda s: None)
    monkeypatch.setattr(holders.urllib.request, "urlopen",
                        lambda req, timeout=None: Resp({"status": "ok"}))
    answers = iter([WORD, "0x"] * 8)
    monkeypatch.setattr(holders, "_urlopen", lambda req, timeout=None: Resp(
        {"jsonrpc": "2.0", "id": 1, "result": next(answers)}))
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_secret")
    monkeypatch.setattr(sys, "argv", ["holders.py", "--probe-rpc"])
    assert holders.main() == 0
    assert list(tmp_path.iterdir()) == []
    out = capsys.readouterr().out
    assert "proapi_secret" not in out and "https://" not in out
    for addr, ca, _ in holders.RPC_PROBE:
        assert addr in out and ca in out
    assert out.count(" ok ") == 4 and out.count(" revert ") == 4
    assert "credits remaining before" in out and "after" in out
