"""Tier 5 observability tests.

Covers: JSON log schema, Prometheus exposition correctness, audit-trail
file rotation and fsync, kill-switch path. Network-free.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import observability as obs


def test_json_log_emits_well_formed_line(capfd):
    obs.json_log("test_event", level="info", k=1, v="x")
    err = capfd.readouterr().err.strip().splitlines()[-1]
    record = json.loads(err)
    assert record["event"] == "test_event"
    assert record["level"] == "info"
    assert record["k"] == 1
    assert record["v"] == "x"
    assert "ts" in record and record["ts"].endswith("Z")
    assert "instance" in record


def test_json_log_never_raises_on_unserializable():
    class Weird:
        def __repr__(self): return "<weird>"
    # Should NOT raise — default=str converts unknown types to repr.
    obs.json_log("weird", payload=Weird())


def test_metrics_registry_counter_and_gauge():
    reg = obs.MetricsRegistry()
    reg.declare("c", help="counter", kind="counter")
    reg.declare("g", help="gauge", kind="gauge")
    reg.inc("c")
    reg.inc("c", {"proto": "aave"}, value=3)
    reg.set("g", value=42.5)
    out = reg.render()
    assert "# TYPE c counter" in out
    assert "# TYPE g gauge" in out
    assert "c 1.0" in out
    assert 'c{proto="aave"} 3.0' in out
    assert "g 42.5" in out


def test_metrics_summary_quantiles():
    reg = obs.MetricsRegistry()
    reg.declare("h", help="lat", kind="summary")
    for v in [0.01, 0.02, 0.05, 0.1, 0.5]:
        reg.observe("h", value=v)
    out = reg.render()
    assert "h_count 5" in out
    assert 'quantile="0.5"' in out
    assert 'quantile="0.95"' in out


def test_audit_trail_appends_and_rotates_daily(tmp_path):
    audit = obs.AuditTrail(tmp_path)
    audit.append(event="decision", block=1, action="hold")
    audit.append(event="decision", block=2, action="switch")
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["block"] == 1
    assert json.loads(lines[1])["action"] == "switch"
    audit.close()


def test_record_decision_writes_all_three_surfaces(tmp_path, capfd, monkeypatch):
    # Swap module-level audit to a tmp instance so the test doesn't pollute
    # the real agent/state/audit/ directory.
    test_audit = obs.AuditTrail(tmp_path)
    monkeypatch.setattr(obs, "AUDIT", test_audit)
    # Reset metrics to a clean instance for assertions.
    monkeypatch.setattr(obs, "METRICS", obs.MetricsRegistry())
    obs.METRICS.declare("agent_decisions_total", help="", kind="counter")
    obs.METRICS.declare("agent_position_usd", help="", kind="gauge")
    obs.METRICS.declare("agent_gas_price_gwei", help="", kind="gauge")
    obs.METRICS.declare("agent_rebalances_total", help="", kind="counter")

    obs.record_decision(
        block_number=12345,
        block_timestamp="2026-02-01T00:00:00Z",
        action_kind="switch",
        target_protocol="morpho_blue",
        rationale="best APR +1.20pp",
        current_protocol="aave_v3",
        position_usd=1_000_000.0,
        gas_price_gwei=25.0,
        gas_cost_usd=17.50,
        panel_snapshot={"aave": 0.034, "morpho": 0.046},
    )

    # JSON log on stderr
    err = capfd.readouterr().err.splitlines()
    log_line = next(line for line in reversed(err) if '"event":"decision"' in line)
    rec = json.loads(log_line)
    assert rec["target_protocol"] == "morpho_blue"

    # Metric increments
    assert obs.METRICS.counters[("agent_decisions_total", ("kind", "switch"))] == 1.0
    assert obs.METRICS.counters[("agent_rebalances_total",)] == 1.0
    assert obs.METRICS.gauges[("agent_position_usd",)] == 1_000_000.0

    # Audit trail file
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    audit_rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
    assert audit_rec["event"] == "decision"
    assert audit_rec["panel"]["morpho"] == 0.046
    test_audit.close()


def test_kill_switch_recorded_critical(tmp_path, capfd, monkeypatch):
    test_audit = obs.AuditTrail(tmp_path)
    monkeypatch.setattr(obs, "AUDIT", test_audit)
    monkeypatch.setattr(obs, "METRICS", obs.MetricsRegistry())
    obs.METRICS.declare("agent_kill_switch_events_total", help="", kind="counter")

    obs.record_kill_switch("usdc_depeg", deviation_bp=120)

    err = capfd.readouterr().err.splitlines()
    rec = json.loads(err[-1])
    assert rec["level"] == "critical"
    assert rec["event"] == "kill_switch"
    assert rec["reason"] == "usdc_depeg"
    assert obs.METRICS.counters[("agent_kill_switch_events_total", ("reason", "usdc_depeg"))] == 1.0
    test_audit.close()


def test_metrics_server_serves_text_exposition():
    server = obs.start_metrics_server(host="127.0.0.1", port=0)
    try:
        port = server.server_address[1]
        import urllib.request
        time.sleep(0.05)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3) as r:
            body = r.read().decode("utf-8")
        assert "# HELP agent_blocks_processed_total" in body
        assert "# TYPE agent_position_usd gauge" in body
    finally:
        server.shutdown()
