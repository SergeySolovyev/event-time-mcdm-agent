"""Fluid liquidity-layer reader.

Fluid (by Instadapp) consolidates lending markets behind a single
``LiquidityResolver``-style contract.  ``getOverallTokenData(address)``
returns the aggregate per-token state including:

* ``supplyRate``  -- WAD (1e18) per-second supply rate
* ``totalSupply`` -- supply-side liquidity, in underlying units
* ``totalBorrow`` -- borrow-side debt, in underlying units

The agent uses the per-second WAD rate to compute an APY and the
totals to derive utilization and TVL.

Contract: ``0x52aa899454998Be5b000Ad077a46Bbe360F4e497`` (Fluid liquidity layer).
"""

from __future__ import annotations

import asyncio

from web3 import Web3

from .base import ProtocolData, ProtocolReader


SCALE_18 = 10**18
USDC_DECIMALS = 10**6
SECONDS_PER_YEAR = 365 * 24 * 60 * 60

FLUID_LIQUIDITY_ADDRESS = "0x52aa899454998Be5b000Ad077a46Bbe360F4e497"

FLUID_ABI = [
    {
        "inputs": [{"name": "token", "type": "address"}],
        "name": "getOverallTokenData",
        "outputs": [
            {"name": "supplyRate", "type": "uint256"},
            {"name": "borrowRate", "type": "uint256"},
            {"name": "totalSupply", "type": "uint256"},
            {"name": "totalBorrow", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]


class FluidReader(ProtocolReader):
    """Reader for Fluid's liquidity-layer per-token view."""

    NAME = "Fluid"
    ADAPTER_INDEX = 4

    def __init__(
        self,
        w3: Web3,
        token_address: str,
        liquidity_address: str = FLUID_LIQUIDITY_ADDRESS,
        adapter_index: int = 4,
    ):
        super().__init__(w3)
        self.ADAPTER_INDEX = adapter_index
        self.token = Web3.to_checksum_address(token_address)
        self.liquidity = w3.eth.contract(
            address=Web3.to_checksum_address(liquidity_address),
            abi=FLUID_ABI,
        )

    def read(self) -> ProtocolData:
        block_number = self.w3.eth.block_number
        return asyncio.run(self.read_at_block(block_number))

    async def read_at_block(self, block_number: int) -> ProtocolData:
        data = self.liquidity.functions.getOverallTokenData(self.token).call(
            block_identifier=block_number
        )
        supply_rate_per_sec = data[0]
        total_supply = data[2]
        total_borrow = data[3]

        # WAD per-second rate -> annualized APY (decimal).
        apy = (supply_rate_per_sec / SCALE_18) * SECONDS_PER_YEAR
        rate_1e18 = int(supply_rate_per_sec * SECONDS_PER_YEAR)

        utilization = (
            (total_borrow / total_supply) if total_supply > 0 else 0.0
        )
        tvl = total_supply / USDC_DECIMALS

        return ProtocolData(
            name=self.NAME,
            adapter_index=self.ADAPTER_INDEX,
            apy=apy,
            utilization=min(utilization, 1.0),
            tvl=tvl,
            raw_rate_1e18=rate_1e18,
        )
