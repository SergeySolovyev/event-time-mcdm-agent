"""Unit tests for the on-chain data reader.

All web3 calls are mocked; the tests exercise the Aave/Compound rate +
utilization + TVL computation paths against synthetic chain responses.
These lock in the two correctness fixes shipped in Tier 1:

  1. Aave utilization is computed from `totalBorrowed / totalSupplied`
     -- the real on-chain formula -- not from `liquidityRate /
     borrowRate` (which is an unrelated ratio).
  2. Aave TVL is the aToken total supply in underlying-asset units,
     not a hardcoded zero.

A regression on either bug will fail one of the corresponding tests.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from data_reader import DataReader, RAY, SECONDS_PER_YEAR, USDC_DECIMALS  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers: assemble a fully-mocked Web3 + DataReader instance.
# ---------------------------------------------------------------------------

def _make_function_mock(return_value):
    """A web3.py-style chainable Function: .call() returns the value."""
    fn = MagicMock()
    fn.call.return_value = return_value
    return fn


def _make_contract_mock():
    """A web3.py-style contract: contract.functions.NAME(args).call() pattern."""
    contract = MagicMock()
    contract.functions = MagicMock()
    return contract


def _make_w3():
    """A web3.py instance that mints the contract per .eth.contract() call."""
    w3 = MagicMock()
    w3.eth.contract = MagicMock(side_effect=lambda **kw: _make_contract_mock())
    return w3


def _build_reader(aave_reserve_data, atoken_supply, vdebt_supply,
                  compound_util_1e18, compound_per_sec_rate, compound_total_supply):
    """Build a DataReader whose every on-chain read returns synthetic values.

    Constructs the reader with a stub Web3, then replaces each contract
    with a hand-tooled MagicMock that returns the specified payloads when
    its functions are .call()ed.
    """
    w3 = _make_w3()
    reader = DataReader(w3)

    # Aave Pool — return the synthetic 15-tuple reserve data
    aave_get_reserve = MagicMock()
    aave_get_reserve.return_value = _make_function_mock(aave_reserve_data)
    reader.aave_pool = MagicMock()
    reader.aave_pool.functions = MagicMock()
    reader.aave_pool.functions.getReserveData = aave_get_reserve

    # The aToken / variableDebtToken contracts are lazily bound on the first
    # _read_aave() call -- we pre-bind them here to inject controlled supplies.
    atoken = MagicMock()
    atoken.functions = MagicMock()
    atoken.functions.totalSupply = MagicMock(return_value=_make_function_mock(atoken_supply))
    reader._aave_atoken = atoken

    vdebt = MagicMock()
    vdebt.functions = MagicMock()
    vdebt.functions.totalSupply = MagicMock(return_value=_make_function_mock(vdebt_supply))
    reader._aave_variable_debt = vdebt

    # Compound Comet
    reader.comet = MagicMock()
    reader.comet.functions = MagicMock()
    reader.comet.functions.getUtilization = MagicMock(
        return_value=_make_function_mock(compound_util_1e18))
    reader.comet.functions.getSupplyRate = MagicMock(
        return_value=_make_function_mock(compound_per_sec_rate))
    reader.comet.functions.totalSupply = MagicMock(
        return_value=_make_function_mock(compound_total_supply))

    return reader


def _empty_aave_reserve(liquidity_rate_ray=0, variable_borrow_rate_ray=0):
    """A 15-element tuple matching Aave V3 ReserveData layout."""
    return (
        0,                      # 0  configuration
        0,                      # 1  liquidityIndex
        liquidity_rate_ray,     # 2  currentLiquidityRate  <-- read
        0,                      # 3  variableBorrowIndex
        variable_borrow_rate_ray,  # 4  currentVariableBorrowRate
        0,                      # 5  currentStableBorrowRate
        0,                      # 6  lastUpdateTimestamp
        0,                      # 7  id
        "0x" + "11" * 20,       # 8  aTokenAddress  <-- read
        "0x" + "00" * 20,       # 9  stableDebtTokenAddress
        "0x" + "22" * 20,       # 10 variableDebtTokenAddress  <-- read
        "0x" + "00" * 20,       # 11 interestRateStrategyAddress
        0,                      # 12 accruedToTreasury
        0,                      # 13 unbacked
        0,                      # 14 isolationModeTotalDebt
    )


# ---------------------------------------------------------------------------
# Aave tests: lock in the Tier-1 correctness fixes.
# ---------------------------------------------------------------------------

class TestAaveUtilization:
    """Regression tests for the totalBorrowed / totalSupplied formula."""

    def test_utilization_uses_token_supplies_not_rate_ratio(self):
        """Utilization MUST come from totalBorrowed / totalSupplied.

        Construct a scenario where the buggy formula (rate ratio) and the
        correct formula give very different answers: borrow_rate is
        deliberately set to a rate-ratio that would imply
        u_buggy = 0.05 / 0.10 = 0.5, while the true on-chain supplies give
        u_correct = 700 / 1000 = 0.70.  Only the latter is sound.
        """
        # 5% liquidity rate, 10% borrow rate  -> rate ratio = 0.5
        liquidity_rate_ray = 5 * RAY // 100
        borrow_rate_ray = 10 * RAY // 100

        reserve_data = _empty_aave_reserve(liquidity_rate_ray, borrow_rate_ray)
        # 1000 USDC supplied, 700 USDC borrowed  -> true util = 0.70
        atoken_supply = 1000 * USDC_DECIMALS
        vdebt_supply = 700 * USDC_DECIMALS

        reader = _build_reader(reserve_data, atoken_supply, vdebt_supply, 0, 0, 0)
        p = reader._read_aave()

        assert abs(p.utilization - 0.70) < 1e-6, \
            f"Aave util must use token supplies; got {p.utilization} (rate-ratio bug would give 0.5)"

    def test_utilization_zero_when_no_supply(self):
        """No deposits -> utilization is 0, not a div-by-zero crash."""
        reserve_data = _empty_aave_reserve(liquidity_rate_ray=0, variable_borrow_rate_ray=0)
        reader = _build_reader(reserve_data, atoken_supply=0, vdebt_supply=0,
                               compound_util_1e18=0, compound_per_sec_rate=0,
                               compound_total_supply=0)
        p = reader._read_aave()
        assert p.utilization == 0.0

    def test_utilization_clamped_to_one(self):
        """Borrow > supply (transient overdraw state) clamps to 1.0."""
        reserve_data = _empty_aave_reserve(0, 0)
        reader = _build_reader(reserve_data, atoken_supply=1000 * USDC_DECIMALS,
                               vdebt_supply=1500 * USDC_DECIMALS,  # > supply
                               compound_util_1e18=0, compound_per_sec_rate=0,
                               compound_total_supply=0)
        p = reader._read_aave()
        assert p.utilization == 1.0


class TestAaveTVL:
    """Regression tests for real TVL (no longer hardcoded zero)."""

    def test_tvl_equals_atoken_supply_in_usdc_units(self):
        """TVL is aToken.totalSupply / 10**6 (USDC decimals)."""
        reserve_data = _empty_aave_reserve(0, 0)
        reader = _build_reader(reserve_data,
                               atoken_supply=1_234_567 * USDC_DECIMALS,
                               vdebt_supply=0,
                               compound_util_1e18=0, compound_per_sec_rate=0,
                               compound_total_supply=0)
        p = reader._read_aave()
        assert p.tvl == 1_234_567.0

    def test_tvl_not_hardcoded_zero(self):
        """A protocol with deposits MUST report nonzero TVL."""
        reserve_data = _empty_aave_reserve(0, 0)
        reader = _build_reader(reserve_data,
                               atoken_supply=1 * USDC_DECIMALS,
                               vdebt_supply=0,
                               compound_util_1e18=0, compound_per_sec_rate=0,
                               compound_total_supply=0)
        p = reader._read_aave()
        assert p.tvl > 0.0, "Aave TVL hardcoded-to-zero regression"


class TestAaveAPY:
    """Sanity tests for the RAY -> decimal conversion."""

    def test_apy_decoded_from_ray(self):
        """A 5% liquidity rate (5e25 in RAY) decodes to 0.05 APY."""
        liquidity_rate_ray = 5 * RAY // 100  # 5% in RAY
        reserve_data = _empty_aave_reserve(liquidity_rate_ray, 0)
        reader = _build_reader(reserve_data, atoken_supply=100 * USDC_DECIMALS,
                               vdebt_supply=0, compound_util_1e18=0,
                               compound_per_sec_rate=0, compound_total_supply=0)
        p = reader._read_aave()
        assert abs(p.apy - 0.05) < 1e-9


# ---------------------------------------------------------------------------
# Compound tests: validate the per-second -> annual conversion path.
# ---------------------------------------------------------------------------

class TestCompound:
    def test_per_second_rate_annualized(self):
        """per-second rate is multiplied by SECONDS_PER_YEAR."""
        # Pick a per-second rate that annualizes to 5% APY.
        per_sec = int(0.05 * 10**18 / SECONDS_PER_YEAR)
        reader = _build_reader(_empty_aave_reserve(),
                               atoken_supply=0, vdebt_supply=0,
                               compound_util_1e18=int(0.4 * 10**18),
                               compound_per_sec_rate=per_sec,
                               compound_total_supply=999 * USDC_DECIMALS)
        p = reader._read_compound()
        assert abs(p.apy - 0.05) < 1e-3  # within rounding from the int floor

    def test_compound_utilization_scaled(self):
        """getUtilization returns 1e18-scaled; reader normalizes to [0,1]."""
        reader = _build_reader(_empty_aave_reserve(),
                               atoken_supply=0, vdebt_supply=0,
                               compound_util_1e18=int(0.65 * 10**18),
                               compound_per_sec_rate=0,
                               compound_total_supply=0)
        p = reader._read_compound()
        assert abs(p.utilization - 0.65) < 1e-9

    def test_compound_tvl_uses_usdc_decimals(self):
        """TVL = totalSupply / 1e6."""
        reader = _build_reader(_empty_aave_reserve(),
                               atoken_supply=0, vdebt_supply=0,
                               compound_util_1e18=0,
                               compound_per_sec_rate=0,
                               compound_total_supply=42_000_000 * USDC_DECIMALS)
        p = reader._read_compound()
        assert p.tvl == 42_000_000.0


# ---------------------------------------------------------------------------
# get_tvl_delta semantics
# ---------------------------------------------------------------------------

class TestTvlDelta:
    def test_first_observation_returns_zero(self):
        """No prior reading -> delta is 0 (we don't penalize for missing history)."""
        reader = _build_reader(_empty_aave_reserve(), 0, 0, 0, 0, 0)
        p = reader._read_aave()
        # Inject TVL since reader returned 0
        p.tvl = 100.0
        assert reader.get_tvl_delta(p) == 0.0

    def test_delta_positive_on_inflow(self):
        reader = _build_reader(_empty_aave_reserve(), 0, 0, 0, 0, 0)
        p = reader._read_aave()
        p.tvl = 100.0
        reader.get_tvl_delta(p)              # prime history
        p.tvl = 110.0
        delta = reader.get_tvl_delta(p)
        assert abs(delta - 0.10) < 1e-9

    def test_delta_negative_on_outflow(self):
        reader = _build_reader(_empty_aave_reserve(), 0, 0, 0, 0, 0)
        p = reader._read_aave()
        p.tvl = 100.0
        reader.get_tvl_delta(p)
        p.tvl = 80.0
        delta = reader.get_tvl_delta(p)
        assert abs(delta + 0.20) < 1e-9


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
