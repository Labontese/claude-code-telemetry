"""
claude_session_parser.py — Team Daniel Token Telemetri
=======================================================
Läser Claude Code:s lokala JSONL-sessionsdata och extraherar
token-usage per agent utan att läsa meddelandeinnehåll.

Datasources:
  ~/.claude/projects/[projekt-path]/[guid].jsonl           — root-sessions
  ~/.claude/projects/[projekt-path]/[guid]/subagents/agent-[id].jsonl  — subagent-sessions
  ~/.claude/projects/[projekt-path]/[guid]/subagents/agent-[id].meta.json — agent-metadata

Privacy: Läser ALDRIG meddelandeinnehåll — bara usage-metadata.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

# ─── Prismodell (Claude Sonnet 4.6) ──────────────────────────────────────────
# USD per token
PRICING = {
    "input":       3.0    / 1_000_000,   # $3/1M
    "output":      15.0   / 1_000_000,   # $15/1M
    "cache_write": 3.75   / 1_000_000,   # $3.75/1M
    "cache_read":  0.30   / 1_000_000,   # $0.30/1M
}


# ─── Data-modell ──────────────────────────────────────────────────────────────

@dataclass
class SessionStats:
    """Token-statistik för en enskild session."""
    session_id: str
    project: str
    agent_name: str            # "Daniel", "Nova", "Wilma" etc.
    agent_type: str            # "user", "general-purpose", "Explore" etc.
    timestamp: datetime
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    total_tokens: int          # input + output
    estimated_cost_usd: float  # baserat på Sonnet 4.6-priser


# ─── ClaudeSessionParser ──────────────────────────────────────────────────────

class ClaudeSessionParser:
    """
    Parser för Claude Code:s lokala JSONL-sessionsdata.

    Scannar ~/.claude/projects/ rekursivt och extraherar token-usage
    från assistant-meddelanden — utan att läsa prompt-/response-text.

    Användning:
        parser = ClaudeSessionParser()
        sessions = parser.parse_all_sessions()
        aggregated = parser.aggregate_by_agent(sessions)
    """

    def __init__(self, claude_dir: Optional[Path] = None):
        """
        Args:
            claude_dir: Sökväg till .claude/-mappen.
                        Default: ~/.claude/
        """
        self.claude_dir = claude_dir or Path.home() / ".claude"
        self.projects_dir = self.claude_dir / "projects"

    # ── Publik API ────────────────────────────────────────────────────────────

    def parse_all_sessions(self) -> list[SessionStats]:
        """
        Scanna alla projekt och returnera lista med SessionStats.
        Hanterar saknade fält och JSON-fel utan att krascha.
        """
        results: list[SessionStats] = []

        if not self.projects_dir.exists():
            return results

        for jsonl_file in self._find_session_files():
            stats = self._parse_session_file(jsonl_file)
            if stats is not None:
                results.append(stats)

        return results

    def aggregate_by_agent(
        self, sessions: list[SessionStats]
    ) -> dict[str, dict]:
        """
        Aggregera session-stats per agent.

        Returns:
            Dict med agent_name som nyckel och aggregerad statistik som värde.
        """
        aggregated: dict[str, dict] = {}

        for s in sessions:
            key = s.agent_name
            if key not in aggregated:
                aggregated[key] = {
                    "agent_name": s.agent_name,
                    "agent_type": s.agent_type,
                    "project": s.project,
                    "session_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                    "total_tokens": 0,
                    "estimated_cost_usd": 0.0,
                    "last_seen": s.timestamp,
                }
            agg = aggregated[key]
            agg["session_count"] += 1
            agg["input_tokens"] += s.input_tokens
            agg["output_tokens"] += s.output_tokens
            agg["cache_creation_tokens"] += s.cache_creation_tokens
            agg["cache_read_tokens"] += s.cache_read_tokens
            agg["total_tokens"] += s.total_tokens
            agg["estimated_cost_usd"] += s.estimated_cost_usd
            if s.timestamp > agg["last_seen"]:
                agg["last_seen"] = s.timestamp
                agg["project"] = s.project  # senaste projekt

        return aggregated

    # ── Intern filsökning ─────────────────────────────────────────────────────

    def _find_session_files(self) -> Iterator[Path]:
        """
        Hitta alla JSONL-sessionsfiler rekursivt.
        Returnerar generator för minneseffektiv hantering.
        """
        # Root-sessions: [projekt-path]/[guid].jsonl
        for jsonl_file in self.projects_dir.rglob("*.jsonl"):
            # Skippa subagent-filer i denna iteration (hanteras nedan)
            if "subagents" not in jsonl_file.parts:
                yield jsonl_file

        # Subagent-sessions: [guid]/subagents/agent-[id].jsonl
        for jsonl_file in self.projects_dir.rglob("subagents/agent-*.jsonl"):
            yield jsonl_file

    # ── Parsning av enskild session ───────────────────────────────────────────

    def _parse_session_file(self, jsonl_file: Path) -> Optional[SessionStats]:
        """
        Läs en JSONL-fil och extrahera aggregerad token-usage.
        Returnerar None om filen inte innehåller användbar data.
        """
        is_subagent = "subagents" in jsonl_file.parts

        # Identifiera agent
        if is_subagent:
            agent_name, agent_type = self._load_agent_meta(jsonl_file)
        else:
            agent_name = "Daniel"
            agent_type = "user"

        # Identifiera projekt
        project = self._extract_project_name(jsonl_file)

        # Läs token-usage rad för rad
        total_input = 0
        total_output = 0
        total_cache_creation = 0
        total_cache_read = 0
        last_timestamp = datetime.now()
        has_data = False

        try:
            with jsonl_file.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # Skippa korrupta rader

                    # Extrahera timestamp
                    ts = self._extract_timestamp(record)
                    if ts:
                        last_timestamp = ts

                    # Bara assistant-meddelanden har usage
                    usage = self._extract_usage(record)
                    if usage is None:
                        continue

                    total_input += usage.get("input_tokens", 0) or 0
                    total_output += usage.get("output_tokens", 0) or 0
                    total_cache_creation += (
                        usage.get("cache_creation_input_tokens", 0) or 0
                    )
                    total_cache_read += (
                        usage.get("cache_read_input_tokens", 0) or 0
                    )
                    has_data = True

        except (OSError, PermissionError):
            return None

        if not has_data:
            return None

        total_tokens = total_input + total_output
        estimated_cost = (
            total_input * PRICING["input"]
            + total_output * PRICING["output"]
            + total_cache_creation * PRICING["cache_write"]
            + total_cache_read * PRICING["cache_read"]
        )

        return SessionStats(
            session_id=jsonl_file.stem,
            project=project,
            agent_name=agent_name,
            agent_type=agent_type,
            timestamp=last_timestamp,
            input_tokens=total_input,
            output_tokens=total_output,
            cache_creation_tokens=total_cache_creation,
            cache_read_tokens=total_cache_read,
            total_tokens=total_tokens,
            estimated_cost_usd=round(estimated_cost, 6),
        )

    # ── Hjälpmetoder ──────────────────────────────────────────────────────────

    def _load_agent_meta(self, jsonl_file: Path) -> tuple[str, str]:
        """
        Läs .meta.json för en subagent och extrahera namn och typ.

        Namngivningsregler:
        - description "Wilma tar bort..." → "Wilma" (första ordet)
        - description "Anna researchar..." → "Anna" (första ordet)
        - Fallback: agentType-värdet
        """
        meta_file = jsonl_file.with_suffix(".meta.json")
        agent_type = "general-purpose"
        agent_name = "Agent"

        if meta_file.exists():
            try:
                with meta_file.open("r", encoding="utf-8", errors="replace") as f:
                    meta = json.load(f)
                agent_type = meta.get("agentType", "general-purpose") or "general-purpose"

                description = meta.get("description", "") or ""
                if description.strip():
                    # Första ordet i description = agentens namn
                    first_word = description.strip().split()[0]
                    # Ta bort eventuella specialtecken
                    first_word = re.sub(r"[^A-Za-zÅÄÖåäö]", "", first_word)
                    if first_word:
                        agent_name = first_word
                    else:
                        agent_name = agent_type
                else:
                    agent_name = agent_type

            except (json.JSONDecodeError, OSError):
                pass

        return agent_name, agent_type

    def _extract_project_name(self, jsonl_file: Path) -> str:
        """
        Extrahera ett läsbart projektnamn från filsökvägen.

        Sökvägsstruktur: .../projects/[encoded-path]/...
        Encoded path är projektets absoluta sökväg med / ersatt av -
        """
        try:
            # Hitta "projects"-delen i sökvägen
            parts = jsonl_file.parts
            projects_idx = None
            for i, p in enumerate(parts):
                if p == "projects":
                    projects_idx = i
                    break

            if projects_idx is not None and projects_idx + 1 < len(parts):
                encoded = parts[projects_idx + 1]
                # Dekoda: ta sista segmentet (projektmappens namn)
                # Typiskt format: "-D--Team-Daniel-team-telemetry"
                # → ta sista komponenten efter sista "-"
                segments = encoded.rstrip("-").split("-")
                # Filtrera bort tomma och ta de sista meningsfulla segmenten
                non_empty = [s for s in segments if s]
                if non_empty:
                    # Ta de 2 sista segmenten som projektnamn
                    return "-".join(non_empty[-2:]) if len(non_empty) >= 2 else non_empty[-1]
        except Exception:
            pass

        return "unknown"

    def _extract_timestamp(self, record: dict) -> Optional[datetime]:
        """Extrahera timestamp från en JSONL-rad."""
        for key in ("timestamp", "created_at", "ts", "time"):
            val = record.get(key)
            if val:
                try:
                    if isinstance(val, (int, float)):
                        return datetime.fromtimestamp(val)
                    return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                except (ValueError, OSError):
                    continue
        return None

    def _extract_usage(self, record: dict) -> Optional[dict]:
        """
        Extrahera usage-dict från ett assistant-meddelande.
        Returnerar None om raden inte är ett assistant-meddelande med usage.

        Läser ALDRIG meddelandeinnehåll (content/text/prompt).
        """
        # Typisk struktur: { "type": "assistant", "message": { "usage": {...} } }
        # Alternativ: { "role": "assistant", "usage": {...} }

        # Kolla direkt usage på root-nivå
        if "usage" in record:
            usage = record["usage"]
            if isinstance(usage, dict) and (
                "input_tokens" in usage or "output_tokens" in usage
            ):
                return usage

        # Kolla nested: record.message.usage
        message = record.get("message") or record.get("msg")
        if isinstance(message, dict):
            usage = message.get("usage")
            if isinstance(usage, dict) and (
                "input_tokens" in usage or "output_tokens" in usage
            ):
                # Verifiera att det är ett assistant-meddelande
                role = message.get("role", "") or record.get("type", "")
                if role in ("assistant", "") or record.get("type") == "assistant":
                    return usage

        return None


# ─── CLI för felsökning ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    parser = ClaudeSessionParser()
    print(f"Scannar: {parser.projects_dir}")

    sessions = parser.parse_all_sessions()
    print(f"Hittade {len(sessions)} sessioner med usage-data\n")

    if not sessions:
        print("Inga sessioner hittades. Kontrollera att ~/.claude/projects/ finns.")
        sys.exit(0)

    aggregated = parser.aggregate_by_agent(sessions)

    print(f"{'Agent':<15} {'Typ':<20} {'Input':>10} {'Output':>10} {'Cache-R':>10} {'Kostnad':>12}")
    print("-" * 80)
    for agent, data in sorted(aggregated.items(), key=lambda x: -x[1]["total_tokens"]):
        print(
            f"{data['agent_name']:<15} {data['agent_type']:<20} "
            f"{data['input_tokens']:>10,} {data['output_tokens']:>10,} "
            f"{data['cache_read_tokens']:>10,} ${data['estimated_cost_usd']:>11.4f}"
        )
    print("-" * 80)
    total_cost = sum(d["estimated_cost_usd"] for d in aggregated.values())
    total_tokens = sum(d["total_tokens"] for d in aggregated.values())
    print(f"{'TOTALT':<15} {'':<20} {total_tokens:>10,} {'':>10} {'':>10} ${total_cost:>11.4f}")
