# Agent Runbook — Sepolia paper-trade for Plan E

Operator playbook for the event-time DeFi lending allocator agent
(`agent/per_block_loop.py`). Follow it top-to-bottom on a fresh clone
to bring the agent up on Sepolia testnet, run ≥10 rebalances, and
verify the Flashbots private-mempool path end-to-end (in `dry_run`
mode — no real-money tx submission).

---

## First-time setup

### 1. Decision bridge (one-shot, after every fresh checkout)

The `agent/decision/` directory is **NOT** a real folder. It is a
Windows directory junction (POSIX symlink in CI) that points at the
research repo's `predictive-mcdm-defi/decision/` package, so the agent
and the research notebooks consume the SAME T1/T2/T3 source files —
zero drift, no copy-paste.

git ignores this path (`.gitignore` entry `agent/decision/`) because
git's Windows port mishandles junctions and would commit it as an
empty directory on fresh clones.

**Recreate after fresh checkout (Windows, cmd.exe — admin NOT required):**

```cmd
cd /d "D:\DeFi\DeFi-Vega Project\agent"
mklink /J decision "D:\DeFi\predictive-mcdm-defi\decision"
```

**POSIX equivalent (Linux/macOS CI):**

```bash
ln -s "$REPO_ROOT/predictive-mcdm-defi/decision" \
      "$REPO_ROOT/DeFi-Vega Project/agent/decision"
```

**Verify:**

```cmd
.venv\Scripts\pytest agent\tests\test_decision_bridge.py -v
```

Expect 5 passed on Windows / 4 passed + 1 skipped on POSIX CI.

### 2. Environment variables

Create `agent/.env` (NEVER commit — `.env` is already in `.gitignore`):

```dotenv
# Sepolia RPC -- Alchemy / Infura free tier suffices
SEPOLIA_WS_URL=wss://eth-sepolia.g.alchemy.com/v2/<YOUR_KEY>
SEPOLIA_HTTP_URL=https://eth-sepolia.g.alchemy.com/v2/<YOUR_KEY>

# Wallet -- a Sepolia-funded account. ALWAYS use a FRESH burner key,
# never your mainnet wallet. Get test ETH from a Sepolia faucet:
#   https://www.alchemy.com/faucets/ethereum-sepolia
#   https://sepoliafaucet.com/
WALLET_KEY=0x<64 hex chars>

# Flashbots reputation signer -- a SEPARATE secp256k1 key from the
# wallet key. NEVER reuse WALLET_KEY here; doing so would dox the
# wallet address to every relay observer for free.
# Generate fresh:
#   .venv\Scripts\python -c "from eth_account import Account; print(Account.create().key.hex())"
FLASHBOTS_AUTH_KEY=0x<64 hex chars>

# Euler V2 USDC vault address (per-asset EVK vault, pinned by operator).
# Find it at https://app.euler.finance/vaults
EULER_USDC_VAULT=0x<40 hex chars>
```

### 3. Install dependencies

```cmd
cd /d "D:\DeFi\DeFi-Vega Project\agent"
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

If you have only one venv across both repos, the research repo's
`.venv` is already wired with `web3`, `aiohttp`, `pandas`, `pyarrow`,
`lifelines` (T3 hazard) — point pytest at it as the test runs
already do.

### 4. Pre-flight tests

```cmd
.venv\Scripts\pytest agent\tests -v -m "not network"
```

Expected layout (41 tests once T7 lands):

| Suite | Tests |
|---|---|
| `test_decision_bridge.py` (T1) | 5 |
| `test_protocols_{spark,morpho,fluid,euler}.py` (T2) | 25 |
| `test_per_block_loop.py` (T3) | 8 |
| `test_mempool.py` (T4) | 10 |
| `test_signal_{f1,f3,f4}.py` (T5) | 15 |
| `test_state_history.py` (T6) | 11 |
| `test_runbook_exists.py` (T7) | 3 |
| **Total Plan E** | **77** |

(plus pre-existing `test_data_reader.py`, `test_scoring.py`).

---

## Sepolia paper-trade

### Configuration

Create `agent/configs/sepolia_paper.yaml`:

```yaml
mode: paper_trade
network: sepolia
position_usd: 1_000_000.0
policy:
  name: t2_optimal_stopping     # one of: t1_threshold | t2_optimal_stopping | t3_hazard
  initial_dwell_blocks: 10000   # T1 only -- ignored by T2/T3
  recalibrate_every: 1000       # T2/T3
mempool:
  dry_run: true                 # CRITICAL -- false would submit real tx
  max_block_offset: 25
history:
  path: agent/state/history.parquet
  max_rows: 5000
