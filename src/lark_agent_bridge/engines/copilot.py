"""GitHub Copilot CLI (copilot) engine adapter.

Drives the standalone GitHub Copilot CLI (``copilot``, not the ``gh copilot``
extension) in non-interactive mode with JSONL output.

Differences from kiro / agy:
- prompt is passed via ``-p/--prompt <text>`` (non-interactive, exits after)
- resume via ``--resume=<session-id>``
- auto-trust is ``--allow-all-tools`` (REQUIRED for non-interactive mode)
- stream schema is ``{"type": ..., "data": {...}}`` (one JSON object per line):
  the final answer arrives in an ``assistant.message`` event's ``content``
  (already the whole message, not a delta); the run ends with a top-level
  ``result`` event carrying ``sessionId`` and ``exitCode``. Many events are
  ``ephemeral`` (mcp status, usage, deltas) and are ignored.

Reads mutable options (model, effort, agent_name) from ``settings`` at build
time so runtime slash-command changes take effect without rebuilding.
"""

from __future__ import annotations

import json

from ..config import Settings
from .base import Engine


class CopilotEngine(Engine):
    """Drives the GitHub Copilot CLI with ``--output-format json``."""

    name = "copilot"

    def __init__(self, binary: str, settings: Settings):
        super().__init__(binary)
        self.settings = settings

    def build_cmd(self, prompt: str, session_id: str | None) -> list[str]:
        # --allow-all-tools is required for non-interactive (-p) mode.
        cmd = [self.binary, "--output-format", "json",
               "--allow-all-tools", "--no-color"]
        if self.settings.agent_name:
            cmd += ["--agent", self.settings.agent_name]
        if self.settings.copilot_model:
            cmd += ["--model", self.settings.copilot_model]
        if self.settings.copilot_effort:
            cmd += ["--reasoning-effort", self.settings.copilot_effort]
        if session_id:
            cmd.append(f"--resume={session_id}")
        cmd += ["-p", prompt]
        return cmd

    def parse(self, stdout: str) -> tuple[list[str], str | None, bool]:
        sid: str | None = None
        ok = True
        have_result = False
        segments: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            etype = event.get("type")
            if etype == "assistant.message":
                # Non-ephemeral: carries the whole message text for this turn.
                content = (event.get("data") or {}).get("content")
                if isinstance(content, str) and content.strip():
                    segments.append(content.strip())
            elif etype == "result":
                have_result = True
                sid = event.get("sessionId") or sid
                ok = event.get("exitCode", 0) == 0
        # If the final result event never arrived the run was interrupted.
        if not have_result:
            ok = False
        return segments, sid, ok
