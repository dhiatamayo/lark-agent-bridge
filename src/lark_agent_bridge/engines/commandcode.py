"""Command Code CLI (``command-code``) engine adapter.

Drives Command Code (https://commandcode.ai) in headless print mode with the
NDJSON output format (``--output-format json``).

Differences from kiro / agy / copilot:
- prompt is passed as the value of ``-p/--print`` (non-interactive, exits after)
- resume via ``--resume <session-id>`` (explicit id; a bare ``--resume`` errors
  in print mode)
- permissions: headless mode blocks file writes and shell commands unless
  ``--yolo`` is passed. We pass ``--yolo`` only when ``AGENT_TRUST_TOOLS=all``;
  otherwise the run stays read-only (reads, grep, glob).
- stream schema: one JSON object per line. Progress frames look like
  ``{"type": "event", "event": {"type": "tool_running", "toolName": ...}}``.
  The run always ends with ONE ``{"type": "result", ...}`` line carrying
  ``subtype`` (``success`` | ``error`` | ``max_turns``), ``finalText`` and,
  when a session was resolved, ``sessionId``. ``sessionId`` and ``stopReason``
  are optional (omitted when the run fails before a session exists).
- auth: ``cmd login`` once on the host, or ``COMMAND_CODE_API_KEY`` in the
  environment (the CLI reads it directly; the bridge never touches it).

Requires Node.js >= 22 to run the CLI itself (see ``COMMANDCODE_BIN``).

Reads mutable options (model, effort) from ``settings`` at build time so
runtime slash-command changes take effect without rebuilding.
"""

from __future__ import annotations

import json

from ..config import Settings
from .base import Engine


class CommandCodeEngine(Engine):
    """Drives Command Code with ``-p ... --output-format json``."""

    name = "commandcode"

    def __init__(self, binary: str, settings: Settings):
        super().__init__(binary)
        self.settings = settings

    def build_cmd(self, prompt: str, session_id: str | None) -> list[str]:
        cmd = [self.binary, "--output-format", "json",
               "--skip-onboarding", "--no-auto-update"]
        # Headless blocks writes/shell by default; only lift it when the bridge
        # is configured to trust everything (same meaning as kiro's trust=all).
        if self.settings.trust_tools == "all":
            cmd.append("--yolo")
        if self.settings.commandcode_model:
            cmd += ["--model", self.settings.commandcode_model]
        if self.settings.commandcode_effort:
            cmd += ["--effort", self.settings.commandcode_effort]
        if session_id:
            cmd += ["--resume", session_id]
        cmd += ["-p", prompt]
        return cmd

    def parse(self, stdout: str) -> tuple[list[str], str | None, bool]:
        sid: str | None = None
        ok = False
        segments: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "result":
                continue
            # The result line is always last; keep the final one.
            sid = event.get("sessionId") or sid
            ok = event.get("subtype") == "success"
            text = event.get("finalText")
            segments = [text.strip()] if isinstance(text, str) and text.strip() else []
        # No result line => the run was interrupted (ok stays False, no text).
        return segments, sid, ok

    def progress_label(self, line: str) -> str | None:
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        etype = event.get("type")
        if etype == "result":
            return "✅ finishing up…"
        if etype == "event":
            inner = event.get("event") or {}
            if isinstance(inner, dict) and inner.get("type") == "tool_running":
                return f"🔧 {inner.get('toolName') or 'tool'}"
        return None
