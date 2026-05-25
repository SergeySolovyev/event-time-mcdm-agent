"""Unit tests for PerBlockLoop (Plan E Task 3).

These tests exercise ``_handle_block`` directly with MagicMock readers /
mempool / history.  ``run(ws_url)`` is integration-only (Plan E T7).

We do NOT depend on pytest-asyncio: each test is a plain sync function
that drives the async surface via ``asyncio.run(...)`` -- the same pattern
the T2 reader tests use.
"""
from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

# Allow `from per_block_loop import ...` against the agent repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from decision.base import Action, BlockState                             # noqa: E402
from per_block_loop import PerBlockLoop                                  # noqa: E402
from protocols.base import ProtocolData                                  # noqa: E402


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #
def _snap(name: str, idx: int, apy: float, util: float = 0.7, tvl: float = 1e6) -> ProtocolData:
    return ProtocolData(
        name=name,
        adapter_index=idx,
        apy=apy,
        utilization=util,
        tvl=tvl,
        raw_rate_1e18=int(apy * 1e18),
    )


def _make_reader(name: str, idx: int, apy: float) -> MagicMock:
    r = MagicMock()
    r.NAME = name
    r.ADAPTER_INDEX = idx
    r.read_at_block = AsyncMock(return_value=_snap(name, idx, apy))
    return r


def _make_w3(gas_price_wei: int = 30_000_000_000, timestamp: int = 1_700_000_000) -> MagicMock:
    w3 = MagicMock()
    # AsyncWeb3 returns an awaitable for gas_price; sync Web3 returns int.
    # The loop handles both; we feed the simpler sync int.
    w3.eth.gas_price = gas_price_wei
    w3.eth.get_block = MagicMock(return_value={"timestamp": timestamp})
    return w3


def _make_loop(
    readers: dict[str, MagicMock],
    policy_action: Action,
    *,
    current_protocol: str | None = None,
) -> tuple[PerBlockLoop, MagicMock, MagicMock, MagicMock]:
    """Build a PerBlockLoop wired to MagicMock collaborators.

    Returns ``(loop, policy, mempool, history)`` so individual tests can
    introspect the mocks.
    """
    policy = MagicMock()
    policy.name = "stub"
    policy.decide = MagicMock(return_value=policy_action)

    mempool = MagicMock()
    mempool.submit_private_tx = AsyncMock(return_value={"status": 1})

    history = MagicMock()
    # T6 HistoryStore.append is async (asyncio.to_thread inside); AsyncMock
    # returns an awaitable so the loop's `await history.append(...)` resolves.
    history.append = AsyncMock()

    loop = PerBlockLoop(
        w3=_make_w3(),
        readers=readers,
        policy=policy,
        mempool=mempool,
        history=history,
        position_usd=1_000_000.0,
        eth_price_usd_provider=lambda: 3500.0,
        gas_used_estimate=200_000,
        per_block_deadline_s=0.5,
        current_protocol=current_protocol,
    )
    return loop, policy, mempool, history


SIX_PROTOS = [
    ("aave",     0, 0.041),
    ("compound", 1, 0.038),
    ("spark",    2, 0.045),
    ("morpho",   3, 0.052),
    ("fluid",    4, 0.039),
    ("euler",    5, 0.047),
]


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #
def test_handle_block_assembles_blockstate_from_six_readers() -> None:
    readers = {n: _make_reader(n, i, apy) for n, i, apy in SIX_PROTOS}
    hold = Action(kind="hold", target_protocol=None, rationale="stub")
    loop, policy, _, _ = _make_loop(readers, hold, current_protocol="aave")

    asyncio.run(loop._handle_block(block_number=21_000_000))

    # The policy was called exactly once with a BlockState carrying
    # all six protocols in the same order as the readers dict.
    assert policy.decide.call_count == 1
    state: BlockState = policy.decide.call_args.args[0]
    assert isinstance(state, BlockState)
    assert state.block_number == 21_000_000
    assert set(state.protocols) == {n for n, _, _ in SIX_PROTOS}
    for name, _idx, apy in SIX_PROTOS:
        assert state.lending_apr[name] == pytest.approx(apy)
        assert state.tvl_usd[name] == 1e6
        assert 0.0 <= state.utilization[name] <= 1.0
    # Sanity: gas-price unit conversion (30 gwei wire -> 30.0 gwei).
    assert state.gas_price_gwei == pytest.approx(30.0)
    assert state.eth_price_usd == pytest.approx(3500.0)
    assert state.current_protocol == "aave"
    assert state.position_usd == 1_000_000.0
    assert state.gas_used_estimate == 200_000


