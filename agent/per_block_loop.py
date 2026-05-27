"""Per-block decision loop for the event-time DeFi lending allocator.

Plan E Task 3.

The :class:`PerBlockLoop` subscribes to Ethereum ``newHeads`` over a
WebSocket and, on every new block:

1. Fetches the current gas price and ETH price.
2. Reads every registered protocol's snapshot at exactly ``block_number``
   via :py:meth:`ProtocolReader.read_at_block`, all in parallel under a
   per-block deadline (readers that time out contribute NaN rates and 0 TVL
   rather than aborting the whole block).
3. Assembles a :class:`agent.decision.base.BlockState`.
4. Asks the configured :class:`DecisionPolicy` for an :class:`Action`.
5. On ``"switch"``, submits a private rebalance tx via the mempool client
   and waits for the receipt; on ``"hold"``, just records the decision.
6. Appends ``(state, action)`` to the history buffer.

The mempool client (T4) and history buffer (T6) are not built yet -- the
loop just types against the duck signatures
``mempool.submit_private_tx(target_protocol, state)`` and
``history.append(state, action)``.

Notes for tests
---------------
* ``_handle_block(block_number)`` is the unit-testable surface.
  ``run(ws_url)`` is the integration entry point and is intentionally
  not exercised by the T3 unit tests (gated to Plan E T7).
* ``read_at_block`` calls are individually wrapped in ``asyncio.wait_for``
  so a single slow reader cannot stall the whole block.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Callable, Mapping

import pandas as pd

from decision.base import Action, BlockState, DecisionPolicy
from protocols.base import ProtocolData, ProtocolReader


logger = logging.getLogger(__name__)


# Sentinel ProtocolData returned when a reader times out or raises.  The
# scorer treats NaN APR as "do not switch into me"; TVL = 0 keeps the
# liquidity-weighted aggregates well-defined.
def _stale_snapshot(name: str, adapter_index: int) -> ProtocolData:
    return ProtocolData(
        name=name,
        adapter_index=adapter_index,
        apy=float("nan"),
        utilization=float("nan"),
        tvl=0.0,
        raw_rate_1e18=0,
    )


class PerBlockLoop:
    """Drives the per-block decision cycle.

    Parameters
    ----------
    w3:
        An :class:`web3.AsyncWeb3` (or sync ``Web3``) instance used to
        fetch the gas price and block timestamp.  Tests pass a
        ``MagicMock`` whose ``eth.gas_price`` / ``eth.get_block`` are
        stubbed.
    readers:
        Mapping of protocol-label -> :class:`ProtocolReader`.  The label
        is used as the key in every per-protocol dict of the resulting
        :class:`BlockState`; the reader's own ``NAME`` is not consulted.
    policy:
        The :class:`DecisionPolicy` to ask on every block.
    mempool:
        Object exposing ``async submit_private_tx(target_protocol, state)``.
        Will be provided by Plan E T4.
    history:
        Object exposing ``append(state, action)`` (sync).  Provided by T6.
    position_usd:
        Current notional under management.
    eth_price_usd_provider:
        Zero-arg callable returning a float ETH/USD spot price.  Kept as
        a callable so tests do not need a price oracle and so future
        integration can wire in a real oracle without touching the loop.
    gas_used_estimate:
        Fixed gas-units estimate for a rebalance tx (default 200k).
    per_block_deadline_s:
        Per-reader wall-clock deadline.  Readers exceeding this become
        stale NaN snapshots for the current block.
    current_protocol:
        Initial deployed-into protocol label (or None if uninvested).
        Updated in-place after a successful ``"switch"``.
    """

    def __init__(
        self,
        w3: Any,
        readers: dict[str, ProtocolReader],
        policy: DecisionPolicy,
        mempool: Any,
        history: Any,
        position_usd: float,
        eth_price_usd_provider: Callable[[], float],
        gas_used_estimate: int = 200_000,
        per_block_deadline_s: float = 4.0,
        current_protocol: str | None = None,
    ) -> None:
        self.w3 = w3
        self.readers = readers
        self.policy = policy
        self.mempool = mempool
        self.history = history
        self.position_usd = position_usd
        self.eth_price_usd_provider = eth_price_usd_provider
        self.gas_used_estimate = gas_used_estimate
        self.per_block_deadline_s = per_block_deadline_s
        self.current_protocol = current_protocol

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    async def _read_one(
        self,
        label: str,
        reader: ProtocolReader,
        block_number: int,
    ) -> ProtocolData:
        """Read a single protocol with the per-block deadline applied.

        Failures (timeout *or* any exception inside the reader) degrade
        gracefully to a stale-NaN snapshot so that one protocol's RPC
        issues do not abort the whole block.
        """
        adapter_index = getattr(reader, "ADAPTER_INDEX", -1)
        try:
            return await asyncio.wait_for(
                reader.read_at_block(block_number),
                timeout=self.per_block_deadline_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "reader %s timed out (>%.2fs) at block %d; using NaN snapshot",
                label,
                self.per_block_deadline_s,
                block_number,
            )
            return _stale_snapshot(label, adapter_index)
        except Exception as exc:  # noqa: BLE001 -- isolate per-protocol failures
            logger.warning(
                "reader %s raised %s at block %d; using NaN snapshot",
                label,
                exc.__class__.__name__,
                block_number,
            )
            return _stale_snapshot(label, adapter_index)

    async def _fetch_gas_price_gwei(self) -> float:
        """Return the current gas price in gwei.

        ``AsyncWeb3.eth.gas_price`` is an awaitable property in web3 7.x;
        the sync ``Web3`` returns a plain int.  Tests pass MagicMocks
        whose ``gas_price`` is just an int -- this helper covers both.
        """
        raw = self.w3.eth.gas_price
        if asyncio.iscoroutine(raw):
            raw = await raw
        return float(raw) / 1e9

    async def _fetch_block_timestamp(self, block_number: int) -> pd.Timestamp:
        """Return the block timestamp as a UTC ``pd.Timestamp``.

        Falls back to ``pd.Timestamp.utcnow()`` if the call fails (tests
        that don't stub ``get_block`` still produce a valid BlockState).
        """
        try:
            getter = self.w3.eth.get_block
            block = getter(block_number)
            if asyncio.iscoroutine(block):
                block = await block
            ts = block["timestamp"] if isinstance(block, Mapping) else block.timestamp
            return pd.Timestamp(int(ts), unit="s", tz="UTC")
        except Exception:  # noqa: BLE001
            return pd.Timestamp.utcnow().tz_convert("UTC") if pd.Timestamp.utcnow().tzinfo else pd.Timestamp.utcnow().tz_localize("UTC")

    # ------------------------------------------------------------------ #
    # Public surface
    # ------------------------------------------------------------------ #
    async def _handle_block(self, block_number: int) -> Action:
        """Run one full decision cycle for ``block_number``.

        Returns the :class:`Action` chosen by the policy (also appended
        to history with the assembled :class:`BlockState`).
        """
        # 1. Gas / oracle / timestamp -- run in parallel with the readers
        #    where possible.  Order is deterministic for test snapshots.
        gas_price_gwei_task = asyncio.create_task(self._fetch_gas_price_gwei())
        block_ts_task = asyncio.create_task(self._fetch_block_timestamp(block_number))

        # 2. All readers in parallel under the per-block deadline.
        labels = list(self.readers.keys())
        reader_tasks = [
            self._read_one(label, self.readers[label], block_number)
            for label in labels
        ]
        snapshots: list[ProtocolData] = await asyncio.gather(*reader_tasks)

        gas_price_gwei = await gas_price_gwei_task
        block_timestamp = await block_ts_task
        eth_price_usd = float(self.eth_price_usd_provider())

        # 3. Assemble per-protocol dicts.
        lending_apr: dict[str, float] = {}
        utilization: dict[str, float] = {}
        tvl_usd: dict[str, float] = {}
        for label, snap in zip(labels, snapshots):
            lending_apr[label] = float(snap.apy)
            utilization[label] = float(snap.utilization)
            tvl_usd[label] = float(snap.tvl)

        state = BlockState(
            block_number=block_number,
            block_timestamp=block_timestamp,
            protocols=tuple(labels),
            lending_apr=lending_apr,
            utilization=utilization,
            tvl_usd=tvl_usd,
            current_protocol=self.current_protocol,
            position_usd=self.position_usd,
            gas_price_gwei=gas_price_gwei,
            eth_price_usd=eth_price_usd,
            gas_used_estimate=self.gas_used_estimate,
        )

        # 4. Ask the policy (timed for Prometheus latency summary).
        import time as _time
        from observability import METRICS, record_decision
        _t0 = _time.perf_counter()
        action = self.policy.decide(state)
        METRICS.observe("agent_decision_latency_seconds",
                        value=_time.perf_counter() - _t0)
        METRICS.inc("agent_blocks_processed_total")

        # 5. Execute.
        if action.kind == "switch":
            assert action.target_protocol is not None  # enforced by Action
            logger.info(
                "block %d: SWITCH %s -> %s (%s)",
                block_number,
                self.current_protocol,
                action.target_protocol,
                action.rationale,
            )
            await self.mempool.submit_private_tx(action.target_protocol, state)
            self.current_protocol = action.target_protocol
        else:
            logger.info(
                "block %d: HOLD on %s (%s)",
                block_number,
                self.current_protocol,
                action.rationale,
            )

        # 5b. Tier 5 observability sink: JSON log + Prometheus + audit trail.
        gas_cost_usd = (state.gas_used_estimate * state.gas_price_gwei * 1e-9
                        * state.eth_price_usd)
        record_decision(
            block_number=state.block_number,
            block_timestamp=state.block_timestamp.isoformat(),
            action_kind=action.kind,
            target_protocol=action.target_protocol,
            rationale=action.rationale,
            current_protocol=self.current_protocol,
            position_usd=state.position_usd,
            gas_price_gwei=state.gas_price_gwei,
            gas_cost_usd=gas_cost_usd,
            panel_snapshot={p: state.lending_apr[p] for p in state.protocols},
        )

        # 6. Persist for backtest replay / live audit.
        # T6 HistoryStore.append is async (asyncio.to_thread for the disk
        # write); legacy MagicMock-using tests are updated to AsyncMock.
        await self.history.append(state, action)

        return action

    async def run(self, ws_url: str) -> None:
        """Subscribe to ``newHeads`` and dispatch each block.

        This is the live-trading entry point; the T3 unit tests do not
        exercise it (Plan E T7 covers live integration).  Implementation
        uses web3.py 7.x's ``AsyncWeb3`` + ``WebSocketProvider`` +
        ``subscription_manager`` API.
        """
        # Imported lazily so unit tests do not need a real WS provider
        # in the module-load path.
        from web3 import AsyncWeb3
        from web3.providers.persistent import WebSocketProvider
        from web3.utils.subscriptions import NewHeadsSubscription

        async with AsyncWeb3(WebSocketProvider(ws_url)) as w3:
            # Replace the (possibly sync) w3 we were constructed with so
            # that gas-price/get-block calls inside _handle_block use the
            # live async session.
            self.w3 = w3

            async def _on_new_head(handler_context: Any) -> None:
                block_number = int(handler_context.result["number"])
                try:
                    await self._handle_block(block_number)
                except Exception:  # noqa: BLE001
                    logger.exception("block %d: handler crashed", block_number)

            await w3.subscription_manager.subscribe(
                NewHeadsSubscription(handler=_on_new_head)
            )
            await w3.subscription_manager.handle_subscriptions()
