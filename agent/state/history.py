"""Rolling per-block ``history.parquet`` store with atomic write.

Plan E Task 6.

The store keeps the last ``max_rows`` blocks in an in-memory pandas
``DataFrame``. Every :py:meth:`append` rewrites the entire window to a
``.tmp`` file, fsyncs the bytes to disk, and ``os.replace``\\ s the tmp
file over the live path. A crash mid-write therefore leaves the previous
good write intact (atomic-rename semantics on NTFS / POSIX same-fs).

Sized at 5,000 rows by default (~16.7 hours at 12 s/block), enough for:

* T2 ``OUCalibrator.fit`` (needs >= 50 spread observations, happy with
  >= 5000 for tight kappa-MLE CI).
* T3 hazard features F1 (EWMA spread), F3 (post-kink slope), F4
  (gas-adjusted advantage) -- each needs >= 500 lags.

The parquet schema is a flat per-block row:

    block_number, block_timestamp,
    <proto>_lending_apr, <proto>_utilization, <proto>_tvl_usd,  (one set per protocol)
    current_protocol, position_usd,
    gas_price_gwei, eth_price_usd, gas_used_estimate,
    action_kind, action_target, action_rationale

Interface notes
---------------
``append`` is declared ``async`` for interface symmetry with the rest of
the agent (the per-block loop and signal builders are all async). The
body is synchronous; the disk write is shipped to a worker thread via
``asyncio.to_thread`` so the event loop is not blocked.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pandas as pd

# Allow ``from decision.base import ...`` whether imported as
# ``state.history`` (via sys.path.insert on the agent root, as the tests
# do) or as ``agent.state.history`` from a future package layout.
_AGENT_ROOT = Path(__file__).resolve().parent.parent
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from decision.base import Action, BlockState  # noqa: E402


class HistoryStore:
    """Rolling parquet-backed history of per-block ``(state, action)``.

    Parameters
    ----------
    path:
        Destination parquet path. Created on first ``append``; the
        parent directory is created on construction if missing.
    max_rows:
        Maximum number of rows retained in the in-memory buffer (and
        therefore in the parquet on disk). Older rows are dropped
        FIFO once this length is exceeded. Default 5_000.
    columns:
        Optional explicit column order. Reserved for future use; the
        current implementation infers columns from each row dict.
    """

    DEFAULT_MAX_ROWS = 5_000

    def __init__(
        self,
        path: Path | str,
        max_rows: int = DEFAULT_MAX_ROWS,
        columns: list[str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.max_rows = int(max_rows)
        self.columns = list(columns) if columns is not None else None

        self.path.parent.mkdir(parents=True, exist_ok=True)

        # In-memory buffer is a list[dict]; we materialize a DataFrame
        # only at write time and in ``snapshot_df``. Using a list keeps
        # appends O(1) and avoids the quadratic cost of repeatedly
        # concatenating pandas DataFrames per block.
        self._rows: list[dict] = []
        self._lock = asyncio.Lock()

        self.load_from_disk()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def append(self, state: BlockState, action: Action) -> None:
        """Append one ``(state, action)`` row and atomically rewrite parquet.

        The signature matches the duck-typed call from
        :class:`per_block_loop.PerBlockLoop` (positional ``state, action``).
        ``async`` is for interface symmetry with the rest of the agent;
        the in-memory bookkeeping is synchronous and the actual disk
        write is dispatched to a worker thread so the event loop is not
        blocked by I/O.
        """
        async with self._lock:
            row = self._flatten(state, action)
            self._rows.append(row)
            if len(self._rows) > self.max_rows:
                # FIFO truncate: keep the last ``max_rows`` rows.
                self._rows = self._rows[-self.max_rows :]
            df = pd.DataFrame(self._rows)
            await asyncio.to_thread(self._write_atomic, df)

    def snapshot_df(self) -> pd.DataFrame:
        """Return a defensive copy of the in-memory buffer as a DataFrame.

        Mutating the returned DataFrame MUST NOT corrupt the store's
        internal buffer -- T5 signal wrappers consume this and the
        signal code path is allowed to mutate / re-index freely.
        """
        if not self._rows:
            return pd.DataFrame()
        return pd.DataFrame(self._rows).copy()

    def load_from_disk(self) -> None:
        """Restore the in-memory buffer from ``self.path`` if it exists.

        Tolerates a missing or unreadable file: in either case the
        buffer is left empty (the next ``append`` will write a fresh
        parquet from scratch).
        """
        if not self.path.exists():
            self._rows = []
            return
        try:
            df = pd.read_parquet(self.path)
        except Exception:  # noqa: BLE001 -- corrupted parquet -> start fresh
            self._rows = []
            return
        # ``DataFrame.to_dict(orient="records")`` preserves the row
        # order, which on disk is FIFO-truncated to ``<= max_rows`` by
        # construction.
        self._rows = df.to_dict(orient="records")
        if len(self._rows) > self.max_rows:
            self._rows = self._rows[-self.max_rows :]

    def __len__(self) -> int:
        return len(self._rows)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _flatten(state: BlockState, action: Action) -> dict:
        """Flatten a BlockState + Action into one flat dict (parquet row)."""
        row: dict = {
            "block_number": int(state.block_number),
            "block_timestamp": state.block_timestamp,
            "current_protocol": state.current_protocol if state.current_protocol is not None else "",
            "position_usd": float(state.position_usd),
            "gas_price_gwei": float(state.gas_price_gwei),
            "eth_price_usd": float(state.eth_price_usd),
            "gas_used_estimate": int(state.gas_used_estimate),
            "action_kind": action.kind,
            "action_target": action.target_protocol if action.target_protocol is not None else "",
            "action_rationale": action.rationale,
        }
        for proto in state.protocols:
            row[f"{proto}_lending_apr"] = float(state.lending_apr[proto])
            row[f"{proto}_utilization"] = float(state.utilization[proto])
            row[f"{proto}_tvl_usd"] = float(state.tvl_usd[proto])
        return row

    def _write_atomic(self, df: pd.DataFrame) -> None:
        """Atomic-rename write: tmp -> fsync -> os.replace -> live path.

        The fsync between ``to_parquet`` and ``os.replace`` is what
        makes this crash-safe: without it, the rename can land before
        the file's bytes hit disk and a power loss would leave the
        live path pointing at a zero-byte / partially-written file.
        """
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        df.to_parquet(tmp, index=False)
        # Flush the tmp file's bytes to disk before the rename. On
        # Windows ``os.fsync`` requires a writable handle, so we open
        # the file with ``os.O_RDWR`` rather than the read-only "rb"
        # mode that would raise ``OSError: [Errno 9] Bad file descriptor``.
        fd = os.open(tmp, os.O_RDWR)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
