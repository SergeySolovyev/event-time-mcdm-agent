"""Unit tests for FluidReader (Plan E Task 2).

Fluid's ``getOverallTokenData`` view returns a 4-tuple
(supplyRate, borrowRate, totalSupply, totalBorrow), with WAD-scaled
per-second rates.  Tests mock the contract directly.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from protocols.base import ProtocolData                                # noqa: E402
from protocols.fluid import (                                          # noqa: E402
    FLUID_LIQUIDITY_ADDRESS,
    SCALE_18,
    SECONDS_PER_YEAR,
    USDC_DECIMALS,
    FluidReader,
)


def _fn(return_value):
    fn = MagicMock()
    fn.call = MagicMock(return_value=return_value)
    return fn


def _build_reader(
    supply_rate_wad: int,
    total_supply: int,
    total_borrow: int,
):
    liquidity_mock = MagicMock()
    liquidity_mock.functions = MagicMock()
    liquidity_mock.functions.getOverallTokenData = MagicMock(
        return_value=_fn((supply_rate_wad, 0, total_supply, total_borrow))
    )

    w3 = MagicMock()
    w3.eth.contract = MagicMock(return_value=liquidity_mock)
    w3.eth.block_number = 19_000_000

    return FluidReader(
        w3,
        token_address="0x" + "aa" * 20,
        liquidity_address=FLUID_LIQUIDITY_ADDRESS,
    )


class TestFluidReadAtBlock:
    def test_returns_protocol_data_with_fluid_name(self):
        reader = _build_reader(
            supply_rate_wad=int(0.04 * SCALE_18 / SECONDS_PER_YEAR),
            total_supply=1000 * USDC_DECIMALS,
            total_borrow=500 * USDC_DECIMALS,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert isinstance(snap, ProtocolData)
        assert snap.name == "Fluid"

    def test_apy_in_unit_interval_and_annualized(self):
        per_sec = int(0.04 * SCALE_18 / SECONDS_PER_YEAR)
        reader = _build_reader(
            supply_rate_wad=per_sec,
            total_supply=1000 * USDC_DECIMALS,
            total_borrow=600 * USDC_DECIMALS,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert 0.0 <= snap.apy <= 1.0
        assert abs(snap.apy - 0.04) < 1e-3

    def test_utilization_from_totals(self):
        reader = _build_reader(
            supply_rate_wad=0,
            total_supply=1000 * USDC_DECIMALS,
            total_borrow=750 * USDC_DECIMALS,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert 0.0 <= snap.utilization <= 1.0
        assert abs(snap.utilization - 0.75) < 1e-9

    def test_utilization_zero_when_no_supply(self):
        reader = _build_reader(
            supply_rate_wad=0,
            total_supply=0,
            total_borrow=0,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert snap.utilization == 0.0

    def test_utilization_clamped_to_one_when_overdrawn(self):
        reader = _build_reader(
            supply_rate_wad=0,
            total_supply=1000 * USDC_DECIMALS,
            total_borrow=1500 * USDC_DECIMALS,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert snap.utilization == 1.0

    def test_tvl_and_raw_rate_positive(self):
        reader = _build_reader(
            supply_rate_wad=int(0.04 * SCALE_18 / SECONDS_PER_YEAR),
            total_supply=10_000 * USDC_DECIMALS,
            total_borrow=4_000 * USDC_DECIMALS,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert snap.tvl > 0
        assert snap.tvl == 10_000.0
        assert snap.raw_rate_1e18 > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
