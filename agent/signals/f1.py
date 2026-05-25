"""F1 lead-rate signal: live single-row wrapper.

Delegates to the research-side ``F1LeadBuilder`` (DSR + sDAI / Curve
3pool lead-rate proxies, MacKenzie 2021 Table 3.2 "futures lead"). The
wrapper appends the current block's APR observations to a history
snapshot, runs the builder, and returns the last row's
``f1_lead_spread_dsr_vs_top`` value as a float.

Notes:
    * If the DSR cached parquet is missing, every ``f1_dsr_*`` column
      is NaN-by-design and this wrapper returns ``float('nan')``. That
      is the documented contract -- the live agent has no fallback
      for an absent DSR snapshot, by symmetry with replay.
    * The history snapshot must already include ``block_timestamp``
      (tz-aware UTC) and ``<proto>_lending_apr`` columns -- this is
      what ``HistoryStore.snapshot_df()`` returns in Plan E Task 6.
"""
from __future__ import annotations

import math

import pandas as pd

from decision.base import BlockState
from decision.features.f1_lead import F1LeadBuilder

# Module-level instance so monkey-patching ``_BUILDER.build`` in tests
# is straightforward. The builder is stateless w.r.t. live calls
# (only the DSR file path is a constructor arg).
_BUILDER = F1LeadBuilder()

# The single output column we surface as the F1 signal. Defined here
# (not inline) so the test suite can introspect what was selected.
F1_OUTPUT_COL = "f1_lead_spread_dsr_vs_top"


def _build_f1(panel: pd.DataFrame) -> pd.DataFrame:
    """Thin indirection so tests can patch this symbol."""
    return _BUILDER.build(panel)


def compute_f1(*, state: BlockState, history) -> float:
    """Return the F1 signal for the live block.

    Args:
        state: live ``BlockState`` for the current block.
        history: object exposing ``snapshot_df() -> pd.DataFrame``. The
            frame must already have columns ``block_number``,
            ``block_timestamp`` (tz-aware UTC), and one
            ``<proto>_lending_apr`` column per protocol in ``state``.

    Returns:
        Last-row ``f1_lead_spread_dsr_vs_top`` as ``float``;
        ``float('nan')`` if the panel is too short or DSR data is
        missing (the research-side builder returns NaN in either case).
    """
    df = history.snapshot_df()

    # Append the live block as the most-recent row.
    live_row = {
        "block_number": int(state.block_number),
        "block_timestamp": state.block_timestamp,
    }
    for p in state.protocols:
        live_row[f"{p}_lending_apr"] = state.lending_apr[p]
    df = pd.concat([df, pd.DataFrame([live_row])], ignore_index=True)

    out = _build_f1(df)
    val = float(out[F1_OUTPUT_COL].iloc[-1])
    return val if not math.isnan(val) else float("nan")
