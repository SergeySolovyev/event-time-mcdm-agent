# ERC-4626 Yield Vault with Multi-Criteria Decision Making

**Sergei Solovev | sesesolovev@edu.hse.ru | HSE University | April 2026**

## 1. Overview

This project implements an ERC-4626 tokenized vault that automatically rebalances user deposits between Aave V3 and Compound V3 lending protocols. An off-chain agent evaluates protocols using a weighted multi-criteria scoring model and submits cryptographically signed rebalance decisions to the on-chain vault. A Chainlink Automation fallback ensures the vault remains managed if the agent goes offline.

## 2. Architecture

The system has two layers:

- **Off-chain agent (Python):** Reads on-chain data every hour, applies EMA smoothing to rates, scores each protocol using 4-factor MCDM, and signs rebalance decisions with EIP-712.
- **On-chain vault (Solidity):** Receives signed parameters, verifies the keeper's ECDSA signature, checks nonce/timestamp/cooldown, and executes the rebalance through protocol adapters.

**Contracts:** AIVault.sol (ERC-4626 + UUPS proxy), StrategyManager.sol (validation + rate tracking), AaveV3Adapter.sol, CompoundV3Adapter.sol, RateMath.sol (normalization library).

The adapter pattern (`IProtocolAdapter` interface) allows adding new protocols without changing existing contracts.

## 3. Formulas

**Share pricing** (ERC-4626 with virtual shares for inflation attack protection):

$$s = \left\lfloor \frac{a \cdot (S + 10^6)}{A + 1} \right\rfloor$$

where $a$ = deposited assets, $S$ = total supply, $A$ = total assets. The $10^6$ offset prevents the known ERC-4626 inflation/donation attack.

**APY normalization** (cross-protocol, to annual 1e18 scale):

- Aave V3: $\text{APY}_{1e18} = \text{liquidityRate}_{RAY} / 10^9$ (RAY = $10^{27}$)
- Compound V3: $\text{APY}_{1e18} = r_{sec} \times 31{,}557{,}600$

**EMA smoothing** (rate noise and manipulation resistance):

$$S_t = \alpha \cdot R_t + (1 - \alpha) \cdot S_{t-1}, \quad \alpha = 0.3$$

Rate jump guard: if $|R_t - S_{t-1}| > 5\%$, the update is skipped.

**MCDM scoring model:**

$$\text{Score}_i = 0.40 \cdot f_{APY} + 0.25 \cdot f_{Risk} + 0.20 \cdot f_{Cost} + 0.15 \cdot f_{Stability}$$

| Factor | Weight | Meaning |
|--------|--------|---------|
| APY | 40% | Normalized smoothed yield |
| Risk | 25% | $1 - \text{utilization}$ (high utilization = risk) |
| Cost | 20% | $1 - \text{gasCost}/0.01$ (gas efficiency) |
| Stability | 15% | $1 - |\Delta TVL|/0.30$ (TVL change penalty) |

**Decision rule:** rebalance when $\text{Score}_{best} - \text{Score}_{current} \geq 0.05$.

**On-chain fallback threshold** (Chainlink path):

$$\text{rebalance if } \frac{\Delta APY \times TVL \times t_{since}}{365 \text{ days}} > \text{gasCost}$$

## 4. Security

| Threat | Mitigation |
|--------|-----------|
| Inflation attack | Virtual shares ($10^6$ offset in `_decimalsOffset`) |
| Reentrancy | OpenZeppelin ReentrancyGuard on all external calls |
| Rate manipulation | Dual EMA (on-chain + agent) + 5% jump guard |
| Signature forgery | EIP-712 typed data + ECDSA verification |
| Replay attack | Sequential nonce + 5-minute timestamp TTL |
| Agent downtime | Chainlink Automation fallback (6-hour timeout) |
| Rapid exploitation | 1-hour cooldown between rebalances |

Access control: deposits/withdrawals are open; rebalance requires a valid keeper signature; emergency functions are owner-only; pause/unpause is owner-only.

## 5. Testing

67 tests total, all passing:

- **37 Solidity unit tests** (concrete + fuzz with 1000 runs each)
- **4 integration tests** (full lifecycle: deposit -> rebalance -> yield -> withdraw)
- **6 invariant tests** (stateful fuzzing: 76,800+ random call sequences, 0 violations)
- **20 Python scoring tests** (boundary conditions, weight interactions)

Key invariants verified: vault solvency, deposit/withdrawal accounting, share conversion consistency, non-decreasing share price.

## 6. Deployment

Deployed on Ethereum Sepolia (chain 11155111). All contracts verified on Sourcify. UUPS proxy pattern (ERC-1967) enables future upgrades without fund migration.

## References

1. ERC-4626: Tokenized Vaults -- https://eips.ethereum.org/EIPS/eip-4626
2. EIP-712: Typed Structured Data Hashing -- https://eips.ethereum.org/EIPS/eip-712
3. Aave V3 -- https://aave.com/docs
4. Compound III -- https://docs.compound.finance/
5. Chainlink Automation -- https://docs.chain.link/chainlink-automation
6. OpenZeppelin Contracts v5 -- https://docs.openzeppelin.com/contracts/5.x/
