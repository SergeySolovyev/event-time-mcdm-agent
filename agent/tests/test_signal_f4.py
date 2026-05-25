"""F4 live single-row wrapper tests (Plan E Task 5).

F4 = gas regime + stablecoin peg deviations (research-side
``F4RelatedBuilder``). The wrapper exposes ``f4_gas_log10`` -- the
log10-transformed current gas price -- which is robust to right-skew
spikes and does not require the 30-day quantile window.

Naming note: agent-side package is ``signals/`` (plural) -- stdlib
owns ``signal``.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from decision.base import BlockState                                  # noqa: E402
from decision.features.f4_related import F4RelatedBuilder             # noqa: E402
from signals.f4 import compute_f4, F4_OUTPUT_COL                      # noqa: E402


class FakeHistory:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def snapshot_df(self) -> pd.DataFrame:
        return self._df.copy()


def _make_state(block: int, gas_gwei: float = 25.0) -> BlockState:
    return BlockState(
        block_number=block,
        block_timestamp=pd.Timestamp(2_000_000_000 + block * 12, unit="s", tz="UTC"),
        protocols=("aave_v3", "compound_v3"),
        lending_apr={"aave_v3": 0.05, "compound_v3": 0.03},
        utilization={"aave_v3": 0.8, "compound_v3": 0.7},
        tvl_usd={"aave_v3": 1e9, "compound_v3": 5e8},
        current_protocol="compound_v3",
        position_usd=1_000_000.0,
        gas_price_gwei=gas_gwei,
        eth_price_usd=3500.0,
        gas_used_estimate=200_000,
    )


def _make_history_df(n: int, gas: float = 25.0,
                     start_block: int = 100) -> pd.DataFrame:
    rows = [
        {
            "block_number": start_block + i,
            "block_timestamp": pd.Timestamp(2_000_000_000 + (start_block + i) * 12, unit="s", tz="UTC"),
            "gas_price_gwei": gas,
            "aave_v3_lending_apr": 0.05,
            "compound_v3_lending_apr": 0.03,
        }
        for i in range(n)
    ]
    return pd.DataFrame(rows)


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #
def test_f4_returns_finite_log10_with_finite_gas():
    """gas=25 gwei -> log10(25) ~ 1.398."""
    df = _make_history_df(50, gas=25.0)
    state = _make_state(block=300, gas_gwei=25.0)
    val = compute_f4(state=state, history=FakeHistory(df))
    assert isinstance(val, float)
    assert math.isclose(val, math.log10(25.0), rel_tol=1e-9)


def test_f4_responds_to_live_gas():
    """Doubling live gas shifts log10 by log10(2) ~ 0.301."""
    df = _make_history_df(20, gas=25.0)
    val_low = compute_f4(state=_make_state(300, gas_gwei=25.0), history=FakeHistory(df))
    val_high = compute_f4(state=_make_state(300, gas_gwei=50.0), history=FakeHistory(df))
    assert math.isclose(val_high - val_low, math.log10(2.0), rel_tol=1e-9)


def test_f4_zero_drift_against_research_builder():
    """SAME-SOURCE guarantee: wrapper value == F4RelatedBuilder
    applied to the identical (snapshot + live-row) panel."""
    df = _make_history_df(50, gas=30.0)
    state = _make_state(block=300, gas_gwei=42.5)

    val_wrapper = compute_f4(state=state, history=FakeHistory(df))

    live_row = {
        "block_number": int(state.block_number),
        "block_timestamp": state.block_timestamp,
        "gas_price_gwei": state.gas_price_gwei,
        "aave_v3_lending_apr": state.lending_apr["aave_v3"],
        "compound_v3_lending_apr": state.lending_apr["compound_v3"],
    }
    direct_panel = pd.concat([df, pd.DataFrame([live_row])], ignore_index=True)
    direct_out = F4RelatedBuilder().build(direct_panel)
    val_direct = float(direct_out[F4_OUTPUT_COL].iloc[-1])

    assert val_wrapper == val_direct


def test_f4_dispatches_to_research_builder():
    """Patching ``_build_f4`` proves no private re-implementation."""
    df = _make_history_df(20)
    state = _make_state(block=300, gas_gwei=25.0)

    fake_out = pd.DataFrame(
        {
            "block_timestamp": pd.to_datetime(
                [pd.Timestamp.utcnow()] * 2
            ).tz_localize(None).tz_localize("UTC"),
            F4_OUTPUT_COL: [np.nan, 1.234],
        },
        index=pd.Index([0, 1], name="block_number"),
    )

    with patch("signals.f4._build_f4", return_value=fake_out) as mock:
        val = compute_f4(state=state, history=FakeHistory(df))
        mock.assert_called_once()
        passed_panel = mock.call_args.args[0]
        assert passed_panel["block_number"].iloc[-1] == state.block_number
        assert passed_panel["gas_price_gwei"].iloc[-1] == state.gas_price_gwei

    assert val == 1.234


def test_f4_log10_floor_protects_against_zero_gas():
    """log10(0) is -inf; the builder floors gas at 1e-3 so log10 stays
    bounded. Identity holds for a zero-gas live row."""
    df = _make_history_df(5, gas=0.0)
    state = _make_state(block=300, gas_gwei=0.0)
    val = compute_f4(state=state, history=FakeHistory(df))
    assert math.isclose(val, math.log10(1e-3), rel_tol=1e-9)
