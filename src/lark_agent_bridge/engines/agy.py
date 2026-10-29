"""Antigravity CLI (agy) engine adapter — Google's Gemini-CLI successor."""

from __future__ import annotations

import json

from ..config import Settings
from .base import Engine


class AgyEngine(Engine):
    """Drives the antigravity ``agy`` CLI with stream-json output.

    Differences from kiro:
    - prompt MUST be attached to the flag: ``--print=<prompt>`` (``--print
      <prompt>`` would swallow the next flag)
    - resume via ``--conversation <id>``
    - trust is all-or-nothing (``--dangerously-skip-permissions``)
    - stream schema is ``{"event": ...}``; the final ``result`` event already
      carries the whole answer in ``response``.

    Reads mutable options (model, effort, agent_name) from ``settings`` at build
    time so runtime slash-command changes take effect without rebuilding.
    """

    name = "agy"

    def __init__(self, binary: str, settings: Settings):
        super().__init__(binary)
        self.settings = settings

    def build_cmd(self, prompt: str, session_id: str | None) -> list[str]:
        cmd = [self.binary, "--output-format", "stream-json",
               "--dangerously-skip-permissions"]
        if self.settings.agent_name:
            cmd += ["--agent", self.settings.agent_name]
        if self.settings.agy_model:
            cmd += ["--model", self.settings.agy_model]
        if self.settings.agy_effort:
            cmd += ["--effort", self.settings.agy_effort]
        if session_id:
            cmd += ["--conversation", session_id]
        cmd.append(f"--print={prompt}")  # prompt MUST be attached to the flag
        return cmd

    def parse(self, stdout: str) -> tuple[list[str], str | None, bool]:
        sid: str | None = None
        ok = True
        response: str | None = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            etype = event.get("event")
            if etype == "init":
                sid = event.get("conversation_id") or sid
            elif etype == "result":
                res = event.get("result") or {}
                sid = res.get("conversation_id") or sid
                ok = res.get("status") == "SUCCESS"
                response = res.get("response")
        # The result event already carries the whole final answer; prefer it.
        segments = [response.strip()] if response else []
        return segments, sid, ok

    def progress_label(self, line: str) -> str | None:
        try:
            event = json.loads(line)
        except ValueError:
            return None
        etype = event.get("event")
        if etype == "result":
            return "✅ finishing up…"
        if etype == "init":
            return "🚀 starting…"
        if isinstance(etype, str) and "tool" in etype:
            return "🔧 running tool…"
        if isinstance(etype, str) and ("message" in etype or "content" in etype):
            return "📝 writing answer…"
        return None
