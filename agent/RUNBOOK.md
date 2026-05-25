# Agent RUNBOOK

Operator playbook for the event-time DeFi lending allocator agent.
Expanded as Plan E proceeds; T1 seeds the decision-bridge section.

---

## First-time setup: decision bridge

The `agent/decision/` directory is **NOT** a real folder. It is a Windows
directory junction (or POSIX symlink in CI) that points at the research
repo's `predictive-mcdm-defi/decision/` package, so the agent and the
research notebooks consume the SAME T1/T2/T3 source files — zero drift,
no copy-paste.

git ignores this path (`.gitignore` entry `agent/decision/`) because git's
Windows port mishandles junctions and would commit it as an empty
directory on fresh clones.

**Recreate after fresh checkout (Windows, cmd.exe — admin NOT required):**

```
cd /d "D:\DeFi\DeFi-Vega Project\agent"
mklink /J decision "D:\DeFi\predictive-mcdm-defi\decision"
```

**POSIX equivalent (Linux/macOS CI):**

```
ln -s "$REPO_ROOT/predictive-mcdm-defi/decision" \
      "$REPO_ROOT/DeFi-Vega Project/agent/decision"
```

**Verify:**

```
.venv\Scripts\pytest agent\tests\test_decision_bridge.py -v
```

Expect 5 passed on Windows / 4 passed + 1 skipped on POSIX CI. The
contract tests pin: junction-exists, importlib-resolves-to-research-file,
BlockState-file-identity, sys.path-order-does-not-shadow, and (Windows
only) junction-target-is-a-directory.

---

## Sections to follow (placeholders for Plan E T2-T7)

- T2: ProtocolReader configuration (RPC URLs, USDC vault addresses per protocol)
- T3: per_block_loop service install / supervisor config
- T4: Flashbots auth key generation
- T5: signal builder feature freshness checks
- T6: history.parquet rotation and backup
- T7: Sepolia paper-trade dry run procedure
