"""Morpho Blue reader.

Morpho Blue exposes per-market state through ``market(bytes32 id)``, which
returns a struct including ``totalSupplyAssets`` and ``totalBorrowAssets``.
The supply rate is obtained via ``borrowRate(bytes32 id, Market memory m)``
on the market's configured Interest Rate Model (IRM).

DEVIATION FROM PLAN-DOC -- AdaptiveCurve IRM:
    Plan E's design doc references a static ``f_kink`` borrow-rate parameter,
    inherited from Aave's static-curve interest-rate strategy.  The IRM
    Morpho ships with by default -- ``AdaptiveCurveIRM`` -- does NOT expose a
    static kink: the rate is path-dependent (it drifts upwards when
    utilization > target, downwards otherwise, and remembers prior state via
    ``rateAtTarget``).  We therefore CANNOT publish a static ``f_kink`` for
    this reader.  Instead we query the IRM's ``borrowRate`` directly each
    block and convert the per-second result to an APY.  Downstream MCDM
    scoring receives the same ``apy`` field as every other reader.

Contract: ``0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFFb`` (Morpho Blue).
"""

from __future__ import annotations

import asyncio

from web3 import Web3

from .base import ProtocolData, ProtocolReader


SCALE_18 = 10**18
USDC_DECIMALS = 10**6
SECONDS_PER_YEAR = 365 * 24 * 60 * 60  # plan-spec annualization (no leap-day)

# Morpho Blue mainnet address.
MORPHO_BLUE_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFFb"

MORPHO_BLUE_ABI = [
    {
        "inputs": [{"name": "id", "type": "bytes32"}],
        "name": "market",
        "outputs": [
            {"name": "totalSupplyAssets", "type": "uint128"},
            {"name": "totalSupplyShares", "type": "uint128"},
            {"name": "totalBorrowAssets", "type": "uint128"},
            {"name": "totalBorrowShares", "type": "uint128"},
            {"name": "lastUpdate", "type": "uint128"},
            {"name": "fee", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "id", "type": "bytes32"}],
        "name": "idToMarketParams",
        "outputs": [
            {"name": "loanToken", "type": "address"},
            {"name": "collateralToken", "type": "address"},
            {"name": "oracle", "type": "address"},
            {"name": "irm", "type": "address"},
            {"name": "lltv", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

# Minimal IRM ABI -- AdaptiveCurveIRM and friends expose ``borrowRateView``
# (read-only variant) returning the per-second borrow rate scaled to 1e18.
IRM_ABI = [
    {
        "inputs": [
            {"name": "id", "type": "bytes32"},
            {
                "components": [
                    {"name": "totalSupplyAssets", "type": "uint128"},
                    {"name": "totalSupplyShares", "type": "uint128"},
                    {"name": "totalBorrowAssets", "type": "uint128"},
                    {"name": "totalBorrowShares", "type": "uint128"},
                    {"name": "lastUpdate", "type": "uint128"},
                    {"name": "fee", "type": "uint128"},
                ],
                "name": "market",
                "type": "tuple",
            },
        ],
        "name": "borrowRateView",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class MorphoBlueReader(ProtocolReader):
    """Reader for one Morpho Blue market (identified by its bytes32 id)."""

    NAME = "Morpho Blue"
    ADAPTER_INDEX = 3

    def __init__(
        self,
        w3: Web3,
        market_id: bytes,
        morpho_address: str = MORPHO_BLUE_ADDRESS,
        adapter_index: int = 3,
    ):
        super().__init__(w3)
        self.ADAPTER_INDEX = adapter_index
        if len(market_id) != 32:
            raise ValueError(
                f"market_id must be exactly 32 bytes; got {len(market_id)}"
            )
        self.market_id = market_id
        self.morpho = w3.eth.contract(
            address=Web3.to_checksum_address(morpho_address),
            abi=MORPHO_BLUE_ABI,
        )
        self._irm = None  # lazily bound on first read

    def read(self) -> ProtocolData:
        block_number = self.w3.eth.block_number
        return asyncio.run(self.read_at_block(block_number))

    async def read_at_block(self, block_number: int) -> ProtocolData:
        market = self.morpho.functions.market(self.market_id).call(
            block_identifier=block_number
        )
        total_supply_assets = market[0]
        total_borrow_assets = market[2]

        # Bind the IRM contract once (the params struct is immutable per market).
        if self._irm is None:
            params = self.morpho.functions.idToMarketParams(self.market_id).call(
                block_identifier=block_number
            )
            irm_addr = Web3.to_checksum_address(params[3])
            self._irm = self.w3.eth.contract(address=irm_addr, abi=IRM_ABI)

        # AdaptiveCurveIRM returns a per-second WAD-scaled borrow rate.
        per_second_rate = self._irm.functions.borrowRateView(
            self.market_id, market[:6]
        ).call(block_identifier=block_number)

        apy = (per_second_rate / SCALE_18) * SECONDS_PER_YEAR
        rate_1e18 = int(per_second_rate * SECONDS_PER_YEAR)

        utilization = (
            (total_borrow_assets / total_supply_assets)
            if total_supply_assets > 0
            else 0.0
        )
        tvl = total_supply_assets / USDC_DECIMALS

        return ProtocolData(
            name=self.NAME,
            adapter_index=self.ADAPTER_INDEX,
            apy=apy,
            utilization=min(utilization, 1.0),
            tvl=tvl,
            raw_rate_1e18=rate_1e18,
        )
