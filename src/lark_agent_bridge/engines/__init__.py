"""Agent engine adapters and factory."""

from __future__ import annotations

from ..config import Settings
from .agy import AgyEngine
from .base import Engine, resolve_binary
from .commandcode import CommandCodeEngine
from .copilot import CopilotEngine
from .kiro import KiroEngine

__all__ = ["Engine", "KiroEngine", "AgyEngine", "CopilotEngine", "CommandCodeEngine", "build_engine", "resolve_binary", "EngineNotFound"]


class EngineNotFound(RuntimeError):
    """Raised when a requested engine's binary cannot be located."""


def build_engine(name: str, settings: Settings) -> Engine:
    """Construct an :class:`Engine` by *name* using *settings*.

    Raises :class:`EngineNotFound` when the binary is missing, or ``ValueError``
    for an unknown engine name.
    """
    name = name.strip().lower()
    if name == "agy":
        binary = resolve_binary(settings.agy_bin, "agy")
        if not binary:
            raise EngineNotFound("agy (antigravity CLI) not found on PATH. Set AGY_BIN or install it.")
        return AgyEngine(binary, settings)
    if name == "kiro":
        binary = resolve_binary(settings.kiro_bin, "kiro-cli")
        if not binary:
            raise EngineNotFound("kiro-cli not found on PATH. Set KIRO_BIN or install Kiro CLI.")
        return KiroEngine(binary, settings)
    if name == "copilot":
        binary = resolve_binary(settings.copilot_bin, "copilot")
        if not binary:
            raise EngineNotFound("copilot (GitHub Copilot CLI) not found on PATH. Set COPILOT_BIN or install it.")
        return CopilotEngine(binary, settings)
    if name == "commandcode":
        binary = resolve_binary(settings.commandcode_bin, "command-code", "commandcode")
        if not binary:
            raise EngineNotFound("command-code (Command Code CLI) not found on PATH. Set COMMANDCODE_BIN or install it (npm i -g command-code).")
        return CommandCodeEngine(binary, settings)
    raise ValueError(f"Unknown engine {name!r}. Use 'kiro', 'agy', 'copilot', or 'commandcode'.")
