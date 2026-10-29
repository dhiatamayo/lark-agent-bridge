"""Knowledge retrieval package (Postgres FTS)."""

from .chunker import Chunk, chunk_markdown
from .service import KnowledgeService, build_knowledge
from .store import Hit, KnowledgeStore

__all__ = [
    "Chunk",
    "chunk_markdown",
    "KnowledgeService",
    "build_knowledge",
    "Hit",
    "KnowledgeStore",
]
