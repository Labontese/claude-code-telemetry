#!/usr/bin/env python3
"""
export_dashboard.py — Exportera Team Daniel Grafana-dashboard

Hämtar dashboard-JSON via Grafana API och sparar en portabel version
som kan importeras i vilken Grafana-instans som helst.

Användning:
    python export_dashboard.py
    python export_dashboard.py --output my-dashboard.json
    python export_dashboard.py --url http://localhost:3002 --user admin --password admin
"""
import argparse
import json
import sys
import urllib.request
import urllib.error
import base64
from pathlib import Path

DEFAULT_URL = "http://localhost:3002"
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "admin"
DASHBOARD_UID = "team-daniel-tokens"

def fetch_dashboard(base_url, user, password):
    url = f"{base_url}/api/dashboards/uid/{DASHBOARD_UID}"
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {credentials}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def make_portable(dashboard):
    # Ta bort id och version så det kan importeras som nytt
    dashboard.pop("id", None)
    dashboard.pop("version", None)
    # Normalisera datasource-UIDs till variabel
    text = json.dumps(dashboard)
    text = text.replace('"uid": "prometheus-team-daniel"', '"uid": "${DS_PROMETHEUS}"')
    return json.loads(text)

def main():
    parser = argparse.ArgumentParser(description="Exportera Team Daniel Grafana-dashboard")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--output", default="team-tokens-export.json")
    args = parser.parse_args()

    print(f"Hämtar dashboard från {args.url}...")
    try:
        data = fetch_dashboard(args.url, args.user, args.password)
    except urllib.error.HTTPError as e:
        print(f"FEL: HTTP {e.code} — {e.reason}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"FEL: Kan inte nå Grafana — {e.reason}")
        sys.exit(1)

    dashboard = make_portable(data["dashboard"])
    output = {
        "__inputs": [{"name": "DS_PROMETHEUS", "label": "Prometheus", "type": "datasource", "pluginId": "prometheus"}],
        "__requires": [{"type": "grafana", "id": "grafana", "name": "Grafana", "version": "10.0.0"}],
        **dashboard
    }

    out_path = Path(args.output)
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Exporterat till: {out_path.resolve()}")
    print(f"Importera i Grafana: Dashboards → Import → Upload JSON-fil")

if __name__ == "__main__":
    main()
