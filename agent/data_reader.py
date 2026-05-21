"""On-chain data reader: thin orchestrator over per-protocol readers.

This module owns the cross-cutting concerns -- the per-protocol error
isolation, the TVL-delta history, the gas-price / block-age helpers --
while delegating each protocol's read mechanics to a dedicated
`ProtocolReader` subclass in `agent/protocols/`.

Adding a new protocol is therefore:

  1. Create `agent/protocols/<P>.py` with a `<P>Reader(ProtocolReader)`
     subclass implementing `read()`.
  2. Register a `<P>Reader(...)` instance with this `DataReader`
     (either via `register()` or by extending `build_default_readers()`).

The legacy module-level constants (`RAY`, `SECONDS_PER_YEAR`,
`USDC_DECIMALS`) are re-exported for backward compatibility with the
existing test suite.
"""

from __future__ import annotations

import logging

from web3 import Web3

import config
from protocols import AaveV3Reader, CompoundV3Reader, ProtocolData, ProtocolReader

# Re-exports for backward compatibility with tests / external callers.
RAY = 10**27
SECONDS_PER_YEAR = 365.25 * 24 * 3600
SCALE_18 = 10**18
USDC_DECIMALS = 10**6


log = logging.getLogger("ai-vault-agent.reader")


def build_default_readers(w3: Web3) -> list[ProtocolReader]:
    """The standard 2-protocol agent: Aave V3 + Compound V3 on USDC.

    Extension point for N-way: append `SparkReader(...)`,
    `MorphoBlueReader(...)`, etc. as those protocols ship.
    """
    return [
        AaveV3Reader(
            w3,
            pool_address=config.AAVE_POOL_ADDRESS,
            asset_address=config.USDC_ADDRESS,
            adapter_index=0,
        ),
        CompoundV3Reader(
            w3,
            comet_address=config.COMPOUND_COMET_ADDRESS,
            adapter_index=1,
        ),
    ]


class DataReader:
    """Reads on-chain data from a registered set of `ProtocolReader`s.

    The two convenience attributes `_aave_atoken` and
    `_aave_variable_debt` are exposed for backward compatibility with
    the Tier-1 test suite, which probes the Aave reader's lazy-binding
    cache.  New tests should target the per-protocol readers directly.
    """

    def __init__(self, w3: Web3, readers: list[ProtocolReader] | None = None):
        self.w3 = w3
        self._readers: list[ProtocolReader] = (
            readers if readers is not None else build_default_readers(w3)
        )
        self._prev_tvl: dict[int, float] = {}

        # ---- backward-compat shims -----------------------------------
        # Tests written against the old 2-protocol DataReader probe these
        # attributes directly.  Bind them to the corresponding readers.
        self.aave_pool = None
        self.comet = None
        self._aave_atoken = None
        self._aave_variable_debt = None
        for r in self._readers:
            if isinstance(r, AaveV3Reader):
                self.aave_pool = r.pool
                # _atoken/_variable_debt are populated lazily on first read;
                # we expose them via property-like access so tests that pre-
                # set them on the DataReader feed through to the reader.
                self._link_aave_compat(r)
            elif isinstance(r, CompoundV3Reader):
                self.comet = r.comet
        # ---------------------------------------------------------------

    def _link_aave_compat(self, reader: AaveV3Reader) -> None:
        """Bidirectional bridge: setting `self._aave_atoken` on the
        DataReader updates the underlying AaveV3Reader's lazy cache.
        Used only by the legacy test scaffolding.
        """
        self._aave_reader = reader

    def __setattr__(self, name, value):
        # Forward legacy `_aave_atoken` / `_aave_variable_debt` writes to
        # the AaveV3Reader's internal cache so test fixtures continue to
        # work after the refactor.
        if name == "_aave_atoken" and hasattr(self, "_aave_reader"):
            self._aave_reader._atoken = value
        elif name == "_aave_variable_debt" and hasattr(self, "_aave_reader"):
            self._aave_reader._variable_debt = value
        super().__setattr__(name, value)

    # -------------------------------------------------------------------

    def register(self, reader: ProtocolReader) -> None:
        """Add a protocol reader at runtime.  Used by tests and by
        future N-way config-driven setups."""
        self._readers.append(reader)

    @property
    def readers(self) -> list[ProtocolReader]:
        return list(self._readers)

    def read_all(self) -> list[ProtocolData]:
        """Read every registered protocol; isolate per-protocol failures."""
        results: list[ProtocolData] = []
        for reader in self._readers:
            try:
                results.append(reader.read())
            except Exception as exc:                                  # noqa: BLE001
                log.error(
                    f"read_all: {reader.NAME} failed -- skipping this cycle: {exc}"
                )
        return results

    # ---- legacy shims so existing tests keep working ------------------

    def _read_aave(self) -> ProtocolData:
        """Legacy alias for the AaveV3 reader."""
        for r in self._readers:
            if isinstance(r, AaveV3Reader):
                return r.read()
        raise RuntimeError("No AaveV3Reader registered")

    def _read_compound(self) -> ProtocolData:
        """Legacy alias for the CompoundV3 reader."""
        for r in self._readers:
            if isinstance(r, CompoundV3Reader):
                return r.read()
        raise RuntimeError("No CompoundV3Reader registered")

    # -------------------------------------------------------------------

    def get_tvl_delta(self, protocol: ProtocolData) -> float:
        """Fractional TVL change since the last call for this adapter."""
        idx = protocol.adapter_index
        prev = self._prev_tvl.get(idx)
        self._prev_tvl[idx] = protocol.tvl
        if prev is None or prev == 0:
            return 0.0
        return (protocol.tvl - prev) / prev

    def get_gas_price(self) -> int:
        """Current gas price in wei."""
        return self.w3.eth.gas_price

    def block_age_seconds(self) -> float:
        """Wall-clock age of the latest block.  See Tier-2 robustness."""
        import time as _t
        try:
            latest = self.w3.eth.get_block("latest")
        except Exception:
            return float("inf")
        block_ts = latest["timestamp"]
        return max(0.0, _t.time() - block_ts)
