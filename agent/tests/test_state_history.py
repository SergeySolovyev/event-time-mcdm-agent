"""Unit tests for ``state.history.HistoryStore`` (Plan E Task 6).

The store is the rolling per-block history backing T2's OUCalibrator
(>= 50 spreads), T3's hazard features F1/F3/F4 (>= 500 lags each), and
T5's live signal wrappers (which consume ``snapshot_df()``).

We do NOT depend on pytest-asyncio: each async test drives the surface
via ``asyncio.run(...)`` -- the same pattern the rest of the agent test
suite uses (T2 reader tests, T3 per-block-loop tests, T5 signal tests).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

# Allow ``from state.history import HistoryStore`` against the agent root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from decision.base import Action, BlockState  # noqa: E402
from state.history import HistoryStore        # noqa: E402


PROTOS = ("aave_v3", "compound_v3", "spark", "morpho", "fluid", "euler")


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #
def _state(block: int, *, current: str = "aave_v3") -> BlockState:
    """Construct a deterministic BlockState for the given block number.

    Per-protocol APRs are made block-dependent so we can detect FIFO
    truncation by inspecting the surviving values.
    """
    return BlockState(
        block_number=block,
        block_timestamp=pd.Timestamp("2026-05-25", tz="UTC") + pd.Timedelta(seconds=12 * block),
        protocols=PROTOS,
        lending_apr={p: 0.04 + 0.0001 * block for p in PROTOS},
        utilization={p: 0.7 for p in PROTOS},
        tvl_usd={p: 1.0e9 for p in PROTOS},
        current_protocol=current,
        position_usd=1_000_000.0,
        gas_price_gwei=25.0,
        eth_price_usd=3_500.0,
        gas_used_estimate=200_000,
    )


def _action(
    kind: str = "hold",
    target: str | None = None,
    rationale: str = "test",
) -> Action:
    return Action(kind=kind, target_protocol=target, rationale=rationale)


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #
def test_empty_store_len_is_zero(tmp_path: Path) -> None:
    store = HistoryStore(path=tmp_path / "h.parquet", max_rows=100)
    assert len(store) == 0
    snap = store.snapshot_df()
    assert isinstance(snap, pd.DataFrame)
    assert len(snap) == 0


def test_append_writes_parquet_file(tmp_path: Path) -> None:
    path = tmp_path / "h.parquet"
    store = HistoryStore(path=path, max_rows=100)
    asyncio.run(store.append(_state(100), _action()))

    assert path.exists()
    df = pd.read_parquet(path)
    assert len(df) == 1
    assert int(df["block_number"].iloc[0]) == 100


def test_snapshot_df_returns_rows_in_order(tmp_path: Path) -> None:
    store = HistoryStore(path=tmp_path / "h.parquet", max_rows=100)

    async def _run() -> None:
        for b in (200, 201, 202):
            await store.append(_state(b), _action())

    asyncio.run(_run())
    snap = store.snapshot_df()
    assert len(snap) == 3
    assert list(snap["block_number"]) == [200, 201, 202]


def test_window_truncates_to_max_rows(tmp_path: Path) -> None:
    """Append more than max_rows; oldest are dropped FIFO."""
    max_rows = 5_000
    store = HistoryStore(path=tmp_path / "h.parquet", max_rows=max_rows)

    # Drive 6,000 appends. We avoid actually writing 6,000 parquets to
    # keep the test fast (~1 minute otherwise) by patching the I/O step
    # to a no-op for the bulk loop, then doing one real write at the
    # end to confirm the on-disk state is also truncated.
    n_total = 6_000
    n_overflow = n_total - max_rows  # 1_000 rows must be dropped

    async def _run() -> None:
        # Patch the disk write to a no-op for speed; we re-enable it
        # for the final append below.
        original_write = store._write_atomic
        store._write_atomic = lambda df: None  # type: ignore[method-assign]
        try:
            for b in range(n_total - 1):
                await store.append(_state(b), _action())
        finally:
            store._write_atomic = original_write  # type: ignore[method-assign]
        # Final append performs a real write so we can also assert
        # on the on-disk truncation.
        await store.append(_state(n_total - 1), _action())

    asyncio.run(_run())

    assert len(store) == max_rows
    snap = store.snapshot_df()
    assert len(snap) == max_rows
    # The earliest surviving block_number is exactly n_overflow.
    assert int(snap["block_number"].iloc[0]) == n_overflow
    assert int(snap["block_number"].iloc[-1]) == n_total - 1

    # On-disk truncation matches in-memory truncation.
    df = pd.read_parquet(tmp_path / "h.parquet")
    assert len(df) == max_rows
    assert int(df["block_number"].iloc[0]) == n_overflow
    assert int(df["block_number"].iloc[-1]) == n_total - 1


def test_atomic_write_via_tmp_file_then_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write path goes ``to_parquet(tmp) -> fsync -> os.replace(tmp, path)``."""
    path = tmp_path / "h.parquet"
    expected_tmp = path.with_suffix(path.suffix + ".tmp")
    store = HistoryStore(path=path, max_rows=10)

    seen_replace_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _capturing_replace(src, dst):  # type: ignore[no-untyped-def]
        seen_replace_calls.append((os.fspath(src), os.fspath(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr("state.history.os.replace", _capturing_replace)

    asyncio.run(store.append(_state(300), _action()))

    assert len(seen_replace_calls) == 1
    src, dst = seen_replace_calls[0]
    assert Path(src) == expected_tmp
    assert Path(dst) == path
    # And the live file landed at the real path, not the .tmp path.
    assert path.exists()
    assert not expected_tmp.exists()


def test_atomic_write_survives_crash_mid_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after ``to_parquet(tmp)`` but BEFORE ``os.replace`` must
    leave the live parquet equal to its last-good contents."""
    path = tmp_path / "h.parquet"
    store = HistoryStore(path=path, max_rows=10)

    # One good write so the live path has known contents.
    asyncio.run(store.append(_state(400), _action()))
    good_df = pd.read_parquet(path)
    assert len(good_df) == 1
    assert int(good_df["block_number"].iloc[0]) == 400

    # Now simulate a crash: os.replace raises before the rename lands.
    def _exploding_replace(src, dst):  # type: ignore[no-untyped-def]
        # tmp must exist by the time we get here -- that's the whole
        # point of the crash-mid-write scenario.
        assert Path(src).exists(), "tmp file should exist before rename"
        raise OSError("simulated crash before rename")

    monkeypatch.setattr("state.history.os.replace", _exploding_replace)

    with pytest.raises(OSError, match="simulated crash"):
        asyncio.run(store.append(_state(401), _action()))

    # The live parquet is unchanged from the last-good write.
    df_after = pd.read_parquet(path)
    assert len(df_after) == 1
    assert int(df_after["block_number"].iloc[0]) == 400


def test_load_from_disk_restores_buffer(tmp_path: Path) -> None:
    """A fresh HistoryStore pointed at an existing parquet picks up the rows."""
    path = tmp_path / "h.parquet"
    store1 = HistoryStore(path=path, max_rows=100)

    async def _run() -> None:
        for b in (500, 501, 502, 503, 504):
            await store1.append(_state(b), _action())

    asyncio.run(_run())

    # Simulate process restart with a fresh store object.
    store2 = HistoryStore(path=path, max_rows=100)
    snap = store2.snapshot_df()
    assert len(store2) == 5
    assert list(snap["block_number"]) == [500, 501, 502, 503, 504]


def test_append_flattens_block_state_correctly(tmp_path: Path) -> None:
    """Per-protocol APR / utilization / tvl_usd surface as flat columns."""
    store = HistoryStore(path=tmp_path / "h.parquet", max_rows=10)
    asyncio.run(store.append(_state(600), _action()))

    df = pd.read_parquet(tmp_path / "h.parquet")
    for proto in PROTOS:
        assert f"{proto}_lending_apr" in df.columns
        assert f"{proto}_utilization" in df.columns
        assert f"{proto}_tvl_usd" in df.columns
        # Block-dependent APR formula from _state.
        assert float(df[f"{proto}_lending_apr"].iloc[0]) == pytest.approx(0.04 + 0.0001 * 600)
        assert float(df[f"{proto}_utilization"].iloc[0]) == pytest.approx(0.7)
        assert float(df[f"{proto}_tvl_usd"].iloc[0]) == pytest.approx(1.0e9)
    # Plus the top-level scalars.
    for col in (
        "block_number",
        "block_timestamp",
        "current_protocol",
        "position_usd",
        "gas_price_gwei",
        "eth_price_usd",
        "gas_used_estimate",
    ):
        assert col in df.columns


def test_append_records_action_fields(tmp_path: Path) -> None:
    """``action_kind`` / ``action_target`` / ``action_rationale`` are preserved."""
    store = HistoryStore(path=tmp_path / "h.parquet", max_rows=10)
    action = _action(kind="switch", target="morpho", rationale="best APR")
    asyncio.run(store.append(_state(700, current="aave_v3"), action))

    df = pd.read_parquet(tmp_path / "h.parquet")
    assert df["action_kind"].iloc[0] == "switch"
    assert df["action_target"].iloc[0] == "morpho"
    assert df["action_rationale"].iloc[0] == "best APR"


def test_concurrent_appends_dont_corrupt(tmp_path: Path) -> None:
    """``asyncio.gather`` of 100 appends -> exactly 100 rows, all block_numbers present.

    The internal ``asyncio.Lock`` serializes the read-modify-write of the
    buffer + disk write, so even when the event loop interleaves the
    coroutines no row is lost and no duplicate write races to disk.
    """
    store = HistoryStore(path=tmp_path / "h.parquet", max_rows=1_000)
    block_numbers = list(range(800, 900))  # 100 distinct blocks

    async def _run() -> None:
        await asyncio.gather(*(store.append(_state(b), _action()) for b in block_numbers))

    asyncio.run(_run())

    assert len(store) == 100
    snap = store.snapshot_df()
    # Every block_number we issued must be present exactly once.
    assert sorted(int(x) for x in snap["block_number"].tolist()) == sorted(block_numbers)

    # And the on-disk parquet is the same set.
    df = pd.read_parquet(tmp_path / "h.parquet")
    assert sorted(int(x) for x in df["block_number"].tolist()) == sorted(block_numbers)


def test_snapshot_df_is_defensive_copy(tmp_path: Path) -> None:
    """Mutating the snapshot must NOT corrupt the store's internal buffer."""
    store = HistoryStore(path=tmp_path / "h.parquet", max_rows=10)

    async def _run() -> None:
        for b in (900, 901, 902):
            await store.append(_state(b), _action())

    asyncio.run(_run())

    snap = store.snapshot_df()
    snap["block_number"] = -1

    snap2 = store.snapshot_df()
    assert (snap2["block_number"] > 0).all()
    assert list(snap2["block_number"]) == [900, 901, 902]
