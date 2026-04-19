"""
metrics_exporter.py — Team Daniel Token Telemetri
==================================================
Exporterar Claude Code session-data som Prometheus-metrics via HTTP.

Kör en HTTP-server på port 8000 (Prometheus scrape-target) och
re-scannar JSONL-filer var 60:e sekund.

Metrics:
  claude_tokens_total{agent, project, type}   — counter (input/output/cache_read/cache_write)
  claude_cost_usd_total{agent, project}        — counter
  claude_sessions_total{agent, project}        — counter

Starta: python metrics_exporter.py
        python metrics_exporter.py --port 8000 --interval 60
"""

import argparse
import logging
import time
import threading
from pathlib import Path
from typing import Optional

from prometheus_client import (
    Counter,
    CollectorRegistry,
    REGISTRY,
    start_http_server,
    Gauge,
)

from claude_session_parser import ClaudeSessionParser, SessionStats

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("metrics_exporter")


# ─── Prometheus-metrics (module-level, skapas en gång) ───────────────────────
#
# Vi använder Gauge istället för Counter eftersom vi läser historisk data
# och måste kunna "uppdatera" värden utan att prometheus_client klagar på
# att counters bara ökar. Gauges visar ackumulerade totalsummor.
#
# I Grafana-queries används dessa som "current value" (lastNotNull / max),
# vilket ger rätt totalsummor för alla time ranges.

_token_gauge = Gauge(
    "claude_tokens_total",
    "Totalt antal tokens per agent, projekt och tokentyp",
    labelnames=["agent", "project", "type"],
)

_cost_gauge = Gauge(
    "claude_cost_usd_total",
    "Uppskattad total kostnad i USD per agent och projekt",
    labelnames=["agent", "project"],
)

_sessions_gauge = Gauge(
    "claude_sessions_total",
    "Antal sessioner per agent och projekt",
    labelnames=["agent", "project"],
)


# ─── MetricsExporter ─────────────────────────────────────────────────────────

class MetricsExporter:
    """
    Scannar Claude Code JSONL-sessioner och uppdaterar Prometheus-metrics.

    Args:
        claude_dir:  Sökväg till .claude/ (default: ~/.claude/)
        port:        HTTP-port för Prometheus scrape (default: 8000)
        interval:    Sekunder mellan re-scan (default: 60)
    """

    def __init__(
        self,
        claude_dir: Optional[Path] = None,
        port: int = 8000,
        interval: int = 60,
    ):
        self.parser = ClaudeSessionParser(claude_dir)
        self.port = port
        self.interval = interval
        self._stop_event = threading.Event()

    # ── Publik API ────────────────────────────────────────────────────────────

    def start(self):
        """Starta HTTP-server och börja exponera metrics."""
        log.info(f"Startar Prometheus metrics-server på port {self.port}")
        start_http_server(self.port)
        log.info(f"Metrics tillgängliga på http://localhost:{self.port}/metrics")

        # Initial scan direkt vid start
        self._update_metrics()

        # Schemalägg regelbundna uppdateringar
        self._schedule_loop()

    def stop(self):
        """Stoppa bakgrunds-loopen."""
        self._stop_event.set()

    # ── Intern logik ──────────────────────────────────────────────────────────

    def _schedule_loop(self):
        """Kör _update_metrics var {interval}:e sekund tills stop() kallas."""
        while not self._stop_event.wait(self.interval):
            self._update_metrics()

    def _update_metrics(self):
        """Scanna alla sessioner och uppdatera Prometheus-gauges."""
        log.info("Scannar Claude Code sessioner...")
        try:
            sessions = self.parser.parse_all_sessions()
            aggregated = self.parser.aggregate_by_agent(sessions)

            if not sessions:
                log.warning("Inga sessioner med usage-data hittades")
                return

            log.info(f"Hittade {len(sessions)} sessioner från {len(aggregated)} agenter")

            for agent_name, data in aggregated.items():
                project = data.get("project", "unknown")
                agent = agent_name

                # Token-gauges per typ
                _token_gauge.labels(agent=agent, project=project, type="input").set(
                    data["input_tokens"]
                )
                _token_gauge.labels(agent=agent, project=project, type="output").set(
                    data["output_tokens"]
                )
                _token_gauge.labels(agent=agent, project=project, type="cache_write").set(
                    data["cache_creation_tokens"]
                )
                _token_gauge.labels(agent=agent, project=project, type="cache_read").set(
                    data["cache_read_tokens"]
                )

                # Kostnad
                _cost_gauge.labels(agent=agent, project=project).set(
                    data["estimated_cost_usd"]
                )

                # Sessionräknare
                _sessions_gauge.labels(agent=agent, project=project).set(
                    data["session_count"]
                )

            log.info("Metrics uppdaterade")

        except Exception as exc:
            log.error(f"Fel vid uppdatering av metrics: {exc}", exc_info=True)


# ─── Startpunkt ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Exportera Claude Code token-metrics till Prometheus"
    )
    ap.add_argument("--port", type=int, default=8000, help="HTTP-port (default: 8000)")
    ap.add_argument(
        "--interval", type=int, default=60, help="Scan-intervall i sekunder (default: 60)"
    )
    ap.add_argument(
        "--claude-dir",
        type=Path,
        default=None,
        help="Sökväg till .claude/ (default: ~/.claude/)",
    )
    args = ap.parse_args()

    exporter = MetricsExporter(
        claude_dir=args.claude_dir,
        port=args.port,
        interval=args.interval,
    )

    try:
        exporter.start()
        # Håll huvudtråden vid liv
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stoppar exporter...")
        exporter.stop()


if __name__ == "__main__":
    main()
