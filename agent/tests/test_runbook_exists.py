"""Smoke check: RUNBOOK.md exists and references the right files.

Plan E Task 7. The runbook itself is operator-executed; this test
only verifies the document is present and well-formed.

Path layout (adapted from plan-doc to match the existing
agent/tests/ convention rather than plan-doc's tests/agent/):
    D:\\DeFi\\DeFi-Vega Project\\agent\\tests\\test_runbook_exists.py
    parents[0] -> agent\\tests
    parents[1] -> agent  (== AGENT_ROOT)
"""
from __future__ import annotations

from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = AGENT_ROOT / "RUNBOOK.md"


def test_runbook_exists():
    assert RUNBOOK.exists(), f"RUNBOOK missing at {RUNBOOK}"


def test_runbook_has_required_sections():
    content = RUNBOOK.read_text(encoding="utf-8")
    required = [
        "## First-time setup",
        "## Sepolia paper-trade",
        "## Flashbots dry-run verification",
        "## Acceptance gates",
        "mklink /J decision",
        "FLASHBOTS_AUTH_KEY",
        "per_block_loop",
        "history.parquet",
    ]
    missing = [s for s in required if s not in content]
    assert not missing, f"RUNBOOK missing sections / refs: {missing}"


def test_runbook_acceptance_gates_explicit():
    """The acceptance gates must mention >=10 rebalances and dry_run path."""
    content = RUNBOOK.read_text(encoding="utf-8")
    assert (
        ">=10" in content
        or "10 or more" in content
        or "ten rebalances" in content.lower()
    )
    assert "dry_run" in content
