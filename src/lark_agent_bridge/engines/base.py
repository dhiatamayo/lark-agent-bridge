"""Engine adapter base class and binary resolution.

The bot is engine-agnostic: each backend (kiro-cli, antigravity agy) is an
adapter that knows how to (a) build its CLI command and (b) parse its
stream-json output into text segments. The rest of the bot never cares which
backend answers.
"""

from __future__ import annotations

import shutil
from pathlib import Path


class Engine:
    """Base adapter. Subclasses implement :meth:`build_cmd` and :meth:`parse`."""

    name = "base"

    def __init__(self, binary: str):
        self.binary = binary

    def build_cmd(self, prompt: str, session_id: str | None) -> list[str]:
        """Return the full argv list to invoke the CLI for *prompt*."""
        raise NotImplementedError

    def parse(self, stdout: str) -> tuple[list[str], str | None, bool]:
        """Parse CLI stdout.

        Returns ``(text_segments, session_id, ok)`` where *text_segments* are
        the agent's answer chunks, *session_id* is the conversation id to resume
        with (or None), and *ok* indicates success.
        """
        raise NotImplementedError


def resolve_binary(explicit: str, *candidates: str) -> str | None:
    """Resolve a CLI binary: honour *explicit* if set, else search PATH.

    *explicit* may be an absolute path (checked for existence) or a bare name
    (looked up on PATH). *candidates* are PATH names tried in order.
    """
    if explicit:
        return explicit if Path(explicit).exists() else shutil.which(explicit)
    for cand in candidates:
        found = shutil.which(cand)
        if found:
            return found
    return None
