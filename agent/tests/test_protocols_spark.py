"""Unit tests for SparkReader (Plan E Task 2).

Spark is an Aave V3 fork; the reader mirrors the AaveV3Reader's RAY
arithmetic but uses Plan E's per-block async slot ``read_at_block``.

Tests mock the web3 contract layer end-to-end (no real RPC).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Allow `from protocols.spark import ...` against the agent repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from protocols.base import ProtocolData                                # noqa: E402
from protocols.spark import (                                          # noqa: E402
    RAY,
    SPARK_POOL_ADDRESS,
    USDC_DECIMALS,
    SparkReader,
)


def _fn(return_value):
    fn = MagicMock()
    fn.call = MagicMock(return_value=return_value)
    return fn


def _reserve_data(liquidity_rate_ray: int):
    """A 15-tuple matching Aave V3 ReserveData layout."""
    return (
        0,                       # configuration
        0,                       # liquidityIndex
        liquidity_rate_ray,      # currentLiquidityRate
        0,                       # variableBorrowIndex
        0,                       # currentVariableBorrowRate
        0,                       # currentStableBorrowRate
        0,                       # lastUpdateTimestamp
        0,                       # id
        "0x" + "11" * 20,        # aTokenAddress
        "0x" + "00" * 20,        # stableDebtTokenAddress
        "0x" + "22" * 20,        # variableDebtTokenAddress
        "0x" + "00" * 20,        # interestRateStrategyAddress
        0,                       # accruedToTreasury
        0,                       # unbacked
        0,                       # isolationModeTotalDebt
    )


def _build_reader(liquidity_rate_ray: int, atoken_supply: int, vdebt_supply: int):
    """Construct a SparkReader with every on-chain call mocked."""
    # First w3.eth.contract() call returns the pool; subsequent calls
    # (for aToken / variableDebtToken) return separate mocks.
    pool_mock = MagicMock()
    pool_mock.functions = MagicMock()
    pool_mock.functions.getReserveData = MagicMock(
        return_value=_fn(_reserve_data(liquidity_rate_ray))
    )

    atoken_mock = MagicMock()
    atoken_mock.functions = MagicMock()
    atoken_mock.functions.totalSupply = MagicMock(return_value=_fn(atoken_supply))

    vdebt_mock = MagicMock()
    vdebt_mock.functions = MagicMock()
    vdebt_mock.functions.totalSupply = MagicMock(return_value=_fn(vdebt_supply))

    contract_queue = [pool_mock, atoken_mock, vdebt_mock]

    w3 = MagicMock()
    w3.eth.contract = MagicMock(side_effect=lambda **kw: contract_queue.pop(0))
    w3.eth.block_number = 19_000_000

    return SparkReader(
        w3,
        asset_address="0x" + "aa" * 20,
        pool_address=SPARK_POOL_ADDRESS,
    )


class TestSparkReadAtBlock:
    def test_returns_protocol_data_with_spark_name(self):
        reader = _build_reader(
            liquidity_rate_ray=5 * RAY // 100,  # 5% APY
            atoken_supply=1000 * USDC_DECIMALS,
            vdebt_supply=500 * USDC_DECIMALS,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert isinstance(snap, ProtocolData)
        assert snap.name == "Spark"

    def test_ray_apy_decoded(self):
        """5% RAY-scaled annualized rate -> apy ~= 0.05."""
        reader = _build_reader(
            liquidity_rate_ray=5 * RAY // 100,
            atoken_supply=1000 * USDC_DECIMALS,
            vdebt_supply=0,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert 0.0 <= snap.apy <= 1.0
        assert abs(snap.apy - 0.05) < 1e-9

    def test_utilization_from_token_supplies(self):
        reader = _build_reader(
            liquidity_rate_ray=0,
            atoken_supply=1000 * USDC_DECIMALS,
            vdebt_supply=700 * USDC_DECIMALS,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert 0.0 <= snap.utilization <= 1.0
        assert abs(snap.utilization - 0.70) < 1e-6

    def test_tvl_positive_in_underlying_units(self):
        reader = _build_reader(
            liquidity_rate_ray=0,
            atoken_supply=2_500_000 * USDC_DECIMALS,
            vdebt_supply=0,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert snap.tvl > 0
        assert snap.tvl == 2_500_000.0

    def test_raw_rate_1e18_positive(self):
        reader = _build_reader(
            liquidity_rate_ray=5 * RAY // 100,
            atoken_supply=1000 * USDC_DECIMALS,
            vdebt_supply=0,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert snap.raw_rate_1e18 > 0

    def test_block_identifier_passed_through(self):
        """read_at_block must pin every call to the requested block."""
        reader = _build_reader(
            liquidity_rate_ray=5 * RAY // 100,
            atoken_supply=1000 * USDC_DECIMALS,
            vdebt_supply=0,
        )
        asyncio.run(reader.read_at_block(18_500_000))
        # The pool's getReserveData was called with block_identifier=...
        kwargs = reader.pool.functions.getReserveData.return_value.call.call_args.kwargs
        assert kwargs.get("block_identifier") == 18_500_000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
