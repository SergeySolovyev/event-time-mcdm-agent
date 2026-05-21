"""
On-chain data reader.

Fetches real-time protocol metrics from Aave V3 and Compound V3
via web3.py. All rates are normalized to annual 1e18 scale before
being returned to the scoring engine.
"""

from dataclasses import dataclass
from web3 import Web3

import config

# Minimal ABIs (only the functions we need)

AAVE_POOL_ABI = [
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

# Minimal ERC20 ABI used to fetch aToken (totalSupplied) and
# variableDebtToken (totalBorrowed) for the Aave utilization formula.
ERC20_ABI = [
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

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

# Constants

RAY = 10**27
SECONDS_PER_YEAR = 365.25 * 24 * 3600
SCALE_18 = 10**18
USDC_DECIMALS = 10**6  # USDC uses 6 decimals; aToken inherits the underlying's decimals.


@dataclass
class ProtocolData:
    """Snapshot of a protocol's current state."""

    name: str
    adapter_index: int
    apy: float                # Annual rate as a decimal (e.g., 0.05 = 5%)
    utilization: float        # 0.0 to 1.0
    tvl: float                # Total value locked in the protocol's market (USD or token units)
    raw_rate_1e18: int        # Raw rate in 1e18 scale (for EMA smoothing)


class DataReader:
    """Reads on-chain data from Aave V3 and Compound V3."""

    def __init__(self, w3: Web3):
        self.w3 = w3
        self.aave_pool = w3.eth.contract(
            address=Web3.to_checksum_address(config.AAVE_POOL_ADDRESS),
            abi=AAVE_POOL_ABI,
        )
        self.comet = w3.eth.contract(
            address=Web3.to_checksum_address(config.COMPOUND_COMET_ADDRESS),
            abi=COMET_ABI,
        )
        self._prev_tvl: dict[int, float] = {}
        # Cache for Aave aToken / variableDebtToken contract instances; their
        # addresses come from getReserveData on first call and do not change
        # for the lifetime of the reserve.
        self._aave_atoken = None              # type: ignore[var-annotated]
        self._aave_variable_debt = None       # type: ignore[var-annotated]

    def read_all(self) -> list[ProtocolData]:
        """Read current data from all supported protocols.

        Each protocol is read in isolation: a failure (RPC timeout,
        revert, malformed return) on one protocol skips THAT protocol
        only, rather than aborting the entire scoring cycle.  The
        caller is responsible for tolerating a degraded list -- the
        scoring engine simply evaluates whichever protocols returned.
        """
        import logging
        log = logging.getLogger("ai-vault-agent.reader")

        results: list[ProtocolData] = []
        for name, fn in (("Aave V3", self._read_aave),
                         ("Compound V3", self._read_compound)):
            try:
                results.append(fn())
            except Exception as exc:                                  # noqa: BLE001
                log.error(f"read_all: {name} failed -- skipping this cycle: {exc}")
        return results

    def _read_aave(self) -> ProtocolData:
        """Read Aave V3 supply rate, utilization, and TVL.

        Utilization is the canonical formula totalBorrowed / totalSupplied,
        sourced directly from the aToken and variableDebtToken total
        supplies returned by getReserveData. TVL is the aToken total supply
        in underlying-asset units (USDC, 6 decimals).
        """
        usdc = Web3.to_checksum_address(config.USDC_ADDRESS)
        reserve_data = self.aave_pool.functions.getReserveData(usdc).call()

        # currentLiquidityRate is in RAY (1e27), already annualized.
        liquidity_rate_ray = reserve_data[2]
        rate_1e18 = liquidity_rate_ray * SCALE_18 // RAY
        apy = liquidity_rate_ray / RAY  # decimal, e.g. 0.045 = 4.5% APY

        # Lazily bind token contracts on first read; addresses are stable.
        if self._aave_atoken is None or self._aave_variable_debt is None:
            atoken_addr = Web3.to_checksum_address(reserve_data[8])           # aTokenAddress
            vdebt_addr = Web3.to_checksum_address(reserve_data[10])           # variableDebtTokenAddress
            self._aave_atoken = self.w3.eth.contract(address=atoken_addr, abi=ERC20_ABI)
            self._aave_variable_debt = self.w3.eth.contract(address=vdebt_addr, abi=ERC20_ABI)

        # totalSupplied = aToken.totalSupply; totalBorrowed = variableDebtToken.totalSupply
        # (stable-rate debt has been disabled across V3 deployments; we omit it.)
        total_supplied = self._aave_atoken.functions.totalSupply().call()
        total_borrowed = self._aave_variable_debt.functions.totalSupply().call()

        if total_supplied > 0:
            utilization = total_borrowed / total_supplied
        else:
            utilization = 0.0

        tvl = total_supplied / USDC_DECIMALS  # USDC has 6 decimals

        return ProtocolData(
            name="Aave V3",
            adapter_index=0,
            apy=apy,
            utilization=min(utilization, 1.0),
            tvl=tvl,
            raw_rate_1e18=rate_1e18,
        )

    def _read_compound(self) -> ProtocolData:
        """Read Compound V3 supply rate and metrics."""
        # Utilization (1e18 scale)
        utilization_raw = self.comet.functions.getUtilization().call()
        utilization = utilization_raw / SCALE_18

        # Supply rate (per-second, needs annualization)
        per_second_rate = self.comet.functions.getSupplyRate(utilization_raw).call()
        rate_1e18 = int(per_second_rate * SECONDS_PER_YEAR)
        apy = rate_1e18 / SCALE_18

        # TVL = totalSupply of the Comet market
        total_supply = self.comet.functions.totalSupply().call()
        tvl = total_supply / 1e6  # USDC has 6 decimals

        return ProtocolData(
            name="Compound V3",
            adapter_index=1,
            apy=apy,
            utilization=min(utilization, 1.0),
            tvl=tvl,
            raw_rate_1e18=rate_1e18,
        )

    def get_tvl_delta(self, protocol: ProtocolData) -> float:
        """
        Compute TVL change since last observation.
        Returns fractional change (e.g., -0.05 means 5% drop).
        """
        idx = protocol.adapter_index
        prev = self._prev_tvl.get(idx)
        self._prev_tvl[idx] = protocol.tvl

        if prev is None or prev == 0:
            return 0.0
        return (protocol.tvl - prev) / prev

    def get_gas_price(self) -> int:
        """Get current gas price in wei."""
        return self.w3.eth.gas_price

    def block_age_seconds(self) -> float:
        """Wall-clock age of the latest block.

        Used as a stale-data guard: if the RPC is lagging by minutes,
        the scoring cycle should skip rather than act on stale on-chain
        state.  Returns a non-negative float; clock skew that puts the
        block in the future is clamped to zero.
        """
        import time as _t
        try:
            latest = self.w3.eth.get_block("latest")
        except Exception:
            # If we cannot even reach the RPC, treat as infinitely stale
            # so the caller skips this cycle.
            return float("inf")
        block_ts = latest["timestamp"]
        return max(0.0, _t.time() - block_ts)
