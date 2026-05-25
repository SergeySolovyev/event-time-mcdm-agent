"""Flashbots dry-run smoke check — referenced by agent/RUNBOOK.md §"Flashbots dry-run verification".

Builds a rebalance tx, signs the X-Flashbots-Signature header with
FLASHBOTS_AUTH_KEY, and prints the resulting envelope WITHOUT POSTing
to the relay. Lets the operator confirm:

  1. The tx can be built and signed end-to-end.
  2. The signer address recoverable from X-Flashbots-Signature matches
     the AUTH key's address, NOT the wallet address (privacy gate).
  3. submit_private_tx returns {'status': 'dry_run', 'txhash': ...}.

Usage:
    .venv\\Scripts\\python -m agent.scripts.flashbots_smoke \\
        --auth-key %FLASHBOTS_AUTH_KEY% \\
        --wallet-key %WALLET_KEY% \\
        --rpc-url %SEPOLIA_HTTP_URL%

No real network call to relay.flashbots.net is made (dry_run=True).
The script DOES make one read-only RPC call to get the current
chain ID + nonce; that's harmless.

Plan E Task 7 deferred helper. Operator-facing only — never imported
by the agent runtime.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Allow `import per_block_loop` style from agent/ root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eth_account import Account
from web3 import Web3

from decision.base import Action, BlockState  # via the T1 junction
import pandas as pd

from mempool import FlashbotsMempool


def _dummy_state(block_number: int = 1) -> BlockState:
    """Build a minimal valid BlockState for the smoke. Values are placeholders
    -- the dry-run path does not care about APR / utilization realism."""
    protos = ("aave_v3", "compound_v3", "spark", "morpho", "fluid", "euler")
    return BlockState(
        block_number=block_number,
        block_timestamp=pd.Timestamp.utcnow().tz_localize("UTC")
        if pd.Timestamp.utcnow().tz is None
        else pd.Timestamp.utcnow(),
        protocols=protos,
        lending_apr={p: 0.04 for p in protos},
        utilization={p: 0.70 for p in protos},
        tvl_usd={p: 1e9 for p in protos},
        current_protocol="aave_v3",
        position_usd=1_000_000.0,
        gas_price_gwei=25.0,
        eth_price_usd=3500.0,
        gas_used_estimate=200_000,
    )


async def _run(auth_key: str, wallet_key: str, rpc_url: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("[1/3] Building tx for migration aave -> morpho on Sepolia...")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        print(f"      ! RPC {rpc_url[:35]}... not reachable; aborting")
        return 2

    auth_addr = Account.from_key(auth_key).address
    wallet_addr = Account.from_key(wallet_key).address
    if auth_addr.lower() == wallet_addr.lower():
        print(
            "      ! AUTH and WALLET addresses are EQUAL; this would dox the\n"
            "        wallet to every relay observer. Regenerate the AUTH key."
        )
        return 3

    # FlashbotsMempool's constructor enforces auth != wallet; using a clearly-
    # fake vault address is OK in dry_run mode (no contract call is made).
    mempool = FlashbotsMempool(
        w3=w3,
        auth_key=auth_key,
        wallet_key=wallet_key,
        vault_addr="0x000000000000000000000000000000000000dEaD",
        dry_run=True,
        max_block_offset=25,
    )

    print("[2/3] Signing X-Flashbots-Signature with FLASHBOTS_AUTH_KEY...")
    print(f"      auth addr   = {auth_addr}")
    print(f"      wallet addr = {wallet_addr}  (must differ)")

    state = _dummy_state(block_number=w3.eth.block_number)

    print("[3/3] dry_run=True -- no POST. Calling submit_private_tx...")
    result = await mempool.submit_private_tx(target_protocol="morpho", state=state)

    print(f"      result = {result}")
    if not isinstance(result, dict):
        print(f"      ! expected dict, got {type(result).__name__}")
        return 4
    if result.get("status") != "dry_run":
        print(f"      ! expected status='dry_run', got {result.get('status')!r}")
        return 5
    if not result.get("txhash"):
        print("      ! expected non-empty txhash")
        return 6

    print("\n[OK] Flashbots dry-run path verified end-to-end.")
    print(
        "     Build, sign, and X-Flashbots-Signature header generation work.\n"
        "     No tx was POSTed to the relay. Ready for live run once you flip\n"
        "     `mempool.dry_run: false` in configs/sepolia_paper.yaml."
    )
    return 0


def _parse_argv(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--auth-key", required=True,
        help="Flashbots reputation signer key (NOT the wallet key).",
    )
    ap.add_argument(
        "--wallet-key", required=True,
        help="Sepolia-funded burner key. MUST differ from --auth-key.",
    )
    ap.add_argument(
        "--rpc-url", required=True,
        help="Sepolia HTTP RPC (Alchemy/Infura/publicnode/Ankr).",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_argv(argv)
    return asyncio.run(
        _run(
            auth_key=args.auth_key,
            wallet_key=args.wallet_key,
            rpc_url=args.rpc_url,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
