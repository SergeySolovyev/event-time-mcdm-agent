"""F4 related-instrument signal: live single-row wrapper.

Delegates to the research-side ``F4RelatedBuilder`` (gas regime +
stablecoin peg deviations, MacKenzie 2021 Table 3.2 "correlated
instruments"). The wrapper appends the current block's gas / peg /
APR observations to a history snapshot, runs the builder, and returns
the last row's ``f4_gas_log10`` value as a float -- a robust,
right-skew-tame summary of the current gas regime.

F4 tolerates missing input columns: any of ``gas_price_gwei``,
``eth_usd``, ``usdc_peg``, ``usdt_peg`` absent from the panel emits a
``UserWarning`` and the corresponding output column is NaN. The live
``BlockState`` always provides ``gas_price_gwei``, so ``f4_gas_log10``
is finite whenever gas is finite.
"""
from __future__ import annotations

import math

import pandas as pd

from decision.base import BlockState
from decision.features.f4_related import F4RelatedBuilder

_BUILDER = F4RelatedBuilder()

# Output column we surface as the F4 signal. ``f4_gas_log10`` is a
# log10-transformed gas price that tames the heavy right tail (a 1000
# gwei spike doesn't dominate the feature scale). The quantile-rank
# alternative requires 30 days of history and would be NaN in the
# live agent's warmup window.
F4_OUTPUT_COL = "f4_gas_log10"


def _build_f4(panel: pd.DataFrame) -> pd.DataFrame:
    """Thin indirection so tests can patch this symbol."""
    return _BUILDER.build(panel)


def compute_f4(*, state: BlockState, history) -> float:
    """Return the F4 signal for the live block.

    Args:
        state: live ``BlockState`` for the current block.
        history: object exposing ``snapshot_df() -> pd.DataFrame``. The
            frame must already have ``block_number``, ``block_timestamp``
            (tz-aware UTC), and ``gas_price_gwei``.

    Returns:
        Last-row ``f4_gas_log10`` as ``float``; ``float('nan')`` if
        gas is NaN.
    """
    df = history.snapshot_df()

    live_row = {
        "block_number": int(state.block_number),
        "block_timestamp": state.block_timestamp,
        "gas_price_gwei": state.gas_price_gwei,
    }
    for p in state.protocols:
        live_row[f"{p}_lending_apr"] = state.lending_apr[p]
    df = pd.concat([df, pd.DataFrame([live_row])], ignore_index=True)

    out = _build_f4(df)
    val = float(out[F4_OUTPUT_COL].iloc[-1])
    return val if not math.isnan(val) else float("nan")
