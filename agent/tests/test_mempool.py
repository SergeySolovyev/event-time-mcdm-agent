"""Unit tests for FlashbotsMempool (Plan E Task 4).

No real network calls.  All HTTP is mocked via an injected fake session
whose ``.post`` returns a context-manager wrapper around a fake response.

We do NOT depend on pytest-asyncio: each test is a plain sync function
that drives the async surface via ``asyncio.run(...)`` -- the same
pattern T2/T3 use.

The well-known burner keys hard-coded here (priv 0x...01 and 0x...02)
are documented across the Ethereum ecosystem; they hold no funds and
exist only to make the test signatures reproducible.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

# Allow `from mempool import ...` against the agent repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from mempool import FLASHBOTS_RELAY_URL, FlashbotsMempool  # noqa: E402


# --------------------------------------------------------------------- #
# Well-known burner test keys (zero balance, intentionally public).
# --------------------------------------------------------------------- #
AUTH_KEY = "0x0000000000000000000000000000000000000000000000000000000000000001"
AUTH_ADDR = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
WALLET_KEY = "0x0000000000000000000000000000000000000000000000000000000000000002"
WALLET_ADDR = "0x2B5AD5c4795c026514f8317c7a215E218DcCD6cF"
VAULT_ADDR = "0x000000000000000000000000000000000000dEaD"


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #
def _make_w3(block_number: int = 18_000_000, nonce: int = 0) -> MagicMock:
    """A sync MagicMock w3 with deterministic block_number / nonce."""
    w3 = MagicMock()
    w3.eth.block_number = block_number
    w3.eth.get_transaction_count = MagicMock(return_value=nonce)
    return w3


def _make_state(gas_price_gwei: float = 30.0) -> MagicMock:
    """Minimal BlockState-shaped MagicMock (mempool only reads gas_price_gwei)."""
    s = MagicMock()
    s.gas_price_gwei = gas_price_gwei
    return s


class _FakeResponse:
    """Async context manager + async .json() to mimic aiohttp's response."""

    def __init__(self, payload: dict):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    """Mimics aiohttp.ClientSession's ``.post`` enough for the live path.

    ``calls`` records (url, headers, body_bytes) for every POST so tests
    can assert on the envelope.
    """

    def __init__(self, response_payload: dict):
        self._payload = response_payload
        self.calls: list[tuple[str, dict, bytes]] = []

    def post(self, url, headers=None, data=None):
        self.calls.append((url, dict(headers or {}), bytes(data or b"")))
        return _FakeResponse(self._payload)


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #
def test_dry_run_does_not_post() -> None:
    session = MagicMock()
    session.post = MagicMock(
        side_effect=AssertionError("dry-run should not POST")
    )
    mp = FlashbotsMempool(
        w3=_make_w3(),
        auth_key=AUTH_KEY,
        wallet_key=WALLET_KEY,
        vault_addr=VAULT_ADDR,
        dry_run=True,
        http_session=session,
    )
    txhash = asyncio.run(mp.submit_private_tx("aave_v3", _make_state()))
    assert isinstance(txhash, str)
    assert txhash.startswith("0x")
    assert len(txhash) == 66  # 0x + sha256 hex
    session.post.assert_not_called()


def test_live_post_constructs_correct_envelope() -> None:
    session = _FakeSession({"jsonrpc": "2.0", "id": 1, "result": "0xabc123"})
    block_number = 18_000_000
    mp = FlashbotsMempool(
        w3=_make_w3(block_number=block_number),
        auth_key=AUTH_KEY,
        wallet_key=WALLET_KEY,
        vault_addr=VAULT_ADDR,
        dry_run=False,
        max_block_offset=25,
        http_session=session,
    )
    asyncio.run(mp.submit_private_tx("aave_v3", _make_state()))

    assert len(session.calls) == 1
    url, headers, body = session.calls[0]
    assert url == FLASHBOTS_RELAY_URL
    envelope = json.loads(body)
    assert envelope["method"] == "eth_sendPrivateTransaction"
    params = envelope["params"][0]
    assert isinstance(params["tx"], str) and params["tx"].startswith("0x")
    assert params["maxBlockNumber"] == hex(block_number + 25)
    assert params["preferences"] == {"fast": True}


def test_x_flashbots_signature_header_set() -> None:
    session = _FakeSession({"jsonrpc": "2.0", "id": 1, "result": "0xabc"})
    mp = FlashbotsMempool(
        w3=_make_w3(),
        auth_key=AUTH_KEY,
        wallet_key=WALLET_KEY,
        vault_addr=VAULT_ADDR,
        dry_run=False,
        http_session=session,
    )
    asyncio.run(mp.submit_private_tx("aave_v3", _make_state()))

    _, headers, _ = session.calls[0]
    sig = headers["X-Flashbots-Signature"]
    # Format: <0x-prefixed 20-byte address>:<0x-prefixed 65-byte signature>
    pattern = r"^0x[a-fA-F0-9]{40}:0x[a-fA-F0-9]{130}$"
    assert re.match(pattern, sig), f"bad sig format: {sig!r}"


