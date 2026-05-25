"""Euler V2 (EVK vault) reader.

Euler V2 ships as an ecosystem of "EVK" (Euler Vault Kit) vaults, one per
asset.  This reader is parameterized by a single vault address -- the USDC
vault for the agent's primary integration -- and queries:

* ``interestRate()`` -- per-second supply rate scaled to 1e27 (RAY).  The
  EVK matches Aave's RAY scaling rather than a WAD per-second figure, so
  we divide by 1e27 first and then annualize.
* ``totalAssets()``  -- underlying-asset supply (TVL numerator).
* ``totalBorrows()`` -- outstanding debt against this vault.

The USDC vault address is read from the ``EULER_USDC_VAULT`` environment
variable.  We deliberately do NOT hardcode it -- the EVK is a factory, so
the canonical USDC vault can be redeployed, and operators must opt-in by
setting the env var.  A missing env raises ``ValueError`` at construction.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from web3 import Web3

from .base import ProtocolData, ProtocolReader


RAY = 10**27
SCALE_18 = 10**18
USDC_DECIMALS = 10**6
SECONDS_PER_YEAR = 365 * 24 * 60 * 60

EVK_ABI = [
    {
        "inputs": [],
        "name": "interestRate",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "totalAssets",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "totalBorrows",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class EulerV2Reader(ProtocolReader):
    """Reader for a single Euler V2 EVK vault (per-asset)."""

    NAME = "Euler V2"
    ADAPTER_INDEX = 5

    def __init__(
        self,
        w3: Web3,
        vault_address: Optional[str] = None,
        adapter_index: int = 5,
    ):
        super().__init__(w3)
        self.ADAPTER_INDEX = adapter_index

        if vault_address is None:
            vault_address = os.environ.get("EULER_USDC_VAULT")
        if not vault_address:
            raise ValueError(
                "EulerV2Reader requires a vault address: pass `vault_address=` "
                "or set the EULER_USDC_VAULT environment variable."
            )

        self.vault = w3.eth.contract(
            address=Web3.to_checksum_address(vault_address),
            abi=EVK_ABI,
        )

    def read(self) -> ProtocolData:
        block_number = self.w3.eth.block_number
        return asyncio.run(self.read_at_block(block_number))

    async def read_at_block(self, block_number: int) -> ProtocolData:
        # EVK interestRate is RAY-scaled (1e27) per-second supply rate.
        per_second_ray = self.vault.functions.interestRate().call(
            block_identifier=block_number
        )
        total_assets = self.vault.functions.totalAssets().call(
            block_identifier=block_number
        )
        total_borrows = self.vault.functions.totalBorrows().call(
            block_identifier=block_number
        )

        apy = (per_second_ray / RAY) * SECONDS_PER_YEAR
        # Normalize the per-second RAY rate into the agent's 1e18 EMA basis.
        rate_1e18 = int(per_second_ray * SECONDS_PER_YEAR * SCALE_18 // RAY)

        utilization = (
            (total_borrows / total_assets) if total_assets > 0 else 0.0
        )
        tvl = total_assets / USDC_DECIMALS

        return ProtocolData(
            name=self.NAME,
            adapter_index=self.ADAPTER_INDEX,
            apy=apy,
            utilization=min(utilization, 1.0),
            tvl=tvl,
            raw_rate_1e18=rate_1e18,
        )
