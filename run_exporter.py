"""
run_exporter.py — Startar metrics_exporter och håller igång
============================================================
Enkelt startskript för Team Daniel Token Telemetri.

Kör: python run_exporter.py
     python run_exporter.py --port 8000 --interval 60
"""

from metrics_exporter import main

if __name__ == "__main__":
    main()
