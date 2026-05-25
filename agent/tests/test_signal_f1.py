"""F1 live single-row wrapper tests (Plan E Task 5).

The wrapper MUST call the same research-side
``decision.features.f1_lead.F1LeadBuilder`` used by replay -- not a
private re-implementation. The zero-drift identity test (#3) is the
load-bearing assertion: it patches the builder out and verifies the
wrapper hands the live panel to it untouched.

Naming note: the agent-side package is ``signals/`` (plural) rather
than ``signal/`` because the stdlib already owns ``signal`` and would
shadow a top-level ``signal`` package once ``sys.path`` includes the
agent root.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import numpy as np

# Allow ``from signal.f1 import compute_f1`` against the agent repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from decision.base import BlockState                                  # noqa: E402
from decision.features.f1_lead import F1LeadBuilder                   # noqa: E402
from signals.f1 import compute_f1, F1_OUTPUT_COL                      # noqa: E402


class FakeHistory:
    """Minimal in-memory history that returns a snapshot DataFrame."""

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


def _make_history_df(n: int, start_block: int = 100) -> pd.DataFrame:
    rows = [
        {
            "block_number": start_block + i,
            "block_timestamp": pd.Timestamp(2_000_000_000 + (start_block + i) * 12, unit="s", tz="UTC"),
            "aave_v3_lending_apr": 0.04 + 0.001 * (i % 7),
            "compound_v3_lending_apr": 0.03 + 0.001 * (i % 5),
        }
        for i in range(n)
    ]
    return pd.DataFrame(rows)


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #
def test_f1_returns_nan_without_dsr_file():
    """Without a DSR parquet cached, every f1_dsr_* column is NaN by
    design, so the wrapper's signal is NaN. This is the documented
    contract -- the live agent has no DSR fallback, by symmetry with
    replay."""
    df = _make_history_df(200)
    state = _make_state(block=300, aave_apr=0.045, comp_apr=0.030)
    val = compute_f1(state=state, history=FakeHistory(df))
    assert isinstance(val, float)
    assert math.isnan(val)


def test_f1_returns_nan_with_short_history():
    """A single-row history still goes through the builder; without
    DSR everything is NaN."""
    df = _make_history_df(1)
    state = _make_state(block=2, aave_apr=0.04, comp_apr=0.03)
    val = compute_f1(state=state, history=FakeHistory(df))
    assert math.isnan(val)


def test_f1_zero_drift_against_research_builder():
    """SAME-SOURCE guarantee: the wrapper's value must equal
    F1LeadBuilder().build(...) applied to the same panel (with the
    live row appended)."""
    df = _make_history_df(50)
    state = _make_state(block=200, aave_apr=0.05, comp_apr=0.025)

    # Wrapper output.
    val_wrapper = compute_f1(state=state, history=FakeHistory(df))

    # Direct research-side call on the identical (snapshot + live-row) panel.
    live_row = {
        "block_number": int(state.block_number),
        "block_timestamp": state.block_timestamp,
        "aave_v3_lending_apr": state.lending_apr["aave_v3"],
        "compound_v3_lending_apr": state.lending_apr["compound_v3"],
    }
    direct_panel = pd.concat([df, pd.DataFrame([live_row])], ignore_index=True)
    direct_out = F1LeadBuilder().build(direct_panel)
    val_direct = float(direct_out[F1_OUTPUT_COL].iloc[-1])

    # Both NaN (no DSR) is the actual case here; assertion uses NaN-equal.
    if math.isnan(val_direct):
        assert math.isnan(val_wrapper)
    else:
        assert val_wrapper == val_direct


def test_f1_dispatches_to_research_builder():
    """The wrapper must call ``signal.f1._build_f1`` (which dispatches
    to F1LeadBuilder.build). Patching it proves no private copy."""
    df = _make_history_df(100)
    state = _make_state(block=300, aave_apr=0.04, comp_apr=0.03)

    fake_out = pd.DataFrame(
        {
            "block_timestamp": pd.to_datetime(
                [pd.Timestamp.utcnow()] * 2
            ).tz_localize(None).tz_localize("UTC"),
            F1_OUTPUT_COL: [np.nan, 0.0123],
        },
        index=pd.Index([0, 1], name="block_number"),
    )

    with patch("signals.f1._build_f1", return_value=fake_out) as mock:
        val = compute_f1(state=state, history=FakeHistory(df))
        mock.assert_called_once()
        # Confirm the panel passed in included the live row.
        passed_panel = mock.call_args.args[0]
        assert passed_panel["block_number"].iloc[-1] == state.block_number

    assert val == 0.0123


def test_f1_current_state_row_is_appended():
    """The live block must be the LAST row in the panel handed to the
    builder. We capture the panel via a sidecar patch."""
    df = _make_history_df(20)
    state = _make_state(block=999_999, aave_apr=0.07, comp_apr=0.02)

    captured: dict[str, pd.DataFrame] = {}
    real_builder = F1LeadBuilder()

    def _spy_build(panel: pd.DataFrame) -> pd.DataFrame:
        captured["panel"] = panel.copy()
        return real_builder.build(panel)

    with patch("signals.f1._build_f1", side_effect=_spy_build):
        compute_f1(state=state, history=FakeHistory(df))

    panel = captured["panel"]
    last = panel.iloc[-1]
    assert int(last["block_number"]) == 999_999
    assert last["aave_v3_lending_apr"] == 0.07
    assert last["compound_v3_lending_apr"] == 0.02
