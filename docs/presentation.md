---
marp: true
theme: default
paginate: true
math: mathjax
style: |
  section {
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    font-size: 24px;
  }
  h1 { font-size: 38px; color: #1a1a2e; }
  h2 { font-size: 30px; color: #16213e; }
  h3 { font-size: 24px; color: #0f3460; }
  table { font-size: 18px; }
  code { font-size: 16px; }
---

<!-- _class: lead -->

# ERC-4626 Yield Vault with MCDM Scoring

**Sergei Solovev**
sesesolovev@edu.hse.ru
HSE University, April 2026

Project 1: Custom DeFi Protocol

---

# Why This Project?

### The problem with DeFi yields

Lending protocols like Aave and Compound offer variable interest rates that change every block. Users who want the best yield face three challenges:

- **Monitoring** -- rates shift every ~12 seconds, impossible to track manually
- **Gas costs** -- each rebalance costs $2-50, eating into profits
- **Risk** -- chasing the highest APY ignores utilization risk and rate stability

### The idea: an autonomous agent

What if a **software agent** could watch the rates, evaluate multiple risk factors, and move funds automatically -- while proving every decision cryptographically on-chain?

This is **agentic DeFi**: an off-chain agent that thinks, and an on-chain vault that verifies and executes.

---

# How It Works

**User deposits USDC into the vault and receives `aiUSDC` shares.**
**From that point, the agent manages everything automatically.**

```
+-------------------------------------------------------+
|  Off-chain: Python Agent (runs every hour)             |
|  1. Read APY, utilization, TVL, gas price              |
|  2. Smooth rates with EMA (anti-manipulation)          |
|  3. Score protocols with MCDM (4 factors)              |
|  4. Sign decision with EIP-712 typed data              |
+----------------------------+--------------------------+
                             | signed tx
+----------------------------+--------------------------+
|  On-chain: AIVault.sol (ERC-4626 + UUPS proxy)        |
|  - Verify keeper signature (ECDSA)                     |
|  - Check nonce, timestamp, cooldown                    |
|  - Execute rebalance via adapter                       |
|  +-- AaveV3Adapter    +-- CompoundV3Adapter            |
+-------------------------------------------------------+
  Fallback: Chainlink Automation (if agent offline > 6h)
```

**Key insight:** The agent can be complex (multi-factor analysis), but the vault only trusts cryptographic proofs -- not the agent itself.

---

# Key Formulas

**ERC-4626 share price** (with inflation attack protection):
$$s = \left\lfloor \frac{a \cdot (S + 10^6)}{A + 1} \right\rfloor$$

**APY normalization** (cross-protocol, to annual 1e18 scale):
- Aave V3: $\text{APY} = \text{liquidityRate}_{RAY} / 10^9$
- Compound V3: $\text{APY} = r_{sec} \times 31{,}557{,}600$

**EMA smoothing** (dampens noise): $S_t = 0.3 \cdot R_t + 0.7 \cdot S_{t-1}$

**MCDM scoring model:**

$$\text{Score}_i = 0.40 \cdot f_{APY} + 0.25 \cdot f_{Risk} + 0.20 \cdot f_{Cost} + 0.15 \cdot f_{Stability}$$

Rebalance if: $\text{Score}_{best} - \text{Score}_{current} \geq 0.05$

**Example:** Aave offers 6% APY but has 95% utilization (risky). Compound offers 5% but with 30% utilization (safe). Simple APY comparison picks Aave. Our MCDM model picks Compound -- the safer choice.

---

# Security and Testing

**7 threat mitigations:**

| Threat | Protection |
|--------|-----------|
| Inflation attack | Virtual shares ($10^6$ offset) |
| Reentrancy | ReentrancyGuard on all external functions |
| Rate manipulation | EMA + 5% jump guard |
| Signature forgery | EIP-712 domain + ECDSA verification |
| Replay attack | Sequential nonce + 5-min timestamp TTL |
| Agent downtime | Chainlink Automation fallback (6h) |
| Rapid exploitation | 1-hour cooldown between rebalances |

**67 tests, 0 failures:**

| Category | Tests | Method |
|----------|-------|--------|
| Unit (Solidity) | 37 | Concrete + fuzz (1000 runs each) |
| Integration | 4 | Full lifecycle (deposit -> rebalance -> yield -> withdraw) |
| Invariant | 6 | Stateful fuzzing: 76,800+ random calls, 0 violations |
| Python scoring | 20 | Pytest unit tests |

---

# Deployment

**Ethereum Sepolia** (Chain ID: 11155111). All contracts verified on Sourcify.

| Contract | Address |
|----------|---------|
| AaveV3Adapter | `0x8545D79f6FaB51EDc93Cf024fBD1FfAc98504ba1` |
| CompoundV3Adapter | `0xEB0D41F07691765314B9A45645Ee995d879c7ac7` |
| StrategyManager | `0x353469534dA4FB64d52Ae5059CEFd098557eBFa9` |
| AIVault (proxy) | `0x1324238b6F56Ccc785fC7f79Ca693546236Ad02C` |

**Tech stack:** Solidity 0.8.24, Python 3.12, Foundry, OpenZeppelin, Chainlink, Docker

**Design patterns used:** ERC-4626 (tokenized vault), UUPS proxy (ERC-1967), Adapter/Strategy pattern, EIP-712 typed signing

---

<!-- _class: lead -->

# Thank You

**Questions?**

GitHub: `github.com/SergeySolovyev/ai-yield-vault`
Contact: sesesolovev@edu.hse.ru | @Sergey_Solovjov | www.sergeisolovev.com