def test_auth_key_is_different_from_wallet_key() -> None:
    """Recovered signer is AUTH_KEY's address, not the wallet's.

    This is the security-critical assertion -- leaking the auth_key
    must not leak the wallet, and vice versa.
    """
    session = _FakeSession({"jsonrpc": "2.0", "id": 1, "result": "0xabc"})
    mp = FlashbotsMempool(
        w3=_make_w3(),
        auth_key=AUTH_KEY,
        wallet_key=WALLET_KEY,
        vault_addr=VAULT_ADDR,
        dry_run=False,
        http_session=session,
    )
    asyncio.run(mp.submit_private_tx("aave_v3", _make_state()))

    _, headers, body = session.calls[0]
    sig = headers["X-Flashbots-Signature"]
    declared_addr, hex_sig = sig.split(":")

    # The declared address in the header is the auth key, not the wallet.
    assert declared_addr.lower() == AUTH_ADDR.lower()
    assert declared_addr.lower() != WALLET_ADDR.lower()

    # And the signature recovers to that same address (replaying the
    # mempool's signing scheme: encode_defunct(text="0x"+keccak(body).hex())).
    digest = keccak(body)
    message = encode_defunct(text="0x" + digest.hex())
    recovered = Account.recover_message(message, signature=hex_sig)
    assert recovered.lower() == AUTH_ADDR.lower()
    assert recovered.lower() != WALLET_ADDR.lower()


def test_relay_error_response_raises() -> None:
    session = _FakeSession(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "nonce too low"}}
    )
    mp = FlashbotsMempool(
        w3=_make_w3(),
        auth_key=AUTH_KEY,
        wallet_key=WALLET_KEY,
        vault_addr=VAULT_ADDR,
        dry_run=False,
        http_session=session,
    )
    with pytest.raises(RuntimeError, match="nonce too low"):
        asyncio.run(mp.submit_private_tx("aave_v3", _make_state()))


def test_wait_for_inclusion_returns_receipt_when_included() -> None:
    receipt = {"transactionHash": "0xabc", "status": 1, "blockNumber": 18_000_001}
    w3 = _make_w3()
    # First call -> None (not yet mined), second call -> the receipt.
    w3.eth.get_transaction_receipt = MagicMock(side_effect=[None, receipt])

    mp = FlashbotsMempool(
        w3=w3,
        auth_key=AUTH_KEY,
        wallet_key=WALLET_KEY,
        vault_addr=VAULT_ADDR,
        dry_run=True,
        max_block_offset=5,
    )

    async def _run():
        with patch("mempool.asyncio.sleep", new=AsyncMock()):
            return await mp.wait_for_inclusion("0xabc", max_block_offset=5)

    got = asyncio.run(_run())
    assert got == receipt


def test_wait_for_inclusion_returns_none_on_timeout() -> None:
    w3 = _make_w3(block_number=18_000_000)
    w3.eth.get_transaction_receipt = MagicMock(return_value=None)

    # Make block_number advance past the deadline so the loop exits
    # cleanly via the deadline-block check.
    block_seq = iter(
        [18_000_000, 18_000_001, 18_000_002, 18_000_003, 18_000_004,
         18_000_005, 18_000_999, 18_001_000]
    )
    # PropertyMock-ish: replace block_number with a side-effecting fn.
    type(w3.eth).block_number = property(lambda _self: next(block_seq))

    mp = FlashbotsMempool(
        w3=w3,
        auth_key=AUTH_KEY,
        wallet_key=WALLET_KEY,
        vault_addr=VAULT_ADDR,
        dry_run=True,
        max_block_offset=3,
    )

    async def _run():
        with patch("mempool.asyncio.sleep", new=AsyncMock()):
            return await mp.wait_for_inclusion("0xabc", max_block_offset=3)

    got = asyncio.run(_run())
    assert got is None


def test_relay_url_overridable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    custom = "https://my-private-relay.example/relay"
    monkeypatch.setenv("FLASHBOTS_RELAY_URL", custom)

    session = _FakeSession({"jsonrpc": "2.0", "id": 1, "result": "0xabc"})
    mp = FlashbotsMempool(
        w3=_make_w3(),
        auth_key=AUTH_KEY,
        wallet_key=WALLET_KEY,
        vault_addr=VAULT_ADDR,
        dry_run=False,
        http_session=session,
    )
    asyncio.run(mp.submit_private_tx("aave_v3", _make_state()))

    url, _, _ = session.calls[0]
    assert url == custom


def test_submit_returns_txhash_on_success() -> None:
    session = _FakeSession(
        {"jsonrpc": "2.0", "id": 1, "result": "0xdeadbeef0123456789"}
    )
    mp = FlashbotsMempool(
        w3=_make_w3(),
        auth_key=AUTH_KEY,
        wallet_key=WALLET_KEY,
        vault_addr=VAULT_ADDR,
        dry_run=False,
        http_session=session,
    )
    txhash = asyncio.run(mp.submit_private_tx("aave_v3", _make_state()))
    assert txhash == "0xdeadbeef0123456789"


def test_constructor_rejects_equal_auth_and_wallet_keys() -> None:
    """Defence-in-depth: rule out the auth==wallet misconfiguration."""
    with pytest.raises(ValueError, match="MUST be different"):
        FlashbotsMempool(
            w3=_make_w3(),
            auth_key=AUTH_KEY,
            wallet_key=AUTH_KEY,
            vault_addr=VAULT_ADDR,
            dry_run=True,
        )
