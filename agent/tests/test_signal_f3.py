"""F3 live single-row wrapper tests (Plan E Task 5).

F3 = cross-protocol APR-spread + dispersion features (research-side
``F3FragmentationBuilder``). The wrapper exposes the
``f3_spread_top2`` column (max-minus-runner-up APR), which is the
primary "should we switch now?" indicator.

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
from decision.features.f3_fragmentation import F3FragmentationBuilder # noqa: E402
from signals.f3 import compute_f3, F3_OUTPUT_COL                      # noqa: E402


class FakeHistory:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def snapshot_df(self) -> pd.DataFrame:
        return self._df.copy()


def _make_state(block: int, aave_apr: float, comp_apr: float) -> BlockState:
    return BlockState(
        block_number=block,
        block_timestamp=pd.Timestamp(2_000_000_000 + block * 12, unit="s", tz="UTC"),
        protocols=("aave_v3", "compound_v3"),
        lending_apr={"aave_v3": aave_apr, "compound_v3": comp_apr},
        utilization={"aave_v3": 0.8, "compound_v3": 0.7},
        tvl_usd={"aave_v3": 1e9, "compound_v3": 5e8},
        current_protocol="aave_v3",
        position_usd=1.0,
        gas_price_gwei=25.0,
        eth_price_usd=3500.0,
        gas_used_estimate=200_000,
    )


def _make_history_df(n: int, start_block: int = 100,
                     aave_base: float = 0.04,
                     comp_base: float = 0.03) -> pd.DataFrame:
    rows = [
        {
            "block_number": start_block + i,
            "block_timestamp": pd.Timestamp(2_000_000_000 + (start_block + i) * 12, unit="s", tz="UTC"),
            "aave_v3_lending_apr": aave_base,
            "compound_v3_lending_apr": comp_base,
        }
        for i in range(n)
    ]
    return pd.DataFrame(rows)


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #
def test_f3_returns_finite_with_two_protocols():
    """Two protocols + finite APRs -> finite f3_spread_top2."""
    df = _make_history_df(200)
    state = _make_state(block=300, aave_apr=0.05, comp_apr=0.03)
    val = compute_f3(state=state, history=FakeHistory(df))
    assert isinstance(val, float)
    assert math.isfinite(val)


def test_f3_top2_equals_absolute_spread_for_two_protocols():
    """With exactly 2 protocols, f3_spread_top2 == |aave - comp|."""
    df = _make_history_df(10)
    state = _make_state(block=300, aave_apr=0.07, comp_apr=0.02)
    val = compute_f3(state=state, history=FakeHistory(df))
    # top - runner_up where top=0.07, runner_up=0.02 -> 0.05
    assert math.isclose(val, 0.05, abs_tol=1e-12)


def test_f3_zero_drift_against_research_builder():
    """SAME-SOURCE guarantee: wrapper value == F3FragmentationBuilder
    applied to the identical (snapshot + live-row) panel."""
    df = _make_history_df(50, aave_base=0.045, comp_base=0.028)
    state = _make_state(block=300, aave_apr=0.055, comp_apr=0.020)

    val_wrapper = compute_f3(state=state, history=FakeHistory(df))

    live_row = {
        "block_number": int(state.block_number),
        "block_timestamp": state.block_timestamp,
        "aave_v3_lending_apr": state.lending_apr["aave_v3"],
        "compound_v3_lending_apr": state.lending_apr["compound_v3"],
    }
    direct_panel = pd.concat([df, pd.DataFrame([live_row])], ignore_index=True)
    direct_out = F3FragmentationBuilder().build(direct_panel)
    val_direct = float(direct_out[F3_OUTPUT_COL].iloc[-1])

    assert val_wrapper == val_direct


def test_f3_dispatches_to_research_builder():
    """Patching ``_build_f3`` proves no private re-implementation."""
    df = _make_history_df(20)
    state = _make_state(block=300, aave_apr=0.04, comp_apr=0.03)

    fake_out = pd.DataFrame(
        {
            "block_timestamp": pd.to_datetime(
                [pd.Timestamp.utcnow()] * 2
            ).tz_localize(None).tz_localize("UTC"),
            F3_OUTPUT_COL: [np.nan, 0.0042],
        },
        index=pd.Index([0, 1], name="block_number"),
    )

    with patch("signals.f3._build_f3", return_value=fake_out) as mock:
        val = compute_f3(state=state, history=FakeHistory(df))
        mock.assert_called_once()
        passed_panel = mock.call_args.args[0]
        assert passed_panel["block_number"].iloc[-1] == state.block_number

    assert val == 0.0042


def test_f3_current_state_row_drives_value():
    """Changing the live APRs (with identical history) must change the
    F3 signal -- proves the live row is read by the builder."""
    df = _make_history_df(20, aave_base=0.04, comp_base=0.04)

    val_close = compute_f3(
        state=_make_state(block=300, aave_apr=0.04, comp_apr=0.04),
        history=FakeHistory(df),
    )
    val_wide = compute_f3(
        state=_make_state(block=300, aave_apr=0.09, comp_apr=0.01),
        history=FakeHistory(df),
    )
    assert val_close == 0.0  # identical APRs -> zero spread
    assert math.isclose(val_wide, 0.08, abs_tol=1e-12)
