"""Split markdown knowledge files into retrievable chunks.

Pure functions (no DB, no I/O): easy to unit-test. A *chunk* is a
``(section, content)`` pair where ``section`` is a human-readable heading path
(e.g. ``"sesi 12 (27-28 Okt) > Review MR 1870"``) and ``content`` is the body
text under that heading.

Chunking strategy:
- Split on ATX headings (``#``..``######``).
- ``section`` carries the heading *path* (parent headings joined with " > ")
  so a chunk keeps its context even when retrieved in isolation.
- Oversized chunks (> ``max_chars``) are split on paragraph boundaries, each
  piece keeping the same section prefix (with a " (part N)" suffix).
- Blank/heading-only chunks are dropped.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass(frozen=True)
class Chunk:
    section: str
    content: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def _heading_path(stack: list[tuple[int, str]]) -> str:
    """Join the current heading stack into a ' > ' path."""
    return " > ".join(title for _level, title in stack)


def _split_oversized(section: str, body: str, max_chars: int) -> list[Chunk]:
    """Break a long body into paragraph-aligned pieces under *max_chars*."""
    body = body.strip()
    if len(body) <= max_chars:
        return [Chunk(section, body)] if body else []

    paras = re.split(r"\n\s*\n", body)
    pieces: list[str] = []
    buf = ""
    for para in paras:
        para = para.strip()
        if not para:
            continue
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) > max_chars and buf:
            pieces.append(buf)
            buf = para
        else:
            buf = candidate
    if buf:
        pieces.append(buf)

    # A single paragraph still too big: hard-wrap on char boundary.
    final: list[str] = []
    for piece in pieces:
        if len(piece) <= max_chars:
            final.append(piece)
        else:
            for i in range(0, len(piece), max_chars):
                final.append(piece[i : i + max_chars])

    if len(final) == 1:
        return [Chunk(section, final[0])]
    return [Chunk(f"{section} (part {i + 1})", p) for i, p in enumerate(final)]


def chunk_markdown(text: str, *, max_chars: int = 1500) -> list[Chunk]:
    """Split *text* into heading-scoped chunks.

    Content before the first heading is attached to a synthetic
    ``"(intro)"`` section.
    """
    lines = text.splitlines()
    stack: list[tuple[int, str]] = []  # (heading level, title)
    buf: list[str] = []
    chunks: list[Chunk] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        buf.clear()
        if not body:
            return
        section = _heading_path(stack) or "(intro)"
        chunks.extend(_split_oversized(section, body, max_chars))

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            # Pop headings at the same or deeper level, then push this one.
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        else:
            buf.append(line)
    flush()
    return chunks
