"""Aave V3 reader.

Reads currentLiquidityRate (RAY-scaled annual), then resolves the
aToken and variableDebtToken addresses from `getReserveData` and
queries their totalSupplies to compute true utilization and TVL.
"""

from __future__ import annotations

from web3 import Web3

from .base import ProtocolData, ProtocolReader


RAY = 10**27
SCALE_18 = 10**18
USDC_DECIMALS = 10**6

POOL_ABI = [
    {
        "inputs": [{"name": "asset", "type": "address"}],
        "name": "getReserveData",
        "outputs": [
            {
                "components": [
                    {"name": "configuration", "type": "uint256"},
                    {"name": "liquidityIndex", "type": "uint128"},
                    {"name": "currentLiquidityRate", "type": "uint128"},
                    {"name": "variableBorrowIndex", "type": "uint128"},
                    {"name": "currentVariableBorrowRate", "type": "uint128"},
                    {"name": "currentStableBorrowRate", "type": "uint128"},
                    {"name": "lastUpdateTimestamp", "type": "uint40"},
                    {"name": "id", "type": "uint16"},
                    {"name": "aTokenAddress", "type": "address"},
                    {"name": "stableDebtTokenAddress", "type": "address"},
                    {"name": "variableDebtTokenAddress", "type": "address"},
                    {"name": "interestRateStrategyAddress", "type": "address"},
                    {"name": "accruedToTreasury", "type": "uint128"},
                    {"name": "unbacked", "type": "uint128"},
                    {"name": "isolationModeTotalDebt", "type": "uint128"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

ERC20_ABI = [
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class AaveV3Reader(ProtocolReader):
    NAME = "Aave V3"
    ADAPTER_INDEX = 0

    def __init__(self, w3: Web3, pool_address: str, asset_address: str,
                 adapter_index: int = 0):
        super().__init__(w3)
        self.ADAPTER_INDEX = adapter_index
        self.asset = Web3.to_checksum_address(asset_address)
        self.pool = w3.eth.contract(
            address=Web3.to_checksum_address(pool_address),
            abi=POOL_ABI,
        )
        self._atoken = None
        self._variable_debt = None

    def read(self) -> ProtocolData:
        reserve_data = self.pool.functions.getReserveData(self.asset).call()

        # currentLiquidityRate is RAY-scaled and already annualized.
        liquidity_rate_ray = reserve_data[2]
        rate_1e18 = liquidity_rate_ray * SCALE_18 // RAY
        apy = liquidity_rate_ray / RAY

        # Lazily bind aToken / variableDebtToken (addresses are stable).
        if self._atoken is None or self._variable_debt is None:
            atoken_addr = Web3.to_checksum_address(reserve_data[8])
            vdebt_addr = Web3.to_checksum_address(reserve_data[10])
            self._atoken = self.w3.eth.contract(address=atoken_addr, abi=ERC20_ABI)
            self._variable_debt = self.w3.eth.contract(address=vdebt_addr, abi=ERC20_ABI)

        total_supplied = self._atoken.functions.totalSupply().call()
        total_borrowed = self._variable_debt.functions.totalSupply().call()

        utilization = (total_borrowed / total_supplied) if total_supplied > 0 else 0.0
        tvl = total_supplied / USDC_DECIMALS

        return ProtocolData(
            name=self.NAME,
            adapter_index=self.ADAPTER_INDEX,
            apy=apy,
            utilization=min(utilization, 1.0),
            tvl=tvl,
            raw_rate_1e18=rate_1e18,
        )
