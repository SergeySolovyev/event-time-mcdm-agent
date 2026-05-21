"""Base interface for per-protocol on-chain readers.

Each concrete protocol (Aave V3, Compound V3, Spark, Morpho, Fluid,
Euler V2, ...) ships a subclass of `ProtocolReader` that returns a
`ProtocolData` snapshot of the protocol's current state.  The agent
calls `read()` on every registered reader once per cycle.

Design notes:

* `ProtocolReader` owns its contract bindings; it is initialized once
  per agent process with a Web3 instance and the protocol's market
  address(es).  Subclasses may cache derived addresses (e.g. Aave's
  aToken / variableDebtToken) on first read.

* `ProtocolData` is the unified snapshot shape consumed by the MCDM
  scorer.  Every reader must populate ALL fields; if a value is
  unavailable for a given protocol the reader returns None / 0.0
  with a documented convention rather than silently substituting.

* Readers raise on RPC failure -- the outer `DataReader` catches and
  isolates per-protocol failures.  Do not swallow exceptions inside
  a reader; that would mask data-quality issues from the operator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from web3 import Web3


@dataclass
class ProtocolData:
    """Unified snapshot returned by every protocol reader.

    Fields:
        name:           Human-readable protocol label (e.g. "Aave V3").
        adapter_index:  Index of the on-chain adapter in the vault's
                        AdapterRegistry; the MCDM scorer and the
                        rebalance tx both key off this.
        apy:            Annualized supply rate as a decimal (e.g.
                        0.05 = 5% APY).
        utilization:    Pool utilization in [0, 1] = totalBorrowed /
                        totalSupplied.
        tvl:            Total supplied in underlying-asset units
                        (e.g. USDC -- aToken total supply / 1e6).
        raw_rate_1e18:  Raw rate in 1e18 scale, retained for EMA
                        smoothing arithmetic in main.py.
    """

    name: str
    adapter_index: int
    apy: float
    utilization: float
    tvl: float
    raw_rate_1e18: int


class ProtocolReader(ABC):
    """Abstract on-chain reader for one lending protocol."""

    #: Human-readable label, set by subclasses.
    NAME: str = "unknown"

    #: Adapter index in the vault registry.  Set by subclasses or
    #: passed via __init__.
    ADAPTER_INDEX: int = -1

    def __init__(self, w3: Web3):
        self.w3 = w3

    @abstractmethod
    def read(self) -> ProtocolData:
        """Return a fresh ProtocolData snapshot for this protocol."""
        raise NotImplementedError