```

### Launch

```cmd
cd /d "D:\DeFi\DeFi-Vega Project\agent"
.venv\Scripts\python -m per_block_loop --config configs\sepolia_paper.yaml --log-level INFO 2>&1 | tee state\runbook_first_run.log
```

Run for **at least 30 minutes (~150 blocks at 12 s/block)**. Watch
the log for lines like:

```
INFO block=4500123 policy=T2OptimalStoppingPolicy action=switch
     rationale=T2 switch: spread 32.4bp > S* 14.1bp (kappa=4.2e-04)
INFO switch submitted: {'status': 'dry_run', 'txhash': '0xabc...'}
```

### Stopping

`Ctrl-C` is safe — the async loop's `KeyboardInterrupt` handler
flushes any in-progress `history.append` before exit. After stopping:

```cmd
.venv\Scripts\python -c "import pandas as pd; df = pd.read_parquet('state/history.parquet'); print(df.tail()); print(f'{len(df)} rows, {(df.action_kind == \"switch\").sum()} switches')"
```

---

## Flashbots dry-run verification

The `dry_run=true` config short-circuits before the POST to
`https://relay.flashbots.net`. To verify the build-and-sign path
WITHOUT ever submitting a real tx, run the dedicated smoke:

```cmd
.venv\Scripts\python -m agent.scripts.flashbots_smoke ^
    --auth-key %FLASHBOTS_AUTH_KEY% ^
    --wallet-key %WALLET_KEY% ^
    --rpc-url %SEPOLIA_HTTP_URL%
```

Expected output:

```
[1/3] Building tx for migration aave -> morpho on Sepolia mock pools...
      raw_tx = 0x02f8...  (217 bytes)
      txhash = 0xc04b...
[2/3] Signing X-Flashbots-Signature with FLASHBOTS_AUTH_KEY...
      signer addr = 0xAE7F...   (NOT the wallet addr 0x1F2C... -- correct)
      sig = 0x9d3a...
[3/3] dry_run=True -- no POST. submit_private_tx returned:
      {'status': 'dry_run', 'txhash': '0xc04b...'}
```

**If `[2/3]` shows the wallet addr instead of the auth-key addr**,
the `X-Flashbots-Signature` header is signed with the wrong key —
this would dox the wallet to every relay observer for free. STOP,
regenerate `FLASHBOTS_AUTH_KEY` (must be different from `WALLET_KEY`,
enforced by the `FlashbotsMempool` constructor), and re-run.

To actually exercise the relay (still dry-run from a fund POV because
Sepolia ETH is worthless), set `dry_run: false` in the YAML and
confirm the relay accepts the tx and the receipt poller returns
`status: included` after one or two blocks. **Roll back to
`dry_run: true` before any mainnet deployment.**

---

## Acceptance gates

For Plan E to be considered complete, the runbook execution must
produce a `state/runbook_first_run.log` that satisfies **ten rebalances
or more**:

| Gate | Threshold | Verify |
|---|---|---|
| Switch decisions | >=10 rebalances logged | `grep -c "action=switch" state/runbook_first_run.log` |
| Dry-run path | every switch returns `status='dry_run'` with non-empty txhash | `grep "switch submitted" state/runbook_first_run.log \| grep -v dry_run` returns 0 lines |
| History persistence | `history.parquet` has >=100 rows | `python -c "import pandas as pd; print(len(pd.read_parquet('state/history.parquet')))"` >=100 |
| No unhandled crashes | zero "unhandled exception" lines | `grep -c "unhandled exception\|crashed" state/runbook_first_run.log` returns 0 |

If any gate fails, debug before declaring Plan E complete. Common
failure modes:

* **No switches logged** — check that the policy isn't permanently
  in `hold`. Likely T2's κ is below the floor (1e-6) on Sepolia mock
  spreads; try T1 instead, or raise `position_usd` to make the
  switching boundary easier to clear.

* **`unhandled exception in block N handler`** — usually one of the
  protocol readers is calling a contract that isn't deployed on
  Sepolia. The plan deploys all six via the
  `agent/scripts/deploy_sepolia_mocks.sh` helper before this runbook
  step (deferred to a Plan E.1 follow-up if not yet done).

* **`history.parquet` corrupted on restart** — `_load_existing()`
  catches the read error and starts fresh, so this is recoverable
  but shouldn't happen. Check disk space; the atomic-write code
  relies on `os.replace` succeeding.

---

## Operator sign-off

After all gates pass, append this block to
`state/runbook_first_run.log` and commit it:

```
=== OPERATOR SIGN-OFF (Plan E Task 7) ===
Operator: <name>
Date: <YYYY-MM-DD>
Run duration: <minutes>
Total blocks observed: <N>
Switch decisions: <K>
Final history.parquet rows: <M>
Flashbots dry-run verified: YES / NO
Unhandled exceptions: <0 expected>
```

Then commit the log:

```cmd
git add agent/state/runbook_first_run.log
git commit -m "Plan E Task 7: Sepolia paper-trade runbook executed, all gates pass"
```
