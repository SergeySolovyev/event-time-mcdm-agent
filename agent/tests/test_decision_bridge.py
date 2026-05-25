"""Decision-bridge contract: agent/decision/ MUST resolve to the
research-side predictive-mcdm-defi/decision/ package via a Windows
directory junction (POSIX symlink in CI). This is the zero-drift
guarantee — there is exactly ONE copy of T1/T2/T3 source on disk.

Plan E Task 1.

Path layout (adapted from plan-doc to match the existing
agent/tests/ convention rather than the plan-doc's tests/agent/):
    D:\\DeFi\\DeFi-Vega Project\\agent\\tests\\test_decision_bridge.py
    parents[0] -> agent\\tests
    parents[1] -> agent  (== AGENT_ROOT)
    parents[2] -> DeFi-Vega Project  (== AGENT_PROJECT_ROOT)
    parents[2].parent -> D:\\DeFi  (== SHARED_PARENT, holds both repos)
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


AGENT_ROOT = Path(__file__).resolve().parents[1]
AGENT_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHARED_PARENT = AGENT_PROJECT_ROOT.parent
RESEARCH_DECISION = SHARED_PARENT / "predictive-mcdm-defi" / "decision"


def test_agent_decision_junction_exists():
    """agent/decision/ must exist as either a directory junction (Windows)
    or a symlink (POSIX). Plain directory means the junction is missing."""
    bridge = AGENT_ROOT / "decision"
    assert bridge.exists(), (
        f"agent/decision bridge missing at {bridge}. Run from agent/:\n"
        f'  mklink /J decision "{RESEARCH_DECISION}"   '
        f"(cmd.exe, admin not required)"
    )
    # Junctions report is_dir()==True but resolve() != the literal path.
    assert bridge.resolve() == RESEARCH_DECISION.resolve(), (
        f"bridge resolves to {bridge.resolve()}, "
        f"expected {RESEARCH_DECISION.resolve()}"
    )


def test_importlib_resolves_to_research_file():
    """Importing agent.decision.base must yield the same module file as
    importing decision.base directly against the research repo."""
    # Pre-pend the agent's parent so `import agent.decision.base` works.
    sys.path.insert(0, str(AGENT_PROJECT_ROOT))
    # Drop any cached `agent` package picked up from elsewhere.
    for mod in list(sys.modules):
        if mod == "agent" or mod.startswith("agent."):
            sys.modules.pop(mod, None)
    try:
        agent_mod = importlib.import_module("agent.decision.base")
    finally:
        sys.path.pop(0)
    # Import research-side directly.
    sys.path.insert(0, str(SHARED_PARENT / "predictive-mcdm-defi"))
    for mod in list(sys.modules):
        if mod == "decision" or mod.startswith("decision."):
            sys.modules.pop(mod, None)
    try:
        research_mod = importlib.import_module("decision.base")
    finally:
        sys.path.pop(0)
    assert (
        Path(agent_mod.__file__).resolve()
        == Path(research_mod.__file__).resolve()
    ), (
        f"agent path {agent_mod.__file__} != "
        f"research path {research_mod.__file__} "
        f"-- junction is broken or points to a copy"
    )


def test_blockstate_class_identity():
    """Both imports must resolve to the same file on disk; the junction
    guarantees one source-of-truth.

    Note: Python's package machinery still creates *two distinct
    ModuleSpec objects* when the same file is imported through two
    different package paths (`agent.decision.base` vs `decision.base`),
    so `is` comparison on the class would fail. The valid invariant
    we pin is FILE identity, which is what makes a follow-up isinstance
    check possible as long as downstream code imports through ONE
    canonical path (the agent's `agent.decision`)."""
    sys.path.insert(0, str(AGENT_PROJECT_ROOT))
    sys.path.insert(0, str(SHARED_PARENT / "predictive-mcdm-defi"))
    for mod in list(sys.modules):
        if (
            mod == "agent"
            or mod.startswith("agent.")
            or mod == "decision"
            or mod.startswith("decision.")
        ):
            sys.modules.pop(mod, None)
    try:
        agent_pkg = importlib.import_module("agent.decision.base")
        research_pkg = importlib.import_module("decision.base")
    finally:
        sys.path.pop(0)
        sys.path.pop(0)
    assert (
        Path(agent_pkg.__file__).resolve()
        == Path(research_pkg.__file__).resolve()
    )


def test_sys_path_order_does_not_shadow_bridge(monkeypatch, tmp_path):
    """Inserting a sibling `decision/` earlier on sys.path must NOT shadow
    the agent.decision junction; agent imports go through `agent.decision`,
    not bare `decision`."""
    shadow_pkg = tmp_path / "decision"
    shadow_pkg.mkdir()
    (shadow_pkg / "__init__.py").write_text(
        '"""Decoy that MUST NOT be picked up by `from agent.decision ..`."""\n'
        "IS_SHADOW = True\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.path.insert(0, str(AGENT_PROJECT_ROOT))
    for mod in list(sys.modules):
        if mod == "agent" or mod.startswith("agent."):
            sys.modules.pop(mod, None)
    try:
        mod = importlib.import_module("agent.decision")
    finally:
        sys.path.pop(0)
    assert not getattr(mod, "IS_SHADOW", False), (
        "bare `decision` package shadowed the agent.decision bridge"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-only junction syntax check")
def test_junction_target_is_a_directory_not_a_file_symlink():
    """A file symlink would import the __init__.py only; we need the
    whole package. Verify the junction target is a directory."""
    bridge = AGENT_ROOT / "decision"
    assert bridge.resolve().is_dir()
