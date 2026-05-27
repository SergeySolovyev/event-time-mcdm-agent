"""Tier 5 observability for the Vault Agent.

Three building blocks an institutional operator expects:

1. **Structured JSON logger** (`json_log`): every event emits a single
   JSON-line with stable schema {ts, level, event, ...kwargs}.  No human
   "f-strings" — log shippers (Datadog/Loki/CloudWatch) parse the JSON
   directly.

2. **Prometheus-format /metrics endpoint** (`MetricsRegistry` + tiny
   stdlib HTTP server): exposes counters and gauges over plain HTTP.
   No `prometheus_client` dependency — we hand-format the text exposition
   (~30 LOC) to keep the agent's image surface small.

3. **Append-only decision audit trail** (`AuditTrail`): each block's
   (decision, rationale, panel snapshot, gas cost) is written as a JSON
   line to `agent/state/audit/YYYY-MM-DD.jsonl`.  Daily rotation keeps
   files grep-friendly while preserving the full forensic chain LPs
   can review.  Designed to be tar.gz-archivable into long-term cold
   storage every month.

The three primitives are designed to be **synchronous and fast**:
* `json_log` writes one line to stderr (~5µs).
* `MetricsRegistry.inc / .set / .observe` mutates a dict (~100ns).
* `AuditTrail.append` writes one fsync'd JSON line (~50-200µs).

All three are thread-safe via a single module-level `RLock`. The agent's
`per_block_loop.py` runs single-threaded, but the HTTP /metrics server
runs in a daemon thread reading the registry concurrently.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_AGENT_INSTANCE_ID = os.environ.get("AGENT_INSTANCE_ID", f"agent-{os.getpid()}")


# ---------------- JSON logger ---------------- #

def json_log(event: str, *, level: str = "info", **fields: Any) -> None:
    """Emit one JSON line to stderr with stable schema.

    Schema:
        ts         : ISO-8601 UTC (millisecond resolution)
        level      : info | warn | error | critical
        instance   : agent-{PID} or env AGENT_INSTANCE_ID
        event      : short snake_case event name
        ...        : arbitrary additional keys (passed as kwargs)

    Never raises — observability MUST NOT break the decision loop.
    """
    try:
        record: dict[str, Any] = {
            "ts": _iso_now_ms(),
            "level": level,
            "instance": _AGENT_INSTANCE_ID,
            "event": event,
        }
        record.update(fields)
        with _LOCK:
            sys.stderr.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
            sys.stderr.flush()
    except Exception:
        pass  # never let observability bring down the agent


def _iso_now_ms() -> str:
    t = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
    ms = int((t - int(t)) * 1000)
    return f"{base}.{ms:03d}Z"


# ---------------- Prometheus metrics ---------------- #

@dataclass
class MetricsRegistry:
    """In-memory metric store with Prometheus text exposition.

    Three kinds:
        counter   : monotonic; .inc(label_dict, value)
        gauge     : settable; .set(label_dict, value)
        histogram : observe-into-buckets; .observe(label_dict, value)

    Label dict is converted to a stable string key on each mutation.
    """
    counters: dict[tuple, float] = field(default_factory=dict)
    gauges: dict[tuple, float] = field(default_factory=dict)
    histograms: dict[tuple, list[float]] = field(default_factory=dict)
    # name -> help text for the exposition format
    help_text: dict[str, str] = field(default_factory=dict)
    metric_type: dict[str, str] = field(default_factory=dict)

    def _key(self, name: str, labels: dict[str, str] | None) -> tuple:
        if not labels:
            return (name,)
        return (name,) + tuple(sorted(labels.items()))

    def declare(self, name: str, *, help: str, kind: str) -> None:
        """Register name/help text + metric kind. Idempotent."""
        with _LOCK:
            self.help_text[name] = help
            self.metric_type[name] = kind

    def inc(self, name: str, labels: dict[str, str] | None = None, *, value: float = 1.0) -> None:
        with _LOCK:
            key = self._key(name, labels)
            self.counters[key] = self.counters.get(key, 0.0) + value

    def set(self, name: str, labels: dict[str, str] | None = None, *, value: float = 0.0) -> None:
        with _LOCK:
            self.gauges[self._key(name, labels)] = value

    def observe(self, name: str, labels: dict[str, str] | None = None, *, value: float = 0.0) -> None:
        with _LOCK:
            self.histograms.setdefault(self._key(name, labels), []).append(value)

    def render(self) -> str:
        """Format all metrics as a Prometheus text exposition document.

        See <https://prometheus.io/docs/instrumenting/exposition_formats/>.
        """
        with _LOCK:
            lines: list[str] = []
            written: set[str] = set()
            # Emit HELP/TYPE for every declared metric (Prometheus convention:
            # exposition includes declarations even before first sample).
            for name in sorted(self.help_text):
                lines.append(f"# HELP {name} {self.help_text[name]}")
                lines.append(f"# TYPE {name} {self.metric_type.get(name, 'gauge')}")
                written.add(name)
            for store, kind in (
                (self.counters, "counter"),
                (self.gauges, "gauge"),
                (self.histograms, "summary"),
            ):
                for key, val in store.items():
                    name = key[0]
                    labels_part = ",".join(f'{k}="{v}"' for k, v in key[1:])
                    if name not in written:
                        lines.append(f"# HELP {name} {self.help_text.get(name, '')}")
                        lines.append(f"# TYPE {name} {self.metric_type.get(name, kind)}")
                        written.add(name)
                    if isinstance(val, list):
                        # Summary: emit count + sum + p50/p95/p99 if non-empty
                        if val:
                            v = sorted(val)
                            n = len(v)
                            label_prefix = f"{{{labels_part}}}" if labels_part else ""
                            lines.append(f"{name}_count{label_prefix} {n}")
                            lines.append(f"{name}_sum{label_prefix} {sum(v)}")
                            lines.append(f'{name}{{{labels_part + "," if labels_part else ""}quantile="0.5"}} {v[n // 2]}')
                            lines.append(f'{name}{{{labels_part + "," if labels_part else ""}quantile="0.95"}} {v[min(int(0.95 * n), n - 1)]}')
                            lines.append(f'{name}{{{labels_part + "," if labels_part else ""}quantile="0.99"}} {v[min(int(0.99 * n), n - 1)]}')
                    else:
                        label_part = f"{{{labels_part}}}" if labels_part else ""
                        lines.append(f"{name}{label_part} {val}")
            return "\n".join(lines) + "\n"


METRICS = MetricsRegistry()

# Declare the agent's canonical metric vocabulary up-front so /metrics
# is well-defined even before any event has fired.
METRICS.declare("agent_blocks_processed_total", help="Blocks consumed by the per-block loop", kind="counter")
METRICS.declare("agent_decisions_total", help="Decisions emitted by the policy", kind="counter")
METRICS.declare("agent_rebalances_total", help="Rebalance txs successfully submitted", kind="counter")
METRICS.declare("agent_kill_switch_events_total", help="Kill-switch triggers (manual or auto)", kind="counter")
METRICS.declare("agent_position_usd", help="Current position USD value", kind="gauge")
METRICS.declare("agent_block_lag_blocks", help="Number of blocks behind chain head", kind="gauge")
METRICS.declare("agent_gas_price_gwei", help="Last-seen gas price (gwei)", kind="gauge")
METRICS.declare("agent_uptime_seconds", help="Seconds since agent process start", kind="gauge")
METRICS.declare("agent_decision_latency_seconds", help="Per-block decide() wall-clock seconds", kind="summary")


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — http.server contract
        if self.path == "/metrics":
            body = METRICS.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args, **kwargs) -> None:  # silence stdout chatter
        pass


def start_metrics_server(*, host: str = "0.0.0.0", port: int = 9090) -> ThreadingHTTPServer:
    """Spin up the /metrics HTTP server in a daemon thread."""
    server = ThreadingHTTPServer((host, port), _MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="metrics-http")
    thread.start()
    json_log("metrics_server_started", host=host, port=port)
    return server


# ---------------- Audit trail ---------------- #

class AuditTrail:
    """Append-only daily-rotated JSONL of every decision.

    One file per UTC day, named YYYY-MM-DD.jsonl, under `dir/`. Lines
    are sorted by timestamp (single-writer guarantee). fsync after each
    write because financial-decision audit trails must survive crashes.
    """

    def __init__(self, dir_path: Path) -> None:
        self.dir = Path(dir_path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fp = None
        self._current_day: str | None = None
        self._lock = threading.Lock()

    def _file_for_today(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self._current_day:
            if self._fp is not None:
                self._fp.close()
            self._fp = open(self.dir / f"{day}.jsonl", "a", encoding="utf-8")
            self._current_day = day
        return self._fp

    def append(self, **fields: Any) -> None:
        """Append one JSON line, fsync, never raise."""
        try:
            with self._lock:
                rec: dict[str, Any] = {"ts": _iso_now_ms(), "instance": _AGENT_INSTANCE_ID}
                rec.update(fields)
                fp = self._file_for_today()
                fp.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
                fp.flush()
                os.fsync(fp.fileno())
        except Exception as e:
            json_log("audit_write_failed", level="error", err=str(e))

    def close(self) -> None:
        with self._lock:
            if self._fp is not None:
                self._fp.close()
                self._fp = None
                self._current_day = None


DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent / "state" / "audit"
AUDIT = AuditTrail(DEFAULT_AUDIT_DIR)


# ---------------- Public helpers ---------------- #

def record_decision(
    *,
    block_number: int,
    block_timestamp: str,
    action_kind: str,
    target_protocol: str | None,
    rationale: str,
    current_protocol: str | None,
    position_usd: float,
    gas_price_gwei: float,
    gas_cost_usd: float,
    panel_snapshot: dict[str, float],
) -> None:
    """One-call sink for every decision the policy emits.

    Touches all three observability surfaces: structured log + metrics +
    audit trail. Cost ~250 µs per call, dominated by audit fsync.
    """
    json_log(
        "decision",
        block_number=block_number,
        action_kind=action_kind,
        target_protocol=target_protocol,
        rationale=rationale,
        position_usd=position_usd,
        gas_cost_usd=gas_cost_usd,
    )
    METRICS.inc("agent_decisions_total", {"kind": action_kind})
    METRICS.set("agent_position_usd", value=position_usd)
    METRICS.set("agent_gas_price_gwei", value=gas_price_gwei)
    if action_kind == "switch":
        METRICS.inc("agent_rebalances_total")
    AUDIT.append(
        event="decision",
        block_number=block_number,
        block_timestamp=block_timestamp,
        action_kind=action_kind,
        target_protocol=target_protocol,
        rationale=rationale,
        current_protocol=current_protocol,
        position_usd=position_usd,
        gas_price_gwei=gas_price_gwei,
        gas_cost_usd=gas_cost_usd,
        panel=panel_snapshot,
    )


def record_kill_switch(reason: str, **details: Any) -> None:
    json_log("kill_switch", level="critical", reason=reason, **details)
    METRICS.inc("agent_kill_switch_events_total", {"reason": reason})
    AUDIT.append(event="kill_switch", reason=reason, **details)
