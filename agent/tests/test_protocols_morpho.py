"""Unit tests for MorphoBlueReader (Plan E Task 2).

Morpho Blue's market state is fetched via ``market(bytes32)``; the
per-second borrow rate comes from the AdaptiveCurveIRM the market is
configured with.  Tests mock both the Morpho contract and the IRM.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from protocols import morpho as morpho_mod                             # noqa: E402
from protocols.base import ProtocolData                                # noqa: E402
from protocols.morpho import (                                         # noqa: E402
    SCALE_18,
    SECONDS_PER_YEAR,
    USDC_DECIMALS,
    MorphoBlueReader,
)


MARKET_ID = b"\x11" * 32


def _fn(return_value):
    fn = MagicMock()
    fn.call = MagicMock(return_value=return_value)
    return fn


def _build_reader(
    total_supply_assets: int,
    total_borrow_assets: int,
    per_second_rate_wad: int,
):
    morpho_mock = MagicMock()
    morpho_mock.functions = MagicMock()
    morpho_mock.functions.market = MagicMock(
        return_value=_fn(
            (
                total_supply_assets,
                0,
                total_borrow_assets,
                0,
                0,
                0,
            )
        )
    )
    morpho_mock.functions.idToMarketParams = MagicMock(
        return_value=_fn(
            (
                "0x" + "aa" * 20,  # loanToken
                "0x" + "bb" * 20,  # collateralToken
                "0x" + "cc" * 20,  # oracle
                "0x" + "dd" * 20,  # irm
                0,                 # lltv
            )
        )
    )

    irm_mock = MagicMock()
    irm_mock.functions = MagicMock()
    irm_mock.functions.borrowRateView = MagicMock(
        return_value=_fn(per_second_rate_wad)
    )

    contract_queue = [morpho_mock, irm_mock]
    w3 = MagicMock()
    w3.eth.contract = MagicMock(side_effect=lambda **kw: contract_queue.pop(0))
    w3.eth.block_number = 19_000_000

    return MorphoBlueReader(w3, market_id=MARKET_ID)


class TestMorphoReadAtBlock:
    def test_returns_protocol_data_with_morpho_name(self):
        reader = _build_reader(
            total_supply_assets=1000 * USDC_DECIMALS,
            total_borrow_assets=500 * USDC_DECIMALS,
            per_second_rate_wad=int(0.05 * SCALE_18 / SECONDS_PER_YEAR),
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert isinstance(snap, ProtocolData)
        assert snap.name == "Morpho Blue"

    def test_apy_annualized_from_per_second_wad(self):
        """A per-second rate that annualizes to 5% must yield apy ~= 0.05."""
        per_sec = int(0.05 * SCALE_18 / SECONDS_PER_YEAR)
        reader = _build_reader(
            total_supply_assets=1000 * USDC_DECIMALS,
            total_borrow_assets=400 * USDC_DECIMALS,
            per_second_rate_wad=per_sec,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert 0.0 <= snap.apy <= 1.0
        assert abs(snap.apy - 0.05) < 1e-3

    def test_utilization_in_unit_interval(self):
        reader = _build_reader(
            total_supply_assets=1000 * USDC_DECIMALS,
            total_borrow_assets=650 * USDC_DECIMALS,
            per_second_rate_wad=0,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert 0.0 <= snap.utilization <= 1.0
        assert abs(snap.utilization - 0.65) < 1e-9

    def test_tvl_and_raw_rate_positive(self):
        reader = _build_reader(
            total_supply_assets=42_000 * USDC_DECIMALS,
            total_borrow_assets=21_000 * USDC_DECIMALS,
            per_second_rate_wad=int(0.04 * SCALE_18 / SECONDS_PER_YEAR),
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert snap.tvl > 0
        assert snap.raw_rate_1e18 > 0
        assert snap.tvl == 42_000.0

    def test_invalid_market_id_length_rejected(self):
        w3 = MagicMock()
        w3.eth.contract = MagicMock(return_value=MagicMock())
        with pytest.raises(ValueError, match="32 bytes"):
            MorphoBlueReader(w3, market_id=b"\x00" * 16)

    def test_module_docstring_mentions_adaptive_curve_deviation(self):
        """Plan E required the docstring to flag the AdaptiveCurve IRM deviation."""
        doc = inspect.getdoc(morpho_mod) or ""
        assert "AdaptiveCurve" in doc
        # The deviation note explicitly references the missing f_kink.
        assert "f_kink" in doc or "kink" in doc.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