def test_handle_block_calls_policy_decide() -> None:
    readers = {n: _make_reader(n, i, apy) for n, i, apy in SIX_PROTOS}
    hold = Action(kind="hold", target_protocol=None)
    loop, policy, _, _ = _make_loop(readers, hold)

    asyncio.run(loop._handle_block(block_number=21_000_001))
    asyncio.run(loop._handle_block(block_number=21_000_002))

    assert policy.decide.call_count == 2


def test_hold_action_does_not_call_mempool() -> None:
    readers = {n: _make_reader(n, i, apy) for n, i, apy in SIX_PROTOS}
    hold = Action(kind="hold", target_protocol=None, rationale="no edge")
    loop, _, mempool, _ = _make_loop(readers, hold, current_protocol="aave")

    action = asyncio.run(loop._handle_block(block_number=21_000_003))

    assert action.kind == "hold"
    mempool.submit_private_tx.assert_not_called()
    # current_protocol unchanged on hold.
    assert loop.current_protocol == "aave"


def test_switch_action_calls_mempool_with_target_protocol() -> None:
    readers = {n: _make_reader(n, i, apy) for n, i, apy in SIX_PROTOS}
    switch = Action(kind="switch", target_protocol="morpho", rationale="best APR")
    loop, _, mempool, _ = _make_loop(readers, switch, current_protocol="aave")

    asyncio.run(loop._handle_block(block_number=21_000_004))

    mempool.submit_private_tx.assert_awaited_once()
    args, _kwargs = mempool.submit_private_tx.call_args
    assert args[0] == "morpho"
    # Second arg is the BlockState.
    assert isinstance(args[1], BlockState)
    assert args[1].block_number == 21_000_004


def test_switch_action_updates_current_protocol() -> None:
    readers = {n: _make_reader(n, i, apy) for n, i, apy in SIX_PROTOS}
    switch = Action(kind="switch", target_protocol="euler", rationale="rotate")
    loop, _, _, _ = _make_loop(readers, switch, current_protocol="aave")

    assert loop.current_protocol == "aave"
    asyncio.run(loop._handle_block(block_number=21_000_005))
    assert loop.current_protocol == "euler"


def test_reader_timeout_yields_nan_apr_for_that_protocol() -> None:
    # Build readers, then replace 'morpho' with a slow one that exceeds
    # the per-block deadline (0.5s in _make_loop).
    readers = {n: _make_reader(n, i, apy) for n, i, apy in SIX_PROTOS}

    async def _slow(_block_number: int) -> ProtocolData:
        await asyncio.sleep(2.0)  # > per_block_deadline_s
        return _snap("morpho", 3, 0.052)

    readers["morpho"].read_at_block = AsyncMock(side_effect=_slow)

    hold = Action(kind="hold", target_protocol=None)
    loop, policy, _, _ = _make_loop(readers, hold, current_protocol="aave")

    asyncio.run(loop._handle_block(block_number=21_000_006))

    state: BlockState = policy.decide.call_args.args[0]
    # The timed-out protocol must surface as NaN APR + zero TVL, and the
    # other five must still have their good values.
    assert math.isnan(state.lending_apr["morpho"])
    assert math.isnan(state.utilization["morpho"])
    assert state.tvl_usd["morpho"] == 0.0
    for name, _idx, apy in SIX_PROTOS:
        if name == "morpho":
            continue
        assert state.lending_apr[name] == pytest.approx(apy)


def test_handle_block_appends_to_history() -> None:
    readers = {n: _make_reader(n, i, apy) for n, i, apy in SIX_PROTOS}
    hold = Action(kind="hold", target_protocol=None)
    loop, _, _, history = _make_loop(readers, hold, current_protocol="aave")

    asyncio.run(loop._handle_block(block_number=21_000_007))

    assert history.append.call_count == 1
    args, _kwargs = history.append.call_args
    state, action = args
    assert isinstance(state, BlockState)
    assert isinstance(action, Action)
    assert state.block_number == 21_000_007
    assert action.kind == "hold"


def test_handle_block_appends_history_once_per_block_on_switch() -> None:
    """Switch path also records exactly one history row per block."""
    readers = {n: _make_reader(n, i, apy) for n, i, apy in SIX_PROTOS}
    switch = Action(kind="switch", target_protocol="spark", rationale="x")
    loop, _, _, history = _make_loop(readers, switch, current_protocol="aave")

    asyncio.run(loop._handle_block(block_number=21_000_008))
    asyncio.run(loop._handle_block(block_number=21_000_009))

    assert history.append.call_count == 2
