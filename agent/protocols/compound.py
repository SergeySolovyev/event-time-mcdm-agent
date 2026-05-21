"""Compound V3 (Comet) reader.

Comet exposes `getUtilization()` and `getSupplyRate(utilization)` as
view functions; the supply rate is per-second and must be annualized
by multiplying by SECONDS_PER_YEAR.  TVL is the base-asset
totalSupply scaled by USDC decimals.
"""

from __future__ import annotations

from web3 import Web3

from .base import ProtocolData, ProtocolReader


SCALE_18 = 10**18
USDC_DECIMALS = 10**6
SECONDS_PER_YEAR = 365.25 * 24 * 3600

COMET_ABI = [
    {
        "inputs": [],
        "name": "getUtilization",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "utilization", "type": "uint256"}],
        "name": "getSupplyRate",
        "outputs": [{"name": "", "type": "uint64"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class CompoundV3Reader(ProtocolReader):
    NAME = "Compound V3"
    ADAPTER_INDEX = 1

    def __init__(self, w3: Web3, comet_address: str, adapter_index: int = 1):
        super().__init__(w3)
        self.ADAPTER_INDEX = adapter_index
        self.comet = w3.eth.contract(
            address=Web3.to_checksum_address(comet_address),
            abi=COMET_ABI,
        )

    def read(self) -> ProtocolData:
        utilization_raw = self.comet.functions.getUtilization().call()
        utilization = utilization_raw / SCALE_18

        per_second_rate = self.comet.functions.getSupplyRate(utilization_raw).call()
        rate_1e18 = int(per_second_rate * SECONDS_PER_YEAR)
        apy = rate_1e18 / SCALE_18

        total_supply = self.comet.functions.totalSupply().call()
        tvl = total_supply / USDC_DECIMALS

        return ProtocolData(
            name=self.NAME,
            adapter_index=self.ADAPTER_INDEX,
            apy=apy,
            utilization=min(utilization, 1.0),
            tvl=tvl,
            raw_rate_1e18=rate_1e18,
        )
