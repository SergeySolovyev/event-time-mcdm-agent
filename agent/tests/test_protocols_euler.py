"""Unit tests for EulerV2Reader (Plan E Task 2).

Euler V2 EVK vaults expose ``interestRate()`` (RAY-scaled, per-second),
``totalAssets()`` and ``totalBorrows()``.  The vault address comes from
the ``EULER_USDC_VAULT`` env var; tests cover both the env-driven and
the explicit-arg construction paths.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from protocols.base import ProtocolData                                # noqa: E402
from protocols.euler import (                                          # noqa: E402
    RAY,
    SECONDS_PER_YEAR,
    USDC_DECIMALS,
    EulerV2Reader,
)


def _fn(return_value):
    fn = MagicMock()
    fn.call = MagicMock(return_value=return_value)
    return fn


def _make_w3(per_second_ray: int, total_assets: int, total_borrows: int):
    vault_mock = MagicMock()
    vault_mock.functions = MagicMock()
    vault_mock.functions.interestRate = MagicMock(
        return_value=_fn(per_second_ray)
    )
    vault_mock.functions.totalAssets = MagicMock(
        return_value=_fn(total_assets)
    )
    vault_mock.functions.totalBorrows = MagicMock(
        return_value=_fn(total_borrows)
    )

    w3 = MagicMock()
    w3.eth.contract = MagicMock(return_value=vault_mock)
    w3.eth.block_number = 19_000_000
    return w3


def _build_reader(per_second_ray: int, total_assets: int, total_borrows: int):
    w3 = _make_w3(per_second_ray, total_assets, total_borrows)
    return EulerV2Reader(w3, vault_address="0x" + "ee" * 20)


class TestEulerEnvVar:
    def test_missing_env_raises_value_error(self, monkeypatch):
        """No vault address + no env var -> ValueError."""
        monkeypatch.delenv("EULER_USDC_VAULT", raising=False)
        w3 = _make_w3(0, 0, 0)
        with pytest.raises(ValueError, match="EULER_USDC_VAULT"):
            EulerV2Reader(w3)

    def test_env_var_provides_vault_address(self, monkeypatch):
        """If env is set, no explicit address is required."""
        monkeypatch.setenv("EULER_USDC_VAULT", "0x" + "ee" * 20)
        w3 = _make_w3(
            per_second_ray=int(0.05 * RAY / SECONDS_PER_YEAR),
            total_assets=1000 * USDC_DECIMALS,
            total_borrows=300 * USDC_DECIMALS,
        )
        reader = EulerV2Reader(w3)
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert snap.name == "Euler V2"


class TestEulerReadAtBlock:
    def test_returns_protocol_data_with_euler_name(self):
        reader = _build_reader(
            per_second_ray=int(0.05 * RAY / SECONDS_PER_YEAR),
            total_assets=1000 * USDC_DECIMALS,
            total_borrows=300 * USDC_DECIMALS,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert isinstance(snap, ProtocolData)
        assert snap.name == "Euler V2"

    def test_apy_annualized_from_ray_per_second(self):
        """A per-second RAY rate annualizing to 5% must yield apy ~= 0.05."""
        per_sec_ray = int(0.05 * RAY / SECONDS_PER_YEAR)
        reader = _build_reader(
            per_second_ray=per_sec_ray,
            total_assets=1000 * USDC_DECIMALS,
            total_borrows=400 * USDC_DECIMALS,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert 0.0 <= snap.apy <= 1.0
        assert abs(snap.apy - 0.05) < 1e-3

    def test_utilization_in_unit_interval(self):
        reader = _build_reader(
            per_second_ray=0,
            total_assets=1000 * USDC_DECIMALS,
            total_borrows=600 * USDC_DECIMALS,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert 0.0 <= snap.utilization <= 1.0
        assert abs(snap.utilization - 0.60) < 1e-9

    def test_tvl_positive_in_underlying_units(self):
        reader = _build_reader(
            per_second_ray=0,
            total_assets=7_500 * USDC_DECIMALS,
            total_borrows=0,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert snap.tvl > 0
        assert snap.tvl == 7_500.0

    def test_raw_rate_1e18_positive(self):
        per_sec_ray = int(0.05 * RAY / SECONDS_PER_YEAR)
        reader = _build_reader(
            per_second_ray=per_sec_ray,
            total_assets=1000 * USDC_DECIMALS,
            total_borrows=0,
        )
        snap = asyncio.run(reader.read_at_block(19_000_000))
        assert snap.raw_rate_1e18 > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
