"""Kiro CLI (kiro-cli) engine adapter."""

from __future__ import annotations

import json

from ..config import Settings
from .base import Engine


class KiroEngine(Engine):
    """Drives ``kiro-cli chat`` with stream-json output.

    - prompt is passed as the final positional argument
    - resume via ``--resume-id <id>``
    - granular tool trust via ``--trust-tools=`` / ``--trust-all-tools``
    - stream schema: ``{"type": ..., "data": {...}}`` where text arrives as
      ``sessionUpdate`` -> ``agent_message_chunk`` -> ``content.text`` and the
      run ends with a ``runFinished`` event carrying ``status``.

    Reads mutable options (trust_tools, agent_name) from ``settings`` at build
    time so runtime slash-command changes take effect without rebuilding.
    """

    name = "kiro"

    def __init__(self, binary: str, settings: Settings):
        super().__init__(binary)
        self.settings = settings

    def build_cmd(self, prompt: str, session_id: str | None) -> list[str]:
        cmd = [self.binary, "chat", "--output-format", "stream-json"]
        trust = self.settings.trust_tools
        if trust == "all":
            cmd.append("--trust-all-tools")
        elif trust:
            cmd.append(f"--trust-tools={trust}")
        if self.settings.agent_name:
            cmd += ["--agent", self.settings.agent_name]
        if session_id:
            cmd += ["--resume-id", session_id]
        cmd.append(prompt)  # kiro takes the prompt as the final positional arg
        return cmd

    def parse(self, stdout: str) -> tuple[list[str], str | None, bool]:
        segments: list[str] = []
        current: list[str] = []
        sid: str | None = None
        finished: dict | None = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            data = event.get("data") or {}
            sid = data.get("sessionId") or sid
            if event.get("type") == "runFinished":
                finished = data
            if event.get("type") != "sessionUpdate":
                continue
            update = data.get("update") or {}
            kind = update.get("sessionUpdate")
            if kind == "agent_message_chunk":
                content = update.get("content") or {}
                if content.get("type") == "text":
                    current.append(content.get("text", ""))
            elif kind == "tool_call" and current:
                segments.append("".join(current))  # a tool call ends the segment
                current = []
        if current:
            segments.append("".join(current))
        ok = finished is None or finished.get("status") == "success"
        return [s.strip() for s in segments if s.strip()], sid, ok
