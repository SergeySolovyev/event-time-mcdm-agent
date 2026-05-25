"""Spark sUSDS pool reader.

Spark is an Aave V3 fork, so the ABI and rate semantics are identical:

* ``getReserveData(asset)`` returns the same 15-tuple struct as Aave V3.
* ``currentLiquidityRate`` is RAY-scaled (1e27) and already annualized --
  no per-second conversion needed.
* TVL is the aToken's ``totalSupply`` in underlying-asset units.

Plan E adds a per-block async slot ``read_at_block(block_number)`` so the
event-time backtester can sample the pool's state at arbitrary historical
blocks.  The legacy sync ``read()`` delegates to ``read_at_block`` pinned
to the current head, preserving the ABC contract used by the orchestrator.
"""

from __future__ import annotations

import asyncio

from web3 import Web3

from .base import ProtocolData, ProtocolReader


RAY = 10**27
SCALE_18 = 10**18
USDC_DECIMALS = 10**6

# Spark mainnet pool address (sUSDS / Spark Lend).
SPARK_POOL_ADDRESS = "0xC13e21B648A5Ee794902342038FF3aDAB66BE987"

POOL_ABI = [
    {
        "inputs": [{"name": "asset", "type": "address"}],
        "name": "getReserveData",
        "outputs": [
            {
                "components": [
                    {"name": "configuration", "type": "uint256"},
                    {"name": "liquidityIndex", "type": "uint128"},
                    {"name": "currentLiquidityRate", "type": "uint128"},
                    {"name": "variableBorrowIndex", "type": "uint128"},
                    {"name": "currentVariableBorrowRate", "type": "uint128"},
                    {"name": "currentStableBorrowRate", "type": "uint128"},
                    {"name": "lastUpdateTimestamp", "type": "uint40"},
                    {"name": "id", "type": "uint16"},
                    {"name": "aTokenAddress", "type": "address"},
                    {"name": "stableDebtTokenAddress", "type": "address"},
                    {"name": "variableDebtTokenAddress", "type": "address"},
                    {"name": "interestRateStrategyAddress", "type": "address"},
                    {"name": "accruedToTreasury", "type": "uint128"},
                    {"name": "unbacked", "type": "uint128"},
                    {"name": "isolationModeTotalDebt", "type": "uint128"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

ERC20_ABI = [
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class SparkReader(ProtocolReader):
    """Reader for Spark's Aave V3-style lending pool."""

    NAME = "Spark"
    ADAPTER_INDEX = 2

    def __init__(
        self,
        w3: Web3,
        asset_address: str,
        pool_address: str = SPARK_POOL_ADDRESS,
        adapter_index: int = 2,
    ):
        super().__init__(w3)
        self.ADAPTER_INDEX = adapter_index
        self.asset = Web3.to_checksum_address(asset_address)
        self.pool = w3.eth.contract(
            address=Web3.to_checksum_address(pool_address),
            abi=POOL_ABI,
        )
        self._atoken = None
        self._variable_debt = None

    # ------------------------------------------------------------------
    # Sync path -- delegates to the per-block async path at head.
    # ------------------------------------------------------------------
    def read(self) -> ProtocolData:
        block_number = self.w3.eth.block_number
        return asyncio.run(self.read_at_block(block_number))

    # ------------------------------------------------------------------
    # Async per-block path (Plan E).
    # ------------------------------------------------------------------
    async def read_at_block(self, block_number: int) -> ProtocolData:
        reserve_data = self.pool.functions.getReserveData(self.asset).call(
            block_identifier=block_number
        )

        # currentLiquidityRate is RAY-scaled and pre-annualized.
        liquidity_rate_ray = reserve_data[2]
        rate_1e18 = liquidity_rate_ray * SCALE_18 // RAY
        apy = liquidity_rate_ray / RAY

        if self._atoken is None or self._variable_debt is None:
            atoken_addr = Web3.to_checksum_address(reserve_data[8])
            vdebt_addr = Web3.to_checksum_address(reserve_data[10])
            self._atoken = self.w3.eth.contract(address=atoken_addr, abi=ERC20_ABI)
            self._variable_debt = self.w3.eth.contract(address=vdebt_addr, abi=ERC20_ABI)

        total_supplied = self._atoken.functions.totalSupply().call(
            block_identifier=block_number
        )
        total_borrowed = self._variable_debt.functions.totalSupply().call(
            block_identifier=block_number
        )

        utilization = (total_borrowed / total_supplied) if total_supplied > 0 else 0.0
        tvl = total_supplied / USDC_DECIMALS

        return ProtocolData(
            name=self.NAME,
            adapter_index=self.ADAPTER_INDEX,
            apy=apy,
            utilization=min(utilization, 1.0),
            tvl=tvl,
            raw_rate_1e18=rate_1e18,
        )
