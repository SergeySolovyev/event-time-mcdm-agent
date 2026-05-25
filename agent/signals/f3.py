"""F3 fragmentation signal: live single-row wrapper.

Delegates to the research-side ``F3FragmentationBuilder``
(cross-protocol APR spread + dispersion features, MacKenzie 2021
Table 3.2 "same instrument across multiple venues"). The wrapper
appends the current block's APR observations to a history snapshot,
runs the builder, and returns the last row's ``f3_spread_top2`` value
as a float -- the immediate-switching signal (max minus runner-up).

F3 requires at least 2 ``<proto>_lending_apr`` columns; with only one
protocol the builder raises ``ValueError``. With the live state this
is guaranteed by ``BlockState.protocols`` containing >= 2 entries in
all production scenarios (Aave + Compound minimum).
"""
from __future__ import annotations

import math

import pandas as pd

from decision.base import BlockState
from decision.features.f3_fragmentation import F3FragmentationBuilder

_BUILDER = F3FragmentationBuilder()

# Output column we surface as the F3 signal. ``f3_spread_top2`` is
# the post-switch advantage between the best and runner-up protocol --
# the primary "should we switch now?" indicator.
F3_OUTPUT_COL = "f3_spread_top2"


def _build_f3(panel: pd.DataFrame) -> pd.DataFrame:
    """Thin indirection so tests can patch this symbol."""
    return _BUILDER.build(panel)


def compute_f3(*, state: BlockState, history) -> float:
    """Return the F3 signal for the live block.

    Args:
        state: live ``BlockState`` for the current block.
        history: object exposing ``snapshot_df() -> pd.DataFrame``. The
            frame must already have columns ``block_number``,
            ``block_timestamp`` (tz-aware UTC), and one
            ``<proto>_lending_apr`` column per protocol in ``state``.

    Returns:
        Last-row ``f3_spread_top2`` as ``float``; ``float('nan')`` if
        the panel has all-NaN APRs (e.g. an empty/very-short history).
    """
    df = history.snapshot_df()

    live_row = {
        "block_number": int(state.block_number),
        "block_timestamp": state.block_timestamp,
    }
    for p in state.protocols:
        live_row[f"{p}_lending_apr"] = state.lending_apr[p]
    df = pd.concat([df, pd.DataFrame([live_row])], ignore_index=True)

    out = _build_f3(df)
    val = float(out[F3_OUTPUT_COL].iloc[-1])
    return val if not math.isnan(val) else float("nan")
