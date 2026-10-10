"""Local transcript persistence and aggregation.

Every inbound/outbound message is appended as one JSON line to
``transcripts/chat-YYYY-MM-DD.jsonl`` so we always keep our own history,
independent of Lark's server and of the agent CLI's (often non-persisted)
session. All functions here are pure I/O over the transcript directory and
carry no Lark dependency, which keeps them easy to unit-test.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()


class TranscriptStore:
    """Append-only JSONL transcript keyed by date, plus read helpers."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _today_path(self) -> Path:
        return self.directory / f"chat-{time.strftime('%Y-%m-%d')}.jsonl"

    def log(self, direction: str, *, message_id: str = "", msg_type: str = "",
            sender: str = "", text: str = "", raw: object = None) -> None:
        """Append one event to today's transcript. Never raises (best-effort)."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "direction": direction,   # "in" (from user) or "out" (bot reply)
                "message_id": message_id,
                "msg_type": msg_type,
                "sender": sender,
                "text": text,
                "raw": raw,               # raw Lark content (helps debug new formats)
            }
            line = json.dumps(entry, ensure_ascii=False)
            with _lock:
                with open(self._today_path(), "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as exc:  # logging must never break the bot
            logger.warning("transcript log failed: %r", exc)

    def _read_today_lines(self) -> list[str]:
        path = self._today_path()
        if not path.exists():
            return []
        try:
            return [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        except Exception:
            return []

    def recent_context(self, n: int = 20) -> str:
        """Format the *n* most recent entries as a cross-engine context preamble.

        Returns "" if there is nothing useful or on any error.
        """
        if n <= 0:
            return ""
        lines = self._read_today_lines()
        if not lines:
            return ""
        parts: list[str] = []
        for raw_line in lines[-n:]:
            try:
                entry = json.loads(raw_line)
            except ValueError:
                continue
            direction = entry.get("direction", "?")
            text = (entry.get("text") or "").strip()
            if not text:
                continue
            ts = entry.get("ts", "")
            short_ts = ts[11:16] if len(ts) >= 16 else ts
            if direction == "in":
                parts.append(f"[{short_ts}] User: {text}")
            elif direction == "out":
                preview = text if len(text) <= 500 else text[:500] + " …(truncated)"
                parts.append(f"[{short_ts}] Bot: {preview}")
        if not parts:
            return ""
        header = (
            "=== Recent conversation context (from previous engine) ===\n"
            "The following is a summary of the recent chat so you have continuity.\n"
        )
        return header + "\n".join(parts) + "\n=== End of context ===\n"

    def usage_stats(self) -> dict:
        """Aggregate message counts for today and across all transcript files."""
        stats = {
            "today_in": 0,
            "today_out": 0,
            "today_total": 0,
            "total_files": 0,
            "all_time_lines": 0,
        }
        for line in self._read_today_lines():
            stats["today_total"] += 1
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("direction") == "in":
                stats["today_in"] += 1
            elif entry.get("direction") == "out":
                stats["today_out"] += 1

        if self.directory.exists():
            for f in self.directory.glob("chat-*.jsonl"):
                stats["total_files"] += 1
                try:
                    stats["all_time_lines"] += sum(
                        1 for line in f.read_text(encoding="utf-8").splitlines() if line.strip()
                    )
                except Exception:
                    pass
        return stats

    def preview_entries(self, n: int = 5) -> list[str]:
        """Return the last *n* entries formatted for the ``/transcript`` command."""
        lines = self._read_today_lines()
        if not lines:
            return []
        recent = lines[-n:] if n > 0 else lines
        formatted: list[str] = []
        for line in recent:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ts = e.get("ts", "")[11:16]
            who = "👤 User" if e.get("direction") == "in" else "🤖 Bot"
            txt = (e.get("text") or "").strip()
            preview = txt if len(txt) <= 120 else txt[:120] + "..."
            formatted.append(f"[{ts}] {who}: {preview}")
        return formatted
