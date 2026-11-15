"""Session statistics shared by runners, dashboard and reports."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

PASS, WARN, FAIL = "pass", "warn", "fail"

# outcome -> severity of a robustness test case
OUTCOMES = {
    "handled": PASS,        # server stayed up and kept the session (ignored or answered)
    "disconnected": WARN,   # server dropped the connection but is still reachable
    "accepted": WARN,       # server acknowledged a frame it should have rejected
    "unresponsive": FAIL,   # server stopped answering valid traffic afterwards
    "skipped": PASS,        # not representable in this protocol: not sent
}


@dataclass
class CaseResult:
    index: int
    name: str
    category: str
    description: str
    size: int
    payload_hex: str
    outcome: str = "handled"
    detail: str = ""
    latency_ms: Optional[float] = None

    @property
    def severity(self) -> str:
        return OUTCOMES.get(self.outcome, FAIL)


class Stats:
    def __init__(self, mode: str, meta: Optional[Dict[str, Any]] = None) -> None:
        self.mode = mode
        self.meta = meta or {}
        self.started_at = datetime.now(timezone.utc)
        self._t0 = time.monotonic()
        self._t_end: Optional[float] = None
        self.sent = 0
        self.bytes_sent = 0
        self.expected = 0          # frames for which an acknowledgement was expected
        self.acked = 0
        self.timeouts = 0
        self.bad_responses = 0
        self.connections_opened = 0
        self.active_connections = 0
        self.connect_errors = 0
        self.disconnects = 0
        self.latencies: List[float] = []            # seconds
        self.events: Deque[str] = deque(maxlen=10)  # shown live
        self.error_log: List[Dict[str, str]] = []   # exported (capped)
        self.cases: List[CaseResult] = []
        self.total_cases = 0
        self.findings: List[Dict[str, Any]] = []   # audit mode

    def add_finding(self, check: str, title: str, severity: str, status: str,
                    detail: str, evidence: Optional[List[str]] = None) -> Dict[str, Any]:
        finding = {"check": check, "title": title, "severity": severity, "status": status,
                   "detail": detail, "evidence": evidence or []}
        self.findings.append(finding)
        return finding

    # ------------------------------------------------------------ recording
    def record_sent(self, size: int, expect_response: bool = False) -> None:
        self.sent += 1
        self.bytes_sent += size
        if expect_response:
            self.expected += 1

    def record_ack(self, latency: float) -> None:
        self.acked += 1
        if len(self.latencies) < 500_000:
            self.latencies.append(latency)

    def record_timeout(self, who: str = "") -> None:
        self.timeouts += 1
        self.event("timeout", f"{who} no response from server")

    def record_bad_response(self, who: str = "") -> None:
        self.bad_responses += 1
        self.event("bad-response", f"{who} unexpected reply")

    def record_open(self) -> None:
        self.connections_opened += 1
        self.active_connections += 1

    def record_close(self) -> None:
        self.active_connections = max(0, self.active_connections - 1)

    def record_connect_error(self, who: str, error: str) -> None:
        self.connect_errors += 1
        self.event("connect-error", f"{who} {error}")

    def record_disconnect(self, who: str, error: str) -> None:
        self.disconnects += 1
        self.event("disconnect", f"{who} {error}")

    def event(self, kind: str, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.events.append(f"{stamp} [{kind}] {message}")
        if len(self.error_log) < 1000:
            self.error_log.append({"time": stamp, "kind": kind, "message": message})

    def finish(self) -> None:
        if self._t_end is None:
            self._t_end = time.monotonic()

    # -------------------------------------------------------------- reading
    @property
    def elapsed(self) -> float:
        return (self._t_end or time.monotonic()) - self._t0

    def latency_summary(self) -> Dict[str, Optional[float]]:
        """Latency statistics in milliseconds."""
        if not self.latencies:
            return {k: None for k in ("min", "avg", "p50", "p95", "p99", "max")}
        data = sorted(self.latencies)

        def pct(p: float) -> float:
            return data[min(len(data) - 1, int(round(p * (len(data) - 1))))] * 1000

        return {
            "min": data[0] * 1000,
            "avg": sum(data) / len(data) * 1000,
            "p50": pct(0.50),
            "p95": pct(0.95),
            "p99": pct(0.99),
            "max": data[-1] * 1000,
        }

    def outcome_counts(self) -> Dict[str, int]:
        counts = {name: 0 for name in OUTCOMES}
        for case in self.cases:
            counts[case.outcome] = counts.get(case.outcome, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        elapsed = self.elapsed
        result: Dict[str, Any] = {
            "tool": "gps-tester",
            "mode": self.mode,
            "started_at": self.started_at.isoformat(),
            "duration_seconds": round(elapsed, 3),
            "settings": self.meta,
            "summary": {
                "packets_sent": self.sent,
                "bytes_sent": self.bytes_sent,
                "ack_expected": self.expected,
                "ack_received": self.acked,
                "ack_rate_percent": round(100 * self.acked / self.expected, 2) if self.expected else None,
                "timeouts": self.timeouts,
                "unexpected_responses": self.bad_responses,
                "connections_opened": self.connections_opened,
                "connect_errors": self.connect_errors,
                "disconnects": self.disconnects,
                "packets_per_second": round(self.sent / elapsed, 2) if elapsed > 0 else 0,
            },
            "latency_ms": {k: (round(v, 3) if v is not None else None)
                           for k, v in self.latency_summary().items()},
            "events": self.error_log,
        }
        if self.mode in ("validate", "audit") and self.cases:
            result["outcomes"] = self.outcome_counts()
            result["cases"] = [dict(asdict(c), severity=c.severity) for c in self.cases]
        if self.mode == "audit":
            result["findings"] = self.findings
        return result
