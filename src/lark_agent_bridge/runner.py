"""The agent runner: owns the active engine, per-engine conversation sessions,
and executes the CLI subprocess for a prompt.
"""

from __future__ import annotations

import os
import subprocess
import threading

from .config import Settings
from .engines import Engine, EngineNotFound, build_engine
from .lark.messages import strip_ansi


class AgentRunner:
    """Serialises agent runs (one at a time) and tracks per-engine state."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()  # one agent run at a time
        # Per-engine conversation id so sessions never bleed across engines or
        # into sessions you run yourself in the same folder.
        self.session_ids: dict[str, str | None] = {"kiro": None, "agy": None}
        # One-shot context preamble injected on the first prompt after a switch.
        self._pending_context: dict[str, str] = {}
        # Validate the default engine now so a bad config fails loudly.
        self.engine: Engine = build_engine(settings.default_engine, settings)

    # -- engine switching -------------------------------------------------- #
    def switch_engine(self, name: str) -> Engine:
        """Switch the active engine, resetting its session. May raise
        :class:`EngineNotFound` / ``ValueError`` (caller reports to chat)."""
        new_engine = build_engine(name, self.settings)
        with self._lock:
            self.engine = new_engine
            self.session_ids[new_engine.name] = None  # fresh session on switch
        return new_engine

    def queue_context(self, engine_name: str, context: str) -> None:
        """Queue a one-shot context preamble for *engine_name*'s next prompt."""
        if context:
            self._pending_context[engine_name] = context

    def reset_session(self) -> None:
        with self._lock:
            self.session_ids[self.engine.name] = None

    # -- run --------------------------------------------------------------- #
    def run(self, prompt: str) -> str:
        """Run the active engine for *prompt* and return the reply text."""
        with self._lock:
            eng = self.engine
            sid = self.session_ids.get(eng.name)

            preamble = self._pending_context.pop(eng.name, "")
            if preamble:
                prompt = preamble + "\n\n" + prompt

            cmd = eng.build_cmd(prompt, sid)
            env = os.environ.copy()
            env["NO_COLOR"] = "1"
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=self.settings.workdir,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.settings.timeout_sec,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                return f"{eng.name} timed out after {self.settings.timeout_sec}s."

            segments, new_sid, ok = eng.parse(proc.stdout or "")
            if new_sid:
                self.session_ids[eng.name] = new_sid

        if proc.returncode != 0 or not ok:
            err = strip_ansi(proc.stderr or "").strip()
            partial = "\n\n".join(segments)
            status = "error" if proc.returncode == 0 else f"exit code {proc.returncode}"
            return f"{eng.name} failed ({status}).\n\n{err or partial}"[: self.settings.max_chunk * 3]
        if not segments:
            return f"({eng.name} returned no output)"
        return segments[-1] if self.settings.reply_mode == "final" else "\n\n".join(segments)


__all__ = ["AgentRunner", "EngineNotFound"]
