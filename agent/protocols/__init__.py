"""Per-protocol on-chain readers.

Each protocol gets a focused subclass of `ProtocolReader` so that
adding a new market (Spark, Morpho, Fluid, Euler V2, etc.) is a
single self-contained file + one entry in the registry.

The top-level `DataReader` orchestrates these readers and applies
cross-cutting concerns (per-protocol isolation, stale-data check).
"""

from .base import ProtocolReader, ProtocolData
from .aave import AaveV3Reader
from .compound import CompoundV3Reader

__all__ = [
    "ProtocolReader",
    "ProtocolData",
    "AaveV3Reader",
    "CompoundV3Reader",
]
