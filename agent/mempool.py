"""Flashbots private-mempool client for the per-block decision loop.

Plan E Task 4.

The :class:`FlashbotsMempool` implements the duck-typed surface that
:class:`agent.per_block_loop.PerBlockLoop` requires:

    async def submit_private_tx(target_protocol: str, state: BlockState) -> str
    async def wait_for_inclusion(txhash: str, max_block_offset: int = 25) -> dict | None

Why Flashbots private submission instead of public mempool
----------------------------------------------------------
Public-mempool submission of a "switch from protocol A to protocol B"
rebalance tx is sandwich-bait: anyone watching the mempool can
front-run by depositing into B (pushing the supply APR down so we
get worse fill) and back-run by withdrawing.  Flashbots'
``eth_sendPrivateTransaction`` keeps the tx out of the public mempool
until it lands in a bundle, eliminating the front-running window.

Signature scheme
----------------
Flashbots authenticates requests with TWO keys -- this is critical:

* ``wallet_key`` signs the *transaction* itself (the tx that moves
  funds from the vault).
* ``auth_key`` signs the *request body* (so Flashbots' reputation
  system can track this searcher's identity without ever seeing the
  wallet key).

These MUST be different addresses: leaking auth_key reveals identity
but not funds; leaking wallet_key drains the vault.  The
:func:`test_auth_key_is_different_from_wallet_key` test asserts the
header signature recovers to the auth_key address, not the wallet.

The header format is exactly:

    X-Flashbots-Signature: <auth_address>:<hex_signature>

where the signature is over ``encode_defunct(keccak(json_body))`` --
i.e. the EIP-191 "personal_sign" wrapping of the keccak digest of the
raw JSON-RPC body.

Dry-run mode
------------
``dry_run=True`` skips the POST entirely and returns a deterministic
mock txhash derived from sha256(body).  This is the default so the
T7 live-trading wiring does not accidentally fire real bundles
during development; the wiring step must explicitly pass
``dry_run=False``.

Notes for tests
---------------
* No real network calls.  Tests inject a MagicMock ``http_session``
  whose ``post`` is patched to return a stubbed response.
* The private helper :py:meth:`_sign_flashbots_header` is split out
  from :py:meth:`submit_private_tx` so signature-format tests can
  patch it without exercising real keccak+sign each time; the live
  envelope test does NOT patch it (we want end-to-end verification of
  the header format with the well-known burner key 0x...01).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak


logger = logging.getLogger(__name__)


# Module-level so tests can monkeypatch via the env var.  Read each call
# rather than at import time so test fixtures setting FLASHBOTS_RELAY_URL
# after import still take effect.
FLASHBOTS_RELAY_URL = os.environ.get(
    "FLASHBOTS_RELAY_URL", "https://relay.flashbots.net"
)


def _current_relay_url() -> str:
    """Return the relay URL, re-reading the env var each call.

    Tests use monkeypatch.setenv after construction; reading lazily here
    means the new value is honoured without re-instantiating the client.
    """
    return os.environ.get("FLASHBOTS_RELAY_URL", FLASHBOTS_RELAY_URL)


class FlashbotsMempool:
    """Async Flashbots ``eth_sendPrivateTransaction`` client.

    Parameters
    ----------
    w3:
        Web3 (sync or async) instance used to read the current block
        number for ``maxBlockNumber`` and to poll for receipts.
    auth_key:
        Hex private key that signs the request body (Flashbots identity).
        MUST differ from ``wallet_key``.
    wallet_key:
        Hex private key that signs the tx itself (drains the vault).
    vault_addr:
        Default ``to`` address for the stub rebalance tx.
    dry_run:
        If True, do not POST -- return a deterministic mock txhash.
        Default True so accidents during dev do not fire real bundles.
    max_block_offset:
        How many blocks ahead of ``current_block`` the bundle remains
        eligible for inclusion (Flashbots ``maxBlockNumber`` param).
    http_session:
        Optional injected aiohttp.ClientSession.  Tests pass a MagicMock
        whose ``.post`` is an AsyncMock.  Real callers leave None and
        the session is created lazily on first use.
    """

    def __init__(
        self,
        w3: Any,
        auth_key: str,
        wallet_key: str,
        vault_addr: str,
        dry_run: bool = True,
        max_block_offset: int = 25,
        http_session: Any = None,
    ) -> None:
        self.w3 = w3
        self.auth_key = auth_key
        self.wallet_key = wallet_key
        self.vault_addr = vault_addr
        self.dry_run = dry_run
        self.max_block_offset = max_block_offset
        self._http_session = http_session

        self._auth_account = Account.from_key(auth_key)
        self._wallet_account = Account.from_key(wallet_key)

        if self._auth_account.address == self._wallet_account.address:
            raise ValueError(
                "auth_key and wallet_key MUST be different addresses "
                "(see module docstring)"
            )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    async def _get_session(self) -> Any:
        """Return the injected session or lazily build a real one.

        We import aiohttp inside the function so module import does not
        require aiohttp for callers that only use ``dry_run=True``.
        """
        if self._http_session is None:
            import aiohttp  # local import: dry-run callers don't need it
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def _current_block_number(self) -> int:
        """Return the current block number, handling sync/async w3 alike."""
        raw = self.w3.eth.block_number
        if asyncio.iscoroutine(raw):
            raw = await raw
        return int(raw)

    async def _get_nonce(self) -> int:
        """Fetch the wallet's transaction count, sync/async tolerant."""
        getter = self.w3.eth.get_transaction_count
        raw = getter(self._wallet_account.address)
        if asyncio.iscoroutine(raw):
            raw = await raw
        return int(raw)

    def _build_rebalance_tx(
        self, target_protocol: str, state: Any
    ) -> dict:
        """Build the rebalance-tx dict.

        STUB.  Real implementation lives in Plan E T7 (live-trading
        wiring) and will ABI-encode a call to the vault's adapter
        switch function.  For T4 the loop just needs *something*
        signable -- a value=0 zero-data tx to the vault.  The
        ``target_protocol`` argument is recorded in the log so the
        eventual real implementation has the right hook point.
        """
        logger.debug(
            "stub rebalance tx for target=%s (T7 wires real calldata)",
            target_protocol,
        )
        return {
            "to": self.vault_addr,
            "value": 0,
            "data": b"",
            "gas": 200_000,
            "gasPrice": int(state.gas_price_gwei * 1e9),
            "nonce": 0,  # filled in by submit_private_tx via _get_nonce
            "chainId": 1,
        }

    def _sign_flashbots_header(self, body: bytes) -> str:
        """Compute the X-Flashbots-Signature header value.

        Format: ``<auth_address>:<hex_signature>`` where the signature
        is over ``encode_defunct(keccak(body))``.

        Split out as a separate method so tests can patch it cheaply;
        the live-path test exercises the real implementation end-to-end.
        """
        digest = keccak(body)
        # keccak() returns 32 raw bytes; encode_defunct expects either
        # text or hex.  The Flashbots spec hashes the body, hex-encodes
        # the digest, and signs *that* string via personal_sign.
        message = encode_defunct(text="0x" + digest.hex())
        signed = self._auth_account.sign_message(message)
        sig_hex = signed.signature.hex()
        if not sig_hex.startswith("0x"):
            sig_hex = "0x" + sig_hex
        return f"{self._auth_account.address}:{sig_hex}"

    def _mock_txhash(self, body: bytes) -> str:
        """Deterministic dry-run txhash from sha256(body).

        sha256 (not keccak) so it's obvious this isn't a real Ethereum
        hash; same length / 0x-prefix so callers can treat it
        identically.
        """
        return "0x" + hashlib.sha256(body).hexdigest()

    # ------------------------------------------------------------------ #
    # Public surface
    # ------------------------------------------------------------------ #
    async def submit_private_tx(
        self, target_protocol: str, state: Any
    ) -> str:
        """Submit a private rebalance tx via Flashbots.

        Returns the txhash (mock txhash in dry-run mode).
        """
        # 1. Build + sign the tx with the wallet key.
        tx = self._build_rebalance_tx(target_protocol, state)
        tx["nonce"] = await self._get_nonce()
        signed_tx = self._wallet_account.sign_transaction(tx)
        signed_raw_hex = "0x" + signed_tx.raw_transaction.hex().lstrip("0x")

        # 2. Determine the maxBlockNumber for the bundle.
        current_block = await self._current_block_number()
        max_block = current_block + self.max_block_offset

        # 3. JSON-RPC envelope.
        envelope = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_sendPrivateTransaction",
            "params": [
                {
                    "tx": signed_raw_hex,
                    "maxBlockNumber": hex(max_block),
                    "preferences": {"fast": True},
                }
            ],
        }
        body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")

        # 4. Sign the body for the X-Flashbots-Signature header.
        sig_header = self._sign_flashbots_header(body)
        headers = {
            "Content-Type": "application/json",
            "X-Flashbots-Signature": sig_header,
        }

        # 5. Dry-run short-circuit -- never touch the network.
        if self.dry_run:
            mock_hash = self._mock_txhash(body)
            logger.info(
                "DRY-RUN: would POST to %s for target=%s; returning mock %s",
                _current_relay_url(),
                target_protocol,
                mock_hash,
            )
            return mock_hash

        # 6. Live path: POST and parse the JSON-RPC response.
        session = await self._get_session()
        relay_url = _current_relay_url()
        async with session.post(relay_url, headers=headers, data=body) as resp:
            payload = await resp.json()

        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"Flashbots relay error: {msg}")

        result = payload.get("result") if isinstance(payload, dict) else None
        if not result:
            raise RuntimeError(f"Flashbots relay returned no result: {payload!r}")

        logger.info(
            "submitted private tx %s for target=%s (maxBlockNumber=%s)",
            result,
            target_protocol,
            hex(max_block),
        )
        return result

    async def wait_for_inclusion(
        self, txhash: str, max_block_offset: int | None = None
    ) -> dict | None:
        """Poll for a receipt until included or the block deadline passes.

        Returns the receipt dict on inclusion, or None on timeout.  Sleeps
        12s between polls (one Ethereum block).  Tests patch
        ``asyncio.sleep`` to skip the wall-clock wait.
        """
        if max_block_offset is None:
            max_block_offset = self.max_block_offset

        start_block = await self._current_block_number()
        deadline_block = start_block + max_block_offset

        for _ in range(max_block_offset):
            getter = self.w3.eth.get_transaction_receipt
            try:
                receipt = getter(txhash)
                if asyncio.iscoroutine(receipt):
                    receipt = await receipt
            except Exception:  # noqa: BLE001 -- TransactionNotFound, etc.
                receipt = None

            if receipt is not None:
                logger.info("tx %s included; receipt obtained", txhash)
                return receipt

            current = await self._current_block_number()
            if current >= deadline_block:
                logger.warning(
                    "tx %s not included by block %d (deadline)",
                    txhash,
                    deadline_block,
                )
                return None

            await asyncio.sleep(12)

        logger.warning("tx %s wait_for_inclusion timed out after %d polls",
                       txhash, max_block_offset)
        return None
